# Next Task

Authoritative contract: `doc/network_radio_integration_plan_v3.md`.

Customer-ready: **false**. Fully closed milestones: **1**. Active milestone:
**M1**.

## Next Exact Action

Execute formal M1 for at least 300 observed seconds on the exact image accepted
by v3 M0 and independently validate all three M1 gates:

```bash
RUN_ID=m1_v3_candidate_<UTC>
CONTAINER_NAME=ams-m1-v3-<UTC> \
./scripts/run_acceptance_container.sh \
  env RUN_ID="$RUN_ID" DURATION_S=300 MINIMUM_DURATION_S=300 WARMUP_S=30 \
  network/scripts/run_five_uav_health.sh

python3 network/scripts/validate_m1_health.py \
  --run-dir "runs/$RUN_ID" --no-write
```

Retain and inspect the stopped container. Close M1 only if provenance,
five-UAV health, and active Gazebo scene gates all pass; the observed window is
at least 300 seconds; all five identities and models remain fresh; the exact
world bundle is proven; cleanup is clean; and independent `--no-write`
revalidation also passes.

After M1, first harden M2 so its independent validator derives non-null
good/down/recovery loss and latency from monotonic raw command/ACK/telemetry,
proves the exact current executable/config/image/source identity, checks the
live namespace/routes/interfaces and all forbidden endpoints, and revalidates a
fresh ns-3 build receipt. Then run formal M2. Only after accepted M2 is the next
implementation target M3: five isolated UAV endpoints, the complete endpoint
matrix, three real traffic classes in both directions, exact capture
correlation, and all-UAV no-bypass.

Do not divert the critical path to video/dashboard polish, replay-only radio
proof, physical modem hardware, or customer-map presentation.

The accepted v3 M0 run is `m0_v3_baseline_20260713T130710Z`, source commit
`95746e37014cce5a974d2dbb7d7e4c8e18b48929`, exact image
`sha256:2aad1f...79afb`. Full evidence sealing and external attestation apply to
the later integrated P0 profiles. The private key remains outside the
repository and must never enter a run container.
