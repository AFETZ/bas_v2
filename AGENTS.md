# Active live/leader task — user-authorized scope, 2026-10-26

For this prepared worktree the current task is `doc/LIVE_LEADER_PLAN.md`.
Read `doc/DEV_ENVIRONMENT.md` before starting agents or a simulator.
The user explicitly authorized multi-agent development and a lightweight native
propagation model in place of mandatory Sionna. These task-specific choices
supersede conflicting older default-profile and one-agent statements below.

- Mandatory: real five-SITL flight and live operation, operator commands through
  native ns-3, an observable GCS-selected coordinator relay/failover experiment,
  effective configuration logs, and plots of actual behavior for a paper.
- Use the explicit native Friis baseline; Sionna and accelerated batch are optional.
  No hidden propagation fallback, fabricated ACK/PER/positions, or custom MAC.
- Current BSS routes peer traffic through GCS AP. Add a native ad-hoc profile and
  on-link peer routes before claiming independent master-to-follower radio relay.
- Root + at most three active workers; each owns disjoint files/worktrees.
  Shared native scratch/runner changes have one runtime owner and sequential integration.
- Full-stack runs and mutable native builds use the integration worktree under
  `flock --nonblock --conflict-exit-code 75 /tmp/bas-paper-runtime.lock <command>`.
  Do not share writable .external/build/install or stop another agent's runtime.
- Reuse existing upstream tooling and reports. New code is limited to necessary
  adapters, GCS role/lease policy, measurements and small launch/config changes.
- Current flight is unproved. ACK accepted is not lift or task completion.
  Never disable all arming checks or relax timing thresholds to manufacture PASS.
- The GCS is the sole grant authority. No overlapping leases at final command
  admission; complete GCS isolation cannot produce a new GCS-issued appointment.
- Use the prepared bas-* skills. Worker models/effort inherit the current session.
  Keep the excluded legacy paths below excluded, including network/swarm/**.
- Preserve /home/bas/bas_v2-main and its dirty state. All work goes to assigned copies.
  Existing historical evidence stays tied to its version and configuration.
- This setup contains no completed new product experiment. Report observed facts.

# Product-First Repository Rules

## Mission

This repository develops a working five-UAV simulation stand, not a formal
certification system. Prefer observable product behavior over attestations,
status machinery, or documentary gates.

## Sources of truth

Use these sources in priority order:

1. The current user prompt.
2. `doc/PRODUCT_REQUIREMENTS.md`.
3. `doc/PRODUCT_ARCHITECTURE.md`.
4. `doc/DEVELOPMENT_PLAN.md`.
5. `network/STATUS.md`.
6. Task-specific source files.

## Integration authenticity

- Upstream tools are authoritative in their own domains.
- Adapters may transform framing, coordinates, and timestamps, never outcomes.
- Integrated product runs forbid mocks and fallback formulas.
- Native baselines and custom policies must remain independently runnable.
- Test traffic may be synthetic; ACK, telemetry, Sionna values, and packet outcomes may not.
- Every non-native model must be explicit in configuration and reported results.

## Legacy exclusion

Without a direct user instruction, do not read, modify, or run:

- `archive/acceptance_v3/**`.
- Old `network_radio_integration_plan*` documents.
- `network/validation/evidence.py`.
- `network/validation/evidence_attestation.py`.
- `network/validation/qualification_identity.py`.
- `network/config/qualification_*`.
- `network/config/*evidence*`.
- `network/config/provenance_schema.json`.
- `network/config/m0_test_manifest.json`.
- `network/config/component_acceptance_profiles.json`.
- `network/scripts/attest_*`.
- `network/scripts/finalize_*`.
- `scripts/acceptance_entrypoint.sh`.
- `scripts/run_acceptance_container.sh`.
- Old M0-M8 status validators.
- `network/swarm/**`.

## Development rules

- One agent works on one product task.
- Do not use subagents unless the user directly requests them.
- Reproduce the problem first.
- Make the smallest change that fixes it.
- Run only tests affected by the change.
- Do not run the full regression suite after a local change.
- Do not refactor adjacent components for cosmetic reasons.
- Do not create new infrastructure when the existing runtime can be fixed directly.
- Do not create a validator before a working product path exists.
- Markdown, receipts, JSON schemas, and PASS flags are not proof that the product works.
- Confirm work with observable behavior, CSV, PCAP, a log, a metric, or a command.
- After three identical failed attempts, record the root cause and choose a different minimal approach.
- Do not repeat a successful expensive test when changed files cannot affect it.
- Rebuild Docker only after Dockerfile, lock-file, or system-dependency changes.
- Update only `network/STATUS.md`, once at the end of a task.
- Do not create status-only commits.
- Keep each commit to one completed product change.
- Do not make false readiness claims.
- Do not require cryptographic proof of results.

## Testing rules

Select the minimum checks from `network/TEST_MATRIX.md`.

## Documentation limits

Keep active documents short:

- Requirements: at most 300 lines.
- Architecture: at most 400 lines.
- Development plan: at most 300 lines.
- Status: at most 150 lines.
- Task report: at most 100 lines.

## Current delivery

Use `main` as the canonical development branch and immutable `test-baseline-*`
tags for repeatable tests. Historical RC1 scope is in `doc/DELIVERY_SCOPE.md`;
record actual results and remaining blockers in `network/STATUS.md`. Preserve user edits.
Keep native ns-3.48 Wi-Fi PHY/MAC with in-process Sionna RT as the default.
No custom PER/ACK/MAC, direct SITL bypass, or silent propagation fallback.
Dependencies remain pinned; use upstream APIs and thin adapters. Full traces
and external scene assets stay outside Git. Physical flight-control hardware
tests need the separate authorization stated in the user request.
