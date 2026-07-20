#!/usr/bin/env python3
"""Fail-closed validator for the one-UAV M2 external-packet vertical slice.

Raw evidence contract ``ams.m2.vertical_slice/v2``
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
* ``logs/m2_lifecycle.jsonl`` and ``logs/m2_monitor.jsonl`` prove the
  continuous capture/endpoint/engine lifecycle and the stable identities of
  its long-lived producers;
* ``logs/ns3_{good,recovery}_config.json`` and packet-event JSONL bind both
  live epochs to the shared ``ams-tap-packet-engine`` with ``uavCount=1``;
* five persistent external classic-PCAP collectors contain exact UDP payloads
  across the whole good -> stopped -> recovery lifecycle, while the ns-3
  engine keeps its two phase-local internal PCAPs;
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
    ``packet_sha256`` and ``source_system``. Positive windows require at least
    three fresh heartbeats: each is causally bound to a unique adapter
    ``tail_to_gcs`` forward in that same window which reaches the raw GCS
    observation within the bounded handoff interval. Older
    continuous-liveness forwards are retained as stale evidence but cannot
    satisfy the minimum.
``heartbeat_timeout``
    finite ``timeout_s`` of at least one second.

Each JSONL common envelope is ``run_id``, ``runtime_id``, ``run_nonce``, a
contiguous one-based ``event_seq``, strictly increasing ``monotonic_ns``, and
UTC ``wall_utc``.  Probe/process records also name ``phase`` (``good``,
``down``, or ``recovery``).  The GCS endpoint is one persistent UDP bind with
globally monotonic raw datagram occurrence sequences across all three windows.
Stable process roles are ``uav_adapter``, ``mavproxy``, ``sitl``, and
``gcs_probe``; ns-3 must be alive in good/recovery, absent in down, and have a
new identity on recovery.

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
EVIDENCE_CONTRACT = "ams.m2.vertical_slice/v2"
RESULT_CONTRACT = "ams.m2.vertical-slice-validation/v2"
MANIFEST_CONTRACT = "ams.m2.vertical_slice.manifest/v2"
ENDPOINT_SUBSET_CONTRACT = "ams.m2.endpoint_subset/v1"
ENDPOINT_TRANSACTION_CONTRACT = "endpoint_transaction_schema=1"
ENDPOINT_MATRIX_ID = "ams.endpoint_matrix.5uav/v1"
ENGINE_CONTRACT = "ams.tap_packet_engine/v1"
ENGINE_EVENT_SCHEMA = "ams.ns3.packet_event/v1"
ENGINE_LIFECYCLE_SCHEMA = "ams.ns3.lifecycle/v1"
ENGINE_LIFECYCLE_EVENTS = (
    "ready",
    "stop_observed",
    "queues_terminal",
    "stopped",
)
ENGINE_PROGRAM = "ams-tap-packet-engine"
ENGINE_PHASES = {"good": 1, "recovery": 2}
ENDPOINT_SCHEMA_RELATIVE = "network/config/endpoint_transaction_schema.json"
ENDPOINT_MATRIX_RELATIVE = "network/config/endpoint_matrix_5uav.json"
ENGINE_CONFIG_TOOL_RELATIVE = "network/ns3/tap_packet_engine_config.py"
ENGINE_RUNNER_RELATIVE = "network/ns3/run_ns3_tap_packet_engine.sh"
ENGINE_SOURCE_RELATIVE = "network/ns3/scratch/ams-tap-packet-engine.cc"
CAPTURE_STATS_CONTRACT = "ams.raw-packet-capture-stats/v2"
CAPTURE_PROTOCOL = "ETH_P_ALL"
CAPTURE_PACKET_FILTER = "none"
CAPTURE_RECEIVE_BUFFER_REQUESTED_BYTES = 8_388_608
CAPTURE_RECEIVE_BUFFER_EFFECTIVE_BYTES = 16_777_216
CAPTURE_RECEIVE_BUFFER_SETTERS = {"SO_RCVBUF", "SO_RCVBUFFORCE"}
CAPTURE_DRAIN_BATCH_PACKET_LIMIT = 256
CAPTURE_DRAIN_BATCH_BYTE_LIMIT = 4_194_304
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
MIN_POSITIVE_HEARTBEATS = 3
# A current adapter relay must reach the independently observed GCS UDP socket
# promptly.  The bound is the M2 control ceiling (250 ms) and avoids letting a
# byte-identical MAVLink heartbeat from an earlier sequence-wrap epoch mask a
# real current relay.  It is deliberately a maximum, not a producer claim.
MAX_HEARTBEAT_FORWARD_TO_RAW_NS = 250_000_000
PROBE_RAW_EVENT_SCHEMA = "ams.m2.probe-event/v2"
PERSISTENT_ENDPOINT_EVENT_SCHEMA = "ams.m2.persistent-gcs-endpoint/v1"
LIFECYCLE_SCHEMA = "ams.m2.lifecycle/v1"
MONITOR_SCHEMA = "ams.m2.monitor/v1"
MONITOR_MAX_SAMPLE_GAP_NS = 1_500_000_000
MIN_READINESS_DWELL_NS = 10_000_000_000
LIFECYCLE_REQUIRED_ORDER = (
    "captures_ready",
    "endpoints_ready",
    "engine1_ready",
    "good_dwell_start",
    "good_dwell_complete",
    "good_start",
    "good_terminal",
    "prestop_dwell_start",
    "prestop_dwell_complete",
    "stop_requested",
    "engine1_stopped",
    "stopped_drain_start",
    "stopped_drain_complete",
    "down_start",
    "down_terminal",
    "engine2_ready",
    "recovery_dwell_start",
    "recovery_dwell_complete",
    "recovery_start",
    "recovery_terminal",
    "recovery_prestop_dwell_start",
    "recovery_prestop_dwell_complete",
    "recovery_stop_requested",
    "engine2_stopped",
)
# These are deliberately physical, persistent collector roles.  They start
# before the first packet-engine epoch and stop only after recovery; replacing
# them with phase-local PCAPs would hide a queued frame at an engine boundary.
PERSISTENT_CAPTURE_SPECS = (
    ("tail", "ams-tail0", "pcap/uav_tail.pcap"),
    ("gcs", "eth0", "pcap/gcs_ingress.pcap"),
    ("ns3_external_gcs", "v-gcs-ns3", "pcap/ns3_external_ingress.pcap"),
    ("ns3_external_uav", "v-uav-ns3", "pcap/ns3_external_egress.pcap"),
    ("uav", "eth0", "pcap/uav_egress.pcap"),
)
PERSISTENT_CAPTURE_PCAPS = tuple(spec[2] for spec in PERSISTENT_CAPTURE_SPECS)
ENGINE_CAPTURE_POINTS = ("ns3_ingress", "ns3_egress")
PHASE_LOCAL_PERSISTENT_CAPTURE_RE = re.compile(
    r"^pcap/(?:uav_tail|gcs_ingress|ns3_external_ingress|ns3_external_egress|uav_egress)_(?:good|down|recovery)\.pcap$"
)
STABLE_PROCESS_ROLES = ("uav_adapter", "mavproxy", "sitl", "gcs_probe")
PHASE_PROCESS_ROLES = (*STABLE_PROCESS_ROLES, "ns3")
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
        "expected_version": "3.40",
        "core_tree_files": 3764,
        "core_tree_sha256": (
            "0119836a7c79f7470f0c2c866de9c14ddc4f22349bbd194112ff2952713b64e8"
        ),
        "expected_core_tree_files": 3764,
        "expected_core_tree_sha256": (
            "0119836a7c79f7470f0c2c866de9c14ddc4f22349bbd194112ff2952713b64e8"
        ),
        "excludes": ["build", "cmake-cache", "scratch", "src/lorawan"],
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
            _, expected_config = _engine_config_expected(run_dir, phase, epoch)
        except Exception as exc:
            expected_config = {}
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
                "lifecycle": _raw_file_record(
                    run_dir, f"logs/ns3_{phase}.lifecycle.jsonl"
                ),
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


def _engine_lifecycle_gate(
    run_dir: Path,
    metadata: dict[str, Any],
    runner_lifecycle: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Prove clean, durable terminal state for both real ns-3 epochs.

    The runner journal proves orchestration order; this independent journal is
    owned by the C++ packet engine and proves that the queues which actually
    carried packets were flushed before the process stopped.  Neither journal
    is allowed to substitute for the other.
    """

    failures: list[str] = []
    details: dict[str, Any] = {"phases": {}}
    engine_identity = (
        metadata.get("packet_engine")
        if isinstance(metadata.get("packet_engine"), dict)
        else {}
    )
    expected_hashes = (
        engine_identity.get("config_sha256")
        if isinstance(engine_identity.get("config_sha256"), dict)
        else {}
    )
    runner_events = {
        "good": ("engine1_ready", "stop_requested", "engine1_stopped"),
        "recovery": (
            "engine2_ready",
            "recovery_stop_requested",
            "engine2_stopped",
        ),
    }
    base_keys = {
        "schema",
        "event",
        "event_sequence",
        "event_epoch",
        "config_sha256",
        "host_monotonic_ns",
        "sim_time_ns",
    }
    expected_keys = {
        "ready": base_keys | {"registered_queue_count"},
        "stop_observed": base_keys | {"stop_reason"},
        "queues_terminal": base_keys | {"stop_reason", "queues", "all_queues_empty"},
        "stopped": base_keys | {"stop_reason"},
    }

    for phase, epoch in ENGINE_PHASES.items():
        relative = f"logs/ns3_{phase}.lifecycle.jsonl"
        path = run_dir / relative
        records: list[dict[str, Any]] = []
        if not path.is_file() or path.is_symlink():
            failures.append(f"{phase}: packet-engine lifecycle log is missing/nonregular")
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            failures.append(f"{phase}: cannot read packet-engine lifecycle: {exc}")
            continue
        if len(lines) != len(ENGINE_LIFECYCLE_EVENTS):
            failures.append(
                f"{phase}: packet-engine lifecycle has {len(lines)} records, expected "
                f"{len(ENGINE_LIFECYCLE_EVENTS)}"
            )
        previous_host_ns: int | None = None
        previous_sim_ns: int | None = None
        for line_number, line in enumerate(lines, start=1):
            label = f"{relative}:{line_number}"
            if not line.strip():
                failures.append(f"{label}: blank JSONL record")
                continue
            try:
                record = _strict_json_loads(line)
            except (ValueError, json.JSONDecodeError) as exc:
                failures.append(f"{label}: invalid strict JSON: {exc}")
                continue
            if not isinstance(record, dict):
                failures.append(f"{label}: record is not an object")
                continue
            records.append(record)
            expected_event = (
                ENGINE_LIFECYCLE_EVENTS[line_number - 1]
                if line_number <= len(ENGINE_LIFECYCLE_EVENTS)
                else None
            )
            if record.get("event") != expected_event:
                failures.append(f"{label}: event is not the required lifecycle transition")
            if record.get("event_sequence") != line_number or not _is_int(record.get("event_sequence")):
                failures.append(f"{label}: event_sequence must be contiguous from one")
            if record.get("schema") != ENGINE_LIFECYCLE_SCHEMA:
                failures.append(f"{label}: schema is not {ENGINE_LIFECYCLE_SCHEMA!r}")
            if record.get("event_epoch") != epoch or not _is_int(record.get("event_epoch")):
                failures.append(f"{label}: event_epoch does not match {phase} epoch")
            if record.get("config_sha256") != expected_hashes.get(phase) or not _is_sha256(
                record.get("config_sha256")
            ):
                failures.append(f"{label}: config_sha256 does not bind the live engine config")
            host_ns = record.get("host_monotonic_ns")
            sim_ns = record.get("sim_time_ns")
            if not _is_int(host_ns) or host_ns <= 0:
                failures.append(f"{label}: host_monotonic_ns is invalid")
            elif previous_host_ns is not None and host_ns <= previous_host_ns:
                failures.append(f"{label}: host_monotonic_ns is not strictly increasing")
            if _is_int(host_ns):
                previous_host_ns = host_ns
            if not _is_int(sim_ns) or sim_ns < 0:
                failures.append(f"{label}: sim_time_ns is invalid")
            elif previous_sim_ns is not None and sim_ns < previous_sim_ns:
                failures.append(f"{label}: sim_time_ns regressed")
            if _is_int(sim_ns):
                previous_sim_ns = sim_ns
            event = record.get("event")
            if event in expected_keys and set(record) != expected_keys[event]:
                failures.append(f"{label}: fields differ from exact {event} lifecycle contract")

        if len(records) != len(ENGINE_LIFECYCLE_EVENTS):
            continue
        by_event = {record.get("event"): record for record in records}
        ready = by_event.get("ready", {})
        stop_observed = by_event.get("stop_observed", {})
        terminal = by_event.get("queues_terminal", {})
        stopped = by_event.get("stopped", {})
        if ready.get("registered_queue_count") != 2:
            failures.append(f"{phase}: ready must register exactly the two live radio queues")
        for event_name, record in (
            ("stop_observed", stop_observed),
            ("queues_terminal", terminal),
            ("stopped", stopped),
        ):
            if record.get("stop_reason") != "stop_file":
                failures.append(f"{phase}: {event_name} is not caused by the sealed stop-file")
        if terminal.get("all_queues_empty") is not True:
            failures.append(f"{phase}: queues_terminal.all_queues_empty is not true")
        queues = terminal.get("queues")
        if not isinstance(queues, list) or len(queues) != 2:
            failures.append(f"{phase}: queues_terminal must cover exactly two live radio queues")
        else:
            device_ids: set[str] = set()
            depth_keys = {
                "control_packets",
                "payload_packets",
                "additional_data_packets",
                "total_packets",
            }
            for index, queue in enumerate(queues, start=1):
                label = f"{phase}: queues_terminal queue {index}"
                if not isinstance(queue, dict) or set(queue) != {
                    "device_id",
                    "before_depths",
                    "after_depths",
                    "flushed_packets",
                }:
                    failures.append(f"{label} fields differ from exact queue terminal contract")
                    continue
                device_id = queue.get("device_id")
                if not isinstance(device_id, str) or not device_id or device_id in device_ids:
                    failures.append(f"{label} device_id is absent or duplicated")
                if isinstance(device_id, str):
                    device_ids.add(device_id)
                before = queue.get("before_depths")
                after = queue.get("after_depths")
                if not isinstance(before, dict) or set(before) != depth_keys:
                    failures.append(f"{label} before_depths are invalid")
                    continue
                if not isinstance(after, dict) or set(after) != depth_keys:
                    failures.append(f"{label} after_depths are invalid")
                    continue
                if any(not _is_int(before[key]) or before[key] < 0 for key in depth_keys):
                    failures.append(f"{label} before_depths contain invalid counters")
                elif before["total_packets"] != sum(
                    before[key]
                    for key in (
                        "control_packets",
                        "payload_packets",
                        "additional_data_packets",
                    )
                ):
                    failures.append(f"{label} before_depths total is inconsistent")
                if any(after.get(key) != 0 or not _is_int(after.get(key)) for key in depth_keys):
                    failures.append(f"{label} after_depths are not terminal zeroes")
                if queue.get("flushed_packets") != before.get("total_packets") or not _is_int(
                    queue.get("flushed_packets")
                ):
                    failures.append(f"{label} flushed_packets does not equal actual pre-stop depth")

        runner_ready_name, runner_stop_name, runner_stopped_name = runner_events[phase]
        runner_ready = (runner_lifecycle.get(runner_ready_name) or {}).get("monotonic_ns")
        runner_stop = (runner_lifecycle.get(runner_stop_name) or {}).get("monotonic_ns")
        runner_stopped = (runner_lifecycle.get(runner_stopped_name) or {}).get("monotonic_ns")
        if not (
            _is_int(ready.get("host_monotonic_ns"))
            and _is_int(runner_ready)
            and ready["host_monotonic_ns"] <= runner_ready
        ):
            failures.append(f"{phase}: C++ ready record does not precede runner readiness")
        if not (
            _is_int(runner_stop)
            and _is_int(stop_observed.get("host_monotonic_ns"))
            and runner_stop <= stop_observed["host_monotonic_ns"]
        ):
            failures.append(f"{phase}: C++ stop observation does not follow runner stop request")
        if not (
            _is_int(stopped.get("host_monotonic_ns"))
            and _is_int(runner_stopped)
            and stopped["host_monotonic_ns"] <= runner_stopped
        ):
            failures.append(f"{phase}: runner records engine stopped before C++ terminal lifecycle")
        details["phases"][phase] = {
            "records": len(records),
            "ready_host_monotonic_ns": ready.get("host_monotonic_ns"),
            "stopped_host_monotonic_ns": stopped.get("host_monotonic_ns"),
        }
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


def _load_lifecycle_records(
    path: Path,
    metadata: dict[str, Any],
    *,
    schema: str,
    schema_version: int | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Load a non-probe lifecycle JSONL with its own durable envelope."""

    failures: list[str] = []
    records: list[dict[str, Any]] = []
    if not path.is_file() or path.is_symlink():
        return records, [f"lifecycle log is missing/nonregular: {path}"]
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        return records, [f"cannot read lifecycle log {path}: {exc}"]
    if not lines:
        return records, [f"lifecycle log is empty: {path}"]
    previous_monotonic: int | None = None
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            failures.append(f"{path}:{line_number}: blank JSONL record")
            continue
        try:
            record = _strict_json_loads(line)
        except (ValueError, json.JSONDecodeError) as exc:
            failures.append(f"{path}:{line_number}: invalid strict JSON: {exc}")
            continue
        if not isinstance(record, dict):
            failures.append(f"{path}:{line_number}: record is not an object")
            continue
        label = f"{path}:{line_number}"
        if record.get("schema") != schema:
            failures.append(f"{label}: schema is not {schema!r}")
        if schema_version is not None and record.get("schema_version") != schema_version:
            failures.append(f"{label}: schema_version is not {schema_version}")
        if (
            record.get("run_id"),
            record.get("runtime_id"),
            record.get("run_nonce"),
        ) != (path.parents[1].name, metadata.get("runtime_id"), metadata.get("run_nonce")):
            failures.append(f"{label}: run/runtime/nonce identity mismatch")
        if record.get("event_seq") != line_number or not _is_int(record.get("event_seq")):
            failures.append(f"{label}: event_seq must be contiguous from one")
        monotonic = record.get("monotonic_ns")
        if not _is_int(monotonic) or monotonic <= 0:
            failures.append(f"{label}: monotonic_ns is invalid")
        elif previous_monotonic is not None and monotonic <= previous_monotonic:
            failures.append(f"{label}: monotonic_ns is not strictly increasing")
        if _is_int(monotonic):
            previous_monotonic = monotonic
        if not _parse_utc(record.get("wall_utc")):
            failures.append(f"{label}: wall_utc is invalid")
        if not isinstance(record.get("event"), str) or not record.get("event"):
            failures.append(f"{label}: event is invalid")
        records.append(record)
    return records, failures


def _lifecycle_gate(
    run_dir: Path,
    metadata: dict[str, Any],
    windows: dict[str, tuple[int, int]],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    path = run_dir / "logs/m2_lifecycle.jsonl"
    records, failures = _load_lifecycle_records(
        path,
        metadata,
        schema=LIFECYCLE_SCHEMA,
        schema_version=None,
    )
    by_event: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        by_event[str(record.get("event"))].append(record)
    ordered: dict[str, dict[str, Any]] = {}
    previous_sequence = 0
    for event in LIFECYCLE_REQUIRED_ORDER:
        occurrences = by_event.get(event, [])
        if len(occurrences) != 1:
            failures.append(f"lifecycle event {event!r} occurs {len(occurrences)} time(s), expected one")
            continue
        record = occurrences[0]
        sequence = record.get("event_seq")
        if not _is_int(sequence) or sequence <= previous_sequence:
            failures.append(f"lifecycle event {event!r} is out of required order")
        else:
            previous_sequence = sequence
        ordered[event] = record

    def timestamp(event: str) -> int | None:
        value = (ordered.get(event) or {}).get("monotonic_ns")
        return value if _is_int(value) else None

    def require_before(left: str, right: str) -> None:
        left_ns, right_ns = timestamp(left), timestamp(right)
        if left_ns is None or right_ns is None or left_ns >= right_ns:
            failures.append(f"lifecycle ordering requires {left} < {right}")

    def require_dwell(start: str, complete: str, minimum_ns: int = MIN_READINESS_DWELL_NS) -> None:
        start_ns, complete_ns = timestamp(start), timestamp(complete)
        if start_ns is None or complete_ns is None or complete_ns - start_ns < minimum_ns:
            failures.append(
                f"lifecycle dwell {start}->{complete} is shorter than {minimum_ns // 1_000_000_000}s"
            )

    for left, right in zip(LIFECYCLE_REQUIRED_ORDER, LIFECYCLE_REQUIRED_ORDER[1:]):
        require_before(left, right)
    require_dwell("good_dwell_start", "good_dwell_complete")
    require_dwell("prestop_dwell_start", "prestop_dwell_complete")
    require_dwell("recovery_dwell_start", "recovery_dwell_complete")
    require_dwell("recovery_prestop_dwell_start", "recovery_prestop_dwell_complete")
    require_dwell("stopped_drain_start", "stopped_drain_complete", 1_000_000_000)

    if (ordered.get("captures_ready") or {}).get("capture_count") != 5:
        failures.append("captures_ready.capture_count must equal five persistent collectors")
    if (ordered.get("endpoints_ready") or {}).get("persistent_capture_count") != 5:
        failures.append("endpoints_ready.persistent_capture_count must equal five")
    if (ordered.get("endpoints_ready") or {}).get("stable_process_count") != 5:
        failures.append("endpoints_ready.stable_process_count must include the persistent GCS endpoint")

    phase_boundaries = {
        "good": ("good_start", "good_terminal"),
        "down": ("down_start", "down_terminal"),
        "recovery": ("recovery_start", "recovery_terminal"),
    }
    for phase, (begin_event, end_event) in phase_boundaries.items():
        window = windows.get(phase)
        begin_ns, end_ns = timestamp(begin_event), timestamp(end_event)
        if window is None or begin_ns is None or end_ns is None:
            failures.append(f"lifecycle/{phase}: phase or lifecycle boundary is missing")
            continue
        if not (begin_ns <= window[0] < window[1] <= end_ns):
            failures.append(f"lifecycle/{phase}: probe window is not contained by lifecycle boundaries")
    engine1_stopped = timestamp("engine1_stopped")
    down_window = windows.get("down")
    if engine1_stopped is None or down_window is None or engine1_stopped >= down_window[0]:
        failures.append("lifecycle: engine1 is not proven stopped before the down phase")
    engine2_ready = timestamp("engine2_ready")
    recovery_window = windows.get("recovery")
    if engine2_ready is None or recovery_window is None or engine2_ready >= recovery_window[0]:
        failures.append("lifecycle: engine2 is not proven ready before recovery")
    return _result(
        failures,
        path=str(path),
        events={event: ordered[event].get("monotonic_ns") for event in ordered},
    ), ordered


def _monitor_gate(
    run_dir: Path,
    metadata: dict[str, Any],
    lifecycle: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    path = run_dir / "logs/m2_monitor.jsonl"
    records, failures = _load_lifecycle_records(
        path,
        metadata,
        schema=MONITOR_SCHEMA,
        schema_version=1,
    )
    starts = [record for record in records if record.get("event") == "monitor_start"]
    stops = [record for record in records if record.get("event") == "monitor_stop"]
    samples = [record for record in records if record.get("event") == "sample"]
    if len(starts) != 1 or not records or records[0] is not starts[0]:
        failures.append("monitor must begin with exactly one monitor_start")
    if len(stops) != 1 or not records or records[-1] is not stops[0]:
        failures.append("monitor must end with exactly one monitor_stop")
    elif stops[0].get("reason") != "stop_file":
        failures.append("monitor must terminate through its stop-file protocol")
    elif stops[0].get("all_roles_alive") is not True:
        failures.append("monitor_stop must retain all persistent roles alive")
    if not samples:
        failures.append("monitor has no samples")
    expected_roles = {
        "launch",
        "sitl",
        "mavproxy",
        "adapter",
        "gcs_endpoint",
        "capture_tail",
        "capture_gcs",
        "capture_ns3_external_gcs",
        "capture_ns3_external_uav",
        "capture_uav",
    }
    previous_sample_ns: int | None = None
    identities: dict[str, tuple[Any, Any, Any]] = {}
    for sample in samples:
        sample_ns = sample.get("monotonic_ns")
        if previous_sample_ns is not None and (
            not _is_int(sample_ns) or sample_ns - previous_sample_ns > MONITOR_MAX_SAMPLE_GAP_NS
        ):
            failures.append("monitor sample gap exceeds 1.5 seconds")
        if _is_int(sample_ns):
            previous_sample_ns = sample_ns
        if sample.get("all_roles_alive") is not True:
            failures.append(f"monitor sample {sample.get('event_seq')} has a dead stable role")
        roles = sample.get("roles") if isinstance(sample.get("roles"), dict) else {}
        if set(roles) != expected_roles:
            failures.append(f"monitor sample {sample.get('event_seq')} roles are not the exact persistent set")
        for role in expected_roles:
            observed = roles.get(role) if isinstance(roles.get(role), dict) else {}
            if observed.get("alive") is not True or observed.get("identity_match") is not True:
                failures.append(f"monitor sample {sample.get('event_seq')} role {role} is not exact/alive")
                continue
            identity = (
                observed.get("expected_pid"),
                observed.get("expected_start_ticks"),
                observed.get("expected_cmdline_sha256"),
            )
            if role in identities and identities[role] != identity:
                failures.append(f"monitor role {role} identity changed across samples")
            identities[role] = identity
        topology = sample.get("topology") if isinstance(sample.get("topology"), dict) else {}
        if not (
            topology.get("exists") is True
            and topology.get("regular") is True
            and topology.get("matches_declared") is True
        ):
            failures.append(f"monitor sample {sample.get('event_seq')} topology identity is not stable")

    def lifecycle_ns(event: str) -> int | None:
        value = (lifecycle.get(event) or {}).get("monotonic_ns")
        return value if _is_int(value) else None

    monitor_start_ns = starts[0].get("monotonic_ns") if len(starts) == 1 else None
    monitor_stop_ns = stops[0].get("monotonic_ns") if len(stops) == 1 else None
    engine1_ready = lifecycle_ns("engine1_ready")
    engine2_stopped = lifecycle_ns("engine2_stopped")
    if not _is_int(monitor_start_ns) or engine1_ready is None or monitor_start_ns >= engine1_ready:
        failures.append("monitor did not start before engine1 readiness")
    if not _is_int(monitor_stop_ns) or engine2_stopped is None or monitor_stop_ns <= engine2_stopped:
        failures.append("monitor did not remain alive through engine2 stop")
    for suffix in ("stdout", "stderr"):
        stream = run_dir / f"logs/m2_lifecycle_monitor.{suffix}"
        if not stream.is_file() or stream.is_symlink() or stream.stat().st_size != 0:
            failures.append(f"m2_lifecycle_monitor.{suffix} is absent, symlinked, or nonempty")
    stop_file = run_dir / "logs/m2_monitor.stop"
    if not stop_file.is_file() or stop_file.is_symlink() or stop_file.stat().st_size == 0:
        failures.append("m2_monitor.stop is absent, symlinked, or empty")
    return _result(
        failures,
        path=str(path),
        sample_count=len(samples),
        stable_roles=sorted(identities),
    )


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
        "datagram_tx",
        "datagram_rx",
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
        timestamp_ns = record["monotonic_ns"]
        if record.get("event") == "phase_start":
            in_declared_interval = timestamp_ns == start_ns
        elif record.get("event") == "phase_end":
            # ``phase_end`` is the boundary marker itself.  Traffic belongs to
            # the half-open interval immediately preceding it, while a record
            # at the boundary never counts as traffic in that interval.
            in_declared_interval = timestamp_ns == end_ns
        else:
            in_declared_interval = start_ns <= timestamp_ns < end_ns
        if not in_declared_interval:
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


def _expected_transaction_id(record: dict[str, Any]) -> str | None:
    """Recompute the probe's canonical v2 transaction identity.

    This is deliberately repeated in the independent validator instead of
    trusting an opaque producer label.  Every field in the canonical mapping
    names a concrete command occurrence and makes a copied marker/command
    pair from another phase or run fail closed.
    """

    keys = (
        "run_nonce",
        "phase",
        "attempt",
        "marker_sha256",
        "command_sha256",
        "mavlink_seq",
        "source_system",
        "source_component",
        "target_system",
        "target_component",
        "mavlink_command",
    )
    if any(key not in record for key in keys):
        return None
    identity = {
        "attempt": record["attempt"],
        "command_sha256": record["command_sha256"],
        "marker_sha256": record["marker_sha256"],
        "mavlink_command": record["mavlink_command"],
        "mavlink_seq": record["mavlink_seq"],
        "phase": record["phase"],
        "run_nonce": record["run_nonce"],
        "source_component": record["source_component"],
        "source_system": record["source_system"],
        "target_component": record["target_component"],
        "target_system": record["target_system"],
    }
    try:
        encoded = json.dumps(identity, allow_nan=False, sort_keys=True, separators=(",", ":")).encode(
            "ascii"
        )
    except (TypeError, ValueError, UnicodeEncodeError):
        return None
    return hashlib.sha256(encoded).hexdigest()


def _raw_probe_occurrence_gate(
    records: list[dict[str, Any]],
    *,
    phase: str,
    attempts: dict[int, dict[str, Any]],
    failures: list[str],
) -> dict[str, Any]:
    """Validate probe-side datagram occurrences independently of annotations.

    ``command_attempt`` is a durable intent record, while ``datagram_tx`` and
    ``datagram_rx`` describe the actual UDP occurrences.  The latter are
    emitted immediately around ``sendto``/``recvfrom`` and are the only
    admissible anchors for adapter causal matching below.
    """

    phase_records = [record for record in records if record.get("phase") == phase]
    tx_records = [record for record in phase_records if record.get("event") == "datagram_tx"]
    rx_records = [record for record in phase_records if record.get("event") == "datagram_rx"]
    expected_count = len(attempts) * 2
    if len(tx_records) != expected_count:
        failures.append(
            f"{phase}: raw datagram_tx records are {len(tx_records)}, expected {expected_count}"
        )

    tx_by_transaction: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    previous_tx_sequence = 0
    for tx in tx_records:
        label = f"{phase}: datagram_tx event_seq {tx.get('event_seq')}"
        if tx.get("event_schema") != PROBE_RAW_EVENT_SCHEMA:
            failures.append(f"{label} has wrong event_schema")
        tx_sequence = tx.get("tx_datagram_seq")
        if not _is_int(tx_sequence) or tx_sequence <= previous_tx_sequence:
            failures.append(f"{label} tx_datagram_seq is not strictly increasing within its phase")
        elif previous_tx_sequence and tx_sequence != previous_tx_sequence + 1:
            failures.append(f"{label} tx_datagram_seq has an in-phase gap")
        if _is_int(tx_sequence):
            previous_tx_sequence = tx_sequence
        transaction_id = tx.get("transaction_id")
        if not _is_sha256(transaction_id):
            failures.append(f"{label} transaction_id is invalid")
            continue
        leg = tx.get("leg")
        if leg not in ("marker", "command"):
            failures.append(f"{label} leg is invalid")
            continue
        if leg in tx_by_transaction[transaction_id]:
            failures.append(f"{label} duplicates {leg} occurrence for transaction")
            continue
        tx_by_transaction[transaction_id][leg] = tx
        payload_hash = tx.get("transport_payload_sha256")
        payload_size = tx.get("transport_payload_size")
        bytes_sent = tx.get("bytes_sent")
        send_start = tx.get("send_start_monotonic_ns")
        send_complete = tx.get("send_complete_monotonic_ns")
        if not _is_sha256(payload_hash):
            failures.append(f"{label} transport_payload_sha256 is invalid")
        if not _is_int(payload_size) or payload_size <= 0:
            failures.append(f"{label} transport_payload_size is invalid")
        if bytes_sent != payload_size or not _is_int(bytes_sent):
            failures.append(f"{label} bytes_sent does not equal transport_payload_size")
        if not _is_int(send_start) or not _is_int(send_complete) or not _is_int(tx.get("monotonic_ns")):
            failures.append(f"{label} send timestamps are invalid")
        elif not (send_start <= send_complete <= tx["monotonic_ns"]):
            failures.append(f"{label} send timestamps are not ordered through durable emission")

    rx_by_seq: dict[int, dict[str, Any]] = {}
    previous_rx_sequence = 0
    for rx in rx_records:
        label = f"{phase}: datagram_rx event_seq {rx.get('event_seq')}"
        if rx.get("event_schema") != PROBE_RAW_EVENT_SCHEMA:
            failures.append(f"{label} has wrong event_schema")
        sequence = rx.get("rx_datagram_seq")
        if not _is_int(sequence) or sequence <= previous_rx_sequence:
            failures.append(f"{label} rx_datagram_seq is not strictly increasing within its phase")
            continue
        if previous_rx_sequence and sequence != previous_rx_sequence + 1:
            failures.append(f"{label} rx_datagram_seq has an in-phase gap")
        previous_rx_sequence = sequence
        rx_by_seq[sequence] = rx
        payload_hash = rx.get("transport_payload_sha256")
        payload_size = rx.get("transport_payload_size")
        received = rx.get("received_monotonic_ns")
        peer = rx.get("peer")
        if not _is_sha256(payload_hash):
            failures.append(f"{label} transport_payload_sha256 is invalid")
        if not _is_int(payload_size) or payload_size <= 0:
            failures.append(f"{label} transport_payload_size is invalid")
        if not _is_int(received) or not _is_int(rx.get("monotonic_ns")) or received > rx.get("monotonic_ns", -1):
            failures.append(f"{label} received timestamp is invalid or postdates durable emission")
        if not isinstance(peer, list) or len(peer) != 2 or not isinstance(peer[0], str) or not _is_int(peer[1]):
            failures.append(f"{label} peer is invalid")

    raw_transactions: dict[int, dict[str, Any]] = {}
    for attempt_number, attempt in attempts.items():
        label = f"{phase}/{attempt_number}"
        if attempt.get("source_system") != 255 or not _is_int(attempt.get("source_system")):
            failures.append(f"{label}: source_system must be integer 255")
        if attempt.get("source_component") != 190 or not _is_int(attempt.get("source_component")):
            failures.append(f"{label}: source_component must be integer 190")
        expected_id = _expected_transaction_id(attempt)
        transaction_id = attempt.get("transaction_id")
        if expected_id is None or not _is_sha256(transaction_id) or transaction_id != expected_id:
            failures.append(f"{label}: command_attempt transaction_id is not canonical")
            continue
        entries = tx_by_transaction.get(transaction_id, {})
        marker = entries.get("marker")
        command = entries.get("command")
        if marker is None or command is None:
            failures.append(f"{label}: marker/command raw sends are incomplete")
            continue
        if marker.get("attempt") != attempt_number or command.get("attempt") != attempt_number:
            failures.append(f"{label}: raw send attempt does not match command_attempt")
        if marker.get("nonce") != attempt.get("nonce") or command.get("nonce") != attempt.get("nonce"):
            failures.append(f"{label}: raw send nonce does not match command_attempt")
        if marker.get("transport_payload_sha256") != attempt.get("marker_sha256"):
            failures.append(f"{label}: marker raw payload hash does not match command_attempt")
        if command.get("transport_payload_sha256") != attempt.get("command_sha256"):
            failures.append(f"{label}: command raw payload hash does not match command_attempt")
        attempt_ns = attempt.get("monotonic_ns")
        if not _is_int(attempt_ns):
            failures.append(f"{label}: command_attempt monotonic timestamp is invalid")
        else:
            marker_start = marker.get("send_start_monotonic_ns")
            command_start = command.get("send_start_monotonic_ns")
            if not _is_int(marker_start) or not _is_int(command_start) or not (
                attempt_ns < marker_start <= command_start
            ):
                failures.append(f"{label}: raw send boundaries do not follow command_attempt")
        if (
            marker.get("event_seq") != attempt.get("event_seq", 0) + 1
            or command.get("event_seq") != attempt.get("event_seq", 0) + 2
        ):
            failures.append(f"{label}: command_attempt is not immediately followed by marker/command raw sends")
        raw_transactions[attempt_number] = {
            "transaction_id": transaction_id,
            "marker": marker,
            "command": command,
        }

    decoded_events = [
        record
        for record in phase_records
        if record.get("event") in {"heartbeat", "command_ack", "telemetry"}
    ]
    decoded_references: set[tuple[int, int]] = set()
    for record in decoded_events:
        label = f"{phase}: decoded {record.get('event')} event_seq {record.get('event_seq')}"
        rx_sequence = record.get("rx_datagram_seq")
        frame_index = record.get("frame_index")
        if not _is_int(rx_sequence) or rx_sequence not in rx_by_seq:
            failures.append(f"{label} does not reference one raw datagram_rx occurrence")
            continue
        if not _is_int(frame_index) or frame_index < 1:
            failures.append(f"{label} frame_index is invalid")
            continue
        reference = (rx_sequence, frame_index)
        if reference in decoded_references:
            failures.append(f"{label} duplicates a decoded raw frame reference")
        decoded_references.add(reference)
        rx = rx_by_seq[rx_sequence]
        if record.get("packet_sha256") != rx.get("transport_payload_sha256"):
            failures.append(f"{label} packet SHA does not equal referenced raw datagram")
        if record.get("peer") != rx.get("peer"):
            failures.append(f"{label} peer does not equal referenced raw datagram")
        if record.get("event") in {"command_ack", "telemetry"}:
            attempt_number = record.get("attempt")
            transaction = raw_transactions.get(attempt_number) if _is_int(attempt_number) else None
            if transaction is None or record.get("transaction_id") != transaction.get("transaction_id"):
                failures.append(f"{label} does not bind to its canonical command transaction")

    return {
        "transactions": raw_transactions,
        "tx": tx_records,
        "rx": rx_by_seq,
        "decoded": decoded_events,
    }


def _persistent_endpoint_gate(
    records: list[dict[str, Any]],
    *,
    phase_evidence: dict[str, Any],
) -> tuple[list[str], dict[str, Any]]:
    """Verify that all M2 traffic came from one surviving GCS UDP endpoint.

    The persistent endpoint owns its UDP socket and global raw occurrence
    counters.  This gate deliberately validates that producer-owned boundary
    before phase-local delivery facts are used by the rest of M2: otherwise a
    new socket could make a queued old heartbeat appear to belong to recovery.
    """

    failures: list[str] = []
    if not records:
        return ["persistent endpoint event log is empty"], {}

    expected_configuration = {
        "gcs_bind": ["10.71.0.10", 14600],
        "uav_endpoint": ["10.71.1.10", 14601],
        "target_system": 1,
        "target_component": 1,
        "source_system": 255,
        "source_component": 190,
    }
    expected_configuration_sha256 = hashlib.sha256(
        json.dumps(expected_configuration, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    instance_ids = {record.get("endpoint_instance_id") for record in records}
    generations = {record.get("endpoint_generation") for record in records}
    configuration_hashes = {record.get("endpoint_configuration_sha256") for record in records}
    for record in records:
        label = f"persistent endpoint event_seq {record.get('event_seq')}"
        if record.get("endpoint_event_schema") != PERSISTENT_ENDPOINT_EVENT_SCHEMA:
            failures.append(f"{label} lacks the persistent endpoint schema envelope")
        event = record.get("event")
        if event in {"datagram_tx", "datagram_rx"}:
            if record.get("event_schema") != PROBE_RAW_EVENT_SCHEMA:
                failures.append(f"{label} raw datagram has the wrong event schema")
        elif record.get("event_schema") != PERSISTENT_ENDPOINT_EVENT_SCHEMA:
            failures.append(f"{label} non-raw endpoint event has the wrong event schema")
    if len(instance_ids) != 1 or not _is_sha256(next(iter(instance_ids), None)):
        failures.append("persistent endpoint instance identity is not one SHA-256 value")
    if generations != {1}:
        failures.append("persistent endpoint generation must remain exactly one")
    if configuration_hashes != {expected_configuration_sha256}:
        failures.append("persistent endpoint configuration hash is not the exact M2 GCS/UAV contract")

    started = [record for record in records if record.get("event") == "endpoint_started"]
    stopped = [record for record in records if record.get("event") == "endpoint_stopped"]
    if len(started) != 1:
        failures.append(f"persistent endpoint_started occurs {len(started)} time(s), expected one")
        start = {}
    else:
        start = started[0]
        if start.get("endpoint_configuration") != expected_configuration:
            failures.append("endpoint_started configuration is not the exact M2 endpoint contract")
        if start.get("gcs_bind") != expected_configuration["gcs_bind"]:
            failures.append("endpoint_started bound an unexpected GCS UDP endpoint")
        if not _is_int(start.get("endpoint_pid")) or start.get("endpoint_pid", 0) <= 1:
            failures.append("endpoint_started endpoint_pid is invalid")
        if not _is_int(start.get("endpoint_uid")) or start.get("endpoint_uid", -1) < 0:
            failures.append("endpoint_started endpoint_uid is invalid")
    if len(stopped) != 1:
        failures.append(f"persistent endpoint_stopped occurs {len(stopped)} time(s), expected one")
        stop = {}
    else:
        stop = stopped[0]
        if stop.get("completed_phases") != list(PHASES):
            failures.append("endpoint_stopped does not record the complete good/down/recovery lifecycle")
        if stop.get("lifecycle_complete") is not True or stop.get("phase_failed") is not False:
            failures.append("endpoint_stopped does not record a successful complete lifecycle")
        if not _is_int(stop.get("tx_datagram_seq")) or not _is_int(stop.get("rx_datagram_seq")):
            failures.append("endpoint_stopped raw sequence totals are invalid")
    if started and stopped and started[0].get("event_seq", 0) >= stopped[0].get("event_seq", 0):
        failures.append("persistent endpoint did not stop after it started")
    rejected = [
        record
        for record in records
        if record.get("event") in {"endpoint_control_rejected", "endpoint_window_abort"}
    ]
    if rejected:
        failures.append("persistent endpoint recorded rejected control or aborted window evidence")

    raw_tx = [record for record in records if record.get("event") == "datagram_tx"]
    raw_rx = [
        record
        for record in records
        if record.get("event") in {"datagram_rx", "endpoint_pre_window_datagram"}
    ]
    tx_sequences = [record.get("tx_datagram_seq") for record in raw_tx]
    rx_sequences = [record.get("rx_datagram_seq") for record in raw_rx]
    if tx_sequences != list(range(1, len(raw_tx) + 1)):
        failures.append("persistent endpoint TX occurrence sequence is not globally contiguous from one")
    if rx_sequences != list(range(1, len(raw_rx) + 1)):
        failures.append("persistent endpoint RX occurrence sequence is not globally contiguous from one")
    if stopped:
        if stop.get("tx_datagram_seq") != len(raw_tx):
            failures.append("endpoint_stopped TX total does not equal global raw TX occurrences")
        if stop.get("rx_datagram_seq") != len(raw_rx):
            failures.append("endpoint_stopped RX total does not equal global raw RX occurrences")
    for record in raw_tx + [record for record in raw_rx if record.get("event") == "datagram_rx"]:
        if not isinstance(record.get("endpoint_window_id"), str):
            failures.append(
                f"raw endpoint traffic event_seq {record.get('event_seq')} is outside a phase window"
            )
    for record in raw_rx:
        if record.get("event") != "endpoint_pre_window_datagram":
            continue
        if record.get("disposition") != "discarded_before_window":
            failures.append("pre-window UDP datagram was not explicitly discarded")
        phase = record.get("pre_window_for_phase")
        if phase not in PHASES or record.get("phase") != phase:
            failures.append("pre-window UDP datagram has no canonical target phase")
        if "endpoint_window_id" in record:
            failures.append("pre-window UDP datagram incorrectly belongs to a phase window")

    window_details: dict[str, dict[str, Any]] = {}
    for index, phase in enumerate(PHASES, start=1):
        window_id = f"{index}-{phase}"
        opens = [
            record
            for record in records
            if record.get("event") == "endpoint_window_open"
            and record.get("phase") == phase
            and record.get("window_id") == window_id
        ]
        closes = [
            record
            for record in records
            if record.get("event") == "endpoint_window_close"
            and record.get("phase") == phase
            and record.get("window_id") == window_id
        ]
        quiescent = [
            record
            for record in records
            if record.get("event") == "endpoint_pre_window_quiescent"
            and record.get("pre_window_for_phase") == phase
        ]
        if len(opens) != 1 or len(closes) != 1:
            failures.append(f"{phase}: persistent endpoint requires exactly one open and close window")
            continue
        if len(quiescent) != 1:
            failures.append(f"{phase}: persistent endpoint requires exactly one pre-window quiescence record")
        open_record, close_record = opens[0], closes[0]
        if open_record.get("endpoint_window_id") != window_id or close_record.get("endpoint_window_id") != window_id:
            failures.append(f"{phase}: endpoint window envelope ID is inconsistent")
        if open_record.get("tx_datagram_seq_before") is None or open_record.get("rx_datagram_seq_before") is None:
            failures.append(f"{phase}: endpoint window lacks pre-window raw sequence boundaries")
        if close_record.get("tx_datagram_seq_after") is None or close_record.get("rx_datagram_seq_after") is None:
            failures.append(f"{phase}: endpoint window lacks post-window raw sequence boundaries")
        phase_window = phase_evidence.get(phase, {}).get("window")
        phase_start = next(
            (record for record in records if record.get("phase") == phase and record.get("event") == "phase_start"),
            None,
        )
        phase_end = next(
            (record for record in records if record.get("phase") == phase and record.get("event") == "phase_end"),
            None,
        )
        if phase_start is None or phase_end is None:
            failures.append(f"{phase}: persistent endpoint cannot bind a missing phase boundary")
        else:
            if phase_start.get("endpoint_window_id") != window_id or phase_end.get("endpoint_window_id") != window_id:
                failures.append(f"{phase}: phase boundaries are not bound to their persistent endpoint window")
            if not (
                _is_int(open_record.get("monotonic_ns"))
                and _is_int(phase_start.get("monotonic_ns"))
                and _is_int(phase_end.get("monotonic_ns"))
                and _is_int(close_record.get("monotonic_ns"))
                and open_record["monotonic_ns"] <= phase_start["monotonic_ns"]
                < phase_end["monotonic_ns"] <= close_record["monotonic_ns"]
            ):
                failures.append(f"{phase}: persistent endpoint window does not contain its phase interval")
            if phase_window and tuple(phase_window) != (
                phase_start.get("monotonic_ns"),
                phase_end.get("monotonic_ns"),
            ):
                failures.append(f"{phase}: phase evidence window disagrees with raw phase boundaries")
        for record in records:
            if record.get("phase") != phase:
                continue
            event = record.get("event")
            requires_window = event in {
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
            if requires_window and record.get("endpoint_window_id") != window_id:
                failures.append(
                    f"{phase}: {event} event_seq {record.get('event_seq')} is outside its persistent endpoint window"
                )
        window_details[phase] = {
            "window_id": window_id,
            "open_event_seq": open_record.get("event_seq"),
            "close_event_seq": close_record.get("event_seq"),
        }
    return failures, {
        "endpoint_instance_id": next(iter(instance_ids), None),
        "endpoint_configuration_sha256": next(iter(configuration_hashes), None),
        "raw_tx_occurrences": len(raw_tx),
        "raw_rx_occurrences": len(raw_rx),
        "windows": window_details,
    }


def _probe_gate(
    run_dir: Path, metadata: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, tuple[int, int]], dict[str, Any]]:
    path = run_dir / "logs/m2_probe_events.jsonl"
    records, failures = _load_event_log(path, metadata, require_phase=True)
    windows, window_failures = _phase_windows(records)
    failures.extend(window_failures)
    phase_evidence: dict[str, Any] = {}
    run_nonce = metadata.get("run_nonce") if isinstance(metadata.get("run_nonce"), str) else ""
    all_marker_hashes: list[str] = []
    all_command_hashes: list[str] = []

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
        all_marker_hashes.extend(value for value in marker_hashes if _is_sha256(value))
        all_command_hashes.extend(value for value in command_hashes if _is_sha256(value))

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
            if len(heartbeats) < MIN_POSITIVE_HEARTBEATS:
                failures.append(
                    f"{phase}: decoded heartbeats are {len(heartbeats)}, "
                    f"expected at least {MIN_POSITIVE_HEARTBEATS}"
                )
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

        raw_occurrences = _raw_probe_occurrence_gate(
            records,
            phase=phase,
            attempts=attempts,
            failures=failures,
        )
        phase_evidence[phase] = {
            "window": windows.get(phase),
            "attempts": attempts,
            "acks": acknowledgements,
            "telemetry": dict(telemetry_by_attempt),
            "heartbeats": heartbeats,
            "timeouts": timeouts,
            "direct_probes": direct_probes,
            "endpoint_health": endpoint_health,
            "raw": raw_occurrences,
        }

    # The down no-bypass gate searches whole-lifecycle PCAPs.  Therefore an
    # exact command frame from recovery must never be able to impersonate a
    # stopped-phase offer merely because an encoder restarted at a phase
    # boundary.  Keep every outbound frame identity unique for the complete
    # good -> down -> recovery lifecycle.
    if len(all_marker_hashes) != len(set(all_marker_hashes)):
        failures.append("marker frame hashes are not globally unique across M2 phases")
    if len(all_command_hashes) != len(set(all_command_hashes)):
        failures.append("command frame hashes are not globally unique across M2 phases")
    all_outbound_hashes = all_marker_hashes + all_command_hashes
    if len(all_outbound_hashes) != len(set(all_outbound_hashes)):
        failures.append("outbound marker/command frame hashes are not globally unique across M2 phases")

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
    persistent_failures, persistent_details = _persistent_endpoint_gate(
        records,
        phase_evidence=phase_evidence,
    )
    failures.extend(persistent_failures)
    return (
        _result(failures, phases=details, persistent_endpoint=persistent_details),
        windows,
        phase_evidence,
    )


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

    def parse_capture(relative: str) -> dict[str, Any]:
        parsed, parse_failures = _parse_pcap(run_dir / relative)
        failures.extend(parse_failures)
        stats[relative] = {
            key: value
            for key, value in parsed.items()
            if key not in {"payload_hashes", "payload_by_hash"}
        }
        return parsed

    def require_payloads(
        relative: str,
        parsed: dict[str, Any],
        required: Counter[str],
        marker_nonces: dict[str, str],
        *,
        description: str = "payload",
    ) -> None:
        observed: Counter[str] = parsed.get("payload_hashes", Counter())
        for payload_hash, count in required.items():
            if observed[payload_hash] < count:
                failures.append(
                    f"{relative}: {description} {payload_hash} observed "
                    f"{observed[payload_hash]} time(s), expected at least {count}"
                )
        for marker_hash, nonce in marker_nonces.items():
            marker_payloads = parsed.get("payload_by_hash", {}).get(marker_hash, [])
            if marker_payloads and not any(
                nonce.encode("utf-8") in payload for payload in marker_payloads
            ):
                failures.append(
                    f"{relative}: marker payload {marker_hash} does not contain its nonce"
                )

    forbidden_phase_local = sorted(
        candidate.relative_to(run_dir).as_posix()
        for candidate in (run_dir / "pcap").rglob("*.pcap")
        if PHASE_LOCAL_PERSISTENT_CAPTURE_RE.fullmatch(
            candidate.relative_to(run_dir).as_posix()
        )
    )
    if forbidden_phase_local:
        failures.append(
            "phase-local persistent capture filenames are forbidden: "
            f"{forbidden_phase_local}"
        )

    # Each external collector is a single lifetime PCAP.  Its content must
    # carry both positive epochs; the phase-local files below are only the
    # packet engine's internal ns-3 captures.
    persistent = {
        relative: parse_capture(relative) for relative in PERSISTENT_CAPTURE_PCAPS
    }
    persistent_hashes = [
        parsed["file_sha256"]
        for parsed in persistent.values()
        if parsed.get("file_sha256")
    ]
    if (
        len(persistent_hashes) == len(PERSISTENT_CAPTURE_PCAPS)
        and len(set(persistent_hashes)) != len(PERSISTENT_CAPTURE_PCAPS)
    ):
        failures.append(
            "persistent capture PCAP files are byte-identical; copied evidence is not accepted"
        )

    for phase in ("good", "recovery"):
        required, marker_nonces = _required_payloads(phase_evidence.get(phase, {}))
        for relative, parsed in persistent.items():
            require_payloads(relative, parsed, required, marker_nonces)
        for point in ENGINE_CAPTURE_POINTS:
            relative = f"pcap/{point}_{phase}.pcap"
            require_payloads(
                relative,
                parse_capture(relative),
                required,
                marker_nonces,
            )

    down_required, down_markers = _required_payloads(phase_evidence.get("down", {}))
    # Down has no ACK/telemetry/heartbeat records, so this counter contains only
    # the nonce marker and command frame for each attempted transaction.  The
    # persistent GCS and ns-3 ingress collectors must see those offers, while
    # the persistent UAV interface must not see them.
    for relative in (
        "pcap/gcs_ingress.pcap",
        "pcap/ns3_external_ingress.pcap",
    ):
        require_payloads(
            relative,
            persistent.get(relative, {}),
            down_required,
            down_markers,
            description="down-attempt payload",
        )
    uav_relative = "pcap/uav_egress.pcap"
    uav_observed: Counter[str] = persistent.get(uav_relative, {}).get(
        "payload_hashes", Counter()
    )
    leaked = {
        payload_hash: uav_observed[payload_hash]
        for payload_hash in down_required
        if uav_observed[payload_hash]
    }
    if leaked:
        failures.append(
            f"{uav_relative}: down-attempt payloads reached the UAV side: {leaked}"
        )
    return _result(failures, pcaps=stats)


def _capture_stats_gate(
    run_dir: Path,
    windows: dict[str, tuple[int, int]],
    lifecycle: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    failures: list[str] = []
    expected = {
        key: (interface, pcap_relative)
        for key, interface, pcap_relative in PERSISTENT_CAPTURE_SPECS
    }
    exact_keys = {
        "contract",
        "interface",
        "capture_protocol",
        "packet_filter",
        "pcap_path",
        "pcap_bytes",
        "linktype",
        "snaplen",
        "receive_buffer_requested_bytes",
        "receive_buffer_effective_bytes",
        "receive_buffer_setter",
        "drain_batch_packet_limit",
        "drain_batch_byte_limit",
        "started_monotonic_ns",
        "stopped_monotonic_ns",
        "stop_signal",
        "packets_written",
        "packets_received_kernel",
        "packets_dropped_kernel",
    }
    details: dict[str, Any] = {}
    good_window = windows.get("good")
    recovery_window = windows.get("recovery")
    lifecycle = lifecycle or {}
    captures_ready_ns = (lifecycle.get("captures_ready") or {}).get("monotonic_ns")
    engine2_stopped_ns = (lifecycle.get("engine2_stopped") or {}).get("monotonic_ns")
    if good_window is None or recovery_window is None:
        failures.append(
            "cannot prove persistent capture coverage without good and recovery phase windows"
        )
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
                failures.append(f"{stats_relative}: fields differ from exact stats v2")
            if (
                stats.get("contract") != CAPTURE_STATS_CONTRACT
                or stats.get("interface") != interface
                or stats.get("capture_protocol") != CAPTURE_PROTOCOL
                or stats.get("packet_filter") != CAPTURE_PACKET_FILTER
                or stats.get("pcap_path") != pcap_path.name
                or not _is_int(stats.get("linktype"))
                or stats.get("linktype") != 1
                or not _is_int(stats.get("snaplen"))
                or stats.get("snaplen") != 65_535
                or not _is_int(stats.get("receive_buffer_requested_bytes"))
                or stats.get("receive_buffer_requested_bytes")
                != CAPTURE_RECEIVE_BUFFER_REQUESTED_BYTES
                or not _is_int(stats.get("receive_buffer_effective_bytes"))
                or stats.get("receive_buffer_effective_bytes")
                != CAPTURE_RECEIVE_BUFFER_EFFECTIVE_BYTES
                or stats.get("receive_buffer_setter")
                not in CAPTURE_RECEIVE_BUFFER_SETTERS
                or not _is_int(stats.get("drain_batch_packet_limit"))
                or stats.get("drain_batch_packet_limit")
                != CAPTURE_DRAIN_BATCH_PACKET_LIMIT
                or not _is_int(stats.get("drain_batch_byte_limit"))
                or stats.get("drain_batch_byte_limit")
                != CAPTURE_DRAIN_BATCH_BYTE_LIMIT
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
            else:
                if good_window is not None and started >= good_window[0]:
                    failures.append(
                        f"{stats_relative}: persistent capture does not start before the good phase"
                    )
                if recovery_window is not None and stopped <= recovery_window[1]:
                    failures.append(
                        f"{stats_relative}: persistent capture does not stop after the recovery phase"
                    )
                if _is_int(captures_ready_ns) and started >= captures_ready_ns:
                    failures.append(
                        f"{stats_relative}: capture did not start before the durable captures_ready boundary"
                    )
                if _is_int(engine2_stopped_ns) and stopped <= engine2_stopped_ns:
                    failures.append(
                        f"{stats_relative}: capture did not remain active through terminal engine2 stop"
                    )
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
                "started_monotonic_ns": started,
                "stopped_monotonic_ns": stopped,
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


def _is_timely_heartbeat_forward(
    forward: dict[str, Any],
    *,
    start_ns: int,
    end_ns: int,
    raw_received_ns: int,
) -> bool:
    """Return whether one adapter relay is a fresh raw-heartbeat occurrence."""
    adapter_received = forward.get("received_monotonic_ns")
    adapter_complete = forward.get("send_complete_monotonic_ns")
    return (
        _is_int(adapter_received)
        and _is_int(adapter_complete)
        and start_ns <= adapter_received <= adapter_complete < end_ns
        and adapter_complete <= raw_received_ns
        and raw_received_ns - adapter_complete <= MAX_HEARTBEAT_FORWARD_TO_RAW_NS
    )


def _adapter_raw_occurrence_gate(
    records: list[dict[str, Any]],
    *,
    windows: dict[str, tuple[int, int]],
    phase_evidence: dict[str, Any],
) -> dict[str, Any]:
    """Bind raw probe UDP occurrences to one exact adapter relay occurrence.

    Hash equality alone is insufficient: periodic MAVLink frames can repeat.
    The v2 contract therefore uses the independently ordered probe TX/RX and
    adapter datagram sequences plus explicit recv/send CLOCK_MONOTONIC bounds.
    No producer-side ACK or summary boolean contributes to this decision.
    """

    failures: list[str] = []
    forwards = [record for record in records if record.get("event") == "forward"]
    by_direction_hash: dict[str, dict[str, list[dict[str, Any]]]] = {
        "gcs_to_tail": defaultdict(list),
        "tail_to_gcs": defaultdict(list),
    }
    forward_by_sequence: dict[int, dict[str, Any]] = {}
    for index, forward in enumerate(forwards, start=1):
        label = f"adapter forward event_seq {forward.get('event_seq')}"
        sequence = forward.get("adapter_datagram_seq")
        if sequence != index or not _is_int(sequence):
            failures.append(f"{label}: adapter_datagram_seq must be contiguous from one")
            continue
        forward_by_sequence[sequence] = forward
        direction = forward.get("direction")
        if direction not in by_direction_hash:
            failures.append(f"{label}: direction is invalid")
            continue
        payload_hash = forward.get("transport_payload_sha256")
        payload_size = forward.get("transport_payload_size")
        received = forward.get("received_monotonic_ns")
        send_start = forward.get("send_start_monotonic_ns")
        send_complete = forward.get("send_complete_monotonic_ns")
        if payload_hash != forward.get("sha256") or not _is_sha256(payload_hash):
            failures.append(f"{label}: explicit transport hash is invalid or differs from legacy sha256")
            continue
        if payload_size != forward.get("bytes") or not _is_int(payload_size) or payload_size <= 0:
            failures.append(f"{label}: explicit transport size is invalid or differs from bytes")
            continue
        if (
            not _is_int(received)
            or not _is_int(send_start)
            or not _is_int(send_complete)
            or not _is_int(forward.get("monotonic_ns"))
            or not (received <= send_start <= send_complete <= forward["monotonic_ns"])
        ):
            failures.append(f"{label}: receive/send/durable timestamps are not ordered")
            continue
        by_direction_hash[direction][payload_hash].append(forward)

    used_sequences: set[int] = set()
    # One UDP datagram can contain several MAVLink frames.  It has exactly one
    # adapter relay occurrence, so a COMMAND_ACK/telemetry frame and a
    # HEARTBEAT frame decoded from the *same* raw datagram must share that
    # occurrence rather than being forced to invent a duplicate forward.
    raw_rx_forward_sequences: dict[tuple[str, int], int] = {}

    def phase_candidates(
        *,
        direction: str,
        payload_hash: Any,
        payload_size: Any,
        predicate: Any,
    ) -> list[dict[str, Any]]:
        if not _is_sha256(payload_hash) or not _is_int(payload_size):
            return []
        candidates: list[dict[str, Any]] = []
        for forward in by_direction_hash[direction].get(payload_hash, []):
            sequence = forward.get("adapter_datagram_seq")
            if not _is_int(sequence) or sequence in used_sequences:
                continue
            if forward.get("transport_payload_size") != payload_size:
                continue
            if predicate(forward):
                candidates.append(forward)
        return candidates

    request_matches = 0
    response_matches = 0
    heartbeat_matches: dict[str, int] = {}
    stale_heartbeat_matches: dict[str, int] = {}
    liveness_pairs: dict[str, dict[str, int]] = {
        phase: {"observed": 0, "causal": 0, "fresh": 0, "stale": 0}
        for phase in PHASES
    }
    for phase in PHASES:
        evidence = phase_evidence.get(phase, {})
        raw = evidence.get("raw") if isinstance(evidence.get("raw"), dict) else {}
        transactions = raw.get("transactions") if isinstance(raw.get("transactions"), dict) else {}
        phase_window = windows.get(phase)
        if phase_window is None:
            failures.append(f"adapter/{phase}: no probe window for raw occurrence matching")
            continue
        start_ns, end_ns = phase_window
        for attempt_number, transaction in sorted(transactions.items()):
            if not _is_int(attempt_number) or not isinstance(transaction, dict):
                failures.append(f"adapter/{phase}: raw transaction map is malformed")
                continue
            if phase == "down":
                # The GCS still emits raw UDP offers while the engine is
                # stopped, but those offers must not cross the adapter's
                # modeled ingress boundary.  PCAP and the legacy adapter gate
                # independently prove the offer and the absence of a forward.
                continue
            for leg in ("marker", "command"):
                tx = transaction.get(leg)
                if not isinstance(tx, dict):
                    failures.append(f"adapter/{phase}/{attempt_number}: missing raw {leg} TX")
                    continue
                tx_complete = tx.get("send_complete_monotonic_ns")
                candidates = phase_candidates(
                    direction="gcs_to_tail",
                    payload_hash=tx.get("transport_payload_sha256"),
                    payload_size=tx.get("transport_payload_size"),
                    predicate=lambda forward, complete=tx_complete: (
                        _is_int(complete)
                        and _is_int(forward.get("received_monotonic_ns"))
                        and _is_int(forward.get("send_complete_monotonic_ns"))
                        and complete <= forward["received_monotonic_ns"]
                        and start_ns <= forward["send_complete_monotonic_ns"] < end_ns
                    ),
                )
                if len(candidates) != 1:
                    failures.append(
                        f"adapter/{phase}/{attempt_number}: raw {leg} TX has "
                        f"{len(candidates)} exact causal gcs_to_tail occurrence(s), expected one"
                    )
                    continue
                sequence = candidates[0].get("adapter_datagram_seq")
                assert _is_int(sequence)
                used_sequences.add(sequence)
                request_matches += 1

        rx_records = raw.get("rx") if isinstance(raw.get("rx"), dict) else {}
        decoded = raw.get("decoded") if isinstance(raw.get("decoded"), list) else []
        decoded_by_rx: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for record in decoded:
            if isinstance(record, dict) and _is_int(record.get("rx_datagram_seq")):
                decoded_by_rx[record["rx_datagram_seq"]].append(record)

        for rx_sequence, semantics in sorted(decoded_by_rx.items()):
            rx = rx_records.get(rx_sequence)
            if not isinstance(rx, dict):
                continue
            response_semantics = [
                record for record in semantics if record.get("event") in {"command_ack", "telemetry"}
            ]
            heartbeat_semantics = [record for record in semantics if record.get("event") == "heartbeat"]
            if response_semantics:
                transaction_ids = {record.get("transaction_id") for record in response_semantics}
                attempt_numbers = {record.get("attempt") for record in response_semantics}
                if len(transaction_ids) != 1 or len(attempt_numbers) != 1:
                    failures.append(
                        f"adapter/{phase}/rx{rx_sequence}: response frames disagree on command transaction"
                    )
                    continue
                attempt_number = next(iter(attempt_numbers))
                transaction = transactions.get(attempt_number) if _is_int(attempt_number) else None
                command = transaction.get("command") if isinstance(transaction, dict) else None
                command_complete = command.get("send_complete_monotonic_ns") if isinstance(command, dict) else None
                received = rx.get("received_monotonic_ns")
                candidates = phase_candidates(
                    direction="tail_to_gcs",
                    payload_hash=rx.get("transport_payload_sha256"),
                    payload_size=rx.get("transport_payload_size"),
                    predicate=lambda forward, command_ns=command_complete, rx_ns=received: (
                        _is_int(command_ns)
                        and _is_int(rx_ns)
                        and _is_int(forward.get("received_monotonic_ns"))
                        and _is_int(forward.get("send_complete_monotonic_ns"))
                        and command_ns <= forward["received_monotonic_ns"]
                        and forward["send_complete_monotonic_ns"] <= rx_ns
                        and start_ns <= forward["send_complete_monotonic_ns"] < end_ns
                    ),
                )
                if len(candidates) != 1:
                    failures.append(
                        f"adapter/{phase}/rx{rx_sequence}: response raw RX has "
                        f"{len(candidates)} exact causal tail_to_gcs occurrence(s), expected one"
                    )
                else:
                    sequence = candidates[0].get("adapter_datagram_seq")
                    assert _is_int(sequence)
                    used_sequences.add(sequence)
                    raw_rx_forward_sequences[(phase, rx_sequence)] = sequence
                    response_matches += 1
            # Count only independently observed liveness RX datagrams.  A
            # command-loop heartbeat cannot replace the full bounded liveness
            # observation, and multiple decoded frames in one datagram count
            # once.
            observed_liveness = [
                record for record in heartbeat_semantics if record.get("liveness_observation") is True
            ]
            if observed_liveness:
                liveness_pairs[phase]["observed"] += 1
                received = rx.get("received_monotonic_ns")
                candidates: list[dict[str, Any]] = []
                payload_hash = rx.get("transport_payload_sha256")
                payload_size = rx.get("transport_payload_size")
                shared_sequence = raw_rx_forward_sequences.get((phase, rx_sequence))
                if shared_sequence is not None:
                    shared_forward = forward_by_sequence.get(shared_sequence)
                    if (
                        isinstance(shared_forward, dict)
                        and shared_forward.get("transport_payload_sha256") == payload_hash
                        and shared_forward.get("transport_payload_size") == payload_size
                        and _is_int(received)
                        and _is_int(shared_forward.get("send_complete_monotonic_ns"))
                        and shared_forward["send_complete_monotonic_ns"] <= received
                    ):
                        candidates = [shared_forward]
                elif _is_sha256(payload_hash) and _is_int(payload_size):
                    for forward in by_direction_hash["tail_to_gcs"].get(payload_hash, []):
                        sequence = forward.get("adapter_datagram_seq")
                        if (
                            not _is_int(sequence)
                            or sequence in used_sequences
                            or forward.get("transport_payload_size") != payload_size
                            or not _is_int(received)
                            or not _is_int(forward.get("send_complete_monotonic_ns"))
                            or forward["send_complete_monotonic_ns"] > received
                        ):
                            continue
                        candidates.append(forward)
                candidates.sort(key=lambda forward: forward["adapter_datagram_seq"])
                selected: dict[str, Any] | None = None
                selection_failed = False
                # MAVLink HEARTBEAT byte strings recur after the 8-bit
                # sequence wraps.  FIFO over all historical byte-identical
                # relays is not a causal discriminator: it can assign a raw
                # RX to a 60-second-old occurrence even when one unique
                # current relay completed milliseconds before that same RX.
                # A fresh pair must therefore be phase-local *and* satisfy a
                # bounded adapter-to-raw handoff.  A historical occurrence
                # remains stale evidence only when no unique timely current
                # occurrence exists, so it can never satisfy the fresh
                # heartbeat minimum by itself.
                stale_candidates = [
                    forward
                    for forward in candidates
                    if forward["send_complete_monotonic_ns"] < start_ns
                ]
                fresh_candidates = [
                    forward
                    for forward in candidates
                    if _is_int(forward.get("received_monotonic_ns"))
                    and start_ns <= forward["received_monotonic_ns"]
                    <= forward["send_complete_monotonic_ns"] < end_ns
                ]
                timely_fresh_candidates = [
                    forward
                    for forward in fresh_candidates
                    if _is_timely_heartbeat_forward(
                        forward,
                        start_ns=start_ns,
                        end_ns=end_ns,
                        raw_received_ns=received,
                    )
                ]
                out_of_window_candidates = [
                    forward
                    for forward in candidates
                    if forward not in stale_candidates and forward not in fresh_candidates
                ]
                if len(timely_fresh_candidates) == 1:
                    selected = timely_fresh_candidates[0]
                elif len(timely_fresh_candidates) > 1:
                    failures.append(
                        f"adapter/{phase}/rx{rx_sequence}: liveness raw RX has "
                        f"{len(timely_fresh_candidates)} current-phase tail_to_gcs occurrence(s), expected one"
                    )
                    selection_failed = True
                elif stale_candidates:
                    # A pre-window liveness datagram is allowed to remain in
                    # the persistent endpoint receive queue, but is recorded
                    # as stale and cannot contribute to fresh liveness.
                    selected = stale_candidates[0]
                elif fresh_candidates:
                    failures.append(
                        f"adapter/{phase}/rx{rx_sequence}: liveness raw RX has no timely "
                        f"current-phase tail_to_gcs occurrence within "
                        f"{MAX_HEARTBEAT_FORWARD_TO_RAW_NS // 1_000_000} ms"
                    )
                    selection_failed = True
                elif out_of_window_candidates:
                    failures.append(
                        f"adapter/{phase}/rx{rx_sequence}: liveness raw RX has "
                        f"{len(out_of_window_candidates)} tail_to_gcs occurrence(s) outside its phase window"
                    )
                    selection_failed = True
                if selected is None:
                    if not selection_failed:
                        failures.append(
                            f"adapter/{phase}/rx{rx_sequence}: liveness raw RX has "
                            f"{len(candidates)} exact causal tail_to_gcs occurrence(s), expected one"
                        )
                else:
                    sequence = selected.get("adapter_datagram_seq")
                    assert _is_int(sequence)
                    used_sequences.add(sequence)
                    liveness_pairs[phase]["causal"] += 1
                    forward_complete = selected.get("send_complete_monotonic_ns")
                    assert _is_int(forward_complete)
                    if start_ns <= forward_complete < end_ns:
                        heartbeat_matches[phase] = heartbeat_matches.get(phase, 0) + 1
                        liveness_pairs[phase]["fresh"] += 1
                    else:
                        stale_heartbeat_matches[phase] = stale_heartbeat_matches.get(phase, 0) + 1
                        liveness_pairs[phase]["stale"] += 1

    return _result(
        failures,
        request_matches=request_matches,
        response_matches=response_matches,
        fresh_liveness_pairs=heartbeat_matches,
        stale_liveness_pairs=stale_heartbeat_matches,
        liveness_pairs=liveness_pairs,
    )


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
    forward_records_by_phase: dict[str, dict[str, dict[str, list[dict[str, Any]]]]] = {
        phase: {"gcs_to_tail": defaultdict(list), "tail_to_gcs": defaultdict(list)}
        for phase in PHASES
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
            if _is_int(monotonic_ns) and start_ns <= monotonic_ns < end_ns
        ]
        if len(matching_phases) == 1:
            phase = matching_phases[0]
            forward_by_phase[phase][direction][payload_hash] += 1
            forward_records_by_phase[phase][direction][payload_hash].append(record)

    transaction_forward_details: dict[str, dict[str, int]] = {}
    heartbeat_details: dict[str, dict[str, int]] = {}
    used_request_forward_sequences: set[int] = set()
    used_response_forward_sequences: set[int] = set()
    raw_occurrence_result = _adapter_raw_occurrence_gate(
        records,
        windows=windows,
        phase_evidence=phase_evidence,
    )
    failures.extend(raw_occurrence_result["failures"])
    raw_liveness_pairs = raw_occurrence_result["details"].get("liveness_pairs", {})
    if not isinstance(raw_liveness_pairs, dict):
        raw_liveness_pairs = {}
    for phase in ("good", "recovery"):
        request_required: Counter[str] = Counter()
        response_required: Counter[str] = Counter()
        evidence = phase_evidence.get(phase, {})
        for attempt in evidence.get("attempts", {}).values():
            for key in ("marker_sha256", "command_sha256"):
                if _is_sha256(attempt.get(key)):
                    request_required[attempt[key]] += 1
        # COMMAND_ACK and telemetry are command-correlated responses, so their
        # adapter forwarding must remain strictly inside the same phase.  Do
        # not fold asynchronous heartbeats into this set; they are handled by
        # the causal liveness check below.
        response_hashes = {
            record["packet_sha256"]
            for record in evidence.get("acks", {}).values()
            if _is_sha256(record.get("packet_sha256"))
        }
        response_hashes.update(
            record["packet_sha256"]
            for rows in evidence.get("telemetry", {}).values()
            for record in rows
            if _is_sha256(record.get("packet_sha256"))
        )
        response_required.update({payload_hash: 1 for payload_hash in response_hashes})
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

        # Count membership alone is not enough: a stale or duplicated frame
        # can share a packet hash with a later transaction.  Bind each request
        # and each unique response datagram to an adapter occurrence which is
        # causally between its local attempt and (for responses) local receive.
        causal_requests = 0
        for attempt_number, attempt in sorted(evidence.get("attempts", {}).items()):
            attempt_ns = attempt.get("monotonic_ns")
            if not _is_int(attempt_ns):
                failures.append(
                    f"adapter/{phase}/{attempt_number}: command attempt has no monotonic timestamp"
                )
                continue
            for label, key in (("marker", "marker_sha256"), ("command", "command_sha256")):
                payload_hash = attempt.get(key)
                if not _is_sha256(payload_hash):
                    continue
                candidates = [
                    record
                    for record in forward_records_by_phase[phase]["gcs_to_tail"].get(payload_hash, [])
                    if _is_int(record.get("event_seq"))
                    and record["event_seq"] not in used_request_forward_sequences
                    and _is_int(record.get("monotonic_ns"))
                    and record["monotonic_ns"] >= attempt_ns
                ]
                if not candidates:
                    failures.append(
                        f"adapter/{phase}/{attempt_number}: {label} payload {payload_hash} "
                        "has no forward at or after command_attempt"
                    )
                    continue
                forward = candidates[0]
                used_request_forward_sequences.add(forward["event_seq"])
                causal_requests += 1

        response_groups: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
        for response in evidence.get("acks", {}).values():
            attempt_number = response.get("attempt")
            payload_hash = response.get("packet_sha256")
            if _is_int(attempt_number) and _is_sha256(payload_hash):
                response_groups[(attempt_number, payload_hash)].append(response)
        for rows in evidence.get("telemetry", {}).values():
            for response in rows:
                attempt_number = response.get("attempt")
                payload_hash = response.get("packet_sha256")
                if _is_int(attempt_number) and _is_sha256(payload_hash):
                    response_groups[(attempt_number, payload_hash)].append(response)

        causal_response_datagrams = 0
        for (attempt_number, payload_hash), response_records in sorted(response_groups.items()):
            attempt = evidence.get("attempts", {}).get(attempt_number)
            attempt_ns = attempt.get("monotonic_ns") if isinstance(attempt, dict) else None
            received_times = [
                record.get("monotonic_ns")
                for record in response_records
                if _is_int(record.get("monotonic_ns"))
            ]
            if not _is_int(attempt_ns) or len(received_times) != len(response_records):
                failures.append(
                    f"adapter/{phase}/{attempt_number}: response payload {payload_hash} lacks a causal timestamp"
                )
                continue
            received_ns = min(received_times)
            candidates = [
                record
                for record in forward_records_by_phase[phase]["tail_to_gcs"].get(payload_hash, [])
                if _is_int(record.get("event_seq"))
                and record["event_seq"] not in used_response_forward_sequences
                and _is_int(record.get("monotonic_ns"))
                and attempt_ns <= record["monotonic_ns"] <= received_ns
            ]
            if not candidates:
                failures.append(
                    f"adapter/{phase}/{attempt_number}: response payload {payload_hash} has no forward "
                    "between command_attempt and probe receive"
                )
                continue
            forward = candidates[0]
            used_response_forward_sequences.add(forward["event_seq"])
            causal_response_datagrams += 1
        transaction_forward_details[phase] = {
            "causal_requests": causal_requests,
            "causal_response_datagrams": causal_response_datagrams,
        }

        raw_pair = raw_liveness_pairs.get(phase, {})
        if not isinstance(raw_pair, dict):
            raw_pair = {}
        fresh_heartbeats = raw_pair.get("fresh") if _is_int(raw_pair.get("fresh")) else 0
        heartbeat_details[phase] = {
            "observed": len(evidence.get("heartbeats", [])),
            "causal": raw_pair.get("causal") if _is_int(raw_pair.get("causal")) else 0,
            "fresh": fresh_heartbeats,
            "stale": raw_pair.get("stale") if _is_int(raw_pair.get("stale")) else 0,
        }
        if fresh_heartbeats < MIN_POSITIVE_HEARTBEATS:
            failures.append(
                f"adapter/{phase}: fresh heartbeat forwards {fresh_heartbeats}, "
                f"expected at least {MIN_POSITIVE_HEARTBEATS}"
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
    return _result(
        failures,
        forwards=details,
        transaction_forwards=transaction_forward_details,
        heartbeat_forwards=heartbeat_details,
        raw_occurrences=raw_occurrence_result["details"],
    ), adapter_pid


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
    try:
        endpoint_records, endpoint_errors = _load_event_log(
            run_dir / "logs/m2_probe_events.jsonl", metadata, require_phase=True
        )
        failures.extend(f"persistent endpoint identity: {error}" for error in endpoint_errors)
        starts = [
            record for record in endpoint_records if record.get("event") == "endpoint_started"
        ]
        if len(starts) != 1:
            failures.append("persistent endpoint identity has no unique endpoint_started record")
        elif "gcs_probe" not in stable_identities:
            failures.append("persistent endpoint identity has no stable gcs_probe snapshot")
        elif stable_identities["gcs_probe"][0] != starts[0].get("endpoint_pid"):
            failures.append("gcs_probe snapshot PID does not match persistent endpoint_started.pid")
    except Exception as exc:
        failures.append(f"persistent endpoint identity could not be read: {exc}")
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
        "network/scripts/m2_lifecycle_event.py",
        "network/scripts/m2_lifecycle_monitor.py",
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
        "logs/m2_lifecycle.jsonl",
        "logs/m2_monitor.jsonl",
        "logs/m2_monitor.stop",
        "logs/m2_lifecycle_monitor.stdout",
        "logs/m2_lifecycle_monitor.stderr",
        "logs/m2_gcs_endpoint.stdout",
        "logs/m2_gcs_endpoint.stderr",
        "logs/m2_gcs_endpoint_shutdown.log",
        "logs/m2_runner.log",
        *(f"logs/ns3_{phase}_config.json" for phase in ENGINE_PHASES),
        *(f"logs/ns3_{phase}_packet_events.jsonl" for phase in ENGINE_PHASES),
        *(f"logs/ns3_{phase}.argv" for phase in ENGINE_PHASES),
        *(f"logs/ns3_{phase}.ready" for phase in ENGINE_PHASES),
        *(f"logs/ns3_{phase}.stop" for phase in ENGINE_PHASES),
        *(f"logs/ns3_{phase}.lifecycle.jsonl" for phase in ENGINE_PHASES),
        *PERSISTENT_CAPTURE_PCAPS,
        *(
            f"pcap/{point}_{phase}.pcap"
            for phase in ("good", "recovery")
            for point in ENGINE_CAPTURE_POINTS
        ),
    }
    for key, _interface, _pcap_relative in PERSISTENT_CAPTURE_SPECS:
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

    phase_local_persistent = sorted(
        relative
        for relative in {*files, *discovered}
        if PHASE_LOCAL_PERSISTENT_CAPTURE_RE.fullmatch(relative)
    )
    if phase_local_persistent:
        failures.append(
            "phase-local persistent capture filenames are forbidden: "
            f"{phase_local_persistent}"
        )

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
        lifecycle_result, lifecycle = _lifecycle_gate(run_dir, metadata, windows)
    except Exception as exc:
        lifecycle_result, lifecycle = _result(
            [f"lifecycle parser failed closed: {exc}"]
        ), {}
    gates["lifecycle"] = lifecycle_result
    try:
        gates["lifecycle_monitor"] = _monitor_gate(run_dir, metadata, lifecycle)
    except Exception as exc:
        gates["lifecycle_monitor"] = _result(
            [f"lifecycle-monitor parser failed closed: {exc}"]
        )

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
        gates["packet_engine_lifecycle"] = _engine_lifecycle_gate(
            run_dir, metadata, lifecycle
        )
    except Exception as exc:
        gates["packet_engine_lifecycle"] = _result(
            [f"packet-engine lifecycle parser failed closed: {exc}"]
        )

    try:
        gates["packet_captures"] = _pcap_gate(run_dir, phase_evidence)
    except Exception as exc:
        gates["packet_captures"] = _result([f"PCAP parser failed closed: {exc}"])
    try:
        gates["capture_accounting"] = _capture_stats_gate(
            run_dir, windows, lifecycle
        )
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
