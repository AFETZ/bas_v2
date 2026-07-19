#!/usr/bin/env python3
"""Adversarial tests for M4 causal-window and F-expiry derivation."""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from network.scripts.m4_adapter_runtime import apply_control  # noqa: E402
from network.radio_provider.sionna_packet_adapter import (  # noqa: E402
    deterministic_loss_sample,
)
from network.radio_provider.sionna_async import node_state_sha256  # noqa: E402
from network.scripts.actual_sitl_control_probe import (  # noqa: E402
    ControlProbeError,
    MavlinkSequencer,
    encode_m4_correlated_control_request,
    heartbeat_counts_for_window,
    mavlink_v2_frame,
    transport_nonce32,
)
from network.scripts.m4_runtime_orchestrator import (  # noqa: E402
    ACTUAL_SITL_AUDIT_LOG_PATHS,
)
from network.validation.m4_common import M4ValidationError  # noqa: E402
from network.validation.m4_pose_observations import (  # noqa: E402
    PoseObservationWriter,
    scan_pose_observation_stream,
)
from network.validation.m4_runtime import (  # noqa: E402
    QUERY_DEADLINE_NS,
    validate_native_world_entity_observations,
    validate_query_pose_runtime_binding,
)
from network.validation.validate_m4_causality import (  # noqa: E402
    BACKGROUND_CELL_ID,
    CAUSAL_PIN_MODELS,
    CAUSAL_PIN_PLUGIN_FILENAME,
    CAUSAL_PIN_PLUGIN_NAME,
    CAUSAL_PIN_PUBLISH_PERIOD_NS,
    CAUSAL_PIN_SYSTEM_ADD_SERVICE,
    CAUSAL_PIN_TOPIC_PREFIX,
    CAUSAL_POSE_VECTOR_MODELS,
    CAUSAL_POSE_VECTOR_MAX_LATENCY_NS,
    CAUSAL_POSE_VECTOR_SERVICE,
    CAUSAL_MEASUREMENT_SPAN_NS,
    CAUSAL_SOURCE_PATHS,
    CORRELATED_TIMESYNC_POLICY,
    EXPIRY_FAULT_ARM_SETTLE_NS,
    EXPIRY_SETUP_GAP_NS,
    FINALIZATION_BUDGET_NS,
    NS3_ENGINE_DURATION_NS,
    PHYSICAL_DOWN_SETUP_GAP_NS,
    PRECONTRACT_SETUP_BUDGET_NS,
    REQUIRED_WRAPPER_RESERVE_NS,
    RESULT_CONTRACT,
    RUN_CONTRACT,
    RUNTIME_READINESS_BUDGET_NS,
    WRAPPER_TIMEOUT_NS,
    WINDOW_IDS,
    WINDOW_SHAPES,
    _consume_causal_packet_occurrence,
    _packet_indexes,
    causal_offer_offset_ns,
    causal_pre_window_gap_ns,
    causal_quiet_drain_map,
    causal_response_policies,
    causal_window_plan,
    derive_causal_window_metrics,
    matrix_flow_group_identity,
    normalize_actual_control_transactions,
    validate_causal_effects,
    validate_causal_execution_budget,
    validate_causal_pose_geometry,
    validate_causal_runtime,
    validate_capacity_prerequisite_version,
    validate_concurrent_flow_groups,
    validate_expiry_sequence,
    validate_paired_causality,
    validate_window_manifest,
)

TEST_ENDPOINT_FORM = "actual_sitl_mavproxy_udp_tail"
TEST_MATRIX_SHA256 = hashlib.sha256(
    (ROOT / "network/config/endpoint_matrix_5uav.json").read_bytes()
).hexdigest()


def window_manifest() -> list[dict[str, object]]:
    records = []
    start = 400_000_000_000
    for window_id in WINDOW_IDS:
        scenario, phase, target_cell, control_cell = WINDOW_SHAPES[window_id]
        target = matrix_flow_group_identity(target_cell, TEST_ENDPOINT_FORM)
        control = matrix_flow_group_identity(control_cell, TEST_ENDPOINT_FORM)
        background = matrix_flow_group_identity(BACKGROUND_CELL_ID, TEST_ENDPOINT_FORM)
        plan = causal_window_plan(window_id)
        if window_id.startswith("terrain_"):
            pose_set = window_id
        elif window_id.startswith("building_"):
            pose_set = window_id
        elif window_id.startswith("jammer_"):
            pose_set = "jammer_pose"
        else:
            pose_set = "terrain_good"
        records.append(
            {
                "window_id": window_id,
                "scenario": scenario,
                "phase": phase,
                "control_endpoint_form": TEST_ENDPOINT_FORM,
                "endpoint_matrix_sha256": TEST_MATRIX_SHA256,
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
                "transport_phase_code": len(records) + 1,
                "offered_per_uav": plan["offered_per_uav"],
                "send_span_ms": plan["send_span_ms"],
                "expected_engine_state": "up_epoch_1",
                "response_policies": causal_response_policies(window_id),
                "minimum_quiet_drain_ns_by_uav": causal_quiet_drain_map(
                    window_id
                ),
                "start_monotonic_ns": start,
                "end_monotonic_ns": start + plan["duration_ns"],
                "pose_set": pose_set,
                "jammer_enabled": window_id == "jammer_on",
                "jammer_on_classification": (
                    "positive_impaired" if window_id.startswith("jammer_") else None
                ),
            }
        )
        start += plan["duration_ns"]
        next_index = len(records)
        if next_index < len(WINDOW_IDS):
            start += causal_pre_window_gap_ns(WINDOW_IDS[next_index])
    return records


def positive_group(
    *,
    sinr: float = 20.0,
    js: float = -20.0,
    tier: int = 20_000_000,
    delivery: float = 1.0,
    offered: int = 100,
) -> dict[str, object]:
    delivered = int(round(offered * delivery))
    return {
        "offered_unique": offered,
        "delivered_unique": delivered,
        "delivery_ratio": delivered / offered,
        "unavailable_decisions": 0,
        "fresh_physical_samples": offered,
        "median_sinr_db": sinr,
        "median_js_db": js,
        "median_service_tier_bps": tier,
        "state_age_p95_ns": 100_000_000.0,
        "delivered_latency_p95_ns": 200_000_000.0 if delivered else None,
        "delivered_jitter_ns": 1_000_000.0 if delivered else None,
    }


def passing_metrics() -> dict[str, dict[str, dict[str, object]]]:
    good = positive_group()
    metrics: dict[str, dict[str, dict[str, object]]] = {}
    for prefix in ("terrain", "building"):
        metrics[f"{prefix}_good"] = {
            "target": copy.deepcopy(good),
            "control": copy.deepcopy(good),
            "background": copy.deepcopy(good),
            "concurrency": {
                "group_count": 3,
                "concurrent_ordinal_slots": 100,
                "maximum_offer_skew_ns": 0,
            },
        }
        down_target = positive_group(sinr=15.0, tier=2_000_000, delivery=0.0)
        metrics[f"{prefix}_down"] = {
            "target": down_target,
            "control": copy.deepcopy(good),
            "background": copy.deepcopy(good),
            "concurrency": {
                "group_count": 3,
                "concurrent_ordinal_slots": 100,
                "maximum_offer_skew_ns": 0,
            },
        }
        metrics[f"{prefix}_recovery"] = {
            "target": copy.deepcopy(good),
            "control": copy.deepcopy(good),
            "background": copy.deepcopy(good),
            "concurrency": {
                "group_count": 3,
                "concurrent_ordinal_slots": 100,
                "maximum_offer_skew_ns": 0,
            },
        }
    metrics["jammer_off_1"] = {
        "target": copy.deepcopy(good),
        "control": copy.deepcopy(good),
        "background": copy.deepcopy(good),
        "concurrency": {
            "group_count": 3,
            "concurrent_ordinal_slots": 100,
            "maximum_offer_skew_ns": 0,
        },
    }
    metrics["jammer_on"] = {
        "target": positive_group(
            sinr=15.0, js=-15.0, tier=2_000_000, delivery=0.8
        ),
        "control": copy.deepcopy(good),
        "background": copy.deepcopy(good),
        "concurrency": {
            "group_count": 3,
            "concurrent_ordinal_slots": 100,
            "maximum_offer_skew_ns": 0,
        },
    }
    metrics["jammer_off_2"] = {
        "target": copy.deepcopy(good),
        "control": copy.deepcopy(good),
        "background": copy.deepcopy(good),
        "concurrency": {
            "group_count": 3,
            "concurrent_ordinal_slots": 100,
            "maximum_offer_skew_ns": 0,
        },
    }
    unavailable = positive_group(delivery=0.0, offered=20)
    unavailable["fresh_physical_samples"] = 0
    unavailable["median_sinr_db"] = None
    unavailable["median_js_db"] = None
    unavailable["median_service_tier_bps"] = None
    unavailable["state_age_p95_ns"] = None
    unavailable["unavailable_decisions"] = 20
    metrics["expiry_unavailable"] = {
        "target": unavailable,
        "control": positive_group(offered=20),
        "background": positive_group(offered=20),
        "concurrency": {
            "group_count": 3,
            "concurrent_ordinal_slots": 20,
            "maximum_offer_skew_ns": 0,
        },
    }
    metrics["expiry_recovery"] = {
        "target": copy.deepcopy(good),
        "control": copy.deepcopy(good),
        "background": copy.deepcopy(good),
        "concurrency": {
            "group_count": 3,
            "concurrent_ordinal_slots": 100,
            "maximum_offer_skew_ns": 0,
        },
    }
    manifests = {record["window_id"]: record for record in window_manifest()}
    for window_id, groups in metrics.items():
        for role in ("target", "control", "background"):
            groups[role]["flow_group_id"] = manifests[window_id][
                f"{role}_flow_group_id"
            ]
    return metrics


def paired_passing_metrics() -> dict[str, dict[str, dict[str, object]]]:
    metrics = passing_metrics()
    for window_id, groups in metrics.items():
        for role, group in groups.items():
            if role == "concurrency":
                continue
            group["flow_group_id"] = window_manifest()[WINDOW_IDS.index(window_id)][
                f"{role}_flow_group_id"
            ]
            delivered = int(group["delivered_unique"])
            group["paired_samples"] = {
                ordinal: {
                    "delivery": 1.0 if ordinal <= delivered else 0.0,
                    "sinr_db": group["median_sinr_db"],
                    "js_db": group["median_js_db"],
                    "service_tier_index": (
                        None
                        if group["median_service_tier_bps"] is None
                        else (
                            0,
                            1_000,
                            10_000,
                            100_000,
                            500_000,
                            2_000_000,
                            20_000_000,
                        ).index(group["median_service_tier_bps"])
                    ),
                }
                for ordinal in range(1, int(group["offered_unique"]) + 1)
            }
    return metrics


def runtime_fixture(windows: dict[str, dict[str, object]]) -> list[dict[str, object]]:
    process = {
        "pid": 101,
        "start_ticks": 1001,
        "pgid": 101,
        "role": "test_role",
        "executable_path": "/qualified/test",
        "executable_sha256": "1" * 64,
        "cmdline_sha256": "2" * 64,
    }
    records: list[dict[str, object]] = []
    first_start = int(windows[WINDOW_IDS[0]]["start_monotonic_ns"])
    last_end = int(windows[WINDOW_IDS[-1]]["end_monotonic_ns"])
    sample_hosts = set(
        range(first_start - 10_000_000_000, last_end, 1_000_000_000)
    )
    sample_hosts.update(
        int(windows[window_id]["start_monotonic_ns"]) for window_id in WINDOW_IDS
    )
    sample_hosts.update(
        int(windows[window_id]["start_monotonic_ns"]) - 10_000_000_000
        for window_id in WINDOW_IDS
    )
    for host in sorted(sample_hosts):
        records.append(
            {
                "host_monotonic_ns": host,
                "event": "causal_resource_sample",
                "scheduled_monotonic_ns": host,
                "processes": {
                    "counts": {"test_role": 1},
                    "required_counts": {"test_role": 1},
                    "roles_exact": True,
                    "unclassified_count": 0,
                    "processes": [copy.deepcopy(process)],
                },
                "sockets": {
                    "ready": True,
                    "identity_sha256": "3" * 64,
                    "unexpected": [],
                },
                "captures": {"ready": True, "kernel_drops": 0},
                "queues": {"bounded": True, "hidden_drops": 0},
                "readiness": {
                    "ready": True,
                    "clocks": True,
                    "odometry": True,
                    "poses": True,
                    "provider": True,
                    "adapter": True,
                    "ns3": True,
                    "endpoints": True,
                    "captures": True,
                    "topology": True,
                },
            }
        )
    prior = None
    records.append(
        {
            "host_monotonic_ns": first_start - 20_000_000_000,
            "event": "causal_velocity_pin_ready",
            "models": list(CAUSAL_PIN_MODELS),
            "model_entity_ids": {
                model: index
                for index, model in enumerate(CAUSAL_PIN_MODELS, start=101)
            },
            "plugin_name": CAUSAL_PIN_PLUGIN_NAME,
            "plugin_filename": CAUSAL_PIN_PLUGIN_FILENAME,
            "system_add_service": CAUSAL_PIN_SYSTEM_ADD_SERVICE,
            "command_topics": {
                model: f"{CAUSAL_PIN_TOPIC_PREFIX}/{model}/cmd_vel"
                for model in CAUSAL_PIN_MODELS
            },
            "preexisting_subscribers": {
                model: False for model in CAUSAL_PIN_MODELS
            },
            "system_add_request_models": list(CAUSAL_PIN_MODELS),
            "system_add_request_count": len(CAUSAL_PIN_MODELS),
            "zero_linear_velocity_mps": [0.0, 0.0, 0.0],
            "zero_angular_velocity_radps": [0.0, 0.0, 0.0],
            "publish_period_ns": CAUSAL_PIN_PUBLISH_PERIOD_NS,
            "all_publishers_connected": True,
            "initial_zero_publish_count": 20,
            "attached_monotonic_ns": first_start - 21_000_000_000,
        }
    )
    predicate = {
        "good": "fresh_state_applied",
        "down": "fresh_physical_down_state_applied",
        "recovery": "fresh_state_applied",
        "off-1": "fresh_state_applied",
        "on": "fresh_jammer_state_applied",
        "off-2": "fresh_state_applied",
        "unavailable": "state_expired",
    }
    for sequence, window_id in enumerate(WINDOW_IDS, start=1):
        window = windows[window_id]
        start = int(window["start_monotonic_ns"])
        end = int(window["end_monotonic_ns"])
        # Every offer has already reached a terminal outcome at least 100 ms
        # before the half-open window boundary.  This leaves a bounded phase
        # transition/drain interval even where two windows are contiguous.
        stimulus_host = (
            start - causal_pre_window_gap_ns(window_id) + 10_000_000
        )
        drain_host = start - 50_000_000
        records.extend(
            (
                {
                    "host_monotonic_ns": stimulus_host,
                    "event": "window_stimulus_applied",
                    "window_id": window_id,
                    "state_predicate": (
                        "fresh_state_applied_after_fault_removed"
                        if window_id == "expiry_recovery"
                        else predicate[str(window["phase"])]
                    ),
                    "target_packet_link": window["target_link"],
                    "pose_fixture_sequence": sequence,
                    "pose_vector_service": CAUSAL_POSE_VECTOR_SERVICE,
                    "pose_vector_models": list(CAUSAL_POSE_VECTOR_MODELS),
                    "pose_vector_size": len(CAUSAL_POSE_VECTOR_MODELS),
                    "pose_apply_started_monotonic_ns": stimulus_host - 5_000_000,
                    "pose_apply_completed_monotonic_ns": stimulus_host - 2_000_000,
                    "pose_apply_latency_ns": 3_000_000,
                    "zero_velocity_published_monotonic_ns": stimulus_host - 1_000_000,
                },
                {
                    "host_monotonic_ns": drain_host,
                    "event": "window_drain_complete",
                    "next_window_id": window_id,
                    "prior_window_id": prior,
                    "terminal_outcomes_complete": True,
                    "queue_depths": {
                        "userspace": 0,
                        "ns3": 0,
                        "qdisc": 0,
                        "capture_pending": 0,
                    },
                },
                {
                    "host_monotonic_ns": start + 10_000_000,
                    "event": "window_measurement_start",
                    "window_id": window_id,
                    "target_monotonic_ns": start,
                },
                {
                    "host_monotonic_ns": end + 1_000_000,
                    "event": "window_measurement_end",
                    "window_id": window_id,
                    "target_monotonic_ns": end,
                },
            )
        )
        for sample_ns in range(start + 20_000_000, end, 500_000_000):
            for uav_index, uav in enumerate(CAUSAL_PIN_MODELS, start=1):
                host_ns = sample_ns + uav_index
                records.append(
                    {
                        "host_monotonic_ns": host_ns,
                        "event": "odometry_sample",
                        "uav": uav,
                        "source_topic": f"/{uav}/odometry",
                        "source_frame": "ros_odometry_world_enu",
                        "transform_version": "ams-m4-coordinate-frames-v1",
                        "source_header_frame": "odom",
                        "source_child_frame": "base_link",
                        "source_callback_monotonic_ns": host_ns,
                        "sim_stamp_ns": host_ns,
                        "position_m": [0.0, 0.0, 100.0],
                        "orientation_quat_xyzw": [0.0, 0.0, 0.0, 1.0],
                        "linear_velocity_mps": [0.0, 0.0, 0.0],
                        "angular_velocity_radps": [0.0, 0.0, 0.0],
                    }
                )
        prior = window_id
    records.sort(key=lambda item: int(item["host_monotonic_ns"]))
    for index, record in enumerate(records, start=1):
        record["schema"] = "ams.m4.runtime_event/v1"
        record["event_sequence"] = index
    return records


def pose_fixture(windows: dict[str, dict[str, object]]) -> list[dict[str, object]]:
    bundle = json.loads(
        (ROOT / "network/config/m4_canonical_scene_bundle.json").read_text()
    )
    records: list[dict[str, object]] = []
    sequence = 0
    for window_id in WINDOW_IDS:
        window = windows[window_id]
        scenario = str(window["scenario"])
        if scenario in {"terrain_shadow", "building_blocked"}:
            expected = bundle["causal_scenarios"][scenario]["pose_sets"][
                window["pose_set"]
            ]
        elif scenario == "jammer_off_on_off":
            expected = bundle["causal_scenarios"][scenario]["pose_set"]
        else:
            expected = bundle["causal_scenarios"]["terrain_shadow"]["pose_sets"][
                "terrain_good"
            ]
        for host in range(
            int(window["start_monotonic_ns"]),
            int(window["end_monotonic_ns"]),
            1_000_000_000,
        ):
            sequence += 1
            records.append(
                {
                    "snapshot_monotonic_ns": host,
                    "nodes": [
                        {"node_id": node, "position_m": list(expected[node])}
                        for node in ("cp", "uav1", "uav2", "uav3", "uav4", "uav5")
                    ],
                    "jammers": [
                        {
                            "jammer_id": "jammer_m4",
                            "position_m": list(expected["jammer_m4"]),
                            "enabled": bool(window["jammer_enabled"]),
                        }
                    ],
                    "pose_sequence": sequence,
                }
            )
    return records


def write_pose_observation_fixture(
    path: Path,
    observations: list[dict[str, object]],
    *,
    run_id: str = "pose-binding-run",
    runtime_id: str = "pose-binding-runtime",
) -> None:
    writer = PoseObservationWriter(
        path,
        run_id,
        runtime_id,
        created_monotonic_ns=9_000_000_000,
    )
    for observation in observations:
        writer.emit(**observation)
    last_callback_ns = max(
        (int(item["source_callback_monotonic_ns"]) for item in observations),
        default=9_000_000_000,
    )
    writer.close(closed_monotonic_ns=last_callback_ns + 1_000_000)


def query_pose_runtime_binding_fixture(path: Path) -> tuple[
    list[dict[str, object]],
    dict[str, list[dict[str, object]]],
    list[dict[str, object]],
    list[dict[str, object]],
]:
    """One query snapshot plus an independent collector observation per entity."""

    base_host_ns = 10_000_000_000
    sim_offset_ns = 2_000_000_000
    snapshot_ns = base_host_ns + 50_000_000
    sent_ns = base_host_ns + 60_000_000
    positions = {
        "cp": [-8_000.0, -2_500.0, 300.0],
        "uav1": [-1_000.0, 0.0, 100.0],
        "uav2": [-500.0, 0.0, 100.0],
        "uav3": [0.0, 0.0, 100.0],
        "uav4": [500.0, 0.0, 100.0],
        "uav5": [1_000.0, 0.0, 100.0],
        "jammer_m4": [2_000.0, -3_000.0, 100.0],
    }
    orientation = [0.0, 0.0, 0.0, 1.0]
    raw_nodes: list[dict[str, object]] = []
    raw_jammers: list[dict[str, object]] = []
    runtime: list[dict[str, object]] = []
    observations: list[dict[str, object]] = []

    for clock_host_ns in (
        base_host_ns - 100_000_000,
        base_host_ns,
        base_host_ns + 100_000_000,
        base_host_ns + 200_000_000,
    ):
        runtime.append(
            {
                "event": "gazebo_clock_sample",
                "host_monotonic_ns": clock_host_ns + 1_000,
                "source_callback_monotonic_ns": clock_host_ns,
                "clock_topic": "/uav1/clock",
                "sim_time_ns": clock_host_ns - sim_offset_ns,
            }
        )

    for ordinal, entity in enumerate(
        ("cp", "uav1", "uav2", "uav3", "uav4", "uav5", "jammer_m4")
    ):
        adapter_callback_ns = base_host_ns + 10_000_000 + ordinal * 1_000_000
        collector_callback_ns = base_host_ns + 20_000_000 + ordinal * 1_000_000
        source_stamp_ns = adapter_callback_ns - sim_offset_ns - 5_000_000
        world_entity = entity in {"cp", "jammer_m4"}
        source_topic = (
            "/world/map/pose/info" if world_entity else f"/{entity}/odometry"
        )
        source_header_frame = "" if world_entity else "odom"
        source_child_frame = entity if world_entity else "base_link"
        raw_entity: dict[str, object] = {
            "pose_monotonic_ns": adapter_callback_ns,
            "source_topic": source_topic,
            "source_header_stamp_ns": source_stamp_ns,
            "source_transport": (
                "gazebo_transport_pose_v" if world_entity else "ros2_dds_odometry"
            ),
            "source_stamp_scope": (
                "pose_v_top_level_header" if world_entity else "ros_header"
            ),
            "source_header_frame": source_header_frame,
            "source_child_frame": source_child_frame,
            "source_frame": "world",
            "transform_version": "enu-identity-v1",
            "position_m": list(positions[entity]),
            "orientation_quat_xyzw": list(orientation),
            "freshness_age_ns": snapshot_ns - adapter_callback_ns,
            "stale": False,
        }
        if entity == "jammer_m4":
            raw_entity.update(
                {
                    "jammer_id": entity,
                    "enabled": False,
                    "center_frequency_hz": 2_437_000_000.0,
                    "bandwidth_hz": 20_000_000.0,
                    "power_dbm": 20.0,
                    "duty_cycle": 1.0,
                    "antenna_pattern": "isotropic",
                }
            )
            raw_jammers.append(raw_entity)
        else:
            raw_entity.update(
                {
                    "node_id": entity,
                    "role": "command_post" if entity == "cp" else "uav",
                }
            )
            raw_nodes.append(raw_entity)

        observation: dict[str, object] = {
            "kind": "w" if world_entity else "o",
            "entity_id": entity,
            "source_callback_monotonic_ns": collector_callback_ns,
            "source_topic": source_topic,
            "source_transport": (
                "gazebo_transport_pose_v" if world_entity else "ros2_dds_odometry"
            ),
            "source_stamp_scope": (
                "pose_v_top_level_header" if world_entity else "ros_header"
            ),
            "source_frame": "world" if world_entity else "ros_odometry_world_enu",
            "transform_version": (
                "enu-identity-v1"
                if world_entity
                else "ams-m4-coordinate-frames-v1"
            ),
            "source_header_frame": source_header_frame,
            "source_child_frame": source_child_frame,
            "sim_stamp_ns": source_stamp_ns,
            "position_m": list(positions[entity]),
            "orientation_quat_xyzw": list(orientation),
        }
        observations.append(observation)

    def query_form(entity: dict[str, object]) -> dict[str, object]:
        result = copy.deepcopy(entity)
        for key in (
            "source_header_stamp_ns",
            "source_header_frame",
            "source_child_frame",
            "source_transport",
            "source_stamp_scope",
        ):
            result.pop(key)
        result["freshness_age_ns"] = sent_ns - int(result["pose_monotonic_ns"])
        return result

    query_nodes = [query_form(item) for item in raw_nodes]
    query_jammers = [query_form(item) for item in raw_jammers]
    digest = node_state_sha256(
        node_state_seq=1,
        snapshot_monotonic_ns=snapshot_ns,
        source_frame="world",
        transform_version="enu-identity-v1",
        nodes=query_nodes,
        jammers=query_jammers,
    )
    pose_records: list[dict[str, object]] = [
        {
            "schema": "ams.m4.pose_snapshot/v2",
            "pose_sequence": 1,
            "node_state_seq": 1,
            "node_state_sha256": digest,
            "snapshot_monotonic_ns": snapshot_ns,
            "host_monotonic_ns": snapshot_ns,
            "source_frame": "world",
            "transform_version": "enu-identity-v1",
            "nodes": raw_nodes,
            "jammers": raw_jammers,
        }
    ]
    wire = {
        "messages": [
            {
                "message_type": "query",
                "query_id": "pose-binding.query.1",
                "request_sent_monotonic_ns": sent_ns,
                "node_state_seq": 1,
                "node_state_sha256": digest,
                "node_state_snapshot_monotonic_ns": snapshot_ns,
                "source_frame": "world",
                "transform_version": "enu-identity-v1",
                "nodes": query_nodes,
                "jammers": query_jammers,
            }
        ]
    }
    write_pose_observation_fixture(path, observations)
    _matches, stream_details = scan_pose_observation_stream(
        path,
        run_id="pose-binding-run",
        runtime_id="pose-binding-runtime",
        required_keys=set(),
    )
    runtime.extend(
        (
            {
                "event": "collector_start",
                "host_monotonic_ns": 9_100_000_000,
                "pose_observation_stream": {
                    "path": "logs/m4_pose_observations.jsonl.gz",
                    "schema": "ams.m4.pose_observation_stream/v1",
                    "encoding": "gzip-jsonl-compact-v1",
                    "created_monotonic_ns": stream_details[
                        "created_monotonic_ns"
                    ],
                    "main_odometry_sample_period_ns": 200_000_000,
                },
            },
            {
                "event": "collector_stop",
                "host_monotonic_ns": base_host_ns + 300_000_000,
                "pose_observation_count": stream_details["observation_count"],
                "pose_observation_content_sha256": stream_details[
                    "content_sha256"
                ],
                "pose_observation_closed_monotonic_ns": stream_details[
                    "closed_monotonic_ns"
                ],
            },
        )
    )
    runtime.sort(key=lambda item: int(item["host_monotonic_ns"]))
    for sequence, record in enumerate(runtime, start=1):
        record["schema"] = "ams.m4.runtime_event/v1"
        record["event_sequence"] = sequence
    return pose_records, wire, runtime, observations


def rehash_query_pose_binding(
    pose_records: list[dict[str, object]],
    wire: dict[str, list[dict[str, object]]],
) -> None:
    raw = pose_records[0]
    message = wire["messages"][0]

    def query_form(entity: dict[str, object]) -> dict[str, object]:
        result = copy.deepcopy(entity)
        for key in (
            "source_header_stamp_ns",
            "source_header_frame",
            "source_child_frame",
            "source_transport",
            "source_stamp_scope",
        ):
            result.pop(key)
        result["freshness_age_ns"] = int(message["request_sent_monotonic_ns"]) - int(
            result["pose_monotonic_ns"]
        )
        return result

    query_nodes = [query_form(item) for item in raw["nodes"]]
    query_jammers = [query_form(item) for item in raw["jammers"]]
    digest = node_state_sha256(
        node_state_seq=int(raw["node_state_seq"]),
        snapshot_monotonic_ns=int(raw["snapshot_monotonic_ns"]),
        source_frame=str(raw["source_frame"]),
        transform_version=str(raw["transform_version"]),
        nodes=query_nodes,
        jammers=query_jammers,
    )
    raw["node_state_sha256"] = digest
    message["node_state_sha256"] = digest
    message["nodes"] = query_nodes
    message["jammers"] = query_jammers


class CausalWindowTests(unittest.TestCase):
    def test_breaking_causality_and_capacity_prerequisite_versions_are_frozen(
        self,
    ) -> None:
        self.assertEqual(RUN_CONTRACT, "ams.m4.causality_run/v2")
        self.assertEqual(RESULT_CONTRACT, "ams.m4.causality-validation/v2")
        receipt = {
            "contract": "ams.m4-capacity.host-final-receipt/v2",
            "result_contract": "ams.m4-capacity.validation/v2",
            "formal_accepted": True,
            "passed": True,
            "result": {
                "contract": "ams.m4-capacity.validation/v2",
                "profile": "m4_capacity_prerequisite",
                "passed": True,
            },
        }
        self.assertEqual(validate_capacity_prerequisite_version(receipt), [])
        for field, legacy in (
            ("contract", "ams.m4-capacity.host-final-receipt/v1"),
            ("result_contract", "ams.m4-capacity.validation/v1"),
        ):
            with self.subTest(field=field):
                mutated = copy.deepcopy(receipt)
                mutated[field] = legacy
                self.assertTrue(validate_capacity_prerequisite_version(mutated))

    def test_exact_manifest_and_quantitative_effects_pass(self) -> None:
        windows, failures = validate_window_manifest(
            window_manifest(),
            control_endpoint_form=TEST_ENDPOINT_FORM,
            endpoint_matrix_sha256=TEST_MATRIX_SHA256,
        )
        self.assertEqual(failures, [])
        self.assertEqual(tuple(windows), WINDOW_IDS)
        self.assertEqual(validate_causal_effects(windows, passing_metrics()), [])
        self.assertEqual(
            int(windows[WINDOW_IDS[-1]]["end_monotonic_ns"])
            - int(windows[WINDOW_IDS[0]]["start_monotonic_ns"]),
            CAUSAL_MEASUREMENT_SPAN_NS,
        )

    def test_mixed_timeout_policy_and_recovery_drain_are_exact(self) -> None:
        manifest = window_manifest()
        terrain_down = manifest[WINDOW_IDS.index("terrain_down")]
        self.assertEqual(
            terrain_down["response_policies"],
            {
                "uav1": "timeout_required",
                "uav2": CORRELATED_TIMESYNC_POLICY,
                "uav3": CORRELATED_TIMESYNC_POLICY,
                "uav4": CORRELATED_TIMESYNC_POLICY,
                "uav5": CORRELATED_TIMESYNC_POLICY,
            },
        )
        terrain_down["response_policies"]["uav5"] = "timeout_required"
        _windows, failures = validate_window_manifest(
            manifest,
            control_endpoint_form=TEST_ENDPOINT_FORM,
            endpoint_matrix_sha256=TEST_MATRIX_SHA256,
        )
        self.assertTrue(any("identity/time differs: terrain_down" in item for item in failures))

        manifest = window_manifest()
        recovery = manifest[WINDOW_IDS.index("terrain_recovery")]
        recovery["minimum_quiet_drain_ns_by_uav"]["uav1"] = 0
        _windows, failures = validate_window_manifest(
            manifest,
            control_endpoint_form=TEST_ENDPOINT_FORM,
            endpoint_matrix_sha256=TEST_MATRIX_SHA256,
        )
        self.assertTrue(
            any("identity/time differs: terrain_recovery" in item for item in failures)
        )

    def test_timeout_schedule_and_exact_recovery_gap_cannot_shrink(self) -> None:
        manifest = window_manifest()
        down_index = WINDOW_IDS.index("building_down")
        manifest[down_index]["send_span_ms"] = 296_900
        _windows, failures = validate_window_manifest(
            manifest,
            control_endpoint_form=TEST_ENDPOINT_FORM,
            endpoint_matrix_sha256=TEST_MATRIX_SHA256,
        )
        self.assertTrue(any("identity/time differs: building_down" in item for item in failures))

        manifest = window_manifest()
        recovery_index = WINDOW_IDS.index("building_recovery")
        manifest[recovery_index]["start_monotonic_ns"] -= 1
        manifest[recovery_index]["end_monotonic_ns"] -= 1
        _windows, failures = validate_window_manifest(
            manifest,
            control_endpoint_form=TEST_ENDPOINT_FORM,
            endpoint_matrix_sha256=TEST_MATRIX_SHA256,
        )
        self.assertTrue(
            any("identity/time differs: building_recovery" in item for item in failures)
        )

        manifest = window_manifest()
        terrain_down_index = WINDOW_IDS.index("terrain_down")
        self.assertEqual(
            int(manifest[terrain_down_index]["start_monotonic_ns"])
            - int(manifest[terrain_down_index - 1]["end_monotonic_ns"]),
            PHYSICAL_DOWN_SETUP_GAP_NS,
        )
        manifest[terrain_down_index]["start_monotonic_ns"] -= 1
        manifest[terrain_down_index]["end_monotonic_ns"] -= 1
        _windows, failures = validate_window_manifest(
            manifest,
            control_endpoint_form=TEST_ENDPOINT_FORM,
            endpoint_matrix_sha256=TEST_MATRIX_SHA256,
        )
        self.assertTrue(
            any("identity/time differs: terrain_down" in item for item in failures)
        )

        for window_id in (
            "building_good",
            "jammer_off_1",
            "jammer_on",
            "jammer_off_2",
        ):
            manifest = window_manifest()
            index = WINDOW_IDS.index(window_id)
            self.assertEqual(
                int(manifest[index]["start_monotonic_ns"])
                - int(manifest[index - 1]["end_monotonic_ns"]),
                PHYSICAL_DOWN_SETUP_GAP_NS,
            )
        self.assertEqual(
            causal_pre_window_gap_ns("terrain_good"),
            PHYSICAL_DOWN_SETUP_GAP_NS,
        )

        manifest = window_manifest()
        expiry_index = WINDOW_IDS.index("expiry_unavailable")
        self.assertEqual(
            int(manifest[expiry_index]["start_monotonic_ns"])
            - int(manifest[expiry_index - 1]["end_monotonic_ns"]),
            EXPIRY_SETUP_GAP_NS,
        )
        positive_slot_ns = causal_offer_offset_ns(
            "jammer_off_2", 100
        ) - causal_offer_offset_ns("jammer_off_2", 99)
        self.assertEqual(positive_slot_ns, 270_707_071)
        positive_tail_ns = 30_000_000_000 - 26_800_000_000
        self.assertEqual(positive_tail_ns - 3_000_000_000, 200_000_000)
        seed_arm_ns = (
            causal_offer_offset_ns("jammer_off_2", 98)
            + QUERY_DEADLINE_NS
            + EXPIRY_FAULT_ARM_SETTLE_NS
        )
        parallel_arm_ns = (
            causal_offer_offset_ns("jammer_off_2", 99)
            + QUERY_DEADLINE_NS
            + EXPIRY_FAULT_ARM_SETTLE_NS
        )
        self.assertLess(seed_arm_ns, causal_offer_offset_ns("jammer_off_2", 99))
        self.assertLess(
            parallel_arm_ns, causal_offer_offset_ns("jammer_off_2", 100)
        )
        self.assertEqual(
            causal_offer_offset_ns("jammer_off_2", 99) - seed_arm_ns,
            120_707_071,
        )
        self.assertEqual(
            causal_offer_offset_ns("jammer_off_2", 100) - parallel_arm_ns,
            120_707_071,
        )

    def test_1500_second_execution_budget_covers_runtime_engine_and_reserve(self) -> None:
        windows = {record["window_id"]: record for record in window_manifest()}
        created = (
            int(windows[WINDOW_IDS[0]]["start_monotonic_ns"])
            - RUNTIME_READINESS_BUDGET_NS
        )
        planned = (
            PRECONTRACT_SETUP_BUDGET_NS
            + RUNTIME_READINESS_BUDGET_NS
            + CAUSAL_MEASUREMENT_SPAN_NS
            + FINALIZATION_BUDGET_NS
            + REQUIRED_WRAPPER_RESERVE_NS
        )
        run = {
            "runner_start_monotonic_ns": created - PRECONTRACT_SETUP_BUDGET_NS,
            "created_monotonic_ns": created,
            "execution_budget": {
                "wrapper_timeout_ns": WRAPPER_TIMEOUT_NS,
                "precontract_setup_budget_ns": PRECONTRACT_SETUP_BUDGET_NS,
                "runtime_readiness_budget_ns": RUNTIME_READINESS_BUDGET_NS,
                "causal_measurement_span_ns": CAUSAL_MEASUREMENT_SPAN_NS,
                "finalization_budget_ns": FINALIZATION_BUDGET_NS,
                "required_wrapper_reserve_ns": REQUIRED_WRAPPER_RESERVE_NS,
                "planned_total_ns": planned,
                "unallocated_margin_ns": WRAPPER_TIMEOUT_NS - planned,
                "ns3_engine_duration_ns": NS3_ENGINE_DURATION_NS,
                "ns3_required_runtime_ns": (
                    RUNTIME_READINESS_BUDGET_NS
                    + CAUSAL_MEASUREMENT_SPAN_NS
                    + FINALIZATION_BUDGET_NS
                ),
                "ns3_unallocated_margin_ns": (
                    NS3_ENGINE_DURATION_NS
                    - RUNTIME_READINESS_BUDGET_NS
                    - CAUSAL_MEASUREMENT_SPAN_NS
                    - FINALIZATION_BUDGET_NS
                ),
            },
        }
        details, failures = validate_causal_execution_budget(run, windows)
        self.assertEqual(failures, [])
        self.assertEqual(details["planned_total_ns"], 1_415_100_000_000)
        self.assertEqual(details["unallocated_margin_ns"], 84_900_000_000)
        self.assertEqual(details["ns3_required_runtime_ns"], 1_175_100_000_000)
        self.assertEqual(details["ns3_unallocated_margin_ns"], 74_900_000_000)
        self.assertEqual(
            RUNTIME_READINESS_BUDGET_NS - 10_000_000_000,
            150_000_000_000,
        )

        run["run_id"] = "run"
        run["runtime_id"] = "runtime"
        last_end = int(windows[WINDOW_IDS[-1]]["end_monotonic_ns"])
        finalization = {
            "contract": "ams.m4.causal-finalization-timing/v1",
            "run_id": "run",
            "runtime_id": "runtime",
            "last_window_end_monotonic_ns": last_end,
            "evidence_finalized_monotonic_ns": last_end + FINALIZATION_BUDGET_NS,
            "elapsed_ns": FINALIZATION_BUDGET_NS,
            "budget_ns": FINALIZATION_BUDGET_NS,
        }
        _details, failures = validate_causal_execution_budget(
            run, windows, finalization=finalization
        )
        self.assertEqual(failures, [])
        finalization["evidence_finalized_monotonic_ns"] += 1
        finalization["elapsed_ns"] += 1
        _details, failures = validate_causal_execution_budget(
            run, windows, finalization=finalization
        )
        self.assertTrue(any("finalization exceeded" in item for item in failures))

        run["execution_budget"]["required_wrapper_reserve_ns"] -= 1
        run["runner_start_monotonic_ns"] -= 1
        _details, failures = validate_causal_execution_budget(run, windows)
        self.assertTrue(any("execution budget differs" in item for item in failures))
        self.assertTrue(any("precontract setup exceeded" in item for item in failures))

    def test_formal_runner_and_shared_actual_stack_launcher_are_static_coherent(self) -> None:
        runner = ROOT / "network/scripts/run_m4_causality.sh"
        capacity_runner = ROOT / "network/scripts/run_m4_capacity.sh"
        stack = ROOT / "network/scripts/actual_sitl_stack_orchestrator.sh"
        phase_driver = ROOT / "network/scripts/m4_causal_phase_driver.py"
        collector = ROOT / "network/scripts/collect_m4_runtime.py"
        validator = ROOT / "network/validation/validate_m4_causality.py"
        for path in (runner, capacity_runner, stack, phase_driver):
            self.assertTrue(path.stat().st_mode & 0o111, path)
        for path in (runner, capacity_runner, stack):
            completed = subprocess.run(
                ["bash", "-n", str(path)], capture_output=True, text=True, check=False
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
        source = runner.read_text(encoding="utf-8")
        self.assertIn("initialize-causality", source)
        self.assertIn("--profile m4_causality", source)
        self.assertIn("actual_sitl_stack_orchestrator.sh", source)
        self.assertIn("--fault-enabled", source)
        self.assertIn("DURATION_MS=1250000", source)
        self.assertIn("--mavproxy-streamrate 1", source)
        self.assertIn("--mavproxy-streamrate 1", capacity_runner.read_text())
        self.assertIn('mavproxy_streamrate:="$MAVPROXY_STREAMRATE"', stack.read_text())
        profiles = json.loads(
            (ROOT / "network/config/component_acceptance_profiles.json").read_text()
        )["profiles"]
        self.assertEqual(profiles["m4_component"]["timeout_s"], 1500)
        self.assertEqual(
            profiles["m4_component"]["receipt_contract"],
            "ams.m4.host-final-receipt/v2",
        )
        self.assertEqual(
            profiles["m4_component"]["result_contract"],
            "ams.m4.causality-validation/v2",
        )
        phase_source = phase_driver.read_text(encoding="utf-8")
        self.assertIn('"ams.m4.causality_run/v2"', phase_source)
        self.assertNotIn("start_ns + 5_000_000_000", phase_source)
        self.assertNotIn("start_ns + 7_000_000_000", phase_source)
        self.assertIn("end_ns + 100_000_000", phase_source)
        self.assertIn("EXPIRY_FAULT_ARM_SETTLE_NS", phase_source)
        self.assertIn("causal_offer_offset_ns", phase_source)
        self.assertIn(
            "transition_ns = start_ns - causal_pre_window_gap_ns(window_id)",
            phase_source,
        )
        self.assertEqual(CAUSAL_POSE_VECTOR_MAX_LATENCY_NS, 250_000_000)
        for token in (
            "has_connections()",
            "publish_zero_velocity",
            "CAUSAL_POSE_VECTOR_MAX_LATENCY_NS",
            "CAUSAL_POSE_VECTOR_SERVICE",
            "CAUSAL_PIN_SYSTEM_ADD_SERVICE",
            "CAUSAL_PIN_PLUGIN_FILENAME",
            "pending_fault_arms",
            "pose_refresh_guard_ns",
        ):
            self.assertIn(token, phase_source)
        self.assertNotIn('"/world/map/set_pose"', phase_source)
        self.assertNotIn("client.set_pose(", phase_source)
        iris_source = (
            ROOT / "src/multiagent_simulation/models/iris_radio_headless/model.sdf"
        ).read_text(encoding="utf-8")
        self.assertNotIn("VelocityControl", iris_source)
        validator_source = validator.read_text(encoding="utf-8")
        for token in (
            'CAUSAL_POSE_VECTOR_SERVICE = "/world/map/set_pose_vector/blocking"',
            'CAUSAL_PIN_SYSTEM_ADD_SERVICE = "/world/map/entity/system/add"',
            'CAUSAL_PIN_PLUGIN_FILENAME = "gz-sim-velocity-control-system"',
        ):
            self.assertIn(token, validator_source)
        self.assertNotIn(
            'CAUSAL_POSE_VECTOR_SERVICE = "/world/map/set_pose"',
            validator_source,
        )
        collector_source = collector.read_text(encoding="utf-8")
        self.assertIn("linear_velocity_mps=", collector_source)
        self.assertIn("angular_velocity_radps=", collector_source)
        self.assertNotIn("technical_synthetic_fixture", source)
        capacity_source = capacity_runner.read_text(encoding="utf-8")
        for token in (
            "actual_sitl_stack_orchestrator.sh",
            "actual_sitl_control_probe.py",
            "--profile m4_capacity",
            "tail-root-uav$index",
            "m3_topology_monitor.py",
            "ACTUAL_STACK_STOPPED",
        ):
            self.assertIn(token, capacity_source)
        self.assertNotIn("actual M3/SITL endpoint API is not frozen", capacity_source)
        self.assertEqual(
            ACTUAL_SITL_AUDIT_LOG_PATHS,
            {
                "logs/actual_sitl_supervisor.jsonl",
                *(f"logs/actual_sitl_uav{index}.jsonl" for index in range(1, 6)),
            },
        )
        self.assertTrue(
            {
                "network/scripts/run_m4_causality.sh",
                "network/scripts/actual_sitl_stack_orchestrator.sh",
                "network/scripts/m4_causal_phase_driver.py",
            }.issubset(CAUSAL_SOURCE_PATHS)
        )

    def test_overlap_and_posthoc_jammer_classification_fail(self) -> None:
        manifest = window_manifest()
        manifest[1]["start_monotonic_ns"] = manifest[0]["end_monotonic_ns"] - 1
        manifest[7]["jammer_on_classification"] = "expected_down"
        _windows, failures = validate_window_manifest(
            manifest,
            control_endpoint_form=TEST_ENDPOINT_FORM,
            endpoint_matrix_sha256=TEST_MATRIX_SHA256,
        )
        self.assertTrue(any("identity/time" in item for item in failures))
        self.assertTrue(any("not frozen consistently" in item for item in failures))

    def test_label_only_flow_id_and_missing_third_stream_fail(self) -> None:
        manifest = window_manifest()
        manifest[0]["target_flow_group_id"] = "terrain_shadow.target.control"
        _windows, failures = validate_window_manifest(
            manifest,
            control_endpoint_form=TEST_ENDPOINT_FORM,
            endpoint_matrix_sha256=TEST_MATRIX_SHA256,
        )
        self.assertTrue(any("identity/time differs" in item for item in failures))

        windows, failures = validate_window_manifest(
            window_manifest(),
            control_endpoint_form=TEST_ENDPOINT_FORM,
            endpoint_matrix_sha256=TEST_MATRIX_SHA256,
        )
        self.assertEqual(failures, [])
        metrics = passing_metrics()
        del metrics["terrain_good"]["background"]
        observed = validate_concurrent_flow_groups(windows, metrics)
        self.assertTrue(any("raw metric roles differ" in item for item in observed))

    def test_expiry_short_window_requires_exactly_20_positive_side_streams(
        self,
    ) -> None:
        windows, failures = validate_window_manifest(
            window_manifest(),
            control_endpoint_form=TEST_ENDPOINT_FORM,
            endpoint_matrix_sha256=TEST_MATRIX_SHA256,
        )
        self.assertEqual(failures, [])
        metrics = passing_metrics()
        self.assertEqual(validate_concurrent_flow_groups(windows, metrics), [])
        metrics["expiry_unavailable"]["control"]["offered_unique"] = 19
        metrics["expiry_unavailable"]["control"]["fresh_physical_samples"] = 19
        failures = validate_concurrent_flow_groups(windows, metrics)
        self.assertTrue(
            any(
                "three distinct positive causal streams differ: expiry_unavailable"
                in item
                for item in failures
            )
        )

    def test_endpoint_form_is_imported_byte_exact_before_flow_derivation(self) -> None:
        _windows, failures = validate_window_manifest(
            window_manifest(),
            control_endpoint_form="different_endpoint_form",
            endpoint_matrix_sha256=TEST_MATRIX_SHA256,
        )
        self.assertTrue(any("identity/time differs" in item for item in failures))
        _windows, failures = validate_window_manifest(window_manifest())
        self.assertEqual(
            failures, ["accepted M3 control endpoint_form is absent/invalid"]
        )
        _windows, failures = validate_window_manifest(
            window_manifest(),
            control_endpoint_form=TEST_ENDPOINT_FORM,
            endpoint_matrix_sha256="f" * 64,
        )
        self.assertEqual(
            failures, ["accepted M3 endpoint matrix SHA-256 is absent/different"]
        )

    def test_observed_gazebo_pose_geometry_is_continuous_and_canonical(self) -> None:
        windows, failures = validate_window_manifest(
            window_manifest(),
            control_endpoint_form=TEST_ENDPOINT_FORM,
            endpoint_matrix_sha256=TEST_MATRIX_SHA256,
        )
        self.assertEqual(failures, [])
        bundle = json.loads(
            (ROOT / "network/config/m4_canonical_scene_bundle.json").read_text()
        )
        records = pose_fixture(windows)
        details, failures = validate_causal_pose_geometry(records, windows, bundle)
        self.assertEqual(failures, [])
        self.assertEqual(details["validated_window_count"], len(WINDOW_IDS))

        records[0]["nodes"][1]["position_m"][0] += 2.0
        _details, failures = validate_causal_pose_geometry(records, windows, bundle)
        self.assertTrue(any("outside canonical 1-m" in item for item in failures))

    def test_each_query_pose_is_bound_to_independent_runtime_and_clock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pose.jsonl.gz"
            pose_records, wire, runtime, _observations = (
                query_pose_runtime_binding_fixture(path)
            )
            details, failures = validate_query_pose_runtime_binding(
                pose_records,
                wire,
                runtime,
                path,
                run_id="pose-binding-run",
                runtime_id="pose-binding-runtime",
            )
        self.assertEqual(failures, [])
        self.assertEqual(details["referenced_snapshot_count"], 1)
        self.assertEqual(details["bound_entity_count"], 7)
        self.assertEqual(details["clock_interpolation_count"], 14)
        self.assertEqual(details["observation_count"], 7)
        self.assertEqual(details["matching_observation_count"], 7)

    def test_runtime_entities_require_exact_native_gazebo_lineage(self) -> None:
        entities = {
            entity: {
                "last_host_ns": 10_000_000_000 + index,
                "sim_stamp_ns": 8_000_000_000,
                "source_topic": "/world/map/pose/info",
                "source_transport": "gazebo_transport_pose_v",
                "source_stamp_scope": "pose_v_top_level_header",
                "source_frame": "world",
                "transform_version": "enu-identity-v1",
                "source_header_frame": "",
                "source_child_frame": entity,
                "position_m": [float(index), 0.0, 100.0],
                "orientation_quat_xyzw": [0.0, 0.0, 0.0, 1.0],
            }
            for index, entity in enumerate(
                ("cp", "uav1", "uav2", "uav3", "uav4", "uav5", "jammer_m4")
            )
        }
        self.assertEqual(validate_native_world_entity_observations(entities), [])
        for field, forged in (
            ("source_topic", "/world/foreign/pose/info"),
            ("source_transport", "ros2_tf_bridge"),
            ("source_stamp_scope", "per_pose_header"),
            ("source_frame", "map"),
            ("transform_version", "forged-v1"),
            ("source_header_frame", "world"),
            ("source_child_frame", "cp"),
        ):
            with self.subTest(field=field):
                mutated = copy.deepcopy(entities)
                mutated["uav3"][field] = forged
                self.assertEqual(
                    validate_native_world_entity_observations(mutated),
                    ["active Gazebo entity evidence differs: uav3"],
                )

    def test_pose_binding_uses_precomputed_capacity_scale_indexes(self) -> None:
        source = (ROOT / "network/validation/m4_runtime.py").read_text()
        stream = (
            ROOT / "network/validation/m4_pose_observations.py"
        ).read_text()
        self.assertIn("scan_pose_observation_stream(", source)
        self.assertIn("if key in required_keys:", stream)
        self.assertIn("clock_hosts = [sample[0] for sample in clocks]", source)
        self.assertIn("bisect.bisect_left(clock_hosts, callback_ns)", source)

    def test_raw_source_stamp_without_independent_observation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pose.jsonl.gz"
            pose_records, wire, runtime, _observations = (
                query_pose_runtime_binding_fixture(path)
            )
            pose_records[0]["nodes"][3]["source_header_stamp_ns"] += 1
            _details, failures = validate_query_pose_runtime_binding(
                pose_records,
                wire,
                runtime,
                path,
                run_id="pose-binding-run",
                runtime_id="pose-binding-runtime",
            )
        self.assertTrue(
            any("uav3 has no exact independent runtime pose sample" in item for item in failures),
            failures,
        )

    def test_self_consistent_pose_forgery_cannot_replace_collector_pose(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pose.jsonl.gz"
            pose_records, wire, runtime, _observations = (
                query_pose_runtime_binding_fixture(path)
            )
            pose_records[0]["nodes"][5]["position_m"][0] += 0.25
            rehash_query_pose_binding(pose_records, wire)
            _details, failures = validate_query_pose_runtime_binding(
                pose_records,
                wire,
                runtime,
                path,
                run_id="pose-binding-run",
                runtime_id="pose-binding-runtime",
            )
        self.assertTrue(
            any("uav5 has no exact independent runtime pose sample" in item for item in failures),
            failures,
        )

    def test_matching_forged_stamp_is_rejected_by_gazebo_clock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pose.jsonl.gz"
            pose_records, wire, runtime, observations = (
                query_pose_runtime_binding_fixture(path)
            )
            forged_stamp_ns = int(
                pose_records[0]["nodes"][2]["source_header_stamp_ns"]
            ) + 5_000_000_000
            pose_records[0]["nodes"][2]["source_header_stamp_ns"] = forged_stamp_ns
            next(
                record
                for record in observations
                if record.get("entity_id") == "uav2"
            )["sim_stamp_ns"] = forged_stamp_ns
            forged_path = Path(directory) / "forged.jsonl.gz"
            write_pose_observation_fixture(forged_path, observations)
            _details, failures = validate_query_pose_runtime_binding(
                pose_records,
                wire,
                runtime,
                forged_path,
                run_id="pose-binding-run",
                runtime_id="pose-binding-runtime",
            )
        self.assertTrue(
            any("uav2 source stamp is inconsistent with Gazebo clock" in item for item in failures),
            failures,
        )

    def test_world_entity_requires_independent_world_pose_sample(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pose.jsonl.gz"
            pose_records, wire, runtime, observations = (
                query_pose_runtime_binding_fixture(path)
            )
            missing_path = Path(directory) / "missing.jsonl.gz"
            write_pose_observation_fixture(
                missing_path,
                [item for item in observations if item.get("entity_id") != "cp"],
            )
            _details, failures = validate_query_pose_runtime_binding(
                pose_records,
                wire,
                runtime,
                missing_path,
                run_id="pose-binding-run",
                runtime_id="pose-binding-runtime",
            )
        self.assertTrue(
            any("cp has no exact independent runtime pose sample" in item for item in failures),
            failures,
        )

    def test_pose_stream_identity_digest_and_clean_footer_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "pose.jsonl.gz"
            _poses, _wire, _runtime, _observations = (
                query_pose_runtime_binding_fixture(path)
            )
            with self.assertRaisesRegex(
                M4ValidationError, "header schema/identity differs"
            ):
                scan_pose_observation_stream(
                    path,
                    run_id="foreign-run",
                    runtime_id="pose-binding-runtime",
                    required_keys=set(),
                )

            truncated = root / "truncated.jsonl.gz"
            truncated.write_bytes(path.read_bytes()[:-8])
            with self.assertRaises(M4ValidationError):
                scan_pose_observation_stream(
                    truncated,
                    run_id="pose-binding-run",
                    runtime_id="pose-binding-runtime",
                    required_keys=set(),
                )

            lines = gzip.decompress(path.read_bytes()).splitlines(keepends=True)
            footer = json.loads(lines[-1])
            footer["content_sha256"] = "0" * 64
            lines[-1] = (
                json.dumps(footer, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode()
            forged = root / "forged-footer.jsonl.gz"
            forged.write_bytes(
                gzip.compress(b"".join(lines), compresslevel=1, mtime=0)
            )
            with self.assertRaisesRegex(
                M4ValidationError, "clean-close footer/count differs"
            ):
                scan_pose_observation_stream(
                    forged,
                    run_id="pose-binding-run",
                    runtime_id="pose-binding-runtime",
                    required_keys=set(),
                )

    def test_pose_stream_must_bind_main_collector_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pose.jsonl.gz"
            pose_records, wire, runtime, _observations = (
                query_pose_runtime_binding_fixture(path)
            )
            stop = next(
                record
                for record in runtime
                if record.get("event") == "collector_stop"
            )
            stop["pose_observation_count"] += 1
            _details, failures = validate_query_pose_runtime_binding(
                pose_records,
                wire,
                runtime,
                path,
                run_id="pose-binding-run",
                runtime_id="pose-binding-runtime",
            )
            self.assertTrue(
                any(
                    "stream/main collector lifecycle binding differs" in item
                    for item in failures
                ),
                failures,
            )

    def test_pose_stream_preserves_exact_source_identity_across_callback_threads(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "concurrent.jsonl.gz"
            writer = PoseObservationWriter(
                path,
                "concurrent-run",
                "concurrent-runtime",
                created_monotonic_ns=100,
            )

            def emit(entity: str, callback_ns: int) -> None:
                writer.emit(
                    kind="o",
                    entity_id=entity,
                    source_callback_monotonic_ns=callback_ns,
                    sim_stamp_ns=0,
                    source_topic=f"/{entity}/odometry",
                    source_transport="ros2_dds_odometry",
                    source_stamp_scope="ros_header",
                    source_frame="ros_odometry_world_enu",
                    transform_version="ams-m4-coordinate-frames-v1",
                    source_header_frame="odom",
                    source_child_frame="base_link",
                    position_m=[0.0, 0.0, 0.0],
                    orientation_quat_xyzw=[0.0, 0.0, 0.0, 1.0],
                )

            # Writer acquisition order can differ from callback-entry order
            # because native Gazebo and ROS DDS callbacks use separate threads.
            emit("uav1", 300)
            emit("uav2", 200)
            with self.assertRaisesRegex(M4ValidationError, "odometry lineage differs"):
                writer.emit(
                    kind="o",
                    entity_id="uav3",
                    source_callback_monotonic_ns=350,
                    sim_stamp_ns=0,
                    source_topic="/uav3/odometry",
                    source_transport="gazebo_transport_pose_v",
                    source_stamp_scope="ros_header",
                    source_frame="ros_odometry_world_enu",
                    transform_version="ams-m4-coordinate-frames-v1",
                    source_header_frame="odom",
                    source_child_frame="base_link",
                    position_m=[0.0, 0.0, 0.0],
                    orientation_quat_xyzw=[0.0, 0.0, 0.0, 1.0],
                )
            writer.close(closed_monotonic_ns=400)
            _matches, details = scan_pose_observation_stream(
                path,
                run_id="concurrent-run",
                runtime_id="concurrent-runtime",
                required_keys=set(),
            )
            self.assertEqual(details["observation_count"], 2)

    def test_pose_stream_is_buffered_compact_and_main_runtime_is_bounded(self) -> None:
        collector_source = (
            ROOT / "network/scripts/collect_m4_runtime.py"
        ).read_text()
        adapter_source = (
            ROOT / "network/scripts/m4_adapter_runtime.py"
        ).read_text()
        capacity_runner = (ROOT / "network/scripts/run_m4_capacity.sh").read_text()
        causality_runner = (ROOT / "network/scripts/run_m4_causality.sh").read_text()
        stream_source = (
            ROOT / "network/validation/m4_pose_observations.py"
        ).read_text()
        self.assertIn(
            "MAIN_RUNTIME_ODOMETRY_SAMPLE_PERIOD_NS = 200_000_000",
            collector_source,
        )
        self.assertEqual(collector_source.count("pose_writer.emit("), 2)
        self.assertNotIn('writer.emit(\n                        "world_pose_sample"', collector_source)
        self.assertIn("GazeboPoseVSource(node.on_world_pose)", collector_source)
        self.assertIn("GazeboPoseVSource(tracker.update_world)", adapter_source)
        self.assertNotIn("tf2_msgs/msg/TFMessage[gz.msgs.Pose_V", capacity_runner)
        self.assertNotIn("tf2_msgs/msg/TFMessage[gz.msgs.Pose_V", causality_runner)
        self.assertIn("compresslevel=1", stream_source)
        self.assertIn(
            "buffer_size=POSE_OBSERVATION_BUFFER_BYTES", stream_source
        )
        emit_body = stream_source.split("    def emit(\n", 1)[1].split(
            "    def close(", 1
        )[0]
        self.assertNotIn("flush(", emit_body)

    def test_high_rate_pose_stream_scans_without_materializing_unmatched_records(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "high-rate.jsonl.gz"
            writer = PoseObservationWriter(
                path,
                "benchmark-run",
                "benchmark-runtime",
                created_monotonic_ns=20_000_000_000,
            )
            count = 20_000
            for sequence in range(count):
                callback_ns = 20_000_001_000 + sequence * 500_000
                writer.emit(
                    kind="o",
                    entity_id="uav1",
                    source_callback_monotonic_ns=callback_ns,
                    sim_stamp_ns=18_000_001_000 + sequence * 500_000,
                    source_topic="/uav1/odometry",
                    source_transport="ros2_dds_odometry",
                    source_stamp_scope="ros_header",
                    source_frame="ros_odometry_world_enu",
                    transform_version="ams-m4-coordinate-frames-v1",
                    source_header_frame="odom",
                    source_child_frame="base_link",
                    position_m=[1.0, 2.0, 100.0],
                    orientation_quat_xyzw=[0.0, 0.0, 0.0, 1.0],
                )
            writer.close(
                closed_monotonic_ns=20_000_001_000 + count * 500_000
            )
            matches, details = scan_pose_observation_stream(
                path,
                run_id="benchmark-run",
                runtime_id="benchmark-runtime",
                required_keys=set(),
            )
            self.assertEqual(matches, {})
            self.assertEqual(details["observation_count"], count)
            self.assertEqual(details["matching_observation_count"], 0)
            self.assertLess(details["compressed_size_bytes"], 1_500_000)

    def test_effect_size_zero_delivery_and_expiry_mutations_fail(self) -> None:
        windows, failures = validate_window_manifest(
            window_manifest(),
            control_endpoint_form=TEST_ENDPOINT_FORM,
            endpoint_matrix_sha256=TEST_MATRIX_SHA256,
        )
        self.assertEqual(failures, [])
        metrics = passing_metrics()
        metrics["terrain_down"]["target"]["delivered_unique"] = 1
        metrics["terrain_down"]["target"]["delivery_ratio"] = 0.01
        metrics["jammer_on"]["target"] = copy.deepcopy(
            metrics["jammer_off_1"]["target"]
        )
        metrics["expiry_unavailable"]["target"]["delivered_unique"] = 1
        observed = validate_causal_effects(windows, metrics)
        self.assertTrue(any("terrain down target" in item for item in observed))
        self.assertTrue(any("jammer on has neither" in item for item in observed))
        self.assertTrue(any("F-expiry unavailable" in item for item in observed))

    def test_actual_offer_packet_state_and_ardupilot_outcome_are_joined(self) -> None:
        windows, failures = validate_window_manifest(
            window_manifest(),
            control_endpoint_form=TEST_ENDPOINT_FORM,
            endpoint_matrix_sha256=TEST_MATRIX_SHA256,
        )
        self.assertEqual(failures, [])
        run = {"run_id": "run", "runtime_id": "runtime", "run_nonce": "a" * 64}
        transactions = []
        packets = []
        states = {}
        transaction_sequence = 0
        packet_sequence = 0
        packet_uid = 0
        config_hash = "c" * 64
        for ordinal, window_id in enumerate(WINDOW_IDS, start=1):
            window = windows[window_id]
            for role_index, (role, link) in enumerate((
                ("target", window["target_link"]),
                ("control", window["control_link"]),
                ("background", window["background_link"]),
            )):
                transaction_sequence += 1
                transaction_id = f"{window_id}-{role}"
                digest = hashlib.sha256(transaction_id.encode()).hexdigest()
                offer_ns = int(window["start_monotonic_ns"]) + 10
                offer = {
                    "schema": "ams.m4.actual_endpoint_transaction/v1",
                    **run,
                    "event_sequence": transaction_sequence,
                    "event": "gcs_valid_command_offer",
                    "transaction_id": transaction_id,
                    "producer_role": "gcs_endpoint_probe",
                    "directed_link": link,
                    "traffic_class": "control",
                    "request_transport_payload_sha256": digest,
                    "flow_group_id": window[f"{role}_flow_group_id"],
                    "matrix_cell_id": window[f"{role}_cell_id"],
                    "endpoint_form": TEST_ENDPOINT_FORM,
                    "direction": "downlink",
                    "ordinal_send_slot": 1,
                    "host_monotonic_ns": offer_ns,
                }
                transactions.append(offer)
                fresh = window_id != "expiry_unavailable"
                delivered = window_id not in {
                    "terrain_down",
                    "building_down",
                    "expiry_unavailable",
                }
                packet_uid += 1
                base_host = offer_ns + 1 + role_index * 20
                uav = int(str(link).split("uav", 1)[1])
                immutable = {
                    "schema": "ams.ns3.packet_event/v1",
                    "event_epoch": 7,
                    "packet_uid": packet_uid,
                    "tos": 184,
                    "dscp": 46,
                    "traffic_class": "control",
                    "directed_link": link,
                    "queue_id": f"{link}.control.q0",
                    "source_ip": "10.71.0.10",
                    "destination_ip": f"10.71.{uav}.10",
                    "transport_protocol": 17,
                    "source_udp_port": 14600,
                    "destination_udp_port": 14600 + uav,
                    "transport_payload_sha256": digest,
                    "transport_payload_size": 136,
                    "p2mp": False,
                    "root_transmission": False,
                    "config_sha256": config_hash,
                    "seed": 42,
                    "run": 1,
                }

                def append_packet_event(
                    event: str,
                    host_ns: int,
                    device_id: str,
                    wire_label: str,
                    **fields: object,
                ) -> dict[str, object]:
                    nonlocal packet_sequence
                    packet_sequence += 1
                    record = {
                        **immutable,
                        "event_sequence": packet_sequence,
                        "sim_time_ns": packet_sequence,
                        "host_monotonic_ns": host_ns,
                        "event": event,
                        "device_id": device_id,
                        "packet_wire_hash_algorithm": "sha256",
                        "packet_wire_hash": hashlib.sha256(
                            wire_label.encode()
                        ).hexdigest(),
                        "packet_wire_size": 178,
                        **fields,
                    }
                    packets.append(record)
                    return record

                ingress = append_packet_event(
                    "ingress",
                    base_host,
                    "cp.tap.ingress",
                    f"ingress-{packet_uid}",
                    radio_state_status="not_decided",
                    radio_delivery=None,
                    radio_intervention=None,
                )
                state_hash = hashlib.sha256(
                    f"state-{transaction_id}".encode()
                ).hexdigest()
                applied_ns = base_host + 1
                decision_ns = base_host + 2
                zero_rate = window_id == "terrain_down" and role == "target"
                effects = {
                    "mapping_version": "sionna-effects-v1",
                    "mapping_seed": 42,
                    "propagation_delay_ns": 100,
                    "loss_probability": 0.0 if delivered or zero_rate else 1.0,
                    "service_rate_bps": 0 if zero_rate else 20_000_000,
                    "intervention": "natural",
                }
                states[state_hash] = {
                    "state_sequence": packet_uid,
                    "availability": "fresh" if fresh else "unavailable",
                    "directed_link": link,
                    "traffic_class": "control",
                    "source_packet_event_epoch": 7,
                    "source_packet_event_sequence": ingress["event_sequence"],
                    "source_packet_uid": packet_uid,
                    "source_packet_causal_sha256": digest,
                    "query_id": f"query-{packet_uid}" if fresh else None,
                    "result_wire_sha256": "d" * 64 if fresh else None,
                    "applied_state_id": f"applied-{packet_uid}" if fresh else None,
                    "validity_start_monotonic_ns": applied_ns if fresh else None,
                    "expires_monotonic_ns": decision_ns + 2_000_000_000
                    if fresh
                    else None,
                    "adapter_applied_monotonic_ns": applied_ns,
                    "physical": {"sinr_db": 20.0, "js_db": -20.0}
                    if fresh
                    else None,
                    "effects": effects if fresh else None,
                }
                sample = (
                    0.0
                    if zero_rate
                    else deterministic_loss_sample(
                        digest, f"applied-{packet_uid}", 42
                    )
                    if fresh
                    else None
                )
                radio = {
                    "radio_state_status": "fresh" if fresh else "unavailable",
                    "radio_state_sequence": packet_uid if fresh else None,
                    "radio_state_sha256": state_hash,
                    "radio_query_id": f"query-{packet_uid}" if fresh else None,
                    "radio_applied_state_id": f"applied-{packet_uid}"
                    if fresh
                    else None,
                    "radio_result_wire_sha256": "d" * 64 if fresh else None,
                    "radio_mapping_version": "sionna-effects-v1" if fresh else None,
                    "radio_mapping_seed": 42 if fresh else None,
                    "radio_delay_ns": 100 if fresh else None,
                    "radio_service_rate_bps": effects["service_rate_bps"]
                    if fresh
                    else None,
                    "radio_loss_probability": effects["loss_probability"]
                    if fresh
                    else None,
                    "radio_loss_sample": sample,
                    "radio_intervention": "natural",
                    "radio_validity_start_monotonic_ns": applied_ns
                    if fresh
                    else None,
                    "radio_adapter_applied_monotonic_ns": applied_ns
                    if fresh
                    else None,
                    "radio_expires_monotonic_ns": decision_ns + 2_000_000_000
                    if fresh
                    else None,
                    "radio_state_age_ns": decision_ns - applied_ns
                    if fresh
                    else None,
                    "radio_delivery": "deliver" if delivered else "drop",
                    "drop_reason": (
                        None
                        if delivered
                        else "sionna_service_rate_zero"
                        if zero_rate
                        else "sionna_loss"
                        if fresh
                        else "sionna_state_unavailable"
                    ),
                }
                radio_wire = f"radio-{packet_uid}"
                append_packet_event(
                    "enqueue" if delivered else "drop",
                    decision_ns,
                    "cp.radio",
                    radio_wire,
                    **radio,
                )
                if delivered:
                    append_packet_event(
                        "dequeue", decision_ns + 1, "cp.radio", radio_wire, **radio
                    )
                    serialization_ns = (
                        178 * 8 * 1_000_000_000 + 20_000_000 - 1
                    ) // 20_000_000
                    append_packet_event(
                        "channel",
                        decision_ns + 2,
                        "cp.radio",
                        radio_wire,
                        **{
                            **radio,
                            "radio_serialization_time_ns": serialization_ns,
                            "radio_base_serialization_time_ns": serialization_ns,
                            "radio_service_padding_ns": 0,
                            "radio_base_channel_delay_ns": 2_000_000,
                            "radio_effective_channel_delay_ns": 100,
                            "radio_rate_applied_at_monotonic_ns": decision_ns + 2,
                            "radio_delay_applied_at_monotonic_ns": decision_ns + 2,
                            "radio_applied_device_id": "cp.radio",
                        },
                    )
                    append_packet_event(
                        "egress",
                        decision_ns + 3,
                        f"uav{uav}.tap.egress",
                        f"egress-{packet_uid}",
                        **radio,
                    )
                    response_digest = hashlib.sha256(
                        f"response-{transaction_id}".encode()
                    ).hexdigest()
                    packet_uid += 1
                    uplink_uid = packet_uid
                    uplink_link = f"uav{uav}>cp"
                    uplink_base_ns = decision_ns + 4
                    immutable = {
                        **immutable,
                        "packet_uid": uplink_uid,
                        "directed_link": uplink_link,
                        "queue_id": f"{uplink_link}.control.q0",
                        "source_ip": f"10.71.{uav}.10",
                        "destination_ip": "10.71.0.10",
                        "source_udp_port": 14600 + uav,
                        "destination_udp_port": 14600,
                        "transport_payload_sha256": response_digest,
                        "transport_payload_size": 28,
                    }
                    uplink_ingress = append_packet_event(
                        "ingress",
                        uplink_base_ns,
                        f"uav{uav}.tap.ingress",
                        f"ingress-{uplink_uid}",
                        radio_state_status="not_decided",
                        radio_delivery=None,
                        radio_intervention=None,
                    )
                    uplink_applied_ns = uplink_base_ns + 1
                    uplink_decision_ns = uplink_base_ns + 2
                    uplink_state_hash = hashlib.sha256(
                        f"uplink-state-{transaction_id}".encode()
                    ).hexdigest()
                    uplink_applied_id = f"applied-{uplink_uid}"
                    uplink_sample = deterministic_loss_sample(
                        response_digest, uplink_applied_id, 42
                    )
                    states[uplink_state_hash] = {
                        "state_sequence": uplink_uid,
                        "availability": "fresh",
                        "directed_link": uplink_link,
                        "traffic_class": "control",
                        "source_packet_event_epoch": 7,
                        "source_packet_event_sequence": uplink_ingress[
                            "event_sequence"
                        ],
                        "source_packet_uid": uplink_uid,
                        "source_packet_causal_sha256": response_digest,
                        "query_id": f"query-{uplink_uid}",
                        "result_wire_sha256": "e" * 64,
                        "applied_state_id": uplink_applied_id,
                        "validity_start_monotonic_ns": uplink_applied_ns,
                        "expires_monotonic_ns": uplink_decision_ns
                        + 2_000_000_000,
                        "adapter_applied_monotonic_ns": uplink_applied_ns,
                        "physical": {"sinr_db": 20.0, "js_db": -20.0},
                        "effects": {
                            "mapping_version": "sionna-effects-v1",
                            "mapping_seed": 42,
                            "propagation_delay_ns": 100,
                            "loss_probability": 0.0,
                            "service_rate_bps": 20_000_000,
                            "intervention": "natural",
                        },
                    }
                    uplink_radio = {
                        "radio_state_status": "fresh",
                        "radio_state_sequence": uplink_uid,
                        "radio_state_sha256": uplink_state_hash,
                        "radio_query_id": f"query-{uplink_uid}",
                        "radio_applied_state_id": uplink_applied_id,
                        "radio_result_wire_sha256": "e" * 64,
                        "radio_mapping_version": "sionna-effects-v1",
                        "radio_mapping_seed": 42,
                        "radio_delay_ns": 100,
                        "radio_service_rate_bps": 20_000_000,
                        "radio_loss_probability": 0.0,
                        "radio_loss_sample": uplink_sample,
                        "radio_intervention": "natural",
                        "radio_validity_start_monotonic_ns": uplink_applied_ns,
                        "radio_adapter_applied_monotonic_ns": uplink_applied_ns,
                        "radio_expires_monotonic_ns": uplink_decision_ns
                        + 2_000_000_000,
                        "radio_state_age_ns": uplink_decision_ns
                        - uplink_applied_ns,
                        "radio_delivery": "deliver",
                        "drop_reason": None,
                    }
                    uplink_wire = f"radio-{uplink_uid}"
                    append_packet_event(
                        "enqueue",
                        uplink_decision_ns,
                        f"uav{uav}.radio",
                        uplink_wire,
                        **uplink_radio,
                    )
                    append_packet_event(
                        "dequeue",
                        uplink_decision_ns + 1,
                        f"uav{uav}.radio",
                        uplink_wire,
                        **uplink_radio,
                    )
                    append_packet_event(
                        "channel",
                        uplink_decision_ns + 2,
                        f"uav{uav}.radio",
                        uplink_wire,
                        **{
                            **uplink_radio,
                            "radio_serialization_time_ns": serialization_ns,
                            "radio_base_serialization_time_ns": serialization_ns,
                            "radio_service_padding_ns": 0,
                            "radio_base_channel_delay_ns": 2_000_000,
                            "radio_effective_channel_delay_ns": 100,
                            "radio_rate_applied_at_monotonic_ns": uplink_decision_ns
                            + 2,
                            "radio_delay_applied_at_monotonic_ns": uplink_decision_ns
                            + 2,
                            "radio_applied_device_id": f"uav{uav}.radio",
                        },
                    )
                    append_packet_event(
                        "egress",
                        uplink_decision_ns + 3,
                        "cp.tap.egress",
                        f"egress-{uplink_uid}",
                        **uplink_radio,
                    )
                    transaction_sequence += 1
                    transactions.append(
                        {
                            "schema": "ams.m4.actual_endpoint_transaction/v1",
                            **run,
                            "event_sequence": transaction_sequence,
                            "event": "ardupilot_timesync_echo",
                            "transaction_id": transaction_id,
                            "producer_role": "arducopter",
                            "host_monotonic_ns": offer_ns + 100,
                            "request_transport_payload_sha256": digest,
                            "response_transport_payload_sha256": response_digest,
                            "uav": f"uav{uav}",
                        }
                    )
        metrics, observed = derive_causal_window_metrics(
            windows,
            packet_records=packets,
            states_by_hash=states,
            transaction_records=transactions,
            run=run,
        )
        self.assertEqual(observed, [])
        self.assertEqual(metrics["terrain_good"]["target"]["delivered_unique"], 1)
        self.assertEqual(metrics["terrain_down"]["target"]["delivered_unique"], 0)
        self.assertEqual(
            metrics["expiry_unavailable"]["target"]["unavailable_decisions"], 1
        )

        for mutation in (
            "missing_delivery_stage",
            "cross_uid_egress",
            "forged_state_effect",
            "forged_loss_sample",
            "forged_delivery",
            "future_effect_application",
            "missing_uplink_lineage",
            "drop_shape",
            "drop_device",
            "drop_state_parent",
        ):
            with self.subTest(packet_lineage_mutation=mutation):
                mutated_packets = copy.deepcopy(packets)
                mutated_states = copy.deepcopy(states)
                mutated_transactions = copy.deepcopy(transactions)
                delivered_uid = next(
                    record["packet_uid"]
                    for record in mutated_packets
                    if record.get("event") == "egress"
                )
                delivered_chain = [
                    record
                    for record in mutated_packets
                    if record.get("packet_uid") == delivered_uid
                ]
                drop = next(
                    record
                    for record in mutated_packets
                    if record.get("event") == "drop"
                    and record.get("radio_state_status") == "fresh"
                )
                if mutation == "missing_delivery_stage":
                    next(
                        record
                        for record in delivered_chain
                        if record.get("event") == "dequeue"
                    )["event"] = "channel"
                elif mutation == "cross_uid_egress":
                    next(
                        record
                        for record in delivered_chain
                        if record.get("event") == "egress"
                    )["packet_uid"] = 999_999
                elif mutation == "forged_state_effect":
                    state = mutated_states[
                        next(
                            record
                            for record in delivered_chain
                            if record.get("event") == "enqueue"
                        )["radio_state_sha256"]
                    ]
                    state["effects"]["service_rate_bps"] = 1_000
                elif mutation == "forged_loss_sample":
                    for record in delivered_chain[1:]:
                        record["radio_loss_sample"] = 0.999999
                elif mutation == "forged_delivery":
                    state = mutated_states[
                        next(
                            record
                            for record in delivered_chain
                            if record.get("event") == "enqueue"
                        )["radio_state_sha256"]
                    ]
                    state["effects"]["loss_probability"] = 1.0
                    for record in delivered_chain[1:]:
                        record["radio_loss_probability"] = 1.0
                elif mutation == "future_effect_application":
                    channel = next(
                        record
                        for record in delivered_chain
                        if record.get("event") == "channel"
                    )
                    future = int(channel["host_monotonic_ns"]) + 1
                    channel["radio_rate_applied_at_monotonic_ns"] = future
                    channel["radio_delay_applied_at_monotonic_ns"] = future
                elif mutation == "missing_uplink_lineage":
                    next(
                        record
                        for record in mutated_transactions
                        if record.get("event") == "ardupilot_timesync_echo"
                    )["response_transport_payload_sha256"] = "f" * 64
                elif mutation == "drop_shape":
                    drop["event"] = "enqueue"
                elif mutation == "drop_device":
                    drop["device_id"] = "uav5.radio"
                elif mutation == "drop_state_parent":
                    mutated_states[drop["radio_state_sha256"]][
                        "source_packet_uid"
                    ] = 999_999
                _metrics, mutated_failures = derive_causal_window_metrics(
                    windows,
                    packet_records=mutated_packets,
                    states_by_hash=mutated_states,
                    transaction_records=mutated_transactions,
                    run=run,
                )
                self.assertTrue(mutated_failures, mutation)
                self.assertTrue(
                    any(
                        token in failure
                        for failure in mutated_failures
                        for token in ("UID", "state binding", "TIMESYNC outcome")
                    ),
                    mutated_failures,
                )

    def test_paired_bootstrap_uses_10000_resamples_and_conservative_bounds(self) -> None:
        details, failures = validate_paired_causality(
            paired_passing_metrics(), seed=42, resamples=10_000
        )
        self.assertEqual(failures, [])
        self.assertGreaterEqual(len(details), 30)
        self.assertTrue(all(item["resamples"] == 10_000 for item in details.values()))
        expiry = details["expiry_control_recovery_sinr_db"]
        self.assertEqual(expiry["pair_count"], 20)
        self.assertEqual(
            expiry["pairing"],
            "flow_group_id+ordinal_send_slot[first_20_of_100]",
        )

    def test_expiry_pairing_rejects_19_of_100_and_mismatched_first_20(
        self,
    ) -> None:
        for mutation in ("nineteen_reference", "mismatched_recovery_prefix"):
            with self.subTest(mutation=mutation):
                metrics = paired_passing_metrics()
                if mutation == "nineteen_reference":
                    del metrics["expiry_unavailable"]["control"][
                        "paired_samples"
                    ][20]
                else:
                    recovery = metrics["expiry_recovery"]["control"][
                        "paired_samples"
                    ]
                    recovery[101] = recovery.pop(20)
                _details, failures = validate_paired_causality(
                    metrics, seed=42, resamples=10_000
                )
                self.assertTrue(
                    any("paired ordinal contract is not exact 20/100" in item for item in failures)
                )

    def test_missing_ordinal_pair_fails_bootstrap(self) -> None:
        metrics = paired_passing_metrics()
        del metrics["terrain_down"]["target"]["paired_samples"][100]
        _details, failures = validate_paired_causality(
            metrics, seed=42, resamples=10_000
        )
        self.assertTrue(any("paired causal bootstrap cannot be derived" in item for item in failures))

    def test_continuous_runtime_and_lifecycle_pass(self) -> None:
        windows, failures = validate_window_manifest(
            window_manifest(),
            control_endpoint_form=TEST_ENDPOINT_FORM,
            endpoint_matrix_sha256=TEST_MATRIX_SHA256,
        )
        self.assertEqual(failures, [])
        details, failures = validate_causal_runtime(
            runtime_fixture(windows),
            windows,
            required_process_counts={"test_role": 1},
        )
        self.assertEqual(failures, [])
        self.assertEqual(details["validated_window_count"], len(WINDOW_IDS))

    def test_stimulus_never_overlaps_prior_window_or_skips_settling_gap(self) -> None:
        windows, failures = validate_window_manifest(
            window_manifest(),
            control_endpoint_form=TEST_ENDPOINT_FORM,
            endpoint_matrix_sha256=TEST_MATRIX_SHA256,
        )
        self.assertEqual(failures, [])
        for window_id, mutation in (
            (
                "building_good",
                int(windows["terrain_recovery"]["end_monotonic_ns"]) - 1,
            ),
            (
                "terrain_good",
                int(windows["terrain_good"]["start_monotonic_ns"])
                - 100_000_000,
            ),
        ):
            with self.subTest(window=window_id):
                records = runtime_fixture(windows)
                next(
                    record
                    for record in records
                    if record.get("event") == "window_stimulus_applied"
                    and record.get("window_id") == window_id
                )["host_monotonic_ns"] = mutation
                records.sort(key=lambda record: int(record["host_monotonic_ns"]))
                for sequence, record in enumerate(records, start=1):
                    record["event_sequence"] = sequence
                _details, observed = validate_causal_runtime(
                    records,
                    windows,
                    required_process_counts={"test_role": 1},
                )
                self.assertTrue(any("ordering differs" in item for item in observed))

    def test_runtime_sample_gap_fails(self) -> None:
        windows, failures = validate_window_manifest(
            window_manifest(),
            control_endpoint_form=TEST_ENDPOINT_FORM,
            endpoint_matrix_sha256=TEST_MATRIX_SHA256,
        )
        self.assertEqual(failures, [])
        records = runtime_fixture(windows)
        sample_indexes = [
            index
            for index, record in enumerate(records)
            if record["event"] == "causal_resource_sample"
        ]
        del records[sample_indexes[20]]
        for sequence, record in enumerate(records, start=1):
            record["event_sequence"] = sequence
        _details, failures = validate_causal_runtime(
            records,
            windows,
            required_process_counts={"test_role": 1},
        )
        self.assertTrue(any("interval gap over 1.5 seconds" in item for item in failures))

    def test_runtime_process_replacement_fails(self) -> None:
        windows, failures = validate_window_manifest(
            window_manifest(),
            control_endpoint_form=TEST_ENDPOINT_FORM,
            endpoint_matrix_sha256=TEST_MATRIX_SHA256,
        )
        self.assertEqual(failures, [])
        records = runtime_fixture(windows)
        samples = [
            record for record in records if record["event"] == "causal_resource_sample"
        ]
        samples[len(samples) // 2]["processes"]["processes"][0]["start_ticks"] = 2002
        _details, failures = validate_causal_runtime(
            records,
            windows,
            required_process_counts={"test_role": 1},
        )
        self.assertTrue(any("process identity changed" in item for item in failures))

    def test_runtime_false_readiness_and_nonempty_drain_fail(self) -> None:
        windows, failures = validate_window_manifest(
            window_manifest(),
            control_endpoint_form=TEST_ENDPOINT_FORM,
            endpoint_matrix_sha256=TEST_MATRIX_SHA256,
        )
        self.assertEqual(failures, [])
        records = runtime_fixture(windows)
        next(
            record
            for record in records
            if record["event"] == "causal_resource_sample"
        )["readiness"]["provider"] = False
        next(
            record
            for record in records
            if record["event"] == "window_drain_complete"
        )["queue_depths"]["ns3"] = 1
        _details, failures = validate_causal_runtime(
            records,
            windows,
            required_process_counts={"test_role": 1},
        )
        self.assertTrue(any("sample 0 is incomplete" in item for item in failures))
        self.assertTrue(any("ordering differs" in item for item in failures))

    def test_runtime_velocity_pin_evidence_mutations_fail_closed(self) -> None:
        windows, failures = validate_window_manifest(
            window_manifest(),
            control_endpoint_form=TEST_ENDPOINT_FORM,
            endpoint_matrix_sha256=TEST_MATRIX_SHA256,
        )
        self.assertEqual(failures, [])

        for mutation in (
            "missing_pin_readiness",
            "disconnected_pin_publisher",
            "duplicate_pin_attach",
            "nonblocking_pose_service",
            "late_pose_response",
            "observed_velocity",
            "wrong_odometry_header",
            "odometry_gap",
        ):
            with self.subTest(mutation=mutation):
                records = runtime_fixture(windows)
                if mutation == "missing_pin_readiness":
                    records = [
                        record
                        for record in records
                        if record.get("event") != "causal_velocity_pin_ready"
                    ]
                elif mutation == "disconnected_pin_publisher":
                    next(
                        record
                        for record in records
                        if record.get("event") == "causal_velocity_pin_ready"
                    )["all_publishers_connected"] = False
                elif mutation == "duplicate_pin_attach":
                    next(
                        record
                        for record in records
                        if record.get("event") == "causal_velocity_pin_ready"
                    )["system_add_request_count"] = len(CAUSAL_PIN_MODELS) + 1
                elif mutation == "nonblocking_pose_service":
                    next(
                        record
                        for record in records
                        if record.get("event") == "window_stimulus_applied"
                    )["pose_vector_service"] = "/world/map/set_pose_vector"
                elif mutation == "late_pose_response":
                    stimulus = next(
                        record
                        for record in records
                        if record.get("event") == "window_stimulus_applied"
                    )
                    window_id = str(stimulus["window_id"])
                    expected_ns = int(
                        windows[window_id]["start_monotonic_ns"]
                    ) - causal_pre_window_gap_ns(window_id)
                    completed_ns = (
                        expected_ns + CAUSAL_POSE_VECTOR_MAX_LATENCY_NS + 1
                    )
                    stimulus["pose_apply_started_monotonic_ns"] = expected_ns
                    stimulus["pose_apply_completed_monotonic_ns"] = completed_ns
                    stimulus["pose_apply_latency_ns"] = (
                        CAUSAL_POSE_VECTOR_MAX_LATENCY_NS + 1
                    )
                    stimulus["zero_velocity_published_monotonic_ns"] = completed_ns
                    stimulus["host_monotonic_ns"] = completed_ns + 1
                elif mutation == "observed_velocity":
                    next(
                        record
                        for record in records
                        if record.get("event") == "odometry_sample"
                    )["linear_velocity_mps"] = [0.0, 0.0, 0.051]
                elif mutation == "wrong_odometry_header":
                    next(
                        record
                        for record in records
                        if record.get("event") == "odometry_sample"
                    )["source_header_frame"] = "map"
                else:
                    gap_window = windows["terrain_good"]
                    gap_start = int(gap_window["start_monotonic_ns"])
                    gap_end = int(gap_window["end_monotonic_ns"])
                    records = [
                        record
                        for record in records
                        if not (
                            record.get("event") == "odometry_sample"
                            and record.get("uav") == "uav3"
                            and gap_start
                            <= int(record["host_monotonic_ns"])
                            < gap_end
                        )
                    ]
                records.sort(key=lambda record: int(record["host_monotonic_ns"]))
                for sequence, record in enumerate(records, start=1):
                    record["event_sequence"] = sequence
                _details, observed = validate_causal_runtime(
                    records,
                    windows,
                    required_process_counts={"test_role": 1},
                )
                expected = {
                    "missing_pin_readiness": "pin readiness cardinality differs",
                    "disconnected_pin_publisher": "pin readiness fields differ",
                    "duplicate_pin_attach": "pin readiness fields differ",
                    "nonblocking_pose_service": "ordering differs",
                    "late_pose_response": "ordering differs",
                    "observed_velocity": "observed pinned odometry sample",
                    "wrong_odometry_header": "observed pinned odometry sample",
                    "odometry_gap": "observed zero-velocity coverage differs",
                }[mutation]
                self.assertTrue(
                    any(expected in item for item in observed), observed
                )


class ActualControlCausalityAuditTests(unittest.TestCase):
    @staticmethod
    def _digest(label: str) -> str:
        return hashlib.sha256(label.encode()).hexdigest()

    def _fixture(self):
        window = copy.deepcopy(
            window_manifest()[WINDOW_IDS.index("terrain_down")]
        )
        window_id = str(window["window_id"])
        start_ns = int(window["start_monotonic_ns"])
        end_ns = int(window["end_monotonic_ns"])
        offered = int(window["offered_per_uav"])
        run = {
            "run_id": "run",
            "runtime_id": "runtime",
            "run_nonce": "a" * 64,
        }
        flow_groups = {
            f"uav{uav}": matrix_flow_group_identity(
                f"uav{uav}.control.downlink",
                TEST_ENDPOINT_FORM,
                matrix_sha256=TEST_MATRIX_SHA256,
            )["flow_group_id"]
            for uav in range(1, 6)
        }
        records: list[dict[str, object]] = []
        transport_nonce, _derivation = transport_nonce32(
            "m4_causality", str(run["run_nonce"])
        )
        sequencer = MavlinkSequencer()

        def frame_evidence(frame: bytes) -> dict[str, object]:
            return {
                "mavlink_frame_hex": frame.hex(),
                "mavlink_frame_sha256": hashlib.sha256(frame).hexdigest(),
                "mavlink_frame_size": len(frame),
            }

        def emit(event: str, monotonic_ns: int, **fields: object) -> None:
            records.append(
                {
                    "schema": "ams.actual-sitl.control-event/v1",
                    **run,
                    "profile": "m4_causality",
                    "role_subject": "gcs_control_probe",
                    "event_sequence": len(records) + 1,
                    "monotonic_ns": monotonic_ns,
                    "event": event,
                    **fields,
                }
            )

        def emit_received_parent(frame: bytes, uav: int, received_ns: int) -> str:
            digest = hashlib.sha256(frame).hexdigest()
            emit(
                "control_datagram_receive",
                received_ns,
                peer_ip=f"10.71.{uav}.10",
                peer_udp_port=14600 + uav,
                received_monotonic_ns=received_ns,
                rx_tos=184,
                transport_payload_hex=frame.hex(),
                transport_payload_sha256=digest,
                transport_payload_size=len(frame),
                decoded_message_count=1,
            )
            return digest

        emit(
            "actual_control_phase_start",
            start_ns + 1,
            phase=window_id,
            window_id=window_id,
            transport_phase_code=window["transport_phase_code"],
            offered_per_downlink_cell=offered,
            declared_start_monotonic_ns=start_ns,
            declared_end_monotonic_ns=end_ns,
            send_span_ms=window["send_span_ms"],
            response_policy="mixed_per_uav",
            response_policies=window["response_policies"],
            minimum_quiet_drain_ns_by_uav=window[
                "minimum_quiet_drain_ns_by_uav"
            ],
            expected_engine_state=window["expected_engine_state"],
            flow_group_ids=flow_groups,
        )
        for heartbeat_ordinal in range(1, 4):
            for uav in range(1, 6):
                received_ns = start_ns + heartbeat_ordinal * 1_000_000_000 + uav
                heartbeat_frame = mavlink_v2_frame(
                    0,
                    struct.pack("<IBBBBB", 0, 2, 3, 0, 4, 3),
                    sequence=heartbeat_ordinal,
                    system_id=uav,
                    component_id=1,
                )
                parent_digest = emit_received_parent(
                    heartbeat_frame, uav, received_ns
                )
                emit(
                    "real_heartbeat",
                    received_ns,
                    uav=uav,
                    source_system=uav,
                    source_component=1,
                    message_type="HEARTBEAT",
                    message_id=0,
                    peer_ip=f"10.71.{uav}.10",
                    peer_udp_port=14600 + uav,
                    received_monotonic_ns=received_ns,
                    transport_payload_sha256=parent_digest,
                    **frame_evidence(heartbeat_frame),
                )
        raw_ack_counts = {f"uav{uav}": 0 for uav in range(1, 6)}
        raw_telemetry_counts = {f"uav{uav}": 0 for uav in range(1, 6)}
        for uav in range(1, 6):
            policy = window["response_policies"][f"uav{uav}"]
            if policy != CORRELATED_TIMESYNC_POLICY:
                continue
            received_ns = start_ns + 4_000_000_000 + uav
            ack_frame = mavlink_v2_frame(
                77,
                struct.pack("<HB", 512, 0),
                sequence=10 + uav,
                system_id=uav,
                component_id=1,
            )
            parent_digest = emit_received_parent(ack_frame, uav, received_ns)
            emit(
                "real_window_command_ack",
                received_ns,
                phase=window_id,
                window_id=window_id,
                response_policy=policy,
                uav=uav,
                source_system=uav,
                source_component=1,
                message_type="COMMAND_ACK",
                message_id=77,
                peer_ip=f"10.71.{uav}.10",
                peer_udp_port=14600 + uav,
                received_monotonic_ns=received_ns,
                transport_payload_sha256=parent_digest,
                mavlink_command=512,
                mavlink_result=0,
                **frame_evidence(ack_frame),
            )
            raw_ack_counts[f"uav{uav}"] += 1
            received_ns += 1_000_000
            version_frame = mavlink_v2_frame(
                148,
                b"\1" + b"\0" * 59,
                sequence=20 + uav,
                system_id=uav,
                component_id=1,
            )
            parent_digest = emit_received_parent(version_frame, uav, received_ns)
            emit(
                "real_window_requested_telemetry",
                received_ns,
                phase=window_id,
                window_id=window_id,
                response_policy=policy,
                uav=uav,
                source_system=uav,
                source_component=1,
                message_type="AUTOPILOT_VERSION",
                message_id=148,
                peer_ip=f"10.71.{uav}.10",
                peer_udp_port=14600 + uav,
                received_monotonic_ns=received_ns,
                transport_payload_sha256=parent_digest,
                **frame_evidence(version_frame),
            )
            raw_telemetry_counts[f"uav{uav}"] += 1
        for ordinal in range(1, offered + 1):
            scheduled_ns = start_ns + causal_offer_offset_ns(window_id, ordinal)
            for uav in range(1, 6):
                sent_ns = scheduled_ns + uav
                flow_group = flow_groups[f"uav{uav}"]
                transaction_id = f"{window_id}:{flow_group}:{uav}:{ordinal}"
                request = encode_m4_correlated_control_request(
                    run_nonce=str(run["run_nonce"]),
                    transport_nonce=transport_nonce,
                    phase_code=int(window["transport_phase_code"]),
                    uav=uav,
                    sequence=ordinal,
                    mavlink=sequencer,
                )
                record_nonce = request["record_nonce"]
                policy = window["response_policies"][f"uav{uav}"]
                emit(
                    "real_command_offered",
                    sent_ns,
                    phase=window_id,
                    window_id=window_id,
                    transport_phase_code=window["transport_phase_code"],
                    flow_group_id=flow_group,
                    ordinal_send_slot=ordinal,
                    transaction_id=transaction_id,
                    uav=uav,
                    endpoint_form=TEST_ENDPOINT_FORM,
                    cell_id=f"uav{uav}.control.downlink",
                    flow_id=f"uav{uav}.control.downlink",
                    record_nonce=record_nonce,
                    marker_text=request["marker_text"],
                    marker_frame_hex=request["marker_frame"].hex(),
                    marker_frame_sha256=request["marker_frame_sha256"],
                    command_frame_hex=request["command_frame"].hex(),
                    command_frame_sha256=request["command_frame_sha256"],
                    timesync_request_tc1=request["timesync_request_tc1"],
                    timesync_request_ts1=request["timesync_request_ts1"],
                    timesync_frame_hex=request["timesync_frame"].hex(),
                    timesync_frame_sha256=request["timesync_frame_sha256"],
                    request_transport_payload_hex=request["request_datagram"].hex(),
                    request_transport_payload_sha256=request[
                        "request_datagram_sha256"
                    ],
                    request_transport_payload_size=len(request["request_datagram"]),
                    request_transport_send_return_size=len(
                        request["request_datagram"]
                    ),
                    correlation_kind="mavlink_timesync_echo_v1",
                    response_policy=policy,
                    requested_message_id=148,
                    mavlink_command=512,
                    target_system=uav,
                    target_component=1,
                    sent_monotonic_ns=sent_ns,
                    scheduled_send_monotonic_ns=scheduled_ns,
                    send_lateness_ns=uav,
                )
                timed_out = policy == "timeout_required"
                completed_ns = sent_ns + (
                    3_000_000_000 if timed_out else 20_000_000
                )
                timesync_response = None
                if not timed_out:
                    token = int(request["timesync_request_ts1"])
                    vehicle_clock_ns = 5_000_000_000 + uav * 1_000_000 + ordinal
                    response_frame = mavlink_v2_frame(
                        111,
                        struct.pack("<qq", vehicle_clock_ns, token),
                        sequence=ordinal,
                        system_id=uav,
                        component_id=1,
                    )
                    parent_digest = emit_received_parent(
                        response_frame, uav, completed_ns
                    )
                    timesync_response = {
                        "uav": uav,
                        "source_system": uav,
                        "source_component": 1,
                        "message_type": "TIMESYNC",
                        "message_id": 111,
                        "peer_ip": f"10.71.{uav}.10",
                        "peer_udp_port": 14600 + uav,
                        "received_monotonic_ns": completed_ns,
                        "transport_payload_sha256": parent_digest,
                        "timesync_tc1": vehicle_clock_ns,
                        "timesync_ts1": token,
                        **frame_evidence(response_frame),
                    }
                emit(
                    "transaction_result",
                    completed_ns,
                    phase=window_id,
                    window_id=window_id,
                    transport_phase_code=window["transport_phase_code"],
                    flow_group_id=flow_group,
                    ordinal_send_slot=ordinal,
                    transaction_id=transaction_id,
                    uav=uav,
                    endpoint_form=TEST_ENDPOINT_FORM,
                    downlink_cell_id=f"uav{uav}.control.downlink",
                    uplink_cell_id=f"uav{uav}.control.uplink",
                    record_nonce=record_nonce,
                    marker_frame_sha256=request["marker_frame_sha256"],
                    command_frame_sha256=request["command_frame_sha256"],
                    timesync_request_frame_sha256=request[
                        "timesync_frame_sha256"
                    ],
                    request_transport_payload_sha256=request[
                        "request_datagram_sha256"
                    ],
                    request_transport_payload_size=len(request["request_datagram"]),
                    correlation_kind="mavlink_timesync_echo_v1",
                    timesync_request_tc1=0,
                    timesync_request_ts1=request["timesync_request_ts1"],
                    sent_monotonic_ns=sent_ns,
                    completed_monotonic_ns=completed_ns,
                    timesync_response=timesync_response,
                    ack=None,
                    requested_telemetry=None,
                    timed_out=timed_out,
                    timeout_elapsed_ms=3000.0 if timed_out else 20.0,
                    timeout_contract_satisfied=True,
                    success=not timed_out,
                )
        emit(
            "actual_control_phase_complete",
            end_ns + 1,
            phase=window_id,
            window_id=window_id,
            transport_phase_code=window["transport_phase_code"],
            expected_engine_state=window["expected_engine_state"],
            response_policy="mixed_per_uav",
            response_policies=window["response_policies"],
            heartbeat_counts={f"uav{uav}": 3 for uav in range(1, 6)},
            raw_command_ack_counts=raw_ack_counts,
            raw_autopilot_version_counts=raw_telemetry_counts,
            offered_counts={f"uav{uav}": offered for uav in range(1, 6)},
            quarantined_uavs=[],
        )
        return records, run, {window_id: window}

    @staticmethod
    def _renumber(records: list[dict[str, object]]) -> None:
        for sequence, record in enumerate(records, start=1):
            record["event_sequence"] = sequence

    def test_all_five_uav_exact_results_and_heartbeats_pass(self) -> None:
        records, run, windows = self._fixture()
        normalized, audit, failures = normalize_actual_control_transactions(
            records, run, windows
        )
        self.assertEqual(failures, [])
        self.assertEqual(len(audit["terrain_down"]), 5)
        self.assertEqual(audit["terrain_down"]["uav1"]["results"], 100)
        self.assertEqual(audit["terrain_down"]["uav5"]["raw_heartbeats"], 3)
        self.assertEqual(audit["terrain_down"]["uav5"]["raw_command_acks"], 1)
        self.assertEqual(len(normalized), 500 + 4 * 100)

    def test_mavlink_sequence_wrap_and_repeated_digest_are_occurrences(self) -> None:
        records, run, windows = self._fixture()
        heartbeats = [
            record
            for record in records
            if record.get("event") == "real_heartbeat"
            and record.get("uav") == 2
        ]
        self.assertEqual(len(heartbeats), 3)
        for child, mavlink_sequence in zip(heartbeats, (0, 255, 0)):
            parent = next(
                record
                for record in records
                if record.get("event") == "control_datagram_receive"
                and record.get("transport_payload_sha256")
                == child["transport_payload_sha256"]
                and record.get("received_monotonic_ns")
                == child["received_monotonic_ns"]
            )
            frame = mavlink_v2_frame(
                0,
                struct.pack("<IBBBBB", 0, 2, 3, 0, 4, 3),
                sequence=mavlink_sequence,
                system_id=2,
                component_id=1,
            )
            digest = hashlib.sha256(frame).hexdigest()
            parent.update(
                transport_payload_hex=frame.hex(),
                transport_payload_sha256=digest,
                transport_payload_size=len(frame),
                decoded_message_count=1,
            )
            child.update(
                transport_payload_sha256=digest,
                mavlink_frame_hex=frame.hex(),
                mavlink_frame_sha256=digest,
                mavlink_frame_size=len(frame),
            )

        self.assertEqual(
            heartbeats[0]["transport_payload_sha256"],
            heartbeats[2]["transport_payload_sha256"],
        )
        self.assertNotEqual(
            heartbeats[0]["received_monotonic_ns"],
            heartbeats[2]["received_monotonic_ns"],
        )
        _normalized, audit, failures = normalize_actual_control_transactions(
            records, run, windows
        )
        self.assertEqual(failures, [])
        self.assertEqual(audit["terrain_down"]["uav2"]["raw_heartbeats"], 3)

    def test_relevant_raw_message_requires_exact_unique_udp_parent(self) -> None:
        mutations = (
            "missing_parent",
            "child_hash",
            "child_peer",
            "parent_hash",
            "decoded_count",
            "parent_peer",
            "rx_tos",
            "duplicate_parent",
            "duplicate_occurrence",
            "truncated",
            "trailing_junk",
            "malformed_crc",
            "short_payload",
            "malformed_sibling_crc",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                records, run, windows = self._fixture()
                child = next(
                    record
                    for record in records
                    if record.get("event") == "real_heartbeat"
                    and record.get("uav") == 2
                )
                parent = next(
                    record
                    for record in records
                    if record.get("event") == "control_datagram_receive"
                    and record.get("transport_payload_sha256")
                    == child["transport_payload_sha256"]
                    and record.get("received_monotonic_ns")
                    == child["received_monotonic_ns"]
                )

                def replace_parent_payload(payload: bytes) -> None:
                    digest = hashlib.sha256(payload).hexdigest()
                    parent.update(
                        transport_payload_hex=payload.hex(),
                        transport_payload_sha256=digest,
                        transport_payload_size=len(payload),
                    )
                    child["transport_payload_sha256"] = digest

                if mutation == "missing_parent":
                    records.remove(parent)
                elif mutation == "child_hash":
                    child["transport_payload_sha256"] = "f" * 64
                elif mutation == "child_peer":
                    child["peer_ip"] = "10.71.3.10"
                elif mutation == "parent_hash":
                    parent["transport_payload_sha256"] = "f" * 64
                elif mutation == "decoded_count":
                    parent["decoded_message_count"] = 2
                elif mutation == "parent_peer":
                    parent["peer_ip"] = "10.71.3.10"
                elif mutation == "rx_tos":
                    parent["rx_tos"] = 0
                elif mutation == "duplicate_parent":
                    records.insert(records.index(parent) + 1, copy.deepcopy(parent))
                elif mutation == "duplicate_occurrence":
                    records.append(copy.deepcopy(child))
                elif mutation == "truncated":
                    replace_parent_payload(
                        bytes.fromhex(str(parent["transport_payload_hex"]))[:-1]
                    )
                elif mutation == "trailing_junk":
                    replace_parent_payload(
                        bytes.fromhex(str(parent["transport_payload_hex"])) + b"\0"
                    )
                elif mutation == "malformed_crc":
                    malformed = bytearray(
                        bytes.fromhex(str(parent["transport_payload_hex"]))
                    )
                    malformed[-1] ^= 0x01
                    replace_parent_payload(bytes(malformed))
                    child.update(
                        mavlink_frame_hex=bytes(malformed).hex(),
                        mavlink_frame_sha256=hashlib.sha256(malformed).hexdigest(),
                        mavlink_frame_size=len(malformed),
                    )
                elif mutation == "short_payload":
                    short = mavlink_v2_frame(
                        0,
                        b"\0" * 8,
                        sequence=1,
                        system_id=2,
                        component_id=1,
                    )
                    replace_parent_payload(short)
                    child.update(
                        mavlink_frame_hex=short.hex(),
                        mavlink_frame_sha256=hashlib.sha256(short).hexdigest(),
                        mavlink_frame_size=len(short),
                    )
                elif mutation == "malformed_sibling_crc":
                    sibling = bytearray(
                        mavlink_v2_frame(
                            111,
                            struct.pack("<qq", 0, 123456789),
                            sequence=2,
                            system_id=2,
                            component_id=1,
                        )
                    )
                    sibling[-1] ^= 0x01
                    replace_parent_payload(
                        bytes.fromhex(str(child["mavlink_frame_hex"]))
                        + bytes(sibling)
                    )
                    parent["decoded_message_count"] = 2
                self._renumber(records)
                _normalized, _audit, failures = normalize_actual_control_transactions(
                    records, run, windows
                )
                expected = {
                    "duplicate_occurrence": "consumed twice",
                    "malformed_crc": "CRC differs",
                    "short_payload": "payload is truncated",
                    "malformed_sibling_crc": "CRC differs",
                }.get(mutation, "UDP parent")
                self.assertTrue(any(expected in item for item in failures), failures)

    def test_relevant_parent_frame_without_derived_event_fails(self) -> None:
        records, run, windows = self._fixture()
        child = next(
            record
            for record in records
            if record.get("event") == "real_heartbeat"
            and record.get("uav") == 1
        )
        records.remove(child)
        complete = next(
            record
            for record in records
            if record.get("event") == "actual_control_phase_complete"
        )
        complete["heartbeat_counts"]["uav1"] = 2
        self._renumber(records)
        _normalized, _audit, failures = normalize_actual_control_transactions(
            records, run, windows
        )
        self.assertTrue(
            any("relevant UDP parent frame lacks exactly one" in item for item in failures),
            failures,
        )

    def test_runtime_heartbeat_summary_uses_exact_half_open_window(self) -> None:
        history = {
            uav: [99, 100, 150, 199, 200]
            for uav in range(1, 6)
        }
        self.assertEqual(
            heartbeat_counts_for_window(history, 100, 200),
            {f"uav{uav}": 3 for uav in range(1, 6)},
        )
        history[3] = [100, 99]
        with self.assertRaises(ControlProbeError):
            heartbeat_counts_for_window(history, 100, 200)

    def test_timeout_required_target_allows_zero_but_reconciled_heartbeat(self) -> None:
        records, run, windows = self._fixture()
        removed_parent_digests = {
            record["transport_payload_sha256"]
            for record in records
            if record.get("event") == "real_heartbeat"
            and record.get("uav") == 1
        }
        records[:] = [
            record
            for record in records
            if not (
                record.get("event") == "real_heartbeat"
                and record.get("uav") == 1
            )
            and not (
                record.get("event") == "control_datagram_receive"
                and record.get("transport_payload_sha256")
                in removed_parent_digests
            )
        ]
        next(
            record
            for record in records
            if record.get("event") == "actual_control_phase_complete"
        )["heartbeat_counts"]["uav1"] = 0
        self._renumber(records)
        _normalized, audit, failures = normalize_actual_control_transactions(
            records, run, windows
        )
        self.assertEqual(failures, [])
        self.assertEqual(audit["terrain_down"]["uav1"]["raw_heartbeats"], 0)

    def test_missing_and_duplicate_transaction_result_fail(self) -> None:
        for mutation in ("missing", "duplicate"):
            with self.subTest(mutation=mutation):
                records, run, windows = self._fixture()
                result_index = next(
                    index
                    for index, record in enumerate(records)
                    if record.get("event") == "transaction_result"
                )
                if mutation == "missing":
                    del records[result_index]
                else:
                    records.append(copy.deepcopy(records[result_index]))
                self._renumber(records)
                _normalized, _audit, failures = normalize_actual_control_transactions(
                    records, run, windows
                )
                self.assertTrue(any("exactly one result" in item for item in failures))

    def test_correlated_natural_loss_budget_accepts_five_and_rejects_six(self) -> None:
        for loss_count, should_pass in ((5, True), (6, False)):
            with self.subTest(loss_count=loss_count):
                records, run, windows = self._fixture()
                selected = [
                    record
                    for record in records
                    if record.get("event") == "transaction_result"
                    and record.get("uav") == 2
                ][:loss_count]
                removed_parent_digests = {
                    result["timesync_response"]["transport_payload_sha256"]
                    for result in selected
                }
                for result in selected:
                    sent_ns = int(result["sent_monotonic_ns"])
                    result.update(
                        success=False,
                        timed_out=True,
                        timesync_response=None,
                        timeout_elapsed_ms=3000.0,
                        timeout_contract_satisfied=True,
                        completed_monotonic_ns=sent_ns + 3_000_000_000,
                    )
                records[:] = [
                    record
                    for record in records
                    if not (
                        record.get("event") == "control_datagram_receive"
                        and record.get("transport_payload_sha256")
                        in removed_parent_digests
                    )
                ]
                self._renumber(records)
                _normalized, audit, failures = normalize_actual_control_transactions(
                    records, run, windows
                )
                if should_pass:
                    self.assertEqual(failures, [])
                    self.assertEqual(
                        audit["terrain_down"]["uav2"][
                            "correlated_timesync_losses"
                        ],
                        5,
                    )
                else:
                    self.assertTrue(any("five percent" in item for item in failures))

    def test_timesync_success_in_timeout_required_target_fails(self) -> None:
        records, run, windows = self._fixture()
        result = next(
            record
            for record in records
            if record.get("event") == "transaction_result"
            and record.get("uav") == 1
        )
        sent_ns = int(result["sent_monotonic_ns"])
        token = int(result["timesync_request_ts1"])
        vehicle_clock = 7_000_000_001
        frame = mavlink_v2_frame(
            111,
            struct.pack("<qq", vehicle_clock, token),
            sequence=1,
            system_id=1,
            component_id=1,
        )
        result.update(
            success=True,
            timed_out=False,
            timeout_elapsed_ms=20.0,
            completed_monotonic_ns=sent_ns + 20_000_000,
            timesync_response={
                "uav": 1,
                "source_system": 1,
                "source_component": 1,
                "message_type": "TIMESYNC",
                "message_id": 111,
                "peer_ip": "10.71.1.10",
                "peer_udp_port": 14601,
                "received_monotonic_ns": sent_ns + 20_000_000,
                "transport_payload_sha256": self._digest("target-echo"),
                "timesync_tc1": vehicle_clock,
                "timesync_ts1": token,
                "mavlink_frame_hex": frame.hex(),
                "mavlink_frame_sha256": hashlib.sha256(frame).hexdigest(),
                "mavlink_frame_size": len(frame),
            },
        )
        _normalized, _audit, failures = normalize_actual_control_transactions(
            records, run, windows
        )
        self.assertTrue(any("response policy" in item for item in failures))

    def test_full_combined_datagram_not_command_frame_is_ns3_authority(self) -> None:
        records, run, windows = self._fixture()
        normalized, _audit, failures = normalize_actual_control_transactions(
            records, run, windows
        )
        self.assertEqual(failures, [])
        raw_offer = next(
            record for record in records if record.get("event") == "real_command_offered"
        )
        normalized_offer = next(
            record
            for record in normalized
            if record.get("event") == "gcs_valid_command_offer"
        )
        self.assertEqual(
            normalized_offer["request_transport_payload_sha256"],
            raw_offer["request_transport_payload_sha256"],
        )
        self.assertNotEqual(
            normalized_offer["request_transport_payload_sha256"],
            raw_offer["command_frame_sha256"],
        )

        transaction_id = raw_offer["transaction_id"]
        raw_offer["request_transport_payload_sha256"] = raw_offer[
            "command_frame_sha256"
        ]
        next(
            record
            for record in records
            if record.get("event") == "transaction_result"
            and record.get("transaction_id") == transaction_id
        )["request_transport_payload_sha256"] = raw_offer[
            "request_transport_payload_sha256"
        ]
        _normalized, _audit, failures = normalize_actual_control_transactions(
            records, run, windows
        )
        self.assertTrue(any("combined request authority" in item for item in failures))

    def test_locked_ardupilot_echo_is_ts1_and_tc1_is_vehicle_clock(self) -> None:
        records, run, windows = self._fixture()
        result = next(
            record
            for record in records
            if record.get("event") == "transaction_result"
            and record.get("uav") == 2
        )
        response = result["timesync_response"]
        original_parent_digest = response["transport_payload_sha256"]
        token = int(result["timesync_request_ts1"])
        vehicle_clock = int(response["timesync_tc1"])
        reversed_frame = mavlink_v2_frame(
            111,
            struct.pack("<qq", token, vehicle_clock),
            sequence=1,
            system_id=2,
            component_id=1,
        )
        response.update(
            timesync_tc1=token,
            timesync_ts1=vehicle_clock,
            mavlink_frame_hex=reversed_frame.hex(),
            mavlink_frame_sha256=hashlib.sha256(reversed_frame).hexdigest(),
            mavlink_frame_size=len(reversed_frame),
            transport_payload_sha256=hashlib.sha256(reversed_frame).hexdigest(),
        )
        parent = next(
            record
            for record in records
            if record.get("event") == "control_datagram_receive"
            and record.get("transport_payload_sha256") == original_parent_digest
        )
        parent.update(
            transport_payload_hex=reversed_frame.hex(),
            transport_payload_sha256=hashlib.sha256(reversed_frame).hexdigest(),
            transport_payload_size=len(reversed_frame),
        )
        _normalized, _audit, failures = normalize_actual_control_transactions(
            records, run, windows
        )
        self.assertTrue(any("tc1-clock/ts1-echo" in item for item in failures))

    def test_raw_ack_and_version_are_window_liveness_not_outcomes(self) -> None:
        for mutation in ("missing", "end_boundary"):
            with self.subTest(mutation=mutation):
                records, run, windows = self._fixture()
                complete = next(
                    record
                    for record in records
                    if record.get("event") == "actual_control_phase_complete"
                )
                raw_ack = next(
                    record
                    for record in records
                    if record.get("event") == "real_window_command_ack"
                    and record.get("uav") == 3
                )
                if mutation == "missing":
                    records.remove(raw_ack)
                    complete["raw_command_ack_counts"]["uav3"] = 0
                    self._renumber(records)
                else:
                    end_ns = int(windows["terrain_down"]["end_monotonic_ns"])
                    parent = next(
                        record
                        for record in records
                        if record.get("event") == "control_datagram_receive"
                        and record.get("transport_payload_sha256")
                        == raw_ack["transport_payload_sha256"]
                    )
                    parent["received_monotonic_ns"] = end_ns
                    parent["monotonic_ns"] = end_ns
                    raw_ack["received_monotonic_ns"] = end_ns
                    raw_ack["monotonic_ns"] = end_ns
                normalized, _audit, failures = normalize_actual_control_transactions(
                    records, run, windows
                )
                self.assertTrue(any("liveness" in item for item in failures))
                self.assertFalse(
                    any(
                        record.get("event")
                        in {"ardupilot_command_ack", "requested_telemetry"}
                        for record in normalized
                    )
                )

    def test_ambient_timesync_request_is_validated_but_never_counted(self) -> None:
        records, run, windows = self._fixture()
        uav = 4
        received_ns = int(windows["terrain_down"]["start_monotonic_ns"]) + 5
        request_ts1 = 9_000_000_004
        frame = mavlink_v2_frame(
            111,
            struct.pack("<qq", 0, request_ts1),
            sequence=1,
            system_id=uav,
            component_id=1,
        )
        digest = hashlib.sha256(frame).hexdigest()
        base = {
                "schema": "ams.actual-sitl.control-event/v1",
                **run,
                "profile": "m4_causality",
                "role_subject": "gcs_control_probe",
                "event_sequence": 0,
                "monotonic_ns": received_ns,
        }
        records[-1:-1] = [
            {
                **base,
                "event": "control_datagram_receive",
                "peer_ip": f"10.71.{uav}.10",
                "peer_udp_port": 14600 + uav,
                "received_monotonic_ns": received_ns,
                "rx_tos": 184,
                "transport_payload_hex": frame.hex(),
                "transport_payload_sha256": digest,
                "transport_payload_size": len(frame),
                "decoded_message_count": 1,
            },
            {
                **base,
                "event": "ambient_timesync_request",
                "uav": uav,
                "source_system": uav,
                "source_component": 1,
                "message_type": "TIMESYNC",
                "message_id": 111,
                "peer_ip": f"10.71.{uav}.10",
                "peer_udp_port": 14600 + uav,
                "received_monotonic_ns": received_ns,
                "transport_payload_sha256": digest,
                "timesync_tc1": 0,
                "timesync_ts1": request_ts1,
                "mavlink_frame_hex": frame.hex(),
                "mavlink_frame_sha256": hashlib.sha256(frame).hexdigest(),
                "mavlink_frame_size": len(frame),
            },
        ]
        self._renumber(records)
        normalized, audit, failures = normalize_actual_control_transactions(
            records, run, windows
        )
        self.assertEqual(failures, [])
        self.assertEqual(len(normalized), 500 + 4 * 100)
        self.assertEqual(audit["terrain_down"]["uav4"]["raw_command_acks"], 1)

    def test_missing_or_foreign_heartbeat_fails(self) -> None:
        for mutation in ("missing", "foreign"):
            with self.subTest(mutation=mutation):
                records, run, windows = self._fixture()
                heartbeats = [
                    record
                    for record in records
                    if record.get("event") == "real_heartbeat"
                    and record.get("uav") == 3
                ]
                if mutation == "missing":
                    records.remove(heartbeats[-1])
                    complete = next(
                        record
                        for record in records
                        if record.get("event") == "actual_control_phase_complete"
                    )
                    complete["heartbeat_counts"]["uav3"] = 2
                else:
                    heartbeats[-1]["peer_ip"] = "10.71.99.10"
                self._renumber(records)
                _normalized, _audit, failures = normalize_actual_control_transactions(
                    records, run, windows
                )
                self.assertTrue(any("heartbeat" in item for item in failures))


class ExpiryTests(unittest.TestCase):
    def _evidence(self):
        old_hash = "1" * 64
        duplicate_hash = "2" * 64
        fault = [
            {
                "event": "hold_armed",
                "monotonic_ns": 36_530_000_000,
                "directed_link_id": "cp-to-uav1-control",
            },
            {
                "event": "real_result_held",
                "monotonic_ns": 36_600_000_000,
                "directed_link_id": "cp-to-uav1-control",
                "query_id": "old",
                "result_wire_sha256": old_hash,
            },
            {
                "event": "held_result_released",
                "monotonic_ns": 107_300_000_000,
                "directed_link_id": "cp-to-uav1-control",
                "query_id": "old",
                "result_wire_sha256": old_hash,
            },
            {
                "event": "byte_identical_duplicate_released",
                "monotonic_ns": 107_400_000_000,
                "directed_link_id": "cp-to-uav1-control",
                "query_id": "newer",
                "result_wire_sha256": duplicate_hash,
            },
        ]
        adapter = [
            {
                "event": "query_submitted",
                "decision": "fault_seed",
                "monotonic_ns": 36_531_000_000,
                "directed_link": "cp>uav1",
                "traffic_class": "control",
                "packet_event_sequence": 99,
                "query_id": "old",
            },
            {
                "event": "query_submitted",
                "decision": "fault_parallel",
                "monotonic_ns": 36_810_000_000,
                "directed_link": "cp>uav1",
                "traffic_class": "control",
                "packet_event_sequence": 100,
                "query_id": "newer",
            },
            {
                "event": "result_applied",
                "monotonic_ns": 36_900_000_000,
                "directed_link": "cp>uav1",
                "traffic_class": "control",
                "query_id": "newer",
            },
            {
                "event": "state_expired",
                "monotonic_ns": 38_900_000_000,
                "directed_link": "cp>uav1",
                "traffic_class": "control",
                "query_id": "newer",
            },
            {
                "event": "result_discarded",
                "decision": "superseded",
                "monotonic_ns": 107_350_000_000,
                "query_id": "old",
            },
            {
                "event": "result_discarded",
                "decision": "duplicate",
                "monotonic_ns": 107_450_000_000,
                "query_id": "newer",
                "result_wire_sha256": duplicate_hash,
            },
            {
                "event": "result_applied",
                "monotonic_ns": 107_500_000_000,
                "directed_link": "cp>uav1",
                "traffic_class": "control",
                "query_id": "fresh",
            },
        ]
        controls = [
            {
                "schema": "ams.m4.adapter_control_event/v1",
                "action": "arm_hold_next",
                "host_monotonic_ns": 36_410_000_000,
                "detail": {
                    "directed_link_id": "cp-to-uav1-control",
                    "directed_link": "cp>uav1",
                    "traffic_class": "control",
                },
            },
            {
                "schema": "ams.m4.adapter_control_event/v1",
                "action": "arm_fault_parallel_next",
                "host_monotonic_ns": 36_680_000_000,
                "detail": {
                    "directed_link_id": "cp-to-uav1-control",
                    "directed_link": "cp>uav1",
                    "traffic_class": "control",
                    "held_query_id": "old",
                },
            },
            {
                "schema": "ams.m4.adapter_control_event/v1",
                "action": "release_held",
                "host_monotonic_ns": 107_200_000_000,
                "detail": {"query_id": "old"},
            },
            {
                "schema": "ams.m4.adapter_control_event/v1",
                "action": "inject_duplicate",
                "host_monotonic_ns": 107_210_000_000,
                "detail": {"query_id": "newer"},
            },
        ]
        wire = {
            "message_by_hash": {
                old_hash: {
                    "message_type": "result",
                    "status": "ok",
                    "expires_monotonic_ns": 38_600_000_000,
                }
            }
        }
        windows = {
            "jammer_off_2": {
                "start_monotonic_ns": 10_000_000_000,
                "end_monotonic_ns": 40_000_000_000,
            },
            "expiry_unavailable": {
                "start_monotonic_ns": 45_000_000_000,
                "end_monotonic_ns": 107_100_000_000,
            },
            "expiry_recovery": {
                "start_monotonic_ns": 117_100_000_000,
                "end_monotonic_ns": 147_100_000_000,
            },
        }
        return fault, adapter, controls, wire, windows

    def test_exact_real_result_expiry_sequence_passes(self) -> None:
        fault, adapter, controls, wire, windows = self._evidence()
        details, failures = validate_expiry_sequence(
            fault_records=fault,
            adapter_records=adapter,
            control_records=controls,
            wire=wire,
            windows=windows,
            target_packet_link="cp>uav1",
            target_directed_link_id="cp-to-uav1-control",
        )
        self.assertEqual(failures, [])
        self.assertEqual(details["fresh_recovery_query_id"], "fresh")

    def test_old_result_release_before_expiry_fails(self) -> None:
        fault, adapter, controls, wire, windows = self._evidence()
        fault[2]["monotonic_ns"] = 38_000_000_000
        _details, failures = validate_expiry_sequence(
            fault_records=fault,
            adapter_records=adapter,
            control_records=controls,
            wire=wire,
            windows=windows,
            target_packet_link="cp>uav1",
            target_directed_link_id="cp-to-uav1-control",
        )
        self.assertTrue(any("not released after its expiry" in item for item in failures))

    def test_release_inside_unavailable_window_fails(self) -> None:
        fault, adapter, controls, wire, windows = self._evidence()
        controls[2]["host_monotonic_ns"] = 107_000_000_000
        _details, failures = validate_expiry_sequence(
            fault_records=fault,
            adapter_records=adapter,
            control_records=controls,
            wire=wire,
            windows=windows,
            target_packet_link="cp>uav1",
            target_directed_link_id="cp-to-uav1-control",
        )
        self.assertTrue(any("outside setup/recovery gaps" in item for item in failures))

    def test_state_that_expires_after_traffic_starts_fails(self) -> None:
        fault, adapter, controls, wire, windows = self._evidence()
        next(
            record for record in adapter if record.get("event") == "state_expired"
        )["monotonic_ns"] = 45_100_000_000
        _details, failures = validate_expiry_sequence(
            fault_records=fault,
            adapter_records=adapter,
            control_records=controls,
            wire=wire,
            windows=windows,
            target_packet_link="cp>uav1",
            target_directed_link_id="cp-to-uav1-control",
        )
        self.assertTrue(any("adapter rejection evidence differs" in item for item in failures))

    def test_state_that_expires_before_last_positive_offer_fails(self) -> None:
        fault, adapter, controls, wire, windows = self._evidence()
        next(
            record for record in adapter if record.get("event") == "state_expired"
        )["monotonic_ns"] = 36_799_999_999
        _details, failures = validate_expiry_sequence(
            fault_records=fault,
            adapter_records=adapter,
            control_records=controls,
            wire=wire,
            windows=windows,
            target_packet_link="cp>uav1",
            target_directed_link_id="cp-to-uav1-control",
        )
        self.assertTrue(any("adapter rejection evidence differs" in item for item in failures))


class PacketOccurrenceTests(unittest.TestCase):
    def test_more_than_256_identical_packet_hashes_remain_ordered_occurrences(self) -> None:
        digest = "a" * 64
        raw = [
            {
                "schema": "ams.ns3.packet_event/v1",
                "event_sequence": ordinal + 1,
                "host_monotonic_ns": 50_000 + ordinal,
                "event": "enqueue",
                "transport_payload_sha256": digest,
                "directed_link": "cp>uav1",
                "traffic_class": "control",
                "ordinal": ordinal,
            }
            for ordinal in range(300)
        ]
        decisions, _downstream, failures = _packet_indexes(raw)
        self.assertEqual(failures, [])
        key = ("cp>uav1", "control", digest)
        self.assertEqual(len(decisions[key]), 300)
        cursors: dict[tuple[object, ...], int] = {}
        observed = [
            _consume_causal_packet_occurrence(
                decisions[key],
                cursors,
                cursor_key=("decision", *key),
                lower_ns=50_000 + ordinal,
                upper_ns=50_001 + ordinal,
            )["ordinal"]
            for ordinal in range(300)
        ]
        self.assertEqual(observed, list(range(300)))


class ControlTests(unittest.TestCase):
    class Tracker:
        def set_jammer_enabled(self, _enabled):
            pass

    class Injector:
        held_query_ids = ("held",)
        captured_query_ids = ("captured",)

        def arm_hold_next(self, _link):
            pass

        def held_query_ids_for_link(self, _link):
            return ("held",)

        def release_held(self, query_id):
            self.released = query_id

        def latest_captured_query_id(self, _link):
            return "captured"

        def inject_duplicate(self, query_id):
            self.duplicated = query_id

    def test_fault_parallel_and_duplicate_controls_are_explicit_and_bounded(self) -> None:
        injector = self.Injector()
        seed_armed: set[tuple[str, str]] = set()
        armed: set[tuple[str, str]] = set()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "001.json"
            action, detail = apply_control(
                path,
                {
                    "action": "arm_fault_parallel_next",
                    "directed_link_id": "cp-to-uav1-control",
                    "directed_link": "cp>uav1",
                    "traffic_class": "control",
                },
                self.Tracker(),
                injector,
                seed_armed,
                armed,
            )
            self.assertEqual(action, "arm_fault_parallel_next")
            self.assertEqual(armed, {("cp>uav1", "control")})
            self.assertEqual(detail["traffic_class"], "control")
            action, detail = apply_control(
                path,
                {
                    "action": "inject_duplicate",
                    "directed_link_id": "cp-to-uav1-control",
                },
                self.Tracker(),
                injector,
                seed_armed,
                armed,
            )
            self.assertEqual(action, "inject_duplicate")
            self.assertEqual(detail["query_id"], "captured")
            self.assertEqual(injector.duplicated, "captured")


if __name__ == "__main__":
    unittest.main()
