from __future__ import annotations

import json
import os
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from network.tests.mavlink_vertical_slice_probe import (
    JsonlWriter,
    PhaseResult,
    attempt_nonce,
    criteria_met,
    latency_stats,
    make_marker,
    mavlink_frame_sequence,
    parse_process_reference,
    read_cmdline_sha256,
    read_start_ticks,
)


class MavlinkVerticalSliceProbeTests(unittest.TestCase):
    def test_attempt_nonce_and_marker_are_deterministic(self) -> None:
        nonce = "m2n-0123456789abcdef01234567"
        self.assertEqual(attempt_nonce(nonce, "recovery", 10), f"{nonce}:recovery:10")
        marker = make_marker(nonce, "recovery", 10)
        self.assertIn(nonce, marker)
        self.assertLessEqual(len(marker.encode("ascii")), 50)

    def test_marker_rejects_oversized_nonce(self) -> None:
        with self.assertRaises(ValueError):
            make_marker("n" * 100, "recovery", 10)

    def test_mavlink_sequence_parsing(self) -> None:
        self.assertEqual(mavlink_frame_sequence(bytes([0xFE, 0, 37, 0, 0])), 37)
        self.assertEqual(mavlink_frame_sequence(bytes([0xFD, 0, 0, 0, 91])), 91)
        with self.assertRaises(ValueError):
            mavlink_frame_sequence(b"bad")

    def test_latency_statistics_are_finite_and_nearest_rank(self) -> None:
        result = latency_stats([1.0, 2.0, 3.0, 4.0, 100.0])
        self.assertEqual(result["count"], 5)
        self.assertEqual(result["p50_ms"], 3.0)
        self.assertEqual(result["p95_ms"], 100.0)
        self.assertEqual(latency_stats([])["p95_ms"], None)

    def test_jsonl_writer_appends_one_identity_with_contiguous_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            common = {
                "run_id": "m2-test",
                "runtime_id": "runtime-test",
                "run_nonce": "nonce_0123456789abcdef",
            }
            with JsonlWriter(path, phase="good", **common) as writer:
                writer.emit("phase_start")
            with JsonlWriter(path, phase="down", **common) as writer:
                writer.emit("phase_start")
            records = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([record["event_seq"] for record in records], [1, 2])
            self.assertTrue(all(record["schema_version"] == 2 for record in records))
            with self.assertRaises(ValueError):
                JsonlWriter(
                    path,
                    phase="recovery",
                    run_id="m2-test",
                    runtime_id="another-runtime",
                    run_nonce=common["run_nonce"],
                )

    def test_process_reference_supports_cmdline_hash(self) -> None:
        digest = "a" * 64
        reference = parse_process_reference(f"123:456:{digest}")
        self.assertEqual(reference.pid, 123)
        self.assertEqual(reference.start_ticks, 456)
        self.assertEqual(reference.cmdline_sha256, digest)

    def test_current_process_identity_helpers(self) -> None:
        ticks = read_start_ticks(os.getpid())
        digest = read_cmdline_sha256(os.getpid())
        self.assertIsInstance(ticks, int)
        self.assertIsNotNone(digest)
        self.assertEqual(len(digest), 64)

    def test_down_criteria_rejects_any_heartbeat(self) -> None:
        args = Namespace(expected_ack=False, attempts=5)
        result = PhaseResult(5, 0, 0, 1, True, [], [])
        self.assertFalse(criteria_met(args, result, True))
        result = PhaseResult(5, 0, 0, 0, True, [], [])
        self.assertTrue(criteria_met(args, result, True))
        result = PhaseResult(5, 0, 1, 0, True, [], [])
        self.assertFalse(criteria_met(args, result, True))


if __name__ == "__main__":
    unittest.main()
