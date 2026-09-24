#!/usr/bin/env bash
set -euo pipefail

# Executed only by the host-final validator in a fresh exact-image container.
# The source snapshot is read-only.  This M0/Q0 runner deliberately does not
# build the Q1-owned multiagent ROS package; M1's fresh overlay qualifies it.
# Dependencies, runtime lock, and the frozen Q0 suite are re-executed here.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUN_ID="${1:-}"
OUTPUT_ROOT="${AMS_M0_ARTIFACT_ROOT:-}"

if (($# != 1)) || [[ ! "$RUN_ID" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]]; then
  printf 'FAIL host re-execution requires one safe RUN_ID\n' >&2
  exit 2
fi
if [[ "$OUTPUT_ROOT" != "/run/ams/m0-artifacts" ]] || \
  [[ "${AMS_M0_SOURCE_MODE:-}" != "clean_git_clone_ro" ]] || \
  [[ "${AMS_M0_PROJECT_OVERLAY_MODE:-}" != "none_q0_source_only" ]] || \
  [[ "${AMS_M0_COLLECTION_SECURITY:-}" != \
    "cap_drop_all_no_new_privileges" ]] || \
  [[ "${AMS_M0_CAPABILITY_PROBE_MODE:-}" != \
    "host_final_isolated_exact_image" ]]; then
  printf 'FAIL host re-execution environment is not the immutable M0 path\n' >&2
  exit 2
fi
CAP_BOUNDING="$(awk '$1 == "CapBnd:" {print $2}' /proc/self/status)"
NO_NEW_PRIVS="$(awk '$1 == "NoNewPrivs:" {print $2}' /proc/self/status)"
if [[ "$CAP_BOUNDING" != "0000000000000000" ]] || \
  [[ "$NO_NEW_PRIVS" != "1" ]] || sudo -n true >/dev/null 2>&1; then
  printf 'FAIL host re-execution retained capabilities or privilege escalation\n' >&2
  exit 2
fi
if [[ -n "$(find "$OUTPUT_ROOT" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
  printf 'FAIL host re-execution output mount is not initially empty\n' >&2
  exit 2
fi

umask 007
set -o noclobber

set +e
"$ROOT_DIR/network/scripts/check_deps.sh" --qualification-profile m0 \
  > "$OUTPUT_ROOT/check_deps.stdout" \
  2> "$OUTPUT_ROOT/check_deps.stderr"
DEPENDENCY_RC=$?
set -e
printf '%s\n' "$DEPENDENCY_RC" > "$OUTPUT_ROOT/check_deps.exit_code"

set +e
/usr/bin/python3.10 "$ROOT_DIR/network/scripts/verify_m0_runtime_lock.py" \
  --lock "$ROOT_DIR/network/config/dependency_lock.yaml" \
  --observed-image-digest "$AMS_CONTAINER_IMAGE_DIGEST" \
  > "$OUTPUT_ROOT/runtime_lock.json" \
  2> "$OUTPUT_ROOT/runtime_lock.stderr"
RUNTIME_LOCK_RC=$?
set -e
printf '%s\n' "$RUNTIME_LOCK_RC" > "$OUTPUT_ROOT/runtime_lock.exit_code"

/usr/bin/python3.10 -c '
import json, pathlib, sys, sitecustomize
print(json.dumps({
  "guard_marker": getattr(sitecustomize, "AMS_M0_INERT_SITECUSTOMIZE", False),
  "no_site": sys.flags.no_site,
  "sitecustomize_path": str(pathlib.Path(sitecustomize.__file__).resolve()),
  "usercustomize_loaded": "usercustomize" in sys.modules,
}, sort_keys=True))
' > "$OUTPUT_ROOT/python_guard.json"

FRESH_SUITE_RUN_DIR="$OUTPUT_ROOT/$RUN_ID"
mkdir -m 0770 "$FRESH_SUITE_RUN_DIR"
mkdir -m 0770 "$FRESH_SUITE_RUN_DIR/logs" "$FRESH_SUITE_RUN_DIR/metrics"
set +e
"$ROOT_DIR/network/scripts/m0_bin/python3" \
  "$ROOT_DIR/network/scripts/run_m0_validation_suite.py" \
  --run-dir "$FRESH_SUITE_RUN_DIR" \
  > "$OUTPUT_ROOT/suite_runner.stdout" \
  2> "$OUTPUT_ROOT/suite_runner.stderr"
SUITE_RC=$?
set -e
printf '%s\n' "$SUITE_RC" > "$OUTPUT_ROOT/suite_runner.exit_code"

if ((DEPENDENCY_RC != 0 || RUNTIME_LOCK_RC != 0 || SUITE_RC != 0)); then
  exit 1
fi
