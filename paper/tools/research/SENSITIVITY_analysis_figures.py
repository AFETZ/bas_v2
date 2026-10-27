#!/usr/bin/env python3
"""Render the existing 18-run analysis without rerunning or pooling experiments.

This ignored research companion consumes relay_sensitivity.py output. It does
not create missing measurements, alter eligibility, or estimate new intervals.
"""
from __future__ import annotations
import argparse
from collections import Counter
import csv
import json
import math
from pathlib import Path
import statistics

BASE=Path(__file__).resolve().parent
DURATIONS=(.5,2.,10.)
POLICIES=("fixed","failover")


def finite(value):
    try:
        value=float(value)
        return value if math.isfinite(value) else None
    except (ValueError,TypeError):return None


def write_csv(path,rows,fields=None):
    fields=fields or list(dict.fromkeys(k for row in rows for k in row))
    with path.open("w",newline="") as stream:
        writer=csv.DictWriter(stream,fieldnames=fields,extrasaction="ignore")
        writer.writeheader();writer.writerows(rows)


def mean(values):
    values=[finite(v) for v in values if finite(v) is not None]
    return statistics.fmean(values) if values else None


def summary_cells(records):
    cells=[]
    for duration in DURATIONS:
        for policy in POLICIES:
            rows=[r for r in records if float(r["outage_duration_s"])==duration and r["policy"]==policy]
            eligible=[r for r in rows if r.get("network_eligible")]
            outcomes=lambda key:[r.get("outcomes",{}).get(key) for r in eligible]
            exposures=[finite(r.get("fault_phase_and_selections",{}).get("actual_RF_duration_s")) for r in rows]
            exposures=[value for value in exposures if value is not None]
            cell=dict(outage_duration_s=duration,policy=policy,planned_runs=3,
                factor_definition="nominal planned RF duration; actual exposure is measured separately",
                recorded_runs=sum(r.get("recorded",False) for r in rows),
                completed_runs=sum(r.get("run_status")=="completed" for r in rows),
                failed_runs=sum(r.get("run_status")=="failed" for r in rows),
                missing_or_incomplete_runs=sum(r.get("run_status") not in ("completed","failed") for r in rows),
                network_eligible_runs=len(eligible),
                network_eligible_failed_runs=sum(r.get("run_status")=="failed" for r in eligible),
                measurement_window_complete_runs=sum(r.get("measurement_window",{}).get("complete") is True for r in rows),
                measurement_window_incomplete_runs=sum(r.get("measurement_window",{}).get("complete") is False for r in rows),
                measurement_window_unknown_runs=sum(r.get("measurement_window",{}).get("complete") is None for r in rows),
                lifecycle_success_runs=sum(r.get("outcomes",{}).get("lifecycle_success")==1 for r in rows),
                lifecycle_failed_runs=sum(r.get("outcomes",{}).get("lifecycle_success")==0 for r in rows),
                lifecycle_unknown_runs=sum(r.get("outcomes",{}).get("lifecycle_success") is None for r in rows),
                timely_per_offered_mean=mean(outcomes("timely_per_offered")),
                offered_availability_mean=mean(outcomes("offered_availability")),
                actual_RF_duration_s_observed_runs=len(exposures),
                actual_RF_duration_s_mean=mean(exposures),
                actual_RF_duration_s_min=min(exposures) if exposures else None,
                actual_RF_duration_s_max=max(exposures) if exposures else None,
                actual_minus_nominal_RF_duration_s_mean=mean([value-duration for value in exposures]))
            for stage in ("before","after"):
                reports=[r.get("parameter_capture",{}).get(stage,{}) for r in rows]
                cell[f"parameter_{stage}_complete_runs"]=sum(r.get("all_five_tables_complete") is True for r in reports)
                cell[f"parameter_{stage}_incomplete_runs"]=sum(r.get("all_five_tables_complete") is False and r.get("status")!="skipped" for r in reports)
                cell[f"parameter_{stage}_skipped_runs"]=sum(r.get("status")=="skipped" for r in reports)
                cell[f"parameter_{stage}_unknown_runs"]=sum(r.get("all_five_tables_complete") is None and r.get("status")!="skipped" for r in reports)
            for endpoint,metric in (("common_UART","common_UART_recovery_s"),("handover","handover_by4s_where_changed"),("physical_goal","physical_new_goal_recovery_s")):
                statuses=Counter(r.get("endpoint_status",{}).get(endpoint,{}).get("status","not_recorded") for r in rows)
                cell[endpoint+"_observed_count"]=statuses["observed"]
                cell[endpoint+"_right_censored_count"]=statuses["right_censored"]
                cell[endpoint+"_other_or_missing_count"]=sum(v for k,v in statuses.items() if k not in ("observed","right_censored"))
                cell[endpoint+"_mean_observed_eligible_recovery_s"]=mean([
                    r.get("endpoint_status",{}).get(endpoint,{}).get("time_s") for r in eligible
                    if r.get("endpoint_status",{}).get(endpoint,{}).get("status")=="observed"])
            for metric in ("common_UART_by4s","common_UART_over4_or_not_recovered","handover_changed","handover_by4s_where_changed",
                    "new_relay_selections","selected_after_RFend","activated_after_RFend",
                    "selected_after_confirmed_old_channel","activated_after_confirmed_old_channel"):
                values=[v for v in outcomes(metric) if v is not None]
                cell[metric+"_sum"]=sum(values) if values else None
                cell[metric+"_observed_runs"]=len(values)
            cells.append(cell)
    return cells


def paired_exposure(records):
    """Retain each nominal pair's observed exposure difference, without adjusting it."""
    rows=[]
    for pair_id in sorted({r["pair_id"] for r in records}):
        pair={r["policy"]:r for r in records if r["pair_id"]==pair_id}
        example=next(iter(pair.values()))
        row=dict(pair_id=pair_id,repeat=example["repeat"],nominal_RF_duration_s=float(example["outage_duration_s"]))
        for policy in POLICIES:
            record=pair.get(policy,{})
            behavior=record.get("fault_phase_and_selections",{})
            row[policy+"_run_id"]=record.get("run_id")
            row[policy+"_run_status"]=record.get("run_status")
            for field in ("actual_RF_duration_s","fault_host_ns","fault_end_host_ns",
                    "native_fault_ns3_time_s","native_fault_end_ns3_time_s"):
                row[policy+"_"+field]=finite(behavior.get(field))
            row[policy+"_phase_ages_at_fault_s"]=json.dumps(behavior.get("ages_at_fault_s"))
            duration=row[policy+"_actual_RF_duration_s"]
            row[policy+"_actual_minus_nominal_s"]=duration-row["nominal_RF_duration_s"] if duration is not None else None
        fixed,failover=(row[policy+"_actual_RF_duration_s"] for policy in POLICIES)
        row["actual_duration_failover_minus_fixed_s"]=failover-fixed if fixed is not None and failover is not None else None
        row["absolute_paired_actual_duration_difference_s"]=abs(failover-fixed) if fixed is not None and failover is not None else None
        rows.append(row)
    return rows


def exposure_summary(pairs):
    rows=[]
    for duration in DURATIONS:
        chosen=[r for r in pairs if r["nominal_RF_duration_s"]==duration]
        absolute=[r["absolute_paired_actual_duration_difference_s"] for r in chosen
            if r["absolute_paired_actual_duration_difference_s"] is not None]
        rows.append(dict(nominal_RF_duration_s=duration,planned_pairs=3,observed_exposure_pairs=len(absolute),
            missing_exposure_pairs=3-len(absolute),absolute_paired_actual_duration_difference_s_min=min(absolute) if absolute else None,
            absolute_paired_actual_duration_difference_s_max=max(absolute) if absolute else None,
            absolute_paired_actual_duration_difference_s_range=max(absolute)-min(absolute) if absolute else None))
    return rows


def render(records,output):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    colors=plt.get_cmap("tab10")
    fig,axes=plt.subplots(3,3,figsize=(13,10),squeeze=False,constrained_layout=True)
    panels=(("timely_per_offered",None,"Timely / all offered (%)",100),
        ("common_UART_recovery_s","common_UART","Common UART recovery (s)",1),
        ("physical_new_goal_recovery_s","physical_goal","New physical goal recovery (s)",1))
    for column,duration in enumerate(DURATIONS):
        rows=[r for r in records if float(r["outage_duration_s"])==duration]
        lookup={(int(r["repeat"]),r["policy"]):r for r in rows}
        for row_index,(metric,endpoint,ylabel,scale) in enumerate(panels):
            ax=axes[row_index,column]
            missing=0;censored=0;invalid=0
            for repeat in (1,2,3):
                points=[]
                for index,policy in enumerate(POLICIES):
                    record=lookup.get((repeat,policy),{})
                    value=finite(record.get("outcomes",{}).get(metric))
                    status=record.get("endpoint_status",{}).get(endpoint,{}) if endpoint else {}
                    eligible=record.get("network_eligible") is True
                    horizontal=index+(repeat-2)*.035
                    if value is not None:
                        points.append((horizontal,value*scale,eligible))
                        marker="X" if record.get("run_status")=="failed" else "o"
                        ax.scatter(horizontal,value*scale,color=colors(repeat-1) if eligible else "tab:orange",
                            marker=marker,facecolors=colors(repeat-1) if eligible else "none",s=45 if marker=="X" else 35,zorder=3)
                        invalid+=not eligible
                    elif status.get("status")=="right_censored" and finite(status.get("censor_time_s")) is not None:
                        bound=float(status["censor_time_s"])*scale
                        ax.scatter(horizontal,bound,marker="^",color=colors(repeat-1),s=45,zorder=3)
                        ax.annotate("≥",(horizontal,bound),xytext=(3,3),textcoords="offset points",fontsize=9)
                        censored+=1
                    else:missing+=1
                if len(points)==2 and all(point[2] for point in points):
                    ax.plot([p[0] for p in points],[p[1] for p in points],color=colors(repeat-1),alpha=.6,lw=1)
            if row_index==0:
                ax.set_title(f"Nominal selective RF outage: {duration:g} s; 3 pairs")
                ax.set_ylim(0,101)
            else:ax.set_ylim(bottom=0)
            if row_index==1:
                ax.axhline(4,color="gray",lw=.8,ls=":")
            ax.set_xticks((0,1),POLICIES)
            ax.set_xlim(-.3,1.3)
            ax.set_ylabel(ylabel)
            ax.grid(axis="y",alpha=.2)
            ax.text(.02,.98,f"missing={missing}; censored={censored}; noneligible values={invalid}; failed runs={sum(r.get('run_status')=='failed' for r in rows)}",
                transform=ax.transAxes,va="top",fontsize=7)
    legend=[Line2D([],[],marker="o",color=colors(i),label=f"paired repeat {i+1}") for i in range(3)]
    legend += [Line2D([],[],marker="^",ls="",color="black",label="censor bound ≥, not a recovery"),
        Line2D([],[],marker="X",ls="",color="black",label="overall failed run; network eligibility separate"),
        Line2D([],[],marker="o",markerfacecolor="none",ls="",color="tab:orange",label="noneligible recorded value")]
    fig.legend(handles=legend,loc="lower center",bbox_to_anchor=(.5,-.04),ncol=3,fontsize=8)
    fig.suptitle("Separate sensitivity study: every planned whole-run pair; no old-30 pooling",fontsize=13)
    for ext in ("png","svg","pdf"):fig.savefig(output/f"sensitivity-paired-points.{ext}",dpi=180,bbox_inches="tight")
    plt.close(fig)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis",type=Path,required=True,help="Existing relay_sensitivity.py analysis directory")
    parser.add_argument("--output",type=Path,default=BASE/"sensitivity-figures")
    parser.add_argument("--no-plots",action="store_true")
    args=parser.parse_args()
    source=args.analysis/"metrics/sensitivity_analysis.json"
    data=json.loads(source.read_text())
    if data.get("study_id")!="relay_sensitivity_18_v1" or data.get("planned_runs")!=18 or data.get("main30_not_pooled") is not True:
        raise SystemExit("Not the separate predeclared 18-run sensitivity analysis")
    records=data["records"]
    if len(records)!=18 or len({r["run_id"] for r in records})!=18:
        raise SystemExit("Expected all 18 planned records including missing/failed rows")
    args.output.mkdir(parents=True,exist_ok=True)
    cells=summary_cells(records)
    exposure_pairs=paired_exposure(records)
    exposure_by_duration=exposure_summary(exposure_pairs)
    write_csv(args.output/"six-cells.csv",cells)
    write_csv(args.output/"paired-actual-RF-exposure.csv",exposure_pairs)
    write_csv(args.output/"paired-actual-RF-exposure-summary.csv",exposure_by_duration)
    write_csv(args.output/"all-paired-points.csv",data["paired_points"])
    write_csv(args.output/"paired-estimates-from-existing-analysis.csv",data["paired_estimates"])
    rows=[]
    for record in records:
        behavior=record.get("fault_phase_and_selections",{})
        row={k:record.get(k) for k in ("order","run_id","pair_id","repeat","policy","outage_duration_s","run_status","run_error","recorded","study_eligible","network_eligible")}
        row.update(record.get("outcomes",{}))
        row["measurement_window_complete"]=record.get("measurement_window",{}).get("complete")
        for stage in ("before","after"):
            for field in ("status","all_five_tables_complete","observed_component_count","complete_component_count","process_returncode","overlaps_measurement_host_interval"):
                row[f"parameter_{stage}_{field}"]=record.get("parameter_capture",{}).get(stage,{}).get(field)
        row.update(exclusion_reasons=json.dumps(record.get("exclusion_reasons",[])),
            actual_RF_duration_s=behavior.get("actual_RF_duration_s"),
            actual_RF_start_host_ns=behavior.get("fault_host_ns"),actual_RF_end_host_ns=behavior.get("fault_end_host_ns"),
            actual_RF_start_ns3_time_s=behavior.get("native_fault_ns3_time_s"),actual_RF_end_ns3_time_s=behavior.get("native_fault_end_ns3_time_s"),
            actual_minus_nominal_RF_duration_s=finite(behavior.get("actual_RF_duration_s"))-float(record["outage_duration_s"]) if finite(behavior.get("actual_RF_duration_s")) is not None else None,
            phase_clock=behavior.get("host_clock"),phase_ages_at_fault_s=json.dumps(behavior.get("ages_at_fault_s")),
            endpoint_status=json.dumps(record.get("endpoint_status",{})))
        rows.append(row)
    write_csv(args.output/"all-18-run-points.csv",rows)
    result=dict(source_analysis=str(source.resolve()),study_id=data["study_id"],planned_runs=18,
        common_configuration_signatures=data.get("common_configuration_signatures"),six_cells=cells,
        paired_actual_exposure=exposure_pairs,
        paired_actual_exposure_summary=exposure_by_duration,
        newly_estimated_intervals=False,old30_pooled=False,
        methods=["Six descriptive cells, three planned whole-run pairs per duration. Every planned run and pair is retained.",
            "The factor is nominal planned outage duration. Actual native host bounds, per-cell mean/min/max and every paired exposure mismatch are retained; actual durations are not rounded or forced to the nominal factor.",
            "Paired policy contrasts at nominal settings include actual-exposure and heartbeat/lease/command-phase variation. No post hoc exposure adjustment, phase selection or run retuning is performed.",
            "Means use network-eligible nonmissing run-level outcomes with equal run weights. Censored recovery times are not replaced by their bounds.",
            "Overall failed runs remain in all planned records and completion/failure denominators; network means require eligible windows. A failed run with a complete independently valid network window can remain network-eligible; it is plotted as X and is not excluded because a >4 s recovery preceded a later failure.",
            "Run completion, five-UAV lifecycle, measurement completeness and before/after parameter-table completeness are reported separately. Missing/skipped capture tables are not zero complete components.",
            "Observed/censored/missing status counts include every planned run, independently of eligibility; their denominators differ explicitly from conditional means.",
            "Primary UART recovery can retain the old relay. New-relay <=4s is a separate conditional endpoint where relay change was observed.",
            "Post-RFend or post-confirmed-old-channel selections/activations are potentially unnecessary-change counts, not proof of no benefit.",
            "Confidence intervals are copied from existing analysis, never recomputed or pooled across outage factors. Three-pair intervals are exploratory.",
            "An open orange value is recorded but noneligible; lines connect only observed eligible members of the same paired repeat.",
            "Censor bounds are upward triangles with >= labels, not numerical successful recoveries. Missing values have no plotted zero."])
    (args.output/"figure-method.json").write_text(json.dumps(result,indent=2,allow_nan=False)+"\n")
    lines=["| Nominal RF outage (s) | Policy | Actual RF min..max (s) | Complete / failed / other | Network eligible (failed included) | Timely / offered (%) | UART observed / censored | UART mean observed (s) | Physical observed / censored | Physical mean observed (s) |",
        "|---:|:---|:---:|---:|---:|---:|:---:|---:|:---:|---:|"]
    fmt=lambda v:"N/A" if v is None else f"{v:.4f}"
    for c in cells:
        timely=c["timely_per_offered_mean"]
        lines.append(f"| {c['outage_duration_s']:g} | {c['policy']} | {fmt(c['actual_RF_duration_s_min'])}..{fmt(c['actual_RF_duration_s_max'])} | {c['completed_runs']} / {c['failed_runs']} / {c['missing_or_incomplete_runs']} | {c['network_eligible_runs']} ({c['network_eligible_failed_runs']}) | {fmt(timely*100 if timely is not None else None)} | {c['common_UART_observed_count']} / {c['common_UART_right_censored_count']} | {fmt(c['common_UART_mean_observed_eligible_recovery_s'])} | {c['physical_goal_observed_count']} / {c['physical_goal_right_censored_count']} | {fmt(c['physical_goal_mean_observed_eligible_recovery_s'])} |")
    lines += ["", "All counts below use the three planned runs per cell. Capture completeness is the recorded exporter verdict; independent wire/index audit remains separate.", "",
        "| Nominal outage (s) | Policy | Full measurement windows / 3 | Lifecycle successes / 3 | Pre complete / incomplete / skipped / unknown | Post complete / incomplete / skipped / unknown |",
        "|---:|:---|---:|---:|:---:|:---:|"]
    for c in cells:
        counts=lambda stage:" / ".join(str(c[f"parameter_{stage}_{label}_runs"]) for label in ("complete","incomplete","skipped","unknown"))
        lines.append(f"| {c['outage_duration_s']:g} | {c['policy']} | {c['measurement_window_complete_runs']} / 3 | {c['lifecycle_success_runs']} / 3 | {counts('before')} | {counts('after')} |")
    failed=[r for r in records if r.get("run_status")=="failed"]
    lines += ["",f"Overall failed runs retained: {len(failed)} / 18. Their valid network observations are assessed by network_eligible, independently of completion.",""]
    for r in failed:
        lines.append(f"- `{r['run_id']}`: measurement_complete={r.get('measurement_window',{}).get('complete')}; network_eligible={r.get('network_eligible')}; lifecycle_success={r.get('outcomes',{}).get('lifecycle_success')}; error={r.get('run_error')}")
    lines += ["", "All planned rows remain in all-18-run-points.csv and all-paired-points.csv. Conditional means do not impute censored or missing runs. Counts and mean denominators are explicit in six-cells.csv.",
        "", "Outage factors are nominal planned durations. Native RF switching is observed, and polling can give unequal actual exposure within a nominal pair. Per-cell actual means/min/max and every actual failover-minus-fixed mismatch are retained in six-cells.csv and paired-actual-RF-exposure.csv; raw actual phases remain in the analysis. These three-pair comparisons include timing/phase variation and are exploratory, with no post hoc exposure adjustment or retuning.",
        "", "Caption: Prespecified selective-fault sensitivity study, separate from the original 30-run experiment. Columns label nominal planned outages; actual per-run exposures and paired mismatches are reported separately. Each line joins the same whole-run random repeat under fixed and failover policies; colors identify three paired repeats per nominal duration. Upward triangles show right-censor bounds; unobserved outcomes have no plotted zero. The 4 s reference concerns command receipt/common UART; changed-relay handover is audited separately. Physical goal completion has no 4 s success threshold. No curve smoothing, packet-level replication, cross-duration pooling, or equivalence claim is used."]
    (args.output/"table-and-caption.md").write_text("\n".join(lines)+"\n")
    if not args.no_plots:render(records,args.output)
    print(json.dumps(dict(planned_runs=18,recorded_runs=sum(bool(r.get("recorded")) for r in records),output=str(args.output))))


if __name__=="__main__":main()
