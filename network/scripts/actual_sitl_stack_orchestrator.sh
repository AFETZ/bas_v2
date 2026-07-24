#!/usr/bin/env bash
set -Eeuo pipefail
umask 0002

# Shared long-lived launcher for the accepted actual ArduPilot/MAVProxy tail.
# The caller owns namespaces/captures.  This process owns exactly one ROS/Gazebo
# flight launch, five byte-opaque UAV adapters, and one lineage supervisor.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ADAPTER="$ROOT_DIR/network/bridge/actual_sitl_mavlink_endpoint.py"
ENDPOINT_SUPERVISOR="$ROOT_DIR/network/scripts/actual_sitl_endpoint_orchestrator.py"

RUN_DIR=""
RUN_ID=""
RUNTIME_ID=""
RUN_NONCE=""
PROFILE=""
INSTALLED_SHARE=""
FLIGHT_SCENARIO=""
WORLD_FILE=""
MANIFEST=""
ENDPOINT_READY=""
STACK_READY=""
STOP_FILE=""
STOPPED_FILE=""
CLOCK_SOCKET=""
HEADLESS_RENDERING="false"
MAVPROXY_STREAMRATE=""

usage() {
  printf '%s\n' \
    'usage: actual_sitl_stack_orchestrator.sh --run-dir PATH --run-id ID' \
    '  --runtime-id HEX32 --run-nonce HEX32_OR_HEX64 --profile PROFILE' \
    '  --installed-share PATH --flight-scenario PATH --world-file RELATIVE' \
    '  --manifest PATH --endpoint-ready PATH --stack-ready PATH' \
    '  --stop-file PATH --stopped-file PATH [--clock-socket PATH]' \
    '  [--headless-rendering true|false] [--mavproxy-streamrate -1|1..50]'
}

while (($#)); do
  case "$1" in
    --run-dir) RUN_DIR=$2; shift 2 ;;
    --run-id) RUN_ID=$2; shift 2 ;;
    --runtime-id) RUNTIME_ID=$2; shift 2 ;;
    --run-nonce) RUN_NONCE=$2; shift 2 ;;
    --profile) PROFILE=$2; shift 2 ;;
    --installed-share) INSTALLED_SHARE=$2; shift 2 ;;
    --flight-scenario) FLIGHT_SCENARIO=$2; shift 2 ;;
    --world-file) WORLD_FILE=$2; shift 2 ;;
    --manifest) MANIFEST=$2; shift 2 ;;
    --endpoint-ready) ENDPOINT_READY=$2; shift 2 ;;
    --stack-ready) STACK_READY=$2; shift 2 ;;
    --stop-file) STOP_FILE=$2; shift 2 ;;
    --stopped-file) STOPPED_FILE=$2; shift 2 ;;
    --clock-socket) CLOCK_SOCKET=$2; shift 2 ;;
    --headless-rendering) HEADLESS_RENDERING=$2; shift 2 ;;
    --mavproxy-streamrate) MAVPROXY_STREAMRATE=$2; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'FAIL unknown actual-SITL stack argument: %s\n' "$1" >&2; exit 2 ;;
  esac
done

for value in RUN_DIR RUN_ID RUNTIME_ID RUN_NONCE PROFILE INSTALLED_SHARE \
  FLIGHT_SCENARIO WORLD_FILE MANIFEST ENDPOINT_READY STACK_READY STOP_FILE STOPPED_FILE; do
  [[ -n "${!value}" ]] || { printf 'FAIL required actual-SITL stack argument is empty: %s\n' "$value" >&2; exit 2; }
done
[[ "$RUNTIME_ID" =~ ^[0-9a-f]{32}$ ]] || { printf 'FAIL runtime ID differs\n' >&2; exit 2; }
[[ "$RUN_NONCE" =~ ^([0-9a-f]{32}|[0-9a-f]{64})$ ]] || { printf 'FAIL run nonce differs\n' >&2; exit 2; }
[[ "$PROFILE" == "m3" || "$PROFILE" == "m4_capacity" || "$PROFILE" == "m4_causality" ]] || {
  printf 'FAIL actual-SITL stack profile differs: %s\n' "$PROFILE" >&2
  exit 2
}
[[ "$HEADLESS_RENDERING" == "true" || "$HEADLESS_RENDERING" == "false" ]] || {
  printf 'FAIL headless rendering must be true or false\n' >&2
  exit 2
}
[[ -z "$MAVPROXY_STREAMRATE" || "$MAVPROXY_STREAMRATE" == "-1" || "$MAVPROXY_STREAMRATE" =~ ^([1-9]|[1-4][0-9]|50)$ ]] || {
  printf 'FAIL MAVProxy stream rate must be empty, -1, or an integer in 1..50\n' >&2
  exit 2
}
if [[ "$PROFILE" == m4_* && -z "$CLOCK_SOCKET" ]]; then
  printf 'FAIL M4 actual-SITL stack requires --clock-socket\n' >&2
  exit 2
fi
for command in ip python3 ros2 setsid; do
  command -v "$command" >/dev/null || { printf 'FAIL actual-SITL stack command absent: %s\n' "$command" >&2; exit 2; }
done
for path in "$RUN_DIR" "$INSTALLED_SHARE" "$FLIGHT_SCENARIO" "$ADAPTER" "$ENDPOINT_SUPERVISOR"; do
  [[ -e "$path" ]] || { printf 'FAIL actual-SITL stack path absent: %s\n' "$path" >&2; exit 2; }
done
for namespace in ams-gcs ams-uav1 ams-uav2 ams-uav3 ams-uav4 ams-uav5; do
  ip netns identify "$(ip netns pids "$namespace" 2>/dev/null | head -n1)" >/dev/null 2>&1 || true
  ip -n "$namespace" link show >/dev/null 2>&1 || { printf 'FAIL namespace absent: %s\n' "$namespace" >&2; exit 2; }
done
for index in 1 2 3 4 5; do
  ip -n "ams-uav$index" address show dev tail0 | grep -Fq "10.72.$index.2/30" || {
    printf 'FAIL uav%s /30 tail is absent\n' "$index" >&2
    exit 2
  }
  ip address show dev "ams-tail$index" | grep -Fq "10.72.$index.1/30" || {
    printf 'FAIL root uav%s /30 tail is absent\n' "$index" >&2
    exit 2
  }
done
for path in "$MANIFEST" "$ENDPOINT_READY" "$STACK_READY" "$STOPPED_FILE"; do
  [[ ! -e "$path" && ! -L "$path" ]] || { printf 'FAIL immutable output already exists: %s\n' "$path" >&2; exit 2; }
done

FLIGHT_PID=""
FLIGHT_PGID=""
GAZEBO_PID=""
GAZEBO_START_TICKS=""
SUPERVISOR_PID=""
ADAPTER_PIDS=()
CLEAN_STOP=0

terminate_all() {
  set +e
  [[ -n "$STOP_FILE" && -d "$RUN_DIR" ]] && : > "$STOP_FILE"
  [[ -n "$SUPERVISOR_PID" ]] && kill -TERM -- "-$SUPERVISOR_PID" 2>/dev/null
  for pid in "${ADAPTER_PIDS[@]}"; do kill -TERM -- "-$pid" 2>/dev/null; done
  [[ -n "$FLIGHT_PGID" ]] && kill -TERM -- "-$FLIGHT_PGID" 2>/dev/null
  wait 2>/dev/null
}
trap 'terminate_all' EXIT INT TERM

mkdir -p "$RUN_DIR/runtime" "$RUN_DIR/logs" "$RUN_DIR/raw/state" "$RUN_DIR/raw/actual_sitl"
export AMS_M1_INSTALLED_SHARE="$INSTALLED_SHARE"
export GZ_SIM_RESOURCE_PATH="$INSTALLED_SHARE/models:$INSTALLED_SHARE/worlds:$INSTALLED_SHARE"
export SDF_PATH="$GZ_SIM_RESOURCE_PATH"

(
  cd "$RUN_DIR/runtime"
  exec setsid ros2 launch multiagent_simulation multiagent_simulation.launch.py \
    robots_config_file:="$FLIGHT_SCENARIO" world_file:="$WORLD_FILE" \
    robot_model:=iris_radio_headless enable_serial2:=false \
    generate_sensor_models:=false gui:=false rviz:=false \
    headless_rendering:="$HEADLESS_RENDERING" use_gz_tf:=true \
    mavproxy_streamrate:="$MAVPROXY_STREAMRATE" \
    use_mapping_camera:=false use_navigation_camera:=false use_zed_camera:=false
) > "$RUN_DIR/logs/actual-sitl-flight.stdout" 2> "$RUN_DIR/logs/actual-sitl-flight.stderr" &
FLIGHT_PID=$!
FLIGHT_PGID=$FLIGHT_PID

discover_refs() {
  python3 - "$FLIGHT_PGID" "$1" <<'PY'
import os, re, sys, time
from pathlib import Path
pgid, role = int(sys.argv[1]), sys.argv[2]
deadline = time.monotonic() + 120.0
def ticks(entry):
    raw = (entry / "stat").read_text()
    return int(raw[raw.rfind(")") + 2:].split()[19])
while time.monotonic() < deadline:
    found, duplicates = {}, set()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            pid = int(entry.name)
            if os.getpgid(pid) != pgid:
                continue
            argv = [v.decode(errors="replace") for v in (entry / "cmdline").read_bytes().split(b"\0") if v]
            joined = " ".join(argv)
            if role == "mavproxy":
                if "mavproxy.py" not in joined:
                    continue
                match = re.search(r"tcp:127[.]0[.]0[.]1:(5760|5770|5780|5790|5800)", joined)
                if match is None:
                    continue
                index = (int(match.group(1)) - 5760) // 10
            else:
                if not any(Path(value).name == "arducopter" for value in argv):
                    continue
                match = re.search(r"(?:^|\s)(?:-I\s*|--instance(?:=|\s+))(\d+)(?:\s|$)", joined)
                sysid = re.search(r"(?:^|\s)--sysid(?:=|\s+)([1-5])(?:\s|$)", joined)
                if match is None and sysid is None:
                    continue
                index = int(match.group(1)) if match is not None else int(sysid.group(1)) - 1
            if index in found:
                duplicates.add(index)
            found[index] = (pid, ticks(entry))
        except (OSError, ProcessLookupError, PermissionError, ValueError, IndexError):
            continue
    if not duplicates and set(found) == set(range(5)):
        for index in range(5):
            print(f"uav{index + 1}={found[index][0]}:{found[index][1]}")
        raise SystemExit(0)
    time.sleep(0.2)
raise SystemExit(1)
PY
}

discover_gazebo_ref() {
  python3 - "$FLIGHT_PGID" <<'PY'
import os, sys, time
from pathlib import Path
pgid = int(sys.argv[1])
deadline = time.monotonic() + 120.0
def ticks(entry):
    raw = (entry / "stat").read_text()
    return int(raw[raw.rfind(")") + 2:].split()[19])
while time.monotonic() < deadline:
    matches = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            pid = int(entry.name)
            if os.getpgid(pid) != pgid:
                continue
            argv = [value.decode(errors="replace") for value in (entry / "cmdline").read_bytes().split(b"\0") if value]
            if not any(
                Path(argv[index]).name == "gz" and argv[index + 1] == "sim"
                for index in range(len(argv) - 1)
            ):
                continue
            matches.append((pid, ticks(entry)))
        except (OSError, ProcessLookupError, PermissionError, ValueError, IndexError):
            continue
    if len(matches) == 1:
        print(f"{matches[0][0]}:{matches[0][1]}")
        raise SystemExit(0)
    time.sleep(0.2)
raise SystemExit(1)
PY
}

gazebo_child_alive() {
  local raw tail
  local -a fields
  [[ -n "$GAZEBO_PID" && -n "$GAZEBO_START_TICKS" && -r "/proc/$GAZEBO_PID/stat" ]] || return 1
  raw="$(<"/proc/$GAZEBO_PID/stat")" || return 1
  tail="${raw##*) }"
  read -r -a fields <<< "$tail"
  [[ "${#fields[@]}" -gt 19 && "${fields[19]}" == "$GAZEBO_START_TICKS" ]]
}

required_children_alive() {
  local index pid
  if ! gazebo_child_alive; then
    printf 'FAIL actual-SITL Gazebo flight child exited: pid=%s:start_ticks=%s\n' "$GAZEBO_PID" "$GAZEBO_START_TICKS" >&2
    return 1
  fi
  if ! kill -0 "$FLIGHT_PID" 2>/dev/null; then
    printf 'FAIL actual-SITL launch child exited: pid=%s\n' "$FLIGHT_PID" >&2
    return 1
  fi
  if ! kill -0 "$SUPERVISOR_PID" 2>/dev/null; then
    printf 'FAIL actual-SITL endpoint-supervisor child exited: pid=%s\n' "$SUPERVISOR_PID" >&2
    return 1
  fi
  for index in 1 2 3 4 5; do
    pid="${ADAPTER_PIDS[$((index - 1))]}"
    if ! kill -0 "$pid" 2>/dev/null; then
      printf 'FAIL actual-SITL uav%s adapter child exited: pid=%s\n' "$index" "$pid" >&2
      return 1
    fi
  done
}

GAZEBO_REF="$(discover_gazebo_ref)" || {
  printf 'FAIL exact Gazebo flight process identity was not discovered\n' >&2
  exit 2
}
[[ "$GAZEBO_REF" =~ ^[1-9][0-9]*:[1-9][0-9]*$ ]] || {
  printf 'FAIL Gazebo flight process identity is malformed\n' >&2
  exit 2
}
IFS=: read -r GAZEBO_PID GAZEBO_START_TICKS <<< "$GAZEBO_REF"
mapfile -t SITL_REFS < <(discover_refs sitl)
mapfile -t MAVPROXY_REFS < <(discover_refs mavproxy)
if ! gazebo_child_alive; then
  printf 'FAIL actual-SITL Gazebo flight child exited: pid=%s:start_ticks=%s\n' "$GAZEBO_PID" "$GAZEBO_START_TICKS" >&2
  exit 2
fi
[[ "${#SITL_REFS[@]}" == 5 && "${#MAVPROXY_REFS[@]}" == 5 ]] || {
  printf 'FAIL exact 5+5 flight process identities were not discovered\n' >&2
  exit 2
}

MANIFEST_ARGS=(
  --build-manifest --run-dir "$RUN_DIR" --manifest "$MANIFEST"
  --run-id "$RUN_ID" --runtime-id "$RUNTIME_ID" --run-nonce "$RUN_NONCE"
  --launch-pgid "$FLIGHT_PGID"
)
for ref in "${MAVPROXY_REFS[@]}"; do MANIFEST_ARGS+=(--mavproxy-ref "$ref"); done
for ref in "${SITL_REFS[@]}"; do MANIFEST_ARGS+=(--sitl-ref "$ref"); done
python3 "$ENDPOINT_SUPERVISOR" "${MANIFEST_ARGS[@]}" \
  > "$RUN_DIR/logs/actual-sitl-manifest.stdout" \
  2> "$RUN_DIR/logs/actual-sitl-manifest.stderr"

CLOCK_ARGS=()
[[ -n "$CLOCK_SOCKET" ]] && CLOCK_ARGS=(--clock-socket "$CLOCK_SOCKET")
for index in 1 2 3 4 5; do
  setsid ip netns exec "ams-uav$index" python3 -u "$ADAPTER" \
    --run-dir "$RUN_DIR" --manifest "$MANIFEST" --uav "uav$index" \
    "${CLOCK_ARGS[@]}" \
    > "$RUN_DIR/logs/actual-sitl-uav$index.stdout" \
    2> "$RUN_DIR/logs/actual-sitl-uav$index.stderr" &
  ADAPTER_PIDS+=("$!")
done
setsid python3 -u "$ENDPOINT_SUPERVISOR" \
  --run-dir "$RUN_DIR" --manifest "$MANIFEST" \
  --ready-file "$ENDPOINT_READY" --stop-file "$STOP_FILE" \
  "${CLOCK_ARGS[@]}" \
  > "$RUN_DIR/logs/actual-sitl-supervisor.stdout" \
  2> "$RUN_DIR/logs/actual-sitl-supervisor.stderr" &
SUPERVISOR_PID=$!

deadline=$((SECONDS + 90))
while [[ ! -s "$ENDPOINT_READY" ]]; do
  required_children_alive || exit 2
  ((SECONDS < deadline)) || { printf 'FAIL actual-SITL aggregate readiness timeout\n' >&2; exit 2; }
  sleep 0.1
done

python3 - "$STACK_READY" "$RUN_ID" "$RUNTIME_ID" "$RUN_NONCE" "$PROFILE" \
  "$MANIFEST" "$ENDPOINT_READY" "$FLIGHT_PGID" "$SUPERVISOR_PID" \
  "${ADAPTER_PIDS[@]}" <<'PY'
import hashlib, json, os, sys, time
from pathlib import Path
out, run_id, runtime_id, nonce, profile, manifest, endpoint = map(str, sys.argv[1:8])
groups = [int(value) for value in sys.argv[8:]]
value = {
    "contract": "ams.actual-sitl-stack-ready/v1",
    "run_id": run_id,
    "runtime_id": runtime_id,
    "run_nonce": nonce,
    "profile": profile,
    "manifest_path": str(Path(manifest).resolve()),
    "manifest_sha256": hashlib.sha256(Path(manifest).read_bytes()).hexdigest(),
    "endpoint_ready_path": str(Path(endpoint).resolve()),
    "endpoint_ready_sha256": hashlib.sha256(Path(endpoint).read_bytes()).hexdigest(),
    "process_groups": groups,
    "ready_monotonic_ns": time.monotonic_ns(),
}
path = Path(out)
path.parent.mkdir(parents=True, exist_ok=True)
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o664)
with os.fdopen(fd, "wb") as stream:
    stream.write((json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode())
    stream.flush(); os.fsync(stream.fileno())
PY

while [[ ! -e "$STOP_FILE" ]]; do
  required_children_alive || exit 2
  sleep 0.25
done

set +e
wait "$SUPERVISOR_PID"; SUPERVISOR_RC=$?
for pid in "${ADAPTER_PIDS[@]}"; do kill -TERM -- "-$pid" 2>/dev/null; done
ADAPTER_RC=0
for pid in "${ADAPTER_PIDS[@]}"; do wait "$pid" || ADAPTER_RC=$?; done
kill -TERM -- "-$FLIGHT_PGID" 2>/dev/null
wait "$FLIGHT_PID"; FLIGHT_RC=$?
set -e
[[ "$SUPERVISOR_RC" == 0 && "$ADAPTER_RC" == 0 ]] || {
  printf 'FAIL actual-SITL shutdown rc supervisor=%s adapter=%s\n' "$SUPERVISOR_RC" "$ADAPTER_RC" >&2
  exit 2
}
[[ "$FLIGHT_RC" == 0 || "$FLIGHT_RC" == 130 || "$FLIGHT_RC" == 143 ]] || {
  printf 'FAIL actual-SITL flight shutdown rc=%s\n' "$FLIGHT_RC" >&2
  exit 2
}

python3 - "$STOPPED_FILE" "$RUN_ID" "$RUNTIME_ID" "$PROFILE" "$SUPERVISOR_RC" "$ADAPTER_RC" "$FLIGHT_RC" <<'PY'
import json, os, sys, time
from pathlib import Path
path = Path(sys.argv[1])
value = {
    "contract": "ams.actual-sitl-stack-stopped/v1",
    "run_id": sys.argv[2], "runtime_id": sys.argv[3], "profile": sys.argv[4],
    "supervisor_exit_code": int(sys.argv[5]), "adapter_exit_code": int(sys.argv[6]),
    "flight_exit_code": int(sys.argv[7]), "stopped_monotonic_ns": time.monotonic_ns(),
}
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o664)
with os.fdopen(fd, "wb") as stream:
    stream.write((json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode())
    stream.flush(); os.fsync(stream.fileno())
PY
CLEAN_STOP=1
trap - EXIT INT TERM
exit 0
