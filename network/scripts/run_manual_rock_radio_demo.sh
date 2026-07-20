#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUN_ID="${RUN_ID:-manual_rock_radio_$(date -u +%Y%m%dT%H%M%SZ)}"
RUN_DIR="${RUN_DIR:-$ROOT_DIR/runs/$RUN_ID}"
DURATION_S="${DURATION_S:-300}"
PERIOD_S="${PERIOD_S:-1}"
TX="${LIVE_RSSI_TX:-uav1}"
RX="${LIVE_RSSI_RX:-uav2}"
GUI="${GUI:-true}"
RVIZ="${RVIZ:-false}"
DASHBOARD_HOST="${DASHBOARD_HOST:-127.0.0.1}"
DASHBOARD_PORT="${DASHBOARD_PORT:-8765}"
SETUP_NS3_SIONNA="${SETUP_NS3_SIONNA:-1}"
SCENARIO_FILE="${SCENARIO_FILE:-$ROOT_DIR/network/config/scenario_rock_demo.yaml}"
ENDPOINTS_FILE="${ENDPOINTS_FILE:-$ROOT_DIR/network/config/endpoints.yaml}"
SCENE_XML="${SCENE_XML:-$ROOT_DIR/src/multiagent_simulation/worlds/rock_demo/sionna_scene.xml}"
NODE_STATE_FILE="$RUN_DIR/logs/node_state.json"
GCS_UAV="${GCS_UAV:-$RX}"
GCS_BRIDGE_PORT="${GCS_BRIDGE_PORT:-14600}"
ALLOW_DIRECT_SITL_PORTS="${ALLOW_DIRECT_SITL_PORTS:-false}"
ORIGINAL_ARGS=("$@")

usage() {
  cat <<'EOF'
Usage: network/scripts/run_manual_rock_radio_demo.sh [options]

Starts the matched Gazebo/Sionna rock-demo scene for manual flying and serves a
live RSSI/SNR dashboard backed by ns-3's upstream Sionna RT pybind11 channel.

Run this inside the project ROS/Gazebo container or a shell with ROS 2 sourced.

Options:
  --duration SECONDS       Live radio run duration. Default: 300
  --period SECONDS         Sionna channel update/sample period. Default: 1
  --tx NODE                Link transmitter. Default: uav1
  --rx NODE                Link receiver. Default: uav2
  --gui true|false         Gazebo GUI. Default: true
  --rviz true|false        RViz. Default: false
  --dashboard-port PORT    Dashboard port. Default: 8765
  --gcs-uav NODE           Manual GCS target UAV. Default: --rx value
  --gcs-bridge-port PORT   Manual GCS bridge TCP port. Default: 14600
  --allow-direct-gcs
                           Print direct SITL master ports as NON-P0 legacy
                           convenience. Off by default.
  --allow-direct-sitl-ports
                           Alias for --allow-direct-gcs.
  --no-setup               Skip ns-3 Sionna RT setup/configure
  -h, --help               Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --duration)
      DURATION_S="${2:?missing duration}"
      shift 2
      ;;
    --period)
      PERIOD_S="${2:?missing period}"
      shift 2
      ;;
    --tx)
      TX="${2:?missing tx}"
      shift 2
      ;;
    --rx)
      RX="${2:?missing rx}"
      shift 2
      ;;
    --gui)
      GUI="${2:?missing gui value}"
      shift 2
      ;;
    --rviz)
      RVIZ="${2:?missing rviz value}"
      shift 2
      ;;
    --dashboard-port)
      DASHBOARD_PORT="${2:?missing dashboard port}"
      shift 2
      ;;
    --gcs-uav)
      GCS_UAV="${2:?missing GCS UAV}"
      shift 2
      ;;
    --gcs-bridge-port)
      GCS_BRIDGE_PORT="${2:?missing GCS bridge port}"
      shift 2
      ;;
    --allow-direct-gcs|--allow-direct-sitl-ports)
      ALLOW_DIRECT_SITL_PORTS=true
      shift
      ;;
    --no-setup)
      SETUP_NS3_SIONNA=0
      shift
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

mkdir -p "$RUN_DIR"/{logs,metrics,plots,pcap}
printf '%q ' "$0" "${ORIGINAL_ARGS[@]}" > "$RUN_DIR/command.txt"
printf '\n' >> "$RUN_DIR/command.txt"

{
  printf 'run_id=%s\n' "$RUN_ID"
  printf 'utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'root=%s\n' "$ROOT_DIR"
  printf 'scenario=%s\n' "$SCENARIO_FILE"
  printf 'scene_xml=%s\n' "$SCENE_XML"
  printf 'endpoints=%s\n' "$ENDPOINTS_FILE"
  printf 'node_state_file=%s\n' "$NODE_STATE_FILE"
  printf 'duration_s=%s\n' "$DURATION_S"
  printf 'period_s=%s\n' "$PERIOD_S"
  printf 'tx=%s\n' "$TX"
  printf 'rx=%s\n' "$RX"
  printf 'gcs_uav=%s\n' "$GCS_UAV"
  printf 'gcs_bridge_endpoint=tcp:%s:%s\n' "127.0.0.1" "$GCS_BRIDGE_PORT"
  printf 'direct_sitl_ports_printed=%s\n' "$ALLOW_DIRECT_SITL_PORTS"
  printf 'manual_gcs_p0_guard=direct SITL ports are NON-P0 and off by default\n'
  printf 'dashboard=http://%s:%s/\n' "$DASHBOARD_HOST" "$DASHBOARD_PORT"
} > "$RUN_DIR/environment.txt"

log() {
  printf '%s\n' "$*" | tee -a "$RUN_DIR/logs/manual_demo.log"
}

cleanup() {
  local pid
  for pid in ${MANUAL_GCS_PID:-} ${BRIDGE_PID:-} ${RADIO_PID:-} ${DASHBOARD_PID:-} ${TRACKER_PID:-} ${LAUNCH_PID:-} ${TCPDUMP_PIDS:-}; do
    if [[ -n "${pid:-}" ]] && kill -0 "$pid" >/dev/null 2>&1; then
      kill "$pid" >/dev/null 2>&1 || true
      wait "$pid" >/dev/null 2>&1 || true
    fi
  done
}
trap cleanup EXIT

wait_for_node_state() {
  local path="$1"
  local timeout_s="$2"
  python3 - "$path" "$timeout_s" "$TX" "$RX" <<'PY'
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
        nodes = {str(n.get("id")): n for n in data.get("nodes", []) if isinstance(n, dict)}
        ok = data.get("source") == "ros_odometry"
        for node_id in required:
            node = nodes.get(node_id)
            ok = ok and node is not None and not node.get("stale") and not str(node.get("source_topic", "")).startswith("fallback:")
        if ok:
            sys.exit(0)
    except Exception:
        pass
    time.sleep(0.25)
sys.exit(1)
PY
}

manual_gcs_contract() {
  python3 "$ROOT_DIR/network/bridge/manual_gcs_bridge.py" \
    --endpoints "$ENDPOINTS_FILE" \
    --run-id "$RUN_ID" \
    --run-dir "$RUN_DIR" \
    --uav "$GCS_UAV" \
    --bind-port "$GCS_BRIDGE_PORT" \
    --dry-run
}

start_control_tcpdump() {
  local port="$1"
  if command -v tcpdump >/dev/null 2>&1; then
    tcpdump -i lo -U -w "$RUN_DIR/pcap/control.pcap" "udp and port $port" \
      > "$RUN_DIR/logs/tcpdump_manual_control.log" 2>&1 &
    TCPDUMP_PIDS="${TCPDUMP_PIDS:-} $!"
  else
    log "tcpdump not available; manual control PCAP capture skipped"
  fi
}

if ! command -v ros2 >/dev/null 2>&1; then
  log "FAIL ros2 is not available. Run inside the project ROS/Gazebo container."
  exit 2
fi

log "manual rock radio demo"
log "Run directory: $RUN_DIR"
log "Dashboard: http://$DASHBOARD_HOST:$DASHBOARD_PORT/"

if ! manual_gcs_contract > "$RUN_DIR/logs/manual_gcs_contract.json"; then
  log "FAIL manual GCS bridge contract could not be resolved from $ENDPOINTS_FILE"
  exit 2
fi
GCS_ENDPOINT="$(
  python3 - "$RUN_DIR/logs/manual_gcs_contract.json" <<'PY'
import json
import sys
from pathlib import Path

print(json.loads(Path(sys.argv[1]).read_text())["gcs_endpoint"])
PY
)"
log "Manual GCS/QGC endpoint: $GCS_ENDPOINT"
log "Manual GCS commands are routed through network/bridge/manual_gcs_bridge.py and mirrored into priority_udp_bridge/ns-3 ingress."
log "P0 guard: direct SITL master ports are not printed and are not P0 evidence."
if [[ "$ALLOW_DIRECT_SITL_PORTS" == "true" ]]; then
  log "NON-P0 convenience requested: direct SITL master ports will be printed for legacy manual piloting only."
fi

set +u
if [[ -f /opt/ros/humble/setup.bash ]]; then
  # shellcheck source=/dev/null
  source /opt/ros/humble/setup.bash
fi
if [[ -f /workspace/ardu_ws/install/setup.bash ]]; then
  # shellcheck source=/dev/null
  source /workspace/ardu_ws/install/setup.bash
fi
if [[ -f "$ROOT_DIR/install/setup.bash" ]]; then
  # shellcheck source=/dev/null
  source "$ROOT_DIR/install/setup.bash"
fi
set -u

PKG_PREFIX="$(ros2 pkg prefix multiagent_simulation)"
PKG_SHARE="$PKG_PREFIX/share/multiagent_simulation"
export GZ_SIM_RESOURCE_PATH="$PKG_SHARE/models:$PKG_SHARE/worlds:$ROOT_DIR/src:${GZ_SIM_RESOURCE_PATH:-}"
export SDF_PATH="$PKG_SHARE/models:$PKG_SHARE/worlds:$ROOT_DIR/src:${SDF_PATH:-}"
log "GZ_SIM_RESOURCE_PATH=$GZ_SIM_RESOURCE_PATH"
log "SDF_PATH=$SDF_PATH"

log "Launching Gazebo/ArduPilot rock_demo world"
ros2 launch multiagent_simulation multiagent_simulation.launch.py \
  world_file:=rock_demo/rock_demo.sdf \
  robots_config_file:="$SCENARIO_FILE" \
  robot_model:=iris_radio_headless \
  gui:="$GUI" \
  rviz:="$RVIZ" \
  use_mapping_camera:=false \
  use_navigation_camera:=false \
  use_zed_camera:=false \
  > "$RUN_DIR/logs/ros_gazebo_launch.log" 2>&1 &
LAUNCH_PID=$!

sleep 5
log "Starting ROS position tracker"
"$ROOT_DIR/network/scripts/run_position_tracker.sh" \
  --scenario "$SCENARIO_FILE" \
  --jammers-config "$ROOT_DIR/network/config/jammers_rock_demo.yaml" \
  --output-json "$NODE_STATE_FILE" \
  --output-jsonl "$RUN_DIR/logs/node_state.jsonl" \
  > "$RUN_DIR/logs/position_tracker.log" 2>&1 &
TRACKER_PID=$!

log "Waiting for fresh $TX/$RX odometry"
if ! wait_for_node_state "$NODE_STATE_FILE" 60; then
  log "FAIL no fresh ROS odometry for $TX/$RX within 60s"
  exit 2
fi

CONTROL_BRIDGE_PORT="$(
  python3 - "$RUN_DIR/logs/manual_gcs_contract.json" <<'PY'
import json
import sys
from pathlib import Path

record = json.loads(Path(sys.argv[1]).read_text())
print(str(record["bridge_ingress"]).rsplit(":", 1)[1])
PY
)"

log "Starting priority UDP bridge for manual GCS control ingress"
python3 "$ROOT_DIR/network/bridge/priority_udp_bridge.py" \
  --endpoints "$ENDPOINTS_FILE" \
  --log "$RUN_DIR/logs/bridge.jsonl" \
  > "$RUN_DIR/logs/bridge_stdout.log" 2>&1 &
BRIDGE_PID=$!
sleep 1
if ! kill -0 "$BRIDGE_PID" >/dev/null 2>&1; then
  wait "$BRIDGE_PID" || true
  log "FAIL priority UDP bridge exited before manual GCS endpoint was ready"
  exit 2
fi
start_control_tcpdump "$CONTROL_BRIDGE_PORT"

CSV="$RUN_DIR/metrics/ns3_sionna_rt_live.csv"
log "Starting dashboard"
python3 "$ROOT_DIR/network/scripts/live_radio_dashboard.py" \
  --csv "$CSV" \
  --host "$DASHBOARD_HOST" \
  --port "$DASHBOARD_PORT" \
  > "$RUN_DIR/logs/live_radio_dashboard.log" 2>&1 &
DASHBOARD_PID=$!

log "Starting ns-3 pybind11 Sionna RT live RSSI/SNR"
RADIO_ARGS=(
  "$ROOT_DIR/network/ns3/run_ns3_sionna_rt_live.sh"
  --scene "$SCENE_XML"
  --node-state "$NODE_STATE_FILE"
  --tx "$TX"
  --rx "$RX"
  --duration "$DURATION_S"
  --period "$PERIOD_S"
)
if (( ! SETUP_NS3_SIONNA )); then
  RADIO_ARGS+=(--no-setup)
fi
RUN_ID="$RUN_ID" RUN_DIR="$RUN_DIR" "${RADIO_ARGS[@]}" > "$RUN_DIR/logs/ns3_sionna_rt_runner.log" 2>&1 &
RADIO_PID=$!

log "Starting fail-closed manual GCS bridge"
python3 "$ROOT_DIR/network/bridge/manual_gcs_bridge.py" \
  --endpoints "$ENDPOINTS_FILE" \
  --run-id "$RUN_ID" \
  --run-dir "$RUN_DIR" \
  --uav "$GCS_UAV" \
  --bind-port "$GCS_BRIDGE_PORT" \
  --bridge-log "$RUN_DIR/logs/bridge.jsonl" \
  --ns3-trace "$CSV" \
  > "$RUN_DIR/logs/manual_gcs_bridge_stdout.log" 2>&1 &
MANUAL_GCS_PID=$!
sleep 1
if ! kill -0 "$MANUAL_GCS_PID" >/dev/null 2>&1; then
  wait "$MANUAL_GCS_PID" || true
  log "FAIL manual GCS bridge exited before the operator endpoint was ready"
  exit 2
fi

log "Ready. Open http://$DASHBOARD_HOST:$DASHBOARD_PORT/ and fly $GCS_UAV behind/around the rock."
log "Connect external GCS/QGC to the bridge endpoint: $GCS_ENDPOINT"
log "This endpoint is the only default manual command path intended for P0 packet-path evidence."
if [[ "$ALLOW_DIRECT_SITL_PORTS" == "true" ]]; then
  log "NON-P0 convenience direct SITL ports: uav1 tcp:127.0.0.1:5760, uav2 tcp:127.0.0.1:5770, uav3 tcp:127.0.0.1:5780, uav4 tcp:127.0.0.1:5790, uav5 tcp:127.0.0.1:5800."
  log "Any run using direct SITL ports remains NON-P0 manual convenience and must not be claimed as no-bypass packet-path evidence."
else
  log "Direct SITL master ports are intentionally hidden. Use --allow-direct-gcs only for NON-P0 legacy convenience."
fi
{
  printf 'Manual GCS P0 guard\n'
  printf 'PASS operator_gcs_endpoint=%s\n' "$GCS_ENDPOINT"
  printf 'PASS operator endpoint is network/bridge/manual_gcs_bridge.py, not a direct SITL master port\n'
  printf 'PASS priority bridge ingress udp:127.0.0.1:%s is captured in pcap/control.pcap when tcpdump is available\n' "$CONTROL_BRIDGE_PORT"
  if [[ "$ALLOW_DIRECT_SITL_PORTS" == "true" ]]; then
    printf 'FAIL direct SITL ports were printed by explicit request; this manual run is NON-P0 convenience\n'
  else
    printf 'PASS direct SITL ports were not printed by the launcher\n'
  fi
  printf 'NOTE P0 packet-path eligibility for manual commands is computed in metrics/manual_gcs_bridge_summary.json after operator traffic is observed.\n'
} > "$RUN_DIR/logs/manual_p0_guard.log"

wait "$RADIO_PID"
log "Radio run complete"

python3 "$ROOT_DIR/network/scripts/plot_live_radio_csv.py" \
  --input "$CSV" \
  --output "$RUN_DIR/plots/manual_live_radio.png" \
  --title "Manual rock radio $TX->$RX"

printf 'Run directory: %s\n' "$RUN_DIR"
