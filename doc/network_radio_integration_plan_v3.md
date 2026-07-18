# Network and Radio Integration Execution Plan v3

## Document Status and Authority

This document supersedes `doc/network_radio_integration_plan_v2.md` for every
new network/radio implementation run and every acceptance decision after v3 is
adopted. Versions 1 and 2 remain historical design and audit records. They may
not be used to weaken, reinterpret, or fill missing v3 evidence.

This file is the normative implementation and acceptance contract. Mutable
milestone state remains in:

- `network/PROGRESS.md`;
- `network/VALIDATION_REPORT.md`;
- `network/NEXT_TASK.md`.

Those three records must cite this contract version and agree before a
milestone is counted. They are the **only** tracked paths outside the Q0-Q8
implementation-identity vector defined below; every other tracked path is
assigned to a Q node. A live status record is reporting state, never a runtime,
suite, validator, configuration, or evidence input.

A `status-only` commit is a clean descendant whose cumulative diff from its
referenced technical or identity-reuse base commit changes only those three
paths. It records the evidence-producing and technical-base commits,
receipt/result identity, and Q-vector where applicable; it cannot embed its own
Git identity. It may be created only after the referenced host-final receipt,
component validation result, or sealed-profile validation result is stable. A
separate post-receipt live-status lint then verifies the three current files,
their cited identities, sequential prefix, active milestone, and next command.
The lint derives the current clean `HEAD` as `report_commit`, proves that the
cumulative base-to-HEAD diff contains exactly the three allowed paths, and
records that commit only in its external lint output. Requiring a status file
to contain the hash of the commit containing that same file is forbidden as a
self-referential identity cycle.
That lint and the live status-file bytes are not part of the technical M0 suite,
its frozen suite IDs, or a runtime raw tree. Passing the lint satisfies the
reporting-state closure rule without changing or repairing technical evidence.

A valid status-only descendant does not invalidate an M0 host-final receipt,
M1-M6 component evidence, or an M7/M8 sealed profile. Any change outside the
three paths is not report-only and follows the Q0-Q8 impact rules. A change to
implementation, configuration, validation logic, evidence schemas, or this
contract creates a new affected content identity and requires the evidence
selected by that impact vector to be rerun or independently proven reusable as
defined below.

### v2 to v3 Migration Rule

The completed v2 M0 implementation may be reused, but its old run is not
automatically a v3 closure. It may close v3 M0 only after the M0 qualification
is rerun against:

1. the exact clean v3 source revision;
2. the exact immutable project image ID used by the qualification;
3. runtime manifests recomputed from that image; and
4. v3 provenance/configuration manifests with no acceptance blocker.

The requalification may reference the same content-addressed image previously
qualified by v2 when its identity and runtime manifests still match. It must
record the new v3 source identity and must retain `p0_eligible=false`.

All evidence for M1 and later milestones must be produced under v3 schemas,
v3 validators, and the v3 contract hash. No v2 M1-M8 evidence is grandfathered.
Sequential milestone count under v3 therefore begins at zero until v3 M0 is
requalified. Thresholds and P0 gates are not reduced by this migration.

## Objective

Deliver a repeatable customer-facing engineering demonstration in which:

- five ArduPilot SITL UAVs and one command post run concurrently;
- real bidirectional endpoint bytes traverse an isolated external ns-3 packet
  path;
- MAVLink control, MAVLink payload, telemetry, and an additional data channel
  use that path without a localhost, kernel-routing, adapter, or application
  bypass;
- online Sionna RT consumes current state from the active ROS/Gazebo runtime
  and causally changes per-link ns-3 packet behavior;
- mobility, obstruction, jammer state, contention, and traffic priority have
  causal and measurable effects;
- serial PTY and Ethernet HitL loopbacks use the same real packet path;
- the required 1, 5, 10, and 20 km range/service cases use validated paired
  Gazebo/Sionna geometry; and
- raw evidence, validation, repeatability aggregation, and the customer bundle
  are reproducible from pinned clean checkouts and content-addressed runtime
  artifacts.

The first accepted radio is a documented 2.4 GHz engineering surrogate. It is
not a prediction of a proprietary modem or a certified RF model.

## Scope Boundaries

### P0 Scope

- Backend: `sim_2_4ghz`.
- Linux, Docker, ROS 2, Gazebo, ArduPilot SITL, ns-3, and online Sionna RT.
- Omni antennas, a continuous jammer, and a maintained ns-3 shared-medium
  surrogate are allowed when explicitly identified.
- All accepted packet metrics originate from real external endpoint bytes.
- PTY and Ethernet HitL loopback, the two declared MAVLink UART mappings per
  UAV, point-to-multipoint additional data, and the 20 km range point are P0
  gates, not optional demonstrations.
- Scenario phases A-I, the integrated soak, repeatability, and extracted bundle
  validation are required for customer-ready status.
- The final engineering scene preserves the original product envelope: at
  least one validated 20 km path, kilometre-scale terrain/building effects,
  terrain relief and settlements described by the final-scene contract below,
  and the numeric low/medium-altitude AGL paths defined by the final-scene
  contract below.

### Explicit Non-Claims

- No certified RF accuracy.
- No match to customer modem waveform, MAC, firmware queue, sensitivity, or
  antenna behavior unless customer data is supplied and separately calibrated.
- No physical RF hardware validation in P0.
- No cached-only, replay-only, mock, test, or free-space Sionna result may
  satisfy an integrated P0 gate.
- Plots, dashboards, heatmaps, videos, and producer-written PASS flags are not
  authoritative acceptance evidence.

## Normative Closure Rules

A milestone is `passed` only when all of the following are true:

1. Every listed deliverable exists in the current clean checkout.
2. Every acceptance result is independently derived from the required raw
   evidence.
3. All milestone tests and validators exit zero.
4. No acceptance caveat, TODO, placeholder, mock, synthesized proof, missing
   measurement, or known contradiction remains.
5. Evidence binds the exact source, contract, resolved configuration,
   dependency lock, image identity, and run identity required by its profile.
6. Required raw seals, signatures, ledger records, and child revalidation pass.
7. The three mutable status records agree.

Milestones are sequential. Later code may exist, but only the longest fully
passed prefix M0..Mn is counted. A later regression that invalidates an earlier
gate prevents customer-ready status until the final candidate reruns and passes
the affected gate.

Allowed states are `not_started`, `in_progress`, `failed`,
`blocked_external`, and `passed`. `Partial`, `mostly passed`, and `passed with
limitations` are never closure states.

## Evidence and Attestation Model

### Runtime Identity

Every raw runtime records at least:

- `plan_version=3` and this contract hash;
- one `run_id`, profile, scenario/phase IDs, UTC start and end;
- execution Git commit, dirty state, diff hash, the Q0-Q8 path-ownership map and
  content-manifest vector, any independently checked descendant-reuse receipt,
  and resolved configuration hashes. A later report commit is recorded
  separately and never substituted for the execution commit;
- dependency lock, external dependency revisions, exact image ID, restored
  image digest/archive hash when applicable, and runtime package manifests;
- container IDs, process IDs, executable hashes, command lines, namespaces,
  capabilities, and host/kernel/GPU identity;
- canonical scene-bundle identity; and
- clock source, timestamp domains, and clock-correlation samples.

One causal claim may use only one continuous runtime and one `run_id`. PCAP,
process logs, Sionna results, queue traces, node state, and command outcomes
from different runs may not be combined into a scenario result.

### Raw Evidence Is Authoritative

- Runtime producers write observations, not acceptance decisions.
- Collection and post-processing may normalize or copy data but may not create
  a packet, result, phase, metric, or PASS value that was absent at runtime.
- Required latency, jitter, loss, goodput, topology, current-state, causality,
  and no-bypass results are recomputed from raw timestamps, decoded bytes,
  counters, lifecycle events, and configuration/provenance records.
- A non-empty file, a timeout, a process name, a summary boolean, or an
  `actual_ns3=true` producer field is never sufficient by itself.

### Seal Sequence

For every sealed profile the sequence is fixed. For a runtime profile, raw
production ends first; for an aggregate or handoff profile, deterministic
package assembly ends first:

1. The supervised runtime or deterministic assembly ends and every writer
   closes its files.
2. The collector/assembler writes its final inventory and marks the profile
   content tree read-only.
3. A profile content manifest hashes every allowed file, records its
   schema/profile and exclusion list, and produces the profile seal document.
4. The profile-seal hash is signed by the configured evidence-attestation key
   and appended to the external append-only ledger.
5. The independent profile validator verifies the content tree, signature,
   public-key fingerprint, ledger inclusion, provenance, and schema before
   deriving acceptance results into a separate validation tree.

Validation reports, customer prose, plots, and bundle indices are never added
back into a child raw seal. Any raw mutation after sealing invalidates the
signature and closure. Regeneration creates a new run ID; resealing an altered
run under the old identity is forbidden.

### Qualification Boundary and Host-Final M0 Receipt

Formal M0 qualification uses the clean committed candidate tree as a read-only
source mount. The only writable mounts are a distinct, initially empty
container scratch/build/cache area and a distinct, initially empty raw-artifact
area. The host-final receipt directory is host-owned and is never mounted into
the container. An uncommitted suite input, writable source mount, overlapping
source/artifact path, or undeclared mount fails qualification.

The suite-producing M0 container and the independent suite re-execution are
deliberately **unprivileged**: their capability bounding set is empty,
`no-new-privileges` is active, the network mode is `none`, and neither sudo nor
another set-ID transition can restore privilege. `CAP_SYS_ADMIN`, a privileged
container, or any equivalent mount authority is forbidden because it can
remount a Docker `:ro` bind writable without changing the host-side mount
declaration. Target-runtime namespace, TUN and passwordless-elevation
capabilities are qualified in a separate exact-image host-final probe. That
probe receives no candidate-source, external-source, artifact, control or
receipt mount; host-owned raw prestart/final inspection and command output bind
its exact image, capabilities and result. Its additional privilege therefore
cannot influence suite bytes or evidence, and its success cannot substitute for
the unprivileged suite runs.

No host-supplied, writable, unmanifested, or unlocked virtual environment,
build/install output, Python bytecode/cache, import-path variable,
`sitecustomize`, `usercustomize`, test plugin, or user-site package may
influence collection or execution. The Python launch mode is selected and
hash-bound before container creation as exactly one of:

1. `isolated_explicit_path`: the suite interpreter starts with `-I -S` (or an
   independently proved equivalent that disables automatic site
   initialization), then loads only an exact ordered explicit path whose
   directories and required files are content-hashed in the lock. A child
   interpreter that cannot preserve the parent flags may use normal site startup
   only with user site disabled and an exact inert `sitecustomize` guard from the
   clean read-only source as the first explicit path entry. The guard path/hash,
   source manifest, marker fields and absence of imports, hooks and path mutation
   are checked; the child must otherwise resolve the identical allowed roots; or
2. `qualified_image_site`: automatic image site initialization, image-owned
   bytecode, customization modules, and image-owned user-site packages are
   allowed only when every allowed absolute root, customization-module byte
   hash, distribution/file manifest, and ordering rule is frozen in the
   qualified-image import policy. No host mount or run-writable directory may
   satisfy an allowed image root.

In either mode, inherited `PYTHONPATH`, `PYTHONHOME`, user-site selection, and
plugin-autoload variables are cleared. A runner-generated import path is
allowed only when it exactly equals the predeclared ordered hash-locked path.
Build output and every runtime-generated bytecode/cache file use only
predeclared initially empty container-owned scratch paths whose environment and
inventory/exclusion policy are hash-bound before launch. The raw import/plugin
trace records selected mode, ordered `sys.path`, customization modules, module
origins and byte/distribution hashes, and proves that every loaded suite
dependency is either from the clean committed source mount or an allowed locked
path in the qualified image.

Before collection, raw image/container inspection retains the exact image ID,
`Entrypoint`, `Cmd`, `User`, complete ordered `Env`, working directory,
capabilities, network mode, and complete ordered mount set with source,
destination and access mode. The container is retained through finalization and
re-inspected after suite termination; any missing field or difference fails.
Docker-generated order is retained exactly in the raw inspection and must be
unchanged between the initial and final inspection of that same container; the
declared expected environment and mounts are checked as exact semantic
name/destination maps because the daemon does not promise a stable construction
order across otherwise identical `create` operations. No field, duplicate name
or extra mount may be hidden by that normalization.
The live qualification probe recomputes, rather than copies, every source,
contract, configuration, dependency-lock, runtime package and executed-binary
identity from the actual mounted tree and running container.

For M0, `executed-binary identity` means the predeclared
acceptance-critical set: image entrypoint, acceptance/host-final entrypoints,
shell and Python interpreters, project build launcher, dependency/runtime-lock
probes, suite runner, validator, and every separately invoked script. Their
resolved paths and byte hashes are recomputed live. A raw ordered execution
policy records the exact `PATH`, allowed executable roots, absolute commands in
the source-bound runners, and command results used by acceptance. An incidental
transitive utility need not have an individual predeclared file hash only when
it resolves exclusively inside one of those immutable-image roots, its owning
package is present in the twice-recomputed runtime package manifest, and neither
host-supplied nor run-writable bytes can satisfy that path. An unresolved owner,
an executable root outside the policy, or a writable directory in command
resolution fails M0; a kernel-wide trace of utilities that make no acceptance
decision is not required.

Collection freezes one exact ordered suite-ID manifest before execution. The
suite command consumes exactly those IDs in that order; missing, extra,
duplicate, reordered or unexpectedly skipped IDs fail. Every collection and
execution input, including runners, validators, tests, fixtures, `conftest` and
pytest configuration, inline assertions, schemas, this contract, dependency
lock and resolved configuration, is tracked in the clean commit and hash-bound
by the pre-run input manifest. The technical suite may test the immutable
status-record schema with fixtures, but it may not import, open, parse, or branch
on the three live status records. Their current semantic consistency is checked
only by the post-receipt live-status lint defined above.

The formal M0 technical suite is Q0-scoped: every discovered test and its
complete import/fixture/configuration closure is owned by Q0. `Complete suite`
in M0 means the complete frozen Q0 qualification/adversarial suite and its
explicit semantic-coverage map, not repository-wide discovery of Q1-Q8
milestone tests. A repository-wide regression command may run as a separate
preflight and may block active development, but it is not an M0 acceptance
input. Each Q1-Q8 validator/test closure is instead authoritative for its owning
milestone and follows the impact table below. This separation prevents a later
downstream test edit from silently changing M0's consumed identity.

While `q0_conservative_bootstrap/v1` is active, all tracked test closures are
Q0 by definition, so its frozen M0 suite is the exact repository-wide discovery
and the preceding separation has not yet begun. This is deliberately stronger
and more invalidating, not permission to omit a test. The first granular map
must split and freeze the Q0 suite and every downstream suite atomically; that
map change itself reopens M0 under the bootstrap rule.

A container-written captured probe or an `accepted=true` producer field is raw
evidence only and can never be the formal acceptance record. After the suite
process and all raw writers terminate, a host finalizer that was not writable
by the container reads each artifact relative to an already opened directory
file descriptor, forbids symlink traversal, opens with no-follow semantics, and
checks `fstat` before and after the read. It rejects a non-regular/replaced file
or any difference between independently computed before-read and after-read
artifact manifests. It then rederives the suite outcomes and all inspected/live
identities and, only on success, writes one host-final receipt by fsyncing a
temporary regular file, atomically renaming it, and fsyncing the parent
directory. Only that stable, independently reproducible host-final receipt may
close M0.

The host finalizer retains the exact raw initial/final image and container
inspections, isolated-capability probe, fresh-source identity records, and fresh
re-execution outputs from which it derived the receipt. The operational
before/after manifest includes inode/device/timestamp identity only for TOCTOU
detection; a separate portable content manifest binds path, kind, mode, size and
content hash and is reproduced after publication. Every fallible validation,
copy/readback comparison and nested-directory fsync completes before the one
canonical no-replace directory rename that is the acceptance point. After that
rename the implementation may report success only; a platform without the
required no-replace atomic primitive fails before publication. The parent
directory is fsynced as part of that terminal transaction, and an interrupted
or failed candidate remains non-canonical/quarantined without a
`formal_accepted=true` receipt at the canonical path.

### Acceptance Profiles

`m7_single_run_candidate`

- Contains exactly one continuously supervised runtime with the mandatory
  `Q1_health` envelope, executable phases A-I, and one source/config/image
  identity.
- It may include the predeclared post-A-I soak extension required when the run
  is intended as an M8 child; M7 validation treats that window as health
  evidence but makes no repeatability claim.
- Has one profile-specific raw manifest, seal, signature, and ledger entry.
- Is independently validated only after sealing.
- May close M7 by itself only when `Q1_health` and all A-I gates pass. It makes
  no repeatability or customer-handoff claim.

`m8_repeatability_aggregate`

- Contains no substituted child raw files and makes no new single-run causal
  claim.
- References exactly two successful independent
  `m7_single_run_candidate` children produced from separate clean-clone
  executions of the same final revision, contract hash, dependency lock,
  resolved config, canonical scene bundle, and accepted image identity.
- Recomputes and verifies every child raw hash, raw seal, signature, ledger
  inclusion, provenance, and validation result from the child package.
- Contains child identities/hashes, deterministic comparison inputs, and the
  already produced post-seal child validation reports. It never contains its
  own aggregate-validator output. It has its own aggregate seal, signature,
  and ledger entry before aggregate validation begins.

`m8_customer_handoff`

- References the accepted repeatability aggregate, content-addressed image,
  rebuild proof, operator material, limitations, and replayable sealed child
  packages.
- Has a profile-specific bundle manifest/seal, signature, and ledger entry.
- Is validated after extraction into an unrelated directory with no access to
  the original workspace, mutable image tag, or undocumented host files.
- Revalidates both repeatability children, including their raw trees,
  signatures, ledger records, and P0 results. It may not trust copied summary
  JSON.

M0 key/sealer adversarial tests do not pretend that a minimal M0 run is a full
P0 seal. M1-M6 formal closure evidence requires independent revalidation of
every required raw artifact, hash binding through run provenance, and a
write-once run ID. A dedicated component manifest, read-only tree, external
signature, and ledger attestation may be used for component progress but are
not mandatory; if sealing is used, it follows the fixed sequence. Any later raw
byte mutation or revalidation failure invalidates component closure and creates
a new run. Component evidence cannot be relabeled as one of the three profiles
above. M7/M8 seals, signatures, external ledger proofs, and full rederivation
of M1-M6 logic remain mandatory for final P0.

### Fail-Closed Conditions

Integrated P0 is false if any of the following occurs. The terms `required RX`
and `required metric` are evaluated against the normative phase-applicability
table below; an intentionally impaired interval is not a positive-delivery
interval:

- a positive-delivery traffic matrix cell has fewer transmitted or received
  application units than its declared minimum;
- an applicable latency, loss, jitter, goodput, queue, freshness, or timing
  field is null or cannot be derived from raw events;
- in a positive-delivery window, a capture point required by the
  phase-applicability schema has no decoded matching data bytes; in a
  stopped/down window, a required persistent capture is not continuously alive
  with usable counters, or a remote capture contains forbidden matching bytes;
- in a positive-delivery window, heartbeat and the expected
  `COMMAND_ACK`/telemetry do not return through the modeled path;
- a required service crashes, exits early, binds incorrectly, or never becomes
  ready, except that the ns-3 packet-engine child is intentionally absent only
  inside the predeclared M2, M3, and M6 component stopped windows and the M7-B
  and M7-H stopped windows defined below;
- a UAV pose is missing or stale;
- a mock/test/free-space/cached-only provider is used;
- active no-bypass, PTY, Ethernet, 20 km, or A-I evidence is missing;
- source/config/runtime/scene provenance or a required raw seal is missing;
- raw evidence crosses run IDs or is changed after sealing; or
- a child signature, ledger inclusion, or independent child revalidation
  fails.

## Accepted Runtime Architecture

```text
scenario/lifecycle supervisor (persists for the complete candidate)
  |
  +-- GCS / command endpoints in ams-gcs netns
  |       |
  |       v
  |   ground veth/TAP capture point
  |       |
  |       v
  +-- ns-3 packet-engine child (stoppable only in declared component/B/H windows)
  |       | external ingress -> per-link queues/channel -> external egress
  |       v
  +-- per-UAV veth/TAP capture points in ams-uav1..5
  |       |
  |       v
  |   UAV MAVLink routers / endpoint adapters -> ArduPilot SITL 1..5
  |
  +-- ROS/Gazebo current state -> versioned asynchronous Sionna worker
              -> latest non-expired per-link state -> ns-3 packet engine
```

The lifecycle supervisor, endpoints, Gazebo, SITLs, state tracker, Sionna
worker, clocks, and collectors required by the active milestone persist through
all of its declared windows. The ns-3 packet engine is a supervised child with
explicit lifecycle states. It may stop only during no-bypass intervals declared
before execution: the M2, M3, and M6 component stopped windows and M7 phases B
and H. Endpoints remain live during those intervals. Any other packet-engine
outage, supervisor restart, or hidden endpoint restart fails the candidate.

Stopping the packet engine must break the only GCS-to-UAV route. No Linux
bridge, router, proxy, adapter, mirrored socket, or application tail may forward
the real bytes independently of ns-3.

Standard FlowMonitor is authoritative only for flows originated or terminated
inside ns-3. For external TAP/veth traffic, accepted proof comes from decoded
PCAP, endpoint counters, ns-3 device events, ns-3-owned queue/channel traces,
and a custom external-flow identity/tagging mechanism. Empty or unrelated
FlowMonitor output cannot pass or fail the external path by itself.

## Endpoint and Packet Identity Contract

The accepted matrix is five UAVs by three traffic classes by two directions.
Every cell records:

- UAV name, MAVLink system/component ID, and process identity;
- source/destination namespace, interface, MAC/IP/port or real PTY endpoint;
- ns-3 ingress/egress device and per-link/per-class queue identity;
- class marker and DSCP/TOS-to-queue mapping;
- serial baud/pacing policy when applicable;
- capture locations; and
- raw TX, enqueue, dequeue, channel/drop, egress, RX, and acknowledgement
  counters.

Control evidence uses real MAVLink frames. Each attempt carries a valid
MAVLink nonce marker in a semantically legal text-capable field, followed by a
valid command. It does not misuse a reserved `COMMAND_LONG` parameter. Raw
evidence records exact frame hashes and decoded source, target, sequence,
command, and result. The validator proves the same byte identity at each path
stage and correlates the bounded returning `COMMAND_ACK`/requested telemetry.
It never claims an ACK echoes a nonce when that MAVLink message has no nonce
field.

Payload and additional data carry full run nonce, flow ID, direction, and
monotonic sequence. Metrics match those identities, not filenames or Ethernet
padding.

## Active Topology, Current State, and Metrics Contract

Every external-path run captures raw, timestamped snapshots before traffic,
after readiness, at each lifecycle transition, and after completion:

- namespace inode/identity, links, addresses, routes, rules, neighbors, bridge
  membership, TAP ownership, sockets, and firewall/NAT state;
- container/process identity and executable hash for endpoint adapters and
  ns-3;
- resolved ns-3 nodes, devices, links, addresses, queues, rates, and active
  error-model mapping emitted by the live packet engine; and
- configured versus live-resolved config hashes.

The validator derives topology continuity and proves there is exactly one
allowed route. A diagram or intended YAML is not active-topology evidence.

For every packet/transaction, raw timestamps must support derivation of:

- offered, ingress, enqueue, dequeue, channel/drop, egress, receive, and ACK
  events where applicable;
- delivered count and loss denominator;
- adapter, ns-3 queue/channel, and end-to-end latency;
- jitter and goodput over declared windows; and
- the exact current node/link state applied to the packet interval.

Metrics must expose sample counts, denominators, warm-up exclusion, clock
domain/correlation, and percentile method. Required values may not be copied
from producer summaries.

## Canonical Scene and Coordinate Contract

M1 proves only the active Gazebo flight world. Its evidence correlates the live
simulator with its resolved launch inputs instead of trusting a configured
filename alone:

- Gazebo version from accepted runtime provenance and raw launch/process
  arguments;
- the live Gazebo transport world name and timestamped entity/model inventory;
- the resolved world URI/path, world-file byte hash, and a manifest of
  transitively resolved model/resource URIs and hashes; and
- the resolved launch-configuration identity that selected that world.

Cross-engine Gazebo-to-Sionna alignment begins at M4, not M1. M4 introduces a
canonical scene bundle containing:

- immutable Gazebo world/assets and Sionna scene/assets manifests;
- both scene hashes and a bundle ID;
- Gazebo world, ROS odometry, ArduPilot NED, and Sionna frame definitions:
  origin, axes, handedness, units, quaternion convention, and transform
  version;
- at least six distributed landmarks, including three non-collinear points,
  matching within 1 metre;
- RF/collision geometry correspondence checks and usable bounds;
- antenna positions/orientations and explicit omni/isotropic assumptions; and
- a machine-validated coordinate conversion fixture.

Every node state records timestamp, source topic, source frame, transform
version, pose, orientation, and freshness. Fixed isotropic arrays may not be
used to claim orientation or pattern effects.

The final kilometre-scale canonical bundle used by M7 must contain a genuine
paired Gazebo/Sionna scene with a validated path supporting at least 20 km
endpoint separation. That one bundle is loaded before the M7 runtime begins and
remains active and unchanged throughout phases A-I; M7 may not restart Gazebo,
switch worlds, or switch Sionna scenes between phases. Any local engineering
world qualified by M1 is component evidence only. Expanding metadata, moving a
model outside collision/RF geometry, or using different geometry for packet
results and heatmaps fails the gate.

## Asynchronous Link-State and Late-Update Contract

- ns-3 runs in real time for endpoint traffic and never synchronously waits for
  Sionna.
- Every node snapshot has `node_state_seq` and generation time.
- Every Sionna query has `query_id`, `node_state_seq`, request generation/send
  times, provider mode, bundle ID, and directed-link inputs. Every result
  correlates the query/state/link identity and carries provider timing plus an
  explicit status; only a successful result carries a validity interval,
  expiry, and finite per-direction physical/link values.
- Every applied update has `applied_state_id`, source IDs, application time,
  target link/direction, and exact packet/time interval.
- ns-3 selects independently for each directed link the newest completed state
  whose generation supersedes the previously applied state and whose expiry is
  in the future.
- Out-of-order, duplicate, superseded, or expired results are never applied.

The v3 P0 late policy is explicit and fail-closed: after the current per-link
state expires, that directed link enters unavailable/error state until a fresh
state is applied. Hold-last beyond expiry is forbidden for accepted P0. Each
late/discard/fail-closed transition is raw-logged and packets affected by it
remain in the offered/delivery/loss denominator. Latency and jitter are derived
only for delivered units; an undelivered attempt instead has a finite,
raw-derived outcome timeout and is never represented as a fabricated latency.

Default P0 thresholds remain:

- runtime real-time factor `0.95 .. 1.05` outside the declared packet-engine
  outage transition;
- link-state age p95 no more than two configured Sionna update periods;
- required stale-pose samples exactly zero;
- late update ratio at most 5 percent;
- control end-to-end p95 under priority overload at most 250 ms; and
- control loss under priority overload at most 5 percent.

The full six-node/jammer workload is benchmarked before selecting the update
period. Raising a deadline above the update period does not make stale state
fresh.

## Configuration and Radio-to-Packet Contract

One generated resolved configuration is authoritative for:

- carrier frequency, bandwidth, transmit power, noise figure, sensitivity;
- shared-medium capacity, service tiers, offered rates, queue bounds, and QoS;
- update period, validity duration, deadlines, and fail-closed late policy;
- canonical scene bundle and transforms; and
- SINR/rate/PER mapping version and calibration/source note.

Sionna supplies physical state: pathloss, RSSI, signal/interference/noise power,
SINR, J/S, geometry state, and freshness. A deterministic ns-3 adapter maps it
to per-directed-link service rate and packet error behavior. Receiver-wide or
global worst-link impairment is forbidden. Mapping tables have a cited source,
calibration limitations, version, units, boundary behavior, and deterministic
tests.

Endpoint adapters may implement finite UART pacing/buffering and backpressure.
ns-3 alone owns network contention, class service queues, network delay/jitter,
and modeled packet loss. Adapter and ns-3 delay are always reported separately.

Validation fails on any configured/resolved mismatch, including mismatched
bandwidth or a service tier above modeled capacity without a separate explicit
capacity model.

## Normative Executability Addendum for M2-M8

This section makes the milestone prose below executable. A value written as
`at least` is a minimum; a value written as `at most` is a maximum; a closed
range is a fixed admissible range; and a cardinality written as `exactly` is a
fixed protocol cardinality, not a minimum that may be increased. A resolved
configuration may be stricter only where doing so preserves those distinctions.
It may not lower a minimum, raise a maximum, widen a tolerance, change an
expected failure into `not_applicable`, or select a more favourable rule after
raw production starts. Every value and applicability decision is hashed before
the run in the resolved phase manifest.

### Phase Applicability and Expected-Outage Semantics

The validator applies the following phase classes. `Application unit` means a
decoded MAVLink frame or a complete nonce/sequence-bearing data record, never
an ARP packet, Ethernet padding, retry copy, or filename count.

| Phase/window | Required offered evidence | Required receive/outcome evidence | Applicable metrics |
| --- | --- | --- | --- |
| M2 good and recovery | Exactly 10 new valid control transactions per window after full readiness | exactly 10 correlated ACKs and 10 requested telemetry responses; at least three fresh heartbeats | transaction success, loss, ACK/telemetry latency, heartbeat age, path-stage latency and jitter |
| M2 stopped | Exactly 5 new transactions after the raw packet-engine `stopped` event and drain | zero matching remote RX/ACK/telemetry; heartbeat absent for `max(3 s, 3 configured heartbeat periods)` | offered count, loss exactly 1.0, finite outcome timeout and persistent-component continuity; delivered latency and jitter are inapplicable |
| M3 and M7 A positive matrix | At least 20 unique units in every UAV/class/direction cell during a window of at least 30 s | `received >= ceil(0.95 * unique_offered)` in every cell | per-cell loss, latency, jitter, goodput, counters and path identity |
| M3 stopped | At least 5 new unique units in every UAV/class/direction cell after the raw `stopped` event and drain | zero matching remote delivery in every cell; every UAV heartbeat absent at the GCS for the declared heartbeat timeout | offered count, loss exactly 1.0, finite outcome timeout and topology/process continuity; delivered latency and jitter are inapplicable |
| M4/M7 E jammer `off-1`, `on`, `off-2` | In each window of at least 30 s, at least 100 unique units in every predeclared target `flow_group` and at least 100 in its matched control `flow_group` | both off windows deliver at least 95 percent; the predeclared `on` mode satisfies the quantitative rule below | physical/link-state metrics, delivery ratio, loss, state age and locality; delivered latency/jitter apply only when `on` is predeclared positive-delivery |
| M4/M7 F `good` and `recovery` | In each window of at least 30 s, at least 100 unique units in every predeclared target `flow_group` and at least 100 in its matched control `flow_group` | both links deliver at least 95 percent and recovery satisfies the quantitative tolerances below | physical/link-state metrics, delivery ratio, loss, delivered latency/jitter, state age and locality |
| M4/M7 F `down` | After the obstructed state is applied, at least 100 new units in every predeclared target `flow_group` and at least 100 in its matched control `flow_group` | target-link delivery exactly zero; control-link delivery at least 95 percent | target loss exactly 1.0 and finite outcome timeout; target delivered latency/jitter are inapplicable; all positive control-link metrics remain applicable |
| M4/M7 F expiry exercise | At least 20 new target-link units after expiry and before recovery, followed by at least 100 new units after a fresh state is applied | zero remote delivery while no fresh state exists; post-fresh delivery at least 95 percent | loss exactly 1.0 and finite outcome timeout while unavailable, discard/order/expiry events, and positive delivered metrics after recovery |
| M5/M7 `C-normal`, `C-overload`, `D-single`, and `D-five` | The fixed warm-up, duration, byte-rate and unit-count rules below | all required groups meet their named delivery and comparison rules; control meets its hard thresholds | offered/ingress rate, delivery, loss, latency, jitter, goodput, every queue and arbitration event |
| M6/M7 H good and recovery | At least 100 unique records for each of the ten UART mapping IDs and for each of the two declared Ethernet directions | at least 95 percent delivery plus correlated responses in every mapping/direction | stream/frame reassembly, pacing, adapter/ns-3/end-to-end timing, loss and backpressure |
| M6/M7 H stopped | At least 20 new records for each of the ten UART mapping IDs and for each of the two declared Ethernet directions after `stopped` | zero remote matching data in every mapping/direction; finite timeout; recovery uses new nonces | offered count, loss exactly 1.0 and endpoint continuity; delivered latency is inapplicable |
| M7 B pre-stop and recovery | Exactly 2 new valid control transactions per UAV, 10 total, in each window | exactly 2 correlated ACKs and 2 requested telemetry responses per UAV; at least three fresh heartbeats per UAV | transaction success, loss, ACK/telemetry latency, heartbeat age, path-stage latency and jitter |
| M7 B stopped | Exactly 1 new valid control transaction per UAV, 5 total, after `stopped` and drain | zero matching remote RX/ACK/telemetry for every UAV; every UAV heartbeat absent for the declared heartbeat timeout | offered count, loss exactly 1.0, finite outcome timeout and persistent-component continuity; delivered latency/jitter are inapplicable |
| M7 G heatmaps | At least 2,601 uniquely identified real-provider grid queries covering the full grid, with the coverage/sample rules below | all five numeric grids and deterministic renderings pass raw-query, mask and tolerance checks | query success/latency/state age, finite-cell coverage, RSS, SINR, J/S, degradation-zone and service-tier consistency |
| M7 I directed range cell | Offered rate, unit count and dwell defined by the range contract below | selected tier, delivery, latency and goodput meet that directed cell, or a declared `down_allowed` directed cell proves failure and new-window recovery | positive cells: RSSI/pathloss/SINR/J/S, selected tier, loss, latency, jitter and goodput; `down_allowed`: physical state, loss and finite outcome timeout, with delivered latency/goodput inapplicable |
| M8 soak | The exact non-zero 30-cell rate vector and temporal-bin rules below for the complete named soak | every cell meets the soak-specific delivery, loss, latency and goodput rules in every required bin | soak health, timing, queue, packet and steady-state radio metrics; A-I causal/outage deltas are not reapplied inside soak |

Acceptance measurement windows are disjoint half-open monotonic intervals
`[start_monotonic_ns, end_monotonic_ns)`. Every unicast offered
application-unit identity is assigned to exactly one phase, window and matrix
cell. A point-to-multipoint root record is assigned once to its P2MP window and
has five derived `(root_identity, intended_receiver)` delivery-leg identities,
each assigned to exactly one receiver cell; the root is never duplicated at
ingress or service. A named health envelope may overlap a traffic window only
where this contract says so; the overlap never duplicates traffic counts or
causal samples. An event on a boundary belongs to the later interval.

Before every positive-delivery window, full readiness is stable for 10 seconds
and previous-window test traffic has reached a terminal delivered, dropped or
timed-out outcome. Continuous liveness traffic is not stopped for drain; its
identity remains bound to its original window and it cannot satisfy a later
minimum. Except for a declared load warm-up, every declared userspace and ns-3
queue is empty at the measurement boundary.

A stopped-window sequence is fixed: full readiness is stable for 10 seconds;
the supervisor emits the requested-stop event; the raw packet-engine state
becomes `stopped`; captures remain active; old test traffic and queues drain;
and only then does the negative interval begin. During that interval every
readiness predicate except `packet_engine_ready` remains continuously true and
the expected engine state is `stopped`. After restart, the engine becomes ready
within 30 seconds and full readiness is stable for a new 10 seconds before
recovery traffic. For F physical-down and F-expiry windows, all services remain
ready; only the predeclared directed-link state is unavailable.

For an E `expected_down`, F physical-down, or F-expiry unavailable interval,
full readiness is stable for 10 seconds in the preceding valid-link state. The
runner then applies the predeclared jammer/geometry/fault stimulus, waits for
the declared state or expiry event, lets earlier test traffic and queues reach
terminal outcomes, and only then opens the negative measurement interval. No
service-readiness exception applies; the unavailable state is link-local and
must be derived from raw provider/adapter/ns-3 events.

For `C-normal`, `C-overload`, `D-single`, and `D-five`, previous-window traffic
and queues drain first, then full readiness is stable for 10 seconds, then the
named load runs for a 10-second warm-up followed immediately by its 60-second
measurement interval. Warm-up units cannot satisfy measurement counts, but
warm-up queue state is intentionally not drained at the measurement boundary.

Every attempt has a 3-second ACK/data outcome timeout unless the table specifies
the longer heartbeat timeout. Late packets retain their original attempt
identity and cannot be reassigned to a later window.

Loss always uses unique offered application units as its denominator. Latency
and jitter use unique delivered units only and report their delivered sample
count. Timeout duration is a separate outcome metric for undelivered units.
`null`, zero samples, or a synthetic timeout timestamp fails any applicable
field; an inapplicable field is encoded by a schema enum and reason, not `null`.

### Quantitative Causality, Contention, and Priority

A `flow_group` is the resolved tuple `(UAV, traffic_class, direction,
endpoint_form, directed_link_id)`. All comparison windows use the same packet
sizes, flow definitions, endpoint adapters, physical state, random-stream map
and resolved configuration except for their one declared stimulus. The
three-concurrent-group rule applies only to multi-flow windows. Every concurrent
group uses a distinct predeclared ns-3 random stream, and at least three groups
are active in each multi-flow comparison.

Raw evidence records seeds and per-group metrics. A paired bootstrap over
unique application units matched by flow group and ordinal send slot uses at
least 10,000 resamples and a predeclared seed to report a 95-percent confidence
interval. A required deterioration or recovery delta passes both in aggregate
and at the conservative confidence bound. `D-single` has exactly one designated
reference group; the identical group and ordinal slots in `D-five` form the
paired sample. The four newly concurrent groups prove shared-medium contention
but are not paired against nonexistent `D-single` units.

- The M4/M7 F obstruction exercise contains exactly two predeclared
  `good/down/recovery` fixture sequences: one terrain-shadow and one
  building-blocked. For each fixture, `down` decreases target-link
  median SINR by at least 3 dB and has exactly zero target delivery. The
  designated control link stays within 1 dB median SINR, 5 percentage points
  delivery and one service tier of `good`, with delivery at least 95 percent.
  `Recovery` delivers at least 95 percent and returns within 1 dB median SINR,
  5 percentage points delivery and one tier of `good`.
- M4/M7 jammer uses `off-1`, `on`, and `off-2`. `On` decreases median SINR or
  increases median J/S by at least 3 dB and worsens delivery by at least 10
  percentage points or one tier. Before the run it is classified as either
  `positive_impaired`, which requires at least 20 delivered target units per
  flow group and applicable latency/jitter, or `expected_down`, which requires
  zero delivery, loss exactly 1.0 and explicitly inapplicable delivered
  latency/jitter. `Off-2` delivers at least 95 percent and recovers within 1 dB,
  5 percentage points and one tier of `off-1`.
- `C-normal` and `C-overload` each contain the readiness and warm-up sequence
  above plus a 60-second measurement. Both use all five UAVs, all three classes
  and both directions. Each UAV offers at least 60 valid control transactions,
  at least 10 in every non-overlapping 10-second bin and with no inter-offer gap
  over 2 seconds. Every payload and additional-data flow group offers at least
  300 unique units spread with at least 50 in each 10-second bin. A unit-count
  minimum never substitutes for the byte-rate requirement.
- During at least 90 percent of `C-normal`, aggregate external ingress offered
  rate is between 50 and 70 percent of instantaneous resolved bottleneck
  capacity; every cell delivers at least 95 percent. During at least 90 percent
  of `C-overload`, it is at least twice that capacity. Capacity is the minimum
  current aggregate ns-3-owned service budget on the traversed shared-medium
  path after Sionna mapping, not a manually lowered label or adapter rate.
- In `C-overload`, control p95 is at most 250 ms and control loss at most 5
  percent in every UAV flow group. Every payload and additional-data group still
  delivers at least 20 unique units so its positive metrics are real. **Both**
  every payload and every additional-data `flow_group` degrades relative only
  to its own corresponding `C-normal` group by at least one of: 25 percent
  higher p95, 5 percentage points more loss, or 10 percentage points less
  achieved/offered goodput.
  Priority must be attributable to ns-3 service/queue events; adapter pacing,
  socket buffering or pre-ns-3 drops cannot satisfy it.
- `D-single` and `D-five` each contain the same readiness, warm-up and 60-second
  measurement sequence. The designated reference group offers at least 300
  units with at least 50 in every 10-second bin in both windows. `D-five` adds
  four homologous UAV groups with the same per-group packet size and offered
  rate, each meeting the same count/bin rule. Concurrency must increase the
  reference group's ns-3-owned p95 queue/channel latency by at least 25 percent
  or reduce its achieved/offered goodput by at least 10 percentage points, and
  every D-five group has non-zero arbitration or queue-wait events. `D-single`
  delivery is at least 95 percent, and every `D-five` group delivers at least 80
  percent and at least 240 of its first 300 offered units.
- `C-normal`, `C-overload`, `D-single`, and `D-five` are four distinct windows.
  A baseline result may be compared as specified above, but no offered unit,
  time interval or sample is counted in two windows or substituted across C and
  D.
- Every declared userspace queue, socket buffer, qdisc, veth/TAP queue, capture
  buffer, adapter queue and ns-3 queue has a finite bound and raw
  enqueue/dequeue/drop/overflow counters. Missing kernel/capture drop counters,
  disabled accounting, or an unexplained difference between adjacent stages is
  a hidden drop and fails M5/M7.

### Versioned Endpoint, Transaction, and Hash-Domain Schema

M2 introduces `endpoint_transaction_schema=1`; M3 extends the same schema and
may not replace it. The schema and resolved endpoint matrix are tracked,
content-hashed build inputs and contain:

- one row for every UAV, traffic class, and direction, naming the real producer,
  consumer, namespace, interface, address/port or PTY, MAVLink system/component
  ID, DSCP/TOS, queue, capture points, response type, timeout, and minimum
  counts;
- an explicit taxonomy: control downlink is the valid command and its marker;
  control uplink contains the correlated ACK and liveness heartbeat; payload is
  a separately declared valid MAVLink payload/telemetry message family in both
  directions; additional data is a framed non-MAVLink flow. A telemetry frame
  cannot be counted in two classes;
- for each of five UAVs, two distinct UART mappings named `mavlink_control` and
  `mavlink_payload`, with unique PTY master/slave identities, baud, framing,
  finite buffer, pacing, and traffic-class mapping. M6 and M7 H exercise all ten
  mappings, not a single representative PTY;
- a command transaction ID composed from full run nonce, UAV, attempt, marker
  frame hash, command frame hash, MAVLink sequence, source/target/command, and
  bounded send window. The ACK is correlated by request hash, command,
  source/target and time window; heartbeat proves liveness only and never proves
  command success;
- a stream-record identity composed from full run nonce, flow ID, direction,
  monotonic sequence, declared length, and payload hash. Serial receive identity
  is derived after deterministic length/framing reassembly, not read-call
  boundaries;
- separate `mavlink_frame_sha256`, `application_unit_sha256`,
  `transport_payload_sha256`, and captured `wire_frame_sha256` domains. Only the
  applicable inner-domain hash is required to remain identical across a routing
  or L2/L3 rewrite; headers, checksums, VLAN tags, and padding are recorded as
  transformations and are never silently normalized into the same hash;
- an Ethernet test EtherType/protocol and nonce-bearing payload that excludes
  ARP/ND, local echo, and unrelated broadcast frames; and
- for each directed path, resolved capture-point IDs covering at least the real
  source endpoint before its network adapter, ns-3 external ingress, ns-3
  external egress, and the remote endpoint after its adapter. The reverse path
  resolves its own four roles; a physical interface may implement two roles
  only when raw direction and stage identity remain unambiguous.

The point-to-multipoint P0 check sends at least 20 additional-data records from
the command post. Each record is intended for all five UAVs, enters ns-3 once
and is serviced once by the declared shared-medium transmission. Each UAV must
receive `ceil(0.95 * unique_offered)` records. Duplicating five unicasts before
ns-3 does not satisfy point-to-multipoint behavior.

### Complete Topology and Continuous Isolation

M2 freezes a machine-readable topology allowlist before execution. It names
every namespace inode, interface, address, route/rule, neighbor class, bridge,
firewall/NAT rule, socket tuple, process/cgroup, capability, and intended
direction. Separate allowlist sections cover the modeled MAVLink/data plane and
the necessary Gazebo-FDM, ROS/DDS, supervision, capture, and Sionna control
planes. A control-plane route may not accept, proxy, mirror, or forward a
nonce-bearing endpoint application unit.

For nonce-bearing modeled endpoint traffic, the deny set includes IPv4 and IPv6
loopback, host/container default routes, Docker-published ports, direct
SITL/MAVProxy ports, undeclared Unix sockets, forwarding/NAT, undeclared Linux
bridges, and every legacy GCS endpoint. A separately allowlisted control-plane
loopback or default route is not a data-plane bypass, but it fails immediately
if a nonce-bearing application unit appears on it. The validator generates
probes from every reachable GCS address family and verifies the resolved
allow/deny matrix; a configured list without probes is not proof.

Raw netlink route/link/address/neighbor events, firewall-rule events, process
and socket lifecycle events are collected continuously. A complete topology
poll is also recorded at most once per second and at every phase transition.
Any unallowlisted state, missed monitoring interval longer than two seconds,
capture loss that hides a transition, privileged helper without executable
identity, or dataplane change outside the phase manifest fails the run.

Before execution, every resolved observation role is classified as a
`persistent_external_capture` or an `engine_internal_event_source`. Persistent
capture collectors for all four mandatory stage roles and both directions stay
alive across positive, stop-transition, stopped, restart, and recovery windows;
the stopped window may contain offered bytes at external ingress but must
contain no forbidden matching bytes at remote egress. Capture snap length,
timestamp source, checksum/segmentation offload state, packet/drop counters, and
filter are resolved and raw-recorded at every persistent capture point.

The ns-3 internal device/queue event stream must overlap every positive and
recovery probe. For a declared stop it emits a final ordered flush/counter and
lifecycle boundary, terminates at the raw `stopped` event, emits **no**
device/queue events during the expected-stopped interval, and resumes only
after restart under a new monotonic event epoch tied to the new child identity.
The expected absence of that internal stream is validated from lifecycle,
process, persistent-capture, and final/restart counter evidence; it is never
filled by synthetic events or treated as missing positive-window evidence.

### Versioned Asynchronous Sionna Protocol and Expiry Exercise

M4 introduces `sionna_async_schema=1` as a checked discriminated-union JSON
Schema (or an explicitly versioned binary-schema replacement). For TCP JSONL,
every UTF-8 line is one object no larger than 1 MiB; the resolved config may
lower that bound. Every variant has the exact common envelope `schema_version`,
`message_type`, `wire_sequence` strictly increasing per `sender_id` across
reconnects, `sender_id`, `run_id`, `profile`, `phase_id`, `contract_hash`,
`config_hash`, `bundle_id`, `reconnect_generation`, `sender_clock_domain`, and
`emitted_monotonic_ns`. Each `message_type` is a separate `oneOf` branch with
`additionalProperties=false`; an inapplicable field is absent, never fabricated
or filled with a sentinel/null value.

The message variants are:

- `hello` and `ready`: protocol/capability versions, executable/provider
  identity, accepted run/config/bundle IDs, reconnect generation and readiness
  state. They contain no `query_id`, directed link, pose or physical output;
- `query`: `query_id`, complete `node_state_seq`, directed link ID, traffic
  class, source pose timestamp/frame/transform, request-generation/send times,
  all node/jammer poses and freshness, radio/antenna/material assumptions,
  mapping version and deterministic provider seed where applicable;
- `result`: the matching query/state/link/class identity, provider
  receive/start/complete/send timestamps with clock domains, and exactly one
  status. `ok` additionally requires validity-start, expiry and all finite raw
  physical outputs with units. `stale_pose`,
  `scene_mismatch`, `provider_error`, and `deadline_missed` require a bounded
  error body and forbid physical outputs and a valid interval; and
- `error` and `disconnect`: a bounded reason, rejected wire-sequence/request
  hash where available, and lifecycle timestamps. `invalid_request` belongs to
  `error`; `disconnected` belongs to `disconnect`. Neither variant invents a
  query/link/state identity that was unavailable when the error occurred.

Missing required fields, fields forbidden by the selected variant, non-finite
values, wrong-run/wrong-bundle data, partial objects and schema-invalid wire
bytes are rejected. A rejected result or disconnect makes only its identified
directed link unavailable; when no link can be identified, all links owned by
that provider connection become unavailable. Reconnect never resurrects an old
result or silently resets query, state or wire-sequence ordering.

Adapter receive, validation, selection and apply are necessarily later than the
provider wire message and therefore are recorded as separate raw adapter events,
not fields forged into that message. Those events carry the exact result-wire
hash, query/state/link identity, adapter clock domain, receive/apply timestamps,
and decision; only an applied-result event carries its resulting validity
interval.

The request/result log records the exact wire bytes in addition to parsed
fields. The validator independently derives newest-compatible selection,
expiry, and applied packet intervals. Provider name, mode, or a producer field
is not proof of Sionna RT execution; executable/import identity, scene access,
query inputs, physical outputs, and mapped ns-3 events must form one trace.

M4 and M7 phase F contain a mandatory `F-expiry` subphase. A supervised
transport fault injector may delay and duplicate **already computed,
byte-identical, raw-captured real-provider results** but may not synthesize or
modify them. It must:

1. hold one real result beyond expiry and observe fail-closed loss;
2. deliver a newer real result before the held result, then release the old
   result and prove out-of-order rejection;
3. deliver a duplicate and prove duplicate rejection; and
4. remove the fault, apply a new fresh result, and prove recovery.

The Sionna process, endpoints, supervisor, Gazebo, SITLs, and ns-3 remain alive
through this subphase. Thus the exercise validates late policy without using a
mock provider or an undeclared service crash.

### Final Scene, Coordinate, and Range/Service Contract

The canonical bundle first accepted by M4 is the final bundle used unchanged by
M5-M8. It has usable collision and RF bounds containing a genuine 20 km path
and at least a `20 km x 10 km` operating region. Within that region it contains:

- measured terrain relief of at least 150 m and no more than 200 m between the
  declared low/high fixtures;
- at least two settlement/building clusters with low-, medium-, and high-rise
  geometry, including at least one 12-15-storey structure using the resolved
  floor-height convention;
- validated LoS and obstructed routes at 1, 5, 10, and 20 km, including both a
  terrain-shadow and a building-blocked route. It also contains at least one
  continuous low-altitude path of at least 1 km with sampled clearance
  `20 m <= AGL < 50 m` and one continuous medium-altitude path of at least 1 km
  with `50 m <= AGL <= 120 m`. Terrain-derived AGL samples are at most 25 m
  apart, at least 95 percent lie inside the declared band, and no sample is
  below 20 m;
- transitive Gazebo and Sionna asset closure with byte hashes, actual mesh
  bounds, collision/RF surface samples, and at least six distributed landmarks
  spanning both horizontal extremes and three elevations, each within 1 metre;
  and
- identical geometry/config identity for runtime link queries and heatmap raw
  grids. Metadata-only bounds, hidden proxy geometry, or a different heatmap
  scene fails.

If implementation work before M4 used a smaller component scene, it is
diagnostic only. Any change to the accepted M4 bundle invalidates M4 and every
downstream milestone and requires their rerun under the new identity.

M7 I uses the real external additional-data endpoint path, not an internal ns-3
generator or a UART-limited stand-in. A directed range-cell key is exactly
`(geometry, distance, tx_power, direction)`. The eight rows, two powers and two
directions below therefore create 32 required directed cells. Directions are
measured in separate windows; a simultaneous bidirectional diagnostic cannot
replace either directed cell or divide one shared capacity budget between them.

Each directed cell has a 10-second pose/state settling interval, a distinct
10-second full-readiness dwell, and then a measurement interval of at least 30
seconds containing at least 100 unique application units. For every positive
cell, offered external-ingress rate remains between 105 and 110 percent of the
selected tier for at least 90 percent of measurement, achieved goodput is at
least 90 percent of that tier, delivery is at least 85 percent, loss is at most
15 percent, delivered p95 latency is at most 3 seconds, and jitter is finite.
The selected tier meets the row minimum and live per-direction ns-3 capacity is
at least 110 percent of it. These are engineering-surrogate targets, not
customer-modem predictions.

For a `down_allowed` cell, the readiness dwell requires every service,
process, provider, capture and timing predicate to be ready while the cell's
predeclared directed-link predicate is `unavailable`; it never fabricates a
positive-link readiness state.

| Geometry | Distance | Minimum tier at 30 dBm | Minimum tier at 33 dBm |
| --- | ---: | ---: | ---: |
| LoS | 1 km | 2 Mbit/s | 20 Mbit/s |
| LoS | 5 km | 500 Kbit/s | 2 Mbit/s |
| LoS | 10 km | 100 Kbit/s | 500 Kbit/s |
| LoS | 20 km | 10 Kbit/s | 100 Kbit/s |
| Obstructed | 1 km | 100 Kbit/s | 500 Kbit/s |
| Obstructed | 5 km | 10 Kbit/s | 100 Kbit/s |
| Obstructed | 10 km | 1 Kbit/s | 10 Kbit/s |
| Obstructed | 20 km | `down_allowed` | 1 Kbit/s |

The sole `down_allowed` table entry is the obstructed-20-km/30-dBm row-power
pair and creates two directed cells. Each still sends at least 100 new units,
proves loss exactly 1.0 and finite raw-derived timeouts, and has inapplicable
delivered latency/goodput. After each down cell, the runner moves to the paired
LoS-20-km/30-dBm fixture and executes a **new** settling, readiness and
measurement window with new unit identities; that recovery must meet the LoS
row and cannot reuse its earlier matrix window.

The resolved phase schedule contains both transmit powers; changing power is a
declared runtime stimulus, not a config mutation. M4 mapping tests exercise
every boundary and output in the six-tier set `1 Kbit/s`, `10 Kbit/s`,
`100 Kbit/s`, `500 Kbit/s`, `2 Mbit/s`, and `20 Mbit/s`; a missing tier or a
configured tier above live ns-3 capacity fails before M7. Thus the accepted
phase-I schedule contains exactly 32 directed matrix windows and exactly two
additional directed recovery windows, with at least 3,200 matrix units. Their
mandatory settling, readiness and measurement intervals total at least 1,700
seconds, excluding state-transition overhead. Extra diagnostic repetitions are
outside the acceptance schedule and cannot replace or duplicate one of these
34 windows.

### Identity Impact DAG and Final-Runtime Reruns

Every tracked path other than the three exact live status paths is assigned to
exactly one earliest affected node below by a versioned path-ownership map. The
map covers regular files, symlinks, submodule identities, additions, deletions,
renames, generated tracked outputs, and build inputs; ambiguous or unassigned
content is Q0. For each clean commit, deterministic hashing produces the
ordered content-manifest vector `(Q0, Q1, ..., Q8)`, including an explicit empty
manifest for a node with no files. The ownership map, vector algorithm, and all
nine manifest hashes are technical Q0 inputs.

The initial `q0_conservative_bootstrap/v1` ownership policy is an explicitly
allowed fail-closed map: it assigns every non-status tracked Git entry to Q0
and records Q1..Q8 as exact empty manifests. Its entries bind pathname, Git
mode, object type and blob/gitlink identity, not only working-tree file bytes.
It cannot support selective descendant reuse: under this policy every
non-status change invalidates M0 and all downstream evidence. Before the first
capacity-prerequisite candidate or formal M2 candidate, a granular ownership
map must be introduced as a Q0 change, independently checked, and followed by
M0 requalification plus rerun of every affected component. Conservatively
assigning a path to an earlier node is allowed only when the policy declares
that fact and accepts the resulting extra invalidation; assigning it later than
its actual earliest effect is forbidden.

Each evidence profile declares the exact vector nodes it consumes. Its raw
records the full vector and execution Git commit, while acceptance equality is
enforced on every consumed node rather than inferred from a matching branch
name or prose. A changed node and every downstream node return to `in_progress`
when active work exists, or `not_started` otherwise; v3 does not add a `stale`
state.

| Node | Identity closure | Invalidates |
| --- | --- | --- |
| Q0 | contract, evidence/attestation schema, shared validator/provenance code, dependency lock or accepted image/runtime manifests | M0-M8 |
| Q1 | flight launch, world/assets, SITL/Gazebo/ROS configuration and health validator | M1-M8 |
| Q2 | endpoint transaction schema, isolation/topology, packet engine ingress/egress and no-bypass validator | M2-M8 |
| Q3 | five-UAV/class/direction matrix and point-to-multipoint behavior | M3-M8 |
| Q4 | final scene/frames, Sionna protocol/provider, radio mapping and time policy | M4-M8 |
| Q5 | shared-medium capacity, queues, traffic profiles and QoS validator | M5-M8 |
| Q6 | UART/PTY and Ethernet adapters/framing | M6-M8 |
| Q7 | A-I runner, phase manifest, profile sealer or M7 validator | M7-M8 |
| Q8 | soak/aggregate, OCI restoration/rebuild or handoff/bundle validator | M8 |

Reuse across a later clean descendant is allowed only through an independently
generated identity-reuse receipt. It binds the evidence-producing commit, the
descendant commit, cumulative Git diff including rename/submodule records, both
complete vectors, ownership-map hash, relevant configuration/image/runtime
identities, and the evidence profile. It passes only when every changed
non-status path maps strictly downstream of all milestones claimed by that
evidence, every node consumed by the evidence is byte-identical, and no
unassigned, ambiguous, dirty, omitted, or dynamically loaded input exists. A
change in a consumed configuration, dependency, image/runtime manifest, scene,
host requirement, or executable identity is handled by its owning Q node and
cannot be hidden by an unchanged source-file subset.

The three exact status paths are absent from every Q manifest. A post-receipt
status-only descendant instead uses the live-status lint and restricted diff
rule defined at the start of this contract; it needs no technical rerun and may
reference an otherwise unchanged receipt, component result, or seal. Mixing a
status update with any other path removes this exemption. Thus an M0 run may
truthfully execute from a clean commit whose live status says `M0=in_progress`,
then be counted from a clean status-only descendant that says `M0=passed` and
cites its stable host-final receipt.

The source diff, ownership assignment, vectors, and any reuse receipt are
independently checked; an unassigned changed file invalidates Q0. Before the
final M7 candidate, the revision/config/image are frozen, M0 is requalified,
and the M7 validator reruns the complete profile-applicable M1-M6 acceptance
logic from that one runtime rather than trusting old component PASS results.
Each M8 child repeats the same full derivation. Component profiles remain useful
development closures, but no old source-bound component result is carried into
final P0 by prose or impact assertion alone.

The final M7 phase manifest contains one named `Q1_health` envelope beginning
after a 30-second warm-up and lasting at least 300 uninterrupted seconds. All
M1 model/SITL, exact-role, helper-continuity, heartbeat, odometry, pose,
Gazebo-world and no-link-down predicates apply inside it. It contains no M7-B or
M7-H stop, F-down/F-expiry unavailable interval, or I `down_allowed` interval.
It may overlap phase A for health observation, but its traffic units remain
assigned only to A and phase-A counts are not duplicated.

M1 endpoint validation is profile-aware. The M1 component profile requires its
resolved optional SERIAL2 endpoints to be disabled. The final M7 profile instead
requires the ten M6 UART mappings to be enabled and live. `Q1_health` validates
the final profile's hash-bound required/disabled endpoint map and forbids every
attempt to open an endpoint marked disabled; it does not reapply the
M1-component-specific `enable_serial2=false` value. All other M1 health and
identity predicates are unchanged.

### Capacity, Clock, Heatmap, Soak, and Repeatability Rules

Two non-milestone feasibility prerequisites expose timing failure early without
changing the M0-M8 state machine. Each produces a write-once raw capacity tree
and a separate independently derived capacity-validation receipt. A producer
PASS field or an earlier benchmark is not sufficient. Before execution, both
profiles bind:

- execution commit, contract, Q-vector and ownership-map hashes, dependency
  lock, exact image/runtime/executable manifests, resolved configuration, phase
  schedule, scene/assets and validator identity;
- acceptance-host identity including CPU model/topology, online CPU set,
  governor/frequency policy, memory, GPU/driver when used, kernel, container
  runtime, capabilities, resource quotas, competing-load policy and clock
  source;
- exact warm-up/measurement boundaries, workload vector, unit sizes/rates,
  random streams, topology, update period, validity/deadline policy and every
  exclusion; and
- raw Gazebo clocks, per-second RTF windows, process/resource samples and all
  profile-specific query, pose, packet and late-state evidence.

The independent validator re-hashes that identity, rejects missing/extra
workload or time, derives all thresholds from raw observations, and names the
exact identity constraint that a next candidate must match for the receipt to
be eligible:

1. After M1 and before a formal M2 candidate, the intended acceptance host runs
   the five-UAV flight world for exactly 300 post-warm-up measurement seconds.
   Aggregate Gazebo RTF and at least 95 percent of its 300 non-overlapping
   one-second RTF windows must be `0.95 .. 1.05`. The receipt is reusable only
   while Q0/Q1 manifests, flight scene/config, image/runtime, acceptance host,
   resource/quota and competing-load identities remain equal.
2. Before M4 closure, the final six-node/jammer scene runs real Sionna queries at
   the proposed update period for exactly 600 measurement seconds while the
   external ns-3 path carries the frozen non-zero 30-cell vector at least as
   large as the accepted M3 per-cell nominal rates. It must meet the RTF,
   query-age, late-ratio and zero-stale-pose P0 thresholds without increasing
   the already resolved validity deadline. Its receipt is reusable only while
   Q0-Q4 manifests, accepted M3 rate vector, final scene/provider/mapping,
   ns-3 topology/workload, update/validity/deadline policy, image/runtime,
   acceptance host, resource/quota and competing-load identities remain equal.

Any change to a bound field or listed node, any host migration, or any identity
reuse receipt that does not cover the capacity profile invalidates the capacity
receipt and requires the complete prerequisite to rerun before the named formal
candidate. A valid status-only commit does not invalidate it. Failure blocks the
next formal candidate and produces a failed capacity report; it does not turn
an earlier passed milestone into a qualified timing claim.

RTF is `delta Gazebo simulation time / delta host CLOCK_MONOTONIC` over
non-overlapping one-second windows after a 30-second warm-up. Packet-engine
stop/start transition windows are excluded only when predeclared. Clock
correlation applies to the exact timestamp-producer set frozen in the phase
manifest: lifecycle supervisor, endpoint producers and consumers, endpoint
adapters, ns-3, ROS/Gazebo position/clock tracker, Sionna worker, capture
timestamp converters and raw collectors. It does not mean every unrelated OS
process. Each listed producer is sampled nominally once per second with no gap
over 1.5 seconds; affine-fit residual is at most 2 ms, drift at most 100 ppm, and
no unexplained step exceeds 5 ms. A missing producer or larger gap fails time
coherence.

Each heatmap layer has a sealed numeric grid, not only a PNG. The grid is at
least `51 x 51`, records axes/units/bounds/altitude/transmitter/jammer,
run/config/bundle/provider IDs, query and node-state ranges, invalid-cell mask,
and every finite value. Every grid coordinate has one uniquely identified raw
real-provider query outcome from the active M7 runtime; at least 2,601 outcomes
are required. Before queries begin, the phase manifest assigns exactly one
acceptance query identity to each coordinate; a retry or extra diagnostic query
cannot replace that query's outcome or be selected post hoc. At least 95 percent
of coordinates are unmasked and finite in all three physical grids, and the
invalid mask covers at most 5 percent. A provider error cannot be relabeled as
a geometric mask and fails G.

PNGs are deterministically rendered from the grids. The validator selects at
least `max(130, ceil(0.05 * finite_cells))` stratified coordinates spanning all
quadrants, extrema and the jammer/obstruction regions. It rederives raw-provider
RSS, SINR and J/S within 0.25 dB absolute error and requires exact
degradation-zone and service-tier categories. A resolved tolerance may be
tighter but never wider.

Each M8 child contains one named soak after A-I and final recovery. The short
child soak is at least 600 uninterrupted seconds; the long child has one total
soak of at least 1,800 seconds, not 600 plus an additional 1,800. ns-3, Sionna,
Gazebo, all SITLs, collectors and endpoints remain continuously ready, and
B/H-style outages or expected-down link states are forbidden.

The pre-run `soak_rate_vector` has exactly the 30
`UAV x class x direction` cells. For each cell it binds packet size, a schedule
phase offset, an integer `units_per_10s`, and the resulting exact application
unit and byte rates. `units_per_10s` is the greater of the ceiling of 10 percent
of that cell's accepted M7-A nominal ten-second unit count and these floors:

- per UAV, 10 valid control commands per 10 seconds downlink and 10 expected
  unique correlated ACKs per 10 seconds uplink, with a fresh heartbeat also
  observed;
- per UAV, 20 unique payload units per 10 seconds in each direction; and
- per UAV, 20 unique additional-data records per 10 seconds in each direction.

The named soak duration is predeclared as an integral multiple of 60 seconds.
Every half-open 10-second bin contains exactly the cell's resolved
`units_per_10s` offered identities; neither undersending nor oversending may be
hidden by a whole-soak average. Control has no inter-offer gap over 2 seconds;
payload and additional data have no gap over 1 second. In every non-overlapping
60-second bin and over the complete soak, every cell
delivers at least 95 percent, payload/additional achieved goodput is at least 90
percent of offered goodput, control p95 is at most 250 ms, payload/additional
p95 is at most 3 seconds, and applicable jitter is finite. Five transactions
per second means five **offered** commands across the swarm, exactly one per UAV
per second; ACK success is governed by the 95-percent rule.

Every declared userspace queue, socket buffer, qdisc, veth/TAP queue, capture
buffer, adapter queue and ns-3 queue is sampled at least once per second with no
gap over 1.5 seconds. Each queue separately may not remain above 90 percent for
10 consecutive seconds, end more than 10 percent of its capacity above its
start, or have a
least-squares occupancy slope over the final 300 seconds greater than 1 percent
of its own capacity per minute. Aggregate occupancy cannot hide a failing queue.

The cross-child soak comparison uses `[soak_start, soak_start + 600 s)` in each
child. The long child's interval `[soak_start + 600 s, soak_end)` lasts at least
1,200 seconds; it and any duration beyond 1,800 seconds remain stability
evidence subject to every soak gate. Every corresponding mandatory steady-state
window is joined by the exact key `(phase, window, UAV, traffic_class,
direction, endpoint_form, geometry, distance, tx_power)`; a missing key fails
instead of being dropped. For matching applicable metrics, children differ by
no more than 5 percentage points delivery/loss, 10 percent relative goodput
using the lower positive child value as denominator, an absolute p95-latency
difference of `max(20 percent of the lower child p95, 25 ms)`, 2 percentage
points late ratio, and 1 dB median SINR. Expected-zero/inapplicable metrics are
compared by their exact state and are not assigned a fabricated relative value.
The children use the same declared random-stream map but different run IDs and
no shared writable run directory, mutable dependency, or unrecorded cache.

The external ledger produces a signed append-only checkpoint and portable
inclusion proof for every seal. Aggregate and handoff packages include the
public checkpoint, log-key fingerprint, entry hash, tree/chain position and
inclusion/consistency proof, never the private attestation key or writable
ledger. After unrelated-directory extraction, validation works offline from
the pinned public keys and these proofs; copying an unsigned ledger line or
requiring an undocumented development-host path fails M8.

## Milestone State Machine

### M0 — Truthful Validation and Exact Runtime Qualification

Purpose: make false progress impossible and qualify one exact source/runtime
pair without making a packet-path or P0 claim.

Deliverables:

- this v3 contract and, after the technical receipt is stable, consistent
  mutable status records plus their separate post-receipt live-status lint;
- content-aware validators and false-positive regression fixtures;
- provenance/config/dependency schemas;
- exact-image dependency/provenance qualification runner;
- attestation public key, external append-only ledger configuration, and
  adversarial sealer tests;
- complete exact runtime package manifests;
- the frozen committed-input and exact ordered suite-ID manifests, raw initial
  and final image/container inspections, live identity recomputation, and
  before/after safe-read artifact manifests required by the M0 qualification
  boundary; and
- a durable qualification-test record containing the exact suite command and
  discovery pattern, ordered test IDs/outcomes/count, exact image and container
  identity, UTC/monotonic suite envelope, Python executable path/hash, and raw
  log bytes/hash from the exact qualification image, plus the atomic host-final
  receipt that binds them. The current Q0 source manifest binds validator, test,
  inline-fixture, assertion, collection, and runner code.

Acceptance:

- historical false-positive, ARP-only, zero-RX, 100-percent-loss, null-metric,
  summary-boolean, and note-only no-bypass fixtures all fail with explanations;
- the complete Q0 positive and adversarial qualification suite runs inside the
  exact qualified image against the current Q0 source/schema, covers every
  frozen M0 semantic class, consumes exactly the frozen ordered suite IDs,
  exits zero as a suite, and its durable record is independently rederived from
  stable files by the host finalizer; a prose claim, repository-wide preflight,
  captured probe, producer acceptance field, exit-code sidecar, success string,
  or earlier test run cannot substitute for the atomic host-final receipt;
- every suite input is tracked and hash-bound, the candidate checkout is clean,
  source is mounted read-only, writable scratch/cache/artifacts are separate,
  and no host build/install/import/plugin state influences the run;
- initial/final inspection proves exact image, Entrypoint, Cmd, User, Env and
  mounts, while live recomputation proves every required lock, runtime-package
  and acceptance-critical executed-binary identity plus the bounded incidental
  command-resolution policy defined above;
- fd-relative no-follow reads, per-file `fstat`, and matching before/after
  artifact manifests prove the receipt did not accept replaced or changing
  evidence;
- dependency lock identifies the exact usable image ID and matches recomputed
  pip/dpkg/ROS/external manifests;
- provenance recomputes current v3 source/config manifests with no blocker;
- qualification passes dependency and provenance gates while recording
  `p0_eligible=false`;
- mutation, cross-run substitution, signature mismatch, ledger mismatch, and
  producer PASS adversarial tests fail closed; and
- a clean status-only descendant cites the stable host-final receipt, reports
  `M0=passed` consistently in all three live records, and passes the separate
  live-status lint without becoming an input to or mutation of that receipt.

M0 qualifies the inspected image and manifests. It does not claim independent
no-cache rebuildability; that remains an M8 gate.

### M1 — Healthy Five-UAV Flight Runtime

Purpose: prove flight/simulator health independently of networking and Sionna.

Deliverables:

- one command launching the active Gazebo world and five UAVs;
- structured lifecycle/readiness log;
- five unique DDS ports, SITL endpoints, names, and system IDs;
- heartbeat and raw fresh ROS odometry for each UAV;
- active Gazebo world provenance defined by this contract;
- a resolved launch contract that marks every serial/network endpoint required
  or disabled for M1 and forbids attempting a disabled optional endpoint;
- a raw scenario/launch assignment map binding each model, instance, system ID,
  DDS port, SITL endpoint and Gazebo FDM endpoint to the resolved scenario hash
  and observed process command line; and
- exact critical-role cardinalities and raw launch assignments: five
  ArduCopter, five MAVProxy, five micro-ROS agents, and one Gazebo server.
  Launch-owned helpers/relays may be discovered during startup, but their
  executable bytes are provenance-locked and their complete identity set is
  captured at stable readiness and remains unchanged through measurement.

Acceptance:

- five expected models are present in the live Gazebo entity inventory;
- five SITL processes remain healthy concurrently for at least 300 seconds;
- system IDs are exactly `1..5`, DDS ports and endpoints are unique;
- every UAV has heartbeat and continuously fresh odometry;
- the live world/entity inventory correlates with launch/process arguments and
  the resolved world/resource hash manifest;
- process samples with no gap greater than 1.5 seconds prove exact critical-role
  cardinalities `5/5/5/1` and stable complete identities for every critical
  process and the readiness-captured launch-owned helper set throughout stable
  readiness and measurement, using PID plus start ticks, executable path/hash,
  raw command line, namespace/cgroup and parent identity;
- the final qualifying readiness identity set is exactly the first measurement
  identity set, their sample gap is at most 1.5 seconds, and the same baseline is
  used for all later measurement comparisons; a restart in the phase transition
  cannot establish a new accepted baseline;
- no zombie/stopped process, unallowlisted executable, duplicate critical role,
  replaced/incompletely identified process, or helper-set change occurs during
  stable readiness or measurement; and
- pre-readiness logs contain no fatal marker or disabled-endpoint open attempt.
  An observed zombie, duplicate critical role, or unallowlisted executable
  prevents readiness and resets the stable-readiness dwell. After readiness,
  no required-endpoint bind/open error, disabled-endpoint open attempt, crash,
  missing/stale pose, link-down, or undeclared restart occurs.

M1 does not claim Gazebo/Sionna scene alignment. It records real-time factor but
does not waive M4 or M7 timing gates.

### M2 — One-UAV External Packet Vertical Slice

Purpose: establish the only accepted real-byte path and active isolation before
radio complexity or scaling.

Deliverables:

- managed `ams-gcs`, `ams-ns3`, and `ams-uav1` namespace lifecycle;
- external veth/TAP ns-3 ingress and egress with no direct tail;
- real bidirectional GCS-to-SITL MAVLink routing;
- ingress, ns-3 device/queue, and egress raw traces/PCAP;
- live topology/current provenance snapshots from this contract;
- automated good/down/recovery transaction runner;
- `endpoint_transaction_schema=1`, resolved hash-domain/timeout matrix, and
  continuously monitored topology allow/deny contract from the normative
  addendum; and
- a hash-bound write-once component raw package and independent M2 validation
  result under the component-evidence rule above.

Acceptance:

- with ns-3 ready, 10 of 10 nonce-tagged valid commands receive expected
  heartbeat and `COMMAND_ACK`/telemetry;
- the correct inner-domain frame/application hashes from
  `endpoint_transaction_schema=1` are identical at GCS ingress, ns-3 ingress,
  ns-3 egress, UAV egress, and separately along the returning path;
- with endpoints and supervisor still live and ns-3 deliberately stopped, 0 of
  5 new commands receive an ACK and heartbeat times out;
- after a supervised ns-3 restart, 10 of 10 new transactions recover;
- direct SITL ports and every forbidden route are unreachable from `ams-gcs`;
- continuous active namespace/ns-3 topology evidence and transition snapshots
  prove the single allowlisted external route;
- TX/RX/loss, adapter latency, ns-3 latency, end-to-end latency, jitter,
  outcome timeouts, and denominators are independently derived from raw
  timestamped events and satisfy the phase-applicability table; and
- provenance proves the exact current executable/config/image/source identity.

No internal ns-3 application or synthesized packet may satisfy an M2 endpoint
or metric gate.

### M3 — Five-UAV External Path and Three Traffic Classes

Purpose: scale the exact M2 route without introducing a second path.

Deliverables:

- five isolated UAV endpoint namespaces extending the M2 topology;
- complete five-by-three-by-two endpoint matrix;
- real bidirectional MAVLink control/payload and nonce-tagged additional data;
- decoded class-aware endpoint/PCAP/ns-3 counters; and
- topology-diff proof showing only the declared M2-to-five-UAV extension.

Acceptance:

- every matrix cell meets the raw-derived minimum TX/RX and delivery ratio in
  the normative phase-applicability table;
- byte/frame identity is correlated from external ingress through ns-3-owned
  devices/queues to external egress for every class and UAV;
- MAVLink system IDs reach only the intended UAV;
- the declared M3 stopped window sends the per-cell minimum and stopping the
  same ns-3 packet-engine child produces zero remote delivery in all 30 cells;
- forbidden direct paths carry zero matching packets and are unreachable;
- labels are derived from decoded bytes, ports, and nonce/flow identities, not
  filenames;
- no accepted traffic, loss, delay, or goodput measurement originates from an
  internal ns-3 traffic generator, replay, or alternate adapter path; and
- the command-post additional-data point-to-multipoint test enters ns-3 once
  and meets the per-UAV delivery rule in the normative addendum.

M3 must import and extend the external M2 path implementation; a parallel
five-UAV packet core cannot pass.

### M4 — Canonical Scene and Online Sionna Causality

Purpose: apply asynchronous, current, per-link physical state to the proven M3
real-byte path.

Deliverables:

- final kilometre-scale paired Gazebo/Sionna scene bundle and conversion
  fixture, already satisfying the M7 product-scene and range geometry contract;
- asynchronous `sionna_async_schema=1` request/result worker;
- latest-nonexpired per-directed-link ns-3 adapter;
- explicit fail-closed late/expiry state machine;
- separate terrain-shadow and building-blocked `good/down/recovery` scenarios;
- jammer `off/on/off` scenario; and
- raw timing, freshness, discard, application, and packet-correlation traces.

Acceptance:

- canonical bundle hashes, transforms, asset/mesh bounds, and six distributed
  landmarks
  pass machine validation within 1 metre;
- raw logs derive `node_state_seq -> query_id -> applied_state_id -> exact
  packet/time interval` for every accepted causal sample;
- results are asynchronous and ns-3 never blocks on Sionna;
- each direction/link consumes only its latest non-expired compatible state;
- expired, superseded, duplicate, and out-of-order responses are not applied;
- the explicit fail-closed expiry behavior is observed and validated;
- mandatory `F-expiry` uses delayed, reordered and duplicated results from the
  real provider and passes every transition in the normative addendum;
- both terrain-shadow and building-blocked sequences meet the target-link
  physical/packet deltas and exact down/recovery outcomes while the designated
  control UAV remains within the normative locality tolerance;
- jammer on changes SINR/J/S and real endpoint delivery; jammer off restores
  both;
- the same external endpoint transaction succeeds, degrades/fails, and
  recovers with modeled state;
- no accepted query uses mock/test/free-space/cached-only mode; and
- all time/freshness thresholds pass.

### M5 — Real-Byte Shared Medium and Priority

Purpose: prove contention and QoS in ns-3 using only external endpoint traffic.

Deliverables:

- named real-endpoint `C-normal` and `C-overload` priority windows;
- named real-endpoint `D-single` and `D-five` contention windows;
- `C-overload` at least twice resolved modeled capacity;
- packet-identity-correlated ns-3 device and queue trace with enqueue, dequeue,
  service, drop, class, directed link, queue depth, and reason; and
- independently derived delay, loss, jitter, goodput, arbitration, and queue
  metrics by class.

Acceptance:

- captured endpoint bytes are the same bytes represented in ns-3-owned queue
  and device events;
- `D-five` meets the normative queue/delay/goodput effect threshold relative to
  the matching reference group in `D-single`;
- `C-normal` and `C-overload` meet their fixed rate, count and temporal-bin
  rules, and overload is at least twice live resolved capacity;
- control p95 is at most 250 ms and control loss at most 5 percent;
- both payload and additional data degrade before control by the minimum
  quantitative deltas in the normative addendum;
- priority is attributable to ns-3 queue/service behavior, not adapter or
  pre-ns-3 Python queueing; and
- all configured queues remain bounded with no hidden drops.

Internal ns-3 application traffic may be used in unit tests but is ineligible
for M5 acceptance.

### M6 — True PTY and Ethernet HitL Paths

Purpose: validate future endpoint forms through the same M2-M5 external route
without claiming physical RF hardware.

Deliverables:

- all ten declared real PTY master/slave serial mappings (control and payload
  for each UAV) with measured byte pacing and finite buffer/backpressure;
- an Ethernet endpoint adapter carrying actual frames;
- both endpoint forms attached to the unchanged external ns-3 path and online M4
  Sionna mapping; and
- stage-separated byte/frame identity and timing traces.

Acceptance:

- bytes written to every declared PTY slave are observed at its PTY master,
  external ns-3 ingress, ns-3-owned queue/device trace, remote egress, and
  response path;
- actual Ethernet frames traverse external ingress, ns-3 queue/channel, remote
  egress, and response path;
- no UDP-only stand-in, in-process callback, direct bridge, local echo, or
  replay may satisfy either endpoint form;
- stopping ns-3 with endpoints live breaks every one of the ten UART mappings
  and both Ethernet directions at the per-mapping minimum; restart restores all
  of them;
- both forms use current online Sionna per-link state; and
- UART pacing/adapter, ns-3 queue/channel, Sionna-state age, and end-to-end
  delays are independently derived and non-null.

### M7 — Executable Integrated Scenario Matrix

Purpose: execute every P0 product behavior in one sealed, supervised
`m7_single_run_candidate` without hiding planned packet-engine lifecycle.

Deliverables:

- one non-interactive scenario runner with a resolved phase manifest;
- lifecycle supervisor and stoppable/restartable ns-3 child state machine;
- the named uninterrupted `Q1_health` envelope and final profile-aware endpoint
  map defined by the normative addendum;
- executable A-I phase definitions with preconditions, stimuli, time windows,
  success observations, and required raw artifacts;
- one final kilometre-scale canonical paired scene bundle, active for all A-I;
- five provider-generated heatmap layers plus their sealed numeric grids bound
  to the active bundle/config;
- a profile-specific raw seal/signature/ledger record; and
- independent post-seal M7 validator.

The runner must execute these phases in one `run_id`:

- **A — baseline:** five-UAV bidirectional three-class connectivity, true
  additional-data point-to-multipoint delivery, and health.
- **B — no bypass:** predeclared supervised ns-3 stop, 0-of-5 failure while
  endpoints remain live, restart, and 10-of-10 recovery.
- **C — congestion/priority:** disjoint `C-normal` and `C-overload` windows with
  the fixed rate/count/bin contract, overload at least twice capacity, and
  control thresholds preserved.
- **D — contention:** disjoint `D-single` and `D-five` windows with the same
  reference flow, four added homologous flows and ns-3 queue/arbitration change.
- **E — jammer:** jammer `off/on/off` with physical and packet degradation and
  recovery.
- **F — mobility/obstruction and expiry:** terrain-shadow and building-blocked
  target-link `good/down/recovery` sequences with control-link locality,
  followed by the mandatory real-result `F-expiry`
  delay/reorder/duplicate/recovery subphase.
- **G — heatmaps:** five declared layers generated online from at least 2,601
  raw real-provider grid queries using the same active provider, canonical
  bundle, and resolved config.
- **H — HitL:** all ten control/payload PTY mappings and Ethernet loopbacks
  through ns-3 and online Sionna, including the declared stop/recovery proof.
- **I — range/service:** 32 sequential directed LoS/obstruction cells at 1, 5,
  10, and 20 km and 30/33 dBm, plus the two new down-to-LoS recovery windows,
  meeting every dwell, traffic, loss, latency, selected-tier and goodput rule.

All phases A-I, not only phase I, use the same validated kilometre-scale bundle
with actual 20 km usable geometry. The existing approximately `200 x 150 m`
engineering terrain and any M1 local-world qualification are ineligible for
M7. A 50 km point is P1 ideal-case only and never substitutes for a required
P0 point.

Acceptance:

- the named `Q1_health` envelope and A-I all execute and pass from the same
  sealed run/source/config/image;
- lifecycle supervisor, endpoints, Gazebo, SITLs, Sionna worker, and collectors
  remain continuously alive; one Gazebo world, one Sionna scene, and one
  canonical bundle ID remain unchanged; ns-3 is absent only inside declared
  B/H stop windows and every lifecycle transition matches the phase manifest;
- all causal metrics are independently derived from profile raw evidence;
- every applicability, minimum-count, dwell, effect-size, priority, expiry,
  point-to-multipoint, UART, range/service, heatmap, clock and RTF rule in the
  normative addendum passes without a phase-local waiver;
- phase H and every phase-I point are P0-pass, not skipped, informational, or
  P1;
- five heatmaps record run ID, bundle/config hashes, provider mode, query/state
  ranges, and are consistent with raw link results;
- timing, freshness, no-bypass, current-state, and process-health gates pass;
- sealing occurs only after raw closure and validation occurs only after the
  raw signature and ledger entry; and
- artifact generation hides no failure and changes no sealed raw file.

M7 closes one-run product behavior only. It deliberately makes no two-run
repeatability, rebuild, or handoff claim.

### M8 — Repeatability, Rebuild, Soak, and Customer Handoff

Purpose: reproduce the final P0 candidate independently, prove runtime
restoration/build equivalence, and ship a self-validating bundle.

Deliverables:

- two independent clean-clone `m7_single_run_candidate` children at the same
  final identities;
- a post-A-I uninterrupted soak of at least 600 seconds in each child, with one
  child's **total** soak at least 1,800 seconds, using the exact traffic, queue
  and service conditions from the normative addendum;
- signed/ledgered `m8_repeatability_aggregate` with child comparison;
- accepted image distributed by registry digest or verified OCI archive hash,
  plus offline/clean-host restoration instructions;
- snapshot-pinned mutable package indices, exact package versions, source
  commits, and checksummed downloads sufficient for a fresh build;
- clean no-cache rebuild proof with manifest-equivalent runtime; and
- signed/ledgered `m8_customer_handoff` containing operator instructions,
  limitations, replayable sealed evidence, validators, and expected results.

Acceptance:

- both clean clones execute full A-I and independently pass every M7/P0 gate;
- both have at least 600 seconds of uninterrupted integrated soak with no
  crash, timeout, stale pose, undeclared restart, stale/expired-state violation,
  unbounded queue growth, or hidden packet-engine outage;
- at least one child contains one valid soak of at least 1,800 seconds, whose
  first 600 seconds form the cross-child comparison slice and whose subsequent
  interval of at least 1,200 seconds remains subject to the same soak gates;
- aggregate validation re-hashes both complete child raw trees, verifies each
  raw seal/signature/ledger inclusion, reruns both child validators, and
  confirms exact revision/contract/dependency/config/scene/image equality and
  all normative cross-child metric tolerances;
- a clean environment restores the accepted image solely by content identity
  and verifies image ID, runtime manifests, external revisions, and capability
  tests without a mutable tag;
- a fresh clean-clone `--no-cache` build uses recorded snapshot identities and
  checksums, performs no unpinned mutable dependency resolution, and reproduces
  the locked pip/dpkg/ROS manifests, external revisions, ABI/import capability
  checks, and runtime behavior required by M0;
- manifest equivalence is computed by declared canonicalization rules; an
  unrecorded exclusion or version drift fails. Bit-for-bit layer equality is
  not substituted for manifest checks, and cached rebuilds are ineligible;
- the customer bundle contains no absolute workspace dependency, secret key,
  mutable tag, symlink escape, or untracked integration source;
- after extraction in an unrelated clean directory, the bundle validator
  verifies its own seal/signature/ledger record, the aggregate, both child raw
  packages, all P0 results, image restoration metadata, rebuild proof, and
  operator steps using the portable signed ledger checkpoint and inclusion
  proofs without a development-workspace path; and
- a documented operator can run validation without oral context or access to
  the development workspace.

Customer-ready becomes true only after the `m8_customer_handoff` validator
passes. An M7 seal, two PASS summaries, an image tag, or a cached rebuild cannot
substitute for M8.

## P0 Gate Matrix

| Gate | Required causal proof |
| --- | --- |
| Provenance | Exact v3 source, contract, config, dependency, image, scene, host, process, and run identities. |
| Raw integrity | Profile-specific raw seal, signature, external ledger inclusion, and post-seal independent validation. |
| Joint runtime | Supervisor and required services overlap; only declared ns-3 B/H lifecycle intervals differ. |
| Five-UAV health | Named >=300 s `Q1_health`; five models/SITLs, heartbeats, fresh odometry, IDs/ports, exact cross-readiness/measurement critical-role identities/cardinalities, and no zombie, unallowlisted executable, or undeclared restart. |
| Active topology | Raw live namespace, route, socket, ns-3 node/device/queue, and executable provenance proves one route. |
| Packet provenance | Matching real endpoint bytes cross ingress, ns-3 queue/device, egress, and response. |
| No bypass | On succeeds, ns-3 stopped fails with live endpoints, restart recovers. |
| Three classes | Decoded/nonced control, payload, and additional data meet the bidirectional minimums for all UAVs; one-ingress point-to-multipoint delivery reaches all five UAVs. |
| Current online Sionna | Current Gazebo state produces asynchronous versioned real-provider link state. |
| Per-link causality | Latest non-expired directed state correlates with exact real packet outcomes. |
| Late policy | The real-result expiry/delay/reorder/duplicate/recovery exercise rejects invalid state and proves fail-closed behavior. |
| Scene alignment | Final kilometre-scale paired bundle, transitive assets, terrain/building envelope, numeric low/medium AGL paths, transforms, mesh bounds/samples, and six distributed landmarks within 1 metre. |
| Shared medium | Real external single/concurrent bytes show ns-3-owned contention/queue effects. |
| Priority | `C-normal/C-overload` rate/count/bin rules pass; at >=2x live bottleneck capacity, control p95 <=250 ms and loss <=5%, and both payload and additional data degrade by the normative minimum. |
| Jamming | Off/on/off changes and restores SINR/J/S and packet results. |
| HitL | All ten control/payload PTY mappings and true Ethernet frames traverse and depend on the same path. |
| Range/service | All 32 directed LoS/obstruction, 1/5/10/20 km and 30/33 dBm cells plus two recovery windows meet the normative dwell, offered-rate, delivery/loss, latency, tier and goodput matrix. |
| Time coherence | Defined RTF windows, state age, late ratio, clock residual/drift, stale-pose, and packet intervals pass. |
| Heatmaps | Five sealed numeric grids and derived layers meet real-query coverage, mask and numeric-tolerance rules and share run/config/bundle/provider identity with packet evidence. |
| Soak | Each child passes the exact 30-cell >=600 s rate/bin/delivery/queue rules; one child's total soak is >=1,800 s without a prohibited failure. |
| Repeatability | Two independent clean-clone children pass signatures/ledger revalidation and the normative cross-child metric tolerances. |
| Runtime restoration | Accepted OCI artifact restored and verified by content identity. |
| Fresh rebuild | Snapshot-pinned no-cache build reproduces canonical runtime manifests and capabilities. |
| Handoff | Extracted self-contained bundle recursively validates every child and instruction. |

Every row blocks customer-ready status.

## Validation Implementation Requirements

The v3 validator suite must:

- select a schema by declared profile and reject missing/extra profile-critical
  artifacts;
- validate the Q0-Q8 ownership map/content vector, consumed-node declarations,
  descendant diff and every identity-reuse receipt; reject an ambiguous path or
  a report-only claim whose diff contains anything beyond the three exact
  status paths;
- for M0, accept only the atomic host-final receipt after verifying the
  read-only committed-source boundary, separate writable paths, controlled
  Python launch mode and import trace, build/cache state, Q0-scoped frozen
  ordered suite/input manifests, absence of live-status inputs, exact
  initial/final container inspection, live identity recomputation, fd-relative
  no-follow artifact reads and identical before/after manifests; ignore a
  container-captured acceptance claim;
- verify raw manifest closure, hashes, signature key fingerprint, and external
  ledger inclusion before computing gates;
- parse/decode PCAP and raw endpoint/ns-3 device/queue events by byte/frame
  identity;
- validate `endpoint_transaction_schema=1`, its distinct hash domains, serial
  stream reassembly, telemetry class assignment, and point-to-multipoint
  one-ingress/five-egress identity;
- reject ARP-only, empty, filename-only, internally generated, replayed, or
  wrong-run traffic;
- derive loss denominators, latency/jitter/goodput/queue percentiles, clock
  correlation, and warm-up windows independently;
- prove acceptance intervals are disjoint half-open monotonic windows, every
  unicast unit has exactly one phase/window/cell assignment, and every P2MP root
  has one ingress/service identity plus exactly five unique receiver-leg
  assignments;
- enforce the phase-applicability table, including expected zero-delivery
  windows and explicit inapplicable delivered-latency fields, without weakening
  positive-cell minimums;
- validate the continuously monitored namespace/ns-3 allow/deny topology,
  control-plane separation, all address families, every resolved capture-stage
  role, forbidden-path probes, persistent external captures across stop, and
  the required termination/absence/new-epoch sequence of internal ns-3 events;
- decode MAVLink heartbeat, nonce marker, valid command,
  `COMMAND_ACK`/telemetry, source, and target;
- verify supervisor/service readiness and declared ns-3 lifecycle against raw
  timestamps and process IDs, including stopped-window sequencing and the
  cross-readiness/measurement `Q1_health` identity baseline;
- independently validate each capacity prerequisite from its closed raw tree,
  complete source/image/host/config/workload binding, exact duration and
  per-second windows, and reject reuse after any declared invalidation edge;
- recompute Sionna current-state, latest-nonexpired per-link selection,
  late/discard/fail-closed behavior, and packet correlation;
- validate the discriminated `sionna_async_schema=1` `oneOf` variants, raw wire
  messages, separate adapter receive/apply events, error/reconnect behavior, the
  real-result expiry/reorder/duplicate exercise, and every normative
  quantitative causal threshold;
- recompute A/B causal comparisons and all A-I gates;
- validate C/D flow-group pairing/count/bin rules, exact F outcomes, final-scene
  product geometry and numeric AGL, all 32 directed range cells plus recovery,
  numeric heatmap query/mask/tolerance rules, RTF/clock producer bounds, exact
  soak traffic/per-queue rules, and cross-child comparison keys/tolerances;
- ignore producer summary booleans when deciding acceptance;
- recursively revalidate repeatability children and customer-bundle contents;
  and
- exit non-zero with every blocker whenever any required proof is absent or
  inconsistent.

Artifact collection is read-only with respect to raw results. It may package
sealed content but may not repair, relabel, fabricate, replace, or backfill it.

## Dependency, OCI, and Rebuild Contract

The final lock records:

- ArduPilot and every external source revision;
- ROS distribution and exact package manifests;
- Gazebo, ns-3 commit/modules, Sionna RT, Mitsuba, Python, NumPy, driver/GPU,
  compiler, libc, and kernel/capability requirements;
- exact project image ID plus registry digest or OCI archive hash;
- snapshot identity/date/URL and signature/checksum trust roots for APT and
  other mutable indices;
- exact rosdep data snapshot and resolution output;
- exact versions and hashes for downloaded installers, archives, models, and
  build inputs; and
- canonicalization/exclusion rules used to compare pip/dpkg/ROS/runtime
  manifests.

Branches, tags, `latest`, live package indices, and unchecksummed downloads are
not pins. Restoration must work by content identity. The independent no-cache
build may produce different non-runtime OCI metadata only where declared, but
its canonical runtime manifests, external revisions, ABI/import capabilities,
and qualification behavior must match exactly.

## Executable Roadmap

Milestones, not elapsed days, control progress. The critical path is:

1. Adopt v3, rerun the exact-image/source M0 qualification, and synchronize
   status records.
2. Run the 300-second M1 flight-health command and validate active Gazebo-world
   provenance.
3. Pass the flight-only capacity prerequisite, then build and accept one-UAV M2
   external real-byte/no-bypass path with raw-derived metrics and continuously
   monitored active topology.
4. Extend only that path to the M3 five-UAV/class/direction matrix and its
   component stopped-window proof.
5. Introduce the final kilometre-scale M4 canonical bundle and asynchronous
   versioned per-link Sionna worker, run the full six-node/provider capacity
   prerequisite with the non-zero matrix load, then execute the deterministic
   causal and expiry exercises required for M4 closure.
6. Drive real endpoint contention/overload through ns-3-owned M5 queues.
7. Attach true PTY and Ethernet endpoints to that unchanged path for M6.
8. Freeze identities, requalify M0, implement one non-interactive runner with
   `Q1_health` and A-I, rederive all profile-applicable M1-M6 gates under that
   identity, and close one sealed `m7_single_run_candidate`.
9. Freeze the final revision/config/lock, execute two clean-clone candidates,
   verify soak/stability, and seal the repeatability aggregate.
10. Restore the content-addressed image in a clean environment, perform the
    snapshot-pinned no-cache manifest-equivalent rebuild, assemble the handoff,
    and validate it after unrelated-directory extraction.

For each step the next-task record must name the exact command, run ID/profile,
expected raw directory, timeout, and validator command. Work on dashboards,
video, advanced antennas, alternate packet cores, physical modem hardware, or
50 km P1 experiments cannot preempt the open critical-path milestone.

## Runtime Status Records

This contract intentionally contains no mutable milestone ledger. Operators
and validators use the three status files named above. They must record the v3
contract hash, evidence-producing execution and technical-base commits,
Q-vector/reuse-receipt identity where applicable, exact current milestone,
longest fully closed prefix, accepted run/profile identities, and next
executable command. Their separate post-receipt live-status lint derives and
records the current clean report commit outside those files and must pass on
that commit. Only milestones passed without caveat under this contract are
counted.
