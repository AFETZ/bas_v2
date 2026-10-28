#!/usr/bin/env python3
"""Run actual native packet/geometry controls, under the integration runtime lock."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[2]


def run(output, config):
    output.mkdir(parents=True, exist_ok=False)
    base = json.loads(config.read_text())
    models = {"unchanged": base}
    models["moved"] = copy.deepcopy(base)
    for b in models["moved"]["buildings"]:
        for field in ("min", "max"):
            b[field][0] += 300
    models["equal"] = copy.deepcopy(base)
    models["equal"]["radio"]["nlos"] = copy.deepcopy(base["radio"]["los"])
    models["los_only"] = copy.deepcopy(base)
    models["los_only"]["radio"]["mode"] = "los_only"
    cases = [
        ("blocked", "unchanged", "0,0,2", "85,80,12", "15,90,16"),
        ("open", "unchanged", "0,0,2", "85,0,12", "15,90,16"),
        ("roof_clear", "unchanged", "0,0,40", "85,80,40", "15,90,40"),
        ("reverse", "unchanged", "85,80,12", "0,0,2", "15,90,16"),
        ("two_buildings", "unchanged", "0,0,2", "135,85,20", "15,90,16"),
        ("moved", "moved", "0,0,2", "85,80,12", "15,90,16"),
        ("equal", "equal", "0,0,2", "85,80,12", "15,90,16"),
        ("los_only", "los_only", "0,0,2", "85,80,12", "15,90,16"),
    ]
    for name, model in models.items():
        (output / (name + "-config.json")).write_text(json.dumps(model, indent=2) + "\n")
    ns3 = ROOT / ".external/ns-3-sionna-native"
    binary = ns3 / "build/scratch/ns3.48-native-geometry-probe-default"
    environment = dict(os.environ, LD_LIBRARY_PATH=str(ns3 / "build/lib") + ":" + os.environ.get("LD_LIBRARY_PATH", ""))
    records = []
    for name, model, start, end, relay in cases:
        argv = [str(binary), "--geometryConfig=" + str(output / (model + "-config.json")),
                "--outputPrefix=" + str(output / name), "--from=" + start, "--to=" + end,
                "--relay=" + relay, "--carrierHz=2412000000", "--txPowerDbm=10", "--RngSeed=1", "--RngRun=1"]
        record = dict(case=name, argv=argv, started_host_monotonic_ns=time.monotonic_ns(),
                      source=os.environ.get("BAS_SOURCE_HEAD") or subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                      binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest())
        with (output / (name + ".log")).open("x") as stream:
            result = subprocess.run(argv, env=environment, stdout=stream, stderr=subprocess.STDOUT)
        record.update(returncode=result.returncode, ended_host_monotonic_ns=time.monotonic_ns())
        records.append(record)
        (output / "commands.json").write_text(json.dumps(records, indent=2) + "\n")
        print(json.dumps(record), flush=True)
        if result.returncode:
            raise SystemExit(result.returncode)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--config", type=Path, default=ROOT / "network/config/urban_two_building.json")
    args = p.parse_args()
    run(args.output.resolve(), args.config.resolve())
