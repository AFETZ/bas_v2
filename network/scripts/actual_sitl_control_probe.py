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
from typing import Any, Iterable, Mapping

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
    "m4_capacity": "ams.m4.capacity_run/v3",
    "m4_causality": "ams.m4.causality_run/v2",
}
HEX_NONCE = re.compile(r"^(?:[0-9a-f]{32}|[0-9a-f]{64})$")
SAFE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
PHASE_CODES = {"positive": 1, "stopped": 2, "recovery": 3}
MAVLINK_CRC_EXTRA = {
    0: 50,
    75: 158,
    76: 152,
    77: 143,
    111: 34,
    148: 178,
    253: 83,
}
TOS_CONTROL = 184
MAX_HEARTBEAT_HISTORY_PER_UAV = 10_000
MAX_RAW_RESPONSE_HISTORY_PER_UAV = 10_000
OUTCOME_TIMEOUT_NS = 3_000_000_000
MAX_RETIRED_TIMESYNC_TOKENS = 100_000
CORRELATED_TIMESYNC_POLICY = "correlated_timesync_required"


class ControlProbeError(RuntimeError):
    """The real control probe cannot continue without ambiguous evidence."""


def heartbeat_counts_for_window(
    history: Mapping[int, Iterable[int]], start_ns: int, end_ns: int
) -> dict[str, int]:
    """Count received heartbeats in the exact half-open causal window."""

    expected_uavs = set(range(1, 6))
    if (
        isinstance(start_ns, bool)
        or not isinstance(start_ns, int)
        or isinstance(end_ns, bool)
        or not isinstance(end_ns, int)
        or start_ns >= end_ns
        or set(history) != expected_uavs
    ):
        raise ControlProbeError("heartbeat window/history contract differs")
    counts: dict[str, int] = {}
    for uav in sorted(expected_uavs):
        previous_ns = -1
        count = 0
        for received_ns in history[uav]:
            if (
                isinstance(received_ns, bool)
                or not isinstance(received_ns, int)
                or received_ns < previous_ns
            ):
                raise ControlProbeError("heartbeat receive history is not monotonic")
            previous_ns = received_ns
            if start_ns <= received_ns < end_ns:
                count += 1
        counts[f"uav{uav}"] = count
    return counts


def raw_response_counts_for_window(
    history: Mapping[int, Iterable[int]], start_ns: int, end_ns: int
) -> dict[str, int]:
    """Count raw vehicle responses in the exact half-open causal window."""

    try:
        return heartbeat_counts_for_window(history, start_ns, end_ns)
    except ControlProbeError as exc:
        raise ControlProbeError("raw response window/history contract differs") from exc


def validate_m4_window_liveness(
    policy: "WindowPolicy",
    heartbeat_counts: Mapping[str, int],
    raw_ack_counts: Mapping[str, int],
    raw_telemetry_counts: Mapping[str, int],
) -> None:
    """Require exact five-UAV liveness without treating raw replies as outcomes."""

    labels = {f"uav{uav}" for uav in range(1, 6)}
    if any(set(value) != labels for value in (
        heartbeat_counts,
        raw_ack_counts,
        raw_telemetry_counts,
    )):
        raise ControlProbeError("M4 window liveness maps are not exact")
    for uav in range(1, 6):
        label = f"uav{uav}"
        values = (
            heartbeat_counts[label],
            raw_ack_counts[label],
            raw_telemetry_counts[label],
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in values
        ):
            raise ControlProbeError("M4 window liveness count is invalid")
        response_policy = policy.response_policy_for(uav)
        if response_policy == CORRELATED_TIMESYNC_POLICY:
            if heartbeat_counts[label] < 3:
                raise ControlProbeError(
                    f"{policy.window_id}/{label} lacks three in-window heartbeats"
                )
            if raw_ack_counts[label] < 1 or raw_telemetry_counts[label] < 1:
                raise ControlProbeError(
                    f"{policy.window_id}/{label} lacks raw ACK/telemetry liveness"
                )
        elif response_policy == "timeout_required":
            if raw_ack_counts[label] != 0 or raw_telemetry_counts[label] != 0:
                raise ControlProbeError(
                    f"{policy.window_id}/{label} has forbidden raw stopped responses"
                )
        else:
            raise ControlProbeError("M4 window liveness policy is unsupported")


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


def timesync_token(
    *, run_nonce: str, phase_code: int, uav: int, ordinal: int
) -> int:
    """Derive one positive signed-63 correlation token without collisions in a run."""

    if (
        HEX_NONCE.fullmatch(run_nonce) is None
        or len(run_nonce) != 64
        or isinstance(phase_code, bool)
        or not isinstance(phase_code, int)
        or not 1 <= phase_code <= 15
        or isinstance(uav, bool)
        or not isinstance(uav, int)
        or not 1 <= uav <= 5
        or isinstance(ordinal, bool)
        or not isinstance(ordinal, int)
        or not 1 <= ordinal <= 0xFFFF
    ):
        raise ControlProbeError("M4 TIMESYNC correlation identity is invalid")
    run_prefix = int.from_bytes(
        hashlib.sha256(bytes.fromhex(run_nonce)).digest()[:5], "big"
    )
    token = (run_prefix << 23) | (phase_code << 19) | (uav << 16) | ordinal
    if not 0 < token < (1 << 63):
        raise ControlProbeError("M4 TIMESYNC correlation token is outside signed-63")
    return token


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


def require_control_rx_tos(
    ancillary: Iterable[tuple[int, int, bytes]], flags: int
) -> int:
    """Fail closed unless one complete IPv4 TOS cmsg proves EF control."""

    if flags & (
        getattr(socket, "MSG_TRUNC", 0) | getattr(socket, "MSG_CTRUNC", 0)
    ):
        raise ControlProbeError("control UDP datagram or ancillary data was truncated")
    values = [
        int(data[0])
        for level, kind, data in ancillary
        if level == socket.IPPROTO_IP and kind == socket.IP_TOS
        and len(data) == 1
    ]
    malformed_tos = any(
        level == socket.IPPROTO_IP
        and kind == socket.IP_TOS
        and len(data) != 1
        for level, kind, data in ancillary
    )
    if malformed_tos or values != [TOS_CONTROL]:
        raise ControlProbeError(
            f"control UDP datagram TOS differs: {values!r}, expected [{TOS_CONTROL}]"
        )
    return values[0]


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


def encode_m4_correlated_control_request(
    *,
    run_nonce: str,
    transport_nonce: str,
    phase_code: int,
    uav: int,
    sequence: int,
    mavlink: MavlinkSequencer,
) -> dict[str, Any]:
    """Encode the M4-only atomic command plus token-bearing TIMESYNC datagram."""

    request = encode_actual_control_request(
        run_nonce=run_nonce,
        transport_nonce=transport_nonce,
        phase_code=phase_code,
        uav=uav,
        sequence=sequence,
        mavlink=mavlink,
    )
    token = timesync_token(
        run_nonce=run_nonce,
        phase_code=phase_code,
        uav=uav,
        ordinal=sequence,
    )
    timesync_frame = mavlink.frame(111, struct.pack("<qq", 0, token))
    datagram = request["marker_frame"] + request["command_frame"] + timesync_frame
    return {
        **request,
        "timesync_request_tc1": 0,
        "timesync_request_ts1": token,
        "timesync_frame": timesync_frame,
        "timesync_frame_sha256": sha256_bytes(timesync_frame),
        "request_datagram": datagram,
        "request_datagram_sha256": sha256_bytes(datagram),
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
    timesync_token: int | None = None
    timesync_frame_sha256: str | None = None
    request_datagram_sha256: str | None = None
    request_datagram_size: int | None = None
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

    def correlated_pending_bound(self) -> int:
        """Return the exact maximum number of live three-second token slots."""

        if self.offered_per_uav <= 1:
            return 1
        minimum_gap_ns = (
            self.send_span_ms * 1_000_000 // (self.offered_per_uav - 1)
        )
        if minimum_gap_ns <= 0:
            raise ControlProbeError("correlated TIMESYNC slot gap is not positive")
        return min(
            self.offered_per_uav,
            (OUTCOME_TIMEOUT_NS + minimum_gap_ns - 1) // minimum_gap_ns,
        )


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
        # Parser state must never cross a UDP datagram or peer.  A new strict
        # parser is created for every recvmsg below and its reconstructed frame
        # bytes must consume the complete datagram exactly.
        self.mavlink_dialect = mavutil.mavlink
        self.sequencer = MavlinkSequencer()
        self.pending: dict[int, PendingRequest] = {}
        self.correlated_pending: dict[tuple[int, int], PendingRequest] = {}
        self.retired_timesync_tokens: dict[tuple[int, int], dict[str, Any]] = {}
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
        self.active_policy: WindowPolicy | None = None
        self.processed_commands: set[str] = set()
        self.heartbeat_totals = {uav: 0 for uav in range(1, 6)}
        self.raw_command_ack_totals = {uav: 0 for uav in range(1, 6)}
        self.raw_autopilot_version_totals = {uav: 0 for uav in range(1, 6)}
        self.raw_command_ack_received_monotonic_ns: dict[int, list[int]] = {
            uav: [] for uav in range(1, 6)
        }
        self.raw_autopilot_version_received_monotonic_ns: dict[
            int, list[int]
        ] = {uav: [] for uav in range(1, 6)}
        self.heartbeat_received_monotonic_ns: dict[int, list[int]] = {
            uav: [] for uav in range(1, 6)
        }
        self.link_ready_written = False
        self.expected_peers = {
            (f"10.71.{uav}.10", 14600 + uav): uav for uav in range(1, 6)
        }
        # Q3 owns this probe.  Import the Q4-only flight helper only for the
        # capacity profile so M3/causality cannot execute unconsumed Q4 bytes.
        self.airborne_controller: Any | None = None
        if args.profile == "m4_capacity":
            from network.scripts.m4_capacity_airborne import (
                AIRBORNE_GATE_CONTRACT,
                CapacityAirborneController,
            )

            gate = run.get("airborne_gate")
            if not isinstance(gate, dict) or gate.get("contract") != AIRBORNE_GATE_CONTRACT:
                raise ControlProbeError("M4 capacity run lacks the airborne gate")
            self.airborne_controller = CapacityAirborneController(
                run_nonce=args.run_nonce,
                gate=gate,
                sock=self.sock,
                sequencer=self.sequencer,
                writer=self.writer,
                pump=self.pump,
            )

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
        m4_correlation = (
            {}
            if pending.timesync_token is None
            else {
                "correlation_kind": "mavlink_timesync_echo_v1",
                "timesync_request_tc1": 0,
                "timesync_request_ts1": pending.timesync_token,
                "timesync_request_frame_sha256": pending.timesync_frame_sha256,
                "request_transport_payload_sha256": pending.request_datagram_sha256,
                "request_transport_payload_size": pending.request_datagram_size,
                "timesync_response": None,
            }
        )
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
            **m4_correlation,
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

    def _ensure_correlated_state(self) -> None:
        if not hasattr(self, "correlated_pending"):
            self.correlated_pending = {}
        if not hasattr(self, "retired_timesync_tokens"):
            self.retired_timesync_tokens = {}

    def _retire_timesync(
        self, key: tuple[int, int], pending: PendingRequest, *, outcome: str
    ) -> None:
        self._ensure_correlated_state()
        if key in self.retired_timesync_tokens:
            raise ControlProbeError("TIMESYNC correlation token was retired twice")
        if len(self.retired_timesync_tokens) >= MAX_RETIRED_TIMESYNC_TOKENS:
            raise ControlProbeError("TIMESYNC correlation tombstone bound exceeded")
        self.retired_timesync_tokens[key] = {
            "outcome": outcome,
            "transaction_id": pending.transaction_id,
            "window_id": pending.window_id,
            "sequence": pending.sequence,
            "late_seen": False,
        }

    def _complete_correlated(
        self,
        key: tuple[int, int],
        *,
        timed_out: bool,
        timesync_response: dict[str, Any] | None = None,
        completed_ns: int | None = None,
    ) -> None:
        self._ensure_correlated_state()
        pending = self.correlated_pending.pop(key)
        completed_ns = time.monotonic_ns() if completed_ns is None else completed_ns
        elapsed_ms = (completed_ns - pending.sent_monotonic_ns) / 1_000_000
        if timed_out == (timesync_response is not None):
            raise ControlProbeError("TIMESYNC outcome/result union is invalid")
        self.writer.emit(
            "transaction_result",
            phase=pending.phase,
            window_id=pending.window_id,
            transport_phase_code=pending.transport_phase_code,
            flow_group_id=pending.flow_group_id,
            ordinal_send_slot=pending.ordinal_send_slot,
            transaction_id=pending.transaction_id,
            uav=pending.uav,
            sequence=pending.sequence,
            endpoint_form=ENDPOINT_FORM,
            downlink_cell_id=f"uav{pending.uav}.control.downlink",
            uplink_cell_id=f"uav{pending.uav}.control.uplink",
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
            correlation_kind="mavlink_timesync_echo_v1",
            timesync_request_tc1=0,
            timesync_request_ts1=pending.timesync_token,
            timesync_request_frame_sha256=pending.timesync_frame_sha256,
            request_transport_payload_sha256=pending.request_datagram_sha256,
            request_transport_payload_size=pending.request_datagram_size,
            timesync_response=timesync_response,
            ack=None,
            requested_telemetry=None,
            timed_out=timed_out,
            timeout_elapsed_ms=round(elapsed_ms, 6),
            timeout_contract_satisfied=not timed_out or elapsed_ms >= 3000.0,
            success=not timed_out and timesync_response is not None,
        )
        self._retire_timesync(
            key, pending, outcome="timeout" if timed_out else "success"
        )

    def _handle_timesync_response(
        self,
        *,
        uav: int,
        message: Any,
        received_ns: int,
        common: dict[str, Any],
    ) -> None:
        tc1 = getattr(message, "tc1", None)
        ts1 = getattr(message, "ts1", None)
        if (
            tc1 == 0
            and not isinstance(tc1, bool)
            and isinstance(ts1, int)
            and not isinstance(ts1, bool)
            and 0 < ts1 < (1 << 63)
        ):
            self.writer.emit(
                "ambient_timesync_request",
                timesync_tc1=tc1,
                timesync_ts1=ts1,
                **common,
            )
            return
        if (
            isinstance(tc1, bool)
            or not isinstance(tc1, int)
            or tc1 <= 0
            or isinstance(ts1, bool)
            or not isinstance(ts1, int)
            or not 0 < ts1 < (1 << 63)
        ):
            self.writer.emit("invalid_timesync_echo", **common)
            raise ControlProbeError("TIMESYNC echo fields are invalid")
        legacy_pending = self.pending.get(uav)
        if (
            legacy_pending is not None
            and legacy_pending.response_policy == "timeout_required"
        ):
            self.writer.emit(
                "forbidden_stopped_control_response",
                phase=self.active_phase,
                sequence=legacy_pending.sequence,
                timesync_tc1=tc1,
                timesync_ts1=ts1,
                **common,
            )
            raise ControlProbeError(f"stopped TIMESYNC response from uav{uav}")
        # Locked ArduPilot replies with its local clock in ``tc1`` and copies
        # our request ``ts1`` token into response ``ts1`` exactly.
        key = (uav, ts1)
        self._ensure_correlated_state()
        pending = self.correlated_pending.get(key)
        if pending is None:
            retired = self.retired_timesync_tokens.get(key)
            if retired is None:
                self.writer.emit(
                    "uncorrelated_timesync_echo",
                    timesync_tc1=tc1,
                    timesync_ts1=ts1,
                    **common,
                )
                raise ControlProbeError("unknown TIMESYNC correlation token")
            if retired["outcome"] != "timeout" or retired["late_seen"] is True:
                self.writer.emit(
                    "duplicate_timesync_echo",
                    transaction_id=retired["transaction_id"],
                    timesync_tc1=tc1,
                    timesync_ts1=ts1,
                    **common,
                )
                raise ControlProbeError("duplicate TIMESYNC correlation response")
            retired["late_seen"] = True
            self.writer.emit(
                "late_timesync_echo",
                transaction_id=retired["transaction_id"],
                window_id=retired["window_id"],
                ordinal_send_slot=retired["sequence"],
                timesync_tc1=tc1,
                timesync_ts1=ts1,
                **common,
            )
            return
        response = {
            **common,
            "timesync_tc1": tc1,
            "timesync_ts1": ts1,
        }
        if received_ns - pending.sent_monotonic_ns >= OUTCOME_TIMEOUT_NS:
            self._complete_correlated(
                key, timed_out=True, completed_ns=received_ns
            )
            retired = self.retired_timesync_tokens[key]
            retired["late_seen"] = True
            self.writer.emit(
                "late_timesync_echo",
                transaction_id=pending.transaction_id,
                window_id=pending.window_id,
                ordinal_send_slot=pending.ordinal_send_slot,
                timesync_tc1=tc1,
                timesync_ts1=ts1,
                **common,
            )
            return
        self._complete_correlated(
            key,
            timed_out=False,
            timesync_response=response,
            completed_ns=received_ns,
        )

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
        airborne = getattr(self, "airborne_controller", None)
        if airborne is not None and airborne.observe_message(
            message_type=message_type,
            uav=system_id,
            message=message,
            received_ns=received_ns,
            common=common,
        ):
            return
        if message_type == "HEARTBEAT":
            history = self.heartbeat_received_monotonic_ns[system_id]
            if len(history) >= MAX_HEARTBEAT_HISTORY_PER_UAV:
                self.writer.emit(
                    "heartbeat_history_overflow",
                    maximum_per_uav=MAX_HEARTBEAT_HISTORY_PER_UAV,
                    **common,
                )
                raise ControlProbeError("heartbeat receive history exceeded its bound")
            history.append(received_ns)
            self.heartbeat_totals[system_id] += 1
            self.writer.emit("real_heartbeat", **common)
            self._write_link_ready_if_complete()
            return
        if message_type == "TIMESYNC":
            self._handle_timesync_response(
                uav=system_id,
                message=message,
                received_ns=received_ns,
                common=common,
            )
            return
        pending = self.pending.get(system_id)
        active_policy = getattr(self, "active_policy", None)
        active_response_policy = (
            active_policy.response_policy_for(system_id)
            if active_policy is not None
            else None
        )
        if (
            pending is not None
            and pending.response_policy == "timeout_required"
            and message_type in {"COMMAND_ACK", "AUTOPILOT_VERSION"}
        ):
            self.writer.emit(
                "forbidden_stopped_control_response",
                phase=self.active_phase,
                sequence=pending.sequence,
                **common,
            )
            raise ControlProbeError(f"stopped response from uav{system_id}")
        if (
            active_response_policy == CORRELATED_TIMESYNC_POLICY
            and message_type in {"COMMAND_ACK", "AUTOPILOT_VERSION"}
        ):
            if not hasattr(self, "raw_command_ack_totals"):
                self.raw_command_ack_totals = {uav: 0 for uav in range(1, 6)}
            if not hasattr(self, "raw_autopilot_version_totals"):
                self.raw_autopilot_version_totals = {
                    uav: 0 for uav in range(1, 6)
                }
            if not hasattr(self, "raw_command_ack_received_monotonic_ns"):
                self.raw_command_ack_received_monotonic_ns = {
                    uav: [] for uav in range(1, 6)
                }
            if not hasattr(
                self, "raw_autopilot_version_received_monotonic_ns"
            ):
                self.raw_autopilot_version_received_monotonic_ns = {
                    uav: [] for uav in range(1, 6)
                }
            raw_response = {
                **common,
                "phase": active_policy.window_id,
                "window_id": active_policy.window_id,
                "response_policy": active_response_policy,
            }
            if message_type == "COMMAND_ACK":
                command = getattr(message, "command", None)
                result = getattr(message, "result", None)
                if (
                    isinstance(command, bool)
                    or command != 512
                    or isinstance(result, bool)
                    or result != 0
                ):
                    self.writer.emit("invalid_window_command_ack", **raw_response)
                    raise ControlProbeError("M4 correlated window ACK is invalid")
                history = self.raw_command_ack_received_monotonic_ns[system_id]
                if len(history) >= MAX_RAW_RESPONSE_HISTORY_PER_UAV:
                    raise ControlProbeError("raw command ACK history exceeded its bound")
                history.append(received_ns)
                self.raw_command_ack_totals[system_id] += 1
                self.writer.emit(
                    "real_window_command_ack",
                    mavlink_command=command,
                    mavlink_result=result,
                    **raw_response,
                )
            else:
                history = self.raw_autopilot_version_received_monotonic_ns[
                    system_id
                ]
                if len(history) >= MAX_RAW_RESPONSE_HISTORY_PER_UAV:
                    raise ControlProbeError(
                        "raw AUTOPILOT_VERSION history exceeded its bound"
                    )
                history.append(received_ns)
                self.raw_autopilot_version_totals[system_id] += 1
                self.writer.emit(
                    "real_window_requested_telemetry", **raw_response
                )
            return
        if pending is None and message_type in {"COMMAND_ACK", "AUTOPILOT_VERSION"}:
            # MAVProxy performs its own ArduPilot startup discovery before the
            # first acceptance window (for example MAV_CMD_GET_HOME_POSITION).
            # Those responses are already preserved byte-for-byte by the
            # enclosing ``control_datagram_receive`` record, but they cannot
            # correlate with an acceptance request because no control command
            # has been consumed yet.  Treat them like other ambient MAVLink
            # traffic only in that strictly bounded pre-window state.  Once a
            # window has ever been consumed, an unbound response remains a
            # fail-closed condition, including between later windows.
            if self.active_phase is None and not self.processed_commands:
                return
            if (
                getattr(getattr(self, "args", None), "profile", None)
                == "m4_causality"
                and system_id not in self.guarded_uavs
            ):
                self.writer.emit("late_unbound_window_response", **common)
                return
            event = (
                "late_stopped_control_response"
                if system_id in self.guarded_uavs
                else "uncorrelated_control_response"
            )
            self.writer.emit(event, **common)
            raise ControlProbeError(f"{event} from uav{system_id}")
        if pending is None:
            return
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
        self._update_capacity_flight_boundaries(time.monotonic_ns())
        readable, _writable, _errors = select.select([self.sock], [], [], timeout_s)
        if not readable:
            self._update_capacity_flight_boundaries(time.monotonic_ns())
            return
        while True:
            try:
                payload, ancillary, flags, peer_raw = self.sock.recvmsg(65535, 128)
            except BlockingIOError:
                return
            peer = (str(peer_raw[0]), int(peer_raw[1]))
            received_ns = time.monotonic_ns()
            rx_tos = require_control_rx_tos(ancillary, flags)
            datagram_hash = sha256_bytes(payload)
            try:
                parser = self.mavlink_dialect.MAVLink(None)
                parser.robust_parsing = False
                parsed = parser.parse_buffer(payload)
                messages = [] if parsed is None else list(parsed)
                if (
                    not messages
                    or any(str(message.get_type()) == "BAD_DATA" for message in messages)
                    or b"".join(bytes(message.get_msgbuf()) for message in messages)
                    != payload
                ):
                    raise ControlProbeError(
                        "MAVLink parser did not consume the exact UDP datagram"
                    )
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
            self.writer.emit(
                "control_datagram_receive",
                peer_ip=peer[0],
                peer_udp_port=peer[1],
                received_monotonic_ns=received_ns,
                rx_tos=rx_tos,
                transport_payload_hex=payload.hex(),
                transport_payload_sha256=datagram_hash,
                transport_payload_size=len(payload),
                decoded_message_count=len(messages),
            )
            for message in messages:
                self._handle_message(
                    message,
                    peer=peer,
                    received_ns=received_ns,
                    datagram_sha256=datagram_hash,
                )
            self._update_capacity_flight_boundaries(time.monotonic_ns())

    def _update_capacity_flight_boundaries(self, now_ns: int) -> None:
        airborne = getattr(self, "airborne_controller", None)
        if airborne is None or not airborne.started:
            return
        start_ns = int(airborne.gate["measurement_start_monotonic_ns"])
        end_ns = int(airborne.gate["measurement_end_monotonic_ns"])
        if not airborne.measurement_started and now_ns >= start_ns:
            airborne.mark_measurement_started()
        if airborne.measurement_started and not airborne.measurement_ended and now_ns >= end_ns:
            airborne.mark_measurement_ended()

    def pump_until(self, deadline_ns: int) -> None:
        while (remaining_ns := deadline_ns - time.monotonic_ns()) > 0:
            self.pump(min(0.05, remaining_ns / 1_000_000_000))

    def _expire_pending(self) -> None:
        now_ns = time.monotonic_ns()
        for uav in [
            value
            for value, pending in self.pending.items()
            if now_ns - pending.sent_monotonic_ns >= OUTCOME_TIMEOUT_NS
        ]:
            pending = self.pending[uav]
            self._complete_pending(uav, timed_out=True)
            if pending.response_policy != "timeout_required":
                self.quarantined_uavs.add(uav)
        self._ensure_correlated_state()
        for key in [
            value
            for value, pending in self.correlated_pending.items()
            if now_ns - pending.sent_monotonic_ns >= OUTCOME_TIMEOUT_NS
        ]:
            self._complete_correlated(
                key, timed_out=True, completed_ns=now_ns
            )

    def send_request(self, policy: WindowPolicy, uav: int, sequence: int) -> None:
        response_policy = policy.response_policy_for(uav)
        correlated_profile = (
            getattr(self.args, "profile", None) == "m4_causality"
        )
        self._ensure_correlated_state()
        if uav in self.quarantined_uavs:
            raise ControlProbeError(f"overlap/quarantine for uav{uav}")
        if correlated_profile:
            if response_policy == CORRELATED_TIMESYNC_POLICY:
                if uav in self.pending:
                    raise ControlProbeError(f"serial/correlated overlap for uav{uav}")
                live_for_uav = sum(
                    key[0] == uav for key in self.correlated_pending
                )
                if live_for_uav >= policy.correlated_pending_bound():
                    raise ControlProbeError(
                        f"correlated TIMESYNC pending bound exceeded for uav{uav}"
                    )
            elif response_policy == "timeout_required":
                if uav in self.pending or any(
                    key[0] == uav for key in self.correlated_pending
                ):
                    raise ControlProbeError(f"overlap/quarantine for uav{uav}")
            else:
                raise ControlProbeError("M4 causal response policy is unsupported")
        elif uav in self.pending or any(
            key[0] == uav for key in self.correlated_pending
        ):
            raise ControlProbeError(f"overlap/quarantine for uav{uav}")
        correlated_key: tuple[int, int] | None = None
        if correlated_profile and response_policy == CORRELATED_TIMESYNC_POLICY:
            correlated_key = (
                uav,
                timesync_token(
                    run_nonce=self.args.run_nonce,
                    phase_code=policy.transport_phase_code,
                    uav=uav,
                    ordinal=sequence,
                ),
            )
            if (
                correlated_key in self.correlated_pending
                or correlated_key in self.retired_timesync_tokens
            ):
                raise ControlProbeError("TIMESYNC correlation token was reused")
            if (
                len(self.correlated_pending) + len(self.retired_timesync_tokens)
                >= MAX_RETIRED_TIMESYNC_TOKENS
            ):
                raise ControlProbeError("TIMESYNC correlation run bound exceeded")
        sent_ns = time.monotonic_ns()
        scheduled_ns = policy.slot_monotonic_ns(sequence)
        encoder = (
            encode_m4_correlated_control_request
            if correlated_profile
            else encode_actual_control_request
        )
        request = encoder(
            run_nonce=self.args.run_nonce,
            transport_nonce=self.args.transport_nonce32,
            phase_code=policy.transport_phase_code,
            uav=uav,
            sequence=sequence,
            mavlink=self.sequencer,
        )
        destination = (f"10.71.{uav}.10", 14600 + uav)
        if correlated_profile:
            datagram_size = self.sock.sendto(
                request["request_datagram"], destination
            )
            if datagram_size != len(request["request_datagram"]):
                raise ControlProbeError("short M4 causal control datagram send")
            send_evidence = {
                "timesync_request_tc1": request["timesync_request_tc1"],
                "timesync_request_ts1": request["timesync_request_ts1"],
                "timesync_frame_hex": request["timesync_frame"].hex(),
                "timesync_frame_sha256": request["timesync_frame_sha256"],
                "request_transport_payload_hex": request[
                    "request_datagram"
                ].hex(),
                "request_transport_payload_sha256": request[
                    "request_datagram_sha256"
                ],
                "request_transport_payload_size": len(
                    request["request_datagram"]
                ),
                "request_transport_send_return_size": datagram_size,
                "correlation_kind": "mavlink_timesync_echo_v1",
                "response_policy": response_policy,
            }
        else:
            marker_size = self.sock.sendto(request["marker_frame"], destination)
            command_size = self.sock.sendto(request["command_frame"], destination)
            send_evidence = {
                "marker_send_return_size": marker_size,
                "command_send_return_size": command_size,
            }
        pending = PendingRequest(
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
            response_policy=response_policy,
            timesync_token=request.get("timesync_request_ts1"),
            timesync_frame_sha256=request.get("timesync_frame_sha256"),
            request_datagram_sha256=request.get("request_datagram_sha256"),
            request_datagram_size=(
                len(request["request_datagram"])
                if "request_datagram" in request
                else None
            ),
        )
        if response_policy == CORRELATED_TIMESYNC_POLICY:
            if (
                correlated_key is None
                or correlated_key[1] != request["timesync_request_ts1"]
            ):
                raise ControlProbeError("TIMESYNC encoder correlation token differs")
            self.correlated_pending[correlated_key] = pending
        else:
            self.pending[uav] = pending
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
            command_frame_hex=request["command_frame"].hex(),
            command_frame_sha256=request["command_frame_sha256"],
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
            **send_evidence,
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
        allowed_response_policies = (
            {CORRELATED_TIMESYNC_POLICY, "timeout_required"}
            if self.args.profile == "m4_causality"
            else {"ack_required", "timeout_required"}
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
                value not in allowed_response_policies
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
        airborne = getattr(self, "airborne_controller", None)
        if airborne is not None and not airborne.started:
            readiness_deadline = int(
                airborne.gate["airborne_ready_deadline_monotonic_ns"]
            )
            while not self.link_ready_written and time.monotonic_ns() < readiness_deadline:
                self.pump(0.05)
            if not self.link_ready_written:
                raise ControlProbeError(
                    "M4 capacity flight lacks five-UAV actual-control readiness"
                )
            airborne.prepare()
        if airborne is not None and not airborne.airborne_ready_confirmed:
            self.pump_until(int(airborne.gate["warmup_start_monotonic_ns"]))
            airborne.confirm_airborne_ready_boundary()
            airborne.start_warmup_motion()
        self.pump_until(policy.start_monotonic_ns)
        self.active_phase = policy.window_id
        self.active_policy = policy
        if self.quarantined_uavs:
            raise ControlProbeError("actual-control window inherited an ACK timeout")
        if self.correlated_pending:
            raise ControlProbeError("actual-control window inherited TIMESYNC pending state")
        self.consume_stopped_drain_guard(policy)
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
                if (
                    policy.response_policy_for(uav)
                    != CORRELATED_TIMESYNC_POLICY
                    and uav in self.pending
                ):
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
                and not self.correlated_pending
            ):
                self.pump_until(policy.end_monotonic_ns)
                break
        self._expire_pending()
        if self.pending or self.correlated_pending:
            for uav, pending in self.pending.items():
                self.writer.emit(
                    "phase_ended_before_outcome_timeout",
                    phase=policy.window_id,
                    window_id=policy.window_id,
                    uav=uav,
                    sequence=pending.sequence,
                    sent_monotonic_ns=pending.sent_monotonic_ns,
                )
            for (uav, _token), pending in self.correlated_pending.items():
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
        heartbeat_counts = heartbeat_counts_for_window(
            self.heartbeat_received_monotonic_ns,
            policy.start_monotonic_ns,
            policy.end_monotonic_ns,
        )
        raw_ack_counts = raw_response_counts_for_window(
            self.raw_command_ack_received_monotonic_ns,
            policy.start_monotonic_ns,
            policy.end_monotonic_ns,
        )
        raw_telemetry_counts = raw_response_counts_for_window(
            self.raw_autopilot_version_received_monotonic_ns,
            policy.start_monotonic_ns,
            policy.end_monotonic_ns,
        )
        m4_liveness_evidence: dict[str, Any] = {}
        if self.args.profile == "m4_causality":
            validate_m4_window_liveness(
                policy,
                heartbeat_counts,
                raw_ack_counts,
                raw_telemetry_counts,
            )
            m4_liveness_evidence = {
                "raw_command_ack_counts": raw_ack_counts,
                "raw_autopilot_version_counts": raw_telemetry_counts,
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
            **m4_liveness_evidence,
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
                **m4_liveness_evidence,
            },
        )
        if airborne is not None:
            self._update_capacity_flight_boundaries(time.monotonic_ns())
            if not airborne.measurement_ended:
                raise ControlProbeError(
                    "M4 capacity workload ended before airborne measurement boundary"
                )
            airborne.land_and_disarm()
        self.active_phase = None
        self.active_policy = None

    def run(self) -> None:
        start_ticks = process_start_ticks(os.getpid())
        transmit_tos = self.sock.getsockopt(socket.IPPROTO_IP, socket.IP_TOS)
        receive_tos_enabled = self.sock.getsockopt(
            socket.IPPROTO_IP, getattr(socket, "IP_RECVTOS", 13)
        )
        if transmit_tos != TOS_CONTROL or receive_tos_enabled != 1:
            raise ControlProbeError(
                "actual-control socket TOS configuration differs after bind"
            )
        self.writer.emit(
            "actual_control_socket_ready",
            pid=os.getpid(),
            process_start_ticks=start_ticks,
            namespace="ams-gcs",
            bound_socket=["10.71.0.10", 14600],
            full_run_nonce=self.args.run_nonce,
            transmit_ip_tos=transmit_tos,
            receive_ip_tos_enabled=receive_tos_enabled,
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
                "transmit_ip_tos": transmit_tos,
                "receive_ip_tos_enabled": receive_tos_enabled,
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
    except (RuntimeError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"FAIL actual SITL control probe: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
