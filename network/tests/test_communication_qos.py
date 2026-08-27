"""Focused checks for the single product QoS source and its ns-3 binding."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from network.ns3.tap_packet_engine_config import data_rate_bps, from_repository
from network.scripts.communication_qos import (
    DEFAULT_PATH,
    GATED_PROFILE_NAMES,
    PROFILE_NAMES,
    QosConfigError,
    load_qos,
)
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
        self.assertTrue(config.strict_control_priority)
        self.assertTrue(config.fair_lower_classes_per_uav)
        self.assertTrue(config.ingress_protection_enabled)
        self.assertEqual(
            config.control_reserved_bps, qos["protection"]["control_reserved_bps"]
        )
        self.assertEqual(
            config.event_log_flush_max_delay_ms,
            qos["protection"]["event_log_flush_max_delay_ms"],
        )
        self.assertEqual(
            qos["protection"]["event_log_snapshot_wait_intervals"], 2
        )
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
            for profile in PROFILE_NAMES
        }
        self.assertLess(offered["nominal"], offered["contention"])
        capacity = data_rate_bps(config.radio_rate)
        self.assertLess(offered["contention"], capacity)
        self.assertGreater(offered["controlled_overload"], capacity)
        self.assertEqual(offered["controlled_overload"], offered["meltdown"])
        self.assertTrue(qos["profiles"]["controlled_overload"]["shaping_enabled"])
        self.assertFalse(qos["profiles"]["meltdown"]["shaping_enabled"])
        self.assertEqual(
            qos["profiles"]["controlled_overload"]["max_scheduler_lag_p95_ms"],
            50,
        )
        self.assertEqual(
            qos["profiles"]["controlled_overload"]["min_gazebo_mean_rtf"],
            0.95,
        )

    def test_reserved_control_capacity_cannot_be_consumed_by_lower_rates(self) -> None:
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
        lower = (
            config.payload_admission_rate_bps
            + config.additional_data_admission_rate_bps
        )
        self.assertLessEqual(
            lower + config.control_reserved_bps, data_rate_bps(config.radio_rate)
        )
        self.assertEqual(
            config.shaping_enabled,
            qos["profiles"]["controlled_overload"]["shaping_enabled"],
        )
        meltdown = from_repository(
            uav_count=5,
            duration_ms=500,
            seed=42,
            run=1,
            event_epoch=1,
            self_test=True,
            self_test_burst=1,
            self_test_unknown_tos=False,
            engine_profile="meltdown",
        )
        self.assertFalse(meltdown.shaping_enabled)
        self.assertNotEqual(config.sha256(), meltdown.sha256())
        self.assertTrue(qos["protection"]["deadline_drop_before_radio_decision"])

    def test_duplicate_or_nonordered_priorities_fail_closed(self) -> None:
        document = yaml.safe_load(DEFAULT_PATH.read_text(encoding="utf-8"))
        document["classes"]["payload"]["priority"] = 0
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "qos.yaml"
            path.write_text(yaml.safe_dump(document), encoding="utf-8")
            with self.assertRaises(QosConfigError):
                load_qos(path)

    def test_drain_must_cover_queue_expiry_and_unimplemented_aggregation_fails(self) -> None:
        document = yaml.safe_load(DEFAULT_PATH.read_text(encoding="utf-8"))
        mutations = (
            ("short_drain", lambda value: value["protection"].update(drain_interval_ms=1499)),
            ("aggregation", lambda value: value["serial_aggregation"].update(enabled=True)),
        )
        with tempfile.TemporaryDirectory() as temporary:
            for name, mutate in mutations:
                with self.subTest(name=name):
                    candidate = yaml.safe_load(yaml.safe_dump(document))
                    mutate(candidate)
                    path = Path(temporary) / f"{name}.yaml"
                    path.write_text(yaml.safe_dump(candidate), encoding="utf-8")
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
            for profile in PROFILE_NAMES
        }
        profiles["controlled_overload"]["classes"]["control"] = {
            "pdr": 0.47,
            "latency_ms": {"p95": 7681.5},
        }
        profiles["meltdown"]["classes"]["control"] = {
            "pdr": 0.0,
            "latency_ms": {"p95": 99999.0},
        }

        checks = configured_control_qos_checks(qos, profiles)

        self.assertTrue(checks["nominal_control_required_pdr"])
        self.assertTrue(checks["contention_control_p95_latency"])
        self.assertEqual(
            {key.split("_control_")[0] for key in checks}, set(GATED_PROFILE_NAMES)
        )
        self.assertFalse(checks["controlled_overload_control_required_pdr"])
        self.assertFalse(checks["controlled_overload_control_p95_latency"])
        self.assertFalse(any(key.startswith("meltdown_") for key in checks))


if __name__ == "__main__":
    unittest.main()
