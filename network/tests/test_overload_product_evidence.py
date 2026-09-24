#!/usr/bin/env python3
"""Focused contracts for the versioned bounded-overload evidence."""

from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def load_metric(name: str) -> dict:
    return json.loads((ROOT / "metrics" / name).read_text(encoding="utf-8"))


class OverloadProductEvidenceTests(unittest.TestCase):
    def assert_terminal_ledger(self, summary: dict, expected_rows: int) -> None:
        terminal = summary["terminal"]
        ledger = terminal["per_packet_ledger"]

        self.assertEqual(ledger["accounted_rows"], expected_rows)
        self.assertEqual(ledger["unique_packet_ids"], expected_rows)
        self.assertEqual(sum(terminal["status_counts"].values()), expected_rows)
        self.assertTrue(ledger["all_terminal"])
        self.assertTrue(terminal["logical_all_packets_terminal"])
        self.assertEqual(terminal["logical_terminal_pending"], 0)
        self.assertEqual(len(ledger["canonical_profile_ledger_sha256"]), 64)
        self.assertEqual(len(ledger["source_file_sha256"]), 64)

    def test_controlled_overload_is_passed_and_every_packet_is_terminal(self) -> None:
        controlled = load_metric("controlled_overload_summary.json")

        self.assertEqual(controlled["status"], "passed")
        self.assertTrue(all(controlled["acceptance"].values()))
        self.assertTrue(controlled["shaping_enabled"])
        self.assert_terminal_ledger(controlled, 18_600)

    def test_meltdown_is_characterization_only_with_a_terminal_ledger(self) -> None:
        meltdown = load_metric("meltdown_characterization.json")

        self.assertTrue(meltdown["characterization_only"])
        self.assertFalse(meltdown["shaping_enabled"])
        self.assertFalse(meltdown["gates_overall_status"])
        self.assert_terminal_ledger(meltdown, 12_400)

    def test_event_profile_reconciles_before_and_after(self) -> None:
        profile = load_metric("event_profile.json")

        self.assertGreater(
            profile["changes"]["scheduler_lag_p95_ms_before"],
            profile["changes"]["scheduler_lag_p95_ms_after"],
        )
        self.assertEqual(profile["changes"]["logical_terminal_pending_after"], 0)
        self.assertFalse(profile["serial"]["aggregation_enabled"])


if __name__ == "__main__":
    unittest.main()
