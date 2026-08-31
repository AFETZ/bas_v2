#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE="${AMS_CONTAINER_IMAGE:-multiagent_simulation:latest}"

if [[ "${1:-}" != "--inside" ]]; then
  exec docker run --rm --privileged --network host \
    --user 0:0 \
    -v "$ROOT_DIR:/workspace" \
    -w /workspace \
    -e RUN_ID="${RUN_ID:-}" \
    -e ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-97}" \
    --entrypoint bash \
    "$IMAGE" network/ns3/run_upstream_sionna_tap_spike.sh --inside
fi

if ((EUID != 0)); then
  printf 'FAIL upstream TAP spike must run as root inside the privileged container\n' >&2
  exit 2
fi

UPSTREAM_DIR="$ROOT_DIR/.external/upstream_integrations/ns3-mr2608"
REVISION="3d0643e7858edcf22da3deebb0d2e423ecfe2961"
PATCH_FILE="$ROOT_DIR/network/ns3/patches/mr2608-spike-compatibility.patch"
PROJECT_SOURCE="$ROOT_DIR/network/ns3/scratch/upstream-sionna-tap-spike.cc"
UPSTREAM_SOURCE="$UPSTREAM_DIR/scratch/upstream-sionna-tap-spike.cc"
BINARY="$UPSTREAM_DIR/build/scratch/ns3-dev-upstream-sionna-tap-spike-default"
PYTHON_DEPS="$UPSTREAM_DIR/.python-deps"
SCENE="$ROOT_DIR/.external/cavise_maps/Town01/map/scene.xml"
SCENARIO="$ROOT_DIR/network/config/scenario_1uav_town01_upstream_spike.yaml"
RUN_ID="${RUN_ID:-}"
if [[ -z "$RUN_ID" ]]; then
  RUN_ID="upstream_sionna_spike_$(date -u +%Y%m%dT%H%M%SZ)"
fi
RUN_DIR="$ROOT_DIR/runs/upstream-radio-integration/$RUN_ID"
LOG_DIR="$RUN_DIR/logs"
PCAP_DIR="$RUN_DIR/pcap"
METRIC_DIR="$RUN_DIR/metrics"
RUNTIME_DIR="$RUN_DIR/runtime"
NODE_STATE="$LOG_DIR/node_state.json"
EVENT_CSV="$LOG_DIR/native_radio_events.csv"
STATS_JSON="$METRIC_DIR/native_radio_stats.json"
RADIO_PCAP="$PCAP_DIR/native_radio.pcap"
BOUNDARY_PCAP="$PCAP_DIR/tap_boundary.pcap"
READY_FILE="$RUNTIME_DIR/ns3.ready"
UDP_LOG="$LOG_DIR/udp_received.csv"
NS3_LOG="$LOG_DIR/ns3_sionna.log"

for required in "$PATCH_FILE" "$PROJECT_SOURCE" "$SCENE" "$SCENARIO"; do
  if [[ ! -f "$required" ]]; then
    printf 'FAIL required spike input is missing: %s\n' "$required" >&2
    exit 2
  fi
done
if [[ ! -d "$UPSTREAM_DIR/.git" ]] || \
   [[ "$(git -c safe.directory="$UPSTREAM_DIR" -C "$UPSTREAM_DIR" rev-parse HEAD)" != "$REVISION" ]]; then
  printf 'FAIL exact MR !2608 checkout is required at %s (%s)\n' \
    "$UPSTREAM_DIR" "$REVISION" >&2
  exit 2
fi
if ! git -c safe.directory="$UPSTREAM_DIR" -C "$UPSTREAM_DIR" \
  apply --reverse --check "$PATCH_FILE"; then
  printf 'FAIL recorded compatibility patch is not exactly applied to MR !2608\n' >&2
  exit 2
fi
if [[ ! -f "$UPSTREAM_SOURCE" ]] || ! cmp -s "$PROJECT_SOURCE" "$UPSTREAM_SOURCE"; then
  printf 'FAIL upstream scratch copy does not match project spike source; rebuild it first\n' >&2
  exit 2
fi
if [[ ! -x "$BINARY" ]]; then
  printf 'FAIL spike binary is missing; build scratch_upstream-sionna-tap-spike first\n' >&2
  exit 2
fi
if [[ ! -d "$PYTHON_DEPS/sionna" ]]; then
  printf 'FAIL pinned upstream Python dependencies are missing: %s\n' "$PYTHON_DEPS" >&2
  exit 2
fi
if [[ -e "$RUN_DIR" ]]; then
  printf 'FAIL run directory already exists: %s\n' "$RUN_DIR" >&2
  exit 2
fi

mkdir -p "$LOG_DIR" "$PCAP_DIR" "$METRIC_DIR" "$RUNTIME_DIR"
printf 'wall_time_s,source_ip,source_port,payload\n' > "$UDP_LOG"

TRACKER_PID=""
PUBLISHER_PID=""
RECEIVER_PID=""
CAPTURE_PID=""
NS3_PID=""

stop_pid() {
  local pid="$1"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    kill -TERM "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  fi
}

cleanup() {
  local status=$?
  trap - EXIT INT TERM
  stop_pid "$NS3_PID"
  stop_pid "$CAPTURE_PID"
  stop_pid "$RECEIVER_PID"
  stop_pid "$PUBLISHER_PID"
  if [[ -n "$TRACKER_PID" ]] && kill -0 "$TRACKER_PID" 2>/dev/null; then
    kill -KILL "$TRACKER_PID" 2>/dev/null || true
    wait "$TRACKER_PID" 2>/dev/null || true
  fi
  bash "$ROOT_DIR/network/scripts/setup_one_uav_netns.sh" down >/dev/null 2>&1 || true
  exit "$status"
}
trap cleanup EXIT INT TERM

set +u
source /opt/ros/humble/setup.bash
set -u
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-97}"

start_publisher() {
  local x="$1"
  local y="$2"
  local z="$3"
  stop_pid "$PUBLISHER_PID"
  ros2 topic pub --rate 10 /uav1/odometry nav_msgs/msg/Odometry \
    "{pose: {pose: {position: {x: $x, y: $y, z: $z}, orientation: {w: 1.0}}}}" \
    > "$LOG_DIR/odometry_${x}_${y}_${z}.log" 2>&1 &
  PUBLISHER_PID=$!
}

wait_pose() {
  local expected_x="$1"
  local expected_y="$2"
  local expected_z="$3"
  local attempt
  for attempt in $(seq 1 150); do
    if python3 - "$NODE_STATE" "$expected_x" "$expected_y" "$expected_z" <<'PY'
import json
import math
import sys
from pathlib import Path

path = Path(sys.argv[1])
expected = [float(value) for value in sys.argv[2:5]]
try:
    state = json.loads(path.read_text(encoding="utf-8"))
    nodes = {node["id"]: node for node in state["nodes"]}
    actual = nodes["uav1"]["position_m"]
    valid = (
        state.get("source") == "ros_odometry"
        and not state.get("missing_nodes")
        and not state.get("stale_nodes")
        and not nodes["uav1"].get("stale")
        and all(math.isclose(a, b, abs_tol=1e-6) for a, b in zip(actual, expected))
    )
except (OSError, KeyError, ValueError, json.JSONDecodeError):
    valid = False
raise SystemExit(0 if valid else 1)
PY
    then
      return 0
    fi
    sleep 0.2
  done
  printf 'FAIL tracker did not publish fresh live pose [%s, %s, %s]\n' \
    "$expected_x" "$expected_y" "$expected_z" >&2
  return 1
}

send_phase() {
  local phase="$1"
  ip netns exec ams-gcs python3 - "$phase" <<'PY'
import socket
import sys
import time

phase = sys.argv[1]
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(("10.71.0.10", 41000))
for sequence in range(4):
    sock.sendto(f"{phase}_{sequence}".encode("ascii"), ("10.71.1.10", 5000))
    time.sleep(0.75)
sock.close()
PY
}

received_count() {
  local phase="$1"
  grep -c ",$phase" "$UDP_LOG" 2>/dev/null || true
}

wait_received() {
  local phase="$1"
  local attempt
  for attempt in $(seq 1 150); do
    if (( $(received_count "$phase") > 0 )); then
      return 0
    fi
    if [[ -n "$NS3_PID" ]] && ! kill -0 "$NS3_PID" 2>/dev/null; then
      printf 'FAIL ns-3/Sionna process exited while waiting for %s\n' "$phase" >&2
      return 1
    fi
    sleep 0.2
  done
  printf 'FAIL no %s UDP datagram arrived\n' "$phase" >&2
  return 1
}

bash "$ROOT_DIR/network/scripts/setup_one_uav_netns.sh" down >/dev/null 2>&1 || true
bash "$ROOT_DIR/network/scripts/setup_one_uav_netns.sh" up \
  > "$LOG_DIR/netns_setup.log" 2>&1
bash "$ROOT_DIR/network/scripts/setup_one_uav_netns.sh" status \
  > "$LOG_DIR/netns_status.log" 2>&1

start_publisher 80.0 0.0 15.0
python3 "$ROOT_DIR/network/position_tracker/tracker.py" \
  --scenario "$SCENARIO" \
  --output-json "$NODE_STATE" \
  --output-jsonl "$LOG_DIR/node_state.jsonl" \
  --rate-hz 5 --stale-after-s 1.0 \
  > "$LOG_DIR/position_tracker.log" 2>&1 &
TRACKER_PID=$!
wait_pose 80.0 0.0 15.0

ip netns exec ams-uav1 python3 -u - "$UDP_LOG" \
  > "$LOG_DIR/udp_receiver.log" 2>&1 <<'PY' &
import socket
import sys
import time

output = open(sys.argv[1], "a", encoding="utf-8", buffering=1)
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(("10.71.1.10", 5000))
while True:
    payload, address = sock.recvfrom(2048)
    text = payload.decode("ascii", errors="replace").replace(",", "_")
    output.write(f"{time.time():.6f},{address[0]},{address[1]},{text}\n")
PY
RECEIVER_PID=$!

ip netns exec ams-ns3 tcpdump -U -i any -nn -w "$BOUNDARY_PCAP" \
  > "$LOG_DIR/tcpdump.log" 2>&1 &
CAPTURE_PID=$!

ip netns exec ams-ns3 env \
  LD_LIBRARY_PATH="$UPSTREAM_DIR/build/lib:${LD_LIBRARY_PATH:-}" \
  PATH="$UPSTREAM_DIR/build/src/tap-bridge:${PATH}" \
  PYTHONPATH="$PYTHON_DEPS" \
  MPLCONFIGDIR="$RUNTIME_DIR/matplotlib" \
  NS_LOG='SionnaRtChannelModel=level_info|prefix_time' \
  "$BINARY" \
  --scene="$SCENE" \
  --positionFile="$NODE_STATE" \
  --radioPcap="$RADIO_PCAP" \
  --eventCsv="$EVENT_CSV" \
  --statsFile="$STATS_JSON" \
  --readyFile="$READY_FILE" \
  --duration=240 \
  > "$NS3_LOG" 2>&1 &
NS3_PID=$!

for _ in $(seq 1 600); do
  [[ -f "$READY_FILE" ]] && break
  if ! kill -0 "$NS3_PID" 2>/dev/null; then
    printf 'FAIL ns-3/Sionna exited before ready; see %s\n' "$NS3_LOG" >&2
    exit 1
  fi
  sleep 0.2
done
if [[ ! -f "$READY_FILE" ]]; then
  printf 'FAIL ns-3/Sionna did not become ready\n' >&2
  exit 1
fi

send_phase LOS
wait_received LOS

start_publisher 80.0 160.0 15.0
wait_pose 80.0 160.0 15.0
sleep 2.5
send_phase NLOS
sleep 5
if (( $(received_count NLOS) != 0 )); then
  printf 'FAIL predeclared NLOS point delivered UDP unexpectedly\n' >&2
  exit 1
fi

start_publisher 80.0 0.0 15.0
wait_pose 80.0 0.0 15.0
sleep 2.5
send_phase RECOVERY
wait_received RECOVERY

kill -TERM "$NS3_PID"
if ! wait "$NS3_PID"; then
  printf 'FAIL ns-3/Sionna did not stop cleanly\n' >&2
  exit 1
fi
NS3_PID=""

send_phase AFTER_STOP
sleep 2
if (( $(received_count AFTER_STOP) != 0 )); then
  printf 'FAIL UDP bypass remained after in-process Sionna/ns-3 stop\n' >&2
  exit 1
fi

stop_pid "$CAPTURE_PID"
CAPTURE_PID=""
tcpdump -nn -r "$BOUNDARY_PCAP" udp port 5000 \
  > "$LOG_DIR/tap_boundary_udp.txt" 2> "$LOG_DIR/tap_boundary_read.log"
tcpdump -nn -r "$RADIO_PCAP" \
  > "$LOG_DIR/native_radio_pcap.txt" 2> "$LOG_DIR/native_radio_pcap_read.log"

python3 - "$RUN_DIR" "$REVISION" <<'PY'
import csv
import json
import sys
from pathlib import Path

run_dir = Path(sys.argv[1])
revision = sys.argv[2]
udp_path = run_dir / "logs/udp_received.csv"
event_path = run_dir / "logs/native_radio_events.csv"
stats_path = run_dir / "metrics/native_radio_stats.json"
radio_pcap = run_dir / "pcap/native_radio.pcap"
boundary_pcap = run_dir / "pcap/tap_boundary.pcap"
sionna_log = (run_dir / "logs/ns3_sionna.log").read_text(encoding="utf-8", errors="replace")

with udp_path.open(encoding="utf-8") as stream:
    udp_rows = list(csv.DictReader(stream))
with event_path.open(encoding="utf-8") as stream:
    events = list(csv.DictReader(stream))
stats = json.loads(stats_path.read_text(encoding="utf-8"))
counts = {
    phase: sum(row["payload"].startswith(phase + "_") for row in udp_rows)
    for phase in ("LOS", "NLOS", "RECOVERY", "AFTER_STOP")
}
poses = {(round(float(row["uav_x"]), 3), round(float(row["uav_y"]), 3), round(float(row["uav_z"]), 3)) for row in events if row["event"] == "live_pose"}
checks = {
    "live_tracker_los_pose": (80.0, 0.0, 15.0) in poses,
    "live_tracker_nlos_pose": (80.0, 160.0, 15.0) in poses,
    "sionna_path_solver_called": "Path computation finished" in sionna_log,
    "native_mac_transmitted": stats.get("cp_mac_tx", 0) > 0,
    "native_phy_received": stats.get("uav_phy_rx_ok", 0) > 0,
    "los_udp_received": counts["LOS"] > 0,
    "nlos_udp_lost": counts["NLOS"] == 0,
    "los_udp_recovered": counts["RECOVERY"] > 0,
    "sionna_stop_fail_closed": stats.get("stop_reason") == "sionna_ns3_process_stopped",
    "no_udp_bypass_after_stop": counts["AFTER_STOP"] == 0,
    "native_radio_pcap_nonempty": radio_pcap.stat().st_size > 24,
    "tap_boundary_pcap_nonempty": boundary_pcap.stat().st_size > 24,
}
summary = {
    "schema_version": 1,
    "upstream_revision": revision,
    "propagation": "SionnaRtSpectrumPropagationLossModel",
    "native_phy": "HalfDuplexIdealPhy",
    "native_mac": "AlohaNoackNetDevice",
    "udp_five_tuple": "10.71.0.10:41000 -> 10.71.1.10:5000/udp",
    "udp_received_by_phase": counts,
    "native_counters": stats,
    "checks": checks,
    "result": "PASS" if all(checks.values()) else "FAIL",
}
(run_dir / "metrics/summary.json").write_text(
    json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
print(json.dumps(summary, indent=2, sort_keys=True))
if not all(checks.values()):
    raise SystemExit(1)
PY

printf 'PASS upstream Sionna/native Spectrum/TAP spike: %s\n' "$RUN_DIR"
