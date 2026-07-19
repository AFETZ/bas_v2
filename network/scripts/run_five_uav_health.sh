#!/usr/bin/bash
set -euo pipefail

SCRIPT_DIR="${BASH_SOURCE[0]%/*}"
if [[ "$SCRIPT_DIR" == "${BASH_SOURCE[0]}" ]]; then
  SCRIPT_DIR=.
fi
cd -- "$SCRIPT_DIR/../.."
ROOT_DIR="$PWD"
# The installed ROS 2 launch module is imported directly from the fresh
# run-local overlay.  Never let that import mutate the evidence tree with a
# launch/__pycache__ entry: source/install equality is an acceptance gate.
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPYCACHEPREFIX=/tmp/ams-m1-pycache
M1_PYTHON=/usr/bin/python3.10
M1_PYTHON_SITE=/home/ubuntu/.local/lib/python3.10/site-packages
case ":${PYTHONPATH:-}:" in
  *":$M1_PYTHON_SITE:"*) ;;
  *) export PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}$M1_PYTHON_SITE" ;;
esac
RUN_PROFILE="${AMS_FLIGHT_RUN_PROFILE:-m1_component}"
case "$RUN_PROFILE" in
  m1_component)
    CAPACITY_MODE=0
    DEFAULT_RUN_PREFIX=m1_five_uav_health
    ;;
  flight_capacity_prerequisite)
    CAPACITY_MODE=1
    DEFAULT_RUN_PREFIX=flight_capacity
    ;;
  *)
    printf 'FAIL unsupported five-UAV flight profile: %s\n' "$RUN_PROFILE" >&2
    exit 2
    ;;
esac
RUN_ID="${RUN_ID:-${DEFAULT_RUN_PREFIX}_$(date -u +%Y%m%dT%H%M%SZ)}"
RUN_DIR="${RUN_DIR:-$ROOT_DIR/runs/$RUN_ID}"
RUNTIME_DIR="$RUN_DIR/runtime"
SCENARIO="${SCENARIO:-$ROOT_DIR/network/config/scenario_5uav.yaml}"
DURATION_S="${DURATION_S:-300}"
MINIMUM_DURATION_S="${MINIMUM_DURATION_S:-300}"
WARMUP_S="${WARMUP_S:-30}"
READINESS_TIMEOUT_S="${READINESS_TIMEOUT_S:-90}"
READINESS_STABILITY_S="${READINESS_STABILITY_S:-5}"
ROBOT_MODEL="${ROBOT_MODEL:-iris_radio_headless}"
RUNTIME_ID="${AMS_RUNTIME_ID:-${RUN_PROFILE//_/-}-$(/usr/bin/python3.10 -c 'import uuid; print(uuid.uuid4())')}"
ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-$((20 + $(printf '%s' "$RUN_ID" | cksum | awk '{print $1}') % 180))}"
GZ_PARTITION="${GZ_PARTITION:-ams_${RUN_ID//[^a-zA-Z0-9_]/_}}"
export GZ_IP=127.0.0.1

if [[ -e "$RUN_DIR" ]]; then
  printf 'FAIL immutable M1 run directory already exists: %s\n' "$RUN_DIR" >&2
  exit 1
fi
mkdir -p "$RUN_DIR/logs" "$RUN_DIR/metrics" "$RUNTIME_DIR"

OVERLAY_ROOT="$RUN_DIR/runtime_overlay"
OVERLAY_BUILD="$OVERLAY_ROOT/build"
OVERLAY_INSTALL="$OVERLAY_ROOT/install"
OVERLAY_LOG="$OVERLAY_ROOT/log"
EXPECTED_SHARE="$OVERLAY_INSTALL/multiagent_simulation/share/multiagent_simulation"
BUILD_COMMAND=(
  /usr/bin/colcon --log-base "$OVERLAY_LOG" build
  --base-paths "$ROOT_DIR/src/multiagent_simulation"
  --build-base "$OVERLAY_BUILD"
  --install-base "$OVERLAY_INSTALL"
)
printf '%q ' "${BUILD_COMMAND[@]}" > "$RUN_DIR/logs/m1_runtime_overlay_build.command"
printf '\n' >> "$RUN_DIR/logs/m1_runtime_overlay_build.command"
set +e
"${BUILD_COMMAND[@]}" > "$RUN_DIR/logs/m1_runtime_overlay_build.log" 2>&1
OVERLAY_BUILD_RC=$?
set -e
printf '%s\n' "$OVERLAY_BUILD_RC" > "$RUN_DIR/logs/m1_runtime_overlay_build.exit_code"
if ((OVERLAY_BUILD_RC != 0)) || [[ ! -f "$OVERLAY_INSTALL/setup.bash" ]]; then
  printf 'FAIL fresh M1 runtime overlay build failed\n' >&2
  exit 1
fi
# Provenance must observe the locked base image/ROS inventory.  The fresh
# run-local overlay is a runtime input proved separately below; sourcing it
# first would add the project package to the dependency inventory itself.
"$M1_PYTHON" "$ROOT_DIR/network/scripts/write_run_provenance.py" --run-dir "$RUN_DIR" \
  --qualification-profile "$RUN_PROFILE" --consumed-node Q0 --consumed-node Q1 \
  > "$RUN_DIR/logs/provenance.log" 2>&1
# shellcheck disable=SC1090
set +u
source "$OVERLAY_INSTALL/setup.bash"
set -u
RESOLVED_SHARE="$("$M1_PYTHON" -c 'from ament_index_python.packages import get_package_share_directory; print(get_package_share_directory("multiagent_simulation"))')"
if [[ "$RESOLVED_SHARE" != "$EXPECTED_SHARE" ]] || [[ ! -d "$EXPECTED_SHARE" ]]; then
  printf 'FAIL M1 resolved package share is not the fresh run overlay: %s\n' \
    "$RESOLVED_SHARE" >&2
  exit 1
fi
mapfile -t M1_PYTHON_RUNTIME < <(
  "$M1_PYTHON" - <<'PY'
import pathlib
import sys

import pymavlink

print(pathlib.Path(sys.executable).resolve())
print(int(sys.flags.no_user_site))
print(pathlib.Path(pymavlink.__file__).resolve())
PY
)
EXPECTED_PYMAVLINK_ORIGIN="$M1_PYTHON_SITE/pymavlink/__init__.py"
if ((${#M1_PYTHON_RUNTIME[@]} != 3)) || \
  [[ "${M1_PYTHON_RUNTIME[0]}" != "$M1_PYTHON" ]] || \
  [[ "${M1_PYTHON_RUNTIME[1]}" != "1" ]] || \
  [[ "${M1_PYTHON_RUNTIME[2]}" != "$EXPECTED_PYMAVLINK_ORIGIN" ]]; then
  printf 'FAIL controlled M1 Python/pymavlink runtime is unavailable\n' >&2
  exit 1
fi
export AMS_M1_INSTALLED_SHARE="$EXPECTED_SHARE"
export GZ_SIM_RESOURCE_PATH="$EXPECTED_SHARE/models:$EXPECTED_SHARE/worlds:$EXPECTED_SHARE"

port_is_bindable() {
  local protocol="$1"
  local port="$2"
  "$M1_PYTHON" - "$protocol" "$port" <<'PY'
import socket
import sys

kind = socket.SOCK_STREAM if sys.argv[1] == "tcp" else socket.SOCK_DGRAM
probe = socket.socket(socket.AF_INET, kind)
try:
    probe.bind(("0.0.0.0", int(sys.argv[2])))
except OSError as exc:
    print(exc, file=sys.stderr)
    raise SystemExit(1)
finally:
    probe.close()
PY
}

for port in 14550 2019 2020 2021 2022 2023; do
  if ! port_is_bindable udp "$port"; then
    printf 'FAIL required M1 UDP port is not bindable: %s\n' "$port" >&2
    exit 1
  fi
done
for port in 5760 5770 5780 5790 5800; do
  if ! port_is_bindable tcp "$port"; then
    printf 'FAIL required M1 TCP port is not bindable: %s\n' "$port" >&2
    exit 1
  fi
done

printf '%q ' "$0" "$@" > "$RUN_DIR/command.txt"
printf '\n' >> "$RUN_DIR/command.txt"
{
  printf 'run_id=%s\n' "$RUN_ID"
  printf 'utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'scenario=%s\n' "$SCENARIO"
  printf 'duration_s=%s\n' "$DURATION_S"
  printf 'minimum_duration_s=%s\n' "$MINIMUM_DURATION_S"
  printf 'warmup_s=%s\n' "$WARMUP_S"
  printf 'readiness_timeout_s=%s\n' "$READINESS_TIMEOUT_S"
  printf 'readiness_stability_s=%s\n' "$READINESS_STABILITY_S"
  printf 'robot_model=%s\n' "$ROBOT_MODEL"
  printf 'enable_serial2=false\n'
  printf 'runtime_id=%s\n' "$RUNTIME_ID"
  printf 'profile=%s\n' "$RUN_PROFILE"
  printf 'scenario_id=scenario_5uav\n'
  if ((CAPACITY_MODE == 1)); then
    printf 'phase_manifest=readiness,warmup_30s,measurement_300s,finalization\n'
  else
    printf 'phase_manifest=readiness,measurement,finalization\n'
  fi
  printf 'ros_domain_id=%s\n' "$ROS_DOMAIN_ID"
  printf 'gz_partition=%s\n' "$GZ_PARTITION"
  printf 'gz_ip=%s\n' "$GZ_IP"
  printf 'component_only=true\n'
  printf 'packet_path_eligible=false\n'
  printf 'source_mode=%s\n' "${AMS_M1_SOURCE_MODE:-diagnostic_live_checkout}"
  printf 'source_commit=%s\n' "${AMS_M1_SOURCE_COMMIT:-unknown}"
  printf 'runtime_overlay=%s\n' "$OVERLAY_ROOT"
  printf 'installed_package_share=%s\n' "$EXPECTED_SHARE"
  printf 'gz_sim_resource_path=%s\n' "$GZ_SIM_RESOURCE_PATH"
  printf 'generate_sensor_models=false\n'
  printf 'python_dont_write_bytecode=%s\n' "$PYTHONDONTWRITEBYTECODE"
  printf 'python_pycache_prefix=%s\n' "$PYTHONPYCACHEPREFIX"
  printf 'python_executable=%s\n' "${M1_PYTHON_RUNTIME[0]}"
  printf 'python_no_user_site=%s\n' "${M1_PYTHON_RUNTIME[1]}"
  printf 'pymavlink_origin=%s\n' "${M1_PYTHON_RUNTIME[2]}"
} > "$RUN_DIR/environment.txt"

cleanup() {
  if [[ -n "${HEALTH_COLLECTOR_PID:-}" ]]; then
    kill -TERM "$HEALTH_COLLECTOR_PID" >/dev/null 2>&1 || true
    wait "$HEALTH_COLLECTOR_PID" >/dev/null 2>&1 || true
  fi
  if [[ -n "${LAUNCH_PID:-}" ]]; then
    kill -TERM -- "-$LAUNCH_PID" >/dev/null 2>&1 || kill -TERM "$LAUNCH_PID" >/dev/null 2>&1 || true
    for _ in {1..25}; do
      if ! kill -0 -- "-$LAUNCH_PID" >/dev/null 2>&1; then
        break
      fi
      sleep 0.2
    done
    kill -KILL -- "-$LAUNCH_PID" >/dev/null 2>&1 || true
    wait "$LAUNCH_PID" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT INT TERM

export AMS_RUNTIME_ID="$RUNTIME_ID"
export ROS_DOMAIN_ID
export GZ_PARTITION

if ! WORLD_FILE="$(
  "$M1_PYTHON" "$ROOT_DIR/network/scripts/write_m1_scene_provenance.py" \
    --run-dir "$RUN_DIR" \
    --scenario "$SCENARIO" \
    --robot-model "$ROBOT_MODEL" \
    --runtime-id "$RUNTIME_ID" \
    --installed-package-share "$EXPECTED_SHARE" \
    2> "$RUN_DIR/logs/m1_scene_provenance.log"
)"; then
  cat "$RUN_DIR/logs/m1_scene_provenance.log" >&2
  exit 1
fi
if ! WORLD_NAME="$(
  "$M1_PYTHON" -c \
    'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["gazebo"]["world_name"])' \
    "$RUN_DIR/metrics/m1_scene_provenance.json"
)"; then
  printf 'FAIL could not read the derived Gazebo world name\n' >&2
  exit 1
fi
{
  printf 'world_file=%s\n' "$WORLD_FILE"
  printf 'world_name=%s\n' "$WORLD_NAME"
  printf 'scene_contract=ams.m1.health/v3\n'
  printf 'scene_provenance=metrics/m1_scene_provenance.json\n'
} >> "$RUN_DIR/environment.txt"

(
  cd "$RUNTIME_DIR"
  exec setsid /opt/ros/humble/bin/ros2 launch multiagent_simulation multiagent_simulation.launch.py \
    robots_config_file:="$SCENARIO" \
    world_file:="$WORLD_FILE" \
    robot_model:="$ROBOT_MODEL" \
    enable_serial2:=false \
    generate_sensor_models:=false \
    gui:=false rviz:=false headless_rendering:=false \
    use_mapping_camera:=false \
    use_navigation_camera:=false \
    use_zed_camera:=false
) > "$RUN_DIR/logs/five_uav_launch.log" 2>&1 &
LAUNCH_PID=$!
printf '%s\n' "$LAUNCH_PID" > "$RUN_DIR/logs/five_uav_launch.pid"

if ((CAPACITY_MODE == 1)); then
  set +e
  "$M1_PYTHON" "$ROOT_DIR/network/tests/collect_five_uav_health.py" \
    --scenario "$SCENARIO" \
    --run-dir "$RUN_DIR" \
    --runtime-id "$RUNTIME_ID" \
    --launch-process-group "$LAUNCH_PID" \
    --duration-s 300 \
    --minimum-duration-s 300 \
    --readiness-timeout-s "$READINESS_TIMEOUT_S" \
    --readiness-stability-s "$READINESS_STABILITY_S" \
    --heartbeat-endpoint "udpin:127.0.0.1:14550" \
    --launch-log "$RUN_DIR/logs/five_uav_launch.log" \
    --world "$WORLD_NAME" \
    > "$RUN_DIR/logs/flight_capacity_health_collector.stdout.log" \
    2> "$RUN_DIR/logs/flight_capacity_health_collector.stderr.log" &
  HEALTH_COLLECTOR_PID=$!
  set -e
  set +e
  "$M1_PYTHON" "$ROOT_DIR/network/scripts/collect_flight_capacity.py" \
    --run-dir "$RUN_DIR" \
    --runtime-id "$RUNTIME_ID" \
    --launch-process-group "$LAUNCH_PID"
  CAPACITY_COLLECTOR_RC=$?
  wait "$HEALTH_COLLECTOR_PID"
  HEALTH_COLLECTOR_RC=$?
  HEALTH_COLLECTOR_PID=""
  set -e
  if ((CAPACITY_COLLECTOR_RC != 0 || HEALTH_COLLECTOR_RC != 0)); then
    printf 'FAIL capacity collectors failed: capacity=%s health=%s\n' \
      "$CAPACITY_COLLECTOR_RC" "$HEALTH_COLLECTOR_RC" >&2
    exit 1
  fi
else
  sleep "$WARMUP_S"
  if ! kill -0 "$LAUNCH_PID" >/dev/null 2>&1; then
    printf 'FAIL five-UAV launch exited during warmup; see %s\n' "$RUN_DIR/logs/five_uav_launch.log" >&2
    exit 1
  fi
  set +e
  "$M1_PYTHON" "$ROOT_DIR/network/tests/collect_five_uav_health.py" \
    --scenario "$SCENARIO" \
    --run-dir "$RUN_DIR" \
    --runtime-id "$RUNTIME_ID" \
    --launch-process-group "$LAUNCH_PID" \
    --duration-s "$DURATION_S" \
    --minimum-duration-s "$MINIMUM_DURATION_S" \
    --readiness-timeout-s "$READINESS_TIMEOUT_S" \
    --readiness-stability-s "$READINESS_STABILITY_S" \
    --heartbeat-endpoint "udpin:127.0.0.1:14550" \
    --launch-log "$RUN_DIR/logs/five_uav_launch.log" \
    --world "$WORLD_NAME"
  COLLECTOR_RC=$?
  set -e
  if ((COLLECTOR_RC != 0)); then
    exit "$COLLECTOR_RC"
  fi
fi

if grep -Eiq \
  'bind error|bind failed|failed to bind|address already in use|segmentation fault|core dumped|process has died|error while starting ipvx agent|failed to open \(.*ttyros|traceback \(most recent call last\)|failed to download /srtm3?' \
  "$RUN_DIR/logs/five_uav_launch.log"; then
  printf 'FAIL five-UAV launch log contains a fatal marker; see %s\n' \
    "$RUN_DIR/logs/five_uav_launch.log" >&2
  exit 1
fi

if ! kill -0 "$LAUNCH_PID" >/dev/null 2>&1; then
  printf 'FAIL five-UAV launch exited during health observation\n' >&2
  exit 1
fi
# Freeze every launch-owned writer before independently reading bounded evidence.
cleanup
LAUNCH_PID=""
# Build intermediates are not runtime inputs and can contain absolute CMake
# links back to the temporary checkout.  Preserve the raw build command/log,
# but publish only the relocatable install tree used by the launch.
/usr/bin/rm -rf -- "$OVERLAY_BUILD" "$OVERLAY_LOG"
if ((CAPACITY_MODE == 1)); then
  "$M1_PYTHON" "$ROOT_DIR/network/scripts/validate_flight_capacity.py" --run-dir "$RUN_DIR"
  printf 'Five-UAV flight capacity run complete: %s\n' "$RUN_DIR"
else
  "$M1_PYTHON" "$ROOT_DIR/network/scripts/validate_m1_health.py" --run-dir "$RUN_DIR"
  printf 'Five-UAV M1 health run complete: %s\n' "$RUN_DIR"
fi
