#!/usr/bin/env python3
"""Independently validate M4 obstruction, jammer, and expiry causality.

The validator deliberately consumes decoded actual-endpoint transactions in
addition to ns-3 and Sionna evidence.  A packet-engine event alone cannot prove
that ArduPilot received a command or emitted its ACK/telemetry response.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

from network.validation.m4_common import (
    HEX64,
    M4ValidationError,
    canonical_json,
    exact_keys,
    finite_number,
    gate,
    regular_file,
    strict_json,
    strict_jsonl,
    validate_fault_audit,
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
    REQUIRED_PROCESS_COUNTS,
    VALIDITY_TTL_NS,
    sha256_file,
    validate_clock_correlations,
    validate_clock_process_binding,
    validate_scene_prerequisite,
)
from network.validation.validate_m4_capacity import (
    REQUIRED_SOURCE_PATHS as CAPACITY_SOURCE_PATHS,
    _validate_actual_endpoint_path,
    _validate_identity,
)


ROOT = Path(__file__).resolve().parents[2]
RUN_CONTRACT = "ams.m4.causality_run/v1"
RESULT_CONTRACT = "ams.m4.causality-validation/v1"
TRANSACTION_SCHEMA = "ams.m4.actual_endpoint_transaction/v1"
ADAPTER_AUDIT_SCHEMA = "ams.sionna.packet_adapter_event/v1"
CONTROL_SCHEMA = "ams.m4.adapter_control_event/v1"
FAULT_SCHEMA = "ams.sionna.result_fault_event/v2"
SERVICE_TIERS = (0, 1_000, 10_000, 100_000, 500_000, 2_000_000, 20_000_000)
WINDOW_IDS = (
    "terrain_good",
    "terrain_down",
    "terrain_recovery",
    "building_good",
    "building_down",
    "building_recovery",
    "jammer_off_1",
    "jammer_on",
    "jammer_off_2",
    "expiry_unavailable",
    "expiry_recovery",
)
WINDOW_SHAPES = {
    "terrain_good": (
        "terrain_shadow",
        "good",
        "uav1.control.downlink",
        "uav5.control.downlink",
    ),
    "terrain_down": (
        "terrain_shadow",
        "down",
        "uav1.control.downlink",
        "uav5.control.downlink",
    ),
    "terrain_recovery": (
        "terrain_shadow",
        "recovery",
        "uav1.control.downlink",
        "uav5.control.downlink",
    ),
    "building_good": (
        "building_blocked",
        "good",
        "uav2.control.downlink",
        "uav5.control.downlink",
    ),
    "building_down": (
        "building_blocked",
        "down",
        "uav2.control.downlink",
        "uav5.control.downlink",
    ),
    "building_recovery": (
        "building_blocked",
        "recovery",
        "uav2.control.downlink",
        "uav5.control.downlink",
    ),
    "jammer_off_1": (
        "jammer_off_on_off",
        "off-1",
        "uav3.control.downlink",
        "uav5.control.downlink",
    ),
    "jammer_on": (
        "jammer_off_on_off",
        "on",
        "uav3.control.downlink",
        "uav5.control.downlink",
    ),
    "jammer_off_2": (
        "jammer_off_on_off",
        "off-2",
        "uav3.control.downlink",
        "uav5.control.downlink",
    ),
    "expiry_unavailable": (
        "F-expiry",
        "unavailable",
        "uav1.control.downlink",
        "uav5.control.downlink",
    ),
    "expiry_recovery": (
        "F-expiry",
        "recovery",
        "uav1.control.downlink",
        "uav5.control.downlink",
    ),
}
BACKGROUND_CELL_ID = "uav4.control.downlink"
FLOW_GROUP_SCHEMA = "ams.endpoint_flow_group/v1"
ENDPOINT_FORM = re.compile(r"[a-z0-9][a-z0-9_.-]{0,95}")
CAUSAL_SOURCE_PATHS = CAPACITY_SOURCE_PATHS | {
    "network/scripts/actual_sitl_stack_orchestrator.sh",
    "network/scripts/m4_causal_phase_driver.py",
    "network/scripts/run_m4_causality.sh",
    "network/scripts/validate_m4_causality.py",
    "network/validation/validate_m4_causality.py",
}
OUTCOME_TIMEOUT_NS = 3_000_000_000
TIMEOUT_SLOT_MARGIN_NS = 100_000_000
QUIET_DRAIN_NS = 10_000_000_000
POSITIVE_WINDOW_PLAN = {
    "offered_per_uav": 100,
    "send_span_ms": 19_800,
    "duration_ns": 23_000_000_000,
}
DOWN_WINDOW_PLAN = {
    "offered_per_uav": 100,
    "send_span_ms": 306_900,
    "duration_ns": 310_000_000_000,
}
EXPIRY_DOWN_WINDOW_PLAN = {
    "offered_per_uav": 20,
    "send_span_ms": 58_900,
    "duration_ns": 62_100_000_000,
}
CAUSAL_GAP_NS = 10_000_000_000
WRAPPER_TIMEOUT_NS = 1_200_000_000_000
PRECONTRACT_SETUP_BUDGET_NS = 90_000_000_000
RUNTIME_READINESS_BUDGET_NS = 30_000_000_000
CAUSAL_MEASUREMENT_SPAN_NS = 896_100_000_000
FINALIZATION_BUDGET_NS = 40_000_000_000
REQUIRED_WRAPPER_RESERVE_NS = 120_000_000_000
TIMEOUT_REQUIRED_WINDOWS = frozenset(
    {"terrain_down", "building_down", "expiry_unavailable"}
)
RECOVERY_DRAIN_UAV = {
    "terrain_recovery": "uav1",
    "building_recovery": "uav2",
    "expiry_recovery": "uav1",
}


def causal_window_plan(window_id: str) -> Mapping[str, int]:
    """Return the one frozen single-inflight timing plan for a causal window."""

    if window_id in {"terrain_down", "building_down"}:
        return DOWN_WINDOW_PLAN
    if window_id == "expiry_unavailable":
        return EXPIRY_DOWN_WINDOW_PLAN
    return POSITIVE_WINDOW_PLAN


def causal_response_policies(window_id: str) -> dict[str, str]:
    """Freeze the per-UAV outcome policy; mixed down windows stay observable."""

    policies = {f"uav{index}": "ack_required" for index in range(1, 6)}
    if window_id in TIMEOUT_REQUIRED_WINDOWS:
        target_cell = WINDOW_SHAPES[window_id][2]
        policies[target_cell.split(".", 1)[0]] = "timeout_required"
    return policies


def causal_quiet_drain_map(window_id: str) -> dict[str, int]:
    """Freeze target-scoped stale-outcome drain before each recovery window."""

    drains = {f"uav{index}": 0 for index in range(1, 6)}
    target_uav = RECOVERY_DRAIN_UAV.get(window_id)
    if target_uav is not None:
        drains[target_uav] = QUIET_DRAIN_NS
    return drains


def matrix_flow_group_identity(
    cell_id: str,
    endpoint_form: str,
    *,
    matrix_sha256: str | None = None,
) -> dict[str, str]:
    """Derive the normative flow identity from one exact endpoint-matrix row.

    ``endpoint_form`` is supplied by the accepted M3 actual-control contract;
    M4 never infers a second endpoint taxonomy from source/destination labels.
    """

    if not isinstance(endpoint_form, str) or ENDPOINT_FORM.fullmatch(endpoint_form) is None:
        raise M4ValidationError("accepted M3 control endpoint_form is invalid")
    matrix_path = ROOT / "network/config/endpoint_matrix_5uav.json"
    if matrix_sha256 is not None and (
        not isinstance(matrix_sha256, str)
        or HEX64.fullmatch(matrix_sha256) is None
        or sha256_file(matrix_path) != matrix_sha256
    ):
        raise M4ValidationError("accepted M3 endpoint matrix SHA-256 differs")
    matrix = strict_json(matrix_path)
    rows = [item for item in matrix.get("cells", []) if item.get("cell_id") == cell_id]
    if len(rows) != 1:
        raise M4ValidationError(f"endpoint matrix cell identity differs: {cell_id}")
    row = rows[0]
    uav = row.get("uav", {}).get("name")
    traffic_class = row.get("traffic_class")
    direction = row.get("direction")
    directed_link_id = row.get("ns3_path", {}).get("directed_link_id")
    matrix_flow_id = row.get("identity", {}).get("flow_id")
    if (
        not isinstance(uav, str)
        or traffic_class not in {"control", "payload", "additional_data"}
        or direction not in {"downlink", "uplink"}
        or not isinstance(directed_link_id, str)
        or directed_link_id.count(">") != 1
        or matrix_flow_id != cell_id
    ):
        raise M4ValidationError(f"endpoint matrix flow tuple differs: {cell_id}")
    identity = {
        "schema": FLOW_GROUP_SCHEMA,
        "uav": uav,
        "traffic_class": str(traffic_class),
        "direction": str(direction),
        "endpoint_form": endpoint_form,
        "directed_link_id": directed_link_id,
    }
    digest = hashlib.sha256(canonical_json(identity)).hexdigest()
    return {
        **identity,
        "cell_id": cell_id,
        "flow_group_id": f"ams-flow-group-v1-{digest}",
    }


def matrix_link_identity(cell_id: str) -> tuple[str, str]:
    """Resolve ns-3 and async link IDs from the one Q2/Q3 matrix row."""

    # This helper is used by the expiry fault injector, where endpoint_form is
    # irrelevant but both link spellings must still originate in the matrix.
    matrix = strict_json(ROOT / "network/config/endpoint_matrix_5uav.json")
    rows = [item for item in matrix.get("cells", []) if item.get("cell_id") == cell_id]
    if len(rows) != 1:
        raise M4ValidationError(f"endpoint matrix cell identity differs: {cell_id}")
    row = rows[0]
    ns3_link = row.get("ns3_path", {}).get("directed_link_id")
    traffic_class = row.get("traffic_class")
    if not isinstance(ns3_link, str) or ns3_link.count(">") != 1:
        raise M4ValidationError(f"endpoint matrix directed link differs: {cell_id}")
    source, destination = ns3_link.split(">", 1)
    async_link = f"{source}-to-{destination}-{traffic_class}"
    return ns3_link, async_link


def _nearest_rank(values: list[float], fraction: float) -> float:
    if not values:
        raise M4ValidationError("metric sample is empty")
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def _median(values: list[float], label: str) -> float:
    if not values or any(not finite_number(value) for value in values):
        raise M4ValidationError(f"{label} has no finite samples")
    return float(statistics.median(values))


def _tier_index(value: int) -> int:
    try:
        return SERVICE_TIERS.index(value)
    except ValueError as exc:
        raise M4ValidationError(f"unknown service tier: {value}") from exc


def validate_window_manifest(
    value: Any,
    *,
    control_endpoint_form: str | None = None,
    endpoint_matrix_sha256: str | None = None,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Validate the predeclared, disjoint half-open causal windows."""

    failures: list[str] = []
    result: dict[str, dict[str, Any]] = {}
    if (
        not isinstance(control_endpoint_form, str)
        or ENDPOINT_FORM.fullmatch(control_endpoint_form) is None
    ):
        return {}, ["accepted M3 control endpoint_form is absent/invalid"]
    if (
        not isinstance(endpoint_matrix_sha256, str)
        or HEX64.fullmatch(endpoint_matrix_sha256) is None
        or endpoint_matrix_sha256
        != sha256_file(ROOT / "network/config/endpoint_matrix_5uav.json")
    ):
        return {}, ["accepted M3 endpoint matrix SHA-256 is absent/different"]
    if not isinstance(value, list) or len(value) != len(WINDOW_IDS):
        return {}, ["causal window manifest does not contain exactly 11 windows"]
    previous_end = -1
    expected_keys = {
        "window_id",
        "scenario",
        "phase",
        "control_endpoint_form",
        "endpoint_matrix_sha256",
        "target_cell_id",
        "control_cell_id",
        "background_cell_id",
        "target_link",
        "control_link",
        "background_link",
        "target_flow_group_id",
        "control_flow_group_id",
        "background_flow_group_id",
        "concurrent_flow_group_ids",
        "traffic_class",
        "transport_phase_code",
        "offered_per_uav",
        "send_span_ms",
        "expected_engine_state",
        "response_policies",
        "minimum_quiet_drain_ns_by_uav",
        "start_monotonic_ns",
        "end_monotonic_ns",
        "pose_set",
        "jammer_enabled",
        "jammer_on_classification",
    }
    for index, (record, expected_id) in enumerate(zip(value, WINDOW_IDS)):
        failures.extend(exact_keys(record, expected_keys, f"causal window {index}"))
        if not isinstance(record, dict):
            continue
        shape = WINDOW_SHAPES[expected_id]
        scenario, phase, target_cell_id, control_cell_id = shape
        try:
            target_identity = matrix_flow_group_identity(
                target_cell_id,
                control_endpoint_form,
                matrix_sha256=endpoint_matrix_sha256,
            )
            control_identity = matrix_flow_group_identity(
                control_cell_id,
                control_endpoint_form,
                matrix_sha256=endpoint_matrix_sha256,
            )
            background_identity = matrix_flow_group_identity(
                BACKGROUND_CELL_ID,
                control_endpoint_form,
                matrix_sha256=endpoint_matrix_sha256,
            )
        except M4ValidationError as exc:
            failures.append(str(exc))
            continue
        expected_group_ids = [
            target_identity["flow_group_id"],
            control_identity["flow_group_id"],
            background_identity["flow_group_id"],
        ]
        start = record.get("start_monotonic_ns")
        end = record.get("end_monotonic_ns")
        plan = causal_window_plan(expected_id)
        expected_gap_ns = (
            CAUSAL_GAP_NS if expected_id in RECOVERY_DRAIN_UAV else 0
        )
        expected_start = (
            None if index == 0 else previous_end + expected_gap_ns
        )
        expected_policies = causal_response_policies(expected_id)
        expected_drains = causal_quiet_drain_map(expected_id)
        if (
            record.get("window_id") != expected_id
            or record.get("scenario") != scenario
            or record.get("phase") != phase
            or record.get("control_endpoint_form") != control_endpoint_form
            or record.get("endpoint_matrix_sha256") != endpoint_matrix_sha256
            or record.get("target_cell_id") != target_cell_id
            or record.get("control_cell_id") != control_cell_id
            or record.get("background_cell_id") != BACKGROUND_CELL_ID
            or record.get("target_link") != target_identity["directed_link_id"]
            or record.get("control_link") != control_identity["directed_link_id"]
            or record.get("background_link") != background_identity["directed_link_id"]
            or record.get("traffic_class") != "control"
            or record.get("target_flow_group_id")
            != target_identity["flow_group_id"]
            or record.get("control_flow_group_id")
            != control_identity["flow_group_id"]
            or record.get("background_flow_group_id")
            != background_identity["flow_group_id"]
            or record.get("concurrent_flow_group_ids") != expected_group_ids
            or len(set(expected_group_ids)) != 3
            or record.get("transport_phase_code") != index + 1
            or record.get("offered_per_uav") != plan["offered_per_uav"]
            or record.get("send_span_ms") != plan["send_span_ms"]
            or record.get("expected_engine_state") != "up_epoch_1"
            or record.get("response_policies") != expected_policies
            or record.get("minimum_quiet_drain_ns_by_uav") != expected_drains
            or isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(end, bool)
            or not isinstance(end, int)
            or (expected_start is not None and start != expected_start)
            or end <= start
            or end - start != plan["duration_ns"]
        ):
            failures.append(f"causal window identity/time differs: {expected_id}")
            continue
        last_outcome_offset_ns = (
            int(plan["send_span_ms"]) * 1_000_000 + OUTCOME_TIMEOUT_NS
        )
        if last_outcome_offset_ns > int(plan["duration_ns"]) - TIMEOUT_SLOT_MARGIN_NS:
            failures.append(f"causal window last outcome is not strictly bounded: {expected_id}")
        if expected_id in TIMEOUT_REQUIRED_WINDOWS:
            offered = int(plan["offered_per_uav"])
            slot_spacing_ns = (
                int(plan["send_span_ms"]) * 1_000_000 // (offered - 1)
            )
            if slot_spacing_ns < OUTCOME_TIMEOUT_NS + TIMEOUT_SLOT_MARGIN_NS:
                failures.append(
                    f"causal timeout slots permit overlapping requests: {expected_id}"
                )
        if expected_id.startswith("jammer_"):
            expected_enabled = expected_id == "jammer_on"
            if (
                record.get("pose_set") != "jammer_pose"
                or record.get("jammer_enabled") is not expected_enabled
                or record.get("jammer_on_classification") != "positive_impaired"
            ):
                failures.append(f"jammer stimulus/classification differs: {expected_id}")
        else:
            if record.get("jammer_enabled") is not False:
                failures.append(f"non-jammer window enables jammer: {expected_id}")
            if record.get("jammer_on_classification") is not None:
                failures.append(f"non-jammer window has jammer classification: {expected_id}")
            if expected_id.startswith("terrain_") and record.get("pose_set") != expected_id:
                failures.append(f"terrain pose set differs: {expected_id}")
            if expected_id.startswith("building_") and record.get("pose_set") != expected_id:
                failures.append(f"building pose set differs: {expected_id}")
            if expected_id.startswith("expiry_") and record.get("pose_set") != "terrain_good":
                failures.append(f"expiry pose set differs: {expected_id}")
        previous_end = end
        result[expected_id] = record
    if set(result) != set(WINDOW_IDS):
        failures.append("valid causal window ID set differs")
    classifications = {
        record.get("jammer_on_classification")
        for record in value
        if isinstance(record, dict) and str(record.get("window_id", "")).startswith("jammer_")
    }
    if len(classifications) != 1 or None in classifications:
        failures.append("jammer on classification was not frozen consistently before run")
    return result, failures


def validate_causal_execution_budget(
    run: Mapping[str, Any],
    windows: Mapping[str, Mapping[str, Any]],
    *,
    finalization: Mapping[str, Any] | None = None,
) -> tuple[dict[str, int], list[str]]:
    """Re-derive the complete 1200-s wrapper budget from frozen constants."""

    failures: list[str] = []
    planned_total_ns = (
        PRECONTRACT_SETUP_BUDGET_NS
        + RUNTIME_READINESS_BUDGET_NS
        + CAUSAL_MEASUREMENT_SPAN_NS
        + FINALIZATION_BUDGET_NS
        + REQUIRED_WRAPPER_RESERVE_NS
    )
    unallocated_margin_ns = WRAPPER_TIMEOUT_NS - planned_total_ns
    expected_budget = {
        "wrapper_timeout_ns": WRAPPER_TIMEOUT_NS,
        "precontract_setup_budget_ns": PRECONTRACT_SETUP_BUDGET_NS,
        "runtime_readiness_budget_ns": RUNTIME_READINESS_BUDGET_NS,
        "causal_measurement_span_ns": CAUSAL_MEASUREMENT_SPAN_NS,
        "finalization_budget_ns": FINALIZATION_BUDGET_NS,
        "required_wrapper_reserve_ns": REQUIRED_WRAPPER_RESERVE_NS,
        "planned_total_ns": planned_total_ns,
        "unallocated_margin_ns": unallocated_margin_ns,
    }
    budget = run.get("execution_budget")
    failures.extend(
        exact_keys(budget, set(expected_budget), "M4 causal execution budget")
    )
    if budget != expected_budget:
        failures.append("M4 causal execution budget differs")
    runner_start = run.get("runner_start_monotonic_ns")
    created = run.get("created_monotonic_ns")
    if (
        isinstance(runner_start, bool)
        or not isinstance(runner_start, int)
        or runner_start <= 0
        or isinstance(created, bool)
        or not isinstance(created, int)
        or created < runner_start
        or created - runner_start > PRECONTRACT_SETUP_BUDGET_NS
    ):
        failures.append("M4 causal precontract setup exceeded its 90-s budget")
    try:
        first_start = int(windows[WINDOW_IDS[0]]["start_monotonic_ns"])
        last_end = int(windows[WINDOW_IDS[-1]]["end_monotonic_ns"])
        if first_start != int(created) + RUNTIME_READINESS_BUDGET_NS:
            failures.append("M4 causal first window does not follow 30-s readiness")
        if last_end - first_start != CAUSAL_MEASUREMENT_SPAN_NS:
            failures.append("M4 causal measurement span differs")
    except (KeyError, TypeError, ValueError):
        failures.append("M4 causal budget cannot bind the window timeline")
    if (
        planned_total_ns > WRAPPER_TIMEOUT_NS
        or REQUIRED_WRAPPER_RESERVE_NS < 120_000_000_000
        or unallocated_margin_ns < 0
    ):
        failures.append("M4 causal wrapper budget has insufficient reserve")
    if finalization is not None:
        expected_finalization_keys = {
            "contract",
            "run_id",
            "runtime_id",
            "last_window_end_monotonic_ns",
            "evidence_finalized_monotonic_ns",
            "elapsed_ns",
            "budget_ns",
        }
        failures.extend(
            exact_keys(
                finalization,
                expected_finalization_keys,
                "M4 causal finalization timing",
            )
        )
        try:
            last_end = int(windows[WINDOW_IDS[-1]]["end_monotonic_ns"])
            finalized = finalization.get("evidence_finalized_monotonic_ns")
            elapsed = finalization.get("elapsed_ns")
            if (
                finalization.get("contract")
                != "ams.m4.causal-finalization-timing/v1"
                or finalization.get("run_id") != run.get("run_id")
                or finalization.get("runtime_id") != run.get("runtime_id")
                or finalization.get("last_window_end_monotonic_ns") != last_end
                or isinstance(finalized, bool)
                or not isinstance(finalized, int)
                or isinstance(elapsed, bool)
                or not isinstance(elapsed, int)
                or elapsed != finalized - last_end
                or not 0 <= elapsed <= FINALIZATION_BUDGET_NS
                or finalization.get("budget_ns") != FINALIZATION_BUDGET_NS
            ):
                failures.append("M4 causal finalization exceeded/differed from 40-s budget")
        except (KeyError, TypeError, ValueError):
            failures.append("M4 causal finalization cannot bind the window timeline")
    return expected_budget, failures


def _expected_pose_set(
    bundle: Mapping[str, Any], window: Mapping[str, Any]
) -> Mapping[str, Any]:
    scenario = str(window["scenario"])
    pose_set = str(window["pose_set"])
    if scenario in {"terrain_shadow", "building_blocked"}:
        value = bundle.get("causal_scenarios", {}).get(scenario, {}).get("pose_sets", {}).get(
            pose_set
        )
    elif scenario == "jammer_off_on_off":
        value = bundle.get("causal_scenarios", {}).get(scenario, {}).get("pose_set")
    elif scenario == "F-expiry":
        value = (
            bundle.get("causal_scenarios", {})
            .get("terrain_shadow", {})
            .get("pose_sets", {})
            .get("terrain_good")
        )
    else:
        value = None
    if not isinstance(value, Mapping):
        raise M4ValidationError(f"canonical pose set cannot be resolved: {scenario}/{pose_set}")
    return value


def validate_causal_pose_geometry(
    records: list[dict[str, Any]],
    windows: Mapping[str, Mapping[str, Any]],
    bundle: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Prove each measurement used the canonical observed Gazebo geometry."""

    failures: list[str] = []
    counts: dict[str, int] = {}
    for window_id in WINDOW_IDS:
        window = windows.get(window_id)
        if not isinstance(window, Mapping):
            failures.append(f"causal pose window is absent: {window_id}")
            continue
        start = int(window["start_monotonic_ns"])
        end = int(window["end_monotonic_ns"])
        selected = [
            record
            for record in records
            if isinstance(record.get("snapshot_monotonic_ns"), int)
            and start <= int(record["snapshot_monotonic_ns"]) < end
        ]
        counts[window_id] = len(selected)
        hosts = [int(record["snapshot_monotonic_ns"]) for record in selected]
        if (
            not selected
            or hosts[0] > start + 1_500_000_000
            or hosts[-1] < end - 1_500_000_000
            or any(
                not 0 < hosts[index] - hosts[index - 1] <= 1_500_000_000
                for index in range(1, len(hosts))
            )
        ):
            failures.append(f"causal pose coverage/gap differs: {window_id}")
            continue
        try:
            expected = _expected_pose_set(bundle, window)
            expected_entities = {"cp", "uav1", "uav2", "uav3", "uav4", "uav5", "jammer_m4"}
            if set(expected) != expected_entities:
                raise M4ValidationError("canonical pose entity set differs")
            for record in selected:
                nodes = {
                    str(item.get("node_id")): item
                    for item in record.get("nodes", [])
                    if isinstance(item, Mapping)
                }
                jammers = {
                    str(item.get("jammer_id")): item
                    for item in record.get("jammers", [])
                    if isinstance(item, Mapping)
                }
                entities = {**nodes, **jammers}
                if set(entities) != expected_entities:
                    raise M4ValidationError("observed pose entity set differs")
                for entity, expected_position in expected.items():
                    observed = entities[entity].get("position_m")
                    if (
                        not isinstance(expected_position, list)
                        or len(expected_position) != 3
                        or not isinstance(observed, list)
                        or len(observed) != 3
                        or any(not finite_number(value) for value in [*expected_position, *observed])
                        or math.dist(
                            [float(value) for value in observed],
                            [float(value) for value in expected_position],
                        )
                        > 1.0
                    ):
                        raise M4ValidationError(
                            f"observed {entity} is outside canonical 1-m pose tolerance"
                        )
                if jammers["jammer_m4"].get("enabled") is not bool(
                    window["jammer_enabled"]
                ):
                    raise M4ValidationError("observed jammer enable state differs")
        except (KeyError, TypeError, ValueError, M4ValidationError) as exc:
            failures.append(f"causal observed geometry differs {window_id}: {exc}")
    return {"window_snapshot_counts": counts, "validated_window_count": len(counts)}, failures


def _transaction_index(
    records: list[dict[str, Any]], run: Mapping[str, Any]
) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]], list[str]]:
    failures: list[str] = []
    offers: dict[str, dict[str, Any]] = {}
    outcomes: dict[str, list[dict[str, Any]]] = defaultdict(list)
    previous = 0
    for number, record in enumerate(records, start=1):
        sequence = record.get("event_sequence")
        if (
            record.get("schema") != TRANSACTION_SCHEMA
            or record.get("run_id") != run.get("run_id")
            or record.get("runtime_id") != run.get("runtime_id")
            or record.get("run_nonce") != run.get("run_nonce")
            or isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence != previous + 1
        ):
            failures.append(f"actual endpoint transaction {number} identity/order differs")
            continue
        previous = sequence
        event = record.get("event")
        transaction_id = record.get("transaction_id")
        if not isinstance(transaction_id, str) or not transaction_id:
            failures.append(f"actual endpoint transaction {number} lacks transaction_id")
            continue
        if event == "gcs_valid_command_offer":
            digest = record.get("request_transport_payload_sha256")
            if (
                transaction_id in offers
                or not isinstance(digest, str)
                or HEX64.fullmatch(digest) is None
                or record.get("producer_role") != "gcs_endpoint_probe"
                or record.get("directed_link") not in {
                    "cp>uav1",
                    "cp>uav2",
                    "cp>uav3",
                    "cp>uav4",
                    "cp>uav5",
                }
                or record.get("traffic_class") != "control"
            ):
                failures.append(f"actual command offer {number} fields differ")
            else:
                offers[transaction_id] = record
        else:
            outcomes[transaction_id].append(record)
    for transaction_id, records_for_transaction in outcomes.items():
        if transaction_id not in offers:
            failures.append(f"endpoint outcome references unknown offer: {transaction_id}")
        if len({record.get("event") for record in records_for_transaction}) != len(
            records_for_transaction
        ):
            failures.append(f"endpoint transaction has duplicate outcome type: {transaction_id}")
    return offers, outcomes, failures


def normalize_actual_control_transactions(
    records: list[dict[str, Any]], run: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Derive causal transaction records only from the Q3 actual-control audit.

    No M4 process is allowed to manufacture a second ACK/heartbeat transcript.
    The normalized view is deterministic validator state: requests come from
    the real GCS probe and successful outcomes retain the actual vehicle sysid,
    component, received timestamp, and transport datagram hash.
    """

    failures: list[str] = []
    normalized: list[dict[str, Any]] = []
    offers: dict[str, dict[str, Any]] = {}
    previous_raw_sequence = 0

    def append(event: str, source: Mapping[str, Any], **fields: Any) -> None:
        normalized.append(
            {
                "schema": TRANSACTION_SCHEMA,
                "run_id": run.get("run_id"),
                "runtime_id": run.get("runtime_id"),
                "run_nonce": run.get("run_nonce"),
                "event_sequence": len(normalized) + 1,
                "event": event,
                "transaction_id": source.get("transaction_id"),
                **fields,
            }
        )

    for number, record in enumerate(records, start=1):
        raw_sequence = record.get("event_sequence")
        if (
            record.get("schema") != "ams.actual-sitl.control-event/v1"
            or record.get("run_id") != run.get("run_id")
            or record.get("runtime_id") != run.get("runtime_id")
            or record.get("run_nonce") != run.get("run_nonce")
            or record.get("profile") != "m4_causality"
            or record.get("role_subject") != "gcs_control_probe"
            or isinstance(raw_sequence, bool)
            or not isinstance(raw_sequence, int)
            or raw_sequence != previous_raw_sequence + 1
        ):
            failures.append(f"raw actual-control causal event {number} identity/order differs")
            continue
        previous_raw_sequence = raw_sequence
        if record.get("event") == "real_command_offered":
            transaction_id = record.get("transaction_id")
            uav = record.get("uav")
            digest = record.get("command_frame_sha256")
            if (
                not isinstance(transaction_id, str)
                or not transaction_id
                or transaction_id in offers
                or isinstance(uav, bool)
                or not isinstance(uav, int)
                or not 1 <= uav <= 5
                or not isinstance(digest, str)
                or HEX64.fullmatch(digest) is None
                or record.get("endpoint_form")
                != "actual_sitl_mavproxy_udp_tail"
                or record.get("cell_id") != f"uav{uav}.control.downlink"
                or record.get("flow_id") != f"uav{uav}.control.downlink"
            ):
                failures.append(f"raw actual-control causal offer {number} differs")
                continue
            offers[transaction_id] = record
            append(
                "gcs_valid_command_offer",
                record,
                window_id=record.get("window_id"),
                uav=f"uav{uav}",
                producer_role="gcs_endpoint_probe",
                host_monotonic_ns=record.get("sent_monotonic_ns"),
                directed_link=f"cp>uav{uav}",
                traffic_class="control",
                request_transport_payload_sha256=digest,
                flow_group_id=record.get("flow_group_id"),
                matrix_cell_id=record.get("cell_id"),
                endpoint_form=record.get("endpoint_form"),
                direction="downlink",
                ordinal_send_slot=record.get("ordinal_send_slot"),
            )
        elif record.get("event") == "transaction_result":
            transaction_id = record.get("transaction_id")
            offer = offers.get(str(transaction_id))
            if not isinstance(offer, dict):
                failures.append(
                    f"raw actual-control causal result {number} references unknown offer"
                )
                continue
            if (
                record.get("record_nonce") != offer.get("record_nonce")
                or record.get("command_frame_sha256")
                != offer.get("command_frame_sha256")
                or record.get("flow_group_id") != offer.get("flow_group_id")
                or record.get("ordinal_send_slot") != offer.get("ordinal_send_slot")
                or record.get("window_id") != offer.get("window_id")
            ):
                failures.append(f"raw actual-control causal result {number} binding differs")
                continue
            if record.get("success") is not True:
                if (
                    record.get("timed_out") is not True
                    or record.get("ack") is not None
                    or record.get("requested_telemetry") is not None
                ):
                    failures.append(
                        f"raw actual-control causal loss {number} is not an exact timeout"
                    )
                continue
            ack = record.get("ack")
            telemetry = record.get("requested_telemetry")
            uav = offer.get("uav")
            if (
                not isinstance(ack, dict)
                or not isinstance(telemetry, dict)
                or ack.get("source_system") != uav
                or ack.get("source_component") != 1
                or ack.get("message_type") != "COMMAND_ACK"
                or telemetry.get("source_system") != uav
                or telemetry.get("source_component") != 1
                or telemetry.get("message_type") != "AUTOPILOT_VERSION"
                or not isinstance(ack.get("received_monotonic_ns"), int)
                or not isinstance(telemetry.get("received_monotonic_ns"), int)
                or not isinstance(ack.get("transport_payload_sha256"), str)
                or HEX64.fullmatch(ack["transport_payload_sha256"]) is None
            ):
                failures.append(
                    f"raw actual-control causal outcome {number} is not real vehicle evidence"
                )
                continue
            common = {
                "window_id": offer.get("window_id"),
                "uav": f"uav{uav}",
                "producer_role": "arducopter",
                "directed_link": f"cp>uav{uav}",
                "traffic_class": "control",
                "request_transport_payload_sha256": offer.get(
                    "command_frame_sha256"
                ),
                "flow_group_id": offer.get("flow_group_id"),
                "matrix_cell_id": offer.get("cell_id"),
                "endpoint_form": offer.get("endpoint_form"),
                "direction": "downlink",
                "ordinal_send_slot": offer.get("ordinal_send_slot"),
            }
            append(
                "ardupilot_command_ack",
                record,
                **common,
                host_monotonic_ns=ack["received_monotonic_ns"],
                response_transport_payload_sha256=ack[
                    "transport_payload_sha256"
                ],
                source_system=ack.get("source_system"),
                source_component=ack.get("source_component"),
            )
            append(
                "requested_telemetry",
                record,
                **common,
                host_monotonic_ns=telemetry["received_monotonic_ns"],
                response_transport_payload_sha256=telemetry.get(
                    "transport_payload_sha256"
                ),
                source_system=telemetry.get("source_system"),
                source_component=telemetry.get("source_component"),
            )
    if not offers:
        failures.append("raw actual-control causal audit has no command offers")
    return normalized, failures


def _packet_indexes(
    packet_records: list[dict[str, Any]],
) -> tuple[
    dict[tuple[str, str, str], list[dict[str, Any]]],
    dict[tuple[str, str, str], list[dict[str, Any]]],
    list[str],
]:
    failures: list[str] = []
    decisions: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    downstream: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    previous = 0
    for number, event in enumerate(packet_records, start=1):
        sequence = event.get("event_sequence")
        if (
            event.get("schema") != "ams.ns3.packet_event/v1"
            or isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence <= previous
        ):
            failures.append(f"packet event {number} identity/order differs")
            continue
        previous = sequence
        digest = event.get("transport_payload_sha256")
        link = event.get("directed_link")
        traffic_class = event.get("traffic_class")
        if not isinstance(digest, str) or HEX64.fullmatch(digest) is None:
            continue
        key = (str(link), str(traffic_class), digest)
        if event.get("event") == "egress":
            downstream[key].append(event)
        if event.get("event") not in {"enqueue", "drop"}:
            continue
        decisions[key].append(event)
    for records in (*decisions.values(), *downstream.values()):
        records.sort(
            key=lambda event: (
                int(event.get("host_monotonic_ns", -1)),
                int(event.get("event_sequence", -1)),
            )
        )
    return dict(decisions), dict(downstream), failures


def _consume_causal_packet_occurrence(
    records: list[dict[str, Any]],
    cursors: dict[tuple[Any, ...], int],
    *,
    cursor_key: tuple[Any, ...],
    lower_ns: int,
    upper_ns: int,
) -> dict[str, Any] | None:
    """Consume one ordered packet occurrence without SHA uniqueness."""

    cursor = cursors.get(cursor_key, 0)
    while cursor < len(records):
        timestamp = records[cursor].get("host_monotonic_ns")
        if isinstance(timestamp, int) and not isinstance(timestamp, bool) and timestamp >= lower_ns:
            break
        cursor += 1
    if cursor >= len(records):
        cursors[cursor_key] = cursor
        return None
    timestamp = records[cursor].get("host_monotonic_ns")
    if not isinstance(timestamp, int) or timestamp >= upper_ns:
        cursors[cursor_key] = cursor
        return None
    cursors[cursor_key] = cursor + 1
    return records[cursor]


def derive_causal_window_metrics(
    windows: Mapping[str, Mapping[str, Any]],
    *,
    packet_records: list[dict[str, Any]],
    states_by_hash: Mapping[str, Mapping[str, Any]],
    transaction_records: list[dict[str, Any]],
    run: Mapping[str, Any],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Derive packet/physical/outcome metrics for every fixed causal window."""

    failures: list[str] = []
    offers, outcomes, transaction_failures = _transaction_index(transaction_records, run)
    failures.extend(transaction_failures)
    decisions, downstream, packet_failures = _packet_indexes(packet_records)
    failures.extend(packet_failures)
    decision_cursors: dict[tuple[Any, ...], int] = {}
    downstream_cursors: dict[tuple[Any, ...], int] = {}
    metrics: dict[str, dict[str, Any]] = {}
    assignment: dict[str, str] = {}
    for window_id in WINDOW_IDS:
        window = windows.get(window_id)
        if not isinstance(window, Mapping):
            failures.append(f"causal window is absent: {window_id}")
            continue
        start = int(window["start_monotonic_ns"])
        end = int(window["end_monotonic_ns"])
        groups: dict[str, dict[str, Any]] = {}
        for role, link in (
            ("target", str(window["target_link"])),
            ("control", str(window["control_link"])),
            ("background", str(window["background_link"])),
        ):
            selected = sorted(
                [
                (transaction_id, offer)
                for transaction_id, offer in offers.items()
                if offer.get("directed_link") == link
                and offer.get("traffic_class") == window["traffic_class"]
                and isinstance(offer.get("host_monotonic_ns"), int)
                and start <= int(offer["host_monotonic_ns"]) < end
                ],
                key=lambda item: (
                    int(item[1]["host_monotonic_ns"]),
                    int(item[1].get("ordinal_send_slot", 0)),
                    item[0],
                ),
            )
            expected_flow_group = str(window[f"{role}_flow_group_id"])
            expected_cell_id = str(window[f"{role}_cell_id"])
            try:
                expected_identity = matrix_flow_group_identity(
                    expected_cell_id,
                    str(window["control_endpoint_form"]),
                    matrix_sha256=str(window["endpoint_matrix_sha256"]),
                )
            except M4ValidationError as exc:
                failures.append(f"{window_id}/{role} flow identity cannot be resolved: {exc}")
                continue
            sinr: list[float] = []
            js: list[float] = []
            tiers: list[int] = []
            state_ages: list[float] = []
            latencies: list[float] = []
            delivered = 0
            unavailable = 0
            paired_samples: dict[int, dict[str, Any]] = {}
            for transaction_id, offer in selected:
                if transaction_id in assignment:
                    failures.append(
                        f"transaction assigned to two causal windows: {transaction_id}"
                    )
                assignment[transaction_id] = window_id
                ordinal = offer.get("ordinal_send_slot")
                if (
                    offer.get("flow_group_id") != expected_flow_group
                    or offer.get("matrix_cell_id") != expected_cell_id
                    or offer.get("endpoint_form")
                    != expected_identity["endpoint_form"]
                    or offer.get("direction") != expected_identity["direction"]
                    or isinstance(ordinal, bool)
                    or not isinstance(ordinal, int)
                    or ordinal < 1
                    or ordinal in paired_samples
                ):
                    failures.append(
                        f"{window_id}/{role} flow-group/ordinal pairing differs: {transaction_id}"
                    )
                    continue
                digest = str(offer["request_transport_payload_sha256"])
                key = (link, str(window["traffic_class"]), digest)
                offer_ns = int(offer["host_monotonic_ns"])
                decision = _consume_causal_packet_occurrence(
                    decisions.get(key, []),
                    decision_cursors,
                    cursor_key=("decision", *key),
                    lower_ns=offer_ns,
                    upper_ns=end,
                )
                if decision is None:
                    failures.append(
                        f"{window_id}/{role} offer lacks ns-3 radio decision: {transaction_id}"
                    )
                    continue
                paired_sample: dict[str, Any] = {
                    "transaction_id": transaction_id,
                    "transport_payload_sha256": digest,
                    "offer_monotonic_ns": int(offer["host_monotonic_ns"]),
                    "delivery": 0.0,
                    "sinr_db": None,
                    "js_db": None,
                    "service_tier_index": None,
                }
                status = decision.get("radio_state_status")
                if status != "fresh":
                    unavailable += 1
                else:
                    state = states_by_hash.get(str(decision.get("radio_state_sha256")))
                    physical = state.get("physical") if isinstance(state, Mapping) else None
                    effects = state.get("effects") if isinstance(state, Mapping) else None
                    if not isinstance(physical, Mapping) or not isinstance(effects, Mapping):
                        failures.append(
                            f"{window_id}/{role} decision has no exact applied state"
                        )
                    else:
                        for source, target, label in (
                            (physical.get("sinr_db"), sinr, "SINR"),
                            (physical.get("js_db"), js, "J/S"),
                        ):
                            if finite_number(source):
                                target.append(float(source))
                            else:
                                failures.append(f"{window_id}/{role} {label} is non-finite")
                        paired_sample["sinr_db"] = (
                            float(physical["sinr_db"])
                            if finite_number(physical.get("sinr_db"))
                            else None
                        )
                        paired_sample["js_db"] = (
                            float(physical["js_db"])
                            if finite_number(physical.get("js_db"))
                            else None
                        )
                        tier = effects.get("service_rate_bps")
                        if tier not in SERVICE_TIERS:
                            failures.append(f"{window_id}/{role} service tier is invalid")
                        else:
                            tiers.append(int(tier))
                            paired_sample["service_tier_index"] = _tier_index(int(tier))
                        applied = state.get("adapter_applied_monotonic_ns")
                        host = decision.get("host_monotonic_ns")
                        if (
                            isinstance(applied, int)
                            and not isinstance(applied, bool)
                            and isinstance(host, int)
                            and not isinstance(host, bool)
                            and 0 <= host - applied < 2_000_000_000
                        ):
                            state_ages.append(float(host - applied))
                        else:
                            failures.append(f"{window_id}/{role} state age is invalid")
                outcome_records = outcomes.get(transaction_id, [])
                ack = [item for item in outcome_records if item.get("event") == "ardupilot_command_ack"]
                telemetry = [item for item in outcome_records if item.get("event") == "requested_telemetry"]
                ack_ns = ack[0].get("host_monotonic_ns") if len(ack) == 1 else None
                egress = (
                    _consume_causal_packet_occurrence(
                        downstream.get(key, []),
                        downstream_cursors,
                        cursor_key=("egress", *key),
                        lower_ns=int(decision.get("host_monotonic_ns", offer_ns)),
                        upper_ns=int(ack_ns) + 1 if isinstance(ack_ns, int) else end,
                    )
                    if len(ack) == 1 and len(telemetry) == 1
                    else None
                )
                complete = len(ack) == 1 and len(telemetry) == 1 and egress is not None
                if complete:
                    if (
                        not isinstance(ack_ns, int)
                        or isinstance(ack_ns, bool)
                        or not offer_ns < ack_ns <= offer_ns + 3_000_000_000
                        or ack[0].get("producer_role") != "arducopter"
                        or telemetry[0].get("producer_role") != "arducopter"
                    ):
                        failures.append(
                            f"{window_id}/{role} ACK/telemetry outcome is invalid: {transaction_id}"
                        )
                    else:
                        delivered += 1
                        paired_sample["delivery"] = 1.0
                        latencies.append(float(ack_ns - offer_ns))
                elif ack or telemetry:
                    failures.append(
                        f"{window_id}/{role} has partial/uncorrelated actual outcome: {transaction_id}"
                    )
                paired_samples[int(ordinal)] = paired_sample
            offered_count = len(selected)
            if set(paired_samples) != set(range(1, offered_count + 1)):
                failures.append(f"{window_id}/{role} ordinal send slots are not exact 1..N")
            ratio = delivered / offered_count if offered_count else 0.0
            group: dict[str, Any] = {
                "offered_unique": offered_count,
                "delivered_unique": delivered,
                "delivery_ratio": ratio,
                "unavailable_decisions": unavailable,
                "fresh_physical_samples": len(sinr),
                "median_sinr_db": None,
                "median_js_db": None,
                "median_service_tier_bps": None,
                "state_age_p95_ns": None,
                "delivered_latency_p95_ns": None,
                "delivered_jitter_ns": None,
                "flow_group_id": expected_flow_group,
                "paired_samples": paired_samples,
            }
            if sinr:
                group["median_sinr_db"] = _median(sinr, f"{window_id}/{role} SINR")
                group["median_js_db"] = _median(js, f"{window_id}/{role} J/S")
                group["median_service_tier_bps"] = int(_nearest_rank([float(item) for item in tiers], 0.5))
                group["state_age_p95_ns"] = _nearest_rank(state_ages, 0.95)
            if latencies:
                group["delivered_latency_p95_ns"] = _nearest_rank(latencies, 0.95)
                group["delivered_jitter_ns"] = (
                    0.0
                    if len(latencies) == 1
                    else sum(
                        abs(right - left) for left, right in zip(latencies, latencies[1:])
                    )
                    / (len(latencies) - 1)
                )
            groups[role] = group
        if set(groups) == {"target", "control", "background"}:
            ordinal_sets = [set(groups[role]["paired_samples"]) for role in groups]
            common_ordinals = set.intersection(*ordinal_sets)
            concurrent_slots = []
            maximum_skew = 0
            for ordinal in sorted(common_ordinals):
                timestamps = [
                    int(groups[role]["paired_samples"][ordinal]["offer_monotonic_ns"])
                    for role in ("target", "control", "background")
                ]
                skew = max(timestamps) - min(timestamps)
                maximum_skew = max(maximum_skew, skew)
                if skew <= 100_000_000:
                    concurrent_slots.append(ordinal)
            groups["concurrency"] = {
                "group_count": 3,
                "concurrent_ordinal_slots": len(concurrent_slots),
                "maximum_offer_skew_ns": maximum_skew,
            }
        metrics[window_id] = groups
    return metrics, failures


def _positive(group: Mapping[str, Any], *, minimum: int = 100) -> bool:
    return (
        group.get("offered_unique", 0) >= minimum
        and group.get("delivery_ratio", 0.0) >= 0.95
        and group.get("fresh_physical_samples", 0) >= minimum
        and finite_number(group.get("median_sinr_db"))
        and finite_number(group.get("delivered_latency_p95_ns"))
        and finite_number(group.get("delivered_jitter_ns"))
    )


def validate_concurrent_flow_groups(
    windows: Mapping[str, Mapping[str, Any]],
    metrics: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    """Require three distinct raw flow groups in every causal window.

    The third stream is an independently identified UAV4 control downlink.  It
    is never aliased to the target/control role and must share at least 100
    ordinal send slots (20 in the deliberately short expiry interval), within
    100 ms per ordinal, with the other two groups.
    """

    failures: list[str] = []
    for window_id in WINDOW_IDS:
        window = windows.get(window_id)
        groups = metrics.get(window_id)
        if not isinstance(window, Mapping) or not isinstance(groups, Mapping):
            failures.append(f"three-group causal evidence is absent: {window_id}")
            continue
        if set(groups) != {"target", "control", "background", "concurrency"}:
            failures.append(f"three-group raw metric roles differ: {window_id}")
            continue
        expected_ids = [
            window.get("target_flow_group_id"),
            window.get("control_flow_group_id"),
            window.get("background_flow_group_id"),
        ]
        observed_ids = [groups[role].get("flow_group_id") for role in (
            "target",
            "control",
            "background",
        )]
        if (
            len(set(expected_ids)) != 3
            or observed_ids != expected_ids
            or not _positive(groups["control"])
            or not _positive(groups["background"])
        ):
            failures.append(f"three distinct positive causal streams differ: {window_id}")
        concurrency = groups["concurrency"]
        minimum_slots = 20 if window_id == "expiry_unavailable" else 100
        if (
            not isinstance(concurrency, Mapping)
            or set(concurrency)
            != {"group_count", "concurrent_ordinal_slots", "maximum_offer_skew_ns"}
            or concurrency.get("group_count") != 3
            or isinstance(concurrency.get("concurrent_ordinal_slots"), bool)
            or not isinstance(concurrency.get("concurrent_ordinal_slots"), int)
            or concurrency["concurrent_ordinal_slots"] < minimum_slots
            or isinstance(concurrency.get("maximum_offer_skew_ns"), bool)
            or not isinstance(concurrency.get("maximum_offer_skew_ns"), int)
            or not 0 <= concurrency["maximum_offer_skew_ns"] <= 100_000_000
        ):
            failures.append(f"three-group concurrent send-slot proof differs: {window_id}")
    return failures


def _locality(
    reference: Mapping[str, Any], observed: Mapping[str, Any]
) -> bool:
    try:
        return (
            abs(float(observed["median_sinr_db"]) - float(reference["median_sinr_db"]))
            <= 1.0
            and abs(float(observed["delivery_ratio"]) - float(reference["delivery_ratio"]))
            <= 0.05 + 1e-12
            and abs(
                _tier_index(int(observed["median_service_tier_bps"]))
                - _tier_index(int(reference["median_service_tier_bps"]))
            )
            <= 1
        )
    except (KeyError, TypeError, ValueError, M4ValidationError):
        return False


def validate_causal_effects(
    windows: Mapping[str, Mapping[str, Any]],
    metrics: Mapping[str, Mapping[str, Mapping[str, Any]]],
) -> list[str]:
    """Apply only the quantitative causal gates frozen in plan v3."""

    failures: list[str] = validate_concurrent_flow_groups(windows, metrics)
    for prefix in ("terrain", "building"):
        good = metrics.get(f"{prefix}_good", {})
        down = metrics.get(f"{prefix}_down", {})
        recovery = metrics.get(f"{prefix}_recovery", {})
        for role in ("target", "control"):
            if not _positive(good.get(role, {})):
                failures.append(f"{prefix} good {role} is not >=100 and >=95% positive")
            if not _positive(recovery.get(role, {})):
                failures.append(f"{prefix} recovery {role} is not >=100 and >=95% positive")
        down_target = down.get("target", {})
        down_control = down.get("control", {})
        if (
            down_target.get("offered_unique", 0) < 100
            or down_target.get("delivery_ratio") != 0.0
            or down_target.get("fresh_physical_samples", 0) < 100
        ):
            failures.append(f"{prefix} down target is not exact zero delivery with 100 physical samples")
        if not _positive(down_control):
            failures.append(f"{prefix} down control is not >=100 and >=95% positive")
        try:
            if float(good["target"]["median_sinr_db"]) - float(
                down_target["median_sinr_db"]
            ) < 3.0:
                failures.append(f"{prefix} target SINR did not decrease by 3 dB")
        except (KeyError, TypeError, ValueError):
            failures.append(f"{prefix} target SINR comparison is unavailable")
        if not _locality(good.get("control", {}), down_control):
            failures.append(f"{prefix} down control locality exceeds 1dB/5pp/one tier")
        if not _locality(good.get("target", {}), recovery.get("target", {})):
            failures.append(f"{prefix} target recovery exceeds 1dB/5pp/one tier")
        if not _locality(good.get("control", {}), recovery.get("control", {})):
            failures.append(f"{prefix} control recovery exceeds 1dB/5pp/one tier")
        if not _locality(good.get("background", {}), down.get("background", {})):
            failures.append(f"{prefix} down background locality exceeds 1dB/5pp/one tier")
        if not _locality(good.get("background", {}), recovery.get("background", {})):
            failures.append(f"{prefix} background recovery exceeds 1dB/5pp/one tier")

    off1 = metrics.get("jammer_off_1", {})
    on = metrics.get("jammer_on", {})
    off2 = metrics.get("jammer_off_2", {})
    for window_name, groups in (("off-1", off1), ("off-2", off2)):
        for role in ("target", "control"):
            if not _positive(groups.get(role, {})):
                failures.append(f"jammer {window_name} {role} is not >=100 and >=95% positive")
    on_target = on.get("target", {})
    on_control = on.get("control", {})
    if on_target.get("offered_unique", 0) < 100 or on_control.get("offered_unique", 0) < 100:
        failures.append("jammer on target/control offers are below 100")
    classification = windows.get("jammer_on", {}).get("jammer_on_classification")
    if classification == "positive_impaired":
        if (
            on_target.get("delivered_unique", 0) < 20
            or not finite_number(on_target.get("delivered_latency_p95_ns"))
            or not finite_number(on_target.get("delivered_jitter_ns"))
        ):
            failures.append("positive-impaired jammer on lacks 20 real delivered outcomes")
    elif classification == "expected_down":
        if on_target.get("delivered_unique") != 0 or on_target.get("delivery_ratio") != 0.0:
            failures.append("expected-down jammer on target delivery is not exactly zero")
    else:
        failures.append("jammer on classification is invalid")
    try:
        physical_delta = (
            float(off1["target"]["median_sinr_db"])
            - float(on_target["median_sinr_db"])
            >= 3.0
            or float(on_target["median_js_db"])
            - float(off1["target"]["median_js_db"])
            >= 3.0
        )
        tier_delta = (
            _tier_index(int(off1["target"]["median_service_tier_bps"]))
            - _tier_index(int(on_target["median_service_tier_bps"]))
            >= 1
        )
        delivery_delta = (
            float(off1["target"]["delivery_ratio"])
            - float(on_target["delivery_ratio"])
            >= 0.10 - 1e-12
        )
        if not physical_delta:
            failures.append("jammer on has neither 3 dB SINR nor J/S effect")
        if not (tier_delta or delivery_delta):
            failures.append("jammer on has neither 10pp delivery nor one-tier effect")
    except (KeyError, TypeError, ValueError, M4ValidationError):
        failures.append("jammer on comparison is unavailable")
    if not _positive(on_control) or not _locality(off1.get("control", {}), on_control):
        failures.append("jammer on control link is not positive/local")
    if not _locality(off1.get("background", {}), on.get("background", {})):
        failures.append("jammer on background link is not local")
    for role in ("target", "control", "background"):
        if not _locality(off1.get(role, {}), off2.get(role, {})):
            failures.append(f"jammer off-2 {role} recovery exceeds 1dB/5pp/one tier")

    expiry_down = metrics.get("expiry_unavailable", {}).get("target", {})
    expiry_recovery = metrics.get("expiry_recovery", {}).get("target", {})
    if (
        expiry_down.get("offered_unique", 0) < 20
        or expiry_down.get("delivered_unique") != 0
        or expiry_down.get("delivery_ratio") != 0.0
        or expiry_down.get("unavailable_decisions", 0) < 20
    ):
        failures.append("F-expiry unavailable interval is not >=20 exact fail-closed losses")
    if not _positive(expiry_recovery):
        failures.append("F-expiry recovery is not >=100 and >=95% positive")
    for role in ("control", "background"):
        if not _locality(
            metrics.get("expiry_unavailable", {}).get(role, {}),
            metrics.get("expiry_recovery", {}).get(role, {}),
        ):
            failures.append(f"F-expiry {role} locality/recovery exceeds 1dB/5pp/one tier")
    return failures


def _paired_bootstrap(
    metrics: Mapping[str, Mapping[str, Mapping[str, Any]]],
    *,
    reference_window: str,
    observed_window: str,
    role: str,
    field: str,
    seed: int,
    resamples: int,
    statistic: str = "median",
) -> dict[str, Any]:
    reference = metrics[reference_window][role]
    observed = metrics[observed_window][role]
    if reference.get("flow_group_id") != observed.get("flow_group_id"):
        raise M4ValidationError(
            f"paired flow_group differs: {reference_window}/{observed_window}/{role}"
        )
    left = reference.get("paired_samples")
    right = observed.get("paired_samples")
    if not isinstance(left, dict) or not isinstance(right, dict):
        raise M4ValidationError("paired causal sample map is absent")
    left_keys = {int(value) for value in left}
    right_keys = {int(value) for value in right}
    if left_keys != right_keys or len(left_keys) < 100:
        raise M4ValidationError(
            f"paired ordinal set differs/is below 100: {reference_window}/{observed_window}/{role}"
        )
    values: list[float] = []
    for ordinal in sorted(left_keys):
        left_value = left[ordinal].get(field)
        right_value = right[ordinal].get(field)
        if not finite_number(left_value) or not finite_number(right_value):
            raise M4ValidationError(
                f"paired {field} sample is missing: {reference_window}/{observed_window}/{role}/{ordinal}"
            )
        values.append(float(left_value) - float(right_value))
    if statistic == "median":
        aggregate = float(statistics.median(values))
        reducer = statistics.median
    elif statistic == "mean":
        aggregate = float(statistics.fmean(values))
        reducer = statistics.fmean
    else:
        raise M4ValidationError("paired bootstrap statistic is unknown")
    material = (
        f"{seed}|{reference_window}|{observed_window}|{role}|{field}|{statistic}"
    ).encode()
    generator = random.Random(int.from_bytes(hashlib.sha256(material).digest()[:8], "big"))
    size = len(values)
    distribution = [
        float(reducer([values[generator.randrange(size)] for _ in range(size)]))
        for _ in range(resamples)
    ]
    distribution.sort()
    lower_index = max(0, math.floor((resamples - 1) * 0.025))
    upper_index = min(resamples - 1, math.ceil((resamples - 1) * 0.975))
    return {
        "reference_window": reference_window,
        "observed_window": observed_window,
        "role": role,
        "field": field,
        "statistic": statistic,
        "pair_count": size,
        "aggregate_delta_reference_minus_observed": aggregate,
        "confidence_level": 0.95,
        "lower_bound": distribution[lower_index],
        "upper_bound": distribution[upper_index],
        "resamples": resamples,
        "seed": seed,
        "pairing": "flow_group_id+ordinal_send_slot",
    }


def validate_paired_causality(
    metrics: Mapping[str, Mapping[str, Mapping[str, Any]]],
    *,
    seed: int,
    resamples: int,
) -> tuple[dict[str, Any], list[str]]:
    """Apply the predeclared 10k paired-bootstrap conservative-bound gates."""

    failures: list[str] = []
    details: dict[str, Any] = {}
    if (
        isinstance(seed, bool)
        or not isinstance(seed, int)
        or not 0 <= seed <= 2**32 - 1
        or isinstance(resamples, bool)
        or not isinstance(resamples, int)
        or not 10_000 <= resamples <= 100_000
    ):
        return {}, ["paired bootstrap seed/resample bound differs"]

    def compare(
        name: str,
        reference: str,
        observed: str,
        role: str,
        field: str,
        *,
        statistic: str = "median",
    ) -> dict[str, Any]:
        value = _paired_bootstrap(
            metrics,
            reference_window=reference,
            observed_window=observed,
            role=role,
            field=field,
            statistic=statistic,
            seed=seed,
            resamples=resamples,
        )
        details[name] = value
        return value

    try:
        for prefix in ("terrain", "building"):
            deterioration = compare(
                f"{prefix}_target_sinr_deterioration",
                f"{prefix}_good",
                f"{prefix}_down",
                "target",
                "sinr_db",
            )
            if (
                deterioration["aggregate_delta_reference_minus_observed"] < 3.0
                or deterioration["lower_bound"] < 3.0
            ):
                failures.append(
                    f"{prefix} paired target SINR deterioration lacks conservative 3 dB bound"
                )
            for role in ("target", "control", "background"):
                recovery_sinr = compare(
                    f"{prefix}_{role}_recovery_sinr",
                    f"{prefix}_good",
                    f"{prefix}_recovery",
                    role,
                    "sinr_db",
                )
                recovery_delivery = compare(
                    f"{prefix}_{role}_recovery_delivery",
                    f"{prefix}_good",
                    f"{prefix}_recovery",
                    role,
                    "delivery",
                    statistic="mean",
                )
                recovery_tier = compare(
                    f"{prefix}_{role}_recovery_tier",
                    f"{prefix}_good",
                    f"{prefix}_recovery",
                    role,
                    "service_tier_index",
                )
                if not (
                    -1.0 <= recovery_sinr["lower_bound"]
                    and recovery_sinr["upper_bound"] <= 1.0
                    and -0.05 <= recovery_delivery["lower_bound"]
                    and recovery_delivery["upper_bound"] <= 0.05
                    and -1.0 <= recovery_tier["lower_bound"]
                    and recovery_tier["upper_bound"] <= 1.0
                ):
                    failures.append(
                        f"{prefix} paired {role} recovery exceeds conservative locality bound"
                    )
            for role in ("control", "background"):
                for field, tolerance, statistic in (
                    ("sinr_db", 1.0, "median"),
                    ("delivery", 0.05, "mean"),
                    ("service_tier_index", 1.0, "median"),
                ):
                    locality = compare(
                        f"{prefix}_{role}_down_{field}",
                        f"{prefix}_good",
                        f"{prefix}_down",
                        role,
                        field,
                        statistic=statistic,
                    )
                    if not (
                        -tolerance <= locality["lower_bound"]
                        and locality["upper_bound"] <= tolerance
                    ):
                        failures.append(
                            f"{prefix} paired {role} down {field} exceeds conservative locality"
                        )

        jammer_sinr = compare(
            "jammer_target_sinr_deterioration",
            "jammer_off_1",
            "jammer_on",
            "target",
            "sinr_db",
        )
        # J/S worsening is observed-reference, hence negate the standard
        # reference-minus-observed interval.
        jammer_js = compare(
            "jammer_target_js_change",
            "jammer_off_1",
            "jammer_on",
            "target",
            "js_db",
        )
        physical_bound = (
            jammer_sinr["aggregate_delta_reference_minus_observed"] >= 3.0
            and jammer_sinr["lower_bound"] >= 3.0
        ) or (
            -jammer_js["aggregate_delta_reference_minus_observed"] >= 3.0
            and -jammer_js["upper_bound"] >= 3.0
        )
        if not physical_bound:
            failures.append("jammer paired physical effect lacks conservative 3 dB bound")
        jammer_delivery = compare(
            "jammer_target_delivery_deterioration",
            "jammer_off_1",
            "jammer_on",
            "target",
            "delivery",
            statistic="mean",
        )
        jammer_tier = compare(
            "jammer_target_tier_deterioration",
            "jammer_off_1",
            "jammer_on",
            "target",
            "service_tier_index",
        )
        if not (
            (
                jammer_delivery["aggregate_delta_reference_minus_observed"] >= 0.10
                and jammer_delivery["lower_bound"] >= 0.10
            )
            or (
                jammer_tier["aggregate_delta_reference_minus_observed"] >= 1.0
                and jammer_tier["lower_bound"] >= 1.0
            )
        ):
            failures.append(
                "jammer paired packet/tier effect lacks conservative 10pp/one-tier bound"
            )
        for role in ("target", "control", "background"):
            for field, tolerance, statistic in (
                ("sinr_db", 1.0, "median"),
                ("delivery", 0.05, "mean"),
                ("service_tier_index", 1.0, "median"),
            ):
                recovery = compare(
                    f"jammer_{role}_off2_{field}",
                    "jammer_off_1",
                    "jammer_off_2",
                    role,
                    field,
                    statistic=statistic,
                )
                if not (
                    -tolerance <= recovery["lower_bound"]
                    and recovery["upper_bound"] <= tolerance
                ):
                    failures.append(
                        f"jammer paired off-2 {role} {field} exceeds conservative recovery"
                    )
        for role in ("control", "background"):
            for field, tolerance, statistic in (
                ("sinr_db", 1.0, "median"),
                ("delivery", 0.05, "mean"),
                ("service_tier_index", 1.0, "median"),
            ):
                control = compare(
                    f"jammer_{role}_on_{field}",
                    "jammer_off_1",
                    "jammer_on",
                    role,
                    field,
                    statistic=statistic,
                )
                if not (
                    -tolerance <= control["lower_bound"]
                    and control["upper_bound"] <= tolerance
                ):
                    failures.append(
                        f"jammer paired on {role} {field} exceeds conservative locality"
                    )
        for role in ("control", "background"):
            for field, tolerance, statistic in (
                ("sinr_db", 1.0, "median"),
                ("delivery", 0.05, "mean"),
                ("service_tier_index", 1.0, "median"),
            ):
                expiry = compare(
                    f"expiry_{role}_recovery_{field}",
                    "expiry_unavailable",
                    "expiry_recovery",
                    role,
                    field,
                    statistic=statistic,
                )
                if not (
                    -tolerance <= expiry["lower_bound"]
                    and expiry["upper_bound"] <= tolerance
                ):
                    failures.append(
                        f"expiry paired {role} {field} exceeds conservative locality"
                    )
    except (KeyError, TypeError, ValueError, M4ValidationError) as exc:
        failures.append(f"paired causal bootstrap cannot be derived: {exc}")
    return details, failures


def validate_causal_runtime(
    records: list[dict[str, Any]],
    windows: Mapping[str, Mapping[str, Any]],
    *,
    required_process_counts: Mapping[str, int] = REQUIRED_PROCESS_COUNTS,
    run_id: str | None = None,
    runtime_id: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Validate continuous services/processes/sockets and phase ordering."""

    failures: list[str] = []
    details: dict[str, Any] = {}
    previous_sequence = 0
    previous_host = -1
    for number, record in enumerate(records, start=1):
        sequence = record.get("event_sequence")
        host = record.get("host_monotonic_ns")
        if (
            record.get("schema") != "ams.m4.runtime_event/v1"
            or isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence != previous_sequence + 1
            or isinstance(host, bool)
            or not isinstance(host, int)
            or host <= previous_host
            or not isinstance(record.get("event"), str)
            or (
                run_id is not None
                and (
                    record.get("run_id") != run_id
                    or record.get("runtime_id") != runtime_id
                    or isinstance(record.get("host_realtime_ns"), bool)
                    or not isinstance(record.get("host_realtime_ns"), int)
                    or record["host_realtime_ns"] <= 0
                )
            )
        ):
            failures.append(f"causal runtime event {number} identity/order differs")
            continue
        previous_sequence = sequence
        previous_host = host

    samples = [record for record in records if record.get("event") == "causal_resource_sample"]
    sample_hosts = [record.get("host_monotonic_ns") for record in samples]
    if (
        not samples
        or any(not isinstance(value, int) or isinstance(value, bool) for value in sample_hosts)
        or any(
            not 0 < int(sample_hosts[index]) - int(sample_hosts[index - 1]) <= 1_500_000_000
            for index in range(1, len(sample_hosts))
        )
    ):
        failures.append("causal runtime sampling has an interval gap over 1.5 seconds")

    frozen_processes: set[tuple[Any, ...]] | None = None
    socket_hash: str | None = None
    for index, sample in enumerate(samples):
        processes = sample.get("processes")
        sockets = sample.get("sockets")
        captures = sample.get("captures")
        queues = sample.get("queues")
        readiness = sample.get("readiness")
        if (
            not isinstance(processes, dict)
            or processes.get("counts") != dict(required_process_counts)
            or processes.get("required_counts") != dict(required_process_counts)
            or processes.get("roles_exact") is not True
            or processes.get("unclassified_count") != 0
            or not isinstance(processes.get("processes"), list)
            or len(processes["processes"]) != sum(required_process_counts.values())
            or not isinstance(sockets, dict)
            or sockets.get("ready") is not True
            or not isinstance(sockets.get("identity_sha256"), str)
            or HEX64.fullmatch(sockets["identity_sha256"]) is None
            or sockets.get("unexpected") != []
            or not isinstance(captures, dict)
            or captures.get("ready") is not True
            or captures.get("kernel_drops") != 0
            or not isinstance(queues, dict)
            or queues.get("bounded") is not True
            or queues.get("hidden_drops") != 0
            or not isinstance(readiness, dict)
            or readiness.get("ready") is not True
            or any(
                readiness.get(name) is not True
                for name in (
                    "clocks",
                    "odometry",
                    "poses",
                    "provider",
                    "adapter",
                    "ns3",
                    "endpoints",
                    "captures",
                    "topology",
                )
            )
        ):
            failures.append(f"causal resource/readiness sample {index} is incomplete")
            break
        scheduled = sample.get("scheduled_monotonic_ns")
        if (
            isinstance(scheduled, bool)
            or not isinstance(scheduled, int)
            or abs(int(sample["host_monotonic_ns"]) - scheduled) > 100_000_000
        ):
            failures.append(f"causal resource sample {index} missed 100-ms schedule")
            break
        identities = {
            (
                item.get("pid"),
                item.get("start_ticks"),
                item.get("pgid"),
                item.get("role"),
                item.get("executable_path"),
                item.get("executable_sha256"),
                item.get("cmdline_sha256"),
            )
            for item in processes["processes"]
        }
        if len(identities) != sum(required_process_counts.values()):
            failures.append(f"causal process identity sample {index} is ambiguous")
            break
        if frozen_processes is None:
            frozen_processes = identities
        elif identities != frozen_processes:
            failures.append(f"causal process identity changed at sample {index}")
            break
        if socket_hash is None:
            socket_hash = sockets["identity_sha256"]
        elif socket_hash != sockets["identity_sha256"]:
            failures.append(f"causal socket identity changed at sample {index}")
            break

    previous_window: str | None = None
    predicate_by_phase = {
        "good": "fresh_state_applied",
        "down": "fresh_physical_down_state_applied",
        "recovery": "fresh_state_applied",
        "off-1": "fresh_state_applied",
        "on": "fresh_jammer_state_applied",
        "off-2": "fresh_state_applied",
        "unavailable": "state_expired",
    }
    for window_id in WINDOW_IDS:
        window = windows.get(window_id)
        if not isinstance(window, Mapping):
            continue
        start = int(window["start_monotonic_ns"])
        end = int(window["end_monotonic_ns"])
        starts = [
            item
            for item in records
            if item.get("event") == "window_measurement_start"
            and item.get("window_id") == window_id
        ]
        ends = [
            item
            for item in records
            if item.get("event") == "window_measurement_end"
            and item.get("window_id") == window_id
        ]
        stimuli = [
            item
            for item in records
            if item.get("event") == "window_stimulus_applied"
            and item.get("window_id") == window_id
        ]
        drains = [
            item
            for item in records
            if item.get("event") == "window_drain_complete"
            and item.get("next_window_id") == window_id
        ]
        if not all(len(values) == 1 for values in (starts, ends, stimuli, drains)):
            failures.append(f"causal lifecycle cardinality differs: {window_id}")
            previous_window = window_id
            continue
        start_event, end_event, stimulus, drain = starts[0], ends[0], stimuli[0], drains[0]
        expected_predicate = (
            "fresh_state_applied_after_fault_removed"
            if window_id == "expiry_recovery"
            else predicate_by_phase[str(window["phase"])]
        )
        if (
            start_event.get("target_monotonic_ns") != start
            or not start <= start_event["host_monotonic_ns"] <= start + 100_000_000
            or end_event.get("target_monotonic_ns") != end
            or not end <= end_event["host_monotonic_ns"] <= end + 250_000_000
            or stimulus.get("state_predicate") != expected_predicate
            or stimulus.get("target_packet_link") != window.get("target_link")
            or isinstance(stimulus.get("pose_fixture_sequence"), bool)
            or not isinstance(stimulus.get("pose_fixture_sequence"), int)
            or stimulus["pose_fixture_sequence"] < 1
            or stimulus["host_monotonic_ns"] >= drain["host_monotonic_ns"]
            or drain["host_monotonic_ns"] >= start
            or drain.get("prior_window_id") != previous_window
            or drain.get("terminal_outcomes_complete") is not True
            or drain.get("queue_depths")
            != {
                "userspace": 0,
                "ns3": 0,
                "qdisc": 0,
                "capture_pending": 0,
            }
        ):
            failures.append(f"causal stimulus/drain/measurement ordering differs: {window_id}")
        stable = [
            item
            for item in samples
            if start - 10_000_000_000 <= item["host_monotonic_ns"] < start
        ]
        measurement = [
            item for item in samples if start <= item["host_monotonic_ns"] < end
        ]
        if (
            len(stable) < 10
            or stable[0]["host_monotonic_ns"] > start - 9_500_000_000
            or stable[-1]["host_monotonic_ns"] < start - 1_500_000_000
            or not measurement
            or measurement[0]["host_monotonic_ns"] > start + 100_000_000
            or measurement[-1]["host_monotonic_ns"] < end - 1_500_000_000
        ):
            failures.append(f"causal 10-s readiness/measurement coverage differs: {window_id}")
        previous_window = window_id
    details = {
        "resource_sample_count": len(samples),
        "frozen_process_identity_count": len(frozen_processes or ()),
        "socket_identity_sha256": socket_hash,
        "validated_window_count": len(windows),
    }
    return details, failures


def validate_expiry_sequence(
    *,
    fault_records: list[dict[str, Any]],
    adapter_records: list[dict[str, Any]],
    control_records: list[dict[str, Any]],
    wire: Mapping[str, Any],
    target_packet_link: str,
    target_directed_link_id: str,
) -> tuple[dict[str, Any], list[str]]:
    """Prove the exact real-result hold/reorder/duplicate/recovery sequence."""

    failures: list[str] = []
    details: dict[str, Any] = {}
    try:
        controls = [
            record
            for record in control_records
            if record.get("schema") == CONTROL_SCHEMA
        ]
        expected_actions = (
            "arm_hold_next",
            "arm_fault_parallel_next",
            "release_held",
            "inject_duplicate",
        )
        positions = []
        for action in expected_actions:
            matches = [index for index, record in enumerate(controls) if record.get("action") == action]
            if len(matches) != 1:
                raise M4ValidationError(f"expiry control {action} count differs")
            positions.append(matches[0])
        if positions != sorted(positions):
            raise M4ValidationError("expiry controls are out of order")

        required_fault_events = {
            "hold_armed",
            "real_result_held",
            "held_result_released",
            "byte_identical_duplicate_released",
        }
        observed = {record.get("event") for record in fault_records}
        if not required_fault_events <= observed:
            raise M4ValidationError("expiry fault event set is incomplete")
        held = next(record for record in fault_records if record.get("event") == "real_result_held")
        released = next(record for record in fault_records if record.get("event") == "held_result_released")
        duplicate = next(
            record
            for record in fault_records
            if record.get("event") == "byte_identical_duplicate_released"
        )
        if (
            held.get("directed_link_id") != target_directed_link_id
            or released.get("query_id") != held.get("query_id")
            or released.get("result_wire_sha256") != held.get("result_wire_sha256")
        ):
            raise M4ValidationError("held/released old-result identity differs")
        held_result = wire.get("message_by_hash", {}).get(held.get("result_wire_sha256"))
        if (
            not isinstance(held_result, Mapping)
            or held_result.get("message_type") != "result"
            or held_result.get("status") != "ok"
            or released.get("monotonic_ns", 0)
            <= held_result.get("expires_monotonic_ns", 2**63)
        ):
            raise M4ValidationError("held real result was not released after its expiry")

        expired = [record for record in adapter_records if record.get("event") == "state_expired"]
        applied = [
            record
            for record in adapter_records
            if record.get("event") == "result_applied"
            and record.get("directed_link") == target_packet_link
            and record.get("traffic_class") == "control"
        ]
        superseded = [
            record
            for record in adapter_records
            if record.get("event") == "result_discarded"
            and record.get("decision") == "superseded"
            and record.get("query_id") == held.get("query_id")
        ]
        duplicate_rejections = [
            record
            for record in adapter_records
            if record.get("event") == "result_discarded"
            and record.get("decision") == "duplicate"
            and record.get("query_id") == duplicate.get("query_id")
            and record.get("result_wire_sha256") == duplicate.get("result_wire_sha256")
        ]
        if not expired or len(superseded) != 1 or len(duplicate_rejections) != 1:
            raise M4ValidationError("expiry/old/duplicate adapter rejection evidence differs")
        expiry_time = expired[-1].get("monotonic_ns")
        newer = [
            record
            for record in applied
            if isinstance(record.get("monotonic_ns"), int)
            and expiry_time < record["monotonic_ns"] < released["monotonic_ns"]
        ]
        fresh_after_fault = [
            record
            for record in applied
            if isinstance(record.get("monotonic_ns"), int)
            and record["monotonic_ns"] > duplicate_rejections[0]["monotonic_ns"]
            and record.get("query_id") != duplicate.get("query_id")
        ]
        if not newer:
            raise M4ValidationError("newer real result was not applied before old release")
        if superseded[0].get("monotonic_ns", 0) <= released.get("monotonic_ns", 0):
            raise M4ValidationError("old result was not rejected after supervised release")
        if duplicate_rejections[0].get("monotonic_ns", 0) <= duplicate.get("monotonic_ns", 0):
            raise M4ValidationError("duplicate was not rejected after supervised release")
        if not fresh_after_fault:
            raise M4ValidationError("new fresh result was not applied after fault removal")
        details = {
            "held_query_id": held.get("query_id"),
            "newer_query_id": newer[0].get("query_id"),
            "duplicate_query_id": duplicate.get("query_id"),
            "fresh_recovery_query_id": fresh_after_fault[0].get("query_id"),
            "expiry_monotonic_ns": expiry_time,
        }
    except (KeyError, StopIteration, TypeError, ValueError, M4ValidationError) as exc:
        failures.append(f"F-expiry sequence cannot be proven: {exc}")
    return details, failures


def _empty_result(failure: str) -> dict[str, Any]:
    return {
        "contract": RESULT_CONTRACT,
        "run_id": "unavailable",
        "runtime_id": "unavailable",
        "profile": "m4_component",
        "passed": False,
        "gates": {"run_identity": gate([failure])},
        "metrics": {},
        "failures": [f"run_identity: {failure}"],
    }


def validate(run_dir: Path) -> dict[str, Any]:
    gate_failures: dict[str, list[str]] = defaultdict(list)
    details: dict[str, Any] = {}
    try:
        run = strict_json(run_dir / "raw/m4_causality_contract.json")
    except M4ValidationError as exc:
        return _empty_result(str(exc))
    run_id = run.get("run_id")
    runtime_id = run.get("runtime_id")
    expected_run_keys = {
        "schema_version",
        "contract",
        "run_id",
        "runtime_id",
        "run_nonce",
        "profile",
        "runner_start_monotonic_ns",
        "created_monotonic_ns",
        "provider_mode",
        "acceptance_eligible",
        "uav_count",
        "expected_cells",
        "bundle",
        "async_policy",
        "workload",
        "endpoint_path",
        "clock_producers",
        "windows",
        "execution_budget",
        "causality_statistics",
        "limits",
        "runtime_assets",
        "identity",
        "source_sha256",
    }
    gate_failures["run_identity"].extend(exact_keys(run, expected_run_keys, "M4 causality contract"))
    if (
        run.get("schema_version") != 1
        or run.get("contract") != RUN_CONTRACT
        or run.get("profile") != "m4_component"
        or run.get("provider_mode") != "real_sionna"
        or run.get("acceptance_eligible") is not True
        or run.get("uav_count") != 5
        or run.get("expected_cells") != 30
        or not isinstance(run_id, str)
        or not isinstance(runtime_id, str)
        or not isinstance(run.get("run_nonce"), str)
        or len(str(run.get("run_nonce"))) != 64
    ):
        gate_failures["run_identity"].append("M4 causality run identity differs")
    created = run.get("created_monotonic_ns")
    if isinstance(created, bool) or not isinstance(created, int) or created <= 0:
        gate_failures["run_identity"].append("M4 causality created_monotonic_ns is invalid")
    expected_policy = {
        "query_period_ns": QUERY_PERIOD_NS,
        "validity_ttl_ns": VALIDITY_TTL_NS,
        "query_deadline_ns": QUERY_DEADLINE_NS,
        "max_pose_age_ns": MAX_POSE_AGE_NS,
        "late_policy": "fail_closed_directed_link",
        "hold_last_beyond_expiry": False,
    }
    if run.get("async_policy") != expected_policy:
        gate_failures["run_identity"].append("M4 causality async policy differs")
    if run.get("clock_producers") != list(REQUIRED_CLOCK_PRODUCERS):
        gate_failures["time_coherence"].append(
            "M4 causality clock producer set differs"
        )
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
    if run.get("limits") != expected_limits:
        gate_failures["run_identity"].append("M4 causality bounded limits differ")
    bundle = run.get("bundle", {})
    scene, scene_failures = validate_scene_prerequisite(
        bundle.get("bundle_id") if isinstance(bundle, dict) else None,
        bundle.get("bundle_sha256") if isinstance(bundle, dict) else None,
    )
    gate_failures["canonical_scene"].extend(scene_failures)
    details["canonical_scene"] = {
        "bundle_id": scene.get("bundle_id"),
        "bundle_sha256": scene.get("bundle_sha256"),
    }
    identity_details, identity_failures = _validate_identity(
        run_dir,
        run,
        expected_profile="m4_component",
        required_source_paths=CAUSAL_SOURCE_PATHS,
        required_component_profiles={"m4_capacity_prerequisite"},
    )
    gate_failures["run_identity"].extend(identity_failures)
    details["run_identity"] = identity_details
    endpoint_details, endpoint_failures = _validate_actual_endpoint_path(run_dir, run)
    gate_failures["actual_m3_sitl_path"].extend(endpoint_failures)
    details["actual_m3_sitl_path"] = endpoint_details
    accepted_api = (
        endpoint_details.get("accepted_api")
        if isinstance(endpoint_details, dict)
        and isinstance(endpoint_details.get("accepted_api"), dict)
        else {}
    )
    workload = run.get("workload")
    expected_workload_keys = {
        "matrix_path",
        "matrix_sha256",
        "control_cell_count",
        "control_endpoint_form",
        "accepted_m3_receipt_path",
        "accepted_m3_receipt_sha256",
        "capacity_receipt_path",
        "capacity_receipt_sha256",
    }
    gate_failures["run_identity"].extend(
        exact_keys(workload, expected_workload_keys, "M4 causality workload")
    )
    if isinstance(workload, dict):
        try:
            m3_path = run_dir / "raw/prerequisites/m3.json"
            capacity_path = (
                run_dir / "raw/prerequisites/m4_capacity_prerequisite.json"
            )
            if (
                workload.get("matrix_path")
                != "network/config/endpoint_matrix_5uav.json"
                or workload.get("matrix_sha256")
                != accepted_api.get("matrix_sha256")
                or workload.get("control_cell_count") != 10
                or workload.get("control_endpoint_form")
                != accepted_api.get("control_endpoint_form")
                or workload.get("accepted_m3_receipt_path")
                != "raw/prerequisites/m3.json"
                or workload.get("accepted_m3_receipt_sha256")
                != sha256_file(m3_path)
                or workload.get("capacity_receipt_path")
                != "raw/prerequisites/m4_capacity_prerequisite.json"
                or workload.get("capacity_receipt_sha256")
                != sha256_file(capacity_path)
            ):
                gate_failures["run_identity"].append(
                    "M4 causality workload/API/prerequisite binding differs"
                )
        except M4ValidationError as exc:
            gate_failures["run_identity"].append(str(exc))
    windows, window_failures = validate_window_manifest(
        run.get("windows"),
        control_endpoint_form=accepted_api.get("control_endpoint_form"),
        endpoint_matrix_sha256=accepted_api.get("matrix_sha256"),
    )
    gate_failures["predeclared_windows"].extend(window_failures)
    try:
        finalization_timing = strict_json(
            run_dir / "raw/m4_finalization_timing.json"
        )
    except M4ValidationError as exc:
        finalization_timing = None
        gate_failures["predeclared_windows"].append(str(exc))
    execution_budget, budget_failures = validate_causal_execution_budget(
        run,
        windows,
        finalization=finalization_timing,
    )
    gate_failures["predeclared_windows"].extend(budget_failures)
    details["predeclared_windows"] = {
        "window_count": len(windows),
        "execution_budget": execution_budget,
    }
    statistics_policy = run.get("causality_statistics")
    if (
        not isinstance(statistics_policy, dict)
        or set(statistics_policy)
        != {
            "paired_bootstrap_seed",
            "paired_bootstrap_resamples",
            "confidence_level",
            "interval_method",
            "pairing",
        }
        or isinstance(statistics_policy.get("paired_bootstrap_seed"), bool)
        or not isinstance(statistics_policy.get("paired_bootstrap_seed"), int)
        or isinstance(statistics_policy.get("paired_bootstrap_resamples"), bool)
        or not isinstance(statistics_policy.get("paired_bootstrap_resamples"), int)
        or not 10_000 <= statistics_policy["paired_bootstrap_resamples"] <= 100_000
        or statistics_policy.get("confidence_level") != 0.95
        or statistics_policy.get("interval_method") != "paired_percentile"
        or statistics_policy.get("pairing") != "flow_group_id+ordinal_send_slot"
    ):
        gate_failures["predeclared_windows"].append(
            "causality statistics policy is not predeclared 95% paired bootstrap >=10000"
        )
    runtime_records: list[dict[str, Any]] = []
    try:
        runtime_records = strict_jsonl(
            run_dir / "logs/m4_runtime_events.jsonl", max_line_bytes=2 * 1024 * 1024
        )
        runtime_details, runtime_failures = validate_causal_runtime(
            runtime_records,
            windows,
            run_id=str(run_id),
            runtime_id=str(runtime_id),
        )
        gate_failures["runtime_continuity"].extend(runtime_failures)
        details["runtime_continuity"] = runtime_details
    except M4ValidationError as exc:
        gate_failures["runtime_continuity"].append(str(exc))

    causal_start_ns = min(
        (int(item["start_monotonic_ns"]) for item in windows.values()), default=0
    )
    causal_end_ns = max(
        (int(item["end_monotonic_ns"]) for item in windows.values()), default=0
    )
    clocks, clock_failures = validate_clock_correlations(
        run_dir / "logs/m4_clock_correlations.jsonl",
        run_id=str(run_id),
        runtime_id=str(runtime_id),
        start_ns=causal_start_ns,
        end_ns=causal_end_ns,
    )
    gate_failures["time_coherence"].extend(clock_failures)
    clock_process, clock_process_failures = validate_clock_process_binding(
        runtime_records, clocks
    )
    gate_failures["time_coherence"].extend(clock_process_failures)
    details["time_coherence"] = {**clocks, **clock_process}

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
    gate_failures["state_lineage"].extend(state_failures)
    pose_start = causal_start_ns
    pose_end = causal_end_ns
    poses, pose_failures = validate_pose_snapshots(
        run_dir / "logs/m4_pose_snapshots.jsonl",
        wire,
        start_monotonic_ns=pose_start,
        end_monotonic_ns=pose_end,
    )
    gate_failures["pose_lineage"].extend(pose_failures)
    try:
        pose_records = strict_jsonl(
            run_dir / "logs/m4_pose_snapshots.jsonl", max_line_bytes=262_144
        )
        geometry_details, geometry_failures = validate_causal_pose_geometry(
            pose_records,
            windows,
            strict_json(ROOT / "network/config/m4_canonical_scene_bundle.json"),
        )
        gate_failures["pose_lineage"].extend(geometry_failures)
        poses.update(geometry_details)
    except M4ValidationError as exc:
        gate_failures["pose_lineage"].append(str(exc))
    details["pose_lineage"] = poses
    try:
        packet_records = strict_jsonl(run_dir / "logs/ns3_packet_events.jsonl")
        actual_control_records = strict_jsonl(
            run_dir / "raw/actual_control/events.jsonl",
            max_line_bytes=2 * 1024 * 1024,
        )
        transaction_records, normalization_failures = normalize_actual_control_transactions(
            actual_control_records,
            run,
        )
        gate_failures["causal_effects"].extend(normalization_failures)
        metrics, metric_failures = derive_causal_window_metrics(
            windows,
            packet_records=packet_records,
            states_by_hash=states.get("by_hash", {}),
            transaction_records=transaction_records,
            run=run,
        )
        gate_failures["causal_effects"].extend(metric_failures)
        gate_failures["causal_effects"].extend(validate_causal_effects(windows, metrics))
        paired, paired_failures = validate_paired_causality(
            metrics,
            seed=(
                int(statistics_policy["paired_bootstrap_seed"])
                if isinstance(statistics_policy, dict)
                and isinstance(statistics_policy.get("paired_bootstrap_seed"), int)
                else -1
            ),
            resamples=(
                int(statistics_policy["paired_bootstrap_resamples"])
                if isinstance(statistics_policy, dict)
                and isinstance(statistics_policy.get("paired_bootstrap_resamples"), int)
                else -1
            ),
        )
        gate_failures["paired_causality"].extend(paired_failures)
        details["paired_causality"] = paired
        details["causal_effects"] = {
            window_id: {
                role: {
                    key: value
                    for key, value in group.items()
                    if key != "paired_samples"
                }
                for role, group in groups.items()
            }
            for window_id, groups in metrics.items()
        }
    except M4ValidationError as exc:
        metrics = {}
        gate_failures["causal_effects"].append(str(exc))
        gate_failures["paired_causality"].append(
            "paired causal bootstrap inputs are unavailable"
        )

    fault, fault_failures = validate_fault_audit(
        run_dir / "logs/sionna_result_faults.jsonl",
        wire,
        required_events={
            "hold_armed",
            "real_result_held",
            "held_result_released",
            "byte_identical_duplicate_released",
        },
    )
    gate_failures["expiry_sequence"].extend(fault_failures)
    try:
        fault_records = strict_jsonl(run_dir / "logs/sionna_result_faults.jsonl")
        adapter_records = strict_jsonl(run_dir / "logs/sionna_packet_adapter.jsonl")
        control_records = strict_jsonl(run_dir / "logs/m4_adapter_controls.jsonl")
        expiry, expiry_failures = validate_expiry_sequence(
            fault_records=fault_records,
            adapter_records=adapter_records,
            control_records=control_records,
            wire=wire,
            target_packet_link=matrix_link_identity("uav1.control.downlink")[0],
            target_directed_link_id=matrix_link_identity("uav1.control.downlink")[1],
        )
        gate_failures["expiry_sequence"].extend(expiry_failures)
        details["expiry_sequence"] = {**fault, **expiry}
    except M4ValidationError as exc:
        gate_failures["expiry_sequence"].append(str(exc))

    gate_names = (
        "run_identity",
        "canonical_scene",
        "actual_m3_sitl_path",
        "predeclared_windows",
        "runtime_continuity",
        "time_coherence",
        "real_provider_wire",
        "pose_lineage",
        "state_lineage",
        "causal_effects",
        "paired_causality",
        "expiry_sequence",
    )
    gates = {name: gate(gate_failures[name], details.get(name)) for name in gate_names}
    failures = [
        f"{name}: {failure}" for name in gate_names for failure in gate_failures[name]
    ]
    return {
        "contract": RESULT_CONTRACT,
        "run_id": run_id,
        "runtime_id": runtime_id,
        "profile": "m4_component",
        "passed": not failures,
        "gates": gates,
        "metrics": {
            "bundle_id": FROZEN_BUNDLE_ID,
            "bundle_sha256": FROZEN_BUNDLE_SHA256,
            "window_count": len(windows),
            "target_transaction_count": sum(
                int(value.get("target", {}).get("offered_unique", 0))
                for value in metrics.values()
            ),
        },
        "failures": failures,
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument(
        "--output", type=Path, default=Path("metrics/m4_validation_results.json")
    )
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)
    run_dir = args.run_dir.resolve()
    output = args.output if args.output.is_absolute() else run_dir / args.output
    try:
        result = validate(run_dir)
        payload = canonical_json(result)
        if args.no_write:
            if not regular_file(output) or output.read_bytes() != payload:
                raise M4ValidationError(
                    "producer causality result differs from independent derivation"
                )
        else:
            write_new(output, payload)
        sys.stdout.buffer.write(payload)
        return 0 if result["passed"] else 1
    except (M4ValidationError, OSError, TypeError, ValueError) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
