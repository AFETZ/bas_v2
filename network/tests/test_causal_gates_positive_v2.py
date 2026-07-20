from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from network.tests.causal_gate_fixture_v2 import SUMMARY_FILES, write_profile
from network.validation.evidence import _raw_experiment, evaluate_run, gate


class StrictCausalGatePositiveControls(unittest.TestCase):
    def test_every_strict_profile_passes_from_valid_raw_samples(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for profile, relative in SUMMARY_FILES.items():
                with self.subTest(profile=profile):
                    run_dir = write_profile(root, profile)
                    status = _raw_experiment(
                        run_dir,
                        relative,
                        profile,
                        (),
                        profile=profile,
                    )
                    self.assertEqual(status["status"], "passed", status)

    def test_strict_profile_does_not_need_summary_success_booleans(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = write_profile(Path(temporary), "priority")
            summary = run_dir / SUMMARY_FILES["priority"]
            text = summary.read_text(encoding="utf-8")
            self.assertNotIn('"ns3_owned_priority"', text)
            status = _raw_experiment(
                run_dir,
                SUMMARY_FILES["priority"],
                "priority",
                ("ns3_owned_priority", "payload_degraded_before_control"),
                numeric_maximums={"control_loss_rate": -1.0},
                profile="priority",
            )
            self.assertEqual(status["status"], "passed", status)
            derived = status["details"]["derived"]
            self.assertAlmostEqual(derived["control"]["loss_rate"], 0.02)
            self.assertEqual(derived["control"]["p95_ms"], 20.0)

    def test_evaluate_run_routes_p0_priority_through_strict_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = write_profile(Path(temporary), "priority")
            result = evaluate_run(run_dir)
            priority = result["gates"]["p0"]["priority"]
            self.assertEqual(priority["status"], "passed", priority)
            self.assertEqual(priority["details"]["profile"], "priority")

    def test_evaluate_run_selects_all_eight_strict_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "empty-run"
            run_dir.mkdir()
            with patch(
                "network.validation.evidence._raw_experiment",
                return_value=gate("failed", "captured"),
            ) as raw_experiment:
                evaluate_run(run_dir)
            profiles = [call.kwargs.get("profile") for call in raw_experiment.call_args_list]
            self.assertEqual(
                {profile for profile in profiles if profile is not None},
                set(SUMMARY_FILES),
            )
            self.assertEqual(profiles.count(None), 1)  # legacy P1 customer_map


if __name__ == "__main__":
    unittest.main()
