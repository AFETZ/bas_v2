#!/usr/bin/env python3
"""Execute the preserved frozen order, serially; never replace an attempt."""
import argparse
import datetime
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freeze", type=Path, required=True)
    args = parser.parse_args()
    freeze_path = args.freeze.resolve()
    frozen = json.loads(freeze_path.read_text())
    study = freeze_path.parent
    exports, controllers = study / "analysis", study / "controllers"
    controllers.mkdir(parents=True, exist_ok=True)
    history = study / "execution.jsonl"
    incomplete_streak = 0

    def event(kind, **fields):
        row = dict(event=kind, utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                   host_monotonic_ns=time.monotonic_ns(), **fields)
        with history.open("a") as f:
            f.write(json.dumps(row, allow_nan=False) + "\n")
        print(json.dumps(row), flush=True)

    event("campaign_started_or_resumed", source_commit=frozen["source_commit"], cases=len(frozen["cases"]))
    for order, case in enumerate(frozen["cases"], 1):
        rid = case["run_id"]
        ledger = controllers / (rid + ".json")
        raw = ROOT / "runs/native-radio-realtime" / rid
        if ledger.exists() or raw.exists():
            event("existing_attempt_preserved_without_replacement", order=order, run_id=rid)
        else:
            argv = [sys.executable, "scripts/product/run_urban_case.py", "--freeze", str(freeze_path),
                    "--run-id", rid, "--config", case["config"], "--policy", case["policy"],
                    "--duration", str(case["duration_s"]), "--rng-run", str(case["rng_run"]), "--parameters"]
            event("attempt_launch", order=order, run_id=rid, argv=argv)
            with (controllers / (rid + "-parent.log")).open("x") as log:
                child = subprocess.Popen(argv, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
                while True:
                    try:
                        code = child.wait(timeout=30)
                        break
                    except subprocess.TimeoutExpired:
                        phase = raw / "logs/current_phase.txt"
                        event("attempt_running", order=order, run_id=rid,
                              phase=phase.read_text().strip() if phase.exists() else "startup")
            event("attempt_launcher_finished", order=order, run_id=rid, returncode=code)
        export = exports / rid
        result_path = export / "metrics/urban_analysis.json"
        if not result_path.exists():
            argv = ["flock", "--nonblock", "--conflict-exit-code", "75", "/tmp/bas-paper-runtime.lock",
                    sys.executable, "scripts/product/analyze_urban_experiment.py", "run", str(raw), "--output", str(export)]
            with (controllers / (rid + "-analysis.log")).open("a") as log:
                result = subprocess.run(argv, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
            event("attempt_analysis_finished", order=order, run_id=rid, returncode=result.returncode)
        result = json.loads(result_path.read_text()) if result_path.exists() else {}
        complete = result.get("observation_window", {}).get("complete") is True
        incomplete_streak = 0 if complete else incomplete_streak + 1
        event("attempt_recorded", order=order, run_id=rid,
              recorded_status=result.get("recorded_status"), full_window=complete,
              recording_errors=result.get("data_quality_errors"), missing_measurements=result.get("missing_measurements"))
        if incomplete_streak >= 3:
            # Predeclared engineering stop, independent of comparative outcomes.
            # Preserve all attempts and pending cases; diagnose before a new version.
            event("campaign_stopped_infrastructure", reason="three consecutive incomplete measurement windows")
            return 2
    event("campaign_all_planned_attempts_processed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
