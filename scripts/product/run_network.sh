#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUNTIME_DIR="${BAS_RUNTIME_DIR:-${XDG_RUNTIME_DIR:-/tmp}/bas-v2-${UID}}"
PID_FILE="$RUNTIME_DIR/network.pid"

command -v setsid >/dev/null 2>&1 || {
  printf 'setsid is unavailable; install util-linux.\n' >&2
  exit 2
}

mkdir -p "$RUNTIME_DIR"
if [[ -f "$PID_FILE" ]]; then
  old_pid="$(<"$PID_FILE")"
  if [[ "$old_pid" =~ ^[0-9]+$ ]] && kill -0 "$old_pid" 2>/dev/null; then
    printf 'Network runtime already appears to be running with PID %s.\n' "$old_pid" >&2
    exit 3
  fi
  rm -f "$PID_FILE"
fi

setsid "$ROOT_DIR/network/scripts/run_network_demo.sh" "$@" &
child_pid=$!
printf '%s\n' "$child_pid" > "$PID_FILE"
printf 'Network runtime started as process group %s. Use make stop from another terminal.\n' "$child_pid"

cleanup() {
  rm -f "$PID_FILE"
}

stop_child() {
  if kill -0 "$child_pid" 2>/dev/null; then
    kill -TERM -- "-$child_pid" 2>/dev/null || true
  fi
}

trap cleanup EXIT
trap stop_child INT TERM HUP
set +e
wait "$child_pid"
status=$?
set -e
exit "$status"
