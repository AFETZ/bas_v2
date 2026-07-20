#!/usr/bin/env bash
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
RUN_DIR="${RUN_DIR:-$ROOT_DIR/runs/$RUN_ID}"
LONG_RUN_MODE="${LONG_RUN_MODE:-optional}"
LONG_RUN_MIN_DURATION_S="${LONG_RUN_MIN_DURATION_S:-1800}"
NO_WRITE=0
JSON_OUTPUT=""
MATRIX="$ROOT_DIR/network/config/validation_matrix.yaml"

usage() {
  cat <<'EOF'
Usage: network/scripts/run_validation.sh [options]

Options:
  --run-dir PATH                    Run directory to inspect.
  --matrix PATH                     Authoritative v3 matrix to enforce.
  --long-run optional|required|skip P1 long-run policy (default: optional).
  --require-long-run                Alias for --long-run required.
  --skip-long-run                   Alias for --long-run skip.
  --long-run-min-duration-s N       Required P1 duration (default: 1800).
  --long-run-timeout-s N            Accepted for legacy compatibility; validation
                                    never launches or waits for a runtime.
  --json-output PATH                Alternate validation-results path.
  --no-write                        Inspect without changing the run directory.
  -h, --help                        Show this help.

The v3 validator reads raw evidence from one existing run. It does not start
dependencies, generate traffic, fabricate active proof, or trust producer gate
booleans. P0 failure returns exit code 1.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-dir)
      RUN_DIR="${2:?missing --run-dir value}"
      shift 2
      ;;
    --matrix)
      MATRIX="${2:?missing --matrix value}"
      shift 2
      ;;
    --long-run)
      LONG_RUN_MODE="${2:?missing --long-run value}"
      shift 2
      ;;
    --require-long-run)
      LONG_RUN_MODE="required"
      shift
      ;;
    --skip-long-run)
      LONG_RUN_MODE="skip"
      shift
      ;;
    --long-run-min-duration-s)
      LONG_RUN_MIN_DURATION_S="${2:?missing --long-run-min-duration-s value}"
      shift 2
      ;;
    --long-run-timeout-s)
      : "${2:?missing --long-run-timeout-s value}"
      shift 2
      ;;
    --json-output)
      JSON_OUTPUT="${2:?missing --json-output value}"
      shift 2
      ;;
    --no-write)
      NO_WRITE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'FAIL unknown argument: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

case "$LONG_RUN_MODE" in
  optional|required|skip) ;;
  *)
    printf 'FAIL --long-run must be optional, required, or skip; got %s\n' "$LONG_RUN_MODE" >&2
    exit 2
    ;;
esac

case "$RUN_DIR" in
  /*) ;;
  *) RUN_DIR="$ROOT_DIR/$RUN_DIR" ;;
esac
case "$MATRIX" in
  /*) ;;
  *) MATRIX="$ROOT_DIR/$MATRIX" ;;
esac

ARGS=(
  --run-dir "$RUN_DIR"
  --matrix "$MATRIX"
  --long-run "$LONG_RUN_MODE"
  --long-run-min-duration-s "$LONG_RUN_MIN_DURATION_S"
)
if [[ -n "$JSON_OUTPUT" ]]; then
  ARGS+=(--json-output "$JSON_OUTPUT")
fi
if (( NO_WRITE )); then
  ARGS+=(--no-write)
  exec python3 "$ROOT_DIR/network/validation/validate_run.py" "${ARGS[@]}"
fi

mkdir -p "$RUN_DIR/logs" "$RUN_DIR/metrics"
python3 "$ROOT_DIR/network/validation/validate_run.py" "${ARGS[@]}" \
  2>&1 | tee "$RUN_DIR/logs/validation.log"
exit "${PIPESTATUS[0]}"
