#!/usr/bin/env python3
"""Exercise one M2 MAVLink phase through the isolated TapBridge path.

The probe is a runtime evidence producer, not an acceptance validator.  It
records every command attempt and observed response in append-only JSONL.  An
independent validator must derive the M2 result from these events and PCAP.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import select
import socket
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


PHASES = ("good", "down", "recovery")
MAVLINK_CONTROL_TOS = 184


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_endpoint(value: str) -> tuple[str, int]:
    host, separator, port_text = value.rpartition(":")
    if not separator or not host:
        raise argparse.ArgumentTypeError(f"invalid HOST:PORT endpoint: {value!r}")
    try:
        port = int(port_text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid endpoint port: {value!r}") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"endpoint port is outside 1..65535: {value!r}")
    return host, port


def attempt_nonce(run_nonce: str, phase: str, attempt: int) -> str:
    return f"{run_nonce}:{phase}:{attempt}"


def make_marker(run_nonce: str, phase: str, attempt: int) -> str:
    marker = f"AMS-M2:{run_nonce}:{phase}:{attempt}"
    encoded = marker.encode("ascii", errors="strict")
    if len(encoded) > 50:
        raise ValueError("MAVLink STATUSTEXT marker exceeds 50 bytes")
    return marker


def mavlink_frame_sequence(payload: bytes) -> int:
    if len(payload) < 5:
        raise ValueError("MAVLink frame is too short")
    if payload[0] == 0xFE:  # MAVLink 1
        return int(payload[2])
    if payload[0] == 0xFD:  # MAVLink 2
        return int(payload[4])
    raise ValueError(f"unsupported MAVLink magic byte: 0x{payload[0]:02x}")


def percentile(values: Iterable[float], fraction: float) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    index = max(0, math.ceil(fraction * len(ordered)) - 1)
    return ordered[index]


def latency_stats(values: Iterable[float]) -> dict[str, float | int | None]:
    samples = [float(value) for value in values]
    return {
        "count": len(samples),
        "p50_ms": None if not samples else round(statistics.median(samples), 6),
        "p95_ms": None if not samples else round(float(percentile(samples, 0.95)), 6),
        "maximum_ms": None if not samples else round(max(samples), 6),
    }


def read_start_ticks(pid: int) -> int | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
        return None
    closing = stat.rfind(")")
    if closing < 0:
        return None
    fields_after_command = stat[closing + 2 :].split()
    try:
        # Field 3 is index zero here; process starttime is proc field 22.
        return int(fields_after_command[19])
    except (IndexError, ValueError):
        return None


def read_cmdline_sha256(pid: int) -> str | None:
    try:
        payload = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
        return None
    if not payload:
        return None
    return hashlib.sha256(payload).hexdigest()


def same_process(pid: int, expected_start_ticks: int) -> bool:
    return read_start_ticks(pid) == expected_start_ticks


@dataclass(frozen=True)
class ProcessReference:
    pid: int
    start_ticks: int
    cmdline_sha256: str | None = None


def parse_process_reference(value: str) -> ProcessReference:
    parts = value.split(":")
    if len(parts) not in (2, 3):
        raise argparse.ArgumentTypeError("process reference must be PID:START_TICKS[:CMDLINE_SHA256]")
    try:
        reference = ProcessReference(int(parts[0]), int(parts[1]), parts[2] if len(parts) == 3 else None)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("process reference must contain integers") from exc
    if reference.pid <= 1 or reference.start_ticks <= 0:
        raise argparse.ArgumentTypeError("process reference values must be positive")
    if reference.cmdline_sha256 is not None and (
        len(reference.cmdline_sha256) != 64
        or any(character not in "0123456789abcdef" for character in reference.cmdline_sha256)
    ):
        raise argparse.ArgumentTypeError("process cmdline SHA-256 is invalid")
    return reference


class JsonlWriter:
    """Append records while preserving a single run/runtime/nonce identity."""

    def __init__(
        self,
        path: Path,
        *,
        run_id: str,
        runtime_id: str,
        run_nonce: str,
        phase: str,
    ) -> None:
        self.path = path
        self.run_id = run_id
        self.runtime_id = runtime_id
        self.run_nonce = run_nonce
        self.phase = phase
        path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = path.open("a+", encoding="utf-8")
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
        try:
            self._handle.seek(0)
            last_sequence = 0
            for line_number, line in enumerate(self._handle, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"invalid existing JSONL at {path}:{line_number}: {exc}") from exc
                expected = (run_id, runtime_id, run_nonce)
                observed = (
                    record.get("run_id"),
                    record.get("runtime_id"),
                    record.get("run_nonce"),
                )
                if observed != expected:
                    raise ValueError(f"mixed run identity in existing JSONL {path}")
                sequence = record.get("event_seq")
                if not isinstance(sequence, int) or sequence <= last_sequence:
                    raise ValueError(f"non-monotonic event_seq in existing JSONL {path}")
                last_sequence = sequence
        except Exception:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            raise
        self._sequence = last_sequence
        self._handle.seek(0, os.SEEK_END)

    def emit(self, event: str, **fields: Any) -> None:
        self._sequence += 1
        record = {
            "schema_version": 2,
            "run_id": self.run_id,
            "runtime_id": self.runtime_id,
            "run_nonce": self.run_nonce,
            "phase": self.phase,
            "event_seq": self._sequence,
            "event": event,
            "wall_utc": utc_now(),
            "monotonic_ns": time.monotonic_ns(),
            **fields,
        }
        self._handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
        self._handle.flush()
        os.fsync(self._handle.fileno())

    def close(self) -> None:
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()

    def __enter__(self) -> "JsonlWriter":
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.close()


def load_process_identity(path: Path, run_id: str, runtime_id: str, run_nonce: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read process identity {path}: {exc}") from exc
    if data.get("run_id") != run_id or data.get("runtime_id") != runtime_id or data.get("run_nonce") != run_nonce:
        raise ValueError("process identity belongs to another run/runtime/nonce")
    processes = data.get("processes")
    if not isinstance(processes, list) or not processes:
        raise ValueError("process identity has no processes")
    required_roles = {"launch", "sitl", "mavproxy", "adapter"}
    roles = {str(item.get("role")) for item in processes if isinstance(item, dict)}
    if roles != required_roles:
        raise ValueError(f"process identity roles are {sorted(roles)}, expected {sorted(required_roles)}")
    return processes


def process_snapshot(
    processes: list[dict[str, Any]],
    *,
    ns3_process: ProcessReference | None,
    absent_processes: list[ProcessReference],
) -> tuple[bool, list[dict[str, Any]], dict[str, Any] | None, list[dict[str, Any]]]:
    endpoint_records: list[dict[str, Any]] = []
    all_live = True
    for item in processes:
        pid = int(item["pid"])
        expected_ticks = int(item["start_ticks"])
        actual_ticks = read_start_ticks(pid)
        actual_cmdline_hash = read_cmdline_sha256(pid)
        live = actual_ticks == expected_ticks and actual_cmdline_hash == item.get("cmdline_sha256")
        all_live = all_live and live
        endpoint_records.append(
            {
                "role": item["role"],
                "pid": pid,
                "expected_start_ticks": expected_ticks,
                "actual_start_ticks": actual_ticks,
                "expected_cmdline_sha256": item.get("cmdline_sha256"),
                "actual_cmdline_sha256": actual_cmdline_hash,
                "alive": live,
            }
        )

    ns3_record: dict[str, Any] | None = None
    if ns3_process is not None:
        actual_ticks = read_start_ticks(ns3_process.pid)
        actual_hash = read_cmdline_sha256(ns3_process.pid)
        ns3_live = actual_ticks == ns3_process.start_ticks and (
            ns3_process.cmdline_sha256 is None
            or actual_hash == ns3_process.cmdline_sha256
        )
        all_live = all_live and ns3_live
        ns3_record = {
            "pid": ns3_process.pid,
            "expected_start_ticks": ns3_process.start_ticks,
            "actual_start_ticks": actual_ticks,
            "alive": ns3_live,
            "cmdline_sha256": actual_hash,
        }

    absent_records: list[dict[str, Any]] = []
    for reference in absent_processes:
        actual_ticks = read_start_ticks(reference.pid)
        same_identity_alive = actual_ticks == reference.start_ticks
        all_live = all_live and not same_identity_alive
        absent_records.append(
            {
                "pid": reference.pid,
                "expected_start_ticks": reference.start_ticks,
                "actual_start_ticks": actual_ticks,
                "same_identity_alive": same_identity_alive,
            }
        )
    return all_live, endpoint_records, ns3_record, absent_records


def emit_phase_process_records(
    process_writer: JsonlWriter,
    probe_writer: JsonlWriter,
    *,
    before: tuple[bool, list[dict[str, Any]], dict[str, Any] | None, list[dict[str, Any]]],
    after: tuple[bool, list[dict[str, Any]], dict[str, Any] | None, list[dict[str, Any]]],
    expected_ns3_state: str,
    ns3_process: ProcessReference | None,
    absent_processes: list[ProcessReference],
) -> bool:
    before_ok, before_endpoints, before_ns3, before_absent = before
    after_ok, after_endpoints, after_ns3, after_absent = after
    before_by_role = {record["role"]: record for record in before_endpoints}
    after_by_role = {record["role"]: record for record in after_endpoints}
    role_map = {"adapter": "uav_adapter", "mavproxy": "mavproxy", "sitl": "sitl"}
    live_roles: list[str] = []
    for identity_role, evidence_role in role_map.items():
        first = before_by_role[identity_role]
        last = after_by_role[identity_role]
        alive = first["alive"] is True and last["alive"] is True
        if alive:
            live_roles.append(evidence_role)
        process_writer.emit(
            "process_snapshot",
            role=evidence_role,
            alive=alive,
            pid=last["pid"],
            start_ticks=last["expected_start_ticks"],
            cmdline_sha256=last["expected_cmdline_sha256"],
        )

    probe_pid = os.getpid()
    probe_ticks = read_start_ticks(probe_pid)
    probe_hash = read_cmdline_sha256(probe_pid)
    probe_alive = probe_ticks is not None and probe_hash is not None
    if probe_alive:
        live_roles.append("gcs_probe")
    process_writer.emit(
        "process_snapshot",
        role="gcs_probe",
        alive=probe_alive,
        pid=probe_pid,
        start_ticks=probe_ticks,
        cmdline_sha256=probe_hash,
    )

    if expected_ns3_state == "up":
        ns3_alive = bool(
            before_ns3
            and after_ns3
            and before_ns3.get("alive") is True
            and after_ns3.get("alive") is True
        )
        ns3_pid = ns3_process.pid if ns3_process else None
        ns3_ticks = ns3_process.start_ticks if ns3_process else None
        ns3_hash = (
            ns3_process.cmdline_sha256
            if ns3_process and ns3_process.cmdline_sha256
            else (after_ns3 or {}).get("cmdline_sha256")
        )
    else:
        same_alive = any(record["same_identity_alive"] for record in before_absent + after_absent)
        ns3_alive = False
        reference = absent_processes[0] if absent_processes else None
        ns3_pid = reference.pid if reference else None
        ns3_ticks = reference.start_ticks if reference else None
        ns3_hash = reference.cmdline_sha256 if reference else None
        before_ok = before_ok and not same_alive
        after_ok = after_ok and not same_alive
    process_writer.emit(
        "process_snapshot",
        role="ns3",
        alive=ns3_alive,
        pid=ns3_pid,
        start_ticks=ns3_ticks,
        cmdline_sha256=ns3_hash,
    )

    all_live = before_ok and after_ok and probe_alive
    probe_writer.emit(
        "endpoint_health",
        expected_ns3_state=expected_ns3_state,
        all_live=all_live,
        endpoint_roles=sorted(live_roles),
        ns3_alive=ns3_alive,
    )
    return all_live


def tcp_reachable(endpoint: tuple[str, int], timeout_s: float) -> tuple[bool, str | None]:
    try:
        with socket.create_connection(endpoint, timeout=timeout_s):
            return True, None
    except OSError as exc:
        return False, str(exc)


class DatagramWriter:
    def __init__(self, sock: socket.socket, destination: tuple[str, int]):
        self.sock = sock
        self.destination = destination
        self.last_payload: bytes | None = None

    def write(self, payload: bytes) -> int:
        self.last_payload = bytes(payload)
        return self.sock.sendto(payload, self.destination)


@dataclass
class PhaseResult:
    attempts: int
    acknowledgements: int
    telemetry_responses: int
    heartbeat_count: int
    heartbeat_timeout: bool
    ack_latencies_ms: list[float]
    telemetry_latencies_ms: list[float]


def relevant_messages(parser: Any, payload: bytes) -> list[Any]:
    messages = parser.parse_buffer(payload)
    return [] if messages is None else list(messages)


def receive_messages(
    sock: socket.socket,
    parser: Any,
    *,
    deadline: float,
) -> Iterable[tuple[Any, tuple[str, int], int, str]]:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        readable, _writable, _errors = select.select([sock], [], [], remaining)
        if not readable:
            return
        payload, peer = sock.recvfrom(65535)
        received_ns = time.monotonic_ns()
        packet_sha256 = hashlib.sha256(payload).hexdigest()
        for message in relevant_messages(parser, payload):
            if message.get_type() != "BAD_DATA":
                yield message, peer, received_ns, packet_sha256


def record_message(
    writer: JsonlWriter,
    message: Any,
    *,
    peer: tuple[str, int],
    attempt: int | None,
    nonce: str | None,
    request_sha256: str | None,
    request_mavlink_seq: int | None,
    packet_sha256: str,
) -> str:
    message_type = str(message.get_type())
    base = {
        "message_type": message_type,
        "source_system": int(message.get_srcSystem()),
        "source_component": int(message.get_srcComponent()),
        "peer": [peer[0], peer[1]],
        "attempt": attempt,
        "nonce": nonce,
        "packet_sha256": packet_sha256,
    }
    if message_type == "HEARTBEAT":
        writer.emit("heartbeat", **base)
    elif (
        message_type == "COMMAND_ACK"
        and request_sha256 is not None
        and int(message.command) == 512  # MAV_CMD_REQUEST_MESSAGE
    ):
        writer.emit(
            "command_ack",
            **base,
            request_sha256=request_sha256,
            request_mavlink_seq=request_mavlink_seq,
            mavlink_command=int(message.command),
            mavlink_result=int(message.result),
        )
    elif message_type == "AUTOPILOT_VERSION" and request_sha256 is not None:
        writer.emit(
            "telemetry",
            **base,
            request_sha256=request_sha256,
            request_mavlink_seq=request_mavlink_seq,
            message_id=int(message.get_msgId()),
            telemetry_kind="AUTOPILOT_VERSION",
        )
    return message_type


def wait_for_heartbeat(
    sock: socket.socket,
    parser: Any,
    writer: JsonlWriter,
    *,
    target_system: int,
    timeout_s: float,
) -> int:
    heartbeats = 0
    for message, peer, _received_ns, packet_sha256 in receive_messages(
        sock,
        parser,
        deadline=time.monotonic() + timeout_s,
    ):
        message_type = record_message(
            writer,
            message,
            peer=peer,
            attempt=None,
            nonce=None,
            request_sha256=None,
            request_mavlink_seq=None,
            packet_sha256=packet_sha256,
        )
        if message_type == "HEARTBEAT" and int(message.get_srcSystem()) == target_system:
            heartbeats += 1
            break
    return heartbeats


def emit_phase_start(writer: JsonlWriter, args: argparse.Namespace) -> None:
    """Open the evidence window before the GCS socket can queue a packet.

    The adapter and probe use the same monotonic clock.  If UDP is bound
    first, a just-recovered link can forward a heartbeat into the socket
    queue before ``phase_start`` is recorded; the probe would then observe a
    response that the independent validator must correctly reject as outside
    its adapter window.  Starting the window before binding makes the
    producer's causal boundary unambiguous without relaxing validation.
    """

    writer.emit(
        "phase_start",
        attempts=args.attempts,
        expected_ack=args.expected_ack,
        gcs_bind=list(args.gcs_bind),
        uav_endpoint=list(args.uav_endpoint),
        target_system=args.target_system,
    )


def execute_phase(args: argparse.Namespace, writer: JsonlWriter) -> PhaseResult:
    os.environ.setdefault("MAVLINK20", "1")
    try:
        from pymavlink import mavutil
    except ImportError as exc:  # pragma: no cover - runtime dependency gate.
        raise RuntimeError(f"pymavlink is required: {exc}") from exc

    # This must remain before bind(): see emit_phase_start().
    emit_phase_start(writer, args)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, MAVLINK_CONTROL_TOS)
    if sock.getsockopt(socket.IPPROTO_IP, socket.IP_TOS) != MAVLINK_CONTROL_TOS:
        raise RuntimeError("M2 GCS socket did not retain the control DSCP/TOS identity")
    sock.bind(args.gcs_bind)
    sock.setblocking(False)
    destination = args.uav_endpoint
    datagram_writer = DatagramWriter(sock, destination)
    outbound = mavutil.mavlink.MAVLink(
        datagram_writer,
        srcSystem=args.source_system,
        srcComponent=args.source_component,
    )
    inbound = mavutil.mavlink.MAVLink(None)
    inbound.robust_parsing = True

    acknowledgements = 0
    telemetry_responses = 0
    heartbeat_count = 0
    ack_latencies_ms: list[float] = []
    telemetry_latencies_ms: list[float] = []
    heartbeat_timeout = False

    try:
        if args.expected_ack:
            heartbeat_count += wait_for_heartbeat(
                sock,
                inbound,
                writer,
                target_system=args.target_system,
                timeout_s=args.heartbeat_timeout_s,
            )
            if heartbeat_count == 0:
                heartbeat_timeout = True
                writer.emit("heartbeat_timeout", timed_out=True, timeout_s=args.heartbeat_timeout_s)

        for attempt in range(1, args.attempts + 1):
            nonce = attempt_nonce(args.run_nonce, args.phase, attempt)
            marker = make_marker(args.run_nonce, args.phase, attempt)
            sent_ns = time.monotonic_ns()
            outbound.statustext_send(mavutil.mavlink.MAV_SEVERITY_INFO, marker.encode("ascii"))
            marker_payload = datagram_writer.last_payload
            if marker_payload is None:
                raise RuntimeError("MAVLink marker frame was not emitted")
            outbound.command_long_send(
                args.target_system,
                args.target_component,
                mavutil.mavlink.MAV_CMD_REQUEST_MESSAGE,
                0,
                float(mavutil.mavlink.MAVLINK_MSG_ID_AUTOPILOT_VERSION),
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
            )
            request_payload = datagram_writer.last_payload
            if request_payload is None:
                raise RuntimeError("MAVLink command frame was not emitted")
            marker_sha256 = hashlib.sha256(marker_payload).hexdigest()
            request_sha256 = hashlib.sha256(request_payload).hexdigest()
            request_mavlink_seq = mavlink_frame_sequence(request_payload)
            writer.emit(
                "command_attempt",
                attempt=attempt,
                nonce=nonce,
                packet_sha256=request_sha256,
                marker_sha256=marker_sha256,
                command_sha256=request_sha256,
                marker_text=marker,
                marker_mavlink_seq=mavlink_frame_sequence(marker_payload),
                mavlink_seq=request_mavlink_seq,
                expected_ack=args.expected_ack,
                mavlink_command=int(mavutil.mavlink.MAV_CMD_REQUEST_MESSAGE),
                target_system=args.target_system,
                target_component=args.target_component,
            )

            ack = False
            telemetry = False
            ack_latency_ms: float | None = None
            telemetry_latency_ms: float | None = None
            deadline = time.monotonic() + args.ack_timeout_s
            for message, peer, received_ns, packet_sha256 in receive_messages(
                sock,
                inbound,
                deadline=deadline,
            ):
                message_type = record_message(
                    writer,
                    message,
                    peer=peer,
                    attempt=attempt,
                    nonce=nonce,
                    request_sha256=request_sha256,
                    request_mavlink_seq=request_mavlink_seq,
                    packet_sha256=packet_sha256,
                )
                if int(message.get_srcSystem()) != args.target_system:
                    continue
                if message_type == "HEARTBEAT":
                    heartbeat_count += 1
                elif (
                    message_type == "COMMAND_ACK"
                    and int(message.command) == int(mavutil.mavlink.MAV_CMD_REQUEST_MESSAGE)
                    and int(message.result) == int(mavutil.mavlink.MAV_RESULT_ACCEPTED)
                ):
                    if not ack:
                        ack = True
                        ack_latency_ms = (received_ns - sent_ns) / 1_000_000.0
                elif message_type == "AUTOPILOT_VERSION" and not telemetry:
                    telemetry = True
                    telemetry_latency_ms = (received_ns - sent_ns) / 1_000_000.0
                if ack and telemetry:
                    break

            if ack:
                acknowledgements += 1
                ack_latencies_ms.append(float(ack_latency_ms))
            if telemetry:
                telemetry_responses += 1
                telemetry_latencies_ms.append(float(telemetry_latency_ms))
            writer.emit(
                "command_result",
                attempt=attempt,
                nonce=nonce,
                request_sha256=request_sha256,
                request_mavlink_seq=request_mavlink_seq,
                expected_ack=args.expected_ack,
                ack=ack,
                telemetry=telemetry,
                ack_latency_ms=None if ack_latency_ms is None else round(ack_latency_ms, 6),
                telemetry_latency_ms=(
                    None if telemetry_latency_ms is None else round(telemetry_latency_ms, 6)
                ),
            )

        if not args.expected_ack:
            observed = wait_for_heartbeat(
                sock,
                inbound,
                writer,
                target_system=args.target_system,
                timeout_s=args.heartbeat_timeout_s,
            )
            heartbeat_count += observed
            heartbeat_timeout = heartbeat_count == 0
            writer.emit(
                "heartbeat_timeout",
                timed_out=heartbeat_timeout,
                timeout_s=args.heartbeat_timeout_s,
            )
    finally:
        sock.close()

    writer.emit(
        "phase_end",
        attempts=args.attempts,
        acknowledgements=acknowledgements,
        telemetry_responses=telemetry_responses,
        heartbeat_count=heartbeat_count,
        heartbeat_timeout=heartbeat_timeout,
        ack_latency=latency_stats(ack_latencies_ms),
        telemetry_latency=latency_stats(telemetry_latencies_ms),
    )
    return PhaseResult(
        attempts=args.attempts,
        acknowledgements=acknowledgements,
        telemetry_responses=telemetry_responses,
        heartbeat_count=heartbeat_count,
        heartbeat_timeout=heartbeat_timeout,
        ack_latencies_ms=ack_latencies_ms,
        telemetry_latencies_ms=telemetry_latencies_ms,
    )


def write_phase_summary(path: Path, args: argparse.Namespace, result: PhaseResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "schema_version": 2,
        "contract": "ams.m2.vertical_slice.phase/v1",
        "run_id": args.run_id,
        "runtime_id": args.runtime_id,
        "run_nonce": args.run_nonce,
        "phase": args.phase,
        "attempts": result.attempts,
        "expected_ack": args.expected_ack,
        "acknowledgements": result.acknowledgements,
        "telemetry_responses": result.telemetry_responses,
        "heartbeat_count": result.heartbeat_count,
        "heartbeat_timeout": result.heartbeat_timeout,
        "loss_rate": (result.attempts - result.acknowledgements) / result.attempts,
        "ack_latency": latency_stats(result.ack_latencies_ms),
        "telemetry_latency": latency_stats(result.telemetry_latencies_ms),
    }
    with path.open("x", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")


def criteria_met(args: argparse.Namespace, result: PhaseResult, health_ok: bool) -> bool:
    if not health_ok:
        return False
    if args.expected_ack:
        return (
            result.acknowledgements == args.attempts
            and result.telemetry_responses == args.attempts
            and result.heartbeat_count > 0
            and not result.heartbeat_timeout
        )
    return (
        result.acknowledgements == 0
        and result.telemetry_responses == 0
        and result.heartbeat_count == 0
        and result.heartbeat_timeout
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", required=True, choices=PHASES)
    parser.add_argument("--attempts", required=True, type=int)
    parser.add_argument("--expected-ack", required=True, choices=("true", "false"))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--runtime-id", required=True)
    parser.add_argument("--run-nonce", required=True)
    parser.add_argument("--event-log", type=Path, required=True)
    parser.add_argument("--process-event-log", type=Path, required=True)
    parser.add_argument("--process-identity", type=Path, required=True)
    parser.add_argument("--phase-summary", type=Path, required=True)
    parser.add_argument("--gcs-bind", type=parse_endpoint, default=parse_endpoint("10.71.0.10:14600"))
    parser.add_argument(
        "--uav-endpoint", type=parse_endpoint, default=parse_endpoint("10.71.1.10:14601")
    )
    parser.add_argument("--target-system", type=int, default=1)
    parser.add_argument("--target-component", type=int, default=1)
    parser.add_argument("--source-system", type=int, default=255)
    parser.add_argument("--source-component", type=int, default=190)
    parser.add_argument("--heartbeat-timeout-s", type=float, default=5.0)
    parser.add_argument("--ack-timeout-s", type=float, default=3.0)
    parser.add_argument("--forbidden-endpoint", action="append", type=parse_endpoint, default=[])
    parser.add_argument("--forbidden-timeout-s", type=float, default=0.5)
    parser.add_argument("--expected-ns3-state", required=True, choices=("up", "down"))
    parser.add_argument("--ns3-process", type=parse_process_reference)
    parser.add_argument("--absent-process", action="append", type=parse_process_reference, default=[])
    args = parser.parse_args(argv)
    args.expected_ack = args.expected_ack == "true"
    if args.attempts <= 0:
        parser.error("--attempts must be positive")
    if len(args.run_id) < 1 or len(args.runtime_id) < 8:
        parser.error("run/runtime/nonce identifiers are missing or too short")
    if re.fullmatch(r"[A-Za-z0-9_-]{16,128}", args.run_nonce) is None:
        parser.error("--run-nonce must match [A-Za-z0-9_-]{16,128}")
    if args.expected_ns3_state == "up" and args.ns3_process is None:
        parser.error("--ns3-process is required when ns-3 is expected up")
    if args.expected_ns3_state == "down" and args.ns3_process is not None:
        parser.error("--ns3-process is forbidden when ns-3 is expected down")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        processes = load_process_identity(
            args.process_identity,
            args.run_id,
            args.runtime_id,
            args.run_nonce,
        )
        with JsonlWriter(
            args.event_log,
            run_id=args.run_id,
            runtime_id=args.runtime_id,
            run_nonce=args.run_nonce,
            phase=args.phase,
        ) as probe_writer, JsonlWriter(
            args.process_event_log,
            run_id=args.run_id,
            runtime_id=args.runtime_id,
            run_nonce=args.run_nonce,
            phase=args.phase,
        ) as process_writer:
            process_before = process_snapshot(
                processes,
                ns3_process=args.ns3_process,
                absent_processes=args.absent_process,
            )
            forbidden_clear = True
            for endpoint in args.forbidden_endpoint:
                reachable, error = tcp_reachable(endpoint, args.forbidden_timeout_s)
                forbidden_clear = forbidden_clear and not reachable
                probe_writer.emit(
                    "direct_endpoint_probe",
                    endpoint=[endpoint[0], endpoint[1]],
                    reachable=reachable,
                    error=error,
                )
            result = execute_phase(args, probe_writer)
            process_after = process_snapshot(
                processes,
                ns3_process=args.ns3_process,
                absent_processes=args.absent_process,
            )
            health_ok = emit_phase_process_records(
                process_writer,
                probe_writer,
                before=process_before,
                after=process_after,
                expected_ns3_state=args.expected_ns3_state,
                ns3_process=args.ns3_process,
                absent_processes=args.absent_process,
            )
        write_phase_summary(args.phase_summary, args, result)
    except Exception as exc:
        print(f"FAIL M2 probe {args.phase}: {exc}", file=sys.stderr)
        return 2

    phase_ok = criteria_met(args, result, health_ok and forbidden_clear)
    print(
        f"M2 phase={args.phase} attempts={result.attempts} "
        f"acks={result.acknowledgements} heartbeats={result.heartbeat_count} "
        f"telemetry={result.telemetry_responses} heartbeat_timeout={result.heartbeat_timeout}"
    )
    return 0 if phase_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
