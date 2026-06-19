# AGENTS.md

## Repository Intent

- Treat the repository author's README, launch files, package entry points, and existing source code as the source of truth.
- Prefer the implementation and workflows already provided by the module developer before adding wrappers, alternate UIs, or replacement control logic.
- If an author-provided command fails because an environment dependency is missing, fix the environment or dependency first instead of rewriting the feature.
- Do not replace upstream control flows such as `ros2 run multiagent_simulation move_drone` with custom controller logic unless the user explicitly asks for a new implementation.
- Keep helper scripts focused on reproducible setup and container launch. Behavioral changes to the simulation or drone control should stay aligned with the author's documented design.
