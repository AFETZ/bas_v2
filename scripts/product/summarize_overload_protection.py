#!/usr/bin/env python3
"""Build the bounded-overload and event-efficiency product artifacts."""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from network.ns3.tap_packet_engine_config import ConfigError, data_rate_bps  # noqa: E402
from network.scripts.communication_qos import DEFAULT_PATH, load_qos  # noqa: E402


CLASSES = ("control", "payload", "additional_data")
UAVS = tuple(f"uav{index}" for index in range(1, 6))
TERMINAL_STATUSES = {
    "delivered",
    "dropped_at_ingress",
    "dropped_in_medium",
    "expired_at_drain",
}


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        stream = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return rows
    with stream:
        for line in stream:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
    return rows


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def distribution(values: list[float]) -> dict[str, int | float | None]:
    return {
        "count": len(values),
        "mean": statistics.fmean(values) if values else None,
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
        "p99": percentile(values, 0.99),
        "max": max(values) if values else None,
    }


def jain(values: list[float]) -> float:
    denominator = len(values) * sum(value * value for value in values)
    return sum(values) ** 2 / denominator if denominator else 0.0


def profile_window_details(run_dir: Path, profile: str) -> dict[str, Any]:
    start_row: dict[str, Any] = {}
    end_row: dict[str, Any] = {}
    for row in read_jsonl(run_dir / "logs/profile_windows.jsonl"):
        if row.get("profile") != profile:
            continue
        if row.get("event") == "profile_start":
            start_row = row
        elif row.get("event") == "profile_end":
            end_row = row
    start = int(start_row.get("scheduled_start_monotonic_ns", 0) or 0)
    end = int(end_row.get("monotonic_ns", 0) or 0)
    duration = float(start_row.get("duration_s", 0) or 0)
    if start <= 0 or end <= start or duration <= 0:
        raise ValueError(f"missing complete profile window for {profile} in {run_dir}")
    wall_interval_s = (end - start) / 1e9
    return {
        "start_monotonic_ns": start,
        "end_monotonic_ns": end,
        "offered_duration_s": duration,
        "wall_interval_s": wall_interval_s,
        "post_offer_drain_wall_s": max(0.0, wall_interval_s - duration),
        "start_record": start_row,
        "end_record": end_row,
    }


def profile_window(run_dir: Path, profile: str) -> tuple[int, int, float]:
    details = profile_window_details(run_dir, profile)
    return (
        int(details["start_monotonic_ns"]),
        int(details["end_monotonic_ns"]),
        float(details["offered_duration_s"]),
    )


def packet_rates(
    attempts_by_id: dict[str, dict[str, Any]],
    packet_ids: set[str],
    *,
    offered_duration_s: float,
    wall_interval_s: float,
) -> dict[str, int | float]:
    octets = sum(
        int(attempts_by_id[packet_id].get("packet_bytes", 0) or 0)
        for packet_id in packet_ids
        if packet_id in attempts_by_id
    )
    return {
        "packets": len(packet_ids),
        "bytes": octets,
        "application_bytes": octets,
        "packets_per_second": len(packet_ids) / offered_duration_s,
        "bits_per_second": octets * 8.0 / offered_duration_s,
        "offered_window_normalized_packets_per_second": len(packet_ids)
        / offered_duration_s,
        "offered_window_normalized_bits_per_second": octets
        * 8.0
        / offered_duration_s,
        "profile_wall_interval_packets_per_second": len(packet_ids)
        / wall_interval_s,
        "profile_wall_interval_bits_per_second": octets * 8.0 / wall_interval_s,
    }


def unavailable_rates() -> dict[str, None]:
    return {
        "packets": None,
        "bytes": None,
        "application_bytes": None,
        "packets_per_second": None,
        "bits_per_second": None,
        "offered_window_normalized_packets_per_second": None,
        "offered_window_normalized_bits_per_second": None,
        "profile_wall_interval_packets_per_second": None,
        "profile_wall_interval_bits_per_second": None,
    }


def terminal_records(
    run_dir: Path,
    profile: str,
    attempts: list[dict[str, Any]],
    deliveries: list[dict[str, Any]],
    events: list[dict[str, Any]],
    *,
    start_ns: int | None = None,
    end_ns: int | None = None,
) -> dict[str, dict[str, Any]]:
    stored_rows = [
        row
        for row in read_jsonl(run_dir / "logs/communication_terminal_outcomes.jsonl")
        if row.get("profile") == profile and isinstance(row.get("packet_id"), str)
    ]
    stored = {str(row["packet_id"]): row for row in stored_rows}
    attempt_ids = {str(row["packet_id"]) for row in attempts}
    stored_is_authoritative = (
        len(stored_rows) == len(stored)
        and set(stored) == attempt_ids
        and all(
            bool(row.get("terminal")) and row.get("status") in TERMINAL_STATUSES
            for row in stored.values()
        )
    )
    from network.scripts.packet_accounting import terminal_packet_outcomes

    def in_bounds(row: dict[str, Any], field: str) -> bool:
        if start_ns is None and end_ns is None:
            return True
        observed = row.get(field)
        return (
            isinstance(observed, (int, float))
            and (start_ns is None or int(observed) >= start_ns)
            and (end_ns is None or int(observed) <= end_ns)
        )

    bounded_deliveries = [
        row for row in deliveries if in_bounds(row, "received_monotonic_ns")
    ]
    bounded_events = [
        row for row in events if in_bounds(row, "host_monotonic_ns")
    ]

    # The runner's exact terminal attempt set remains authoritative at the
    # bounded drain cutoff.  Reconcile only an expiry for which the persisted,
    # bounded delivery/drop evidence proves a more specific factual terminal
    # outcome.  This covers a snapshot ending between a complete ns-3 JSONL row
    # and the following partial row without allowing later flushes to rewrite
    # the cutoff.
    if stored_is_authoritative:
        factual = terminal_packet_outcomes(
            attempts,
            bounded_deliveries,
            bounded_events,
            finalize_pending=False,
        )
        reconciled = {packet_id: dict(row) for packet_id, row in stored.items()}
        factual_fields = (
            "status",
            "terminal",
            "drop_reason",
            "delivery_count",
            "duplicate_deliveries",
            "drop_event_count",
            "backoff_events",
            "retry_events",
        )
        for packet_id, row in reconciled.items():
            if row.get("status") != "expired_at_drain":
                continue
            observed = factual.get(packet_id, {})
            if observed.get("status") not in {
                "delivered",
                "dropped_at_ingress",
                "dropped_in_medium",
            }:
                continue
            for field in factual_fields:
                row[field] = observed.get(field)
            row["reconciled_from_expired_at_drain"] = True
        return reconciled

    return terminal_packet_outcomes(
        attempts,
        bounded_deliveries,
        bounded_events,
        finalize_pending=False,
    )


def uart_fragmentation(run_dir: Path) -> dict[str, Any]:
    source = run_dir / "metrics/communication_summary.json"
    communication = read_json(source)
    transports = communication.get("by_uart", {})
    if not isinstance(transports, dict):
        transports = {}
    records = sum(
        int(row.get("records_encoded", 0) or 0)
        for row in transports.values()
        if isinstance(row, dict)
    )
    fragments = sum(
        int(row.get("chunks_encoded", 0) or 0)
        for row in transports.values()
        if isinstance(row, dict)
    )
    serial_bytes = sum(
        int(row.get("uart_input_bytes", 0) or 0)
        for row in transports.values()
        if isinstance(row, dict)
    )
    return {
        "source_file": str(source),
        "source_file_present": source.is_file(),
        "evidence_available": records > 0,
        "serial_records": records,
        "bsf1_fragments": fragments,
        "fragments_per_serial_record": fragments / records if records else None,
        "mean_serial_record_bytes": serial_bytes / records if records else None,
    }


def environment_provenance(run_dir: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        lines = (run_dir / "environment.txt").read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
    except OSError:
        return result
    for line in lines:
        key, separator, value = line.partition("=")
        if separator and key:
            result[key] = value
    return result


def profile_radio_queries(run_dir: Path, start_ns: int, end_ns: int) -> dict[str, int]:
    rows = 0
    queries: set[tuple[str, int]] = set()
    try:
        stream = (run_dir / "metrics/radio_links.csv").open(
            encoding="utf-8", errors="replace", newline=""
        )
    except OSError:
        return {"query_count": 0, "link_rows": 0}
    with stream:
        for row in csv.DictReader(stream):
            try:
                observed = int(row.get("query_started_monotonic_ns", 0) or 0)
            except ValueError:
                continue
            if not start_ns <= observed <= end_ns:
                continue
            rows += 1
            queries.add((str(row.get("query_index") or ""), observed))
    return {"query_count": len(queries), "link_rows": rows}


def provider_mode_from_log(run_dir: Path) -> str | None:
    try:
        text = (run_dir / "logs/sionna_provider.log").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return None
    for line in text.splitlines():
        if "listening" not in line:
            continue
        for token in line.split():
            if token.startswith("mode="):
                return token.partition("=")[2] or None
    return None


def runtime_context(
    run_dir: Path,
    *,
    start_ns: int,
    end_ns: int,
    window_events: list[dict[str, Any]],
) -> dict[str, Any]:
    summary = read_json(run_dir / "metrics/summary.json")
    radio = summary.get("radio", {}) if isinstance(summary, dict) else {}
    no_bypass = summary.get("no_bypass", {}) if isinstance(summary, dict) else {}
    stop_probe = read_json(run_dir / "metrics/ns3_stopped_probe.json")
    engine = read_json(run_dir / "logs/ns3_packet_engine_config.json")
    resolved = engine.get("resolved", {}) if isinstance(engine, dict) else {}
    if not isinstance(resolved, dict):
        resolved = {}
    provider_mode = (
        radio.get("provider_mode") if isinstance(radio, dict) else None
    ) or provider_mode_from_log(run_dir)
    profile_queries = profile_radio_queries(run_dir, start_ns, end_ns)
    applied_event_count = sum(
        1
        for row in window_events
        if isinstance(row.get("radio_applied_state_id"), str)
        and bool(row.get("radio_applied_state_id"))
    )
    engine_sionna_enabled = bool(resolved.get("sionna_ipc_enabled"))
    global_query_count = (
        int(radio.get("query_count", 0) or 0) if isinstance(radio, dict) else 0
    )
    profile_coupling_evidence = (
        profile_queries["query_count"] > 0 or applied_event_count > 0
    )
    coupling_enabled = (
        provider_mode == "real_sionna"
        and engine_sionna_enabled
        and profile_coupling_evidence
    )
    resolved_product_parameters = {
        key: resolved[key]
        for key in (
            "radio_rate",
            "strict_control_priority",
            "fair_lower_classes_per_uav",
            "ingress_protection_enabled",
            "shaping_enabled",
            "minimum_control_headroom_bps",
            "payload_admission_rate_bps",
            "additional_data_admission_rate_bps",
            "token_bucket_burst_bytes_per_uav",
            "lower_retry_limit",
            "mac_retry_limit",
            "event_log_flush_every",
            "event_log_flush_max_delay_ms",
            "sionna_ipc_enabled",
        )
        if key in resolved
    }
    try:
        configured_capacity_bps = data_rate_bps(str(resolved["radio_rate"]))
        lower_admission_total_bps = int(resolved["payload_admission_rate_bps"]) + int(
            resolved["additional_data_admission_rate_bps"]
        )
        minimum_control_headroom_bps = int(
            resolved["minimum_control_headroom_bps"]
        )
    except (ConfigError, KeyError, TypeError, ValueError):
        configured_capacity_bps = None
        lower_admission_total_bps = None
        minimum_control_headroom_bps = None
    actual_static_headroom_bps = (
        configured_capacity_bps - lower_admission_total_bps
        if isinstance(configured_capacity_bps, int)
        and isinstance(lower_admission_total_bps, int)
        else None
    )
    summary_no_bypass = (
        bool(no_bypass.get("ns3_stop_breaks_control_exchange"))
        if isinstance(no_bypass, dict)
        else False
    )
    probe_no_bypass = all(
        stop_probe.get(key) is True
        for key in (
            "exchange_stopped",
            "new_command_response_stopped",
            "reverse_telemetry_stopped",
        )
    )
    return {
        "medium_access": {
            "mode": "centralized_priority_scheduler_over_csma_channel",
            "arbitration_mode": "centralized_priority_scheduler",
            "transport_medium": "ns3_csma_channel",
            "collisions_expected": False,
            "non_preemptive_current_frame": True,
        },
        "profile_rtf_status": "unmeasured",
        "gazebo_rtf_scope": "unmeasured",
        "configured_capacity_bps": configured_capacity_bps,
        "lower_admission_total_bps": lower_admission_total_bps,
        "actual_static_headroom_bps": actual_static_headroom_bps,
        "minimum_control_headroom_bps": minimum_control_headroom_bps,
        "sionna_coupling_enabled": coupling_enabled,
        "sionna_query_count": profile_queries["query_count"],
        "sionna_profile_evidence": {
            "provider_mode": provider_mode,
            "engine_sionna_ipc_enabled": engine_sionna_enabled,
            "profile_window_query_count": profile_queries["query_count"],
            "profile_window_link_rows": profile_queries["link_rows"],
            "profile_window_events_with_applied_state": applied_event_count,
            "run_summary_query_count": global_query_count,
        },
        "no_bypass": summary_no_bypass or probe_no_bypass,
        "no_bypass_scope": "full_run",
        "not_profile_local": True,
        "provenance": {
            "environment": environment_provenance(run_dir),
            "engine_contract": engine.get("contract"),
            "engine_config_sha256": engine.get("config_sha256"),
            "engine_source_sha256": engine.get("source_sha256", {}),
            "resolved_product_parameters": resolved_product_parameters,
            "summary_present": (run_dir / "metrics/summary.json").is_file(),
        },
    }


def positive_lag_attribution(
    window_events: list[dict[str, Any]],
) -> dict[str, dict[str, int | float]]:
    """Attribute positive lag growth to the previously sampled event.

    PacketEventLogger samples scheduler lag before serializing the current row.
    A positive increase at the next sample therefore contains the previous
    event's serialization/callback cost.  This remains an attribution rather
    than an exact profiler, so negative catch-up intervals are deliberately not
    netted against positive backlog creation.
    """

    positive_ns: Counter[str] = Counter()
    positive_samples: Counter[str] = Counter()
    opportunities: Counter[str] = Counter()
    same_sim_positive_ns: Counter[str] = Counter()
    same_sim_positive_samples: Counter[str] = Counter()
    previous: dict[str, Any] | None = None
    for row in window_events:
        if previous is None:
            previous = row
            continue
        previous_lag = previous.get("scheduler_lag_ns")
        current_lag = row.get("scheduler_lag_ns")
        if not isinstance(previous_lag, (int, float)) or not isinstance(
            current_lag, (int, float)
        ):
            previous = row
            continue
        event = str(previous.get("event") or "unknown")
        opportunities[event] += 1
        increase = int(current_lag) - int(previous_lag)
        if increase > 0:
            positive_ns[event] += increase
            positive_samples[event] += 1
            previous_sim = previous.get("sim_time_ns")
            current_sim = row.get("sim_time_ns")
            if isinstance(previous_sim, (int, float)) and isinstance(
                current_sim, (int, float)
            ) and int(previous_sim) == int(current_sim):
                same_sim_positive_ns[event] += increase
                same_sim_positive_samples[event] += 1
        previous = row

    ordered = sorted(
        positive_ns, key=lambda event: (-positive_ns[event], event)
    )[:12]
    return {
        event: {
            "sampling_opportunities": opportunities[event],
            "positive_growth_samples": positive_samples[event],
            "total_positive_lag_growth_ms": positive_ns[event] / 1e6,
            "mean_positive_lag_growth_us": positive_ns[event]
            / max(1, positive_samples[event])
            / 1e3,
            "same_sim_time_positive_samples": same_sim_positive_samples[event],
            "same_sim_time_positive_lag_growth_ms": same_sim_positive_ns[event]
            / 1e6,
        }
        for event in ordered
    }


def is_queue_residence_observation(row: dict[str, Any]) -> bool:
    age = row.get("queue_age_ns")
    if not isinstance(age, (int, float)) or float(age) < 0:
        return False
    if row.get("event") == "dequeue":
        return True
    reason = str(row.get("drop_reason") or row.get("reason") or "")
    return row.get("event") == "drop" and reason.startswith("deadline_drop_") and float(
        age
    ) > 0


def analyze_profile(
    run_dir: Path,
    profile: str,
    *,
    shaping_enabled: bool,
    gates_overall_status: bool | None = None,
) -> dict[str, Any]:
    window = profile_window_details(run_dir, profile)
    start_ns = int(window["start_monotonic_ns"])
    end_ns = int(window["end_monotonic_ns"])
    duration_s = float(window["offered_duration_s"])
    wall_interval_s = float(window["wall_interval_s"])
    offered_end_ns = start_ns + int(duration_s * 1e9)
    attempts = [
        row
        for row in read_jsonl(run_dir / "logs/communication_attempts.jsonl")
        if row.get("profile") == profile
    ]
    deliveries = [
        row
        for row in read_jsonl(run_dir / "logs/communication_deliveries.jsonl")
        if row.get("profile") == profile
        and row.get("event") == "delivery"
        and not row.get("malformed")
        and isinstance(row.get("packet_id"), str)
    ]
    attempts_by_id = {str(row["packet_id"]): row for row in attempts}
    attempt_ids = set(attempts_by_id)
    attempts_inside_offered_window = {
        packet_id
        for packet_id, row in attempts_by_id.items()
        if isinstance(row.get("attempted_monotonic_ns"), (int, float))
        and start_ns <= int(row["attempted_monotonic_ns"]) <= offered_end_ns
    }
    attempts_with_unknown_time = {
        packet_id
        for packet_id, row in attempts_by_id.items()
        if not isinstance(row.get("attempted_monotonic_ns"), (int, float))
    }
    attempts_after_offered_window = (
        attempt_ids - attempts_inside_offered_window - attempts_with_unknown_time
    )
    hashes_to_ids: dict[str, set[str]] = defaultdict(set)
    for packet_id, row in attempts_by_id.items():
        for digest in row.get("fragment_hashes", []):
            if isinstance(digest, str):
                hashes_to_ids[digest].add(packet_id)

    full_events: list[dict[str, Any]] = []
    full_event_packet_ids: list[set[str]] = []
    window_events: list[dict[str, Any]] = []
    window_matched_events: list[dict[str, Any]] = []
    window_matched_packet_ids: list[set[str]] = []
    matched_without_bounded_timestamp = 0
    for row in read_jsonl(run_dir / "logs/ns3_packet_events.jsonl"):
        observed = int(row.get("host_monotonic_ns", 0) or 0)
        inside_window = observed > 0 and start_ns <= observed <= end_ns
        if inside_window:
            window_events.append(row)
        digest = row.get("transport_payload_sha256")
        ids = hashes_to_ids.get(str(digest), set()) if digest else set()
        if ids:
            full_events.append(row)
            full_event_packet_ids.append(ids)
            if inside_window:
                window_matched_events.append(row)
                window_matched_packet_ids.append(ids)
            elif observed == 0:
                matched_without_bounded_timestamp += 1

    # Terminal state uses the runner's authoritative bounded snapshot when it
    # exists.  Historical runs are reconstructed only from bounded rows.
    outcomes = terminal_records(
        run_dir,
        profile,
        attempts,
        deliveries,
        window_matched_events,
        start_ns=start_ns,
        end_ns=end_ns,
    )
    delivered_ids = {
        str(row["packet_id"])
        for row in deliveries
        if str(row["packet_id"]) in attempts_by_id
    }

    engine = read_json(run_dir / "logs/ns3_packet_engine_config.json")
    resolved = engine.get("resolved", {}) if isinstance(engine, dict) else {}
    if not isinstance(resolved, dict):
        resolved = {}
    supports_explicit_admit = "ingress_protection_enabled" in resolved or any(
        row.get("event") == "admit" for row in full_events
    )

    def observed_packet_ids(
        rows: list[dict[str, Any]],
        row_ids: list[set[str]],
        event_name: str,
    ) -> set[str]:
        result: set[str] = set()
        for row, ids in zip(rows, row_ids):
            if row.get("event") == event_name:
                result.update(ids)
        return result

    full_admitted_ids = observed_packet_ids(
        full_events, full_event_packet_ids, "admit"
    )
    admitted_ids = observed_packet_ids(
        window_matched_events, window_matched_packet_ids, "admit"
    )
    full_ingress_ids = observed_packet_ids(
        full_events, full_event_packet_ids, "ingress"
    )
    ingress_ids = observed_packet_ids(
        window_matched_events, window_matched_packet_ids, "ingress"
    )
    full_enqueued_ids = observed_packet_ids(
        full_events, full_event_packet_ids, "enqueue"
    )
    enqueued_ids = observed_packet_ids(
        window_matched_events, window_matched_packet_ids, "enqueue"
    )

    full_event_counts = Counter(
        str(row.get("event") or "unknown") for row in full_events
    )
    window_event_counts = Counter(
        str(row.get("event") or "unknown") for row in window_matched_events
    )
    statuses = Counter(str(row.get("status") or "pending") for row in outcomes.values())
    lag_ms = [
        float(row["scheduler_lag_ns"]) / 1e6
        for row in window_events
        if isinstance(row.get("scheduler_lag_ns"), (int, float))
    ]
    queue_ms: dict[str, list[float]] = defaultdict(list)
    full_queue_ms: dict[str, list[float]] = defaultdict(list)
    for row in full_events:
        if is_queue_residence_observation(row):
            full_queue_ms[str(row.get("traffic_class"))].append(
                float(row["queue_age_ns"]) / 1e6
            )
    for row in window_matched_events:
        if is_queue_residence_observation(row):
            queue_ms[str(row.get("traffic_class"))].append(
                float(row["queue_age_ns"]) / 1e6
            )

    backoffs_by_packet: Counter[str] = Counter()
    window_backoffs_by_packet: Counter[str] = Counter()
    backoffs_by_class_packet: dict[str, Counter[str]] = {
        name: Counter() for name in CLASSES
    }
    window_backoffs_by_class_packet: dict[str, Counter[str]] = {
        name: Counter() for name in CLASSES
    }
    for row, ids in zip(full_events, full_event_packet_ids):
        if row.get("event") == "backoff":
            traffic_class = str(row.get("traffic_class") or "")
            for packet_id in ids:
                backoffs_by_packet[packet_id] += 1
                if traffic_class in backoffs_by_class_packet:
                    backoffs_by_class_packet[traffic_class][packet_id] += 1
    for row, ids in zip(window_matched_events, window_matched_packet_ids):
        if row.get("event") == "backoff":
            traffic_class = str(row.get("traffic_class") or "")
            for packet_id in ids:
                window_backoffs_by_packet[packet_id] += 1
                if traffic_class in window_backoffs_by_class_packet:
                    window_backoffs_by_class_packet[traffic_class][packet_id] += 1
    cpu = [
        float(row["cpu_percent_one_core"])
        for row in read_jsonl(run_dir / "logs/runtime_resources.jsonl")
        if row.get("component") == "ns3_packet_engine"
        and start_ns <= int(row.get("monotonic_ns", 0) or 0) <= end_ns
        and isinstance(row.get("cpu_percent_one_core"), (int, float))
    ]

    offered_rates = packet_rates(
        attempts_by_id,
        attempt_ids,
        offered_duration_s=duration_s,
        wall_interval_s=wall_interval_s,
    )
    offered_window_actual_rates = packet_rates(
        attempts_by_id,
        attempts_inside_offered_window,
        offered_duration_s=duration_s,
        wall_interval_s=wall_interval_s,
    )
    policy_admission_rates: dict[str, Any]
    full_policy_admission_rates: dict[str, Any]
    if supports_explicit_admit:
        policy_admission_rates = packet_rates(
            attempts_by_id,
            admitted_ids,
            offered_duration_s=duration_s,
            wall_interval_s=wall_interval_s,
        )
        full_policy_admission_rates = packet_rates(
            attempts_by_id,
            full_admitted_ids,
            offered_duration_s=duration_s,
            wall_interval_s=wall_interval_s,
        )
    else:
        policy_admission_rates = unavailable_rates()
        full_policy_admission_rates = unavailable_rates()
    policy_admission_rates.update(
        {
            "available": supports_explicit_admit,
            "measurement": "explicit_ns3_admit_event"
            if supports_explicit_admit
            else "unavailable_legacy_engine_without_admit_event",
            "shaping_policy": "enabled" if shaping_enabled else "bypassed_or_disabled",
            "full_matched_lifecycle": full_policy_admission_rates,
        }
    )
    ingress_rates = packet_rates(
        attempts_by_id,
        ingress_ids,
        offered_duration_s=duration_s,
        wall_interval_s=wall_interval_s,
    )
    ingress_rates.update(
        {
            "measurement": "unique_packets_with_ns3_ingress_event",
            "full_matched_lifecycle": packet_rates(
                attempts_by_id,
                full_ingress_ids,
                offered_duration_s=duration_s,
                wall_interval_s=wall_interval_s,
            ),
            "unobserved_offered_packets": len(attempt_ids - ingress_ids),
        }
    )
    enqueued_rates = packet_rates(
        attempts_by_id,
        enqueued_ids,
        offered_duration_s=duration_s,
        wall_interval_s=wall_interval_s,
    )
    enqueued_rates.update(
        {
            "measurement": "unique_packets_with_ns3_enqueue_event",
            "full_matched_lifecycle": packet_rates(
                attempts_by_id,
                full_enqueued_ids,
                offered_duration_s=duration_s,
                wall_interval_s=wall_interval_s,
            ),
        }
    )
    delivered_rates = packet_rates(
        attempts_by_id,
        delivered_ids,
        offered_duration_s=duration_s,
        wall_interval_s=wall_interval_s,
    )
    full_channel_wire_bytes = sum(
        int(row.get("packet_wire_size", 0) or 0)
        for row in full_events
        if row.get("event") == "channel"
    )
    window_channel_wire_bytes = sum(
        int(row.get("packet_wire_size", 0) or 0)
        for row in window_matched_events
        if row.get("event") == "channel"
    )

    latency_by_packet: dict[str, float] = {}
    for row in deliveries:
        packet_id = str(row.get("packet_id") or "")
        if packet_id not in attempts_by_id or not isinstance(
            row.get("latency_ms"), (int, float)
        ):
            continue
        latency_by_packet.setdefault(packet_id, float(row["latency_ms"]))

    classes: dict[str, Any] = {}
    for traffic_class in CLASSES:
        class_ids = {
            packet_id
            for packet_id, row in attempts_by_id.items()
            if row.get("traffic_class") == traffic_class
        }
        class_delivered = class_ids & delivered_ids
        class_admitted = class_ids & admitted_ids
        class_ingress = class_ids & ingress_ids
        class_statuses = Counter(
            str(outcomes[packet_id].get("status") or "pending")
            for packet_id in class_ids
            if packet_id in outcomes
        )
        per_uav_delivered = {
            uav: len(
                {
                    packet_id
                    for packet_id in class_delivered
                    if attempts_by_id[packet_id].get("uav") == uav
                }
            )
            for uav in UAVS
        }
        per_uav_admitted = {
            uav: len(
                {
                    packet_id
                    for packet_id in class_admitted
                    if attempts_by_id[packet_id].get("uav") == uav
                }
            )
            for uav in UAVS
        }
        per_uav_ingress = {
            uav: len(
                {
                    packet_id
                    for packet_id in class_ingress
                    if attempts_by_id[packet_id].get("uav") == uav
                }
            )
            for uav in UAVS
        }
        latencies = [
            latency_by_packet[packet_id]
            for packet_id in class_delivered
            if packet_id in latency_by_packet
        ]
        class_offered_rates = packet_rates(
            attempts_by_id,
            class_ids,
            offered_duration_s=duration_s,
            wall_interval_s=wall_interval_s,
        )
        class_delivered_rates = packet_rates(
            attempts_by_id,
            class_delivered,
            offered_duration_s=duration_s,
            wall_interval_s=wall_interval_s,
        )
        class_admission_rates = (
            packet_rates(
                attempts_by_id,
                class_admitted,
                offered_duration_s=duration_s,
                wall_interval_s=wall_interval_s,
            )
            if supports_explicit_admit
            else unavailable_rates()
        )
        classes[traffic_class] = {
            "offered_packets": len(class_ids),
            "admitted_packets": len(class_admitted)
            if supports_explicit_admit
            else None,
            "delivered_packets": len(class_delivered),
            "terminal_status_counts": dict(sorted(class_statuses.items())),
            "dropped_at_ingress": class_statuses["dropped_at_ingress"],
            "dropped_in_medium": class_statuses["dropped_in_medium"],
            "expired_at_drain": class_statuses["expired_at_drain"],
            "logical_terminal_pending": class_statuses["pending"],
            "pdr_from_offered": len(class_delivered) / len(class_ids)
            if class_ids
            else 0.0,
            "offered": class_offered_rates,
            "admitted": {
                **class_admission_rates,
                "available": supports_explicit_admit,
            },
            "delivered": class_delivered_rates,
            "delivered_throughput_bps": class_delivered_rates["bits_per_second"],
            "delivered_profile_wall_interval_bps": class_delivered_rates[
                "profile_wall_interval_bits_per_second"
            ],
            "latency_ms": distribution(latencies),
            "queue_delay_ms": distribution(queue_ms[traffic_class]),
            "queue_delay_ms_full_matched_lifecycle": distribution(
                full_queue_ms[traffic_class]
            ),
            "per_uav_delivered_packets": per_uav_delivered,
            "per_uav_admitted_packets": per_uav_admitted
            if supports_explicit_admit
            else None,
            "per_uav_observed_ingress_packets": per_uav_ingress,
            "jain_fairness_delivered": jain(
                [float(per_uav_delivered[uav]) for uav in UAVS]
            ),
            "jain_fairness_admitted": jain(
                [float(per_uav_admitted[uav]) for uav in UAVS]
            )
            if supports_explicit_admit
            else None,
            "no_uav_starvation": all(
                value > 0 for value in per_uav_delivered.values()
            ),
        }

    runtime = runtime_context(
        run_dir,
        start_ns=start_ns,
        end_ns=end_ns,
        window_events=window_events,
    )
    event_config_hashes = sorted(
        {
            str(row["config_sha256"])
            for row in full_events
            if isinstance(row.get("config_sha256"), str)
        }
    )
    event_epochs = sorted(
        {
            int(row["event_epoch"])
            for row in full_events
            if isinstance(row.get("event_epoch"), (int, float))
        }
    )
    engine_hash = runtime["provenance"].get("engine_config_sha256")
    declared_shaping = window["start_record"].get("shaping_enabled")
    declared_gating = window["start_record"].get("gates_overall_status")
    runtime["provenance"].update(
        {
            "event_config_sha256_values": event_config_hashes,
            "event_epoch_values": event_epochs,
            "event_config_matches_engine": bool(engine_hash)
            and event_config_hashes == [engine_hash],
            "profile_window_declared_shaping_enabled": declared_shaping
            if isinstance(declared_shaping, bool)
            else None,
            "profile_window_matches_expected_shaping": declared_shaping
            == shaping_enabled
            if isinstance(declared_shaping, bool)
            else None,
            "profile_window_declared_gates_overall_status": declared_gating
            if isinstance(declared_gating, bool)
            else None,
            "profile_window_matches_configured_gating": declared_gating
            == gates_overall_status
            if isinstance(declared_gating, bool)
            and isinstance(gates_overall_status, bool)
            else None,
        }
    )
    if profile in {"controlled_overload", "meltdown"}:
        resolved_shaping = runtime["provenance"][
            "resolved_product_parameters"
        ].get("shaping_enabled")
        if resolved_shaping is not shaping_enabled:
            raise ValueError(
                f"{profile} engine startup shaping mode is missing or mismatched "
                f"in {run_dir}: expected {shaping_enabled}, observed {resolved_shaping!r}"
            )
        if declared_shaping is not shaping_enabled:
            raise ValueError(
                f"{profile} profile-window shaping declaration is missing or mismatched "
                f"in {run_dir}: expected {shaping_enabled}, observed {declared_shaping!r}"
            )
        if declared_gating is not gates_overall_status:
            raise ValueError(
                f"{profile} profile-window gating declaration is missing or mismatched "
                f"in {run_dir}: expected {gates_overall_status}, observed {declared_gating!r}"
            )
        if not runtime["provenance"]["event_config_matches_engine"]:
            raise ValueError(
                f"{profile} event/config provenance does not match the launched engine "
                f"in {run_dir}"
            )

    result = {
        "run_id": run_dir.name,
        "profile": profile,
        "duration_s": duration_s,
        "shaping_enabled": shaping_enabled,
        "gates_overall_status": gates_overall_status
        if isinstance(gates_overall_status, bool)
        else declared_gating
        if isinstance(declared_gating, bool)
        else None,
        "time_bases": {
            "offered_window_s": duration_s,
            "profile_wall_interval_s": wall_interval_s,
            "post_offer_drain_wall_s": window["post_offer_drain_wall_s"],
            "profile_start_monotonic_ns": start_ns,
            "profile_end_monotonic_ns": end_ns,
        },
        "offered": {
            **offered_rates,
            "measurement": "all application attempt records normalized by the scheduled offered window",
            "attempts_inside_scheduled_offered_window": len(
                attempts_inside_offered_window
            ),
            "attempts_after_scheduled_offered_window": len(
                attempts_after_offered_window
            ),
            "attempts_with_unknown_timestamp": len(attempts_with_unknown_time),
            "scheduled_window_actual": offered_window_actual_rates,
        },
        "admitted": dict(policy_admission_rates),
        "policy_admission": dict(policy_admission_rates),
        "observed_ns3_ingress": ingress_rates,
        "queue_enqueued": enqueued_rates,
        "queue_enqueued_packets": len(enqueued_ids),
        "application_delivered": {
            **delivered_rates,
            "measurement": "unique application deliveries; rates expose both time bases",
        },
        "channel_wire": {
            "measurement": "matched channel events inside the bounded profile wall interval",
            "bounded_window_bytes": window_channel_wire_bytes,
            "offered_window_normalized_bits_per_second": window_channel_wire_bytes
            * 8.0
            / duration_s,
            "profile_wall_interval_bits_per_second": window_channel_wire_bytes
            * 8.0
            / wall_interval_s,
            "full_matched_lifecycle_bytes": full_channel_wire_bytes,
        },
        "channel_wire_bits_per_second": window_channel_wire_bytes
        * 8.0
        / duration_s,
        "terminal": {
            "status_counts": dict(sorted(statuses.items())),
            "dropped_at_ingress": statuses["dropped_at_ingress"],
            "dropped_in_medium": statuses["dropped_in_medium"],
            "expired_at_drain": statuses["expired_at_drain"],
            "logical_terminal_pending": statuses["pending"],
            "reconciled_from_expired_at_drain": sum(
                bool(row.get("reconciled_from_expired_at_drain"))
                for row in outcomes.values()
            ),
            "logical_all_packets_terminal": len(outcomes) == len(attempts)
            and statuses["pending"] == 0,
            "accounting_cutoff_monotonic_ns": end_ns,
            "physical_ns3_queue_state_measured": False,
            "physical_ns3_queue_empty": None,
        },
        "classes": classes,
        "lower_class_throughput_bps": sum(
            float(classes[name]["delivered_throughput_bps"])
            for name in ("payload", "additional_data")
        ),
        "lower_class_profile_wall_interval_bps": sum(
            float(classes[name]["delivered_profile_wall_interval_bps"])
            for name in ("payload", "additional_data")
        ),
        "scheduler_lag_ms": {
            **distribution(lag_ms),
            "scope": "all ns-3 events with host timestamps inside the bounded profile window",
            "sampling": "event_weighted",
        },
        "ns3_cpu_percent_one_core": distribution(cpu),
        "queue_delay_ms_by_class": {
            name: distribution(queue_ms[name]) for name in CLASSES
        },
        "events": {
            "scope": "full matched logical-packet lifecycle regardless of bounded profile timestamp",
            "count_by_type": dict(sorted(full_event_counts.items())),
            "total": len(full_events),
            "per_offered_logical_packet": len(full_events) / len(attempts)
            if attempts
            else None,
            "per_admitted_logical_packet": len(full_events)
            / len(full_admitted_ids)
            if supports_explicit_admit and full_admitted_ids
            else None,
            "per_delivered_logical_packet": len(full_events) / len(delivered_ids)
            if delivered_ids
            else None,
            "bounded_window": {
                "count_by_type": dict(sorted(window_event_counts.items())),
                "total": len(window_matched_events),
                "per_offered_logical_packet": len(window_matched_events)
                / len(attempts)
                if attempts
                else None,
                "per_admitted_logical_packet": len(window_matched_events)
                / len(admitted_ids)
                if supports_explicit_admit and admitted_ids
                else None,
                "per_delivered_logical_packet": len(window_matched_events)
                / len(delivered_ids)
                if delivered_ids
                else None,
            },
            "outside_bounded_window": len(full_events)
            - len(window_matched_events),
            "without_host_monotonic_timestamp": matched_without_bounded_timestamp,
        },
        "scheduler_lag_attribution": positive_lag_attribution(window_events),
        "retry_backoff": {
            "scope": "full matched lifecycle",
            "events": full_event_counts["backoff"],
            "affected_logical_packets": len(backoffs_by_packet),
            "events_per_affected_packet": distribution(
                [float(value) for value in backoffs_by_packet.values()]
            ),
            "bounded_window": {
                "events": window_event_counts["backoff"],
                "affected_logical_packets": len(window_backoffs_by_packet),
                "events_per_affected_packet": distribution(
                    [float(value) for value in window_backoffs_by_packet.values()]
                ),
            },
            "by_class": {
                name: {
                    "events": sum(backoffs_by_class_packet[name].values()),
                    "affected_logical_packets": len(
                        backoffs_by_class_packet[name]
                    ),
                    "events_per_affected_packet": distribution(
                        [
                            float(value)
                            for value in backoffs_by_class_packet[name].values()
                        ]
                    ),
                    "bounded_window_events": sum(
                        window_backoffs_by_class_packet[name].values()
                    ),
                }
                for name in CLASSES
            },
        },
        **runtime,
    }
    return result


def controlled_acceptance_thresholds(qos_config: dict[str, Any]) -> dict[str, float]:
    control = qos_config["classes"]["control"]
    profile = qos_config["profiles"]["controlled_overload"]
    return {
        "control_required_pdr": float(control["required_pdr"]),
        "control_max_p95_latency_ms": float(control["max_p95_latency_ms"]),
        "scheduler_lag_max_p95_ms": float(profile["max_scheduler_lag_p95_ms"]),
    }


def acceptance(
    summary: dict[str, Any], qos_config: dict[str, Any] | None = None
) -> dict[str, bool]:
    if qos_config is None:
        qos_config = load_qos()
    thresholds = controlled_acceptance_thresholds(qos_config)
    classes = summary["classes"]
    control = classes["control"]
    lag = summary["scheduler_lag_ms"].get("p95")
    cpu_samples = summary.get("ns3_cpu_percent_one_core", {}).get("count")
    queue_distributions = summary.get("queue_delay_ms_by_class", {})
    return {
        "control_pdr_at_least_0_99": float(control["pdr_from_offered"])
        >= thresholds["control_required_pdr"],
        "control_p95_latency_at_most_250_ms": isinstance(
            control["latency_ms"].get("p95"), (int, float)
        )
        and float(control["latency_ms"]["p95"])
        <= thresholds["control_max_p95_latency_ms"],
        "scheduler_lag_p95_at_most_50_ms": isinstance(lag, (int, float))
        and float(lag) <= thresholds["scheduler_lag_max_p95_ms"],
        "ns3_cpu_samples_present": isinstance(cpu_samples, int)
        and cpu_samples > 0,
        "queue_delay_samples_present": all(
            isinstance(queue_distributions.get(name, {}).get("count"), int)
            and int(queue_distributions[name]["count"]) > 0
            for name in CLASSES
        ),
        "logical_terminal_pending_zero": int(
            summary["terminal"]["logical_terminal_pending"]
        ) == 0,
        "lower_priority_delivered": all(
            int(classes[name]["delivered_packets"]) > 0
            for name in ("payload", "additional_data")
        ),
        "no_uav_starvation": all(
            bool(classes[name]["no_uav_starvation"]) for name in CLASSES
        ),
        "no_bypass": bool(summary.get("no_bypass")),
        "sionna_coupling_enabled": bool(summary.get("sionna_coupling_enabled")),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-run", type=Path, required=True)
    parser.add_argument("--baseline-profile", default="overload")
    parser.add_argument("--controlled-run", type=Path, required=True)
    parser.add_argument("--meltdown-run", type=Path, required=True)
    parser.add_argument("--serial-baseline-run", type=Path)
    parser.add_argument("--qos-config", type=Path, default=DEFAULT_PATH)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()

    qos_path = args.qos_config.resolve()
    qos_config = load_qos(qos_path)
    controlled_config = qos_config["profiles"]["controlled_overload"]
    meltdown_config = qos_config["profiles"]["meltdown"]

    baseline = analyze_profile(
        args.baseline_run.resolve(), args.baseline_profile, shaping_enabled=False
    )
    controlled = analyze_profile(
        args.controlled_run.resolve(),
        "controlled_overload",
        shaping_enabled=bool(controlled_config["shaping_enabled"]),
        gates_overall_status=bool(controlled_config["gates_overall_status"]),
    )
    controlled["acceptance_thresholds"] = {
        "source": str(qos_path),
        **controlled_acceptance_thresholds(qos_config),
        "cpu_sample_distribution_required": True,
        "queue_delay_sample_distribution_required_for_classes": list(CLASSES),
    }
    controlled["acceptance"] = acceptance(controlled, qos_config)
    controlled["status"] = (
        "passed" if all(controlled["acceptance"].values()) else "failed"
    )
    meltdown = analyze_profile(
        args.meltdown_run.resolve(),
        "meltdown",
        shaping_enabled=bool(meltdown_config["shaping_enabled"]),
        gates_overall_status=bool(meltdown_config["gates_overall_status"]),
    )
    meltdown["characterization_only"] = (
        meltdown["gates_overall_status"] is False
        and meltdown["provenance"].get(
            "profile_window_declared_gates_overall_status"
        )
        is False
    )
    meltdown["observed_limit"] = {
        "characterization_kind": "one_point_characterization_at_maximum_tested_load",
        "saturation_threshold_found": False,
        "maximum_tested_offered_bps": meltdown["offered"]["bits_per_second"],
        "observed_application_goodput_offered_window_normalized_bps": meltdown[
            "application_delivered"
        ]["offered_window_normalized_bits_per_second"],
        "observed_application_goodput_profile_wall_interval_bps": meltdown[
            "application_delivered"
        ]["profile_wall_interval_bits_per_second"],
        "observed_channel_wire_profile_wall_interval_bps": meltdown[
            "channel_wire"
        ]["profile_wall_interval_bits_per_second"],
        "scheduler_lag_p95_ms": meltdown["scheduler_lag_ms"]["p95"],
        "interpretation": "maximum tested point only; no saturation threshold sweep was performed",
    }

    serial_baseline = (
        args.serial_baseline_run.resolve()
        if args.serial_baseline_run
        else args.baseline_run.resolve()
    )
    serial_before = uart_fragmentation(serial_baseline)
    serial_after = uart_fragmentation(args.controlled_run.resolve())
    if serial_before["evidence_available"] and serial_after["evidence_available"]:
        before_ratio = float(serial_before["fragments_per_serial_record"])
        after_ratio = float(serial_after["fragments_per_serial_record"])
        if max(before_ratio, after_ratio) <= 1.0:
            serial_decision = (
                "aggregation remains disabled: BSF1 has no fragmentation fan-out "
                f"({before_ratio:.6g} -> {after_ratio:.6g} fragments/record), and the "
                "hash-matched overload lag attribution contains no BSF1 lifecycle; "
                "this does not claim that individual serial records are large"
            )
        else:
            serial_decision = (
                "aggregation is configured disabled, but factual evidence contains "
                f"more than one fragment/record ({before_ratio:.6g} -> {after_ratio:.6g}); "
                "the aggregation decision requires review"
            )
    elif serial_before["evidence_available"]:
        before_ratio = float(serial_before["fragments_per_serial_record"])
        serial_decision = (
            "aggregation remains disabled because baseline evidence is non-excessive; "
            "controlled-run serial after-evidence is unavailable, so no before/after "
            "claim is made"
            if before_ratio <= 1.0
            else "baseline shows more than one fragment/record, but controlled-run "
            "after-evidence is unavailable; no aggregation efficacy claim is made"
        )
    else:
        serial_decision = (
            "serial fragmentation evidence is unavailable; aggregation remains "
            "disabled and no fragments-per-record claim is made"
        )

    baseline_lag_attribution = baseline.get("scheduler_lag_attribution", {})
    dominant_previous_event = next(iter(baseline_lag_attribution), None)
    dominant_lag_evidence = (
        baseline_lag_attribution.get(dominant_previous_event, {})
        if dominant_previous_event
        else {}
    )
    root_primary = (
        "baseline positive scheduler-lag growth is dominated by work following "
        f"the {dominant_previous_event!r} callback; see the attributed positive "
        "growth and same-simulation-time evidence"
        if dominant_previous_event
        else "not established: the baseline has no usable bounded scheduler-lag samples"
    )
    baseline_resolved = baseline.get("provenance", {}).get(
        "resolved_product_parameters", {}
    )
    legacy_unbounded_retry = "lower_retry_limit" not in baseline_resolved
    baseline_backoff_events = int(baseline["retry_backoff"]["events"])
    retry_amplifier = (
        "legacy baseline used the vendored ns-3 SetBackoffParams fourth/fifth "
        "argument mismatch, leaving the old fifth argument as maxRetries=1000000; "
        f"{baseline_backoff_events} rich backoff lifecycle rows amplified the backlog"
        if legacy_unbounded_retry and baseline_backoff_events > 0
        else "the supplied baseline does not prove the legacy unbounded-retry amplifier"
    )
    serial_not_primary = (
        "baseline BSF1 evidence is one fragment per serial record"
        if serial_before["evidence_available"]
        and serial_before["fragments_per_serial_record"] == 1.0
        else "not concluded because serial fragmentation evidence is unavailable or non-unit"
    )
    event_profile = {
        "definitions": {
            "event_count_scope": "full ns-3 lifecycle matched to offered BQO1 hashes; bounded_window is reported separately",
            "policy_admission": "unique packets with an explicit ns-3 admit event; never inferred from offered attempts",
            "observed_ingress": "unique packets with an ns-3 ingress event, independent of policy admission",
            "events_per_delivered": "full matched lifecycle events divided by unique application deliveries",
            "scheduler_lag": "host monotonic elapsed minus ns-3 simulation elapsed, sampled at every ns-3 event in the bounded window",
            "lag_attribution": "positive increase at the next pre-serialization lag sample attributed to the previous event; negative catch-up is not netted",
            "rate_time_bases": "offered_window_normalized uses configured source duration; profile_wall_interval includes bounded drain",
        },
        "root_cause": {
            "primary": root_primary,
            "mechanism": "unbounded offered traffic entered ingress/Sionna/queue work before capacity admission",
            "largest_amplifier": retry_amplifier,
            "legacy_per_event_cost": "each lifecycle callback reparsed and SHA-256-hashed the frame, serialized roughly 2 KiB of JSON, and synchronously flushed the stream",
            "dominant_previous_event_by_positive_lag_growth": dominant_previous_event,
            "dominant_previous_event_evidence": dominant_lag_evidence,
            "baseline_backoff_events": baseline_backoff_events,
            "baseline_backoff_max_per_affected_packet": baseline["retry_backoff"][
                "events_per_affected_packet"
            ]["max"],
            "serial_not_primary": serial_not_primary,
        },
        "before": baseline,
        "after": controlled,
        "changes": {
            "scheduler_lag_p95_ms_before": baseline["scheduler_lag_ms"]["p95"],
            "scheduler_lag_p95_ms_after": controlled["scheduler_lag_ms"]["p95"],
            "events_per_delivered_before": baseline["events"][
                "per_delivered_logical_packet"
            ],
            "events_per_delivered_after": controlled["events"][
                "per_delivered_logical_packet"
            ],
            "logical_terminal_pending_before": baseline["terminal"][
                "logical_terminal_pending"
            ],
            "logical_terminal_pending_after": controlled["terminal"][
                "logical_terminal_pending"
            ],
        },
        "serial": {
            "before": serial_before,
            "after": serial_after,
            "aggregation_enabled": False,
            "overload_event_scope": (
                "BQO1 logical-packet hashes only; BSF1 serial records are excluded "
                "from the scheduler-lag attribution"
            ),
            "decision": serial_decision,
        },
    }

    output_dir = (
        args.output_dir.resolve()
        if args.output_dir
        else args.controlled_run.resolve() / "metrics"
    )
    write_json(output_dir / "controlled_overload_summary.json", controlled)
    write_json(output_dir / "meltdown_characterization.json", meltdown)
    write_json(output_dir / "event_profile.json", event_profile)
    print(
        json.dumps(
            {
                "controlled_status": controlled["status"],
                "controlled_run": controlled["run_id"],
                "meltdown_run": meltdown["run_id"],
                "output_dir": str(output_dir),
            },
            sort_keys=True,
        )
    )
    return 0 if controlled["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
