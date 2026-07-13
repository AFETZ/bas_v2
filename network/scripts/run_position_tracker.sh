#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
RUN_DIR="${RUN_DIR:-$ROOT_DIR/runs/$RUN_ID}"

mkdir -p "$RUN_DIR/logs"

exec python3 "$ROOT_DIR/network/position_tracker/tracker.py" \
  --output-json "$RUN_DIR/logs/node_state.json" \
  --output-jsonl "$RUN_DIR/logs/node_state.jsonl" \
  "$@"
