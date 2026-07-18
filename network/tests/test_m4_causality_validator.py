#!/usr/bin/env python3
"""Adversarial tests for M4 causal-window and F-expiry derivation."""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from network.scripts.m4_adapter_runtime import apply_control  # noqa: E402
from network.validation.validate_m4_causality import (  # noqa: E402
    BACKGROUND_CELL_ID,
    CAUSAL_GAP_NS,
    CAUSAL_MEASUREMENT_SPAN_NS,
    CAUSAL_SOURCE_PATHS,
    FINALIZATION_BUDGET_NS,
    PRECONTRACT_SETUP_BUDGET_NS,
    REQUIRED_WRAPPER_RESERVE_NS,
    RUNTIME_READINESS_BUDGET_NS,
    WRAPPER_TIMEOUT_NS,
    WINDOW_IDS,
    WINDOW_SHAPES,
    _consume_causal_packet_occurrence,
    _packet_indexes,
    causal_quiet_drain_map,
    causal_response_policies,
    causal_window_plan,
    derive_causal_window_metrics,
    matrix_flow_group_identity,
    validate_causal_effects,
    validate_causal_execution_budget,
    validate_causal_pose_geometry,
    validate_causal_runtime,
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
    start = 200_000_000_000
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
        if (
            next_index < len(WINDOW_IDS)
            and WINDOW_IDS[next_index]
            in {"terrain_recovery", "building_recovery", "expiry_recovery"}
        ):
            start += CAUSAL_GAP_NS
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
        "control": copy.deepcopy(good),
        "background": copy.deepcopy(good),
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
    prior_end = first_start - 2_000_000_000
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
        stimulus_host = start - 90_000_000
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
        prior = window_id
        prior_end = end
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


class CausalWindowTests(unittest.TestCase):
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
                "uav2": "ack_required",
                "uav3": "ack_required",
                "uav4": "ack_required",
                "uav5": "ack_required",
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

    def test_1200_second_execution_budget_has_a_real_120_second_reserve(self) -> None:
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
            },
        }
        details, failures = validate_causal_execution_budget(run, windows)
        self.assertEqual(failures, [])
        self.assertEqual(details["planned_total_ns"], 1_176_100_000_000)
        self.assertEqual(details["unallocated_margin_ns"], 23_900_000_000)

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
        stack = ROOT / "network/scripts/actual_sitl_stack_orchestrator.sh"
        phase_driver = ROOT / "network/scripts/m4_causal_phase_driver.py"
        for path in (runner, stack, phase_driver):
            self.assertTrue(path.stat().st_mode & 0o111, path)
        for path in (runner, stack):
            completed = subprocess.run(
                ["bash", "-n", str(path)], capture_output=True, text=True, check=False
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
        source = runner.read_text(encoding="utf-8")
        self.assertIn("initialize-causality", source)
        self.assertIn("--profile m4_causality", source)
        self.assertIn("actual_sitl_stack_orchestrator.sh", source)
        self.assertIn("--fault-enabled", source)
        self.assertNotIn("technical_synthetic_fixture", source)
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
        for ordinal, window_id in enumerate(WINDOW_IDS, start=1):
            window = windows[window_id]
            for role, link in (
                ("target", window["target_link"]),
                ("control", window["control_link"]),
                ("background", window["background_link"]),
            ):
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
                state_hash = hashlib.sha256(f"state-{transaction_id}".encode()).hexdigest()
                fresh = window_id != "expiry_unavailable"
                if fresh:
                    states[state_hash] = {
                        "physical": {"sinr_db": 20.0, "js_db": -20.0},
                        "effects": {"service_rate_bps": 20_000_000},
                        "adapter_applied_monotonic_ns": offer_ns - 1,
                    }
                packet_sequence += 1
                decision = {
                    "schema": "ams.ns3.packet_event/v1",
                    "event_sequence": packet_sequence,
                    "event": "enqueue" if fresh else "drop",
                    "directed_link": link,
                    "traffic_class": "control",
                    "transport_payload_sha256": digest,
                    "radio_state_status": "fresh" if fresh else "unavailable",
                    "radio_state_sha256": state_hash if fresh else None,
                    "host_monotonic_ns": offer_ns + 1,
                }
                packets.append(decision)
                delivered = window_id not in {
                    "terrain_down",
                    "building_down",
                    "expiry_unavailable",
                }
                if delivered:
                    packet_sequence += 1
                    packets.append(
                        {
                            **decision,
                            "event_sequence": packet_sequence,
                            "event": "egress",
                        }
                    )
                    for event in ("ardupilot_command_ack", "requested_telemetry"):
                        transaction_sequence += 1
                        transactions.append(
                            {
                                "schema": "ams.m4.actual_endpoint_transaction/v1",
                                **run,
                                "event_sequence": transaction_sequence,
                                "event": event,
                                "transaction_id": transaction_id,
                                "producer_role": "arducopter",
                                "host_monotonic_ns": offer_ns + 100,
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

    def test_paired_bootstrap_uses_10000_resamples_and_conservative_bounds(self) -> None:
        details, failures = validate_paired_causality(
            paired_passing_metrics(), seed=42, resamples=10_000
        )
        self.assertEqual(failures, [])
        self.assertGreaterEqual(len(details), 30)
        self.assertTrue(all(item["resamples"] == 10_000 for item in details.values()))

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


class ExpiryTests(unittest.TestCase):
    def _evidence(self):
        old_hash = "1" * 64
        duplicate_hash = "2" * 64
        fault = [
            {"event": "hold_armed", "monotonic_ns": 90},
            {
                "event": "real_result_held",
                "monotonic_ns": 100,
                "directed_link_id": "cp-to-uav1-control",
                "query_id": "old",
                "result_wire_sha256": old_hash,
            },
            {
                "event": "held_result_released",
                "monotonic_ns": 200,
                "directed_link_id": "cp-to-uav1-control",
                "query_id": "old",
                "result_wire_sha256": old_hash,
            },
            {
                "event": "byte_identical_duplicate_released",
                "monotonic_ns": 220,
                "directed_link_id": "cp-to-uav1-control",
                "query_id": "newer",
                "result_wire_sha256": duplicate_hash,
            },
        ]
        adapter = [
            {"event": "state_expired", "monotonic_ns": 160},
            {
                "event": "result_applied",
                "monotonic_ns": 170,
                "directed_link": "cp>uav1",
                "traffic_class": "control",
                "query_id": "newer",
            },
            {
                "event": "result_discarded",
                "decision": "superseded",
                "monotonic_ns": 210,
                "query_id": "old",
            },
            {
                "event": "result_discarded",
                "decision": "duplicate",
                "monotonic_ns": 230,
                "query_id": "newer",
                "result_wire_sha256": duplicate_hash,
            },
            {
                "event": "result_applied",
                "monotonic_ns": 240,
                "directed_link": "cp>uav1",
                "traffic_class": "control",
                "query_id": "fresh",
            },
        ]
        controls = [
            {"schema": "ams.m4.adapter_control_event/v1", "action": action}
            for action in (
                "arm_hold_next",
                "arm_fault_parallel_next",
                "release_held",
                "inject_duplicate",
            )
        ]
        wire = {
            "message_by_hash": {
                old_hash: {
                    "message_type": "result",
                    "status": "ok",
                    "expires_monotonic_ns": 150,
                }
            }
        }
        return fault, adapter, controls, wire

    def test_exact_real_result_expiry_sequence_passes(self) -> None:
        fault, adapter, controls, wire = self._evidence()
        details, failures = validate_expiry_sequence(
            fault_records=fault,
            adapter_records=adapter,
            control_records=controls,
            wire=wire,
            target_packet_link="cp>uav1",
            target_directed_link_id="cp-to-uav1-control",
        )
        self.assertEqual(failures, [])
        self.assertEqual(details["fresh_recovery_query_id"], "fresh")

    def test_old_result_release_before_expiry_fails(self) -> None:
        fault, adapter, controls, wire = self._evidence()
        fault[2]["monotonic_ns"] = 140
        _details, failures = validate_expiry_sequence(
            fault_records=fault,
            adapter_records=adapter,
            control_records=controls,
            wire=wire,
            target_packet_link="cp>uav1",
            target_directed_link_id="cp-to-uav1-control",
        )
        self.assertTrue(any("not released after its expiry" in item for item in failures))


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
        armed: set[tuple[str, str]] = set()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "001.json"
            action, detail = apply_control(
                path,
                {
                    "action": "arm_fault_parallel_next",
                    "directed_link": "cp>uav1",
                    "traffic_class": "control",
                },
                self.Tracker(),
                injector,
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
                armed,
            )
            self.assertEqual(action, "inject_duplicate")
            self.assertEqual(detail["query_id"], "captured")
            self.assertEqual(injector.duplicated, "captured")


if __name__ == "__main__":
    unittest.main()
