#!/usr/bin/env bash
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

RUN_ID="${RUN_ID:-queue_$(date -u +%Y%m%dT%H%M%SZ)}"
WORKERS_CSV="${WORKERS:-foundation,sionna,ns3,bridge,hitl,validation}"
SANDBOX="${SANDBOX:-danger-full-access}"
APPROVAL="${APPROVAL:-never}"
LOG_ROOT="$ROOT/runs/codex-swarm/$RUN_ID"

mkdir -p "$LOG_ROOT"
printf '%s\n' "$RUN_ID" > "$ROOT/network/swarm/.last_run"

IFS=',' read -r -a WORKER_LIST <<< "$WORKERS_CSV"

echo "Queue run: $RUN_ID"
echo "Workers: $WORKERS_CSV"
echo "Logs: $LOG_ROOT"

for worker in "${WORKER_LIST[@]}"; do
  echo
  echo "== starting $worker =="
  RUN_ID="$RUN_ID" WORKERS="$worker" SANDBOX="$SANDBOX" APPROVAL="$APPROVAL" \
    "$ROOT/network/swarm/run_swarm.sh"

  pid_file="$LOG_ROOT/$worker/pid"
  if [[ ! -f "$pid_file" ]]; then
    echo "Missing pid file for $worker: $pid_file" >&2
    exit 1
  fi

  pid="$(cat "$pid_file")"
  while kill -0 "$pid" 2>/dev/null; do
    sleep 15
    echo "waiting for $worker pid=$pid"
  done

  worktree="$(awk -F= '$1 == "worktree" {print $2}' "$LOG_ROOT/$worker/meta.env")"
  exitcode_file="$worktree/runs/codex/$worker.exitcode"
  exitcode="missing"
  if [[ -f "$exitcode_file" ]]; then
    exitcode="$(cat "$exitcode_file")"
  fi

  echo "== finished $worker exitcode=$exitcode =="
  "$ROOT/network/swarm/collect_swarm.sh" >/dev/null || true
done

echo
echo "Queue complete: $RUN_ID"
"$ROOT/network/swarm/collect_swarm.sh"
