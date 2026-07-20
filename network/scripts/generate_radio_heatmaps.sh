#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
RUN_DIR="${RUN_DIR:-$ROOT_DIR/runs/$RUN_ID}"
MODE="${SIONNA_PROVIDER_MODE:-real_sionna}"

mkdir -p "$RUN_DIR/heatmaps" "$RUN_DIR/logs" "$RUN_DIR/metrics"

exec python3 "$ROOT_DIR/network/radio_provider/provider.py" heatmap \
  --mode "$MODE" \
  --run-dir "$RUN_DIR" \
  "$@"
