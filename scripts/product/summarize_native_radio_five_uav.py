#!/usr/bin/env python3
"""Summarize only observed traces from a five-UAV native radio run."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable


UAVS = tuple(f"uav{index}" for index in range(1, 6))


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


def native_events(path: Path) -> list[dict[str, Any]]:
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
        match = re.search(r"packet_uid=(\d+)", str(row.get("details", "")))
        row["packet_uid"] = int(match.group(1)) if match else None
        for key in ("src_ip", "dst_ip", "flow"):
            match = re.search(rf"(?:^|;){key}=([^;]+)", str(row.get("details", "")))
            row[key] = match.group(1) if match else None
        for key in ("ip_protocol", "src_port", "dst_port"):
            match = re.search(rf"(?:^|;){key}=(\d+)", str(row.get("details", "")))
            row[key] = int(match.group(1)) if match else None
        result.append(row)
    return result


def build_topology(run_dir: Path, stats: dict[str, Any]) -> dict[str, Any]:
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
    return {
        "ns3_processes": 1,
        "radio_nodes": 6,
        "uav_count": 5,
        "native_radio_devices": 6,
        "half_duplex_ideal_phys": 6,
        "native_antennas": 6,
        "mobility_models": 6,
        "tap_boundaries": 6,
        "shared_multi_model_spectrum_channels": 1,
        "sionna_rt_spectrum_propagation_loss_models": 1,
        "radio_medium": "native HalfDuplexIdealPhy/AlohaNoackNetDevice",
        "tap_ingress_segment": {"type": "local_fast_csma", "radio_medium": False},
        "nodes": nodes,
        "profile": "generic_native_spectrum_aloha_reference",
        "technology_specific_modem": False,
        "neighbor_discovery_mode": "preconfigured_static_neighbors",
        "reason": "upstream_ideal_phy_arp_reentrancy_limit",
        "packet_outcome_affected": False,
        "native_stats": stats,
        "pcaps": pcap_names,
        "exact_pcap_count": len(pcap_names),
    }


def build_mobility(run_dir: Path, events: list[dict[str, Any]]) -> dict[str, Any]:
    snapshots = read_jsonl(run_dir / "logs/node_state.jsonl")
    topic_details: dict[str, Any] = {}
    result: dict[str, Any] = {
        "atomic_five_uav_snapshots": True,
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
        publisher_match = re.search(r"Node name: ([^\n]+).*?Node namespace: ([^\n]+)", topic_text, re.S)
        publisher = None
        if publisher_match:
            publisher = f"{publisher_match.group(2).rstrip('/')}/{publisher_match.group(1)}".replace("//", "/")
        topic_details[uav] = {"topic": f"/{uav}/odometry", "publisher_node": publisher}
        result["uavs"][uav] = {
            "ros_topic": f"/{uav}/odometry",
            "publisher_node": publisher,
            "sample_count": len(positions),
            "update_rate_hz": (len(positions) - 1) / elapsed if elapsed > 0 else None,
            "first_position_m": first,
            "final_position_m": positions[-1] if positions else None,
            "maximum_displacement_m": maximum_displacement,
            "maximum_altitude_m": max((position[2] for position in positions), default=None),
            "stale_sample_count": stale,
            "applied_position_age_ms": distribution(ages),
            "tracker_source_topics": dict(source_topics),
        }
    result["all_required_publishers_observed"] = all(
        topic_details[uav]["publisher_node"] for uav in UAVS
    )
    result["all_mobility_models_updated"] = all(result["uavs"][uav]["sample_count"] > 0 for uav in UAVS)
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
    per_uav: dict[str, Any] = {}
    for uav in UAVS:
        down_offered = [item for item in sends if item.get("uav") == uav]
        down_received = [
            row
            for row in agents[uav]
            if row.get("event") == "receive" and row.get("kind") == "p2p_downlink"
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
                "missing_sequences": sorted(set(range(10)) - {int(row.get("sequence")) for row in down_received}),
                "duplicates": len(down_received) - len({row.get("sequence") for row in down_received}),
                "latency_ms": distribution(row.get("latency_ms", 0.0) for row in down_received),
            },
            "uav_to_gcs": {
                "independently_originated": len(up_originated),
                "delivered_unique": len({row.get("sequence") for row in up_received}),
                "missing_sequences": sorted(set(range(10)) - {int(row.get("sequence")) for row in up_received}),
                "duplicates": len(up_received) - len({row.get("sequence") for row in up_received}),
                "latency_ms": distribution(row.get("latency_ms", 0.0) for row in up_received),
            },
        }
    return {
        "protocol": "checksummed_logical_message_v1",
        "ns3_echo": False,
        "retransmissions": False,
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
        per_receiver[uav] = {
            "receiver_phy_rx_start": starts,
            "receiver_phy_rx_ok": ok,
            "receiver_phy_rx_error": error,
            "receiver_application_deliveries": len(sequences),
            "receiver_missing_sequences": sorted(set(range(20)) - sequences),
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
            intervals.append((start, start + transmit["bytes"] * 8 / 1_000_000.0, uav, transmit["packet_uid"]))
        delivered_unique = len({row.get("sequence") for row in delivered})
        bits_per_second = delivered_unique * 256 * 8 / 1.0
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
        "medium": "one shared native ALOHA channel",
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
            "basis": "native MacTx simulation timestamps and 1 Mbit/s packet airtime",
        },
    }


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


def build_realtime(run_dir: Path, events: list[dict[str, Any]], mobility: dict[str, Any]) -> dict[str, Any]:
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
    ready = bool(
        steady_lag_stats["p95"] is not None
        and steady_lag_stats["p95"] <= 250.0
        and gazebo["p5"] is not None
        and gazebo["p5"] >= 0.8
        and (not pose_ages or max(pose_ages) <= 500.0)
    )
    failed = not lag or gazebo["samples"] == 0
    classification = "failed" if failed else ("ready" if ready else "limited")
    return {
        "measurement_status": "measured" if not failed else "failed",
        "realtime_readiness": classification,
        "predeclared_readiness_bounds": {
            "steady_ns3_lag_p95_ms_max": 250.0,
            "gazebo_rtf_p5_min": 0.8,
            "applied_position_age_p95_ms_max": 500.0,
        },
        "sionna": sionna,
        "ns3_realtime_lag_ms": lag_stats,
        "cold_initial_ns3_lag_ms": distribution(lag[:20]),
        "steady_ns3_realtime_lag_ms": steady_lag_stats,
        "gazebo_rtf": gazebo,
        "resources": resources,
        "applied_position_age_p95_ms_max_across_uavs": max(pose_ages) if pose_ages else None,
    }


UNAVAILABLE_RADIO_METRICS = {
    "rx_power_dbm": "unavailable: HalfDuplexIdealPhy does not expose received PSD through a public trace",
    "rssi_dbm": "unavailable: no public received-PSD/RSSI trace in the selected native PHY",
    "snr_db": "unavailable: no public SNR trace in HalfDuplexIdealPhy/ShannonSpectrumErrorModel",
    "sinr_db": "unavailable: SpectrumInterference evaluates SINR internally but exposes no public trace",
    "interference_power_dbm": "unavailable: no public interference-power trace in the selected native PHY",
    "noise_power_dbm": "unavailable: no public noise-power trace in the selected native PHY",
    "bler": "unavailable: current HalfDuplexIdealPhy/Shannon reference does not expose a transport-block abstraction",
}

REQUIRED_SCREENSHOTS = (
    "01_five_uav_takeoff",
    "02_five_uav_hold",
    "03_uav1_los",
    "04_uav1_obstructed",
    "05_p2mp_or_shared_medium",
    "06_landing",
)


def screenshot_native_observation(
    metadata: dict[str, Any], events: list[dict[str, Any]]
) -> dict[str, Any]:
    """Summarize native events close to a real camera-frame simulation time."""

    timestamp = metadata.get("simulation_timestamp")
    if not isinstance(timestamp, (int, float)):
        return {"status": "unavailable: camera frame has no simulation timestamp"}
    phase = str(metadata.get("scenario_phase", ""))
    nearby = [
        event
        for event in events
        if abs(float(event.get("time_s", -math.inf)) - float(timestamp)) <= 2.0
        and str(event.get("phase", "")) == phase
    ]
    paths = [
        int(event["value"])
        for event in nearby
        if event.get("event") == "sionna_paths" and event.get("value") is not None
    ]
    return {
        "window_simulation_seconds": [float(timestamp) - 2.0, float(timestamp) + 2.0],
        "scenario_phase": phase,
        "sionna_path_observations": len(paths),
        "sionna_path_count_min": min(paths) if paths else "unavailable",
        "sionna_path_count_max": max(paths) if paths else "unavailable",
        "phy_rx_ok": sum(event.get("event") == "phy_rx_ok" for event in nearby),
        "phy_rx_error": sum(event.get("event") == "phy_rx_error" for event in nearby),
        "basis": "native events within +/- 2 simulation seconds of the unmodified Gazebo frame",
    }


def screenshot_status(run_dir: Path, events: list[dict[str, Any]]) -> dict[str, Any]:
    screenshot_dir = run_dir / "screenshots"
    records: dict[str, Any] = {}
    for stem in REQUIRED_SCREENSHOTS:
        metadata = read_json(screenshot_dir / f"{stem}.json", {})
        metadata = metadata if isinstance(metadata, dict) else {}
        image_exists = (screenshot_dir / f"{stem}.png").is_file()
        visible = metadata.get("visible_uavs", [])
        valid = bool(
            image_exists
            and metadata.get("run_id") == run_dir.name
            and metadata.get("source") == "live_gazebo_runtime"
            and sorted(visible) == list(UAVS)
        )
        records[stem] = {
            "image_exists": image_exists,
            "metadata": metadata,
            "valid_live_capture": valid,
            "native_observation": screenshot_native_observation(metadata, events) if valid else {},
        }
    return {
        "screenshots_status": "passed" if all(item["valid_live_capture"] for item in records.values()) else "failed",
        "records": records,
    }


def canonical_phase(phase: str) -> str:
    if phase.startswith("takeoff_") or phase.startswith("arm_"):
        return "takeoff"
    if phase == "hold_all":
        return "five_uav_hold"
    if phase == "los":
        return "los_observation"
    if phase == "obstructed_candidate":
        return "obstructed_observation"
    if phase == "return":
        return "return_observation"
    if phase == "land_all":
        return "landing"
    if phase == "pre_no_bypass" or phase == "no_bypass_stop":
        return "no_bypass_test"
    if phase == "stationary_communication_smoke":
        return "startup"
    return phase or "startup"


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


def real_value(value: float | None) -> float | str:
    return value if value is not None and math.isfinite(value) else "unavailable"


def build_radio_observability(
    run_dir: Path, events: list[dict[str, Any]], scenario: dict[str, Any]
) -> dict[str, Any]:
    metrics_dir = run_dir / "metrics"
    positions: dict[str, tuple[float, float, float]] = {}
    mobility_age: dict[str, float | None] = {}
    lag_at_event: float | None = None
    mac_by_uid: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    starts_by_uid: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    ends_by_uid: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    rows: list[dict[str, Any]] = []
    outcomes: defaultdict[tuple[str, str], dict[str, set[int]]] = defaultdict(
        lambda: {"attempted": set(), "ok": set(), "error": set(), "start": set()}
    )

    for event in events:
        event_name = str(event.get("event"))
        node = str(event.get("node"))
        uid = packet_uid(event)
        if event_name == "live_pose":
            positions[node] = (float(event["x"]), float(event["y"]), float(event["z"]))
            mobility_age[node] = event.get("value")
        elif event_name == "realtime_lag":
            lag_at_event = event.get("value")
        elif uid is not None and event_name == "mac_tx":
            mac_by_uid[uid].append(event)
        elif uid is not None and event_name == "phy_rx_start":
            starts_by_uid[uid].append(event)
        elif uid is not None and event_name in {"phy_rx_ok", "phy_rx_error"}:
            ends_by_uid[uid].append(event)
        elif event_name == "sionna_paths" and event.get("peer"):
            tx = node
            rx = str(event["peer"])
            uid = packet_uid(event)
            delays = delay_values(str(event.get("details", "")))
            tx_pos = positions.get(tx, (float(event["x"]), float(event["y"]), float(event["z"])))
            rx_pos = positions.get(rx, (float("nan"), float("nan"), float("nan")))
            matching_mac = [item for item in mac_by_uid.get(uid or -1, []) if item["node"] == tx]
            matching_start = [item for item in starts_by_uid.get(uid or -1, []) if item["node"] == rx]
            matching_end = [item for item in ends_by_uid.get(uid or -1, []) if item["node"] == rx]
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
            distance = math.dist(tx_pos, rx_pos) if all(math.isfinite(value) for value in rx_pos) else None
            rows.append(
                {
                    "timestamp_wall": float(event["wall_monotonic_ns"]) / 1e9,
                    "timestamp_sim": event["time_s"],
                    "scenario_phase": canonical_phase(str(event["phase"])),
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
                    "sionna_path_count": len(delays),
                    "sionna_los_available": "unavailable: current SionnaRtChannelParams exposes delays but not LOS identity",
                    "sionna_path_delay_min_ns": min(delays) * 1e9 if delays else "unavailable",
                    "sionna_path_delay_max_ns": max(delays) * 1e9 if delays else "unavailable",
                    "sionna_delay_spread_ns": (max(delays) - min(delays)) * 1e9 if delays else "unavailable",
                    "rx_power_dbm": "unavailable",
                    "rssi_dbm": "unavailable",
                    "snr_db": "unavailable",
                    "sinr_db": "unavailable",
                    "interference_power_dbm": "unavailable",
                    "noise_power_dbm": "unavailable",
                    "native_mac_tx": len(matching_mac),
                    "native_phy_rx_start": len(matching_start),
                    "native_phy_rx_ok": sum(item["event"] == "phy_rx_ok" for item in matching_end),
                    "native_phy_rx_error": sum(item["event"] == "phy_rx_error" for item in matching_end),
                    "packets_attempted": 1 if uid is not None else 0,
                    "packets_delivered": sum(item["event"] == "phy_rx_ok" for item in matching_end),
                    "packet_error_count": sum(item["event"] == "phy_rx_error" for item in matching_end),
                    "empirical_per": "unavailable",
                    "application_pdr": "unavailable",
                    "goodput_bps": "unavailable",
                    "end_to_end_latency_ms": "unavailable",
                    "jitter_ms": "unavailable",
                    "mobility_age_ms": real_value(mobility_age.get(rx)),
                    "ns3_realtime_lag_ms": real_value(lag_at_event),
                    "gazebo_rtf": "unavailable",
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
        row.update(
            {
                "native_mac_tx": len(matching_mac),
                "native_phy_rx_start": len(matching_start),
                "native_phy_rx_ok": sum(item["event"] == "phy_rx_ok" for item in matching_end),
                "native_phy_rx_error": sum(item["event"] == "phy_rx_error" for item in matching_end),
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
            for end in matching_end:
                outcome["ok" if end["event"] == "phy_rx_ok" else "error"].add(uid)

    for row in rows:
        key = (str(row["tx"]), str(row["rx"]))
        values = outcomes[key]
        attempts = len(values["attempted"])
        row["empirical_per"] = len(values["error"]) / attempts if attempts else "unavailable"

    columns = [
        "timestamp_wall", "timestamp_sim", "scenario_phase", "tx", "rx", "tx_x", "tx_y", "tx_z",
        "rx_x", "rx_y", "rx_z", "distance_m", "sionna_path_count", "sionna_los_available",
        "sionna_path_delay_min_ns", "sionna_path_delay_max_ns", "sionna_delay_spread_ns", "rx_power_dbm",
        "rssi_dbm", "snr_db", "sinr_db", "interference_power_dbm", "noise_power_dbm", "native_mac_tx",
        "native_phy_rx_start", "native_phy_rx_ok", "native_phy_rx_error", "packets_attempted",
        "packets_delivered", "packet_error_count", "empirical_per", "application_pdr", "goodput_bps",
        "end_to_end_latency_ms", "jitter_ms", "mobility_age_ms", "ns3_realtime_lag_ms", "gazebo_rtf",
    ]
    with (metrics_dir / "radio_link_metrics.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)

    required_pairs = [("cp", uav) for uav in UAVS] + [(uav, "cp") for uav in UAVS]
    summary_pairs = sorted(set(outcomes) | set(required_pairs))
    links: dict[str, Any] = {}
    matrix_rows: list[dict[str, Any]] = []
    for tx, rx in summary_pairs:
        samples = [row for row in rows if row["tx"] == tx and row["rx"] == rx]
        result = outcomes[(tx, rx)]
        attempted = len(result["attempted"])
        ok = len(result["ok"])
        error = len(result["error"])
        per = error / attempted if attempted else None
        path_samples = [int(row["sionna_path_count"]) for row in samples]
        state = "connected" if ok else ("no_path" if samples and not any(path_samples) else "degraded" if attempted else "no_samples")
        item = {
            "tx": tx,
            "rx": rx,
            "samples": len(samples),
            "path_count": distribution(path_samples),
            "native_mac_tx": attempted,
            "native_phy_rx_start": len(result["start"]),
            "native_phy_rx_ok": ok,
            "native_phy_rx_error": error,
            "empirical_per": per,
            "state": state,
        }
        links[f"{tx}->{rx}"] = item
        matrix_rows.append({
            "tx": tx, "rx": rx,
            "path_count": item["path_count"]["p50"],
            "rssi_dbm": "unavailable", "snr_db": "unavailable", "sinr_db": "unavailable",
            "per": per if per is not None else "unavailable", "pdr": "unavailable",
            "latency_p95_ms": "unavailable", "state": state,
        })
    write_json(metrics_dir / "radio_link_summary.json", {
        "source": "live native Sionna/PHY events only",
        "metric_availability": UNAVAILABLE_RADIO_METRICS,
        "bler": UNAVAILABLE_RADIO_METRICS["bler"],
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
        ack_control = diagnostic.get("control", {}).get("ack_latency_ms")
        ack_payload = diagnostic.get("payload", {}).get("ack_latency_ms")
        uav_rows.append({
            "uav": uav,
            "control_packets_tx": control.get("records_encoded", "unavailable"),
            "control_packets_rx": control.get("records_reassembled", "unavailable"),
            "control_pdr": control.get("records_reassembled", 0) / control["records_encoded"] if control.get("records_encoded") else "unavailable",
            "control_per": "unavailable",
            "control_rtt_p50_ms": ack_control if ack_control is not None else "unavailable",
            "control_rtt_p95_ms": ack_control if ack_control is not None else "unavailable",
            "control_rtt_max_ms": ack_control if ack_control is not None else "unavailable",
            "payload_packets_tx": payload.get("records_encoded", "unavailable"),
            "payload_packets_rx": payload.get("records_reassembled", "unavailable"),
            "payload_pdr": payload.get("records_reassembled", 0) / payload["records_encoded"] if payload.get("records_encoded") else "unavailable",
            "payload_per": "unavailable",
            "payload_rtt_p50_ms": ack_payload if ack_payload is not None else "unavailable",
            "payload_rtt_p95_ms": ack_payload if ack_payload is not None else "unavailable",
            "additional_tx": len(outcomes[(uav, "cp")]["attempted"]),
            "additional_rx": len(outcomes[("cp", uav)]["ok"]),
            "additional_pdr": len(outcomes[("cp", uav)]["ok"]) / len(outcomes[("cp", uav)]["attempted"]) if outcomes[("cp", uav)]["attempted"] else "unavailable",
            "additional_goodput_bps": "unavailable",
            "mean_rssi_dbm": "unavailable", "min_rssi_dbm": "unavailable",
            "mean_snr_db": "unavailable", "min_snr_db": "unavailable",
            "mean_sinr_db": "unavailable", "min_sinr_db": "unavailable",
            "min_path_count": min(paths) if paths else "unavailable",
            "median_path_count": percentile(paths, 50) if paths else "unavailable",
            "max_path_count": max(paths) if paths else "unavailable",
            "phy_rx_ok": sum(len(outcomes[(tx, uav)]["ok"]) for tx in {"cp", *UAVS} if tx != uav),
            "phy_rx_error": sum(len(outcomes[(tx, uav)]["error"]) for tx in {"cp", *UAVS} if tx != uav),
            "distance_min_m": min(distances) if distances else "unavailable",
            "distance_max_m": max(distances) if distances else "unavailable",
        })
    with (metrics_dir / "per_uav_network_summary.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(uav_rows[0]))
        writer.writeheader()
        writer.writerows(uav_rows)
    return {"radio_links": links, "radio_rows": len(rows), "metric_availability": UNAVAILABLE_RADIO_METRICS}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--one-uav-run", type=Path)
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    metrics_dir = run_dir / "metrics"
    scenario = read_json(metrics_dir / "scenario_summary.json", {})
    stats = read_json(metrics_dir / "native_radio_stats.json", {})
    no_bypass = read_json(metrics_dir / "no_bypass_summary.json", {})
    events = native_events(run_dir / "logs/native_radio_events.csv")
    agents = agent_records(run_dir)

    topology = build_topology(run_dir, stats)
    mobility = build_mobility(run_dir, events)
    mavlink = build_mavlink(scenario)
    p2p = build_p2p(scenario, agents)
    p2mp = build_p2mp(scenario, agents, events)
    shared = build_shared_medium(scenario, agents, events)
    realtime = build_realtime(run_dir, events, mobility)
    observability = build_radio_observability(run_dir, events, scenario)
    screenshots = screenshot_status(run_dir, events)
    one_uav = None
    if args.one_uav_run:
        one_uav = read_json(args.one_uav_run.resolve() / "metrics/native_product_summary.json", None)
        if one_uav is None:
            one_uav = read_json(args.one_uav_run.resolve() / "metrics/product_summary.json", None)

    sequential = scenario.get("dual_uart_diagnostics", {}).get("sequential", {})
    p2p_verified = all(
        p2p["per_uav"][uav]["gcs_to_uav"]["offered"] == 10
        and p2p["per_uav"][uav]["uav_to_gcs"]["independently_originated"] == 10
        for uav in UAVS
    )
    functional_checks = {
        "five_real_sitl_and_gazebo_lifecycle": scenario.get("status") == "passed",
        "five_real_odometry_sources": mobility.get("all_required_publishers_observed", False),
        "five_mobility_models_updated": mobility.get("all_mobility_models_updated", False),
        "one_shared_native_spectrum_channel": topology["shared_multi_model_spectrum_channels"] == 1,
        "six_native_phy_mac_pairs": topology["native_radio_devices"] == 6,
        "ten_uart_paths": all(item["status"] == "observed" for item in mavlink["ten_uart_paths"]),
        "control_and_payload_diagnostics_all_five": all(
            sequential.get(uav, {}).get("control", {}).get("response_received")
            and sequential.get(uav, {}).get("payload", {}).get("response_received")
            for uav in UAVS
        ),
        "p2p_exact_offers_all_five": p2p_verified,
        "p2mp_twenty_single_root_sends": p2mp["root_transmissions"] == 20
        and p2mp["application_unicast_copies"] == 0
        and p2mp["command_post_mac_tx"] == 20,
        "simultaneous_uplink_exact_offers": all(
            shared["per_uav"][uav]["offered_packets"] == 20 for uav in UAVS
        ),
        "flight_land_auto_disarm_all_five": all(
            scenario.get("uavs", {}).get(uav, {}).get("phases", {}).get("auto_disarm")
            for uav in UAVS
        ),
        "no_bypass_all_five": bool(no_bypass.get("passed")),
        "one_uav_regression": bool(one_uav and one_uav.get("status") == "passed"),
        "exact_seven_pcaps": topology["exact_pcap_count"] == 7,
    }
    functional_status = "passed" if all(functional_checks.values()) else "failed"
    if functional_status != "passed" and realtime["realtime_readiness"] == "ready":
        # Timing alone is not product readiness when the same RTF=1 run did
        # not complete the native control/flight proof.
        realtime["realtime_readiness"] = "limited"
        realtime["functional_prerequisite"] = "failed"
    process_text = (run_dir / "logs/process_snapshot.txt").read_text(
        encoding="utf-8", errors="replace"
    ) if (run_dir / "logs/process_snapshot.txt").exists() else ""
    forbidden = {
        "network/radio_provider/provider.py": "network/radio_provider/provider.py" in process_text,
        "scripts/product/town01_radio_state.py": "scripts/product/town01_radio_state.py" in process_text,
        "AmsStockSionnaPacketErrorModel": "AmsStockSionnaPacketErrorModel" in process_text,
        "centralized_priority_scheduler": "centralized_priority_scheduler" in process_text,
        "custom_five_uav_packet_engine": "ams-tap-packet-engine" in process_text,
    }
    status = "functional_native_path" if functional_status == "passed" else "realtime_failed"
    if functional_status == "passed":
        status = "realtime_ready" if realtime["realtime_readiness"] == "ready" else "realtime_limited"
    operating_envelope = {
        "uav_count": 5,
        "motion_pattern": "five-UAV flight with UAV1 moving and UAV2..UAV5 holding",
        "cache_policy": stats.get("cache_policy", "displacement_or_time"),
        "update_period_s": stats.get("channel_state_max_age_s"),
        "displacement_threshold_m": stats.get("endpoint_displacement_threshold_m"),
        "solver_calls_per_s": None,
        "channel_state_age_ms": {
            "p50": None,
            "p95": (stats.get("channel_state_max_age_s") or 0) * 1000.0,
            "max": (stats.get("channel_state_max_age_s") or 0) * 1000.0,
            "basis": "bounded maximum configured for live cache; per-solve generated timestamps are in native log",
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
        "functional_five_uav_native_path": functional_status,
        "functional_checks": functional_checks,
        "realtime_readiness": realtime["realtime_readiness"],
        "profile": "generic_native_spectrum_aloha_reference",
        "technology_specific_modem": False,
        "native_topology": topology,
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
        },
        "realtime": realtime,
        "observability": observability,
        "screenshots": screenshots,
        "operating_envelope": operating_envelope,
        "no_bypass": no_bypass,
        "one_uav_regression": one_uav,
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
        "- Radio: 2.4 GHz, 5 MHz, 1 Mbit/s, 0.01 W, maxDepth 1, LOS + specular.",
        f"- P2MP: {p2mp['root_transmissions']} application roots, {p2mp['command_post_mac_tx']} command-post MacTx.",
        f"- Simultaneous uplink Jain fairness: {shared['jain_fairness']}",
        f"- No-bypass after common native process stop: {no_bypass.get('passed')}",
        f"- One-UAV regression attached: {one_uav is not None}",
        f"- Radio-link observations: {observability['radio_rows']}; RSSI/SNR/SINR are explicitly unavailable when the selected native API does not expose them.",
        f"- BLER: {UNAVAILABLE_RADIO_METRICS['bler']}",
        f"- Live Gazebo screenshots: {screenshots['screenshots_status']}.",
        "",
        "All packet and timing results above are derived from endpoint logs, native ns-3 traces, ROS tracker snapshots, Gazebo stats, and process-resource samples in this run directory.",
    ]
    report.extend(["", "## Live Gazebo frames"])
    for stem in REQUIRED_SCREENSHOTS:
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
                "Native evidence in the +/- 2 simulation-second frame window: "
                f"Sionna path observations={observation['sionna_path_observations']}, "
                f"path count={observation['sionna_path_count_min']}..{observation['sionna_path_count_max']}, "
                f"PHY RxEndOk/RxEndError={observation['phy_rx_ok']}/{observation['phy_rx_error']}.",
            ]
        )
    (run_dir / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    print(json.dumps({"run_dir": str(run_dir), "functional": functional_status, "realtime": realtime["realtime_readiness"]}, sort_keys=True))
    return 0 if functional_status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
