# Legacy Acceptance Archive

The former formal acceptance and requalification workflow is inactive. It is
retained only for historical analysis. Do not read it by default, run it by
default, or use it as a prerequisite for product work.

## Archived paths

- `archive/acceptance_v3/network_radio_integration_plan*.md`
- `archive/acceptance_v3/PROGRESS.md`
- `archive/acceptance_v3/VALIDATION_REPORT.md`
- `archive/acceptance_v3/NEXT_TASK.md`
- `archive/acceptance_v3/STATUS_ONLY_CHAIN.md`
- `archive/acceptance_v3/DECISIONS.md`
- `archive/acceptance_v3/codex_agents/`
- `archive/acceptance_v3/swarm/`

## Inactive paths left in place

These files remain at their original paths because runtime modules, tests, or
imports may still depend on their locations. They are excluded from the active
workflow and should be separated only in a dedicated product-code change:

- `network/validation/evidence.py`
- `network/validation/evidence_attestation.py`
- `network/validation/qualification_identity.py`
- other `network/validation/validate_*` and M0-M8 validation modules
- `network/config/qualification_*`
- `network/config/*evidence*`
- `network/config/provenance_schema.json`
- `network/config/m0_test_manifest.json`
- `network/config/component_acceptance_profiles.json`
- `network/scripts/attest_*`
- `network/scripts/finalize_*`
- `network/scripts/run_m0_*` and M0-M8 validators/orchestrators
- `scripts/acceptance_entrypoint.sh`
- `scripts/run_acceptance_container.sh`
- acceptance-, evidence-, qualification-, and status-validator tests

Some active runtime scripts still contain legacy terminology or optional
validation branches. They are not proof of product behavior. Product tasks may
cleanly split those mixed files when a failing runtime path demonstrates the
need; this process-reset commit does not move them and risk breaking imports.
