#!/usr/bin/env python3
"""Compare configured target, recorded PARAM_VALUE and actual Gazebo velocity."""
import argparse
import csv
import hashlib
import json
import math
from pathlib import Path


def main():
    p=argparse.ArgumentParser()
    p.add_argument("run",type=Path)
    p.add_argument("--output",type=Path,required=True)
    a=p.parse_args()
    config=a.run/"logs/urban_scenario.json"
    gcs_path=a.run/"logs/coordination_gcs.jsonl"
    raw_path=a.run/"logs/node_state.jsonl"
    params_path=a.run/"configuration/parameters-before/parameters.csv"
    c=json.loads(config.read_text())
    gcs=[json.loads(x)for x in gcs_path.open()]
    start=next(x for x in gcs if x["event"]=="phase_requested" and x["phase"]=="experiment_normal")
    end=next(x for x in gcs if x["event"]=="urban_measurement_window_closed")
    t0,t1=start["source_sim_time_s"],end["measurement_end_source_sim_time_s"]
    samples={i:{}for i in range(1,6)}
    for line in raw_path.open():
        for node in json.loads(line).get("nodes",[]):
            if not node["id"].startswith("uav") or node.get("stale"):
                continue
            source=node.get("source_sim_time_s")
            if source is None:continue
            v=node["velocity_enu_mps"]
            samples[int(node["id"][3:])].setdefault(source,math.hypot(v[0],v[1]))
    with params_path.open()as f:
        params={int(x["system_id"]):x for x in csv.DictReader(f)if x["param_id"]=="WP_SPD"}
    output=[]
    for i in range(1,6):
        window=[v for t,v in samples[i].items()if t0<=t<t1]
        command_acks=[x for x in gcs if x["event"]=="autopilot_ack" and x["uav_id"]==i
                      and x.get("mavlink_command")==178]
        row=dict(uav_id=i,requested_mission_speed_mps=c["mission"]["speed_mps"],
            observed_parameter_name="WP_SPD",observed_parameter_value_mps=json.loads(params[i]["value_json"]),
            observed_parameter_index=int(params[i]["param_index"]),
            observed_parameter_count=int(params[i]["param_count"]),
            source_samples_full=len(samples[i]),source_samples_window=len(window),
            actual_horizontal_speed_max_full_mps=max(samples[i].values()),
            actual_horizontal_speed_max_window_mps=max(window),
            samples_above_requested_plus_0p2_mps=sum(v>c["mission"]["speed_mps"]+.2 for v in window),
            do_change_speed_ack_count=len(command_acks),do_change_speed_ack_results=";".join(str(x["result"])for x in command_acks))
        output.append(row)
    a.output.parent.mkdir(parents=True,exist_ok=True)
    with a.output.open("w",newline="")as stream:
        writer=csv.DictWriter(stream,fieldnames=list(output[0]))
        writer.writeheader();writer.writerows(output)
    manifest=dict(run_id=a.run.name,measurement_source_s=[t0,t1],
        scope="Independent observed-speed/parameter mismatch, no inferred flight at configured speed",
        inputs_sha256={str(x.resolve()):hashlib.sha256(x.read_bytes()).hexdigest()for x in
                       (config,gcs_path,raw_path,params_path)},
        script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        command=["python3","reports/ispras/urban_ns3/tools/review_requested_speed.py",str(a.run),"--output",str(a.output)])
    a.output.with_suffix(".provenance.json").write_text(json.dumps(manifest,indent=2)+"\n")
    print(json.dumps(output,indent=2))


if __name__=="__main__":main()
