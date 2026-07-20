#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NS3_DIR="${NS3_DIR:-$ROOT_DIR/.external/ns-3}"
PACKET_SOURCE="$ROOT_DIR/network/ns3/scratch/ams-tap-packet-engine.cc"
PACKET_TARGET="$NS3_DIR/scratch/ams-tap-packet-engine.cc"
VERTICAL_SOURCE="$ROOT_DIR/network/ns3/scratch/ams-tap-vertical-slice.cc"
VERTICAL_TARGET="$NS3_DIR/scratch/ams-tap-vertical-slice.cc"
RECEIPT_TOOL="$ROOT_DIR/network/ns3/ns3_build_receipt.py"
# This exact canonical union is shared with the locked M2 build so both
# scratch executables and their receipts coexist in one pinned ns-3 tree.
# Module availability does not authorize this engine to instantiate ns-3
# Applications; the C++ source and focused tests enforce that behavior rule.
REQUIRED_MODULES="applications,bridge,core,csma,flow-monitor,internet,mobility,network,stats,tap-bridge,traffic-control"

# Build hosts used by the qualification workflow may expose a read-only HOME.
# Redirect and disable the optional compiler cache so the exact source build is
# independent of host-global ccache state.
CCACHE_STATE_DIR="${TMPDIR:-/tmp}/ams-ns3-packet-engine-ccache-${UID:-0}"
mkdir -p "$CCACHE_STATE_DIR/cache" "$CCACHE_STATE_DIR/tmp"
export CCACHE_CONFIGPATH="${CCACHE_CONFIGPATH:-$CCACHE_STATE_DIR/ccache.conf}"
export CCACHE_DIR="${CCACHE_DIR:-$CCACHE_STATE_DIR/cache}"
export CCACHE_TEMPDIR="${CCACHE_TEMPDIR:-$CCACHE_STATE_DIR/tmp}"
export CCACHE_DISABLE="${CCACHE_DISABLE:-1}"

NS3_DIR="$NS3_DIR" "$ROOT_DIR/network/ns3/setup_ns3_core.sh"
test -x "$NS3_DIR/ns3"
test -f "$PACKET_SOURCE"
test -f "$VERTICAL_SOURCE"

NS3_VERSION="$(tr -d '[:space:]' < "$NS3_DIR/VERSION")"
if [[ "$NS3_VERSION" != "3.40" ]]; then
  printf 'FAIL ns-3 VERSION must be exactly 3.40, observed: %s\n' "$NS3_VERSION" >&2
  exit 2
fi

cp "$PACKET_SOURCE" "$PACKET_TARGET"
cp "$VERTICAL_SOURCE" "$VERTICAL_TARGET"
(
  cd "$NS3_DIR"
  ./ns3 configure \
    --disable-examples \
    --disable-tests \
    --enable-modules="$REQUIRED_MODULES"
  for program in ams-tap-vertical-slice ams-tap-packet-engine; do
    target_clean="cmake-cache/scratch/CMakeFiles/scratch_${program}.dir/cmake_clean.cmake"
    if [[ -f "$target_clean" ]]; then
      cmake -P "$target_clean"
    else
      rm -f "$NS3_DIR/build/scratch/ns${NS3_VERSION}-${program}-default"
    fi
  done
  ./ns3 build scratch/ams-tap-vertical-slice
  ./ns3 build scratch/ams-tap-packet-engine
  ./ns3 build tap-creator
)

BINARY="$NS3_DIR/build/scratch/ns${NS3_VERSION}-ams-tap-packet-engine-default"
VERTICAL_BINARY="$NS3_DIR/build/scratch/ns${NS3_VERSION}-ams-tap-vertical-slice-default"
TAP_CREATOR="$NS3_DIR/build/src/tap-bridge/ns${NS3_VERSION}-tap-creator-default"
test -x "$BINARY"
test -x "$VERTICAL_BINARY"
test -x "$TAP_CREATOR"
test -e "$NS3_DIR/build/include/ns3/tap-bridge-module.h"

VERTICAL_RECEIPT="$(python3 "$RECEIPT_TOOL" create \
  --ns3-dir "$NS3_DIR" \
  --program ams-tap-vertical-slice \
  --project-source "$VERTICAL_SOURCE" \
  --copied-source "$VERTICAL_TARGET" \
  --executable "$VERTICAL_BINARY" \
  --required-modules "$REQUIRED_MODULES")"
PACKET_RECEIPT="$(python3 "$RECEIPT_TOOL" create \
  --ns3-dir "$NS3_DIR" \
  --program ams-tap-packet-engine \
  --project-source "$PACKET_SOURCE" \
  --copied-source "$PACKET_TARGET" \
  --executable "$BINARY" \
  --required-modules "$REQUIRED_MODULES")"
printf 'Built exact ns-%s shared TapBridge targets; vertical receipt: %s; packet receipt: %s\n' \
  "$NS3_VERSION" "$VERTICAL_RECEIPT" "$PACKET_RECEIPT"
