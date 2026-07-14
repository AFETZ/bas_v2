# Network/Radio Progress

Updated: 2026-07-14 UTC.

Authoritative contract: `doc/network_radio_integration_plan_v3.md`.

## Acceptance Status

- Customer-ready: **false**.
- Fully closed sequential milestones: **2**.
- Active milestone: **M2 — One-UAV External Packet Vertical Slice**.
- Historical plan/status claims do not count toward v3 closure.

| Milestone | Formal status | Current position |
| --- | --- | --- |
| M0 | `passed` | Clean exact-image v3 qualification `m0_v3_baseline_20260713T130710Z` passed dependency and provenance gates and passed independent host revalidation. |
| M1 | `passed` | Formal run `m1_v3_candidate_20260714T072723Z` passed provenance, 300 s five-UAV health, and active-scene gates plus independent host revalidation. |
| M2 | `in_progress` | The real TapBridge/MAVLink path exists; its raw contract and independent validator are being completed to cover every v3 metric, topology, current-identity, and no-bypass requirement before formal execution. |
| M3–M8 | `not_started` | No milestone has complete current-run acceptance evidence. |

Only `passed` closes a milestone. Diagnostic or subsystem success is recorded
below but is never added to the closed-milestone count.

## Implemented Foundation

- Added the corrected v2 contract beside the original historical plan, then
  superseded it with v3 after the full M1-M8 executability audit exposed and
  removed the remaining acceptance-profile cycles.
- Replaced self-certifying validation with raw-derived, fail-closed gates in
  `network/validation/`.
- Removed PCAP substitution, synthesized no-bypass PASS files, and producer
  acceptance flags from post-processing authority.
- Added write-once provenance, exact source/config manifests, dependency and
  container records, ns-3 release-tree verification, and raw-evidence sealing.
- Fixed authoritative raw-evidence paths to the validation matrix, required a
  complete start-to-completion event envelope, and now reject symlink components,
  hardlinks, duplicate inodes, writable sealed artifacts, and malformed or
  substituted manifests.
- Added detached Ed25519 evidence attestation with an independently pinned
  public key, full raw-artifact rehashing, stopped-container inspection, and an
  external one-time ledger. The external private key and ledger are provisioned;
  only the pinned public key/fingerprint is tracked in the repository.
- Added a formal acceptance-container launcher that runs an inspected immutable
  image ID, retains the stopped container for host attestation, and injects the
  full 64-character container ID through a host-owned mount.
- Added content-addressed ns-3 build receipts binding the official 3.40 tree,
  copied scratch sources, exact enabled modules, CMake/toolchain state, wrapper
  lock, and executable. M2 now requires and re-verifies that receipt.
- Replaced the incompatible Python closure with a 61-package, hash-locked,
  binary-only Python 3.10/x86_64 closure using NumPy 1.26.4, Sionna RT 1.2.2,
  and Mitsuba 3.8.0. Full TensorFlow/Sionna PHY and pybind11/cppyy remain
  diagnostic-only, outside the accepted runtime.
- Added real runtime compatibility checks for NumPy/OpenCV/cv_bridge/Sionna
  RT/Mitsuba/Matplotlib, exact normalized package manifests, runtime capability
  records, and fail-closed comparison against the dependency policy.
- Added strict causal profiles for Sionna causality, link locality, shared
  medium, priority, jamming, time coherence, scene alignment, and
  repeatability. Summary PASS booleans are ignored.
- Hardened packet parsing against ARP-only proof, Ethernet-padding nonce hits,
  malformed lengths, copied captures, impossible counters, and non-finite
  metrics.
- Hardened general no-bypass evidence: decoded command/ACK correlation, exact
  hashes/nonces, stable endpoint and ns-3 process identities, and ordered
  on/stopped/recovered raw phases are required. A producer `ack=true` is not
  proof.
- Corrected the MAVLink contract: the nonce is carried by a valid STATUSTEXT
  marker; COMMAND_ACK is correlated by request frame hash/sequence/system and
  is not claimed to echo a nonce field that it does not have.
- Normalized the P0 engineering surrogate to 2.4 GHz, 20 MHz, and 20 Mbit/s,
  including the primary jammer and service-tier configuration.
- Added M1 five-UAV health tooling with unique DDS ports, GPS/heartbeat/
  odometry readiness, async process monitoring, bindable-port preflight,
  fail-fast startup/readiness behavior, and write-once raw identity envelopes.
- Added v3 M1 active-world proof that binds the scenario, v3 contract, source
  and installed asset manifests, Gazebo version/world/entity probe, and the raw
  running `gz sim` world argument; a standalone three-gate validator cannot
  convert the component run into a packet-path or P0 claim.
- Added M2 `ams-gcs -> ams-ns3/TapBridge/ns-3 -> ams-uav1 -> SITL`
  lifecycle, MAVLink probe, four-point captures, good/down/recovery runner,
  sealed evidence, and independent adversarial validator.

Current verification:

```text
python3 -m unittest discover -v network/tests 'test_*.py'  -> 130/130 passed
bash network/tests/check_ns3_packet_core_config.sh          -> passed
Python compile, shell syntax, git diff --check              -> passed
historical false-positive validation                        -> exit 1
```

## Closed v3 M1 Qualification

- Formal run: `runs/m1_v3_candidate_20260714T072723Z`.
- Accepted source commit:
  `ad9c16f2fb584125bdee0ebb682612c4d89a4d50`, clean at execution;
  implementation source hash
  `d61482883c39243d5c6a5e995b3690fdf13f5b9f1c2e30e8fc778b83635c5c56`.
- Exact image:
  `sha256:2aad1f25789fc1e5c23c3a4b05c91927198ad42ff6e97cde2c26cb2f18979afb`;
  retained stopped container
  `1f353129b340dc924a7d3a4316156be7af3903e45afa8d75ef8c15c2c6e9bbc1`,
  exit `0`, restart count `0`, no OOM or dead state.
- Container execution window: `2026-07-14T07:27:29.37008259Z` through
  `2026-07-14T07:33:26.329303893Z`; provenance generated
  `2026-07-14T07:27:48Z`.
- Observed health window: `300.017294 s`; raw process samples: `301`;
  minimum simultaneous ArduCopter/MAVProxy/micro-ROS/Gazebo counts:
  `5/5/5/2`.
- Each UAV recorded `11488` odometry samples at
  `38.290896..38.291261 Hz`, `230` heartbeat samples, `919` valid MAVLink
  positions, a fresh final odometry age no greater than `0.024674 s`, no
  invalid/nonadvancing odometry sample, and no per-UAV failure.
- Names are exactly `uav1..uav5`, system IDs exactly `1..5`, and DDS ports
  exactly `2019..2023`.
- The live Gazebo world is `map`; the running world path is the installed
  `modelflughafen/model.sdf`; all six source/install bundle hashes match with
  bundle hash
  `6ab71d524ba62fc32b78613aafe3161e8f46253c297a1b0ae364437c1e491eec`.
- Built-in and independent host validators both report all three gates
  `passed`, `failures=[]`, `acceptance_eligible=true`, and
  `acceptance_blockers=[]`.
- Post-run inspection found no listener or surviving ArduPilot, MAVProxy,
  micro-ROS, or Gazebo process. The five transient TCP connections were only
  kernel `TIME_WAIT` entries, not listeners.

The observed odometry real-time factor was `0.765818..0.765825`. M1 requires
it to be recorded, not to satisfy the later M6/M7 `0.95..1.05` timing gate, so
this is not an M1 qualification caveat or waiver. The result correctly remains
component-only and `p0_eligible=false`.

## Runtime Diagnostics

### M1 five-UAV health

Failed diagnostic:
`runs/m1_candidate_20260712T145123Z`.

- uav1 ArduCopter exited because TCP port 5760 was not bindable.
- The run was stopped once the failure was proven; cleanup left no matching
  processes, namespaces, or listeners.
- The runner was then hardened to use an actual socket `bind()` preflight,
  scan fatal startup markers, and stop immediately when readiness fails.

Successful component diagnostic under its captured source:
`runs/m1_candidate_retry_20260712T145712Z`.

- observed window: `300.007265 s`;
- process minimums: ArduCopter `5`, MAVProxy `5`, micro-ROS agent `5`, Gazebo
  launcher/server processes `2`;
- each UAV: approximately `11147..11149` odometry samples at `37.16 Hz`, `222`
  heartbeat samples, `892` GPS samples, and no per-UAV failure;
- observed odometry real-time factor: approximately `0.7432`;
- cleanup: no matching process, namespace, or required-port listener remained.

The component gate passed with the validator captured by that run. Subsequent
hardening now also requires the fixed run-relative launch-log path and recorded
post-warm-up byte offset; the old diagnostic intentionally fails those new
fields and is not current acceptance evidence. The RTF is also below the later
integrated M6/M7 contract of `0.95..1.05`.

### M2 one-UAV external packet slice

Current diagnostic: `runs/m2_current_20260712T144509Z`.

- good: command ACK `10/10`, telemetry `10/10`, heartbeat present, loss `0`;
- ns-3 stopped: ACK `0/5`, telemetry `0/5`, heartbeat `0`, timeout present,
  loss `1`;
- recovery: command ACK `10/10`, telemetry `10/10`, heartbeat present, loss
  `0`;
- metadata, raw transactions, all capture points, adapter path, process
  identity, critical-log scan, and the then-current sealed manifest passed
  independently;
- current formal result remains **M2 false**. Its checkout was dirty and the
  run predates the stronger full-container-ID, fixed raw-path, ns-3 receipt,
  and external-attestation requirements, so it cannot be promoted retroactively.

The current standalone M2 validator now reports `ns3_build_receipt: failed`,
`provenance: failed`, and `manifest: failed` because this old run contains no
required `metrics/ns3_tap_build_receipt.json` and its source provenance is
stale. This is the intended fail-closed result, not a regression in the
diagnostic packet behavior above.

The good-phase ACK p95 was `700.189283 ms`; M2 requires populated bounded
latency but has no 250 ms acceptance threshold. The 250 ms limit applies later
to the M5 priority scenario and remains unproven.

### Historical false positive

`runs/real_packet_loop_20260702T113341Z` is a permanent negative regression
fixture. It has zero RX, complete loss, null mandatory latency, ARP-only/copied
class PCAP, and no active no-bypass proof. Current validation exits `1` with
`P0 passed: false`.

## Closed v3 M0 Qualification

- Formal run: `runs/m0_v3_baseline_20260713T130710Z`.
- Accepted source commit:
  `95746e37014cce5a974d2dbb7d7e4c8e18b48929`, clean at execution.
- Source manifest hash:
  `d61482883c39243d5c6a5e995b3690fdf13f5b9f1c2e30e8fc778b83635c5c56`.
- Accepted immutable image ID:
  `sha256:2aad1f25789fc1e5c23c3a4b05c91927198ad42ff6e97cde2c26cb2f18979afb`.
- Retained stopped container:
  `afb765f5507f69d3fceaf4b4c178898e57e0e1d71b335bf7f196baa4e21ab5a7`;
  exit `0`, restart count `0`, not OOM-killed, exact accepted image.
- Container execution window: `2026-07-14T06:45:18.909582676Z` through
  `2026-07-14T06:45:41.490310806Z`; provenance generated
  `2026-07-14T06:45:40Z`.
- v3 plan hash:
  `18fd8309e35341e8f1fec0ae28a7dcf38cd9ae042785b20fb9b9df40e8aa7156`;
  validation-matrix hash:
  `f5923bdc38470519cffb35071aae95fd0c5597a24df853e324e321d0d4a48d14`.
- Dependency and provenance gates pass, `acceptance_eligible=true`,
  `acceptance_blockers=[]`, and the independent host validator passes.
- The result correctly retains `p0_eligible=false` and scopes packet path,
  sealing, and attestation to false: M0 qualifies the exact runtime and does
  not claim later integrated evidence.

The run ID is an immutable identifier allocated before execution. The
authoritative execution and evidence times are the recorded container and
provenance timestamps above.

Locked normalized manifests remain pip `342` entries/
`36941db39413d66f80191197d8df8d771221dce3a440cbe98f9078cca012b70e`,
dpkg `1956` entries/
`bdd042c1249d3aa238997d3c5222af6eed56e56701ab64f623fb74439ecc39aa`,
and ROS `309` entries/
`274b14bf4ad003e7fded28bf3e068715c13068252bd84ecb7b61abc0cd44916f`.

This closes M0 under v3 without qualifications. M1 has since closed separately
and M2 is now the only active sequential milestone.

## Historical v2 M0 Baseline

- Accepted source commit: `aae15dbbf114ac8a0fe285d6742b702188568634`;
  the checkout was clean during the run and remains clean after ignored run
  artifacts.
- Accepted image ID:
  `sha256:2aad1f25789fc1e5c23c3a4b05c91927198ad42ff6e97cde2c26cb2f18979afb`
  (`linux/amd64`, user `ubuntu`, 34 layers, 6,373,951,440 bytes).
- Locked normalized manifests: pip `342` entries/
  `36941db39413d66f80191197d8df8d771221dce3a440cbe98f9078cca012b70e`,
  dpkg `1956` entries/
  `bdd042c1249d3aa238997d3c5222af6eed56e56701ab64f623fb74439ecc39aa`,
  ROS `309` entries/
  `274b14bf4ad003e7fded28bf3e068715c13068252bd84ecb7b61abc0cd44916f`.
- Formal probe: `runs/m0_baseline_20260713T090234Z`; dependency and provenance
  gates pass, `acceptance_blockers=[]`, and independent host revalidation also
  passes. The retained stopped container is
  `5334f05783e44a3bd6fe83bc7e32d204934a433e56dc1ebd57ec2ebface18a6a`
  with exit `0` and the exact accepted image.
- The probe deliberately records `p0_eligible=false`: M0 qualifies the runtime
  baseline and validator, not the later integrated packet/radio P0 result.
  Actual evidence sealing/Ed25519 attestation remains mandatory when the full
  P0 raw set exists; adversarial sealing/attestation behavior is already covered
  by the M0 test suite.

This evidence closed M0 under v2 only. It is retained as history; the separate
v3 qualification above is the evidence that now counts.

## Product-Critical Open Work

- M2 must first gain the complete v3 raw-derived metric, topology,
  current-identity, and forbidden-path validator gates, then be run formally
  from the accepted source/image after the sequential M0/M1 passes.
- M3 still needs five isolated UAV packet endpoints and all three traffic
  classes in both directions.
- M4–M6 still need the real online Sionna-to-packet causal adapter, contention/
  priority, jammer/mobility causality, and PTY/Ethernet loopbacks through the
  same path.
- M7 needs one supervised integrated matrix and a new kilometre-scale matched
  Gazebo/Sionna scene. The current `scenario_5uav.yaml` terrain is only about
  `200 x 150 m`; the rock visual extent does not supply a validated 20 km
  collision/RF scene.
- M8 still needs soak/stability runs, snapshot-pinned mutable dependency inputs,
  content-addressed image distribution, a no-cache manifest-equivalent rebuild,
  two clean-clone passes, bundle extraction validation, and customer handoff
  instructions.
