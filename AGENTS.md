# AGENTS.md

## Repository Intent

- Treat the repository author's README, launch files, package entry points, and existing source code as the source of truth.
- Prefer the implementation and workflows already provided by the module developer before adding wrappers, alternate UIs, or replacement control logic.
- If an author-provided command fails because an environment dependency is missing, fix the environment or dependency first instead of rewriting the feature.
- Do not replace upstream control flows such as `ros2 run multiagent_simulation move_drone` with custom controller logic unless the user explicitly asks for a new implementation.
- Keep helper scripts focused on reproducible setup and container launch. Behavioral changes to the simulation or drone control should stay aligned with the author's documented design.

## Network/Radio Long-Run Rules

- Before working on the network/radio integration, read
  `doc/network_radio_integration_plan_v2.md`, the historical
  `doc/network_radio_integration_plan.md`, `network/PROGRESS.md`,
  `network/DECISIONS.md`, `network/VALIDATION_REPORT.md`, and
  `network/NEXT_TASK.md`.
- Do not rely on conversation history as the source of truth. Treat the repository state files as durable memory after context compaction, resume, restart, or handoff.
- At every stop or checkpoint, update `network/PROGRESS.md`, `network/DECISIONS.md` when decisions changed, `network/VALIDATION_REPORT.md`, and `network/NEXT_TASK.md`.
- Never claim customer-ready status unless every P0 gate in `doc/network_radio_integration_plan_v2.md` passes and the proof is recorded in `network/VALIDATION_REPORT.md`.
- Use subagents and swarm workers for read-heavy research, scans, validation, and isolated worktree implementation. Do not let multiple agents edit the same working tree at the same time.
- For parallel implementation, use `network/swarm/run_swarm.sh` so each worker receives an isolated git worktree and branch.
- Keep external simulator dependencies outside source or under ignored directories such as `.external/`. Do not vendor ns-3, Sionna, 5G-LENA, or generated run artifacts into the repository.
