#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUN_ID="${RUN_ID:-sim_2_4ghz_$(date -u +%Y%m%dT%H%M%SZ)}"
RUN_DIR="${RUN_DIR:-$ROOT_DIR/runs/$RUN_ID}"
LOOP_DURATION_S="${LOOP_DURATION_S:-8}"
HEATMAP_GRID_POINTS="${HEATMAP_GRID_POINTS:-7}"
FIVE_UAV_LAUNCH_TIMEOUT_S="${FIVE_UAV_LAUNCH_TIMEOUT_S:-25}"
RUN_FIVE_UAV_LAUNCH="${RUN_FIVE_UAV_LAUNCH:-1}"

export AMS_RADIO_BACKEND="${AMS_RADIO_BACKEND:-sim_2_4ghz}"
export RUN_ID RUN_DIR

if [[ "$AMS_RADIO_BACKEND" != "sim_2_4ghz" ]]; then
  printf 'FAIL this runner only accepts AMS_RADIO_BACKEND=sim_2_4ghz, got %s\n' "$AMS_RADIO_BACKEND" >&2
  exit 64
fi

mkdir -p "$RUN_DIR"/{logs,pcap,flowmon,heatmaps,metrics,ns3}

printf '%q ' "$0" "$@" > "$RUN_DIR/command.txt"
printf '\n' >> "$RUN_DIR/command.txt"

{
  printf 'run_id=%s\n' "$RUN_ID"
  printf 'utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'root=%s\n' "$ROOT_DIR"
  printf 'radio_backend=%s\n' "$AMS_RADIO_BACKEND"
  printf 'loop_duration_s=%s\n' "$LOOP_DURATION_S"
  printf 'git_head=%s\n' "$(git -C "$ROOT_DIR" rev-parse --short HEAD 2>/dev/null || printf unknown)"
  printf 'kernel=%s\n' "$(uname -a)"
} > "$RUN_DIR/environment.txt"

log() {
  printf '%s\n' "$*" | tee -a "$RUN_DIR/logs/launch.log"
}

cleanup() {
  local pid
  for pid in ${TCPDUMP_PIDS:-} ${BRIDGE_PID:-} ${SIONNA_PID:-}; do
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

run_five_uav_launch_probe() {
  local log_file="$RUN_DIR/logs/five_uav_launch.log"
  : > "$log_file"
  {
    printf 'Five-UAV launch probe\n'
    printf 'Command: ros2 launch multiagent_simulation multiagent_simulation.launch.py robots_config_file:=%s gui:=false rviz:=false use_mapping_camera:=false use_navigation_camera:=false use_zed_camera:=false\n' "$ROOT_DIR/network/config/scenario_5uav.yaml"
    python3 - "$ROOT_DIR/network/config/scenario_5uav.yaml" <<'PY'
import sys
from pathlib import Path
import yaml

robots = (yaml.safe_load(Path(sys.argv[1]).read_text()) or {}).get("robots", [])
for idx, robot in enumerate(robots, start=1):
    print(f"{robot.get('name')} system_id={robot.get('system_id', idx)} launch_index={idx}")
PY
  } >> "$log_file" 2>&1

  if (( RUN_FIVE_UAV_LAUNCH == 0 )); then
    printf 'FIVE_UAV_LAUNCH_PROOF passed=false reason=disabled\n' >> "$log_file"
    return 0
  fi

  set +e
  timeout -k 5s "$FIVE_UAV_LAUNCH_TIMEOUT_S" ros2 launch multiagent_simulation multiagent_simulation.launch.py \
    robots_config_file:="$ROOT_DIR/network/config/scenario_5uav.yaml" \
    gui:=false rviz:=false use_mapping_camera:=false use_navigation_camera:=false use_zed_camera:=false \
    >> "$log_file" 2>&1
  local rc=$?
  set -e
  if [[ "$rc" == "124" ]]; then
    printf 'FIVE_UAV_LAUNCH_PROOF passed=true reason=launch_remained_active_for_%ss\n' "$FIVE_UAV_LAUNCH_TIMEOUT_S" >> "$log_file"
  elif [[ "$rc" == "0" ]]; then
    printf 'FIVE_UAV_LAUNCH_PROOF passed=true reason=launch_exited_zero\n' >> "$log_file"
  else
    printf 'FIVE_UAV_LAUNCH_PROOF passed=false exit_code=%s\n' "$rc" >> "$log_file"
  fi
  pkill -f 'ros2 launch multiagent_simulation|gz sim|ardupilot|micro_ros_agent|parameter_bridge|robot_state_publisher' >/dev/null 2>&1 || true
}

start_tcpdump() {
  local class="$1"
  local filter="$2"
  local file="$RUN_DIR/pcap/$class.pcap"
  if ! command -v tcpdump >/dev/null 2>&1; then
    printf 'FAIL tcpdump is required for %s packet-path capture\n' "$class" >&2
    return 1
  fi
  tcpdump -i lo -U -w "$file" "$filter" > "$RUN_DIR/logs/tcpdump_$class.log" 2>&1 &
  local pid=$!
  sleep 0.25
  if ! kill -0 "$pid" >/dev/null 2>&1; then
    wait "$pid" || true
    printf 'FAIL tcpdump did not remain active for %s; see %s\n' \
      "$class" "$RUN_DIR/logs/tcpdump_$class.log" >&2
    return 1
  fi
  TCPDUMP_PIDS="${TCPDUMP_PIDS:-} $pid"
}

generate_bridge_traffic() {
  local class="$1"
  local rate="$2"
  for uav in uav1 uav2 uav3 uav4 uav5; do
    python3 "$ROOT_DIR/network/bridge/traffic_generator.py" \
      --uav "$uav" \
      --traffic-class "$class" \
      --rate-bps "$rate" \
      --duration-s 1.2 \
      --payload-size 160 \
      >> "$RUN_DIR/logs/traffic_${class}.jsonl" 2>&1 || true
  done
}

log "sim_2_4ghz packet-in-the-loop runtime"
log "Run directory: $RUN_DIR"

log "Running dependency check"
set +e
"$ROOT_DIR/network/scripts/check_deps.sh" > "$RUN_DIR/logs/check_deps.log" 2>&1
CHECK_DEPS_RC=$?
set -e
printf '%s\n' "$CHECK_DEPS_RC" > "$RUN_DIR/logs/check_deps.log.exit_code"
if (( CHECK_DEPS_RC != 0 )); then
  log "FAIL dependency check exited $CHECK_DEPS_RC"
  exit "$CHECK_DEPS_RC"
fi

log "Recording source/config/dependency provenance"
python3 "$ROOT_DIR/network/scripts/write_run_provenance.py" --run-dir "$RUN_DIR" \
  > "$RUN_DIR/logs/provenance.log" 2>&1

log "Rendering bridge configs"
python3 "$ROOT_DIR/network/bridge/bridge_config.py" --render --run-dir "$RUN_DIR" >> "$RUN_DIR/logs/bridge_render.log" 2>&1

log "Running five-UAV launch probe"
run_five_uav_launch_probe

log "Starting real Sionna RT provider"
SIONNA_PROVIDER_MODE=real_sionna "$ROOT_DIR/network/scripts/run_sionna_provider.sh" \
  > "$RUN_DIR/logs/sionna_provider.log" 2>&1 &
SIONNA_PID=$!
wait_for_tcp 127.0.0.1 5090 30

log "Starting bridge and traffic capture"
python3 "$ROOT_DIR/network/bridge/priority_udp_bridge.py" --log "$RUN_DIR/logs/bridge.jsonl" \
  > "$RUN_DIR/logs/bridge_stdout.log" 2>&1 &
BRIDGE_PID=$!
sleep 1
start_tcpdump control 'udp and portrange 14600-14605'
start_tcpdump payload 'udp and portrange 14700-14705'
start_tcpdump additional_data 'udp and portrange 14800-14805'
sleep 1
generate_bridge_traffic control 2000
generate_bridge_traffic payload 300000
generate_bridge_traffic additional_data 120000
sleep 1

log "Running ns-3 packet core against live Sionna"
"$ROOT_DIR/network/ns3/run_ns3_core.sh" --duration "$LOOP_DURATION_S" >> "$RUN_DIR/logs/ns3_wrapper.log" 2>&1

log "Generating Sionna heatmaps"
"$ROOT_DIR/network/scripts/generate_radio_heatmaps.sh" --include-jammers --grid-points "$HEATMAP_GRID_POINTS" \
  >> "$RUN_DIR/logs/heatmaps.log" 2>&1

log "Running HitL virtual endpoint loopback through timing supervisor"
"$ROOT_DIR/network/scripts/run_hitl_loopback.sh" --mode both --run-id "$RUN_ID" --run-dir "$RUN_DIR" \
  >> "$RUN_DIR/logs/hitl_loopback.log" 2>&1

log "Running no-bypass smoke"
"$ROOT_DIR/network/tests/check_no_bypass.sh" > "$RUN_DIR/logs/no_bypass.log" 2>&1

log "Post-processing validation artifacts"
python3 "$ROOT_DIR/network/scripts/postprocess_sim_2_4ghz_run.py" --run-dir "$RUN_DIR" \
  >> "$RUN_DIR/logs/postprocess.log" 2>&1

log "sim_2_4ghz loop complete"
printf 'Run directory: %s\n' "$RUN_DIR"
