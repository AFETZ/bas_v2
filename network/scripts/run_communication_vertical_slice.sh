#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NS3_DIR="${NS3_DIR:-$ROOT_DIR/.external/ns-3}"
PROBE="$ROOT_DIR/network/scripts/communication_vertical.py"
GCS_NS="ams-gcs"
NS3_NS="ams-ns3"
UAV_NAMESPACES=(ams-uav1 ams-uav2 ams-uav3)

if ((EUID != 0)); then
  printf 'Communication vertical slice requires root inside its privileged runtime container.\n' >&2
  exit 2
fi
for command in ip ros2 socat setsid python3; do
  command -v "$command" >/dev/null 2>&1 || {
    printf 'Required command is unavailable: %s\n' "$command" >&2
    exit 2
  }
done

run_id="${BAS_NETWORK_RUN_ID:-communication-$(date -u +%Y%m%dT%H%M%SZ)}"
RUN_DIR="${BAS_NETWORK_RUN_DIR:-$ROOT_DIR/runs/$run_id}"
work_dir="${BAS_NETWORK_WORK_DIR:-/tmp/bas-v2-network-work}"
uart_dir="${BAS_NETWORK_UART_DIR:-/tmp/bas-v2-uart-${run_id}}"
mkdir -p "$RUN_DIR/logs" "$RUN_DIR/metrics" "$RUN_DIR/pcap" "$work_dir" "$uart_dir"

control_sitl="$uart_dir/control-sitl"
control_adapter="$uart_dir/control-adapter"
payload_sitl="$uart_dir/payload-sitl"
payload_adapter="$uart_dir/payload-adapter"
ns3_ready="$RUN_DIR/logs/ns3_vertical.ready"
ns3_stop="$RUN_DIR/logs/ns3_vertical.stop"
ns3_stats="$RUN_DIR/metrics/ns3_vertical.json"
control_events="$RUN_DIR/logs/control_uart.jsonl"
payload_events="$RUN_DIR/logs/payload_uart.jsonl"
control_ready="$RUN_DIR/logs/control_uart.ready"
payload_ready="$RUN_DIR/logs/payload_uart.ready"
launch_log="$RUN_DIR/logs/sitl_gazebo.log"

created_namespaces=()
managed_pids=()

namespace_exists() {
  ip netns list | awk '{print $1}' | grep -Fxq "$1"
}

terminate_managed() {
  local pid
  for pid in "${managed_pids[@]}"; do
    if kill -0 "$pid" 2>/dev/null; then
      kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    fi
  done
  for _ in {1..50}; do
    local alive=0
    for pid in "${managed_pids[@]}"; do
      kill -0 "$pid" 2>/dev/null && alive=1
    done
    ((alive == 0)) && break
    sleep 0.1
  done
  for pid in "${managed_pids[@]}"; do
    if kill -0 "$pid" 2>/dev/null; then
      kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
    fi
    wait "$pid" 2>/dev/null || true
  done
}

cleanup() {
  local status=$?
  trap - EXIT INT TERM HUP
  touch "$ns3_stop" 2>/dev/null || true
  terminate_managed
  local namespace
  for namespace in "${created_namespaces[@]}"; do
    ip netns del "$namespace" 2>/dev/null || true
  done
  rm -f "$control_sitl" "$control_adapter" "$payload_sitl" "$payload_adapter"
  rmdir "$uart_dir" 2>/dev/null || true
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP

for namespace in "$GCS_NS" "$NS3_NS" "${UAV_NAMESPACES[@]}"; do
  if namespace_exists "$namespace"; then
    printf 'Refusing to reuse existing network namespace: %s\n' "$namespace" >&2
    exit 3
  fi
done

for namespace in "$GCS_NS" "$NS3_NS" "${UAV_NAMESPACES[@]}"; do
  ip netns add "$namespace"
  created_namespaces+=("$namespace")
  ip -n "$namespace" link set lo up
done

ip link add v-bas-gcs type veth peer name v-bas-gcs-n3
ip link set v-bas-gcs netns "$GCS_NS"
ip link set v-bas-gcs-n3 netns "$NS3_NS"
ip -n "$GCS_NS" link set v-bas-gcs name eth0

for index in 1 2 3; do
  endpoint_if="v-bas-u${index}"
  ns3_if="v-bas-u${index}-n3"
  ip link add "$endpoint_if" type veth peer name "$ns3_if"
  ip link set "$endpoint_if" netns "ams-uav${index}"
  ip link set "$ns3_if" netns "$NS3_NS"
  ip -n "ams-uav${index}" link set "$endpoint_if" name eth0
done

ip -n "$NS3_NS" link add br-gcs type bridge
ip netns exec "$NS3_NS" ip tuntap add dev tap-gcs mode tap user 0
ip -n "$NS3_NS" link set v-bas-gcs-n3 master br-gcs
ip -n "$NS3_NS" link set tap-gcs master br-gcs
for index in 1 2 3; do
  ip -n "$NS3_NS" link add "br-uav${index}" type bridge
  ip netns exec "$NS3_NS" ip tuntap add dev "tap-uav${index}" mode tap user 0
  ip -n "$NS3_NS" link set "v-bas-u${index}-n3" master "br-uav${index}"
  ip -n "$NS3_NS" link set "tap-uav${index}" master "br-uav${index}"
done

ip -n "$GCS_NS" link set eth0 addrgenmode none
ip -n "$GCS_NS" link set eth0 address 02:71:00:00:00:10
ip -n "$GCS_NS" address add 10.71.0.10/24 dev eth0
ip -n "$GCS_NS" link set eth0 up
ip -n "$GCS_NS" route add default via 10.71.0.1 dev eth0
for index in 1 2 3; do
  host=$((9 + index))
  namespace="ams-uav${index}"
  ip -n "$namespace" link set eth0 addrgenmode none
  printf -v mac '02:71:01:00:00:%02x' "$host"
  ip -n "$namespace" link set eth0 address "$mac"
  ip -n "$namespace" address add "10.71.1.${host}/24" dev eth0
  ip -n "$namespace" link set eth0 up
  ip -n "$namespace" route add default via 10.71.1.1 dev eth0
done

for interface in v-bas-gcs-n3 tap-gcs br-gcs \
  v-bas-u1-n3 tap-uav1 br-uav1 \
  v-bas-u2-n3 tap-uav2 br-uav2 \
  v-bas-u3-n3 tap-uav3 br-uav3; do
  ip -n "$NS3_NS" link set "$interface" addrgenmode none
  ip -n "$NS3_NS" link set "$interface" up
done

rm -f "$ns3_ready" "$ns3_stop" "$ns3_stats" "$control_ready" "$payload_ready"
setsid env \
  RUN_DIR="$RUN_DIR" \
  PHASE=vertical \
  NS3_NS="$NS3_NS" \
  NS3_DIR="$NS3_DIR" \
  TAP_GCS=tap-gcs \
  TAP_UAVS=tap-uav1,tap-uav2,tap-uav3 \
  STATS_FILE="$ns3_stats" \
  RADIO_RATE=64kbps \
  RADIO_DELAY=5ms \
  QUEUE_MAX_PACKETS=20 \
  "$ROOT_DIR/network/ns3/run_ns3_tap_slice.sh" \
  >"$RUN_DIR/logs/ns3.log" 2>&1 &
ns3_pid=$!
managed_pids+=("$ns3_pid")

deadline=$((SECONDS + 15))
while [[ ! -f "$ns3_ready" ]]; do
  kill -0 "$ns3_pid" 2>/dev/null || {
    printf 'ns-3 exited before readiness; see %s\n' "$RUN_DIR/logs/ns3.log" >&2
    exit 1
  }
  ((SECONDS < deadline)) || {
    printf 'ns-3 readiness timed out; see %s\n' "$RUN_DIR/logs/ns3.log" >&2
    exit 1
  }
  sleep 0.1
done

setsid socat -d -d \
  "pty,raw,echo=0,link=$control_sitl,mode=660" \
  "pty,raw,echo=0,link=$control_adapter,mode=660" \
  >"$RUN_DIR/logs/control_socat.log" 2>&1 &
managed_pids+=("$!")
setsid socat -d -d \
  "pty,raw,echo=0,link=$payload_sitl,mode=660" \
  "pty,raw,echo=0,link=$payload_adapter,mode=660" \
  >"$RUN_DIR/logs/payload_socat.log" 2>&1 &
managed_pids+=("$!")

deadline=$((SECONDS + 5))
while [[ ! -e "$control_sitl" || ! -e "$control_adapter" \
     || ! -e "$payload_sitl" || ! -e "$payload_adapter" ]]; do
  ((SECONDS < deadline)) || {
    printf 'UART PTY creation timed out.\n' >&2
    exit 1
  }
  sleep 0.05
done

setsid ip netns exec ams-uav1 python3 -u "$PROBE" uart-adapter \
  --channel control \
  --tty "$control_adapter" \
  --bind 10.71.1.10:14601 \
  --peer 10.71.0.10:14600 \
  --event-log "$control_events" \
  --ready-file "$control_ready" \
  >"$RUN_DIR/logs/control_adapter.log" 2>&1 &
managed_pids+=("$!")
setsid ip netns exec ams-uav1 python3 -u "$PROBE" uart-adapter \
  --channel payload \
  --tty "$payload_adapter" \
  --bind 10.71.1.10:14701 \
  --peer 10.71.0.10:14700 \
  --event-log "$payload_events" \
  --ready-file "$payload_ready" \
  >"$RUN_DIR/logs/payload_adapter.log" 2>&1 &
managed_pids+=("$!")

deadline=$((SECONDS + 5))
while [[ ! -f "$control_ready" || ! -f "$payload_ready" ]]; do
  ((SECONDS < deadline)) || {
    printf 'UART adapter readiness timed out.\n' >&2
    exit 1
  }
  sleep 0.05
done

cd "$work_dir"
setsid ros2 launch multiagent_simulation multiagent_simulation.launch.py \
  robots_config_file:="$ROOT_DIR/network/config/communication_vertical_slice.yaml" \
  world_file:=modelflughafen/model.sdf \
  robot_model:=iris_radio_headless \
  gui:=false \
  rviz:=false \
  headless_rendering:=true \
  generate_sensor_models:=false \
  use_mapping_camera:=false \
  use_navigation_camera:=false \
  use_zed_camera:=false \
  start_mavproxy:=false \
  control_uart:="$control_sitl" \
  payload_uart:="$payload_sitl" \
  >"$launch_log" 2>&1 &
launch_pid=$!
managed_pids+=("$launch_pid")

printf 'Communication vertical slice started; run directory: %s\n' "$RUN_DIR"
set +e
python3 "$PROBE" run \
  --run-dir "$RUN_DIR" \
  --ns3-stop-file "$ns3_stop" \
  --ns3-stats-file "$ns3_stats" \
  --control-event-log "$control_events" \
  --control-tty "$control_sitl" \
  --payload-tty "$payload_sitl" \
  --mavlink-timeout-s "${BAS_MAVLINK_TIMEOUT_S:-90}"
probe_status=$?
set -e

if ((probe_status == 0)); then
  printf 'Communication vertical slice is healthy. Summary: %s\n' \
    "$RUN_DIR/metrics/communication_summary.json"
else
  printf 'Communication vertical slice failed; inspect %s and %s.\n' \
    "$RUN_DIR/metrics/communication_summary.json" "$launch_log" >&2
fi
exit "$probe_status"
