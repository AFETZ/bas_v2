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
milestone is counted. Report-only updates do not change a sealed run. A change
to implementation, configuration, validation logic, evidence schemas, or this
contract creates a new source/config identity and requires new affected
evidence.

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
- PTY and Ethernet HitL loopback and the 20 km range point are P0 gates, not
  optional demonstrations.
- Scenario phases A-I, the integrated soak, repeatability, and extracted bundle
  validation are required for customer-ready status.

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
- Git commit, dirty state, diff hash, tracked source manifest, and resolved
  configuration hashes;
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

### Acceptance Profiles

`m7_single_run_candidate`

- Contains exactly one continuously supervised runtime with executable phases
  A-I and one source/config/image identity.
- Has one profile-specific raw manifest, seal, signature, and ledger entry.
- Is independently validated only after sealing.
- May close M7 by itself when all A-I gates pass. It makes no repeatability or
  customer-handoff claim.

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
P0 seal. M1-M6 component evidence may be sealed with a component schema for
milestone closure, but it cannot be relabeled as one of the three profiles
above. The final P0 result is always derived from sealed v3 profile evidence.

### Fail-Closed Conditions

Integrated P0 is false if any of the following occurs:

- a required traffic matrix cell has zero transmitted or received packets;
- a required latency, loss, jitter, goodput, queue, freshness, or timing field
  is null or cannot be derived from raw events;
- a required capture has no decoded matching data bytes;
- heartbeat and the expected `COMMAND_ACK`/telemetry do not return through the
  modeled path;
- a required service crashes, exits early, binds incorrectly, or never becomes
  ready, except for the declared ns-3 stop intervals in phases B and H;
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
  +-- ns-3 packet-engine child (stoppable only in declared B/H intervals)
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
worker, clocks, and collectors persist through all declared phases. The ns-3
packet engine is a supervised child with explicit lifecycle states. It may stop
only during no-bypass intervals declared before execution: phase B and the M7
phase-H adapter check. Endpoints remain live during those intervals. Any other
packet-engine outage, supervisor restart, or hidden endpoint restart fails the
candidate.

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
- at least three non-collinear landmarks matching within 1 metre;
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
- Every Sionna request/result has `query_id`, `node_state_seq`, generation,
  start, completion, validity interval, expiry, provider mode, bundle ID, and
  per-direction link values.
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
remain in loss/latency denominators.

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

## Milestone State Machine

### M0 — Truthful Validation and Exact Runtime Qualification

Purpose: make false progress impossible and qualify one exact source/runtime
pair without making a packet-path or P0 claim.

Deliverables:

- this v3 contract and consistent mutable status records;
- content-aware validators and false-positive regression fixtures;
- provenance/config/dependency schemas;
- exact-image dependency/provenance qualification runner;
- attestation public key, external append-only ledger configuration, and
  adversarial sealer tests; and
- complete exact runtime package manifests.

Acceptance:

- historical false-positive, ARP-only, zero-RX, 100-percent-loss, null-metric,
  summary-boolean, and note-only no-bypass fixtures all fail with explanations;
- current implementation/config/schema/validation files are tracked and the
  candidate checkout is clean;
- dependency lock identifies the exact usable image ID and matches recomputed
  pip/dpkg/ROS/external manifests;
- provenance recomputes current v3 source/config manifests with no blocker;
- qualification passes dependency and provenance gates while recording
  `p0_eligible=false`; and
- mutation, cross-run substitution, signature mismatch, ledger mismatch, and
  producer PASS adversarial tests fail closed.

M0 qualifies the inspected image and manifests. It does not claim independent
no-cache rebuildability; that remains an M8 gate.

### M1 — Healthy Five-UAV Flight Runtime

Purpose: prove flight/simulator health independently of networking and Sionna.

Deliverables:

- one command launching the active Gazebo world and five UAVs;
- structured lifecycle/readiness log;
- five unique DDS ports, SITL endpoints, names, and system IDs;
- heartbeat and raw fresh ROS odometry for each UAV; and
- active Gazebo world provenance defined by this contract.

Acceptance:

- five expected models are present in the live Gazebo entity inventory;
- five SITL processes remain healthy concurrently for at least 300 seconds;
- system IDs are exactly `1..5`, DDS ports and endpoints are unique;
- every UAV has heartbeat and continuously fresh odometry;
- the live world/entity inventory correlates with launch/process arguments and
  the resolved world/resource hash manifest; and
- no bind error, crash, link-down, missing pose, stale pose, or undeclared
  process restart occurs.

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
- live topology/current provenance snapshots from this contract; and
- automated good/down/recovery transaction runner.

Acceptance:

- with ns-3 ready, 10 of 10 nonce-tagged valid commands receive expected
  heartbeat and `COMMAND_ACK`/telemetry;
- identical packet/frame hashes are derived at GCS ingress, ns-3 ingress,
  ns-3 egress, UAV egress, and the returning path;
- with endpoints and supervisor still live and ns-3 deliberately stopped, 0 of
  5 new commands receive an ACK and heartbeat times out;
- after a supervised ns-3 restart, 10 of 10 new transactions recover;
- direct SITL ports and every forbidden route are unreachable from `ams-gcs`;
- active namespace and ns-3 topology snapshots prove a single external route;
- TX/RX/loss, adapter latency, ns-3 latency, end-to-end latency, jitter, and
  denominators are independently derived from raw timestamped events and are
  non-null; and
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

- every matrix cell has non-zero raw-derived TX and RX;
- byte/frame identity is correlated from external ingress through ns-3-owned
  devices/queues to external egress for every class and UAV;
- MAVLink system IDs reach only the intended UAV;
- stopping the same ns-3 packet-engine child disconnects all five UAVs;
- forbidden direct paths carry zero matching packets and are unreachable;
- labels are derived from decoded bytes, ports, and nonce/flow identities, not
  filenames; and
- no accepted traffic, loss, delay, or goodput measurement originates from an
  internal ns-3 traffic generator, replay, or alternate adapter path.

M3 must import and extend the external M2 path implementation; a parallel
five-UAV packet core cannot pass.

### M4 — Canonical Scene and Online Sionna Causality

Purpose: apply asynchronous, current, per-link physical state to the proven M3
real-byte path.

Deliverables:

- canonical paired Gazebo/Sionna scene bundle and conversion fixture;
- asynchronous versioned Sionna request/result worker;
- latest-nonexpired per-directed-link ns-3 adapter;
- explicit fail-closed late/expiry state machine;
- good/down/recovery mobility/obstruction scenario;
- jammer `off/on/off` scenario; and
- raw timing, freshness, discard, application, and packet-correlation traces.

Acceptance:

- canonical bundle hashes, transforms, bounds, and at least three landmarks
  pass machine validation within 1 metre;
- raw logs derive `node_state_seq -> query_id -> applied_state_id -> exact
  packet/time interval` for every accepted causal sample;
- results are asynchronous and ns-3 never blocks on Sionna;
- each direction/link consumes only its latest non-expired compatible state;
- expired, superseded, duplicate, and out-of-order responses are not applied;
- the explicit fail-closed expiry behavior is observed and validated;
- moving/obstructing one UAV changes its own link and packet outcome while a
  designated control UAV remains within a predeclared raw-derived tolerance;
- jammer on changes SINR/J/S and real endpoint delivery; jammer off restores
  both;
- the same external endpoint transaction succeeds, degrades/fails, and
  recovers with modeled state;
- no accepted query uses mock/test/free-space/cached-only mode; and
- all time/freshness thresholds pass.

### M5 — Real-Byte Shared Medium and Priority

Purpose: prove contention and QoS in ns-3 using only external endpoint traffic.

Deliverables:

- real-endpoint single-flow baseline;
- real-endpoint five-UAV concurrent run;
- real-endpoint overload at at least twice resolved modeled capacity;
- packet-identity-correlated ns-3 device and queue trace with enqueue, dequeue,
  service, drop, class, directed link, queue depth, and reason; and
- independently derived delay, loss, jitter, goodput, arbitration, and queue
  metrics by class.

Acceptance:

- captured endpoint bytes are the same bytes represented in ns-3-owned queue
  and device events;
- concurrent load measurably changes queue/delay/goodput from single-flow raw
  evidence;
- offered load during overload is at least twice live resolved capacity;
- control p95 is at most 250 ms and control loss at most 5 percent;
- payload or additional data degrades before control;
- priority is attributable to ns-3 queue/service behavior, not adapter or
  pre-ns-3 Python queueing; and
- all configured queues remain bounded with no hidden drops.

Internal ns-3 application traffic may be used in unit tests but is ineligible
for M5 acceptance.

### M6 — True PTY and Ethernet HitL Paths

Purpose: validate future endpoint forms through the same M2-M5 external route
without claiming physical RF hardware.

Deliverables:

- a real PTY master/slave serial adapter with measured byte pacing and finite
  buffer/backpressure;
- an Ethernet endpoint adapter carrying actual frames;
- both adapters attached to the unchanged external ns-3 path and online M4
  Sionna mapping; and
- stage-separated byte/frame identity and timing traces.

Acceptance:

- bytes written to the PTY slave are observed at the PTY master, external ns-3
  ingress, ns-3-owned queue/device trace, remote egress, and response path;
- actual Ethernet frames traverse external ingress, ns-3 queue/channel, remote
  egress, and response path;
- no UDP-only stand-in, in-process callback, direct bridge, local echo, or
  replay may satisfy either endpoint form;
- stopping ns-3 with endpoints live breaks both loopbacks and restart restores
  both;
- both forms use current online Sionna per-link state; and
- UART pacing/adapter, ns-3 queue/channel, Sionna-state age, and end-to-end
  delays are independently derived and non-null.

### M7 — Executable Integrated Scenario Matrix

Purpose: execute every P0 product behavior in one sealed, supervised
`m7_single_run_candidate` without hiding planned packet-engine lifecycle.

Deliverables:

- one non-interactive scenario runner with a resolved phase manifest;
- lifecycle supervisor and stoppable/restartable ns-3 child state machine;
- executable A-I phase definitions with preconditions, stimuli, time windows,
  success observations, and required raw artifacts;
- one final kilometre-scale canonical paired scene bundle, active for all A-I;
- five provider-generated heatmap layers bound to the active bundle/config;
- a profile-specific raw seal/signature/ledger record; and
- independent post-seal M7 validator.

The runner must execute these phases in one `run_id`:

- **A — baseline:** five-UAV bidirectional three-class connectivity and health.
- **B — no bypass:** predeclared supervised ns-3 stop, 0-of-5 failure while
  endpoints remain live, restart, and 10-of-10 recovery.
- **C — congestion/priority:** real external offered load at least twice
  capacity with control thresholds preserved.
- **D — contention:** single-flow reference followed by five concurrent real
  endpoint flows and ns-3 queue/arbitration change.
- **E — jammer:** jammer `off/on/off` with physical and packet degradation and
  recovery.
- **F — mobility/obstruction:** target-link good/down/recovery with control-link
  locality.
- **G — heatmaps:** five declared layers generated online from the same active
  provider, canonical bundle, and resolved config.
- **H — HitL:** true PTY and Ethernet loopbacks through ns-3 and online Sionna,
  including the declared stop/recovery proof.
- **I — range/service:** LoS and obstruction at 1, 5, 10, and 20 km, at 30 and
  33 dBm, with raw-derived goodput compared to the selected resolved service
  tier.

All phases A-I, not only phase I, use the same validated kilometre-scale bundle
with actual 20 km usable geometry. The existing approximately `200 x 150 m`
engineering terrain and any M1 local-world qualification are ineligible for
M7. A 50 km point is P1 ideal-case only and never substitutes for a required
P0 point.

Acceptance:

- A-I all execute and pass from the same sealed run/source/config/image;
- lifecycle supervisor, endpoints, Gazebo, SITLs, Sionna worker, and collectors
  remain continuously alive; one Gazebo world, one Sionna scene, and one
  canonical bundle ID remain unchanged; ns-3 is absent only inside declared
  B/H stop windows and every lifecycle transition matches the phase manifest;
- all causal metrics are independently derived from profile raw evidence;
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
- at least 600 seconds of uninterrupted integrated P0 soak in each child after
  warm-up, with all required services and traffic active;
- at least one 1,800-second stability window under the final integrated
  configuration;
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
- at least one child contains a valid 1,800-second stability window with the
  same health constraints;
- aggregate validation re-hashes both complete child raw trees, verifies each
  raw seal/signature/ledger inclusion, reruns both child validators, and
  confirms exact revision/contract/dependency/config/scene/image equality;
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
  operator steps; and
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
| Five-UAV health | Five models/SITLs, heartbeats, fresh odometry, IDs/ports, and no undeclared restart. |
| Active topology | Raw live namespace, route, socket, ns-3 node/device/queue, and executable provenance proves one route. |
| Packet provenance | Matching real endpoint bytes cross ingress, ns-3 queue/device, egress, and response. |
| No bypass | On succeeds, ns-3 stopped fails with live endpoints, restart recovers. |
| Three classes | Decoded/nonced control, payload, and additional data have non-zero bidirectional TX/RX for all UAVs. |
| Current online Sionna | Current Gazebo state produces asynchronous versioned real-provider link state. |
| Per-link causality | Latest non-expired directed state correlates with exact real packet outcomes. |
| Late policy | Expired/superseded/out-of-order state is rejected and fail-closed behavior passes. |
| Scene alignment | Canonical paired bundle, transforms, bounds, and three landmarks within 1 metre. |
| Shared medium | Real external single/concurrent bytes show ns-3-owned contention/queue effects. |
| Priority | At >=2x capacity, control p95 <=250 ms and loss <=5%, lower class degrades first. |
| Jamming | Off/on/off changes and restores SINR/J/S and packet results. |
| HitL | True PTY bytes and Ethernet frames traverse and depend on the same path. |
| Range/service | LoS/obstruction at 1/5/10/20 km and 30/33 dBm in validated geometry. |
| Time coherence | RTF, state age, late ratio, clock correlation, stale-pose, and packet intervals pass. |
| Heatmaps | Five layers share run/config/bundle/provider identity with packet evidence. |
| Soak | Each repeatability child >=600 s; one stability window >=1,800 s without prohibited failure. |
| Repeatability | Two clean-clone children independently revalidated with signatures and ledger entries. |
| Runtime restoration | Accepted OCI artifact restored and verified by content identity. |
| Fresh rebuild | Snapshot-pinned no-cache build reproduces canonical runtime manifests and capabilities. |
| Handoff | Extracted self-contained bundle recursively validates every child and instruction. |

Every row blocks customer-ready status.

## Validation Implementation Requirements

The v3 validator suite must:

- select a schema by declared profile and reject missing/extra profile-critical
  artifacts;
- verify raw manifest closure, hashes, signature key fingerprint, and external
  ledger inclusion before computing gates;
- parse/decode PCAP and raw endpoint/ns-3 device/queue events by byte/frame
  identity;
- reject ARP-only, empty, filename-only, internally generated, replayed, or
  wrong-run traffic;
- derive loss denominators, latency/jitter/goodput/queue percentiles, clock
  correlation, and warm-up windows independently;
- validate active namespace/ns-3 topology and forbidden-path probes;
- decode MAVLink heartbeat, nonce marker, valid command,
  `COMMAND_ACK`/telemetry, source, and target;
- verify supervisor/service readiness and declared ns-3 lifecycle against raw
  timestamps and process IDs;
- recompute Sionna current-state, latest-nonexpired per-link selection,
  late/discard/fail-closed behavior, and packet correlation;
- recompute A/B causal comparisons and all A-I gates;
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
3. Build and accept one-UAV M2 external real-byte/no-bypass path with raw-derived
   metrics and active topology.
4. Extend only that path to M3 five-UAV/class/direction matrix.
5. Add the M4 canonical bundle, asynchronous versioned per-link Sionna worker,
   and explicit expiry policy.
6. Drive real endpoint contention/overload through ns-3-owned M5 queues.
7. Attach true PTY and Ethernet endpoints to that unchanged path for M6.
8. Implement one non-interactive A-I runner and close one sealed
   `m7_single_run_candidate`.
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
contract hash, exact current milestone, longest fully closed prefix, accepted
run/profile identities, and next executable command. Only milestones passed
without caveat under this contract are counted.
