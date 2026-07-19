#!/usr/bin/env bash
set -Eeuo pipefail
umask 0002

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUN_ID="${RUN_ID:?RUN_ID is required}"
RUN_DIR="${RUN_DIR:-$ROOT_DIR/runs/$RUN_ID}"
NS3_DIR="${NS3_DIR:-$ROOT_DIR/.external/ns-3}"
NS3_BINARY="$NS3_DIR/build/scratch/ns3.40-ams-tap-packet-engine-default"
PACKET_SOURCE="$ROOT_DIR/network/ns3/scratch/ams-tap-packet-engine.cc"
COPIED_SOURCE="$NS3_DIR/scratch/ams-tap-packet-engine.cc"
RECEIPT_TOOL="$ROOT_DIR/network/ns3/ns3_build_receipt.py"
CONFIG_TOOL="$ROOT_DIR/network/ns3/tap_packet_engine_config.py"
NS3_RUNNER="$ROOT_DIR/network/ns3/run_ns3_tap_packet_engine.sh"
PROBE="$ROOT_DIR/network/scripts/m3_external_matrix_probe.py"
CAPTURE_TOOL="$ROOT_DIR/network/scripts/raw_packet_capture.py"
TOPOLOGY_MONITOR="$ROOT_DIR/network/scripts/m3_topology_monitor.py"
VALIDATOR="$ROOT_DIR/network/scripts/validate_m3_external_matrix.py"
PROVENANCE_TOOL="$ROOT_DIR/network/scripts/write_run_provenance.py"
ACTUAL_ADAPTER="$ROOT_DIR/network/bridge/actual_sitl_mavlink_endpoint.py"
OPAQUE_RELAY="$ROOT_DIR/network/bridge/opaque_udp_relay.py"
CLOCK_BEACON="$ROOT_DIR/network/bridge/runtime_clock_beacon.py"
ACTUAL_ORCHESTRATOR="$ROOT_DIR/network/scripts/actual_sitl_endpoint_orchestrator.py"
ACTUAL_CONTROL_PROBE="$ROOT_DIR/network/scripts/actual_sitl_control_probe.py"
MAVPROXY_SCRIPT="/home/ubuntu/.local/bin/mavproxy.py"
MAVLINK_PYTHON="/usr/bin/python3.10"
MAVLINK_PYTHON_SITE="/home/ubuntu/.local/lib/python3.10/site-packages"
MATRIX="$ROOT_DIR/network/config/endpoint_matrix_5uav.json"
ENDPOINT_SCHEMA="$ROOT_DIR/network/config/endpoint_transaction_schema.json"
FLIGHT_SCENARIO="$ROOT_DIR/network/config/scenario_5uav.yaml"
M2_RECEIPT="${M2_RECEIPT:-/run/ams/prerequisites/m2.json}"
REQUIRED_MODULES="applications,bridge,core,csma,flow-monitor,internet,mobility,network,stats,tap-bridge,traffic-control"
ENDPOINTS=(gcs uav1 uav2 uav3 uav4 uav5)
UAVS=(uav1 uav2 uav3 uav4 uav5)
RUNTIME_ID="${RUNTIME_ID:-$(python3 -c 'import secrets; print(secrets.token_hex(16))')}"
RUN_NONCE="${RUN_NONCE:-$(python3 -c 'import secrets; print(secrets.token_hex(16))')}"
M3_TECHNICAL_SMOKE="${M3_TECHNICAL_SMOKE:-0}"
RUNTIME_DIR="$RUN_DIR/runtime"
OVERLAY_ROOT="$RUN_DIR/runtime_overlay"
OVERLAY_BUILD="$OVERLAY_ROOT/build"
OVERLAY_INSTALL="$OVERLAY_ROOT/install"
OVERLAY_LOG="$OVERLAY_ROOT/log"
EXPECTED_SHARE="$OVERLAY_INSTALL/multiagent_simulation/share/multiagent_simulation"
RESOLVED_FLIGHT_SCENARIO="$RUN_DIR/raw/resolved_flight_scenario.yaml"
ACTUAL_MANIFEST="$RUN_DIR/raw/actual_sitl_endpoint_manifest.json"
ACTUAL_READY="$RUN_DIR/raw/state/actual-sitl-endpoints.ready.json"
ACTUAL_STOP="$RUN_DIR/raw/state/actual-sitl-endpoints.stop"

if [[ "$(id -u)" != "0" ]]; then
  printf 'FAIL M3 runner requires the component wrapper root user/capability profile\n' >&2
  exit 2
fi
for command in colcon ip python3 ros2 setsid; do
  command -v "$command" >/dev/null || {
    printf 'FAIL required command is absent: %s\n' "$command" >&2
    exit 2
  }
done
if [[ ! -f "$MAVPROXY_SCRIPT" || ! -x "$MAVPROXY_SCRIPT" ]]; then
  printf 'FAIL pinned M3 MAVProxy script is absent or non-executable: %s\n' \
    "$MAVPROXY_SCRIPT" >&2
  exit 2
fi
case ":${PYTHONPATH:-}:" in
  *":$MAVLINK_PYTHON_SITE:"*) ;;
  *) export PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}$MAVLINK_PYTHON_SITE" ;;
esac
mapfile -t MAVLINK_PYTHON_RUNTIME < <(
  "$MAVLINK_PYTHON" - <<'PY'
import pathlib
import sys

import pymavlink

print(pathlib.Path(sys.executable).resolve())
print(int(sys.flags.no_user_site))
print(pathlib.Path(pymavlink.__file__).resolve())
PY
)
if ((${#MAVLINK_PYTHON_RUNTIME[@]} != 3)) || \
  [[ "${MAVLINK_PYTHON_RUNTIME[0]}" != "$MAVLINK_PYTHON" ]] || \
  [[ "${MAVLINK_PYTHON_RUNTIME[1]}" != "1" ]] || \
  [[ "${MAVLINK_PYTHON_RUNTIME[2]}" != "$MAVLINK_PYTHON_SITE/pymavlink/__init__.py" ]]; then
  printf 'FAIL controlled M3 Python/pymavlink runtime is unavailable\n' >&2
  exit 2
fi
REQUIRED_PATHS=(
  "$NS3_BINARY" "$PACKET_SOURCE" "$COPIED_SOURCE" "$RECEIPT_TOOL"
  "$CONFIG_TOOL" "$NS3_RUNNER" "$PROBE" "$CAPTURE_TOOL"
  "$TOPOLOGY_MONITOR" "$VALIDATOR" "$PROVENANCE_TOOL" "$ACTUAL_ADAPTER" "$OPAQUE_RELAY" "$CLOCK_BEACON"
  "$ACTUAL_ORCHESTRATOR" "$ACTUAL_CONTROL_PROBE" "$MATRIX" "$ENDPOINT_SCHEMA" "$FLIGHT_SCENARIO"
)
if [[ "$M3_TECHNICAL_SMOKE" != "1" ]]; then
  REQUIRED_PATHS+=("$M2_RECEIPT")
fi
for path in "${REQUIRED_PATHS[@]}"; do
  [[ -e "$path" ]] || {
    printf 'FAIL required M3 artifact is absent: %s\n' "$path" >&2
    exit 2
  }
done
[[ -x "$NS3_BINARY" ]] || {
  printf 'FAIL packet engine is not executable: %s\n' "$NS3_BINARY" >&2
  exit 2
}

AGENT_PIDS=()
ACTUAL_ADAPTER_PIDS=()
FORBIDDEN_LISTENER_PIDS=()
CAPTURE_PIDS=()
ACTUAL_CONTROL_PID=""
ACTUAL_SUPERVISOR_PID=""
FLIGHT_LAUNCH_PID=""
FLIGHT_LAUNCH_PGID=""
ENGINE_PID=""
ENGINE_STOP_FILE=""
TOPOLOGY_MONITOR_PID=""
TOPOLOGY_TRANSITION_SEQUENCE=0
NAMESPACES_CREATED=0
SUCCESS=0

cleanup() {
  local exit_code=$?
  set +e
  if [[ -n "$ENGINE_STOP_FILE" ]]; then
    : > "$ENGINE_STOP_FILE"
  fi
  if [[ -n "$ENGINE_PID" ]] && kill -0 "$ENGINE_PID" 2>/dev/null; then
    kill -TERM "$ENGINE_PID" 2>/dev/null
  fi
  if [[ -n "$TOPOLOGY_MONITOR_PID" ]] && kill -0 "$TOPOLOGY_MONITOR_PID" 2>/dev/null; then
    TOPOLOGY_TRANSITION_SEQUENCE=$((TOPOLOGY_TRANSITION_SEQUENCE + 1))
    python3 "$TOPOLOGY_MONITOR" stop \
      --run-dir "$RUN_DIR" --run-id "$RUN_ID" \
      --runtime-id "$RUNTIME_ID" --run-nonce "$RUN_NONCE" \
      --sequence "$TOPOLOGY_TRANSITION_SEQUENCE" --timeout-s 3 \
      >/dev/null 2>&1 || kill -TERM "$TOPOLOGY_MONITOR_PID" 2>/dev/null
  fi
  [[ -n "$ACTUAL_STOP" && -d "$RUN_DIR" ]] && : > "$ACTUAL_STOP" 2>/dev/null || true
  for pid in "$ACTUAL_CONTROL_PID" "$ACTUAL_SUPERVISOR_PID" \
    "${ACTUAL_ADAPTER_PIDS[@]}" "${AGENT_PIDS[@]}" \
    "${FORBIDDEN_LISTENER_PIDS[@]}" "${CAPTURE_PIDS[@]}"; do
    [[ -n "$pid" ]] && kill -TERM "$pid" 2>/dev/null
  done
  if [[ -n "$FLIGHT_LAUNCH_PGID" ]]; then
    kill -TERM -- "-$FLIGHT_LAUNCH_PGID" 2>/dev/null
  fi
  wait 2>/dev/null
  if [[ "$NAMESPACES_CREATED" == "1" ]]; then
    for endpoint in "${ENDPOINTS[@]}"; do
      ip netns del "ams-$endpoint" 2>/dev/null
    done
    ip netns del ams-ns3 2>/dev/null
    for index in 1 2 3 4 5; do
      ip link del "ams-tail$index" 2>/dev/null
    done
  fi
  if [[ -d "$RUN_DIR" ]]; then
    chown -R 1000:1000 "$RUN_DIR" 2>/dev/null
  fi
  if [[ "$SUCCESS" == "1" ]]; then
    exit 0
  fi
  exit "$exit_code"
}
trap cleanup EXIT INT TERM

for namespace in ams-gcs ams-ns3 ams-uav1 ams-uav2 ams-uav3 ams-uav4 ams-uav5; do
  if ip netns list | awk '{print $1}' | grep -Fxq "$namespace"; then
    printf 'FAIL namespace already exists; refusing to reuse mutable state: %s\n' "$namespace" >&2
    exit 2
  fi
done
for index in 1 2 3 4 5; do
  if ip link show "ams-tail$index" >/dev/null 2>&1; then
    printf 'FAIL root tail interface already exists: ams-tail%s\n' "$index" >&2
    exit 2
  fi
done

RECEIPT="$(python3 "$RECEIPT_TOOL" verify \
  --ns3-dir "$NS3_DIR" \
  --program ams-tap-packet-engine \
  --project-source "$PACKET_SOURCE" \
  --copied-source "$COPIED_SOURCE" \
  --executable "$NS3_BINARY" \
  --required-modules "$REQUIRED_MODULES")"

mkdir -p "$RUN_DIR"
INITIALIZE_ARGS=(
  initialize
  --run-dir "$RUN_DIR"
  --run-id "$RUN_ID"
  --runtime-id "$RUNTIME_ID"
  --run-nonce "$RUN_NONCE"
  --matrix "$MATRIX"
  --endpoint-schema "$ENDPOINT_SCHEMA"
  --flight-scenario "$FLIGHT_SCENARIO"
  --engine-binary "$NS3_BINARY"
  --ns3-dir "$NS3_DIR"
  --build-receipt "$RECEIPT"
)
if [[ "$M3_TECHNICAL_SMOKE" == "1" ]]; then
  INITIALIZE_ARGS+=(--technical-smoke)
else
  INITIALIZE_ARGS+=(--m2-receipt "$M2_RECEIPT")
fi
python3 "$PROBE" "${INITIALIZE_ARGS[@]}"

mkdir -p "$RUNTIME_DIR"
BUILD_COMMAND=(
  colcon --log-base "$OVERLAY_LOG" build
  --base-paths "$ROOT_DIR/src/multiagent_simulation"
  --build-base "$OVERLAY_BUILD"
  --install-base "$OVERLAY_INSTALL"
)
printf '%q ' "${BUILD_COMMAND[@]}" > "$RUN_DIR/logs/m3_runtime_overlay_build.command"
printf '\n' >> "$RUN_DIR/logs/m3_runtime_overlay_build.command"
set +e
"${BUILD_COMMAND[@]}" > "$RUN_DIR/logs/m3_runtime_overlay_build.log" 2>&1
OVERLAY_BUILD_RC=$?
set -e
printf '%s\n' "$OVERLAY_BUILD_RC" > "$RUN_DIR/logs/m3_runtime_overlay_build.exit_code"
if ((OVERLAY_BUILD_RC != 0)) || [[ ! -f "$OVERLAY_INSTALL/setup.bash" ]]; then
  printf 'FAIL fresh M3 runtime overlay build failed\n' >&2
  exit 1
fi
# shellcheck disable=SC1090
set +u
source "$OVERLAY_INSTALL/setup.bash"
set -u
RESOLVED_SHARE="$(python3 -c 'from ament_index_python.packages import get_package_share_directory; print(get_package_share_directory("multiagent_simulation"))')"
if [[ "$RESOLVED_SHARE" != "$EXPECTED_SHARE" ]]; then
  printf 'FAIL M3 resolved package share is not the fresh overlay: %s\n' "$RESOLVED_SHARE" >&2
  exit 1
fi
export AMS_M1_INSTALLED_SHARE="$EXPECTED_SHARE"
export GZ_SIM_RESOURCE_PATH="$EXPECTED_SHARE/models:$EXPECTED_SHARE/worlds:$EXPECTED_SHARE"
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-$((20 + $(printf '%s' "$RUN_ID" | cksum | awk '{print $1}') % 180))}"
export GZ_PARTITION="${GZ_PARTITION:-ams_${RUN_ID//[^a-zA-Z0-9_]/_}}"
export AMS_RUNTIME_ID="$RUNTIME_ID"
export AMS_RUN_NONCE="$RUN_NONCE"

python3 "$PROVENANCE_TOOL" \
  --run-dir "$RUN_DIR" \
  --qualification-profile m3_component \
  --consumed-node Q0 --consumed-node Q1 --consumed-node Q2 --consumed-node Q3 \
  --packet-ingress-mode tap_bridge_external \
  --medium-model csma_surrogate \
  --radio-provider-id tcp_jsonl_real_sionna \
  --radio-provider-runtime-consumed false \
  --runtime-provider-id not_applicable_pre_m4 \
  --radio-provider-runtime-reason profile_pre_m4 \
  > "$RUN_DIR/logs/provenance_generation.log"
python3 "$RECEIPT_TOOL" verify \
  --ns3-dir "$NS3_DIR" \
  --program ams-tap-packet-engine \
  --project-source "$PACKET_SOURCE" \
  --copied-source "$COPIED_SOURCE" \
  --executable "$NS3_BINARY" \
  --required-modules "$REQUIRED_MODULES" \
  --receipt "$RECEIPT" \
  --copy-to "$RUN_DIR/raw/ns3_build_receipt.json" \
  > "$RUN_DIR/logs/ns3_build_receipt_verify.log"

lifecycle() {
  local details_json="${2:-}"
  if [[ -z "$details_json" ]]; then
    details_json='{}'
  fi
  python3 "$PROBE" lifecycle --run-dir "$RUN_DIR" --event "$1" --details-json "$details_json"
  if [[ -n "$FLIGHT_LAUNCH_PID" ]] && ! kill -0 "$FLIGHT_LAUNCH_PID" 2>/dev/null; then
    printf 'FAIL five-UAV flight launch exited before lifecycle transition: %s\n' "$1" >&2
    exit 2
  fi
  if [[ -n "$ACTUAL_SUPERVISOR_PID" ]] && ! kill -0 "$ACTUAL_SUPERVISOR_PID" 2>/dev/null; then
    printf 'FAIL actual-SITL lineage supervisor exited before transition: %s\n' "$1" >&2
    exit 2
  fi
  for pid in "${ACTUAL_ADAPTER_PIDS[@]}"; do
    if ! kill -0 "$pid" 2>/dev/null; then
      printf 'FAIL actual-SITL adapter exited before transition %s: %s\n' "$1" "$pid" >&2
      exit 2
    fi
  done
  if [[ -n "$TOPOLOGY_MONITOR_PID" ]]; then
    kill -0 "$TOPOLOGY_MONITOR_PID" || {
      printf 'FAIL continuous topology monitor exited before transition: %s\n' "$1" >&2
      exit 2
    }
    TOPOLOGY_TRANSITION_SEQUENCE=$((TOPOLOGY_TRANSITION_SEQUENCE + 1))
    python3 "$TOPOLOGY_MONITOR" notify \
      --run-dir "$RUN_DIR" --run-id "$RUN_ID" \
      --runtime-id "$RUNTIME_ID" --run-nonce "$RUN_NONCE" \
      --sequence "$TOPOLOGY_TRANSITION_SEQUENCE" --event "$1" --timeout-s 5
  fi
}

python3 -u "$TOPOLOGY_MONITOR" run \
  --run-dir "$RUN_DIR" --run-id "$RUN_ID" \
  --runtime-id "$RUNTIME_ID" --run-nonce "$RUN_NONCE" --interval-ms 500 \
  > "$RUN_DIR/logs/topology-monitor.stdout" \
  2> "$RUN_DIR/logs/topology-monitor.stderr" &
TOPOLOGY_MONITOR_PID=$!
topology_ready_deadline=$((SECONDS + 10))
while [[ ! -s "$RUN_DIR/raw/topology_monitor/ready.json" ]]; do
  kill -0 "$TOPOLOGY_MONITOR_PID" || {
    printf 'FAIL continuous topology monitor exited before readiness\n' >&2
    exit 2
  }
  (( SECONDS < topology_ready_deadline )) || {
    printf 'FAIL continuous topology monitor readiness timed out\n' >&2
    exit 2
  }
  sleep 0.05
done

lifecycle run_initialized "$(printf '{"runner_pid":%d}' "$$")"

ip netns add ams-ns3
for endpoint in "${ENDPOINTS[@]}"; do
  ip netns add "ams-$endpoint"
done
NAMESPACES_CREATED=1

for endpoint in "${ENDPOINTS[@]}"; do
  if [[ "$endpoint" == "gcs" ]]; then
    index=0
  else
    index="${endpoint#uav}"
  fi
  endpoint_ns="ams-$endpoint"
  endpoint_if="ve-$endpoint"
  peer_if="vp-$endpoint"
  bridge="br-$endpoint"
  tap="tap-$endpoint"
  ip link add "$endpoint_if" type veth peer name "$peer_if"
  ip link set "$endpoint_if" netns "$endpoint_ns"
  ip link set "$peer_if" netns ams-ns3
  ip -n "$endpoint_ns" link set "$endpoint_if" name eth0
  ip -n "$endpoint_ns" link set lo up
  ip -n "$endpoint_ns" link set eth0 address "02:71:$(printf '%02x' "$index"):00:10:10"
  ip -n "$endpoint_ns" link set eth0 txqueuelen 1000
  ip -n "$endpoint_ns" address add "10.71.$index.10/24" dev eth0
  ip -n "$endpoint_ns" link set eth0 up
  ip -n "$endpoint_ns" route add default via "10.71.$index.1" dev eth0

  if [[ "$endpoint" != "gcs" ]]; then
    tail_root="ams-tail$index"
    tail_peer="tail-peer$index"
    ip link add "$tail_root" type veth peer name "$tail_peer"
    ip link set "$tail_peer" netns "$endpoint_ns"
    ip -n "$endpoint_ns" link set "$tail_peer" name tail0
    ip -n "$endpoint_ns" link set tail0 addrgenmode none
    ip -n "$endpoint_ns" link set tail0 address "02:72:$(printf '%02x' "$index"):00:00:02"
    ip -n "$endpoint_ns" address add "10.72.$index.2/30" dev tail0
    ip -n "$endpoint_ns" link set tail0 up
    ip link set "$tail_root" addrgenmode none
    ip link set "$tail_root" address "02:72:$(printf '%02x' "$index"):00:00:01"
    ip address add "10.72.$index.1/30" dev "$tail_root"
    ip link set "$tail_root" up
  fi

  ip -n ams-ns3 link add name "$bridge" type bridge
  ip -n ams-ns3 link set dev "$bridge" type bridge mcast_snooping 0
  ip -n ams-ns3 tuntap add dev "$tap" mode tap
  ip -n ams-ns3 link set "$peer_if" master "$bridge"
  ip -n ams-ns3 link set "$tap" master "$bridge"
  ip -n ams-ns3 link set "$peer_if" txqueuelen 1000
  ip -n ams-ns3 link set "$tap" txqueuelen 1000
  ip -n ams-ns3 link set "$peer_if" up
  ip -n ams-ns3 link set "$tap" up
  ip -n ams-ns3 link set "$bridge" up
done
ip -n ams-ns3 link set lo up

for namespace in ams-ns3 ams-gcs ams-uav1 ams-uav2 ams-uav3 ams-uav4 ams-uav5; do
  ip -n "$namespace" -j -d link show > "$RUN_DIR/raw/topology/$namespace.link.json"
  ip -n "$namespace" -j addr show > "$RUN_DIR/raw/topology/$namespace.addr.json"
  ip -n "$namespace" -j route show table all > "$RUN_DIR/raw/topology/$namespace.route.json"
done
ip -j -d link show > "$RUN_DIR/raw/topology/container-root.link.json"
ip -j addr show > "$RUN_DIR/raw/topology/container-root.addr.json"
ip -j route show table all > "$RUN_DIR/raw/topology/container-root.route.json"
lifecycle topology_ready '{"namespace_count":7,"external_segment_count":11,"root_tail_count":5}'
lifecycle captures_start_requested '{"capture_processes":29,"tail_capture_processes":10}'

wait_for_files() {
  local timeout_s=$1
  shift
  local deadline=$((SECONDS + timeout_s))
  while (( SECONDS < deadline )); do
    local missing=0
    for path in "$@"; do
      [[ -s "$path" ]] || missing=1
    done
    (( missing == 0 )) && return 0
    sleep 0.1
  done
  return 1
}

start_capture() {
  local namespace=$1
  local interface=$2
  local pcap=$3
  local stats=$4
  local log=$5
  local udp_port_filter=${6:-}
  local filter_args=()
  if [[ -n "$udp_port_filter" ]]; then
    filter_args=(--udp-port-filter "$udp_port_filter")
  fi
  if [[ "$namespace" == "container-root" ]]; then
    python3 -u "$CAPTURE_TOOL" \
      --interface "$interface" --pcap "$pcap" --stats "$stats" \
      "${filter_args[@]}" \
      > /dev/null 2> "$log" &
  else
    ip netns exec "$namespace" python3 -u "$CAPTURE_TOOL" \
      --interface "$interface" --pcap "$pcap" --stats "$stats" \
      "${filter_args[@]}" \
      > /dev/null 2> "$log" &
  fi
  CAPTURE_PIDS+=("$!")
}

for endpoint in "${ENDPOINTS[@]}"; do
  start_capture "ams-$endpoint" eth0 \
    "$RUN_DIR/pcap/endpoint-$endpoint.pcap" \
    "$RUN_DIR/logs/capture-endpoint-$endpoint.json" \
    "$RUN_DIR/logs/capture-endpoint-$endpoint.stderr"
  start_capture ams-ns3 "vp-$endpoint" \
    "$RUN_DIR/pcap/ns3-external-$endpoint.pcap" \
    "$RUN_DIR/logs/capture-ns3-external-$endpoint.json" \
    "$RUN_DIR/logs/capture-ns3-external-$endpoint.stderr"
  start_capture "ams-$endpoint" lo \
    "$RUN_DIR/pcap/loopback-$endpoint.pcap" \
    "$RUN_DIR/logs/capture-loopback-$endpoint.json" \
    "$RUN_DIR/logs/capture-loopback-$endpoint.stderr"
done
for index in 1 2 3 4 5; do
  start_capture "ams-uav$index" tail0 \
    "$RUN_DIR/pcap/tail-uav$index.pcap" \
    "$RUN_DIR/logs/capture-tail-uav$index.json" \
    "$RUN_DIR/logs/capture-tail-uav$index.stderr"
  start_capture container-root "ams-tail$index" \
    "$RUN_DIR/pcap/tail-root-uav$index.pcap" \
    "$RUN_DIR/logs/capture-tail-root-uav$index.json" \
    "$RUN_DIR/logs/capture-tail-root-uav$index.stderr"
done
start_capture container-root lo \
  "$RUN_DIR/pcap/loopback-container-root.pcap" \
  "$RUN_DIR/logs/capture-loopback-container-root.json" \
  "$RUN_DIR/logs/capture-loopback-container-root.stderr" \
  "14550,15201,15202,15203,15204,15205,15300,15301,15400,15401,15402,15403,15404,15405,15406,15500,15501,15502,15503,15504,15505,15506,15600,15601,15602,15603,15604,15605,15606,15700,15701,15702,15703,15704,15705,15706"
sleep 0.5
for pid in "${CAPTURE_PIDS[@]}"; do
  kill -0 "$pid" || {
    printf 'FAIL persistent raw capture exited before traffic: %s\n' "$pid" >&2
    exit 2
  }
done
lifecycle captures_started '{"capture_processes":29,"tail_capture_processes":10}'

lifecycle forbidden_listeners_start_requested '{"listener_processes":6,"active_bindings":20}'
for endpoint in "${ENDPOINTS[@]}"; do
  ip netns exec "ams-$endpoint" python3 -u "$PROBE" forbidden-listener \
    --run-dir "$RUN_DIR" --run-id "$RUN_ID" \
    --runtime-id "$RUNTIME_ID" --run-nonce "$RUN_NONCE" \
    --endpoint "$endpoint" \
    > "$RUN_DIR/logs/forbidden-listener-$endpoint.log" 2>&1 &
  FORBIDDEN_LISTENER_PIDS+=("$!")
done
FORBIDDEN_READY_FILES=()
for endpoint in "${ENDPOINTS[@]}"; do
  FORBIDDEN_READY_FILES+=("$RUN_DIR/raw/state/forbidden-listener-$endpoint.ready.json")
done
wait_for_files 15 "${FORBIDDEN_READY_FILES[@]}" || {
  printf 'FAIL forbidden-path listeners did not all become ready\n' >&2
  exit 2
}
for pid in "${FORBIDDEN_LISTENER_PIDS[@]}"; do
  kill -0 "$pid" || {
    printf 'FAIL forbidden-path listener exited after readiness: %s\n' "$pid" >&2
    exit 2
  }
done
lifecycle forbidden_listeners_started '{"listener_processes":6,"active_bindings":20}'

lifecycle endpoint_agents_start_requested '{"endpoint_agents":6}'
for endpoint in "${ENDPOINTS[@]}"; do
  ip netns exec "ams-$endpoint" python3 -u "$PROBE" agent \
    --run-dir "$RUN_DIR" \
    --run-id "$RUN_ID" \
    --runtime-id "$RUNTIME_ID" \
    --run-nonce "$RUN_NONCE" \
    --endpoint "$endpoint" \
    --matrix "$MATRIX" \
    > "$RUN_DIR/logs/agent-$endpoint.log" 2>&1 &
  AGENT_PIDS+=("$!")
done

READY_FILES=()
for endpoint in "${ENDPOINTS[@]}"; do
  READY_FILES+=("$RUN_DIR/raw/state/$endpoint.ready.json")
done
wait_for_files 15 "${READY_FILES[@]}" || {
  printf 'FAIL endpoint agents did not all become ready\n' >&2
  exit 2
}
for pid in "${AGENT_PIDS[@]}"; do
  kill -0 "$pid" || {
    printf 'FAIL endpoint agent exited after readiness: %s\n' "$pid" >&2
    exit 2
  }
done
lifecycle endpoint_agents_started '{"endpoint_agents":6}'

(
  cd "$RUNTIME_DIR"
  exec setsid ros2 launch multiagent_simulation multiagent_simulation.launch.py \
    robots_config_file:="$RESOLVED_FLIGHT_SCENARIO" \
    robot_model:=iris_radio_headless \
    gui:=false rviz:=false headless_rendering:="${HEADLESS_RENDERING:-false}" \
    generate_sensor_models:=false \
    use_mapping_camera:=false \
    use_navigation_camera:=false \
    use_zed_camera:=false
) > "$RUN_DIR/logs/m3_flight_launch.log" 2>&1 &
FLIGHT_LAUNCH_PID=$!
FLIGHT_LAUNCH_PGID=$FLIGHT_LAUNCH_PID
printf '%s\n' "$FLIGHT_LAUNCH_PID" > "$RUN_DIR/logs/m3_flight_launch.pid"

discover_swarm_process_refs() {
  local role=$1
  local timeout_s=$2
  python3 - "$FLIGHT_LAUNCH_PGID" "$role" "$timeout_s" <<'PY'
import os
import re
import sys
import time
from pathlib import Path

pgid = int(sys.argv[1])
role = sys.argv[2]
deadline = time.monotonic() + float(sys.argv[3])

def start_ticks(path: Path) -> int:
    raw = (path / "stat").read_text(encoding="utf-8")
    return int(raw[raw.rfind(")") + 2:].split()[19])

while time.monotonic() < deadline:
    found = {}
    duplicates = set()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            if os.getpgid(pid) != pgid:
                continue
            argv = [
                value.decode("utf-8", errors="replace")
                for value in (entry / "cmdline").read_bytes().split(b"\0")
                if value
            ]
            if not argv:
                continue
            joined = " ".join(argv)
            if role == "mavproxy":
                if "mavproxy.py" not in joined:
                    continue
                match = re.search(r"tcp:127[.]0[.]0[.]1:(5760|5770|5780|5790|5800)", joined)
                if match is None:
                    continue
                index = (int(match.group(1)) - 5760) // 10
            else:
                if Path(argv[0]).name != "arducopter" and not any(
                    Path(value).name == "arducopter" for value in argv
                ):
                    continue
                match = re.search(r"(?:^|\s)(?:-I\s*|--instance(?:=|\s+))(\d+)(?:\s|$)", joined)
                if match is None:
                    sysid = re.search(r"(?:^|\s)--sysid(?:=|\s+)([1-5])(?:\s|$)", joined)
                    if sysid is None:
                        continue
                    index = int(sysid.group(1)) - 1
                else:
                    index = int(match.group(1))
            if not 0 <= index < 5:
                continue
            if index in found:
                duplicates.add(index)
            found[index] = (pid, start_ticks(entry))
        except (OSError, ProcessLookupError, PermissionError, ValueError):
            continue
    if not duplicates and set(found) == set(range(5)):
        for index in range(5):
            pid, ticks = found[index]
            print(f"uav{index + 1}={pid}:{ticks}")
        raise SystemExit(0)
    time.sleep(0.2)
raise SystemExit(1)
PY
}

if ! discover_swarm_process_refs sitl 120 > "$RUN_DIR/logs/m3_sitl_refs.txt"; then
  printf 'FAIL could not identify exact five ArduCopter process identities\n' >&2
  exit 1
fi
if ! discover_swarm_process_refs mavproxy 120 > "$RUN_DIR/logs/m3_mavproxy_refs.txt"; then
  printf 'FAIL could not identify exact five MAVProxy process identities\n' >&2
  exit 1
fi
mapfile -t SITL_REFS < "$RUN_DIR/logs/m3_sitl_refs.txt"
mapfile -t MAVPROXY_REFS < "$RUN_DIR/logs/m3_mavproxy_refs.txt"
[[ "${#SITL_REFS[@]}" == "5" && "${#MAVPROXY_REFS[@]}" == "5" ]] || {
  printf 'FAIL flight process reference cardinality is not five plus five\n' >&2
  exit 1
}
lifecycle flight_stack_started "$(printf '{\"launch_pid\":%d,\"launch_pgid\":%d,\"sitl_processes\":5,\"mavproxy_processes\":5}' "$FLIGHT_LAUNCH_PID" "$FLIGHT_LAUNCH_PGID")"

MANIFEST_ARGS=(
  --build-manifest
  --run-dir "$RUN_DIR"
  --manifest "$ACTUAL_MANIFEST"
  --run-id "$RUN_ID"
  --runtime-id "$RUNTIME_ID"
  --run-nonce "$RUN_NONCE"
  --launch-pgid "$FLIGHT_LAUNCH_PGID"
)
for reference in "${MAVPROXY_REFS[@]}"; do
  MANIFEST_ARGS+=(--mavproxy-ref "$reference")
done
for reference in "${SITL_REFS[@]}"; do
  MANIFEST_ARGS+=(--sitl-ref "$reference")
done
python3 "$ACTUAL_ORCHESTRATOR" "${MANIFEST_ARGS[@]}" \
  > "$RUN_DIR/logs/actual_sitl_manifest.stdout" \
  2> "$RUN_DIR/logs/actual_sitl_manifest.stderr"
lifecycle actual_sitl_manifest_frozen '{"channels":5,"actual_sitl_processes":10,"tail_segments":5}'

for uav in "${UAVS[@]}"; do
  ip netns exec "ams-$uav" python3 -u "$ACTUAL_ADAPTER" \
    --run-dir "$RUN_DIR" --manifest "$ACTUAL_MANIFEST" --uav "$uav" \
    > "$RUN_DIR/logs/actual_sitl_${uav}.stdout" \
    2> "$RUN_DIR/logs/actual_sitl_${uav}.stderr" &
  ACTUAL_ADAPTER_PIDS+=("$!")
done
python3 -u "$ACTUAL_ORCHESTRATOR" \
  --run-dir "$RUN_DIR" --manifest "$ACTUAL_MANIFEST" \
  --ready-file "$ACTUAL_READY" --stop-file "$ACTUAL_STOP" \
  > "$RUN_DIR/logs/actual_sitl_supervisor.stdout" \
  2> "$RUN_DIR/logs/actual_sitl_supervisor.stderr" &
ACTUAL_SUPERVISOR_PID=$!
wait_for_files 90 "$ACTUAL_READY" || {
  printf 'FAIL actual-SITL endpoint supervisor did not become ready\n' >&2
  exit 1
}
kill -0 "$ACTUAL_SUPERVISOR_PID" || {
  printf 'FAIL actual-SITL endpoint supervisor exited after readiness\n' >&2
  exit 1
}
for pid in "${ACTUAL_ADAPTER_PIDS[@]}"; do
  kill -0 "$pid" || {
    printf 'FAIL actual-SITL adapter exited after readiness: %s\n' "$pid" >&2
    exit 1
  }
done
lifecycle actual_sitl_adapters_ready '{"adapter_processes":5,"authorized_channels":5,"tail_segments":5}'

lifecycle actual_control_start_requested '{"control_socket":"10.71.0.10:14600"}'
ip netns exec ams-gcs python3 -u "$ACTUAL_CONTROL_PROBE" \
  --run-dir "$RUN_DIR" --run-id "$RUN_ID" \
  --runtime-id "$RUNTIME_ID" --run-nonce "$RUN_NONCE" --profile m3 --matrix "$MATRIX" \
  > "$RUN_DIR/logs/actual-control.log" 2>&1 &
ACTUAL_CONTROL_PID=$!
wait_for_files 15 "$RUN_DIR/raw/state/actual-control.socket-ready.json" || {
  printf 'FAIL actual-control GCS probe did not bind its sole control socket\n' >&2
  exit 1
}
kill -0 "$ACTUAL_CONTROL_PID" || {
  printf 'FAIL actual-control GCS probe exited after socket readiness\n' >&2
  exit 1
}
lifecycle actual_control_started "$(printf '{\"pid\":%d,\"control_socket\":\"10.71.0.10:14600\"}' "$ACTUAL_CONTROL_PID")"

start_engine() {
  local epoch=$1
  local lifecycle_event=$2
  local config="$RUN_DIR/logs/ns3_epoch${epoch}_config.json"
  local argv_file="$RUN_DIR/logs/ns3_epoch${epoch}.argv"
  local events="$RUN_DIR/logs/ns3_epoch${epoch}_events.jsonl"
  local ready="$RUN_DIR/logs/ns3_epoch${epoch}.ready.json"
  local stop="$RUN_DIR/logs/ns3_epoch${epoch}.stop"
  local pcap_prefix="$RUN_DIR/pcap/ns3-epoch${epoch}"
  rm -f "$ready" "$stop"
  env \
    RUN_DIR="$RUN_DIR" \
    UAV_COUNT=5 \
    EVENT_EPOCH="$epoch" \
    NS3_NS=ams-ns3 \
    NS3_DIR="$NS3_DIR" \
    DURATION_MS=120000 \
    NS3_SEED=42 \
    NS3_RUN="$epoch" \
    SELF_TEST=0 \
    SIONNA_IPC_ENABLED=0 \
    TAP_GCS=tap-gcs \
    TAP_UAVS=tap-uav1,tap-uav2,tap-uav3,tap-uav4,tap-uav5 \
    EVENTS_FILE="$events" \
    PCAP_PREFIX="$pcap_prefix" \
    READY_FILE="$ready" \
    STOP_FILE="$stop" \
    CONFIG_REPORT="$config" \
    ARGV_FILE="$argv_file" \
    "$NS3_RUNNER" \
    > "$RUN_DIR/logs/ns3_epoch${epoch}.stdout" \
    2> "$RUN_DIR/logs/ns3_epoch${epoch}.stderr" &
  ENGINE_PID=$!
  ENGINE_STOP_FILE=$stop
  lifecycle "$lifecycle_event" "$(printf '{"event_epoch":%d,"pid":%d}' "$epoch" "$ENGINE_PID")"
  wait_for_files 15 "$ready" || {
    printf 'FAIL epoch %s packet engine did not become ready\n' "$epoch" >&2
    exit 2
  }
  kill -0 "$ENGINE_PID" || {
    printf 'FAIL epoch %s packet engine exited at readiness\n' "$epoch" >&2
    exit 2
  }
  lifecycle engine_ready "$(printf '{"event_epoch":%d,"pid":%d}' "$epoch" "$ENGINE_PID")"
}

stop_engine() {
  local epoch=$1
  local event_name=$2
  : > "$ENGINE_STOP_FILE"
  set +e
  wait "$ENGINE_PID"
  local exit_code=$?
  set -e
  lifecycle "$event_name" "$(printf '{"event_epoch":%d,"exit_code":%d}' "$epoch" "$exit_code")"
  ENGINE_PID=""
  ENGINE_STOP_FILE=""
  [[ "$exit_code" == "0" ]] || {
    printf 'FAIL epoch %s packet engine exit code: %s\n' "$epoch" "$exit_code" >&2
    exit 2
  }
}

monotonic_ns() {
  python3 -c 'import time; print(time.monotonic_ns())'
}

wait_until_ns() {
  python3 - "$1" <<'PY'
import sys
import time
target = int(sys.argv[1])
while True:
    remaining = target - time.monotonic_ns()
    if remaining <= 0:
        break
    time.sleep(min(0.1, remaining / 1_000_000_000))
PY
}

phase_field() {
  python3 - "$RUN_DIR/raw/phase_contract.json" "$1" <<'PY'
import json
import sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
print(data[sys.argv[2]])
PY
}

phase_window_field() {
  python3 - "$RUN_DIR/raw/phase_contract.json" "$1" "$2" <<'PY'
import json
import sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
window = next(item for item in data["windows"] if item["phase"] == sys.argv[2])
print(window[sys.argv[3]])
PY
}

start_engine 1 engine_started
wait_for_files 30 "$RUN_DIR/raw/state/actual-control.link-ready.json" || {
  printf 'FAIL five real SITL heartbeat paths did not become ready through ns-3\n' >&2
  exit 1
}
kill -0 "$ACTUAL_CONTROL_PID" || {
  printf 'FAIL actual-control GCS probe exited before full link readiness\n' >&2
  exit 1
}
lifecycle actual_control_link_ready '{"uav_links":5,"minimum_real_heartbeats_per_uav":3}'
positive_start=$(( $(monotonic_ns) + 10500000000 ))
python3 "$PROBE" schedule \
  --run-dir "$RUN_DIR" \
  --positive-start-monotonic-ns "$positive_start"
lifecycle schedule_committed '{"windows":4,"positive_cells":30}'

python3 "$PROBE" forbidden-canaries \
  --run-dir "$RUN_DIR" --run-id "$RUN_ID" \
  --runtime-id "$RUNTIME_ID" --run-nonce "$RUN_NONCE" \
  --source-endpoint container-root
for endpoint in "${ENDPOINTS[@]}"; do
  ip netns exec "ams-$endpoint" python3 "$PROBE" forbidden-canaries \
    --run-dir "$RUN_DIR" --run-id "$RUN_ID" \
    --runtime-id "$RUNTIME_ID" --run-nonce "$RUN_NONCE" \
    --source-endpoint "$endpoint"
done
sleep 1
lifecycle forbidden_canaries_completed '{"canary_count":20,"remote_application_delivery":0}'

stop_request="$(phase_field stop_request_monotonic_ns)"
restart_request="$(phase_field restart_request_monotonic_ns)"
wait_until_ns "$stop_request"
lifecycle engine_stop_requested '{"event_epoch":1}'
stop_engine 1 engine_stopped

wait_until_ns "$restart_request"
start_engine 2 engine_restarted

shutdown_not_before=$(( $(phase_window_field recovery end_monotonic_ns) + 500000000 ))
wait_until_ns "$((shutdown_not_before - 2000000000))"
lifecycle actual_control_stop_requested '{"actual_control_processes":1}'
lifecycle endpoint_agents_stop_requested '{"endpoint_agents":6}'

set +e
wait "$ACTUAL_CONTROL_PID"
actual_control_exit=$?
set -e
[[ "$actual_control_exit" == "0" ]] || {
  printf 'FAIL actual-control GCS probe exited nonzero: %s\n' "$actual_control_exit" >&2
  exit 2
}
ACTUAL_CONTROL_PID=""
lifecycle actual_control_stopped '{"exit_code":0,"actual_control_processes":1}'

for pid in "${AGENT_PIDS[@]}"; do
  set +e
  wait "$pid"
  agent_exit=$?
  set -e
  [[ "$agent_exit" == "0" ]] || {
    printf 'FAIL endpoint agent exited nonzero: pid=%s exit=%s\n' "$pid" "$agent_exit" >&2
    exit 2
  }
done
AGENT_PIDS=()
lifecycle endpoint_agents_stopped '{"exit_code":0,"endpoint_agents":6}'

lifecycle forbidden_listeners_stop_requested '{"listener_processes":6}'
: > "$RUN_DIR/raw/forbidden/listeners.stop"
for pid in "${FORBIDDEN_LISTENER_PIDS[@]}"; do
  set +e
  wait "$pid"
  listener_exit=$?
  set -e
  [[ "$listener_exit" == "0" ]] || {
    printf 'FAIL forbidden-path listener exited nonzero: pid=%s exit=%s\n' "$pid" "$listener_exit" >&2
    exit 2
  }
done
FORBIDDEN_LISTENER_PIDS=()
lifecycle forbidden_listeners_stopped '{"exit_code":0,"listener_processes":6}'
stop_engine 2 engine_final_stop

lifecycle actual_sitl_stack_stop_requested '{"adapter_processes":5,"supervisor_processes":1,"flight_process_groups":1}'
: > "$ACTUAL_STOP"
set +e
wait "$ACTUAL_SUPERVISOR_PID"
actual_supervisor_exit=$?
set -e
[[ "$actual_supervisor_exit" == "0" ]] || {
  printf 'FAIL actual-SITL supervisor exited nonzero: %s\n' "$actual_supervisor_exit" >&2
  exit 2
}
ACTUAL_SUPERVISOR_PID=""
for pid in "${ACTUAL_ADAPTER_PIDS[@]}"; do
  kill -TERM "$pid"
done
for pid in "${ACTUAL_ADAPTER_PIDS[@]}"; do
  set +e
  wait "$pid"
  adapter_exit=$?
  set -e
  [[ "$adapter_exit" == "0" ]] || {
    printf 'FAIL actual-SITL adapter exited nonzero: pid=%s exit=%s\n' "$pid" "$adapter_exit" >&2
    exit 2
  }
done
ACTUAL_ADAPTER_PIDS=()
kill -TERM -- "-$FLIGHT_LAUNCH_PGID"
set +e
wait "$FLIGHT_LAUNCH_PID"
flight_exit=$?
set -e
if [[ "$flight_exit" != "0" && "$flight_exit" != "130" && "$flight_exit" != "143" ]]; then
  printf 'FAIL five-UAV flight launch exited unexpectedly: %s\n' "$flight_exit" >&2
  exit 2
fi
FLIGHT_LAUNCH_PID=""
FLIGHT_LAUNCH_PGID=""
lifecycle actual_sitl_stack_stopped "$(printf '{\"adapter_exit_code\":0,\"supervisor_exit_code\":0,\"flight_exit_code\":%d}' "$flight_exit")"

lifecycle captures_stop_requested '{"capture_processes":29,"tail_capture_processes":10}'
for pid in "${CAPTURE_PIDS[@]}"; do
  kill -INT "$pid"
done
for pid in "${CAPTURE_PIDS[@]}"; do
  set +e
  wait "$pid"
  capture_exit=$?
  set -e
  if [[ "$capture_exit" != "0" ]]; then
    printf 'FAIL raw capture exited nonzero: pid=%s exit=%s\n' "$pid" "$capture_exit" >&2
    exit 2
  fi
done
CAPTURE_PIDS=()
lifecycle captures_stopped '{"exit_code":0,"capture_processes":29,"tail_capture_processes":10}'

TOPOLOGY_TRANSITION_SEQUENCE=$((TOPOLOGY_TRANSITION_SEQUENCE + 1))
python3 "$TOPOLOGY_MONITOR" stop \
  --run-dir "$RUN_DIR" --run-id "$RUN_ID" \
  --runtime-id "$RUNTIME_ID" --run-nonce "$RUN_NONCE" \
  --sequence "$TOPOLOGY_TRANSITION_SEQUENCE" --timeout-s 5
set +e
wait "$TOPOLOGY_MONITOR_PID"
topology_monitor_exit=$?
set -e
TOPOLOGY_MONITOR_PID=""
[[ "$topology_monitor_exit" == "0" ]] || {
  printf 'FAIL continuous topology monitor exited nonzero: %s\n' "$topology_monitor_exit" >&2
  exit 2
}

for endpoint in "${ENDPOINTS[@]}"; do
  ip netns del "ams-$endpoint"
done
ip netns del ams-ns3
for index in 1 2 3 4 5; do
  ip link del "ams-tail$index" 2>/dev/null || true
done
NAMESPACES_CREATED=0

VALIDATOR_ARGS=(--run-dir "$RUN_DIR")
RESULT_PATH="$RUN_DIR/metrics/m3_validation_results.json"
if [[ "$M3_TECHNICAL_SMOKE" == "1" ]]; then
  VALIDATOR_ARGS+=(--technical-smoke --output metrics/m3_actual_sitl_smoke.json)
  RESULT_PATH="$RUN_DIR/metrics/m3_actual_sitl_smoke.json"
else
  VALIDATOR_ARGS+=(--m2-receipt "$M2_RECEIPT")
fi
python3 "$VALIDATOR" "${VALIDATOR_ARGS[@]}" \
  > "$RUN_DIR/logs/m3_validator_producer.stdout" \
  2> "$RUN_DIR/logs/m3_validator_producer.stderr"
INDEPENDENT_RESULT="/tmp/${RUN_ID}.m3-independent-result.json"
rm -f "$INDEPENDENT_RESULT"
python3 "$VALIDATOR" "${VALIDATOR_ARGS[@]}" --no-write \
  > "$INDEPENDENT_RESULT" \
  2> "$RUN_DIR/logs/m3_validator_independent.stderr"
cmp "$RESULT_PATH" "$INDEPENDENT_RESULT"
rm -f "$INDEPENDENT_RESULT"

chown -R 1000:1000 "$RUN_DIR"
SUCCESS=1
if [[ "$M3_TECHNICAL_SMOKE" == "1" ]]; then
  [[ ! -e "$RUN_DIR/metrics/m3_validation_results.json" ]] || {
    printf 'FAIL technical smoke created a formal M3 result\n' >&2
    exit 2
  }
  printf 'PASS ineligible M3 actual-SITL technical smoke: %s\n' "$RUN_DIR"
else
  printf 'PASS M3 external 30-cell matrix: %s\n' "$RUN_DIR"
fi
