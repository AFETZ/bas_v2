#!/usr/bin/env python3
"""Render four paper figures from real frozen urban exports; never run BAS.

Matplotlib is imported only by render(). Dataset preparation can be inspected or
tested without graphics dependencies. Statistical intervals come from the stored
aggregator result, not from a second fit or packet-level bootstrap in this script.
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
import sys
import textwrap

NS=1_000_000_000
PUBLICATION_WIDTH_CM=13.9
PUBLICATION_WIDTH_IN=PUBLICATION_WIDTH_CM/2.54
PUBLICATION_DPI=320  # >=1642px at13.9cm; SVG/PDF retain the same physical width.
FONT_MAIN=8.
FONT_SMALL=7.5
PRIMARY=("timely_per_offered","completed_waypoint_fraction")
CAPTIONS={
 "urban_first_pair":(
  "Первая заранее запланированная geometry-пара: фактические XY/высоты пяти БПЛА, корпуса и геометрический LOS/NLOS исходного канала. Пунктир — последнее известное намерение НПУ до конца окна, не подтверждение UART и не интерполяция физики. Подготовка relay70м, горизонтальный сегмент60м, фасадный этап12м; при критическом событии followers преимущественно удерживают точки. Разрывы фактических данных не соединяются.",
  "First preplanned geometry pair: actual five-UAV XY/altitude, buildings and initial-link geometric LOS/NLOS. Dashed altitude carries last-known GCS intent to window end, not UART evidence or physical interpolation. Relay preparation70m, horizontal segment60m and facade stage12m; followers mostly hold at the critical event. Actual-data gaps remain disconnected."),
 "urban_primary_effects":(
  "Все заранее запланированные основные пары: timely/offered и доля физически подтверждённых waypoint. Крестик — failed attempt с наблюдённым исходом; отсутствующие исходы указаны счётчиком. Интервалы взяты из агрегатора по целым парам; interaction использует полные четырёхслучайные блоки.",
  "All preplanned main pairs: timely/offered and verified waypoint fraction. Crosses mark failed attempts with observed outcomes; missing outcomes are counted, never plotted at zero. Stored whole-pair intervals and complete four-case interaction are reported."),
 "urban_chronology":(
  "Та же первая geometry-пара: затенение, timeout, назначение/valid_from/активация, прикладной приём/UART критического CID и физический прогресс. Верхний ряд укрупнён в host секундах после NLOS; цель и скорость ниже показаны за полное окно в host секундах после старта. Отсутствующие события не восстанавливаются из других часов. Post-NLOS latency без нарушения обмена не называется восстановлением.",
  "Same first geometry pair: shadowing, timeout, selection/valid_from/activation, critical-CID application receipt/UART and physical progress. Top row zooms host seconds after NLOS; goal and speed below use the full window in host seconds after start. Missing events are not reconstructed from other clocks. Post-NLOS latency alone is not labelled recovery."),
 "urban_sensitivity_resources":(
  "Чувствительность к объявленным NLOS-гипотезам и наблюдённая стоимость: независимые парные точки, RTF/lag и CPU/RSS записанных компонентов. Auxiliary n3/boundary n1 описательные; main CI к ним не переносится. CPU нормирован на одно ядро, RSS не включает весь хост/tmpfs. GPU показывается только при наличии записи.",
  "Sensitivity to declared NLOS assumptions and observed cost: independent pair points, RTF/lag and recorded-component CPU/RSS. Auxiliary n3/boundary n1 are descriptive without borrowed main CIs. CPU is relative to one core; process RSS excludes whole-host/tmpfs memory. GPU is reported only when recorded.")}


def finite(value):
    if isinstance(value,bool):
        return None
    try:
        value=float(value)
        return value if math.isfinite(value) else None
    except (TypeError,ValueError):
        return None


def event_name(row):
    return row.get("stage",row.get("event"))


def host(row):
    return finite(row.get("host_monotonic_ns",row.get("wall_monotonic_ns")))


class Inputs:
    def __init__(self):
        self.used={}
        self.missing=set()

    def record(self,path):
        path=Path(path).resolve()
        if not path.is_file():
            self.missing.add(str(path));return False
        self.used[str(path)]=dict(bytes=path.stat().st_size,sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        return True

    def json(self,path):
        return json.loads(Path(path).read_text()) if self.record(path) else {}

    def csv(self,path):
        if not self.record(path):
            return []
        with Path(path).open(newline="") as stream:
            return list(csv.DictReader(stream))

    def jsonl(self,path):
        if not self.record(path):
            return []
        with Path(path).open() as stream:
            return [json.loads(line) for line in stream]


def first_geometry_pair(frozen):
    """Selection depends on frozen order only, never outcomes or file presence."""
    cases=sorted((r for r in frozen["cases"] if r.get("cohort")=="main"),key=lambda r:r["order"])
    if not cases:
        raise ValueError("freeze has no main cases")
    first=cases[0]
    selected=[r for r in cases if r["block_id"]==first["block_id"]
              and r["scenario_variant"]==first["scenario_variant"] and r["parameter_variant"]==first["parameter_variant"]
              and r["radio_mode"]=="geometry"]
    if len(selected)!=2 or {r["policy"] for r in selected}!={"fixed","failover"}:
        raise ValueError("first frozen block lacks its exact geometry pair")
    return {r["policy"]:r for r in selected}


def trajectory_segments(rows,uav):
    """Keep stale publications, clock resets and >0.5s source gaps as breaks."""
    segments=[];current=[];previous=None
    for row in rows:
        if str(row.get("uav_id"))!=str(uav):
            continue
        at=finite(row.get("source_sim_time_s"))
        valid=(row.get("quality")=="observed" and at is not None
               and all(finite(row.get(k)) is not None for k in ("x_m","y_m","z_m","host_monotonic_ns")))
        same=(previous is None or (row.get("session_id")==previous.get("session_id")
               and 0<at-finite(previous.get("source_sim_time_s"))<=.5)) if valid else False
        if not valid or not same:
            if current: segments.append(current)
            current=[]
        if valid:
            current.append(row);previous=row
        else:
            previous=None
    if current: segments.append(current)
    return segments


def read_case(case,analysis_root,runs_root,inputs):
    export=Path(analysis_root)/case["run_id"];raw=Path(runs_root)/case["run_id"]
    result=inputs.json(export/"metrics/urban_analysis.json")
    scenario=inputs.json(raw/"logs/urban_scenario.json")
    if not scenario:
        scenario=read_frozen_config(dict(path=case.get("config","/missing-config"),sha256=case.get("config_sha256")),inputs)
    trajectory=inputs.csv(export/"metrics/urban_trajectory.csv")
    window=result.get("observation_window",{})
    begin,end=finite(window.get("start_host_monotonic_ns")),finite(window.get("end_host_monotonic_ns"))
    trajectory=[r for r in trajectory if begin is not None and end is not None and host(r) is not None and begin<=host(r)<=end]
    geometry=[]
    for row in inputs.csv(raw/"logs/geometry_links.csv"):
        if {row.get("from_id"),row.get("to_id")}!={"0","1"}:
            continue
        at=host(row)
        if at is not None and begin is not None and end is not None and begin<=at<=end:
            geometry.append(dict(row,host_monotonic_ns=at,geom_los=row["geom_los"].lower() in ("1","true","los")))
    gcs=inputs.jsonl(raw/"logs/coordination_gcs.jsonl")
    return dict(case=case,result=result,scenario=scenario,trajectory=trajectory,geometry=geometry,gcs=gcs,
        begin=begin,end=end,raw=raw,export=export)


def primary_points(aggregate,frozen,metric,mode):
    actual={r["run_id"]:r for r in aggregate.get("runs",[])}
    blocks=defaultdict(dict)
    for case in frozen["cases"]:
        if case.get("cohort")!="main" or case["radio_mode"]!=mode:
            continue
        key=(case["scenario_variant"],case["parameter_variant"],case["block_id"])
        record=actual.get(case["run_id"],{})
        value=finite(record.get("outcomes",{}).get(metric))
        blocks[key][case["policy"]]=dict(run_id=case["run_id"],value=value,failed=record.get("attempt_failed",False),
            recorded_status=record.get("recorded_status","missing"))
    return blocks


def batch_complete(aggregate):
    """An open controller ledger counts as attempted, not a finished batch."""
    planned=aggregate.get("planned_attempts",0)
    rows=aggregate.get("runs",[])
    return planned>0 and len(rows)==planned and all(r.get("controller_returncode") is not None for r in rows)


def requested_altitude_series(events,uav,begin,end):
    """Carry the last recorded GCS intent, never an observed physical altitude."""
    if begin is None or end is None or end<begin:
        return []
    valid=[]
    for row in events:
        point=row.get("target_position_m");at=host(row)
        if (event_name(row)=="goal_created" and str(row.get("uav_id"))==str(uav)
                and row.get("target_frame")=="ENU" and isinstance(point,list) and len(point)==3
                and all(finite(v) is not None for v in point) and at is not None and at<=end):
            valid.append((at,float(point[2])))
    valid.sort(key=lambda r:r[0])
    prior=[r for r in valid if r[0]<=begin]
    points=([(begin,prior[-1][1])] if prior else [])+[r for r in valid if begin<r[0]<=end]
    if points and points[-1][0]<end:
        points.append((end,points[-1][1]))
    return [((at-begin)/NS,z) for at,z in points]


def common_critical_command(group,stage_rows):
    """Latest UART among the exact CIDs of the common all-five relay/epoch."""
    IDs=group.get("command_ids") or {}
    if finite(group.get("epoch")) is None or finite(group.get("leader_id")) is None:
        return None
    matched=[]
    for uav,CID in IDs.items():
        rows=[r for r in stage_rows if r.get("command_id")==CID and str(r.get("uav_id"))==str(uav)
              and finite(r.get("epoch"))==finite(group.get("epoch"))
              and finite(r.get("leader_id"))==finite(group.get("leader_id"))
              and finite(r.get("UART_write_end_host_ns")) is not None]
        if not rows:
            return None
        # Exact-CID deduplication does not substitute another epoch or member.
        matched.append(min(rows,key=lambda r:finite(r["UART_write_end_host_ns"])))
    return max(matched,key=lambda r:(finite(r["UART_write_end_host_ns"]),int(r["uav_id"]))) if matched else None


def chronology_data(data,inputs):
    result=data["result"];begin,end=data["begin"],data["end"]
    if begin is None or end is None:
        return []
    trigger=result.get("trigger",{}).get("event")
    start=host(trigger) if trigger else None
    timeline=[]
    def append(name,at,source,**fields):
        if at is not None and begin<=at<=end:
            timeline.append(dict(event=name,host_ns=at,elapsed_host_s=(at-begin)/NS,source=source,**fields))
    if trigger:
        append("NLOS",host(trigger),"native geometry")
    append("LOS return",finite(result.get("geometry",{}).get("LOS_return_host_ns")),"observed native geometry")
    for row in data["gcs"]:
        at=host(row)
        if at is None or start is None or at<start:
            continue
        if event_name(row)=="link_failure_detected":
            append("probe timeout",at,"GCS event")
        if event_name(row)=="grant_issued" and row.get("reason")=="selection":
            append("selection",at,"GCS issued grant",epoch=row.get("epoch"),leader_id=row.get("leader_id"))
            append("valid_from",finite(row.get("valid_from_ns")),"issued grant absolute host time",epoch=row.get("epoch"))
    group=result.get("uart_after_NLOS",{}).get("all_five",{})
    stages=inputs.csv(data["export"]/"metrics/urban_target_CID_stages.csv")
    command=common_critical_command(group,stages)
    if command:
        critical=str(command["uav_id"]);CID=command["command_id"]
        for label,key in (("command created","command_created_host_ns"),("master receive","master_receive_host_ns"),
                          ("master forward","master_forward_host_ns"),("app receipt","follower_receive_host_ns"),("UART","UART_write_end_host_ns")):
            append(label,finite(command.get(key)),"common all-five target CID stage CSV",command_id=CID,uav_id=critical,
                   common_epoch=group.get("epoch"),common_leader_id=group.get("leader_id"))
        for row in inputs.jsonl(data["raw"]/f"logs/coordination_uav{critical}.jsonl"):
            if (event_name(row)=="leader_activated" and row.get("epoch")==group.get("epoch")
                    and row.get("leader_id")==group.get("leader_id")):
                append("activation",host(row),"critical common-epoch endpoint event",uav_id=critical,
                       common_epoch=group.get("epoch"),common_leader_id=group.get("leader_id"))
    new_goal=result.get("new_goal_after_NLOS",{}).get("all_five",{})
    i=new_goal.get("critical_uav_id")
    if i is not None:
        row=result["new_goal_after_NLOS"]["per_uav"][str(i)]
        for label,key in (("selected goal created","goal_created_host_ns"),("goal first command","first_command_created_host_ns"),
                          ("goal first UART","first_goal_UART_host_ns"),("goal physical completion","completed_host_ns")):
            append(label,finite(row.get(key)),"selected verified new goal",uav_id=i,goal_id=row.get("goal_id"))
    return sorted(timeline,key=lambda r:r["host_ns"])


def percent_text(value):
    return "—" if value is None else f"{100*value:+.2f}"


def draw_geometry(axis,data,*,y_offset=0):
    segments=[];current=[];previous=None
    for row in data["geometry"]:
        at=host(row)
        if previous is not None and not 0<at-previous<=.5*NS:
            if current: segments.append(current)
            current=[]
        current.append(((at-data["begin"])/NS,(1 if row["geom_los"] else 0)+y_offset));previous=at
    if current: segments.append(current)
    for part in segments:
        axis.step([r[0] for r in part],[r[1] for r in part],where="post",color="0.35",lw=.9)


def figure_pair(plt,pair):
    from matplotlib.patches import Rectangle
    fig,axes=plt.subplots(3,2,figsize=(PUBLICATION_WIDTH_IN,6.6),gridspec_kw={"height_ratios":[2.1,1.3,.6]})
    for col,policy in enumerate(("fixed","failover")):
        data=pair[policy];begin,end=data["begin"],data["end"]
        for b in data["scenario"].get("buildings",[]):
            low,high=b["min"],b["max"]
            axes[0,col].add_patch(Rectangle(low[:2],high[0]-low[0],high[1]-low[1],fc=".65",ec=".3",alpha=.5))
            axes[0,col].text(low[0],low[1],f"{b.get('id','')}\nh={high[2]-low[2]:g} m",fontsize=FONT_SMALL)
        gcs=data["scenario"].get("gcs")
        if isinstance(gcs,list) and len(gcs)==3 and all(finite(v) is not None for v in gcs):
            axes[0,col].scatter([gcs[0]],[gcs[1]],marker="*",s=65,color="black",label=f"GCS, z={gcs[2]:g}m",zorder=5)
            axes[0,col].annotate("GCS",(gcs[0],gcs[1]),xytext=(4,-10),textcoords="offset points",fontsize=FONT_SMALL)
        if begin is None:
            axes[0,col].text(.5,.5,"No recorded measurement window",ha="center",transform=axes[0,col].transAxes)
        for i in range(1,6):
            for n,part in enumerate(trajectory_segments(data["trajectory"],i)):
                xs,ys=[float(r["x_m"]) for r in part],[float(r["y_m"]) for r in part]
                axes[0,col].plot(xs,ys,color=f"C{i-1}",lw=1,label=f"UAV{i}" if n==0 else None)
                if len(part)>5:
                    k=len(part)//2;j=min(len(part)-1,k+max(1,len(part)//25))
                    axes[0,col].annotate("",xy=(xs[j],ys[j]),xytext=(xs[k],ys[k]),arrowprops=dict(arrowstyle="->",color=f"C{i-1}",lw=.8))
                if begin is not None:
                    axes[1,col].plot([(host(r)-begin)/NS for r in part],[float(r["z_m"]) for r in part],color=f"C{i-1}",lw=1)
            requested=requested_altitude_series(data["gcs"],i,begin,end)
            if requested:
                axes[1,col].step([r[0] for r in requested],[r[1] for r in requested],
                    where="post",ls="--",lw=.8,color=f"C{i-1}",alpha=.6)
        nlos=[r for r in data["geometry"] if not r["geom_los"]]
        if nlos:
            axes[0,col].scatter([float(r["to_x"]) if r["to_id"]=="1" else float(r["from_x"]) for r in nlos[::10]],
                [float(r["to_y"]) if r["to_id"]=="1" else float(r["from_y"]) for r in nlos[::10]],s=3,c="crimson",label="UAV1 NLOS",zorder=4)
            initial=nlos[0]
            axes[0,col].plot([float(initial["from_x"]),float(initial["to_x"])],
                             [float(initial["from_y"]),float(initial["to_y"])],ls=":",lw=.8,color="crimson",
                             label="3D link: XY view")
        if begin is not None:
            draw_geometry(axes[2,col],data)
        axes[0,col].set(title=f"{policy}\n{data['result'].get('recorded_status','missing')}",xlabel="ENU X, m",ylabel="ENU Y, m" if col==0 else "",aspect="equal")
        axes[1,col].set(ylabel="ENU Z, m" if col==0 else "",title="Actual / dashed GCS intent")
        axes[2,col].set(xlabel="Host s after start",yticks=[0,1],yticklabels=["NLOS","LOS"] if col==0 else ["", ""],ylim=(-.2,1.2))
        if begin is not None and end is not None:
            for ax in axes[1:,col]: ax.set_xlim(0,(end-begin)/NS)
    handles,labels=axes[0,0].get_legend_handles_labels()
    fig.legend(handles,labels,loc="lower center",ncol=3,fontsize=FONT_SMALL,frameon=False)
    fig.suptitle("First geometry pair: "+pair["fixed"]["case"]["block_id"],fontsize=FONT_MAIN+1)
    fig.tight_layout(rect=(0,.1,1,.97),pad=.8,h_pad=1.8)
    return fig


def figure_primary(plt,aggregate,frozen):
    fig,axes=plt.subplots(2,2,figsize=(PUBLICATION_WIDTH_IN,5.8))
    audit=[];estimates=[]
    names={"timely_per_offered":"Timely / offered, %","completed_waypoint_fraction":"Verified points, %"}
    for row,metric in enumerate(PRIMARY):
        for col,mode in enumerate(("geometry","los_only")):
            ax=axes[row,col];blocks=primary_points(aggregate,frozen,metric,mode)
            missing=failed=complete=0
            for j,(key,pair) in enumerate(sorted(blocks.items())):
                offset=(j-(len(blocks)-1)/2)*.012
                observed=[pair.get(p,{}).get("value") for p in ("fixed","failover")]
                if all(v is not None for v in observed):
                    ax.plot([offset,1+offset],[100*v for v in observed],color=f"C{j%10}",alpha=.5,lw=.8);complete+=1
                for x,policy in enumerate(("fixed","failover")):
                    value=pair.get(policy,{}).get("value")
                    if value is None: missing+=1;continue
                    status=pair[policy]["failed"];failed+=int(status)
                    ax.scatter(x+offset,100*value,marker="x" if status else "o",color=f"C{j%10}",s=28,zorder=4)
                    audit.append(dict(metric=metric,mode=mode,block_id=key[-1],policy=policy,value=value,failed=status,run_id=pair[policy]["run_id"]))
            summaries=[r for r in aggregate.get("paired_contrasts",[]) if r["cohort"]=="main" and r["radio_mode"]==mode and r["metric"]==metric]
            for summary in summaries:
                estimate=summary["failover_minus_fixed"];CI=estimate.get("interval_95")
                name="Timely" if metric==PRIMARY[0] else "Mission"
                text=f"{name} {mode.replace('los_only','LOS')}: Δ{percent_text(estimate.get('mean'))}pp"
                text+=f";95% [{100*CI[0]:+.2f},{100*CI[1]:+.2f}]" if CI else "; CI unavailable"
                estimates.append(text)
            title=f"{mode.replace('los_only','LOS-only')} · pairs {complete}/{len(blocks)}\nmissing {missing}; failed observed {failed}"
            ax.set(xticks=[0,1],xticklabels=["fixed","failover"],xlim=(-.2,1.2),ylim=(-2,104),ylabel=names[metric] if col==0 else "",title=title)
    interaction=[]
    for row in aggregate.get("policy_geometry_interactions",[]):
        if row["metric"] in PRIMARY:
            estimate=row["estimate"];CI=estimate.get("interval_95")
            name="Timely" if row["metric"]==PRIMARY[0] else "Mission"
            text=f"{name} interaction {percent_text(estimate.get('mean'))} pp; blocks {row['complete_four_case_blocks']}/{row['planned_blocks']}"
            if CI: text+=f";95% [{100*CI[0]:+.2f},{100*CI[1]:+.2f}]"
            interaction.append(text)
    fig.text(.04,.015,"\n".join(textwrap.fill(s,width=68) for s in estimates+interaction),fontsize=FONT_SMALL)
    fig.suptitle("All main pairs · × = failed observed attempt",fontsize=FONT_MAIN+1)
    fig.tight_layout(rect=(0,.21,1,.97),pad=.7)
    return fig,audit


def figure_chronology(plt,pair,inputs):
    fig,axes=plt.subplots(3,2,figsize=(PUBLICATION_WIDTH_IN,7.7),gridspec_kw={"height_ratios":[2.1,1.15,1.35]})
    timelines={}
    lanes={"NLOS":0,"LOS return":0,"probe timeout":1,"selection":2,"valid_from":3,"activation":4,"command created":5,
           "master receive":6,"master forward":6,"app receipt":7,"UART":8}
    focus=(-1.,6.)
    for data in pair.values():
        t=data["result"].get("uart_after_NLOS",{}).get("all_five",{}).get("time_s")
        if finite(t) is not None: focus=(focus[0],max(focus[1],float(t)+2))
    for col,policy in enumerate(("fixed","failover")):
        data=pair[policy];begin,end=data["begin"],data["end"]
        timeline=chronology_data(data,inputs);timelines[policy]=timeline
        trigger=data["result"].get("trigger",{}).get("event")
        onset=(host(trigger)-begin)/NS if trigger and begin is not None else None
        for item in timeline:
            if item["event"] in lanes and onset is not None:
                y=lanes[item["event"]]
                axes[0,col].scatter(item["elapsed_host_s"]-onset,y,s=35,marker="|",color="C3")
            if item["event"].startswith("selected goal") or item["event"].startswith("goal "):
                y={"selected goal created":0,"goal first command":1,"goal first UART":2,"goal physical completion":3}[item["event"]]
                axes[1,col].scatter(item["elapsed_host_s"],y,s=35,marker="|",color="C2")
        if not any(r["event"]=="selection" for r in timeline):
            axes[0,col].text(.98,.25,"No new\nselection",ha="right",transform=axes[0,col].transAxes,fontsize=FONT_SMALL)
        if data["result"].get("uart_after_NLOS",{}).get("all_five",{}).get("status")=="observed" and not any(r["event"]=="UART" for r in timeline):
            axes[0,col].text(.02,.05,"UART in summary;\nCID trace missing",transform=axes[0,col].transAxes,fontsize=FONT_SMALL)
        if not any(r["event"]=="goal physical completion" for r in timeline):
            axes[1,col].text(.98,.4,"No all-five\nnew-goal completion",ha="right",transform=axes[1,col].transAxes,fontsize=FONT_SMALL)
        for i in range(1,6):
            for n,part in enumerate(trajectory_segments(data["trajectory"],i)):
                selected=[r for r in part if finite(r.get("speed_mps")) is not None]
                if selected and begin is not None:
                    axes[2,col].plot([(host(r)-begin)/NS for r in selected],[float(r["speed_mps"]) for r in selected],color=f"C{i-1}",lw=.8,label=f"UAV{i}" if n==0 else None)
        if trigger and begin is not None:
            axes[0,col].axvline(0,color=".4",ls=":",lw=.8)
            for ax in axes[1:,col]: ax.axvline(onset,color=".4",ls=":",lw=.8)
        axes[0,col].set(yticks=list(range(9)),yticklabels=["Geometry","Timeout","Select","Lease start","Active","CID made","Relay RX/TX","App RX","UART"] if col==0 else [""]*9,
                        title=policy,xlim=focus,xlabel="Host s after NLOS (zoom)")
        if onset is None:
            axes[0,col].set_xticks([]);axes[0,col].set_xlabel("NLOS origin unavailable")
            axes[0,col].text(.98,.6,"NLOS not\nobserved",ha="right",transform=axes[0,col].transAxes,fontsize=FONT_SMALL)
        axes[1,col].set(yticks=list(range(4)),yticklabels=["Goal made","CID made","UART","Reached"] if col==0 else [""]*4,
                        title="Selected new goal · full window")
        axes[2,col].set(xlabel="Host s after start",ylabel="Speed, m/s" if col==0 else "")
        if begin is not None and end is not None:
            for ax in axes[1:,col]: ax.set_xlim(0,(end-begin)/NS)
    handles,labels=axes[2,0].get_legend_handles_labels()
    fig.legend(handles,labels,loc="lower center",ncol=5,fontsize=FONT_SMALL,frameon=False)
    fig.suptitle("First pair: protocol zoom / goal and motion",fontsize=FONT_MAIN+1)
    fig.tight_layout(rect=(0,.045,1,.97),pad=.7)
    return fig,timelines


def read_frozen_config(metadata,inputs):
    """Relocate a frozen host config into the same checkout in a document image."""
    configured=Path(metadata.get("path","/missing-config"))
    candidate=configured if configured.is_file() else Path(__file__).resolve().parents[2]/"network/config"/configured.name
    if candidate.is_file() and metadata.get("sha256") and hashlib.sha256(candidate.read_bytes()).hexdigest()!=metadata["sha256"]:
        raise ValueError(f"configuration bytes differ from freeze: {candidate}")
    return inputs.json(candidate)


def sensitivity_rows(aggregate,frozen,inputs):
    values={}
    for variant in ("low","nominal","high"):
        item=frozen.get("scenario_inputs",{}).get(variant,{})
        cfg=read_frozen_config(item,inputs)
        values[variant]=finite(cfg.get("radio",{}).get("nlos",{}).get("exponent"))
    output=[]
    for row in aggregate.get("paired_contrasts",[]):
        if row["radio_mode"]!="geometry" or row["metric"] not in PRIMARY or row["cohort"] not in ("main","sensitivity"):
            continue
        exponent=values.get(row["parameter_variant"])
        differences=row["failover_minus_fixed"].get("differences",[])
        for n,value in enumerate(differences or [None]):
            output.append(dict(cohort=row["cohort"],parameter_variant=row["parameter_variant"],exponent=exponent,
                metric=row["metric"],difference=value,index=n,complete_pairs=row["complete_pairs"],planned_pairs=row["planned_pairs"]))
    return output


def resource_rows(frozen,analysis_root,inputs):
    output=[]
    for case in frozen["cases"]:
        export=Path(analysis_root)/case["run_id"]
        result=inputs.json(export/"metrics/urban_analysis.json")
        by_component=defaultdict(list)
        for row in inputs.csv(export/"metrics/urban_resources.csv"):
            by_component[row["component"]].append(row)
        perf=result.get("performance",{})
        rtf=perf.get("integral_RTF");lag=perf.get("scheduler_lag_ms",{}).get("p95")
        for component,samples in by_component.items():
            cpu=[finite(r.get("sum_cpu_percent_one_core")) for r in samples]
            rss=[finite(r.get("sum_process_RSS_bytes")) for r in samples]
            cpu=[v for v in cpu if v is not None];rss=[v for v in rss if v is not None]
            output.append(dict(run_id=case["run_id"],order=case["order"],component=component,
                mean_CPU_one_core_pct=statistics.fmean(cpu) if cpu else None,max_process_RSS_MiB=max(rss)/2**20 if rss else None,
                RTF=finite(rtf),lag_p95_ms=finite(lag),recorded_status=result.get("recorded_status","missing"),
                GPU_utilization=perf.get("GPU_utilization")))
        if not by_component:
            output.append(dict(run_id=case["run_id"],order=case["order"],component="not_recorded",mean_CPU_one_core_pct=None,
                max_process_RSS_MiB=None,RTF=finite(rtf),lag_p95_ms=finite(lag),recorded_status=result.get("recorded_status","missing"),GPU_utilization=perf.get("GPU_utilization")))
    return output


def figure_cost(plt,aggregate,frozen,analysis_root,inputs):
    fig,axes=plt.subplots(2,2,figsize=(PUBLICATION_WIDTH_IN,6.7))
    sensitivity=sensitivity_rows(aggregate,frozen,inputs);resources=resource_rows(frozen,analysis_root,inputs)
    for index,metric in enumerate(PRIMARY):
        ax=axes[index,0];points=[r for r in sensitivity if r["metric"]==metric and r["exponent"] is not None]
        groups=defaultdict(list)
        for r in points: groups[(r["cohort"],r["exponent"])].append(r)
        for (cohort,exponent),rows in groups.items():
            for j,row in enumerate(rows):
                if row["difference"] is not None:
                    ax.scatter(exponent+(j-(len(rows)-1)/2)*.006,100*row["difference"],marker="o" if cohort=="main" else "s",s=20,
                               color="C0" if cohort=="main" else "C1")
        ticks=sorted({exponent for cohort,exponent in groups})
        ax.set_xticks(ticks)
        ax.set_xticklabels([f"{x:g}\n"+"/".join(str(v) for v in (next(rows for (cohort,exponent),rows in groups.items() if exponent==x)[0]["complete_pairs"],
                                                   next(rows for (cohort,exponent),rows in groups.items() if exponent==x)[0]["planned_pairs"])) for x in ticks])
        ax.axhline(0,color="grey",lw=.7)
        ax.set(xlabel="NLOS exponent / complete pairs",ylabel="Δ, pp",title="Timely effect" if metric==PRIMARY[0] else "Mission effect")
    runs={}
    for row in resources: runs.setdefault(row["run_id"],row)
    lag_axis=axes[0,1].twinx()
    for row in runs.values():
        marker="x" if row["recorded_status"]=="failed" else "o"
        if row["RTF"] is not None: axes[0,1].scatter(row["order"],row["RTF"],s=15,c="C0",marker=marker)
        if row["lag_p95_ms"] is not None: lag_axis.scatter(row["order"],row["lag_p95_ms"],s=12,c="C3",marker="+")
    axes[0,1].axhline(1,color="grey",ls=":",lw=.7)
    axes[0,1].set(xlabel="Frozen order",ylabel="RTF (blue)",title="Timing")
    lag_axis.set_ylabel("Lag p95, ms (red)")
    rss_axis=axes[1,1].twinx()
    components=sorted({r["component"] for r in resources if r["component"]!="not_recorded"})
    for k,component in enumerate(components):
        rows=[r for r in resources if r["component"]==component]
        label={"native_ns3_sionna":"native ns-3","arducopter":"SITL ×5","gazebo":"Gazebo"}.get(component,component)
        axes[1,1].plot([r["order"] for r in rows],[r["mean_CPU_one_core_pct"] if r["mean_CPU_one_core_pct"] is not None else float("nan") for r in rows],
                      marker=".",ls="none",color=f"C{k}",label=label)
        rss_axis.plot([r["order"] for r in rows],[r["max_process_RSS_MiB"] if r["max_process_RSS_MiB"] is not None else float("nan") for r in rows],
                      marker="+",ls="none",color=f"C{k}",alpha=.6)
    axes[1,1].set(xlabel="Frozen order",ylabel="CPU, % core (dots)",title="Monitored processes")
    rss_axis.set_ylabel("Max RSS, MiB (+)")
    axes[1,1].legend(fontsize=FONT_SMALL,loc="best",handletextpad=.3)
    boundary=[]
    for r in aggregate.get("paired_contrasts",[]):
        if r["cohort"]=="engineering_boundary" and r["metric"] in PRIMARY:
            name="timely" if r["metric"]==PRIMARY[0] else "mission"
            scenario=r["scenario_variant"].replace("short_occlusion","Short").replace("no_candidate","No candidate")
            boundary.append(f"{scenario}, {name}: Δ{percent_text(r['failover_minus_fixed'].get('mean'))}pp; pairs {r['complete_pairs']}/{r['planned_pairs']}")
    missing=sum(row["RTF"] is None for row in runs.values())
    gpu="GPU not recorded" if not any(r.get("GPU_utilization") is not None for r in resources) else "GPU measured: see data"
    footer=boundary+[f"Timing missing: {missing}; {gpu}. Auxiliary pairs are descriptive."]
    fig.text(.04,.015,"\n".join(textwrap.fill(s,width=68) for s in footer),fontsize=FONT_SMALL)
    fig.tight_layout(rect=(0,.14,1,.99),pad=.7)
    return fig,dict(sensitivity=sensitivity,resources=resources,boundary_descriptions=boundary)


def render(args):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.family":"DejaVu Sans","font.size":FONT_MAIN,"axes.titlesize":FONT_MAIN,
                         "axes.labelsize":FONT_MAIN,"xtick.labelsize":FONT_SMALL,"ytick.labelsize":FONT_SMALL,
                         "legend.fontsize":FONT_SMALL,"axes.grid":True,"grid.alpha":.2,"svg.fonttype":"none"})
    inputs=Inputs();frozen=inputs.json(args.freeze);aggregate=inputs.json(args.aggregate)
    if aggregate.get("freeze_sha256")!=hashlib.sha256(Path(args.freeze).read_bytes()).hexdigest():
        raise ValueError("aggregate references a different freeze")
    complete=batch_complete(aggregate)
    if not complete and not args.allow_incomplete:
        raise ValueError("batch incomplete; final figures require all planned attempts (or explicit --allow-incomplete preview)")
    args.output.mkdir(parents=True,exist_ok=True)
    pair_cases=first_geometry_pair(frozen)
    pair={p:read_case(case,args.analysis_root,args.runs_root,inputs) for p,case in pair_cases.items()}
    manifest=dict(schema_version=1,preview=args.allow_incomplete,batch_complete=complete,figures=[],not_rendered=[],
        publication_width_cm=PUBLICATION_WIDTH_CM,font_floor_pt=FONT_SMALL,png_dpi=PUBLICATION_DPI,
        argv=[sys.executable,*sys.argv],freeze_sha256=hashlib.sha256(Path(args.freeze).read_bytes()).hexdigest(),
        renderer_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),first_block=pair_cases["fixed"]["block_id"])
    tasks=[]
    if any(d["trajectory"] for d in pair.values()):
        tasks.append(("urban_first_pair",lambda:(figure_pair(plt,pair),dict(first_pair={p:d["case"]["run_id"] for p,d in pair.items()}))))
        tasks.append(("urban_chronology",lambda:figure_chronology(plt,pair,inputs)))
    else: manifest["not_rendered"].append(dict(id="urban_first_pair/urban_chronology",reason="no actual trajectories for first frozen pair"))
    if any(finite(r.get("outcomes",{}).get("timely_per_offered")) is not None for r in aggregate.get("runs",[])):
        tasks.append(("urban_primary_effects",lambda:figure_primary(plt,aggregate,frozen)))
        tasks.append(("urban_sensitivity_resources",lambda:figure_cost(plt,aggregate,frozen,args.analysis_root,inputs)))
    else: manifest["not_rendered"].append(dict(id="urban_primary_effects/urban_sensitivity_resources",reason="no observed primary outcomes"))
    for identity,make in tasks:
        fig,data=make();paths={}
        for extension in ("png","svg","pdf"):
            path=args.output/(identity+"."+extension);fig.savefig(path,dpi=PUBLICATION_DPI,bbox_inches=None)
            paths[extension]=str(path.resolve())
        plt.close(fig)
        data_path=args.output/(identity+".data.json");data_path.write_text(json.dumps(data,indent=2,allow_nan=False)+"\n")
        manifest["figures"].append(dict(id=identity,measured=True,path=paths["png"],paths=paths,data_path=str(data_path.resolve()),
            caption_ru=CAPTIONS[identity][0],caption_en=CAPTIONS[identity][1],
            output_sha256={key:hashlib.sha256(Path(path).read_bytes()).hexdigest() for key,path in paths.items()}))
    manifest.update(input_files=inputs.used,missing_input_files=sorted(inputs.missing))
    (args.output/"figure_manifest.json").write_text(json.dumps(manifest,indent=2,allow_nan=False)+"\n")
    print(json.dumps(dict(figures=[r["id"] for r in manifest["figures"]],output=str(args.output),missing_inputs=len(inputs.missing))))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--freeze",type=Path,required=True)
    p.add_argument("--aggregate",type=Path,required=True,help="urban_study.json from frozen aggregator")
    p.add_argument("--analysis-root",type=Path,required=True)
    p.add_argument("--runs-root",type=Path,required=True)
    p.add_argument("--output",type=Path,required=True)
    p.add_argument("--allow-incomplete",action="store_true",help="explicit preview; never silently replace final batch")
    render(p.parse_args())


if __name__=="__main__":
    main()
