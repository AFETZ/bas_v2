# Codex Swarm Runner

This folder lets you start multiple Codex workers from the VS Code terminal
without putting every worker in the same working tree.

Why this exists:

- Long-running agents lose chat detail after context compaction.
- The durable memory must live in repository files.
- Parallel agents should not edit the same checkout at the same time.
- Each worker gets its own git worktree and branch.

## Start From VS Code

Use **Terminal: Run Task** and select:

```text
Codex Swarm: Start
```

For a safer run that cannot write outside the workspace sandbox, select:

```text
Codex Swarm: Start Safe Sandbox
```

You can also open a VS Code terminal at the repository root and run:

```bash
./network/swarm/run_swarm.sh
```

Default workers:

- `foundation`
- `sionna`
- `ns3`
- `bridge`
- `hitl`
- `validation`

If the local Codex CLI only keeps one `codex exec` alive at a time, run workers
one by one:

```bash
./network/swarm/run_worker.sh foundation
./network/swarm/run_worker.sh sionna
./network/swarm/run_worker.sh ns3
./network/swarm/run_worker.sh bridge
./network/swarm/run_worker.sh hitl
./network/swarm/run_worker.sh validation
```

This still keeps the work split by role and worktree. It is slower than true
parallel execution, but more reliable on local CLI/auth setups that limit
concurrent active sessions.

Or let the queue supervisor run them one after another:

```bash
./network/swarm/run_queue.sh
```

For a week-long unattended run, use the detached queue launcher. This starts
the queue in a separate process session, writes the supervisor PID, and keeps
logs under the run directory even if the launching terminal is closed:

```bash
./network/swarm/start_queue_detached.sh
```

From VS Code, select:

```text
Codex Queue: Start Detached
```

The script creates worktrees under:

```text
../codex-swarm/Ardupilot_Multiagent_Simulation/<run_id>/
```

Each worker writes logs under:

```text
runs/codex-swarm/<run_id>/
```

## Check Status

```bash
./network/swarm/status_swarm.sh
```

## Dashboard

Start the local monitoring page:

```bash
./network/swarm/start_dashboard.sh
```

From VS Code, select:

```text
Codex Swarm: Dashboard
```

The dashboard shows the active run, worker status, recent agent messages,
changed worktree counts, and live tails for `events`, `stderr`, `queue`,
`final`, and `meta` logs.

## Continue Workers

```bash
./network/swarm/continue_swarm.sh
```

This starts new Codex turns in the same worker worktrees. The prompt forces
workers to recover state from `network/NEXT_TASK.md` and the other state files
instead of relying on chat memory.

## Stop Workers

```bash
./network/swarm/stop_swarm.sh
```

## Collect A Summary

```bash
./network/swarm/collect_swarm.sh
```

This writes:

```text
runs/codex-swarm/<run_id>/SWARM_SUMMARY.md
```

## Cost And Safety

This can consume substantial tokens and compute. The default sandbox is
`danger-full-access` because the radio/network work may need Docker, Linux
networking, external dependency setup, and ignored directories outside the
source tree. Override it when you want a safer dry run:

```bash
SANDBOX=workspace-write ./network/swarm/run_swarm.sh
```

Do not merge worker branches blindly. Review diffs and integrate one workstream
at a time.
