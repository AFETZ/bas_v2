#!/usr/bin/env python3
"""Independently validate the real five-endpoint M3 matrix evidence.

The validator re-decodes every preserved transport payload, re-derives all 30
cell identities and metrics, correlates inner bytes with ns-3 queue/channel
events, and treats producer labels as untrusted observations.  ``--no-write``
performs the same derivation in a fresh process and additionally requires exact
byte equality with the producer-written result without modifying the run tree.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
import os
import re
import stat
import struct
import subprocess
import sys
import zlib
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MATRIX = ROOT / "network/config/endpoint_matrix_5uav.json"
DEFAULT_OUTPUT = Path("metrics/m3_validation_results.json")
DEFAULT_M2_RECEIPT = Path("/run/ams/prerequisites/m2.json")
RESULT_CONTRACT = "ams.m3.external-matrix-validation/v1"
SMOKE_RESULT_CONTRACT = "ams.m3.actual-sitl-smoke/v1"
RUN_CONTRACT = "ams.m3.external_matrix_run/v1"
PHASE_CONTRACT = "ams.m3.external_matrix_phase/v1"
ENDPOINT_SCHEMA = "ams.m3.endpoint_event/v1"
ACTUAL_CONTROL_EVENT_SCHEMA = "ams.m3.actual_control_event/v1"
ACTUAL_CONTROL_ENDPOINT_FORM = "actual_sitl_mavproxy_udp_tail"
RESOLVED_FLIGHT_CONTRACT = "ams.m3.resolved_flight_scenario/v1"
ENGINE_SCHEMA = "ams.ns3.packet_event/v1"
LIFECYCLE_SCHEMA = "ams.m3.lifecycle_event/v1"
CAPTURE_STATS_CONTRACT = "ams.raw-packet-capture-stats/v2"
CAPTURE_PROTOCOL = "ETH_P_ALL"
CAPTURE_PACKET_FILTER = "none"
CAPTURE_UDP_PORT_FILTER_PREFIX = "udp-ports:v1:"
CAPTURE_RECEIVE_BUFFER_REQUESTED_BYTES = 8_388_608
CAPTURE_RECEIVE_BUFFER_EFFECTIVE_BYTES = 16_777_216
CAPTURE_RECEIVE_BUFFER_SETTERS = {"SO_RCVBUF", "SO_RCVBUFFORCE"}
CAPTURE_DRAIN_BATCH_PACKET_LIMIT = 256
CAPTURE_DRAIN_BATCH_BYTE_LIMIT = 4_194_304
TOPOLOGY_SAMPLE_SCHEMA = "ams.m3.topology_sample/v1"
TOPOLOGY_SUMMARY_CONTRACT = "ams.m3.topology_monitor_summary/v1"
TOPOLOGY_ACK_CONTRACT = "ams.m3.topology_monitor_ack/v1"
PROCESS_TRANSITION_MAX_NS = 30_000_000_000
FORBIDDEN_CONTRACT = "ams.m3.forbidden_canary_contract/v1"
FORBIDDEN_RESULT_CONTRACT = "ams.m3.forbidden_canary_observation/v1"
FORBIDDEN_LISTENER_SCHEMA = "ams.m3.forbidden_listener_event/v1"
M2_RECEIPT_CONTRACT = "ams.m2.host-final-receipt/v1"
M2_RESULT_CONTRACT = "ams.m2.vertical-slice-validation/v2"
M2_EXTENSION_CONTRACT = "ams.m2-to-m3.shared-packet-core/v1"
ENDPOINTS = ("gcs", "uav1", "uav2", "uav3", "uav4", "uav5")
NAMESPACES = {
    "gcs": "ams-gcs",
    **{f"uav{index}": f"ams-uav{index}" for index in range(1, 6)},
}
MONITORED_NAMESPACES = ("container-root", "ams-ns3", *NAMESPACES.values())
TRAFFIC_CLASSES = ("control", "payload", "additional_data")
TOS_BY_CLASS = {"control": 184, "payload": 40, "additional_data": 0}
PHASE_CODES = {"positive": 1, "stopped": 2, "recovery": 3, "p2mp": 4}
CLASS_CODES = {name: index + 1 for index, name in enumerate(TRAFFIC_CLASSES)}
DIRECTION_CODES = {"downlink": 1, "uplink": 2}
MAVLINK_CRC_EXTRA = {
    0: 50,
    33: 104,
    76: 152,
    77: 143,
    148: 178,
    253: 83,
    385: 147,
}
STREAM_RECORD = struct.Struct(">4sBBBBBBH16sQ16s16s")
FORBIDDEN_RECORD = struct.Struct(">4sBBH16s32s")
FORBIDDEN_MAGIC = b"AMFC"
FORBIDDEN_KIND_CODES = {
    "loopback_ipv4": 1,
    "loopback_ipv6": 2,
    "legacy_direct_port": 3,
    "unreachable_ipv4": 4,
}
P2MP_GROUP = "239.71.0.1"
P2MP_PORT = 14900
REQUIRED_NS3_MODULES = (
    "applications,bridge,core,csma,flow-monitor,internet,mobility,network,stats,"
    "tap-bridge,traffic-control"
)
HEX64 = re.compile(r"^[0-9a-f]{64}$")
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX32 = re.compile(r"^[0-9a-f]{32}$")


class ValidationError(ValueError):
    """A raw artifact cannot be parsed unambiguously."""


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


def regular_file(path: Path) -> bool:
    try:
        details = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(details.st_mode) and details.st_nlink == 1


def strict_json(path: Path) -> Any:
    if not regular_file(path):
        raise ValidationError(f"missing/nonregular/hardlinked artifact: {path}")

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValidationError(f"duplicate key {key!r} in {path}")
            result[key] = value
        return result

    def reject_nonfinite(value: str) -> None:
        raise ValidationError(f"non-finite JSON value {value!r} in {path}")

    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=unique,
            parse_constant=reject_nonfinite,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"invalid JSON {path}: {exc}") from exc


def strict_jsonl(path: Path) -> list[dict[str, Any]]:
    if not regular_file(path):
        raise ValidationError(f"missing/nonregular/hardlinked JSONL: {path}")
    records: list[dict[str, Any]] = []
    payload = path.read_bytes()
    if not payload or not payload.endswith(b"\n"):
        raise ValidationError(f"JSONL is empty or lacks final newline: {path}")
    for line_number, line in enumerate(payload.splitlines(), start=1):
        if not line:
            raise ValidationError(f"blank JSONL line at {path}:{line_number}")

        def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValidationError(
                        f"duplicate key {key!r} at {path}:{line_number}"
                    )
                result[key] = value
            return result

        def reject_nonfinite(value: str) -> None:
            raise ValidationError(
                f"non-finite JSON value {value!r} at {path}:{line_number}"
            )

        try:
            record = json.loads(
                line,
                object_pairs_hook=unique,
                parse_constant=reject_nonfinite,
            )
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValidationError(
                f"invalid JSONL at {path}:{line_number}: {exc}"
            ) from exc
        if not isinstance(record, dict):
            raise ValidationError(f"non-object JSONL record at {path}:{line_number}")
        records.append(record)
    return records


def exact_keys(value: Any, expected: set[str], label: str) -> list[str]:
    if not isinstance(value, dict):
        return [f"{label} is not an object"]
    observed = set(value)
    if observed == expected:
        return []
    return [
        f"{label} keys differ: missing={sorted(expected - observed)} extra={sorted(observed - expected)}"
    ]


def repository_commit() -> str:
    try:
        value = subprocess.run(
            [
                "/usr/bin/git",
                "-c",
                f"safe.directory={ROOT.resolve(strict=True)}",
                "-C",
                str(ROOT),
                "rev-parse",
                "HEAD",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValidationError(f"cannot resolve repository commit: {exc}") from exc
    if not HEX40.fullmatch(value):
        raise ValidationError("repository commit is not exact 40-hex")
    return value


def validate_m2_extension(
    run_dir: Path,
    run: dict[str, Any],
    matrix: dict[str, Any],
    engine_identity: dict[str, Any],
    m2_receipt_path: Path,
) -> tuple[list[str], dict[str, Any], dict[str, Any]]:
    failures: list[str] = []
    shared: dict[str, Any] = {}
    details: dict[str, Any] = {}
    raw_copy = run_dir / "raw/m2_component_host_final_receipt.json"
    try:
        if (
            not regular_file(m2_receipt_path)
            or m2_receipt_path.stat().st_mode & 0o222
            or not regular_file(raw_copy)
            or raw_copy.stat().st_mode & 0o222
        ):
            raise ValidationError(
                "M2 external/run-local receipts are not immutable regular files"
            )
        external_payload = m2_receipt_path.read_bytes()
        if external_payload != raw_copy.read_bytes():
            raise ValidationError(
                "run-local M2 receipt is not byte-identical to mounted authority"
            )
        receipt = strict_json(m2_receipt_path)
        receipt_keys = {
            "schema_version",
            "contract",
            "profile",
            "run_id",
            "receipt_path",
            "source_commit",
            "image_reference",
            "image_digest",
            "container_id",
            "validation_container_id",
            "consumed_nodes",
            "qualification_content_vector",
            "qualification_consumption",
            "qualification_contract_sha256",
            "formal_accepted",
            "passed",
            "failures",
            "result_contract",
            "result_sha256",
            "result",
            "component_content_manifest",
            "host_validation_manifest",
            "status_authority",
            "prerequisite_receipts",
            "required_component_receipts",
        }
        if set(receipt) != receipt_keys:
            raise ValidationError("M2 host-final receipt keys are not exact")
        if (
            external_payload
            != (
                json.dumps(receipt, allow_nan=False, indent=2, sort_keys=True) + "\n"
            ).encode()
        ):
            raise ValidationError("M2 host-final receipt bytes are not canonical")
        source_commit = receipt.get("source_commit")
        predecessor_run_id = receipt.get("run_id")
        if (
            receipt.get("schema_version") != 1
            or receipt.get("contract") != M2_RECEIPT_CONTRACT
            or receipt.get("profile") != "m2_component"
            or receipt.get("consumed_nodes") != ["Q0", "Q1", "Q2"]
            or receipt.get("formal_accepted") is not True
            or receipt.get("passed") is not True
            or receipt.get("failures") != []
            or receipt.get("result_contract") != M2_RESULT_CONTRACT
            or not isinstance(predecessor_run_id, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", predecessor_run_id)
            is None
            or receipt.get("receipt_path")
            != f"runs/{predecessor_run_id}/metrics/m2_host_final_receipt.json"
            or not isinstance(source_commit, str)
            or not HEX40.fullmatch(source_commit)
        ):
            raise ValidationError(
                "M2 milestone host-final identity/commit is not exact"
            )
        result = receipt.get("result")
        result_keys = {
            "schema_version",
            "contract",
            "validation_contract",
            "run_id",
            "runtime_id",
            "packet_engine",
            "endpoint_transaction",
            "passed",
            "failures",
            "gates",
        }
        if not isinstance(result, dict) or set(result) != result_keys:
            raise ValidationError("embedded M2 result keys are not exact")
        result_payload = (
            json.dumps(result, allow_nan=False, indent=2, sort_keys=True) + "\n"
        ).encode()
        if (
            sha256_bytes(result_payload) != receipt.get("result_sha256")
            or result.get("schema_version") != 2
            or result.get("contract") != M2_RESULT_CONTRACT
            or result.get("validation_contract") != "ams.m2.vertical_slice/v1"
            or result.get("run_id") != receipt.get("run_id")
            or result.get("passed") is not True
            or result.get("failures") != []
            or not isinstance(result.get("gates"), dict)
            or not result["gates"]
            or any(
                not isinstance(value, dict)
                or value.get("status") != "passed"
                or value.get("failures") != []
                for value in result["gates"].values()
            )
        ):
            raise ValidationError("embedded M2 result/hash/gates are not passing exact")

        m2_engine = result.get("packet_engine")
        engine_keys = {
            "contract",
            "program",
            "uav_count",
            "source_sha256",
            "binary_sha256",
            "build_receipt_sha256",
            "config_contract",
            "config_sha256",
            "config_tool_sha256",
            "runner_sha256",
            "event_schema",
        }
        current_source_hash = sha256_file(
            ROOT / "network/ns3/scratch/ams-tap-packet-engine.cc"
        )
        current_config_tool_hash = sha256_file(
            ROOT / "network/ns3/tap_packet_engine_config.py"
        )
        current_runner_hash = sha256_file(
            ROOT / "network/ns3/run_ns3_tap_packet_engine.sh"
        )
        config_hashes = (
            m2_engine.get("config_sha256") if isinstance(m2_engine, dict) else None
        )
        if (
            not isinstance(m2_engine, dict)
            or set(m2_engine) != engine_keys
            or m2_engine.get("contract") != "ams.tap_packet_engine/v1"
            or m2_engine.get("program") != "ams-tap-packet-engine"
            or m2_engine.get("uav_count") != 1
            or m2_engine.get("source_sha256") != current_source_hash
            or m2_engine.get("binary_sha256") != engine_identity.get("sha256")
            or m2_engine.get("build_receipt_sha256")
            != engine_identity.get("build_receipt", {}).get("sha256")
            or m2_engine.get("config_contract") != "ams.tap_packet_engine/v1"
            or not isinstance(config_hashes, dict)
            or set(config_hashes) != {"good", "recovery"}
            or any(
                not isinstance(value, str) or not HEX64.fullmatch(value)
                for value in config_hashes.values()
            )
            or len(set(config_hashes.values())) != 2
            or m2_engine.get("config_tool_sha256") != current_config_tool_hash
            or m2_engine.get("runner_sha256") != current_runner_hash
            or m2_engine.get("event_schema") != ENGINE_SCHEMA
        ):
            raise ValidationError(
                "M2 receipt does not bind the current shared engine/config/event core"
            )

        endpoint = result.get("endpoint_transaction")
        endpoint_keys = {
            "schema_version",
            "schema_sha256",
            "matrix_sha256",
            "subset_cell_ids",
            "subset_cells_sha256",
        }
        uav1_cells = [
            cell
            for cell in matrix.get("cells", [])
            if cell.get("uav", {}).get("name") == "uav1"
        ]
        expected_cell_ids = [
            f"uav1.{traffic_class}.{direction}"
            for traffic_class in TRAFFIC_CLASSES
            for direction in ("downlink", "uplink")
        ]
        subset_payload = json.dumps(
            uav1_cells,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        if (
            not isinstance(endpoint, dict)
            or set(endpoint) != endpoint_keys
            or endpoint.get("schema_version") != 1
            or endpoint.get("schema_sha256")
            != sha256_file(ROOT / "network/config/endpoint_transaction_schema.json")
            or endpoint.get("matrix_sha256")
            != sha256_file(ROOT / "network/config/endpoint_matrix_5uav.json")
            or endpoint.get("subset_cell_ids") != expected_cell_ids
            or endpoint.get("subset_cells_sha256") != sha256_bytes(subset_payload)
        ):
            raise ValidationError(
                "M2 receipt endpoint schema/matrix/uav1 subset is not exact"
            )

        expected_summary = {
            "contract": M2_EXTENSION_CONTRACT,
            "receipt": {
                "external_path": str(m2_receipt_path.resolve(strict=True)),
                "raw_copy_path": "raw/m2_component_host_final_receipt.json",
                "canonical_path": receipt.get("receipt_path"),
                "sha256": sha256_bytes(external_payload),
                "run_id": receipt.get("run_id"),
                "source_commit": source_commit,
                "result_sha256": receipt.get("result_sha256"),
            },
            "packet_engine": m2_engine,
            "endpoint_transaction": endpoint,
        }
        if run.get("m2_predecessor") != expected_summary:
            raise ValidationError(
                "M3 run contract does not exactly bind its M2 receipt predecessor"
            )
        shared = {
            "contract": M2_EXTENSION_CONTRACT,
            "m2_source_commit": source_commit,
            "m3_source_commit": repository_commit(),
            "packet_engine": {
                "contract": "ams.tap_packet_engine/v1",
                "program": "ams-tap-packet-engine",
                "source_sha256": current_source_hash,
                "binary_sha256": engine_identity.get("sha256"),
                "build_receipt_sha256": engine_identity.get("build_receipt", {}).get(
                    "sha256"
                ),
                "config_contract": "ams.tap_packet_engine/v1",
                "config_tool_sha256": current_config_tool_hash,
                "runner_sha256": current_runner_hash,
                "event_schema": ENGINE_SCHEMA,
                "m2_uav_count": 1,
                "m2_config_sha256": config_hashes,
                "m3_uav_count": 5,
                "m3_config_sha256": {},
            },
            "endpoint_transaction": {
                "schema_version": 1,
                "schema_sha256": endpoint["schema_sha256"],
                "matrix_sha256": endpoint["matrix_sha256"],
                "m2_subset_cell_ids": expected_cell_ids,
                "m2_subset_cells_sha256": endpoint["subset_cells_sha256"],
                "m3_cell_count": 30,
                "m3_resolved_cells_sha256": matrix.get("resolved_cells_sha256"),
            },
            "m2_receipt": expected_summary["receipt"],
        }
        details = {
            "predecessor_run_id": receipt.get("run_id"),
            "predecessor_receipt_sha256": sha256_bytes(external_payload),
        }
    except (KeyError, OSError, TypeError, ValueError, ValidationError) as exc:
        failures.append(str(exc))
    return failures, details, shared


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


def parse_mavlink(payload: bytes) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    offset = 0
    while offset < len(payload):
        if len(payload) - offset < 12 or payload[offset] != 0xFD:
            raise ValidationError("payload is not contiguous unsigned MAVLink v2")
        length = payload[offset + 1]
        frame_length = 12 + length
        if offset + frame_length > len(payload):
            raise ValidationError("MAVLink frame is truncated")
        frame = payload[offset : offset + frame_length]
        if frame[2:4] != b"\0\0":
            raise ValidationError("MAVLink incompat/compat flags are not zero")
        message_id = int.from_bytes(frame[7:10], "little")
        if message_id not in MAVLINK_CRC_EXTRA:
            raise ValidationError(
                f"MAVLink message id {message_id} is outside contract"
            )
        expected_crc = x25_crc(frame[1:-2] + bytes([MAVLINK_CRC_EXTRA[message_id]]))
        if int.from_bytes(frame[-2:], "little") != expected_crc:
            raise ValidationError("MAVLink X25 checksum mismatch")
        frames.append(
            {
                "message_id": message_id,
                "sequence": frame[4],
                "system_id": frame[5],
                "component_id": frame[6],
                "payload": frame[10:-2],
                "sha256": sha256_bytes(frame),
            }
        )
        offset += frame_length
    return frames


def parse_actual_mavlink_frame(payload: bytes) -> dict[str, Any]:
    """Decode one real MAVLink v1/v2 frame and verify its common-message CRC."""

    if not payload:
        raise ValidationError("actual MAVLink frame is empty")
    magic = payload[0]
    if magic == 0xFD:
        if len(payload) < 12:
            raise ValidationError("actual MAVLink v2 frame is truncated")
        body_length = payload[1]
        signed = bool(payload[2] & 0x01)
        frame_length = 12 + body_length + (13 if signed else 0)
        if len(payload) != frame_length:
            raise ValidationError("actual MAVLink v2 frame length is not exact")
        message_id = int.from_bytes(payload[7:10], "little")
        system_id, component_id = payload[5], payload[6]
        body = payload[10 : 10 + body_length]
        checksum_offset = 10 + body_length
        crc_material = payload[1:checksum_offset]
        version = 2
    elif magic == 0xFE:
        if len(payload) < 8:
            raise ValidationError("actual MAVLink v1 frame is truncated")
        body_length = payload[1]
        frame_length = 8 + body_length
        if len(payload) != frame_length:
            raise ValidationError("actual MAVLink v1 frame length is not exact")
        message_id = payload[5]
        system_id, component_id = payload[3], payload[4]
        body = payload[6 : 6 + body_length]
        checksum_offset = 6 + body_length
        crc_material = payload[1:checksum_offset]
        version = 1
    else:
        raise ValidationError("actual MAVLink frame has an unsupported magic byte")
    extra = MAVLINK_CRC_EXTRA.get(message_id)
    if extra is None:
        raise ValidationError(f"actual MAVLink message {message_id} is outside contract")
    expected_crc = x25_crc(crc_material + bytes([extra]))
    observed_crc = int.from_bytes(payload[checksum_offset : checksum_offset + 2], "little")
    if observed_crc != expected_crc:
        raise ValidationError("actual MAVLink frame CRC mismatch")
    return {
        "version": version,
        "message_id": message_id,
        "system_id": system_id,
        "component_id": component_id,
        "payload": body,
        "sha256": sha256_bytes(payload),
        "size": len(payload),
    }


def strict_hash_chain_audit(
    path: Path,
    *,
    run_id: Any,
    runtime_id: Any,
    run_nonce: Any,
    uav: str,
) -> list[dict[str, Any]]:
    """Validate an actual-SITL JSONL audit without trusting its producer."""

    if not regular_file(path):
        raise ValidationError(f"actual-SITL audit is absent/nonregular: {path.name}")
    payload = path.read_bytes()
    if not payload or not payload.endswith(b"\n"):
        raise ValidationError(f"actual-SITL audit is empty/incomplete: {path.name}")
    records: list[dict[str, Any]] = []
    previous: str | None = None
    for sequence, line in enumerate(payload.splitlines(keepends=True), start=1):
        def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValidationError(f"duplicate key {key!r}")
                result[key] = value
            return result

        try:
            record = json.loads(line, object_pairs_hook=unique)
        except (UnicodeError, json.JSONDecodeError, ValidationError) as exc:
            raise ValidationError(
                f"actual-SITL audit {path.name} line {sequence} is invalid: {exc}"
            ) from exc
        if not isinstance(record, dict):
            raise ValidationError(f"actual-SITL audit {path.name} line is not an object")
        if record.get("event_seq") != sequence:
            raise ValidationError(f"actual-SITL audit sequence gap: {path.name}")
        if record.get("previous_record_sha256") != previous:
            raise ValidationError(f"actual-SITL audit hash-chain break: {path.name}")
        if (
            record.get("schema_version"),
            record.get("run_id"),
            record.get("runtime_id"),
            record.get("run_nonce"),
            record.get("uav"),
        ) != (1, run_id, runtime_id, run_nonce, uav):
            raise ValidationError(f"actual-SITL audit identity mismatch: {path.name}")
        if not isinstance(record.get("monotonic_ns"), int):
            raise ValidationError(f"actual-SITL audit timestamp is invalid: {path.name}")
        previous = sha256_bytes(line)
        records.append(record)
    times = [int(record["monotonic_ns"]) for record in records]
    if times != sorted(times):
        raise ValidationError(f"actual-SITL audit time regressed: {path.name}")
    return records


def strict_control_event_audit(
    path: Path,
    *,
    run_id: Any,
    runtime_id: Any,
    run_nonce: Any,
) -> list[dict[str, Any]]:
    """Validate the neutral actual-control event log and its full hash chain."""

    if not regular_file(path):
        raise ValidationError("actual control event audit is absent/nonregular")
    payload = path.read_bytes()
    if not payload or not payload.endswith(b"\n"):
        raise ValidationError("actual control event audit is empty/incomplete")
    records: list[dict[str, Any]] = []
    previous: str | None = None
    for sequence, line in enumerate(payload.splitlines(keepends=True), start=1):
        def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValidationError(f"duplicate control audit key {key!r}")
                result[key] = value
            return result

        try:
            record = json.loads(line, object_pairs_hook=unique)
        except (UnicodeError, json.JSONDecodeError, ValidationError) as exc:
            raise ValidationError(
                f"actual control event audit line {sequence} is invalid: {exc}"
            ) from exc
        if not isinstance(record, dict):
            raise ValidationError("actual control audit record is not an object")
        if (
            record.get("schema"),
            record.get("run_id"),
            record.get("runtime_id"),
            record.get("run_nonce"),
            record.get("profile"),
            record.get("transport_nonce32"),
            record.get("transport_nonce_derivation"),
            record.get("role_subject"),
            record.get("event_sequence"),
            record.get("previous_record_sha256"),
        ) != (
            "ams.actual-sitl.control-event/v1",
            run_id,
            runtime_id,
            run_nonce,
            "m3",
            run_nonce,
            "identity/full_run_nonce32",
            "gcs_control_probe",
            sequence,
            previous,
        ):
            raise ValidationError("actual control event identity/hash chain differs")
        if not isinstance(record.get("monotonic_ns"), int):
            raise ValidationError("actual control event timestamp is invalid")
        previous = sha256_bytes(line)
        records.append(record)
    timestamps = [int(record["monotonic_ns"]) for record in records]
    if timestamps != sorted(timestamps):
        raise ValidationError("actual control event timestamps regressed")
    return records


def decode_marker(marker: str) -> dict[str, Any]:
    if len(marker) != 50 or not marker.startswith("AMS3"):
        raise ValidationError("STATUSTEXT marker length/prefix mismatch")
    base = marker[:44]
    if hashlib.sha256(base.encode()).hexdigest()[:6] != marker[44:]:
        raise ValidationError("STATUSTEXT marker checksum mismatch")
    run_nonce = marker[4:36]
    if not HEX32.fullmatch(run_nonce):
        raise ValidationError("STATUSTEXT marker nonce is not 16 raw bytes")
    reverse_phase = {value: key for key, value in PHASE_CODES.items()}
    reverse_direction = {value: key for key, value in DIRECTION_CODES.items()}
    try:
        phase_code = int(marker[36], 16)
        class_code = int(marker[37], 16)
        direction_code = int(marker[38], 16)
        uav = int(marker[39], 16)
        sequence = int(marker[40:44], 16)
    except ValueError as exc:
        raise ValidationError("STATUSTEXT marker codes are not hexadecimal") from exc
    if phase_code not in reverse_phase or class_code != CLASS_CODES["control"]:
        raise ValidationError("STATUSTEXT marker phase/class mismatch")
    if direction_code not in reverse_direction or not 1 <= uav <= 5 or sequence < 1:
        raise ValidationError("STATUSTEXT marker direction/UAV/sequence is invalid")
    phase = reverse_phase[phase_code]
    direction = reverse_direction[direction_code]
    flow = f"uav{uav}.control.{direction}"
    return {
        "phase": phase,
        "traffic_class": "control",
        "direction": direction,
        "uav": uav,
        "sequence": sequence,
        "run_nonce": run_nonce,
        "flow_id": flow,
        "record_nonce": sha256_bytes(marker.encode()),
        "p2mp": False,
        "application_unit_sha256": sha256_bytes(marker.encode()),
    }


def expected_record_nonce(
    run_nonce: str, phase: str, flow: str, sequence: int, sent_ns: int
) -> bytes:
    material = (
        bytes.fromhex(run_nonce)
        + bytes([PHASE_CODES[phase]])
        + flow.encode()
        + sequence.to_bytes(2, "big")
        + sent_ns.to_bytes(8, "big")
    )
    return hashlib.sha256(material).digest()[:16]


def decode_stream_record(payload: bytes) -> dict[str, Any]:
    if len(payload) != STREAM_RECORD.size:
        raise ValidationError("stream record size mismatch")
    unpacked = STREAM_RECORD.unpack(payload)
    magic, version = unpacked[:2]
    phase_code, class_code, direction_code, uav, flags, sequence = unpacked[2:8]
    nonce_bytes, sent_ns, flow_hash, nonce_hash = unpacked[8:]
    reverse_phase = {value: key for key, value in PHASE_CODES.items()}
    reverse_class = {value: key for key, value in CLASS_CODES.items()}
    reverse_direction = {value: key for key, value in DIRECTION_CODES.items()}
    if magic != b"AMU1" or version != 1:
        raise ValidationError("stream record magic/version mismatch")
    if phase_code not in reverse_phase or class_code not in reverse_class:
        raise ValidationError("stream record phase/class code mismatch")
    if direction_code not in reverse_direction or flags not in (0, 1) or sequence < 1:
        raise ValidationError("stream record direction/flags/sequence mismatch")
    phase = reverse_phase[phase_code]
    traffic_class = reverse_class[class_code]
    direction = reverse_direction[direction_code]
    p2mp = flags == 1
    if p2mp:
        if (phase, traffic_class, direction, uav) != (
            "p2mp",
            "additional_data",
            "downlink",
            0,
        ):
            raise ValidationError("P2MP record taxonomy mismatch")
        flow = "p2mp.additional_data.downlink"
    else:
        if not 1 <= uav <= 5 or phase == "p2mp":
            raise ValidationError("unicast stream record phase/UAV mismatch")
        flow = f"uav{uav}.{traffic_class}.{direction}"
    if flow_hash != hashlib.sha256(flow.encode()).digest()[:16]:
        raise ValidationError("stream record flow hash mismatch")
    run_nonce = nonce_bytes.hex()
    if nonce_hash != expected_record_nonce(run_nonce, phase, flow, sequence, sent_ns):
        raise ValidationError("stream record nonce hash mismatch")
    return {
        "phase": phase,
        "traffic_class": traffic_class,
        "direction": direction,
        "uav": uav,
        "sequence": sequence,
        "run_nonce": run_nonce,
        "flow_id": flow,
        "record_nonce": nonce_hash.hex(),
        "sent_monotonic_ns": sent_ns,
        "p2mp": p2mp,
        "application_unit_sha256": sha256_bytes(payload),
    }


def decode_forbidden_payload(payload: bytes, canary_id: str) -> dict[str, Any]:
    if len(payload) != FORBIDDEN_RECORD.size + 4:
        raise ValidationError("forbidden canary payload length is invalid")
    body, checksum_bytes = payload[:-4], payload[-4:]
    if zlib.crc32(body) != int.from_bytes(checksum_bytes, "big"):
        raise ValidationError("forbidden canary CRC mismatch")
    magic, version, kind_code, sequence, run_nonce, identity_hash = (
        FORBIDDEN_RECORD.unpack(body)
    )
    if magic != FORBIDDEN_MAGIC or version != 1:
        raise ValidationError("forbidden canary magic/version mismatch")
    if identity_hash != hashlib.sha256(canary_id.encode("ascii")).digest():
        raise ValidationError("forbidden canary identity hash mismatch")
    kinds = {code: name for name, code in FORBIDDEN_KIND_CODES.items()}
    if kind_code not in kinds:
        raise ValidationError("forbidden canary kind code is invalid")
    return {
        "kind": kinds[kind_code],
        "sequence": sequence,
        "run_nonce": run_nonce.hex(),
        "transport_payload_sha256": sha256_bytes(payload),
        "transport_payload_size": len(payload),
    }


def expected_forbidden_canaries(run_nonce: str) -> list[dict[str, Any]]:
    capture_names = {
        *(f"endpoint-{endpoint}.pcap" for endpoint in ENDPOINTS),
        *(f"ns3-external-{endpoint}.pcap" for endpoint in ENDPOINTS),
        *(f"loopback-{endpoint}.pcap" for endpoint in ENDPOINTS),
        *(f"tail-uav{index}.pcap" for index in range(1, 6)),
        *(f"tail-root-uav{index}.pcap" for index in range(1, 6)),
        "loopback-container-root.pcap",
    }
    records: list[dict[str, Any]] = []

    def add(
        canary_id: str,
        kind: str,
        source_endpoint: str,
        source_namespace: str,
        family: str,
        source_ip: str,
        source_port: int,
        destination_ip: str,
        destination_port: int,
        listener_endpoint: str,
        expected_names: list[str],
    ) -> None:
        sequence = len(records) + 1
        body = FORBIDDEN_RECORD.pack(
            FORBIDDEN_MAGIC,
            1,
            FORBIDDEN_KIND_CODES[kind],
            sequence,
            bytes.fromhex(run_nonce),
            hashlib.sha256(canary_id.encode("ascii")).digest(),
        )
        payload = body + zlib.crc32(body).to_bytes(4, "big")
        expected = sorted(expected_names)
        records.append(
            {
                "canary_id": canary_id,
                "kind": kind,
                "sequence": sequence,
                "source_endpoint": source_endpoint,
                "source_namespace": source_namespace,
                "address_family": family,
                "source_ip": source_ip,
                "source_udp_port": source_port,
                "destination_ip": destination_ip,
                "destination_udp_port": destination_port,
                "listener_endpoint": listener_endpoint,
                "listener_namespace": NAMESPACES[listener_endpoint],
                "tos": 0,
                "transport_payload_hex": payload.hex(),
                "transport_payload_sha256": sha256_bytes(payload),
                "transport_payload_size": len(payload),
                "expected_capture_names": expected,
                "forbidden_capture_names": sorted(capture_names - set(expected)),
                "expected_send_return_size": len(payload),
                "remote_application_delivery": "forbidden_zero",
            }
        )

    loopback_sources = (
        ("container-root", "container-root", "container-root"),
        *((endpoint, NAMESPACES[endpoint], endpoint) for endpoint in ENDPOINTS),
    )
    for index, (source, namespace, suffix) in enumerate(loopback_sources):
        listener = "uav1" if source == "gcs" else "gcs"
        add(
            f"loopback_ipv4.{source}",
            "loopback_ipv4",
            source,
            namespace,
            "ipv4",
            "127.0.0.1",
            15400 + index,
            "127.0.0.1",
            15500 + index,
            listener,
            [f"loopback-{suffix}.pcap"],
        )
        add(
            f"loopback_ipv6.{source}",
            "loopback_ipv6",
            source,
            namespace,
            "ipv6",
            "::1",
            15600 + index,
            "::1",
            15700 + index,
            listener,
            [f"loopback-{suffix}.pcap"],
        )
    for index in range(1, 6):
        add(
            f"legacy_direct_port.uav{index}",
            "legacy_direct_port",
            "gcs",
            "ams-gcs",
            "ipv4",
            "10.71.0.10",
            15200 + index,
            f"10.71.{index}.10",
            14550,
            f"uav{index}",
            [
                "endpoint-gcs.pcap",
                "ns3-external-gcs.pcap",
            ],
        )
    add(
        "unreachable_ipv4.gcs",
        "unreachable_ipv4",
        "gcs",
        "ams-gcs",
        "ipv4",
        "10.71.0.10",
        15301,
        "198.18.0.1",
        15300,
        "uav1",
        ["endpoint-gcs.pcap", "ns3-external-gcs.pcap"],
    )
    return records


def expected_root_loopback_packet_filter(run_nonce: str) -> str:
    """Independently derive the lossless root-loopback canary capture filter."""

    ports = sorted(
        {
            int(record[field])
            for record in expected_forbidden_canaries(run_nonce)
            for field in ("source_udp_port", "destination_udp_port")
        }
    )
    return CAPTURE_UDP_PORT_FILTER_PREFIX + ",".join(str(port) for port in ports)


def decode_transport(payload_hex: Any) -> dict[str, Any]:
    if not isinstance(payload_hex, str) or len(payload_hex) % 2:
        raise ValidationError("transport_payload_hex is not even-length hexadecimal")
    try:
        payload = bytes.fromhex(payload_hex)
    except ValueError as exc:
        raise ValidationError("transport_payload_hex is invalid") from exc
    if payload.startswith(b"AMSD"):
        if len(payload) < 10:
            raise ValidationError("additional-data frame is truncated")
        length = int.from_bytes(payload[4:6], "big")
        if len(payload) != length + 10:
            raise ValidationError("additional-data length mismatch")
        record = payload[6 : 6 + length]
        if int.from_bytes(payload[-4:], "big") != zlib.crc32(record):
            raise ValidationError("additional-data CRC mismatch")
        decoded = decode_stream_record(record)
        if decoded["traffic_class"] != "additional_data":
            raise ValidationError("additional-data frame carries another class")
        family = "AMS_ADDITIONAL_DATA_V1"
        frames: list[dict[str, Any]] = []
        source_system = target_system = source_component = target_component = None
    else:
        frames = parse_mavlink(payload)
        ids = [frame["message_id"] for frame in frames]
        source_ids = {(frame["system_id"], frame["component_id"]) for frame in frames}
        if len(source_ids) != 1:
            raise ValidationError("MAVLink frames mix source identities")
        source_system, source_component = next(iter(source_ids))
        if ids in ([253, 76], [253, 77, 0, 33]):
            marker_payload = frames[0]["payload"]
            if len(marker_payload) != 51 or marker_payload[0] != 6:
                raise ValidationError("STATUSTEXT marker payload mismatch")
            try:
                marker = marker_payload[1:].rstrip(b"\0").decode("ascii")
            except UnicodeError as exc:
                raise ValidationError("STATUSTEXT marker is not ASCII") from exc
            decoded = decode_marker(marker)
            if ids == [253, 76]:
                if (
                    decoded["direction"] != "downlink"
                    or len(frames[1]["payload"]) != 33
                ):
                    raise ValidationError("control downlink family/direction mismatch")
                command = struct.unpack("<7fHBBB", frames[1]["payload"])
                if command[7] != 512 or command[10] != 0:
                    raise ValidationError("COMMAND_LONG command/confirmation mismatch")
                target_system, target_component = command[8:10]
                family = "STATUSTEXT_MARKER_PLUS_COMMAND_LONG"
            else:
                if decoded["direction"] != "uplink" or len(frames[1]["payload"]) != 10:
                    raise ValidationError("control uplink family/direction mismatch")
                ack = struct.unpack("<HBBiBB", frames[1]["payload"])
                if ack[0:3] != (512, 0, 100):
                    raise ValidationError(
                        "COMMAND_ACK command/result/progress mismatch"
                    )
                target_system, target_component = ack[4:6]
                if frames[2]["payload"][-1] != 3:
                    raise ValidationError("HEARTBEAT MAVLink version mismatch")
                family = "COMMAND_ACK_HEARTBEAT_REQUESTED_TELEMETRY"
        elif ids == [385]:
            tunnel = frames[0]["payload"]
            if len(tunnel) != 133:
                raise ValidationError("MAVLink TUNNEL size mismatch")
            payload_type, target_system, target_component, length = struct.unpack(
                "<HBBB", tunnel[:5]
            )
            if (
                payload_type != 20000
                or not 1 <= length <= 128
                or any(tunnel[5 + length :])
            ):
                raise ValidationError("MAVLink TUNNEL type/length/padding mismatch")
            decoded = decode_stream_record(tunnel[5 : 5 + length])
            if decoded["traffic_class"] != "payload":
                raise ValidationError("MAVLink TUNNEL carries another class")
            family = "MAVLINK_TUNNEL_V2"
        else:
            raise ValidationError(f"MAVLink family {ids} is outside M3 taxonomy")
    decoded.update(
        {
            "protocol_family": family,
            "transport_payload_sha256": sha256_bytes(payload),
            "transport_payload_size": len(payload),
            "mavlink_frame_sha256": [frame["sha256"] for frame in frames],
            "source_system": source_system,
            "source_component": source_component,
            "target_system": target_system,
            "target_component": target_component,
        }
    )
    return decoded


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def metric_record(
    offered: list[dict[str, Any]], received: list[dict[str, Any]], duration_ns: int
) -> dict[str, Any]:
    offered_by_key = {
        (record["record_nonce"], record["transport_payload_sha256"]): record
        for record in offered
    }
    latencies = [
        (record["received_monotonic_ns"] - offered_by_key[key]["sent_monotonic_ns"])
        / 1_000_000
        for record in received
        if (key := (record["record_nonce"], record["transport_payload_sha256"]))
        in offered_by_key
    ]
    latency_sorted = sorted(latencies)
    jitter = [
        abs(right - left) for left, right in zip(latency_sorted, latency_sorted[1:])
    ]
    delivered_bytes = sum(record["transport_payload_size"] for record in received)
    return {
        "offered_unique": len(offered_by_key),
        "received_unique": len(received),
        "delivery_ratio": round(len(received) / len(offered_by_key), 9)
        if offered_by_key
        else 0.0,
        "loss_ratio": round(1.0 - len(received) / len(offered_by_key), 9)
        if offered_by_key
        else 1.0,
        "latency_sample_count": len(latencies),
        "latency_p50_ms": round(percentile(latencies, 0.5), 6)
        if latencies
        else "inapplicable",
        "latency_p95_ms": round(percentile(latencies, 0.95), 6)
        if latencies
        else "inapplicable",
        "jitter_mean_ms": round(sum(jitter) / len(jitter), 6)
        if jitter
        else "inapplicable",
        "goodput_bps": round(delivered_bytes * 8 * 1_000_000_000 / duration_ns, 6)
        if received and duration_ns > 0
        else "inapplicable",
        "outcome_timeout_ms": 3000 if not received else "inapplicable",
    }


def _internet_checksum(payload: bytes) -> int:
    if len(payload) % 2:
        payload += b"\0"
    total = sum(
        int.from_bytes(payload[offset : offset + 2], "big")
        for offset in range(0, len(payload), 2)
    )
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


def _decode_ethernet_udp(
    frame: bytes,
    *,
    frame_index: int,
    timestamp_ns: int,
) -> tuple[dict[str, Any] | None, str | None]:
    """Decode one Ethernet/IPv4/UDP frame without trusting capture labels."""

    if len(frame) < 14:
        return None, f"frame {frame_index} is shorter than an Ethernet header"
    destination_mac = ":".join(f"{byte:02x}" for byte in frame[0:6])
    source_mac = ":".join(f"{byte:02x}" for byte in frame[6:12])
    ethertype = int.from_bytes(frame[12:14], "big")
    network_offset = 14
    vlan_tags: list[dict[str, int]] = []
    while ethertype in {0x8100, 0x88A8}:
        if len(vlan_tags) >= 2 or len(frame) < network_offset + 4:
            return None, f"frame {frame_index} has invalid VLAN encapsulation"
        vlan_tags.append(
            {
                "ethertype": ethertype,
                "tci": int.from_bytes(
                    frame[network_offset : network_offset + 2], "big"
                ),
            }
        )
        ethertype = int.from_bytes(
            frame[network_offset + 2 : network_offset + 4], "big"
        )
        network_offset += 4
    if ethertype == 0x86DD:
        if len(frame) < network_offset + 40:
            return None, f"frame {frame_index} has a truncated IPv6 header"
        if frame[network_offset] >> 4 != 6:
            return None, f"frame {frame_index} has an invalid IPv6 version"
        payload_length = int.from_bytes(
            frame[network_offset + 4 : network_offset + 6], "big"
        )
        if network_offset + 40 + payload_length > len(frame):
            return None, f"frame {frame_index} has an invalid IPv6 payload length"
        next_header = frame[network_offset + 6]
        if next_header != 17:
            return None, None
        transport_offset = network_offset + 40
        if payload_length < 8:
            return None, f"frame {frame_index} has a truncated IPv6 UDP header"
        udp_length = int.from_bytes(
            frame[transport_offset + 4 : transport_offset + 6], "big"
        )
        if udp_length < 8 or udp_length > payload_length:
            return None, f"frame {frame_index} has an invalid IPv6 UDP length"
        transport_payload = frame[transport_offset + 8 : transport_offset + udp_length]
        return (
            {
                "frame_index": frame_index,
                "timestamp_ns": timestamp_ns,
                "wire_frame_sha256": sha256_bytes(frame),
                "wire_frame_size": len(frame),
                "source_mac": source_mac,
                "destination_mac": destination_mac,
                "vlan_tags": vlan_tags,
                "address_family": "ipv6",
                "tos": ((frame[network_offset] & 0x0F) << 4)
                | (frame[network_offset + 1] >> 4),
                "source_ip": str(
                    ipaddress.IPv6Address(
                        frame[network_offset + 8 : network_offset + 24]
                    )
                ),
                "destination_ip": str(
                    ipaddress.IPv6Address(
                        frame[network_offset + 24 : network_offset + 40]
                    )
                ),
                "source_udp_port": int.from_bytes(
                    frame[transport_offset : transport_offset + 2], "big"
                ),
                "destination_udp_port": int.from_bytes(
                    frame[transport_offset + 2 : transport_offset + 4], "big"
                ),
                "transport_payload_sha256": sha256_bytes(transport_payload),
                "transport_payload_size": len(transport_payload),
            },
            None,
        )
    if ethertype != 0x0800:
        return None, None
    if len(frame) < network_offset + 20:
        return None, f"frame {frame_index} has a truncated IPv4 header"
    version_ihl = frame[network_offset]
    version = version_ihl >> 4
    ihl = (version_ihl & 0x0F) * 4
    if version != 4 or ihl < 20 or len(frame) < network_offset + ihl:
        return None, f"frame {frame_index} has an invalid IPv4 header length"
    total_length = int.from_bytes(frame[network_offset + 2 : network_offset + 4], "big")
    if total_length < ihl or network_offset + total_length > len(frame):
        return None, f"frame {frame_index} has an invalid IPv4 total length"
    ipv4_header = frame[network_offset : network_offset + ihl]
    if _internet_checksum(ipv4_header) != 0:
        return None, f"frame {frame_index} has an invalid IPv4 checksum"
    fragment = int.from_bytes(frame[network_offset + 6 : network_offset + 8], "big")
    if fragment & 0x3FFF:
        return None, f"frame {frame_index} contains unsupported IPv4 fragmentation"
    protocol = frame[network_offset + 9]
    if protocol != 17:
        return None, None
    transport_offset = network_offset + ihl
    if total_length - ihl < 8:
        return None, f"frame {frame_index} has a truncated UDP header"
    udp_length = int.from_bytes(
        frame[transport_offset + 4 : transport_offset + 6], "big"
    )
    if udp_length < 8 or udp_length > total_length - ihl:
        return None, f"frame {frame_index} has an invalid UDP length"
    transport_payload = frame[transport_offset + 8 : transport_offset + udp_length]
    return (
        {
            "frame_index": frame_index,
            "timestamp_ns": timestamp_ns,
            "wire_frame_sha256": sha256_bytes(frame),
            "wire_frame_size": len(frame),
            "source_mac": source_mac,
            "destination_mac": destination_mac,
            "vlan_tags": vlan_tags,
            "address_family": "ipv4",
            "tos": frame[network_offset + 1],
            "source_ip": str(
                ipaddress.IPv4Address(frame[network_offset + 12 : network_offset + 16])
            ),
            "destination_ip": str(
                ipaddress.IPv4Address(frame[network_offset + 16 : network_offset + 20])
            ),
            "source_udp_port": int.from_bytes(
                frame[transport_offset : transport_offset + 2], "big"
            ),
            "destination_udp_port": int.from_bytes(
                frame[transport_offset + 2 : transport_offset + 4], "big"
            ),
            "transport_payload_sha256": sha256_bytes(transport_payload),
            "transport_payload_size": len(transport_payload),
        },
        None,
    )


def parse_pcap(path: Path) -> tuple[int, list[dict[str, Any]], list[str]]:
    """Strictly parse PCAP and independently decode Ethernet/IPv4/UDP bytes."""

    if not regular_file(path):
        return 0, [], [f"missing/nonregular/hardlinked PCAP: {path}"]
    data = path.read_bytes()
    if len(data) < 24:
        return 0, [], [f"PCAP {path.name} is truncated"]
    magics = {
        b"\xd4\xc3\xb2\xa1": ("little", 1_000),
        b"\xa1\xb2\xc3\xd4": ("big", 1_000),
        b"\x4d\x3c\xb2\xa1": ("little", 1),
        b"\xa1\xb2\x3c\x4d": ("big", 1),
    }
    magic = magics.get(data[:4])
    if magic is None:
        return 0, [], [f"PCAP {path.name} magic is invalid"]
    byteorder, subsecond_scale = magic
    major = int.from_bytes(data[4:6], byteorder)
    minor = int.from_bytes(data[6:8], byteorder)
    snaplen = int.from_bytes(data[16:20], byteorder)
    linktype = int.from_bytes(data[20:24], byteorder)
    errors: list[str] = []
    if (major, minor) != (2, 4):
        errors.append(f"PCAP {path.name} version is not 2.4")
    if snaplen != 65_535:
        errors.append(f"PCAP {path.name} snaplen is not 65535")
    if linktype != 1:
        errors.append(f"PCAP {path.name} linktype is not Ethernet")
    offset = 24
    count = 0
    decoded: list[dict[str, Any]] = []
    while offset < len(data):
        if len(data) - offset < 16:
            errors.append(f"PCAP {path.name} has truncated record header")
            break
        seconds = int.from_bytes(data[offset : offset + 4], byteorder)
        subseconds = int.from_bytes(data[offset + 4 : offset + 8], byteorder)
        included = int.from_bytes(data[offset + 8 : offset + 12], byteorder)
        original = int.from_bytes(data[offset + 12 : offset + 16], byteorder)
        resolution = 1_000_000 if subsecond_scale == 1_000 else 1_000_000_000
        if subseconds >= resolution:
            errors.append(f"PCAP {path.name} record {count + 1} timestamp is invalid")
            break
        if (
            included < 1
            or included != original
            or included > snaplen
            or offset + 16 + included > len(data)
        ):
            errors.append(f"PCAP {path.name} record {count + 1} lengths are invalid")
            break
        count += 1
        frame = data[offset + 16 : offset + 16 + included]
        record, error = _decode_ethernet_udp(
            frame,
            frame_index=count,
            timestamp_ns=seconds * 1_000_000_000 + subseconds * subsecond_scale,
        )
        if error is not None:
            errors.append(f"PCAP {path.name} {error}")
        elif record is not None:
            decoded.append(record)
        offset += 16 + included
    if count == 0:
        errors.append(f"PCAP {path.name} has no packet records")
    return count, decoded, errors


def gate(failures: list[str], details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"passed": not failures, "failures": failures, "details": details or {}}


def _firewall_failures(record: dict[str, Any], label: str) -> list[str]:
    failures: list[str] = []
    nftables = record.get("nftables")
    entries = nftables.get("nftables") if isinstance(nftables, dict) else None
    if not isinstance(entries, list) or any(
        not isinstance(entry, dict) or set(entry) != {"metainfo"} for entry in entries
    ):
        failures.append(f"{label} nftables ruleset is not empty")
    for family in ("iptables_ipv4", "iptables_ipv6"):
        lines = record.get(family)
        if not isinstance(lines, list) or any(
            not isinstance(line, str) for line in lines
        ):
            failures.append(f"{label} {family} output is malformed")
            continue
        for line in lines:
            if line.startswith("-A "):
                failures.append(f"{label} {family} contains a packet rule")
            if line.startswith(":"):
                fields = line.split()
                if len(fields) < 2 or fields[1] not in {"ACCEPT", "-"}:
                    failures.append(
                        f"{label} {family} contains a non-ACCEPT chain policy"
                    )
    return failures


def _default_rule_failures(rules: Any, label: str) -> list[str]:
    if not isinstance(rules, list) or any(not isinstance(rule, dict) for rule in rules):
        return [f"{label} policy-rule inventory is malformed"]
    observed = {(rule.get("priority"), str(rule.get("table"))) for rule in rules}
    allowed = {
        (0, "local"),
        (32766, "main"),
        (32767, "default"),
        (0, "255"),
        (32766, "254"),
        (32767, "253"),
    }
    priorities = {priority for priority, _table in observed}
    if priorities not in ({0, 32766, 32767}, {0, 32766}):
        return [f"{label} policy-rule priorities are not kernel defaults"]
    if any(item not in allowed for item in observed):
        return [f"{label} contains a non-default policy route rule"]
    forbidden_fields = {
        "fwmark",
        "fwmask",
        "iif",
        "oif",
        "goto",
        "nat",
        "sport",
        "dport",
        "ipproto",
        "uidrange",
    }
    if any(forbidden_fields & set(rule) for rule in rules):
        return [f"{label} contains a selector/NAT policy route rule"]
    return []


def _continuous_namespace_failures(
    namespace: str, record: Any, label: str
) -> list[str]:
    failures: list[str] = []
    expected_keys = {
        "present",
        "namespace_inode",
        "links",
        "addresses",
        "routes_ipv4",
        "routes_ipv6",
        "rules_ipv4",
        "rules_ipv6",
        "neighbours_ipv4",
        "neighbours_ipv6",
        "bridge_links",
        "sockets",
        "nftables",
        "iptables_ipv4",
        "iptables_ipv6",
    }
    if not isinstance(record, dict) or set(record) != expected_keys:
        return [f"{label} namespace record keys are not exact"]
    if record.get("present") is not True or not isinstance(
        record.get("namespace_inode"), int
    ):
        return [f"{label} namespace is absent or has no inode"]
    for field in (
        "links",
        "addresses",
        "routes_ipv4",
        "routes_ipv6",
        "rules_ipv4",
        "rules_ipv6",
        "neighbours_ipv4",
        "neighbours_ipv6",
        "bridge_links",
        "sockets",
        "iptables_ipv4",
        "iptables_ipv6",
    ):
        if not isinstance(record.get(field), list):
            failures.append(f"{label} {field} is not a raw list")
    if failures:
        return failures
    failures.extend(_firewall_failures(record, label))
    failures.extend(_default_rule_failures(record["rules_ipv4"], f"{label}/IPv4"))
    failures.extend(_default_rule_failures(record["rules_ipv6"], f"{label}/IPv6"))
    link_names = {item.get("ifname") for item in record["links"]}
    main_ipv4_routes: list[dict[str, Any]] = []
    local_ipv4_routes: list[dict[str, Any]] = []
    for route in record["routes_ipv4"]:
        if not isinstance(route, dict):
            failures.append(f"{label} IPv4 route record is not an object")
            continue
        table = route.get("table", "main")
        if table in {"main", 254, "254"}:
            main_ipv4_routes.append(route)
        elif table in {"local", 255, "255"}:
            local_ipv4_routes.append(route)
        else:
            failures.append(f"{label} has a route in an undeclared IPv4 table")

    loopback_local_routes = {
        ("local", "127.0.0.0/8", "lo"),
        ("local", "127.0.0.1", "lo"),
        ("broadcast", "127.255.255.255", "lo"),
    }

    def local_route_id(route: dict[str, Any]) -> tuple[str, str, str]:
        return (
            str(route.get("type", "unicast")),
            str(route.get("dst")),
            str(route.get("dev")),
        )

    if namespace == "container-root":
        expected_links = {"lo", *(f"ams-tail{index}" for index in range(1, 6))}
        if link_names != expected_links:
            failures.append(f"{label} root tail interface set is not exact")
        root_ipv4 = {
            (link.get("ifname"), info.get("local"), info.get("prefixlen"))
            for link in record["addresses"]
            if link.get("ifname") != "lo"
            for info in link.get("addr_info", [])
            if info.get("family") == "inet"
        }
        expected_ipv4 = {
            (f"ams-tail{index}", f"10.72.{index}.1", 30)
            for index in range(1, 6)
        }
        if root_ipv4 != expected_ipv4:
            failures.append(f"{label} root tail IPv4 set is not exact")
        if any(route.get("dst") == "default" for route in main_ipv4_routes):
            failures.append(f"{label} root container has an undeclared default route")
        allowed_routes = {
            (f"10.72.{index}.0/30", f"ams-tail{index}")
            for index in range(1, 6)
        }
        observed_routes = {
            (str(route.get("dst")), str(route.get("dev")))
            for route in main_ipv4_routes
        }
        if observed_routes != allowed_routes:
            failures.append(f"{label} root tail route set is not exact")
        expected_local_routes = loopback_local_routes | {
            route
            for index in range(1, 6)
            for route in (
                ("local", f"10.72.{index}.1", f"ams-tail{index}"),
                ("broadcast", f"10.72.{index}.3", f"ams-tail{index}"),
            )
        }
        if (
            {local_route_id(route) for route in local_ipv4_routes}
            != expected_local_routes
        ):
            failures.append(f"{label} root local IPv4 route set is not exact")
        return failures
    if namespace == "ams-ns3":
        expected = (
            {"lo"}
            | {f"br-{endpoint}" for endpoint in ENDPOINTS}
            | {f"tap-{endpoint}" for endpoint in ENDPOINTS}
            | {f"vp-{endpoint}" for endpoint in ENDPOINTS}
        )
        if link_names != expected:
            failures.append(f"{label} ns3 interface set is not exact")
        nonlocal_addresses = [
            info
            for link in record["addresses"]
            if link.get("ifname") != "lo"
            for info in link.get("addr_info", [])
            if not (
                info.get("family") == "inet6"
                and info.get("scope") == "link"
                and str(info.get("local", "")).lower().startswith("fe80:")
            )
        ]
        if nonlocal_addresses or main_ipv4_routes:
            failures.append(f"{label} ns3 namespace has an IP routing bypass")
        if (
            {local_route_id(route) for route in local_ipv4_routes}
            != loopback_local_routes
        ):
            failures.append(f"{label} ns3 local IPv4 route set is not exact")
        # `bridge -j -d link show` includes one self row per bridge on current
        # iproute2.  Self rows are bridge identities, not enslaved ports.  Keep
        # the raw rows in evidence, but compare every actual port exactly.
        members = [
            (item.get("ifname"), item.get("master"))
            for item in record["bridge_links"]
            if isinstance(item, dict) and item.get("ifname") != item.get("master")
        ]
        expected_members = {
            (f"tap-{endpoint}", f"br-{endpoint}") for endpoint in ENDPOINTS
        } | {(f"vp-{endpoint}", f"br-{endpoint}") for endpoint in ENDPOINTS}
        if len(members) != len(expected_members) or set(members) != expected_members:
            failures.append(f"{label} ns3 bridge membership is not exact")
        if record["neighbours_ipv4"] or record["neighbours_ipv6"]:
            failures.append(f"{label} ns3 Linux namespace has neighbour state")
        return failures
    endpoint = "gcs" if namespace == "ams-gcs" else namespace.removeprefix("ams-")
    index = 0 if endpoint == "gcs" else int(endpoint[3:])
    expected_endpoint_links = (
        {"lo", "eth0", "tail0"} if endpoint != "gcs" else {"lo", "eth0"}
    )
    if link_names != expected_endpoint_links:
        failures.append(f"{label} endpoint interface set is not exact")
    eth0 = next((item for item in record["links"] if item.get("ifname") == "eth0"), {})
    if eth0.get("address") != f"02:71:{index:02x}:00:10:10":
        failures.append(f"{label} endpoint MAC is not exact")
    ipv4 = [
        (info.get("local"), info.get("prefixlen"))
        for link in record["addresses"]
        if link.get("ifname") == "eth0"
        for info in link.get("addr_info", [])
        if info.get("family") == "inet"
    ]
    if ipv4 != [(f"10.71.{index}.10", 24)]:
        failures.append(f"{label} endpoint IPv4 identity is not exact")
    if endpoint != "gcs":
        tail_ipv4 = [
            (info.get("local"), info.get("prefixlen"))
            for link in record["addresses"]
            if link.get("ifname") == "tail0"
            for info in link.get("addr_info", [])
            if info.get("family") == "inet"
        ]
        if tail_ipv4 != [(f"10.72.{index}.2", 30)]:
            failures.append(f"{label} actual MAVProxy tail IPv4 identity is not exact")
    defaults = [
        route for route in main_ipv4_routes if route.get("dst") == "default"
    ]
    if (
        len(defaults) != 1
        or defaults[0].get("gateway") != f"10.71.{index}.1"
        or defaults[0].get("dev") != "eth0"
    ):
        failures.append(f"{label} endpoint default route is not exact")
    segment = ipaddress.IPv4Network(f"10.71.{index}.0/24")
    for route in main_ipv4_routes:
        destination = route.get("dst")
        if destination == "default":
            continue
        try:
            network = ipaddress.IPv4Network(str(destination), strict=False)
        except ValueError:
            failures.append(f"{label} endpoint has malformed IPv4 route")
            continue
        tail_segment = ipaddress.IPv4Network(f"10.72.{index}.0/30")
        route_dev = route.get("dev")
        if not network.subnet_of(segment) and not (
            endpoint != "gcs"
            and network == tail_segment
            and route_dev == "tail0"
        ):
            failures.append(f"{label} endpoint has an out-of-segment IPv4 route")
    expected_local_routes = loopback_local_routes | {
        ("local", f"10.71.{index}.10", "eth0"),
        ("broadcast", f"10.71.{index}.255", "eth0"),
    }
    if endpoint != "gcs":
        expected_local_routes |= {
            ("local", f"10.72.{index}.2", "tail0"),
            ("broadcast", f"10.72.{index}.3", "tail0"),
        }
    if (
        {local_route_id(route) for route in local_ipv4_routes}
        != expected_local_routes
    ):
        failures.append(f"{label} endpoint local IPv4 route set is not exact")

    allowed_neighbour = (
        f"10.71.{index}.1",
        "eth0",
        f"02:71:{index:02x}:00:00:01",
    )
    expected_neighbours = {allowed_neighbour}
    if endpoint != "gcs":
        expected_neighbours.add(
            (
                f"10.72.{index}.1",
                "tail0",
                f"02:72:{index:02x}:00:00:01",
            )
        )
    for neighbour in record["neighbours_ipv4"]:
        identity = (
            neighbour.get("dst"),
            neighbour.get("dev"),
            neighbour.get("lladdr"),
        )
        unresolved_expected_gateway = (
            neighbour.get("lladdr") is None
            and neighbour.get("state") in (["INCOMPLETE"], ["FAILED"])
            and any(
                identity[:2] == candidate[:2] for candidate in expected_neighbours
            )
        )
        if identity not in expected_neighbours and not unresolved_expected_gateway:
            failures.append(f"{label} endpoint has an undeclared IPv4 neighbour")
    if record["neighbours_ipv6"] or record["bridge_links"]:
        failures.append(f"{label} endpoint has IPv6 neighbours/bridge membership")
    return failures


def validate_continuous_topology(
    run_dir: Path,
    *,
    run_id: Any,
    runtime_id: Any,
    run_nonce: Any,
    lifecycle_events: list[str],
    windows: dict[str, Any],
) -> tuple[list[str], dict[str, Any]]:
    failures: list[str] = []
    details: dict[str, Any] = {}
    base = run_dir / "raw/topology_monitor"
    try:
        samples = strict_jsonl(base / "samples.jsonl")
        summary = strict_json(base / "summary.json")
        ready = strict_json(base / "ready.json")
        sample_keys = {
            "schema",
            "run_id",
            "runtime_id",
            "run_nonce",
            "sample_sequence",
            "monotonic_ns",
            "reason",
            "transition_sequence",
            "transition_event",
            "command_sha256",
            "namespaces",
            "processes",
            "netlink_monitors",
        }
        if any(set(sample) != sample_keys for sample in samples):
            failures.append("continuous topology sample keys are not exact")
        if [sample.get("sample_sequence") for sample in samples] != list(
            range(1, len(samples) + 1)
        ):
            failures.append("continuous topology sample sequence is not contiguous")
        sample_times = [sample.get("monotonic_ns") for sample in samples]
        if any(
            not isinstance(value, int) for value in sample_times
        ) or sample_times != sorted(sample_times):
            failures.append("continuous topology sample timestamps are invalid")
            sample_times = []
        if any(
            (
                sample.get("schema"),
                sample.get("run_id"),
                sample.get("runtime_id"),
                sample.get("run_nonce"),
            )
            != (TOPOLOGY_SAMPLE_SCHEMA, run_id, runtime_id, run_nonce)
            for sample in samples
        ):
            failures.append("continuous topology samples cross run identity/schema")
        transition_samples = [
            sample for sample in samples if sample.get("reason") == "transition"
        ]
        transition_events = [
            sample.get("transition_event") for sample in transition_samples
        ]
        if transition_events != lifecycle_events:
            failures.append(
                "continuous topology transitions differ from lifecycle events"
            )
        if [sample.get("transition_sequence") for sample in transition_samples] != list(
            range(1, len(transition_samples) + 1)
        ):
            failures.append("continuous topology transition sequence is not contiguous")
        if any(
            sample.get("reason") not in {"periodic", "transition"}
            or (
                sample.get("reason") == "periodic"
                and any(
                    sample.get(field) is not None
                    for field in (
                        "transition_sequence",
                        "transition_event",
                        "command_sha256",
                    )
                )
            )
            for sample in samples
        ):
            failures.append("continuous topology sample reason fields are invalid")

        summary_keys = {
            "contract",
            "run_id",
            "runtime_id",
            "run_nonce",
            "interval_ms",
            "sample_count",
            "first_sample_monotonic_ns",
            "last_sample_monotonic_ns",
            "maximum_sample_gap_ns",
            "transition_events",
            "command_paths",
            "netlink_monitors",
            "stopped_monotonic_ns",
        }
        if set(summary) != summary_keys or (
            summary.get("contract"),
            summary.get("run_id"),
            summary.get("runtime_id"),
            summary.get("run_nonce"),
        ) != (TOPOLOGY_SUMMARY_CONTRACT, run_id, runtime_id, run_nonce):
            failures.append("continuous topology summary identity/keys mismatch")
        recomputed_gap = max(
            (right - left for left, right in zip(sample_times, sample_times[1:])),
            default=0,
        )
        if (
            summary.get("interval_ms") != 500
            or summary.get("sample_count") != len(samples)
            or summary.get("first_sample_monotonic_ns")
            != (sample_times[0] if sample_times else None)
            or summary.get("last_sample_monotonic_ns")
            != (sample_times[-1] if sample_times else None)
            or summary.get("maximum_sample_gap_ns") != recomputed_gap
            or recomputed_gap > 1_000_000_000
            or summary.get("transition_events") != lifecycle_events
        ):
            failures.append(
                "continuous topology summary/cadence does not match raw samples"
            )
        command_paths = summary.get("command_paths")
        if not isinstance(command_paths, dict) or set(command_paths) != {
            "ip",
            "bridge",
            "ss",
            "nft",
            "iptables-save",
            "ip6tables-save",
        }:
            failures.append("continuous topology command inventory is not exact")
        else:
            for command, path_value in command_paths.items():
                path = Path(path_value) if isinstance(path_value, str) else Path("/")
                if (
                    path.name != command
                    or not path.is_file()
                    or not os.access(path, os.X_OK)
                ):
                    failures.append(
                        f"continuous topology command path is invalid: {command}"
                    )
        ready_keys = {
            "contract",
            "run_id",
            "runtime_id",
            "run_nonce",
            "pid",
            "start_ticks",
            "interval_ms",
            "command_paths",
            "ready_monotonic_ns",
        }
        if (
            set(ready) != ready_keys
            or (
                ready.get("contract"),
                ready.get("run_id"),
                ready.get("runtime_id"),
                ready.get("run_nonce"),
            )
            != (TOPOLOGY_SUMMARY_CONTRACT, run_id, runtime_id, run_nonce)
            or ready.get("interval_ms") != 500
            or ready.get("command_paths") != command_paths
            or not isinstance(ready.get("pid"), int)
            or not isinstance(ready.get("start_ticks"), int)
        ):
            failures.append("continuous topology ready identity is invalid")

        transition_by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for sample in transition_samples:
            transition_by_name[str(sample.get("transition_event"))].append(sample)
        process_transition_events = {
            "captures": (
                "captures_start_requested",
                "captures_started",
                "captures_stop_requested",
                "captures_stopped",
            ),
            "forbidden_listeners": (
                "forbidden_listeners_start_requested",
                "forbidden_listeners_started",
                "forbidden_listeners_stop_requested",
                "forbidden_listeners_stopped",
            ),
            "endpoint_agents": (
                "endpoint_agents_start_requested",
                "endpoint_agents_started",
                "endpoint_agents_stop_requested",
                "endpoint_agents_stopped",
            ),
            "actual_control": (
                "actual_control_start_requested",
                "actual_control_started",
                "actual_control_stop_requested",
                "actual_control_stopped",
            ),
        }
        required_boundaries = {
            "topology_ready",
            "actual_sitl_stack_stop_requested",
            *(event for events in process_transition_events.values() for event in events),
        }
        missing_boundaries = sorted(
            event
            for event in required_boundaries
            if len(transition_by_name.get(event, [])) != 1
        )
        if missing_boundaries:
            raise ValidationError(
                "continuous topology acceptance boundaries are absent/nonunique: "
                + str(missing_boundaries)
            )
        transition_bounds: dict[str, tuple[int, int, int, int]] = {}
        for role, events in process_transition_events.items():
            bounds = tuple(
                int(transition_by_name[event][0]["monotonic_ns"])
                for event in events
            )
            if (
                list(bounds) != sorted(bounds)
                or len(set(bounds)) != len(bounds)
                or bounds[1] - bounds[0] > PROCESS_TRANSITION_MAX_NS
                or bounds[3] - bounds[2] > PROCESS_TRANSITION_MAX_NS
            ):
                raise ValidationError(
                    f"continuous {role} transition interval is invalid/unbounded"
                )
            transition_bounds[role] = bounds

        def expected_process_state(role: str, sample_time: int) -> bool | None:
            start_requested, started, stop_requested, stopped = transition_bounds[role]
            if started <= sample_time < stop_requested:
                return True
            if sample_time < start_requested or sample_time >= stopped:
                return False
            return None

        acceptance_start = transition_by_name["topology_ready"][0]["monotonic_ns"]
        acceptance_end = transition_by_name["captures_stopped"][0]["monotonic_ns"]
        acceptance_samples = [
            sample
            for sample in samples
            if acceptance_start <= sample["monotonic_ns"] <= acceptance_end
        ]
        if not acceptance_samples:
            raise ValidationError(
                "continuous topology acceptance interval has no samples"
            )
        inodes_by_namespace: dict[str, set[int]] = defaultdict(set)
        netlink_identities: dict[str, set[tuple[int, int]]] = defaultdict(set)
        process_identities: dict[str, set[tuple[int, int]]] = defaultdict(set)
        listener_bindings: dict[str, list[tuple[str, int]]] = defaultdict(list)
        for canary in expected_forbidden_canaries(str(run_nonce)):
            listener_bindings[canary["listener_namespace"]].append(
                (canary["destination_ip"], canary["destination_udp_port"])
            )
        listener_ready_identities: dict[str, tuple[int, int]] = {}
        for endpoint, namespace in NAMESPACES.items():
            listener_ready = strict_json(
                run_dir / f"raw/state/forbidden-listener-{endpoint}.ready.json"
            )
            listener_ready_identities[namespace] = (
                int(listener_ready["pid"]),
                int(listener_ready["start_ticks"]),
            )
        for sample in acceptance_samples:
            namespaces = sample.get("namespaces")
            if not isinstance(namespaces, dict) or set(namespaces) != set(
                MONITORED_NAMESPACES
            ):
                failures.append("continuous topology namespace set is not exact")
                continue
            for namespace in MONITORED_NAMESPACES:
                record = namespaces[namespace]
                failures.extend(
                    _continuous_namespace_failures(
                        namespace,
                        record,
                        f"sample {sample['sample_sequence']}/{namespace}",
                    )
                )
                if isinstance(record, dict) and isinstance(
                    record.get("namespace_inode"), int
                ):
                    inodes_by_namespace[namespace].add(record["namespace_inode"])
            monitors = sample.get("netlink_monitors")
            if not isinstance(monitors, dict) or set(monitors) != set(
                MONITORED_NAMESPACES
            ):
                failures.append("continuous netlink monitor set is not exact/alive")
            else:
                for namespace, monitor in monitors.items():
                    if (
                        not isinstance(monitor, dict)
                        or set(monitor) != {"pid", "start_ticks", "alive"}
                        or monitor.get("alive") is not True
                        or not isinstance(monitor.get("pid"), int)
                        or not isinstance(monitor.get("start_ticks"), int)
                    ):
                        failures.append(
                            f"continuous netlink monitor identity invalid: {namespace}"
                        )
                    else:
                        netlink_identities[namespace].add(
                            (monitor["pid"], monitor["start_ticks"])
                        )
            processes = sample.get("processes")
            if not isinstance(processes, list):
                failures.append("continuous process inventory is not a list")
                continue
            by_namespace: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for process in processes:
                if not isinstance(process, dict) or set(process) != {
                    "pid",
                    "start_ticks",
                    "namespace",
                    "namespace_inode",
                    "executable",
                    "executable_sha256",
                    "cmdline",
                    "cap_eff",
                    "cgroup",
                }:
                    failures.append("continuous process record keys are not exact")
                    continue
                if (
                    not isinstance(process.get("pid"), int)
                    or not isinstance(process.get("start_ticks"), int)
                    or not isinstance(process.get("cmdline"), list)
                    or not isinstance(process.get("executable_sha256"), str)
                    or HEX64.fullmatch(process["executable_sha256"]) is None
                ):
                    failures.append("continuous process identity is malformed")
                    continue
                by_namespace[str(process.get("namespace"))].append(process)
            for namespace in ("ams-ns3", *NAMESPACES.values()):
                allowed: list[dict[str, Any]] = []
                for process in by_namespace[namespace]:
                    command = " ".join(str(item) for item in process["cmdline"])
                    role = None
                    if " -ts monitor all" in command:
                        role = f"netlink:{namespace}"
                    elif "m3_external_matrix_probe.py agent" in command:
                        role = f"agent:{namespace}"
                    elif "m3_external_matrix_probe.py forbidden-listener" in command:
                        role = f"forbidden_listener:{namespace}"
                    elif "m3_external_matrix_probe.py forbidden-canaries" in command:
                        role = f"forbidden_sender:{namespace}"
                    elif "actual_sitl_mavlink_endpoint.py" in command:
                        role = f"actual_adapter:{namespace}"
                    elif "actual_sitl_control_probe.py" in command:
                        role = f"actual_control:{namespace}"
                    elif "raw_packet_capture.py" in command:
                        interface = next(
                            (
                                str(process["cmdline"][index + 1])
                                for index, item in enumerate(process["cmdline"][:-1])
                                if item == "--interface"
                            ),
                            "unknown",
                        )
                        role = f"capture:{namespace}:{interface}"
                    elif "ams-tap-packet-engine" in command:
                        role = "engine:ams-ns3"
                    if role is None:
                        failures.append(
                            f"sample {sample['sample_sequence']}/{namespace} has undeclared process {command!r}"
                        )
                    else:
                        process_identities[role].add(
                            (process["pid"], process["start_ticks"])
                        )
                        allowed.append(process)
                commands = [
                    " ".join(str(item) for item in process["cmdline"])
                    for process in allowed
                ]
                netlink_count = sum(
                    " -ts monitor all" in command for command in commands
                )
                if netlink_count != 1:
                    failures.append(
                        f"sample {sample['sample_sequence']}/{namespace} netlink process count is {netlink_count}"
                    )
                agent_count = sum(
                    "m3_external_matrix_probe.py agent" in command
                    for command in commands
                )
                agent_state = expected_process_state(
                    "endpoint_agents", int(sample["monotonic_ns"])
                )
                expected_agents = 1 if namespace != "ams-ns3" else 0
                if (
                    agent_state is True and agent_count != expected_agents
                ) or (
                    agent_state is False and agent_count != 0
                ) or (
                    agent_state is None and not 0 <= agent_count <= expected_agents
                ):
                    expected_label = expected_agents if agent_state is True else 0
                    failures.append(
                        f"sample {sample['sample_sequence']}/{namespace} agent count is {agent_count}, expected {expected_label}"
                    )
                capture_count = sum(
                    "raw_packet_capture.py" in command for command in commands
                )
                capture_state = expected_process_state(
                    "captures", int(sample["monotonic_ns"])
                )
                if namespace == "ams-ns3":
                    active_capture_count = 6
                elif namespace == "ams-gcs":
                    active_capture_count = 2
                else:
                    active_capture_count = 3
                if (
                    capture_state is True and capture_count != active_capture_count
                ) or (
                    capture_state is False and capture_count != 0
                ) or (
                    capture_state is None
                    and not 0 <= capture_count <= active_capture_count
                ):
                    expected_captures = (
                        active_capture_count if capture_state is True else 0
                    )
                    failures.append(
                        f"sample {sample['sample_sequence']}/{namespace} capture count is {capture_count}, expected {expected_captures}"
                    )
                listener_count = sum(
                    "m3_external_matrix_probe.py forbidden-listener" in command
                    for command in commands
                )
                listener_state = expected_process_state(
                    "forbidden_listeners", int(sample["monotonic_ns"])
                )
                active_listener_count = 1 if namespace != "ams-ns3" else 0
                if (
                    listener_state is True
                    and listener_count != active_listener_count
                ) or (
                    listener_state is False and listener_count != 0
                ) or (
                    listener_state is None
                    and not 0 <= listener_count <= active_listener_count
                ):
                    expected_listeners = (
                        active_listener_count if listener_state is True else 0
                    )
                    failures.append(
                        f"sample {sample['sample_sequence']}/{namespace} forbidden listener count is {listener_count}, expected {expected_listeners}"
                    )
                if listener_state is True and active_listener_count == 1:
                    socket_lines = namespaces[namespace]["sockets"]
                    for ip, port in listener_bindings[namespace]:
                        ip_tokens = (f"[{ip}]:", f"{ip}:") if ":" in ip else (f"{ip}:",)
                        if not any(
                            any(token in line for token in ip_tokens)
                            and f":{port}" in line
                            for line in socket_lines
                        ):
                            failures.append(
                                f"sample {sample['sample_sequence']}/{namespace} listener socket is absent: {ip}:{port}"
                            )
            root_capture_processes = [
                process
                for process in by_namespace["container-root"]
                if "raw_packet_capture.py"
                in " ".join(str(item) for item in process.get("cmdline", []))
            ]
            root_capture_state = expected_process_state(
                "captures", int(sample["monotonic_ns"])
            )
            if (
                root_capture_state is True and len(root_capture_processes) != 6
            ) or (
                root_capture_state is False and root_capture_processes
            ) or (
                root_capture_state is None
                and not 0 <= len(root_capture_processes) <= 6
            ):
                expected_root_captures = 6 if root_capture_state is True else 0
                failures.append(
                    f"sample {sample['sample_sequence']}/container-root capture count is {len(root_capture_processes)}, expected {expected_root_captures}"
                )
            for process in root_capture_processes:
                command = [str(item) for item in process.get("cmdline", [])]
                interface = next(
                    (
                        command[index + 1]
                        for index, token in enumerate(command[:-1])
                        if token == "--interface"
                    ),
                    "unknown",
                )
                process_identities[f"capture:container-root:{interface}"].add(
                    (process["pid"], process["start_ticks"])
                )
            engine_count = sum(
                "ams-tap-packet-engine"
                in " ".join(str(item) for item in process.get("cmdline", []))
                for process in by_namespace["ams-ns3"]
            )
            sample_time = sample["monotonic_ns"]
            if (
                "stopped" in windows
                and windows["stopped"]["start_monotonic_ns"]
                <= sample_time
                < windows["stopped"]["end_monotonic_ns"]
                and engine_count != 0
            ):
                failures.append("packet engine exists inside monitored stopped window")
            if (
                any(
                    phase in windows
                    and windows[phase]["start_monotonic_ns"]
                    <= sample_time
                    < windows[phase]["end_monotonic_ns"]
                    for phase in ("positive", "p2mp", "recovery")
                )
                and engine_count != 1
            ):
                failures.append(
                    f"sample {sample['sample_sequence']} positive engine count is {engine_count}"
                )
        if any(len(values) != 1 for values in inodes_by_namespace.values()) or set(
            inodes_by_namespace
        ) != set(MONITORED_NAMESPACES):
            failures.append(
                "namespace inode changed/missing during continuous acceptance"
            )
        if any(len(values) != 1 for values in netlink_identities.values()) or set(
            netlink_identities
        ) != set(MONITORED_NAMESPACES):
            failures.append("netlink monitor PID/start_ticks changed during acceptance")
        for namespace in NAMESPACES.values():
            if len(process_identities[f"agent:{namespace}"]) != 1:
                failures.append(f"endpoint agent PID/start_ticks changed: {namespace}")
        for namespace in NAMESPACES.values():
            if len(process_identities[f"capture:{namespace}:eth0"]) != 1:
                failures.append(
                    f"endpoint capture PID/start_ticks changed: {namespace}"
                )
            if len(process_identities[f"capture:{namespace}:lo"]) != 1:
                failures.append(
                    f"loopback capture PID/start_ticks changed: {namespace}"
                )
            if namespace != "ams-gcs" and len(
                process_identities[f"capture:{namespace}:tail0"]
            ) != 1:
                failures.append(
                    f"actual tail capture PID/start_ticks changed: {namespace}"
                )
            if namespace != "ams-gcs" and len(
                process_identities[f"actual_adapter:{namespace}"]
            ) != 1:
                failures.append(
                    f"actual adapter PID/start_ticks changed: {namespace}"
                )
            if process_identities[f"forbidden_listener:{namespace}"] != {
                listener_ready_identities[namespace]
            }:
                failures.append(
                    f"forbidden listener PID/start_ticks differs from ready evidence: {namespace}"
                )
        if len(process_identities["actual_control:ams-gcs"]) != 1:
            failures.append("actual control PID/start_ticks changed: ams-gcs")
        for endpoint in ENDPOINTS:
            if len(process_identities[f"capture:ams-ns3:vp-{endpoint}"]) != 1:
                failures.append(
                    f"ns3 external capture PID/start_ticks changed: {endpoint}"
                )
        if len(process_identities["capture:container-root:lo"]) != 1:
            failures.append("root loopback capture PID/start_ticks changed")
        for index in range(1, 6):
            if len(
                process_identities[f"capture:container-root:ams-tail{index}"]
            ) != 1:
                failures.append(
                    f"root actual tail capture PID/start_ticks changed: uav{index}"
                )

        monitor_summary = summary.get("netlink_monitors")
        if not isinstance(monitor_summary, dict) or set(monitor_summary) != set(
            MONITORED_NAMESPACES
        ):
            failures.append("netlink monitor final summary set is not exact")
        else:
            for namespace, monitor in monitor_summary.items():
                if not isinstance(monitor, dict) or set(monitor) != {
                    "pid",
                    "returncode",
                    "stdout_path",
                    "stdout_bytes",
                    "stderr_path",
                    "stderr_bytes",
                }:
                    failures.append(f"netlink final summary malformed: {namespace}")
                    continue
                stdout_path = run_dir / str(monitor["stdout_path"])
                stderr_path = run_dir / str(monitor["stderr_path"])
                if (
                    monitor.get("returncode") not in {-15, -2, 0, 130, 143}
                    or not regular_file(stdout_path)
                    or not regular_file(stderr_path)
                    or stdout_path.stat().st_size != monitor.get("stdout_bytes")
                    or stderr_path.stat().st_size != monitor.get("stderr_bytes")
                    or monitor.get("stderr_bytes") != 0
                ):
                    failures.append(f"netlink final evidence invalid: {namespace}")
        for path in (
            run_dir / "logs/topology-monitor.stdout",
            run_dir / "logs/topology-monitor.stderr",
        ):
            if not regular_file(path) or path.stat().st_size != 0:
                failures.append(
                    f"topology monitor stdio is absent/nonempty: {path.name}"
                )
        details = {
            "sample_count": len(samples),
            "acceptance_sample_count": len(acceptance_samples),
            "maximum_sample_gap_ns": recomputed_gap,
            "namespace_inodes": {
                name: sorted(values)
                for name, values in sorted(inodes_by_namespace.items())
            },
            "process_identities": {
                role: [list(value) for value in sorted(values)]
                for role, values in sorted(process_identities.items())
            },
        }
    except (KeyError, OSError, TypeError, ValueError, ValidationError) as exc:
        failures.append(str(exc))
    return failures, details


def validate_actual_sitl_control(
    run_dir: Path,
    *,
    run: dict[str, Any],
    matrix: dict[str, Any],
    windows: dict[str, Any],
) -> tuple[
    list[str],
    dict[str, Any],
    dict[tuple[str, str], list[dict[str, Any]]],
    dict[tuple[str, str], list[dict[str, Any]]],
]:
    """Validate and normalize the real five-SITL control slice.

    Companion endpoint records are deliberately not accepted for control.  The
    returned records are derived only from request bytes, actual adapter audits,
    and real vehicle replies and can therefore feed the common 30-cell metric,
    ns-3, and PCAP correlation gates.
    """

    failures: list[str] = []
    details: dict[str, Any] = {}
    offered: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    received: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    run_id, runtime_id, run_nonce = (
        run.get("run_id"),
        run.get("runtime_id"),
        run.get("run_nonce"),
    )

    try:
        flight = run.get("flight_runtime")
        expected_roles = {
            "gcs": "gcs_control_probe",
            "adapters": {
                f"uav{index}": f"uav_control_adapter_uav{index}"
                for index in range(1, 6)
            },
            "supervisor": "actual_endpoint_supervisor",
        }
        expected_flight_keys = {
            "contract",
            "robot_count",
            "mavproxy_out",
            "payload_sha256",
            "control_endpoint_form",
            "control_process_role_ids",
            "source_path",
            "source_sha256",
            "resolved_path",
        }
        if not isinstance(flight, dict) or set(flight) != expected_flight_keys:
            raise ValidationError("resolved flight runtime binding keys are not exact")
        expected_out = {
            f"uav{index}": f"10.72.{index}.2:{14559 + index}"
            for index in range(1, 6)
        }
        if (
            flight.get("contract") != RESOLVED_FLIGHT_CONTRACT
            or flight.get("robot_count") != 5
            or flight.get("mavproxy_out") != expected_out
            or flight.get("control_endpoint_form") != ACTUAL_CONTROL_ENDPOINT_FORM
            or flight.get("control_process_role_ids") != expected_roles
            or flight.get("source_path") != "network/config/scenario_5uav.yaml"
            or flight.get("resolved_path") != "raw/resolved_flight_scenario.yaml"
        ):
            raise ValidationError("resolved five-UAV flight runtime identity differs")
        source_path = ROOT / str(flight["source_path"])
        resolved_path = run_dir / str(flight["resolved_path"])
        if (
            not regular_file(source_path)
            or sha256_file(source_path) != flight.get("source_sha256")
            or not regular_file(resolved_path)
            or sha256_file(resolved_path) != flight.get("payload_sha256")
        ):
            raise ValidationError("resolved flight source/payload hash differs")
        try:
            import yaml

            source_document = yaml.safe_load(source_path.read_text(encoding="utf-8"))
        except (ImportError, OSError, UnicodeError, ValueError) as exc:
            raise ValidationError(f"cannot independently resolve flight scenario: {exc}") from exc
        if not isinstance(source_document, dict) or not isinstance(
            source_document.get("robots"), list
        ):
            raise ValidationError("flight source has no exact robots list")
        expected_document = json.loads(json.dumps(source_document))
        robots = expected_document["robots"]
        if len(robots) != 5:
            raise ValidationError("flight source robot count is not five")
        for index, robot in enumerate(robots, start=1):
            if not isinstance(robot, dict) or any(
                robot.get(key) != value
                for key, value in {
                    "name": f"uav{index}",
                    "instance": index - 1,
                    "system_id": index,
                }.items()
            ):
                raise ValidationError(f"flight source uav{index} identity differs")
            robot["mavproxy_out"] = expected_out[f"uav{index}"]
        expected_payload = (
            json.dumps(
                expected_document,
                allow_nan=False,
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode()
        if resolved_path.read_bytes() != expected_payload:
            raise ValidationError("resolved flight scenario is not byte-exact derivation")
        details["flight_runtime"] = {
            "control_endpoint_form": ACTUAL_CONTROL_ENDPOINT_FORM,
            "process_role_ids": expected_roles,
            "mavproxy_out": expected_out,
        }
    except (KeyError, TypeError, ValidationError) as exc:
        failures.append(str(exc))

    adapter_audits: dict[str, list[dict[str, Any]]] = {}
    adapter_forward: dict[str, dict[str, list[dict[str, Any]]]] = {}
    manifest: dict[str, Any] = {}
    manifest_hash = "unavailable"
    ready_documents: dict[str, dict[str, Any]] = {}
    authorization_documents: dict[str, dict[str, Any]] = {}
    aggregate: dict[str, Any] = {}
    try:
        from network.bridge.actual_sitl_mavlink_endpoint import (
            AUTHORIZATION_CONTRACT,
            CANDIDATE_CONTRACT,
            EXPECTED_PROCESS_KEYS,
            FAILURE_CONTRACT,
            MANIFEST_CONTRACT,
            READY_CONTRACT,
            EndpointError,
            _validate_adapter_candidate_base,
            validate_authorization,
            validate_manifest,
        )
        from network.scripts.actual_sitl_endpoint_orchestrator import (
            AGGREGATE_READY_CONTRACT,
            SUPERVISOR_FAILURE_CONTRACT,
        )

        manifest_path = run_dir / "raw/actual_sitl_endpoint_manifest.json"
        manifest = strict_json(manifest_path)
        if manifest_path.read_bytes() != canonical_json(manifest):
            raise ValidationError("actual-SITL manifest bytes are not canonical")
        try:
            validate_manifest(manifest)
        except EndpointError as exc:
            raise ValidationError(f"actual-SITL manifest is invalid: {exc}") from exc
        manifest_hash = sha256_bytes(canonical_json(manifest))
        if (
            manifest.get("contract") != MANIFEST_CONTRACT
            or (manifest.get("run_id"), manifest.get("runtime_id"), manifest.get("run_nonce"))
            != (run_id, runtime_id, run_nonce)
            or manifest.get("adapter_source_sha256")
            != sha256_file(ROOT / "network/bridge/actual_sitl_mavlink_endpoint.py")
            or manifest.get("relay_core_source_sha256")
            != sha256_file(ROOT / "network/bridge/opaque_udp_relay.py")
        ):
            raise ValidationError("actual-SITL manifest source/run identity differs")
        channels = manifest["channels"]
        launch_pgids = {int(channel["launch_pgid"]) for channel in channels}
        if len(launch_pgids) != 1:
            raise ValidationError("actual five-SITL processes do not share one launch PGID")
        process_pids: set[int] = set()
        aggregate_channels: dict[str, Any] = {}
        supervisor_identities: list[dict[str, Any]] = []
        for index, channel in enumerate(channels, start=1):
            uav = f"uav{index}"
            if channel.get("uav") != uav:
                raise ValidationError("actual-SITL channel order/name differs")
            for role in ("mavproxy", "sitl"):
                identity = channel[role]
                if not isinstance(identity, dict) or set(identity) != EXPECTED_PROCESS_KEYS:
                    raise ValidationError(f"{uav} {role} frozen identity is incomplete")
                pid = int(identity["pid"])
                if pid in process_pids:
                    raise ValidationError("actual-SITL process PID reused across roles")
                process_pids.add(pid)
            base = run_dir / "raw/actual_sitl"
            candidate = strict_json(base / f"{uav}.peer-candidate.json")
            authorization = strict_json(base / f"{uav}.authorization.json")
            ready = strict_json(base / f"{uav}.ready.json")
            if (base / f"{uav}.failure.json").exists():
                failure = strict_json(base / f"{uav}.failure.json")
                if failure.get("contract") == FAILURE_CONTRACT:
                    raise ValidationError(f"{uav} adapter published failure evidence")
                raise ValidationError(f"{uav} has an unknown failure artifact")
            try:
                _validate_adapter_candidate_base(candidate, manifest, manifest_hash, channel)
                validate_authorization(
                    authorization,
                    manifest,
                    manifest_hash,
                    channel,
                    candidate,
                    sha256_bytes(canonical_json(candidate)),
                    require_live_issuer=False,
                )
            except EndpointError as exc:
                raise ValidationError(f"{uav} candidate/authorization invalid: {exc}") from exc
            if (
                candidate.get("contract") != CANDIDATE_CONTRACT
                or authorization.get("contract") != AUTHORIZATION_CONTRACT
                or ready.get("contract") != READY_CONTRACT
                or ready.get("status") != "ready"
                or ready.get("manifest_sha256") != manifest_hash
                or ready.get("candidate_sha256") != sha256_bytes(canonical_json(candidate))
                or ready.get("authorization_sha256")
                != sha256_bytes(canonical_json(authorization))
                or ready.get("uav") != uav
                or ready.get("system_id") != index
                or ready.get("adapter") != candidate.get("adapter")
                or ready.get("radio_socket") != candidate.get("radio_socket")
                or ready.get("tail_socket") != candidate.get("tail_socket")
                or ready.get("mavproxy_peer") != candidate.get("mavproxy_peer")
            ):
                raise ValidationError(f"{uav} ready receipt does not bind candidate/authorization")
            for role in ("mavproxy", "sitl"):
                frozen = channel[role]
                for lineage_document in (candidate.get("lineage"), ready.get("lineage")):
                    observed = (
                        lineage_document.get(role)
                        if isinstance(lineage_document, dict)
                        else None
                    )
                    if not isinstance(observed, dict) or any(
                        observed.get(key) != frozen[key] for key in EXPECTED_PROCESS_KEYS
                    ):
                        raise ValidationError(f"{uav} {role} lineage differs from manifest")
            supervisor_identities.append(authorization["issuer"])
            audit = strict_hash_chain_audit(
                run_dir / f"logs/actual_sitl_{uav}.jsonl",
                run_id=run_id,
                runtime_id=runtime_id,
                run_nonce=run_nonce,
                uav=uav,
            )
            events = [record.get("event") for record in audit]
            for required_event in (
                "adapter_bound_not_ready",
                "peer_candidate_published_not_ready",
                "adapter_ready",
                "adapter_stop",
            ):
                if events.count(required_event) != 1:
                    raise ValidationError(f"{uav} audit {required_event} cardinality differs")
            if "adapter_failed_closed" in events:
                raise ValidationError(f"{uav} adapter audit failed closed")
            if events[-1] != "adapter_stop":
                raise ValidationError(f"{uav} adapter audit has no clean terminal stop")
            forwards = {
                direction: [
                    record
                    for record in audit
                    if record.get("event") == "forward"
                    and record.get("direction") == direction
                ]
                for direction in ("gcs_to_tail", "tail_to_gcs")
            }
            if not forwards["gcs_to_tail"] or not forwards["tail_to_gcs"]:
                raise ValidationError(f"{uav} audit lacks bidirectional real forwarding")
            adapter_audits[uav] = audit
            adapter_forward[uav] = forwards
            authorization_documents[uav] = authorization
            ready_documents[uav] = ready
            aggregate_channels[uav] = {
                "system_id": index,
                "candidate_sha256": sha256_bytes(canonical_json(candidate)),
                "authorization_sha256": sha256_bytes(canonical_json(authorization)),
                "ready_sha256": sha256_bytes(canonical_json(ready)),
                "radio_socket": ready["radio_socket"],
                "tail_socket": ready["tail_socket"],
                "mavproxy_peer": ready["mavproxy_peer"],
                "tail_pcap_roles": channel["tail_pcap_roles"],
            }
        if any(identity != supervisor_identities[0] for identity in supervisor_identities[1:]):
            raise ValidationError("five authorizations were not issued by one supervisor")
        if not any(
            Path(str(token)).name == "actual_sitl_endpoint_orchestrator.py"
            for token in supervisor_identities[0].get("argv", [])
        ):
            raise ValidationError("authorization issuer is not the endpoint supervisor")
        aggregate = strict_json(run_dir / "raw/state/actual-sitl-endpoints.ready.json")
        if (
            set(aggregate)
            != {
                "schema_version",
                "contract",
                "status",
                "run_id",
                "runtime_id",
                "run_nonce",
                "manifest_sha256",
                "ready_wall_utc",
                "ready_monotonic_ns",
                "supervisor",
                "channels",
            }
            or aggregate.get("schema_version") != 1
            or aggregate.get("contract") != AGGREGATE_READY_CONTRACT
            or aggregate.get("status") != "ready"
            or (aggregate.get("run_id"), aggregate.get("runtime_id"), aggregate.get("run_nonce"))
            != (run_id, runtime_id, run_nonce)
            or aggregate.get("manifest_sha256") != manifest_hash
            or aggregate.get("channels") != aggregate_channels
            or aggregate.get("supervisor") != supervisor_identities[0]
        ):
            raise ValidationError("aggregate actual-SITL readiness differs")
        supervisor_failure = run_dir / "raw/actual_sitl/endpoint-supervisor.failure.json"
        if supervisor_failure.exists():
            failure = strict_json(supervisor_failure)
            if failure.get("contract") == SUPERVISOR_FAILURE_CONTRACT:
                raise ValidationError("actual-SITL supervisor published failure evidence")
            raise ValidationError("unknown actual-SITL supervisor failure artifact")
        supervisor_audit = strict_hash_chain_audit(
            run_dir / "logs/actual_sitl_supervisor.jsonl",
            run_id=run_id,
            runtime_id=runtime_id,
            run_nonce=run_nonce,
            uav="all",
        )
        supervisor_events = [record.get("event") for record in supervisor_audit]
        expected_counts = {
            "supervisor_start_not_ready": 1,
            "endpoint_authorized_not_aggregate_ready": 5,
            "endpoint_ready_not_aggregate_ready": 5,
            "aggregate_ready": 1,
            "supervisor_stop": 1,
        }
        if any(supervisor_events.count(event) != count for event, count in expected_counts.items()):
            raise ValidationError("actual-SITL supervisor lifecycle cardinality differs")
        if supervisor_events[-1] != "supervisor_stop" or "supervisor_failed_closed" in supervisor_events:
            raise ValidationError("actual-SITL supervisor did not stop cleanly")
        issuer = supervisor_identities[0]
        start_event = next(
            record
            for record in supervisor_audit
            if record.get("event") == "supervisor_start_not_ready"
        )
        if (
            start_event.get("pid") != issuer["pid"]
            or start_event.get("manifest_sha256") != manifest_hash
        ):
            raise ValidationError("authorization issuer is not bound to supervisor start")
        for uav in (f"uav{index}" for index in range(1, 6)):
            authorization_events = [
                record
                for record in supervisor_audit
                if record.get("event") == "endpoint_authorized_not_aggregate_ready"
                and record.get("endpoint_uav") == uav
            ]
            ready_events = [
                record
                for record in supervisor_audit
                if record.get("event") == "endpoint_ready_not_aggregate_ready"
                and record.get("endpoint_uav") == uav
            ]
            if len(authorization_events) != 1 or len(ready_events) != 1:
                raise ValidationError(f"{uav} supervisor authorization/readiness audit differs")
            authorization_event = authorization_events[0]
            ready_event = ready_events[0]
            channel_evidence = aggregate_channels[uav]
            if (
                authorization_event.get("candidate_sha256")
                != channel_evidence["candidate_sha256"]
                or authorization_event.get("authorization_sha256")
                != channel_evidence["authorization_sha256"]
                or authorization_event.get("mavproxy_peer")
                != channel_evidence["mavproxy_peer"]
                or int(authorization_event["monotonic_ns"])
                < int(authorization_documents[uav]["authorized_monotonic_ns"])
                or ready_event.get("ready_sha256") != channel_evidence["ready_sha256"]
                or int(ready_event["monotonic_ns"])
                < int(ready_documents[uav]["ready_monotonic_ns"])
            ):
                raise ValidationError(f"{uav} supervisor hash authorization binding differs")
        aggregate_event = next(
            record
            for record in supervisor_audit
            if record.get("event") == "aggregate_ready"
        )
        if (
            aggregate_event.get("ready_path")
            != "raw/state/actual-sitl-endpoints.ready.json"
            or aggregate_event.get("ready_sha256")
            != sha256_bytes(canonical_json(aggregate))
            or int(aggregate_event["monotonic_ns"])
            < int(aggregate["ready_monotonic_ns"])
        ):
            raise ValidationError("aggregate readiness is not hash-bound to supervisor audit")
        samples = [
            record for record in supervisor_audit if record.get("event") == "lineage_sample_pass"
        ]
        if not samples or any(
            set(record.get("channel_lineage_sha256", {}))
            != {f"uav{index}" for index in range(1, 6)}
            for record in samples
        ):
            raise ValidationError("continuous five-channel lineage samples are absent")
        sample_times = [int(record["monotonic_ns"]) for record in samples]
        if max(
            (right - left for left, right in zip(sample_times, sample_times[1:])),
            default=0,
        ) > 1_500_000_000:
            raise ValidationError("actual-SITL lineage sampling gap exceeds 1.5s")
        for phase in ("positive", "stopped", "recovery"):
            if phase in windows and not any(
                windows[phase]["start_monotonic_ns"] <= timestamp < windows[phase]["end_monotonic_ns"]
                for timestamp in sample_times
            ):
                raise ValidationError(f"actual-SITL lineage has no {phase} sample")
        details["actual_sitl"] = {
            "manifest_sha256": manifest_hash,
            "channel_count": 5,
            "launch_pgid": next(iter(launch_pgids)),
            "lineage_sample_count": len(samples),
            "authorization_issuer_sha256": sha256_bytes(canonical_json(issuer)),
            "aggregate_ready_sha256": sha256_bytes(canonical_json(aggregate)),
            "relay_core_source_sha256": manifest["relay_core_source_sha256"],
        }
    except (
        KeyError,
        OSError,
        TypeError,
        ValueError,
        ValidationError,
    ) as exc:
        failures.append(str(exc))

    try:
        events = strict_control_event_audit(
            run_dir / "raw/actual_control/events.jsonl",
            run_id=run_id,
            runtime_id=runtime_id,
            run_nonce=run_nonce,
        )
        forbidden_events = {
            "control_parse_error",
            "foreign_control_message",
            "uncorrelated_control_response",
            "late_stopped_control_response",
            "forbidden_stopped_control_response",
            "phase_ended_before_outcome_timeout",
        }
        observed_forbidden = sorted(
            {str(record.get("event")) for record in events} & forbidden_events
        )
        if observed_forbidden:
            raise ValidationError(f"actual control audit contains forbidden events: {observed_forbidden}")
        socket_events = [record for record in events if record.get("event") == "actual_control_socket_ready"]
        link_events = [record for record in events if record.get("event") == "actual_control_link_ready"]
        shutdown_events = [record for record in events if record.get("event") == "actual_control_shutdown"]
        if len(socket_events) != 1 or len(link_events) != 1 or len(shutdown_events) != 1:
            raise ValidationError("actual control ready/link/shutdown cardinality differs")
        socket_ready = strict_json(run_dir / "raw/state/actual-control.socket-ready.json")
        link_ready = strict_json(run_dir / "raw/state/actual-control.link-ready.json")
        expected_ready_identity = {
            "run_id": run_id,
            "runtime_id": runtime_id,
            "run_nonce": run_nonce,
            "profile": "m3",
            "transport_nonce32": run_nonce,
            "transport_nonce_derivation": "identity/full_run_nonce32",
            "role_subject": "gcs_control_probe",
        }
        if (
            socket_ready.get("contract") != "ams.actual-sitl.control-socket-ready/v1"
            or any(socket_ready.get(key) != value for key, value in expected_ready_identity.items())
            or socket_ready.get("bound_socket") != ["10.71.0.10", 14600]
            or socket_ready.get("pid") != socket_events[0].get("pid")
            or socket_ready.get("start_ticks") != socket_events[0].get("process_start_ticks")
            or link_ready.get("contract") != "ams.actual-sitl.control-link-ready/v1"
            or any(link_ready.get(key) != value for key, value in expected_ready_identity.items())
            or link_ready.get("pid") != socket_ready.get("pid")
            or any(
                not isinstance(link_ready.get("heartbeat_counts", {}).get(f"uav{index}"), int)
                or link_ready["heartbeat_counts"][f"uav{index}"] < 3
                for index in range(1, 6)
            )
        ):
            raise ValidationError("actual control sole socket/link readiness differs")

        command_hashes: dict[str, str] = {}
        for command_index, phase in enumerate(("positive", "stopped", "recovery"), start=1):
            window = windows[phase]
            path = run_dir / f"raw/control/actual-control/{command_index:03d}-{phase}.json"
            command = strict_json(path)
            if command != {
                "action": "phase",
                "endpoint": "actual-control",
                "run_id": run_id,
                "runtime_id": runtime_id,
                "run_nonce": run_nonce,
                **window,
            }:
                raise ValidationError(f"actual control {phase} command differs from phase contract")
            command_hashes[phase] = sha256_file(path)
        shutdown_path = run_dir / "raw/control/actual-control/999-shutdown.json"
        if strict_json(shutdown_path) != {
            "action": "shutdown",
            "endpoint": "actual-control",
            "run_id": run_id,
            "runtime_id": runtime_id,
            "run_nonce": run_nonce,
            "not_before_monotonic_ns": windows["recovery"]["end_monotonic_ns"] + 500_000_000,
        }:
            raise ValidationError("actual control shutdown command differs")

        datagrams_by_hash: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for record in events:
            if record.get("event") != "control_datagram_receive":
                continue
            try:
                payload = bytes.fromhex(str(record["transport_payload_hex"]))
            except ValueError as exc:
                raise ValidationError("actual control datagram hex is invalid") from exc
            if (
                sha256_bytes(payload) != record.get("transport_payload_sha256")
                or len(payload) != record.get("transport_payload_size")
                or record.get("peer_ip") not in {f"10.71.{index}.10" for index in range(1, 6)}
            ):
                raise ValidationError("actual control datagram byte/source identity differs")
            datagrams_by_hash[str(record["transport_payload_sha256"])].append(record)

        offers = [record for record in events if record.get("event") == "real_command_offered"]
        results = [record for record in events if record.get("event") == "transaction_result"]
        result_by_identity: dict[tuple[str, int, int], dict[str, Any]] = {}
        for result in results:
            identity = (str(result.get("phase")), int(result.get("uav", 0)), int(result.get("sequence", 0)))
            if identity in result_by_identity:
                raise ValidationError(f"duplicate actual transaction result {identity}")
            result_by_identity[identity] = result
        expected_phase_counts = {"positive": 20, "stopped": 5, "recovery": 20}
        success_count = 0
        stopped_hashes: set[str] = set()
        for phase, expected_count in expected_phase_counts.items():
            starts = [
                record
                for record in events
                if record.get("event") == "actual_control_phase_start" and record.get("phase") == phase
            ]
            completes = [
                record
                for record in events
                if record.get("event") == "actual_control_phase_complete" and record.get("phase") == phase
            ]
            if len(starts) != 1 or len(completes) != 1:
                raise ValidationError(f"actual control {phase} boundary cardinality differs")
            if (
                starts[0].get("command_sha256") != command_hashes[phase]
                or completes[0].get("command_sha256") != command_hashes[phase]
                or completes[0].get("offered_counts")
                != {f"uav{index}": expected_count for index in range(1, 6)}
                or completes[0].get("quarantined_uavs") != []
            ):
                raise ValidationError(f"actual control {phase} boundary evidence differs")
            heartbeat_values = [
                int(completes[0].get("heartbeat_counts", {}).get(f"uav{index}", -1))
                for index in range(1, 6)
            ]
            if (
                phase == "stopped" and heartbeat_values != [0] * 5
            ) or (
                phase != "stopped" and any(value < 3 for value in heartbeat_values)
            ):
                raise ValidationError(f"actual control {phase} heartbeat evidence is insufficient")
            for uav in range(1, 6):
                selected = sorted(
                    (
                        record
                        for record in offers
                        if record.get("phase") == phase and record.get("uav") == uav
                    ),
                    key=lambda record: int(record.get("sequence", 0)),
                )
                if [record.get("sequence") for record in selected] != list(range(1, expected_count + 1)):
                    raise ValidationError(f"{phase}/uav{uav} real command offer sequence differs")
                previous_complete: int | None = None
                for request in selected:
                    sequence = int(request["sequence"])
                    if (
                        request.get("endpoint_form") != ACTUAL_CONTROL_ENDPOINT_FORM
                        or request.get("cell_id") != f"uav{uav}.control.downlink"
                        or request.get("flow_id") != f"uav{uav}.control.downlink"
                        or request.get("source_ip") != "10.71.0.10"
                        or request.get("source_udp_port") != 14600
                        or request.get("destination_ip") != f"10.71.{uav}.10"
                        or request.get("destination_udp_port") != 14600 + uav
                        or request.get("tos") != TOS_BY_CLASS["control"]
                        or request.get("full_run_nonce") != run_nonce
                        or request.get("transport_nonce32") != run_nonce
                        or request.get("transport_nonce_derivation") != "identity/full_run_nonce32"
                    ):
                        raise ValidationError(f"{phase}/uav{uav}/{sequence} request route/nonce differs")
                    marker_bytes = bytes.fromhex(str(request["marker_frame_hex"]))
                    command_bytes = bytes.fromhex(str(request["command_frame_hex"]))
                    marker_frame = parse_actual_mavlink_frame(marker_bytes)
                    command_frame = parse_actual_mavlink_frame(command_bytes)
                    marker_payload = marker_frame["payload"]
                    marker = marker_payload[1:51].rstrip(b"\0").decode("ascii")
                    marker_decoded = decode_marker(marker)
                    if (
                        marker_frame["message_id"] != 253
                        or (marker_frame["system_id"], marker_frame["component_id"]) != (255, 190)
                        or marker != request.get("marker_text")
                        or marker_decoded.get("run_nonce") != run_nonce
                        or marker_decoded.get("phase") != phase
                        or marker_decoded.get("uav") != uav
                        or marker_decoded.get("sequence") != sequence
                        or marker_frame["sha256"] != request.get("marker_frame_sha256")
                        or request.get("marker_send_return_size") != len(marker_bytes)
                    ):
                        raise ValidationError(f"{phase}/uav{uav}/{sequence} marker bytes differ")
                    command_payload = command_frame["payload"]
                    if len(command_payload) != 33:
                        raise ValidationError("MAV_CMD_REQUEST_MESSAGE payload length differs")
                    command_fields = struct.unpack("<7fHBBB", command_payload)
                    if (
                        command_frame["message_id"] != 76
                        or (command_frame["system_id"], command_frame["component_id"]) != (255, 190)
                        or command_fields[0] != 148.0
                        or any(value != 0.0 for value in command_fields[1:7])
                        or command_fields[7:] != (512, uav, 1, 0)
                        or command_frame["sha256"] != request.get("command_frame_sha256")
                        or request.get("command_send_return_size") != len(command_bytes)
                    ):
                        raise ValidationError(f"{phase}/uav{uav}/{sequence} command bytes differ")
                    identity = (phase, uav, sequence)
                    result = result_by_identity.get(identity)
                    if result is None:
                        raise ValidationError(f"{phase}/uav{uav}/{sequence} has no outcome")
                    if (
                        result.get("endpoint_form") != ACTUAL_CONTROL_ENDPOINT_FORM
                        or result.get("record_nonce") != request.get("record_nonce")
                        or result.get("command_frame_sha256") != command_frame["sha256"]
                        or result.get("marker_frame_sha256") != marker_frame["sha256"]
                        or result.get("full_run_nonce") != run_nonce
                        or result.get("transport_nonce32") != run_nonce
                        or result.get("transport_nonce_derivation") != "identity/full_run_nonce32"
                    ):
                        raise ValidationError(f"{phase}/uav{uav}/{sequence} outcome binding differs")
                    sent_ns = int(request["sent_monotonic_ns"])
                    completed_ns = int(result["completed_monotonic_ns"])
                    if previous_complete is not None and sent_ns < previous_complete:
                        raise ValidationError(f"{phase}/uav{uav} requests overlap")
                    previous_complete = completed_ns
                    downlink_record = {
                        "record_nonce": str(request["record_nonce"]),
                        "transport_payload_sha256": command_frame["sha256"],
                        "transport_payload_size": len(command_bytes),
                        "sent_monotonic_ns": sent_ns,
                        "sequence": sequence,
                    }
                    offered[(phase, f"uav{uav}.control.downlink")].append(downlink_record)
                    matching_downlink = [
                        record
                        for record in adapter_forward.get(f"uav{uav}", {}).get("gcs_to_tail", [])
                        if record.get("sha256") == command_frame["sha256"]
                    ]
                    if phase == "stopped":
                        stopped_hashes.add(command_frame["sha256"])
                        if matching_downlink:
                            raise ValidationError("stopped command reached actual MAVProxy tail")
                        if (
                            result.get("timed_out") is not True
                            or result.get("success") is not False
                            or result.get("ack") is not None
                            or result.get("requested_telemetry") is not None
                            or result.get("timeout_contract_satisfied") is not True
                            or float(result.get("timeout_elapsed_ms", 0.0)) < 3000.0
                        ):
                            raise ValidationError(f"stopped/uav{uav}/{sequence} timeout differs")
                        continue
                    if len(matching_downlink) != 1:
                        raise ValidationError(f"{phase}/uav{uav}/{sequence} lacks exact tail request forward")
                    received[(phase, f"uav{uav}.control.downlink")].append(
                        {
                            **downlink_record,
                            "received_monotonic_ns": int(matching_downlink[0]["monotonic_ns"]),
                        }
                    )
                    if (
                        result.get("timed_out") is not False
                        or result.get("success") is not True
                        or not isinstance(result.get("ack"), dict)
                        or not isinstance(result.get("requested_telemetry"), dict)
                    ):
                        raise ValidationError(f"{phase}/uav{uav}/{sequence} real outcome is incomplete")
                    ack = result["ack"]
                    telemetry = result["requested_telemetry"]
                    ack_frame = parse_actual_mavlink_frame(bytes.fromhex(str(ack["mavlink_frame_hex"])))
                    telemetry_frame = parse_actual_mavlink_frame(
                        bytes.fromhex(str(telemetry["mavlink_frame_hex"]))
                    )
                    if (
                        ack_frame["message_id"] != 77
                        or (ack_frame["system_id"], ack_frame["component_id"]) != (uav, 1)
                        or int.from_bytes(ack_frame["payload"][:2], "little") != 512
                        or ack_frame["payload"][2] != 0
                        or ack.get("mavlink_frame_sha256") != ack_frame["sha256"]
                        or ack.get("mavlink_frame_size") != ack_frame["size"]
                        or ack.get("transport_payload_sha256") != ack_frame["sha256"]
                        or ack.get("mavlink_command") != 512
                        or ack.get("mavlink_result") != 0
                        or ack.get("peer_ip") != f"10.71.{uav}.10"
                        or ack.get("peer_udp_port") != 14600 + uav
                        or ack.get("uav") != uav
                        or ack.get("phase") != phase
                        or ack.get("sequence") != sequence
                        or ack.get("request_command_frame_sha256") != command_frame["sha256"]
                        or telemetry_frame["message_id"] != 148
                        or (telemetry_frame["system_id"], telemetry_frame["component_id"]) != (uav, 1)
                    ):
                        raise ValidationError(f"{phase}/uav{uav}/{sequence} ACK/telemetry bytes differ")
                    ack_hash = str(ack["transport_payload_sha256"])
                    ack_received_ns = ack.get("received_monotonic_ns")
                    if (
                        isinstance(ack_received_ns, bool)
                        or not isinstance(ack_received_ns, int)
                        or not sent_ns <= ack_received_ns <= completed_ns
                    ):
                        raise ValidationError(
                            f"{phase}/uav{uav}/{sequence} ACK receive time differs"
                        )
                    ack_datagrams = [
                        record
                        for record in datagrams_by_hash.get(ack_hash, [])
                        if record.get("received_monotonic_ns") == ack_received_ns
                        and isinstance(record.get("monotonic_ns"), int)
                        and not isinstance(record.get("monotonic_ns"), bool)
                        and ack_received_ns
                        <= record["monotonic_ns"]
                        <= completed_ns
                        and record.get("peer_ip") == ack["peer_ip"]
                        and record.get("peer_udp_port") == ack["peer_udp_port"]
                        and record.get("rx_tos") == TOS_BY_CLASS["control"]
                        and record.get("transport_payload_size") == ack_frame["size"]
                    ]
                    tail_forwards = [
                        record
                        for record in adapter_forward.get(f"uav{uav}", {}).get("tail_to_gcs", [])
                        if record.get("sha256") == ack_hash
                        and record.get("bytes") == ack_frame["size"]
                        and sent_ns
                        <= int(record.get("monotonic_ns", -1))
                        <= completed_ns
                    ]
                    if len(ack_datagrams) != 1 or len(tail_forwards) != 1:
                        raise ValidationError(f"{phase}/uav{uav}/{sequence} ACK datagram lineage differs")
                    telemetry_hash = str(telemetry["transport_payload_sha256"])
                    if (
                        not datagrams_by_hash.get(telemetry_hash)
                        or not any(
                            record.get("sha256") == telemetry_hash
                            for record in adapter_forward.get(f"uav{uav}", {}).get("tail_to_gcs", [])
                        )
                    ):
                        raise ValidationError(f"{phase}/uav{uav}/{sequence} telemetry datagram lineage differs")
                    uplink_nonce = sha256_bytes(
                        f"{request['record_nonce']}:real-ack".encode("ascii")
                    )
                    uplink_record = {
                        "record_nonce": uplink_nonce,
                        "transport_payload_sha256": ack_hash,
                        "transport_payload_size": int(ack_datagrams[0]["transport_payload_size"]),
                        "sent_monotonic_ns": int(tail_forwards[0]["monotonic_ns"]),
                        "sequence": sequence,
                    }
                    offered[(phase, f"uav{uav}.control.uplink")].append(uplink_record)
                    received[(phase, f"uav{uav}.control.uplink")].append(
                        {
                            **uplink_record,
                            "received_monotonic_ns": ack_received_ns,
                        }
                    )
                    success_count += 1

        quarantines = [record for record in events if record.get("event") == "stopped_attempt_quarantined"]
        if len(quarantines) != 25:
            raise ValidationError("durable stopped attempt quarantine count is not 25")
        guard = [record for record in events if record.get("event") == "recovery_drain_guard_passed"]
        if (
            len(guard) != 1
            or guard[0].get("endpoint_form") != ACTUAL_CONTROL_ENDPOINT_FORM
            or guard[0].get("expired_attempt_counts")
            != {f"uav{index}": 5 for index in range(1, 6)}
            or int(guard[0].get("quiet_drain_ns", 0)) < 10_000_000_000
        ):
            raise ValidationError("recovery durable stopped drain guard differs")
        for uav in range(1, 6):
            stopped_forwards = [
                record
                for record in adapter_forward.get(f"uav{uav}", {}).get("tail_to_gcs", [])
                if windows["stopped"]["start_monotonic_ns"]
                <= int(record.get("monotonic_ns", -1))
                < windows["stopped"]["end_monotonic_ns"]
            ]
            unique_stopped: dict[tuple[str, int], dict[str, Any]] = {}
            for record in stopped_forwards:
                key = (str(record.get("sha256")), int(record.get("bytes", 0)))
                unique_stopped.setdefault(key, record)
            if len(unique_stopped) < 5:
                raise ValidationError(f"stopped/uav{uav} real tail uplink offers are fewer than five")
            for sequence, ((payload_hash, payload_size), record) in enumerate(
                list(unique_stopped.items())[:5], start=1
            ):
                offered[("stopped", f"uav{uav}.control.uplink")].append(
                    {
                        "record_nonce": sha256_bytes(
                            f"stopped:uav{uav}:{sequence}:{payload_hash}".encode()
                        ),
                        "transport_payload_sha256": payload_hash,
                        "transport_payload_size": payload_size,
                        "sent_monotonic_ns": int(record["monotonic_ns"]),
                        "sequence": sequence,
                    }
                )
        if any(
            record.get("sha256") in stopped_hashes
            for uav in range(1, 6)
            for record in adapter_forward.get(f"uav{uav}", {}).get("gcs_to_tail", [])
        ):
            raise ValidationError("a stopped request was released after engine recovery")
        expected_keys = {
            (phase, f"uav{uav}.control.{direction}")
            for phase in ("positive", "stopped", "recovery")
            for uav in range(1, 6)
            for direction in ("downlink", "uplink")
        }
        if set(offered) != expected_keys:
            raise ValidationError("normalized actual control matrix key set is incomplete")
        details["actual_control"] = {
            "event_count": len(events),
            "successful_real_transactions": success_count,
            "stopped_timeout_transactions": 25,
            "control_endpoint_form": ACTUAL_CONTROL_ENDPOINT_FORM,
            "role_subject": "gcs_control_probe",
        }
    except (KeyError, OSError, TypeError, ValueError, ValidationError) as exc:
        failures.append(str(exc))

    try:
        topology_samples = strict_jsonl(run_dir / "raw/topology_monitor/samples.jsonl")
        lifecycle_records = strict_jsonl(run_dir / "raw/lifecycle.jsonl")
        lifecycle_times = {
            str(record.get("event")): int(record["monotonic_ns"])
            for record in lifecycle_records
            if record.get("event")
            in {
                "actual_sitl_adapters_ready",
                "actual_control_start_requested",
                "actual_control_started",
                "actual_control_stop_requested",
                "actual_control_stopped",
                "actual_sitl_stack_stop_requested",
                "actual_sitl_stack_stopped",
            }
        }
        required_lifecycle = {
            "actual_sitl_adapters_ready",
            "actual_control_start_requested",
            "actual_control_started",
            "actual_control_stop_requested",
            "actual_control_stopped",
            "actual_sitl_stack_stop_requested",
            "actual_sitl_stack_stopped",
        }
        if set(lifecycle_times) != required_lifecycle:
            raise ValidationError("critical actual process lifecycle boundaries differ")
        critical: dict[tuple[int, int], tuple[str, str]] = {}
        for channel in manifest.get("channels", []):
            for role in ("mavproxy", "sitl"):
                identity = channel[role]
                critical[(int(identity["pid"]), int(identity["start_ticks"]))] = (
                    "container-root",
                    str(identity["exe_sha256"]),
                )
        for uav, ready in ready_documents.items():
            adapter = ready["adapter"]
            critical[(int(adapter["pid"]), int(adapter["start_ticks"]))] = (
                f"ams-{uav}",
                str(adapter["exe_sha256"]),
            )
        supervisor = aggregate.get("supervisor", {})
        supervisor_identity = (
            int(supervisor["pid"]),
            int(supervisor["start_ticks"]),
        )
        critical[supervisor_identity] = (
            "container-root",
            str(supervisor["exe_sha256"]),
        )
        socket_ready = strict_json(run_dir / "raw/state/actual-control.socket-ready.json")
        control_identity = (int(socket_ready["pid"]), int(socket_ready["start_ticks"]))
        stack_samples = [
            sample
            for sample in topology_samples
            if lifecycle_times["actual_sitl_adapters_ready"]
            <= int(sample.get("monotonic_ns", -1))
            < lifecycle_times["actual_sitl_stack_stop_requested"]
        ]
        if not stack_samples:
            raise ValidationError("continuous topology has no actual stack samples")
        for sample in stack_samples:
            process_index = {
                (int(process["pid"]), int(process["start_ticks"])): process
                for process in sample.get("processes", [])
                if isinstance(process, dict)
                and isinstance(process.get("pid"), int)
                and isinstance(process.get("start_ticks"), int)
            }
            for identity, (namespace, executable_hash) in critical.items():
                process = process_index.get(identity)
                if (
                    process is None
                    or process.get("namespace") != namespace
                    or process.get("executable_sha256") != executable_hash
                ):
                    raise ValidationError(
                        f"critical actual process vanished/restarted: {identity}"
                    )
            supervisor_process = process_index.get(supervisor_identity)
            if supervisor_process is None or not any(
                str(token).endswith("actual_sitl_endpoint_orchestrator.py")
                for token in supervisor_process.get("cmdline", [])
            ):
                raise ValidationError("authorization issuer supervisor role differs")
            sample_time = int(sample["monotonic_ns"])
            control_process = process_index.get(control_identity)
            control_present = (
                control_process is not None
                and control_process.get("namespace") == "ams-gcs"
                and any(
                    str(token).endswith("actual_sitl_control_probe.py")
                    for token in control_process.get("cmdline", [])
                )
            )
            control_expected: bool | None
            if (
                lifecycle_times["actual_control_started"]
                <= sample_time
                < lifecycle_times["actual_control_stop_requested"]
            ):
                control_expected = True
            elif (
                sample_time < lifecycle_times["actual_control_start_requested"]
                or sample_time >= lifecycle_times["actual_control_stopped"]
            ):
                control_expected = False
            else:
                control_expected = None
            if control_expected is not None and control_expected != control_present:
                raise ValidationError("GCS actual control process continuity differs")
        details["critical_process_lineage"] = {
            "stack_sample_count": len(stack_samples),
            "critical_identity_count": len(critical) + 1,
        }
    except (KeyError, OSError, TypeError, ValueError, ValidationError) as exc:
        failures.append(str(exc))

    return failures, details, offered, received


def validate(
    run_dir: Path,
    matrix_path: Path = DEFAULT_MATRIX,
    m2_receipt_path: Path = DEFAULT_M2_RECEIPT,
) -> dict[str, Any]:
    gate_failures: dict[str, list[str]] = defaultdict(list)
    details: dict[str, dict[str, Any]] = defaultdict(dict)

    try:
        run = strict_json(run_dir / "raw/run_contract.json")
    except ValidationError as exc:
        return {
            "contract": RESULT_CONTRACT,
            "run_id": "unavailable",
            "runtime_id": "unavailable",
            "passed": False,
            "gates": {"run_identity": gate([str(exc)])},
            "metrics": {},
            "failures": [f"run_identity: {exc}"],
        }
    run_id = run.get("run_id")
    runtime_id = run.get("runtime_id")
    run_nonce = run.get("run_nonce")
    execution = run.get("execution")
    formal_execution = execution == {
        "mode": "formal",
        "acceptance_eligible": True,
        "formal_m2_predecessor_bound": True,
    }
    technical_smoke = execution == {
        "mode": "technical_smoke",
        "acceptance_eligible": False,
        "formal_m2_predecessor_bound": False,
    }
    gate_failures["run_identity"].extend(
        exact_keys(
            run,
            {
                "contract",
                "run_id",
                "runtime_id",
                "run_nonce",
                "created_monotonic_ns",
                "execution",
                "matrix",
                "endpoint_namespaces",
                "ns3_namespace",
                "flight_runtime",
                "packet_engine",
                "m2_predecessor",
                "p2mp",
                "source_sha256",
            },
            "run contract",
        )
    )
    if run.get("contract") != RUN_CONTRACT:
        gate_failures["run_identity"].append("run contract identity mismatch")
    if not formal_execution and not technical_smoke:
        gate_failures["run_identity"].append("execution mode/eligibility tuple is invalid")
    if not isinstance(run_id, str) or not run_id:
        gate_failures["run_identity"].append("run_id is invalid")
    if not isinstance(runtime_id, str) or not HEX32.fullmatch(runtime_id):
        gate_failures["run_identity"].append("runtime_id is not exact 32-hex")
    if not isinstance(run_nonce, str) or not HEX32.fullmatch(run_nonce):
        gate_failures["run_identity"].append("run_nonce is not exact 32-hex")
    if (
        run.get("endpoint_namespaces") != NAMESPACES
        or run.get("ns3_namespace") != "ams-ns3"
    ):
        gate_failures["run_identity"].append("namespace identity is not exact")
    engine_identity = run.get("packet_engine")
    if not isinstance(engine_identity, dict):
        gate_failures["run_identity"].append("packet_engine identity is absent")
    else:
        try:
            binary = Path(engine_identity["path"])
            if (
                engine_identity.get("contract") != "ams.tap_packet_engine/v1"
                or engine_identity.get("uav_count") != 5
                or not regular_file(binary)
                or sha256_file(binary) != engine_identity.get("sha256")
                or binary.stat().st_size != engine_identity.get("size")
            ):
                gate_failures["run_identity"].append(
                    "packet-engine executable identity mismatch"
                )
            if engine_identity.get("required_modules") != REQUIRED_NS3_MODULES.split(
                ","
            ):
                gate_failures["run_identity"].append(
                    "packet-engine required module union mismatch"
                )
        except (KeyError, OSError, TypeError):
            gate_failures["run_identity"].append(
                "packet-engine executable identity is malformed"
            )
    sources = run.get("source_sha256")
    expected_source_paths = {
        matrix_path.resolve().relative_to(ROOT).as_posix(),
        "network/config/endpoint_transaction_schema.json",
        "network/validation/endpoint_transaction.py",
        "network/ns3/scratch/ams-tap-packet-engine.cc",
        "network/ns3/tap_packet_engine_config.py",
        "network/ns3/run_ns3_tap_packet_engine.sh",
        "network/ns3/build_ns3_tap_packet_engine.sh",
        "network/ns3/ns3_build_receipt.py",
        "network/scripts/raw_packet_capture.py",
        "network/bridge/opaque_udp_relay.py",
        "network/bridge/runtime_clock_beacon.py",
        "network/bridge/actual_sitl_mavlink_endpoint.py",
        "network/scripts/actual_sitl_endpoint_orchestrator.py",
        "network/scripts/actual_sitl_control_probe.py",
        "network/scripts/m3_topology_monitor.py",
        "network/scripts/write_run_provenance.py",
        "network/scripts/m3_external_matrix_probe.py",
        "network/scripts/run_m3_external_matrix.sh",
        "network/scripts/validate_m3_external_matrix.py",
        "network/validation/validate_m3_external_matrix.py",
        "network/config/scenario_5uav.yaml",
        "src/multiagent_simulation/launch/multiagent_simulation.launch.py",
    }
    if not isinstance(sources, dict) or set(sources) != expected_source_paths:
        gate_failures["run_identity"].append("source hash map key set is not exact")
    else:
        for relative, expected_hash in sources.items():
            path = ROOT / relative
            if not regular_file(path) or sha256_file(path) != expected_hash:
                gate_failures["run_identity"].append(
                    f"source identity mismatch: {relative}"
                )

    if isinstance(engine_identity, dict):
        try:
            from network.ns3 import ns3_build_receipt as receipt_tool

            ns3_dir = Path(engine_identity["ns3_dir"])
            copied_source = Path(engine_identity["copied_source"])
            source_receipt = Path(engine_identity["build_receipt"]["path"])
            run_receipt = run_dir / "raw/ns3_build_receipt.json"
            receipt_args = receipt_tool.parse_args(
                [
                    "verify",
                    "--ns3-dir",
                    str(ns3_dir),
                    "--program",
                    "ams-tap-packet-engine",
                    "--project-source",
                    str(ROOT / "network/ns3/scratch/ams-tap-packet-engine.cc"),
                    "--copied-source",
                    str(copied_source),
                    "--executable",
                    str(Path(engine_identity["path"])),
                    "--required-modules",
                    REQUIRED_NS3_MODULES,
                    "--receipt",
                    str(source_receipt),
                ]
            )
            subject = receipt_tool.build_subject(receipt_args)
            receipt_tool.validate_receipt_file(source_receipt, subject)
            receipt_tool.validate_receipt_file(run_receipt, subject)
            if (
                sha256_file(source_receipt)
                != engine_identity["build_receipt"].get("sha256")
                or source_receipt.read_bytes() != run_receipt.read_bytes()
            ):
                gate_failures["ns3_build_receipt"].append(
                    "run-local receipt is not byte-identical to bound source receipt"
                )
        except (
            KeyError,
            OSError,
            TypeError,
            ValueError,
            receipt_tool.ReceiptError,
        ) as exc:
            gate_failures["ns3_build_receipt"].append(str(exc))

    try:
        matrix = strict_json(matrix_path)
        from network.validation.endpoint_transaction import (
            build_resolved_matrix,
            validate_matrix_data,
        )

        matrix_errors = validate_matrix_data(matrix)
        if matrix_errors:
            gate_failures["matrix_contract"].extend(matrix_errors)
        if matrix != build_resolved_matrix():
            gate_failures["matrix_contract"].append(
                "matrix differs from independent config derivation"
            )
        matrix_hash = sha256_file(matrix_path)
        endpoint_schema_path = ROOT / "network/config/endpoint_transaction_schema.json"
        endpoint_schema = strict_json(endpoint_schema_path)
        endpoint_schema_payload = endpoint_schema_path.read_bytes()
        endpoint_schema_copy = run_dir / "raw/endpoint_transaction_schema.json"
        endpoint_schema_binding = {
            "path": endpoint_schema_path.relative_to(ROOT).as_posix(),
            "sha256": sha256_bytes(endpoint_schema_payload),
            "$id": "https://ams.local/schemas/endpoint-transaction-v1.json",
            "matrix_contract": matrix.get("contract"),
            "raw_copy_path": "raw/endpoint_transaction_schema.json",
        }
        if run.get("matrix") != {
            "path": matrix_path.resolve().relative_to(ROOT).as_posix(),
            "sha256": matrix_hash,
            "resolved_cells_sha256": matrix.get("resolved_cells_sha256"),
            "cell_count": 30,
            "profile": "m3_full",
            "endpoint_schema": endpoint_schema_binding,
        }:
            gate_failures["matrix_contract"].append(
                "run contract does not exactly bind M3 matrix"
            )
        if (
            endpoint_schema.get("$id") != endpoint_schema_binding["$id"]
            or not regular_file(endpoint_schema_copy)
            or endpoint_schema_copy.read_bytes() != endpoint_schema_payload
        ):
            gate_failures["matrix_contract"].append(
                "run-local endpoint schema is absent or not byte-identical"
            )
        predecessor_schema = (
            run.get("m2_predecessor", {})
            .get("endpoint_transaction", {})
            .get("schema_sha256")
            if formal_execution and isinstance(run.get("m2_predecessor"), dict)
            else None
        )
        if formal_execution and predecessor_schema != endpoint_schema_binding["sha256"]:
            gate_failures["matrix_contract"].append(
                "M3 endpoint schema differs from formal M2 predecessor"
            )
        cells = {cell["cell_id"]: cell for cell in matrix["cells"]}
    except (ValidationError, ValueError, KeyError, TypeError) as exc:
        gate_failures["matrix_contract"].append(str(exc))
        matrix, cells, matrix_hash = {}, {}, "unavailable"

    shared_core_identity: dict[str, Any] = {}
    if technical_smoke:
        if run.get("m2_predecessor") is not None or (
            run_dir / "raw/m2_component_host_final_receipt.json"
        ).exists():
            gate_failures["m2_extension"].append(
                "technical smoke imported forbidden formal M2 predecessor evidence"
            )
        details["m2_extension"] = {
            "mode": "technical_smoke",
            "acceptance_eligible": False,
        }
    elif isinstance(engine_identity, dict):
        extension_failures, extension_details, shared_core_identity = (
            validate_m2_extension(
                run_dir,
                run,
                matrix,
                engine_identity,
                m2_receipt_path,
            )
        )
        gate_failures["m2_extension"].extend(extension_failures)
        details["m2_extension"] = extension_details
    else:
        gate_failures["m2_extension"].append(
            "packet-engine identity is unavailable for M2 extension proof"
        )

    try:
        phase_contract = strict_json(run_dir / "raw/phase_contract.json")
        gate_failures["phase_contract"].extend(
            exact_keys(
                phase_contract,
                {
                    "contract",
                    "run_id",
                    "runtime_id",
                    "run_nonce",
                    "matrix_sha256",
                    "created_monotonic_ns",
                    "stop_request_monotonic_ns",
                    "restart_request_monotonic_ns",
                    "windows",
                },
                "phase contract",
            )
        )
        if phase_contract.get("contract") != PHASE_CONTRACT:
            gate_failures["phase_contract"].append("phase contract identity mismatch")
        if (
            phase_contract.get("run_id"),
            phase_contract.get("runtime_id"),
            phase_contract.get("run_nonce"),
            phase_contract.get("matrix_sha256"),
        ) != (run_id, runtime_id, run_nonce, matrix_hash):
            gate_failures["phase_contract"].append(
                "phase contract crosses run/matrix identity"
            )
        windows_list = phase_contract.get("windows")
        if not isinstance(windows_list, list) or [
            item.get("phase") for item in windows_list
        ] != [
            "positive",
            "p2mp",
            "stopped",
            "recovery",
        ]:
            raise ValidationError("phase windows/order is not exact")
        windows = {item["phase"]: item for item in windows_list}
        expected_counts = {
            "positive": (20, 0),
            "p2mp": (0, 20),
            "stopped": (5, 0),
            "recovery": (20, 0),
        }
        expected_states = {
            "positive": "up_epoch_1",
            "p2mp": "up_epoch_1",
            "stopped": "stopped",
            "recovery": "up_epoch_2",
        }
        for phase, window in windows.items():
            if set(window) != {
                "phase",
                "start_monotonic_ns",
                "end_monotonic_ns",
                "offered_per_cell",
                "p2mp_roots",
                "send_span_ms",
                "expected_engine_state",
            }:
                gate_failures["phase_contract"].append(
                    f"{phase} window keys are not exact"
                )
                continue
            duration = window["end_monotonic_ns"] - window["start_monotonic_ns"]
            if duration <= 0 or window["send_span_ms"] * 1_000_000 >= duration:
                gate_failures["phase_contract"].append(
                    f"{phase} window timing is invalid"
                )
            if (window["offered_per_cell"], window["p2mp_roots"]) != expected_counts[
                phase
            ]:
                gate_failures["phase_contract"].append(
                    f"{phase} declared counts are not exact"
                )
            if window["expected_engine_state"] != expected_states[phase]:
                gate_failures["phase_contract"].append(
                    f"{phase} engine state is not exact"
                )
        if (
            windows["positive"]["end_monotonic_ns"]
            - windows["positive"]["start_monotonic_ns"]
            < 30_000_000_000
        ):
            gate_failures["phase_contract"].append(
                "positive window is shorter than 30 seconds"
            )
        if any(
            left["end_monotonic_ns"] > right["start_monotonic_ns"]
            for left, right in zip(windows_list, windows_list[1:])
        ):
            gate_failures["phase_contract"].append("phase windows overlap")
        command_hashes: dict[tuple[str, str], str] = {}
        for endpoint in ENDPOINTS:
            for index, window in enumerate(windows_list, start=1):
                command_path = run_dir / (
                    f"raw/control/{endpoint}/{index:03d}-{window['phase']}.json"
                )
                command = strict_json(command_path)
                expected_command = {
                    "action": "phase",
                    "endpoint": endpoint,
                    "run_id": run_id,
                    "runtime_id": runtime_id,
                    "run_nonce": run_nonce,
                    **window,
                }
                if command != expected_command:
                    gate_failures["phase_contract"].append(
                        f"{endpoint}/{window['phase']} command differs from phase contract"
                    )
                command_hashes[(endpoint, window["phase"])] = sha256_file(command_path)
            shutdown_path = run_dir / f"raw/control/{endpoint}/999-shutdown.json"
            shutdown = strict_json(shutdown_path)
            if shutdown != {
                "action": "shutdown",
                "endpoint": endpoint,
                "run_id": run_id,
                "runtime_id": runtime_id,
                "run_nonce": run_nonce,
                "not_before_monotonic_ns": windows["recovery"]["end_monotonic_ns"]
                + 500_000_000,
            }:
                gate_failures["phase_contract"].append(
                    f"{endpoint} shutdown command is not exact"
                )
    except (ValidationError, KeyError, TypeError) as exc:
        gate_failures["phase_contract"].append(str(exc))
        windows = {}
        command_hashes = {}

    actual_failures, actual_details, actual_offered, actual_received = (
        validate_actual_sitl_control(
            run_dir,
            run=run,
            matrix=matrix if isinstance(matrix, dict) else {},
            windows=windows,
        )
    )
    gate_failures["actual_sitl_control"].extend(actual_failures)
    details["actual_sitl_control"] = actual_details

    endpoint_records: list[dict[str, Any]] = []
    endpoint_by_name: dict[str, list[dict[str, Any]]] = {}
    for endpoint in ENDPOINTS:
        try:
            records = strict_jsonl(run_dir / f"raw/endpoints/{endpoint}.jsonl")
            endpoint_by_name[endpoint] = records
            endpoint_records.extend(records)
            sequences = [record.get("event_sequence") for record in records]
            if sequences != list(range(1, len(records) + 1)):
                gate_failures["endpoint_lifecycle"].append(
                    f"{endpoint} event sequence is not contiguous"
                )
            if any(
                (
                    record.get("schema"),
                    record.get("run_id"),
                    record.get("runtime_id"),
                    record.get("run_nonce"),
                    record.get("endpoint"),
                )
                != (ENDPOINT_SCHEMA, run_id, runtime_id, run_nonce, endpoint)
                for record in records
            ):
                gate_failures["endpoint_lifecycle"].append(
                    f"{endpoint} records cross identity/schema"
                )
            times = [record.get("monotonic_ns") for record in records]
            if any(not isinstance(value, int) for value in times) or times != sorted(
                times
            ):
                gate_failures["endpoint_lifecycle"].append(
                    f"{endpoint} timestamps are invalid/nonmonotonic"
                )
            if len([r for r in records if r.get("event") == "agent_ready"]) != 1:
                gate_failures["endpoint_lifecycle"].append(
                    f"{endpoint} has no exact single ready event"
                )
            if len([r for r in records if r.get("event") == "agent_shutdown"]) != 1:
                gate_failures["endpoint_lifecycle"].append(
                    f"{endpoint} has no exact single shutdown event"
                )
            if any(
                r.get("event") in {"send_error", "foreign_receive"} for r in records
            ):
                gate_failures["endpoint_lifecycle"].append(
                    f"{endpoint} recorded send/foreign traffic error"
                )
            for phase in ("positive", "p2mp", "stopped", "recovery"):
                starts = [
                    r
                    for r in records
                    if r.get("event") == "phase_start" and r.get("phase") == phase
                ]
                completes = [
                    r
                    for r in records
                    if r.get("event") == "phase_complete" and r.get("phase") == phase
                ]
                if len(starts) != 1 or len(completes) != 1:
                    gate_failures["endpoint_lifecycle"].append(
                        f"{endpoint}/{phase} boundary cardinality mismatch"
                    )
                    continue
                window = windows.get(phase)
                command_hash = command_hashes.get((endpoint, phase))
                if not window or not command_hash:
                    continue
                start, complete = starts[0], completes[0]
                if (
                    start.get("command_sha256") != command_hash
                    or start.get("declared_start_monotonic_ns")
                    != window["start_monotonic_ns"]
                    or start.get("declared_end_monotonic_ns")
                    != window["end_monotonic_ns"]
                    or start.get("expected_engine_state")
                    != window["expected_engine_state"]
                    or complete.get("command_sha256") != command_hash
                    or complete.get("expected_engine_state")
                    != window["expected_engine_state"]
                ):
                    gate_failures["endpoint_lifecycle"].append(
                        f"{endpoint}/{phase} boundary fields do not bind its command"
                    )
                if not (
                    window["start_monotonic_ns"]
                    <= start["monotonic_ns"]
                    < window["end_monotonic_ns"]
                    <= complete["monotonic_ns"]
                    <= window["end_monotonic_ns"] + 1_000_000_000
                ):
                    gate_failures["endpoint_lifecycle"].append(
                        f"{endpoint}/{phase} actual boundary timing is outside tolerance"
                    )
        except ValidationError as exc:
            gate_failures["endpoint_lifecycle"].append(str(exc))

    canary_records: list[dict[str, Any]] = []
    canary_observations: dict[str, dict[str, Any]] = {}
    listener_windows: dict[str, tuple[int, int, int]] = {}
    try:
        canary_contract = strict_json(run_dir / "raw/forbidden_canary_contract.json")
        expected_canaries = expected_forbidden_canaries(str(run_nonce))
        if set(canary_contract) != {
            "contract",
            "run_id",
            "runtime_id",
            "run_nonce",
            "created_monotonic_ns",
            "canaries",
        } or (
            canary_contract.get("contract"),
            canary_contract.get("run_id"),
            canary_contract.get("runtime_id"),
            canary_contract.get("run_nonce"),
            canary_contract.get("canaries"),
        ) != (
            FORBIDDEN_CONTRACT,
            run_id,
            runtime_id,
            run_nonce,
            expected_canaries,
        ):
            raise ValidationError("forbidden canary contract is not exact/predeclared")
        if not isinstance(canary_contract.get("created_monotonic_ns"), int):
            raise ValidationError("forbidden canary contract timestamp is invalid")
        canary_records = expected_canaries
        for source in ("container-root", *ENDPOINTS):
            observation = strict_json(run_dir / f"raw/forbidden/{source}.json")
            expected_source = [
                canary
                for canary in canary_records
                if canary["source_endpoint"] == source
            ]
            if set(observation) != {
                "contract",
                "run_id",
                "runtime_id",
                "run_nonce",
                "source_endpoint",
                "started_monotonic_ns",
                "completed_monotonic_ns",
                "observations",
            } or (
                observation.get("contract"),
                observation.get("run_id"),
                observation.get("runtime_id"),
                observation.get("run_nonce"),
                observation.get("source_endpoint"),
            ) != (
                FORBIDDEN_RESULT_CONTRACT,
                run_id,
                runtime_id,
                run_nonce,
                source,
            ):
                raise ValidationError(
                    f"forbidden observation identity mismatch: {source}"
                )
            if (
                not isinstance(observation.get("started_monotonic_ns"), int)
                or not isinstance(observation.get("completed_monotonic_ns"), int)
                or observation["completed_monotonic_ns"]
                <= observation["started_monotonic_ns"]
                or not isinstance(observation.get("observations"), list)
                or len(observation["observations"]) != len(expected_source)
            ):
                raise ValidationError(
                    f"forbidden observation timing/count invalid: {source}"
                )
            for actual, expected in zip(
                observation["observations"], expected_source, strict=True
            ):
                if set(actual) != {
                    "canary_id",
                    "sequence",
                    "sent_monotonic_ns",
                    "send_return_size",
                    "transport_payload_sha256",
                } or (
                    actual.get("canary_id"),
                    actual.get("sequence"),
                    actual.get("send_return_size"),
                    actual.get("transport_payload_sha256"),
                ) != (
                    expected["canary_id"],
                    expected["sequence"],
                    expected["expected_send_return_size"],
                    expected["transport_payload_sha256"],
                ):
                    raise ValidationError(
                        f"forbidden send observation mismatch: {expected['canary_id']}"
                    )
                if not (
                    observation["started_monotonic_ns"]
                    <= actual["sent_monotonic_ns"]
                    <= observation["completed_monotonic_ns"]
                ):
                    raise ValidationError(
                        f"forbidden send time mismatch: {expected['canary_id']}"
                    )
                decoded = decode_forbidden_payload(
                    bytes.fromhex(expected["transport_payload_hex"]),
                    expected["canary_id"],
                )
                if (
                    decoded["kind"],
                    decoded["sequence"],
                    decoded["run_nonce"],
                    decoded["transport_payload_sha256"],
                ) != (
                    expected["kind"],
                    expected["sequence"],
                    run_nonce,
                    expected["transport_payload_sha256"],
                ):
                    raise ValidationError(
                        f"forbidden payload decode mismatch: {expected['canary_id']}"
                    )
                canary_observations[expected["canary_id"]] = actual
        for endpoint in ENDPOINTS:
            listener_events = strict_jsonl(
                run_dir / f"raw/forbidden/listener-{endpoint}.jsonl"
            )
            if [event.get("event") for event in listener_events] != [
                "listener_ready",
                "listener_shutdown",
            ]:
                raise ValidationError(
                    f"forbidden listener received traffic/restarted: {endpoint}"
                )
            if [event.get("event_sequence") for event in listener_events] != [1, 2]:
                raise ValidationError(
                    f"forbidden listener event sequence invalid: {endpoint}"
                )
            expected_bindings = [
                {
                    "canary_id": canary["canary_id"],
                    "address_family": canary["address_family"],
                    "ip": canary["destination_ip"],
                    "udp_port": canary["destination_udp_port"],
                }
                for canary in canary_records
                if canary["listener_endpoint"] == endpoint
            ]
            identity_keys = {
                "pid",
                "start_ticks",
                "executable",
                "executable_sha256",
                "bindings",
            }
            event_keys = {
                "schema",
                "run_id",
                "runtime_id",
                "run_nonce",
                "event_sequence",
                "monotonic_ns",
                "event",
                "endpoint",
                *identity_keys,
            }
            if any(set(event) != event_keys for event in listener_events):
                raise ValidationError(
                    f"forbidden listener event keys invalid: {endpoint}"
                )
            ready_event, shutdown_event = listener_events
            identity = {key: ready_event[key] for key in identity_keys}
            if (
                any(
                    (
                        event.get("schema"),
                        event.get("run_id"),
                        event.get("runtime_id"),
                        event.get("run_nonce"),
                        event.get("endpoint"),
                    )
                    != (
                        FORBIDDEN_LISTENER_SCHEMA,
                        run_id,
                        runtime_id,
                        run_nonce,
                        endpoint,
                    )
                    for event in listener_events
                )
                or {key: shutdown_event[key] for key in identity_keys} != identity
                or identity["bindings"] != expected_bindings
                or not isinstance(identity["pid"], int)
                or not isinstance(identity["start_ticks"], int)
                or not isinstance(identity["executable"], str)
                or not isinstance(identity["executable_sha256"], str)
                or HEX64.fullmatch(identity["executable_sha256"]) is None
            ):
                raise ValidationError(
                    f"forbidden listener lifecycle/identity invalid: {endpoint}"
                )
            executable = Path(identity["executable"])
            if (
                not executable.is_file()
                or sha256_file(executable) != identity["executable_sha256"]
            ):
                raise ValidationError(
                    f"forbidden listener executable identity mismatch: {endpoint}"
                )
            ready_file = strict_json(
                run_dir / f"raw/state/forbidden-listener-{endpoint}.ready.json"
            )
            if set(ready_file) != {
                "contract",
                "run_id",
                "runtime_id",
                "run_nonce",
                "endpoint",
                *identity_keys,
                "ready_monotonic_ns",
            } or (
                ready_file.get("contract"),
                ready_file.get("run_id"),
                ready_file.get("runtime_id"),
                ready_file.get("run_nonce"),
                ready_file.get("endpoint"),
                {key: ready_file.get(key) for key in identity_keys},
            ) != (
                FORBIDDEN_LISTENER_SCHEMA,
                run_id,
                runtime_id,
                run_nonce,
                endpoint,
                identity,
            ):
                raise ValidationError(
                    f"forbidden listener ready identity mismatch: {endpoint}"
                )
            for canary in canary_records:
                if canary["listener_endpoint"] != endpoint:
                    continue
                sent_ns = canary_observations[canary["canary_id"]]["sent_monotonic_ns"]
                if not (
                    ready_event["monotonic_ns"]
                    <= ready_file["ready_monotonic_ns"]
                    <= sent_ns
                    < shutdown_event["monotonic_ns"]
                ):
                    raise ValidationError(
                        f"forbidden listener was not active for {canary['canary_id']}"
                    )
            listener_windows[endpoint] = (
                ready_event["monotonic_ns"],
                shutdown_event["monotonic_ns"],
                ready_file["ready_monotonic_ns"],
            )
    except (KeyError, OSError, TypeError, ValueError, ValidationError) as exc:
        gate_failures["forbidden_paths"].append(str(exc))

    offered_by_phase_cell: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(
        list
    )
    received_by_phase_cell: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(
        list
    )
    offered_by_identity: dict[tuple[str, str, int, str], dict[str, Any]] = {}
    p2mp_offered: list[dict[str, Any]] = []
    p2mp_received_by_endpoint: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in endpoint_records:
        event = record.get("event")
        if event not in {"offered", "remote_receive"}:
            continue
        try:
            decoded = decode_transport(record.get("transport_payload_hex"))
            for key in (
                "transport_payload_sha256",
                "transport_payload_size",
                "traffic_class",
                "direction",
                "sequence",
                "record_nonce",
                "application_unit_sha256",
                "protocol_family",
                "p2mp",
            ):
                if record.get(key) != decoded.get(key):
                    raise ValidationError(
                        f"producer field {key} differs from decoded bytes"
                    )
            if (
                decoded["run_nonce"] != run_nonce
                or record.get("phase") != decoded["phase"]
            ):
                raise ValidationError("decoded unit crosses run/phase identity")
            if decoded.get("traffic_class") == "control":
                raise ValidationError(
                    "companion endpoint agent emitted forbidden synthetic control traffic"
                )
            phase = decoded["phase"]
            if phase not in windows:
                raise ValidationError("decoded phase has no declared window")
            event_time = (
                record.get("sent_monotonic_ns")
                if event == "offered"
                else record.get("received_monotonic_ns")
            )
            if not isinstance(event_time, int) or not (
                windows[phase]["start_monotonic_ns"]
                <= event_time
                < windows[phase]["end_monotonic_ns"]
            ):
                raise ValidationError(
                    "unit timestamp lies outside its half-open window"
                )
            payload_hash = decoded["transport_payload_sha256"]
            identity = (phase, decoded["flow_id"], decoded["sequence"], payload_hash)
            if decoded["p2mp"]:
                if event == "offered":
                    if (
                        record.get("endpoint") != "gcs"
                        or record.get("destination_ip") != P2MP_GROUP
                        or record.get("destination_udp_port") != P2MP_PORT
                    ):
                        raise ValidationError("P2MP root source/destination mismatch")
                    if identity in offered_by_identity:
                        raise ValidationError("duplicate P2MP root identity")
                    offered_by_identity[identity] = record
                    p2mp_offered.append(record)
                else:
                    endpoint = str(record.get("endpoint"))
                    if endpoint == "gcs" or record.get("local_udp_port") != P2MP_PORT:
                        raise ValidationError(
                            "P2MP delivery leg reached wrong endpoint/port"
                        )
                    p2mp_received_by_endpoint[endpoint].append(record)
                continue
            cell_id = decoded["flow_id"]
            cell = cells.get(cell_id)
            if cell is None:
                raise ValidationError(f"decoded unit has no matrix cell {cell_id}")
            if record.get("cell_id") != cell_id:
                raise ValidationError("producer cell label differs from decoded bytes")
            if decoded["protocol_family"] != cell["protocol"]["message_family"]:
                raise ValidationError(f"{cell_id} protocol family mismatch")
            expected_source_system = cell["source"]["mavlink_system_id"]
            expected_source_component = cell["source"]["mavlink_component_id"]
            expected_target_system = cell["destination"]["mavlink_system_id"]
            expected_target_component = cell["destination"]["mavlink_component_id"]
            if decoded["traffic_class"] != "additional_data" and (
                decoded["source_system"],
                decoded["source_component"],
                decoded["target_system"],
                decoded["target_component"],
            ) != (
                expected_source_system,
                expected_source_component,
                expected_target_system,
                expected_target_component,
            ):
                raise ValidationError(
                    f"{cell_id} MAVLink source/target identity mismatch"
                )
            if event == "offered":
                source_endpoint = (
                    "gcs"
                    if cell["source"]["namespace"] == "ams-gcs"
                    else cell["uav"]["name"]
                )
                if (
                    record.get("endpoint") != source_endpoint
                    or record.get("source_ip") != cell["source"]["ip"]
                    or record.get("source_udp_port") != cell["source"]["udp_port"]
                    or record.get("destination_ip") != cell["destination"]["ip"]
                    or record.get("destination_udp_port")
                    != cell["destination"]["udp_port"]
                    or record.get("tos") != cell["ns3_path"]["dscp_tos"]
                    or record.get("send_return_size")
                    != decoded["transport_payload_size"]
                ):
                    raise ValidationError(
                        f"{cell_id} offered endpoint/port/TOS mismatch"
                    )
                if identity in offered_by_identity:
                    raise ValidationError(f"duplicate offered identity {identity}")
                offered_by_identity[identity] = record
                offered_by_phase_cell[(phase, cell_id)].append(record)
            else:
                destination_endpoint = (
                    "gcs"
                    if cell["destination"]["namespace"] == "ams-gcs"
                    else cell["uav"]["name"]
                )
                if (
                    record.get("endpoint") != destination_endpoint
                    or record.get("local_udp_port") != cell["destination"]["udp_port"]
                    or record.get("peer_ip") != cell["source"]["ip"]
                    or record.get("peer_udp_port") != cell["source"]["udp_port"]
                    or record.get("socket_class") != cell["traffic_class"]
                ):
                    raise ValidationError(
                        f"{cell_id} remote endpoint/port/source mismatch"
                    )
                received_by_phase_cell[(phase, cell_id)].append(record)
        except (ValidationError, KeyError, TypeError, ValueError) as exc:
            gate_failures["decoded_endpoint_matrix"].append(
                f"{record.get('endpoint')}/{event}/{record.get('event_sequence')}: {exc}"
            )

    for key, records in actual_offered.items():
        offered_by_phase_cell[key].extend(records)
    for key, records in actual_received.items():
        received_by_phase_cell[key].extend(records)

    metrics: dict[str, Any] = {
        "cells": {},
        "nominal_rate_vector": {},
        "p2mp": {},
    }
    for phase in ("positive", "stopped", "recovery"):
        metrics["cells"][phase] = {}
        if phase not in windows:
            continue
        duration = (
            windows[phase]["end_monotonic_ns"] - windows[phase]["start_monotonic_ns"]
        )
        for cell_id in sorted(cells):
            offered = offered_by_phase_cell[(phase, cell_id)]
            received = received_by_phase_cell[(phase, cell_id)]
            offered_keys = {
                (r["record_nonce"], r["transport_payload_sha256"]) for r in offered
            }
            received_keys = [
                (r["record_nonce"], r["transport_payload_sha256"]) for r in received
            ]
            if len(received_keys) != len(set(received_keys)):
                gate_failures["decoded_endpoint_matrix"].append(
                    f"{phase}/{cell_id} duplicate remote delivery"
                )
            if any(key not in offered_keys for key in received_keys):
                gate_failures["decoded_endpoint_matrix"].append(
                    f"{phase}/{cell_id} remote delivery has no offer"
                )
            record_metrics = metric_record(offered, received, duration)
            metrics["cells"][phase][cell_id] = record_metrics
            if phase == "positive":
                unique_offers = {
                    (record["record_nonce"], record["transport_payload_sha256"]): record
                    for record in offered
                }
                offered_units = len(unique_offers)
                offered_bytes = sum(
                    int(record["transport_payload_size"])
                    for record in unique_offers.values()
                )
                metrics["nominal_rate_vector"][cell_id] = {
                    "offered_units": offered_units,
                    "offered_bytes": offered_bytes,
                    "duration_ns": duration,
                    "unit_rate_hz": round(offered_units * 1_000_000_000 / duration, 9),
                    "byte_rate_bps": round(
                        offered_bytes * 8 * 1_000_000_000 / duration, 9
                    ),
                }
            minimum = 20 if phase in {"positive", "recovery"} else 5
            if record_metrics["offered_unique"] < minimum:
                gate_failures[
                    "positive_matrix" if phase != "stopped" else "stopped_isolation"
                ].append(
                    f"{phase}/{cell_id} offered {record_metrics['offered_unique']} < {minimum}"
                )
            if phase == "stopped":
                if (
                    record_metrics["received_unique"] != 0
                    or record_metrics["loss_ratio"] != 1.0
                ):
                    gate_failures["stopped_isolation"].append(
                        f"{cell_id} delivered during stopped window"
                    )
                if (
                    record_metrics["latency_sample_count"] != 0
                    or record_metrics["latency_p95_ms"] != "inapplicable"
                ):
                    gate_failures["stopped_isolation"].append(
                        f"{cell_id} stopped latency is falsely applicable"
                    )
            else:
                required = math.ceil(0.95 * record_metrics["offered_unique"])
                if record_metrics["received_unique"] < required:
                    gate_failures["positive_matrix"].append(
                        f"{phase}/{cell_id} received {record_metrics['received_unique']} < {required}"
                    )
                if (
                    record_metrics["latency_sample_count"]
                    != record_metrics["received_unique"]
                ):
                    gate_failures["positive_matrix"].append(
                        f"{phase}/{cell_id} latency samples incomplete"
                    )
                if record_metrics["received_unique"] and (
                    record_metrics["latency_p95_ms"] == "inapplicable"
                    or record_metrics["goodput_bps"] == "inapplicable"
                ):
                    gate_failures["positive_matrix"].append(
                        f"{phase}/{cell_id} positive metric is inapplicable"
                    )

    offered_p2mp_keys = {
        (record["record_nonce"], record["transport_payload_sha256"]): record
        for record in p2mp_offered
    }
    if len(offered_p2mp_keys) < 20:
        gate_failures["p2mp"].append(f"P2MP roots {len(offered_p2mp_keys)} < 20")
    metrics["p2mp"]["root_records"] = len(offered_p2mp_keys)
    metrics["p2mp"]["receivers"] = {}
    for endpoint in ENDPOINTS[1:]:
        records = p2mp_received_by_endpoint[endpoint]
        keys = [(r["record_nonce"], r["transport_payload_sha256"]) for r in records]
        if len(keys) != len(set(keys)) or any(
            key not in offered_p2mp_keys for key in keys
        ):
            gate_failures["p2mp"].append(f"{endpoint} P2MP legs duplicate/unoffered")
        ratio = len(set(keys)) / len(offered_p2mp_keys) if offered_p2mp_keys else 0.0
        metrics["p2mp"]["receivers"][endpoint] = {
            "received_unique": len(set(keys)),
            "delivery_ratio": round(ratio, 9),
        }
        if len(set(keys)) < math.ceil(0.95 * len(offered_p2mp_keys)):
            gate_failures["p2mp"].append(f"{endpoint} P2MP delivery below 0.95")

    engine_records_by_epoch: dict[int, list[dict[str, Any]]] = {}
    engine_hashes: dict[int, str] = {}
    for epoch in (1, 2):
        try:
            config = strict_json(run_dir / f"logs/ns3_epoch{epoch}_config.json")
            canonical = config.get("canonical_config")
            config_hash = config.get("config_sha256")
            resolved = config.get("resolved")
            if (
                config.get("contract") != "ams.tap_packet_engine/v1"
                or not isinstance(canonical, str)
                or sha256_bytes(canonical.encode()) != config_hash
                or not isinstance(config_hash, str)
                or not HEX64.fullmatch(config_hash)
                or not isinstance(resolved, dict)
                or resolved.get("uav_count") != 5
                or resolved.get("event_epoch") != epoch
                or resolved.get("self_test") is not False
                or resolved.get("tap_gcs") != "tap-gcs"
                or resolved.get("tap_uavs")
                != [f"tap-uav{index}" for index in range(1, 6)]
            ):
                raise ValidationError(f"epoch {epoch} engine config/hash is not exact")
            engine_hashes[epoch] = config_hash
            records = strict_jsonl(run_dir / f"logs/ns3_epoch{epoch}_events.jsonl")
            sequences = [record.get("event_sequence") for record in records]
            if sequences != list(range(1, len(records) + 1)):
                raise ValidationError(
                    f"epoch {epoch} engine sequence is not contiguous"
                )
            sim_times = [record.get("sim_time_ns") for record in records]
            if any(
                not isinstance(value, int) for value in sim_times
            ) or sim_times != sorted(sim_times):
                raise ValidationError(f"epoch {epoch} sim timestamps are invalid")
            if any(
                record.get("schema") != ENGINE_SCHEMA
                or record.get("event_epoch") != epoch
                or record.get("config_sha256") != config_hash
                for record in records
            ):
                raise ValidationError(
                    f"epoch {epoch} engine events cross schema/config"
                )
            engine_records_by_epoch[epoch] = records
        except ValidationError as exc:
            gate_failures["ns3_path"].append(str(exc))

    if shared_core_identity:
        shared_core_identity["packet_engine"]["m3_config_sha256"] = {
            f"epoch{epoch}": engine_hashes.get(epoch) for epoch in (1, 2)
        }
        if set(engine_hashes) != {1, 2} or len(set(engine_hashes.values())) != 2:
            gate_failures["m2_extension"].append(
                "M3 epoch config hashes are unavailable/non-distinct"
            )

    engine_by_hash_epoch: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(
        list
    )
    forbidden_ns3_metrics: dict[str, dict[str, Any]] = {}
    for epoch, records in engine_records_by_epoch.items():
        for record in records:
            payload_hash = record.get("transport_payload_sha256")
            if isinstance(payload_hash, str):
                engine_by_hash_epoch[(epoch, payload_hash)].append(record)

    for cell_id in cells:
        for stopped_offer in offered_by_phase_cell[("stopped", cell_id)]:
            payload_hash = str(stopped_offer["transport_payload_sha256"])
            released = sum(
                len(engine_by_hash_epoch[(epoch, payload_hash)]) for epoch in (1, 2)
            )
            if released:
                gate_failures["stopped_isolation"].append(
                    f"stopped/{cell_id} payload appeared in an engine epoch ({released} events)"
                )

    for canary in canary_records:
        if canary["kind"] not in {"legacy_direct_port", "unreachable_ipv4"}:
            continue
        canary_id = str(canary["canary_id"])
        events = engine_by_hash_epoch[(1, canary["transport_payload_sha256"])]
        ingress = [event for event in events if event.get("event") == "ingress"]
        drops = [event for event in events if event.get("event") == "drop"]
        nonterminal = [
            event
            for event in events
            if event.get("event") in {"enqueue", "dequeue", "channel", "egress"}
        ]
        if len(ingress) != 1 or len(drops) != 1 or nonterminal:
            gate_failures["forbidden_paths"].append(
                f"{canary_id} ns3 ingress/drop cardinality is not 1/1/0: "
                f"ingress={len(ingress)} drop={len(drops)} nonterminal={len(nonterminal)}"
            )
            continue
        expected_identity = (
            17,
            canary["source_ip"],
            canary["destination_ip"],
            canary["source_udp_port"],
            canary["destination_udp_port"],
            canary["tos"],
            canary["transport_payload_sha256"],
            canary["transport_payload_size"],
        )
        for event in (*ingress, *drops):
            observed_identity = (
                event.get("transport_protocol"),
                event.get("source_ip"),
                event.get("destination_ip"),
                event.get("source_udp_port"),
                event.get("destination_udp_port"),
                event.get("tos"),
                event.get("transport_payload_sha256"),
                event.get("transport_payload_size"),
            )
            if observed_identity != expected_identity:
                gate_failures["forbidden_paths"].append(
                    f"{canary_id} ns3 event identity differs from declared bytes"
                )
        if canary["kind"] == "legacy_direct_port":
            if (
                drops[0].get("drop_reason")
                != "udp_destination_port_not_in_endpoint_matrix"
            ):
                gate_failures["forbidden_paths"].append(
                    f"{canary_id} ns3 terminal drop reason is not the endpoint-port allowlist"
                )
        elif drops[0].get("drop_reason") != "ipv4_no_route":
            gate_failures["forbidden_paths"].append(
                f"{canary_id} ns3 terminal drop reason is not explicit IPv4 no-route"
            )
        forbidden_ns3_metrics[canary_id] = {
            "ingress_count": 1,
            "drop_count": 1,
            "nonterminal_count": 0,
            "drop_reason": drops[0].get("drop_reason"),
        }

    for phase, epoch in (("positive", 1), ("recovery", 2)):
        for cell_id, cell in cells.items():
            offers_by_payload_hash: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for offered in offered_by_phase_cell[(phase, cell_id)]:
                offers_by_payload_hash[offered["transport_payload_sha256"]].append(
                    offered
                )
            for payload_hash, offers in offers_by_payload_hash.items():
                packet_events_by_uid: dict[int, list[dict[str, Any]]] = defaultdict(
                    list
                )
                invalid_uid_count = 0
                for record in engine_by_hash_epoch[(epoch, payload_hash)]:
                    packet_uid = record.get("packet_uid")
                    if (
                        isinstance(packet_uid, bool)
                        or not isinstance(packet_uid, int)
                        or packet_uid < 0
                    ):
                        invalid_uid_count += 1
                        continue
                    packet_events_by_uid[packet_uid].append(record)
                if (
                    invalid_uid_count
                    or len(packet_events_by_uid) != len(offers)
                ):
                    gate_failures["ns3_path"].append(
                        f"{phase}/{cell_id} ns3 UID occurrences differ: "
                        f"offers={len(offers)} uids={len(packet_events_by_uid)} "
                        f"invalid_uid_events={invalid_uid_count}"
                    )
                    continue
                expected_link = cell["ns3_path"]["directed_link_id"]
                expected_queue = cell["ns3_path"]["queue_id"]
                expected_size = offers[0]["transport_payload_size"]
                for packet_uid, packet_events in packet_events_by_uid.items():
                    by_stage = {
                        stage: [
                            record
                            for record in packet_events
                            if record.get("event") == stage
                        ]
                        for stage in (
                            "ingress",
                            "enqueue",
                            "dequeue",
                            "channel",
                            "egress",
                        )
                    }
                    if any(len(by_stage[stage]) != 1 for stage in by_stage):
                        gate_failures["ns3_path"].append(
                            f"{phase}/{cell_id}/uid={packet_uid} lacks exact one "
                            "ns3 stage: "
                            + str(
                                {
                                    stage: len(values)
                                    for stage, values in by_stage.items()
                                }
                            )
                        )
                        continue
                    if any(
                        record.get("transport_payload_size") != expected_size
                        or record.get("traffic_class") != cell["traffic_class"]
                        or record.get("tos") != cell["ns3_path"]["dscp_tos"]
                        or record.get("directed_link") != expected_link
                        or record.get("queue_id") != expected_queue
                        or record.get("source_ip") != cell["source"]["ip"]
                        or record.get("destination_ip") != cell["destination"]["ip"]
                        or record.get("source_udp_port") != cell["source"]["udp_port"]
                        or record.get("destination_udp_port")
                        != cell["destination"]["udp_port"]
                        for record in packet_events
                    ):
                        gate_failures["ns3_path"].append(
                            f"{phase}/{cell_id} ns3 decoded identity mismatch"
                        )
                    if (
                        by_stage["ingress"][0].get("device_id")
                        != cell["ns3_path"]["ingress_device_id"]
                    ):
                        gate_failures["ns3_path"].append(
                            f"{phase}/{cell_id} ingress device mismatch"
                        )
                    if (
                        by_stage["egress"][0].get("device_id")
                        != cell["ns3_path"]["egress_device_id"]
                    ):
                        gate_failures["ns3_path"].append(
                            f"{phase}/{cell_id} egress device mismatch"
                        )

    for key, offered in offered_p2mp_keys.items():
        packet_events = engine_by_hash_epoch[(1, offered["transport_payload_sha256"])]
        stages = {
            stage: [r for r in packet_events if r.get("event") == stage]
            for stage in ("ingress", "enqueue", "dequeue", "channel", "egress")
        }
        if (
            any(
                len(stages[stage]) != 1
                for stage in ("ingress", "enqueue", "dequeue", "channel")
            )
            or len(stages["egress"]) != 5
        ):
            gate_failures["p2mp"].append(
                f"P2MP root {key} stage counts are not 1/1/1/1/5: "
                + str({stage: len(values) for stage, values in stages.items()})
            )
            continue
        channel = stages["channel"][0]
        if (
            channel.get("root_transmission") is not True
            or channel.get("directed_link") != "cp>p2mp"
            or channel.get("queue_id") != "cp>p2mp.additional_data.q2"
            or channel.get("traffic_class") != "additional_data"
        ):
            gate_failures["p2mp"].append(f"P2MP root {key} service identity mismatch")
        if {r.get("device_id") for r in stages["egress"]} != {
            f"uav{index}.tap.egress" for index in range(1, 6)
        }:
            gate_failures["p2mp"].append(
                f"P2MP root {key} egress receiver set mismatch"
            )

    lifecycle_event_names: list[str] = []
    try:
        lifecycle = strict_jsonl(run_dir / "raw/lifecycle.jsonl")
        lifecycle_keys = {
            "schema",
            "run_id",
            "runtime_id",
            "run_nonce",
            "event_sequence",
            "monotonic_ns",
            "event",
            "details",
        }
        if any(set(record) != lifecycle_keys for record in lifecycle):
            gate_failures["lifecycle"].append("lifecycle record keys are not exact")
        if [r.get("event_sequence") for r in lifecycle] != list(
            range(1, len(lifecycle) + 1)
        ):
            gate_failures["lifecycle"].append("lifecycle sequence is not contiguous")
        if any(
            (r.get("schema"), r.get("run_id"), r.get("runtime_id"), r.get("run_nonce"))
            != (LIFECYCLE_SCHEMA, run_id, runtime_id, run_nonce)
            for r in lifecycle
        ):
            gate_failures["lifecycle"].append("lifecycle records cross identity/schema")
        lifecycle_times = [record.get("monotonic_ns") for record in lifecycle]
        lifecycle_times_valid = (
            all(isinstance(value, int) for value in lifecycle_times)
            and lifecycle_times == sorted(lifecycle_times)
            and len(lifecycle_times) == len(set(lifecycle_times))
        )
        if not lifecycle_times_valid or any(
            not isinstance(record.get("details"), dict) for record in lifecycle
        ):
            gate_failures["lifecycle"].append(
                "lifecycle timestamps/details are invalid or nonmonotonic"
            )
        event_names = [r.get("event") for r in lifecycle]
        lifecycle_event_names = [str(name) for name in event_names]
        required_order = [
            "run_initialized",
            "topology_ready",
            "captures_start_requested",
            "captures_started",
            "forbidden_listeners_start_requested",
            "forbidden_listeners_started",
            "endpoint_agents_start_requested",
            "endpoint_agents_started",
            "flight_stack_started",
            "actual_sitl_manifest_frozen",
            "actual_sitl_adapters_ready",
            "actual_control_start_requested",
            "actual_control_started",
            "engine_started",
            "engine_ready",
            "actual_control_link_ready",
            "schedule_committed",
            "forbidden_canaries_completed",
            "engine_stop_requested",
            "engine_stopped",
            "engine_restarted",
            "engine_ready",
            "actual_control_stop_requested",
            "endpoint_agents_stop_requested",
            "actual_control_stopped",
            "endpoint_agents_stopped",
            "forbidden_listeners_stop_requested",
            "forbidden_listeners_stopped",
            "engine_final_stop",
            "actual_sitl_stack_stop_requested",
            "actual_sitl_stack_stopped",
            "captures_stop_requested",
            "captures_stopped",
        ]
        if event_names != required_order:
            gate_failures["lifecycle"].append(
                "lifecycle event sequence is not exact/canonical"
            )
        lifecycle_by_event: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for record in lifecycle:
            lifecycle_by_event[str(record.get("event"))].append(record)
        exact_details = {
            "topology_ready": {
                "namespace_count": 7,
                "external_segment_count": 11,
                "root_tail_count": 5,
            },
            "captures_start_requested": {
                "capture_processes": 29,
                "tail_capture_processes": 10,
            },
            "captures_started": {
                "capture_processes": 29,
                "tail_capture_processes": 10,
            },
            "forbidden_listeners_start_requested": {
                "listener_processes": 6,
                "active_bindings": 20,
            },
            "forbidden_listeners_started": {
                "listener_processes": 6,
                "active_bindings": 20,
            },
            "endpoint_agents_start_requested": {"endpoint_agents": 6},
            "endpoint_agents_started": {"endpoint_agents": 6},
            "actual_sitl_manifest_frozen": {
                "channels": 5,
                "actual_sitl_processes": 10,
                "tail_segments": 5,
            },
            "actual_sitl_adapters_ready": {
                "adapter_processes": 5,
                "authorized_channels": 5,
                "tail_segments": 5,
            },
            "actual_control_start_requested": {
                "control_socket": "10.71.0.10:14600"
            },
            "actual_control_link_ready": {
                "uav_links": 5,
                "minimum_real_heartbeats_per_uav": 3,
            },
            "schedule_committed": {"windows": 4, "positive_cells": 30},
            "forbidden_canaries_completed": {
                "canary_count": 20,
                "remote_application_delivery": 0,
            },
            "engine_stop_requested": {"event_epoch": 1},
            "engine_stopped": {"event_epoch": 1, "exit_code": 0},
            "actual_control_stop_requested": {"actual_control_processes": 1},
            "endpoint_agents_stop_requested": {"endpoint_agents": 6},
            "actual_control_stopped": {
                "exit_code": 0,
                "actual_control_processes": 1,
            },
            "endpoint_agents_stopped": {"exit_code": 0, "endpoint_agents": 6},
            "forbidden_listeners_stop_requested": {"listener_processes": 6},
            "forbidden_listeners_stopped": {
                "exit_code": 0,
                "listener_processes": 6,
            },
            "engine_final_stop": {"event_epoch": 2, "exit_code": 0},
            "actual_sitl_stack_stop_requested": {
                "adapter_processes": 5,
                "supervisor_processes": 1,
                "flight_process_groups": 1,
            },
            "captures_stop_requested": {
                "capture_processes": 29,
                "tail_capture_processes": 10,
            },
            "captures_stopped": {
                "exit_code": 0,
                "capture_processes": 29,
                "tail_capture_processes": 10,
            },
        }
        if any(
            len(lifecycle_by_event[event]) != 1
            or lifecycle_by_event[event][0].get("details") != expected
            for event, expected in exact_details.items()
        ):
            gate_failures["lifecycle"].append(
                "lifecycle static event details are not exact"
            )
        transition_pairs = (
            ("captures_start_requested", "captures_started"),
            ("forbidden_listeners_start_requested", "forbidden_listeners_started"),
            ("endpoint_agents_start_requested", "endpoint_agents_started"),
            ("actual_control_start_requested", "actual_control_started"),
            ("actual_control_stop_requested", "actual_control_stopped"),
            ("endpoint_agents_stop_requested", "endpoint_agents_stopped"),
            ("forbidden_listeners_stop_requested", "forbidden_listeners_stopped"),
            ("actual_sitl_stack_stop_requested", "actual_sitl_stack_stopped"),
            ("captures_stop_requested", "captures_stopped"),
        )
        if any(
            len(lifecycle_by_event[requested]) != 1
            or len(lifecycle_by_event[completed]) != 1
            or not 0
            < lifecycle_by_event[completed][0]["monotonic_ns"]
            - lifecycle_by_event[requested][0]["monotonic_ns"]
            <= PROCESS_TRANSITION_MAX_NS
            for requested, completed in transition_pairs
        ):
            gate_failures["lifecycle"].append(
                "process lifecycle transition interval is invalid/unbounded"
            )
        run_initialized = lifecycle_by_event.get("run_initialized", [])
        run_details = (
            run_initialized[0].get("details")
            if len(run_initialized) == 1
            and isinstance(run_initialized[0].get("details"), dict)
            else {}
        )
        if (
            len(run_initialized) != 1
            or set(run_details) != {"runner_pid"}
            or not isinstance(run_details.get("runner_pid"), int)
            or run_details.get("runner_pid", 0) <= 0
        ):
            gate_failures["lifecycle"].append("run_initialized runner PID is not exact")
        flight_started = lifecycle_by_event.get("flight_stack_started", [])
        flight_details = (
            flight_started[0].get("details")
            if len(flight_started) == 1
            and isinstance(flight_started[0].get("details"), dict)
            else {}
        )
        if (
            set(flight_details)
            != {"launch_pid", "launch_pgid", "sitl_processes", "mavproxy_processes"}
            or flight_details.get("sitl_processes") != 5
            or flight_details.get("mavproxy_processes") != 5
            or not isinstance(flight_details.get("launch_pid"), int)
            or not isinstance(flight_details.get("launch_pgid"), int)
            or flight_details.get("launch_pid", 0) <= 1
            or flight_details.get("launch_pgid", 0) <= 1
        ):
            gate_failures["lifecycle"].append("five-UAV flight launch identity is invalid")
        actual_started = lifecycle_by_event.get("actual_control_started", [])
        actual_started_details = (
            actual_started[0].get("details")
            if len(actual_started) == 1
            and isinstance(actual_started[0].get("details"), dict)
            else {}
        )
        try:
            actual_socket_ready = strict_json(
                run_dir / "raw/state/actual-control.socket-ready.json"
            )
            if (
                set(actual_started_details) != {"pid", "control_socket"}
                or actual_started_details.get("pid") != actual_socket_ready.get("pid")
                or actual_started_details.get("control_socket") != "10.71.0.10:14600"
            ):
                gate_failures["lifecycle"].append(
                    "actual control lifecycle PID/socket differs from ready evidence"
                )
        except ValidationError as exc:
            gate_failures["lifecycle"].append(str(exc))
        stack_stopped = lifecycle_by_event.get("actual_sitl_stack_stopped", [])
        stack_details = (
            stack_stopped[0].get("details")
            if len(stack_stopped) == 1
            and isinstance(stack_stopped[0].get("details"), dict)
            else {}
        )
        if (
            set(stack_details)
            != {"adapter_exit_code", "supervisor_exit_code", "flight_exit_code"}
            or stack_details.get("adapter_exit_code") != 0
            or stack_details.get("supervisor_exit_code") != 0
            or stack_details.get("flight_exit_code") not in {0, 130, 143}
        ):
            gate_failures["lifecycle"].append(
                "actual-SITL stack terminal exit evidence differs"
            )
        for epoch, start_name in ((1, "engine_started"), (2, "engine_restarted")):
            starts = lifecycle_by_event.get(start_name, [])
            epoch_ready = [
                record
                for record in lifecycle_by_event.get("engine_ready", [])
                if isinstance(record.get("details"), dict)
                and record["details"].get("event_epoch") == epoch
            ]
            start_details = (
                starts[0].get("details")
                if len(starts) == 1 and isinstance(starts[0].get("details"), dict)
                else {}
            )
            ready_details = (
                epoch_ready[0].get("details")
                if len(epoch_ready) == 1
                and isinstance(epoch_ready[0].get("details"), dict)
                else {}
            )
            if (
                len(starts) != 1
                or len(epoch_ready) != 1
                or set(start_details) != {"event_epoch", "pid"}
                or set(ready_details) != {"event_epoch", "pid"}
                or start_details.get("event_epoch") != epoch
                or not isinstance(start_details.get("pid"), int)
                or start_details.get("pid", 0) <= 0
                or ready_details != start_details
            ):
                gate_failures["lifecycle"].append(
                    f"epoch {epoch} lifecycle PID/readiness identity is invalid"
                )
        if event_names == required_order and lifecycle_times_valid:
            schedule_time = lifecycle_by_event["schedule_committed"][0]["monotonic_ns"]
            canary_complete_time = lifecycle_by_event["forbidden_canaries_completed"][
                0
            ]["monotonic_ns"]
            if any(
                not schedule_time
                <= observation.get("sent_monotonic_ns", -1)
                <= canary_complete_time
                for observation in canary_observations.values()
            ):
                gate_failures["lifecycle"].append(
                    "forbidden canary sends are outside lifecycle boundaries"
                )
            listener_start_time = lifecycle_by_event["forbidden_listeners_started"][0][
                "monotonic_ns"
            ]
            listener_stop_time = lifecycle_by_event["forbidden_listeners_stopped"][0][
                "monotonic_ns"
            ]
            if len(listener_windows) != len(ENDPOINTS) or any(
                not ready_event_time
                <= ready_file_time
                <= listener_start_time
                < listener_stop_time
                or shutdown_event_time > listener_stop_time
                for ready_event_time, shutdown_event_time, ready_file_time in listener_windows.values()
            ):
                gate_failures["lifecycle"].append(
                    "forbidden listener evidence is outside lifecycle boundaries"
                )
        ready = [r for r in lifecycle if r.get("event") == "engine_ready"]
        stopped = [r for r in lifecycle if r.get("event") == "engine_stopped"]
        restarted = [r for r in lifecycle if r.get("event") == "engine_restarted"]
        stop_requested = [
            r for r in lifecycle if r.get("event") == "engine_stop_requested"
        ]
        if (
            len(ready) != 2
            or len(stopped) != 1
            or len(restarted) != 1
            or len(stop_requested) != 1
        ):
            gate_failures["lifecycle"].append("engine lifecycle cardinality mismatch")
        elif windows:
            if (
                ready[0]["monotonic_ns"]
                > windows["positive"]["start_monotonic_ns"] - 10_000_000_000
            ):
                gate_failures["lifecycle"].append(
                    "epoch1 readiness was not stable for 10 seconds"
                )
            if not (
                windows["p2mp"]["end_monotonic_ns"]
                <= stop_requested[0]["monotonic_ns"]
                < stopped[0]["monotonic_ns"]
                < windows["stopped"]["start_monotonic_ns"]
            ):
                gate_failures["lifecycle"].append(
                    "stop transition is outside declared drain interval"
                )
            if not (
                windows["stopped"]["end_monotonic_ns"]
                < restarted[0]["monotonic_ns"]
                <= ready[1]["monotonic_ns"]
                <= windows["recovery"]["start_monotonic_ns"] - 10_000_000_000
            ):
                gate_failures["lifecycle"].append(
                    "restart/readiness transition is outside declared interval"
                )
            if stopped[0].get("details", {}).get("exit_code") != 0:
                gate_failures["lifecycle"].append("stopped engine did not exit cleanly")
    except ValidationError as exc:
        gate_failures["lifecycle"].append(str(exc))

    expected_pcaps = [
        *(run_dir / f"pcap/endpoint-{endpoint}.pcap" for endpoint in ENDPOINTS),
        *(run_dir / f"pcap/ns3-external-{endpoint}.pcap" for endpoint in ENDPOINTS),
        *(run_dir / f"pcap/loopback-{endpoint}.pcap" for endpoint in ENDPOINTS),
        *(run_dir / f"pcap/tail-uav{index}.pcap" for index in range(1, 6)),
        *(run_dir / f"pcap/tail-root-uav{index}.pcap" for index in range(1, 6)),
        run_dir / "pcap/loopback-container-root.pcap",
        *(
            run_dir / f"pcap/ns3-epoch{epoch}-radio-{device}.pcap"
            for epoch in (1, 2)
            for device in ENDPOINTS
        ),
    ]
    pcap_counts: dict[str, int] = {}
    pcap_records: dict[str, list[dict[str, Any]]] = {}
    for path in expected_pcaps:
        count, decoded_records, errors = parse_pcap(path)
        pcap_counts[path.name] = count
        pcap_records[path.name] = decoded_records
        gate_failures["captures"].extend(errors)
    capture_stats: dict[str, Any] = {}
    stats_keys = {
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
    capture_specs = [
        *((f"endpoint-{endpoint}", "eth0", CAPTURE_PACKET_FILTER) for endpoint in ENDPOINTS),
        *(
            (f"ns3-external-{endpoint}", f"vp-{endpoint}", CAPTURE_PACKET_FILTER)
            for endpoint in ENDPOINTS
        ),
        *((f"loopback-{endpoint}", "lo", CAPTURE_PACKET_FILTER) for endpoint in ENDPOINTS),
        *((f"tail-uav{index}", "tail0", CAPTURE_PACKET_FILTER) for index in range(1, 6)),
        *(
            (f"tail-root-uav{index}", f"ams-tail{index}", CAPTURE_PACKET_FILTER)
            for index in range(1, 6)
        ),
        (
            "loopback-container-root",
            "lo",
            expected_root_loopback_packet_filter(str(run_nonce)),
        ),
    ]
    for name, expected_interface, expected_packet_filter in capture_specs:
        stats_path = run_dir / f"logs/capture-{name}.json"
        stderr_path = run_dir / f"logs/capture-{name}.stderr"
        try:
            stats = strict_json(stats_path)
            pcap_path = run_dir / f"pcap/{name}.pcap"
            if set(stats) != stats_keys:
                raise ValidationError(f"{stats_path.name} keys are not exact")
            if (
                stats.get("contract") != CAPTURE_STATS_CONTRACT
                or stats.get("interface") != expected_interface
                or stats.get("capture_protocol") != CAPTURE_PROTOCOL
                or stats.get("packet_filter") != expected_packet_filter
                or stats.get("pcap_path") != pcap_path.name
                or type(stats.get("pcap_bytes")) is not int
                or stats.get("pcap_bytes") != pcap_path.stat().st_size
                or type(stats.get("linktype")) is not int
                or stats.get("linktype") != 1
                or type(stats.get("snaplen")) is not int
                or stats.get("snaplen") != 65_535
                or type(stats.get("receive_buffer_requested_bytes")) is not int
                or stats.get("receive_buffer_requested_bytes")
                != CAPTURE_RECEIVE_BUFFER_REQUESTED_BYTES
                or type(stats.get("receive_buffer_effective_bytes")) is not int
                or stats.get("receive_buffer_effective_bytes")
                != CAPTURE_RECEIVE_BUFFER_EFFECTIVE_BYTES
                or stats.get("receive_buffer_setter")
                not in CAPTURE_RECEIVE_BUFFER_SETTERS
                or type(stats.get("drain_batch_packet_limit")) is not int
                or stats.get("drain_batch_packet_limit")
                != CAPTURE_DRAIN_BATCH_PACKET_LIMIT
                or type(stats.get("drain_batch_byte_limit")) is not int
                or stats.get("drain_batch_byte_limit")
                != CAPTURE_DRAIN_BATCH_BYTE_LIMIT
                or stats.get("stop_signal") != "SIGINT"
                or type(stats.get("started_monotonic_ns")) is not int
                or type(stats.get("stopped_monotonic_ns")) is not int
                or stats["stopped_monotonic_ns"] <= stats["started_monotonic_ns"]
                or type(stats.get("packets_written")) is not int
                or stats.get("packets_written") != pcap_counts[pcap_path.name]
                or type(stats.get("packets_received_kernel")) is not int
                or stats["packets_received_kernel"] < stats["packets_written"]
                or type(stats.get("packets_dropped_kernel")) is not int
                or stats.get("packets_dropped_kernel") != 0
            ):
                raise ValidationError(
                    f"{stats_path.name} identity/count/drop accounting mismatch"
                )
            if not regular_file(stderr_path) or stderr_path.stat().st_size != 0:
                raise ValidationError(f"{stderr_path.name} is absent/nonempty")
            capture_stats[name] = stats
        except (OSError, TypeError, ValidationError) as exc:
            gate_failures["captures"].append(str(exc))
    details["captures"] = {
        "packet_counts": pcap_counts,
        "decoded_udp_counts": {
            name: len(records) for name, records in sorted(pcap_records.items())
        },
        "raw_capture_stats": capture_stats,
    }

    pcap_indexes: dict[
        str,
        dict[tuple[str, str, str, int, int, int, int], list[dict[str, Any]]],
    ] = {}
    for name, records in pcap_records.items():
        index: dict[tuple[str, str, str, int, int, int, int], list[dict[str, Any]]] = (
            defaultdict(list)
        )
        for record in records:
            key = (
                record["transport_payload_sha256"],
                record["source_ip"],
                record["destination_ip"],
                record["source_udp_port"],
                record["destination_udp_port"],
                record["tos"],
                record["transport_payload_size"],
            )
            index[key].append(record)
        pcap_indexes[name] = index

    def capture_key(
        payload_record: dict[str, Any], cell: dict[str, Any] | None
    ) -> tuple[str, str, str, int, int, int, int]:
        if cell is None:
            return (
                payload_record["transport_payload_sha256"],
                "10.71.0.10",
                P2MP_GROUP,
                14800,
                P2MP_PORT,
                TOS_BY_CLASS["additional_data"],
                payload_record["transport_payload_size"],
            )
        return (
            payload_record["transport_payload_sha256"],
            cell["source"]["ip"],
            cell["destination"]["ip"],
            cell["source"]["udp_port"],
            cell["destination"]["udp_port"],
            cell["ns3_path"]["dscp_tos"],
            payload_record["transport_payload_size"],
        )

    def role_evidence(
        capture_name: str,
        payload_records: list[dict[str, Any]],
        cell: dict[str, Any] | None,
    ) -> tuple[set[str], list[str]]:
        matched_payloads: set[str] = set()
        wire_hashes: set[str] = set()
        index = pcap_indexes.get(capture_name, {})
        for payload_record in payload_records:
            key = capture_key(payload_record, cell)
            matches = index.get(key, [])
            if matches:
                matched_payloads.add(key[0])
                wire_hashes.update(str(match["wire_frame_sha256"]) for match in matches)
        return matched_payloads, sorted(wire_hashes)

    metrics["pcap_transport"] = {
        phase: {} for phase in ("positive", "stopped", "recovery")
    }
    external_capture_names = {
        f"{prefix}-{endpoint}.pcap"
        for prefix in ("endpoint", "ns3-external")
        for endpoint in ENDPOINTS
    }
    tail_capture_names = {
        *(f"tail-uav{index}.pcap" for index in range(1, 6)),
        *(f"tail-root-uav{index}.pcap" for index in range(1, 6)),
    }

    def tail_role_evidence(
        capture_name: str,
        payload_records: list[dict[str, Any]],
        *,
        uav: int,
        direction: str,
    ) -> tuple[set[str], list[str]]:
        expected_hashes = {
            str(record["transport_payload_sha256"]): int(record["transport_payload_size"])
            for record in payload_records
        }
        matched: set[str] = set()
        wire: set[str] = set()
        for record in pcap_records.get(capture_name, []):
            payload_hash = str(record.get("transport_payload_sha256"))
            if payload_hash not in expected_hashes or record.get("transport_payload_size") != expected_hashes[payload_hash]:
                continue
            endpoints = (record.get("source_ip"), record.get("destination_ip"))
            expected_endpoints = (
                (f"10.72.{uav}.2", f"10.72.{uav}.1")
                if direction == "downlink"
                else (f"10.72.{uav}.1", f"10.72.{uav}.2")
            )
            if endpoints == expected_endpoints:
                matched.add(payload_hash)
                wire.add(str(record["wire_frame_sha256"]))
        return matched, sorted(wire)
    for phase in ("positive", "stopped", "recovery"):
        for cell_id, cell in sorted(cells.items()):
            offered = offered_by_phase_cell[(phase, cell_id)]
            received = received_by_phase_cell[(phase, cell_id)]
            source_endpoint = (
                "gcs"
                if cell["source"]["namespace"] == "ams-gcs"
                else cell["uav"]["name"]
            )
            destination_endpoint = (
                "gcs"
                if cell["destination"]["namespace"] == "ams-gcs"
                else cell["uav"]["name"]
            )
            role_inputs = {
                "source_before_adapter": (f"endpoint-{source_endpoint}.pcap", offered),
                "ns3_external_ingress": (
                    f"ns3-external-{source_endpoint}.pcap",
                    offered,
                ),
                "ns3_external_egress": (
                    f"ns3-external-{destination_endpoint}.pcap",
                    received,
                ),
                "remote_after_adapter": (
                    f"endpoint-{destination_endpoint}.pcap",
                    received,
                ),
            }
            role_metrics: dict[str, Any] = {}
            offered_hashes = {
                str(record["transport_payload_sha256"]) for record in offered
            }
            received_hashes = {
                str(record["transport_payload_sha256"]) for record in received
            }
            for role, (capture_name, expected_records) in role_inputs.items():
                matched, wire_hashes = role_evidence(
                    capture_name, expected_records, cell
                )
                expected_hashes = {
                    str(record["transport_payload_sha256"])
                    for record in expected_records
                }
                if matched != expected_hashes:
                    gate_failures["pcap_transport"].append(
                        f"{phase}/{cell_id}/{role} decoded payload set differs: "
                        f"missing={len(expected_hashes - matched)} extra={len(matched - expected_hashes)}"
                    )
                role_metrics[role] = {
                    "capture": capture_name,
                    "expected_unique": len(expected_hashes),
                    "decoded_unique": len(matched),
                    "wire_frame_sha256": wire_hashes,
                }
            downstream_names = {
                f"ns3-external-{destination_endpoint}.pcap",
                f"endpoint-{destination_endpoint}.pcap",
            }
            if phase == "stopped":
                for capture_name in downstream_names:
                    forbidden, _ = role_evidence(capture_name, offered, cell)
                    if forbidden:
                        gate_failures["pcap_transport"].append(
                            f"stopped/{cell_id} has {len(forbidden)} matching payloads at {capture_name}"
                        )
            permitted_names = {
                f"endpoint-{source_endpoint}.pcap",
                f"ns3-external-{source_endpoint}.pcap",
                *downstream_names,
            }
            for capture_name in sorted(external_capture_names - permitted_names):
                leaked, _ = role_evidence(capture_name, offered, cell)
                if leaked:
                    gate_failures["pcap_transport"].append(
                        f"{phase}/{cell_id} payload appeared on unrelated capture {capture_name}"
                    )
            role_metrics["adjacent_unique_counts"] = {
                "offered_endpoint_events": len(offered_hashes),
                "source_capture": role_metrics["source_before_adapter"][
                    "decoded_unique"
                ],
                "ns3_ingress_capture": role_metrics["ns3_external_ingress"][
                    "decoded_unique"
                ],
                "ns3_egress_capture": role_metrics["ns3_external_egress"][
                    "decoded_unique"
                ],
                "remote_capture": role_metrics["remote_after_adapter"][
                    "decoded_unique"
                ],
                "received_endpoint_events": len(received_hashes),
            }
            if cell.get("traffic_class") == "control":
                uav_index = int(cell["uav"]["system_id"])
                tail_expected = received if cell["direction"] == "downlink" else offered
                expected_tail_hashes = {
                    str(record["transport_payload_sha256"]) for record in tail_expected
                }
                tail_metrics: dict[str, Any] = {}
                for capture_name in (
                    f"tail-uav{uav_index}.pcap",
                    f"tail-root-uav{uav_index}.pcap",
                ):
                    matched, wire_hashes = tail_role_evidence(
                        capture_name,
                        tail_expected,
                        uav=uav_index,
                        direction=str(cell["direction"]),
                    )
                    if matched != expected_tail_hashes:
                        gate_failures["pcap_transport"].append(
                            f"{phase}/{cell_id}/actual_tail/{capture_name} decoded payload set differs: "
                            f"missing={len(expected_tail_hashes - matched)}"
                        )
                    tail_metrics[capture_name] = {
                        "expected_unique": len(expected_tail_hashes),
                        "decoded_unique": len(matched),
                        "wire_frame_sha256": wire_hashes,
                    }
                role_metrics["actual_mavproxy_tail"] = tail_metrics
                unrelated_tail_names = tail_capture_names - {
                    f"tail-uav{uav_index}.pcap",
                    f"tail-root-uav{uav_index}.pcap",
                }
                offered_tail_hashes = {
                    str(record["transport_payload_sha256"]) for record in offered
                }
                for capture_name in unrelated_tail_names:
                    if any(
                        record.get("transport_payload_sha256") in offered_tail_hashes
                        for record in pcap_records.get(capture_name, [])
                    ):
                        gate_failures["pcap_transport"].append(
                            f"{phase}/{cell_id} control payload leaked to {capture_name}"
                        )
            metrics["pcap_transport"][phase][cell_id] = role_metrics

    p2mp_role_metrics: dict[str, Any] = {}
    for role, capture_name, payload_records in (
        ("source_before_adapter", "endpoint-gcs.pcap", p2mp_offered),
        ("ns3_external_ingress", "ns3-external-gcs.pcap", p2mp_offered),
    ):
        matched, wire_hashes = role_evidence(capture_name, payload_records, None)
        expected = {
            str(record["transport_payload_sha256"]) for record in payload_records
        }
        if matched != expected:
            gate_failures["pcap_transport"].append(
                f"p2mp/{role} decoded payload set differs: missing={len(expected - matched)}"
            )
        p2mp_role_metrics[role] = {
            "capture": capture_name,
            "expected_unique": len(expected),
            "decoded_unique": len(matched),
            "wire_frame_sha256": wire_hashes,
        }
    for endpoint in ENDPOINTS[1:]:
        legs = p2mp_received_by_endpoint[endpoint]
        for role, capture_name in (
            ("ns3_external_egress", f"ns3-external-{endpoint}.pcap"),
            ("remote_after_adapter", f"endpoint-{endpoint}.pcap"),
        ):
            matched, wire_hashes = role_evidence(capture_name, legs, None)
            expected = {str(record["transport_payload_sha256"]) for record in legs}
            if matched != expected:
                gate_failures["pcap_transport"].append(
                    f"p2mp/{endpoint}/{role} decoded payload set differs: missing={len(expected - matched)}"
                )
            p2mp_role_metrics[f"{endpoint}.{role}"] = {
                "capture": capture_name,
                "expected_unique": len(expected),
                "decoded_unique": len(matched),
                "wire_frame_sha256": wire_hashes,
            }
    metrics["pcap_transport"]["p2mp"] = p2mp_role_metrics

    forbidden_metrics: dict[str, Any] = {}
    known_capture_names = {
        *(f"endpoint-{endpoint}.pcap" for endpoint in ENDPOINTS),
        *(f"ns3-external-{endpoint}.pcap" for endpoint in ENDPOINTS),
        *(f"loopback-{endpoint}.pcap" for endpoint in ENDPOINTS),
        *(f"tail-uav{index}.pcap" for index in range(1, 6)),
        *(f"tail-root-uav{index}.pcap" for index in range(1, 6)),
        "loopback-container-root.pcap",
    }
    for canary in canary_records:
        canary_id = str(canary["canary_id"])
        expected_names = set(canary["expected_capture_names"])
        forbidden_names = set(canary["forbidden_capture_names"])
        if expected_names | forbidden_names != known_capture_names or (
            expected_names & forbidden_names
        ):
            gate_failures["forbidden_paths"].append(
                f"{canary_id} capture partition is not exact"
            )
            continue

        def exact_canary_matches(capture_name: str) -> list[dict[str, Any]]:
            return [
                record
                for record in pcap_records[capture_name]
                if (
                    record.get("address_family"),
                    record.get("source_ip"),
                    record.get("destination_ip"),
                    record.get("source_udp_port"),
                    record.get("destination_udp_port"),
                    record.get("tos"),
                    record.get("transport_payload_sha256"),
                    record.get("transport_payload_size"),
                )
                == (
                    canary["address_family"],
                    canary["source_ip"],
                    canary["destination_ip"],
                    canary["source_udp_port"],
                    canary["destination_udp_port"],
                    canary["tos"],
                    canary["transport_payload_sha256"],
                    canary["transport_payload_size"],
                )
            ]

        expected_counts = {
            name: len(exact_canary_matches(name)) for name in sorted(expected_names)
        }
        missing = [name for name, count in expected_counts.items() if count < 1]
        if missing:
            gate_failures["forbidden_paths"].append(
                f"{canary_id} is absent from expected captures: {missing}"
            )
        leaked: dict[str, int] = {}
        for name in sorted(forbidden_names):
            hash_matches = sum(
                record.get("transport_payload_sha256")
                == canary["transport_payload_sha256"]
                for record in pcap_records[name]
            )
            if hash_matches:
                leaked[name] = hash_matches
        if leaked:
            gate_failures["forbidden_paths"].append(
                f"{canary_id} appeared on forbidden captures: {leaked}"
            )
        observation = canary_observations.get(canary_id)
        sent_monotonic_ns = (
            observation.get("sent_monotonic_ns")
            if isinstance(observation, dict)
            else None
        )
        if not isinstance(sent_monotonic_ns, int):
            gate_failures["forbidden_paths"].append(
                f"{canary_id} has no valid send timestamp for capture correlation"
            )
        else:
            for capture_name in sorted(expected_names):
                stats = capture_stats.get(capture_name.removesuffix(".pcap"))
                started = (
                    stats.get("started_monotonic_ns")
                    if isinstance(stats, dict)
                    else None
                )
                stopped = (
                    stats.get("stopped_monotonic_ns")
                    if isinstance(stats, dict)
                    else None
                )
                if (
                    not isinstance(started, int)
                    or not isinstance(stopped, int)
                    or not started <= sent_monotonic_ns <= stopped
                ):
                    gate_failures["forbidden_paths"].append(
                        f"{canary_id} send time is outside capture interval: {capture_name}"
                    )
        forbidden_metrics[canary_id] = {
            "kind": canary["kind"],
            "expected_capture_packet_counts": expected_counts,
            "forbidden_capture_packet_count": sum(leaked.values()),
            "remote_application_delivery": 0,
            "listener_endpoint": canary["listener_endpoint"],
            **(
                {"ns3_terminal_event": forbidden_ns3_metrics[canary_id]}
                if canary_id in forbidden_ns3_metrics
                else {}
            ),
        }

    canary_hashes = {
        str(canary["transport_payload_sha256"]) for canary in canary_records
    }
    application_deliveries = [
        record
        for record in endpoint_records
        if record.get("event") == "remote_receive"
        and record.get("transport_payload_sha256") in canary_hashes
    ]
    if application_deliveries:
        gate_failures["forbidden_paths"].append(
            f"forbidden canaries reached endpoint application events: {len(application_deliveries)}"
        )
    if len(forbidden_metrics) != 20:
        gate_failures["forbidden_paths"].append(
            f"forbidden canary metric count is {len(forbidden_metrics)}, expected 20"
        )
    metrics["forbidden_paths"] = forbidden_metrics
    details["forbidden_paths"] = {
        "declared_canary_count": len(canary_records),
        "observed_canary_count": len(forbidden_metrics),
        "application_delivery_count": len(application_deliveries),
    }

    topology_dir = run_dir / "raw/topology"
    expected_names = ("container-root", "ams-ns3", *NAMESPACES.values())
    for namespace in expected_names:
        for kind in ("link", "addr", "route"):
            path = topology_dir / f"{namespace}.{kind}.json"
            try:
                value = strict_json(path)
                if not isinstance(value, list):
                    gate_failures["topology_isolation"].append(
                        f"{path.name} is not a raw iproute array"
                    )
            except ValidationError as exc:
                gate_failures["topology_isolation"].append(str(exc))
    try:
        for endpoint, namespace in NAMESPACES.items():
            links = strict_json(topology_dir / f"{namespace}.link.json")
            addresses = strict_json(topology_dir / f"{namespace}.addr.json")
            routes = strict_json(topology_dir / f"{namespace}.route.json")
            expected_links = (
                {"lo", "eth0"}
                if endpoint == "gcs"
                else {"lo", "eth0", "tail0"}
            )
            if {link.get("ifname") for link in links} != expected_links:
                gate_failures["topology_isolation"].append(
                    f"{namespace} has undeclared interfaces"
                )
            eth0 = next((link for link in links if link.get("ifname") == "eth0"), {})
            index = 0 if endpoint == "gcs" else int(endpoint[3:])
            expected_mac = f"02:71:{index:02x}:00:10:10"
            if eth0.get("address") != expected_mac or eth0.get("operstate") not in {
                "UP",
                "UNKNOWN",
            }:
                gate_failures["topology_isolation"].append(
                    f"{namespace} eth0 MAC/state mismatch"
                )
            expected_ip = f"10.71.{index}.10"
            ipv4 = [
                info
                for item in addresses
                if item.get("ifname") == "eth0"
                for info in item.get("addr_info", [])
                if info.get("family") == "inet"
            ]
            if [(item.get("local"), item.get("prefixlen")) for item in ipv4] != [
                (expected_ip, 24)
            ]:
                gate_failures["topology_isolation"].append(
                    f"{namespace} IPv4 assignment mismatch"
                )
            if endpoint != "gcs":
                tail_ipv4 = [
                    info
                    for item in addresses
                    if item.get("ifname") == "tail0"
                    for info in item.get("addr_info", [])
                    if info.get("family") == "inet"
                ]
                if [
                    (item.get("local"), item.get("prefixlen")) for item in tail_ipv4
                ] != [(f"10.72.{index}.2", 30)]:
                    gate_failures["topology_isolation"].append(
                        f"{namespace} actual tail IPv4 assignment mismatch"
                    )
            defaults = [route for route in routes if route.get("dst") == "default"]
            if (
                len(defaults) != 1
                or defaults[0].get("gateway") != f"10.71.{index}.1"
                or defaults[0].get("dev") != "eth0"
            ):
                gate_failures["topology_isolation"].append(
                    f"{namespace} default route mismatch"
                )
        root_links = strict_json(topology_dir / "container-root.link.json")
        root_addresses = strict_json(topology_dir / "container-root.addr.json")
        root_routes = strict_json(topology_dir / "container-root.route.json")
        expected_root_links = {"lo", *(f"ams-tail{index}" for index in range(1, 6))}
        if {link.get("ifname") for link in root_links} != expected_root_links:
            gate_failures["topology_isolation"].append(
                "container-root tail interface set differs"
            )
        root_ipv4 = {
            (item.get("ifname"), info.get("local"), info.get("prefixlen"))
            for item in root_addresses
            if item.get("ifname") != "lo"
            for info in item.get("addr_info", [])
            if info.get("family") == "inet"
        }
        if root_ipv4 != {
            (f"ams-tail{index}", f"10.72.{index}.1", 30)
            for index in range(1, 6)
        }:
            gate_failures["topology_isolation"].append(
                "container-root tail address set differs"
            )
        if any(route.get("dst") == "default" for route in root_routes):
            gate_failures["topology_isolation"].append(
                "container-root has an undeclared default route"
            )
        ns3_links = strict_json(topology_dir / "ams-ns3.link.json")
        ns3_names = {link.get("ifname") for link in ns3_links}
        expected_ns3 = (
            {"lo"}
            | {f"br-{endpoint}" for endpoint in ENDPOINTS}
            | {f"tap-{endpoint}" for endpoint in ENDPOINTS}
            | {f"vp-{endpoint}" for endpoint in ENDPOINTS}
        )
        if ns3_names != expected_ns3:
            gate_failures["topology_isolation"].append(
                f"ams-ns3 interface set differs: missing={sorted(expected_ns3 - ns3_names)} extra={sorted(ns3_names - expected_ns3)}"
            )
        ns3_addresses = strict_json(topology_dir / "ams-ns3.addr.json")
        nonlocal_addresses = [
            info
            for link in ns3_addresses
            if link.get("ifname") != "lo"
            for info in link.get("addr_info", [])
            if not (
                info.get("family") == "inet6"
                and info.get("scope") == "link"
                and str(info.get("local", "")).lower().startswith("fe80:")
            )
        ]
        if nonlocal_addresses:
            gate_failures["topology_isolation"].append(
                "ams-ns3 Linux namespace has a non-link-local IP address/bypass"
            )
        ns3_routes = strict_json(topology_dir / "ams-ns3.route.json")
        bypass_routes = [
            route
            for route in ns3_routes
            if route.get("dev") != "lo"
            and not (
                route.get("protocol") == "kernel"
                and route.get("gateway") is None
                and route.get("dst") in {"fe80::/64", "ff00::/8"}
            )
        ]
        if bypass_routes:
            gate_failures["topology_isolation"].append(
                "ams-ns3 Linux namespace has a routed IP bypass"
            )
    except (ValidationError, StopIteration, TypeError) as exc:
        gate_failures["topology_isolation"].append(str(exc))

    continuous_failures, continuous_details = validate_continuous_topology(
        run_dir,
        run_id=run_id,
        runtime_id=runtime_id,
        run_nonce=run_nonce,
        lifecycle_events=lifecycle_event_names,
        windows=windows,
    )
    gate_failures["continuous_topology"].extend(continuous_failures)
    details["continuous_topology"] = continuous_details

    all_gate_names = (
        "run_identity",
        "matrix_contract",
        "m2_extension",
        "ns3_build_receipt",
        "phase_contract",
        "actual_sitl_control",
        "topology_isolation",
        "continuous_topology",
        "endpoint_lifecycle",
        "decoded_endpoint_matrix",
        "positive_matrix",
        "stopped_isolation",
        "ns3_path",
        "p2mp",
        "lifecycle",
        "captures",
        "pcap_transport",
        "forbidden_paths",
    )
    gates = {
        name: gate(gate_failures[name], details.get(name)) for name in all_gate_names
    }
    failures = [
        f"{name}: {failure}"
        for name in all_gate_names
        for failure in gate_failures[name]
    ]
    matrix_sha256 = run.get("matrix", {}).get("sha256") if isinstance(run.get("matrix"), dict) else None
    actual_control_api = {
        "contract": "ams.m3.actual-control-api/v1",
        "control_endpoint_form": ACTUAL_CONTROL_ENDPOINT_FORM,
        "matrix_path": "network/config/endpoint_matrix_5uav.json",
        "matrix_sha256": matrix_sha256,
        "endpoint_schema_path": "network/config/endpoint_transaction_schema.json",
        "endpoint_schema_sha256": sha256_file(
            ROOT / "network/config/endpoint_transaction_schema.json"
        ),
        "event_schema": "ams.actual-sitl.control-event/v1",
        "probe_source": {
            "path": "network/scripts/actual_sitl_control_probe.py",
            "sha256": sha256_file(ROOT / "network/scripts/actual_sitl_control_probe.py"),
        },
        "adapter_source": {
            "path": "network/bridge/actual_sitl_mavlink_endpoint.py",
            "sha256": sha256_file(
                ROOT / "network/bridge/actual_sitl_mavlink_endpoint.py"
            ),
        },
        "relay_core_source": {
            "path": "network/bridge/opaque_udp_relay.py",
            "sha256": sha256_file(ROOT / "network/bridge/opaque_udp_relay.py"),
        },
        "process_role_ids": {
            "gcs": "gcs_control_probe",
            "adapters": {
                f"uav{index}": f"uav_control_adapter_uav{index}"
                for index in range(1, 6)
            },
            "supervisor": "actual_endpoint_supervisor",
        },
        "profile_contracts": {
            "m3": {
                "run_contract": RUN_CONTRACT,
                "run_nonce_hex_length": 32,
                "transport_nonce32_derivation": "identity/full_run_nonce32",
            },
            "m4_capacity": {
                "run_contract": "ams.m4.capacity_run/v3",
                "run_nonce_hex_length": 64,
                "transport_nonce32_derivation": "sha256(raw_full_run_nonce64)[:32]",
            },
            "m4_causality": {
                "run_contract": "ams.m4.causality_run/v2",
                "run_nonce_hex_length": 64,
                "transport_nonce32_derivation": "sha256(raw_full_run_nonce64)[:32]",
            },
        },
        "m4_window_command": {
            "contract": "ams.actual-sitl.control-window-command/v1",
            "action": "window",
            "exact_keys": [
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
            ],
            "endpoint": "actual-control",
            "per_uav_keys": [
                "uav1",
                "uav2",
                "uav3",
                "uav4",
                "uav5",
            ],
            "response_policy_values": [
                "ack_required",
                "timeout_required",
            ],
            "send_slot_formula": (
                "start_monotonic_ns + "
                "((ordinal_send_slot - 1) * send_span_ms * 1000000) // "
                "max(1, offered_per_uav - 1)"
            ),
            "single_pending_transaction_per_uav": True,
            "timeout_ns": 3_000_000_000,
            "guard_scope": "per_uav_active_timeout_batch_with_append_only_history",
        },
        "channels": {
            f"uav{index}": {
                "system_id": index,
                "instance": index - 1,
                "radio_bind": {"host": f"10.71.{index}.10", "port": 14600 + index},
                "gcs_peer": {"host": "10.71.0.10", "port": 14600},
                "tail_root": {"host": f"10.72.{index}.1", "prefixlen": 30},
                "tail_uav": {
                    "host": f"10.72.{index}.2",
                    "prefixlen": 30,
                    "port": 14559 + index,
                },
                "master": {"host": "127.0.0.1", "port": 5760 + 10 * (index - 1)},
                "tail_pcap_roles": {
                    "root": f"tail-root-uav{index}",
                    "uav": f"tail-uav{index}",
                },
                "matrix": {
                    "downlink_cell_id": f"uav{index}.control.downlink",
                    "downlink_directed_link_id": f"cp>uav{index}",
                    "uplink_cell_id": f"uav{index}.control.uplink",
                    "uplink_directed_link_id": f"uav{index}>cp",
                },
            }
            for index in range(1, 6)
        },
        "capture_role_mapping": {
            "downlink": [
                "endpoint-gcs",
                "ns3-external-gcs",
                "ns3-external-uavN",
                "endpoint-uavN",
                "tail-uavN",
                "tail-root-uavN",
            ],
            "uplink": [
                "tail-root-uavN",
                "tail-uavN",
                "endpoint-uavN",
                "ns3-external-uavN",
                "ns3-external-gcs",
                "endpoint-gcs",
            ],
        },
    }
    return {
        "contract": RESULT_CONTRACT,
        "run_id": run_id,
        "runtime_id": runtime_id,
        "execution_mode": execution.get("mode") if isinstance(execution, dict) else None,
        "acceptance_eligible": formal_execution,
        "passed": not failures,
        "gates": gates,
        "shared_core_identity": shared_core_identity,
        "actual_control_api": actual_control_api,
        "metrics": metrics,
        "failures": failures,
    }


def write_new(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o664)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    parser.add_argument("--m2-receipt", type=Path, default=DEFAULT_M2_RECEIPT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--technical-smoke", action="store_true")
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)
    run_dir = args.run_dir.resolve()
    try:
        run_preview = strict_json(run_dir / "raw/run_contract.json")
        execution_preview = run_preview.get("execution")
        run_is_smoke = execution_preview == {
            "mode": "technical_smoke",
            "acceptance_eligible": False,
            "formal_m2_predecessor_bound": False,
        }
        if args.technical_smoke != run_is_smoke:
            raise ValidationError(
                "validator mode must exactly match the immutable run execution mode"
            )
        default_output = (
            Path("metrics/m3_actual_sitl_smoke.json")
            if args.technical_smoke
            else DEFAULT_OUTPUT
        )
        output_arg = args.output or default_output
        output = output_arg if output_arg.is_absolute() else run_dir / output_arg
        formal_output = run_dir / DEFAULT_OUTPUT
        if args.technical_smoke and formal_output.exists():
            raise ValidationError(
                "technical smoke refuses a run tree containing a formal M3 result"
            )
        m2_receipt = (
            args.m2_receipt
            if args.technical_smoke
            else args.m2_receipt.resolve(strict=True)
        )
        result = validate(
            run_dir,
            args.matrix.resolve(),
            m2_receipt,
        )
        if args.technical_smoke:
            result["contract"] = SMOKE_RESULT_CONTRACT
            result["acceptance_eligible"] = False
            result["formal_result_artifact_written"] = False
        payload = canonical_json(result)
        if args.no_write:
            if not regular_file(output):
                raise ValidationError(f"producer result is absent/nonregular: {output}")
            producer_payload = output.read_bytes()
            if producer_payload != payload:
                raise ValidationError(
                    "producer result differs byte-for-byte from independent derivation"
                )
            if hasattr(sys.stdout, "buffer"):
                sys.stdout.buffer.write(payload)
            else:  # pragma: no cover - exercised by in-process test harnesses.
                sys.stdout.write(payload.decode("utf-8"))
        else:
            write_new(output, payload)
            if hasattr(sys.stdout, "buffer"):
                sys.stdout.buffer.write(payload)
            else:  # pragma: no cover - exercised by in-process test harnesses.
                sys.stdout.write(payload.decode("utf-8"))
    except (ValidationError, OSError, ValueError, TypeError) as exc:
        print(f"FAIL M3 independent validation: {exc}", file=sys.stderr)
        return 2
    return 0 if result.get("passed") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
