#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUNTIME_DIR="${BAS_RUNTIME_DIR:-${XDG_RUNTIME_DIR:-/tmp}/bas-v2-${UID}}"
PID_FILE="$RUNTIME_DIR/base.pid"
CONTAINER_NAME="${BAS_BASE_CONTAINER_NAME:-bas-v2-baseline}"

run_in_container() {
  command -v docker >/dev/null 2>&1 || {
    printf 'ros2 is unavailable and Docker is not installed.\n' >&2
    return 2
  }

  local image="${BAS_CONTAINER_IMAGE:-multiagent_simulation:latest}"
  local image_id
  image_id="$(docker image inspect --format '{{.Id}}' "$image" 2>/dev/null)" || {
    printf 'Runtime image is unavailable: %s (no rebuild was attempted).\n' "$image" >&2
    return 2
  }

  local host_uid host_gid device device_gid
  host_uid="$(id -u)"
  host_gid="$(id -g)"
  local -a group_args=(--group-add 1000)
  local seen_groups=" 1000 "
  for device in /dev/dri/renderD* /dev/dri/card*; do
    [[ -e "$device" ]] || continue
    device_gid="$(stat -c '%g' "$device")"
    if [[ "$seen_groups" != *" $device_gid "* ]]; then
      group_args+=(--group-add "$device_gid")
      seen_groups+="$device_gid "
    fi
  done

  local -a gpu_args=()
  if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
    gpu_args=(
      --gpus all
      -e NVIDIA_VISIBLE_DEVICES=all
      -e NVIDIA_DRIVER_CAPABILITIES=graphics,utility,compute
    )
  fi

  printf 'Starting five-UAV baseline in existing image %s.\n' "$image_id"
  set +e
  docker run --rm \
    --name "$CONTAINER_NAME" \
    --label bas.product=base \
    --privileged \
    --network=host \
    --user "$host_uid:$host_gid" \
    "${group_args[@]}" \
    "${gpu_args[@]}" \
    -e BAS_IN_CONTAINER=1 \
    -e HOME=/tmp/bas-home \
    -e XDG_RUNTIME_DIR=/tmp/bas-xdg \
    -e PYTHONPATH=/home/ubuntu/.local/lib/python3.10/site-packages \
    -v "$ROOT_DIR":/workspace/multiagent_simulation \
    -w /workspace/multiagent_simulation \
    "$image_id" \
    bash -lc '
      set -eo pipefail
      mkdir -p "$HOME" "$XDG_RUNTIME_DIR"
      chmod 700 "$HOME" "$XDG_RUNTIME_DIR"
      set +u
      source /opt/ros/humble/setup.bash
      source /workspace/ardu_ws/install/setup.bash
      export PATH="/home/ubuntu/.local/bin:$PATH"
      export GZ_VERSION=harmonic
      export GZ_SIM_RESOURCE_PATH="${GZ_SIM_RESOURCE_PATH:-}:$PWD/src/multiagent_simulation/models:$PWD/src/multiagent_simulation/worlds:$PWD/src"
      colcon build --symlink-install
      source install/setup.bash
      set -u
      exec ./scripts/product/run_5uav_base.sh
    '
  local status=$?
  set -e
  if [[ "$status" -eq 137 || "$status" -eq 143 ]]; then
    return 0
  fi
  return "$status"
}

source_if_present() {
  local setup_file="$1"
  if [[ -f "$setup_file" ]]; then
    set +u
    # shellcheck disable=SC1090
    source "$setup_file"
    set -u
  fi
}

source_if_present /opt/ros/humble/setup.bash
source_if_present /workspace/ardu_ws/install/setup.bash
source_if_present "$ROOT_DIR/install/setup.bash"

if [[ "${BAS_IN_CONTAINER:-0}" != "1" ]] && ! command -v ros2 >/dev/null 2>&1; then
  run_in_container
  exit $?
fi

command -v ros2 >/dev/null 2>&1 || {
  printf 'ros2 is unavailable; source ROS 2, ArduPilot, and this workspace first.\n' >&2
  exit 2
}
command -v setsid >/dev/null 2>&1 || {
  printf 'setsid is unavailable; install util-linux.\n' >&2
  exit 2
}

mkdir -p "$RUNTIME_DIR"
if [[ -f "$PID_FILE" ]]; then
  old_pid="$(<"$PID_FILE")"
  if [[ "$old_pid" =~ ^[0-9]+$ ]] && kill -0 "$old_pid" 2>/dev/null; then
    printf 'Base simulation already appears to be running with PID %s.\n' "$old_pid" >&2
    exit 3
  fi
  rm -f "$PID_FILE"
fi

run_id="${BAS_RUN_ID:-baseline-$(date -u +%Y%m%dT%H%M%SZ)}"
run_dir="${BAS_RUN_DIR:-$ROOT_DIR/runs/$run_id}"
work_dir="$RUNTIME_DIR/base-work"
launch_log="$run_dir/logs/base.log"
tracker_log="$run_dir/logs/position_tracker.log"
tracker_json="$run_dir/metrics/node_state.json"
tracker_jsonl="$run_dir/metrics/node_state.jsonl"
health_json="$run_dir/metrics/health.json"
mkdir -p "$run_dir/logs" "$run_dir/metrics" "$work_dir"

cd "$work_dir"
setsid ros2 launch multiagent_simulation multiagent_simulation.launch.py \
  robots_config_file:="$ROOT_DIR/network/config/scenario_5uav.yaml" \
  world_file:=modelflughafen/model.sdf \
  robot_model:=iris_radio_headless \
  gui:="${BAS_GUI:-false}" \
  rviz:="${BAS_RVIZ:-false}" \
  headless_rendering:=true \
  generate_sensor_models:=false \
  use_mapping_camera:=false \
  use_navigation_camera:=false \
  use_zed_camera:=false \
  >"$launch_log" 2>&1 &
child_pid=$!
printf '%s\n' "$child_pid" > "$PID_FILE"

python3 "$ROOT_DIR/network/position_tracker/tracker.py" \
  --scenario "$ROOT_DIR/network/config/scenario_5uav.yaml" \
  --output-json "$tracker_json" \
  --output-jsonl "$tracker_jsonl" \
  >"$tracker_log" 2>&1 &
tracker_pid=$!

printf 'Five-UAV runtime started as process group %s; logs: %s\n' "$child_pid" "$launch_log"

cleanup() {
  rm -f "$PID_FILE"
}

stop_child() {
  if kill -0 "$child_pid" 2>/dev/null; then
    kill -TERM -- "-$child_pid" 2>/dev/null || true
  fi
  if kill -0 "$tracker_pid" 2>/dev/null; then
    kill -TERM "$tracker_pid" 2>/dev/null || true
  fi
}

trap cleanup EXIT
trap 'stop_child; exit 0' INT TERM HUP

set +e
python3 "$ROOT_DIR/scripts/product/five_uav_health.py" \
  --scenario "$ROOT_DIR/network/config/scenario_5uav.yaml" \
  --tracker-state "$tracker_json" \
  --tracker-events "$tracker_jsonl" \
  --output "$health_json" \
  --timeout-s "${BAS_HEALTH_TIMEOUT_S:-120}"
health_status=$?
set -e
if [[ "$health_status" -ne 0 ]]; then
  printf 'Baseline health failed; see %s, %s and %s.\n' \
    "$health_json" "$launch_log" "$tracker_log" >&2
  stop_child
  wait "$child_pid" 2>/dev/null || true
  wait "$tracker_pid" 2>/dev/null || true
  exit "$health_status"
fi

printf 'Baseline is healthy. Summary: %s\n' "$health_json"
printf 'Use make stop from another terminal.\n'
set +e
wait "$child_pid"
status=$?
set -e
if kill -0 "$tracker_pid" 2>/dev/null; then
  kill -TERM "$tracker_pid" 2>/dev/null || true
fi
wait "$tracker_pid" 2>/dev/null || true
exit "$status"
