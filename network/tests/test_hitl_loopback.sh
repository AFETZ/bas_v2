#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUN_DIR="$(mktemp -d "$ROOT_DIR/runs/hitl_loopback_test_XXXXXX")"

printf 'HitL loopback test run directory: %s\n' "$RUN_DIR"

python3 "$ROOT_DIR/network/hitl/hitl_loopback.py" \
  --mode both \
  --run-dir "$RUN_DIR" \
  --packets-per-class 1 \
  --payload-bytes 80

python3 - "$RUN_DIR" <<'PY'
import json
import sys
from pathlib import Path

run_dir = Path(sys.argv[1])
summary_path = run_dir / "metrics" / "hitl_loopback_summary.json"
summary = json.loads(summary_path.read_text())

errors = []
if not summary.get("serial", {}).get("passed"):
    errors.append("serial loopback did not pass")
if not summary.get("ethernet", {}).get("passed"):
    errors.append("ethernet loopback did not pass")
if not summary.get("timing", {}).get("has_stage_correlation"):
    errors.append("timing log does not include endpoint, queue, ns3, and sionna stages")
if summary.get("packets", {}).get("dropped", 0) < 1:
    errors.append("deadline/drop logging was not exercised")
if not (run_dir / "logs" / "timing.jsonl").exists():
    errors.append("timing.jsonl was not created")

if errors:
    for error in errors:
        print(f"FAIL {error}")
    sys.exit(1)

print("PASS HitL serial/Ethernet virtual loopback and timing log checks")
PY

set +e
python3 "$ROOT_DIR/network/hitl/hitl_loopback.py" \
  --mode real-hardware \
  --run-dir "$RUN_DIR/real_hardware_guard" >/tmp/ams_hitl_guard_stdout.$$ 2>/tmp/ams_hitl_guard_stderr.$$
guard_status=$?
set -e
rm -f /tmp/ams_hitl_guard_stdout.$$ /tmp/ams_hitl_guard_stderr.$$

if [[ "$guard_status" -eq 0 ]]; then
  printf 'FAIL real-hardware mode unexpectedly succeeded\n' >&2
  exit 1
fi

printf 'PASS real-hardware mode fails closed without probing devices\n'

set +e
AMS_RADIO_BACKEND=real_modem_2_4ghz python3 "$ROOT_DIR/network/hitl/hitl_loopback.py" \
  --mode both \
  --run-dir "$RUN_DIR/real_backend_virtual_guard" >/tmp/ams_hitl_backend_guard_stdout.$$ 2>/tmp/ams_hitl_backend_guard_stderr.$$
backend_guard_status=$?
set -e
rm -f /tmp/ams_hitl_backend_guard_stdout.$$ /tmp/ams_hitl_backend_guard_stderr.$$

if [[ "$backend_guard_status" -eq 0 ]]; then
  printf 'FAIL virtual loopback unexpectedly ran with real_modem_2_4ghz selected\n' >&2
  exit 1
fi

printf 'PASS virtual loopback refuses the future real-modem backend\n'
