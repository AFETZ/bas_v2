# Network/Radio Progress

Updated: 2026-07-12 UTC.

Authoritative contract: `doc/network_radio_integration_plan_v2.md`.

## Acceptance Status

- Customer-ready: **false**.
- Fully closed sequential milestones: **0**.
- Active milestone: **M0 — Truthful Validation and Reproducible Baseline**.
- Historical plan/status claims do not count toward v2 closure.

| Milestone | Formal status | Current position |
| --- | --- | --- |
| M0 | `in_progress` | Fail-closed validation, provenance, sealing, external attestation, dependency closure, and ns-3 build receipts are implemented and tested; the clean tracked revision, rebuilt accepted image, and completed dependency lock are still absent. |
| M1 | `not_started` | Collector and a 300 s diagnostic exist, but sequential M0/provenance requirements are not met and the validator was hardened after that run. |
| M2 | `not_started` | Real one-UAV TapBridge/MAVLink diagnostic passes functional gates, but its formal result is false on provenance and M0/M1 are open. |
| M3–M8 | `not_started` | No milestone has complete current-run acceptance evidence. |

Only `passed` closes a milestone. Diagnostic or subsystem success is recorded
below but is never added to the closed-milestone count.

## Implemented Foundation

- Added the corrected immutable execution/acceptance contract v2 beside the
  original historical plan.
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
  external one-time ledger. The implementation is tested; the operator key and
  ledger are deliberately not provisioned in the repository.
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
- Added M2 `ams-gcs -> ams-ns3/TapBridge/ns-3 -> ams-uav1 -> SITL`
  lifecycle, MAVLink probe, four-point captures, good/down/recovery runner,
  sealed evidence, and independent adversarial validator.

Current verification:

```text
python3 -m unittest discover -v network/tests 'test_*.py'  -> 116/116 passed
bash network/tests/check_ns3_packet_core_config.sh          -> passed
Python compile, shell syntax, git diff --check              -> passed
historical false-positive validation                        -> exit 1
```

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

## M0 Blockers

- The checkout is dirty and much of `network/` plus both plans is untracked.
  Existing modified files may include user work, so Codex must not silently
  commit the mixed tree.
- `network/config/dependency_lock.yaml` remains
  `rebuild_required_before_acceptance`.
- Evidence signing identity is provisioned: the private Ed25519 key and one-time
  ledger are external, while only the pinned public key/fingerprint is in the
  repository. No accepted runtime evidence has been signed yet.
- The running image digest is
  `sha256:ff0a0b3b3171e51bf328ac58806afc6c1127f9aad83fee8a0727b8136bea0011`;
  it predates the pinned Dockerfile and Python-lock changes. No replacement
  image has been built or accepted.
- Only about `23 GiB` is free on the Docker filesystem. Automatic pruning of
  user-owned images/cache is not authorized.
- Exact runtime manifests and the accepted project-image digest are still
  `REBUILD_REQUIRED`; a clean-clone reconstruction and exact-image verification
  have not run.

## Product-Critical Open Work

- M1 must be rerun for 300 seconds from the clean, pinned M0 source/image.
- M2 must be rerun from the same accepted source after sequential M0/M1 pass.
- M3 still needs five isolated UAV packet endpoints and all three traffic
  classes in both directions.
- M4–M6 still need the real online Sionna-to-packet causal adapter, contention/
  priority, jammer/mobility causality, and PTY/Ethernet loopbacks through the
  same path.
- M7 needs one supervised integrated matrix and a new kilometre-scale matched
  Gazebo/Sionna scene. The current `scenario_5uav.yaml` terrain is only about
  `200 x 150 m`; the rock visual extent does not supply a validated 20 km
  collision/RF scene.
- M8 still needs soak/stability runs, two clean-clone passes, bundle extraction
  validation, and customer handoff instructions.
