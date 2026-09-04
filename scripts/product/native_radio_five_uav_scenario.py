#!/usr/bin/env python3
"""Exercise five real dual-UART SITLs through one native ns-3/Sionna radio."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import selectors
import signal
import socket
import sys
import time
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from network.scripts.data_transport import (  # noqa: E402
    DataProtocolError,
    decode as decode_data,
    encode as encode_data,
)
from scripts.product.town01_full_stack_scenario import (  # noqa: E402
    FlightHarness,
    ScenarioError,
    UAV_IDS,
    endpoint_ip,
    write_json,
)


GCS_IP = "10.71.0.10"
P2P_PORT = 14800
MULTICAST_GROUP = "239.71.0.1"
MULTICAST_PORT = 14900
RETRY_CHARACTERIZATION_CANDIDATES_S = (0.5, 1.0, 3.0)
DIAGNOSTIC_OPERATIONS_PER_UAV = 10
DEFAULT_SCENARIO_CONFIG = ROOT / "network/config/scenario_5uav_town01_native_product.yaml"


def load_flight_plan(path: Path) -> dict[str, Any]:
    """Load and validate the product mission from the selected scenario YAML."""

    value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(value, dict):
        raise ScenarioError(f"scenario config must contain a mapping: {path}")
    flight = value.get("flight") or {}
    if not isinstance(flight, dict):
        raise ScenarioError(f"flight config must contain a mapping: {path}")

    takeoff: dict[int, float] = {}
    for name, altitude in (flight.get("takeoff_relative_altitude_m") or {}).items():
        text = str(name)
        if not text.startswith("uav") or not text[3:].isdigit():
            raise ScenarioError(f"invalid takeoff UAV key: {name!r}")
        system_id = int(text[3:])
        takeoff[system_id] = float(altitude)
    if takeoff and set(takeoff) != set(UAV_IDS):
        raise ScenarioError("takeoff_relative_altitude_m must define all five UAVs")

    missions: dict[int, list[dict[str, Any]]] = {}
    phase_names: set[str] = set()
    for name, entries in (flight.get("missions") or {}).items():
        text = str(name)
        if not text.startswith("uav") or not text[3:].isdigit():
            raise ScenarioError(f"invalid mission UAV key: {name!r}")
        system_id = int(text[3:])
        if system_id not in UAV_IDS or not isinstance(entries, list) or not entries:
            raise ScenarioError(f"invalid mission for {name}")
        normalized: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ScenarioError(f"mission item for {name} must be a mapping")
            phase = str(entry.get("name", "")).strip()
            position = entry.get("position_m")
            if not phase or not isinstance(position, list) or len(position) != 3:
                raise ScenarioError(f"mission item for {name} needs name and position_m[3]")
            point = [float(component) for component in position]
            if not all(math.isfinite(component) for component in point):
                raise ScenarioError(f"mission point for {name}/{phase} is not finite")
            hold_s = float(entry.get("hold_s", 0.0))
            if hold_s < 0.0:
                raise ScenarioError(f"mission hold for {name}/{phase} must be non-negative")
            normalized.append({"name": phase, "position_m": point, "hold_s": hold_s})
            phase_names.add(phase)
        missions[system_id] = normalized

    observations: list[dict[str, Any]] = []
    for entry in flight.get("observations") or []:
        if not isinstance(entry, dict):
            raise ScenarioError("flight observation must be a mapping")
        name = str(entry.get("name", "")).strip()
        if not name or name not in phase_names:
            raise ScenarioError(f"observation {name!r} has no matching mission point")
        targets = {
            system_id: next(
                item["position_m"] for item in items if item["name"] == name
            )
            for system_id, items in missions.items()
            if any(item["name"] == name for item in items)
        }
        if not targets:
            raise ScenarioError(f"observation {name!r} has no UAV targets")
        observations.append(
            {
                "name": name,
                "timeout_s": float(entry.get("timeout_s", 240.0)),
                "probe_packets_per_uav": int(entry.get("probe_packets_per_uav", 0)),
                "targets": targets,
            }
        )

    traffic = value.get("traffic") or {}
    simultaneous = traffic.get("simultaneous_uplink") if isinstance(traffic, dict) else None
    delivery_gates = traffic.get("delivery_gates") if isinstance(traffic, dict) else None
    if not isinstance(traffic, dict) or not isinstance(simultaneous, dict):
        raise ScenarioError(f"traffic and traffic.simultaneous_uplink must be mappings: {path}")
    if not isinstance(delivery_gates, dict):
        raise ScenarioError(f"traffic.delivery_gates must be a mapping: {path}")
    normalized_traffic = {
        "diagnostic_retry_interval_s": float(traffic["diagnostic_retry_interval_s"]),
        "forced_mavlink_stream_intervals": traffic["forced_mavlink_stream_intervals"],
        "p2p_packets_per_direction_per_uav": int(
            traffic["p2p_packets_per_direction_per_uav"]
        ),
        "p2mp_root_transmissions": int(traffic["p2mp_root_transmissions"]),
        "simultaneous_uplink": {
            "packets_per_uav": int(simultaneous["packets_per_uav"]),
            "packet_payload_bytes": int(simultaneous["packet_payload_bytes"]),
            "interval_ms": float(simultaneous["interval_ms"]),
            "duration_s": float(simultaneous["duration_s"]),
            "retransmissions": simultaneous["retransmissions"],
        },
        "delivery_gates": {
            "p2p_min_delivered_per_direction_per_uav": int(
                delivery_gates["p2p_min_delivered_per_direction_per_uav"]
            ),
            "p2mp_min_delivered_per_uav": int(
                delivery_gates["p2mp_min_delivered_per_uav"]
            ),
            "simultaneous_min_delivered_per_uav": int(
                delivery_gates["simultaneous_min_delivered_per_uav"]
            ),
            "simultaneous_jain_fairness_min": float(
                delivery_gates["simultaneous_jain_fairness_min"]
            ),
        },
    }
    positive_integer_fields = (
        "p2p_packets_per_direction_per_uav",
        "p2mp_root_transmissions",
    )
    if any(normalized_traffic[name] <= 0 for name in positive_integer_fields):
        raise ScenarioError(f"traffic packet counts must be positive: {path}")
    simultaneous_runtime = normalized_traffic["simultaneous_uplink"]
    if (
        normalized_traffic["diagnostic_retry_interval_s"] <= 0.0
        or normalized_traffic["forced_mavlink_stream_intervals"] is not False
        or simultaneous_runtime["packets_per_uav"] <= 0
        or simultaneous_runtime["packet_payload_bytes"] <= 0
        or simultaneous_runtime["interval_ms"] <= 0.0
        or simultaneous_runtime["duration_s"] <= 0.0
        or simultaneous_runtime["retransmissions"] is not False
    ):
        raise ScenarioError(f"invalid traffic product contract: {path}")
    gates = normalized_traffic["delivery_gates"]
    if (
        not 0 <= gates["p2p_min_delivered_per_direction_per_uav"]
        <= normalized_traffic["p2p_packets_per_direction_per_uav"]
        or not 0 <= gates["p2mp_min_delivered_per_uav"]
        <= normalized_traffic["p2mp_root_transmissions"]
        or not 0 <= gates["simultaneous_min_delivered_per_uav"]
        <= simultaneous_runtime["packets_per_uav"]
        or not 0.0 <= gates["simultaneous_jain_fairness_min"] <= 1.0
    ):
        raise ScenarioError(f"invalid traffic delivery gates: {path}")

    return {
        "scenario_name": str((value.get("scenario") or {}).get("name", path.stem)),
        "map": dict((value.get("scenario") or {}).get("map") or {}),
        "takeoff": takeoff,
        "missions": missions,
        "observations": observations,
        "position_tolerance_m": float(flight.get("mission_position_tolerance_m", 8.0)),
        "causal_expectation": dict(flight.get("causal_expectation") or {}),
        "hold_time_s": float(flight.get("hold_time_s", 0.0)),
        "traffic": normalized_traffic,
    }


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, allow_nan=False, sort_keys=True) + "\n")


def run_additional_agent(args: argparse.Namespace) -> int:
    """Endpoint-only traffic source/sink; there is no ns-3 echo or retry logic."""

    index = int(args.index)
    traffic = load_flight_plan(Path(args.scenario_config).resolve())["traffic"]
    p2p_packets = traffic["p2p_packets_per_direction_per_uav"]
    simultaneous = traffic["simultaneous_uplink"]
    simultaneous_packets = simultaneous["packets_per_uav"]
    simultaneous_interval_ns = int(round(simultaneous["interval_ms"] * 1e6))
    simultaneous_payload_bytes = simultaneous["packet_payload_bytes"]
    local_ip = endpoint_ip(index)
    schedule_file = Path(args.schedule_file)
    event_log = Path(args.event_log)
    stop = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    p2p = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    p2p.bind((local_ip, P2P_PORT + index))
    p2p.setblocking(False)
    multicast = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    multicast.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    multicast.bind(("0.0.0.0", MULTICAST_PORT))
    multicast.setsockopt(
        socket.IPPROTO_IP,
        socket.IP_ADD_MEMBERSHIP,
        socket.inet_aton(MULTICAST_GROUP) + socket.inet_aton(local_ip),
    )
    multicast.setblocking(False)
    selector = selectors.DefaultSelector()
    selector.register(p2p, selectors.EVENT_READ, "p2p")
    selector.register(multicast, selectors.EVENT_READ, "p2mp")
    write_json(
        Path(args.ready_file),
        {
            "status": "ready",
            "pid": os.getpid(),
            "uav_id": index,
            "p2p_endpoint": f"{local_ip}:{P2P_PORT + index}",
            "p2mp_endpoint": f"{MULTICAST_GROUP}:{MULTICAST_PORT}",
        },
    )
    p2p_sent = False
    simultaneous_sent = False
    append_jsonl(event_log, {"event": "start", "uav": index, "monotonic_ns": time.monotonic_ns()})
    try:
        while not stop:
            for key, _mask in selector.select(0.01):
                datagram, source = key.fileobj.recvfrom(65535)
                now_ns = time.monotonic_ns()
                try:
                    message = decode_data(datagram)
                except DataProtocolError as error:
                    append_jsonl(
                        event_log,
                        {"event": "malformed", "error": str(error), "monotonic_ns": now_ns},
                    )
                    continue
                append_jsonl(
                    event_log,
                    {
                        "event": "receive",
                        "kind": message.kind,
                        "uav": index,
                        "sequence": message.sequence,
                        "sender_id": message.sender_id,
                        "receiver_id": message.receiver_id,
                        "source_monotonic_ns": message.sent_monotonic_ns,
                        "received_monotonic_ns": now_ns,
                        "latency_ms": (now_ns - message.sent_monotonic_ns) / 1e6,
                        "payload_length": len(message.payload),
                        "checksum": message.checksum,
                        "sha256": hashlib.sha256(datagram).hexdigest(),
                        "source": f"{source[0]}:{source[1]}",
                    },
                )
                if (
                    key.data == "p2p"
                    and message.kind == "p2p_downlink"
                    and message.sender_id == 0
                    and message.receiver_id == index
                ):
                    # A single endpoint ACK makes delivery observable at the
                    # application boundary.  It is deliberately un-retried and
                    # crosses the same native Wi-Fi/Sionna medium on the return
                    # path; ns-3 never synthesizes application success.
                    response = encode_data(
                        "p2p_downlink_ack",
                        sender_id=index,
                        receiver_id=0,
                        sequence=message.sequence,
                        payload=f"{message.checksum:08x}".encode(),
                    )
                    p2p.sendto(response, (GCS_IP, P2P_PORT))
                    append_jsonl(
                        event_log,
                        {
                            "event": "transmit",
                            "kind": "p2p_downlink_ack",
                            "uav": index,
                            "sequence": message.sequence,
                            "bytes": len(response),
                            "monotonic_ns": time.monotonic_ns(),
                        },
                    )

            try:
                schedule = json.loads(schedule_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                schedule = {}
            now_ns = time.monotonic_ns()
            p2p_start_ns = int(schedule.get("p2p_uplink_start_monotonic_ns", 0) or 0)
            if p2p_start_ns and not p2p_sent and now_ns >= p2p_start_ns:
                for sequence in range(p2p_packets):
                    target_ns = p2p_start_ns + (index - 1) * 25_000_000 + sequence * 100_000_000
                    while time.monotonic_ns() < target_ns and not stop:
                        time.sleep(0.001)
                    datagram = encode_data(
                        "p2p_uplink",
                        sender_id=index,
                        receiver_id=0,
                        sequence=sequence,
                        payload=f"native-five-p2p-uplink-uav{index}-{sequence}".encode(),
                    )
                    message = decode_data(datagram)
                    p2p.sendto(datagram, (GCS_IP, P2P_PORT))
                    append_jsonl(
                        event_log,
                        {
                            "event": "transmit",
                            "kind": "p2p_uplink",
                            "uav": index,
                            "sequence": sequence,
                            "source_monotonic_ns": message.sent_monotonic_ns,
                            "payload_length": len(message.payload),
                            "bytes": len(datagram),
                            "checksum": message.checksum,
                            "monotonic_ns": time.monotonic_ns(),
                        },
                    )
                p2p_sent = True

            simultaneous_start_ns = int(schedule.get("simultaneous_start_monotonic_ns", 0) or 0)
            if simultaneous_start_ns and not simultaneous_sent and now_ns >= simultaneous_start_ns:
                for sequence in range(simultaneous_packets):
                    target_ns = simultaneous_start_ns + sequence * simultaneous_interval_ns
                    while time.monotonic_ns() < target_ns and not stop:
                        time.sleep(0.001)
                    payload = f"native-five-simultaneous-uplink-uav{index}-{sequence}".encode()
                    payload = payload.ljust(simultaneous_payload_bytes, b".")
                    datagram = encode_data(
                        "simultaneous_uplink",
                        sender_id=index,
                        receiver_id=0,
                        sequence=sequence,
                        payload=payload,
                    )
                    message = decode_data(datagram)
                    p2p.sendto(datagram, (GCS_IP, P2P_PORT))
                    append_jsonl(
                        event_log,
                        {
                            "event": "transmit",
                            "kind": "simultaneous_uplink",
                            "uav": index,
                            "sequence": sequence,
                            "source_monotonic_ns": message.sent_monotonic_ns,
                            "payload_length": len(message.payload),
                            "bytes": len(datagram),
                            "checksum": message.checksum,
                            "scheduled_monotonic_ns": target_ns,
                            "monotonic_ns": time.monotonic_ns(),
                        },
                    )
                simultaneous_sent = True
    finally:
        append_jsonl(event_log, {"event": "stop", "uav": index, "monotonic_ns": time.monotonic_ns()})
        selector.close()
        p2p.close()
        multicast.close()
    return 0


class NativeFiveUavHarness(FlightHarness):
    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__(Path(args.run_dir).resolve(), Path(args.node_state).resolve(), args.timeout_scale)
        self.phase_file = Path(args.phase_file).resolve()
        self.schedule_file = Path(args.schedule_file).resolve()
        self.scenario_config = Path(
            getattr(args, "scenario_config", None) or DEFAULT_SCENARIO_CONFIG
        ).resolve()
        self.flight_plan = load_flight_plan(self.scenario_config)
        self.traffic: dict[str, Any] = self.flight_plan["traffic"]
        self.simultaneous: dict[str, Any] = self.traffic["simultaneous_uplink"]
        self.diagnostic_retry_interval_s = self.traffic["diagnostic_retry_interval_s"]
        self.p2p_packets = self.traffic["p2p_packets_per_direction_per_uav"]
        self.p2mp_roots = self.traffic["p2mp_root_transmissions"]
        self.simultaneous_packets = self.simultaneous["packets_per_uav"]
        self.simultaneous_interval_ns = int(round(self.simultaneous["interval_ms"] * 1e6))
        self.simultaneous_payload_bytes = self.simultaneous["packet_payload_bytes"]
        self.takeoff_altitudes: dict[int, float] = self.flight_plan["takeoff"]
        self.flight_missions: dict[int, list[dict[str, Any]]] = self.flight_plan["missions"]
        self.flight_observations: list[dict[str, Any]] = self.flight_plan["observations"]
        reported_missions = {
            f"uav{system_id}": waypoints
            for system_id, waypoints in self.flight_missions.items()
        }
        reported_observations = [
            {
                **observation,
                "targets": {
                    f"uav{system_id}": target
                    for system_id, target in observation["targets"].items()
                },
            }
            for observation in self.flight_observations
        ]
        self.summary.update(
            {
                "profile": args.radio_profile,
                "technology_specific_modem": args.radio_profile.startswith("native_wifi_"),
                "scenario_config": str(self.scenario_config),
                "scenario_name": self.flight_plan["scenario_name"],
                "map": self.flight_plan["map"],
                "predeclared_parameters": {
                    "p2p_packets_per_direction_per_uav": self.p2p_packets,
                    "p2mp_root_transmissions": self.p2mp_roots,
                    "simultaneous_packets_per_uav": self.simultaneous_packets,
                    "simultaneous_interval_ms": self.simultaneous["interval_ms"],
                    "simultaneous_payload_bytes": self.simultaneous_payload_bytes,
                    "diagnostic_retry_interval_s": self.diagnostic_retry_interval_s,
                    "hold_time_s": self.flight_plan["hold_time_s"],
                    "delivery_gates": self.traffic["delivery_gates"],
                    "traffic": self.traffic,
                    "forced_mavlink_stream_intervals": False,
                    "flight_missions": reported_missions,
                    "flight_observations": reported_observations,
                    "takeoff_relative_altitudes_m": {
                        f"uav{system_id}": altitude
                        for system_id, altitude in self.takeoff_altitudes.items()
                    },
                    "causal_expectation": self.flight_plan["causal_expectation"],
                },
            }
        )

    def phase(self, name: str) -> None:
        temporary = self.phase_file.with_name(self.phase_file.name + ".tmp")
        temporary.write_text(name + "\n", encoding="utf-8")
        os.replace(temporary, self.phase_file)
        self.event("phase", detail=name)

    def observe_for(self, duration_s: float) -> None:
        deadline = time.monotonic() + duration_s
        while time.monotonic() < deadline:
            self.pump(min(0.2, deadline - time.monotonic()))

    def command_operation(
        self,
        *,
        channel: str,
        system_id: int,
        command: int,
        params: list[float],
        label: str,
        retry_policy: str,
        retry_interval_s: float | None,
        maximum_attempts: int,
        timeout_s: float,
    ) -> dict[str, Any]:
        """Record attempts separately from successful-attempt and recovery time."""

        if retry_policy not in {"one_shot", "characterization"}:
            raise ValueError(f"unsupported command retry policy: {retry_policy}")
        if retry_policy == "one_shot" and maximum_attempts != 1:
            raise ValueError("one_shot command policy permits exactly one attempt")
        operation_id = f"op-{len(self.summary.setdefault('command_operations', [])) + 1:05d}"
        operation: dict[str, Any] = {
            "operation_id": operation_id,
            "uav": f"uav{system_id}",
            "channel": channel,
            "command": command,
            "label": label,
            "retry_policy": retry_policy,
            "retry_interval_s": retry_interval_s,
            "maximum_attempts": maximum_attempts,
            "attempt_count": 0,
            "attempts": [],
            "first_attempt_success": False,
            "first_attempt_rtt_ms": None,
            "successful_attempt": None,
            "successful_attempt_rtt_ms": None,
            "time_to_success_ms": None,
            "retry_count": 0,
            "timeout_count": 0,
            "deprecated_ambiguous_metric": True,
        }
        self.summary["command_operations"].append(operation)
        accepted = {
            int(self.mavutil.mavlink.MAV_RESULT_ACCEPTED),
            int(self.mavutil.mavlink.MAV_RESULT_IN_PROGRESS),
        }
        started_ns = time.monotonic_ns()
        for attempt_number in range(1, maximum_attempts + 1):
            attempt_id = f"{operation_id}-a{attempt_number}"
            sent_ns = time.monotonic_ns()
            message = self.transmitters[(channel, system_id)].command_long_encode(
                system_id, 1, command, attempt_number - 1, *params
            )
            self.send(channel, system_id, message)
            attempt: dict[str, Any] = {
                "attempt_id": attempt_id,
                "confirmation": attempt_number - 1,
                "command_frame_hex": bytes(message.get_msgbuf()).hex(),
                "send_monotonic_ns": sent_ns,
                "uav_uart_delivery_monotonic_ns": None,
                "uav_uart_delivery_unavailable_reason": (
                    "resolved only by post-run adapter-frame correlation; no synthetic timestamp"
                ),
                "ack_uav_uart_observed_monotonic_ns": None,
                "ack_uav_uart_unavailable_reason": (
                    "COMMAND_ACK carries no request confirmation; a delayed ACK cannot be assigned "
                    "to a retry without a transport correlation"
                ),
                "ack_gcs_received_monotonic_ns": None,
                "ack_gcs_uart_delivery_monotonic_ns": None,
                "ack_gcs_uart_unavailable_reason": (
                    "the GCS endpoint is UDP-only in this topology; the scenario receive boundary is recorded instead"
                ),
                "attempt_rtt_ms": None,
                "outcome": "timeout",
            }
            operation["attempts"].append(attempt)
            operation["attempt_count"] = attempt_number
            deadline = time.monotonic() + timeout_s * self.timeout_scale
            while time.monotonic() < deadline:
                self.pump(0.1)
                ack = self.acks.get((channel, system_id, command))
                if ack is None or ack[1] < sent_ns:
                    continue
                if int(ack[0].result) not in accepted:
                    attempt["outcome"] = "command_rejected"
                    return operation
                received_ns = int(ack[1])
                rtt_ms = (received_ns - sent_ns) / 1e6
                attempt.update(
                    {
                        "ack_gcs_received_monotonic_ns": received_ns,
                        "ack_frame_hex": bytes(ack[0].get_msgbuf()).hex(),
                        "attempt_rtt_ms": rtt_ms,
                        "outcome": "ack_received",
                    }
                )
                operation.update(
                    {
                        "first_attempt_success": attempt_number == 1,
                        "first_attempt_rtt_ms": rtt_ms if attempt_number == 1 else None,
                        "successful_attempt": attempt_number,
                        "successful_attempt_rtt_ms": rtt_ms,
                        "time_to_success_ms": (received_ns - started_ns) / 1e6,
                        "retry_count": attempt_number - 1,
                    }
                )
                return operation
            operation["timeout_count"] += 1
            if attempt_number < maximum_attempts and retry_interval_s is not None:
                self.observe_for(retry_interval_s)
        return operation

    def parallel_one_shot_operations(
        self,
        *,
        systems: tuple[int, ...],
        command: int,
        params: list[float],
        round_number: int,
        timeout_s: float,
    ) -> list[dict[str, Any]]:
        """Send one safe request per UAV before waiting for any response.

        Each UAV has exactly one outstanding MAV_CMD_REQUEST_MESSAGE.  Different
        UAVs are deliberately sent back-to-back so this is a genuine parallel
        offered-load round rather than a loop of sequential measurements.
        """

        accepted = {
            int(self.mavutil.mavlink.MAV_RESULT_ACCEPTED),
            int(self.mavutil.mavlink.MAV_RESULT_IN_PROGRESS),
        }
        operations: list[dict[str, Any]] = []
        for system_id in systems:
            operation_id = f"op-{len(self.summary.setdefault('command_operations', [])) + 1:05d}"
            sent_ns = time.monotonic_ns()
            attempt = {
                "attempt_id": f"{operation_id}-a1",
                "confirmation": 0,
                "send_monotonic_ns": sent_ns,
                "uav_uart_delivery_monotonic_ns": None,
                "uav_uart_delivery_unavailable_reason": (
                    "resolved only by post-run adapter-frame correlation; no synthetic timestamp"
                ),
                "ack_uav_uart_observed_monotonic_ns": None,
                "ack_uav_uart_unavailable_reason": (
                    "COMMAND_ACK carries no request confirmation; a delayed ACK cannot be assigned "
                    "without a transport correlation"
                ),
                "ack_gcs_received_monotonic_ns": None,
                "ack_gcs_uart_delivery_monotonic_ns": None,
                "ack_gcs_uart_unavailable_reason": (
                    "the GCS endpoint is UDP-only in this topology; the scenario receive boundary is recorded instead"
                ),
                "attempt_rtt_ms": None,
                "outcome": "timeout",
            }
            operation: dict[str, Any] = {
                "operation_id": operation_id,
                "uav": f"uav{system_id}",
                "channel": "control",
                "command": command,
                "label": f"one_shot_parallel_round_{round_number}",
                "retry_policy": "one_shot",
                "retry_interval_s": None,
                "maximum_attempts": 1,
                "attempt_count": 1,
                "attempts": [attempt],
                "first_attempt_success": False,
                "first_attempt_rtt_ms": None,
                "successful_attempt": None,
                "successful_attempt_rtt_ms": None,
                "time_to_success_ms": None,
                "retry_count": 0,
                "timeout_count": 0,
                "deprecated_ambiguous_metric": True,
            }
            self.summary["command_operations"].append(operation)
            message = self.transmitters[("control", system_id)].command_long_encode(
                system_id, 1, command, 0, *params
            )
            self.send("control", system_id, message)
            attempt["command_frame_hex"] = bytes(message.get_msgbuf()).hex()
            operations.append(operation)

        deadline = time.monotonic() + timeout_s * self.timeout_scale
        pending = {int(operation["uav"].removeprefix("uav")): operation for operation in operations}
        while pending and time.monotonic() < deadline:
            self.pump(min(0.1, deadline - time.monotonic()))
            for system_id, operation in tuple(pending.items()):
                attempt = operation["attempts"][0]
                ack = self.acks.get(("control", system_id, command))
                if ack is None or ack[1] < attempt["send_monotonic_ns"]:
                    continue
                received_ns = int(ack[1])
                if int(ack[0].result) in accepted:
                    rtt_ms = (received_ns - attempt["send_monotonic_ns"]) / 1e6
                    attempt.update(
                        {
                            "ack_gcs_received_monotonic_ns": received_ns,
                            "ack_frame_hex": bytes(ack[0].get_msgbuf()).hex(),
                            "attempt_rtt_ms": rtt_ms,
                            "outcome": "ack_received",
                        }
                    )
                    operation.update(
                        {
                            "first_attempt_success": True,
                            "first_attempt_rtt_ms": rtt_ms,
                            "successful_attempt": 1,
                            "successful_attempt_rtt_ms": rtt_ms,
                            "time_to_success_ms": rtt_ms,
                        }
                    )
                else:
                    attempt["outcome"] = "command_rejected"
                del pending[system_id]
        for operation in pending.values():
            operation["timeout_count"] = 1
        return operations

    def ping_diagnostics(self, systems: tuple[int, ...]) -> dict[str, Any]:
        """Measure MAVLink PING if this SITL responds; never fabricate a reply."""

        operations: list[dict[str, Any]] = []
        def send_one(system_id: int, sequence: int) -> None:
            sent_ns = time.monotonic_ns()
            time_usec = sent_ns // 1_000
            message = self.transmitters[("control", system_id)].ping_encode(
                time_usec, system_id * 100 + sequence, system_id, 1
            )
            self.send("control", system_id, message)
            operation: dict[str, Any] = {
                "uav": f"uav{system_id}",
                "sequence": sequence,
                "send_monotonic_ns": sent_ns,
                "time_usec": time_usec,
                "reply_gcs_received_monotonic_ns": None,
                "rtt_ms": None,
                "outcome": "timeout",
            }
            deadline = time.monotonic() + self.timeout_scale
            while time.monotonic() < deadline:
                self.pump(min(0.1, deadline - time.monotonic()))
                reply = self.latest.get(("control", system_id, "PING"))
                reply_ns = self.latest_at_ns.get(("control", system_id, "PING"), 0)
                if reply is None or reply_ns < sent_ns:
                    continue
                if int(reply.time_usec) != time_usec or int(reply.seq) != system_id * 100 + sequence:
                    continue
                operation.update(
                    {
                        "reply_gcs_received_monotonic_ns": reply_ns,
                        "rtt_ms": (reply_ns - sent_ns) / 1e6,
                        "outcome": "reply_received",
                    }
                )
                break
            operations.append(operation)

        # A single real probe per UAV establishes whether this SITL exposes a
        # PING responder.  Only if it does do we run the required 20-per-UAV
        # sample; an unsupported PING must not add 100 seconds of fabricated
        # timeout pressure to an unrelated control-latency diagnosis.
        for system_id in systems:
            send_one(system_id, 0)
        if any(operation["outcome"] == "reply_received" for operation in operations):
            for system_id in systems:
                for sequence in range(1, 20):
                    send_one(system_id, sequence)
        replies = sum(operation["outcome"] == "reply_received" for operation in operations)
        return {
            "attempts_per_uav": 20 if len(operations) == 20 * len(systems) else 1,
            "operations": operations,
            "reply_count": replies,
            "supported": replies > 0,
            "unavailable_reason": (
                None if replies else "the real SITL returned no matching MAVLink PING reply; no response was synthesized"
            ),
        }

    def latency_diagnostics(self, uav_count: int, channels: tuple[str, ...]) -> None:
        """Stationary, non-flight decision-gate commands; no synthetic response is used."""

        request = int(self.mavutil.mavlink.MAV_CMD_REQUEST_MESSAGE)
        version_id = int(self.mavutil.mavlink.MAVLINK_MSG_ID_AUTOPILOT_VERSION)
        systems = tuple(range(1, uav_count + 1))
        self.phase("latency_stationary_warmup")
        self.wait(
            lambda: all((channel, system_id, "HEARTBEAT") in self.latest
                        for channel in channels for system_id in systems),
            90,
            "diagnostic UART heartbeats",
        )
        self.observe_for(30.0)
        self.phase("latency_one_shot_sequential")
        for system_id in systems:
            for sequence in range(DIAGNOSTIC_OPERATIONS_PER_UAV):
                self.command_operation(
                    channel="control", system_id=system_id, command=request,
                    params=[float(version_id), 0, 0, 0, 0, 0, 0],
                    label=f"one_shot_request_message_{sequence}", retry_policy="one_shot",
                    retry_interval_s=None, maximum_attempts=1, timeout_s=1.0,
                )
        self.phase("latency_one_shot_parallel")
        for round_number in range(DIAGNOSTIC_OPERATIONS_PER_UAV):
            self.parallel_one_shot_operations(
                systems=systems, command=request,
                params=[float(version_id), 0, 0, 0, 0, 0, 0],
                round_number=round_number, timeout_s=1.0,
            )
        self.phase("latency_retry_characterization")
        for retry_interval_s in RETRY_CHARACTERIZATION_CANDIDATES_S:
            for system_id in systems:
                self.command_operation(
                    channel="control", system_id=system_id, command=request,
                    params=[float(version_id), 0, 0, 0, 0, 0, 0],
                    label=f"retry_characterization_{retry_interval_s:g}s",
                    retry_policy="characterization", retry_interval_s=retry_interval_s,
                    maximum_attempts=3, timeout_s=1.0,
                )
        self.summary["latency_diagnostic"] = {
            "uav_count": uav_count,
            "channels": list(channels),
            "one_shot_operations_per_uav_per_mode": DIAGNOSTIC_OPERATIONS_PER_UAV,
            "retry_candidates_s": list(RETRY_CHARACTERIZATION_CANDIDATES_S),
            "mavlink_ping": self.ping_diagnostics(systems),
        }

    def native_command_one(
        self,
        channel: str,
        system_id: int,
        command: int,
        params: list[float],
        timeout_s: float,
        label: str,
    ) -> tuple[int, float]:
        accepted = {
            int(self.mavutil.mavlink.MAV_RESULT_ACCEPTED),
            int(self.mavutil.mavlink.MAV_RESULT_IN_PROGRESS),
        }
        sent_at_ns = time.monotonic_ns()
        next_send = 0.0
        deadline = time.monotonic() + timeout_s * self.timeout_scale
        while time.monotonic() < deadline:
            if time.monotonic() >= next_send:
                message = self.transmitters[(channel, system_id)].command_long_encode(
                    system_id, 1, command, 0, *params
                )
                self.send(channel, system_id, message)
                next_send = time.monotonic() + self.diagnostic_retry_interval_s
            self.pump(0.2)
            ack = self.acks.get((channel, system_id, command))
            if ack is None or ack[1] < sent_at_ns or int(ack[0].result) not in accepted:
                continue
            latency = (ack[1] - sent_at_ns) / 1e6
            self.summary["command_acks"].append(
                {
                    "channel": channel,
                    "uav": f"uav{system_id}",
                    "command": command,
                    "label": label,
                    "result": int(ack[0].result),
                    "latency_ms": latency,
                }
            )
            return sent_at_ns, latency
        raise ScenarioError(f"{label} ACK missing for uav{system_id} on {channel}")

    def diagnose_dual_uart(self) -> None:
        request = int(self.mavutil.mavlink.MAV_CMD_REQUEST_MESSAGE)
        version_id = int(self.mavutil.mavlink.MAVLINK_MSG_ID_AUTOPILOT_VERSION)
        attitude_id = int(self.mavutil.mavlink.MAVLINK_MSG_ID_ATTITUDE)
        diagnostics: dict[str, Any] = {}
        for system_id in UAV_IDS:
            uav = f"uav{system_id}"
            control_sent_ns, control_latency = self.native_command_one(
                "control",
                system_id,
                request,
                [float(version_id), 0, 0, 0, 0, 0, 0],
                90,
                "control_autopilot_version_diagnostic",
            )
            self.wait(
                lambda system_id=system_id, sent=control_sent_ns: self.latest_at_ns.get(
                    ("control", system_id, "AUTOPILOT_VERSION"), 0
                )
                >= sent,
                60,
                f"AUTOPILOT_VERSION on uav{system_id} control UART",
            )
            self.wait(
                lambda system_id=system_id, sent=control_sent_ns: self.latest_at_ns.get(
                    ("control", system_id, "HEARTBEAT"), 0
                )
                >= sent,
                60,
                f"post-command telemetry on uav{system_id} control UART",
            )
            control_ack_before_payload = self.acks.get(("control", system_id, request))
            payload_sent_ns, payload_latency = self.native_command_one(
                "payload",
                system_id,
                request,
                [float(attitude_id), 0, 0, 0, 0, 0, 0],
                90,
                "payload_attitude_diagnostic",
            )
            self.wait(
                lambda system_id=system_id, sent=payload_sent_ns: self.latest_at_ns.get(
                    ("payload", system_id, "ATTITUDE"), 0
                )
                >= sent,
                60,
                f"ATTITUDE on uav{system_id} payload UART",
            )
            self.observe_for(0.5)
            control_ack_after_payload = self.acks.get(("control", system_id, request))
            matching_control = bool(
                control_ack_after_payload
                and control_ack_after_payload[1] >= payload_sent_ns
                and control_ack_after_payload != control_ack_before_payload
            )
            if matching_control:
                raise ScenarioError(f"payload request for uav{system_id} produced control ACK")
            diagnostics[uav] = {
                "system_id": system_id,
                "control": {
                    "heartbeat": True,
                    "request": "AUTOPILOT_VERSION",
                    "ack_from_system_id": system_id,
                    "ack_latency_ms": control_latency,
                    "response_received": True,
                    "telemetry_after_command": True,
                },
                "payload": {
                    "heartbeat": True,
                    "request": "ATTITUDE",
                    "ack_from_system_id": system_id,
                    "ack_latency_ms": payload_latency,
                    "response_received": True,
                    "matching_control_ack_observed": False,
                },
                "serial_configuration_evidence": "launch SERIAL1/SERIAL2 UART arguments at 115200 baud",
            }
            self.event("dual_uart_diagnostic", uav=system_id)

        pending = set(UAV_IDS)
        sent_at: dict[int, int] = {}
        latency: dict[int, float] = {}
        next_send = 0.0
        deadline = time.monotonic() + 120 * self.timeout_scale
        while pending and time.monotonic() < deadline:
            if time.monotonic() >= next_send:
                for system_id in pending:
                    message = self.transmitters[("control", system_id)].command_long_encode(
                        system_id, 1, request, 0, float(version_id), 0, 0, 0, 0, 0, 0
                    )
                    self.send("control", system_id, message)
                    sent_at.setdefault(system_id, time.monotonic_ns())
                next_send = time.monotonic() + self.diagnostic_retry_interval_s
            self.pump(0.2)
            for system_id in tuple(pending):
                ack = self.acks.get(("control", system_id, request))
                if ack and ack[1] >= sent_at[system_id]:
                    latency[system_id] = (ack[1] - sent_at[system_id]) / 1e6
                    pending.remove(system_id)
        if pending:
            raise ScenarioError(f"parallel safe request ACK missing for {sorted(pending)}")
        self.summary["dual_uart_diagnostics"] = {
            "sequential": diagnostics,
            "parallel_safe_request": {
                f"uav{system_id}": {"ack_latency_ms": latency[system_id]}
                for system_id in UAV_IDS
            },
        }
        self.event("parallel_safe_request_complete")

    def additional_data_experiments(self) -> None:
        sock = self.sockets["additional_data"]
        self.additional_received = []
        p2p_start_ns = time.monotonic_ns() + 1_500_000_000
        write_json(
            self.schedule_file,
            {
                "p2p_uplink_start_monotonic_ns": p2p_start_ns,
                "simultaneous_start_monotonic_ns": None,
            },
        )
        self.phase("p2p")
        downlink_sends: list[dict[str, Any]] = []
        for sequence in range(self.p2p_packets):
            for system_id in UAV_IDS:
                datagram = encode_data(
                    "p2p_downlink",
                    sender_id=0,
                    receiver_id=system_id,
                    sequence=sequence,
                    payload=f"native-five-p2p-downlink-uav{system_id}-{sequence}".encode(),
                )
                message = decode_data(datagram)
                sock.sendto(datagram, (endpoint_ip(system_id), P2P_PORT + system_id))
                downlink_sends.append(
                    {
                        "uav": f"uav{system_id}",
                        "sequence": sequence,
                        "source_monotonic_ns": message.sent_monotonic_ns,
                        "payload_length": len(message.payload),
                        "bytes": len(datagram),
                        "checksum": message.checksum,
                    }
                )
                self.observe_for(0.03)
        self.observe_for(8.0)
        p2p_received = [
            {
                "uav": f"uav{message.sender_id}",
                "sequence": message.sequence,
                "source_monotonic_ns": message.sent_monotonic_ns,
                "received_monotonic_ns": received_ns,
                "latency_ms": (received_ns - message.sent_monotonic_ns) / 1e6,
                "payload_length": len(message.payload),
                "checksum": message.checksum,
            }
            for message, _source, received_ns in self.additional_received
            if message.kind == "p2p_uplink" and message.sender_id in UAV_IDS
        ]
        self.summary["p2p"] = {
            "retransmissions": False,
            "ns3_echo": False,
            "gcs_originated_packets": len(downlink_sends),
            "uav_originated_packets": self.p2p_packets * len(UAV_IDS),
            "downlink_sends": downlink_sends,
            "uplink_deliveries": p2p_received,
        }

        self.additional_received = []
        self.phase("p2mp")
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(GCS_IP))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 8)
        root_sends: list[dict[str, Any]] = []
        for sequence in range(self.p2mp_roots):
            payload = f"native-five-p2mp-root-{sequence}".encode().ljust(192, b".")
            datagram = encode_data(
                "p2mp_downlink",
                sender_id=0,
                receiver_id=0,
                sequence=sequence,
                payload=payload,
            )
            message = decode_data(datagram)
            sock.sendto(datagram, (MULTICAST_GROUP, MULTICAST_PORT))
            root_sends.append(
                {
                    "sequence": sequence,
                    "source_monotonic_ns": message.sent_monotonic_ns,
                    "payload_length": len(message.payload),
                    "bytes": len(datagram),
                    "checksum": message.checksum,
                }
            )
            self.observe_for(0.25)
        self.observe_for(8.0)
        self.summary["p2mp"] = {
            "root_transmissions": self.p2mp_roots,
            "application_sends": root_sends,
            "application_unicast_copies": 0,
            "ack_required": False,
        }

        self.additional_received = []
        simultaneous_start_ns = time.monotonic_ns() + 2_000_000_000
        self.phase("simultaneous_uplink")
        write_json(
            self.schedule_file,
            {
                "p2p_uplink_start_monotonic_ns": p2p_start_ns,
                "simultaneous_start_monotonic_ns": simultaneous_start_ns,
                "packets_per_uav": self.simultaneous_packets,
                "interval_ns": self.simultaneous_interval_ns,
                "payload_bytes": self.simultaneous_payload_bytes,
            },
        )
        self.observe_for(10.0)
        simultaneous_received = [
            {
                "uav": f"uav{message.sender_id}",
                "sequence": message.sequence,
                "source_monotonic_ns": message.sent_monotonic_ns,
                "received_monotonic_ns": received_ns,
                "latency_ms": (received_ns - message.sent_monotonic_ns) / 1e6,
                "payload_length": len(message.payload),
            }
            for message, _source, received_ns in self.additional_received
            if message.kind == "simultaneous_uplink" and message.sender_id in UAV_IDS
        ]
        self.summary["simultaneous_uplink"] = {
            "predeclared_start_monotonic_ns": simultaneous_start_ns,
            "offered_packets": self.simultaneous_packets * len(UAV_IDS),
            "packets_per_uav": self.simultaneous_packets,
            "packet_payload_bytes": self.simultaneous_payload_bytes,
            "interval_ms": self.simultaneous["interval_ms"],
            "duration_s": self.simultaneous["duration_s"],
            "retransmissions": False,
            "custom_scheduler": False,
            "shaping": False,
            "application_deliveries": simultaneous_received,
        }

    @staticmethod
    def offset_global(lat: float, lon: float, east_m: float, north_m: float) -> tuple[int, int]:
        radius = 6378137.0
        return (
            int(round((lat + math.degrees(north_m / radius)) * 1e7)),
            int(
                round(
                    (lon + math.degrees(east_m / (radius * math.cos(math.radians(lat)))))
                    * 1e7
                )
            ),
        )

    def command_until_accepted(
        self, system_id: int, command: int, params: list[float], label: str, timeout_s: float
    ) -> float:
        accepted = {
            int(self.mavutil.mavlink.MAV_RESULT_ACCEPTED),
            int(self.mavutil.mavlink.MAV_RESULT_IN_PROGRESS),
        }
        started_ns = time.monotonic_ns()
        deadline = time.monotonic() + timeout_s * self.timeout_scale
        next_send = 0.0
        while time.monotonic() < deadline:
            if time.monotonic() >= next_send:
                message = self.transmitters[("control", system_id)].command_long_encode(
                    system_id, 1, command, 0, *params
                )
                self.send("control", system_id, message)
                next_send = time.monotonic() + self.diagnostic_retry_interval_s
            self.pump(0.2)
            ack = self.acks.get(("control", system_id, command))
            if ack and ack[1] >= started_ns and int(ack[0].result) in accepted:
                latency = (ack[1] - started_ns) / 1e6
                self.summary["command_acks"].append(
                    {
                        "channel": "control",
                        "uav": f"uav{system_id}",
                        "command": command,
                        "label": label,
                        "result": int(ack[0].result),
                        "latency_ms": latency,
                    }
                )
                return latency
        raise ScenarioError(f"{label} was not accepted by uav{system_id}")

    def set_mode_all(
        self,
        custom_mode: int,
        label: str,
        timeout_s: float = 30.0,
        systems: tuple[int, ...] = UAV_IDS,
    ) -> None:
        flag = int(self.mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED)
        pending = set(systems)
        deadline = time.monotonic() + timeout_s * self.timeout_scale
        next_send = 0.0
        while pending and time.monotonic() < deadline:
            if time.monotonic() >= next_send:
                for system_id in pending:
                    self.send(
                        "control",
                        system_id,
                        self.transmitters[("control", system_id)].set_mode_encode(
                            system_id, flag, custom_mode
                        ),
                    )
                next_send = time.monotonic() + self.diagnostic_retry_interval_s
            self.pump(0.2)
            for system_id in tuple(pending):
                heartbeat = self.latest.get(("control", system_id, "HEARTBEAT"))
                if heartbeat is not None and int(heartbeat.custom_mode) == custom_mode:
                    pending.remove(system_id)
        if pending:
            raise ScenarioError(f"{label} mode not observed for {sorted(pending)}")
        self.event(label)

    def request_global_position(self, system_id: int) -> tuple[float, float]:
        command = int(self.mavutil.mavlink.MAV_CMD_REQUEST_MESSAGE)
        sent_ns, _latency = self.native_command_one(
            "control",
            system_id,
            command,
            [float(self.mavutil.mavlink.MAVLINK_MSG_ID_GLOBAL_POSITION_INT), 0, 0, 0, 0, 0, 0],
            20,
            f"uav{system_id}_global_position",
        )
        deadline = time.monotonic() + 15 * self.timeout_scale
        while time.monotonic() < deadline:
            received_ns = self.latest_at_ns.get(("control", system_id, "GLOBAL_POSITION_INT"), 0)
            message = self.latest.get(("control", system_id, "GLOBAL_POSITION_INT"))
            if received_ns >= sent_ns and message is not None:
                lat = float(message.lat) / 1e7
                lon = float(message.lon) / 1e7
                # ArduPilot can acknowledge REQUEST_MESSAGE before its GPS origin
                # yields a usable coordinate.  Never upload an origin-zero mission:
                # MAV_MISSION_INVALID_PARAM5_X is a valid flight-controller rejection.
                if 1.0 <= abs(lat) <= 90.0 and 1.0 <= abs(lon) <= 180.0:
                    self.summary.setdefault("uav_global_positions", {})[f"uav{system_id}"] = {
                        "latitude_deg": lat,
                        "longitude_deg": lon,
                    }
                    return lat, lon
            self.pump(0.2)
        raise ScenarioError(
            f"uav{system_id} GLOBAL_POSITION_INT did not contain a valid GPS coordinate"
        )

    def upload_uav_mission(self, system_id: int, waypoints: list[dict[str, Any]]) -> None:
        uav = f"uav{system_id}"
        initial = self.positions().get(uav)
        if initial is None:
            raise ScenarioError(f"{uav} tracker position missing before mission upload")
        lat, lon = self.request_global_position(system_id)
        mission_points: list[dict[str, Any]] = []
        for waypoint in waypoints:
            position = waypoint["position_m"]
            coordinate = self.offset_global(
                lat, lon, position[0] - initial[0], position[1] - initial[1]
            )
            relative_altitude_m = position[2] - initial[2]
            if relative_altitude_m <= 2.0:
                raise ScenarioError(
                    f"{uav}/{waypoint['name']} relative altitude must exceed 2 m"
                )
            mission_points.append(
                {
                    **waypoint,
                    "coordinate_int": coordinate,
                    "relative_altitude_m": relative_altitude_m,
                }
            )
        self.summary.setdefault("mission_coordinates_int", {})[uav] = mission_points
        mav = self.mavutil.mavlink
        definitions: list[tuple[int, float, tuple[int, int], float, str]] = []
        for point in mission_points:
            definitions.append(
                (
                    int(mav.MAV_CMD_NAV_WAYPOINT),
                    0.0,
                    point["coordinate_int"],
                    point["relative_altitude_m"],
                    point["name"],
                )
            )
            if point["hold_s"] > 0.0:
                definitions.append(
                    (
                        int(mav.MAV_CMD_NAV_LOITER_TIME),
                        point["hold_s"],
                        point["coordinate_int"],
                        point["relative_altitude_m"],
                        point["name"],
                    )
                )
        items = [
            self.transmitters[("control", system_id)].mission_item_int_encode(
                system_id,
                1,
                sequence,
                int(mav.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT),
                int(command),
                1 if sequence == 0 else 0,
                1,
                hold,
                2.0,
                8.0,
                0.0,
                coordinate[0],
                coordinate[1],
                relative_altitude_m,
                int(mav.MAV_MISSION_TYPE_MISSION),
            )
            for sequence, (command, hold, coordinate, relative_altitude_m, _name) in enumerate(
                definitions
            )
        ]
        started_ns = time.monotonic_ns()
        handled_at_ns = 0
        last_requested_sequence = -1
        count_packets_sent = 0
        ignored_stale_requests: list[int] = []
        request_history: list[dict[str, Any]] = []
        deferred_repeat_sequence: int | None = None
        deferred_repeat_deadline = 0.0
        mission_upload = {
            "item_count": len(items),
            "request_history": request_history,
            "mission_count_packets_sent": count_packets_sent,
        }
        self.summary.setdefault("mission_uploads", {})[uav] = mission_upload
        deadline = time.monotonic() + 60 * self.timeout_scale
        requested: set[int] = set()
        while time.monotonic() < deadline:
            # The ALOHA medium can delay an earlier MISSION_COUNT until the item
            # exchange is in progress.  A second count restarts that exchange in
            # ArduPilot, so send this protocol opener exactly once.  Item repeats
            # remain driven only by the flight controller's MISSION_REQUEST.
            if count_packets_sent == 0:
                self.send(
                    "control",
                    system_id,
                    self.transmitters[("control", system_id)].mission_count_encode(
                        system_id, 1, len(items), int(mav.MAV_MISSION_TYPE_MISSION)
                    ),
                )
                count_packets_sent += 1
                mission_upload["mission_count_packets_sent"] = count_packets_sent
            if (
                deferred_repeat_sequence is not None
                and time.monotonic() >= deferred_repeat_deadline
            ):
                self.send("control", system_id, items[deferred_repeat_sequence])
                request_history.append(
                    {
                        "message_type": "MISSION_REQUEST",
                        "sequence": deferred_repeat_sequence,
                        "action": "delayed_resent",
                    }
                )
                deferred_repeat_sequence = None
            self.pump(0.2)
            for message_type in ("MISSION_REQUEST_INT", "MISSION_REQUEST"):
                at_ns = self.latest_at_ns.get(("control", system_id, message_type), 0)
                if at_ns > max(started_ns, handled_at_ns):
                    sequence = int(self.latest[("control", system_id, message_type)].seq)
                    if not 0 <= sequence < len(items):
                        raise ScenarioError(f"invalid mission request {sequence}")
                    if sequence < last_requested_sequence:
                        ignored_stale_requests.append(sequence)
                        request_history.append(
                            {"message_type": message_type, "sequence": sequence, "action": "ignored_stale"}
                        )
                        handled_at_ns = at_ns
                        continue
                    if sequence > last_requested_sequence + 1:
                        request_history.append(
                            {"message_type": message_type, "sequence": sequence, "action": "rejected_gap"}
                        )
                        raise ScenarioError(
                            f"out-of-order mission request {sequence}; expected {last_requested_sequence + 1}"
                        )
                    if sequence == last_requested_sequence:
                        # A request can be delayed behind the item it asked for.
                        # Wait briefly for the controller's next sequence before
                        # retransmitting; otherwise that late request would inject
                        # an already-accepted item and make ArduPilot reject the plan.
                        if deferred_repeat_sequence is None:
                            deferred_repeat_sequence = sequence
                            deferred_repeat_deadline = time.monotonic() + 1.0
                            action = "deferred_repeat"
                        else:
                            action = "duplicate_repeat_pending"
                        request_history.append(
                            {"message_type": message_type, "sequence": sequence, "action": action}
                        )
                        handled_at_ns = at_ns
                        continue
                    self.send("control", system_id, items[sequence])
                    requested.add(sequence)
                    request_history.append(
                        {
                            "message_type": message_type,
                            "sequence": sequence,
                            "action": "sent",
                        }
                    )
                    last_requested_sequence = sequence
                    deferred_repeat_sequence = None
                    handled_at_ns = at_ns
            ack_at_ns = self.latest_at_ns.get(("control", system_id, "MISSION_ACK"), 0)
            if ack_at_ns >= started_ns:
                result = int(self.latest[("control", system_id, "MISSION_ACK")].type)
                mission_upload.update(
                    {
                        "ack_result": result,
                        "last_requested_sequence": last_requested_sequence,
                        "ignored_stale_requests": ignored_stale_requests,
                    }
                )
                if result != int(mav.MAV_MISSION_ACCEPTED):
                    raise ScenarioError(f"{uav} mission rejected: {result}")
                self.summary.setdefault("missions", {})[uav] = {
                    "item_count": len(items),
                    "requested_items": sorted(requested),
                    "mission_count_packets_sent": count_packets_sent,
                    "ignored_stale_requests": ignored_stale_requests,
                    "route_m": waypoints,
                    "upload_ack": result,
                }
                return
        raise ScenarioError(f"{uav} mission upload timed out")

    def probe_link_phase(self, name: str, packets_per_uav: int) -> None:
        """Offer identical, un-retried endpoint traffic to every UAV in a frozen phase."""

        if packets_per_uav <= 0:
            return
        sock = self.sockets["additional_data"]
        phase_index = len(self.summary.setdefault("causal_link_probes", [])) + 1
        sequence_base = phase_index * 100_000
        first_received_index = len(self.additional_received)
        sends: dict[tuple[int, int], dict[str, Any]] = {}
        for local_sequence in range(packets_per_uav):
            for system_id in UAV_IDS:
                sequence = sequence_base + local_sequence
                payload = f"native-sionna-causal-{system_id}-{local_sequence}".encode().ljust(
                    256, b"."
                )
                datagram = encode_data(
                    "p2p_downlink",
                    sender_id=0,
                    receiver_id=system_id,
                    sequence=sequence,
                    payload=payload,
                )
                sent_ns = time.monotonic_ns()
                sock.sendto(datagram, (endpoint_ip(system_id), P2P_PORT + system_id))
                sends[(system_id, sequence)] = {
                    "uav": f"uav{system_id}",
                    "sequence": local_sequence,
                    "wire_sequence": sequence,
                    "sent_monotonic_ns": sent_ns,
                    "bytes": len(datagram),
                }
                self.observe_for(0.01)
        self.observe_for(6.0)

        deliveries: dict[tuple[int, int], dict[str, Any]] = {}
        for message, _source, received_ns in self.additional_received[first_received_index:]:
            key = (int(message.sender_id), int(message.sequence))
            sent = sends.get(key)
            if message.kind != "p2p_downlink_ack" or sent is None or key in deliveries:
                continue
            deliveries[key] = {
                "uav": sent["uav"],
                "sequence": sent["sequence"],
                "wire_sequence": sent["wire_sequence"],
                "received_monotonic_ns": received_ns,
                "latency_ms": (received_ns - sent["sent_monotonic_ns"]) / 1e6,
            }

        by_uav: dict[str, dict[str, Any]] = {}
        for system_id in UAV_IDS:
            offered = [record for (uav_id, _sequence), record in sends.items() if uav_id == system_id]
            delivered = [
                record
                for (uav_id, _sequence), record in deliveries.items()
                if uav_id == system_id
            ]
            latencies = sorted(record["latency_ms"] for record in delivered)
            by_uav[f"uav{system_id}"] = {
                "offered_packets": len(offered),
                "delivered_packets": len(delivered),
                "pdr": len(delivered) / len(offered),
                "latency_ms": latencies,
                "position_m": self.positions().get(f"uav{system_id}"),
            }
        self.summary["causal_link_probes"].append(
            {
                "phase": name,
                "application_retransmissions": False,
                "packet_payload_bytes": 256,
                "inter_packet_interval_ms": 10.0,
                "offered_packets": len(sends),
                "delivered_packets": len(deliveries),
                "per_uav": by_uav,
                "sends": list(sends.values()),
                "deliveries": list(deliveries.values()),
            }
        )
        self.event(
            "causal_link_probe_complete",
            phase=name,
            offered=len(sends),
            delivered=len(deliveries),
        )

    def wait_observation(self, observation: dict[str, Any]) -> None:
        name = observation["name"]
        targets: dict[int, list[float]] = observation["targets"]
        tolerance = self.flight_plan["position_tolerance_m"]
        self.phase(f"{name}_transit")
        self.wait(
            lambda: all(
                self.positions().get(f"uav{system_id}") is not None
                and math.dist(self.positions()[f"uav{system_id}"], target) <= tolerance
                for system_id, target in targets.items()
            ),
            observation["timeout_s"],
            f"mission UAVs at frozen {name} points",
        )
        positions = self.positions()
        self.summary.setdefault("flight_points", {})[name] = {
            f"uav{system_id}": {
                "target_m": target,
                "gazebo_position_m": positions[f"uav{system_id}"],
                "distance_m": math.dist(positions[f"uav{system_id}"], target),
            }
            for system_id, target in targets.items()
        }
        self.phase(name)
        self.observe_for(2.0)
        self.probe_link_phase(name, observation["probe_packets_per_uav"])

    def flight(self) -> None:
        initial = self.positions()
        if set(initial) < {f"uav{index}" for index in UAV_IDS}:
            raise ScenarioError("fresh five-UAV tracker snapshot unavailable before flight")
        if not self.flight_missions or not self.flight_observations:
            raise ScenarioError("selected product scenario has no configured missions/observations")
        self.summary["initial_positions_m"] = initial
        for system_id, waypoints in sorted(self.flight_missions.items()):
            self.upload_uav_mission(system_id, waypoints)
        self.set_guided()
        arm_command = int(self.mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM)
        takeoff_command = int(self.mavutil.mavlink.MAV_CMD_NAV_TAKEOFF)
        for system_id in UAV_IDS:
            self.phase(f"arm_uav{system_id}")
            self.command_until_accepted(
                system_id, arm_command, [1.0, 0, 0, 0, 0, 0, 0], "staggered_arm", 120
            )
            self.wait(
                lambda system_id=system_id: bool(
                    int(self.latest[("control", system_id, "HEARTBEAT")].base_mode)
                    & int(self.mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED)
                ),
                20,
                f"uav{system_id} armed heartbeat",
            )
            self.observe_for(1.0)
            self.summary["uavs"][f"uav{system_id}"]["phases"]["arm"] = True

            # Keep each vehicle's arm-to-takeoff interval shorter than the
            # ArduPilot idle auto-disarm interval.  This matters for the
            # deliberately slowed functional evidence run, where a separate
            # arm-all loop leaves the first vehicles idle for several seconds
            # of simulated time while commands traverse the shared medium.
            self.phase(f"takeoff_uav{system_id}")
            try:
                self.command_until_accepted(
                    system_id,
                    takeoff_command,
                    [0, 0, 0, 0, 0, 0, self.takeoff_altitudes[system_id]],
                    "staggered_takeoff",
                    12,
                )
            except ScenarioError:
                # The physical ALOHA medium may lose the first TAKEOFF command.
                # ArduPilot then auto-disarms rather than silently taking off.
                # Re-arm and issue one bounded, observable recovery attempt; this
                # is ordinary MAVLink command handling, not a PHY/MAC retry.
                recovery = self.summary["uavs"][f"uav{system_id}"].setdefault(
                    "takeoff_recovery", {"attempted": True}
                )
                recovery["reason"] = "initial_takeoff_ack_missing"
                self.phase(f"arm_retry_uav{system_id}")
                self.command_until_accepted(
                    system_id, arm_command, [1.0, 0, 0, 0, 0, 0, 0], "takeoff_rearm", 20
                )
                self.wait(
                    lambda system_id=system_id: bool(
                        int(self.latest[("control", system_id, "HEARTBEAT")].base_mode)
                        & int(self.mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED)
                    ),
                    20,
                    f"uav{system_id} recovery armed heartbeat",
                )
                self.phase(f"takeoff_retry_uav{system_id}")
                self.command_until_accepted(
                    system_id,
                    takeoff_command,
                    [0, 0, 0, 0, 0, 0, self.takeoff_altitudes[system_id]],
                    "staggered_takeoff_recovery",
                    20,
                )
                recovery["succeeded"] = True
            self.observe_for(1.0)
        self.wait(
            lambda: all(
                self.positions().get(f"uav{system_id}", [0, 0, -1e9])[2]
                >= initial[f"uav{system_id}"][2] + self.takeoff_altitudes[system_id] - 7.0
                for system_id in UAV_IDS
            ),
            120,
            "all five UAVs above their separated holding altitudes",
        )
        for system_id in UAV_IDS:
            self.summary["uavs"][f"uav{system_id}"]["phases"]["takeoff"] = True
        self.phase("takeoff_complete")
        self.observe_for(2.0)
        self.phase("hold_all")
        self.observe_for(self.flight_plan["hold_time_s"])
        hold_positions = self.positions()
        self.summary["hold_positions_m"] = hold_positions

        mission_systems = tuple(sorted(self.flight_missions))
        self.set_mode_all(3, "auto_mode_mission_uavs", 45, systems=mission_systems)
        for observation in self.flight_observations:
            self.wait_observation(observation)
        final_hold = self.positions()
        self.summary["holding_uav_displacement_m"] = {
            f"uav{system_id}": math.dist(
                hold_positions[f"uav{system_id}"], final_hold[f"uav{system_id}"]
            )
            for system_id in UAV_IDS
            if system_id not in self.flight_missions
        }
        self.summary["mission_uav_displacement_m"] = {
            f"uav{system_id}": math.dist(
                hold_positions[f"uav{system_id}"], final_hold[f"uav{system_id}"]
            )
            for system_id in mission_systems
        }

        self.phase("land_all")
        land_command = int(self.mavutil.mavlink.MAV_CMD_NAV_LAND)
        for system_id in UAV_IDS:
            self.command_until_accepted(
                system_id, land_command, [0, 0, 0, 0, 0, 0, 0], "land_all", 45
            )
        self.set_mode_all(9, "land_mode_all", 45)
        self.wait(
            lambda: all(
                not (
                    int(self.latest[("control", system_id, "HEARTBEAT")].base_mode)
                    & int(self.mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED)
                )
                for system_id in UAV_IDS
            ),
            150,
            "all five UAVs automatically disarmed after landing",
        )
        for system_id in UAV_IDS:
            self.summary["uavs"][f"uav{system_id}"]["phases"].update(
                {"hold": True, "land": True, "auto_disarm": True}
            )
        self.summary["final_positions_m"] = self.positions()
        self.phase("landing_complete")
        self.observe_for(2.0)

    def run_native(self) -> dict[str, Any]:
        self.phase("stationary_communication_smoke")
        self.wait_heartbeats()
        self.diagnose_dual_uart()
        self.additional_data_experiments()
        self.flight()
        self.phase("pre_no_bypass")
        self.summary["status"] = "passed"
        self.summary["duration_s"] = round(time.monotonic() - self.started, 3)
        self.summary["message_counts"] = {
            f"{channel}:uav{system_id}:{message_type}": count
            for (channel, system_id, message_type), count in sorted(self.message_counts.items())
        }
        return self.summary


def run_scenario(args: argparse.Namespace) -> int:
    harness = NativeFiveUavHarness(args)
    output = Path(args.run_dir).resolve() / "metrics/scenario_summary.json"
    try:
        if args.mode == "latency_diagnostic":
            channels = tuple(args.channels.split(","))
            harness.latency_diagnostics(args.uav_count, channels)
            harness.summary["status"] = "diagnostic_complete"
            harness.summary["duration_s"] = round(time.monotonic() - harness.started, 3)
            summary = harness.summary
        else:
            summary = harness.run_native()
    except Exception as error:
        harness.summary.update(
            {
                "status": "failed",
                "error": str(error),
                "duration_s": round(time.monotonic() - harness.started, 3),
            }
        )
        write_json(output, harness.summary)
        print(f"FAIL native five-UAV scenario: {error}", file=sys.stderr)
        return 1
    finally:
        harness.close()
    write_json(output, summary)
    print(json.dumps(summary, sort_keys=True))
    return 0


def run_no_bypass_probe(args: argparse.Namespace) -> int:
    probe_args = argparse.Namespace(
        run_dir=str(Path(args.run_dir).resolve() / "logs/no_bypass_probe_workspace"),
        node_state=args.node_state,
        timeout_scale=1.0,
        phase_file=str(Path(args.run_dir).resolve() / "logs/current_phase.txt"),
        schedule_file=str(Path(args.run_dir).resolve() / "logs/additional_schedule.json"),
        radio_profile=args.radio_profile,
        scenario_config=args.scenario_config,
    )
    harness = NativeFiveUavHarness(probe_args)
    before = sum(harness.message_counts.values())
    request = int(harness.mavutil.mavlink.MAV_CMD_REQUEST_MESSAGE)
    for system_id in UAV_IDS:
        for channel, message_id in (
            ("control", harness.mavutil.mavlink.MAVLINK_MSG_ID_AUTOPILOT_VERSION),
            ("payload", harness.mavutil.mavlink.MAVLINK_MSG_ID_ATTITUDE),
        ):
            message = harness.transmitters[(channel, system_id)].command_long_encode(
                system_id, 1, request, 0, float(message_id), 0, 0, 0, 0, 0, 0
            )
            harness.send(channel, system_id, message)
        datagram = encode_data(
            "p2p_downlink",
            sender_id=0,
            receiver_id=system_id,
            sequence=system_id,
            payload=b"native-process-stopped",
        )
        harness.sockets["additional_data"].sendto(
            datagram, (endpoint_ip(system_id), P2P_PORT + system_id)
        )
    started_ns = time.monotonic_ns()
    harness.observe_for(float(args.duration_s))
    messages = sum(harness.message_counts.values()) - before
    result = {
        "duration_s": float(args.duration_s),
        "started_monotonic_ns": started_ns,
        "ended_monotonic_ns": time.monotonic_ns(),
        "control_or_payload_messages_received": messages,
        "additional_packets_received": len(harness.additional_received),
        "control_ack_absent_all_five": not any(
            key[0] == "control" for key in harness.acks
        ),
        "payload_response_absent_all_five": not any(
            key[0] == "payload" for key in harness.acks
        ),
        "reverse_telemetry_absent_all_five": messages == 0,
        "additional_data_absent": not harness.additional_received,
        "passed": messages == 0 and not harness.additional_received,
    }
    harness.close()
    write_json(Path(args.output), result)
    return 0 if result["passed"] else 1


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    agent = commands.add_parser("additional-agent")
    agent.add_argument("--index", type=int, choices=UAV_IDS, required=True)
    agent.add_argument("--schedule-file", required=True)
    agent.add_argument("--event-log", required=True)
    agent.add_argument("--ready-file", required=True)
    agent.add_argument("--scenario-config", default=str(DEFAULT_SCENARIO_CONFIG))
    agent.set_defaults(function=run_additional_agent)
    scenario = commands.add_parser("run")
    scenario.add_argument("--run-dir", required=True)
    scenario.add_argument("--node-state", required=True)
    scenario.add_argument("--phase-file", required=True)
    scenario.add_argument("--schedule-file", required=True)
    scenario.add_argument("--scenario-config", default=str(DEFAULT_SCENARIO_CONFIG))
    scenario.add_argument("--timeout-scale", type=float, default=1.0)
    scenario.add_argument("--mode", choices=("product", "latency_diagnostic"), default="product")
    scenario.add_argument("--uav-count", type=int, choices=(1, 5), default=5)
    scenario.add_argument("--channels", default="control,payload")
    scenario.add_argument(
        "--radio-profile", default="native_wifi_80211n_spectrum_reference_v1"
    )
    scenario.set_defaults(function=run_scenario)
    probe = commands.add_parser("no-bypass-probe")
    probe.add_argument("--run-dir", required=True)
    probe.add_argument("--node-state", required=True)
    probe.add_argument("--scenario-config", default=str(DEFAULT_SCENARIO_CONFIG))
    probe.add_argument("--duration-s", type=float, default=10.5)
    probe.add_argument("--output", required=True)
    probe.add_argument(
        "--radio-profile", default="native_wifi_80211n_spectrum_reference_v1"
    )
    probe.set_defaults(function=run_no_bypass_probe)
    return root


def main() -> int:
    args = parser().parse_args()
    return int(args.function(args))


if __name__ == "__main__":
    raise SystemExit(main())
