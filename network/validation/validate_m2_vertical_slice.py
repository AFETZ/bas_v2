#!/usr/bin/env python3
"""Fail-closed validator for the one-UAV M2 external-packet vertical slice.

Raw evidence contract ``ams.m2.vertical_slice/v1``
==================================================

The validator intentionally does not consume producer ``passed``/``ack``
booleans or the general P0 summary.  It derives the M2 result from these sealed
raw files (all JSON records use ``schema_version: 2``):

* ``metrics/m2_run.json`` identifies ``run_id``, ``runtime_id``, ``run_nonce``
  and ``source_hash``;
* ``logs/m2_probe_events.jsonl`` contains ordered phase, command, decoded ACK,
  telemetry, and heartbeat events;
* ``logs/uav_adapter.jsonl`` contains the adapter's ordered frame hashes;
* ``logs/m2_process_events.jsonl`` contains Linux PID/start-tick snapshots;
* classic PCAP files contain exact UDP payloads at the four packet-path capture
  points;
* ``metrics/ns3_tap_build_receipt.json`` binds the TapBridge executable to the
  pinned ns-3 source, scratch input, module set, and build identity;
* ``metrics/m2_evidence_manifest.json`` seals every raw file by size and SHA256.

Probe nonces are exactly ``<run_nonce>:<phase>:<attempt>``.  Every attempt has
a MAVLink2 STATUSTEXT marker (which contains that nonce) and a separate command
frame.  A COMMAND_ACK is correlated to the command by request frame SHA,
decoded request sequence/command, target/source system, and event time; the
validator does not claim that COMMAND_ACK itself echoes a nonce.

Required probe event fields beyond the common envelope are:

``command_attempt``
    ``attempt``, ``nonce``, ``marker_sha256``, ``command_sha256``,
    ``mavlink_seq``, ``target_system``, ``target_component``,
    ``mavlink_command``.
``command_ack``
    ``attempt``, ``nonce``, ``request_sha256``, ``request_mavlink_seq``,
    ``packet_sha256``, ``source_system``, ``mavlink_command``,
    ``mavlink_result`` (zero means accepted).
``telemetry``
    ``attempt``, ``nonce``, ``request_sha256``, ``request_mavlink_seq``,
    ``packet_sha256``, ``source_system``, ``message_id``.
``heartbeat``
    ``packet_sha256`` and ``source_system``.
``heartbeat_timeout``
    finite ``timeout_s`` of at least one second.

Each JSONL common envelope is ``run_id``, ``runtime_id``, ``run_nonce``, a
contiguous one-based ``event_seq``, strictly increasing ``monotonic_ns``, and
UTC ``wall_utc``.  Probe/process records also name ``phase`` (``good``,
``down``, or ``recovery``).  Stable process roles are ``uav_adapter``,
``mavproxy``, and ``sitl``.  A phase-local ``gcs_probe`` may restart; ns-3 must
be alive in good/recovery, absent in down, and have a new identity on recovery.

Manifest contract ``ams.m2.vertical_slice.manifest/v1`` uses the same IDs plus
``source_hash``, ``sealed_utc``, and ``files`` entries containing ``sha256`` and
``size_bytes``.  Symlinks, unmanifested raw logs/PCAP, non-standard JSON NaN,
wrong scalar types, malformed/truncated PCAP, and critical runtime logs fail.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import struct
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from network.validation.evidence import ns3_build_receipt_evidence_status  # noqa: E402


SCHEMA_VERSION = 2
EVIDENCE_CONTRACT = "ams.m2.vertical_slice/v1"
MANIFEST_CONTRACT = "ams.m2.vertical_slice.manifest/v1"
PHASES = ("good", "down", "recovery")
EXPECTED_ATTEMPTS = {"good": 10, "down": 5, "recovery": 10}
CAPTURE_POINTS = ("gcs_ingress", "ns3_ingress", "ns3_egress", "uav_egress")
STABLE_PROCESS_ROLES = ("uav_adapter", "mavproxy", "sitl")
PHASE_PROCESS_ROLES = (*STABLE_PROCESS_ROLES, "gcs_probe", "ns3")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")
NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
CRITICAL_LOG_PATTERNS = (
    "operation not permitted",
    "permission denied",
    "traceback (most recent call last)",
    "segmentation fault",
    "core dumped",
    "address already in use",
    "bind failed",
    "bind error",
    "no such device",
    "process has died",
    "assertion failed",
    "fatal error",
)
CRITICAL_EVENT_TOKENS = ("failed", "failure", "crash", "fatal", "segfault")


def _is_int(value: Any) -> bool:
    return type(value) is int


def _is_number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(float(value))


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and HEX64_RE.fullmatch(value) is not None


def _strict_json_loads(text: str) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-standard JSON numeric constant {value!r}")

    return json.loads(text, parse_constant=reject_constant)


def _load_object(path: Path) -> tuple[dict[str, Any], list[str]]:
    if not path.is_file():
        return {}, [f"missing file: {path}"]
    if path.is_symlink():
        return {}, [f"raw evidence may not be a symlink: {path}"]
    try:
        value = _strict_json_loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        return {}, [f"invalid JSON object {path}: {exc}"]
    if not isinstance(value, dict):
        return {}, [f"expected a JSON object: {path}"]
    return value, []


def _parse_utc(value: Any) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _result(failures: Iterable[str], **details: Any) -> dict[str, Any]:
    items = list(failures)
    return {
        "status": "failed" if items else "passed",
        "failures": items,
        "details": details,
    }


def _metadata_gate(run_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    path = run_dir / "metrics/m2_run.json"
    data, failures = _load_object(path)
    if data:
        if data.get("schema_version") != SCHEMA_VERSION:
            failures.append("m2_run.schema_version must equal 2")
        if data.get("contract") != EVIDENCE_CONTRACT:
            failures.append(f"m2_run.contract must equal {EVIDENCE_CONTRACT!r}")
        if data.get("run_id") != run_dir.name:
            failures.append("m2_run.run_id does not match the run directory")
        runtime_id = data.get("runtime_id")
        if not isinstance(runtime_id, str) or ID_RE.fullmatch(runtime_id) is None:
            failures.append("m2_run.runtime_id has an invalid type or format")
        run_nonce = data.get("run_nonce")
        if not isinstance(run_nonce, str) or NONCE_RE.fullmatch(run_nonce) is None:
            failures.append("m2_run.run_nonce has an invalid type or format")
        if not _is_sha256(data.get("source_hash")):
            failures.append("m2_run.source_hash must be a lowercase SHA256")
        if not _parse_utc(data.get("started_utc")):
            failures.append("m2_run.started_utc must be an ISO-8601 UTC timestamp")
    return data, _result(failures, path=str(path))


def _common_record_failures(
    record: dict[str, Any],
    *,
    run_id: str,
    runtime_id: Any,
    run_nonce: Any,
    sequence: int,
    previous_monotonic_ns: int | None,
    require_phase: bool,
) -> list[str]:
    failures: list[str] = []
    if record.get("schema_version") != SCHEMA_VERSION:
        failures.append("schema_version is not 2")
    if record.get("run_id") != run_id:
        failures.append("run_id does not match")
    if record.get("runtime_id") != runtime_id:
        failures.append("runtime_id does not match")
    if record.get("run_nonce") != run_nonce:
        failures.append("run_nonce does not match")
    if record.get("event_seq") != sequence or not _is_int(record.get("event_seq")):
        failures.append(f"event_seq must be contiguous; expected {sequence}")
    monotonic_ns = record.get("monotonic_ns")
    if not _is_int(monotonic_ns) or monotonic_ns <= 0:
        failures.append("monotonic_ns must be a positive integer")
    elif previous_monotonic_ns is not None and monotonic_ns <= previous_monotonic_ns:
        failures.append("monotonic_ns is not strictly increasing")
    if not _parse_utc(record.get("wall_utc")):
        failures.append("wall_utc is not an ISO-8601 UTC timestamp")
    if not isinstance(record.get("event"), str) or not record.get("event"):
        failures.append("event must be a non-empty string")
    if require_phase and record.get("phase") not in PHASES:
        failures.append(f"phase must be one of {PHASES}")
    return failures


def _load_event_log(
    path: Path,
    metadata: dict[str, Any],
    *,
    require_phase: bool,
) -> tuple[list[dict[str, Any]], list[str]]:
    failures: list[str] = []
    records: list[dict[str, Any]] = []
    if not path.is_file():
        return records, [f"missing event log: {path}"]
    if path.is_symlink():
        return records, [f"event log may not be a symlink: {path}"]
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        return records, [f"cannot read event log {path}: {exc}"]
    if not lines:
        return records, [f"event log is empty: {path}"]
    previous_monotonic_ns: int | None = None
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            failures.append(f"{path}:{line_number}: blank JSONL record")
            continue
        try:
            value = _strict_json_loads(line)
        except (ValueError, json.JSONDecodeError) as exc:
            failures.append(f"{path}:{line_number}: invalid JSON: {exc}")
            continue
        if not isinstance(value, dict):
            failures.append(f"{path}:{line_number}: expected JSON object")
            continue
        common = _common_record_failures(
            value,
            run_id=path.parents[1].name,
            runtime_id=metadata.get("runtime_id"),
            run_nonce=metadata.get("run_nonce"),
            sequence=line_number,
            previous_monotonic_ns=previous_monotonic_ns,
            require_phase=require_phase,
        )
        failures.extend(f"{path}:{line_number}: {item}" for item in common)
        if _is_int(value.get("monotonic_ns")):
            previous_monotonic_ns = value["monotonic_ns"]
        records.append(value)
    return records, failures


def _phase_windows(records: list[dict[str, Any]]) -> tuple[dict[str, tuple[int, int]], list[str]]:
    failures: list[str] = []
    windows: dict[str, tuple[int, int]] = {}
    previous_end: int | None = None
    previous_start_seq = 0
    for phase in PHASES:
        starts = [record for record in records if record.get("event") == "phase_start" and record.get("phase") == phase]
        ends = [record for record in records if record.get("event") == "phase_end" and record.get("phase") == phase]
        if len(starts) != 1 or len(ends) != 1:
            failures.append(f"{phase}: expected exactly one phase_start and one phase_end")
            continue
        start_ns = starts[0].get("monotonic_ns")
        end_ns = ends[0].get("monotonic_ns")
        if not _is_int(start_ns) or not _is_int(end_ns) or start_ns >= end_ns:
            failures.append(f"{phase}: invalid phase monotonic interval")
            continue
        if starts[0].get("event_seq", 0) <= previous_start_seq:
            failures.append(f"{phase}: phase order is not good/down/recovery")
        if previous_end is not None and start_ns <= previous_end:
            failures.append(f"{phase}: phase interval overlaps the preceding phase")
        if ends[0].get("event_seq", 0) <= starts[0].get("event_seq", 0):
            failures.append(f"{phase}: phase_end precedes phase_start")
        windows[phase] = (start_ns, end_ns)
        previous_start_seq = starts[0].get("event_seq", 0)
        previous_end = end_ns
    interval_events = {
        "phase_start",
        "command_attempt",
        "command_ack",
        "telemetry",
        "heartbeat",
        "heartbeat_timeout",
        "command_result",
        "phase_end",
    }
    for record in records:
        phase = record.get("phase")
        if (
            phase not in windows
            or record.get("event") not in interval_events
            or not _is_int(record.get("monotonic_ns"))
        ):
            continue
        start_ns, end_ns = windows[phase]
        if not start_ns <= record["monotonic_ns"] <= end_ns:
            failures.append(
                f"event_seq {record.get('event_seq')} is outside its declared {phase} phase interval"
            )
    return windows, failures


def _valid_attempt_fields(record: dict[str, Any], phase: str, run_nonce: str, failures: list[str]) -> None:
    attempt = record.get("attempt")
    if not _is_int(attempt) or not 1 <= attempt <= EXPECTED_ATTEMPTS[phase]:
        failures.append(f"{phase}: command_attempt has invalid attempt {attempt!r}")
        return
    expected_nonce = f"{run_nonce}:{phase}:{attempt}"
    if record.get("nonce") != expected_nonce:
        failures.append(f"{phase}/{attempt}: nonce does not equal {expected_nonce!r}")
    for key in ("marker_sha256", "command_sha256"):
        if not _is_sha256(record.get(key)):
            failures.append(f"{phase}/{attempt}: {key} is not a SHA256")
    if record.get("marker_sha256") == record.get("command_sha256"):
        failures.append(f"{phase}/{attempt}: marker and command hashes are identical")
    if not _is_int(record.get("mavlink_seq")) or not 0 <= record.get("mavlink_seq", -1) <= 255:
        failures.append(f"{phase}/{attempt}: mavlink_seq is not an integer in 0..255")
    if record.get("target_system") != 1 or not _is_int(record.get("target_system")):
        failures.append(f"{phase}/{attempt}: target_system must be integer 1")
    if not _is_int(record.get("target_component")) or not 0 <= record.get("target_component", -1) <= 255:
        failures.append(f"{phase}/{attempt}: target_component is invalid")
    if not _is_int(record.get("mavlink_command")) or record.get("mavlink_command", -1) < 0:
        failures.append(f"{phase}/{attempt}: mavlink_command is invalid")


def _probe_gate(
    run_dir: Path, metadata: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, tuple[int, int]], dict[str, Any]]:
    path = run_dir / "logs/m2_probe_events.jsonl"
    records, failures = _load_event_log(path, metadata, require_phase=True)
    windows, window_failures = _phase_windows(records)
    failures.extend(window_failures)
    phase_evidence: dict[str, Any] = {}
    run_nonce = metadata.get("run_nonce") if isinstance(metadata.get("run_nonce"), str) else ""

    for phase in PHASES:
        expected = EXPECTED_ATTEMPTS[phase]
        attempts_list = [
            record for record in records if record.get("phase") == phase and record.get("event") == "command_attempt"
        ]
        attempts: dict[int, dict[str, Any]] = {}
        for record in attempts_list:
            _valid_attempt_fields(record, phase, run_nonce, failures)
            attempt = record.get("attempt")
            if _is_int(attempt):
                if attempt in attempts:
                    failures.append(f"{phase}/{attempt}: duplicate command_attempt")
                else:
                    attempts[attempt] = record
        if set(attempts) != set(range(1, expected + 1)):
            failures.append(
                f"{phase}: command attempts are {sorted(attempts)}, expected 1..{expected}"
            )
        marker_hashes = [record.get("marker_sha256") for record in attempts.values()]
        command_hashes = [record.get("command_sha256") for record in attempts.values()]
        if len(marker_hashes) != len(set(marker_hashes)):
            failures.append(f"{phase}: nonce marker hashes are not unique")
        if len(command_hashes) != len(set(command_hashes)):
            failures.append(f"{phase}: command frame hashes are not unique")

        ack_records = [
            record for record in records if record.get("phase") == phase and record.get("event") == "command_ack"
        ]
        telemetry_records = [
            record for record in records if record.get("phase") == phase and record.get("event") == "telemetry"
        ]
        heartbeats = [
            record for record in records if record.get("phase") == phase and record.get("event") == "heartbeat"
        ]
        timeouts = [
            record
            for record in records
            if record.get("phase") == phase and record.get("event") == "heartbeat_timeout"
        ]
        direct_probes = [
            record
            for record in records
            if record.get("phase") == phase and record.get("event") == "direct_endpoint_probe"
        ]
        expected_forbidden = {("127.0.0.1", 5760), ("10.72.1.1", 5760)}
        observed_forbidden: set[tuple[str, int]] = set()
        for record in direct_probes:
            endpoint = record.get("endpoint")
            if (
                not isinstance(endpoint, list)
                or len(endpoint) != 2
                or not isinstance(endpoint[0], str)
                or not _is_int(endpoint[1])
            ):
                failures.append(f"{phase}: direct endpoint probe has invalid endpoint")
                continue
            observed_forbidden.add((endpoint[0], endpoint[1]))
            if record.get("reachable") is not False:
                failures.append(f"{phase}: forbidden direct endpoint {endpoint} was reachable")
        if observed_forbidden != expected_forbidden or len(direct_probes) != len(expected_forbidden):
            failures.append(
                f"{phase}: forbidden endpoint probes are {sorted(observed_forbidden)}, expected {sorted(expected_forbidden)}"
            )

        endpoint_health = [
            record
            for record in records
            if record.get("phase") == phase and record.get("event") == "endpoint_health"
        ]
        if len(endpoint_health) != 1:
            failures.append(f"{phase}: expected exactly one endpoint_health record")
        else:
            health = endpoint_health[0]
            if health.get("all_live") is not True:
                failures.append(f"{phase}: endpoint_health.all_live is not true")
            expected_ns3_alive = phase != "down"
            if health.get("ns3_alive") is not expected_ns3_alive:
                failures.append(
                    f"{phase}: endpoint_health.ns3_alive is not {expected_ns3_alive}"
                )
            live_roles = set(health.get("endpoint_roles") or [])
            required_live_roles = {"uav_adapter", "mavproxy", "sitl", "gcs_probe"}
            if not required_live_roles.issubset(live_roles):
                failures.append(
                    f"{phase}: endpoint_health lacks live roles {sorted(required_live_roles - live_roles)}"
                )

        acknowledgements: dict[int, dict[str, Any]] = {}
        for record in ack_records:
            attempt = record.get("attempt")
            attempt_record = attempts.get(attempt) if _is_int(attempt) else None
            if attempt_record is None:
                failures.append(f"{phase}: ACK references unknown attempt {attempt!r}")
                continue
            if attempt in acknowledgements:
                failures.append(f"{phase}/{attempt}: duplicate COMMAND_ACK")
                continue
            acknowledgements[attempt] = record
            expected_nonce = f"{run_nonce}:{phase}:{attempt}"
            if record.get("nonce") != expected_nonce:
                failures.append(f"{phase}/{attempt}: ACK envelope nonce mismatch")
            if record.get("request_sha256") != attempt_record.get("command_sha256"):
                failures.append(f"{phase}/{attempt}: ACK request SHA does not match command frame")
            if record.get("request_mavlink_seq") != attempt_record.get("mavlink_seq") or not _is_int(
                record.get("request_mavlink_seq")
            ):
                failures.append(f"{phase}/{attempt}: ACK request sequence mismatch")
            if record.get("mavlink_command") != attempt_record.get("mavlink_command") or not _is_int(
                record.get("mavlink_command")
            ):
                failures.append(f"{phase}/{attempt}: ACK command mismatch")
            if record.get("mavlink_result") != 0 or not _is_int(record.get("mavlink_result")):
                failures.append(f"{phase}/{attempt}: ACK result is not MAV_RESULT_ACCEPTED (0)")
            if record.get("source_system") != 1 or not _is_int(record.get("source_system")):
                failures.append(f"{phase}/{attempt}: ACK source_system must be integer 1")
            if not _is_sha256(record.get("packet_sha256")):
                failures.append(f"{phase}/{attempt}: ACK packet_sha256 is invalid")
            if _is_int(record.get("monotonic_ns")) and _is_int(attempt_record.get("monotonic_ns")):
                if record["monotonic_ns"] <= attempt_record["monotonic_ns"]:
                    failures.append(f"{phase}/{attempt}: ACK does not follow the command")

        telemetry_by_attempt: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for record in telemetry_records:
            attempt = record.get("attempt")
            attempt_record = attempts.get(attempt) if _is_int(attempt) else None
            if attempt_record is None:
                failures.append(f"{phase}: telemetry references unknown attempt {attempt!r}")
                continue
            telemetry_by_attempt[attempt].append(record)
            expected_nonce = f"{run_nonce}:{phase}:{attempt}"
            if record.get("nonce") != expected_nonce:
                failures.append(f"{phase}/{attempt}: telemetry envelope nonce mismatch")
            if record.get("request_sha256") != attempt_record.get("command_sha256"):
                failures.append(f"{phase}/{attempt}: telemetry request SHA mismatch")
            if record.get("request_mavlink_seq") != attempt_record.get("mavlink_seq") or not _is_int(
                record.get("request_mavlink_seq")
            ):
                failures.append(f"{phase}/{attempt}: telemetry request sequence mismatch")
            if record.get("source_system") != 1 or not _is_int(record.get("source_system")):
                failures.append(f"{phase}/{attempt}: telemetry source_system must be integer 1")
            if not _is_int(record.get("message_id")) or record.get("message_id", -1) < 0:
                failures.append(f"{phase}/{attempt}: telemetry message_id is invalid")
            if not _is_sha256(record.get("packet_sha256")):
                failures.append(f"{phase}/{attempt}: telemetry packet_sha256 is invalid")
            if _is_int(record.get("monotonic_ns")) and _is_int(attempt_record.get("monotonic_ns")):
                if record["monotonic_ns"] <= attempt_record["monotonic_ns"]:
                    failures.append(f"{phase}/{attempt}: telemetry does not follow the command")

        for heartbeat in heartbeats:
            if not _is_sha256(heartbeat.get("packet_sha256")):
                failures.append(f"{phase}: heartbeat packet_sha256 is invalid")
            if heartbeat.get("source_system") != 1 or not _is_int(heartbeat.get("source_system")):
                failures.append(f"{phase}: heartbeat source_system must be integer 1")

        if phase in ("good", "recovery"):
            if set(acknowledgements) != set(range(1, expected + 1)):
                failures.append(
                    f"{phase}: ACK attempts are {sorted(acknowledgements)}, expected 1..{expected}"
                )
            missing_telemetry = [attempt for attempt in range(1, expected + 1) if not telemetry_by_attempt[attempt]]
            if missing_telemetry:
                failures.append(f"{phase}: telemetry missing for attempts {missing_telemetry}")
            if not heartbeats:
                failures.append(f"{phase}: no decoded heartbeat was observed")
            if timeouts:
                failures.append(f"{phase}: unexpected heartbeat_timeout event")
        else:
            if ack_records:
                failures.append(f"down: observed {len(ack_records)} COMMAND_ACK event(s), expected zero")
            if telemetry_records:
                failures.append(f"down: observed {len(telemetry_records)} telemetry event(s), expected zero")
            if heartbeats:
                failures.append(f"down: observed {len(heartbeats)} heartbeat event(s), expected zero")
            if len(timeouts) != 1:
                failures.append("down: expected exactly one heartbeat_timeout")
            elif not _is_number(timeouts[0].get("timeout_s")) or float(timeouts[0]["timeout_s"]) < 1.0:
                failures.append("down: heartbeat timeout_s must be finite and at least 1 second")
            elif attempts and _is_int(timeouts[0].get("monotonic_ns")):
                last_attempt_ns = max(
                    record.get("monotonic_ns", 0)
                    for record in attempts.values()
                    if _is_int(record.get("monotonic_ns"))
                )
                if timeouts[0]["monotonic_ns"] <= last_attempt_ns:
                    failures.append("down: heartbeat timeout was recorded before the final attempt")

        phase_evidence[phase] = {
            "attempts": attempts,
            "acks": acknowledgements,
            "telemetry": dict(telemetry_by_attempt),
            "heartbeats": heartbeats,
            "timeouts": timeouts,
            "direct_probes": direct_probes,
            "endpoint_health": endpoint_health,
        }

    details = {
        phase: {
            "attempts": len(value["attempts"]),
            "acks": len(value["acks"]),
            "telemetry_attempts": len([key for key, rows in value["telemetry"].items() if rows]),
            "heartbeats": len(value["heartbeats"]),
            "timeouts": len(value["timeouts"]),
        }
        for phase, value in phase_evidence.items()
    }
    return _result(failures, phases=details), windows, phase_evidence


def _extract_udp_payload(frame: bytes, linktype: int) -> bytes | None:
    if linktype != 1:  # Captures in this contract are Ethernet/TAP captures.
        raise ValueError(f"unsupported PCAP linktype {linktype}; expected Ethernet (1)")
    if len(frame) < 14:
        raise ValueError("truncated Ethernet frame")
    ether_type = struct.unpack("!H", frame[12:14])[0]
    offset = 14
    while ether_type in (0x8100, 0x88A8, 0x9100):
        if len(frame) < offset + 4:
            raise ValueError("truncated VLAN header")
        ether_type = struct.unpack("!H", frame[offset + 2 : offset + 4])[0]
        offset += 4
    if ether_type != 0x0800:
        return None
    if len(frame) < offset + 20:
        raise ValueError("truncated IPv4 header")
    version = frame[offset] >> 4
    ihl = (frame[offset] & 0x0F) * 4
    if version != 4 or ihl < 20 or len(frame) < offset + ihl:
        raise ValueError("invalid IPv4 header")
    total_length = struct.unpack("!H", frame[offset + 2 : offset + 4])[0]
    if total_length < ihl or len(frame) < offset + total_length:
        raise ValueError("truncated or invalid IPv4 total length")
    flags_fragment = struct.unpack("!H", frame[offset + 6 : offset + 8])[0]
    if flags_fragment & 0x3FFF:
        raise ValueError("fragmented IPv4 packets are not accepted as M2 proof")
    if frame[offset + 9] != 17:
        return None
    udp_offset = offset + ihl
    if total_length < ihl + 8 or len(frame) < udp_offset + 8:
        raise ValueError("truncated UDP header")
    udp_length = struct.unpack("!H", frame[udp_offset + 4 : udp_offset + 6])[0]
    if udp_length < 8 or udp_length > total_length - ihl:
        raise ValueError("invalid UDP length")
    return frame[udp_offset + 8 : udp_offset + udp_length]


def _parse_pcap(path: Path) -> tuple[dict[str, Any], list[str]]:
    failures: list[str] = []
    payloads: list[bytes] = []
    packet_count = 0
    if not path.is_file():
        return {}, [f"missing PCAP: {path}"]
    if path.is_symlink():
        return {}, [f"PCAP may not be a symlink: {path}"]
    try:
        with path.open("rb") as handle:
            header = handle.read(24)
            if len(header) != 24:
                raise ValueError("missing classic-PCAP global header")
            magic = header[:4]
            if magic in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1"):
                endian = "<"
            elif magic in (b"\xa1\xb2\xc3\xd4", b"\xa1\xb2\x3c\x4d"):
                endian = ">"
            elif magic == b"\x0a\x0d\x0d\x0a":
                raise ValueError("pcapng is not accepted; use classic PCAP")
            else:
                raise ValueError(f"unknown PCAP magic {magic.hex()}")
            major, minor = struct.unpack(endian + "HH", header[4:8])
            if (major, minor) != (2, 4):
                raise ValueError(f"unsupported PCAP version {major}.{minor}")
            linktype = struct.unpack(endian + "I", header[20:24])[0]
            previous_timestamp: tuple[int, int] | None = None
            while True:
                packet_header = handle.read(16)
                if not packet_header:
                    break
                if len(packet_header) != 16:
                    raise ValueError("truncated PCAP packet header")
                ts_sec, ts_fraction, captured_len, original_len = struct.unpack(
                    endian + "IIII", packet_header
                )
                if captured_len > 64 * 1024 * 1024 or original_len < captured_len:
                    raise ValueError("invalid PCAP captured/original length")
                frame = handle.read(captured_len)
                if len(frame) != captured_len:
                    raise ValueError("truncated PCAP packet data")
                timestamp = (ts_sec, ts_fraction)
                if previous_timestamp is not None and timestamp < previous_timestamp:
                    raise ValueError("PCAP packet timestamps are not monotonic")
                previous_timestamp = timestamp
                packet_count += 1
                payload = _extract_udp_payload(frame, linktype)
                if payload is not None:
                    payloads.append(payload)
    except (OSError, ValueError, struct.error) as exc:
        failures.append(f"{path}: {exc}")
    hashes = Counter(hashlib.sha256(payload).hexdigest() for payload in payloads)
    payload_by_hash: dict[str, list[bytes]] = defaultdict(list)
    for payload in payloads:
        payload_by_hash[hashlib.sha256(payload).hexdigest()].append(payload)
    return {
        "path": str(path),
        "file_sha256": _sha256_file(path) if path.is_file() else None,
        "packet_count": packet_count,
        "udp_payload_count": len(payloads),
        "payload_hashes": hashes,
        "payload_by_hash": dict(payload_by_hash),
    }, failures


def _required_payloads(evidence: dict[str, Any]) -> tuple[Counter[str], dict[str, str]]:
    # One UDP datagram may contain multiple MAVLink frames.  In that case the
    # probe emits (for example) both COMMAND_ACK and telemetry decode records
    # with the same exact datagram SHA.  Count each unique datagram hash once;
    # summing semantic messages would demand duplicate packets that never
    # existed.  Per-attempt ACK/telemetry cardinality is independently enforced
    # by the ordered probe events above.
    required_hashes: set[str] = set()
    marker_nonces: dict[str, str] = {}
    for attempt in evidence.get("attempts", {}).values():
        marker_hash = attempt.get("marker_sha256")
        command_hash = attempt.get("command_sha256")
        if _is_sha256(marker_hash):
            required_hashes.add(marker_hash)
            marker_nonces[marker_hash] = str(attempt.get("nonce", ""))
        if _is_sha256(command_hash):
            required_hashes.add(command_hash)
    for ack in evidence.get("acks", {}).values():
        if _is_sha256(ack.get("packet_sha256")):
            required_hashes.add(ack["packet_sha256"])
    for rows in evidence.get("telemetry", {}).values():
        for telemetry in rows:
            if _is_sha256(telemetry.get("packet_sha256")):
                required_hashes.add(telemetry["packet_sha256"])
    for heartbeat in evidence.get("heartbeats", []):
        if _is_sha256(heartbeat.get("packet_sha256")):
            required_hashes.add(heartbeat["packet_sha256"])
    return Counter({payload_hash: 1 for payload_hash in required_hashes}), marker_nonces


def _pcap_gate(run_dir: Path, phase_evidence: dict[str, Any]) -> dict[str, Any]:
    failures: list[str] = []
    stats: dict[str, dict[str, Any]] = {}
    phase_file_hashes: dict[str, list[str]] = defaultdict(list)
    for phase in ("good", "recovery"):
        required, marker_nonces = _required_payloads(phase_evidence.get(phase, {}))
        for point in CAPTURE_POINTS:
            relative = f"pcap/{point}_{phase}.pcap"
            parsed, parse_failures = _parse_pcap(run_dir / relative)
            failures.extend(parse_failures)
            stats[relative] = {
                key: value for key, value in parsed.items() if key not in {"payload_hashes", "payload_by_hash"}
            }
            observed: Counter[str] = parsed.get("payload_hashes", Counter())
            for payload_hash, count in required.items():
                if observed[payload_hash] < count:
                    failures.append(
                        f"{relative}: payload {payload_hash} observed {observed[payload_hash]} time(s), expected at least {count}"
                    )
            for marker_hash, nonce in marker_nonces.items():
                marker_payloads = parsed.get("payload_by_hash", {}).get(marker_hash, [])
                if marker_payloads and not any(nonce.encode("utf-8") in payload for payload in marker_payloads):
                    failures.append(f"{relative}: marker payload {marker_hash} does not contain its nonce")
            if parsed.get("file_sha256"):
                phase_file_hashes[phase].append(parsed["file_sha256"])
        if len(phase_file_hashes[phase]) == len(CAPTURE_POINTS) and len(set(phase_file_hashes[phase])) != len(
            CAPTURE_POINTS
        ):
            failures.append(f"{phase}: capture-point PCAP files are byte-identical; copied evidence is not accepted")

    down_required, down_markers = _required_payloads(phase_evidence.get("down", {}))
    # Down has no ACK/telemetry/heartbeat records, so this counter contains only
    # the nonce marker and command frame for each attempted transaction.
    for point in ("gcs_ingress", "uav_egress"):
        relative = f"pcap/{point}_down.pcap"
        parsed, parse_failures = _parse_pcap(run_dir / relative)
        failures.extend(parse_failures)
        stats[relative] = {
            key: value for key, value in parsed.items() if key not in {"payload_hashes", "payload_by_hash"}
        }
        observed: Counter[str] = parsed.get("payload_hashes", Counter())
        if point == "gcs_ingress":
            for payload_hash, count in down_required.items():
                if observed[payload_hash] < count:
                    failures.append(
                        f"{relative}: down-attempt payload {payload_hash} observed {observed[payload_hash]} time(s), expected {count}"
                    )
            for marker_hash, nonce in down_markers.items():
                payloads = parsed.get("payload_by_hash", {}).get(marker_hash, [])
                if payloads and not any(nonce.encode("utf-8") in payload for payload in payloads):
                    failures.append(f"{relative}: down marker {marker_hash} does not contain its nonce")
        else:
            leaked = {payload_hash: observed[payload_hash] for payload_hash in down_required if observed[payload_hash]}
            if leaked:
                failures.append(f"{relative}: down-attempt payloads reached the UAV side: {leaked}")
    return _result(failures, pcaps=stats)


def _adapter_gate(
    run_dir: Path,
    metadata: dict[str, Any],
    windows: dict[str, tuple[int, int]],
    phase_evidence: dict[str, Any],
) -> tuple[dict[str, Any], int | None]:
    path = run_dir / "logs/uav_adapter.jsonl"
    records, failures = _load_event_log(path, metadata, require_phase=False)
    starts = [record for record in records if record.get("event") == "adapter_start"]
    stops = [record for record in records if record.get("event") == "adapter_stop"]
    adapter_pid: int | None = None
    if len(starts) != 1:
        failures.append("adapter log must contain exactly one adapter_start")
    else:
        adapter_pid = starts[0].get("pid") if _is_int(starts[0].get("pid")) and starts[0]["pid"] > 0 else None
        if adapter_pid is None:
            failures.append("adapter_start.pid must be a positive integer")
        if "good" in windows and _is_int(starts[0].get("monotonic_ns")):
            if starts[0]["monotonic_ns"] >= windows["good"][0]:
                failures.append("adapter did not start before the good phase")
    if len(stops) != 1:
        failures.append("adapter log must contain exactly one adapter_stop")
    elif "recovery" in windows and _is_int(stops[0].get("monotonic_ns")):
        if stops[0]["monotonic_ns"] <= windows["recovery"][1]:
            failures.append("adapter stopped before the recovery phase completed")

    forward_by_phase: dict[str, dict[str, Counter[str]]] = {
        phase: {"gcs_to_tail": Counter(), "tail_to_gcs": Counter()} for phase in PHASES
    }
    counted = {
        "gcs_to_tail": 0,
        "tail_to_gcs": 0,
        "dropped_no_peer": 0,
        "dropped_unexpected_peer": 0,
    }
    for record in records:
        event = record.get("event")
        if event == "drop":
            reason = record.get("reason")
            if reason == "mavproxy_peer_unknown":
                counted["dropped_no_peer"] += 1
            elif reason in ("unexpected_tail_peer", "unexpected_gcs_peer"):
                counted["dropped_unexpected_peer"] += 1
            failures.append(f"adapter drop event_seq {record.get('event_seq')} is not accepted")
        if event != "forward":
            continue
        direction = record.get("direction")
        if direction not in ("gcs_to_tail", "tail_to_gcs"):
            failures.append(f"adapter forward has invalid direction {direction!r}")
            continue
        payload_hash = record.get("sha256")
        if not _is_sha256(payload_hash):
            failures.append(f"adapter forward event_seq {record.get('event_seq')} has invalid SHA256")
            continue
        if not _is_int(record.get("bytes")) or record.get("bytes", 0) <= 0:
            failures.append(f"adapter forward event_seq {record.get('event_seq')} has invalid byte count")
        counted[direction] += 1
        monotonic_ns = record.get("monotonic_ns")
        matching_phases = [
            phase
            for phase, (start_ns, end_ns) in windows.items()
            if _is_int(monotonic_ns) and start_ns <= monotonic_ns <= end_ns
        ]
        if len(matching_phases) == 1:
            forward_by_phase[matching_phases[0]][direction][payload_hash] += 1

    for phase in ("good", "recovery"):
        required, _marker_nonces = _required_payloads(phase_evidence.get(phase, {}))
        request_required: Counter[str] = Counter()
        response_required: Counter[str] = Counter()
        evidence = phase_evidence.get(phase, {})
        for attempt in evidence.get("attempts", {}).values():
            for key in ("marker_sha256", "command_sha256"):
                if _is_sha256(attempt.get(key)):
                    request_required[attempt[key]] += 1
        response_required = required - request_required
        for payload_hash, count in request_required.items():
            observed = forward_by_phase[phase]["gcs_to_tail"][payload_hash]
            if observed < count:
                failures.append(
                    f"adapter/{phase}: request payload {payload_hash} forwarded {observed} time(s), expected {count}"
                )
        for payload_hash, count in response_required.items():
            observed = forward_by_phase[phase]["tail_to_gcs"][payload_hash]
            if observed < count:
                failures.append(
                    f"adapter/{phase}: response payload {payload_hash} forwarded {observed} time(s), expected {count}"
                )

    down_evidence = phase_evidence.get("down", {})
    down_request_hashes = {
        value
        for attempt in down_evidence.get("attempts", {}).values()
        for value in (attempt.get("marker_sha256"), attempt.get("command_sha256"))
        if _is_sha256(value)
    }
    leaked = {
        payload_hash: forward_by_phase["down"]["gcs_to_tail"][payload_hash]
        for payload_hash in down_request_hashes
        if forward_by_phase["down"]["gcs_to_tail"][payload_hash]
    }
    if leaked:
        failures.append(f"adapter/down: command payloads crossed the stopped ns-3 path: {leaked}")

    if stops:
        stop_counters = stops[0].get("counters")
        if not isinstance(stop_counters, dict):
            failures.append("adapter_stop.counters is missing")
        else:
            for key, observed in counted.items():
                if stop_counters.get(key) != observed or not _is_int(stop_counters.get(key)):
                    failures.append(
                        f"adapter_stop counter {key}={stop_counters.get(key)!r}, independently counted {observed}"
                    )
    details = {
        phase: {direction: sum(counter.values()) for direction, counter in directions.items()}
        for phase, directions in forward_by_phase.items()
    }
    return _result(failures, forwards=details), adapter_pid


def _process_gate(
    run_dir: Path,
    metadata: dict[str, Any],
    windows: dict[str, tuple[int, int]],
    adapter_pid: int | None,
) -> dict[str, Any]:
    path = run_dir / "logs/m2_process_events.jsonl"
    records, failures = _load_event_log(path, metadata, require_phase=True)
    snapshots: dict[str, dict[str, list[dict[str, Any]]]] = {
        phase: {role: [] for role in PHASE_PROCESS_ROLES} for phase in PHASES
    }
    for record in records:
        if record.get("event") != "process_snapshot":
            continue
        phase = record.get("phase")
        role = record.get("role")
        if phase not in PHASES or role not in PHASE_PROCESS_ROLES:
            failures.append(f"process_snapshot has unknown phase/role: {phase!r}/{role!r}")
            continue
        monotonic_ns = record.get("monotonic_ns")
        # The probe samples /proc before and after the transaction window, then
        # appends the consolidated snapshot immediately after phase_end.  Event
        # ordering and cross-phase identity are authoritative here; requiring
        # the append timestamp to lie inside the transaction window would
        # reject that intentional after-sample.
        alive = record.get("alive")
        if type(alive) is not bool:
            failures.append(f"{phase}/{role}: alive must be a JSON boolean")
        if alive is True:
            if not _is_int(record.get("pid")) or record.get("pid", 0) <= 0:
                failures.append(f"{phase}/{role}: live PID must be a positive integer")
            if not _is_int(record.get("start_ticks")) or record.get("start_ticks", 0) <= 0:
                failures.append(f"{phase}/{role}: live start_ticks must be a positive integer")
            if not _is_sha256(record.get("cmdline_sha256")):
                failures.append(f"{phase}/{role}: live cmdline_sha256 is invalid")
        snapshots[phase][role].append(record)

    for phase in PHASES:
        for role in PHASE_PROCESS_ROLES:
            if not snapshots[phase][role]:
                failures.append(f"{phase}: no process_snapshot for {role}")

    stable_identities: dict[str, tuple[Any, Any, Any]] = {}
    for role in STABLE_PROCESS_ROLES:
        identities = set()
        for phase in PHASES:
            for record in snapshots[phase][role]:
                if record.get("alive") is not True:
                    failures.append(f"{phase}/{role}: stable process is not alive")
                identities.add((record.get("pid"), record.get("start_ticks"), record.get("cmdline_sha256")))
        if len(identities) != 1:
            failures.append(f"{role}: PID/start_ticks/cmdline identity changed across phases")
        elif identities:
            stable_identities[role] = next(iter(identities))

    for phase in PHASES:
        gcs_identities = {
            (record.get("pid"), record.get("start_ticks"), record.get("cmdline_sha256"))
            for record in snapshots[phase]["gcs_probe"]
            if record.get("alive") is True
        }
        if not gcs_identities:
            failures.append(f"{phase}/gcs_probe: no live phase-local probe")
        elif len(gcs_identities) != 1:
            failures.append(f"{phase}/gcs_probe: identity changed within the phase")

    ns3_identity: dict[str, set[tuple[Any, Any, Any]]] = {}
    for phase in PHASES:
        records_for_phase = snapshots[phase]["ns3"]
        if phase == "down":
            if any(record.get("alive") is not False for record in records_for_phase):
                failures.append("down/ns3: ns-3 was not consistently absent")
            for record in records_for_phase:
                if not _is_int(record.get("pid")) or record.get("pid", 0) <= 0:
                    failures.append("down/ns3: stopped identity PID is invalid")
                if not _is_int(record.get("start_ticks")) or record.get("start_ticks", 0) <= 0:
                    failures.append("down/ns3: stopped identity start_ticks is invalid")
                if not _is_sha256(record.get("cmdline_sha256")):
                    failures.append("down/ns3: stopped identity cmdline_sha256 is invalid")
            continue
        identities = {
            (record.get("pid"), record.get("start_ticks"), record.get("cmdline_sha256"))
            for record in records_for_phase
            if record.get("alive") is True
        }
        if len(identities) != 1 or any(record.get("alive") is not True for record in records_for_phase):
            failures.append(f"{phase}/ns3: expected one consistently live identity")
        ns3_identity[phase] = identities
    if ns3_identity.get("good") and ns3_identity.get("recovery"):
        if ns3_identity["good"] == ns3_identity["recovery"]:
            failures.append("ns3 recovery reused the same PID/start_ticks identity; restart is not proven")
    if ns3_identity.get("good") and snapshots["down"]["ns3"]:
        down_identities = {
            (record.get("pid"), record.get("start_ticks"), record.get("cmdline_sha256"))
            for record in snapshots["down"]["ns3"]
        }
        if down_identities != ns3_identity["good"]:
            failures.append("down/ns3: stopped identity does not match the good-phase ns-3 process")

    if adapter_pid is not None and "uav_adapter" in stable_identities:
        if stable_identities["uav_adapter"][0] != adapter_pid:
            failures.append("uav_adapter process snapshot PID does not match adapter_start.pid")
    return _result(failures, stable_identities={key: list(value) for key, value in stable_identities.items()})


def _critical_logs_gate(run_dir: Path, event_records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    failures: list[str] = []
    runner_log = run_dir / "logs/m2_runner.log"
    if not runner_log.is_file() or runner_log.stat().st_size == 0:
        failures.append("logs/m2_runner.log is missing or empty")
    scanned: list[str] = []
    for path in sorted((run_dir / "logs").rglob("*.log")):
        if path.is_symlink():
            failures.append(f"critical log may not be a symlink: {path.relative_to(run_dir)}")
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="strict").lower()
        except (OSError, UnicodeError) as exc:
            failures.append(f"cannot read critical log {path.relative_to(run_dir)}: {exc}")
            continue
        scanned.append(path.relative_to(run_dir).as_posix())
        for pattern in CRITICAL_LOG_PATTERNS:
            if pattern in text:
                failures.append(f"{path.relative_to(run_dir)} contains critical pattern {pattern!r}")
    for record in event_records:
        event = str(record.get("event", "")).lower()
        if any(token in event for token in CRITICAL_EVENT_TOKENS):
            failures.append(f"event_seq {record.get('event_seq')} reports critical event {event!r}")
        error = record.get("error")
        expected_connect_failure = (
            record.get("event") == "direct_endpoint_probe" and record.get("reachable") is False
        )
        if error not in (None, "", [], {}) and not expected_connect_failure:
            failures.append(f"event_seq {record.get('event_seq')} contains a non-empty error field")
    return _result(failures, scanned_logs=scanned)


def _provenance_gate(run_dir: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    path = run_dir / "metrics/provenance.json"
    data, failures = _load_object(path)
    if not data:
        return _result(failures, path=str(path))
    if data.get("schema_version") != 2:
        failures.append("provenance.schema_version must equal 2")
    if data.get("run_id") != run_dir.name:
        failures.append("provenance.run_id does not match run directory")
    if data.get("source_hash") != metadata.get("source_hash") or not _is_sha256(data.get("source_hash")):
        failures.append("provenance.source_hash does not match m2_run.source_hash")
    if data.get("git_dirty") is not False:
        failures.append("provenance records a dirty source checkout")
    commit = data.get("git_commit")
    if not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        failures.append("provenance.git_commit is not a full lowercase Git SHA")
    container = data.get("container_image") if isinstance(data.get("container_image"), dict) else {}
    digest = container.get("digest")
    if not isinstance(digest, str) or digest in ("", "unknown", "UNPINNED_BLOCKER") or len(digest) < 12:
        failures.append("provenance container image digest is not pinned")
    implementation = data.get("implementation") if isinstance(data.get("implementation"), dict) else {}
    if implementation.get("packet_ingress_mode") != "tap_bridge_external":
        failures.append("provenance packet_ingress_mode is not tap_bridge_external")

    scenario_relative = "network/config/scenario_1uav_vertical_slice.yaml"
    scenario_path = ROOT_DIR / scenario_relative
    config_hashes = data.get("config_hashes") if isinstance(data.get("config_hashes"), dict) else {}
    expected_scenario_hash = _sha256_file(scenario_path) if scenario_path.is_file() else None
    if config_hashes.get(scenario_relative) != expected_scenario_hash:
        failures.append("provenance scenario config hash does not match the current checkout")

    source_manifest = data.get("source_manifest") if isinstance(data.get("source_manifest"), dict) else {}
    current_sources = (
        "network/validation/validate_m2_vertical_slice.py",
        "network/bridge/uav_mavlink_endpoint.py",
        "network/ns3/scratch/ams-tap-vertical-slice.cc",
        "network/scripts/setup_one_uav_netns.sh",
        "network/ns3/run_ns3_tap_slice.sh",
        "network/scripts/run_one_uav_vertical_slice.sh",
        "network/tests/mavlink_vertical_slice_probe.py",
    )
    for relative in current_sources:
        current = ROOT_DIR / relative
        expected = _sha256_file(current) if current.is_file() else None
        if source_manifest.get(relative) != expected:
            failures.append(f"provenance source hash is absent/stale for {relative}")
    return _result(failures, path=str(path))


def _manifest_gate(run_dir: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    path = run_dir / "metrics/m2_evidence_manifest.json"
    data, failures = _load_object(path)
    if not data:
        return _result(failures, path=str(path))
    if data.get("schema_version") != 2:
        failures.append("M2 manifest schema_version must equal 2")
    if data.get("contract") != MANIFEST_CONTRACT:
        failures.append(f"M2 manifest contract must equal {MANIFEST_CONTRACT!r}")
    for key in ("run_id", "runtime_id", "run_nonce", "source_hash"):
        if data.get(key) != metadata.get(key):
            failures.append(f"M2 manifest {key} does not match m2_run")
    if not _parse_utc(data.get("sealed_utc")):
        failures.append("M2 manifest sealed_utc is invalid")
    files = data.get("files") if isinstance(data.get("files"), dict) else {}
    if not files:
        failures.append("M2 manifest files map is missing or empty")

    required = {
        "metrics/m2_run.json",
        "metrics/provenance.json",
        "metrics/ns3_tap_build_receipt.json",
        "logs/m2_probe_events.jsonl",
        "logs/uav_adapter.jsonl",
        "logs/m2_process_events.jsonl",
        "logs/m2_runner.log",
        *(f"pcap/{point}_{phase}.pcap" for phase in ("good", "recovery") for point in CAPTURE_POINTS),
        "pcap/gcs_ingress_down.pcap",
        "pcap/uav_egress_down.pcap",
    }
    missing = sorted(required - set(files))
    if missing:
        failures.append(f"M2 manifest lacks required raw files: {missing}")

    discovered: set[str] = set()
    for root, suffixes in ((run_dir / "logs", {".log", ".jsonl"}), (run_dir / "pcap", {".pcap"})):
        if root.is_dir():
            for candidate in root.rglob("*"):
                if candidate.is_file() and candidate.suffix in suffixes:
                    discovered.add(candidate.relative_to(run_dir).as_posix())
    unmanifested = sorted(discovered - set(files))
    if unmanifested:
        failures.append(f"unmanifested raw logs/PCAP are present: {unmanifested}")

    forbidden_outputs = {
        "metrics/m2_validation_results.json",
        "metrics/validation_results.json",
        "metrics/summary.json",
        "validation_report.md",
    }
    for relative, entry in files.items():
        if relative in forbidden_outputs:
            failures.append(f"validator output may not be sealed as raw evidence: {relative}")
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            failures.append(f"unsafe M2 manifest path: {relative!r}")
            continue
        candidate = run_dir / relative_path
        try:
            candidate.resolve().relative_to(run_dir)
        except (OSError, ValueError):
            failures.append(f"M2 manifest path escapes run directory: {relative!r}")
            continue
        if candidate.is_symlink():
            failures.append(f"manifested raw evidence may not be a symlink: {relative}")
            continue
        if not candidate.is_file():
            failures.append(f"manifested raw evidence is missing: {relative}")
            continue
        if (
            relative == "metrics/ns3_tap_build_receipt.json"
            and candidate.stat().st_mode & 0o222
        ):
            failures.append("manifested ns-3 build receipt remains writable")
        if not isinstance(entry, dict):
            failures.append(f"M2 manifest entry is not an object: {relative}")
            continue
        expected_size = entry.get("size_bytes")
        expected_hash = entry.get("sha256")
        actual_size = candidate.stat().st_size
        minimum_size = 1 if relative in required else 0
        if (
            not _is_int(expected_size)
            or expected_size < minimum_size
            or expected_size != actual_size
        ):
            failures.append(
                f"M2 manifest size mismatch for {relative}: manifest={expected_size!r}, actual={actual_size}"
            )
        if not _is_sha256(expected_hash):
            failures.append(f"M2 manifest SHA256 is invalid for {relative}")
        else:
            actual_hash = _sha256_file(candidate)
            if actual_hash != expected_hash:
                failures.append(f"M2 manifest SHA256 mismatch for {relative}")
    return _result(failures, files=len(files), discovered_raw=len(discovered))


def evaluate_m2_vertical_slice(run_dir: Path) -> dict[str, Any]:
    """Evaluate one M2 run without mutating it."""

    run_dir = run_dir.resolve()
    metadata, metadata_result = _metadata_gate(run_dir)
    gates: dict[str, dict[str, Any]] = {"metadata": metadata_result}
    all_event_records: list[dict[str, Any]] = []

    try:
        probe_result, windows, phase_evidence = _probe_gate(run_dir, metadata)
    except Exception as exc:  # Malformed evidence must fail closed, not crash the CLI.
        probe_result, windows, phase_evidence = _result([f"probe parser failed closed: {exc}"]), {}, {}
    gates["probe_transactions"] = probe_result
    try:
        records, _errors = _load_event_log(run_dir / "logs/m2_probe_events.jsonl", metadata, require_phase=True)
        all_event_records.extend(records)
    except Exception:
        pass

    try:
        gates["packet_captures"] = _pcap_gate(run_dir, phase_evidence)
    except Exception as exc:
        gates["packet_captures"] = _result([f"PCAP parser failed closed: {exc}"])

    try:
        adapter_result, adapter_pid = _adapter_gate(run_dir, metadata, windows, phase_evidence)
    except Exception as exc:
        adapter_result, adapter_pid = _result([f"adapter parser failed closed: {exc}"]), None
    gates["adapter_path"] = adapter_result
    try:
        records, _errors = _load_event_log(run_dir / "logs/uav_adapter.jsonl", metadata, require_phase=False)
        all_event_records.extend(records)
    except Exception:
        pass

    try:
        gates["process_identity"] = _process_gate(run_dir, metadata, windows, adapter_pid)
    except Exception as exc:
        gates["process_identity"] = _result([f"process parser failed closed: {exc}"])
    try:
        records, _errors = _load_event_log(
            run_dir / "logs/m2_process_events.jsonl", metadata, require_phase=True
        )
        all_event_records.extend(records)
    except Exception:
        pass

    try:
        gates["critical_logs"] = _critical_logs_gate(run_dir, all_event_records)
    except Exception as exc:
        gates["critical_logs"] = _result([f"critical-log parser failed closed: {exc}"])
    try:
        gates["ns3_build_receipt"] = ns3_build_receipt_evidence_status(run_dir)
    except Exception as exc:
        gates["ns3_build_receipt"] = _result(
            [f"ns-3 build-receipt parser failed closed: {exc}"]
        )
    try:
        gates["provenance"] = _provenance_gate(run_dir, metadata)
    except Exception as exc:
        gates["provenance"] = _result([f"provenance parser failed closed: {exc}"])
    try:
        gates["manifest"] = _manifest_gate(run_dir, metadata)
    except Exception as exc:
        gates["manifest"] = _result([f"manifest parser failed closed: {exc}"])

    passed = all(value.get("status") == "passed" for value in gates.values())
    return {
        "schema_version": 2,
        "validation_contract": EVIDENCE_CONTRACT,
        "run_id": run_dir.name,
        "runtime_id": metadata.get("runtime_id"),
        "passed": passed,
        "gates": gates,
    }


def _write_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args(argv)
    run_dir = args.run_dir.resolve()
    if not run_dir.is_dir():
        print(f"FAIL M2 run directory does not exist: {run_dir}", file=sys.stderr)
        return 2
    result = evaluate_m2_vertical_slice(run_dir)
    if args.json_output:
        output = args.json_output.resolve()
        fixed_output = run_dir / "metrics/m2_validation_results.json"
        if output != fixed_output:
            print(f"FAIL --json-output must be exactly {fixed_output}", file=sys.stderr)
            return 2
        _write_atomic(output, result)
        print(f"M2 validation results: {output}")
    for gate_name, gate_value in result["gates"].items():
        print(f"{gate_name}: {gate_value['status']}")
        for failure in gate_value.get("failures", []):
            print(f"  - {failure}")
    print(f"M2 passed: {str(result['passed']).lower()}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
