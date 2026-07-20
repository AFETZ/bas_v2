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
POSITIVE_HEARTBEAT_OBSERVATION_S="${POSITIVE_HEARTBEAT_OBSERVATION_S:-5}"
POSITIVE_READINESS_STABILITY_S="${POSITIVE_READINESS_STABILITY_S:-10}"
PRESTOP_READINESS_STABILITY_S="${PRESTOP_READINESS_STABILITY_S:-10}"
STOPPED_DRAIN_S="${STOPPED_DRAIN_S:-10}"
ACK_TIMEOUT_S="${ACK_TIMEOUT_S:-3}"
DOWN_HEARTBEAT_TIMEOUT_S="${DOWN_HEARTBEAT_TIMEOUT_S:-5}"
DOWN_ACK_TIMEOUT_S="${DOWN_ACK_TIMEOUT_S:-2}"
DOWN_SETTLE_S="${DOWN_SETTLE_S:-1}"
CAPTURE_DRAIN_S="${CAPTURE_DRAIN_S:-2}"
HEADLESS_RENDERING="${HEADLESS_RENDERING:-false}"
NS3_DIR="${NS3_DIR:-$ROOT_DIR/.external/ns-3}"
NS3_PROGRAM="ams-tap-packet-engine"
NS3_BINARY="$NS3_DIR/build/scratch/ns3.40-${NS3_PROGRAM}-default"
NS3_RECEIPT_TOOL="$ROOT_DIR/network/ns3/ns3_build_receipt.py"
NS3_PROJECT_SOURCE="$ROOT_DIR/network/ns3/scratch/${NS3_PROGRAM}.cc"
NS3_COPIED_SOURCE="$NS3_DIR/scratch/${NS3_PROGRAM}.cc"
NS3_RUNNER="$ROOT_DIR/network/ns3/run_ns3_tap_packet_engine.sh"
NS3_CONFIG_TOOL="$ROOT_DIR/network/ns3/tap_packet_engine_config.py"
NS3_REQUIRED_MODULES="applications,bridge,core,csma,flow-monitor,internet,mobility,network,stats,tap-bridge,traffic-control"
ENDPOINT_SCHEMA="$ROOT_DIR/network/config/endpoint_transaction_schema.json"
ENDPOINT_MATRIX="$ROOT_DIR/network/config/endpoint_matrix_5uav.json"
ENDPOINT_CONTRACT="$RUN_DIR/metrics/m2_endpoint_contract.json"
RAW_CAPTURE="$ROOT_DIR/network/scripts/raw_packet_capture.py"
LIFECYCLE_EVENT="$ROOT_DIR/network/scripts/m2_lifecycle_event.py"
LIFECYCLE_MONITOR="$ROOT_DIR/network/scripts/m2_lifecycle_monitor.py"
PROBE="$ROOT_DIR/network/tests/mavlink_vertical_slice_probe.py"
MAVPROXY_SCRIPT="/home/ubuntu/.local/bin/mavproxy.py"
MAVLINK_PYTHON="/usr/bin/python3.10"
MAVLINK_PYTHON_SITE="/home/ubuntu/.local/lib/python3.10/site-packages"
PROCESS_IDENTITY="$RUN_DIR/logs/m2_process_identity.json"
PROBE_EVENTS="$RUN_DIR/logs/m2_probe_events.jsonl"
PROCESS_EVENTS="$RUN_DIR/logs/m2_process_events.jsonl"
ADAPTER_EVENTS="$RUN_DIR/logs/uav_adapter.jsonl"
LIFECYCLE_EVENTS="$RUN_DIR/logs/m2_lifecycle.jsonl"
MONITOR_EVENTS="$RUN_DIR/logs/m2_monitor.jsonl"
MONITOR_STOP_FILE="$RUN_DIR/logs/m2_monitor.stop"
GCS_ENDPOINT_CONTROL_SOCKET="$RUN_DIR/logs/m2_gcs_endpoint.sock"
GCS_ENDPOINT_STDOUT="$RUN_DIR/logs/m2_gcs_endpoint.stdout"
GCS_ENDPOINT_STDERR="$RUN_DIR/logs/m2_gcs_endpoint.stderr"
GCS_ENDPOINT_SHUTDOWN_LOG="$RUN_DIR/logs/m2_gcs_endpoint_shutdown.log"
STARTED_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

if [[ ! "$RUN_ID" =~ ^[a-zA-Z0-9_.-]+$ ]]; then
  printf 'FAIL RUN_ID contains unsafe characters: %s\n' "$RUN_ID" >&2
  exit 2
fi
if [[ ! "$POSITIVE_HEARTBEAT_OBSERVATION_S" =~ ^[1-9][0-9]*$ ]] || \
   (( POSITIVE_HEARTBEAT_OBSERVATION_S < 3 )); then
  printf 'FAIL POSITIVE_HEARTBEAT_OBSERVATION_S must be an integer of at least 3 seconds\n' >&2
  exit 2
fi
if [[ ! "$POSITIVE_READINESS_STABILITY_S" =~ ^[1-9][0-9]*$ ]] || \
   (( POSITIVE_READINESS_STABILITY_S < 10 )); then
  printf 'FAIL POSITIVE_READINESS_STABILITY_S must be an integer of at least 10 seconds\n' >&2
  exit 2
fi
if [[ ! "$PRESTOP_READINESS_STABILITY_S" =~ ^[1-9][0-9]*$ ]] || \
   (( PRESTOP_READINESS_STABILITY_S < 10 )); then
  printf 'FAIL PRESTOP_READINESS_STABILITY_S must be an integer of at least 10 seconds\n' >&2
  exit 2
fi
if [[ ! "$STOPPED_DRAIN_S" =~ ^[1-9][0-9]*$ ]] || (( STOPPED_DRAIN_S < 1 )); then
  printf 'FAIL STOPPED_DRAIN_S must be an integer of at least one second\n' >&2
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

for command in bash colcon ip mount python3 ros2 setsid sysctl \
  unshare; do
  if ! command -v "$command" >/dev/null 2>&1; then
    printf 'FAIL required M2 command is missing: %s\n' "$command" >&2
    exit 2
  fi
done
if [[ ! -f "$MAVPROXY_SCRIPT" || ! -x "$MAVPROXY_SCRIPT" ]]; then
  printf 'FAIL pinned M2 MAVProxy script is missing or non-executable: %s\n' \
    "$MAVPROXY_SCRIPT" >&2
  exit 2
fi
case ":${PYTHONPATH:-}:" in
  *":$MAVLINK_PYTHON_SITE:"*) ;;
  *) export PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}$MAVLINK_PYTHON_SITE" ;;
esac
mapfile -t MAVLINK_PYTHON_RUNTIME < <(
  "$MAVLINK_PYTHON" - <<'PY'
import pathlib
import sys

import pymavlink

print(pathlib.Path(sys.executable).resolve())
print(int(sys.flags.no_user_site))
print(pathlib.Path(pymavlink.__file__).resolve())
PY
)
if ((${#MAVLINK_PYTHON_RUNTIME[@]} != 3)) || \
  [[ "${MAVLINK_PYTHON_RUNTIME[0]}" != "$MAVLINK_PYTHON" ]] || \
  [[ "${MAVLINK_PYTHON_RUNTIME[1]}" != "1" ]] || \
  [[ "${MAVLINK_PYTHON_RUNTIME[2]}" != "$MAVLINK_PYTHON_SITE/pymavlink/__init__.py" ]]; then
  printf 'FAIL controlled M2 Python/pymavlink runtime is unavailable\n' >&2
  exit 2
fi
if ((EUID != 0)); then
  printf 'FAIL formal M2 requires an already capability-bounded root process\n' >&2
  exit 2
fi
umask 0002
if [[ ! -c /dev/net/tun ]]; then
  printf 'FAIL /dev/net/tun is missing; run inside the privileged project container\n' >&2
  exit 2
fi

mkdir -p "$(dirname "$RUN_DIR")"
mkdir "$RUN_DIR"
mkdir "$RUN_DIR/logs" "$RUN_DIR/metrics" "$RUN_DIR/pcap" "$RUNTIME_DIR"
RUNNER_LOG="$RUN_DIR/logs/m2_runner.log"
: > "$RUNNER_LOG"

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
printf '%q ' "${BUILD_COMMAND[@]}" > "$RUN_DIR/logs/m2_runtime_overlay_build.command"
printf '\n' >> "$RUN_DIR/logs/m2_runtime_overlay_build.command"
set +e
"${BUILD_COMMAND[@]}" > "$RUN_DIR/logs/m2_runtime_overlay_build.log" 2>&1
OVERLAY_BUILD_RC=$?
set -e
printf '%s\n' "$OVERLAY_BUILD_RC" > "$RUN_DIR/logs/m2_runtime_overlay_build.exit_code"
if ((OVERLAY_BUILD_RC != 0)) || [[ ! -f "$OVERLAY_INSTALL/setup.bash" ]]; then
  printf 'FAIL fresh M2 runtime overlay build failed\n' >&2
  exit 1
fi
# shellcheck disable=SC1090
set +u
source "$OVERLAY_INSTALL/setup.bash"
set -u
RESOLVED_SHARE="$(/usr/bin/python3.10 -c 'from ament_index_python.packages import get_package_share_directory; print(get_package_share_directory("multiagent_simulation"))')"
if [[ "$RESOLVED_SHARE" != "$EXPECTED_SHARE" ]] || [[ ! -d "$EXPECTED_SHARE" ]]; then
  printf 'FAIL M2 resolved package share is not the fresh run overlay: %s\n' \
    "$RESOLVED_SHARE" >&2
  exit 1
fi
export AMS_M1_INSTALLED_SHARE="$EXPECTED_SHARE"
export GZ_SIM_RESOURCE_PATH="$EXPECTED_SHARE/models:$EXPECTED_SHARE/worlds:$EXPECTED_SHARE"

if [[ ! -f "$NS3_DIR/VERSION" ]] || \
   [[ "$(tr -d '[:space:]' < "$NS3_DIR/VERSION")" != "3.40" ]]; then
  printf 'FAIL M2 requires the pinned ns-3 VERSION 3.40 tree: %s\n' "$NS3_DIR" >&2
  exit 2
fi
if [[ ! -x "$NS3_BINARY" ]]; then
  printf 'FAIL shared ns-3 TapBridge packet-engine binary is missing: %s\n' "$NS3_BINARY" >&2
  printf 'Run network/ns3/build_ns3_tap_packet_engine.sh inside /workspace/multiagent_simulation.\n' >&2
  exit 2
fi
if [[ ! -f "$RAW_CAPTURE" || ! -f "$LIFECYCLE_EVENT" || ! -f "$LIFECYCLE_MONITOR" || \
      ! -f "$NS3_CONFIG_TOOL" || ! -f "$NS3_RUNNER" ]]; then
  printf 'FAIL shared M2 packet-engine/capture tooling is incomplete\n' >&2
  exit 2
fi

runner_log() {
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "$RUNNER_LOG"
}

lifecycle_event() {
  local event="$1"
  shift
  python3 "$LIFECYCLE_EVENT" \
    --output "$LIFECYCLE_EVENTS" \
    --run-id "$RUN_ID" \
    --runtime-id "$RUNTIME_ID" \
    --run-nonce "$RUN_NONCE" \
    --event "$event" \
    "$@" >> "$RUNNER_LOG"
}

# This verification is outside the optional build block by design: a skipped
# build is never evidence that the existing executable matches current source.
python3 "$NS3_RECEIPT_TOOL" verify \
  --ns3-dir "$NS3_DIR" \
  --program "$NS3_PROGRAM" \
  --project-source "$NS3_PROJECT_SOURCE" \
  --copied-source "$NS3_COPIED_SOURCE" \
  --executable "$NS3_BINARY" \
  --required-modules "$NS3_REQUIRED_MODULES" \
  --copy-to "$RUN_DIR/metrics/ns3_tap_build_receipt.json" \
  > "$RUN_DIR/logs/ns3_build_receipt.log"
runner_log "verified current ns-3 TapBridge build receipt"

# Freeze the exact schema-v1 endpoint input before any runtime producer starts.
# M2 exercises control traffic today, but it consumes the complete six-cell
# uav1 slice of the same matrix that M3 expands to all five UAVs.
python3 - "$ROOT_DIR" "$ENDPOINT_SCHEMA" "$ENDPOINT_MATRIX" "$ENDPOINT_CONTRACT" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

root, schema_path, matrix_path, output_path = map(Path, sys.argv[1:])
sys.path.insert(0, str(root))
from network.validation.endpoint_transaction import (  # noqa: E402
    CONTRACT,
    MATRIX_ID,
    canonical_json,
    load_strict_json,
    sha256_bytes,
    validate_matrix_file,
)

schema = load_strict_json(schema_path)
matrix = validate_matrix_file(matrix_path, schema_path=schema_path)
expected_ids = [
    f"uav1.{traffic_class}.{direction}"
    for traffic_class in ("control", "payload", "additional_data")
    for direction in ("downlink", "uplink")
]
cells = [cell for cell in matrix["cells"] if cell.get("uav", {}).get("name") == "uav1"]
if [cell.get("cell_id") for cell in cells] != expected_ids:
    raise SystemExit("uav1 endpoint subset is not the exact ordered six-cell contract")
control_downlink, control_uplink = cells[0], cells[1]
if (
    control_downlink.get("capture_points", {}).get("remote_after_adapter")
    != "uav1.mavproxy.tail"
    or control_uplink.get("capture_points", {}).get("source_before_adapter")
    != "uav1.mavproxy.tail"
    or cells[2].get("capture_points", {}).get("remote_after_adapter")
    != "uav1.sink.eth0"
    or cells[3].get("capture_points", {}).get("source_before_adapter")
    != "uav1.source.eth0"
    or cells[4].get("capture_points", {}).get("remote_after_adapter")
    != "uav1.sink.eth0"
    or cells[5].get("capture_points", {}).get("source_before_adapter")
    != "uav1.source.eth0"
):
    raise SystemExit("uav1 control/companion capture-point sides are not exact")
if (
    matrix.get("schema_version") != 1
    or matrix.get("contract") != CONTRACT
    or matrix.get("matrix_id") != MATRIX_ID
    or schema.get("$id") != "https://ams.local/schemas/endpoint-transaction-v1.json"
    or schema.get("properties", {}).get("schema_version") != {"const": 1}
):
    raise SystemExit("endpoint transaction schema-v1 identity mismatch")

def source_record(path):
    relative = path.resolve(strict=True).relative_to(root.resolve(strict=True)).as_posix()
    return {"path": relative, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

document = {
    "contract": "ams.m2.endpoint_subset/v1",
    "schema_version": 1,
    "endpoint_transaction_contract": CONTRACT,
    "matrix_id": MATRIX_ID,
    "schema": source_record(schema_path),
    "matrix": source_record(matrix_path),
    "subset": {
        "subset_id": "uav1_all_traffic_classes",
        "uav": "uav1",
        "cell_count": 6,
        "cell_ids": expected_ids,
        "resolved_cells_sha256": sha256_bytes(canonical_json(cells)),
        "actual_control_tail_capture": {
            "capture_role": "tail",
            "interface": "ams-tail0",
            "pcap_path": "pcap/uav_tail.pcap",
            "downlink": {
                "cell_id": "uav1.control.downlink",
                "capture_point_role": "remote_after_adapter",
                "capture_point_id": "uav1.mavproxy.tail",
            },
            "uplink": {
                "cell_id": "uav1.control.uplink",
                "capture_point_role": "source_before_adapter",
                "capture_point_id": "uav1.mavproxy.tail",
            },
        },
    },
}
output_path.write_text(
    json.dumps(document, allow_nan=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY
runner_log "sealed endpoint schema-v1 and exact uav1 six-cell subset identity"

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
  printf 'source_mode=%s\n' "${AMS_COMPONENT_SOURCE_MODE:-diagnostic_live_checkout}"
  printf 'source_commit=%s\n' "${AMS_COMPONENT_SOURCE_COMMIT:-unknown}"
  printf 'runtime_overlay=%s\n' "$OVERLAY_ROOT"
  printf 'installed_package_share=%s\n' "$EXPECTED_SHARE"
  printf 'gz_sim_resource_path=%s\n' "$GZ_SIM_RESOURCE_PATH"
} > "$RUN_DIR/environment.txt"

export AMS_RUNTIME_ID="$RUNTIME_ID"
export AMS_RUN_NONCE="$RUN_NONCE"
export ROS_DOMAIN_ID
export GZ_PARTITION

python3 "$ROOT_DIR/network/scripts/write_run_provenance.py" \
  --run-dir "$RUN_DIR" \
  --qualification-profile m2_component \
  --consumed-node Q0 --consumed-node Q1 --consumed-node Q2 \
  --packet-ingress-mode tap_bridge_external \
  --medium-model csma_surrogate \
  --radio-provider-id tcp_jsonl_real_sionna \
  --radio-provider-runtime-consumed false \
  --runtime-provider-id not_applicable_pre_m4 \
  --radio-provider-runtime-reason profile_pre_m4 \
  > "$RUN_DIR/logs/provenance.log" 2>&1
SOURCE_HASH="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["source_hash"])' "$RUN_DIR/metrics/provenance.json")"
runner_log "M2 runtime initialized run_id=$RUN_ID runtime_id=$RUNTIME_ID"

declare -A CAPTURE_PID=()
declare -A CAPTURE_PGID=()
declare -A CAPTURE_FILE=()
declare -A CAPTURE_LOG=()
declare -A CAPTURE_STATS=()
declare -A CAPTURE_STDOUT=()
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
MONITOR_PID=""
MONITOR_PGID=""
GCS_ENDPOINT_WRAPPER_PID=""
GCS_ENDPOINT_PGID=""
GCS_ENDPOINT_ACTUAL_PID=""
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
  [[ -n "$state" && "$state" != "Z" ]] && kill -0 "$pid" >/dev/null 2>&1
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
  kill -INT -- "-$pgid" >/dev/null 2>&1 || true
  if ! wait_pid_dead "$pid" 50; then
    kill -TERM -- "-$pgid" >/dev/null 2>&1 || true
    wait_pid_dead "$pid" 20 || kill -KILL -- "-$pgid" >/dev/null 2>&1 || true
  fi
  wait "$pid" >/dev/null 2>&1 || true
  CAPTURE_PID[$key]=""
  if [[ ! -s "${CAPTURE_FILE[$key]}" ]]; then
    printf 'FAIL capture is missing or empty: %s\n' "${CAPTURE_FILE[$key]}" >&2
    return 1
  fi
  if [[ ! -s "${CAPTURE_STATS[$key]}" ]]; then
    printf 'FAIL capture statistics are missing: %s\n' "${CAPTURE_STATS[$key]}" >&2
    return 1
  fi
  if [[ -s "${CAPTURE_LOG[$key]}" || -s "${CAPTURE_STDOUT[$key]}" ]]; then
    printf 'FAIL raw capture wrote unexpected stdout/stderr for %s\n' "$key" >&2
    return 1
  fi
}

stop_lifecycle_monitor() {
  local strict="${1:-false}"
  local pid="$MONITOR_PID"
  local pgid="$MONITOR_PGID"
  [[ -n "$pid" ]] || return 0
  printf 'stop\n' > "$MONITOR_STOP_FILE"
  if ! wait_pid_dead "$pid" 100; then
    if [[ "$strict" == "true" ]]; then
      printf 'FAIL M2 lifecycle monitor did not stop through its stop-file protocol\n' >&2
      return 1
    fi
    kill -TERM -- "-$pgid" >/dev/null 2>&1 || true
    wait_pid_dead "$pid" 20 || kill -KILL -- "-$pgid" >/dev/null 2>&1 || true
  fi
  local monitor_rc=0
  wait "$pid" || monitor_rc=$?
  MONITOR_PID=""
  MONITOR_PGID=""
  if [[ "$strict" == "true" && "$monitor_rc" -ne 0 ]]; then
    printf 'FAIL M2 lifecycle monitor exited with %s\n' "$monitor_rc" >&2
    return 1
  fi
  if [[ ! -s "$MONITOR_EVENTS" ]]; then
    printf 'FAIL M2 lifecycle monitor evidence is missing or empty: %s\n' "$MONITOR_EVENTS" >&2
    return 1
  fi
}

stop_gcs_endpoint() {
  local strict="${1:-false}"
  local wrapper="$GCS_ENDPOINT_WRAPPER_PID"
  local pgid="$GCS_ENDPOINT_PGID"
  [[ -n "$wrapper" ]] || return 0
  local shutdown_rc=0
  if python3 "$PROBE" control \
    --control-socket "$GCS_ENDPOINT_CONTROL_SOCKET" \
    --run-id "$RUN_ID" \
    --runtime-id "$RUNTIME_ID" \
    --run-nonce "$RUN_NONCE" \
    --operation shutdown \
    --timeout-s 20 \
    > "$GCS_ENDPOINT_SHUTDOWN_LOG" 2>&1; then
    :
  else
    shutdown_rc=$?
  fi
  if [[ "$strict" == "true" && "$shutdown_rc" -ne 0 ]]; then
    printf 'FAIL persistent GCS endpoint shutdown control failed; see %s\n' \
      "$GCS_ENDPOINT_SHUTDOWN_LOG" >&2
    return 1
  fi
  if ! wait_pid_dead "$wrapper" 100; then
    if [[ "$strict" == "true" ]]; then
      printf 'FAIL persistent GCS endpoint did not stop through its control protocol\n' >&2
      return 1
    fi
    kill -TERM -- "-$pgid" >/dev/null 2>&1 || true
    wait_pid_dead "$wrapper" 20 || kill -KILL -- "-$pgid" >/dev/null 2>&1 || true
  fi
  local endpoint_rc=0
  wait "$wrapper" || endpoint_rc=$?
  GCS_ENDPOINT_WRAPPER_PID=""
  GCS_ENDPOINT_PGID=""
  GCS_ENDPOINT_ACTUAL_PID=""
  if [[ "$strict" == "true" && "$endpoint_rc" -ne 0 ]]; then
    printf 'FAIL persistent GCS endpoint exited with %s\n' "$endpoint_rc" >&2
    return 1
  fi
  if [[ "$strict" == "true" && ( ! -s "$PROBE_EVENTS" || -s "$GCS_ENDPOINT_STDERR" ) ]]; then
    printf 'FAIL persistent GCS endpoint evidence/stdERR is incomplete\n' >&2
    return 1
  fi
}

start_capture() {
  local key="$1"
  local namespace="$2"
  local interface="$3"
  local output="$4"
  local log="$RUN_DIR/logs/capture_${key}.stderr"
  local stdout="$RUN_DIR/logs/capture_${key}.stdout"
  local stats="$RUN_DIR/logs/capture_${key}_stats.json"
  if [[ -e "$output" ]]; then
    printf 'FAIL refusing to overwrite capture: %s\n' "$output" >&2
    return 1
  fi
  if [[ "$namespace" == "root" ]]; then
    setsid python3 -u "$RAW_CAPTURE" \
      --interface "$interface" --pcap "$output" --stats "$stats" \
      > "$stdout" 2> "$log" &
  else
    setsid ip netns exec "$namespace" \
      python3 -u "$RAW_CAPTURE" \
      --interface "$interface" --pcap "$output" --stats "$stats" \
      > "$stdout" 2> "$log" &
  fi
  local pid=$!
  CAPTURE_PID[$key]="$pid"
  CAPTURE_PGID[$key]="$pid"
  CAPTURE_FILE[$key]="$output"
  CAPTURE_LOG[$key]="$log"
  CAPTURE_STATS[$key]="$stats"
  CAPTURE_STDOUT[$key]="$stdout"
  sleep 0.5
  if ! pid_alive "$pid"; then
    wait "$pid" >/dev/null 2>&1 || true
    printf 'FAIL raw capture exited for %s; see %s\n' "$key" "$log" >&2
    return 1
  fi
}

stop_ns3_without_artifact_checks() {
  local strict="${1:-false}"
  local wrapper="$CURRENT_NS3_WRAPPER_PID"
  local actual="$CURRENT_NS3_ACTUAL_PID"
  local pgid="$CURRENT_NS3_PGID"
  local phase="$CURRENT_NS3_PHASE"
  [[ -n "$wrapper" ]] || return 0
  if [[ -n "$phase" ]]; then
    printf 'stop\n' > "$RUN_DIR/logs/ns3_${phase}.stop"
  fi
  if [[ -n "$actual" ]] && ! wait_pid_dead "$actual" 100; then
    if [[ "$strict" == "true" ]]; then
      printf 'FAIL ns-3 %s did not terminate through its stop-file lifecycle\n' "$phase" >&2
      return 1
    fi
    kill -TERM "$actual" >/dev/null 2>&1 || true
    wait_pid_dead "$actual" 30 || kill -KILL "$actual" >/dev/null 2>&1 || true
  fi
  if ! wait_pid_dead "$wrapper" 30; then
    if [[ "$strict" == "true" ]]; then
      printf 'FAIL ns-3 %s wrapper did not terminate cleanly\n' "$phase" >&2
      return 1
    fi
    kill -TERM -- "-$pgid" >/dev/null 2>&1 || true
    wait_pid_dead "$wrapper" 20 || kill -KILL -- "-$pgid" >/dev/null 2>&1 || true
  fi
  local wrapper_rc=0
  wait "$wrapper" >/dev/null 2>&1 || wrapper_rc=$?
  if [[ "$strict" == "true" && "$wrapper_rc" -ne 0 ]]; then
    printf 'FAIL ns-3 %s wrapper exited with %s after stop-file request\n' "$phase" "$wrapper_rc" >&2
    return 1
  fi
  CURRENT_NS3_WRAPPER_PID=""
  CURRENT_NS3_PGID=""
  CURRENT_NS3_ACTUAL_PID=""
  CURRENT_NS3_PHASE=""
}

stop_adapter() {
  [[ -n "$ADAPTER_WRAPPER_PID" ]] || return 0
  if [[ -n "$ADAPTER_ACTUAL_PID" ]] && pid_alive "$ADAPTER_ACTUAL_PID"; then
    kill -TERM "$ADAPTER_ACTUAL_PID" >/dev/null 2>&1 || true
    if ! wait_pid_dead "$ADAPTER_ACTUAL_PID" 80; then
      kill -KILL "$ADAPTER_ACTUAL_PID" >/dev/null 2>&1 || true
      wait_pid_dead "$ADAPTER_ACTUAL_PID" 20 || true
    fi
  fi
  if ! wait_pid_dead "$ADAPTER_WRAPPER_PID" 30; then
    kill -TERM -- "-$ADAPTER_PGID" >/dev/null 2>&1 || true
    wait_pid_dead "$ADAPTER_WRAPPER_PID" 20 || \
      kill -KILL -- "-$ADAPTER_PGID" >/dev/null 2>&1 || true
  fi
  wait "$ADAPTER_WRAPPER_PID" >/dev/null 2>&1 || true
  ADAPTER_WRAPPER_PID=""
}

stop_launch() {
  [[ -n "$LAUNCH_PID" ]] || return 0
  kill -TERM -- "-$LAUNCH_PGID" >/dev/null 2>&1 || true
  if ! wait_pid_dead "$LAUNCH_PID" 100; then
    kill -KILL -- "-$LAUNCH_PGID" >/dev/null 2>&1 || true
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
  stop_lifecycle_monitor false
  stop_gcs_endpoint false
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
        command = ["ip", "-j", "-n", namespace, kind, "show"]
        result = subprocess.run(command, check=True, capture_output=True, text=True)
        record[kind] = json.loads(result.stdout)
    data["namespaces"][namespace] = record
Path(output).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

start_persistent_captures() {
  # M2's capture boundary deliberately spans the whole engine lifecycle:
  # good -> stop/drain -> down -> recovery.  A phase-local collector can hide
  # a queued frame at either transition, so these five collectors are started
  # before any producer and stopped only after the recovery terminal boundary.
  start_capture tail root "$TAIL_ROOT" "$RUN_DIR/pcap/uav_tail.pcap"
  start_capture gcs "$GCS_NS" eth0 "$RUN_DIR/pcap/gcs_ingress.pcap"
  start_capture ns3_external_gcs "$NS3_NS" v-gcs-ns3 \
    "$RUN_DIR/pcap/ns3_external_ingress.pcap"
  start_capture ns3_external_uav "$NS3_NS" v-uav-ns3 \
    "$RUN_DIR/pcap/ns3_external_egress.pcap"
  start_capture uav "$UAV_NS" eth0 "$RUN_DIR/pcap/uav_egress.pcap"
}

stop_persistent_captures() {
  local key
  for key in tail gcs ns3_external_gcs ns3_external_uav uav; do
    stop_capture "$key"
  done
}

start_persistent_captures
lifecycle_event captures_ready \
  --field capture_count=5 \
  --field capture_contract=persistent_good_stopped_recovery

ADAPTER_READY="$RUN_DIR/logs/uav_adapter.ready"
setsid ip netns exec "$UAV_NS" env PYTHONUNBUFFERED=1 \
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
    generate_sensor_models:=false \
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

start_gcs_endpoint() {
  if [[ -e "$GCS_ENDPOINT_CONTROL_SOCKET" || -e "$PROBE_EVENTS" || -e "$PROCESS_EVENTS" ]]; then
    printf 'FAIL refusing to replace persistent GCS endpoint evidence or control socket\n' >&2
    return 1
  fi
  setsid ip netns exec "$GCS_NS" env PYTHONUNBUFFERED=1 \
    python3 "$PROBE" serve \
    --run-id "$RUN_ID" \
    --runtime-id "$RUNTIME_ID" \
    --run-nonce "$RUN_NONCE" \
    --endpoint-event-log "$PROBE_EVENTS" \
    --process-identity "$PROCESS_IDENTITY" \
    --process-event-log "$PROCESS_EVENTS" \
    --control-socket "$GCS_ENDPOINT_CONTROL_SOCKET" \
    --gcs-bind 10.71.0.10:14600 \
    --uav-endpoint 10.71.1.10:14601 \
    > "$GCS_ENDPOINT_STDOUT" 2> "$GCS_ENDPOINT_STDERR" &
  GCS_ENDPOINT_WRAPPER_PID=$!
  GCS_ENDPOINT_PGID=$GCS_ENDPOINT_WRAPPER_PID
  local index
  for ((index = 0; index < STARTUP_TIMEOUT_S * 10; index++)); do
    if [[ -S "$GCS_ENDPOINT_CONTROL_SOCKET" && -s "$GCS_ENDPOINT_STDOUT" && -s "$PROBE_EVENTS" ]]; then
      if GCS_ENDPOINT_ACTUAL_PID="$(python3 - "$GCS_ENDPOINT_STDOUT" "$PROBE_EVENTS" <<'PY'
import json
import sys
from pathlib import Path

stdout_path, events_path = map(Path, sys.argv[1:])
ready = [json.loads(line) for line in stdout_path.read_text(encoding="utf-8").splitlines() if line]
if len(ready) != 1:
    raise SystemExit("persistent endpoint did not emit exactly one readiness record")
record = ready[0]
if (
    record.get("schema") != "ams.m2.persistent-gcs-endpoint/v1"
    or record.get("event") != "endpoint_ready"
    or record.get("gcs_bind") != ["10.71.0.10", 14600]
):
    raise SystemExit("persistent endpoint readiness record is not exact")
events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines() if line]
starts = [event for event in events if event.get("event") == "endpoint_started"]
if len(starts) != 1 or not isinstance(starts[0].get("endpoint_pid"), int):
    raise SystemExit("persistent endpoint start identity is absent")
if record.get("endpoint_instance_id") != starts[0].get("endpoint_instance_id"):
    raise SystemExit("persistent endpoint readiness identity does not bind endpoint_started")
print(starts[0]["endpoint_pid"])
PY
      )" && [[ -n "$GCS_ENDPOINT_ACTUAL_PID" ]] && pid_alive "$GCS_ENDPOINT_ACTUAL_PID"; then
        break
      fi
    fi
    if ! pid_alive "$GCS_ENDPOINT_WRAPPER_PID"; then
      wait "$GCS_ENDPOINT_WRAPPER_PID" || true
      printf 'FAIL persistent GCS endpoint exited before readiness; see %s\n' "$GCS_ENDPOINT_STDERR" >&2
      return 1
    fi
    sleep 0.1
  done
  if [[ -z "$GCS_ENDPOINT_ACTUAL_PID" ]] || ! pid_alive "$GCS_ENDPOINT_ACTUAL_PID"; then
    printf 'FAIL persistent GCS endpoint did not become ready\n' >&2
    return 1
  fi
  runner_log "persistent GCS endpoint ready pid=$GCS_ENDPOINT_ACTUAL_PID"
}

start_lifecycle_monitor() {
  local topology_sha256
  topology_sha256="$(python3 - "$RUN_DIR/logs/m2_netns_snapshot.json" <<'PY'
import hashlib
import sys
from pathlib import Path
print(hashlib.sha256(Path(sys.argv[1]).read_bytes()).hexdigest())
PY
)"
  rm -f -- "$MONITOR_STOP_FILE"
  setsid python3 "$LIFECYCLE_MONITOR" \
    --run-id "$RUN_ID" \
    --runtime-id "$RUNTIME_ID" \
    --run-nonce "$RUN_NONCE" \
    --output "$MONITOR_EVENTS" \
    --sample-period-s 0.5 \
    --stop-file "$MONITOR_STOP_FILE" \
    --topology-file "$RUN_DIR/logs/m2_netns_snapshot.json" \
    --topology-sha256 "$topology_sha256" \
    --role "launch=$(process_reference "$LAUNCH_PID")" \
    --role "sitl=$(process_reference "$SITL_PID")" \
    --role "mavproxy=$(process_reference "$MAVPROXY_PID")" \
    --role "adapter=$(process_reference "$ADAPTER_ACTUAL_PID")" \
    --role "gcs_endpoint=$(process_reference "$GCS_ENDPOINT_ACTUAL_PID")" \
    --role "capture_tail=$(process_reference "${CAPTURE_PID[tail]}")" \
    --role "capture_gcs=$(process_reference "${CAPTURE_PID[gcs]}")" \
    --role "capture_ns3_external_gcs=$(process_reference "${CAPTURE_PID[ns3_external_gcs]}")" \
    --role "capture_ns3_external_uav=$(process_reference "${CAPTURE_PID[ns3_external_uav]}")" \
    --role "capture_uav=$(process_reference "${CAPTURE_PID[uav]}")" \
    > "$RUN_DIR/logs/m2_lifecycle_monitor.stdout" \
    2> "$RUN_DIR/logs/m2_lifecycle_monitor.stderr" &
  MONITOR_PID=$!
  MONITOR_PGID=$MONITOR_PID
  local index
  for ((index = 0; index < 50; index++)); do
    [[ -s "$MONITOR_EVENTS" ]] && break
    if ! pid_alive "$MONITOR_PID"; then
      wait "$MONITOR_PID" || true
      printf 'FAIL M2 lifecycle monitor exited before first sample\n' >&2
      return 1
    fi
    sleep 0.1
  done
  if [[ ! -s "$MONITOR_EVENTS" ]] || ! pid_alive "$MONITOR_PID"; then
    printf 'FAIL M2 lifecycle monitor did not become ready\n' >&2
    return 1
  fi
}

start_gcs_endpoint
start_lifecycle_monitor
lifecycle_event endpoints_ready \
  --field monitor_pid="$MONITOR_PID" \
  --field stable_process_count=5 \
  --field persistent_capture_count=5

find_ns3_process() {
  local matches=()
  local pid
  while read -r pid; do
    [[ -n "$pid" ]] || continue
    if [[ "$(readlink "/proc/$pid/exe" 2>/dev/null || true)" == "$NS3_BINARY" ]]; then
      matches+=("$pid")
    fi
  done < <(ip netns pids "$NS3_NS")
  if (( ${#matches[@]} != 1 )); then
    printf 'FAIL expected one ns-3 vertical-slice PID, found %s\n' "${matches[*]:-none}" >&2
    return 1
  fi
  printf '%s\n' "${matches[0]}"
}

start_ns3() {
  local phase="$1"
  local event_epoch
  case "$phase" in
    good) event_epoch=1 ;;
    recovery) event_epoch=2 ;;
    *) printf 'FAIL invalid live ns-3 phase: %s\n' "$phase" >&2; return 2 ;;
  esac
  # Re-hash the source, CMake configuration and executable immediately before
  # each runtime process.  This rejects changes after initial run setup.
  python3 "$NS3_RECEIPT_TOOL" verify \
    --ns3-dir "$NS3_DIR" \
    --program "$NS3_PROGRAM" \
    --project-source "$NS3_PROJECT_SOURCE" \
    --copied-source "$NS3_COPIED_SOURCE" \
    --executable "$NS3_BINARY" \
    --required-modules "$NS3_REQUIRED_MODULES" \
    >> "$RUN_DIR/logs/ns3_build_receipt.log"
  RUN_DIR="$RUN_DIR" NS3_NS="$NS3_NS" NS3_DIR="$NS3_DIR" \
    UAV_COUNT=1 EVENT_EPOCH="$event_epoch" TAP_GCS=tap-gcs TAP_UAVS=tap-uav \
    DURATION_MS=3600000 NS3_SEED=42 NS3_RUN=1 SELF_TEST=0 \
    SIONNA_IPC_ENABLED=0 \
    EVENTS_FILE="$RUN_DIR/logs/ns3_${phase}_packet_events.jsonl" \
    LIFECYCLE_FILE="$RUN_DIR/logs/ns3_${phase}.lifecycle.jsonl" \
    PCAP_PREFIX="$RUN_DIR/pcap/ns3_${phase}" \
    READY_FILE="$RUN_DIR/logs/ns3_${phase}.ready" \
    STOP_FILE="$RUN_DIR/logs/ns3_${phase}.stop" \
    CONFIG_REPORT="$RUN_DIR/logs/ns3_${phase}_config.json" \
    ARGV_FILE="$RUN_DIR/logs/ns3_${phase}.argv" \
    setsid "$NS3_RUNNER" \
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
  if [[ ! -s "$RUN_DIR/logs/ns3_${phase}.lifecycle.jsonl" ]]; then
    printf 'FAIL ns-3 %s lifecycle journal is absent before readiness acceptance\n' "$phase" >&2
    return 1
  fi
  CURRENT_NS3_ACTUAL_PID="$(find_ns3_process)"
  pid_alive "$CURRENT_NS3_ACTUAL_PID"
  case "$phase" in
    good)
      lifecycle_event engine1_ready \
        --field phase=good \
        --field event_epoch="$event_epoch" \
        --field ns3_pid="$CURRENT_NS3_ACTUAL_PID"
      ;;
    recovery)
      lifecycle_event engine2_ready \
        --field phase=recovery \
        --field event_epoch="$event_epoch" \
        --field ns3_pid="$CURRENT_NS3_ACTUAL_PID"
      ;;
  esac
}

wait_positive_readiness() {
  local phase="$1"
  local stability_s="${2:-$POSITIVE_READINESS_STABILITY_S}"
  local expected_ns3_pid="$CURRENT_NS3_ACTUAL_PID"
  local expected_ns3_wrapper_pid="$CURRENT_NS3_WRAPPER_PID"
  local iterations=$((stability_s * 10))
  local index
  if [[ -z "$expected_ns3_pid" || -z "$expected_ns3_wrapper_pid" ]]; then
    printf 'FAIL %s positive readiness has no ns-3 process identity\n' "$phase" >&2
    return 1
  fi
  runner_log \
    "phase $phase positive readiness stability started duration_s=$stability_s ns3_pid=$expected_ns3_pid"
  for ((index = 0; index < iterations; index++)); do
    if [[ "$CURRENT_NS3_ACTUAL_PID" != "$expected_ns3_pid" ]] || \
       [[ "$CURRENT_NS3_WRAPPER_PID" != "$expected_ns3_wrapper_pid" ]] || \
       ! pid_alive "$expected_ns3_pid" || \
       ! pid_alive "$expected_ns3_wrapper_pid" || \
       ! pid_alive "$ADAPTER_WRAPPER_PID" || \
       ! pid_alive "$ADAPTER_ACTUAL_PID" || \
       ! pid_alive "$GCS_ENDPOINT_WRAPPER_PID" || \
       ! pid_alive "$GCS_ENDPOINT_ACTUAL_PID" || \
       ! pid_alive "$LAUNCH_PID" || \
       ! pid_alive "$SITL_PID" || \
       ! pid_alive "$MAVPROXY_PID"; then
      printf 'FAIL %s positive readiness lost a required runtime process\n' "$phase" >&2
      return 1
    fi
    sleep 0.1
  done
  runner_log \
    "phase $phase positive readiness stability complete duration_s=$stability_s ns3_pid=$expected_ns3_pid"
}

normalize_ns3_pcaps() {
  local phase="$1"
  local mapping=(
    "ns3_${phase}-radio-gcs.pcap:ns3_ingress_${phase}.pcap"
    "ns3_${phase}-radio-uav1.pcap:ns3_egress_${phase}.pcap"
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
  case "$phase" in
    good)
      lifecycle_event stop_requested --field engine=1 --field phase=good
      ;;
    recovery)
      lifecycle_event recovery_stop_requested --field engine=2 --field phase=recovery
      ;;
  esac
  stop_ns3_without_artifact_checks true
  normalize_ns3_pcaps "$phase"
  case "$phase" in
    good)
      lifecycle_event engine1_stopped --field engine=1 --field phase=good
      ;;
    recovery)
      lifecycle_event engine2_stopped --field engine=2 --field phase=recovery
      ;;
  esac
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
  # The control client must remain connected until the endpoint has completed
  # the whole phase.  In particular, the intentionally blackholed down phase
  # can consume every ACK timeout plus its final heartbeat timeout; the CLI
  # default of 10 seconds is therefore not a safe upper bound.
  local control_timeout_s
  control_timeout_s="$(python3 - "$attempts" "$expected_ack" "$heartbeat_timeout" \
    "$ack_timeout" "$POSITIVE_HEARTBEAT_OBSERVATION_S" <<'PY'
import math
import sys

attempts_text, expected_ack, heartbeat_text, ack_text, observation_text = sys.argv[1:]
try:
    attempts = int(attempts_text)
    heartbeat = float(heartbeat_text)
    ack = float(ack_text)
    observation = float(observation_text)
except ValueError as exc:
    raise SystemExit(f"invalid persistent control timeout input: {exc}") from exc
if attempts <= 0 or not all(math.isfinite(value) and value > 0 for value in (heartbeat, ack, observation)):
    raise SystemExit("persistent control timeout inputs must be positive finite values")
# 10s maximum pre-window quiet wait, two 0.5s direct-endpoint checks, and a
# conservative 15s process/control scheduling margin surround the probe work.
phase_work = (observation if expected_ack == "true" else heartbeat) + attempts * ack
timeout_s = math.ceil(10.0 + 1.0 + phase_work + 15.0)
if timeout_s > 600:
    raise SystemExit("persistent control timeout exceeds the 600-second protocol maximum")
print(max(30, timeout_s))
PY
)"
  local command=(
    python3 "$PROBE" control
    --control-socket "$GCS_ENDPOINT_CONTROL_SOCKET"
    --run-id "$RUN_ID"
    --runtime-id "$RUNTIME_ID"
    --run-nonce "$RUN_NONCE"
    --operation run-phase
    --phase "$phase"
    --expected-ns3-state "$expected_ns3_state"
    --attempts "$attempts"
    --heartbeat-timeout-s "$heartbeat_timeout"
    --ack-timeout-s "$ack_timeout"
    --timeout-s "$control_timeout_s"
    "${extra[@]}"
  )
  if [[ "$expected_ack" == "true" ]]; then
    command+=(
      --positive-heartbeat-observation-s "$POSITIVE_HEARTBEAT_OBSERVATION_S"
    )
  fi
  if ! env PYTHONUNBUFFERED=1 "${command[@]}" > "$RUN_DIR/logs/probe_${phase}.log" 2>&1; then
    cat "$RUN_DIR/logs/probe_${phase}.log" >&2
    return 1
  fi
  python3 - "$RUN_DIR/logs/probe_${phase}.log" "$RUN_DIR/metrics/m2_phase_${phase}.json" \
    "$RUN_ID" "$RUNTIME_ID" "$RUN_NONCE" "$phase" "$attempts" "$expected_ack" <<'PY'
import json
import math
import sys
from pathlib import Path

log_path, output, run_id, runtime_id, nonce, phase, attempts_text, expected_ack_text = sys.argv[1:]
lines = [line for line in Path(log_path).read_text(encoding="utf-8").splitlines() if line]
if len(lines) != 1:
    raise SystemExit("persistent phase control did not emit exactly one response")
response = json.loads(lines[0])
result = response.get("result") if isinstance(response.get("result"), dict) else {}
phase_result = result.get("phase_result") if isinstance(result.get("phase_result"), dict) else {}
attempts = int(attempts_text)
expected_ack = expected_ack_text == "true"
if (
    response.get("ok") is not True
    or response.get("operation") != "run_phase"
    or result.get("phase") != phase
    or phase_result.get("attempts") != attempts
):
    raise SystemExit("persistent phase control response is not the requested phase result")
acks = phase_result.get("acknowledgements")
if isinstance(acks, bool) or not isinstance(acks, int) or not 0 <= acks <= attempts:
    raise SystemExit("persistent phase acknowledgement count is invalid")
loss_rate = (attempts - acks) / attempts
data = {
    "schema_version": 2,
    "contract": "ams.m2.vertical_slice.phase/v1",
    "run_id": run_id,
    "runtime_id": runtime_id,
    "run_nonce": nonce,
    "phase": phase,
    "attempts": attempts,
    "expected_ack": expected_ack,
    "acknowledgements": acks,
    "telemetry_responses": phase_result.get("telemetry_responses"),
    "heartbeat_count": phase_result.get("heartbeat_count"),
    "heartbeat_observation_count": phase_result.get("heartbeat_observation_count"),
    "heartbeat_observation_s": phase_result.get("heartbeat_observation_s"),
    "heartbeat_timeout": phase_result.get("heartbeat_timeout"),
    "loss_rate": loss_rate,
    "ack_latency": phase_result.get("ack_latency"),
    "telemetry_latency": phase_result.get("telemetry_latency"),
}
Path(output).write_text(json.dumps(data, allow_nan=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
}

start_ns3 good
lifecycle_event good_dwell_start --field duration_s="$POSITIVE_READINESS_STABILITY_S"
wait_positive_readiness good
lifecycle_event good_dwell_complete --field duration_s="$POSITIVE_READINESS_STABILITY_S"
GOOD_NS3_REFERENCE="$(process_reference "$CURRENT_NS3_ACTUAL_PID")"
runner_log "phase good started ns3_pid=$CURRENT_NS3_ACTUAL_PID"
lifecycle_event good_start --field ns3_pid="$CURRENT_NS3_ACTUAL_PID"
run_probe_phase good "$GOOD_ATTEMPTS" true up "$HEARTBEAT_TIMEOUT_S" "$ACK_TIMEOUT_S" \
  --ns3-process "$GOOD_NS3_REFERENCE"
sleep "$CAPTURE_DRAIN_S"
lifecycle_event good_terminal --field attempts="$GOOD_ATTEMPTS"
lifecycle_event prestop_dwell_start --field duration_s="$PRESTOP_READINESS_STABILITY_S"
wait_positive_readiness good "$PRESTOP_READINESS_STABILITY_S"
lifecycle_event prestop_dwell_complete --field duration_s="$PRESTOP_READINESS_STABILITY_S"
stop_ns3_phase good
runner_log "phase good complete and ns-3 stopped"

lifecycle_event stopped_drain_start --field duration_s="$STOPPED_DRAIN_S"
sleep "$STOPPED_DRAIN_S"
lifecycle_event stopped_drain_complete --field duration_s="$STOPPED_DRAIN_S"
sleep "$DOWN_SETTLE_S"
runner_log "phase down started with prior_ns3=$GOOD_NS3_REFERENCE"
lifecycle_event down_start --field prior_engine=1
run_probe_phase down "$DOWN_ATTEMPTS" false down "$DOWN_HEARTBEAT_TIMEOUT_S" "$DOWN_ACK_TIMEOUT_S" \
  --absent-process "$GOOD_NS3_REFERENCE"
sleep "$CAPTURE_DRAIN_S"
lifecycle_event down_terminal --field attempts="$DOWN_ATTEMPTS"
runner_log "phase down complete"

start_ns3 recovery
lifecycle_event recovery_dwell_start --field duration_s="$POSITIVE_READINESS_STABILITY_S"
wait_positive_readiness recovery
lifecycle_event recovery_dwell_complete --field duration_s="$POSITIVE_READINESS_STABILITY_S"
RECOVERY_NS3_REFERENCE="$(process_reference "$CURRENT_NS3_ACTUAL_PID")"
runner_log "phase recovery started ns3_pid=$CURRENT_NS3_ACTUAL_PID"
lifecycle_event recovery_start --field ns3_pid="$CURRENT_NS3_ACTUAL_PID"
run_probe_phase recovery "$RECOVERY_ATTEMPTS" true up "$HEARTBEAT_TIMEOUT_S" "$ACK_TIMEOUT_S" \
  --ns3-process "$RECOVERY_NS3_REFERENCE" \
  --absent-process "$GOOD_NS3_REFERENCE"
sleep "$CAPTURE_DRAIN_S"
lifecycle_event recovery_terminal --field attempts="$RECOVERY_ATTEMPTS"
lifecycle_event recovery_prestop_dwell_start --field duration_s="$PRESTOP_READINESS_STABILITY_S"
wait_positive_readiness recovery "$PRESTOP_READINESS_STABILITY_S"
lifecycle_event recovery_prestop_dwell_complete --field duration_s="$PRESTOP_READINESS_STABILITY_S"
stop_ns3_phase recovery
runner_log "phase recovery complete and ns-3 stopped"

# Stop all live producers before hashing the immutable raw evidence.
stop_lifecycle_monitor true
stop_gcs_endpoint true
stop_adapter
stop_launch
stop_persistent_captures
GCS_NS="$GCS_NS" NS3_NS="$NS3_NS" UAV_NS="$UAV_NS" TAIL_ROOT="$TAIL_ROOT" \
  "$ROOT_DIR/network/scripts/setup_one_uav_netns.sh" down \
  > "$RUN_DIR/logs/netns_teardown.log" 2>&1
NAMESPACES_OWNED=0
runner_log "all runtime producers stopped; sealing raw evidence"
/usr/bin/rm -rf -- "$OVERLAY_BUILD" "$OVERLAY_LOG"

ENDED_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
python3 - "$RUN_DIR/metrics/m2_run.json" "$ROOT_DIR" "$ENDPOINT_CONTRACT" \
  "$RUN_DIR/metrics/ns3_tap_build_receipt.json" \
  "$RUN_ID" "$RUNTIME_ID" "$RUN_NONCE" \
  "$SOURCE_HASH" "$STARTED_UTC" "$ENDED_UTC" "$PROCESS_IDENTITY" \
  "$RUN_DIR/metrics/m2_phase_good.json" "$RUN_DIR/metrics/m2_phase_down.json" \
  "$RUN_DIR/metrics/m2_phase_recovery.json" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

(
    output,
    root_dir,
    endpoint_contract_path,
    receipt_path,
    run_id,
    runtime_id,
    nonce,
    source_hash,
    started_utc,
    ended_utc,
    process_identity_path,
    *phase_paths,
) = sys.argv[1:]
output = Path(output)
root_dir = Path(root_dir).resolve(strict=True)
run_dir = output.parents[1].resolve(strict=True)

def strict_object(path):
    value = json.loads(
        Path(path).read_text(encoding="utf-8"),
        parse_constant=lambda token: (_ for _ in ()).throw(
            ValueError(f"non-finite JSON constant {token}")
        ),
    )
    if not isinstance(value, dict):
        raise SystemExit(f"raw identity is not a JSON object: {path}")
    return value

def raw_record(relative):
    path = run_dir / relative
    if not path.is_file() or path.is_symlink() or path.stat().st_size <= 0:
        raise SystemExit(f"packet-engine raw identity is missing: {relative}")
    return {
        "path": relative,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "size_bytes": path.stat().st_size,
    }

def source_record(relative):
    path = root_dir / relative
    if not path.is_file() or path.is_symlink() or path.stat().st_size <= 0:
        raise SystemExit(f"packet-engine source identity is missing: {relative}")
    return {"path": relative, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

phases = {}
for value in phase_paths:
    record = strict_object(value)
    if (record.get("run_id"), record.get("runtime_id"), record.get("run_nonce")) != (
        run_id,
        runtime_id,
        nonce,
    ):
        raise SystemExit(f"mixed phase identity: {value}")
    phases[record["phase"]] = record
process_identity = strict_object(process_identity_path)
endpoint_contract = strict_object(endpoint_contract_path)
receipt = strict_object(receipt_path)
receipt_subject = receipt.get("subject")
if not isinstance(receipt_subject, dict) or not isinstance(
    receipt_subject.get("executable"), dict
):
    raise SystemExit("packet-engine build receipt lacks executable identity")

engine_phases = {}
for phase, epoch in (("good", 1), ("recovery", 2)):
    config_relative = f"logs/ns3_{phase}_config.json"
    config = strict_object(run_dir / config_relative)
    config_sha256 = config.get("config_sha256")
    if not isinstance(config_sha256, str):
        raise SystemExit(f"packet-engine {phase} config lacks config_sha256")
    engine_phases[phase] = {
        "event_epoch": epoch,
        "config_sha256": config_sha256,
        "config": raw_record(config_relative),
        "events": raw_record(f"logs/ns3_{phase}_packet_events.jsonl"),
        "argv": raw_record(f"logs/ns3_{phase}.argv"),
        "ready": raw_record(f"logs/ns3_{phase}.ready"),
        "stop": raw_record(f"logs/ns3_{phase}.stop"),
        "lifecycle": raw_record(f"logs/ns3_{phase}.lifecycle.jsonl"),
    }

data = {
    "schema_version": 2,
    "contract": "ams.m2.vertical_slice/v2",
    "run_id": run_id,
    "runtime_id": runtime_id,
    "run_nonce": nonce,
    "source_hash": source_hash,
    "started_utc": started_utc,
    "ended_utc": ended_utc,
    "scenario": "scenario_1uav_vertical_slice",
    "component_only": True,
    "p0_eligible": False,
    "endpoint_transaction": endpoint_contract,
    "packet_engine": {
        "contract": "ams.tap_packet_engine/v1",
        "program": "ams-tap-packet-engine",
        "uav_count": 1,
        "source_sha256": source_record(
            "network/ns3/scratch/ams-tap-packet-engine.cc"
        )["sha256"],
        "binary_sha256": receipt_subject["executable"].get("sha256"),
        "build_receipt_sha256": raw_record(
            "metrics/ns3_tap_build_receipt.json"
        )["sha256"],
        "config_contract": "ams.tap_packet_engine/v1",
        "config_sha256": {
            phase: identity["config_sha256"]
            for phase, identity in engine_phases.items()
        },
        "config_tool_sha256": source_record(
            "network/ns3/tap_packet_engine_config.py"
        )["sha256"],
        "runner_sha256": source_record(
            "network/ns3/run_ns3_tap_packet_engine.sh"
        )["sha256"],
        "event_schema": "ams.ns3.packet_event/v1",
        "config_tool": source_record("network/ns3/tap_packet_engine_config.py"),
        "runner": source_record("network/ns3/run_ns3_tap_packet_engine.sh"),
        "build_receipt": raw_record("metrics/ns3_tap_build_receipt.json"),
        "executable": receipt_subject["executable"],
        "phases": engine_phases,
    },
    "processes": process_identity["processes"],
    "phases": phases,
    "raw_logs": {
        "probe": "logs/m2_probe_events.jsonl",
        "processes": "logs/m2_process_events.jsonl",
        "adapter": "logs/uav_adapter.jsonl",
        "gcs_endpoint_stdout": "logs/m2_gcs_endpoint.stdout",
        "gcs_endpoint_shutdown": "logs/m2_gcs_endpoint_shutdown.log",
        "lifecycle": "logs/m2_lifecycle.jsonl",
        "monitor": "logs/m2_monitor.jsonl",
    },
}
output.write_text(
    json.dumps(data, allow_nan=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
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
    "logs/m2_lifecycle.jsonl",
    "logs/m2_monitor.jsonl",
    "logs/m2_monitor.stop",
    "logs/m2_lifecycle_monitor.stdout",
    "logs/m2_lifecycle_monitor.stderr",
    "logs/m2_gcs_endpoint.stdout",
    "logs/m2_gcs_endpoint.stderr",
    "logs/m2_gcs_endpoint_shutdown.log",
    "logs/m2_process_identity.json",
    "logs/m2_netns_snapshot.json",
    "metrics/m2_run.json",
    "metrics/m2_endpoint_contract.json",
    "metrics/provenance.json",
    "metrics/ns3_tap_build_receipt.json",
    "logs/ns3_good_config.json",
    "logs/ns3_good_packet_events.jsonl",
    "logs/ns3_good.argv",
    "logs/ns3_good.ready",
    "logs/ns3_good.stop",
    "logs/ns3_good.lifecycle.jsonl",
    "logs/ns3_recovery_config.json",
    "logs/ns3_recovery_packet_events.jsonl",
    "logs/ns3_recovery.argv",
    "logs/ns3_recovery.ready",
    "logs/ns3_recovery.stop",
    "logs/ns3_recovery.lifecycle.jsonl",
    "pcap/uav_tail.pcap",
    "pcap/gcs_ingress.pcap",
    "pcap/ns3_external_ingress.pcap",
    "pcap/ns3_external_egress.pcap",
    "pcap/ns3_ingress_good.pcap",
    "pcap/ns3_egress_good.pcap",
    "pcap/ns3_ingress_recovery.pcap",
    "pcap/ns3_egress_recovery.pcap",
    "pcap/uav_egress.pcap",
]
for key in ("tail", "gcs", "ns3_external_gcs", "ns3_external_uav", "uav"):
    required.extend(
        (
            f"logs/capture_{key}_stats.json",
            f"logs/capture_{key}.stdout",
            f"logs/capture_{key}.stderr",
        )
    )
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
    empty_allowed = relative.endswith((".stdout", ".stderr"))
    if not path.is_file() or (
        relative in required and not empty_allowed and path.stat().st_size <= 0
    ):
        raise SystemExit(f"required M2 evidence missing or empty: {relative}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    files[relative] = {"sha256": digest, "size_bytes": path.stat().st_size}
data = {
    "schema_version": 2,
    "contract": "ams.m2.vertical_slice.manifest/v2",
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
find "$RUN_DIR/logs" "$RUN_DIR/pcap" -type f -exec chmod a-w {} +
chmod a-w "$RUN_DIR/command.txt" "$RUN_DIR/environment.txt" \
  "$RUN_DIR/metrics/provenance.json" "$RUN_DIR/metrics/m2_run.json" \
  "$RUN_DIR/metrics/m2_endpoint_contract.json" \
  "$RUN_DIR/metrics/m2_phase_good.json" "$RUN_DIR/metrics/m2_phase_down.json" \
  "$RUN_DIR/metrics/m2_phase_recovery.json" "$RUN_DIR/metrics/m2_evidence_manifest.json"

python3 "$ROOT_DIR/network/validation/validate_m2_vertical_slice.py" \
  --run-dir "$RUN_DIR" \
  --json-output "$RUN_DIR/metrics/m2_validation_results.json"
# The formal producer runs as root with a minimal capability set because Linux
# namespace/TUN setup requires it.  Return ownership only to the fixed host
# acceptance uid/gid so the no-sudo host finalizer can freeze and publish.
chown -R 1000:1000 "$RUN_DIR"

trap - EXIT
printf 'M2 one-UAV vertical-slice component run complete: %s\n' "$RUN_DIR"
