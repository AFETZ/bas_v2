#!/usr/bin/env python3
"""Independently recompute Town01 communication evidence from raw runtime logs."""

from __future__ import annotations

import argparse
import binascii
import csv
import json
import math
import re
import statistics
import struct
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Iterator

import yaml


PROFILES = ("nominal", "contention", "overload")
CLASSES = ("control", "payload", "additional_data")
UAVS = tuple(f"uav{index}" for index in range(1, 6))
BSF1_HEADER_BYTES = 38
BSF1_HEADER = struct.Struct("!4sBBBBIHHHIQII")
QUEUE_DROP_PREFIXES = ("queue_", "aggregate_queue", "deadline_drop")


def iter_jsonl(path: Path, parse_errors: Counter[str]) -> Iterator[dict[str, Any]]:
    try:
        stream = path.open(encoding="utf-8", errors="replace")
    except OSError:
        parse_errors[str(path)] += 1
        return
    with stream:
        for line in stream:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                parse_errors[str(path)] += 1
                continue
            if isinstance(value, dict):
                yield value
            else:
                parse_errors[str(path)] += 1


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def percentile(values: Iterable[float], fraction: float) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    return ordered[max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))]


def numeric(values: Iterable[float]) -> dict[str, float | int | None]:
    materialized = list(values)
    return {
        "count": len(materialized),
        "min": min(materialized) if materialized else None,
        "mean": statistics.fmean(materialized) if materialized else None,
        "p50": percentile(materialized, 0.50),
        "p95": percentile(materialized, 0.95),
        "p99": percentile(materialized, 0.99),
        "max": max(materialized) if materialized else None,
    }


def jain_fairness(values: Iterable[int]) -> float | None:
    samples = list(values)
    denominator = len(samples) * sum(value * value for value in samples)
    return (sum(samples) ** 2 / denominator) if denominator else None


def gazebo_realtime(path: Path) -> dict[str, Any]:
    factors: list[float] = []
    try:
        stream = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return numeric(factors)
    with stream:
        for line in stream:
            match = re.search(r"real_time_factor:\s*([0-9.eE+-]+)", line)
            if match:
                factors.append(float(match.group(1)))
    return numeric(factors)


def read_environment(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return result
    for line in lines:
        key, separator, value = line.partition("=")
        if separator:
            result[key] = value
    return result


def read_windows(path: Path, parse_errors: Counter[str]) -> dict[str, tuple[int, int]]:
    starts: dict[str, int] = {}
    ends: dict[str, int] = {}
    for row in iter_jsonl(path, parse_errors):
        profile = str(row.get("profile") or "")
        if row.get("event") == "profile_start":
            starts[profile] = int(row.get("scheduled_start_monotonic_ns", 0) or 0)
        elif row.get("event") == "profile_end":
            ends[profile] = int(row.get("monotonic_ns", 0) or 0)
    return {
        profile: (starts[profile], ends[profile])
        for profile in PROFILES
        if profile in starts and profile in ends and starts[profile] < ends[profile]
    }


def event_profile(timestamp_ns: int, windows: dict[str, tuple[int, int]]) -> str | None:
    for profile, (start, end) in windows.items():
        if start <= timestamp_ns <= end:
            return profile
    return None


def drop_kind(reason: str) -> str:
    return "queue" if reason.startswith(QUEUE_DROP_PREFIXES) else "PHY"


def packet_direction(event: dict[str, Any]) -> str:
    link = str(event.get("directed_link") or "")
    if link.startswith("uav") and link.endswith(">cp"):
        return "uplink"
    if link.startswith("cp>uav"):
        return "downlink"
    return "other"


def uart_route(event: dict[str, Any]) -> tuple[str, str, str] | None:
    if event.get("transport_protocol") != 17:
        return None
    source = str(event.get("source_ip") or "")
    destination = str(event.get("destination_ip") or "")
    source_port = event.get("source_udp_port")
    destination_port = event.get("destination_udp_port")
    for index in range(1, 6):
        uav = f"uav{index}"
        uav_ip = f"10.71.{index}.10"
        for channel, base in (("control", 14600), ("payload", 14700)):
            if (
                source == uav_ip
                and destination == "10.71.0.10"
                and source_port == base + index
                and destination_port == base
            ):
                return channel, uav, "uart_to_gcs"
            if (
                source == "10.71.0.10"
                and destination == uav_ip
                and source_port == base
                and destination_port == base + index
            ):
                return channel, uav, "gcs_to_uart"
    return None


def new_packet_state() -> dict[str, Any]:
    return {
        "delivery_count": 0,
        "latencies_ms": [],
        "events": Counter(),
        "event_first_ns": {},
        "event_last_ns": {},
        "drop_reasons": Counter(),
        "queue_drop_events": 0,
        "phy_drop_events": 0,
        "backoff_events": 0,
        "retry_events": 0,
        "last_event": None,
        "last_event_monotonic_ns": None,
    }


def load_logical_packets(
    run_dir: Path, parse_errors: Counter[str]
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, set[str]],
    Counter[tuple[str, str]],
    Counter[tuple[str, str]],
]:
    packets: dict[str, dict[str, Any]] = {}
    hash_to_packets: dict[str, set[str]] = defaultdict(set)
    for row in iter_jsonl(run_dir / "logs/communication_attempts.jsonl", parse_errors):
        packet_id = row.get("packet_id")
        if not isinstance(packet_id, str) or not packet_id or packet_id in packets:
            parse_errors["attempt_record"] += 1
            continue
        state = new_packet_state()
        state.update(row)
        packets[packet_id] = state
        hashes = row.get("fragment_hashes")
        if isinstance(hashes, list):
            for digest in hashes:
                if isinstance(digest, str) and digest:
                    hash_to_packets[digest].add(packet_id)

    malformed: Counter[tuple[str, str]] = Counter()
    background: Counter[tuple[str, str]] = Counter()
    for row in iter_jsonl(run_dir / "logs/communication_deliveries.jsonl", parse_errors):
        cell = (str(row.get("profile") or "unknown"), str(row.get("traffic_class") or "unknown"))
        if row.get("background_serial_datagram") or (
            row.get("malformed")
            and row.get("error") == "profile magic/version mismatch"
            and not row.get("packet_id")
        ):
            background[cell] += 1
            continue
        if row.get("malformed"):
            malformed[cell] += 1
            continue
        packet_id = row.get("packet_id")
        state = packets.get(packet_id) if isinstance(packet_id, str) else None
        if state is None:
            malformed[cell] += 1
            continue
        state["delivery_count"] += 1
        latency = row.get("latency_ms")
        if isinstance(latency, (int, float)):
            state["latencies_ms"].append(float(latency))
    return packets, hash_to_packets, malformed, background


def new_medium_state() -> dict[str, Any]:
    return {
        "actual_ingress_packets": 0,
        "actual_ingress_bytes": 0,
        "logical_test_ingress_packets": 0,
        "logical_test_ingress_bytes": 0,
        "background_ingress_packets": 0,
        "background_ingress_bytes": 0,
        "egress_packets": 0,
        "egress_bytes": 0,
        "queue_drop_events": 0,
        "PHY_drop_events": 0,
        "backoff_events": 0,
        "retry_events": 0,
        "queue_depth_ratios": [],
        "maximum_queue_depth_packets": 0,
        "channel_busy_ns": 0,
    }


def new_uart_network_state() -> dict[str, Any]:
    return {
        "offered_sizes": {},
        "delivered_sizes": {},
        "offered_counts": Counter(),
        "delivered_counts": Counter(),
        "drop_reasons": Counter(),
    }


def scan_packet_events(
    run_dir: Path,
    packets: dict[str, dict[str, Any]],
    hash_to_packets: dict[str, set[str]],
    windows: dict[str, tuple[int, int]],
    parse_errors: Counter[str],
) -> tuple[
    dict[tuple[str, str, str], dict[str, Any]],
    dict[tuple[str, str, str], dict[str, Any]],
    dict[tuple[str, str, str], dict[str, Any]],
    dict[str, Any],
]:
    medium: dict[tuple[str, str, str], dict[str, Any]] = defaultdict(new_medium_state)
    uart_network: dict[tuple[str, str, str], dict[str, Any]] = defaultdict(new_uart_network_state)
    channel_starts: dict[tuple[int, str], tuple[int, tuple[str, str, str]]] = {}
    sionna_cells: dict[tuple[str, str, str], dict[str, Any]] = defaultdict(
        lambda: {
            "first_ns3_applied_monotonic_ns": None,
            "state_ages_ms": [],
            "packet_uids": set(),
            "state_ids": set(),
        }
    )
    sionna_status_events: Counter[str] = Counter()
    stale_packets: set[tuple[int, str]] = set()
    state_uses: Counter[str] = Counter()
    seen_state_packets: set[tuple[str, int, str]] = set()
    scheduler_lag_ms: list[float] = []
    event_path = run_dir / "logs/ns3_packet_events.jsonl"
    if not event_path.is_file():
        event_path = run_dir / "logs/packet_events.jsonl"

    for event in iter_jsonl(event_path, parse_errors):
        name = str(event.get("event") or "")
        timestamp = int(event.get("host_monotonic_ns", 0) or 0)
        digest = event.get("transport_payload_sha256")
        matching = hash_to_packets.get(digest, ()) if isinstance(digest, str) else ()
        scheduler_lag = event.get("scheduler_lag_ns")
        if isinstance(scheduler_lag, int):
            scheduler_lag_ms.append(scheduler_lag / 1e6)
        for packet_id in matching:
            state = packets[packet_id]
            state["events"][name] += 1
            state["event_first_ns"].setdefault(name, timestamp)
            state["event_last_ns"][name] = timestamp
            state["last_event"] = name
            state["last_event_monotonic_ns"] = timestamp
            if name == "backoff":
                state["backoff_events"] += 1
                state["retry_events"] += 1
            elif name == "retry":
                state["retry_events"] += 1
            elif name == "drop":
                reason = str(event.get("drop_reason") or "unknown")
                state["drop_reasons"][reason] += 1
                if drop_kind(reason) == "queue":
                    state["queue_drop_events"] += 1
                else:
                    state["phy_drop_events"] += 1

        profile = event_profile(timestamp, windows)
        traffic_class = str(event.get("traffic_class") or "unknown")
        if profile and traffic_class in CLASSES and event.get("transport_protocol") == 17:
            direction = packet_direction(event)
            bucket = medium[(profile, traffic_class, direction)]
            size = int(event.get("transport_payload_size", 0) or 0)
            if name == "ingress":
                bucket["actual_ingress_packets"] += 1
                bucket["actual_ingress_bytes"] += size
                logical = any(packets[item].get("profile") == profile for item in matching)
                prefix = "logical_test" if logical else "background"
                bucket[f"{prefix}_ingress_packets"] += 1
                bucket[f"{prefix}_ingress_bytes"] += size
            elif name == "egress":
                bucket["egress_packets"] += 1
                bucket["egress_bytes"] += size
            elif name == "backoff":
                bucket["backoff_events"] += 1
                bucket["retry_events"] += 1
            elif name == "retry":
                bucket["retry_events"] += 1
            elif name == "drop":
                reason = str(event.get("drop_reason") or "unknown")
                bucket[f"{drop_kind(reason)}_drop_events"] += 1
            if name in {"enqueue", "dequeue", "drop"}:
                depth = event.get("queue_depth_packets")
                limit = event.get("queue_limit_packets")
                if isinstance(depth, int) and depth >= 0:
                    bucket["maximum_queue_depth_packets"] = max(
                        bucket["maximum_queue_depth_packets"], depth
                    )
                    if isinstance(limit, int) and limit > 0:
                        bucket["queue_depth_ratios"].append(depth / limit)
            channel_key = (int(event.get("packet_uid", -1)), str(event.get("device_id") or ""))
            if name == "channel":
                channel_starts[channel_key] = (timestamp, (profile, traffic_class, direction))
            elif name == "phy_tx_end" and channel_key in channel_starts:
                started, start_bucket = channel_starts.pop(channel_key)
                if start_bucket == (profile, traffic_class, direction):
                    medium[start_bucket]["channel_busy_ns"] += max(0, timestamp - started)

        route = uart_route(event)
        if route and isinstance(digest, str):
            bucket = uart_network[route]
            size = int(event.get("transport_payload_size", 0) or 0)
            if name == "ingress":
                bucket["offered_sizes"].setdefault(digest, size)
                bucket["offered_counts"][digest] += 1
            elif name == "egress":
                bucket["delivered_sizes"].setdefault(digest, size)
                bucket["delivered_counts"][digest] += 1
            elif name == "drop":
                bucket["drop_reasons"][str(event.get("drop_reason") or "unknown")] += 1

        query_id = event.get("radio_query_id")
        link = str(event.get("directed_link") or "")
        if isinstance(query_id, str) and query_id:
            traffic_class = str(event.get("traffic_class") or "")
            cell = sionna_cells[(query_id, link, traffic_class)]
            applied = event.get("radio_rate_applied_at_monotonic_ns")
            if isinstance(applied, int) and applied > 0:
                current = cell["first_ns3_applied_monotonic_ns"]
                cell["first_ns3_applied_monotonic_ns"] = applied if current is None else min(current, applied)
            age = event.get("radio_state_age_ns")
            if isinstance(age, int) and age >= 0:
                cell["state_ages_ms"].append(age / 1e6)
            uid = int(event.get("packet_uid", -1))
            state_id = event.get("radio_applied_state_id")
            cell["packet_uids"].add(uid)
            if isinstance(state_id, str) and state_id:
                cell["state_ids"].add(state_id)
                use_key = (state_id, uid, link)
                if use_key not in seen_state_packets:
                    seen_state_packets.add(use_key)
                    state_uses[state_id] += 1
        status = str(event.get("radio_state_status") or "")
        if status:
            sionna_status_events[status] += 1
            if any(word in status for word in ("expired", "missing", "unavailable", "ipc_fault")):
                stale_packets.add((int(event.get("packet_uid", -1)), link))

    sionna = {
        "cells": sionna_cells,
        "status_events": sionna_status_events,
        "stale_unique_packets": len(stale_packets),
        "used_state_count": len(state_uses),
        "reused_state_count": sum(1 for count in state_uses.values() if count > 1),
        "reused_state_packet_uses": sum(max(0, count - 1) for count in state_uses.values()),
        "scheduler_lag_ms": numeric(scheduler_lag_ms),
    }
    return medium, uart_network, sionna_cells, sionna


def classify_packets(packets: dict[str, dict[str, Any]]) -> Counter[str]:
    totals: Counter[str] = Counter()
    for state in packets.values():
        if state["delivery_count"]:
            status = "delivered"
        elif state["drop_reasons"]:
            status = "dropped"
        else:
            status = "pending"
        state["audit_status"] = status
        totals[status] += 1
        totals["duplicates"] += max(0, int(state["delivery_count"]) - 1)
    return totals


def pending_reason(state: dict[str, Any]) -> str:
    events: Counter[str] = state["events"]
    if not events:
        return "no_ns3_observation"
    if events["egress"]:
        return "ns3_egress_without_application_delivery"
    if events["phy_tx_end"]:
        return "phy_tx_end_without_endpoint_egress"
    if events["channel"]:
        return "channel_without_endpoint_egress"
    if events["dequeue"]:
        return "dequeued_without_channel"
    if events["enqueue"]:
        return "queued_without_terminal_event"
    if events["ingress"]:
        return "ingress_without_enqueue"
    return "other_nonterminal_observation"


def accounting_result(
    selected: Iterable[dict[str, Any]], malformed: int = 0
) -> dict[str, Any]:
    rows = list(selected)
    attempted = len(rows)
    delivered = sum(row["audit_status"] == "delivered" for row in rows)
    dropped = sum(row["audit_status"] == "dropped" for row in rows)
    pending = sum(row["audit_status"] == "pending" for row in rows)
    latencies = [value for row in rows for value in row["latencies_ms"]]
    return {
        "packets_attempted": attempted,
        "packets_delivered_unique": delivered,
        "packets_dropped_unique": dropped,
        "packets_pending": pending,
        "duplicates": sum(max(0, int(row["delivery_count"]) - 1) for row in rows),
        "malformed": malformed,
        "queue_drop_events": sum(int(row["queue_drop_events"]) for row in rows),
        "PHY_drop_events": sum(int(row["phy_drop_events"]) for row in rows),
        "backoff_events": sum(int(row["backoff_events"]) for row in rows),
        "retry_events": sum(int(row["retry_events"]) for row in rows),
        "pdr": delivered / attempted if attempted else None,
        "latency_ms": numeric(latencies),
        "invariant_holds": attempted == delivered + dropped + pending,
    }


def grouped_accounting(
    packets: dict[str, dict[str, Any]], malformed: Counter[tuple[str, str]]
) -> dict[str, Any]:
    rows = list(packets.values())
    result: dict[str, Any] = {
        "all": accounting_result(rows, sum(malformed.values())),
    }
    dimensions = {
        "profile": PROFILES,
        "uav": UAVS,
        "channel": CLASSES,
        "direction": ("uplink", "downlink"),
    }
    fields = {"profile": "profile", "uav": "uav", "channel": "traffic_class", "direction": "direction"}
    for dimension, values in dimensions.items():
        result[dimension] = {}
        for value in values:
            bad = 0
            if dimension == "profile":
                bad = sum(count for (profile, _channel), count in malformed.items() if profile == value)
            elif dimension == "channel":
                bad = sum(count for (_profile, channel), count in malformed.items() if channel == value)
            result[dimension][value] = accounting_result(
                (row for row in rows if row.get(fields[dimension]) == value), bad
            )
    joint: dict[str, Any] = {}
    for profile in PROFILES:
        for uav in UAVS:
            for channel in CLASSES:
                for direction in ("uplink", "downlink"):
                    key = f"{profile}|{uav}|{channel}|{direction}"
                    selected = (
                        row
                        for row in rows
                        if row.get("profile") == profile
                        and row.get("uav") == uav
                        and row.get("traffic_class") == channel
                        and row.get("direction") == direction
                    )
                    value = accounting_result(selected)
                    if value["packets_attempted"]:
                        joint[key] = value
    result["profile_uav_channel_direction"] = joint
    result["profile_channel"] = {
        f"{profile}|{channel}": accounting_result(
            (
                row
                for row in rows
                if row.get("profile") == profile and row.get("traffic_class") == channel
            ),
            malformed.get((profile, channel), 0),
        )
        for profile in PROFILES
        for channel in CLASSES
    }
    return result


def delivery_fairness(packets: dict[str, dict[str, Any]]) -> dict[str, float | None]:
    result: dict[str, float | None] = {}
    rows = list(packets.values())
    for profile in PROFILES:
        for channel in CLASSES:
            delivered_by_uav = [
                sum(
                    row.get("profile") == profile
                    and row.get("traffic_class") == channel
                    and row.get("uav") == uav
                    and row.get("audit_status") == "delivered"
                    for row in rows
                )
                for uav in UAVS
            ]
            result[f"{profile}|{channel}"] = jain_fairness(delivered_by_uav)
    return result


def evaluate_qos(
    accounting: dict[str, Any], qos_path: Path, pending_detail: dict[str, Any]
) -> dict[str, Any]:
    try:
        qos = yaml.safe_load(qos_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {"status": "unproven", "error": f"cannot read {qos_path}"}
    required_pdr = float(qos["classes"]["control"]["required_pdr"])
    maximum_p95 = float(qos["classes"]["control"]["max_p95_latency_ms"])
    profiles: dict[str, Any] = {}
    for profile in PROFILES:
        control = accounting["profile_channel"][f"{profile}|control"]
        overall = accounting["profile"][profile]
        p95 = control["latency_ms"]["p95"]
        threshold_ok = (
            control["pdr"] is not None
            and float(control["pdr"]) >= required_pdr
            and p95 is not None
            and float(p95) <= maximum_p95
        )
        if profile in {"nominal", "contention"}:
            pending_ok = overall["packets_pending"] == 0
        else:
            pending_ok = (
                overall["packets_pending"]
                == pending_detail["explicit_ns3_queue_or_transmit_backlog"]
            )
        profiles[profile] = {
            "status": "verified" if threshold_ok and pending_ok else "failed",
            "control_required_pdr": required_pdr,
            "control_observed_pdr": control["pdr"],
            "control_max_p95_latency_ms": maximum_p95,
            "control_observed_p95_latency_ms": p95,
            "control_thresholds_met": threshold_ok,
            "packets_pending": overall["packets_pending"],
            "pending_policy_met": pending_ok,
        }
    return {"status": "verified" if all(item["status"] == "verified" for item in profiles.values()) else "failed", "profiles": profiles}


def finish_medium(
    medium: dict[tuple[str, str, str], dict[str, Any]],
    windows: dict[str, tuple[int, int]],
    background: Counter[tuple[str, str]],
) -> dict[str, Any]:
    result: dict[str, Any] = {profile: {} for profile in PROFILES}
    for (profile, traffic_class, direction), state in sorted(medium.items()):
        duration_ns = windows[profile][1] - windows[profile][0]
        ratios = state.pop("queue_depth_ratios")
        busy_ns = state.pop("channel_busy_ns")
        result[profile][f"{traffic_class}:{direction}"] = {
            **state,
            "actual_medium_load_bps": state["actual_ingress_bytes"] * 8e9 / duration_ns,
            "event_sampled_queue_utilization": statistics.fmean(ratios) if ratios else None,
            "channel_busy_ms": busy_ns / 1e6,
            "channel_utilization": busy_ns / duration_ns,
            "excluded_background_serial_datagrams_observed_at_receiver": background.get(
                (profile, traffic_class), 0
            ),
        }
    return result


def audit_uart(
    run_dir: Path,
    uart_network: dict[tuple[str, str, str], dict[str, Any]],
    parse_errors: Counter[str],
) -> dict[str, Any]:
    tx_records: dict[tuple[str, str, str, int], dict[str, Any]] = {}
    rx: dict[tuple[str, str, str], dict[str, Any]] = defaultdict(
        lambda: {"network_bytes": 0, "released_bytes": 0, "records": 0, "hashes": Counter()}
    )
    for row in iter_jsonl(run_dir / "logs/uart_events.jsonl", parse_errors):
        channel = str(row.get("channel") or "")
        uav = f"uav{int(row.get('uav_id', 0) or 0)}"
        direction = str(row.get("direction") or "")
        digest = row.get("sha256")
        if direction == "uart_to_ns3":
            key = (channel, uav, str(row.get("source_log") or ""), int(row.get("monotonic_ns", 0) or 0))
            record = tx_records.setdefault(
                key,
                {
                    "payload_bytes": int(row.get("uart_record_bytes", 0) or 0),
                    "network_bytes": 0,
                    "fragment_count": int(row.get("fragment_count", 0) or 0),
                    "fragment_indices": set(),
                    "hashes": [],
                },
            )
            record["network_bytes"] += int(row.get("network_bytes", 0) or 0)
            record["fragment_indices"].add(int(row.get("fragment_index", -1)))
            if isinstance(digest, str):
                record["hashes"].append(digest)
        elif direction == "ns3_to_uart":
            key = (channel, uav, "gcs_to_uart")
            rx[key]["network_bytes"] += int(row.get("network_bytes", 0) or 0)
            rx[key]["released_bytes"] += int(row.get("uart_bytes_released", 0) or 0)
            rx[key]["records"] += int(row.get("uart_records_released", 0) or 0)
            if isinstance(digest, str):
                rx[key]["hashes"][digest] += 1

    result: dict[str, Any] = {}
    for channel in ("control", "payload"):
        for uav in UAVS:
            for direction in ("uart_to_gcs", "gcs_to_uart"):
                key = (channel, uav, direction)
                network = uart_network.get(key, new_uart_network_state())
                offered: dict[str, int] = network["offered_sizes"]
                delivered: dict[str, int] = network["delivered_sizes"]
                delivered_counts: Counter[str] = network["delivered_counts"]
                if direction == "uart_to_gcs":
                    records = [
                        value
                        for (record_channel, record_uav, _source, _timestamp), value in tx_records.items()
                        if record_channel == channel and record_uav == uav
                    ]
                    source_bytes = sum(int(record["payload_bytes"]) for record in records)
                    framed_bytes = sum(int(record["network_bytes"]) for record in records)
                    complete = [
                        record
                        for record in records
                        if record["fragment_count"] == len(record["fragment_indices"])
                        and record["hashes"]
                        and all(digest in delivered for digest in record["hashes"])
                    ]
                    reassembled = sum(int(record["payload_bytes"]) for record in complete)
                    duplicate_payload = sum(
                        int(record["payload_bytes"])
                        * max(0, min(delivered_counts[digest] for digest in record["hashes"]) - 1)
                        for record in complete
                    )
                    raw_destination = reassembled
                    record_count = len(records)
                    complete_count = len(complete)
                    destination_basis = "independent complete-fragment reconstruction from raw UART TX and ns-3 egress JSONL"
                else:
                    framed_bytes = sum(offered.values())
                    source_bytes = sum(max(0, size - BSF1_HEADER_BYTES) for size in offered.values())
                    destination = rx.get(key, {"network_bytes": 0, "released_bytes": 0, "records": 0})
                    reassembled = int(destination["released_bytes"])
                    raw_destination = reassembled
                    duplicate_payload = max(0, raw_destination - source_bytes)
                    record_count = None
                    complete_count = int(destination["records"])
                    destination_basis = "raw adapter uart_bytes_released JSONL"
                result[f"{uav}:{channel}:{direction}"] = {
                    "uav": uav,
                    "uart": channel,
                    "direction": direction,
                    "raw_uart_source_bytes": source_bytes,
                    "logical_serial_payload_bytes": source_bytes,
                    "BSF1_header_bytes": max(0, framed_bytes - source_bytes),
                    "framed_transport_bytes": framed_bytes,
                    "ns3_offered_bytes": sum(offered.values()),
                    "ns3_delivered_bytes": sum(delivered.values()),
                    "reassembled_payload_bytes": reassembled,
                    "raw_uart_destination_bytes": raw_destination,
                    "lost_payload_bytes": max(0, source_bytes - raw_destination),
                    "duplicate_payload_bytes": duplicate_payload,
                    "logical_records_source": record_count,
                    "logical_records_reassembled": complete_count,
                    "source_equals_reassembled_for_delivered_records": duplicate_payload == 0,
                    "destination_basis": destination_basis,
                    "drop_reasons": dict(network["drop_reasons"]),
                }
    return result


def audit_sionna(
    run_dir: Path,
    cells: dict[tuple[str, str, str], dict[str, Any]],
    summary: dict[str, Any],
    output_csv: Path,
    parse_errors: Counter[str],
) -> tuple[dict[str, Any], list[int]]:
    tracker_snapshots: list[tuple[float, dict[str, tuple[float, ...]]]] = []
    for tracker in iter_jsonl(run_dir / "metrics/node_state.jsonl", parse_errors):
        if not isinstance(tracker.get("time_s"), (int, float)):
            continue
        tracker_positions = {
            str(node.get("id")): tuple(float(value) for value in node.get("position_m", []))
            for node in tracker.get("nodes", [])
            if isinstance(node, dict) and not node.get("stale") and len(node.get("position_m", [])) == 3
        }
        if len(tracker_positions) == 6:
            tracker_snapshots.append((float(tracker["time_s"]), tracker_positions))
    requests: list[dict[str, Any]] = []
    responses: dict[float, dict[str, Any]] = {}
    for row in iter_jsonl(run_dir / "logs/sionna_link_queries.jsonl", parse_errors):
        message = row.get("message")
        if not isinstance(message, dict):
            continue
        query_time = float(message.get("time_s", 0.0) or 0.0)
        if row.get("direction") == "request" and message.get("type") == "link_query":
            if len(message.get("nodes", [])) == 6 and len(message.get("links", [])) == 30:
                requests.append(message)
        elif row.get("direction") == "response" and message.get("type") == "link_state":
            responses[query_time] = message

    states: dict[tuple[str, str, str], dict[str, Any]] = {}
    query_ids: dict[int, str] = {}
    clock_offsets: list[int] = []
    request_times = {index + 1: request for index, request in enumerate(requests)}
    for row in iter_jsonl(run_dir / "logs/sionna_packet_states.jsonl", parse_errors):
        query_id = str(row.get("query_id") or "")
        match = re.match(r"town01-(\d+)-(\d+)$", query_id)
        if not match:
            continue
        index = int(match.group(1))
        query_ids[index] = query_id
        states[(query_id, str(row.get("directed_link") or ""), str(row.get("traffic_class") or ""))] = row
        request = request_times.get(index)
        if request:
            clock_offsets.append(int(float(request["time_s"]) * 1e9) - int(match.group(2)))

    rows: list[dict[str, Any]] = []
    position_sets: set[tuple[tuple[str, tuple[float, ...]], ...]] = set()
    position_sets_cm: set[tuple[tuple[str, tuple[float, ...]], ...]] = set()
    provider_latencies: list[float] = []
    state_application_latencies: list[float] = []
    state_ages: list[float] = []
    applied_cells = 0
    coupling_errors_m: list[float] = []
    position_ranges: dict[str, list[tuple[float, ...]]] = defaultdict(list)
    for index, request in enumerate(requests, 1):
        query_time = float(request["time_s"])
        response = responses.get(query_time, {})
        provider_latency = response.get("provider_latency_ms")
        if isinstance(provider_latency, (int, float)):
            provider_latencies.append(float(provider_latency))
        positions = {
            str(node.get("id")): tuple(float(value) for value in node.get("position_m", []))
            for node in request.get("nodes", [])
            if isinstance(node, dict) and len(node.get("position_m", [])) == 3
        }
        position_sets.add(tuple(sorted(positions.items())))
        position_sets_cm.add(
            tuple(sorted((name, tuple(round(value, 2) for value in position)) for name, position in positions.items()))
        )
        for name, position in positions.items():
            position_ranges[name].append(position)
        candidate_errors: list[float] = []
        for tracker_time, tracker_positions in tracker_snapshots:
            if abs(tracker_time - query_time) > 2.0:
                continue
            if set(positions) - set(tracker_positions):
                continue
            candidate_errors.append(
                max(
                    math.dist(positions[name], tracker_positions[name])
                    for name in positions
                )
            )
        if candidate_errors:
            coupling_errors_m.append(min(candidate_errors))
        response_links = {
            (str(link.get("tx")), str(link.get("rx")), str(link.get("traffic_class"))): link
            for link in response.get("links", [])
            if isinstance(link, dict)
        }
        query_id = query_ids.get(index, "")
        for link in request.get("links", []):
            if not isinstance(link, dict):
                continue
            tx = str(link.get("tx"))
            rx = str(link.get("rx"))
            traffic_class = str(link.get("traffic_class"))
            response_link = response_links.get((tx, rx, traffic_class), {})
            state = states.get((query_id, f"{tx}>{rx}", traffic_class), {})
            application = cells.get((query_id, f"{tx}>{rx}", traffic_class), {})
            applied_ns = application.get("first_ns3_applied_monotonic_ns")
            adapter_ns = state.get("adapter_applied_monotonic_ns")
            application_latency = (
                (int(applied_ns) - int(adapter_ns)) / 1e6
                if isinstance(applied_ns, int) and isinstance(adapter_ns, int)
                else None
            )
            if application_latency is not None:
                state_application_latencies.append(application_latency)
                applied_cells += 1
            ages = application.get("state_ages_ms", [])
            age_max = max(ages) if ages else None
            if age_max is not None:
                state_ages.extend(ages)
            rows.append(
                {
                    "query_index": index,
                    "query_timestamp_s": query_time,
                    "query_id": query_id,
                    "tx": tx,
                    "rx": rx,
                    "traffic_class": traffic_class,
                    "tx_position": json.dumps(positions.get(tx), separators=(",", ":")),
                    "rx_position": json.dumps(positions.get(rx), separators=(",", ":")),
                    "rssi_dbm": response_link.get("rssi_dbm"),
                    "sinr_db": response_link.get("sinr_db"),
                    "jammer_state": json.dumps(request.get("emitters", []), separators=(",", ":")),
                    "provider_response_ms": provider_latency,
                    "adapter_applied_monotonic_ns": adapter_ns,
                    "ns3_applied_monotonic_ns": applied_ns,
                    "state_application_latency_ms": application_latency,
                    "state_age_max_ms": age_max,
                    "packet_uses": len(application.get("packet_uids", set())),
                }
            )

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0]) if rows else ["query_index"]
    with output_csv.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    span = requests[-1]["time_s"] - requests[0]["time_s"] if len(requests) > 1 else 0.0
    result = {
        "run_duration_s_first_request_to_last_response": (
            float(requests[-1]["time_s"])
            - float(requests[0]["time_s"])
            + (float(responses.get(float(requests[-1]["time_s"]), {}).get("provider_latency_ms", 0.0)) / 1000)
            if requests
            else 0.0
        ),
        "real_query_count": len(requests),
        "query_rate_hz_interarrival": (len(requests) - 1) / span if span > 0 else None,
        "unique_position_sets_exact": len(position_sets),
        "unique_position_sets_rounded_1cm": len(position_sets_cm),
        "gazebo_position_coupling": {
            "queries_compared": len(coupling_errors_m),
            "queries_matching_tracker_within_1mm": sum(error <= 0.001 for error in coupling_errors_m),
            "maximum_best_match_error_m": max(coupling_errors_m) if coupling_errors_m else None,
            "per_node_position_span_m": {
                name: (
                    max(
                        math.dist(position, samples[0])
                        for position in samples
                    )
                    if samples
                    else 0.0
                )
                for name, samples in sorted(position_ranges.items())
            },
        },
        "links_per_query": sorted({len(request.get("links", [])) for request in requests}),
        "provider_latency_ms": numeric(provider_latencies),
        "state_application_latency_ms": numeric(state_application_latencies),
        "channel_state_age_ms": numeric(state_ages),
        "cells_ever_applied_by_ns3": applied_cells,
        "used_state_count": summary["used_state_count"],
        "reused_state_count": summary["reused_state_count"],
        "reused_state_packet_uses": summary["reused_state_packet_uses"],
        "stale_unique_packets": summary["stale_unique_packets"],
        "radio_status_event_counts": dict(summary["status_events"]),
        "per_query_link_rows": len(rows),
    }
    return result, clock_offsets


def audit_p2mp(run_dir: Path, parse_errors: Counter[str]) -> dict[str, Any]:
    sent: dict[int, dict[str, Any]] = {}
    for row in iter_jsonl(run_dir / "logs/audit_p2mp_sent.jsonl", parse_errors):
        try:
            sequence = int(row["sequence"])
            sent_ns = int(row["sent_monotonic_ns"])
        except (KeyError, TypeError, ValueError):
            parse_errors["audit_p2mp_sent"] += 1
            continue
        sent[sequence] = {**row, "sent_monotonic_ns": sent_ns}

    result: dict[str, Any] = {}
    for uav in UAVS:
        seen: Counter[int] = Counter()
        received_at: dict[int, int] = {}
        malformed = 0
        for row in iter_jsonl(run_dir / f"logs/additional_{uav}.jsonl", parse_errors):
            if row.get("event") == "malformed":
                malformed += 1
            if row.get("event") == "receive" and row.get("kind") == "p2mp_downlink":
                try:
                    sequence = int(row["sequence"])
                    if sent and sequence not in sent:
                        continue
                    seen[sequence] += 1
                    received_at.setdefault(sequence, int(row["monotonic_ns"]))
                except (KeyError, TypeError, ValueError):
                    malformed += 1
        sequences = sorted(seen)
        if sent:
            missing = sorted(set(sent) - set(seen))
            latencies = [
                (received_at[sequence] - int(sent[sequence]["sent_monotonic_ns"])) / 1e6
                for sequence in sequences
                if sequence in received_at
            ]
            phases: dict[str, Any] = {}
            for phase in sorted({str(row.get("phase") or "unknown") for row in sent.values()}):
                phase_sequences = {
                    sequence
                    for sequence, row in sent.items()
                    if str(row.get("phase") or "unknown") == phase
                }
                delivered_phase = phase_sequences & set(seen)
                phase_latencies = [
                    (received_at[sequence] - int(sent[sequence]["sent_monotonic_ns"])) / 1e6
                    for sequence in delivered_phase
                ]
                phases[phase] = {
                    "attempted": len(phase_sequences),
                    "delivered_unique": len(delivered_phase),
                    "missing_sequences": sorted(phase_sequences - delivered_phase),
                    "latency_ms": numeric(phase_latencies),
                }
            result[uav] = {
                "attempted_logical_messages": len(sent),
                "delivered_unique": len(sequences),
                "duplicates": sum(max(0, count - 1) for count in seen.values()),
                "missing_sequences": missing,
                "checksum_failures_or_malformed": malformed,
                "latency_ms": numeric(latencies),
                "phases": phases,
            }
        else:
            missing = list(range(sequences[0], sequences[-1] + 1)) if sequences else []
            missing = [sequence for sequence in missing if sequence not in seen]
            result[uav] = {
                "attempted_logical_messages_observable": None,
                "delivered_unique": len(sequences),
                "duplicates": sum(max(0, count - 1) for count in seen.values()),
                "sequences": sequences,
                "missing_sequences_within_observed_range": missing,
                "checksum_failures_or_malformed": malformed,
                "latency_ms": {"p50": None, "p95": None, "p99": None},
            }
    return result


def iter_pcap_udp(path: Path, parse_errors: Counter[str]) -> Iterator[bytes]:
    try:
        stream = path.open("rb")
    except OSError:
        parse_errors[str(path)] += 1
        return
    with stream:
        header = stream.read(24)
        if len(header) != 24:
            parse_errors[str(path)] += 1
            return
        if header[:4] in {b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1"}:
            endian = "<"
        elif header[:4] in {b"\xa1\xb2\xc3\xd4", b"\xa1\xb2\x3c\x4d"}:
            endian = ">"
        else:
            parse_errors[str(path)] += 1
            return
        while True:
            record_header = stream.read(16)
            if not record_header:
                return
            if len(record_header) != 16:
                parse_errors[str(path)] += 1
                return
            _seconds, _fraction, captured, _wire = struct.unpack(
                f"{endian}IIII", record_header
            )
            packet = stream.read(captured)
            if len(packet) != captured or len(packet) < 42:
                parse_errors[str(path)] += 1
                continue
            offset = 14
            ether_type = struct.unpack_from("!H", packet, 12)[0]
            if ether_type == 0x8100 and len(packet) >= 18:
                ether_type = struct.unpack_from("!H", packet, 16)[0]
                offset = 18
            if ether_type != 0x0800 or len(packet) < offset + 28:
                continue
            ip_length = (packet[offset] & 0x0F) * 4
            if ip_length < 20 or packet[offset + 9] != 17:
                continue
            udp_offset = offset + ip_length
            if len(packet) < udp_offset + 8:
                continue
            udp_length = struct.unpack_from("!H", packet, udp_offset + 4)[0]
            if udp_length < 8 or len(packet) < udp_offset + udp_length:
                continue
            yield packet[udp_offset + 8 : udp_offset + udp_length]


def serial_records_from_pcap(
    path: Path, parse_errors: Counter[str]
) -> dict[tuple[str, int, str], list[tuple[int, bytes]]]:
    channels = {1: "control", 2: "payload"}
    directions = {1: "uart_to_gcs", 2: "gcs_to_uart"}
    fragments: dict[tuple[int, int, int, int], dict[str, Any]] = {}
    for datagram in iter_pcap_udp(path, parse_errors):
        if len(datagram) < BSF1_HEADER.size or not datagram.startswith(b"BSF1"):
            continue
        try:
            (
                magic,
                version,
                channel_id,
                uav_id,
                direction_id,
                sequence,
                fragment_index,
                fragment_count,
                payload_length,
                total_length,
                _sent_ns,
                record_crc,
                fragment_crc,
            ) = BSF1_HEADER.unpack_from(datagram)
        except struct.error:
            parse_errors["pcap_bsf1"] += 1
            continue
        payload = datagram[BSF1_HEADER.size :]
        if (
            magic != b"BSF1"
            or version != 1
            or channel_id not in channels
            or direction_id not in directions
            or not 1 <= uav_id <= 5
            or fragment_count < 1
            or fragment_index >= fragment_count
            or payload_length != len(payload)
            or total_length < payload_length
            or (binascii.crc32(payload) & 0xFFFFFFFF) != fragment_crc
        ):
            parse_errors["pcap_bsf1"] += 1
            continue
        key = (channel_id, uav_id, direction_id, sequence)
        state = fragments.setdefault(
            key,
            {
                "count": fragment_count,
                "total": total_length,
                "crc": record_crc,
                "fragments": {},
            },
        )
        if (
            state["count"] != fragment_count
            or state["total"] != total_length
            or state["crc"] != record_crc
        ):
            parse_errors["pcap_bsf1_conflict"] += 1
            continue
        previous = state["fragments"].get(fragment_index)
        if previous is not None and previous != payload:
            parse_errors["pcap_bsf1_conflict"] += 1
            continue
        state["fragments"][fragment_index] = payload

    result: dict[tuple[str, int, str], list[tuple[int, bytes]]] = defaultdict(list)
    for (channel_id, uav_id, direction_id, sequence), state in fragments.items():
        if len(state["fragments"]) != state["count"]:
            continue
        payload = b"".join(state["fragments"][index] for index in range(state["count"]))
        if len(payload) != state["total"] or (
            binascii.crc32(payload) & 0xFFFFFFFF
        ) != state["crc"]:
            parse_errors["pcap_bsf1_record_crc"] += 1
            continue
        result[(channels[channel_id], uav_id, directions[direction_id])].append(
            (sequence, payload)
        )
    for records in result.values():
        records.sort(key=lambda item: item[0])
    return result


def mavlink_frames(records: list[tuple[int, bytes]]) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    buffer = bytearray()
    previous_sequence: int | None = None
    for sequence, record in records:
        if previous_sequence is not None and sequence != (previous_sequence + 1) & 0xFFFFFFFF:
            buffer.clear()
        previous_sequence = sequence
        buffer.extend(record)
        while buffer:
            starts = [index for index in (buffer.find(b"\xfd"), buffer.find(b"\xfe")) if index >= 0]
            if not starts:
                buffer.clear()
                break
            start = min(starts)
            if start:
                del buffer[:start]
            if len(buffer) < 2:
                break
            if buffer[0] == 0xFD:
                if len(buffer) < 10:
                    break
                frame_length = 12 + buffer[1] + (13 if buffer[2] & 1 else 0)
                payload_offset = 10
                system_id = int(buffer[5])
                component_id = int(buffer[6])
                message_id = int(buffer[7]) | (int(buffer[8]) << 8) | (int(buffer[9]) << 16)
            else:
                if len(buffer) < 6:
                    break
                frame_length = 8 + buffer[1]
                payload_offset = 6
                system_id = int(buffer[3])
                component_id = int(buffer[4])
                message_id = int(buffer[5])
            if len(buffer) < frame_length:
                break
            frame = bytes(buffer[:frame_length])
            del buffer[:frame_length]
            payload_length = frame[1]
            frames.append(
                {
                    "message_id": message_id,
                    "system_id": system_id,
                    "component_id": component_id,
                    "payload": frame[payload_offset : payload_offset + payload_length],
                }
            )
    return frames


def audit_mavlink_lifecycle(
    run_dir: Path, parse_errors: Counter[str]
) -> dict[str, Any]:
    records = serial_records_from_pcap(
        run_dir / "pcap/ns3_packet_engine-radio-gcs.pcap", parse_errors
    )
    mode_names = {0: "STABILIZE", 4: "GUIDED", 9: "LAND"}
    result: dict[str, Any] = {"uavs": {}}
    for uav_id in range(1, 6):
        inbound = {
            channel: mavlink_frames(records.get((channel, uav_id, "uart_to_gcs"), []))
            for channel in ("control", "payload")
        }
        outbound = [
            frame
            for channel in ("control", "payload")
            for frame in mavlink_frames(records.get((channel, uav_id, "gcs_to_uart"), []))
        ]
        heartbeats = [
            frame
            for frame in inbound["control"]
            if frame["message_id"] == 0 and frame["system_id"] == uav_id and len(frame["payload"]) >= 9
        ]
        heartbeat_states = [
            {
                "custom_mode": struct.unpack_from("<I", frame["payload"], 0)[0],
                "armed": bool(frame["payload"][6] & 0x80),
                "system_status": int(frame["payload"][7]),
            }
            for frame in heartbeats
        ]
        commands: list[dict[str, Any]] = []
        parameter_sets: list[str] = []
        for frame in outbound:
            payload = frame["payload"]
            if frame["message_id"] == 76 and len(payload) >= 32:
                params = list(struct.unpack_from("<7f", payload, 0))
                command = struct.unpack_from("<H", payload, 28)[0]
                if int(payload[30]) == uav_id:
                    commands.append({"command": command, "params": params})
            elif frame["message_id"] == 23 and len(payload) >= 22:
                name = payload[6:22].split(b"\0", 1)[0].decode("ascii", errors="replace")
                parameter_sets.append(name)
        acknowledgements: dict[int, list[int]] = defaultdict(list)
        for channel in ("control", "payload"):
            for frame in inbound[channel]:
                if frame["message_id"] == 77 and frame["system_id"] == uav_id and len(frame["payload"]) >= 3:
                    acknowledgements[struct.unpack_from("<H", frame["payload"], 0)[0]].append(
                        int(frame["payload"][2])
                    )
        initial = heartbeat_states[0] if heartbeat_states else None
        final = heartbeat_states[-1] if heartbeat_states else None
        arm_commands = [item for item in commands if item["command"] == 400]
        takeoff_commands = [item for item in commands if item["command"] == 22]
        result["uavs"][f"uav{uav_id}"] = {
            "initial_mode": mode_names.get(initial["custom_mode"], str(initial["custom_mode"])) if initial else None,
            "initial_armed_state": initial["armed"] if initial else None,
            "heartbeat_count_control": len(heartbeats),
            "heartbeat_count_payload": sum(
                frame["message_id"] == 0 and frame["system_id"] == uav_id
                for frame in inbound["payload"]
            ),
            "arm_result": acknowledgements.get(400),
            "takeoff_command_result": acknowledgements.get(22),
            "target_altitude_m": takeoff_commands[-1]["params"][6] if takeoff_commands else None,
            "land_command": any(item["command"] == 21 for item in commands),
            "land_command_result": acknowledgements.get(21),
            "disarm": (not final["armed"]) if final else None,
            "final_mode": mode_names.get(final["custom_mode"], str(final["custom_mode"])) if final else None,
            "final_armed_state": final["armed"] if final else None,
            "force_arm_used": any(
                len(item["params"]) > 1 and abs(float(item["params"][1])) > 0.5
                for item in arm_commands
            ),
            "arming_check_parameter_writes": [
                name for name in parameter_sets if name == "ARMING_CHECK"
            ],
            "real_sitl_source_system_ids": sorted(
                {
                    frame["system_id"]
                    for channel in ("control", "payload")
                    for frame in inbound[channel]
                    if frame["system_id"] > 0
                }
            ),
        }
    result["complete_bsf1_records_from_pcap"] = sum(len(value) for value in records.values())
    return result


def audit_flight(
    run_dir: Path, clock_offsets: list[int], parse_errors: Counter[str]
) -> dict[str, Any]:
    scenario_events = {
        str(row.get("event")): int(row.get("monotonic_ns", 0) or 0)
        for row in iter_jsonl(run_dir / "logs/scenario_events.jsonl", parse_errors)
    }
    offset = int(statistics.median(clock_offsets)) if clock_offsets else None
    positions: dict[str, list[tuple[int, tuple[float, float, float]]]] = defaultdict(list)
    for row in iter_jsonl(run_dir / "metrics/node_state.jsonl", parse_errors):
        if offset is None or not isinstance(row.get("time_s"), (int, float)):
            continue
        monotonic_ns = int(float(row["time_s"]) * 1e9) - offset
        for node in row.get("nodes", []):
            if not isinstance(node, dict) or node.get("stale"):
                continue
            name = str(node.get("id") or "")
            value = node.get("position_m")
            if name in UAVS and isinstance(value, list) and len(value) == 3:
                positions[name].append((monotonic_ns, tuple(float(item) for item in value)))

    guided = scenario_events.get("guided_mode", 0)
    takeoff = scenario_events.get("takeoff_complete", 0)
    hold_end = scenario_events.get("hold_complete", 0)
    movement = scenario_events.get("movement_complete", 0)
    landing = scenario_events.get("landing_complete", 2**63 - 1)
    result: dict[str, Any] = {"clock_alignment_offset_ns": offset, "uavs": {}}
    for uav in UAVS:
        samples = [(stamp, position) for stamp, position in positions[uav] if guided <= stamp <= landing]
        initial = min(samples, key=lambda item: abs(item[0] - guided))[1] if samples else None
        final = min(samples, key=lambda item: abs(item[0] - landing))[1] if samples else None
        hold = [position for stamp, position in samples if takeoff <= stamp <= hold_end]
        maximum_altitude = max((position[2] for _stamp, position in samples), default=None)
        stabilized = statistics.median(position[2] for position in hold) if hold else None
        horizontal = (
            max(math.hypot(position[0] - initial[0], position[1] - initial[1]) for _stamp, position in samples)
            if samples and initial
            else None
        )
        result["uavs"][uav] = {
            "initial_mode": None,
            "initial_armed_state": None,
            "heartbeat": scenario_events.get("heartbeats_ready") is not None,
            "arm_result": None,
            "takeoff_command_result": None,
            "target_altitude_m": 15.0,
            "maximum_altitude_m": maximum_altitude,
            "stabilized_altitude_m": stabilized,
            "hold_duration_s": (hold_end - takeoff) / 1e9 if hold_end and takeoff else None,
            "horizontal_displacement_m": horizontal,
            "land_command": None,
            "touchdown_position_consistent": (
                abs(final[2] - initial[2]) <= 0.5 if final and initial else None
            ),
            "disarm": None,
            "final_mode": None,
            "initial_position_m": initial,
            "final_position_m": final,
            "position_sample_count": len(samples),
            "movement_before_movement_complete_observed": any(
                stamp <= movement
                and initial is not None
                and math.hypot(position[0] - initial[0], position[1] - initial[1]) >= 3.0
                for stamp, position in samples
            ),
        }
    return result


def write_packet_csv(path: Path, packets: dict[str, dict[str, Any]]) -> None:
    fields = [
        "packet_id", "profile", "uav", "channel", "direction", "sequence", "packet_bytes",
        "status", "deliveries", "duplicates", "latency_ms", "queue_drop_events",
        "PHY_drop_events", "backoff_events", "retry_events", "drop_reasons",
        "pending_reason", "last_event", "last_event_monotonic_ns",
    ]
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for packet_id, state in sorted(packets.items()):
            writer.writerow(
                {
                    "packet_id": packet_id,
                    "profile": state.get("profile"),
                    "uav": state.get("uav"),
                    "channel": state.get("traffic_class"),
                    "direction": state.get("direction"),
                    "sequence": state.get("sequence"),
                    "packet_bytes": state.get("packet_bytes"),
                    "status": state["audit_status"],
                    "deliveries": state["delivery_count"],
                    "duplicates": max(0, int(state["delivery_count"]) - 1),
                    "latency_ms": state["latencies_ms"][0] if state["latencies_ms"] else None,
                    "queue_drop_events": state["queue_drop_events"],
                    "PHY_drop_events": state["phy_drop_events"],
                    "backoff_events": state["backoff_events"],
                    "retry_events": state["retry_events"],
                    "drop_reasons": json.dumps(dict(state["drop_reasons"]), separators=(",", ":")),
                    "pending_reason": pending_reason(state) if state["audit_status"] == "pending" else "",
                    "last_event": state["last_event"],
                    "last_event_monotonic_ns": state["last_event_monotonic_ns"],
                }
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--qos",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "network/config/communication_qos.yaml",
    )
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    parse_errors: Counter[str] = Counter()

    windows = read_windows(run_dir / "logs/profile_windows.jsonl", parse_errors)
    packets, hash_to_packets, malformed, background = load_logical_packets(
        run_dir, parse_errors
    )
    medium_raw, uart_network, sionna_cells, sionna_event_summary = scan_packet_events(
        run_dir, packets, hash_to_packets, windows, parse_errors
    )
    totals = classify_packets(packets)
    accounting = grouped_accounting(packets, malformed)
    pending = Counter(
        pending_reason(state) for state in packets.values() if state["audit_status"] == "pending"
    )
    pending_by_profile_channel: Counter[str] = Counter()
    pending_timing: Counter[str] = Counter()
    for state in packets.values():
        if state["audit_status"] != "pending":
            continue
        pending_by_profile_channel[f"{state.get('profile')}:{state.get('traffic_class')}"] += 1
        egress_ns = state["event_last_ns"].get("egress")
        profile_window = windows.get(str(state.get("profile")))
        if egress_ns is not None and profile_window is not None:
            pending_timing[
                "egress_after_profile_receiver_end"
                if egress_ns > profile_window[1]
                else "egress_before_profile_receiver_end"
            ] += 1
    pending_detail = {
        "by_last_observed_stage": dict(pending),
        "by_profile_and_channel": dict(pending_by_profile_channel),
        "egress_timing": dict(pending_timing),
        "explicit_ns3_queue_or_transmit_backlog": pending["queued_without_terminal_event"]
        + pending["dequeued_without_channel"]
        + pending["channel_without_endpoint_egress"],
        "not_backlog": pending["no_ns3_observation"]
        + pending["ns3_egress_without_application_delivery"],
    }
    qos_evaluation = evaluate_qos(accounting, args.qos.resolve(), pending_detail)
    medium = finish_medium(medium_raw, windows, background)
    uart = audit_uart(run_dir, uart_network, parse_errors)
    sionna, clock_offsets = audit_sionna(
        run_dir,
        sionna_cells,
        sionna_event_summary,
        output_dir / "sionna_queries.csv",
        parse_errors,
    )
    p2mp = audit_p2mp(run_dir, parse_errors)
    flight = audit_flight(run_dir, clock_offsets, parse_errors)
    mavlink_lifecycle = audit_mavlink_lifecycle(run_dir, parse_errors)
    for uav, evidence in mavlink_lifecycle["uavs"].items():
        flight["uavs"].setdefault(uav, {}).update(evidence)
    flight["complete_bsf1_records_from_pcap"] = mavlink_lifecycle[
        "complete_bsf1_records_from_pcap"
    ]
    write_packet_csv(output_dir / "logical_packets.csv", packets)

    required = (
        "report.md",
        "metrics/runtime_topology.json",
        "metrics/communication_summary.json",
        "metrics/communication_summary.csv",
        "metrics/qos_summary.json",
        "metrics/realtime_summary.json",
        "logs/uart_events.jsonl",
        "logs/packet_events.jsonl",
        "logs/queue_events.jsonl",
        "pcap/control.pcap",
        "pcap/payload.pcap",
        "pcap/additional_data.pcap",
    )
    artifact_sizes = {
        relative: (run_dir / relative).stat().st_size if (run_dir / relative).is_file() else None
        for relative in required
    }
    limitations = [
        "GCS-to-UART source payload bytes are inferred from BSF1-sized ns-3 ingress records because the run has no separate GCS serial-byte dump; UART-to-GCS destination bytes are independently reconstructed from complete fragment egress.",
        "MAVLink lifecycle fields are structurally decoded from BSF1 records reconstructed from raw PCAP; the audit does not use the producer-owned lifecycle PASS fields.",
    ]
    p2mp_phases = {
        str(phase)
        for receiver in p2mp.values()
        if isinstance(receiver, dict)
        for phase in (receiver.get("phases") or {})
    }
    p2mp_attempts = [
        int(receiver.get("attempted_logical_messages") or 0)
        for receiver in p2mp.values()
        if isinstance(receiver, dict)
    ]
    if not p2mp_attempts or max(p2mp_attempts) < 100:
        limitations.append(
            "This run does not contain 100 timestamped P2MP logical messages; use a separate targeted P2MP runtime for that claim."
        )
    if not {"outage_uav3", "recovery"}.issubset(p2mp_phases):
        limitations.append(
            "This run has no P2MP endpoint-outage and recovery phases; use a separate targeted P2MP runtime for resilience evidence."
        )

    result = {
        "run_id": run_dir.name,
        "environment": read_environment(run_dir / "environment.txt"),
        "required_artifact_sizes": artifact_sizes,
        "raw_parse_errors": dict(parse_errors),
        "logical_packet_totals": dict(totals),
        "accounting": accounting,
        "pending_explanation": pending_detail,
        "qos_evaluation": qos_evaluation,
        "fairness_jain_delivered_packets": delivery_fairness(packets),
        "runtime": {
            "gazebo_real_time_factor": gazebo_realtime(run_dir / "logs/gazebo_stats.log"),
            "ns3_scheduler_lag_ms": sionna_event_summary["scheduler_lag_ms"],
        },
        "background_serial_datagrams": {
            "total": sum(background.values()),
            "by_profile_and_channel": {
                f"{profile}:{channel}": count
                for (profile, channel), count in sorted(background.items())
            },
            "medium_load_treatment": "included in actual/background ingress bytes and queue/channel observations; excluded only from BQO1 logical attempts",
        },
        "profile_medium": medium,
        "uart_paths": uart,
        "sionna": sionna,
        "p2mp": p2mp,
        "flight": flight,
        "limitations": limitations,
    }
    write_json(output_dir / "audit_metrics.json", result)
    print(json.dumps({"run_id": run_dir.name, "accounting": accounting["all"], "pending": dict(pending), "sionna": sionna}, sort_keys=True))
    return 0 if all(value and value > 0 for value in artifact_sizes.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
