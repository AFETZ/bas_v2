#!/usr/bin/env python3
"""Independently validate the exact 30+600 s real-Sionna M4 prerequisite."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

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
    FROZEN_BUNDLE_SHA256,
    MAX_POSE_AGE_NS,
    QUERY_DEADLINE_NS,
    QUERY_PERIOD_NS,
    REQUIRED_CLOCK_PRODUCERS,
    VALIDITY_TTL_NS,
    load_runtime_events,
    sha256_file,
    validate_capacity_freshness,
    validate_capacity_runtime,
    validate_capacity_workload,
    validate_clock_correlations,
    validate_clock_process_binding,
    validate_external_captures,
    validate_scene_prerequisite,
)


ROOT = Path(__file__).resolve().parents[2]
RUN_CONTRACT = "ams.m4.capacity_run/v2"
RESULT_CONTRACT = "ams.m4-capacity.validation/v1"
DEFAULT_OUTPUT = Path("metrics/m4_capacity_validation.json")
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
IMAGE = re.compile(r"^sha256:[0-9a-f]{64}$")
ACTUAL_CONTROL_API_CONTRACT = "ams.m3.actual-control-api/v1"
ACTUAL_CONTROL_ENDPOINT_FORM = "actual_sitl_mavproxy_udp_tail"
ACTUAL_CONTROL_EVENT_SCHEMA = "ams.actual-sitl.control-event/v1"
M3_RESULT_CONTRACT = "ams.m3.external-matrix-validation/v1"
M3_RECEIPT_CONTRACT = "ams.m3.host-final-receipt/v1"
REQUIRED_SOURCE_PATHS = {
    "doc/network_radio_integration_plan_v3.md",
    "network/config/component_acceptance_profiles.json",
    "network/config/dependency_lock.yaml",
    "network/config/endpoint_matrix_5uav.json",
    "network/config/jammers_m4_canonical.yaml",
    "network/config/m4_canonical_scene_bundle.json",
    "network/config/qualification_path_ownership.json",
    "network/config/radio_m4_canonical.yaml",
    "network/config/scenario_m4_canonical.yaml",
    "network/config/sionna_async_protocol_v1.json",
    "network/config/sionna_async_schema_v1.json",
    "network/config/sionna_packet_effects_v1.json",
    "network/ns3/run_ns3_tap_packet_engine.sh",
    "network/ns3/scratch/ams-tap-packet-engine.cc",
    "network/ns3/tap_packet_engine_config.py",
    "network/radio_provider/provider.py",
    "network/radio_provider/sionna_async.py",
    "network/radio_provider/sionna_async_service.py",
    "network/radio_provider/sionna_packet_adapter.py",
    "network/scripts/check_m4_canonical_scene_runtime.py",
    "network/scripts/collect_m4_clock_correlations.py",
    "network/scripts/collect_m4_runtime.py",
    "network/scripts/m3_external_matrix_probe.py",
    "network/scripts/m4_adapter_runtime.py",
    "network/bridge/runtime_clock_beacon.py",
    "network/scripts/m4_endpoint_agent.py",
    "network/scripts/m4_runtime_orchestrator.py",
    "network/scripts/run_m4_capacity.sh",
    "network/scripts/validate_m4_capacity.py",
    "network/validation/m4_common.py",
    "network/validation/m4_runtime.py",
    "network/validation/validate_m4_capacity.py",
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
                "run_contract": "ams.m4.causality_run/v1",
                "run_nonce_hex_length": 64,
                "transport_nonce32_derivation": "sha256(raw_full_run_nonce64)[:32]",
            },
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
        samples = strict_jsonl(
            run_dir / "raw/topology_monitor/samples.jsonl",
            max_line_bytes=16 * 1024 * 1024,
        )
        selected = [
            sample
            for sample in samples
            if isinstance(sample.get("monotonic_ns"), int)
            and start_ns <= sample["monotonic_ns"] <= end_ns
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
        for sample in selected:
            if (
                sample.get("run_id") != run.get("run_id")
                or sample.get("runtime_id") != run.get("runtime_id")
                or sample.get("run_nonce") != run.get("run_nonce")
            ):
                raise M4ValidationError("topology sample crosses run identity")
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
        details = {
            "sample_count": len(selected),
            "maximum_sample_gap_ns": max(
                (right - left for left, right in zip(times, times[1:])), default=0
            ),
            "namespace_inodes": {
                key: next(iter(value)) for key, value in sorted(namespace_inodes.items())
            },
        }
    except (KeyError, OSError, TypeError, ValueError, M4ValidationError) as exc:
        failures.append(f"actual-SITL /30 topology cannot be proven: {exc}")
    return details, failures


def _pcap_udp_payload_hashes(path: Path) -> tuple[set[str], int]:
    """Decode Ethernet/IPv4/UDP payload hashes from one bounded classic PCAP."""

    if not regular_file(path) or path.stat().st_size > 512 * 1024 * 1024:
        raise M4ValidationError(f"PCAP is absent/nonregular/oversized: {path}")
    payload = path.read_bytes()
    if len(payload) < 24:
        raise M4ValidationError(f"PCAP header is truncated: {path}")
    magic = payload[:4]
    if magic == b"\xd4\xc3\xb2\xa1":
        byteorder = "little"
    elif magic == b"\xa1\xb2\xc3\xd4":
        byteorder = "big"
    else:
        raise M4ValidationError(f"PCAP magic differs: {path}")
    if int.from_bytes(payload[20:24], byteorder) != 1:
        raise M4ValidationError(f"PCAP linktype is not Ethernet: {path}")
    hashes: set[str] = set()
    packet_count = 0
    offset = 24
    while offset < len(payload):
        if offset + 16 > len(payload):
            raise M4ValidationError(f"PCAP record header is truncated: {path}")
        captured = int.from_bytes(payload[offset + 8 : offset + 12], byteorder)
        offset += 16
        if captured <= 0 or offset + captured > len(payload):
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
        ):
            continue
        udp_offset = ip_offset + ihl
        udp_length = int.from_bytes(frame[udp_offset + 4 : udp_offset + 6], "big")
        if udp_length < 8 or udp_offset + udp_length > ip_offset + total:
            continue
        datagram = frame[udp_offset + 8 : udp_offset + udp_length]
        hashes.add(hashlib.sha256(datagram).hexdigest())
    if packet_count < 1:
        raise M4ValidationError(f"PCAP contains no packets: {path}")
    return hashes, packet_count


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
                    stats.get("contract") != "ams.raw-packet-capture-stats/v1"
                    or stats.get("interface") != interface
                    or stats.get("pcap_path") != pcap_path.name
                    or stats.get("pcap_bytes") != pcap_path.stat().st_size
                    or stats.get("packets_written") != count
                    or stats.get("packets_dropped_kernel") != 0
                    or not isinstance(stats.get("started_monotonic_ns"), int)
                    or not isinstance(stats.get("stopped_monotonic_ns"), int)
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
            start_ns = int(schedule.get("measurement_start_monotonic_ns", 0))
            end_ns = int(schedule.get("measurement_end_monotonic_ns", 0))
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
        "schema_version": 2,
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
    details["real_provider_wire"] = {
        key: value
        for key, value in wire.items()
        if key not in {"messages", "message_by_hash", "raw_by_hash"}
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
    details["current_pose_lineage"] = poses
    adapter, adapter_failures = validate_adapter_audit(
        run_dir / "logs/sionna_packet_adapter.jsonl",
        start_monotonic_ns=start_ns,
        end_monotonic_ns=end_ns,
    )
    gate_failures["bounded_nonblocking"].extend(adapter_failures)
    details["bounded_nonblocking"].update(adapter)
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
        wire, states, packets, start_ns=start_ns, end_ns=end_ns
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
        "time_coherence",
        "real_provider_wire",
        "current_pose_lineage",
        "applied_state_lineage",
        "bounded_nonblocking",
        "capacity_vector",
        "actual_m3_sitl_path",
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
