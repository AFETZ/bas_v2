#!/usr/bin/env python3
"""Replay pilot candidate freshness and geometry from immutable primary logs.

This does not invoke the production eligibility policy or start a simulator.
Probe receipt is represented by its observed log stamp, a few instructions after
the internal receipt assignment; decision-boundary microseconds are not claimed.
"""
import argparse
from bisect import bisect_right
import csv
import hashlib
import json
from pathlib import Path

NS = 1_000_000_000


def read_jsonl(path):
    with path.open() as stream:
        return [json.loads(line) for line in stream]


def write_csv(path, rows):
    fields = list(dict.fromkeys(k for r in rows for k in r))
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader(); writer.writerows(rows)


def eligible_at(probes, at, deadline_s=1):
    result = []
    for i in range(1,6):
        row = next((r for r in reversed(probes) if r["uav_id"] == i and r["host_monotonic_ns"] <= at), None)
        stamps = ({"log_receipt":row["host_monotonic_ns"], "probe_sent":row["probe_sent_host_monotonic_ns"],
                   "reply_sent":row["reply_sent_host_monotonic_ns"]} if row else {})
        ages = {key:(at-value)/NS for key,value in stamps.items()}
        direct = bool(row) and all(0 <= value <= deadline_s for value in ages.values())
        peers = {str(j):(at-row["peer_echo_ns"][str(j)])/NS
                 if row and str(j) in row.get("peer_echo_ns", {}) else None for j in range(1,6) if j != i}
        bad = [j for j,age in peers.items() if age is None or not 0 <= age <= deadline_s]
        result.append(dict(uav_id=i, direct_fresh=direct, peer_stale_or_missing=";".join(bad),
            candidate_reconstructed=direct and not bad,
            log_receipt_age_s=ages.get("log_receipt"), probe_sent_age_s=ages.get("probe_sent"),
            reply_sent_age_s=ages.get("reply_sent"), peer_ages_s=json.dumps(peers, sort_keys=True),
            observed_RTT_log_proxy_ms=(row["host_monotonic_ns"]-row["probe_sent_host_monotonic_ns"])/1e6 if row else None,
            reason="missing_or_stale_direct_probe" if not direct else "stale_or_missing_peer" if bad else "eligible"))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--analysis", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run, output = args.run.resolve(), args.output.resolve()
    if output == run or run in output.parents:
        parser.error("output must be outside immutable raw")
    output.mkdir(parents=True, exist_ok=True)
    summary = json.loads((run/"metrics/relay_experiment_summary.json").read_text())
    config = json.loads((run/"logs/urban_scenario.json").read_text())
    analysis = json.loads((args.analysis/"metrics/urban_analysis.json").read_text())
    start = summary["measurement_start_host_monotonic_ns"]
    end = start+summary["measurement_duration_wall_s"]*NS
    rows = sorted(read_jsonl(run/"logs/coordination_gcs.jsonl"), key=lambda r:r["host_monotonic_ns"])
    probes = [r for r in rows if r["event"] == "gcs_probe_echo_received"]
    with (run/"logs/geometry_links.csv").open() as stream:
        geometry = list(csv.DictReader(stream))
    previous, transitions = {}, []
    for row in geometry:
        pair = "-".join(str(i) for i in sorted((int(row["from_id"]),int(row["to_id"]))))
        at = int(row["wall_monotonic_ns"])
        los = row["geom_los"].lower() in ("1","true","los")
        old = previous.get(pair)
        if old is not None and los != old[1] and start <= at <= end:
            transitions.append(dict(kind="LOS_return" if los else "NLOS_entry", pair=pair,
                host_ns=at, from_measurement_s=(at-start)/NS, ns3_time_s=float(row["time_s"]),
                observation_bracket_s=(at-old[0])/NS, blockers=row["blocker_ids"], pathloss_db=float(row["pathloss_db"])))
        previous[pair] = (at,los)
    trigger = analysis["trigger"]["event"]
    trigger_ns = trigger["host_monotonic_ns"] if trigger else None
    selections = [r for r in rows if r["event"] == "grant_issued" and r.get("reason") == "selection" and start <= r["host_monotonic_ns"] <= end]
    detections = [r for r in rows if r["event"] == "link_failure_detected" and start <= r["host_monotonic_ns"] <= end]
    checkpoints = [("measurement_start",start)]
    if trigger_ns is not None:
        checkpoints.append(("primary_CP1_NLOS",trigger_ns))
    checkpoints.extend(("detection_"+str(i),r["host_monotonic_ns"]) for i,r in enumerate(detections))
    checkpoints.extend(("selection_"+str(i),r["host_monotonic_ns"]) for i,r in enumerate(selections))
    first_eligible = None
    if detections and selections:
        for probe in probes:
            at = probe["host_monotonic_ns"]
            if detections[0]["host_monotonic_ns"] <= at <= selections[0]["host_monotonic_ns"]:
                eligible = [r["uav_id"] for r in eligible_at(probes,at) if r["candidate_reconstructed"]]
                if eligible:
                    first_eligible = dict(host_ns=at,from_measurement_s=(at-start)/NS,candidates=eligible)
                    checkpoints.append(("first_reconstructed_eligible_after_detection",at))
                    break
    for r in transitions:
        if r["pair"] == "3-5" and r["kind"] == "NLOS_entry":
            checkpoints.extend([("CP3_UAV5_NLOS_"+str(round(r["from_measurement_s"],3)),r["host_ns"]),
                                ("CP3_UAV5_NLOS_plus_2s_"+str(round(r["from_measurement_s"],3)),r["host_ns"]+2*NS)])
    checkpoints.append(("measurement_end",end))
    eligibility = []
    for name, at in checkpoints:
        for row in eligible_at(probes,at):
            eligibility.append(dict(checkpoint=name, host_ns=at, from_measurement_s=(at-start)/NS, **row))
    timeline = list(transitions)
    relevant = {"link_failure_detected", "grant_issued", "urban_measurement_window_closed",
                "urban_laboratory_cleanup_started", "autopilot_mode_observed"}
    for row in rows:
        at = row["host_monotonic_ns"]
        if not start <= at <= end or row["event"] not in relevant:
            continue
        if row["event"] == "grant_issued" and row.get("reason") != "selection":
            continue
        timeline.append(dict(kind=row["event"], host_ns=at, from_measurement_s=(at-start)/NS,
                             uav_id=row.get("uav_id"), leader_id=row.get("leader_id"), epoch=row.get("epoch"),
                             mode=row.get("mode"), valid_from_ns=row.get("valid_from_ns"), valid_until_ns=row.get("valid_until_ns")))
    critical_CIDs = analysis["uart_after_NLOS"]["all_five"].get("command_ids") or {}
    critical = (max(critical_CIDs, key=lambda i:analysis["uart_after_NLOS"][f"uav{i}"]["event_host_ns"])
                if critical_CIDs else None)
    critical_CID = critical_CIDs.get(critical)
    chain = []
    for path in [run/"logs/coordination_gcs.jsonl", *sorted((run/"logs").glob("coordination_uav*.jsonl"))]:
        with path.open() as stream:
            for line in stream:
                row = json.loads(line)
                at = row["host_monotonic_ns"]
                if not start <= at <= end:
                    continue
                if row.get("command_id") == critical_CID and row.get("event") in ("gcs_send","master_receive","master_forward","follower_receive","uart_admitted"):
                    chain.append(row)
                if path.name != "coordination_gcs.jsonl" and row.get("event") in ("hold_uart_written","hold_mode_observed","leader_activated"):
                    timeline.append(dict(kind=row["event"],host_ns=at,from_measurement_s=(at-start)/NS,
                        uav_id=row.get("uav_id"),leader_id=row.get("leader_id"),epoch=row.get("epoch")))
    command_path = args.analysis/"relay_audit/metrics/relay_commands.csv"
    with command_path.open() as stream:
        commands = list(csv.DictReader(stream))
    last_UART = []
    for i in range(1,6):
        admitted = [r for r in commands if r["uav_id"] == str(i) and r["admitted_ns"]]
        last = max(admitted,key=lambda r:float(r["admitted_ns"])) if admitted else None
        last_UART.append(dict(uav_id=i, last_target_UART_host_ns=last["admitted_ns"] if last else None,
            from_measurement_s=(float(last["admitted_ns"])-start)/NS if last else None,
            command_id=last["command_id"] if last else None, goal_id=last["goal_id"] if last else None))
    telemetry_goals = {(r["uav_id"],r["goal_id"]) for r in rows if r["event"] == "waypoint_reached_telemetry" and start <= r["host_monotonic_ns"] <= end}
    physical_goals = {(r["uav_id"],r["goal_id"]) for r in rows if r["event"] == "action_completed" and start <= r["host_monotonic_ns"] <= end}
    env = dict(line.split("=",1) for line in (run/"environment.txt").read_text().splitlines() if "=" in line)
    with (args.analysis/"metrics/urban_trajectory.csv").open() as stream:
        final_poses = {row["uav_id"]:row for row in csv.DictReader(stream) if row["quality"] == "observed"}
    common = analysis["uart_after_NLOS"]["all_five"].get("event_host_ns")
    late_shadow = next((r["host_ns"] for r in transitions if r["pair"] == "3-5" and r["kind"] == "NLOS_entry"
                        and selections and r["host_ns"] > selections[0]["host_monotonic_ns"]), None)
    phases = []
    for name, left, right in [("whole_measurement",start,end),("first_NLOS_to_common_UART",trigger_ns,common),
                              ("late_peer_shadow_to_end",late_shadow,end)]:
        if left is None or right is None:
            continue
        offered = [r for r in rows if r["event"] == "control_refresh_offered" and left <= r["host_monotonic_ns"] <= right]
        failed = [r for r in rows if r["event"] == "control_refresh_unavailable" and left <= r["host_monotonic_ns"] <= right]
        phases.append(dict(phase=name,left_host_ns=left,right_host_ns=right,offered=len(offered),
            unavailable=len(failed),unavailable_by_reason=json.dumps({reason:sum(r.get("reason")==reason for r in failed)
                for reason in sorted({r.get("reason","") for r in failed})},sort_keys=True)))
    result = dict(run_id=summary["run_id"], raw=str(run), recorded_status=summary["status"], recorded_error=summary.get("error"),
        runtime_source_commit=env.get("git_head"), source_dirty_paths=env.get("source_dirty_paths"),
        measured_window_complete=analysis["observation_window"]["complete"], measurement_source_s=summary["measurement_duration_source_s"],
        measurement_wall_s=summary["measurement_duration_wall_s"], primary_NLOS_from_measurement_s=(trigger_ns-start)/NS if trigger_ns else None,
        post_NLOS_common_UART_s=analysis["uart_after_NLOS"]["all_five"]["time_s"], detections=detections, selections=selections,
        first_reconstructed_eligible_after_detection=first_eligible,
        maximum_old_issued_expiry_host_ns=max((r["valid_until_ns"] for r in rows if r["event"] == "grant_issued"
            and selections and r.get("epoch",0) < selections[0]["epoch"]),default=None),
        last_target_UART=last_UART, lifecycle=summary["uavs"],
        telemetry_waypoint_marks=len(telemetry_goals), physical_completion_events=len(physical_goals),
        telemetry_without_physical_completion_event=sorted(telemetry_goals-physical_goals),
        physical_checks=analysis["physical_checks"], authority=analysis["authority"],
        critical_CID=critical_CID, critical_CID_chain=sorted(chain,key=lambda r:r["host_monotonic_ns"]),
        final_measurement_poses=final_poses, rates=analysis["rates"]["all_five"],performance=analysis["performance"],
        analyzer_provenance=analysis["provenance"],
        source_sha256={str(path):hashlib.sha256(path.read_bytes()).hexdigest() for path in
            [run/"logs/coordination_gcs.jsonl",run/"logs/geometry_links.csv",run/"logs/urban_scenario.json",
             run/"metrics/relay_experiment_summary.json",Path(__file__)]},
        interpretation_limits=["one development pilot, no fixed/failover causal effect or confidence interval",
            "geometric NLOS is not packet outage; actual direct-probe timeout is separately observed",
            "latest log receipt is a proxy for the immediately preceding internal receipt assignment",
            "candidate values replay the recorded probes, not independent RF reachability or future usability",
            "measurement-window observations survive failed laboratory teardown; failed attempt remains failed",
            "telemetry waypoint progression and Gazebo completion events have distinct clock/tolerance checks"])
    (output/"summary.json").write_text(json.dumps(result,indent=2,allow_nan=False)+"\n")
    write_csv(output/"candidate_freshness.csv",eligibility)
    write_csv(output/"timeline.csv",sorted(timeline,key=lambda r:r["host_ns"]))
    write_csv(output/"last_target_UART.csv",last_UART)
    write_csv(output/"offer_phases.csv",phases)
    print(json.dumps(dict(output=str(output), run_id=summary["run_id"], checkpoints=len(checkpoints))))


if __name__ == "__main__":
    main()
