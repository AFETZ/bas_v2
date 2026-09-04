#!/usr/bin/env python3
"""Validate the bounded five-UAV native Wi-Fi/Sionna reference summary."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def validate(summary: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    expected = {
        "profile": "native_wifi_80211n_spectrum_reference_v1",
        "uav_count": 5,
        "radio": "ns3::SpectrumWifiPhy",
        "propagation": "ns3::SionnaRtSpectrumPropagationLossModel",
        "sionna_enabled": True,
        "scalar_fallback": False,
        "application_bypass": False,
    }
    for key, value in expected.items():
        if summary.get(key) != value:
            errors.append(f"{key}: expected {value!r}, got {summary.get(key)!r}")

    thresholds = (
        ("control_pdr", 0.99, lambda actual, bound: actual >= bound, ">="),
        ("control_rtt_p95_ms", 250.0, lambda actual, bound: actual <= bound, "<="),
        ("scheduler_lag_profile_p95_ms", 50.0, lambda actual, bound: actual <= bound, "<="),
        ("mean_rtf", 0.95, lambda actual, bound: actual >= bound, ">="),
        ("jain_fairness", 0.99, lambda actual, bound: actual >= bound, ">="),
    )
    for key, bound, predicate, operator in thresholds:
        value = summary.get(key)
        if not isinstance(value, (int, float)) or not predicate(float(value), bound):
            errors.append(f"{key}: expected {operator} {bound}, got {value!r}")

    if summary.get("wifi_mac_tx_drop") != 0:
        errors.append(f"wifi_mac_tx_drop: expected 0, got {summary.get('wifi_mac_tx_drop')!r}")
    if summary.get("no_starvation") is not True:
        errors.append("no_starvation: expected true")
    if summary.get("pass") is not True or summary.get("exit_code") != 0:
        errors.append("producer did not mark the run successful")

    per_uav = summary.get("per_uav")
    if not isinstance(per_uav, list) or len(per_uav) != 5:
        errors.append("per_uav: expected exactly five records")
    else:
        for expected_uav, record in enumerate(per_uav, start=1):
            if record.get("uav") != expected_uav:
                errors.append(f"per_uav[{expected_uav}]: wrong UAV identity")
            if not record.get("acked"):
                errors.append(f"per_uav[{expected_uav}]: starvation")
            if record.get("acked") != record.get("offered"):
                errors.append(f"per_uav[{expected_uav}]: incomplete ACK accounting")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("summary", type=Path)
    args = parser.parse_args()
    try:
        value = json.loads(args.summary.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"FAIL: cannot read summary: {error}")
        return 2
    errors = validate(value)
    if errors:
        for error in errors:
            print(f"FAIL: {error}")
        return 1
    print(f"PASS: {args.summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
