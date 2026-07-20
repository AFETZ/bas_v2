#!/usr/bin/env bash
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8765}"
LOG_DIR="$ROOT/runs/codex-swarm/dashboard"
PID_FILE="$LOG_DIR/pid"
URL_FILE="$LOG_DIR/url"
LOG_FILE="$LOG_DIR/dashboard.log"

mkdir -p "$LOG_DIR"

if [[ -f "$PID_FILE" ]]; then
  old_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "$old_pid" ]] && kill -0 "$old_pid" 2>/dev/null; then
    echo "Dashboard already running"
    echo "pid=$old_pid"
    echo "url=$(cat "$URL_FILE" 2>/dev/null || echo "http://$HOST:$PORT/")"
    exit 0
  fi
fi

find_port() {
  local port="$1"
  while python3 - "$HOST" "$port" <<'PY'
import socket
import sys

host = sys.argv[1]
port = int(sys.argv[2])
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.settimeout(0.2)
    sys.exit(0 if sock.connect_ex((host, port)) != 0 else 1)
PY
  do
    echo "$port"
    return 0
  done
  for port in $(seq "$((port + 1))" "$((port + 50))"); do
    if python3 - "$HOST" "$port" <<'PY'
import socket
import sys

host = sys.argv[1]
port = int(sys.argv[2])
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.settimeout(0.2)
    sys.exit(0 if sock.connect_ex((host, port)) != 0 else 1)
PY
    then
      echo "$port"
      return 0
    fi
  done
  return 1
}

PORT="$(find_port "$PORT")"
URL="http://$HOST:$PORT/"

setsid python3 "$ROOT/network/swarm/dashboard.py" --host "$HOST" --port "$PORT" \
  >"$LOG_FILE" 2>&1 < /dev/null &

pid="$!"
printf '%s\n' "$pid" > "$PID_FILE"
printf '%s\n' "$URL" > "$URL_FILE"

echo "Dashboard started"
echo "pid=$pid"
echo "url=$URL"
echo "log=$LOG_FILE"
