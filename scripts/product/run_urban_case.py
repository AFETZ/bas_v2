#!/usr/bin/env python3
"""Launch one preserved urban attempt under the existing native runtime lock."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[2]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-id", required=True)
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--policy", choices=("fixed", "failover"), required=True)
    p.add_argument("--duration", type=float, default=300)
    p.add_argument("--rng-run", type=int, default=1)
    p.add_argument("--build", action="store_true")
    p.add_argument("--parameters", action="store_true")
    p.add_argument("--parameter-workers", type=int, choices=range(1, 6), default=5)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--pilot", action="store_true")
    group.add_argument("--freeze", type=Path, help="Frozen plan containing this exact run/config/version")
    args = p.parse_args()
    if not args.run_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in args.run_id):
        p.error("run-id must be a simple unique basename")
    raw = ROOT / "runs/native-radio-realtime" / args.run_id
    controllers = ROOT / "runs/urban-ns3-dev/controllers"
    controllers.mkdir(parents=True, exist_ok=True)
    ledger = controllers / (args.run_id + ".json")
    if raw.exists() or ledger.exists():
        p.error("existing attempt is immutable: choose a new ID")
    config = args.config.resolve()
    config.relative_to(ROOT)  # must be mounted into native container
    source = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    config_sha = hashlib.sha256(config.read_bytes()).hexdigest()
    if args.freeze:
        freeze = json.loads(args.freeze.read_text())
        if freeze["source_commit"] != source:
            p.error("HEAD differs from frozen implementation")
        row = next((r for r in freeze["cases"] if r["run_id"] == args.run_id), None)
        if not row or (row["policy"], float(row["duration_s"]), int(row["rng_run"]), row["config_sha256"]) != (
                args.policy, args.duration, args.rng_run, config_sha):
            p.error("case arguments differ from freeze")
        if subprocess.check_output(["git", "diff", "HEAD", "--"], cwd=ROOT):
            p.error("tracked source differs from frozen HEAD")
    env = {k: v for k, v in os.environ.items() if not k.startswith("BAS_")}
    baseline = json.loads((ROOT / "runs/paper-live-20261026/campaign/freeze.json").read_text())["base_environment"]
    env.update(baseline)
    env.update(BAS_NATIVE_FIVE_RUN_ID=args.run_id, BAS_NATIVE_FIVE_SCENARIO="urban",
               BAS_NATIVE_PROPAGATION_PROFILE="geometry_logdistance", BAS_NATIVE_URBAN_CONFIG=str(config.relative_to(ROOT)),
               BAS_NATIVE_COORDINATION_POLICY=args.policy, BAS_NATIVE_FAULT_PROFILE="none",
               BAS_NATIVE_EXPERIMENT_PROFILE="urban", BAS_NATIVE_EXPERIMENT_DURATION_S=str(args.duration),
               BAS_NATIVE_EXPERIMENT_PILOT="1" if args.pilot else "0", BAS_NATIVE_PARAMETER_SNAPSHOTS="1" if args.parameters else "0",
               BAS_NATIVE_PARAMETER_WORKERS=str(args.parameter_workers),
               BAS_NATIVE_FIVE_SKIP_BUILD="0" if args.build else "1", BAS_NATIVE_RNG_SEED="1", BAS_NATIVE_RNG_RUN=str(args.rng_run))
    argv = ["flock", "--nonblock", "--conflict-exit-code", "75", "/tmp/bas-paper-runtime.lock",
            "./network/ns3/run_native_radio_five_uav.sh"]
    record = dict(run_id=args.run_id, source_commit=source, config=str(config.relative_to(ROOT)),
                  config_sha256=config_sha, config_snapshot=json.loads(config.read_text()),
                  controller_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), argv=argv,
                  environment={k:v for k,v in env.items() if k.startswith("BAS_")}, pilot=args.pilot,
                  freeze=str(args.freeze) if args.freeze else None, started_host_monotonic_ns=time.monotonic_ns())
    ledger.write_text(json.dumps(record, indent=2) + "\n")
    with (controllers / (args.run_id + ".log")).open("x") as log:
        child = subprocess.Popen(argv, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        result = child.wait()
    record.update(returncode=result, ended_host_monotonic_ns=time.monotonic_ns(), raw_exists=raw.exists())
    ledger.write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps({k: record[k] for k in ("run_id", "returncode", "raw_exists", "ended_host_monotonic_ns")}), flush=True)
    raise SystemExit(result)


if __name__ == "__main__":
    main()
