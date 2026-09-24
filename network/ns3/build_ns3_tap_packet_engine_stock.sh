#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NS3_DIR="${NS3_STOCK_DIR:-$ROOT_DIR/.external/ns-3-stock}"
SOURCE="$ROOT_DIR/network/ns3/scratch/ams-tap-packet-engine-stock.cc"
TARGET="$NS3_DIR/scratch/ams-tap-packet-engine-stock.cc"
PATCH="$ROOT_DIR/network/ns3/patches/ns-3.40-csma-global-queue-scheduler.patch"
MODULES="bridge,core,csma,flow-monitor,internet,mobility,network,stats,tap-bridge"

if [[ ! -d "$NS3_DIR/.git" ]]; then
  [[ ! -e "$NS3_DIR" ]] || { printf 'FAIL stock tree path already exists: %s\n' "$NS3_DIR" >&2; exit 2; }
  git clone --depth 1 --branch ns-3.40 https://gitlab.com/nsnam/ns-3-dev.git "$NS3_DIR"
fi

EXCLUDE_FILE="$(git -C "$NS3_DIR" rev-parse --git-path info/exclude)"
grep -qxF 'scratch/ams-tap-packet-engine-stock.cc' "$EXCLUDE_FILE" 2>/dev/null || \
  printf 'scratch/ams-tap-packet-engine-stock.cc\n' >> "$EXCLUDE_FILE"
rm -f "$TARGET"
git -C "$NS3_DIR" diff --exit-code
[[ -z "$(git -C "$NS3_DIR" status --short)" ]] || {
  printf 'FAIL stock source tree is not pristine before build\n' >&2
  git -C "$NS3_DIR" status --short >&2
  exit 2
}
[[ "$(tr -d '[:space:]' < "$NS3_DIR/VERSION")" == "3.40" ]] || {
  printf 'FAIL stock source tree is not ns-3.40\n' >&2
  exit 2
}
if git -C "$NS3_DIR" apply --check --reverse "$PATCH" >/dev/null 2>&1; then
  printf 'FAIL the centralized scheduler patch is present in stock source\n' >&2
  exit 2
fi

CACHE_FILE="$NS3_DIR/cmake-cache/CMakeCache.txt"
if [[ -f "$CACHE_FILE" ]] && ! grep -qxF "CMAKE_HOME_DIRECTORY:INTERNAL=$NS3_DIR" "$CACHE_FILE"; then
  # Build outputs are path-sensitive.  Clear only generated ns-3 artifacts when
  # the same clean source is mounted at a different runtime path.
  (
    cd "$NS3_DIR"
    ./ns3 clean
  )
fi

install -m 0644 "$SOURCE" "$TARGET"
(
  cd "$NS3_DIR"
  ./ns3 configure --disable-examples --disable-tests --enable-modules="$MODULES"
  ./ns3 build tap-creator
  ./ns3 build scratch/ams-tap-packet-engine-stock
)

BINARY="$NS3_DIR/build/scratch/ns3.40-ams-tap-packet-engine-stock-default"
test -x "$BINARY"
test -x "$NS3_DIR/build/src/tap-bridge/ns3.40-tap-creator-default"
printf 'Built native stock ns-3.40 target: %s\n' "$BINARY"
