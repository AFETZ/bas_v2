#!/usr/bin/env python3
"""Shared independent evidence checks for the two M4 validators."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

from network.radio_provider.sionna_async import (
    ProtocolValidationError,
    decode_message,
    node_state_sha256,
)
from network.radio_provider.sionna_packet_adapter import deterministic_loss_sample


STATE_SCHEMA = "ams.sionna.packet_state/v1"
PACKET_SCHEMA = "ams.ns3.packet_event/v1"
AUDIT_SCHEMA = "ams.sionna.packet_adapter_event/v1"
POSE_SCHEMA = "ams.m4.pose_snapshot/v2"
FAULT_SCHEMA = "ams.sionna.result_fault_event/v2"
TRAFFIC_CLASSES = ("control", "payload", "additional_data")
EXPECTED_CELLS = {
    (link, traffic_class)
    for uav in range(1, 6)
    for link in (f"cp>uav{uav}", f"uav{uav}>cp")
    for traffic_class in TRAFFIC_CLASSES
}
HEX64 = re.compile(r"^[0-9a-f]{64}$")
POSE_NODE_IDS = {"cp", "uav1", "uav2", "uav3", "uav4", "uav5"}
POSE_MAX_AGE_NS = 1_500_000_000
PATH_TYPE_NAMES = {
    "los",
    "specular",
    "diffuse",
    "refracted",
    "diffracted",
    "mixed",
}


class M4ValidationError(ValueError):
    """Raw evidence is missing, ambiguous, mutable, or internally inconsistent."""


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def regular_file(path: Path) -> bool:
    try:
        details = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(details.st_mode) and details.st_nlink == 1


def _unique(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise M4ValidationError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def strict_json(path: Path) -> dict[str, Any]:
    if not regular_file(path):
        raise M4ValidationError(f"missing/nonregular/hardlinked JSON: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise M4ValidationError(f"invalid JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise M4ValidationError(f"JSON root is not an object: {path}")
    return value


def strict_jsonl(path: Path, *, max_line_bytes: int = 1_048_576) -> list[dict[str, Any]]:
    if not regular_file(path):
        raise M4ValidationError(f"missing/nonregular/hardlinked JSONL: {path}")
    payload = path.read_bytes()
    if not payload or not payload.endswith(b"\n"):
        raise M4ValidationError(f"JSONL is empty or lacks final newline: {path}")
    output: list[dict[str, Any]] = []
    for line_number, line in enumerate(payload.splitlines(), start=1):
        if not line or len(line) + 1 > max_line_bytes:
            raise M4ValidationError(f"invalid bounded line at {path}:{line_number}")
        try:
            value = json.loads(line.decode("utf-8"), object_pairs_hook=_unique)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise M4ValidationError(
                f"invalid JSONL at {path}:{line_number}: {exc}"
            ) from exc
        if not isinstance(value, dict):
            raise M4ValidationError(f"non-object JSONL at {path}:{line_number}")
        output.append(value)
    return output


def exact_keys(value: Any, expected: set[str], label: str) -> list[str]:
    if not isinstance(value, dict):
        return [f"{label} is not an object"]
    actual = set(value)
    if actual == expected:
        return []
    return [
        f"{label} keys differ: missing={sorted(expected-actual)} extra={sorted(actual-expected)}"
    ]


def finite_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def gate(failures: list[str], details: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {"passed": not failures, "failures": failures, "details": dict(details or {})}


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


def validate_wire_log(directory: Path) -> tuple[dict[str, Any], list[str]]:
    failures: list[str] = []
    details: dict[str, Any] = {}
    data_path = directory / "sionna_async_wire.bin"
    index_path = directory / "sionna_async_wire_index.jsonl"
    if not regular_file(data_path):
        return {}, [f"missing/nonregular wire data: {data_path}"]
    try:
        index = strict_jsonl(index_path, max_line_bytes=16_384)
    except M4ValidationError as exc:
        return {}, [str(exc)]
    data = data_path.read_bytes()
    expected_offset = 0
    messages: list[dict[str, Any]] = []
    raw_by_hash: dict[str, bytes] = {}
    message_by_hash: dict[str, dict[str, Any]] = {}
    for number, record in enumerate(index, start=1):
        failures.extend(
            exact_keys(
                record,
                {"connection_id", "direction", "length", "monotonic_ns", "offset", "sha256"},
                f"wire index record {number}",
            )
        )
        offset = record.get("offset")
        length = record.get("length")
        digest = record.get("sha256")
        if (
            isinstance(offset, bool)
            or not isinstance(offset, int)
            or offset != expected_offset
            or isinstance(length, bool)
            or not isinstance(length, int)
            or not 1 <= length <= 1_048_576
            or not isinstance(digest, str)
            or not HEX64.fullmatch(digest)
            or offset + length > len(data)
        ):
            failures.append(f"wire index record {number} has invalid offset/length/hash")
            continue
        raw = data[offset : offset + length]
        expected_offset += length
        if hashlib.sha256(raw).hexdigest() != digest:
            failures.append(f"wire index record {number} SHA-256 mismatch")
            continue
        raw_by_hash[digest] = raw
        try:
            message = dict(decode_message(raw))
        except (ProtocolValidationError, ValueError) as exc:
            failures.append(f"wire frame {number} violates async schema: {exc}")
            continue
        messages.append(message)
        message_by_hash[digest] = message
    if expected_offset != len(data):
        failures.append("wire index does not cover the exact binary stream")
    message_types = [message.get("message_type") for message in messages]
    for required in ("hello", "ready", "query", "result"):
        if required not in message_types:
            failures.append(f"wire log has no {required} frame")
    provider_hellos = [
        message
        for message in messages
        if message.get("message_type") == "hello"
        and message.get("sender_role") == "provider"
    ]
    if not provider_hellos:
        failures.append("wire log has no provider hello")
    elif any(
        hello.get("provider_identity", {}).get("provider_mode") != "real_sionna"
        or hello.get("provider_identity", {}).get("acceptance_eligible") is not True
        for hello in provider_hellos
    ):
        failures.append("provider hello is not acceptance-eligible real_sionna")
    sequences: dict[str, list[int]] = defaultdict(list)
    for message in messages:
        sender = message.get("sender_id")
        sequence = message.get("wire_sequence")
        if isinstance(sender, str) and isinstance(sequence, int):
            sequences[sender].append(sequence)
    for sender, values in sequences.items():
        # Exact-wire logs may intentionally contain the same frame once at
        # each socket endpoint.  Deduplicate byte-identical observations only.
        compact: list[int] = []
        for value in values:
            if not compact or compact[-1] != value:
                compact.append(value)
        if any(right <= left for left, right in zip(compact, compact[1:])):
            failures.append(f"wire_sequence is not strictly increasing for {sender}")
    details.update(
        {
            "wire_records": len(index),
            "wire_bytes": len(data),
            "message_counts": {
                kind: message_types.count(kind) for kind in sorted(set(message_types))
            },
            "messages": messages,
            "message_by_hash": message_by_hash,
            "raw_by_hash": raw_by_hash,
        }
    )
    return details, failures


def validate_states(path: Path, wire: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    failures: list[str] = []
    try:
        records = strict_jsonl(path, max_line_bytes=65_536)
    except M4ValidationError as exc:
        return {}, [str(exc)]
    previous_sequence = 0
    by_hash: dict[str, dict[str, Any]] = {}
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    message_by_hash = wire.get("message_by_hash", {})
    unavailable = 0
    geometry_counts: dict[str, int] = defaultdict(int)
    for number, record in enumerate(records, start=1):
        sequence = record.get("state_sequence")
        state_hash = record.get("state_sha256")
        if record.get("schema") != STATE_SCHEMA:
            failures.append(f"state record {number} schema mismatch")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence <= previous_sequence:
            failures.append(f"state record {number} sequence is not strictly increasing")
        else:
            previous_sequence = sequence
        if not isinstance(state_hash, str) or not HEX64.fullmatch(state_hash):
            failures.append(f"state record {number} has invalid state_sha256")
            continue
        unhashed = dict(record)
        del unhashed["state_sha256"]
        canonical = json.dumps(unhashed, sort_keys=True, separators=(",", ":")).encode()
        if hashlib.sha256(canonical).hexdigest() != state_hash:
            failures.append(f"state record {number} self-hash mismatch")
            continue
        by_hash[state_hash] = record
        cell = (record.get("directed_link"), record.get("traffic_class"))
        if cell not in EXPECTED_CELLS:
            failures.append(f"state record {number} has an undeclared cell {cell}")
            continue
        if record.get("availability") != "fresh":
            unavailable += 1
            latest[cell] = record
            continue
        required = (
            "query_id",
            "query_wire_sha256",
            "result_wire_sha256",
            "applied_state_id",
            "validity_start_monotonic_ns",
            "expires_monotonic_ns",
            "adapter_applied_monotonic_ns",
            "physical",
            "effects",
        )
        if any(record.get(key) is None for key in required):
            failures.append(f"state record {number} lacks fresh-state lineage")
            continue
        query_message = message_by_hash.get(record.get("query_wire_sha256"))
        result_message = message_by_hash.get(record.get("result_wire_sha256"))
        if not query_message or query_message.get("message_type") != "query":
            failures.append(f"state record {number} query wire hash is not captured")
        if not result_message or result_message.get("message_type") != "result":
            failures.append(f"state record {number} result wire hash is not captured")
        if query_message and query_message.get("query_id") != record.get("query_id"):
            failures.append(f"state record {number} query correlation mismatch")
        if result_message:
            if result_message.get("query_id") != record.get("query_id"):
                failures.append(f"state record {number} result correlation mismatch")
            if result_message.get("status") != "ok":
                failures.append(f"state record {number} comes from a failed result")
            if result_message.get("physical") != record.get("physical"):
                failures.append(f"state record {number} physical output differs from wire")
        physical = record.get("physical")
        if isinstance(physical, dict):
            geometry = physical.get("geometry_state")
            path_count = physical.get("path_count")
            path_types = physical.get("path_type_counts")
            if (
                geometry not in {"los", "nlos", "blocked_no_path"}
                or isinstance(path_count, bool)
                or not isinstance(path_count, int)
                or path_count < 0
                or not isinstance(path_types, dict)
                or set(path_types) != PATH_TYPE_NAMES
                or any(
                    isinstance(value, bool) or not isinstance(value, int) or value < 0
                    for value in path_types.values()
                )
                or sum(path_types.values()) != path_count
                or (path_count == 0) != (geometry == "blocked_no_path")
                or (geometry == "los" and path_types.get("los", 0) == 0)
                or (geometry == "nlos" and path_types.get("los", 0) != 0)
            ):
                failures.append(f"state record {number} path geometry evidence is invalid")
            else:
                geometry_counts[str(geometry)] += 1
        start = record.get("validity_start_monotonic_ns")
        expiry = record.get("expires_monotonic_ns")
        applied = record.get("adapter_applied_monotonic_ns")
        if not all(isinstance(item, int) and not isinstance(item, bool) for item in (start, expiry, applied)):
            failures.append(f"state record {number} has invalid monotonic interval")
        elif not start <= applied < expiry:
            failures.append(f"state record {number} was applied outside validity")
        effects = record.get("effects")
        if not isinstance(effects, dict):
            failures.append(f"state record {number} effects are absent")
        else:
            expected_effect_keys = {
                "mapping_version",
                "mapping_seed",
                "propagation_delay_ns",
                "loss_probability",
                "service_rate_bps",
                "reference_loss_sample",
                "reference_delivery",
                "intervention",
            }
            delay = effects.get("propagation_delay_ns")
            loss = effects.get("loss_probability")
            rate = effects.get("service_rate_bps")
            if (
                set(effects) != expected_effect_keys
                or
                not isinstance(delay, int)
                or isinstance(delay, bool)
                or not 0 <= delay <= 100_000_000
                or not finite_number(loss)
                or not 0.0 <= float(loss) <= 1.0
                or not isinstance(effects.get("mapping_seed"), int)
                or not isinstance(effects.get("mapping_version"), str)
                or isinstance(rate, bool)
                or rate not in {0, 1_000, 10_000, 100_000, 500_000, 2_000_000, 20_000_000}
                or not isinstance(physical, dict)
                or delay != int(round(float(physical.get("propagation_delay_ns", -1))))
            ):
                failures.append(f"state record {number} has invalid mapped effects")
        latest[cell] = record
    missing = EXPECTED_CELLS - set(latest)
    if missing:
        failures.append(f"applied state log misses {len(missing)} of 30 cells")
    details = {
        "records": records,
        "record_count": len(records),
        "fresh_count": sum(record.get("availability") == "fresh" for record in records),
        "unavailable_count": unavailable,
        "latest_cells": len(latest),
        "by_hash": by_hash,
        "latest": latest,
        "geometry_counts": dict(sorted(geometry_counts.items())),
    }
    return details, failures


def _pose_query_form(record: Mapping[str, Any], *, jammer: bool) -> dict[str, Any]:
    expected = {
        "pose_monotonic_ns",
        "source_topic",
        "source_frame",
        "transform_version",
        "position_m",
        "orientation_quat_xyzw",
        "freshness_age_ns",
        "stale",
        "jammer_id" if jammer else "node_id",
    }
    if jammer:
        expected |= {
            "enabled",
            "center_frequency_hz",
            "bandwidth_hz",
            "power_dbm",
            "duty_cycle",
            "antenna_pattern",
        }
    else:
        expected.add("role")
    raw_lineage = {
        "source_header_stamp_ns",
        "source_header_frame",
        "source_child_frame",
    }
    if set(record) != expected | raw_lineage:
        raise M4ValidationError(
            f"raw pose entity keys differ: "
            f"missing={sorted((expected | raw_lineage)-set(record))} "
            f"extra={sorted(set(record)-(expected | raw_lineage))}"
        )
    source_stamp_ns = record.get("source_header_stamp_ns")
    source_header_frame = record.get("source_header_frame")
    source_child_frame = record.get("source_child_frame")
    identity_key = "jammer_id" if jammer else "node_id"
    identity = record.get(identity_key)
    child_parts = (
        [part for part in source_child_frame.strip("/").split("/") if part]
        if isinstance(source_child_frame, str)
        else []
    )
    if (
        isinstance(source_stamp_ns, bool)
        or not isinstance(source_stamp_ns, int)
        or source_stamp_ns < 0
        or not isinstance(source_header_frame, str)
        or not isinstance(source_child_frame, str)
        or not source_child_frame
        or record.get("source_frame") != "world"
        or record.get("transform_version") != "enu-identity-v1"
    ):
        raise M4ValidationError("raw pose source frame/timestamp differs")
    if identity in {"uav1", "uav2", "uav3", "uav4", "uav5"}:
        if (
            record.get("source_topic") != f"/{identity}/odometry"
            or source_header_frame != "odom"
            or source_child_frame != "base_link"
        ):
            raise M4ValidationError(f"raw {identity} odometry lineage differs")
    elif identity in {"cp", "jammer_m4"}:
        if (
            record.get("source_topic") != "/world/map/pose/info"
            or not child_parts
            or child_parts[-1] != identity
        ):
            raise M4ValidationError(f"raw {identity} world-pose lineage differs")
    else:
        raise M4ValidationError("raw pose entity identity differs")
    value = {
        key: item for key, item in record.items() if key not in raw_lineage
    }
    if set(value) != expected:
        raise M4ValidationError(
            f"pose entity keys differ: missing={sorted(expected-set(value))} "
            f"extra={sorted(set(value)-expected)}"
        )
    return value


def _immutable_pose(value: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(value)
    result.pop("freshness_age_ns", None)
    result.pop("stale", None)
    return result


def validate_pose_snapshots(
    path: Path,
    wire: Mapping[str, Any],
    *,
    start_monotonic_ns: int | None = None,
    end_monotonic_ns: int | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Bind every query to one atomic raw ROS/Gazebo pose snapshot."""

    failures: list[str] = []
    try:
        records = strict_jsonl(path, max_line_bytes=262_144)
    except M4ValidationError as exc:
        return {}, [str(exc)]
    expected_keys = {
        "schema",
        "pose_sequence",
        "node_state_seq",
        "node_state_sha256",
        "snapshot_monotonic_ns",
        "host_monotonic_ns",
        "source_frame",
        "transform_version",
        "nodes",
        "jammers",
    }
    snapshots: dict[tuple[int, str, int], dict[str, Any]] = {}
    previous_sequence = 0
    previous_snapshot = -1
    for number, record in enumerate(records, start=1):
        if set(record) != expected_keys or record.get("schema") != POSE_SCHEMA:
            failures.append(f"pose snapshot {number} envelope differs")
            continue
        sequence = record.get("pose_sequence")
        state_sequence = record.get("node_state_seq")
        snapshot_ns = record.get("snapshot_monotonic_ns")
        host_ns = record.get("host_monotonic_ns")
        digest = record.get("node_state_sha256")
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence != previous_sequence + 1
            or state_sequence != sequence
            or isinstance(snapshot_ns, bool)
            or not isinstance(snapshot_ns, int)
            or snapshot_ns <= previous_snapshot
            or host_ns != snapshot_ns
            or not isinstance(digest, str)
            or HEX64.fullmatch(digest) is None
        ):
            failures.append(f"pose snapshot {number} sequence/time/hash differs")
            continue
        previous_sequence = sequence
        previous_snapshot = snapshot_ns
        try:
            nodes = [_pose_query_form(item, jammer=False) for item in record["nodes"]]
            jammers = [_pose_query_form(item, jammer=True) for item in record["jammers"]]
            if {item.get("node_id") for item in nodes} != POSE_NODE_IDS:
                raise M4ValidationError("pose snapshot node set differs")
            if {item.get("jammer_id") for item in jammers} != {"jammer_m4"}:
                raise M4ValidationError("pose snapshot jammer set differs")
            for pose in [*nodes, *jammers]:
                pose_ns = pose.get("pose_monotonic_ns")
                age = pose.get("freshness_age_ns")
                if (
                    isinstance(pose_ns, bool)
                    or not isinstance(pose_ns, int)
                    or isinstance(age, bool)
                    or not isinstance(age, int)
                    or age != snapshot_ns - pose_ns
                    or pose.get("stale") is not (age < 0 or age > POSE_MAX_AGE_NS)
                ):
                    raise M4ValidationError("pose snapshot freshness is not derived")
            expected_hash = node_state_sha256(
                node_state_seq=sequence,
                snapshot_monotonic_ns=snapshot_ns,
                source_frame=str(record["source_frame"]),
                transform_version=str(record["transform_version"]),
                nodes=nodes,
                jammers=jammers,
            )
            if digest != expected_hash:
                raise M4ValidationError("pose snapshot immutable SHA-256 differs")
            snapshots[(sequence, digest, snapshot_ns)] = {
                "nodes": nodes,
                "jammers": jammers,
                "source_frame": record["source_frame"],
                "transform_version": record["transform_version"],
            }
        except (KeyError, TypeError, ValueError, M4ValidationError) as exc:
            failures.append(f"pose snapshot {number} is invalid: {exc}")

    query_count = 0
    referenced: set[tuple[int, str, int]] = set()
    for message in wire.get("messages", []):
        if message.get("message_type") != "query":
            continue
        sent = message.get("request_sent_monotonic_ns")
        if start_monotonic_ns is not None and (
            not isinstance(sent, int) or sent < start_monotonic_ns
        ):
            continue
        if end_monotonic_ns is not None and (
            not isinstance(sent, int) or sent >= end_monotonic_ns
        ):
            continue
        query_count += 1
        key = (
            message.get("node_state_seq"),
            message.get("node_state_sha256"),
            message.get("node_state_snapshot_monotonic_ns"),
        )
        snapshot = snapshots.get(key)
        if snapshot is None:
            failures.append(f"query {message.get('query_id')} references no raw pose snapshot")
            continue
        referenced.add(key)
        if (
            message.get("source_frame") != snapshot["source_frame"]
            or message.get("transform_version") != snapshot["transform_version"]
            or [_immutable_pose(item) for item in message.get("nodes", [])]
            != [_immutable_pose(item) for item in snapshot["nodes"]]
            or [_immutable_pose(item) for item in message.get("jammers", [])]
            != [_immutable_pose(item) for item in snapshot["jammers"]]
        ):
            failures.append(f"query {message.get('query_id')} pose bytes differ from snapshot")
    if query_count == 0:
        failures.append("pose lineage has no query in the requested window")
    return {
        "snapshot_count": len(records),
        "valid_snapshot_count": len(snapshots),
        "query_count": query_count,
        "referenced_snapshot_count": len(referenced),
    }, failures


def validate_adapter_audit(
    path: Path,
    *,
    start_monotonic_ns: int | None = None,
    end_monotonic_ns: int | None = None,
) -> tuple[dict[str, Any], list[str]]:
    failures: list[str] = []
    try:
        records = strict_jsonl(path, max_line_bytes=65_536)
    except M4ValidationError as exc:
        return {}, [str(exc)]
    last = 0
    submitted: set[tuple[str, str]] = set()
    applied: set[tuple[str, str]] = set()
    forbidden_reasons: list[str] = []
    for number, record in enumerate(records, start=1):
        if record.get("schema") != AUDIT_SCHEMA:
            failures.append(f"adapter audit {number} schema mismatch")
        sequence = record.get("audit_sequence")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence <= last:
            failures.append(f"adapter audit {number} sequence is not increasing")
        else:
            last = sequence
        timestamp = record.get("monotonic_ns")
        in_window = (
            (
                start_monotonic_ns is None
                or isinstance(timestamp, int)
                and timestamp >= start_monotonic_ns
            )
            and (
                end_monotonic_ns is None
                or isinstance(timestamp, int)
                and timestamp < end_monotonic_ns
            )
        )
        if not in_window:
            continue
        cell = (record.get("directed_link"), record.get("traffic_class"))
        event = record.get("event")
        if event == "query_submitted" and cell in EXPECTED_CELLS:
            submitted.add(cell)
        if event == "result_applied" and cell in EXPECTED_CELLS:
            applied.add(cell)
        reason = record.get("reason")
        if isinstance(reason, str) and any(
            token in reason
            for token in (
                "overflow",
                "transport_not_ready",
                "stale",
                "expired",
                "deadline",
                "disconnect",
                "provider_error",
            )
        ):
            forbidden_reasons.append(reason)
    if submitted != EXPECTED_CELLS:
        failures.append(f"adapter submitted queries for {len(submitted)} of 30 cells")
    if applied != EXPECTED_CELLS:
        failures.append(f"adapter applied results for {len(applied)} of 30 cells")
    if forbidden_reasons:
        failures.append(f"adapter audit contains overload/stale/expiry faults: {forbidden_reasons[:5]}")
    return {
        "record_count": len(records),
        "submitted_cells": len(submitted),
        "applied_cells": len(applied),
        "forbidden_reasons": forbidden_reasons,
    }, failures


def validate_fault_audit(
    path: Path,
    wire: Mapping[str, Any],
    *,
    required_events: set[str] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Prove that expiry faults used captured byte-identical real results."""

    failures: list[str] = []
    try:
        records = strict_jsonl(path, max_line_bytes=32_768)
    except M4ValidationError as exc:
        return {}, [str(exc)]
    expected_keys = {
        "schema",
        "fault_sequence",
        "monotonic_ns",
        "event",
        "directed_link_id",
        "query_id",
        "result_wire_sha256",
        "payload_policy",
        "bounded_state",
    }
    bounded_keys = {
        "captured_count",
        "captured_evictions",
        "captured_high_watermark",
        "captured_overflows",
        "held_count",
        "held_overflows",
        "max_captured_results",
        "max_held_results",
        "max_release_queue",
        "release_queue_overflows",
        "release_queue_size",
    }
    previous_sequence = 0
    previous_time = -1
    observed_events: set[str] = set()
    message_by_hash = wire.get("message_by_hash", {})
    result_hash_by_query: dict[str, str] = {}
    for number, record in enumerate(records, start=1):
        sequence = record.get("fault_sequence")
        timestamp = record.get("monotonic_ns")
        event = record.get("event")
        state = record.get("bounded_state")
        if (
            set(record) != expected_keys
            or record.get("schema") != FAULT_SCHEMA
            or isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence != previous_sequence + 1
            or isinstance(timestamp, bool)
            or not isinstance(timestamp, int)
            or timestamp <= previous_time
            or not isinstance(event, str)
            or record.get("payload_policy")
            != "byte_identical_real_provider_result"
            or not isinstance(state, dict)
            or set(state) != bounded_keys
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in state.values()
            )
            or state["captured_count"] > state["max_captured_results"]
            or state["captured_high_watermark"] > state["max_captured_results"]
            or state["held_count"] > state["max_held_results"]
            or state["release_queue_size"] > state["max_release_queue"]
            or any(
                state[name] != 0
                for name in (
                    "captured_overflows",
                    "held_overflows",
                    "release_queue_overflows",
                )
            )
        ):
            failures.append(f"fault audit record {number} envelope/bounds differ")
            continue
        previous_sequence = sequence
        previous_time = timestamp
        observed_events.add(event)
        digest = record.get("result_wire_sha256")
        query_id = record.get("query_id")
        if digest is None and query_id is None:
            if event != "hold_armed":
                failures.append(f"fault audit record {number} lacks result identity")
            continue
        if (
            not isinstance(digest, str)
            or HEX64.fullmatch(digest) is None
            or not isinstance(query_id, str)
            or not query_id
        ):
            failures.append(f"fault audit record {number} result identity is invalid")
            continue
        message = message_by_hash.get(digest)
        if (
            not isinstance(message, dict)
            or message.get("message_type") != "result"
            or message.get("query_id") != query_id
            or message.get("directed_link_id") != record.get("directed_link_id")
        ):
            failures.append(f"fault audit record {number} is not captured provider wire")
        previous_hash = result_hash_by_query.setdefault(query_id, digest)
        if previous_hash != digest:
            failures.append(f"fault audit query {query_id} changed result bytes")
    required = required_events or set()
    if not required <= observed_events:
        failures.append(
            f"fault audit events differ: missing={sorted(required-observed_events)}"
        )
    return {
        "record_count": len(records),
        "observed_events": sorted(observed_events),
        "identified_result_count": len(result_hash_by_query),
    }, failures


def decision_key(event: Mapping[str, Any]) -> tuple[int, int, str, str]:
    """Return the ns-3 occurrence identity, never a payload identity.

    MAVLink sequence numbers wrap during the ten-minute capacity window, so
    byte-identical UDP payloads are legitimate.  The ns-3 epoch/UID pair is the
    engine-owned occurrence identity; link/class keep accidental cross-route
    reuse fail closed.
    """

    epoch = event.get("event_epoch")
    uid = event.get("packet_uid")
    return (
        epoch if isinstance(epoch, int) and not isinstance(epoch, bool) else -1,
        uid if isinstance(uid, int) and not isinstance(uid, bool) else -1,
        str(event.get("directed_link")),
        str(event.get("traffic_class")),
    )


def validate_packet_events(
    path: Path,
    states: Mapping[str, Any],
    *,
    required_intervention: str | None = None,
    start_monotonic_ns: int | None = None,
    end_monotonic_ns: int | None = None,
) -> tuple[dict[str, Any], list[str]]:
    failures: list[str] = []
    try:
        records = strict_jsonl(path, max_line_bytes=65_536)
    except M4ValidationError as exc:
        return {}, [str(exc)]
    previous = 0
    by_source_sequence: dict[int, dict[str, Any]] = {}
    decisions: dict[tuple[int, int, str, str], dict[str, Any]] = {}
    decision_cells: set[tuple[str, str]] = set()
    downstream: set[tuple[int, int, str, str]] = set()
    events_by_key: dict[
        tuple[int, int, str, str], list[dict[str, Any]]
    ] = defaultdict(list)
    forbidden_statuses: list[str] = []
    state_by_hash = states.get("by_hash", {})
    for number, event in enumerate(records, start=1):
        if event.get("schema") != PACKET_SCHEMA:
            failures.append(f"packet event {number} schema mismatch")
        sequence = event.get("event_sequence")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence <= previous:
            failures.append(f"packet event {number} sequence is not increasing")
        else:
            previous = sequence
            by_source_sequence[sequence] = event
        cell = (event.get("directed_link"), event.get("traffic_class"))
        if cell not in EXPECTED_CELLS:
            continue
        epoch = event.get("event_epoch")
        uid = event.get("packet_uid")
        if (
            isinstance(epoch, bool)
            or not isinstance(epoch, int)
            or epoch <= 0
            or isinstance(uid, bool)
            or not isinstance(uid, int)
            or uid < 0
        ):
            failures.append(f"packet event {number} occurrence identity is invalid")
        host = event.get("host_monotonic_ns")
        in_window = (
            (start_monotonic_ns is None or isinstance(host, int) and host >= start_monotonic_ns)
            and (end_monotonic_ns is None or isinstance(host, int) and host < end_monotonic_ns)
        )
        if not in_window:
            continue
        status = event.get("radio_state_status")
        if status in {"missing", "unavailable", "expired", "ipc_fault", "lineage_cache_overflow"}:
            forbidden_statuses.append(str(status))
        key = decision_key(event)
        events_by_key[key].append(event)
        if event.get("event") in {"channel", "egress"}:
            downstream.add(key)
        if status != "fresh" or event.get("event") not in {"enqueue", "drop"}:
            continue
        if key in decisions:
            failures.append(f"duplicate packet decision key {key}")
            continue
        decisions[key] = event
        decision_cells.add(cell)
        state = state_by_hash.get(event.get("radio_state_sha256"))
        if not state:
            failures.append(f"packet decision {number} references an unknown state hash")
            continue
        mappings = {
            "radio_state_sequence": "state_sequence",
            "radio_query_id": "query_id",
            "radio_applied_state_id": "applied_state_id",
            "radio_result_wire_sha256": "result_wire_sha256",
        }
        for packet_field, state_field in mappings.items():
            if event.get(packet_field) != state.get(state_field):
                failures.append(f"packet decision {number} {packet_field} lineage mismatch")
        effects = state.get("effects", {})
        for packet_field, effect_field in (
            ("radio_mapping_version", "mapping_version"),
            ("radio_mapping_seed", "mapping_seed"),
            ("radio_delay_ns", "propagation_delay_ns"),
            ("radio_loss_probability", "loss_probability"),
            ("radio_service_rate_bps", "service_rate_bps"),
        ):
            if event.get(packet_field) != effects.get(effect_field):
                failures.append(f"packet decision {number} {packet_field} effects mismatch")
        causal_hash = event.get("transport_payload_sha256")
        if not isinstance(causal_hash, str) or not HEX64.fullmatch(causal_hash):
            failures.append(f"packet decision {number} lacks causal payload hash")
        else:
            expected_sample = deterministic_loss_sample(
                causal_hash,
                str(state.get("applied_state_id")),
                int(effects.get("mapping_seed", -1)),
            )
            observed_sample = event.get("radio_loss_sample")
            if not finite_number(observed_sample) or abs(float(observed_sample) - expected_sample) > 1e-15:
                failures.append(f"packet decision {number} deterministic loss sample mismatch")
        delivery = event.get("radio_delivery")
        if delivery not in {"deliver", "drop"}:
            failures.append(f"packet decision {number} delivery is invalid")
        if required_intervention is not None and event.get("radio_intervention") != required_intervention:
            failures.append(f"packet decision {number} intervention mismatch")
        expected_drop_reason = (
            "sionna_service_rate_zero"
            if effects.get("service_rate_bps") == 0
            else "sionna_loss"
        )
        if delivery == "drop" and event.get("drop_reason") != expected_drop_reason:
            failures.append(
                f"packet decision {number} does not carry {expected_drop_reason}"
            )
        source_sequence = state.get("source_packet_event_sequence")
        source = by_source_sequence.get(source_sequence)
        if source is None:
            failures.append(f"packet decision {number} state source event is absent")
        elif source.get("event") != "ingress":
            failures.append(f"packet decision {number} state source is not ingress")
        elif (
            isinstance(source_sequence, bool)
            or not isinstance(source_sequence, int)
            or source_sequence >= sequence
            or state.get("source_packet_event_epoch") != source.get("event_epoch")
            or state.get("source_packet_uid") != source.get("packet_uid")
            or state.get("source_packet_causal_sha256")
            != source.get("transport_payload_sha256")
            or state.get("directed_link") != source.get("directed_link")
            or state.get("traffic_class") != source.get("traffic_class")
            or state.get("directed_link") != event.get("directed_link")
            or state.get("traffic_class") != event.get("traffic_class")
        ):
            failures.append(
                f"packet decision {number} state source lineage mismatch"
            )
    if decision_cells != EXPECTED_CELLS:
        failures.append(f"packet decisions cover {len(decision_cells)} of 30 cells")
    for key, event in decisions.items():
        delivered = event.get("radio_delivery") == "deliver"
        if delivered != (key in downstream):
            failures.append(f"packet outcome/downstream mismatch for {key}")
        related = events_by_key.get(key, [])
        dequeues = [item for item in related if item.get("event") == "dequeue"]
        channels = [item for item in related if item.get("event") == "channel"]
        if delivered:
            if len(dequeues) != 1:
                failures.append(f"delivered packet has {len(dequeues)} dequeue effects: {key}")
                continue
            if len(channels) != 1:
                failures.append(f"delivered packet has {len(channels)} channel effects: {key}")
                continue
            channel = channels[0]
            rate = channel.get("radio_service_rate_bps")
            size = channel.get("packet_wire_size")
            serialization = channel.get("radio_serialization_time_ns")
            base_serialization = channel.get("radio_base_serialization_time_ns")
            padding = channel.get("radio_service_padding_ns")
            base_channel_delay = channel.get("radio_base_channel_delay_ns")
            effective_delay = channel.get("radio_effective_channel_delay_ns")
            propagation = channel.get("radio_delay_ns")
            applied_at = channel.get("radio_rate_applied_at_monotonic_ns")
            delay_applied_at = channel.get("radio_delay_applied_at_monotonic_ns")
            source = str(channel.get("directed_link", "")).split(">", 1)[0]
            expected_serialization = (
                (size * 8 * 1_000_000_000 + rate - 1) // rate
                if isinstance(size, int)
                and not isinstance(size, bool)
                and isinstance(rate, int)
                and not isinstance(rate, bool)
                and rate > 0
                else -1
            )
            expected_base = (
                (size * 8 * 1_000_000_000 + 20_000_000 - 1) // 20_000_000
                if isinstance(size, int) and not isinstance(size, bool) and size > 0
                else -1
            )
            if (
                rate not in {1_000, 10_000, 100_000, 500_000, 2_000_000, 20_000_000}
                or isinstance(size, bool)
                or not isinstance(size, int)
                or size <= 0
                or serialization != expected_serialization
                or base_serialization != expected_base
                or padding != max(0, expected_serialization - expected_base)
                or base_channel_delay != 2_000_000
                or not isinstance(propagation, int)
                or isinstance(propagation, bool)
                or effective_delay != propagation + padding
                or isinstance(applied_at, bool)
                or not isinstance(applied_at, int)
                or delay_applied_at != applied_at
                or channel.get("radio_applied_device_id") != f"{source}.radio"
                or applied_at > int(channel.get("host_monotonic_ns", -1))
            ):
                failures.append(f"packet-scoped rate/delay application differs: {key}")
    if forbidden_statuses:
        failures.append(
            f"packet path contains unavailable/stale/overflow state: {sorted(set(forbidden_statuses))}"
        )
    return {
        "records": records,
        "record_count": len(records),
        "decision_count": len(decisions),
        "decision_cells": len(decision_cells),
        "decisions": decisions,
        "downstream": downstream,
        "forbidden_statuses": forbidden_statuses,
    }, failures


__all__ = [
    "EXPECTED_CELLS",
    "HEX64",
    "M4ValidationError",
    "canonical_json",
    "decision_key",
    "exact_keys",
    "finite_number",
    "gate",
    "regular_file",
    "strict_json",
    "strict_jsonl",
    "validate_adapter_audit",
    "validate_fault_audit",
    "validate_packet_events",
    "validate_pose_snapshots",
    "validate_states",
    "validate_wire_log",
    "write_new",
]
