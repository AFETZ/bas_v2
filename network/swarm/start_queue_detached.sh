#!/usr/bin/env bash
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

RUN_ID="${RUN_ID:-queue_$(date -u +%Y%m%dT%H%M%SZ)}"
WORKERS="${WORKERS:-foundation,sionna,ns3,bridge,hitl,validation}"
SANDBOX="${SANDBOX:-danger-full-access}"
APPROVAL="${APPROVAL:-never}"
LOG_ROOT="$ROOT/runs/codex-swarm/$RUN_ID"
QUEUE_LOG="$LOG_ROOT/queue/queue.out"
QUEUE_PID="$LOG_ROOT/queue/pid"

mkdir -p "$LOG_ROOT/queue"
printf '%s\n' "$RUN_ID" > "$ROOT/network/swarm/.last_run"

setsid env \
  RUN_ID="$RUN_ID" \
  WORKERS="$WORKERS" \
  SANDBOX="$SANDBOX" \
  APPROVAL="$APPROVAL" \
  "$ROOT/network/swarm/run_queue.sh" \
  >"$QUEUE_LOG" 2>&1 < /dev/null &

pid="$!"
printf '%s\n' "$pid" > "$QUEUE_PID"

echo "Detached queue started"
echo "run_id=$RUN_ID"
echo "pid=$pid"
echo "workers=$WORKERS"
echo "logs=$LOG_ROOT"
echo "queue_log=$QUEUE_LOG"
echo "status: RUN_ID=$RUN_ID ./network/swarm/status_swarm.sh"
