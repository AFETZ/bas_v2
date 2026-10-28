#!/usr/bin/env python3
"""Read-only urban BAS export. A model label, UART and physical progress differ.

This module reuses the reviewed relay command/admission audit, not its vertical
goal or injected-fault endpoints. Geometry and physical dwell are replayed here.
No simulator is started, no source raw file is written, and no gap is filled.
"""
from __future__ import annotations

import argparse
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
import csv
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import random
import re
import sys

SPEC = importlib.util.spec_from_file_location("urban_relay_audit", Path(__file__).with_name("analyze_relay_experiment.py"))
RELAY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RELAY)
UAVS = tuple(range(1, 6))
NS = 1_000_000_000


def number(value):
    return RELAY.finite(value)


def stamp(row):
    return number(row.get("host_monotonic_ns", row.get("wall_monotonic_ns")))


def stage(row):
    return row.get("stage", row.get("event"))


def node_id(value):
    text = str(value).lower()
    if text in ("0", "cp", "gcs"):
        return 0
    return RELAY.identifier(value)


def boolean(value):
    if value is True or str(value).lower() in ("1", "true", "los"):
        return True
    if value is False or str(value).lower() in ("0", "false", "nlos"):
        return False
    return None


def write_rows(path, rows, fields=None):
    if fields is None:
        fields = list(dict.fromkeys(key for row in rows for key in row))
    RELAY.write_csv(path, rows, fields)


def load_events(run, errors):
    combined = run / "logs/coordination_events.jsonl"
    paths = [combined] if combined.is_file() else sorted((run / "logs").glob("coordination_*.jsonl"))
    rows = [row for path in paths for row in RELAY.read_jsonl(path, errors)]
    return sorted(rows, key=lambda row: stamp(row) or 0)


def load_geometry(path, errors):
    """The native sampler emits observed host time and ns-3 time independently."""
    result, seen = [], set()
    for index, row in enumerate(RELAY.read_csv(path), 2):
        a, b, at = node_id(row.get("from_id")), node_id(row.get("to_id")), stamp(row)
        los = boolean(row.get("geom_los"))
        model = number(row.get("time_s"))
        if a is None or b is None or a == b or at is None or model is None or los is None:
            errors.append(f"geometry_links.csv:{index}: invalid link/clock/LOS")
            continue
        pair = tuple(sorted((a, b)))
        key = pair, at, los
        if key in seen:
            continue
        seen.add(key)
        result.append(dict(row, from_id=a, to_id=b, pair=f"{pair[0]}-{pair[1]}",
                           host_monotonic_ns=at, ns3_time_s=model, geom_los=los,
                           pathloss_db=number(row.get("pathloss_db"))))
    return sorted(result, key=lambda row: row["host_monotonic_ns"])


def geometry_transitions(rows, start, end):
    previous, result = {}, []
    for row in rows:
        at = stamp(row)
        if end is not None and at > end:
            break
        old = previous.get(row["pair"])
        if old is not None and old["geom_los"] != row["geom_los"] and start is not None and at >= start:
            result.append(dict(row, transition="LOS_return" if row["geom_los"] else "NLOS_entry",
                               previous_observed_host_ns=stamp(old),
                               transition_bracket_s=(at-stamp(old))/NS))
        previous[row["pair"]] = row
    return result


def endpoint(point, trigger, end, *, full_window, data_valid=True, applicable=True):
    base = dict(time_s=None, event_host_ns=point, censor_time_s=None, recovered=None)
    if not applicable:
        return dict(base, status="not_applicable_no_handover")
    if trigger is None:
        return dict(base, status="not_applicable_no_observed_NLOS_transition")
    if end is None or end < trigger or not data_valid:
        return dict(base, status="unverifiable_recording")
    if point is not None and trigger <= point <= end:
        return dict(base, status="observed", recovered=True, time_s=(point-trigger)/NS)
    if full_window:
        return dict(base, status="administratively_right_censored", recovered=False,
                    censor_time_s=(end-trigger)/NS)
    return dict(base, status="interrupted_attempt", recovered=None)


def admitted(command):
    return (command.get("admitted_ns") is not None and command.get("lease_status") == "valid"
            and command.get("lineage_status") == "valid" and command.get("application_chain_complete")
            and command.get("policy_allowed"))


def uart_endpoints(commands, trigger, end, full_window, data_valid, *, initial_leader=1,
                   initial_epoch=1, handover=False, handover_observed=False):
    rows = [row for row in commands if admitted(row) and trigger is not None and end is not None
            and row.get("created_ns") is not None and trigger <= row["created_ns"] <= row["admitted_ns"] <= end
            and (not handover or (row["leader_id"] != initial_leader and row["epoch"] > initial_epoch))]
    result, scopes = {}, defaultdict(dict)
    for row in sorted(rows, key=lambda r: r["admitted_ns"]):
        scopes[(row["leader_id"], row["epoch"])].setdefault(row["uav_id"], row)
    for i in UAVS:
        row = min((r for r in rows if r["uav_id"] == i), key=lambda r:r["admitted_ns"], default=None)
        result[f"uav{i}"] = dict(endpoint(row["admitted_ns"] if row else None, trigger, end,
            full_window=full_window, data_valid=data_valid, applicable=not handover or handover_observed),
            command_id=row["command_id"] if row else None,
            leader_id=row["leader_id"] if row else None, epoch=row["epoch"] if row else None)
    candidates = [(max(scope[i]["admitted_ns"] for i in UAVS), leader, epoch, scope)
                  for (leader, epoch), scope in scopes.items() if set(UAVS) <= set(scope)]
    selected = min(candidates, key=lambda item:item[:3]) if candidates else None
    result["all_five"] = dict(endpoint(selected[0] if selected else None, trigger, end,
        full_window=full_window, data_valid=data_valid, applicable=not handover or handover_observed),
        leader_id=selected[1] if selected else None, epoch=selected[2] if selected else None,
        command_ids={str(i): selected[3][i]["command_id"] for i in UAVS} if selected else None,
        definition="last first valid post-trigger-created target CID, all five under one relay/epoch")
    return result


def read_poses(path, start, end, errors):
    """Deduplicate actual odometry; invalid/stale publications are gap markers."""
    result, previous = [], {}
    if not path.is_file():
        return result
    with path.open() as stream:
        for line_no, line in enumerate(stream, 1):
            try:
                row = json.loads(line)
            except ValueError:
                errors.append(f"node_state.jsonl:{line_no}: malformed JSON")
                continue
            host = number(row.get("published_monotonic_ns"))
            if host is None or start is None or end is None or not start <= host <= end:
                continue
            for node in row.get("nodes", []):
                i = node_id(node.get("id"))
                if i not in UAVS:
                    continue
                at = number(node.get("source_sim_time_s", row.get("source_sim_time_s")))
                sample_host = number(node.get("sample_monotonic_ns"))
                source_clock = number(row.get("source_sim_time_s"))
                point, velocity = node.get("position_m"), node.get("velocity_enu_mps")
                quality = ("stale_or_fault" if row.get("fault") or node.get("stale", True)
                           else "unsupported_frame" if row.get("coordinate_frame") != "ENU"
                           else "observed")
                vectors_valid = (isinstance(point, list) and isinstance(velocity, list)
                    and len(point) == len(velocity) == 3
                    and all(number(value) is not None for value in point+velocity))
                if at is None or not vectors_valid:
                    quality = "missing_pose_or_clock"
                elif sample_host is None:
                    quality = "missing_sample_clock"
                elif not 0 <= host-sample_host <= .5*NS:
                    quality = "stale_or_future_sample_clock"
                elif source_clock is None or at-source_clock > .1:
                    quality = "inconsistent_source_clock"
                key = (row.get("session_id"), at, quality)
                if previous.get(i) == key:
                    continue
                previous[i] = key
                result.append(dict(uav_id=i, session_id=row.get("session_id"), source_sim_time_s=at,
                    host_monotonic_ns=host, sample_host_ns=sample_host,
                    quality=quality, x_m=point[0] if quality == "observed" else None,
                    y_m=point[1] if quality == "observed" else None, z_m=point[2] if quality == "observed" else None,
                    speed_mps=math.sqrt(sum(v*v for v in velocity)) if quality == "observed" else None,
                    sample_age_ms=number(node.get("sample_age_ms")), altitude_datum="world_ENU_z"))
    return result


def replay_dwell(completion, poses, config, *, first_goal_admission_ns=None):
    """Independent observed-pose check, never fills a missing interval."""
    i, target = node_id(completion.get("uav_id")), RELAY.target(completion)
    finish = number(completion.get("source_sim_time_s"))
    hold = number(completion.get("dwell_s", config.get("hold_s", 2)))
    base = dict(uav_id=i, goal_id=completion.get("goal_id"), status="unverifiable", dwell_s=hold,
                completed_host_ns=stamp(completion), completed_source_sim_time_s=finish)
    if target is None or finish is None or hold is None or hold <= 0:
        return base
    xy, z, speed = (float(config.get(k, default)) for k, default in
                    [("xy_tolerance_m", 3), ("z_tolerance_m", 1.5), ("hold_speed_mps", 1)])
    candidates = [row for row in poses if row["uav_id"] == i and row["source_sim_time_s"] is not None
                  and finish-hold-.5 <= row["source_sim_time_s"] <= finish]
    started, last, session, sample_count = None, None, None, 0
    max_gap, max_speed, max_xy, max_z = 0., 0., 0., 0.
    for pose in candidates:
        at = pose["source_sim_time_s"]
        gap = at-last if last is not None else None
        if pose["quality"] != "observed" or (last is not None and (gap <= 0 or gap > .5 or pose["session_id"] != session)):
            started, sample_count = None, 0
        last, session = at, pose["session_id"]
        if pose["quality"] != "observed":
            continue
        error_xy = math.hypot(pose["x_m"]-target[0], pose["y_m"]-target[1])
        error_z = abs(pose["z_m"]-target[2])
        if (error_xy > xy or error_z > z or pose["speed_mps"] > speed
                or (first_goal_admission_ns is not None and
                    (pose.get("sample_host_ns") is None or pose["sample_host_ns"] < first_goal_admission_ns))):
            started, sample_count = None, 0
            continue
        if started is None:
            started = at
            max_gap, max_speed, max_xy, max_z = 0., 0., 0., 0.
        sample_count += 1
        max_gap = max(max_gap, gap or 0)
        max_speed, max_xy, max_z = max(max_speed, pose["speed_mps"]), max(max_xy, error_xy), max(max_z, error_z)
    span = finish-started if started is not None and last is not None and abs(last-finish) <= .05 else None
    return dict(base, status="verified" if span is not None and span+1e-8 >= hold else "not_verified",
                observed_span_s=span, samples=sample_count, max_gap_s=max_gap,
                max_speed_mps=max_speed, max_xy_error_m=max_xy, max_z_error_m=max_z,
                first_goal_admission_host_ns=first_goal_admission_ns)


def lease_conflicts(events):
    rows = [r for r in events if stage(r) == "grant_issued"
            and number(r.get("valid_from_ns")) is not None and number(r.get("valid_until_ns")) is not None]
    active, conflicts = [], []
    for row in sorted(rows, key=lambda r:r["valid_from_ns"]):
        active = [r for r in active if r["valid_until_ns"] > row["valid_from_ns"]]
        for old in active:
            if old.get("leader_id") != row.get("leader_id") or old.get("epoch") != row.get("epoch"):
                conflicts.append(dict(old_sequence=old.get("grant_sequence"), new_sequence=row.get("grant_sequence"),
                    overlap_ns=min(old["valid_until_ns"], row["valid_until_ns"])-row["valid_from_ns"]))
        active.append(row)
    return conflicts


def performance(run, start, end, errors):
    """Observe host/source clocks and recorded process samples within the window."""
    clocks, windows, resources = [], [], []
    if start is None or end is None:
        return dict(status="missing_measurement_window"), clocks, windows, resources
    path = run/"logs/node_state.jsonl"
    if path.is_file():
        with path.open() as stream:
            for index, line in enumerate(stream, 1):
                try:
                    row = json.loads(line)
                except ValueError:
                    # read_poses also reports the exact source error; don't invent clocks.
                    continue
                host, model = number(row.get("published_monotonic_ns")), number(row.get("source_sim_time_s"))
                if host is None or model is None or not start <= host <= end:
                    continue
                clocks.append(dict(host_monotonic_ns=host, source_sim_time_s=model,
                    session_id=row.get("session_id"), fresh_UAVs=sum(node_id(n.get("id")) in UAVS
                        and not n.get("stale", True) for n in row.get("nodes", []))))
    previous, anchor, segments, gap = None, None, 0, []
    for row in clocks:
        if (previous is None or row["session_id"] != previous["session_id"]
                or row["source_sim_time_s"] < previous["source_sim_time_s"]
                or row["host_monotonic_ns"] <= previous["host_monotonic_ns"]):
            segments += 1
            anchor = row
        else:
            gap.append((row["host_monotonic_ns"]-previous["host_monotonic_ns"])/NS)
            elapsed = (row["host_monotonic_ns"]-anchor["host_monotonic_ns"])/NS
            if elapsed >= 1:
                windows.append(dict(host_monotonic_ns=row["host_monotonic_ns"],
                    source_sim_time_s=row["source_sim_time_s"], wall_duration_s=elapsed,
                    RTF=(row["source_sim_time_s"]-anchor["source_sim_time_s"])/elapsed))
                anchor = row
        previous = row
    grouped = defaultdict(list)
    for row in RELAY.read_jsonl(run/"logs/runtime_resources.jsonl", errors):
        host = number(row.get("monotonic_ns"))
        if host is not None and start <= host <= end:
            grouped[(host, row.get("component"))].append(row)
    for (at, component), samples in sorted(grouped.items(), key=lambda item:item[0][0]):
        cpu, rss = ([number(r.get(field)) for r in samples] for field in ("cpu_percent_one_core", "rss_bytes"))
        resources.append(dict(host_monotonic_ns=at, component=component, process_count=len(samples),
            sum_cpu_percent_one_core=sum(cpu) if all(v is not None for v in cpu) else None,
            sum_process_RSS_bytes=sum(rss) if all(v is not None for v in rss) else None))
    lag = [number(r.get("value")) for r in RELAY.read_csv(run/"logs/native_radio_events.csv")
           if r.get("event") == "realtime_lag" and stamp(r) is not None and start <= stamp(r) <= end]
    model = {name:dict(CPU_percent_one_core=RELAY.distribution([r["sum_cpu_percent_one_core"] for r in resources
                if r["component"] == name and r["sum_cpu_percent_one_core"] is not None]),
            process_RSS_bytes=RELAY.distribution([r["sum_process_RSS_bytes"] for r in resources
                if r["component"] == name and r["sum_process_RSS_bytes"] is not None]))
             for name in sorted({str(r["component"]) for r in resources})}
    integral = ((clocks[-1]["source_sim_time_s"]-clocks[0]["source_sim_time_s"])/
                ((clocks[-1]["host_monotonic_ns"]-clocks[0]["host_monotonic_ns"])/NS)
                if len(clocks) > 1 and segments == 1 else None)
    return dict(status="observed" if clocks else "not_recorded", integral_RTF=integral,
        approximately_1s_RTF=RELAY.distribution([r["RTF"] for r in windows]),
        RTF_window_wall_duration_s=RELAY.distribution([r["wall_duration_s"] for r in windows]),
        tracker_publication_gap_s=RELAY.distribution(gap), clock_segments=segments,
        scheduler_lag_ms=RELAY.distribution([v for v in lag if v is not None]), resources=model,
        resource_scope="recorded components/process RSS only, not all host helpers or tmpfs memory",
        GPU_utilization=None, GPU_status="not_recorded_by_this_exporter; do not infer zero load",
        radio_computation_time=None, radio_computation_status="not_inferred_from_packet_count"), clocks, windows, resources


def mission_progress(events, poses, commands, scenario, config, start, end, trigger):
    routes = scenario.get("routes", {})
    command_index = {r["command_id"]:r for r in commands}
    checks, completions, seen = [], [], set()
    for row in events:
        if (stage(row) != "action_completed" or stamp(row) is None or start is None or end is None
                or not start <= stamp(row) <= end):
            continue
        i = node_id(row.get("uav_id"))
        waypoint_index = row.get("waypoint_index")
        match = re.fullmatch(r"waypoint(\d+):uav(\d+)", str(row.get("goal_id", "")))
        if waypoint_index is None and match is not None and int(match[2]) == i:
            waypoint_index = int(match[1])
        key = i, waypoint_index
        if key in seen:
            continue
        # Takeoff/vertical goals are not urban inspection points.
        if waypoint_index is None:
            continue
        seen.add(key)
        command = command_index.get(str(row.get("admitted_command_id", row.get("command_id"))))
        goal_commands = [c for c in commands if c.get("goal_id") == row.get("goal_id") and c["uav_id"] == i
            and admitted(c) and c.get("epoch") == row.get("admitted_epoch", row.get("epoch"))
            and c.get("target_position_m") == RELAY.target(row) and c["admitted_ns"] <= stamp(row)]
        first_admission = min((c["admitted_ns"] for c in goal_commands), default=None)
        check = replay_dwell(row, poses, config, first_goal_admission_ns=first_admission)
        chain = (command is not None and admitted(command) and command["target_position_m"] == RELAY.target(row)
                 and command["admitted_ns"] <= stamp(row) and command.get("goal_id") == row.get("goal_id")
                 and command["uav_id"] == key[0] and command["epoch"] == row.get("admitted_epoch", row.get("epoch"))
                 and command["leader_id"] == node_id(row.get("leader_id"))
                 and command.get("lease_valid_until_ns") is not None and stamp(row) < command["lease_valid_until_ns"])
        route = routes.get(str(i), [])
        membership = (isinstance(waypoint_index, int) and not isinstance(waypoint_index, bool)
            and 0 <= waypoint_index < len(route) and RELAY.target(row) is not None
            and math.dist(tuple(route[waypoint_index]), RELAY.target(row)) <= 1e-7)
        maneuver_poses = [p for p in poses if p["uav_id"] == i and p["quality"] == "observed"
            and first_admission is not None and p.get("sample_host_ns") is not None
            and first_admission <= p["sample_host_ns"] <= stamp(row)]
        first_pose = maneuver_poses[0] if maneuver_poses else None
        displacement = max((math.hypot(p["x_m"]-first_pose["x_m"], p["y_m"]-first_pose["y_m"])
                            for p in maneuver_poses), default=None) if first_pose else None
        check.update(command_id=command["command_id"] if command else None, command_lineage_valid=bool(chain),
                     configured_waypoint_valid=membership, max_horizontal_displacement_m=displacement,
                     horizontal_movement_ge5m=displacement >= 5 if displacement is not None else None,
                     waypoint_index=waypoint_index, waypoint_id=row.get("waypoint_id"))
        checks.append(check)
        if check["status"] == "verified" and chain and membership:
            completions.append(row)
    progress, physical_resume = {}, {}
    for i in UAVS:
        done = [row for row in completions if node_id(row.get("uav_id")) == i]
        series = [r for r in poses if r["uav_id"] == i]
        distance, max_displacement, first, previous = 0., 0., None, None
        for row in series:
            if row["quality"] != "observed":
                previous = None
                continue
            point = row["x_m"], row["y_m"], row["z_m"]
            first = first or point
            max_displacement = max(max_displacement, math.hypot(point[0]-first[0], point[1]-first[1]))
            if previous is not None and previous["session_id"] == row["session_id"] and 0 < row["source_sim_time_s"]-previous["source_sim_time_s"] <= .5:
                distance += math.hypot(point[0]-previous["x_m"], point[1]-previous["y_m"])
            previous = row
        planned = len(routes[str(i)]) if str(i) in routes else None
        progress[str(i)] = dict(planned_waypoints=planned, completed_waypoints=len(done),
            completed_fraction=len(done)/planned if planned else None,
            route_finished=len(done) >= planned if planned is not None else None,
            observed_horizontal_path_m=distance if first is not None else None,
            max_horizontal_displacement_m=max_displacement if first is not None else None,
            pose_quality=dict(Counter(r["quality"] for r in series)))
        eligible = []
        if trigger is not None:
            for row in done:
                matching = [c for c in commands if c.get("goal_id") == row.get("goal_id") and c["uav_id"] == i
                    and admitted(c) and c.get("created_ns") is not None
                    and trigger <= c["created_ns"] <= c["admitted_ns"] <= stamp(row)]
                if stamp(row) >= trigger and matching:
                    eligible.append((row, min(matching, key=lambda r:r["admitted_ns"])))
        selected = min(eligible, key=lambda pair:stamp(pair[0])) if eligible else None
        physical_resume[str(i)] = dict(completed_host_ns=stamp(selected[0]) if selected else None,
            time_s=(stamp(selected[0])-trigger)/NS if selected else None,
            goal_id=selected[0].get("goal_id") if selected else None,
            first_postevent_CID=selected[1]["command_id"] if selected else None,
            goal_created_ns=number(selected[0].get("goal_created_host_monotonic_ns")) if selected else None,
            interpretation="observed post-event progress; refresh alone does not establish causation or recovery after HOLD")
    return progress, physical_resume, checks


def analyze(run_dir, output_dir, *, scenario_path=None, plots=False, deadline_ms=250., after_return_s=5.):
    run, out = Path(run_dir).resolve(), Path(output_dir).resolve()
    if out == run or run in out.parents:
        raise ValueError("analysis output must be outside immutable raw run")
    out.mkdir(parents=True, exist_ok=True)
    errors = []
    common = RELAY.analyze_run(run, out/"relay_audit", deadline_ms=deadline_ms, plots=False, live_export=False)
    meta = RELAY.read_json(run/"metrics/relay_experiment_summary.json")
    config = common["metadata"]["config"]
    events = load_events(run, errors)
    expected_run = common["run_id"]
    foreign = [r for r in events if r.get("run_id") not in (None, expected_run)]
    if foreign:
        errors.append(f"{len(foreign)} foreign-run coordination rows excluded")
    events = [r for r in events if r.get("run_id") in (None, expected_run)]
    window = common["observation_window"]
    start, end, complete = window["start_host_monotonic_ns"], window["end_host_monotonic_ns"], window["complete"]
    geometry = load_geometry(run/"logs/geometry_links.csv", errors)
    transitions = geometry_transitions(geometry, start, end)
    initial = int(config.get("initial_master", 1))
    pair = f"0-{initial}"
    trigger_row = next((r for r in transitions if r["pair"] == pair and r["transition"] == "NLOS_entry"), None)
    trigger = stamp(trigger_row) if trigger_row else None
    returned = next((r for r in transitions if trigger is not None and r["pair"] == pair
                     and r["transition"] == "LOS_return" and stamp(r) > trigger), None)
    return_ns = stamp(returned) if returned else None
    commands = RELAY.read_csv(out/"relay_audit/metrics/relay_commands.csv")
    raw_sends = {str(r["command_id"]):r for r in events if stage(r) == "gcs_send" and r.get("command_id") is not None}
    for row in commands:
        for key in ("created_ns", "sent_ns", "admitted_ns", "lease_valid_until_ns", "lease_valid_from_ns", "goal_created_ns", "latency_ms"):
            row[key] = number(row.get(key))
        for key in ("uav_id", "leader_id", "epoch"):
            row[key] = int(row[key]) if row.get(key) else None
        for key in ("timely", "application_chain_complete", "policy_allowed"):
            row[key] = boolean(row.get(key))
        # csv tuple repr is not JSON; recover target from its originating raw event.
        sent = raw_sends.get(row["command_id"], {})
        row["target_position_m"] = RELAY.target(sent)
    grants = [r for r in events if stage(r) == "grant_issued"]
    initial_epoch = next((r.get("epoch") for r in grants if r.get("leader_id") == initial), 1)
    selections = [r for r in grants if r.get("reason") == "selection" and start is not None and end is not None and stamp(r) is not None
                  and start <= stamp(r) <= end and (r.get("leader_id") != initial or r.get("epoch") != initial_epoch)]
    selected_after = [r for r in selections if trigger is not None and stamp(r) >= trigger]
    valid_geometry = bool(geometry) and not any("geometry_links.csv" in e for e in errors)
    valid_recording = valid_geometry and not any("malformed" in e for e in errors+common["data_quality_errors"])
    uart = uart_endpoints(commands, trigger, end, complete, valid_recording, initial_leader=initial, initial_epoch=initial_epoch)
    handover = uart_endpoints(commands, trigger, end, complete, valid_recording, initial_leader=initial,
        initial_epoch=initial_epoch, handover=True, handover_observed=bool(selected_after))
    poses = read_poses(run/"logs/node_state.jsonl", start, end, errors)
    perf, clocks, clock_windows, resources = performance(run, start, end, errors)
    snapshot = scenario_path or (run/"logs/urban_scenario.json" if (run/"logs/urban_scenario.json").is_file()
                                 else run/"configuration/urban_scenario.json")
    scenario = RELAY.read_json(Path(snapshot))
    if not scenario:
        errors.append("scenario snapshot missing; planned-waypoint denominator unavailable")
    progress, resume, checks = mission_progress(events, poses, commands, scenario, config, start, end, trigger)
    index = {r["command_id"]:r for r in commands}
    offers = RELAY.read_csv(run/"metrics/relay_control_refresh.csv")
    opportunity = []
    for offer in offers:
        at = stamp(offer)
        if at is None or start is None or end is None or not start <= at <= end:
            continue
        command = index.get(str(offer.get("command_id")))
        scheduled = number(offer.get("scheduled_host_monotonic_ns"))
        opportunity.append(dict(request_id=offer.get("request_id"), uav_id=node_id(offer.get("uav_id")),
            offered_host_ns=at, scheduled_host_ns=scheduled, goal_id=offer.get("goal_id"),
            command_id=command["command_id"] if command else None, timely=bool(command and command["timely"]),
            admitted=bool(command and admitted(command)),
            opportunity_to_UART_ms=(command["admitted_ns"]-scheduled)/1e6
                if command and command["admitted_ns"] is not None and scheduled is not None else None))
    after = [r for r in opportunity if return_ns is not None and return_ns <= r["offered_host_ns"] < return_ns+after_return_s*NS]
    postreturn_complete = return_ns is not None and complete and return_ns+after_return_s*NS <= end
    postreturn = dict(status="observed" if postreturn_complete else "not_applicable_no_LOS_return" if return_ns is None else "partial_window",
        window_s=after_return_s, offered=len(after) if return_ns is not None else None,
        admitted=sum(r["admitted"] for r in after) if return_ns is not None else None,
        admitted_per_offered=sum(r["admitted"] for r in after)/len(after) if after and postreturn_complete else None)
    conflicts = lease_conflicts(events)
    activation_rows = [r for r in events if stage(r) == "leader_activated" and return_ns is not None
        and return_ns <= (stamp(r) or -1) <= end and r.get("leader_id") != initial]
    activation_scopes = sorted({(r.get("leader_id"), r.get("epoch")) for r in activation_rows})
    result = dict(analysis_version=1, run_id=expected_run, raw_run_dir=str(run), recorded_status=common["run_status"],
        recorded_error=common["run_error"], policy=common["policy"], pilot=common["pilot"], observation_window=window,
        trigger=dict(status="observed" if trigger_row else "not_observed", pair=pair, event=trigger_row,
            definition="first observed LOS→NLOS CP↔initial-relay transition after measurement start; detection resolution is bracketed"),
        geometry=dict(samples=len(geometry), pairs=sorted({r["pair"] for r in geometry}), transitions=len(transitions),
            LOS_return_host_ns=return_ns, selected_radio_modes=sorted({r.get("radio_state", "") for r in geometry}),
            parameter_sets=sorted({r.get("parameter_set", "") for r in geometry})),
        rates=common["rates"], uart_after_NLOS=uart, handover_uart=handover, performance=perf,
        uart_endpoint_label="first post-NLOS update latency; do not call recovery unless delivery impairment is independently established",
        mission_progress=progress, inspection_resume=resume,
        physical_checks=dict(total=len(checks), verified=sum(r["status"] == "verified" and r["command_lineage_valid"]
                             and r["configured_waypoint_valid"] for r in checks)),
        post_LOS_return=postreturn, authority=dict(admission_audit=common["admission_audit"],
            total_UART_admissions=common["admission_total"], unverifiable_admissions=common["unverifiable_admissions"],
            overlapping_issued_grants=conflicts, selections=len(selections), selection_events=selections,
            role_scopes_activated_after_LOS_return=len(activation_scopes) if return_ns is not None else None,
            role_scopes_activated_after_LOS_return_ids=activation_scopes if return_ns is not None else None,
            endpoint_activation_rows_after_LOS_return=len(activation_rows) if return_ns is not None else None),
        provenance=dict(scenario_path=str(snapshot), scenario_sha256=hashlib.sha256(Path(snapshot).read_bytes()).hexdigest() if Path(snapshot).is_file() else None,
            script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            common_audit_script_sha256=hashlib.sha256(Path(RELAY.__file__).read_bytes()).hexdigest(),
            command_audit="reuses existing relay admission/issued-authority/payload lineage audit; urban geometry and pose dwell independently replayed",
            physical_evidence="Gazebo odometry; not command ACK or network receipt; stale/missing intervals are not filled"),
        data_quality_errors=errors+common["data_quality_errors"],
        missing_measurements=[name for name, ok in [("geometry",bool(geometry)), ("poses",bool(poses)), ("scenario",bool(scenario))] if not ok])
    for row in opportunity:
        row["run_id"] = expected_run
    write_rows(out/"metrics/urban_geometry_transitions.csv", transitions,
        ["pair", "transition", "host_monotonic_ns", "ns3_time_s", "previous_observed_host_ns", "transition_bracket_s", "geom_los", "radio_state", "blocker_ids", "pathloss_db", "parameter_set"])
    write_rows(out/"metrics/urban_trajectory.csv", poses,
        ["uav_id", "session_id", "source_sim_time_s", "host_monotonic_ns", "sample_host_ns", "quality", "x_m", "y_m", "z_m", "speed_mps", "sample_age_ms", "altitude_datum"])
    write_rows(out/"metrics/urban_physical_checks.csv", checks,
        ["uav_id", "goal_id", "status", "completed_host_ns", "completed_source_sim_time_s", "dwell_s", "observed_span_s", "samples", "max_gap_s", "max_speed_mps", "max_xy_error_m", "max_z_error_m", "command_id", "command_lineage_valid", "configured_waypoint_valid", "first_goal_admission_host_ns", "max_horizontal_displacement_m", "horizontal_movement_ge5m", "waypoint_index", "waypoint_id"])
    write_rows(out/"metrics/urban_opportunities.csv", opportunity,
        ["run_id", "request_id", "uav_id", "offered_host_ns", "scheduled_host_ns", "goal_id", "command_id", "timely", "admitted", "opportunity_to_UART_ms"])
    write_rows(out/"metrics/urban_clocks.csv", clocks, ["host_monotonic_ns", "source_sim_time_s", "session_id", "fresh_UAVs"])
    write_rows(out/"metrics/urban_RTF.csv", clock_windows, ["host_monotonic_ns", "source_sim_time_s", "wall_duration_s", "RTF"])
    write_rows(out/"metrics/urban_resources.csv", resources, ["host_monotonic_ns", "component", "process_count", "sum_cpu_percent_one_core", "sum_process_RSS_bytes"])
    RELAY.write_json(out/"metrics/urban_analysis.json", result)
    if plots:
        plot_run(out, result, poses, scenario, geometry, events)
    return result


def plot_run(out, result, poses, scenario, geometry, events):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle
    fig, axes = plt.subplots(2, 2, figsize=(12, 8))
    origin = result["observation_window"]["start_host_monotonic_ns"]
    end = result["observation_window"]["end_host_monotonic_ns"]
    for building in scenario.get("buildings", []):
        low, high = building.get("min"), building.get("max")
        if low is not None and high is not None:
            axes[0, 0].add_patch(Rectangle(low[:2], high[0]-low[0], high[1]-low[1], color="grey", alpha=.3))
            axes[0, 0].text(low[0], low[1], building.get("id", ""))
    for i in UAVS:
        series = [r for r in poses if r["uav_id"] == i]
        xs, ys, ts, zs = [], [], [], []
        previous = None
        for row in series:
            gap = (previous is not None and (row["session_id"] != previous["session_id"] or
                   row["source_sim_time_s"] is None or previous["source_sim_time_s"] is None or
                   not 0 < row["source_sim_time_s"]-previous["source_sim_time_s"] <= .5))
            if gap or row["quality"] != "observed":
                xs.append(float("nan")); ys.append(float("nan")); ts.append(float("nan")); zs.append(float("nan"))
            if row["quality"] == "observed":
                xs.append(row["x_m"]); ys.append(row["y_m"])
                ts.append((row["host_monotonic_ns"]-origin)/NS); zs.append(row["z_m"])
            previous = row
        axes[0, 0].plot(xs, ys, label=f"UAV{i}", color=f"C{i-1}")
        axes[0, 1].plot(ts, zs, label=f"UAV{i} actual", color=f"C{i-1}")
        requested = [r for r in events if stage(r) == "goal_created" and node_id(r.get("uav_id")) == i
                     and RELAY.target(r) is not None and origin is not None and end is not None
                     and stamp(r) is not None and origin <= stamp(r) <= end]
        if requested:
            axes[0, 1].step([(stamp(r)-origin)/NS for r in requested],
                            [RELAY.target(r)[2] for r in requested],where="post",linestyle="--",
                            marker=".",markersize=3,color=f"C{i-1}",alpha=.7,label=f"UAV{i} requested")
    axes[0, 0].set(xlabel="world ENU X (m)", ylabel="world ENU Y (m)", title="Actual trajectories; gaps retained", aspect="equal")
    axes[0, 0].legend(fontsize=8)
    axes[0, 1].set(xlabel="host seconds since measurement", ylabel="world ENU Z (m)",
                  title="Actual / GCS-requested altitude (intent, not UART proof)")
    axes[0, 1].legend(fontsize=6,ncol=2)
    for row in geometry:
        if (row["pair"] == result["trigger"]["pair"] and origin is not None and end is not None
                and origin <= stamp(row) <= end):
            axes[1, 0].scatter((stamp(row)-origin)/NS, 1 if row["geom_los"] else 0, s=2, color="C0")
            axes[1, 1].scatter((stamp(row)-origin)/NS, row["pathloss_db"], s=2, color="C0")
    for row in events:
        if origin is None or end is None or stamp(row) is None or not origin <= stamp(row) <= end:
            continue
        at = (stamp(row)-origin)/NS
        if stage(row) == "grant_issued" and row.get("reason") == "selection":
            for ax in axes[1]:
                ax.axvline(at, linestyle="--", color="C3", alpha=.7)
    axes[1, 0].set(xlabel="host seconds since measurement", yticks=[0, 1], yticklabels=["NLOS", "LOS"], title="CP↔initial relay geometry; dashed: selection")
    axes[1, 1].set(xlabel="host seconds since measurement", ylabel="pathloss (dB)", title="Model loss of CP↔initial relay")
    if origin is not None and end is not None:
        for ax in (axes[0, 1], axes[1, 0], axes[1, 1]):
            ax.set_xlim(0, (end-origin)/NS)
    for ax in axes.flat:
        ax.grid(alpha=.2)
    fig.suptitle(result["run_id"])
    fig.tight_layout()
    (out/"plots").mkdir(exist_ok=True)
    for extension in ("png", "svg", "pdf"):
        fig.savefig(out/f"plots/urban_observed.{extension}", dpi=160)
    plt.close(fig)


def study_plan(repeats=10, order_seed=20261028, rng_seed=1):
    """Design only: callers must freeze source/config/pilots before execution."""
    if repeats < 1 or rng_seed < 1:
        raise ValueError("positive repeat count and RNG seed required")
    generator, rows = random.Random(order_seed), []
    blocks = list(range(1, repeats+1))
    generator.shuffle(blocks)
    for block in blocks:
        cases = [(policy, mode) for policy in ("fixed", "failover") for mode in ("geometry", "los_only")]
        generator.shuffle(cases)
        for policy, mode in cases:
            rows.append(dict(order=len(rows)+1, block_id=f"seed{rng_seed}-r{block:02d}", rng_seed=rng_seed,
                rng_run=block, policy=policy, radio_mode=mode, order_seed=order_seed,
                status="planned_not_frozen_not_run"))
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("run_dir", type=Path)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--scenario", type=Path)
    run.add_argument("--plots", action="store_true")
    run.add_argument("--deadline-ms", type=float, default=250)
    run.add_argument("--after-return-s", type=float, default=5)
    plan = sub.add_parser("plan")
    plan.add_argument("--output", type=Path, required=True)
    plan.add_argument("--repeats", type=int, choices=(5, 10), default=10)
    plan.add_argument("--order-seed", type=int, default=20261028)
    plan.add_argument("--rng-seed", type=int, default=1)
    args = parser.parse_args()
    if args.command == "plan":
        RELAY.write_json(args.output, dict(status="planned_not_frozen_not_run", rows=study_plan(args.repeats, args.order_seed, args.rng_seed)))
    else:
        if args.deadline_ms <= 0 or args.after_return_s <= 0:
            parser.error("positive deadline and return window required")
        result = analyze(args.run_dir, args.output, scenario_path=args.scenario, plots=args.plots,
                         deadline_ms=args.deadline_ms, after_return_s=args.after_return_s)
        print(json.dumps(dict(run_id=result["run_id"], output=str(args.output), missing=result["missing_measurements"],
                             errors=result["data_quality_errors"])))


if __name__ == "__main__":
    main()
