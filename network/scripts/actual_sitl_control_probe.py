#!/usr/bin/env python3
"""Drive the sole real GCS-to-five-SITL MAVLink control acceptance path.

This executable is intentionally separate from the companion traffic producer.
It can encode only a STATUSTEXT correlation marker and
MAV_CMD_REQUEST_MESSAGE(AUTOPILOT_VERSION); every ACK, heartbeat, and telemetry
frame in its evidence must be received from an actual vehicle through the
strict byte-opaque endpoint adapters.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import select
import socket
import struct
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from network.bridge.runtime_clock_beacon import beacon  # noqa: E402


DEFAULT_MATRIX = ROOT / "network/config/endpoint_matrix_5uav.json"
EVENT_SCHEMA = "ams.actual-sitl.control-event/v1"
ENDPOINT_FORM = "actual_sitl_mavproxy_udp_tail"
ROLE_SUBJECT = "gcs_control_probe"
PROFILE_RUN_CONTRACTS = {
    "m3": "ams.m3.external_matrix_run/v1",
    "m4_capacity": "ams.m4.capacity_run/v2",
    "m4_causality": "ams.m4.causality_run/v1",
}
HEX_NONCE = re.compile(r"^(?:[0-9a-f]{32}|[0-9a-f]{64})$")
SAFE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
PHASE_CODES = {"positive": 1, "stopped": 2, "recovery": 3}
MAVLINK_CRC_EXTRA = {0: 50, 76: 152, 77: 143, 148: 178, 253: 83}
TOS_CONTROL = 184


class ControlProbeError(RuntimeError):
    """The real control probe cannot continue without ambiguous evidence."""


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def transport_nonce32(profile: str, run_nonce: str) -> tuple[str, str]:
    """Return the exact marker nonce and its profile-bound derivation label."""

    if HEX_NONCE.fullmatch(run_nonce) is None:
        raise ControlProbeError("run_nonce must be lowercase 32/64-hex")
    if profile == "m3":
        if len(run_nonce) != 32:
            raise ControlProbeError("M3 profile requires an exact 32-hex run_nonce")
        return run_nonce, "identity/full_run_nonce32"
    if profile in {"m4_capacity", "m4_causality"}:
        if len(run_nonce) != 64:
            raise ControlProbeError("M4 profiles require an exact 64-hex run_nonce")
        derived = hashlib.sha256(bytes.fromhex(run_nonce)).hexdigest()[:32]
        return derived, "sha256(raw_full_run_nonce64)[:32]"
    raise ControlProbeError(f"unsupported actual-control profile: {profile}")


def strict_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ControlProbeError(f"JSON artifact is absent/nonregular: {path}")

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ControlProbeError(f"duplicate JSON key {key!r} in {path}")
            result[key] = value
        return result

    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ControlProbeError(f"cannot read strict JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ControlProbeError(f"JSON root is not an object: {path}")
    return value


def write_exclusive(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        payload = canonical_json(value)
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def process_start_ticks(pid: int) -> int:
    stat_line = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8")
    return int(stat_line[stat_line.rfind(")") + 2 :].split()[19])


def socket_for_control() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, TOS_CONTROL)
    sock.setsockopt(socket.IPPROTO_IP, getattr(socket, "IP_RECVTOS", 13), 1)
    sock.setblocking(False)
    sock.bind(("10.71.0.10", 14600))
    return sock


def x25_crc(payload: bytes) -> int:
    crc = 0xFFFF
    for byte in payload:
        temporary = byte ^ (crc & 0xFF)
        temporary ^= (temporary << 4) & 0xFF
        crc = (
            (crc >> 8)
            ^ ((temporary << 8) & 0xFFFF)
            ^ ((temporary << 3) & 0xFFFF)
            ^ (temporary >> 4)
        ) & 0xFFFF
    return crc


def mavlink_v2_frame(
    message_id: int,
    payload: bytes,
    *,
    sequence: int,
    system_id: int = 255,
    component_id: int = 190,
) -> bytes:
    extra = MAVLINK_CRC_EXTRA.get(message_id)
    if extra is None or len(payload) > 255:
        raise ControlProbeError(f"unsupported MAVLink request message {message_id}")
    header = bytes(
        [len(payload), 0, 0, sequence & 0xFF, system_id, component_id]
    ) + message_id.to_bytes(3, "little")
    checksum = x25_crc(header + payload + bytes([extra]))
    return b"\xfd" + header + payload + checksum.to_bytes(2, "little")


@dataclass
class MavlinkSequencer:
    value: int = 0

    def frame(self, message_id: int, payload: bytes) -> bytes:
        frame = mavlink_v2_frame(message_id, payload, sequence=self.value)
        self.value = (self.value + 1) & 0xFF
        return frame


def marker_text(
    *, transport_nonce: str, phase_code: int, uav: int, sequence: int
) -> str:
    if not 1 <= phase_code <= 15:
        raise ControlProbeError("transport phase code must be in 1..15")
    base = f"AMS3{transport_nonce}{phase_code:x}11{uav:x}{sequence:04x}"
    return base + hashlib.sha256(base.encode("ascii")).hexdigest()[:6]


def encode_actual_control_request(
    *,
    run_nonce: str,
    transport_nonce: str,
    phase_code: int,
    uav: int,
    sequence: int,
    mavlink: MavlinkSequencer,
) -> dict[str, Any]:
    marker = marker_text(
        transport_nonce=transport_nonce,
        phase_code=phase_code,
        uav=uav,
        sequence=sequence,
    )
    marker_frame = mavlink.frame(
        253, struct.pack("<B50s", 6, marker.encode("ascii"))
    )
    command_frame = mavlink.frame(
        76,
        struct.pack(
            "<7fHBBB",
            148.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            512,
            uav,
            1,
            0,
        ),
    )
    return {
        "marker_text": marker,
        "marker_frame": marker_frame,
        "marker_frame_sha256": sha256_bytes(marker_frame),
        "command_frame": command_frame,
        "command_frame_sha256": sha256_bytes(command_frame),
        "record_nonce": sha256_bytes(marker.encode("ascii")),
        "full_run_nonce": run_nonce,
        "transport_nonce32": transport_nonce,
    }


class EventWriter:
    def __init__(self, path: Path, args: argparse.Namespace) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = path.open("xb", buffering=0)
        self.args = args
        self.sequence = 0
        self.previous_record_sha256: str | None = None
        self.last_fsync_ns = time.monotonic_ns()
        self.dirty = False

    def emit(self, event: str, **fields: Any) -> None:
        self.sequence += 1
        record = {
            "schema": EVENT_SCHEMA,
            "run_id": self.args.run_id,
            "runtime_id": self.args.runtime_id,
            "run_nonce": self.args.run_nonce,
            "profile": self.args.profile,
            "transport_nonce32": self.args.transport_nonce32,
            "transport_nonce_derivation": self.args.transport_nonce_derivation,
            "event_sequence": self.sequence,
            "previous_record_sha256": self.previous_record_sha256,
            "monotonic_ns": time.monotonic_ns(),
            "event": event,
            "role_subject": ROLE_SUBJECT,
            **fields,
        }
        payload = canonical_json(record)
        self.handle.write(payload)
        self.previous_record_sha256 = sha256_bytes(payload)
        self.dirty = True
        now_ns = time.monotonic_ns()
        if now_ns - self.last_fsync_ns >= 1_000_000_000:
            os.fsync(self.handle.fileno())
            self.last_fsync_ns = now_ns
            self.dirty = False

    def close(self) -> None:
        if self.dirty:
            os.fsync(self.handle.fileno())
            self.dirty = False
        self.handle.close()


@dataclass
class PendingRequest:
    phase: str
    uav: int
    sequence: int
    sent_monotonic_ns: int
    scheduled_send_monotonic_ns: int
    send_lateness_ns: int
    marker_frame_sha256: str
    command_frame_sha256: str
    record_nonce: str
    full_run_nonce: str
    transport_nonce32: str
    transport_nonce_derivation: str
    window_id: str
    transport_phase_code: int
    flow_group_id: str
    ordinal_send_slot: int
    transaction_id: str
    response_policy: str
    ack: dict[str, Any] | None = None
    telemetry: dict[str, Any] | None = None


@dataclass(frozen=True)
class WindowPolicy:
    window_id: str
    transport_phase_code: int
    start_monotonic_ns: int
    end_monotonic_ns: int
    offered_per_uav: int
    send_span_ms: int
    expected_engine_state: str
    response_policies: dict[int, str]
    minimum_quiet_drain_ns_by_uav: dict[int, int]
    flow_group_ids: dict[int, str]

    def response_policy_for(self, uav: int) -> str:
        return self.response_policies[uav]

    @property
    def response_policy_label(self) -> str:
        values = set(self.response_policies.values())
        return next(iter(values)) if len(values) == 1 else "mixed_per_uav"

    def slot_monotonic_ns(self, ordinal: int) -> int:
        if self.offered_per_uav <= 1:
            return self.start_monotonic_ns
        numerator = (ordinal - 1) * self.send_span_ms * 1_000_000
        return self.start_monotonic_ns + numerator // (self.offered_per_uav - 1)


class ActualSitlControlProbe:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        if not SAFE_ID.fullmatch(args.run_id) or re.fullmatch(r"[0-9a-f]{32}", args.runtime_id) is None:
            raise ControlProbeError("run_id/runtime_id is invalid")
        args.transport_nonce32, args.transport_nonce_derivation = transport_nonce32(
            args.profile, args.run_nonce
        )
        run = strict_json(args.run_dir / "raw/run_contract.json")
        if (
            run.get("contract") != PROFILE_RUN_CONTRACTS[args.profile]
            or any(run.get(key) != getattr(args, key) for key in ("run_id", "runtime_id", "run_nonce"))
        ):
            raise ControlProbeError("actual control argv/run contract identity differs")
        if args.profile == "m3":
            if (
                run.get("flight_runtime", {}).get("control_endpoint_form") != ENDPOINT_FORM
                or run.get("matrix", {}).get("sha256") != sha256_file(args.matrix)
            ):
                raise ControlProbeError("M3 run lacks the frozen actual endpoint form")
        else:
            if args.m3_result is None:
                raise ControlProbeError("M4 profile requires an accepted M3 result binding")
            accepted = strict_json(args.m3_result)
            api = accepted.get("actual_control_api")
            if (
                accepted.get("contract") != "ams.m3.external-matrix-validation/v1"
                or accepted.get("passed") is not True
                or accepted.get("acceptance_eligible") is not True
                or not isinstance(api, dict)
                or api.get("control_endpoint_form") != ENDPOINT_FORM
                or api.get("matrix_sha256") != sha256_file(args.matrix)
            ):
                raise ControlProbeError("M4 profile M3 API predecessor is not formally accepted")
        matrix = strict_json(args.matrix)
        cells = matrix.get("cells")
        expected_ids = [
            f"uav{uav}.control.{direction}"
            for uav in range(1, 6)
            for direction in ("downlink", "uplink")
        ]
        if not isinstance(cells, list) or [
            cell.get("cell_id")
            for cell in cells
            if isinstance(cell, dict) and cell.get("traffic_class") == "control"
        ] != expected_ids:
            raise ControlProbeError("matrix does not contain the exact ten control cells")
        self.writer = EventWriter(args.run_dir / "raw/actual_control/events.jsonl", args)
        self.sock = socket_for_control()
        self.clock_stop = threading.Event()
        self.clock_thread: threading.Thread | None = None
        if args.profile == "m3":
            if args.clock_socket is not None:
                raise ControlProbeError("M3 profile forbids an M4 clock socket")
        else:
            if args.clock_socket is None:
                raise ControlProbeError("M4 actual control profile requires --clock-socket")
            self.clock_thread = threading.Thread(
                target=beacon,
                args=(args.clock_socket, ROLE_SUBJECT, self.clock_stop),
                name="actual-control-clock",
            )
            self.clock_thread.start()
        os.environ.setdefault("MAVLINK20", "1")
        try:
            from pymavlink import mavutil
        except ImportError as exc:
            raise ControlProbeError(f"pymavlink is required: {exc}") from exc
        self.parser = mavutil.mavlink.MAVLink(None)
        self.parser.robust_parsing = True
        self.sequencer = MavlinkSequencer()
        self.pending: dict[int, PendingRequest] = {}
        self.quarantined_uavs: set[int] = set()
        self.expired_stopped_attempts: dict[int, list[dict[str, Any]]] = {
            uav: [] for uav in range(1, 6)
        }
        self.active_expired_stopped_attempts: dict[
            int, list[dict[str, Any]]
        ] = {uav: [] for uav in range(1, 6)}
        self.guarded_uavs: set[int] = set()
        self.last_stopped_timeout_monotonic_ns_by_uav: dict[int, int | None] = {
            uav: None for uav in range(1, 6)
        }
        self.expected_expired_per_uav = {uav: 0 for uav in range(1, 6)}
        self.active_phase: str | None = None
        self.processed_commands: set[str] = set()
        self.heartbeat_totals = {uav: 0 for uav in range(1, 6)}
        self.link_ready_written = False
        self.expected_peers = {
            (f"10.71.{uav}.10", 14600 + uav): uav for uav in range(1, 6)
        }

    @staticmethod
    def _frame_record(message: Any) -> dict[str, Any]:
        frame = bytes(message.get_msgbuf())
        return {
            "message_type": str(message.get_type()),
            "message_id": int(message.get_msgId()),
            "source_system": int(message.get_srcSystem()),
            "source_component": int(message.get_srcComponent()),
            "mavlink_frame_hex": frame.hex(),
            "mavlink_frame_sha256": sha256_bytes(frame),
            "mavlink_frame_size": len(frame),
        }

    def _write_link_ready_if_complete(self) -> None:
        if self.link_ready_written or any(value < 3 for value in self.heartbeat_totals.values()):
            return
        counts = {f"uav{uav}": self.heartbeat_totals[uav] for uav in range(1, 6)}
        write_exclusive(
            self.args.run_dir / "raw/state/actual-control.link-ready.json",
            {
                "contract": "ams.actual-sitl.control-link-ready/v1",
                "run_id": self.args.run_id,
                "runtime_id": self.args.runtime_id,
                "run_nonce": self.args.run_nonce,
                "profile": self.args.profile,
                "transport_nonce32": self.args.transport_nonce32,
                "transport_nonce_derivation": self.args.transport_nonce_derivation,
                "role_subject": ROLE_SUBJECT,
                "pid": os.getpid(),
                "heartbeat_counts": counts,
                "monotonic_ns": time.monotonic_ns(),
            },
        )
        self.writer.emit("actual_control_link_ready", heartbeat_counts=counts)
        self.link_ready_written = True

    def _complete_pending(self, uav: int, *, timed_out: bool) -> None:
        pending = self.pending.pop(uav)
        completed_ns = time.monotonic_ns()
        elapsed_ms = (completed_ns - pending.sent_monotonic_ns) / 1_000_000
        self.writer.emit(
            "transaction_result",
            phase=pending.phase,
            window_id=pending.window_id,
            transport_phase_code=pending.transport_phase_code,
            flow_group_id=pending.flow_group_id,
            ordinal_send_slot=pending.ordinal_send_slot,
            transaction_id=pending.transaction_id,
            uav=uav,
            sequence=pending.sequence,
            endpoint_form=ENDPOINT_FORM,
            downlink_cell_id=f"uav{uav}.control.downlink",
            uplink_cell_id=f"uav{uav}.control.uplink",
            record_nonce=pending.record_nonce,
            full_run_nonce=pending.full_run_nonce,
            transport_nonce32=pending.transport_nonce32,
            transport_nonce_derivation=pending.transport_nonce_derivation,
            sent_monotonic_ns=pending.sent_monotonic_ns,
            scheduled_send_monotonic_ns=pending.scheduled_send_monotonic_ns,
            send_lateness_ns=pending.send_lateness_ns,
            completed_monotonic_ns=completed_ns,
            command_frame_sha256=pending.command_frame_sha256,
            marker_frame_sha256=pending.marker_frame_sha256,
            ack=pending.ack,
            requested_telemetry=pending.telemetry,
            timed_out=timed_out,
            timeout_elapsed_ms=round(elapsed_ms, 6),
            timeout_contract_satisfied=not timed_out or elapsed_ms >= 3000.0,
            success=not timed_out and pending.ack is not None and pending.telemetry is not None,
        )
        if timed_out and pending.response_policy == "timeout_required":
            expired = {
                "uav": uav,
                "sequence": pending.sequence,
                "record_nonce": pending.record_nonce,
                "marker_frame_sha256": pending.marker_frame_sha256,
                "command_frame_sha256": pending.command_frame_sha256,
                "sent_monotonic_ns": pending.sent_monotonic_ns,
                "expired_monotonic_ns": completed_ns,
                "timeout_elapsed_ms": round(elapsed_ms, 6),
            }
            # Preserve an append-only in-memory history for the whole process,
            # while the active batch is the exact set the next recovery guard
            # must drain.  Clearing history here would make a second
            # timeout/recovery pair unauditable; accumulating the active batch
            # would make its cardinality depend on earlier windows.
            self.expired_stopped_attempts[uav].append(expired)
            self.active_expired_stopped_attempts[uav].append(expired)
            self.last_stopped_timeout_monotonic_ns_by_uav[uav] = completed_ns
            self.guarded_uavs.add(uav)
            self.writer.emit("stopped_attempt_quarantined", **expired)

    def _handle_message(
        self,
        message: Any,
        *,
        peer: tuple[str, int],
        received_ns: int,
        datagram_sha256: str,
    ) -> None:
        frame = self._frame_record(message)
        system_id = int(frame["source_system"])
        message_type = str(frame["message_type"])
        expected_uav = self.expected_peers.get(peer)
        if expected_uav is None or system_id != expected_uav or frame["source_component"] != 1:
            self.writer.emit(
                "foreign_control_message",
                peer_ip=peer[0],
                peer_udp_port=peer[1],
                received_monotonic_ns=received_ns,
                transport_payload_sha256=datagram_sha256,
                **frame,
            )
            raise ControlProbeError("foreign control source identity")
        common = {
            "uav": system_id,
            "peer_ip": peer[0],
            "peer_udp_port": peer[1],
            "received_monotonic_ns": received_ns,
            "transport_payload_sha256": datagram_sha256,
            **frame,
        }
        if message_type == "HEARTBEAT":
            self.heartbeat_totals[system_id] += 1
            self.writer.emit("real_heartbeat", **common)
            self._write_link_ready_if_complete()
            return
        pending = self.pending.get(system_id)
        if pending is None and message_type in {"COMMAND_ACK", "AUTOPILOT_VERSION"}:
            event = (
                "late_stopped_control_response"
                if system_id in self.guarded_uavs
                else "uncorrelated_control_response"
            )
            self.writer.emit(event, **common)
            raise ControlProbeError(f"{event} from uav{system_id}")
        if pending is None:
            return
        if pending.response_policy == "timeout_required" and message_type in {
            "COMMAND_ACK",
            "AUTOPILOT_VERSION",
        }:
            self.writer.emit(
                "forbidden_stopped_control_response",
                phase=self.active_phase,
                sequence=pending.sequence,
                **common,
            )
            raise ControlProbeError(f"stopped response from uav{system_id}")
        response = {
            **common,
            "phase": pending.phase,
            "sequence": pending.sequence,
            "request_command_frame_sha256": pending.command_frame_sha256,
        }
        if message_type == "COMMAND_ACK":
            command = int(getattr(message, "command", -1))
            result = int(getattr(message, "result", -1))
            response.update({"mavlink_command": command, "mavlink_result": result})
            if command == 512 and result == 0 and pending.ack is None:
                pending.ack = response
                self.writer.emit("real_command_ack", **response)
        elif message_type == "AUTOPILOT_VERSION" and pending.telemetry is None:
            pending.telemetry = response
            self.writer.emit("real_requested_telemetry", **response)
        if pending.ack is not None and pending.telemetry is not None:
            self._complete_pending(system_id, timed_out=False)

    def pump(self, timeout_s: float) -> None:
        readable, _writable, _errors = select.select([self.sock], [], [], timeout_s)
        if not readable:
            return
        while True:
            try:
                payload, ancillary, _flags, peer_raw = self.sock.recvmsg(65535, 128)
            except BlockingIOError:
                return
            peer = (str(peer_raw[0]), int(peer_raw[1]))
            received_ns = time.monotonic_ns()
            rx_tos: int | None = None
            for level, kind, data in ancillary:
                if level == socket.IPPROTO_IP and kind == getattr(socket, "IP_TOS", 1):
                    rx_tos = int.from_bytes(data, sys.byteorder)
            datagram_hash = sha256_bytes(payload)
            try:
                parsed = self.parser.parse_buffer(payload)
            except Exception as exc:
                self.writer.emit(
                    "control_parse_error",
                    peer_ip=peer[0],
                    peer_udp_port=peer[1],
                    received_monotonic_ns=received_ns,
                    rx_tos=rx_tos,
                    transport_payload_hex=payload.hex(),
                    transport_payload_sha256=datagram_hash,
                    transport_payload_size=len(payload),
                    error=f"{type(exc).__name__}: {exc}",
                )
                raise ControlProbeError("real control MAVLink parse failure") from exc
            messages = [] if parsed is None else list(parsed)
            self.writer.emit(
                "control_datagram_receive",
                peer_ip=peer[0],
                peer_udp_port=peer[1],
                received_monotonic_ns=received_ns,
                rx_tos=rx_tos,
                transport_payload_hex=payload.hex(),
                transport_payload_sha256=datagram_hash,
                transport_payload_size=len(payload),
                decoded_message_count=sum(str(message.get_type()) != "BAD_DATA" for message in messages),
            )
            for message in messages:
                if str(message.get_type()) != "BAD_DATA":
                    self._handle_message(
                        message,
                        peer=peer,
                        received_ns=received_ns,
                        datagram_sha256=datagram_hash,
                    )

    def pump_until(self, deadline_ns: int) -> None:
        while (remaining_ns := deadline_ns - time.monotonic_ns()) > 0:
            self.pump(min(0.05, remaining_ns / 1_000_000_000))

    def _expire_pending(self) -> None:
        now_ns = time.monotonic_ns()
        for uav in [
            value
            for value, pending in self.pending.items()
            if now_ns - pending.sent_monotonic_ns >= 3_000_000_000
        ]:
            pending = self.pending[uav]
            self._complete_pending(uav, timed_out=True)
            if pending.response_policy != "timeout_required":
                self.quarantined_uavs.add(uav)

    def send_request(self, policy: WindowPolicy, uav: int, sequence: int) -> None:
        if uav in self.pending or uav in self.quarantined_uavs:
            raise ControlProbeError(f"overlap/quarantine for uav{uav}")
        sent_ns = time.monotonic_ns()
        scheduled_ns = policy.slot_monotonic_ns(sequence)
        request = encode_actual_control_request(
            run_nonce=self.args.run_nonce,
            transport_nonce=self.args.transport_nonce32,
            phase_code=policy.transport_phase_code,
            uav=uav,
            sequence=sequence,
            mavlink=self.sequencer,
        )
        destination = (f"10.71.{uav}.10", 14600 + uav)
        marker_size = self.sock.sendto(request["marker_frame"], destination)
        command_size = self.sock.sendto(request["command_frame"], destination)
        self.pending[uav] = PendingRequest(
            phase=policy.window_id,
            uav=uav,
            sequence=sequence,
            sent_monotonic_ns=sent_ns,
            scheduled_send_monotonic_ns=scheduled_ns,
            send_lateness_ns=sent_ns - scheduled_ns,
            marker_frame_sha256=request["marker_frame_sha256"],
            command_frame_sha256=request["command_frame_sha256"],
            record_nonce=request["record_nonce"],
            full_run_nonce=request["full_run_nonce"],
            transport_nonce32=request["transport_nonce32"],
            transport_nonce_derivation=self.args.transport_nonce_derivation,
            window_id=policy.window_id,
            transport_phase_code=policy.transport_phase_code,
            flow_group_id=policy.flow_group_ids[uav],
            ordinal_send_slot=sequence,
            transaction_id=(
                f"{policy.window_id}:{policy.flow_group_ids[uav]}:{uav}:{sequence}"
            ),
            response_policy=policy.response_policy_for(uav),
        )
        self.writer.emit(
            "real_command_offered",
            phase=policy.window_id,
            window_id=policy.window_id,
            transport_phase_code=policy.transport_phase_code,
            flow_group_id=policy.flow_group_ids[uav],
            ordinal_send_slot=sequence,
            transaction_id=(
                f"{policy.window_id}:{policy.flow_group_ids[uav]}:{uav}:{sequence}"
            ),
            uav=uav,
            sequence=sequence,
            endpoint_form=ENDPOINT_FORM,
            cell_id=f"uav{uav}.control.downlink",
            flow_id=f"uav{uav}.control.downlink",
            record_nonce=request["record_nonce"],
            full_run_nonce=request["full_run_nonce"],
            marker_text=request["marker_text"],
            marker_frame_hex=request["marker_frame"].hex(),
            marker_frame_sha256=request["marker_frame_sha256"],
            marker_send_return_size=marker_size,
            command_frame_hex=request["command_frame"].hex(),
            command_frame_sha256=request["command_frame_sha256"],
            command_send_return_size=command_size,
            source_ip="10.71.0.10",
            source_udp_port=14600,
            destination_ip=destination[0],
            destination_udp_port=destination[1],
            tos=TOS_CONTROL,
            sent_monotonic_ns=sent_ns,
            scheduled_send_monotonic_ns=scheduled_ns,
            send_lateness_ns=sent_ns - scheduled_ns,
            requested_message_id=148,
            mavlink_command=512,
            target_system=uav,
            target_component=1,
        )

    def normalize_window(self, command: dict[str, Any]) -> WindowPolicy:
        if self.args.profile == "m3":
            phase = str(command.get("phase"))
            if command.get("action") != "phase" or phase not in PHASE_CODES:
                raise ControlProbeError("M3 actual-control command is not an exact phase")
            policy = WindowPolicy(
                window_id=phase,
                transport_phase_code=PHASE_CODES[phase],
                start_monotonic_ns=int(command["start_monotonic_ns"]),
                end_monotonic_ns=int(command["end_monotonic_ns"]),
                offered_per_uav=int(command["offered_per_cell"]),
                send_span_ms=int(command["send_span_ms"]),
                expected_engine_state=str(command["expected_engine_state"]),
                response_policies={
                    uav: (
                        "timeout_required"
                        if phase == "stopped"
                        else "ack_required"
                    )
                    for uav in range(1, 6)
                },
                minimum_quiet_drain_ns_by_uav={
                    uav: (10_000_000_000 if phase == "recovery" else 0)
                    for uav in range(1, 6)
                },
                flow_group_ids={
                    uav: f"uav{uav}.control.downlink" for uav in range(1, 6)
                },
            )
        else:
            exact_keys = {
                "action",
                "endpoint",
                "run_id",
                "runtime_id",
                "run_nonce",
                "profile",
                "window_id",
                "transport_phase_code",
                "start_monotonic_ns",
                "end_monotonic_ns",
                "offered_per_uav",
                "send_span_ms",
                "expected_engine_state",
                "response_policies",
                "minimum_quiet_drain_ns_by_uav",
                "flow_group_ids",
            }
            if set(command) != exact_keys or command.get("action") != "window":
                raise ControlProbeError("M4 actual-control window command keys differ")
            if command.get("profile") != self.args.profile:
                raise ControlProbeError("M4 window profile differs from probe profile")
            window_id = str(command.get("window_id"))
            if (
                not SAFE_ID.fullmatch(window_id)
                or not isinstance(command.get("flow_group_ids"), dict)
                or set(command["flow_group_ids"])
                != {f"uav{uav}" for uav in range(1, 6)}
                or any(
                    not SAFE_ID.fullmatch(str(value))
                    for value in command["flow_group_ids"].values()
                )
                or not isinstance(command.get("response_policies"), dict)
                or set(command["response_policies"])
                != {f"uav{uav}" for uav in range(1, 6)}
                or not isinstance(
                    command.get("minimum_quiet_drain_ns_by_uav"), dict
                )
                or set(command["minimum_quiet_drain_ns_by_uav"])
                != {f"uav{uav}" for uav in range(1, 6)}
                or any(
                    isinstance(value, bool) or not isinstance(value, int)
                    for value in command[
                        "minimum_quiet_drain_ns_by_uav"
                    ].values()
                )
            ):
                raise ControlProbeError("M4 window identity/flow groups are invalid")
            policy = WindowPolicy(
                window_id=window_id,
                transport_phase_code=int(command["transport_phase_code"]),
                start_monotonic_ns=int(command["start_monotonic_ns"]),
                end_monotonic_ns=int(command["end_monotonic_ns"]),
                offered_per_uav=int(command["offered_per_uav"]),
                send_span_ms=int(command["send_span_ms"]),
                expected_engine_state=str(command["expected_engine_state"]),
                response_policies={
                    uav: str(command["response_policies"][f"uav{uav}"])
                    for uav in range(1, 6)
                },
                minimum_quiet_drain_ns_by_uav={
                    uav: int(
                        command["minimum_quiet_drain_ns_by_uav"][f"uav{uav}"]
                    )
                    for uav in range(1, 6)
                },
                flow_group_ids={
                    uav: str(command["flow_group_ids"][f"uav{uav}"])
                    for uav in range(1, 6)
                },
            )
        if (
            not 1 <= policy.transport_phase_code <= 15
            or not 1 <= policy.offered_per_uav <= 10_000
            or policy.send_span_ms < 0
            or policy.start_monotonic_ns >= policy.end_monotonic_ns
            or policy.slot_monotonic_ns(policy.offered_per_uav)
            + 3_000_000_000
            > policy.end_monotonic_ns
            or set(policy.response_policies) != set(range(1, 6))
            or any(
                value not in {"ack_required", "timeout_required"}
                for value in policy.response_policies.values()
            )
            or set(policy.minimum_quiet_drain_ns_by_uav) != set(range(1, 6))
            or (
                "timeout_required" in policy.response_policies.values()
                and policy.offered_per_uav > 1
                and policy.send_span_ms * 1_000_000
                // (policy.offered_per_uav - 1)
                < 3_000_000_000
            )
            or any(
                value < 0
                for value in policy.minimum_quiet_drain_ns_by_uav.values()
            )
        ):
            raise ControlProbeError("actual-control window timing/policy is invalid")
        return policy

    def consume_stopped_drain_guard(self, policy: WindowPolicy) -> None:
        """Consume exactly one timeout batch while retaining full-run history."""

        if not self.guarded_uavs:
            if any(policy.minimum_quiet_drain_ns_by_uav.values()):
                raise ControlProbeError("quiet drain requested without expired attempts")
            return
        self.pump(0.0)
        expired_counts = {
            f"uav{uav}": len(self.active_expired_stopped_attempts[uav])
            for uav in range(1, 6)
        }
        history_counts = {
            f"uav{uav}": len(self.expired_stopped_attempts[uav])
            for uav in range(1, 6)
        }
        expected_counts = {
            f"uav{uav}": self.expected_expired_per_uav[uav]
            for uav in range(1, 6)
        }
        guarded = sorted(self.guarded_uavs)
        last_timeout_by_uav = {
            f"uav{uav}": self.last_stopped_timeout_monotonic_ns_by_uav[uav]
            for uav in guarded
        }
        quiet_by_uav = {
            f"uav{uav}": (
                policy.start_monotonic_ns
                - int(self.last_stopped_timeout_monotonic_ns_by_uav[uav])
            )
            for uav in guarded
            if self.last_stopped_timeout_monotonic_ns_by_uav[uav] is not None
        }
        required_by_uav = {
            f"uav{uav}": policy.minimum_quiet_drain_ns_by_uav[uav]
            for uav in range(1, 6)
        }
        if (
            expired_counts != expected_counts
            or set(last_timeout_by_uav) != {f"uav{uav}" for uav in guarded}
            or any(value is None for value in last_timeout_by_uav.values())
            or any(
                policy.minimum_quiet_drain_ns_by_uav[uav] <= 0
                or quiet_by_uav.get(f"uav{uav}", -1)
                < policy.minimum_quiet_drain_ns_by_uav[uav]
                for uav in guarded
            )
        ):
            raise ControlProbeError(
                "next window lacks durable expired-attempt quiet drain proof"
            )
        last_timeout = max(int(value) for value in last_timeout_by_uav.values())
        quiet_drain = min(quiet_by_uav.values())
        self.writer.emit(
            "recovery_drain_guard_passed",
            phase=policy.window_id,
            window_id=policy.window_id,
            endpoint_form=ENDPOINT_FORM,
            expired_attempt_counts=expired_counts,
            guarded_uavs=[f"uav{uav}" for uav in guarded],
            last_stopped_timeout_monotonic_ns=last_timeout,
            last_stopped_timeout_monotonic_ns_by_uav=last_timeout_by_uav,
            recovery_start_monotonic_ns=policy.start_monotonic_ns,
            quiet_drain_ns=quiet_drain,
            quiet_drain_ns_by_uav=quiet_by_uav,
            required_quiet_drain_ns=max(required_by_uav.values()),
            required_quiet_drain_ns_by_uav=required_by_uav,
            expired_attempts={
                f"uav{uav}": self.active_expired_stopped_attempts[uav]
                for uav in range(1, 6)
            },
            expired_attempt_history_counts=history_counts,
        )
        self.guarded_uavs.clear()
        self.active_expired_stopped_attempts = {
            uav: [] for uav in range(1, 6)
        }
        self.expected_expired_per_uav = {uav: 0 for uav in range(1, 6)}

    def execute_window(self, command: dict[str, Any], command_hash: str) -> None:
        policy = self.normalize_window(command)
        self.pump_until(policy.start_monotonic_ns)
        self.active_phase = policy.window_id
        if self.quarantined_uavs:
            raise ControlProbeError("actual-control window inherited an ACK timeout")
        self.consume_stopped_drain_guard(policy)
        heartbeats_before = dict(self.heartbeat_totals)
        self.writer.emit(
            "actual_control_phase_start",
            phase=policy.window_id,
            window_id=policy.window_id,
            transport_phase_code=policy.transport_phase_code,
            command_sha256=command_hash,
            offered_per_downlink_cell=policy.offered_per_uav,
            declared_start_monotonic_ns=policy.start_monotonic_ns,
            declared_end_monotonic_ns=policy.end_monotonic_ns,
            send_span_ms=policy.send_span_ms,
            response_policy=policy.response_policy_label,
            response_policies={
                f"uav{uav}": policy.response_policy_for(uav)
                for uav in range(1, 6)
            },
            minimum_quiet_drain_ns_by_uav={
                f"uav{uav}": policy.minimum_quiet_drain_ns_by_uav[uav]
                for uav in range(1, 6)
            },
            expected_engine_state=policy.expected_engine_state,
            flow_group_ids={
                f"uav{uav}": policy.flow_group_ids[uav] for uav in range(1, 6)
            },
        )
        next_sequence = {uav: 1 for uav in range(1, 6)}
        offered_counts = {uav: 0 for uav in range(1, 6)}

        while time.monotonic_ns() < policy.end_monotonic_ns:
            self.pump(0.01)
            self._expire_pending()
            now_ns = time.monotonic_ns()
            for uav in range(1, 6):
                sequence = next_sequence[uav]
                if sequence > policy.offered_per_uav:
                    continue
                scheduled_ns = policy.slot_monotonic_ns(sequence)
                if now_ns < scheduled_ns:
                    continue
                if uav in self.pending:
                    self.writer.emit(
                        "ordinal_send_slot_overlap",
                        phase=policy.window_id,
                        window_id=policy.window_id,
                        uav=uav,
                        ordinal_send_slot=sequence,
                        scheduled_send_monotonic_ns=scheduled_ns,
                    )
                    raise ControlProbeError(
                        f"{policy.window_id}/uav{uav} prior outcome overlaps send slot"
                    )
                self.send_request(policy, uav, sequence)
                offered_counts[uav] += 1
                next_sequence[uav] += 1
            if (
                all(
                    next_sequence[uav] > policy.offered_per_uav
                    for uav in range(1, 6)
                )
                and not self.pending
            ):
                self.pump_until(policy.end_monotonic_ns)
                break
        self._expire_pending()
        if self.pending:
            for uav, pending in self.pending.items():
                self.writer.emit(
                    "phase_ended_before_outcome_timeout",
                    phase=policy.window_id,
                    window_id=policy.window_id,
                    uav=uav,
                    sequence=pending.sequence,
                    sent_monotonic_ns=pending.sent_monotonic_ns,
                )
            raise ControlProbeError("window ended before all exact outcomes")
        if any(
            next_sequence[uav] <= policy.offered_per_uav for uav in range(1, 6)
        ):
            raise ControlProbeError("window ended before every declared send slot")
        self.expected_expired_per_uav = {
            uav: (
                policy.offered_per_uav
                if policy.response_policy_for(uav) == "timeout_required"
                else 0
            )
            for uav in range(1, 6)
        }
        heartbeat_counts = {
            f"uav{uav}": self.heartbeat_totals[uav] - heartbeats_before[uav]
            for uav in range(1, 6)
        }
        offered_map = {
            f"uav{uav}": offered_counts[uav] for uav in range(1, 6)
        }
        quarantine = [f"uav{uav}" for uav in sorted(self.quarantined_uavs)]
        self.writer.emit(
            "actual_control_phase_complete",
            phase=policy.window_id,
            window_id=policy.window_id,
            transport_phase_code=policy.transport_phase_code,
            command_sha256=command_hash,
            expected_engine_state=policy.expected_engine_state,
            response_policy=policy.response_policy_label,
            response_policies={
                f"uav{uav}": policy.response_policy_for(uav)
                for uav in range(1, 6)
            },
            heartbeat_counts=heartbeat_counts,
            offered_counts=offered_map,
            quarantined_uavs=quarantine,
        )
        write_exclusive(
            self.args.run_dir
            / f"raw/state/actual-control.{policy.window_id}.done.json",
            {
                "endpoint": "actual-control",
                "profile": self.args.profile,
                "phase": policy.window_id,
                "window_id": policy.window_id,
                "transport_phase_code": policy.transport_phase_code,
                "command_sha256": command_hash,
                "heartbeat_counts": heartbeat_counts,
                "offered_counts": offered_map,
                "quarantined_uavs": quarantine,
                "completed_monotonic_ns": time.monotonic_ns(),
            },
        )
        self.active_phase = None

    def run(self) -> None:
        start_ticks = process_start_ticks(os.getpid())
        self.writer.emit(
            "actual_control_socket_ready",
            pid=os.getpid(),
            process_start_ticks=start_ticks,
            namespace="ams-gcs",
            bound_socket=["10.71.0.10", 14600],
            full_run_nonce=self.args.run_nonce,
            receive_buffer_bytes=self.sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF),
            send_buffer_bytes=self.sock.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF),
        )
        write_exclusive(
            self.args.run_dir / "raw/state/actual-control.socket-ready.json",
            {
                "contract": "ams.actual-sitl.control-socket-ready/v1",
                "run_id": self.args.run_id,
                "runtime_id": self.args.runtime_id,
                "run_nonce": self.args.run_nonce,
                "profile": self.args.profile,
                "transport_nonce32": self.args.transport_nonce32,
                "transport_nonce_derivation": self.args.transport_nonce_derivation,
                "role_subject": ROLE_SUBJECT,
                "pid": os.getpid(),
                "start_ticks": start_ticks,
                "bound_socket": ["10.71.0.10", 14600],
                "monotonic_ns": time.monotonic_ns(),
            },
        )
        command_dir = self.args.run_dir / "raw/control/actual-control"
        while True:
            pending_paths = [
                path
                for path in sorted(command_dir.glob("*.json"))
                if path.name not in self.processed_commands
            ]
            if not pending_paths:
                self.pump(0.05)
                continue
            path = pending_paths[0]
            command = strict_json(path)
            command_hash = sha256_file(path)
            self.processed_commands.add(path.name)
            if (
                any(command.get(key) != getattr(self.args, key) for key in ("run_id", "runtime_id", "run_nonce"))
                or command.get("endpoint") != "actual-control"
            ):
                raise ControlProbeError(f"command identity mismatch: {path}")
            if command.get("action") == "shutdown":
                self.pump_until(int(command["not_before_monotonic_ns"]))
                self.writer.emit("actual_control_shutdown", command_sha256=command_hash)
                return
            self.execute_window(command, command_hash)

    def close(self) -> None:
        self.clock_stop.set()
        if self.clock_thread is not None:
            self.clock_thread.join(timeout=2.0)
            if self.clock_thread.is_alive():
                raise ControlProbeError("actual control clock beacon did not stop")
        self.sock.close()
        self.writer.close()


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--runtime-id", required=True)
    parser.add_argument("--run-nonce", required=True)
    parser.add_argument(
        "--profile", choices=tuple(PROFILE_RUN_CONTRACTS), required=True
    )
    parser.add_argument("--m3-result", type=Path)
    parser.add_argument("--clock-socket", type=Path)
    parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        probe = ActualSitlControlProbe(args)
        try:
            probe.run()
        finally:
            probe.close()
    except (ControlProbeError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"FAIL actual SITL control probe: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
