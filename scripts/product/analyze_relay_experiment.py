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
        low = finite(row.get("valid_from_ns", row.get("command_valid_from_ns")))
        high = finite(row.get("valid_until_ns", row.get("command_valid_until_ns")))
        status = "unverifiable_missing_boundary_or_interval"
        if checked is not None and low is not None and high is not None:
            status = "valid" if low <= checked < high else "expired_or_not_yet_valid"
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
        admitted = next((row for row in records if stage(row) == "uart_admitted" and inside(stamp(row))), None)
        latency = (stamp(admitted) - stamp(sent)) / 1e6 if admitted is not None and stamp(sent) is not None else None
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
        commands.append(dict(command_id=sent["command_id"], uav_id=uav, leader_id=leader,
            epoch=sent.get("epoch"), created_ns=finite(sent.get("command_created_host_monotonic_ns")),
            sent_ns=stamp(sent), admitted_ns=stamp(admitted) if admitted else None,
            grant_sequence=admitted.get("grant_sequence") if admitted else None,
            lease_valid_from_ns=finite(admitted.get("valid_from_ns")) if admitted else None,
            lease_valid_until_ns=finite(admitted.get("valid_until_ns")) if admitted else None,
            admission_checked_ns=finite(admitted.get("admission_checked_host_monotonic_ns")) if admitted else None,
            latency_ms=latency, timely=latency is not None and 0 <= latency <= deadline_ms
                and lease_status == "valid" and lineage == "valid",
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
            sent_to_uart_latency_ms=distribution([row["latency_ms"] for row in sent if row["latency_ms"] is not None]))
    eligible = [row for row in commands if fault is not None and row["created_ns"] is not None
                and row["created_ns"] >= fault and row["admitted_ns"] is not None and row["admitted_ns"] >= fault
                and row["lease_status"] == "valid" and row["lineage_status"] == "valid" and row["application_chain_complete"]]

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
    handover = group_points([row for row in eligible if row["leader_id"] != initial_master and row["epoch"] != initial_epoch])
    per_uav = {str(i): recovery(min((r["admitted_ns"] for r in eligible if r["uav_id"] == i), default=None),
                fault, end, applicable=applicable, restricted_s=restricted_s) for i in UAVS}
    completions = [row for row in events if stage(row) == "action_completed" and inside(stamp(row))]
    goals = []
    goal_recovery, completion_gap = {}, {}
    hold_s = float(config.get("hold_s", 2.))
    for i in UAVS:
        before = [row for row in completions if identifier(row.get("uav_id")) == i and fault is not None and stamp(row) < fault]
        previous = max(before, key=stamp) if before else None
        reference = target(previous) if previous else None
        choices = []
        for row in completions:
            if identifier(row.get("uav_id")) != i:
                continue
            created = finite(row.get("goal_created_host_monotonic_ns"))
            changed = reference is not None and target(row) is not None and math.dist(reference, target(row)) >= 1.
            admission = command_index.get(str(row.get("admitted_command_id", row.get("command_id"))))
            qualifies = bool(fault is not None and created is not None and created >= fault and stamp(row) >= fault
                and changed and row.get("maneuver_observed") is True and (finite(row.get("dwell_s")) or 0) >= hold_s
                and admission is not None and admission["lease_status"] == "valid"
                and admission["lineage_status"] == "valid" and admission["application_chain_complete"]
                and admission["created_ns"] is not None and admission["created_ns"] >= created
                and admission["admitted_ns"] is not None and admission["admitted_ns"] <= stamp(row))
            goals.append(dict(uav_id=i, goal_id=row.get("goal_id"), completed_ns=stamp(row), created_ns=created,
                qualifies_postfault_recovery=qualifies, target_changed_from_prefault_completion=changed,
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
    selected_during = [row for row in issued_during if row.get("reason") == "selection"
                       and (row.get("leader_id") != initial_master or row.get("epoch") != initial_epoch)]
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
        policy=policy, fault_profile=condition, pilot=bool(meta.get("pilot", config.get("pilot", False))),
        run_status=meta.get("status", "not_recorded"), run_error=meta.get("error"),
        observation_window=dict(start_host_monotonic_ns=start, end_host_monotonic_ns=end, complete=full_window),
        actual_fault=dict(start_host_monotonic_ns=fault, end_host_monotonic_ns=fault_end, events=fault_events),
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
        criteria=meta.get("criteria", {}), metadata=dict(environment=environment, config=config),
        data_quality_errors=errors,
        definitions=dict(deadline_ms=deadline_ms, latency_clock="host monotonic; gcs_send event to UART event",
            offered_denominator="all unique offered refresh requests, including unsent",
            sent_denominator="all unique target-bearing GCS sends, including lost/late; timely requires verified lease/payload lineage",
            group_recovery="first postfault-created refresh admitted to every member under one leader/epoch",
            goal_recovery="new postfault goal, changed target >=1m, observed maneuver and dwell; not latest refresh creation",
            censoring="right-censored time is never substituted as a successful recovery",
            restricted_loss=f"unrecovered time restricted to {restricted_s}s; missing if horizon not observed",
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
        "sent_ns", "admitted_ns", "grant_sequence", "lease_valid_from_ns", "lease_valid_until_ns", "admission_checked_ns",
        "latency_ms", "timely", "lease_status", "lineage_status", "application_chain_complete", "target_position_m"])
    write_csv(output / "metrics/relay_goals.csv", goals, ["uav_id", "goal_id", "created_ns", "completed_ns",
        "qualifies_postfault_recovery", "target_changed_from_prefault_completion", "maneuver_observed", "dwell_s"])
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
    axes[0, 1].set(title="Delivery CDF; denominator includes lost sends", xlabel="GCS send → UART (ms)", ylabel="fraction of all sent", ylim=(0, 1.02))
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--deadline-ms", type=float, default=250.)
    parser.add_argument("--restricted-horizon-s", type=float, default=30.)
    parser.add_argument("--no-plots", action="store_true")
    parser.add_argument("--skip-live-export", action="store_true")
    args = parser.parse_args(argv)
    result = analyze_run(args.run_dir, args.output_dir, deadline_ms=args.deadline_ms,
                         restricted_s=args.restricted_horizon_s, plots=not args.no_plots,
                         live_export=not args.skip_live_export)
    print(json.dumps(dict(run_id=result["run_id"], status=result["run_status"],
                         control_recovery=result["control_recovery"], data_quality_errors=result["data_quality_errors"]), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
