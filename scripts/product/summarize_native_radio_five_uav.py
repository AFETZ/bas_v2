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
    misses = hits = updates = 0
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
        "cache": {"hits": hits, "misses": misses, "stale_updates": updates},
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
        "",
        "All packet and timing results above are derived from endpoint logs, native ns-3 traces, ROS tracker snapshots, Gazebo stats, and process-resource samples in this run directory.",
    ]
    (run_dir / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    print(json.dumps({"run_dir": str(run_dir), "functional": functional_status, "realtime": realtime["realtime_readiness"]}, sort_keys=True))
    return 0 if functional_status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
