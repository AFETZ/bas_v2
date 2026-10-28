#!/usr/bin/env python3
"""Export diagnostics from existing urban analyses, never rerun simulations."""
import argparse
import csv
import hashlib
import json
from pathlib import Path


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--analysis", type=Path, action="append", required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    records, inputs = [], []
    for path in args.analysis:
        x = json.loads(path.read_text())
        stages, rates = x["target_stage_counts"], x["rates"]["all_five"]
        goals = x["mission_progress"]
        new = x["new_goal_after_NLOS"]["all_five"]
        row = dict(run_id=x["run_id"], policy=x["policy"], attempt_status=x["recorded_status"],
                   full_window=x["observation_window"]["complete"], offered=rates["offered"],
                   target_created=stages["target_CIDs_created_in_window"],
                   master_receive=stages["master_receive_CIDs"],
                   follower_application_receive=stages["follower_application_receive_CIDs"],
                   target_UART=stages["UART_write_end_CIDs"], timely=rates["timely"],
                   timely_per_offered=rates["timely_per_offered"],
                   all_UART_admissions=x["authority"]["total_UART_admissions"],
                   physical_waypoints=sum(g["completed_waypoints"] for g in goals.values()),
                   planned_waypoints=sum(g["planned_waypoints"] for g in goals.values()),
                   relay_changes=x["authority"]["selections"],
                   T_UART_post_NLOS_s=x["uart_after_NLOS"]["all_five"]["time_s"],
                   UART_endpoint_status=x["uart_after_NLOS"]["all_five"]["status"],
                   T_new_goal_after_NLOS_s=new["time_s"], new_goal_endpoint_status=new["status"],
                   integral_RTF=x["performance"]["integral_RTF"])
        records.append(row)
        inputs.append(dict(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                           raw_run_dir=x["raw_run_dir"], analysis_provenance=x["provenance"]))
    with (args.output / "pilot_runs.csv").open("w") as f:
        w = csv.DictWriter(f, fieldnames=list(records[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(records)
    (args.output / "pilot_provenance.json").write_text(json.dumps(dict(
        scope="diagnostic pilots, not pooled with the future frozen series; n1 per policy, no CI",
        script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), inputs=inputs,
        rates_alias="legacy delivered means UART admission; application receipt is separately counted",
        censoring="blank time is not zero; failed post-window landing remains failed"), indent=2) + "\n")
    print(json.dumps(records, indent=2))


if __name__ == "__main__":
    main()
