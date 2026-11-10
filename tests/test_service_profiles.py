"""Engineering profiles consume audited offered records, never RF guesses."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from network.coordination.payload import LOAD_CLASSES, normalize_payload_load, payload_load_slots, payload_load_slot_ns
from network.coordination.service_profiles import (
    load_service_profiles, normalize_service_profiles, qualify_payload_services)


def lineage(name="10kbps", seconds=60, uavs=(1,)):
    profile = normalize_payload_load({"class": name, "uav_ids": list(uavs)}, uavs)
    start, stop = 10**9, round((seconds + 1) * 10**9)
    records = []
    for uav in uavs:
        for slot in range(payload_load_slots(profile, stop - start)):
            created = payload_load_slot_ns(profile, start, slot)
            records.append(dict(frame_uid=f"{uav}:{slot}", load_id="payload_load",
                uav_id=uav, direction="gcs_to_uav", process_epoch="gcs:1",
                created_host_monotonic_ns=created, accepted_host_monotonic_ns=created + 1_000_000,
                target_observation_end_host_monotonic_ns=stop + 10_000_000_000,
                application_useful_bytes=profile["useful_bytes"], outcome="timely"))
    return dict(measurement_eligible=True, records=records,
        trace_files=[dict(path="payload/logs/scenario_events.jsonl", sha256="a" * 64, bytes=123, status="observed")],
        offered_load=dict(status="recorded", generator_eligible=True, profile=profile, config_sha256="b" * 64,
        load_window=dict(start_host_monotonic_ns=start, end_host_monotonic_ns=stop)))


class ServiceProfileTests(unittest.TestCase):
    def test_six_selected_profiles_use_existing_payload_class_rates(self):
        bundle = load_service_profiles()
        self.assertEqual(set(bundle["config"]["profiles"]), set(LOAD_CLASSES))
        self.assertEqual([bundle["config"]["profiles"][c]["deadline_ms"] for c in LOAD_CLASSES],
                         [250, 250, 500, 1000, 1000, 5000])
        self.assertEqual(len(bundle["sha256"]), 64)

    def test_unknown_fields_units_rates_and_invalid_numbers_are_rejected(self):
        base = load_service_profiles()["config"]
        mutations = [
            lambda p: p.update(unknown=True),
            lambda p: p.update(schema_version=True),
            lambda p: p.update(minimum_observation_s=20),
            lambda p: p["units"].update(deadline_ms="seconds"),
            lambda p: p["profiles"]["10kbps"].update(useful_bitrate_bps=1000),
            lambda p: p["profiles"]["1kbps"].update(deadline_ms=True),
            lambda p: p["profiles"]["1kbps"].update(deadline_ms=float("nan")),
            lambda p: p["profiles"]["1kbps"].update(timely_delivery_ratio_min=1.1),
            lambda p: p["profiles"]["1kbps"].update(queue_limit=20),
        ]
        for change in mutations:
            value = copy.deepcopy(base)
            change(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_service_profiles(value)

    def test_duplicate_profile_json_fields_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            text = json.dumps(load_service_profiles()["config"])
            path.write_text(text.replace('"schema_version": 1', '"schema_version": 99, "schema_version": 1'))
            with self.assertRaisesRegex(ValueError, "duplicate"):
                load_service_profiles(path)

    def test_twenty_second_nominal_success_does_not_qualify_sixty_seconds(self):
        result = qualify_payload_services(lineage(seconds=20))
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertEqual(result["classes"]["10kbps"]["streams"][0]["windows"], [])

    def test_complete_window_is_measured_proxy_without_series_or_other_class_claim(self):
        result = qualify_payload_services(lineage())
        self.assertEqual(result["status"], "measured_pass")
        self.assertEqual(result["independent_series_status"], "insufficient_evidence")
        self.assertIn("synthetic", result["semantic_scope"])
        self.assertEqual(result["classes"]["2mbps"]["status"], "not_measured")

    def test_missing_and_late_keep_offered_denominator(self):
        value = lineage()
        value["records"][0]["accepted_host_monotonic_ns"] = None
        value["records"][0]["outcome"] = "missing"
        value["records"][1]["accepted_host_monotonic_ns"] += 500_000_000
        value["records"][1]["outcome"] = "late"
        result = qualify_payload_services(value)
        window = result["classes"]["10kbps"]["streams"][0]["windows"][0]
        self.assertEqual(window["offered"], len(value["records"]))
        self.assertEqual((window["missing"], window["late"]), (1, 1))
        self.assertEqual(result["status"], "measured_fail")

    def test_generator_failure_is_invalid_even_when_every_created_record_arrived(self):
        value = lineage()
        value["offered_load"]["generator_eligible"] = False
        self.assertEqual(qualify_payload_services(value)["status"], "invalid")

    def test_class_deadline_uses_timestamps_instead_of_legacy_250ms_labels(self):
        value = lineage(name="100kbps")
        for row in value["records"]:
            row["accepted_host_monotonic_ns"] = row["created_host_monotonic_ns"] + 400_000_000
            row["outcome"] = "late"
        result = qualify_payload_services(value)
        self.assertEqual(result["status"], "measured_pass")
        self.assertEqual(result["classes"]["100kbps"]["streams"][0]["windows"][0]["late"], 0)

    def test_missing_without_new_class_deadline_horizon_is_censored(self):
        value = lineage(name="100kbps")
        row = value["records"][0]
        row.update(accepted_host_monotonic_ns=None, outcome="missing",
                   target_observation_end_host_monotonic_ns=row["created_host_monotonic_ns"] + 300_000_000)
        result = qualify_payload_services(value)
        window = result["classes"]["100kbps"]["streams"][0]["windows"][0]
        self.assertEqual((window["right_censored"], window["missing"]), (1, 0))
        self.assertEqual(result["status"], "insufficient_evidence")

    def test_failing_uav_is_not_hidden_by_other_uav_delivery(self):
        value = lineage(uavs=(1, 2))
        for row in [r for r in value["records"] if r["uav_id"] == 2][:2]:
            row.update(accepted_host_monotonic_ns=None, outcome="missing")
        result = qualify_payload_services(value)
        self.assertEqual(result["status"], "measured_fail")
        self.assertEqual([s["status"] for s in result["classes"]["10kbps"]["streams"]],
                         ["measured_pass", "measured_fail"])

    def test_epochs_are_not_merged_and_duplicate_offers_are_invalid(self):
        value = lineage()
        for row in value["records"][len(value["records"]) // 2:]:
            row["process_epoch"] = "gcs:2"
        result = qualify_payload_services(value)
        self.assertEqual(len(result["classes"]["10kbps"]["streams"]), 2)
        self.assertNotEqual(result["status"], "measured_pass")
        value = lineage()
        value["records"].append(copy.deepcopy(value["records"][0]))
        self.assertEqual(qualify_payload_services(value)["status"], "invalid")

    def test_rolling_windows_preserve_first_failure_then_recovery(self):
        value = lineage(seconds=61)
        for row in value["records"][:2]:
            row.update(accepted_host_monotonic_ns=None, outcome="missing")
        result = qualify_payload_services(value)
        self.assertEqual([w["status"] for w in result["classes"]["10kbps"]["streams"][0]["windows"]],
                         ["measured_fail", "measured_pass"])
        self.assertEqual(result["status"], "measured_fail")


    def test_final_partial_step_is_not_dropped_to_hide_tail_losses(self):
        value = lineage(seconds=60.5)
        for row in value["records"][-2:]:
            row.update(accepted_host_monotonic_ns=None, outcome="missing")
        result = qualify_payload_services(value)
        windows = result["classes"]["10kbps"]["streams"][0]["windows"]
        self.assertEqual([w["status"] for w in windows], ["measured_pass", "measured_fail"])
        self.assertEqual(windows[-1]["end_host_monotonic_ns"],
                         value["offered_load"]["load_window"]["end_host_monotonic_ns"])
        self.assertEqual(result["status"], "measured_fail")

    def test_replication_identity_uses_fixed_seed_and_run_substreams_separately_from_grade(self):
        first = qualify_payload_services(lineage(), run_metadata=dict(
            run_id="a", rng_seed=1, rng_run=1, rt_solver_seed=42))
        repeat = qualify_payload_services(lineage(), run_metadata=dict(
            run_id="b", rng_seed=1, rng_run=1, rt_solver_seed=42))
        next_run = qualify_payload_services(lineage(), run_metadata=dict(
            run_id="c", rng_seed=1, rng_run=2, rt_solver_seed=42))
        self.assertEqual(first["status"], "measured_pass")
        self.assertEqual(first["replication_qualification"]["status"], "insufficient_replications")
        self.assertEqual(first["replication_qualification"]["observed_independent_replications"], 1)
        key = lambda r: tuple(r["replication_qualification"]["identity"]["key"])
        self.assertEqual(key(first), key(repeat))
        self.assertNotEqual(key(first), key(next_run))
        self.assertEqual(len({key(r) for r in (first, repeat, next_run)}), 2)
        self.assertEqual(next_run["replication_qualification"]["identity"]["rt_solver_seed"], 42)

    def test_missing_rng_identity_does_not_reject_observed_window_or_invent_replications(self):
        result = qualify_payload_services(lineage())
        self.assertEqual(result["status"], "measured_pass")
        self.assertEqual(result["replication_qualification"]["observed_independent_replications"], 0)
        self.assertEqual(result["replication_qualification"]["status"], "insufficient_replications")
        invalid = qualify_payload_services(lineage(), run_metadata=dict(rng_seed=True, rng_run=1))
        self.assertEqual(invalid["replication_qualification"]["observed_independent_replications"], 0)

    def test_supplied_pass_and_timely_labels_do_not_replace_timestamp_classification(self):
        value = lineage()
        value.update(passed=True, status="measured_pass")
        for row in value["records"][:2]:
            row["accepted_host_monotonic_ns"] = row["created_host_monotonic_ns"] + 500_000_000
            row["outcome"] = "timely"
        self.assertEqual(qualify_payload_services(value)["status"], "measured_fail")

    def test_profile_limits_cannot_change_under_old_file_sha(self):
        profile = load_service_profiles()
        profile["config"]["profiles"]["10kbps"]["deadline_ms"] = 5000
        with self.assertRaisesRegex(ValueError, "binding disagrees"):
            qualify_payload_services(lineage(), profiles=profile)

    def test_raw_lineage_profile_and_metadata_bindings_are_always_emitted(self):
        value = lineage()
        value["trace_files"] = [dict(path="payload/logs/scenario_events.jsonl",
            sha256="a" * 64, bytes=123, status="observed")]
        value["offered_load"]["config_sha256"] = "b" * 64
        metadata = dict(run_id="fixture", rng_seed=1, rng_run=2, rt_solver_seed=42,
            source_commit="c" * 40, source_files=[dict(path="environment.txt", sha256="d" * 64, bytes=20)])
        result = qualify_payload_services(value, run_metadata=metadata)
        bindings = result["source_bindings"]
        self.assertEqual(bindings["service_profile"]["sha256"], result["service_profile_sha256"])
        self.assertEqual(bindings["raw_lineage_files"], value["trace_files"])
        self.assertEqual(bindings["runtime_metadata_files"], metadata["source_files"])
        self.assertEqual(bindings["offered_config_sha256"], "b" * 64)
        self.assertEqual(len(bindings["normalized_records_sha256"]), 64)
        self.assertEqual(bindings["service_profile"]["path"], "network/config/service_profiles_v1.json")
        self.assertIn("source_bindings", qualify_payload_services({}))


    def test_supplied_eligible_flag_cannot_hide_missing_or_changing_raw_bindings(self):
        for mutate in (lambda v: v.pop("trace_files"),
                       lambda v: v["trace_files"][0].update(status="changing"),
                       lambda v: v["offered_load"].pop("config_sha256")):
            value = lineage()
            value.update(passed=True, status="measured_pass")
            mutate(value)
            with self.subTest(value=value):
                self.assertEqual(qualify_payload_services(value)["status"], "invalid")

    def test_new_replication_budget_is_typed_and_old_distinct_seed_requirement_rejected(self):
        profile = load_service_profiles()["config"]
        self.assertEqual(profile["minimum_independent_replications"], 10)
        old = copy.deepcopy(profile)
        old["minimum_distinct_seeds"] = old.pop("minimum_independent_replications")
        with self.assertRaises(ValueError):
            normalize_service_profiles(old)
        profile["minimum_independent_replications"] = True
        with self.assertRaises(ValueError):
            normalize_service_profiles(profile)


if __name__ == "__main__":
    unittest.main()
