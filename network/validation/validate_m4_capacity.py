#!/usr/bin/env python3
"""Independently validate the exact 30+600 s real-Sionna M4 prerequisite."""

from __future__ import annotations

import argparse
import bisect
import hashlib
import importlib.metadata
import json
import math
import re
import struct
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from network.radio_provider.sionna_async import (
    ProtocolValidationError,
    decode_message,
)
from network.validation.m4_common import (
    M4ValidationError,
    canonical_json,
    exact_keys,
    gate,
    regular_file,
    strict_json,
    strict_jsonl,
    validate_adapter_audit,
    validate_packet_events,
    validate_pose_snapshots,
    validate_states,
    validate_wire_log,
    write_new,
)
from network.validation.m4_runtime import (
    FROZEN_BUNDLE_ID,
    FROZEN_BUNDLE_PATH,
    FROZEN_BUNDLE_SHA256,
    MAX_POSE_AGE_NS,
    QUERY_DEADLINE_NS,
    QUERY_PERIOD_NS,
    REQUIRED_CLOCK_PRODUCERS,
    VALIDITY_TTL_NS,
    bind_actual_control_frame,
    index_actual_control_datagrams,
    index_exact_ns3_unicast_drops,
    index_exact_ns3_unicast_deliveries,
    load_runtime_events,
    sha256_file,
    validate_capacity_freshness,
    validate_capacity_runtime,
    validate_capacity_workload,
    validate_clock_correlations,
    validate_clock_process_binding,
    validate_external_captures,
    validate_query_pose_runtime_binding,
    validate_scene_prerequisite,
    split_exact_mavlink_datagram,
)
from network.validation.m4_frame_alignment import (
    validate_runtime_frame_correspondence,
)
from network.validation.m4_airborne_motion import validate_measurement_motion
from network.validation.m4_capacity_budget import (
    validate_capacity_execution_budget,
)
from network.scripts.m4_capacity_airborne import (
    AIRBORNE_GATE_CONTRACT,
    COPTER_MODE_GUIDED,
    EXPECTED_UAVS,
    HEARTBEAT_FRESHNESS_NS,
    HIGH_RATE_STATE_FRESHNESS_NS,
    MAV_LANDED_STATE_IN_AIR,
    MAV_LANDED_STATE_ON_GROUND,
    MAV_MODE_FLAG_SAFETY_ARMED,
    MAV_RESULT_ACCEPTED,
    MINIMUM_GAZEBO_RISE_M,
    MINIMUM_RELATIVE_ALT_M,
    MINIMUM_SEPARATION_M,
    OUTCOME_TIMEOUT_NS,
    POSE_FRESHNESS_NS,
    POST_MEASUREMENT_STAGES,
    PRE_MEASUREMENT_STAGES,
    STAGE_DEFINITIONS,
    STAGE_BY_NAME,
    WARMUP_MOTION_STAGES,
    airborne_gate_contract,
    finite_vector3,
    flight_timesync_token,
)


ROOT = Path(__file__).resolve().parents[2]
RUN_CONTRACT = "ams.m4.capacity_run/v3"
RESULT_CONTRACT = "ams.m4-capacity.validation/v2"
DEFAULT_OUTPUT = Path("metrics/m4_capacity_validation.json")
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
IMAGE = re.compile(r"^sha256:[0-9a-f]{64}$")
ACTUAL_CONTROL_API_CONTRACT = "ams.m3.actual-control-api/v2"
SIONNA_RT_DISTRIBUTION = "sionna-rt"
ACTUAL_CONTROL_ENDPOINT_FORM = "actual_sitl_mavproxy_udp_tail"
ACTUAL_CONTROL_EVENT_SCHEMA = "ams.actual-sitl.control-event/v1"
M3_RESULT_CONTRACT = "ams.m3.external-matrix-validation/v1"
M3_RECEIPT_CONTRACT = "ams.m3.host-final-receipt/v1"
CAPTURE_STATS_CONTRACT = "ams.raw-packet-capture-stats/v2"
CAPTURE_PROTOCOL = "ETH_P_ALL"
CAPTURE_PACKET_FILTER = "none"
CAPTURE_RECEIVE_BUFFER_REQUESTED_BYTES = 8_388_608
CAPTURE_RECEIVE_BUFFER_EFFECTIVE_BYTES = 16_777_216
CAPTURE_RECEIVE_BUFFER_SETTERS = {"SO_RCVBUF", "SO_RCVBUFFORCE"}
CAPTURE_DRAIN_BATCH_PACKET_LIMIT = 256
CAPTURE_DRAIN_BATCH_BYTE_LIMIT = 4_194_304
PROVIDER_SCRIPT_PATH = "network/scripts/m4_runtime_orchestrator.py"
PROVIDER_READY_PATH = "raw/state/provider.ready.json"
PROVIDER_STOP_PATH = "raw/control/provider.stop"
PROVIDER_SENDER_ID = "sionna-provider-m4"
ADAPTER_SENDER_ID = "sionna-adapter-m4"
PROVIDER_ID = "sionna-rt-cuda-m4"
ADAPTER_SCRIPT_PATH = "network/scripts/m4_adapter_runtime.py"
ADAPTER_READY_PATH = "raw/state/adapter.ready.json"
ADAPTER_STOP_PATH = "raw/control/adapter.stop"
WIRE_MINIMAL_MESSAGE_KEYS = {
    "message_type",
    "sender_id",
    "wire_sequence",
    "reconnect_generation",
    "query_id",
}
REQUIRED_SOURCE_PATHS = {
    "doc/network_radio_integration_plan_v3.md",
    "network/config/component_acceptance_profiles.json",
    "network/config/dependency_lock.yaml",
    "network/config/endpoints.yaml",
    "network/config/endpoint_matrix_5uav.json",
    "network/config/endpoint_transaction_schema.json",
    "network/config/jammers_m4_canonical.yaml",
    "network/config/m4_canonical_scene_bundle.json",
    "network/config/qualification_path_ownership.json",
    "network/config/radio_m4_canonical.yaml",
    "network/config/radio_24ghz.yaml",
    "network/config/scenario_m4_canonical.yaml",
    "network/config/sionna_async_protocol_v1.json",
    "network/config/sionna_async_schema_v1.json",
    "network/config/sionna_packet_effects_v1.json",
    "network/ns3/run_ns3_tap_packet_engine.sh",
    "network/ns3/ns3_build_receipt.py",
    "network/ns3/scratch/ams-tap-packet-engine.cc",
    "network/ns3/tap_packet_engine_config.py",
    "network/radio_provider/provider.py",
    "network/radio_provider/sionna_async.py",
    "network/radio_provider/sionna_async_service.py",
    "network/radio_provider/sionna_packet_adapter.py",
    "network/scripts/check_m4_canonical_scene_runtime.py",
    "network/scripts/collect_m4_clock_correlations.py",
    "network/scripts/collect_flight_capacity.py",
    "network/scripts/collect_m4_runtime.py",
    "network/bridge/actual_sitl_mavlink_endpoint.py",
    "network/bridge/opaque_udp_relay.py",
    "network/scripts/actual_sitl_control_probe.py",
    "network/scripts/actual_sitl_endpoint_orchestrator.py",
    "network/scripts/actual_sitl_stack_orchestrator.sh",
    "network/scripts/m3_external_matrix_probe.py",
    "network/scripts/m3_topology_monitor.py",
    "network/scripts/m4_adapter_runtime.py",
    "network/scripts/m4_capacity_airborne.py",
    "network/bridge/runtime_clock_beacon.py",
    "network/scripts/m4_endpoint_agent.py",
    "network/scripts/m4_gazebo_pose_source.py",
    "network/scripts/m4_runtime_orchestrator.py",
    "network/scripts/run_m4_capacity.sh",
    "network/scripts/raw_packet_capture.py",
    "network/scripts/validate_m4_capacity.py",
    "network/scripts/write_run_provenance.py",
    "network/validation/m4_airborne_motion.py",
    "network/validation/m4_capacity_budget.py",
    "network/validation/component_profiles.py",
    "network/validation/endpoint_transaction.py",
    "network/validation/m4_frame_alignment.py",
    "network/validation/m4_pose_observations.py",
    "network/validation/m4_common.py",
    "network/validation/m4_runtime.py",
    "network/validation/qualification_identity.py",
    "network/validation/validate_m4_capacity.py",
    "network/validation/validate_m3_external_matrix.py",
    "network/validation/validate_m4_causality.py",
    "network/validation/validate_m4_scene_bundle.py",
}


def _runtime_process_samples(run_dir: Path) -> tuple[dict[str, list[dict[str, Any]]], int]:
    """Return roles from one process set proven identical across all samples."""

    events = strict_jsonl(run_dir / "logs/m4_runtime_events.jsonl", max_line_bytes=2 * 1024 * 1024)
    samples = [
        record
        for record in events
        if record.get("event")
        in {"measurement_resource_sample", "causal_resource_sample"}
    ]
    if not samples:
        raise M4ValidationError("runtime has no continuous process sample")

    def identity(record: Mapping[str, Any]) -> tuple[Any, ...]:
        return (
            record.get("pid"),
            record.get("start_ticks"),
            record.get("pgid"),
            record.get("role"),
            record.get("executable_path"),
            record.get("executable_sha256"),
            record.get("cmdline_sha256"),
        )

    frozen: set[tuple[Any, ...]] | None = None
    frozen_records: list[dict[str, Any]] = []
    for number, sample in enumerate(samples, start=1):
        process = sample.get("processes")
        records = process.get("processes") if isinstance(process, dict) else None
        if not isinstance(records, list) or not records:
            raise M4ValidationError(f"runtime process sample {number} is empty")
        identities = {identity(record) for record in records if isinstance(record, dict)}
        if len(identities) != len(records):
            raise M4ValidationError(f"runtime process sample {number} is ambiguous")
        if frozen is None:
            frozen = identities
            frozen_records = records
        elif identities != frozen:
            raise M4ValidationError(f"runtime process identity changed at sample {number}")
    roles: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in frozen_records:
        roles[str(record.get("role"))].append(record)
    return dict(roles), len(samples)


def _exact_wire_occurrences(
    directory: Path,
    validated_wire: Mapping[str, Any] | None,
    *,
    label: str,
    expected_envelope: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    """Stream exact wire bytes and retain only correlation-sized metadata."""

    failures: list[str] = []
    occurrences: list[dict[str, Any]] = []
    message_counts: dict[str, int] = defaultdict(int)
    retained_payload_bytes = 0
    maximum_retained_message_bytes = 0
    expected_offset = 0

    def unique_object(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise M4ValidationError(f"duplicate JSON key {key!r}")
            value[key] = item
        return value

    try:
        data_path = directory / "sionna_async_wire.bin"
        index_path = directory / "sionna_async_wire_index.jsonl"
        if not regular_file(data_path):
            raise M4ValidationError(f"{label} wire data is missing/nonregular")
        if not regular_file(index_path):
            raise M4ValidationError(f"{label} wire index is missing/nonregular")
        validated_messages = (
            validated_wire.get("message_by_hash")
            if isinstance(validated_wire, Mapping)
            else None
        )
        if not isinstance(validated_messages, dict):
            validated_messages = None
        saw_record = False
        with data_path.open("rb") as data_stream, index_path.open("rb") as index_stream:
            ordinal = 0
            while True:
                line = index_stream.readline(16_385)
                if not line:
                    break
                ordinal += 1
                saw_record = True
                if (
                    not line.endswith(b"\n")
                    or len(line) > 16_384
                    or not line.rstrip(b"\r\n")
                ):
                    raise M4ValidationError(
                        f"{label} wire index record {ordinal} is not a bounded line"
                    )
                record = json.loads(
                    line.decode("utf-8"), object_pairs_hook=unique_object
                )
                if not isinstance(record, dict):
                    raise M4ValidationError(
                        f"{label} wire index record {ordinal} is not an object"
                    )
                record_failures = exact_keys(
                    record,
                    {
                        "connection_id",
                        "direction",
                        "length",
                        "monotonic_ns",
                        "offset",
                        "sha256",
                    },
                    f"{label} wire index record {ordinal}",
                )
                if record_failures:
                    failures.extend(record_failures)
                    raise M4ValidationError(
                        f"{label} wire occurrence {ordinal} envelope differs"
                    )
                offset = record.get("offset")
                length = record.get("length")
                digest = record.get("sha256")
                observed_ns = record.get("monotonic_ns")
                connection_id = record.get("connection_id")
                direction = record.get("direction")
                if (
                    isinstance(offset, bool)
                    or not isinstance(offset, int)
                    or offset != expected_offset
                    or isinstance(length, bool)
                    or not isinstance(length, int)
                    or not 1 <= length <= 1_048_576
                    or not isinstance(digest, str)
                    or HEX64.fullmatch(digest) is None
                    or isinstance(observed_ns, bool)
                    or not isinstance(observed_ns, int)
                    or observed_ns <= 0
                    or not isinstance(connection_id, str)
                    or not connection_id
                    or direction not in {"inbound", "outbound"}
                ):
                    raise M4ValidationError(
                        f"{label} wire occurrence {ordinal} envelope differs"
                    )
                raw = data_stream.read(length)
                if len(raw) != length:
                    raise M4ValidationError(
                        f"{label} wire occurrence {ordinal} is truncated"
                    )
                expected_offset += length
                if hashlib.sha256(raw).hexdigest() != digest:
                    failures.append(
                        f"{label} wire occurrence {ordinal} bytes/hash differ"
                    )
                    continue
                cached = (
                    validated_messages.get(digest)
                    if validated_messages is not None
                    else None
                )
                try:
                    message = (
                        dict(cached)
                        if isinstance(cached, dict)
                        else dict(decode_message(raw, max_bytes=1_048_576))
                    )
                except (ProtocolValidationError, TypeError, ValueError) as exc:
                    failures.append(
                        f"{label} wire occurrence {ordinal} schema differs: {exc}"
                    )
                    continue
                generation = message.get("reconnect_generation")
                if (
                    isinstance(generation, bool)
                    or not isinstance(generation, int)
                    or generation < 0
                ):
                    failures.append(
                        f"{label} wire occurrence {ordinal} reconnect generation differs"
                    )
                    continue
                if expected_envelope is not None and any(
                    message.get(key) != expected
                    for key, expected in expected_envelope.items()
                ):
                    failures.append(
                        f"{label} wire occurrence {ordinal} "
                        "is not bound to the current contract/config"
                    )
                message_type = message.get("message_type")
                message_counts[str(message_type)] += 1
                retained_message = (
                    message
                    if message_type in {"hello", "ready"}
                    else {
                        key: message[key]
                        for key in WIRE_MINIMAL_MESSAGE_KEYS
                        if key in message
                    }
                )
                retained_size = len(canonical_json(retained_message))
                retained_payload_bytes += retained_size
                maximum_retained_message_bytes = max(
                    maximum_retained_message_bytes, retained_size
                )
                occurrences.append(
                    {
                        "ordinal": ordinal,
                        "connection_id": connection_id,
                        "direction": direction,
                        "monotonic_ns": observed_ns,
                        "length": length,
                        "sha256": digest,
                        "message": retained_message,
                        "generation": generation,
                    }
                )
            if not saw_record:
                raise M4ValidationError(f"{label} wire index is empty")
            if data_stream.read(1):
                failures.append(
                    f"{label} wire index does not cover its exact byte stream"
                )
        for required in ("hello", "ready", "query", "result"):
            if message_counts.get(required, 0) == 0:
                failures.append(f"{label} wire log has no {required} frame")
    except (OSError, TypeError, UnicodeError, ValueError, M4ValidationError) as exc:
        failures.append(f"{label} wire occurrences cannot be recovered: {exc}")
    details = {
        "wire_records": sum(message_counts.values()),
        "wire_bytes": expected_offset,
        "message_counts": dict(sorted(message_counts.items())),
        "retained_message_payload_bytes": retained_payload_bytes,
        "maximum_retained_message_bytes": maximum_retained_message_bytes,
        "streamed_binary_and_index": True,
    }
    return occurrences, details, failures


def _correlate_exact_peer_wire(
    client_directory: Path,
    provider_directory: Path,
    client_wire: Mapping[str, Any] | None,
    *,
    expected_envelope: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str], list[dict[str, Any]], list[dict[str, Any]]]:
    """Prove that each TCP frame was observed once, in order, at both peers."""

    failures: list[str] = []
    client, client_scan, client_failures = _exact_wire_occurrences(
        client_directory,
        client_wire,
        label="client",
        expected_envelope=expected_envelope,
    )
    provider, provider_scan, provider_failures = _exact_wire_occurrences(
        provider_directory,
        None,
        label="provider",
        expected_envelope=expected_envelope,
    )
    failures.extend(client_failures)
    failures.extend(provider_failures)

    side_cardinality: dict[str, dict[str, int]] = {}
    for side_label, records in (("client", client), ("provider", provider)):
        sender_sequences: set[tuple[str, int]] = set()
        query_ids: dict[str, int] = defaultdict(int)
        result_ids: dict[str, int] = defaultdict(int)
        for record in records:
            message = record["message"]
            sender_id = message.get("sender_id")
            wire_sequence = message.get("wire_sequence")
            if (
                not isinstance(sender_id, str)
                or not sender_id
                or isinstance(wire_sequence, bool)
                or not isinstance(wire_sequence, int)
                or wire_sequence <= 0
            ):
                failures.append(
                    f"{side_label} wire occurrence {record['ordinal']} sender sequence differs"
                )
            else:
                sender_sequence = (sender_id, wire_sequence)
                if sender_sequence in sender_sequences:
                    failures.append(
                        f"{side_label} wire repeats sender/wire_sequence {sender_sequence}"
                    )
                sender_sequences.add(sender_sequence)

            message_type = message.get("message_type")
            if message_type not in {"query", "result"}:
                continue
            query_id = message.get("query_id")
            if not isinstance(query_id, str) or not query_id:
                failures.append(
                    f"{side_label} {message_type} occurrence {record['ordinal']} "
                    "has invalid query_id"
                )
                continue
            target = query_ids if message_type == "query" else result_ids
            target[query_id] += 1

        for message_type, identifiers in (
            ("query", query_ids),
            ("result", result_ids),
        ):
            for query_id, count in identifiers.items():
                if count != 1:
                    failures.append(
                        f"{side_label} {message_type} query_id {query_id!r} "
                        f"has {count} exact wire occurrences"
                    )
        orphan_results = sorted(set(result_ids) - set(query_ids))
        if orphan_results:
            failures.append(
                f"{side_label} wire has orphan result query_ids {orphan_results}"
            )
        side_cardinality[side_label] = {
            "unique_sender_sequence_count": len(sender_sequences),
            "unique_query_id_count": len(query_ids),
            "unique_result_query_id_count": len(result_ids),
        }

    client_out = [item for item in client if item["direction"] == "outbound"]
    client_in = [item for item in client if item["direction"] == "inbound"]
    provider_out = [item for item in provider if item["direction"] == "outbound"]
    provider_in = [item for item in provider if item["direction"] == "inbound"]

    def exact_peer_sequence(
        sent: list[dict[str, Any]],
        received: list[dict[str, Any]],
        label: str,
    ) -> None:
        if len(sent) != len(received):
            failures.append(
                f"{label} occurrence count differs: sent={len(sent)} received={len(received)}"
            )
        for ordinal, (source, destination) in enumerate(
            zip(sent, received), start=1
        ):
            if (
                source["sha256"] != destination["sha256"]
                or source["length"] != destination["length"]
                or source["generation"] != destination["generation"]
            ):
                failures.append(f"{label} occurrence {ordinal} bytes/order differ")

    exact_peer_sequence(client_out, provider_in, "client-to-provider")
    exact_peer_sequence(provider_out, client_in, "provider-to-client")

    generation_sets: dict[str, set[int]] = {}
    for side_label, records in (("client", client), ("provider", provider)):
        by_connection: dict[str, set[int]] = defaultdict(set)
        by_generation: dict[int, set[str]] = defaultdict(set)
        for record in records:
            by_connection[record["connection_id"]].add(record["generation"])
            by_generation[record["generation"]].add(record["connection_id"])
        if any(len(values) != 1 for values in by_connection.values()) or any(
            len(values) != 1 for values in by_generation.values()
        ):
            failures.append(f"{side_label} wire connection/generation map is ambiguous")
        generation_sets[side_label] = set(by_generation)
    if generation_sets.get("client") != generation_sets.get("provider"):
        failures.append("client/provider reconnect generation sets differ")
    if generation_sets.get("client") != {0}:
        failures.append("formal capacity wire must use exactly reconnect_generation 0")

    expected_handshake_prefixes = {
        "client": [
            ("inbound", "hello", PROVIDER_SENDER_ID),
            ("inbound", "ready", PROVIDER_SENDER_ID),
            ("outbound", "hello", ADAPTER_SENDER_ID),
            ("outbound", "ready", ADAPTER_SENDER_ID),
        ],
        "provider": [
            ("outbound", "hello", PROVIDER_SENDER_ID),
            ("outbound", "ready", PROVIDER_SENDER_ID),
            ("inbound", "hello", ADAPTER_SENDER_ID),
            ("inbound", "ready", ADAPTER_SENDER_ID),
        ],
    }
    for side_label, records in (("client", client), ("provider", provider)):
        observed_prefix = [
            (
                item["direction"],
                item["message"].get("message_type"),
                item["message"].get("sender_id"),
            )
            for item in records[:4]
        ]
        if observed_prefix != expected_handshake_prefixes[side_label]:
            failures.append(f"{side_label} exact peer handshake prefix differs")

    allowed_by_origin = {
        ADAPTER_SENDER_ID: {"hello", "ready", "query"},
        PROVIDER_SENDER_ID: {"hello", "ready", "result"},
    }
    for side_label, records, direction, sender_id in (
        ("client", client, "outbound", ADAPTER_SENDER_ID),
        ("provider", provider, "outbound", PROVIDER_SENDER_ID),
    ):
        origin = [item for item in records if item["direction"] == direction]
        for record in origin:
            message = record["message"]
            if (
                message.get("sender_id") != sender_id
                or message.get("message_type") not in allowed_by_origin[sender_id]
            ):
                failures.append(
                    f"{side_label} outbound occurrence {record['ordinal']} has wrong origin/type"
                )
        for generation in sorted(generation_sets.get(side_label, set())):
            lifecycle = [
                item["message"].get("message_type")
                for item in origin
                if item["generation"] == generation
            ]
            if (
                lifecycle[:2] != ["hello", "ready"]
                or lifecycle.count("hello") != 1
                or lifecycle.count("ready") != 1
            ):
                failures.append(
                    f"{side_label} generation {generation} handshake order/count differs"
                )

    details = {
        "client_wire_occurrence_count": len(client),
        "provider_wire_occurrence_count": len(provider),
        "client_to_provider_occurrence_count": len(client_out),
        "provider_to_client_occurrence_count": len(provider_out),
        "reconnect_generations": sorted(generation_sets.get("client", set())),
        "per_side_cardinality": side_cardinality,
        "stream_scans": {
            "client": client_scan,
            "provider": provider_scan,
        },
    }
    return details, failures, client, provider


def _expected_provider_cmdline_sha256(
    run_dir: Path, *, port: int, runtime_id: str
) -> str:
    argv = [
        "python3",
        "-u",
        str((ROOT / PROVIDER_SCRIPT_PATH).resolve()),
        "provider",
        "--run-dir",
        str(run_dir.resolve()),
        "--contract",
        str((run_dir / "raw/m4_capacity_contract.json").resolve()),
        "--port",
        str(port),
        "--ready-file",
        str((run_dir / PROVIDER_READY_PATH).resolve()),
        "--stop-file",
        str((run_dir / PROVIDER_STOP_PATH).resolve()),
        "--clock-socket",
        f"/tmp/ams-m4-clock-{runtime_id}.sock",
    ]
    return hashlib.sha256(
        b"".join(item.encode("utf-8") + b"\0" for item in argv)
    ).hexdigest()


def _expected_adapter_cmdline_sha256(
    run_dir: Path, *, port: int, runtime_id: str
) -> str:
    argv = [
        "python3",
        "-u",
        str((ROOT / ADAPTER_SCRIPT_PATH).resolve()),
        "--run-dir",
        str(run_dir.resolve()),
        "--contract",
        str((run_dir / "raw/m4_capacity_contract.json").resolve()),
        "--packet-events",
        str((run_dir / "logs/ns3_packet_events.jsonl").resolve()),
        "--state-file",
        str((run_dir / "logs/sionna_applied_states.jsonl").resolve()),
        "--ready-file",
        str((run_dir / ADAPTER_READY_PATH).resolve()),
        "--stop-file",
        str((run_dir / ADAPTER_STOP_PATH).resolve()),
        "--control-dir",
        str((run_dir / "raw/control/adapter").resolve()),
        "--clock-socket",
        f"/tmp/ams-m4-clock-{runtime_id}.sock",
        "--provider-port",
        str(port),
    ]
    return hashlib.sha256(
        b"".join(item.encode("utf-8") + b"\0" for item in argv)
    ).hexdigest()


def _validate_real_provider_wire_binding(
    run_dir: Path,
    run: Mapping[str, Any],
    client_wire: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Bind exact peer observations to the sampled real provider process."""

    details: dict[str, Any] = {}
    failures: list[str] = []
    try:
        contract_path = run_dir / "raw/m4_capacity_contract.json"
        contract_hash = sha256_file(contract_path)
        bundle = strict_json(FROZEN_BUNDLE_PATH)
        script_path = (ROOT / PROVIDER_SCRIPT_PATH).resolve(strict=True)
        script_sha256 = sha256_file(script_path)
        source_hashes = run.get("source_sha256")
        if (
            not isinstance(source_hashes, dict)
            or source_hashes.get(PROVIDER_SCRIPT_PATH) != script_sha256
        ):
            failures.append("provider handshake script is not bound by source identity")

        config_material = {
            "async_policy": run["async_policy"],
            "bundle": run["bundle"],
            "limits": run["limits"],
            "profile": run["profile"],
            "radio_sha256": sha256_file(
                ROOT / "network/config/radio_m4_canonical.yaml"
            ),
            "effects_sha256": sha256_file(
                ROOT / "network/config/sionna_packet_effects_v1.json"
            ),
        }
        config_hash = hashlib.sha256(canonical_json(config_material)).hexdigest()
        expected_envelope = {
            "run_id": run.get("run_id"),
            "profile": run.get("profile"),
            "phase_id": "m4_continuous_runtime",
            "contract_hash": contract_hash,
            "config_hash": config_hash,
            "bundle_id": FROZEN_BUNDLE_ID,
            "sender_clock_domain": "host-monotonic",
        }
        (
            correlation_details,
            correlation_failures,
            client_occurrences,
            provider_occurrences,
        ) = _correlate_exact_peer_wire(
            run_dir / "logs",
            run_dir / "logs/provider_wire",
            client_wire,
            expected_envelope=expected_envelope,
        )
        details.update(correlation_details)
        failures.extend(correlation_failures)
        installed_versions = {
            "sionna_rt_version": importlib.metadata.version(SIONNA_RT_DISTRIBUTION),
            "mitsuba_version": importlib.metadata.version("mitsuba"),
        }
        dependency_lock = yaml.safe_load(
            (ROOT / "network/config/dependency_lock.yaml").read_text(encoding="utf-8")
        )
        locked_dependencies = (
            dependency_lock.get("dependencies")
            if isinstance(dependency_lock, dict)
            else None
        )
        locked_versions = {
            "sionna_rt_version": (
                locked_dependencies.get("sionna_rt", {}).get("version")
                if isinstance(locked_dependencies, dict)
                else None
            ),
            "mitsuba_version": (
                locked_dependencies.get("mitsuba", {}).get("version")
                if isinstance(locked_dependencies, dict)
                else None
            ),
        }
        if any(
            not isinstance(locked, (str, int, float))
            or str(locked) != installed_versions[name]
            for name, locked in locked_versions.items()
        ):
            failures.append("installed provider package versions differ from dependency lock")
        expected_versions = installed_versions
        expected_executable = {
            "path": str(script_path),
            "sha256": script_sha256,
        }
        expected_provider_identity = {
            "provider_id": PROVIDER_ID,
            "provider_mode": "real_sionna",
            "acceptance_eligible": True,
            **expected_versions,
        }
        expected_scene = {
            "bundle_id": FROZEN_BUNDLE_ID,
            "scene_manifest_sha256": str(bundle["bundle_sha256"]),
            "scene_path": str((ROOT / str(bundle["sionna_scene_xml"])).resolve()),
        }

        provider_out = [
            item
            for item in provider_occurrences
            if item.get("direction") == "outbound"
        ]
        hellos = [
            item for item in provider_out if item["message"].get("message_type") == "hello"
        ]
        readies = [
            item for item in provider_out if item["message"].get("message_type") == "ready"
        ]
        if len(hellos) != 1 or len(readies) != 1:
            failures.append("provider wire must contain exactly one hello/ready pair")
        else:
            hello = hellos[0]["message"]
            ready_message = readies[0]["message"]
            for label, message, readiness in (
                ("hello", hello, "initializing"),
                ("ready", ready_message, "ready"),
            ):
                if (
                    message.get("sender_id") != PROVIDER_SENDER_ID
                    or message.get("sender_role") != "provider"
                    or message.get("run_id") != run.get("run_id")
                    or message.get("profile") != run.get("profile")
                    or message.get("phase_id") != "m4_continuous_runtime"
                    or message.get("contract_hash") != contract_hash
                    or message.get("config_hash") != config_hash
                    or message.get("bundle_id") != FROZEN_BUNDLE_ID
                    or message.get("reconnect_generation") != 0
                    or message.get("sender_clock_domain") != "host-monotonic"
                    or message.get("protocol_name") != "sionna_async"
                    or message.get("protocol_version") != 1
                    or message.get("accepted_run_id") != run.get("run_id")
                    or message.get("accepted_config_hash") != config_hash
                    or message.get("accepted_bundle_id") != FROZEN_BUNDLE_ID
                    or message.get("readiness_state") != readiness
                    or message.get("executable_identity") != expected_executable
                    or message.get("provider_identity") != expected_provider_identity
                ):
                    failures.append(f"provider {label} process/package identity differs")
            if ready_message.get("scene_identity") != expected_scene:
                failures.append("provider ready canonical scene identity differs")

        adapter_path = (ROOT / ADAPTER_SCRIPT_PATH).resolve(strict=True)
        adapter_sha256 = sha256_file(adapter_path)
        if (
            not isinstance(source_hashes, dict)
            or source_hashes.get(ADAPTER_SCRIPT_PATH) != adapter_sha256
        ):
            failures.append("adapter handshake script is not bound by source identity")
        adapter_out = [
            item
            for item in client_occurrences
            if item.get("direction") == "outbound"
        ]
        adapter_hellos = [
            item for item in adapter_out if item["message"].get("message_type") == "hello"
        ]
        adapter_readies = [
            item for item in adapter_out if item["message"].get("message_type") == "ready"
        ]
        if len(adapter_hellos) != 1 or len(adapter_readies) != 1:
            failures.append("adapter wire must contain exactly one hello/ready pair")
        else:
            adapter_executable = {
                "path": str(adapter_path),
                "sha256": adapter_sha256,
            }
            for label, item, readiness in (
                ("hello", adapter_hellos[0]["message"], "initializing"),
                ("ready", adapter_readies[0]["message"], "ready"),
            ):
                if (
                    item.get("sender_id") != ADAPTER_SENDER_ID
                    or item.get("sender_role") != "adapter"
                    or item.get("protocol_name") != "sionna_async"
                    or item.get("protocol_version") != 1
                    or item.get("accepted_run_id") != run.get("run_id")
                    or item.get("accepted_config_hash") != config_hash
                    or item.get("accepted_bundle_id") != FROZEN_BUNDLE_ID
                    or item.get("readiness_state") != readiness
                    or item.get("executable_identity") != adapter_executable
                ):
                    failures.append(f"adapter {label} executable/contract identity differs")
            if adapter_readies[0]["message"].get("scene_identity") != expected_scene:
                failures.append("adapter ready canonical scene identity differs")

        ready_path = run_dir / PROVIDER_READY_PATH
        ready = strict_json(ready_path)
        failures.extend(
            exact_keys(
                ready,
                {
                    "pid",
                    "port",
                    "monotonic_ns",
                    "provider_mode",
                    "bundle_sha256",
                    "run_id",
                },
                "provider readiness",
            )
        )
        ready_pid = ready.get("pid")
        port = ready.get("port")
        ready_ns = ready.get("monotonic_ns")
        if (
            isinstance(ready_pid, bool)
            or not isinstance(ready_pid, int)
            or ready_pid <= 1
            or isinstance(port, bool)
            or not isinstance(port, int)
            or not 1 <= port <= 65_535
            or isinstance(ready_ns, bool)
            or not isinstance(ready_ns, int)
            or ready_ns <= 0
            or ready.get("provider_mode") != "real_sionna"
            or ready.get("bundle_sha256") != bundle.get("bundle_sha256")
            or ready.get("run_id") != run.get("run_id")
        ):
            failures.append("provider readiness identity differs")

        roles, sample_count = _runtime_process_samples(run_dir)
        identity = run.get("identity")
        manifest = identity.get("executable_manifest") if isinstance(identity, dict) else None
        python_executable = manifest.get("python") if isinstance(manifest, dict) else None
        workers = roles.get("sionna_worker", [])
        if len(workers) != 1:
            failures.append("runtime must contain exactly one frozen sionna_worker")
        elif isinstance(port, int) and not isinstance(port, bool):
            worker = workers[0]
            expected_cmdline_sha256 = _expected_provider_cmdline_sha256(
                run_dir, port=port, runtime_id=str(run.get("runtime_id"))
            )
            if (
                not isinstance(python_executable, dict)
                or worker.get("pid") != ready_pid
                or worker.get("role") != "sionna_worker"
                or worker.get("state") == "Z"
                or worker.get("executable_path") != python_executable.get("path")
                or worker.get("executable_sha256") != python_executable.get("sha256")
                or worker.get("cmdline_sha256") != expected_cmdline_sha256
            ):
                failures.append("provider handshake is not bound to the exact sampled process")
            details.update(
                {
                    "provider_pid": worker.get("pid"),
                    "provider_process_sample_count": sample_count,
                    "provider_process_executable_path": worker.get("executable_path"),
                    "provider_cmdline_sha256": worker.get("cmdline_sha256"),
                }
            )

        adapter_ready_path = run_dir / ADAPTER_READY_PATH
        adapter_ready = strict_json(adapter_ready_path)
        failures.extend(
            exact_keys(
                adapter_ready,
                {
                    "pid",
                    "monotonic_ns",
                    "run_id",
                    "runtime_id",
                    "provider_mode",
                    "pose_entities",
                },
                "adapter readiness",
            )
        )
        adapter_ready_pid = adapter_ready.get("pid")
        adapter_ready_ns = adapter_ready.get("monotonic_ns")
        if (
            isinstance(adapter_ready_pid, bool)
            or not isinstance(adapter_ready_pid, int)
            or adapter_ready_pid <= 1
            or isinstance(adapter_ready_ns, bool)
            or not isinstance(adapter_ready_ns, int)
            or adapter_ready_ns <= 0
            or adapter_ready.get("run_id") != run.get("run_id")
            or adapter_ready.get("runtime_id") != run.get("runtime_id")
            or adapter_ready.get("provider_mode") != "real_sionna"
            or adapter_ready.get("pose_entities")
            != ["cp", "uav1", "uav2", "uav3", "uav4", "uav5", "jammer_m4"]
        ):
            failures.append("adapter readiness identity differs")

        adapters = roles.get("sionna_adapter", [])
        if len(adapters) != 1:
            failures.append("runtime must contain exactly one frozen sionna_adapter")
        elif isinstance(port, int) and not isinstance(port, bool):
            adapter_process = adapters[0]
            expected_adapter_cmdline_sha256 = _expected_adapter_cmdline_sha256(
                run_dir, port=port, runtime_id=str(run.get("runtime_id"))
            )
            if (
                not isinstance(python_executable, dict)
                or adapter_process.get("pid") != adapter_ready_pid
                or adapter_process.get("role") != "sionna_adapter"
                or adapter_process.get("state") == "Z"
                or adapter_process.get("executable_path")
                != python_executable.get("path")
                or adapter_process.get("executable_sha256")
                != python_executable.get("sha256")
                or adapter_process.get("cmdline_sha256")
                != expected_adapter_cmdline_sha256
            ):
                failures.append(
                    "adapter handshake is not bound to the exact sampled process"
                )
            details.update(
                {
                    "adapter_pid": adapter_process.get("pid"),
                    "adapter_process_sample_count": sample_count,
                    "adapter_process_executable_path": adapter_process.get(
                        "executable_path"
                    ),
                    "adapter_cmdline_sha256": adapter_process.get(
                        "cmdline_sha256"
                    ),
                }
            )

        adapter_handshake_records = [*adapter_hellos, *adapter_readies]
        if (
            adapter_handshake_records
            and isinstance(adapter_ready_ns, int)
            and not isinstance(adapter_ready_ns, bool)
            and adapter_ready_ns
            < max(item["monotonic_ns"] for item in adapter_handshake_records)
        ):
            failures.append("adapter process readiness predates its exact handshake")

        if provider_out and isinstance(ready_ns, int) and not isinstance(ready_ns, bool):
            first_provider_wire_ns = min(item["monotonic_ns"] for item in provider_out)
            if first_provider_wire_ns < ready_ns:
                failures.append("provider wire predates provider process readiness")

        details.update(
            {
                "provider_script_path": str(script_path),
                "provider_script_sha256": script_sha256,
                "provider_versions": expected_versions,
                "provider_scene_identity": expected_scene,
            }
        )
    except (
        KeyError,
        OSError,
        TypeError,
        ValueError,
        importlib.metadata.PackageNotFoundError,
        yaml.YAMLError,
        M4ValidationError,
    ) as exc:
        failures.append(f"real provider process/handshake binding cannot be proven: {exc}")
    return details, failures


def _safe_relative(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise M4ValidationError(f"{label} is not a relative path")
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise M4ValidationError(f"{label} is unsafe")
    return value


def _expected_actual_control_api() -> dict[str, Any]:
    """Return the one Q3 API shape that a formal Q4 run may consume.

    This is deliberately derived from the repository-owned matrix and source
    bytes.  A copied M3 receipt is authority for the API, but it cannot make a
    stale matrix, stale adapter, or locally invented tail layout acceptable.
    """

    matrix_path = "network/config/endpoint_matrix_5uav.json"
    schema_path = "network/config/endpoint_transaction_schema.json"
    probe_path = "network/scripts/actual_sitl_control_probe.py"
    adapter_path = "network/bridge/actual_sitl_mavlink_endpoint.py"
    relay_path = "network/bridge/opaque_udp_relay.py"
    return {
        "contract": ACTUAL_CONTROL_API_CONTRACT,
        "control_endpoint_form": ACTUAL_CONTROL_ENDPOINT_FORM,
        "matrix_path": matrix_path,
        "matrix_sha256": sha256_file(ROOT / matrix_path),
        "endpoint_schema_path": schema_path,
        "endpoint_schema_sha256": sha256_file(ROOT / schema_path),
        "event_schema": ACTUAL_CONTROL_EVENT_SCHEMA,
        "probe_source": {
            "path": probe_path,
            "sha256": sha256_file(ROOT / probe_path),
        },
        "adapter_source": {
            "path": adapter_path,
            "sha256": sha256_file(ROOT / adapter_path),
        },
        "relay_core_source": {
            "path": relay_path,
            "sha256": sha256_file(ROOT / relay_path),
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
                "run_contract": "ams.m3.external_matrix_run/v1",
                "run_nonce_hex_length": 32,
                "transport_nonce32_derivation": "identity/full_run_nonce32",
            },
            "m4_capacity": {
                "run_contract": RUN_CONTRACT,
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
            "per_uav_keys": ["uav1", "uav2", "uav3", "uav4", "uav5"],
            "response_policy_values_by_profile": {
                "m3": ["ack_required", "timeout_required"],
                "m4_capacity": ["ack_required", "timeout_required"],
                "m4_causality": [
                    "correlated_timesync_required",
                    "timeout_required",
                ],
            },
            "send_slot_formula": (
                "start_monotonic_ns + "
                "((ordinal_send_slot - 1) * send_span_ms * 1000000) // "
                "max(1, offered_per_uav - 1)"
            ),
            "pending_per_uav": {
                "ack_required": {"mode": "single", "maximum": 1},
                "timeout_required": {"mode": "single", "maximum": 1},
                "correlated_timesync_required": {
                    "mode": "bounded",
                    "maximum_formula": (
                        "offered_per_uav == 1 ? 1 : min(offered_per_uav, "
                        "ceil(timeout_ns / ((send_span_ms * 1000000) // "
                        "(offered_per_uav - 1))))"
                    ),
                },
            },
            "timeout_ns": 3_000_000_000,
            "guard_scope": "per_uav_active_timeout_batch_with_append_only_history",
        },
        "channels": {
            f"uav{index}": {
                "system_id": index,
                "instance": index - 1,
                "radio_bind": {
                    "host": f"10.71.{index}.10",
                    "port": 14600 + index,
                },
                "gcs_peer": {"host": "10.71.0.10", "port": 14600},
                "tail_root": {"host": f"10.72.{index}.1", "prefixlen": 30},
                "tail_uav": {
                    "host": f"10.72.{index}.2",
                    "prefixlen": 30,
                    "port": 14559 + index,
                },
                "master": {
                    "host": "127.0.0.1",
                    "port": 5760 + 10 * (index - 1),
                },
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


def _accepted_m3_actual_control_api(
    run_dir: Path, run: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Load and byte-bind the accepted M3 actual-control API.

    Q4 is a consumer of the host-final M3 result.  It must never reconstruct
    an endpoint contract from labels in its own runner or accept a bare M3
    result copied without its immutable host-final receipt.
    """

    failures: list[str] = []
    details: dict[str, Any] = {}
    accepted: dict[str, Any] = {}
    try:
        binding = run.get("endpoint_path")
        expected_binding_keys = {
            "mode",
            "acceptance_eligible",
            "traffic_origin",
            "accepted_m3_receipt_path",
            "accepted_m3_receipt_sha256",
            "actual_control_api_contract",
            "actual_control_api_sha256",
            "actual_sitl_manifest_path",
            "actual_sitl_ready_path",
            "actual_control_events_path",
        }
        binding_failures = exact_keys(
            binding, expected_binding_keys, "actual-control API binding"
        )
        if binding_failures:
            raise M4ValidationError("; ".join(binding_failures))
        if not isinstance(binding, dict):
            raise M4ValidationError("actual-control API binding is not an object")
        receipt_relative = _safe_relative(
            binding.get("accepted_m3_receipt_path"), "accepted M3 receipt path"
        )
        if receipt_relative != "raw/prerequisites/m3.json":
            raise M4ValidationError("accepted M3 receipt path differs")
        receipt_path = run_dir / receipt_relative
        receipt_sha256 = sha256_file(receipt_path)
        receipt = strict_json(receipt_path)
        identity = run.get("identity")
        source_commit = (
            identity.get("source_commit") if isinstance(identity, dict) else None
        )
        result = receipt.get("result")
        if (
            receipt.get("contract") != M3_RECEIPT_CONTRACT
            or receipt.get("profile") != "m3_component"
            or not isinstance(receipt.get("source_commit"), str)
            or HEX40.fullmatch(receipt["source_commit"]) is None
            or receipt.get("formal_accepted") is not True
            or receipt.get("passed") is not True
            or receipt.get("failures") != []
            or receipt.get("result_contract") != M3_RESULT_CONTRACT
            or not isinstance(result, dict)
            or result.get("contract") != M3_RESULT_CONTRACT
            or result.get("passed") is not True
            or result.get("acceptance_eligible") is not True
            or result.get("failures") != []
        ):
            raise M4ValidationError("accepted M3 host-final receipt/result is invalid")
        api = result.get("actual_control_api")
        expected_api = _expected_actual_control_api()
        if api != expected_api:
            raise M4ValidationError(
                "accepted M3 actual_control_api differs from the frozen Q3 API"
            )
        api_sha256 = hashlib.sha256(canonical_json(api)).hexdigest()
        expected_binding = {
            "mode": ACTUAL_CONTROL_ENDPOINT_FORM,
            "acceptance_eligible": True,
            "traffic_origin": "actual_ardupilot_mavproxy",
            "accepted_m3_receipt_path": receipt_relative,
            "accepted_m3_receipt_sha256": receipt_sha256,
            "actual_control_api_contract": ACTUAL_CONTROL_API_CONTRACT,
            "actual_control_api_sha256": api_sha256,
            "actual_sitl_manifest_path": "raw/actual_sitl_endpoint_manifest.json",
            "actual_sitl_ready_path": "raw/state/actual-sitl-endpoints.ready.json",
            "actual_control_events_path": "raw/actual_control/events.jsonl",
        }
        if binding != expected_binding:
            raise M4ValidationError(
                "run endpoint_path does not exactly bind the accepted M3 actual-control API"
            )
        workload = run.get("workload")
        if isinstance(workload, dict) and (
            workload.get("accepted_m3_receipt_path") != receipt_relative
            or workload.get("accepted_m3_receipt_sha256") != receipt_sha256
        ):
            raise M4ValidationError("workload and endpoint path bind different M3 receipts")
        prerequisite_manifest = strict_json(run_dir / "raw/prerequisites.json")
        m3_record = prerequisite_manifest.get("receipts", {}).get("m3")
        expected_profile = str(run.get("profile"))
        status = prerequisite_manifest.get("status")
        if (
            prerequisite_manifest.get("contract") != "ams.component-prerequisites/v1"
            or prerequisite_manifest.get("profile") != expected_profile
            or not isinstance(source_commit, str)
            or HEX40.fullmatch(source_commit) is None
            or prerequisite_manifest.get("source_commit") != source_commit
            or not isinstance(status, dict)
            or status.get("contract") != "ams.live-status/v4"
            or status.get("closed_count") != 4
            or set(prerequisite_manifest.get("receipts", {}))
            != {"m0", "m1", "m2", "m3"}
            or not isinstance(m3_record, dict)
            or m3_record.get("milestone") != "M3"
            or m3_record.get("contract") != M3_RECEIPT_CONTRACT
            or m3_record.get("run_id") != receipt.get("run_id")
            or m3_record.get("sha256") != receipt_sha256
        ):
            raise M4ValidationError(
                "status-authorized prerequisite manifest and endpoint path bind different M3 authority"
            )
        accepted = api
        details = {
            "accepted_m3_receipt_sha256": receipt_sha256,
            "actual_control_api_sha256": api_sha256,
            "control_endpoint_form": api["control_endpoint_form"],
            "matrix_sha256": api["matrix_sha256"],
            "channel_count": len(api["channels"]),
            "tail_prefixlen": 30,
            "tail_ports": [
                api["channels"][f"uav{index}"]["tail_uav"]["port"]
                for index in range(1, 6)
            ],
        }
    except (KeyError, OSError, TypeError, ValueError, M4ValidationError) as exc:
        failures.append(f"accepted M3 actual-control API cannot be bound: {exc}")
    return accepted, details, failures


def _runtime_matches_frozen_process(
    expected: Mapping[str, Any], records: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Match a Q3 process identity to the independently sampled Q4 process."""

    return [
        record
        for record in records
        if (
            record.get("pid"),
            record.get("start_ticks"),
            record.get("pgid"),
            record.get("executable_path"),
            record.get("executable_sha256"),
            record.get("cmdline_sha256"),
        )
        == (
            expected.get("pid"),
            expected.get("start_ticks"),
            expected.get("pgid"),
            expected.get("exe_path"),
            expected.get("exe_sha256"),
            expected.get("cmdline_sha256"),
        )
    ]


def _actual_control_event_audit(
    path: Path,
    *,
    run: Mapping[str, Any],
    api: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Validate the sole GCS actual-control audit and its ten matrix cells."""

    failures: list[str] = []
    details: dict[str, Any] = {}
    try:
        records = strict_jsonl(path, max_line_bytes=2 * 1024 * 1024)
        raw_lines = path.read_bytes().splitlines(keepends=True)
        if len(raw_lines) != len(records) or any(
            canonical_json(record) != raw
            for record, raw in zip(records, raw_lines)
        ):
            raise M4ValidationError("actual-control audit is not canonical JSONL")
        profile = (
            "m4_capacity"
            if run.get("profile") == "m4_capacity_prerequisite"
            else "m4_causality"
            if run.get("profile") == "m4_component"
            else None
        )
        run_nonce = str(run.get("run_nonce"))
        if profile is None or HEX64.fullmatch(run_nonce) is None:
            raise M4ValidationError("actual-control run profile/nonce differs")
        transport_nonce32 = hashlib.sha256(bytes.fromhex(run_nonce)).hexdigest()[:32]
        previous_hash: str | None = None
        previous_ns = 0
        forbidden = {
            "control_parse_error",
            "foreign_control_message",
            "uncorrelated_control_response",
            "late_stopped_control_response",
            "forbidden_stopped_control_response",
            "phase_ended_before_outcome_timeout",
        }
        event_counts: dict[str, int] = defaultdict(int)
        downlink_cells: set[str] = set()
        uplink_cells: set[str] = set()
        request_hashes: dict[str, set[str]] = defaultdict(set)
        delivered_request_hashes: dict[str, set[str]] = defaultdict(set)
        response_hashes: dict[str, set[str]] = defaultdict(set)
        for sequence, (record, raw) in enumerate(zip(records, raw_lines), start=1):
            timestamp = record.get("monotonic_ns")
            if (
                record.get("schema") != api.get("event_schema")
                or record.get("run_id") != run.get("run_id")
                or record.get("runtime_id") != run.get("runtime_id")
                or record.get("run_nonce") != run_nonce
                or record.get("profile") != profile
                or record.get("transport_nonce32") != transport_nonce32
                or record.get("transport_nonce_derivation")
                != "sha256(raw_full_run_nonce64)[:32]"
                or record.get("role_subject")
                != api.get("process_role_ids", {}).get("gcs")
                or record.get("event_sequence") != sequence
                or record.get("previous_record_sha256") != previous_hash
                or isinstance(timestamp, bool)
                or not isinstance(timestamp, int)
                or timestamp <= previous_ns
            ):
                raise M4ValidationError(
                    f"actual-control audit identity/hash chain differs at {sequence}"
                )
            previous_hash = hashlib.sha256(raw).hexdigest()
            previous_ns = timestamp
            event = str(record.get("event"))
            event_counts[event] += 1
            if event in forbidden:
                raise M4ValidationError(f"actual-control audit failed closed: {event}")
            if record.get("producer_kind") == "synthetic_matrix_fixture":
                raise M4ValidationError("synthetic producer appears in actual-control audit")
            if event == "actual_control_socket_ready" and (
                record.get("bound_socket") != ["10.71.0.10", 14600]
                or record.get("full_run_nonce") != run_nonce
                or record.get("transmit_ip_tos") != 184
                or record.get("receive_ip_tos_enabled") != 1
            ):
                raise M4ValidationError("actual-control GCS socket identity differs")
            if event == "real_command_offered":
                uav = record.get("uav")
                if isinstance(uav, bool) or not isinstance(uav, int) or not 1 <= uav <= 5:
                    raise M4ValidationError("actual-control offer UAV differs")
                cell_id = f"uav{uav}.control.downlink"
                digest = record.get("command_frame_sha256")
                if (
                    record.get("endpoint_form") != api.get("control_endpoint_form")
                    or record.get("cell_id") != cell_id
                    or record.get("source_ip") != "10.71.0.10"
                    or record.get("source_udp_port") != 14600
                    or record.get("destination_ip") != f"10.71.{uav}.10"
                    or record.get("destination_udp_port") != 14600 + uav
                    or record.get("tos") != 184
                    or record.get("full_run_nonce") != run_nonce
                    or not isinstance(digest, str)
                    or HEX64.fullmatch(digest) is None
                ):
                    raise M4ValidationError("actual-control offer route/bytes differ")
                downlink_cells.add(cell_id)
                request_hashes[f"uav{uav}"].add(digest)
            if event == "transaction_result":
                uav = record.get("uav")
                if isinstance(uav, bool) or not isinstance(uav, int) or not 1 <= uav <= 5:
                    raise M4ValidationError("actual-control result UAV differs")
                downlink = f"uav{uav}.control.downlink"
                uplink = f"uav{uav}.control.uplink"
                if (
                    record.get("endpoint_form") != api.get("control_endpoint_form")
                    or record.get("downlink_cell_id") != downlink
                    or record.get("uplink_cell_id") != uplink
                    or record.get("full_run_nonce") != run_nonce
                ):
                    raise M4ValidationError("actual-control result matrix/nonce differs")
                downlink_cells.add(downlink)
                if record.get("success") is True:
                    ack = record.get("ack")
                    telemetry = record.get("requested_telemetry")
                    digest = ack.get("transport_payload_sha256") if isinstance(ack, dict) else None
                    if (
                        not isinstance(ack, dict)
                        or not isinstance(telemetry, dict)
                        or ack.get("source_system") != uav
                        or ack.get("source_component") != 1
                        or ack.get("message_type") != "COMMAND_ACK"
                        or ack.get("mavlink_command") != 512
                        or ack.get("mavlink_result") != 0
                        or telemetry.get("source_system") != uav
                        or telemetry.get("source_component") != 1
                        or telemetry.get("message_type") != "AUTOPILOT_VERSION"
                        or not isinstance(digest, str)
                        or HEX64.fullmatch(digest) is None
                    ):
                        raise M4ValidationError(
                            "successful control result is not a real ArduPilot ACK/telemetry pair"
                        )
                    uplink_cells.add(uplink)
                    command_digest = record.get("command_frame_sha256")
                    if (
                        not isinstance(command_digest, str)
                        or HEX64.fullmatch(command_digest) is None
                    ):
                        raise M4ValidationError(
                            "successful control result lacks request payload identity"
                        )
                    delivered_request_hashes[f"uav{uav}"].add(command_digest)
                    response_hashes[f"uav{uav}"].add(digest)
        if event_counts.get("actual_control_socket_ready") != 1:
            raise M4ValidationError("actual-control socket-ready cardinality differs")
        if event_counts.get("actual_control_link_ready") != 1:
            raise M4ValidationError("actual-control link-ready cardinality differs")
        if event_counts.get("actual_control_shutdown") != 1:
            raise M4ValidationError("actual-control clean shutdown cardinality differs")
        expected_down = {f"uav{index}.control.downlink" for index in range(1, 6)}
        expected_up = {f"uav{index}.control.uplink" for index in range(1, 6)}
        if downlink_cells != expected_down or uplink_cells != expected_up:
            raise M4ValidationError(
                "actual-control evidence does not cover the exact ten accepted M3 control cells"
            )
        if any(
            not request_hashes[f"uav{index}"] or not response_hashes[f"uav{index}"]
            for index in range(1, 6)
        ):
            raise M4ValidationError("actual-control request/ACK hash coverage differs")
        details = {
            "event_count": len(records),
            "event_counts": dict(sorted(event_counts.items())),
            "control_cell_count": len(downlink_cells | uplink_cells),
            "request_hashes": {key: sorted(value) for key, value in request_hashes.items()},
            "delivered_request_hashes": {
                key: sorted(value) for key, value in delivered_request_hashes.items()
            },
            "response_hashes": {key: sorted(value) for key, value in response_hashes.items()},
        }
    except (OSError, TypeError, ValueError, M4ValidationError) as exc:
        failures.append(f"actual-control event audit is invalid: {exc}")
    return details, failures


def _tail_topology_evidence(
    run_dir: Path,
    *,
    run: Mapping[str, Any],
    start_ns: int,
    end_ns: int,
) -> tuple[dict[str, Any], list[str]]:
    """Prove the five /30 MAVProxy tails continuously during acceptance."""

    failures: list[str] = []
    details: dict[str, Any] = {}
    try:
        from network.validation.validate_m3_external_matrix import (
            _continuous_namespace_failures,
        )

        mavproxy_ports = {
            index: int(
                strict_json(
                    run_dir / f"raw/actual_sitl/uav{index}.ready.json"
                )["mavproxy_peer"]["port"]
            )
            for index in range(1, 6)
        }
        samples = strict_jsonl(
            run_dir / "raw/topology_monitor/samples.jsonl",
            max_line_bytes=16 * 1024 * 1024,
        )
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
        if (
            not samples
            or any(set(sample) != sample_keys for sample in samples)
            or [sample.get("sample_sequence") for sample in samples]
            != list(range(1, len(samples) + 1))
            or any(
                sample.get("schema") != "ams.m3.topology_sample/v1"
                or sample.get("run_id") != run.get("run_id")
                or sample.get("runtime_id") != run.get("runtime_id")
                or sample.get("run_nonce") != run.get("run_nonce")
                or isinstance(sample.get("monotonic_ns"), bool)
                or not isinstance(sample.get("monotonic_ns"), int)
                or sample.get("reason") not in {"periodic", "transition"}
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
            )
        ):
            raise M4ValidationError(
                "topology sample schema/sequence/run identity differs"
            )
        all_times = [int(sample["monotonic_ns"]) for sample in samples]
        if all_times != sorted(all_times) or len(all_times) != len(set(all_times)):
            raise M4ValidationError("topology sample chronology differs")
        selected = [
            sample
            for sample in samples
            if start_ns <= sample["monotonic_ns"] <= end_ns
        ]
        if not selected:
            raise M4ValidationError("topology monitor has no acceptance samples")
        times = [int(sample["monotonic_ns"]) for sample in selected]
        if (
            times != sorted(times)
            or times[0] > start_ns + 1_000_000_000
            or times[-1] < end_ns - 1_000_000_000
            or max((right - left for left, right in zip(times, times[1:])), default=0)
            > 1_000_000_000
        ):
            raise M4ValidationError("topology monitor coverage/cadence differs")
        expected_namespaces = {
            "container-root",
            "ams-ns3",
            "ams-gcs",
            *(f"ams-uav{index}" for index in range(1, 6)),
        }
        namespace_inodes: dict[str, set[int]] = defaultdict(set)
        netlink_identities: dict[str, set[tuple[int, int]]] = defaultdict(set)
        critical_process_identities: dict[
            str, set[tuple[int, int]]
        ] = defaultdict(set)
        process_fingerprints: dict[tuple[int, int], set[str]] = defaultdict(set)
        process_keys = {
            "pid",
            "start_ticks",
            "namespace",
            "namespace_inode",
            "executable",
            "executable_sha256",
            "cmdline",
            "cap_eff",
            "cgroup",
        }

        def command_interface(command: list[str]) -> str | None:
            return next(
                (
                    command[index + 1]
                    for index, token in enumerate(command[:-1])
                    if token == "--interface"
                ),
                None,
            )

        expected_critical_roles = {
            "engine:ams-ns3",
            "actual_control:ams-gcs",
            *(f"actual_adapter:ams-uav{index}" for index in range(1, 6)),
            *(f"netlink:{namespace}" for namespace in expected_namespaces),
            *(
                f"capture:ams-ns3:vp-{endpoint}"
                for endpoint in ("gcs", "uav1", "uav2", "uav3", "uav4", "uav5")
            ),
            "capture:ams-gcs:eth0",
            *(
                f"capture:ams-uav{index}:{interface}"
                for index in range(1, 6)
                for interface in ("eth0", "tail0")
            ),
        }
        for sample in selected:
            namespaces = sample.get("namespaces")
            if not isinstance(namespaces, dict) or set(namespaces) != expected_namespaces:
                raise M4ValidationError("topology namespace set differs")
            for name, record in namespaces.items():
                if not isinstance(record, dict) or record.get("present") is not True:
                    raise M4ValidationError(f"topology namespace is absent: {name}")
                inode = record.get("namespace_inode")
                if isinstance(inode, bool) or not isinstance(inode, int) or inode <= 0:
                    raise M4ValidationError(f"topology namespace inode invalid: {name}")
                namespace_inodes[name].add(inode)
                namespace_failures = _continuous_namespace_failures(
                    name,
                    record,
                    f"capacity sample {sample.get('sample_sequence')}/{name}",
                )
                if namespace_failures:
                    raise M4ValidationError("; ".join(namespace_failures))

            monitors = sample.get("netlink_monitors")
            if not isinstance(monitors, dict) or set(monitors) != expected_namespaces:
                raise M4ValidationError("topology netlink monitor set differs")
            for namespace, monitor in monitors.items():
                if (
                    not isinstance(monitor, dict)
                    or set(monitor) != {"pid", "start_ticks", "alive"}
                    or monitor.get("alive") is not True
                    or isinstance(monitor.get("pid"), bool)
                    or not isinstance(monitor.get("pid"), int)
                    or monitor["pid"] <= 1
                    or isinstance(monitor.get("start_ticks"), bool)
                    or not isinstance(monitor.get("start_ticks"), int)
                    or monitor["start_ticks"] <= 0
                ):
                    raise M4ValidationError(
                        f"topology netlink monitor identity differs: {namespace}"
                    )
                netlink_identities[namespace].add(
                    (monitor["pid"], monitor["start_ticks"])
                )

            processes = sample.get("processes")
            if not isinstance(processes, list):
                raise M4ValidationError("topology process inventory is not a list")
            roles_in_sample: dict[str, list[tuple[int, int]]] = defaultdict(list)
            for process in processes:
                if not isinstance(process, dict) or set(process) != process_keys:
                    raise M4ValidationError("topology process record keys differ")
                pid = process.get("pid")
                start_ticks = process.get("start_ticks")
                namespace = process.get("namespace")
                namespace_inode = process.get("namespace_inode")
                command = process.get("cmdline")
                if (
                    isinstance(pid, bool)
                    or not isinstance(pid, int)
                    or pid <= 1
                    or isinstance(start_ticks, bool)
                    or not isinstance(start_ticks, int)
                    or start_ticks <= 0
                    or namespace not in expected_namespaces
                    or namespace_inode
                    != namespaces[str(namespace)].get("namespace_inode")
                    or not isinstance(process.get("executable"), str)
                    or not process["executable"].startswith("/")
                    or not isinstance(process.get("executable_sha256"), str)
                    or HEX64.fullmatch(process["executable_sha256"]) is None
                    or not isinstance(command, list)
                    or not command
                    or any(not isinstance(token, str) for token in command)
                    or not isinstance(process.get("cap_eff"), str)
                    or not isinstance(process.get("cgroup"), list)
                    or any(not isinstance(line, str) for line in process["cgroup"])
                ):
                    raise M4ValidationError("topology process identity is malformed")
                identity = (pid, start_ticks)
                process_fingerprints[identity].add(
                    json.dumps(process, sort_keys=True, separators=(",", ":"))
                )
                command_text = " ".join(command)
                role: str | None = None
                if " -ts monitor all" in command_text:
                    role = f"netlink:{namespace}"
                elif namespace == "ams-ns3" and "ams-tap-packet-engine" in command_text:
                    role = "engine:ams-ns3"
                elif namespace == "ams-gcs" and "actual_sitl_control_probe.py" in command_text:
                    role = "actual_control:ams-gcs"
                elif str(namespace).startswith("ams-uav") and "actual_sitl_mavlink_endpoint.py" in command_text:
                    role = f"actual_adapter:{namespace}"
                elif "raw_packet_capture.py" in command_text:
                    interface = command_interface(command)
                    if interface is None:
                        raise M4ValidationError(
                            "topology capture process lacks an interface"
                        )
                    role = f"capture:{namespace}:{interface}"
                elif "m4_endpoint_agent.py" in command_text:
                    role = f"endpoint_agent:{namespace}"
                elif namespace != "container-root":
                    raise M4ValidationError(
                        f"topology data namespace has undeclared process: {command_text!r}"
                    )
                if role is not None:
                    roles_in_sample[role].append(identity)
                    critical_process_identities[role].add(identity)
            for role in expected_critical_roles:
                if len(roles_in_sample.get(role, [])) != 1:
                    raise M4ValidationError(
                        f"topology critical process count differs: {role}"
                    )
            for namespace, monitor in monitors.items():
                if roles_in_sample.get(f"netlink:{namespace}") != [
                    (monitor["pid"], monitor["start_ticks"])
                ]:
                    raise M4ValidationError(
                        f"topology netlink process/monitor binding differs: {namespace}"
                    )

            def socket_lines(namespace: str) -> list[str]:
                value = namespaces[namespace].get("sockets")
                if not isinstance(value, list) or any(
                    not isinstance(line, str) for line in value
                ):
                    raise M4ValidationError(
                        f"topology socket snapshot differs: {namespace}"
                    )
                return value

            def endpoint_token(host: str, port: int) -> re.Pattern[str]:
                return re.compile(
                    rf"(?<![0-9A-Fa-f:.]){re.escape(host)}:{port}(?![0-9])"
                )

            gcs_matches = [
                line
                for line in socket_lines("ams-gcs")
                if endpoint_token("10.71.0.10", 14600).search(line)
            ]
            if len(gcs_matches) != 1:
                raise M4ValidationError("GCS exact control socket is absent/ambiguous")
            for index in range(1, 6):
                endpoint_lines = socket_lines(f"ams-uav{index}")
                radio_matches = [
                    line
                    for line in endpoint_lines
                    if endpoint_token(
                        f"10.71.{index}.10", 14600 + index
                    ).search(line)
                ]
                tail_matches = [
                    line
                    for line in endpoint_lines
                    if endpoint_token(
                        f"10.72.{index}.2", 14559 + index
                    ).search(line)
                ]
                if len(radio_matches) != 1 or len(tail_matches) != 1:
                    raise M4ValidationError(
                        f"uav{index} exact radio/tail socket set differs"
                    )
                root_lines = socket_lines("container-root")
                dynamic = mavproxy_ports[index]
                mavproxy_matches = [
                    line
                    for line in root_lines
                    if re.search(rf":{dynamic}(?![0-9])", line)
                    and endpoint_token(
                        f"10.72.{index}.2", 14559 + index
                    ).search(line)
                ]
                if len(mavproxy_matches) != 1:
                    raise M4ValidationError(
                        f"uav{index} exact MAVProxy tail socket differs"
                    )

            formal_tokens = [
                endpoint_token("10.71.0.10", 14600),
                *[
                    endpoint_token(f"10.71.{index}.10", 14600 + index)
                    for index in range(1, 6)
                ],
                *[
                    endpoint_token(f"10.72.{index}.2", 14559 + index)
                    for index in range(1, 6)
                ],
            ]
            expected_formal_line_counts = {
                "container-root": 5,
                "ams-ns3": 0,
                "ams-gcs": 1,
                **{f"ams-uav{index}": 2 for index in range(1, 6)},
            }
            for namespace, expected_count in expected_formal_line_counts.items():
                matching = {
                    line
                    for line in socket_lines(namespace)
                    if any(pattern.search(line) for pattern in formal_tokens)
                    or (
                        namespace == "container-root"
                        and any(
                            re.search(rf":{port}(?![0-9])", line)
                            for port in mavproxy_ports.values()
                        )
                    )
                }
                if len(matching) != expected_count:
                    raise M4ValidationError(
                        f"topology has alternate/missing formal control sockets: {namespace}"
                    )
            root = namespaces["container-root"]
            root_addresses = {
                (link.get("ifname"), info.get("local"), info.get("prefixlen"))
                for link in root.get("addresses", [])
                for info in link.get("addr_info", [])
                if str(link.get("ifname", "")).startswith("ams-tail")
                and info.get("family") == "inet"
            }
            expected_root = {
                (f"ams-tail{index}", f"10.72.{index}.1", 30)
                for index in range(1, 6)
            }
            if root_addresses != expected_root:
                raise M4ValidationError("root actual-SITL /30 tail address set differs")
            for index in range(1, 6):
                endpoint = namespaces[f"ams-uav{index}"]
                tail_addresses = [
                    (info.get("local"), info.get("prefixlen"))
                    for link in endpoint.get("addresses", [])
                    if link.get("ifname") == "tail0"
                    for info in link.get("addr_info", [])
                    if info.get("family") == "inet"
                ]
                if tail_addresses != [(f"10.72.{index}.2", 30)]:
                    raise M4ValidationError(
                        f"uav{index} actual-SITL /30 tail address differs"
                    )
        if any(len(values) != 1 for values in namespace_inodes.values()):
            raise M4ValidationError("network namespace identity changed during acceptance")
        if any(len(values) != 1 for values in netlink_identities.values()):
            raise M4ValidationError(
                "netlink monitor identity changed during acceptance"
            )
        if set(critical_process_identities) < expected_critical_roles or any(
            len(critical_process_identities[role]) != 1
            for role in expected_critical_roles
        ):
            raise M4ValidationError(
                "critical topology process identity changed during acceptance"
            )
        if any(len(values) != 1 for values in process_fingerprints.values()):
            raise M4ValidationError(
                "topology process executable/cmdline/cgroup identity changed"
            )
        details = {
            "sample_count": len(selected),
            "maximum_sample_gap_ns": max(
                (right - left for left, right in zip(times, times[1:])), default=0
            ),
            "namespace_inodes": {
                key: next(iter(value)) for key, value in sorted(namespace_inodes.items())
            },
            "netlink_monitor_count": len(netlink_identities),
            "critical_process_role_count": len(expected_critical_roles),
        }
    except (KeyError, OSError, TypeError, ValueError, M4ValidationError) as exc:
        failures.append(f"actual-SITL /30 topology cannot be proven: {exc}")
    return details, failures


def _pcap_udp_occurrences(path: Path) -> tuple[list[dict[str, Any]], int]:
    """Decode ordered Ethernet/IPv4/UDP occurrences from bounded classic PCAP."""

    if not regular_file(path) or path.stat().st_size > 512 * 1024 * 1024:
        raise M4ValidationError(f"PCAP is absent/nonregular/oversized: {path}")
    payload = path.read_bytes()
    if len(payload) < 24:
        raise M4ValidationError(f"PCAP header is truncated: {path}")
    magic = payload[:4]
    if magic in {b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1"}:
        byteorder, timestamp_scale = "little", (
            1_000 if magic == b"\xd4\xc3\xb2\xa1" else 1
        )
    elif magic in {b"\xa1\xb2\xc3\xd4", b"\xa1\xb2\x3c\x4d"}:
        byteorder, timestamp_scale = "big", (
            1_000 if magic == b"\xa1\xb2\xc3\xd4" else 1
        )
    else:
        raise M4ValidationError(f"PCAP magic differs: {path}")
    if int.from_bytes(payload[20:24], byteorder) != 1:
        raise M4ValidationError(f"PCAP linktype is not Ethernet: {path}")
    occurrences: list[dict[str, Any]] = []
    packet_count = 0
    offset = 24
    while offset < len(payload):
        if offset + 16 > len(payload):
            raise M4ValidationError(f"PCAP record header is truncated: {path}")
        timestamp_seconds = int.from_bytes(payload[offset : offset + 4], byteorder)
        timestamp_fraction = int.from_bytes(payload[offset + 4 : offset + 8], byteorder)
        captured = int.from_bytes(payload[offset + 8 : offset + 12], byteorder)
        original = int.from_bytes(payload[offset + 12 : offset + 16], byteorder)
        offset += 16
        if captured <= 0 or captured != original or offset + captured > len(payload):
            raise M4ValidationError(f"PCAP record length is invalid: {path}")
        frame = payload[offset : offset + captured]
        offset += captured
        packet_count += 1
        if len(frame) < 14:
            continue
        ethernet_type = int.from_bytes(frame[12:14], "big")
        ip_offset = 14
        if ethernet_type in {0x8100, 0x88A8} and len(frame) >= 18:
            ethernet_type = int.from_bytes(frame[16:18], "big")
            ip_offset = 18
        if ethernet_type != 0x0800 or len(frame) < ip_offset + 20:
            continue
        ihl = (frame[ip_offset] & 0x0F) * 4
        total = int.from_bytes(frame[ip_offset + 2 : ip_offset + 4], "big")
        if (
            ihl < 20
            or frame[ip_offset + 9] != 17
            or total < ihl + 8
            or ip_offset + total > len(frame)
            or int.from_bytes(frame[ip_offset + 6 : ip_offset + 8], "big")
            & 0x3FFF
            != 0
        ):
            continue
        udp_offset = ip_offset + ihl
        udp_length = int.from_bytes(frame[udp_offset + 4 : udp_offset + 6], "big")
        if udp_length < 8 or udp_offset + udp_length > ip_offset + total:
            continue
        datagram = frame[udp_offset + 8 : udp_offset + udp_length]
        occurrences.append(
            {
                "packet_index": packet_count,
                "realtime_ns": timestamp_seconds * 1_000_000_000
                + timestamp_fraction * timestamp_scale,
                "tos": frame[ip_offset + 1],
                "source_ip": ".".join(
                    str(value) for value in frame[ip_offset + 12 : ip_offset + 16]
                ),
                "destination_ip": ".".join(
                    str(value) for value in frame[ip_offset + 16 : ip_offset + 20]
                ),
                "source_udp_port": int.from_bytes(
                    frame[udp_offset : udp_offset + 2], "big"
                ),
                "destination_udp_port": int.from_bytes(
                    frame[udp_offset + 2 : udp_offset + 4], "big"
                ),
                "transport_payload_sha256": hashlib.sha256(datagram).hexdigest(),
                "transport_payload_size": len(datagram),
            }
        )
    if packet_count < 1:
        raise M4ValidationError(f"PCAP contains no packets: {path}")
    return occurrences, packet_count


def _pcap_udp_payload_hashes(path: Path) -> tuple[set[str], int]:
    occurrences, packet_count = _pcap_udp_occurrences(path)
    return {
        str(record["transport_payload_sha256"]) for record in occurrences
    }, packet_count


def _validate_airborne_network_lineage(
    run_dir: Path,
    *,
    run: Mapping[str, Any],
    offers_by_transaction: Mapping[str, Mapping[str, Any]],
    terminal_kind: Mapping[str, str],
    required_uplink_parents: Mapping[int, Mapping[str, Any]],
) -> dict[str, Any]:
    """Bind every flight occurrence through ns-3, Sionna, adapter, and PCAP."""

    from network.bridge.actual_sitl_mavlink_endpoint import (
        CONTROL_TOS,
        validate_jsonl_audit,
    )

    handoff_limit_ns = 1_000_000_000
    config = strict_json(run_dir / "logs/ns3_packet_engine_config.json")
    ready = strict_json(run_dir / "raw/state/ns3-engine.ready.json")
    canonical = config.get("canonical_config")
    config_sha256 = config.get("config_sha256")
    resolved = config.get("resolved")
    if (
        config.get("contract") != "ams.tap_packet_engine/v1"
        or not isinstance(canonical, str)
        or not isinstance(config_sha256, str)
        or HEX64.fullmatch(config_sha256) is None
        or hashlib.sha256(canonical.encode()).hexdigest() != config_sha256
        or not isinstance(resolved, dict)
        or resolved.get("uav_count") != 5
        or resolved.get("event_epoch") != 1
        or resolved.get("self_test") is not False
        or resolved.get("sionna_ipc_enabled") is not True
        or resolved.get("sionna_intervention") != "natural"
        or resolved.get("tap_gcs") != "tap-gcs"
        or resolved.get("tap_uavs")
        != [f"tap-uav{uav}" for uav in EXPECTED_UAVS]
        or resolved.get("seed") != 42
        or resolved.get("run") != 1
        or ready
        != {
            "status": "ready",
            "contract": "ams.tap_packet_engine/v1",
            "config_sha256": config_sha256,
            "event_epoch": 1,
            "uav_count": 5,
        }
    ):
        raise M4ValidationError("airborne ns-3 engine config/readiness differs")

    state_records = strict_jsonl(
        run_dir / "logs/sionna_applied_states.jsonl", max_line_bytes=65_536
    )
    states_by_hash: dict[str, dict[str, Any]] = {}
    for ordinal, state in enumerate(state_records, start=1):
        state_hash = state.get("state_sha256")
        unhashed = dict(state)
        unhashed.pop("state_sha256", None)
        recomputed = hashlib.sha256(
            json.dumps(unhashed, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if (
            not isinstance(state_hash, str)
            or HEX64.fullmatch(state_hash) is None
            or state_hash != recomputed
            or state_hash in states_by_hash
        ):
            raise M4ValidationError(
                f"airborne Sionna state self-identity differs at record {ordinal}"
            )
        states_by_hash[state_hash] = state

    packet_records = strict_jsonl(
        run_dir / "logs/ns3_packet_events.jsonl", max_line_bytes=65_536
    )
    deliveries = index_exact_ns3_unicast_deliveries(
        packet_records,
        expected_event_epoch=1,
        expected_config_sha256=config_sha256,
        states_by_hash=states_by_hash,
    )
    drops = index_exact_ns3_unicast_drops(
        packet_records,
        expected_event_epoch=1,
        expected_config_sha256=config_sha256,
        states_by_hash=states_by_hash,
        required_intervention="natural",
    )

    adapter_forwards: dict[tuple[int, str], list[dict[str, Any]]] = {}
    tail_occurrences: dict[int, list[dict[str, Any]]] = {}
    required_hash_routes: dict[str, set[tuple[Any, ...]]] = defaultdict(set)
    for uav in EXPECTED_UAVS:
        ready_document = strict_json(
            run_dir / f"raw/actual_sitl/uav{uav}.ready.json"
        )
        peer = ready_document.get("mavproxy_peer")
        radio_socket = ready_document.get("radio_socket")
        tail_socket = ready_document.get("tail_socket")
        expected_peer_host = f"10.72.{uav}.1"
        if (
            not isinstance(peer, dict)
            or peer.get("host") != expected_peer_host
            or isinstance(peer.get("port"), bool)
            or not isinstance(peer.get("port"), int)
            or not 1 <= peer["port"] <= 65_535
            or not isinstance(radio_socket, dict)
            or radio_socket.get("local")
            != {"host": f"10.71.{uav}.10", "port": 14600 + uav}
            or radio_socket.get("state") != "07"
            or not isinstance(tail_socket, dict)
            or tail_socket.get("local")
            != {"host": f"10.72.{uav}.2", "port": 14559 + uav}
            or tail_socket.get("state") != "07"
        ):
            raise M4ValidationError(f"uav{uav} airborne adapter socket identity differs")
        audit = validate_jsonl_audit(
            run_dir / f"logs/actual_sitl_uav{uav}.jsonl",
            run_id=str(run.get("run_id")),
            runtime_id=str(run.get("runtime_id")),
            run_nonce=str(run.get("run_nonce")),
            uav=f"uav{uav}",
        )
        bound = [
            record
            for record in audit
            if record.get("event") == "adapter_bound_not_ready"
        ]
        if (
            len(bound) != 1
            or bound[0].get("radio_ip_tos") != CONTROL_TOS
            or bound[0].get("radio_ip_recvtos") is not True
            or bound[0].get("tail_ip_tos") != CONTROL_TOS
        ):
            raise M4ValidationError(f"uav{uav} adapter TOS socket proof differs")
        forwards = [record for record in audit if record.get("event") == "forward"]
        times = [record.get("monotonic_ns") for record in forwards]
        if (
            any(isinstance(value, bool) or not isinstance(value, int) for value in times)
            or times != sorted(times)
        ):
            raise M4ValidationError(f"uav{uav} adapter forward chronology differs")
        down_forwards: list[dict[str, Any]] = []
        up_forwards: list[dict[str, Any]] = []
        for record in forwards:
            digest = record.get("sha256")
            size = record.get("bytes")
            if (
                not isinstance(digest, str)
                or HEX64.fullmatch(digest) is None
                or isinstance(size, bool)
                or not isinstance(size, int)
                or not 0 < size <= 65_507
            ):
                raise M4ValidationError(f"uav{uav} adapter forward bytes differ")
            if record.get("direction") == "gcs_to_tail":
                if (
                    record.get("source")
                    != {"host": "10.71.0.10", "port": 14600}
                    or record.get("destination") != peer
                    or record.get("received_tos") != CONTROL_TOS
                    or "transmit_tos" in record
                ):
                    raise M4ValidationError(
                        f"uav{uav} adapter downlink forward route/TOS differs"
                    )
                down_forwards.append(record)
            elif record.get("direction") == "tail_to_gcs":
                if (
                    record.get("source") != peer
                    or record.get("destination")
                    != {"host": "10.71.0.10", "port": 14600}
                    or record.get("transmit_tos") != CONTROL_TOS
                ):
                    raise M4ValidationError(
                        f"uav{uav} adapter uplink forward route/TOS differs"
                    )
                up_forwards.append(record)
            else:
                raise M4ValidationError(f"uav{uav} adapter forward direction differs")
        adapter_forwards[(uav, "downlink")] = down_forwards
        adapter_forwards[(uav, "uplink")] = up_forwards

        root_pcap, _root_count = _pcap_udp_occurrences(
            run_dir / f"pcap/tail-root-uav{uav}.pcap"
        )
        endpoint_pcap, _endpoint_count = _pcap_udp_occurrences(
            run_dir / f"pcap/tail-uav{uav}.pcap"
        )
        dynamic_port = int(peer["port"])

        def is_tail_route(record: Mapping[str, Any]) -> bool:
            return (
                (
                    record.get("source_ip") == f"10.72.{uav}.2"
                    and record.get("source_udp_port") == 14559 + uav
                    and record.get("destination_ip") == expected_peer_host
                    and record.get("destination_udp_port") == dynamic_port
                    and record.get("tos") == CONTROL_TOS
                )
                or (
                    record.get("source_ip") == expected_peer_host
                    and record.get("source_udp_port") == dynamic_port
                    and record.get("destination_ip") == f"10.72.{uav}.2"
                    and record.get("destination_udp_port") == 14559 + uav
                    and record.get("tos") == 0
                )
            )

        root_tail = [record for record in root_pcap if is_tail_route(record)]
        endpoint_tail = [record for record in endpoint_pcap if is_tail_route(record)]

        def pcap_identity(record: Mapping[str, Any]) -> tuple[Any, ...]:
            return (
                record.get("tos"),
                record.get("source_ip"),
                record.get("source_udp_port"),
                record.get("destination_ip"),
                record.get("destination_udp_port"),
                record.get("transport_payload_sha256"),
                record.get("transport_payload_size"),
            )

        if [pcap_identity(record) for record in root_tail] != [
            pcap_identity(record) for record in endpoint_tail
        ]:
            raise M4ValidationError(f"uav{uav} two tail PCAP occurrence streams differ")

        expected_down_tail = [
            (
                CONTROL_TOS,
                f"10.72.{uav}.2",
                14559 + uav,
                expected_peer_host,
                dynamic_port,
                record["sha256"],
                record["bytes"],
            )
            for record in down_forwards
        ]
        tail_ingress_audit: list[Mapping[str, Any]] = []
        for record in audit:
            if (
                record.get("event") == "peer_candidate_published_not_ready"
                and record.get("source") == peer
            ):
                tail_ingress_audit.append(record)
            elif (
                record.get("event") == "drop"
                and record.get("direction") == "tail_to_gcs"
                and record.get("source") == peer
            ):
                tail_ingress_audit.append(record)
            elif (
                record.get("event") == "forward"
                and record.get("direction") == "tail_to_gcs"
                and record.get("buffered_pre_authorization") is False
            ):
                tail_ingress_audit.append(record)
        expected_up_tail = [
            (
                0,
                expected_peer_host,
                dynamic_port,
                f"10.72.{uav}.2",
                14559 + uav,
                record.get("sha256"),
                record.get("bytes"),
            )
            for record in tail_ingress_audit
        ]
        observed_down_tail = [
            pcap_identity(record)
            for record in root_tail
            if record.get("source_ip") == f"10.72.{uav}.2"
        ]
        observed_up_tail = [
            pcap_identity(record)
            for record in root_tail
            if record.get("source_ip") == expected_peer_host
        ]
        if observed_down_tail != expected_down_tail:
            raise M4ValidationError(
                f"uav{uav} adapter/downlink tail PCAP occurrence join differs"
            )
        if observed_up_tail != expected_up_tail:
            raise M4ValidationError(
                f"uav{uav} MAVProxy/uplink tail PCAP occurrence join differs"
            )
        tail_occurrences[uav] = root_pcap

    consumed_uids: set[tuple[int, int]] = set()
    consumed_audit: set[tuple[int, int]] = set()
    adapter_forward_index: dict[
        tuple[int, str, str, int], list[dict[str, Any]]
    ] = defaultdict(list)
    for (uav, direction), records in adapter_forwards.items():
        for record in records:
            adapter_forward_index[
                (uav, direction, str(record["sha256"]), int(record["bytes"]))
            ].append(record)

    def exact_route(
        record: Mapping[str, Any], *, uav: int, direction: str, digest: str, size: int
    ) -> bool:
        downlink = direction == "downlink"
        return (
            record.get("directed_link")
            == (f"cp>uav{uav}" if downlink else f"uav{uav}>cp")
            and record.get("traffic_class") == "control"
            and record.get("source_ip")
            == ("10.71.0.10" if downlink else f"10.71.{uav}.10")
            and record.get("destination_ip")
            == (f"10.71.{uav}.10" if downlink else "10.71.0.10")
            and record.get("source_udp_port")
            == (14600 if downlink else 14600 + uav)
            and record.get("destination_udp_port")
            == (14600 + uav if downlink else 14600)
            and record.get("tos") == CONTROL_TOS
            and record.get("dscp") == 46
            and record.get("transport_payload_sha256") == digest
            and record.get("transport_payload_size") == size
        )

    def bind_audit(
        *,
        uav: int,
        direction: str,
        digest: str,
        size: int,
        reference_ns: int,
        ordered_after: bool,
    ) -> Mapping[str, Any]:
        candidates = []
        # The flight interval contains tens of thousands of telemetry
        # datagrams.  Indexing by preserved byte identity keeps this join
        # linear in evidence size instead of rescanning a whole UAV audit for
        # every raw parent occurrence.
        for record in adapter_forward_index.get((uav, direction, digest, size), []):
            key = (uav, int(record["event_seq"]))
            delta = int(record["monotonic_ns"]) - reference_ns
            if (
                key not in consumed_audit
                and record.get("sha256") == digest
                and record.get("bytes") == size
                and abs(delta) <= handoff_limit_ns
                and (delta >= 0 if ordered_after else delta <= 0)
            ):
                candidates.append(record)
        if len(candidates) != 1:
            raise M4ValidationError(
                f"uav{uav} {direction} datagram lacks one exact adapter occurrence"
            )
        selected = candidates[0]
        consumed_audit.add((uav, int(selected["event_seq"])))
        return selected

    delivered_offer_count = 0
    dropped_offer_count = 0
    required_route_by_digest: dict[str, set[str]] = defaultdict(set)
    for transaction_id, offer in sorted(
        offers_by_transaction.items(), key=lambda item: int(item[1]["sent_monotonic_ns"])
    ):
        uav = int(offer["uav"])
        digest = str(offer["request_transport_payload_sha256"])
        size = int(offer["request_transport_payload_size"])
        link = f"cp>uav{uav}"
        sent_ns = int(offer["sent_monotonic_ns"])
        required_route_by_digest[digest].add(link)
        required_hash_routes[digest].add(
            (
                CONTROL_TOS,
                f"10.72.{uav}.2",
                14559 + uav,
                f"10.72.{uav}.1",
            )
        )
        candidates = []
        for outcome, outcome_name in (
            (deliveries.get((link, "control", digest), []), "deliver"),
            (drops.get((link, "control", digest), []), "drop"),
        ):
            for chain in outcome:
                ingress = chain["ingress"]
                if (
                    tuple(chain["uid_key"]) not in consumed_uids
                    and exact_route(
                        ingress,
                        uav=uav,
                        direction="downlink",
                        digest=digest,
                        size=size,
                    )
                    and sent_ns
                    <= int(ingress["host_monotonic_ns"])
                    < sent_ns + OUTCOME_TIMEOUT_NS
                ):
                    candidates.append((outcome_name, chain))
        if len(candidates) != 1:
            raise M4ValidationError(
                f"flight offer lacks one exact ns-3 occurrence: {transaction_id}"
            )
        outcome_name, chain = candidates[0]
        consumed_uids.add(tuple(chain["uid_key"]))
        if terminal_kind[transaction_id] in {"accepted", "temporarily_rejected"}:
            if outcome_name != "deliver":
                raise M4ValidationError(
                    f"completed flight offer was not delivered: {transaction_id}"
                )
        if outcome_name == "deliver":
            delivered_offer_count += 1
            bind_audit(
                uav=uav,
                direction="downlink",
                digest=digest,
                size=size,
                reference_ns=int(chain["egress"]["host_monotonic_ns"]),
                ordered_after=True,
            )
        else:
            dropped_offer_count += 1

    delivered_parent_count = 0
    for parent in sorted(
        required_uplink_parents.values(),
        key=lambda item: int(item["received_monotonic_ns"]),
    ):
        uav = int(parent["uav"])
        digest = str(parent["sha256"])
        size = len(parent["payload"])
        link = f"uav{uav}>cp"
        received_ns = int(parent["received_monotonic_ns"])
        required_route_by_digest[digest].add(link)
        required_hash_routes[digest].add(
            (
                0,
                f"10.72.{uav}.1",
                None,
                f"10.72.{uav}.2",
            )
        )
        candidates = []
        for chain in deliveries.get((link, "control", digest), []):
            egress_ns = int(chain["egress"]["host_monotonic_ns"])
            if (
                tuple(chain["uid_key"]) not in consumed_uids
                and exact_route(
                    chain["ingress"],
                    uav=uav,
                    direction="uplink",
                    digest=digest,
                    size=size,
                )
                and 0 <= received_ns - egress_ns <= handoff_limit_ns
            ):
                candidates.append(chain)
        if len(candidates) != 1:
            raise M4ValidationError(
                f"raw flight datagram parent lacks one delivered ns-3 UID: "
                f"uav{uav}/{parent['event_sequence']}"
            )
        chain = candidates[0]
        consumed_uids.add(tuple(chain["uid_key"]))
        bind_audit(
            uav=uav,
            direction="uplink",
            digest=digest,
            size=size,
            reference_ns=int(chain["ingress"]["host_monotonic_ns"]),
            ordered_after=False,
        )
        delivered_parent_count += 1

    mandatory_digests = set(required_route_by_digest)
    for (uav, _direction), records in adapter_forwards.items():
        if any(
            record.get("sha256") in mandatory_digests
            and (uav, int(record["event_seq"])) not in consumed_audit
            for record in records
        ):
            raise M4ValidationError(
                "mandatory flight digest has an unowned adapter occurrence"
            )

    # A required digest on another ns-3 link or another /30 tuple is an
    # alternate route, not additional evidence for the same occurrence.
    for record in packet_records:
        digest = record.get("transport_payload_sha256")
        if (
            record.get("event") == "ingress"
            and digest in required_route_by_digest
            and record.get("directed_link") not in required_route_by_digest[digest]
        ):
            raise M4ValidationError("required flight digest crossed a foreign ns-3 route")
    for uav, records in tail_occurrences.items():
        peer_port = int(
            strict_json(run_dir / f"raw/actual_sitl/uav{uav}.ready.json")[
                "mavproxy_peer"
            ]["port"]
        )
        allowed_by_digest: dict[str, set[tuple[Any, ...]]] = defaultdict(set)
        for digest, routes in required_hash_routes.items():
            for tos, source_ip, source_port, destination_ip in routes:
                if source_ip not in {f"10.72.{uav}.1", f"10.72.{uav}.2"}:
                    continue
                allowed_by_digest[digest].add(
                    (
                        tos,
                        source_ip,
                        peer_port if source_port is None else source_port,
                        destination_ip,
                        14559 + uav if tos == 0 else peer_port,
                    )
                )
        for record in records:
            digest = str(record["transport_payload_sha256"])
            if digest not in allowed_by_digest:
                continue
            route = (
                record["tos"],
                record["source_ip"],
                record["source_udp_port"],
                record["destination_ip"],
                record["destination_udp_port"],
            )
            if route not in allowed_by_digest[digest]:
                raise M4ValidationError("required flight digest crossed a foreign tail route")

    return {
        "ns3_event_epoch": 1,
        "ns3_config_sha256": config_sha256,
        "flight_offer_uid_count": delivered_offer_count + dropped_offer_count,
        "flight_offer_delivered_count": delivered_offer_count,
        "flight_offer_sionna_drop_count": dropped_offer_count,
        "required_uplink_parent_uid_count": delivered_parent_count,
        "consumed_ns3_uid_count": len(consumed_uids),
        "consumed_adapter_forward_count": len(consumed_audit),
        "tail_pcap_stream_count": 10,
    }


def _tail_capture_evidence(
    run_dir: Path,
    *,
    control: Mapping[str, Any],
    start_ns: int,
    end_ns: int,
) -> tuple[dict[str, Any], list[str]]:
    failures: list[str] = []
    details: dict[str, Any] = {}
    try:
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
        packet_counts: dict[str, int] = {}
        for index in range(1, 6):
            uav = f"uav{index}"
            request_hashes = set(
                control.get("delivered_request_hashes", {}).get(uav, [])
            )
            response_hashes = set(control.get("response_hashes", {}).get(uav, []))
            if not request_hashes or not response_hashes:
                raise M4ValidationError(f"{uav} control hashes are unavailable")
            for role, interface in (
                (f"tail-root-uav{index}", f"ams-tail{index}"),
                (f"tail-uav{index}", "tail0"),
            ):
                pcap_path = run_dir / f"pcap/{role}.pcap"
                observed, count = _pcap_udp_payload_hashes(pcap_path)
                packet_counts[role] = count
                if not request_hashes.issubset(observed) or not response_hashes.issubset(
                    observed
                ):
                    raise M4ValidationError(
                        f"{uav} actual command/ACK bytes miss mandatory capture {role}"
                    )
                stats = strict_json(run_dir / f"logs/capture-{role}.json")
                stderr = run_dir / f"logs/capture-{role}.stderr"
                if (
                    set(stats) != stats_keys
                    or stats.get("contract") != CAPTURE_STATS_CONTRACT
                    or stats.get("interface") != interface
                    or stats.get("capture_protocol") != CAPTURE_PROTOCOL
                    or stats.get("packet_filter") != CAPTURE_PACKET_FILTER
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
                    or type(stats.get("packets_written")) is not int
                    or stats.get("packets_written") != count
                    or type(stats.get("packets_received_kernel")) is not int
                    or stats["packets_received_kernel"] < count
                    or type(stats.get("packets_dropped_kernel")) is not int
                    or stats.get("packets_dropped_kernel") != 0
                    or type(stats.get("started_monotonic_ns")) is not int
                    or type(stats.get("stopped_monotonic_ns")) is not int
                    or stats["started_monotonic_ns"] >= start_ns
                    or stats["stopped_monotonic_ns"] <= end_ns
                    or not regular_file(stderr)
                    or stderr.stat().st_size != 0
                ):
                    raise M4ValidationError(f"capture accounting differs: {role}")
        details = {"tail_capture_count": 10, "packet_counts": packet_counts}
    except (KeyError, OSError, TypeError, ValueError, M4ValidationError) as exc:
        failures.append(f"actual-SITL tail capture evidence is invalid: {exc}")
    return details, failures


def _validate_actual_endpoint_path(
    run_dir: Path, run: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Bind formal M4 traffic to the accepted Q3 actual-SITL API and bytes."""

    failures: list[str] = []
    details: dict[str, Any] = {}
    api, api_details, api_failures = _accepted_m3_actual_control_api(run_dir, run)
    failures.extend(api_failures)
    details["accepted_api"] = api_details
    if not api:
        return details, failures
    try:
        from network.bridge.actual_sitl_mavlink_endpoint import (
            AUTHORIZATION_CONTRACT,
            CANDIDATE_CONTRACT,
            MANIFEST_CONTRACT,
            READY_CONTRACT,
            EndpointError,
            validate_jsonl_audit,
            validate_manifest,
        )

        binding = run["endpoint_path"]
        manifest_path = run_dir / _safe_relative(
            binding["actual_sitl_manifest_path"], "actual-SITL manifest path"
        )
        manifest = strict_json(manifest_path)
        if manifest_path.read_bytes() != canonical_json(manifest):
            raise M4ValidationError("actual-SITL manifest is not canonical")
        try:
            validate_manifest(manifest)
        except EndpointError as exc:
            raise M4ValidationError(f"actual-SITL manifest is invalid: {exc}") from exc
        if (
            manifest.get("contract") != MANIFEST_CONTRACT
            or manifest.get("run_id") != run.get("run_id")
            or manifest.get("runtime_id") != run.get("runtime_id")
            or manifest.get("run_nonce") != run.get("run_nonce")
            or manifest.get("adapter_source_sha256")
            != api["adapter_source"]["sha256"]
            or manifest.get("relay_core_source_sha256")
            != api["relay_core_source"]["sha256"]
        ):
            raise M4ValidationError("actual-SITL manifest/API/run identity differs")
        manifest_sha256 = hashlib.sha256(canonical_json(manifest)).hexdigest()
        channels = manifest["channels"]
        for index, channel in enumerate(channels, start=1):
            accepted = api["channels"][f"uav{index}"]
            if (
                channel.get("uav") != f"uav{index}"
                or channel.get("system_id") != accepted["system_id"]
                or channel.get("instance") != accepted["instance"]
                or channel.get("radio_bind") != accepted["radio_bind"]
                or channel.get("gcs_peer") != accepted["gcs_peer"]
                or channel.get("tail_bind")
                != {
                    "host": accepted["tail_uav"]["host"],
                    "port": accepted["tail_uav"]["port"],
                }
                or channel.get("tail_peer_host") != accepted["tail_root"]["host"]
                or channel.get("tail_pcap_roles") != accepted["tail_pcap_roles"]
                or channel.get("master") != accepted["master"]
            ):
                raise M4ValidationError(f"actual-SITL channel differs from M3 API: uav{index}")

        runtime_roles, runtime_sample_count = _runtime_process_samples(run_dir)
        endpoint_counts = {
            "arducopter": 5,
            "mavproxy": 5,
            "gcs_endpoint_probe": 1,
            "uav_endpoint_adapter": 5,
            "actual_endpoint_supervisor": 1,
            "endpoint_companion_agent": 6,
        }
        for role, count in endpoint_counts.items():
            if len(runtime_roles.get(role, [])) != count:
                raise M4ValidationError(f"actual endpoint runtime role count differs: {role}")
        for channel in channels:
            if len(
                _runtime_matches_frozen_process(
                    channel["sitl"], runtime_roles["arducopter"]
                )
            ) != 1:
                raise M4ValidationError(f"{channel['uav']} ArduCopter identity differs")
            if len(
                _runtime_matches_frozen_process(
                    channel["mavproxy"], runtime_roles["mavproxy"]
                )
            ) != 1:
                raise M4ValidationError(f"{channel['uav']} MAVProxy identity differs")

        companion_matrix = strict_json(ROOT / api["matrix_path"])
        for endpoint_index, endpoint in enumerate(
            ("gcs", "uav1", "uav2", "uav3", "uav4", "uav5")
        ):
            records = strict_jsonl(run_dir / f"raw/endpoints/{endpoint}.jsonl")
            ready_records = [item for item in records if item.get("event") == "agent_ready"]
            expected_ip = f"10.71.{endpoint_index}.10"
            expected_sockets = {
                "payload": [expected_ip, 14700 + endpoint_index],
                "additional_data": [expected_ip, 14800 + endpoint_index],
            }
            if len(ready_records) != 1 or ready_records[0].get("bound_sockets") != expected_sockets:
                raise M4ValidationError(
                    f"{endpoint} companion does not bind exactly payload/additional_data"
                )
            if any(
                record.get("traffic_class") == "control"
                or record.get("socket_class") == "control"
                or record.get("cell_id")
                in {
                    f"uav{index}.control.{direction}"
                    for index in range(1, 6)
                    for direction in ("downlink", "uplink")
                }
                for record in records
            ):
                raise M4ValidationError(
                    f"{endpoint} companion generated/bound acceptance control traffic"
                )
        del companion_matrix

        supervisor_identities: list[dict[str, Any]] = []
        adapter_identities: list[dict[str, Any]] = []
        for index, channel in enumerate(channels, start=1):
            uav = f"uav{index}"
            base = run_dir / "raw/actual_sitl"
            candidate = strict_json(base / f"{uav}.peer-candidate.json")
            authorization = strict_json(base / f"{uav}.authorization.json")
            ready = strict_json(base / f"{uav}.ready.json")
            if (base / f"{uav}.failure.json").exists():
                raise M4ValidationError(f"{uav} actual adapter published failure evidence")
            candidate_sha256 = hashlib.sha256(canonical_json(candidate)).hexdigest()
            authorization_sha256 = hashlib.sha256(canonical_json(authorization)).hexdigest()
            if (
                candidate.get("contract") != CANDIDATE_CONTRACT
                or authorization.get("contract") != AUTHORIZATION_CONTRACT
                or ready.get("contract") != READY_CONTRACT
                or candidate.get("manifest_sha256") != manifest_sha256
                or authorization.get("manifest_sha256") != manifest_sha256
                or ready.get("manifest_sha256") != manifest_sha256
                or authorization.get("candidate_sha256") != candidate_sha256
                or ready.get("candidate_sha256") != candidate_sha256
                or ready.get("authorization_sha256") != authorization_sha256
                or any(
                    document.get("uav") != uav
                    or document.get("run_id") != run.get("run_id")
                    or document.get("runtime_id") != run.get("runtime_id")
                    or document.get("run_nonce") != run.get("run_nonce")
                    for document in (candidate, authorization, ready)
                )
                or ready.get("adapter") != candidate.get("adapter")
                or ready.get("radio_socket") != candidate.get("radio_socket")
                or ready.get("tail_socket") != candidate.get("tail_socket")
                or ready.get("mavproxy_peer") != candidate.get("mavproxy_peer")
            ):
                raise M4ValidationError(f"{uav} candidate/authorization/ready binding differs")
            adapter = ready.get("adapter")
            issuer = authorization.get("issuer")
            if (
                not isinstance(adapter, dict)
                or len(
                    _runtime_matches_frozen_process(
                        adapter, runtime_roles["uav_endpoint_adapter"]
                    )
                )
                != 1
                or not isinstance(issuer, dict)
                or len(
                    _runtime_matches_frozen_process(
                        issuer, runtime_roles["actual_endpoint_supervisor"]
                    )
                )
                != 1
            ):
                raise M4ValidationError(f"{uav} adapter/supervisor process binding differs")
            adapter_identities.append(adapter)
            supervisor_identities.append(issuer)
            audit = validate_jsonl_audit(
                run_dir / f"logs/actual_sitl_{uav}.jsonl",
                run_id=str(run.get("run_id")),
                runtime_id=str(run.get("runtime_id")),
                run_nonce=str(run.get("run_nonce")),
                uav=uav,
            )
            events = [record.get("event") for record in audit]
            if (
                events.count("adapter_bound_not_ready") != 1
                or events.count("adapter_ready") != 1
                or events.count("adapter_stop") != 1
                or events[-1] != "adapter_stop"
                or "adapter_failed_closed" in events
                or not any(
                    record.get("event") == "forward"
                    and record.get("direction") == "gcs_to_tail"
                    for record in audit
                )
                or not any(
                    record.get("event") == "forward"
                    and record.get("direction") == "tail_to_gcs"
                    for record in audit
                )
            ):
                raise M4ValidationError(f"{uav} actual adapter audit lifecycle differs")
        if any(identity != supervisor_identities[0] for identity in supervisor_identities[1:]):
            raise M4ValidationError("five adapters were not authorized by one supervisor")
        if len({int(identity["pid"]) for identity in adapter_identities}) != 5:
            raise M4ValidationError("actual adapter PID set is not five distinct processes")

        aggregate_path = run_dir / _safe_relative(
            binding["actual_sitl_ready_path"], "actual-SITL ready path"
        )
        aggregate = strict_json(aggregate_path)
        if (
            aggregate.get("contract") != "ams.actual-sitl-endpoints-ready/v1"
            or aggregate.get("status") != "ready"
            or aggregate.get("run_id") != run.get("run_id")
            or aggregate.get("runtime_id") != run.get("runtime_id")
            or aggregate.get("run_nonce") != run.get("run_nonce")
            or aggregate.get("manifest_sha256") != manifest_sha256
            or aggregate.get("supervisor") != supervisor_identities[0]
            or set(aggregate.get("channels", {}))
            != {f"uav{index}" for index in range(1, 6)}
        ):
            raise M4ValidationError("aggregate actual-SITL readiness differs")
        if (run_dir / "raw/actual_sitl/endpoint-supervisor.failure.json").exists():
            raise M4ValidationError("actual-SITL supervisor published failure evidence")
        supervisor_audit = validate_jsonl_audit(
            run_dir / "logs/actual_sitl_supervisor.jsonl",
            run_id=str(run.get("run_id")),
            runtime_id=str(run.get("runtime_id")),
            run_nonce=str(run.get("run_nonce")),
            uav="all",
        )
        supervisor_events = [record.get("event") for record in supervisor_audit]
        if (
            supervisor_events.count("aggregate_ready") != 1
            or supervisor_events.count("supervisor_stop") != 1
            or supervisor_events[-1] != "supervisor_stop"
            or "supervisor_failed_closed" in supervisor_events
            or not any(record.get("event") == "lineage_sample_pass" for record in supervisor_audit)
        ):
            raise M4ValidationError("actual-SITL supervisor lifecycle/lineage differs")

        control, control_failures = _actual_control_event_audit(
            run_dir / binding["actual_control_events_path"], run=run, api=api
        )
        if control_failures:
            raise M4ValidationError("; ".join(control_failures))
        if run.get("profile") == "m4_capacity_prerequisite":
            schedule = run.get("schedule", {})
            start_ns = int(schedule.get("warmup_start_monotonic_ns", 0))
            raw_control_events = strict_jsonl(
                run_dir / binding["actual_control_events_path"],
                max_line_bytes=2 * 1024 * 1024,
            )
            shutdown_events = [
                item
                for item in raw_control_events
                if item.get("event") == "flight_plan_shutdown"
            ]
            if (
                len(shutdown_events) != 1
                or isinstance(shutdown_events[0].get("monotonic_ns"), bool)
                or not isinstance(shutdown_events[0].get("monotonic_ns"), int)
            ):
                raise M4ValidationError(
                    "actual endpoint lacks exact airborne shutdown boundary"
                )
            end_ns = int(shutdown_events[0]["monotonic_ns"])
        else:
            window_records = [item for item in run.get("windows", []) if isinstance(item, dict)]
            start_ns = min(
                (int(item.get("start_monotonic_ns", 0)) for item in window_records),
                default=0,
            )
            end_ns = max(
                (int(item.get("end_monotonic_ns", 0)) for item in window_records),
                default=0,
            )
        if start_ns <= 0 or end_ns <= start_ns:
            raise M4ValidationError("actual endpoint acceptance interval is invalid")
        topology, topology_failures = _tail_topology_evidence(
            run_dir, run=run, start_ns=start_ns, end_ns=end_ns
        )
        if topology_failures:
            raise M4ValidationError("; ".join(topology_failures))
        captures, capture_failures = _tail_capture_evidence(
            run_dir,
            control=control,
            start_ns=start_ns,
            end_ns=end_ns,
        )
        if capture_failures:
            raise M4ValidationError("; ".join(capture_failures))
        details.update(
            {
                "backend": ACTUAL_CONTROL_ENDPOINT_FORM,
                "manifest_sha256": manifest_sha256,
                "process_counts": endpoint_counts,
                "runtime_process_sample_count": runtime_sample_count,
                "control": control,
                "topology": topology,
                "tail_captures": captures,
            }
        )
    except (KeyError, OSError, TypeError, ValueError, M4ValidationError) as exc:
        failures.append(f"actual M3/SITL runtime cannot be bound: {exc}")
    return details, failures


_FLIGHT_CRC_EXTRA = {
    0: 50,
    32: 185,
    33: 104,
    75: 158,
    76: 152,
    77: 143,
    111: 34,
    245: 130,
}


def _flight_x25_crc(payload: bytes) -> int:
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


def _decode_flight_mavlink_frame(payload: bytes) -> dict[str, Any]:
    """Independently decode one preserved common-dialect MAVLink frame."""

    if not payload:
        raise M4ValidationError("flight MAVLink frame is empty")
    if payload[0] == 0xFD:
        if len(payload) < 12 or payload[2] & 0x01:
            raise M4ValidationError("flight MAVLink v2 frame is truncated/signed")
        body_size = payload[1]
        if len(payload) != body_size + 12:
            raise M4ValidationError("flight MAVLink v2 frame length differs")
        message_id = int.from_bytes(payload[7:10], "little")
        system_id, component_id = payload[5], payload[6]
        body = payload[10 : 10 + body_size]
        checksum_offset = 10 + body_size
        crc_material = payload[1:checksum_offset]
    elif payload[0] == 0xFE:
        if len(payload) < 8:
            raise M4ValidationError("flight MAVLink v1 frame is truncated")
        body_size = payload[1]
        if len(payload) != body_size + 8:
            raise M4ValidationError("flight MAVLink v1 frame length differs")
        message_id = payload[5]
        system_id, component_id = payload[3], payload[4]
        body = payload[6 : 6 + body_size]
        checksum_offset = 6 + body_size
        crc_material = payload[1:checksum_offset]
    else:
        raise M4ValidationError("flight MAVLink magic byte differs")
    crc_extra = _FLIGHT_CRC_EXTRA.get(message_id)
    if crc_extra is None:
        raise M4ValidationError(f"flight MAVLink message {message_id} is unsupported")
    observed_crc = int.from_bytes(payload[checksum_offset : checksum_offset + 2], "little")
    expected_crc = _flight_x25_crc(crc_material + bytes([crc_extra]))
    if observed_crc != expected_crc:
        raise M4ValidationError("flight MAVLink frame CRC differs")
    return {
        "message_id": message_id,
        "system_id": system_id,
        "component_id": component_id,
        "payload": body,
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _flight_event_frame(record: Mapping[str, Any], expected_message_id: int) -> dict[str, Any]:
    encoded = record.get("mavlink_frame_hex")
    if not isinstance(encoded, str) or len(encoded) > 1024:
        raise M4ValidationError("flight vehicle event lacks bounded MAVLink bytes")
    try:
        payload = bytes.fromhex(encoded)
    except ValueError as exc:
        raise M4ValidationError("flight vehicle event MAVLink hex differs") from exc
    decoded = _decode_flight_mavlink_frame(payload)
    if (
        decoded["message_id"] != expected_message_id
        or record.get("message_id") != expected_message_id
        or record.get("message_type")
        != {
            0: "HEARTBEAT",
            32: "LOCAL_POSITION_NED",
            33: "GLOBAL_POSITION_INT",
            77: "COMMAND_ACK",
            111: "TIMESYNC",
            245: "EXTENDED_SYS_STATE",
        }[expected_message_id]
        or record.get("mavlink_frame_sha256") != decoded["sha256"]
        or record.get("mavlink_frame_size") != len(payload)
    ):
        raise M4ValidationError("flight vehicle event MAVLink identity differs")
    return decoded


def _latest_at_or_before(
    records: list[dict[str, Any]], timestamp_ns: int
) -> dict[str, Any] | None:
    times = [int(record["received_monotonic_ns"]) for record in records]
    index = bisect.bisect_right(times, timestamp_ns) - 1
    return records[index] if index >= 0 else None


def _latest_pose_at_or_before(
    records: list[dict[str, Any]], timestamp_ns: int
) -> dict[str, Any] | None:
    times = [int(record["host_monotonic_ns"]) for record in records]
    index = bisect.bisect_right(times, timestamp_ns) - 1
    return records[index] if index >= 0 else None


def _validate_m4_airborne_gate(
    run_dir: Path, run: Mapping[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Derive continuous five-UAV flight from raw network and odometry facts."""

    failures: list[str] = []
    details: dict[str, Any] = {}
    try:
        schedule = run.get("schedule")
        declared_gate = run.get("airborne_gate")
        if not isinstance(schedule, dict) or not isinstance(declared_gate, dict):
            raise M4ValidationError("capacity contract lacks airborne schedule")
        expected_gate = airborne_gate_contract(schedule)
        if declared_gate != expected_gate or declared_gate.get("contract") != AIRBORNE_GATE_CONTRACT:
            raise M4ValidationError("capacity airborne gate differs from exact schedule")
        start_ns = int(expected_gate["measurement_start_monotonic_ns"])
        end_ns = int(expected_gate["measurement_end_monotonic_ns"])
        ready_deadline_ns = int(expected_gate["airborne_ready_deadline_monotonic_ns"])
        warmup_motion_deadline_ns = int(
            expected_gate["warmup_motion_deadline_monotonic_ns"]
        )
        landing_deadline_ns = int(expected_gate["landing_deadline_monotonic_ns"])
        disarm_deadline_ns = int(expected_gate["disarm_deadline_monotonic_ns"])
        post_control_boundary_ns = end_ns + int(
            expected_gate["post_measurement_control_ns"]
        )

        events = strict_jsonl(
            run_dir / "raw/actual_control/events.jsonl",
            max_line_bytes=2 * 1024 * 1024,
        )
        event_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for record in events:
            event_groups[str(record.get("event"))].append(record)
        allowed_events = {
            "actual_control_socket_ready",
            "control_datagram_receive",
            "real_heartbeat",
            "actual_control_link_ready",
            "ambient_timesync_request",
            "flight_plan_started",
            "flight_command_offered",
            "flight_command_ack",
            "flight_command_timesync_echo",
            "flight_command_complete",
            "flight_command_retryable_rejection_complete",
            "flight_command_outcome_timeout",
            "flight_command_quiet_drain",
            "late_flight_command_ack",
            "late_flight_timesync_echo",
            "flight_vehicle_heartbeat",
            "flight_vehicle_extended_state",
            "flight_vehicle_global_position",
            "flight_vehicle_local_position",
            "flight_prearm_boundary",
            "flight_airborne_ready_boundary",
            "flight_warmup_motion_boundary",
            "flight_measurement_boundary",
            "actual_control_phase_start",
            "real_command_offered",
            "real_command_ack",
            "real_requested_telemetry",
            "transaction_result",
            "actual_control_phase_complete",
            "flight_plan_shutdown",
            "actual_control_shutdown",
        }
        unknown_events = sorted(set(event_groups) - allowed_events)
        if unknown_events:
            raise M4ValidationError(
                f"capacity actual-control event envelope differs: {unknown_events}"
            )
        for forbidden in (
            "flight_command_rejected",
            "duplicate_flight_command_ack",
            "duplicate_flight_timesync_echo",
        ):
            if event_groups.get(forbidden):
                raise M4ValidationError(f"forbidden flight event observed: {forbidden}")
        datagram_index = index_actual_control_datagrams(
            events,
            expected_peers={
                (f"10.71.{uav}.10", 14600 + uav): uav
                for uav in EXPECTED_UAVS
            },
            expected_rx_tos=184,
        )
        consumed_frame_occurrences: set[tuple[int, int]] = set()

        def bound_flight_event_frame(
            record: Mapping[str, Any], message_id: int
        ) -> tuple[dict[str, Any], Mapping[str, Any]]:
            declared = _flight_event_frame(record, message_id)
            decoded, parent = bind_actual_control_frame(
                record,
                expected_message_id=message_id,
                datagram_index=datagram_index,
                consumed_occurrences=consumed_frame_occurrences,
                frame_decoder=_decode_flight_mavlink_frame,
            )
            if decoded != declared:
                raise M4ValidationError(
                    "standalone and UDP-parent MAVLink decodes differ"
                )
            return declared, parent
        plan_starts = event_groups.get("flight_plan_started", [])
        if len(plan_starts) != 1:
            raise M4ValidationError("flight plan start cardinality differs")
        plan_start = plan_starts[0]
        if (
            plan_start.get("airborne_gate_contract") != AIRBORNE_GATE_CONTRACT
            or plan_start.get("declared_airborne_gate") != expected_gate
            or plan_start.get("airborne_ready_deadline_monotonic_ns")
            != ready_deadline_ns
            or plan_start.get("measurement_start_monotonic_ns") != start_ns
            or plan_start.get("measurement_end_monotonic_ns") != end_ns
            or isinstance(plan_start.get("monotonic_ns"), bool)
            or not isinstance(plan_start.get("monotonic_ns"), int)
            or plan_start["monotonic_ns"] >= ready_deadline_ns
        ):
            raise M4ValidationError("flight plan start/gate binding differs")
        if len(event_groups.get("flight_plan_shutdown", [])) != 1:
            raise M4ValidationError("flight plan shutdown cardinality differs")
        ready_boundaries = event_groups.get("flight_airborne_ready_boundary", [])
        if (
            len(ready_boundaries) != 1
            or ready_boundaries[0].get("target_monotonic_ns") != ready_deadline_ns
            or not isinstance(ready_boundaries[0].get("monotonic_ns"), int)
            or not ready_deadline_ns
            <= ready_boundaries[0]["monotonic_ns"]
            <= ready_deadline_ns + 500_000_000
        ):
            raise M4ValidationError("flight airborne-ready boundary evidence differs")
        boundaries = event_groups.get("flight_measurement_boundary", [])
        if (
            len(boundaries) != 2
            or [item.get("boundary") for item in boundaries] != ["start", "end"]
            or [item.get("target_monotonic_ns") for item in boundaries]
            != [start_ns, end_ns]
            or any(
                not isinstance(item.get("monotonic_ns"), int)
                or not target <= item["monotonic_ns"] <= target + 500_000_000
                for item, target in zip(boundaries, (start_ns, end_ns))
            )
        ):
            raise M4ValidationError("flight measurement boundary evidence differs")
        motion_boundaries = event_groups.get("flight_warmup_motion_boundary", [])
        if (
            len(motion_boundaries) != 2
            or [item.get("boundary") for item in motion_boundaries]
            != ["start", "complete"]
            or any(
                item.get("target_monotonic_ns") != ready_deadline_ns
                or item.get("deadline_monotonic_ns")
                != warmup_motion_deadline_ns
                or isinstance(item.get("monotonic_ns"), bool)
                or not isinstance(item.get("monotonic_ns"), int)
                for item in motion_boundaries
            )
            or not ready_deadline_ns
            <= motion_boundaries[0]["monotonic_ns"]
            <= ready_deadline_ns + 500_000_000
            or isinstance(
                motion_boundaries[1].get("completed_monotonic_ns"), bool
            )
            or not isinstance(
                motion_boundaries[1].get("completed_monotonic_ns"), int
            )
            or not motion_boundaries[0]["monotonic_ns"]
            <= motion_boundaries[1]["completed_monotonic_ns"]
            <= motion_boundaries[1]["monotonic_ns"]
            < warmup_motion_deadline_ns
        ):
            raise M4ValidationError("flight warm-up motion boundary evidence differs")

        offers_by_transaction: dict[str, dict[str, Any]] = {}
        for offer in event_groups.get("flight_command_offered", []):
            transaction_id = offer.get("transaction_id")
            stage = offer.get("flight_stage")
            definition = STAGE_BY_NAME.get(str(stage))
            uav = offer.get("uav")
            attempt = offer.get("attempt")
            if stage in PRE_MEASUREMENT_STAGES:
                attempt_key = "pre_measurement_max_attempts"
            elif stage in WARMUP_MOTION_STAGES:
                attempt_key = "warmup_motion_max_attempts"
            elif stage in POST_MEASUREMENT_STAGES:
                attempt_key = "post_measurement_max_attempts"
            else:
                attempt_key = ""
            if (
                not isinstance(transaction_id, str)
                or transaction_id in offers_by_transaction
                or definition is None
                or isinstance(uav, bool)
                or uav not in EXPECTED_UAVS
                or isinstance(attempt, bool)
                or not isinstance(attempt, int)
                or not attempt_key
                or not 1 <= attempt <= int(expected_gate[attempt_key])
                or transaction_id
                != f"m4-capacity-flight:{stage}:uav{uav}:attempt{attempt}"
            ):
                raise M4ValidationError("flight command offer identity differs")
            sent_ns = offer.get("sent_monotonic_ns")
            if (
                isinstance(sent_ns, bool)
                or not isinstance(sent_ns, int)
                or (
                    stage in PRE_MEASUREMENT_STAGES
                    and sent_ns >= ready_deadline_ns
                )
                or (
                    stage in WARMUP_MOTION_STAGES
                    and not ready_deadline_ns
                    <= sent_ns
                    < warmup_motion_deadline_ns
                )
                or (
                    stage in POST_MEASUREMENT_STAGES
                    and (
                        (stage == "land" and not post_control_boundary_ns <= sent_ns < landing_deadline_ns)
                        or (stage == "disarm" and not post_control_boundary_ns <= sent_ns < disarm_deadline_ns)
                    )
                )
                or offer.get("flight_stage_code") != definition["stage_code"]
                or offer.get("command_id") != definition["command_id"]
                or offer.get("command_encoding") != definition["encoding"]
                or offer.get("command_message_id")
                != (75 if definition["encoding"] == "COMMAND_INT" else 76)
                or offer.get("target_system") != uav
                or offer.get("target_component") != 1
                or offer.get("source_ip") != "10.71.0.10"
                or offer.get("source_udp_port") != 14600
                or offer.get("destination_ip") != f"10.71.{uav}.10"
                or offer.get("destination_udp_port") != 14600 + uav
                or offer.get("tos") != 184
            ):
                raise M4ValidationError("flight command offer route/timing differs")
            try:
                command_frame = bytes.fromhex(str(offer["command_frame_hex"]))
                timesync_frame = bytes.fromhex(str(offer["timesync_frame_hex"]))
                request = bytes.fromhex(str(offer["request_transport_payload_hex"]))
            except (KeyError, ValueError) as exc:
                raise M4ValidationError("flight command preserved bytes differ") from exc
            command = _decode_flight_mavlink_frame(command_frame)
            timesync = _decode_flight_mavlink_frame(timesync_frame)
            request_frames = split_exact_mavlink_datagram(request)
            token = flight_timesync_token(
                run_nonce=str(run.get("run_nonce")),
                stage_code=int(definition["stage_code"]),
                uav=int(uav),
                ordinal=int(attempt),
            )
            encoding = str(definition["encoding"])
            if encoding == "COMMAND_LONG":
                if len(command["payload"]) != 33:
                    raise M4ValidationError(
                        "flight COMMAND_LONG payload length differs"
                    )
                unpacked_long = struct.unpack("<7fHBBB", command["payload"])
                command_matches = (
                    command["message_id"] == 76
                    and unpacked_long[7:]
                    == (definition["command_id"], uav, 1, attempt - 1)
                    and all(
                        math.isclose(
                            observed, expected, rel_tol=0.0, abs_tol=1e-5
                        )
                        for observed, expected in zip(
                            unpacked_long[:7], definition["params"]
                        )
                    )
                    and offer.get("command_params")
                    == list(definition["params"])
                    and offer.get("command_int_frame") is None
                    and offer.get("command_int_x_e7") is None
                    and offer.get("command_int_y_e7") is None
                    and offer.get("command_int_z_m") is None
                )
                expected_command_message_id = 76
            elif encoding == "COMMAND_INT":
                if len(command["payload"]) != 35:
                    raise M4ValidationError(
                        "flight COMMAND_INT payload length differs"
                    )
                unpacked_int = struct.unpack("<4fiifHBBBBB", command["payload"])
                command_matches = (
                    command["message_id"] == 75
                    and all(
                        math.isclose(
                            observed, expected, rel_tol=0.0, abs_tol=1e-5
                        )
                        for observed, expected in zip(
                            unpacked_int[:4], definition["params"]
                        )
                    )
                    and unpacked_int[4] == offer.get("command_int_x_e7")
                    and unpacked_int[5] == offer.get("command_int_y_e7")
                    and math.isclose(
                        unpacked_int[6],
                        float(definition["target_relative_alt_m"]),
                        rel_tol=0.0,
                        abs_tol=1e-5,
                    )
                    and unpacked_int[7:]
                    == (
                        definition["command_id"],
                        uav,
                        1,
                        definition["frame"],
                        0,
                        0,
                    )
                    and offer.get("command_params")
                    == list(definition["params"])
                    and offer.get("command_int_frame") == definition["frame"]
                    and offer.get("command_int_z_m")
                    == definition["target_relative_alt_m"]
                    and isinstance(offer.get("command_int_x_e7"), int)
                    and not isinstance(offer.get("command_int_x_e7"), bool)
                    and isinstance(offer.get("command_int_y_e7"), int)
                    and not isinstance(offer.get("command_int_y_e7"), bool)
                )
                expected_command_message_id = 75
            else:
                raise M4ValidationError("flight command encoding differs")
            if (
                not command_matches
                or command["system_id"] != 255
                or command["component_id"] != 190
                or timesync["message_id"] != 111
                or timesync["system_id"] != 255
                or timesync["component_id"] != 190
                or struct.unpack("<qq", timesync["payload"]) != (0, token)
                or len(request_frames) != 2
                or [frame["message_id"] for frame in request_frames]
                != [expected_command_message_id, 111]
                or request_frames[0]["bytes"] != command_frame
                or request_frames[1]["bytes"] != timesync_frame
                or request != command_frame + timesync_frame
                or offer.get("command_frame_sha256") != command["sha256"]
                or offer.get("timesync_frame_sha256") != timesync["sha256"]
                or offer.get("timesync_request_tc1") != 0
                or offer.get("timesync_request_ts1") != token
                or offer.get("request_transport_payload_sha256")
                != hashlib.sha256(request).hexdigest()
                or offer.get("request_transport_payload_size") != len(request)
                or offer.get("request_transport_send_return_size") != len(request)
            ):
                raise M4ValidationError("flight command/TIMESYNC bytes differ")
            offers_by_transaction[transaction_id] = offer

        required_uplink_parents: dict[int, Mapping[str, Any]] = {}
        ack_by_transaction: dict[str, dict[str, Any]] = {}
        for ack in event_groups.get("flight_command_ack", []):
            transaction_id = str(ack.get("transaction_id"))
            offer = offers_by_transaction.get(transaction_id)
            decoded, parent = bound_flight_event_frame(ack, 77)
            required_uplink_parents[int(parent["event_sequence"])] = parent
            received_ns = ack.get("received_monotonic_ns")
            if len(decoded["payload"]) > 10 or len(decoded["payload"]) < 3:
                raise M4ValidationError("flight COMMAND_ACK payload is truncated")
            payload_command, payload_result = struct.unpack(
                "<HB", decoded["payload"][:3].ljust(3, b"\0")
            )
            if (
                offer is None
                or transaction_id in ack_by_transaction
                or decoded["system_id"] != offer["uav"]
                or decoded["component_id"] != 1
                or ack.get("command_id") != offer["command_id"]
                or ack.get("command_result") not in {0, 1}
                or payload_command != offer["command_id"]
                or payload_result != ack.get("command_result")
                or isinstance(received_ns, bool)
                or not isinstance(received_ns, int)
                or not offer["sent_monotonic_ns"]
                <= received_ns
                < offer["sent_monotonic_ns"] + OUTCOME_TIMEOUT_NS
            ):
                raise M4ValidationError("flight COMMAND_ACK correlation differs")
            ack_by_transaction[transaction_id] = ack

        timesync_by_transaction: dict[str, dict[str, Any]] = {}
        for response in event_groups.get("flight_command_timesync_echo", []):
            transaction_id = str(response.get("transaction_id"))
            offer = offers_by_transaction.get(transaction_id)
            decoded, parent = bound_flight_event_frame(response, 111)
            required_uplink_parents[int(parent["event_sequence"])] = parent
            received_ns = response.get("received_monotonic_ns")
            if (
                offer is None
                or transaction_id in timesync_by_transaction
                or decoded["system_id"] != offer["uav"]
                or decoded["component_id"] != 1
                or response.get("timesync_ts1")
                != offer.get("timesync_request_ts1")
                or not isinstance(response.get("timesync_tc1"), int)
                or isinstance(response.get("timesync_tc1"), bool)
                or response["timesync_tc1"] <= 0
                or struct.unpack("<qq", decoded["payload"])
                != (response["timesync_tc1"], response["timesync_ts1"])
                or isinstance(received_ns, bool)
                or not isinstance(received_ns, int)
                or not offer["sent_monotonic_ns"]
                <= received_ns
                < offer["sent_monotonic_ns"] + OUTCOME_TIMEOUT_NS
            ):
                raise M4ValidationError("flight TIMESYNC echo correlation differs")
            timesync_by_transaction[transaction_id] = response

        late_ack_by_transaction: dict[str, dict[str, Any]] = {}
        for late_ack in event_groups.get("late_flight_command_ack", []):
            transaction_id = str(late_ack.get("transaction_id"))
            offer = offers_by_transaction.get(transaction_id)
            decoded, parent = bound_flight_event_frame(late_ack, 77)
            required_uplink_parents[int(parent["event_sequence"])] = parent
            payload_command, payload_result = struct.unpack(
                "<HB", decoded["payload"][:3].ljust(3, b"\0")
            )
            received_ns = late_ack.get("received_monotonic_ns")
            if (
                offer is None
                or transaction_id in late_ack_by_transaction
                or transaction_id in ack_by_transaction
                or decoded["system_id"] != offer["uav"]
                or decoded["component_id"] != 1
                or payload_command != offer["command_id"]
                or late_ack.get("command_id") != offer["command_id"]
                or late_ack.get("command_result") not in {0, 1}
                or payload_result != late_ack.get("command_result")
                or not isinstance(received_ns, int)
                or isinstance(received_ns, bool)
                or received_ns
                < offer["sent_monotonic_ns"] + OUTCOME_TIMEOUT_NS
            ):
                raise M4ValidationError("late flight ACK lineage differs")
            late_ack_by_transaction[transaction_id] = late_ack

        late_timesync_by_transaction: dict[str, dict[str, Any]] = {}
        for late_echo in event_groups.get("late_flight_timesync_echo", []):
            transaction_id = str(late_echo.get("transaction_id"))
            offer = offers_by_transaction.get(transaction_id)
            decoded, parent = bound_flight_event_frame(late_echo, 111)
            required_uplink_parents[int(parent["event_sequence"])] = parent
            received_ns = late_echo.get("received_monotonic_ns")
            if (
                offer is None
                or transaction_id in late_timesync_by_transaction
                or transaction_id in timesync_by_transaction
                or decoded["system_id"] != offer["uav"]
                or decoded["component_id"] != 1
                or late_echo.get("timesync_ts1")
                != offer.get("timesync_request_ts1")
                or not isinstance(late_echo.get("timesync_tc1"), int)
                or isinstance(late_echo.get("timesync_tc1"), bool)
                or late_echo["timesync_tc1"] <= 0
                or struct.unpack("<qq", decoded["payload"])
                != (late_echo["timesync_tc1"], late_echo["timesync_ts1"])
                or not isinstance(received_ns, int)
                or isinstance(received_ns, bool)
                or received_ns
                < offer["sent_monotonic_ns"] + OUTCOME_TIMEOUT_NS
            ):
                raise M4ValidationError("late flight TIMESYNC lineage differs")
            late_timesync_by_transaction[transaction_id] = late_echo

        accepted = event_groups.get("flight_command_complete", [])
        rejected = event_groups.get("flight_command_retryable_rejection_complete", [])
        timeouts = event_groups.get("flight_command_outcome_timeout", [])
        terminal_by_transaction: dict[str, dict[str, Any]] = {}
        terminal_kind: dict[str, str] = {}
        for kind, records in (
            ("accepted", accepted),
            ("temporarily_rejected", rejected),
            ("timeout", timeouts),
        ):
            for record in records:
                transaction_id = str(record.get("transaction_id"))
                if transaction_id in terminal_by_transaction:
                    raise M4ValidationError("flight command has multiple terminal outcomes")
                terminal_by_transaction[transaction_id] = record
                terminal_kind[transaction_id] = kind
        if set(terminal_by_transaction) != set(offers_by_transaction):
            raise M4ValidationError("flight command terminal outcome coverage differs")
        accepted_by_stage_uav: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
        attempts_by_stage_uav: dict[tuple[str, int], list[int]] = defaultdict(list)
        for transaction_id, offer in offers_by_transaction.items():
            kind = terminal_kind[transaction_id]
            key = (str(offer["flight_stage"]), int(offer["uav"]))
            attempts_by_stage_uav[key].append(int(offer["attempt"]))
            if kind in {"accepted", "temporarily_rejected"}:
                if transaction_id not in ack_by_transaction or transaction_id not in timesync_by_transaction:
                    raise M4ValidationError("completed flight attempt lacks ACK/TIMESYNC")
                expected_result = (
                    MAV_RESULT_ACCEPTED if kind == "accepted" else 1
                )
                if ack_by_transaction[transaction_id].get("command_result") != expected_result:
                    raise M4ValidationError("flight terminal/ACK result differs")
                terminal = terminal_by_transaction[transaction_id]
                ack = ack_by_transaction[transaction_id]
                timesync_response = timesync_by_transaction[transaction_id]
                expected_terminal_ack = {
                    field: ack.get(field)
                    for field in (
                        "received_monotonic_ns",
                        "command_id",
                        "command_result",
                        "source_system",
                        "source_component",
                        "transport_payload_sha256",
                        "mavlink_frame_sha256",
                    )
                }
                expected_terminal_timesync = {
                    field: timesync_response.get(field)
                    for field in (
                        "received_monotonic_ns",
                        "timesync_tc1",
                        "timesync_ts1",
                        "source_system",
                        "source_component",
                        "transport_payload_sha256",
                        "mavlink_frame_sha256",
                    )
                }
                completed_ns = terminal.get("completed_monotonic_ns")
                if (
                    terminal.get("flight_stage") != offer["flight_stage"]
                    or terminal.get("flight_stage_code")
                    != offer["flight_stage_code"]
                    or terminal.get("uav") != offer["uav"]
                    or terminal.get("attempt") != offer["attempt"]
                    or terminal.get("command_id") != offer["command_id"]
                    or terminal.get("sent_monotonic_ns")
                    != offer["sent_monotonic_ns"]
                    or terminal.get("command_frame_sha256")
                    != offer["command_frame_sha256"]
                    or terminal.get("timesync_frame_sha256")
                    != offer["timesync_frame_sha256"]
                    or terminal.get("request_transport_payload_sha256")
                    != offer["request_transport_payload_sha256"]
                    or terminal.get("ack") != expected_terminal_ack
                    or terminal.get("timesync_response")
                    != expected_terminal_timesync
                    or isinstance(completed_ns, bool)
                    or not isinstance(completed_ns, int)
                    or completed_ns
                    < max(
                        int(ack["received_monotonic_ns"]),
                        int(timesync_response["received_monotonic_ns"]),
                    )
                    or completed_ns
                    >= int(offer["sent_monotonic_ns"]) + OUTCOME_TIMEOUT_NS
                ):
                    raise M4ValidationError("flight terminal raw outcome binding differs")
            elif transaction_id in ack_by_transaction and transaction_id in timesync_by_transaction:
                raise M4ValidationError("timed-out flight attempt has both outcomes")
            if kind == "timeout":
                terminal = terminal_by_transaction[transaction_id]
                missing = terminal.get("missing")
                if (
                    terminal.get("flight_stage") != offer["flight_stage"]
                    or terminal.get("uav") != offer["uav"]
                    or terminal.get("attempt") != offer["attempt"]
                    or terminal.get("command_id") != offer["command_id"]
                    or terminal.get("sent_monotonic_ns")
                    != offer["sent_monotonic_ns"]
                    or terminal.get("deadline_monotonic_ns")
                    != offer["sent_monotonic_ns"] + OUTCOME_TIMEOUT_NS
                    or terminal.get("timeout_ns") != OUTCOME_TIMEOUT_NS
                    or not isinstance(terminal.get("observed_monotonic_ns"), int)
                    or terminal["observed_monotonic_ns"]
                    < terminal["deadline_monotonic_ns"]
                    or not isinstance(missing, dict)
                    or set(missing) != {"ack", "timesync"}
                    or any(not isinstance(value, bool) for value in missing.values())
                    or not any(missing.values())
                    or missing["ack"] != (transaction_id not in ack_by_transaction)
                    or missing["timesync"]
                    != (transaction_id not in timesync_by_transaction)
                ):
                    raise M4ValidationError("flight timeout raw outcome binding differs")
            if kind == "accepted":
                accepted_by_stage_uav[key].append(offer)
        expected_keys = {
            (stage, uav)
            for stage in (
                *PRE_MEASUREMENT_STAGES,
                *WARMUP_MOTION_STAGES,
                *POST_MEASUREMENT_STAGES,
            )
            for uav in EXPECTED_UAVS
        }
        if set(attempts_by_stage_uav) != expected_keys:
            raise M4ValidationError("flight stage/UAV offer coverage differs")

        for transaction_id in late_ack_by_transaction:
            terminal = terminal_by_transaction.get(transaction_id, {})
            if (
                terminal_kind.get(transaction_id) != "timeout"
                or terminal.get("missing", {}).get("ack") is not True
            ):
                raise M4ValidationError("late flight ACK lacks an ACK-missing timeout owner")
        for transaction_id in late_timesync_by_transaction:
            terminal = terminal_by_transaction.get(transaction_id, {})
            if (
                terminal_kind.get(transaction_id) != "timeout"
                or terminal.get("missing", {}).get("timesync") is not True
            ):
                raise M4ValidationError(
                    "late flight TIMESYNC lacks a TIMESYNC-missing timeout owner"
                )
        for key in sorted(expected_keys):
            attempts = sorted(attempts_by_stage_uav[key])
            accepted_attempts = [
                int(item["attempt"]) for item in accepted_by_stage_uav[key]
            ]
            maximum_attempts = int(
                expected_gate[
                    "pre_measurement_max_attempts"
                    if key[0] in PRE_MEASUREMENT_STAGES
                    else (
                        "warmup_motion_max_attempts"
                        if key[0] in WARMUP_MOTION_STAGES
                        else "post_measurement_max_attempts"
                    )
                ]
            )
            if (
                attempts != list(range(1, len(attempts) + 1))
                or len(attempts) > maximum_attempts
                or accepted_attempts != [attempts[-1]]
                or any(
                    terminal_kind[
                        f"m4-capacity-flight:{key[0]}:uav{key[1]}:attempt{attempt}"
                    ]
                    not in {"timeout", "temporarily_rejected"}
                    for attempt in attempts[:-1]
                )
            ):
                raise M4ValidationError(f"flight attempts/final acceptance differ: {key}")

        def terminal_time_ns(transaction_id: str) -> int:
            terminal = terminal_by_transaction[transaction_id]
            key = (
                "observed_monotonic_ns"
                if terminal_kind[transaction_id] == "timeout"
                else "completed_monotonic_ns"
            )
            value = terminal.get(key)
            if isinstance(value, bool) or not isinstance(value, int):
                raise M4ValidationError("flight terminal timestamp differs")
            return value

        ordered_stages = [str(item["stage"]) for item in STAGE_DEFINITIONS]
        for previous_stage, following_stage in zip(
            ordered_stages, ordered_stages[1:]
        ):
            previous_terminal_ns = max(
                terminal_time_ns(transaction_id)
                for transaction_id, item in offers_by_transaction.items()
                if item["flight_stage"] == previous_stage
            )
            following_sent_ns = min(
                int(item["sent_monotonic_ns"])
                for item in offers_by_transaction.values()
                if item["flight_stage"] == following_stage
            )
            if previous_terminal_ns > following_sent_ns:
                raise M4ValidationError(
                    f"flight global stage order overlaps: {previous_stage}/{following_stage}"
                )

        # Every retry and repeated command-id boundary must contain an exact
        # three-second quiet drain before the next indistinguishable ACK can
        # be admitted.
        drains = event_groups.get("flight_command_quiet_drain", [])
        for drain in drains:
            if (
                not isinstance(drain.get("started_monotonic_ns"), int)
                or not isinstance(drain.get("last_response_monotonic_ns"), int)
                or not isinstance(drain.get("completed_monotonic_ns"), int)
                or drain.get("required_quiet_ns") != OUTCOME_TIMEOUT_NS
                or drain.get("reason")
                not in {"bounded_retry", "same_command_id_stage_boundary"}
                or not isinstance(drain.get("guarded_uavs"), list)
                or len(drain["guarded_uavs"]) != len(set(drain["guarded_uavs"]))
                or not set(drain["guarded_uavs"])
                <= {f"uav{uav}" for uav in EXPECTED_UAVS}
                or not drain["started_monotonic_ns"]
                <= drain["last_response_monotonic_ns"]
                <= drain["completed_monotonic_ns"]
                or drain["completed_monotonic_ns"]
                - drain["last_response_monotonic_ns"]
                < OUTCOME_TIMEOUT_NS
                or drain["completed_monotonic_ns"]
                - drain["started_monotonic_ns"]
                > OUTCOME_TIMEOUT_NS + 250_000_000
            ):
                raise M4ValidationError("flight command quiet drain differs")
            started_ns = int(drain["started_monotonic_ns"])
            completed_ns = int(drain["completed_monotonic_ns"])
            guarded = set(drain["guarded_uavs"])
            command_id = drain.get("command_id")
            late_response_times: list[int] = []
            for transaction_id, late_record in (
                *late_ack_by_transaction.items(),
                *late_timesync_by_transaction.items(),
            ):
                offer = offers_by_transaction[transaction_id]
                received_ns = int(late_record["received_monotonic_ns"])
                if (
                    offer.get("command_id") == command_id
                    and f"uav{offer['uav']}" in guarded
                    and started_ns <= received_ns <= completed_ns
                ):
                    late_response_times.append(received_ns)
            expected_last_response_ns = max(
                [started_ns, *late_response_times]
            )
            # The producer has a hard three-second drain deadline: any late
            # response would restart quiet time and make the bounded attempt
            # fail before a drain record can be emitted.  Reject forged
            # evidence that claims both a successful bounded drain and such a
            # response.
            if (
                late_response_times
                or expected_last_response_ns != started_ns
                or drain.get("last_response_monotonic_ns") != started_ns
            ):
                raise M4ValidationError(
                    "flight command quiet drain last response is not raw-derived"
                )
        for key, attempts in attempts_by_stage_uav.items():
            if len(attempts) <= 1:
                continue
            offers = sorted(
                (item for item in offers_by_transaction.values() if (item["flight_stage"], item["uav"]) == key),
                key=lambda item: item["attempt"],
            )
            for previous, following in zip(offers, offers[1:]):
                previous_terminal_ns = terminal_time_ns(
                    str(previous["transaction_id"])
                )
                if not any(
                    drain.get("reason") == "bounded_retry"
                    and drain.get("command_id") == previous["command_id"]
                    and f"uav{key[1]}" in drain.get("guarded_uavs", [])
                    and previous_terminal_ns
                    <= drain.get("started_monotonic_ns", -1)
                    and drain.get("completed_monotonic_ns", 1 << 63)
                    <= following["sent_monotonic_ns"]
                    for drain in drains
                ):
                    raise M4ValidationError("flight retry lacks a preceding quiet drain")
        last_stage_by_command: dict[int, str] = {}
        repeated_boundaries: list[tuple[str, str, int]] = []
        for definition in STAGE_DEFINITIONS:
            command_id = int(definition["command_id"])
            stage = str(definition["stage"])
            if command_id in last_stage_by_command:
                repeated_boundaries.append(
                    (last_stage_by_command[command_id], stage, command_id)
                )
            last_stage_by_command[command_id] = stage
        for previous_stage, following_stage, command_id in repeated_boundaries:
            previous_completed_ns = max(
                int(terminal_by_transaction[item["transaction_id"]]["completed_monotonic_ns"])
                for item in offers_by_transaction.values()
                if item["flight_stage"] == previous_stage
                and terminal_kind[item["transaction_id"]] == "accepted"
            )
            following_sent_ns = min(
                int(item["sent_monotonic_ns"])
                for item in offers_by_transaction.values()
                if item["flight_stage"] == following_stage
            )
            if not any(
                drain.get("reason") == "same_command_id_stage_boundary"
                and drain.get("command_id") == command_id
                and set(drain.get("guarded_uavs", []))
                == {f"uav{uav}" for uav in EXPECTED_UAVS}
                and previous_completed_ns
                <= drain.get("started_monotonic_ns", -1)
                <= drain.get("completed_monotonic_ns", -1)
                <= following_sent_ns
                for drain in drains
            ):
                raise M4ValidationError(
                    "repeated MAV_CMD stages lack exact quiet drain"
                )

        state_specs = {
            "heartbeat": ("flight_vehicle_heartbeat", 0, HEARTBEAT_FRESHNESS_NS),
            "extended": ("flight_vehicle_extended_state", 245, HIGH_RATE_STATE_FRESHNESS_NS),
            "global": ("flight_vehicle_global_position", 33, HIGH_RATE_STATE_FRESHNESS_NS),
            "local": ("flight_vehicle_local_position", 32, HIGH_RATE_STATE_FRESHNESS_NS),
        }
        state_source_frames = {
            "heartbeat": "ardupilot_body_ned",
            "extended": "ardupilot_body_ned",
            "global": "ardupilot_global_wgs84",
            "local": "ardupilot_local_ned",
        }
        histories: dict[str, dict[int, list[dict[str, Any]]]] = {
            name: {uav: [] for uav in EXPECTED_UAVS} for name in state_specs
        }
        for name, (event_name, message_id, _freshness_ns) in state_specs.items():
            for record in event_groups.get(event_name, []):
                decoded, parent = bound_flight_event_frame(record, message_id)
                required_uplink_parents[int(parent["event_sequence"])] = parent
                uav = decoded["system_id"]
                received_ns = record.get("received_monotonic_ns")
                if (
                    uav not in EXPECTED_UAVS
                    or decoded["component_id"] != 1
                    or isinstance(received_ns, bool)
                    or not isinstance(received_ns, int)
                    or record.get("source_topic") != "actual_sitl_mavlink"
                    or record.get("source_frame") != state_source_frames[name]
                    or record.get("transform_version")
                    != "ams-m4-coordinate-frames-v1"
                ):
                    raise M4ValidationError("flight vehicle state identity/time differs")
                payload = decoded["payload"]
                if name == "heartbeat":
                    if len(payload) != 9:
                        raise M4ValidationError("flight HEARTBEAT payload differs")
                    (
                        custom_mode,
                        mav_type,
                        autopilot,
                        base_mode,
                        system_status,
                        _mavlink_version,
                    ) = struct.unpack("<IBBBBB", payload.ljust(9, b"\0"))
                    raw_matches = (
                        record.get("custom_mode") == custom_mode
                        and record.get("mav_type") == mav_type
                        and record.get("autopilot") == autopilot
                        and record.get("base_mode") == base_mode
                        and record.get("system_status") == system_status
                    )
                elif name == "extended":
                    if len(payload) != 2:
                        raise M4ValidationError("flight EXTENDED_SYS_STATE payload differs")
                    vtol_state, landed_state = struct.unpack(
                        "<BB", payload.ljust(2, b"\0")
                    )
                    raw_matches = (
                        record.get("vtol_state") == vtol_state
                        and record.get("landed_state") == landed_state
                    )
                elif name == "global":
                    if len(payload) != 28:
                        raise M4ValidationError("flight GLOBAL_POSITION_INT payload differs")
                    (
                        _time_boot_ms,
                        lat_e7,
                        lon_e7,
                        alt_msl_mm,
                        relative_alt_mm,
                        vx_cms,
                        vy_cms,
                        vz_cms,
                        _heading_cdeg,
                    ) = struct.unpack("<IiiiihhhH", payload.ljust(28, b"\0"))
                    raw_matches = all(
                        record.get(key) == value
                        for key, value in {
                            "lat_e7": lat_e7,
                            "lon_e7": lon_e7,
                            "alt_msl_mm": alt_msl_mm,
                            "relative_alt_mm": relative_alt_mm,
                            "vx_cms": vx_cms,
                            "vy_cms": vy_cms,
                            "vz_cms": vz_cms,
                        }.items()
                    )
                else:
                    if len(payload) != 28:
                        raise M4ValidationError("flight LOCAL_POSITION_NED payload differs")
                    (
                        _time_boot_ms,
                        x_m,
                        y_m,
                        z_down_m,
                        vx_mps,
                        vy_mps,
                        vz_mps,
                    ) = struct.unpack("<Iffffff", payload.ljust(28, b"\0"))
                    raw_matches = all(
                        isinstance(record.get(key), (int, float))
                        and not isinstance(record.get(key), bool)
                        and math.isclose(
                            float(record[key]), float(value), rel_tol=0.0, abs_tol=1e-6
                        )
                        for key, value in {
                            "x_m": x_m,
                            "y_m": y_m,
                            "z_down_m": z_down_m,
                            "vx_mps": vx_mps,
                            "vy_mps": vy_mps,
                            "vz_mps": vz_mps,
                        }.items()
                    )
                if not raw_matches:
                    raise M4ValidationError(
                        f"flight {name} declared state differs from raw MAVLink bytes"
                    )
                histories[name][uav].append(record)
            for uav in EXPECTED_UAVS:
                history = histories[name][uav]
                times = [int(item["received_monotonic_ns"]) for item in history]
                # Multiple MAVLink frames can legitimately share one UDP
                # recvmsg timestamp.  Frame occurrences and event_sequence,
                # not an invented sub-datagram clock, disambiguate them.
                if not history or times != sorted(times):
                    raise M4ValidationError(f"uav{uav} {name} state history is absent/nonmonotonic")

        reposition_terminal_ns = 0
        for transaction_id, offer in offers_by_transaction.items():
            if offer["flight_stage"] != "reposition":
                continue
            uav = int(offer["uav"])
            sent_ns = int(offer["sent_monotonic_ns"])
            position = _latest_at_or_before(histories["global"][uav], sent_ns)
            if (
                position is None
                or sent_ns - int(position["received_monotonic_ns"])
                > HIGH_RATE_STATE_FRESHNESS_NS
                or offer.get("command_int_x_e7") != position.get("lat_e7")
                or offer.get("command_int_y_e7") != position.get("lon_e7")
                or terminal_kind.get(transaction_id) != "accepted"
            ):
                raise M4ValidationError(
                    f"uav{uav} reposition does not bind fresh current WGS84 position"
                )
            reposition_terminal_ns = max(
                reposition_terminal_ns, terminal_time_ns(transaction_id)
            )
        if (
            reposition_terminal_ns <= 0
            or motion_boundaries[1].get("completed_monotonic_ns")
            < reposition_terminal_ns
        ):
            raise M4ValidationError(
                "warm-up motion completion precedes reposition outcomes"
            )

        # Every raw state frame, flight COMMAND_ACK, and token-bearing flight
        # TIMESYNC occurrence must own exactly one derived flight event.  This
        # catches responses silently consumed by the concurrent workload path.
        relevant_raw_occurrences: set[tuple[int, int]] = set()
        flight_command_ids = {
            int(definition["command_id"]) for definition in STAGE_DEFINITIONS
        }
        flight_tokens = {
            int(offer["timesync_request_ts1"])
            for offer in offers_by_transaction.values()
        }
        state_message_ids = {message_id for _event, message_id, _age in state_specs.values()}
        for parent_lists in datagram_index.values():
            for parent in parent_lists:
                parent_sequence = int(parent["event_sequence"])
                for frame in parent["frames"]:
                    message_id = int(frame["message_id"])
                    relevant = message_id in state_message_ids
                    if message_id == 77:
                        decoded = _decode_flight_mavlink_frame(frame["bytes"])
                        if len(decoded["payload"]) < 2:
                            raise M4ValidationError("raw COMMAND_ACK command field is truncated")
                        relevant = (
                            int.from_bytes(decoded["payload"][:2], "little")
                            in flight_command_ids
                        )
                    elif message_id == 111:
                        decoded = _decode_flight_mavlink_frame(frame["bytes"])
                        if len(decoded["payload"]) != 16:
                            raise M4ValidationError("raw TIMESYNC payload length differs")
                        _tc1, token = struct.unpack("<qq", decoded["payload"])
                        relevant = token in flight_tokens
                    if relevant:
                        relevant_raw_occurrences.add(
                            (parent_sequence, int(frame["offset"]))
                        )
        if relevant_raw_occurrences != consumed_frame_occurrences:
            raise M4ValidationError(
                "raw flight MAVLink occurrence coverage differs: "
                f"unmapped={len(relevant_raw_occurrences-consumed_frame_occurrences)} "
                f"foreign={len(consumed_frame_occurrences-relevant_raw_occurrences)}"
            )

        network_lineage = _validate_airborne_network_lineage(
            run_dir,
            run=run,
            offers_by_transaction=offers_by_transaction,
            terminal_kind=terminal_kind,
            required_uplink_parents=required_uplink_parents,
        )

        def state_valid(name: str, record: Mapping[str, Any]) -> bool:
            if name == "heartbeat":
                return (
                    isinstance(record.get("base_mode"), int)
                    and record["base_mode"] & MAV_MODE_FLAG_SAFETY_ARMED != 0
                    and record.get("custom_mode") == COPTER_MODE_GUIDED
                )
            if name == "extended":
                return record.get("landed_state") == MAV_LANDED_STATE_IN_AIR
            if name == "global":
                return (
                    isinstance(record.get("relative_alt_mm"), int)
                    and record["relative_alt_mm"]
                    >= int(MINIMUM_RELATIVE_ALT_M * 1000)
                )
            return (
                isinstance(record.get("z_down_m"), (int, float))
                and not isinstance(record.get("z_down_m"), bool)
                and math.isfinite(float(record["z_down_m"]))
                and float(record["z_down_m"]) <= -MINIMUM_RELATIVE_ALT_M
            )

        def require_continuous_state(
            name: str, uav: int, interval_start_ns: int, interval_end_ns: int
        ) -> None:
            freshness_ns = int(state_specs[name][2])
            history = histories[name][uav]
            baseline_state = _latest_at_or_before(history, interval_start_ns)
            if (
                baseline_state is None
                or interval_start_ns
                - int(baseline_state["received_monotonic_ns"])
                > freshness_ns
                or not state_valid(name, baseline_state)
            ):
                raise M4ValidationError(
                    f"uav{uav} {name} does not cover airborne interval start"
                )
            coverage = [
                baseline_state,
                *[
                    record
                    for record in history
                    if interval_start_ns
                    < int(record["received_monotonic_ns"])
                    < interval_end_ns
                ],
            ]
            times = [int(record["received_monotonic_ns"]) for record in coverage]
            if (
                any(
                    right - left > freshness_ns
                    for left, right in zip(times, times[1:])
                )
                or interval_end_ns - times[-1] > freshness_ns
                or any(not state_valid(name, record) for record in coverage)
            ):
                raise M4ValidationError(
                    f"uav{uav} {name} has a stale/invalid airborne interval gap"
                )

        for name in state_specs:
            for uav in EXPECTED_UAVS:
                require_continuous_state(name, uav, start_ns, end_ns)

        prearm_boundaries = event_groups.get("flight_prearm_boundary", [])
        first_arm_sent_ns = min(
            int(item["sent_monotonic_ns"])
            for item in offers_by_transaction.values()
            if item["flight_stage"] == "arm"
        )
        if (
            len(prearm_boundaries) != 1
            or prearm_boundaries[0].get("guarded_uavs")
            != [f"uav{uav}" for uav in EXPECTED_UAVS]
            or isinstance(prearm_boundaries[0].get("checked_monotonic_ns"), bool)
            or not isinstance(prearm_boundaries[0].get("checked_monotonic_ns"), int)
            or not prearm_boundaries[0]["checked_monotonic_ns"]
            <= prearm_boundaries[0].get("monotonic_ns", -1)
            <= first_arm_sent_ns
        ):
            raise M4ValidationError("exact pre-arm flight boundary differs")
        prearm_checked_ns = int(prearm_boundaries[0]["checked_monotonic_ns"])
        for uav in EXPECTED_UAVS:
            arm_sent_ns = int(
                accepted_by_stage_uav[("arm", uav)][0]["sent_monotonic_ns"]
            )
            for boundary_ns in (prearm_checked_ns, arm_sent_ns):
                prearm = {
                    name: _latest_at_or_before(
                        histories[name][uav], boundary_ns
                    )
                    for name in state_specs
                }
                if any(
                    record is None
                    or boundary_ns - int(record["received_monotonic_ns"])
                    > int(state_specs[name][2])
                    for name, record in prearm.items()
                ):
                    raise M4ValidationError(f"uav{uav} lacks fresh pre-arm raw state")
                heartbeat = prearm["heartbeat"]
                extended = prearm["extended"]
                global_position = prearm["global"]
                local_position = prearm["local"]
                assert heartbeat and extended and global_position and local_position
                if (
                    int(heartbeat["base_mode"]) & MAV_MODE_FLAG_SAFETY_ARMED != 0
                    or heartbeat.get("custom_mode") != COPTER_MODE_GUIDED
                    or extended.get("landed_state") != MAV_LANDED_STATE_ON_GROUND
                    or abs(int(global_position["relative_alt_mm"]))
                    > int(
                        expected_gate["vehicle_state_requirements"][
                            "prearm_relative_alt_abs_max_mm"
                        ]
                    )
                    or abs(float(local_position["z_down_m"]))
                    > float(
                        expected_gate["vehicle_state_requirements"][
                            "prearm_local_z_abs_max_m"
                        ]
                    )
                ):
                    raise M4ValidationError(
                        f"uav{uav} pre-arm GUIDED/ground transition differs"
                    )

        runtime_records = load_runtime_events(
            run_dir / "logs/m4_runtime_events.jsonl",
            run_id=str(run.get("run_id")),
            runtime_id=str(run.get("runtime_id")),
        )
        motion = validate_measurement_motion(
            runtime_records,
            start_ns=start_ns,
            end_ns=end_ns,
            declared_requirements=expected_gate["motion_requirements"],
        )
        frozen_bundle = strict_json(FROZEN_BUNDLE_PATH)
        frame_alignment = validate_runtime_frame_correspondence(
            runtime_records,
            local_position_histories=histories["local"],
            global_position_histories=histories["global"],
            prearm_monotonic_ns=prearm_checked_ns,
            measurement_start_ns=start_ns,
            measurement_end_ns=end_ns,
            declared_frame_contract=frozen_bundle.get("frame_contract", {}),
        )

        for boundary_name, boundary_ns in (
            ("airborne_ready", ready_deadline_ns),
            (
                "pre_land",
                min(
                    int(item["sent_monotonic_ns"])
                    for item in offers_by_transaction.values()
                    if item["flight_stage"] == "land"
                ),
            ),
        ):
            for name, (_event, _message_id, freshness_ns) in state_specs.items():
                for uav in EXPECTED_UAVS:
                    record = _latest_at_or_before(histories[name][uav], boundary_ns)
                    if (
                        record is None
                        or boundary_ns - int(record["received_monotonic_ns"])
                        > freshness_ns
                        or not state_valid(name, record)
                    ):
                        raise M4ValidationError(
                            f"uav{uav} lacks fresh {name} at {boundary_name} boundary"
                        )

        poses = strict_jsonl(
            run_dir / "logs/m4_pose_snapshots.jsonl", max_line_bytes=262_144
        )
        pose_times = [record.get("host_monotonic_ns") for record in poses]
        if (
            not poses
            or any(isinstance(value, bool) or not isinstance(value, int) for value in pose_times)
            or pose_times != sorted(pose_times)
            or len(pose_times) != len(set(pose_times))
        ):
            raise M4ValidationError("Gazebo pose history is absent/nonmonotonic")
        arm_sent_ns = min(
            int(item["sent_monotonic_ns"])
            for item in offers_by_transaction.values()
            if item["flight_stage"] == "arm"
        )
        baseline = _latest_pose_at_or_before(poses, arm_sent_ns - 1)
        if baseline is None or arm_sent_ns - int(baseline["host_monotonic_ns"]) > POSE_FRESHNESS_NS:
            raise M4ValidationError("fresh immutable pre-arm Gazebo baseline is absent")

        def uav_positions(snapshot: Mapping[str, Any]) -> dict[int, tuple[float, float, float]]:
            nodes = snapshot.get("nodes")
            if not isinstance(nodes, list):
                raise M4ValidationError("Gazebo snapshot nodes differ")
            by_id = {
                str(node.get("node_id")): node
                for node in nodes
                if isinstance(node, dict) and str(node.get("node_id", "")).startswith("uav")
            }
            if set(by_id) != {f"uav{uav}" for uav in EXPECTED_UAVS}:
                raise M4ValidationError("Gazebo snapshot five-UAV set differs")
            result: dict[int, tuple[float, float, float]] = {}
            for uav in EXPECTED_UAVS:
                node = by_id[f"uav{uav}"]
                age = node.get("freshness_age_ns")
                if (
                    node.get("source_topic") != f"/uav{uav}/odometry"
                    or node.get("source_header_frame") != "odom"
                    or node.get("source_child_frame") != "base_link"
                    or node.get("source_frame") != "world"
                    or node.get("transform_version") != "enu-identity-v1"
                    or node.get("stale") is not False
                    or isinstance(age, bool)
                    or not isinstance(age, int)
                    or not 0 <= age <= POSE_FRESHNESS_NS
                ):
                    raise M4ValidationError(f"uav{uav} Gazebo odometry lineage/freshness differs")
                result[uav] = finite_vector3(node.get("position_m"))
            return result

        baseline_positions = uav_positions(baseline)
        declared_ground = expected_gate["gazebo_ground_z_m"]
        if any(
            abs(baseline_positions[uav][2] - float(declared_ground[f"uav{uav}"])) > 3.0
            for uav in EXPECTED_UAVS
        ):
            raise M4ValidationError("observed pre-arm Gazebo baseline differs from scenario")

        measurement_pose_start = _latest_pose_at_or_before(poses, start_ns)
        if (
            measurement_pose_start is None
            or start_ns - int(measurement_pose_start["host_monotonic_ns"])
            > POSE_FRESHNESS_NS
        ):
            raise M4ValidationError("Gazebo pose does not cover measurement start")
        measurement_poses = [
            measurement_pose_start,
            *[
                snapshot
                for snapshot in poses
                if start_ns < int(snapshot["host_monotonic_ns"]) < end_ns
            ],
        ]
        measurement_pose_times = [
            int(snapshot["host_monotonic_ns"]) for snapshot in measurement_poses
        ]
        if (
            any(
                right - left > POSE_FRESHNESS_NS
                for left, right in zip(
                    measurement_pose_times, measurement_pose_times[1:]
                )
            )
            or end_ns - measurement_pose_times[-1] > POSE_FRESHNESS_NS
        ):
            raise M4ValidationError("Gazebo snapshot interval has a freshness gap")

        minimum_rise = math.inf
        minimum_separation = math.inf

        def validate_airborne_positions(snapshot: Mapping[str, Any]) -> None:
            nonlocal minimum_rise, minimum_separation
            positions = uav_positions(snapshot)
            for uav in EXPECTED_UAVS:
                rise = positions[uav][2] - baseline_positions[uav][2]
                minimum_rise = min(minimum_rise, rise)
                if rise < MINIMUM_GAZEBO_RISE_M:
                    raise M4ValidationError(
                        f"uav{uav} Gazebo altitude lacks vertical takeoff"
                    )
            for left in EXPECTED_UAVS:
                for right in EXPECTED_UAVS:
                    if right <= left:
                        continue
                    separation = math.dist(positions[left], positions[right])
                    minimum_separation = min(minimum_separation, separation)
                    if separation < MINIMUM_SEPARATION_M:
                        raise M4ValidationError(
                            f"uav{left}/uav{right} safe separation violated"
                        )

        node_observation_times: dict[int, list[int]] = {
            uav: [] for uav in EXPECTED_UAVS
        }
        for snapshot in measurement_poses:
            validate_airborne_positions(snapshot)
            nodes = {
                str(node.get("node_id")): node
                for node in snapshot["nodes"]
                if isinstance(node, dict)
            }
            for uav in EXPECTED_UAVS:
                node = nodes[f"uav{uav}"]
                node_observation_times[uav].append(
                    int(snapshot["host_monotonic_ns"])
                    - int(node["freshness_age_ns"])
                )
        for uav, observations in node_observation_times.items():
            if (
                not observations
                or observations[0] > start_ns
                or start_ns - observations[0] > POSE_FRESHNESS_NS
                or any(
                    right < left or right - left > POSE_FRESHNESS_NS
                    for left, right in zip(observations, observations[1:])
                )
                or end_ns - observations[-1] > POSE_FRESHNESS_NS
            ):
                raise M4ValidationError(
                    f"uav{uav} Gazebo odometry source has a continuous freshness gap"
                )

        ready_pose = _latest_pose_at_or_before(poses, ready_deadline_ns)
        if (
            ready_pose is None
            or ready_deadline_ns - int(ready_pose["host_monotonic_ns"])
            > POSE_FRESHNESS_NS
        ):
            raise M4ValidationError("Gazebo pose lacks airborne-ready boundary coverage")
        validate_airborne_positions(ready_pose)

        grid = [start_ns + index * 1_000_000_000 for index in range(600)]
        for timestamp_ns in grid:
            for name, (_event_name, _message_id, freshness_ns) in state_specs.items():
                for uav in EXPECTED_UAVS:
                    latest = _latest_at_or_before(histories[name][uav], timestamp_ns)
                    if (
                        latest is None
                        or timestamp_ns - latest["received_monotonic_ns"] < 0
                        or timestamp_ns - latest["received_monotonic_ns"] > freshness_ns
                        or not state_valid(name, latest)
                    ):
                        raise M4ValidationError(
                            f"simultaneous airborne grid lacks fresh {name}: {timestamp_ns}/uav{uav}"
                        )
            snapshot = _latest_pose_at_or_before(poses, timestamp_ns)
            if (
                snapshot is None
                or timestamp_ns - snapshot["host_monotonic_ns"] < 0
                or timestamp_ns - snapshot["host_monotonic_ns"] > POSE_FRESHNESS_NS
            ):
                raise M4ValidationError("simultaneous airborne grid lacks fresh Gazebo pose")
            validate_airborne_positions(snapshot)

        # Landing is proven from new post-LAND vehicle state and a final fresh
        # odometry snapshot returning close to the observed (not asserted)
        # pre-arm baseline.
        land_sent_by_uav = {
            uav: int(accepted_by_stage_uav[("land", uav)][0]["sent_monotonic_ns"])
            for uav in EXPECTED_UAVS
        }
        disarm_sent_by_uav = {
            uav: int(accepted_by_stage_uav[("disarm", uav)][0]["sent_monotonic_ns"])
            for uav in EXPECTED_UAVS
        }
        last_required_post_state_ns = 0
        for uav in EXPECTED_UAVS:
            landed_records = [
                record
                for record in histories["extended"][uav]
                if land_sent_by_uav[uav]
                <= record["received_monotonic_ns"]
                < landing_deadline_ns
                and record.get("landed_state") == MAV_LANDED_STATE_ON_GROUND
            ]
            if not landed_records:
                raise M4ValidationError(f"uav{uav} lacks post-LAND on-ground state")
            if disarm_sent_by_uav[uav] < int(
                landed_records[0]["received_monotonic_ns"]
            ):
                raise M4ValidationError(
                    f"uav{uav} DISARM precedes raw on-ground state"
                )
            disarmed_records = [
                record
                for record in histories["heartbeat"][uav]
                if disarm_sent_by_uav[uav]
                <= record["received_monotonic_ns"]
                < disarm_deadline_ns
                and isinstance(record.get("base_mode"), int)
                and record["base_mode"] & MAV_MODE_FLAG_SAFETY_ARMED == 0
            ]
            if not disarmed_records:
                raise M4ValidationError(f"uav{uav} lacks post-LAND disarmed heartbeat")
            last_required_post_state_ns = max(
                last_required_post_state_ns,
                int(landed_records[0]["received_monotonic_ns"]),
                int(disarmed_records[0]["received_monotonic_ns"]),
            )
        shutdown = event_groups["flight_plan_shutdown"][0]
        shutdown_ns = shutdown.get("monotonic_ns")
        final_pose = (
            _latest_pose_at_or_before(poses, shutdown_ns)
            if isinstance(shutdown_ns, int)
            else None
        )
        if (
            final_pose is None
            or shutdown.get("post_control_boundary_monotonic_ns")
            != post_control_boundary_ns
            or shutdown.get("landing_deadline_monotonic_ns")
            != landing_deadline_ns
            or shutdown.get("disarm_deadline_monotonic_ns")
            != disarm_deadline_ns
            or not post_control_boundary_ns < shutdown_ns < disarm_deadline_ns
            or shutdown_ns < last_required_post_state_ns
            or shutdown_ns
            < max(
                terminal_time_ns(transaction_id)
                for transaction_id, offer in offers_by_transaction.items()
                if offer["flight_stage"] == "disarm"
            )
            or shutdown_ns - final_pose["host_monotonic_ns"] > POSE_FRESHNESS_NS
        ):
            raise M4ValidationError("clean landing lacks fresh final Gazebo pose")
        final_positions = uav_positions(final_pose)
        if any(
            abs(final_positions[uav][2] - baseline_positions[uav][2]) > 3.0
            for uav in EXPECTED_UAVS
        ):
            raise M4ValidationError("final Gazebo pose did not return to observed baseline")

        details = {
            "contract": AIRBORNE_GATE_CONTRACT,
            "uav_count": 5,
            "simultaneous_airborne_grid_count": len(grid),
            "measurement_duration_ns": end_ns - start_ns,
            "minimum_observed_gazebo_rise_m": round(minimum_rise, 6),
            "minimum_observed_pair_separation_m": round(minimum_separation, 6),
            "pre_arm_baseline_monotonic_ns": baseline["host_monotonic_ns"],
            "accepted_flight_command_count": len(accepted),
            "flight_command_attempt_count": len(offers_by_transaction),
            "flight_command_retry_count": len(offers_by_transaction) - len(expected_keys),
            "clean_landing_shutdown_monotonic_ns": shutdown_ns,
            "network_lineage": network_lineage,
            "frame_alignment": frame_alignment,
            "motion": motion,
        }
    except (
        KeyError,
        OSError,
        TypeError,
        ValueError,
        struct.error,
        M4ValidationError,
    ) as exc:
        failures.append(f"five-UAV airborne gate cannot be proven: {exc}")
    return details, failures


def _empty_result(failure: str) -> dict[str, Any]:
    return {
        "contract": RESULT_CONTRACT,
        "run_id": "unavailable",
        "runtime_id": "unavailable",
        "profile": "m4_capacity_prerequisite",
        "passed": False,
        "gates": {"run_identity": gate([failure])},
        "metrics": {},
        "failures": [f"run_identity: {failure}"],
    }


def _validate_identity(
    run_dir: Path,
    run: dict[str, Any],
    *,
    expected_profile: str = "m4_capacity_prerequisite",
    required_source_paths: set[str] | None = None,
    required_component_profiles: set[str] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    failures: list[str] = []
    details: dict[str, Any] = {}
    identity = run.get("identity")
    expected_keys = {
        "source_commit",
        "git_dirty",
        "container_image_digest",
        "nvidia_driver_capabilities",
        "mitsuba_variant",
        "competing_load_policy",
        "provenance_sha256",
        "dependency_lock_sha256",
        "ownership_map_sha256",
        "qualification_vector_sha256",
        "prerequisite_manifest_sha256",
        "runtime_asset_manifest_sha256",
        "executable_manifest",
    }
    failures.extend(exact_keys(identity, expected_keys, "capacity identity"))
    if not isinstance(identity, dict):
        return details, failures
    source_commit = identity.get("source_commit")
    if not isinstance(source_commit, str) or not HEX40.fullmatch(source_commit):
        failures.append("source_commit is not exact 40-hex")
    if identity.get("git_dirty") is not False:
        failures.append("capacity source was not a clean immutable checkout")
    if not isinstance(identity.get("container_image_digest"), str) or not IMAGE.fullmatch(
        identity["container_image_digest"]
    ):
        failures.append("container image digest is not immutable")
    if identity.get("nvidia_driver_capabilities") != "compute,utility,graphics":
        failures.append("M4 NVIDIA driver capability set is not exact")
    if identity.get("mitsuba_variant") != "cuda_ad_mono_polarized":
        failures.append("Mitsuba variant differs from the qualified CUDA variant")
    if identity.get("competing_load_policy") != "exclusive_simulation_and_gpu":
        failures.append("competing-load policy differs")

    try:
        provenance_path = run_dir / "metrics/provenance.json"
        provenance = strict_json(provenance_path)
        consumption = provenance.get("qualification_consumption")
        vector = provenance.get("qualification_content_vector")
        vector_payload = json.dumps(vector, sort_keys=True, separators=(",", ":")).encode()
        if (
            sha256_file(provenance_path) != identity.get("provenance_sha256")
            or provenance.get("run_id") != run.get("run_id")
            or provenance.get("acceptance_eligible") is not True
            or provenance.get("acceptance_blockers") != []
            or provenance.get("git_dirty") is not False
            or provenance.get("git_status") != []
            or not isinstance(consumption, dict)
            or consumption.get("profile") != expected_profile
            or consumption.get("consumed_nodes") != ["Q0", "Q1", "Q2", "Q3", "Q4"]
            or not isinstance(vector, dict)
            or vector.get("git_commit") != source_commit
            or hashlib.sha256(vector_payload).hexdigest()
            != identity.get("qualification_vector_sha256")
        ):
            failures.append("provenance/Q-vector/source binding is not exact")
        details["source_commit"] = source_commit
        details["qualification_vector_sha256"] = identity.get(
            "qualification_vector_sha256"
        )
    except (OSError, TypeError, ValueError, M4ValidationError) as exc:
        failures.append(f"provenance cannot be independently bound: {exc}")

    for relative, field in (
        ("network/config/dependency_lock.yaml", "dependency_lock_sha256"),
        ("network/config/qualification_path_ownership.json", "ownership_map_sha256"),
    ):
        try:
            if sha256_file(ROOT / relative) != identity.get(field):
                failures.append(f"identity hash differs: {relative}")
        except M4ValidationError as exc:
            failures.append(str(exc))

    expected_sources = (
        REQUIRED_SOURCE_PATHS
        if required_source_paths is None
        else required_source_paths
    )
    expected_components = (
        set()
        if required_component_profiles is None
        else required_component_profiles
    )
    sources = run.get("source_sha256")
    if not isinstance(sources, dict) or set(sources) != expected_sources:
        failures.append("source hash map does not exactly cover the M4 implementation")
    else:
        for relative, expected in sources.items():
            try:
                if not isinstance(expected, str) or not HEX64.fullmatch(expected):
                    raise M4ValidationError("hash is not 64-hex")
                if sha256_file(ROOT / relative) != expected:
                    failures.append(f"source identity mismatch: {relative}")
            except M4ValidationError as exc:
                failures.append(f"source identity invalid {relative}: {exc}")

    try:
        prerequisite_manifest_path = run_dir / "raw/prerequisites.json"
        prerequisite_manifest = strict_json(prerequisite_manifest_path)
        if (
            sha256_file(prerequisite_manifest_path)
            != identity.get("prerequisite_manifest_sha256")
            or prerequisite_manifest.get("profile") != expected_profile
            or prerequisite_manifest.get("source_commit") != source_commit
            or set(prerequisite_manifest.get("receipts", {})) != {"m0", "m1", "m2", "m3"}
            or set(prerequisite_manifest.get("component_receipts", {}))
            != expected_components
        ):
            failures.append("component prerequisite manifest/profile graph is not exact")
        for name, record in prerequisite_manifest.get("receipts", {}).items():
            path = run_dir / f"raw/prerequisites/{name}.json"
            receipt = strict_json(path)
            if (
                sha256_file(path) != record.get("sha256")
                or receipt.get("formal_accepted") is not True
                or receipt.get("passed") is not True
                or not isinstance(receipt.get("source_commit"), str)
                or HEX40.fullmatch(receipt["source_commit"]) is None
                or receipt.get("contract") != record.get("contract")
                or receipt.get("run_id") != record.get("run_id")
            ):
                failures.append(f"copied prerequisite receipt differs: {name}")
        for name, record in prerequisite_manifest.get(
            "component_receipts", {}
        ).items():
            path = run_dir / f"raw/prerequisites/{name}.json"
            receipt = strict_json(path)
            if (
                sha256_file(path) != record.get("sha256")
                or receipt.get("formal_accepted") is not True
                or receipt.get("passed") is not True
                or receipt.get("failures") != []
                or receipt.get("profile") != name
                or receipt.get("source_commit") != source_commit
                or receipt.get("contract") != record.get("contract")
                or receipt.get("run_id") != record.get("run_id")
            ):
                failures.append(f"copied required component receipt differs: {name}")
    except (OSError, TypeError, ValueError, M4ValidationError) as exc:
        failures.append(f"prerequisite receipt binding failed: {exc}")

    try:
        binding = run.get("runtime_assets")
        failures.extend(
            exact_keys(binding, {"path", "sha256"}, "runtime asset binding")
        )
        manifest_path = run_dir / "raw/runtime_asset_manifest.json"
        manifest = strict_json(manifest_path)
        if (
            not isinstance(binding, dict)
            or binding.get("path") != "raw/runtime_asset_manifest.json"
            or binding.get("sha256") != sha256_file(manifest_path)
            or identity.get("runtime_asset_manifest_sha256")
            != sha256_file(manifest_path)
            or set(manifest)
            != {
                "schema",
                "installed_package_share",
                "package",
                "robot_model",
                "world",
                "asset_count",
                "assets_sha256",
                "assets",
            }
            or manifest.get("schema") != "ams.m4.runtime_asset_manifest/v1"
            or manifest.get("package") != "multiagent_simulation"
            or manifest.get("robot_model") != "iris_radio_headless"
            or manifest.get("world") != "m4_canonical/m4_canonical.sdf"
        ):
            raise M4ValidationError("runtime asset manifest envelope differs")
        share = Path(str(manifest["installed_package_share"])).resolve(strict=True)
        expected_share = (
            run_dir
            / "runtime_overlay/install/multiagent_simulation/share/multiagent_simulation"
        ).resolve(strict=True)
        if share != expected_share:
            raise M4ValidationError("runtime package share is not the fresh run overlay")
        assets = manifest.get("assets")
        if (
            not isinstance(assets, list)
            or manifest.get("asset_count") != len(assets)
            or hashlib.sha256(canonical_json(assets)).hexdigest()
            != manifest.get("assets_sha256")
        ):
            raise M4ValidationError("runtime asset list/hash differs")
        roles: dict[str, int] = defaultdict(int)
        paths: set[str] = set()
        for record in assets:
            if not isinstance(record, dict) or set(record) != {
                "path",
                "role",
                "sha256",
                "size_bytes",
            }:
                raise M4ValidationError("runtime asset record shape differs")
            relative = str(record["path"])
            if relative in paths or relative.startswith("/") or ".." in Path(relative).parts:
                raise M4ValidationError("runtime asset path is duplicate/unsafe")
            paths.add(relative)
            target = (share / relative).resolve(strict=True)
            if (
                not target.is_relative_to(share)
                or sha256_file(target) != record.get("sha256")
                or target.stat().st_size != record.get("size_bytes")
            ):
                raise M4ValidationError(f"runtime asset bytes differ: {relative}")
            roles[str(record["role"])] += 1
        if (
            roles.get("launch") != 1
            or roles.get("bridge_template") != 1
            or roles.get("robot_model", 0) < 2
            or roles.get("canonical_world", 0) < 5
            or set(roles)
            != {"launch", "bridge_template", "robot_model", "canonical_world"}
        ):
            raise M4ValidationError("runtime asset transitive role coverage differs")
        details["runtime_asset_count"] = len(assets)
        details["runtime_asset_roles"] = dict(sorted(roles.items()))
    except (OSError, TypeError, ValueError, M4ValidationError) as exc:
        failures.append(f"runtime assets cannot be independently bound: {exc}")

    manifest = identity.get("executable_manifest")
    required_executables = {"ns3_packet_engine", "python", "sionna_rt"}
    if not isinstance(manifest, dict) or set(manifest) != required_executables:
        failures.append("executable manifest does not exactly cover ns3/Python/Sionna")
    else:
        for name, record in manifest.items():
            try:
                if not isinstance(record, dict) or set(record) != {"path", "sha256", "size_bytes"}:
                    raise M4ValidationError("executable record shape differs")
                path = Path(record["path"])
                if (
                    not path.is_absolute()
                    or sha256_file(path) != record.get("sha256")
                    or path.stat().st_size != record.get("size_bytes")
                ):
                    raise M4ValidationError("executable bytes/size differ")
            except (OSError, KeyError, TypeError, M4ValidationError) as exc:
                failures.append(f"{name} executable identity invalid: {exc}")
    return details, failures


def validate(run_dir: Path) -> dict[str, Any]:
    gate_failures: dict[str, list[str]] = defaultdict(list)
    details: dict[str, dict[str, Any]] = defaultdict(dict)
    try:
        run = strict_json(run_dir / "raw/m4_capacity_contract.json")
    except M4ValidationError as exc:
        return _empty_result(str(exc))
    run_id = run.get("run_id")
    runtime_id = run.get("runtime_id")
    gate_failures["run_identity"].extend(
        exact_keys(
            run,
            {
                "schema_version",
                "contract",
                "run_id",
                "runtime_id",
                "run_nonce",
                "profile",
                "created_monotonic_ns",
                "provider_mode",
                "acceptance_eligible",
                "uav_count",
                "expected_cells",
                "bundle",
                "async_policy",
                "schedule",
                "execution_budget",
                "airborne_gate",
                "workload",
                "endpoint_path",
                "clock_producers",
                "limits",
                "runtime_assets",
                "identity",
                "source_sha256",
            },
            "M4 capacity contract",
        )
    )
    for key, expected in {
        "schema_version": 3,
        "contract": RUN_CONTRACT,
        "profile": "m4_capacity_prerequisite",
        "provider_mode": "real_sionna",
        "acceptance_eligible": True,
        "uav_count": 5,
        "expected_cells": 30,
    }.items():
        if run.get(key) != expected:
            gate_failures["run_identity"].append(
                f"{key} must equal {expected!r}, observed {run.get(key)!r}"
            )
    if not isinstance(run_id, str) or not run_id or len(run_id) > 128:
        gate_failures["run_identity"].append("run_id is invalid")
    if not isinstance(runtime_id, str) or not runtime_id or len(runtime_id) > 128:
        gate_failures["run_identity"].append("runtime_id is invalid")
    run_nonce = run.get("run_nonce")
    if not isinstance(run_nonce, str) or HEX64.fullmatch(run_nonce) is None:
        gate_failures["run_identity"].append("run_nonce is not exact 64-hex")
    created = run.get("created_monotonic_ns")
    if isinstance(created, bool) or not isinstance(created, int) or created <= 0:
        gate_failures["run_identity"].append("created_monotonic_ns is invalid")

    bundle = run.get("bundle")
    gate_failures["run_identity"].extend(
        exact_keys(bundle, {"path", "bundle_id", "bundle_sha256"}, "bundle binding")
    )
    scene_result, scene_failures = validate_scene_prerequisite(
        bundle.get("bundle_id") if isinstance(bundle, dict) else None,
        bundle.get("bundle_sha256") if isinstance(bundle, dict) else None,
    )
    if not isinstance(bundle, dict) or bundle.get("path") != (
        "network/config/m4_canonical_scene_bundle.json"
    ):
        scene_failures.append("run contract bundle path differs")
    gate_failures["canonical_scene"].extend(scene_failures)
    details["canonical_scene"] = {
        "bundle_id": scene_result.get("bundle_id"),
        "bundle_sha256": scene_result.get("bundle_sha256"),
        "independent_gate_count": len(scene_result.get("gates", {})),
    }

    policy = run.get("async_policy")
    expected_policy = {
        "query_period_ns": QUERY_PERIOD_NS,
        "validity_ttl_ns": VALIDITY_TTL_NS,
        "query_deadline_ns": QUERY_DEADLINE_NS,
        "max_pose_age_ns": MAX_POSE_AGE_NS,
        "late_policy": "fail_closed_directed_link",
        "hold_last_beyond_expiry": False,
    }
    if policy != expected_policy:
        gate_failures["run_identity"].append("async policy differs from frozen 1s/2s/100ms/1.5s P0")
    if run.get("clock_producers") != list(REQUIRED_CLOCK_PRODUCERS):
        gate_failures["time_coherence"].append("predeclared clock producer set differs")
    limits = run.get("limits")
    expected_limits = {
        "request_queue_capacity": 64,
        "completion_queue_capacity": 64,
        "max_poll_batch": 64,
        "max_message_bytes": 1_048_576,
        "max_state_line_bytes": 65_536,
        "max_packet_event_line_bytes": 65_536,
        "max_fault_captured_results": 64,
        "max_fault_pending_per_cell": 2,
        "max_fault_release_queue": 8,
    }
    if limits != expected_limits:
        gate_failures["bounded_nonblocking"].append("bounded async/IPC limits differ")

    workload = run.get("workload")
    expected_workload_keys = {
        "matrix_path",
        "matrix_sha256",
        "cell_count",
        "endpoint_phase",
        "transport_nonce_derivation",
        "accepted_m3_receipt_path",
        "accepted_m3_receipt_sha256",
    }
    gate_failures["capacity_vector"].extend(
        exact_keys(workload, expected_workload_keys, "capacity workload")
    )
    if isinstance(workload, dict):
        try:
            m3_path = run_dir / "raw/prerequisites/m3.json"
            if (
                workload.get("matrix_path") != "network/config/endpoint_matrix_5uav.json"
                or workload.get("matrix_sha256")
                != sha256_file(ROOT / "network/config/endpoint_matrix_5uav.json")
                or workload.get("cell_count") != 30
                or workload.get("endpoint_phase") != "positive"
                or workload.get("transport_nonce_derivation")
                != "sha256-run-nonce-prefix16-v1"
                or workload.get("accepted_m3_receipt_path") != "raw/prerequisites/m3.json"
                or workload.get("accepted_m3_receipt_sha256") != sha256_file(m3_path)
            ):
                gate_failures["capacity_vector"].append("workload/M3 receipt binding differs")
        except M4ValidationError as exc:
            gate_failures["capacity_vector"].append(str(exc))

    endpoint_details, endpoint_failures = _validate_actual_endpoint_path(run_dir, run)
    gate_failures["actual_m3_sitl_path"].extend(endpoint_failures)
    details["actual_m3_sitl_path"] = endpoint_details
    airborne_details, airborne_failures = _validate_m4_airborne_gate(run_dir, run)
    gate_failures["actual_five_uav_airborne"].extend(airborne_failures)
    details["actual_five_uav_airborne"] = airborne_details

    budget_details, budget_failures = validate_capacity_execution_budget(
        run_dir, run
    )
    gate_failures["execution_budget"].extend(budget_failures)
    details["execution_budget"] = budget_details

    identity_details, identity_failures = _validate_identity(run_dir, run)
    gate_failures["run_identity"].extend(identity_failures)
    details["run_identity"].update(identity_details)

    events: list[dict[str, Any]] = []
    try:
        events = load_runtime_events(
            run_dir / "logs/m4_runtime_events.jsonl",
            run_id=str(run_id),
            runtime_id=str(runtime_id),
        )
        runtime, runtime_failures = validate_capacity_runtime(
            events, schedule=run.get("schedule") if isinstance(run.get("schedule"), dict) else {}
        )
    except M4ValidationError as exc:
        runtime, runtime_failures = {}, [str(exc)]
    gate_failures["schedule_rtf_resources"].extend(runtime_failures)
    details["schedule_rtf_resources"] = runtime
    start_ns = int(runtime.get("measurement_start_monotonic_ns", 0))
    end_ns = int(runtime.get("measurement_end_monotonic_ns", 0))

    clocks, clock_failures = validate_clock_correlations(
        run_dir / "logs/m4_clock_correlations.jsonl",
        run_id=str(run_id),
        runtime_id=str(runtime_id),
        start_ns=start_ns,
        end_ns=end_ns,
    )
    gate_failures["time_coherence"].extend(clock_failures)
    details["time_coherence"] = clocks
    clock_process, clock_process_failures = validate_clock_process_binding(
        events if isinstance(events, list) else [], clocks
    )
    gate_failures["time_coherence"].extend(clock_process_failures)
    details["time_coherence"].update(clock_process)

    wire, wire_failures = validate_wire_log(run_dir / "logs")
    gate_failures["real_provider_wire"].extend(wire_failures)
    provider_binding, provider_binding_failures = _validate_real_provider_wire_binding(
        run_dir, run, wire
    )
    gate_failures["real_provider_wire"].extend(provider_binding_failures)
    details["real_provider_wire"] = {
        "client": {
            key: value
            for key, value in wire.items()
            if key not in {"messages", "message_by_hash"}
        },
        "provider": provider_binding.get("stream_scans", {}).get("provider", {}),
        "binding": provider_binding,
    }
    states, state_failures = validate_states(
        run_dir / "logs/sionna_applied_states.jsonl", wire
    )
    gate_failures["applied_state_lineage"].extend(state_failures)
    details["applied_state_lineage"] = {
        key: value for key, value in states.items() if key not in {"records", "by_hash", "latest"}
    }
    poses, pose_failures = validate_pose_snapshots(
        run_dir / "logs/m4_pose_snapshots.jsonl",
        wire,
        start_monotonic_ns=start_ns,
        end_monotonic_ns=end_ns,
    )
    gate_failures["current_pose_lineage"].extend(pose_failures)
    try:
        pose_records = strict_jsonl(
            run_dir / "logs/m4_pose_snapshots.jsonl", max_line_bytes=262_144
        )
        binding_details, binding_failures = validate_query_pose_runtime_binding(
            pose_records,
            wire,
            events,
            run_dir / "logs/m4_pose_observations.jsonl.gz",
            run_id=str(run_id),
            runtime_id=str(runtime_id),
            start_monotonic_ns=start_ns,
            end_monotonic_ns=end_ns,
        )
        gate_failures["current_pose_lineage"].extend(binding_failures)
        poses["runtime_binding"] = binding_details
    except M4ValidationError as exc:
        gate_failures["current_pose_lineage"].append(str(exc))
    details["current_pose_lineage"] = poses
    adapter, adapter_failures = validate_adapter_audit(
        run_dir / "logs/sionna_packet_adapter.jsonl",
        start_monotonic_ns=start_ns,
        end_monotonic_ns=end_ns,
    )
    gate_failures["bounded_nonblocking"].extend(adapter_failures)
    details["bounded_nonblocking"].update(
        {key: value for key, value in adapter.items() if key != "records"}
    )
    packets, packet_failures = validate_packet_events(
        run_dir / "logs/ns3_packet_events.jsonl",
        states,
        start_monotonic_ns=start_ns,
        end_monotonic_ns=end_ns,
    )
    gate_failures["packet_causality"].extend(packet_failures)
    details["packet_causality"] = {
        key: value for key, value in packets.items() if key not in {"records", "decisions", "downstream"}
    }

    freshness, freshness_failures = validate_capacity_freshness(
        wire, states, packets, adapter, start_ns=start_ns, end_ns=end_ns
    )
    gate_failures["freshness_expiry"].extend(freshness_failures)
    details["freshness_expiry"] = freshness
    workload_metrics, workload_failures = validate_capacity_workload(
        run_dir, start_ns=start_ns, end_ns=end_ns
    )
    gate_failures["capacity_vector"].extend(workload_failures)
    details["capacity_vector"] = workload_metrics
    captures, capture_failures = validate_external_captures(
        run_dir, start_ns=start_ns, end_ns=end_ns
    )
    gate_failures["external_tap_path"].extend(capture_failures)
    details["external_tap_path"] = captures

    gate_names = (
        "run_identity",
        "canonical_scene",
        "schedule_rtf_resources",
        "execution_budget",
        "time_coherence",
        "real_provider_wire",
        "current_pose_lineage",
        "applied_state_lineage",
        "bounded_nonblocking",
        "capacity_vector",
        "actual_m3_sitl_path",
        "actual_five_uav_airborne",
        "external_tap_path",
        "freshness_expiry",
        "packet_causality",
    )
    gates = {name: gate(gate_failures[name], details.get(name)) for name in gate_names}
    failures = [
        f"{name}: {failure}" for name in gate_names for failure in gate_failures[name]
    ]
    return {
        "contract": RESULT_CONTRACT,
        "run_id": run_id,
        "runtime_id": runtime_id,
        "profile": "m4_capacity_prerequisite",
        "passed": not failures,
        "gates": gates,
        "metrics": {
            "bundle_id": FROZEN_BUNDLE_ID,
            "bundle_sha256": FROZEN_BUNDLE_SHA256,
            "measurement_seconds": 600,
            "simultaneous_airborne_uav_count": airborne_details.get("uav_count"),
            "simultaneous_airborne_grid_count": airborne_details.get(
                "simultaneous_airborne_grid_count"
            ),
            "minimum_observed_gazebo_rise_m": airborne_details.get(
                "minimum_observed_gazebo_rise_m"
            ),
            "clean_landing_shutdown_monotonic_ns": airborne_details.get(
                "clean_landing_shutdown_monotonic_ns"
            ),
            "expected_cells": 30,
            "aggregate_realtime_factor": runtime.get("aggregate_realtime_factor"),
            "passing_rtf_windows": runtime.get("rtf_passing_window_count"),
            "query_count": freshness.get("query_count", 0),
            "late_update_ratio": freshness.get("late_update_ratio"),
            "stale_pose_count": freshness.get("stale_pose_count"),
            "state_age_p95_ns": freshness.get("state_age_p95_ns"),
            "m3_nominal_vector_sha256": workload_metrics.get(
                "m3_nominal_vector_sha256"
            ),
        },
        "failures": failures,
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)
    run_dir = args.run_dir.resolve()
    output = args.output if args.output.is_absolute() else run_dir / args.output
    try:
        result = validate(run_dir)
        payload = canonical_json(result)
        if args.no_write:
            if not regular_file(output):
                raise M4ValidationError(f"producer result is absent/nonregular: {output}")
            if output.read_bytes() != payload:
                raise M4ValidationError("producer result differs from independent derivation")
        else:
            write_new(output, payload)
        sys.stdout.buffer.write(payload)
        return 0 if result["passed"] else 1
    except (M4ValidationError, OSError, ValueError, TypeError) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
