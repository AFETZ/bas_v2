#!/usr/bin/env bash
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

if ! command -v codex >/dev/null 2>&1; then
  echo "codex executable not found in PATH" >&2
  exit 1
fi

RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
BASE_REF="${BASE_REF:-HEAD}"
SANDBOX="${SANDBOX:-danger-full-access}"
APPROVAL="${APPROVAL:-never}"
WORKERS_CSV="${WORKERS:-foundation,sionna,ns3,bridge,hitl,validation}"
SWARM_BASE="${SWARM_BASE:-$(dirname "$ROOT")/codex-swarm/$(basename "$ROOT")/$RUN_ID}"
LOG_ROOT="$ROOT/runs/codex-swarm/$RUN_ID"

mkdir -p "$SWARM_BASE/worktrees" "$LOG_ROOT"
printf '%s\n' "$RUN_ID" > "$ROOT/network/swarm/.last_run"

IFS=',' read -r -a WORKER_LIST <<< "$WORKERS_CSV"

sync_bootstrap_files() {
  local worktree="$1"
  mkdir -p "$worktree/doc" "$worktree/network" "$worktree/.codex"
  rsync -a --delete "$ROOT/.codex/" "$worktree/.codex/"
  rsync -a "$ROOT/.gitignore" "$worktree/.gitignore"
  rsync -a "$ROOT/AGENTS.md" "$worktree/AGENTS.md"
  rsync -a "$ROOT/doc/network_radio_integration_plan.md" "$worktree/doc/network_radio_integration_plan.md"
  rsync -a "$ROOT/network/" "$worktree/network/"
}

start_worker() {
  local worker="$1"
  local prompt="$ROOT/network/swarm/prompts/${worker}.md"
  local worktree="$SWARM_BASE/worktrees/$worker"
  local branch="codex/radio-${worker}-${RUN_ID}"
  local worker_log="$LOG_ROOT/$worker"

  if [[ ! -f "$prompt" ]]; then
    echo "Missing prompt for worker: $prompt" >&2
    return 1
  fi

  mkdir -p "$worker_log"

  if [[ ! -d "$worktree/.git" ]]; then
    git worktree add -b "$branch" "$worktree" "$BASE_REF" >"$worker_log/worktree.log" 2>&1
  fi

  sync_bootstrap_files "$worktree"

  {
    echo "worker=$worker"
    echo "branch=$branch"
    echo "worktree=$worktree"
    echo "sandbox=$SANDBOX"
    echo "approval=$APPROVAL"
    echo "started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } > "$worker_log/meta.env"

  setsid bash -lc '
    set -euo pipefail
    cd "$1"
    mkdir -p runs/codex
    prompt_file="runs/codex/$2.prompt.md"
    {
      cat network/swarm/prompts/swarm_common.md
      printf "\n\n"
      cat "network/swarm/prompts/$2.md"
    } > "$prompt_file"
    set +e
    codex \
      --ask-for-approval "$3" \
      --dangerously-bypass-hook-trust \
      exec \
      --sandbox "$4" \
      --json \
      -o "runs/codex/$2.final.md" \
      - < "$prompt_file"
    status=$?
    printf "%s\n" "$status" > "runs/codex/$2.exitcode"
    exit "$status"
  ' bash "$worktree" "$worker" "$APPROVAL" "$SANDBOX" \
    >"$worker_log/events.jsonl" \
    2>"$worker_log/stderr.log" &

  echo "$!" > "$worker_log/pid"
  echo "started $worker pid=$(cat "$worker_log/pid") worktree=$worktree"
}

for worker in "${WORKER_LIST[@]}"; do
  start_worker "$worker"
done

echo
echo "Swarm run: $RUN_ID"
echo "Logs: $LOG_ROOT"
echo "Worktrees: $SWARM_BASE/worktrees"
echo "Status: ./network/swarm/status_swarm.sh"
