#!/usr/bin/env bash
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

RUN_ID="${RUN_ID:-$(cat "$ROOT/network/swarm/.last_run" 2>/dev/null || true)}"
if [[ -z "$RUN_ID" ]]; then
  echo "No RUN_ID set and network/swarm/.last_run is missing" >&2
  exit 1
fi

SWARM_BASE="${SWARM_BASE:-$(dirname "$ROOT")/codex-swarm/$(basename "$ROOT")/$RUN_ID}"
LOG_ROOT="$ROOT/runs/codex-swarm/$RUN_ID"

echo "Swarm run: $RUN_ID"
echo "Logs: $LOG_ROOT"
echo "Worktrees: $SWARM_BASE/worktrees"
echo

for worker_log in "$LOG_ROOT"/*; do
  [[ -d "$worker_log" ]] || continue
  worker="$(basename "$worker_log")"
  pid_file="$worker_log/pid"
  status="unknown"
  if [[ -f "$pid_file" ]]; then
    pid="$(cat "$pid_file")"
    if kill -0 "$pid" 2>/dev/null; then
      status="running pid=$pid"
    else
      status="not running last_pid=$pid"
    fi
  fi

  echo "== $worker: $status =="
  if [[ -f "$worker_log/stderr.log" ]]; then
    tail -n 8 "$worker_log/stderr.log" || true
  fi
  worktree="$SWARM_BASE/worktrees/$worker"
  if [[ -f "$worktree/runs/codex/$worker.exitcode" ]]; then
    echo "exitcode=$(cat "$worktree/runs/codex/$worker.exitcode")"
  fi
  if [[ -f "$worktree/runs/codex/$worker.final.md" ]]; then
    echo "final=$(wc -c < "$worktree/runs/codex/$worker.final.md") bytes"
  fi
  echo
done

if [[ -d "$SWARM_BASE/worktrees" ]]; then
  for worktree in "$SWARM_BASE"/worktrees/*; do
    [[ -d "$worktree/.git" ]] || continue
    echo "== git status: $(basename "$worktree") =="
    git -C "$worktree" status --short || true
    echo
  done
fi
