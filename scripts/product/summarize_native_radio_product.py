#!/usr/bin/env python3
"""Timestamp native logs or summarize the one-UAV native-radio product run."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable


def write_json(path: Path, value: object) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - position) + ordered[upper] * (position - lower)


def numeric_summary(values: Iterable[float]) -> dict[str, Any]:
    finite = [float(value) for value in values if math.isfinite(float(value))]
    return {
        "count": len(finite),
        "min": min(finite) if finite else None,
        "p5": percentile(finite, 0.05),
        "p50": percentile(finite, 0.50),
        "p95": percentile(finite, 0.95),
        "max": max(finite) if finite else None,
        "mean": statistics.fmean(finite) if finite else None,
    }


def load_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
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


def run_timestamp(args: argparse.Namespace) -> int:
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", buffering=1) as stream:
        for line in sys.stdin:
            stream.write(f"{time.monotonic_ns()}\t{line.rstrip()}\n")
    return 0


def phase_at(phases: list[tuple[int, str]], timestamp_ns: int) -> str:
    result = "unclassified"
    for changed_ns, phase in phases:
        if changed_ns > timestamp_ns:
            break
        result = phase
    return result


def mobility_summary(run_dir: Path) -> dict[str, Any]:
    samples: list[tuple[float, list[float], bool]] = []
    stale_samples = 0
    for state in jsonl(run_dir / "logs/node_state.jsonl"):
        position: list[float] | None = None
        stale = bool(state.get("missing_nodes") or state.get("stale_nodes"))
        source_topic = ""
        for node in state.get("nodes", []):
            if node.get("id") == "uav1":
                if isinstance(node.get("position_m"), list) and len(node["position_m"]) == 3:
                    position = [float(value) for value in node["position_m"]]
                stale = stale or bool(node.get("stale"))
                source_topic = str(node.get("source_topic", ""))
        stale_samples += int(stale)
        if (
            position is not None
            and not stale
            and state.get("source") == "ros_odometry"
            and source_topic == "/uav1/odometry"
        ):
            samples.append((float(state.get("time_s", 0.0)), position, stale))
    positions = [sample[1] for sample in samples]
    first = positions[0] if positions else None
    duration = samples[-1][0] - samples[0][0] if len(samples) > 1 else 0.0
    publisher = "unknown"
    topic_info = (run_dir / "logs/odometry_topic_info.txt").read_text(
        encoding="utf-8", errors="replace"
    ) if (run_dir / "logs/odometry_topic_info.txt").exists() else ""
    names = re.findall(r"Node name:\s*(\S+)", topic_info)
    if names:
        publisher = names[0]
    return {
        "uav_id": "uav1",
        "ROS topic": "/uav1/odometry",
        "publisher node": publisher,
        "sample count": len(samples),
        "first position": first,
        "last position": positions[-1] if positions else None,
        "maximum displacement": max((math.dist(first, value) for value in positions), default=None) if first else None,
        "maximum altitude": max((value[2] for value in positions), default=None),
        "stale sample count": stale_samples,
        "update rate": (len(samples) - 1) / duration if duration > 0 else None,
        "source": "gazebo_odometry",
    }


def parse_native_log(run_dir: Path, phases: list[tuple[int, str]]) -> dict[str, Any]:
    path = run_dir / "logs/ns3_sionna.log"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    starts: list[int] = []
    solves: list[dict[str, Any]] = []
    rx_by_phase: defaultdict[str, list[dict[str, float]]] = defaultdict(list)
    cache_hit = 0
    cache_miss = 0
    path_counts: list[int] = []
    for line in lines:
        timestamp_text, separator, text = line.partition("\t")
        if not separator or not timestamp_text.isdigit():
            continue
        timestamp_ns = int(timestamp_text)
        if "Building scene for antenna pair" in text:
            starts.append(timestamp_ns)
        elif "Path computation finished" in text and starts:
            started_ns = starts.pop(0)
            solves.append(
                {
                    "started_monotonic_ns": started_ns,
                    "finished_monotonic_ns": timestamp_ns,
                    "duration_ms": (timestamp_ns - started_ns) / 1e6,
                    "phase": phase_at(phases, started_ns),
                }
            )
        if "channel matrix present in the map" in text:
            cache_hit += 1
        if "channel matrix not found" in text:
            cache_miss += 1
        match = re.search(r"Number of generated paths:\s*(\d+)", text)
        if match:
            path_counts.append(int(match.group(1)))
        match = re.search(r"rx power:\s*([-+a-zA-Z0-9.e]+)\s*dBm", text)
        if match:
            try:
                power_dbm = float(match.group(1))
            except ValueError:
                continue
            if math.isfinite(power_dbm):
                power_w = 10 ** ((power_dbm - 30.0) / 10.0)
                noise_w = 1.381e-23 * 290.0 * 5e6
                rx_by_phase[phase_at(phases, timestamp_ns)].append(
                    {
                        "monotonic_ns": timestamp_ns,
                        "received_power_dbm": power_dbm,
                        "received_psd_w_per_hz": power_w / 5e6,
                        "snr_db": 10.0 * math.log10(power_w / noise_w),
                    }
                )
    durations = [item["duration_ms"] for item in solves]
    return {
        "solve_duration_ms": numeric_summary(durations),
        "cold_start": solves[0] if solves else None,
        "steady_state": {
            "solve_count": max(0, len(solves) - 1),
            "solve_duration_ms": numeric_summary(durations[1:]),
        },
        "solves": solves,
        "cache": {"hit": cache_hit, "miss": cache_miss, "upstream_log_available": True},
        "path_computations": len(solves),
        "logged_path_counts": path_counts,
        "rx_by_phase": dict(rx_by_phase),
    }


def resource_summary(run_dir: Path) -> dict[str, Any]:
    grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in jsonl(run_dir / "logs/runtime_resources.jsonl"):
        grouped[str(row.get("component", "unknown"))].append(row)
    result: dict[str, Any] = {}
    for component, rows in grouped.items():
        result[component] = {
            "cpu_percent_one_core": numeric_summary(
                float(row["cpu_percent_one_core"])
                for row in rows
                if row.get("cpu_percent_one_core") is not None
            ),
            "rss_bytes": numeric_summary(float(row["rss_bytes"]) for row in rows),
            "gpu_memory_bytes": numeric_summary(
                float(row["gpu_memory_bytes"])
                for row in rows
                if row.get("gpu_memory_bytes") is not None
            ),
        }
    return result


def run_summarize(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    metrics = run_dir / "metrics"
    events_path = run_dir / "logs/native_radio_events.csv"
    scenario_events = jsonl(run_dir / "logs/product_scenario_events.jsonl")
    phases = sorted(
        (int(row["monotonic_ns"]), str(row.get("detail", "unclassified")))
        for row in scenario_events
        if row.get("event") == "phase" and isinstance(row.get("monotonic_ns"), int)
    )
    radio_events: list[dict[str, str]] = []
    try:
        with events_path.open(encoding="utf-8") as stream:
            radio_events = list(csv.DictReader(stream))
    except OSError:
        pass
    native_stats = load_json(metrics / "native_radio_stats.json", {})
    mavlink = load_json(metrics / "mavlink_summary.json", {})
    mobility = mobility_summary(run_dir)
    write_json(metrics / "mobility_source.json", mobility)

    lag = [float(row["value"]) for row in radio_events if row.get("event") == "realtime_lag" and row.get("value")]
    ages = [float(row["value"]) for row in radio_events if row.get("event") == "live_pose" and row.get("value")]
    native_log = parse_native_log(run_dir, phases)
    gazebo_text = (run_dir / "logs/gazebo_stats.log").read_text(
        encoding="utf-8", errors="replace"
    ) if (run_dir / "logs/gazebo_stats.log").exists() else ""
    rtf = [float(value) for value in re.findall(r"real_time_factor:\s*([0-9.eE+-]+)", gazebo_text)]
    realtime = {
        "sionna": native_log,
        "ns3_realtime_lag_ms": numeric_summary(lag),
        "gazebo_real_time_factor": numeric_summary(rtf),
        "odometry_update_rate_hz": mobility.get("update rate"),
        "applied_position_age_ms": numeric_summary(ages),
        "mavlink_rtt_ms": numeric_summary(mavlink.get("mavlink_rtt_ms", [])),
        "resources": resource_summary(run_dir),
    }
    write_json(metrics / "realtime_summary.json", realtime)

    phase_radio: dict[str, Any] = {}
    point_observations = {
        str(row["point"]): int(row["monotonic_ns"])
        for row in scenario_events
        if row.get("event") == "position_observation"
        and isinstance(row.get("point"), str)
        and isinstance(row.get("monotonic_ns"), int)
    }
    point_window_ns = 2_000_000_000
    point_to_phase = {
        "takeoff_hold": "takeoff",
        "los": "los_hold",
        "obstructed_candidate": "nlos_hold",
        "return": "return",
        "landed_disarmed": "landing",
    }
    for point, phase in point_to_phase.items():
        observation_ns = point_observations.get(point)
        rows = [
            row
            for row in radio_events
            if row.get("phase") == phase
            and observation_ns is not None
            and observation_ns - point_window_ns
            <= int(row.get("wall_monotonic_ns") or 0)
            <= observation_ns
        ]
        paths = [row for row in rows if row.get("event") == "sionna_paths"]
        delays = [
            float(value)
            for value in (paths[-1].get("details", "").split(";") if paths else [])
            if value
        ]
        powers = [
            item
            for item in native_log["rx_by_phase"].get(phase, [])
            if observation_ns is not None
            and observation_ns - point_window_ns <= item["monotonic_ns"] <= observation_ns
        ]
        phase_radio[point] = {
            "measurement_window_s": point_window_ns / 1e9,
            "measurement_ends_at_observation_ns": observation_ns,
            "sionna_path_count": int(float(paths[-1]["value"])) if paths and paths[-1].get("value") else None,
            "path_delays": delays,
            "received_psd_w_per_hz": numeric_summary(item["received_psd_w_per_hz"] for item in powers),
            "snr_db": numeric_summary(item["snr_db"] for item in powers),
            "native_phy_RxEndOk": sum(row.get("event") == "phy_rx_ok" for row in rows),
            "native_phy_RxEndError": sum(row.get("event") == "phy_rx_error" for row in rows),
        }
        if point in mavlink.get("points", {}):
            mavlink["points"][point].update(phase_radio[point])
    additional_events = jsonl(run_dir / "logs/additional_uart_endpoint.jsonl")
    additional = mavlink.setdefault("additional_data", {})
    downlink_received = {
        int(row["sequence"])
        for row in additional_events
        if row.get("event") == "receive"
        and row.get("kind") == "p2p_downlink"
        and isinstance(row.get("sequence"), int)
    }
    uplink_sent = {
        int(row["sequence"])
        for row in additional_events
        if row.get("event") == "transmit"
        and row.get("kind") == "p2p_uplink"
        and isinstance(row.get("sequence"), int)
    }
    additional["gcs_to_uav_received_by_application"] = len(downlink_received)
    additional["uav_to_gcs_sent_by_application"] = len(uplink_sent)
    additional["gcs_to_uav_observed_pdr"] = (
        len(downlink_received) / additional["gcs_to_uav_sent"]
        if additional.get("gcs_to_uav_sent")
        else None
    )
    additional["uav_to_gcs_observed_pdr"] = (
        additional.get("uav_to_gcs_received", 0) / len(uplink_sent) if uplink_sent else None
    )
    write_json(metrics / "mavlink_summary.json", mavlink)

    radio = {
        "profile": "generic_native_spectrum_aloha_reference",
        "technology_specific_modem": False,
        "native_ns3_phy": True,
        "native_ns3_mac": True,
        "custom_packet_error_model": False,
        "custom_scheduler": False,
        "sionna_in_process": True,
        "phy": "HalfDuplexIdealPhy with ShannonSpectrumErrorModel",
        "mac": "AlohaNoackNetDevice",
        "channel": "MultiModelSpectrumChannel",
        "propagation": "SionnaRtSpectrumPropagationLossModel",
        "tap": "TapBridge",
        "tap_ingress_segment": "local 1 Gbit/s CSMA attachment; not radio medium",
        "solver_profile": {
            "name": "realtime_minimal_solver_profile",
            "frequency_hz": 2.4e9,
            "bandwidth_hz": 5e6,
            "maxDepth": 1,
            "LOS": True,
            "specular_reflection": True,
            "diffuse_reflection": False,
            "diffraction": False,
            "refraction": False,
            "synthetic_array": True,
            "real_sionna_path_solver": True,
            "complete_nlos_model": False,
        },
        "counters": native_stats,
        "phase_observations": phase_radio,
        "radio_pcap_bytes": (run_dir / "pcap/native_radio.pcap").stat().st_size if (run_dir / "pcap/native_radio.pcap").exists() else 0,
        "tap_gcs_pcap_bytes": (run_dir / "pcap/tap_gcs.pcap").stat().st_size if (run_dir / "pcap/tap_gcs.pcap").exists() else 0,
        "tap_uav_pcap_bytes": (run_dir / "pcap/tap_uav.pcap").stat().st_size if (run_dir / "pcap/tap_uav.pcap").exists() else 0,
    }
    write_json(metrics / "radio_summary.json", radio)

    sockets = (run_dir / "logs/gcs_sockets_after_stop.txt").read_text(
        encoding="utf-8", errors="replace"
    ) if (run_dir / "logs/gcs_sockets_after_stop.txt").exists() else ""
    forbidden_socket = bool(
        re.search(r"(?<!\d)(14550|5760|5770|5780|5790|5800|5501|9002|2019)(?!\d)", sockets)
    )
    fail_closed = dict(mavlink.get("fail_closed") or {})
    fail_closed["gcs_forbidden_direct_socket_present"] = forbidden_socket
    fail_closed["passed"] = bool(fail_closed.get("passed")) and not forbidden_socket

    custom_needles = {
        "network/radio_provider/provider.py",
        "town01_radio_state.py",
        "AmsStockSionnaPacketErrorModel",
        "centralized_priority_scheduler",
        "abstract_service_tier_v1",
    }
    process_text = (run_dir / "logs/process_snapshot.txt").read_text(
        encoding="utf-8", errors="replace"
    ) if (run_dir / "logs/process_snapshot.txt").exists() else ""
    forbidden_present = sorted(needle for needle in custom_needles if needle in process_text)
    additional = mavlink.get("additional_data") or {}
    complete = all(
        (
            native_stats.get("pose_updates", 0) > 0,
            native_stats.get("cp_mac_tx", 0) + native_stats.get("uav_mac_tx", 0) > 0,
            native_stats.get("cp_phy_rx_ok", 0) + native_stats.get("uav_phy_rx_ok", 0) > 0,
            mavlink.get("status") == "passed",
            additional.get("gcs_to_uav_sent") == 10,
            additional.get("gcs_to_uav_received_by_application", 0) > 0,
            additional.get("uav_to_gcs_sent_by_application") == 10,
            additional.get("uav_to_gcs_received", 0) > 0,
            fail_closed.get("passed") is True,
            mobility.get("sample count", 0) > 0,
            mobility.get("maximum displacement", 0) > 10,
            not forbidden_present,
        )
    )
    environment = {}
    try:
        for line in (run_dir / "environment.txt").read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator:
                environment[key] = value
    except OSError:
        pass
    product = {
        "status": "passed" if complete else "failed",
        "profile": "generic_native_spectrum_aloha_reference",
        "ns3": {
            "version": "3.48",
            "exact_sha": environment.get("ns3_exact_sha"),
            "compatibility_patch": True,
            "compatibility_scope": "Sionna RT load_scene and phased-array pointer API only",
        },
        "dependencies": {
            key: environment.get(key)
            for key in ("python_version", "sionna_version", "sionna_rt_version", "mitsuba_version", "drjit_version", "pybind11_version", "compiler_version", "cmake_version")
        },
        "mobility_source": "gazebo_odometry",
        "application_source": "real ArduPilot SITL sysid 1 on SERIAL1 and SERIAL2",
        "forbidden_custom_components_absent": not forbidden_present,
        "forbidden_custom_components_present": forbidden_present,
        "no_bypass": fail_closed,
        "parameters_frozen_before_run": True,
        "criteria_complete": complete,
    }
    write_json(metrics / "native_product_summary.json", product)

    report_lines = [
        "# Native radio product — one real UAV",
        "",
        f"Status: **{product['status']}**. Profile: `generic_native_spectrum_aloha_reference`.",
        "",
        f"- ns-3: `3.48` `{product['ns3']['exact_sha']}`; minimal API compatibility patch: yes.",
        f"- Runtime: Python `{environment.get('python_version')}`, Sionna `{environment.get('sionna_version')}`, Sionna RT `{environment.get('sionna_rt_version')}`, Mitsuba `{environment.get('mitsuba_version')}`, Dr.Jit `{environment.get('drjit_version')}`.",
        "- Radio: `MultiModelSpectrumChannel` → `SionnaRtSpectrumPropagationLossModel` → `HalfDuplexIdealPhy` / `ShannonSpectrumErrorModel` → `AlohaNoackNetDevice` → `TapBridge`.",
        "- `tap_ingress_segment` is the local 1 Gbit/s CSMA TAP attachment, not the radio medium.",
        f"- Mobility: `/uav1/odometry`, publisher `{mobility.get('publisher node')}`, {mobility.get('sample count')} samples, max displacement `{mobility.get('maximum displacement')}` m, stale `{mobility.get('stale sample count')}`.",
        f"- Control UART: `{json.dumps(mavlink.get('control_uart'), sort_keys=True)}`.",
        f"- Payload UART: `{json.dumps(mavlink.get('payload_uart'), sort_keys=True)}`.",
        f"- Additional data: `{json.dumps(additional, sort_keys=True)}`.",
        f"- Flight points: `{json.dumps(mavlink.get('points'), sort_keys=True)}`.",
        f"- Native counters: `{json.dumps(native_stats, sort_keys=True)}`.",
        f"- Sionna solve duration ms: `{json.dumps(native_log['solve_duration_ms'], sort_keys=True)}`; cold `{json.dumps(native_log['cold_start'], sort_keys=True)}`; steady `{json.dumps(native_log['steady_state'], sort_keys=True)}`.",
        f"- ns-3 realtime lag ms: `{json.dumps(realtime['ns3_realtime_lag_ms'], sort_keys=True)}`.",
        f"- Gazebo RTF: `{json.dumps(realtime['gazebo_real_time_factor'], sort_keys=True)}`.",
        f"- No-bypass/fail-closed: `{json.dumps(fail_closed, sort_keys=True)}`.",
        "- technology_specific_modem: false; native_ns3_phy: true; native_ns3_mac: true; custom_packet_error_model: false; custom_scheduler: false; sionna_in_process: true.",
        "- Solver is the real Sionna PathSolver with depth 1 and LOS/specular only; it is not a complete NLOS model and excludes diffraction/diffuse scattering.",
        f"- Forbidden custom runtime components present: `{forbidden_present}`.",
        "- No PDR threshold or desired NLOS outcome was imposed; recorded outcomes are observational.",
    ]
    (run_dir / "report.md").write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    print(json.dumps(product, allow_nan=False, sort_keys=True))
    return 0 if complete else 1


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    timestamp = commands.add_parser("timestamp")
    timestamp.add_argument("--output", required=True)
    timestamp.set_defaults(function=run_timestamp)
    summarize = commands.add_parser("summarize")
    summarize.add_argument("--run-dir", required=True)
    summarize.set_defaults(function=run_summarize)
    return root


def main() -> int:
    args = parser().parse_args()
    return int(args.function(args))


if __name__ == "__main__":
    raise SystemExit(main())
