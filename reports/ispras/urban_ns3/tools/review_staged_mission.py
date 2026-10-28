#!/usr/bin/env python3
"""Finite raw review of mission/barrier/runtime identity, not a metrics self-review."""
import argparse
import bisect
import csv
import hashlib
import json
import math
from pathlib import Path
import subprocess

NS=1_000_000_000


def rows(path):
    with path.open() as stream:
        for line in stream:
            yield json.loads(line)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def check_dwell(samples,target,hold_s,finish_s,*,telemetry=False):
    selected=[r for r in samples if finish_s-hold_s-.5<=r["t"]<=finish_s]
    since,last=None,None
    gaps=[]
    for row in selected:
        gap=row["t"]-last if last is not None else 0
        if gap<0 or gap>.5 or not row["fresh"]:
            since=None
        if row["fresh"]:
            inside=(math.hypot(row["p"][0]-target[0],row["p"][1]-target[1])<=3
                    and abs(row["p"][2]-target[2])<=1.5 and math.sqrt(sum(v*v for v in row["v"]))<=1)
            if not inside:
                since=None
            elif since is None:
                since=row["t"]
            if since is not None:
                gaps.append(gap)
        last=row["t"]
    span=finish_s-since if since is not None and last is not None and abs(last-finish_s)<.051 else None
    return dict(pass_observed=span is not None and span>=hold_s-1e-8,observed_span_s=span,
                sample_count=len(selected),max_source_gap_s=max(gaps) if gaps else None)


def review_run(raw):
    gcs=list(rows(raw/"logs/coordination_gcs.jsonl"))
    cfg=json.loads((raw/"logs/urban_scenario.json").read_text())
    summary=json.loads((raw/"metrics/relay_experiment_summary.json").read_text())
    start=summary["measurement_start_host_monotonic_ns"]
    end=start+summary["measurement_duration_wall_s"]*NS
    reached=[r for r in gcs if r["event"]=="waypoint_reached_telemetry" and start<=r["host_monotonic_ns"]<=end]
    release=[r for r in gcs if r["event"]=="barrier_released" and start<=r["host_monotonic_ns"]<=end]
    gate_checks=[]
    for row in release:
        bad=[]
        for actor,index in row["minimum_reached"].items():
            marks=[r for r in reached if str(r["uav_id"])==actor and r["waypoint_index"]==index
                   and r["host_monotonic_ns"]<=row["host_monotonic_ns"]]
            produced=row["reference_telemetry_producer_ns"][actor]
            received=row["reference_telemetry_received_ns"][actor]
            if not marks or not produced<=received<=row["host_monotonic_ns"] or row["host_monotonic_ns"]-produced>.5*NS:
                bad.append(actor)
        gate_checks.append(dict(barrier_id=row["barrier_id"],host_ns=row["host_monotonic_ns"],
            elapsed_host_s=(row["host_monotonic_ns"]-start)/NS,requirements=row["minimum_reached"],
            checked_reference_telemetry_producer_ns=row["reference_telemetry_producer_ns"],bad=bad))
    goal_checks=[]
    for row in gcs:
        key=f"{row.get('uav_id')}:{row.get('waypoint_index')}"
        if row["event"]=="goal_created" and key in cfg["mission"].get("barriers",{}):
            gate=[r for r in release if r["barrier_id"]==key and r["host_monotonic_ns"]<=row["host_monotonic_ns"]]
            goal_checks.append(dict(goal_id=row["goal_id"],barrier=key,released_before_goal=bool(gate)))
    telemetry=[]
    for mark in reached:
        source=[dict(t=r["time_boot_ms"]/1000,p=r["telemetry_position_m"],v=r["velocity_enu_mps"],
                     fresh=0<=r["host_monotonic_ns"]-r["producer_host_monotonic_ns"]<=.5*NS)
                for r in gcs if r["event"]=="urban_position_telemetry" and r["uav_id"]==mark["uav_id"] and r["goal_id"]==mark["goal_id"]]
        check=check_dwell(source,mark["target_position_m"],3,mark["time_boot_ms"]/1000,telemetry=True)
        telemetry.append(dict(uav_id=mark["uav_id"],goal_id=mark["goal_id"],declared_dwell_s=mark["dwell_s"],**check))
    actual={i:{} for i in range(1,6)}
    for snapshot in rows(raw/"logs/node_state.jsonl"):
        host=snapshot["published_monotonic_ns"]
        if not start<=host<=end:
            continue
        for node in snapshot.get("nodes",[]):
            if not str(node.get("id","")).startswith("uav"):
                continue
            i=int(node["id"][3:]);t=node.get("source_sim_time_s")
            if t is None:
                continue
            sample=node.get("sample_monotonic_ns")
            actual[i].setdefault(t,dict(t=t,p=node["position_m"],v=node["velocity_enu_mps"],
                fresh=not snapshot.get("fault") and not node.get("stale",True) and sample is not None and 0<=host-sample<=.5*NS))
    physical=[]
    for mark in gcs:
        if mark["event"]=="action_completed" and mark.get("waypoint_index") is not None and start<=mark["host_monotonic_ns"]<=end:
            samplelist=sorted(actual[mark["uav_id"]].values(),key=lambda r:r["t"])
            check=check_dwell(samplelist,mark["target_position_m"],2,mark["source_sim_time_s"])
            physical.append(dict(uav_id=mark["uav_id"],goal_id=mark["goal_id"],declared_dwell_s=mark["dwell_s"],**check))
    parameters={}
    for name in ("before","after"):
        path=raw/f"configuration/parameters-{name}/parameters.csv"
        if path.is_file():
            with path.open() as stream:
                parameters[name]=[{k:r[k] for k in ("system_id","component_id","param_id","value_json","param_index","param_count")}
                                  for r in csv.DictReader(stream) if r["param_id"]=="WP_SPD"]
        else:
            parameters[name]=None
    # Independently read the selected critical goal's raw mission records. This
    # arithmetic does not certify the author's own metrics implementation.
    critical_goal=next((r for r in gcs if r["event"]=="goal_created" and r.get("goal_id")=="waypoint4:uav1"),None)
    critical_done=next((r for r in gcs if r["event"]=="action_completed" and r.get("goal_id")=="waypoint4:uav1"),None)
    primary_NLOS=None;last_los=None
    with (raw/"logs/geometry_links.csv").open() as stream:
        for row in csv.DictReader(stream):
            if {row["from_id"],row["to_id"]}!={"0","1"}:
                continue
            los=row["geom_los"].lower() in ("true","1","los");at=int(row["wall_monotonic_ns"])
            if last_los is True and not los and start<=at<=end and primary_NLOS is None:
                primary_NLOS=at
            last_los=los
    raw_goal_timing=None
    if critical_goal and critical_done and primary_NLOS is not None:
        refresh={r["command_id"]:r for r in gcs if r["event"]=="control_refresh_created" and r.get("goal_id")=="waypoint4:uav1"}
        admits=[r for r in rows(raw/"logs/coordination_uav1.jsonl") if r["event"]=="uart_admitted" and r["command_id"] in refresh]
        first=min(admits,key=lambda r:r["uart_write_end_host_monotonic_ns"])
        points=[primary_NLOS,critical_goal["goal_created_host_monotonic_ns"],first["command_created_host_monotonic_ns"],
                first["uart_write_end_host_monotonic_ns"],critical_done["host_monotonic_ns"]]
        raw_goal_timing=dict(goal_id="waypoint4:uav1",host_ns_sequence=points,time_ordered=points==sorted(points),
            intervals_s=[(b-a)/NS for a,b in zip(points,points[1:])],total_s=(points[-1]-points[0])/NS,
            first_admitted_CID=first["command_id"],completion_CID=critical_done["command_id"],
            interpretation="raw mission timing support, not independent review of author-owned metric selection implementation")
    return dict(run_id=raw.name,recorded_status=summary["status"],barrier_checks=gate_checks,gated_goal_checks=goal_checks,
        telemetry_3s=dict(count=len(telemetry),passes=sum(r["pass_observed"] for r in telemetry),checks=telemetry),
        physical_2s=dict(count=len(physical),passes=sum(r["pass_observed"] for r in physical),checks=physical),
        WP_SPD=parameters,critical_mission_goal_created_ns=critical_goal["goal_created_host_monotonic_ns"] if critical_goal else None,
        critical_mission_goal_done_ns=critical_done["host_monotonic_ns"] if critical_done else None,
        raw_selected_goal_timing=raw_goal_timing,
        input_sha256={str(path):sha(path) for path in [raw/"logs/coordination_gcs.jsonl",raw/"logs/node_state.jsonl",raw/"logs/urban_scenario.json"]})


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--integration",type=Path,required=True)
    p.add_argument("--output",type=Path,required=True)
    args=p.parse_args();root=args.integration.resolve()
    frozen_path=root/"runs/urban-ns3-20261028/freeze.json"
    frozen=json.loads(frozen_path.read_text())
    configs={k:dict(expected=v["sha256"],observed=sha(v["path"])) for k,v in frozen["scenario_inputs"].items()}
    mode_a=json.loads(Path(frozen["scenario_inputs"]["nominal"]["path"]).read_text())
    mode_b=json.loads(Path(frozen["scenario_inputs"]["los_only"]["path"]).read_text())
    mode_a["radio"].pop("mode");mode_b["radio"].pop("mode")
    result=dict(scope="noncreator review of mission barriers/dwell and root launcher/frozen configuration; author of metrics does not independently certify own analysis code",
        observed_integration_HEAD=subprocess.check_output(["git","-C",str(root),"rev-parse","HEAD"],text=True).strip(),
        observed_tracked_dirty=subprocess.check_output(["git","-C",str(root),"diff","HEAD","--name-only"],text=True).strip(),
        frozen_source=frozen["source_commit"],frozen_native_SHA=frozen["native_binary"]["sha256"],
        observed_native_SHA=sha(frozen["native_binary"]["path"]),config_hashes=configs,ablation_only_mode=mode_a==mode_b,
        cases=len(frozen["cases"]),main_cases=sum(r["cohort"]=="main" for r in frozen["cases"]),
        source_sha256={str(path):sha(path) for path in [root/"scripts/product/relay_experiment.py",root/"scripts/product/run_urban_case.py",frozen_path,Path(__file__)]},
        runs=[review_run(root/"runs/native-radio-realtime"/name) for name in ("urban-staged-pilot05-failover","urban-staged-pilot06-fixed")])
    first=frozen["cases"][0]
    export=root/"runs/urban-ns3-20261028/analysis"/first["run_id"]
    common_path=export/"relay_audit/metrics/relay_analysis.json"
    if common_path.is_file():
        common=json.loads(common_path.read_text())
        metadata=common["metadata"];env=metadata["environment"];cfg=metadata["config"];stats=metadata["native_stats"]
        ledger_path=root/"runs/urban-ns3-20261028/controllers"/(first["run_id"]+".json")
        ledger=json.loads(ledger_path.read_text())
        raw=root/"runs/native-radio-realtime"/first["run_id"]
        observed=dict(source=env["git_head"],native_SHA=env.get("native_binary_sha256"),
            config_SHA=sha(raw/"logs/urban_scenario.json"),image=metadata["configuration_snapshot"]["container_image_id"],
            rng_seed=stats["rng_seed"],rng_run=stats["rng_run"],radio_mode=stats["geometry_radio_mode"],
            duration_s=cfg["duration_s"],workers=cfg["parameter_workers"],pilot=cfg["pilot"],
            recorded_status=common["run_status"],controller_returncode=ledger.get("returncode"))
        checks=dict(source_matches=observed["source"]==frozen["source_commit"],
            native_matches=observed["native_SHA"]==frozen["native_binary"]["sha256"],
            config_matches=observed["config_SHA"]==first["config_sha256"],image_matches=observed["image"]==frozen["image_id"],
            RNG_matches=(observed["rng_seed"],observed["rng_run"])==(first["rng_seed"],first["rng_run"]),
            mode_matches=observed["radio_mode"]==first["radio_mode"],duration_matches=observed["duration_s"]==first["duration_s"],
            workers_match=observed["workers"]==frozen["parameter_workers"]==int(ledger["environment"]["BAS_NATIVE_PARAMETER_WORKERS"]),
            no_pilot=observed["pilot"] is False,tracked_clean=env["source_dirty_paths"]=="0")
        checks["freeze_precedes_attempt"]=frozen["frozen_at_host_monotonic_ns"]<ledger["started_host_monotonic_ns"]
        result["first_main_identity"]=dict(run_id=first["run_id"],run_id_length=len(first["run_id"]),observed=observed,checks=checks,
            common_export_sha=sha(common_path),controller_sha=sha(ledger_path),
            scope="direct source/config/runtime identity fields only; not an independent certification of metric calculations")
    else:
        result["first_main_identity"]=dict(status="first export not yet present; no busy polling")
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2,allow_nan=False)+"\n")
    print(json.dumps(dict(output=str(args.output),runs=[dict(run_id=r["run_id"],barrier_errors=sum(bool(x["bad"]) for x in r["barrier_checks"]),
        gated_goal_errors=sum(not x["released_before_goal"] for x in r["gated_goal_checks"]),telemetry=r["telemetry_3s"]["passes"],physical=r["physical_2s"]["passes"]) for r in result["runs"]])))


if __name__=="__main__":
    main()
