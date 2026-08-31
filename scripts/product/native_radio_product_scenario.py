#!/usr/bin/env python3
"""Drive one real Town01 SITL through the existing dual-UART radio endpoints."""

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
from collections import Counter
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

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


GCS_IP = "10.71.0.10"
UAV_IP = "10.71.1.10"
SYSTEM_ID = 1
CONTROL_PORT = 14600
PAYLOAD_PORT = 14700
ADDITIONAL_PORT = 14800


class ScenarioError(RuntimeError):
    """The real one-UAV product lifecycle did not complete."""


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, allow_nan=False, sort_keys=True) + "\n")


def run_additional_agent(args: argparse.Namespace) -> int:
    """Existing-endpoint application; it never participates in ns-3 outcomes."""

    event_log = Path(args.event_log)
    ready_file = Path(args.ready_file)
    stop = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind((UAV_IP, ADDITIONAL_PORT + 1))
    udp.setblocking(False)
    reverse_started = False
    reverse_next_sequence = 0
    reverse_next_ns: int | None = None
    last_downlink_ns: int | None = None
    write_json(
        ready_file,
        {"status": "ready", "pid": os.getpid(), "endpoint": f"{UAV_IP}:{ADDITIONAL_PORT + 1}"},
    )
    append_jsonl(event_log, {"event": "start", "monotonic_ns": time.monotonic_ns()})
    try:
        while not stop:
            readable, _, _ = __import__("select").select([udp], [], [], 0.05)
            if readable:
                datagram, source = udp.recvfrom(65535)
                now_ns = time.monotonic_ns()
                try:
                    message = decode_data(datagram)
                except DataProtocolError as error:
                    append_jsonl(
                        event_log,
                        {"event": "malformed", "error": str(error), "monotonic_ns": now_ns},
                    )
                else:
                    append_jsonl(
                        event_log,
                        {
                            "event": "receive",
                            "kind": message.kind,
                            "sequence": message.sequence,
                            "sent_monotonic_ns": message.sent_monotonic_ns,
                            "received_monotonic_ns": now_ns,
                            "checksum": message.checksum,
                            "sha256": hashlib.sha256(datagram).hexdigest(),
                            "source": f"{source[0]}:{source[1]}",
                        },
                    )
                    if message.kind == "p2p_downlink" and message.receiver_id == SYSTEM_ID:
                        last_downlink_ns = now_ns

            now_ns = time.monotonic_ns()
            if (
                not reverse_started
                and last_downlink_ns is not None
                and now_ns - last_downlink_ns >= 750_000_000
            ):
                reverse_started = True
                reverse_next_ns = now_ns
                append_jsonl(
                    event_log,
                    {"event": "independent_uplink_start", "monotonic_ns": now_ns},
                )
            if (
                reverse_started
                and reverse_next_sequence < 10
                and reverse_next_ns is not None
                and now_ns >= reverse_next_ns
            ):
                response = encode_data(
                    "p2p_uplink",
                    sender_id=SYSTEM_ID,
                    receiver_id=0,
                    sequence=reverse_next_sequence,
                    payload=f"independent-uav-originated-{reverse_next_sequence}".encode(),
                )
                decoded = decode_data(response)
                udp.sendto(response, (GCS_IP, ADDITIONAL_PORT))
                append_jsonl(
                    event_log,
                    {
                        "event": "transmit",
                        "kind": "p2p_uplink",
                        "sequence": reverse_next_sequence,
                        "sent_monotonic_ns": decoded.sent_monotonic_ns,
                        "checksum": decoded.checksum,
                        "bytes": len(response),
                        "sha256": hashlib.sha256(response).hexdigest(),
                        "monotonic_ns": time.monotonic_ns(),
                    },
                )
                reverse_next_sequence += 1
                reverse_next_ns = now_ns + 250_000_000
    finally:
        append_jsonl(event_log, {"event": "stop", "monotonic_ns": time.monotonic_ns()})
        udp.close()
    return 0


class ProductHarness:
    def __init__(self, args: argparse.Namespace) -> None:
        os.environ.setdefault("MAVLINK20", "1")
        from pymavlink import mavutil

        self.args = args
        self.mavutil = mavutil
        self.run_dir = Path(args.run_dir).resolve()
        self.node_state = Path(args.node_state).resolve()
        self.phase_file = Path(args.phase_file).resolve()
        self.events_file = self.run_dir / "logs/product_scenario_events.jsonl"
        self.started_ns = time.monotonic_ns()
        self.selector = selectors.DefaultSelector()
        self.sockets: dict[str, socket.socket] = {}
        for channel, port, tos in (
            ("control", CONTROL_PORT, 184),
            ("payload", PAYLOAD_PORT, 40),
            ("additional", ADDITIONAL_PORT, 0),
        ):
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, tos)
            sock.bind((GCS_IP, port))
            sock.setblocking(False)
            self.sockets[channel] = sock
            self.selector.register(sock, selectors.EVENT_READ, channel)

        self.parsers: dict[str, Any] = {}
        self.transmitters: dict[str, Any] = {}
        self.encoders: dict[str, Encoder] = {}
        self.reassemblers: dict[str, Reassembler] = {}
        self.transport_counters: dict[str, TransportCounters] = {}
        self.input_frames: dict[str, MavlinkStreamCounter] = {}
        self.output_frames: dict[str, MavlinkStreamCounter] = {}
        for channel in ("control", "payload"):
            parser = mavutil.mavlink.MAVLink(None)
            parser.robust_parsing = True
            transmitter = mavutil.mavlink.MAVLink(None)
            transmitter.srcSystem = 255
            transmitter.srcComponent = 190
            counters = TransportCounters()
            self.parsers[channel] = parser
            self.transmitters[channel] = transmitter
            self.transport_counters[channel] = counters
            self.encoders[channel] = Encoder(
                channel=channel,
                uav_id=SYSTEM_ID,
                direction="gcs_to_uart",
                max_payload=args.chunk_payload_bytes,
            )
            self.reassemblers[channel] = Reassembler(
                channel=channel,
                uav_id=SYSTEM_ID,
                direction="uart_to_gcs",
                timeout_ms=args.reassembly_timeout_ms,
                counters=counters,
            )
            self.input_frames[channel] = MavlinkStreamCounter()
            self.output_frames[channel] = MavlinkStreamCounter()

        self.latest: dict[tuple[str, str], tuple[Any, int]] = {}
        self.received_messages: list[tuple[str, Any, int]] = []
        self.acks: dict[tuple[str, int], tuple[Any, int]] = {}
        self.message_counts: Counter[tuple[str, str]] = Counter()
        self.datagram_counts: Counter[str] = Counter()
        self.additional_received: list[tuple[Any, int]] = []
        self.rtts_ms: list[float] = []
        self.summary: dict[str, Any] = {
            "status": "running",
            "system_id": SYSTEM_ID,
            "profile": "generic_native_spectrum_aloha_reference",
            "points": {},
            "mavlink_rtt_ms": [],
        }

    def close(self) -> None:
        self.selector.close()
        for sock in self.sockets.values():
            sock.close()

    def phase(self, name: str) -> None:
        self.phase_file.parent.mkdir(parents=True, exist_ok=True)
        self.phase_file.write_text(name + "\n", encoding="utf-8")
        self.event("phase", detail=name)

    def event(self, name: str, **values: Any) -> None:
        append_jsonl(
            self.events_file,
            {
                "event": name,
                "elapsed_s": (time.monotonic_ns() - self.started_ns) / 1e9,
                "monotonic_ns": time.monotonic_ns(),
                **values,
            },
        )

    def position(self) -> list[float] | None:
        try:
            state = json.loads(self.node_state.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if state.get("source") != "ros_odometry" or state.get("missing_nodes") or state.get("stale_nodes"):
            return None
        for node in state.get("nodes", []):
            if node.get("id") == "uav1" and not node.get("stale"):
                return [float(value) for value in node["position_m"]]
        return None

    def _consume(self, channel: str, records: list[bytes], now_ns: int) -> None:
        for record in records:
            self.output_frames[channel].feed(record)
            self.transport_counters[channel].uart_output_bytes += len(record)
            for message in self.parsers[channel].parse_buffer(record) or []:
                if message.get_type() == "BAD_DATA" or int(message.get_srcSystem()) != SYSTEM_ID:
                    continue
                message_type = str(message.get_type())
                self.latest[(channel, message_type)] = (message, now_ns)
                self.received_messages.append((channel, message, now_ns))
                self.message_counts[(channel, message_type)] += 1
                if message_type == "STATUSTEXT":
                    text = message.text
                    if isinstance(text, bytes):
                        text = text.decode("utf-8", errors="replace")
                    self.event(
                        "mavlink_statustext",
                        channel=channel,
                        severity=int(message.severity),
                        text=str(text).rstrip("\x00"),
                    )
                if message_type == "COMMAND_ACK":
                    self.acks[(channel, int(message.command))] = (message, now_ns)

    def pump(self, timeout_s: float = 0.2) -> None:
        now_ns = time.monotonic_ns()
        for channel, reassembler in self.reassemblers.items():
            self._consume(channel, reassembler.expire(now_ns), now_ns)
        for key, _mask in self.selector.select(timeout_s):
            channel = str(key.data)
            datagram, _source = key.fileobj.recvfrom(65535)
            self.datagram_counts[channel] += 1
            now_ns = time.monotonic_ns()
            if channel == "additional":
                try:
                    self.additional_received.append((decode_data(datagram), now_ns))
                except DataProtocolError:
                    self.summary.setdefault("additional_malformed", 0)
                    self.summary["additional_malformed"] += 1
                continue
            try:
                chunk = decode_chunk(datagram)
            except ValueError:
                self.transport_counters[channel].malformed_chunks += 1
                continue
            if chunk.uav_id != SYSTEM_ID:
                continue
            self._consume(channel, self.reassemblers[channel].ingest(datagram, now_ns), now_ns)

    def wait(self, predicate: Callable[[], bool], timeout_s: float, description: str) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if predicate():
                return
            self.pump(min(0.2, deadline - time.monotonic()))
        raise ScenarioError(f"timeout waiting for {description}")

    def send(self, channel: str, message: Any) -> int:
        frame = message.pack(self.transmitters[channel], force_mavlink1=False)
        self.input_frames[channel].feed(frame)
        counters = self.transport_counters[channel]
        counters.uart_input_bytes += len(frame)
        datagrams = self.encoders[channel].encode(frame)
        counters.records_encoded += 1
        counters.chunks_encoded += len(datagrams)
        port = 14601 if channel == "control" else 14701
        total = 0
        for datagram in datagrams:
            total += self.sockets[channel].sendto(datagram, (UAV_IP, port))
            counters.ns3_input_bytes += len(datagram)
        return total

    def command(
        self,
        channel: str,
        command: int,
        params: list[float],
        label: str,
        timeout_s: float = 20,
        retry_results: set[int] | None = None,
    ) -> tuple[int, float]:
        sent_ns = time.monotonic_ns()
        processed_ack_ns = sent_ns
        deadline = time.monotonic() + timeout_s
        next_send = 0.0
        accepted = {
            int(self.mavutil.mavlink.MAV_RESULT_ACCEPTED),
            int(self.mavutil.mavlink.MAV_RESULT_IN_PROGRESS),
        }
        while time.monotonic() < deadline:
            if time.monotonic() >= next_send:
                message = self.transmitters[channel].command_long_encode(
                    SYSTEM_ID, 1, command, 0, *params
                )
                self.send(channel, message)
                next_send = time.monotonic() + 1.0
            self.pump(0.2)
            ack = self.acks.get((channel, command))
            if ack is None or ack[1] <= processed_ack_ns:
                continue
            processed_ack_ns = ack[1]
            result = int(ack[0].result)
            if result not in accepted:
                if retry_results is not None and result in retry_results:
                    self.event(
                        "command_retry",
                        channel=channel,
                        command=command,
                        label=label,
                        result=result,
                    )
                    continue
                raise ScenarioError(f"{label} rejected with MAV_RESULT {result}")
            latency = (ack[1] - sent_ns) / 1e6
            self.rtts_ms.append(latency)
            self.event("command_ack", channel=channel, command=command, label=label, result=result, rtt_ms=latency)
            return sent_ns, latency
        raise ScenarioError(f"{label} COMMAND_ACK missing on {channel}")

    def request_message(self, channel: str, message_id: int, message_name: str) -> dict[str, Any]:
        command = int(self.mavutil.mavlink.MAV_CMD_REQUEST_MESSAGE)
        sent_ns, latency = self.command(
            channel, command, [float(message_id), 0, 0, 0, 0, 0, 0], f"request_{message_name.lower()}"
        )
        self.wait(
            lambda: self.latest.get((channel, message_name), (None, 0))[1] >= sent_ns,
            12,
            f"{message_name} on {channel} UART",
        )
        message, received_ns = self.latest[(channel, message_name)]
        return {
            "request": message_name,
            "command_ack": {"system_id": int(message.get_srcSystem()), "rtt_ms": latency},
            "response": {"message": message_name, "system_id": int(message.get_srcSystem())},
            "response_latency_ms": (received_ns - sent_ns) / 1e6,
        }

    def diagnostics(self) -> None:
        self.phase("preflight")
        self.wait(
            lambda: all((channel, "HEARTBEAT") in self.latest for channel in ("control", "payload")),
            120,
            "real HEARTBEAT from sysid 1 on both UARTs",
        )
        heartbeats = {
            channel: {
                "system_id": int(self.latest[(channel, "HEARTBEAT")][0].get_srcSystem()),
                "component_id": int(self.latest[(channel, "HEARTBEAT")][0].get_srcComponent()),
            }
            for channel in ("control", "payload")
        }
        control_before = self.message_counts[("control", "HEARTBEAT")]
        control = self.request_message(
            "control", int(self.mavutil.mavlink.MAVLINK_MSG_ID_AUTOPILOT_VERSION), "AUTOPILOT_VERSION"
        )
        self.wait(
            lambda: self.message_counts[("control", "HEARTBEAT")] > control_before,
            8,
            "control telemetry after safe command",
        )
        control["telemetry_after_command"] = True
        control_ack_before = self.acks.get(("control", int(self.mavutil.mavlink.MAV_CMD_REQUEST_MESSAGE)))
        payload = self.request_message(
            "payload", int(self.mavutil.mavlink.MAVLINK_MSG_ID_ATTITUDE), "ATTITUDE"
        )
        self.pump(0.5)
        control_ack_after = self.acks.get(("control", int(self.mavutil.mavlink.MAV_CMD_REQUEST_MESSAGE)))
        matching_control = bool(
            control_ack_after and control_ack_after != control_ack_before and control_ack_after[1] >= self.latest[("payload", "ATTITUDE")][1]
        )
        if matching_control:
            raise ScenarioError("payload request produced a matching control-UART ACK")
        payload["matching_control_ack_observed"] = False
        self.summary["heartbeats"] = heartbeats
        self.summary["control_uart"] = control
        self.summary["payload_uart"] = payload
        self.event("dual_uart_diagnostics_complete")

    def additional_data(self) -> None:
        sent: list[dict[str, Any]] = []
        before = len(self.additional_received)
        for sequence in range(10):
            datagram = encode_data(
                "p2p_downlink",
                sender_id=0,
                receiver_id=SYSTEM_ID,
                sequence=sequence,
                payload=f"native-product-downlink-{sequence}".encode(),
            )
            self.sockets["additional"].sendto(datagram, (UAV_IP, ADDITIONAL_PORT + 1))
            decoded = decode_data(datagram)
            sent.append(
                {
                    "sequence": sequence,
                    "timestamp_ns": decoded.sent_monotonic_ns,
                    "checksum": decoded.checksum,
                    "sha256": hashlib.sha256(datagram).hexdigest(),
                }
            )
            spacing_deadline = time.monotonic() + 0.25
            while time.monotonic() < spacing_deadline:
                self.pump(min(0.1, spacing_deadline - time.monotonic()))
        self.wait(
            lambda: len(
                {
                    message.sequence
                    for message, _at in self.additional_received[before:]
                    if message.kind == "p2p_uplink" and message.sender_id == SYSTEM_ID
                }
            )
            > 0,
            20,
            "at least one independently originated UAV-to-GCS checksummed packet",
        )
        observation_deadline = time.monotonic() + 5.0
        while time.monotonic() < observation_deadline:
            self.pump(min(0.2, observation_deadline - time.monotonic()))
        received = [
            {
                "sequence": message.sequence,
                "timestamp_ns": message.sent_monotonic_ns,
                "checksum": message.checksum,
                "latency_ms": (received_ns - message.sent_monotonic_ns) / 1e6,
            }
            for message, received_ns in self.additional_received[before:]
            if message.kind == "p2p_uplink" and message.sender_id == SYSTEM_ID
        ]
        self.summary["additional_data"] = {
            "protocol": "checksummed_logical_message_v1",
            "gcs_to_uav_sent": 10,
            "uav_to_gcs_received": len({item["sequence"] for item in received}),
            "downlink": sent,
            "uplink": received,
            "ns3_echo": False,
        }
        self.event("additional_data_complete")

    def _global_position(self) -> tuple[float, float]:
        self.request_message(
            "control", int(self.mavutil.mavlink.MAVLINK_MSG_ID_GLOBAL_POSITION_INT), "GLOBAL_POSITION_INT"
        )
        message = self.latest[("control", "GLOBAL_POSITION_INT")][0]
        return float(message.lat) / 1e7, float(message.lon) / 1e7

    @staticmethod
    def _offset_global(lat: float, lon: float, east_m: float, north_m: float) -> tuple[int, int]:
        radius = 6378137.0
        latitude = lat + math.degrees(north_m / radius)
        longitude = lon + math.degrees(east_m / (radius * math.cos(math.radians(lat))))
        return int(round(latitude * 1e7)), int(round(longitude * 1e7))

    def upload_mission(self) -> None:
        lat, lon = self._global_position()
        start = self.position()
        if start is None:
            raise ScenarioError("fresh Gazebo odometry is unavailable before mission upload")
        los = self.args.los_point
        nlos = self.args.nlos_point
        transit_south = self.args.transit_south
        transit_north = self.args.transit_north
        back = self.args.return_point
        targets = {
            "start": self._offset_global(lat, lon, 0, 0),
            "los": self._offset_global(lat, lon, los[0] - start[0], los[1] - start[1]),
            "nlos": self._offset_global(lat, lon, nlos[0] - start[0], nlos[1] - start[1]),
            "transit_south": self._offset_global(
                lat, lon, transit_south[0] - start[0], transit_south[1] - start[1]
            ),
            "transit_north": self._offset_global(
                lat, lon, transit_north[0] - start[0], transit_north[1] - start[1]
            ),
            "return": self._offset_global(lat, lon, back[0] - start[0], back[1] - start[1]),
        }
        mav = self.mavutil.mavlink
        definitions = [
            (mav.MAV_CMD_NAV_TAKEOFF, 0.0, targets["start"], 15.0),
            (mav.MAV_CMD_NAV_LOITER_TIME, float(self.args.hold_time_s), targets["start"], 15.0),
            (mav.MAV_CMD_NAV_WAYPOINT, 0.0, targets["los"], 15.0),
            (mav.MAV_CMD_NAV_LOITER_TIME, float(self.args.hold_time_s), targets["los"], 15.0),
            (mav.MAV_CMD_NAV_WAYPOINT, 0.0, targets["transit_south"], 15.0),
            (mav.MAV_CMD_NAV_WAYPOINT, 0.0, targets["transit_north"], 15.0),
            (mav.MAV_CMD_NAV_WAYPOINT, 0.0, targets["nlos"], 15.0),
            (mav.MAV_CMD_NAV_LOITER_TIME, float(self.args.hold_time_s), targets["nlos"], 15.0),
            (mav.MAV_CMD_NAV_WAYPOINT, 0.0, targets["transit_north"], 15.0),
            (mav.MAV_CMD_NAV_WAYPOINT, 0.0, targets["transit_south"], 15.0),
            (mav.MAV_CMD_NAV_WAYPOINT, 0.0, targets["return"], 15.0),
            (mav.MAV_CMD_NAV_LOITER_TIME, 3.0, targets["return"], 15.0),
            (mav.MAV_CMD_NAV_LAND, 0.0, targets["return"], 0.0),
        ]
        items = [
            self.transmitters["control"].mission_item_int_encode(
                SYSTEM_ID,
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
                coords[0],
                coords[1],
                altitude,
                int(mav.MAV_MISSION_TYPE_MISSION),
            )
            for sequence, (command, hold, coords, altitude) in enumerate(definitions)
        ]
        self.received_messages.clear()
        deadline = time.monotonic() + 45
        next_count = 0.0
        sent_items: set[int] = set()
        while time.monotonic() < deadline:
            if time.monotonic() >= next_count:
                self.send(
                    "control",
                    self.transmitters["control"].mission_count_encode(
                        SYSTEM_ID, 1, len(items), int(mav.MAV_MISSION_TYPE_MISSION)
                    ),
                )
                next_count = time.monotonic() + 2.0
            self.pump(0.2)
            pending = self.received_messages
            self.received_messages = []
            for channel, message, _received_ns in pending:
                if channel != "control":
                    continue
                if message.get_type() in {"MISSION_REQUEST", "MISSION_REQUEST_INT"}:
                    sequence = int(message.seq)
                    if not 0 <= sequence < len(items):
                        raise ScenarioError(f"SITL requested invalid mission sequence {sequence}")
                    self.send("control", items[sequence])
                    sent_items.add(sequence)
                    next_count = time.monotonic() + 30.0
                elif message.get_type() == "MISSION_ACK":
                    result = int(message.type)
                    if result != int(mav.MAV_MISSION_ACCEPTED):
                        raise ScenarioError(f"mission upload rejected with result {result}")
                    self.summary["mission"] = {
                        "upload_ack": result,
                        "item_count": len(items),
                        "requested_items": sorted(sent_items),
                        "geometry_targets_m": {
                            "los": los,
                            "obstructed_candidate": nlos,
                            "transit_south": transit_south,
                            "transit_north": transit_north,
                            "return": back,
                        },
                    }
                    self.event("mission_upload_complete", item_count=len(items))
                    return
        raise ScenarioError("mission upload did not receive MISSION_ACK")

    def _mode(self) -> str:
        heartbeat = self.latest.get(("control", "HEARTBEAT"))
        return self.mavutil.mode_string_v10(heartbeat[0]) if heartbeat else "UNKNOWN"

    def set_mode(self, custom_mode: int, label: str, timeout_s: float = 30.0) -> None:
        flag = int(self.mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED)
        deadline = time.monotonic() + timeout_s
        next_send = 0.0
        while time.monotonic() < deadline:
            if time.monotonic() >= next_send:
                self.send(
                    "control",
                    self.transmitters["control"].set_mode_encode(SYSTEM_ID, flag, custom_mode),
                )
                next_send = time.monotonic() + 1.0
            self.pump(0.2)
            heartbeat = self.latest.get(("control", "HEARTBEAT"))
            if heartbeat is not None and int(heartbeat[0].custom_mode) == custom_mode:
                self.event("mode_observed", label=label, custom_mode=custom_mode)
                return
        raise ScenarioError(f"{label} mode not observed")

    def snapshot(self, name: str, target: list[float]) -> None:
        position = self.position()
        if position is None:
            raise ScenarioError(f"fresh Gazebo odometry missing at {name}")
        distance = math.dist(position, target)
        self.summary["points"][name] = {
            "gazebo_position_m": position,
            "target_m": target,
            "distance_from_target_m": distance,
            "ardupilot_mode": self._mode(),
            "delivered_mavlink_messages": sum(self.message_counts.values()),
            "mavlink_rtt_ms": list(self.rtts_ms),
        }
        self.event(
            "position_observation",
            point=name,
            position_m=position,
            target_m=target,
            distance_m=distance,
        )

    def execute_flight(self) -> None:
        self.upload_mission()
        mav = self.mavutil.mavlink
        self.set_mode(4, "GUIDED", 45)
        self.command(
            "control",
            int(mav.MAV_CMD_COMPONENT_ARM_DISARM),
            [1.0, 0, 0, 0, 0, 0, 0],
            "arm",
            45,
            {
                int(mav.MAV_RESULT_TEMPORARILY_REJECTED),
                int(mav.MAV_RESULT_FAILED),
            },
        )
        self.wait(
            lambda: bool(
                int(self.latest[("control", "HEARTBEAT")][0].base_mode)
                & int(mav.MAV_MODE_FLAG_SAFETY_ARMED)
            ),
            20,
            "armed HEARTBEAT",
        )
        self.phase("takeoff")
        initial = self.position()
        if initial is None:
            raise ScenarioError("initial Gazebo odometry missing")
        self.command(
            "control",
            int(mav.MAV_CMD_NAV_TAKEOFF),
            [0, 0, 0, 0, 0, 0, 15.0],
            "takeoff",
            45,
        )
        self.wait(
            lambda: self.position() is not None and self.position()[2] >= initial[2] + 8.0,
            90,
            "mission takeoff from Gazebo physics",
        )
        self.snapshot("takeoff_hold", [initial[0], initial[1], initial[2] + 15.0])

        self.set_mode(3, "AUTO", 45)
        self.phase("los_hold")
        self.wait(
            lambda: self.position() is not None and math.dist(self.position(), self.args.los_point) <= self.args.tolerance_m,
            120,
            "predeclared LOS point",
        )
        self.snapshot("los", self.args.los_point)

        self.phase("nlos_hold")
        self.wait(
            lambda: self.position() is not None and math.dist(self.position(), self.args.nlos_point) <= self.args.tolerance_m,
            160,
            "predeclared obstructed candidate",
        )
        self.snapshot("obstructed_candidate", self.args.nlos_point)

        self.phase("return")
        self.wait(
            lambda: self.position() is not None and math.dist(self.position(), self.args.return_point) <= self.args.tolerance_m,
            180,
            "return point",
        )
        self.snapshot("return", self.args.return_point)

        self.phase("landing")
        self.wait(
            lambda: self.position() is not None and self.position()[2] <= initial[2] + 1.5,
            120,
            "mission landing",
        )
        self.wait(
            lambda: not bool(
                int(self.latest[("control", "HEARTBEAT")][0].base_mode)
                & int(mav.MAV_MODE_FLAG_SAFETY_ARMED)
            ),
            60,
            "automatic disarm after landing",
        )
        self.snapshot("landed_disarmed", initial)
        self.summary["flight_lifecycle"] = [
            "heartbeat",
            "arm",
            "takeoff",
            "hold",
            "los",
            "hold",
            "obstructed_candidate",
            "hold",
            "return",
            "land",
            "disarm",
        ]

    def transport_summary(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for channel in ("control", "payload"):
            values = self.transport_counters[channel].as_dict()
            values["mavlink_input"] = self.input_frames[channel].snapshot()
            values["mavlink_output"] = self.output_frames[channel].snapshot()
            result[channel] = values
        return result

    def fail_closed(self) -> None:
        self.phase("fail_closed")
        write_json(Path(self.args.fail_closed_ready), {"ready": True, "monotonic_ns": time.monotonic_ns()})
        stopped = Path(self.args.radio_stopped_file)
        deadline = time.monotonic() + 30
        while not stopped.exists() and time.monotonic() < deadline:
            self.pump(0.1)
        if not stopped.exists():
            raise ScenarioError("runner did not confirm native ns-3/Sionna stop")
        baseline_datagrams = dict(self.datagram_counts)
        baseline_messages = dict(self.message_counts)
        baseline_additional = len(self.additional_received)
        request = int(self.mavutil.mavlink.MAV_CMD_REQUEST_MESSAGE)
        self.send(
            "control",
            self.transmitters["control"].command_long_encode(
                SYSTEM_ID, 1, request, 0, float(self.mavutil.mavlink.MAVLINK_MSG_ID_AUTOPILOT_VERSION), 0, 0, 0, 0, 0, 0
            ),
        )
        self.send(
            "payload",
            self.transmitters["payload"].command_long_encode(
                SYSTEM_ID, 1, request, 0, float(self.mavutil.mavlink.MAVLINK_MSG_ID_ATTITUDE), 0, 0, 0, 0, 0, 0
            ),
        )
        self.sockets["additional"].sendto(
            encode_data("p2p_downlink", sender_id=0, receiver_id=SYSTEM_ID, sequence=999, payload=b"after-stop"),
            (UAV_IP, ADDITIONAL_PORT + 1),
        )
        started = time.monotonic()
        while time.monotonic() - started < 10.5:
            self.pump(0.1)
        datagram_delta = {
            channel: self.datagram_counts[channel] - baseline_datagrams.get(channel, 0)
            for channel in ("control", "payload", "additional")
        }
        message_delta = {
            f"{channel}:{kind}": count - baseline_messages.get((channel, kind), 0)
            for (channel, kind), count in self.message_counts.items()
            if count - baseline_messages.get((channel, kind), 0)
        }
        additional_delta = len(self.additional_received) - baseline_additional
        passed = not any(datagram_delta.values()) and not message_delta and additional_delta == 0
        self.summary["fail_closed"] = {
            "observation_s": 10.5,
            "new_udp_datagrams": datagram_delta,
            "new_mavlink_messages": message_delta,
            "new_additional_data": additional_delta,
            "control_ack_absent": message_delta.get("control:COMMAND_ACK", 0) == 0,
            "payload_response_absent": not any(key.startswith("payload:") for key in message_delta),
            "reverse_telemetry_absent": not message_delta,
            "passed": passed,
        }
        if not passed:
            raise ScenarioError(f"radio path did not fail closed: {self.summary['fail_closed']}")

    def run(self) -> dict[str, Any]:
        self.diagnostics()
        self.additional_data()
        self.execute_flight()
        self.summary["mavlink_rtt_ms"] = self.rtts_ms
        self.summary["message_counts"] = {
            f"{channel}:{kind}": count for (channel, kind), count in sorted(self.message_counts.items())
        }
        self.summary["serial_transport"] = self.transport_summary()
        self.fail_closed()
        self.summary["status"] = "passed"
        self.summary["duration_s"] = (time.monotonic_ns() - self.started_ns) / 1e9
        return self.summary


def run_product(args: argparse.Namespace) -> int:
    harness = ProductHarness(args)
    summary_path = Path(args.run_dir) / "metrics/mavlink_summary.json"
    try:
        summary = harness.run()
    except Exception as error:
        harness.summary["status"] = "failed"
        harness.summary["error"] = str(error)
        harness.summary["mavlink_rtt_ms"] = harness.rtts_ms
        harness.summary["message_counts"] = {
            f"{channel}:{kind}": count for (channel, kind), count in sorted(harness.message_counts.items())
        }
        harness.summary["serial_transport"] = harness.transport_summary()
        write_json(summary_path, harness.summary)
        print(f"FAIL native product scenario: {error}", file=sys.stderr)
        return 1
    finally:
        harness.close()
    write_json(summary_path, summary)
    print(json.dumps(summary, allow_nan=False, sort_keys=True))
    return 0


def point(value: str) -> list[float]:
    values = [float(item) for item in value.split(",")]
    if len(values) != 3:
        raise argparse.ArgumentTypeError("point must be x,y,z")
    return values


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    agent = commands.add_parser("additional-agent")
    agent.add_argument("--event-log", required=True)
    agent.add_argument("--ready-file", required=True)
    agent.set_defaults(function=run_additional_agent)
    run = commands.add_parser("run")
    run.add_argument("--run-dir", required=True)
    run.add_argument("--node-state", required=True)
    run.add_argument("--phase-file", required=True)
    run.add_argument("--fail-closed-ready", required=True)
    run.add_argument("--radio-stopped-file", required=True)
    run.add_argument("--los-point", type=point, default=[80.0, 0.0, 17.0])
    run.add_argument("--nlos-point", type=point, default=[80.0, 110.0, 17.0])
    run.add_argument("--transit-south", type=point, default=[118.0, 0.0, 17.0])
    run.add_argument("--transit-north", type=point, default=[118.0, 110.0, 17.0])
    run.add_argument("--return-point", type=point, default=[20.0, 0.0, 17.0])
    run.add_argument("--tolerance-m", type=float, default=8.0)
    run.add_argument("--hold-time-s", type=int, default=5)
    run.add_argument("--chunk-payload-bytes", type=int, default=192)
    run.add_argument("--reassembly-timeout-ms", type=int, default=500)
    run.set_defaults(function=run_product)
    return root


def main() -> int:
    args = parser().parse_args()
    return int(args.function(args))


if __name__ == "__main__":
    raise SystemExit(main())
