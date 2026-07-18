#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUN_DIR="${RUN_DIR:?RUN_DIR is required}"
PHASE="${PHASE:-initial}"
NS3_NS="${NS3_NS:-ams-ns3}"
NS3_DIR="${NS3_DIR:-$ROOT_DIR/.external/ns-3}"
BINARY="$NS3_DIR/build/scratch/ns3.40-ams-tap-vertical-slice-default"
READY_FILE="$RUN_DIR/logs/ns3_${PHASE}.ready"
STOP_FILE="$RUN_DIR/logs/ns3_${PHASE}.stop"
PCAP_PREFIX="$RUN_DIR/pcap/ns3_${PHASE}"

test -x "$BINARY"
rm -f "$READY_FILE" "$STOP_FILE"
mkdir -p "$RUN_DIR/logs" "$RUN_DIR/pcap"

if ((EUID != 0)); then
  printf 'FAIL live M2 ns-3 runner requires an already capability-bounded root process\n' >&2
  exit 2
fi
exec ip netns exec "$NS3_NS" env \
  LD_LIBRARY_PATH="$NS3_DIR/build/lib:${LD_LIBRARY_PATH:-}" \
  PATH="$NS3_DIR/build/src/tap-bridge:$PATH" \
  "$BINARY" \
  --tapGcs=tap-gcs \
  --tapUav=tap-uav \
  --readyFile="$READY_FILE" \
  --stopFile="$STOP_FILE" \
  --pcapPrefix="$PCAP_PREFIX" \
  --duration="${DURATION_S:-3600}" \
  --radioRate="${RADIO_RATE:-1Mbps}" \
  --radioDelay="${RADIO_DELAY:-5ms}"
