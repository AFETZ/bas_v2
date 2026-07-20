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
import stat
import statistics
import struct
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


PHASES = ("good", "down", "recovery")
REQUIRED_FORBIDDEN_ENDPOINTS = (("127.0.0.1", 5760), ("10.72.1.1", 5760))
MAVLINK_CONTROL_TOS = 184
# A heartbeat is liveness evidence only.  Positive M2 windows nevertheless
# need enough independently observable liveness samples to distinguish a live
# recovered path from one queued packet at a phase boundary.
MIN_POSITIVE_HEARTBEATS = 3
DEFAULT_POSITIVE_HEARTBEAT_OBSERVATION_S = 5.0
PROBE_RAW_EVENT_SCHEMA = "ams.m2.probe-event/v2"
PERSISTENT_ENDPOINT_EVENT_SCHEMA = "ams.m2.persistent-gcs-endpoint/v1"
PERSISTENT_CONTROL_SCHEMA = "ams.m2.persistent-gcs-control/v1"
PERSISTENT_CONTROL_RESPONSE_SCHEMA = "ams.m2.persistent-gcs-control-response/v1"
PERSISTENT_CONTROL_MAX_PACKET_BYTES = 64 * 1024
PERSISTENT_CONTROL_REQUEST_ID = re.compile(r"[A-Za-z0-9_.:-]{8,128}")


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


def make_transaction_id(
    *,
    run_nonce: str,
    phase: str,
    attempt: int,
    marker_sha256: str,
    command_sha256: str,
    mavlink_seq: int,
    source_system: int,
    source_component: int,
    target_system: int,
    target_component: int,
    mavlink_command: int,
) -> str:
    """Return a stable, self-describing ID for one marker/request pair."""

    identity = {
        "attempt": attempt,
        "command_sha256": command_sha256,
        "marker_sha256": marker_sha256,
        "mavlink_command": mavlink_command,
        "mavlink_seq": mavlink_seq,
        "phase": phase,
        "run_nonce": run_nonce,
        "source_component": source_component,
        "source_system": source_system,
        "target_component": target_component,
        "target_system": target_system,
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


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


def process_reference_payload(reference: ProcessReference) -> dict[str, Any]:
    """Encode a process identity for the atomic persistent-control request."""

    if reference.cmdline_sha256 is None:
        raise ValueError("persistent control requires a process cmdline SHA-256")
    return {
        "pid": reference.pid,
        "start_ticks": reference.start_ticks,
        "cmdline_sha256": reference.cmdline_sha256,
    }


def control_process_reference(value: Any, field: str) -> ProcessReference:
    if not isinstance(value, dict) or set(value) != {"pid", "start_ticks", "cmdline_sha256"}:
        raise ValueError(f"{field} must be a process reference object")
    pid = value.get("pid")
    start_ticks = value.get("start_ticks")
    cmdline_sha256 = value.get("cmdline_sha256")
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 1
        or isinstance(start_ticks, bool)
        or not isinstance(start_ticks, int)
        or start_ticks <= 0
        or not isinstance(cmdline_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", cmdline_sha256) is None
    ):
        raise ValueError(f"{field} is invalid")
    return ProcessReference(pid=pid, start_ticks=start_ticks, cmdline_sha256=cmdline_sha256)


def control_endpoints(value: Any, field: str) -> list[tuple[str, int]]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list of endpoints")
    endpoints: list[tuple[str, int]] = []
    for index, endpoint in enumerate(value):
        if (
            not isinstance(endpoint, list)
            or len(endpoint) != 2
            or not isinstance(endpoint[0], str)
            or not endpoint[0]
            or isinstance(endpoint[1], bool)
            or not isinstance(endpoint[1], int)
            or not 1 <= endpoint[1] <= 65535
        ):
            raise ValueError(f"{field}[{index}] is not a valid HOST:PORT endpoint")
        endpoints.append((endpoint[0], endpoint[1]))
    if len(set(endpoints)) != len(endpoints):
        raise ValueError(f"{field} contains duplicate endpoints")
    return endpoints


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


class PhaseScopedJsonlWriter:
    """Keep one physical JSONL writer while assigning an explicit M2 phase."""

    def __init__(self, writer: JsonlWriter, phase: str) -> None:
        if phase not in PHASES:
            raise ValueError(f"invalid phase-scoped JSONL phase: {phase!r}")
        self._writer = writer
        self.phase = phase

    def emit(self, event: str, **fields: Any) -> None:
        supplied_phase = fields.pop("phase", self.phase)
        if supplied_phase != self.phase:
            raise ValueError("phase-scoped JSONL writer cannot emit another phase")
        self._writer.emit(event, phase=self.phase, **fields)


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
    def __init__(
        self,
        sock: socket.socket,
        destination: tuple[str, int],
        *,
        transmit: bool = True,
    ):
        self.sock = sock
        self.destination = destination
        self.transmit = transmit
        self.last_payload: bytes | None = None

    def write(self, payload: bytes) -> int:
        self.last_payload = bytes(payload)
        if not self.transmit:
            return len(payload)
        return self.send(payload)

    def send(self, payload: bytes) -> int:
        return self.sock.sendto(payload, self.destination)


@dataclass
class DatagramSequences:
    """Per-phase raw UDP occurrence sequence numbers."""

    tx_datagram_seq: int = 0
    rx_datagram_seq: int = 0

    def next_tx(self) -> int:
        self.tx_datagram_seq += 1
        return self.tx_datagram_seq

    def next_rx(self) -> int:
        self.rx_datagram_seq += 1
        return self.rx_datagram_seq


@dataclass(frozen=True)
class PersistentEndpointConfig:
    """Immutable transport and MAVLink identity owned by a persistent GCS endpoint."""

    gcs_bind: tuple[str, int]
    uav_endpoint: tuple[str, int]
    target_system: int = 1
    target_component: int = 1
    source_system: int = 255
    source_component: int = 190

    def as_dict(self) -> dict[str, Any]:
        return {
            "gcs_bind": list(self.gcs_bind),
            "uav_endpoint": list(self.uav_endpoint),
            "target_system": self.target_system,
            "target_component": self.target_component,
            "source_system": self.source_system,
            "source_component": self.source_component,
        }

    def fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


def persistent_endpoint_instance_id(
    *,
    run_id: str,
    runtime_id: str,
    run_nonce: str,
    configuration: PersistentEndpointConfig,
) -> str:
    """Derive a stable endpoint identity without relying on a process PID."""

    identity = {
        "configuration": configuration.as_dict(),
        "run_id": run_id,
        "run_nonce": run_nonce,
        "runtime_id": runtime_id,
    }
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class EndpointEventWriter:
    """Add a single-endpoint causal envelope around the existing probe events.

    A normal ``JsonlWriter`` guarantees append-only identity and event order.
    This wrapper additionally makes UDP occurrence numbers global across the
    three M2 windows and rejects duplicated request/leg/result evidence before
    it can be written.  The same event log is therefore meaningful even though
    a single UDP socket survives the phase boundary.
    """

    _WINDOW_EVENTS = frozenset(
        {
            "phase_start",
            "phase_end",
            "command_attempt",
            "command_result",
            "command_ack",
            "telemetry",
            "heartbeat",
            "heartbeat_timeout",
            "endpoint_health",
            "datagram_tx",
            "datagram_rx",
        }
    )

    def __init__(
        self,
        writer: JsonlWriter,
        *,
        endpoint_instance_id: str,
        configuration_fingerprint: str,
    ) -> None:
        self._writer = writer
        self.endpoint_instance_id = endpoint_instance_id
        self.configuration_fingerprint = configuration_fingerprint
        self._active_phase: str | None = None
        self._active_window_id: str | None = None
        # Every record remains consumable by the legacy M2 JSONL loader, which
        # requires a canonical phase even for endpoint-lifecycle records that
        # happen just outside a transaction interval.
        self._ambient_phase = PHASES[0]
        self._last_tx_datagram_seq = 0
        self._last_rx_datagram_seq = 0
        self._transactions: dict[str, dict[str, Any]] = {}

    @property
    def active_phase(self) -> str | None:
        return self._active_phase

    @property
    def active_window_id(self) -> str | None:
        return self._active_window_id

    @property
    def raw_sequences(self) -> tuple[int, int]:
        return self._last_tx_datagram_seq, self._last_rx_datagram_seq

    def set_ambient_phase(self, phase: str) -> None:
        if phase not in PHASES:
            raise ValueError(f"persistent endpoint ambient phase is invalid: {phase!r}")
        if self._active_phase is not None and phase != self._active_phase:
            raise ValueError("persistent endpoint cannot change ambient phase inside an active window")
        self._ambient_phase = phase

    def _require_active_window(self, event: str, phase: str) -> None:
        if self._active_phase is None or self._active_window_id is None:
            raise ValueError(f"{event} is outside a persistent endpoint window")
        if phase != self._active_phase:
            raise ValueError(
                f"{event} belongs to phase {phase!r}, active endpoint phase is {self._active_phase!r}"
            )

    @staticmethod
    def _transaction_id(value: Any) -> str:
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError("persistent endpoint event has an invalid transaction_id")
        return value

    @staticmethod
    def _strict_next(value: Any, previous: int, field: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value != previous + 1:
            raise ValueError(
                f"persistent endpoint {field} must be exactly {previous + 1}, got {value!r}"
            )
        return value

    def _validate_transaction_event(self, event: str, fields: dict[str, Any]) -> None:
        if event == "command_attempt":
            transaction_id = self._transaction_id(fields.get("transaction_id"))
            if transaction_id in self._transactions:
                raise ValueError(f"duplicate persistent endpoint transaction_id {transaction_id}")
            self._transactions[transaction_id] = {
                "phase": self._active_phase,
                "legs": set(),
                "result": False,
            }
            return

        if event == "datagram_tx":
            next_sequence = self._strict_next(
                fields.get("tx_datagram_seq"), self._last_tx_datagram_seq, "tx_datagram_seq"
            )
            transaction_id = self._transaction_id(fields.get("transaction_id"))
            transaction = self._transactions.get(transaction_id)
            if transaction is None or transaction["phase"] != self._active_phase:
                raise ValueError("datagram_tx does not belong to an active persistent endpoint transaction")
            leg = fields.get("leg")
            legs = transaction["legs"]
            if leg == "marker":
                if legs:
                    raise ValueError("persistent endpoint marker leg is duplicated or out of order")
            elif leg == "command":
                if legs != {"marker"}:
                    raise ValueError("persistent endpoint command leg must follow exactly one marker leg")
            else:
                raise ValueError(f"invalid persistent endpoint transaction leg: {leg!r}")
            legs.add(leg)
            self._last_tx_datagram_seq = next_sequence
            return

        if event == "datagram_rx":
            self._last_rx_datagram_seq = self._strict_next(
                fields.get("rx_datagram_seq"), self._last_rx_datagram_seq, "rx_datagram_seq"
            )
            return

        transaction_value = fields.get("transaction_id")
        if transaction_value is None:
            return
        transaction_id = self._transaction_id(transaction_value)
        transaction = self._transactions.get(transaction_id)
        if transaction is None or transaction["phase"] != self._active_phase:
            raise ValueError(f"{event} does not belong to an active persistent endpoint transaction")
        if event == "command_result":
            if transaction["legs"] != {"marker", "command"}:
                raise ValueError("persistent endpoint command_result lacks a complete marker/command pair")
            if transaction["result"]:
                raise ValueError("duplicate persistent endpoint command_result")
            transaction["result"] = True

    @staticmethod
    def _fixed_envelope_field(fields: dict[str, Any], field: str, expected: Any) -> None:
        if field in fields and fields[field] != expected:
            raise ValueError(f"persistent endpoint event attempts to override {field}")
        fields[field] = expected

    def emit(self, event: str, **fields: Any) -> None:
        phase_value = fields.pop("phase", self._active_phase or self._ambient_phase)
        if not isinstance(phase_value, str):
            raise ValueError("persistent endpoint event phase must be a string")
        reserved = {
            "run_id",
            "runtime_id",
            "run_nonce",
            "schema_version",
            "event_seq",
            "wall_utc",
            "monotonic_ns",
        }
        supplied_reserved = reserved & fields.keys()
        if supplied_reserved:
            raise ValueError(
                f"persistent endpoint event attempts to override reserved fields {sorted(supplied_reserved)}"
            )
        if event in self._WINDOW_EVENTS:
            self._require_active_window(event, phase_value)
            self._validate_transaction_event(event, fields)
        if event in {"datagram_tx", "datagram_rx"}:
            self._fixed_envelope_field(fields, "event_schema", PROBE_RAW_EVENT_SCHEMA)
        else:
            self._fixed_envelope_field(fields, "event_schema", PERSISTENT_ENDPOINT_EVENT_SCHEMA)
        self._fixed_envelope_field(
            fields, "endpoint_event_schema", PERSISTENT_ENDPOINT_EVENT_SCHEMA
        )
        self._fixed_envelope_field(fields, "endpoint_instance_id", self.endpoint_instance_id)
        self._fixed_envelope_field(fields, "endpoint_generation", 1)
        self._fixed_envelope_field(
            fields, "endpoint_configuration_sha256", self.configuration_fingerprint
        )
        if self._active_window_id is not None:
            self._fixed_envelope_field(fields, "endpoint_window_id", self._active_window_id)
        elif "endpoint_window_id" in fields:
            raise ValueError("persistent endpoint event has a window identity outside a window")
        self._writer.emit(event, phase=phase_value, **fields)

    def begin_window(self, phase: str, window_id: str) -> None:
        if phase not in PHASES:
            raise ValueError(f"unsupported persistent endpoint phase: {phase!r}")
        if self._active_phase is not None:
            raise ValueError(f"persistent endpoint window {self._active_window_id!r} is still active")
        if not window_id or any(character.isspace() for character in window_id):
            raise ValueError("persistent endpoint window_id is invalid")
        self._active_phase = phase
        self._active_window_id = window_id
        self._ambient_phase = phase
        tx_before, rx_before = self.raw_sequences
        self.emit(
            "endpoint_window_open",
            phase=phase,
            window_id=window_id,
            tx_datagram_seq_before=tx_before,
            rx_datagram_seq_before=rx_before,
        )

    def close_window(self, *, completed: bool, reason: str | None = None) -> None:
        if self._active_phase is None or self._active_window_id is None:
            raise ValueError("persistent endpoint has no active window to close")
        phase = self._active_phase
        window_id = self._active_window_id
        incomplete = [
            transaction_id
            for transaction_id, transaction in self._transactions.items()
            if transaction["phase"] == phase and not transaction["result"]
        ]
        if completed and incomplete:
            raise ValueError(
                "persistent endpoint cannot close a completed window with unfinished transactions"
            )
        tx_after, rx_after = self.raw_sequences
        self.emit(
            "endpoint_window_close" if completed else "endpoint_window_abort",
            phase=phase,
            window_id=window_id,
            completed=completed,
            reason=reason,
            unfinished_transaction_ids=sorted(incomplete),
            tx_datagram_seq_after=tx_after,
            rx_datagram_seq_after=rx_after,
        )
        self._active_phase = None
        self._active_window_id = None

    def record_pre_window_datagram(
        self,
        *,
        phase: str,
        rx_datagram_seq: int,
        payload: bytes,
        peer: tuple[str, int],
        received_monotonic_ns: int,
    ) -> None:
        """Record a packet deliberately discarded before an M2 window opens.

        These packets never become positive/negative phase evidence, but their
        sequence numbers remain part of the one endpoint's complete UDP
        history.  That makes an old queued heartbeat visible instead of letting
        it be reinterpreted as fresh recovery traffic.
        """

        if phase not in PHASES or self._active_phase is not None:
            raise ValueError("persistent endpoint pre-window drain is invalid")
        next_sequence = self._strict_next(
            rx_datagram_seq, self._last_rx_datagram_seq, "rx_datagram_seq"
        )
        self.emit(
            "endpoint_pre_window_datagram",
            phase=phase,
            pre_window_for_phase=phase,
            rx_datagram_seq=rx_datagram_seq,
            transport_payload_sha256=hashlib.sha256(payload).hexdigest(),
            transport_payload_size=len(payload),
            peer=[peer[0], peer[1]],
            received_monotonic_ns=received_monotonic_ns,
            disposition="discarded_before_window",
        )
        self._last_rx_datagram_seq = next_sequence


def emit_datagram_tx(
    writer: JsonlWriter,
    datagram_writer: DatagramWriter,
    sequences: DatagramSequences,
    *,
    transaction_id: str,
    leg: str,
    attempt: int,
    nonce: str,
    payload: bytes,
) -> int:
    """Transmit one already-encoded frame and fsync its raw occurrence event."""

    if leg not in ("marker", "command"):
        raise ValueError(f"invalid M2 transaction leg: {leg!r}")
    payload = bytes(payload)
    payload_sha256 = hashlib.sha256(payload).hexdigest()
    tx_datagram_seq = sequences.next_tx()
    send_start_monotonic_ns = time.monotonic_ns()
    bytes_sent = datagram_writer.send(payload)
    send_complete_monotonic_ns = time.monotonic_ns()
    writer.emit(
        "datagram_tx",
        event_schema=PROBE_RAW_EVENT_SCHEMA,
        transaction_id=transaction_id,
        leg=leg,
        attempt=attempt,
        nonce=nonce,
        tx_datagram_seq=tx_datagram_seq,
        transport_payload_sha256=payload_sha256,
        transport_payload_size=len(payload),
        bytes_sent=bytes_sent,
        destination=list(datagram_writer.destination),
        send_start_monotonic_ns=send_start_monotonic_ns,
        send_complete_monotonic_ns=send_complete_monotonic_ns,
    )
    if bytes_sent != len(payload):
        raise RuntimeError(
            f"M2 {leg} datagram send was partial: sent {bytes_sent}, expected {len(payload)}"
        )
    return send_complete_monotonic_ns


@dataclass
class PhaseResult:
    attempts: int
    acknowledgements: int
    telemetry_responses: int
    heartbeat_count: int
    heartbeat_timeout: bool
    ack_latencies_ms: list[float]
    telemetry_latencies_ms: list[float]
    heartbeat_observation_count: int = 0
    heartbeat_observation_s: float | None = None


@dataclass(frozen=True)
class PhaseProcessContext:
    expected_ns3_state: str
    ns3_process: ProcessReference | None
    absent_processes: tuple[ProcessReference, ...]
    forbidden_endpoints: tuple[tuple[str, int], ...]
    forbidden_timeout_s: float


def relevant_messages(parser: Any, payload: bytes) -> list[Any]:
    messages = parser.parse_buffer(payload)
    return [] if messages is None else list(messages)


def receive_messages(
    sock: socket.socket,
    parser: Any,
    writer: JsonlWriter,
    sequences: DatagramSequences,
    *,
    deadline: float,
) -> Iterable[tuple[Any, tuple[str, int], int, str, int, int]]:
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
        rx_datagram_seq = sequences.next_rx()
        # This append/fsync intentionally precedes MAVLink parsing.  A parser
        # failure must not erase evidence that the UDP datagram reached GCS.
        writer.emit(
            "datagram_rx",
            event_schema=PROBE_RAW_EVENT_SCHEMA,
            rx_datagram_seq=rx_datagram_seq,
            transport_payload_sha256=packet_sha256,
            transport_payload_size=len(payload),
            peer=[peer[0], peer[1]],
            received_monotonic_ns=received_ns,
        )
        for frame_index, message in enumerate(relevant_messages(parser, payload), start=1):
            if message.get_type() != "BAD_DATA":
                yield message, peer, received_ns, packet_sha256, rx_datagram_seq, frame_index


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
    rx_datagram_seq: int,
    frame_index: int,
    transaction_id: str | None,
    liveness_observation: bool = False,
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
        "rx_datagram_seq": rx_datagram_seq,
        "frame_index": frame_index,
    }
    if transaction_id is not None:
        base["transaction_id"] = transaction_id
    if message_type == "HEARTBEAT":
        writer.emit("heartbeat", **base, liveness_observation=liveness_observation)
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
    sequences: DatagramSequences,
    *,
    target_system: int,
    timeout_s: float,
) -> int:
    heartbeats = 0
    for message, peer, _received_ns, packet_sha256, rx_datagram_seq, frame_index in receive_messages(
        sock,
        parser,
        writer,
        sequences,
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
            rx_datagram_seq=rx_datagram_seq,
            frame_index=frame_index,
            transaction_id=None,
        )
        if message_type == "HEARTBEAT" and int(message.get_srcSystem()) == target_system:
            heartbeats += 1
            break
    return heartbeats


def observe_positive_heartbeats(
    sock: socket.socket,
    parser: Any,
    writer: JsonlWriter,
    sequences: DatagramSequences,
    *,
    target_system: int,
    observation_s: float,
) -> int:
    """Record liveness for the complete bounded positive-phase interval.

    This deliberately does not stop after the first (or third) heartbeat.
    The entire configured interval belongs to the positive phase, starts only
    after ``phase_start`` was emitted, and provides the independent validator
    with several candidate fresh liveness frames to correlate with adapter
    forwarding.  It neither holds nor suppresses the continuous SITL stream.
    """

    if not math.isfinite(observation_s) or observation_s <= 0:
        raise ValueError("positive heartbeat observation must be finite and positive")
    heartbeats = 0
    for message, peer, _received_ns, packet_sha256, rx_datagram_seq, frame_index in receive_messages(
        sock,
        parser,
        writer,
        sequences,
        deadline=time.monotonic() + observation_s,
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
            rx_datagram_seq=rx_datagram_seq,
            frame_index=frame_index,
            transaction_id=None,
            liveness_observation=True,
        )
        if message_type == "HEARTBEAT" and int(message.get_srcSystem()) == target_system:
            heartbeats += 1
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
        minimum_positive_heartbeats=(MIN_POSITIVE_HEARTBEATS if args.expected_ack else 0),
        positive_heartbeat_observation_s=(
            args.positive_heartbeat_observation_s if args.expected_ack else None
        ),
    )


def execute_phase(
    args: argparse.Namespace,
    writer: JsonlWriter,
    *,
    persistent_sock: Any | None = None,
    datagram_sequences: DatagramSequences | None = None,
) -> PhaseResult:
    """Run one probe phase, optionally through an already-bound UDP endpoint.

    The optional endpoint is used only by the persistent M2 service.  Keeping
    the historical one-shot path as the default preserves its command-line
    contract while allowing one UDP identity to span good/down/recovery.
    """

    os.environ.setdefault("MAVLINK20", "1")
    try:
        from pymavlink import mavutil
    except ImportError as exc:  # pragma: no cover - runtime dependency gate.
        raise RuntimeError(f"pymavlink is required: {exc}") from exc

    # This must remain before bind() in one-shot mode: see emit_phase_start().
    # The persistent service writes its endpoint-window boundary before it
    # calls this function and deliberately retains its existing UDP bind.
    emit_phase_start(writer, args)
    owns_socket = persistent_sock is None
    if persistent_sock is None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, MAVLINK_CONTROL_TOS)
        if sock.getsockopt(socket.IPPROTO_IP, socket.IP_TOS) != MAVLINK_CONTROL_TOS:
            raise RuntimeError("M2 GCS socket did not retain the control DSCP/TOS identity")
        sock.bind(args.gcs_bind)
        sock.setblocking(False)
    else:
        sock = persistent_sock
        if sock.getsockopt(socket.IPPROTO_IP, socket.IP_TOS) != MAVLINK_CONTROL_TOS:
            raise RuntimeError("persistent M2 GCS socket lost the control DSCP/TOS identity")
    destination = args.uav_endpoint
    # Build each marker/command pair first, then commit the local
    # ``command_attempt`` record before emitting either datagram.  The
    # adapter's matching GCS->tail forward can therefore be causally checked
    # against a real monotonic send boundary rather than a post-send log.
    datagram_writer = DatagramWriter(sock, destination, transmit=False)
    outbound = mavutil.mavlink.MAVLink(
        datagram_writer,
        srcSystem=args.source_system,
        srcComponent=args.source_component,
    )
    inbound = mavutil.mavlink.MAVLink(None)
    inbound.robust_parsing = True
    sequences = datagram_sequences if datagram_sequences is not None else DatagramSequences()

    acknowledgements = 0
    telemetry_responses = 0
    heartbeat_count = 0
    heartbeat_observation_count = 0
    ack_latencies_ms: list[float] = []
    telemetry_latencies_ms: list[float] = []
    heartbeat_timeout = False

    try:
        if args.expected_ack:
            heartbeat_observation_count = observe_positive_heartbeats(
                sock,
                inbound,
                writer,
                sequences,
                target_system=args.target_system,
                observation_s=args.positive_heartbeat_observation_s,
            )
            heartbeat_count += heartbeat_observation_count
            if heartbeat_observation_count < MIN_POSITIVE_HEARTBEATS:
                heartbeat_timeout = True
                writer.emit(
                    "heartbeat_timeout",
                    timed_out=True,
                    timeout_s=args.positive_heartbeat_observation_s,
                    required_heartbeats=MIN_POSITIVE_HEARTBEATS,
                    observed_heartbeats=heartbeat_observation_count,
                )

        for attempt in range(1, args.attempts + 1):
            nonce = attempt_nonce(args.run_nonce, args.phase, attempt)
            marker = make_marker(args.run_nonce, args.phase, attempt)
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
            mavlink_command = int(mavutil.mavlink.MAV_CMD_REQUEST_MESSAGE)
            transaction_id = make_transaction_id(
                run_nonce=args.run_nonce,
                phase=args.phase,
                attempt=attempt,
                marker_sha256=marker_sha256,
                command_sha256=request_sha256,
                mavlink_seq=request_mavlink_seq,
                source_system=args.source_system,
                source_component=args.source_component,
                target_system=args.target_system,
                target_component=args.target_component,
                mavlink_command=mavlink_command,
            )
            writer.emit(
                "command_attempt",
                attempt=attempt,
                nonce=nonce,
                transaction_id=transaction_id,
                packet_sha256=request_sha256,
                marker_sha256=marker_sha256,
                command_sha256=request_sha256,
                marker_text=marker,
                marker_mavlink_seq=mavlink_frame_sequence(marker_payload),
                mavlink_seq=request_mavlink_seq,
                expected_ack=args.expected_ack,
                mavlink_command=mavlink_command,
                source_system=args.source_system,
                source_component=args.source_component,
                target_system=args.target_system,
                target_component=args.target_component,
            )
            emit_datagram_tx(
                writer,
                datagram_writer,
                sequences,
                transaction_id=transaction_id,
                leg="marker",
                attempt=attempt,
                nonce=nonce,
                payload=marker_payload,
            )
            sent_ns = emit_datagram_tx(
                writer,
                datagram_writer,
                sequences,
                transaction_id=transaction_id,
                leg="command",
                attempt=attempt,
                nonce=nonce,
                payload=request_payload,
            )

            ack = False
            telemetry = False
            ack_latency_ms: float | None = None
            telemetry_latency_ms: float | None = None
            deadline = time.monotonic() + args.ack_timeout_s
            for (
                message,
                peer,
                received_ns,
                packet_sha256,
                rx_datagram_seq,
                frame_index,
            ) in receive_messages(
                sock,
                inbound,
                writer,
                sequences,
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
                    rx_datagram_seq=rx_datagram_seq,
                    frame_index=frame_index,
                    transaction_id=transaction_id,
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
                transaction_id=transaction_id,
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
                sequences,
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
        if owns_socket:
            sock.close()

    writer.emit(
        "phase_end",
        attempts=args.attempts,
        acknowledgements=acknowledgements,
        telemetry_responses=telemetry_responses,
        heartbeat_count=heartbeat_count,
        heartbeat_observation_count=heartbeat_observation_count,
        heartbeat_observation_s=(
            args.positive_heartbeat_observation_s if args.expected_ack else None
        ),
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
        heartbeat_observation_count=heartbeat_observation_count,
        heartbeat_observation_s=(
            args.positive_heartbeat_observation_s if args.expected_ack else None
        ),
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
        "heartbeat_observation_count": result.heartbeat_observation_count,
        "heartbeat_observation_s": result.heartbeat_observation_s,
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
            and result.heartbeat_observation_count >= MIN_POSITIVE_HEARTBEATS
            and not result.heartbeat_timeout
        )
    return (
        result.acknowledgements == 0
        and result.telemetry_responses == 0
        and result.heartbeat_count == 0
        and result.heartbeat_timeout
    )


def _require_finite_number(value: Any, field: str, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return result


def _require_positive_int(value: Any, field: str, *, maximum: int = 10_000) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"{field} must be an integer in 1..{maximum}")
    return value


def _validate_persistent_identity(run_id: Any, runtime_id: Any, run_nonce: Any) -> None:
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("persistent endpoint run_id is missing")
    if not isinstance(runtime_id, str) or len(runtime_id) < 8:
        raise ValueError("persistent endpoint runtime_id is missing or too short")
    if not isinstance(run_nonce, str) or re.fullmatch(r"[A-Za-z0-9_-]{16,128}", run_nonce) is None:
        raise ValueError("persistent endpoint run_nonce is invalid")


def _validate_persistent_configuration(configuration: PersistentEndpointConfig) -> None:
    for name, endpoint, allow_ephemeral in (
        ("gcs_bind", configuration.gcs_bind, True),
        ("uav_endpoint", configuration.uav_endpoint, False),
    ):
        if (
            not isinstance(endpoint, tuple)
            or len(endpoint) != 2
            or not isinstance(endpoint[0], str)
            or not endpoint[0]
            or isinstance(endpoint[1], bool)
            or not isinstance(endpoint[1], int)
        ):
            raise ValueError(f"persistent endpoint {name} is invalid")
        minimum_port = 0 if allow_ephemeral else 1
        if not minimum_port <= endpoint[1] <= 65535:
            raise ValueError(f"persistent endpoint {name} port is invalid")
    for name in ("target_system", "target_component", "source_system", "source_component"):
        value = getattr(configuration, name)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 255:
            raise ValueError(f"persistent endpoint {name} must be in 1..255")


def _phase_result_payload(result: PhaseResult) -> dict[str, Any]:
    return {
        "attempts": result.attempts,
        "acknowledgements": result.acknowledgements,
        "telemetry_responses": result.telemetry_responses,
        "heartbeat_count": result.heartbeat_count,
        "heartbeat_timeout": result.heartbeat_timeout,
        "heartbeat_observation_count": result.heartbeat_observation_count,
        "heartbeat_observation_s": result.heartbeat_observation_s,
        "ack_latency": latency_stats(result.ack_latencies_ms),
        "telemetry_latency": latency_stats(result.telemetry_latencies_ms),
    }


PersistentPhaseExecutor = Callable[
    [argparse.Namespace, EndpointEventWriter, socket.socket, DatagramSequences], PhaseResult
]


class PersistentGcsEndpoint:
    """One UDP GCS endpoint with a local, authenticated phase-control plane.

    The service intentionally has one owner for the UDP socket and its raw
    occurrence sequence numbers.  A controller cannot re-bind a GCS port for
    each phase: it can only request the canonical ``good -> down -> recovery``
    lifecycle over an AF_UNIX/SOCK_SEQPACKET control socket.
    """

    _COMMON_REQUEST_KEYS = frozenset(
        {"schema", "run_id", "runtime_id", "run_nonce", "request_id", "operation"}
    )
    _RUN_PHASE_KEYS = frozenset(
        {
            "phase",
            "attempts",
            "ack_timeout_s",
            "heartbeat_timeout_s",
            "positive_heartbeat_observation_s",
        }
    )
    _PROCESS_EVIDENCE_KEYS = frozenset(
        {
            "expected_ns3_state",
            "ns3_process",
            "absent_processes",
            "forbidden_endpoints",
            "forbidden_timeout_s",
        }
    )

    def __init__(
        self,
        configuration: PersistentEndpointConfig,
        *,
        control_socket: Path,
        event_log: Path,
        run_id: str,
        runtime_id: str,
        run_nonce: str,
        phase_executor: PersistentPhaseExecutor | None = None,
        pre_window_quiet_s: float = 0.1,
        pre_window_max_wait_s: float = 10.0,
        process_identity: Path | None = None,
        process_event_log: Path | None = None,
    ) -> None:
        _validate_persistent_identity(run_id, runtime_id, run_nonce)
        _validate_persistent_configuration(configuration)
        pre_window_quiet_s = _require_finite_number(
            pre_window_quiet_s,
            "pre_window_quiet_s",
            minimum=0.001,
            maximum=30.0,
        )
        pre_window_max_wait_s = _require_finite_number(
            pre_window_max_wait_s,
            "pre_window_max_wait_s",
            minimum=pre_window_quiet_s,
            maximum=600.0,
        )
        if not hasattr(socket, "SOCK_SEQPACKET"):
            raise RuntimeError("persistent M2 control requires AF_UNIX/SOCK_SEQPACKET support")
        if not control_socket.name:
            raise ValueError("persistent endpoint control socket path is invalid")
        if (process_identity is None) != (process_event_log is None):
            raise ValueError(
                "persistent endpoint requires both process_identity and process_event_log, or neither"
            )
        if process_event_log is not None and process_event_log == event_log:
            raise ValueError("persistent endpoint process event log must differ from endpoint event log")
        self.configuration = configuration
        self.control_socket = control_socket
        self.event_log = event_log
        self.process_identity = process_identity
        self.process_event_log = process_event_log
        self.run_id = run_id
        self.runtime_id = runtime_id
        self.run_nonce = run_nonce
        self.endpoint_instance_id = persistent_endpoint_instance_id(
            run_id=run_id,
            runtime_id=runtime_id,
            run_nonce=run_nonce,
            configuration=configuration,
        )
        self._phase_executor = phase_executor or self._execute_phase
        self.pre_window_quiet_s = pre_window_quiet_s
        self.pre_window_max_wait_s = pre_window_max_wait_s
        self._json_writer: JsonlWriter | None = None
        self._process_json_writer: JsonlWriter | None = None
        self._processes: list[dict[str, Any]] | None = None
        self._events: EndpointEventWriter | None = None
        self._udp_socket: socket.socket | None = None
        self._control_listener: socket.socket | None = None
        self._control_socket_inode: tuple[int, int] | None = None
        self._bound_gcs: tuple[str, int] | None = None
        self._sequences = DatagramSequences()
        self._completed_phases: list[str] = []
        self._seen_request_ids: set[str] = set()
        self._phase_failed = False
        self._shutdown_requested = False
        self._started = False
        self._closed = False

    @property
    def events(self) -> EndpointEventWriter:
        if self._events is None:
            raise RuntimeError("persistent endpoint is not started")
        return self._events

    @property
    def udp_socket(self) -> socket.socket:
        if self._udp_socket is None:
            raise RuntimeError("persistent endpoint UDP socket is not started")
        return self._udp_socket

    @property
    def bound_gcs(self) -> tuple[str, int]:
        if self._bound_gcs is None:
            raise RuntimeError("persistent endpoint UDP socket is not started")
        return self._bound_gcs

    def start(self) -> None:
        if self._closed:
            raise RuntimeError("persistent endpoint has already been closed")
        if self._started:
            return
        if os.path.lexists(self.control_socket):
            raise FileExistsError(
                f"refusing to replace existing persistent endpoint control socket {self.control_socket}"
            )
        if os.path.lexists(self.event_log):
            raise FileExistsError(
                f"refusing to append to existing persistent endpoint event log {self.event_log}"
            )
        if self.process_event_log is not None and os.path.lexists(self.process_event_log):
            raise FileExistsError(
                f"refusing to append to existing persistent process event log {self.process_event_log}"
            )

        self.control_socket.parent.mkdir(parents=True, exist_ok=True)
        self.event_log.parent.mkdir(parents=True, exist_ok=True)
        if self.process_event_log is not None:
            self.process_event_log.parent.mkdir(parents=True, exist_ok=True)
        try:
            if self.process_identity is not None:
                self._processes = load_process_identity(
                    self.process_identity,
                    self.run_id,
                    self.runtime_id,
                    self.run_nonce,
                )
            self._json_writer = JsonlWriter(
                self.event_log,
                run_id=self.run_id,
                runtime_id=self.runtime_id,
                run_nonce=self.run_nonce,
                phase="endpoint",
            )
            if self.process_event_log is not None:
                self._process_json_writer = JsonlWriter(
                    self.process_event_log,
                    run_id=self.run_id,
                    runtime_id=self.runtime_id,
                    run_nonce=self.run_nonce,
                    phase=PHASES[0],
                )
            self._events = EndpointEventWriter(
                self._json_writer,
                endpoint_instance_id=self.endpoint_instance_id,
                configuration_fingerprint=self.configuration.fingerprint(),
            )
            udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            udp_socket.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, MAVLINK_CONTROL_TOS)
            if udp_socket.getsockopt(socket.IPPROTO_IP, socket.IP_TOS) != MAVLINK_CONTROL_TOS:
                raise RuntimeError("persistent M2 GCS socket did not retain the control DSCP/TOS identity")
            udp_socket.bind(self.configuration.gcs_bind)
            udp_socket.setblocking(False)
            bound = udp_socket.getsockname()
            self._udp_socket = udp_socket
            self._bound_gcs = (str(bound[0]), int(bound[1]))

            control_listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            control_listener.bind(os.fspath(self.control_socket))
            control_stat = os.lstat(self.control_socket)
            self._control_socket_inode = (control_stat.st_dev, control_stat.st_ino)
            os.chmod(self.control_socket, 0o600)
            control_listener.listen(8)
            control_listener.settimeout(0.25)
            self._control_listener = control_listener
            self.events.emit(
                "endpoint_started",
                control_socket=os.fspath(self.control_socket),
                gcs_bind=list(self.bound_gcs),
                endpoint_configuration=self.configuration.as_dict(),
                endpoint_pid=os.getpid(),
                endpoint_uid=os.getuid(),
                pre_window_quiet_s=self.pre_window_quiet_s,
                pre_window_max_wait_s=self.pre_window_max_wait_s,
            )
            self._started = True
        except Exception:
            self._close_resources(emit_stop=False)
            raise

    def _close_resources(self, *, emit_stop: bool) -> None:
        if emit_stop and self._events is not None and self._started:
            try:
                if self.events.active_phase is not None:
                    self.events.close_window(completed=False, reason="endpoint_close")
                tx_sequence, rx_sequence = self.events.raw_sequences
                self.events.emit(
                    "endpoint_stopped",
                    completed_phases=list(self._completed_phases),
                    lifecycle_complete=self._completed_phases == list(PHASES) and not self._phase_failed,
                    phase_failed=self._phase_failed,
                    tx_datagram_seq=tx_sequence,
                    rx_datagram_seq=rx_sequence,
                )
            except Exception:
                # Cleanup must not leave sockets or a flock held merely because
                # a final diagnostic event could not be appended.
                pass
        for sock in (self._control_listener, self._udp_socket):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        self._control_listener = None
        self._udp_socket = None
        if self._control_socket_inode is not None and os.path.lexists(self.control_socket):
            try:
                current_stat = os.lstat(self.control_socket)
                if (
                    stat.S_ISSOCK(current_stat.st_mode)
                    and (current_stat.st_dev, current_stat.st_ino) == self._control_socket_inode
                ):
                    os.unlink(self.control_socket)
            except OSError:
                pass
        self._control_socket_inode = None
        if self._json_writer is not None:
            try:
                self._json_writer.close()
            except OSError:
                pass
        self._json_writer = None
        if self._process_json_writer is not None:
            try:
                self._process_json_writer.close()
            except OSError:
                pass
        self._process_json_writer = None

    def close(self) -> None:
        if self._closed:
            return
        self._close_resources(emit_stop=True)
        self._closed = True

    def _response(
        self,
        *,
        request_id: str | None,
        operation: str | None,
        ok: bool,
        error: str | None = None,
        result: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        response: dict[str, Any] = {
            "schema": PERSISTENT_CONTROL_RESPONSE_SCHEMA,
            "run_id": self.run_id,
            "runtime_id": self.runtime_id,
            "run_nonce": self.run_nonce,
            "request_id": request_id,
            "operation": operation,
            "ok": ok,
        }
        if error is not None:
            response["error"] = error
        if result is not None:
            response["result"] = dict(result)
        return response

    @staticmethod
    def _request_digest(request: Any) -> str:
        try:
            encoded = json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError):
            encoded = repr(request).encode("utf-8", errors="replace")
        return hashlib.sha256(encoded).hexdigest()

    def _record_rejection(
        self,
        *,
        request_id: str | None,
        operation: str | None,
        error: str,
        request_digest: str,
    ) -> None:
        if not self._started:
            return
        try:
            self.events.emit(
                "endpoint_control_rejected",
                request_id=request_id,
                operation=operation,
                error=error,
                request_sha256=request_digest,
            )
        except (OSError, RuntimeError, ValueError):
            pass

    def _validate_request(self, request: Mapping[str, Any]) -> tuple[str, str]:
        if not isinstance(request.get("schema"), str) or request["schema"] != PERSISTENT_CONTROL_SCHEMA:
            raise ValueError("control schema is invalid")
        observed_identity = (
            request.get("run_id"),
            request.get("runtime_id"),
            request.get("run_nonce"),
        )
        expected_identity = (self.run_id, self.runtime_id, self.run_nonce)
        if observed_identity != expected_identity:
            raise ValueError("control request identity does not match the persistent endpoint")
        request_id = request.get("request_id")
        if not isinstance(request_id, str) or PERSISTENT_CONTROL_REQUEST_ID.fullmatch(request_id) is None:
            raise ValueError("control request_id is invalid")
        operation = request.get("operation")
        if not isinstance(operation, str) or operation not in {"run_phase", "status", "shutdown"}:
            raise ValueError("control operation is invalid")
        expected_keys = self._COMMON_REQUEST_KEYS | (
            (self._RUN_PHASE_KEYS | self._PROCESS_EVIDENCE_KEYS)
            if operation == "run_phase"
            else frozenset()
        )
        actual_keys = frozenset(request)
        missing = expected_keys - actual_keys
        unexpected = actual_keys - expected_keys
        if missing or unexpected:
            raise ValueError(
                f"control request keys are invalid (missing={sorted(missing)}, unexpected={sorted(unexpected)})"
            )
        if request_id in self._seen_request_ids:
            raise ValueError(f"duplicate control request_id {request_id}")
        return request_id, str(operation)

    def _phase_arguments(self, request: Mapping[str, Any]) -> argparse.Namespace:
        phase = request.get("phase")
        if not isinstance(phase, str) or phase not in PHASES:
            raise ValueError("control phase is invalid")
        if self._phase_failed:
            raise ValueError("persistent endpoint will not accept another phase after a failed phase")
        if self.events.active_phase is not None:
            raise ValueError("persistent endpoint already has an active phase")
        expected_phase = PHASES[len(self._completed_phases)] if len(self._completed_phases) < len(PHASES) else None
        if phase != expected_phase:
            raise ValueError(f"control phase {phase!r} is out of order; expected {expected_phase!r}")
        attempts = _require_positive_int(request.get("attempts"), "attempts")
        ack_timeout_s = _require_finite_number(
            request.get("ack_timeout_s"), "ack_timeout_s", minimum=0.001, maximum=600.0
        )
        heartbeat_timeout_s = _require_finite_number(
            request.get("heartbeat_timeout_s"), "heartbeat_timeout_s", minimum=0.001, maximum=600.0
        )
        positive_observation_s = _require_finite_number(
            request.get("positive_heartbeat_observation_s"),
            "positive_heartbeat_observation_s",
            minimum=float(MIN_POSITIVE_HEARTBEATS),
            maximum=600.0,
        )
        return argparse.Namespace(
            phase=phase,
            attempts=attempts,
            expected_ack=phase != "down",
            run_id=self.run_id,
            runtime_id=self.runtime_id,
            run_nonce=self.run_nonce,
            gcs_bind=self.bound_gcs,
            uav_endpoint=self.configuration.uav_endpoint,
            target_system=self.configuration.target_system,
            target_component=self.configuration.target_component,
            source_system=self.configuration.source_system,
            source_component=self.configuration.source_component,
            ack_timeout_s=ack_timeout_s,
            heartbeat_timeout_s=heartbeat_timeout_s,
            positive_heartbeat_observation_s=positive_observation_s,
        )

    @staticmethod
    def _execute_phase(
        args: argparse.Namespace,
        writer: EndpointEventWriter,
        sock: socket.socket,
        sequences: DatagramSequences,
    ) -> PhaseResult:
        return execute_phase(
            args,
            writer,
            persistent_sock=sock,
            datagram_sequences=sequences,
        )

    def _phase_process_context(
        self,
        request: Mapping[str, Any],
        *,
        phase: str,
    ) -> PhaseProcessContext | None:
        if self._processes is None:
            return None
        expected_ns3_state = request.get("expected_ns3_state")
        canonical_state = "down" if phase == "down" else "up"
        if expected_ns3_state != canonical_state:
            raise ValueError(
                f"control expected_ns3_state for {phase} must be {canonical_state!r}"
            )
        ns3_payload = request.get("ns3_process")
        if canonical_state == "up":
            ns3_process = control_process_reference(ns3_payload, "ns3_process")
        else:
            if ns3_payload is not None:
                raise ValueError("ns3_process must be null while ns-3 is expected down")
            ns3_process = None
        absent_payload = request.get("absent_processes")
        if not isinstance(absent_payload, list):
            raise ValueError("absent_processes must be a list")
        absent_processes = tuple(
            control_process_reference(item, f"absent_processes[{index}]")
            for index, item in enumerate(absent_payload)
        )
        absent_identities = {
            (reference.pid, reference.start_ticks, reference.cmdline_sha256)
            for reference in absent_processes
        }
        if len(absent_identities) != len(absent_processes):
            raise ValueError("absent_processes contains a duplicate identity")
        if canonical_state == "down" and not absent_processes:
            raise ValueError("down phase requires the prior ns-3 process in absent_processes")
        forbidden_endpoints = tuple(control_endpoints(request.get("forbidden_endpoints"), "forbidden_endpoints"))
        if set(forbidden_endpoints) != set(REQUIRED_FORBIDDEN_ENDPOINTS) or len(
            forbidden_endpoints
        ) != len(REQUIRED_FORBIDDEN_ENDPOINTS):
            raise ValueError(
                f"forbidden_endpoints must be exactly {list(REQUIRED_FORBIDDEN_ENDPOINTS)}"
            )
        forbidden_timeout_s = _require_finite_number(
            request.get("forbidden_timeout_s"),
            "forbidden_timeout_s",
            minimum=0.001,
            maximum=60.0,
        )
        return PhaseProcessContext(
            expected_ns3_state=canonical_state,
            ns3_process=ns3_process,
            absent_processes=absent_processes,
            forbidden_endpoints=forbidden_endpoints,
            forbidden_timeout_s=forbidden_timeout_s,
        )

    def _record_forbidden_direct_probes(
        self,
        *,
        phase: str,
        context: PhaseProcessContext,
    ) -> bool:
        forbidden_clear = True
        for endpoint in context.forbidden_endpoints:
            reachable, error = tcp_reachable(endpoint, context.forbidden_timeout_s)
            forbidden_clear = forbidden_clear and not reachable
            self.events.emit(
                "direct_endpoint_probe",
                phase=phase,
                endpoint=[endpoint[0], endpoint[1]],
                reachable=reachable,
                error=error,
            )
        return forbidden_clear

    def _emit_phase_process_evidence(
        self,
        *,
        args: argparse.Namespace,
        context: PhaseProcessContext,
        before: tuple[bool, list[dict[str, Any]], dict[str, Any] | None, list[dict[str, Any]]],
        after: tuple[bool, list[dict[str, Any]], dict[str, Any] | None, list[dict[str, Any]]],
    ) -> bool:
        if self._processes is None or self._process_json_writer is None:
            raise RuntimeError("persistent endpoint process evidence is not initialized")
        return emit_phase_process_records(
            PhaseScopedJsonlWriter(self._process_json_writer, args.phase),
            self.events,
            before=before,
            after=after,
            expected_ns3_state=context.expected_ns3_state,
            ns3_process=context.ns3_process,
            absent_processes=list(context.absent_processes),
        )

    def _run_phase(self, request: Mapping[str, Any]) -> dict[str, Any]:
        args = self._phase_arguments(request)
        process_context = self._phase_process_context(request, phase=args.phase)
        self.events.set_ambient_phase(args.phase)
        self._drain_before_window(args.phase)
        forbidden_clear = True
        process_before: tuple[
            bool,
            list[dict[str, Any]],
            dict[str, Any] | None,
            list[dict[str, Any]],
        ] | None = None
        if process_context is not None:
            process_before = process_snapshot(
                self._processes or [],
                ns3_process=process_context.ns3_process,
                absent_processes=list(process_context.absent_processes),
            )
            forbidden_clear = self._record_forbidden_direct_probes(
                phase=args.phase,
                context=process_context,
            )
        window_id = f"{len(self._completed_phases) + 1}-{args.phase}"
        self.events.begin_window(args.phase, window_id)
        try:
            result = self._phase_executor(args, self.events, self.udp_socket, self._sequences)
            health_ok = True
            if process_context is not None and process_before is not None:
                process_after = process_snapshot(
                    self._processes or [],
                    ns3_process=process_context.ns3_process,
                    absent_processes=list(process_context.absent_processes),
                )
                health_ok = self._emit_phase_process_evidence(
                    args=args,
                    context=process_context,
                    before=process_before,
                    after=process_after,
                )
            if not criteria_met(args, result, health_ok and forbidden_clear):
                raise RuntimeError("persistent endpoint phase criteria or process health failed")
            self.events.close_window(completed=True)
        except Exception as exc:
            self._phase_failed = True
            if self.events.active_phase is not None:
                self.events.close_window(completed=False, reason=type(exc).__name__)
            raise
        self._completed_phases.append(args.phase)
        tx_sequence, rx_sequence = self.events.raw_sequences
        return {
            "phase": args.phase,
            "window_id": window_id,
            "phase_result": _phase_result_payload(result),
            "completed_phases": list(self._completed_phases),
            "next_phase": (
                PHASES[len(self._completed_phases)]
                if len(self._completed_phases) < len(PHASES)
                else None
            ),
            "gcs_bind": list(self.bound_gcs),
            "tx_datagram_seq": tx_sequence,
            "rx_datagram_seq": rx_sequence,
        }

    def _drain_before_window(self, phase: str) -> None:
        """Require a quiet, logged UDP boundary before opening a phase window."""

        if self.events.active_phase is not None:
            raise RuntimeError("persistent endpoint cannot drain while a phase is active")
        started = time.monotonic()
        quiet_deadline = started + self.pre_window_quiet_s
        hard_deadline = started + self.pre_window_max_wait_s
        discarded = 0
        while True:
            try:
                payload, peer = self.udp_socket.recvfrom(65535)
            except BlockingIOError:
                now = time.monotonic()
                if now >= quiet_deadline:
                    break
                if now >= hard_deadline:
                    raise TimeoutError(
                        f"persistent endpoint did not become quiet before {phase} within "
                        f"{self.pre_window_max_wait_s}s"
                    )
                wait_s = min(quiet_deadline, hard_deadline) - now
                select.select([self.udp_socket], [], [], wait_s)
                continue
            received_ns = time.monotonic_ns()
            self.events.record_pre_window_datagram(
                phase=phase,
                rx_datagram_seq=self._sequences.next_rx(),
                payload=payload,
                peer=(str(peer[0]), int(peer[1])),
                received_monotonic_ns=received_ns,
            )
            discarded += 1
            quiet_deadline = time.monotonic() + self.pre_window_quiet_s
        self.events.emit(
            "endpoint_pre_window_quiescent",
            pre_window_for_phase=phase,
            quiet_s=self.pre_window_quiet_s,
            max_wait_s=self.pre_window_max_wait_s,
            discarded_datagrams=discarded,
            waited_s=round(time.monotonic() - started, 6),
        )

    def status(self) -> dict[str, Any]:
        tx_sequence, rx_sequence = self.events.raw_sequences
        return {
            "endpoint_instance_id": self.endpoint_instance_id,
            "endpoint_configuration_sha256": self.configuration.fingerprint(),
            "gcs_bind": list(self.bound_gcs),
            "completed_phases": list(self._completed_phases),
            "next_phase": (
                PHASES[len(self._completed_phases)]
                if len(self._completed_phases) < len(PHASES)
                else None
            ),
            "active_phase": self.events.active_phase,
            "phase_failed": self._phase_failed,
            "shutdown_requested": self._shutdown_requested,
            "tx_datagram_seq": tx_sequence,
            "rx_datagram_seq": rx_sequence,
        }

    def handle_control_request(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Validate and process exactly one SOCK_SEQPACKET request."""

        if not isinstance(request, Mapping):
            request_digest = self._request_digest(request)
            error = "control request root must be an object"
            self._record_rejection(
                request_id=None,
                operation=None,
                error=error,
                request_digest=request_digest,
            )
            return self._response(request_id=None, operation=None, ok=False, error=error)
        request_id = request.get("request_id") if isinstance(request.get("request_id"), str) else None
        operation = request.get("operation") if isinstance(request.get("operation"), str) else None
        request_digest = self._request_digest(request)
        try:
            request_id, operation = self._validate_request(request)
            self._seen_request_ids.add(request_id)
            self.events.emit(
                "endpoint_control_request",
                request_id=request_id,
                operation=operation,
                request_sha256=request_digest,
            )
            if operation == "run_phase":
                result = self._run_phase(request)
            elif operation == "status":
                result = self.status()
            else:
                if self.events.active_phase is not None:
                    raise ValueError("persistent endpoint cannot shut down while a phase is active")
                self._shutdown_requested = True
                self.events.emit("endpoint_shutdown_requested", request_id=request_id)
                result = self.status()
            self.events.emit(
                "endpoint_control_result",
                request_id=request_id,
                operation=operation,
                request_sha256=request_digest,
                ok=True,
            )
            return self._response(request_id=request_id, operation=operation, ok=True, result=result)
        except Exception as exc:
            error = str(exc)
            self._record_rejection(
                request_id=request_id,
                operation=operation,
                error=error,
                request_digest=request_digest,
            )
            return self._response(request_id=request_id, operation=operation, ok=False, error=error)

    @staticmethod
    def _peer_is_current_user(connection: socket.socket) -> bool:
        if not hasattr(socket, "SO_PEERCRED"):
            return False
        try:
            credentials = connection.getsockopt(
                socket.SOL_SOCKET,
                socket.SO_PEERCRED,
                struct.calcsize("3i"),
            )
            _pid, uid, _gid = struct.unpack("3i", credentials)
        except OSError:
            return False
        return uid == os.getuid()

    @staticmethod
    def _receive_control_packet(connection: socket.socket) -> Mapping[str, Any]:
        payload, _ancillary, flags, _address = connection.recvmsg(PERSISTENT_CONTROL_MAX_PACKET_BYTES)
        if flags & getattr(socket, "MSG_TRUNC", 0):
            raise ValueError("control packet exceeds the maximum size")
        if not payload:
            raise ValueError("control packet is empty")
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"control packet is not valid JSON: {exc}") from exc
        if not isinstance(decoded, dict):
            raise ValueError("control packet root must be an object")
        return decoded

    @staticmethod
    def _send_control_packet(connection: socket.socket, response: Mapping[str, Any]) -> None:
        payload = json.dumps(response, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(payload) > PERSISTENT_CONTROL_MAX_PACKET_BYTES:
            raise ValueError("control response exceeds the maximum size")
        sent = connection.send(payload)
        if sent != len(payload):
            raise RuntimeError("control response was partially sent")

    def _handle_connection(self, connection: socket.socket) -> None:
        if not self._peer_is_current_user(connection):
            self._send_control_packet(
                connection,
                self._response(
                    request_id=None,
                    operation=None,
                    ok=False,
                    error="control peer credentials are not authorized",
                ),
            )
            return
        try:
            request = self._receive_control_packet(connection)
        except ValueError as exc:
            self._record_rejection(
                request_id=None,
                operation=None,
                error=str(exc),
                request_digest=hashlib.sha256(str(exc).encode("utf-8")).hexdigest(),
            )
            response = self._response(request_id=None, operation=None, ok=False, error=str(exc))
        else:
            response = self.handle_control_request(request)
        self._send_control_packet(connection, response)

    def serve_forever(self) -> None:
        if not self._started:
            self.start()
        while not self._shutdown_requested:
            listener = self._control_listener
            if listener is None:
                break
            try:
                connection, _address = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._shutdown_requested or self._closed:
                    break
                raise
            with connection:
                try:
                    self._handle_connection(connection)
                except (OSError, RuntimeError, ValueError) as exc:
                    self._record_rejection(
                        request_id=None,
                        operation=None,
                        error=f"control connection failure: {exc}",
                        request_digest=hashlib.sha256(str(exc).encode("utf-8")).hexdigest(),
                    )


def send_persistent_control_request(
    control_socket: Path,
    request: Mapping[str, Any],
    *,
    timeout_s: float = 10.0,
) -> dict[str, Any]:
    """Send one atomic local control request and return its one atomic response."""

    timeout_s = _require_finite_number(timeout_s, "control timeout", minimum=0.001, maximum=600.0)
    payload = json.dumps(dict(request), sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(payload) > PERSISTENT_CONTROL_MAX_PACKET_BYTES:
        raise ValueError("control request exceeds the maximum size")
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as client:
        client.settimeout(timeout_s)
        client.connect(os.fspath(control_socket))
        sent = client.send(payload)
        if sent != len(payload):
            raise RuntimeError("control request was partially sent")
        response_payload, _ancillary, flags, _address = client.recvmsg(PERSISTENT_CONTROL_MAX_PACKET_BYTES)
    if flags & getattr(socket, "MSG_TRUNC", 0):
        raise ValueError("control response exceeds the maximum size")
    if not response_payload:
        raise ValueError("control response is empty")
    try:
        response = json.loads(response_payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"control response is not valid JSON: {exc}") from exc
    if not isinstance(response, dict):
        raise ValueError("control response root must be an object")
    return response


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
    parser.add_argument(
        "--positive-heartbeat-observation-s",
        type=float,
        default=DEFAULT_POSITIVE_HEARTBEAT_OBSERVATION_S,
    )
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
    if (
        not math.isfinite(args.positive_heartbeat_observation_s)
        or args.positive_heartbeat_observation_s < MIN_POSITIVE_HEARTBEATS
    ):
        parser.error(
            "--positive-heartbeat-observation-s must be finite and at least "
            f"{MIN_POSITIVE_HEARTBEATS} seconds"
        )
    if len(args.run_id) < 1 or len(args.runtime_id) < 8:
        parser.error("run/runtime/nonce identifiers are missing or too short")
    if re.fullmatch(r"[A-Za-z0-9_-]{16,128}", args.run_nonce) is None:
        parser.error("--run-nonce must match [A-Za-z0-9_-]{16,128}")
    if args.expected_ns3_state == "up" and args.ns3_process is None:
        parser.error("--ns3-process is required when ns-3 is expected up")
    if args.expected_ns3_state == "down" and args.ns3_process is not None:
        parser.error("--ns3-process is forbidden when ns-3 is expected down")
    return args


def parse_persistent_args(argv: list[str]) -> argparse.Namespace:
    """Parse the non-legacy ``serve`` and ``control`` endpoint commands."""

    parser = argparse.ArgumentParser(
        description=(
            "Persistent M2 GCS endpoint.  `serve` owns one UDP bind; `control` "
            "submits one authenticated lifecycle request over SOCK_SEQPACKET."
        )
    )
    commands = parser.add_subparsers(dest="persistent_command", required=True)

    serve = commands.add_parser("serve", help="bind one GCS UDP endpoint and serve phase controls")
    serve.add_argument("--run-id", required=True)
    serve.add_argument("--runtime-id", required=True)
    serve.add_argument("--run-nonce", required=True)
    serve.add_argument("--endpoint-event-log", type=Path, required=True)
    serve.add_argument("--control-socket", type=Path, required=True)
    serve.add_argument("--process-identity", type=Path)
    serve.add_argument("--process-event-log", type=Path)
    serve.add_argument("--gcs-bind", type=parse_endpoint, default=parse_endpoint("10.71.0.10:14600"))
    serve.add_argument(
        "--uav-endpoint", type=parse_endpoint, default=parse_endpoint("10.71.1.10:14601")
    )
    serve.add_argument("--target-system", type=int, default=1)
    serve.add_argument("--target-component", type=int, default=1)
    serve.add_argument("--source-system", type=int, default=255)
    serve.add_argument("--source-component", type=int, default=190)
    serve.add_argument("--pre-window-quiet-s", type=float, default=0.1)
    serve.add_argument("--pre-window-max-wait-s", type=float, default=10.0)

    control = commands.add_parser("control", help="submit one request to a persistent endpoint")
    control.add_argument("--control-socket", type=Path, required=True)
    control.add_argument("--run-id", required=True)
    control.add_argument("--runtime-id", required=True)
    control.add_argument("--run-nonce", required=True)
    control.add_argument("--request-id")
    control.add_argument(
        "--operation",
        choices=("run-phase", "status", "shutdown"),
        default="run-phase",
    )
    control.add_argument("--phase", choices=PHASES)
    control.add_argument("--expected-ns3-state", choices=("up", "down"))
    control.add_argument("--ns3-process", type=parse_process_reference)
    control.add_argument("--absent-process", action="append", type=parse_process_reference, default=[])
    control.add_argument("--forbidden-endpoint", action="append", type=parse_endpoint)
    control.add_argument("--forbidden-timeout-s", type=float, default=0.5)
    control.add_argument("--attempts", type=int, default=10)
    control.add_argument("--ack-timeout-s", type=float, default=3.0)
    control.add_argument("--heartbeat-timeout-s", type=float, default=5.0)
    control.add_argument(
        "--positive-heartbeat-observation-s",
        type=float,
        default=DEFAULT_POSITIVE_HEARTBEAT_OBSERVATION_S,
    )
    control.add_argument("--timeout-s", type=float, default=10.0)

    args = parser.parse_args(argv)
    try:
        _validate_persistent_identity(args.run_id, args.runtime_id, args.run_nonce)
        if args.persistent_command == "serve":
            for field in (
                "target_system",
                "target_component",
                "source_system",
                "source_component",
            ):
                value = getattr(args, field)
                if not 1 <= value <= 255:
                    raise ValueError(f"--{field.replace('_', '-')} must be in 1..255")
            quiet_s = _require_finite_number(
                args.pre_window_quiet_s,
                "--pre-window-quiet-s",
                minimum=0.001,
                maximum=30.0,
            )
            _require_finite_number(
                args.pre_window_max_wait_s,
                "--pre-window-max-wait-s",
                minimum=quiet_s,
                maximum=600.0,
            )
            if (args.process_identity is None) != (args.process_event_log is None):
                raise ValueError(
                    "--process-identity and --process-event-log must be supplied together"
                )
        else:
            if args.request_id is not None and PERSISTENT_CONTROL_REQUEST_ID.fullmatch(args.request_id) is None:
                raise ValueError("--request-id must match [A-Za-z0-9_.:-]{8,128}")
            _require_finite_number(args.timeout_s, "--timeout-s", minimum=0.001, maximum=600.0)
            if args.operation == "run-phase":
                if args.phase is None:
                    raise ValueError("--phase is required with --operation run-phase")
                expected_ns3_state = "down" if args.phase == "down" else "up"
                if args.expected_ns3_state != expected_ns3_state:
                    raise ValueError(
                        f"--expected-ns3-state must be {expected_ns3_state!r} for {args.phase}"
                    )
                if args.phase == "down":
                    if args.ns3_process is not None:
                        raise ValueError("--ns3-process is forbidden for the down phase")
                    if not args.absent_process:
                        raise ValueError("the down phase requires --absent-process")
                elif args.ns3_process is None:
                    raise ValueError("--ns3-process is required while ns-3 is expected up")
                for reference in [args.ns3_process, *args.absent_process]:
                    if reference is not None and reference.cmdline_sha256 is None:
                        raise ValueError(
                            "persistent control process references require :CMDLINE_SHA256"
                        )
                if args.forbidden_endpoint is None:
                    args.forbidden_endpoint = list(REQUIRED_FORBIDDEN_ENDPOINTS)
                if (
                    set(args.forbidden_endpoint) != set(REQUIRED_FORBIDDEN_ENDPOINTS)
                    or len(args.forbidden_endpoint) != len(REQUIRED_FORBIDDEN_ENDPOINTS)
                ):
                    raise ValueError(
                        f"--forbidden-endpoint must be exactly {list(REQUIRED_FORBIDDEN_ENDPOINTS)}"
                    )
                _require_finite_number(
                    args.forbidden_timeout_s,
                    "--forbidden-timeout-s",
                    minimum=0.001,
                    maximum=60.0,
                )
                _require_positive_int(args.attempts, "--attempts")
                _require_finite_number(
                    args.ack_timeout_s,
                    "--ack-timeout-s",
                    minimum=0.001,
                    maximum=600.0,
                )
                _require_finite_number(
                    args.heartbeat_timeout_s,
                    "--heartbeat-timeout-s",
                    minimum=0.001,
                    maximum=600.0,
                )
                _require_finite_number(
                    args.positive_heartbeat_observation_s,
                    "--positive-heartbeat-observation-s",
                    minimum=float(MIN_POSITIVE_HEARTBEATS),
                    maximum=600.0,
                )
    except ValueError as exc:
        parser.error(str(exc))
    return args


def make_persistent_control_request(args: argparse.Namespace) -> dict[str, Any]:
    if args.persistent_command != "control":
        raise ValueError("persistent control request requires the control command")
    operation = args.operation.replace("-", "_")
    request_id = args.request_id or uuid.uuid4().hex
    request: dict[str, Any] = {
        "schema": PERSISTENT_CONTROL_SCHEMA,
        "run_id": args.run_id,
        "runtime_id": args.runtime_id,
        "run_nonce": args.run_nonce,
        "request_id": request_id,
        "operation": operation,
    }
    if operation == "run_phase":
        request.update(
            {
                "phase": args.phase,
                "attempts": args.attempts,
                "ack_timeout_s": args.ack_timeout_s,
                "heartbeat_timeout_s": args.heartbeat_timeout_s,
                "positive_heartbeat_observation_s": args.positive_heartbeat_observation_s,
                "expected_ns3_state": args.expected_ns3_state,
                "ns3_process": (
                    None if args.ns3_process is None else process_reference_payload(args.ns3_process)
                ),
                "absent_processes": [
                    process_reference_payload(reference) for reference in args.absent_process
                ],
                "forbidden_endpoints": [list(endpoint) for endpoint in args.forbidden_endpoint],
                "forbidden_timeout_s": args.forbidden_timeout_s,
            }
        )
    return request


def persistent_main(argv: list[str]) -> int:
    args = parse_persistent_args(argv)
    if args.persistent_command == "control":
        request = make_persistent_control_request(args)
        try:
            response = send_persistent_control_request(
                args.control_socket,
                request,
                timeout_s=args.timeout_s,
            )
            expected = {
                "schema": PERSISTENT_CONTROL_RESPONSE_SCHEMA,
                "run_id": args.run_id,
                "runtime_id": args.runtime_id,
                "run_nonce": args.run_nonce,
                "request_id": request["request_id"],
                "operation": request["operation"],
            }
            for field, value in expected.items():
                if response.get(field) != value:
                    raise ValueError(f"persistent control response {field!r} does not match the request")
            if not isinstance(response.get("ok"), bool):
                raise ValueError("persistent control response has no boolean ok field")
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"FAIL persistent M2 control: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(response, sort_keys=True))
        return 0 if response["ok"] else 1

    endpoint = PersistentGcsEndpoint(
        PersistentEndpointConfig(
            gcs_bind=args.gcs_bind,
            uav_endpoint=args.uav_endpoint,
            target_system=args.target_system,
            target_component=args.target_component,
            source_system=args.source_system,
            source_component=args.source_component,
        ),
        control_socket=args.control_socket,
        event_log=args.endpoint_event_log,
        run_id=args.run_id,
        runtime_id=args.runtime_id,
        run_nonce=args.run_nonce,
        pre_window_quiet_s=args.pre_window_quiet_s,
        pre_window_max_wait_s=args.pre_window_max_wait_s,
        process_identity=args.process_identity,
        process_event_log=args.process_event_log,
    )
    try:
        endpoint.start()
        print(
            json.dumps(
                {
                    "schema": PERSISTENT_ENDPOINT_EVENT_SCHEMA,
                    "event": "endpoint_ready",
                    "endpoint_instance_id": endpoint.endpoint_instance_id,
                    "control_socket": os.fspath(endpoint.control_socket),
                    "gcs_bind": list(endpoint.bound_gcs),
                },
                sort_keys=True,
            ),
            flush=True,
        )
        endpoint.serve_forever()
        return 0
    except KeyboardInterrupt:
        return 130
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"FAIL persistent M2 endpoint: {exc}", file=sys.stderr)
        return 2
    finally:
        endpoint.close()


def main(argv: list[str] | None = None) -> int:
    actual_argv = list(sys.argv[1:] if argv is None else argv)
    if actual_argv and actual_argv[0] in {"serve", "control"}:
        return persistent_main(actual_argv)
    args = parse_args(actual_argv)
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
        f"heartbeat_observation={result.heartbeat_observation_count} "
        f"telemetry={result.telemetry_responses} heartbeat_timeout={result.heartbeat_timeout}"
    )
    return 0 if phase_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
