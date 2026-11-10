"""Service series count independent runs once and retain adverse attempts."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from network.coordination.service_profiles import load_service_profiles, qualify_payload_services
from scripts.product.analyze_relay_experiment import (
    _aggregate_service_attempts, _service_attempt, analyze_service_series, analyze_service_batch)
from test_service_profiles import lineage


def attempt(order, run=None, *, status=None, scene="a" * 64, rt_seed=42, records=1000):
    run = order if run is None else run
    raw = lineage()
    grade = qualify_payload_services(raw, run_metadata={
        "run_id": f"test-{order}", "rng_seed": 1, "rng_run": run, "rt_solver_seed": rt_seed})
    selected = grade["status"] if status is None else status
    conditions = dict(service_profile_sha256=load_service_profiles()["sha256"],
        scene_sha256=scene, rt_solver_seed=rt_seed, ns3_seed=1,
        source_commit="c" * 40, load_profile=raw["offered_load"]["profile"])
    fingerprint = hashlib.sha256(json.dumps(conditions, sort_keys=True).encode()).hexdigest()
    return dict(order=order, run_id=f"test-{order}", conditions=conditions,
        condition_sha256=fingerprint, qualification=grade, status=selected,
        runtime_valid=selected in ("measured_pass", "measured_fail"),
        input_binding_errors=[], record_counts={"1|gcs_to_uav": records},
        runtime_status="completed", mission_outcome="succeeded", measurement_validity="valid")


class ServiceSeriesTests(unittest.TestCase):
    def aggregate(self, attempts):
        return _aggregate_service_attempts(attempts, load_service_profiles())

    def test_ten_substreams_realize_both_planning_budgets(self):
        result = self.aggregate([attempt(i) for i in range(1, 11)])
        self.assertEqual(result["status"], "qualified_observed_transport_series")
        group = result["groups"][0]
        self.assertEqual(group["eligible_independent_replications"], 10)
        self.assertEqual(group["independent_counted_records"], {"1|gcs_to_uav": 10000})
        self.assertEqual(group["conditions"]["rt_solver_seed"], 42)

    def test_one_pair_repeated_ten_times_is_one_replication_and_one_record_count(self):
        result = self.aggregate([attempt(i, run=1) for i in range(1, 11)])
        group = result["groups"][0]
        self.assertEqual(result["supplied_attempts"], 10)
        self.assertEqual(group["eligible_independent_replications"], 1)
        self.assertEqual(group["independent_counted_records"], {"1|gcs_to_uav": 1000})
        self.assertEqual(result["status"], "not_qualified")

    def test_conflicting_repeat_keeps_failure_and_counts_smallest_sample_once(self):
        attempts = [attempt(i) for i in range(1, 11)]
        attempts.append(attempt(11, run=1, status="measured_fail", records=900))
        result = self.aggregate(attempts)
        group = result["groups"][0]
        self.assertEqual(group["observed_service_status"], "measured_fail")
        self.assertEqual(group["replication_qualification"], "observed_service_limits_failed")
        self.assertEqual(group["independent_counted_records"]["1|gcs_to_uav"], 9900)
        self.assertEqual(group["pairs"][0]["attempt_orders"], [1, 11])
        self.assertEqual(group["pairs"][0]["observed_service_status"], "measured_fail")

    def test_failed_or_incomplete_attempt_cannot_be_dropped_for_successful_series(self):
        for status in ("invalid", "insufficient_evidence", "not_measured"):
            attempts = [attempt(i) for i in range(1, 11)]
            attempts.append(attempt(11, status=status))
            result = self.aggregate(attempts)
            self.assertEqual(result["supplied_attempts"], 11)
            self.assertEqual(result["groups"][0]["replication_qualification"], "invalid_or_incomplete_attempts")
            self.assertEqual(result["status"], "not_qualified")

    def test_extra_short_but_runtime_valid_attempt_prevents_series_pass(self):
        attempts = [attempt(i) for i in range(1, 11)]
        short = attempt(11, status="insufficient_evidence")
        short["runtime_valid"] = True
        attempts.append(short)
        result = self.aggregate(attempts)
        self.assertFalse(result["groups"][0]["checks"]["all_observed_service_windows_pass"])
        self.assertEqual(result["status"], "not_qualified")

    def test_scene_or_rt_seed_changes_split_groups_instead_of_pooling(self):
        for changed in (dict(scene="b" * 64), dict(rt_seed=43)):
            attempts = [attempt(i) for i in range(1, 6)] + [attempt(i, **changed) for i in range(6, 11)]
            result = self.aggregate(attempts)
            self.assertEqual(len(result["groups"]), 2)
            self.assertEqual([g["eligible_independent_replications"] for g in result["groups"]], [5, 5])
            self.assertEqual(result["status"], "not_qualified")

    def test_all_streams_need_records_and_missing_attempt_never_disappears(self):
        attempts = [attempt(i, records=999) for i in range(1, 11)]
        result = self.aggregate(attempts)
        self.assertFalse(result["groups"][0]["checks"]["independent_records_per_stream"])
        missing = dict(order=11, status="not_started", condition_sha256=None)
        result = self.aggregate(attempts + [missing])
        self.assertEqual(result["ungrouped_attempt_orders"], [11])
        self.assertEqual(result["supplied_attempts"], 11)

    def test_binding_error_blocks_even_a_supplied_pass_status(self):
        attempts = [attempt(i) for i in range(1, 11)]
        attempts[0]["input_binding_errors"].append("scene_frozen_input_hash_disagrees")
        result = self.aggregate(attempts)
        self.assertEqual(result["groups"][0]["eligible_independent_replications"], 9)
        self.assertEqual(result["groups"][0]["replication_qualification"], "invalid_or_incomplete_attempts")

    def test_mission_failure_stays_separate_from_valid_service_evidence(self):
        attempts = [attempt(i) for i in range(1, 11)]
        attempts[0]["mission_outcome"] = "failed"
        result = self.aggregate(attempts)
        self.assertEqual(result["status"], "qualified_observed_transport_series")
        self.assertFalse(result["groups"][0]["all_missions_succeeded"])


class ServiceSeriesArtifactTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.native = self.root / "runs/native-radio-realtime"
        self.native.mkdir(parents=True)

    def fixture(self, run_id="audit-c0001"):
        native, control = self.native / run_id, self.root / "runs/control" / run_id
        (native / "metrics").mkdir(parents=True)
        control.mkdir(parents=True)
        scene = b'{"schema":1}\n'
        (control / "scene.json").write_bytes(scene)
        config = dict(propagation=dict(model="simple"), experiment=dict(seed=1, run=1, duration_s=60),
            runtime=dict(image="sha256:" + "d" * 64), drone=dict(count=1), flows={})
        record = dict(run_id=run_id, source_commit="c" * 40, source_dirty=False,
            scene_sha256=hashlib.sha256(scene).hexdigest(), runtime_status="completed",
            mission_outcome="succeeded", measurement_validity="valid")
        summary = dict(run_id=run_id, runtime_status="completed", measurement_validity="valid",
            measurement_start_host_monotonic_ns=10**9, measurement_end_host_monotonic_ns=61 * 10**9,
            measurement_window_completed=True)
        for path, data in ((control / "run.json", record), (control / "config.json", config),
            (native / "metrics/relay_experiment_summary.json", summary),
            (native / "metrics/relay_experiment_config.json", dict(uav_count=1, duration_s=60)),
            (native / "metrics/native_radio_stats.json", dict(rng_seed=1, rng_run=1, stop_reason="duration"))):
            path.write_text(json.dumps(data))
        (native / "environment.txt").write_text("git_head=" + "c" * 40 + "\nsource_dirty_paths=0\nns3_exact_sha=" + "d" * 40 + "\nnative_binary_sha256=" + "e" * 64 + "\n")
        # A forged cached result must never be read as observed delivery evidence.
        (native / "metrics/service_class_qualification.json").write_text('{"status":"measured_pass"}')
        return native, control, config

    def test_public_recomputation_ignores_cached_pass_and_rejects_corrupt_frozen_scene(self):
        native, control, _ = self.fixture()
        first = _service_attempt(native, load_service_profiles(), 1)
        self.assertEqual(first["evaluation_origin"], "fresh_raw_payload_lineage")
        self.assertEqual(first["status"], "not_measured")
        self.assertEqual(first["qualification"]["status"], "not_measured")
        (control / "scene.json").write_text('{"changed":true}')
        corrupt = _service_attempt(native, load_service_profiles(), 1)
        self.assertEqual(corrupt["status"], "invalid")
        self.assertIn("scene_frozen_input_hash_disagrees", corrupt["input_binding_errors"])
        self.assertEqual(corrupt["qualification"]["source_bindings"]["raw_lineage_files"], [])

    def test_missing_generator_is_invalid_but_retains_declared_condition(self):
        native, control, config = self.fixture()
        declared = lineage()["offered_load"]["profile"]
        config["flows"]["payload_load"] = declared
        (control / "config.json").write_text(json.dumps(config))
        result = _service_attempt(native, load_service_profiles(), 1)
        self.assertEqual(result["status"], "invalid")
        self.assertEqual(result["conditions"]["load_profile"], declared)
        self.assertIn("audited_load_profile_missing", result["input_binding_errors"])

    def test_duplicate_raw_metadata_fields_are_rejected(self):
        native, _, _ = self.fixture()
        (native / "metrics/native_radio_stats.json").write_text('{"rng_seed":1,"rng_seed":2}')
        result = _service_attempt(native, load_service_profiles(), 1)
        self.assertEqual(result["status"], "invalid")
        self.assertIn("duplicate configuration field", str(result["input_binding_errors"]))

    def test_batch_keeps_all_unstarted_cases_and_cli_writes_machine_report(self):
        cases = [dict(run_id=f"batch-c{i:04d}", seed=1, rng_run=i, status="not_started",
                      run_record=f"runs/control/batch-c{i:04d}/run.json") for i in (1, 2)]
        plan = self.root / "batch.json"
        plan.write_text(json.dumps(dict(schema_version=1, planned_cases=2, cases=cases)))
        out = self.root / "out"
        command = [sys.executable, "scripts/product/analyze_relay_experiment.py", "--service-batch",
                   str(plan), "--runs-root", str(self.native), "--output-dir", str(out)]
        completed = subprocess.run(command, cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads((out / "metrics/service_series_analysis.json").read_text())
        self.assertEqual(report["attempt_status_counts"], {"not_started": 2})
        self.assertEqual(report["supplied_attempts"], 2)
        self.assertEqual(report["status"], "not_qualified")
        with self.assertRaisesRegex(ValueError, "exists"):
            analyze_service_batch(plan, self.native, out)

    def test_batch_duplicate_run_identity_is_an_error_not_a_missing_repeat(self):
        plan = self.root / "batch.json"
        case = dict(run_id="same", run_record="runs/control/same/run.json")
        plan.write_text(json.dumps(dict(schema_version=1, planned_cases=2, cases=[case, case])))
        with self.assertRaisesRegex(ValueError, "unique"):
            analyze_service_batch(plan, self.native)

    def test_batch_declared_inputs_are_checked_against_raw_attempt(self):
        native, _, config = self.fixture("batch-c0001")
        case = dict(run_id=native.name, seed=1, rng_run=1, status="attempted",
                    run_record=f"runs/control/{native.name}/run.json")
        plan = self.root / "batch.json"
        plan.write_text(json.dumps(dict(schema_version=1, planned_cases=1, cases=[case],
            config=config, input_sha256={"scene":dict(path="scene.json", sha256="f" * 64)})))
        result = analyze_service_batch(plan, self.native)
        self.assertEqual(result["attempts"][0]["status"], "invalid")
        self.assertIn("scene_disagrees_with_batch_plan", result["attempts"][0]["input_binding_errors"])

    def test_missing_and_repeated_paths_remain_distinct_attempt_entries(self):
        missing = self.native / "missing"
        result = analyze_service_series([missing, missing])
        self.assertEqual(result["supplied_attempts"], 2)
        self.assertEqual(result["attempt_status_counts"], {"missing_attempt_artifacts": 2})
