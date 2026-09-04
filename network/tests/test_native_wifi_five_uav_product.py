#!/usr/bin/env python3
"""Focused checks for the five-UAV native Wi-Fi/Sionna product path."""

from __future__ import annotations

import copy
import csv
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
    assert radio["data_mode"] == "HtMcs0"
    assert radio["control_mode"] == "ErpOfdmRate6Mbps"
    assert radio["technology_specific_modem"] is False
    assert radio["channel_width_mhz"] == 20
    assert radio["tx_power_w"] == 0.01
    assert 0 < sionna["cache_expiry_jitter_fraction"] <= 0.9
    assert sionna["channel_state_max_age_s"] == 20.0
    assert sionna["endpoint_displacement_threshold_m"] == 10.0

    runner = (ROOT / "network/ns3/run_native_radio_five_uav.sh").read_text(
        encoding="utf-8"
    )
    town_scenario = yaml.safe_load(
        (ROOT / "network/config/scenario_5uav_town01_native_product.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert town_scenario["radio"]["config"] == str(config_path.relative_to(ROOT))
    assert 'RADIO_CONFIG="${SCENARIO_VALUES[3]}"' in runner
    assert '--txPowerW="$TX_POWER_W"' in runner
    assert '--sionnaCacheJitterFraction="$SIONNA_CACHE_JITTER_FRACTION"' in runner
    assert "--txPowerW=0.01" not in runner
    assert 'RADIO_BACKEND="${RADIO_VALUES[0]}"' in runner
    assert "reject_product_override BAS_NATIVE_RADIO_BACKEND" in runner


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


def _write_causal_scenario_config(tmp_path: Path) -> dict:
    config = {
        "scenario": {
            "name": "config_driven_test",
            "map": {"id": "custom_rugged_map"},
        },
        "robots": [
            {
                "name": f"uav{index}",
                "nominal_radio_position_m": [float(index), 0.0, 0.0],
            }
            for index in range(1, 6)
        ],
        "flight": {
            "mission_position_tolerance_m": 1.0,
            "missions": {
                f"uav{index}": [
                    {
                        "name": phase,
                        "position_m": [float(index), float(phase_index), 20.0],
                    }
                    for phase_index, phase in enumerate(
                        ("clear_observation", "shadow_observation", "recovery_observation")
                    )
                ]
                for index in range(1, 6)
            },
            "observations": [
                {"name": phase, "probe_packets_per_uav": 10}
                for phase in (
                    "clear_observation",
                    "shadow_observation",
                    "recovery_observation",
                )
            ],
            "causal_expectation": {
                "controlled_uavs": ["uav3", "uav4", "uav5"],
                "shadowed_uavs": ["uav1", "uav2"],
                "clear_min_pdr": 0.9,
                "shadow_max_pdr": 0.25,
                "recovery_min_pdr": 0.9,
                "minimum_shadow_pdr_drop": 0.65,
            },
        },
        "traffic": {
            "diagnostic_retry_interval_s": 20.0,
            "forced_mavlink_stream_intervals": False,
            "p2p_packets_per_direction_per_uav": 10,
            "p2mp_root_transmissions": 20,
            "simultaneous_uplink": {
                "packets_per_uav": 20,
                "packet_payload_bytes": 256,
                "interval_ms": 50,
                "duration_s": 1.0,
                "retransmissions": False,
            },
            "delivery_gates": {
                "p2p_min_delivered_per_direction_per_uav": 9,
                "p2mp_min_delivered_per_uav": 18,
                "simultaneous_min_delivered_per_uav": 1,
                "simultaneous_jain_fairness_min": 0.8,
            },
        },
        "evidence": {
            "phase_aliases": {"raw_clear": "clear_observation"},
            "realtime_gates": {
                "gazebo_mean_rtf_min": 0.95,
                "gazebo_p5_rtf_min": 0.8,
                "applied_position_age_p95_ms_max": 500.0,
            },
            "screenshots": [
                {
                    "phase": "raw_clear",
                    "stem": "custom_clear_frame",
                    "camera": "ridge_camera",
                    "mission_phase": "clear_observation",
                    "required_projected_uavs": ["uav1", "uav5"],
                }
            ],
        },
    }
    path = tmp_path / "scenario.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return SUMMARY.load_scenario_config(path)


def _causal_probe(phase: str, phase_index: int) -> dict:
    per_uav = {}
    sends = []
    deliveries = []
    for index in range(1, 6):
        uav = f"uav{index}"
        delivered = 2 if phase == "shadow_observation" and index <= 2 else 10
        per_uav[uav] = {
            "offered_packets": 10,
            "delivered_packets": delivered,
            "pdr": delivered / 10,
            "latency_ms": [5.0] * delivered,
            "position_m": [float(index), float(phase_index), 20.0],
        }
        sends.extend(
            {"uav": uav, "wire_sequence": phase_index * 100 + sequence}
            for sequence in range(10)
        )
        deliveries.extend(
            {"uav": uav, "wire_sequence": phase_index * 100 + sequence}
            for sequence in range(delivered)
        )
    return {
        "phase": phase,
        "application_retransmissions": False,
        "packet_payload_bytes": 256,
        "inter_packet_interval_ms": 10.0,
        "offered_packets": 50,
        "delivered_packets": sum(row["delivered_packets"] for row in per_uav.values()),
        "per_uav": per_uav,
        "sends": sends,
        "deliveries": deliveries,
    }


def _causal_native_events() -> list[dict]:
    events = []
    for phase in ("clear_observation", "shadow_observation", "recovery_observation"):
        for index in range(1, 6):
            uav = f"uav{index}"
            endpoints = {
                "src_ip": "10.71.1.1",
                "dst_ip": f"10.71.{index}.10",
            }
            events.append(
                {
                    "event": "sionna_link_state",
                    "phase": phase,
                    "node": "cp",
                    "peer": uav,
                    "value": 0.0 if phase == "shadow_observation" and index <= 2 else 3.0,
                }
            )
            if phase == "shadow_observation" and index <= 2:
                # The shadowed group still has two real deliveries in the synthetic probe.
                pass
            for event_name in ("wifi_rx_power", "wifi_phy_rx_end", "phy_rx_ok"):
                events.append(
                    {
                        "event": event_name,
                        "phase": phase,
                        "node": uav,
                        "peer": "",
                        "rx_power_dbm": -82.0 if phase == "shadow_observation" else -45.0,
                        "rx_power_w": 1e-8,
                        **endpoints,
                    }
                )
    return events


def test_summary_loads_phase_aliases_and_screenshots_from_scenario_yaml(
    tmp_path: Path,
) -> None:
    config = _write_causal_scenario_config(tmp_path)

    assert config["scenario_name"] == "config_driven_test"
    assert config["screenshots"] == [
        {
            "phase": "clear_observation",
            "stem": "custom_clear_frame",
            "camera": "ridge_camera",
            "mission_phase": "clear_observation",
            "required_projected_uavs": ["uav1", "uav5"],
        }
    ]
    assert config["mission_targets"]["recovery_observation"]["uav5"] == [
        5.0,
        2.0,
        20.0,
    ]


def test_native_event_schema_keeps_rx_end_neutral_and_parses_sionna_power(
    tmp_path: Path,
) -> None:
    path = tmp_path / "native_radio_events.csv"
    columns = [
        "time_s", "wall_monotonic_ns", "phase", "event", "node", "peer",
        "bytes", "x", "y", "z", "value", "details",
    ]
    raw_rows = [
        [1, 100, "raw_clear", "wifi_rx_power_dbm", "uav1", "", 100, 0, 0, 1, -51.5,
         "packet_uid=7;rx_power_w=7e-9;src_ip=10.71.1.1;dst_ip=10.71.1.10"],
        [1, 101, "raw_clear", "phy_rx_end", "uav1", "", 100, 0, 0, 1, "",
         "packet_uid=7;verdict=not_yet_available;src_ip=10.71.1.1;dst_ip=10.71.1.10"],
        [1, 102, "raw_clear", "phy_rx_drop", "uav1", "", 100, 0, 0, 1, 4,
         "packet_uid=8;reason_code=4;src_ip=10.71.1.1;dst_ip=10.71.1.10"],
        [1, 103, "raw_clear", "sionna_paths", "cp", "uav1", 0, 0, 0, 0, 3,
         "sample=periodic;scope=cp_uav_reciprocal;channel_generation_time_s=0.75;delays_s=1e-8"],
    ]
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(columns)
        writer.writerows(raw_rows)

    events = SUMMARY.native_events(path, {"raw_clear": "clear_observation"})

    assert [event["event"] for event in events] == [
        "wifi_rx_power",
        "wifi_phy_rx_end",
        "wifi_phy_rx_drop",
        "sionna_link_state",
    ]
    assert events[0]["rx_power_dbm"] == -51.5
    assert events[0]["rx_power_w"] == 7e-9
    assert events[1]["verdict"] == "not_yet_available"
    assert events[2]["reason_code"] == 4
    assert events[3]["scope"] == "cp_uav_reciprocal"
    assert events[3]["channel_generation_time_s"] == 0.75
    assert all(event["phase"] == "clear_observation" for event in events)

    receive = SUMMARY.receiver_event_evidence(
        events, "uav1", transmitter="cp", phase="clear_observation"
    )
    assert receive["wifi_phy_rx_end"] == 1
    assert receive["wifi_phy_rx_drop"] == 1
    assert receive["wifi_phy_rx_drop_reason_counts"] == {"4": 1}
    assert receive["phy_rx_ok"] == 0
    assert "not a success" not in receive["decode_semantics"]
    assert "only phy_rx_ok" in receive["decode_semantics"]

    (tmp_path / "metrics").mkdir()
    contention = SUMMARY.summarize_native_contention(
        tmp_path, events, {"radio_backend": "wifi"}
    )
    assert contention["half_duplex"]["uav1"]["wifi_phy_rx_end"] == 1
    assert contention["half_duplex"]["uav1"]["wifi_phy_rx_drop"] == 1


def test_radio_summary_accounts_new_wifi_events_without_inventing_decode(
    tmp_path: Path,
) -> None:
    metrics = tmp_path / "metrics"
    metrics.mkdir()
    (metrics / "native_radio_stats.json").write_text(
        json.dumps({"radio_backend": "wifi"}), encoding="utf-8"
    )
    endpoints = {"src_ip": "10.71.1.1", "dst_ip": "10.71.1.10"}
    events = [
        {
            "event": "live_pose", "phase": "clear_observation", "node": "cp",
            "peer": "", "time_s": 1.0, "wall_monotonic_ns": 1_000_000_000,
            "x": 0.0, "y": 0.0, "z": 1.0, "value": 1.0,
        },
        {
            "event": "live_pose", "phase": "clear_observation", "node": "uav1",
            "peer": "", "time_s": 1.0, "wall_monotonic_ns": 1_000_000_001,
            "x": 10.0, "y": 0.0, "z": 20.0, "value": 1.0,
        },
        {
            "event": "sionna_link_state", "phase": "clear_observation", "node": "cp",
            "peer": "uav1", "time_s": 1.1, "wall_monotonic_ns": 1_100_000_000,
            "x": 0.0, "y": 0.0, "z": 1.0, "value": 3.0,
            "details": "sample=periodic;scope=cp_uav_reciprocal;delays_s=1e-8;2e-8",
            "scope": "cp_uav_reciprocal", "channel_generation_time_s": 1.0,
        },
        {
            "event": "wifi_rx_power", "phase": "clear_observation", "node": "uav1",
            "peer": "", "time_s": 1.1, "packet_uid": 9, "rx_power_dbm": -54.0,
            "rx_power_w": 4e-9, **endpoints,
        },
        {
            "event": "wifi_phy_rx_end", "phase": "clear_observation", "node": "uav1",
            "peer": "", "time_s": 1.1, "packet_uid": 9, **endpoints,
        },
        {
            "event": "wifi_phy_rx_drop", "phase": "clear_observation", "node": "uav1",
            "peer": "", "time_s": 1.1, "packet_uid": 9, **endpoints,
        },
    ]

    SUMMARY.build_radio_observability(tmp_path, events, {}, {})
    link_summary = json.loads(
        (metrics / "radio_link_summary.json").read_text(encoding="utf-8")
    )
    downlink = link_summary["links"]["cp->uav1"]
    reverse = link_summary["links"]["uav1->cp"]

    assert downlink["wifi_rx_power_dbm"]["p50"] == -54.0
    assert downlink["native_wifi_phy_rx_end"] == 1
    assert downlink["native_wifi_phy_rx_drop"] == 1
    assert downlink["native_phy_rx_ok"] == 0
    assert downlink["state"] == "degraded"
    assert reverse["path_sample_basis"] == "reciprocal_cp_uav_channel_params"
    assert link_summary["event_schema"]["canonical_events"]["wifi_phy_rx_end"].startswith(
        "neutral"
    )


def test_causal_clear_shadow_recovery_gate_is_strict_and_config_driven(
    tmp_path: Path,
) -> None:
    config = _write_causal_scenario_config(tmp_path)
    probes = [
        _causal_probe("clear_observation", 0),
        _causal_probe("shadow_observation", 1),
        _causal_probe("recovery_observation", 2),
    ]
    scenario = {
        "predeclared_parameters": {
            "causal_expectation": config["causal_expectation"]
        },
        "causal_link_probes": probes,
    }

    result = SUMMARY.summarize_causal_link_probes(
        tmp_path, scenario, config, _causal_native_events()
    )

    assert result["passed"] is True
    assert result["observed_sequence"] == [
        "clear_observation",
        "shadow_observation",
        "recovery_observation",
    ]
    assert result["per_uav"]["uav1"]["clear_to_shadow_pdr_drop"] == 0.8
    assert result["per_uav"]["uav3"]["shadow"]["pdr"] == 1.0
    assert result["phases"]["clear_observation"]["per_uav"]["uav1"][
        "native_evidence"
    ]["wifi_phy_rx_end"] == 1

    failed_scenario = copy.deepcopy(scenario)
    shadow = failed_scenario["causal_link_probes"][1]
    shadow["per_uav"]["uav1"].update(
        {"delivered_packets": 5, "pdr": 0.5, "latency_ms": [5.0] * 5}
    )
    shadow["delivered_packets"] += 3
    shadow["deliveries"].extend(
        {"uav": "uav1", "wire_sequence": sequence} for sequence in (2, 3, 4)
    )
    failed = SUMMARY.summarize_causal_link_probes(
        tmp_path, failed_scenario, config, _causal_native_events()
    )
    assert failed["passed"] is False
    assert "uav1: shadow PDR above shadow_max_pdr" in failed["failures"]


def test_one_uav_regression_is_only_a_gate_when_requested() -> None:
    checks = {"five_uav": True}
    SUMMARY.add_optional_one_uav_regression_check(checks, None, None)
    assert checks == {"five_uav": True}

    SUMMARY.add_optional_one_uav_regression_check(
        checks, Path("one_uav_run"), None
    )
    assert checks["one_uav_regression"] is False


def test_report_runtime_facts_do_not_embed_town_motion_or_tx_power() -> None:
    scenario = {
        "predeclared_parameters": {
            "flight_missions": {uav: [{"name": "ridge"}] for uav in SUMMARY.UAVS}
        },
        "mission_uav_displacement_m": {uav: 100.0 for uav in SUMMARY.UAVS},
        "holding_uav_displacement_m": {},
    }

    motion = SUMMARY.scenario_motion_pattern(scenario)

    assert motion["mission_uavs"] == list(SUMMARY.UAVS)
    assert motion["holding_uavs"] == []
    serialized_motion = SUMMARY.scenario_motion_pattern(
        {
            "predeclared_parameters": {
                "flight_missions": {
                    str(index): [{"name": "ridge"}] for index in range(1, 6)
                }
            }
        }
    )
    assert serialized_motion["mission_uavs"] == list(SUMMARY.UAVS)
    assert SUMMARY.recorded_tx_power_w({"tx_power_w": 0.05}, scenario) == 0.05
    assert SUMMARY.recorded_tx_power_w({}, scenario) is None
    rugged = SUMMARY.load_scenario_config(
        ROOT / "network/config/scenario_5uav_rock_demo_native_product.yaml"
    )
    assert SUMMARY.recorded_tx_power_w({}, scenario, rugged) == 0.05
    source = SUMMARY_PATH.read_text(encoding="utf-8")
    assert "UAV1 moving and UAV2..UAV5 holding" not in source
    assert "0.01 W" not in source


def test_native_product_runtime_contract_fails_closed() -> None:
    config = SUMMARY.load_scenario_config(
        ROOT / "network/config/scenario_5uav_rock_demo_native_product.yaml"
    )
    radio = config["radio"]
    sionna = config["sionna"]
    stats = {
        "uav_count": 5,
        "radio_node_count": 6,
        "shared_spectrum_channel_count": 1,
        "native_ns3_phy": True,
        "native_ns3_mac": True,
        "custom_packet_error_model": False,
        "custom_scheduler": False,
        "radio_backend": "wifi",
        "profile": radio["profile"],
        "technology_specific_modem": False,
        "neighbor_discovery_mode": radio["neighbor_discovery_mode"],
        "wifi_data_mode": radio["data_mode"],
        "wifi_control_mode": radio["control_mode"],
        "wifi_ssid": radio["ssid"],
        "phased_array_spectrum_propagation_model_count": 1,
        "rx_psd_propagation_model": "ns3::SionnaRtSpectrumPropagationLossModel",
        "sionna_in_process": True,
        "sionna_drives_rx_psd": True,
        "packet_outcome_affected": True,
        "pose_snapshots": 10,
        "stale_pose_samples": 0,
        "solver_profile": sionna["solver_profile"],
        "tx_power_w": radio["tx_power_w"],
        "carrier_hz": radio["carrier_hz"],
        "wifi_channel_number": radio["channel_number"],
        "wifi_actual_channel_number": radio["channel_number"],
        "wifi_actual_center_frequency_hz": radio["carrier_hz"],
        "wifi_channel_width_mhz": radio["channel_width_mhz"],
        "channel_state_max_age_s": sionna["channel_state_max_age_s"],
        "endpoint_displacement_threshold_m": sionna[
            "endpoint_displacement_threshold_m"
        ],
        "readiness_lag_max_ms": sionna["readiness_lag_max_ms"],
        "sionna_solver": {
            key: sionna[key]
            for key in (
                "max_depth",
                "los",
                "specular_reflection",
                "diffuse_reflection",
                "diffraction",
                "edge_diffraction",
                "refraction",
                "synthetic_array",
                "seed",
                "max_number_of_paths",
                "cache_expiry_jitter_fraction",
            )
        },
    }

    assert SUMMARY.native_product_runtime_contract(stats, config)["passed"] is True
    stats["sionna_drives_rx_psd"] = False
    failed = SUMMARY.native_product_runtime_contract(stats, config)
    assert failed["passed"] is False
    assert failed["checks"]["sionna_drives_rx_psd"] is False


def test_real_endpoint_delivery_gates_fail_closed() -> None:
    traffic = SUMMARY.load_scenario_config(
        ROOT / "network/config/scenario_5uav_rock_demo_native_product.yaml"
    )["traffic"]
    p2p = {
        "per_uav": {
            uav: {
                "gcs_to_uav": {"offered": 10, "delivered_unique": 9, "duplicates": 0},
                "uav_to_gcs": {
                    "independently_originated": 10,
                    "delivered_unique": 9,
                    "duplicates": 0,
                },
            }
            for uav in SUMMARY.UAVS
        }
    }
    p2mp = {
        "root_transmissions": 20,
        "application_unicast_copies": 0,
        "command_post_mac_tx": 20,
        "per_receiver": {
            uav: {"receiver_application_deliveries": 18, "duplicates": 0}
            for uav in SUMMARY.UAVS
        },
    }
    shared = {
        "per_uav": {
            uav: {"offered_packets": 20, "delivered_application_packets": 1}
            for uav in SUMMARY.UAVS
        },
        "jain_fairness": 1.0,
    }

    assert SUMMARY.traffic_delivery_checks(p2p, p2mp, shared, traffic) == {
        "p2p": True,
        "p2mp": True,
        "simultaneous": True,
    }
    p2p["per_uav"]["uav1"]["gcs_to_uav"]["delivered_unique"] = 0
    p2mp["per_receiver"]["uav2"]["receiver_application_deliveries"] = 0
    shared["per_uav"]["uav3"]["delivered_application_packets"] = 0
    assert SUMMARY.traffic_delivery_checks(p2p, p2mp, shared, traffic) == {
        "p2p": False,
        "p2mp": False,
        "simultaneous": False,
    }


def test_baseline_p2p_delivery_excludes_later_causal_probe_sequences() -> None:
    scenario = {
        "predeclared_parameters": {"p2p_packets_per_direction_per_uav": 10},
        "p2p": {
            "downlink_sends": [
                {"uav": uav, "sequence": sequence}
                for uav in SUMMARY.UAVS
                for sequence in range(10)
            ],
            "uplink_deliveries": [],
        },
    }
    agents = {
        uav: [
            {
                "event": "receive",
                "kind": "p2p_downlink",
                "sender_id": 0,
                "receiver_id": int(uav[3:]),
                "sequence": 100_000 + sequence,
                "latency_ms": 1.0,
            }
            for sequence in range(10)
        ]
        for uav in SUMMARY.UAVS
    }

    result = SUMMARY.build_p2p(scenario, agents)
    assert all(
        result["per_uav"][uav]["gcs_to_uav"]["delivered_unique"] == 0
        and result["per_uav"][uav]["gcs_to_uav"]["missing_sequences"]
        == list(range(10))
        for uav in SUMMARY.UAVS
    )


def test_realtime_ready_requires_configured_mean_gazebo_rtf(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "gazebo_stats.log").write_text(
        "real_time_factor: 0.90\nreal_time_factor: 0.90\n", encoding="utf-8"
    )
    mobility = {
        "uavs": {
            uav: {"applied_position_age_ms": {"p95": 10.0}}
            for uav in SUMMARY.UAVS
        }
    }
    events = [
        {"event": "realtime_lag", "value": 10.0, "phase": "mission"}
        for _ in range(5)
    ]
    gates = {
        "gazebo_mean_rtf_min": 0.95,
        "gazebo_p5_rtf_min": 0.8,
        "applied_position_age_p95_ms_max": 500.0,
    }

    result = SUMMARY.build_realtime(
        tmp_path, events, mobility, {"readiness_lag_max_ms": 50.0}, gates
    )
    assert result["realtime_readiness"] == "limited"
    assert result["predeclared_readiness_bounds"]["gazebo_rtf_mean_min"] == 0.95
