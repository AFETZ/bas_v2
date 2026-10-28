"""Presentation-only synthetic fixtures; never experimental observations."""
import unittest
import study_inputs as S


def record(run_id, *, eligible=True, failed=False, timely=.9, progress=.5):
    return dict(run_id=run_id, cohort="main", scenario_variant="nominal", parameter_variant="nominal",
                radio_mode="geometry", policy="fixed", attempted=True, attempt_failed=failed,
                primary_eligible=eligible, outcomes=dict(timely_per_offered=timely,
                completed_waypoint_fraction=progress, new_relay_selections=0, integral_RTF=1))


class PresentationTests(unittest.TestCase):
    def test_final_requires_aggregate_and_closed_controller_not_just_attempted(self):
        manifest = dict(kind="frozen_series", source_commit="runtime")
        freeze = dict(source_commit="runtime", cases=[dict(run_id="one", config_sha256="cfg")])
        study = dict(planned_attempts=1, observed_attempts=1, freeze_sha256="freeze",
                     runs=[dict(run_id="one", controller_recorded=True, controller_returncode=None)])
        controller = dict(run_id="one", source_commit="runtime", config_sha256="cfg",
                          started_host_monotonic_ns=1)
        with self.assertRaisesRegex(ValueError, "requires study_json"):
            S.validate_closed_study(manifest, freeze, None, {}, "freeze")
        with self.assertRaisesRegex(ValueError, "not closed"):
            S.validate_closed_study(manifest, freeze, study, {"one": controller}, "freeze")
        # A nonzero closed attempt is retained, even if its analysis is missing.
        controller.update(returncode=2, ended_host_monotonic_ns=3)
        study["runs"][0].update(controller_returncode=2, attempt_failed=True, analysis_recorded=False)
        S.validate_closed_study(manifest, freeze, study, {"one": controller}, "freeze")
        study["runs"][0]["controller_returncode"] = None
        with self.assertRaisesRegex(ValueError, "re-export"):
            S.validate_closed_study(manifest, freeze, study, {"one": controller}, "freeze")

    def test_final_rejects_mismatched_freeze_source_and_incomplete_roster(self):
        manifest = dict(kind="frozen_series", source_commit="runtime")
        freeze = dict(source_commit="runtime", cases=[dict(run_id="one", config_sha256="cfg")])
        study = dict(planned_attempts=1, observed_attempts=1, freeze_sha256="freeze",
                     runs=[dict(run_id="one", controller_recorded=True, controller_returncode=0)])
        controller = dict(run_id="one", source_commit="runtime", config_sha256="cfg", returncode=0,
                          started_host_monotonic_ns=1, ended_host_monotonic_ns=3)
        S.validate_closed_study(manifest, freeze, study, {"one": controller}, "freeze")
        with self.assertRaisesRegex(ValueError, "aggregate and freeze differ"):
            S.validate_closed_study(manifest, freeze, study, {"one": controller}, "otherfreeze")
        controller["source_commit"] = "other-runtime"
        with self.assertRaisesRegex(ValueError, "source/config differs"):
            S.validate_closed_study(manifest, freeze, study, {"one": controller}, "freeze")
        study["observed_attempts"] = 0
        with self.assertRaisesRegex(ValueError, "complete planned"):
            S.validate_closed_study(manifest, freeze, study, {"one": controller}, "freeze")

    def test_failed_after_valid_window_remains_failed_and_missing_window_is_not_zero(self):
        study = dict(runs=[record("one", failed=True), record("two", eligible=False, timely=None, progress=None)])
        row = S.cells(study)[0]
        self.assertEqual((row["planned"], row["failed"], row["eligible"]), (2, 1, 1))
        self.assertEqual(row["timely_per_offered"], .9)
        self.assertEqual(row["completed_waypoint_fraction"], .5)
        empty = S.cells(dict(runs=[record("two", eligible=False)]))[0]
        self.assertIsNone(empty["timely_per_offered"])

    def test_receipt_uart_and_physical_counts_remain_distinct(self):
        stages = dict(target_CIDs_created_in_window=8, master_receive_CIDs=7,
            follower_application_receive_CIDs=6, UART_write_end_CIDs=5,
            audited_valid_UART_CIDs=5, independently_verified_unique_inspection_goals=2)
        actual = dict(target_stage_counts=stages, rates=dict(all_five=dict(offered=10)))
        row = S.stage_counts(dict(runs=[record("one")]), {"one": actual})[0]
        self.assertEqual((row["follower_application_receive_CIDs"], row["UART_write_end_CIDs"],
                          row["independently_verified_unique_inspection_goals"]), (6, 5, 2))
        del stages["follower_application_receive_CIDs"]
        missing = S.stage_counts(dict(runs=[record("one")]), {"one": actual})[0]
        self.assertEqual(missing["complete_stage_runs"], 0)
        self.assertEqual(missing["missing_stage_run_ids"], ["one"])
        self.assertIsNone(missing["UART_write_end_CIDs"])

    def test_effect_intervals_are_transferred_by_whole_pair_and_bounds_are_separate(self):
        study = dict(paired_contrasts=[dict(cohort="main", scenario_variant="nominal", parameter_variant="nominal",
            radio_mode="geometry", metric="timely_per_offered", planned_pairs=10, complete_pairs=8,
            failover_minus_fixed=dict(mean=.2, interval_95=[.1,.3], method="saved whole-block bootstrap"),
            missing_outcome_sensitivity=dict(status="deterministic_range_bounds", mean_difference_bounds=[-.1,.4]),
            missing_pair_ids=["seed8","seed9"])])
        row = S.contrasts(study)[0]
        self.assertEqual(row["mean"], 20)
        self.assertEqual(row["interval_95"], [10,30])
        self.assertEqual(row["missing_bounds"], [-10,40])
        self.assertEqual((row["complete_pairs"],row["planned_pairs"]),(8,10))

    def test_unidentified_interaction_stays_missing(self):
        study = dict(policy_geometry_interactions=[dict(metric="completed_waypoint_fraction",
            planned_blocks=10, complete_four_case_blocks=0, estimate=dict(mean=None, interval_95=None),
            missing_outcome_sensitivity=None)])
        row = S.interactions(study)[0]
        self.assertIsNone(row["mean"])
        self.assertIsNone(row["interval_95"])
        self.assertIsNone(row["missing_bounds"])


if __name__ == "__main__":
    unittest.main()
