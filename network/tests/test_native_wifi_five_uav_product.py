#!/usr/bin/env python3
"""Focused checks for the five-UAV native Wi-Fi/Sionna product path."""

from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
SUMMARY_PATH = ROOT / "scripts/product/summarize_native_radio_five_uav.py"
SPEC = importlib.util.spec_from_file_location("summarize_native_radio_five_uav", SUMMARY_PATH)
assert SPEC and SPEC.loader
SUMMARY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SUMMARY)


def test_product_radio_configuration_is_single_source() -> None:
    config_path = ROOT / "network/config/native_wifi_80211n_spectrum_product.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    radio = config["radio"]
    sionna = config["sionna"]

    assert radio["backend"] == "wifi"
    assert radio["standard"] == "802.11n"
    assert radio["data_mode"] == radio["control_mode"] == "HtMcs0"
    assert radio["channel_width_mhz"] == 20
    assert radio["tx_power_w"] == 0.01
    assert 0 < sionna["cache_expiry_jitter_fraction"] <= 0.9
    assert sionna["channel_state_max_age_s"] == 20.0
    assert sionna["endpoint_displacement_threshold_m"] == 10.0

    runner = (ROOT / "network/ns3/run_native_radio_five_uav.sh").read_text(
        encoding="utf-8"
    )
    assert str(config_path.relative_to(ROOT)) in runner
    assert '--txPowerW="$TX_POWER_W"' in runner
    assert '--sionnaCacheJitterFraction="$SIONNA_CACHE_JITTER_FRACTION"' in runner
    assert "--txPowerW=0.01" not in runner


def test_realtime_patch_spreads_pair_expiry_below_declared_maxima() -> None:
    patch = (ROOT / "network/ns3/patches/mr2608-realtime-scene-cache.patch").read_text(
        encoding="utf-8"
    )
    assert "UpdateJitterFraction" in patch
    assert "pairThresholdScale = 1.0 - m_updateJitterFraction * pairUnit" in patch
    assert "m_updatePeriod.GetSeconds() * pairThresholdScale" in patch
    assert "m_updateDistanceThreshold * pairThresholdScale" in patch


def test_exact_tracker_stream_survives_transient_ros_graph_discovery(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    snapshots = []
    for timestamp in (1.0, 1.1):
        snapshots.append(
            {
                "time_s": timestamp,
                "nodes": [
                    {
                        "id": f"uav{index}",
                        "position_m": [float(index), 0.0, timestamp],
                        "source_topic": f"/uav{index}/odometry",
                        "stale": False,
                    }
                    for index in range(1, 6)
                ],
            }
        )
    (logs / "node_state.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in snapshots), encoding="utf-8"
    )
    for index in range(1, 6):
        text = "Unknown topic\n"
        if index != 3:
            text = (
                "Publisher count: 1\n\n"
                "Node name: ros_gz_bridge\n"
                f"Node namespace: /uav{index}\n"
            )
        (logs / f"odometry_uav{index}.txt").write_text(text, encoding="utf-8")

    mobility = SUMMARY.build_mobility(tmp_path, [])

    assert mobility["all_required_odometry_streams_observed"] is True
    assert mobility["uavs"]["uav3"]["publisher_count"] == 0
    assert mobility["uavs"]["uav3"]["stream_observed"] is True
    assert (
        mobility["uavs"]["uav3"]["stream_observation_basis"]
        == "tracker_received_exact_ros_topic"
    )


def test_versioned_product_evidence_passes_declared_runtime_bounds() -> None:
    path = ROOT / "network/ns3/evidence/native_wifi_80211n_spectrum_product_v1.json"
    evidence = json.loads(path.read_text(encoding="utf-8"))

    assert evidence["pass"] is True
    assert evidence["functional_checks_passed"] == evidence["functional_checks_total"]
    assert evidence["scheduler"]["steady_lag_p95_ms"] <= 50.0
    assert evidence["gazebo"]["mean_rtf"] >= 0.95
    assert evidence["mavlink"]["control_ack_count"] == 5
    assert evidence["topology"]["uart_paths"] == 10
    assert evidence["shared_uplink"]["jain_fairness"] == 1.0
    assert evidence["no_bypass"]["passed"] is True

    primary_summary = ROOT / evidence["primary_run"] / "metrics/five_uav_native_summary.json"
    if primary_summary.is_file():
        summary_bytes = primary_summary.read_bytes()
        summary = json.loads(summary_bytes)
        assert hashlib.sha256(summary_bytes).hexdigest() == evidence["integrity"][
            "primary_summary_sha256"
        ]
        assert summary["realtime"]["gazebo_rtf"]["samples"] == evidence["gazebo"][
            "samples"
        ]
        assert summary["realtime"]["gazebo_rtf"]["mean"] == evidence["gazebo"][
            "mean_rtf"
        ]
