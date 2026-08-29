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
