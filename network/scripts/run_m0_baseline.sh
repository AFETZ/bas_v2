#!/usr/bin/env bash
set -euo pipefail

# This is intentionally a dependency/provenance-only M0 qualification probe.
# It does not seal, attest, or produce packet-path/P0 evidence.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUN_ID="${RUN_ID:-m0_baseline_$(date -u +%Y%m%dT%H%M%SZ)}"
RUN_ROOT="$ROOT_DIR/runs"
EXPECTED_RUN_DIR="$RUN_ROOT/$RUN_ID"
RUN_DIR="${RUN_DIR:-$EXPECTED_RUN_DIR}"

if (($# != 0)); then
  printf 'Usage: RUN_ID=<safe-id> %s\n' "$0" >&2
  exit 2
fi
if [[ ! "$RUN_ID" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]]; then
  printf 'FAIL RUN_ID contains unsafe characters: %s\n' "$RUN_ID" >&2
  exit 2
fi
if [[ "$RUN_DIR" != "$EXPECTED_RUN_DIR" ]]; then
  printf 'FAIL RUN_DIR must be exactly %s\n' "$EXPECTED_RUN_DIR" >&2
  exit 2
fi
if [[ -L "$RUN_ROOT" ]]; then
  printf 'FAIL runs directory must not be a symbolic link: %s\n' "$RUN_ROOT" >&2
  exit 2
fi
if [[ -e "$RUN_ROOT" && ! -d "$RUN_ROOT" ]]; then
  printf 'FAIL runs path is not a directory: %s\n' "$RUN_ROOT" >&2
  exit 2
fi

umask 077
mkdir -p "$RUN_ROOT"
if ! mkdir "$RUN_DIR"; then
  printf 'FAIL immutable M0 run directory already exists or cannot be created: %s\n' \
    "$RUN_DIR" >&2
  exit 1
fi
mkdir "$RUN_DIR/logs" "$RUN_DIR/metrics"

# Every redirect below creates a new file.  Never replace a file inserted into
# this fresh run while the probe is active.
set -o noclobber
printf '%q ' "$0" "$@" > "$RUN_DIR/command.txt"
printf '\n' >> "$RUN_DIR/command.txt"
{
  printf 'run_id=%s\n' "$RUN_ID"
  printf 'utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'probe=m0_dependency_provenance\n'
  printf 'packet_path_executed=false\n'
  printf 'sealed=false\n'
  printf 'attested=false\n'
  printf 'p0_eligible=false\n'
} > "$RUN_DIR/environment.txt"

set +e
"$ROOT_DIR/network/scripts/check_deps.sh" \
  > "$RUN_DIR/logs/check_deps.log" 2>&1
CHECK_DEPS_RC=$?
set -e
printf '%s\n' "$CHECK_DEPS_RC" > "$RUN_DIR/logs/check_deps.log.exit_code"

set +e
python3 "$ROOT_DIR/network/scripts/write_run_provenance.py" \
  --run-dir "$RUN_DIR" \
  > "$RUN_DIR/logs/provenance.log" 2>&1
PROVENANCE_RC=$?
set -e
printf '%s\n' "$PROVENANCE_RC" > "$RUN_DIR/logs/provenance.log.exit_code"

VALIDATION_OUTPUT="$RUN_DIR/metrics/m0_baseline_validation.json"
set +e
python3 "$ROOT_DIR/network/scripts/validate_m0_baseline.py" \
  --run-dir "$RUN_DIR" > "$VALIDATION_OUTPUT"
VALIDATION_RC=$?
set -e

if [[ -f "$VALIDATION_OUTPUT" ]]; then
  cat "$VALIDATION_OUTPUT"
else
  printf 'FAIL M0 validator did not produce JSON output\n' >&2
  exit 1
fi
printf 'M0 dependency/provenance probe retained at %s (not sealed, attested, or P0-eligible).\n' \
  "$RUN_DIR" >&2
exit "$VALIDATION_RC"
