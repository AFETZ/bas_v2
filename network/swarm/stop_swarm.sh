#!/usr/bin/env bash
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

RUN_ID="${RUN_ID:-$(cat "$ROOT/network/swarm/.last_run" 2>/dev/null || true)}"
if [[ -z "$RUN_ID" ]]; then
  echo "No RUN_ID set and network/swarm/.last_run is missing" >&2
  exit 1
fi

LOG_ROOT="$ROOT/runs/codex-swarm/$RUN_ID"

for pid_file in "$LOG_ROOT"/*/pid; do
  [[ -f "$pid_file" ]] || continue
  pid="$(cat "$pid_file")"
  if kill -0 "$pid" 2>/dev/null; then
    echo "stopping process group pid=$pid"
    kill -- "-$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true
  fi
done

sleep 2

for pid_file in "$LOG_ROOT"/*/pid; do
  [[ -f "$pid_file" ]] || continue
  pid="$(cat "$pid_file")"
  if kill -0 "$pid" 2>/dev/null; then
    echo "force stopping process group pid=$pid"
    kill -9 -- "-$pid" 2>/dev/null || kill -9 "$pid" 2>/dev/null || true
  fi
done

if [[ -d "$LOG_ROOT" ]]; then
  for meta in "$LOG_ROOT"/*/meta.env; do
    [[ -f "$meta" ]] || continue
    worktree="$(awk -F= '$1 == "worktree" {print $2}' "$meta")"
    [[ -n "$worktree" ]] || continue
    pkill -TERM -f "$worktree.*codex" 2>/dev/null || true
  done
fi
