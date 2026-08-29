#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CONTAINER_NAME="${BAS_TOWN01_CONTAINER_NAME:-bas-v2-town01-full-stack}"
IMAGE="${BAS_CONTAINER_IMAGE:-multiagent_simulation:latest}"

run_in_container() {
  command -v docker >/dev/null 2>&1 || {
    printf 'Docker is required for the Town01 full-stack runtime.\n' >&2
    return 2
  }
  local image_id
  image_id="$(docker image inspect --format '{{.Id}}' "$IMAGE" 2>/dev/null)" || {
    printf 'Runtime image is unavailable: %s (no rebuild was attempted).\n' "$IMAGE" >&2
    return 2
  }
  if [[ "$(docker inspect --format '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null || true)" == "true" ]]; then
    printf 'Town01 full-stack container is already running: %s\n' "$CONTAINER_NAME" >&2
    return 3
  fi
  python3 "$ROOT_DIR/scripts/product/prepare_town01_gazebo.py"
  local -a gpu_args=()
  if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
    gpu_args=(
      --gpus all
      -e NVIDIA_VISIBLE_DEVICES=all
      -e NVIDIA_DRIVER_CAPABILITIES=graphics,utility,compute
    )
  fi
  printf 'Starting Town01 full stack in existing image %s.\n' "$image_id"
  set +e
  docker run --rm \
    --name "$CONTAINER_NAME" \
    --label bas.product=town01-full-stack \
    --privileged \
    --network=host \
    --user 0:0 \
    "${gpu_args[@]}" \
    -e BAS_TOWN01_IN_CONTAINER=1 \
    -e BAS_TOWN01_RUN_ID="${BAS_TOWN01_RUN_ID:-}" \
    -e BAS_TOWN01_PROFILES="${BAS_TOWN01_PROFILES:-}" \
    -e BAS_TOWN01_MEDIUM_ACCESS_MODE="${BAS_TOWN01_MEDIUM_ACCESS_MODE:-}" \
    -e BAS_TOWN01_COMPARISON_RUN="${BAS_TOWN01_COMPARISON_RUN:-}" \
    -e BAS_TOWN01_SKIP_HEATMAPS="${BAS_TOWN01_SKIP_HEATMAPS:-0}" \
    -e BAS_TOWN01_SKIP_FLIGHT_SCENARIO="${BAS_TOWN01_SKIP_FLIGHT_SCENARIO:-0}" \
    -e BAS_TOWN01_HOST_UID="$(id -u)" \
    -e BAS_TOWN01_HOST_GID="$(id -g)" \
    -e HOME=/tmp/bas-town01-home \
    -e XDG_RUNTIME_DIR=/tmp/bas-town01-xdg \
    -e PYTHONPATH=/home/ubuntu/.local/lib/python3.10/site-packages \
    -v "$ROOT_DIR":/workspace/multiagent_simulation \
    -v "$ROOT_DIR":/home/bas/bas_v2 \
    -w /workspace/multiagent_simulation \
    "$image_id" \
    bash -lc '
      set -eo pipefail
      mkdir -p "$HOME" "$XDG_RUNTIME_DIR"
      chmod 700 "$HOME" "$XDG_RUNTIME_DIR"
      set +u
      source /opt/ros/humble/setup.bash
      source /workspace/ardu_ws/install/setup.bash
      source /workspace/multiagent_simulation/install/setup.bash
      export PATH="/home/ubuntu/.local/bin:$PATH"
      export GZ_VERSION=harmonic
      export GZ_SIM_RESOURCE_PATH="${GZ_SIM_RESOURCE_PATH:-}:$PWD/src/multiagent_simulation/models:$PWD/src/multiagent_simulation/worlds:$PWD/src:$PWD/.external/cavise_maps/Town01/gazebo"
      set -u
      exec ./scripts/product/run_town01_full_stack.sh
    '
  local status=$?
  set -e
  if [[ "$status" -eq 137 || "$status" -eq 143 ]]; then
    return 0
  fi
  return "$status"
}

if [[ "${BAS_TOWN01_IN_CONTAINER:-0}" != "1" ]]; then
  run_in_container
  exit $?
fi

if ((EUID != 0)); then
  printf 'Town01 full stack requires root inside its privileged runtime container.\n' >&2
  exit 2
fi

for command in ip ros2 gz socat setsid python3; do
  command -v "$command" >/dev/null 2>&1 || {
    printf 'Required runtime command is unavailable: %s\n' "$command" >&2
    exit 2
  }
done

MEDIUM_ACCESS_MODE="${BAS_TOWN01_MEDIUM_ACCESS_MODE:-}"
if [[ -z "$MEDIUM_ACCESS_MODE" ]]; then
  MEDIUM_ACCESS_MODE="$(python3 - "$ROOT_DIR/network/config/communication_qos.yaml" <<'PY'
import sys, yaml
print(yaml.safe_load(open(sys.argv[1], encoding="utf-8"))["medium_access"]["mode"])
PY
)"
fi
case "$MEDIUM_ACCESS_MODE" in
  stock_ns3_csma)
    NS3_DIR="${NS3_STOCK_DIR:-$ROOT_DIR/.external/ns-3-stock}"
    NS3_BINARY="$NS3_DIR/build/scratch/ns3.40-ams-tap-packet-engine-stock-default"
    NS3_RUNNER="$ROOT_DIR/network/ns3/run_ns3_tap_packet_engine_stock.sh"
    NS3_SOURCE="$ROOT_DIR/network/ns3/scratch/ams-tap-packet-engine-stock.cc"
    ;;
  centralized_priority_scheduler_over_csma_channel)
    NS3_DIR="${NS3_DIR:-$ROOT_DIR/.external/ns-3}"
    NS3_BINARY="$NS3_DIR/build/scratch/ns3.40-ams-tap-packet-engine-default"
    NS3_RUNNER="$ROOT_DIR/network/ns3/run_ns3_tap_packet_engine.sh"
    NS3_SOURCE="$ROOT_DIR/network/ns3/scratch/ams-tap-packet-engine.cc"
    ;;
  *)
    printf 'Unsupported medium access mode: %s\n' "$MEDIUM_ACCESS_MODE" >&2
    exit 2
    ;;
esac
test -x "$NS3_BINARY"
cmp -s "$NS3_SOURCE" "$NS3_DIR/scratch/$(basename "$NS3_SOURCE")" || {
  printf 'selected ns-3 packet-engine source differs from its built external copy; run its focused build first.\n' >&2
  exit 2
}

RUN_ID="${BAS_TOWN01_RUN_ID:-}"
if [[ -z "$RUN_ID" ]]; then
  RUN_ID="town01-full-$(date -u +%Y%m%dT%H%M%SZ)"
fi
RUN_DIR="${BAS_TOWN01_RUN_DIR:-$ROOT_DIR/runs/$RUN_ID}"
RUNTIME_DIR="${BAS_TOWN01_RUNTIME_DIR:-/tmp/bas-v2-$RUN_ID}"
UART_DIR="$RUNTIME_DIR/uart"
WORK_DIR="$RUNTIME_DIR/work"
SCENARIO="$ROOT_DIR/network/config/scenario_5uav_town01.yaml"
RADIO="$ROOT_DIR/network/config/radio_24ghz_town01.yaml"
QOS="$ROOT_DIR/network/config/communication_qos.yaml"
WORLD="$ROOT_DIR/.external/cavise_maps/Town01/gazebo/town01.sdf"
NODE_STATE="$RUN_DIR/metrics/node_state.json"
NODE_EVENTS="$RUN_DIR/metrics/node_state.jsonl"
SIONNA_STATES="$RUN_DIR/logs/sionna_packet_states.jsonl"
SIONNA_READY="$RUN_DIR/logs/sionna_packet_states.ready"
NS3_READY="$RUN_DIR/logs/ns3_packet_engine.ready"
NS3_STOP="$RUN_DIR/logs/ns3_packet_engine.stop"
TOWN01_PROFILES="${BAS_TOWN01_PROFILES:-}"
TOWN01_SKIP_HEATMAPS="${BAS_TOWN01_SKIP_HEATMAPS:-0}"
TOWN01_SKIP_FLIGHT_SCENARIO="${BAS_TOWN01_SKIP_FLIGHT_SCENARIO:-0}"
TOWN01_COMPARISON_RUN="${BAS_TOWN01_COMPARISON_RUN:-}"

for flag in "$TOWN01_SKIP_HEATMAPS" "$TOWN01_SKIP_FLIGHT_SCENARIO"; do
  [[ "$flag" == "0" || "$flag" == "1" ]] || {
    printf 'Focused-run flags must be 0 or 1.\n' >&2
    exit 2
  }
done

mapfile -t QOS_VALUES < <(
  python3 - "$QOS" <<'PY'
import sys, yaml
value = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
serial = value["serial_transport"]
classes = value["classes"]
print(serial["chunk_payload_bytes"])
print(serial["reassembly_timeout_ms"])
print(serial["metrics_period_ms"])
print(classes["control"]["baud_rate"])
print(classes["payload"]["baud_rate"])
print(value["channel_state"]["maximum_age_ms"] / 1000)
PY
)
UART_CHUNK_BYTES="${QOS_VALUES[0]}"
UART_REASSEMBLY_MS="${QOS_VALUES[1]}"
UART_METRICS_MS="${QOS_VALUES[2]}"
CONTROL_BAUD="${QOS_VALUES[3]}"
PAYLOAD_BAUD="${QOS_VALUES[4]}"
CHANNEL_STATE_TTL_S="${QOS_VALUES[5]}"

mkdir -p "$RUN_DIR"/{logs,metrics,pcap,heatmaps} "$UART_DIR" "$WORK_DIR"
printf '%q ' "$0" "$@" > "$RUN_DIR/command.txt"
printf '\n' >> "$RUN_DIR/command.txt"
{
  printf 'run_id=%s\n' "$RUN_ID"
  printf 'utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'git_head=%s\n' "$(git -C "$ROOT_DIR" rev-parse --short HEAD 2>/dev/null || printf unknown)"
  printf 'scenario=%s\n' "$SCENARIO"
  printf 'radio=%s\n' "$RADIO"
  printf 'world=%s\n' "$WORLD"
  printf 'ns3_binary=%s\n' "$NS3_BINARY"
  printf 'medium_access_mode=%s\n' "$MEDIUM_ACCESS_MODE"
} > "$RUN_DIR/environment.txt"

managed_pids=()
created_namespaces=()

namespace_exists() {
  ip netns list | awk '{print $1}' | grep -Fxq "$1"
}

terminate_managed() {
  local index pid
  for ((index=${#managed_pids[@]}-1; index>=0; index--)); do
    pid="${managed_pids[$index]}"
    if kill -0 "$pid" 2>/dev/null; then
      kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    fi
  done
  for _ in {1..100}; do
    local alive=0
    for pid in "${managed_pids[@]}"; do
      kill -0 "$pid" 2>/dev/null && alive=1
    done
    ((alive == 0)) && break
    sleep 0.1
  done
  for pid in "${managed_pids[@]}"; do
    if kill -0 "$pid" 2>/dev/null; then
      kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
    fi
    wait "$pid" 2>/dev/null || true
  done
}

cleanup() {
  local status=$?
  trap - EXIT INT TERM HUP
  touch "$NS3_STOP" 2>/dev/null || true
  terminate_managed
  local namespace path
  for namespace in "${created_namespaces[@]}"; do
    ip netns del "$namespace" 2>/dev/null || true
  done
  for path in "$UART_DIR"/*; do
    [[ -e "$path" || -L "$path" ]] && rm -f "$path"
  done
  rmdir "$UART_DIR" "$WORK_DIR" "$RUNTIME_DIR" 2>/dev/null || true
  if [[ "${BAS_TOWN01_HOST_UID:-}" =~ ^[0-9]+$ \
    && "${BAS_TOWN01_HOST_GID:-}" =~ ^[0-9]+$ ]]; then
    chown -R "${BAS_TOWN01_HOST_UID}:${BAS_TOWN01_HOST_GID}" "$RUN_DIR" \
      2>/dev/null || true
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP

wait_for_file() {
  local path="$1"
  local timeout_s="$2"
  local label="$3"
  local deadline=$((SECONDS + timeout_s))
  while [[ ! -s "$path" ]]; do
    ((SECONDS < deadline)) || {
      printf 'Timed out waiting for %s: %s\n' "$label" "$path" >&2
      return 1
    }
    sleep 0.1
  done
}

wait_for_path() {
  local path="$1"
  local timeout_s="$2"
  local label="$3"
  local deadline=$((SECONDS + timeout_s))
  while [[ ! -e "$path" ]]; do
    ((SECONDS < deadline)) || {
      printf 'Timed out waiting for %s: %s\n' "$label" "$path" >&2
      return 1
    }
    sleep 0.1
  done
}

wait_for_tcp() {
  python3 - "$1" "$2" "$3" <<'PY'
import socket, sys, time
host, port, timeout = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
deadline = time.monotonic() + timeout
while time.monotonic() < deadline:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            raise SystemExit(0)
    except OSError:
        time.sleep(0.25)
raise SystemExit(1)
PY
}

printf 'Town01 full-stack run directory: %s\n' "$RUN_DIR"
for namespace in ams-gcs ams-ns3 ams-uav1 ams-uav2 ams-uav3 ams-uav4 ams-uav5; do
  if namespace_exists "$namespace"; then
    printf 'Refusing to reuse existing network namespace: %s\n' "$namespace" >&2
    exit 3
  fi
  ip netns add "$namespace"
  created_namespaces+=("$namespace")
  ip -n "$namespace" link set lo up
done

ip link add v-t1-g type veth peer name v-t1-g-n3
ip link set v-t1-g netns ams-gcs
ip link set v-t1-g-n3 netns ams-ns3
ip -n ams-gcs link set v-t1-g name eth0
for index in 1 2 3 4 5; do
  ip link add "v-t1-u$index" type veth peer name "v-t1-u${index}-n3"
  ip link set "v-t1-u$index" netns "ams-uav$index"
  ip link set "v-t1-u${index}-n3" netns ams-ns3
  ip -n "ams-uav$index" link set "v-t1-u$index" name eth0
done

ip -n ams-ns3 link add br-gcs type bridge
ip netns exec ams-ns3 ip tuntap add dev tap-gcs mode tap user 0
ip -n ams-ns3 link set v-t1-g-n3 master br-gcs
ip -n ams-ns3 link set tap-gcs master br-gcs
for index in 1 2 3 4 5; do
  ip -n ams-ns3 link add "br-uav$index" type bridge
  ip netns exec ams-ns3 ip tuntap add dev "tap-uav$index" mode tap user 0
  ip -n ams-ns3 link set "v-t1-u${index}-n3" master "br-uav$index"
  ip -n ams-ns3 link set "tap-uav$index" master "br-uav$index"
done

ip -n ams-gcs link set eth0 addrgenmode none
ip -n ams-gcs link set eth0 address 02:71:00:00:10:10
ip -n ams-gcs address add 10.71.0.10/24 dev eth0
ip -n ams-gcs link set eth0 up
ip -n ams-gcs route add default via 10.71.0.1 dev eth0
for index in 1 2 3 4 5; do
  namespace="ams-uav$index"
  printf -v endpoint_mac '02:71:%02x:00:10:10' "$index"
  ip -n "$namespace" link set eth0 addrgenmode none
  ip -n "$namespace" link set eth0 address "$endpoint_mac"
  ip -n "$namespace" address add "10.71.$index.10/24" dev eth0
  ip -n "$namespace" link set eth0 up
  ip -n "$namespace" route add default via "10.71.$index.1" dev eth0
done
for interface in v-t1-g-n3 tap-gcs br-gcs; do
  ip -n ams-ns3 link set "$interface" addrgenmode none
  ip -n ams-ns3 link set "$interface" up
done
for index in 1 2 3 4 5; do
  for interface in "v-t1-u${index}-n3" "tap-uav$index" "br-uav$index"; do
    ip -n ams-ns3 link set "$interface" addrgenmode none
    ip -n ams-ns3 link set "$interface" up
  done
done

for instance in 0 1 2 3 4; do
  setsid socat -d -d \
    "pty,raw,echo=0,link=$UART_DIR/control-sitl-$instance,mode=660" \
    "pty,raw,echo=0,link=$UART_DIR/control-adapter-$instance,mode=660" \
    > "$RUN_DIR/logs/control_socat_uav$((instance + 1)).log" 2>&1 &
  managed_pids+=("$!")
  setsid socat -d -d \
    "pty,raw,echo=0,link=$UART_DIR/payload-sitl-$instance,mode=660" \
    "pty,raw,echo=0,link=$UART_DIR/payload-adapter-$instance,mode=660" \
    > "$RUN_DIR/logs/payload_socat_uav$((instance + 1)).log" 2>&1 &
  managed_pids+=("$!")
done
for instance in 0 1 2 3 4; do
  wait_for_path "$UART_DIR/control-sitl-$instance" 10 "control PTY"
  wait_for_path "$UART_DIR/payload-sitl-$instance" 10 "payload PTY"
done

for index in 1 2 3 4 5; do
  instance=$((index - 1))
  setsid ip netns exec "ams-uav$index" python3 -u \
    "$ROOT_DIR/network/scripts/communication_vertical.py" uart-adapter \
    --channel control \
    --uav-id "$index" \
    --framed \
    --baud-rate "$CONTROL_BAUD" \
    --chunk-payload-bytes "$UART_CHUNK_BYTES" \
    --reassembly-timeout-ms "$UART_REASSEMBLY_MS" \
    --metrics-period-ms "$UART_METRICS_MS" \
    --tty "$UART_DIR/control-adapter-$instance" \
    --bind "10.71.$index.10:$((14600 + index))" \
    --peer 10.71.0.10:14600 \
    --event-log "$RUN_DIR/logs/control_uart_uav$index.jsonl" \
    --metrics-output "$RUN_DIR/metrics/control_uart_uav$index.json" \
    --ready-file "$RUN_DIR/logs/control_uart_uav$index.ready" \
    > "$RUN_DIR/logs/control_uart_uav$index.log" 2>&1 &
  managed_pids+=("$!")
  setsid ip netns exec "ams-uav$index" python3 -u \
    "$ROOT_DIR/network/scripts/communication_vertical.py" uart-adapter \
    --channel payload \
    --uav-id "$index" \
    --framed \
    --baud-rate "$PAYLOAD_BAUD" \
    --chunk-payload-bytes "$UART_CHUNK_BYTES" \
    --reassembly-timeout-ms "$UART_REASSEMBLY_MS" \
    --metrics-period-ms "$UART_METRICS_MS" \
    --tty "$UART_DIR/payload-adapter-$instance" \
    --bind "10.71.$index.10:$((14700 + index))" \
    --peer 10.71.0.10:14700 \
    --event-log "$RUN_DIR/logs/payload_uart_uav$index.jsonl" \
    --metrics-output "$RUN_DIR/metrics/payload_uart_uav$index.json" \
    --ready-file "$RUN_DIR/logs/payload_uart_uav$index.ready" \
    > "$RUN_DIR/logs/payload_uart_uav$index.log" 2>&1 &
  managed_pids+=("$!")
  setsid ip netns exec "ams-uav$index" python3 -u \
    "$ROOT_DIR/scripts/product/town01_full_stack_scenario.py" additional-agent \
    --index "$index" \
    --event-log "$RUN_DIR/logs/additional_uav$index.jsonl" \
    --ready-file "$RUN_DIR/logs/additional_uav$index.ready" \
    > "$RUN_DIR/logs/additional_uav$index.log" 2>&1 &
  managed_pids+=("$!")
done
for index in 1 2 3 4 5; do
  wait_for_file "$RUN_DIR/logs/control_uart_uav$index.ready" 10 "control adapter"
  wait_for_file "$RUN_DIR/logs/payload_uart_uav$index.ready" 10 "payload adapter"
  wait_for_file "$RUN_DIR/logs/additional_uav$index.ready" 10 "additional-data agent"
done

cd "$WORK_DIR"
setsid ros2 launch multiagent_simulation multiagent_simulation.launch.py \
  robots_config_file:="$SCENARIO" \
  world_file:="$WORLD" \
  robot_model:=iris_radio_headless \
  gui:=false \
  rviz:=false \
  headless_rendering:=true \
  generate_sensor_models:=false \
  use_mapping_camera:=false \
  use_navigation_camera:=false \
  use_zed_camera:=false \
  start_mavproxy:=false \
  sitl_extra_defaults:="$ROOT_DIR/network/config/town01_sitl.parm" \
  control_uart:="$UART_DIR/control-sitl-{instance}" \
  payload_uart:="$UART_DIR/payload-sitl-{instance}" \
  > "$RUN_DIR/logs/gazebo_sitl.log" 2>&1 &
managed_pids+=("$!")

setsid python3 "$ROOT_DIR/network/position_tracker/tracker.py" \
  --scenario "$SCENARIO" \
  --jammers-config "$ROOT_DIR/network/config/jammers_rock_demo.yaml" \
  --output-json "$NODE_STATE" \
  --output-jsonl "$NODE_EVENTS" \
  > "$RUN_DIR/logs/position_tracker.log" 2>&1 &
managed_pids+=("$!")

setsid stdbuf -oL gz topic -e -t /world/map/stats \
  > "$RUN_DIR/logs/gazebo_stats.log" 2>&1 &
managed_pids+=("$!")

python3 "$ROOT_DIR/scripts/product/town01_stack_health.py" \
  --scenario "$SCENARIO" \
  --tracker-state "$NODE_STATE" \
  --tracker-events "$NODE_EVENTS" \
  --output "$RUN_DIR/metrics/health.json" \
  --timeout-s 180

cd "$ROOT_DIR"
setsid python3 -u "$ROOT_DIR/network/radio_provider/provider.py" serve \
  --mode real_sionna \
  --host 127.0.0.1 \
  --port 5090 \
  --run-dir "$RUN_DIR" \
  --scenario "$SCENARIO" \
  --radio-config "$RADIO" \
  --jammers-config "$ROOT_DIR/network/config/jammers_rock_demo.yaml" \
  > "$RUN_DIR/logs/sionna_provider.log" 2>&1 &
managed_pids+=("$!")
wait_for_tcp 127.0.0.1 5090 120

setsid python3 -u "$ROOT_DIR/scripts/product/town01_radio_state.py" \
  --node-state "$NODE_STATE" \
  --radio-config "$RADIO" \
  --state-output "$SIONNA_STATES" \
  --metrics-output "$RUN_DIR/metrics/radio_links.csv" \
  --ready-file "$SIONNA_READY" \
  --period-s 5 \
  --ttl-s "$CHANNEL_STATE_TTL_S" \
  > "$RUN_DIR/logs/town01_radio_state.log" 2>&1 &
managed_pids+=("$!")
wait_for_file "$SIONNA_READY" 120 "first 30-cell Sionna state"

rm -f "$NS3_READY" "$NS3_STOP"
EVENT_EPOCH="$(date +%s%N)"
NS3_ENV=(
  RUN_DIR="$RUN_DIR" \
  UAV_COUNT=5 \
  EVENT_EPOCH="$EVENT_EPOCH" \
  NS3_NS=ams-ns3 \
  NS3_DIR="$NS3_DIR" \
  NS3_STOCK_DIR="$NS3_DIR" \
  TAP_GCS=tap-gcs \
  TAP_UAVS=tap-uav1,tap-uav2,tap-uav3,tap-uav4,tap-uav5 \
  DURATION_MS=600000 \
  RADIO_FILE="$RADIO" \
  QOS_FILE="$QOS" \
  MEDIUM_ACCESS_MODE="$MEDIUM_ACCESS_MODE" \
  SIONNA_IPC_ENABLED=1 \
  SIONNA_STATE_FILE="$SIONNA_STATES" \
  SIONNA_MAX_UPDATES_PER_POLL=128
)
if [[ "$MEDIUM_ACCESS_MODE" == "centralized_priority_scheduler_over_csma_channel" ]]; then
  NS3_ENV+=(ENGINE_PROFILE=gated)
fi
setsid env "${NS3_ENV[@]}" "$NS3_RUNNER" \
  > "$RUN_DIR/logs/ns3_packet_engine.log" 2>&1 &
NS3_PID=$!
managed_pids+=("$NS3_PID")
wait_for_file "$NS3_READY" 30 "ns-3 packet engine"

setsid python3 -u "$ROOT_DIR/scripts/product/town01_runtime_monitor.py" \
  --output "$RUN_DIR/logs/runtime_resources.jsonl" \
  --stop-file "$NS3_STOP" \
  > "$RUN_DIR/logs/runtime_monitor.log" 2>&1 &
managed_pids+=("$!")

SCENARIO_STATUS=0
if [[ "$TOWN01_SKIP_FLIGHT_SCENARIO" == "0" ]]; then
  set +e
  ip netns exec ams-gcs python3 -u "$ROOT_DIR/scripts/product/town01_full_stack_scenario.py" run \
    --run-dir "$RUN_DIR" \
    --node-state "$NODE_STATE" \
    > "$RUN_DIR/logs/scenario.log" 2>&1
  SCENARIO_STATUS=$?
  set -e
else
  printf 'Flight lifecycle scenario skipped by BAS_TOWN01_SKIP_FLIGHT_SCENARIO=1.\n' \
    > "$RUN_DIR/logs/scenario.log"
fi

PROFILE_STATUS=1
if ((SCENARIO_STATUS == 0)); then
  PROFILE_ARGS=()
  if [[ -n "$TOWN01_PROFILES" ]]; then
    PROFILE_ARGS=(--profiles "$TOWN01_PROFILES")
  fi
  set +e
  python3 -u "$ROOT_DIR/scripts/product/town01_communication_profiles.py" run \
    --run-dir "$RUN_DIR" \
    --qos "$QOS" \
    --medium-access-mode "$MEDIUM_ACCESS_MODE" \
    "${PROFILE_ARGS[@]}" \
    > "$RUN_DIR/logs/communication_profiles.log" 2>&1
  PROFILE_STATUS=$?
  set -e
fi

HEATMAP_STATUS=0
if [[ "$TOWN01_SKIP_HEATMAPS" == "0" ]]; then
  set +e
  python3 "$ROOT_DIR/scripts/product/town01_heatmaps.py" \
    --run-dir "$RUN_DIR" \
    --points 7 \
    > "$RUN_DIR/logs/heatmaps.log" 2>&1
  HEATMAP_STATUS=$?
  set -e
else
  printf 'Heatmaps skipped by BAS_TOWN01_SKIP_HEATMAPS=1.\n' \
    > "$RUN_DIR/logs/heatmaps.log"
fi

set +e
python3 "$ROOT_DIR/scripts/product/collect_town01_runtime_topology.py" \
  --run-dir "$RUN_DIR" \
  > "$RUN_DIR/logs/runtime_topology.log" 2>&1
TOPOLOGY_STATUS=$?
set -e

touch "$NS3_STOP"
for _ in {1..100}; do
  kill -0 "$NS3_PID" 2>/dev/null || break
  sleep 0.1
done
if kill -0 "$NS3_PID" 2>/dev/null; then
  printf 'ns-3 packet engine did not stop within 10 seconds; refusing an unflushed summary.\n' >&2
  exit 1
fi
wait "$NS3_PID"

ip netns exec ams-gcs python3 "$ROOT_DIR/network/scripts/communication_vertical.py" down-probe \
  --bind 10.71.0.10:14600 \
  --target 10.71.1.10:14601 \
  --framed \
  --channel control \
  --uav-id 1 \
  --chunk-payload-bytes "$UART_CHUNK_BYTES" \
  --timeout-s 3 \
  --output "$RUN_DIR/metrics/ns3_stopped_probe.json" \
  > "$RUN_DIR/logs/ns3_stopped_probe.log" 2>&1

SUMMARY_STATUS=0
if [[ "$TOWN01_SKIP_FLIGHT_SCENARIO" == "0" && ( "$MEDIUM_ACCESS_MODE" == "stock_ns3_csma" || -n "$TOWN01_PROFILES" ) ]]; then
  set +e
  python3 "$ROOT_DIR/scripts/product/summarize_medium_access_baseline.py" --run-dir "$RUN_DIR" \
    > "$RUN_DIR/logs/summary.log" 2>&1
  SUMMARY_STATUS=$?
  set -e
elif [[ "$TOWN01_SKIP_FLIGHT_SCENARIO" == "0" ]]; then
  set +e
  python3 "$ROOT_DIR/scripts/product/summarize_town01_full_stack.py" --run-dir "$RUN_DIR" \
    > "$RUN_DIR/logs/summary.log" 2>&1
  SUMMARY_STATUS=$?
  set -e
else
  printf 'Full-stack summary skipped because the focused run omitted the flight lifecycle.\n' \
    > "$RUN_DIR/logs/summary.log"
fi

if [[ "$MEDIUM_ACCESS_MODE" == "stock_ns3_csma" && -n "$TOWN01_COMPARISON_RUN" && "$SUMMARY_STATUS" == "0" ]]; then
  python3 "$ROOT_DIR/scripts/product/compare_medium_access_runs.py" \
    --stock-run "$RUN_DIR" \
    --centralized-run "$TOWN01_COMPARISON_RUN" \
    --output-run "$RUN_DIR" \
    > "$RUN_DIR/logs/medium_access_comparison.log" 2>&1
fi

printf 'Town01 full-stack run complete: %s\n' "$RUN_DIR"
printf 'Scenario status=%s profiles status=%s topology status=%s heatmap status=%s summary status=%s\n' \
  "$SCENARIO_STATUS" "$PROFILE_STATUS" "$TOPOLOGY_STATUS" "$HEATMAP_STATUS" "$SUMMARY_STATUS"
if ((SCENARIO_STATUS != 0 || PROFILE_STATUS != 0 || TOPOLOGY_STATUS != 0 \
  || HEATMAP_STATUS != 0 || SUMMARY_STATUS != 0)); then
  exit 1
fi
