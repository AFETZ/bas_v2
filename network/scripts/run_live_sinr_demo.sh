#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ORIGINAL_ARGS=("$@")
RUN_ID="${RUN_ID:-live_sinr_$(date -u +%Y%m%dT%H%M%SZ)}"
RUN_DIR="${RUN_DIR:-$ROOT_DIR/runs/$RUN_ID}"
DURATION_S="${DURATION_S:-20}"
RATE_HZ="${RATE_HZ:-1}"
SCENARIO_FILE="${SCENARIO_FILE:-$ROOT_DIR/network/config/scenario_5uav.yaml}"
JAMMERS_FILE="${JAMMERS_FILE:-$ROOT_DIR/network/config/jammers.yaml}"
SOURCE="${LIVE_SINR_SOURCE:-auto}"
PROVIDER_MODE="${SIONNA_PROVIDER_MODE:-real_sionna}"
TX="${LIVE_SINR_TX:-uav1}"
RX="${LIVE_SINR_RX:-uav2}"
TRAFFIC_CLASS="${LIVE_SINR_TRAFFIC_CLASS:-control}"
WITH_NS3="${WITH_NS3:-1}"
START_PROVIDER="${START_PROVIDER:-1}"
START_TRACKER="${START_TRACKER:-1}"
DEADLINE_MS="${SIONNA_DEADLINE_MS:-30000}"
ROS_NODE_STATE_TIMEOUT_S="${ROS_NODE_STATE_TIMEOUT_S:-60}"
REPLAY_AMPLITUDE_M="${REPLAY_AMPLITUDE_M:-1600}"
REPLAY_PERIOD_S="${REPLAY_PERIOD_S:-20}"
REPLAY_MOVING_NODE="${REPLAY_MOVING_NODE:-}"
RADIO_FILE="${RADIO_FILE:-$ROOT_DIR/network/config/radio_24ghz.yaml}"

usage() {
  cat <<'EOF'
Usage: network/scripts/run_live_sinr_demo.sh [options]

Options:
  --duration SECONDS          Demo duration. Default: 20
  --rate-hz HZ                Live Sionna query rate. Default: 1
  --scenario PATH             Scenario YAML. Default: network/config/scenario_5uav.yaml
  --jammers-config PATH       Jammer YAML. Default: network/config/jammers.yaml
  --source auto|ros|replay    Node-state source. Default: auto
  --tx NODE                   Link transmitter. Default: uav1
  --rx NODE                   Link receiver. Default: uav2
  --traffic-class NAME        Traffic class. Default: control
  --provider-mode MODE        real_sionna or test_free_space. Default: real_sionna
  --radio-config PATH         Radio YAML for provider, monitor, and ns-3. Default: network/config/radio_24ghz.yaml
  --ros-node-state-timeout S  Wait for fresh ROS odometry node-state. Default: 60
  --test-free-space           Shortcut for --provider-mode test_free_space
  --no-ns3                    Do not run ns-3 alongside the live monitor
  --no-provider               Use an already-running Sionna provider
  --no-tracker                Do not start the ROS position tracker
  --replay-moving-node NODE   Node moved by deterministic replay. Default: receiver
  --replay-amplitude-m M      Replay sweep amplitude. Default: 1600
  --replay-period-s SECONDS   Replay sweep period. Default: 20
  -h, --help                  Show this help

Environment:
  RUN_ID, RUN_DIR, LIVE_SINR_SOURCE, LIVE_SINR_TX, LIVE_SINR_RX,
  SCENARIO_FILE, RADIO_FILE, JAMMERS_FILE, WITH_NS3, START_PROVIDER,
  START_TRACKER, SIONNA_PROVIDER_MODE, ROS_NODE_STATE_TIMEOUT_S
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --duration)
      DURATION_S="${2:?missing duration value}"
      shift 2
      ;;
    --rate-hz)
      RATE_HZ="${2:?missing rate value}"
      shift 2
      ;;
    --scenario)
      SCENARIO_FILE="${2:?missing scenario path}"
      shift 2
      ;;
    --jammers-config)
      JAMMERS_FILE="${2:?missing jammers config path}"
      shift 2
      ;;
    --source)
      SOURCE="${2:?missing source value}"
      shift 2
      ;;
    --tx)
      TX="${2:?missing tx value}"
      shift 2
      ;;
    --rx)
      RX="${2:?missing rx value}"
      shift 2
      ;;
    --traffic-class)
      TRAFFIC_CLASS="${2:?missing traffic-class value}"
      shift 2
      ;;
    --provider-mode)
      PROVIDER_MODE="${2:?missing provider mode value}"
      shift 2
      ;;
    --radio-config)
      RADIO_FILE="${2:?missing radio config path}"
      shift 2
      ;;
    --ros-node-state-timeout)
      ROS_NODE_STATE_TIMEOUT_S="${2:?missing ROS node-state timeout value}"
      shift 2
      ;;
    --test-free-space)
      PROVIDER_MODE="test_free_space"
      shift
      ;;
    --no-ns3)
      WITH_NS3=0
      shift
      ;;
    --no-provider)
      START_PROVIDER=0
      shift
      ;;
    --no-tracker)
      START_TRACKER=0
      shift
      ;;
    --replay-moving-node)
      REPLAY_MOVING_NODE="${2:?missing replay moving node value}"
      shift 2
      ;;
    --replay-amplitude-m)
      REPLAY_AMPLITUDE_M="${2:?missing replay amplitude value}"
      shift 2
      ;;
    --replay-period-s)
      REPLAY_PERIOD_S="${2:?missing replay period value}"
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

case "$SOURCE" in
  auto|ros|replay) ;;
  *)
    printf 'FAIL --source must be auto, ros, or replay; got %s\n' "$SOURCE" >&2
    exit 2
    ;;
esac

case "$PROVIDER_MODE" in
  real_sionna|test_free_space) ;;
  *)
    printf 'FAIL --provider-mode must be real_sionna or test_free_space; got %s\n' "$PROVIDER_MODE" >&2
    exit 2
    ;;
esac

abspath_file() {
  local path="$1"
  local label="$2"
  if [[ ! -f "$path" ]]; then
    printf 'FAIL %s file missing: %s\n' "$label" "$path" >&2
    exit 2
  fi
  printf '%s/%s\n' "$(cd "$(dirname "$path")" && pwd -P)" "$(basename "$path")"
}

SCENARIO_FILE="$(abspath_file "$SCENARIO_FILE" "scenario")"
JAMMERS_FILE="$(abspath_file "$JAMMERS_FILE" "jammers config")"
RADIO_FILE="$(abspath_file "$RADIO_FILE" "radio config")"

export RUN_ID RUN_DIR SCENARIO_FILE JAMMERS_FILE RADIO_FILE
mkdir -p "$RUN_DIR"/{logs,metrics,plots,pcap,flowmon,ns3}

printf '%q ' "$0" "${ORIGINAL_ARGS[@]}" > "$RUN_DIR/command.txt"
printf '\n' >> "$RUN_DIR/command.txt"

{
  printf 'run_id=%s\n' "$RUN_ID"
  printf 'utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'root=%s\n' "$ROOT_DIR"
  printf 'duration_s=%s\n' "$DURATION_S"
  printf 'rate_hz=%s\n' "$RATE_HZ"
  printf 'scenario=%s\n' "$SCENARIO_FILE"
  printf 'jammers_config=%s\n' "$JAMMERS_FILE"
  printf 'source=%s\n' "$SOURCE"
  printf 'radio_config=%s\n' "$RADIO_FILE"
  printf 'ros_node_state_timeout_s=%s\n' "$ROS_NODE_STATE_TIMEOUT_S"
  printf 'provider_mode=%s\n' "$PROVIDER_MODE"
  printf 'tx=%s\n' "$TX"
  printf 'rx=%s\n' "$RX"
  printf 'traffic_class=%s\n' "$TRAFFIC_CLASS"
  printf 'replay_moving_node=%s\n' "${REPLAY_MOVING_NODE:-$RX}"
  printf 'replay_amplitude_m=%s\n' "$REPLAY_AMPLITUDE_M"
  printf 'replay_period_s=%s\n' "$REPLAY_PERIOD_S"
  printf 'with_ns3=%s\n' "$WITH_NS3"
  printf 'git_head=%s\n' "$(git -C "$ROOT_DIR" rev-parse --short HEAD 2>/dev/null || printf unknown)"
	} > "$RUN_DIR/environment.txt"
cp "$RUN_DIR/environment.txt" "$RUN_DIR/logs/live_sinr_demo_environment.txt"

log() {
  printf '%s\n' "$*" | tee -a "$RUN_DIR/logs/live_sinr_demo.log"
}

cleanup() {
  local pid
  for pid in ${NS3_PID:-} ${TRACKER_PID:-} ${SIONNA_PID:-}; do
    if [[ -n "${pid:-}" ]] && kill -0 "$pid" >/dev/null 2>&1; then
      kill "$pid" >/dev/null 2>&1 || true
      wait "$pid" >/dev/null 2>&1 || true
    fi
  done
}
trap cleanup EXIT

wait_for_tcp() {
  local host="$1"
  local port="$2"
  local timeout_s="$3"
  python3 - "$host" "$port" "$timeout_s" <<'PY'
import socket
import sys
import time

host = sys.argv[1]
port = int(sys.argv[2])
deadline = time.monotonic() + float(sys.argv[3])
while time.monotonic() < deadline:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            sys.exit(0)
    except OSError:
        time.sleep(0.25)
sys.exit(1)
PY
}

wait_for_node_state() {
  local path="$1"
  local timeout_s="$2"
  local tx="$3"
  local rx="$4"
  python3 - "$path" "$timeout_s" "$tx" "$rx" <<'PY'
import json
import sys
import time
from pathlib import Path

path = Path(sys.argv[1])
deadline = time.monotonic() + float(sys.argv[2])
required = {sys.argv[3], sys.argv[4]}
while time.monotonic() < deadline:
    try:
        data = json.loads(path.read_text())
        nodes = {
            str(node.get("id")): node
            for node in data.get("nodes", [])
            if isinstance(node, dict)
        }
        fresh = True
        for node_id in required:
            node = nodes.get(node_id)
            if node is None or node.get("stale") or str(node.get("source_topic", "")).startswith("fallback:"):
                fresh = False
                break
        if data.get("type") == "node_state" and data.get("source") == "ros_odometry" and fresh:
            sys.exit(0)
    except Exception:
        pass
    time.sleep(0.2)
sys.exit(1)
PY
}

can_import_rclpy() {
  python3 - <<'PY' >/dev/null 2>&1
import rclpy
PY
}

NODE_STATE_FILE="$RUN_DIR/logs/node_state.json"

log "live SINR demo"
log "Run directory: $RUN_DIR"

if [[ "$PROVIDER_MODE" == "test_free_space" ]]; then
  log "WARNING: provider mode test_free_space is development-only and is not customer acceptance evidence"
fi

if (( START_PROVIDER )); then
  log "Starting Sionna provider mode=$PROVIDER_MODE radio_config=$RADIO_FILE"
  SIONNA_PROVIDER_MODE="$PROVIDER_MODE" "$ROOT_DIR/network/scripts/run_sionna_provider.sh" \
    --scenario "$SCENARIO_FILE" \
    --radio-config "$RADIO_FILE" \
    --jammers-config "$JAMMERS_FILE" \
    > "$RUN_DIR/logs/sionna_provider.log" 2>&1 &
  SIONNA_PID=$!
else
  log "Using already-running Sionna provider; expected radio_config=$RADIO_FILE"
fi
wait_for_tcp 127.0.0.1 5090 60

if (( START_TRACKER )) && [[ "$SOURCE" != "replay" ]]; then
  if can_import_rclpy; then
	    log "Starting ROS position tracker"
	    "$ROOT_DIR/network/scripts/run_position_tracker.sh" \
	      --scenario "$SCENARIO_FILE" \
	      --jammers-config "$JAMMERS_FILE" \
	      --output-json "$NODE_STATE_FILE" \
	      --output-jsonl "$RUN_DIR/logs/node_state.jsonl" \
	      > "$RUN_DIR/logs/position_tracker.log" 2>&1 &
    TRACKER_PID=$!
	  elif [[ "$SOURCE" == "ros" ]]; then
	    log "FAIL source=ros requires rclpy/ROS 2 Python packages"
	    exit 2
	  else
    log "ROS position tracker unavailable; source=auto will use replay until node-state appears"
	  fi
	fi

if [[ "$SOURCE" == "ros" ]]; then
  log "Waiting for ROS node-state from position tracker"
  if ! wait_for_node_state "$NODE_STATE_FILE" "$ROS_NODE_STATE_TIMEOUT_S" "$TX" "$RX"; then
    log "FAIL source=ros did not produce fresh $TX/$RX node-state within ${ROS_NODE_STATE_TIMEOUT_S}s"
    exit 2
  fi
fi

if (( WITH_NS3 )); then
  log "Starting ns-3 packet core with periodic Sionna updates radio_config=$RADIO_FILE"
  SCENARIO_FILE="$SCENARIO_FILE" \
    JAMMERS_FILE="$JAMMERS_FILE" \
    NODE_STATE_FILE="$NODE_STATE_FILE" \
    RADIO_FILE="$RADIO_FILE" \
    "$ROOT_DIR/network/ns3/run_ns3_core.sh" --duration "$DURATION_S" \
    > "$RUN_DIR/logs/ns3_wrapper.log" 2>&1 &
  NS3_PID=$!
fi

log "Starting live SINR monitor $TX->$RX/$TRAFFIC_CLASS radio_config=$RADIO_FILE"
MONITOR_ARGS=(
  "$ROOT_DIR/network/radio_provider/live_sinr_monitor.py"
  --scenario "$SCENARIO_FILE"
  --run-dir "$RUN_DIR" \
  --jammers-config "$JAMMERS_FILE"
  --radio-config "$RADIO_FILE" \
  --node-state "$NODE_STATE_FILE"
  --source "$SOURCE"
  --duration "$DURATION_S"
  --rate-hz "$RATE_HZ"
  --tx "$TX"
  --rx "$RX"
  --traffic-class "$TRAFFIC_CLASS"
  --deadline-ms "$DEADLINE_MS"
  --replay-amplitude-m "$REPLAY_AMPLITUDE_M"
  --replay-period-s "$REPLAY_PERIOD_S"
)
if [[ -n "$REPLAY_MOVING_NODE" ]]; then
  MONITOR_ARGS+=(--replay-moving-node "$REPLAY_MOVING_NODE")
fi
python3 "${MONITOR_ARGS[@]}" > "$RUN_DIR/logs/live_sinr_monitor.log" 2>&1

if (( WITH_NS3 )) && [[ -n "${NS3_PID:-}" ]]; then
  log "Waiting for ns-3"
  wait "$NS3_PID"
fi

log "live SINR demo complete"
printf 'Run directory: %s\n' "$RUN_DIR"
