#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NS3_DIR="${NS3_DIR:-$ROOT_DIR/.external/ns-3}"
SRC="$ROOT_DIR/network/ns3/scratch/ams-radio-core.cc"
DST_DIR="$NS3_DIR/scratch"
DST="$DST_DIR/ams-radio-core.cc"
RECEIPT_TOOL="$ROOT_DIR/network/ns3/ns3_build_receipt.py"
REQUIRED_MODULES="applications,core,csma,flow-monitor,internet,mobility,network,traffic-control"

if [[ ! -f "$SRC" ]]; then
  printf 'FAIL ns-3 packet-core source missing: %s\n' "$SRC" >&2
  exit 2
fi
if [[ ! -d "$NS3_DIR" ]]; then
  printf 'FAIL ns-3 directory missing: %s\n' "$NS3_DIR" >&2
  printf 'Install/build ns-3 under .external/ns-3 or set NS3_DIR. Do not vendor it into source.\n' >&2
  exit 2
fi
if [[ ! -x "$NS3_DIR/ns3" ]]; then
  printf 'FAIL pinned ns-3 CMake launcher is missing: %s/ns3\n' "$NS3_DIR" >&2
  exit 2
fi

NS3_VERSION="$(tr -d '[:space:]' < "$NS3_DIR/VERSION")"
if [[ "$NS3_VERSION" != "3.40" ]]; then
  printf 'FAIL ns-3 VERSION must be exactly 3.40, observed: %s\n' "$NS3_VERSION" >&2
  exit 2
fi
NS3_BINARY="$NS3_DIR/build/scratch/ns${NS3_VERSION}-ams-radio-core-default"

mkdir -p "$DST_DIR"
cp "$SRC" "$DST"
printf 'Copied packet-core scratch program to %s\n' "$DST"

(
  cd "$NS3_DIR"
  ./ns3 configure \
    --disable-examples \
    --disable-tests \
    --enable-modules="$REQUIRED_MODULES"
  # Force this scratch target through compile and link.  A successful no-op
  # build must not mint a new receipt around an old executable.
  target_clean="cmake-cache/scratch/CMakeFiles/scratch_ams-radio-core.dir/cmake_clean.cmake"
  if [[ -f "$target_clean" ]]; then
    cmake -P "$target_clean"
  else
    rm -f "$NS3_BINARY"
  fi
  ./ns3 build scratch/ams-radio-core
)

if [[ ! -x "$NS3_BINARY" ]]; then
  printf 'FAIL built packet-core executable is missing: %s\n' "$NS3_BINARY" >&2
  exit 2
fi

RECEIPT="$(python3 "$RECEIPT_TOOL" create \
  --ns3-dir "$NS3_DIR" \
  --program ams-radio-core \
  --project-source "$SRC" \
  --copied-source "$DST" \
  --executable "$NS3_BINARY" \
  --required-modules "$REQUIRED_MODULES")"
printf 'Built ns-3 packet core with immutable receipt: %s\n' "$RECEIPT"
