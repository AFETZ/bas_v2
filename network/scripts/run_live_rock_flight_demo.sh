#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUN_ID="${RUN_ID:-live_rock_flight_$(date -u +%Y%m%dT%H%M%SZ)}"
RUN_DIR="${RUN_DIR:-$ROOT_DIR/runs/$RUN_ID}"
DURATION_S="${DURATION_S:-72}"
RATE_HZ="${RATE_HZ:-2}"
DASHBOARD_HOST="${DASHBOARD_HOST:-127.0.0.1}"
DASHBOARD_PORT="${DASHBOARD_PORT:-8765}"
SCENARIO_FILE="${SCENARIO_FILE:-$ROOT_DIR/network/config/scenario_rock_demo.yaml}"
JAMMERS_FILE="${JAMMERS_FILE:-$ROOT_DIR/network/config/jammers_rock_demo.yaml}"
RADIO_FILE="${RADIO_FILE:-$ROOT_DIR/network/config/radio_24ghz_rock_demo.yaml}"
TX="${LIVE_SINR_TX:-uav1}"
RX="${LIVE_SINR_RX:-uav2}"
FLY_START_X="${FLY_START_X:--300}"
FLY_END_X="${FLY_END_X:-500}"
FLY_Y="${FLY_Y:-0}"
FLY_Z="${FLY_Z:-80}"
FLY_HOLD_BEFORE_S="${FLY_HOLD_BEFORE_S:-8}"
FLY_TRANSITION_S="${FLY_TRANSITION_S:-42}"
FLY_HOLD_AFTER_S="${FLY_HOLD_AFTER_S:-12}"
FLY_RATE_HZ="${FLY_RATE_HZ:-20}"

usage() {
  cat <<'EOF'
Usage: network/scripts/run_live_rock_flight_demo.sh [options]

Runs a live ROS/Gazebo/Sionna demo: starts the live SINR monitor and browser
dashboard, then moves uav2 through the shared Gazebo/Sionna rock-shadow line.

Run this inside the already-running simulation container opened with:
  ./scripts/enter_container.sh

Options:
  --duration SECONDS       Live monitor duration. Default: 72
  --rate-hz HZ             Sionna query rate. Default: 2
  --dashboard-port PORT    Dashboard port. Default: 8765
  --start-x X              uav2 starting X. Default: -300
  --end-x X                uav2 ending X. Default: 500
  --z Z                    uav2 forced altitude. Default: 80
  -h, --help               Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --duration)
      DURATION_S="${2:?missing duration}"
      shift 2
      ;;
    --rate-hz)
      RATE_HZ="${2:?missing rate}"
      shift 2
      ;;
    --dashboard-port)
      DASHBOARD_PORT="${2:?missing dashboard port}"
      shift 2
      ;;
    --start-x)
      FLY_START_X="${2:?missing start x}"
      shift 2
      ;;
    --end-x)
      FLY_END_X="${2:?missing end x}"
      shift 2
      ;;
    --z)
      FLY_Z="${2:?missing z}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'Unknown option: %s\n\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

mkdir -p "$RUN_DIR"/{logs,metrics,plots}

log() {
  printf '%s\n' "$*" | tee -a "$RUN_DIR/logs/live_rock_flight_demo.log"
}

cleanup() {
  local pid
  for pid in ${DASHBOARD_PID:-} ${MONITOR_PID:-}; do
    if [[ -n "${pid:-}" ]] && kill -0 "$pid" >/dev/null 2>&1; then
      kill "$pid" >/dev/null 2>&1 || true
      wait "$pid" >/dev/null 2>&1 || true
    fi
  done
}
trap cleanup EXIT

if ! command -v ros2 >/dev/null 2>&1 || ! command -v gz >/dev/null 2>&1; then
  log "FAIL ros2/gz not available; run inside the project simulation container"
  exit 2
fi

CSV="$RUN_DIR/metrics/live_sinr.csv"
log "live rock flight demo"
log "Run directory: $RUN_DIR"
log "Dashboard: http://$DASHBOARD_HOST:$DASHBOARD_PORT/"

log "Resetting $RX to clear start position x=$FLY_START_X y=$FLY_Y z=$FLY_Z"
python3 "$ROOT_DIR/network/scripts/fly_rock_shadow_path.py" \
  --model "$RX" \
  --start-x "$FLY_START_X" \
  --end-x "$FLY_START_X" \
  --y "$FLY_Y" \
  --z "$FLY_Z" \
  --hold-before-s 1 \
  --transition-s 0.001 \
  --hold-after-s 0 \
  --rate-hz "$FLY_RATE_HZ" \
  --output-csv "$RUN_DIR/logs/gz_set_pose_reset.csv" \
  > "$RUN_DIR/logs/fly_rock_shadow_reset.log" 2>&1

python3 "$ROOT_DIR/network/scripts/live_radio_dashboard.py" \
  --csv "$CSV" \
  --host "$DASHBOARD_HOST" \
  --port "$DASHBOARD_PORT" \
  > "$RUN_DIR/logs/live_radio_dashboard.log" 2>&1 &
DASHBOARD_PID=$!

RUN_ID="$RUN_ID" RUN_DIR="$RUN_DIR" "$ROOT_DIR/network/scripts/run_live_sinr_demo.sh" \
  --duration "$DURATION_S" \
  --rate-hz "$RATE_HZ" \
  --source ros \
  --tx "$TX" \
  --rx "$RX" \
  --scenario "$SCENARIO_FILE" \
  --jammers-config "$JAMMERS_FILE" \
  --radio-config "$RADIO_FILE" \
  --ros-node-state-timeout 60 \
  --no-ns3 \
  > "$RUN_DIR/logs/live_sinr_demo_stdout.log" 2>&1 &
MONITOR_PID=$!

log "Waiting for first live SINR sample before flying $RX"
for _ in $(seq 1 90); do
  if [[ -s "$CSV" ]] && [[ "$(wc -l < "$CSV")" -ge 2 ]]; then
    break
  fi
  if ! kill -0 "$MONITOR_PID" >/dev/null 2>&1; then
    wait "$MONITOR_PID" || true
    log "FAIL live SINR monitor exited before first sample"
    exit 2
  fi
  sleep 1
done
if [[ ! -s "$CSV" ]] || [[ "$(wc -l < "$CSV")" -lt 2 ]]; then
  log "FAIL no live SINR sample within startup window"
  exit 2
fi

log "Flying $RX through rock-shadow path x=$FLY_START_X->$FLY_END_X y=$FLY_Y z=$FLY_Z"
python3 "$ROOT_DIR/network/scripts/fly_rock_shadow_path.py" \
  --model "$RX" \
  --start-x "$FLY_START_X" \
  --end-x "$FLY_END_X" \
  --y "$FLY_Y" \
  --z "$FLY_Z" \
  --hold-before-s "$FLY_HOLD_BEFORE_S" \
  --transition-s "$FLY_TRANSITION_S" \
  --hold-after-s "$FLY_HOLD_AFTER_S" \
  --rate-hz "$FLY_RATE_HZ" \
  --output-csv "$RUN_DIR/logs/gz_set_pose.csv" \
  > "$RUN_DIR/logs/fly_rock_shadow_path.log" 2>&1

log "Waiting for live SINR monitor to finish"
wait "$MONITOR_PID"
log "live rock flight demo complete"
printf 'Run directory: %s\n' "$RUN_DIR"
printf 'Dashboard: http://%s:%s/\n' "$DASHBOARD_HOST" "$DASHBOARD_PORT"
