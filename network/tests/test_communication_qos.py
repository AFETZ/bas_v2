"""Focused checks for the single product QoS source and its ns-3 binding."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from network.ns3.tap_packet_engine_config import from_repository
from network.scripts.communication_qos import DEFAULT_PATH, QosConfigError, load_qos
from scripts.product.summarize_town01_full_stack import configured_control_qos_checks


class CommunicationQosTests(unittest.TestCase):
    def test_one_config_binds_serial_classes_scheduler_and_load_profiles(self) -> None:
        qos = load_qos()
        config = from_repository(
            uav_count=5,
            duration_ms=500,
            seed=42,
            run=1,
            event_epoch=1,
            self_test=True,
            self_test_burst=1,
            self_test_unknown_tos=False,
        )
        self.assertEqual(config.control_burst_limit, qos["scheduler"]["control_burst_limit"])
        self.assertEqual(config.queue_control_deadline_ms, qos["classes"]["control"]["deadline_ms"])
        self.assertEqual(config.queue_payload_max_age_ms, qos["classes"]["payload"]["max_queue_age_ms"])
        self.assertEqual(config.control_tos, qos["classes"]["control"]["tos"])
        offered = {
            profile: sum(
                qos["profiles"][profile][name]["packets_per_second_per_uav"]
                * qos["profiles"][profile][name]["packet_bytes"]
                * 8
                * 5
                for name in ("control", "payload", "additional_data")
            )
            for profile in ("nominal", "contention", "overload")
        }
        self.assertLess(offered["nominal"], offered["contention"])
        self.assertLess(offered["contention"], 20_000_000)
        self.assertGreater(offered["overload"], 20_000_000)

    def test_duplicate_or_nonordered_priorities_fail_closed(self) -> None:
        document = yaml.safe_load(DEFAULT_PATH.read_text(encoding="utf-8"))
        document["classes"]["payload"]["priority"] = 0
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "qos.yaml"
            path.write_text(yaml.safe_dump(document), encoding="utf-8")
            with self.assertRaises(QosConfigError):
                load_qos(path)

    def test_configured_control_thresholds_apply_to_every_profile(self) -> None:
        qos = load_qos()
        profiles = {
            profile: {
                "classes": {
                    "control": {"pdr": 1.0, "latency_ms": {"p95": 5.0}}
                }
            }
            for profile in ("nominal", "contention", "overload")
        }
        profiles["overload"]["classes"]["control"] = {
            "pdr": 0.47,
            "latency_ms": {"p95": 7681.5},
        }

        checks = configured_control_qos_checks(qos, profiles)

        self.assertTrue(checks["nominal_control_required_pdr"])
        self.assertTrue(checks["contention_control_p95_latency"])
        self.assertFalse(checks["overload_control_required_pdr"])
        self.assertFalse(checks["overload_control_p95_latency"])


if __name__ == "__main__":
    unittest.main()
