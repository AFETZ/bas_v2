#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUN_DIR="${RUN_DIR:?RUN_DIR is required}"
UAV_COUNT="${UAV_COUNT:?UAV_COUNT in 1..5 is required}"
EVENT_EPOCH="${EVENT_EPOCH:?EVENT_EPOCH is required}"
NS3_NS="${NS3_NS:-ams-ns3}"
NS3_DIR="${NS3_STOCK_DIR:-$ROOT_DIR/.external/ns-3-stock}"
BINARY="$NS3_DIR/build/scratch/ns3.40-ams-tap-packet-engine-stock-default"
CONFIG_TOOL="$ROOT_DIR/network/ns3/tap_packet_engine_stock_config.py"
EVENTS_FILE="${EVENTS_FILE:-$RUN_DIR/logs/ns3_packet_events.jsonl}"
PCAP_PREFIX="${PCAP_PREFIX:-$RUN_DIR/pcap/ns3-packet-engine-stock}"
READY_FILE="${READY_FILE:-$RUN_DIR/logs/ns3_packet_engine.ready}"
STOP_FILE="${STOP_FILE:-$RUN_DIR/logs/ns3_packet_engine.stop}"
CONFIG_REPORT="${CONFIG_REPORT:-$RUN_DIR/logs/ns3_packet_engine_config.json}"
ARGV_FILE="${ARGV_FILE:-$RUN_DIR/logs/ns3_packet_engine.argv}"
RADIO_FILE="${RADIO_FILE:-$ROOT_DIR/network/config/radio_24ghz_town01.yaml}"
QOS_FILE="${QOS_FILE:-$ROOT_DIR/network/config/communication_qos.yaml}"
SELF_TEST="${SELF_TEST:-0}"

test -x "$BINARY"
test -f "$CONFIG_TOOL"
mkdir -p "$RUN_DIR/logs" "$RUN_DIR/pcap" "$RUN_DIR/metrics"
rm -f "$READY_FILE" "$STOP_FILE"

CONFIG_ARGS=(
  --uav-count "$UAV_COUNT" --duration-ms "${DURATION_MS:-3600000}"
  --seed "${NS3_SEED:-42}" --run "${NS3_RUN:-1}" --event-epoch "$EVENT_EPOCH"
  --tap-gcs "${TAP_GCS:-tap-gcs}" --events-file "$EVENTS_FILE"
  --pcap-prefix "$PCAP_PREFIX" --radio "$RADIO_FILE" --qos "$QOS_FILE"
  --medium-access-mode stock_ns3_csma
)
if [[ -n "${TAP_UAVS:-}" ]]; then CONFIG_ARGS+=(--tap-uavs "$TAP_UAVS"); fi
if [[ "$SELF_TEST" == "1" ]]; then CONFIG_ARGS+=(--self-test --self-test-burst "${SELF_TEST_BURST:-1}"); fi
if [[ "${SIONNA_IPC_ENABLED:-0}" == "1" ]]; then
  CONFIG_ARGS+=(--sionna-ipc --sionna-state-file "${SIONNA_STATE_FILE:-$RUN_DIR/logs/sionna_applied_states.jsonl}" --sionna-poll-interval-ms "${SIONNA_POLL_INTERVAL_MS:-1}")
  if [[ -n "${SIONNA_MAX_STATE_TTL_MS:-}" ]]; then CONFIG_ARGS+=(--sionna-max-state-ttl-ms "$SIONNA_MAX_STATE_TTL_MS"); fi
fi
python3 "$CONFIG_TOOL" "${CONFIG_ARGS[@]}" --json-output "$CONFIG_REPORT" --print-argv > "$ARGV_FILE"
mapfile -t ENGINE_ARGS < "$ARGV_FILE"
ENGINE_ARGS+=(--readyFile="$READY_FILE" --stopFile="$STOP_FILE")
python3 - "$CONFIG_REPORT" "$RUN_DIR/metrics/medium_access_run.json" <<'PY'
import json, sys
from pathlib import Path
source, output = map(Path, sys.argv[1:])
value = json.loads(source.read_text(encoding="utf-8"))
resolved = value["resolved"]
keys = (
    "medium_access_mode", "ns3_source_version", "ns3_tree_kind",
    "upstream_patch_applied", "global_scheduler_enabled",
    "ingress_shaping_enabled", "radio_mapping_mode", "propagation_backend",
)
output.write_text(json.dumps({key: resolved[key] for key in keys}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

if [[ "$SELF_TEST" == "1" ]]; then
  exec env LD_LIBRARY_PATH="$NS3_DIR/build/lib:${LD_LIBRARY_PATH:-}" PATH="$NS3_DIR/build/src/tap-bridge:$PATH" "$BINARY" "${ENGINE_ARGS[@]}"
fi
if ((EUID != 0)); then
  printf 'FAIL live stock packet-engine runner requires root in the network namespace\n' >&2
  exit 2
fi
exec ip netns exec "$NS3_NS" env LD_LIBRARY_PATH="$NS3_DIR/build/lib:${LD_LIBRARY_PATH:-}" PATH="$NS3_DIR/build/src/tap-bridge:$PATH" "$BINARY" "${ENGINE_ARGS[@]}"
