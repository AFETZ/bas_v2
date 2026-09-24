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
SUMMARY="$LOG_ROOT/SWARM_SUMMARY.md"

mkdir -p "$LOG_ROOT"

{
  echo "# Swarm Summary"
  echo
  echo "- Run ID: \`$RUN_ID\`"
  echo "- Logs: \`$LOG_ROOT\`"
  echo "- Worktrees: \`$SWARM_BASE/worktrees\`"
  echo

  if [[ -d "$SWARM_BASE/worktrees" ]]; then
    for worktree in "$SWARM_BASE"/worktrees/*; do
      [[ -d "$worktree/.git" ]] || continue
      worker="$(basename "$worktree")"
      echo "## $worker"
      echo
      echo "- Worktree: \`$worktree\`"
      echo "- Branch: \`$(git -C "$worktree" branch --show-current)\`"
      echo
      echo "### Status"
      echo
      echo '```text'
      git -C "$worktree" status --short || true
      echo '```'
      echo
      echo "### Diff Stat"
      echo
      echo '```text'
      git -C "$worktree" diff --stat || true
      echo '```'
      echo
      if [[ -f "$worktree/runs/codex/$worker.final.md" ]]; then
        echo "### Final Message"
        echo
        sed -n '1,160p' "$worktree/runs/codex/$worker.final.md"
        echo
      fi
    done
  fi
} > "$SUMMARY"

echo "Wrote $SUMMARY"
