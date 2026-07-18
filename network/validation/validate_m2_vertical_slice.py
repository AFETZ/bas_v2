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
* ``logs/ns3_{good,recovery}_config.json`` and packet-event JSONL bind both
  live epochs to the shared ``ams-tap-packet-engine`` with ``uavCount=1``;
* classic PCAP files contain exact UDP payloads at the four packet-path capture
  points;
* ``metrics/ns3_tap_build_receipt.json`` binds the TapBridge executable to the
  pinned ns-3 source, scratch input, module set, and build identity;
* ``metrics/m2_endpoint_contract.json`` binds endpoint transaction schema v1
  and the exact ordered six-cell ``uav1`` matrix subset;
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
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import yaml


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

SCHEMA_VERSION = 2
EVIDENCE_CONTRACT = "ams.m2.vertical_slice/v1"
RESULT_CONTRACT = "ams.m2.vertical-slice-validation/v2"
MANIFEST_CONTRACT = "ams.m2.vertical_slice.manifest/v1"
ENDPOINT_SUBSET_CONTRACT = "ams.m2.endpoint_subset/v1"
ENDPOINT_TRANSACTION_CONTRACT = "endpoint_transaction_schema=1"
ENDPOINT_MATRIX_ID = "ams.endpoint_matrix.5uav/v1"
ENGINE_CONTRACT = "ams.tap_packet_engine/v1"
ENGINE_EVENT_SCHEMA = "ams.ns3.packet_event/v1"
ENGINE_PROGRAM = "ams-tap-packet-engine"
ENGINE_PHASES = {"good": 1, "recovery": 2}
ENDPOINT_SCHEMA_RELATIVE = "network/config/endpoint_transaction_schema.json"
ENDPOINT_MATRIX_RELATIVE = "network/config/endpoint_matrix_5uav.json"
ENGINE_CONFIG_TOOL_RELATIVE = "network/ns3/tap_packet_engine_config.py"
ENGINE_RUNNER_RELATIVE = "network/ns3/run_ns3_tap_packet_engine.sh"
ENGINE_SOURCE_RELATIVE = "network/ns3/scratch/ams-tap-packet-engine.cc"
REQUIRED_NS3_MODULES = (
    "applications",
    "bridge",
    "core",
    "csma",
    "flow-monitor",
    "internet",
    "mobility",
    "network",
    "stats",
    "tap-bridge",
    "traffic-control",
)
UAV1_CELL_IDS = tuple(
    f"uav1.{traffic_class}.{direction}"
    for traffic_class in ("control", "payload", "additional_data")
    for direction in ("downlink", "uplink")
)
PHASES = ("good", "down", "recovery")
EXPECTED_ATTEMPTS = {"good": 10, "down": 5, "recovery": 10}
CAPTURE_POINTS = (
    "gcs_ingress",
    "ns3_external_ingress",
    "ns3_ingress",
    "ns3_egress",
    "uav_egress",
)
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
ENGINE_EVENT_KEYS = {
    "schema",
    "event_epoch",
    "event_sequence",
    "sim_time_ns",
    "event",
    "packet_wire_hash_algorithm",
    "packet_wire_hash",
    "packet_wire_size",
    "packet_uid",
    "tos",
    "dscp",
    "traffic_class",
    "directed_link",
    "queue_id",
    "device_id",
    "source_mac",
    "destination_mac",
    "source_ip",
    "destination_ip",
    "transport_protocol",
    "source_udp_port",
    "destination_udp_port",
    "transport_payload_sha256",
    "transport_payload_size",
    "p2mp",
    "root_transmission",
    "queue_depth_packets",
    "queue_limit_packets",
    "drop_reason",
    "config_sha256",
    "seed",
    "run",
}
ENGINE_EVENTS = {
    "ingress",
    "enqueue",
    "dequeue",
    "channel",
    "drop",
    "egress",
    "phy_tx_drop",
    "phy_rx_drop",
}


def _is_int(value: Any) -> bool:
    return type(value) is int


def _is_number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(float(value))


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and HEX64_RE.fullmatch(value) is not None


def _strict_json_loads(text: str) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-standard JSON numeric constant {value!r}")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    return json.loads(
        text,
        parse_constant=reject_constant,
        object_pairs_hook=reject_duplicates,
    )


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


def _repository_source_record(relative: str) -> dict[str, Any]:
    path = ROOT_DIR / relative
    if not path.is_file() or path.is_symlink() or path.stat().st_size <= 0:
        raise ValueError(f"repository source is missing/nonregular: {relative}")
    return {"path": relative, "sha256": _sha256_file(path)}


def _raw_file_record(run_dir: Path, relative: str) -> dict[str, Any]:
    path = run_dir / relative
    if not path.is_file() or path.is_symlink() or path.stat().st_size <= 0:
        raise ValueError(f"raw packet-engine file is missing/nonregular: {relative}")
    return {
        "path": relative,
        "sha256": _sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


def derive_endpoint_subset_contract() -> dict[str, Any]:
    """Derive the exact six-cell uav1 schema-v1 identity from tracked inputs."""

    from network.validation.endpoint_transaction import (
        CONTRACT,
        MATRIX_ID,
        canonical_json,
        load_strict_json,
        sha256_bytes,
        validate_matrix_file,
    )

    schema_path = ROOT_DIR / ENDPOINT_SCHEMA_RELATIVE
    matrix_path = ROOT_DIR / ENDPOINT_MATRIX_RELATIVE
    schema = load_strict_json(schema_path)
    matrix = validate_matrix_file(matrix_path, schema_path=schema_path)
    cells = [
        cell
        for cell in matrix["cells"]
        if cell.get("uav", {}).get("name") == "uav1"
    ]
    if [cell.get("cell_id") for cell in cells] != list(UAV1_CELL_IDS):
        raise ValueError("endpoint matrix lacks the exact ordered six-cell uav1 subset")
    if (
        cells[0].get("capture_points", {}).get("remote_after_adapter")
        != "uav1.mavproxy.tail"
        or cells[1].get("capture_points", {}).get("source_before_adapter")
        != "uav1.mavproxy.tail"
        or cells[2].get("capture_points", {}).get("remote_after_adapter")
        != "uav1.sink.eth0"
        or cells[3].get("capture_points", {}).get("source_before_adapter")
        != "uav1.source.eth0"
        or cells[4].get("capture_points", {}).get("remote_after_adapter")
        != "uav1.sink.eth0"
        or cells[5].get("capture_points", {}).get("source_before_adapter")
        != "uav1.source.eth0"
    ):
        raise ValueError("endpoint matrix control/companion capture-point sides are not exact")
    if (
        matrix.get("schema_version") != 1
        or matrix.get("contract") != CONTRACT
        or CONTRACT != ENDPOINT_TRANSACTION_CONTRACT
        or matrix.get("matrix_id") != MATRIX_ID
        or MATRIX_ID != ENDPOINT_MATRIX_ID
        or schema.get("$id")
        != "https://ams.local/schemas/endpoint-transaction-v1.json"
        or schema.get("properties", {}).get("schema_version") != {"const": 1}
    ):
        raise ValueError("endpoint transaction schema-v1 identity is not exact")
    return {
        "contract": ENDPOINT_SUBSET_CONTRACT,
        "schema_version": 1,
        "endpoint_transaction_contract": ENDPOINT_TRANSACTION_CONTRACT,
        "matrix_id": ENDPOINT_MATRIX_ID,
        "schema": _repository_source_record(ENDPOINT_SCHEMA_RELATIVE),
        "matrix": _repository_source_record(ENDPOINT_MATRIX_RELATIVE),
        "subset": {
            "subset_id": "uav1_all_traffic_classes",
            "uav": "uav1",
            "cell_count": 6,
            "cell_ids": list(UAV1_CELL_IDS),
            "resolved_cells_sha256": sha256_bytes(canonical_json(cells)),
            "actual_control_tail_capture": {
                "capture_role": "tail",
                "interface": "ams-tail0",
                "pcap_path": "pcap/uav_tail.pcap",
                "downlink": {
                    "cell_id": "uav1.control.downlink",
                    "capture_point_role": "remote_after_adapter",
                    "capture_point_id": "uav1.mavproxy.tail",
                },
                "uplink": {
                    "cell_id": "uav1.control.uplink",
                    "capture_point_role": "source_before_adapter",
                    "capture_point_id": "uav1.mavproxy.tail",
                },
            },
        },
    }


def _endpoint_contract_gate(run_dir: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    path = run_dir / "metrics/m2_endpoint_contract.json"
    observed, failures = _load_object(path)
    try:
        expected = derive_endpoint_subset_contract()
    except Exception as exc:
        expected = {}
        failures.append(f"cannot independently derive endpoint subset: {exc}")
    if observed and observed != expected:
        failures.append(
            "m2_endpoint_contract is not the exact schema-v1 uav1 six-cell subset"
        )
    if metadata.get("endpoint_transaction") != observed:
        failures.append("m2_run endpoint_transaction does not equal the sealed raw contract")
    tail_contract = ((expected.get("subset") or {}).get("actual_control_tail_capture") or {})
    tail_stats, tail_stats_failures = _load_object(run_dir / "logs/capture_tail_stats.json")
    failures.extend(tail_stats_failures)
    if (
        tail_contract.get("capture_role") != "tail"
        or tail_contract.get("interface") != "ams-tail0"
        or tail_contract.get("pcap_path") != "pcap/uav_tail.pcap"
        or tail_stats.get("interface") != tail_contract.get("interface")
        or tail_stats.get("pcap_path") != "uav_tail.pcap"
    ):
        failures.append("M2 control capture contract is not bound to the actual MAVProxy tail PCAP")
    return _result(
        failures,
        path=str(path),
        cell_ids=list(UAV1_CELL_IDS),
        resolved_cells_sha256=(expected.get("subset") or {}).get(
            "resolved_cells_sha256"
        ),
        actual_control_tail_capture=tail_contract,
    )


def _packet_engine_receipt_gate(
    run_dir: Path, metadata: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    path = run_dir / "metrics/ns3_tap_build_receipt.json"
    receipt, failures = _load_object(path)
    expected_top = {
        "schema_version",
        "contract",
        "created_utc",
        "subject_sha256",
        "subject",
    }
    if receipt and set(receipt) != expected_top:
        failures.append("ns-3 receipt fields differ from the exact v1 contract")
    if (
        receipt.get("schema_version") != 1
        or receipt.get("contract") != "ams.ns3.build-receipt/v1"
    ):
        failures.append("ns-3 packet-engine receipt schema/contract is invalid")
    if not _parse_utc(receipt.get("created_utc")):
        failures.append("ns-3 packet-engine receipt created_utc is invalid")
    subject = receipt.get("subject") if isinstance(receipt.get("subject"), dict) else {}
    try:
        from network.ns3.ns3_build_receipt import subject_digest

        if receipt.get("subject_sha256") != subject_digest(subject):
            failures.append("ns-3 packet-engine receipt subject digest is invalid")
    except Exception as exc:
        failures.append(f"ns-3 packet-engine receipt subject hashing failed: {exc}")
    if subject.get("program") != ENGINE_PROGRAM:
        failures.append("ns-3 receipt is not for ams-tap-packet-engine")
    official = (
        subject.get("official_source")
        if isinstance(subject.get("official_source"), dict)
        else {}
    )
    if official != {
        "root": "/workspace/multiagent_simulation/.external/ns-3",
        "version": "3.40",
        "core_tree_files": 3764,
        "core_tree_sha256": (
            "0119836a7c79f7470f0c2c866de9c14ddc4f22349bbd194112ff2952713b64e8"
        ),
    }:
        failures.append("ns-3 receipt does not bind the canonical official 3.40 tree")
    scratch = (
        subject.get("scratch_source")
        if isinstance(subject.get("scratch_source"), dict)
        else {}
    )
    project = scratch.get("project") if isinstance(scratch.get("project"), dict) else {}
    copied = scratch.get("copied") if isinstance(scratch.get("copied"), dict) else {}
    current_hash = _sha256_file(ROOT_DIR / ENGINE_SOURCE_RELATIVE)
    if (
        project.get("path")
        != "/workspace/multiagent_simulation/network/ns3/scratch/ams-tap-packet-engine.cc"
        or copied.get("path")
        != "/workspace/multiagent_simulation/.external/ns-3/scratch/ams-tap-packet-engine.cc"
        or project.get("sha256") != current_hash
        or copied.get("sha256") != current_hash
        or scratch.get("byte_identical") is not True
    ):
        failures.append("ns-3 receipt does not bind the current shared packet-engine source")
    build = subject.get("build") if isinstance(subject.get("build"), dict) else {}
    if (
        build.get("enabled_modules") != list(REQUIRED_NS3_MODULES)
        or build.get("required_modules") != list(REQUIRED_NS3_MODULES)
    ):
        failures.append("ns-3 packet-engine receipt module union is not exact")
    executable = (
        subject.get("executable")
        if isinstance(subject.get("executable"), dict)
        else {}
    )
    if (
        executable.get("path")
        != "/workspace/multiagent_simulation/.external/ns-3/build/scratch/"
        "ns3.40-ams-tap-packet-engine-default"
        or not _is_sha256(executable.get("sha256"))
        or not _is_int(executable.get("size_bytes"))
        or executable.get("size_bytes", 0) <= 0
        or not _is_int(executable.get("mode"))
        or executable.get("mode", 0) & 0o111 == 0
    ):
        failures.append("ns-3 packet-engine executable identity is invalid")
    try:
        lock = yaml.safe_load(
            (ROOT_DIR / "network/config/dependency_lock.yaml").read_text(
                encoding="utf-8"
            )
        )
        locked_modules = sorted(lock["dependencies"]["ns3"]["required_modules"])
        if locked_modules != list(REQUIRED_NS3_MODULES):
            failures.append("validator module union differs from dependency lock")
    except Exception as exc:
        failures.append(f"cannot independently read locked ns-3 modules: {exc}")
    engine_identity = (
        metadata.get("packet_engine")
        if isinstance(metadata.get("packet_engine"), dict)
        else {}
    )
    try:
        if engine_identity.get("build_receipt") != _raw_file_record(
            run_dir, "metrics/ns3_tap_build_receipt.json"
        ):
            failures.append("m2_run packet-engine receipt file identity is stale")
    except ValueError as exc:
        failures.append(str(exc))
    if engine_identity.get("executable") != executable:
        failures.append("m2_run executable identity differs from the build receipt")
    return _result(failures, path=str(path), program=subject.get("program")), executable


def _engine_config_expected(run_dir: Path, phase: str, epoch: int) -> tuple[Any, dict[str, Any]]:
    from network.ns3.tap_packet_engine_config import from_repository

    config = from_repository(
        uav_count=1,
        duration_ms=3_600_000,
        seed=42,
        run=1,
        event_epoch=epoch,
        self_test=False,
        self_test_burst=1,
        self_test_unknown_tos=False,
        tap_gcs="tap-gcs",
        tap_uavs=("tap-uav",),
    )
    payload = {
        "contract": ENGINE_CONTRACT,
        "config_sha256": config.sha256(),
        "canonical_config": config.canonical_text(),
        "resolved": {**asdict(config), "tap_uavs": list(config.tap_uavs)},
        "engine_argv": config.engine_argv(
            events_file=str(run_dir / f"logs/ns3_{phase}_packet_events.jsonl"),
            pcap_prefix=str(run_dir / f"pcap/ns3_{phase}"),
        ),
        "source_sha256": {
            str(ROOT_DIR / "network/config/endpoints.yaml"): _sha256_file(
                ROOT_DIR / "network/config/endpoints.yaml"
            ),
            str(ROOT_DIR / "network/config/radio_24ghz.yaml"): _sha256_file(
                ROOT_DIR / "network/config/radio_24ghz.yaml"
            ),
        },
    }
    return config, payload


def _load_engine_events(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    records: list[dict[str, Any]] = []
    failures: list[str] = []
    if not path.is_file() or path.is_symlink():
        return records, [f"packet-engine event log is missing/nonregular: {path}"]
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        return records, [f"cannot read packet-engine event log {path}: {exc}"]
    if not lines:
        return records, [f"packet-engine event log is empty: {path}"]
    for line_number, line in enumerate(lines, start=1):
        if not line:
            failures.append(f"{path}:{line_number}: blank event record")
            continue
        try:
            record = _strict_json_loads(line)
        except (ValueError, json.JSONDecodeError) as exc:
            failures.append(f"{path}:{line_number}: invalid strict JSON: {exc}")
            continue
        if not isinstance(record, dict):
            failures.append(f"{path}:{line_number}: event is not an object")
            continue
        records.append(record)
    return records, failures


def _engine_required_directions(evidence: dict[str, Any]) -> dict[str, str]:
    directions: dict[str, str] = {}

    def add(value: Any, direction: str) -> None:
        if not _is_sha256(value):
            return
        previous = directions.get(value)
        if previous is not None and previous != direction:
            raise ValueError(f"payload hash {value} appears in both packet directions")
        directions[value] = direction

    for attempt in evidence.get("attempts", {}).values():
        add(attempt.get("marker_sha256"), "downlink")
        add(attempt.get("command_sha256"), "downlink")
    for ack in evidence.get("acks", {}).values():
        add(ack.get("packet_sha256"), "uplink")
    for rows in evidence.get("telemetry", {}).values():
        for telemetry in rows:
            add(telemetry.get("packet_sha256"), "uplink")
    for heartbeat in evidence.get("heartbeats", []):
        add(heartbeat.get("packet_sha256"), "uplink")
    return directions


def _packet_engine_event_failures(
    records: list[dict[str, Any]],
    *,
    phase: str,
    epoch: int,
    config_sha256: str,
    phase_evidence: dict[str, Any],
) -> tuple[list[str], dict[str, Any]]:
    failures: list[str] = []
    stages: Counter[str] = Counter()
    previous_sim_time = -1
    for index, record in enumerate(records, start=1):
        label = f"{phase}/engine event {index}"
        if set(record) != ENGINE_EVENT_KEYS:
            failures.append(f"{label}: fields differ from exact non-Sionna v1 schema")
        if record.get("schema") != ENGINE_EVENT_SCHEMA:
            failures.append(f"{label}: schema identity mismatch")
        if record.get("event_epoch") != epoch or not _is_int(
            record.get("event_epoch")
        ):
            failures.append(f"{label}: event_epoch mismatch")
        if record.get("event_sequence") != index or not _is_int(
            record.get("event_sequence")
        ):
            failures.append(f"{label}: event_sequence is not contiguous")
        sim_time = record.get("sim_time_ns")
        if not _is_int(sim_time) or sim_time < 0 or sim_time < previous_sim_time:
            failures.append(f"{label}: sim_time_ns is invalid/nonmonotonic")
        elif _is_int(sim_time):
            previous_sim_time = sim_time
        event = record.get("event")
        if event not in ENGINE_EVENTS:
            failures.append(f"{label}: unknown packet-engine event {event!r}")
        elif isinstance(event, str):
            stages[event] += 1
        if record.get("config_sha256") != config_sha256:
            failures.append(f"{label}: config_sha256 crosses phase identity")
        if record.get("seed") != 42 or record.get("run") != 1:
            failures.append(f"{label}: RNG seed/run identity mismatch")
        if (
            record.get("packet_wire_hash_algorithm") != "sha256"
            or not _is_sha256(record.get("packet_wire_hash"))
            or not _is_int(record.get("packet_wire_size"))
            or record.get("packet_wire_size", 0) <= 0
            or not _is_int(record.get("packet_uid"))
            or record.get("packet_uid", -1) < 0
        ):
            failures.append(f"{label}: wire packet identity is invalid")
        payload_hash = record.get("transport_payload_sha256")
        payload_size = record.get("transport_payload_size")
        if payload_hash is None:
            if payload_size is not None:
                failures.append(f"{label}: null payload hash has nonnull size")
        elif (
            not _is_sha256(payload_hash)
            or not _is_int(payload_size)
            or payload_size <= 0
        ):
            failures.append(f"{label}: transport payload identity is invalid")
        if type(record.get("p2mp")) is not bool or type(
            record.get("root_transmission")
        ) is not bool:
            failures.append(f"{label}: multicast flags are not booleans")
        elif record.get("p2mp") or record.get("root_transmission"):
            failures.append(f"{label}: one-UAV unicast run contains P2MP identity")

    for stage in ("ingress", "enqueue", "dequeue", "channel", "egress"):
        if stages[stage] <= 0:
            failures.append(f"{phase}: packet-engine has no {stage} event")

    try:
        required = _engine_required_directions(phase_evidence)
    except ValueError as exc:
        failures.append(f"{phase}: {exc}")
        required = {}
    if not required:
        failures.append(f"{phase}: no decoded probe payloads are available for engine binding")
    for payload_hash, direction in required.items():
        matching = [
            record
            for record in records
            if record.get("transport_payload_sha256") == payload_hash
        ]
        ingress = [record for record in matching if record.get("event") == "ingress"]
        egress = [record for record in matching if record.get("event") == "egress"]
        if not ingress or not egress:
            failures.append(
                f"{phase}: payload {payload_hash} lacks exact engine ingress/egress"
            )
            continue
        link = "cp>uav1" if direction == "downlink" else "uav1>cp"
        expected_ports = (14600, 14601) if direction == "downlink" else (14601, 14600)
        expected_ips = (
            ("10.71.0.10", "10.71.1.10")
            if direction == "downlink"
            else ("10.71.1.10", "10.71.0.10")
        )
        for record in ingress + egress:
            event = record["event"]
            expected_device = (
                ("cp.tap.ingress" if direction == "downlink" else "uav1.tap.ingress")
                if event == "ingress"
                else ("uav1.tap.egress" if direction == "downlink" else "cp.tap.egress")
            )
            if (
                record.get("tos") != 184
                or record.get("dscp") != 46
                or record.get("traffic_class") != "control"
                or record.get("directed_link") != link
                or record.get("queue_id") != f"{link}.control.q0"
                or record.get("device_id") != expected_device
                or record.get("transport_protocol") != 17
                or (record.get("source_udp_port"), record.get("destination_udp_port"))
                != expected_ports
                or (record.get("source_ip"), record.get("destination_ip"))
                != expected_ips
            ):
                failures.append(
                    f"{phase}: payload {payload_hash} has wrong engine path/class identity"
                )
        if any(record.get("event") in {"drop", "phy_tx_drop", "phy_rx_drop"} for record in matching):
            failures.append(f"{phase}: required payload {payload_hash} has a drop event")
    return failures, {"records": len(records), "stages": dict(sorted(stages.items()))}


def _packet_engine_gate(
    run_dir: Path,
    metadata: dict[str, Any],
    phase_evidence: dict[str, Any],
    executable: dict[str, Any],
) -> dict[str, Any]:
    failures: list[str] = []
    details: dict[str, Any] = {"phases": {}}
    engine_identity = (
        metadata.get("packet_engine")
        if isinstance(metadata.get("packet_engine"), dict)
        else {}
    )
    phase_identities: dict[str, Any] = {}
    config_hashes: dict[str, str] = {}
    for phase, epoch in ENGINE_PHASES.items():
        config_relative = f"logs/ns3_{phase}_config.json"
        config_path = run_dir / config_relative
        observed_config, config_failures = _load_object(config_path)
        failures.extend(config_failures)
        try:
            config, expected_config = _engine_config_expected(run_dir, phase, epoch)
        except Exception as exc:
            config, expected_config = None, {}
            failures.append(f"{phase}: cannot independently derive engine config: {exc}")
        if observed_config and observed_config != expected_config:
            failures.append(f"{phase}: resolved packet-engine config/hash is not exact")
        config_sha256 = expected_config.get("config_sha256", "")
        config_hashes[phase] = config_sha256

        argv_relative = f"logs/ns3_{phase}.argv"
        argv_path = run_dir / argv_relative
        try:
            expected_argv_text = "".join(
                f"{argument}\n" for argument in expected_config["engine_argv"]
            )
            if argv_path.is_symlink() or argv_path.read_text(encoding="utf-8") != expected_argv_text:
                failures.append(f"{phase}: packet-engine argv file is not exact")
        except (OSError, UnicodeError, KeyError) as exc:
            failures.append(f"{phase}: cannot verify packet-engine argv: {exc}")

        ready_relative = f"logs/ns3_{phase}.ready"
        ready, ready_failures = _load_object(run_dir / ready_relative)
        failures.extend(ready_failures)
        expected_ready = {
            "status": "ready",
            "contract": ENGINE_CONTRACT,
            "config_sha256": config_sha256,
            "event_epoch": epoch,
            "uav_count": 1,
        }
        if ready and ready != expected_ready:
            failures.append(f"{phase}: packet-engine readiness identity is not exact")
        stop_relative = f"logs/ns3_{phase}.stop"
        try:
            stop_path = run_dir / stop_relative
            if stop_path.is_symlink() or stop_path.read_bytes() != b"stop\n":
                failures.append(f"{phase}: packet-engine stop marker is not exact")
        except OSError as exc:
            failures.append(f"{phase}: packet-engine stop marker is missing: {exc}")

        events_relative = f"logs/ns3_{phase}_packet_events.jsonl"
        records, event_load_failures = _load_engine_events(run_dir / events_relative)
        failures.extend(event_load_failures)
        event_failures, event_details = _packet_engine_event_failures(
            records,
            phase=phase,
            epoch=epoch,
            config_sha256=config_sha256,
            phase_evidence=phase_evidence.get(phase, {}),
        )
        failures.extend(event_failures)
        details["phases"][phase] = event_details
        try:
            phase_identities[phase] = {
                "event_epoch": epoch,
                "config_sha256": config_sha256,
                "config": _raw_file_record(run_dir, config_relative),
                "events": _raw_file_record(run_dir, events_relative),
                "argv": _raw_file_record(run_dir, argv_relative),
                "ready": _raw_file_record(run_dir, ready_relative),
                "stop": _raw_file_record(run_dir, stop_relative),
            }
        except ValueError as exc:
            failures.append(str(exc))

    if len(set(config_hashes.values())) != len(ENGINE_PHASES):
        failures.append("good/recovery packet-engine config hashes are not epoch-distinct")
    try:
        receipt_record = _raw_file_record(
            run_dir, "metrics/ns3_tap_build_receipt.json"
        )
        expected_identity = {
            "contract": ENGINE_CONTRACT,
            "program": ENGINE_PROGRAM,
            "uav_count": 1,
            "source_sha256": _sha256_file(ROOT_DIR / ENGINE_SOURCE_RELATIVE),
            "binary_sha256": executable.get("sha256"),
            "build_receipt_sha256": receipt_record["sha256"],
            "config_contract": ENGINE_CONTRACT,
            "config_sha256": config_hashes,
            "config_tool_sha256": _sha256_file(
                ROOT_DIR / ENGINE_CONFIG_TOOL_RELATIVE
            ),
            "runner_sha256": _sha256_file(ROOT_DIR / ENGINE_RUNNER_RELATIVE),
            "event_schema": ENGINE_EVENT_SCHEMA,
            "config_tool": _repository_source_record(ENGINE_CONFIG_TOOL_RELATIVE),
            "runner": _repository_source_record(ENGINE_RUNNER_RELATIVE),
            "build_receipt": receipt_record,
            "executable": executable,
            "phases": phase_identities,
        }
        if engine_identity != expected_identity:
            failures.append("m2_run packet_engine identity is not the exact shared core contract")
    except ValueError as exc:
        failures.append(str(exc))
    details["config_sha256"] = config_hashes
    details["event_schema"] = ENGINE_EVENT_SCHEMA
    return _result(failures, **details)


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
    for point in ("gcs_ingress", "ns3_external_ingress", "uav_egress"):
        relative = f"pcap/{point}_down.pcap"
        parsed, parse_failures = _parse_pcap(run_dir / relative)
        failures.extend(parse_failures)
        stats[relative] = {
            key: value for key, value in parsed.items() if key not in {"payload_hashes", "payload_by_hash"}
        }
        observed: Counter[str] = parsed.get("payload_hashes", Counter())
        if point in {"gcs_ingress", "ns3_external_ingress"}:
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


def _capture_stats_gate(run_dir: Path) -> dict[str, Any]:
    failures: list[str] = []
    expected = {
        "tail": ("ams-tail0", "pcap/uav_tail.pcap"),
        "gcs_good": ("eth0", "pcap/gcs_ingress_good.pcap"),
        "ns3_external_good": (
            "v-gcs-ns3",
            "pcap/ns3_external_ingress_good.pcap",
        ),
        "uav_good": ("eth0", "pcap/uav_egress_good.pcap"),
        "gcs_down": ("eth0", "pcap/gcs_ingress_down.pcap"),
        "ns3_external_down": (
            "v-gcs-ns3",
            "pcap/ns3_external_ingress_down.pcap",
        ),
        "uav_down": ("eth0", "pcap/uav_egress_down.pcap"),
        "gcs_recovery": ("eth0", "pcap/gcs_ingress_recovery.pcap"),
        "ns3_external_recovery": (
            "v-gcs-ns3",
            "pcap/ns3_external_ingress_recovery.pcap",
        ),
        "uav_recovery": ("eth0", "pcap/uav_egress_recovery.pcap"),
    }
    exact_keys = {
        "contract",
        "interface",
        "pcap_path",
        "pcap_bytes",
        "linktype",
        "snaplen",
        "started_monotonic_ns",
        "stopped_monotonic_ns",
        "stop_signal",
        "packets_written",
        "packets_received_kernel",
        "packets_dropped_kernel",
    }
    details: dict[str, Any] = {}
    for key, (interface, pcap_relative) in expected.items():
        stats_relative = f"logs/capture_{key}_stats.json"
        stats, load_failures = _load_object(run_dir / stats_relative)
        failures.extend(load_failures)
        pcap_path = run_dir / pcap_relative
        parsed, parse_failures = _parse_pcap(pcap_path)
        failures.extend(parse_failures)
        packet_count = parsed.get("packet_count")
        if stats:
            if set(stats) != exact_keys:
                failures.append(f"{stats_relative}: fields differ from exact stats v1")
            if (
                stats.get("contract") != "ams.raw-packet-capture-stats/v1"
                or stats.get("interface") != interface
                or stats.get("pcap_path") != pcap_path.name
                or stats.get("linktype") != 1
                or stats.get("snaplen") != 65_535
                or stats.get("stop_signal") != "SIGINT"
            ):
                failures.append(f"{stats_relative}: capture identity is not exact")
            if (
                not _is_int(stats.get("pcap_bytes"))
                or not pcap_path.is_file()
                or stats.get("pcap_bytes") != pcap_path.stat().st_size
            ):
                failures.append(f"{stats_relative}: PCAP byte identity mismatch")
            started = stats.get("started_monotonic_ns")
            stopped = stats.get("stopped_monotonic_ns")
            if (
                not _is_int(started)
                or not _is_int(stopped)
                or started <= 0
                or stopped <= started
            ):
                failures.append(f"{stats_relative}: capture interval is invalid")
            written = stats.get("packets_written")
            received = stats.get("packets_received_kernel")
            dropped = stats.get("packets_dropped_kernel")
            if (
                not _is_int(written)
                or written < 0
                or written != packet_count
                or not _is_int(received)
                or received < written
                or dropped != 0
                or not _is_int(dropped)
            ):
                failures.append(
                    f"{stats_relative}: packet/drop accounting is not exact"
                )
            details[key] = {
                "packets_written": written,
                "packets_received_kernel": received,
                "packets_dropped_kernel": dropped,
            }
        for suffix in ("stdout", "stderr"):
            stream = run_dir / f"logs/capture_{key}.{suffix}"
            try:
                if stream.is_symlink() or not stream.is_file() or stream.stat().st_size != 0:
                    failures.append(
                        f"capture_{key}.{suffix} is absent, symlinked, or nonempty"
                    )
            except OSError as exc:
                failures.append(f"cannot inspect capture_{key}.{suffix}: {exc}")
    return _result(failures, captures=details)


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
    expected_implementation = {
        "packet_ingress_mode": "tap_bridge_external",
        "medium_model": "csma_surrogate",
        "radio_provider_id": "tcp_jsonl_real_sionna",
        "radio_provider_runtime_consumed": False,
        "runtime_provider_id": "not_applicable_pre_m4",
        "reason": "profile_pre_m4",
    }
    if implementation != expected_implementation:
        failures.append(
            "provenance implementation/provider-consumption contract is not exact for M2"
        )

    config_hashes = data.get("config_hashes") if isinstance(data.get("config_hashes"), dict) else {}
    current_configs = (
        "network/config/scenario_1uav_vertical_slice.yaml",
        "network/config/endpoints.yaml",
        "network/config/radio_24ghz.yaml",
        ENDPOINT_SCHEMA_RELATIVE,
        ENDPOINT_MATRIX_RELATIVE,
    )
    for relative in current_configs:
        current = ROOT_DIR / relative
        expected = _sha256_file(current) if current.is_file() else None
        if config_hashes.get(relative) != expected:
            failures.append(f"provenance config hash is absent/stale for {relative}")

    source_manifest = data.get("source_manifest") if isinstance(data.get("source_manifest"), dict) else {}
    current_sources = (
        "network/validation/validate_m2_vertical_slice.py",
        "network/validation/endpoint_transaction.py",
        "network/bridge/opaque_udp_relay.py",
        "network/bridge/uav_mavlink_endpoint.py",
        ENGINE_SOURCE_RELATIVE,
        ENGINE_CONFIG_TOOL_RELATIVE,
        "network/scripts/setup_one_uav_netns.sh",
        "network/scripts/raw_packet_capture.py",
        ENGINE_RUNNER_RELATIVE,
        "network/scripts/run_one_uav_vertical_slice.sh",
        "network/tests/mavlink_vertical_slice_probe.py",
        ENDPOINT_SCHEMA_RELATIVE,
        ENDPOINT_MATRIX_RELATIVE,
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
        "metrics/m2_endpoint_contract.json",
        "metrics/provenance.json",
        "metrics/ns3_tap_build_receipt.json",
        "logs/m2_probe_events.jsonl",
        "logs/uav_adapter.jsonl",
        "logs/m2_process_events.jsonl",
        "logs/m2_runner.log",
        *(f"logs/ns3_{phase}_config.json" for phase in ENGINE_PHASES),
        *(f"logs/ns3_{phase}_packet_events.jsonl" for phase in ENGINE_PHASES),
        *(f"logs/ns3_{phase}.argv" for phase in ENGINE_PHASES),
        *(f"logs/ns3_{phase}.ready" for phase in ENGINE_PHASES),
        *(f"logs/ns3_{phase}.stop" for phase in ENGINE_PHASES),
        *(f"pcap/{point}_{phase}.pcap" for phase in ("good", "recovery") for point in CAPTURE_POINTS),
        "pcap/gcs_ingress_down.pcap",
        "pcap/ns3_external_ingress_down.pcap",
        "pcap/uav_egress_down.pcap",
    }
    for key in (
        "tail",
        "gcs_good",
        "ns3_external_good",
        "uav_good",
        "gcs_down",
        "ns3_external_down",
        "uav_down",
        "gcs_recovery",
        "ns3_external_recovery",
        "uav_recovery",
    ):
        required.update(
            {
                f"logs/capture_{key}_stats.json",
                f"logs/capture_{key}.stdout",
                f"logs/capture_{key}.stderr",
            }
        )
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
        empty_allowed = relative.endswith((".stdout", ".stderr"))
        minimum_size = 1 if relative in required and not empty_allowed else 0
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
        gates["endpoint_contract"] = _endpoint_contract_gate(run_dir, metadata)
    except Exception as exc:
        gates["endpoint_contract"] = _result(
            [f"endpoint-contract parser failed closed: {exc}"]
        )

    try:
        receipt_result, executable = _packet_engine_receipt_gate(run_dir, metadata)
    except Exception as exc:
        receipt_result, executable = (
            _result([f"ns-3 packet-engine receipt parser failed closed: {exc}"]),
            {},
        )
    gates["ns3_build_receipt"] = receipt_result
    try:
        gates["packet_engine"] = _packet_engine_gate(
            run_dir, metadata, phase_evidence, executable
        )
    except Exception as exc:
        gates["packet_engine"] = _result(
            [f"packet-engine evidence parser failed closed: {exc}"]
        )

    try:
        gates["packet_captures"] = _pcap_gate(run_dir, phase_evidence)
    except Exception as exc:
        gates["packet_captures"] = _result([f"PCAP parser failed closed: {exc}"])
    try:
        gates["capture_accounting"] = _capture_stats_gate(run_dir)
    except Exception as exc:
        gates["capture_accounting"] = _result(
            [f"raw-capture accounting parser failed closed: {exc}"]
        )

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
        gates["provenance"] = _provenance_gate(run_dir, metadata)
    except Exception as exc:
        gates["provenance"] = _result([f"provenance parser failed closed: {exc}"])
    try:
        gates["manifest"] = _manifest_gate(run_dir, metadata)
    except Exception as exc:
        gates["manifest"] = _result([f"manifest parser failed closed: {exc}"])

    passed = all(value.get("status") == "passed" for value in gates.values())
    engine_identity = (
        metadata.get("packet_engine")
        if isinstance(metadata.get("packet_engine"), dict)
        else {}
    )
    endpoint_identity = (
        metadata.get("endpoint_transaction")
        if isinstance(metadata.get("endpoint_transaction"), dict)
        else {}
    )
    endpoint_subset = (
        endpoint_identity.get("subset")
        if isinstance(endpoint_identity.get("subset"), dict)
        else {}
    )
    endpoint_schema = (
        endpoint_identity.get("schema")
        if isinstance(endpoint_identity.get("schema"), dict)
        else {}
    )
    endpoint_matrix = (
        endpoint_identity.get("matrix")
        if isinstance(endpoint_identity.get("matrix"), dict)
        else {}
    )
    return {
        "schema_version": 2,
        "contract": RESULT_CONTRACT,
        "validation_contract": EVIDENCE_CONTRACT,
        "run_id": run_dir.name,
        "runtime_id": metadata.get("runtime_id"),
        "packet_engine": {
            "contract": engine_identity.get("contract"),
            "program": engine_identity.get("program"),
            "uav_count": engine_identity.get("uav_count"),
            "source_sha256": engine_identity.get("source_sha256"),
            "binary_sha256": engine_identity.get("binary_sha256"),
            "build_receipt_sha256": engine_identity.get(
                "build_receipt_sha256"
            ),
            "config_contract": engine_identity.get("config_contract"),
            "config_sha256": engine_identity.get("config_sha256"),
            "config_tool_sha256": engine_identity.get("config_tool_sha256"),
            "runner_sha256": engine_identity.get("runner_sha256"),
            "event_schema": engine_identity.get("event_schema"),
        },
        "endpoint_transaction": {
            "schema_version": endpoint_identity.get("schema_version"),
            "schema_sha256": endpoint_schema.get("sha256"),
            "matrix_sha256": endpoint_matrix.get("sha256"),
            "subset_cell_ids": endpoint_subset.get("cell_ids"),
            "subset_cells_sha256": endpoint_subset.get("resolved_cells_sha256"),
        },
        "passed": passed,
        "failures": [
            f"{gate_name}: {failure}"
            for gate_name, gate in gates.items()
            for failure in gate.get("failures", [])
        ],
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
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)
    run_dir = args.run_dir.resolve()
    if not run_dir.is_dir():
        print(f"FAIL M2 run directory does not exist: {run_dir}", file=sys.stderr)
        return 2
    result = evaluate_m2_vertical_slice(run_dir)
    if args.json_output and args.no_write:
        print("FAIL --json-output and --no-write are mutually exclusive", file=sys.stderr)
        return 2
    encoded = json.dumps(result, allow_nan=False, indent=2, sort_keys=True) + "\n"
    if args.no_write:
        sys.stdout.write(encoded)
        return 0 if result["passed"] else 1
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
