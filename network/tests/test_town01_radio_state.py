#!/usr/bin/env python3
"""Focused product-config tests for the Town01 radio-state bridge."""

from __future__ import annotations

import copy
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.product.town01_radio_state import (  # noqa: E402
    load_radio,
    load_radio_policy,
    state_record,
)


RADIO_PATH = ROOT / "network/config/radio_24ghz_town01.yaml"


def provider_link(service_rate_bps: int) -> dict[str, object]:
    return {
        "tx": "uav1",
        "rx": "cp",
        "traffic_class": "control",
        "propagation_delay_ns": 167,
        "service_tier_bps": service_rate_bps,
        "per_input": 0.001,
    }


class Town01RadioStateConfigTests(unittest.TestCase):
    def test_repository_config_supplies_capacity_and_supported_rates(self) -> None:
        policy = load_radio_policy(RADIO_PATH)
        self.assertEqual(policy.channel_capacity_bps, 20_000_000)
        self.assertEqual(
            policy.service_rates_bps,
            (20_000_000, 2_000_000, 500_000, 100_000, 10_000, 1_000, 0),
        )
        self.assertEqual(
            load_radio(RADIO_PATH),
            {
                "carrier_hz": 2_400_000_000.0,
                "bandwidth_hz": 20_000_000.0,
                "tx_power_dbm": 33.0,
            },
        )

    def test_state_record_accepts_only_rates_from_passed_radio_config(self) -> None:
        policy = load_radio_policy(RADIO_PATH)
        common = {
            "sequence": 1,
            "query_id": "query-1",
            "response_hash": hashlib.sha256(b"response").hexdigest(),
            "applied_ns": 100,
            "ttl_ns": 1_000,
            "mapping_seed": 42,
            "service_rates_bps": policy.service_rates_bps,
        }
        record = state_record(link=provider_link(2_000_000), **common)
        self.assertEqual(record["service_rate_bps"], 2_000_000)
        with self.assertRaisesRegex(ValueError, "unsupported service tier"):
            state_record(link=provider_link(2_000_001), **common)

    def test_custom_capacity_is_loaded_and_inconsistent_tiers_are_rejected(self) -> None:
        source = yaml.safe_load(RADIO_PATH.read_text(encoding="utf-8"))
        configured_capacity = 21_000_000
        source["ns3"]["channel_rate_bps"] = configured_capacity
        source["service_tier_selection"][0]["service_tier_bps"] = configured_capacity
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "radio.yaml"
            path.write_text(yaml.safe_dump(source), encoding="utf-8")
            policy = load_radio_policy(path)
            self.assertEqual(policy.channel_capacity_bps, configured_capacity)
            self.assertEqual(policy.service_rates_bps[0], configured_capacity)

            invalid = copy.deepcopy(source)
            invalid["service_tier_selection"][0]["service_tier_bps"] -= 1
            path.write_text(yaml.safe_dump(invalid), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "highest service tier"):
                load_radio_policy(path)


if __name__ == "__main__":
    unittest.main()
