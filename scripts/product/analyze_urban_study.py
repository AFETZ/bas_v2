#!/usr/bin/env python3
"""Aggregate frozen urban attempts by independent paired seed blocks.

Missing attempts remain in the run table. Whole-block contrasts, missing-data
bounds and descriptive auxiliary cohorts are distinct from packet-level counts.
This tool reads existing exports/controllers/raw and never launches a simulator.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import importlib.util
import itertools
import json
import math
from pathlib import Path
import random
import statistics

SPEC = importlib.util.spec_from_file_location("urban_study_run", Path(__file__).with_name("analyze_urban_experiment.py"))
U = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(U)
A = U.RELAY
NS = 1_000_000_000
# Predeclared before the main freeze. A bounded loss is NOT a recovery time.
RESTRICTED_HORIZON_HOST_S = 60.
BOOTSTRAP_SEED = 20261028
BOOTSTRAP_DRAWS = 50000
METRICS = ("timely_per_offered", "completed_waypoint_fraction", "all_routes_finished",
           "new_relay_selections", "integral_RTF", "restricted_post_NLOS_UART_60s")
BOUNDS = dict(timely_per_offered=(0.,1.), completed_waypoint_fraction=(0.,1.),
              all_routes_finished=(0.,1.), restricted_post_NLOS_UART_60s=(0.,60.))


def validate_frozen_blocks(frozen):
    """Reject a malformed design before selecting or estimating any outcomes."""
    cases=frozen.get("cases",[])
    if not cases or len({r.get("run_id") for r in cases})!=len(cases):
        raise ValueError("freeze must contain nonempty unique planned run IDs")
    if any(r.get("cohort","main")=="main" for r in cases) and not frozen.get("native_binary",{}).get("sha256"):
        raise ValueError("main freeze requires the actual post-build native_binary.sha256")
    groups=defaultdict(list)
    for row in cases:
        if not row.get("run_id") or not row.get("block_id"):
            raise ValueError("every frozen case requires run_id and block_id")
        cohort=row.get("cohort","main")
        if cohort not in ("main","sensitivity","engineering_boundary"):
            raise ValueError(f"unknown frozen cohort: {cohort}")
        if row.get("policy") not in ("fixed","failover") or row.get("radio_mode") not in ("geometry","los_only"):
            raise ValueError("invalid frozen policy/radio_mode")
        for key in ("rng_seed","rng_run"):
            if type(row.get(key)) is not int or row[key] < 1:
                raise ValueError(f"every frozen case requires a positive integer {key}")
        if A.finite(row.get("duration_s")) is None or A.finite(row["duration_s"]) <= 0:
            raise ValueError("every frozen case requires a positive duration_s")
        for key in ("scenario_variant","parameter_variant"):
            if not isinstance(row.get(key),str) or not row[key]:
                raise ValueError(f"every frozen case requires explicit {key}")
        if not isinstance(row.get("config_sha256"),str) or not row["config_sha256"]:
            raise ValueError("every frozen case requires config_sha256")
        for key in ("source_commit","image_id"):
            if not frozen.get(key) or row.get(key,frozen[key]) != frozen[key]:
                raise ValueError(f"case {key} differs from the immutable frozen version")
        # block_id is unique within a cohort: splitting by variant here would
        # conceal a mixed-condition block as two apparently incomplete blocks.
        groups[(cohort,row["block_id"])].append(row)
    common=("rng_seed","rng_run","duration_s","scenario_variant","parameter_variant")
    for (cohort,block),rows in groups.items():
        for key in common:
            values={A.finite(r[key]) if key=="duration_s" else json.dumps(r.get(key),sort_keys=True) for r in rows}
            if len(values)!=1:
                raise ValueError(f"frozen block {cohort}/{block} mixes {key}")
        cells=[(r["policy"],r["radio_mode"]) for r in rows]
        if len(set(cells))!=len(cells):
            raise ValueError(f"duplicate frozen policy/radio cell in {cohort}/{block}")
        if cohort=="main":
            expected={(p,m) for p in ("fixed","failover") for m in ("geometry","los_only")}
            if set(cells)!=expected:
                raise ValueError(f"main block {block} must contain exactly four factorial cells")
        elif len(rows)!=2 or {r["policy"] for r in rows}!={"fixed","failover"} or len({r["radio_mode"] for r in rows})!=1:
            raise ValueError(f"auxiliary block {cohort}/{block} must contain one exact fixed/failover pair")
        for mode in {r["radio_mode"] for r in rows}:
            pair=[r for r in rows if r["radio_mode"]==mode]
            if len({r["config_sha256"] for r in pair})!=1:
                raise ValueError(f"fixed/failover config differs in {cohort}/{block}/{mode}")
    return cases


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest() if Path(path).is_file() else None


def interval(differences, *, seed=BOOTSTRAP_SEED, draws=BOOTSTRAP_DRAWS):
    """Resample independent paired/block differences, never members or packets."""
    n = len(differences)
    result = dict(n=n, mean=statistics.fmean(differences) if n else None,
                  median=statistics.median(differences) if n else None,
                  interval_95=None, method=None, resamples=0, differences=differences)
    if n < 2:
        return result
    if n <= 5:
        samples = [sum(values)/n for values in itertools.product(differences,repeat=n)]
        method = "exact ordered whole-pair/block percentile bootstrap"
    else:
        generator = random.Random(seed)
        samples = [sum(generator.choices(differences,k=n))/n for _ in range(draws)]
        method = f"{draws} deterministic whole-pair/block percentile bootstrap; seed={seed}"
    samples.sort()
    def quantile(p):
        point = (len(samples)-1)*p
        lo, hi = math.floor(point), math.ceil(point)
        return samples[lo]+(samples[hi]-samples[lo])*(point-lo)
    return dict(result, interval_95=[quantile(.025),quantile(.975)],method=method,resamples=len(samples),
                scope="exploratory interval; finite resampling does not guarantee nominal coverage")


def bounded_endpoint(endpoint, trigger, end, *, complete, recording_valid, tau=RESTRICTED_HORIZON_HOST_S):
    """Known min(T,tau), with administrative censoring distinct from interruption."""
    base = dict(value=None, observed_time_s=None, censor_time_s=None, status="not_applicable_no_trigger", tau_host_s=tau)
    if trigger is None:
        return dict(base, status="interrupted_before_observed_trigger", unknown_applicability=True,
                    interpretation="interruption prevents determining whether a trigger would occur within the full horizon") if not complete else base
    if not recording_valid or end is None or end < trigger:
        return dict(base,status="unverifiable")
    t = A.finite(endpoint.get("time_s"))
    observed = (end-trigger)/NS
    if observed < tau:
        return dict(base,observed_time_s=t,censor_time_s=observed,
                    status="administrative_short_followup" if complete else "interrupted_before_tau")
    if endpoint.get("status") == "observed" and t is not None and 0 <= t <= observed:
        return dict(base,value=min(t,tau),observed_time_s=t,status="observed",
                    recovered_within_tau=t <= tau)
    if observed >= tau:
        return dict(base,value=tau,censor_time_s=observed,status="right_censored_bounded_loss_known",
                    recovered_within_tau=False,
                    interpretation="60 is bounded unrecovered time, not an observed recovery at60")
    return dict(base,censor_time_s=observed,status="administrative_short_followup" if complete else "interrupted_before_tau")


def interrupted_endpoint(endpoint, *, complete, trigger, origin):
    """Overlay scope on saved endpoint labels without changing measured events."""
    if not complete and trigger is None and str(endpoint.get("status", "")).startswith("not_applicable"):
        return dict(endpoint, original_status=endpoint["status"], status="interrupted_before_observed_trigger",
                    unknown_applicability=True, trigger_kind=origin,
                    interpretation="trigger was not observed before interruption; full-horizon applicability is unknown")
    return endpoint


def after_detection(run_dir, export_dir, result, complete, valid):
    """Re-anchor verified target CIDs on the actual direct-probe timeout event."""
    error = []
    rows = U.load_events(run_dir,error)
    window = result.get("observation_window", {})
    start, end = window.get("start_host_monotonic_ns"),window.get("end_host_monotonic_ns")
    initial = int(A.read_json(run_dir/"metrics/relay_experiment_config.json").get("initial_master",1))
    detection = next((r for r in rows if U.stage(r)=="link_failure_detected" and r.get("leader_id")==initial
                      and r.get("reason")=="direct_GCS_probe_timeout"
                      and start is not None and end is not None and start <= U.stamp(r) <= end), None)
    if detection is None:
        return dict(trigger_event=None,endpoint=interrupted_endpoint(
            dict(status="not_applicable_no_direct_probe_detection",time_s=None,recovered=None),
            complete=complete,trigger=None,origin="direct_GCS_probe_timeout"),errors=error)
    commands = A.read_csv(export_dir/"relay_audit/metrics/relay_commands.csv")
    for row in commands:
        for key in ("created_ns","admitted_ns"):
            row[key]=A.finite(row.get(key))
        for key in ("uav_id","leader_id","epoch"):
            row[key]=int(row[key]) if row.get(key) else None
        for key in ("application_chain_complete","policy_allowed"):
            row[key]=U.boolean(row.get(key))
    point = U.stamp(detection) if detection else None
    endpoints = U.uart_endpoints(commands,point,end,complete,valid and not error)
    return dict(trigger_event=detection,endpoint=endpoints["all_five"],
                definition="actual initial-relay direct-probe timeout → all-five fresh post-detection-created target UART under one relay/epoch",
                errors=error)


def run_record(case, frozen, export_dir, controller, *, runs_root=None):
    """Per-attempt provenance and observation scope remain explicit."""
    export_dir = Path(export_dir)
    path = export_dir/"metrics/urban_analysis.json"
    result, ledger = A.read_json(path), A.read_json(Path(controller))
    common = A.read_json(export_dir/"relay_audit/metrics/relay_analysis.json")
    record = dict(case, attempted=bool(result or ledger), analysis_recorded=bool(result),
        controller_recorded=bool(ledger), recorded_status=result.get("recorded_status","not_run"),
        controller_returncode=ledger.get("returncode"), attempt_failed=bool(result and result.get("recorded_status")!="completed")
            or bool(ledger and ledger.get("returncode") not in (None,0)),
        full_measurement_window=False, primary_eligible=False, provenance_errors=[], recording_errors=[],
        outcomes={k:None for k in METRICS}, input_paths=dict(analysis=str(path),controller=str(controller)),
        input_sha256=dict(analysis=digest(path),controller=digest(controller)))
    if not result:
        record["recording_errors"].append("analysis not recorded; preserve planned/attempted row")
        return record
    problems = record["provenance_errors"]
    if result.get("run_id") != case["run_id"] or ledger.get("run_id") != case["run_id"]:
        problems.append("run identity differs or controller absent")
    if result.get("policy") != case["policy"]:
        problems.append("policy differs from frozen case")
    if result.get("pilot"):
        problems.append("pilot cannot be pooled into the main/auxiliary frozen series")
    for field in ("source_commit","config_sha256"):
        expected = frozen.get(field) if field=="source_commit" else case.get(field)
        if not expected or ledger.get(field) != expected:
            problems.append(f"controller {field} missing/different")
    if result.get("provenance",{}).get("scenario_sha256") != case.get("config_sha256"):
        problems.append("analyzed scenario snapshot differs from frozen config")
    env, stats, cfg = (common.get("metadata",{}).get(k,{}) for k in ("environment","native_stats","config"))
    snapshot=common.get("metadata",{}).get("configuration_snapshot",{})
    if env.get("git_head") != frozen.get("source_commit") or str(env.get("source_dirty_paths")) != "0":
        problems.append("clean frozen runtime source not established")
    expected_binary=frozen.get("native_binary",{}).get("sha256")
    if expected_binary is not None or case.get("cohort","main")=="main":
        observed_binary=env.get("native_binary_sha256")
        if not observed_binary:
            problems.append("post-build native executable digest missing from runtime environment")
        elif not expected_binary or observed_binary != expected_binary:
            problems.append("post-build native executable digest differs from frozen binary")
    if not frozen.get("image_id") or snapshot.get("container_image_id") != frozen["image_id"]:
        problems.append("pinned runtime image differs/not recorded")
    for key in ("rng_seed","rng_run"):
        if A.finite(stats.get(key,env.get("ns3_"+key))) != A.finite(case.get(key)):
            problems.append(f"effective {key} differs/not recorded")
    if stats.get("geometry_radio_mode") is None or stats.get("selected_propagation_profile") is None:
        problems.append("effective geometry propagation/mode not recorded in final native stats")
    elif stats.get("geometry_radio_mode") != case.get("radio_mode") or stats.get("selected_propagation_profile") != "geometry_logdistance":
        problems.append("effective geometry propagation/mode differs")
    if cfg.get("fault_profile") is None or stats.get("fault_profile") is None:
        problems.append("fault profile not recorded in final native stats or experiment config")
    elif cfg.get("fault_profile") != "none" or stats.get("fault_profile") != "none":
        problems.append("artificial fault present in geometric experiment")
    if A.finite(cfg.get("duration_s")) != A.finite(case.get("duration_s")):
        problems.append("measurement horizon differs from frozen case")
    record["recording_errors"] = list(result.get("data_quality_errors",[]))+list(result.get("missing_measurements",[]))
    if not common:
        record["recording_errors"].append("common command/authority export missing")
    window=result.get("observation_window",{})
    complete=window.get("complete") is True
    record["full_measurement_window"]=complete
    eligible=complete and not problems and not record["recording_errors"]
    record["primary_eligible"]=eligible
    rates=result.get("rates",{}).get("all_five",{})
    progress=result.get("mission_progress",{})
    planned=[A.finite(progress.get(str(i),{}).get("planned_waypoints")) for i in range(1,6)]
    completed=[A.finite(progress.get(str(i),{}).get("completed_waypoints")) for i in range(1,6)]
    mission_valid=(all(v is not None and v>0 for v in planned) and all(v is not None and v>=0 for v in completed)
                   and all(a<=b for a,b in zip(completed,planned)))
    if eligible:
        record["outcomes"].update(timely_per_offered=A.finite(rates.get("timely_per_offered")),
            completed_waypoint_fraction=sum(completed)/sum(planned) if mission_valid else None,
            all_routes_finished=float(all(progress[str(i)].get("route_finished") is True for i in range(1,6))) if mission_valid else None,
            new_relay_selections=A.finite(result.get("authority",{}).get("selections")),
            integral_RTF=A.finite(result.get("performance",{}).get("integral_RTF")))
    trigger=result.get("trigger",{}).get("event")
    trigger_host=U.stamp(trigger) if trigger else None
    uart=result.get("uart_after_NLOS",{}).get("all_five",{})
    valid=not problems and not record["recording_errors"]
    restricted=bounded_endpoint(uart,trigger_host,window.get("end_host_monotonic_ns"),complete=complete,recording_valid=valid)
    # Restricted endpoint can be known despite a later crash, but short crashed
    # observations never enter the fixed-horizon primary outcomes.
    record["restricted_post_NLOS"]=restricted
    record["outcomes"]["restricted_post_NLOS_UART_60s"]=restricted["value"] if valid else None
    record["post_NLOS_UART"]=interrupted_endpoint(uart,complete=complete,trigger=trigger_host,origin="CP_initial_relay_LOS_to_NLOS")
    record["post_NLOS_label"]="first update latency; impairment/recovery is not inferred from NLOS alone"
    record["authority_audit"]=result.get("authority",{})
    raw=Path(runs_root)/case["run_id"] if runs_root else Path(result.get("raw_run_dir", "/nonexistent"))
    if raw.is_dir():
        record["after_direct_probe_detection"]=after_detection(raw,export_dir,result,complete,valid)
    else:
        record["after_direct_probe_detection"]=dict(status="not_recorded_raw_unavailable")
    return record


def missing_bounds(pair_rows, metric):
    bounds=BOUNDS.get(metric)
    if bounds is None or not pair_rows:
        return None
    if any(r.get("not_applicable") for r in pair_rows):
        return dict(status="not_identified_due_nonapplicable_trigger",planned_pairs=len(pair_rows))
    if any(r.get("unknown_applicability") for r in pair_rows):
        return dict(status="not_identified_due_unknown_trigger_applicability",planned_pairs=len(pair_rows),
                    interpretation="no numerical range bounds without assuming that the unobserved trigger would occur")
    lo,hi=bounds
    low,high=[],[]
    for row in pair_rows:
        f,x=row.get("failover"),row.get("fixed")
        low.append((f if f is not None else lo)-(x if x is not None else hi))
        high.append((f if f is not None else hi)-(x if x is not None else lo))
    return dict(status="deterministic_range_bounds",planned_pairs=len(pair_rows),
        mean_difference_bounds=[statistics.fmean(low),statistics.fmean(high)],outcome_range=[lo,hi],
        interpretation="worst/best missing-outcome sensitivity, not a confidence interval or imputed observations")


def contrasts(records):
    groups=defaultdict(dict)
    for row in records:
        key=(row.get("cohort","main"),row.get("scenario_variant","nominal"),row.get("parameter_variant","nominal"),row["radio_mode"],row["block_id"])
        if row["policy"] in groups[key]:
            raise ValueError(f"duplicate policy/case within planned pair: {key}")
        groups[key][row["policy"]]=row
    pairs, summary_groups=[],defaultdict(list)
    for key, policies in groups.items():
        cohort,scenario,parameter,mode,block=key
        for metric in METRICS:
            f=policies.get("failover",{}).get("outcomes",{}).get(metric)
            x=policies.get("fixed",{}).get("outcomes",{}).get(metric)
            na=metric=="restricted_post_NLOS_UART_60s" and any(
                r.get("restricted_post_NLOS",{}).get("status")=="not_applicable_no_trigger" for r in policies.values())
            unknown=metric=="restricted_post_NLOS_UART_60s" and any(
                r.get("restricted_post_NLOS",{}).get("unknown_applicability") is True for r in policies.values())
            pair=dict(cohort=cohort,scenario_variant=scenario,parameter_variant=parameter,radio_mode=mode,block_id=block,
                metric=metric,fixed=x,failover=f,difference=f-x if f is not None and x is not None else None,
                complete=f is not None and x is not None,not_applicable=na,unknown_applicability=unknown,
                fixed_run_id=policies.get("fixed",{}).get("run_id"),failover_run_id=policies.get("failover",{}).get("run_id"))
            pairs.append(pair)
            summary_groups[key[:4]+(metric,)].append(pair)
    summaries=[]
    for key,rows in summary_groups.items():
        cohort,scenario,parameter,mode,metric=key
        values=[r["difference"] for r in rows if r["difference"] is not None]
        # n=3 parameter sweeps/n=1 boundaries stay descriptive, independent of
        # whether a bootstrap implementation can technically resample them.
        estimate=(interval(values) if cohort=="main" else
                  dict(n=len(values),mean=statistics.fmean(values) if values else None,
                       differences=values,interval_95=None,method="descriptive auxiliary cohort"))
        summaries.append(dict(cohort=cohort,scenario_variant=scenario,parameter_variant=parameter,radio_mode=mode,metric=metric,
            planned_pairs=len(rows),complete_pairs=len(values),missing_pair_ids=[r["block_id"] for r in rows if not r["complete"]],
            nonapplicable_pair_ids=[r["block_id"] for r in rows if r["not_applicable"]],
            unknown_applicability_pair_ids=[r["block_id"] for r in rows if r["unknown_applicability"]],
            failover_minus_fixed=estimate,missing_outcome_sensitivity=missing_bounds(rows,metric)))
    interactions=[]
    for metric in METRICS:
        blocks=defaultdict(dict)
        for row in pairs:
            if row["cohort"]=="main" and row["metric"]==metric:
                key=(row["scenario_variant"],row["parameter_variant"],row["block_id"])
                if row["radio_mode"] in blocks[key]:
                    raise ValueError(f"duplicate interaction cell: {key}/{row['radio_mode']}")
                blocks[key][row["radio_mode"]]=row
        variant_rows=defaultdict(list)
        for (scenario,parameter,block),cells in blocks.items():
            g,l=cells.get("geometry",{}).get("difference"),cells.get("los_only",{}).get("difference")
            limits=[]
            if metric in BOUNDS and not any(cell.get("not_applicable") or cell.get("unknown_applicability") for cell in cells.values()):
                lo,hi=BOUNDS[metric]
                for mode,policy,sign in [("geometry","failover",1),("geometry","fixed",-1),
                                          ("los_only","failover",-1),("los_only","fixed",1)]:
                    value=cells.get(mode,{}).get(policy)
                    limits.append((sign*value,sign*value) if value is not None else (lo,hi) if sign==1 else (-hi,-lo))
            variant_rows[(scenario,parameter)].append(dict(block_id=block,scenario_variant=scenario,parameter_variant=parameter,
                metric=metric,geometry_effect=g,LOS_only_effect=l,
                interaction=g-l if g is not None and l is not None else None,
                interaction_lower_bound=sum(v[0] for v in limits) if limits else None,
                interaction_upper_bound=sum(v[1] for v in limits) if limits else None))
        for (scenario,parameter),rows in variant_rows.items():
            values=[r["interaction"] for r in rows if r["interaction"] is not None]
            interactions.append(dict(metric=metric,scenario_variant=scenario,parameter_variant=parameter,
                planned_blocks=len(rows),complete_four_case_blocks=len(values),
                definition="(failover-fixed) geometry minus (failover-fixed) LOS-only, within independent seed block and common scenario/parameter variant",
                estimate=interval(values),block_rows=rows,
                missing_outcome_sensitivity=dict(status="deterministic_range_bounds_not_CI",
                    mean_interaction_bounds=[statistics.fmean(r[key] for r in rows) for key in
                        ("interaction_lower_bound","interaction_upper_bound")])
                    if rows and all(r["interaction_lower_bound"] is not None for r in rows) else None))
    return pairs,summaries,interactions


def aggregate(freeze_path,analysis_root,controllers_root,output,*,runs_root=None):
    freeze_path,analysis_root,controllers_root,output=map(Path,(freeze_path,analysis_root,controllers_root,output))
    frozen=A.read_json(freeze_path)
    cases=validate_frozen_blocks(frozen)
    records=[run_record(case,frozen,analysis_root/case["run_id"],controllers_root/(case["run_id"]+".json"),runs_root=runs_root) for case in cases]
    pairs,summaries,interactions=contrasts(records)
    planned={r["run_id"] for r in cases}
    extras=[]
    for path in sorted(controllers_root.glob("*.json")):
        row=A.read_json(path)
        if row.get("run_id") and row["run_id"] not in planned:
            extras.append(dict(run_id=row["run_id"],controller=str(path),returncode=row.get("returncode"),
                status="unplanned_attempt_not_pooled",controller_sha256=digest(path)))
    output.mkdir(parents=True,exist_ok=True)
    result=dict(analysis_version=3,freeze_path=str(freeze_path.resolve()),freeze_sha256=digest(freeze_path),
        analyzer_sha256=digest(__file__),planned_attempts=len(cases),observed_attempts=sum(r["attempted"] for r in records),
        failed_attempts=sum(r["attempt_failed"] for r in records),valid_full_window_attempts=sum(r["primary_eligible"] for r in records),
        recorded_status_counts=dict(Counter(r["recorded_status"] for r in records)),runs=records,
        paired_contrasts=summaries,policy_geometry_interactions=interactions,unplanned_attempts=extras,
        predeclared_analysis=dict(restricted_horizon_host_s=RESTRICTED_HORIZON_HOST_S,
            bootstrap_seed=BOOTSTRAP_SEED,bootstrap_draws_when_n_gt5=BOOTSTRAP_DRAWS,
            unit="independent seed block/paired run, no packet/UAV/episode pseudoreplication",
            primary="fixed-horizon timely/offered and configured-waypoint progress; whole attempt failure also reported",
            restricted_endpoint="optional bounded first-post-NLOS UART latency, never recovery inferred from geometry alone",
            auxiliary="sensitivity n3 and boundary n1 remain descriptive and unpooled",
            missingness="planned failures remain rows; no replacement. Complete-pair intervals conditional on observed data; explicit worst/best range bounds accompany bounded metrics"))
    A.write_json(output/"urban_study.json",result)
    flat=[dict({k:v for k,v in r.items() if k not in ("outcomes","authority_audit")},**r["outcomes"],
               provenance_errors=";".join(r["provenance_errors"]),recording_errors=";".join(r["recording_errors"])) for r in records]
    flat=[{key:json.dumps(value,sort_keys=True,allow_nan=False) if isinstance(value,(dict,list)) else value
           for key,value in row.items()} for row in flat]
    U.write_rows(output/"runs.csv",flat)
    U.write_rows(output/"pairs.csv",pairs)
    U.write_rows(output/"interaction_blocks.csv",[r for i in interactions for r in i["block_rows"]])
    U.write_rows(output/"unplanned_attempts.csv",extras,["run_id","controller","returncode","status","controller_sha256"])
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--freeze",type=Path,required=True)
    p.add_argument("--analysis-root",type=Path,required=True)
    p.add_argument("--controllers-root",type=Path,required=True)
    p.add_argument("--runs-root",type=Path)
    p.add_argument("--output",type=Path,required=True)
    args=p.parse_args()
    r=aggregate(args.freeze,args.analysis_root,args.controllers_root,args.output,runs_root=args.runs_root)
    print(json.dumps({k:r[k] for k in ("planned_attempts","observed_attempts","failed_attempts","valid_full_window_attempts")}))


if __name__=="__main__":
    main()
