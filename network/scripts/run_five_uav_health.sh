#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUN_ID="${RUN_ID:-m1_five_uav_health_$(date -u +%Y%m%dT%H%M%SZ)}"
RUN_DIR="${RUN_DIR:-$ROOT_DIR/runs/$RUN_ID}"
RUNTIME_DIR="$RUN_DIR/runtime"
SCENARIO="${SCENARIO:-$ROOT_DIR/network/config/scenario_5uav.yaml}"
DURATION_S="${DURATION_S:-300}"
MINIMUM_DURATION_S="${MINIMUM_DURATION_S:-300}"
WARMUP_S="${WARMUP_S:-30}"
READINESS_TIMEOUT_S="${READINESS_TIMEOUT_S:-90}"
READINESS_STABILITY_S="${READINESS_STABILITY_S:-5}"
ROBOT_MODEL="${ROBOT_MODEL:-iris_radio_headless}"
RUNTIME_ID="${AMS_RUNTIME_ID:-m1-$(python3 -c 'import uuid; print(uuid.uuid4())')}"
ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-$((20 + $(printf '%s' "$RUN_ID" | cksum | awk '{print $1}') % 180))}"
GZ_PARTITION="${GZ_PARTITION:-ams_${RUN_ID//[^a-zA-Z0-9_]/_}}"

if [[ -e "$RUN_DIR" ]]; then
  printf 'FAIL immutable M1 run directory already exists: %s\n' "$RUN_DIR" >&2
  exit 1
fi
mkdir -p "$RUN_DIR/logs" "$RUN_DIR/metrics" "$RUNTIME_DIR"

port_is_bindable() {
  local protocol="$1"
  local port="$2"
  python3 - "$protocol" "$port" <<'PY'
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
  printf 'profile=m1_component\n'
  printf 'scenario_id=scenario_5uav\n'
  printf 'phase_manifest=readiness,measurement,finalization\n'
  printf 'ros_domain_id=%s\n' "$ROS_DOMAIN_ID"
  printf 'gz_partition=%s\n' "$GZ_PARTITION"
  printf 'component_only=true\n'
  printf 'packet_path_eligible=false\n'
} > "$RUN_DIR/environment.txt"

cleanup() {
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

python3 "$ROOT_DIR/network/scripts/write_run_provenance.py" --run-dir "$RUN_DIR" \
  > "$RUN_DIR/logs/provenance.log" 2>&1
if ! WORLD_FILE="$(
  python3 "$ROOT_DIR/network/scripts/write_m1_scene_provenance.py" \
    --run-dir "$RUN_DIR" \
    --scenario "$SCENARIO" \
    --robot-model "$ROBOT_MODEL" \
    --runtime-id "$RUNTIME_ID" \
    2> "$RUN_DIR/logs/m1_scene_provenance.log"
)"; then
  cat "$RUN_DIR/logs/m1_scene_provenance.log" >&2
  exit 1
fi
if ! WORLD_NAME="$(
  python3 -c \
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
  exec setsid ros2 launch multiagent_simulation multiagent_simulation.launch.py \
    robots_config_file:="$SCENARIO" \
    world_file:="$WORLD_FILE" \
    robot_model:="$ROBOT_MODEL" \
    enable_serial2:=false \
    gui:=false rviz:=false headless_rendering:=false \
    use_mapping_camera:=false \
    use_navigation_camera:=false \
    use_zed_camera:=false
) > "$RUN_DIR/logs/five_uav_launch.log" 2>&1 &
LAUNCH_PID=$!
printf '%s\n' "$LAUNCH_PID" > "$RUN_DIR/logs/five_uav_launch.pid"

sleep "$WARMUP_S"
if ! kill -0 "$LAUNCH_PID" >/dev/null 2>&1; then
  printf 'FAIL five-UAV launch exited during warmup; see %s\n' "$RUN_DIR/logs/five_uav_launch.log" >&2
  exit 1
fi
if grep -Eiq \
  'bind error|bind failed|failed to bind|address already in use|segmentation fault|core dumped|process has died|error while starting ipvx agent|failed to open \(.*ttyros|traceback \(most recent call last\)|failed to download /srtm3?' \
  "$RUN_DIR/logs/five_uav_launch.log"; then
  printf 'FAIL five-UAV launch log contains a fatal startup marker; see %s\n' \
    "$RUN_DIR/logs/five_uav_launch.log" >&2
  exit 1
fi
set +e
python3 "$ROOT_DIR/network/tests/collect_five_uav_health.py" \
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

if ! kill -0 "$LAUNCH_PID" >/dev/null 2>&1; then
  printf 'FAIL five-UAV launch exited during health observation\n' >&2
  exit 1
fi
if (( COLLECTOR_RC != 0 )); then
  exit "$COLLECTOR_RC"
fi

# Freeze every launch-owned writer before independently reading bounded evidence.
cleanup
LAUNCH_PID=""
python3 "$ROOT_DIR/network/scripts/validate_m1_health.py" --run-dir "$RUN_DIR"

printf 'Five-UAV M1 health run complete: %s\n' "$RUN_DIR"
