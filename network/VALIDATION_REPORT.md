# Network/Radio Validation Report

Updated: 2026-07-14 UTC.

Authoritative contract: `doc/network_radio_integration_plan_v3.md`.

Customer-ready: **false**. Accepted integrated P0 run: **none**. Fully closed
sequential milestones: **1**.

## Strict Milestone Assessment

| Milestone | Formal status | Evidence and blocker |
| --- | --- | --- |
| M0 | `passed` | Clean v3 run `m0_v3_baseline_20260713T130710Z` passed both formal gates and independent host revalidation on the exact accepted image. |
| M1 | `in_progress` | A 300.007 s historical diagnostic proved feasibility; formal v3 execution is now unblocked and is the active work. |
| M2 | `not_started` | Real MAVLink/TapBridge good/down/recovery functional gates passed diagnostically; M1 is open and the current formal path still needs the complete v3 raw-derived provenance/metric/topology contract. |
| M3 | `not_started` | No five-UAV external packet matrix with three bidirectional traffic classes. |
| M4 | `not_started` | Strict validator profile exists, but no accepted online Sionna causal packet run. |
| M5 | `not_started` | Strict contention/priority profiles exist, but no accepted overload runtime. |
| M6 | `not_started` | Existing HitL artifacts do not prove PTY and Ethernet through the accepted M2–M5 path. |
| M7 | `not_started` | No single integrated scenario matrix; no eligible kilometre-scale matched scene. |
| M8 | `not_started` | No accepted soak, clean-clone pair, or independently extracted customer bundle. |

M0 has a caveat-free `passed` state under v3, so the closed sequential count is
one. No M1 evidence is grandfathered from v2.

## Accepted v3 M0 Evidence

- Formal qualification run: `runs/m0_v3_baseline_20260713T130710Z`.
- Source commit: `95746e37014cce5a974d2dbb7d7e4c8e18b48929`, clean at
  execution; source hash
  `d61482883c39243d5c6a5e995b3690fdf13f5b9f1c2e30e8fc778b83635c5c56`.
- Exact immutable image ID:
  `sha256:2aad1f25789fc1e5c23c3a4b05c91927198ad42ff6e97cde2c26cb2f18979afb`.
- Retained container:
  `afb765f5507f69d3fceaf4b4c178898e57e0e1d71b335bf7f196baa4e21ab5a7`;
  stopped with exit `0`, restart count `0`, no OOM, and the exact image.
- Container execution: `2026-07-14T06:45:18.909582676Z` to
  `2026-07-14T06:45:41.490310806Z`; evidence generated
  `2026-07-14T06:45:40Z`.
- v3 plan hash:
  `18fd8309e35341e8f1fec0ae28a7dcf38cd9ae042785b20fb9b9df40e8aa7156`;
  matrix hash:
  `f5923bdc38470519cffb35071aae95fd0c5597a24df853e324e321d0d4a48d14`.
- Dependency and provenance gates both pass; `acceptance_eligible=true`,
  `acceptance_blockers=[]`; independent host `validate_m0_baseline.py` passes.
- Complete manifest hashes match pip
  `36941db39413d66f80191197d8df8d771221dce3a440cbe98f9078cca012b70e`,
  dpkg
  `bdd042c1249d3aa238997d3c5222af6eed56e56701ab64f623fb74439ecc39aa`,
  and ROS
  `274b14bf4ad003e7fded28bf3e068715c13068252bd84ecb7b61abc0cd44916f`.

The run ID was allocated before execution and is retained unchanged; recorded
container/provenance timestamps are authoritative. The result intentionally
has `p0_eligible=false`, `packet_path=false`, `sealing=false`, and
`attestation=false`. These are the correct M0 scope, not missing M0 gates.

## Historical v2 M0 Evidence

- Accepted source commit:
  `aae15dbbf114ac8a0fe285d6742b702188568634`, clean at execution.
- Accepted immutable image ID:
  `sha256:2aad1f25789fc1e5c23c3a4b05c91927198ad42ff6e97cde2c26cb2f18979afb`.
- Formal qualification run: `runs/m0_baseline_20260713T090234Z`.
- Retained stopped container:
  `5334f05783e44a3bd6fe83bc7e32d204934a433e56dc1ebd57ec2ebface18a6a`,
  exit `0`, restart count `0`, exact accepted image.
- Independent dependency and provenance gates pass with
  `acceptance_eligible=true` and `acceptance_blockers=[]`.
- Normalized manifest hashes match the complete lock: pip
  `36941db39413d66f80191197d8df8d771221dce3a440cbe98f9078cca012b70e`,
  dpkg
  `bdd042c1249d3aa238997d3c5222af6eed56e56701ab64f623fb74439ecc39aa`,
  and ROS
  `274b14bf4ad003e7fded28bf3e068715c13068252bd84ecb7b61abc0cd44916f`.
- The exact image passes `pip check`, all 21 runtime ABI/import checks, all nine
  pinned external-source revision/cleanliness checks, and the complete 130-test
  regression suite.

The v2 result remains historical corroboration only. The accepted v3 evidence
above independently requalifies the same content-addressed runtime against the
current contract and source identity.

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

The current 130-test suite covers positive controls and adversarial mutations
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
  process health, readiness, v3 contract binding, installed-asset mutation,
  path traversal, and forged active-world evidence;
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

1. Produce and independently validate a fresh 300-second v3 M1 five-UAV health
   run with active-world provenance on the accepted exact image.
2. Only after M1 passes, rerun the real one-UAV M2 vertical slice with a fresh
   ns-3 build receipt and current provenance contracts.
3. Implement and execute M3-M7 in order; tests, old subsystem records, and
   replay/video evidence cannot substitute for current real runtime gates.
4. Close M8 with soak/stability evidence, content-addressed image distribution,
   snapshot-pinned mutable dependency inputs, a no-cache manifest-equivalent
   reconstruction, two clean-clone passes, independently extracted bundle
   validation, and customer handoff instructions.
