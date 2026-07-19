#!/usr/bin/env python3
"""Independently validate M4 obstruction, jammer, and expiry causality.

The validator deliberately consumes decoded actual-endpoint transactions in
addition to ns-3 and Sionna evidence.  A packet-engine event alone cannot prove
that ArduPilot received a command and emitted the exact TIMESYNC token echo.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import random
import re
import statistics
import struct
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

from network.validation.m4_common import (
    HEX64,
    M4ValidationError,
    canonical_json,
    deterministic_loss_sample,
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
from network.validation.m4_airborne_motion import (
    COORDINATE_TRANSFORM_VERSION,
    ODOMETRY_CHILD_FRAME,
    ODOMETRY_HEADER_FRAME,
    ODOMETRY_SOURCE_FRAME,
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
    bind_actual_control_frame,
    index_actual_control_datagrams,
    index_exact_ns3_unicast_deliveries,
    sha256_file,
    validate_clock_correlations,
    validate_clock_process_binding,
    validate_query_pose_runtime_binding,
    validate_scene_prerequisite,
)
from network.validation.validate_m4_capacity import (
    REQUIRED_SOURCE_PATHS as CAPACITY_SOURCE_PATHS,
    _validate_actual_endpoint_path,
    _validate_identity,
)


ROOT = Path(__file__).resolve().parents[2]
RUN_CONTRACT = "ams.m4.causality_run/v2"
RESULT_CONTRACT = "ams.m4.causality-validation/v2"
CAPACITY_RECEIPT_CONTRACT = "ams.m4-capacity.host-final-receipt/v2"
CAPACITY_RESULT_CONTRACT = "ams.m4-capacity.validation/v2"
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
CORRELATED_TIMESYNC_POLICY = "correlated_timesync_required"
MAX_CORRELATED_LOSS_PERCENT = 5
CAUSAL_MAVLINK_CRC_EXTRA = {0: 50, 76: 152, 77: 143, 111: 34, 148: 178, 253: 83}
CAUSAL_MAVLINK_MIN_PAYLOAD = {0: 9, 76: 33, 77: 3, 111: 16, 148: 60, 253: 51}
TIMEOUT_SLOT_MARGIN_NS = 100_000_000
QUIET_DRAIN_NS = 10_000_000_000
POSITIVE_WINDOW_PLAN = {
    "offered_per_uav": 100,
    "send_span_ms": 26_800,
    "duration_ns": 30_000_000_000,
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
PHYSICAL_DOWN_SETUP_GAP_NS = 3_000_000_000
EXPIRY_SETUP_GAP_NS = 5_000_000_000
EXPIRY_FAULT_ARM_SETTLE_NS = 50_000_000
CAUSAL_PIN_MODELS = ("uav1", "uav2", "uav3", "uav4", "uav5")
CAUSAL_POSE_VECTOR_MODELS = (
    "cp",
    *CAUSAL_PIN_MODELS,
    "jammer_m4",
)
CAUSAL_PIN_PLUGIN_NAME = "gz::sim::systems::VelocityControl"
CAUSAL_PIN_PLUGIN_FILENAME = "gz-sim-velocity-control-system"
CAUSAL_PIN_SYSTEM_ADD_SERVICE = "/world/map/entity/system/add"
CAUSAL_PIN_TOPIC_PREFIX = "/ams/m4/causal_pin"
CAUSAL_POSE_VECTOR_SERVICE = "/world/map/set_pose_vector/blocking"
CAUSAL_PIN_PUBLISH_PERIOD_NS = 50_000_000
CAUSAL_POSE_REFRESH_PERIOD_NS = 500_000_000
# Match the bounded Gazebo transport timeout while remaining an order of
# magnitude inside every three-second causal transition gap.  The phase driver
# suppresses periodic pose refreshes around the tighter staged-fault arms.
CAUSAL_POSE_VECTOR_MAX_LATENCY_NS = 250_000_000
CAUSAL_OBSERVED_VELOCITY_LIMIT = 0.05
CAUSAL_ODOMETRY_MAX_GAP_NS = 750_000_000
CAUSAL_ODOMETRY_EDGE_NS = 750_000_000
WRAPPER_TIMEOUT_NS = 1_500_000_000_000
PRECONTRACT_SETUP_BUDGET_NS = 120_000_000_000
# The formal runner's bounded sequential post-contract readiness waits total
# 145 seconds.  The collector must then retain a ten-second stable history
# before the first window, so the frozen budget is 160 seconds fail-closed.
RUNTIME_READINESS_BUDGET_NS = 160_000_000_000
CAUSAL_MEASUREMENT_SPAN_NS = 975_100_000_000
FINALIZATION_BUDGET_NS = 40_000_000_000
REQUIRED_WRAPPER_RESERVE_NS = 120_000_000_000
NS3_ENGINE_DURATION_NS = 1_250_000_000_000
TIMEOUT_REQUIRED_WINDOWS = frozenset(
    {"terrain_down", "building_down", "expiry_unavailable"}
)
RECOVERY_DRAIN_UAV = {
    "terrain_recovery": "uav1",
    "building_recovery": "uav2",
    "expiry_recovery": "uav1",
}


def validate_capacity_prerequisite_version(receipt: Any) -> list[str]:
    """Reject self-consistent legacy capacity receipts at the causal boundary."""

    result = receipt.get("result") if isinstance(receipt, Mapping) else None
    if (
        not isinstance(receipt, Mapping)
        or receipt.get("contract") != CAPACITY_RECEIPT_CONTRACT
        or receipt.get("result_contract") != CAPACITY_RESULT_CONTRACT
        or receipt.get("formal_accepted") is not True
        or receipt.get("passed") is not True
        or not isinstance(result, Mapping)
        or result.get("contract") != CAPACITY_RESULT_CONTRACT
        or result.get("passed") is not True
        or result.get("profile") != "m4_capacity_prerequisite"
    ):
        return ["M4 capacity prerequisite receipt/result version differs"]
    return []


def causal_pre_window_gap_ns(window_id: str) -> int:
    """Return the frozen state-settling/drain gap before one causal window."""

    if window_id in RECOVERY_DRAIN_UAV:
        return CAUSAL_GAP_NS
    if window_id == "expiry_unavailable":
        # One query period + one query deadline + the two-second state TTL
        # must elapse before the first fail-closed expiry transaction.
        return EXPIRY_SETUP_GAP_NS
    # Every pose/jammer transition, including the first fixture, gets a full
    # query-period/deadline/apply settling interval.  This also prevents a
    # next-window stimulus from contaminating the preceding half-open window.
    return PHYSICAL_DOWN_SETUP_GAP_NS


def causal_offer_offset_ns(window_id: str, ordinal: int) -> int:
    """Return one exact zero-based scheduled-send offset for a causal window."""

    plan = causal_window_plan(window_id)
    offered = int(plan["offered_per_uav"])
    if isinstance(ordinal, bool) or not 1 <= ordinal <= offered:
        raise M4ValidationError("causal offer ordinal is outside the frozen plan")
    if offered == 1:
        return 0
    return (
        (ordinal - 1)
        * int(plan["send_span_ms"])
        * 1_000_000
        // (offered - 1)
    )


def causal_window_plan(window_id: str) -> Mapping[str, int]:
    """Return the one frozen single-inflight timing plan for a causal window."""

    if window_id in {"terrain_down", "building_down"}:
        return DOWN_WINDOW_PLAN
    if window_id == "expiry_unavailable":
        return EXPIRY_DOWN_WINDOW_PLAN
    return POSITIVE_WINDOW_PLAN


def causal_response_policies(window_id: str) -> dict[str, str]:
    """Freeze the per-UAV outcome policy; mixed down windows stay observable."""

    policies = {
        f"uav{index}": CORRELATED_TIMESYNC_POLICY for index in range(1, 6)
    }
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
        expected_gap_ns = causal_pre_window_gap_ns(expected_id)
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
    """Re-derive the complete 1500-s wrapper budget from frozen constants."""

    failures: list[str] = []
    planned_total_ns = (
        PRECONTRACT_SETUP_BUDGET_NS
        + RUNTIME_READINESS_BUDGET_NS
        + CAUSAL_MEASUREMENT_SPAN_NS
        + FINALIZATION_BUDGET_NS
        + REQUIRED_WRAPPER_RESERVE_NS
    )
    unallocated_margin_ns = WRAPPER_TIMEOUT_NS - planned_total_ns
    ns3_required_runtime_ns = (
        RUNTIME_READINESS_BUDGET_NS
        + CAUSAL_MEASUREMENT_SPAN_NS
        + FINALIZATION_BUDGET_NS
    )
    expected_budget = {
        "wrapper_timeout_ns": WRAPPER_TIMEOUT_NS,
        "precontract_setup_budget_ns": PRECONTRACT_SETUP_BUDGET_NS,
        "runtime_readiness_budget_ns": RUNTIME_READINESS_BUDGET_NS,
        "causal_measurement_span_ns": CAUSAL_MEASUREMENT_SPAN_NS,
        "finalization_budget_ns": FINALIZATION_BUDGET_NS,
        "required_wrapper_reserve_ns": REQUIRED_WRAPPER_RESERVE_NS,
        "planned_total_ns": planned_total_ns,
        "unallocated_margin_ns": unallocated_margin_ns,
        "ns3_engine_duration_ns": NS3_ENGINE_DURATION_NS,
        "ns3_required_runtime_ns": ns3_required_runtime_ns,
        "ns3_unallocated_margin_ns": (
            NS3_ENGINE_DURATION_NS - ns3_required_runtime_ns
        ),
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
        failures.append("M4 causal precontract setup exceeded its 120-s budget")
    try:
        first_start = int(windows[WINDOW_IDS[0]]["start_monotonic_ns"])
        last_end = int(windows[WINDOW_IDS[-1]]["end_monotonic_ns"])
        if first_start != int(created) + RUNTIME_READINESS_BUDGET_NS:
            failures.append("M4 causal first window does not follow frozen 160-s readiness")
        if last_end - first_start != CAUSAL_MEASUREMENT_SPAN_NS:
            failures.append("M4 causal measurement span differs")
    except (KeyError, TypeError, ValueError):
        failures.append("M4 causal budget cannot bind the window timeline")
    if (
        planned_total_ns > WRAPPER_TIMEOUT_NS
        or REQUIRED_WRAPPER_RESERVE_NS < 120_000_000_000
        or unallocated_margin_ns < 0
        or NS3_ENGINE_DURATION_NS < ns3_required_runtime_ns
        or f"DURATION_MS={NS3_ENGINE_DURATION_NS // 1_000_000}" not in (
            ROOT / "network/scripts/run_m4_causality.sh"
        ).read_text(encoding="utf-8")
    ):
        failures.append("M4 causal wrapper/engine budget has insufficient reserve")
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
        elif event == "ardupilot_timesync_echo":
            outcomes[transaction_id].append(record)
        else:
            failures.append(
                f"actual endpoint transaction {number} event type differs"
            )
    for transaction_id, records_for_transaction in outcomes.items():
        if transaction_id not in offers:
            failures.append(f"endpoint outcome references unknown offer: {transaction_id}")
        if len({record.get("event") for record in records_for_transaction}) != len(
            records_for_transaction
        ):
            failures.append(f"endpoint transaction has duplicate outcome type: {transaction_id}")
    return offers, outcomes, failures


def _causal_x25_crc(payload: bytes) -> int:
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


def _strict_causal_hex(value: Any, label: str) -> bytes:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"(?:[0-9a-f]{2})+", value) is None
    ):
        raise M4ValidationError(f"{label} is not canonical lowercase hex")
    return bytes.fromhex(value)


def _parse_causal_mavlink_frame(frame: bytes) -> dict[str, Any]:
    """Independently validate one actual MAVLink v1/v2 common-message frame."""

    if not frame:
        raise M4ValidationError("causal MAVLink frame is empty")
    if frame[0] == 0xFD:
        if len(frame) < 12 or frame[2] & ~0x01:
            raise M4ValidationError("causal MAVLink v2 header is invalid")
        body_size = frame[1]
        signed = bool(frame[2] & 0x01)
        expected_size = 12 + body_size + (13 if signed else 0)
        if len(frame) != expected_size:
            raise M4ValidationError("causal MAVLink v2 frame size differs")
        message_id = int.from_bytes(frame[7:10], "little")
        source_system, source_component = frame[5], frame[6]
        body = frame[10 : 10 + body_size]
        checksum_offset = 10 + body_size
        crc_material = frame[1:checksum_offset]
        sequence = frame[4]
        version = 2
    elif frame[0] == 0xFE:
        if len(frame) < 8:
            raise M4ValidationError("causal MAVLink v1 frame is truncated")
        body_size = frame[1]
        if len(frame) != 8 + body_size:
            raise M4ValidationError("causal MAVLink v1 frame size differs")
        message_id = frame[5]
        source_system, source_component = frame[3], frame[4]
        body = frame[6 : 6 + body_size]
        checksum_offset = 6 + body_size
        crc_material = frame[1:checksum_offset]
        sequence = frame[2]
        version = 1
    else:
        raise M4ValidationError("causal MAVLink magic differs")
    extra = CAUSAL_MAVLINK_CRC_EXTRA.get(message_id)
    if extra is None:
        raise M4ValidationError(
            f"causal MAVLink message {message_id} is outside contract"
        )
    minimum_payload = CAUSAL_MAVLINK_MIN_PAYLOAD[message_id]
    if len(body) < minimum_payload:
        raise M4ValidationError(
            f"causal MAVLink message {message_id} payload is truncated"
        )
    if int.from_bytes(frame[checksum_offset : checksum_offset + 2], "little") != (
        _causal_x25_crc(crc_material + bytes([extra]))
    ):
        raise M4ValidationError("causal MAVLink frame CRC differs")
    return {
        "version": version,
        "message_id": message_id,
        "sequence": sequence,
        "source_system": source_system,
        "source_component": source_component,
        "payload": body,
        "sha256": hashlib.sha256(frame).hexdigest(),
        "size": len(frame),
        "raw": frame,
    }


def _parse_causal_request_datagram(payload: bytes) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    offset = 0
    while offset < len(payload):
        if len(payload) - offset < 12 or payload[offset] != 0xFD:
            raise M4ValidationError(
                "causal request is not contiguous unsigned MAVLink v2"
            )
        body_size = payload[offset + 1]
        if payload[offset + 2 : offset + 4] != b"\0\0":
            raise M4ValidationError("causal request MAVLink flags differ")
        frame_size = 12 + body_size
        if offset + frame_size > len(payload):
            raise M4ValidationError("causal request MAVLink frame is truncated")
        frames.append(
            _parse_causal_mavlink_frame(payload[offset : offset + frame_size])
        )
        offset += frame_size
    return frames


def _expected_causal_timesync_token(
    run_nonce: Any, phase_code: Any, uav: Any, ordinal: Any
) -> int:
    if (
        not isinstance(run_nonce, str)
        or HEX64.fullmatch(run_nonce) is None
        or isinstance(phase_code, bool)
        or not isinstance(phase_code, int)
        or not 1 <= phase_code <= 15
        or isinstance(uav, bool)
        or not isinstance(uav, int)
        or not 1 <= uav <= 5
        or isinstance(ordinal, bool)
        or not isinstance(ordinal, int)
        or not 1 <= ordinal <= 0xFFFF
    ):
        raise M4ValidationError("causal TIMESYNC token identity is invalid")
    prefix = int.from_bytes(
        hashlib.sha256(bytes.fromhex(run_nonce)).digest()[:5], "big"
    )
    return (prefix << 23) | (phase_code << 19) | (uav << 16) | ordinal


def _validate_causal_offer_payload(
    record: Mapping[str, Any], *, run_nonce: Any, response_policy: Any
) -> tuple[str, int]:
    """Bind the ns-3 packet key to the full marker+command+TIMESYNC datagram."""

    full_payload = _strict_causal_hex(
        record.get("request_transport_payload_hex"),
        "causal request transport payload",
    )
    full_digest = hashlib.sha256(full_payload).hexdigest()
    size = record.get("request_transport_payload_size")
    if (
        record.get("request_transport_payload_sha256") != full_digest
        or isinstance(size, bool)
        or not isinstance(size, int)
        or size != len(full_payload)
        or record.get("request_transport_send_return_size") != size
        or record.get("correlation_kind") != "mavlink_timesync_echo_v1"
        or record.get("response_policy") != response_policy
    ):
        raise M4ValidationError("causal combined request authority differs")
    frames = _parse_causal_request_datagram(full_payload)
    if (
        len(frames) != 3
        or [frame["message_id"] for frame in frames] != [253, 76, 111]
        or any(
            frame["version"] != 2
            or frame["source_system"] != 255
            or frame["source_component"] != 190
            for frame in frames
        )
        or frames[1]["sequence"] != (frames[0]["sequence"] + 1) & 0xFF
        or frames[2]["sequence"] != (frames[1]["sequence"] + 1) & 0xFF
    ):
        raise M4ValidationError("causal combined request frame envelope differs")
    marker, command, timesync = frames
    nested = (
        ("marker_frame_hex", "marker_frame_sha256", marker),
        ("command_frame_hex", "command_frame_sha256", command),
        ("timesync_frame_hex", "timesync_frame_sha256", timesync),
    )
    for hex_key, hash_key, frame in nested:
        if (
            _strict_causal_hex(record.get(hex_key), hex_key) != frame["raw"]
            or record.get(hash_key) != frame["sha256"]
        ):
            raise M4ValidationError(f"causal nested {hex_key} binding differs")
    marker_text = record.get("marker_text")
    try:
        decoded_marker = marker["payload"][1:].decode("ascii")
    except UnicodeDecodeError as exc:
        raise M4ValidationError("causal marker text is not ASCII") from exc
    if (
        len(marker["payload"]) != 51
        or marker["payload"][0] != 6
        or decoded_marker != marker_text
        or not isinstance(marker_text, str)
        or len(marker_text) != 50
    ):
        raise M4ValidationError("causal marker frame differs")
    if len(command["payload"]) != struct.calcsize("<7fHBBB"):
        raise M4ValidationError("causal COMMAND_LONG payload size differs")
    command_fields = struct.unpack("<7fHBBB", command["payload"])
    uav = record.get("uav")
    if (
        command_fields[0] != 148.0
        or any(value != 0.0 for value in command_fields[1:7])
        or command_fields[7:] != (512, uav, 1, 0)
        or record.get("requested_message_id") != 148
        or record.get("mavlink_command") != 512
        or record.get("target_system") != uav
        or record.get("target_component") != 1
    ):
        raise M4ValidationError("causal COMMAND_LONG request differs")
    if len(timesync["payload"]) != 16:
        raise M4ValidationError("causal TIMESYNC request size differs")
    tc1, token = struct.unpack("<qq", timesync["payload"])
    expected_token = _expected_causal_timesync_token(
        run_nonce,
        record.get("transport_phase_code"),
        uav,
        record.get("ordinal_send_slot"),
    )
    if (
        tc1 != 0
        or token != expected_token
        or record.get("timesync_request_tc1") != 0
        or record.get("timesync_request_ts1") != token
    ):
        raise M4ValidationError("causal TIMESYNC request token differs")
    return full_digest, token


def _validate_causal_raw_message(
    record: Mapping[str, Any],
    *,
    uav: int,
    message_type: str,
    message_id: int,
    datagram_index: Mapping[tuple[int, str, int], list[dict[str, Any]]],
    consumed_occurrences: set[tuple[int, int]],
    event_sequence: int | None = None,
) -> tuple[int, dict[str, Any]]:
    bound_record = dict(record)
    if event_sequence is not None:
        bound_record["event_sequence"] = event_sequence
    frame, parent = bind_actual_control_frame(
        bound_record,
        expected_message_id=message_id,
        datagram_index=datagram_index,
        consumed_occurrences=consumed_occurrences,
        frame_decoder=_parse_causal_mavlink_frame,
    )
    # The common parent index proves exact framing and no trailing bytes.  The
    # causal contract additionally recomputes message CRC and minimum payload
    # length for *every* frame in the selected UDP datagram, not only the child
    # event's standalone frame.  This prevents an otherwise-unchecked sibling
    # frame from turning forged concatenated bytes into a valid parent.
    for parent_frame in parent["frames"]:
        parsed_parent_frame = _parse_causal_mavlink_frame(parent_frame["bytes"])
        if (
            parsed_parent_frame["message_id"] != parent_frame["message_id"]
            or parsed_parent_frame["source_system"] != parent_frame["system_id"]
            or parsed_parent_frame["source_component"]
            != parent_frame["component_id"]
            or parsed_parent_frame["size"] != parent_frame["size"]
            or parsed_parent_frame["sha256"] != parent_frame["sha256"]
        ):
            raise M4ValidationError(
                "causal UDP parent frame reconstruction differs"
            )
    received_ns = record.get("received_monotonic_ns")
    if (
        frame["message_id"] != message_id
        or frame["source_system"] != uav
        or frame["source_component"] != 1
        or record.get("message_type") != message_type
        or record.get("message_id") != message_id
        or record.get("source_system") != uav
        or record.get("source_component") != 1
        or record.get("uav") != uav
        or record.get("peer_ip") != f"10.71.{uav}.10"
        or record.get("peer_udp_port") != 14600 + uav
        or isinstance(received_ns, bool)
        or not isinstance(received_ns, int)
        or record.get("mavlink_frame_sha256") != frame["sha256"]
        or record.get("mavlink_frame_size") != frame["size"]
        or not isinstance(record.get("transport_payload_sha256"), str)
        or HEX64.fullmatch(str(record["transport_payload_sha256"])) is None
    ):
        raise M4ValidationError(f"raw {message_type} modeled-path envelope differs")
    return received_ns, frame


def normalize_actual_control_transactions(
    records: list[dict[str, Any]],
    run: Mapping[str, Any],
    windows: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    """Derive causal transaction records only from the Q3 actual-control audit.

    No M4 process is allowed to manufacture a second ACK/heartbeat transcript.
    The normalized view is deterministic validator state: requests come from
    the real GCS probe and successful outcomes retain the actual vehicle sysid,
    component, received timestamp, and transport datagram hash.
    """

    failures: list[str] = []
    normalized: list[dict[str, Any]] = []
    offers: dict[str, dict[str, Any]] = {}
    results_by_transaction: dict[str, list[dict[str, Any]]] = defaultdict(list)
    heartbeats: list[dict[str, Any]] = []
    raw_command_acks: list[dict[str, Any]] = []
    raw_autopilot_versions: list[dict[str, Any]] = []
    ambient_timesync_requests: list[dict[str, Any]] = []
    uncounted_raw_messages: list[dict[str, Any]] = []
    phase_starts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    phase_completes: dict[str, list[dict[str, Any]]] = defaultdict(list)
    previous_raw_sequence = 0
    try:
        datagram_index = index_actual_control_datagrams(
            records,
            expected_peers={
                (f"10.71.{uav}.10", 14600 + uav): uav
                for uav in range(1, 6)
            },
            expected_rx_tos=184,
        )
    except M4ValidationError as exc:
        failures.append(f"raw actual-control UDP parent audit differs: {exc}")
        datagram_index = {}
    consumed_frame_occurrences: set[tuple[int, int]] = set()

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
        event = record.get("event")
        if event == "real_command_offered":
            transaction_id = record.get("transaction_id")
            uav = record.get("uav")
            window = windows.get(str(record.get("window_id")))
            response_policy = (
                window.get("response_policies", {}).get(f"uav{uav}")
                if isinstance(window, Mapping)
                and isinstance(window.get("response_policies"), Mapping)
                else None
            )
            try:
                digest, _token = _validate_causal_offer_payload(
                    record,
                    run_nonce=run.get("run_nonce"),
                    response_policy=response_policy,
                )
            except M4ValidationError as exc:
                failures.append(
                    f"raw actual-control causal offer {number} payload differs: {exc}"
                )
                continue
            if (
                not isinstance(transaction_id, str)
                or not transaction_id
                or transaction_id in offers
                or isinstance(uav, bool)
                or not isinstance(uav, int)
                or not 1 <= uav <= 5
                or response_policy
                not in {CORRELATED_TIMESYNC_POLICY, "timeout_required"}
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
        elif event == "transaction_result":
            transaction_id = record.get("transaction_id")
            if isinstance(transaction_id, str):
                results_by_transaction[transaction_id].append(record)
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
                or record.get("marker_frame_sha256")
                != offer.get("marker_frame_sha256")
                or record.get("timesync_request_frame_sha256")
                != offer.get("timesync_frame_sha256")
                or record.get("request_transport_payload_sha256")
                != offer.get("request_transport_payload_sha256")
                or record.get("request_transport_payload_size")
                != offer.get("request_transport_payload_size")
                or record.get("timesync_request_tc1") != 0
                or record.get("timesync_request_ts1")
                != offer.get("timesync_request_ts1")
                or record.get("correlation_kind") != "mavlink_timesync_echo_v1"
                or record.get("flow_group_id") != offer.get("flow_group_id")
                or record.get("ordinal_send_slot") != offer.get("ordinal_send_slot")
                or record.get("window_id") != offer.get("window_id")
                or record.get("uav") != offer.get("uav")
                or record.get("transport_phase_code")
                != offer.get("transport_phase_code")
                or record.get("endpoint_form") != offer.get("endpoint_form")
                or record.get("downlink_cell_id") != offer.get("cell_id")
                or record.get("uplink_cell_id")
                != f"uav{offer.get('uav')}.control.uplink"
                or record.get("sent_monotonic_ns")
                != offer.get("sent_monotonic_ns")
                or record.get("ack") is not None
                or record.get("requested_telemetry") is not None
            ):
                failures.append(f"raw actual-control causal result {number} binding differs")
                continue
            window = windows.get(str(offer.get("window_id")))
            policy = (
                window.get("response_policies", {}).get(
                    f"uav{offer.get('uav')}"
                )
                if isinstance(window, Mapping)
                and isinstance(window.get("response_policies"), Mapping)
                else None
            )
            sent_ns = offer.get("sent_monotonic_ns")
            completed_ns = record.get("completed_monotonic_ns")
            elapsed_ms = record.get("timeout_elapsed_ms")
            valid_times = (
                isinstance(sent_ns, int)
                and not isinstance(sent_ns, bool)
                and isinstance(completed_ns, int)
                and not isinstance(completed_ns, bool)
                and completed_ns >= sent_ns
                and finite_number(elapsed_ms)
                and math.isclose(
                    float(elapsed_ms),
                    round((completed_ns - sent_ns) / 1_000_000, 6),
                    rel_tol=0.0,
                    abs_tol=1e-6,
                )
            )
            if record.get("success") is not True:
                if (
                    policy
                    not in {CORRELATED_TIMESYNC_POLICY, "timeout_required"}
                    or record.get("success") is not False
                    or record.get("timed_out") is not True
                    or record.get("timesync_response") is not None
                    or record.get("timeout_contract_satisfied") is not True
                    or not valid_times
                    or completed_ns - sent_ns < OUTCOME_TIMEOUT_NS
                ):
                    failures.append(
                        f"raw actual-control causal loss {number} is not an exact timeout"
                    )
                continue
            if (
                policy != CORRELATED_TIMESYNC_POLICY
                or record.get("timed_out") is not False
                or record.get("timeout_contract_satisfied") is not True
                or not valid_times
            ):
                failures.append(
                    f"raw actual-control causal success {number} violates its response policy"
                )
                continue
            response = record.get("timesync_response")
            uav = offer.get("uav")
            try:
                if not isinstance(response, Mapping):
                    raise M4ValidationError("TIMESYNC response is absent")
                received_ns, response_frame = _validate_causal_raw_message(
                    response,
                    uav=int(uav),
                    message_type="TIMESYNC",
                    message_id=111,
                    datagram_index=datagram_index,
                    consumed_occurrences=consumed_frame_occurrences,
                    event_sequence=raw_sequence,
                )
                if len(response_frame["payload"]) != 16:
                    raise M4ValidationError("TIMESYNC response payload size differs")
                response_tc1, response_ts1 = struct.unpack(
                    "<qq", response_frame["payload"]
                )
                if (
                    response.get("timesync_tc1") != response_tc1
                    or response.get("timesync_ts1") != response_ts1
                    or not 0 < response_tc1 < (1 << 63)
                    or response_ts1 != offer.get("timesync_request_ts1")
                    or received_ns != completed_ns
                    or not sent_ns <= received_ns < sent_ns + OUTCOME_TIMEOUT_NS
                ):
                    raise M4ValidationError(
                        "locked-ArduPilot TIMESYNC tc1-clock/ts1-echo differs"
                    )
            except (TypeError, ValueError, M4ValidationError) as exc:
                failures.append(
                    f"raw actual-control causal outcome {number} is not exact TIMESYNC evidence: {exc}"
                )
                continue
            common = {
                "window_id": offer.get("window_id"),
                "uav": f"uav{uav}",
                "producer_role": "arducopter",
                "directed_link": f"cp>uav{uav}",
                "traffic_class": "control",
                "request_transport_payload_sha256": offer.get(
                    "request_transport_payload_sha256"
                ),
                "flow_group_id": offer.get("flow_group_id"),
                "matrix_cell_id": offer.get("cell_id"),
                "endpoint_form": offer.get("endpoint_form"),
                "direction": "downlink",
                "ordinal_send_slot": offer.get("ordinal_send_slot"),
            }
            append(
                "ardupilot_timesync_echo",
                record,
                **common,
                host_monotonic_ns=received_ns,
                response_transport_payload_sha256=response[
                    "transport_payload_sha256"
                ],
                source_system=response.get("source_system"),
                source_component=response.get("source_component"),
                timesync_tc1_vehicle_clock=response_tc1,
                timesync_ts1_echo_token=response_ts1,
            )
        elif event == "real_heartbeat":
            heartbeats.append(record)
        elif event == "real_window_command_ack":
            raw_command_acks.append(record)
        elif event == "real_window_requested_telemetry":
            raw_autopilot_versions.append(record)
        elif event == "ambient_timesync_request":
            ambient_timesync_requests.append(record)
        elif event in {"late_timesync_echo", "late_unbound_window_response"}:
            # These frames are deliberately excluded from causal outcomes and
            # liveness counts, but their raw bytes still require the same UDP
            # parent proof as counted responses.
            uncounted_raw_messages.append(record)
        elif event == "actual_control_phase_start":
            phase_starts[str(record.get("window_id"))].append(record)
        elif event == "actual_control_phase_complete":
            phase_completes[str(record.get("window_id"))].append(record)
        elif event in {
            "foreign_control_message",
            "control_parse_error",
            "invalid_timesync_echo",
            "uncorrelated_timesync_echo",
            "duplicate_timesync_echo",
            "forbidden_stopped_control_response",
            "late_stopped_control_response",
            "uncorrelated_control_response",
            "invalid_window_command_ack",
            "phase_ended_before_outcome_timeout",
            "heartbeat_history_overflow",
        }:
            failures.append(f"raw actual-control fatal event is present: {event}")
    if not offers:
        failures.append("raw actual-control causal audit has no command offers")
    valid_heartbeat_sequences: set[int] = set()
    for number, heartbeat in enumerate(heartbeats, start=1):
        try:
            uav = heartbeat.get("uav")
            if isinstance(uav, bool) or not isinstance(uav, int):
                raise M4ValidationError("heartbeat UAV identity differs")
            _validate_causal_raw_message(
                heartbeat,
                uav=uav,
                message_type="HEARTBEAT",
                message_id=0,
                datagram_index=datagram_index,
                consumed_occurrences=consumed_frame_occurrences,
            )
            sequence = heartbeat.get("event_sequence")
            if isinstance(sequence, int) and not isinstance(sequence, bool):
                valid_heartbeat_sequences.add(sequence)
        except (TypeError, ValueError, M4ValidationError) as exc:
            failures.append(
                f"raw actual-control heartbeat {number} envelope differs: {exc}"
            )
    for number, request in enumerate(ambient_timesync_requests, start=1):
        try:
            uav = request.get("uav")
            if isinstance(uav, bool) or not isinstance(uav, int):
                raise M4ValidationError("ambient TIMESYNC UAV identity differs")
            _received_ns, frame = _validate_causal_raw_message(
                request,
                uav=uav,
                message_type="TIMESYNC",
                message_id=111,
                datagram_index=datagram_index,
                consumed_occurrences=consumed_frame_occurrences,
            )
            if len(frame["payload"]) != 16:
                raise M4ValidationError("ambient TIMESYNC payload size differs")
            tc1, ts1 = struct.unpack("<qq", frame["payload"])
            if (
                tc1 != 0
                or not 0 < ts1 < (1 << 63)
                or request.get("timesync_tc1") != tc1
                or request.get("timesync_ts1") != ts1
            ):
                raise M4ValidationError("ambient TIMESYNC request fields differ")
        except (TypeError, ValueError, M4ValidationError) as exc:
            failures.append(
                f"raw ambient TIMESYNC request {number} envelope differs: {exc}"
            )
    for event_name, message_type, message_id, raw_records in (
        (
            "real_window_command_ack",
            "COMMAND_ACK",
            77,
            raw_command_acks,
        ),
        (
            "real_window_requested_telemetry",
            "AUTOPILOT_VERSION",
            148,
            raw_autopilot_versions,
        ),
    ):
        for number, response in enumerate(raw_records, start=1):
            try:
                uav = response.get("uav")
                if isinstance(uav, bool) or not isinstance(uav, int):
                    raise M4ValidationError("raw liveness UAV identity differs")
                received_ns, frame = _validate_causal_raw_message(
                    response,
                    uav=uav,
                    message_type=message_type,
                    message_id=message_id,
                    datagram_index=datagram_index,
                    consumed_occurrences=consumed_frame_occurrences,
                )
                window_id = response.get("window_id")
                window = windows.get(str(window_id))
                policy = (
                    window.get("response_policies", {}).get(f"uav{uav}")
                    if isinstance(window, Mapping)
                    and isinstance(window.get("response_policies"), Mapping)
                    else None
                )
                if (
                    not isinstance(window, Mapping)
                    or response.get("phase") != window_id
                    or policy != CORRELATED_TIMESYNC_POLICY
                    or response.get("response_policy") != policy
                    or not int(window["start_monotonic_ns"])
                    <= received_ns
                    < int(window["end_monotonic_ns"])
                ):
                    raise M4ValidationError(
                        "raw liveness window/policy envelope differs"
                    )
                if message_id == 77:
                    if (
                        len(frame["payload"]) < 3
                        or struct.unpack("<HB", frame["payload"][:3]) != (512, 0)
                        or response.get("mavlink_command") != 512
                        or response.get("mavlink_result") != 0
                    ):
                        raise M4ValidationError("raw COMMAND_ACK fields differ")
            except (KeyError, TypeError, ValueError, M4ValidationError) as exc:
                failures.append(
                    f"raw {event_name} {number} envelope differs: {exc}"
                )
    for number, response in enumerate(uncounted_raw_messages, start=1):
        try:
            uav = response.get("uav")
            message_type = response.get("message_type")
            expected_message_id = {
                "COMMAND_ACK": 77,
                "AUTOPILOT_VERSION": 148,
                "TIMESYNC": 111,
            }.get(str(message_type))
            if (
                isinstance(uav, bool)
                or not isinstance(uav, int)
                or expected_message_id is None
            ):
                raise M4ValidationError("uncounted raw message identity differs")
            _received_ns, frame = _validate_causal_raw_message(
                response,
                uav=uav,
                message_type=str(message_type),
                message_id=expected_message_id,
                datagram_index=datagram_index,
                consumed_occurrences=consumed_frame_occurrences,
            )
            if expected_message_id == 111:
                tc1, ts1 = struct.unpack("<qq", frame["payload"])
                if (
                    response.get("timesync_tc1") != tc1
                    or response.get("timesync_ts1") != ts1
                ):
                    raise M4ValidationError(
                        "uncounted TIMESYNC decoded fields differ"
                    )
        except (TypeError, ValueError, struct.error, M4ValidationError) as exc:
            failures.append(
                f"raw actual-control uncounted response {number} envelope differs: {exc}"
            )

    required_parent_occurrences = {
        (int(parent["event_sequence"]), int(frame["offset"]))
        for parents in datagram_index.values()
        for parent in parents
        for frame in parent["frames"]
        if frame["message_id"] in {0, 77, 111, 148}
        and isinstance(parent.get("event_sequence"), int)
        and not isinstance(parent.get("event_sequence"), bool)
    }
    unconsumed_parent_occurrences = (
        required_parent_occurrences - consumed_frame_occurrences
    )
    if unconsumed_parent_occurrences:
        failures.append(
            "raw actual-control relevant UDP parent frame lacks exactly one "
            f"derived event: count={len(unconsumed_parent_occurrences)}"
        )
    audit: dict[str, Any] = {}
    for transaction_id in sorted(set(results_by_transaction) - set(offers)):
        failures.append(
            f"raw actual-control result has no accepted offer: {transaction_id}"
        )
    for transaction_id in sorted(offers):
        if len(results_by_transaction.get(transaction_id, [])) != 1:
            failures.append(
                f"raw actual-control offer lacks exactly one result: {transaction_id}"
            )

    uav_labels = {f"uav{uav}" for uav in range(1, 6)}
    if set(phase_starts) != set(windows) or set(phase_completes) != set(windows):
        failures.append("raw actual-control phase window set differs")
    for window_id, window in windows.items():
        window_failures: list[str] = []
        try:
            start_ns = int(window["start_monotonic_ns"])
            end_ns = int(window["end_monotonic_ns"])
            offered_per_uav = int(window["offered_per_uav"])
            send_span_ms = int(window["send_span_ms"])
            transport_phase_code = int(window["transport_phase_code"])
            response_policies = window["response_policies"]
            quiet_drain = window["minimum_quiet_drain_ns_by_uav"]
            if (
                not isinstance(response_policies, Mapping)
                or set(response_policies) != uav_labels
                or not isinstance(quiet_drain, Mapping)
                or set(quiet_drain) != uav_labels
            ):
                raise M4ValidationError("window policy maps differ")
            expected_flow_groups = {
                f"uav{uav}": matrix_flow_group_identity(
                    f"uav{uav}.control.downlink",
                    str(window["control_endpoint_form"]),
                    matrix_sha256=str(window["endpoint_matrix_sha256"]),
                )["flow_group_id"]
                for uav in range(1, 6)
            }
            starts = phase_starts.get(window_id, [])
            completes = phase_completes.get(window_id, [])
            if len(starts) != 1 or len(completes) != 1:
                raise M4ValidationError("phase start/complete count differs")
            phase_start = starts[0]
            phase_complete = completes[0]
            policy_label = (
                next(iter(set(response_policies.values())))
                if len(set(response_policies.values())) == 1
                else "mixed_per_uav"
            )
            phase_start_ns = phase_start.get("monotonic_ns")
            phase_complete_ns = phase_complete.get("monotonic_ns")
            window_result_completion_times = [
                result.get("completed_monotonic_ns")
                for transaction_id, offer in offers.items()
                if offer.get("window_id") == window_id
                for result in results_by_transaction.get(transaction_id, [])
                if isinstance(result.get("completed_monotonic_ns"), int)
                and not isinstance(result.get("completed_monotonic_ns"), bool)
            ]
            if (
                phase_start.get("phase") != window_id
                or phase_start.get("transport_phase_code") != transport_phase_code
                or phase_start.get("offered_per_downlink_cell") != offered_per_uav
                or phase_start.get("declared_start_monotonic_ns") != start_ns
                or phase_start.get("declared_end_monotonic_ns") != end_ns
                or phase_start.get("send_span_ms") != send_span_ms
                or phase_start.get("expected_engine_state")
                != window.get("expected_engine_state")
                or phase_start.get("response_policies") != response_policies
                or phase_start.get("response_policy") != policy_label
                or phase_start.get("minimum_quiet_drain_ns_by_uav")
                != quiet_drain
                or phase_start.get("flow_group_ids") != expected_flow_groups
                or phase_complete.get("phase") != window_id
                or phase_complete.get("transport_phase_code")
                != transport_phase_code
                or phase_complete.get("expected_engine_state")
                != window.get("expected_engine_state")
                or phase_complete.get("response_policies") != response_policies
                or phase_complete.get("response_policy") != policy_label
                or phase_complete.get("offered_counts")
                != {label: offered_per_uav for label in sorted(uav_labels)}
                or phase_complete.get("quarantined_uavs") != []
                or isinstance(phase_start_ns, bool)
                or not isinstance(phase_start_ns, int)
                or not start_ns <= phase_start_ns <= start_ns + 100_000_000
                or isinstance(phase_complete_ns, bool)
                or not isinstance(phase_complete_ns, int)
                or not end_ns <= phase_complete_ns <= end_ns + 250_000_000
                or (
                    window_result_completion_times
                    and phase_complete_ns < max(window_result_completion_times)
                )
            ):
                raise M4ValidationError("phase declaration/completion fields differ")
            phase_heartbeat_counts = phase_complete.get("heartbeat_counts")
            if (
                not isinstance(phase_heartbeat_counts, Mapping)
                or set(phase_heartbeat_counts) != uav_labels
                or any(
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or value < 0
                    or (
                        response_policies[label] == CORRELATED_TIMESYNC_POLICY
                        and value < 3
                    )
                    for label, value in phase_heartbeat_counts.items()
                )
            ):
                raise M4ValidationError(
                    "phase heartbeat count is invalid for its response policy"
                )
            phase_raw_ack_counts = phase_complete.get("raw_command_ack_counts")
            phase_raw_telemetry_counts = phase_complete.get(
                "raw_autopilot_version_counts"
            )
            if any(
                not isinstance(counts, Mapping)
                or set(counts) != uav_labels
                or any(
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or value < 0
                    or (
                        response_policies[label] == CORRELATED_TIMESYNC_POLICY
                        and value < 1
                    )
                    or (
                        response_policies[label] == "timeout_required"
                        and value != 0
                    )
                    for label, value in counts.items()
                )
                for counts in (phase_raw_ack_counts, phase_raw_telemetry_counts)
            ):
                raise M4ValidationError(
                    "phase raw ACK/AUTOPILOT_VERSION liveness counts differ"
                )

            window_audit: dict[str, Any] = {}
            for uav in range(1, 6):
                label = f"uav{uav}"
                expected_flow_group = expected_flow_groups[label]
                selected = sorted(
                    [
                        offer
                        for offer in offers.values()
                        if offer.get("window_id") == window_id
                        and offer.get("uav") == uav
                    ],
                    key=lambda offer: int(offer.get("ordinal_send_slot", -1)),
                )
                ordinals = [offer.get("ordinal_send_slot") for offer in selected]
                if (
                    len(selected) != offered_per_uav
                    or ordinals != list(range(1, offered_per_uav + 1))
                ):
                    window_failures.append(
                        f"{label} offer cardinality/ordinals differ"
                    )
                correlated_losses = 0
                for offer in selected:
                    ordinal = offer.get("ordinal_send_slot")
                    if isinstance(ordinal, bool) or not isinstance(ordinal, int):
                        window_failures.append(f"{label} offer ordinal is invalid")
                        continue
                    scheduled_ns = start_ns + causal_offer_offset_ns(
                        window_id, ordinal
                    )
                    sent_ns = offer.get("sent_monotonic_ns")
                    expected_transaction_id = (
                        f"{window_id}:{expected_flow_group}:{uav}:{ordinal}"
                    )
                    if (
                        offer.get("transaction_id") != expected_transaction_id
                        or offer.get("phase") != window_id
                        or offer.get("transport_phase_code")
                        != transport_phase_code
                        or offer.get("flow_group_id") != expected_flow_group
                        or offer.get("scheduled_send_monotonic_ns") != scheduled_ns
                        or isinstance(sent_ns, bool)
                        or not isinstance(sent_ns, int)
                        or not scheduled_ns <= sent_ns < end_ns
                        or offer.get("send_lateness_ns") != sent_ns - scheduled_ns
                    ):
                        window_failures.append(
                            f"{label} offer {ordinal} timing/identity differs"
                        )
                        continue
                    result_records = results_by_transaction.get(
                        expected_transaction_id, []
                    )
                    if len(result_records) != 1:
                        continue
                    result = result_records[0]
                    completed_ns = result.get("completed_monotonic_ns")
                    if (
                        isinstance(completed_ns, bool)
                        or not isinstance(completed_ns, int)
                        or not sent_ns < completed_ns <= end_ns
                    ):
                        window_failures.append(
                            f"{label} offer {ordinal} completion time differs"
                        )
                        continue
                    policy = response_policies[label]
                    if policy == CORRELATED_TIMESYNC_POLICY:
                        if result.get("success") is True:
                            response = result.get("timesync_response")
                            if (
                                result.get("timed_out") is not False
                                or not isinstance(response, Mapping)
                                or response.get("timesync_ts1")
                                != offer.get("timesync_request_ts1")
                                or not sent_ns
                                <= int(response.get("received_monotonic_ns", -1))
                                < sent_ns + OUTCOME_TIMEOUT_NS
                            ):
                                window_failures.append(
                                    f"{label} offer {ordinal} lacks exact TIMESYNC echo"
                                )
                        elif (
                            result.get("success") is False
                            and result.get("timed_out") is True
                            and result.get("timesync_response") is None
                            and result.get("timeout_contract_satisfied") is True
                        ):
                            correlated_losses += 1
                        else:
                            window_failures.append(
                                f"{label} offer {ordinal} has invalid correlated outcome"
                            )
                    elif policy == "timeout_required":
                        if (
                            result.get("success") is not False
                            or result.get("timed_out") is not True
                            or result.get("ack") is not None
                            or result.get("requested_telemetry") is not None
                            or result.get("timeout_contract_satisfied") is not True
                        ):
                            window_failures.append(
                                f"{label} offer {ordinal} is not an exact timeout"
                            )
                    else:
                        window_failures.append(f"{label} response policy differs")

                if (
                    response_policies[label] == CORRELATED_TIMESYNC_POLICY
                    and correlated_losses * 100
                    > offered_per_uav * MAX_CORRELATED_LOSS_PERCENT
                ):
                    window_failures.append(
                        f"{label} correlated TIMESYNC loss exceeds five percent"
                    )

                heartbeat_records = [
                    heartbeat
                    for heartbeat in heartbeats
                    if heartbeat.get("uav") == uav
                    and isinstance(heartbeat.get("received_monotonic_ns"), int)
                    and not isinstance(heartbeat.get("received_monotonic_ns"), bool)
                    and start_ns
                    <= int(heartbeat["received_monotonic_ns"])
                    < end_ns
                ]
                minimum_heartbeats = (
                    3
                    if response_policies[label] == CORRELATED_TIMESYNC_POLICY
                    else 0
                )
                heartbeat_envelope_invalid = any(
                    heartbeat.get("event_sequence")
                    not in valid_heartbeat_sequences
                    for heartbeat in heartbeat_records
                )
                if len(heartbeat_records) < minimum_heartbeats or heartbeat_envelope_invalid:
                    window_failures.append(
                        f"{label} has fewer than three exact modeled-path heartbeats"
                    )
                if phase_heartbeat_counts[label] != len(heartbeat_records):
                    window_failures.append(
                        f"{label} phase heartbeat summary differs from raw window records"
                    )
                raw_ack_records = [
                    response
                    for response in raw_command_acks
                    if response.get("window_id") == window_id
                    and response.get("uav") == uav
                ]
                raw_telemetry_records = [
                    response
                    for response in raw_autopilot_versions
                    if response.get("window_id") == window_id
                    and response.get("uav") == uav
                ]
                if (
                    phase_raw_ack_counts[label] != len(raw_ack_records)
                    or phase_raw_telemetry_counts[label]
                    != len(raw_telemetry_records)
                    or (
                        response_policies[label] == CORRELATED_TIMESYNC_POLICY
                        and (
                            not raw_ack_records or not raw_telemetry_records
                        )
                    )
                    or (
                        response_policies[label] == "timeout_required"
                        and (raw_ack_records or raw_telemetry_records)
                    )
                ):
                    window_failures.append(
                        f"{label} raw ACK/AUTOPILOT_VERSION liveness differs"
                    )
                window_audit[label] = {
                    "offered": len(selected),
                    "results": sum(
                        len(results_by_transaction.get(str(offer.get("transaction_id")), []))
                        for offer in selected
                    ),
                    "raw_heartbeats": len(heartbeat_records),
                    "phase_heartbeat_count": phase_heartbeat_counts[label],
                    "raw_command_acks": len(raw_ack_records),
                    "raw_autopilot_versions": len(raw_telemetry_records),
                    "response_policy": response_policies[label],
                    "correlated_timesync_losses": correlated_losses,
                }
            audit[window_id] = window_audit
        except (KeyError, TypeError, ValueError, M4ValidationError) as exc:
            window_failures.append(str(exc))
        failures.extend(
            f"raw actual-control {window_id}: {failure}"
            for failure in window_failures
        )
    return normalized, audit, failures


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


def _exact_causal_packet_lineages(
    packet_records: list[dict[str, Any]],
    states_by_hash: Mapping[str, Mapping[str, Any]],
) -> tuple[
    dict[int, dict[str, Any]],
    dict[int, dict[str, Any]],
    dict[tuple[str, str, str], list[dict[str, Any]]],
    list[str],
]:
    """Bind every causal decision to one exact ns-3 UID-owned stage chain."""

    failures: list[str] = []
    epochs = {
        record.get("event_epoch")
        for record in packet_records
        if isinstance(record.get("event_epoch"), int)
        and not isinstance(record.get("event_epoch"), bool)
    }
    config_hashes = {
        record.get("config_sha256")
        for record in packet_records
        if isinstance(record.get("config_sha256"), str)
    }
    if (
        len(epochs) != 1
        or len(config_hashes) != 1
        or not config_hashes
        or HEX64.fullmatch(str(next(iter(config_hashes)))) is None
    ):
        return {}, {}, {}, ["causal ns-3 event epoch/config identity differs"]
    epoch = int(next(iter(epochs)))
    config_hash = str(next(iter(config_hashes)))
    try:
        deliveries = index_exact_ns3_unicast_deliveries(
            packet_records,
            expected_event_epoch=epoch,
            expected_config_sha256=config_hash,
            states_by_hash=states_by_hash,
        )
    except M4ValidationError as exc:
        return {}, {}, {}, [f"causal delivered ns-3 UID lineage differs: {exc}"]

    by_sequence = {
        int(record["event_sequence"]): record
        for record in packet_records
        if isinstance(record.get("event_sequence"), int)
        and not isinstance(record.get("event_sequence"), bool)
    }
    by_uid: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for record in packet_records:
        uid = record.get("packet_uid")
        if isinstance(uid, int) and not isinstance(uid, bool):
            by_uid[(epoch, uid)].append(record)

    by_decision_sequence: dict[int, dict[str, Any]] = {}
    by_route: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for chains in deliveries.values():
        for chain in chains:
            sequence = int(chain["enqueue"]["event_sequence"])
            if sequence in by_decision_sequence:
                failures.append("causal ns-3 decision sequence owns two UID chains")
            by_decision_sequence[sequence] = chain
            ingress = chain["ingress"]
            by_route[
                (
                    str(ingress.get("directed_link")),
                    str(ingress.get("traffic_class")),
                    str(ingress.get("transport_payload_sha256")),
                )
            ].append(chain)

    immutable_fields = {
        "event_epoch",
        "packet_uid",
        "tos",
        "dscp",
        "traffic_class",
        "directed_link",
        "queue_id",
        "source_ip",
        "destination_ip",
        "transport_protocol",
        "source_udp_port",
        "destination_udp_port",
        "transport_payload_sha256",
        "transport_payload_size",
        "p2mp",
        "root_transmission",
        "config_sha256",
        "seed",
        "run",
    }
    delivered_uid_keys = {
        tuple(chain["uid_key"])
        for chains in deliveries.values()
        for chain in chains
    }
    for uid_key, chain in by_uid.items():
        if uid_key in delivered_uid_keys or any(
            record.get("p2mp") is True for record in chain
        ):
            continue
        ingress = chain[0] if chain else {}
        if ingress.get("traffic_class") != "control":
            continue
        kinds = [record.get("event") for record in chain]
        if kinds != ["ingress", "drop"]:
            failures.append(
                f"causal dropped ns-3 UID chain shape differs: {uid_key}/{kinds}"
            )
            continue
        drop = chain[1]
        link = ingress.get("directed_link")
        if not isinstance(link, str) or link.count(">") != 1:
            failures.append(f"causal dropped ns-3 UID link differs: {uid_key}")
            continue
        source, _destination = link.split(">", 1)
        if (
            any(
                drop.get(field) != ingress.get(field)
                for field in immutable_fields
            )
            or [record.get("device_id") for record in chain]
            != [f"{source}.tap.ingress", f"{source}.radio"]
            or ingress.get("queue_id") != f"{link}.control.q0"
            or ingress.get("tos") != 184
            or ingress.get("dscp") != 46
            or ingress.get("transport_protocol") != 17
            or drop.get("radio_delivery") != "drop"
            or drop.get("radio_intervention") != "natural"
        ):
            failures.append(f"causal dropped ns-3 UID identity/path differs: {uid_key}")
            continue
        sequence = int(drop["event_sequence"])
        if sequence in by_decision_sequence:
            failures.append("causal ns-3 decision sequence owns two UID chains")
        by_decision_sequence[sequence] = {
            "uid_key": uid_key,
            "ingress": ingress,
            "drop": drop,
            "egress": None,
        }
    for chains in by_route.values():
        chains.sort(
            key=lambda chain: (
                int(chain["ingress"]["host_monotonic_ns"]),
                int(chain["ingress"]["event_sequence"]),
            )
        )
    return by_decision_sequence, by_sequence, dict(by_route), failures


def _validate_causal_packet_state_binding(
    decision: Mapping[str, Any],
    *,
    states_by_hash: Mapping[str, Mapping[str, Any]],
    packet_by_sequence: Mapping[int, Mapping[str, Any]],
) -> None:
    """Prove one causal ns-3 decision used the exact adapter-applied state."""

    state = states_by_hash.get(str(decision.get("radio_state_sha256")))
    status = decision.get("radio_state_status")
    if not isinstance(state, Mapping) or status not in {"fresh", "unavailable"}:
        raise M4ValidationError("causal decision state/status identity differs")
    source_sequence = state.get("source_packet_event_sequence")
    source = (
        packet_by_sequence.get(source_sequence)
        if isinstance(source_sequence, int) and not isinstance(source_sequence, bool)
        else None
    )
    if (
        not isinstance(source, Mapping)
        or source.get("event") != "ingress"
        or source.get("event_sequence", 1 << 63)
        >= decision.get("event_sequence", -1)
        or state.get("source_packet_event_epoch") != source.get("event_epoch")
        or state.get("source_packet_uid") != source.get("packet_uid")
        or state.get("source_packet_causal_sha256")
        != source.get("transport_payload_sha256")
        or state.get("directed_link") != source.get("directed_link")
        or state.get("traffic_class") != source.get("traffic_class")
        or state.get("directed_link") != decision.get("directed_link")
        or state.get("traffic_class") != decision.get("traffic_class")
    ):
        raise M4ValidationError("causal decision applied-state parent differs")
    if status == "unavailable":
        if (
            state.get("availability") != "unavailable"
            or decision.get("radio_delivery") != "drop"
            or decision.get("event") != "drop"
            or decision.get("drop_reason") != "sionna_state_unavailable"
        ):
            raise M4ValidationError("causal unavailable decision fields differ")
        return

    effects = state.get("effects")
    host = decision.get("host_monotonic_ns")
    validity_start = state.get("validity_start_monotonic_ns")
    expires = state.get("expires_monotonic_ns")
    applied = state.get("adapter_applied_monotonic_ns")
    if not isinstance(effects, Mapping) or state.get("availability") != "fresh":
        raise M4ValidationError("causal fresh decision state differs")
    if effects.get("intervention") != "natural":
        raise M4ValidationError("causal decision intervention is not natural")
    field_map = {
        "radio_state_sequence": state.get("state_sequence"),
        "radio_query_id": state.get("query_id"),
        "radio_applied_state_id": state.get("applied_state_id"),
        "radio_result_wire_sha256": state.get("result_wire_sha256"),
        "radio_mapping_version": effects.get("mapping_version"),
        "radio_mapping_seed": effects.get("mapping_seed"),
        "radio_delay_ns": effects.get("propagation_delay_ns"),
        "radio_service_rate_bps": effects.get("service_rate_bps"),
        "radio_loss_probability": effects.get("loss_probability"),
        "radio_intervention": effects.get("intervention"),
        "radio_validity_start_monotonic_ns": validity_start,
        "radio_adapter_applied_monotonic_ns": applied,
        "radio_expires_monotonic_ns": expires,
    }
    if any(decision.get(field) != expected for field, expected in field_map.items()):
        raise M4ValidationError("causal decision packet/state fields differ")
    if (
        isinstance(host, bool)
        or not isinstance(host, int)
        or isinstance(validity_start, bool)
        or not isinstance(validity_start, int)
        or isinstance(expires, bool)
        or not isinstance(expires, int)
        or isinstance(applied, bool)
        or not isinstance(applied, int)
        or not validity_start <= applied <= host < expires
        or decision.get("radio_state_age_ns") != host - validity_start
    ):
        raise M4ValidationError("causal decision state validity/time differs")
    digest = decision.get("transport_payload_sha256")
    applied_state_id = state.get("applied_state_id")
    mapping_seed = effects.get("mapping_seed")
    service_rate = effects.get("service_rate_bps")
    expected_sample = (
        0.0
        if service_rate == 0
        else deterministic_loss_sample(digest, applied_state_id, mapping_seed)
        if isinstance(digest, str)
        and isinstance(applied_state_id, str)
        and isinstance(mapping_seed, int)
        and not isinstance(mapping_seed, bool)
        else None
    )
    if (
        not isinstance(digest, str)
        or not isinstance(applied_state_id, str)
        or isinstance(mapping_seed, bool)
        or not isinstance(mapping_seed, int)
        or not finite_number(decision.get("radio_loss_sample"))
        or expected_sample is None
        or abs(
            float(decision["radio_loss_sample"])
            - expected_sample
        )
        > 1e-15
    ):
        raise M4ValidationError("causal decision deterministic loss sample differs")
    loss_probability = effects.get("loss_probability")
    if (
        not finite_number(loss_probability)
        or isinstance(service_rate, bool)
        or not isinstance(service_rate, int)
    ):
        raise M4ValidationError("causal decision mapped delivery inputs differ")
    expected_delivery = (
        "deliver"
        if service_rate > 0
        and float(expected_sample) >= float(loss_probability)
        else "drop"
    )
    expected_event = "enqueue" if expected_delivery == "deliver" else "drop"
    expected_drop_reason = (
        None
        if expected_delivery == "deliver"
        else "sionna_service_rate_zero"
        if service_rate == 0
        else "sionna_loss"
    )
    if (
        decision.get("radio_delivery") != expected_delivery
        or decision.get("event") != expected_event
        or decision.get("drop_reason") != expected_drop_reason
    ):
        raise M4ValidationError("causal decision deterministic delivery differs")


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


def _consume_causal_lineage_occurrence(
    chains: list[dict[str, Any]],
    cursors: dict[tuple[Any, ...], int],
    *,
    cursor_key: tuple[Any, ...],
    lower_ns: int,
    upper_ns: int,
) -> dict[str, Any] | None:
    """Consume one exact delivered UID chain by its egress occurrence time."""

    cursor = cursors.get(cursor_key, 0)
    while cursor < len(chains):
        timestamp = chains[cursor]["egress"].get("host_monotonic_ns")
        if (
            isinstance(timestamp, int)
            and not isinstance(timestamp, bool)
            and timestamp >= lower_ns
        ):
            break
        cursor += 1
    if cursor >= len(chains):
        cursors[cursor_key] = cursor
        return None
    timestamp = chains[cursor]["egress"].get("host_monotonic_ns")
    if (
        isinstance(timestamp, bool)
        or not isinstance(timestamp, int)
        or timestamp > upper_ns
    ):
        cursors[cursor_key] = cursor
        return None
    cursors[cursor_key] = cursor + 1
    return chains[cursor]


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
    decisions, _downstream, packet_failures = _packet_indexes(packet_records)
    failures.extend(packet_failures)
    (
        lineages_by_decision_sequence,
        packet_by_sequence,
        delivered_lineages_by_route,
        lineage_failures,
    ) = _exact_causal_packet_lineages(packet_records, states_by_hash)
    failures.extend(lineage_failures)
    decision_cursors: dict[tuple[Any, ...], int] = {}
    uplink_lineage_cursors: dict[tuple[Any, ...], int] = {}
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
                decision_sequence = decision.get("event_sequence")
                lineage = (
                    lineages_by_decision_sequence.get(decision_sequence)
                    if isinstance(decision_sequence, int)
                    and not isinstance(decision_sequence, bool)
                    else None
                )
                exact_decision = (
                    lineage.get("enqueue")
                    if isinstance(lineage, Mapping) and "enqueue" in lineage
                    else lineage.get("drop") if isinstance(lineage, Mapping) else None
                )
                if not isinstance(lineage, Mapping) or exact_decision != decision:
                    failures.append(
                        f"{window_id}/{role} decision lacks exact ns-3 UID chain: "
                        f"{transaction_id}"
                    )
                    continue
                ingress_ns = lineage.get("ingress", {}).get("host_monotonic_ns")
                if (
                    isinstance(ingress_ns, bool)
                    or not isinstance(ingress_ns, int)
                    or not offer_ns <= ingress_ns <= int(decision["host_monotonic_ns"])
                ):
                    failures.append(
                        f"{window_id}/{role} ns-3 ingress predates its actual offer: "
                        f"{transaction_id}"
                    )
                    continue
                try:
                    _validate_causal_packet_state_binding(
                        decision,
                        states_by_hash=states_by_hash,
                        packet_by_sequence=packet_by_sequence,
                    )
                except M4ValidationError as exc:
                    failures.append(
                        f"{window_id}/{role} exact ns-3 state binding differs: "
                        f"{transaction_id}: {exc}"
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
                echoes = [
                    item
                    for item in outcome_records
                    if item.get("event") == "ardupilot_timesync_echo"
                ]
                echo_ns = (
                    echoes[0].get("host_monotonic_ns")
                    if len(echoes) == 1
                    else None
                )
                egress = lineage.get("egress") if len(echoes) == 1 else None
                if isinstance(egress, Mapping) and (
                    not isinstance(echo_ns, int)
                    or isinstance(echo_ns, bool)
                    or not int(decision["host_monotonic_ns"])
                    <= int(egress.get("host_monotonic_ns", -1))
                    <= echo_ns
                ):
                    failures.append(
                        f"{window_id}/{role} exact ns-3 egress timing differs: "
                        f"{transaction_id}"
                    )
                    egress = None
                response_lineage = None
                response_digest = (
                    echoes[0].get("response_transport_payload_sha256")
                    if len(echoes) == 1
                    else None
                )
                response_uav = echoes[0].get("uav") if len(echoes) == 1 else None
                if (
                    isinstance(egress, Mapping)
                    and isinstance(echo_ns, int)
                    and not isinstance(echo_ns, bool)
                    and isinstance(response_digest, str)
                    and HEX64.fullmatch(response_digest) is not None
                    and isinstance(response_uav, str)
                    and re.fullmatch(r"uav[1-5]", response_uav) is not None
                ):
                    uplink_key = (
                        f"{response_uav}>cp",
                        "control",
                        response_digest,
                    )
                    response_lineage = _consume_causal_lineage_occurrence(
                        delivered_lineages_by_route.get(uplink_key, []),
                        uplink_lineage_cursors,
                        cursor_key=("uplink", *uplink_key),
                        lower_ns=int(egress["host_monotonic_ns"]),
                        upper_ns=echo_ns,
                    )
                complete = (
                    len(echoes) == 1
                    and egress is not None
                    and response_lineage is not None
                )
                if complete:
                    if (
                        not isinstance(echo_ns, int)
                        or isinstance(echo_ns, bool)
                        or not offer_ns <= echo_ns < offer_ns + OUTCOME_TIMEOUT_NS
                        or echoes[0].get("producer_role") != "arducopter"
                        or echoes[0].get("request_transport_payload_sha256")
                        != digest
                        or echoes[0].get("response_transport_payload_sha256")
                        != response_lineage["ingress"].get(
                            "transport_payload_sha256"
                        )
                    ):
                        failures.append(
                            f"{window_id}/{role} TIMESYNC echo outcome is invalid: {transaction_id}"
                        )
                    else:
                        delivered += 1
                        paired_sample["delivery"] = 1.0
                        latencies.append(float(echo_ns - offer_ns))
                elif echoes:
                    failures.append(
                        f"{window_id}/{role} has uncorrelated TIMESYNC outcome: {transaction_id}"
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
        minimum_slots = 20 if window_id == "expiry_unavailable" else 100
        if (
            len(set(expected_ids)) != 3
            or observed_ids != expected_ids
            or not _positive(groups["control"], minimum=minimum_slots)
            or not _positive(groups["background"], minimum=minimum_slots)
        ):
            failures.append(f"three distinct positive causal streams differ: {window_id}")
        concurrency = groups["concurrency"]
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
    exact_pair_counts: tuple[int, int] | None = None,
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
    if exact_pair_counts is None:
        if (
            left_keys != right_keys
            or len(left_keys) < 100
            or left_keys != set(range(1, len(left_keys) + 1))
        ):
            raise M4ValidationError(
                "paired ordinal set differs/is below 100: "
                f"{reference_window}/{observed_window}/{role}"
            )
        paired_ordinals = sorted(left_keys)
        pairing = "flow_group_id+ordinal_send_slot"
    else:
        reference_count, observed_count = exact_pair_counts
        expected_left = set(range(1, reference_count + 1))
        expected_right = set(range(1, observed_count + 1))
        if (
            reference_count < 20
            or observed_count < reference_count
            or left_keys != expected_left
            or right_keys != expected_right
        ):
            raise M4ValidationError(
                "paired ordinal contract is not exact "
                f"{reference_count}/{observed_count}: "
                f"{reference_window}/{observed_window}/{role}"
            )
        paired_ordinals = list(range(1, reference_count + 1))
        pairing = (
            "flow_group_id+ordinal_send_slot"
            f"[first_{reference_count}_of_{observed_count}]"
        )
    values: list[float] = []
    for ordinal in paired_ordinals:
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
        "pairing": pairing,
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
        exact_pair_counts: tuple[int, int] | None = None,
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
            exact_pair_counts=exact_pair_counts,
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
                    exact_pair_counts=(20, 100),
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

    pin_records = [
        record
        for record in records
        if record.get("event") == "causal_velocity_pin_ready"
    ]
    pin_ready: Mapping[str, Any] | None = None
    expected_pin_topics = {
        model: f"{CAUSAL_PIN_TOPIC_PREFIX}/{model}/cmd_vel"
        for model in CAUSAL_PIN_MODELS
    }
    first_transition_ns = min(
        (
            int(window["start_monotonic_ns"])
            - causal_pre_window_gap_ns(window_id)
            for window_id, window in windows.items()
            if isinstance(window, Mapping)
        ),
        default=0,
    )
    if len(pin_records) != 1:
        failures.append("causal zero-velocity pin readiness cardinality differs")
    else:
        candidate = pin_records[0]
        entity_ids = candidate.get("model_entity_ids")
        attached_ns = candidate.get("attached_monotonic_ns")
        initial_publish_count = candidate.get("initial_zero_publish_count")
        candidate_host_ns = candidate.get("host_monotonic_ns")
        if (
            candidate.get("models") != list(CAUSAL_PIN_MODELS)
            or not isinstance(entity_ids, Mapping)
            or set(entity_ids) != set(CAUSAL_PIN_MODELS)
            or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or value <= 0
                for value in entity_ids.values()
            )
            or len(set(entity_ids.values())) != len(CAUSAL_PIN_MODELS)
            or candidate.get("plugin_name") != CAUSAL_PIN_PLUGIN_NAME
            or candidate.get("plugin_filename") != CAUSAL_PIN_PLUGIN_FILENAME
            or candidate.get("system_add_service")
            != CAUSAL_PIN_SYSTEM_ADD_SERVICE
            or candidate.get("command_topics") != expected_pin_topics
            or candidate.get("preexisting_subscribers")
            != {model: False for model in CAUSAL_PIN_MODELS}
            or candidate.get("system_add_request_models")
            != list(CAUSAL_PIN_MODELS)
            or candidate.get("system_add_request_count")
            != len(CAUSAL_PIN_MODELS)
            or candidate.get("zero_linear_velocity_mps") != [0.0, 0.0, 0.0]
            or candidate.get("zero_angular_velocity_radps") != [0.0, 0.0, 0.0]
            or candidate.get("publish_period_ns")
            != CAUSAL_PIN_PUBLISH_PERIOD_NS
            or candidate.get("all_publishers_connected") is not True
            or isinstance(initial_publish_count, bool)
            or not isinstance(initial_publish_count, int)
            or initial_publish_count < len(CAUSAL_PIN_MODELS)
            or initial_publish_count % len(CAUSAL_PIN_MODELS) != 0
            or isinstance(attached_ns, bool)
            or not isinstance(attached_ns, int)
            or attached_ns <= 0
            or isinstance(candidate_host_ns, bool)
            or not isinstance(candidate_host_ns, int)
            or attached_ns > candidate_host_ns
            or candidate_host_ns >= first_transition_ns
        ):
            failures.append("causal zero-velocity pin readiness fields differ")
        else:
            pin_ready = candidate

    odometry_records = [
        record for record in records if record.get("event") == "odometry_sample"
    ]
    valid_odometry: list[dict[str, Any]] = []
    for number, record in enumerate(odometry_records, start=1):
        uav = record.get("uav")
        source_ns = record.get("source_callback_monotonic_ns")
        sim_ns = record.get("sim_stamp_ns")
        host_ns = record.get("host_monotonic_ns")
        position = record.get("position_m")
        orientation = record.get("orientation_quat_xyzw")
        linear = record.get("linear_velocity_mps")
        angular = record.get("angular_velocity_radps")
        vectors = (position, orientation, linear, angular)
        if (
            uav not in CAUSAL_PIN_MODELS
            or record.get("source_topic") != f"/{uav}/odometry"
            or record.get("source_frame") != ODOMETRY_SOURCE_FRAME
            or record.get("transform_version")
            != COORDINATE_TRANSFORM_VERSION
            or record.get("source_header_frame") != ODOMETRY_HEADER_FRAME
            or record.get("source_child_frame") != ODOMETRY_CHILD_FRAME
            or isinstance(source_ns, bool)
            or not isinstance(source_ns, int)
            or isinstance(sim_ns, bool)
            or not isinstance(sim_ns, int)
            or sim_ns < 0
            or not isinstance(host_ns, int)
            or not 0 <= host_ns - source_ns <= 50_000_000
            or any(not isinstance(value, list) for value in vectors)
            or len(position) != 3
            or len(orientation) != 4
            or len(linear) != 3
            or len(angular) != 3
            or any(
                not finite_number(value)
                for vector in vectors
                for value in vector
            )
            or any(
                abs(float(value)) > CAUSAL_OBSERVED_VELOCITY_LIMIT
                for vector in (linear, angular)
                for value in vector
            )
        ):
            failures.append(
                f"causal observed pinned odometry sample {number} differs"
            )
        else:
            valid_odometry.append(record)

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
        expected_stimulus_ns = start - causal_pre_window_gap_ns(window_id)
        previous_end = (
            int(windows[previous_window]["end_monotonic_ns"])
            if previous_window is not None
            and isinstance(windows.get(previous_window), Mapping)
            else None
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
            or stimulus.get("pose_vector_service")
            != CAUSAL_POSE_VECTOR_SERVICE
            or stimulus.get("pose_vector_models")
            != list(CAUSAL_POSE_VECTOR_MODELS)
            or stimulus.get("pose_vector_size")
            != len(CAUSAL_POSE_VECTOR_MODELS)
            or isinstance(stimulus.get("pose_apply_started_monotonic_ns"), bool)
            or not isinstance(
                stimulus.get("pose_apply_started_monotonic_ns"), int
            )
            or isinstance(
                stimulus.get("pose_apply_completed_monotonic_ns"), bool
            )
            or not isinstance(
                stimulus.get("pose_apply_completed_monotonic_ns"), int
            )
            or isinstance(stimulus.get("pose_apply_latency_ns"), bool)
            or not isinstance(stimulus.get("pose_apply_latency_ns"), int)
            or isinstance(
                stimulus.get("zero_velocity_published_monotonic_ns"), bool
            )
            or not isinstance(
                stimulus.get("zero_velocity_published_monotonic_ns"), int
            )
            or not expected_stimulus_ns
            <= stimulus["pose_apply_started_monotonic_ns"]
            <= stimulus["pose_apply_completed_monotonic_ns"]
            <= expected_stimulus_ns + CAUSAL_POSE_VECTOR_MAX_LATENCY_NS
            or stimulus["pose_apply_latency_ns"]
            != stimulus["pose_apply_completed_monotonic_ns"]
            - stimulus["pose_apply_started_monotonic_ns"]
            or not 0
            <= stimulus["pose_apply_latency_ns"]
            <= CAUSAL_POSE_VECTOR_MAX_LATENCY_NS
            or not stimulus["pose_apply_completed_monotonic_ns"]
            <= stimulus["zero_velocity_published_monotonic_ns"]
            <= stimulus["host_monotonic_ns"]
            or not expected_stimulus_ns
            <= stimulus["host_monotonic_ns"]
            <= expected_stimulus_ns + 500_000_000
            or (previous_end is not None and stimulus["host_monotonic_ns"] < previous_end)
            or stimulus["host_monotonic_ns"] >= drain["host_monotonic_ns"]
            or not start - 100_000_000
            <= drain["host_monotonic_ns"]
            < start
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
        for uav in CAUSAL_PIN_MODELS:
            pinned = sorted(
                (
                    record
                    for record in valid_odometry
                    if record.get("uav") == uav
                    and start <= int(record["host_monotonic_ns"]) < end
                ),
                key=lambda record: int(record["host_monotonic_ns"]),
            )
            pinned_hosts = [int(record["host_monotonic_ns"]) for record in pinned]
            if (
                not pinned_hosts
                or pinned_hosts[0] > start + CAUSAL_ODOMETRY_EDGE_NS
                or pinned_hosts[-1] < end - CAUSAL_ODOMETRY_EDGE_NS
                or any(
                    not 0
                    < pinned_hosts[index] - pinned_hosts[index - 1]
                    <= CAUSAL_ODOMETRY_MAX_GAP_NS
                    for index in range(1, len(pinned_hosts))
                )
            ):
                failures.append(
                    f"causal observed zero-velocity coverage differs: {window_id}/{uav}"
                )
        previous_window = window_id
    details = {
        "resource_sample_count": len(samples),
        "frozen_process_identity_count": len(frozen_processes or ()),
        "socket_identity_sha256": socket_hash,
        "validated_window_count": len(windows),
        "velocity_pin_ready": pin_ready is not None,
        "valid_pinned_odometry_sample_count": len(valid_odometry),
    }
    return details, failures


def validate_expiry_sequence(
    *,
    fault_records: list[dict[str, Any]],
    adapter_records: list[dict[str, Any]],
    control_records: list[dict[str, Any]],
    wire: Mapping[str, Any],
    windows: Mapping[str, Mapping[str, Any]],
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
        control_by_action: dict[str, Mapping[str, Any]] = {}
        for action in expected_actions:
            matches = [
                (index, record)
                for index, record in enumerate(controls)
                if record.get("action") == action
            ]
            if len(matches) != 1:
                raise M4ValidationError(f"expiry control {action} count differs")
            positions.append(matches[0][0])
            control_by_action[action] = matches[0][1]
        if positions != sorted(positions):
            raise M4ValidationError("expiry controls are out of order")

        expiry_window = windows.get("expiry_unavailable")
        recovery_window = windows.get("expiry_recovery")
        prearm_window = windows.get("jammer_off_2")
        if not isinstance(expiry_window, Mapping) or not isinstance(
            recovery_window, Mapping
        ) or not isinstance(prearm_window, Mapping):
            raise M4ValidationError("expiry prearm/unavailable/recovery windows are absent")
        prearm_start = int(prearm_window["start_monotonic_ns"])
        prearm_plan = causal_window_plan("jammer_off_2")
        offered = int(prearm_plan["offered_per_uav"])
        prior_seed_offer = prearm_start + causal_offer_offset_ns(
            "jammer_off_2", offered - 2
        )
        seed_offer = prearm_start + causal_offer_offset_ns(
            "jammer_off_2", offered - 1
        )
        last_prearm_offer = prearm_start + causal_offer_offset_ns(
            "jammer_off_2", offered
        )
        seed_arm_earliest = (
            prior_seed_offer + QUERY_DEADLINE_NS + EXPIRY_FAULT_ARM_SETTLE_NS
        )
        parallel_arm_earliest = (
            seed_offer + QUERY_DEADLINE_NS + EXPIRY_FAULT_ARM_SETTLE_NS
        )
        expiry_start = int(expiry_window["start_monotonic_ns"])
        expiry_end = int(expiry_window["end_monotonic_ns"])
        recovery_start = int(recovery_window["start_monotonic_ns"])
        control_times = {
            action: record.get("host_monotonic_ns")
            for action, record in control_by_action.items()
        }
        if (
            any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in control_times.values()
            )
            or not seed_arm_earliest
            <= int(control_times["arm_hold_next"])
            < seed_offer
            or not parallel_arm_earliest
            <= int(control_times["arm_fault_parallel_next"])
            < last_prearm_offer
            or not expiry_end
            <= int(control_times["release_held"])
            < int(control_times["inject_duplicate"])
            < recovery_start
        ):
            raise M4ValidationError(
                "expiry controls are outside setup/recovery gaps"
            )

        fault_by_event = {
            event: [record for record in fault_records if record.get("event") == event]
            for event in (
                "hold_armed",
                "real_result_held",
                "held_result_released",
                "byte_identical_duplicate_released",
            )
        }
        if any(len(records) != 1 for records in fault_by_event.values()):
            raise M4ValidationError("expiry fault event counts differ")
        hold_armed = fault_by_event["hold_armed"][0]
        held = fault_by_event["real_result_held"][0]
        released = fault_by_event["held_result_released"][0]
        duplicate = fault_by_event["byte_identical_duplicate_released"][0]
        if (
            hold_armed.get("directed_link_id") != target_directed_link_id
            or not seed_offer
            <= int(hold_armed.get("monotonic_ns", -1))
            < int(held.get("monotonic_ns", -1))
            < int(control_times["arm_fault_parallel_next"])
            or not int(control_times["arm_hold_next"])
            < int(hold_armed.get("monotonic_ns", -1))
        ):
            raise M4ValidationError(
                "expiry seed was not armed/submitted/held on its exact factual slot"
            )
        if (
            held.get("directed_link_id") != target_directed_link_id
            or released.get("query_id") != held.get("query_id")
            or released.get("result_wire_sha256") != held.get("result_wire_sha256")
            or not isinstance(
                control_by_action["arm_fault_parallel_next"].get("detail"),
                Mapping,
            )
            or control_by_action["arm_fault_parallel_next"]["detail"].get(
                "held_query_id"
            )
            != held.get("query_id")
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

        staged_queries = [
            record
            for record in adapter_records
            if record.get("event") == "query_submitted"
            and record.get("directed_link") == target_packet_link
            and record.get("traffic_class") == "control"
            and record.get("decision") in {"fault_seed", "fault_parallel"}
        ]
        seed_queries = [
            record for record in staged_queries if record.get("decision") == "fault_seed"
        ]
        parallel_queries = [
            record
            for record in staged_queries
            if record.get("decision") == "fault_parallel"
        ]
        if (
            len(seed_queries) != 1
            or len(parallel_queries) != 1
            or seed_queries[0].get("query_id") != held.get("query_id")
            or not int(hold_armed.get("monotonic_ns", -1))
            <= int(seed_queries[0].get("monotonic_ns", -1))
            < int(held.get("monotonic_ns", -1))
            or not int(control_times["arm_fault_parallel_next"])
            < int(parallel_queries[0].get("monotonic_ns", -1))
            or int(parallel_queries[0].get("monotonic_ns", -1))
            < last_prearm_offer
            or not isinstance(seed_queries[0].get("packet_event_sequence"), int)
            or not isinstance(parallel_queries[0].get("packet_event_sequence"), int)
            or seed_queries[0]["packet_event_sequence"]
            >= parallel_queries[0]["packet_event_sequence"]
        ):
            raise M4ValidationError(
                "expiry staged queries are not two ordered factual submissions"
            )

        expired = [
            record
            for record in adapter_records
            if record.get("event") == "state_expired"
            and record.get("directed_link") == target_packet_link
            and record.get("traffic_class") == "control"
            and isinstance(record.get("monotonic_ns"), int)
            and not isinstance(record.get("monotonic_ns"), bool)
            and last_prearm_offer < record["monotonic_ns"] < expiry_start
        ]
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
        if len(expired) != 1 or len(superseded) != 1 or len(duplicate_rejections) != 1:
            raise M4ValidationError("expiry/old/duplicate adapter rejection evidence differs")
        expiry_time = expired[0]["monotonic_ns"]
        if (
            expired[0].get("query_id") != parallel_queries[0].get("query_id")
            or not last_prearm_offer < expiry_time < expiry_start
        ):
            raise M4ValidationError(
                "fault-parallel state did not expire after the last positive offer"
            )
        newer = [
            record
            for record in applied
            if isinstance(record.get("monotonic_ns"), int)
            and record.get("query_id") == expired[0].get("query_id")
            and held.get("monotonic_ns", 0)
            < record["monotonic_ns"]
            < expiry_time
        ]
        fresh_after_fault = [
            record
            for record in applied
            if isinstance(record.get("monotonic_ns"), int)
            and record["monotonic_ns"] > duplicate_rejections[0]["monotonic_ns"]
            and record["monotonic_ns"] < recovery_start
            and record.get("query_id") != duplicate.get("query_id")
        ]
        if not newer:
            raise M4ValidationError(
                "newer real result did not apply and expire before unavailable traffic"
            )
        if (
            released.get("monotonic_ns", 0) < expiry_end
            or released.get("monotonic_ns", 0) >= recovery_start
            or duplicate.get("monotonic_ns", 0) <= released.get("monotonic_ns", 0)
            or duplicate.get("monotonic_ns", 0) >= recovery_start
        ):
            raise M4ValidationError(
                "old release/duplicate did not stay inside the recovery gap"
            )
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
        run.get("schema_version") != 2
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
    try:
        capacity_prerequisite = strict_json(
            run_dir / "raw/prerequisites/m4_capacity_prerequisite.json"
        )
        gate_failures["run_identity"].extend(
            validate_capacity_prerequisite_version(capacity_prerequisite)
        )
    except M4ValidationError as exc:
        gate_failures["run_identity"].append(str(exc))
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
        if key not in {"messages", "message_by_hash"}
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
        binding_details, binding_failures = validate_query_pose_runtime_binding(
            pose_records,
            wire,
            runtime_records,
            run_dir / "logs/m4_pose_observations.jsonl.gz",
            run_id=str(run_id),
            runtime_id=str(runtime_id),
            start_monotonic_ns=pose_start,
            end_monotonic_ns=pose_end,
        )
        gate_failures["pose_lineage"].extend(binding_failures)
        poses["runtime_binding"] = binding_details
    except M4ValidationError as exc:
        gate_failures["pose_lineage"].append(str(exc))
    details["pose_lineage"] = poses
    actual_control_audit: dict[str, Any] = {}
    try:
        packet_records = strict_jsonl(run_dir / "logs/ns3_packet_events.jsonl")
        actual_control_records = strict_jsonl(
            run_dir / "raw/actual_control/events.jsonl",
            max_line_bytes=2 * 1024 * 1024,
        )
        (
            transaction_records,
            actual_control_audit,
            normalization_failures,
        ) = normalize_actual_control_transactions(
            actual_control_records,
            run,
            windows,
        )
        gate_failures["actual_control_evidence"].extend(normalization_failures)
        details["actual_control_evidence"] = actual_control_audit
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
        gate_failures["actual_control_evidence"].append(
            f"actual-control evidence is unavailable: {exc}"
        )
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
            windows=windows,
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
        "actual_control_evidence",
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
