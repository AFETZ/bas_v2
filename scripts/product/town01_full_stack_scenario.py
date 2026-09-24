#!/usr/bin/env python3
"""Drive five real SITLs through Town01 via the ns-3 dual-UART packet path."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import selectors
import signal
import socket
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from network.scripts.communication_qos import load_qos  # noqa: E402
from network.scripts.data_transport import (  # noqa: E402
    DataProtocolError,
    decode as decode_data,
    encode as encode_data,
)
from network.scripts.serial_transport import (  # noqa: E402
    Encoder,
    MavlinkStreamCounter,
    Reassembler,
    TransportCounters,
    decode_chunk,
)


CONTROL_TOS = 184
PAYLOAD_TOS = 40
GCS_IP = "10.71.0.10"
MULTICAST_GROUP = "239.71.0.1"
MULTICAST_PORT = 14900
UAV_IDS = tuple(range(1, 6))


def endpoint_ip(index: int) -> str:
    return f"10.71.{index}.10"


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


def run_additional_agent(args: argparse.Namespace) -> int:
    index = int(args.index)
    local_ip = endpoint_ip(index)
    event_log = Path(args.event_log)
    ready_file = Path(args.ready_file)
    stop = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    p2p = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    p2p.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, 0)
    p2p.bind((local_ip, 14800 + index))
    p2p.setblocking(False)
    multicast = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    multicast.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    multicast.bind(("0.0.0.0", MULTICAST_PORT))
    membership = socket.inet_aton(MULTICAST_GROUP) + socket.inet_aton(local_ip)
    multicast.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
    multicast.setblocking(False)
    selector = selectors.DefaultSelector()
    selector.register(p2p, selectors.EVENT_READ, "p2p")
    selector.register(multicast, selectors.EVENT_READ, "p2mp")
    counters = Counter()
    ready_file.parent.mkdir(parents=True, exist_ok=True)
    write_json(
        ready_file,
        {
            "status": "ready",
            "pid": os.getpid(),
            "uav_id": index,
            "p2p_endpoint": f"{local_ip}:{14800 + index}",
            "p2mp_endpoint": f"{MULTICAST_GROUP}:{MULTICAST_PORT}",
        },
    )
    delivered: set[str] = set()
    append_jsonl(event_log, {"event": "start", "uav": index, "monotonic_ns": time.monotonic_ns()})
    try:
        while not stop:
            for key, _mask in selector.select(0.25):
                data, source = key.fileobj.recvfrom(65535)
                digest = hashlib.sha256(data).hexdigest()
                try:
                    message = decode_data(data)
                except DataProtocolError as exc:
                    counters["malformed"] += 1
                    append_jsonl(
                        event_log,
                        {
                            "event": "malformed",
                            "uav": index,
                            "error": str(exc),
                            "sha256": digest,
                            "monotonic_ns": time.monotonic_ns(),
                        },
                    )
                    continue
                if message.receiver_id not in (0, index) or message.sender_id != 0:
                    counters["wrong_receiver"] += 1
                    continue
                duplicate = message.logical_id in delivered
                if not duplicate:
                    delivered.add(message.logical_id)
                    counters[message.kind] += 1
                append_jsonl(
                    event_log,
                    {
                        "event": "receive",
                        "kind": message.kind,
                        "uav": index,
                        "bytes": len(data),
                        "sha256": digest,
                        "logical_id": message.logical_id,
                        "sequence": message.sequence,
                        "sender_id": message.sender_id,
                        "receiver_id": message.receiver_id,
                        "payload_checksum": message.checksum,
                        "duplicate": duplicate,
                        "source": f"{source[0]}:{source[1]}",
                        "monotonic_ns": time.monotonic_ns(),
                    },
                )
                responses: list[bytes] = []
                if message.kind == "p2p_downlink":
                    responses.append(
                        encode_data(
                            "p2p_downlink_ack",
                            sender_id=index,
                            receiver_id=0,
                            sequence=message.sequence,
                            payload=f"{message.checksum:08x}".encode(),
                        )
                    )
                    responses.append(
                        encode_data(
                            "p2p_uplink",
                            sender_id=index,
                            receiver_id=0,
                            sequence=message.sequence,
                            payload=b"return:" + message.payload,
                        )
                    )
                elif message.kind == "p2mp_downlink":
                    responses.append(
                        encode_data(
                            "p2mp_ack",
                            sender_id=index,
                            receiver_id=0,
                            sequence=message.sequence,
                            payload=f"{message.checksum:08x}".encode(),
                        )
                    )
                elif message.kind == "p2p_uplink_ack":
                    counters["p2p_uplink_confirmed"] += 1
                for response in responses:
                    p2p.sendto(response, (GCS_IP, 14800))
                append_jsonl(
                    event_log,
                    {
                        "event": "ack",
                        "kind": message.kind,
                        "uav": index,
                        "response_count": len(responses),
                        "response_bytes": sum(len(response) for response in responses),
                        "monotonic_ns": time.monotonic_ns(),
                    },
                )
    finally:
        append_jsonl(
            event_log,
            {"event": "stop", "uav": index, "counters": dict(counters), "monotonic_ns": time.monotonic_ns()},
        )
        selector.close()
        p2p.close()
        multicast.close()
    return 0


class ScenarioError(RuntimeError):
    """The observable five-UAV lifecycle did not complete."""


class FlightHarness:
    def __init__(self, run_dir: Path, node_state: Path, timeout_scale: float) -> None:
        os.environ.setdefault("MAVLINK20", "1")
        from pymavlink import mavutil

        self.mavutil = mavutil
        self.run_dir = run_dir
        self.node_state = node_state
        self.timeout_scale = timeout_scale
        self.qos = load_qos()
        serial_config = self.qos["serial_transport"]
        self.selector = selectors.DefaultSelector()
        self.sockets: dict[str, socket.socket] = {}
        self.parsers: dict[tuple[str, int], Any] = {}
        self.transmitters: dict[tuple[str, int], Any] = {}
        self.transport_encoders: dict[tuple[str, int], Encoder] = {}
        self.transport_receivers: dict[tuple[str, int], Reassembler] = {}
        self.transport_counters: dict[tuple[str, int], TransportCounters] = {}
        self.transport_input_frames: dict[tuple[str, int], MavlinkStreamCounter] = {}
        self.transport_output_frames: dict[tuple[str, int], MavlinkStreamCounter] = {}
        self.transport_malformed_datagrams = 0
        self.additional_received: list[tuple[Any, tuple[str, int], int]] = []
        self.additional_malformed = 0
        for channel, port, tos in (
            ("control", 14600, CONTROL_TOS),
            ("payload", 14700, PAYLOAD_TOS),
            ("additional_data", 14800, 0),
        ):
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, tos)
            sock.bind((GCS_IP, port))
            sock.setblocking(False)
            self.sockets[channel] = sock
            self.selector.register(sock, selectors.EVENT_READ, channel)
            if channel != "additional_data":
                for system_id in UAV_IDS:
                    key = (channel, system_id)
                    parser = mavutil.mavlink.MAVLink(None)
                    parser.robust_parsing = True
                    self.parsers[key] = parser
                    transmitter = mavutil.mavlink.MAVLink(None)
                    transmitter.srcSystem = 255
                    transmitter.srcComponent = 190
                    self.transmitters[key] = transmitter
                    counters = TransportCounters()
                    self.transport_counters[key] = counters
                    self.transport_encoders[key] = Encoder(
                        channel=channel,
                        uav_id=system_id,
                        direction="gcs_to_uart",
                        max_payload=int(serial_config["chunk_payload_bytes"]),
                    )
                    self.transport_receivers[key] = Reassembler(
                        channel=channel,
                        uav_id=system_id,
                        direction="uart_to_gcs",
                        timeout_ms=int(serial_config["reassembly_timeout_ms"]),
                        counters=counters,
                    )
                    self.transport_input_frames[key] = MavlinkStreamCounter()
                    self.transport_output_frames[key] = MavlinkStreamCounter()
        self.message_counts: Counter[tuple[str, int, str]] = Counter()
        self.latest: dict[tuple[str, int, str], Any] = {}
        self.latest_at_ns: dict[tuple[str, int, str], int] = {}
        self.acks: dict[tuple[str, int, int], tuple[Any, int]] = {}
        self.parameters: dict[tuple[str, int, str], tuple[Any, int]] = {}
        self.events_path = run_dir / "logs/scenario_events.jsonl"
        self.flight_csv = run_dir / "metrics/flight_lifecycle.csv"
        self.flight_csv.parent.mkdir(parents=True, exist_ok=True)
        self.flight_handle = self.flight_csv.open("w", encoding="utf-8", newline="")
        self.flight_writer = csv.DictWriter(
            self.flight_handle,
            fieldnames=["elapsed_s", "event", "uav", "x_m", "y_m", "z_m", "detail"],
        )
        self.flight_writer.writeheader()
        self.started = time.monotonic()
        self.summary: dict[str, Any] = {
            "status": "running",
            "uavs": {f"uav{index}": {"system_id": index, "phases": {}} for index in UAV_IDS},
            "command_acks": [],
            "dual_uart_diagnostics": {},
            "status_texts": {f"uav{index}": [] for index in UAV_IDS},
        }

    def close(self) -> None:
        self.summary["gcs_serial_transport"] = self.transport_summary()
        self.flight_handle.close()
        self.selector.close()
        for sock in self.sockets.values():
            sock.close()

    def event(self, name: str, *, uav: int = 0, detail: str = "") -> None:
        position = self.positions().get(f"uav{uav}") if uav else None
        row = {
            "elapsed_s": round(time.monotonic() - self.started, 3),
            "event": name,
            "uav": f"uav{uav}" if uav else "all",
            "x_m": position[0] if position else "",
            "y_m": position[1] if position else "",
            "z_m": position[2] if position else "",
            "detail": detail,
        }
        self.flight_writer.writerow(row)
        self.flight_handle.flush()
        append_jsonl(self.events_path, {**row, "monotonic_ns": time.monotonic_ns()})
        print(f"SCENARIO {name} uav={row['uav']} {detail}", flush=True)

    def positions(self) -> dict[str, list[float]]:
        try:
            state = json.loads(self.node_state.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        result: dict[str, list[float]] = {}
        for node in state.get("nodes", []):
            if not isinstance(node, dict) or node.get("stale"):
                continue
            position = node.get("position_m")
            if isinstance(position, list) and len(position) == 3:
                result[str(node.get("id"))] = [float(value) for value in position]
        return result

    def _consume_mavlink(
        self,
        channel: str,
        system_id: int,
        records: list[bytes],
        source: tuple[str, int],
        now_ns: int,
    ) -> None:
        key = (channel, system_id)
        for record in records:
            self.transport_counters[key].uart_output_bytes += len(record)
            self.transport_output_frames[key].feed(record)
            for message in self.parsers[key].parse_buffer(record) or []:
                if message.get_type() == "BAD_DATA":
                    continue
                observed_system_id = int(message.get_srcSystem())
                if observed_system_id != system_id:
                    self.transport_malformed_datagrams += 1
                    continue
                message_type = str(message.get_type())
                self.message_counts[(channel, system_id, message_type)] += 1
                key_name = (channel, system_id, message_type)
                self.latest[key_name] = message
                self.latest_at_ns[key_name] = now_ns
                if message_type == "COMMAND_ACK":
                    self.acks[(channel, system_id, int(message.command))] = (message, now_ns)
                elif message_type == "PARAM_VALUE":
                    raw_name = message.param_id
                    name = (
                        raw_name.decode("ascii", errors="replace")
                        if isinstance(raw_name, bytes)
                        else str(raw_name)
                    ).rstrip("\x00")
                    self.parameters[(channel, system_id, name)] = (message, now_ns)
                elif message_type == "STATUSTEXT" and system_id in UAV_IDS:
                    raw_text = message.text
                    text = (
                        raw_text.decode("utf-8", errors="replace")
                        if isinstance(raw_text, bytes)
                        else str(raw_text)
                    ).rstrip("\x00")
                    record_value = {
                        "channel": channel,
                        "severity": int(message.severity),
                        "text": text,
                    }
                    status_records = self.summary["status_texts"][f"uav{system_id}"]
                    if not status_records or status_records[-1]["text"] != text:
                        status_records.append(record_value)
                        del status_records[:-20]
                        print(
                            f"AUTOPILOT uav={system_id} severity={record_value['severity']} {text}",
                            flush=True,
                        )

    def pump(self, timeout_s: float = 0.2) -> None:
        now_ns = time.monotonic_ns()
        for (channel, system_id), receiver in self.transport_receivers.items():
            records = receiver.expire(now_ns)
            if records:
                self._consume_mavlink(
                    channel,
                    system_id,
                    records,
                    (endpoint_ip(system_id), (14600 if channel == "control" else 14700) + system_id),
                    now_ns,
                )
        for key, _mask in self.selector.select(timeout_s):
            channel = str(key.data)
            data, source = key.fileobj.recvfrom(65535)
            if channel == "additional_data":
                try:
                    message = decode_data(data)
                except DataProtocolError:
                    self.additional_malformed += 1
                    continue
                self.additional_received.append((message, source, time.monotonic_ns()))
                continue
            now_ns = time.monotonic_ns()
            try:
                chunk = decode_chunk(data)
            except ValueError:
                self.transport_malformed_datagrams += 1
                continue
            system_id = int(chunk.uav_id)
            if system_id not in UAV_IDS or source[0] != endpoint_ip(system_id):
                self.transport_malformed_datagrams += 1
                continue
            records = self.transport_receivers[(channel, system_id)].ingest(data, now_ns)
            self._consume_mavlink(channel, system_id, records, source, now_ns)

    def wait(self, predicate: Callable[[], bool], timeout_s: float, description: str) -> None:
        deadline = time.monotonic() + timeout_s * self.timeout_scale
        while time.monotonic() < deadline:
            if predicate():
                return
            self.pump(min(0.25, deadline - time.monotonic()))
        raise ScenarioError(f"timeout waiting for {description}")

    def send(self, channel: str, system_id: int, message: Any) -> int:
        key = (channel, system_id)
        frame = message.pack(self.transmitters[key], force_mavlink1=False)
        port = (14600 if channel == "control" else 14700) + system_id
        counters = self.transport_counters[key]
        counters.uart_input_bytes += len(frame)
        self.transport_input_frames[key].feed(frame)
        datagrams = self.transport_encoders[key].encode(frame)
        counters.records_encoded += 1
        counters.chunks_encoded += len(datagrams)
        sent = 0
        for datagram in datagrams:
            sent += self.sockets[channel].sendto(datagram, (endpoint_ip(system_id), port))
            counters.ns3_input_bytes += len(datagram)
        return sent

    def transport_summary(self) -> dict[str, Any]:
        result: dict[str, Any] = {"malformed_datagrams": self.transport_malformed_datagrams}
        for key, counters in sorted(self.transport_counters.items()):
            channel, system_id = key
            values = counters.as_dict()
            values["mavlink_input"] = self.transport_input_frames[key].snapshot()
            values["mavlink_output"] = self.transport_output_frames[key].snapshot()
            result[f"{channel}:uav{system_id}"] = values
        return result

    def command_all(
        self,
        channel: str,
        command: int,
        params: list[float],
        timeout_s: float,
        label: str,
    ) -> dict[int, float]:
        pending = set(UAV_IDS)
        sent_at: dict[int, int] = {}
        latency: dict[int, float] = {}
        next_send = 0.0
        deadline = time.monotonic() + timeout_s * self.timeout_scale
        accepted = {
            int(self.mavutil.mavlink.MAV_RESULT_ACCEPTED),
            int(self.mavutil.mavlink.MAV_RESULT_IN_PROGRESS),
        }
        while pending and time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_send:
                for system_id in sorted(pending):
                    message = self.transmitters[(channel, system_id)].command_long_encode(
                        system_id, 1, command, 0, *params
                    )
                    self.send(channel, system_id, message)
                    sent_at.setdefault(system_id, time.monotonic_ns())
                next_send = now + 1.0
            self.pump(0.2)
            for system_id in tuple(pending):
                ack = self.acks.get((channel, system_id, command))
                if ack is None or ack[1] < sent_at[system_id]:
                    continue
                result = int(ack[0].result)
                if result in accepted:
                    latency[system_id] = (ack[1] - sent_at[system_id]) / 1e6
                    self.summary["command_acks"].append(
                        {
                            "channel": channel,
                            "uav": f"uav{system_id}",
                            "command": command,
                            "label": label,
                            "result": result,
                            "latency_ms": latency[system_id],
                        }
                    )
                    pending.remove(system_id)
        if pending:
            observed = {
                system_id: int(self.acks[(channel, system_id, command)][0].result)
                for system_id in UAV_IDS
                if (channel, system_id, command) in self.acks
            }
            raise ScenarioError(f"{label} ACK missing/rejected for {sorted(pending)}; observed={observed}")
        return latency

    def command_one(
        self,
        channel: str,
        system_id: int,
        command: int,
        params: list[float],
        timeout_s: float,
        label: str,
    ) -> tuple[int, float]:
        sent_at_ns = time.monotonic_ns()
        deadline = time.monotonic() + timeout_s * self.timeout_scale
        next_send = 0.0
        accepted = {
            int(self.mavutil.mavlink.MAV_RESULT_ACCEPTED),
            int(self.mavutil.mavlink.MAV_RESULT_IN_PROGRESS),
        }
        while time.monotonic() < deadline:
            if time.monotonic() >= next_send:
                message = self.transmitters[(channel, system_id)].command_long_encode(
                    system_id, 1, command, 0, *params
                )
                self.send(channel, system_id, message)
                next_send = time.monotonic() + 1.0
            self.pump(0.2)
            ack = self.acks.get((channel, system_id, command))
            if ack is None or ack[1] < sent_at_ns:
                continue
            result = int(ack[0].result)
            if result not in accepted:
                raise ScenarioError(
                    f"{label} rejected by uav{system_id} on {channel}: result={result}"
                )
            latency_ms = (ack[1] - sent_at_ns) / 1e6
            self.summary["command_acks"].append(
                {
                    "channel": channel,
                    "uav": f"uav{system_id}",
                    "command": command,
                    "label": label,
                    "result": result,
                    "latency_ms": latency_ms,
                }
            )
            return sent_at_ns, latency_ms
        raise ScenarioError(f"{label} ACK missing for uav{system_id} on {channel}")

    def request_parameter(self, system_id: int, name: str, timeout_s: float = 10.0) -> float:
        key = ("control", system_id, name)
        sent_at_ns = time.monotonic_ns()
        deadline = time.monotonic() + timeout_s * self.timeout_scale
        next_send = 0.0
        while time.monotonic() < deadline:
            if time.monotonic() >= next_send:
                message = self.transmitters[("control", system_id)].param_request_read_encode(
                    system_id, 1, name.encode("ascii"), -1
                )
                self.send("control", system_id, message)
                next_send = time.monotonic() + 1.0
            self.pump(0.2)
            response = self.parameters.get(key)
            if response is not None and response[1] >= sent_at_ns:
                return float(response[0].param_value)
        raise ScenarioError(f"parameter {name} missing from real uav{system_id} control UART")

    def wait_heartbeats(self) -> None:
        self.wait(
            lambda: all(
                (channel, system_id, "HEARTBEAT") in self.latest
                for channel in ("control", "payload")
                for system_id in UAV_IDS
            ),
            90,
            "control and payload UART heartbeats from all five SITLs",
        )
        for system_id in UAV_IDS:
            self.summary["uavs"][f"uav{system_id}"]["phases"]["heartbeat"] = True
        self.event("heartbeats_ready")

    def diagnose_dual_uart(self) -> None:
        request = int(self.mavutil.mavlink.MAV_CMD_REQUEST_MESSAGE)
        version_id = int(self.mavutil.mavlink.MAVLINK_MSG_ID_AUTOPILOT_VERSION)
        attitude_id = int(self.mavutil.mavlink.MAVLINK_MSG_ID_ATTITUDE)
        diagnostics: dict[str, Any] = {}
        parameter_names = (
            "SERIAL0_PROTOCOL",
            "SERIAL0_BAUD",
            "SERIAL1_PROTOCOL",
            "SERIAL1_BAUD",
            "SERIAL2_PROTOCOL",
            "SERIAL2_BAUD",
        )
        for system_id in UAV_IDS:
            uav = f"uav{system_id}"
            control_sent_ns, control_latency = self.command_one(
                "control",
                system_id,
                request,
                [float(version_id), 0, 0, 0, 0, 0, 0],
                15,
                "control_autopilot_version_diagnostic",
            )
            self.wait(
                lambda system_id=system_id, sent=control_sent_ns: self.latest_at_ns.get(
                    ("control", system_id, "AUTOPILOT_VERSION"), 0
                )
                >= sent,
                10,
                f"AUTOPILOT_VERSION on uav{system_id} control UART",
            )
            self.wait(
                lambda system_id=system_id, sent=control_sent_ns: self.latest_at_ns.get(
                    ("control", system_id, "HEARTBEAT"), 0
                )
                >= sent,
                5,
                f"post-command telemetry on uav{system_id} control UART",
            )
            parameters = {
                name: self.request_parameter(system_id, name) for name in parameter_names
            }
            control_ack_before_payload = self.acks.get(("control", system_id, request))
            payload_sent_ns, payload_latency = self.command_one(
                "payload",
                system_id,
                request,
                [float(attitude_id), 0, 0, 0, 0, 0, 0],
                15,
                "payload_attitude_diagnostic",
            )
            self.wait(
                lambda system_id=system_id, sent=payload_sent_ns: self.latest_at_ns.get(
                    ("payload", system_id, "ATTITUDE"), 0
                )
                >= sent,
                10,
                f"ATTITUDE on uav{system_id} payload UART",
            )
            isolation_deadline = time.monotonic() + 0.5
            while time.monotonic() < isolation_deadline:
                self.pump(0.1)
            control_ack_after_payload = self.acks.get(("control", system_id, request))
            control_used = bool(
                control_ack_after_payload
                and control_ack_after_payload[1] >= payload_sent_ns
                and control_ack_after_payload != control_ack_before_payload
            )
            if control_used:
                raise ScenarioError(
                    f"payload diagnostic for uav{system_id} produced a matching control ACK"
                )
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
                "parameters": parameters,
            }
            self.event("dual_uart_diagnostic", uav=system_id)

        parallel_latency = self.command_all(
            "control",
            request,
            [float(version_id), 0, 0, 0, 0, 0, 0],
            20,
            "parallel_five_uav_safe_request",
        )
        self.summary["dual_uart_diagnostics"] = {
            "sequential": diagnostics,
            "parallel_safe_request": {
                f"uav{system_id}": {"ack_latency_ms": parallel_latency[system_id]}
                for system_id in UAV_IDS
            },
        }
        self.event("parallel_safe_request_complete")

    def set_mode_all(self, custom_mode: int, label: str, timeout_s: float = 30.0) -> None:
        flag = int(self.mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED)
        pending = set(UAV_IDS)
        deadline = time.monotonic() + timeout_s * self.timeout_scale
        next_send = 0.0
        while pending and time.monotonic() < deadline:
            if time.monotonic() >= next_send:
                for system_id in sorted(pending):
                    message = self.transmitters[("control", system_id)].set_mode_encode(
                        system_id, flag, custom_mode
                    )
                    self.send("control", system_id, message)
                next_send = time.monotonic() + 1.0
            self.pump(0.2)
            for system_id in tuple(pending):
                heartbeat = self.latest.get(("control", system_id, "HEARTBEAT"))
                if heartbeat is not None and int(heartbeat.custom_mode) == custom_mode:
                    pending.remove(system_id)
        if pending:
            raise ScenarioError(f"{label} mode not observed for {sorted(pending)}")
        self.event(label)

    def set_guided(self) -> None:
        self.set_mode_all(4, "guided_mode")

    def request_streams(self) -> None:
        command = int(self.mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL)
        self.command_all(
            "payload",
            command,
            [float(self.mavutil.mavlink.MAVLINK_MSG_ID_ATTITUDE), 200000.0, 0, 0, 0, 0, 0],
            30,
            "payload_attitude_interval",
        )
        payload_start = time.monotonic_ns()
        self.wait(
            lambda: all(
                self.latest_at_ns.get(("payload", system_id, "ATTITUDE"), 0) >= payload_start
                for system_id in UAV_IDS
            ),
            20,
            "payload ATTITUDE telemetry from all UAVs",
        )
        self.command_all(
            "control",
            command,
            [float(self.mavutil.mavlink.MAVLINK_MSG_ID_LOCAL_POSITION_NED), 200000.0, 0, 0, 0, 0, 0],
            30,
            "control_local_position_interval",
        )
        self.event("dual_uart_telemetry")

    def arm_takeoff_move_land(self) -> None:
        initial = self.positions()
        if set(initial) < {f"uav{index}" for index in UAV_IDS}:
            raise ScenarioError("fresh tracker positions are unavailable before flight")
        self.summary["initial_positions_m"] = initial
        self.set_guided()
        self.command_all(
            "control",
            int(self.mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM),
            [1.0, 0, 0, 0, 0, 0, 0],
            45,
            "arm",
        )
        self.wait(
            lambda: all(
                (int(self.latest[("control", system_id, "HEARTBEAT")].base_mode)
                 & int(self.mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED))
                for system_id in UAV_IDS
            ),
            20,
            "armed heartbeat for all UAVs",
        )
        for system_id in UAV_IDS:
            self.summary["uavs"][f"uav{system_id}"]["phases"]["arm"] = True
        self.event("armed")

        self.command_all(
            "control",
            int(self.mavutil.mavlink.MAV_CMD_NAV_TAKEOFF),
            [0, 0, 0, 0, 0, 0, 15.0],
            30,
            "takeoff",
        )
        self.wait(
            lambda: all(
                self.positions().get(f"uav{system_id}", [0, 0, -1e9])[2]
                >= initial[f"uav{system_id}"][2] + 8.0
                for system_id in UAV_IDS
            ),
            60,
            "all UAVs above takeoff threshold",
        )
        for system_id in UAV_IDS:
            self.summary["uavs"][f"uav{system_id}"]["phases"]["takeoff"] = True
        self.event("takeoff_complete")

        hold_started = time.monotonic()
        while time.monotonic() - hold_started < 4.0:
            self.pump(0.2)
        for system_id in UAV_IDS:
            self.summary["uavs"][f"uav{system_id}"]["phases"]["hold"] = True
        self.event("hold_complete")

        move_start = self.positions()
        type_mask = 3576
        for system_id in UAV_IDS:
            message = self.transmitters[("control", system_id)].set_position_target_local_ned_encode(
                0,
                system_id,
                1,
                int(self.mavutil.mavlink.MAV_FRAME_LOCAL_OFFSET_NED),
                type_mask,
                4.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
            )
            self.send("control", system_id, message)
        self.wait(
            lambda: all(
                math.hypot(
                    self.positions().get(f"uav{system_id}", move_start[f"uav{system_id}"])[0]
                    - move_start[f"uav{system_id}"][0],
                    self.positions().get(f"uav{system_id}", move_start[f"uav{system_id}"])[1]
                    - move_start[f"uav{system_id}"][1],
                )
                >= 3.0
                for system_id in UAV_IDS
            ),
            45,
            "five-UAV guided displacement",
        )
        for system_id in UAV_IDS:
            self.summary["uavs"][f"uav{system_id}"]["phases"]["movement"] = True
        self.event("movement_complete")

        self.command_all(
            "control",
            int(self.mavutil.mavlink.MAV_CMD_NAV_LAND),
            [0, 0, 0, 0, 0, 0, 0],
            30,
            "land",
        )
        landing_started = self.positions()
        self.set_mode_all(9, "land_mode")
        self.wait(
            lambda: all(
                not (
                    int(self.latest[("control", system_id, "HEARTBEAT")].base_mode)
                    & int(self.mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED)
                )
                for system_id in UAV_IDS
            ),
            90,
            "all UAVs disarmed after landing",
        )
        final = self.positions()
        self.summary["final_positions_m"] = final
        self.summary["landing_descent_m"] = {
            f"uav{system_id}": round(
                landing_started[f"uav{system_id}"][2] - final[f"uav{system_id}"][2], 3
            )
            for system_id in UAV_IDS
        }
        for system_id in UAV_IDS:
            self.summary["uavs"][f"uav{system_id}"]["phases"]["land"] = True
        self.event("landing_complete")

    def additional_data(self) -> None:
        sock = self.sockets["additional_data"]
        target = 3
        p2p_payloads = {
            sequence: f"TOWN01:P2P:uav{target}:{sequence}".encode().ljust(256, b".")
            for sequence in range(10)
        }
        downlink_acks: set[int] = set()
        uplinks: set[int] = set()
        downlink_attempts: Counter[int] = Counter()
        next_p2p = 0.0
        deadline = time.monotonic() + 20 * self.timeout_scale
        multicast_acks: set[int] = set()
        multicast_payload = b"TOWN01:P2MP:ONE-LOGICAL-MESSAGE:ALL-FIVE"
        multicast_sent = 0
        next_multicast = 0.0
        while time.monotonic() < deadline and (
            len(downlink_acks) < len(p2p_payloads)
            or len(uplinks) < len(p2p_payloads)
            or len(multicast_acks) < 5
        ):
            if time.monotonic() >= next_p2p and (
                len(downlink_acks) < len(p2p_payloads) or len(uplinks) < len(p2p_payloads)
            ):
                for sequence, payload in p2p_payloads.items():
                    if sequence in downlink_acks and sequence in uplinks:
                        continue
                    datagram = encode_data(
                        "p2p_downlink",
                        sender_id=0,
                        receiver_id=target,
                        sequence=sequence,
                        payload=payload,
                    )
                    sock.sendto(datagram, (endpoint_ip(target), 14800 + target))
                    downlink_attempts[sequence] += 1
                next_p2p = time.monotonic() + 1.0
            if time.monotonic() >= next_multicast and len(multicast_acks) < 5:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(GCS_IP))
                # The packet crosses the GCS endpoint router, the shared ns-3
                # radio, and each UAV endpoint router.  A TTL of one expires at
                # the first routed hop and can never exercise the P2MP path.
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 8)
                sock.sendto(
                    encode_data(
                        "p2mp_downlink",
                        sender_id=0,
                        receiver_id=0,
                        sequence=0,
                        payload=multicast_payload,
                    ),
                    (MULTICAST_GROUP, MULTICAST_PORT),
                )
                multicast_sent += 1
                next_multicast = time.monotonic() + 1.0
            self.pump(0.2)
            received = self.additional_received
            self.additional_received = []
            for message, source, _received_ns in received:
                if message.kind == "p2p_downlink_ack" and message.sender_id == target:
                    downlink_acks.add(message.sequence)
                elif message.kind == "p2p_uplink" and message.sender_id == target:
                    uplinks.add(message.sequence)
                    confirmation = encode_data(
                        "p2p_uplink_ack",
                        sender_id=0,
                        receiver_id=target,
                        sequence=message.sequence,
                        payload=f"{message.checksum:08x}".encode(),
                    )
                    sock.sendto(confirmation, source)
                elif message.kind == "p2mp_ack" and message.sender_id in UAV_IDS:
                    multicast_acks.add(message.sender_id)
        if len(downlink_acks) != len(p2p_payloads) or len(uplinks) != len(p2p_payloads):
            raise ScenarioError(
                f"bidirectional P2P incomplete: down={sorted(downlink_acks)} up={sorted(uplinks)}"
            )
        if multicast_acks != set(UAV_IDS):
            raise ScenarioError(f"P2MP data reached only {sorted(multicast_acks)}")
        self.summary["additional_data"] = {
            "protocol": "checksummed_logical_message_v1",
            "p2p": {
                "target": f"uav{target}",
                "downlink_delivered_unique": len(downlink_acks),
                "downlink_confirmations": len(downlink_acks),
                "uplink_delivered_unique": len(uplinks),
                "uplink_confirmations_sent": len(uplinks),
                "send_attempts": sum(downlink_attempts.values()),
                "sequences": sorted(downlink_acks & uplinks),
            },
            "p2mp_root_packets_sent": multicast_sent,
            "p2mp_logical_messages": 1,
            "p2mp_receivers": [f"uav{index}" for index in sorted(multicast_acks)],
            "p2mp_per_receiver_deliveries": {
                f"uav{index}": 1 for index in sorted(multicast_acks)
            },
            "malformed_packets": self.additional_malformed,
        }
        self.event("additional_data_complete")

    def run(self) -> dict[str, Any]:
        self.wait_heartbeats()
        self.diagnose_dual_uart()
        self.request_streams()
        self.additional_data()
        self.arm_takeoff_move_land()
        self.summary["status"] = "passed"
        self.summary["duration_s"] = round(time.monotonic() - self.started, 3)
        self.summary["message_counts"] = {
            f"{channel}:uav{system_id}:{message_type}": count
            for (channel, system_id, message_type), count in sorted(self.message_counts.items())
        }
        return self.summary


def run_scenario(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    harness = FlightHarness(run_dir, Path(args.node_state).resolve(), args.timeout_scale)
    summary_path = run_dir / "metrics/scenario_summary.json"
    try:
        summary = harness.run()
    except Exception as exc:
        harness.summary["status"] = "failed"
        harness.summary["error"] = str(exc)
        harness.summary["duration_s"] = round(time.monotonic() - harness.started, 3)
        write_json(summary_path, harness.summary)
        print(f"FAIL scenario: {exc}", file=sys.stderr)
        return 1
    finally:
        harness.close()
    write_json(summary_path, summary)
    print(json.dumps(summary, sort_keys=True))
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    agent = commands.add_parser("additional-agent")
    agent.add_argument("--index", type=int, choices=UAV_IDS, required=True)
    agent.add_argument("--event-log", required=True)
    agent.add_argument("--ready-file", required=True)
    agent.set_defaults(function=run_additional_agent)
    scenario = commands.add_parser("run")
    scenario.add_argument("--run-dir", required=True)
    scenario.add_argument("--node-state", required=True)
    scenario.add_argument("--timeout-scale", type=float, default=1.0)
    scenario.set_defaults(function=run_scenario)
    return root


def main() -> int:
    args = parser().parse_args()
    return int(args.function(args))


if __name__ == "__main__":
    raise SystemExit(main())
