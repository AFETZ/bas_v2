#!/usr/bin/env bash
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

RUN_ID="${RUN_ID:-$(cat "$ROOT/network/swarm/.last_run" 2>/dev/null || true)}"
if [[ -z "$RUN_ID" ]]; then
  echo "No RUN_ID set and network/swarm/.last_run is missing" >&2
  exit 1
fi

SANDBOX="${SANDBOX:-danger-full-access}"
APPROVAL="${APPROVAL:-never}"
SWARM_BASE="${SWARM_BASE:-$(dirname "$ROOT")/codex-swarm/$(basename "$ROOT")/$RUN_ID}"
LOG_ROOT="$ROOT/runs/codex-swarm/$RUN_ID"

for worktree in "$SWARM_BASE"/worktrees/*; do
  [[ -d "$worktree/.git" ]] || continue
  worker="$(basename "$worktree")"
  worker_log="$LOG_ROOT/$worker"
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  mkdir -p "$worker_log"

  setsid bash -lc '
    set -euo pipefail
    cd "$1"
    mkdir -p runs/codex
    prompt_file="runs/codex/$4.continue.prompt.md"
    cat > "$prompt_file" <<'"'"'EOF'"'"'
Continue your assigned worker scope.

First reread:

- AGENTS.md
- README.md
- doc/network_radio_integration_plan.md
- network/PROGRESS.md
- network/DECISIONS.md
- network/VALIDATION_REPORT.md
- network/NEXT_TASK.md

Then continue from repository state, update the state files before stopping,
and do not rely on previous chat memory.
EOF
    set +e
    codex \
      --ask-for-approval "$2" \
      --dangerously-bypass-hook-trust \
      exec \
      --sandbox "$3" \
      --json \
      -o "runs/codex/$4.continue.final.md" \
      - < "$prompt_file"
    status=$?
    printf "%s\n" "$status" > "runs/codex/$4.continue.exitcode"
    exit "$status"
  ' bash "$worktree" "$APPROVAL" "$SANDBOX" "$worker" \
    >"$worker_log/continue-$stamp.events.jsonl" \
    2>"$worker_log/continue-$stamp.stderr.log" &

  echo "$!" > "$worker_log/pid"
  echo "continued $worker pid=$(cat "$worker_log/pid")"
done
