#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE="${BAS_CONTAINER_IMAGE:-multiagent_simulation:latest}"
CONTAINER_NAME="${BAS_NATIVE_FIVE_CONTAINER_NAME:-bas-v2-native-radio-five-uav}"

run_in_container() {
  command -v docker >/dev/null 2>&1 || { printf 'Docker is required.\n' >&2; return 2; }
  local image_id
  image_id="$(docker image inspect --format '{{.Id}}' "$IMAGE" 2>/dev/null)" || {
    printf 'Runtime image is unavailable: %s\n' "$IMAGE" >&2
    return 2
  }
  [[ "$(docker inspect --format '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null || true)" != true ]] || {
    printf 'Native five-UAV container is already running: %s\n' "$CONTAINER_NAME" >&2
    return 3
  }
  python3 "$ROOT_DIR/scripts/product/prepare_town01_gazebo.py"
  local -a gpu_args=()
  if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
    gpu_args=(--gpus all -e NVIDIA_VISIBLE_DEVICES=all -e NVIDIA_DRIVER_CAPABILITIES=graphics,utility,compute)
  fi
  docker run --rm \
    --name "$CONTAINER_NAME" \
    --label bas.product=native-radio-five-uav \
    --privileged --network=host --user 0:0 \
    "${gpu_args[@]}" \
    -e BAS_NATIVE_FIVE_IN_CONTAINER=1 \
    -e BAS_NATIVE_FIVE_RUN_ID="${BAS_NATIVE_FIVE_RUN_ID:-}" \
    -e BAS_NATIVE_FIVE_SKIP_BUILD="${BAS_NATIVE_FIVE_SKIP_BUILD:-0}" \
    -e BAS_NATIVE_FIVE_ONE_UAV_RUN="${BAS_NATIVE_FIVE_ONE_UAV_RUN:-}" \
    -e BAS_NATIVE_FIVE_GAZEBO_RTF="${BAS_NATIVE_FIVE_GAZEBO_RTF:-1.0}" \
    -e BAS_NATIVE_CHANNEL_STATE_MAX_AGE_S="${BAS_NATIVE_CHANNEL_STATE_MAX_AGE_S:-2.0}" \
    -e BAS_NATIVE_UPDATE_DISTANCE_THRESHOLD_M="${BAS_NATIVE_UPDATE_DISTANCE_THRESHOLD_M:-1.0}" \
    -e BAS_NATIVE_FIVE_TIMEOUT_SCALE="${BAS_NATIVE_FIVE_TIMEOUT_SCALE:-5.0}" \
    -e BAS_NATIVE_FIVE_HOST_UID="$(id -u)" \
    -e BAS_NATIVE_FIVE_HOST_GID="$(id -g)" \
    -e XDG_RUNTIME_DIR=/tmp/bas-native-five-xdg \
    -e PYTHONPATH=/home/ubuntu/.local/lib/python3.10/site-packages \
    -v "$ROOT_DIR":/workspace/multiagent_simulation \
    -v "$ROOT_DIR":/home/bas/bas_v2 \
    -w /workspace/multiagent_simulation \
    "$image_id" bash -lc '
      set -eo pipefail
      mkdir -p "$XDG_RUNTIME_DIR"
      chmod 700 "$XDG_RUNTIME_DIR"
      set +u
      source /opt/ros/humble/setup.bash
      source /workspace/ardu_ws/install/setup.bash
      source /workspace/multiagent_simulation/install/setup.bash
      export PATH="/home/ubuntu/.local/bin:$PATH"
      export GZ_VERSION=harmonic
      export GZ_SIM_RESOURCE_PATH="${GZ_SIM_RESOURCE_PATH:-}:$PWD/src/multiagent_simulation/models:$PWD/src/multiagent_simulation/worlds:$PWD/src:$PWD/.external/cavise_maps/Town01/gazebo"
      set -u
      exec ./network/ns3/run_native_radio_five_uav.sh
    '
}

if [[ "${BAS_NATIVE_FIVE_IN_CONTAINER:-0}" != 1 ]]; then
  run_in_container
  exit $?
fi

((EUID == 0)) || { printf 'Root is required in the privileged runtime container.\n' >&2; exit 2; }
for required_command in cmake c++ gz ip nproc python3 ros2 socat ss stdbuf taskset tcpdump; do
  command -v "$required_command" >/dev/null 2>&1 || {
    printf 'Required command is unavailable: %s\n' "$required_command" >&2
    exit 2
  }
done

RUN_ID="${BAS_NATIVE_FIVE_RUN_ID:-native-five-$(date -u +%Y%m%dT%H%M%SZ)}"
[[ "$RUN_ID" =~ ^[a-zA-Z0-9_.-]+$ ]] || { printf 'Unsafe run ID: %s\n' "$RUN_ID" >&2; exit 2; }
RUN_DIR="$ROOT_DIR/runs/native-radio-realtime/$RUN_ID"
[[ ! -e "$RUN_DIR" ]] || { printf 'Run directory exists: %s\n' "$RUN_DIR" >&2; exit 2; }
RUNTIME_DIR="/tmp/bas-native-five-$RUN_ID"
ONE_UAV_RUN="${BAS_NATIVE_FIVE_ONE_UAV_RUN:-}"
UART_DIR="$RUNTIME_DIR/uart"
WORK_DIR="$RUNTIME_DIR/work"
NS3_DIR="$ROOT_DIR/.external/ns-3-sionna-native"
PYTHON_DEPS="$NS3_DIR/.python-deps-py310"
PYTHON_TOOLING="$NS3_DIR/.tooling-py310"
PROJECT_SOURCE="$ROOT_DIR/network/ns3/scratch/upstream-sionna-tap-spike.cc"
UPSTREAM_SOURCE="$NS3_DIR/scratch/upstream-sionna-tap-spike.cc"
BINARY="$NS3_DIR/build/scratch/ns3.48-upstream-sionna-tap-spike-default"
PATCH_FILE="$ROOT_DIR/network/ns3/patches/mr2608-spike-compatibility.patch"
REALTIME_CACHE_PATCH="$ROOT_DIR/network/ns3/patches/mr2608-realtime-scene-cache.patch"
SCENARIO="$ROOT_DIR/network/config/scenario_5uav_town01_native_product.yaml"
WORLD="$ROOT_DIR/.external/cavise_maps/Town01/gazebo/town01.sdf"
CAMERA_FRAGMENT="$ROOT_DIR/network/ns3/runtime_live_cameras.sdf.inc"
GAZEBO_RTF="${BAS_NATIVE_FIVE_GAZEBO_RTF:-1.0}"
SCENARIO_TIMEOUT_SCALE="${BAS_NATIVE_FIVE_TIMEOUT_SCALE:-5.0}"
CHANNEL_STATE_MAX_AGE_S="${BAS_NATIVE_CHANNEL_STATE_MAX_AGE_S:-2.0}"
UPDATE_DISTANCE_THRESHOLD_M="${BAS_NATIVE_UPDATE_DISTANCE_THRESHOLD_M:-1.0}"
LAUNCH_WORLD="$WORK_DIR/town01-native-live-cameras.sdf"
SCENE="$ROOT_DIR/.external/cavise_maps/Town01/map/scene.xml"
NODE_STATE="$RUN_DIR/logs/node_state.json"
NODE_EVENTS="$RUN_DIR/logs/node_state.jsonl"
PHASE_FILE="$RUN_DIR/logs/current_phase.txt"
SCHEDULE_FILE="$RUN_DIR/logs/additional_schedule.json"
NS3_READY="$RUN_DIR/logs/ns3.ready"
MONITOR_STOP="$RUN_DIR/logs/runtime_monitor.stop"
ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-$((100 + $(printf '%s' "$RUN_ID" | cksum | awk '{print $1}') % 100))}"
GZ_PARTITION="${GZ_PARTITION:-native_five_${RUN_ID//[^a-zA-Z0-9_]/_}}"
CPU_COUNT="$(nproc)"
if ((CPU_COUNT >= 16)); then
  STACK_CPUSET="0-7"
  RADIO_CPUSET="8-$((CPU_COUNT - 1))"
elif ((CPU_COUNT >= 4)); then
  STACK_CPUSET="0-$(((CPU_COUNT / 2) - 1))"
  RADIO_CPUSET="$((CPU_COUNT / 2))-$((CPU_COUNT - 1))"
else
  STACK_CPUSET="0-$((CPU_COUNT - 1))"
  RADIO_CPUSET="$STACK_CPUSET"
fi

for required_file in "$PROJECT_SOURCE" "$PATCH_FILE" "$REALTIME_CACHE_PATCH" "$SCENARIO" "$WORLD" "$SCENE" "$CAMERA_FRAGMENT"; do
  [[ -f "$required_file" ]] || { printf 'Missing input: %s\n' "$required_file" >&2; exit 2; }
done
[[ "$(git -c safe.directory="$NS3_DIR" -C "$NS3_DIR" rev-parse HEAD)" == d2add90b452d600cfb4859baed8e9ea633519447 ]] || {
  printf 'Official ns-3.48 exact revision is absent.\n' >&2
  exit 2
}
git -c safe.directory="$NS3_DIR" -C "$NS3_DIR" apply --reverse --check "$PATCH_FILE" || {
  printf 'Compatibility patch does not match exactly.\n' >&2
  exit 2
}
if ! git -c safe.directory="$NS3_DIR" -C "$NS3_DIR" apply --reverse --check "$REALTIME_CACHE_PATCH"; then
  git -c safe.directory="$NS3_DIR" -C "$NS3_DIR" apply "$REALTIME_CACHE_PATCH" || {
    printf 'Realtime scene-cache patch does not apply to the compatible upstream checkout.\n' >&2
    exit 2
  }
fi
PYTHONPATH="$PYTHON_DEPS" python3 - <<'PY'
from importlib.metadata import version
assert version("sionna") == "1.2.0"
assert version("sionna-rt") == "1.2.0"
assert version("pybind11") == "2.11.1"
assert version("cppyy") == "3.5.0"
PY
[[ -x "$PYTHON_TOOLING/bin/cmake" ]] || { printf 'Pinned CMake tooling is absent.\n' >&2; exit 2; }

mkdir -p "$RUN_DIR"/{logs,metrics,pcap,screenshots,plots} "$UART_DIR" "$WORK_DIR"
python3 "$ROOT_DIR/scripts/product/inject_native_radio_runtime_cameras.py" \
  --world "$WORLD" --fragment "$CAMERA_FRAGMENT" --output "$LAUNCH_WORLD"
printf '%q ' "$0" "$@" > "$RUN_DIR/command.txt"
printf '\n' >> "$RUN_DIR/command.txt"
printf 'preflight\n' > "$PHASE_FILE"
printf '{}\n' > "$SCHEDULE_FILE"
if [[ "$GAZEBO_RTF" != 1.0 ]]; then
  printf 'Gazebo RTF must be 1.0 for a realtime run; got %s\n' "$GAZEBO_RTF" >&2
  exit 2
fi

export PATH="$PYTHON_TOOLING/bin:$PATH"
export PYTHONPATH="$PYTHON_TOOLING:$PYTHON_DEPS:${PYTHONPATH:-}"
cp "$PROJECT_SOURCE" "$UPSTREAM_SOURCE"
if [[ "${BAS_NATIVE_FIVE_SKIP_BUILD:-0}" == 1 ]]; then
  [[ -x "$BINARY" ]] || { printf 'Requested binary reuse but binary is absent.\n' >&2; exit 2; }
  printf 'Reused focused native target after exact project/upstream source synchronization.\n' \
    > "$RUN_DIR/logs/ns3_build.log"
else
  (
    cd "$NS3_DIR"
    for build_directory in "$NS3_DIR/build" "$NS3_DIR/cmake-cache"; do
      [[ "$(dirname "$build_directory")" == "$NS3_DIR" ]] || exit 2
      rm -rf "$build_directory"
    done
    PYTHONPATH="$PYTHON_TOOLING:$PYTHON_DEPS" ./ns3 configure --enable-examples --enable-tests --enable-python-bindings
    PYTHONPATH="$PYTHON_TOOLING:$PYTHON_DEPS" ./ns3 build upstream-sionna-tap-spike
  ) > "$RUN_DIR/logs/ns3_build.log" 2>&1
fi
[[ -x "$BINARY" ]] || { printf 'Focused native target did not build.\n' >&2; exit 1; }

mapfile -t DEPENDENCIES < <(PYTHONPATH="$PYTHON_DEPS" python3 - <<'PY'
from importlib.metadata import version
for name in ("sionna", "sionna-rt", "mitsuba", "drjit", "pybind11", "cppyy"):
    print(f"{name}={version(name)}")
PY
)
{
  printf 'run_id=%s\n' "$RUN_ID"
  printf 'started_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'git_head=%s\n' "$(git -C "$ROOT_DIR" rev-parse HEAD)"
  printf 'ns3_version=3.48\n'
  printf 'ns3_exact_sha=%s\n' "$(git -c safe.directory="$NS3_DIR" -C "$NS3_DIR" rev-parse HEAD)"
  printf 'ns3_compatibility_patch=true\n'
  printf 'python_version=%s\n' "$(python3 --version | awk '{print $2}')"
  printf '%s\n' "${DEPENDENCIES[@]}"
  printf 'compiler_version=%s\n' "$(c++ --version | head -n1)"
  printf 'cmake_version=%s\n' "$(cmake --version | head -n1 | awk '{print $3}')"
  printf 'ros_domain_id=%s\n' "$ROS_DOMAIN_ID"
  printf 'gz_partition=%s\n' "$GZ_PARTITION"
  printf 'scenario=%s\nworld=%s\nscene=%s\n' "$SCENARIO" "$WORLD" "$SCENE"
  printf 'launch_world=%s\ngazebo_requested_rtf=%s\nscenario_timeout_scale=%s\n' \
    "$LAUNCH_WORLD" "$GAZEBO_RTF" "$SCENARIO_TIMEOUT_SCALE"
  printf 'stack_cpuset=%s\nradio_cpuset=%s\n' "$STACK_CPUSET" "$RADIO_CPUSET"
  printf 'profile=generic_native_spectrum_aloha_reference\n'
  printf 'technology_specific_modem=false\n'
  printf 'uav_count=5\nradio_node_count=6\nshared_spectrum_channels=1\n'
  printf 'carrier_hz=2400000000\nbandwidth_hz=5000000\nphy_rate_bps=1000000\ntx_power_w=0.01\n'
  printf 'solver_profile=realtime_minimal_solver_profile\n'
  printf 'cache_policy=displacement_or_time\nchannel_state_max_age_s=%s\nendpoint_displacement_threshold_m=%s\n' \
    "$CHANNEL_STATE_MAX_AGE_S" "$UPDATE_DISTANCE_THRESHOLD_M"
  printf 'neighbor_discovery_mode=preconfigured_static_neighbors\n'
  printf 'reason=upstream_ideal_phy_arp_reentrancy_limit\npacket_outcome_affected=false\n'
} > "$RUN_DIR/environment.txt"

managed_pids=()
created_namespaces=()
NS3_PID=""
NS3_LOGGER_PID=""
capture_pids=()
CLEANUP_ACTIVE=0

stop_pid() {
  local pid="${1:-}"
  [[ -n "$pid" ]] || return 0
  if kill -0 "$pid" 2>/dev/null; then
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
  fi
  wait "$pid" 2>/dev/null || true
}

cleanup() {
  local status=$?
  ((CLEANUP_ACTIVE == 0)) || exit "$status"
  CLEANUP_ACTIVE=1
  trap - EXIT INT TERM HUP
  [[ -n "$NS3_PID" ]] && stop_pid "$NS3_PID"
  local pid namespace
  for pid in "${capture_pids[@]}"; do
    kill -INT -- "-$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  done
  touch "$MONITOR_STOP" 2>/dev/null || true
  for ((pid_index=${#managed_pids[@]}-1; pid_index>=0; pid_index--)); do
    stop_pid "${managed_pids[$pid_index]}"
  done
  [[ -n "$NS3_LOGGER_PID" ]] && wait "$NS3_LOGGER_PID" 2>/dev/null || true
  for namespace in "${created_namespaces[@]}"; do
    ip netns del "$namespace" 2>/dev/null || true
  done
  if [[ -d "$RUN_DIR" ]]; then
    chown -R "${BAS_NATIVE_FIVE_HOST_UID:-0}:${BAS_NATIVE_FIVE_HOST_GID:-0}" "$RUN_DIR" 2>/dev/null || true
  fi
  [[ "$RUNTIME_DIR" == /tmp/bas-native-five-* ]] && rm -rf "$RUNTIME_DIR"
  exit "$status"
}
trap cleanup EXIT INT TERM HUP

wait_for_file() {
  local path="$1"
  local timeout_s="$2"
  local label="$3"
  local deadline=$((SECONDS + timeout_s))
  while [[ ! -s "$path" ]]; do
    ((SECONDS < deadline)) || { printf 'Timed out waiting for %s: %s\n' "$label" "$path" >&2; return 1; }
    sleep 0.1
  done
}

namespace_exists() { ip netns list | awk '{print $1}' | grep -Fxq "$1"; }

for namespace in ams-gcs ams-ns3 ams-uav1 ams-uav2 ams-uav3 ams-uav4 ams-uav5; do
  namespace_exists "$namespace" && { printf 'Namespace already exists: %s\n' "$namespace" >&2; exit 3; }
  ip netns add "$namespace"
  created_namespaces+=("$namespace")
  ip -n "$namespace" link set lo up
done

ip link add v-n5-g type veth peer name v-n5-g-n3
ip link set v-n5-g netns ams-gcs
ip link set v-n5-g-n3 netns ams-ns3
ip -n ams-gcs link set v-n5-g name eth0
for index in 1 2 3 4 5; do
  ip link add "v-n5-u$index" type veth peer name "v-n5-u${index}-n3"
  ip link set "v-n5-u$index" netns "ams-uav$index"
  ip link set "v-n5-u${index}-n3" netns ams-ns3
  ip -n "ams-uav$index" link set "v-n5-u$index" name eth0
done
ip -n ams-ns3 link add br-gcs type bridge
ip -n ams-ns3 link set br-gcs type bridge mcast_snooping 0
ip netns exec ams-ns3 ip tuntap add dev tap-gcs mode tap user 0
ip -n ams-ns3 link set v-n5-g-n3 master br-gcs
ip -n ams-ns3 link set tap-gcs master br-gcs
for index in 1 2 3 4 5; do
  ip -n ams-ns3 link add "br-uav$index" type bridge
  ip -n ams-ns3 link set "br-uav$index" type bridge mcast_snooping 0
  ip netns exec ams-ns3 ip tuntap add dev "tap-uav$index" mode tap user 0
  ip -n ams-ns3 link set "v-n5-u${index}-n3" master "br-uav$index"
  ip -n ams-ns3 link set "tap-uav$index" master "br-uav$index"
done
ip -n ams-gcs link set eth0 addrgenmode none
ip -n ams-gcs link set eth0 address 02:71:00:00:10:10
ip -n ams-gcs address add 10.71.0.10/24 dev eth0
ip -n ams-gcs link set eth0 up
ip -n ams-gcs route add default via 10.71.0.1 dev eth0
ip -n ams-gcs route add 239.71.0.1/32 via 10.71.0.1 dev eth0
for index in 1 2 3 4 5; do
  printf -v endpoint_mac '02:71:%02x:00:10:10' "$index"
  ip -n "ams-uav$index" link set eth0 addrgenmode none
  ip -n "ams-uav$index" link set eth0 address "$endpoint_mac"
  ip -n "ams-uav$index" address add "10.71.$index.10/24" dev eth0
  ip -n "ams-uav$index" link set eth0 up
  ip -n "ams-uav$index" route add default via "10.71.$index.1" dev eth0
done
for interface in v-n5-g-n3 tap-gcs br-gcs; do
  ip -n ams-ns3 link set "$interface" addrgenmode none
  ip -n ams-ns3 link set "$interface" up
done
for index in 1 2 3 4 5; do
  for interface in "v-n5-u${index}-n3" "tap-uav$index" "br-uav$index"; do
    ip -n ams-ns3 link set "$interface" addrgenmode none
    ip -n ams-ns3 link set "$interface" up
  done
done
ip -n ams-gcs neigh replace 10.71.0.1 lladdr 02:71:00:00:00:01 nud permanent dev eth0
for index in 1 2 3 4 5; do
  ip -n "ams-uav$index" neigh replace "10.71.$index.1" lladdr 02:71:ff:00:00:01 nud permanent dev eth0
done
{
  ip -n ams-gcs neigh show dev eth0
  for index in 1 2 3 4 5; do ip -n "ams-uav$index" neigh show dev eth0; done
} > "$RUN_DIR/logs/static_neighbors.txt"

for instance in 0 1 2 3 4; do
  setsid socat -d -d "pty,raw,echo=0,link=$UART_DIR/control-sitl-$instance,mode=660" \
    "pty,raw,echo=0,link=$UART_DIR/control-adapter-$instance,mode=660" \
    > "$RUN_DIR/logs/control_socat_uav$((instance + 1)).log" 2>&1 &
  managed_pids+=("$!")
  setsid socat -d -d "pty,raw,echo=0,link=$UART_DIR/payload-sitl-$instance,mode=660" \
    "pty,raw,echo=0,link=$UART_DIR/payload-adapter-$instance,mode=660" \
    > "$RUN_DIR/logs/payload_socat_uav$((instance + 1)).log" 2>&1 &
  managed_pids+=("$!")
done
for instance in 0 1 2 3 4; do
  for path in "$UART_DIR/control-sitl-$instance" "$UART_DIR/payload-sitl-$instance"; do
    for _ in $(seq 1 100); do [[ -e "$path" ]] && break; sleep 0.1; done
    [[ -e "$path" ]] || { printf 'PTY missing: %s\n' "$path" >&2; exit 1; }
  done
done

for index in 1 2 3 4 5; do
  instance=$((index - 1))
  for channel in control payload; do
    if [[ "$channel" == control ]]; then base_port=14600; else base_port=14700; fi
    setsid ip netns exec "ams-uav$index" python3 -u \
      "$ROOT_DIR/network/scripts/communication_vertical.py" uart-adapter \
      --channel "$channel" --uav-id "$index" --framed --baud-rate 115200 \
      --tty "$UART_DIR/$channel-adapter-$instance" \
      --bind "10.71.$index.10:$((base_port + index))" --peer "10.71.0.10:$base_port" \
      --event-log "$RUN_DIR/logs/${channel}_uart_uav$index.jsonl" \
      --metrics-output "$RUN_DIR/metrics/${channel}_uart_uav$index.json" \
      --ready-file "$RUN_DIR/logs/${channel}_uart_uav$index.ready" \
      > "$RUN_DIR/logs/${channel}_uart_uav$index.log" 2>&1 &
    managed_pids+=("$!")
  done
  setsid ip netns exec "ams-uav$index" python3 -u \
    "$ROOT_DIR/scripts/product/native_radio_five_uav_scenario.py" additional-agent \
    --index "$index" --schedule-file "$SCHEDULE_FILE" \
    --event-log "$RUN_DIR/logs/additional_uav$index.jsonl" \
    --ready-file "$RUN_DIR/logs/additional_uav$index.ready" \
    > "$RUN_DIR/logs/additional_uav$index.log" 2>&1 &
  managed_pids+=("$!")
done
for index in 1 2 3 4 5; do
  wait_for_file "$RUN_DIR/logs/control_uart_uav$index.ready" 15 "control UART adapter"
  wait_for_file "$RUN_DIR/logs/payload_uart_uav$index.ready" 15 "payload UART adapter"
  wait_for_file "$RUN_DIR/logs/additional_uav$index.ready" 15 "additional endpoint"
done

export ROS_DOMAIN_ID GZ_PARTITION
cd "$WORK_DIR"
setsid taskset -c "$STACK_CPUSET" ros2 launch multiagent_simulation multiagent_simulation.launch.py \
  robots_config_file:="$SCENARIO" world_file:="$LAUNCH_WORLD" robot_model:=iris_radio_headless \
  gui:=false rviz:=false headless_rendering:=true generate_sensor_models:=false \
  use_mapping_camera:=false use_navigation_camera:=false use_zed_camera:=false \
  start_mavproxy:=false sitl_extra_defaults:="$ROOT_DIR/network/config/town01_sitl.parm" \
  control_uart:="$UART_DIR/control-sitl-{instance}" \
  payload_uart:="$UART_DIR/payload-sitl-{instance}" \
  > "$RUN_DIR/logs/gazebo_sitl.log" 2>&1 &
managed_pids+=("$!")
cd "$ROOT_DIR"
setsid taskset -c "$STACK_CPUSET" python3 "$ROOT_DIR/network/position_tracker/tracker.py" \
  --scenario "$SCENARIO" --output-json "$NODE_STATE" --output-jsonl "$NODE_EVENTS" \
  --rate-hz 10 --stale-after-s 1.0 \
  > "$RUN_DIR/logs/position_tracker.log" 2>&1 &
managed_pids+=("$!")
setsid stdbuf -oL gz topic -e -t /world/map/stats > "$RUN_DIR/logs/gazebo_stats.log" 2>&1 &
managed_pids+=("$!")

fresh=0
for _ in $(seq 1 2400); do
  if python3 - "$NODE_STATE" <<'PY' >/dev/null 2>&1
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
nodes = {node["id"]: node for node in value.get("nodes", [])}
assert value.get("source") == "ros_odometry"
assert not value.get("missing_nodes") and not value.get("stale_nodes")
assert all(not nodes[f"uav{i}"].get("stale") for i in range(1, 6))
PY
  then fresh=1; break; fi
  sleep 0.1
done
((fresh == 1)) || { printf 'Five live ROS odometry streams did not become fresh.\n' >&2; exit 1; }
python3 "$ROOT_DIR/scripts/product/town01_stack_health.py" \
  --scenario "$SCENARIO" --tracker-state "$NODE_STATE" --tracker-events "$NODE_EVENTS" \
  --output "$RUN_DIR/metrics/health.json" --timeout-s 180 \
  > "$RUN_DIR/logs/stack_health.log" 2>&1
for index in 1 2 3 4 5; do
  timeout 15 ros2 topic info --verbose "/uav$index/odometry" \
    > "$RUN_DIR/logs/odometry_uav$index.txt" 2>&1 || true
done

for camera in overview obstacle uav_focus; do
  setsid ros2 run ros_gz_image image_bridge "/native_radio/$camera/image" \
    > "$RUN_DIR/logs/gazebo_${camera}_image_bridge.log" 2>&1 &
  managed_pids+=("$!")
done
setsid python3 -u "$ROOT_DIR/scripts/product/capture_live_gazebo_screenshots.py" \
  --run-id "$RUN_ID" --output "$RUN_DIR/screenshots" --node-state "$NODE_STATE" \
  --phase-file "$PHASE_FILE" --stop-file "$MONITOR_STOP" \
  > "$RUN_DIR/logs/live_screenshot_capture.log" 2>&1 &
managed_pids+=("$!")

for endpoint in gcs uav1 uav2 uav3 uav4 uav5; do
  setsid ip netns exec ams-ns3 tcpdump -U -i "tap-$endpoint" -nn \
    -w "$RUN_DIR/pcap/tap_${endpoint}.pcap" \
    > "$RUN_DIR/logs/tap_${endpoint}_tcpdump.log" 2>&1 &
  capture_pids+=("$!")
done

NS3_FIFO="$RUNTIME_DIR/ns3-log.fifo"
mkfifo "$NS3_FIFO"
python3 -u "$ROOT_DIR/scripts/product/summarize_native_radio_product.py" timestamp \
  --output "$RUN_DIR/logs/ns3_sionna.log" < "$NS3_FIFO" &
NS3_LOGGER_PID=$!
setsid python3 -u "$ROOT_DIR/scripts/product/town01_runtime_monitor.py" \
  --output "$RUN_DIR/logs/runtime_resources.jsonl" --stop-file "$MONITOR_STOP" \
  > "$RUN_DIR/logs/runtime_monitor.log" 2>&1 &
managed_pids+=("$!")
setsid ip netns exec ams-ns3 env \
  LD_LIBRARY_PATH="$NS3_DIR/build/lib:${LD_LIBRARY_PATH:-}" \
  PATH="$NS3_DIR/build/src/tap-bridge:$PATH" \
  PYTHONPATH="$PYTHON_DEPS" MPLCONFIGDIR="$RUNTIME_DIR/matplotlib" \
  NS_LOG='SionnaRtChannelModel=level_debug|prefix_time' \
  taskset -c "$RADIO_CPUSET" stdbuf -oL -eL "$BINARY" \
  --uavCount=5 --tapGcs=tap-gcs --tapUavs=tap-uav1,tap-uav2,tap-uav3,tap-uav4,tap-uav5 \
  --scene="$SCENE" --positionFile="$NODE_STATE" --phaseFile="$PHASE_FILE" \
  --radioPcap="$RUN_DIR/pcap/native_radio.pcap" \
  --eventCsv="$RUN_DIR/logs/native_radio_events.csv" \
  --statsFile="$RUN_DIR/metrics/native_radio_stats.json" \
  --readyFile="$NS3_READY" --duration=2400 --txPowerW=0.01 \
  --channelStateMaxAgeS="$CHANNEL_STATE_MAX_AGE_S" \
  --updateDistanceThresholdM="$UPDATE_DISTANCE_THRESHOLD_M" \
  > "$NS3_FIFO" 2>&1 &
NS3_PID=$!
for _ in $(seq 1 1800); do
  [[ -s "$NS3_READY" ]] && break
  kill -0 "$NS3_PID" 2>/dev/null || { printf 'Native ns-3/Sionna stopped before readiness.\n' >&2; exit 1; }
  sleep 0.1
done
[[ -s "$NS3_READY" ]] || { printf 'Native ns-3/Sionna readiness timed out.\n' >&2; exit 1; }

ps -eo pid,ppid,pgid,etimes,cmd > "$RUN_DIR/logs/process_snapshot.txt"

set +e
ip netns exec ams-gcs python3 -u "$ROOT_DIR/scripts/product/native_radio_five_uav_scenario.py" run \
  --run-dir "$RUN_DIR" --node-state "$NODE_STATE" --phase-file "$PHASE_FILE" \
  --schedule-file "$SCHEDULE_FILE" --timeout-scale "$SCENARIO_TIMEOUT_SCALE" \
  > "$RUN_DIR/logs/flight_scenario.log" 2>&1
SCENARIO_STATUS=$?
set -e
if ((SCENARIO_STATUS != 0)); then
  printf 'Five-UAV scenario failed (rc=%s).\n' "$SCENARIO_STATUS" >&2
  exit "$SCENARIO_STATUS"
fi

printf 'no_bypass_stop\n' > "$PHASE_FILE"
kill -TERM "$NS3_PID"
for _ in $(seq 1 1200); do
  kill -0 "$NS3_PID" 2>/dev/null || break
  sleep 0.1
done
kill -0 "$NS3_PID" 2>/dev/null && { printf 'Native process did not stop cleanly.\n' >&2; exit 1; }
set +e
wait "$NS3_PID"
NS3_STATUS=$?
set -e
NS3_PID=""
[[ "$NS3_STATUS" == 0 || "$NS3_STATUS" == 143 ]] || {
  printf 'Native process stop status=%s\n' "$NS3_STATUS" >&2
  exit 1
}
wait "$NS3_LOGGER_PID" || true
NS3_LOGGER_PID=""
ip netns exec ams-gcs ss -H -tunap > "$RUN_DIR/logs/gcs_sockets_after_stop.txt" 2>&1 || true
ip netns exec ams-gcs python3 -u "$ROOT_DIR/scripts/product/native_radio_five_uav_scenario.py" no-bypass-probe \
  --run-dir "$RUN_DIR" --node-state "$NODE_STATE" --duration-s 10.5 \
  --output "$RUN_DIR/metrics/no_bypass_summary.json" \
  > "$RUN_DIR/logs/no_bypass_probe.log" 2>&1
ip netns exec ams-gcs ss -H -tunap > "$RUN_DIR/logs/gcs_sockets_after_10s.txt" 2>&1 || true

for pid in "${capture_pids[@]}"; do
  kill -INT -- "-$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
done
capture_pids=()
touch "$MONITOR_STOP"
sleep 2
summary_args=(--run-dir "$RUN_DIR")
if [[ -n "$ONE_UAV_RUN" ]]; then
  [[ -d "$ONE_UAV_RUN" ]] || { printf 'One-UAV regression run is absent: %s\n' "$ONE_UAV_RUN" >&2; exit 2; }
  summary_args+=(--one-uav-run "$ONE_UAV_RUN")
fi
python3 "$ROOT_DIR/scripts/product/summarize_native_radio_five_uav.py" "${summary_args[@]}" \
  > "$RUN_DIR/logs/summary.log" 2>&1
printf 'Native five-UAV run complete: %s\n' "$RUN_DIR"
