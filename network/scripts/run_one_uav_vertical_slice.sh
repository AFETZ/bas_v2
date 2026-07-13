#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUN_ID="${RUN_ID:-m2_one_uav_$(date -u +%Y%m%dT%H%M%SZ)}"
RUN_DIR="${RUN_DIR:-$ROOT_DIR/runs/$RUN_ID}"
RUNTIME_DIR="$RUN_DIR/runtime"
SCENARIO="${SCENARIO:-$ROOT_DIR/network/config/scenario_1uav_vertical_slice.yaml}"
ROBOT_MODEL="${ROBOT_MODEL:-iris_radio_headless}"
RUNTIME_ID="${AMS_RUNTIME_ID:-m2-$(python3 -c 'import uuid; print(uuid.uuid4())')}"
RUN_NONCE="${AMS_RUN_NONCE:-m2n-$(python3 -c 'import secrets; print(secrets.token_hex(12))')}"
ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-$((20 + $(printf '%s' "$RUN_ID" | cksum | awk '{print $1}') % 180))}"
GZ_PARTITION="${GZ_PARTITION:-ams_${RUN_ID//[^a-zA-Z0-9_]/_}}"
GCS_NS="${GCS_NS:-ams-gcs}"
NS3_NS="${NS3_NS:-ams-ns3}"
UAV_NS="${UAV_NS:-ams-uav1}"
TAIL_ROOT="${TAIL_ROOT:-ams-tail0}"
GOOD_ATTEMPTS=10
DOWN_ATTEMPTS=5
RECOVERY_ATTEMPTS=10
STARTUP_TIMEOUT_S="${STARTUP_TIMEOUT_S:-90}"
HEARTBEAT_TIMEOUT_S="${HEARTBEAT_TIMEOUT_S:-20}"
ACK_TIMEOUT_S="${ACK_TIMEOUT_S:-3}"
DOWN_HEARTBEAT_TIMEOUT_S="${DOWN_HEARTBEAT_TIMEOUT_S:-5}"
DOWN_ACK_TIMEOUT_S="${DOWN_ACK_TIMEOUT_S:-2}"
DOWN_SETTLE_S="${DOWN_SETTLE_S:-1}"
CAPTURE_DRAIN_S="${CAPTURE_DRAIN_S:-2}"
HEADLESS_RENDERING="${HEADLESS_RENDERING:-false}"
NS3_DIR="${NS3_DIR:-$ROOT_DIR/.external/ns-3}"
NS3_BINARY="$NS3_DIR/build/scratch/ns3.40-ams-tap-vertical-slice-default"
NS3_RECEIPT_TOOL="$ROOT_DIR/network/ns3/ns3_build_receipt.py"
NS3_PROJECT_SOURCE="$ROOT_DIR/network/ns3/scratch/ams-tap-vertical-slice.cc"
NS3_COPIED_SOURCE="$NS3_DIR/scratch/ams-tap-vertical-slice.cc"
NS3_REQUIRED_MODULES="applications,bridge,core,csma,flow-monitor,internet,mobility,network,stats,tap-bridge,traffic-control"
PROBE="$ROOT_DIR/network/tests/mavlink_vertical_slice_probe.py"
PROCESS_IDENTITY="$RUN_DIR/logs/m2_process_identity.json"
PROBE_EVENTS="$RUN_DIR/logs/m2_probe_events.jsonl"
PROCESS_EVENTS="$RUN_DIR/logs/m2_process_events.jsonl"
ADAPTER_EVENTS="$RUN_DIR/logs/uav_adapter.jsonl"
STARTED_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

if [[ ! "$RUN_ID" =~ ^[a-zA-Z0-9_.-]+$ ]]; then
  printf 'FAIL RUN_ID contains unsafe characters: %s\n' "$RUN_ID" >&2
  exit 2
fi
if [[ "$(basename "$RUN_DIR")" != "$RUN_ID" ]]; then
  printf 'FAIL RUN_DIR basename must equal RUN_ID (%s): %s\n' "$RUN_ID" "$RUN_DIR" >&2
  exit 2
fi
if [[ -e "$RUN_DIR" ]]; then
  printf 'FAIL immutable M2 run directory already exists: %s\n' "$RUN_DIR" >&2
  exit 1
fi
if [[ ! -f "$SCENARIO" ]]; then
  printf 'FAIL M2 scenario is missing: %s\n' "$SCENARIO" >&2
  exit 2
fi

for command in bash colcon getent ip mavproxy.py python3 ros2 setsid sudo tcpdump; do
  if ! command -v "$command" >/dev/null 2>&1; then
    printf 'FAIL required M2 command is missing: %s\n' "$command" >&2
    exit 2
  fi
done
if ! sudo -n true >/dev/null 2>&1; then
  printf 'FAIL passwordless sudo is required for isolated namespaces and capture\n' >&2
  exit 2
fi
if ! getent hosts "$(hostname)" >/dev/null 2>&1; then
  printf '127.0.1.1 %s\n' "$(hostname)" | sudo tee -a /etc/hosts >/dev/null
fi
if [[ ! -c /dev/net/tun ]]; then
  printf 'FAIL /dev/net/tun is missing; run inside the privileged project container\n' >&2
  exit 2
fi

if [[ "${M2_SKIP_BUILDS:-0}" != "1" ]]; then
  (
    cd "$ROOT_DIR"
    colcon build --packages-select multiagent_simulation --symlink-install
  )
  NS3_DIR="$NS3_DIR" "$ROOT_DIR/network/ns3/build_ns3_tap.sh"
fi
if [[ ! -f "$NS3_DIR/VERSION" ]] || \
   [[ "$(tr -d '[:space:]' < "$NS3_DIR/VERSION")" != "3.40" ]]; then
  printf 'FAIL M2 requires the pinned ns-3 VERSION 3.40 tree: %s\n' "$NS3_DIR" >&2
  exit 2
fi
if [[ ! -x "$NS3_BINARY" ]]; then
  printf 'FAIL ns-3 TapBridge binary is missing: %s\n' "$NS3_BINARY" >&2
  printf 'Run network/ns3/build_ns3_tap.sh inside /workspace/multiagent_simulation.\n' >&2
  exit 2
fi

mkdir -p "$(dirname "$RUN_DIR")"
mkdir "$RUN_DIR"
mkdir "$RUN_DIR/logs" "$RUN_DIR/metrics" "$RUN_DIR/pcap" "$RUNTIME_DIR"
RUNNER_LOG="$RUN_DIR/logs/m2_runner.log"
: > "$RUNNER_LOG"

runner_log() {
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "$RUNNER_LOG"
}

# This verification is outside the optional build block by design: a skipped
# build is never evidence that the existing executable matches current source.
python3 "$NS3_RECEIPT_TOOL" verify \
  --ns3-dir "$NS3_DIR" \
  --program ams-tap-vertical-slice \
  --project-source "$NS3_PROJECT_SOURCE" \
  --copied-source "$NS3_COPIED_SOURCE" \
  --executable "$NS3_BINARY" \
  --required-modules "$NS3_REQUIRED_MODULES" \
  --copy-to "$RUN_DIR/metrics/ns3_tap_build_receipt.json" \
  > "$RUN_DIR/logs/ns3_build_receipt.log"
runner_log "verified current ns-3 TapBridge build receipt"

printf '%q ' "$0" "$@" > "$RUN_DIR/command.txt"
printf '\n' >> "$RUN_DIR/command.txt"
{
  printf 'run_id=%s\n' "$RUN_ID"
  printf 'runtime_id=%s\n' "$RUNTIME_ID"
  printf 'run_nonce=%s\n' "$RUN_NONCE"
  printf 'utc=%s\n' "$STARTED_UTC"
  printf 'scenario=%s\n' "$SCENARIO"
  printf 'robot_model=%s\n' "$ROBOT_MODEL"
  printf 'ros_domain_id=%s\n' "$ROS_DOMAIN_ID"
  printf 'gz_partition=%s\n' "$GZ_PARTITION"
  printf 'gcs_namespace=%s\n' "$GCS_NS"
  printf 'ns3_namespace=%s\n' "$NS3_NS"
  printf 'uav_namespace=%s\n' "$UAV_NS"
  printf 'component_only=true\n'
  printf 'p0_eligible=false\n'
} > "$RUN_DIR/environment.txt"

export AMS_RUNTIME_ID="$RUNTIME_ID"
export AMS_RUN_NONCE="$RUN_NONCE"
export ROS_DOMAIN_ID
export GZ_PARTITION

python3 "$ROOT_DIR/network/scripts/write_run_provenance.py" \
  --run-dir "$RUN_DIR" \
  --config "$ROOT_DIR/doc/network_radio_integration_plan_v2.md" \
  --config "$SCENARIO" \
  --config "$ROOT_DIR/network/config/endpoints.yaml" \
  --config "$ROOT_DIR/network/config/radio_backend.yaml" \
  --config "$ROOT_DIR/network/config/dependency_lock.yaml" \
  --config "$ROOT_DIR/network/config/validation_matrix.yaml" \
  --packet-ingress-mode tap_bridge_external \
  --medium-model csma_surrogate \
  --radio-provider-id not_used_m2 \
  > "$RUN_DIR/logs/provenance.log" 2>&1
SOURCE_HASH="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["source_hash"])' "$RUN_DIR/metrics/provenance.json")"
runner_log "M2 runtime initialized run_id=$RUN_ID runtime_id=$RUNTIME_ID"

declare -A CAPTURE_PID=()
declare -A CAPTURE_PGID=()
declare -A CAPTURE_FILE=()
declare -A CAPTURE_LOG=()
NAMESPACES_OWNED=0
LAUNCH_PID=""
LAUNCH_PGID=""
ADAPTER_WRAPPER_PID=""
ADAPTER_PGID=""
ADAPTER_ACTUAL_PID=""
CURRENT_NS3_WRAPPER_PID=""
CURRENT_NS3_PGID=""
CURRENT_NS3_ACTUAL_PID=""
CURRENT_NS3_PHASE=""
CLEANUP_ACTIVE=0

pid_alive() {
  local pid="${1:-}"
  [[ -n "$pid" ]] || return 1
  local state
  state="$(python3 - "$pid" <<'PY' 2>/dev/null || true
import sys
from pathlib import Path
raw = Path(f"/proc/{int(sys.argv[1])}/stat").read_text(encoding="utf-8")
print(raw[raw.rfind(")") + 2:].split()[0])
PY
)"
  [[ -n "$state" && "$state" != "Z" ]] && sudo kill -0 "$pid" >/dev/null 2>&1
}

wait_pid_dead() {
  local pid="$1"
  local iterations="${2:-100}"
  local index
  for ((index = 0; index < iterations; index++)); do
    if ! pid_alive "$pid"; then
      return 0
    fi
    sleep 0.1
  done
  return 1
}

stop_capture() {
  local key="$1"
  local pid="${CAPTURE_PID[$key]:-}"
  local pgid="${CAPTURE_PGID[$key]:-}"
  [[ -n "$pid" ]] || return 0
  sudo kill -INT -- "-$pgid" >/dev/null 2>&1 || true
  if ! wait_pid_dead "$pid" 50; then
    sudo kill -TERM -- "-$pgid" >/dev/null 2>&1 || true
    wait_pid_dead "$pid" 20 || sudo kill -KILL -- "-$pgid" >/dev/null 2>&1 || true
  fi
  wait "$pid" >/dev/null 2>&1 || true
  CAPTURE_PID[$key]=""
  if [[ ! -s "${CAPTURE_FILE[$key]}" ]]; then
    printf 'FAIL capture is missing or empty: %s\n' "${CAPTURE_FILE[$key]}" >&2
    return 1
  fi
}

start_capture() {
  local key="$1"
  local namespace="$2"
  local interface="$3"
  local output="$4"
  local filter="$5"
  local log="$RUN_DIR/logs/tcpdump_${key}.log"
  if [[ -e "$output" ]]; then
    printf 'FAIL refusing to overwrite capture: %s\n' "$output" >&2
    return 1
  fi
  if [[ "$namespace" == "root" ]]; then
    setsid sudo tcpdump -i "$interface" -U -n -s 0 -w "$output" "$filter" > "$log" 2>&1 &
  else
    setsid sudo ip netns exec "$namespace" \
      tcpdump -i "$interface" -U -n -s 0 -w "$output" "$filter" > "$log" 2>&1 &
  fi
  local pid=$!
  CAPTURE_PID[$key]="$pid"
  CAPTURE_PGID[$key]="$pid"
  CAPTURE_FILE[$key]="$output"
  CAPTURE_LOG[$key]="$log"
  sleep 0.5
  if ! pid_alive "$pid"; then
    wait "$pid" >/dev/null 2>&1 || true
    printf 'FAIL tcpdump exited for %s; see %s\n' "$key" "$log" >&2
    return 1
  fi
}

stop_ns3_without_artifact_checks() {
  local wrapper="$CURRENT_NS3_WRAPPER_PID"
  local actual="$CURRENT_NS3_ACTUAL_PID"
  local pgid="$CURRENT_NS3_PGID"
  local phase="$CURRENT_NS3_PHASE"
  [[ -n "$wrapper" ]] || return 0
  if [[ -n "$phase" ]]; then
    : > "$RUN_DIR/logs/ns3_${phase}.stop"
  fi
  if [[ -n "$actual" ]] && ! wait_pid_dead "$actual" 100; then
    sudo kill -TERM "$actual" >/dev/null 2>&1 || true
    wait_pid_dead "$actual" 30 || sudo kill -KILL "$actual" >/dev/null 2>&1 || true
  fi
  if ! wait_pid_dead "$wrapper" 30; then
    sudo kill -TERM -- "-$pgid" >/dev/null 2>&1 || true
    wait_pid_dead "$wrapper" 20 || sudo kill -KILL -- "-$pgid" >/dev/null 2>&1 || true
  fi
  wait "$wrapper" >/dev/null 2>&1 || true
  CURRENT_NS3_WRAPPER_PID=""
  CURRENT_NS3_PGID=""
  CURRENT_NS3_ACTUAL_PID=""
  CURRENT_NS3_PHASE=""
}

stop_adapter() {
  [[ -n "$ADAPTER_WRAPPER_PID" ]] || return 0
  if [[ -n "$ADAPTER_ACTUAL_PID" ]] && pid_alive "$ADAPTER_ACTUAL_PID"; then
    sudo kill -TERM "$ADAPTER_ACTUAL_PID" >/dev/null 2>&1 || true
    if ! wait_pid_dead "$ADAPTER_ACTUAL_PID" 80; then
      sudo kill -KILL "$ADAPTER_ACTUAL_PID" >/dev/null 2>&1 || true
      wait_pid_dead "$ADAPTER_ACTUAL_PID" 20 || true
    fi
  fi
  if ! wait_pid_dead "$ADAPTER_WRAPPER_PID" 30; then
    sudo kill -TERM -- "-$ADAPTER_PGID" >/dev/null 2>&1 || true
    wait_pid_dead "$ADAPTER_WRAPPER_PID" 20 || \
      sudo kill -KILL -- "-$ADAPTER_PGID" >/dev/null 2>&1 || true
  fi
  wait "$ADAPTER_WRAPPER_PID" >/dev/null 2>&1 || true
  ADAPTER_WRAPPER_PID=""
}

stop_launch() {
  [[ -n "$LAUNCH_PID" ]] || return 0
  kill -TERM -- "-$LAUNCH_PGID" >/dev/null 2>&1 || true
  if ! wait_pid_dead "$LAUNCH_PID" 100; then
    sudo kill -KILL -- "-$LAUNCH_PGID" >/dev/null 2>&1 || true
    wait_pid_dead "$LAUNCH_PID" 20 || true
  fi
  wait "$LAUNCH_PID" >/dev/null 2>&1 || true
  LAUNCH_PID=""
}

cleanup() {
  if (( CLEANUP_ACTIVE )); then
    return
  fi
  CLEANUP_ACTIVE=1
  set +e
  stop_ns3_without_artifact_checks
  local key
  for key in "${!CAPTURE_PID[@]}"; do
    [[ -n "${CAPTURE_PID[$key]:-}" ]] && stop_capture "$key"
  done
  stop_adapter
  stop_launch
  if (( NAMESPACES_OWNED )); then
    GCS_NS="$GCS_NS" NS3_NS="$NS3_NS" UAV_NS="$UAV_NS" TAIL_ROOT="$TAIL_ROOT" \
      "$ROOT_DIR/network/scripts/setup_one_uav_netns.sh" down
    NAMESPACES_OWNED=0
  fi
  set -e
}
trap cleanup EXIT
trap 'exit 130' INT TERM

for namespace in "$GCS_NS" "$NS3_NS" "$UAV_NS"; do
  if ip netns list | awk '{print $1}' | grep -Fxq "$namespace"; then
    printf 'FAIL namespace already exists before M2 run: %s\n' "$namespace" >&2
    exit 1
  fi
done

GCS_NS="$GCS_NS" NS3_NS="$NS3_NS" UAV_NS="$UAV_NS" TAIL_ROOT="$TAIL_ROOT" \
  "$ROOT_DIR/network/scripts/setup_one_uav_netns.sh" up \
  > "$RUN_DIR/logs/netns_setup.log" 2>&1
NAMESPACES_OWNED=1
GCS_NS="$GCS_NS" NS3_NS="$NS3_NS" UAV_NS="$UAV_NS" TAIL_ROOT="$TAIL_ROOT" \
  "$ROOT_DIR/network/scripts/setup_one_uav_netns.sh" status \
  > "$RUN_DIR/logs/netns_status.log" 2>&1
runner_log "namespace topology ready"

python3 - "$RUN_DIR/logs/m2_netns_snapshot.json" "$RUN_ID" "$RUNTIME_ID" "$RUN_NONCE" \
  "$GCS_NS" "$NS3_NS" "$UAV_NS" <<'PY'
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

output, run_id, runtime_id, nonce, *namespaces = sys.argv[1:]
data = {
    "schema_version": 2,
    "run_id": run_id,
    "runtime_id": runtime_id,
    "run_nonce": nonce,
    "wall_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "namespaces": {},
}
for namespace in namespaces:
    record = {}
    for kind in ("link", "address", "route"):
        command = ["sudo", "ip", "-j", "-n", namespace, kind, "show"]
        result = subprocess.run(command, check=True, capture_output=True, text=True)
        record[kind] = json.loads(result.stdout)
    data["namespaces"][namespace] = record
Path(output).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

start_capture tail root "$TAIL_ROOT" "$RUN_DIR/pcap/uav_tail.pcap" 'udp port 14560'

ADAPTER_READY="$RUN_DIR/logs/uav_adapter.ready"
setsid sudo ip netns exec "$UAV_NS" sudo -u "$(id -un)" env PYTHONUNBUFFERED=1 \
  python3 "$ROOT_DIR/network/bridge/uav_mavlink_endpoint.py" \
  --event-log "$ADAPTER_EVENTS" \
  --ready-file "$ADAPTER_READY" \
  --run-id "$RUN_ID" \
  --runtime-id "$RUNTIME_ID" \
  --run-nonce "$RUN_NONCE" \
  > "$RUN_DIR/logs/uav_adapter_stdout.log" 2>&1 &
ADAPTER_WRAPPER_PID=$!
ADAPTER_PGID=$ADAPTER_WRAPPER_PID

for ((i = 0; i < STARTUP_TIMEOUT_S * 10; i++)); do
  if [[ -s "$ADAPTER_READY" ]]; then
    ADAPTER_ACTUAL_PID="$(tr -dc '0-9' < "$ADAPTER_READY")"
    [[ -n "$ADAPTER_ACTUAL_PID" ]] && pid_alive "$ADAPTER_ACTUAL_PID" && break
  fi
  if ! pid_alive "$ADAPTER_WRAPPER_PID"; then
    printf 'FAIL UAV adapter wrapper exited; see logs/uav_adapter_stdout.log\n' >&2
    exit 1
  fi
  sleep 0.1
done
if [[ -z "$ADAPTER_ACTUAL_PID" ]] || ! pid_alive "$ADAPTER_ACTUAL_PID"; then
  printf 'FAIL UAV adapter did not become ready\n' >&2
  exit 1
fi
runner_log "UAV adapter ready pid=$ADAPTER_ACTUAL_PID"

(
  cd "$RUNTIME_DIR"
  exec setsid ros2 launch multiagent_simulation multiagent_simulation.launch.py \
    robots_config_file:="$SCENARIO" \
    robot_model:="$ROBOT_MODEL" \
    gui:=false rviz:=false headless_rendering:="$HEADLESS_RENDERING" \
    use_mapping_camera:=false \
    use_navigation_camera:=false \
    use_zed_camera:=false
) > "$RUN_DIR/logs/m2_launch.log" 2>&1 &
LAUNCH_PID=$!
LAUNCH_PGID=$LAUNCH_PID
printf '%s\n' "$LAUNCH_PID" > "$RUN_DIR/logs/m2_launch.pid"

find_group_process() {
  local pgid="$1"
  local pattern="$2"
  local timeout_s="$3"
  python3 - "$pgid" "$pattern" "$timeout_s" <<'PY'
import os
import re
import sys
import time
from pathlib import Path

pgid = int(sys.argv[1])
pattern = re.compile(sys.argv[2])
deadline = time.monotonic() + float(sys.argv[3])
while time.monotonic() < deadline:
    matches = []
    for item in Path("/proc").iterdir():
        if not item.name.isdigit():
            continue
        pid = int(item.name)
        try:
            if os.getpgid(pid) != pgid:
                continue
            command = (item / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except (OSError, ProcessLookupError, PermissionError):
            continue
        if pattern.search(command):
            matches.append(pid)
    if len(matches) == 1:
        print(matches[0])
        raise SystemExit(0)
    if len(matches) > 1:
        print(f"multiple matching processes: {matches}", file=sys.stderr)
        raise SystemExit(2)
    time.sleep(0.2)
raise SystemExit(1)
PY
}

if ! pid_alive "$LAUNCH_PID"; then
  printf 'FAIL ROS/Gazebo/SITL launch exited immediately\n' >&2
  exit 1
fi
SITL_PID="$(find_group_process "$LAUNCH_PGID" '(^|/)arducopter(\s|$)' "$STARTUP_TIMEOUT_S")" || {
  printf 'FAIL could not identify stable arducopter PID; see logs/m2_launch.log\n' >&2
  exit 1
}
MAVPROXY_PID="$(find_group_process "$LAUNCH_PGID" 'mavproxy[.]py' "$STARTUP_TIMEOUT_S")" || {
  printf 'FAIL could not identify stable MAVProxy PID; see logs/m2_launch.log\n' >&2
  exit 1
}

python3 - "$PROCESS_IDENTITY" "$RUN_ID" "$RUNTIME_ID" "$RUN_NONCE" "$SOURCE_HASH" \
  "launch=$LAUNCH_PID" "sitl=$SITL_PID" "mavproxy=$MAVPROXY_PID" \
  "adapter=$ADAPTER_ACTUAL_PID" <<'PY'
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

output, run_id, runtime_id, nonce, source_hash, *entries = sys.argv[1:]

def identity(role, pid):
    raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    rest = raw[raw.rfind(")") + 2:].split()
    command = Path(f"/proc/{pid}/cmdline").read_bytes()
    return {
        "role": role,
        "pid": pid,
        "start_ticks": int(rest[19]),
        "cmdline_sha256": hashlib.sha256(command).hexdigest(),
    }

processes = []
for entry in entries:
    role, pid_text = entry.split("=", 1)
    processes.append(identity(role, int(pid_text)))
data = {
    "schema_version": 2,
    "contract": "ams.m2.vertical_slice.process_identity/v1",
    "run_id": run_id,
    "runtime_id": runtime_id,
    "run_nonce": nonce,
    "source_hash": source_hash,
    "recorded_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "processes": processes,
}
Path(output).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

process_reference() {
  python3 - "$1" <<'PY'
import hashlib
import sys
from pathlib import Path
pid = int(sys.argv[1])
raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
command = Path(f"/proc/{pid}/cmdline").read_bytes()
print(f"{pid}:{int(raw[raw.rfind(')') + 2:].split()[19])}:{hashlib.sha256(command).hexdigest()}")
PY
}

find_ns3_process() {
  local matches=()
  local pid
  while read -r pid; do
    [[ -n "$pid" ]] || continue
    if [[ "$(readlink "/proc/$pid/exe" 2>/dev/null || true)" == "$NS3_BINARY" ]]; then
      matches+=("$pid")
    fi
  done < <(sudo ip netns pids "$NS3_NS")
  if (( ${#matches[@]} != 1 )); then
    printf 'FAIL expected one ns-3 vertical-slice PID, found %s\n' "${matches[*]:-none}" >&2
    return 1
  fi
  printf '%s\n' "${matches[0]}"
}

start_ns3() {
  local phase="$1"
  # Re-hash the source, CMake configuration and executable immediately before
  # each runtime process.  This rejects changes after initial run setup.
  python3 "$NS3_RECEIPT_TOOL" verify \
    --ns3-dir "$NS3_DIR" \
    --program ams-tap-vertical-slice \
    --project-source "$NS3_PROJECT_SOURCE" \
    --copied-source "$NS3_COPIED_SOURCE" \
    --executable "$NS3_BINARY" \
    --required-modules "$NS3_REQUIRED_MODULES" \
    >> "$RUN_DIR/logs/ns3_build_receipt.log"
  PHASE="$phase" RUN_DIR="$RUN_DIR" NS3_NS="$NS3_NS" NS3_DIR="$NS3_DIR" \
    setsid "$ROOT_DIR/network/ns3/run_ns3_tap_slice.sh" \
    > "$RUN_DIR/logs/ns3_${phase}.log" 2>&1 &
  CURRENT_NS3_WRAPPER_PID=$!
  CURRENT_NS3_PGID=$CURRENT_NS3_WRAPPER_PID
  CURRENT_NS3_PHASE="$phase"
  local ready="$RUN_DIR/logs/ns3_${phase}.ready"
  local i
  for ((i = 0; i < 300; i++)); do
    [[ -s "$ready" ]] && break
    if ! pid_alive "$CURRENT_NS3_WRAPPER_PID"; then
      printf 'FAIL ns-3 %s exited before readiness; see logs/ns3_%s.log\n' "$phase" "$phase" >&2
      return 1
    fi
    sleep 0.1
  done
  if [[ ! -s "$ready" ]]; then
    printf 'FAIL ns-3 %s readiness timeout\n' "$phase" >&2
    return 1
  fi
  CURRENT_NS3_ACTUAL_PID="$(find_ns3_process)"
  pid_alive "$CURRENT_NS3_ACTUAL_PID"
}

normalize_ns3_pcaps() {
  local phase="$1"
  local mapping=(
    "ns3_${phase}-gcs.pcap:ns3_ingress_${phase}.pcap"
    "ns3_${phase}-router-gcs.pcap:ns3_core_gcs_${phase}.pcap"
    "ns3_${phase}-router-uav.pcap:ns3_core_uav_${phase}.pcap"
    "ns3_${phase}-uav.pcap:ns3_egress_${phase}.pcap"
  )
  local entry source destination
  for entry in "${mapping[@]}"; do
    source="$RUN_DIR/pcap/${entry%%:*}"
    destination="$RUN_DIR/pcap/${entry##*:}"
    if [[ ! -s "$source" || -e "$destination" ]]; then
      printf 'FAIL cannot normalize real ns-3 capture %s -> %s\n' "$source" "$destination" >&2
      return 1
    fi
    mv "$source" "$destination"
  done
}

stop_ns3_phase() {
  local phase="$1"
  [[ "$CURRENT_NS3_PHASE" == "$phase" ]]
  stop_ns3_without_artifact_checks
  normalize_ns3_pcaps "$phase"
}

run_probe_phase() {
  local phase="$1"
  local attempts="$2"
  local expected_ack="$3"
  local expected_ns3_state="$4"
  local heartbeat_timeout="$5"
  local ack_timeout="$6"
  shift 6
  local extra=("$@")
  local command=(
    python3 "$PROBE"
    --phase "$phase"
    --attempts "$attempts"
    --expected-ack "$expected_ack"
    --run-id "$RUN_ID"
    --runtime-id "$RUNTIME_ID"
    --run-nonce "$RUN_NONCE"
    --event-log "$PROBE_EVENTS"
    --process-event-log "$PROCESS_EVENTS"
    --process-identity "$PROCESS_IDENTITY"
    --phase-summary "$RUN_DIR/metrics/m2_phase_${phase}.json"
    --heartbeat-timeout-s "$heartbeat_timeout"
    --ack-timeout-s "$ack_timeout"
    --expected-ns3-state "$expected_ns3_state"
    --forbidden-endpoint 127.0.0.1:5760
    --forbidden-endpoint 10.72.1.1:5760
    "${extra[@]}"
  )
  if ! sudo ip netns exec "$GCS_NS" sudo -u "$(id -un)" env PYTHONUNBUFFERED=1 \
    "${command[@]}" > "$RUN_DIR/logs/probe_${phase}.log" 2>&1; then
    cat "$RUN_DIR/logs/probe_${phase}.log" >&2
    return 1
  fi
}

start_phase_captures() {
  local phase="$1"
  start_capture "gcs_${phase}" "$GCS_NS" eth0 \
    "$RUN_DIR/pcap/gcs_ingress_${phase}.pcap" \
    'arp or (udp and (port 14600 or port 14601))'
  start_capture "uav_${phase}" "$UAV_NS" eth0 \
    "$RUN_DIR/pcap/uav_egress_${phase}.pcap" \
    'arp or (udp and (port 14600 or port 14601))'
}

stop_phase_captures() {
  local phase="$1"
  stop_capture "gcs_${phase}"
  stop_capture "uav_${phase}"
}

start_phase_captures good
start_ns3 good
GOOD_NS3_REFERENCE="$(process_reference "$CURRENT_NS3_ACTUAL_PID")"
runner_log "phase good started ns3_pid=$CURRENT_NS3_ACTUAL_PID"
run_probe_phase good "$GOOD_ATTEMPTS" true up "$HEARTBEAT_TIMEOUT_S" "$ACK_TIMEOUT_S" \
  --ns3-process "$GOOD_NS3_REFERENCE"
sleep "$CAPTURE_DRAIN_S"
stop_ns3_phase good
stop_phase_captures good
runner_log "phase good complete and ns-3 stopped"

sleep "$DOWN_SETTLE_S"
start_phase_captures down
runner_log "phase down started with prior_ns3=$GOOD_NS3_REFERENCE"
run_probe_phase down "$DOWN_ATTEMPTS" false down "$DOWN_HEARTBEAT_TIMEOUT_S" "$DOWN_ACK_TIMEOUT_S" \
  --absent-process "$GOOD_NS3_REFERENCE"
sleep "$CAPTURE_DRAIN_S"
stop_phase_captures down
runner_log "phase down complete"

start_phase_captures recovery
start_ns3 recovery
RECOVERY_NS3_REFERENCE="$(process_reference "$CURRENT_NS3_ACTUAL_PID")"
runner_log "phase recovery started ns3_pid=$CURRENT_NS3_ACTUAL_PID"
run_probe_phase recovery "$RECOVERY_ATTEMPTS" true up "$HEARTBEAT_TIMEOUT_S" "$ACK_TIMEOUT_S" \
  --ns3-process "$RECOVERY_NS3_REFERENCE" \
  --absent-process "$GOOD_NS3_REFERENCE"
sleep "$CAPTURE_DRAIN_S"
stop_ns3_phase recovery
stop_phase_captures recovery
runner_log "phase recovery complete and ns-3 stopped"

# Stop all live producers before hashing the immutable raw evidence.
stop_adapter
stop_launch
stop_capture tail
GCS_NS="$GCS_NS" NS3_NS="$NS3_NS" UAV_NS="$UAV_NS" TAIL_ROOT="$TAIL_ROOT" \
  "$ROOT_DIR/network/scripts/setup_one_uav_netns.sh" down \
  > "$RUN_DIR/logs/netns_teardown.log" 2>&1
NAMESPACES_OWNED=0
runner_log "all runtime producers stopped; sealing raw evidence"

ENDED_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
python3 - "$RUN_DIR/metrics/m2_run.json" "$RUN_ID" "$RUNTIME_ID" "$RUN_NONCE" \
  "$SOURCE_HASH" "$STARTED_UTC" "$ENDED_UTC" "$PROCESS_IDENTITY" \
  "$RUN_DIR/metrics/m2_phase_good.json" "$RUN_DIR/metrics/m2_phase_down.json" \
  "$RUN_DIR/metrics/m2_phase_recovery.json" <<'PY'
import json
import sys
from pathlib import Path

(
    output,
    run_id,
    runtime_id,
    nonce,
    source_hash,
    started_utc,
    ended_utc,
    process_identity_path,
    *phase_paths,
) = sys.argv[1:]
phases = {}
for value in phase_paths:
    record = json.loads(Path(value).read_text(encoding="utf-8"))
    if (record.get("run_id"), record.get("runtime_id"), record.get("run_nonce")) != (
        run_id,
        runtime_id,
        nonce,
    ):
        raise SystemExit(f"mixed phase identity: {value}")
    phases[record["phase"]] = record
process_identity = json.loads(Path(process_identity_path).read_text(encoding="utf-8"))
data = {
    "schema_version": 2,
    "contract": "ams.m2.vertical_slice/v1",
    "run_id": run_id,
    "runtime_id": runtime_id,
    "run_nonce": nonce,
    "source_hash": source_hash,
    "started_utc": started_utc,
    "ended_utc": ended_utc,
    "scenario": "scenario_1uav_vertical_slice",
    "component_only": True,
    "p0_eligible": False,
    "processes": process_identity["processes"],
    "phases": phases,
    "raw_logs": {
        "probe": "logs/m2_probe_events.jsonl",
        "processes": "logs/m2_process_events.jsonl",
        "adapter": "logs/uav_adapter.jsonl",
    },
}
Path(output).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

python3 - "$RUN_DIR" "$RUN_ID" "$RUNTIME_ID" "$RUN_NONCE" "$SOURCE_HASH" <<'PY'
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

run_dir = Path(sys.argv[1]).resolve()
run_id, runtime_id, nonce, source_hash = sys.argv[2:]
manifest_path = run_dir / "metrics/m2_evidence_manifest.json"
required = [
    "logs/m2_runner.log",
    "logs/m2_probe_events.jsonl",
    "logs/m2_process_events.jsonl",
    "logs/uav_adapter.jsonl",
    "logs/m2_process_identity.json",
    "logs/m2_netns_snapshot.json",
    "metrics/m2_run.json",
    "metrics/provenance.json",
    "metrics/ns3_tap_build_receipt.json",
    "pcap/gcs_ingress_good.pcap",
    "pcap/ns3_ingress_good.pcap",
    "pcap/ns3_egress_good.pcap",
    "pcap/uav_egress_good.pcap",
    "pcap/gcs_ingress_recovery.pcap",
    "pcap/ns3_ingress_recovery.pcap",
    "pcap/ns3_egress_recovery.pcap",
    "pcap/uav_egress_recovery.pcap",
    "pcap/gcs_ingress_down.pcap",
    "pcap/uav_egress_down.pcap",
]
selected = set(required)
selected.update(path.relative_to(run_dir).as_posix() for path in (run_dir / "logs").glob("*.log"))
selected.update(path.relative_to(run_dir).as_posix() for path in (run_dir / "logs").glob("*.jsonl"))
selected.update(path.relative_to(run_dir).as_posix() for path in (run_dir / "pcap").glob("*.pcap"))
selected.update(
    {
        "command.txt",
        "environment.txt",
        "logs/m2_process_identity.json",
        "logs/m2_netns_snapshot.json",
        "metrics/m2_phase_good.json",
        "metrics/m2_phase_down.json",
        "metrics/m2_phase_recovery.json",
    }
)
files = {}
for relative in sorted(selected):
    path = run_dir / relative
    if not path.is_file() or (relative in required and path.stat().st_size <= 0):
        raise SystemExit(f"required M2 evidence missing or empty: {relative}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    files[relative] = {"sha256": digest, "size_bytes": path.stat().st_size}
data = {
    "schema_version": 2,
    "contract": "ams.m2.vertical_slice.manifest/v1",
    "run_id": run_id,
    "runtime_id": runtime_id,
    "run_nonce": nonce,
    "source_hash": source_hash,
    "sealed_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "files": files,
}
with manifest_path.open("x", encoding="utf-8") as handle:
    handle.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
PY

# Runtime producers are finished.  Make raw evidence files append-proof while
# leaving directories writable for independent derived validation outputs.
sudo find "$RUN_DIR/logs" "$RUN_DIR/pcap" -type f -exec chmod a-w {} +
chmod a-w "$RUN_DIR/command.txt" "$RUN_DIR/environment.txt" \
  "$RUN_DIR/metrics/provenance.json" "$RUN_DIR/metrics/m2_run.json" \
  "$RUN_DIR/metrics/m2_phase_good.json" "$RUN_DIR/metrics/m2_phase_down.json" \
  "$RUN_DIR/metrics/m2_phase_recovery.json" "$RUN_DIR/metrics/m2_evidence_manifest.json"

trap - EXIT
printf 'M2 one-UAV vertical-slice raw run complete: %s\n' "$RUN_DIR"
