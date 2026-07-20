from __future__ import annotations

import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "network" / "config"


def load(name: str) -> dict:
    value = yaml.safe_load((CONFIG / name).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"{name} is not a YAML mapping")
    return value


class P0ConfigConsistencyTests(unittest.TestCase):
    def test_frequency_bandwidth_and_capacity_are_consistent(self) -> None:
        backend = load("radio_backend.yaml")["backends"]["sim_2_4ghz"]
        tiers = load("service_tiers.yaml")["service_tiers"]
        max_tier_bps = max(int(tier["target_bps"]) for tier in tiers)

        for scenario_name, radio_name in (
            ("scenario_5uav.yaml", "radio_24ghz.yaml"),
            ("scenario_rock_demo.yaml", "radio_24ghz_rock_demo.yaml"),
        ):
            with self.subTest(scenario=scenario_name):
                scenario_radio = load(scenario_name)["radio"]
                radio_config = load(radio_name)
                radio = radio_config["radio"]
                ns3 = radio_config["ns3"]

                self.assertEqual(radio["carrier_hz"], backend["frequency_hz"])
                self.assertEqual(scenario_radio["carrier_hz"], radio["carrier_hz"])
                self.assertEqual(radio["bandwidth_hz"], backend["bandwidth_hz"])
                self.assertEqual(scenario_radio["bandwidth_hz"], radio["bandwidth_hz"])
                self.assertGreaterEqual(int(ns3["channel_rate_bps"]), max_tier_bps)

                configured_tiers = {
                    int(entry["service_tier_bps"])
                    for entry in radio_config["service_tier_selection"]
                }
                declared_tiers = {int(entry["target_bps"]) for entry in tiers}
                self.assertTrue(configured_tiers.issubset(declared_tiers | {0}))

    def test_primary_jammer_uses_the_selected_channel(self) -> None:
        backend = load("radio_backend.yaml")["backends"]["sim_2_4ghz"]
        enabled = [jammer for jammer in load("jammers.yaml")["jammers"] if jammer["enabled"]]
        self.assertEqual(len(enabled), 1)
        self.assertEqual(enabled[0]["center_hz"], backend["frequency_hz"])
        self.assertEqual(enabled[0]["bandwidth_hz"], backend["bandwidth_hz"])


if __name__ == "__main__":
    unittest.main()
