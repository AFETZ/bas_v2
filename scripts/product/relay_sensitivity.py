#!/usr/bin/env python3
"""Prepare/analyze the separate 18-run study; runtime launch belongs to root."""
from __future__ import annotations

import argparse
from collections import defaultdict
import csv
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import random
import subprocess
import time

SPEC = importlib.util.spec_from_file_location("relay_analysis", Path(__file__).with_name("analyze_relay_experiment.py"))
A = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(A)
STUDY = "relay_sensitivity_18_v1"
DURATIONS = (.5, 2., 10.)
START_SEED, ORDER_SEED = 20261027, 20261028
METRICS = ("run_completed", "lifecycle_success", "offered_availability", "timely_per_offered", "timely_per_sent",
    "common_UART_recovery_s", "common_UART_by4s", "common_UART_over4_or_not_recovered", "common_followers_recovery_s",
    "handover_changed", "handover_by4s_where_changed", "physical_new_goal_recovery_s", "new_relay_selections",
    "selected_after_RFend", "activated_after_RFend", "selected_after_confirmed_old_channel", "activated_after_confirmed_old_channel",
    "integral_rtf", "p5_rtf", "control_unrecovered_time_30s", "goal_unrecovered_time_30s")


def plan() -> list[dict]:
    start_rng, order_rng = random.Random(START_SEED), random.Random(ORDER_SEED)
    starts = {repeat: round(start_rng.uniform(40, 60), 6) for repeat in (1, 2, 3)}
    blocks = [(duration, repeat) for duration in DURATIONS for repeat in (1, 2, 3)]
    order_rng.shuffle(blocks)
    result = []
    for duration, repeat in blocks:
        policies = ["fixed", "failover"]
        order_rng.shuffle(policies)
        milliseconds = int(duration * 1000)
        for policy in policies:
            result.append(dict(order=len(result)+1, study_id=STUDY, experiment_profile="sensitivity",
                pair_id=f"sensitivity-d{milliseconds:05d}ms-seed1-run{repeat}", repeat=repeat,
                rng_seed=1, rng_run=repeat, policy=policy, fault_profile="selective",
                outage_duration_s=f"{duration:.6f}", fault_start_s=f"{starts[repeat]:.6f}",
                fault_end_s=f"{starts[repeat]+duration:.6f}", duration_s="180.000000",
                run_id=f"paper-sensitivity-d{milliseconds:05d}ms-{policy}-s1-r{repeat:02d}",
                start_seed=START_SEED, order_seed=ORDER_SEED, configuration_status="root_freeze_after_code_integration"))
    return result


def validate_plan(rows: list[dict]) -> None:
    expected = [{k:str(v) for k,v in row.items()} for row in plan()]
    if [{k:str(v) for k,v in row.items()} for row in rows] != expected:
        raise ValueError("Sensitivity plan differs from the predeclared seeds/order/start/factor matrix")


def launch_specs(rows: list[dict]) -> list[dict]:
    validate_plan(rows)
    return [dict(order=r["order"], run_id=r["run_id"], root_freeze_required=True,
        argv=["flock", "--nonblock", "--conflict-exit-code", "75", "/tmp/bas-paper-runtime.lock", "./network/ns3/run_native_radio_five_uav.sh"],
        environment_overrides=dict(BAS_NATIVE_FIVE_RUN_ID=r["run_id"], BAS_NATIVE_EXPERIMENT_PROFILE="sensitivity",
            BAS_NATIVE_EXPERIMENT_PILOT="0", BAS_NATIVE_COORDINATION_POLICY=r["policy"], BAS_NATIVE_FAULT_PROFILE="selective",
            BAS_NATIVE_RNG_SEED=str(r["rng_seed"]), BAS_NATIVE_RNG_RUN=str(r["rng_run"]),
            BAS_NATIVE_EXPERIMENT_DURATION_S=str(r["duration_s"]), BAS_NATIVE_FAULT_START_S=str(r["fault_start_s"]),
            BAS_NATIVE_FAULT_END_S=str(r["fault_end_s"]))) for r in rows]


def timing_and_selections(events: list[dict], offers: list[dict], fault: float | None, fault_end: float | None,
                          initial_master: int, *, measurement_start_ns: float | None = None,
                          measurement_end_ns: float | None = None) -> dict:
    if fault is None or fault_end is None or measurement_start_ns is None or measurement_end_ns is None:
        return dict(status="not_recorded", reason="fault or measurement boundaries unavailable")
    stamp = lambda r: A.finite(r.get("host_monotonic_ns"))
    within = lambda r: stamp(r) is not None and measurement_start_ns <= stamp(r) <= measurement_end_ns
    before = lambda r: stamp(r) is not None and stamp(r) <= fault
    probes = [r for r in events if r.get("event") == "gcs_probe_echo_received" and r.get("uav_id") == initial_master]
    heartbeat = max((r for r in probes if before(r)), key=stamp, default=None)
    sent = [A.finite(r.get("probe_sent_host_monotonic_ns")) for r in probes]
    explicit = [r for r in events if r.get("event") == "gcs_probe_issued" and r.get("destination_id", r.get("uav_id")) == initial_master]
    explicit_sent = [A.finite(r.get("probe_sent_host_monotonic_ns")) for r in explicit if r.get("send_ok") is True]
    sent = explicit_sent if explicit else sent
    previous_probe = max((t for t in sent if t is not None and t <= fault), default=None)
    renewals = [r for r in events if r.get("event") == "grant_issued" and r.get("reason") == "renewal"]
    renewal = max((r for r in renewals if before(r)), key=stamp, default=None)
    grants = [r for r in events if r.get("event") == "grant_issued"]
    previous_grant = max((r for r in grants if before(r)), key=stamp, default=None)
    scheduled = [A.finite(r.get("scheduled_host_monotonic_ns")) for r in offers]
    actual = [A.finite(r.get("host_monotonic_ns")) for r in offers]
    target_sends = [r for r in events if r.get("event") == "gcs_send" and A.target(r) is not None]
    created = [A.finite(r.get("command_created_host_monotonic_ns")) for r in target_sends]
    previous = lambda values: max((t for t in values if t is not None and t <= fault), default=None)
    phase_request = max((r for r in events if r.get("event") == "phase_requested" and r.get("phase") == "experiment_fault" and before(r)), key=stamp, default=None)
    direct_restored = min((stamp(r) for r in probes if within(r) and stamp(r) >= fault_end
        and (A.finite(r.get("probe_sent_host_monotonic_ns")) or 0) >= fault_end), default=None)
    roles, selections = set(), []
    ordered_grants = sorted(grants, key=lambda r: stamp(r) or 0)
    first_scope = (ordered_grants[0].get("leader_id"), ordered_grants[0].get("epoch")) if ordered_grants else (initial_master,1)
    for r in ordered_grants:
        scope = r.get("leader_id"), r.get("epoch")
        if scope not in roles:
            roles.add(scope)
            if scope[0] != initial_master or len(roles) > 1:
                selections.append(r)
    outside_selections=sum(not within(r) for r in selections)
    selections=[r for r in selections if within(r)]
    active = {}
    for r in events:
        if r.get("event") == "leader_activated" and r.get("node_id") == r.get("leader_id") and (r.get("leader_id"),r.get("epoch")) != first_scope:
            key = r["leader_id"], r["epoch"]
            if stamp(r) is not None and (key not in active or stamp(r) < stamp(active[key])):
                active[key] = r
    outside_activations=sum(not within(r) for r in active.values())
    active={key:r for key,r in active.items() if within(r)}
    count_after = lambda rows, boundary: sum(stamp(r) is not None and stamp(r) >= boundary for r in rows) if boundary is not None else None
    times = dict(previous_probe_sent_host_ns=previous_probe,
        previous_master_echo_received_host_ns=stamp(heartbeat) if heartbeat else None,
        previous_master_echo_reply_sent_host_ns=heartbeat.get("reply_sent_host_monotonic_ns") if heartbeat else None,
        previous_grant_issue_event_host_ns=stamp(previous_grant) if previous_grant else None,
        previous_renewal_issue_event_host_ns=stamp(renewal) if renewal else None,
        previous_scheduled_refresh_host_ns=previous(scheduled), previous_actual_refresh_offer_host_ns=previous(actual),
        previous_actual_target_command_created_host_ns=previous(created))
    return dict(status="observed", host_clock="one Linux host_monotonic; ns",
        measurement_window_host_ns=[measurement_start_ns,measurement_end_ns],
        fault_host_ns=fault, fault_end_host_ns=fault_end,
        fault_phase_request_host_ns=stamp(phase_request) if phase_request else None,
        fault_phase_request_source_sim_time_s=phase_request.get("source_sim_time_s") if phase_request else None,
        probe_send_coverage="all_logged_local_UDP_accepted_probe_issues" if explicit else "observed_echoed_probes_only",
        probe_local_send_failures=sum(r.get("send_ok") is False for r in explicit),
        previous_times=times, ages_at_fault_s={k:(fault-v)/1e9 if v is not None else None for k,v in times.items()},
        old_channel_confirmed_restored_host_ns=direct_restored,
        new_relay_selections=len(selections), new_relay_own_activations=len(active),
        outside_measurement_new_selections=outside_selections, outside_measurement_first_activations=outside_activations,
        selected_after_RFend=count_after(selections,fault_end), activated_after_RFend=count_after(active.values(),fault_end),
        selected_after_confirmed_old_channel=count_after(selections,direct_restored),
        activated_after_confirmed_old_channel=count_after(active.values(),direct_restored),
        interpretation="Post-RFend/confirmed-echo counts are prespecified potentially unnecessary changes, not an assertion of no benefit.")


def outcome_by_four(recovery: dict) -> float | None:
    if recovery.get("recovered") is True:
        return float(recovery["time_s"] <= 4)
    horizon = recovery.get("censor_time_s")
    return 0. if horizon is not None and horizon >= 4 else None


def parameter_capture_report(raw: Path, requested: bool | None, window: dict) -> dict:
    """Report capture completeness independently of run/network eligibility.

    These are recorded exporter verdicts, not a second wire/index validation.
    Absent summaries are unknown; a skipped post-LAND capture stays skipped.
    """
    metadata=A.read_json(raw/"metrics/relay_experiment_summary.json").get("parameter_snapshots",{})
    result=dict(requested=requested,completeness_source="recorded parameter exporter summaries; independent raw audit remains separate")
    expected={(i,1) for i in range(1,6)}
    for stage in ("before","after"):
        path=raw/f"configuration/parameters-{stage}/summary.json"
        exported=A.read_json(path)
        components=exported.get("components",[])
        parent=metadata.get(stage,{})
        targets={(c.get("system_id"),c.get("component_id")) for c in components}
        complete=(len(components)==5 and targets==expected and all(c.get("complete") is True for c in components)) if exported else None
        start,end=(A.finite(parent.get(k)) for k in ("started_host_monotonic_ns","ended_host_monotonic_ns"))
        low,high=(A.finite(window.get(k)) for k in ("start_host_monotonic_ns","end_host_monotonic_ns"))
        overlap=max(start,low)<min(end,high) if None not in (start,end,low,high) else None
        if exported:status=exported.get("status","unclassified_exporter_summary")
        elif path.is_file():status="unreadable_or_empty_summary"
        elif parent:status=parent.get("status") or "missing_table_summary"
        else:status="not_recorded" if requested is True else "not_requested" if requested is False else "request_not_recorded"
        result[stage]=dict(status=status,all_five_tables_complete=complete,
            observed_component_count=len(components) if exported else None,
            complete_component_count=sum(c.get("complete") is True for c in components) if exported else None,
            expected_component_count=5,observed_target_components=sorted(targets,key=lambda pair:tuple(map(str,pair))),
            requested_workers=exported.get("settings",{}).get("workers"),effective_workers=exported.get("effective_workers"),
            process_returncode=parent.get("returncode"),parent_stage_record=parent,
            overlaps_measurement_host_interval=overlap,exporter_summary_path=str(path),
            exporter_summary_present=path.is_file(),exporter_summary_readable=bool(exported))
    return result


def sensitivity_record(planned: dict, result: dict, events: list[dict], offers: list[dict]) -> dict:
    record = A.campaign_record(planned,result)
    cfg = result["metadata"]["config"]
    issues = []
    if cfg.get("experiment_profile") != "sensitivity" or result.get("pilot"):
        issues.append("explicit nonpilot sensitivity profile not recorded")
    if type(cfg.get("parameter_snapshots")) is not bool:
        issues.append("effective parameter_snapshots flag not recorded")
    if type(cfg.get("parameter_workers")) is not int or not 1 <= cfg["parameter_workers"] <= 5:
        issues.append("effective parameter_workers integer 1..5 not recorded")
    for key in ("duration_s","fault_start_s","fault_end_s"):
        actual, expected = A.finite(cfg.get(key)), A.finite(planned[key])
        if actual is None or abs(actual-expected) > 1e-6:
            issues.append(f"actual {key} differs from frozen sensitivity row")
    # Validate planned factors before excluding them from the common signature.
    record["planned_variable_settings"] = {k:cfg.get(k) for k in ("fault_start_s","fault_end_s")}
    for key in ("fault_start_s","fault_end_s"):
        record["comparability"]["mission"].pop(key,None)
    record["comparability"]["experiment_profile"] = cfg.get("experiment_profile")
    record["comparability"]["parameter_snapshots"] = cfg.get("parameter_snapshots")
    record["comparability"]["parameter_workers"] = cfg.get("parameter_workers")
    record["comparability"]["study_id"] = STUDY
    record["study_eligible"] = record["study_eligible"] and not issues
    record["network_eligible"] = record["network_eligible"] and not issues
    record["exclusion_reasons"] += issues
    fault, end = (result["actual_fault"][k] for k in ("start_host_monotonic_ns","end_host_monotonic_ns"))
    behavior = timing_and_selections(events,offers,fault,end,int(cfg.get("initial_master",1)),
        measurement_start_ns=result["observation_window"]["start_host_monotonic_ns"],
        measurement_end_ns=result["observation_window"]["end_host_monotonic_ns"])
    behavior["native_fault_ns3_time_s"] = next((A.finite(r.get("time_s")) for r in result["actual_fault"]["events"] if r["event"] == "fault_start"),None)
    behavior["native_fault_end_ns3_time_s"] = next((A.finite(r.get("time_s")) for r in result["actual_fault"]["events"] if r["event"] == "fault_end"),None)
    behavior["actual_RF_duration_s"] = (end-fault)/1e9 if fault is not None and end is not None else None
    record["fault_phase_and_selections"] = behavior
    record["measurement_window"]=dict(result["observation_window"])
    record["parameter_capture"]=parameter_capture_report(Path(result["raw_run_dir"]),cfg.get("parameter_snapshots"),result["observation_window"])
    control = result["control_recovery"]["all_five"]
    record["endpoint_status"] = dict(common_UART=control, handover=result["handover_recovery"]["all_five"],
        physical_goal=result["goal_recovery_all_five"])
    group_handover = result["handover_recovery"]["all_five"].get("recovered") is True
    changed = (behavior.get("new_relay_own_activations",0) > 0 or group_handover) if behavior["status"] == "observed" else None
    record["handover_change_evidence"] = "own activation or verified new-relay UART group" if changed else "none observed" if changed is False else "not recorded"
    by4 = outcome_by_four(control)
    record["outcomes"].update(common_UART_recovery_s=control.get("time_s"), common_UART_by4s=by4,
        common_UART_over4_or_not_recovered=1-by4 if by4 is not None else None,
        common_followers_recovery_s=result["control_recovery"]["four_followers"].get("time_s"),
        handover_by4s_where_changed=outcome_by_four(result["handover_recovery"]["all_five"]) if changed else None,
        handover_changed=float(changed) if changed is not None else None, physical_new_goal_recovery_s=result["goal_recovery_all_five"].get("time_s"),
        new_relay_selections=behavior.get("new_relay_selections"),
        selected_after_RFend=behavior.get("selected_after_RFend"), activated_after_RFend=behavior.get("activated_after_RFend"),
        selected_after_confirmed_old_channel=behavior.get("selected_after_confirmed_old_channel"),
        activated_after_confirmed_old_channel=behavior.get("activated_after_confirmed_old_channel"))
    return record


def analyze(rows: list[dict], raw_root: Path, output: Path, live_export: bool) -> dict:
    validate_plan(rows)
    records = []
    for planned in rows:
        raw = raw_root / planned["run_id"]
        if not raw.is_dir():
            records.append(dict(planned,recorded=False,study_eligible=False,network_eligible=False,
                run_status="not_run",outcomes={},comparability={},exclusion_reasons=["not recorded"]))
            continue
        result = A.analyze_run(raw,output/"runs"/planned["run_id"],plots=False,live_export=live_export)
        paths = [raw/"logs/coordination_events.jsonl"] if (raw/"logs/coordination_events.jsonl").exists() else sorted((raw/"logs").glob("coordination_*.jsonl"))
        events = [row for p in paths for row in A.read_jsonl(p,[])]
        records.append(sensitivity_record(planned,result,events,A.read_csv(raw/"metrics/relay_control_refresh.csv")))
    signatures = {json.dumps(r["comparability"],sort_keys=True) for r in records if r["recorded"] and r["study_eligible"]}
    if len(signatures)>1:
        for r in records:
            if r["recorded"]:
                r["study_eligible"]=r["network_eligible"]=False
                r["exclusion_reasons"].append("frozen study configuration changed; do not pool")
    groups = defaultdict(dict)
    for r in records:
        groups[r["pair_id"]][r["policy"]]=r
    points, estimates = [], []
    completion = {"run_completed","lifecycle_success"}
    for pair_id,pair in groups.items():
        fixed,failover=pair["fixed"],pair["failover"]
        metrics=set(METRICS)|set(fixed["outcomes"])|set(failover["outcomes"])
        for metric in sorted(metrics):
            a,b=(r["outcomes"].get(metric) for r in (fixed,failover))
            allowed=all(r.get("study_eligible") if metric in completion else r.get("network_eligible") for r in (fixed,failover))
            points.append(dict(pair_id=pair_id,outage_duration_s=fixed["outage_duration_s"],repeat=fixed["repeat"],metric=metric,
                fixed=a,failover=b,difference=b-a if allowed and a is not None and b is not None else None,
                reason=None if allowed and a is not None and b is not None else "missing/N/A outcome or incomplete/noncomparable run; retain point"))
    for duration in DURATIONS:
        chosen=[r for r in points if float(r["outage_duration_s"])==duration]
        for metric in sorted({r["metric"] for r in chosen}):
            values=[r["difference"] for r in chosen if r["metric"]==metric and r["difference"] is not None]
            estimates.append(dict(outage_duration_s=duration,metric=metric,**A.paired_interval(values)))
    result=dict(study_id=STUDY,main30_not_pooled=True,planned_runs=18,records=records,paired_points=points,paired_estimates=estimates,
        common_configuration_signatures=len(signatures),methods=["Three whole paired runs per duration; all paired points including failed/missing/N/A remain visible.",
            "Common UART recovery may retain the old relay; new-relay handover and its <=4s target are separate.",
            "Counts after RFend or a fresh old-channel echo are potentially unnecessary changes, not an oracle judgment or a promised policy benefit.",
            "No-recovery remains censored. Actual times >4s are retained; by4s failure includes observed no-recovery past4s.",
            "Actual phases use logged host timestamps and separately labelled source/ns3 times; no nominal modulo phase is invented.",
            "Three-pair percentile intervals are exploratory; a degenerate count interval is not a reliability/equivalence guarantee.",
            "Do not pool old30 with new18 or average the three duration factors into one policy effect."])
    A.write_json(output/"metrics/sensitivity_analysis.json",result)
    A.write_csv(output/"metrics/paired_points.csv",points,list(points[0]) if points else ["pair_id","metric","difference"])
    flat = [dict({k:r.get(k) for k in ("order","run_id","pair_id","repeat","policy","outage_duration_s","fault_start_s","fault_end_s","run_status","run_error","recorded","study_eligible","network_eligible")},
        **r["outcomes"], exclusion_reasons="; ".join(r["exclusion_reasons"]),
        measurement_window_complete=r.get("measurement_window",{}).get("complete"),
        **{f"parameter_{stage}_{field}":r.get("parameter_capture",{}).get(stage,{}).get(field)
            for stage in ("before","after") for field in ("status","all_five_tables_complete","observed_component_count","complete_component_count","process_returncode","overlaps_measurement_host_interval")},
        **{f"{name}_{field}":r.get("endpoint_status",{}).get(name,{}).get(field) for name in ("common_UART","handover","physical_goal") for field in ("status","censor_time_s")},
        actual_RF_duration_s=r.get("fault_phase_and_selections",{}).get("actual_RF_duration_s"),
        fault_phase_and_selections=json.dumps(r.get("fault_phase_and_selections"))) for r in records]
    A.write_csv(output/"metrics/sensitivity_runs.csv",flat,
        ["order","run_id","pair_id","repeat","policy","outage_duration_s","fault_start_s","fault_end_s","actual_RF_duration_s","run_status","run_error","recorded","study_eligible","network_eligible","measurement_window_complete",
         *[f"parameter_{stage}_{field}" for stage in ("before","after") for field in ("status","all_five_tables_complete","observed_component_count","complete_component_count","process_returncode","overlaps_measurement_host_interval")],
         *sorted(set(METRICS)|{k for r in records for k in r["outcomes"]}),
         *[f"{name}_{field}" for name in ("common_UART","handover","physical_goal") for field in ("status","censor_time_s")],"exclusion_reasons","fault_phase_and_selections"])
    A.write_csv(output/"metrics/paired_estimates.csv",estimates,list(dict.fromkeys(k for r in estimates for k in r)) if estimates else ["outage_duration_s","metric","n_pairs"])
    return result


def verify_freeze(frozen: dict, plan_path: Path, head: str, dirty: bool, image_id: str) -> None:
    if (frozen.get("study_id") != STUDY or frozen.get("frozen_head") != head or dirty
            or frozen.get("container_image_id") != image_id
            or frozen.get("plan_sha256") != hashlib.sha256(plan_path.read_bytes()).hexdigest()):
        raise ValueError("Root freeze/source/image/plan does not match; preserve data and stop")
    env=frozen.get("base_environment",{})
    if not isinstance(env,dict) or env.get("BAS_NATIVE_PARAMETER_SNAPSHOTS") not in ("0","1"):
        raise ValueError("Root freeze must declare base_environment and parameter snapshot flag explicitly")
    if env.get("BAS_NATIVE_PARAMETER_WORKERS") not in tuple(str(i) for i in range(1,6)):
        raise ValueError("Root freeze must declare parameter workers explicitly as an integer 1..5")


def execute(rows: list[dict], plan_path: Path, frozen_path: Path, raw_root: Path, output: Path, resume: bool) -> int:
    """Root invokes after integration/freeze; each runner subprocess acquires flock."""
    root=Path(__file__).resolve().parents[2]
    if raw_root.resolve() != (root/"runs/native-radio-realtime").resolve():
        raise ValueError("Runner raw root is fixed to this integration's runs/native-radio-realtime")
    frozen=A.read_json(frozen_path)
    image=frozen.get("image_ref","multiagent_simulation:latest")
    def checked():
        head=subprocess.check_output(["git","rev-parse","HEAD"],cwd=root,text=True).strip()
        dirty=subprocess.call(["git","diff","--quiet","HEAD","--"],cwd=root)!=0
        identity=subprocess.check_output(["docker","image","inspect","--format","{{.Id}}",image],cwd=root,text=True).strip()
        verify_freeze(frozen,plan_path,head,dirty,identity)
    validate_plan(rows)
    output.mkdir(parents=True,exist_ok=True)
    failure_streak,any_failure=0,False
    with (output/"controller_events.jsonl").open("a",buffering=1) as log:
        for spec in launch_specs(rows):
            checked()
            raw=raw_root/spec["run_id"]
            if raw.exists():
                if not resume:
                    raise ValueError(f"Raw already exists; no retries/overwrite: {raw}. Root may --resume preserving it.")
                status=A.read_json(raw/"metrics/relay_experiment_summary.json").get("status","incomplete")
                any_failure=any_failure or status!="completed"
                log.write(json.dumps(dict(event="preserved_existing_no_retry",run_id=spec["run_id"],run_status=status,host_monotonic_ns=time.monotonic_ns()))+"\n")
                continue
            # Prevent inherited BAS_* values from silently changing frozen settings.
            env={k:v for k,v in os.environ.items() if not k.startswith("BAS_")}
            env.update({k:str(v) for k,v in frozen["base_environment"].items()})
            env.update(spec["environment_overrides"])
            env["BAS_CONTAINER_IMAGE"]=image
            path=output/(spec["run_id"]+".log")
            suffix=2
            while path.exists():
                path=output/(spec["run_id"]+f".attempt{suffix:02d}.log")
                suffix+=1
            log.write(json.dumps(dict(event="case_started",**spec,log_path=str(path),host_monotonic_ns=time.monotonic_ns()))+"\n")
            with path.open("x") as stream:
                completed=subprocess.run(spec["argv"],cwd=root,env=env,stdout=stream,stderr=subprocess.STDOUT)
            log.write(json.dumps(dict(event="case_exited",run_id=spec["run_id"],returncode=completed.returncode,host_monotonic_ns=time.monotonic_ns()))+"\n")
            if completed.returncode==75:
                return 75  # Another owner's slot; no cleanup or retry.
            failure_streak=failure_streak+1 if completed.returncode else 0
            any_failure=any_failure or completed.returncode!=0
            if failure_streak>=3:
                return 1  # Preserve failures; root diagnoses before the remaining rows.
            # Other failed runs remain raw outcomes; continue the predeclared plan.
    return 1 if any_failure else 0


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    mode=p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write-plan",type=Path)
    mode.add_argument("--launch-specs",type=Path,help="Write root-owned launch specifications; does not execute")
    mode.add_argument("--analyze",action="store_true")
    mode.add_argument("--run",action="store_true",help="Root only after freeze; launches sequential flock-prefixed runner subprocesses")
    p.add_argument("--plan",type=Path,default=Path(__file__).resolve().parents[2]/"network/config/relay_sensitivity_18.csv")
    p.add_argument("--raw-runs-root",type=Path)
    p.add_argument("--output-dir",type=Path)
    p.add_argument("--skip-live-export",action="store_true")
    p.add_argument("--freeze",type=Path)
    p.add_argument("--resume",action="store_true",help="Preserve existing raw without retry; never overwrite")
    a=p.parse_args(argv)
    if a.write_plan:
        generated=plan()
        if a.write_plan.exists():
            validate_plan(A.read_csv(a.write_plan))
        else:
            A.write_csv(a.write_plan,generated,list(generated[0]))
        print(json.dumps(dict(plan=str(a.write_plan),planned_runs=18,executed_runs=0,freeze="root after code integration")))
        return 0
    rows=A.read_csv(a.plan)
    validate_plan(rows)
    if a.launch_specs:
        A.write_json(a.launch_specs,dict(study_id=STUDY,executed_runs=0,launch_specs=launch_specs(rows)))
        return 0
    if not a.raw_runs_root or not a.output_dir:
        p.error("--analyze requires --raw-runs-root and --output-dir")
    if a.run:
        if not a.freeze:
            p.error("--run requires root-created --freeze JSON after code integration")
        return execute(rows,a.plan,a.freeze,a.raw_runs_root,a.output_dir,a.resume)
    analyze(rows,a.raw_runs_root,a.output_dir,not a.skip_live_export)
    return 0


if __name__=="__main__":
    raise SystemExit(main())
