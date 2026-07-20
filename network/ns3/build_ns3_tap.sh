#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NS3_DIR="${NS3_DIR:-$ROOT_DIR/.external/ns-3}"
SOURCE="$ROOT_DIR/network/ns3/scratch/ams-tap-vertical-slice.cc"
TARGET="$NS3_DIR/scratch/ams-tap-vertical-slice.cc"
RECEIPT_TOOL="$ROOT_DIR/network/ns3/ns3_build_receipt.py"
REQUIRED_MODULES="applications,bridge,core,csma,flow-monitor,internet,mobility,network,stats,tap-bridge,traffic-control"

NS3_DIR="$NS3_DIR" "$ROOT_DIR/network/ns3/setup_ns3_core.sh"
if [[ ! -x "$NS3_DIR/ns3" ]]; then
  printf 'FAIL pinned ns-3 CMake launcher is missing: %s/ns3\n' "$NS3_DIR" >&2
  exit 2
fi
if [[ ! -f "$SOURCE" ]]; then
  printf 'FAIL ns-3 TapBridge source is missing: %s\n' "$SOURCE" >&2
  exit 2
fi

NS3_VERSION="$(tr -d '[:space:]' < "$NS3_DIR/VERSION")"
if [[ "$NS3_VERSION" != "3.40" ]]; then
  printf 'FAIL ns-3 VERSION must be exactly 3.40, observed: %s\n' "$NS3_VERSION" >&2
  exit 2
fi
NS3_BINARY="$NS3_DIR/build/scratch/ns${NS3_VERSION}-ams-tap-vertical-slice-default"
TAP_CREATOR="$NS3_DIR/build/src/tap-bridge/ns${NS3_VERSION}-tap-creator-default"

cp "$SOURCE" "$TARGET"
(
  cd "$NS3_DIR"
  ./ns3 configure \
    --disable-examples \
    --disable-tests \
    --enable-modules="$REQUIRED_MODULES"
  # Force this scratch target through compile and link before attestation.
  target_clean="cmake-cache/scratch/CMakeFiles/scratch_ams-tap-vertical-slice.dir/cmake_clean.cmake"
  if [[ -f "$target_clean" ]]; then
    cmake -P "$target_clean"
  else
    rm -f "$NS3_BINARY"
  fi
  ./ns3 build scratch/ams-tap-vertical-slice
  ./ns3 build tap-creator
)

test -e "$NS3_DIR/build/include/ns3/tap-bridge-module.h"
test -x "$TAP_CREATOR"
test -x "$NS3_BINARY"

RECEIPT="$(python3 "$RECEIPT_TOOL" create \
  --ns3-dir "$NS3_DIR" \
  --program ams-tap-vertical-slice \
  --project-source "$SOURCE" \
  --copied-source "$TARGET" \
  --executable "$NS3_BINARY" \
  --required-modules "$REQUIRED_MODULES")"
printf 'Built ns-3 TapBridge vertical slice with immutable receipt: %s\n' "$RECEIPT"
