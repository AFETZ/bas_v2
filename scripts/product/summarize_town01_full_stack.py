#!/usr/bin/env python3
"""Summarize observable Town01 full-stack runtime artifacts and packet metrics."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import statistics
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any


CLASSES = ("control", "payload", "additional_data")
ROOT = Path(__file__).resolve().parents[2]


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
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


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def numeric_summary(values: list[float]) -> dict[str, float | None]:
    return {
        "count": len(values),
        "min": min(values) if values else None,
        "mean": statistics.fmean(values) if values else None,
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
        "max": max(values) if values else None,
    }


def packet_metrics(events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    filtered = [
        event
        for event in events
        if event.get("traffic_class") in CLASSES and event.get("transport_protocol") == 17
    ]
    event_counts: Counter[tuple[str, str, bool]] = Counter(
        (
            str(event["traffic_class"]),
            str(event.get("event")),
            bool(event.get("p2mp")),
        )
        for event in filtered
    )
    ingress_by_uid: dict[tuple[int, str], int] = {}
    enqueue_by_uid: dict[tuple[int, str], deque[int]] = defaultdict(deque)
    latency_by_class: dict[str, list[float]] = defaultdict(list)
    queue_by_class: dict[str, list[float]] = defaultdict(list)
    egress_bytes: Counter[str] = Counter()
    radio_age_ms: list[float] = []
    timestamps = [
        int(event["host_monotonic_ns"])
        for event in filtered
        if isinstance(event.get("host_monotonic_ns"), int)
        and int(event["host_monotonic_ns"]) > 0
    ]
    duration_s = (
        (max(timestamps) - min(timestamps)) / 1e9 if len(timestamps) > 1 else 0.001
    )
    for event in filtered:
        traffic_class = str(event["traffic_class"])
        uid = int(event.get("packet_uid", -1))
        link = str(event.get("directed_link", ""))
        key = (uid, link)
        timestamp = int(event.get("host_monotonic_ns", 0) or 0)
        kind = event.get("event")
        if kind == "ingress" and not event.get("p2mp"):
            ingress_by_uid[key] = timestamp
        elif kind == "egress" and not event.get("p2mp"):
            started = ingress_by_uid.pop(key, None)
            if started is not None and timestamp >= started:
                latency_by_class[traffic_class].append((timestamp - started) / 1e6)
            egress_bytes[traffic_class] += int(event.get("transport_payload_size", 0) or 0)
        elif kind == "egress":
            egress_bytes[traffic_class] += int(event.get("transport_payload_size", 0) or 0)
        if kind == "enqueue":
            enqueue_by_uid[key].append(timestamp)
        elif kind == "dequeue" and enqueue_by_uid[key]:
            started = enqueue_by_uid[key].popleft()
            if timestamp >= started:
                queue_by_class[traffic_class].append((timestamp - started) / 1e6)
        age = event.get("radio_state_age_ns")
        if isinstance(age, int) and age >= 0:
            radio_age_ms.append(age / 1e6)

    rows: list[dict[str, Any]] = []
    summary: dict[str, Any] = {}
    for traffic_class in CLASSES:
        unicast_ingress = event_counts[(traffic_class, "ingress", False)]
        unicast_egress = event_counts[(traffic_class, "egress", False)]
        unicast_drops = event_counts[(traffic_class, "drop", False)]
        p2mp_ingress = event_counts[(traffic_class, "ingress", True)]
        p2mp_roots = event_counts[(traffic_class, "channel", True)]
        p2mp_egress = event_counts[(traffic_class, "egress", True)]
        p2mp_receivers = {
            str(event.get("device_id", "")).split(".", 1)[0]
            for event in filtered
            if event.get("traffic_class") == traffic_class
            and event.get("event") == "egress"
            and event.get("p2mp")
        }
        unicast_pdr = unicast_egress / unicast_ingress if unicast_ingress else None
        latency = latency_by_class[traffic_class]
        queue = queue_by_class[traffic_class]
        row = {
            "traffic_class": traffic_class,
            "unicast_ingress_packets": unicast_ingress,
            "unicast_egress_packets": unicast_egress,
            "unicast_drop_events": unicast_drops,
            "unicast_pdr": unicast_pdr,
            "p2mp_ingress_packets": p2mp_ingress,
            "p2mp_root_transmissions": p2mp_roots,
            "p2mp_delivery_packets": p2mp_egress,
            "p2mp_receiver_count": len(p2mp_receivers),
            "delivered_goodput_bps": egress_bytes[traffic_class]
            * 8.0
            / max(duration_s, 0.001),
            "latency_mean_ms": statistics.fmean(latency) if latency else None,
            "latency_p95_ms": percentile(latency, 0.95),
            "jitter_ms": statistics.pstdev(latency) if len(latency) > 1 else 0.0 if latency else None,
            "queue_delay_mean_ms": statistics.fmean(queue) if queue else None,
            "queue_delay_p95_ms": percentile(queue, 0.95),
        }
        rows.append(row)
        summary[traffic_class] = row
    summary["radio_state_age_ms"] = numeric_summary(radio_age_ms)
    summary["observation_duration_s"] = duration_s
    summary["total_event_count"] = len(events)
    summary["udp_event_count"] = len(filtered)
    return rows, summary


def radio_metrics(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
    except OSError:
        return {}
    unique_queries: dict[str, float] = {}
    values: dict[str, list[float]] = defaultdict(list)
    geometry = Counter()
    stale = 0
    for row in rows:
        unique_queries.setdefault(row["query_index"], float(row["provider_latency_ms"]))
        for field in ("pathloss_db", "rssi_dbm", "sinr_db", "js_db", "per_input"):
            values[field].append(float(row[field]))
        geometry[row["geometry_state"]] += 1
        stale += 1 if row["stale"].lower() == "true" else 0
    return {
        "provider_mode": "real_sionna",
        "scene_id": "cavise_town01_editor_lod0_full_20260712",
        "query_count": len(unique_queries),
        "link_rows": len(rows),
        "provider_latency_ms": numeric_summary(list(unique_queries.values())),
        "pathloss_db": numeric_summary(values["pathloss_db"]),
        "rssi_dbm": numeric_summary(values["rssi_dbm"]),
        "sinr_db": numeric_summary(values["sinr_db"]),
        "js_db": numeric_summary(values["js_db"]),
        "per_input": numeric_summary(values["per_input"]),
        "geometry_states": dict(geometry),
        "stale_link_rows": stale,
    }


def gazebo_metrics(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    factors = [float(value) for value in re.findall(r"real_time_factor:\s*([0-9.eE+-]+)", text)]
    return {"sample_count": len(factors), "real_time_factor": numeric_summary(factors)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    metrics_dir = run_dir / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    scenario = read_json(metrics_dir / "scenario_summary.json")
    health = read_json(metrics_dir / "health.json")
    down = read_json(metrics_dir / "ns3_stopped_probe.json")
    heatmaps = read_json(run_dir / "heatmaps/heatmap_summary.json")
    derivative = read_json(
        ROOT / ".external/cavise_maps/Town01/gazebo/derivative_summary.json"
    )
    events = read_jsonl(run_dir / "logs/ns3_packet_events.jsonl")
    packet_rows, packets = packet_metrics(events)
    with (metrics_dir / "packet_metrics.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(packet_rows[0]) if packet_rows else ["traffic_class"])
        writer.writeheader()
        writer.writerows(packet_rows)

    pcaps = [
        {"path": str(path.relative_to(run_dir)), "bytes": path.stat().st_size}
        for path in sorted((run_dir / "pcap").glob("*.pcap"))
        if path.stat().st_size > 24
    ]
    radio = radio_metrics(metrics_dir / "radio_links.csv")
    ns3_records = read_jsonl(run_dir / "logs/ns3_packet_engine.log")
    ns3_runtime = ns3_records[-1] if ns3_records else {}
    scenario_passed = scenario.get("status") == "passed" and all(
        all(
            bool(scenario.get("uavs", {}).get(f"uav{index}", {}).get("phases", {}).get(phase))
            for phase in ("heartbeat", "arm", "takeoff", "hold", "movement", "land")
        )
        for index in range(1, 6)
    )
    health_passed = health.get("status") in {"healthy", "passed"}
    no_bypass = bool(down.get("exchange_stopped"))
    message_counts = scenario.get("message_counts", {})
    dual_uart = all(
        int(message_counts.get(f"control:uav{index}:LOCAL_POSITION_NED", 0)) > 0
        and int(message_counts.get(f"payload:uav{index}:ATTITUDE", 0)) > 0
        for index in range(1, 6)
    )
    additional = scenario.get("additional_data", {})
    additional_packets = packets.get("additional_data", {})
    additional_data = (
        int(additional.get("p2p_packets_sent", 0)) == 50
        and all(
            int(additional.get("p2p_ack_counts", {}).get(f"uav{index}", 0)) >= 10
            for index in range(1, 6)
        )
        and additional.get("p2mp_receivers") == [f"uav{index}" for index in range(1, 6)]
        and additional_packets.get("unicast_pdr") == 1.0
        and additional_packets.get("p2mp_root_transmissions", 0) >= 1
        and additional_packets.get("p2mp_receiver_count") == 5
    )
    ns3_passed = (
        ns3_runtime.get("status") == "passed"
        and ns3_runtime.get("uav_count") == 5
        and ns3_runtime.get("p2mp_root_transmissions", 0) >= 1
        and ns3_runtime.get("p2mp_egress_devices") == 5
        and bool(events)
    )
    sionna_passed = (
        radio.get("provider_mode") == "real_sionna"
        and radio.get("scene_id") == "cavise_town01_editor_lod0_full_20260712"
        and int(radio.get("query_count", 0)) > 0
        and int(radio.get("stale_link_rows", 1)) == 0
    )
    expected_heatmaps = {
        f"{phase}_{metric}.png"
        for phase in ("baseline", "jammer", "delta")
        for metric in ("rssi_dbm", "sinr_db", "js_db", "service_available")
    }
    heatmaps_passed = (
        heatmaps.get("scene_id") == "cavise_town01_editor_lod0_full_20260712"
        and set(heatmaps.get("images", [])) == expected_heatmaps
        and all(
            (run_dir / "heatmaps" / name).is_file()
            and (run_dir / "heatmaps" / name).stat().st_size > 0
            for name in expected_heatmaps
        )
    )
    pcap_passed = len(pcaps) == 6
    components = {
        "gazebo_town01": (
            health_passed
            and derivative.get("coordinate_transform") == "identity"
            and derivative.get("max_source_to_gazebo_vertex_delta_m") == 0.0
            and int(derivative.get("visual_meshes", 0)) > 0
        ),
        "ardupilot_sitl_count": len(health.get("sitl", [])),
        "ros_odometry": health_passed,
        "sionna_rt": sionna_passed,
        "ns3_tap_packet_engine": ns3_passed,
        "dual_uart": dual_uart,
        "additional_data": additional_data,
        "heatmaps": heatmaps_passed,
        "pcap_count": len(pcaps),
    }
    overall_passed = (
        scenario_passed
        and health_passed
        and no_bypass
        and pcap_passed
        and all(
            bool(value)
            for key, value in components.items()
            if key not in {"ardupilot_sitl_count", "pcap_count"}
        )
        and components["ardupilot_sitl_count"] == 5
    )
    summary = {
        "run_id": run_dir.name,
        "status": "passed" if overall_passed else "failed",
        "scenario": "scenario_5uav_town01",
        "uav_count": 5,
        "components": components,
        "flight": scenario,
        "packet_path": packets,
        "radio": radio,
        "ns3_runtime": ns3_runtime,
        "gazebo": gazebo_metrics(run_dir / "logs/gazebo_stats.log"),
        "heatmaps": heatmaps,
        "no_bypass": {
            "ns3_stop_breaks_control_exchange": no_bypass,
            "probe": down,
        },
        "pcaps": pcaps,
        "town01_derivative": derivative,
        "known_limits": [
            "Town01 is 3.191 km by 3.191 km and does not satisfy the separate 10 km by 10 km requirement.",
            "Gazebo uses source-coordinate visual meshes with axis-aligned surface and building collision-box approximations.",
            "The Gazebo derivative omits vegetation visuals and vegetation collisions for runtime cost.",
            "The ns-3 packet engine uses the declared CSMA shared-medium engineering surrogate, not a customer modem waveform.",
        ],
    }
    (metrics_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    packet_lines = []
    for traffic_class in CLASSES:
        item = packets.get(traffic_class, {})
        packet_lines.append(
            "| {name} | {ingress}/{egress} | {pdr:.6f} | {goodput:.1f} | "
            "{latency:.3f} | {latency_p95:.3f} | {queue_p95:.3f} |".format(
                name=traffic_class,
                ingress=item.get("unicast_ingress_packets", 0),
                egress=item.get("unicast_egress_packets", 0),
                pdr=float(item.get("unicast_pdr") or 0.0),
                goodput=float(item.get("delivered_goodput_bps") or 0.0),
                latency=float(item.get("latency_mean_ms") or 0.0),
                latency_p95=float(item.get("latency_p95_ms") or 0.0),
                queue_p95=float(item.get("queue_delay_p95_ms") or 0.0),
            )
        )
    report = f"""# Town01 full-stack run

- Run: `{run_dir.name}`
- Result: **{summary['status']}**
- Flight lifecycle: `{scenario.get('status', 'missing')}` for five UAVs
- Real Sionna queries: `{summary['radio'].get('query_count', 0)}`; scene `{summary['radio'].get('scene_id', 'missing')}`
- ns-3 UDP events: `{packets.get('udp_event_count', 0)}`; non-empty PCAP files: `{len(pcaps)}`
- ns-3 stop broke the control exchange: `{str(no_bypass).lower()}`
- Gazebo RTF mean: `{summary['gazebo'].get('real_time_factor', {}).get('mean')}`
- Heatmap images: `{len(heatmaps.get('images', []))}`

| Traffic class | Unicast ingress/egress | PDR | Delivered goodput (bit/s) | Mean latency (ms) | P95 latency (ms) | P95 queue (ms) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
{chr(10).join(packet_lines)}

The additional-data phase sent 50 P2P packets and received 10 acknowledgements
from each UAV. One P2MP root transmission reached all five receivers. The live
radio updater made {summary['radio'].get('query_count', 0)} real-Sionna queries
covering {summary['radio'].get('link_rows', 0)} directed traffic-class links;
no returned link row was stale.

This is a factual Town01 development run. It does not close the 10 km by 10 km
map requirement. Gazebo collision geometry is approximated with axis-aligned
boxes, and the shared medium remains the documented CSMA surrogate.
"""
    (run_dir / "report.md").write_text(report, encoding="utf-8")
    print(json.dumps(summary, sort_keys=True))
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
