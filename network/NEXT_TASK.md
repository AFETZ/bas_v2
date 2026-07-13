# Next Task

Authoritative contract: `doc/network_radio_integration_plan_v2.md`.

Customer-ready: **false**. Fully closed milestones: **0**. Active milestone:
**M0**.

## Next Exact Action

Finish M0 reproducibility and evidence identity without weakening its gates:

1. Freeze and review the intended source set; keep mutable progress/report files
   outside the runtime implementation hash.
2. Obtain explicit scope for committing the currently mixed dirty/untracked
   worktree. Do not silently include unrelated user changes.
3. Ensure enough Docker storage using the explicitly authorized dangling-image
   and old build-cache cleanup.
4. Rebuild `multiagent_simulation:latest` from the pinned Dockerfile, hash-locked
   Python closure, and exact repo manifests for `linux/amd64` and UID/GID 1000.
5. Inspect the immutable image, record its new `sha256:` digest and exact
   normalized pip/dpkg/ROS manifest hashes, run compatibility/capability checks,
   and set the dependency lock to `complete` only if every comparison passes.
6. From a clean checkout, launch the exact image ID with
   `scripts/run_acceptance_container.sh`, preserve its full container identity,
   seal and externally attest the evidence, and require independent
   `acceptance_eligible=true` validation.

Then rerun sequential evidence:

```bash
# M1: only after M0 has a clean revision, complete lock, and accepted attestation
RUN_ID=m1_candidate_<UTC> \
DURATION_S=300 MINIMUM_DURATION_S=300 WARMUP_S=30 \
./network/scripts/run_five_uav_health.sh

# M2 only after M0 and M1 pass; build/verify a fresh ns-3 receipt
RUN_ID=m2_candidate_<UTC> \
./network/scripts/run_one_uav_vertical_slice.sh
python3 network/validation/validate_m2_vertical_slice.py \
  --run-dir runs/m2_candidate_<UTC>
```

After accepted M2, the next implementation target is M3: five isolated UAV
endpoints, complete endpoint matrix, three real traffic classes in both
directions, exact capture correlation, and all-UAV no-bypass.

Do not divert the critical path to video/dashboard polish, replay-only radio
proof, physical modem hardware, or customer-map presentation.

The operator approved the current commit scope, external signing key/ledger,
Docker cleanup, and pinned image build on 2026-07-13. The private key remains
outside the repository and must never enter a run container.
