# Network/Radio Decisions

Updated: 2026-07-14 UTC.

## Current Authoritative Decisions

- `doc/network_radio_integration_plan_v3.md` supersedes v2 and the original plan
  for sequencing, closure, acceptance, and customer-ready claims. Both older
  plans are historical context only.
- Current customer-ready status is false and no historical run is accepted P0
  evidence.
- M0 passed under v3 on clean source
  `95746e37014cce5a974d2dbb7d7e4c8e18b48929`, exact image
  `sha256:2aad1f25789fc1e5c23c3a4b05c91927198ad42ff6e97cde2c26cb2f18979afb`,
  and qualification run `m0_v3_baseline_20260713T130710Z`; the retained
  container exited `0` and independent host revalidation passed. M1 is now the
  first open sequential milestone. The older v2 M0 result is historical only.
- Only independent raw-derived validation can pass a gate. Runtime producers,
  postprocessors, filenames, dashboards, and summary booleans are observations,
  not acceptance authority.
- Milestones are sequential and only caveat-free `passed` milestones count.
- v3 separates final evidence into three acyclic profiles:
  `m7_single_run_candidate`, `m8_repeatability_aggregate`, and
  `m8_customer_handoff`. A repeatability child never contains or self-references
  its own repeatability result; the aggregate re-hashes and revalidates both
  independently sealed/signed children.
- The legacy single-run validation matrix is explicitly fail-closed for
  customer-ready output until v3 profile-specific raw sets, recursive child
  validation, and the final handoff profile are implemented. Even a forged
  all-green legacy result must return customer-ready false.
- P0 uses the real TCP JSONL Sionna provider until another integration replaces
  it end-to-end. The separate pybind11 checkout remains diagnostic and its
  evidence may not be mixed into a JSONL-provider acceptance run.
- Packet ingress and medium fidelity are separate axes. M2 uses `TapBridge` in
  `UseBridge` mode for external namespace packets and a maintained ns-3 CSMA
  shared-medium surrogate. `FdNetDevice` is an alternative ingress design, not
  a required serial stage after TapBridge.
- `tap_bridge_external` is implemented as an M2 diagnostic path. General P0
  runtime selection remains fail-closed until sequential M0/M1/M2 acceptance.
- The CSMA model is an engineering surrogate, not a customer modem waveform or
  firmware/MAC claim.
- The P0 configuration contract is 2.4 GHz carrier, 20 MHz bandwidth, and 20
  Mbit/s maximum shared-medium rate. Overload must offer at least twice modeled
  capacity.
- Valid MAVLink nonce evidence uses a STATUSTEXT marker followed by a
  semantically valid command. COMMAND_ACK is correlated by exact request frame
  hash, decoded sequence/system/command, and time window; it is not claimed to
  contain a nonce field.
- ns-3 owns network queues, contention, priority, loss, delay, and jitter.
  Endpoint adapters may own only finite transport pacing/backpressure, reported
  separately.
- Stopping ns-3 must break the only route. Direct SITL ports and legacy
  localhost MAVLink paths are forbidden from the GCS namespace in acceptance
  runs.
- The continuously healthy lifecycle supervisor and the ns-3 packet-engine
  child are distinct identities. Only the predeclared M7 phase-B and phase-H
  intervals may stop/restart the packet-engine; treating that planned outage as
  a supervisor crash, or allowing any undeclared outage, is invalid.
- M1 direct MAVProxy telemetry is allowed only as component-health evidence and
  is explicitly packet-path-ineligible.
- M1 qualifies only the active Gazebo flight world. Its v3 scene gate binds the
  scenario and source/install asset manifests to the raw running `gz sim`
  world argument, live transport world/entity probe, Gazebo runtime version,
  current source hash, and v3 contract. Gazebo/Sionna alignment begins at M4.
- `scenario_5uav.yaml` is bounded to its actual approximately `200 x 150 m`
  terrain. M7 requires a separately named, matched and validated kilometre-scale
  Gazebo/Sionna scene; moving models outside the collision mesh or changing
  metadata alone is invalid.
- All M7 phases A-I use one unchanged final paired Gazebo/Sionna scene with at
  least 20 km of validated usable collision/RF geometry. Switching worlds or
  Sionna scenes inside the candidate is forbidden.
- Mutable status lives in `PROGRESS.md`, `VALIDATION_REPORT.md`, and
  `NEXT_TASK.md`. These reports are excluded from the implementation hash; the
  immutable v3 plan and all runtime/configuration files are hashed.
- Dependency acceptance requires a clean tracked checkout, exact external
  revisions, completed lock, exact project-image digest, and matching runtime
  manifests. A local `latest` tag alone is not reproducible proof.
- Normalized Python runtime manifests use `pip freeze --all
  --exclude-editable`; editable checkout identity is bound independently by the
  exact Git commit and source manifest. This prevents a report-only commit from
  changing the lock through pip's embedded editable VCS revision without
  weakening source identity.
- M0 qualifies one inspected immutable local image and its exact runtime
  manifests. It does not assert a bit-for-bit independent rebuild because APT,
  rosdep, and prerequisite installers still consume mutable indices. M8 must
  add snapshot-pinned mutable inputs, content-addressed image distribution, and
  a no-cache manifest-equivalent reconstruction.
- The accepted Python runtime is the hash-locked Python 3.10/x86_64 closure
  with NumPy 1.26.4, Sionna RT 1.2.2, and Mitsuba 3.8.0. Full TensorFlow/Sionna
  PHY and pybind11/cppyy are diagnostic-only because the P0 provider imports
  `sionna.rt`, not those alternate stacks.
- Formal runs execute an inspected immutable image ID, retain the stopped
  container for host-side inspection, and record its full 64-character ID.
  Interactive `latest`-tag containers are diagnostic only.
- Sealed evidence requires detached Ed25519 attestation by a host-controlled
  private key, a repository-pinned public key/fingerprint, and a one-time
  external ledger. The private key and ledger must never enter the repository,
  run directory, or run container.
- A minimal M0 qualification has no complete P0 raw-artifact set and therefore
  is neither sealed nor attested. M0 instead requires adversarial
  sealing/attestation tests and explicitly records `p0_eligible=false`; actual
  sealing and external attestation are mandatory for the later complete
  integrated P0 evidence set.
- The provisioned evidence identity is `ams-evidence-2026-07-13` with public-key
  fingerprint
  `sha256:e5807a01ac1c9b54c36f5c87b8714c555ff90bc36e3c83658cb087f8341ca462`.
  Only its public key is tracked; the private key and ledger remain external.
- M2 acceptance requires a content-addressed ns-3 build receipt binding the
  verified official source tree, copied scratch inputs, exact modules,
  CMake/toolchain state, wrapper lock, and executable. A pre-existing binary or
  `M2_SKIP_BUILDS=1` cannot bypass that receipt.
- Physical radio hardware is outside current P0 and must not be probed or
  configured without a separate explicit scope.

## Open Decisions Requiring External Input

- Customer modem/waveform calibration and customer terrain assets, if those
  future claims are required.
