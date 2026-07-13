# Network/Radio Validation Report

Updated: 2026-07-13 UTC.

Authoritative contract: `doc/network_radio_integration_plan_v3.md`.

Customer-ready: **false**. Accepted integrated P0 run: **none**. Fully closed
sequential milestones: **0**.

## Strict Milestone Assessment

| Milestone | Formal status | Evidence and blocker |
| --- | --- | --- |
| M0 | `in_progress` | v2 probe `m0_baseline_20260713T090234Z` passed, but v3 changes the normative contract/source manifest and explicitly requires a clean requalification before it counts. |
| M1 | `not_started` | A 300.007 s v2 diagnostic proved feasibility; no M1 evidence is grandfathered into v3, and sequential v3 M0 remains open. |
| M2 | `not_started` | Real MAVLink/TapBridge good/down/recovery functional gates passed diagnostically; the run is ineligible under current provenance, build-receipt, attestation, and sequential prerequisites. |
| M3 | `not_started` | No five-UAV external packet matrix with three bidirectional traffic classes. |
| M4 | `not_started` | Strict validator profile exists, but no accepted online Sionna causal packet run. |
| M5 | `not_started` | Strict contention/priority profiles exist, but no accepted overload runtime. |
| M6 | `not_started` | Existing HitL artifacts do not prove PTY and Ethernet through the accepted M2–M5 path. |
| M7 | `not_started` | No single integrated scenario matrix; no eligible kilometre-scale matched scene. |
| M8 | `not_started` | No accepted soak, clean-clone pair, or independently extracted customer bundle. |

No milestone yet has a caveat-free `passed` state under v3, so the closed
sequential count is zero. The accepted v2 image/runtime work remains reusable
input, not grandfathered v3 closure evidence.

## Superseded v2 M0 Evidence

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

The M0 probe intentionally records `p0_eligible=false`. M0 qualifies the exact
runtime baseline and fail-closed validation behavior; it cannot claim packet
path, sealing, attestation, or integrated P0 proof before the corresponding raw
M1-M8 evidence exists. Full raw-evidence sealing and external Ed25519
attestation remain mandatory for the later integrated P0 run. The v3 migration
rule now requires this qualification to be rerun against the clean v3 source
and contract identity.

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

1. Commit the v3 contract/validator transition, then produce and independently
   validate a clean exact-image v3 M0 qualification.
2. Produce a fresh 300-second v3 M1 five-UAV health run with active-world
   provenance only after v3 M0 passes.
3. Only after M1 passes, rerun the real one-UAV M2 vertical slice with a fresh
   ns-3 build receipt and current provenance contracts.
4. Implement and execute M3-M7 in order; tests, old subsystem records, and
   replay/video evidence cannot substitute for current real runtime gates.
5. Close M8 with soak/stability evidence, content-addressed image distribution,
   snapshot-pinned mutable dependency inputs, a no-cache manifest-equivalent
   reconstruction, two clean-clone passes, independently extracted bundle
   validation, and customer handoff instructions.
