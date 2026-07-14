# Next Task

Authoritative contract: `doc/network_radio_integration_plan_v3.md`.

Customer-ready: **false**. Fully closed milestones: **2**. Active milestone:
**M2**.

## Next Exact Action

Complete the M2 v3 evidence contract before running another candidate:

- derive good/down/recovery TX/RX/loss denominators, adapter latency, ns-3
  latency, end-to-end latency, and jitter from strictly monotonic raw events;
- correlate exact real frame hashes in both directions at GCS ingress, ns-3
  ingress, ns-3 egress, and UAV egress;
- validate live namespace links, addresses, routes, interfaces, the single
  external path, and every forbidden endpoint/route from raw snapshots;
- require exact current source/config/image/container/executable provenance and
  a freshly revalidated content-addressed ns-3 build receipt; and
- add adversarial tests for missing/null/forged metrics, stale topology,
  alternate routes, wrong container/source, copied capture, and phase identity.

Run the focused adversarial suite and full regression suite, commit a clean M2
implementation, and only then execute the exact-image formal candidate:

```bash
RUN_ID=m2_v3_candidate_<UTC>
CONTAINER_NAME=ams-m2-v3-<UTC> \
./scripts/run_acceptance_container.sh \
  env RUN_ID="$RUN_ID" network/scripts/run_one_uav_vertical_slice.sh

python3 network/validation/validate_m2_vertical_slice.py \
  --run-dir "runs/$RUN_ID"
```

Retain and inspect the stopped container. Close M2 only if every v3 acceptance
claim is independently raw-derived, all gates pass without qualifications,
the good/down/recovery counts are exactly `10/10`, `0/5`, and `10/10`, and the
stopped phase proves that the ns-3 child was the only route.

Only after accepted M2 is the next implementation target M3: five isolated UAV
endpoints, the complete endpoint matrix, three real traffic classes in both
directions, exact capture correlation, and all-UAV no-bypass.

Do not divert the critical path to video/dashboard polish, replay-only radio
proof, physical modem hardware, or customer-map presentation.

The accepted v3 M1 run is `m1_v3_candidate_20260714T072723Z`, source commit
`ad9c16f2fb584125bdee0ebb682612c4d89a4d50`, exact image
`sha256:2aad1f...79afb`. Full evidence sealing and external attestation apply to
the later integrated P0 profiles. The private key remains outside the
repository and must never enter a run container.
