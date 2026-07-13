# Next Task

Authoritative contract: `doc/network_radio_integration_plan_v3.md`.

Customer-ready: **false**. Fully closed milestones: **0**. Active milestone:
**M0**.

## Next Exact Action

Commit the v3 contract, provenance pointer, fail-closed M1 workflow, and
consistent zero-closure status. Then requalify the exact accepted image from a
clean checkout:

```bash
RUN_ID=m0_v3_baseline_<UTC>
CONTAINER_NAME=ams-m0-v3-<UTC> \
./scripts/run_acceptance_container.sh \
  env RUN_ID="$RUN_ID" network/scripts/run_m0_baseline.sh

python3 network/scripts/validate_m0_baseline.py \
  --run-dir "runs/$RUN_ID"
```

Retain the stopped container and independently rerun
`validate_m0_baseline.py`. Close v3 M0 only when dependency and provenance both
pass, the source checkout is clean, the exact image/runtime manifests still
match, and the result retains `p0_eligible=false`.

Only after that caveat-free result, execute formal M1 for at least 300 observed
seconds and validate active Gazebo-world provenance:

```bash
RUN_ID=m1_candidate_<UTC>
CONTAINER_NAME=ams-m1-<UTC> \
./scripts/run_acceptance_container.sh \
  env RUN_ID="$RUN_ID" DURATION_S=300 MINIMUM_DURATION_S=300 WARMUP_S=30 \
  network/scripts/run_five_uav_health.sh

python3 network/scripts/validate_m1_health.py \
  --run-dir "runs/$RUN_ID" --no-write
```

After accepted M2, the next implementation target is M3: five isolated UAV
endpoints, complete endpoint matrix, three real traffic classes in both
directions, exact capture correlation, and all-UAV no-bypass.

Do not divert the critical path to video/dashboard polish, replay-only radio
proof, physical modem hardware, or customer-map presentation.

The v2 probe `m0_baseline_20260713T090234Z` on image
`sha256:2aad1f...79afb` remains the reusable runtime baseline, but it is not v3
closure. Full evidence sealing and external attestation apply to the later
integrated P0 profiles. The private key remains outside the repository and must
never enter a run container.
