# Worker Scope: Sionna Provider

Implement Day 2 from the execution plan.

Primary files/directories:

- `network/radio_provider/`
- `network/position_tracker/`
- `network/config/radio_24ghz.yaml`
- `network/config/jammers.yaml`
- heatmap generation command
- Sionna query JSONL logging

Use the JSON-lines TCP IPC contract from the plan unless you document a better
upstream adapter in `network/DECISIONS.md`.

Done when:

- A runtime provider command exists.
- Query input/output follows the documented schema.
- A real Sionna-backed path is attempted when dependencies exist.
- Any mock or fallback mode is clearly marked as test-only and cannot satisfy
  customer acceptance.
