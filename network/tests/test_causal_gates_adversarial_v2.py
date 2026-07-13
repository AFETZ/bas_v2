from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from network.tests.causal_gate_fixture_v2 import (
    SUMMARY_FILES,
    positive_events,
    rewrite_raw,
    write_profile,
)
from network.validation.evidence import _raw_experiment


def _status(run_dir: Path, profile: str) -> dict[str, Any]:
    return _raw_experiment(
        run_dir,
        SUMMARY_FILES[profile],
        profile,
        ("forged_summary_success",),
        numeric_minimums={"forged_metric": -999.0},
        profile=profile,
    )


def _remove_effect(profile: str, events: list[dict[str, Any]]) -> None:
    if profile == "sionna_causality":
        for event in events:
            if event["event"] == "packet_outcome_impaired":
                event["rx_packets"] = event["tx_packets"]
                event["latency_ms"] = [10.0] * event["rx_packets"]
    elif profile == "link_locality":
        for event in events:
            if event["event"] == "target_link_impaired":
                event["rx_packets"] = 98
                event["latency_ms"] = [20.0] * 98
    elif profile == "shared_medium":
        for event in events:
            if event["event"] == "concurrent_flow_sample":
                event["rx_packets"] = event["tx_packets"]
                event["latency_ms"] = [10.0] * event["rx_packets"]
    elif profile == "priority":
        for event in events:
            if event["event"] == "payload_delivery":
                event["rx_packets"] = 98
                event["latency_ms"] = [20.0] * 98
    elif profile == "jamming":
        for event in events:
            if event["event"] == "jammer_on":
                event["sinr_db"] = 20.0
                event["js_db"] = -10.0
            if event["event"] == "packet_outcome" and event["phase"] == "on":
                event["rx_packets"] = 98
                event["latency_ms"] = [20.0] * 98
    elif profile == "time_coherence":
        next(event for event in events if event["event"] == "packet_decision")["state_ttl_ns"] = 1
    elif profile == "scene_alignment":
        landmark = next(event for event in events if event["event"] == "landmark_measurement")
        landmark["sionna_xyz_m"] = [100.0, 100.0, 100.0]
    else:
        raise ValueError(profile)


class StrictCausalGateAdversarialTests(unittest.TestCase):
    def test_forged_summary_flags_cannot_hide_missing_raw_effect(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for profile in (
                "sionna_causality",
                "link_locality",
                "shared_medium",
                "priority",
                "jamming",
                "time_coherence",
                "scene_alignment",
            ):
                with self.subTest(profile=profile):
                    events = positive_events(profile)
                    _remove_effect(profile, events)
                    run_dir = write_profile(root, profile, events=events)
                    status = _status(run_dir, profile)
                    self.assertEqual(status["status"], "failed", status)

    def test_repeatability_is_derived_from_two_clean_hashed_children(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = write_profile(
                Path(temporary), "repeatability", dirty_repeatability_child=True
            )
            status = _status(run_dir, "repeatability")
            self.assertEqual(status["status"], "failed", status)
            self.assertTrue(
                any("not clean" in failure for failure in status["details"]["failures"]),
                status,
            )

    def test_repeatability_rejects_parent_level_packet_evidence_mixing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = write_profile(Path(temporary), "repeatability")

            def inject_packet_counter(records: list[dict[str, Any]]) -> None:
                clone = next(record for record in records if record["event"] == "clean_clone_run")
                clone["tx_packets"] = 100

            rewrite_raw(run_dir, "repeatability", inject_packet_counter)
            status = _status(run_dir, "repeatability")
            self.assertEqual(status["status"], "failed", status)
            self.assertTrue(
                any("must not embed" in failure for failure in status["details"]["failures"]),
                status,
            )

    def test_bool_nonfinite_and_impossible_counters_are_rejected(self) -> None:
        mutations = {
            "boolean counter": lambda records: next(
                record for record in records if record["event"] == "control_delivery"
            ).update(tx_packets=True),
            "NaN latency": lambda records: next(
                record for record in records if record["event"] == "control_delivery"
            )["latency_ms"].__setitem__(0, float("nan")),
            "infinite offer": lambda records: next(
                record for record in records if record["event"] == "overload_offer"
            ).update(offered_bps=float("inf")),
            "rx greater than tx": lambda records: next(
                record for record in records if record["event"] == "control_delivery"
            ).update(tx_packets=10),
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, (name, mutation) in enumerate(mutations.items()):
                with self.subTest(case=name):
                    run_dir = write_profile(root / str(index), "priority")
                    rewrite_raw(run_dir, "priority", mutation)
                    self.assertEqual(_status(run_dir, "priority")["status"], "failed")

    def test_common_envelope_identity_sequence_and_clock_are_mandatory(self) -> None:
        def wrong_runtime(records: list[dict[str, Any]]) -> None:
            records[1]["runtime_id"] = "another-runtime"

        def duplicate_sequence(records: list[dict[str, Any]]) -> None:
            records[2]["event_seq"] = records[1]["event_seq"]

        def duplicate_clock(records: list[dict[str, Any]]) -> None:
            records[2]["monotonic_ns"] = records[1]["monotonic_ns"]

        def duplicate_start(records: list[dict[str, Any]]) -> None:
            records[1]["event"] = "experiment_start"

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, mutation in enumerate(
                (wrong_runtime, duplicate_sequence, duplicate_clock, duplicate_start)
            ):
                with self.subTest(mutation=mutation.__name__):
                    run_dir = write_profile(root / str(index), "link_locality")
                    rewrite_raw(run_dir, "link_locality", mutation)
                    self.assertEqual(_status(run_dir, "link_locality")["status"], "failed")

    def test_summary_runtime_must_match_joint_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = write_profile(Path(temporary), "priority")
            joint_path = run_dir / "metrics/joint_runtime.json"
            joint = json.loads(joint_path.read_text(encoding="utf-8"))
            joint["runtime_id"] = "different-joint-runtime"
            joint_path.write_text(json.dumps(joint), encoding="utf-8")
            self.assertEqual(_status(run_dir, "priority")["status"], "failed")

    def test_strict_profile_rejects_an_alternate_raw_log_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = write_profile(Path(temporary), "priority")
            summary_path = run_dir / SUMMARY_FILES["priority"]
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            original = run_dir / summary["raw_event_log"]
            alternate = run_dir / "logs/renamed_priority_events.jsonl"
            original.rename(alternate)
            summary["raw_event_log"] = str(alternate.relative_to(run_dir))
            summary_path.write_text(json.dumps(summary), encoding="utf-8")
            status = _status(run_dir, "priority")
            self.assertEqual(status["status"], "failed", status)
            self.assertTrue(
                any("fixed strict-profile path" in item for item in status["details"]["failures"]),
                status,
            )


if __name__ == "__main__":
    unittest.main()
