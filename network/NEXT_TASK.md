# Next Task

Authoritative contract: `doc/network_radio_integration_plan_v2.md`.

Customer-ready: **false**. Fully closed milestones: **1**. Active milestone:
**M1**.

## Next Exact Action

Run the formal M1 health gate for at least 300 observed seconds from the clean
accepted M0 revision and immutable image:

```bash
RUN_ID=m1_candidate_<UTC>
CONTAINER_NAME=ams-m1-<UTC> \
./scripts/run_acceptance_container.sh \
  env RUN_ID="$RUN_ID" DURATION_S=300 MINIMUM_DURATION_S=300 WARMUP_S=30 \
  network/scripts/run_five_uav_health.sh
```

Retain the stopped container and independently require both
`provenance_status(run_dir)` and `five_uav_health_status(run_dir)` to return
`status=passed`. Close M1 only if all five models and SITL processes remain
healthy for the full observation, identities/ports are exact and unique, every
UAV has fresh advancing heartbeat/odometry evidence, and the launch log has no
bind, crash, link-down, missing-pose, or stale-pose failure.

Only after that caveat-free result, execute M2:

```bash
RUN_ID=m2_candidate_<UTC>
CONTAINER_NAME=ams-m2-<UTC> \
./scripts/run_acceptance_container.sh \
  env RUN_ID="$RUN_ID" network/scripts/run_one_uav_vertical_slice.sh

python3 network/validation/validate_m2_vertical_slice.py \
  --run-dir "runs/$RUN_ID"
```

After accepted M2, the next implementation target is M3: five isolated UAV
endpoints, complete endpoint matrix, three real traffic classes in both
directions, exact capture correlation, and all-UAV no-bypass.

Do not divert the critical path to video/dashboard polish, replay-only radio
proof, physical modem hardware, or customer-map presentation.

M0 is closed by `runs/m0_baseline_20260713T090234Z` on source `aae15db` and
image `sha256:2aad1f...79afb`. Its deliberate `p0_eligible=false` is not a
caveat: M0 is a baseline qualification, while full evidence sealing and
external attestation apply when the integrated P0 raw set exists. The private
key remains outside the repository and must never enter a run container.
