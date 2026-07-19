#!/usr/bin/env bash
set -Eeuo pipefail
umask 0002

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUNNER_START_MONOTONIC_NS="$(python3 -c 'import time; print(time.monotonic_ns())')"
RUN_ID="${RUN_ID:?RUN_ID is required}"
RUN_DIR="${RUN_DIR:-$ROOT_DIR/runs/$RUN_ID}"
NS3_DIR="${NS3_DIR:-$ROOT_DIR/.external/ns-3}"
NS3_BINARY="$NS3_DIR/build/scratch/ns3.40-ams-tap-packet-engine-default"
PACKET_SOURCE="$ROOT_DIR/network/ns3/scratch/ams-tap-packet-engine.cc"
COPIED_SOURCE="$NS3_DIR/scratch/ams-tap-packet-engine.cc"
RECEIPT_TOOL="$ROOT_DIR/network/ns3/ns3_build_receipt.py"
NS3_RUNNER="$ROOT_DIR/network/ns3/run_ns3_tap_packet_engine.sh"
ORCHESTRATOR="$ROOT_DIR/network/scripts/m4_runtime_orchestrator.py"
STACK_LAUNCHER="$ROOT_DIR/network/scripts/actual_sitl_stack_orchestrator.sh"
CONTROL_PROBE="$ROOT_DIR/network/scripts/actual_sitl_control_probe.py"
ENDPOINT_AGENT="$ROOT_DIR/network/scripts/m4_endpoint_agent.py"
ADAPTER="$ROOT_DIR/network/scripts/m4_adapter_runtime.py"
PHASE_DRIVER="$ROOT_DIR/network/scripts/m4_causal_phase_driver.py"
RUNTIME_COLLECTOR="$ROOT_DIR/network/scripts/collect_m4_runtime.py"
CLOCK_COLLECTOR="$ROOT_DIR/network/scripts/collect_m4_clock_correlations.py"
CAPTURE_TOOL="$ROOT_DIR/network/scripts/raw_packet_capture.py"
TOPOLOGY_MONITOR="$ROOT_DIR/network/scripts/m3_topology_monitor.py"
VALIDATOR="$ROOT_DIR/network/scripts/validate_m4_causality.py"
PROVENANCE_TOOL="$ROOT_DIR/network/scripts/write_run_provenance.py"
MATRIX="$ROOT_DIR/network/config/endpoint_matrix_5uav.json"
FLIGHT_SCENARIO="$ROOT_DIR/network/config/scenario_m4_canonical.yaml"
REQUIRED_MODULES="applications,bridge,core,csma,flow-monitor,internet,mobility,network,stats,tap-bridge,traffic-control"
ENDPOINTS=(gcs uav1 uav2 uav3 uav4 uav5)
RUNTIME_ID="${RUNTIME_ID:-$(python3 -c 'import secrets; print(secrets.token_hex(16))')}"
RUN_NONCE="${RUN_NONCE:-$(python3 -c 'import secrets; print(secrets.token_hex(32))')}"
ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-$((20 + $(printf '%s' "$RUN_ID" | cksum | awk '{print $1}') % 180))}"
GZ_PARTITION="${GZ_PARTITION:-ams_m4_causal_${RUN_ID//[^a-zA-Z0-9_]/_}}"
PROVIDER_PORT="${M4_PROVIDER_PORT:-5090}"
CLOCK_SOCKET="/tmp/ams-m4-causal-$RUNTIME_ID.sock"
OVERLAY_ROOT="$RUN_DIR/runtime_overlay"
OVERLAY_BUILD="$OVERLAY_ROOT/build"
OVERLAY_INSTALL="$OVERLAY_ROOT/install"
OVERLAY_LOG="$OVERLAY_ROOT/log"
EXPECTED_SHARE="$OVERLAY_INSTALL/multiagent_simulation/share/multiagent_simulation"
RESOLVED_FLIGHT="$RUN_DIR/raw/resolved_flight_scenario.yaml"
RESOLVED_FLIGHT_ID="$RUN_DIR/raw/resolved_flight_scenario.identity.json"
CONTRACT="$RUN_DIR/raw/m4_causality_contract.json"
ACTUAL_MANIFEST="$RUN_DIR/raw/actual_sitl_endpoint_manifest.json"
ACTUAL_ENDPOINT_READY="$RUN_DIR/raw/state/actual-sitl-endpoints.ready.json"
ACTUAL_STACK_READY="$RUN_DIR/raw/state/actual-sitl-stack.ready.json"
ACTUAL_STACK_STOP="$RUN_DIR/raw/state/actual-sitl-endpoints.stop"
ACTUAL_STACK_STOPPED="$RUN_DIR/raw/state/actual-sitl-stack.stopped.json"

[[ "$(id -u)" == 0 ]] || { printf 'FAIL M4 causality runner requires bounded root profile\n' >&2; exit 2; }
[[ ! -e "$RUN_DIR" ]] || { printf 'FAIL immutable M4 causality run exists: %s\n' "$RUN_DIR" >&2; exit 2; }
[[ "$RUNTIME_ID" =~ ^[0-9a-f]{32}$ && "$RUN_NONCE" =~ ^[0-9a-f]{64}$ ]] || {
  printf 'FAIL M4 causality runtime identity differs\n' >&2
  exit 2
}
for command in colcon ip python3 ros2 setsid gz; do
  command -v "$command" >/dev/null || { printf 'FAIL M4 causality command absent: %s\n' "$command" >&2; exit 2; }
done
for path in "$NS3_BINARY" "$PACKET_SOURCE" "$COPIED_SOURCE" "$RECEIPT_TOOL" \
  "$NS3_RUNNER" "$ORCHESTRATOR" "$STACK_LAUNCHER" "$CONTROL_PROBE" \
  "$ENDPOINT_AGENT" "$ADAPTER" "$PHASE_DRIVER" "$RUNTIME_COLLECTOR" \
  "$CLOCK_COLLECTOR" "$CAPTURE_TOOL" "$TOPOLOGY_MONITOR" "$VALIDATOR" \
  "$PROVENANCE_TOOL" "$MATRIX" "$FLIGHT_SCENARIO"; do
  [[ -e "$path" ]] || { printf 'FAIL M4 causality artifact absent: %s\n' "$path" >&2; exit 2; }
done

PROCESS_GROUPS=()
CAPTURE_PIDS=()
COMPANION_PIDS=()
NAMESPACES_CREATED=0
STACK_PID=""
TOPOLOGY_PID=""
TOPOLOGY_SEQUENCE=0
SUCCESS=0

stop_group() {
  local pid="${1:-}"
  [[ -n "$pid" ]] || return 0
  kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
  local deadline=$((SECONDS + 10))
  while kill -0 -- "-$pid" 2>/dev/null && ((SECONDS < deadline)); do sleep 0.1; done
  kill -KILL -- "-$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
}

cleanup() {
  local rc=$?
  set +e
  for stop_file in \
    "${RUNTIME_STOP_FILE:-}" "${ADAPTER_STOP_FILE:-}" "${ENGINE_STOP_FILE:-}" \
    "${PROVIDER_STOP_FILE:-}" "${CLOCK_STOP_FILE:-}" "$ACTUAL_STACK_STOP"; do
    [[ -n "$stop_file" && -d "$RUN_DIR" ]] && : > "$stop_file" 2>/dev/null
  done
  for pid in "${COMPANION_PIDS[@]}" "${CAPTURE_PIDS[@]}" \
    "${CONTROL_PID:-}" "${PHASE_PID:-}" "${RUNTIME_PID:-}" \
    "${ADAPTER_PID:-}" "${ENGINE_PID:-}" "${PROVIDER_PID:-}" \
    "${CLOCK_PID:-}" "${WORLD_BRIDGE_PID:-}"; do
    [[ -n "$pid" ]] && kill -TERM -- "-$pid" 2>/dev/null
  done
  [[ -n "$STACK_PID" ]] && kill -TERM "$STACK_PID" 2>/dev/null
  if [[ -n "$TOPOLOGY_PID" ]]; then kill -TERM "$TOPOLOGY_PID" 2>/dev/null; fi
  wait 2>/dev/null
  if [[ "$NAMESPACES_CREATED" == 1 ]]; then
    for endpoint in "${ENDPOINTS[@]}"; do ip netns del "ams-$endpoint" 2>/dev/null; done
    ip netns del ams-ns3 2>/dev/null
    for index in 1 2 3 4 5; do ip link del "ams-tail$index" 2>/dev/null; done
  fi
  [[ -d "$RUN_DIR" ]] && chown -R 1000:1000 "$RUN_DIR" 2>/dev/null
  [[ "$SUCCESS" == 1 ]] && exit 0
  exit "$rc"
}
trap cleanup EXIT INT TERM

for namespace in ams-gcs ams-ns3 ams-uav1 ams-uav2 ams-uav3 ams-uav4 ams-uav5; do
  ! ip netns list | awk '{print $1}' | grep -Fxq "$namespace" || {
    printf 'FAIL refusing mutable namespace reuse: %s\n' "$namespace" >&2
    exit 2
  }
done
for index in 1 2 3 4 5; do
  ! ip link show "ams-tail$index" >/dev/null 2>&1 || {
    printf 'FAIL refusing mutable tail reuse: ams-tail%s\n' "$index" >&2
    exit 2
  }
done

RECEIPT="$(python3 "$RECEIPT_TOOL" verify \
  --ns3-dir "$NS3_DIR" --program ams-tap-packet-engine \
  --project-source "$PACKET_SOURCE" --copied-source "$COPIED_SOURCE" \
  --executable "$NS3_BINARY" --required-modules "$REQUIRED_MODULES")"

mkdir -p "$RUN_DIR/logs" "$RUN_DIR/metrics" "$RUN_DIR/raw/state" \
  "$RUN_DIR/raw/topology" "$RUN_DIR/raw/actual_sitl" "$RUN_DIR/runtime"
BUILD_COMMAND=(
  colcon --log-base "$OVERLAY_LOG" build
  --base-paths "$ROOT_DIR/src/multiagent_simulation"
  --build-base "$OVERLAY_BUILD" --install-base "$OVERLAY_INSTALL"
)
printf '%q ' "${BUILD_COMMAND[@]}" > "$RUN_DIR/logs/m4_causal_overlay_build.command"
printf '\n' >> "$RUN_DIR/logs/m4_causal_overlay_build.command"
"${BUILD_COMMAND[@]}" > "$RUN_DIR/logs/m4_causal_overlay_build.log" 2>&1
[[ -f "$OVERLAY_INSTALL/setup.bash" ]] || { printf 'FAIL M4 causal overlay build absent\n' >&2; exit 2; }
# shellcheck disable=SC1090
set +u
source "$OVERLAY_INSTALL/setup.bash"
set -u
RESOLVED_SHARE="$(python3 -c 'from ament_index_python.packages import get_package_share_directory; print(get_package_share_directory("multiagent_simulation"))')"
[[ "$RESOLVED_SHARE" == "$EXPECTED_SHARE" ]] || { printf 'FAIL M4 causal overlay resolution differs\n' >&2; exit 2; }
export ROS_DOMAIN_ID GZ_PARTITION
export GZ_SIM_RESOURCE_PATH="$EXPECTED_SHARE/models:$EXPECTED_SHARE/worlds:$EXPECTED_SHARE"
export SDF_PATH="$GZ_SIM_RESOURCE_PATH"

python3 "$PROVENANCE_TOOL" --run-dir "$RUN_DIR" \
  --qualification-profile m4_component \
  --consumed-node Q0 --consumed-node Q1 --consumed-node Q2 \
  --consumed-node Q3 --consumed-node Q4 \
  > "$RUN_DIR/logs/provenance.log" 2>&1
python3 "$ORCHESTRATOR" prepare-causality-flight \
  --flight-scenario "$FLIGHT_SCENARIO" --output "$RESOLVED_FLIGHT" \
  --identity-output "$RESOLVED_FLIGHT_ID"

ip netns add ams-ns3
for endpoint in "${ENDPOINTS[@]}"; do ip netns add "ams-$endpoint"; done
NAMESPACES_CREATED=1
for endpoint in "${ENDPOINTS[@]}"; do
  if [[ "$endpoint" == gcs ]]; then index=0; else index="${endpoint#uav}"; fi
  endpoint_ns="ams-$endpoint"; endpoint_if="ve-$endpoint"; peer_if="vp-$endpoint"
  bridge="br-$endpoint"; tap="tap-$endpoint"
  ip link add "$endpoint_if" type veth peer name "$peer_if"
  ip link set "$endpoint_if" netns "$endpoint_ns"
  ip link set "$peer_if" netns ams-ns3
  ip -n "$endpoint_ns" link set "$endpoint_if" name eth0
  ip -n "$endpoint_ns" link set lo up
  ip -n "$endpoint_ns" link set eth0 address "02:71:$(printf '%02x' "$index"):00:10:10"
  ip -n "$endpoint_ns" address add "10.71.$index.10/24" dev eth0
  ip -n "$endpoint_ns" link set eth0 txqueuelen 1000
  ip -n "$endpoint_ns" link set eth0 up
  ip -n "$endpoint_ns" route add default via "10.71.$index.1" dev eth0
  if [[ "$endpoint" != gcs ]]; then
    tail_root="ams-tail$index"; tail_peer="tail-peer$index"
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

bash "$STACK_LAUNCHER" --run-dir "$RUN_DIR" --run-id "$RUN_ID" \
  --runtime-id "$RUNTIME_ID" --run-nonce "$RUN_NONCE" --profile m4_causality \
  --installed-share "$EXPECTED_SHARE" --flight-scenario "$RESOLVED_FLIGHT" \
  --world-file m4_canonical/m4_canonical.sdf --manifest "$ACTUAL_MANIFEST" \
  --endpoint-ready "$ACTUAL_ENDPOINT_READY" --stack-ready "$ACTUAL_STACK_READY" \
  --stop-file "$ACTUAL_STACK_STOP" --stopped-file "$ACTUAL_STACK_STOPPED" \
  --mavproxy-streamrate 1 \
  --clock-socket "$CLOCK_SOCKET" --headless-rendering false \
  > "$RUN_DIR/logs/actual-sitl-stack.stdout" \
  2> "$RUN_DIR/logs/actual-sitl-stack.stderr" &
STACK_PID=$!
deadline=$((SECONDS + 90))
while [[ ! -s "$ACTUAL_STACK_READY" ]]; do
  kill -0 "$STACK_PID" || { printf 'FAIL actual-SITL shared launcher exited\n' >&2; exit 2; }
  ((SECONDS < deadline)) || { printf 'FAIL actual-SITL shared launcher readiness timeout\n' >&2; exit 2; }
  sleep 0.1
done

python3 "$ORCHESTRATOR" initialize-causality \
  --run-dir "$RUN_DIR" --run-id "$RUN_ID" --runtime-id "$RUNTIME_ID" \
  --run-nonce "$RUN_NONCE" --runner-start-monotonic-ns "$RUNNER_START_MONOTONIC_NS" \
  --engine-binary "$NS3_BINARY" --installed-share "$EXPECTED_SHARE" \
  --flight-scenario "$FLIGHT_SCENARIO" \
  > "$RUN_DIR/logs/m4_causality_initialize.stdout" \
  2> "$RUN_DIR/logs/m4_causality_initialize.stderr"
python3 "$RECEIPT_TOOL" verify --ns3-dir "$NS3_DIR" \
  --program ams-tap-packet-engine --project-source "$PACKET_SOURCE" \
  --copied-source "$COPIED_SOURCE" --executable "$NS3_BINARY" \
  --required-modules "$REQUIRED_MODULES" --receipt "$RECEIPT" \
  --copy-to "$RUN_DIR/raw/ns3_build_receipt.json" \
  > "$RUN_DIR/logs/ns3_build_receipt_verify.log"

wait_files() {
  local timeout=$1; shift; local deadline=$((SECONDS + timeout))
  while ((SECONDS < deadline)); do
    local missing=0
    for path in "$@"; do [[ -s "$path" ]] || missing=1; done
    ((missing == 0)) && return 0
    sleep 0.05
  done
  return 1
}

python3 -u "$TOPOLOGY_MONITOR" run --run-dir "$RUN_DIR" --run-id "$RUN_ID" \
  --runtime-id "$RUNTIME_ID" --run-nonce "$RUN_NONCE" --interval-ms 500 \
  > "$RUN_DIR/logs/topology-monitor.stdout" 2> "$RUN_DIR/logs/topology-monitor.stderr" &
TOPOLOGY_PID=$!
TOPOLOGY_READY="$RUN_DIR/raw/topology_monitor/ready.json"
wait_files 10 "$TOPOLOGY_READY" || { printf 'FAIL topology monitor readiness timeout\n' >&2; exit 2; }

start_capture() {
  local namespace=$1 interface=$2 role=$3
  if [[ "$namespace" == container-root ]]; then
    setsid python3 -u "$CAPTURE_TOOL" --interface "$interface" \
      --pcap "$RUN_DIR/pcap/$role.pcap" --stats "$RUN_DIR/logs/capture-$role.json" \
      > /dev/null 2> "$RUN_DIR/logs/capture-$role.stderr" &
  else
    setsid ip netns exec "$namespace" python3 -u "$CAPTURE_TOOL" \
      --interface "$interface" --pcap "$RUN_DIR/pcap/$role.pcap" \
      --stats "$RUN_DIR/logs/capture-$role.json" \
      > /dev/null 2> "$RUN_DIR/logs/capture-$role.stderr" &
  fi
  CAPTURE_PIDS+=("$!"); PROCESS_GROUPS+=("$!")
}
mkdir -p "$RUN_DIR/pcap"
for endpoint in "${ENDPOINTS[@]}"; do
  start_capture "ams-$endpoint" eth0 "endpoint-$endpoint"
  start_capture ams-ns3 "vp-$endpoint" "ns3-external-$endpoint"
done
for index in 1 2 3 4 5; do
  start_capture container-root "ams-tail$index" "tail-root-uav$index"
  start_capture "ams-uav$index" tail0 "tail-uav$index"
done
sleep 0.5
for pid in "${CAPTURE_PIDS[@]}"; do kill -0 "$pid" || { printf 'FAIL capture exited\n' >&2; exit 2; }; done

CLOCK_READY="$RUN_DIR/raw/state/clock-collector.ready.json"
CLOCK_STOP_FILE="$RUN_DIR/raw/control/clock-collector.stop"
mkdir -p "$RUN_DIR/raw/control"
setsid python3 -u "$CLOCK_COLLECTOR" --run-dir "$RUN_DIR" --contract "$CONTRACT" \
  --socket "$CLOCK_SOCKET" --ready-file "$CLOCK_READY" --stop-file "$CLOCK_STOP_FILE" \
  > "$RUN_DIR/logs/clock-collector.stdout" 2> "$RUN_DIR/logs/clock-collector.stderr" &
CLOCK_PID=$!; PROCESS_GROUPS+=("$CLOCK_PID")
wait_files 10 "$CLOCK_READY" || { printf 'FAIL clock collector readiness timeout\n' >&2; exit 2; }

setsid ros2 run ros_gz_bridge parameter_bridge \
  '/world/map/pose/info@tf2_msgs/msg/TFMessage[gz.msgs.Pose_V' \
  > "$RUN_DIR/logs/world-pose-bridge.stdout" 2> "$RUN_DIR/logs/world-pose-bridge.stderr" &
WORLD_BRIDGE_PID=$!; PROCESS_GROUPS+=("$WORLD_BRIDGE_PID")

COMPANION_READY=()
for endpoint in "${ENDPOINTS[@]}"; do
  setsid ip netns exec "ams-$endpoint" python3 -u "$ENDPOINT_AGENT" \
    --run-dir "$RUN_DIR" --run-id "$RUN_ID" --runtime-id "$RUNTIME_ID" \
    --run-nonce "$RUN_NONCE" --endpoint "$endpoint" --matrix "$MATRIX" \
    --clock-socket "$CLOCK_SOCKET" \
    > "$RUN_DIR/logs/endpoint-$endpoint.stdout" 2> "$RUN_DIR/logs/endpoint-$endpoint.stderr" &
  COMPANION_PIDS+=("$!"); PROCESS_GROUPS+=("$!")
  COMPANION_READY+=("$RUN_DIR/raw/state/$endpoint.ready.json")
done
wait_files 15 "${COMPANION_READY[@]}" || { printf 'FAIL companion readiness timeout\n' >&2; exit 2; }

PROVIDER_READY="$RUN_DIR/raw/state/provider.ready.json"
PROVIDER_STOP_FILE="$RUN_DIR/raw/control/provider.stop"
setsid python3 -u "$ORCHESTRATOR" provider --run-dir "$RUN_DIR" --contract "$CONTRACT" \
  --port "$PROVIDER_PORT" --ready-file "$PROVIDER_READY" \
  --stop-file "$PROVIDER_STOP_FILE" --clock-socket "$CLOCK_SOCKET" \
  > "$RUN_DIR/logs/provider.stdout" 2> "$RUN_DIR/logs/provider.stderr" &
PROVIDER_PID=$!; PROCESS_GROUPS+=("$PROVIDER_PID")
wait_files 30 "$PROVIDER_READY" || { printf 'FAIL provider readiness timeout\n' >&2; exit 2; }

ENGINE_READY="$RUN_DIR/raw/state/ns3-engine.ready.json"
ENGINE_STOP_FILE="$RUN_DIR/raw/control/ns3-engine.stop"
setsid env RUN_DIR="$RUN_DIR" UAV_COUNT=5 EVENT_EPOCH=1 NS3_NS=ams-ns3 \
  NS3_DIR="$NS3_DIR" DURATION_MS=1250000 NS3_SEED=42 NS3_RUN=1 SELF_TEST=0 \
  SIONNA_IPC_ENABLED=1 SIONNA_STATE_FILE="$RUN_DIR/logs/sionna_applied_states.jsonl" \
  SIONNA_MAX_STATE_TTL_MS=2000 SIONNA_POLL_INTERVAL_MS=1 \
  SIONNA_MAX_UPDATES_PER_POLL=64 SIONNA_INTERVENTION=natural \
  M4_CLOCK_DATAGRAM_SOCKET="$CLOCK_SOCKET" TAP_GCS=tap-gcs \
  TAP_UAVS=tap-uav1,tap-uav2,tap-uav3,tap-uav4,tap-uav5 \
  EVENTS_FILE="$RUN_DIR/logs/ns3_packet_events.jsonl" \
  PCAP_PREFIX="$RUN_DIR/pcap/ns3-packet-engine" READY_FILE="$ENGINE_READY" \
  STOP_FILE="$ENGINE_STOP_FILE" CONFIG_REPORT="$RUN_DIR/logs/ns3_packet_engine_config.json" \
  ARGV_FILE="$RUN_DIR/logs/ns3_packet_engine.argv" "$NS3_RUNNER" \
  > "$RUN_DIR/logs/ns3.stdout" 2> "$RUN_DIR/logs/ns3.stderr" &
ENGINE_PID=$!; PROCESS_GROUPS+=("$ENGINE_PID")
wait_files 20 "$ENGINE_READY" || { printf 'FAIL ns-3 readiness timeout\n' >&2; exit 2; }

ADAPTER_READY="$RUN_DIR/raw/state/adapter.ready.json"
ADAPTER_STOP_FILE="$RUN_DIR/raw/control/adapter.stop"
setsid python3 -u "$ADAPTER" --run-dir "$RUN_DIR" --contract "$CONTRACT" \
  --packet-events "$RUN_DIR/logs/ns3_packet_events.jsonl" \
  --state-file "$RUN_DIR/logs/sionna_applied_states.jsonl" \
  --ready-file "$ADAPTER_READY" --stop-file "$ADAPTER_STOP_FILE" \
  --control-dir "$RUN_DIR/raw/control/adapter" --clock-socket "$CLOCK_SOCKET" \
  --provider-port "$PROVIDER_PORT" --fault-enabled \
  > "$RUN_DIR/logs/adapter.stdout" 2> "$RUN_DIR/logs/adapter.stderr" &
ADAPTER_PID=$!; PROCESS_GROUPS+=("$ADAPTER_PID")
wait_files 20 "$ADAPTER_READY" || { printf 'FAIL adapter readiness timeout\n' >&2; exit 2; }

setsid ip netns exec ams-gcs python3 -u "$CONTROL_PROBE" --run-dir "$RUN_DIR" \
  --run-id "$RUN_ID" --runtime-id "$RUNTIME_ID" --run-nonce "$RUN_NONCE" \
  --profile m4_causality --m3-result "$RUN_DIR/raw/prerequisites/m3-result.json" \
  --clock-socket "$CLOCK_SOCKET" --matrix "$MATRIX" \
  > "$RUN_DIR/logs/actual-control.stdout" 2> "$RUN_DIR/logs/actual-control.stderr" &
CONTROL_PID=$!; PROCESS_GROUPS+=("$CONTROL_PID")
ACTUAL_CONTROL_READY="$RUN_DIR/raw/state/actual-control.link-ready.json"
wait_files 30 "$ACTUAL_CONTROL_READY" || { printf 'FAIL actual control link readiness timeout\n' >&2; exit 2; }

PHASE_EVENT_DIR="$RUN_DIR/raw/control/runtime-events"
PHASE_READY="$RUN_DIR/raw/state/causal-phase-driver.ready.json"
PHASE_DONE="$RUN_DIR/raw/state/causal-phase-driver.done.json"
mkdir -p "$PHASE_EVENT_DIR"
python3 -u "$PHASE_DRIVER" --run-dir "$RUN_DIR" --contract "$CONTRACT" \
  --event-dir "$PHASE_EVENT_DIR" --adapter-control-dir "$RUN_DIR/raw/control/adapter" \
  --ready-file "$PHASE_READY" --done-file "$PHASE_DONE" \
  > "$RUN_DIR/logs/causal-phase-driver.stdout" 2> "$RUN_DIR/logs/causal-phase-driver.stderr" &
PHASE_PID=$!
wait_files 5 "$PHASE_READY" || { printf 'FAIL causal phase driver readiness timeout\n' >&2; exit 2; }

mapfile -t STACK_GROUPS < <(python3 -c 'import json,sys; print(*json.load(open(sys.argv[1]))["process_groups"], sep="\n")' "$ACTUAL_STACK_READY")
PROCESS_GROUPS+=("${STACK_GROUPS[@]}")
RUNTIME_READY="$RUN_DIR/raw/state/runtime-collector.ready.json"
RUNTIME_STOP_FILE="$RUN_DIR/raw/control/runtime-collector.stop"
COLLECTOR_ARGS=(
  --run-dir "$RUN_DIR" --contract "$CONTRACT" --ready-file "$RUNTIME_READY"
  --stop-file "$RUNTIME_STOP_FILE" --clock-socket "$CLOCK_SOCKET"
  --include-own-process-group --event-dir "$PHASE_EVENT_DIR"
  --causal-done-file "$PHASE_DONE" --required-ready "$CLOCK_READY"
  --required-ready "$ACTUAL_STACK_READY" --required-ready "$TOPOLOGY_READY"
  --required-ready "$PROVIDER_READY" --required-ready "$ENGINE_READY"
  --required-ready "$ADAPTER_READY" --required-ready "$ACTUAL_CONTROL_READY"
  --required-ready "$PHASE_READY"
)
for path in "${COMPANION_READY[@]}"; do COLLECTOR_ARGS+=(--required-ready "$path"); done
for pgid in "${PROCESS_GROUPS[@]}"; do COLLECTOR_ARGS+=(--process-group "$pgid"); done
setsid python3 -u "$RUNTIME_COLLECTOR" "${COLLECTOR_ARGS[@]}" \
  > "$RUN_DIR/logs/runtime-collector.stdout" 2> "$RUN_DIR/logs/runtime-collector.stderr" &
RUNTIME_PID=$!
wait_files 5 "$RUNTIME_READY" || { printf 'FAIL causal runtime collector readiness timeout\n' >&2; exit 2; }

wait "$RUNTIME_PID"; RUNTIME_PID=""
wait "$PHASE_PID"; PHASE_PID=""
wait "$CONTROL_PID"; CONTROL_PID=""
for pid in "${COMPANION_PIDS[@]}"; do wait "$pid"; done
COMPANION_PIDS=()

: > "$ADAPTER_STOP_FILE"; wait "$ADAPTER_PID"; ADAPTER_PID=""
: > "$ENGINE_STOP_FILE"; wait "$ENGINE_PID"; ENGINE_PID=""
: > "$PROVIDER_STOP_FILE"; wait "$PROVIDER_PID"; PROVIDER_PID=""
: > "$CLOCK_STOP_FILE"; wait "$CLOCK_PID"; CLOCK_PID=""
: > "$ACTUAL_STACK_STOP"; wait "$STACK_PID"; STACK_PID=""
stop_group "$WORLD_BRIDGE_PID"; WORLD_BRIDGE_PID=""

for pid in "${CAPTURE_PIDS[@]}"; do kill -INT -- "-$pid" 2>/dev/null || kill -INT "$pid"; done
for pid in "${CAPTURE_PIDS[@]}"; do wait "$pid"; done
CAPTURE_PIDS=()
TOPOLOGY_SEQUENCE=$((TOPOLOGY_SEQUENCE + 1))
python3 "$TOPOLOGY_MONITOR" stop --run-dir "$RUN_DIR" --run-id "$RUN_ID" \
  --runtime-id "$RUNTIME_ID" --run-nonce "$RUN_NONCE" \
  --sequence "$TOPOLOGY_SEQUENCE" --timeout-s 5
wait "$TOPOLOGY_PID"; TOPOLOGY_PID=""

for endpoint in "${ENDPOINTS[@]}"; do ip netns del "ams-$endpoint"; done
ip netns del ams-ns3
NAMESPACES_CREATED=0

python3 - "$RUN_DIR/raw/m4_finalization_timing.json" "$CONTRACT" <<'PY'
import json, os, sys, time
from pathlib import Path
contract = json.load(open(sys.argv[2], encoding="utf-8"))
now = time.monotonic_ns()
value = {
    "contract": "ams.m4.causal-finalization-timing/v1",
    "run_id": contract["run_id"], "runtime_id": contract["runtime_id"],
    "last_window_end_monotonic_ns": contract["windows"][-1]["end_monotonic_ns"],
    "evidence_finalized_monotonic_ns": now,
    "elapsed_ns": now - contract["windows"][-1]["end_monotonic_ns"],
    "budget_ns": contract["execution_budget"]["finalization_budget_ns"],
}
path = Path(sys.argv[1]); fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o664)
with os.fdopen(fd, "w", encoding="utf-8") as stream:
    json.dump(value, stream, sort_keys=True, separators=(",", ":")); stream.write("\n")
    stream.flush(); os.fsync(stream.fileno())
PY

python3 "$VALIDATOR" --run-dir "$RUN_DIR" \
  > "$RUN_DIR/logs/m4_causality_validator.stdout" \
  2> "$RUN_DIR/logs/m4_causality_validator.stderr"
INDEPENDENT="/tmp/$RUN_ID.m4-causality-independent.json"
rm -f "$INDEPENDENT"
python3 "$VALIDATOR" --run-dir "$RUN_DIR" --no-write \
  > "$INDEPENDENT" 2> "$RUN_DIR/logs/m4_causality_validator_independent.stderr"
cmp "$RUN_DIR/metrics/m4_validation_results.json" "$INDEPENDENT"
rm -f "$INDEPENDENT"

chown -R 1000:1000 "$RUN_DIR"
SUCCESS=1
printf 'PASS M4 formal causal validation: %s\n' "$RUN_DIR"
