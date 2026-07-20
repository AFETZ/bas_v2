# Worker Scope: Validation And Handoff

Implement Day 6/7 validation and customer handoff scaffolding.

Primary files/directories:

- `network/scripts/run_validation.sh`
- `network/scripts/collect_artifacts.sh`
- `network/config/validation_matrix.yaml`
- `network/VALIDATION_REPORT.md`
- customer bundle documentation
- metrics summary schema

Do not fake passing gates. If a dependency or component is missing, record the
exact blocker and the command that proves it.

Done when:

- Validation command exists.
- Artifact collection command exists.
- P0/P1 gates are represented in machine-readable or script-checkable form.
- Customer-ready status remains false until evidence proves every P0 gate.
