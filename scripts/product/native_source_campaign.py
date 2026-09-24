#!/usr/bin/env python3
"""Bounded native Spectrum source compatibility campaign, using the existing target."""
import argparse
import copy
import csv
import json
import math
from pathlib import Path
import statistics
import subprocess
import sys
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/"scripts/product"))
from prepare_native_sources import prepare


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--scene", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    args.run_dir.mkdir(parents=True, exist_ok=True)
    original = yaml.safe_load((ROOT/"network/config/native_jammers_reference.yaml").read_text())
    cases = {"baseline": {"sources": []}, "continuous": copy.deepcopy(original)}
    for name in ("pulsed", "sweep", "direction_front", "direction_back", "multiple", "nonoverlap"):
        case = copy.deepcopy(original)
        src = case["sources"][0]
        if name == "pulsed":
            src.update(mode="pulsed", duty_cycle=.5)
        elif name == "sweep":
            src.update(mode="sweep", sweep_hz=[2412000000, 2462000000], dwell_s=.5)
        elif name.startswith("direction"):
            src.update(pattern="tr38901", orientation_rad=[-math.pi/4 if name.endswith("front") else 3*math.pi/4, 0])
        elif name == "multiple":
            other = copy.deepcopy(src)
            other.update(id="source2", position_m=[30,10,2])
            case["sources"].append(other)
        elif name == "nonoverlap":
            src["center_hz"] = 2462000000
        cases[name] = case
    results = {}
    for name, case in cases.items():
        path = args.run_dir/name
        path.mkdir(exist_ok=False)
        (path/"sources.yaml").write_text(yaml.safe_dump(case))
        (path/"sources.json").write_text(json.dumps(prepare(case), indent=2))
        command = [str(args.binary.resolve()), "--scene="+str(args.scene.resolve()),
                   "--simulationSeconds=7", "--offeredPackets=200", "--distanceM=15",
                   "--output="+str((path/"result.json").resolve())]
        if case["sources"]:
            command.append("--sources="+str((path/"sources.json").resolve()))
        (path/"command.json").write_text(json.dumps(command, indent=2))
        print("Testing", name, flush=True)
        with (path/"run.log").open("w") as log:
            completed = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=120)
        result_file = path/"result.json"
        result = json.loads(result_file.read_text()) if result_file.exists() else {}
        result["process_exit_code"] = completed.returncode
        events_file = path/"result.json.events.csv"
        events = list(csv.DictReader(events_file.open())) if events_file.exists() else []
        foreign = [float(e["value"]) for e in events if e["event"] == "foreign_signal" and e["receiver"] == "sta"]
        result["foreign_signal_samples_sta"] = len(foreign)
        result["foreign_power_dbm_median_sta"] = statistics.median(foreign) if foreign else None
        result["application_rx_by_phase"] = {
            phase: sum(e["event"] == "application_rx" and low <= float(e["time_s"]) < high for e in events)
            for phase, low, high in (("before",0,3),("during",3,5),("after",5,7))}
        result["sampling"] = "decoded-MPDU signal/noise; SignalArrival foreign power; actual UdpServer Rx"
        results[name] = result
    checks = {
        "all_native_runs_completed": all(r.get("process_exit_code") == 0 for r in results.values()),
        "reference_delivery": results["baseline"].get("delivered_packets") == 200,
        "source_reaches_phy": results["continuous"]["foreign_signal_samples_sta"] > 0,
        "orientation_changes_power": (results["direction_front"]["foreign_power_dbm_median_sta"] or -999) > (results["direction_back"]["foreign_power_dbm_median_sta"] or -999),
        "nonoverlap_no_inband_power": results["nonoverlap"]["foreign_signal_samples_sta"] == 0,
        "recovery_has_application_delivery": all(r["application_rx_by_phase"]["after"] > 0 for r in results.values()),
    }
    (args.run_dir/"summary.json").write_text(json.dumps(dict(checks=checks, cases=results), indent=2)+"\n")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(10,4))
    names = list(results)
    values = [r.get("foreign_power_dbm_median_sta") for r in results.values()]
    ax.bar([n for n,v in zip(names,values) if v is not None], [v for v in values if v is not None])
    ax.set(ylabel="Native received foreign-signal power (dBm)", title="Town01 source campaign: observed in-band power at STA")
    ax.tick_params(axis="x", rotation=25)
    fig.tight_layout()
    fig.savefig(args.run_dir/"foreign_signal_power.png", dpi=160)
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
