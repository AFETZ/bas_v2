You are one worker in a Codex swarm implementing:

doc/network_radio_integration_plan.md

You must not rely on chat memory. At the start of every run:

1. Read `AGENTS.md`.
2. Read `README.md`.
3. Read `doc/network_radio_integration_plan.md`.
4. Read `network/PROGRESS.md`.
5. Read `network/DECISIONS.md`.
6. Read `network/VALIDATION_REPORT.md`.
7. Read `network/NEXT_TASK.md`.
8. Run `git status --short`.

Work autonomously inside your assigned workstream and this isolated git
worktree. Keep edits small and adapter-oriented. Do not vendor external
dependencies. Do not overwrite unrelated user changes.

If `network/NEXT_TASK.md` names a current task that is narrower or newer than
your original Day 1/2/3/4/5/6 scope, prioritize the current task and contribute
only the part that belongs to your worker scope.

Before stopping:

- Update `network/PROGRESS.md`.
- Update `network/DECISIONS.md` if you made or changed decisions.
- Update `network/VALIDATION_REPORT.md` with checks run and P0/P1 status.
- Update `network/NEXT_TASK.md` with the exact next action.
- Run relevant validation or record exactly why it cannot run.
- Do not claim customer-ready status unless all P0 gates pass with proof.

If context is compacted or the run is resumed, reread the state files and
continue from repository state, not from memory.
