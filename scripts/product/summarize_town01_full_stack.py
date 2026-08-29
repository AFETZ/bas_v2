#!/usr/bin/env python3
"""Summarize observable Town01 full-stack runtime artifacts and packet metrics."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import statistics
import struct
import sys
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any


CLASSES = ("control", "payload", "additional_data")
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from network.scripts.communication_qos import (  # noqa: E402
    PROFILE_NAMES,
    load_qos,
)
from network.ns3.tap_packet_engine_config import ConfigError, data_rate_bps  # noqa: E402
from network.scripts.packet_accounting import account_packets, group_accounting  # noqa: E402


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
        "p5": percentile(values, 0.05),
        "mean": statistics.fmean(values) if values else None,
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
        "p99": percentile(values, 0.99),
        "max": max(values) if values else None,
    }


def configured_control_qos_checks(
    qos_config: dict[str, Any], qos_profiles: dict[str, Any]
) -> dict[str, bool]:
    """Apply declared control limits to profiles configured to gate the run."""

    required_pdr = float(qos_config["classes"]["control"]["required_pdr"])
    maximum_p95_ms = float(qos_config["classes"]["control"]["max_p95_latency_ms"])
    checks: dict[str, bool] = {}
    for profile_name in PROFILE_NAMES:
        if not bool(
            qos_config["profiles"][profile_name]["gates_overall_status"]
        ):
            continue
        control = (
            qos_profiles.get(profile_name, {}).get("classes", {}).get("control", {})
        )
        p95 = control.get("latency_ms", {}).get("p95")
        checks[f"{profile_name}_control_required_pdr"] = (
            float(control.get("pdr", 0.0)) >= required_pdr
        )
        checks[f"{profile_name}_control_p95_latency"] = (
            isinstance(p95, (int, float)) and float(p95) <= maximum_p95_ms
        )
    return checks


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
    accepted_ingress_by_class: dict[str, set[tuple[int, str]]] = defaultdict(set)
    enqueue_by_uid: dict[tuple[int, str], deque[int]] = defaultdict(deque)
    latency_by_class: dict[str, list[float]] = defaultdict(list)
    queue_by_class: dict[str, list[float]] = defaultdict(list)
    queue_state: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "current_depth_packets": 0,
            "maximum_depth_packets": 0,
            "enqueued_packets": 0,
            "dequeued_packets": 0,
            "dropped_packets": 0,
            "deadline_drops": 0,
            "tail_drops": 0,
            "queue_delay_ms": [],
        }
    )
    egress_bytes: Counter[str] = Counter()
    radio_age_ms: list[float] = []
    scheduler_lag_ms: list[float] = []
    stale_states = 0
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
        if kind in {"ingress", "admit"} and not event.get("p2mp"):
            accepted_ingress_by_class[traffic_class].add(key)
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
        elif kind == "drop":
            reason = str(event.get("drop_reason") or "")
            age_ns = event.get("queue_age_ns")
            if (
                reason.startswith("deadline_drop")
                and isinstance(age_ns, int)
                and age_ns > 0
            ):
                queue_by_class[traffic_class].append(age_ns / 1e6)
        queue_id = str(event.get("queue_id") or "")
        if queue_id and kind in {"enqueue", "dequeue", "drop"}:
            state = queue_state[queue_id]
            depth = event.get("queue_depth_packets")
            if isinstance(depth, int) and depth >= 0:
                state["current_depth_packets"] = depth
                state["maximum_depth_packets"] = max(
                    int(state["maximum_depth_packets"]), depth
                )
            if kind == "enqueue":
                state["enqueued_packets"] += 1
            elif kind == "dequeue":
                state["dequeued_packets"] += 1
            else:
                state["dropped_packets"] += 1
                reason = str(event.get("drop_reason") or "")
                if reason.startswith("deadline_drop"):
                    state["deadline_drops"] += 1
                if reason.startswith(("queue_limit_", "aggregate_queue_limit")):
                    state["tail_drops"] += 1
            age_ns = event.get("queue_age_ns")
            reason = str(event.get("drop_reason") or "")
            if (
                kind == "dequeue"
                and isinstance(age_ns, int)
                and age_ns >= 0
            ) or (
                kind == "drop"
                and reason.startswith("deadline_drop")
                and isinstance(age_ns, int)
                and age_ns > 0
            ):
                state["queue_delay_ms"].append(age_ns / 1e6)
        age = event.get("radio_state_age_ns")
        if isinstance(age, int) and age >= 0:
            radio_age_ms.append(age / 1e6)
        lag = event.get("scheduler_lag_ns")
        if isinstance(lag, int):
            scheduler_lag_ms.append(lag / 1e6)
        radio_status = str(event.get("radio_state_status") or "")
        if any(token in radio_status for token in ("expired", "missing", "unavailable", "ipc_fault")):
            stale_states += 1

    rows: list[dict[str, Any]] = []
    summary: dict[str, Any] = {}
    for traffic_class in CLASSES:
        # Profile packets intentionally do not emit the expensive rich ingress
        # row before policy admission.  Their explicit admit row is therefore
        # the accepted-ingress observation; non-profile traffic keeps the
        # legacy ingress row.
        unicast_ingress = len(accepted_ingress_by_class[traffic_class])
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
            "unicast_ingress_basis": "legacy_ingress_plus_profile_admit",
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
            "queue_delay_sample_count": len(queue),
            "queue_delay_mean_ms": statistics.fmean(queue) if queue else None,
            "queue_delay_p95_ms": percentile(queue, 0.95),
            "queue_delay_p99_ms": percentile(queue, 0.99),
            "backoff_events": event_counts[(traffic_class, "backoff", False)]
            + event_counts[(traffic_class, "backoff", True)],
            "retry_events": event_counts[(traffic_class, "backoff", False)]
            + event_counts[(traffic_class, "backoff", True)],
        }
        rows.append(row)
        summary[traffic_class] = row
    summary["radio_state_age_ms"] = numeric_summary(radio_age_ms)
    summary["stale_channel_state_events"] = stale_states
    summary["scheduler_lag_ms"] = numeric_summary(scheduler_lag_ms)
    queues: dict[str, Any] = {}
    for queue_id, state in sorted(queue_state.items()):
        delays = state.pop("queue_delay_ms")
        queues[queue_id] = {
            **state,
            "average_queue_delay_ms": statistics.fmean(delays) if delays else None,
            "p95_queue_delay_ms": percentile(delays, 0.95),
            "p99_queue_delay_ms": percentile(delays, 0.99),
        }
    summary["queues"] = queues
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


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def aggregate_runtime_logs(run_dir: Path, events: list[dict[str, Any]]) -> None:
    uart_output = run_dir / "logs/uart_events.jsonl"
    with uart_output.open("w", encoding="utf-8") as output:
        for path in sorted((run_dir / "logs").glob("*_uart_uav*.jsonl")):
            for event in read_jsonl(path):
                event["source_log"] = path.name
                output.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
    with (run_dir / "logs/packet_events.jsonl").open("w", encoding="utf-8") as output:
        for event in events:
            output.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
    with (run_dir / "logs/queue_events.jsonl").open("w", encoding="utf-8") as output:
        for event in events:
            if event.get("event") in {"enqueue", "dequeue", "drop", "backoff"}:
                output.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")


def pcap_tos(data: bytes) -> int | None:
    if len(data) < 16:
        return None
    offset = 14
    ether_type = struct.unpack_from("!H", data, 12)[0]
    if ether_type == 0x8100 and len(data) >= 18:
        ether_type = struct.unpack_from("!H", data, 16)[0]
        offset = 18
    if ether_type != 0x0800 or len(data) < offset + 2:
        return None
    return data[offset + 1]


def split_pcaps(run_dir: Path, tos_by_class: dict[str, int]) -> list[dict[str, Any]]:
    source_paths = sorted((run_dir / "pcap").glob("ns3_packet_engine-radio-*.pcap"))
    outputs = {name: run_dir / "pcap" / f"{name}.pcap" for name in CLASSES}
    handles: dict[str, Any] = {}
    counts: Counter[str] = Counter()
    try:
        global_header: bytes | None = None
        endian = "<"
        for source in source_paths:
            with source.open("rb") as stream:
                header = stream.read(24)
                if len(header) != 24:
                    continue
                magic = header[:4]
                if magic in {b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1"}:
                    source_endian = "<"
                elif magic in {b"\xa1\xb2\xc3\xd4", b"\xa1\xb2\x3c\x4d"}:
                    source_endian = ">"
                else:
                    continue
                if global_header is None:
                    global_header = header
                    endian = source_endian
                    for name, path in outputs.items():
                        handles[name] = path.open("wb")
                        handles[name].write(global_header)
                if source_endian != endian:
                    continue
                while True:
                    record_header = stream.read(16)
                    if not record_header:
                        break
                    if len(record_header) != 16:
                        break
                    _sec, _fraction, captured, _wire = struct.unpack(
                        f"{source_endian}IIII", record_header
                    )
                    packet = stream.read(captured)
                    if len(packet) != captured:
                        break
                    tos = pcap_tos(packet)
                    for name, expected in tos_by_class.items():
                        if tos == expected:
                            handles[name].write(record_header)
                            handles[name].write(packet)
                            counts[name] += 1
                            break
        if global_header is None:
            for name, path in outputs.items():
                path.write_bytes(b"")
    finally:
        for handle in handles.values():
            handle.close()
    return [
        {
            "traffic_class": name,
            "path": str(outputs[name].relative_to(run_dir)),
            "packets": counts[name],
            "bytes": outputs[name].stat().st_size if outputs[name].exists() else 0,
        }
        for name in CLASSES
    ]


def jain_fairness(values: list[float]) -> float | None:
    if not values or sum(value * value for value in values) == 0:
        return None
    return sum(values) ** 2 / (len(values) * sum(value * value for value in values))


def profile_windows(path: Path) -> dict[str, tuple[int, int]]:
    starts: dict[str, int] = {}
    ends: dict[str, int] = {}
    for item in read_jsonl(path):
        profile = str(item.get("profile") or "")
        if item.get("event") == "profile_start":
            starts[profile] = int(item.get("scheduled_start_monotonic_ns", 0))
        elif item.get("event") == "profile_end":
            ends[profile] = int(item.get("monotonic_ns", 0))
    return {
        profile: (start, ends[profile])
        for profile, start in starts.items()
        if profile in ends and start < ends[profile]
    }


def profile_medium_metrics(
    events: list[dict[str, Any]],
    start_ns: int,
    end_ns: int,
    profile_id: int,
    resource_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    selected = [
        event
        for event in events
        if start_ns <= int(event.get("host_monotonic_ns", 0) or 0) <= end_ns
    ]
    profile_events = [
        event
        for event in selected
        if int(event.get("application_profile_id", -1) or -1) == profile_id
    ]
    starts: dict[tuple[int, str], int] = {}
    busy_ns = 0
    for event in selected:
        key = (int(event.get("packet_uid", -1)), str(event.get("device_id") or ""))
        observed = int(event.get("host_monotonic_ns", 0) or 0)
        if event.get("event") == "channel":
            starts[key] = observed
        elif event.get("event") == "phy_tx_end" and key in starts:
            busy_ns += max(0, observed - starts.pop(key))
    _rows, packet = packet_metrics(selected)
    class_queue_distributions = {
        name: {
            "count": int(packet.get(name, {}).get("queue_delay_sample_count", 0)),
            "mean": packet.get(name, {}).get("queue_delay_mean_ms"),
            "p95": packet.get(name, {}).get("queue_delay_p95_ms"),
            "p99": packet.get(name, {}).get("queue_delay_p99_ms"),
        }
        for name in CLASSES
    }
    cpu = [
        float(row["cpu_percent_one_core"])
        for row in resource_rows or []
        if row.get("component") == "ns3_packet_engine"
        and start_ns <= int(row.get("monotonic_ns", 0) or 0) <= end_ns
        and isinstance(row.get("cpu_percent_one_core"), (int, float))
    ]
    return {
        "channel_utilization": min(1.0, busy_ns / max(1, end_ns - start_ns)),
        "channel_busy_ms": busy_ns / 1e6,
        "profile_packet_events": len(profile_events),
        "backoff_events": sum(
            1 for event in profile_events if event.get("event") == "backoff"
        ),
        "retry_events": sum(
            1 for event in profile_events if event.get("event") == "backoff"
        ),
        "sionna_fresh_events": sum(
            1
            for event in profile_events
            if event.get("radio_state_status") == "fresh"
            and isinstance(event.get("radio_query_id"), str)
        ),
        "sionna_stale_events": sum(
            1
            for event in profile_events
            if str(event.get("radio_state_status") or "").startswith("stale")
        ),
        "queues": packet.get("queues", {}),
        "queue_delay_ms_by_class": class_queue_distributions,
        "ns3_cpu_percent_one_core": numeric_summary(cpu),
        "scheduler_lag_ms": packet.get("scheduler_lag_ms", {}),
    }


def resource_metrics(path: Path) -> dict[str, Any]:
    by_component: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in read_jsonl(path):
        by_component[str(item.get("component") or "unknown")].append(item)
    result: dict[str, Any] = {}
    for component, rows in sorted(by_component.items()):
        cpu = [float(row["cpu_percent_one_core"]) for row in rows if row.get("cpu_percent_one_core") is not None]
        rss = [float(row.get("rss_bytes", 0)) / (1024 * 1024) for row in rows]
        gpu = [float(row["gpu_memory_bytes"]) / (1024 * 1024) for row in rows if row.get("gpu_memory_bytes") is not None]
        result[component] = {
            "process_samples": len(rows),
            "cpu_percent_one_core": numeric_summary(cpu),
            "rss_mib": numeric_summary(rss),
            "gpu_memory_mib": numeric_summary(gpu),
        }
    return result


def uart_metrics(run_dir: Path) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for channel in ("control", "payload"):
        for uav_id in range(1, 6):
            key = f"{channel}:uav{uav_id}"
            result[key] = read_json(run_dir / f"metrics/{channel}_uart_uav{uav_id}.json")
    return result


def write_communication_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "dimension",
        "value",
        "packets_attempted",
        "packets_delivered_unique",
        "packets_dropped_unique",
        "packets_pending",
        "duplicate_deliveries",
        "queue_drop_events",
        "phy_drop_events",
        "backoff_events",
        "retry_events",
        "malformed_packets",
        "packet_invariant_holds",
        "uart_input_bytes",
        "ns3_input_bytes",
        "ns3_output_bytes",
        "uart_output_bytes",
    ]
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    metrics_dir = run_dir / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    qos_config = load_qos()
    gated_profile_names = tuple(
        name
        for name in PROFILE_NAMES
        if bool(qos_config["profiles"][name]["gates_overall_status"])
    )
    controlled_config = qos_config["profiles"]["controlled_overload"]
    scenario = read_json(metrics_dir / "scenario_summary.json")
    health = read_json(metrics_dir / "health.json")
    down = read_json(metrics_dir / "ns3_stopped_probe.json")
    topology = read_json(metrics_dir / "runtime_topology.json")
    gazebo = gazebo_metrics(run_dir / "logs/gazebo_stats.log")
    events = read_jsonl(run_dir / "logs/ns3_packet_events.jsonl")
    runtime_resource_rows = read_jsonl(run_dir / "logs/runtime_resources.jsonl")
    aggregate_runtime_logs(run_dir, events)
    packet_rows, packets = packet_metrics(events)
    with (metrics_dir / "packet_metrics.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(packet_rows[0]) if packet_rows else ["traffic_class"])
        writer.writeheader()
        writer.writerows(packet_rows)

    class_pcaps = split_pcaps(
        run_dir,
        {
            name: int(qos_config["classes"][name]["tos"])
            for name in CLASSES
        },
    )
    radio = radio_metrics(metrics_dir / "radio_links.csv")
    ns3_records = read_jsonl(run_dir / "logs/ns3_packet_engine.log")
    ns3_runtime = ns3_records[-1] if ns3_records else {}
    engine_config = read_json(run_dir / "logs/ns3_packet_engine_config.json")
    engine_resolved = engine_config.get("resolved", {})
    try:
        radio_capacity_bps = data_rate_bps(str(engine_resolved["radio_rate"]))
    except (KeyError, TypeError, ConfigError):
        radio_capacity_bps = None
    attempts = read_jsonl(run_dir / "logs/communication_attempts.jsonl")
    deliveries = read_jsonl(run_dir / "logs/communication_deliveries.jsonl")
    foreign_serial_datagrams = sum(
        1
        for item in deliveries
        if item.get("background_serial_datagram")
        or (item.get("malformed") and item.get("error") == "profile magic/version mismatch")
    )
    accounting_deliveries = [
        item
        for item in deliveries
        if not (
            item.get("background_serial_datagram")
            or (
                item.get("malformed")
                and item.get("error") == "profile magic/version mismatch"
                and not item.get("packet_id")
            )
        )
    ]
    accounting = account_packets(
        attempts, accounting_deliveries, events, finalize_pending=True
    )
    grouped = group_accounting(
        attempts,
        accounting_deliveries,
        events,
        ("profile", "traffic_class", "uav", "direction"),
        finalize_pending=True,
    )
    traffic_profiles = read_json(metrics_dir / "traffic_profiles.json")
    windows = profile_windows(run_dir / "logs/profile_windows.jsonl")
    qos_profiles: dict[str, Any] = {}
    fairness: dict[str, Any] = {}
    for profile_name in gated_profile_names:
        snapshot = traffic_profiles.get(profile_name, {})
        classes = snapshot.get("classes", {})
        medium = (
            profile_medium_metrics(
                events,
                *windows[profile_name],
                PROFILE_NAMES.index(profile_name) + 1,
                runtime_resource_rows,
            )
            if profile_name in windows
            else {}
        )
        qos_profiles[profile_name] = {
            "classes": classes,
            "medium": medium,
            "shaping_enabled": snapshot.get("shaping_enabled"),
            "gates_overall_status": snapshot.get("gates_overall_status"),
            "offered": snapshot.get("offered", {}),
            "admitted_policy_observed": snapshot.get(
                "admitted_policy_observed", {}
            ),
            "queue_enqueued_observed": snapshot.get(
                "queue_enqueued_observed", {}
            ),
            "terminal_accounting": snapshot.get("terminal_accounting", {}),
            "packets_pending": int(snapshot.get("packets_pending", -1)),
            "offered_load_bps": sum(
                int(qos_config["profiles"][profile_name][name]["packets_per_second_per_uav"])
                * int(qos_config["profiles"][profile_name][name]["packet_bytes"])
                * 8
                * 5
                for name in CLASSES
            ),
        }
        if profile_name in {"nominal", "contention", "controlled_overload"}:
            fairness[profile_name] = {
                name: jain_fairness(
                    [
                        float(classes.get(name, {}).get("per_uav_delivered_unique", {}).get(f"uav{uav_id}", 0))
                        for uav_id in range(1, 6)
                    ]
                )
                for name in CLASSES
            }

    controlled = qos_profiles.get("controlled_overload", {})
    controlled_classes = controlled.get("classes", {})
    controlled_control = controlled_classes.get("control", {})
    controlled_control_per_uav = controlled_control.get(
        "per_uav_delivered_unique", {}
    )
    controlled_lag_p95 = controlled.get("medium", {}).get(
        "scheduler_lag_ms", {}
    ).get("p95")
    controlled_cpu_samples = controlled.get("medium", {}).get(
        "ns3_cpu_percent_one_core", {}
    ).get("count")
    controlled_queue_distributions = controlled.get("medium", {}).get(
        "queue_delay_ms_by_class", {}
    )
    controlled_offered = int(controlled.get("offered", {}).get("packets", 0) or 0)
    controlled_admitted = int(
        controlled.get("admitted_policy_observed", {}).get("packets", 0) or 0
    )
    controlled_offered_bps = controlled.get("offered", {}).get("bits_per_second")
    nominal_offered = int(
        qos_profiles.get("nominal", {}).get("offered", {}).get("packets", 0) or 0
    )
    contention_offered = int(
        qos_profiles.get("contention", {}).get("offered", {}).get("packets", 0)
        or 0
    )
    controlled_terminal = controlled.get("terminal_accounting", {})
    qos_checks = {
        **configured_control_qos_checks(qos_config, qos_profiles),
        "controlled_overload_no_control_starvation": all(
            int(controlled_control_per_uav.get(f"uav{uav_id}", 0)) > 0
            for uav_id in range(1, 6)
        ),
        "controlled_overload_lower_classes_served": all(
            int(
                controlled_classes.get(name, {})
                .get("per_uav_delivered_unique", {})
                .get(f"uav{uav_id}", 0)
            )
            > 0
            for name in ("payload", "additional_data")
            for uav_id in range(1, 6)
        ),
        "controlled_overload_scheduler_lag_p95": isinstance(
            controlled_lag_p95, (int, float)
        )
        and float(controlled_lag_p95)
        <= float(controlled_config["max_scheduler_lag_p95_ms"]),
        "controlled_overload_cpu_samples_present": isinstance(
            controlled_cpu_samples, int
        )
        and controlled_cpu_samples > 0,
        "controlled_overload_queue_delay_samples_present": all(
            isinstance(
                controlled_queue_distributions.get(name, {}).get("count"), int
            )
            and int(controlled_queue_distributions[name]["count"]) > 0
            for name in CLASSES
        ),
        "controlled_overload_pending_zero": int(
            qos_profiles.get("controlled_overload", {}).get("packets_pending", -1)
        )
        == 0,
        "controlled_overload_shaping_observed": (
            controlled.get("shaping_enabled") is True
            and controlled_offered > controlled_admitted > 0
            and int(controlled_terminal.get("dropped_at_ingress", 0) or 0) > 0
        ),
        "controlled_overload_measured_offer_above_capacity": (
            isinstance(controlled_offered_bps, (int, float))
            and isinstance(radio_capacity_bps, int)
            and float(controlled_offered_bps) > radio_capacity_bps
        ),
        "all_gated_profiles_terminal": all(
            int(profile.get("packets_pending", -1)) == 0
            and bool(profile.get("terminal_accounting", {}).get("invariant_holds"))
            for profile in qos_profiles.values()
        ),
        "controlled_overload_sionna_coupled": (
            int(controlled.get("medium", {}).get("sionna_fresh_events", 0) or 0)
            > 0
            and int(controlled.get("medium", {}).get("sionna_stale_events", 0) or 0)
            == 0
        ),
        "profile_gating_matches_config": all(
            isinstance(
                qos_profiles.get(name, {}).get("gates_overall_status"), bool
            )
            and qos_profiles[name]["gates_overall_status"]
            == bool(qos_config["profiles"][name]["gates_overall_status"])
            for name in gated_profile_names
        ),
        "lower_classes_served_when_capacity_exists": all(
            int(qos_profiles.get("nominal", {}).get("classes", {}).get(name, {}).get("packets_delivered_unique", 0))
            > 0
            for name in ("payload", "additional_data")
        ),
        # The product MAC arbiter is expected to remove avoidable native CSMA
        # collisions.  Exercise the higher-load profile without requiring a
        # backoff event that would contradict that scheduler behavior.
        "contention_profile_exercised": (
            contention_offered > nominal_offered > 0
            and int(
                qos_profiles.get("contention", {})
                .get("medium", {})
                .get("profile_packet_events", 0)
                or 0
            )
            > 0
        ),
    }
    qos_summary = {
        "config": {
            "path": "network/config/communication_qos.yaml",
            "classes": qos_config["classes"],
            "scheduler": qos_config["scheduler"],
            "protection": qos_config["protection"],
            "profile_gating": {
                name: bool(qos_config["profiles"][name]["gates_overall_status"])
                for name in PROFILE_NAMES
            },
            "controlled_overload_acceptance": {
                "control_required_pdr": float(
                    qos_config["classes"]["control"]["required_pdr"]
                ),
                "control_max_p95_latency_ms": float(
                    qos_config["classes"]["control"]["max_p95_latency_ms"]
                ),
                "scheduler_lag_max_p95_ms": float(
                    controlled_config["max_scheduler_lag_p95_ms"]
                ),
                "profile_rtf_status": "unmeasured",
                "cpu_sample_distribution_required": True,
                "queue_delay_sample_distribution_required_for_classes": list(
                    CLASSES
                ),
            },
        },
        "profiles": qos_profiles,
        "fairness_jain_throughput": fairness,
        "checks": qos_checks,
        "status": "passed" if all(qos_checks.values()) else "failed",
    }
    write_json(metrics_dir / "qos_summary.json", qos_summary)

    uarts = uart_metrics(run_dir)
    uart_age_values = [
        float(item.get("average_ingress_queue_age_ms", 0.0))
        for item in uarts.values()
        if item
    ]
    control_delivery_latency = [
        float(item["latency_ms"])
        for item in deliveries
        if not item.get("malformed")
        and item.get("traffic_class") == "control"
        and isinstance(item.get("latency_ms"), (int, float))
    ]
    real_ack_latency = [
        float(item["latency_ms"])
        for item in scenario.get("command_acks", [])
        if item.get("channel") == "control" and isinstance(item.get("latency_ms"), (int, float))
    ]
    safe_ack_latency = [
        float(item["latency_ms"])
        for item in scenario.get("command_acks", [])
        if item.get("channel") == "control"
        and item.get("label")
        in {
            "control_autopilot_version_diagnostic",
            "parallel_five_uav_safe_request",
            "control_local_position_interval",
        }
        and isinstance(item.get("latency_ms"), (int, float))
    ]
    realtime = {
        "gazebo": {
            "real_time_factor": gazebo.get("real_time_factor", {})
        },
        "sionna_query_latency_ms": radio.get("provider_latency_ms", {}),
        "channel_state_age_ms": packets.get("radio_state_age_ms", {}),
        "stale_channel_state_count": packets.get("stale_channel_state_events", 0),
        "ns3_scheduler_lag_ms": packets.get("scheduler_lag_ms", {}),
        "uart_ingress_queue_age_ms": numeric_summary(uart_age_values),
        "control_end_to_end_latency_ms": numeric_summary(control_delivery_latency),
        "real_control_ack_latency_ms": numeric_summary(real_ack_latency),
        "safe_control_ack_latency_ms": numeric_summary(safe_ack_latency),
        "resources": resource_metrics(run_dir / "logs/runtime_resources.jsonl"),
    }
    write_json(metrics_dir / "realtime_summary.json", realtime)

    scenario_passed = scenario.get("status") == "passed" and all(
        all(
            bool(scenario.get("uavs", {}).get(f"uav{index}", {}).get("phases", {}).get(phase))
            for phase in ("heartbeat", "arm", "takeoff", "hold", "movement", "land")
        )
        for index in range(1, 6)
    )
    health_passed = health.get("status") in {"healthy", "passed"}
    no_bypass = (
        bool(down.get("exchange_stopped"))
        and int(down.get("received_datagrams", -1)) == 0
        and topology.get("gcs_direct_sitl_ports_present") is False
    )
    message_counts = scenario.get("message_counts", {})
    dual_uart_telemetry = all(
        int(message_counts.get(f"control:uav{index}:LOCAL_POSITION_NED", 0)) > 0
        and int(message_counts.get(f"payload:uav{index}:ATTITUDE", 0)) > 0
        for index in range(1, 6)
    )
    dual_uart_diagnostics = scenario.get("dual_uart_diagnostics", {}).get("sequential", {})
    dual_uart = (
        dual_uart_telemetry
        and topology.get("uart_path_count") == 10
        and topology.get("all_uart_paths_independent") is True
        and all(
            dual_uart_diagnostics.get(f"uav{index}", {}).get("control", {}).get("ack_from_system_id") == index
            and dual_uart_diagnostics.get(f"uav{index}", {}).get("payload", {}).get("ack_from_system_id") == index
            and dual_uart_diagnostics.get(f"uav{index}", {}).get("payload", {}).get("matching_control_ack_observed") is False
            for index in range(1, 6)
        )
    )
    additional = scenario.get("additional_data", {})
    additional_data = (
        int(additional.get("p2p", {}).get("downlink_delivered_unique", 0)) == 10
        and int(additional.get("p2p", {}).get("uplink_delivered_unique", 0)) == 10
        and additional.get("p2mp_receivers") == [f"uav{index}" for index in range(1, 6)]
        and len(additional.get("p2mp_per_receiver_deliveries", {})) == 5
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
    pcap_passed = all(item["packets"] > 0 and item["bytes"] > 24 for item in class_pcaps)
    profiles_passed = set(traffic_profiles) == set(gated_profile_names)
    components = {
        "gazebo_town01": health_passed,
        "ardupilot_sitl_count": len(health.get("sitl", [])),
        "ros_odometry": health_passed,
        "sionna_rt": sionna_passed,
        "ns3_tap_packet_engine": ns3_passed,
        "dual_uart": dual_uart,
        "additional_data": additional_data,
        "communication_profiles": profiles_passed,
        "qos": qos_summary["status"] == "passed",
        "packet_accounting": (
            accounting["packet_invariant_holds"]
            and accounting["all_packets_terminal"]
            and accounting["packets_pending"] == 0
        ),
        "class_pcaps": pcap_passed,
    }
    overall_passed = (
        scenario_passed
        and health_passed
        and no_bypass
        and profiles_passed
        and pcap_passed
        and qos_summary["status"] == "passed"
        and bool(accounting["packet_invariant_holds"])
        and bool(accounting["all_packets_terminal"])
        and accounting["packets_pending"] == 0
        and all(
            bool(value)
            for key, value in components.items()
            if key != "ardupilot_sitl_count"
        )
        and components["ardupilot_sitl_count"] == 5
    )
    communication_rows: list[dict[str, Any]] = []
    for compound, values in sorted(grouped.items()):
        dimension, value = compound.split(":", 1)
        communication_rows.append({"dimension": dimension, "value": value, **values})
    for uart_name, values in sorted(uarts.items()):
        communication_rows.append({"dimension": "uart", "value": uart_name, **values})
    write_communication_csv(metrics_dir / "communication_summary.csv", communication_rows)
    communication = {
        "run_id": run_dir.name,
        "status": "passed" if overall_passed else "failed",
        "by_uav": scenario.get("dual_uart_diagnostics", {}).get("sequential", {}),
        "by_uart": uarts,
        "by_traffic_class": {
            key.split(":", 1)[1]: value
            for key, value in grouped.items()
            if key.startswith("traffic_class:")
        },
        "by_direction": {
            key.split(":", 1)[1]: value
            for key, value in grouped.items()
            if key.startswith("direction:")
        },
        "by_profile": {
            key.split(":", 1)[1]: value
            for key, value in grouped.items()
            if key.startswith("profile:")
        },
        "profiles": traffic_profiles,
        "logical_packet_accounting": accounting,
        "foreign_serial_datagrams_ignored_by_profile_accounting": foreign_serial_datagrams,
        "additional_data": additional,
        "runtime_topology": "metrics/runtime_topology.json",
        "no_bypass": {
            "passed": no_bypass,
            "gcs_direct_sitl_ports_present": topology.get("gcs_direct_sitl_ports_present"),
            "ns3_stop_probe": down,
        },
    }
    write_json(metrics_dir / "communication_summary.json", communication)

    summary = {
        "run_id": run_dir.name,
        "status": "passed" if overall_passed else "failed",
        "scenario": "scenario_5uav_town01",
        "uav_count": 5,
        "components": components,
        "flight": scenario,
        "communication": communication,
        "packet_path": packets,
        "qos": qos_summary,
        "realtime": realtime,
        "radio": radio,
        "ns3_runtime": ns3_runtime,
        "gazebo": gazebo,
        "no_bypass": {
            "ns3_stop_breaks_control_exchange": no_bypass,
            "probe": down,
        },
        "pcaps": class_pcaps,
        "known_limits": [
            "Town01 is 3.191 km by 3.191 km and does not satisfy the separate 10 km by 10 km requirement.",
            "Gazebo uses source-coordinate visual meshes with axis-aligned surface and building collision-box approximations.",
            "The Gazebo derivative omits vegetation visuals and vegetation collisions for runtime cost.",
            "The ns-3 packet engine uses the declared CSMA shared-medium engineering surrogate, not a customer modem waveform.",
        ],
    }
    write_json(metrics_dir / "summary.json", summary)
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
    profile_lines = []
    for profile_name in traffic_profiles:
        classes = qos_profiles.get(profile_name, {}).get("classes", {})
        profile_lines.append(
            "| {profile} | {cpdr:.6f} | {ppdr:.6f} | {apdr:.6f} | {c95} | {p95} | {backoff} | {util:.6f} |".format(
                profile=profile_name,
                cpdr=float(classes.get("control", {}).get("pdr", 0.0)),
                ppdr=float(classes.get("payload", {}).get("pdr", 0.0)),
                apdr=float(classes.get("additional_data", {}).get("pdr", 0.0)),
                c95=classes.get("control", {}).get("latency_ms", {}).get("p95"),
                p95=classes.get("payload", {}).get("latency_ms", {}).get("p95"),
                backoff=qos_profiles.get(profile_name, {}).get("medium", {}).get("backoff_events", 0),
                util=float(qos_profiles.get(profile_name, {}).get("medium", {}).get("channel_utilization", 0.0)),
            )
        )
    report = f"""# Town01 five-UAV communication run

- Run: `{run_dir.name}`
- Result: **{summary['status']}**
- Flight lifecycle: `{scenario.get('status', 'missing')}` for five UAVs
- Real Sionna queries: `{summary['radio'].get('query_count', 0)}`; scene `{summary['radio'].get('scene_id', 'missing')}`
- Ten independent UART paths: `{topology.get('all_uart_paths_independent')}`
- ns-3 UDP events: `{packets.get('udp_event_count', 0)}`; class PCAP files: `{len(class_pcaps)}`
- ns-3 stop broke the control exchange: `{str(no_bypass).lower()}`
- Gazebo RTF mean: `{summary['gazebo'].get('real_time_factor', {}).get('mean')}`
- Packet invariant: `{accounting.get('packets_attempted')} = {accounting.get('packets_delivered_unique')} + {accounting.get('packets_dropped_unique')} + {accounting.get('packets_pending')}`

| Traffic class | Accepted unicast ingress/egress | PDR | Delivered goodput (bit/s) | Mean latency (ms) | P95 latency (ms) | P95 queue (ms) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
{chr(10).join(packet_lines)}

| Profile | Control PDR | Payload PDR | Additional PDR | Control p95 ms | Payload p95 ms | Backoff events | Channel utilization |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
{chr(10).join(profile_lines)}

The checksummed additional-data path completed bidirectional P2P with uav3 and
one logical P2MP delivery to five separately counted receivers. The live radio
updater made {summary['radio'].get('query_count', 0)} real-Sionna queries.

This is a factual Town01 development run. It does not close the 10 km by 10 km
map requirement. Gazebo collision geometry is approximated with axis-aligned
boxes, and the shared medium remains the documented CSMA surrogate.
"""
    (run_dir / "report.md").write_text(report, encoding="utf-8")
    print(json.dumps(summary, sort_keys=True))
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
