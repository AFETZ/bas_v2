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
NS3_RUNNER="$ROOT_DIR/network/ns3/run_ns3_tap_packet_engine.sh"
ORCHESTRATOR="$ROOT_DIR/network/scripts/m4_runtime_orchestrator.py"
ENDPOINT_AGENT="$ROOT_DIR/network/scripts/m4_endpoint_agent.py"
ADAPTER="$ROOT_DIR/network/scripts/m4_adapter_runtime.py"
RUNTIME_COLLECTOR="$ROOT_DIR/network/scripts/collect_m4_runtime.py"
CLOCK_COLLECTOR="$ROOT_DIR/network/scripts/collect_m4_clock_correlations.py"
CAPTURE_TOOL="$ROOT_DIR/network/scripts/raw_packet_capture.py"
VALIDATOR="$ROOT_DIR/network/scripts/validate_m4_capacity.py"
PROVENANCE_TOOL="$ROOT_DIR/network/scripts/write_run_provenance.py"
SCENARIO="$ROOT_DIR/network/config/scenario_m4_canonical.yaml"
MATRIX="$ROOT_DIR/network/config/endpoint_matrix_5uav.json"
REQUIRED_MODULES="applications,bridge,core,csma,flow-monitor,internet,mobility,network,stats,tap-bridge,traffic-control"
ENDPOINTS=(gcs uav1 uav2 uav3 uav4 uav5)
RUNTIME_ID="${RUNTIME_ID:-$(python3 -c 'import secrets; print(secrets.token_hex(16))')}"
RUN_NONCE="${RUN_NONCE:-$(python3 -c 'import secrets; print(secrets.token_hex(32))')}"
ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-$((20 + $(printf '%s' "$RUN_ID" | cksum | awk '{print $1}') % 180))}"
GZ_PARTITION="${GZ_PARTITION:-ams_m4_${RUN_ID//[^a-zA-Z0-9_]/_}}"
PROVIDER_PORT="${M4_PROVIDER_PORT:-5090}"
CLOCK_SOCKET="/tmp/ams-m4-clock-$RUNTIME_ID.sock"
ENDPOINT_MODE="${M4_ENDPOINT_MODE:-actual_m3_sitl_v1}"
TECHNICAL_FIXTURE_ACK="${M4_ALLOW_SYNTHETIC_ENDPOINT_FIXTURE:-0}"

# The old M3/M4 EndpointAgent manufactures the UAV uplink
# COMMAND_ACK/heartbeat family itself.  It is useful for exercising the async
# radio plumbing, but it is detached from MAVProxy/ArduPilot and is therefore
# never an acceptance endpoint.  Formal execution stays fail-closed until the
# frozen actual-M3/SITL launcher API is checked in.
case "$ENDPOINT_MODE" in
  actual_m3_sitl_v1)
    printf 'FAIL M4 actual M3/SITL endpoint API is not frozen yet; synthetic EndpointAgent is ineligible\n' >&2
    exit 2
    ;;
  technical_synthetic_fixture_v1)
    if [[ "$TECHNICAL_FIXTURE_ACK" != "1" ]]; then
      printf 'FAIL technical synthetic endpoint fixture requires M4_ALLOW_SYNTHETIC_ENDPOINT_FIXTURE=1\n' >&2
      exit 2
    fi
    ;;
  *)
    printf 'FAIL unknown M4 endpoint mode: %s\n' "$ENDPOINT_MODE" >&2
    exit 2
    ;;
esac

if [[ "$(id -u)" != "0" ]]; then
  printf 'FAIL M4 capacity runner requires the bounded root component profile\n' >&2
  exit 2
fi
if [[ -e "$RUN_DIR" ]]; then
  printf 'FAIL immutable M4 run directory already exists: %s\n' "$RUN_DIR" >&2
  exit 2
fi
for command in ip setsid python3 colcon ros2; do
  command -v "$command" >/dev/null || {
    printf 'FAIL required M4 command is absent: %s\n' "$command" >&2
    exit 2
  }
done
for path in \
  "$NS3_BINARY" "$PACKET_SOURCE" "$COPIED_SOURCE" "$RECEIPT_TOOL" \
  "$NS3_RUNNER" "$ORCHESTRATOR" "$ENDPOINT_AGENT" "$ADAPTER" \
  "$RUNTIME_COLLECTOR" "$CLOCK_COLLECTOR" "$CAPTURE_TOOL" "$VALIDATOR" \
  "$PROVENANCE_TOOL" "$SCENARIO" "$MATRIX"; do
  [[ -e "$path" ]] || {
    printf 'FAIL required M4 artifact is absent: %s\n' "$path" >&2
    exit 2
  }
done
[[ -x "$NS3_BINARY" ]] || {
  printf 'FAIL M4 ns-3 packet engine is not executable\n' >&2
  exit 2
}
[[ "$RUNTIME_ID" =~ ^[0-9a-f]{32}$ && "$RUN_NONCE" =~ ^[0-9a-f]{64}$ ]] || {
  printf 'FAIL M4 runtime identity/nonce shape differs\n' >&2
  exit 2
}

PROCESS_GROUPS=()
CAPTURE_PIDS=()
ENDPOINT_PIDS=()
NAMESPACES_CREATED=0
SUCCESS=0
ENGINE_STOP_FILE=""
ADAPTER_STOP_FILE=""
PROVIDER_STOP_FILE=""
CLOCK_STOP_FILE=""
RUNTIME_STOP_FILE=""

terminate_group() {
  local pid="${1:-}"
  [[ -n "$pid" ]] || return 0
  kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
}

stop_group() {
  local pid="${1:-}"
  [[ -n "$pid" ]] || return 0
  terminate_group "$pid"
  local deadline=$((SECONDS + 10))
  while kill -0 -- "-$pid" 2>/dev/null && ((SECONDS < deadline)); do sleep 0.1; done
  if kill -0 -- "-$pid" 2>/dev/null; then kill -KILL -- "-$pid" 2>/dev/null || true; fi
  wait "$pid" 2>/dev/null || true
}

cleanup() {
  local exit_code=$?
  set +e
  for stop_file in \
    "$RUNTIME_STOP_FILE" "$ADAPTER_STOP_FILE" "$PROVIDER_STOP_FILE" \
    "$ENGINE_STOP_FILE" "$CLOCK_STOP_FILE"; do
    [[ -n "$stop_file" ]] && : > "$stop_file"
  done
  for pid in "${PROCESS_GROUPS[@]}"; do
    terminate_group "$pid"
  done
  wait 2>/dev/null || true
  if [[ "$NAMESPACES_CREATED" == "1" ]]; then
    for endpoint in "${ENDPOINTS[@]}"; do
      ip netns del "ams-$endpoint" 2>/dev/null || true
    done
    ip netns del ams-ns3 2>/dev/null || true
  fi
  rm -f "$CLOCK_SOCKET"
  [[ -d "$RUN_DIR" ]] && chown -R 1000:1000 "$RUN_DIR" 2>/dev/null || true
  ((SUCCESS == 1)) && exit 0
  exit "$exit_code"
}
trap cleanup EXIT INT TERM

for namespace in ams-gcs ams-ns3 ams-uav1 ams-uav2 ams-uav3 ams-uav4 ams-uav5; do
  if ip netns list | awk '{print $1}' | grep -Fxq "$namespace"; then
    printf 'FAIL refusing to reuse namespace: %s\n' "$namespace" >&2
    exit 2
  fi
done
[[ ! -e "$CLOCK_SOCKET" ]] || {
  printf 'FAIL refusing to reuse M4 clock socket path\n' >&2
  exit 2
}

RECEIPT="$(python3 "$RECEIPT_TOOL" verify \
  --ns3-dir "$NS3_DIR" --program ams-tap-packet-engine \
  --project-source "$PACKET_SOURCE" --copied-source "$COPIED_SOURCE" \
  --executable "$NS3_BINARY" --required-modules "$REQUIRED_MODULES")"

mkdir -p "$RUN_DIR/logs" "$RUN_DIR/metrics" "$RUN_DIR/runtime"
OVERLAY_ROOT="$RUN_DIR/runtime_overlay"
OVERLAY_BUILD="$OVERLAY_ROOT/build"
OVERLAY_INSTALL="$OVERLAY_ROOT/install"
OVERLAY_LOG="$OVERLAY_ROOT/log"
OVERLAY_EVIDENCE="$OVERLAY_ROOT/evidence"
EXPECTED_SHARE="$OVERLAY_INSTALL/multiagent_simulation/share/multiagent_simulation"
mkdir -p "$OVERLAY_EVIDENCE"
BUILD_COMMAND=(
  /usr/bin/colcon --log-base "$OVERLAY_LOG" build
  --base-paths "$ROOT_DIR/src/multiagent_simulation"
  --build-base "$OVERLAY_BUILD" --install-base "$OVERLAY_INSTALL"
)
printf '%q ' "${BUILD_COMMAND[@]}" > "$OVERLAY_EVIDENCE/build.command"
printf '\n' >> "$OVERLAY_EVIDENCE/build.command"
set +e
"${BUILD_COMMAND[@]}" > "$OVERLAY_EVIDENCE/build.stdout" \
  2> "$OVERLAY_EVIDENCE/build.stderr"
BUILD_RC=$?
set -e
printf '%s\n' "$BUILD_RC" > "$OVERLAY_EVIDENCE/build.exit_code"
if ((BUILD_RC != 0)) || [[ ! -f "$OVERLAY_INSTALL/setup.bash" ]]; then
  printf 'FAIL fresh M4 runtime overlay build failed\n' >&2
  exit 2
fi
# shellcheck disable=SC1090
source "$OVERLAY_INSTALL/setup.bash"
RESOLVED_SHARE="$(python3 -c 'from ament_index_python.packages import get_package_share_directory; print(get_package_share_directory("multiagent_simulation"))')"
[[ "$RESOLVED_SHARE" == "$EXPECTED_SHARE" && -d "$EXPECTED_SHARE" ]] || {
  printf 'FAIL M4 package share did not resolve to fresh overlay: %s\n' "$RESOLVED_SHARE" >&2
  exit 2
}
export ROS_DOMAIN_ID GZ_PARTITION
export GZ_SIM_RESOURCE_PATH="$EXPECTED_SHARE/models:$EXPECTED_SHARE/worlds:$EXPECTED_SHARE"
export SDF_PATH="$GZ_SIM_RESOURCE_PATH"

python3 "$PROVENANCE_TOOL" --run-dir "$RUN_DIR" \
  --qualification-profile m4_capacity_prerequisite \
  --consumed-node Q0 --consumed-node Q1 --consumed-node Q2 \
  --consumed-node Q3 --consumed-node Q4 \
  > "$RUN_DIR/logs/provenance.log" 2>&1

python3 "$ORCHESTRATOR" initialize-capacity \
  --run-dir "$RUN_DIR" --run-id "$RUN_ID" --runtime-id "$RUNTIME_ID" \
  --run-nonce "$RUN_NONCE" --engine-binary "$NS3_BINARY" \
  --endpoint-mode "$ENDPOINT_MODE" \
  --installed-share "$EXPECTED_SHARE" \
  > "$RUN_DIR/logs/m4_initialize.stdout" \
  2> "$RUN_DIR/logs/m4_initialize.stderr"
CONTRACT="$RUN_DIR/raw/m4_capacity_contract.json"
python3 "$RECEIPT_TOOL" verify \
  --ns3-dir "$NS3_DIR" --program ams-tap-packet-engine \
  --project-source "$PACKET_SOURCE" --copied-source "$COPIED_SOURCE" \
  --executable "$NS3_BINARY" --required-modules "$REQUIRED_MODULES" \
  --receipt "$RECEIPT" --copy-to "$RUN_DIR/raw/ns3_build_receipt.json" \
  > "$RUN_DIR/logs/ns3_build_receipt_verify.log"

ip netns add ams-ns3
for endpoint in "${ENDPOINTS[@]}"; do
  ip netns add "ams-$endpoint"
done
NAMESPACES_CREATED=1
for endpoint in "${ENDPOINTS[@]}"; do
  if [[ "$endpoint" == "gcs" ]]; then index=0; else index="${endpoint#uav}"; fi
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
  ip -n "$endpoint_ns" address add "10.71.$index.10/24" dev eth0
  ip -n "$endpoint_ns" link set eth0 up
  ip -n "$endpoint_ns" route add default via "10.71.$index.1" dev eth0
  ip -n ams-ns3 link add name "$bridge" type bridge
  ip -n ams-ns3 link set dev "$bridge" type bridge mcast_snooping 0
  ip -n ams-ns3 tuntap add dev "$tap" mode tap
  ip -n ams-ns3 link set "$peer_if" master "$bridge"
  ip -n ams-ns3 link set "$tap" master "$bridge"
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

wait_for_files() {
  local timeout_s=$1
  shift
  local deadline=$((SECONDS + timeout_s))
  while ((SECONDS < deadline)); do
    local missing=0
    for path in "$@"; do [[ -s "$path" ]] || missing=1; done
    ((missing == 0)) && return 0
    sleep 0.1
  done
  return 1
}

start_capture() {
  local namespace=$1 interface=$2 name=$3
  setsid ip netns exec "$namespace" python3 -u "$CAPTURE_TOOL" \
    --interface "$interface" --pcap "$RUN_DIR/pcap/$name.pcap" \
    --stats "$RUN_DIR/logs/capture-$name.json" \
    > /dev/null 2> "$RUN_DIR/logs/capture-$name.stderr" &
  local pid=$!
  CAPTURE_PIDS+=("$pid")
  PROCESS_GROUPS+=("$pid")
}
for endpoint in "${ENDPOINTS[@]}"; do
  start_capture "ams-$endpoint" eth0 "endpoint-$endpoint"
  start_capture ams-ns3 "vp-$endpoint" "ns3-external-$endpoint"
done
sleep 0.5
for pid in "${CAPTURE_PIDS[@]}"; do
  kill -0 "$pid" || { printf 'FAIL M4 capture exited at startup: %s\n' "$pid" >&2; exit 2; }
done

CLOCK_READY="$RUN_DIR/raw/state/clock-collector.ready.json"
CLOCK_STOP_FILE="$RUN_DIR/raw/control/clock-collector.stop"
setsid python3 -u "$CLOCK_COLLECTOR" --run-dir "$RUN_DIR" \
  --contract "$CONTRACT" --socket "$CLOCK_SOCKET" \
  --ready-file "$CLOCK_READY" --stop-file "$CLOCK_STOP_FILE" \
  > "$RUN_DIR/logs/clock-collector.stdout" \
  2> "$RUN_DIR/logs/clock-collector.stderr" &
CLOCK_PID=$!
PROCESS_GROUPS+=("$CLOCK_PID")
wait_for_files 10 "$CLOCK_READY" || { printf 'FAIL M4 clock collector readiness timeout\n' >&2; exit 2; }

(
  cd "$RUN_DIR/runtime"
  exec setsid ros2 launch multiagent_simulation multiagent_simulation.launch.py \
    robots_config_file:="$SCENARIO" world_file:=m4_canonical/m4_canonical.sdf \
    robot_model:=iris_radio_headless enable_serial2:=false \
    generate_sensor_models:=false gui:=false rviz:=false \
    headless_rendering:=false use_gz_tf:=true \
    use_mapping_camera:=false use_navigation_camera:=false use_zed_camera:=false
) > "$RUN_DIR/logs/m4_ros_gazebo.stdout" \
  2> "$RUN_DIR/logs/m4_ros_gazebo.stderr" &
LAUNCH_PID=$!
PROCESS_GROUPS+=("$LAUNCH_PID")

setsid ros2 run ros_gz_bridge parameter_bridge \
  '/world/map/pose/info@tf2_msgs/msg/TFMessage[gz.msgs.Pose_V' \
  > "$RUN_DIR/logs/world-pose-bridge.stdout" \
  2> "$RUN_DIR/logs/world-pose-bridge.stderr" &
WORLD_BRIDGE_PID=$!
PROCESS_GROUPS+=("$WORLD_BRIDGE_PID")

PROVIDER_READY="$RUN_DIR/raw/state/provider.ready.json"
PROVIDER_STOP_FILE="$RUN_DIR/raw/control/provider.stop"
setsid python3 -u "$ORCHESTRATOR" provider --run-dir "$RUN_DIR" \
  --contract "$CONTRACT" --port "$PROVIDER_PORT" \
  --ready-file "$PROVIDER_READY" --stop-file "$PROVIDER_STOP_FILE" \
  --clock-socket "$CLOCK_SOCKET" \
  > "$RUN_DIR/logs/provider.stdout" 2> "$RUN_DIR/logs/provider.stderr" &
PROVIDER_PID=$!
PROCESS_GROUPS+=("$PROVIDER_PID")
wait_for_files 30 "$PROVIDER_READY" || { printf 'FAIL real Sionna provider readiness timeout\n' >&2; exit 2; }

ENDPOINT_READY=()
for endpoint in "${ENDPOINTS[@]}"; do
  setsid ip netns exec "ams-$endpoint" python3 -u "$ENDPOINT_AGENT" \
    --run-dir "$RUN_DIR" --run-id "$RUN_ID" --runtime-id "$RUNTIME_ID" \
    --run-nonce "$RUN_NONCE" --endpoint "$endpoint" --matrix "$MATRIX" \
    --clock-socket "$CLOCK_SOCKET" \
    > "$RUN_DIR/logs/endpoint-$endpoint.stdout" \
    2> "$RUN_DIR/logs/endpoint-$endpoint.stderr" &
  pid=$!
  ENDPOINT_PIDS+=("$pid")
  PROCESS_GROUPS+=("$pid")
  ENDPOINT_READY+=("$RUN_DIR/raw/state/$endpoint.ready.json")
done
wait_for_files 20 "${ENDPOINT_READY[@]}" || { printf 'FAIL M4 endpoint readiness timeout\n' >&2; exit 2; }

ENGINE_READY="$RUN_DIR/raw/state/ns3-engine.ready.json"
ENGINE_STOP_FILE="$RUN_DIR/raw/control/ns3-engine.stop"
setsid env RUN_DIR="$RUN_DIR" UAV_COUNT=5 EVENT_EPOCH=1 NS3_NS=ams-ns3 \
  NS3_DIR="$NS3_DIR" DURATION_MS=900000 NS3_SEED=42 NS3_RUN=1 SELF_TEST=0 \
  SIONNA_IPC_ENABLED=1 \
  SIONNA_STATE_FILE="$RUN_DIR/logs/sionna_applied_states.jsonl" \
  SIONNA_MAX_STATE_TTL_MS=2000 SIONNA_POLL_INTERVAL_MS=1 \
  SIONNA_MAX_UPDATES_PER_POLL=64 SIONNA_INTERVENTION=natural \
  M4_CLOCK_DATAGRAM_SOCKET="$CLOCK_SOCKET" \
  TAP_GCS=tap-gcs TAP_UAVS=tap-uav1,tap-uav2,tap-uav3,tap-uav4,tap-uav5 \
  EVENTS_FILE="$RUN_DIR/logs/ns3_packet_events.jsonl" \
  PCAP_PREFIX="$RUN_DIR/pcap/ns3-packet-engine" \
  READY_FILE="$ENGINE_READY" STOP_FILE="$ENGINE_STOP_FILE" \
  CONFIG_REPORT="$RUN_DIR/logs/ns3_packet_engine_config.json" \
  ARGV_FILE="$RUN_DIR/logs/ns3_packet_engine.argv" \
  "$NS3_RUNNER" > "$RUN_DIR/logs/ns3.stdout" 2> "$RUN_DIR/logs/ns3.stderr" &
ENGINE_PID=$!
PROCESS_GROUPS+=("$ENGINE_PID")
wait_for_files 20 "$ENGINE_READY" || { printf 'FAIL M4 ns-3 readiness timeout\n' >&2; exit 2; }

ADAPTER_READY="$RUN_DIR/raw/state/adapter.ready.json"
ADAPTER_STOP_FILE="$RUN_DIR/raw/control/adapter.stop"
setsid python3 -u "$ADAPTER" --run-dir "$RUN_DIR" --contract "$CONTRACT" \
  --packet-events "$RUN_DIR/logs/ns3_packet_events.jsonl" \
  --state-file "$RUN_DIR/logs/sionna_applied_states.jsonl" \
  --ready-file "$ADAPTER_READY" --stop-file "$ADAPTER_STOP_FILE" \
  --control-dir "$RUN_DIR/raw/control/adapter" --clock-socket "$CLOCK_SOCKET" \
  --provider-port "$PROVIDER_PORT" \
  > "$RUN_DIR/logs/adapter.stdout" 2> "$RUN_DIR/logs/adapter.stderr" &
ADAPTER_PID=$!
PROCESS_GROUPS+=("$ADAPTER_PID")
wait_for_files 120 "$ADAPTER_READY" || { printf 'FAIL M4 adapter/pose readiness timeout\n' >&2; exit 2; }

RUNTIME_READY="$RUN_DIR/raw/state/runtime-collector.ready.json"
RUNTIME_STOP_FILE="$RUN_DIR/raw/control/runtime-collector.stop"
COLLECTOR_ARGS=(
  --run-dir "$RUN_DIR" --contract "$CONTRACT" --ready-file "$RUNTIME_READY"
  --stop-file "$RUNTIME_STOP_FILE" --clock-socket "$CLOCK_SOCKET"
  --include-own-process-group
  --required-ready "$CLOCK_READY" --required-ready "$PROVIDER_READY"
  --required-ready "$ENGINE_READY" --required-ready "$ADAPTER_READY"
)
for path in "${ENDPOINT_READY[@]}"; do COLLECTOR_ARGS+=(--required-ready "$path"); done
for pid in "${PROCESS_GROUPS[@]}"; do COLLECTOR_ARGS+=(--process-group "$pid"); done
setsid python3 -u "$RUNTIME_COLLECTOR" "${COLLECTOR_ARGS[@]}" \
  > "$RUN_DIR/logs/runtime-collector.stdout" \
  2> "$RUN_DIR/logs/runtime-collector.stderr" &
RUNTIME_PID=$!
PROCESS_GROUPS+=("$RUNTIME_PID")
wait_for_files 10 "$RUNTIME_READY" || { printf 'FAIL M4 runtime collector startup timeout\n' >&2; exit 2; }

set +e
wait "$RUNTIME_PID"
RUNTIME_RC=$?
set -e
[[ "$RUNTIME_RC" == "0" ]] || {
  printf 'FAIL M4 30+600 second runtime collector failed: %s\n' "$RUNTIME_RC" >&2
  exit 2
}

for pid in "${ENDPOINT_PIDS[@]}"; do
  set +e; wait "$pid"; endpoint_rc=$?; set -e
  [[ "$endpoint_rc" == "0" ]] || { printf 'FAIL M4 endpoint exited nonzero: %s\n' "$endpoint_rc" >&2; exit 2; }
done
ENDPOINT_PIDS=()

: > "$ADAPTER_STOP_FILE"
wait "$ADAPTER_PID"
: > "$ENGINE_STOP_FILE"
wait "$ENGINE_PID"
: > "$PROVIDER_STOP_FILE"
wait "$PROVIDER_PID"
: > "$CLOCK_STOP_FILE"
wait "$CLOCK_PID"

for pid in "${CAPTURE_PIDS[@]}"; do kill -INT -- "-$pid" 2>/dev/null || kill -INT "$pid"; done
for pid in "${CAPTURE_PIDS[@]}"; do wait "$pid"; done
CAPTURE_PIDS=()

stop_group "$WORLD_BRIDGE_PID"
stop_group "$LAUNCH_PID"
for endpoint in "${ENDPOINTS[@]}"; do ip netns del "ams-$endpoint"; done
ip netns del ams-ns3
NAMESPACES_CREATED=0

python3 "$VALIDATOR" --run-dir "$RUN_DIR" \
  > "$RUN_DIR/logs/m4_validator_producer.stdout" \
  2> "$RUN_DIR/logs/m4_validator_producer.stderr"
INDEPENDENT_RESULT="/tmp/$RUN_ID.m4-capacity-independent.json"
rm -f "$INDEPENDENT_RESULT"
python3 "$VALIDATOR" --run-dir "$RUN_DIR" --no-write \
  > "$INDEPENDENT_RESULT" \
  2> "$RUN_DIR/logs/m4_validator_independent.stderr"
cmp "$RUN_DIR/metrics/m4_capacity_validation.json" "$INDEPENDENT_RESULT"
rm -f "$INDEPENDENT_RESULT"

chown -R 1000:1000 "$RUN_DIR"
SUCCESS=1
printf 'PASS M4 formal capacity prerequisite: %s\n' "$RUN_DIR"
