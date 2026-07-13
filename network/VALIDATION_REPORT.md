# Network/Radio Validation Report

Updated: 2026-07-12 UTC.

Authoritative contract: `doc/network_radio_integration_plan_v2.md`.

Customer-ready: **false**. Accepted integrated P0 run: **none**. Fully closed
sequential milestones: **0**.

## Strict Milestone Assessment

| Milestone | Formal status | Evidence and blocker |
| --- | --- | --- |
| M0 | `in_progress` | Fail-closed validation, provenance, sealing, external attestation, ns-3 receipts, and 116 tests exist; the signing identity/ledger are now provisioned, but the checkout is not yet committed/clean, the dependency lock requires rebuild, and no replacement image digest/runtime manifests are accepted. |
| M1 | `not_started` | A 300.007 s component diagnostic kept all five UAV stacks healthy, but M0 is open, provenance failed, and later validator hardening makes the older record ineligible as current evidence. |
| M2 | `not_started` | Real MAVLink/TapBridge good/down/recovery functional gates passed diagnostically; the run is ineligible under current provenance, build-receipt, attestation, and sequential prerequisites. |
| M3 | `not_started` | No five-UAV external packet matrix with three bidirectional traffic classes. |
| M4 | `not_started` | Strict validator profile exists, but no accepted online Sionna causal packet run. |
| M5 | `not_started` | Strict contention/priority profiles exist, but no accepted overload runtime. |
| M6 | `not_started` | Existing HitL artifacts do not prove PTY and Ethernet through the accepted M2–M5 path. |
| M7 | `not_started` | No single integrated scenario matrix; no eligible kilometre-scale matched scene. |
| M8 | `not_started` | No accepted soak, clean-clone pair, or independently extracted customer bundle. |

No row has a caveat-free `passed` state, so the count is zero.

## Verified Negative Regression

Command:

```bash
./network/scripts/run_validation.sh \
  --run-dir runs/real_packet_loop_20260702T113341Z \
  --no-write
```

Result: exit `1`, `P0 passed: false`.

The historical run is rejected for zero RX, loss `1.0`, null mandatory
latencies, ARP-only/copied class PCAP, capture-permission errors, synthesized
summary claims, and absent structured runtime/causal/no-bypass evidence.

## Current Component Evidence

### M1 diagnostic

`runs/m1_candidate_retry_20260712T145712Z` recorded:

- `300.007265 s` observation;
- process minimums `5/5/5/2` for ArduCopter/MAVProxy/micro-ROS/Gazebo;
- all names `uav1..uav5`, system IDs `1..5`, unique DDS ports `2019..2023`;
- per UAV about `37.16 Hz` odometry, `222` heartbeat and `892` valid GPS
  samples, with no invalid/stale/nonadvancing samples;
- real-time factor about `0.7432` and clean process/port cleanup.

This proves feasibility of the base runtime, not formal M1 closure. Its
provenance is dirty/ineligible, and the validator was subsequently strengthened
to require launch-log identity/offset fields that this older diagnostic did not
record. The preceding run `m1_candidate_20260712T145123Z` correctly exposed a
port-5760 bind failure and led to fail-fast/preflight fixes.

### M2 diagnostic

`runs/m2_current_20260712T144509Z` established the following component-level
facts under the validator version captured by that run:

```text
metadata            passed
probe_transactions  passed
packet_captures     passed
adapter_path        passed
process_identity    passed
critical_logs       passed
manifest            passed
provenance          failed: dirty source checkout
formal M2           false
```

Transactions were good `10/10`, stopped `0/5` with zero telemetry/heartbeat,
and recovery `10/10`. The captured global provenance also records dependency
lock status `rebuild_required_before_acceptance`. The run predates the current
full-container-ID, fixed raw-path, ns-3 build-receipt, and external-attestation
contracts; those additions intentionally cannot make old evidence eligible.

Revalidation with the current standalone M2 validator exits `1` and adds:

```text
ns3_build_receipt   failed: required receipt is absent
provenance          failed: dirty/stale source identity
manifest            failed: receipt is absent from sealed raw files
formal M2           false
```

## Validator Coverage

The current 116-test suite covers positive controls and adversarial mutations
for:

- false summary flags, zero/impossible delivery, NaN/boolean metrics;
- ARP-only, copied, truncated, padded-nonce, and missing-payload PCAP;
- exact-schema, fixed-path provenance and artifact manifests, complete raw
  event envelopes, and rejection of symlinks, hardlinks, duplicate inodes,
  writable sealed artifacts, and substituted matrices;
- dirty/unknown/mismatched dependency and image provenance, full container IDs,
  normalized package manifests, and runtime capabilities;
- Ed25519 evidence signatures, pinned-key identity, raw mutation, re-signing,
  rogue-key, running/wrong-container, and external-ledger attacks;
- ns-3 build receipts binding source, scratch copy, CMake/toolchain/module
  state, wrapper lock, and executable, including stale/tampered variants;
- hash-locked Python closure and real NumPy/OpenCV/cv_bridge/Sionna RT/Mitsuba/
  Matplotlib compatibility checks;
- M1 mixed identity, event sequence, launch fatal markers, duration, freshness,
  process health, and readiness;
- M2 exact command/ACK/telemetry hashes, down isolation, process identity,
  manifest mutation, and critical logs;
- general no-bypass rejection of producer `ack=true` booleans;
- MAVLink ACK correlation without falsely claiming that ACK echoes a nonce;
- all eight strict raw-derived causal/repeatability profiles;
- allowed milestone states, sequential status, closed-count, active-milestone,
  and customer-ready agreement across the mutable status documents.

Verification commands currently pass:

```bash
python3 -m unittest discover -v network/tests 'test_*.py'
bash network/tests/check_ns3_packet_core_config.sh
```

## Current Acceptance Blockers

1. Define and commit the intended implementation scope without absorbing
   unrelated user changes; then verify a clean reconstruction.
2. Reclaim enough Docker storage, rebuild the
   pinned image, record its exact digest/runtime manifests, and change the lock
   to `complete` only after every comparison passes.
3. Produce host-attested M0 evidence from the clean revision and immutable
   image, then rerun M1 and M2 sequentially from that accepted baseline.
4. Implement and execute M3–M8; tests or old subsystem/video artifacts cannot
   substitute for those runtime gates.
