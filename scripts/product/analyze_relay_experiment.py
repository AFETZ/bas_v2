#!/usr/bin/env python3
"""Analyze observed BAS relay runs and prepare a paired campaign; never run SITL."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import copy
import hashlib
import importlib.util
import itertools
import json
import math
from pathlib import Path
import random
import statistics
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

UAVS = tuple(range(1, 6))
CONDITIONS = ("none", "selective", "total_GCS")
ENGINEERING_DELIVERY_V1 = dict(criterion_set="engineering-delivery-v1", deadline_ms=250.,
    latency_p95_max_ms=100., latency_p99_max_ms=150., jitter_p95_max_ms=50.,
    jitter_p99_max_ms=100., timely_per_offered_min=.999, minimum_samples_per_uav=10000,
    minimum_distinct_rng_seeds=10, runtime_lag_p99_max_ms=20., runtime_lag_observed_max_ms=50.,
    runtime_lag_sample_period_ms=50., runtime_lag_host_gap_max_ms=100.,
    runtime_lag_sim_period_tolerance_ms=.001)


def finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def identifier(value: Any) -> int | None:
    text = str(value).removeprefix("uav")
    return int(text) if text.isdigit() and int(text) in UAVS else None


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def read_jsonl(path: Path, errors: list[str]) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    result = []
    with path.open() as stream:
        for line_number, line in enumerate(stream, 1):
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("not an object")
                result.append(row)
            except ValueError:
                errors.append(f"{path.name}:{line_number}: malformed JSONL record")
    return result


def read_csv(path: Path, *, evidence=None, keep=None) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    if evidence is not None:
        before = path.stat()
        digest, size = hashlib.sha256(), 0
        with path.open('rb') as stream:
            def lines():
                nonlocal size
                for line in stream:
                    digest.update(line)
                    size += len(line)
                    yield line.decode('utf-8')
            records, rows, malformed = [], 0, 0
            for row in csv.DictReader(lines()):
                rows += 1
                malformed += None in row or None in row.values()
                if keep is None or keep(row):
                    records.append(row)
        after = path.stat()
        evidence.update(sha256=digest.hexdigest(), bytes=size, rows=rows, retained_rows=len(records),
            malformed_rows=malformed,
            stable=(before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)
                and size == after.st_size)
        return records
    with path.open(newline="") as stream:
        return [row for row in csv.DictReader(stream) if keep is None or keep(row)]


def native_analysis_event(row):
    """Only these native observations enter relay timing, faults or refined gates.

    Filtering changes memory use only: the reader still hashes/counts/validates
    every original CSV row, including unretained PHY/MAC traffic.
    """
    return (row.get('event') in {'fault_start', 'fault_end', 'realtime_lag', 'jammer_on', 'jammer_off'}
        or row.get('event') == 'spectrum_signal_arrival'
        and 'foreign_signal=1' in str(row.get('details', '')).split(';'))


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, allow_nan=False, indent=2, sort_keys=True) + "\n")


def target(row: dict[str, Any]) -> tuple[float, float, float] | None:
    point = row.get("target_position_m")
    if (row.get("target_frame") == "ENU" and isinstance(point, (list, tuple))
            and len(point) == 3 and all(finite(value) is not None for value in point)):
        return tuple(float(value) for value in point)
    return None


def recovery(point_ns: float | None, fault_ns: float | None, end_ns: float | None,
             *, applicable: bool = True, restricted_s: float = 30.) -> dict[str, Any]:
    if not applicable:
        return dict(status="not_applicable_no_fault", recovered=None, time_s=None,
                    censor_time_s=None, restricted_unrecovered_time_s=None)
    if fault_ns is None or end_ns is None or end_ns < fault_ns:
        return dict(status="not_recorded", recovered=None, time_s=None,
                    censor_time_s=None, restricted_unrecovered_time_s=None)
    observed = (end_ns - fault_ns) / 1e9
    succeeded = point_ns is not None and fault_ns <= point_ns <= end_ns
    duration = (point_ns - fault_ns) / 1e9 if succeeded else None
    # This bounded loss is not a substituted successful recovery time. It is
    # available only when the whole predeclared horizon has been observed, or
    # recovery itself was observed within that horizon.
    bounded = (min(duration, restricted_s) if succeeded else restricted_s
               if observed >= restricted_s else None)
    return dict(status="observed" if succeeded else "right_censored", recovered=succeeded,
                time_s=duration, censor_time_s=None if succeeded else observed,
                restricted_horizon_s=restricted_s, restricted_unrecovered_time_s=bounded,
                recovered_within_restricted_horizon=duration <= restricted_s if succeeded else False
                    if observed >= restricted_s else None)


def distribution(values: list[float]) -> dict[str, Any]:
    def percentile(percent):
        if not values:
            return None
        ordered = sorted(values)
        at = (len(ordered) - 1) * percent / 100
        low, high = math.floor(at), math.ceil(at)
        return ordered[low] + (ordered[high] - ordered[low]) * (at - low)
    return dict(samples=len(values), mean=statistics.fmean(values) if values else None,
                p5=percentile(5), p50=percentile(50), p95=percentile(95), p99=percentile(99),
                min=min(values) if values else None, max=max(values) if values else None)


def runtime_timing_evidence(meta, native, stats):
    """Qualify the observed scheduler samples, never physical clock accuracy."""
    limits = ENGINEERING_DELIVERY_V1
    start = meta.get('measurement_start_host_monotonic_ns')
    duration = meta.get('measurement_duration_wall_s')
    end = meta.get('measurement_end_host_monotonic_ns')
    window_valid = type(start) is int and 0 <= start < 2**63
    duration_valid = (type(duration) in (int, float) and finite(duration) is not None and duration > 0
                      and window_valid and duration <= (2**63 - 1 - start) / 1e9)
    basis = 'measurement_end_host_monotonic_ns'
    if end is None:
        end = start + round(duration * 1e9) if window_valid and duration_valid else None
        basis = 'measurement_start_host_monotonic_ns + rounded recorded wall duration in ns'
    window_valid = bool(window_valid and type(end) is int and start < end < 2**63)
    if window_valid and duration_valid and abs(end - start - round(duration * 1e9)) > 1:
        window_valid = False
    problems = [] if window_valid else ['exact_recorded_host_window_missing_or_invalid']
    records, unplaced = [], 0
    for row in native:
        if row.get('event') != 'realtime_lag':
            continue
        timestamp = row.get('wall_monotonic_ns')
        if not (type(timestamp) is int and timestamp >= 0 or
                isinstance(timestamp, str) and timestamp.isascii() and timestamp.isdigit()):
            unplaced += 1
            continue
        records.append((int(timestamp), finite(row.get('time_s')), finite(row.get('value'))))
    if unplaced:
        problems.append('unplaceable_lag_timestamps')
    selected = [r for r in records if start <= r[0] < end] if window_valid else []
    before = max((r for r in records if r[0] <= start), key=lambda r: r[0], default=None) if window_valid else None
    after = min((r for r in records if r[0] >= end), key=lambda r: r[0], default=None) if window_valid else None
    coverage = [r for r in records if before is not None and after is not None and before[0] <= r[0] <= after[0]]
    valid = [r for r in selected if r[1] is not None and r[1] >= 0 and r[2] is not None and r[2] >= 0]
    if not selected:
        problems.append('measurement_lag_samples_missing')
    if len(valid) != len(selected):
        problems.append('invalid_measurement_lag_samples')
    if before is None or after is None:
        problems.append('measurement_boundary_coverage_censored')
    host_gaps = [(b[0] - a[0]) / 1e6 for a, b in zip(coverage, coverage[1:])]
    sim_gaps = [(b[1] - a[1]) * 1e3 for a, b in zip(coverage, coverage[1:])
                if a[1] is not None and b[1] is not None]
    if any(r[1] is None or r[1] < 0 for r in coverage):
        problems.append('invalid_sampler_simulation_timestamps')
    if any(gap <= 0 for gap in host_gaps):
        problems.append('sampler_host_clock_not_strictly_increasing')
    if any(gap > limits['runtime_lag_host_gap_max_ms'] for gap in host_gaps):
        problems.append('sampler_host_gap_exceeded')
    if any(not math.isclose(gap, limits['runtime_lag_sample_period_ms'], rel_tol=0,
                           abs_tol=limits['runtime_lag_sim_period_tolerance_ms']) for gap in sim_gaps):
        problems.append('sampler_simulation_period_gap_or_reset')
    lag = distribution([r[2] for r in valid])
    complete = not problems
    checks = dict(p99=lag['p99'] <= limits['runtime_lag_p99_max_ms'] if complete else None,
                  observed_max=lag['max'] <= limits['runtime_lag_observed_max_ms'] if complete else None)
    return dict(status='not_evaluated' if not complete else 'passed' if all(checks.values()) else 'observed_limits_failed',
        eligible=complete and all(checks.values()), checks=checks, coverage_status='sampled_window_covered' if complete else 'unavailable',
        coverage_problems=problems, window=dict(start_host_monotonic_ns=start if type(start) is int else None,
            end_host_monotonic_ns=end if type(end) is int else None,
            interval='half_open', end_basis=basis), selected_sample_rows=len(selected), invalid_selected_rows=len(selected) - len(valid),
        unplaceable_timestamp_rows=unplaced, first_sample_host_monotonic_ns=selected[0][0] if selected else None,
        last_sample_host_monotonic_ns=selected[-1][0] if selected else None,
        boundary_before_host_monotonic_ns=before[0] if before else None,
        boundary_after_host_monotonic_ns=after[0] if after else None,
        lag_ms=lag, observed_max_lag_ms=lag['max'], host_sample_gaps_ms=distribution(host_gaps),
        simulation_sample_intervals_ms=distribution(sim_gaps), nominal_simulation_sample_period_ms=limits['runtime_lag_sample_period_ms'],
        recorded_runtime_hard_abort_ms=finite(stats.get('runtime_lag_max_ms')),
        clock_domain='Selection: recorded host monotonic ns. Value: native RealtimeNow minus Simulator::Now in ms.',
        scope='Observed discrete scheduler lag only; no continuous maximum, inter-device synchronization accuracy or physical UART timing accuracy is established.')


def engineering_delivery(offers, commands, uav_ids, period_s, *, observation_end_ns=None):
    """Use audited CID/UART joins, retaining every unique offered slot."""
    index = {str(row['command_id']): row for row in commands}
    result = {}
    period_ns = period_s * 1e9 if period_s is not None and period_s > 0 else None
    for uav in uav_ids:
        selected = sorted((r for r in offers if identifier(r.get('uav_id')) == uav),
            key=lambda r: finite(r.get('scheduled_host_monotonic_ns')) or finite(r.get('host_monotonic_ns')) or 0)
        counts = Counter(dict(timely=0, late=0, missing=0, unavailable=0, unverifiable=0))
        latency, jitter, seen = [], [], set()
        previous = None
        excluded = Counter()
        censored = 0
        for offer in selected:
            cid = str(offer.get('command_id'))
            command = index.get(cid)
            delay = finite(command.get('latency_ms')) if command else None
            verified = bool(command and offer.get('request_id') and cid not in seen
                and command.get('uav_id') == uav and command.get('lease_status') == 'valid'
                and command.get('lineage_status') == 'valid' and command.get('application_chain_complete')
                and command.get('policy_allowed') and delay is not None and delay >= 0)
            category = ('unverifiable' if not offer.get('request_id') or command and cid in seen else 'unavailable' if command is None else
                        'missing' if command.get('admitted_ns') is None else 'unverifiable' if not verified else
                        'timely' if delay <= ENGINEERING_DELIVERY_V1['deadline_ms'] else 'late')
            created = finite(command.get('created_ns')) if command else None
            if (category == 'missing' and created is not None and observation_end_ns is not None
                    and created + ENGINEERING_DELIVERY_V1['deadline_ms'] * 1e6 > observation_end_ns):
                category = 'unverifiable'
                censored += 1
            counts[category] += 1
            if command:
                seen.add(cid)
            scheduled = finite(offer.get('scheduled_host_monotonic_ns'))
            flow = (uav, command.get('leader_id'), command.get('epoch')) if command else None
            if verified:
                latency.append(delay)
                if previous is not None:
                    if previous[0] != flow:
                        excluded['changed_flow'] += 1
                    elif scheduled is None or previous[1] is None or period_ns is None:
                        excluded['unrecorded_schedule'] += 1
                    elif not math.isclose(scheduled - previous[1], period_ns, rel_tol=0, abs_tol=1.):
                        excluded['nonconsecutive_offered_slots'] += 1
                    else:
                        jitter.append(abs(delay - previous[2]))
                previous = (flow, scheduled, delay)
            else:
                previous = None  # Never bridge a missing/unavailable/unverifiable offer.
                excluded['undelivered_or_unverifiable_slot'] += 1
        result[str(uav)] = dict(offered=len(selected), **counts, right_censored_deadline_slots=censored,
            timely_per_offered=counts['timely'] / len(selected) if selected else None,
            created_to_uart_latency_ms=distribution(latency), consecutive_delay_variation_ms=distribution(jitter),
            excluded_jitter_slots_or_pairs=dict(excluded))
    return result


def delivery_limit_checks(metrics):
    limits = ENGINEERING_DELIVERY_V1
    values = {'latency_p95': (metrics['created_to_uart_latency_ms']['p95'], limits['latency_p95_max_ms']),
              'latency_p99': (metrics['created_to_uart_latency_ms']['p99'], limits['latency_p99_max_ms']),
              'jitter_p95': (metrics['consecutive_delay_variation_ms']['p95'], limits['jitter_p95_max_ms']),
              'jitter_p99': (metrics['consecutive_delay_variation_ms']['p99'], limits['jitter_p99_max_ms'])}
    checks = {name: value <= high if value is not None else None for name, (value, high) in values.items()}
    timely = metrics['timely_per_offered']
    checks['timely_per_offered'] = timely >= limits['timely_per_offered_min'] if timely is not None else None
    return checks


def engineering_delivery_series(records):
    """Conservative per-run limits; never average quantiles or join across runs."""
    grouped = defaultdict(list)
    for record in records:
        grouped[(record['policy'], record['fault_profile'])].append(record)
    result = []
    for (policy, fault), members in sorted(grouped.items()):
        blocks = [r.get('engineering_delivery_v1', {}) for r in members]
        rosters = {tuple(sorted(b.get('per_uav', {}))) for b in blocks if b}
        usable = bool(len(rosters) == 1 and rosters != {()} and all(
            r.get('network_eligible') and b.get('measurement_eligible') and
            b.get('runtime_timing', {}).get('eligible') is True and b.get('limits') == ENGINEERING_DELIVERY_V1
            for r, b in zip(members, blocks)))
        seeds = {r.get('actual_rng_seed') for r in members if r.get('actual_rng_seed') is not None}
        per_uav = {}
        for uav in sorted(set().union(*(set(b.get('per_uav', {})) for b in blocks))):
            observations = [b['per_uav'][uav] for b in blocks if uav in b.get('per_uav', {})]
            counts = {key: sum(m[key] for m in observations) for key in
                      ('offered', 'timely', 'late', 'missing', 'unavailable', 'unverifiable', 'right_censored_deadline_slots')}
            checks = [delivery_limit_checks(m) for m in observations]
            complete = len(observations) == len(members) and all(v is not None for c in checks for v in c.values())
            numeric = complete and all(v for c in checks for v in c.values())
            latency_n = sum(m['created_to_uart_latency_ms']['samples'] for m in observations)
            jitter_n = sum(m['consecutive_delay_variation_ms']['samples'] for m in observations)
            enough = min(latency_n, jitter_n) >= ENGINEERING_DELIVERY_V1['minimum_samples_per_uav']
            per_uav[uav] = dict(**counts, latency_samples=latency_n, jitter_samples=jitter_n,
                timely_per_offered=counts['timely'] / counts['offered'] if counts['offered'] else None,
                every_run_limits_satisfied=numeric if complete else None, sufficient_samples=enough)
        failed = any(v is False for b in blocks for m in b.get('per_uav', {}).values()
                     for v in delivery_limit_checks(m).values())
        sufficient = usable and len(seeds) >= ENGINEERING_DELIVERY_V1['minimum_distinct_rng_seeds'] and all(
            m['sufficient_samples'] and m['every_run_limits_satisfied'] for m in per_uav.values())
        result.append(dict(policy=policy, fault_profile=fault, limits=ENGINEERING_DELIVERY_V1,
            status='observed_limits_failed' if failed else 'passed' if sufficient else 'insufficient_evidence',
            comparable_measurements=usable, distinct_actual_rng_seeds=sorted(seeds), per_uav=per_uav,
            runtime_timing_eligible=all(b.get('runtime_timing', {}).get('eligible') is True for b in blocks),
            percentile_policy='Every comparable run must satisfy the limits; quantiles are not averaged or pooled.'))
    return result


def decompose_goal(completion: dict[str, Any], commands: list[dict[str, Any]], fault_ns: float | None) -> dict[str, Any]:
    """A goal spans refreshes; its first CID is not its completion-refresh CID."""
    base = dict(status="unverifiable_goal_lineage", uav_id=identifier(completion.get("uav_id")),
        goal_id=completion.get("goal_id"), leader_id=identifier(completion.get("leader_id")),
        epoch=completion.get("admitted_epoch", completion.get("epoch")),
        completion_refresh_CID=completion.get("admitted_command_id", completion.get("command_id")))
    created, finished = finite(completion.get("goal_created_host_monotonic_ns")), finite(completion.get("host_monotonic_ns"))
    if fault_ns is None or created is None or finished is None or not fault_ns <= created <= finished:
        return base
    unlabelled = [r for r in commands if r.get("goal_id") is None and r.get("uav_id") == base["uav_id"]
        and r.get("leader_id") == base["leader_id"] and r.get("epoch") == base["epoch"]
        and r.get("target_position_m") == target(completion) and r.get("admitted_ns") is not None
        and r.get("created_ns") is not None and created <= r["created_ns"] <= r["admitted_ns"] <= finished]
    if unlabelled:
        return dict(base, reason="unlabelled same-target admissions could precede the first goal-labelled CID")
    eligible = [r for r in commands if r.get("goal_id") == base["goal_id"] and r.get("goal_created_ns") == created
        and r.get("uav_id") == base["uav_id"] and r.get("leader_id") == base["leader_id"] and r.get("epoch") == base["epoch"]
        and r.get("target_position_m") == target(completion) and r.get("lease_status") == "valid"
        and r.get("lineage_status") == "valid" and r.get("application_chain_complete") and r.get("policy_allowed")
        and r.get("created_ns") is not None and r.get("admitted_ns") is not None
        and r.get("lease_valid_from_ns") is not None and r.get("lease_valid_until_ns") is not None
        and r["lease_valid_from_ns"] <= r["admitted_ns"] < r["lease_valid_until_ns"]
        and created <= r["created_ns"] <= r["admitted_ns"] <= finished]
    latest = next((r for r in eligible if r["command_id"] == base["completion_refresh_CID"]), None)
    if not eligible or latest is None or latest.get("lease_valid_until_ns") is None or finished >= latest["lease_valid_until_ns"]:
        return base
    first = min(eligible, key=lambda r: r["admitted_ns"])
    total = (finished-fault_ns)/1e9
    phases = [(created-fault_ns)/1e9, (first["admitted_ns"]-created)/1e9, (finished-first["admitted_ns"])/1e9]
    return dict(base, status="observed", first_admitted_goal_CID=first["command_id"],
        fault_host_ns=fault_ns, goal_created_host_ns=created, first_goal_uart_host_ns=first["admitted_ns"],
        first_command_created_host_ns=first["created_ns"],
        completed_host_ns=finished, completion_refresh_uart_host_ns=latest["admitted_ns"],
        fault_to_goal_creation_s=phases[0], goal_creation_to_first_UART_s=phases[1], first_UART_to_completion_s=phases[2],
        goal_creation_to_first_command_creation_s=(first["created_ns"]-created)/1e9,
        first_command_creation_to_UART_s=(first["admitted_ns"]-first["created_ns"])/1e9,
        fault_to_completion_s=total, sum_error_s=sum(phases)-total,
        latest_CID_chain=dict(command_id=latest["command_id"], command_created_host_ns=latest["created_ns"],
            uart_host_ns=latest["admitted_ns"], fault_to_command_creation_s=(latest["created_ns"]-fault_ns)/1e9,
            command_creation_to_UART_s=(latest["admitted_ns"]-latest["created_ns"])/1e9,
            UART_to_completion_s=(finished-latest["admitted_ns"])/1e9, fault_to_completion_s=total),
        first_grant_sequence=first.get("grant_sequence"), completion_grant_sequence=latest.get("grant_sequence"),
        lease_scope="Both admissions verified; completion before completion-refresh expiry. Continuous received authority between refreshes is not inferred.",
        target_position_m=target(completion))


def group_goal_decomposition(completions: list[dict[str, Any]], commands: list[dict[str, Any]], fault_ns: float | None) -> dict[str, Any]:
    scopes: dict[tuple[int, int], dict[int, dict[str, Any]]] = defaultdict(dict)
    for row in sorted(completions, key=lambda r: r["host_monotonic_ns"]):
        scopes[(row["leader_id"], row.get("admitted_epoch", row.get("epoch")))].setdefault(row["uav_id"], row)
    result = {}
    for name in ("all_five", "four_followers"):
        candidates = []
        for (leader, epoch), firsts in scopes.items():
            required = set(UAVS) - ({leader} if name == "four_followers" else set())
            if required <= set(firsts):
                critical = max((firsts[i] for i in required), key=lambda r: (r["host_monotonic_ns"], r["uav_id"]))
                candidates.append((critical["host_monotonic_ns"], leader, epoch, sorted(required), critical))
        chosen = min(candidates, key=lambda row: row[:3]) if candidates else None
        result[name] = (dict(decompose_goal(chosen[4], commands, fault_ns), members=chosen[3],
            critical_uav_id=chosen[4]["uav_id"], selection="last-finishing UAV among first eligible completions under one assigned relay/epoch")
            if chosen else dict(status="not_observed", members=None, critical_uav_id=None))
    return result


def analyze_payload_load(run_dir, rows, records, start_ns, end_ns):
    """Audit planned slots separately from generated frames and RF delivery."""
    from network.coordination.payload import normalize_payload_load, payload_load_slots, payload_load_slot_ns
    config_path = run_dir / 'metrics/relay_experiment_config.json'
    config = read_json(config_path)
    starts = [row for row in rows if row.get('event') == 'payload_load_started']
    selected = [row for row in records if row.get('load_id') is not None]
    result = dict(status='not_requested', generator_eligible=None, service_classes='not_evaluated_requires_qualified_load_and_series')
    if config.get('payload_load') is None and not starts and not selected:
        return result
    result.update(status='unverifiable', generator_eligible=False, audit_errors=[])
    try:
        count = config.get('uav_count')
        if type(count) is not int or not 1 <= count <= 5 or config.get('payload_load') is None:
            raise ValueError('frozen payload_load and UAV roster are required')
        profile = normalize_payload_load(config['payload_load'], tuple(range(1, count + 1)))
        if profile != config['payload_load'] or len(starts) != 1:
            raise ValueError('one normalized frozen load and one start are required')
        # The source-time flight phase may finish slightly after its planned
        # host-time load schedule. Audit that EXACT frozen schedule; phase-loop
        # overhead must not silently lengthen the requested load denominator.
        observed_end_ns = end_ns
        duration_s = config.get('duration_s')
        if (type(start_ns) is not int or type(end_ns) is not int or not 0 <= start_ns < end_ns < 2**63
                or type(duration_s) not in (int, float) or finite(duration_s) is None
                or not 0 < duration_s <= (2**63 - 1 - start_ns) / 1e9):
            raise ValueError('exact observed window and frozen load duration are required')
        end_ns = start_ns + round(duration_s * 1e9)
        if not start_ns < end_ns <= observed_end_ns:
            raise ValueError('the complete frozen load schedule is not inside the observed window')
        begin = starts[0]
        epoch = begin.get('process_epoch')
        ground = 'payload/logs/scenario_events.jsonl'
        load_events = [row for row in rows if row.get('event') in
                       ('payload_load_started', 'payload_load_closed', 'payload_load_slots_skipped')]
        if (not isinstance(epoch, str) or not epoch or any(row.get('_path') != ground
                or row.get('run_id') != run_dir.name or type(row.get('schema')) is not int or row['schema'] != 1
                or row.get('clock_domain') != 'host_monotonic' or row.get('process_epoch') != epoch
                or type(row.get('observed_host_monotonic_ns')) is not int for row in load_events)):
            raise ValueError('load lifecycle provenance is missing or belongs to another run/epoch')
        closes = [row for row in rows if row.get('event') == 'payload_load_closed' and row.get('process_epoch') == epoch]
        if (len(closes) != 1 or begin.get('_path') != ground or closes[0].get('_path') != ground
                or begin.get('profile') != profile or begin.get('load_id') != 'payload_load'
                or begin.get('start_host_monotonic_ns') != start_ns or begin.get('end_host_monotonic_ns') != end_ns
                or type(start_ns) is not int or type(end_ns) is not int or not start_ns < end_ns):
            raise ValueError('load start/close/profile/window evidence disagrees')
        planned = payload_load_slots(profile, end_ns - start_ns)
        closed = closes[0].get('offered_load', {})
        if not isinstance(closed, dict) or not isinstance(closed.get('per_uav'), dict):
            raise ValueError('closed generator ledger is missing')
        channel = read_json(run_dir / 'metrics/payload_channel.json')
        if (channel.get('measurement_window', {}).get('start_host_monotonic_ns') != start_ns
                or channel.get('measurement_window', {}).get('end_host_monotonic_ns') != end_ns
                or type(begin.get('planned_slots_per_uav')) is not int or begin['planned_slots_per_uav'] != planned or closed.get('status') != 'closed'
                or closed.get('configured') is not True or closed.get('profile') != profile
                or type(closed.get('planned_slots_per_uav')) is not int or closed['planned_slots_per_uav'] != planned
                or begin['observed_host_monotonic_ns'] > closes[0]['observed_host_monotonic_ns']
                or closes[0]['observed_host_monotonic_ns'] < end_ns
                or closed != channel.get('offered_load')):
            raise ValueError('closed generator ledger does not match the observed trace')
        ranges = []
        for row in rows:
            if row.get('event') != 'payload_load_slots_skipped':
                continue
            first, size = row.get('first_sequence'), row.get('count')
            if (row.get('_path') != ground or row.get('process_epoch') != epoch
                    or row.get('load_id') != 'payload_load' or row.get('uav_ids') != profile['uav_ids']
                    or row.get('reason') not in ('publisher_missed_slots', 'window_ended_before_generation', 'producer_closed_before_generation')
                    or type(first) is not int or type(size) is not int or not 0 <= first < first + size <= planned):
                raise ValueError('invalid skipped-slot interval')
            ranges.append((first, first + size))
        per_uav = {}
        duration = (end_ns - start_ns) / 1e9
        for i in profile['uav_ids']:
            offers = [row for row in selected if row['uav_id'] == i]
            intervals = list(ranges)
            late = 0
            for row in offers:
                sequence = row['load_sequence']
                scheduled = payload_load_slot_ns(profile, start_ns, sequence)
                if (row['process_epoch'] != epoch or row['direction'] != 'gcs_to_uav'
                        or type(sequence) is not int or not 0 <= sequence < planned
                        or row['application_useful_bytes'] != profile['useful_bytes']
                        or row['scheduled_host_monotonic_ns'] != scheduled):
                    raise ValueError('application offer disagrees with its frozen load slot')
                intervals.append((sequence, sequence + 1))
                late += not scheduled <= row['created_host_monotonic_ns'] < min(end_ns,
                    payload_load_slot_ns(profile, start_ns, sequence + 1))
            cursor = 0
            for first, last in sorted(intervals):
                if first != cursor:
                    raise ValueError('planned load slots contain a gap or overlap')
                cursor = last
            if cursor != planned:
                raise ValueError('planned load slots are not fully accounted for')
            skipped = sum(last - first for first, last in ranges)
            sent = sum(row['transmitted_host_monotonic_ns'] is not None for row in offers)
            failed = len(offers) - sent
            ledger = closed.get('per_uav', {}).get(str(i), {})
            if (not isinstance(ledger, dict) or any(type(ledger.get(k)) is not int or ledger[k] < 0
                    for k in ('created','skipped','outside_slot','sent','send_failed'))
                    or ledger.get('created') != len(offers) or ledger.get('skipped') != skipped
                    or ledger.get('outside_slot') != late or ledger.get('sent') != sent or ledger.get('send_failed') != failed):
                raise ValueError('generator slot counters disagree with frame evidence')
            accepted = [row for row in offers if row['outcome'] in ('timely', 'late')
                        and start_ns <= row['accepted_host_monotonic_ns'] < end_ns]
            timely = sum(row['outcome'] == 'timely' for row in offers)
            per_uav[str(i)] = dict(planned=planned, created=len(offers), generator_skipped=skipped,
                outside_slot=late, requested_useful_bps=profile['useful_bitrate_bps'],
                source_not_fully_transmitted=failed, planned_useful_bps=planned * profile['useful_bytes'] * 8 / duration,
                created_useful_bps=len(offers) * profile['useful_bytes'] * 8 / duration,
                outcomes=dict(Counter(row['outcome'] for row in offers), generator_skipped=skipped),
                timely_per_planned=timely / planned, timely_per_created=timely / len(offers) if offers else None,
                latency_ms=distribution([row['latency_ms'] for row in offers if row['latency_ms'] is not None]),
                delivered_unique_inside_window=len(accepted),
                application_useful_goodput_bps=sum(row['application_useful_bytes'] for row in accepted) * 8 / duration)
        if len(selected) != sum(row['created'] for row in per_uav.values()):
            raise ValueError('application offers have an unknown UAV')
        eligible = all(row['generator_skipped'] == row['outside_slot'] == row['source_not_fully_transmitted'] == 0
                       for row in per_uav.values())
        if closed.get('generator_eligible') is not eligible:
            raise ValueError('claimed generator eligibility disagrees with actual slot coverage')
        result.update(status='recorded', generator_eligible=eligible, profile=profile, per_uav=per_uav,
            load_window=dict(start_host_monotonic_ns=start_ns, end_host_monotonic_ns=end_ns,
                basis='frozen duration_s and observed measurement start; half-open'),
            enclosing_observation_end_host_monotonic_ns=observed_end_ns,
            byte_definition='TUNNEL payload[:payload_length], excluding all protocol headers; synthetic test bytes',
            receipt_boundary='UART OS-write end; not flight-controller application acceptance',
            config_sha256=hashlib.sha256(config_path.read_bytes()).hexdigest())
    except (ValueError, TypeError, KeyError, OSError) as error:
        result['audit_errors'].append(str(error))
    return result


def analyze_payload_lineage(run_dir: Path, start_ns=None, end_ns=None, *, complete=False):
    """Audit existing BSF1/MAVLink observations; never substitute ACKs or RF power."""
    run_dir = Path(run_dir)
    errors = []
    ground = run_dir / 'payload/logs/scenario_events.jsonl'
    paths = [ground, *sorted((run_dir / 'logs').glob('payload_uart_uav*.jsonl'))]
    initial_files={path:(path.stat().st_size,path.stat().st_mtime_ns) for path in paths if path.is_file()}
    rows = [dict(row, _path=path.relative_to(run_dir).as_posix()) for path in paths
            for row in read_jsonl(path, errors)]
    sessions = defaultdict(dict)
    for row in rows:
        if row.get('event') in ('payload_lineage_started', 'payload_lineage_closed'):
            sessions[(row['_path'], row.get('process_epoch'))][row['event']] = row
    result = dict(status='not_evaluated_missing_trace', measurement_eligible=False,
        byte_definition='Actual MAVLink wire body bytes; excludes MAVLink/BSF1/IP framing and is not semantic customer video/file bytes.',
        timestamp_scope='Host UART ingress to GCS CRC parse; GCS creation to UART OS-write end. Sensor creation and physical final wire bit are not measured.',
        goodput_definition='Unique valid offers and acceptances inside the same measurement window; post-window drain receipts remain deadline outcomes but are excluded from goodput.',
        service_class_availability='not_evaluated_requires_qualified_load_and_series',
        minimum_distinct_seeds=ENGINEERING_DELIVERY_V1['minimum_distinct_rng_seeds'],
        minimum_samples_per_stream=ENGINEERING_DELIVERY_V1['minimum_samples_per_uav'],
        records=[], per_stream={}, audit_errors=errors,trace_files=[])
    if not any(path == ground.relative_to(run_dir).as_posix() for path, _epoch in sessions):
        logging=read_json(run_dir/'metrics/payload_channel.json').get('application_lineage',{})
        if isinstance(logging,dict) and logging.get('event_logging')=='metrics_only':
            result['status']='not_evaluated_logging_disabled'
        return result
    from pymavlink.dialects.v20 import ardupilotmega as mavlink
    from network.scripts.serial_transport import mavlink_payload_lineage
    offers, received, transmitted, record_sends = [], defaultdict(list), defaultdict(list), defaultdict(dict)
    excluded_foreign = invalid = 0
    raw_offers = 0

    def validate(row, direction):
        if (type(row.get('schema')) is not int or row['schema'] != 1 or row.get('run_id') != run_dir.name
                or row.get('clock_domain') != 'host_monotonic' or row.get('channel') != 'payload'
                or type(row.get('uav_id')) is not int
                or row['uav_id'] not in UAVS or not isinstance(row.get('process_epoch'), str)
                or not row['process_epoch'] or row.get('provenance_valid') is not True):
            raise ValueError('missing run/clock/UAV/process/BSF1 frame provenance')
        frame = bytes.fromhex(row['frame_hex'])
        parser = mavlink.MAVLink(None); parser.robust_parsing = True
        messages = parser.parse_buffer(frame) or []
        if len(messages) != 1 or messages[0].get_type() == 'BAD_DATA':
            raise ValueError('frame is not a single CRC-valid MAVLink message')
        refs = row['record_references']
        if (not isinstance(refs, list) or not refs or any(not isinstance(ref, dict)
                or set(ref) != {'sequence','producer_host_monotonic_ns','offset','length'}
                or any(type(ref[k]) is not int or ref[k] < 0 for k in ref) or ref['length'] == 0
                for ref in refs) or sum(ref['length'] for ref in refs) != len(frame)):
            raise ValueError('invalid original BSF1 byte spans')
        expected = mavlink_payload_lineage(messages[0], [((r['sequence'],r['producer_host_monotonic_ns']),r['offset'],r['length'])
            for r in refs], run_id=run_dir.name, uav_id=row['uav_id'], direction=direction)
        if (not expected['provenance_valid'] or any(type(row.get(key)) is not int for key in ('frame_bytes','mavlink_body_bytes'))
                or any(row.get(key) != expected[key] for key in ('frame_uid','frame_sha256','frame_bytes','mavlink_body_bytes',
                    'record_references','source_first_host_monotonic_ns','source_last_host_monotonic_ns',
                    'message_type','msgid','sysid','compid'))):
            raise ValueError('frame identity, bytes or original BSF1 references disagree')
        if 'application_load' in row:
            app = row['application_load']
            message = messages[0]
            if (not isinstance(app, dict) or set(app) != {'load_id','sequence','scheduled_host_monotonic_ns','useful_bytes','useful_sha256'}
                    or app['load_id'] != 'payload_load' or direction != 'gcs_to_uav'
                    or any(type(app[k]) is not int or app[k] < 0 for k in ('sequence','scheduled_host_monotonic_ns','useful_bytes'))
                    or not 1 <= app['useful_bytes'] <= 128 or message.get_type() != 'TUNNEL'
                    or message.target_system != row['uav_id'] or message.target_component != 1 or message.payload_type != 0
                    or message.payload_length != app['useful_bytes']
                    or hashlib.sha256(bytes(message.payload[:message.payload_length])).hexdigest() != app['useful_sha256']):
                raise ValueError('application useful bytes disagree with the actual TUNNEL frame')
        return expected

    for row in rows:
        event, stage = row.get('event'), row.get('payload_lineage_stage')
        direction = row.get('payload_direction', row.get('direction'))
        if event == 'payload_record_sent':
            keys=('uav_id','sequence','producer_host_monotonic_ns','fragment_index','fragment_count','record_bytes','record_crc32','transmitted_host_monotonic_ns')
            if (all(type(row.get(key)) is int and row[key]>=0 for key in keys)
                    and row['fragment_count']>0 and row['fragment_index']<row['fragment_count']
                    and row['transmitted_host_monotonic_ns']>=row['producer_host_monotonic_ns']):
                key = (row['uav_id'],row['sequence'],row['producer_host_monotonic_ns'])
                record_sends[key].setdefault(row['fragment_index'],row)
            else:
                invalid+=1
            continue
        offered = event == 'payload_frame_offered' or event == 'mavlink_frame' and stage == 'offered'
        accepted = event == 'payload_frame_accepted' or event == 'mavlink_frame' and stage == 'accepted'
        sent = event in ('payload_frame_sent','payload_frame_send_failed')
        if not (offered or accepted or sent):
            continue
        raw_offers += offered
        try:
            if direction not in ('uav_to_gcs','gcs_to_uav'):
                raise ValueError('unsupported payload direction')
            value = validate(row, direction)
            if offered and direction == 'uav_to_gcs' and value['sysid'] != row['uav_id']:
                excluded_foreign += 1
                continue
            if offered:
                created, deadline = row.get('created_host_monotonic_ns'), row.get('deadline_host_monotonic_ns')
                if (type(created) is not int or type(deadline) is not int or created < 0 or deadline <= created
                        or created > value['source_first_host_monotonic_ns']
                        or direction=='uav_to_gcs' and created!=value['source_first_host_monotonic_ns']):
                    raise ValueError('invalid original creation/deadline clocks')
                offers.append(dict(row, direction=direction))
            elif accepted:
                if type(row.get('accepted_host_monotonic_ns')) is not int:
                    raise ValueError('missing actual endpoint acceptance time')
                received[row['frame_uid']].append(row)
            else:
                transmitted[row['frame_uid']].append(row)
        except (ValueError, TypeError, KeyError, OverflowError) as error:
            invalid += 1
            if len(errors) < 20:
                errors.append(str(error))
    counts = Counter(row['frame_uid'] for row in offers)
    window_valid = type(start_ns) is int and type(end_ns) is int and 0 <= start_ns < end_ns
    chosen = [row for row in offers if not window_valid or start_ns <= row['created_host_monotonic_ns'] < end_ns]

    def session_interval(session):
        opened=session.get('payload_lineage_started',{}).get('observed_host_monotonic_ns')
        closed=session.get('payload_lineage_closed',{})
        ended=closed.get('observed_host_monotonic_ns')
        return ((opened,ended) if type(opened) is int and type(ended) is int
                and 0<=opened<=ended and closed.get('close_error') is None else None)

    for offer in chosen:
        uid = offer['frame_uid']; replies = sorted(received[uid], key=lambda row: row['accepted_host_monotonic_ns'])
        arrival = replies[0]['accepted_host_monotonic_ns'] if replies else None
        epoch = sessions.get((offer['_path'],offer['process_epoch']), {})
        target = (ground.relative_to(run_dir).as_posix() if offer['direction']=='uav_to_gcs'
                  else (Path('logs')/f"payload_uart_uav{offer['uav_id']}.jsonl").as_posix())
        # Never pair an old process's start with a later restart's close. A
        # receipt is usable only in its OWN continuous target epoch. Without a
        # receipt, a clean epoch must individually cover the deadline horizon.
        targets={process:interval for (path,process),value in sessions.items()
            if path==target and isinstance(process,str) and process and (interval:=session_interval(value)) is not None
            and interval[0]<=offer['created_host_monotonic_ns']<=interval[1]}
        if replies:
            target_epoch=replies[0]['process_epoch'] if replies[0]['_path']==target else None
        else:
            target_epoch=max(targets,key=lambda process:targets[process][1],default=None)
        target_interval=targets.get(target_epoch)
        horizon=target_interval[1] if target_interval else -1
        source_interval=session_interval(epoch)
        trace_complete=(source_interval is not None
            and source_interval[0]<=offer['created_host_monotonic_ns']<=source_interval[1]
            and target_interval is not None and (arrival is None or arrival<=horizon))
        tx_end = None
        if offer['direction']=='gcs_to_uav':
            tx_end = min((row['observed_host_monotonic_ns'] for row in transmitted[uid]
                if row['event']=='payload_frame_sent' and row.get('sent_fragments')==row.get('expected_fragments')), default=None)
        else:
            tx = [record_sends[(offer['uav_id'],r['sequence'],r['producer_host_monotonic_ns'])] for r in offer['record_references']]
            if all(group and set(group)==set(range(next(iter(group.values())).get('fragment_count',0)))
                    and all(row['fragment_count']==len(group) for row in group.values()) for group in tx):
                tx_end = max(row['transmitted_host_monotonic_ns'] for group in tx for row in group.values())
        outcome = ('unverifiable' if counts[uid]!=1 or not trace_complete else
            'right_censored' if arrival is None and horizon < offer['deadline_host_monotonic_ns'] else
            'unverifiable' if tx_end is None and arrival is not None else
            'not_fully_transmitted' if tx_end is None else 'missing' if arrival is None else
            'unverifiable' if arrival < max(offer['created_host_monotonic_ns'],offer['source_last_host_monotonic_ns']) else
            'timely' if arrival <= offer['deadline_host_monotonic_ns'] else 'late')
        app = offer.get('application_load', {})
        result['records'].append(dict(frame_uid=uid,uav_id=offer['uav_id'],direction=offer['direction'],
            load_id=app.get('load_id'), load_sequence=app.get('sequence'),
            application_useful_bytes=app.get('useful_bytes'), application_useful_sha256=app.get('useful_sha256'),
            scheduled_host_monotonic_ns=app.get('scheduled_host_monotonic_ns'),
            process_epoch=offer['process_epoch'],message_type=offer['message_type'],mavlink_body_bytes=offer['mavlink_body_bytes'],
            target_process_epoch=target_epoch,
            target_observation_start_host_monotonic_ns=target_interval[0] if target_interval else None,
            target_observation_end_host_monotonic_ns=target_interval[1] if target_interval else None,
            created_host_monotonic_ns=offer['created_host_monotonic_ns'],transmitted_host_monotonic_ns=tx_end,
            accepted_host_monotonic_ns=arrival,deadline_host_monotonic_ns=offer['deadline_host_monotonic_ns'],
            outcome=outcome,duplicate_receipts=max(0,len(replies)-1),
            latency_ms=(arrival-offer['created_host_monotonic_ns'])/1e6 if arrival is not None and outcome in ('timely','late') else None))
    grouped = defaultdict(list)
    for row in result['records']:
        grouped[(row['uav_id'],row['direction'],row['message_type'],row['process_epoch'])].append(row)
    duration = (end_ns-start_ns)/1e9 if window_valid else None
    for key, members in grouped.items():
        categories = Counter(row['outcome'] for row in members)
        delivered = [row for row in members if row['outcome'] in ('timely','late')]
        result['per_stream']['|'.join(map(str,key))] = dict(uav_id=key[0],direction=key[1],message_type=key[2],process_epoch=key[3],
            offered=len(members),outcomes=dict(categories),timely_per_offered=categories['timely']/len(members),
            delivered_unique=len(delivered),duplicate_receipts=sum(row['duplicate_receipts'] for row in members),
            mavlink_body_goodput_bps=sum(row['mavlink_body_bytes'] for row in delivered
                if start_ns<=row['accepted_host_monotonic_ns']<end_ns)*8/duration if duration else None,
            offered_mavlink_body_bps=sum(row['mavlink_body_bytes'] for row in members)*8/duration if duration else None,
            application_useful_goodput_bps=sum(row['application_useful_bytes'] or 0 for row in delivered
                if start_ns<=row['accepted_host_monotonic_ns']<end_ns)*8/duration if duration else None,
            latency_ms=distribution([row['latency_ms'] for row in delivered]))
    result.update(status='recorded' if window_valid and chosen else 'not_evaluated_measurement_window_or_offers',
        measurement_eligible=bool(window_valid and complete and chosen and not invalid and not errors
            and all(row['outcome'] not in ('unverifiable','right_censored') for row in result['records'])),
        excluded_foreign_source_frames=excluded_foreign,invalid_trace_rows=invalid)
    result['raw_offered_frames']=raw_offers
    result['orphan_receipts']=sum(len(value) for uid,value in received.items() if uid not in counts)
    result['measurement_eligible'] &= result['orphan_receipts']==0
    for path in paths:
        if not path.is_file():
            continue
        before=path.stat();digest=hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda:stream.read(1024*1024),b''):
                digest.update(block)
        after=path.stat()
        stable=initial_files.get(path)==(before.st_size,before.st_mtime_ns)==(after.st_size,after.st_mtime_ns)
        result['trace_files'].append(dict(path=path.relative_to(run_dir).as_posix(),sha256=digest.hexdigest(),
            bytes=after.st_size,status='observed' if stable else 'changing'))
        result['measurement_eligible'] &= stable
    result['offered_load'] = analyze_payload_load(run_dir, rows, result['records'], start_ns, end_ns)
    if result['offered_load']['generator_eligible'] is False:
        result['measurement_eligible'] = False
    return result


def refined_case_evidence(run_dir, meta, stats, environment, events, native, commands, payload, errors,
                          native_capture=None):
    """Observe the refined flight case without importing the separate SLA limits.

    Only frozen run geometry is usable. A successful process/mission summary is
    necessary but never sufficient; missing observations remain a failed gate.
    HitL remains a separate bench, even when all flight gates are satisfied.
    """
    run_dir = Path(run_dir).resolve()
    root = run_dir.parents[2]
    control_dir = root / 'runs/control' / run_dir.name
    frozen = run_dir / 'logs/urban_scenario.json'
    raw_scene = read_json(frozen)
    config = read_json(control_dir / 'config.json')
    if (raw_scene.get('scenario') != 'terrain_10km_five_uav_refined_case_v1'
            and Path(config.get('environment', {}).get('scene', '')).name != 'terrain_10km_refined_mission.json'):
        return dict(schema_version=1, status='not_applicable', applicable=False)
    import bas
    from scripts.product.urban_mission import UrbanScenario
    from scripts.product.prepare_terrain_scene import terrain_height
    from scripts.product.summarize_native_radio_five_uav import _native_source_schedule
    checks, details, failures = {}, {}, []
    result = dict(schema_version=1, applicable=True, status='incomplete', flight_case_passed=False,
        checks=checks, observations=details, evidence_errors=failures,
        hitl='separate_bench_required',
        scope='Frozen refined-case input, recorded Gazebo samples and audited packet delivery; no physical hardware or statistical SLA certification.',
        sampling_limit='AGL and collision checks describe recorded Gazebo positions, not continuous motion between samples or the whole map.')
    record = read_json(control_dir / 'run.json')
    checks['runtime_and_complete_window'] = (
        record.get('run_id') == run_dir.name and record.get('runtime_status') == 'completed'
        and record.get('mission_outcome') == 'succeeded' and record.get('measurement_validity') == 'valid'
        and not record.get('runtime_verification_errors') and not errors
        and meta.get('runtime_status') == 'completed' and meta.get('mission_outcome') == 'succeeded'
        and meta.get('measurement_validity') == 'valid' and meta.get('measurement_window_completed') is True
        and stats.get('stop_reason') in ('duration', 'sionna_ns3_process_stopped'))
    capture = read_json(run_dir / 'metrics/native_source_summary.json').get('event_capture', {})
    native_capture = native_capture or {}
    checks['native_event_capture'] = (native_capture.get('stable') is True
        and native_capture.get('malformed_rows') == 0
        and capture.get('status') == 'observed' and capture.get('bytes') == native_capture.get('bytes')
        and capture.get('sha256') == native_capture.get('sha256'))
    details['native_event_capture'] = native_capture
    checks['one_shared_radio_channel'] = (
        type(stats.get('uav_count')) is int and stats['uav_count'] == 5
        and type(stats.get('radio_node_count')) is int and stats['radio_node_count'] == 6
        and type(stats.get('shared_spectrum_channel_count')) is int and stats['shared_spectrum_channel_count'] == 1
        and stats.get('wifi_topology') == 'adhoc' and stats.get('sionna_drives_rx_psd') is True)
    valid_commands = Counter(row['uav_id'] for row in commands if row.get('lease_status') == 'valid'
        and row.get('lineage_status') == 'valid' and row.get('application_chain_complete') is True
        and row.get('policy_allowed') is True and finite(row.get('admitted_ns')) is not None)
    delivered_payload = Counter(row.get('uav_id') for row in payload.get('records', [])
        if row.get('outcome') in ('timely', 'late') and row.get('direction') == 'gcs_to_uav')
    checks['two_mavlink_uart_channels'] = (
        environment.get('active_uart_channels') == 'control,payload'
        and payload.get('status') == 'recorded' and not payload.get('audit_errors')
        and payload.get('invalid_trace_rows') == 0 and payload.get('orphan_receipts') == 0
        and bool(payload.get('trace_files')) and all(f.get('status') == 'observed' for f in payload['trace_files'])
        and all(valid_commands[i] > 0 and delivered_payload[i] > 0 for i in UAVS))
    details['delivered_control_by_uav'] = dict(valid_commands)
    details['delivered_payload_by_uav'] = dict(delivered_payload)
    checks['frozen_terrain'] = False
    checks['five_uav_flight_lifecycle'] = False
    checks['gazebo_positions_and_agl'] = False
    try:
        bundle = (root / raw_scene['terrain_bundle']).resolve()
        if bundle != (control_dir / 'generated/terrain').resolve():
            raise ValueError('terrain is not the frozen bundle of this run')
        # The initial/final simulator snapshots bind the run-local bundle and
        # mission. Never fall back to the mutable checkout .external assets.
        manifest_path = bundle / 'scene-manifest.json'
        for stage in ('initial', 'final'):
            inputs = run_dir / 'configuration' / stage / 'inputs'
            for label, path in (('geometry_source', frozen), ('sionna_geometry_manifest', manifest_path)):
                captured = inputs / label / 'value.json'
                if captured.read_bytes() != path.read_bytes():
                    raise ValueError(f'{stage} {label} differs from frozen runtime input')
        scenario = UrbanScenario.load(frozen, root=root)  # Verifies every terrain asset hash.
        manifest = scenario.terrain_manifest
        details['terrain_manifest_sha256'] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        floors = [b['storeys'] for b in manifest['building_bounds'].values()]
        checks['frozen_terrain'] = (manifest['extent_m'] == [10000, 10000]
            and 0 < manifest['relief_m'] <= 200 and bool(floors) and max(floors) == 15
            and scenario.uav_ids == UAVS)
        observed_events = defaultdict(set)
        reached = defaultdict(set)
        for row in events:
            uav, stage = row.get('uav_id'), row.get('stage', row.get('event'))
            if uav not in UAVS:
                continue
            observed_events[uav].add(stage)
            index = row.get('waypoint_index')
            position = row.get('target_position_m')
            if (stage == 'waypoint_reached_telemetry' and type(index) is int
                    and row.get('progression_source') == 'radio_delivered_LOCAL_POSITION_NED'
                    and 0 <= index < len(scenario.routes[uav]) and isinstance(position, (list, tuple))
                    and len(position) == 3 and all(finite(p) is not None for p in position)
                    and math.dist(position, scenario.routes[uav][index]) <= 1e-6):
                reached[uav].add(index)
        checks['five_uav_flight_lifecycle'] = all(
            {'takeoff_observed', 'landing_disarm_observed'} <= observed_events[i]
            and reached[i] == set(range(len(scenario.routes[i])))
            and all(meta.get('uavs', {}).get(str(i), {}).get(k) is True
                    for k in ('armed', 'takeoff', 'landed', 'auto_disarm')) for i in UAVS)
        details['waypoints_observed_by_uav'] = {str(i): sorted(reached[i]) for i in UAVS}
        journal = bas.node_state_journal_verification(run_dir)
        details['position_journal_verification'] = journal
        if journal.get('passed') is not True:
            raise ValueError('closed position journal is not hash verified')
        start, stop = meta.get('measurement_start_host_monotonic_ns'), meta.get('measurement_end_host_monotonic_ns')
        if type(start) is not int or type(stop) is not int or not start < stop:
            raise ValueError('exact measurement host window is missing')
        source_start = finite(meta.get('measurement_start_source_sim_time_s'))
        source_stop = finite(meta.get('measurement_end_source_sim_time_s'))
        if source_start is None or source_stop is None or not 0 <= source_start < source_stop:
            raise ValueError('measurement source-clock window is missing')
        session = read_json(run_dir / 'logs/node_state.json').get('session_id')
        if not isinstance(session, str) or not session:
            raise ValueError('verified position session identity is missing')
        agl = {i: [] for i in UAVS}
        seen, cp_samples, bad, collisions = {}, 0, 0, 0
        last_source = {}
        with (run_dir / 'logs/node_state.jsonl').open(encoding='utf-8') as stream:
            for line in stream:
                snapshot = json.loads(line)
                stamp = snapshot.get('published_monotonic_ns')
                if type(stamp) is not int or not start <= stamp <= stop:
                    continue
                if (snapshot.get('type') != 'node_state' or snapshot.get('schema_version') != 2
                        or snapshot.get('session_id') != session or snapshot.get('coordinate_frame') != 'ENU'
                        or snapshot.get('source') != 'gazebo_odometry' or snapshot.get('fault')):
                    bad += 1
                    continue
                nodes = snapshot.get('nodes', [])
                if ({n.get('id') for n in nodes} != {'cp', *(f'uav{i}' for i in UAVS)}
                        or len(nodes) != 6):
                    bad += 1
                    continue
                for node in nodes:
                    if node.get('id') == 'cp':
                        if node.get('role') == 'command_post' and node.get('position_m') == raw_scene['gcs']:
                            cp_samples += 1
                        continue
                    uav = identifier(node.get('id'))
                    if uav is None:
                        bad += 1
                        continue
                    point, source_s = node.get('position_m'), finite(node.get('source_sim_time_s'))
                    if (node.get('role') != 'uav' or node.get('stale') is not False or node['id'] in snapshot.get('stale_nodes', [])
                            or source_s is None or not isinstance(point, list) or len(point) != 3
                            or any(finite(p) is None for p in point)):
                        bad += 1
                        continue
                    if not source_start <= source_s < source_stop:
                        continue  # A just-published row can still contain the preceding source sample.
                    if uav in last_source and source_s < last_source[uav]:
                        bad += 1
                        continue
                    last_source[uav] = source_s
                    identity = (snapshot.get('session_id'), uav, source_s)
                    if identity in seen:
                        if seen[identity] != point:
                            bad += 1
                        continue
                    seen[identity] = point
                    agl[uav].append(point[2] - terrain_height(scenario.terrain_grid, *point[:2]))
                    collisions += sum(all(low <= value <= high for value, low, high in zip(
                        point, b['bounds_m'][0], b['bounds_m'][1])) for b in manifest['building_bounds'].values())
        details.update(agl_m_by_uav={str(i): distribution(values) for i, values in agl.items()},
            cp_samples=cp_samples, invalid_position_samples=bad, sampled_building_intersections=collisions)
        checks['gazebo_positions_and_agl'] = (cp_samples > 0 and not bad and not collisions
            and all(values and min(values) > 0 and max(values) >= 20 for values in agl.values()))
    except (ValueError, OSError, KeyError, TypeError, ImportError) as error:
        failures.append(f'geometry_or_pose: {type(error).__name__}: {error}')
    checks['separate_em_source_observed'] = False
    try:
        schedule = _native_source_schedule(run_dir)
        segments = (schedule.get('actual_sources') or {}).get('segments', [])
        if segments:
            for stage, label in (('initial', 'native_sources'), ('final', 'native_sources'),
                                 ('final', 'native_sources_actual')):
                captured = run_dir / 'configuration' / stage / 'inputs' / label / 'value.json'
                if captured.read_bytes() != (run_dir / 'logs' / (label + '.json')).read_bytes():
                    raise ValueError(f'{stage} {label} differs from its runtime snapshot')
        source_rows = []
        for segment in segments:
            switches = [e for e in native if e.get('node') == segment['id']]
            observed = {}
            for event, at in (('jammer_on', segment['start_s']), ('jammer_off', segment['stop_s'])):
                observed[event] = False
                for row in switches:
                    params = dict(part.split('=', 1) for part in row.get('details', '').split(';') if '=' in part)
                    if (row.get('event') == event and finite(row.get('time_s')) is not None
                            and abs(float(row['time_s']) - at) < 1e-6 and params.get('pattern') == segment['pattern']
                            and all(finite(params.get(key)) is not None and math.isclose(float(params[key]), segment[key], rel_tol=0, abs_tol=1e-6)
                                    for key in ('center_hz', 'bandwidth_hz', 'power_w'))):
                        observed[event] = True
            arrivals = sum(e.get('event') == 'spectrum_signal_arrival'
                and 'foreign_signal=1' in str(e.get('details', '')).split(';') and finite(e.get('value')) is not None
                and e.get('node') in {'cp', *(f'uav{i}' for i in UAVS)}
                and finite(e.get('time_s')) is not None and segment['start_s'] <= float(e['time_s']) < segment['stop_s']
                for e in native)
            source_rows.append(dict(id=segment['id'], center_hz=segment['center_hz'], pattern=segment['pattern'],
                switches=observed, foreign_signal_arrivals=arrivals,
                passed=all(observed.values()) and arrivals > 0))
        details['sources'] = source_rows
        checks['separate_em_source_observed'] = (schedule['actual_schedule_status'] == 'observed'
            and len(source_rows) == 1 and source_rows[0]['passed'])
    except (ValueError, OSError, KeyError, TypeError) as error:
        failures.append(f'source_observation: {type(error).__name__}: {error}')
    result['flight_case_passed'] = bool(checks) and all(checks.values()) and not failures
    result['status'] = ('flight_case_observed' if result['flight_case_passed'] else
        'runtime_or_mission_failed' if not checks['runtime_and_complete_window'] else 'incomplete')
    return result


def analyze_run(run_dir: Path, output_dir: Path | None = None, *, deadline_ms: float = 250.,
                restricted_s: float = 30., plots: bool = True, live_export: bool = True) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    output = (output_dir or run_dir).resolve()
    errors: list[str] = []
    meta = read_json(run_dir / "metrics/relay_experiment_summary.json")
    config = read_json(run_dir / "metrics/relay_experiment_config.json")
    stats = read_json(run_dir / "metrics/native_radio_stats.json")
    environment = {}
    if (run_dir / "environment.txt").is_file():
        environment = dict(line.split("=", 1) for line in (run_dir / "environment.txt").read_text().splitlines() if "=" in line)
    combined = run_dir / "logs/coordination_events.jsonl"
    paths = [combined] if combined.is_file() else sorted((run_dir / "logs").glob("coordination_*.jsonl"))
    events = [row for path in paths for row in read_jsonl(path, errors)]
    expected_run = meta.get("run_id", run_dir.name)
    foreign = [row for row in events if row.get("run_id") is not None and row["run_id"] != expected_run]
    if foreign:
        errors.append(f"{len(foreign)} foreign-run records excluded from this run's analysis")
        events = [row for row in events if row.get("run_id") is None or row["run_id"] == expected_run]
    events.sort(key=lambda row: finite(row.get("host_monotonic_ns")) or 0)
    stage = lambda row: row.get("stage", row.get("event"))
    stamp = lambda row: finite(row.get("host_monotonic_ns"))
    start = finite(meta.get("measurement_start_host_monotonic_ns"))
    if start is None:
        start = next((stamp(row) for row in events if row.get("phase") == "experiment_normal"
                      and stamp(row) is not None), None)
    duration_wall = finite(meta.get("measurement_duration_wall_s"))
    end = start + duration_wall * 1e9 if start is not None and duration_wall is not None else None
    if end is None:
        end = next((stamp(row) for row in events if row.get("phase") == "experiment_landing"
                    and stamp(row) is not None and (start is None or stamp(row) >= start)), None)
    if end is None:
        end = max((stamp(row) for row in events if stamp(row) is not None), default=None)
        errors.append("measurement end not explicitly recorded; latest observed event is an incomplete-run bound")
    if start is None:
        errors.append("measurement start not recorded")
    inside = lambda at: at is not None and (start is None or at >= start) and (end is None or at <= end)
    condition = str(meta.get("fault_profile", config.get("fault_profile", stats.get("fault_profile", "none"))))
    policy = str(meta.get("policy", config.get("policy", "not_recorded")))
    native_capture = {}
    native = read_csv(run_dir / "logs/native_radio_events.csv", evidence=native_capture, keep=native_analysis_event)
    fault_events = [row for row in native if row.get("event") in ("fault_start", "fault_end")
                    and inside(finite(row.get("wall_monotonic_ns")))]
    fault_starts = [finite(row["wall_monotonic_ns"]) for row in fault_events if row["event"] == "fault_start"]
    fault = fault_starts[0] if fault_starts else None
    fault_end = next((finite(row["wall_monotonic_ns"]) for row in fault_events
                      if row["event"] == "fault_end" and fault is not None
                      and finite(row["wall_monotonic_ns"]) >= fault), None)
    if condition != "none" and fault is None:
        errors.append("declared fault has no actual native fault_start")
    if condition == "none" and fault is not None:
        errors.append("no-fault run contains actual native fault_start")
    if len(fault_starts) > 1:
        errors.append("multiple native fault windows; single-window recovery interpretation is limited")
    applicable = condition != "none"
    grant_rows = [row for row in events if stage(row) == "grant_issued"]
    grants = {str(row.get("grant_sequence")): row for row in grant_rows if row.get("grant_sequence") is not None}
    initial_master = int(config.get("initial_master", 1))
    initial_epoch = next((row.get("epoch") for row in grant_rows if row.get("leader_id") == initial_master), 1)
    before_fault_grants = [row for row in grant_rows if fault is not None
        and finite(row.get("valid_from_ns")) is not None and finite(row.get("valid_until_ns")) is not None
        and finite(row["valid_from_ns"]) <= fault < finite(row["valid_until_ns"])
        and (finite(row.get("issued_ns", row.get("host_monotonic_ns"))) or 0) <= fault]
    preceding = max(before_fault_grants, key=lambda row: (row.get("epoch", 0), row.get("grant_sequence", 0))) if before_fault_grants else None
    preceding_leader = identifier(preceding.get("leader_id")) if preceding else None
    preceding_epoch = preceding.get("epoch") if preceding else None
    fault_target = identifier(next((row.get("peer") for row in fault_events if row["event"] == "fault_start"), None))
    if applicable and fault is not None and preceding is None:
        errors.append("active coordinator at actual fault is not established")
    if condition == "selective" and fault is not None and fault_target != preceding_leader:
        errors.append("native selective fault does not establish loss of the actual pre-fault coordinator link")

    admissions = [row for row in events if stage(row) == "uart_admitted"]
    admission_age_limit_ms = finite(config.get("effective_service_args", {}).get("command_max_age_ms"))
    if admission_age_limit_ms is not None and admission_age_limit_ms <= 0:
        admission_age_limit_ms = None
    audit = Counter()
    admission_status: dict[int, str] = {}
    seen = set()
    for row in admissions:
        key = (row.get("run_id"), identifier(row.get("uav_id")), row.get("command_id"))
        if key in seen:
            audit["duplicate_admissions"] += 1
        seen.add(key)
        checked = finite(row.get("admission_checked_host_monotonic_ns"))
        write_start = finite(row.get("uart_write_started_host_monotonic_ns"))
        write_end = finite(row.get("uart_write_end_host_monotonic_ns"))
        created = finite(row.get("command_created_host_monotonic_ns"))
        low = finite(row.get("valid_from_ns", row.get("command_valid_from_ns")))
        high = finite(row.get("valid_until_ns", row.get("command_valid_until_ns")))
        status = "unverifiable_missing_boundary_or_interval"
        if all(value is not None for value in (checked, write_start, write_end, created, low, high)):
            status = "valid" if low <= checked < high else "expired_or_not_yet_valid"
            if not low <= write_start <= write_end < high:
                status = "write_outside_lease"
            elif not created <= write_start <= write_end <= checked:
                status = "invalid_write_chronology"
            elif status == "valid":
                if admission_age_limit_ms is None:
                    status = "unverifiable_missing_command_age_limit"
                elif write_end - created > admission_age_limit_ms * 1e6:
                    status = "command_age_exceeded"
                elif checked - created > admission_age_limit_ms * 1e6:
                    status = "command_age_exceeded_at_final_check"
            issued = grants.get(str(row.get("grant_sequence")))
            if issued is None:
                status = "unverifiable_missing_issued_grant"
            elif (issued.get("epoch") != row.get("epoch") or issued.get("leader_id") != row.get("leader_id")
                  or finite(issued.get("valid_from_ns")) != low or finite(issued.get("valid_until_ns")) != high):
                status = "grant_metadata_mismatch"
        elif low is not None and high is not None and stamp(row) is not None and not low <= stamp(row) < high:
            audit["admission_event_outside_lease_without_exact_boundary"] += 1
        if row.get("node_id") is not None and row.get("node_id") != row.get("uav_id"):
            status = "wrong_uart_node"
        admission_status[id(row)] = status
        audit[status] += 1
    audit["uart_write_invalidated"] = sum(stage(row) == "uart_write_invalidated" for row in events)
    by_command: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in events:
        if row.get("command_id") is not None:
            by_command[str(row["command_id"])].append(row)
    sends = [row for row in events if stage(row) == "gcs_send" and target(row) is not None and inside(stamp(row))]
    commands = []
    for sent in sends:
        records = by_command[str(sent["command_id"])]
        admitted = next((row for row in records if stage(row) == "uart_admitted"
                         and inside(finite(row.get("uart_write_end_host_monotonic_ns")))), None)
        write_end = finite(admitted.get("uart_write_end_host_monotonic_ns")) if admitted else None
        created = finite(sent.get("command_created_host_monotonic_ns"))
        latency = (write_end - created) / 1e6 if write_end is not None and created is not None else None
        send_latency = (write_end - stamp(sent)) / 1e6 if write_end is not None and stamp(sent) is not None else None
        required = {"gcs_send", "master_receive", "follower_receive", "uart_admitted"}
        uav, leader = identifier(sent.get("uav_id")), identifier(sent.get("leader_id"))
        if uav != leader:
            required.add("master_forward")
        observed_stages = {stage(row) for row in records}
        lease_status = admission_status.get(id(admitted)) if admitted is not None else "no_admission"
        lineage = "no_admission"
        if admitted is not None:
            required_values = ("epoch", "leader_id", "uav_id", "command_created_host_monotonic_ns", "payload_sha256")
            lineage = "valid" if all(sent.get(key) is not None and sent.get(key) == admitted.get(key)
                                      for key in required_values) and target(sent) == target(admitted) else "unverifiable_or_mismatched"
        actor_for = {"gcs_send": 0, "master_receive": leader, "master_forward": leader,
                     "follower_receive": uav, "uart_admitted": uav}
        chain = all(any(stage(row) == name and row.get("node_id") == actor_for[name] for row in records)
                    for name in required)
        goal_metadata = {(row["goal_id"], finite(row.get("goal_created_host_monotonic_ns")))
            for row in records if row.get("goal_id") is not None and finite(row.get("goal_created_host_monotonic_ns")) is not None}
        if len(goal_metadata) > 1:
            errors.append(f"conflicting goal metadata for {sent['command_id']}")
        goal_id, goal_created = next(iter(goal_metadata)) if len(goal_metadata) == 1 else (None, None)
        policy_allowed = policy != "fixed" or leader == initial_master
        commands.append(dict(command_id=sent["command_id"], uav_id=uav, leader_id=leader,
            epoch=sent.get("epoch"), created_ns=created, goal_id=goal_id, goal_created_ns=goal_created,
            sent_ns=stamp(sent), admitted_ns=write_end, admission_event_ns=stamp(admitted) if admitted else None,
            grant_sequence=admitted.get("grant_sequence") if admitted else None,
            lease_valid_from_ns=finite(admitted.get("valid_from_ns")) if admitted else None,
            lease_valid_until_ns=finite(admitted.get("valid_until_ns")) if admitted else None,
            admission_checked_ns=finite(admitted.get("admission_checked_host_monotonic_ns")) if admitted else None,
            latency_ms=latency, sent_to_uart_latency_ms=send_latency,
            timely=latency is not None and 0 <= latency <= deadline_ms
                and lease_status == "valid" and lineage == "valid" and chain and policy_allowed,
            policy_allowed=policy_allowed,
            lease_status=lease_status, lineage_status=lineage, application_chain_complete=chain,
            target_position_m=target(sent)))
    unique_commands = {}
    for row in commands:
        if row["command_id"] in unique_commands:
            audit["duplicate_gcs_send_ids"] += 1
        unique_commands.setdefault(row["command_id"], row)
    commands = list(unique_commands.values())
    command_index = unique_commands
    offers = read_csv(run_dir / "metrics/relay_control_refresh.csv")
    if not offers:
        offers = [dict(row) for row in events if stage(row) == "control_refresh_offered"]
        created = {row.get("request_id"): row.get("command_id") for row in events if stage(row) == "control_refresh_created"}
        for row in offers:
            row["command_id"] = created.get(row.get("request_id"))
    offers = [row for row in offers if inside(finite(row.get("host_monotonic_ns")))]
    unique_offers = {}
    for row in offers:
        key = row.get("request_id")
        if key in unique_offers:
            audit["duplicate_offer_ids"] += 1
        unique_offers.setdefault(key, row)
    offers = list(unique_offers.values())
    for offered in offers:
        command = command_index.get(str(offered.get("command_id")))
        if command is not None and command["goal_id"] is None and offered.get("goal_id"):
            command.update(goal_id=offered["goal_id"], goal_created_ns=finite(offered.get("goal_created_host_monotonic_ns")))
    rates = {}
    for name, uavs in [("all_five", set(UAVS)), *((f"uav{i}", {i}) for i in UAVS)]:
        offered = [row for row in offers if identifier(row.get("uav_id")) in uavs]
        sent = [row for row in commands if row["uav_id"] in uavs]
        available = sum(str(row.get("command_id")) in command_index for row in offered)
        timely_offers = sum(command_index.get(str(row.get("command_id")), {}).get("timely", False) for row in offered)
        timely = sum(row["timely"] for row in sent)
        delivered = sum(row["admitted_ns"] is not None for row in sent)
        rates[name] = dict(offered=len(offered), sent=len(sent), delivered=delivered, timely=timely,
            sent_per_offered=available / len(offered) if offered else None,
            timely_per_offered=timely_offers / len(offered) if offered else None,
            timely_per_sent=timely / len(sent) if sent else None,
            delivered_per_sent=delivered / len(sent) if sent else None,
            created_to_uart_latency_ms=distribution([row["latency_ms"] for row in sent if row["latency_ms"] is not None]),
            sent_to_uart_latency_ms=distribution([row["sent_to_uart_latency_ms"] for row in sent if row["sent_to_uart_latency_ms"] is not None]))
    eligible = [row for row in commands if fault is not None and row["created_ns"] is not None
                and row["created_ns"] >= fault and row["admitted_ns"] is not None and row["admitted_ns"] >= fault
                and row["lease_status"] == "valid" and row["lineage_status"] == "valid"
                and row["application_chain_complete"] and row["policy_allowed"]]

    def group_points(rows):
        scopes: dict[tuple[int, int], dict[int, float]] = defaultdict(dict)
        for row in sorted(rows, key=lambda row: row["admitted_ns"]):
            scopes[(row["leader_id"], row["epoch"])].setdefault(row["uav_id"], row["admitted_ns"])
        result = {}
        for label in ("all_five", "four_followers"):
            complete = []
            for (leader, epoch), firsts in scopes.items():
                required = set(UAVS) - ({leader} if label == "four_followers" else set())
                if required <= set(firsts):
                    complete.append((max(firsts[i] for i in required), leader, epoch, sorted(required)))
            chosen = min(complete) if complete else None
            result[label] = dict(**recovery(chosen[0] if chosen else None, fault, end,
                applicable=applicable, restricted_s=restricted_s), leader_id=chosen[1] if chosen else None,
                epoch=chosen[2] if chosen else None, members=chosen[3] if chosen else None)
        return result
    control = group_points(eligible)
    handover = group_points([row for row in eligible if preceding_leader is not None and preceding_epoch is not None
                            and row["leader_id"] != preceding_leader and row["epoch"] > preceding_epoch])
    if applicable and policy == "fixed":
        for row in handover.values():
            row.update(status="not_applicable_fixed_policy", recovered=None, time_s=None, censor_time_s=None,
                       restricted_unrecovered_time_s=None)
    per_uav = {str(i): recovery(min((r["admitted_ns"] for r in eligible if r["uav_id"] == i), default=None),
                fault, end, applicable=applicable, restricted_s=restricted_s) for i in UAVS}
    completions = [row for row in events if stage(row) == "action_completed" and inside(stamp(row))]
    goals, eligible_completions = [], []
    goal_recovery, completion_gap = {}, {}
    hold_s = float(config.get("hold_s", 2.))
    for i in UAVS:
        before = [row for row in completions if identifier(row.get("uav_id")) == i and fault is not None and stamp(row) < fault]
        previous = max(before, key=stamp) if before else None
        before_admitted = [row for row in admissions if identifier(row.get("uav_id")) == i
                           and fault is not None and finite(row.get("uart_write_end_host_monotonic_ns")) is not None
                           and finite(row["uart_write_end_host_monotonic_ns"]) < fault
                           and admission_status[id(row)] == "valid" and target(row) is not None]
        active_target = max(before_admitted, key=lambda row: finite(row["uart_write_end_host_monotonic_ns"])) if before_admitted else None
        reference = target(active_target) if active_target else None
        choices = []
        for row in completions:
            if identifier(row.get("uav_id")) != i:
                continue
            created = finite(row.get("goal_created_host_monotonic_ns"))
            changed = reference is not None and target(row) is not None and math.dist(reference, target(row)) >= 1.
            admission = command_index.get(str(row.get("admitted_command_id", row.get("command_id"))))
            qualifies = bool(fault is not None and created is not None and created >= fault and stamp(row) >= fault
                and changed and row.get("maneuver_observed") is True
                and (finite(row.get("max_vertical_displacement_m")) or 0) >= 1.
                and (finite(row.get("dwell_s")) or 0) >= hold_s
                and admission is not None and admission["lease_status"] == "valid"
                and admission["lineage_status"] == "valid" and admission["application_chain_complete"] and admission["policy_allowed"]
                and target(row) == admission["target_position_m"]
                and admission["created_ns"] is not None and admission["created_ns"] >= created
                and admission["admitted_ns"] is not None and admission["admitted_ns"] <= stamp(row))
            goals.append(dict(uav_id=i, goal_id=row.get("goal_id"), completed_ns=stamp(row), created_ns=created,
                qualifies_postfault_recovery=qualifies, target_changed_from_prefault_admission=changed,
                maneuver_observed=row.get("maneuver_observed"), dwell_s=row.get("dwell_s")))
            if qualifies:
                choices.append(stamp(row))
                eligible_completions.append(row)
        point = min(choices) if choices else None
        goal_recovery[str(i)] = recovery(point, fault, end, applicable=applicable, restricted_s=restricted_s)
        completion_gap[str(i)] = ((point - stamp(previous)) / 1e9 if point is not None and previous else None)
    group_goal_point = (fault + max(row["time_s"] for row in goal_recovery.values()) * 1e9
                        if fault is not None and all(row["recovered"] is True for row in goal_recovery.values()) else None)
    decomposition = group_goal_decomposition(eligible_completions, commands, fault)
    if not applicable:
        for row in decomposition.values():
            row["status"] = "not_applicable_no_fault"
    fault_limit = fault_end if fault_end is not None else end
    during_fault = lambda row: fault is not None and stamp(row) is not None and fault <= stamp(row) < (fault_limit or fault)
    issued_during = [row for row in grant_rows if during_fault(row)]
    first_role_grants = {}
    for row in grant_rows:
        first_role_grants.setdefault((row.get("leader_id"), row.get("epoch")), row)
    selected_during = [row for (leader, epoch), row in first_role_grants.items()
                       if during_fault(row) and (leader != initial_master or epoch != initial_epoch)]
    fixed_role_changes = sum(leader != initial_master for leader, _epoch in first_role_grants) if policy == "fixed" else None
    held = set()
    for row in events:
        i = identifier(row.get("uav_id"))
        if stage(row) == "failsafe_hold_observed" and during_fault(row) and i is not None:
            native_hold = [e for e in events if identifier(e.get("uav_id")) == i and during_fault(e)
                           and stage(e) == "hold_mode_observed" and stamp(e) <= stamp(row)]
            if native_hold and (finite(row.get("hold_s")) or 0) >= hold_s:
                held.add(i)
    requested_duration = finite(config.get("duration_s", 180.))
    completed_duration = finite(meta.get("measurement_duration_source_s"))
    full_window = requested_duration is not None and completed_duration is not None and completed_duration >= requested_duration
    result = dict(run_id=meta.get("run_id", run_dir.name), raw_run_dir=str(run_dir), output_dir=str(output),
        analysis_version=2,
        policy=policy, fault_profile=condition, pilot=bool(meta.get("pilot", config.get("pilot", False))),
        run_status=meta.get("status", "not_recorded"), run_error=meta.get("error"),
        observation_window=dict(start_host_monotonic_ns=start, end_host_monotonic_ns=end, complete=full_window),
        actual_fault=dict(start_host_monotonic_ns=fault, end_host_monotonic_ns=fault_end, events=fault_events),
        pre_fault_authority=dict(leader_id=preceding_leader, epoch=preceding_epoch, selective_target_uav=fault_target),
        policy_audit=dict(fixed_policy_new_leader_appointments=fixed_role_changes),
        rates=rates, control_recovery=control, control_recovery_per_uav=per_uav, handover_recovery=handover,
        goal_recovery_per_uav=goal_recovery, completion_gap_s_per_uav=completion_gap,
        goal_recovery_all_five=recovery(group_goal_point, fault, end, applicable=applicable, restricted_s=restricted_s),
        goal_decomposition=decomposition,
        admission_audit=dict(audit), unverifiable_admissions=sum(value for key, value in audit.items() if key.startswith("unverifiable")),
        admission_audit_settings=dict(command_max_age_ms=admission_age_limit_ms,
            source="recorded effective_service_args.command_max_age_ms; missing is unverifiable"),
        admission_total=len(admissions),
        command_lineage_errors=sum(row["lineage_status"] == "unverifiable_or_mismatched" for row in commands),
        total_GCS=dict(applicable=condition == "total_GCS", all_grants_issued_during_fault=len(issued_during),
            new_leader_selection_grants_during_fault=len(selected_during), observed_hold_uavs=sorted(held),
            hold_all_five=len(held) == 5 if condition == "total_GCS" else None,
            no_new_leader_selection=len(selected_during) == 0 if condition == "total_GCS" and fault is not None else None),
        criteria=meta.get("criteria", {}), metadata=dict(environment=environment, config=config, native_stats=stats,
            configuration_snapshot=read_json(run_dir / "configuration/final/manifest.json") or read_json(run_dir / "configuration/initial/manifest.json"),
            physics_runtime=read_json(run_dir / "logs/physics_runtime.json"),
            native_sources=read_json(run_dir / 'logs/native_sources.json')),
        data_quality_errors=errors,
        definitions=dict(deadline_ms=deadline_ms, latency_clock="host monotonic; command creation to actual UART write end; send-event latency reported separately",
            restricted_horizon_s=restricted_s,
            offered_denominator="all unique offered refresh requests, including unsent",
            sent_denominator="all unique target-bearing GCS sends, including lost/late; timely requires verified lease/payload lineage and complete application relay chain",
            group_recovery="first postfault-created refresh admitted to every member under one leader/epoch",
            goal_recovery="new postfault goal, target changed >=1m from last pre-fault admitted target, observed maneuver and dwell; not latest refresh creation",
            goal_decomposition="critical last-finishing UAV under one assigned relay/epoch; first goal CID and latest completion-refresh CID are distinct explicit lineages; no continuous authority inferred",
            goal_delivery_subphases="goal creation to first command creation includes refresh scheduling and issuer/authority availability; first command creation to UART is application delivery including processing/relay, not pure RF latency",
            completion_gap="last pre-fault completed goal to first eligible new goal completion",
            censoring="right-censored time is never substituted as a successful recovery",
            restricted_loss=f"min(time to first recovery, {restricted_s}s); a bounded loss, not an imputed recovery; missing if horizon not observed",
            two_hop_evidence="application stage lineage; native PHY/PCAP proof remains a separate requirement",
            total_GCS="renewals before failure detection can be issued but lost; new leader selection must remain absent"))
    if not meta:
        errors.append("relay_experiment_summary.json missing; no successful experiment inferred")
    declared_roster = meta.get('uav_ids')
    roster_verified = (isinstance(declared_roster, list) and bool(declared_roster) and
        all(type(i) is int and i in UAVS for i in declared_roster) and len(set(declared_roster)) == len(declared_roster))
    roster = sorted(declared_roster) if roster_verified else sorted({identifier(o.get('uav_id')) for o in offers} - {None})
    delivery = engineering_delivery(offers, commands, roster, finite(config.get('command_period_s')), observation_end_ns=end)
    delivery_checks = {uav: delivery_limit_checks(row) for uav, row in delivery.items()}
    numeric_failed = any(v is False for checks in delivery_checks.values() for v in checks.values())
    timing = runtime_timing_evidence(meta, native, stats)
    measured = bool(roster_verified and full_window and not errors and admission_age_limit_ms == ENGINEERING_DELIVERY_V1['deadline_ms']
        and meta.get('measurement_validity') == 'valid' and not any(value for key, value in audit.items() if key != 'valid')
        and not result['command_lineage_errors'] and not any(row['unverifiable'] for row in delivery.values()) and timing['eligible'])
    result['engineering_delivery_v1'] = dict(limits=ENGINEERING_DELIVERY_V1,
        status='observed_limits_failed' if numeric_failed else 'insufficient_evidence',
        measurement_eligible=measured, runtime_timing=timing, per_uav=delivery, observed_limit_checks=delivery_checks,
        scope='Control creation to audited UART write end, not pure RF delay, ACK RTT, physical completion or payload SLA.',
        evidence_policy='A single run cannot establish the required ten independent RNG seeds; series criteria also require 10000 latency and jitter samples per UAV.',
        jitter_definition='Absolute delay difference for adjacent recorded scheduled offers of one UAV/leader/epoch; missing, unavailable or unverifiable slots and schedule gaps break pairs.',
        denominator_definition='All unique measurement-window offers partition into timely at <=250ms, late verified admission, sent but missing admission, unavailable send, or unverifiable lineage/identity. Missing admissions without a fully observed deadline horizon are unverifiable, not asserted losses.')
    result['definitions']['engineering_delivery_limits'] = ENGINEERING_DELIVERY_V1
    result['definitions']['engineering_delivery_roster'] = roster
    payload_window = timing['window']
    payload = result['payload_lineage'] = analyze_payload_lineage(run_dir,
        payload_window['start_host_monotonic_ns'], payload_window['end_host_monotonic_ns'],
        complete=bool(full_window and meta.get('measurement_validity')=='valid' and timing['eligible']))
    from network.coordination.service_profiles import qualify_payload_services
    service_metadata_files = [run_dir / name for name in
        ('metrics/relay_experiment_summary.json', 'metrics/relay_experiment_config.json',
         'metrics/native_radio_stats.json', 'environment.txt')]
    rt_solver_metadata = stats.get('sionna_solver')
    service_metadata = dict(run_id=expected_run, rng_seed=stats.get('rng_seed'), rng_run=stats.get('rng_run'),
        rt_solver_seed=rt_solver_metadata.get('seed') if isinstance(rt_solver_metadata, dict) else None,
        source_commit=environment.get('git_head'),
        source_dirty_paths=environment.get('source_dirty_paths'),
        source_files=[dict(path=path.relative_to(run_dir).as_posix(), bytes=path.stat().st_size,
                           sha256=hashlib.sha256(path.read_bytes()).hexdigest())
                      for path in service_metadata_files if path.is_file()])
    result['service_class_qualification'] = qualify_payload_services(payload, run_metadata=service_metadata)
    write_json(output / 'metrics/service_class_qualification.json', result['service_class_qualification'])
    write_csv(output / 'metrics/payload_records.csv',payload['records'],
        ['frame_uid','uav_id','direction','process_epoch','message_type','mavlink_body_bytes',
         'load_id','load_sequence','application_useful_bytes','application_useful_sha256','scheduled_host_monotonic_ns',
         'target_process_epoch','target_observation_start_host_monotonic_ns','target_observation_end_host_monotonic_ns',
         'created_host_monotonic_ns','transmitted_host_monotonic_ns','accepted_host_monotonic_ns',
         'deadline_host_monotonic_ns','outcome','duplicate_receipts','latency_ms'])
    result["rtf"] = {}
    if live_export and (run_dir / "logs/node_state.jsonl").is_file():
        spec = importlib.util.spec_from_file_location("bas_live_report", Path(__file__).with_name("summarize_native_radio_five_uav.py"))
        report = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(report)
        report.export_live_metrics(run_dir, output / "live", plots=plots,
                                  start_sim_time_s=finite(meta.get("measurement_start_source_sim_time_s")))
        clocks = [row for row in read_csv(output / "live/metrics/tracker_clock.csv") if inside(finite(row.get("host_monotonic_ns")))]
        windows = [row for row in read_csv(output / "live/metrics/end_to_end_rtf.csv") if inside(finite(row.get("host_monotonic_ns")))]
        first, last = (clocks[0], clocks[-1]) if clocks else (None, None)
        wall = (float(last["host_monotonic_ns"]) - float(first["host_monotonic_ns"])) / 1e9 if len(clocks) > 1 else None
        result["rtf"] = dict(integral=(float(last["source_sim_time_s"]) - float(first["source_sim_time_s"])) / wall if wall and wall > 0 else None,
            windows=distribution([float(row["end_to_end_rtf"]) for row in windows]),
            source="existing live exporter; tracker/host samples filtered to measurement host interval",
            window_duration_s=distribution([float(row["wall_duration_s"]) for row in windows]))
    write_csv(output / "metrics/relay_commands.csv", commands, ["command_id", "uav_id", "leader_id", "epoch", "created_ns", "goal_id", "goal_created_ns",
        "sent_ns", "admitted_ns", "admission_event_ns", "grant_sequence", "lease_valid_from_ns", "lease_valid_until_ns", "admission_checked_ns",
        "latency_ms", "sent_to_uart_latency_ms", "timely", "lease_status", "lineage_status", "application_chain_complete", "policy_allowed", "target_position_m"])
    write_csv(output / "metrics/relay_goals.csv", goals, ["uav_id", "goal_id", "created_ns", "completed_ns",
        "qualifies_postfault_recovery", "target_changed_from_prefault_admission", "maneuver_observed", "dwell_s"])
    decomposition_rows = [dict(scope=name, **{key:value for key,value in row.items() if key != "latest_CID_chain"},
        latest_CID_chain=json.dumps(row.get("latest_CID_chain"))) for name,row in decomposition.items()]
    write_csv(output / "metrics/relay_goal_decomposition.csv", decomposition_rows, ["scope", "status", "critical_uav_id", "goal_id", "leader_id", "epoch", "members",
        "first_admitted_goal_CID", "completion_refresh_CID", "goal_created_host_ns", "first_command_created_host_ns", "first_goal_uart_host_ns", "completion_refresh_uart_host_ns", "completed_host_ns",
        "fault_to_goal_creation_s", "goal_creation_to_first_UART_s", "goal_creation_to_first_command_creation_s", "first_command_creation_to_UART_s",
        "first_UART_to_completion_s", "fault_to_completion_s", "sum_error_s", "latest_CID_chain"])
    recovery_rows = [dict(endpoint=f"uav{i}", **per_uav[str(i)]) for i in UAVS]
    recovery_rows += [dict(endpoint=name, **row) for name, row in control.items()]
    write_csv(output / "metrics/relay_recovery.csv", recovery_rows, ["endpoint", "status", "recovered", "time_s",
        "censor_time_s", "restricted_horizon_s", "restricted_unrecovered_time_s", "leader_id", "epoch", "members"])
    result['refined_case'] = refined_case_evidence(run_dir, meta, stats, environment,
        events, native, commands, payload, errors, native_capture)
    write_json(output / "metrics/relay_analysis.json", result)
    if plots:
        plot_run(output, result, commands, events)
    report_lines = [f"# Observed relay analysis: {result['run_id']}", "",
        f"Policy `{policy}`, fault `{condition}`, recorded status `{result['run_status']}`.",
        f"Raw: `{run_dir}`.", "", "[Numerical outcomes and definitions](metrics/relay_analysis.json).",
        "[Commands, including missing delivery](metrics/relay_commands.csv); [censored recovery](metrics/relay_recovery.csv).",
        "Renewals issued before detecting total GCS loss are reported separately from new leader appointments."]
    report_lines.append(f"Engineering delivery v1: `{result['engineering_delivery_v1']['status']}`; this single run does not establish a series SLA. See per-UAV counts, p95/p99, jitter and eligibility in the numerical outcomes.")
    report_lines.append(f"Runtime timing: `{timing['status']}`; {timing['lag_ms']['samples']} in-window samples, "
        f"lag p99={timing['lag_ms']['p99']} ms, observed maximum={timing['observed_max_lag_ms']} ms. "
        f"Coverage limitations: `{timing['coverage_problems']}`. Discrete scheduler samples do not establish physical clock or UART accuracy.")
    if plots:
        report_lines += ["", "![Relay outcomes](plots/relay_outcomes.png)"]
    if errors:
        report_lines += ["", "Data limitations:"] + ["- " + value for value in errors]
    (output / "relay_analysis.md").write_text("\n".join(report_lines) + "\n")
    return result


def plot_run(output, result, commands, events):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(2, 2, figsize=(12, 8))
    for offset, key in [(-.25, "offered"), (0., "sent"), (.25, "timely")]:
        axes[0, 0].bar([i + offset for i in UAVS], [result["rates"][f"uav{i}"][key] for i in UAVS], width=.25, label=key)
    axes[0, 0].set(title="Offered / sent / timely UART refreshes", xlabel="UAV", ylabel="unique refreshes", xticks=UAVS)
    axes[0, 0].legend(fontsize=8)
    latencies = sorted(row["latency_ms"] for row in commands if row["latency_ms"] is not None and row["latency_ms"] >= 0)
    denominator = len(commands)
    axes[0, 1].step(latencies, [(i + 1) / denominator for i in range(len(latencies))] if denominator else [], where="post")
    axes[0, 1].axvline(result["definitions"]["deadline_ms"], color="grey", linestyle="--")
    axes[0, 1].set(title="Delivery CDF; denominator includes lost sends", xlabel="command created → UART write end (ms)", ylabel="fraction of all sent", ylim=(0, 1.02))
    fault = result["actual_fault"]["start_host_monotonic_ns"]
    origin = result["observation_window"]["start_host_monotonic_ns"]
    for i in UAVS:
        row = result["control_recovery_per_uav"][str(i)]
        if row["time_s"] is not None:
            axes[1, 0].scatter(row["time_s"], i, color=f"C{i-1}")
        elif row["censor_time_s"] is not None:
            axes[1, 0].scatter(row["censor_time_s"], i, marker=">", color="red")
    if fault is None:
        axes[1, 0].text(.5, .5, "Recovery not applicable / actual fault unavailable", ha="center", transform=axes[1, 0].transAxes)
    axes[1, 0].set(title="New refresh UART recovery (arrows: censored)", xlabel="seconds since actual native fault", ylabel="UAV", yticks=UAVS)
    if origin is not None:
        for row in events:
            at = finite(row.get("host_monotonic_ns"))
            if at is None:
                continue
            if row.get("stage", row.get("event")) == "leader_activated":
                axes[1, 1].scatter((at - origin) / 1e9, identifier(row.get("node_id", row.get("uav_id"))),
                                    marker="|", color=f"C{int(row.get('leader_id', 1))-1}", s=35)
        if fault is not None:
            end = result["actual_fault"]["end_host_monotonic_ns"] or result["observation_window"]["end_host_monotonic_ns"]
            axes[1, 1].axvspan((fault - origin) / 1e9, (end - origin) / 1e9, color="grey", alpha=.15)
    axes[1, 1].set(title="Endpoint role activations; shaded native fault", xlabel="host seconds since measurement start", ylabel="UAV", yticks=UAVS)
    for axis in axes.flat:
        axis.grid(alpha=.2)
    fig.suptitle(str(result["run_id"]))
    fig.tight_layout()
    (output / "plots").mkdir(parents=True, exist_ok=True)
    for extension in ("png", "svg", "pdf"):
        fig.savefig(output / f"plots/relay_outcomes.{extension}", dpi=160)
    plt.close(fig)


def campaign_plan(*, repeats: int = 5, rng_seed: int = 1, order_seed: int = 20261026) -> list[dict[str, Any]]:
    if repeats < 1 or rng_seed < 1:
        raise ValueError("positive repeats and native RNG seed required")
    generator = random.Random(order_seed)
    pairs = [(condition, run) for condition in CONDITIONS for run in range(1, repeats + 1)]
    generator.shuffle(pairs)
    result = []
    for condition, run in pairs:
        policies = ["fixed", "failover"]
        generator.shuffle(policies)
        for policy in policies:
            result.append(dict(order=len(result) + 1, pair_id=f"{condition}-seed{rng_seed}-run{run}",
                rng_seed=rng_seed, rng_run=run, policy=policy, fault_profile=condition,
                run_id=f"paper-relay-{condition.lower().replace('_', '-')}-{policy}-s{rng_seed}-r{run:02d}",
                order_seed=order_seed, configuration_status="freeze_after_successful_pilots"))
    return result


def paired_interval(differences: list[float], *, seed: int = 20261026) -> dict[str, Any]:
    n = len(differences)
    result = dict(n_pairs=n, mean_difference=statistics.fmean(differences) if n else None,
                  median_difference=statistics.median(differences) if n else None,
                  interval_95=None, interval_method=None, differences=differences)
    if n < 2:
        return result
    if n <= 6:
        means = [sum(values) / n for values in itertools.product(differences, repeat=n)]
        method = "exact ordered paired percentile bootstrap"
    else:
        generator = random.Random(seed)
        means = [statistics.fmean(generator.choices(differences, k=n)) for _ in range(10000)]
        method = "10000 paired percentile bootstrap resamples"
    means.sort()
    def quantile(p):
        at = (len(means) - 1) * p
        low, high = math.floor(at), math.ceil(at)
        return means[low] + (means[high] - means[low]) * (at - low)
    result.update(interval_95=[quantile(.025), quantile(.975)], interval_method=method,
                  bootstrap_resamples=len(means))
    return result


MISSION_FIELDS = ("initial_master", "duration_s", "fault_start_s", "fault_end_s", "command_period_s", "task_period_s",
    "base_altitude_m", "altitude_separation_m", "altitude_step_m", "hold_s", "z_tolerance_m",
    "xy_tolerance_m", "hold_speed_mps", "ready_timeout_s", "arm_timeout_s", "takeoff_timeout_s", "land_timeout_s",
    "watchdog_s", "land_z_tolerance_m", "max_landing_descent_mps")
SERVICE_FIELDS = ("role", "port", "initial_master", "duration_s", "watchdog_s", "heartbeat_s", "lease_s", "renew_s",
    "detection_s", "skew_margin_s", "telemetry_max_age_ms", "command_max_age_ms")
ENVIRONMENT_FIELDS = ("map_id", "scenario_key", "physics_profile", "gazebo_requested_rtf", "active_uart_channels",
    "stack_cpuset", "radio_cpuset", "event_logging", "capture_images", "gui", "carrier_hz", "channel_number",
    "channel_width_mhz", "tx_power_w", "phy_rate_bps", "channel_state_max_age_s", "readiness_lag_max_ms",
    "wifi_data_mode", "wifi_control_mode", "wifi_explicit_bar_after_missed_block_ack_requested", "wifi_aggregation_requested",
    "native_capture_storage_requested", "native_capture_storage_effective", "native_capture_tmpfs_limit_bytes",
    "native_capture_mount_fstype", "native_capture_mount_total_bytes")
RADIO_FIELDS = ("selected_propagation_profile", "rx_psd_propagation_model", "sionna_in_process", "sionna_drives_rx_psd",
    "wifi_topology", "fault_extra_loss_db", "fault_master_id", "tx_power_w",
    "state_max_age_s", "carrier_hz", "wifi_actual_channel_number", "wifi_actual_center_frequency_hz",
    "wifi_channel_width_mhz", "wifi_standard", "wifi_data_mode", "wifi_control_mode",
    "wifi_explicit_bar_after_missed_block_ack", "wifi_aggregation_enabled")
MAC_REQUESTS = {"wifi_explicit_bar_after_missed_block_ack": "wifi_explicit_bar_after_missed_block_ack_requested",
                "wifi_aggregation_enabled": "wifi_aggregation_requested"}


def campaign_record(plan_row: dict[str, Any], result: dict[str, Any] | None) -> dict[str, Any]:
    record = dict(plan_row, run_status="not_run", recorded=False, network_eligible=False,
                  exclusion_reasons=["run not recorded"], outcomes={}, comparability={})
    if result is None:
        return record
    metadata = result.get("metadata", {})
    env, cfg, stats = metadata.get("environment", {}), metadata.get("config", {}), metadata.get("native_stats", {})
    snapshot = metadata.get("configuration_snapshot", {})
    source = snapshot.get("source", {})
    head = env.get("git_head") or source.get("head")
    image = snapshot.get("container_image_id")
    service = cfg.get("effective_service_args", {})
    reasons = []
    study_reasons = []
    def study_problem(reason):
        reasons.append(reason)
        study_reasons.append(reason)
    if result.get("pilot"):
        study_problem("pilot is not part of the confirmatory campaign")
    if result.get("policy") != plan_row["policy"] or result.get("fault_profile") != plan_row["fault_profile"]:
        study_problem("recorded policy/condition disagrees with frozen plan")
    if service.get("policy") is not None and service["policy"] != result.get("policy"):
        study_problem("effective GCS service policy disagrees with the recorded experiment policy")
    for key in ("rng_seed", "rng_run"):
        observed = finite(stats.get(key, env.get("ns3_" + key)))
        if observed != finite(plan_row[key]):
            study_problem(f"actual {key} missing or different from plan")
    if not head or not image:
        study_problem("immutable source revision or image ID not recorded")
    dirty = finite(env.get("source_dirty_paths", source.get("dirty_count")))
    if dirty != 0:
        study_problem("clean frozen source is not established")
    for effective, requested in MAC_REQUESTS.items():
        observed, requested_value = stats.get(effective), env.get(requested)
        if type(observed) is not bool or requested_value not in ("0", "1"):
            study_problem(f"requested/effective native MAC setting not recorded: {effective}")
        elif observed != (requested_value == "1"):
            study_problem(f"requested/effective native MAC setting disagrees: {effective}")
    if not result.get("observation_window", {}).get("complete"):
        reasons.append("full measurement window not observed; outcome remains in failure counts")
    if result.get("data_quality_errors"):
        reasons.append("input recording/fault-window limitations: " + "; ".join(result["data_quality_errors"]))
    if result.get("unverifiable_admissions", 0):
        reasons.append("UART authority measurement is incomplete")
    if any(key not in cfg for key in MISSION_FIELDS) or any(key not in service for key in SERVICE_FIELDS):
        study_problem("frozen mission/service settings incomplete")
    rates = result.get("rates", {}).get("all_five", {})
    criteria = result.get("criteria", {})
    horizon = float(result.get("definitions", {}).get("restricted_horizon_s", 30.))
    terminal = result.get("run_status") in ("completed", "failed")
    outcomes = dict(run_completed=float(result["run_status"] == "completed") if terminal else None,
        lifecycle_success=float(criteria["five_uav_lifecycle"]) if "five_uav_lifecycle" in criteria else None,
        offered_availability=rates.get("sent_per_offered"), timely_per_offered=rates.get("timely_per_offered"),
        timely_per_sent=rates.get("timely_per_sent"),
        integral_rtf=result.get("rtf", {}).get("integral"), p5_rtf=result.get("rtf", {}).get("windows", {}).get("p5"))
    outcomes[f"control_unrecovered_time_{horizon:g}s"] = result.get("control_recovery", {}).get("all_five", {}).get("restricted_unrecovered_time_s")
    outcomes[f"goal_unrecovered_time_{horizon:g}s"] = result.get("goal_recovery_all_five", {}).get("restricted_unrecovered_time_s")
    signature = dict(source_head=head, image_id=image, ns3_exact_sha=env.get("ns3_exact_sha"),
        environment={key: env.get(key) for key in ENVIRONMENT_FIELDS},
        mission={key: cfg.get(key) for key in MISSION_FIELDS},
        service={key: service.get(key) for key in SERVICE_FIELDS},
        radio={key: stats.get(key) for key in RADIO_FIELDS}, physics_runtime=metadata.get("physics_runtime", {}),
        native_sources=metadata.get('native_sources', {}),
        analysis=result.get("definitions", {}))
    actual_seed = finite(stats.get('rng_seed', env.get('ns3_rng_seed')))
    record.update(recorded=terminal, has_artifacts=True, run_status=result.get("run_status", "not_recorded"),
        run_error=result.get("run_error"), raw_run_dir=result.get("raw_run_dir"),
        analysis_path=str(Path(result["output_dir"]) / "metrics/relay_analysis.json"),
        network_eligible=not reasons, exclusion_reasons=reasons, outcomes=outcomes, comparability=signature,
        study_eligible=not study_reasons,
        admission_audit=result.get("admission_audit", {}), criteria=criteria,
        engineering_delivery_v1=result.get('engineering_delivery_v1', {}),
        actual_rng_seed=int(actual_seed) if actual_seed is not None and actual_seed >= 1 and actual_seed.is_integer() else None)
    return record


def aggregate_campaign(records: list[dict[str, Any]]) -> dict[str, Any]:
    records = [dict(row, exclusion_reasons=list(row["exclusion_reasons"])) for row in records]
    signatures = {json.dumps(row["comparability"], sort_keys=True) for row in records
                  if row["recorded"] and row.get("study_eligible")}
    if len(signatures) > 1:
        for row in records:
            if row["recorded"] and row.get("study_eligible"):
                row["study_eligible"] = row["network_eligible"] = False
                row["exclusion_reasons"].append("campaign source/configuration changed; do not pool implementations/settings")
    grouped = defaultdict(dict)
    for row in records:
        grouped[row["pair_id"]][row["policy"]] = row
    points, exclusions = [], []
    for pair, policies in grouped.items():
        fixed, failover = policies.get("fixed"), policies.get("failover")
        if fixed is None or failover is None or not fixed["recorded"] or not failover["recorded"]:
            exclusions.append(dict(pair_id=pair, reason="one or both runs not recorded"))
            continue
        same = fixed["comparability"] == failover["comparability"]
        for metric in fixed["outcomes"]:
            a, b = finite(fixed["outcomes"].get(metric)), finite(failover["outcomes"].get(metric))
            is_completion = metric in ("run_completed", "lifecycle_success")
            allowed = same and a is not None and b is not None and (
                (is_completion and fixed["study_eligible"] and failover["study_eligible"])
                or (not is_completion and fixed["network_eligible"] and failover["network_eligible"]))
            if allowed:
                points.append(dict(pair_id=pair, fault_profile=fixed["fault_profile"], metric=metric,
                                   fixed=a, failover=b, difference=b - a))
            else:
                exclusions.append(dict(pair_id=pair, metric=metric,
                    reason="settings differ" if not same else "missing outcome" if a is None or b is None
                           else "conditional network endpoint: " + "; ".join(fixed["exclusion_reasons"] + failover["exclusion_reasons"])))
    estimates = []
    for condition in CONDITIONS:
        metrics = sorted({row["metric"] for row in points if row["fault_profile"] == condition})
        for metric in metrics:
            values = [row["difference"] for row in points if row["fault_profile"] == condition and row["metric"] == metric]
            estimates.append(dict(fault_profile=condition, metric=metric, difference_direction="failover minus fixed",
                                  **paired_interval(values)))
    return dict(planned_runs=len(records), recorded_runs=sum(row["recorded"] for row in records),
        attempted_runs=sum(row.get("has_artifacts", row["recorded"]) for row in records),
        run_status_counts=dict(Counter(row["run_status"] for row in records)),
        records=records, paired_points=points, paired_estimates=estimates, pair_exclusions=exclusions,
        global_configuration_signature_count=len(signatures),
        global_frozen_configuration_consistent=len(signatures) == 1,
        engineering_delivery_v1=engineering_delivery_series(records),
        campaign_status=("all_recorded_comparable" if all(row["network_eligible"] for row in records)
                         else "all_recorded_with_conditional_limits") if records and all(row["recorded"] for row in records) else "incomplete_campaign",
        methods=["Independent repetition is a whole paired run, never a packet or UAV.",
                 "All planned/missing/failed runs remain in the table and status counts.",
                 "Completed/lifecycle outcomes include recorded failed runs; network estimates condition on a complete comparable measurement window.",
                 "Changed source or frozen settings prevent pooling confirmatory pairs across a campaign; every affected run remains visible.",
                 "Restricted unrecovered time is a bounded loss, not successful recovery imputed at a censoring limit.",
                 "With five pairs, percentile bootstrap intervals are exploratory; all observed pairs are shown and no significance claim is made.",
                 "A degenerate interval for identical observed effects does not establish population certainty."])


def analyze_campaign(plan_path: Path, runs_root: Path, output: Path, *, plots: bool = True,
                     live_export: bool = True, deadline_ms: float = 250., restricted_s: float = 30.) -> dict[str, Any]:
    plan = read_csv(plan_path)
    if not plan:
        raise ValueError("campaign plan is empty or missing")
    records = []
    for row in plan:
        run_dir = Path(row["run_dir"]) if row.get("run_dir") else runs_root / row["run_id"]
        result = None
        if run_dir.is_dir():
            result = analyze_run(run_dir, output / "runs" / row["run_id"], plots=False, live_export=live_export,
                                 deadline_ms=deadline_ms, restricted_s=restricted_s)
        records.append(campaign_record(row, result))
    result = aggregate_campaign(records)
    result.update(plan_path=str(plan_path.resolve()), runs_root=str(runs_root.resolve()))
    write_json(output / "metrics/relay_campaign_analysis.json", result)
    flattened = [dict({key: row.get(key) for key in ("order", "pair_id", "run_id", "rng_seed", "rng_run", "policy", "fault_profile", "run_status", "recorded", "network_eligible")},
                     **row["outcomes"], exclusion_reasons="; ".join(row["exclusion_reasons"])) for row in records]
    endpoint_names = sorted({key for row in records for key in row["outcomes"]})
    write_csv(output / "metrics/relay_campaign_runs.csv", flattened,
        ["order", "pair_id", "run_id", "rng_seed", "rng_run", "policy", "fault_profile", "run_status", "recorded", "network_eligible", *endpoint_names, "exclusion_reasons"])
    write_csv(output / "metrics/relay_campaign_pairs.csv", result["paired_points"],
              ["pair_id", "fault_profile", "metric", "fixed", "failover", "difference"])
    interval_rows = [dict(row, interval_low=row["interval_95"][0] if row["interval_95"] else None,
                         interval_high=row["interval_95"][1] if row["interval_95"] else None) for row in result["paired_estimates"]]
    write_csv(output / "metrics/relay_campaign_intervals.csv", interval_rows,
              ["fault_profile", "metric", "n_pairs", "mean_difference", "median_difference", "interval_low", "interval_high", "interval_method"])
    if plots:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, axes = plt.subplots(2, 3, figsize=(12, 7))
        for column, condition in enumerate(CONDITIONS):
            axis = axes[0, column]
            selected = [row for row in result["paired_points"] if row["fault_profile"] == condition and row["metric"] == "timely_per_offered"]
            for row in selected:
                pair_record = next(r for r in records if r["pair_id"] == row["pair_id"])
                jitter = (int(pair_record["rng_run"]) - 3) * .025
                axis.plot([jitter, 1 + jitter], [row["fixed"], row["failover"]], alpha=.45,
                          color=f"C{(int(pair_record['rng_run']) - 1) % 10}")
            visible = [row for row in records if row["fault_profile"] == condition]
            for row in visible:
                value = finite(row["outcomes"].get("timely_per_offered"))
                if value is not None:
                    at = (0 if row["policy"] == "fixed" else 1) + (int(row["rng_run"]) - 3) * .025
                    axis.scatter(at, value, marker="o" if row["network_eligible"] else "x",
                                 color=f"C{(int(row['rng_run']) - 1) % 10}" if row["network_eligible"] else "grey")
            axis.set(title=f"{condition}: {len(selected)} comparable pairs", xticks=[0, 1], xticklabels=["fixed", "failover"],
                     ylabel="timely / all offered requests", ylim=(-.02, 1.02))
            missing = sum(finite(row["outcomes"].get("timely_per_offered")) is None for row in visible)
            axis.text(.01, .02, f"Missing outcome: {missing}; grey x: excluded", transform=axis.transAxes, fontsize=7)
            axis.grid(alpha=.2)
            effect_axis = axes[1, column]
            estimate = next((row for row in result["paired_estimates"] if row["fault_profile"] == condition and row["metric"] == "timely_per_offered"), None)
            if estimate and estimate["mean_difference"] is not None:
                mean, interval = estimate["mean_difference"], estimate["interval_95"]
                error = [[max(0, mean - interval[0])], [max(0, interval[1] - mean)]] if interval else None
                effect_axis.errorbar(mean, 0, xerr=error, fmt="o", capsize=4)
                effect_axis.text(.02, .9, f"n={estimate['n_pairs']}; {'95% exploratory interval' if interval else 'CI unavailable'}",
                                 transform=effect_axis.transAxes, fontsize=8)
            else:
                effect_axis.text(.5, .5, "No comparable observed pairs", ha="center", transform=effect_axis.transAxes)
            effect_axis.axvline(0, color="grey", linestyle="--")
            effect_axis.set(xlabel="paired mean effect: failover − fixed", yticks=[], ylim=(-1, 1), xlim=(-1.02, 1.02))
            effect_axis.grid(alpha=.2)
        fig.suptitle(f"Whole-run paired outcomes; {result['run_status_counts']}", fontsize=10)
        fig.tight_layout()
        (output / "plots").mkdir(parents=True, exist_ok=True)
        for extension in ("png", "svg", "pdf"):
            fig.savefig(output / f"plots/relay_campaign_pairs.{extension}", dpi=160)
        plt.close(fig)
    report = ["# Observed paired relay campaign", "", f"Status: `{result['campaign_status']}`.",
        f"Recorded {result['recorded_runs']} of {result['planned_runs']} planned runs; statuses: `{result['run_status_counts']}`.",
        "", "[All runs and exclusions](metrics/relay_campaign_runs.csv); [all paired points](metrics/relay_campaign_pairs.csv); "
        "[exploratory intervals](metrics/relay_campaign_intervals.csv).", ""] + result["methods"]
    report.extend(f"Engineering delivery v1, policy `{row['policy']}`, fault `{row['fault_profile']}`: "
                  f"`{row['status']}` ({len(row['distinct_actual_rng_seeds'])} actual seeds; "
                  f"runtime timing qualified: `{row['runtime_timing_eligible']}`). "
                  "Per-UAV evidence and fixed limits are in metrics/relay_campaign_analysis.json."
                  for row in result['engineering_delivery_v1'])
    if plots:
        report += ["", "![Paired outcomes](plots/relay_campaign_pairs.png)"]
    (output / "relay_campaign_analysis.md").write_text("\n".join(report) + "\n")
    return result


def _service_file_binding(path, base):
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError(f"input changed during service analysis: {path}")
    return dict(path=path.relative_to(base).as_posix(), bytes=after.st_size, sha256=digest.hexdigest())


def _service_small_snapshot(path, base):
    before = path.stat()
    raw = path.read_bytes()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError(f"input changed during service analysis: {path}")
    return raw, dict(path=path.relative_to(base).as_posix(), bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())


def _service_attempt(run_dir, profiles, order, planned=None):
    """Rebuild delivery observations from raw files, never cached pass reports."""
    from network.coordination.service_profiles import qualify_payload_services
    run_dir = Path(run_dir).resolve()
    attempt = dict(order=order, run_id=run_dir.name, run_dir=str(run_dir),
        planned_case=copy.deepcopy(planned), input_binding_errors=[], source_files=[],
        conditions=None, condition_sha256=None, record_counts={}, qualification=None,
        runtime_status="not_recorded", mission_outcome="not_recorded", measurement_validity="not_recorded")
    control_dir = run_dir.parents[1] / "control" / run_dir.name
    try:
        if not run_dir.is_dir():
            attempt["status"] = "not_started" if planned and planned.get("status") == "not_started" else "missing_attempt_artifacts"
            return attempt
        from bas import unique_json_fields
        def frozen_json(path, base):
            raw, binding = _service_small_snapshot(path, base)
            value = json.loads(raw, object_pairs_hook=unique_json_fields)
            if not isinstance(value, dict):
                raise ValueError(f"frozen metadata must be a JSON object: {path}")
            return value, binding
        control, control_binding = frozen_json(control_dir / "run.json", run_dir.parents[1])
        config, config_binding = frozen_json(control_dir / "config.json", run_dir.parents[1])
        summary, summary_binding = frozen_json(run_dir / "metrics/relay_experiment_summary.json", run_dir)
        stats, stats_binding = frozen_json(run_dir / "metrics/native_radio_stats.json", run_dir)
        _, load_binding = frozen_json(run_dir / "metrics/relay_experiment_config.json", run_dir)
        raw_environment, environment_binding = _service_small_snapshot(run_dir / "environment.txt", run_dir)
        environment = dict(line.split("=", 1) for line in raw_environment.decode().splitlines() if "=" in line)
        attempt["source_files"] += [control_binding, config_binding]
        source_files = [summary_binding, load_binding, stats_binding, environment_binding]
        errors = attempt["input_binding_errors"]
        if control.get("run_id") != run_dir.name or summary.get("run_id") != run_dir.name:
            errors.append("run_identity_disagrees")
        source = control.get("source_commit")
        if (not isinstance(source, str) or len(source) != 40 or any(c not in "0123456789abcdef" for c in source)
                or environment.get("git_head") != source or control.get("source_dirty") is not False
                or environment.get("source_dirty_paths") != "0"):
            errors.append("source_revision_not_clean_or_bound")
        for name, length in (("ns3_exact_sha", 40), ("native_binary_sha256", 64)):
            value = environment.get(name)
            if not isinstance(value, str) or len(value) != length or any(c not in "0123456789abcdef" for c in value):
                errors.append(name + "_identity_missing")
        labels = {"scene"}
        if config["propagation"]["model"] in ("sionna", "hybrid"):
            labels.add("radio")
        if config.get("interference", {}).get("sources") is not None:
            labels.add("sources")
        input_sha = {}
        for label in ("scene", "radio", "sources"):
            digest = None
            if label in labels:
                suffix = ".json" if label == "scene" else ".yaml"
                binding = _service_file_binding(control_dir / (label + suffix), run_dir.parents[1])
                attempt["source_files"].append(binding)
                digest = binding["sha256"]
                if control.get(label + "_sha256") != digest:
                    errors.append(label + "_frozen_input_hash_disagrees")
            elif control.get(label + "_sha256") is not None:
                errors.append(label + "_unexpected_recorded_input")
            input_sha[label] = digest
        solver = stats.get("sionna_solver")
        metadata = dict(run_id=run_dir.name, source_commit=source,
            source_dirty_paths=environment.get("source_dirty_paths"), rng_seed=stats.get("rng_seed"),
            rng_run=stats.get("rng_run"), rt_solver_seed=solver.get("seed") if isinstance(solver, dict) else None,
            source_files=source_files)
        if (metadata["rng_seed"] != config["experiment"]["seed"] or metadata["rng_run"] != config["experiment"]["run"]):
            errors.append("declared_and_actual_rng_identity_disagree")
        if planned and (planned.get("seed") != metadata["rng_seed"] or planned.get("rng_run") != metadata["rng_run"]):
            errors.append("planned_and_actual_rng_identity_disagree")
        attempt.update({key: control.get(key, "not_recorded") for key in
                        ("runtime_status", "mission_outcome", "measurement_validity")})
        for field in ("runtime_status", "measurement_validity"):
            if control.get(field) != summary.get(field):
                errors.append(field + "_control_summary_disagrees")
        start, stop = summary.get("measurement_start_host_monotonic_ns"), summary.get("measurement_end_host_monotonic_ns")
        runtime_valid = (attempt["runtime_status"] == "completed" and attempt["measurement_validity"] == "valid"
            and summary.get("measurement_window_completed") is True
            and stats.get("stop_reason") in ("duration", "sionna_ns3_process_stopped") and not errors)
        lineage = analyze_payload_lineage(run_dir, start, stop, complete=runtime_valid)
        if errors:
            lineage["measurement_eligible"] = False
        load = lineage.get("offered_load", {}).get("profile")
        if load != config.get("flows", {}).get("payload_load"):
            errors.append("audited_load_profile_missing" if load is None else "control_and_audited_load_profile_disagree")
            lineage["measurement_eligible"] = False
        if lineage.get("offered_load", {}).get("config_sha256") not in (None, load_binding["sha256"]):
            errors.append("offered_configuration_changed_during_analysis")
            lineage["measurement_eligible"] = False
        qualification = qualify_payload_services(lineage, profiles, run_metadata=metadata)
        counts = Counter()
        for row in lineage.get("records", []):
            if row.get("load_id") is not None:
                counts[f'{row["uav_id"]}|{row["direction"]}'] += 1
        fixed_config = copy.deepcopy(config)
        fixed_config["experiment"].pop("run", None)
        conditions = dict(effective_config_except_rng_run=fixed_config, service_profile_sha256=profiles["sha256"], source_commit=source,
            image=config["runtime"]["image"], propagation_model=config["propagation"]["model"],
            scene_sha256=input_sha["scene"], radio_sha256=input_sha["radio"], sources_sha256=input_sha["sources"],
            uav_ids=list(range(1, config["drone"]["count"] + 1)), load_profile=config.get("flows", {}).get("payload_load"),
            ns3_seed=metadata["rng_seed"], rt_solver_seed=metadata["rt_solver_seed"],
            observation_duration_s=config["experiment"]["duration_s"],
            runtime_implementation={key: environment.get(key) for key in (
                "ns3_version", "ns3_exact_sha", "native_binary_sha256", "sionna", "sionna-rt",
                "mitsuba", "drjit", "pybind11", "cppyy", "physics_profile", "state_transport",
                "stack_cpuset", "radio_cpuset", "pose_publication_rate_hz", "sionna_worker_count")})
        attempt.update(qualification=qualification, metadata=metadata, conditions=conditions,
            condition_sha256=hashlib.sha256(json.dumps(conditions, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest(),
            record_counts=dict(counts), runtime_valid=runtime_valid and not errors,
            status="invalid" if errors else qualification["status"], source_files=attempt["source_files"] + source_files,
            evaluation_origin="fresh_raw_payload_lineage",
            runtime_gate_scope="Recorded runtime/measurement gates plus actual native stop reason; this service comparison does not rehash the full RF-event capture.")
    except (ValueError, TypeError, KeyError, OSError, ImportError) as error:
        attempt.update(status="invalid", runtime_valid=False)
        attempt["input_binding_errors"].append(f"{type(error).__name__}: {error}")
    return attempt


def _aggregate_service_attempts(attempts, profiles):
    """All attempts stay visible; no best repeat or rolling-window double count."""
    from network.coordination.service_profiles import _combined_status
    config = profiles["config"]
    grouped, ungrouped = defaultdict(list), []
    for attempt in attempts:
        if attempt.get("condition_sha256") is None:
            ungrouped.append(attempt["order"])
        else:
            grouped[attempt["condition_sha256"]].append(attempt)
    groups = []
    for fingerprint, members in grouped.items():
        pairs, unknown = defaultdict(list), []
        for attempt in members:
            identity = attempt.get("qualification", {}).get("replication_qualification", {}).get("identity", {}).get("key")
            if (not isinstance(identity, list) or len(identity) != 2 or any(type(x) is not int for x in identity)):
                unknown.append(attempt["order"])
            else:
                pairs[tuple(identity)].append(attempt)
        counted = Counter()
        pair_rows = []
        for pair, repeats in sorted(pairs.items()):
            statuses = [a["status"] for a in repeats]
            measurable = all(a.get("runtime_valid") is True and a["status"] in ("measured_pass", "measured_fail")
                             and not a.get("input_binding_errors") for a in repeats)
            streams = set().union(*(set(a["record_counts"]) for a in repeats))
            minimum = {key: min(a["record_counts"].get(key, 0) for a in repeats) for key in streams}
            if measurable:
                counted.update(minimum)
            pair_rows.append(dict(key=list(pair), attempt_orders=[a["order"] for a in repeats],
                repeat_count=len(repeats), observed_service_status=_combined_status(statuses),
                measurement_eligible=measurable, independent_counted_records=minimum if measurable else {},
                record_count_rule="Count the smallest observed offer count across repeats once; retain every repeat's outcome."))
        eligible_pairs = sum(pair["measurement_eligible"] for pair in pair_rows)
        statuses = [a["status"] for a in members]
        observed = _combined_status(statuses)
        stream_names = {f'{uav}|{members[0]["conditions"]["load_profile"]["direction"]}'
                        for uav in (members[0]["conditions"].get("load_profile") or {}).get("uav_ids", [])}
        checks = dict(all_attempts_valid=all(a.get("runtime_valid") is True and not a.get("input_binding_errors") for a in members),
            all_observed_service_windows_pass=all(s == "measured_pass" for s in statuses),
            rng_identity_complete=not unknown,
            independent_replications=eligible_pairs >= config["minimum_independent_replications"],
            independent_records_per_stream=bool(stream_names) and all(counted[name] >= config["minimum_records_per_stream"] for name in stream_names))
        grade = ("qualified_observed_transport_series" if all(checks.values()) else
                 "invalid_or_incomplete_attempts" if not checks["all_attempts_valid"] else
                 "observed_service_limits_failed" if "measured_fail" in statuses else "insufficient_replication_evidence")
        groups.append(dict(condition_sha256=fingerprint, conditions=members[0]["conditions"],
            attempt_orders=[a["order"] for a in members], observed_service_status=observed,
            configured_rng_pairs=len(pairs), eligible_independent_replications=eligible_pairs,
            unknown_rng_attempt_orders=unknown, independent_counted_records=dict(counted), pairs=pair_rows,
            replication_qualification=grade, checks=checks,
            all_missions_succeeded=all(a.get("mission_outcome") == "succeeded" for a in members),
            scope="Fixed ns3 seed with different RngRun substreams; fixed RT seed and all listed inputs. This is transport-load evidence, not semantic application or hardware certification."))
    qualified = bool(groups) and not ungrouped and all(g["replication_qualification"] == "qualified_observed_transport_series" for g in groups)
    return dict(schema_version=1, profile_id=config["profile_id"], service_profile_sha256=profiles["sha256"],
        service_profile_path=profiles["path"], profile_planning_defaults=dict(
            minimum_independent_replications=config["minimum_independent_replications"],
            minimum_records_per_stream=config["minimum_records_per_stream"]),
        status="qualified_observed_transport_series" if qualified else "not_qualified",
        supplied_attempts=len(attempts), attempt_status_counts=dict(Counter(a["status"] for a in attempts)),
        ungrouped_attempt_orders=ungrouped, groups=groups, attempts=attempts,
        retention_policy="Every supplied/planned attempt is retained, including failed, missing, not-started and repeated executions; no best-of selection.",
        independence_policy="Group by fixed ns3 seed and fixed RT seed/inputs; deduplicate(seed,RngRun) within each group. Repeated execution is not another independent replication.",
        interpretation="A valid mission failure remains separately visible; service grade does not rewrite runtime, mission or measurement outcomes.")


def analyze_service_series(run_dirs, output_dir=None, *, profile_path=None, planned_cases=None):
    from network.coordination.service_profiles import load_service_profiles
    profiles = load_service_profiles(profile_path)
    plans = planned_cases or [None] * len(run_dirs)
    if len(plans) != len(run_dirs):
        raise ValueError("one planned case is required for every service-series input")
    attempts = [_service_attempt(path, profiles, order, planned)
                for order, (path, planned) in enumerate(zip(run_dirs, plans), 1)]
    result = _aggregate_service_attempts(attempts, profiles)
    if output_dir is not None:
        target = Path(output_dir) / "metrics/service_series_analysis.json"
        if target.exists():
            raise ValueError("service-series report exists; use a fresh output directory")
        write_json(target, result)
    return result


def analyze_service_batch(plan_path, runs_root, output_dir=None, *, profile_path=None):
    """Consume the existing run_batch.py ledger, retaining unstarted cases."""
    from bas import identifier, unique_json_fields
    from network.coordination.service_profiles import load_service_profiles
    plan_path = Path(plan_path).resolve()
    raw_plan, plan_binding = _service_small_snapshot(plan_path, plan_path.parent)
    plan = json.loads(raw_plan, object_pairs_hook=unique_json_fields)
    if not isinstance(plan, dict):
        raise ValueError("batch ledger must be an object")
    cases = plan.get("cases")
    if (type(plan.get("schema_version")) is not int or plan["schema_version"] != 1
            or not isinstance(cases, list) or not cases or plan.get("planned_cases") != len(cases)):
        raise ValueError("service batch requires an existing schema1 batch ledger and all planned cases")
    ids = []
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("batch cases must be objects")
        run_id = identifier(case.get("run_id"))
        if case.get("run_record") != f"runs/control/{run_id}/run.json" or run_id in ids:
            raise ValueError("batch case IDs/paths must be unique and match the existing run controller")
        ids.append(run_id)
    result = analyze_service_series([Path(runs_root) / run_id for run_id in ids], None,
                                   profile_path=profile_path, planned_cases=cases)
    # Cross-check declared batch expectations, retaining any mismatched attempt.
    for attempt, case in zip(result["attempts"], cases):
        if attempt.get("conditions") is None:
            continue
        expected_config = copy.deepcopy(plan.get("config"))
        if not isinstance(expected_config, dict) or not isinstance(expected_config.get("experiment"), dict):
            raise ValueError("batch ledger must retain its effective config")
        expected_config["experiment"].update(seed=case["seed"])
        expected_config["experiment"].pop("run", None)
        errors = attempt["input_binding_errors"]
        if attempt["conditions"]["effective_config_except_rng_run"] != expected_config:
            errors.append("attempt_config_disagrees_with_batch_plan")
        for label, entry in plan.get("input_sha256", {}).items():
            if label in ("scene", "radio", "sources") and (not isinstance(entry, dict) or
                    attempt["conditions"].get(label + "_sha256") != entry.get("sha256")):
                errors.append(label + "_disagrees_with_batch_plan")
        if errors:
            attempt.update(status="invalid", runtime_valid=False)
    profiles = load_service_profiles(profile_path)
    if profiles["sha256"] != result["service_profile_sha256"]:
        raise ValueError("service profile changed during batch analysis")
    if _service_file_binding(plan_path, plan_path.parent) != plan_binding:
        raise ValueError("batch ledger changed during analysis")
    result = _aggregate_service_attempts(result["attempts"], profiles)
    result["batch_source"] = dict(path=str(plan_path), sha256=plan_binding["sha256"],
                                batch_id=plan.get("batch_id"), recorded_status=plan.get("status"))
    if output_dir is not None:
        target = Path(output_dir) / "metrics/service_series_analysis.json"
        if target.exists():
            raise ValueError("service-series report exists; use a fresh output directory")
        write_json(target, result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--run-dir", type=Path)
    mode.add_argument("--service-series", type=Path, nargs="+", help="Recompute and compare all raw service attempts")
    mode.add_argument("--service-batch", type=Path, help="Compare every case in an existing run_batch.py batch.json")
    mode.add_argument("--write-plan", type=Path, help="Write the randomized 30-row plan; no simulator is launched")
    mode.add_argument("--campaign-plan", type=Path, help="Analyze all rows of an existing frozen plan")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--deadline-ms", type=float, default=250.)
    parser.add_argument("--restricted-horizon-s", type=float, default=30.)
    parser.add_argument("--no-plots", action="store_true")
    parser.add_argument("--skip-live-export", action="store_true")
    parser.add_argument("--runs-root", type=Path)
    parser.add_argument("--service-profile", type=Path)
    args = parser.parse_args(argv)
    if not args.deadline_ms > 0 or not math.isfinite(args.deadline_ms) or not args.restricted_horizon_s > 0 or not math.isfinite(args.restricted_horizon_s):
        parser.error("finite positive deadline and restricted horizon required")
    if args.service_series or args.service_batch:
        if args.output_dir is None or args.service_batch and args.runs_root is None:
            parser.error("service comparison requires --output-dir; --service-batch also requires --runs-root")
        if args.service_batch:
            result = analyze_service_batch(args.service_batch, args.runs_root, args.output_dir,
                                           profile_path=args.service_profile)
        else:
            result = analyze_service_series(args.service_series, args.output_dir, profile_path=args.service_profile)
        print(json.dumps(dict(status=result["status"], supplied_attempts=result["supplied_attempts"],
                              attempt_status_counts=result["attempt_status_counts"], conditions=len(result["groups"]))))
        return 0
    if args.write_plan:
        rows = campaign_plan()
        fields = list(rows[0])
        if args.write_plan.exists():
            if read_csv(args.write_plan) != [{key: str(value) for key, value in row.items()} for row in rows]:
                parser.error("existing plan differs; use a fresh plan path")
        else:
            write_csv(args.write_plan, rows, fields)
        print(json.dumps(dict(plan=str(args.write_plan.resolve()), planned_runs=len(rows), executed_runs=0)))
        return 0
    if args.campaign_plan:
        if args.runs_root is None or args.output_dir is None:
            parser.error("--campaign-plan requires --runs-root and --output-dir")
        result = analyze_campaign(args.campaign_plan, args.runs_root, args.output_dir,
                                  plots=not args.no_plots, live_export=not args.skip_live_export,
                                  deadline_ms=args.deadline_ms, restricted_s=args.restricted_horizon_s)
        print(json.dumps(dict(status=result["campaign_status"], counts=result["run_status_counts"]), sort_keys=True))
        return 0
    if not args.run_dir.is_dir():
        parser.error("run directory does not exist")
    result = analyze_run(args.run_dir, args.output_dir, deadline_ms=args.deadline_ms,
                         restricted_s=args.restricted_horizon_s, plots=not args.no_plots,
                         live_export=not args.skip_live_export)
    print(json.dumps(dict(run_id=result["run_id"], status=result["run_status"],
                         control_recovery=result["control_recovery"], data_quality_errors=result["data_quality_errors"]), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
