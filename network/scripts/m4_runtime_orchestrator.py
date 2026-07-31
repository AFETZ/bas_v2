#!/usr/bin/env python3
"""Initialize and supervise immutable M4 capacity/causality runtime pieces."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import secrets
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from network.radio_provider.provider import RuntimeFiles
from network.radio_provider.sionna_async import ProtocolIdentity, load_protocol_limits
from network.radio_provider.sionna_async_service import (
    ExactWireLog,
    ProviderServiceConfig,
    create_production_service,
)
from network.bridge.runtime_clock_beacon import beacon
from network.validation.m4_common import M4ValidationError, strict_json
from network.validation.m4_runtime import (
    FROZEN_BUNDLE_ID,
    FROZEN_BUNDLE_SHA256,
    MAX_POSE_AGE_NS,
    QUERY_DEADLINE_NS,
    QUERY_PERIOD_NS,
    REQUIRED_CLOCK_PRODUCERS,
    VALIDITY_TTL_NS,
    sha256_file,
)
from network.validation.validate_m4_capacity import REQUIRED_SOURCE_PATHS
from network.validation.validate_m4_capacity import _expected_actual_control_api
from network.validation.validate_m4_causality import (
    BACKGROUND_CELL_ID,
    CAUSAL_MEASUREMENT_SPAN_NS,
    CAUSAL_SOURCE_PATHS,
    FINALIZATION_BUDGET_NS,
    NS3_ENGINE_DURATION_NS,
    PRECONTRACT_SETUP_BUDGET_NS,
    REQUIRED_WRAPPER_RESERVE_NS,
    RUNTIME_READINESS_BUDGET_NS,
    WINDOW_IDS,
    WINDOW_SHAPES,
    WRAPPER_TIMEOUT_NS,
    causal_pre_window_gap_ns,
    causal_quiet_drain_map,
    causal_response_policies,
    causal_window_plan,
    matrix_flow_group_identity,
)
from network.scripts.m3_external_matrix_probe import resolve_five_uav_flight_scenario
from network.scripts.m4_capacity_airborne import airborne_gate_contract


CAPACITY_CONTRACT = "ams.m4.capacity_run/v3"
CAUSALITY_CONTRACT = "ams.m4.causality_run/v2"
CAPACITY_EXECUTION_BUDGET_CONTRACT = "ams.m4.capacity-execution-budget/v1"
CAPACITY_READINESS_RUNWAY_NS = 720_000_000_000
CAPACITY_DECLARED_READINESS_WAITS_NS = 415_500_000_000
CAPACITY_BOUNDED_PREFLIGHT_NS = 219_000_000_000
CAPACITY_WARMUP_NS = 30_000_000_000
CAPACITY_BOUNDED_WARMUP_MOTION_NS = 15_000_000_000
CAPACITY_MEASUREMENT_NS = 600_000_000_000
CAPACITY_POST_MEASUREMENT_CONTROL_NS = 10_000_000_000
CAPACITY_LANDING_STATE_NS = 120_000_000_000
CAPACITY_DISARM_NS = 60_000_000_000
CAPACITY_NS3_ENGINE_DURATION_NS = 1_600_000_000_000
CAPACITY_WRAPPER_TIMEOUT_NS = 1_800_000_000_000
SAFE_PRODUCERS = set(REQUIRED_CLOCK_PRODUCERS)
ACTUAL_ENDPOINT_MODE = "actual_m3_sitl_v1"
TECHNICAL_ENDPOINT_MODE = "technical_synthetic_fixture_v1"
ACTUAL_SITL_AUDIT_LOG_PATHS = frozenset(
    {
        "logs/actual_sitl_supervisor.jsonl",
        *(f"logs/actual_sitl_uav{index}.jsonl" for index in range(1, 6)),
    }
)


def canonical(value: Any) -> bytes:
    return (json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def write_exclusive(path: Path, value: Any, mode: int = 0o664) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, mode)
    try:
        payload = value if isinstance(value, bytes) else canonical(value)
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def copy_exclusive(source: Path, destination: Path) -> str:
    if source.is_symlink() or not source.is_file():
        raise M4ValidationError(f"copy source is not a regular file: {source}")
    payload = source.read_bytes()
    write_exclusive(destination, payload)
    return hashlib.sha256(payload).hexdigest()


def identity_for_contract(contract_path: Path) -> tuple[ProtocolIdentity, str, str]:
    contract = strict_json(contract_path)
    contract_hash = sha256_file(contract_path)
    config_material = {
        "async_policy": contract["async_policy"],
        "bundle": contract["bundle"],
        "limits": contract["limits"],
        "profile": contract["profile"],
        "radio_sha256": sha256_file(ROOT / "network/config/radio_m4_canonical.yaml"),
        "effects_sha256": sha256_file(ROOT / "network/config/sionna_packet_effects_v1.json"),
    }
    config_hash = hashlib.sha256(canonical(config_material)).hexdigest()
    return (
        ProtocolIdentity(
            run_id=str(contract["run_id"]),
            profile=str(contract["profile"]),
            contract_hash=contract_hash,
            config_hash=config_hash,
            bundle_id=str(contract["bundle"]["bundle_id"]),
        ),
        contract_hash,
        config_hash,
    )


def _executable_record(path: Path) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    return {
        "path": str(resolved),
        "sha256": sha256_file(resolved),
        "size_bytes": resolved.stat().st_size,
    }


def _runtime_asset_manifest(installed_share: Path) -> dict[str, Any]:
    share = installed_share.resolve(strict=True)
    roots = {
        "launch": share / "launch/multiagent_simulation.launch.py",
        "bridge_template": share / "config/multiagent_lidar_camera_bridge.yaml",
        "robot_model": share / "models/iris_radio_headless",
        "canonical_world": share / "worlds/m4_canonical",
    }
    files: dict[Path, str] = {}
    for role, source in roots.items():
        if source.is_file() and not source.is_symlink():
            files[source] = role
            continue
        if not source.is_dir() or source.is_symlink():
            raise M4ValidationError(f"runtime asset root is absent: {source}")
        for path in sorted(source.rglob("*")):
            if path.is_file() and not path.is_symlink():
                files[path] = role
    records = []
    for path, role in sorted(files.items(), key=lambda item: item[0].as_posix()):
        relative = path.relative_to(share).as_posix()
        records.append(
            {
                "path": relative,
                "role": role,
                "sha256": sha256_file(path),
                "size_bytes": path.stat().st_size,
            }
        )
    if not records:
        raise M4ValidationError("runtime asset manifest is empty")
    content_sha256 = hashlib.sha256(canonical(records)).hexdigest()
    return {
        "schema": "ams.m4.runtime_asset_manifest/v1",
        "installed_package_share": str(share),
        "package": "multiagent_simulation",
        "robot_model": "iris_radio_headless",
        "world": "m4_canonical/m4_canonical.sdf",
        "asset_count": len(records),
        "assets_sha256": content_sha256,
        "assets": records,
    }


def _copy_prerequisites(run_dir: Path) -> tuple[dict[str, Any], dict[str, str]]:
    manifest_source = Path(os.environ.get("AMS_COMPONENT_PREREQUISITES_PATH", "/run/ams/prerequisites.json"))
    manifest = strict_json(manifest_source)
    destination_root = run_dir / "raw/prerequisites"
    hashes: dict[str, str] = {}
    for name in sorted(manifest.get("receipts", {})):
        hashes[name] = copy_exclusive(
            Path(f"/run/ams/prerequisites/{name}.json"), destination_root / f"{name}.json"
        )
    for name in sorted(manifest.get("component_receipts", {})):
        hashes[name] = copy_exclusive(
            Path(f"/run/ams/prerequisites/{name}.json"), destination_root / f"{name}.json"
        )
    copy_exclusive(manifest_source, run_dir / "raw/prerequisites.json")
    return manifest, hashes


def prepare_causality_flight(args: argparse.Namespace) -> int:
    payload, identity = resolve_five_uav_flight_scenario(
        args.flight_scenario.resolve(strict=True)
    )
    write_exclusive(args.output.resolve(), payload)
    write_exclusive(args.identity_output.resolve(), identity)
    return 0


def initialize_capacity(args: argparse.Namespace) -> int:
    run_dir = args.run_dir.resolve()
    if not run_dir.is_dir():
        raise M4ValidationError(f"run directory was not pre-created: {run_dir}")
    # Provenance must be frozen before this contract is created.  Permit only
    # that receipt and its redirected stdout; every other run artifact is
    # created below with O_EXCL semantics.
    existing_files = {
        path.relative_to(run_dir).as_posix()
        for path in run_dir.rglob("*")
        if path.is_file() or path.is_symlink()
    }
    allowed_files = {
        "metrics/provenance.json",
        "logs/provenance.log",
        # Redirections are opened by the trusted runner before this process
        # begins.  They contain no input and are not used to derive contract
        # identity.
        "logs/m4_initialize.stdout",
        "logs/m4_initialize.stderr",
    }
    unexpected_files = {
        relative
        for relative in existing_files
        if relative not in allowed_files
        and not relative.startswith("runtime_overlay/")
        and relative
        not in {
            "raw/resolved_flight_scenario.yaml",
            "raw/resolved_flight_scenario.identity.json",
        }
    }
    if not existing_files or unexpected_files:
        raise M4ValidationError(
            f"unexpected files before M4 initialization: {sorted(unexpected_files)}"
        )
    for relative in (
        "logs/provider_wire",
        "metrics",
        "pcap",
        "raw/control",
        "raw/endpoints",
        "raw/state",
        "raw/topology",
        "raw/clock_sources",
        "raw/prerequisites",
        "runtime",
    ):
        (run_dir / relative).mkdir(parents=True, exist_ok=True)
    prerequisite_manifest, receipt_hashes = _copy_prerequisites(run_dir)
    if set(receipt_hashes) != {"m0", "m1", "m2", "m3"}:
        raise M4ValidationError("M4 capacity requires exact M0-M3 receipts")
    provenance_path = run_dir / "metrics/provenance.json"
    provenance = strict_json(provenance_path)
    source_commit = os.environ.get("AMS_COMPONENT_SOURCE_COMMIT")
    image_digest = os.environ.get("AMS_CONTAINER_IMAGE_DIGEST")
    if provenance.get("run_id") != args.run_id or provenance.get("git_dirty") is not False:
        raise M4ValidationError("capacity provenance was not created from clean source")
    created = time.monotonic_ns()
    if args.endpoint_mode not in {ACTUAL_ENDPOINT_MODE, TECHNICAL_ENDPOINT_MODE}:
        raise M4ValidationError("unknown M4 endpoint mode")
    endpoint_acceptance_eligible = args.endpoint_mode == ACTUAL_ENDPOINT_MODE
    accepted_api: dict[str, Any] | None = None
    api_sha256: str | None = None
    if endpoint_acceptance_eligible:
        m3_receipt = strict_json(run_dir / "raw/prerequisites/m3.json")
        m3_result = m3_receipt.get("result")
        accepted_api = (
            m3_result.get("actual_control_api")
            if isinstance(m3_result, dict)
            else None
        )
        if (
            m3_receipt.get("contract") != "ams.m3.host-final-receipt/v1"
            or m3_receipt.get("profile") != "m3_component"
            or m3_receipt.get("formal_accepted") is not True
            or m3_receipt.get("passed") is not True
            or m3_receipt.get("failures") != []
            or not isinstance(m3_result, dict)
            or m3_result.get("contract")
            != "ams.m3.external-matrix-validation/v1"
            or m3_result.get("passed") is not True
            or m3_result.get("acceptance_eligible") is not True
            or m3_result.get("failures") != []
            or accepted_api != _expected_actual_control_api()
        ):
            raise M4ValidationError("M4 capacity accepted M3 API is absent/different")
        write_exclusive(run_dir / "raw/prerequisites/m3-result.json", m3_result)
        api_sha256 = hashlib.sha256(canonical(accepted_api)).hexdigest()
    runtime_assets = _runtime_asset_manifest(args.installed_share)
    write_exclusive(run_dir / "raw/runtime_asset_manifest.json", runtime_assets)
    # The formal five-UAV flight controller is admitted only after the shared
    # SITL tail, ns-3 engine, Sionna adapter, and three-heartbeat link gate are
    # live.  The runway covers every declared sequential readiness wait plus
    # all bounded retry/drain, pre-arm-state, and airborne-state waits.  Keep
    # the remaining reserve explicit so future timeout edits cannot silently
    # make the schedule impossible.
    readiness_reserve_ns = (
        CAPACITY_READINESS_RUNWAY_NS
        - CAPACITY_DECLARED_READINESS_WAITS_NS
        - CAPACITY_BOUNDED_PREFLIGHT_NS
    )
    if readiness_reserve_ns != 85_500_000_000:
        raise M4ValidationError("capacity readiness execution budget differs")
    contract_to_clean_shutdown_ns = (
        CAPACITY_READINESS_RUNWAY_NS
        + CAPACITY_WARMUP_NS
        + CAPACITY_MEASUREMENT_NS
        + CAPACITY_POST_MEASUREMENT_CONTROL_NS
        + CAPACITY_LANDING_STATE_NS
        + CAPACITY_DISARM_NS
    )
    if (
        CAPACITY_NS3_ENGINE_DURATION_NS - contract_to_clean_shutdown_ns
        != 60_000_000_000
        or CAPACITY_WRAPPER_TIMEOUT_NS - contract_to_clean_shutdown_ns
        != 260_000_000_000
    ):
        raise M4ValidationError("capacity outer execution budget differs")
    warmup_start = created + CAPACITY_READINESS_RUNWAY_NS
    measurement_start = warmup_start + CAPACITY_WARMUP_NS
    measurement_end = measurement_start + CAPACITY_MEASUREMENT_NS
    schedule = {
        "readiness_deadline_monotonic_ns": warmup_start,
        "warmup_start_monotonic_ns": warmup_start,
        "measurement_start_monotonic_ns": measurement_start,
        "measurement_end_monotonic_ns": measurement_end,
        "readiness_stability_ns": 10_000_000_000,
        "warmup_ns": CAPACITY_WARMUP_NS,
        "measurement_ns": CAPACITY_MEASUREMENT_NS,
        "rtf_window_ns": 1_000_000_000,
        "rtf_window_count": 600,
        "rtf_passing_minimum": 570,
    }
    vector = provenance.get("qualification_content_vector")
    vector_hash = hashlib.sha256(
        json.dumps(vector, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    try:
        import sionna.rt as sionna_rt
    except ImportError as exc:
        raise M4ValidationError(f"Sionna RT import failed before contract: {exc}") from exc
    contract = {
        "schema_version": 3,
        "contract": CAPACITY_CONTRACT,
        "run_id": args.run_id,
        "runtime_id": args.runtime_id,
        "run_nonce": args.run_nonce,
        "profile": "m4_capacity_prerequisite",
        "created_monotonic_ns": created,
        "provider_mode": "real_sionna",
        "acceptance_eligible": endpoint_acceptance_eligible,
        "uav_count": 5,
        "expected_cells": 30,
        "bundle": {
            "path": "network/config/m4_canonical_scene_bundle.json",
            "bundle_id": FROZEN_BUNDLE_ID,
            "bundle_sha256": FROZEN_BUNDLE_SHA256,
        },
        "async_policy": {
            "query_period_ns": QUERY_PERIOD_NS,
            "validity_ttl_ns": VALIDITY_TTL_NS,
            "query_deadline_ns": QUERY_DEADLINE_NS,
            "max_pose_age_ns": MAX_POSE_AGE_NS,
            "late_policy": "fail_closed_directed_link",
            "hold_last_beyond_expiry": False,
        },
        "schedule": schedule,
        "execution_budget": {
            "contract": CAPACITY_EXECUTION_BUDGET_CONTRACT,
            "readiness_runway_ns": CAPACITY_READINESS_RUNWAY_NS,
            "declared_sequential_readiness_waits_ns": (
                CAPACITY_DECLARED_READINESS_WAITS_NS
            ),
            "bounded_preflight_ns": CAPACITY_BOUNDED_PREFLIGHT_NS,
            "readiness_reserve_ns": readiness_reserve_ns,
            "warmup_ns": CAPACITY_WARMUP_NS,
            "bounded_warmup_motion_ns": CAPACITY_BOUNDED_WARMUP_MOTION_NS,
            "warmup_after_motion_reserve_ns": (
                CAPACITY_WARMUP_NS - CAPACITY_BOUNDED_WARMUP_MOTION_NS
            ),
            "measurement_ns": CAPACITY_MEASUREMENT_NS,
            "post_measurement_control_ns": CAPACITY_POST_MEASUREMENT_CONTROL_NS,
            "landing_state_ns": CAPACITY_LANDING_STATE_NS,
            "disarm_ns": CAPACITY_DISARM_NS,
            "contract_to_clean_shutdown_bound_ns": contract_to_clean_shutdown_ns,
            "ns3_engine_duration_ns": CAPACITY_NS3_ENGINE_DURATION_NS,
            "ns3_unallocated_margin_ns": (
                CAPACITY_NS3_ENGINE_DURATION_NS - contract_to_clean_shutdown_ns
            ),
            "wrapper_timeout_ns": CAPACITY_WRAPPER_TIMEOUT_NS,
            "wrapper_precontract_and_finalization_reserve_ns": (
                CAPACITY_WRAPPER_TIMEOUT_NS - contract_to_clean_shutdown_ns
            ),
        },
        "airborne_gate": airborne_gate_contract(schedule),
        "workload": {
            "matrix_path": "network/config/endpoint_matrix_5uav.json",
            "matrix_sha256": sha256_file(ROOT / "network/config/endpoint_matrix_5uav.json"),
            "cell_count": 30,
            "endpoint_phase": "positive",
            "transport_nonce_derivation": "sha256-run-nonce-prefix16-v1",
            "accepted_m3_receipt_path": "raw/prerequisites/m3.json",
            "accepted_m3_receipt_sha256": receipt_hashes["m3"],
        },
        "endpoint_path": (
            {
                "mode": accepted_api["control_endpoint_form"],
                "acceptance_eligible": True,
                "traffic_origin": "actual_ardupilot_mavproxy",
                "accepted_m3_receipt_path": "raw/prerequisites/m3.json",
                "accepted_m3_receipt_sha256": receipt_hashes["m3"],
                "actual_control_api_contract": accepted_api["contract"],
                "actual_control_api_sha256": api_sha256,
                "actual_sitl_manifest_path": "raw/actual_sitl_endpoint_manifest.json",
                "actual_sitl_ready_path": "raw/state/actual-sitl-endpoints.ready.json",
                "actual_control_events_path": "raw/actual_control/events.jsonl",
            }
            if accepted_api is not None
            else {
                "mode": args.endpoint_mode,
                "acceptance_eligible": False,
                "traffic_origin": "synthetic_matrix_fixture",
                "lineage_contract": "ineligible_no_sitl_lineage",
                "lineage_path": None,
                "synthetic_endpoint_agent_sha256": sha256_file(
                    ROOT / "network/scripts/m4_endpoint_agent.py"
                ),
            }
        ),
        "clock_producers": list(REQUIRED_CLOCK_PRODUCERS),
        "limits": {
            "request_queue_capacity": 64,
            "completion_queue_capacity": 64,
            "max_poll_batch": 64,
            "max_message_bytes": 1_048_576,
            "max_state_line_bytes": 65_536,
            "max_packet_event_line_bytes": 65_536,
            "max_fault_captured_results": 64,
            "max_fault_pending_per_cell": 2,
            "max_fault_release_queue": 8,
        },
        "runtime_assets": {
            "path": "raw/runtime_asset_manifest.json",
            "sha256": sha256_file(run_dir / "raw/runtime_asset_manifest.json"),
        },
        "identity": {
            "source_commit": source_commit,
            "git_dirty": False,
            "container_image_digest": image_digest,
            "nvidia_driver_capabilities": os.environ.get("NVIDIA_DRIVER_CAPABILITIES"),
            "mitsuba_variant": os.environ.get("SIONNA_MITSUBA_VARIANT"),
            "competing_load_policy": "exclusive_simulation_and_gpu",
            "provenance_sha256": sha256_file(provenance_path),
            "dependency_lock_sha256": sha256_file(ROOT / "network/config/dependency_lock.yaml"),
            "ownership_map_sha256": sha256_file(
                ROOT / "network/config/qualification_path_ownership.json"
            ),
            "qualification_vector_sha256": vector_hash,
            "prerequisite_manifest_sha256": sha256_file(run_dir / "raw/prerequisites.json"),
            "runtime_asset_manifest_sha256": sha256_file(
                run_dir / "raw/runtime_asset_manifest.json"
            ),
            "executable_manifest": {
                "ns3_packet_engine": _executable_record(args.engine_binary),
                "python": _executable_record(Path(sys.executable)),
                "sionna_rt": _executable_record(Path(sionna_rt.__file__)),
            },
        },
        "source_sha256": {
            relative: sha256_file(ROOT / relative) for relative in sorted(REQUIRED_SOURCE_PATHS)
        },
    }
    if contract["bundle"]["bundle_sha256"] != strict_json(
        ROOT / contract["bundle"]["path"]
    ).get("bundle_sha256"):
        raise M4ValidationError("checked-in bundle differs before capacity execution")
    contract_path = run_dir / "raw/m4_capacity_contract.json"
    write_exclusive(contract_path, contract)
    write_exclusive(run_dir / "raw/run_contract.json", contract)

    # The frozen nominal workload starts exactly at the measurement boundary;
    # warm-up is reserved for the modeled-path reposition command.  Four
    # hundred units over the 600-second half-open interval equal the accepted
    # M3 nominal 20-units-per-30-seconds rate without relying on excluded load.
    command_start = measurement_start
    command_end = measurement_end
    count = 400
    command = {
        "action": "phase",
        "run_id": args.run_id,
        "runtime_id": args.runtime_id,
        "run_nonce": args.run_nonce,
        "phase": "positive",
        "window_id": "capacity_measurement",
        "start_monotonic_ns": command_start,
        "end_monotonic_ns": command_end,
        "offered_per_cell": count,
        "p2mp_roots": 0,
        "send_span_ms": 598_500,
        "expected_engine_state": "up_epoch_1",
    }
    for endpoint in ("gcs", "uav1", "uav2", "uav3", "uav4", "uav5"):
        target = run_dir / f"raw/control/{endpoint}"
        target.mkdir(parents=True, exist_ok=True)
        write_exclusive(target / "001-capacity.json", {**command, "endpoint": endpoint})
        write_exclusive(
            target / "999-shutdown.json",
            {
                "action": "shutdown",
                "endpoint": endpoint,
                "run_id": args.run_id,
                "runtime_id": args.runtime_id,
                "run_nonce": args.run_nonce,
                "not_before_monotonic_ns": measurement_end + 500_000_000,
            },
        )
    if accepted_api is not None:
        actual_control_dir = run_dir / "raw/control/actual-control"
        actual_control_dir.mkdir(parents=True, exist_ok=True)
        actual_control_end = measurement_end + CAPACITY_POST_MEASUREMENT_CONTROL_NS
        flow_group_ids = {
            f"uav{index}": accepted_api["channels"][f"uav{index}"]["matrix"][
                "downlink_cell_id"
            ]
            for index in range(1, 6)
        }
        write_exclusive(
            actual_control_dir / "001-capacity.json",
            {
                "action": "window",
                "endpoint": "actual-control",
                "run_id": args.run_id,
                "runtime_id": args.runtime_id,
                "run_nonce": args.run_nonce,
                "profile": "m4_capacity",
                "window_id": "capacity_measurement",
                "transport_phase_code": 4,
                "start_monotonic_ns": command_start,
                "end_monotonic_ns": actual_control_end,
                "offered_per_uav": count,
                "send_span_ms": 598_500,
                "expected_engine_state": "up_epoch_1",
                "response_policies": {
                    f"uav{index}": "ack_required" for index in range(1, 6)
                },
                "minimum_quiet_drain_ns_by_uav": {
                    f"uav{index}": 0 for index in range(1, 6)
                },
                "flow_group_ids": flow_group_ids,
            },
        )
        write_exclusive(
            actual_control_dir / "999-shutdown.json",
            {
                "action": "shutdown",
                "endpoint": "actual-control",
                "run_id": args.run_id,
                "runtime_id": args.runtime_id,
                "run_nonce": args.run_nonce,
                "not_before_monotonic_ns": actual_control_end + 500_000_000,
            },
        )
    write_exclusive(run_dir / "raw/capacity_schedule.json", schedule)
    print(contract_path)
    return 0


def initialize_causality(args: argparse.Namespace) -> int:
    """Freeze the actual-SITL causal schedule and all producer commands."""

    run_dir = args.run_dir.resolve()
    if not run_dir.is_dir():
        raise M4ValidationError(f"run directory was not pre-created: {run_dir}")
    existing_files = {
        path.relative_to(run_dir).as_posix()
        for path in run_dir.rglob("*")
        if path.is_file() or path.is_symlink()
    }
    allowed_files = {
        "metrics/provenance.json",
        "logs/provenance.log",
        "logs/m4_causality_initialize.stdout",
        "logs/m4_causality_initialize.stderr",
    }
    unexpected = {
        relative
        for relative in existing_files
        if relative not in allowed_files
        and relative not in ACTUAL_SITL_AUDIT_LOG_PATHS
        and not relative.startswith("runtime_overlay/")
        and not relative.startswith("runtime/")
        and not relative.startswith("logs/m4_causal_overlay_build.")
        and not relative.startswith("logs/actual-sitl-")
        and not relative.startswith("raw/actual_sitl/")
        and not relative.startswith("raw/topology/")
        and not relative.startswith("raw/state/actual-sitl")
        and relative
        not in {
            "raw/actual_sitl_endpoint_manifest.json",
            "raw/resolved_flight_scenario.yaml",
            "raw/resolved_flight_scenario.identity.json",
        }
    }
    if not existing_files or unexpected:
        raise M4ValidationError(
            f"unexpected files before M4 causality initialization: {sorted(unexpected)}"
        )
    for relative in (
        "logs/provider_wire",
        "metrics",
        "pcap",
        "raw/actual_control",
        "raw/control/actual-control",
        "raw/control/adapter",
        "raw/endpoints",
        "raw/state",
        "raw/topology",
        "raw/clock_sources",
        "raw/prerequisites",
        "runtime",
    ):
        (run_dir / relative).mkdir(parents=True, exist_ok=True)

    prerequisite_manifest, receipt_hashes = _copy_prerequisites(run_dir)
    expected_prerequisites = {
        "m0",
        "m1",
        "m2",
        "m3",
        "m4_capacity_prerequisite",
    }
    if set(receipt_hashes) != expected_prerequisites:
        raise M4ValidationError("M4 causality requires exact M0-M3 and capacity receipts")
    m3_receipt = strict_json(run_dir / "raw/prerequisites/m3.json")
    m3_result = m3_receipt.get("result")
    accepted_api = (
        m3_result.get("actual_control_api") if isinstance(m3_result, dict) else None
    )
    if (
        m3_receipt.get("formal_accepted") is not True
        or not isinstance(m3_result, dict)
        or m3_result.get("passed") is not True
        or accepted_api != _expected_actual_control_api()
    ):
        raise M4ValidationError("M4 causality accepted M3 API is absent/different")
    write_exclusive(run_dir / "raw/prerequisites/m3-result.json", m3_result)
    api_sha256 = hashlib.sha256(canonical(accepted_api)).hexdigest()

    provenance_path = run_dir / "metrics/provenance.json"
    provenance = strict_json(provenance_path)
    source_commit = os.environ.get("AMS_COMPONENT_SOURCE_COMMIT")
    image_digest = os.environ.get("AMS_CONTAINER_IMAGE_DIGEST")
    if (
        provenance.get("run_id") != args.run_id
        or provenance.get("git_dirty") is not False
        or prerequisite_manifest.get("profile") != "m4_component"
    ):
        raise M4ValidationError("causality provenance/prerequisite profile differs")
    runtime_assets = _runtime_asset_manifest(args.installed_share)
    write_exclusive(run_dir / "raw/runtime_asset_manifest.json", runtime_assets)
    resolved_payload, resolved_details = resolve_five_uav_flight_scenario(
        args.flight_scenario.resolve(strict=True)
    )
    resolved_path = run_dir / "raw/resolved_flight_scenario.yaml"
    resolved_identity_path = run_dir / "raw/resolved_flight_scenario.identity.json"
    if resolved_path.exists():
        if (
            resolved_path.read_bytes() != resolved_payload
            or strict_json(resolved_identity_path) != resolved_details
        ):
            raise M4ValidationError("prestarted actual-SITL resolved scenario differs")
    else:
        write_exclusive(resolved_path, resolved_payload)
        write_exclusive(resolved_identity_path, resolved_details)

    try:
        import sionna.rt as sionna_rt
    except ImportError as exc:
        raise M4ValidationError(f"Sionna RT import failed before contract: {exc}") from exc
    created = time.monotonic_ns()
    if (
        isinstance(args.runner_start_monotonic_ns, bool)
        or args.runner_start_monotonic_ns <= 0
        or created < args.runner_start_monotonic_ns
        or created - args.runner_start_monotonic_ns > PRECONTRACT_SETUP_BUDGET_NS
    ):
        raise M4ValidationError("causality precontract setup exceeded 120 seconds")

    windows: list[dict[str, Any]] = []
    start = created + RUNTIME_READINESS_BUDGET_NS
    for index, window_id in enumerate(WINDOW_IDS):
        if index:
            start += causal_pre_window_gap_ns(window_id)
        scenario, phase, target_cell, control_cell = WINDOW_SHAPES[window_id]
        target = matrix_flow_group_identity(
            target_cell,
            accepted_api["control_endpoint_form"],
            matrix_sha256=accepted_api["matrix_sha256"],
        )
        control = matrix_flow_group_identity(
            control_cell,
            accepted_api["control_endpoint_form"],
            matrix_sha256=accepted_api["matrix_sha256"],
        )
        background = matrix_flow_group_identity(
            BACKGROUND_CELL_ID,
            accepted_api["control_endpoint_form"],
            matrix_sha256=accepted_api["matrix_sha256"],
        )
        plan = causal_window_plan(window_id)
        if window_id.startswith("terrain_") or window_id.startswith("building_"):
            pose_set = window_id
        elif window_id.startswith("jammer_"):
            pose_set = "jammer_pose"
        else:
            pose_set = "terrain_good"
        end = start + int(plan["duration_ns"])
        windows.append(
            {
                "window_id": window_id,
                "scenario": scenario,
                "phase": phase,
                "control_endpoint_form": accepted_api["control_endpoint_form"],
                "endpoint_matrix_sha256": accepted_api["matrix_sha256"],
                "target_cell_id": target_cell,
                "control_cell_id": control_cell,
                "background_cell_id": BACKGROUND_CELL_ID,
                "target_link": target["directed_link_id"],
                "control_link": control["directed_link_id"],
                "background_link": background["directed_link_id"],
                "target_flow_group_id": target["flow_group_id"],
                "control_flow_group_id": control["flow_group_id"],
                "background_flow_group_id": background["flow_group_id"],
                "concurrent_flow_group_ids": [
                    target["flow_group_id"],
                    control["flow_group_id"],
                    background["flow_group_id"],
                ],
                "traffic_class": "control",
                "transport_phase_code": index + 1,
                "offered_per_uav": int(plan["offered_per_uav"]),
                "send_span_ms": int(plan["send_span_ms"]),
                "expected_engine_state": "up_epoch_1",
                "response_policies": causal_response_policies(window_id),
                "minimum_quiet_drain_ns_by_uav": causal_quiet_drain_map(window_id),
                "start_monotonic_ns": start,
                "end_monotonic_ns": end,
                "pose_set": pose_set,
                "jammer_enabled": window_id == "jammer_on",
                "jammer_on_classification": (
                    "positive_impaired" if window_id.startswith("jammer_") else None
                ),
            }
        )
        start = end
    if windows[-1]["end_monotonic_ns"] - windows[0]["start_monotonic_ns"] != CAUSAL_MEASUREMENT_SPAN_NS:
        raise M4ValidationError("causality generated measurement span differs")

    planned_total = (
        PRECONTRACT_SETUP_BUDGET_NS
        + RUNTIME_READINESS_BUDGET_NS
        + CAUSAL_MEASUREMENT_SPAN_NS
        + FINALIZATION_BUDGET_NS
        + REQUIRED_WRAPPER_RESERVE_NS
    )
    ns3_required_runtime = (
        RUNTIME_READINESS_BUDGET_NS
        + CAUSAL_MEASUREMENT_SPAN_NS
        + FINALIZATION_BUDGET_NS
    )
    vector = provenance.get("qualification_content_vector")
    vector_hash = hashlib.sha256(
        json.dumps(vector, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    contract = {
        "schema_version": 2,
        "contract": CAUSALITY_CONTRACT,
        "run_id": args.run_id,
        "runtime_id": args.runtime_id,
        "run_nonce": args.run_nonce,
        "profile": "m4_component",
        "runner_start_monotonic_ns": args.runner_start_monotonic_ns,
        "created_monotonic_ns": created,
        "provider_mode": "real_sionna",
        "acceptance_eligible": True,
        "uav_count": 5,
        "expected_cells": 30,
        "bundle": {
            "path": "network/config/m4_canonical_scene_bundle.json",
            "bundle_id": FROZEN_BUNDLE_ID,
            "bundle_sha256": FROZEN_BUNDLE_SHA256,
        },
        "async_policy": {
            "query_period_ns": QUERY_PERIOD_NS,
            "validity_ttl_ns": VALIDITY_TTL_NS,
            "query_deadline_ns": QUERY_DEADLINE_NS,
            "max_pose_age_ns": MAX_POSE_AGE_NS,
            "late_policy": "fail_closed_directed_link",
            "hold_last_beyond_expiry": False,
        },
        "workload": {
            "matrix_path": accepted_api["matrix_path"],
            "matrix_sha256": accepted_api["matrix_sha256"],
            "control_cell_count": 10,
            "control_endpoint_form": accepted_api["control_endpoint_form"],
            "accepted_m3_receipt_path": "raw/prerequisites/m3.json",
            "accepted_m3_receipt_sha256": receipt_hashes["m3"],
            "capacity_receipt_path": "raw/prerequisites/m4_capacity_prerequisite.json",
            "capacity_receipt_sha256": receipt_hashes["m4_capacity_prerequisite"],
        },
        "endpoint_path": {
            "mode": accepted_api["control_endpoint_form"],
            "acceptance_eligible": True,
            "traffic_origin": "actual_ardupilot_mavproxy",
            "accepted_m3_receipt_path": "raw/prerequisites/m3.json",
            "accepted_m3_receipt_sha256": receipt_hashes["m3"],
            "actual_control_api_contract": accepted_api["contract"],
            "actual_control_api_sha256": api_sha256,
            "actual_sitl_manifest_path": "raw/actual_sitl_endpoint_manifest.json",
            "actual_sitl_ready_path": "raw/state/actual-sitl-endpoints.ready.json",
            "actual_control_events_path": "raw/actual_control/events.jsonl",
        },
        "clock_producers": list(REQUIRED_CLOCK_PRODUCERS),
        "windows": windows,
        "execution_budget": {
            "wrapper_timeout_ns": WRAPPER_TIMEOUT_NS,
            "precontract_setup_budget_ns": PRECONTRACT_SETUP_BUDGET_NS,
            "runtime_readiness_budget_ns": RUNTIME_READINESS_BUDGET_NS,
            "causal_measurement_span_ns": CAUSAL_MEASUREMENT_SPAN_NS,
            "finalization_budget_ns": FINALIZATION_BUDGET_NS,
            "required_wrapper_reserve_ns": REQUIRED_WRAPPER_RESERVE_NS,
            "planned_total_ns": planned_total,
            "unallocated_margin_ns": WRAPPER_TIMEOUT_NS - planned_total,
            "ns3_engine_duration_ns": NS3_ENGINE_DURATION_NS,
            "ns3_required_runtime_ns": ns3_required_runtime,
            "ns3_unallocated_margin_ns": (
                NS3_ENGINE_DURATION_NS - ns3_required_runtime
            ),
        },
        "causality_statistics": {
            "paired_bootstrap_seed": 42,
            "paired_bootstrap_resamples": 10_000,
            "confidence_level": 0.95,
            "interval_method": "paired_percentile",
            "pairing": "flow_group_id+ordinal_send_slot",
        },
        "limits": {
            "request_queue_capacity": 64,
            "completion_queue_capacity": 64,
            "max_poll_batch": 64,
            "max_message_bytes": 1_048_576,
            "max_state_line_bytes": 65_536,
            "max_packet_event_line_bytes": 65_536,
            "max_fault_captured_results": 64,
            "max_fault_pending_per_cell": 2,
            "max_fault_release_queue": 8,
        },
        "runtime_assets": {
            "path": "raw/runtime_asset_manifest.json",
            "sha256": sha256_file(run_dir / "raw/runtime_asset_manifest.json"),
        },
        "identity": {
            "source_commit": source_commit,
            "git_dirty": False,
            "container_image_digest": image_digest,
            "nvidia_driver_capabilities": os.environ.get("NVIDIA_DRIVER_CAPABILITIES"),
            "mitsuba_variant": os.environ.get("SIONNA_MITSUBA_VARIANT"),
            "competing_load_policy": "exclusive_simulation_and_gpu",
            "provenance_sha256": sha256_file(provenance_path),
            "dependency_lock_sha256": sha256_file(ROOT / "network/config/dependency_lock.yaml"),
            "ownership_map_sha256": sha256_file(
                ROOT / "network/config/qualification_path_ownership.json"
            ),
            "qualification_vector_sha256": vector_hash,
            "prerequisite_manifest_sha256": sha256_file(run_dir / "raw/prerequisites.json"),
            "runtime_asset_manifest_sha256": sha256_file(
                run_dir / "raw/runtime_asset_manifest.json"
            ),
            "executable_manifest": {
                "ns3_packet_engine": _executable_record(args.engine_binary),
                "python": _executable_record(Path(sys.executable)),
                "sionna_rt": _executable_record(Path(sionna_rt.__file__)),
            },
        },
        "source_sha256": {
            relative: sha256_file(ROOT / relative)
            for relative in sorted(CAUSAL_SOURCE_PATHS)
        },
    }
    write_exclusive(run_dir / "raw/m4_causality_contract.json", contract)
    write_exclusive(run_dir / "raw/run_contract.json", contract)

    flow_groups = {
        f"uav{index}": matrix_flow_group_identity(
            f"uav{index}.control.downlink",
            accepted_api["control_endpoint_form"],
            matrix_sha256=accepted_api["matrix_sha256"],
        )["flow_group_id"]
        for index in range(1, 6)
    }
    for sequence, window in enumerate(windows, start=1):
        control_command = {
            "action": "window",
            "endpoint": "actual-control",
            "run_id": args.run_id,
            "runtime_id": args.runtime_id,
            "run_nonce": args.run_nonce,
            "profile": "m4_causality",
            "window_id": window["window_id"],
            "transport_phase_code": window["transport_phase_code"],
            "start_monotonic_ns": window["start_monotonic_ns"],
            "end_monotonic_ns": window["end_monotonic_ns"],
            "offered_per_uav": window["offered_per_uav"],
            "send_span_ms": window["send_span_ms"],
            "expected_engine_state": window["expected_engine_state"],
            "response_policies": window["response_policies"],
            "minimum_quiet_drain_ns_by_uav": window[
                "minimum_quiet_drain_ns_by_uav"
            ],
            "flow_group_ids": flow_groups,
        }
        write_exclusive(
            run_dir / f"raw/control/actual-control/{sequence:03d}-{window['window_id']}.json",
            control_command,
        )
        companion_command = {
            "action": "phase",
            "run_id": args.run_id,
            "runtime_id": args.runtime_id,
            "run_nonce": args.run_nonce,
            "phase": "positive",
            "window_id": window["window_id"],
            "start_monotonic_ns": window["start_monotonic_ns"],
            "end_monotonic_ns": window["end_monotonic_ns"],
            "offered_per_cell": window["offered_per_uav"],
            "p2mp_roots": 0,
            "send_span_ms": window["send_span_ms"],
            "expected_engine_state": window["expected_engine_state"],
        }
        for endpoint in ("gcs", "uav1", "uav2", "uav3", "uav4", "uav5"):
            directory = run_dir / f"raw/control/{endpoint}"
            directory.mkdir(parents=True, exist_ok=True)
            write_exclusive(
                directory / f"{sequence:03d}-{window['window_id']}.json",
                {**companion_command, "endpoint": endpoint},
            )
    shutdown_ns = int(windows[-1]["end_monotonic_ns"])
    write_exclusive(
        run_dir / "raw/control/actual-control/012-shutdown.json",
        {
            "action": "shutdown",
            "endpoint": "actual-control",
            "run_id": args.run_id,
            "runtime_id": args.runtime_id,
            "run_nonce": args.run_nonce,
            "not_before_monotonic_ns": shutdown_ns,
        },
    )
    for endpoint in ("gcs", "uav1", "uav2", "uav3", "uav4", "uav5"):
        write_exclusive(
            run_dir / f"raw/control/{endpoint}/012-shutdown.json",
            {
                "action": "shutdown",
                "endpoint": endpoint,
                "run_id": args.run_id,
                "runtime_id": args.runtime_id,
                "run_nonce": args.run_nonce,
                "not_before_monotonic_ns": shutdown_ns,
            },
        )
    return 0


def run_provider(args: argparse.Namespace) -> int:
    contract_path = args.contract.resolve()
    contract = strict_json(contract_path)
    identity, _contract_hash, _config_hash = identity_for_contract(contract_path)
    bundle = strict_json(ROOT / "network/config/m4_canonical_scene_bundle.json")
    executable = Path(__file__).resolve()
    try:
        sionna_version = importlib.metadata.version("sionna-rt")
        mitsuba_version = importlib.metadata.version("mitsuba")
    except importlib.metadata.PackageNotFoundError as exc:
        raise M4ValidationError(f"provider package identity missing: {exc}") from exc
    config = ProviderServiceConfig(
        identity=identity,
        phase_id="m4_continuous_runtime",
        sender_id="sionna-provider-m4",
        clock_domain="host-monotonic",
        executable_path=str(executable),
        executable_sha256=sha256_file(executable),
        scene_path=str((ROOT / bundle["sionna_scene_xml"]).resolve()),
        scene_manifest_sha256=str(bundle["bundle_sha256"]),
        scene_material_manifest_sha256=str(bundle["scene_material_manifest_sha256"]),
        provider_id="sionna-rt-cuda-m4",
        sionna_rt_version=sionna_version,
        mitsuba_version=mitsuba_version,
    )
    files = RuntimeFiles(
        scenario=ROOT / "network/config/scenario_m4_canonical.yaml",
        radio=ROOT / "network/config/radio_m4_canonical.yaml",
        jammers=ROOT / "network/config/jammers_m4_canonical.yaml",
        service_tiers=ROOT / "network/config/service_tiers.yaml",
    )
    wire = ExactWireLog(args.run_dir / "logs/provider_wire", fsync=False)
    service = create_production_service(
        runtime_files=files,
        config=config,
        wire_log=wire,
        host="127.0.0.1",
        port=args.port,
        limits=load_protocol_limits(),
    )
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_unused: stop.set())
    signal.signal(signal.SIGINT, lambda *_unused: stop.set())
    beacon_thread = threading.Thread(
        target=beacon,
        args=(args.clock_socket.resolve(), "sionna_worker", stop),
        daemon=True,
    )
    service.start()
    beacon_thread.start()
    write_exclusive(
        args.ready_file,
        {
            "pid": os.getpid(),
            "port": service.port,
            "monotonic_ns": time.monotonic_ns(),
            "provider_mode": "real_sionna",
            "bundle_sha256": bundle["bundle_sha256"],
            "run_id": contract["run_id"],
        },
    )
    try:
        while not stop.is_set() and not args.stop_file.exists():
            if service.fatal_exception is not None:
                raise M4ValidationError(
                    f"real Sionna service failed: {service.fatal_exception}"
                )
            stop.wait(0.05)
    finally:
        stop.set()
        service.stop(timeout_s=10.0)
        beacon_thread.join(2.0)
    return 0


def run_beacon(args: argparse.Namespace) -> int:
    if args.producer not in SAFE_PRODUCERS:
        raise M4ValidationError("unknown frozen clock producer")
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_unused: stop.set())
    signal.signal(signal.SIGINT, lambda *_unused: stop.set())
    beacon(args.socket.resolve(), args.producer, stop)
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare-causality-flight")
    prepare.add_argument("--flight-scenario", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--identity-output", type=Path, required=True)
    prepare.set_defaults(function=prepare_causality_flight)

    initialize = commands.add_parser("initialize-capacity")
    initialize.add_argument("--run-dir", type=Path, required=True)
    initialize.add_argument("--run-id", required=True)
    initialize.add_argument("--runtime-id", default=None)
    initialize.add_argument("--run-nonce", required=True)
    initialize.add_argument("--engine-binary", type=Path, required=True)
    initialize.add_argument("--installed-share", type=Path, required=True)
    initialize.add_argument("--endpoint-mode", required=True)
    initialize.set_defaults(function=initialize_capacity)

    causality = commands.add_parser("initialize-causality")
    causality.add_argument("--run-dir", type=Path, required=True)
    causality.add_argument("--run-id", required=True)
    causality.add_argument("--runtime-id", required=True)
    causality.add_argument("--run-nonce", required=True)
    causality.add_argument("--runner-start-monotonic-ns", type=int, required=True)
    causality.add_argument("--engine-binary", type=Path, required=True)
    causality.add_argument("--installed-share", type=Path, required=True)
    causality.add_argument("--flight-scenario", type=Path, required=True)
    causality.set_defaults(function=initialize_causality)

    provider = commands.add_parser("provider")
    provider.add_argument("--run-dir", type=Path, required=True)
    provider.add_argument("--contract", type=Path, required=True)
    provider.add_argument("--port", type=int, default=5090)
    provider.add_argument("--ready-file", type=Path, required=True)
    provider.add_argument("--stop-file", type=Path, required=True)
    provider.add_argument("--clock-socket", type=Path, required=True)
    provider.set_defaults(function=run_provider)

    clock = commands.add_parser("beacon")
    clock.add_argument("--producer", required=True)
    clock.add_argument("--socket", type=Path, required=True)
    clock.set_defaults(function=run_beacon)

    return root


def main() -> int:
    args = parser().parse_args()
    if hasattr(args, "runtime_id") and args.runtime_id is None:
        args.runtime_id = secrets.token_hex(16)
    try:
        return int(args.function(args))
    except (M4ValidationError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"FAIL M4 orchestrator: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
