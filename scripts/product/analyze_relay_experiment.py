#!/usr/bin/env python3
"""Analyze observed BAS relay runs and prepare a paired campaign; never run SITL."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import importlib.util
import itertools
import json
import math
from pathlib import Path
import random
import statistics
from typing import Any

UAVS = tuple(range(1, 6))
CONDITIONS = ("none", "selective", "total_GCS")


def finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
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


def read_csv(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    with path.open(newline="") as stream:
        return list(csv.DictReader(stream))


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
                p5=percentile(5), p50=percentile(50), p95=percentile(95),
                min=min(values) if values else None, max=max(values) if values else None)


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
    native = read_csv(run_dir / "logs/native_radio_events.csv")
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
        policy_allowed = policy != "fixed" or leader == initial_master
        commands.append(dict(command_id=sent["command_id"], uav_id=uav, leader_id=leader,
            epoch=sent.get("epoch"), created_ns=created,
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
    goals = []
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
        point = min(choices) if choices else None
        goal_recovery[str(i)] = recovery(point, fault, end, applicable=applicable, restricted_s=restricted_s)
        completion_gap[str(i)] = ((point - stamp(previous)) / 1e9 if point is not None and previous else None)
    group_goal_point = (fault + max(row["time_s"] for row in goal_recovery.values()) * 1e9
                        if fault is not None and all(row["recovered"] is True for row in goal_recovery.values()) else None)
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
        admission_audit=dict(audit), unverifiable_admissions=sum(value for key, value in audit.items() if key.startswith("unverifiable")),
        admission_total=len(admissions),
        command_lineage_errors=sum(row["lineage_status"] == "unverifiable_or_mismatched" for row in commands),
        total_GCS=dict(applicable=condition == "total_GCS", all_grants_issued_during_fault=len(issued_during),
            new_leader_selection_grants_during_fault=len(selected_during), observed_hold_uavs=sorted(held),
            hold_all_five=len(held) == 5 if condition == "total_GCS" else None,
            no_new_leader_selection=len(selected_during) == 0 if condition == "total_GCS" and fault is not None else None),
        criteria=meta.get("criteria", {}), metadata=dict(environment=environment, config=config, native_stats=stats,
            configuration_snapshot=read_json(run_dir / "configuration/final/manifest.json") or read_json(run_dir / "configuration/initial/manifest.json"),
            physics_runtime=read_json(run_dir / "logs/physics_runtime.json")),
        data_quality_errors=errors,
        definitions=dict(deadline_ms=deadline_ms, latency_clock="host monotonic; command creation to actual UART write end; send-event latency reported separately",
            restricted_horizon_s=restricted_s,
            offered_denominator="all unique offered refresh requests, including unsent",
            sent_denominator="all unique target-bearing GCS sends, including lost/late; timely requires verified lease/payload lineage and complete application relay chain",
            group_recovery="first postfault-created refresh admitted to every member under one leader/epoch",
            goal_recovery="new postfault goal, target changed >=1m from last pre-fault admitted target, observed maneuver and dwell; not latest refresh creation",
            completion_gap="last pre-fault completed goal to first eligible new goal completion",
            censoring="right-censored time is never substituted as a successful recovery",
            restricted_loss=f"min(time to first recovery, {restricted_s}s); a bounded loss, not an imputed recovery; missing if horizon not observed",
            two_hop_evidence="application stage lineage; native PHY/PCAP proof remains a separate requirement",
            total_GCS="renewals before failure detection can be issued but lost; new leader selection must remain absent"))
    if not meta:
        errors.append("relay_experiment_summary.json missing; no successful experiment inferred")
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
    write_csv(output / "metrics/relay_commands.csv", commands, ["command_id", "uav_id", "leader_id", "epoch", "created_ns",
        "sent_ns", "admitted_ns", "admission_event_ns", "grant_sequence", "lease_valid_from_ns", "lease_valid_until_ns", "admission_checked_ns",
        "latency_ms", "sent_to_uart_latency_ms", "timely", "lease_status", "lineage_status", "application_chain_complete", "policy_allowed", "target_position_m"])
    write_csv(output / "metrics/relay_goals.csv", goals, ["uav_id", "goal_id", "created_ns", "completed_ns",
        "qualifies_postfault_recovery", "target_changed_from_prefault_admission", "maneuver_observed", "dwell_s"])
    recovery_rows = [dict(endpoint=f"uav{i}", **per_uav[str(i)]) for i in UAVS]
    recovery_rows += [dict(endpoint=name, **row) for name, row in control.items()]
    write_csv(output / "metrics/relay_recovery.csv", recovery_rows, ["endpoint", "status", "recovered", "time_s",
        "censor_time_s", "restricted_horizon_s", "restricted_unrecovered_time_s", "leader_id", "epoch", "members"])
    write_json(output / "metrics/relay_analysis.json", result)
    if plots:
        plot_run(output, result, commands, events)
    report_lines = [f"# Observed relay analysis: {result['run_id']}", "",
        f"Policy `{policy}`, fault `{condition}`, recorded status `{result['run_status']}`.",
        f"Raw: `{run_dir}`.", "", "[Numerical outcomes and definitions](metrics/relay_analysis.json).",
        "[Commands, including missing delivery](metrics/relay_commands.csv); [censored recovery](metrics/relay_recovery.csv).",
        "Renewals issued before detecting total GCS loss are reported separately from new leader appointments."]
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
    "wifi_data_mode", "wifi_control_mode", "wifi_explicit_bar_after_missed_block_ack_requested", "wifi_aggregation_requested")
RADIO_FIELDS = ("propagation_profile", "wifi_topology", "fault_extra_loss_db", "fault_master_id", "tx_power_w",
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
        analysis=result.get("definitions", {}))
    record.update(recorded=terminal, has_artifacts=True, run_status=result.get("run_status", "not_recorded"),
        run_error=result.get("run_error"), raw_run_dir=result.get("raw_run_dir"),
        analysis_path=str(Path(result["output_dir"]) / "metrics/relay_analysis.json"),
        network_eligible=not reasons, exclusion_reasons=reasons, outcomes=outcomes, comparability=signature,
        study_eligible=not study_reasons,
        admission_audit=result.get("admission_audit", {}), criteria=criteria)
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
    if plots:
        report += ["", "![Paired outcomes](plots/relay_campaign_pairs.png)"]
    (output / "relay_campaign_analysis.md").write_text("\n".join(report) + "\n")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--run-dir", type=Path)
    mode.add_argument("--write-plan", type=Path, help="Write the randomized 30-row plan; no simulator is launched")
    mode.add_argument("--campaign-plan", type=Path, help="Analyze all rows of an existing frozen plan")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--deadline-ms", type=float, default=250.)
    parser.add_argument("--restricted-horizon-s", type=float, default=30.)
    parser.add_argument("--no-plots", action="store_true")
    parser.add_argument("--skip-live-export", action="store_true")
    parser.add_argument("--runs-root", type=Path)
    args = parser.parse_args(argv)
    if not args.deadline_ms > 0 or not math.isfinite(args.deadline_ms) or not args.restricted_horizon_s > 0 or not math.isfinite(args.restricted_horizon_s):
        parser.error("finite positive deadline and restricted horizon required")
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
