#!/usr/bin/env python3
"""Unit tests for the durable runner-owned M2 lifecycle journal."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from network.scripts.m2_lifecycle_event import SCHEMA, append_event, parse_args


COMMON = {
    "run_id": "m2-lifecycle-test",
    "runtime_id": "runtime-01234567",
    "run_nonce": "nonce_0123456789abcdef",
}


class M2LifecycleEventTests(unittest.TestCase):
    def test_append_is_identity_bound_contiguous_and_fsyncable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "m2_lifecycle.jsonl"
            first = append_event(output, event="captures_ready", fields={"capture_count": 5}, **COMMON)
            second = append_event(output, event="endpoints_ready", fields={}, **COMMON)
            self.assertEqual((first["event_seq"], second["event_seq"]), (1, 2))
            records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([record["schema"] for record in records], [SCHEMA, SCHEMA])
            self.assertEqual([record["event"] for record in records], ["captures_ready", "endpoints_ready"])
            self.assertLess(records[0]["monotonic_ns"], records[1]["monotonic_ns"])

    def test_mixed_identity_and_reserved_fields_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "m2_lifecycle.jsonl"
            append_event(output, event="captures_ready", fields={}, **COMMON)
            with self.assertRaises(ValueError):
                append_event(
                    output,
                    event="endpoints_ready",
                    fields={},
                    **{**COMMON, "runtime_id": "other-runtime-01234567"},
                )
            with self.assertRaises(ValueError):
                append_event(output, event="endpoints_ready", fields={"event": "forged"}, **COMMON)

    def test_cli_field_parser_preserves_strings_and_strict_json_scalars(self) -> None:
        args = parse_args(
            [
                "--output",
                "/tmp/m2_lifecycle.jsonl",
                "--run-id",
                COMMON["run_id"],
                "--runtime-id",
                COMMON["runtime_id"],
                "--run-nonce",
                COMMON["run_nonce"],
                "--event",
                "good_dwell_complete",
                "--field",
                "phase=good",
                "--field",
                "duration_s=10",
                "--field",
                "healthy=true",
            ]
        )
        self.assertEqual(args.fields, {"phase": "good", "duration_s": 10, "healthy": True})


if __name__ == "__main__":
    unittest.main()
