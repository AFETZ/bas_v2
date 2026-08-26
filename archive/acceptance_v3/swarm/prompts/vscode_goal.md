Use this from the VS Code Codex panel if you want one interactive controller
thread:

```text
/goal
Implement doc/network_radio_integration_plan.md to customer-ready status.

Before every step, reread AGENTS.md and the network state files:
- network/PROGRESS.md
- network/DECISIONS.md
- network/VALIDATION_REPORT.md
- network/NEXT_TASK.md

Use repository files as durable memory. Do not rely on chat history.
Use subagents for read-only research and review. For parallel implementation,
run ./network/swarm/run_swarm.sh from the VS Code terminal so workers use
isolated git worktrees.

Done only when all P0 gates in the plan pass and proof is recorded in
network/VALIDATION_REPORT.md and artifacts/.
```
