#!/usr/bin/env python3
"""After live18 stops: split old10 goal→UART using real first-CID GCS/UART.

Read-only raw, no simulator/analyzer invocation, plotting, resampling or changes
to the old critical-UAV choice, three components, or physical endpoint.
"""
from __future__ import annotations
import argparse
from collections import defaultdict
import csv
import hashlib
import json
import math
from pathlib import Path
import statistics

ROOT=Path(__file__).resolve().parents[2]
BASE=Path(__file__).resolve().parent
PHASES=("fault_to_goal_creation_s","goal_creation_to_first_command_creation_s",
    "first_command_creation_to_UART_s","first_UART_to_completion_s",
    "goal_creation_to_first_UART_s","fault_to_completion_s")


def num(value):
    try:
        result=float(value)
        return result if math.isfinite(result) else None
    except (ValueError,TypeError):return None


def stamp(row,key):
    value=num(row.get(key))
    return int(value) if value is not None and value.is_integer() else None


def csv_rows(path):
    if not path.is_file():return []
    with path.open(newline="") as stream:return list(csv.DictReader(stream))


def write_csv(path,rows):
    fields=list(dict.fromkeys(k for row in rows for k in row))
    with path.open("w",newline="") as stream:
        writer=csv.DictWriter(stream,fieldnames=fields,extrasaction="ignore")
        writer.writeheader();writer.writerows(rows)


def select_events(path,run_id,wanted):
    matches=[]
    if not path.is_file():return matches
    with path.open() as stream:
        for line_number,line in enumerate(stream,1):
            try:row=json.loads(line)
            except ValueError as error:raise ValueError(f"{path}:{line_number}: malformed JSONL") from error
            if row.get("command_id") in wanted and row.get("event",row.get("stage")) in ("gcs_send","uart_admitted"):
                if row.get("run_id")!=run_id:raise ValueError(f"foreign-run matching CID in {path}:{line_number}")
                matches.append(row)
    return matches


def split_one(original,events,offers,max_age_ms):
    row=dict(original)
    row.update(subphase_status="unverifiable_raw_first_CID",first_command_created_host_ns=None,
        goal_creation_to_first_command_creation_s=None,first_command_creation_to_UART_s=None,
        subphase_sum_error_s=None)
    cid=original["first_admitted_goal_CID"]
    select=lambda name:[r for r in events if r.get("command_id")==cid and r.get("event",r.get("stage"))==name]
    sends,admissions=select("gcs_send"),select("uart_admitted")
    matching_offers=[r for r in offers if r.get("command_id")==cid]
    if len(sends)!=1 or len(admissions)!=1 or len(matching_offers)!=1:
        return dict(row,reason=f"first CID matches: sends={len(sends)}, admissions={len(admissions)}, offers={len(matching_offers)}")
    sent,admitted,offer=sends[0],admissions[0],matching_offers[0]
    created=stamp(sent,"command_created_host_monotonic_ns")
    goal=stamp(original,"goal_created_host_ns")
    uart=stamp(admitted,"uart_write_end_host_monotonic_ns")
    start_write=stamp(admitted,"uart_write_started_host_monotonic_ns")
    checked=stamp(admitted,"admission_checked_host_monotonic_ns")
    low,high=stamp(admitted,"valid_from_ns"),stamp(admitted,"valid_until_ns")
    reasons=[]
    if None in (created,goal,uart,start_write,checked,low,high) or max_age_ms is None:
        reasons.append("missing exact first-command/goal/UART/grant/age-bound timestamp")
    else:
        if not goal <= created <= start_write <= uart <= checked:reasons.append("unordered first-goal/command/UART/check clocks")
        if not low <= start_write <= uart <= checked < high:reasons.append("first admission is not within its exact grant")
        if (uart-created)/1e6>max_age_ms:reasons.append("first command exceeds effective command max age")
    for raw in (sent,admitted):
        if num(raw.get("uav_id"))!=original["critical_uav_id"]:reasons.append("first CID UAV mismatch")
        if raw.get("leader_id")!=original["leader_id"] or raw.get("epoch")!=original["epoch"]:reasons.append("first CID relay/epoch mismatch")
        if raw.get("grant_sequence")!=original["first_grant_sequence"]:reasons.append("first CID grant mismatch")
        if raw.get("target_frame")!="ENU" or raw.get("target_position_m")!=original["target_position_m"]:reasons.append("first CID ENU target mismatch")
    if sent.get("node_id")!=0 or admitted.get("node_id")!=original["critical_uav_id"]:reasons.append("first CID sender/UART actor mismatch")
    if stamp(admitted,"command_created_host_monotonic_ns")!=created:reasons.append("created timestamp differs between GCS and UART")
    if uart!=stamp(original,"first_goal_uart_host_ns"):reasons.append("raw UART differs from original first-CID selection")
    if offer.get("goal_id")!=original["goal_id"] or stamp(offer,"goal_created_host_monotonic_ns")!=goal:reasons.append("raw offer goal identity/time mismatch")
    if num(offer.get("uav_id"))!=original["critical_uav_id"] or num(offer.get("leader_id"))!=original["leader_id"] or num(offer.get("epoch"))!=original["epoch"]:reasons.append("raw offer UAV/relay/epoch mismatch")
    if reasons:return dict(row,reason="; ".join(dict.fromkeys(reasons)))
    wait,delivery=(created-goal)/1e9,(uart-created)/1e9
    error=wait+delivery-original["goal_creation_to_first_UART_s"]
    if abs(error)>1e-9:return dict(row,reason="subphases do not reproduce original combined component",subphase_sum_error_s=error)
    return dict(row,subphase_status="observed",first_command_created_host_ns=created,
        goal_creation_to_first_command_creation_s=wait,first_command_creation_to_UART_s=delivery,
        subphase_sum_error_s=error,first_command_creation_source="raw gcs_send.command_created_host_monotonic_ns",
        first_UART_source="raw uart_admitted.uart_write_end_host_monotonic_ns",
        goal_identity_source="original first-CID lineage + raw relay_control_refresh offer",
        first_command_to_UART_age_bound_ms=max_age_ms)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-decomposition",type=Path,default=BASE/"decomposition-old30/decomposition.json")
    parser.add_argument("--raw-runs-root",type=Path,default=ROOT/"runs/native-radio-realtime")
    parser.add_argument("--output",type=Path,default=BASE/"decomposition-old30-subphases")
    args=parser.parse_args()
    original=json.loads(args.input_decomposition.read_text())
    if len(original)!=20 or len({r["run_id"] for r in original})!=10 or any(r.get("status")!="observed" for r in original):
        raise SystemExit("Expected exactly the previously observed old10 selective decompositions ×2 scopes; do not pool new18/pilots")
    if any("paper-relay-selective-" not in r["run_id"] for r in original):
        raise SystemExit("Unexpected cohort; only old10 selective runs are permitted")
    grouped=defaultdict(list)
    for row in original:grouped[row["run_id"]].append(row)
    rows=[]
    for run_id,decompositions in sorted(grouped.items()):
        raw=args.raw_runs_root/run_id
        wanted={r["first_admitted_goal_CID"] for r in decompositions}
        combined=raw/"logs/coordination_events.jsonl"
        paths=([combined] if combined.is_file() else [raw/"logs/coordination_gcs.jsonl",
            *[raw/f"logs/coordination_uav{i}.jsonl" for i in sorted({r["critical_uav_id"] for r in decompositions})]])
        events=[r for path in paths for r in select_events(path,run_id,wanted)]
        offers=csv_rows(raw/"metrics/relay_control_refresh.csv")
        config_path=raw/"metrics/relay_experiment_config.json"
        config=json.loads(config_path.read_text()) if config_path.is_file() else {}
        max_age_ms=num(config.get("effective_service_args",{}).get("command_max_age_ms"))
        rows.extend(split_one(row,events,offers,max_age_ms) for row in decompositions)
    means=[]
    for policy in ("fixed","failover"):
        for group in ("all_five","four_followers"):
            chosen=[r for r in rows if r["policy"]==policy and r["group"]==group]
            observed=[r for r in chosen if r["subphase_status"]=="observed"]
            means.append(dict(policy=policy,group=group,planned_runs=5,observed_subphase_runs=len(observed),
                unverifiable_subphase_runs=len(chosen)-len(observed),
                **{key:statistics.fmean(r[key] for r in observed) if observed else None for key in PHASES}))
    points=[]
    for group in ("all_five","four_followers"):
        for repeat in range(1,6):
            pair={r["policy"]:r for r in rows if r["group"]==group and int(r["rng_run"])==repeat}
            for metric in PHASES:
                a,b=(pair.get(policy,{}) for policy in ("fixed","failover"))
                valid=all(r.get("subphase_status")=="observed" for r in (a,b))
                points.append(dict(group=group,rng_run=repeat,metric=metric,fixed=a.get(metric),failover=b.get(metric),
                    failover_minus_fixed=b[metric]-a[metric] if valid else None,
                    paired_status="observed" if valid else "unverifiable_first_CID_subphase"))
    args.output.mkdir(parents=True,exist_ok=True)
    write_csv(args.output/"rows.csv",[{k:json.dumps(v) if isinstance(v,(dict,list)) else v for k,v in r.items()} for r in rows])
    write_csv(args.output/"means.csv",means);write_csv(args.output/"all-paired-points.csv",points)
    result=dict(input_decomposition=str(args.input_decomposition.resolve()),
        input_sha256=hashlib.sha256(args.input_decomposition.read_bytes()).hexdigest(),
        analysis_method_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        raw_runs_root=str(args.raw_runs_root.resolve()),old_selective_runs=10,new18_pooled=False,resimulated=False,
        rows=rows,means=means,paired_points=points,
        limits=["Original first eligible completion and critical UAV are retained; no first-CID or endpoint is reselected.",
            "Goal creation→first command creation includes refresh scheduling and issuer/lease availability. It is not radio latency.",
            "First command creation→UART is same-CID application delivery, including local processing/relay and network. It is not pure RF airtime.",
            "Latest completion-refresh CID remains separate and is never substituted for the first movement-goal CID.",
            "Original three-component endpoint remains unchanged. Failed lineage joins remain unverifiable, never zero.",
            "This companion computes descriptive means and all five paired points per scope; it does not estimate a new confidence interval."])
    (args.output/"subphases.json").write_text(json.dumps(result,indent=2,allow_nan=False)+"\n")
    print(json.dumps(dict(old_selective_runs=10,observed_lineages=sum(r["subphase_status"]=="observed" for r in rows),planned_lineages=20,means=means),allow_nan=False))


if __name__=="__main__":main()
