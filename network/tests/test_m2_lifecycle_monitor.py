from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from network.scripts import m2_lifecycle_monitor as monitor


class M2LifecycleMonitorTests(unittest.TestCase):
    def current_reference(self, role: str = "launch") -> monitor.ProcessReference:
        start_ticks = monitor.read_start_ticks(os.getpid())
        command_hash = monitor.read_cmdline_sha256(os.getpid())
        self.assertIsInstance(start_ticks, int)
        self.assertIsInstance(command_hash, str)
        return monitor.ProcessReference(role, os.getpid(), int(start_ticks), str(command_hash))

    def test_parse_role_reference_is_strict_and_named(self) -> None:
        reference = monitor.parse_role_reference(f"adapter=123:456:{'a' * 64}")
        self.assertEqual(reference.role, "adapter")
        self.assertEqual(reference.pid, 123)
        self.assertEqual(reference.start_ticks, 456)
        self.assertEqual(reference.cmdline_sha256, "a" * 64)
        with self.assertRaises(Exception):
            monitor.parse_role_reference(f"unknown=123:456:{'a' * 64}")
        with self.assertRaises(Exception):
            monitor.parse_role_reference("adapter=123:456:not-a-digest")

    def test_one_sample_is_fsynced_jsonl_with_file_identities(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            topology = root / "topology.json"
            queue = root / "queues.yaml"
            output = root / "monitor.jsonl"
            topology.write_text('{"topology":"stable"}\n', encoding="utf-8")
            queue.write_text("control: 64\n", encoding="utf-8")
            topology_hash = hashlib.sha256(topology.read_bytes()).hexdigest()
            queue_hash = hashlib.sha256(queue.read_bytes()).hexdigest()
            reference = self.current_reference()
            config = monitor.MonitorConfig(
                run_id="m2-monitor-test",
                runtime_id="m2-monitor-runtime",
                run_nonce="m2-monitor-nonce",
                output=output,
                roles={"launch": reference},
                topology=monitor.FileInput("topology", topology, topology_hash),
                queue_inputs={"control": monitor.FileInput("control", queue, queue_hash)},
                sample_period_s=0.001,
                duration_s=0.001,
                stop_file=None,
            )
            self.assertEqual(monitor.run_monitor(config), 0)

            records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([record["event_seq"] for record in records], list(range(1, len(records) + 1)))
            self.assertEqual(records[0]["event"], "monitor_start")
            self.assertEqual(records[-1]["event"], "monitor_stop")
            self.assertTrue(all(record["schema"] == monitor.MONITOR_SCHEMA for record in records))
            samples = [record for record in records if record["event"] == "sample"]
            self.assertGreaterEqual(len(samples), 1)
            sample = samples[0]
            self.assertTrue(sample["roles"]["launch"]["alive"])
            self.assertTrue(sample["topology"]["matches_declared"])
            self.assertTrue(sample["queue_inputs"]["control"]["matches_declared"])
            self.assertLessEqual(
                sample["sample_started_monotonic_ns"],
                sample["sample_completed_monotonic_ns"],
            )

    def test_identity_mismatch_is_reported_not_relabelled_alive(self) -> None:
        start_ticks = monitor.read_start_ticks(os.getpid())
        self.assertIsInstance(start_ticks, int)
        reference = monitor.ProcessReference("sitl", os.getpid(), int(start_ticks), "0" * 64)
        observed = monitor.sample_process(reference)
        self.assertTrue(observed["pid_present"])
        self.assertFalse(observed["alive"])
        self.assertFalse(observed["identity_match"])
        self.assertIn("cmdline_sha256_mismatch", observed["mismatches"])
        self.assertNotEqual(observed["cmdline_sha256"], "0" * 64)


if __name__ == "__main__":
    unittest.main()
