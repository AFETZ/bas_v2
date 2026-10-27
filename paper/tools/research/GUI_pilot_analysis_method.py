#!/usr/bin/env python3
"""One completed technical GUI pilot; research artifact, never launches BAS.

Run after the controller exits, in the pinned image with Matplotlib. All writes
go to --output; source raw and the frozen production implementation stay intact.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import importlib.util
import json
import math
from pathlib import Path
import statistics

ROOT = next(p for p in Path(__file__).resolve().parents if (p/"scripts/product/analyze_relay_experiment.py").is_file())
BASE = Path(__file__).resolve().parent
UAVS = tuple(f"uav{i}" for i in range(1, 6))


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def json_file(path):
    return json.loads(path.read_text()) if path.is_file() else {}


def json_lines(path):
    if path.is_file():
        with path.open() as stream:
            for line in stream:
                if line.strip():
                    yield json.loads(line)


def csv_rows(path):
    if not path.is_file():
        return []
    with path.open(newline="") as stream:
        return list(csv.DictReader(stream))


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")


def write_csv(path, rows, fields=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = fields or list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def distribution(values):
    values = sorted(v for v in values if v is not None and math.isfinite(v))
    def quantile(p):
        if not values:
            return None
        at = (len(values)-1)*p
        low, high = math.floor(at), math.ceil(at)
        return values[low]+(values[high]-values[low])*(at-low)
    return dict(n=len(values), mean=statistics.fmean(values) if values else None,
        p5=quantile(.05), p50=quantile(.5), p95=quantile(.95),
        min=min(values) if values else None, max=max(values) if values else None)


def clock_analysis(clocks, start, end):
    """Same observed >=1 s window convention as the existing live exporter.

    Restart each window inside the explicitly selected interval. Do not split
    on a long wall gap: a pause must remain in RTF, not disappear as warmup.
    Clock resets form separate segments and make a single integral unavailable.
    """
    selected = [r for r in clocks if number(r.get("host_monotonic_ns")) is not None
        and number(r.get("source_sim_time_s")) is not None
        and start <= float(r["host_monotonic_ns"]) <= end]
    segments = []
    for row in selected:
        previous = segments[-1][-1] if segments else None
        if (previous is None or row["session_id"] != previous["session_id"]
                or float(row["host_monotonic_ns"]) <= float(previous["host_monotonic_ns"])
                or float(row["source_sim_time_s"]) < float(previous["source_sim_time_s"])):
            segments.append([])
        segments[-1].append(row)
    windows, summaries, gaps = [], [], []
    for segment_id, segment in enumerate(segments):
        anchor = segment[0]
        for previous, row in zip(segment, segment[1:]):
            gap = (float(row["host_monotonic_ns"])-float(previous["host_monotonic_ns"]))/1e9
            if gap > .5:
                gaps.append(dict(start_host_ns=float(previous["host_monotonic_ns"]),
                    end_host_ns=float(row["host_monotonic_ns"]), wall_gap_s=gap))
            wall = (float(row["host_monotonic_ns"])-float(anchor["host_monotonic_ns"]))/1e9
            if wall >= 1:
                model = float(row["source_sim_time_s"])-float(anchor["source_sim_time_s"])
                windows.append(dict(segment=segment_id, start_host_ns=float(anchor["host_monotonic_ns"]),
                    end_host_ns=float(row["host_monotonic_ns"]), wall_duration_s=wall,
                    model_duration_s=model, end_to_end_rtf=model/wall))
                anchor = row
        first, last = segment[0], segment[-1]
        wall = (float(last["host_monotonic_ns"])-float(first["host_monotonic_ns"]))/1e9
        model = float(last["source_sim_time_s"])-float(first["source_sim_time_s"])
        summaries.append(dict(segment=segment_id, samples=len(segment),
            first_host_ns=float(first["host_monotonic_ns"]), last_host_ns=float(last["host_monotonic_ns"]),
            first_source_sim_time_s=float(first["source_sim_time_s"]),
            last_source_sim_time_s=float(last["source_sim_time_s"]), wall_duration_s=wall,
            model_duration_s=model, integral_rtf=model/wall if wall > 0 else None,
            unused_window_tail_wall_s=(float(last["host_monotonic_ns"])-float(anchor["host_monotonic_ns"]))/1e9))
    return dict(status="observed" if len(selected)>1 else "unavailable_insufficient_clock_samples",
        requested_host_bounds_ns=[start,end], requested_wall_duration_s=(end-start)/1e9,
        first_sample_offset_s=(float(selected[0]["host_monotonic_ns"])-start)/1e9 if selected else None,
        last_sample_offset_to_bound_s=(end-float(selected[-1]["host_monotonic_ns"]))/1e9 if selected else None,
        clock_segments=summaries, reset_count=max(0,len(segments)-1),
        integral_rtf=summaries[0]["integral_rtf"] if len(summaries)==1 else None,
        observed_window_rtf=distribution([r["end_to_end_rtf"] for r in windows]),
        window_wall_duration_s=distribution([r["wall_duration_s"] for r in windows]),
        fresh_uavs_counts=dict(Counter(str(r.get("fresh_uavs")) for r in selected)),
        wall_clock_gaps_over_500ms=gaps), windows


def docker_pids(path):
    rows = []
    if path.is_file():
        for line in path.read_text().splitlines():
            fields = line.split(None, 2)
            if len(fields)==3 and fields[0].isdigit() and fields[1].isdigit():
                rows.append(dict(pid=int(fields[0]), ppid=int(fields[1]), command=fields[2]))
    return rows


def viewer_resources(path, expected, start, end):
    expected_ids = {r["pid"] for r in expected}
    by_time = defaultdict(list)
    seen = set()
    for row in json_lines(path):
        stamp = number(row.get("monotonic_ns"))
        if stamp is not None and start <= stamp <= end and row.get("pid") in expected_ids:
            by_time[stamp].append(row)
            seen.add(row["pid"])
    samples = []
    for stamp, group in sorted(by_time.items()):
        cpu = [number(r.get("cpu_percent_one_core")) for r in group]
        rss = [number(r.get("rss_bytes")) for r in group]
        samples.append(dict(host_monotonic_ns=stamp, observed_pid_count=len(group),
            scope="matched viewer host PIDs present in docker top snapshot",
            cpu_percent_one_core=sum(cpu) if all(v is not None for v in cpu) else None,
            sum_process_rss_bytes=sum(rss) if all(v is not None for v in rss) else None,
            complete_expected_pid_coverage={r["pid"] for r in group}==expected_ids,
            graphics_gpu_memory_bytes=None))
    return dict(status="observed_scoped_processes" if samples else "unavailable_no_matching_host_PID_records",
        expected_docker_top_processes=expected, observed_host_pids=sorted(seen),
        missing_expected_host_pids=sorted(expected_ids-seen),
        process_coverage="complete for static top PID set" if expected_ids and seen==expected_ids else "partial or unavailable",
        sample_count=len(samples), cpu_percent_one_core=distribution([r["cpu_percent_one_core"] for r in samples]),
        sum_process_rss_bytes=distribution([r["sum_process_rss_bytes"] for r in samples]),
        host_sample_gap_s=distribution([(b["host_monotonic_ns"]-a["host_monotonic_ns"])/1e9 for a,b in zip(samples,samples[1:])]),
        boundary_unobserved_s=[(samples[0]["host_monotonic_ns"]-start)/1e9,(end-samples[-1]["host_monotonic_ns"])/1e9] if samples else None,
        graphics_gpu_memory_bytes=None,
        graphics_memory_status="unavailable: collector queries nvidia-smi compute-apps, which does not measure graphics allocation",
        collector_scope="Only monitor-matched processes intersecting viewer docker top host PIDs. Later descendants and other GUI processes may be unobserved.",
        rss_limit="Sum of per-process RSS may count shared pages repeatedly; CPU 100% means one logical core."), samples


def extract_targets(live_metrics, commands_path):
    commands = {(str(r["uav_id"]),r["command_id"]):r for r in csv_rows(commands_path)}
    targets, missing = [], 0
    for row in csv_rows(live_metrics/"uav_admitted_targets.csv"):
        command = commands.get((row["uav"].removeprefix("uav"),row["command_id"]))
        admitted = number(command.get("admitted_ns")) if command else None
        if admitted is None:
            missing += 1
            continue
        targets.append(dict(row, logger_event_host_monotonic_ns=row["host_monotonic_ns"],
            host_monotonic_ns=admitted, host_time_source="actual UART write end from relay_commands.admitted_ns",
            lease_status=command.get("lease_status"), lineage_status=command.get("lineage_status")))
    return targets, missing


def render(output, trajectories, targets, windows, lag, viewer, server, intervals, measurement_start,
           *, origin_label="scenario measurement start", observation_status="complete_actual_GUI_interval"):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    colors = plt.get_cmap("tab10")
    def x(stamp):
        return (float(stamp)-measurement_start)/1e9
    def mark(ax):
        for name,bounds in intervals.items():
            ax.axvspan(x(bounds[0]),x(bounds[1]),color="tab:blue" if name=="GUI" else "tab:gray",alpha=.08,
                label=name+" observed interval")
        ax.grid(alpha=.2)
    def gap_plot_values(rows,stamp_key,value_key,scale=1,threshold_s=2.5):
        xx,yy,previous=[],[],None
        for row in sorted(rows,key=lambda r:float(r[stamp_key])):
            at=float(row[stamp_key])
            if previous is not None and (at-previous)/1e9>threshold_s:
                xx.append(float("nan"));yy.append(float("nan"))
            xx.append(x(at))
            value=number(row.get(value_key))
            yy.append(value/scale if value is not None else float("nan"))
            previous=at
        return xx,yy
    fig, axes = plt.subplots(3,1,figsize=(12,10),sharex=True,constrained_layout=True)
    for index,uav in enumerate(UAVS):
        rows = sorted((r for r in trajectories if r["uav"]==uav),key=lambda r:float(r["host_monotonic_ns"]))
        xx, zz, previous = [], [], None
        for row in rows:
            at, z = number(row.get("host_monotonic_ns")), number(row.get("z_m"))
            if at is None:
                continue
            if previous is not None and (at-previous)/1e9 > .5:
                xx.append(float("nan")); zz.append(float("nan"))
            xx.append(x(at)); zz.append(z if z is not None and row.get("quality")=="observed"
                and row.get("altitude_datum")=="world_ENU_z" else float("nan"))
            previous = at
        axes[0].plot(xx,zz,color=colors(index),lw=.9,label=uav+" actual")
        points = [r for r in targets if r["uav"]==uav and r.get("altitude_datum")=="world_ENU_z"]
        axes[0].scatter([x(r["host_monotonic_ns"]) for r in points],[float(r["z_m"]) for r in points],
            s=14,marker="x",alpha=.6,color=colors(index))
    axes[0].set_ylabel("World ENU z (m)")
    axes[0].legend(ncol=5,fontsize=8,loc="lower right")
    axes[0].set_title("Actual heights; crosses are target markers at actual UART write end")
    for name,rows in windows.items():
        axes[1].plot([x(r["end_host_ns"]) for r in rows],[r["end_to_end_rtf"] for r in rows],".-",ms=3,label=name)
    axes[1].axhline(1,color="black",lw=.6,ls=":")
    axes[1].set_ylabel("Model Δ / host Δ")
    axes[1].set_title("Non-overlapping observed windows ≥1 s, restarted inside each interval")
    axes[1].legend(fontsize=8)
    axes[2].plot([x(r["host_monotonic_ns"]) for r in lag],[float(r["scheduler_lag_ms"]) for r in lag],lw=.7)
    axes[2].set_ylabel("Native ns-3 lag (ms)")
    axes[2].set_xlabel("Host seconds since "+origin_label)
    for ax in axes:
        mark(ax)
    fig.suptitle("Technical GUI pilot: "+observation_status+"; no causal GUI/headless estimate",fontsize=11)
    for ext in ("png","svg","pdf"):
        fig.savefig(output/f"GUI-timing-heights.{ext}",dpi=180)
    plt.close(fig)
    fig, axes = plt.subplots(2,1,figsize=(12,7),sharex=True,constrained_layout=True)
    for component in sorted({r["component"] for r in server}):
        rows = [r for r in server if r["component"]==component]
        for ax,key,scale in ((axes[0],"sum_cpu_percent_one_core",1),(axes[1],"sum_process_rss_bytes",1024**2)):
            xx,yy=gap_plot_values(rows,"host_monotonic_ns",key,scale)
            ax.plot(xx,yy,lw=.8,label="server / "+component)
    for ax,key,scale in ((axes[0],"cpu_percent_one_core",1),(axes[1],"sum_process_rss_bytes",1024**2)):
        xx,yy=gap_plot_values(viewer,"host_monotonic_ns",key,scale)
        ax.plot(xx,yy,"k.-",lw=1,ms=3,label="viewer / matched host PID scope")
        mark(ax); ax.legend(fontsize=8,ncol=2)
    axes[0].set_ylabel("CPU % of one core")
    axes[1].set_ylabel("Sum process RSS (MiB)")
    axes[1].set_xlabel("Host seconds since "+origin_label)
    fig.suptitle("Scoped process resources; graphics GPU memory unavailable; RAM capture spool outside RSS",fontsize=11)
    for ext in ("png","svg","pdf"):
        fig.savefig(output/f"GUI-resources.{ext}",dpi=180)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw",type=Path,default=ROOT/"runs/native-radio-realtime/paper-hook-gui-pilot-01")
    parser.add_argument("--controller-dir",type=Path,default=BASE/"hook-gui-pilot")
    parser.add_argument("--output",type=Path,default=BASE/"gui-analysis")
    parser.add_argument("--viewer-processes",type=Path,help="Actual docker-top PID snapshot for this viewer")
    parser.add_argument("--reuse-export",type=Path,help="Existing completed analyze_run output, containing live/metrics")
    parser.add_argument("--censor-host-monotonic-ns",type=int,
        help="Explicit actual native fail-closed boundary for an incomplete GUI interval; must match raw native CSV")
    parser.add_argument("--censor-reason",help="Declared reason for terminating observation, not a causal explanation of the failure")
    parser.add_argument("--gui-only-observation",action="store_true",
        help="Use actual complete GUI start/end events without claiming an experiment/flight measurement window")
    parser.add_argument("--no-plots",action="store_true")
    args = parser.parse_args()
    controller = json_file(args.controller_dir/"summary.json")
    if not controller:
        raise SystemExit("Controller summary absent: wait for pilot completion; no partial raw analysis performed")
    events = list(json_lines(args.controller_dir/"controller.jsonl"))
    first = lambda name: next((r for r in events if r.get("event")==name),{})
    measurement = json_file(args.raw/"metrics/relay_experiment_summary.json")
    measurement_start = number(measurement.get("measurement_start_host_monotonic_ns"))
    wall_duration = number(measurement.get("measurement_duration_wall_s"))
    measurement_end = measurement_start+wall_duration*1e9 if measurement_start is not None and wall_duration is not None else None
    original_measurement_bounds=[measurement_start,measurement_end] if measurement_start is not None and measurement_end is not None else None
    gui_start,gui_end = first("GUI_measurement_start"),first("GUI_measurement_end")
    start,end = number(gui_start.get("host_monotonic_ns")),number(gui_end.get("host_monotonic_ns"))
    output = args.output.resolve()
    output.mkdir(parents=True,exist_ok=True)
    censor=None
    if args.censor_host_monotonic_ns is not None:
        if not args.censor_reason or start is None or end is not None or args.censor_host_monotonic_ns <= start:
            parser.error("Censor branch requires reason, actual GUI start, missing GUI end and a later explicit actual bound")
        matched=[]
        native_path=args.raw/"logs/native_radio_events.csv"
        if native_path.is_file():
            with native_path.open(newline="") as stream:
                matched=[row for row in csv.DictReader(stream)
                    if number(row.get("wall_monotonic_ns"))==args.censor_host_monotonic_ns]
        if not any(row.get("event")=="fail_closed" for row in matched):
            raise SystemExit("Explicit censor timestamp does not match a raw native fail_closed event; no nominal fallback")
        end=float(args.censor_host_monotonic_ns)
        if original_measurement_bounds is not None and not measurement_start <= start < end <= measurement_end:
            raise SystemExit("Explicit censor bound is outside recorded scenario measurement")
        censor=dict(status="right_censored_at_actual_native_failure",host_monotonic_ns=end,
            declared_reason=args.censor_reason,evidence_path=str(native_path.resolve()),matched_native_events=matched,
            interpretation="Observed coupled GUI interval ends at failure; successful intended duration and readiness are not inferred.")
    valid=(start is not None and end is not None and start < end
        and (original_measurement_bounds is not None and measurement_start <= start < end <= measurement_end
            or censor is not None or args.gui_only_observation and original_measurement_bounds is None))
    if not valid:
        unavailable=dict(run_id=args.raw.name,status="unavailable_no_complete_actual_GUI_interval",
            GUI_interval=None,GUI_RTF=None,GUI_resources=None,GUI_start_event=gui_start or None,
            GUI_end_event=gui_end or None,controller=controller,
            scenario_summary_status=measurement.get("status"),
            measurement_host_bounds_ns=original_measurement_bounds,
            viewer_effective_config=json_file(args.controller_dir/"viewer-effective-config.json"),
            viewer_failures=[r for r in events if r.get("event")=="viewer_failed"],
            excluded_from_sensitivity18=True,
            limits=["Complete actual GUI bounds are absent/invalid; no nominal 85..145 or headless interval substituted.",
                "The technical pilot status and GUI failure are retained separately. This analysis does not assign a causal reason for runtime/parameter failure.",
                "Unavailable GUI timing/resources are missing, never zero. No GUI performance figure is rendered."])
        write_json(output/"GUI-analysis.json",unavailable)
        (output/"METHOD-and-caption.md").write_text(
            "GUI analysis unavailable: complete actual GUI_measurement_start/end host bounds were not recorded inside the scenario measurement. No nominal interval, interpolated GUI state, or zero resource value is substituted. No GUI performance figure is rendered. The original technical pilot and controller results remain authoritative and preserved; this artifact does not infer a causal reason for any runtime/parameter failure. This pilot is excluded from the 18-run study.\n")
        print(json.dumps(dict(status=unavailable["status"],GUI_RTF=None)))
        return
    origin_label="scenario measurement start" if original_measurement_bounds is not None else "actual GUI observation start"
    if original_measurement_bounds is None:
        measurement_start,measurement_end=start,end
    elif censor is not None:
        measurement_end=end
    observation_status=("right_censored_at_actual_native_failure" if censor is not None
        else "complete_actual_GUI_interval" if original_measurement_bounds is not None else "complete_actual_GUI_only_observation")
    intervals = {"GUI":(start,end)}
    stopped = first("owned_viewer_stopped")
    stop_host = number(stopped.get("host_monotonic_ns"))
    # Confirmed stop is after Docker returns, not the end-of-GUI marker.
    if censor is None and stopped.get("returncode",stopped.get("rc"))==0 and stop_host is not None and end <= stop_host < measurement_end:
        intervals["post_viewer_stop"]=(stop_host,measurement_end)
    export = args.reuse_export or output/"full-export"
    if not args.reuse_export:
        spec = importlib.util.spec_from_file_location("bas_pilot_analysis",ROOT/"scripts/product/analyze_relay_experiment.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.analyze_run(args.raw,export,plots=False,live_export=True)
    live = export/"live/metrics"
    clocks = csv_rows(live/"tracker_clock.csv")
    if not clocks:
        raise SystemExit("No paired source/host tracker clock CSV")
    trajectories = [r for r in csv_rows(live/"uav_trajectories.csv")
        if number(r.get("host_monotonic_ns")) is not None and measurement_start <= float(r["host_monotonic_ns"]) <= measurement_end]
    targets, unjoined = extract_targets(live,export/"metrics/relay_commands.csv")
    targets = [r for r in targets if measurement_start <= r["host_monotonic_ns"] <= measurement_end]
    lag = [r for r in csv_rows(live/"scheduler_lag.csv") if number(r.get("host_monotonic_ns")) is not None
        and measurement_start <= float(r["host_monotonic_ns"]) <= measurement_end]
    server = [r for r in csv_rows(live/"runtime_resources.csv") if number(r.get("host_monotonic_ns")) is not None
        and measurement_start <= float(r["host_monotonic_ns"]) <= measurement_end]
    expected = docker_pids(args.viewer_processes or args.controller_dir/"bas-paper-gui-viewer-processes.txt")
    summary,windows,viewer_samples = {},{},[]
    for name,(low,high) in intervals.items():
        timing,window = clock_analysis(clocks,low,high)
        windows[name]=window
        timing["native_ns3_lag_ms"]=distribution([float(r["scheduler_lag_ms"]) for r in lag if low <= float(r["host_monotonic_ns"]) <= high])
        scope=[r for r in trajectories if low <= float(r["host_monotonic_ns"]) <= high]
        timing["uav_quality"]={uav:dict(Counter(r["quality"] for r in scope if r["uav"]==uav)) for uav in UAVS}
        timing["uav_gaps"]={}
        for uav in UAVS:
            stamps=sorted(float(r["host_monotonic_ns"]) for r in scope if r["uav"]==uav)
            timing["uav_gaps"][uav]=dict(
                host_gaps_over_500ms_s=[(b-a)/1e9 for a,b in zip(stamps,stamps[1:]) if (b-a)/1e9>.5],
                boundary_unobserved_s=[(stamps[0]-low)/1e9,(high-stamps[-1])/1e9] if stamps else None)
        timing["height_source_age_ms"]=distribution([number(r.get("sample_age_ms")) for r in scope])
        if name=="GUI":
            timing["viewer_resources"],viewer_samples=viewer_resources(args.controller_dir/"gui-host-resources.jsonl",expected,low,high)
        summary[name]=timing
        write_csv(output/f"{name}-RTF-windows.csv",window,["segment","start_host_ns","end_host_ns","wall_duration_s","model_duration_s","end_to_end_rtf"])
    write_csv(output/"heights.csv",trajectories,["session_id","uav","source_sim_time_s","timestamp_source","host_monotonic_ns",
        "x_m","y_m","z_m","altitude_datum","quality","sample_age_ms","source_topic"])
    write_csv(output/"admitted-target-markers.csv",targets,["uav","source_sim_time_s","host_monotonic_ns","clock_resolution",
        "host_join_abs_age_ms","x_m","y_m","z_m","altitude_datum","command_id","epoch","source",
        "logger_event_host_monotonic_ns","host_time_source","lease_status","lineage_status"])
    write_csv(output/"native-lag.csv",lag,["ns3_time_s","scheduler_lag_ms","host_monotonic_ns","phase"])
    write_csv(output/"viewer-host-resources.csv",viewer_samples,["host_monotonic_ns","observed_pid_count","scope","cpu_percent_one_core","sum_process_rss_bytes","complete_expected_pid_coverage","graphics_gpu_memory_bytes"])
    write_csv(output/"server-resources.csv",server,["host_monotonic_ns","component","process_count","cpu_samples","sum_cpu_percent_one_core","sum_process_rss_bytes"])
    result=dict(run_id=args.raw.name,controller=controller,raw_path=str(args.raw.resolve()),
        status=observation_status,censoring=censor,GUI_observed_wall_duration_s=(end-start)/1e9,
        measurement_host_bounds_ns=original_measurement_bounds,analysis_host_bounds_ns=[measurement_start,measurement_end],
        host_axis_origin_label=origin_label,host_axis_origin_ns=measurement_start,
        experiment_measurement_window_recorded=original_measurement_bounds is not None,
        GUI_start_event=gui_start,GUI_end_event=gui_end,viewer_stop_event=stopped,
        viewer_effective_config=json_file(args.controller_dir/"viewer-effective-config.json"),
        intervals=summary,unmatched_target_markers_without_actual_UART_time=unjoined,
        height_datums=sorted({r.get("altitude_datum","") for r in trajectories}),
        target_status="observed_UART_target_markers" if targets else "unavailable_no_actual_timestamped_UART_target_markers",
        target_marker_counts={uav:sum(r["uav"]==uav for r in targets) for uav in UAVS},
        target_scope="Actual UART write-end markers only; ENU world z; no interpolated or persistent active target",
        server_resource_component_coverage=sorted({r["component"] for r in server}),
        excluded_from_sensitivity18=True,source_implementation_changed=False,
        limits=["One technical pilot. GUI and post-stop intervals differ in mission/fault phase: observational, not a causal overhead benchmark.",
            "A right-censored GUI interval is observed only until the explicit native failure event. It is not a passed GUI duration, and its summary does not explain the cause of failure.",
            "Only explicit actual host bounds are used. Clock samples are not interpolated to boundary timestamps.",
            "RTF windows are non-overlapping observed intervals >=1 s; pauses/gaps remain in wall time. Segment resets prevent a single integral.",
            "World ENU z is neither AGL nor AMSL. Missing or stale heights are gaps. Targets mark admission, not achieved flight.",
            "Host PID resource scope is a static Docker top snapshot intersected with collector-matched processes; unobserved processes are not zero.",
            "Compute-apps GPU memory is not graphics memory. Summed process RSS can count shared pages; RAM capture spool is outside RSS.",
            "Successful rendering alone is not evidence of physical flight, protocol correctness or successful complete run."])
    write_json(output/"GUI-analysis.json",result)
    (output/"METHOD-and-caption.md").write_text(
        f"Technical GUI pilot, separate from the 18-run sensitivity study. Observation status: {observation_status}. Observed GUI interval: {(end-start)/1e9:.9f} host seconds. Actual GUI start comes from the controller. A complete end uses GUI_measurement_end; a right-censor end uses only the explicitly supplied timestamp matched against raw native events, retaining that evidence. Post-stop comparison is disabled for censoring; otherwise it begins only after confirmed viewer Docker stop. Host-axis origin: {origin_label}. RTF is source model time / host time, recomputed in non-overlapping observed windows ≥1 s inside each interval. No sample is interpolated to a boundary, and gaps remain in elapsed time.\n\n"
        "The height panel shows world ENU z for five UAVs; missing/stale values break lines. Crosses show admitted ENU targets at actual UART write end, joined by UAV and command ID. They are not continuous active-target estimates. Resource panels distinguish server collector components from viewer host PIDs matched to Docker top. GPU graphics memory is unavailable from the compute-apps-only collector; no zero is imputed. RSS includes shared pages and excludes capture tmpfs.\n\n"
        "Caption: Five-UAV technical GUI pilot with an upstream native Gazebo viewer in a separate container. Shading denotes actual observation intervals. A right-censored interval ends at native failure and does not demonstrate a completed live GUI duration or successful flight. Any GUI/headless numerical difference is descriptive and does not estimate causal GUI cost. This pilot is not pooled with the 18 frozen runs.\n")
    if not args.no_plots:
        render(output,trajectories,targets,windows,lag,viewer_samples,server,intervals,measurement_start,
            origin_label=origin_label,observation_status=observation_status)
    print(json.dumps(dict(status=observation_status,controller_status=controller.get("status"),
        censor_host_monotonic_ns=censor["host_monotonic_ns"] if censor else None,
        intervals={name:{"integral_rtf":r["integral_rtf"],"window_p5":r["observed_window_rtf"]["p5"],
            "lag_p95_ms":r["native_ns3_lag_ms"]["p95"]} for name,r in summary.items()}),allow_nan=False))


if __name__=="__main__":
    main()
