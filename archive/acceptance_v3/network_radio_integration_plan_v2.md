# Network and Radio Integration Execution Plan v2

## Document Status

This document supersedes `doc/network_radio_integration_plan.md` for all new
network/radio implementation and acceptance work. The original document remains
as historical design context.

This file is the immutable execution/acceptance contract for plan version 2.
Current milestone state is maintained in `network/PROGRESS.md`, accepted and
rejected evidence in `network/VALIDATION_REPORT.md`, and the next exact action
in `network/NEXT_TASK.md`. Updating those reports after a run does not alter the
runtime implementation hash; changing this contract does alter the recorded
config hash and requires new evidence.

Initial audit state when version 2 was introduced:

- Customer-ready: **false**.
- Fully closed milestones: **0**.
- Current milestone: **M0 — truthful validation and immutable runtime baseline**.
- The historical run `real_packet_loop_20260702T113341Z` is retained as a
  regression fixture for false-positive detection. It is not accepted P0
  evidence.

This status may change only when the milestone closure rules and evidence gates
in this document pass against the current checkout.

## Objective

Deliver a repeatable customer-facing engineering demonstration in which:

- five ArduPilot SITL UAVs and one command post run concurrently;
- real bidirectional endpoint packets traverse an isolated ns-3 packet path;
- MAVLink command, MAVLink payload, telemetry, and an additional data channel
  use that path without a localhost bypass;
- online Sionna RT consumes current ROS/Gazebo state and changes ns-3 packet
  behavior;
- mobility, obstruction, jammer, contention, and traffic priority have causal,
  measurable effects;
- raw evidence, validation results, and a customer bundle are reproducible from
  a pinned clean checkout.

The first accepted model is a documented 2.4 GHz engineering surrogate. It is
not a validated prediction of a proprietary customer modem.

## Scope Boundaries

### P0 Scope

- Backend: `sim_2_4ghz`.
- Linux, Docker, ROS 2, Gazebo, ArduPilot SITL, ns-3, and Sionna RT.
- Engineering scene, omni antennas, continuous jammer, and a maintained ns-3
  shared-medium model are allowed.
- Real external packet ingress/egress is mandatory even when the packet core is
  a CSMA or Wi-Fi surrogate.
- Serial PTY and Ethernet loopback must use the same modeled packet path if they
  are claimed as delivered features.

### Explicit Non-Claims

- No certified RF accuracy.
- No claim of matching a customer modem waveform, firmware queue, or MAC unless
  customer model data is supplied and calibrated.
- No physical RF hardware validation in P0.
- No cached-only, replay-only, or mock Sionna result may satisfy integrated P0.
- Dashboards, plots, and videos are presentation evidence only.

## Milestone Closure Rule

A milestone is `passed` only when all of the following are true:

1. Every listed deliverable exists in the current checkout.
2. Every listed acceptance check passes from raw evidence.
3. The milestone's required tests exit zero.
4. No acceptance caveat, TODO, placeholder, mock, synthesized proof, missing
   measurement, or known contradiction remains.
5. The evidence records the current source revision and configuration hashes.
6. `network/PROGRESS.md`, `network/VALIDATION_REPORT.md`, and
   `network/NEXT_TASK.md` agree on the status.

Milestones are sequential. Work for later milestones may exist, but it is not
counted as a closed milestone until every earlier milestone passes.

Allowed status values are:

- `not_started`
- `in_progress`
- `failed`
- `blocked_external`
- `passed`

`partial`, `mostly passed`, and `passed with limitations` are not closure
states. A limitation must either be outside the milestone contract or keep the
milestone open.

## Evidence Invariants

These invariants apply to every P0 run and override weaker file-presence checks.

### One Runtime, One Revision

- All P0 evidence comes from one `run_id` and one continuous runtime interval.
- Gazebo, all required SITL instances, position tracker, endpoint bridges,
  ns-3, Sionna provider, and traffic endpoints overlap for the full scenario.
- A P0 result may not combine PCAP from one run, Sionna logs from another, and
  video or node-state replay from a third.
- Every run records Git commit, dirty state, diff hash, config hashes, dependency
  versions, external dependency commits, and container image digest.

The M8 repeatability gate is the only deliberate cross-run aggregate: it points
to two independently sealed, independently validated clean-clone runs from the
same revision and dependency lock. Evidence files from those runs may not be
substituted into one another or into the candidate run; the aggregate contains
only identities, hashes, and independently recomputed validation results.

### Raw Evidence Is Authoritative

- Artifact collection never creates acceptance evidence that did not exist in
  the runtime.
- Post-processing may normalize raw data but may not write `PASS`, set P0 gate
  booleans, copy an unrelated PCAP into a traffic-class filename, or infer an
  experiment that was not run.
- `validation.* = true`, a non-empty file, a process timeout, or a line containing
  a UAV name is not proof by itself.
- The validator independently derives every gate from raw logs, PCAP, process
  health, packet counters, and command outcomes.

### Fail-Closed Conditions

Integrated P0 is automatically false when any of these occurs:

- any required traffic class has zero transmitted or zero received packets in
  the baseline scenario;
- required latency/loss/jitter fields are `null`;
- required PCAP contains no matching data packets;
- no MAVLink heartbeat and `COMMAND_ACK` return through the modeled path;
- a required process crashes, exits early, reports a bind error, or never
  becomes ready;
- any UAV pose is missing or stale;
- mock/test/free-space mode is used for accepted Sionna evidence;
- the active no-bypass experiment was not performed with live endpoints;
- source/config provenance is missing;
- evidence comes from different run IDs.

## Accepted Runtime Architecture

```text
GCS / command application in ams-gcs netns
  |
  | MAVLink control, MAVLink payload, additional data
  v
ground veth/TAP capture point
  |
  v
ns-3 external ingress (`TapBridge` in `UseBridge` mode)
  |
  | real-time shared-medium packet model, queues, QoS, loss and PCAP
  v
ns-3 external egress
  |
  v
per-UAV veth/TAP capture point in ams-uavN netns
  |
  v
UAV-side MAVLink router / endpoint adapter
  |
  v
ArduPilot SITL

ROS/Gazebo odometry -> normalized node state -> online Sionna RT
                                            -> versioned link state -> ns-3
```

Stopping ns-3 must break the only route between the GCS and UAV endpoint sides.
No bridge may mirror traffic through ns-3 while independently forwarding the
real bytes directly to SITL.

Standard ns-3 FlowMonitor is authoritative only for flows originated or
terminated inside ns-3. External host-to-host TAP traffic requires PCAP,
device/queue traces, endpoint counters, and a custom external-flow monitor (or
equivalent packet tags). An empty FlowMonitor file may not fail or pass an
external packet-path gate by itself.

## Authoritative Implementation Choices

### P0 Sionna Integration

Use the existing TCP JSON-lines provider for P0 until the vertical packet path
passes. It already supports the project-specific scene, jammer, metrics, and
heatmap contracts.

The upstream pybind11/ns-3 Sionna integration remains an evaluation path until
it replaces the JSONL path end-to-end. Evidence from both implementations may
not be mixed in one accepted run.

### P0 Packet Core

The current CSMA surrogate may remain the first packet-core model only if:

- real external packets enter and leave it;
- it produces measurable shared-medium behavior;
- its limitations are explicit in the customer report;
- no customer-modem waveform claim is made.

`tap_bridge_external` becomes runtime-selectable only after lifecycle, namespace,
bidirectional traffic, and active no-bypass tests pass. The name of a mode is
not evidence that the mode exists.

### Queue Ownership

- Endpoint adapters may perform UART byte pacing, finite buffering, and
  backpressure required to connect real endpoints.
- ns-3 owns network contention, service queues, traffic priority, packet loss,
  packet delay, and network jitter.
- Metrics must identify adapter delay separately from ns-3 queue/channel delay.

### Radio-to-Packet Ownership

- Sionna returns physical state: pathloss, RSSI, noise/interference power, SINR,
  J/S, geometry state, and freshness.
- A documented ns-3 adapter maps physical state to service rate and PER/error
  behavior.
- A single receiver-wide worst-link error rate is forbidden. Impairment must be
  link/direction specific.
- SINR-to-rate/PER tables require a cited source, calibration note, version, and
  deterministic unit tests.

## Endpoint Contract

The accepted endpoint matrix is five UAVs multiplied by three traffic classes
and two directions.

For every matrix entry, runtime metadata must record:

- UAV name and MAVLink system/component ID;
- source and destination namespace;
- ingress and egress interface;
- source/destination IP and port or PTY;
- traffic class and DSCP/TOS/queue mapping;
- baud/pacing policy where applicable;
- capture files;
- transmitted, received, dropped, and acknowledged counters.

Control evidence must include real MAVLink frames. Each attempt sends a valid
MAVLink nonce marker in a field whose semantics allow text (for example,
`STATUSTEXT.text`) followed by a semantically valid command; it must not abuse a
reserved/defined `COMMAND_LONG` parameter as a nonce. The probe records the
exact frame hashes and decoded MAVLink sequence, command, source, and target.
The validator proves those frame hashes at each capture point and correlates the
returning `COMMAND_ACK`/requested telemetry in a bounded time window. An ACK is
not claimed to echo a nonce when that MAVLink message has no nonce field.
Payload and additional data carry the full run nonce and monotonic sequence.
This proves packet identity without searching Ethernet padding or relying on a
filename.

## Coordinate and Scene Contract

Before mobility or obstruction can pass:

- Gazebo world frame, ROS odometry frame, ArduPilot NED conversion, and Sionna
  scene frame are documented with origin, axes, handedness, units, and quaternion
  convention;
- the position tracker records source frame and transformation version;
- Gazebo and Sionna scene files have recorded hashes;
- at least three landmarks match between Gazebo and Sionna within 1 metre;
- every UAV state includes timestamp, source topic, orientation, and freshness;
- antenna assumptions are explicit. P0 may use omni antennas, but must not claim
  orientation/pattern effects while fixed isotropic arrays are active.

## Time Contract

- ns-3 uses real-time execution for endpoint traffic.
- Sionna runs asynchronously from the ns-3 event loop.
- Every node state has `node_state_seq` and generation time.
- Every Sionna response has `query_id`, `node_state_seq`, generation time,
  completion time, and expiry.
- Every applied ns-3 update records `applied_state_id` and packet/time range.
- ns-3 consumes the latest non-expired state without blocking the scheduler.
- Expired state follows a configured fail-closed or bounded hold-last policy.

Default P0 timing thresholds:

- runtime real-time factor: `0.95 .. 1.05`;
- link-state age p95: no more than two configured Sionna update periods;
- required stale-pose samples: zero;
- late update ratio: at most 5 percent;
- control end-to-end p95 under priority scenario: at most 250 ms.

The full six-node/jammer batch must be benchmarked before selecting the Sionna
period. Increasing a deadline above the update period does not make an old
channel state fresh.

## Configuration Consistency Contract

One generated resolved config must be the source of truth for:

- carrier frequency;
- channel bandwidth;
- transmit power;
- noise figure and sensitivity;
- shared-medium capacity;
- service tiers;
- traffic offered rates;
- update periods and deadlines;
- scene and coordinate transform.

Validation fails when, for example, backend bandwidth is 20 MHz while the
provider uses 1 MHz, or a 20 Mbit/s service tier is selected on a modeled
1 Mbit/s channel without an explicit separate capacity model.

## Milestone State Machine

### M0 — Truthful Validation and Immutable Runtime Baseline

Purpose: make false progress impossible and qualify one exact, usable runtime
artifact before adding more runtime features.

Deliverables:

- this v2 plan;
- `network/VALIDATION_REPORT.md` reset to customer-ready false;
- corrected postprocessor that never synthesizes gate evidence;
- content-aware validator;
- regression test proving the historical false-positive run is rejected;
- provenance schema and dependency/version record;
- exact-image M0 dependency/provenance qualification runner whose output
  explicitly makes no packet-path, sealing, attestation, or P0 claim;
- durable status files updated consistently.

Acceptance:

- `real_packet_loop_20260702T113341Z` validates as not ready;
- ARP-only PCAP fails the packet-path gate;
- zero RX, 100 percent loss, and null required latency fail baseline/packet
  gates;
- a no-bypass smoke note cannot become active proof;
- previous summary booleans cannot directly pass causal gates;
- validation exits non-zero and explains every blocker;
- all implementation, configuration, schema, and validation files are tracked
  and the candidate checkout is clean;
- the dependency lock has status `complete`, identifies an exact usable
  project-image digest, and matches the recorded runtime package manifests;
- provenance independently recomputes the current source/configuration
  manifests and has no acceptance blocker;
- the M0 qualification validator passes both `dependency_check` and
  `provenance`, while retaining `p0_eligible=false`;
- sealing and adversarial tests prove that raw evidence is write-once and that
  post-run mutation, cross-run substitution, and producer PASS flags fail.

M0 qualifies the inspected immutable image ID and its exact runtime manifests.
It does not claim that a later no-cache build is bit-for-bit identical: the
current Dockerfile still consumes mutable APT, rosdep, and ArduPilot prerequisite
indices. Independent rebuildability is an explicit M8 gate and may not be
inferred from a cached rebuild or from two launches of the same local image.

### M1 — Healthy Base Simulator

Purpose: prove the flight runtime independently before network integration.

Deliverables:

- one command launching five UAVs with the current scenario;
- structured process/readiness log;
- five unique DDS ports, SITL endpoints, names, and system IDs;
- heartbeat and fresh ROS odometry evidence for every UAV;
- matched engineering scene provenance.

Acceptance:

- five Gazebo models exist;
- five SITL processes remain healthy for at least 300 seconds;
- system IDs are exactly `1..5` and DDS ports are unique;
- every UAV produces a heartbeat and fresh odometry;
- no bind error, crash, link-down, missing pose, or stale pose appears.

M1 records real-time factor but is a component-health gate, so it does not by
itself waive or satisfy the stricter M6-M7 wall-clock timing contract.

### M2 — One-UAV External Packet Vertical Slice

Purpose: prove packet-in-the-loop and isolation before adding radio complexity.

Deliverables:

- `ams-gcs`, `ams-ns3`, and `ams-uav1` namespace lifecycle;
- veth/TAP or equivalent maintained external ns-3 ingress/egress;
- bidirectional GCS-to-SITL routing with no direct tail;
- ingress/core/egress PCAP;
- automated good/down/recovery MAVLink transaction test.

Acceptance:

- with ns-3 running, 10 of 10 nonce-tagged commands receive the expected
  heartbeat/`COMMAND_ACK`/telemetry response;
- the nonce appears at GCS ingress, ns-3 ingress/egress, and UAV egress;
- with endpoints still live and ns-3 stopped, 0 of 5 new commands receive an
  ACK and heartbeat times out;
- after ns-3 restarts, communication recovers;
- direct SITL ports are unreachable from `ams-gcs`;
- baseline RX is non-zero and required latency/loss fields are populated.

### M3 — Five-UAV Packet Path and Three Traffic Classes

Purpose: scale the proven vertical slice without changing its route.

Deliverables:

- five isolated UAV-side endpoints;
- complete endpoint matrix;
- real MAVLink control and payload traffic plus nonce-tagged additional data;
- bidirectional class-aware counters and PCAP.

Acceptance:

- every UAV has non-zero TX/RX in every required direction/class;
- MAVLink system IDs route to the intended UAV only;
- stopping ns-3 disconnects all five UAVs;
- no traffic is observed on forbidden direct paths;
- all PCAP class labels are derived from decoded traffic, not filenames.

### M4 — Online Sionna Causality

Purpose: connect real geometry and interference to the proven packet path.

Deliverables:

- asynchronous versioned Sionna update worker;
- resolved coordinate/scene contract;
- per-link ns-3 impairment mapping;
- good/down/recovery mobility scenario;
- jammer `off/on/off` scenario;
- timing/freshness metrics.

Acceptance:

- raw logs correlate `node_state_seq -> query_id -> applied_state_id -> packets`;
- moving one UAV changes its link while a control UAV remains within tolerance;
- jammer on changes SINR/J/S and delivery metrics; jammer off restores them;
- the same real endpoint command succeeds, degrades/fails, and recovers with the
  modeled state;
- no accepted query uses mock/test/free-space mode;
- timing thresholds in this document pass.

### M5 — Shared Medium and Priority

Purpose: prove multi-UAV contention and control protection.

Deliverables:

- single-flow baseline;
- five-UAV concurrent-flow run;
- overload run at at least twice modeled capacity;
- ns-3 queue, delay, loss, goodput, and arbitration/contention metrics by class.

Acceptance:

- concurrent load produces a measurable queue/delay/goodput change relative to
  the single-flow baseline;
- under overload, control p95 is at most 250 ms and control loss at most 5
  percent;
- payload or additional-data service degrades before control;
- the priority result is produced by ns-3 network behavior, not only by a
  pre-ns-3 Python queue;
- queues remain bounded.

### M6 — HitL Loopback and Timing Correlation

Purpose: validate future endpoint forms without physical radio hardware.

Deliverables:

- PTY serial adapter with measured byte pacing;
- Ethernet adapter;
- both adapters connected to the exact M2-M5 packet path;
- stage-separated timing log.

Acceptance:

- PTY and Ethernet nonce packets traverse real ns-3 and online Sionna;
- evidence contains `actual_ns3=true` and `actual_provider=true`;
- stopping ns-3 breaks both loopbacks;
- adapter, ns-3 queue/channel, Sionna age, and end-to-end delays are separately
  observable.

### M7 — Integrated Scenario Matrix

Purpose: execute all product behavior in a single supervised runtime.

Required scenarios:

- A: five-UAV baseline connectivity;
- B: ns-3 stopped and recovery;
- C: payload congestion and priority;
- D: multi-UAV contention;
- E: jammer off/on/off;
- F: mobility and terrain/building shadowing;
- G: heatmaps from the same scene/config;
- H: PTY and Ethernet loopback;
- I: range/service sweep.

Range/service sweep defaults:

- distance points: 1, 5, 10, and 20 km;
- 50 km is ideal-case only and may use a dedicated larger engineering scene;
- transmit powers: 30 and 33 dBm;
- LoS and obstruction cases;
- measured goodput compared with the selected service tier.

The current `scenario_5uav.yaml` engineering terrain is approximately
`200 x 150 m` and is explicitly ineligible for this kilometre-scale sweep.
Before M7, the repository must add a dedicated large-area Gazebo/Sionna scene
or a separately named range-test scene pair whose collision/RF geometry,
coordinate transform, usable bounds, and hashes are validated. Moving models
outside the existing collision mesh or changing only scene metadata cannot
satisfy the range gate.

Acceptance:

- all required P0 scenarios pass from one run/revision;
- every metric is derived from raw evidence;
- five heatmaps contain run ID, scene/config hashes, and provider mode;
- no failure is hidden by artifact generation.

### M8 — Hardening and Customer Handoff

Purpose: prove repeatability and package the engineering demo.

Deliverables:

- at least 10-minute integrated P0 soak;
- 30-minute P1 stability run;
- two successful clean-clone executions;
- pinned dependency manifest and container image digest;
- content-addressed distribution of the accepted image by registry digest or
  verified OCI-archive hash, with restoration instructions;
- snapshot-pinned mutable package indices and versions sufficient for a fresh
  no-cache build to reproduce the locked runtime manifests;
- customer bundle with limitations and replayable raw evidence.

Acceptance:

- no crash, timeout, stale-pose failure, or unbounded queue growth;
- both clean-clone runs pass all P0 gates;
- a clean environment restores the accepted image by content identity and
  independently verifies its image ID and runtime manifests;
- a fresh no-cache source build reproduces the locked external revisions and
  pip/dpkg/ROS manifest hashes and passes the same capability checks;
- the bundle validator passes after extraction in a separate directory;
- operator instructions require no oral context;
- repository source and all integration code are tracked and reproducible.

## P0 Gate Matrix

| Gate | Required causal proof |
| --- | --- |
| Provenance | One run/revision; clean source/config/dependency hashes recorded. |
| Joint runtime | Required processes overlap and remain healthy for the full scenario. |
| Five-UAV health | Five models, SITLs, heartbeats, fresh odometry streams, unique IDs/ports, no errors. |
| Packet provenance | Nonce crosses GCS ingress, ns-3 ingress/egress, UAV egress, ACK/telemetry returns. |
| No bypass | ns-3 on succeeds, stopped fails with live endpoints, restart recovers. |
| Three traffic classes | Decoded/nonced control, payload, and additional data have non-zero TX/RX. |
| Online Sionna | Real provider consumes current node/jammer state and returns versioned fresh state. |
| Sionna causality | Applied state IDs correlate with changed real packet outcomes. |
| Link locality | Impaired target link changes while control link remains within tolerance. |
| Shared medium | Single versus concurrent flow comparison shows contention/queue effect. |
| Priority | Under overload, control meets latency/loss thresholds while lower priority degrades. |
| Jamming | Off/on/off changes and restores SINR/J/S and packet outcomes. |
| Time coherence | Real-time factor, state age, late ratio, and stale policy pass. |
| Scene alignment | Shared hashes/frame and landmark alignment are proven. |
| Heatmaps | Five layers use the same run/config/scene and real provider. |
| Artifacts | Bundle contains valid raw proof, not only expected filenames. |
| Repeatability | Two clean-clone runs pass. |

All P0 gates block customer-ready status.

## Validation Implementation Requirements

`network/scripts/run_validation.sh` must:

- parse PCAP content and packet counts;
- reject ARP-only class captures;
- verify traffic-class ports/markers and run nonce;
- decode or independently confirm MAVLink heartbeat and `COMMAND_ACK` evidence;
- parse process readiness and failure markers;
- reject zero RX, complete loss, and missing mandatory metrics;
- validate active no-bypass phase transitions using timestamps and process IDs;
- compute causal gates from paired/A-B scenario records;
- ignore pre-existing summary gate booleans when deciding P0;
- exit non-zero whenever any P0 gate lacks proof.

Artifact collection must be read-only with respect to gate results. It may copy
already-produced evidence into a bundle but may not repair, replace, fabricate,
or rename unrelated artifacts to satisfy the matrix.

## Dependency and Provenance Contract

The repository must pin or record:

- ArduPilot revision;
- ROS 2 distribution and package versions;
- Gazebo version;
- ns-3 commit and enabled modules;
- Sionna RT, Mitsuba, Python, NumPy, and GPU/runtime versions;
- external Sionna/ns-3 integration commit if selected;
- Docker image ID/digest;
- kernel and network namespace capabilities.

External branches must not be accepted by branch name alone. The exact commit
must be recorded and reproducible.

## Execution Strategy

Milestones, not elapsed days, control progress. A one-week implementation is a
target only when dependencies, namespace privileges, and the base five-UAV
runtime already pass.

Recommended effort order:

1. M0 validator/provenance freeze.
2. M1 healthy five-UAV runtime.
3. M2 one-UAV external packet path.
4. M3 scale packet path to all UAVs/classes.
5. M4 online Sionna causality.
6. M5 contention and priority.
7. M6 HitL through the same path.
8. M7 integrated matrix.
9. M8 clean-clone hardening and handoff.

Work on dashboards, video, customer maps, advanced antenna patterns, alternate
simulators, and physical modem hardware must not preempt the open critical-path
milestone.

## Runtime Status Records

This contract intentionally contains no mutable milestone ledger. Operators and
validators must use the three durable status files named in Document Status.
Those files must agree, and only fully passed sequential milestones are counted.
