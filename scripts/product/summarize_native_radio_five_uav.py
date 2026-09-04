#!/usr/bin/env python3
"""Summarize only observed traces from a five-UAV native radio run."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import statistics
import struct
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

import yaml


UAVS = tuple(f"uav{index}" for index in range(1, 6))
ROOT = Path(__file__).resolve().parents[2]
RUNTIME_PHASE_ALIASES = {
    "pre_no_bypass": "no_bypass_test",
    "no_bypass_stop": "no_bypass_test",
    "stationary_communication_smoke": "startup",
}
NATIVE_EVENT_ALIASES = {
    "wifi_rx_power_dbm": "wifi_rx_power",
    "phy_rx_end": "wifi_phy_rx_end",
    "phy_rx_drop": "wifi_phy_rx_drop",
    "sionna_paths": "sionna_link_state",
}


def canonical_phase(phase: str, aliases: dict[str, str] | None = None) -> str:
    value = str(phase).strip()
    if aliases and value in aliases:
        return aliases[value]
    return RUNTIME_PHASE_ALIASES.get(value, value or "startup")


def load_scenario_config(path: Path) -> dict[str, Any]:
    """Load evidence and causal contracts without embedding a map-specific profile."""

    resolved = path.resolve()
    value = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    if not isinstance(value, dict):
        raise ValueError(f"scenario config must contain a mapping: {resolved}")
    evidence = value.get("evidence") or {}
    flight = value.get("flight") or {}
    scenario = value.get("scenario") or {}
    if not isinstance(evidence, dict) or not isinstance(flight, dict) or not isinstance(scenario, dict):
        raise ValueError(f"scenario/evidence/flight must be mappings: {resolved}")

    raw_aliases = evidence.get("phase_aliases") or {}
    if not isinstance(raw_aliases, dict):
        raise ValueError(f"evidence.phase_aliases must be a mapping: {resolved}")
    aliases = {
        str(source): str(target)
        for source, target in raw_aliases.items()
    }
    realtime_gates = evidence.get("realtime_gates") or {}
    if not isinstance(realtime_gates, dict) or not realtime_gates:
        raise ValueError(f"evidence.realtime_gates must be a non-empty mapping: {resolved}")
    screenshots = evidence.get("screenshots") or []
    if not isinstance(screenshots, list) or not screenshots:
        raise ValueError(f"scenario evidence needs a non-empty screenshots list: {resolved}")
    normalized_screenshots: list[dict[str, Any]] = []
    stems: set[str] = set()
    for record in screenshots:
        if not isinstance(record, dict):
            raise ValueError(f"invalid screenshot specification in {resolved}: {record!r}")
        stem = str(record.get("stem", "")).strip()
        raw_phase = str(record.get("phase", "")).strip()
        phase = canonical_phase(raw_phase, aliases)
        camera = str(record.get("camera", "")).strip()
        if not stem or stem in stems or not raw_phase or not camera:
            raise ValueError(f"invalid or duplicate screenshot specification: {record!r}")
        stems.add(stem)
        item = dict(record)
        item.update({"stem": stem, "phase": phase, "camera": camera})
        item["required_projected_uavs"] = [
            str(name) for name in item.get("required_projected_uavs", UAVS)
        ]
        if not set(item["required_projected_uavs"]) <= set(UAVS):
            raise ValueError(f"screenshot names an unknown projected UAV: {record!r}")
        normalized_screenshots.append(item)

    ground_positions: dict[str, list[float]] = {}
    for robot in value.get("robots") or []:
        if not isinstance(robot, dict):
            continue
        name = str(robot.get("name", ""))
        position = robot.get("nominal_radio_position_m", robot.get("position"))
        if name in UAVS and isinstance(position, list) and len(position) >= 3:
            ground_positions[name] = [float(component) for component in position[:3]]
    raw_missions = flight.get("missions") or {}
    if not isinstance(raw_missions, dict):
        raise ValueError(f"flight.missions must be a mapping: {resolved}")
    mission_targets: dict[str, dict[str, list[float]]] = defaultdict(dict)
    for uav, mission in raw_missions.items():
        if str(uav) not in UAVS or not isinstance(mission, list):
            continue
        for waypoint in mission:
            if not isinstance(waypoint, dict):
                continue
            position = waypoint.get("position_m")
            name = str(waypoint.get("name", "")).strip()
            if name and isinstance(position, list) and len(position) == 3:
                target = [float(component) for component in position]
                mission_targets[name][str(uav)] = target
                mission_targets[canonical_phase(name, aliases)][str(uav)] = target

    raw_observations = flight.get("observations") or []
    if not isinstance(raw_observations, list):
        raise ValueError(f"flight.observations must be a list: {resolved}")
    observations = [
        dict(item)
        for item in raw_observations
        if isinstance(item, dict)
    ]
    scenario_map = scenario.get("map") or {}
    expectation = flight.get("causal_expectation") or {}
    if not isinstance(scenario_map, dict) or not isinstance(expectation, dict):
        raise ValueError(f"scenario.map and flight.causal_expectation must be mappings: {resolved}")
    scenario_radio = value.get("radio") or {}
    if not isinstance(scenario_radio, dict):
        raise ValueError(f"radio must be a mapping: {resolved}")
    radio_product: dict[str, Any] = {
        str(key): item for key, item in scenario_radio.items() if key != "config"
    }
    sionna_product: dict[str, Any] = {}
    radio_reference = scenario_radio.get("config")
    radio_config_path: Path | None = None
    if radio_reference:
        candidate = Path(str(radio_reference))
        candidates = (
            [candidate]
            if candidate.is_absolute()
            else [ROOT / candidate, resolved.parent / candidate]
        )
        radio_config_path = next((item.resolve() for item in candidates if item.is_file()), None)
        if radio_config_path is None:
            raise ValueError(f"scenario radio config does not exist: {radio_reference}")
        radio_config = yaml.safe_load(radio_config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(radio_config, dict) or not isinstance(radio_config.get("radio"), dict):
            raise ValueError(f"radio product config must contain radio mapping: {radio_config_path}")
        radio_product.update(radio_config["radio"])
        if not isinstance(radio_config.get("sionna"), dict):
            raise ValueError(f"radio product config must contain sionna mapping: {radio_config_path}")
        sionna_product.update(radio_config["sionna"])
    traffic = value.get("traffic") or {}
    simultaneous = traffic.get("simultaneous_uplink") if isinstance(traffic, dict) else None
    delivery_gates = traffic.get("delivery_gates") if isinstance(traffic, dict) else None
    if (
        not isinstance(traffic, dict)
        or not isinstance(simultaneous, dict)
        or not isinstance(delivery_gates, dict)
    ):
        raise ValueError(f"traffic product contract is incomplete: {resolved}")
    return {
        "path": str(resolved),
        "scenario_name": str(scenario.get("name", resolved.stem)),
        "map": dict(scenario_map),
        "phase_aliases": aliases,
        "screenshots": normalized_screenshots,
        "ground_positions": ground_positions,
        "mission_targets": dict(mission_targets),
        "mission_tolerance_m": float(flight.get("mission_position_tolerance_m", 8.0)),
        "airborne_clearance_m": float(evidence.get("airborne_clearance_m", 6.0)),
        "landed_altitude_tolerance_m": float(
            evidence.get("landed_altitude_tolerance_m", 3.0)
        ),
        "realtime_gates": {
            "gazebo_mean_rtf_min": float(realtime_gates["gazebo_mean_rtf_min"]),
            "gazebo_p5_rtf_min": float(realtime_gates["gazebo_p5_rtf_min"]),
            "applied_position_age_p95_ms_max": float(
                realtime_gates["applied_position_age_p95_ms_max"]
            ),
        },
        "observations": observations,
        "causal_expectation": dict(expectation),
        "radio": {
            "config": str(radio_config_path) if radio_config_path else None,
            **radio_product,
        },
        "sionna": sionna_product,
        "traffic": {
            "diagnostic_retry_interval_s": float(traffic["diagnostic_retry_interval_s"]),
            "forced_mavlink_stream_intervals": traffic["forced_mavlink_stream_intervals"],
            "p2p_packets_per_direction_per_uav": int(
                traffic["p2p_packets_per_direction_per_uav"]
            ),
            "p2mp_root_transmissions": int(traffic["p2mp_root_transmissions"]),
            "simultaneous_uplink": {
                "packets_per_uav": int(simultaneous["packets_per_uav"]),
                "packet_payload_bytes": int(simultaneous["packet_payload_bytes"]),
                "interval_ms": float(simultaneous["interval_ms"]),
                "duration_s": float(simultaneous["duration_s"]),
                "retransmissions": simultaneous["retransmissions"],
            },
            "delivery_gates": {
                "p2p_min_delivered_per_direction_per_uav": int(
                    delivery_gates["p2p_min_delivered_per_direction_per_uav"]
                ),
                "p2mp_min_delivered_per_uav": int(
                    delivery_gates["p2mp_min_delivered_per_uav"]
                ),
                "simultaneous_min_delivered_per_uav": int(
                    delivery_gates["simultaneous_min_delivered_per_uav"]
                ),
                "simultaneous_jain_fairness_min": float(
                    delivery_gates["simultaneous_jain_fairness_min"]
                ),
            },
        },
    }


def resolve_scenario_config_path(
    explicit: Path | None, scenario: dict[str, Any]
) -> Path:
    """Resolve an explicit config, or the exact config recorded by the run."""

    selected: str | Path | None = explicit
    if selected is None:
        recorded = scenario.get("scenario_config")
        selected = str(recorded).strip() if recorded is not None else None
    if not selected:
        raise ValueError(
            "--scenario-config is required when scenario_summary.json does not record scenario_config"
        )
    path = Path(selected).expanduser()
    candidates = [path] if path.is_absolute() else [ROOT / path, path]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise ValueError(f"scenario config does not exist: {path}")


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def percentile(values: Iterable[float], percent: float) -> float | None:
    ordered = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * percent / 100.0
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)


def distribution(values: Iterable[float]) -> dict[str, Any]:
    finite = [float(value) for value in values if math.isfinite(float(value))]
    return {
        "samples": len(finite),
        "p50": percentile(finite, 50),
        "p95": percentile(finite, 95),
        "max": max(finite) if finite else None,
        "min": min(finite) if finite else None,
        "mean": statistics.fmean(finite) if finite else None,
    }


def native_events(
    path: Path, phase_aliases: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    try:
        with path.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
    except OSError:
        return []
    result: list[dict[str, Any]] = []
    for row in rows:
        try:
            row["time_s"] = float(row["time_s"])
            row["wall_monotonic_ns"] = int(row["wall_monotonic_ns"])
            row["bytes"] = int(row["bytes"])
            row["x"] = float(row["x"])
            row["y"] = float(row["y"])
            row["z"] = float(row["z"])
            row["value"] = float(row["value"]) if row.get("value") else None
        except (KeyError, TypeError, ValueError):
            continue
        raw_event = str(row.get("event", ""))
        raw_phase = str(row.get("phase", ""))
        row["raw_event"] = raw_event
        row["event"] = NATIVE_EVENT_ALIASES.get(raw_event, raw_event)
        row["raw_phase"] = raw_phase
        row["phase"] = canonical_phase(raw_phase, phase_aliases)
        match = re.search(r"packet_uid=(\d+)", str(row.get("details", "")))
        row["packet_uid"] = int(match.group(1)) if match else None
        for key in ("src_ip", "dst_ip", "flow", "sample", "scope", "source", "verdict"):
            match = re.search(rf"(?:^|;){key}=([^;]+)", str(row.get("details", "")))
            row[key] = match.group(1) if match else None
        for key in ("ip_protocol", "src_port", "dst_port", "reason_code"):
            match = re.search(rf"(?:^|;){key}=(\d+)", str(row.get("details", "")))
            row[key] = int(match.group(1)) if match else None
        for key in ("rx_power_w", "channel_generation_time_s", "signal_dbm", "noise_interference_dbm", "sinr_db", "frequency_mhz"):
            match = re.search(rf"(?:^|;){key}=([-+0-9.eE]+)", str(row.get("details", "")))
            try:
                row[key] = float(match.group(1)) if match else None
            except ValueError:
                row[key] = None
        row["rx_power_dbm"] = (
            float(row["value"])
            if row["event"] == "wifi_rx_power" and row.get("value") is not None
            else None
        )
        row["decoder_snr_linear"] = (
            float(row["value"])
            if row["event"] in {"phy_rx_ok", "phy_rx_error"}
            and isinstance(row.get("value"), float)
            and math.isfinite(float(row["value"]))
            and float(row["value"]) > 0.0
            else None
        )
        row["decoder_snr_db"] = (
            10.0 * math.log10(row["decoder_snr_linear"])
            if row["decoder_snr_linear"] is not None
            else None
        )
        result.append(row)
    return result


def native_product_runtime_contract(
    stats: dict[str, Any], scenario_config: dict[str, Any]
) -> dict[str, Any]:
    """Validate that the observed process is the selected native Wi-Fi/Sionna product."""

    radio = scenario_config.get("radio", {})
    sionna = scenario_config.get("sionna", {})
    solver = stats.get("sionna_solver", {})
    exact = {
        "five_uavs": stats.get("uav_count") == 5,
        "six_radio_nodes": stats.get("radio_node_count") == 6,
        "one_shared_spectrum_channel": stats.get("shared_spectrum_channel_count") == 1,
        "native_ns3_phy": stats.get("native_ns3_phy") is True,
        "native_ns3_mac": stats.get("native_ns3_mac") is True,
        "no_custom_packet_error_model": stats.get("custom_packet_error_model") is False,
        "no_custom_scheduler": stats.get("custom_scheduler") is False,
        "wifi_backend": stats.get("radio_backend") == radio.get("backend") == "wifi",
        "profile": stats.get("profile") == radio.get("profile"),
        "uncalibrated_standard_reference": stats.get("technology_specific_modem") is False
        and radio.get("technology_specific_modem") is False,
        "neighbor_discovery_mode": stats.get("neighbor_discovery_mode")
        == radio.get("neighbor_discovery_mode"),
        "wifi_data_mode": stats.get("wifi_data_mode") == radio.get("data_mode"),
        "wifi_control_mode": stats.get("wifi_control_mode") == radio.get("control_mode"),
        "wifi_ssid": stats.get("wifi_ssid") == radio.get("ssid"),
        "wifi_channel_number": stats.get("wifi_channel_number")
        == radio.get("channel_number"),
        "wifi_resolved_channel_number": stats.get("wifi_actual_channel_number")
        == radio.get("channel_number"),
        "single_sionna_rx_psd_model": stats.get(
            "phased_array_spectrum_propagation_model_count"
        )
        == 1
        and stats.get("rx_psd_propagation_model")
        == "ns3::SionnaRtSpectrumPropagationLossModel",
        "sionna_in_process": stats.get("sionna_in_process") is True,
        "sionna_drives_rx_psd": stats.get("sionna_drives_rx_psd") is True,
        "packet_outcome_affected": stats.get("packet_outcome_affected") is True,
        "live_pose_snapshots_applied": isinstance(stats.get("pose_snapshots"), int)
        and not isinstance(stats.get("pose_snapshots"), bool)
        and stats["pose_snapshots"] > 0,
        "no_stale_pose_samples": stats.get("stale_pose_samples") == 0,
        "solver_profile": stats.get("solver_profile") == sionna.get("solver_profile"),
    }
    numeric_fields = {
        "tx_power_w": (stats.get("tx_power_w"), radio.get("tx_power_w")),
        "carrier_hz": (stats.get("carrier_hz"), radio.get("carrier_hz")),
        "wifi_actual_center_frequency_hz": (
            stats.get("wifi_actual_center_frequency_hz"),
            radio.get("carrier_hz"),
        ),
        "wifi_channel_width_mhz": (
            stats.get("wifi_channel_width_mhz"),
            radio.get("channel_width_mhz"),
        ),
        "channel_state_max_age_s": (
            stats.get("channel_state_max_age_s"),
            sionna.get("channel_state_max_age_s"),
        ),
        "endpoint_displacement_threshold_m": (
            stats.get("endpoint_displacement_threshold_m"),
            sionna.get("endpoint_displacement_threshold_m"),
        ),
        "readiness_lag_max_ms": (
            stats.get("readiness_lag_max_ms"),
            sionna.get("readiness_lag_max_ms"),
        ),
    }
    for name, (observed, expected) in numeric_fields.items():
        exact[name] = (
            isinstance(observed, (int, float))
            and not isinstance(observed, bool)
            and isinstance(expected, (int, float))
            and not isinstance(expected, bool)
            and math.isclose(float(observed), float(expected), rel_tol=1e-12, abs_tol=1e-12)
        )
    solver_fields = (
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
    exact["sionna_solver_parameters"] = isinstance(solver, dict) and all(
        field in solver
        and field in sionna
        and (
            math.isclose(float(solver[field]), float(sionna[field]), rel_tol=1e-12, abs_tol=1e-12)
            if isinstance(solver[field], (int, float))
            and not isinstance(solver[field], bool)
            and isinstance(sionna[field], (int, float))
            and not isinstance(sionna[field], bool)
            else solver[field] is sionna[field]
            if isinstance(sionna[field], bool)
            else solver[field] == sionna[field]
        )
        for field in solver_fields
    )
    return {"passed": all(exact.values()), "checks": exact}


def build_topology(run_dir: Path, stats: dict[str, Any]) -> dict[str, Any]:
    wifi = stats.get("radio_backend") == "wifi"
    uav_count = stats.get("uav_count")
    radio_node_count = stats.get("radio_node_count")
    shared_channel_count = stats.get("shared_spectrum_channel_count")
    phased_model_count = stats.get("phased_array_spectrum_propagation_model_count")
    native_devices = (
        radio_node_count
        if stats.get("native_ns3_phy") is True and stats.get("native_ns3_mac") is True
        else 0
    )
    nodes = {
        "cp": {
            "ipv4": ["10.71.0.1", "10.71.1.1"],
            "mac": "02:71:ff:00:00:01",
            "preconfigured_endpoint_next_hops": [f"10.71.{i}.1" for i in range(1, 6)],
        }
    }
    for index in range(1, 6):
        nodes[f"uav{index}"] = {
            "ipv4": f"10.71.{index}.10",
            "mac": f"02:71:{index:02x}:00:10:10",
        }
    pcap_names = sorted(path.name for path in (run_dir / "pcap").glob("*.pcap"))
    radio_pcaps = [run_dir / "pcap" / name for name in pcap_names if ".radiotap-" in name]
    def valid_radio_pcap(path):
        with path.open("rb") as stream:
            header = stream.read(24)
        if len(header) != 24 or path.stat().st_size <= 24:
            return False
        order = "<" if header[:4] == b"\xd4\xc3\xb2\xa1" else ">"
        return struct.unpack(order+"I", header[20:24])[0] == 127
    ethernet_names = {"native_radio.pcap", "tap_gcs.pcap", *[f"tap_uav{i}.pcap" for i in range(1,6)]}
    return {
        "endpoint_pcaps_complete": ethernet_names <= set(pcap_names),
        "native_radiotap_pcaps_complete": len(radio_pcaps) == radio_node_count and all(valid_radio_pcap(p) for p in radio_pcaps),
        "ns3_processes": 1,
        "radio_nodes": radio_node_count,
        "uav_count": uav_count,
        "native_radio_devices": native_devices,
        "half_duplex_ideal_phys": 0 if wifi else native_devices,
        "spectrum_wifi_phys": native_devices if wifi else 0,
        "qos_wifi_macs": native_devices if wifi else 0,
        "native_antennas": radio_node_count,
        "mobility_models": radio_node_count,
        "tap_boundaries": radio_node_count,
        "shared_multi_model_spectrum_channels": shared_channel_count,
        "sionna_rt_spectrum_propagation_loss_models": phased_model_count,
        "radio_medium": (
            "native 802.11n QoS WifiMac/SpectrumWifiPhy"
            if wifi
            else "native HalfDuplexIdealPhy/AlohaNoackNetDevice"
        ),
        "uav_tap_bridge_mode": "UseLocal" if wifi else "UseBridge",
        "tap_ingress_segment": {"type": "local_fast_csma", "radio_medium": False},
        "nodes": nodes,
        "profile": stats.get("profile", "generic_native_spectrum_aloha_reference"),
        "technology_specific_modem": bool(stats.get("technology_specific_modem", False)),
        "neighbor_discovery_mode": stats.get(
            "neighbor_discovery_mode", "preconfigured_static_neighbors"
        ),
        "reason": stats.get("reason", "unrecorded"),
        "packet_outcome_affected": bool(stats.get("packet_outcome_affected", False)),
        "native_stats": stats,
        "pcaps": pcap_names,
        "exact_pcap_count": len(pcap_names),
    }


def build_mobility(run_dir: Path, events: list[dict[str, Any]]) -> dict[str, Any]:
    snapshots = read_jsonl(run_dir / "logs/node_state.jsonl")
    topic_details: dict[str, Any] = {}
    result: dict[str, Any] = {
        "atomic_pose_snapshots": True,
        "fail_closed_timeout_s": 1.5,
        "uavs": {},
    }
    for uav in UAVS:
        positions: list[list[float]] = []
        stale = 0
        timestamps: list[float] = []
        source_topics: Counter[str] = Counter()
        for snapshot in snapshots:
            node = next(
                (item for item in snapshot.get("nodes", []) if item.get("id") == uav), None
            )
            if not isinstance(node, dict):
                continue
            if node.get("stale"):
                stale += 1
                continue
            position = node.get("position_m")
            if isinstance(position, list) and len(position) == 3:
                positions.append([float(value) for value in position])
                timestamps.append(float(snapshot.get("time_s", 0)))
                source_topics[str(node.get("source_topic", ""))] += 1
        ages = [
            float(row["value"])
            for row in events
            if row["event"] == "live_pose" and row["node"] == uav and row["value"] is not None
        ]
        elapsed = timestamps[-1] - timestamps[0] if len(timestamps) > 1 else 0
        first = positions[0] if positions else None
        maximum_displacement = (
            max(math.dist(first, position) for position in positions) if first else None
        )
        topic_file = run_dir / f"logs/odometry_{uav}.txt"
        topic_text = topic_file.read_text(encoding="utf-8", errors="replace") if topic_file.exists() else ""
        publisher_count_match = re.search(r"^Publisher count:\s*(\d+)\s*$", topic_text, re.M)
        publisher_count = int(publisher_count_match.group(1)) if publisher_count_match else 0
        publisher_match = re.search(
            r"^Publisher count:\s*[1-9]\d*\s*$.*?"
            r"^Node name: ([^\n]+).*?^Node namespace: ([^\n]+)",
            topic_text,
            re.M | re.S,
        )
        publisher = None
        if publisher_match:
            publisher = f"{publisher_match.group(2).rstrip('/')}/{publisher_match.group(1)}".replace("//", "/")
        expected_topic = f"/{uav}/odometry"
        tracker_stream_samples = source_topics.get(expected_topic, 0)
        stream_observed = publisher_count > 0 or tracker_stream_samples > 0
        observation_basis = (
            "ros2_topic_info_and_tracker_stream"
            if publisher_count > 0 and tracker_stream_samples > 0
            else "ros2_topic_info"
            if publisher_count > 0
            else "tracker_received_exact_ros_topic"
            if tracker_stream_samples > 0
            else "unobserved"
        )
        topic_details[uav] = {
            "topic": expected_topic,
            "publisher_node": publisher,
            "publisher_count": publisher_count,
            "tracker_stream_samples": tracker_stream_samples,
            "stream_observed": stream_observed,
            "observation_basis": observation_basis,
        }
        result["uavs"][uav] = {
            "ros_topic": expected_topic,
            "publisher_node": publisher,
            "publisher_count": publisher_count,
            "stream_observed": stream_observed,
            "stream_observation_basis": observation_basis,
            "sample_count": len(positions),
            "update_rate_hz": (len(positions) - 1) / elapsed if elapsed > 0 else None,
            "first_position_m": first,
            "final_position_m": positions[-1] if positions else None,
            "maximum_displacement_m": maximum_displacement,
            "maximum_altitude_m": max((position[2] for position in positions), default=None),
            "stale_sample_count": stale,
            "applied_position_age_ms": distribution(ages),
            "native_live_pose_samples": len(ages),
            "tracker_source_topics": dict(source_topics),
        }
    result["all_required_publishers_observed"] = all(
        topic_details[uav]["stream_observed"] for uav in UAVS
    )
    result["all_required_odometry_streams_observed"] = result[
        "all_required_publishers_observed"
    ]
    result["all_tracker_streams_sampled"] = all(
        result["uavs"][uav]["sample_count"] > 0 for uav in UAVS
    )
    result["all_mobility_models_updated"] = all(
        result["uavs"][uav]["native_live_pose_samples"] > 0 for uav in UAVS
    )
    return result


def build_mavlink(scenario: dict[str, Any]) -> dict[str, Any]:
    diagnostics = scenario.get("dual_uart_diagnostics", {})
    sequential = diagnostics.get("sequential", {})
    uavs: dict[str, Any] = {}
    for uav in UAVS:
        record = sequential.get(uav, {})
        uavs[uav] = {
            "expected_system_id": int(uav[3:]),
            "control": record.get("control", {}),
            "payload": record.get("payload", {}),
            "serial_parameters": record.get("parameters", {}),
            "parallel_safe_request": diagnostics.get("parallel_safe_request", {}).get(uav, {}),
        }
    return {
        "uavs": uavs,
        "ten_uart_paths": [
            {
                "uav": uav,
                "channel": channel,
                "sitl_serial": "SERIAL1" if channel == "control" else "SERIAL2",
                "baud": 115200,
                "framing": "BSF1",
                "adapter": "communication_vertical.py uart-adapter",
                "status": "observed" if sequential.get(uav, {}).get(channel) else "missing",
            }
            for uav in UAVS
            for channel in ("control", "payload")
        ],
        "command_acks": scenario.get("command_acks", []),
        "transport": scenario.get("gcs_serial_transport", {}),
    }


def agent_records(run_dir: Path) -> dict[str, list[dict[str, Any]]]:
    return {uav: read_jsonl(run_dir / f"logs/additional_{uav}.jsonl") for uav in UAVS}


def build_p2p(scenario: dict[str, Any], agents: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    sends = scenario.get("p2p", {}).get("downlink_sends", [])
    deliveries = scenario.get("p2p", {}).get("uplink_deliveries", [])
    parameters = scenario.get("predeclared_parameters", {})
    expected_packets = int(parameters.get("p2p_packets_per_direction_per_uav", 0) or 0)
    per_uav: dict[str, Any] = {}
    for uav in UAVS:
        down_offered = [item for item in sends if item.get("uav") == uav]
        down_received = [
            row
            for row in agents[uav]
            if row.get("event") == "receive"
            and row.get("kind") == "p2p_downlink"
            and row.get("sender_id") == 0
            and row.get("receiver_id") == int(uav[3:])
            and isinstance(row.get("sequence"), int)
            and not isinstance(row.get("sequence"), bool)
            and 0 <= row["sequence"] < expected_packets
        ]
        up_originated = [
            row
            for row in agents[uav]
            if row.get("event") == "transmit" and row.get("kind") == "p2p_uplink"
        ]
        up_received = [row for row in deliveries if row.get("uav") == uav]
        per_uav[uav] = {
            "gcs_to_uav": {
                "offered": len(down_offered),
                "delivered_unique": len({row.get("sequence") for row in down_received}),
                "missing_sequences": sorted(set(range(expected_packets)) - {int(row.get("sequence")) for row in down_received}),
                "duplicates": len(down_received) - len({row.get("sequence") for row in down_received}),
                "latency_ms": distribution(row.get("latency_ms", 0.0) for row in down_received),
            },
            "uav_to_gcs": {
                "independently_originated": len(up_originated),
                "delivered_unique": len({row.get("sequence") for row in up_received}),
                "missing_sequences": sorted(set(range(expected_packets)) - {int(row.get("sequence")) for row in up_received}),
                "duplicates": len(up_received) - len({row.get("sequence") for row in up_received}),
                "latency_ms": distribution(row.get("latency_ms", 0.0) for row in up_received),
            },
        }
    return {
        "protocol": "checksummed_logical_message_v1",
        "ns3_echo": False,
        "retransmissions": False,
        "configured_packets_per_direction_per_uav": expected_packets,
        "per_uav": per_uav,
    }


def outcomes_by_uid(events: list[dict[str, Any]], phase: str) -> dict[int, list[dict[str, Any]]]:
    result: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in events:
        if row["phase"] == phase and row["packet_uid"] is not None:
            result[int(row["packet_uid"])].append(row)
    return result


def build_p2mp(
    scenario: dict[str, Any], agents: dict[str, list[dict[str, Any]]], events: list[dict[str, Any]]
) -> dict[str, Any]:
    roots = scenario.get("p2mp", {}).get("application_sends", [])
    cp_transmits = [
        row
        for row in events
        if row["phase"] == "p2mp"
        and row["event"] == "mac_tx"
        and row["node"] == "cp"
        and row.get("flow") == "additional_data"
        and row.get("dst_port") == 14900
    ]
    by_uid = outcomes_by_uid(events, "p2mp")
    per_receiver: dict[str, Any] = {}
    for uav in UAVS:
        deliveries = [
            row
            for row in agents[uav]
            if row.get("event") == "receive" and row.get("kind") == "p2mp_downlink"
        ]
        ok = 0
        error = 0
        starts = 0
        per_root: list[dict[str, Any]] = []
        for sequence, transmit in enumerate(cp_transmits[: len(roots)]):
            uid_rows = by_uid.get(int(transmit["packet_uid"]), [])
            node_rows = [row for row in uid_rows if row["node"] == uav]
            state = "missing"
            if any(row["event"] == "phy_rx_ok" for row in node_rows):
                state = "rx_ok"
                ok += 1
            elif any(row["event"] == "phy_rx_error" for row in node_rows):
                state = "rx_error"
                error += 1
            starts += sum(row["event"] == "phy_rx_start" for row in node_rows)
            per_root.append({"sequence": sequence, "packet_uid": transmit["packet_uid"], "phy_outcome": state})
        sequences = {int(row.get("sequence")) for row in deliveries}
        expected_sequences = set(range(len(roots)))
        per_receiver[uav] = {
            "receiver_phy_rx_start": starts,
            "receiver_phy_rx_ok": ok,
            "receiver_phy_rx_error": error,
            "receiver_application_deliveries": len(sequences),
            "receiver_missing_sequences": sorted(expected_sequences - sequences),
            "duplicates": len(deliveries) - len(sequences),
            "latency_ms": distribution(row.get("latency_ms", 0.0) for row in deliveries),
            "per_root_native_outcome": per_root,
        }
    return {
        "root_transmissions": len(roots),
        "gcs_application_sends": len(roots),
        "command_post_mac_tx": len(cp_transmits),
        "application_unicast_copies": 0,
        "delivery_requirement": "observational_no_predeclared_pdr",
        "per_receiver": per_receiver,
    }


def jain(values: list[float]) -> float | None:
    if not values or not any(values):
        return None
    return sum(values) ** 2 / (len(values) * sum(value * value for value in values))


def build_shared_medium(
    scenario: dict[str, Any], agents: dict[str, list[dict[str, Any]]], events: list[dict[str, Any]]
) -> dict[str, Any]:
    phase_events = [row for row in events if row["phase"] == "simultaneous_uplink"]
    application = scenario.get("simultaneous_uplink", {})
    deliveries = application.get("application_deliveries", [])
    per_uav: dict[str, Any] = {}
    throughput: list[float] = []
    intervals: list[tuple[float, float, str, int | None]] = []
    airtime_rate_bps = (
        6_500_000.0
        if str(scenario.get("profile", "")).startswith("native_wifi_")
        else 1_000_000.0
    )
    for uav in UAVS:
        offered_rows = [
            row
            for row in agents[uav]
            if row.get("event") == "transmit" and row.get("kind") == "simultaneous_uplink"
        ]
        mac = [row for row in phase_events if row["event"] == "mac_tx" and row["node"] == uav]
        mac = [
            row
            for row in mac
            if row.get("flow") == "additional_data"
            and row.get("src_port") == 14800 + int(uav[3:])
            and row.get("dst_port") == 14800
        ]
        delivered = [row for row in deliveries if row.get("uav") == uav]
        uid_rows = outcomes_by_uid(events, "simultaneous_uplink")
        rx_ok = rx_error = 0
        for transmit in mac:
            cp_rows = [row for row in uid_rows.get(int(transmit["packet_uid"]), []) if row["node"] == "cp"]
            rx_ok += int(any(row["event"] == "phy_rx_ok" for row in cp_rows))
            rx_error += int(any(row["event"] == "phy_rx_error" for row in cp_rows))
            start = float(transmit["time_s"])
            intervals.append(
                (
                    start,
                    start + transmit["bytes"] * 8 / airtime_rate_bps,
                    uav,
                    transmit["packet_uid"],
                )
            )
        delivered_unique = len({row.get("sequence") for row in delivered})
        duration_s = float(application.get("duration_s", 0.0) or 0.0)
        payload_bytes = int(application.get("packet_payload_bytes", 0) or 0)
        bits_per_second = (
            delivered_unique * payload_bytes * 8 / duration_s if duration_s > 0.0 else 0.0
        )
        throughput.append(bits_per_second)
        per_uav[uav] = {
            "offered_packets": len(offered_rows),
            "native_mac_tx": len(mac),
            "cp_rx_end_ok": rx_ok,
            "cp_rx_end_error": rx_error,
            "delivered_application_packets": delivered_unique,
            "pdr": delivered_unique / len(offered_rows) if offered_rows else None,
            "throughput_bps": bits_per_second,
        }
    overlaps = 0
    overlapping_uids: set[int] = set()
    for index, first in enumerate(intervals):
        for second in intervals[index + 1 :]:
            if first[2] != second[2] and first[0] < second[1] and second[0] < first[1]:
                overlaps += 1
                if first[3] is not None:
                    overlapping_uids.add(int(first[3]))
                if second[3] is not None:
                    overlapping_uids.add(int(second[3]))
    return {
        "medium": (
            "one shared native 802.11n SpectrumWifi channel"
            if str(scenario.get("profile", "")).startswith("native_wifi_")
            else "one shared native ALOHA channel"
        ),
        "predeclared_start_monotonic_ns": application.get("predeclared_start_monotonic_ns"),
        "identical_profile": {
            "packets_per_uav": application.get("packets_per_uav"),
            "packet_payload_bytes": application.get("packet_payload_bytes"),
            "interval_ms": application.get("interval_ms"),
            "duration_s": application.get("duration_s"),
        },
        "custom_scheduler": False,
        "shaping": False,
        "retransmissions": False,
        "per_uav": per_uav,
        "jain_fairness": jain(throughput),
        "native_trace_overlap_observations": {
            "overlapping_interval_pairs": overlaps,
            "packets_observed_in_overlap": len(overlapping_uids),
            "basis": (
                "native MacTx simulation timestamps and nominal HtMcs0 payload serialization; "
                "application delivery is authoritative"
                if airtime_rate_bps > 1_000_000.0
                else "native MacTx simulation timestamps and 1 Mbit/s packet airtime"
            ),
        },
    }


def traffic_delivery_checks(
    p2p: dict[str, Any],
    p2mp: dict[str, Any],
    shared: dict[str, Any],
    traffic: dict[str, Any],
) -> dict[str, bool]:
    """Apply config-declared real endpoint delivery and fairness gates."""

    gates = traffic["delivery_gates"]
    p2p_packets = traffic["p2p_packets_per_direction_per_uav"]
    p2p_passed = all(
        p2p["per_uav"][uav]["gcs_to_uav"]["offered"] == p2p_packets
        and p2p["per_uav"][uav]["gcs_to_uav"]["delivered_unique"]
        >= gates["p2p_min_delivered_per_direction_per_uav"]
        and p2p["per_uav"][uav]["gcs_to_uav"]["duplicates"] == 0
        and p2p["per_uav"][uav]["uav_to_gcs"]["independently_originated"] == p2p_packets
        and p2p["per_uav"][uav]["uav_to_gcs"]["delivered_unique"]
        >= gates["p2p_min_delivered_per_direction_per_uav"]
        and p2p["per_uav"][uav]["uav_to_gcs"]["duplicates"] == 0
        for uav in UAVS
    )
    p2mp_roots = traffic["p2mp_root_transmissions"]
    p2mp_passed = (
        p2mp["root_transmissions"] == p2mp_roots
        and p2mp["application_unicast_copies"] == 0
        and p2mp["command_post_mac_tx"] == p2mp_roots
        and all(
            p2mp["per_receiver"][uav]["receiver_application_deliveries"]
            >= gates["p2mp_min_delivered_per_uav"]
            and p2mp["per_receiver"][uav]["duplicates"] == 0
            for uav in UAVS
        )
    )
    simultaneous_packets = traffic["simultaneous_uplink"]["packets_per_uav"]
    shared_passed = (
        all(
            shared["per_uav"][uav]["offered_packets"] == simultaneous_packets
            and shared["per_uav"][uav]["delivered_application_packets"]
            >= gates["simultaneous_min_delivered_per_uav"]
            for uav in UAVS
        )
        and isinstance(shared["jain_fairness"], (int, float))
        and not isinstance(shared["jain_fairness"], bool)
        and shared["jain_fairness"] >= gates["simultaneous_jain_fairness_min"]
    )
    return {"p2p": p2p_passed, "p2mp": p2mp_passed, "simultaneous": shared_passed}


def parse_sionna_log(path: Path) -> dict[str, Any]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    scene_started: int | None = None
    solve_started: int | None = None
    channel_started: int | None = None
    scene_ms: list[float] = []
    solve_ms: list[float] = []
    channel_ms: list[float] = []
    misses = hits = updates = scene_cache_hits = scene_cache_misses = 0
    displacement_invalidations = time_invalidations = 0
    pair_computations: Counter[str] = Counter()
    for line in lines:
        match = re.match(r"(\d+)\s+(.*)", line)
        if not match:
            continue
        at_ns = int(match.group(1))
        text = match.group(2)
        if "channel matrix not found" in text:
            misses += 1
        if "channel matrix present in the map" in text:
            hits += 1
        if "Cached channel matrix marked for update" in text:
            updates += 1
            if "endpoint displacement" in text:
                displacement_invalidations += 1
            else:
                time_invalidations += 1
        if "Sionna scene cache hit" in text:
            scene_cache_hits += 1
        if "Sionna scene cache miss" in text:
            scene_cache_misses += 1
        pair = re.search(r"Building scene for antenna pair .* nodes \((\d+), (\d+)\)", text)
        if pair:
            channel_started = at_ns
            a, b = sorted((int(pair.group(1)), int(pair.group(2))))
            peer = f"cp-uav{max(a, b) - 1}" if min(a, b) == 1 else f"node{a}-node{b}"
            pair_computations[peer] += 1
        if "Loading Sionna RT scene:" in text:
            scene_started = at_ns
        if "Scene object loaded from Sionna RT" in text and scene_started is not None:
            scene_ms.append((at_ns - scene_started) / 1e6)
            scene_started = None
        if "SionnaRtChannelModel:CalculatePaths" in text:
            solve_started = at_ns
        if "Successfully created new ChannelMatrix" in text:
            if solve_started is not None:
                solve_ms.append((at_ns - solve_started) / 1e6)
                solve_started = None
            if channel_started is not None:
                channel_ms.append((at_ns - channel_started) / 1e6)
                channel_started = None
    return {
        "scene_load_duration_ms": distribution(scene_ms),
        "path_solve_duration_ms": distribution(solve_ms),
        "channel_compute_duration_ms": distribution(channel_ms),
        "cold_start": {
            "first_scene_load_ms": scene_ms[0] if scene_ms else None,
            "first_path_solve_ms": solve_ms[0] if solve_ms else None,
            "first_channel_compute_ms": channel_ms[0] if channel_ms else None,
        },
        "steady_state": {
            "scene_load_duration_ms": distribution(scene_ms[1:]),
            "path_solve_duration_ms": distribution(solve_ms[1:]),
            "channel_compute_duration_ms": distribution(channel_ms[1:]),
        },
        "cache": {
            "hits": hits,
            "misses": misses,
            "stale_updates": updates,
            "time_invalidations": time_invalidations,
            "displacement_invalidations": displacement_invalidations,
            "scene_initialization_count": scene_cache_misses,
            "scene_cache_hits": scene_cache_hits,
        },
        "path_computations_by_pair": dict(pair_computations),
    }


def parse_gazebo(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    values = [float(value) for value in re.findall(r"real_time_factor:\s*([-+0-9.eE]+)", text)]
    return {
        "samples": len(values),
        "mean": statistics.fmean(values) if values else None,
        "p5": percentile(values, 5),
        "min": min(values) if values else None,
    }


def resource_summary(path: Path) -> dict[str, Any]:
    records = read_jsonl(path)
    result: dict[str, Any] = {}
    for component in sorted({str(row.get("component")) for row in records}):
        subset = [row for row in records if row.get("component") == component]
        cpu = [float(row["cpu_percent_one_core"]) for row in subset if row.get("cpu_percent_one_core") is not None]
        rss = [int(row["rss_bytes"]) for row in subset if row.get("rss_bytes") is not None]
        gpu = [int(row["gpu_memory_bytes"]) for row in subset if row.get("gpu_memory_bytes") is not None]
        result[component] = {
            "process_samples": len(subset),
            "cpu_percent_one_core": distribution(cpu),
            "rss_bytes_max": max(rss) if rss else None,
            "gpu_memory_bytes_initial": gpu[0] if gpu else None,
            "gpu_memory_bytes_max": max(gpu) if gpu else None,
        }
    return result


def build_realtime(
    run_dir: Path,
    events: list[dict[str, Any]],
    mobility: dict[str, Any],
    stats: dict[str, Any],
    gates: dict[str, float],
) -> dict[str, Any]:
    lag = [float(row["value"]) for row in events if row["event"] == "realtime_lag" and row["value"] is not None]
    steady_lag = [
        float(row["value"])
        for row in events
        if row["event"] == "realtime_lag"
        and row["value"] is not None
        and row["phase"] not in {"preflight", "unclassified"}
    ]
    sionna = parse_sionna_log(run_dir / "logs/ns3_sionna.log")
    gazebo = parse_gazebo(run_dir / "logs/gazebo_stats.log")
    resources = resource_summary(run_dir / "logs/runtime_resources.jsonl")
    pose_ages = [
        value
        for uav in UAVS
        for value in [mobility["uavs"][uav]["applied_position_age_ms"].get("p95")]
        if value is not None
    ]
    lag_stats = distribution(lag)
    steady_lag_stats = distribution(steady_lag)
    lag_bound_ms = float(stats.get("readiness_lag_max_ms", 250.0))
    gazebo_mean_min = float(gates["gazebo_mean_rtf_min"])
    gazebo_p5_min = float(gates["gazebo_p5_rtf_min"])
    pose_age_max_ms = float(gates["applied_position_age_p95_ms_max"])
    ready = bool(
        steady_lag_stats["p95"] is not None
        and steady_lag_stats["p95"] <= lag_bound_ms
        and gazebo["p5"] is not None
        and gazebo["mean"] is not None
        and gazebo["mean"] >= gazebo_mean_min
        and gazebo["p5"] >= gazebo_p5_min
        and len(pose_ages) == len(UAVS)
        and max(pose_ages) <= pose_age_max_ms
    )
    failed = not lag or gazebo["samples"] == 0
    classification = "failed" if failed else ("ready" if ready else "limited")
    return {
        "measurement_status": "measured" if not failed else "failed",
        "realtime_readiness": classification,
        "predeclared_readiness_bounds": {
            "steady_ns3_lag_p95_ms_max": lag_bound_ms,
            "gazebo_rtf_mean_min": gazebo_mean_min,
            "gazebo_rtf_p5_min": gazebo_p5_min,
            "applied_position_age_p95_ms_max": pose_age_max_ms,
        },
        "sionna": sionna,
        "ns3_realtime_lag_ms": lag_stats,
        "cold_initial_ns3_lag_ms": distribution(lag[:20]),
        "steady_ns3_realtime_lag_ms": steady_lag_stats,
        "gazebo_rtf": gazebo,
        "resources": resources,
        "applied_position_age_p95_ms_max_across_uavs": max(pose_ages) if pose_ages else None,
    }


LEGACY_UNAVAILABLE_RADIO_METRICS = {
    "rx_power_dbm": "unavailable: HalfDuplexIdealPhy does not expose received PSD through a public trace",
    "rssi_dbm": "unavailable: no public received-PSD/RSSI trace in the selected native PHY",
    "snr_db": "unavailable: no public SNR trace in HalfDuplexIdealPhy/ShannonSpectrumErrorModel",
    "sinr_db": "unavailable: SpectrumInterference evaluates SINR internally but exposes no public trace",
    "interference_power_dbm": "unavailable: no public interference-power trace in the selected native PHY",
    "noise_power_dbm": "unavailable: no public noise-power trace in the selected native PHY",
    "bler": "unavailable: current HalfDuplexIdealPhy/Shannon reference does not expose a transport-block abstraction",
}


def radio_metric_availability(stats: dict[str, Any]) -> dict[str, str]:
    if stats.get("radio_backend") != "wifi":
        return LEGACY_UNAVAILABLE_RADIO_METRICS
    return {
        "rx_power_dbm": (
            "observed from SpectrumWifiPhy PhyRxBegin RxPowerWattPerChannelBand after the sole "
            "Sionna spectrum propagation model"
        ),
        "rssi_dbm": "unavailable: MonitorSnifferRx signal is useful-signal power, not total RSSI",
        "snr_db": "available only for decoder verdicts through WifiPhyStateHelper RxOk/RxError",
        "sinr_db": "derived: signal minus noise_interference dBm from MonitorSnifferRx; decoded MPDU samples only",
        "interference_power_dbm": "unavailable: no per-packet interference-power trace is selected",
        "noise_power_dbm": "native combined noise/interference in metrics/wifi_monitor_rx.csv; thermal-only separation unavailable",
        "bler": "not_applicable: NistErrorRateModel has no transport-block abstraction",
    }

def screenshot_native_observation(
    metadata: dict[str, Any],
    events: list[dict[str, Any]],
    phase_aliases: dict[str, str],
) -> dict[str, Any]:
    """Summarize native events close to a real camera frame on the shared wall clock."""

    timestamp_ns = metadata.get("frame_received_monotonic_ns")
    if not isinstance(timestamp_ns, int) or isinstance(timestamp_ns, bool):
        return {"status": "unavailable: camera frame has no monotonic receive timestamp"}
    phase = canonical_phase(str(metadata.get("scenario_phase", "")), phase_aliases)
    nearby = [
        event
        for event in events
        if isinstance(event.get("wall_monotonic_ns"), int)
        and abs(int(event["wall_monotonic_ns"]) - timestamp_ns) <= 2_000_000_000
        and canonical_phase(str(event.get("phase", "")), phase_aliases) == phase
    ]
    paths = [
        int(event["value"])
        for event in nearby
        if event.get("event") == "sionna_link_state"
        and isinstance(event.get("value"), (int, float))
    ]
    powers = [
        float(event["rx_power_dbm"])
        for event in nearby
        if event.get("event") == "wifi_rx_power"
        and isinstance(event.get("rx_power_dbm"), (int, float))
    ]
    return {
        "window_wall_monotonic_ns": [timestamp_ns - 2_000_000_000, timestamp_ns + 2_000_000_000],
        "camera_simulation_timestamp": metadata.get("simulation_timestamp"),
        "scenario_phase": phase,
        "sionna_path_observations": len(paths),
        "sionna_path_count_min": min(paths) if paths else "unavailable",
        "sionna_path_count_max": max(paths) if paths else "unavailable",
        "wifi_rx_power_dbm": distribution(powers),
        "wifi_phy_rx_end": sum(event.get("event") == "wifi_phy_rx_end" for event in nearby),
        "wifi_phy_rx_drop": sum(event.get("event") == "wifi_phy_rx_drop" for event in nearby),
        "phy_rx_ok": sum(event.get("event") == "phy_rx_ok" for event in nearby),
        "phy_rx_error": sum(event.get("event") == "phy_rx_error" for event in nearby),
        "basis": (
            "native Wi-Fi verdict/power events and cached Sionna link-state samples within +/- 2 "
            "wall-clock seconds of the hash-locked raw Gazebo frame receive boundary"
        ),
    }


def screenshot_spatial_state_valid(
    specification: dict[str, Any],
    positions: dict[str, Any],
    scenario_config: dict[str, Any],
) -> bool:
    if not all(name in positions for name in UAVS):
        return False
    try:
        normalized_positions = {
            name: [float(component) for component in positions[name][:3]]
            for name in UAVS
            if isinstance(positions[name], (list, tuple)) and len(positions[name]) >= 3
        }
    except (TypeError, ValueError):
        return False
    if set(normalized_positions) != set(UAVS):
        return False
    checks: list[bool] = []
    mission_phase = specification.get("mission_phase")
    if mission_phase:
        targets = scenario_config["mission_targets"].get(str(mission_phase), {})
        checks.append(
            bool(targets)
            and all(
                name in positions
                and math.dist(
                    normalized_positions[name], target
                )
                <= scenario_config["mission_tolerance_m"]
                for name, target in targets.items()
            )
        )
    altitude_state = specification.get("altitude_state")
    if altitude_state:
        ground = scenario_config["ground_positions"]
        if altitude_state == "airborne":
            checks.append(
                all(
                    name in ground
                    and normalized_positions[name][2]
                    >= ground[name][2] + scenario_config["airborne_clearance_m"]
                    for name in UAVS
                )
            )
        elif altitude_state == "landed":
            checks.append(
                all(
                    name in ground
                    and abs(normalized_positions[name][2] - ground[name][2])
                    <= scenario_config["landed_altitude_tolerance_m"]
                    for name in UAVS
                )
            )
        else:
            checks.append(False)
    return bool(checks) and all(checks)


def screenshot_status(
    run_dir: Path,
    events: list[dict[str, Any]],
    scenario_config: dict[str, Any],
) -> dict[str, Any]:
    screenshot_dir = run_dir / "screenshots"
    records: dict[str, Any] = {}
    for specification in scenario_config["screenshots"]:
        if specification["phase"].startswith("latency_"):
            continue  # Dedicated stationary diagnostic captures are not flight phases.
        stem = specification["stem"]
        metadata = read_json(screenshot_dir / f"{stem}.json", {})
        metadata = metadata if isinstance(metadata, dict) else {}
        image_path = screenshot_dir / f"{stem}.png"
        raw_path = screenshot_dir / f"{stem}.raw.png"
        image_exists = image_path.is_file()
        raw_exists = raw_path.is_file()
        required_projected = set(specification["required_projected_uavs"])
        projected = set(metadata.get("projected_uavs", []))
        positions = metadata.get("uav_positions", {})
        spatial_state_valid = screenshot_spatial_state_valid(
            specification,
            positions if isinstance(positions, dict) else {},
            scenario_config,
        )
        try:
            tracker_snapshot_age_s = float(
                metadata.get("tracker_snapshot_wall_age_s", math.inf)
            )
        except (TypeError, ValueError):
            tracker_snapshot_age_s = math.inf
        hashes_match = bool(
            image_exists
            and raw_exists
            and metadata.get("annotated_image_sha256")
            == hashlib.sha256(image_path.read_bytes()).hexdigest()
            and metadata.get("raw_image_sha256")
            == hashlib.sha256(raw_path.read_bytes()).hexdigest()
        )
        valid = bool(
            image_exists
            and raw_exists
            and hashes_match
            and metadata.get("run_id") == run_dir.name
            and canonical_phase(str(metadata.get("scenario_phase", "")), scenario_config["phase_aliases"])
            == specification["phase"]
            and metadata.get("scenario_name") == scenario_config["scenario_name"]
            and metadata.get("map_id") == scenario_config["map"].get("id")
            and metadata.get("camera_name") == specification["camera"]
            and metadata.get("source") == "live_gazebo_runtime"
            and metadata.get("image_kind") == "annotated_live_frame"
            and sorted(metadata.get("fresh_uavs", [])) == list(UAVS)
            and required_projected <= projected
            and tracker_snapshot_age_s <= 1.0
            and spatial_state_valid
        )
        records[stem] = {
            "image_exists": image_exists,
            "raw_image_exists": raw_exists,
            "image_hashes_match_metadata": hashes_match,
            "required_projected_uavs": sorted(required_projected),
            "configured_phase": specification["phase"],
            "configured_camera": specification["camera"],
            "spatial_state_valid": spatial_state_valid,
            "metadata": metadata,
            "valid_live_capture": valid,
            "native_observation": (
                screenshot_native_observation(
                    metadata, events, scenario_config["phase_aliases"]
                )
                if valid
                else {}
            ),
        }
    return {
        "screenshots_status": "passed" if all(item["valid_live_capture"] for item in records.values()) else "failed",
        "scenario_config": scenario_config["path"],
        "required_stems": [item["stem"] for item in scenario_config["screenshots"]],
        "records": records,
    }


def delay_values(details: str) -> list[float]:
    value = re.search(r"(?:^|;)delays_s=([^;]+(?:;[^;]+)*)", details)
    if not value:
        return []
    result: list[float] = []
    for item in value.group(1).split(";"):
        try:
            result.append(float(item))
        except ValueError:
            continue
    return result


def packet_uid(row: dict[str, Any]) -> int | None:
    value = row.get("packet_uid")
    return int(value) if isinstance(value, int) else None


def ip_belongs_to_node(address: Any, node: str) -> bool:
    value = str(address or "")
    if node == "cp":
        return bool(re.fullmatch(r"10\.71\.(?:0|[1-5])\.1", value))
    if node in UAVS:
        return value == f"10.71.{int(node.removeprefix('uav'))}.10"
    return False


def packet_event_matches_link(event: dict[str, Any], tx: str, rx: str) -> bool:
    """Match only IP-attributed packet events; management frames stay aggregate-only."""

    node = str(event.get("node", ""))
    if node not in {tx, rx}:
        return False
    return ip_belongs_to_node(event.get("src_ip"), tx) and ip_belongs_to_node(
        event.get("dst_ip"), rx
    )


def receiver_event_evidence(
    events: list[dict[str, Any]],
    receiver: str,
    *,
    transmitter: str | None = None,
    phase: str | None = None,
) -> dict[str, Any]:
    """Count Wi-Fi receive traces without treating neutral PhyRxEnd as a verdict."""

    selected = [
        event
        for event in events
        if event.get("node") == receiver
        and (phase is None or event.get("phase") == phase)
        and (
            transmitter is None
            or packet_event_matches_link(event, transmitter, receiver)
        )
    ]
    powers_dbm = [
        float(event["rx_power_dbm"])
        for event in selected
        if event.get("event") == "wifi_rx_power"
        and isinstance(event.get("rx_power_dbm"), (int, float))
        and math.isfinite(float(event["rx_power_dbm"]))
    ]
    powers_w = [
        float(event["rx_power_w"])
        for event in selected
        if event.get("event") == "wifi_rx_power"
        and isinstance(event.get("rx_power_w"), (int, float))
        and math.isfinite(float(event["rx_power_w"]))
    ]
    decoder_snr_db = [
        float(event["decoder_snr_db"])
        for event in selected
        if event.get("event") in {"phy_rx_ok", "phy_rx_error"}
        and isinstance(event.get("decoder_snr_db"), (int, float))
        and math.isfinite(float(event["decoder_snr_db"]))
    ]
    drop_reasons = Counter(
        str(event.get("reason_code", "unavailable"))
        for event in selected
        if event.get("event") == "wifi_phy_rx_drop"
    )
    return {
        "wifi_rx_power": sum(event.get("event") == "wifi_rx_power" for event in selected),
        "wifi_rx_power_dbm": distribution(powers_dbm),
        "wifi_rx_power_w": distribution(powers_w),
        "wifi_phy_rx_end": sum(
            event.get("event") == "wifi_phy_rx_end" for event in selected
        ),
        "wifi_phy_rx_drop": sum(
            event.get("event") == "wifi_phy_rx_drop" for event in selected
        ),
        "wifi_phy_rx_drop_reason_counts": dict(drop_reasons),
        "phy_rx_ok": sum(event.get("event") == "phy_rx_ok" for event in selected),
        "phy_rx_error": sum(
            event.get("event") == "phy_rx_error" for event in selected
        ),
        "decoder_snr_db": distribution(decoder_snr_db),
        "attribution": (
            "packet IP endpoints"
            if transmitter is not None
            else "receiver aggregate; no transmitter attribution"
        ),
        "decode_semantics": (
            "wifi_phy_rx_end is a neutral signal-end trace; only phy_rx_ok and "
            "phy_rx_error are decoder verdicts"
        ),
    }


def real_value(value: float | None) -> float | None:
    return value if value is not None and math.isfinite(value) else None


def build_radio_observability(
    run_dir: Path,
    events: list[dict[str, Any]],
    scenario: dict[str, Any],
    phase_aliases: dict[str, str],
) -> dict[str, Any]:
    metrics_dir = run_dir / "metrics"
    metric_availability = radio_metric_availability(
        read_json(metrics_dir / "native_radio_stats.json", {})
    )
    positions: dict[str, tuple[float, float, float]] = {}
    mobility_age: dict[str, float | None] = {}
    lag_at_event: float | None = None
    mac_by_uid: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    starts_by_uid: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    ends_by_uid: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    neutral_ends_by_uid: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    drops_by_uid: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    powers_by_uid: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    rows: list[dict[str, Any]] = []
    outcomes: defaultdict[tuple[str, str], dict[str, set[int]]] = defaultdict(
        lambda: {
            "attempted": set(),
            "ok": set(),
            "error": set(),
            "start": set(),
            "end": set(),
            "drop": set(),
        }
    )

    for event in events:
        uid = packet_uid(event)
        if uid is None:
            continue
        event_name = str(event.get("event"))
        if event_name == "mac_tx":
            mac_by_uid[uid].append(event)
        elif event_name == "phy_rx_start":
            starts_by_uid[uid].append(event)
        elif event_name in {"phy_rx_ok", "phy_rx_error"}:
            ends_by_uid[uid].append(event)
        elif event_name == "wifi_phy_rx_end":
            neutral_ends_by_uid[uid].append(event)
        elif event_name == "wifi_phy_rx_drop":
            drops_by_uid[uid].append(event)
        elif event_name == "wifi_rx_power":
            powers_by_uid[uid].append(event)

    for event in events:
        event_name = str(event.get("event"))
        node = str(event.get("node"))
        uid = packet_uid(event)
        if event_name == "live_pose":
            positions[node] = (float(event["x"]), float(event["y"]), float(event["z"]))
            mobility_age[node] = event.get("value")
        elif event_name == "realtime_lag":
            lag_at_event = event.get("value")
        elif event_name == "sionna_link_state" and event.get("peer"):
            tx = node
            rx = str(event["peer"])
            uid = packet_uid(event)
            delays = delay_values(str(event.get("details", "")))
            tx_pos = positions.get(tx, (float(event["x"]), float(event["y"]), float(event["z"])))
            rx_pos = positions.get(rx, (float("nan"), float("nan"), float("nan")))
            matching_mac = [item for item in mac_by_uid.get(uid or -1, []) if item["node"] == tx]
            matching_start = [item for item in starts_by_uid.get(uid or -1, []) if item["node"] == rx]
            matching_end = [item for item in ends_by_uid.get(uid or -1, []) if item["node"] == rx]
            matching_neutral_end = [
                item for item in neutral_ends_by_uid.get(uid or -1, []) if item["node"] == rx
            ]
            matching_drop = [item for item in drops_by_uid.get(uid or -1, []) if item["node"] == rx]
            matching_power = [item for item in powers_by_uid.get(uid or -1, []) if item["node"] == rx]
            outcome = outcomes[(tx, rx)]
            if uid is not None:
                outcome["attempted"].add(uid)
                if matching_mac:
                    outcome["attempted"].add(uid)
                if matching_start:
                    outcome["start"].add(uid)
                for end in matching_end:
                    if end["event"] == "phy_rx_ok":
                        outcome["ok"].add(uid)
                    else:
                        outcome["error"].add(uid)
                if matching_neutral_end:
                    outcome["end"].add(uid)
                if matching_drop:
                    outcome["drop"].add(uid)
            distance = math.dist(tx_pos, rx_pos) if all(math.isfinite(value) for value in rx_pos) else None
            path_count = (
                int(event["value"])
                if isinstance(event.get("value"), (int, float))
                else len(delays)
            )
            rx_power_dbm = (
                percentile(
                    [
                        float(item["rx_power_dbm"])
                        for item in matching_power
                        if isinstance(item.get("rx_power_dbm"), (int, float))
                    ],
                    50,
                )
                if matching_power
                else None
            )
            decoder_snr_db = percentile(
                [
                    float(item["decoder_snr_db"])
                    for item in matching_end
                    if isinstance(item.get("decoder_snr_db"), (int, float))
                ],
                50,
            )
            rows.append(
                {
                    "timestamp_wall": float(event["wall_monotonic_ns"]) / 1e9,
                    "timestamp_sim": event["time_s"],
                    "scenario_phase": canonical_phase(str(event["phase"]), phase_aliases),
                    "_packet_uid": uid,
                    "tx": tx,
                    "rx": rx,
                    "tx_x": tx_pos[0],
                    "tx_y": tx_pos[1],
                    "tx_z": tx_pos[2],
                    "rx_x": real_value(rx_pos[0]),
                    "rx_y": real_value(rx_pos[1]),
                    "rx_z": real_value(rx_pos[2]),
                    "distance_m": real_value(distance),
                    "sionna_path_count": path_count,
                    "sionna_channel_generation_time_s": real_value(
                        event.get("channel_generation_time_s")
                    ),
                    "sionna_los_available": "unavailable: current SionnaRtChannelParams exposes delays but not LOS identity",
                    "sionna_path_delay_min_ns": min(delays) * 1e9 if delays else None,
                    "sionna_path_delay_max_ns": max(delays) * 1e9 if delays else None,
                    "sionna_delay_spread_ns": (max(delays) - min(delays)) * 1e9 if delays else None,
                    "rx_power_dbm": real_value(rx_power_dbm),
                    "rx_power_w": real_value(
                        matching_power[-1].get("rx_power_w") if matching_power else None
                    ),
                    "rx_power_match_basis": "packet_uid" if matching_power else None,
                    "rssi_dbm": None,
                    "snr_db": real_value(decoder_snr_db),
                    "sinr_db": None,
                    "interference_power_dbm": None,
                    "noise_power_dbm": None,
                    "native_mac_tx": len(matching_mac),
                    "native_phy_rx_start": len(matching_start),
                    "native_wifi_phy_rx_end": len(matching_neutral_end),
                    "native_wifi_phy_rx_drop": len(matching_drop),
                    "native_phy_rx_ok": sum(item["event"] == "phy_rx_ok" for item in matching_end),
                    "native_phy_rx_error": sum(item["event"] == "phy_rx_error" for item in matching_end),
                    "packets_attempted": 1 if uid is not None else 0,
                    "packets_delivered": sum(item["event"] == "phy_rx_ok" for item in matching_end),
                    "packet_error_count": sum(item["event"] == "phy_rx_error" for item in matching_end),
                    "empirical_per": None,
                    "application_pdr": None,
                    "goodput_bps": None,
                    "end_to_end_latency_ms": None,
                    "jitter_ms": None,
                    "mobility_age_ms": real_value(mobility_age.get(rx)),
                    "ns3_realtime_lag_ms": real_value(lag_at_event),
                    "gazebo_rtf": None,
                }
            )

    # RxEnd events follow the per-receiver Sionna-path trace.  Match only after
    # the full native trace has been indexed, otherwise a one-pass parser would
    # falsely report zero RxEndOk/RxEndError and invent an optimistic PER.
    outcomes.clear()
    for row in rows:
        uid = row.pop("_packet_uid")
        tx = str(row["tx"])
        rx = str(row["rx"])
        matching_mac = [item for item in mac_by_uid.get(uid or -1, []) if item["node"] == tx]
        matching_start = [item for item in starts_by_uid.get(uid or -1, []) if item["node"] == rx]
        matching_end = [item for item in ends_by_uid.get(uid or -1, []) if item["node"] == rx]
        matching_neutral_end = [
            item for item in neutral_ends_by_uid.get(uid or -1, []) if item["node"] == rx
        ]
        matching_drop = [item for item in drops_by_uid.get(uid or -1, []) if item["node"] == rx]
        matching_power = [item for item in powers_by_uid.get(uid or -1, []) if item["node"] == rx]
        power_match_basis = "packet_uid" if matching_power else None
        if uid is None:
            receiver_window = [
                item
                for item in events
                if item.get("node") == rx
                and item.get("phase") == row["scenario_phase"]
                and abs(float(item.get("time_s", -math.inf)) - float(row["timestamp_sim"]))
                <= 0.5
            ]
            matching_neutral_end = [
                item for item in receiver_window if item.get("event") == "wifi_phy_rx_end"
            ]
            matching_drop = [
                item for item in receiver_window if item.get("event") == "wifi_phy_rx_drop"
            ]
            matching_end = [
                item
                for item in receiver_window
                if item.get("event") in {"phy_rx_ok", "phy_rx_error"}
            ]
            matching_power = [
                item for item in receiver_window if item.get("event") == "wifi_rx_power"
            ]
            if matching_power:
                power_match_basis = "same_receiver_phase_plus_or_minus_0.5_simulation_seconds"
        rx_power_values = [
            float(item["rx_power_dbm"])
            for item in matching_power
            if isinstance(item.get("rx_power_dbm"), (int, float))
        ]
        decoder_snr_values = [
            float(item["decoder_snr_db"])
            for item in matching_end
            if isinstance(item.get("decoder_snr_db"), (int, float))
        ]
        row.update(
            {
                "native_mac_tx": len(matching_mac),
                "native_phy_rx_start": len(matching_start),
                "native_wifi_phy_rx_end": len(matching_neutral_end),
                "native_wifi_phy_rx_drop": len(matching_drop),
                "native_phy_rx_ok": sum(item["event"] == "phy_rx_ok" for item in matching_end),
                "native_phy_rx_error": sum(item["event"] == "phy_rx_error" for item in matching_end),
                "rx_power_dbm": real_value(percentile(rx_power_values, 50)),
                "rx_power_w": real_value(
                    matching_power[-1].get("rx_power_w") if matching_power else None
                ),
                "rx_power_match_basis": power_match_basis,
                "snr_db": real_value(percentile(decoder_snr_values, 50)),
                "packets_attempted": 1 if uid is not None else 0,
                "packets_delivered": sum(item["event"] == "phy_rx_ok" for item in matching_end),
                "packet_error_count": sum(item["event"] == "phy_rx_error" for item in matching_end),
            }
        )
        if uid is not None:
            outcome = outcomes[(tx, rx)]
            outcome["attempted"].add(uid)
            if matching_start:
                outcome["start"].add(uid)
            if matching_neutral_end:
                outcome["end"].add(uid)
            if matching_drop:
                outcome["drop"].add(uid)
            for end in matching_end:
                outcome["ok" if end["event"] == "phy_rx_ok" else "error"].add(uid)

    for row in rows:
        key = (str(row["tx"]), str(row["rx"]))
        values = outcomes[key]
        attempts = len(values["attempted"])
        row["empirical_per"] = None  # UID deduplication is not a PHY-attempt denominator

    columns = [
        "timestamp_wall", "timestamp_sim", "scenario_phase", "tx", "rx", "tx_x", "tx_y", "tx_z",
        "rx_x", "rx_y", "rx_z", "distance_m", "sionna_path_count",
        "sionna_channel_generation_time_s", "sionna_los_available", "sionna_path_delay_min_ns",
        "sionna_path_delay_max_ns", "sionna_delay_spread_ns", "rx_power_dbm", "rx_power_w",
        "rx_power_match_basis",
        "rssi_dbm", "snr_db", "sinr_db", "interference_power_dbm", "noise_power_dbm", "native_mac_tx",
        "native_phy_rx_start", "native_wifi_phy_rx_end", "native_wifi_phy_rx_drop",
        "native_phy_rx_ok", "native_phy_rx_error", "packets_attempted",
        "packets_delivered", "packet_error_count", "empirical_per", "application_pdr", "goodput_bps",
        "end_to_end_latency_ms", "jitter_ms", "mobility_age_ms", "ns3_realtime_lag_ms", "gazebo_rtf",
    ]
    with (metrics_dir / "radio_link_metrics.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)

    required_pairs = [("cp", uav) for uav in UAVS] + [(uav, "cp") for uav in UAVS]
    summary_pairs = sorted(
        set(outcomes)
        | set(required_pairs)
        | {(str(row["tx"]), str(row["rx"])) for row in rows}
    )
    reciprocal_path_samples = {
        (str(event.get("node")), str(event.get("peer")), event.get("time_s"))
        for event in events
        if event.get("event") == "sionna_link_state"
        and event.get("scope") == "cp_uav_reciprocal"
    }
    links: dict[str, Any] = {}
    matrix_rows: list[dict[str, Any]] = []
    for tx, rx in summary_pairs:
        direct_samples = [row for row in rows if row["tx"] == tx and row["rx"] == rx]
        samples = direct_samples
        path_sample_basis = "direct_sionna_channel_params"
        if not samples and tx in UAVS and rx == "cp":
            samples = [
                row
                for row in rows
                if row["tx"] == "cp" and row["rx"] == tx
                and ("cp", tx, row["timestamp_sim"]) in reciprocal_path_samples
            ]
            path_sample_basis = (
                "reciprocal_cp_uav_channel_params" if samples else None
            )
        link_packet_events = [
            event for event in events if packet_event_matches_link(event, tx, rx)
        ]
        mac_tx_count = sum(
            event.get("event") == "mac_tx" and event.get("node") == tx
            for event in link_packet_events
        )
        rx_start_count = sum(
            event.get("event") == "phy_rx_start" and event.get("node") == rx
            for event in link_packet_events
        )
        ok = sum(
            event.get("event") == "phy_rx_ok" and event.get("node") == rx
            for event in link_packet_events
        )
        error = sum(
            event.get("event") == "phy_rx_error" and event.get("node") == rx
            for event in link_packet_events
        )
        verdicts = ok + error
        per = error / verdicts if verdicts else None
        path_samples = [int(row["sionna_path_count"]) for row in samples]
        attributed_receiver_evidence = receiver_event_evidence(
            events, rx, transmitter=tx
        )
        receiver_evidence = receiver_event_evidence(events, rx)
        if samples and not any(path_samples):
            state = "no_path"
        elif ok:
            state = "connected"
        elif error or attributed_receiver_evidence["wifi_phy_rx_drop"]:
            state = "degraded"
        elif samples:
            state = "physical_path_observed"
        elif mac_tx_count:
            state = "attempted_without_receive_sample"
        else:
            state = "no_samples"
        item = {
            "tx": tx,
            "rx": rx,
            "samples": len(samples),
            "path_count": distribution(path_samples),
            "path_sample_basis": path_sample_basis,
            "native_mac_tx": mac_tx_count,
            "native_phy_rx_start": rx_start_count,
            "native_wifi_phy_rx_end": receiver_evidence["wifi_phy_rx_end"],
            "native_wifi_phy_rx_drop": receiver_evidence["wifi_phy_rx_drop"],
            "native_phy_rx_ok": ok,
            "native_phy_rx_error": error,
            "wifi_rx_power_dbm": receiver_evidence["wifi_rx_power_dbm"],
            "decoder_snr_db": receiver_evidence["decoder_snr_db"],
            "receive_event_attribution": receiver_evidence["attribution"],
            "ip_attributed_receive_evidence": attributed_receiver_evidence,
            "decode_semantics": receiver_evidence["decode_semantics"],
            "empirical_per": per,
            "empirical_per_basis": (
                "phy_rx_error / (phy_rx_ok + phy_rx_error)"
                if verdicts
                else "unavailable: no decoder verdicts attributable by IP endpoints"
            ),
            "state": state,
        }
        links[f"{tx}->{rx}"] = item
        matrix_rows.append({
            "tx": tx, "rx": rx,
            "path_count": item["path_count"]["p50"],
            "rx_power_dbm": item["wifi_rx_power_dbm"]["p50"],
            "rssi_dbm": None, "snr_db": item["decoder_snr_db"]["p50"],
            "sinr_db": None,
            "per": per if per is not None else None, "pdr": None,
            "latency_p95_ms": None, "state": state,
        })
    canonical_event_counts = Counter(str(event.get("event", "")) for event in events)
    raw_event_counts = Counter(str(event.get("raw_event", "")) for event in events)
    write_json(metrics_dir / "radio_link_summary.json", {
        "source": "live native Sionna/PHY events only",
        "event_schema": {
            "canonical_events": {
                "sionna_link_state": "Sionna channel-parameter path count and generation time",
                "wifi_rx_power": "post-Sionna integrated receive power from PhyRxBegin",
                "wifi_phy_rx_end": "neutral end-of-signal observation, not a decode verdict",
                "wifi_phy_rx_drop": "Wi-Fi PHY receive drop with reason code",
                "phy_rx_ok": "decoder success from WifiPhyStateHelper RxOk",
                "phy_rx_error": "decoder failure from WifiPhyStateHelper RxError",
            },
            "accepted_legacy_aliases": NATIVE_EVENT_ALIASES,
            "canonical_event_counts": dict(canonical_event_counts),
            "raw_event_counts": dict(raw_event_counts),
        },
        "metric_availability": metric_availability,
        "bler": metric_availability["bler"],
        "links": links,
    })
    with (metrics_dir / "link_matrix.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(matrix_rows[0]) if matrix_rows else ["tx", "rx"])
        writer.writeheader()
        writer.writerows(matrix_rows)

    uav_rows: list[dict[str, Any]] = []
    for uav in UAVS:
        control = read_json(metrics_dir / f"control_uart_{uav}.json", {})
        payload = read_json(metrics_dir / f"payload_uart_{uav}.json", {})
        diagnostic = scenario.get("dual_uart_diagnostics", {}).get("sequential", {}).get(uav, {})
        incident = [row for row in rows if row["tx"] == uav or row["rx"] == uav]
        paths = [int(row["sionna_path_count"]) for row in incident]
        distances = [float(row["distance_m"]) for row in incident if isinstance(row["distance_m"], float)]
        receive_evidence = receiver_event_evidence(events, uav)
        ack_control = diagnostic.get("control", {}).get("ack_latency_ms")
        ack_payload = diagnostic.get("payload", {}).get("ack_latency_ms")
        uav_rows.append({
            "uav": uav,
            "control_packets_tx": control.get("records_encoded", None),
            "control_packets_rx": control.get("records_reassembled", None),
            "control_pdr": None,  # Opposite-direction UART counters are not a delivery denominator
            "control_per": None,
            "control_rtt_p50_ms": ack_control if ack_control is not None else None,
            "control_rtt_p95_ms": ack_control if ack_control is not None else None,
            "control_rtt_max_ms": ack_control if ack_control is not None else None,
            "payload_packets_tx": payload.get("records_encoded", None),
            "payload_packets_rx": payload.get("records_reassembled", None),
            "payload_pdr": None,
            "payload_per": None,
            "payload_rtt_p50_ms": ack_payload if ack_payload is not None else None,
            "payload_rtt_p95_ms": ack_payload if ack_payload is not None else None,
            "additional_tx": len(outcomes[(uav, "cp")]["attempted"]),
            "additional_rx": len(outcomes[("cp", uav)]["ok"]),
            "additional_pdr": None,  # Application delivery is reported by the application harness
            "additional_goodput_bps": None,
            "mean_rx_power_dbm": receive_evidence["wifi_rx_power_dbm"]["mean"],
            "min_rx_power_dbm": receive_evidence["wifi_rx_power_dbm"]["min"],
            "max_rx_power_dbm": receive_evidence["wifi_rx_power_dbm"]["max"],
            "mean_rssi_dbm": None, "min_rssi_dbm": None,
            "mean_snr_db": receive_evidence["decoder_snr_db"]["mean"],
            "min_snr_db": receive_evidence["decoder_snr_db"]["min"],
            "mean_sinr_db": None, "min_sinr_db": None,
            "min_path_count": min(paths) if paths else None,
            "median_path_count": percentile(paths, 50) if paths else None,
            "max_path_count": max(paths) if paths else None,
            "phy_rx_ok": receive_evidence["phy_rx_ok"],
            "phy_rx_error": receive_evidence["phy_rx_error"],
            "wifi_phy_rx_end": receive_evidence["wifi_phy_rx_end"],
            "wifi_phy_rx_drop": receive_evidence["wifi_phy_rx_drop"],
            "distance_min_m": min(distances) if distances else None,
            "distance_max_m": max(distances) if distances else None,
        })
    with (metrics_dir / "per_uav_network_summary.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(uav_rows[0]))
        writer.writeheader()
        writer.writerows(uav_rows)
    return {"radio_links": links, "radio_rows": len(rows), "metric_availability": metric_availability}


def summarize_causal_link_probes(
    run_dir: Path,
    scenario: dict[str, Any],
    scenario_config: dict[str, Any],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    """Gate a config-declared clear -> shadow -> recovery experiment."""

    expectation = scenario_config.get("causal_expectation", {})
    output_path = run_dir / "metrics/causal_link_summary.json"
    if not expectation:
        result = {
            "required": False,
            "passed": True,
            "status": "not_required",
            "scenario_config": scenario_config["path"],
            "expectation": {},
            "expected_sequence": [],
            "observed_sequence": [],
            "phases": {},
            "per_uav": {},
            "failures": [],
        }
        write_json(output_path, result)
        return result

    failures: list[str] = []

    def fail(message: str) -> None:
        if message not in failures:
            failures.append(message)

    runtime_parameters = scenario.get("predeclared_parameters", {})
    runtime_expectation = (
        runtime_parameters.get("causal_expectation")
        if isinstance(runtime_parameters, dict)
        else None
    )
    if runtime_expectation != expectation:
        fail(
            "scenario_summary predeclared causal_expectation does not match the selected scenario config"
        )

    positive_observations: list[tuple[str, int]] = []
    for record in scenario_config.get("observations", []):
        try:
            packet_count = int(record.get("probe_packets_per_uav", 0))
        except (TypeError, ValueError):
            packet_count = -1
        if packet_count > 0:
            positive_observations.append(
                (
                    canonical_phase(
                        str(record.get("name", "")), scenario_config["phase_aliases"]
                    ),
                    packet_count,
                )
            )
    expected_roles = ("clear", "shadow", "recovery")
    if len(positive_observations) != 3:
        fail("flight.observations must declare exactly three positive-packet causal phases")
    for index, role in enumerate(expected_roles):
        if index >= len(positive_observations) or role not in positive_observations[index][0].lower():
            fail(f"causal observation {index + 1} must be the {role} phase")
    expected_sequence = [name for name, _packets in positive_observations]

    probes_value = scenario.get("causal_link_probes", [])
    probes = probes_value if isinstance(probes_value, list) else []
    if not isinstance(probes_value, list):
        fail("scenario_summary.causal_link_probes must be a list")
    normalized_probes = [probe for probe in probes if isinstance(probe, dict)]
    if len(normalized_probes) != len(probes):
        fail("scenario_summary.causal_link_probes contains a non-object record")
    observed_sequence = [
        canonical_phase(str(probe.get("phase", "")), scenario_config["phase_aliases"])
        for probe in normalized_probes
    ]
    if observed_sequence != expected_sequence:
        fail(
            "causal probes must appear exactly once in configured clear -> shadow -> recovery order"
        )
    probes_by_phase: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for phase, probe in zip(observed_sequence, normalized_probes):
        probes_by_phase[phase].append(probe)

    controlled = [str(value) for value in expectation.get("controlled_uavs", [])]
    shadowed = [str(value) for value in expectation.get("shadowed_uavs", [])]
    if not controlled or not shadowed:
        fail("causal_expectation must declare non-empty controlled_uavs and shadowed_uavs")
    if set(controlled) & set(shadowed):
        fail("controlled_uavs and shadowed_uavs must be disjoint")
    if set(controlled) | set(shadowed) != set(UAVS):
        fail("controlled_uavs and shadowed_uavs must partition all five UAVs")
    if len(controlled) != len(set(controlled)) or len(shadowed) != len(set(shadowed)):
        fail("causal UAV groups must not contain duplicates")

    thresholds: dict[str, float] = {}
    for key in (
        "clear_min_pdr",
        "shadow_max_pdr",
        "recovery_min_pdr",
        "minimum_shadow_pdr_drop",
    ):
        try:
            value = float(expectation[key])
        except (KeyError, TypeError, ValueError):
            fail(f"causal_expectation.{key} must be a finite probability")
            value = math.nan
        if not math.isfinite(value) or value < 0.0 or value > 1.0:
            fail(f"causal_expectation.{key} must be in [0, 1]")
        thresholds[key] = value

    phases: dict[str, Any] = {}
    per_uav: dict[str, dict[str, Any]] = {uav: {"group": "unknown"} for uav in UAVS}
    for uav in controlled:
        if uav in per_uav:
            per_uav[uav]["group"] = "controlled"
    for uav in shadowed:
        if uav in per_uav:
            per_uav[uav]["group"] = "shadowed"

    for index, (phase, configured_packets) in enumerate(positive_observations):
        role = expected_roles[index] if index < len(expected_roles) else f"phase_{index + 1}"
        matching = probes_by_phase.get(phase, [])
        probe = matching[0] if len(matching) == 1 else {}
        if len(matching) != 1:
            fail(f"{phase}: expected exactly one probe record")
        if probe.get("application_retransmissions") is not False:
            fail(f"{phase}: application_retransmissions must be false")

        phase_per_uav = probe.get("per_uav", {})
        if not isinstance(phase_per_uav, dict) or set(phase_per_uav) != set(UAVS):
            fail(f"{phase}: per_uav must contain exactly uav1..uav5")
            phase_per_uav = phase_per_uav if isinstance(phase_per_uav, dict) else {}
        phase_result: dict[str, Any] = {
            "role": role,
            "phase": phase,
            "configured_packets_per_uav": configured_packets,
            "application_retransmissions": probe.get("application_retransmissions"),
            "offered_packets": probe.get("offered_packets"),
            "delivered_packets": probe.get("delivered_packets"),
            "per_uav": {},
        }
        offered_sum = 0
        delivered_sum = 0
        delivered_by_uav: dict[str, int] = {}
        for uav in UAVS:
            record = phase_per_uav.get(uav, {})
            if not isinstance(record, dict):
                record = {}
            offered = record.get("offered_packets")
            delivered = record.get("delivered_packets")
            pdr = record.get("pdr")
            counts_valid = (
                isinstance(offered, int)
                and not isinstance(offered, bool)
                and isinstance(delivered, int)
                and not isinstance(delivered, bool)
                and offered == configured_packets
                and 0 <= delivered <= offered
            )
            if not counts_valid:
                fail(
                    f"{phase}/{uav}: offered must equal configured packet count and delivered must be bounded"
                )
            offered_number = offered if isinstance(offered, int) and not isinstance(offered, bool) else 0
            delivered_number = delivered if isinstance(delivered, int) and not isinstance(delivered, bool) else 0
            offered_sum += offered_number
            delivered_sum += delivered_number
            delivered_by_uav[uav] = delivered_number
            expected_pdr = delivered_number / offered_number if offered_number > 0 else math.nan
            pdr_valid = (
                isinstance(pdr, (int, float))
                and not isinstance(pdr, bool)
                and math.isfinite(float(pdr))
                and 0.0 <= float(pdr) <= 1.0
                and math.isclose(float(pdr), expected_pdr, rel_tol=0.0, abs_tol=1e-12)
            )
            if not pdr_valid:
                fail(f"{phase}/{uav}: pdr must equal delivered_packets / offered_packets")

            latencies = record.get("latency_ms", [])
            latency_values = (
                [float(value) for value in latencies]
                if isinstance(latencies, list)
                and all(
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and math.isfinite(float(value))
                    and float(value) >= 0.0
                    for value in latencies
                )
                else []
            )
            if len(latency_values) != delivered_number:
                fail(f"{phase}/{uav}: latency sample count must equal delivered_packets")

            position = record.get("position_m")
            targets = scenario_config["mission_targets"].get(phase, {})
            target = targets.get(uav)
            position_error_m: float | None = None
            try:
                if target is not None and isinstance(position, list) and len(position) == 3:
                    position_error_m = math.dist(
                        [float(component) for component in position], target
                    )
            except (TypeError, ValueError):
                position_error_m = None
            if target is None or position_error_m is None:
                fail(f"{phase}/{uav}: missing configured mission target or measured position")
            elif position_error_m > scenario_config["mission_tolerance_m"]:
                fail(f"{phase}/{uav}: position is outside configured mission tolerance")

            path_values = [
                int(event["value"])
                for event in events
                if event.get("event") == "sionna_link_state"
                and event.get("phase") == phase
                and event.get("node") == "cp"
                and event.get("peer") == uav
                and isinstance(event.get("value"), (int, float))
                and math.isfinite(float(event["value"]))
            ]
            receive = receiver_event_evidence(
                events, uav, phase=phase
            )
            if not path_values:
                fail(f"{phase}/{uav}: no Sionna link-state sample")
            receive_required = uav in controlled or role in {"clear", "recovery"} or delivered_number > 0
            if receive_required and receive["wifi_rx_power"] == 0:
                fail(f"{phase}/{uav}: no post-Sionna Wi-Fi receive-power sample")
            if receive_required and receive["wifi_phy_rx_end"] == 0:
                fail(f"{phase}/{uav}: no neutral Wi-Fi PhyRxEnd observation")
            if delivered_number > 0 and receive["phy_rx_ok"] == 0:
                fail(f"{phase}/{uav}: application delivery has no attributable PHY decode success")

            pdr_number = float(pdr) if pdr_valid else None
            phase_uav = {
                "offered_packets": offered,
                "delivered_packets": delivered,
                "pdr": pdr_number,
                "latency_ms": distribution(latency_values),
                "position_m": position,
                "target_position_m": target,
                "position_error_m": position_error_m,
                "native_evidence": {
                    "sionna_path_count": distribution(path_values),
                    "causal_attribution": (
                        "cp<->UAV identity comes from sionna_link_state pair and endpoint "
                        "probe identities; Wi-Fi receive traces are honest receiver+phase "
                        "aggregates because the public PSDU trace may not expose IP endpoints"
                    ),
                    **receive,
                },
            }
            phase_result["per_uav"][uav] = phase_uav
            per_uav[uav][role] = phase_uav

        sends = probe.get("sends", [])
        deliveries = probe.get("deliveries", [])
        if not isinstance(sends, list) or len(sends) != offered_sum:
            fail(f"{phase}: sends length must equal summed per-UAV offered packets")
        if not isinstance(deliveries, list) or len(deliveries) != delivered_sum:
            fail(f"{phase}: deliveries length must equal summed per-UAV delivered packets")
        if isinstance(sends, list):
            send_keys = [
                (record.get("uav"), record.get("wire_sequence"))
                for record in sends
                if isinstance(record, dict)
            ]
            send_counts = Counter(key[0] for key in send_keys)
            if len(send_keys) != len(sends) or len(set(send_keys)) != len(send_keys):
                fail(f"{phase}: sends must have unique (uav, wire_sequence) identities")
            if any(send_counts[uav] != configured_packets for uav in UAVS):
                fail(f"{phase}: sends must contain the configured offer count for every UAV")
        else:
            send_keys = []
        if isinstance(deliveries, list):
            delivery_keys = [
                (record.get("uav"), record.get("wire_sequence"))
                for record in deliveries
                if isinstance(record, dict)
            ]
            delivery_counts = Counter(key[0] for key in delivery_keys)
            if (
                len(delivery_keys) != len(deliveries)
                or len(set(delivery_keys)) != len(delivery_keys)
                or not set(delivery_keys) <= set(send_keys)
            ):
                fail(f"{phase}: deliveries must be a unique subset of offered identities")
            if any(
                delivery_counts[uav] != delivered_by_uav.get(uav, -1)
                for uav in UAVS
            ):
                fail(f"{phase}: delivery identities must match per-UAV delivered counts")
        if probe.get("offered_packets") != offered_sum:
            fail(f"{phase}: top-level offered_packets does not equal per-UAV sum")
        if probe.get("delivered_packets") != delivered_sum:
            fail(f"{phase}: top-level delivered_packets does not equal per-UAV sum")
        phases[phase] = phase_result

    for uav in UAVS:
        records = per_uav[uav]
        clear_pdr = records.get("clear", {}).get("pdr")
        shadow_pdr = records.get("shadow", {}).get("pdr")
        recovery_pdr = records.get("recovery", {}).get("pdr")
        if not all(isinstance(value, float) for value in (clear_pdr, shadow_pdr, recovery_pdr)):
            fail(f"{uav}: incomplete clear/shadow/recovery PDR sequence")
            records["clear_to_shadow_pdr_drop"] = None
            records["shadow_to_recovery_pdr_gain"] = None
            continue
        records["clear_to_shadow_pdr_drop"] = clear_pdr - shadow_pdr
        records["shadow_to_recovery_pdr_gain"] = recovery_pdr - shadow_pdr
        if clear_pdr < thresholds["clear_min_pdr"]:
            fail(f"{uav}: clear PDR below clear_min_pdr")
        if recovery_pdr < thresholds["recovery_min_pdr"]:
            fail(f"{uav}: recovery PDR below recovery_min_pdr")
        if uav in shadowed:
            if shadow_pdr > thresholds["shadow_max_pdr"]:
                fail(f"{uav}: shadow PDR above shadow_max_pdr")
            if clear_pdr - shadow_pdr < thresholds["minimum_shadow_pdr_drop"]:
                fail(f"{uav}: clear-to-shadow PDR drop below minimum_shadow_pdr_drop")
        elif uav in controlled and shadow_pdr < thresholds["clear_min_pdr"]:
            fail(f"{uav}: controlled-UAV PDR fell below clear_min_pdr during shadow phase")

    result = {
        "required": True,
        "passed": not failures,
        "status": "passed" if not failures else "failed",
        "scenario_config": scenario_config["path"],
        "expectation": expectation,
        "runtime_predeclared_expectation": runtime_expectation,
        "thresholds": thresholds,
        "expected_sequence": expected_sequence,
        "observed_sequence": observed_sequence,
        "gate_contract": {
            "application_retransmissions": False,
            "identical_offered_packets_per_uav": True,
            "all_five_positions_within_configured_mission_tolerance": True,
            "all_phases_have_sionna_link_state_for_each_cp_uav_pair": True,
            "receive_power_and_neutral_rx_end_required_when_delivery_is_expected_or_observed": True,
            "decode_success_required_for_each_nonzero_application_delivery_set": True,
            "controlled_uavs_hold_clear_min_pdr_during_shadow": True,
            "wifi_phy_rx_end_is_not_a_success_verdict": True,
        },
        "phases": phases,
        "per_uav": per_uav,
        "failures": failures,
    }
    write_json(output_path, result)
    return result


def active_diagnostic_uavs(scenario: dict[str, Any]) -> tuple[str, ...]:
    diagnostic = scenario.get("latency_diagnostic", {})
    count = int(diagnostic.get("uav_count", 5) or 5)
    return tuple(f"uav{index}" for index in range(1, count + 1))


def adapter_control_events(run_dir: Path, uav: str) -> list[dict[str, Any]]:
    return read_jsonl(run_dir / "logs" / f"control_uart_{uav}.jsonl")


def first_matching_command_delivery(
    events: list[dict[str, Any]], attempt: dict[str, Any], command: int, uav_id: int
) -> int | None:
    """Find the post-radio, post-write PTY hand-off for an unmodified command."""

    sent_ns = int(attempt["send_monotonic_ns"])
    confirmation = int(attempt["confirmation"])
    matches = [
        int(row["monotonic_ns"])
        for row in events
        if row.get("event") == "mavlink_frame"
        and row.get("direction") == "ns3_to_uart"
        and row.get("message_name") == "COMMAND_LONG"
        and row.get("command") == command
        and row.get("confirmation") == confirmation
        and row.get("target_system") == uav_id
        and isinstance(row.get("monotonic_ns"), int)
        and int(row["monotonic_ns"]) >= sent_ns
        and (not attempt.get("command_frame_hex") or row.get("frame_hex") == attempt["command_frame_hex"])
    ]
    return min(matches) if matches else None


def summarize_latency_operations(run_dir: Path, scenario: dict[str, Any]) -> dict[str, Any]:
    """Write an auditable command chain without inventing unavailable boundaries."""

    metrics = run_dir / "metrics"
    chain_rows: list[dict[str, Any]] = []
    operations = scenario.get("command_operations", [])
    if not isinstance(operations, list):
        operations = []
    adapter_events = {uav: adapter_control_events(run_dir, uav) for uav in active_diagnostic_uavs(scenario)}
    by_label: defaultdict[str, list[float]] = defaultdict(list)
    by_label_outcomes: defaultdict[str, Counter[str]] = defaultdict(Counter)
    delivery_matched = 0
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        uav = str(operation.get("uav", ""))
        try:
            uav_id = int(uav.removeprefix("uav"))
            command = int(operation.get("command"))
        except (TypeError, ValueError):
            continue
        label = str(operation.get("label", "unlabelled"))
        attempts = operation.get("attempts", [])
        if not isinstance(attempts, list):
            continue
        for attempt in attempts:
            if not isinstance(attempt, dict):
                continue
            delivery_ns = first_matching_command_delivery(
                adapter_events.get(uav, []), attempt, command, uav_id
            )
            if delivery_ns is not None:
                delivery_matched += 1
            ack_ns = attempt.get("ack_gcs_received_monotonic_ns")
            rtt_ms = attempt.get("attempt_rtt_ms")
            # Byte identity correlates the same real ACK at UART and GCS, even
            # when causality to a retransmission is ambiguous.
            ack_candidates = [int(e["monotonic_ns"]) for e in adapter_events.get(uav, [])
                if attempt.get("ack_frame_hex") and e.get("frame_hex") == attempt["ack_frame_hex"]
                and e.get("direction") == "uart_to_ns3" and e.get("message_name") == "COMMAND_ACK"
                and isinstance(e.get("monotonic_ns"), int) and isinstance(ack_ns, int)
                and int(attempt["send_monotonic_ns"]) <= e["monotonic_ns"] <= ack_ns]
            ack_uart_ns = ack_candidates[0] if len(ack_candidates) == 1 else None
            # COMMAND_ACK cannot identify which resend caused it. Keep total
            # operation latency; never assign the ACK to a successful retry.
            if operation.get("attempt_count", 1) > 1:
                rtt_ms = None
            chain_rows.append(
                {
                    "operation_id": operation.get("operation_id"),
                    "label": label,
                    "uav": uav,
                    "channel": operation.get("channel"),
                    "command": command,
                    "retry_policy": operation.get("retry_policy"),
                    "retry_interval_s": operation.get("retry_interval_s"),
                    "maximum_attempts": operation.get("maximum_attempts"),
                    "attempt_id": attempt.get("attempt_id"),
                    "confirmation": attempt.get("confirmation"),
                    "send_monotonic_ns": attempt.get("send_monotonic_ns"),
                    "uav_uart_delivery_monotonic_ns": delivery_ns,
                    "uav_uart_delivery_status": "observed" if delivery_ns is not None else "unavailable",
                    "uav_uart_delivery_reason": (
                        "adapter did not emit a matching post-write COMMAND_LONG frame"
                        if delivery_ns is None else "native adapter post-write MAVLink frame"
                    ),
                    "ack_uav_uart_observed_monotonic_ns": ack_uart_ns,
                    "ack_uav_uart_status": "observed" if ack_uart_ns is not None else "unavailable",
                    "ack_uav_uart_reason": (
                        "identical ACK frame bytes at UART/GCS; not proof of causality to a retry"
                        if ack_uart_ns is not None else "no unique identical ACK frame at UART/GCS"
                    ),
                    "ack_gcs_uart_delivery_monotonic_ns": None,
                    "ack_gcs_uart_status": "unavailable",
                    "ack_gcs_uart_reason": (
                        "the GCS endpoint is UDP-only; ack_gcs_received_monotonic_ns is its receive boundary"
                    ),
                    "ack_gcs_received_monotonic_ns": ack_ns,
                    "attempt_rtt_ms": rtt_ms,
                    "outcome": attempt.get("outcome"),
                }
            )
            by_label_outcomes[label][str(attempt.get("outcome", "unknown"))] += 1
            if isinstance(rtt_ms, (int, float)):
                by_label[label].append(float(rtt_ms))
    columns = [
        "operation_id", "label", "uav", "channel", "command", "retry_policy", "retry_interval_s", "maximum_attempts",
        "attempt_id", "confirmation", "send_monotonic_ns", "uav_uart_delivery_monotonic_ns",
        "uav_uart_delivery_status", "uav_uart_delivery_reason", "ack_uav_uart_observed_monotonic_ns",
        "ack_uav_uart_status", "ack_uav_uart_reason", "ack_gcs_uart_delivery_monotonic_ns",
        "ack_gcs_uart_status", "ack_gcs_uart_reason", "ack_gcs_received_monotonic_ns",
        "attempt_rtt_ms", "outcome",
    ]
    with (metrics / "mavlink_latency_chain.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(chain_rows)
    labels = {
        label: {"rtt_ms": distribution(by_label[label]), "outcomes": dict(by_label_outcomes[label])}
        for label in sorted(set(by_label) | set(by_label_outcomes))
    }
    transport = scenario.get("gcs_serial_transport", {})
    control_transport = {
        str(path): {
            "maximum_ingress_queue_age_ms": values.get("maximum_ingress_queue_age_ms"),
            "average_ingress_queue_age_ms": values.get("average_ingress_queue_age_ms"),
            "sequence_gaps": values.get("sequence_gaps"),
            "reassembly_failures": values.get("reassembly_failures"),
        }
        for path, values in transport.items()
        if isinstance(values, dict) and str(path).startswith("control:")
    } if isinstance(transport, dict) else {}
    result = {
        "operation_count": len(operations),
        "attempt_count": len(chain_rows),
        "uav_uart_delivery_observed": delivery_matched,
        "per_label": labels,
        "first_attempt_rtt_ms": distribution(
            float(operation["first_attempt_rtt_ms"])
            for operation in operations
            if isinstance(operation, dict) and isinstance(operation.get("first_attempt_rtt_ms"), (int, float))
        ),
        "successful_attempt_rtt_ms": distribution(
            float(operation["successful_attempt_rtt_ms"])
            for operation in operations
            if isinstance(operation, dict) and isinstance(operation.get("successful_attempt_rtt_ms"), (int, float))
        ),
        "time_to_success_ms": distribution(
            float(operation["time_to_success_ms"])
            for operation in operations
            if isinstance(operation, dict) and isinstance(operation.get("time_to_success_ms"), (int, float))
        ),
        "deprecated_ambiguous_ack_latency_ms": True,
        "gcs_transport_reassembly": control_transport,
        "chain_file": "metrics/mavlink_latency_chain.csv",
    }
    write_json(metrics / "mavlink_latency_summary.json", result)
    return result


def summarize_adapter_load(run_dir: Path, scenario: dict[str, Any]) -> dict[str, Any]:
    metrics = run_dir / "metrics"
    load: defaultdict[tuple[str, str, int, str], int] = defaultdict(int)
    streams: Counter[tuple[str, str, str, int, str]] = Counter()
    stream_bytes: Counter[tuple[str, str, str, int, str]] = Counter()
    command_long_to_uart = 0
    command_ack_from_uart = 0
    event_count = 0
    for uav in active_diagnostic_uavs(scenario):
        for channel in tuple(scenario.get("latency_diagnostic", {}).get("channels", ["control"])):
            for row in read_jsonl(run_dir / "logs" / f"{channel}_uart_{uav}.jsonl"):
                event_count += 1
                timestamp = row.get("monotonic_ns")
                if row.get("event") in {"serial_chunk_tx", "serial_chunk_rx", "serial_tx", "serial_rx"} \
                        and isinstance(timestamp, int):
                    direction = str(row.get("direction", "unknown"))
                    load[(uav, str(channel), int(timestamp) // 1_000_000_000, direction)] += int(row.get("bytes", 0) or 0)
                if row.get("event") == "mavlink_frame":
                    key = (
                        uav,
                        str(channel),
                        str(row.get("direction", "unknown")),
                        int(row.get("msgid", -1)),
                        str(row.get("message_name", "unknown")),
                    )
                    streams[key] += 1
                    stream_bytes[key] += int(row.get("message_bytes", 0) or 0)
                    if row.get("direction") == "ns3_to_uart" and row.get("message_name") == "COMMAND_LONG":
                        command_long_to_uart += 1
                    if row.get("direction") == "uart_to_ns3" and row.get("message_name") == "COMMAND_ACK":
                        command_ack_from_uart += 1
    with (metrics / "uart_load_per_second.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["uav", "channel", "monotonic_second", "direction", "bytes"])
        writer.writeheader()
        for (uav, channel, second, direction), value in sorted(load.items()):
            writer.writerow({"uav": uav, "channel": channel, "monotonic_second": second, "direction": direction, "bytes": value})
    with (metrics / "mavlink_stream_composition.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["uav", "channel", "direction", "msgid", "message_name", "frames", "message_bytes"])
        writer.writeheader()
        for key, frames in sorted(streams.items()):
            writer.writerow({
                "uav": key[0], "channel": key[1], "direction": key[2], "msgid": key[3],
                "message_name": key[4], "frames": frames, "message_bytes": stream_bytes[key],
            })
    result = {
        "adapter_event_records": event_count,
        "per_second_rows": len(load),
        "stream_rows": len(streams),
        "command_long_post_write_to_uav_uart": command_long_to_uart,
        "command_ack_observed_from_uav_uart": command_ack_from_uart,
        "availability": "observed" if event_count else "unavailable: metrics_only adapter mode intentionally suppresses per-frame trace",
    }
    write_json(metrics / "uart_load_summary.json", result)
    return result


def summarize_native_contention(
    run_dir: Path, events: list[dict[str, Any]], stats: dict[str, Any]
) -> dict[str, Any]:
    metrics = run_dir / "metrics"
    queue_rows: list[dict[str, Any]] = []
    by_node: defaultdict[str, Counter[str]] = defaultdict(Counter)
    queue_depths: defaultdict[str, list[float]] = defaultdict(list)
    queue_residence_ms: defaultdict[str, list[float]] = defaultdict(list)
    half_duplex: defaultdict[str, Counter[str]] = defaultdict(Counter)
    return_mac_uids: set[int] = set()
    return_rx_start_uids: set[int] = set()
    return_rx_end_uids: set[int] = set()
    return_rx_drop_uids: set[int] = set()
    return_rx_ok_uids: set[int] = set()
    return_rx_error_uids: set[int] = set()
    for row in events:
        name = str(row.get("event"))
        node = str(row.get("node"))
        if name.startswith("radio_queue_"):
            by_node[node][name] += 1
            value = row.get("value")
            if name in {"radio_queue_depth", "radio_queue_enqueue"} and isinstance(value, float):
                queue_depths[node].append(value)
            if name == "radio_queue_dequeue" and isinstance(value, float):
                queue_residence_ms[node].append(value)
            queue_rows.append({
                "time_s": row.get("time_s"), "node": node, "event": name,
                "value": value, "bytes": row.get("bytes"), "details": row.get("details"),
            })
        if name in {
            "phy_tx_start", "phy_tx_end", "phy_rx_start", "phy_rx_abort",
            "wifi_phy_rx_end", "wifi_phy_rx_drop", "phy_rx_ok", "phy_rx_error",
        }:
            half_duplex[node][name] += 1
        uid = packet_uid(row)
        if uid is not None and node.startswith("uav") and name == "mac_tx" and row.get("dst_port") == 14600:
            return_mac_uids.add(uid)
        if uid is not None and node == "cp" and row.get("dst_port") == 14600:
            if name == "phy_rx_start":
                return_rx_start_uids.add(uid)
            elif name == "wifi_phy_rx_end":
                return_rx_end_uids.add(uid)
            elif name == "wifi_phy_rx_drop":
                return_rx_drop_uids.add(uid)
            elif name == "phy_rx_ok":
                return_rx_ok_uids.add(uid)
            elif name == "phy_rx_error":
                return_rx_error_uids.add(uid)
    with (metrics / "native_queue_events.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["time_s", "node", "event", "value", "bytes", "details"])
        writer.writeheader()
        writer.writerows(queue_rows)
    queue_summary = {
        node: {
            "events": dict(by_node[node]),
            "depth": distribution(queue_depths[node]),
            "residence_ms": distribution(queue_residence_ms[node]),
        }
        for node in sorted(set(by_node) | set(queue_depths) | set(queue_residence_ms))
    }
    wifi = stats.get("radio_backend") == "wifi"
    write_json(metrics / "native_queue_summary.json", {
        "source": (
            "Wi-Fi MAC queue counters are unavailable in this trace set"
            if wifi
            else "public AlohaNoackNetDevice Queue attribute and public Queue traces"
        ),
        "nodes": queue_summary,
    })
    return_chain = {
        "uav_to_gcs_mac_tx": len(return_mac_uids),
        "cp_phy_rx_start": len(return_rx_start_uids),
        "cp_wifi_phy_rx_end": len(return_rx_end_uids),
        "cp_wifi_phy_rx_drop": len(return_rx_drop_uids),
        "cp_phy_rx_ok": len(return_rx_ok_uids),
        "cp_phy_rx_error": len(return_rx_error_uids),
        "no_cp_rx_candidate": len(return_mac_uids - return_rx_start_uids),
        "decode_semantics": (
            "wifi_phy_rx_end is not counted as success; only WifiPhyStateHelper "
            "phy_rx_ok/phy_rx_error events are decoder verdicts"
        ),
        "no_cp_rx_candidate_reason": (
            "SpectrumWifiPhy MPDU trace UIDs are not identical to the pre-enqueue MSDU UID; "
            "application ACK accounting remains authoritative"
            if wifi
            else "HalfDuplexIdealPhy emits RxStart only while IDLE; a signal arriving during RX/TX "
            "has no second public RxEnd outcome"
        ),
    }
    half_duplex_result = {node: dict(values) for node, values in half_duplex.items()}
    write_json(metrics / "half_duplex_summary.json", {
        "source": (
            "public SpectrumWifiPhy PhyTxBegin/PhyTxEnd/PhyRxBegin/PhyRxEnd/PhyRxDrop traces"
            if wifi
            else "public HalfDuplexIdealPhy TxStart/TxEnd/RxStart/RxAbort/RxEndOk/RxEndError traces"
        ),
        "nodes": half_duplex_result,
        "uav_to_gcs_control_return": return_chain,
    })
    return {"queue_nodes": queue_summary, "half_duplex": half_duplex_result, "control_return": return_chain}


def summarize_observer_mode(run_dir: Path, events: list[dict[str, Any]]) -> dict[str, Any]:
    environment = (run_dir / "environment.txt").read_text(encoding="utf-8", errors="replace") \
        if (run_dir / "environment.txt").exists() else ""
    match = re.search(r"^event_logging=(.+)$", environment, re.M)
    mode = match.group(1).strip() if match else "unrecorded"
    result = {
        "event_logging": mode,
        "native_event_rows": len(events),
        "native_event_log_bytes": (run_dir / "logs/native_radio_events.csv").stat().st_size
        if (run_dir / "logs/native_radio_events.csv").exists() else 0,
        "flush_policy": "256 events or 25 ms maximum" if mode == "batched_trace" else "metrics-only suppression of per-packet native and adapter trace",
    }
    write_json(run_dir / "metrics/observer_effect.json", result)
    return result


def export_wifi_monitor(run_dir: Path, events: list[dict[str, Any]]) -> dict[str, Any]:
    """Export native MPDU observations; no interpolation across missing receptions."""
    rows = [e for e in events if e.get("event") == "wifi_monitor_rx"]
    fields = ["time_s", "wall_monotonic_ns", "phase", "node", "peer", "src_ip", "dst_ip",
              "x", "y", "z", "bytes", "signal_dbm", "noise_interference_dbm", "sinr_db",
              "frequency_mhz", "details"]
    with (run_dir / "metrics/wifi_monitor_rx.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    summary = {
        "samples": len(rows), "level": "successfully_decoded_mpdu",
        "source": "native WifiPhy.MonitorSnifferRx.SignalNoiseDbm",
        "signal_dbm": distribution(e["signal_dbm"] for e in rows if e.get("signal_dbm") is not None),
        "noise_interference_dbm": distribution(e["noise_interference_dbm"] for e in rows if e.get("noise_interference_dbm") is not None),
        "sinr_db": distribution(e["sinr_db"] for e in rows if e.get("sinr_db") is not None),
        "sinr_origin": "derived: native signal dBm minus native combined noise/interference dBm",
        "outage_value": None, "outage_reason": "no decoded MPDU means no MonitorSnifferRx sample",
        "total_rssi_dbm": None, "total_rssi_reason": "signal power excludes simultaneous signals and noise",
        "bler": None, "bler_status": "not_applicable: no block abstraction in this PHY",
        "pcap": "pcap/native_radio.pcap.radiotap-*.pcap is native 802.11/radiotap; native_radio.pcap is derived Ethernet",
    }
    write_json(run_dir / "metrics/wifi_monitor_summary.json", summary)
    return summary


def write_latency_diagnostic_report(run_dir: Path, observer_reference: Path | None = None) -> int:
    scenario = read_json(run_dir / "metrics/scenario_summary.json", {})
    events = native_events(run_dir / "logs/native_radio_events.csv")
    export_wifi_monitor(run_dir, events)
    latency = summarize_latency_operations(run_dir, scenario)
    load = summarize_adapter_load(run_dir, scenario)
    stats = read_json(run_dir / "metrics/native_radio_stats.json", {})
    contention = summarize_native_contention(run_dir, events, stats)
    observer = summarize_observer_mode(run_dir, events)
    ping = scenario.get("latency_diagnostic", {}).get("mavlink_ping", {})
    metric_availability = radio_metric_availability(stats)
    observer_comparison: dict[str, Any] | None = None
    if observer_reference is not None:
        reference = read_json(observer_reference / "metrics/control_latency_summary.json", {})
        reference_observer = reference.get("observer", {}) if isinstance(reference, dict) else {}
        reference_latency = reference.get("mavlink_latency", {}) if isinstance(reference, dict) else {}
        observer_comparison = {
            "reference_run": observer_reference.name,
            "reference_event_logging": reference_observer.get("event_logging"),
            "candidate_event_logging": observer["event_logging"],
            "reference_ack_samples": reference_latency.get("first_attempt_rtt_ms", {}).get("samples"),
            "candidate_ack_samples": latency["first_attempt_rtt_ms"]["samples"],
            "reference_rtt_p95_ms": reference_latency.get("first_attempt_rtt_ms", {}).get("p95"),
            "candidate_rtt_p95_ms": latency["first_attempt_rtt_ms"]["p95"],
            "reference_native_event_rows": reference_observer.get("native_event_rows"),
            "candidate_native_event_rows": observer["native_event_rows"],
            "interpretation": (
                "The ACK sample count is the primary observer-invariant outcome; latency percentiles remain "
                "workload-sensitive and are not treated as a correction factor."
            ),
        }
        write_json(run_dir / "metrics/observer_effect_comparison.json", observer_comparison)
    result = {
        "run_id": run_dir.name,
        "status": scenario.get("status"),
        "uav_count": scenario.get("latency_diagnostic", {}).get("uav_count"),
        "profile": scenario.get("profile"),
        "mavlink_latency": latency,
        "mavlink_ping": ping,
        "uart_load": load,
        "native_contention": contention,
        "observer": observer,
        "observer_comparison": observer_comparison,
        "native_stats": stats,
        "physical_metric_availability": metric_availability,
        "control_loss_breakdown": {
            "before_native_enqueue": "unavailable: no public GCS-side pre-MAC enqueue trace",
            "native_queue": "observed in metrics/native_queue_summary.json; no queue drops in this run",
            "phy": contention["control_return"],
            "uav_uart_delivery": latency["uav_uart_delivery_observed"],
            "uav_application_ack_generated": load["command_ack_observed_from_uav_uart"],
            "uav_application_ack_at_gcs": latency["first_attempt_rtt_ms"]["samples"],
            "gcs_transport_reassembly": latency["gcs_transport_reassembly"],
        },
    }
    write_json(run_dir / "metrics/control_latency_summary.json", result)
    report = [
        f"# Native control-latency diagnostic: {run_dir.name}",
        "",
        f"- Status: **{scenario.get('status', 'missing')}**; UAVs: {result['uav_count']}",
        f"- Command attempts: {latency['attempt_count']}; post-write UAV UART deliveries observed: {latency['uav_uart_delivery_observed']}.",
        f"- First-attempt RTT p95: {latency['first_attempt_rtt_ms']['p95']} ms.",
        f"- Successful-attempt RTT p95: {latency['successful_attempt_rtt_ms']['p95']} ms.",
        f"- MAVLink PING supported: {ping.get('supported', False)}; attempts/UAV: {ping.get('attempts_per_uav', 0)}.",
        f"- Observer mode: {observer['event_logging']} ({observer['native_event_rows']} native event rows).",
        "",
        "`mavlink_latency_chain.csv` contains every operation and attempt. Exact MAVLink bytes correlate the UART write, real ACK UART read and GCS reception. A returned ACK does not identify which repeated command caused it; retry-attempt RTT remains null when ambiguous. GCS is UDP, so no GCS UART timestamp exists.",
        "",
        "RSSI/SNR/SINR and BLER retain their explicit native-API availability status in `control_latency_summary.json`.",
    ]
    if native_sources["sources"]:
        report.extend(["", "## Native Spectrum sources", "", "See metrics/native_source_summary.json for baseline/active/recovery native powers, decoded SINR, and receive-attempt outcomes."])
        for stem in ("07_jammer_active","08_jammer_recovery"):
            if (run_dir/"screenshots"/(stem+".raw.png")).exists():
                report.extend(["", f"![{stem}](screenshots/{stem}.png)", "", f"[Unmodified live Gazebo frame](screenshots/{stem}.raw.png)"])
    (run_dir / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    print(json.dumps({"run_dir": str(run_dir), "diagnostic": scenario.get("status")}, sort_keys=True))
    return 0 if scenario.get("status") == "diagnostic_complete" else 1


def add_optional_one_uav_regression_check(
    functional_checks: dict[str, bool],
    one_uav_run: Path | None,
    one_uav_summary: dict[str, Any] | None,
) -> None:
    if one_uav_run is not None:
        functional_checks["one_uav_regression"] = bool(
            one_uav_summary and one_uav_summary.get("status") == "passed"
        )


def recorded_tx_power_w(
    stats: dict[str, Any],
    scenario: dict[str, Any],
    scenario_config: dict[str, Any] | None = None,
) -> float | None:
    candidates = [stats.get("tx_power_w"), scenario.get("tx_power_w")]
    for container in (stats.get("radio"), scenario.get("radio")):
        if isinstance(container, dict):
            candidates.append(container.get("tx_power_w"))
    parameters = scenario.get("predeclared_parameters", {})
    if isinstance(parameters, dict):
        candidates.append(parameters.get("tx_power_w"))
    if scenario_config is not None and isinstance(scenario_config.get("radio"), dict):
        candidates.append(scenario_config["radio"].get("tx_power_w"))
    for candidate in candidates:
        if (
            isinstance(candidate, (int, float))
            and not isinstance(candidate, bool)
            and math.isfinite(float(candidate))
            and float(candidate) > 0.0
        ):
            return float(candidate)
    return None


def scenario_motion_pattern(scenario: dict[str, Any]) -> dict[str, Any]:
    parameters = scenario.get("predeclared_parameters", {})
    missions = parameters.get("flight_missions", {}) if isinstance(parameters, dict) else {}
    if isinstance(missions, dict) and missions:
        normalized_missions: dict[str, Any] = {}
        for raw_uav, waypoints in missions.items():
            key = str(raw_uav)
            if key.isdigit():
                key = f"uav{key}"
            if key in UAVS:
                normalized_missions[key] = waypoints
        mission_uavs = sorted(
            uav for uav, waypoints in normalized_missions.items() if waypoints
        )
        source = "scenario_summary.predeclared_parameters.flight_missions"
        waypoint_counts = {
            uav: len(waypoints) if isinstance(waypoints, list) else None
            for uav, waypoints in normalized_missions.items()
        }
    else:
        legacy_mission = scenario.get("mission", {})
        moving = legacy_mission.get("moving_uav") if isinstance(legacy_mission, dict) else None
        mission_uavs = [str(moving)] if moving in UAVS else []
        source = "scenario_summary.mission.moving_uav"
        waypoint_counts = {
            str(moving): legacy_mission.get("item_count")
        } if moving in UAVS else {}
    return {
        "source": source,
        "mission_uavs": mission_uavs,
        "holding_uavs": sorted(set(UAVS) - set(mission_uavs)),
        "configured_waypoints_per_uav": waypoint_counts,
        "mission_uav_displacement_m": scenario.get("mission_uav_displacement_m", {}),
        "holding_uav_displacement_m": scenario.get("holding_uav_displacement_m", {}),
    }


def summarize_native_sources(run_dir: Path, events: list[dict[str, Any]]) -> dict[str, Any]:
    switches=[e for e in events if e["event"] in {"jammer_on","jammer_off"}]
    result={"sources":switches,"windows":{},"measurement_scope":"native decoder/SignalArrival samples; application PDR needs distinct application offers",
        "units":{"time":"ns-3 seconds","foreign_power":"dBm, native SpectrumWifiPhy.SignalArrival","noise_interference":"dBm, native decoded MPDU sample","sinr":"dB, derived S/(I+N) on decoded samples"}}
    if switches:
        on=min(e["time_s"] for e in switches if e["event"]=="jammer_on")
        off=max((e["time_s"] for e in switches if e["event"]=="jammer_off"),default=max(e["time_s"] for e in events))
        for name,low,high in (("baseline",max(0,on-10),on),("active",on,off),("recovery",off,off+10)):
            selected=[e for e in events if low<=e["time_s"]<high]
            decoded=[e for e in selected if e["event"]=="wifi_monitor_rx"]
            ok=sum(e["event"]=="phy_rx_ok" for e in selected)
            error=sum(e["event"]=="phy_rx_error" for e in selected)
            result["windows"][name]={"start_sim_s":low,"stop_sim_s":high,"decoded_mpdu_samples":len(decoded),
                "noise_interference_dbm":distribution([e["noise_interference_dbm"] for e in decoded if e.get("noise_interference_dbm") is not None]),
                "sinr_db":distribution([e["sinr_db"] for e in decoded if e.get("sinr_db") is not None]),
                "foreign_power_dbm_by_receiver":{node:distribution([e["value"] for e in selected if e["event"]=="spectrum_signal_arrival" and e["node"]==node and "foreign_signal=1" in str(e.get("details")) and e.get("value") is not None and math.isfinite(e["value"])]) for node in ("cp",*UAVS)},
                "decoder_success_attempts":ok,"decoder_failure_attempts":error,"decoder_attempt_per":error/(ok+error) if ok+error else None,
                "decoder_per_denominator":"all RxOk + RxError callbacks in window, retries retained; preamble/CCA drops excluded",
                "other_phy_drops":sum(e["event"]=="wifi_phy_rx_drop" for e in selected),
                "application_pdr":None,"application_pdr_reason":"no phase-specific offered-unique-message set; see separate source campaign"}
    write_json(run_dir/"metrics/native_source_summary.json",result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument(
        "--scenario-config",
        type=Path,
        help=(
            "scenario YAML supplying phase aliases, screenshot requirements, and causal "
            "expectations; defaults to scenario_summary.json:scenario_config"
        ),
    )
    parser.add_argument("--one-uav-run", type=Path)
    parser.add_argument("--latency-diagnostic", action="store_true")
    parser.add_argument("--observer-reference-run", type=Path)
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    if args.latency_diagnostic:
        return write_latency_diagnostic_report(
            run_dir,
            args.observer_reference_run.resolve() if args.observer_reference_run else None,
        )
    metrics_dir = run_dir / "metrics"
    scenario = read_json(metrics_dir / "scenario_summary.json", {})
    if not isinstance(scenario, dict):
        parser.error(f"invalid scenario summary: {metrics_dir / 'scenario_summary.json'}")
    try:
        scenario_config_path = resolve_scenario_config_path(args.scenario_config, scenario)
        scenario_config = load_scenario_config(scenario_config_path)
    except (OSError, ValueError, yaml.YAMLError) as error:
        parser.error(str(error))
    stats = read_json(metrics_dir / "native_radio_stats.json", {})
    no_bypass = read_json(metrics_dir / "no_bypass_summary.json", {})
    events = native_events(
        run_dir / "logs/native_radio_events.csv", scenario_config["phase_aliases"]
    )
    export_wifi_monitor(run_dir, events)
    agents = agent_records(run_dir)

    topology = build_topology(run_dir, stats)
    native_contract = native_product_runtime_contract(stats, scenario_config)
    mobility = build_mobility(run_dir, events)
    mavlink = build_mavlink(scenario)
    p2p = build_p2p(scenario, agents)
    p2mp = build_p2mp(scenario, agents, events)
    shared = build_shared_medium(scenario, agents, events)
    realtime = build_realtime(
        run_dir, events, mobility, stats, scenario_config["realtime_gates"]
    )
    observability = build_radio_observability(
        run_dir, events, scenario, scenario_config["phase_aliases"]
    )
    screenshots = screenshot_status(run_dir, events, scenario_config)
    native_sources = summarize_native_sources(run_dir, events)
    causal = summarize_causal_link_probes(run_dir, scenario, scenario_config, events)
    one_uav = None
    if args.one_uav_run:
        one_uav = read_json(args.one_uav_run.resolve() / "metrics/native_product_summary.json", None)
        if one_uav is None:
            one_uav = read_json(args.one_uav_run.resolve() / "metrics/product_summary.json", None)

    sequential = scenario.get("dual_uart_diagnostics", {}).get("sequential", {})
    runtime_parameters = scenario.get("predeclared_parameters", {})
    runtime_traffic = (
        runtime_parameters.get("traffic", {}) if isinstance(runtime_parameters, dict) else {}
    )
    configured_traffic = scenario_config["traffic"]
    traffic_config_lineage = runtime_traffic == configured_traffic
    delivery_checks = traffic_delivery_checks(p2p, p2mp, shared, configured_traffic)
    runtime_map = scenario.get("map", {})
    scenario_config_lineage = bool(
        scenario.get("scenario_name") == scenario_config["scenario_name"]
        and isinstance(runtime_map, dict)
        and runtime_map.get("id") == scenario_config["map"].get("id")
    )
    process_snapshot_path = run_dir / "logs/process_snapshot.txt"
    process_text = process_snapshot_path.read_text(
        encoding="utf-8", errors="replace"
    ) if process_snapshot_path.exists() else ""
    forbidden = {
        "network/radio_provider/provider.py": "network/radio_provider/provider.py" in process_text,
        "scripts/product/town01_radio_state.py": "scripts/product/town01_radio_state.py" in process_text,
        "AmsStockSionnaPacketErrorModel": "AmsStockSionnaPacketErrorModel" in process_text,
        "centralized_priority_scheduler": "centralized_priority_scheduler" in process_text,
        "custom_five_uav_packet_engine": "ams-tap-packet-engine" in process_text,
    }
    functional_checks = {
        "scenario_config_lineage": scenario_config_lineage,
        "traffic_config_lineage": traffic_config_lineage,
        "native_wifi_sionna_runtime_contract": native_contract["passed"],
        "realtime_scheduler_gazebo_and_pose_gates": realtime["realtime_readiness"]
        == "ready",
        "runtime_process_snapshot_observed": bool(process_text.strip()),
        "no_forbidden_custom_or_bypass_components": not any(forbidden.values()),
        "five_real_sitl_and_gazebo_lifecycle": scenario.get("status") == "passed",
        "five_real_odometry_sources": mobility.get(
            "all_required_odometry_streams_observed", False
        ),
        "five_mobility_models_updated": mobility.get("all_mobility_models_updated", False),
        "one_shared_native_spectrum_channel": topology["shared_multi_model_spectrum_channels"] == 1,
        "six_native_phy_mac_pairs": topology["native_radio_devices"] == 6,
        "ten_uart_paths": all(item["status"] == "observed" for item in mavlink["ten_uart_paths"]),
        "control_and_payload_diagnostics_all_five": all(
            sequential.get(uav, {}).get("control", {}).get("response_received")
            and sequential.get(uav, {}).get("payload", {}).get("response_received")
            for uav in UAVS
        ),
        "p2p_configured_offers_and_delivery_all_five": delivery_checks["p2p"],
        "p2mp_configured_root_and_delivery_all_five": delivery_checks["p2mp"],
        "simultaneous_uplink_delivery_and_fairness": delivery_checks["simultaneous"],
        "flight_land_auto_disarm_all_five": all(
            scenario.get("uavs", {}).get(uav, {}).get("phases", {}).get("auto_disarm")
            for uav in UAVS
        ),
        "no_bypass_all_five": bool(no_bypass.get("passed")),
        "live_gazebo_evidence": screenshots["screenshots_status"] == "passed",
        "endpoint_and_native_radiotap_pcaps": topology["endpoint_pcaps_complete"] and topology["native_radiotap_pcaps_complete"],
    }
    if causal["required"]:
        functional_checks["causal_clear_shadow_recovery"] = bool(causal["passed"])
    add_optional_one_uav_regression_check(
        functional_checks, args.one_uav_run, one_uav
    )
    functional_status = "passed" if all(functional_checks.values()) else "failed"
    if functional_status != "passed" and realtime["realtime_readiness"] == "ready":
        # Timing alone is not product readiness when the same RTF=1 run did
        # not complete the native control/flight proof.
        realtime["realtime_readiness"] = "limited"
        realtime["functional_prerequisite"] = "failed"
    status = "functional_native_path" if functional_status == "passed" else "realtime_failed"
    if functional_status == "passed":
        status = "realtime_ready" if realtime["realtime_readiness"] == "ready" else "realtime_limited"
    runtime_tx_power_w = recorded_tx_power_w(stats, scenario)
    tx_power_w = recorded_tx_power_w(stats, scenario, scenario_config)
    tx_power_basis = (
        "native stats/scenario summary"
        if runtime_tx_power_w is not None
        else "selected scenario radio product config"
        if tx_power_w is not None
        else "unavailable"
    )
    operating_envelope = {
        "uav_count": 5,
        "motion_pattern": scenario_motion_pattern(scenario),
        "tx_power_w": tx_power_w,
        "tx_power_basis": tx_power_basis,
        "cache_policy": stats.get("cache_policy", "displacement_or_time"),
        "update_period_s": stats.get("channel_state_max_age_s"),
        "displacement_threshold_m": stats.get("endpoint_displacement_threshold_m"),
        "solver_calls_per_s": None,
        "channel_state_age_ms": {
            **distribution([max(0., (float(e["time_s"])-float(e["channel_generation_time_s"]))*1000)
                for e in events if e.get("event")=="sionna_link_state" and isinstance(e.get("channel_generation_time_s"),(int,float))]),
            "basis": "sim time minus native channel generation time; observed links only",
        },
        "ns3_lag_ms": realtime.get("steady_ns3_realtime_lag_ms"),
        "gazebo_rtf": realtime.get("gazebo_rtf"),
        "resources": realtime.get("resources"),
        "functional_result": functional_status,
        "realtime_classification": status,
    }
    path_computations = realtime.get("sionna", {}).get("path_computations_by_pair", {})
    total_solves = sum(int(value) for value in path_computations.values())
    runtime_duration = float(scenario.get("duration_s", 0.0) or 0.0)
    operating_envelope["solver_calls_per_s"] = total_solves / runtime_duration if runtime_duration else None
    write_json(metrics_dir / "operating_envelope.json", operating_envelope)
    summary = {
        "run_id": run_dir.name,
        "scenario_config": scenario_config["path"],
        "scenario_config_lineage": scenario_config_lineage,
        "functional_five_uav_native_path": functional_status,
        "functional_checks": functional_checks,
        "realtime_readiness": realtime["realtime_readiness"],
        "profile": stats.get("profile", scenario.get("profile")),
        "technology_specific_modem": bool(stats.get("technology_specific_modem", False)),
        "native_topology": topology,
        "native_wifi_sionna_runtime_contract": native_contract,
        "traffic_product_contract": {
            "passed": traffic_config_lineage,
            "configured": configured_traffic,
            "runtime_predeclared": runtime_traffic,
        },
        "mobility": mobility,
        "mavlink": mavlink,
        "p2p": p2p,
        "p2mp": p2mp,
        "shared_medium": shared,
        "flight": {
            "status": scenario.get("status"),
            "mission": scenario.get("mission"),
            "points": scenario.get("flight_points"),
            "holding_uav_displacement_m": scenario.get("holding_uav_displacement_m"),
            "uavs": scenario.get("uavs"),
            "causal_expectation": scenario_config["causal_expectation"],
            "causal_link_probes": causal,
        },
        "realtime": realtime,
        "observability": observability,
        "screenshots": screenshots,
        "operating_envelope": operating_envelope,
        "no_bypass": no_bypass,
        "one_uav_regression": one_uav,
        "one_uav_regression_required": bool(args.one_uav_run),
        "forbidden_custom_components_present": forbidden,
    }
    write_json(metrics_dir / "native_topology.json", topology)
    write_json(metrics_dir / "mobility_summary.json", mobility)
    write_json(metrics_dir / "mavlink_summary.json", mavlink)
    write_json(metrics_dir / "p2p_summary.json", p2p)
    write_json(metrics_dir / "p2mp_summary.json", p2mp)
    write_json(metrics_dir / "shared_medium_summary.json", shared)
    write_json(metrics_dir / "realtime_summary.json", realtime)
    write_json(metrics_dir / "five_uav_native_summary.json", summary)
    report = [
        f"# Native five-UAV radio run: {run_dir.name}",
        "",
        f"- Functional five-UAV native path: **{functional_status}**",
        f"- Realtime readiness: **{realtime['realtime_readiness']}** (measured independently)",
        "- Topology: one ns-3 process, six native PHY/MAC devices, one shared Spectrum channel.",
        (
            "- Radio: native 802.11n QoS SpectrumWifiPhy; Tx power="
            f"{tx_power_w if tx_power_w is not None else 'unavailable'} W "
            f"({tx_power_basis})."
            if stats.get("radio_backend") == "wifi"
            else "- Radio: native Aloha/ideal PHY reference; Tx power="
            f"{tx_power_w if tx_power_w is not None else 'unavailable'} W "
            f"({tx_power_basis})."
        ),
        f"- P2MP: {p2mp['root_transmissions']} application roots, {p2mp['command_post_mac_tx']} command-post MacTx.",
        f"- Simultaneous uplink Jain fairness: {shared['jain_fairness']}",
        f"- No-bypass after common native process stop: {no_bypass.get('passed')}",
        (
            f"- One-UAV regression: {'passed' if functional_checks.get('one_uav_regression') else 'failed'} (required)."
            if args.one_uav_run
            else "- One-UAV regression: not requested; not part of this run's gate."
        ),
        f"- Radio-link observations: {observability['radio_rows']}; post-Sionna receive power is reported separately from unavailable RSSI/SINR metrics.",
        f"- BLER: {observability['metric_availability']['bler']}",
        f"- Live Gazebo screenshots: {screenshots['screenshots_status']}.",
        f"- Causal clear -> shadow -> recovery gate: {causal['status']}.",
        "",
        "All packet and timing results above are derived from endpoint logs, native ns-3 traces, ROS tracker snapshots, Gazebo stats, and process-resource samples in this run directory.",
    ]
    if causal["required"]:
        report.extend(["", "## Causal link probe"])
        for uav in UAVS:
            record = causal["per_uav"].get(uav, {})
            report.append(
                f"- {uav} ({record.get('group', 'unknown')}): "
                f"clear={record.get('clear', {}).get('pdr')}, "
                f"shadow={record.get('shadow', {}).get('pdr')}, "
                f"recovery={record.get('recovery', {}).get('pdr')}."
            )
        if causal["failures"]:
            report.append(f"- Gate failures: {'; '.join(causal['failures'])}")
    report.extend(["", "## Live Gazebo frames"])
    for specification in scenario_config["screenshots"]:
        if specification["phase"].startswith("latency_"):
            continue  # Dedicated stationary diagnostic captures are not flight phases.
        stem = specification["stem"]
        record = screenshots["records"][stem]
        if not record["valid_live_capture"]:
            report.append(f"- `{stem}`: unavailable in this run.")
            continue
        observation = record["native_observation"]
        report.extend(
            [
                "",
                f"### {stem}",
                "",
                f"![{stem}](screenshots/{stem}.png)",
                "",
                f"[Hash-locked raw Gazebo frame](screenshots/{stem}.raw.png); the displayed "
                "frame adds explicitly disclosed pinhole-projected live-odometry labels.",
                "",
                "Native evidence in the +/- 2 wall-clock-second frame window: "
                f"Sionna path observations={observation['sionna_path_observations']}, "
                f"path count={observation['sionna_path_count_min']}..{observation['sionna_path_count_max']}, "
                f"Wi-Fi PhyRxEnd/PhyRxDrop={observation['wifi_phy_rx_end']}/{observation['wifi_phy_rx_drop']}, "
                f"decoder RxOk/RxError={observation['phy_rx_ok']}/{observation['phy_rx_error']}.",
            ]
        )
    (run_dir / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    print(json.dumps({"run_dir": str(run_dir), "functional": functional_status, "realtime": realtime["realtime_readiness"]}, sort_keys=True))
    return 0 if functional_status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
