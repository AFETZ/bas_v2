#!/usr/bin/env python3
"""Independent stdlib raw replay; imports no BAS runtime/analyzer modules.

Checks the flight/frame qualification pilot, not urban radio-model validity.
"""
import argparse
import bisect
import collections
import csv
import hashlib
import json
import math
from pathlib import Path


def rows(path):
    with path.open() as stream:
        for line in stream:
            yield json.loads(line)


def percentile(values, probability):
    if not values:
        return None
    values = sorted(values)
    index = (len(values) - 1) * probability
    lo, hi = math.floor(index), math.ceil(index)
    return values[lo] + (values[hi] - values[lo]) * (index - lo)


def distribution(values):
    return dict(n=len(values), mean=sum(values)/len(values) if values else None,
                p50=percentile(values,.5), p95=percentile(values,.95),
                max=max(values) if values else None)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("run", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run = args.run.resolve()
    logs = run / "logs"
    gcs = list(rows(logs / "coordination_gcs.jsonl"))
    scenario = json.loads((logs / "urban_scenario.json").read_text())
    phases = [x for x in gcs if x["event"] == "phase_requested"]
    start = next(x for x in phases if x["phase"] == "experiment_normal")
    end = next(x for x in gcs if x["event"] == "urban_measurement_window_closed")
    begin_ns, end_ns = start["host_monotonic_ns"], end["host_monotonic_ns"]
    begin_s, end_s = start["source_sim_time_s"], end["source_sim_time_s"]
    in_window = lambda x: begin_ns <= x["host_monotonic_ns"] < end_ns
    grants = {(x["epoch"],x["grant_sequence"]): x for x in gcs if x["event"] == "grant_issued"}
    sent = {x["command_id"]: x for x in gcs if x["event"] == "gcs_send"}
    refreshes = [x for x in gcs if x["event"] == "control_refresh_created" and in_window(x)]
    offers = [x for x in gcs if x["event"] == "control_refresh_offered" and in_window(x)]
    all_admissions, anomalies, duplicate = {}, [], []
    stop_failures = []
    for uav in range(1,6):
        for row in rows(logs / f"coordination_uav{uav}.jsonl"):
            if row["event"] == "service_failed":
                stop_failures.append(dict(uav_id=uav,**{k:row[k] for k in ("host_monotonic_ns","reason")}))
            if row["event"] != "uart_admitted":
                continue
            key = (uav,row["command_id"])
            if key in all_admissions:
                duplicate.append(key)
            all_admissions[key] = row
            origin = sent.get(row["command_id"])
            grant = grants.get((row["epoch"],row["grant_sequence"]))
            if origin is None or grant is None:
                anomalies.append([key,"missing_issued_lineage"])
                continue
            for field in ("epoch","leader_id","grant_sequence","valid_from_ns","valid_until_ns",
                          "destination_id","payload_sha256","command_created_host_monotonic_ns"):
                if origin[field] != row[field]:
                    anomalies.append([key,f"command_{field}_mismatch"])
            for field in ("epoch","leader_id","grant_sequence","valid_from_ns","valid_until_ns"):
                if grant[field] != row[field]:
                    anomalies.append([key,f"grant_{field}_mismatch"])
            if row["destination_id"] != uav:
                anomalies.append([key,"wrong_destination"])
            write_start, write_end = row["uart_write_started_host_monotonic_ns"], row["uart_write_end_host_monotonic_ns"]
            checked = row["admission_checked_host_monotonic_ns"]
            if not grant["valid_from_ns"] <= write_start <= write_end <= checked < grant["valid_until_ns"]:
                anomalies.append([key,"invalid_authority_at_write"])
            if not 0 <= checked-origin["command_created_host_monotonic_ns"] <= 250_000_000:
                anomalies.append([key,"invalid_command_age"])
    conflicts = []
    grant_list = sorted(grants.values(),key=lambda x:x["valid_from_ns"])
    for i,a in enumerate(grant_list):
        for b in grant_list[i+1:]:
            if b["valid_from_ns"] >= a["valid_until_ns"]:
                break
            if (a["epoch"],a["leader_id"]) != (b["epoch"],b["leader_id"]):
                conflicts.append([a["epoch"],a["grant_sequence"],b["epoch"],b["grant_sequence"]])
    latency = []
    for command in refreshes:
        admission = all_admissions.get((command["destination_id"],command["command_id"]))
        if admission:
            latency.append((admission["uart_write_end_host_monotonic_ns"]-
                            command["command_created_host_monotonic_ns"])/1e6)

    # Genuine producer poses are deduplicated by source timestamp per UAV.
    poses = {i:{} for i in range(1,6)}
    conflicting_poses = []
    for snapshot in rows(logs / "node_state.jsonl"):
        for node in snapshot.get("nodes",[]):
            if not node["id"].startswith("uav") or node.get("stale"):
                continue
            uav = int(node["id"][3:])
            t = node.get("source_sim_time_s")
            if t is None:
                continue
            pose = dict(source_s=t,ns=node["sample_monotonic_ns"],p=node["position_m"],
                        v=node["velocity_enu_mps"])
            if t in poses[uav] and poses[uav][t]["p"] != pose["p"]:
                conflicting_poses.append([uav,t])
            poses[uav].setdefault(t,pose)
    poses = {i:sorted(values.values(),key=lambda x:x["ns"]) for i,values in poses.items()}
    times = {i:[p["ns"] for p in values] for i,values in poses.items()}
    def nearest(i,ns):
        index = bisect.bisect_left(times[i],ns)
        choices = [j for j in (index-1,index) if 0 <= j < len(times[i])]
        return min((poses[i][j] for j in choices),key=lambda x:abs(x["ns"]-ns))

    landing_start = next(x["host_monotonic_ns"] for x in phases if x["phase"] == "experiment_landing")
    lifecycle = {}
    for i in range(1,6):
        heartbeats = [x for x in gcs if x["event"] == "autopilot_heartbeat_received" and x["uav_id"] == i]
        armed = [x for x in heartbeats if x["base_mode"] & 128]
        disarmed_land = [x for x in heartbeats if x["host_monotonic_ns"] >= landing_start
                         and not x["base_mode"] & 128 and x["custom_mode"] == 9]
        airborne = [p for p in poses[i] if armed[0]["host_monotonic_ns"] <= p["ns"] < landing_start]
        land_modes = [x for x in rows(logs/f"coordination_uav{i}.jsonl")
                      if x["event"] == "autopilot_mode_observed" and x.get("mode") == "LAND"
                      and x["host_monotonic_ns"] >= landing_start]
        disarm_pose = nearest(i,disarmed_land[0]["producer_host_monotonic_ns"]) if disarmed_land else None
        lifecycle[str(i)] = dict(first_armed_heartbeat_ns=armed[0]["host_monotonic_ns"],
            armed_heartbeat_count=len(armed),land_mode_event_count=len(land_modes),
            first_disarmed_land_heartbeat_ns=disarmed_land[0]["host_monotonic_ns"] if disarmed_land else None,
            min_z_between_ARM_and_LAND_m=min(p["p"][2] for p in airborne),
            max_z_between_ARM_and_LAND_m=max(p["p"][2] for p in airborne),
            disarm_actual_position_m=disarm_pose["p"] if disarm_pose else None,
            disarm_actual_speed_mps=math.sqrt(sum(v*v for v in disarm_pose["v"])) if disarm_pose else None)

    residuals = {i:[] for i in range(1,6)}
    frame_errors, match_age = [], []
    frames = {x["uav_id"]:x for x in gcs if x["event"] == "urban_frame_anchored"}
    for row in gcs:
        if row["event"] != "urban_position_telemetry":
            continue
        i = row["uav_id"]
        world, anchor = frames[i]["world_anchor"], frames[i]["ned_anchor"]
        n,e,d = row["local_ned_m"]
        independent = [world[0]+e-anchor[1],world[1]+n-anchor[0],world[2]-d+anchor[2]]
        if max(abs(a-b) for a,b in zip(independent,row["telemetry_position_m"])) > 1e-9:
            frame_errors.append([i,row["producer_host_monotonic_ns"]])
        actual = nearest(i,row["producer_host_monotonic_ns"])
        dt = abs(actual["ns"]-row["producer_host_monotonic_ns"])/1e6
        match_age.append(dt)
        residuals[i].append([a-b for a,b in zip(independent,actual["p"])])

    # Replay actual target-region dwell before each recorded completion, without
    # trusting its online pass/displacement flag.
    physical = []
    for row in gcs:
        if row["event"] != "action_completed" or row.get("waypoint_index") is None:
            continue
        i, goal = row["uav_id"], row["goal_id"]
        cids = [x for x in refreshes if x["goal_id"] == goal]
        eligible = [all_admissions[(i,x["command_id"])] for x in cids if (i,x["command_id"]) in all_admissions]
        first = min(eligible,key=lambda x:x["uart_write_end_host_monotonic_ns"])
        before = nearest(i,first["uart_write_end_host_monotonic_ns"])
        actual = min(poses[i],key=lambda x:abs(x["source_s"]-row["source_sim_time_s"]))
        target = row["target_position_m"]
        after = [p for p in poses[i] if before["ns"] <= p["ns"] <= actual["ns"]]
        since, last, success, maximum_gap = None, None, False, 0.
        for p in after:
            speed = math.sqrt(sum(v*v for v in p["v"]))
            gap = 0 if last is None else p["source_s"]-last
            maximum_gap = max(maximum_gap,gap)
            inside = math.hypot(p["p"][0]-target[0],p["p"][1]-target[1]) <= 3 and abs(p["p"][2]-target[2]) <= 1.5 and speed <= 1
            if not inside or gap > .5:
                since = None
            if inside and since is None:
                since = p["source_s"]
            if since is not None and p["source_s"]-since >= 2-1e-9:
                success = True
            last = p["source_s"]
        delta = [a-b for a,b in zip(actual["p"],before["p"])]
        physical.append(dict(uav_id=i,goal_id=goal,waypoint_index=row["waypoint_index"],
            start_position=before["p"],end_position=actual["p"],target=target,
            delta=delta,horizontal_displacement_m=math.hypot(*delta[:2]),
            independent_dwell_pass=success,max_source_gap_s=maximum_gap,
            first_admitted_cid=first["command_id"],completion_cid=row["command_id"]))

    lag = []
    with (logs / "native_radio_events.csv").open() as stream:
        for row in csv.DictReader(stream):
            if row["event"] == "realtime_lag" and begin_ns <= int(row["wall_monotonic_ns"]) < end_ns:
                lag.append(float(row["value"]))
    geometry_counts = collections.Counter()
    with (logs / "geometry_links.csv").open() as stream:
        for row in csv.DictReader(stream):
            geometry_counts[(row["geom_los"],row["radio_state"])] += 1
    live = [p for p in poses[1] if begin_s <= p["source_s"] < end["measurement_end_source_sim_time_s"]]
    windows, j = [], 0
    while j < len(live):
        k = j+1
        while k < len(live) and live[k]["source_s"]-live[j]["source_s"] < 1:
            k += 1
        if k >= len(live):
            break
        windows.append((live[k]["source_s"]-live[j]["source_s"])/((live[k]["ns"]-live[j]["ns"])/1e9))
        j = k
    hashes = {}
    for path in [logs/"coordination_gcs.jsonl",logs/"node_state.jsonl",logs/"native_radio_events.csv",
                 logs/"geometry_links.csv",run/"environment.txt",logs/"urban_scenario.json",
                 *[logs/f"coordination_uav{i}.jsonl" for i in range(1,6)]]:
        hashes[str(path.relative_to(run))] = hashlib.sha256(path.read_bytes()).hexdigest()
    output = dict(run_id=run.name,raw_directory=str(run),source=(run/"environment.txt").read_text().splitlines()[:4],
        scope="independent raw replay of LOS-only five-UAV axes pilot, not building-NLOS validation",
        geometry_radio_mode=scenario["radio"]["mode"],
        input_sha256=hashes,review_script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        offers=len(offers),created_refreshes=len(refreshes),
        uart=dict(all_admissions=len(all_admissions),duplicate_cids=duplicate,anomalies=anomalies,
                  all_issued_grants=len(grants),conflicting_grants=conflicts,
                  target_refresh_admitted=len(latency),timely=sum(x<=250 for x in latency),
                  timely_offered=sum(x<=250 for x in latency)/len(offers),latency_ms=distribution(latency)),
        frame=dict(reconstructed_mapping_errors=frame_errors,matched_pose_age_ms=distribution(match_age),
            telemetry_estimate_minus_nearest_actual_m={str(i):dict(
                mean_signed=[sum(x[k] for x in values)/len(values) for k in range(3)],
                euclidean=distribution([math.sqrt(sum(v*v for v in x)) for x in values]),
                n=len(values)) for i,values in residuals.items()},
            alignment_limit="Nearest Gazebo producer-host timestamp to sidecar UART producer-host timestamp; residual includes estimator/delay error, not pure coordinate-transform error."),
        physical_goals=physical,lifecycle_raw_replay=lifecycle,conflicting_duplicate_poses=conflicting_poses,
        geometry_record_counts={f"geom_los={k[0]},radio={k[1]}":v for k,v in geometry_counts.items()},
        tested_axes="+X, -Y, +Z, -X; +Y not flown by this route",
        live=dict(observed_window_source_s=end_s-begin_s,observed_window_wall_s=(end_ns-begin_ns)/1e9,
            observed_rtf=(end_s-begin_s)/((end_ns-begin_ns)/1e9),uav1_window_samples=len(windows),
            rtf_approximately_1s_p5=percentile(windows,.05),rtf_approximately_1s_min=min(windows),
            native_lag_ms=distribution(lag)),
        service_failure_events=stop_failures,
        limitations=["No independent tlog/MAVLink payload decoding or full PCAP hop replay in this narrow check.",
            "Service failures are listed with raw timestamps: after-window watchdog teardown is not a measurement crash.",
            "Physical completion is checked against actual Gazebo poses; native UART admission is not assumed to prove physical execution."])
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(output,indent=2,allow_nan=False)+"\n")
    print(json.dumps(dict(output=str(args.output),uart=output["uart"],frame_errors=len(frame_errors),
        physical_goals=len(physical),physical_dwell_passes=sum(x["independent_dwell_pass"] for x in physical),
        live=output["live"]),indent=2))


if __name__ == "__main__":
    main()
