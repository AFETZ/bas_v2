#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUNTIME_DIR="${BAS_RUNTIME_DIR:-${XDG_RUNTIME_DIR:-/tmp}/bas-v2-${UID}}"
PID_FILE="$RUNTIME_DIR/base.pid"

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

setsid ros2 launch multiagent_simulation multiagent_simulation.launch.py \
  robots_config_file:="$ROOT_DIR/network/config/scenario_5uav.yaml" \
  world_file:=modelflughafen/model.sdf \
  gui:="${BAS_GUI:-false}" \
  rviz:="${BAS_RVIZ:-false}" \
  use_mapping_camera:=false \
  use_navigation_camera:=false \
  use_zed_camera:=false &
child_pid=$!
printf '%s\n' "$child_pid" > "$PID_FILE"
printf 'Five-UAV base started as process group %s. Use make stop from another terminal.\n' "$child_pid"

cleanup() {
  rm -f "$PID_FILE"
}

stop_child() {
  if kill -0 "$child_pid" 2>/dev/null; then
    kill -TERM -- "-$child_pid" 2>/dev/null || true
  fi
}

trap cleanup EXIT
trap stop_child INT TERM HUP
set +e
wait "$child_pid"
status=$?
set -e
exit "$status"
