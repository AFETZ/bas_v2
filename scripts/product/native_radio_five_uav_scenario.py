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
P2P_PACKETS = 10
P2MP_ROOTS = 20
SIMULTANEOUS_PACKETS = 20
SIMULTANEOUS_INTERVAL_NS = 50_000_000
SIMULTANEOUS_PAYLOAD_BYTES = 256
DIAGNOSTIC_RETRY_INTERVAL_S = 20.0
TAKEOFF_ALTITUDES = {1: 15.0, 2: 23.0, 3: 25.0, 4: 27.0, 5: 29.0}
ROUTE = {
    "los": [80.0, 0.0, 17.0],
    "transit_south": [118.0, 0.0, 17.0],
    "transit_north": [118.0, 110.0, 17.0],
    "obstructed_candidate": [80.0, 110.0, 17.0],
    "return": [20.0, 0.0, 17.0],
}


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, allow_nan=False, sort_keys=True) + "\n")


def run_additional_agent(args: argparse.Namespace) -> int:
    """Endpoint-only traffic source/sink; there is no ns-3 echo or retry logic."""

    index = int(args.index)
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

            try:
                schedule = json.loads(schedule_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                schedule = {}
            now_ns = time.monotonic_ns()
            p2p_start_ns = int(schedule.get("p2p_uplink_start_monotonic_ns", 0) or 0)
            if p2p_start_ns and not p2p_sent and now_ns >= p2p_start_ns:
                for sequence in range(P2P_PACKETS):
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
                for sequence in range(SIMULTANEOUS_PACKETS):
                    target_ns = simultaneous_start_ns + sequence * SIMULTANEOUS_INTERVAL_NS
                    while time.monotonic_ns() < target_ns and not stop:
                        time.sleep(0.001)
                    payload = f"native-five-simultaneous-uplink-uav{index}-{sequence}".encode()
                    payload = payload.ljust(SIMULTANEOUS_PAYLOAD_BYTES, b".")
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
        self.summary.update(
            {
                "profile": "generic_native_spectrum_aloha_reference",
                "technology_specific_modem": False,
                "predeclared_parameters": {
                    "p2p_packets_per_direction_per_uav": P2P_PACKETS,
                    "p2mp_root_transmissions": P2MP_ROOTS,
                    "simultaneous_packets_per_uav": SIMULTANEOUS_PACKETS,
                    "simultaneous_interval_ms": SIMULTANEOUS_INTERVAL_NS / 1e6,
                    "simultaneous_payload_bytes": SIMULTANEOUS_PAYLOAD_BYTES,
                    "diagnostic_retry_interval_s": DIAGNOSTIC_RETRY_INTERVAL_S,
                    "forced_mavlink_stream_intervals": False,
                    "flight_route_m": ROUTE,
                    "takeoff_relative_altitudes_m": TAKEOFF_ALTITUDES,
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
                next_send = time.monotonic() + DIAGNOSTIC_RETRY_INTERVAL_S
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
                next_send = time.monotonic() + DIAGNOSTIC_RETRY_INTERVAL_S
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
        for sequence in range(P2P_PACKETS):
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
            "uav_originated_packets": P2P_PACKETS * len(UAV_IDS),
            "downlink_sends": downlink_sends,
            "uplink_deliveries": p2p_received,
        }

        self.additional_received = []
        self.phase("p2mp")
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(GCS_IP))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 8)
        root_sends: list[dict[str, Any]] = []
        for sequence in range(P2MP_ROOTS):
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
            "root_transmissions": P2MP_ROOTS,
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
                "packets_per_uav": SIMULTANEOUS_PACKETS,
                "interval_ns": SIMULTANEOUS_INTERVAL_NS,
                "payload_bytes": SIMULTANEOUS_PAYLOAD_BYTES,
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
            "offered_packets": SIMULTANEOUS_PACKETS * len(UAV_IDS),
            "packets_per_uav": SIMULTANEOUS_PACKETS,
            "packet_payload_bytes": SIMULTANEOUS_PAYLOAD_BYTES,
            "interval_ms": SIMULTANEOUS_INTERVAL_NS / 1e6,
            "duration_s": 1.0,
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
                next_send = time.monotonic() + DIAGNOSTIC_RETRY_INTERVAL_S
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

    def set_mode_all(self, custom_mode: int, label: str, timeout_s: float = 30.0) -> None:
        flag = int(self.mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED)
        pending = set(UAV_IDS)
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
                next_send = time.monotonic() + DIAGNOSTIC_RETRY_INTERVAL_S
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
            "moving_uav_global_position",
        )
        self.wait(
            lambda: self.latest_at_ns.get(("control", system_id, "GLOBAL_POSITION_INT"), 0)
            >= sent_ns,
            15,
            "moving UAV GLOBAL_POSITION_INT",
        )
        message = self.latest[("control", system_id, "GLOBAL_POSITION_INT")]
        return float(message.lat) / 1e7, float(message.lon) / 1e7

    def upload_moving_uav_mission(self) -> None:
        system_id = 1
        initial = self.positions().get("uav1")
        if initial is None:
            raise ScenarioError("moving UAV tracker position missing before mission upload")
        lat, lon = self.request_global_position(system_id)
        coordinates = {
            name: self.offset_global(lat, lon, point[0] - initial[0], point[1] - initial[1])
            for name, point in ROUTE.items()
        }
        mav = self.mavutil.mavlink
        definitions = [
            (mav.MAV_CMD_NAV_WAYPOINT, 0.0, coordinates["los"]),
            (mav.MAV_CMD_NAV_LOITER_TIME, 4.0, coordinates["los"]),
            (mav.MAV_CMD_NAV_WAYPOINT, 0.0, coordinates["transit_south"]),
            (mav.MAV_CMD_NAV_WAYPOINT, 0.0, coordinates["transit_north"]),
            (mav.MAV_CMD_NAV_WAYPOINT, 0.0, coordinates["obstructed_candidate"]),
            (mav.MAV_CMD_NAV_LOITER_TIME, 4.0, coordinates["obstructed_candidate"]),
            (mav.MAV_CMD_NAV_WAYPOINT, 0.0, coordinates["transit_north"]),
            (mav.MAV_CMD_NAV_WAYPOINT, 0.0, coordinates["transit_south"]),
            (mav.MAV_CMD_NAV_WAYPOINT, 0.0, coordinates["return"]),
            (mav.MAV_CMD_NAV_LOITER_TIME, 4.0, coordinates["return"]),
        ]
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
                15.0,
                int(mav.MAV_MISSION_TYPE_MISSION),
            )
            for sequence, (command, hold, coordinate) in enumerate(definitions)
        ]
        started_ns = time.monotonic_ns()
        next_count = 0.0
        handled_at_ns = 0
        deadline = time.monotonic() + 60 * self.timeout_scale
        requested: set[int] = set()
        while time.monotonic() < deadline:
            if time.monotonic() >= next_count:
                self.send(
                    "control",
                    system_id,
                    self.transmitters[("control", system_id)].mission_count_encode(
                        system_id, 1, len(items), int(mav.MAV_MISSION_TYPE_MISSION)
                    ),
                )
                next_count = time.monotonic() + DIAGNOSTIC_RETRY_INTERVAL_S
            self.pump(0.2)
            for message_type in ("MISSION_REQUEST_INT", "MISSION_REQUEST"):
                at_ns = self.latest_at_ns.get(("control", system_id, message_type), 0)
                if at_ns > max(started_ns, handled_at_ns):
                    sequence = int(self.latest[("control", system_id, message_type)].seq)
                    if not 0 <= sequence < len(items):
                        raise ScenarioError(f"invalid mission request {sequence}")
                    self.send("control", system_id, items[sequence])
                    requested.add(sequence)
                    handled_at_ns = at_ns
                    next_count = time.monotonic() + 30.0
            ack_at_ns = self.latest_at_ns.get(("control", system_id, "MISSION_ACK"), 0)
            if ack_at_ns >= started_ns:
                result = int(self.latest[("control", system_id, "MISSION_ACK")].type)
                if result != int(mav.MAV_MISSION_ACCEPTED):
                    raise ScenarioError(f"moving UAV mission rejected: {result}")
                self.summary["mission"] = {
                    "moving_uav": "uav1",
                    "item_count": len(items),
                    "requested_items": sorted(requested),
                    "route_m": ROUTE,
                    "upload_ack": result,
                }
                return
        raise ScenarioError("moving UAV mission upload timed out")

    def wait_position(self, name: str, target: list[float], timeout_s: float) -> None:
        self.phase(name)
        self.wait(
            lambda: (
                self.positions().get("uav1") is not None
                and math.dist(self.positions()["uav1"], target) <= 8.0
            ),
            timeout_s,
            f"uav1 at frozen {name} point",
        )
        position = self.positions()["uav1"]
        self.summary.setdefault("flight_points", {})[name] = {
            "target_m": target,
            "gazebo_position_m": position,
            "distance_m": math.dist(position, target),
        }

    def flight(self) -> None:
        initial = self.positions()
        if set(initial) < {f"uav{index}" for index in UAV_IDS}:
            raise ScenarioError("fresh five-UAV tracker snapshot unavailable before flight")
        self.summary["initial_positions_m"] = initial
        self.upload_moving_uav_mission()
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
            self.command_until_accepted(
                system_id,
                takeoff_command,
                [0, 0, 0, 0, 0, 0, TAKEOFF_ALTITUDES[system_id]],
                "staggered_takeoff",
                60,
            )
            self.observe_for(1.0)
        self.wait(
            lambda: all(
                self.positions().get(f"uav{system_id}", [0, 0, -1e9])[2]
                >= initial[f"uav{system_id}"][2] + TAKEOFF_ALTITUDES[system_id] - 7.0
                for system_id in UAV_IDS
            ),
            120,
            "all five UAVs above their separated holding altitudes",
        )
        for system_id in UAV_IDS:
            self.summary["uavs"][f"uav{system_id}"]["phases"]["takeoff"] = True
        self.phase("hold_all")
        self.observe_for(4.0)
        hold_positions = self.positions()
        self.summary["hold_positions_m"] = hold_positions

        flag = int(self.mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED)
        deadline = time.monotonic() + 45 * self.timeout_scale
        next_send = 0.0
        while time.monotonic() < deadline:
            if time.monotonic() >= next_send:
                self.send(
                    "control",
                    1,
                    self.transmitters[("control", 1)].set_mode_encode(1, flag, 3),
                )
                next_send = time.monotonic() + DIAGNOSTIC_RETRY_INTERVAL_S
            self.pump(0.5)
            if int(self.latest[("control", 1, "HEARTBEAT")].custom_mode) == 3:
                break
        else:
            raise ScenarioError("AUTO mode not observed on moving uav1")
        self.wait_position("los", ROUTE["los"], 180)
        self.wait_position("obstructed_candidate", ROUTE["obstructed_candidate"], 240)
        self.wait_position("return", ROUTE["return"], 240)
        final_hold = self.positions()
        self.summary["holding_uav_displacement_m"] = {
            f"uav{system_id}": math.dist(
                hold_positions[f"uav{system_id}"], final_hold[f"uav{system_id}"]
            )
            for system_id in range(2, 6)
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
    agent.set_defaults(function=run_additional_agent)
    scenario = commands.add_parser("run")
    scenario.add_argument("--run-dir", required=True)
    scenario.add_argument("--node-state", required=True)
    scenario.add_argument("--phase-file", required=True)
    scenario.add_argument("--schedule-file", required=True)
    scenario.add_argument("--timeout-scale", type=float, default=1.0)
    scenario.set_defaults(function=run_scenario)
    probe = commands.add_parser("no-bypass-probe")
    probe.add_argument("--run-dir", required=True)
    probe.add_argument("--node-state", required=True)
    probe.add_argument("--duration-s", type=float, default=10.5)
    probe.add_argument("--output", required=True)
    probe.set_defaults(function=run_no_bypass_probe)
    return root


def main() -> int:
    args = parser().parse_args()
    return int(args.function(args))


if __name__ == "__main__":
    raise SystemExit(main())
