# Network/Radio Decisions

Updated: 2026-07-12 UTC.

## Current Authoritative Decisions

- `doc/network_radio_integration_plan_v2.md` supersedes the original plan for
  sequencing, closure, acceptance, and customer-ready claims. The original is
  historical context only.
- Current customer-ready status is false and no historical run is accepted P0
  evidence.
- Only independent raw-derived validation can pass a gate. Runtime producers,
  postprocessors, filenames, dashboards, and summary booleans are observations,
  not acceptance authority.
- Milestones are sequential and only caveat-free `passed` milestones count.
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
- M1 direct MAVProxy telemetry is allowed only as component-health evidence and
  is explicitly packet-path-ineligible.
- `scenario_5uav.yaml` is bounded to its actual approximately `200 x 150 m`
  terrain. M7 requires a separately named, matched and validated kilometre-scale
  Gazebo/Sionna scene; moving models outside the collision mesh or changing
  metadata alone is invalid.
- Mutable status lives in `PROGRESS.md`, `VALIDATION_REPORT.md`, and
  `NEXT_TASK.md`. These reports are excluded from the implementation hash; the
  immutable v2 plan and all runtime/configuration files are hashed.
- Dependency acceptance requires a clean tracked checkout, exact external
  revisions, completed lock, exact project-image digest, and matching runtime
  manifests. A local `latest` tag alone is not reproducible proof.
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
