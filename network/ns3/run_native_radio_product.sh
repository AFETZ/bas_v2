#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE="${BAS_CONTAINER_IMAGE:-multiagent_simulation:latest}"
CONTAINER_NAME="${BAS_NATIVE_PRODUCT_CONTAINER_NAME:-bas-v2-native-radio-product}"

run_in_container() {
  command -v docker >/dev/null 2>&1 || {
    printf 'Docker is required for the native radio product runtime.\n' >&2
    return 2
  }
  local image_id
  image_id="$(docker image inspect --format '{{.Id}}' "$IMAGE" 2>/dev/null)" || {
    printf 'Runtime image is unavailable: %s\n' "$IMAGE" >&2
    return 2
  }
  if [[ "$(docker inspect --format '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null || true)" == true ]]; then
    printf 'Native product container is already running: %s\n' "$CONTAINER_NAME" >&2
    return 3
  fi
  python3 "$ROOT_DIR/scripts/product/prepare_town01_gazebo.py"
  local -a gpu_args=()
  if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
    gpu_args=(--gpus all -e NVIDIA_VISIBLE_DEVICES=all -e NVIDIA_DRIVER_CAPABILITIES=graphics,utility,compute)
  fi
  docker run --rm \
    --name "$CONTAINER_NAME" \
    --label bas.product=native-radio-product \
    --privileged \
    --network=host \
    --user 0:0 \
    "${gpu_args[@]}" \
    -e BAS_NATIVE_PRODUCT_IN_CONTAINER=1 \
    -e BAS_NATIVE_PRODUCT_RUN_ID="${BAS_NATIVE_PRODUCT_RUN_ID:-}" \
    -e BAS_NATIVE_PRODUCT_SKIP_BUILD="${BAS_NATIVE_PRODUCT_SKIP_BUILD:-0}" \
    -e BAS_NATIVE_PRODUCT_HOST_UID="$(id -u)" \
    -e BAS_NATIVE_PRODUCT_HOST_GID="$(id -g)" \
    -e HOME=/tmp/bas-native-product-home \
    -e XDG_RUNTIME_DIR=/tmp/bas-native-product-xdg \
    -e PYTHONPATH=/home/ubuntu/.local/lib/python3.10/site-packages \
    -v "$ROOT_DIR":/workspace/multiagent_simulation \
    -v "$ROOT_DIR":/home/bas/bas_v2 \
    -w /workspace/multiagent_simulation \
    "$image_id" bash -lc '
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
      exec ./network/ns3/run_native_radio_product.sh
    '
}

if [[ "${BAS_NATIVE_PRODUCT_IN_CONTAINER:-0}" != 1 ]]; then
  run_in_container
  exit $?
fi

if ((EUID != 0)); then
  printf 'Native radio product requires root in its privileged runtime container.\n' >&2
  exit 2
fi
for command in cmake c++ gz ip python3 ros2 socat ss stdbuf tcpdump; do
  command -v "$command" >/dev/null 2>&1 || {
    printf 'Required native product command is unavailable: %s\n' "$command" >&2
    exit 2
  }
done

RUN_ID="${BAS_NATIVE_PRODUCT_RUN_ID:-native-product-$(date -u +%Y%m%dT%H%M%SZ)}"
[[ "$RUN_ID" =~ ^[a-zA-Z0-9_.-]+$ ]] || { printf 'Unsafe run ID: %s\n' "$RUN_ID" >&2; exit 2; }
RUN_DIR="$ROOT_DIR/runs/native-radio-product/$RUN_ID"
[[ ! -e "$RUN_DIR" ]] || { printf 'Run directory already exists: %s\n' "$RUN_DIR" >&2; exit 2; }
RUNTIME_DIR="/tmp/bas-native-product-$RUN_ID"
UART_DIR="$RUNTIME_DIR/uart"
WORK_DIR="$RUNTIME_DIR/work"
NS3_DIR="$ROOT_DIR/.external/ns-3-sionna-native"
PYTHON_DEPS="$NS3_DIR/.python-deps-py310"
PYTHON_TOOLING="$NS3_DIR/.tooling-py310"
PROJECT_SOURCE="$ROOT_DIR/network/ns3/scratch/upstream-sionna-tap-spike.cc"
UPSTREAM_SOURCE="$NS3_DIR/scratch/upstream-sionna-tap-spike.cc"
BINARY="$NS3_DIR/build/scratch/ns3.48-upstream-sionna-tap-spike-default"
PATCH_FILE="$ROOT_DIR/network/ns3/patches/mr2608-spike-compatibility.patch"
SCENARIO="$ROOT_DIR/network/config/scenario_1uav_town01_native_product.yaml"
WORLD="$ROOT_DIR/.external/cavise_maps/Town01/gazebo/town01.sdf"
SCENE="$ROOT_DIR/.external/cavise_maps/Town01/map/scene.xml"
NODE_STATE="$RUN_DIR/logs/node_state.json"
NODE_EVENTS="$RUN_DIR/logs/node_state.jsonl"
PHASE_FILE="$RUN_DIR/logs/current_phase.txt"
NS3_READY="$RUN_DIR/logs/ns3.ready"
FAIL_CLOSED_READY="$RUN_DIR/logs/fail_closed.ready.json"
RADIO_STOPPED="$RUN_DIR/logs/radio_stopped"
MONITOR_STOP="$RUN_DIR/logs/runtime_monitor.stop"
ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-$((100 + $(printf '%s' "$RUN_ID" | cksum | awk '{print $1}') % 100))}"
GZ_PARTITION="${GZ_PARTITION:-native_${RUN_ID//[^a-zA-Z0-9_]/_}}"

for required in "$PROJECT_SOURCE" "$PATCH_FILE" "$SCENARIO" "$WORLD" "$SCENE"; do
  [[ -f "$required" ]] || { printf 'Required native product input is missing: %s\n' "$required" >&2; exit 2; }
done
[[ "$(git -c safe.directory="$NS3_DIR" -C "$NS3_DIR" rev-parse HEAD)" == d2add90b452d600cfb4859baed8e9ea633519447 ]] || {
  printf 'Official ns-3.48 exact checkout is missing at %s\n' "$NS3_DIR" >&2
  exit 2
}
git -c safe.directory="$NS3_DIR" -C "$NS3_DIR" apply --reverse --check "$PATCH_FILE" || {
  printf 'Minimal ns-3.48 compatibility patch is not exactly applied.\n' >&2
  exit 2
}

mkdir -p "$RUN_DIR"/{logs,metrics,pcap} "$UART_DIR" "$WORK_DIR"
printf '%q ' "$0" "$@" > "$RUN_DIR/command.txt"
printf '\n' >> "$RUN_DIR/command.txt"
printf 'preflight\n' > "$PHASE_FILE"

if ! PYTHONPATH="$PYTHON_DEPS" python3 - <<'PY' >/dev/null 2>&1
from importlib.metadata import version
assert version("sionna") == "1.2.0"
assert version("sionna-rt") == "1.2.0"
assert version("pybind11") == "2.11.1"
assert version("cppyy") == "3.5.0"
PY
then
  rm -rf "$PYTHON_DEPS"
  python3 -m pip install --target "$PYTHON_DEPS" \
    'sionna==1.2.0' 'sionna-rt==1.2.0' 'pybind11==2.11.1' 'cppyy==3.5.0' \
    > "$RUN_DIR/logs/python_dependencies_install.log" 2>&1
fi
if [[ ! -x "$PYTHON_TOOLING/bin/cmake" ]]; then
  python3 -m pip install --target "$PYTHON_TOOLING" 'cmake==3.31.6' \
    > "$RUN_DIR/logs/cmake_tool_install.log" 2>&1
fi
export PATH="$PYTHON_TOOLING/bin:$PATH"
export PYTHONPATH="$PYTHON_TOOLING:$PYTHON_DEPS:${PYTHONPATH:-}"
cmake_version_number="$(cmake --version | head -n1 | awk '{print $3}')"
python3 - "$cmake_version_number" <<'PY'
import sys
parts = tuple(int(value) for value in sys.argv[1].split(".")[:2])
raise SystemExit(0 if parts >= (3, 25) else 1)
PY

cp "$PROJECT_SOURCE" "$UPSTREAM_SOURCE"
if [[ "${BAS_NATIVE_PRODUCT_SKIP_BUILD:-0}" == 1 ]]; then
  [[ -x "$BINARY" ]] || { printf 'Requested build reuse but binary is absent.\n' >&2; exit 2; }
  printf 'Reused the exact container-built binary; project C++ and upstream scratch copy match.\n' \
    > "$RUN_DIR/logs/ns3_build.log"
else
  (
    cd "$NS3_DIR"
    # The checkout is mounted at two absolute paths (host and container).  An
    # existing host CMake cache therefore cannot be safely cleaned by ns3's path
    # guard.  Remove only these two resolved build directories after proving they
    # are direct children of the exact official checkout.
    for build_directory in "$NS3_DIR/build" "$NS3_DIR/cmake-cache"; do
      [[ "$(dirname "$build_directory")" == "$NS3_DIR" ]] || exit 2
      rm -rf "$build_directory"
    done
    PYTHONPATH="$PYTHON_TOOLING:$PYTHON_DEPS" ./ns3 configure --enable-examples --enable-tests --enable-python-bindings
    PYTHONPATH="$PYTHON_TOOLING:$PYTHON_DEPS" ./ns3 build upstream-sionna-tap-spike
  ) > "$RUN_DIR/logs/ns3_build.log" 2>&1
fi
[[ -x "$BINARY" ]] || { printf 'Native ns-3.48 product binary was not built.\n' >&2; exit 1; }

mapfile -t DEPENDENCY_VERSIONS < <(PYTHONPATH="$PYTHON_DEPS" python3 - <<'PY'
from importlib.metadata import version
for name in ("sionna", "sionna-rt", "mitsuba", "drjit", "pybind11", "cppyy"):
    print(f"{name}={version(name)}")
PY
)
declare -A VERSION_BY_NAME=()
for record in "${DEPENDENCY_VERSIONS[@]}"; do VERSION_BY_NAME["${record%%=*}"]="${record#*=}"; done
{
  printf 'run_id=%s\n' "$RUN_ID"
  printf 'started_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'git_head=%s\n' "$(git -C "$ROOT_DIR" rev-parse HEAD)"
  printf 'ns3_version=3.48\n'
  printf 'ns3_exact_sha=%s\n' "$(git -c safe.directory="$NS3_DIR" -C "$NS3_DIR" rev-parse HEAD)"
  printf 'ns3_compatibility_patch=true\n'
  printf 'python_version=%s\n' "$(python3 --version | awk '{print $2}')"
  printf 'sionna_version=%s\n' "${VERSION_BY_NAME[sionna]}"
  printf 'sionna_rt_version=%s\n' "${VERSION_BY_NAME[sionna-rt]}"
  printf 'mitsuba_version=%s\n' "${VERSION_BY_NAME[mitsuba]}"
  printf 'drjit_version=%s\n' "${VERSION_BY_NAME[drjit]}"
  printf 'pybind11_version=%s\n' "${VERSION_BY_NAME[pybind11]}"
  printf 'cppyy_version=%s\n' "${VERSION_BY_NAME[cppyy]}"
  printf 'compiler_version=%s\n' "$(c++ --version | head -n1)"
  printf 'cmake_version=%s\n' "$(cmake --version | head -n1 | awk '{print $3}')"
  printf 'ros_domain_id=%s\n' "$ROS_DOMAIN_ID"
  printf 'gz_partition=%s\n' "$GZ_PARTITION"
  printf 'scenario=%s\n' "$SCENARIO"
  printf 'world=%s\n' "$WORLD"
  printf 'scene=%s\n' "$SCENE"
  printf 'profile=generic_native_spectrum_aloha_reference\n'
  printf 'solver_profile=realtime_minimal_solver_profile\n'
} > "$RUN_DIR/environment.txt"

managed_pids=()
NS3_PID=""
NS3_LOGGER_PID=""
SCENARIO_PID=""
CAPTURE_GCS_PID=""
CAPTURE_UAV_PID=""
CLEANUP_ACTIVE=0

stop_group() {
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
  trap - EXIT INT TERM
  [[ -n "$NS3_PID" ]] && stop_group "$NS3_PID"
  [[ -n "$CAPTURE_GCS_PID" ]] && kill -INT -- "-$CAPTURE_GCS_PID" 2>/dev/null || true
  [[ -n "$CAPTURE_UAV_PID" ]] && kill -INT -- "-$CAPTURE_UAV_PID" 2>/dev/null || true
  touch "$MONITOR_STOP" 2>/dev/null || true
  local pid
  for ((pid_index=${#managed_pids[@]}-1; pid_index>=0; pid_index--)); do
    pid="${managed_pids[$pid_index]}"
    stop_group "$pid"
  done
  "$ROOT_DIR/network/scripts/setup_one_uav_netns.sh" down >/dev/null 2>&1 || true
  if [[ -d "$RUN_DIR" ]]; then
    chown -R "${BAS_NATIVE_PRODUCT_HOST_UID:-0}:${BAS_NATIVE_PRODUCT_HOST_GID:-0}" "$RUN_DIR" 2>/dev/null || true
  fi
  rm -rf "$RUNTIME_DIR"
  exit "$status"
}
trap cleanup EXIT INT TERM

"$ROOT_DIR/network/scripts/setup_one_uav_netns.sh" down >/dev/null 2>&1 || true
"$ROOT_DIR/network/scripts/setup_one_uav_netns.sh" up > "$RUN_DIR/logs/netns_setup.log" 2>&1
"$ROOT_DIR/network/scripts/setup_one_uav_netns.sh" status > "$RUN_DIR/logs/netns_status.log" 2>&1
# Endpoint and router MACs are fixed by the existing namespace contract.  Keep
# ARP control replies out of the ideal PHY's receive callback; this does not
# bypass the IP packet path and all UDP/MAVLink frames still traverse TapBridge.
ip -n ams-gcs neigh replace 10.71.0.1 lladdr 02:71:00:00:00:01 nud permanent dev eth0
ip -n ams-uav1 neigh replace 10.71.1.1 lladdr 02:71:ff:00:00:01 nud permanent dev eth0
{
  ip -n ams-gcs neigh show dev eth0
  ip -n ams-uav1 neigh show dev eth0
} > "$RUN_DIR/logs/static_neighbors.txt"

control_sitl="$UART_DIR/control-sitl"
control_adapter="$UART_DIR/control-adapter"
payload_sitl="$UART_DIR/payload-sitl"
payload_adapter="$UART_DIR/payload-adapter"
setsid socat -d -d "pty,raw,echo=0,link=$control_sitl,mode=660" \
  "pty,raw,echo=0,link=$control_adapter,mode=660" > "$RUN_DIR/logs/control_socat.log" 2>&1 &
managed_pids+=("$!")
setsid socat -d -d "pty,raw,echo=0,link=$payload_sitl,mode=660" \
  "pty,raw,echo=0,link=$payload_adapter,mode=660" > "$RUN_DIR/logs/payload_socat.log" 2>&1 &
managed_pids+=("$!")
for _ in $(seq 1 100); do
  [[ -e "$control_sitl" && -e "$control_adapter" && -e "$payload_sitl" && -e "$payload_adapter" ]] && break
  sleep 0.1
done
[[ -e "$control_sitl" && -e "$payload_sitl" ]] || { printf 'UART PTY creation timed out.\n' >&2; exit 1; }

export ROS_DOMAIN_ID GZ_PARTITION
cd "$WORK_DIR"
setsid ros2 launch multiagent_simulation multiagent_simulation.launch.py \
  robots_config_file:="$SCENARIO" \
  world_file:="$WORLD" \
  robot_model:=iris_radio_headless \
  gui:=false rviz:=false headless_rendering:=true generate_sensor_models:=false \
  use_mapping_camera:=false use_navigation_camera:=false use_zed_camera:=false \
  start_mavproxy:=false \
  sitl_extra_defaults:="$ROOT_DIR/network/config/town01_sitl.parm" \
  control_uart:="$control_sitl" payload_uart:="$payload_sitl" \
  > "$RUN_DIR/logs/gazebo_sitl.log" 2>&1 &
managed_pids+=("$!")
cd "$ROOT_DIR"
setsid python3 "$ROOT_DIR/network/position_tracker/tracker.py" \
  --scenario "$SCENARIO" \
  --jammers-config "$ROOT_DIR/network/config/jammers_rock_demo.yaml" \
  --output-json "$NODE_STATE" --output-jsonl "$NODE_EVENTS" \
  --rate-hz 10 --stale-after-s 1.0 \
  > "$RUN_DIR/logs/position_tracker.log" 2>&1 &
managed_pids+=("$!")
setsid stdbuf -oL gz topic -e -t /world/map/stats > "$RUN_DIR/logs/gazebo_stats.log" 2>&1 &
managed_pids+=("$!")

fresh=0
for _ in $(seq 1 1800); do
  if python3 - "$NODE_STATE" <<'PY' >/dev/null 2>&1
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
nodes = {node["id"]: node for node in value.get("nodes", [])}
assert value.get("source") == "ros_odometry"
assert not value.get("missing_nodes") and not value.get("stale_nodes")
assert not nodes["uav1"].get("stale")
PY
  then fresh=1; break; fi
  sleep 0.1
done
((fresh == 1)) || { printf 'Real /uav1/odometry did not become fresh.\n' >&2; exit 1; }
timeout 15 ros2 topic info --verbose /uav1/odometry > "$RUN_DIR/logs/odometry_topic_info.txt" 2>&1 || true

setsid ip netns exec ams-ns3 tcpdump -U -i tap-gcs -nn -w "$RUN_DIR/pcap/tap_gcs.pcap" \
  > "$RUN_DIR/logs/tap_gcs_tcpdump.log" 2>&1 &
CAPTURE_GCS_PID=$!
setsid ip netns exec ams-ns3 tcpdump -U -i tap-uav -nn -w "$RUN_DIR/pcap/tap_uav.pcap" \
  > "$RUN_DIR/logs/tap_uav_tcpdump.log" 2>&1 &
CAPTURE_UAV_PID=$!

NS3_FIFO="$RUNTIME_DIR/ns3-log.fifo"
mkfifo "$NS3_FIFO"
python3 -u "$ROOT_DIR/scripts/product/summarize_native_radio_product.py" timestamp \
  --output "$RUN_DIR/logs/ns3_sionna.log" < "$NS3_FIFO" &
NS3_LOGGER_PID=$!
setsid ip netns exec ams-ns3 env \
  LD_LIBRARY_PATH="$NS3_DIR/build/lib:${LD_LIBRARY_PATH:-}" \
  PATH="$NS3_DIR/build/src/tap-bridge:${PATH}" \
  PYTHONPATH="$PYTHON_DEPS" \
  MPLCONFIGDIR="$RUNTIME_DIR/matplotlib" \
  NS_LOG='SionnaRtChannelModel=level_debug|prefix_time:HalfDuplexIdealPhy=level_logic|prefix_time' \
  stdbuf -oL -eL "$BINARY" \
  --uavCount=1 --tapGcs=tap-gcs --tapUavs=tap-uav \
  --scene="$SCENE" --positionFile="$NODE_STATE" --phaseFile="$PHASE_FILE" \
  --radioPcap="$RUN_DIR/pcap/native_radio.pcap" \
  --eventCsv="$RUN_DIR/logs/native_radio_events.csv" \
  --statsFile="$RUN_DIR/metrics/native_radio_stats.json" \
  --readyFile="$NS3_READY" --duration=1200 --txPowerW=0.01 \
  > "$NS3_FIFO" 2>&1 &
NS3_PID=$!
for _ in $(seq 1 1200); do
  [[ -f "$NS3_READY" ]] && break
  kill -0 "$NS3_PID" 2>/dev/null || { printf 'Native ns-3/Sionna exited before readiness.\n' >&2; exit 1; }
  sleep 0.1
done
[[ -f "$NS3_READY" ]] || { printf 'Native ns-3/Sionna readiness timed out.\n' >&2; exit 1; }

setsid ip netns exec ams-gcs python3 -u "$ROOT_DIR/scripts/product/native_radio_product_scenario.py" run \
  --run-dir "$RUN_DIR" --node-state "$NODE_STATE" --phase-file "$PHASE_FILE" \
  --fail-closed-ready "$FAIL_CLOSED_READY" --radio-stopped-file "$RADIO_STOPPED" \
  > "$RUN_DIR/logs/product_scenario.log" 2>&1 &
SCENARIO_PID=$!

setsid ip netns exec ams-uav1 python3 -u "$ROOT_DIR/network/scripts/communication_vertical.py" uart-adapter \
  --channel control --tty "$control_adapter" --bind 10.71.1.10:14601 --peer 10.71.0.10:14600 \
  --event-log "$RUN_DIR/logs/control_uart.jsonl" --ready-file "$RUN_DIR/logs/control_uart.ready" \
  --metrics-output "$RUN_DIR/metrics/control_uart.json" --framed \
  > "$RUN_DIR/logs/control_uart.log" 2>&1 &
managed_pids+=("$!")
setsid ip netns exec ams-uav1 python3 -u "$ROOT_DIR/network/scripts/communication_vertical.py" uart-adapter \
  --channel payload --tty "$payload_adapter" --bind 10.71.1.10:14701 --peer 10.71.0.10:14700 \
  --event-log "$RUN_DIR/logs/payload_uart.jsonl" --ready-file "$RUN_DIR/logs/payload_uart.ready" \
  --metrics-output "$RUN_DIR/metrics/payload_uart.json" --framed \
  > "$RUN_DIR/logs/payload_uart.log" 2>&1 &
managed_pids+=("$!")
setsid ip netns exec ams-uav1 python3 -u "$ROOT_DIR/scripts/product/native_radio_product_scenario.py" additional-agent \
  --event-log "$RUN_DIR/logs/additional_uart_endpoint.jsonl" \
  --ready-file "$RUN_DIR/logs/additional_uart_endpoint.ready" \
  > "$RUN_DIR/logs/additional_uart_endpoint.log" 2>&1 &
managed_pids+=("$!")

setsid python3 -u "$ROOT_DIR/scripts/product/town01_runtime_monitor.py" \
  --output "$RUN_DIR/logs/runtime_resources.jsonl" --stop-file "$MONITOR_STOP" \
  > "$RUN_DIR/logs/runtime_monitor.log" 2>&1 &
managed_pids+=("$!")

for ready in "$RUN_DIR/logs/control_uart.ready" "$RUN_DIR/logs/payload_uart.ready" "$RUN_DIR/logs/additional_uart_endpoint.ready"; do
  for _ in $(seq 1 100); do [[ -f "$ready" ]] && break; sleep 0.1; done
  [[ -f "$ready" ]] || { printf 'Endpoint readiness timed out: %s\n' "$ready" >&2; exit 1; }
done
ps -eo pid,ppid,pgid,etimes,cmd > "$RUN_DIR/logs/process_snapshot.txt"

for _ in $(seq 1 9000); do
  [[ -f "$FAIL_CLOSED_READY" ]] && break
  kill -0 "$SCENARIO_PID" 2>/dev/null || {
    wait "$SCENARIO_PID" || true
    printf 'Product scenario exited before fail-closed gate.\n' >&2
    exit 1
  }
  kill -0 "$NS3_PID" 2>/dev/null || { printf 'Native ns-3/Sionna exited during flight.\n' >&2; exit 1; }
  sleep 0.1
done
[[ -f "$FAIL_CLOSED_READY" ]] || { printf 'Product flight did not reach fail-closed gate.\n' >&2; exit 1; }

kill -TERM "$NS3_PID"
set +e
wait "$NS3_PID"
NS3_RC=$?
set -e
NS3_PID=""
[[ "$NS3_RC" == 0 || "$NS3_RC" == 143 ]] || { printf 'Native ns-3/Sionna stop rc=%s\n' "$NS3_RC" >&2; exit 1; }
wait "$NS3_LOGGER_PID" || true
NS3_LOGGER_PID=""
touch "$RADIO_STOPPED"
ip netns exec ams-gcs ss -H -tunap > "$RUN_DIR/logs/gcs_sockets_after_stop.txt" 2>&1 || true

set +e
wait "$SCENARIO_PID"
SCENARIO_RC=$?
set -e
SCENARIO_PID=""
ip netns exec ams-gcs ss -H -tunap > "$RUN_DIR/logs/gcs_sockets_after_10s.txt" 2>&1 || true
((SCENARIO_RC == 0)) || { printf 'Product scenario failed rc=%s.\n' "$SCENARIO_RC" >&2; exit 1; }

kill -INT -- "-$CAPTURE_GCS_PID" 2>/dev/null || true
kill -INT -- "-$CAPTURE_UAV_PID" 2>/dev/null || true
wait "$CAPTURE_GCS_PID" 2>/dev/null || true
wait "$CAPTURE_UAV_PID" 2>/dev/null || true
CAPTURE_GCS_PID=""
CAPTURE_UAV_PID=""
touch "$MONITOR_STOP"
sleep 1

python3 "$ROOT_DIR/scripts/product/summarize_native_radio_product.py" summarize --run-dir "$RUN_DIR" \
  > "$RUN_DIR/logs/summary.log" 2>&1
printf 'Native radio product run complete: %s\n' "$RUN_DIR"
