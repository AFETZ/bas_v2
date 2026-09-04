#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE="${BAS_CONTAINER_IMAGE:-multiagent_simulation:latest}"
CONTAINER_NAME="${BAS_NATIVE_FIVE_CONTAINER_NAME:-bas-v2-native-radio-five-uav}"
SCENARIO_KEY="${BAS_NATIVE_FIVE_SCENARIO:-town01}"
GUI="${BAS_NATIVE_FIVE_GUI:-0}"

[[ "$SCENARIO_KEY" == town01 || "$SCENARIO_KEY" == rock_demo ]] || {
  printf 'Scenario must be town01 or rock_demo: %s\n' "$SCENARIO_KEY" >&2
  exit 2
}
[[ "$GUI" == 0 || "$GUI" == 1 ]] || { printf 'BAS_NATIVE_FIVE_GUI must be 0 or 1.\n' >&2; exit 2; }

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
  if [[ "$SCENARIO_KEY" == town01 ]]; then
    python3 "$ROOT_DIR/scripts/product/prepare_town01_gazebo.py"
  fi
  local -a gpu_args=()
  if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
    gpu_args=(--gpus all -e NVIDIA_VISIBLE_DEVICES=all -e NVIDIA_DRIVER_CAPABILITIES=graphics,utility,compute)
  fi
  local -a gui_args=()
  if [[ "$GUI" == 1 ]]; then
    [[ -n "${DISPLAY:-}" ]] || { printf 'DISPLAY is required for the Gazebo GUI.\n' >&2; return 2; }
    [[ -d /tmp/.X11-unix ]] || { printf 'X11 socket directory is unavailable.\n' >&2; return 2; }
    gui_args=(-e DISPLAY="$DISPLAY" -e QT_X11_NO_MITSHM=1 -v /tmp/.X11-unix:/tmp/.X11-unix:rw)
    local xauthority_file="${XAUTHORITY:-${HOME}/.Xauthority}"
    if [[ -f "$xauthority_file" ]]; then
      gui_args+=(-e XAUTHORITY=/tmp/bas-native-xauthority -v "$xauthority_file:/tmp/bas-native-xauthority:ro")
    fi
  fi
  docker run --rm \
    --name "$CONTAINER_NAME" \
    --label bas.product=native-radio-five-uav \
    --privileged --network=host --user 0:0 \
    "${gpu_args[@]}" \
    "${gui_args[@]}" \
    -e BAS_NATIVE_FIVE_IN_CONTAINER=1 \
    -e BAS_SOURCE_HEAD="$(git -C "$ROOT_DIR" rev-parse HEAD)" \
    -e BAS_SOURCE_DIRTY="$(git -C "$ROOT_DIR" status --porcelain | wc -l)" \
    -e BAS_NATIVE_SOURCES="${BAS_NATIVE_SOURCES:-}" \
    -e BAS_NATIVE_EXTERNAL_CONFIG="${BAS_NATIVE_EXTERNAL_CONFIG:-}" \
    -e BAS_NATIVE_FIVE_RUN_ID="${BAS_NATIVE_FIVE_RUN_ID:-}" \
    -e BAS_NATIVE_FIVE_SCENARIO="$SCENARIO_KEY" \
    -e BAS_NATIVE_FIVE_GUI="$GUI" \
    -e BAS_NATIVE_FIVE_SKIP_BUILD="${BAS_NATIVE_FIVE_SKIP_BUILD:-0}" \
    -e BAS_NATIVE_FIVE_ONE_UAV_RUN="${BAS_NATIVE_FIVE_ONE_UAV_RUN:-}" \
    -e BAS_NATIVE_FIVE_GAZEBO_RTF="${BAS_NATIVE_FIVE_GAZEBO_RTF:-1.0}" \
    -e BAS_NATIVE_CHANNEL_STATE_MAX_AGE_S="${BAS_NATIVE_CHANNEL_STATE_MAX_AGE_S:-}" \
    -e BAS_NATIVE_UPDATE_DISTANCE_THRESHOLD_M="${BAS_NATIVE_UPDATE_DISTANCE_THRESHOLD_M:-}" \
    -e BAS_NATIVE_FIVE_TIMEOUT_SCALE="${BAS_NATIVE_FIVE_TIMEOUT_SCALE:-5.0}" \
    -e BAS_NATIVE_LATENCY_MODE="${BAS_NATIVE_LATENCY_MODE:-0}" \
    -e BAS_NATIVE_UAV_COUNT="${BAS_NATIVE_UAV_COUNT:-5}" \
    -e BAS_NATIVE_UART_CHANNELS="${BAS_NATIVE_UART_CHANNELS:-control,payload}" \
    -e BAS_NATIVE_RADIO_BACKEND="${BAS_NATIVE_RADIO_BACKEND:-}" \
    -e BAS_NATIVE_WIFI_DATA_MODE="${BAS_NATIVE_WIFI_DATA_MODE:-}" \
    -e BAS_NATIVE_WIFI_CHANNEL_NUMBER="${BAS_NATIVE_WIFI_CHANNEL_NUMBER:-}" \
    -e BAS_NATIVE_WIFI_CHANNEL_WIDTH_MHZ="${BAS_NATIVE_WIFI_CHANNEL_WIDTH_MHZ:-}" \
    -e BAS_NATIVE_PHY_RATE_BPS="${BAS_NATIVE_PHY_RATE_BPS:-}" \
    -e BAS_NATIVE_EVENT_LOGGING="${BAS_NATIVE_EVENT_LOGGING:-batched_trace}" \
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
      export GZ_SIM_RESOURCE_PATH="${GZ_SIM_RESOURCE_PATH:-}:$PWD/src/multiagent_simulation/models:$PWD/src/multiagent_simulation/worlds:$PWD/src"
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
LATENCY_MODE="${BAS_NATIVE_LATENCY_MODE:-0}"
UAV_COUNT="${BAS_NATIVE_UAV_COUNT:-5}"
ACTIVE_UART_CHANNELS="${BAS_NATIVE_UART_CHANNELS:-control,payload}"
EVENT_LOGGING="${BAS_NATIVE_EVENT_LOGGING:-batched_trace}"
[[ "$UAV_COUNT" == 1 || "$UAV_COUNT" == 5 ]] || { printf 'UAV count must be 1 or 5: %s\n' "$UAV_COUNT" >&2; exit 2; }
[[ ",$ACTIVE_UART_CHANNELS," == *,control,* ]] || { printf 'Control UART must remain active.\n' >&2; exit 2; }
[[ "$EVENT_LOGGING" == metrics_only || "$EVENT_LOGGING" == batched_trace ]] || { printf 'Invalid event logging mode.\n' >&2; exit 2; }
[[ "$SCENARIO_KEY" == town01 || "$UAV_COUNT" == 5 ]] || {
  printf 'The rock_demo product scenario requires all five UAVs.\n' >&2
  exit 2
}
SCENARIO_MODE="product"
[[ "$LATENCY_MODE" == 1 ]] && SCENARIO_MODE="latency_diagnostic"
[[ "$SCENARIO_MODE" == latency_diagnostic || "$UAV_COUNT" == 5 ]] || {
  printf 'The product flight proof requires five UAVs; one UAV is diagnostic-only.\n' >&2
  exit 2
}
UAV_INDICES=()
for ((index=1; index<=UAV_COUNT; ++index)); do UAV_INDICES+=("$index"); done
TAP_UAVS=""
TAP_ENDPOINTS=(gcs)
for index in "${UAV_INDICES[@]}"; do
  TAP_UAVS+="${TAP_UAVS:+,}tap-uav$index"
  TAP_ENDPOINTS+=("uav$index")
done
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
PHASED_ARRAY_ADAPTER_PATCH="$ROOT_DIR/network/ns3/patches/mr2608-spectrumwifi-phased-array-adapter.patch"
if [[ "$SCENARIO_KEY" == rock_demo ]]; then
  SCENARIO="$ROOT_DIR/network/config/scenario_5uav_rock_demo_native_product.yaml"
else
  SCENARIO="$ROOT_DIR/network/config/scenario_${UAV_COUNT}uav_town01_native_product.yaml"
fi
GAZEBO_RTF="${BAS_NATIVE_FIVE_GAZEBO_RTF:-1.0}"
SCENARIO_TIMEOUT_SCALE="${BAS_NATIVE_FIVE_TIMEOUT_SCALE:-5.0}"
LAUNCH_WORLD="$WORK_DIR/${SCENARIO_KEY}-native-live-cameras.sdf"
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

[[ -f "$SCENARIO" ]] || { printf 'Missing scenario config: %s\n' "$SCENARIO" >&2; exit 2; }
mapfile -t SCENARIO_VALUES < <(python3 - "$ROOT_DIR" "$SCENARIO" <<'PY'
from pathlib import Path
import sys
import yaml

root = Path(sys.argv[1]).resolve()
config = yaml.safe_load(Path(sys.argv[2]).read_text(encoding="utf-8"))
scenario = config["scenario"]
scene_map = scenario["map"]
base = config["base_simulation"]
radio = config["radio"]
for value in (
    scene_map["world_file"],
    scene_map["scene_xml"],
    scene_map["camera_fragment"],
    radio["config"],
    base["sitl_defaults"],
):
    path = Path(str(value))
    print(str((root / path).resolve()) if not path.is_absolute() else str(path))
print(scene_map["id"])
PY
)
(( ${#SCENARIO_VALUES[@]} == 6 )) || {
  printf 'Invalid scenario config: expected six resolved product values.\n' >&2
  exit 2
}
WORLD="${SCENARIO_VALUES[0]}"
SCENE="${SCENARIO_VALUES[1]}"
CAMERA_FRAGMENT="${SCENARIO_VALUES[2]}"
RADIO_CONFIG="${SCENARIO_VALUES[3]}"
SITL_DEFAULTS="${SCENARIO_VALUES[4]}"
MAP_ID="${SCENARIO_VALUES[5]}"

for required_file in "$PROJECT_SOURCE" "$PATCH_FILE" "$REALTIME_CACHE_PATCH" \
  "$PHASED_ARRAY_ADAPTER_PATCH" "$RADIO_CONFIG" "$WORLD" "$SCENE" "$CAMERA_FRAGMENT" "$SITL_DEFAULTS"; do
  [[ -f "$required_file" ]] || { printf 'Missing input: %s\n' "$required_file" >&2; exit 2; }
done
export GZ_SIM_RESOURCE_PATH="${GZ_SIM_RESOURCE_PATH:-}:$(dirname "$WORLD")"
mapfile -t RADIO_VALUES < <(python3 - "$RADIO_CONFIG" <<'PY'
import sys
import yaml

value = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
radio = value["radio"]
sionna = value["sionna"]
for item in (
    radio["backend"],
    radio["profile"],
    str(radio["technology_specific_modem"]).lower(),
    radio["data_mode"],
    radio["control_mode"],
    str(radio["channel_number"]),
    str(radio["channel_width_mhz"]),
    str(radio["carrier_hz"]),
    str(radio["tx_power_w"]),
    str(radio["legacy_aloha_phy_rate_bps"]),
    radio["ssid"],
    radio["neighbor_discovery_mode"],
    radio["reason"],
    sionna["solver_profile"],
    str(sionna["max_depth"]),
    str(sionna["los"]).lower(),
    str(sionna["specular_reflection"]).lower(),
    str(sionna["diffuse_reflection"]).lower(),
    str(sionna["diffraction"]).lower(),
    str(sionna["edge_diffraction"]).lower(),
    str(sionna["refraction"]).lower(),
    str(sionna["synthetic_array"]).lower(),
    str(sionna["seed"]),
    str(sionna["max_number_of_paths"]),
    str(sionna["cache_expiry_jitter_fraction"]),
    str(sionna["channel_state_max_age_s"]),
    str(sionna["endpoint_displacement_threshold_m"]),
    str(sionna["readiness_lag_max_ms"]),
    str(sionna["readiness_consecutive_samples"]),
):
    print(item)
PY
)
(( ${#RADIO_VALUES[@]} == 29 )) || {
  printf 'Invalid native product radio config: expected 29 resolved values, got %s\n' "${#RADIO_VALUES[@]}" >&2
  exit 2
}
RADIO_BACKEND="${RADIO_VALUES[0]}"
RADIO_PROFILE="${RADIO_VALUES[1]}"
TECHNOLOGY_SPECIFIC_MODEM="${RADIO_VALUES[2]}"
WIFI_DATA_MODE="${RADIO_VALUES[3]}"
WIFI_CONTROL_MODE="${RADIO_VALUES[4]}"
WIFI_CHANNEL_NUMBER="${RADIO_VALUES[5]}"
WIFI_CHANNEL_WIDTH_MHZ="${RADIO_VALUES[6]}"
CARRIER_HZ="${RADIO_VALUES[7]}"
TX_POWER_W="${RADIO_VALUES[8]}"
PHY_RATE_BPS="${RADIO_VALUES[9]}"
WIFI_SSID="${RADIO_VALUES[10]}"
NEIGHBOR_DISCOVERY_MODE="${RADIO_VALUES[11]}"
RADIO_REASON="${RADIO_VALUES[12]}"
SOLVER_PROFILE="${RADIO_VALUES[13]}"
SIONNA_MAX_DEPTH="${RADIO_VALUES[14]}"
SIONNA_LOS="${RADIO_VALUES[15]}"
SIONNA_SPECULAR_REFLECTION="${RADIO_VALUES[16]}"
SIONNA_DIFFUSE_REFLECTION="${RADIO_VALUES[17]}"
SIONNA_DIFFRACTION="${RADIO_VALUES[18]}"
SIONNA_EDGE_DIFFRACTION="${RADIO_VALUES[19]}"
SIONNA_REFRACTION="${RADIO_VALUES[20]}"
SIONNA_SYNTHETIC_ARRAY="${RADIO_VALUES[21]}"
SIONNA_SEED="${RADIO_VALUES[22]}"
SIONNA_MAX_NUMBER_OF_PATHS="${RADIO_VALUES[23]}"
SIONNA_CACHE_JITTER_FRACTION="${RADIO_VALUES[24]}"
CHANNEL_STATE_MAX_AGE_S="${RADIO_VALUES[25]}"
UPDATE_DISTANCE_THRESHOLD_M="${RADIO_VALUES[26]}"
READINESS_LAG_MAX_MS="${RADIO_VALUES[27]}"
READINESS_CONSECUTIVE_SAMPLES="${RADIO_VALUES[28]}"
reject_product_override() {
  local name="$1" requested="$2" configured="$3"
  [[ -z "$requested" || "$requested" == "$configured" ]] || {
    printf '%s=%s conflicts with selected product config value %s.\n' \
      "$name" "$requested" "$configured" >&2
    exit 2
  }
}
reject_product_override BAS_NATIVE_RADIO_BACKEND "${BAS_NATIVE_RADIO_BACKEND:-}" "$RADIO_BACKEND"
reject_product_override BAS_NATIVE_WIFI_DATA_MODE "${BAS_NATIVE_WIFI_DATA_MODE:-}" "$WIFI_DATA_MODE"
reject_product_override BAS_NATIVE_WIFI_CHANNEL_NUMBER "${BAS_NATIVE_WIFI_CHANNEL_NUMBER:-}" "$WIFI_CHANNEL_NUMBER"
reject_product_override BAS_NATIVE_WIFI_CHANNEL_WIDTH_MHZ "${BAS_NATIVE_WIFI_CHANNEL_WIDTH_MHZ:-}" "$WIFI_CHANNEL_WIDTH_MHZ"
reject_product_override BAS_NATIVE_PHY_RATE_BPS "${BAS_NATIVE_PHY_RATE_BPS:-}" "$PHY_RATE_BPS"
reject_product_override BAS_NATIVE_CHANNEL_STATE_MAX_AGE_S "${BAS_NATIVE_CHANNEL_STATE_MAX_AGE_S:-}" "$CHANNEL_STATE_MAX_AGE_S"
reject_product_override BAS_NATIVE_UPDATE_DISTANCE_THRESHOLD_M "${BAS_NATIVE_UPDATE_DISTANCE_THRESHOLD_M:-}" "$UPDATE_DISTANCE_THRESHOLD_M"
[[ "$RADIO_BACKEND" == wifi || "$RADIO_BACKEND" == aloha ]] || { printf 'Invalid radio backend: %s\n' "$RADIO_BACKEND" >&2; exit 2; }
[[ "$PHY_RATE_BPS" =~ ^[1-9][0-9]*$ ]] || { printf 'Invalid native PHY rate: %s\n' "$PHY_RATE_BPS" >&2; exit 2; }
[[ "$WIFI_CHANNEL_NUMBER" =~ ^[1-9][0-9]*$ ]] || { printf 'Invalid Wi-Fi channel number: %s\n' "$WIFI_CHANNEL_NUMBER" >&2; exit 2; }
[[ "$WIFI_CHANNEL_WIDTH_MHZ" =~ ^[1-9][0-9]*$ ]] || { printf 'Invalid Wi-Fi channel width: %s\n' "$WIFI_CHANNEL_WIDTH_MHZ" >&2; exit 2; }
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
if ! git -c safe.directory="$NS3_DIR" -C "$NS3_DIR" apply --reverse --check "$PHASED_ARRAY_ADAPTER_PATCH"; then
  git -c safe.directory="$NS3_DIR" -C "$NS3_DIR" apply "$PHASED_ARRAY_ADAPTER_PATCH" || {
    printf 'SpectrumWifiPhy phased-array adapter patch does not apply.\n' >&2
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
printf 'Starting native five-UAV demo: scenario=%s map=%s gui=%s run=%s\n' \
  "$SCENARIO_KEY" "$MAP_ID" "$GUI" "$RUN_ID"
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
cp "$ROOT_DIR/network/ns3/scratch/native-spectrum-sources.h" "$NS3_DIR/scratch/"
source_args=()
if [[ -n "${BAS_NATIVE_SOURCES:-}" ]]; then
  python3 "$ROOT_DIR/scripts/product/prepare_native_sources.py" \
    --config "$ROOT_DIR/$BAS_NATIVE_SOURCES" --output "$RUN_DIR/logs/native_sources.json"
  source_args+=(--sources="$RUN_DIR/logs/native_sources.json")
fi
if [[ -n "${BAS_NATIVE_EXTERNAL_CONFIG:-}" && "$SCENARIO_MODE" != latency_diagnostic ]]; then
  printf 'External controller requires BAS_NATIVE_LATENCY_MODE=1 (safe requests only).\n' >&2
  exit 2
fi
if [[ "${BAS_NATIVE_FIVE_SKIP_BUILD:-0}" == 1 ]]; then
  [[ -x "$BINARY" ]] || { printf 'Requested binary reuse but binary is absent.\n' >&2; exit 2; }
  printf 'Reused focused native target after exact project/upstream source synchronization.\n' \
    > "$RUN_DIR/logs/ns3_build.log"
else
  (
    cd "$NS3_DIR"
    if [[ ! -f "$NS3_DIR/cmake-cache/CMakeCache.txt" ]]; then
      PYTHONPATH="$PYTHON_TOOLING:$PYTHON_DEPS" ./ns3 configure --enable-examples --enable-tests --enable-python-bindings
    fi
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
  printf 'git_head=%s\n' "${BAS_SOURCE_HEAD:-$(git -C "$ROOT_DIR" rev-parse HEAD)}"
  printf 'source_dirty_paths=%s\n' "${BAS_SOURCE_DIRTY:-unknown}"
  printf 'ns3_version=3.48\n'
  printf 'ns3_exact_sha=%s\n' "$(git -c safe.directory="$NS3_DIR" -C "$NS3_DIR" rev-parse HEAD)"
  printf 'ns3_compatibility_patch=true\n'
  printf 'python_version=%s\n' "$(python3 --version | awk '{print $2}')"
  printf '%s\n' "${DEPENDENCIES[@]}"
  printf 'compiler_version=%s\n' "$(c++ --version | head -n1)"
  printf 'cmake_version=%s\n' "$(cmake --version | head -n1 | awk '{print $3}')"
  printf 'ros_domain_id=%s\n' "$ROS_DOMAIN_ID"
  printf 'gz_partition=%s\n' "$GZ_PARTITION"
  printf 'scenario_key=%s\nscenario=%s\nmap_id=%s\nworld=%s\nscene=%s\n' \
    "$SCENARIO_KEY" "$SCENARIO" "$MAP_ID" "$WORLD" "$SCENE"
  printf 'gui=%s\n' "$GUI"
  printf 'launch_world=%s\ngazebo_requested_rtf=%s\nscenario_timeout_scale=%s\n' \
    "$LAUNCH_WORLD" "$GAZEBO_RTF" "$SCENARIO_TIMEOUT_SCALE"
  printf 'stack_cpuset=%s\nradio_cpuset=%s\n' "$STACK_CPUSET" "$RADIO_CPUSET"
  printf 'radio_config=%s\nradio_backend=%s\n' "$RADIO_CONFIG" "$RADIO_BACKEND"
  printf 'profile=%s\n' "$RADIO_PROFILE"
  printf 'technology_specific_modem=%s\n' "$TECHNOLOGY_SPECIFIC_MODEM"
  printf 'uav_count=%s\nradio_node_count=%s\nshared_spectrum_channels=1\n' "$UAV_COUNT" "$((UAV_COUNT + 1))"
  printf 'carrier_hz=%s\nchannel_number=%s\nchannel_width_mhz=%s\nwifi_data_mode=%s\nwifi_control_mode=%s\n' \
    "$CARRIER_HZ" "$WIFI_CHANNEL_NUMBER" "$WIFI_CHANNEL_WIDTH_MHZ" "$WIFI_DATA_MODE" "$WIFI_CONTROL_MODE"
  printf 'phy_rate_bps=%s\ntx_power_w=%s\nwifi_ssid=%s\n' "$PHY_RATE_BPS" "$TX_POWER_W" "$WIFI_SSID"
  printf 'event_logging=%s\nactive_uart_channels=%s\nlatency_mode=%s\n' "$EVENT_LOGGING" "$ACTIVE_UART_CHANNELS" "$LATENCY_MODE"
  printf 'solver_profile=%s\n' "$SOLVER_PROFILE"
  printf 'sionna_max_depth=%s\nsionna_los=%s\nsionna_specular_reflection=%s\n' \
    "$SIONNA_MAX_DEPTH" "$SIONNA_LOS" "$SIONNA_SPECULAR_REFLECTION"
  printf 'sionna_diffuse_reflection=%s\nsionna_diffraction=%s\nsionna_edge_diffraction=%s\n' \
    "$SIONNA_DIFFUSE_REFLECTION" "$SIONNA_DIFFRACTION" "$SIONNA_EDGE_DIFFRACTION"
  printf 'sionna_refraction=%s\nsionna_synthetic_array=%s\nsionna_seed=%s\n' \
    "$SIONNA_REFRACTION" "$SIONNA_SYNTHETIC_ARRAY" "$SIONNA_SEED"
  printf 'sionna_max_number_of_paths=%s\nsionna_cache_jitter_fraction=%s\n' \
    "$SIONNA_MAX_NUMBER_OF_PATHS" "$SIONNA_CACHE_JITTER_FRACTION"
  printf 'cache_policy=displacement_or_time\nchannel_state_max_age_s=%s\nendpoint_displacement_threshold_m=%s\n' \
    "$CHANNEL_STATE_MAX_AGE_S" "$UPDATE_DISTANCE_THRESHOLD_M"
  printf 'readiness_lag_max_ms=%s\nreadiness_consecutive_samples=%s\n' \
    "$READINESS_LAG_MAX_MS" "$READINESS_CONSECUTIVE_SAMPLES"
  printf 'neighbor_discovery_mode=%s\n' "$NEIGHBOR_DISCOVERY_MODE"
  printf 'reason=%s\npacket_outcome_affected=derived_from_runtime_propagation_chain\n' "$RADIO_REASON"
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
    for _ in $(seq 1 50); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.1
    done
    if kill -0 "$pid" 2>/dev/null; then
      kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
    fi
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

NAMESPACES=(ams-gcs ams-ns3)
for index in "${UAV_INDICES[@]}"; do NAMESPACES+=("ams-uav$index"); done
for namespace in "${NAMESPACES[@]}"; do
  namespace_exists "$namespace" && { printf 'Namespace already exists: %s\n' "$namespace" >&2; exit 3; }
  ip netns add "$namespace"
  created_namespaces+=("$namespace")
  ip -n "$namespace" link set lo up
done

ip link add v-n5-g type veth peer name v-n5-g-n3
ip link set v-n5-g netns ams-gcs
ip link set v-n5-g-n3 netns ams-ns3
ip -n ams-gcs link set v-n5-g name eth0
for index in "${UAV_INDICES[@]}"; do
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
for index in "${UAV_INDICES[@]}"; do
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
for index in "${UAV_INDICES[@]}"; do
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
for index in "${UAV_INDICES[@]}"; do
  for interface in "v-n5-u${index}-n3" "tap-uav$index" "br-uav$index"; do
    ip -n ams-ns3 link set "$interface" addrgenmode none
    ip -n ams-ns3 link set "$interface" up
  done
done
ip -n ams-gcs neigh replace 10.71.0.1 lladdr 02:71:00:00:00:01 nud permanent dev eth0
for index in "${UAV_INDICES[@]}"; do
  ip -n "ams-uav$index" neigh replace "10.71.$index.1" lladdr 02:71:ff:00:00:01 nud permanent dev eth0
done
{
  ip -n ams-gcs neigh show dev eth0
  for index in "${UAV_INDICES[@]}"; do ip -n "ams-uav$index" neigh show dev eth0; done
} > "$RUN_DIR/logs/static_neighbors.txt"

for ((instance=0; instance<UAV_COUNT; ++instance)); do
  setsid socat -d -d "pty,raw,echo=0,link=$UART_DIR/control-sitl-$instance,mode=660" \
    "pty,raw,echo=0,link=$UART_DIR/control-adapter-$instance,mode=660" \
    > "$RUN_DIR/logs/control_socat_uav$((instance + 1)).log" 2>&1 &
  managed_pids+=("$!")
  setsid socat -d -d "pty,raw,echo=0,link=$UART_DIR/payload-sitl-$instance,mode=660" \
    "pty,raw,echo=0,link=$UART_DIR/payload-adapter-$instance,mode=660" \
    > "$RUN_DIR/logs/payload_socat_uav$((instance + 1)).log" 2>&1 &
  managed_pids+=("$!")
done
for ((instance=0; instance<UAV_COUNT; ++instance)); do
  for path in "$UART_DIR/control-sitl-$instance" "$UART_DIR/payload-sitl-$instance"; do
    for _ in $(seq 1 100); do [[ -e "$path" ]] && break; sleep 0.1; done
    [[ -e "$path" ]] || { printf 'PTY missing: %s\n' "$path" >&2; exit 1; }
  done
done

for index in "${UAV_INDICES[@]}"; do
  instance=$((index - 1))
  for channel in control payload; do
    [[ ",$ACTIVE_UART_CHANNELS," == *,$channel,* ]] || continue
    if [[ "$index" == 1 && "$channel" == control && -n "${BAS_NATIVE_EXTERNAL_CONFIG:-}" ]]; then
      setsid ip netns exec ams-uav1 python3 -u "$ROOT_DIR/network/scripts/external_endpoint.py" \
        --config "$ROOT_DIR/$BAS_NATIVE_EXTERNAL_CONFIG" --output "$RUN_DIR/external_endpoint" \
        > "$RUN_DIR/logs/external_endpoint.log" 2>&1 &
      managed_pids+=("$!")
      continue
    fi
    if [[ "$channel" == control ]]; then base_port=14600; else base_port=14700; fi
    setsid ip netns exec "ams-uav$index" python3 -u \
      "$ROOT_DIR/network/scripts/communication_vertical.py" uart-adapter \
      --channel "$channel" --uav-id "$index" --framed --baud-rate 115200 \
      --tty "$UART_DIR/$channel-adapter-$instance" \
      --bind "10.71.$index.10:$((base_port + index))" --peer "10.71.0.10:$base_port" \
      --event-log "$RUN_DIR/logs/${channel}_uart_uav$index.jsonl" \
      --metrics-output "$RUN_DIR/metrics/${channel}_uart_uav$index.json" \
      --event-logging "$EVENT_LOGGING" \
      --ready-file "$RUN_DIR/logs/${channel}_uart_uav$index.ready" \
      > "$RUN_DIR/logs/${channel}_uart_uav$index.log" 2>&1 &
    managed_pids+=("$!")
  done
  setsid ip netns exec "ams-uav$index" python3 -u \
    "$ROOT_DIR/scripts/product/native_radio_five_uav_scenario.py" additional-agent \
    --index "$index" --schedule-file "$SCHEDULE_FILE" \
    --scenario-config "$SCENARIO" \
    --event-log "$RUN_DIR/logs/additional_uav$index.jsonl" \
    --ready-file "$RUN_DIR/logs/additional_uav$index.ready" \
    > "$RUN_DIR/logs/additional_uav$index.log" 2>&1 &
  managed_pids+=("$!")
done
for index in "${UAV_INDICES[@]}"; do
  for channel in control payload; do
    [[ ",$ACTIVE_UART_CHANNELS," == *,$channel,* ]] || continue
    if [[ "$index" == 1 && "$channel" == control && -n "${BAS_NATIVE_EXTERNAL_CONFIG:-}" ]]; then
      wait_for_file "$RUN_DIR/external_endpoint/metrics.json" 15 "external endpoint"
      continue
    fi
    wait_for_file "$RUN_DIR/logs/${channel}_uart_uav$index.ready" 15 "$channel UART adapter"
  done
  wait_for_file "$RUN_DIR/logs/additional_uav$index.ready" 15 "additional endpoint"
done

export ROS_DOMAIN_ID GZ_PARTITION
GAZEBO_GUI=false
HEADLESS_RENDERING=true
if [[ "$GUI" == 1 ]]; then
  GAZEBO_GUI=true
  HEADLESS_RENDERING=false
fi
cd "$WORK_DIR"
setsid taskset -c "$STACK_CPUSET" ros2 launch multiagent_simulation multiagent_simulation.launch.py \
  robots_config_file:="$SCENARIO" world_file:="$LAUNCH_WORLD" robot_model:=iris_radio_headless \
  gui:="$GAZEBO_GUI" rviz:=false headless_rendering:="$HEADLESS_RENDERING" generate_sensor_models:=false \
  use_mapping_camera:=false use_navigation_camera:=false use_zed_camera:=false \
  start_mavproxy:=false sitl_extra_defaults:="$SITL_DEFAULTS" \
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
  if python3 - "$NODE_STATE" "$UAV_COUNT" <<'PY' >/dev/null 2>&1
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
nodes = {node["id"]: node for node in value.get("nodes", [])}
assert value.get("source") == "ros_odometry"
assert not value.get("missing_nodes") and not value.get("stale_nodes")
assert all(not nodes[f"uav{i}"].get("stale") for i in range(1, int(sys.argv[2]) + 1))
PY
  then fresh=1; break; fi
  sleep 0.1
done
((fresh == 1)) || { printf 'Live ROS odometry streams did not become fresh.\n' >&2; exit 1; }
python3 "$ROOT_DIR/scripts/product/town01_stack_health.py" \
  --scenario "$SCENARIO" --tracker-state "$NODE_STATE" --tracker-events "$NODE_EVENTS" \
  --output "$RUN_DIR/metrics/health.json" --timeout-s 180 \
  > "$RUN_DIR/logs/stack_health.log" 2>&1
for index in "${UAV_INDICES[@]}"; do
  topic_log="$RUN_DIR/logs/odometry_uav$index.txt"
  publisher_observed=0
  for _ in $(seq 1 30); do
    timeout 5 ros2 topic info --verbose "/uav$index/odometry" \
      > "$topic_log" 2>&1 || true
    if grep -Eq '^Publisher count: [1-9][0-9]*$' "$topic_log"; then
      publisher_observed=1
      break
    fi
    sleep 0.2
  done
  ((publisher_observed == 1)) || {
    printf 'ROS publisher discovery did not stabilize for /uav%s/odometry.\n' "$index" >&2
    exit 1
  }
done

for camera in overview obstacle uav_focus; do
  setsid ros2 run ros_gz_image image_bridge "/native_radio/$camera/image" \
    > "$RUN_DIR/logs/gazebo_${camera}_image_bridge.log" 2>&1 &
  managed_pids+=("$!")
done
setsid python3 -u "$ROOT_DIR/scripts/product/capture_live_gazebo_screenshots.py" \
  --run-id "$RUN_ID" --output "$RUN_DIR/screenshots" --node-state "$NODE_STATE" \
  --phase-file "$PHASE_FILE" --stop-file "$MONITOR_STOP" --scenario-config "$SCENARIO" \
  > "$RUN_DIR/logs/live_screenshot_capture.log" 2>&1 &
managed_pids+=("$!")

for endpoint in "${TAP_ENDPOINTS[@]}"; do
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
  SIONNA_MITSUBA_VARIANT=cuda_ad_mono_polarized \
  NS_LOG='SionnaRtChannelModel=level_debug|prefix_time' \
  taskset -c "$RADIO_CPUSET" stdbuf -oL -eL "$BINARY" \
  --uavCount="$UAV_COUNT" --radioBackend="$RADIO_BACKEND" \
  --radioProfile="$RADIO_PROFILE" --technologySpecificModem="$TECHNOLOGY_SPECIFIC_MODEM" \
  --neighborDiscoveryMode="$NEIGHBOR_DISCOVERY_MODE" --radioReason="$RADIO_REASON" \
  --solverProfile="$SOLVER_PROFILE" \
  --wifiDataMode="$WIFI_DATA_MODE" --wifiControlMode="$WIFI_CONTROL_MODE" \
  --wifiChannelNumber="$WIFI_CHANNEL_NUMBER" \
  --wifiChannelWidthMhz="$WIFI_CHANNEL_WIDTH_MHZ" --wifiSsid="$WIFI_SSID" \
  --carrierHz="$CARRIER_HZ" --tapGcs=tap-gcs --tapUavs="$TAP_UAVS" \
  --scene="$SCENE" --positionFile="$NODE_STATE" --phaseFile="$PHASE_FILE" \
  --radioPcap="$RUN_DIR/pcap/native_radio.pcap" \
  --eventCsv="$RUN_DIR/logs/native_radio_events.csv" \
  --statsFile="$RUN_DIR/metrics/native_radio_stats.json" \
  --readyFile="$NS3_READY" --duration=2400 --txPowerW="$TX_POWER_W" \
  "${source_args[@]}" \
  --phyRateBps="$PHY_RATE_BPS" --eventLogging="$EVENT_LOGGING" \
  --channelStateMaxAgeS="$CHANNEL_STATE_MAX_AGE_S" \
  --updateDistanceThresholdM="$UPDATE_DISTANCE_THRESHOLD_M" \
  --sionnaMaxDepth="$SIONNA_MAX_DEPTH" --sionnaLos="$SIONNA_LOS" \
  --sionnaSpecularReflection="$SIONNA_SPECULAR_REFLECTION" \
  --sionnaDiffuseReflection="$SIONNA_DIFFUSE_REFLECTION" \
  --sionnaDiffraction="$SIONNA_DIFFRACTION" \
  --sionnaEdgeDiffraction="$SIONNA_EDGE_DIFFRACTION" \
  --sionnaRefraction="$SIONNA_REFRACTION" \
  --sionnaSyntheticArray="$SIONNA_SYNTHETIC_ARRAY" \
  --sionnaSeed="$SIONNA_SEED" --sionnaMaxNumberOfPaths="$SIONNA_MAX_NUMBER_OF_PATHS" \
  --sionnaCacheJitterFraction="$SIONNA_CACHE_JITTER_FRACTION" \
  --readinessLagMaxMs="$READINESS_LAG_MAX_MS" \
  --readinessConsecutiveSamples="$READINESS_CONSECUTIVE_SAMPLES" \
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
  --mode="$SCENARIO_MODE" --uav-count="$UAV_COUNT" --channels="$ACTIVE_UART_CHANNELS" \
  --radio-profile="$RADIO_PROFILE" --scenario-config="$SCENARIO" \
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
if [[ "$SCENARIO_MODE" == product ]]; then
  ip netns exec ams-gcs python3 -u "$ROOT_DIR/scripts/product/native_radio_five_uav_scenario.py" no-bypass-probe \
    --run-dir "$RUN_DIR" --node-state "$NODE_STATE" --duration-s 10.5 \
    --radio-profile="$RADIO_PROFILE" --scenario-config="$SCENARIO" \
    --output "$RUN_DIR/metrics/no_bypass_summary.json" \
    > "$RUN_DIR/logs/no_bypass_probe.log" 2>&1
  ip netns exec ams-gcs ss -H -tunap > "$RUN_DIR/logs/gcs_sockets_after_10s.txt" 2>&1 || true
fi

for pid in "${capture_pids[@]}"; do
  kill -INT -- "-$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
done
capture_pids=()
touch "$MONITOR_STOP"
sleep 2
summary_args=(--run-dir "$RUN_DIR" --scenario-config "$SCENARIO")
if [[ "$SCENARIO_MODE" == latency_diagnostic ]]; then
  summary_args+=(--latency-diagnostic)
elif [[ -n "$ONE_UAV_RUN" ]]; then
  [[ -d "$ONE_UAV_RUN" ]] || { printf 'One-UAV regression run is absent: %s\n' "$ONE_UAV_RUN" >&2; exit 2; }
  summary_args+=(--one-uav-run "$ONE_UAV_RUN")
fi
python3 "$ROOT_DIR/scripts/product/summarize_native_radio_five_uav.py" "${summary_args[@]}" \
  > "$RUN_DIR/logs/summary.log" 2>&1
printf 'Native radio run complete: %s\n' "$RUN_DIR"
