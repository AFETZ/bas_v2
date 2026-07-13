# Network and Radio Integration Execution Plan

This document is the execution contract for turning the existing
ArduPilot/Gazebo/ROS 2 multi-agent simulation into a packet-in-the-loop radio
and network simulation that can be demonstrated to a customer.

It is written for an autonomous implementation agent. The agent must be able to
work for one week without waiting for design clarification, make conservative
default choices, produce a working customer demo, and leave behind a repeatable
validation package.

The target is a customer-ready engineering demo, not a certified radio product.
No result may be presented as realistic unless real packets traverse the modeled
path and the logs prove that Sionna RT, ns-3, MAVLink traffic, interference, and
timing were active in the same runtime.

## Hard Audit Verdict

The previous plan had the correct architectural direction but was not yet safe
to hand to an autonomous agent. It described what the final system should look
like, but it did not force enough operational decisions, validation gates, or
customer handoff artifacts.

The main gaps were:

- No explicit definition of "customer-ready" or demo failure conditions.
- No default decisions for dependencies, directory layout, IPC, modem model, or
  fallback behavior when upstream integrations are incomplete.
- No hard proof requirements for "no bypass" between ground control and UAVs.
- No mapping from the current repository's launch ports, MAVLink behavior, and
  two-drone default config to the required five-UAV scenario.
- No one-week autonomous execution backlog with daily outcomes.
- No validation matrix proving packet path, contention, priority, jamming,
  heatmaps, Sionna runtime queries, and HitL adapters.
- No artifact contract for PCAP, FlowMonitor, logs, heatmaps, metrics, reports,
  and demo scripts.
- No risk register for likely blockers such as Sionna runtime latency, scene
  conversion, namespace/TAP privileges, MAVLink endpoint conflicts, and HitL
  hardware availability.

This version fixes those gaps by making the plan test-first, artifact-driven,
and strict about what counts as an accepted demo.

## Customer-Ready Definition

The week is successful only if the repository contains a repeatable system that
can be handed to a customer with:

- A single documented command to launch the full SITL demo.
- A single documented command to run the validation suite.
- A five-UAV plus one command-post scenario.
- MAVLink control traffic, MAVLink payload traffic, and one additional data
  channel routed through ns-3.
- Online Sionna RT radio state queried during runtime and used by ns-3 packet
  behavior.
- At least one jammer/interferer changing SINR, J/S, and packet outcomes.
- Shared-medium or point-to-multipoint contention between UAVs.
- Control traffic priority over payload traffic under congestion.
- PCAP evidence that packets traverse the modeled path.
- Logs and metrics for delay, jitter, packet loss, queue depth, RSS/pathloss,
  SINR, J/S, service tier, and late channel updates.
- Heatmaps for RSS, SINR, J/S, degradation zone, and service tier.
- A customer handoff bundle under `artifacts/` with run instructions,
  validation results, known limitations, and replayable evidence.

If any P0 acceptance gate in this document fails, the agent must not call the
system customer-ready. It must instead produce the best working system, mark the
failed gate in `network/VALIDATION_REPORT.md`, and explain the exact blocker.

## Current Repository Facts

The base simulator is:

```text
Ardupilot_Multiagent_Simulation/
```

It already provides the ArduPilot/Gazebo/ROS 2 multi-agent runtime. The network
integration must wrap this runtime. It must not replace it with a separate
simulator.

Important local files:

- `AGENTS.md`
  - Follow the repository author's README, launch files, package entry points,
    and source code as the source of truth.
- `README.md`
  - Documents the base ROS 2/Gazebo/ArduPilot workflow.
- `src/multiagent_simulation/launch/multiagent_simulation.launch.py`
  - Spawns drones from a YAML file.
  - Assigns MAVLink system IDs as `i + 1`.
  - Uses `master_port = 5760 + 10 * instance`.
  - Uses `sitl_port = 5501 + 10 * instance`.
  - Uses Gazebo FDM/control port `9002 + 10 * instance`.
  - Uses virtual ports `./dev/ttyROS{instance * 10}` and
    `./dev/ttyROS{instance * 10 + 1}`.
- `src/multiagent_simulation/config/robots.yaml`
  - Currently defines two drones only.
- `src/multiagent_simulation/multiagent_simulation/move_drone.py`
  - Currently connects directly to `udp:127.0.0.1:14550`.
  - This is a bypass risk and must be made configurable or routed through the
    network bridge before acceptance.
- `scripts/run_simulation.sh`
  - Runs a GPU-capable privileged container with host networking.
  - This is useful for Gazebo/Sionna/ns-3 privilege requirements, but the
    network scripts must still prove isolation.

The implementation agent must start by confirming these facts against the
actual checkout, because the repository may have changed.

## Non-Negotiable Architecture Decisions

### Switchable Radio Backend

The current acceptance target is a simulated 2.4 GHz radio/network backend:
`sim_2_4ghz`.

In this mode, Sionna RT provides online 2.4 GHz radio state, ns-3 models packet
behavior, and the bridge routes real command, telemetry, payload, and
additional-data packets through the modeled path. This is the only backend that
may satisfy the current P0/customer-demo path.

The future physical modem backend is `real_modem_2_4ghz`. It must reuse the
same command-post, UAV, traffic-class, artifact, and validation contracts as the
simulated backend. Switching to it must be possible through `AMS_RADIO_BACKEND`
or a `--radio-backend` option, without changing UAV control logic, MAVLink
system IDs, validation report layout, or the customer demo flow.

Physical 2.4 GHz modem hardware must not be required, probed, configured, or
used in the current implementation run. Agents may add fail-closed readiness
checks and future live-hardware instructions, but all current validation must
prioritize the software-modeled 2.4 GHz path.

### Packet-In-The-Loop First

The simulator must route real command, telemetry, and selected payload packets
through the modeled network path from the beginning of the integration.

No accepted demo may use a direct bypass path between the ground control side
and the UAV side. If the ns-3/radio path is down or degraded, MAVLink behavior
must degrade accordingly.

Accepted proof:

- Stop ns-3 while endpoints remain running.
- Show that ground side traffic cannot reach UAV endpoints.
- Show PCAP files from the modeled path when ns-3 is running.
- Show that the same command succeeds or fails according to modeled link state.

### Online Sionna RT First

Sionna RT is part of the first complete runtime loop.

The accepted demo must not use offline trace replay, cached-only radio maps, or
mock radio outputs as replacements for online Sionna RT. Static scene
preparation, acceleration structures, GPU warmup, batching, and worker
parallelism are allowed, but the runtime must query current node and jammer
state and use the result in packet behavior.

Mock radio providers are allowed only for unit tests and dependency smoke tests.
Any mock run must be named as such and cannot satisfy customer acceptance.

### ns-3 Owns Packet Behavior

ns-3 is responsible for:

- MAC/shared-medium access.
- Point-to-point and point-to-multipoint behavior.
- Queues.
- Priorities.
- Contention, arbitration, collision, or scheduling effects.
- Delay.
- Jitter.
- Packet loss.
- Flow metrics.
- PCAP capture.

Sionna RT is responsible for:

- Terrain and building effects.
- Reflection, diffraction, scattering, and NLoS behavior where configured.
- RSS/pathloss.
- SINR.
- J/S.
- Interference from explicit emitters.
- Antenna orientation and radiation patterns.

Sionna must not implement medium access arbitration. Its output is converted
into ns-3 channel, PHY, rate, or packet error behavior.

### HitL Readiness Uses The Same Modeled Path

HitL radio transceiver readiness is a target interface, not the current primary
runtime. The current primary runtime is `sim_2_4ghz`.

Future physical modem support must preserve:

- Serial/COM endpoint mode.
- Ethernet endpoint mode.
- The same command-post and UAV endpoint contract as the simulated backend.
- The same PCAP, metrics, timing, and validation artifact layout.

The same ns-3 plus online Sionna path must be used for SITL and current loopback
validation. Future physical HitL may require extra endpoint adapters, but it
must not force a separate control path or incompatible validation format.

Physical HitL hardware is intentionally out of scope for the current P0 path.
The agent must still deliver:

- Serial pseudo-terminal loopback validation.
- Ethernet loopback validation.
- Clear future hardware validation instructions for `real_modem_2_4ghz`.
- A `P1` limitation entry saying live hardware was not validated and was not
  required for the software-modeled 2.4 GHz demo.

Hardware absence must not block the SITL customer demo or the simulated 2.4 GHz
customer demo.

### Minimal Custom Code, Maximum Reuse

The implementation must avoid speculative from-scratch subsystems.

Prefer adapting existing modules, examples, documented APIs, and maintained
interfaces over writing new systems. Custom code is limited to:

- Thin integration layers.
- Scenario configuration.
- Protocol adapters.
- Launch scripts.
- Runtime supervision.
- Artifact collection.
- Project-specific glue.

Do not write a custom radio stack, custom MAC layer, custom ray tracer, custom
packet simulator, or custom MAVLink router unless all suitable existing options
are explicitly ruled out and the decision is documented.

Every new local component must state which upstream module or interface it wraps
or adapts. If it does not wrap or adapt an upstream interface, it needs an
explicit justification in `network/DECISIONS.md`.

### Research Before Inventing

When implementation details are unclear, do not guess and do not create a local
model from intuition.

Research is mandatory before adding any custom model for:

- Propagation.
- Packet error probability.
- Interference.
- Medium access.
- Real-time synchronization.
- HitL serial/Ethernet timing.
- MAVLink routing behavior.

The result of research must be translated into a small local adapter-oriented
design. Cite the source or upstream project in `network/DECISIONS.md` wherever
the decision is non-obvious.

## Default Decisions For Autonomous Work

If no user clarification is available, the agent must use these defaults.

### Runtime And Dependency Defaults

- Host OS target: Linux with Docker available.
- ROS/Gazebo target: the repository's existing ROS 2/Gazebo workflow.
- Container: extend or wrap the existing project container workflow.
- ns-3 location: `.external/ns-3/` or another ignored external directory.
- Sionna RT location: Python virtual environment or external dependency under
  `.external/`, never vendored into source.
- Generated runs: `runs/`.
- Customer artifacts: `artifacts/`.
- External dependency check command:
  `./network/scripts/check_deps.sh`.
- Full demo launch command:
  `./network/scripts/run_network_demo.sh`.
- Validation command:
  `./network/scripts/run_validation.sh`.

### IPC Defaults

Use a small TCP JSON-lines protocol between ns-3 and the online Sionna provider
unless an upstream ns-3/Sionna integration is adopted quickly.

Default endpoint:

```text
127.0.0.1:5090
```

Reason:

- Easy to implement in C++ ns-3 and Python Sionna without vendoring a large RPC
  stack.
- Easy to log and replay for debugging.
- Sufficient for a six-node engineering demo if queries are batched and cached.

If the agent selects another IPC mechanism, document the reason and keep the
same request/response fields.

### Modem And MAC Defaults

Use an existing ns-3 shared-medium model as the first accepted packet/MAC
surrogate. The preferred default is an ns-3 Wi-Fi/ad hoc or infrastructure
model configured as a 2.4 GHz radio abstraction with:

- Configurable transmit power from 1 W to 2 W.
- Configurable channel bandwidth.
- Service tiers from 1 Kbit/s to 20+ Mbit/s.
- Priority queues for control, payload, and additional data.
- Sionna-derived link state used for rate, PER, loss, or channel condition.

This is a modem abstraction, not a claim that the customer modem is Wi-Fi. If
the customer provides a proprietary modem MAC or waveform specification, replace
the ns-3 model configuration while preserving the same packet path and
validation gates.

Do not implement a custom MAC during the first week unless every suitable ns-3
model is ruled out and the decision is documented.

### Scene Defaults

The target scene is a 10 km x 10 km terrain/building environment with:

- Terrain height variation up to 200 m.
- Settlements or building clusters up to 15 floors.
- UAV low and medium altitude paths, including tens of meters above terrain.

If no customer scene asset is available, create a deterministic engineering
scene with synthetic terrain and building blocks. It must still be loaded by
Sionna RT and used for runtime queries. The customer report must label it as an
engineering scene, not a customer-supplied map.

### Position Source Defaults

Use the existing Gazebo/ROS 2 runtime as the source of UAV state.

Preferred implementation:

- Add a small ROS 2 position tracker under `network/position_tracker/`.
- Subscribe to the existing per-drone transform or pose topics.
- Publish a normalized node-state stream to the Sionna provider.
- Include command-post and jammer positions from scenario config.

The tracker must preserve the repository's existing launch and namespace model.

### MAVLink Defaults

Use existing MAVLink routing tooling where practical, especially
`mavlink-router`, before writing local MAVLink routing logic.

The ground control endpoint must be configurable. The hardcoded
`udp:127.0.0.1:14550` in `move_drone.py` is not acceptable for the final demo
unless that port is inside the isolated ground-side namespace and traverses the
modeled path.

Per-UAV routing must preserve:

- MAVLink system ID.
- Component ID.
- Control channel identity.
- Payload channel identity.
- Additional data channel identity.

## Target Scenario

The first complete scenario must support:

- 5 UAVs.
- 1 ground control station / command post.
- A 10 km x 10 km map.
- Terrain with hills and height changes up to 200 m.
- Settlements with buildings up to 15 floors.
- UAV flight at low and medium altitudes, including tens of meters above
  terrain.

Radio and traffic:

- 2 standard MAVLink channels over UART:
  - control.
  - payload.
- 1 additional radio channel:
  - point-to-point or point-to-multipoint.
  - point-to-multipoint is required for the 5 UAV plus 1 command-post
    scenario.
- Modem baseline:
  - 2.4 GHz carrier.
  - 1 W to 2 W transmitter power.
  - 10 km to 20 km normal operating range.
  - up to 50 km only as an ideal-condition case, not a guaranteed service
    target.

Service levels to evaluate:

- 1 Kbit/s: critical commands and low-rate telemetry.
- 10 Kbit/s: target forms.
- 100 Kbit/s: object situation updates.
- 500 Kbit/s: images and target portraits.
- 2 Mbit/s: video fragments.
- 20+ Mbit/s: full synchronization, expected only in near or ideal conditions.

## Runtime Architecture

```text
Ground control / command post app
  |
  | MAVLink control, MAVLink payload, additional data channel
  v
Ground-side namespace / TAP / veth isolation
  |
  v
Ground radio endpoint bridge
  |
  | real-time packet ingress/egress
  v
ns-3 real-time packet core
  |
  | batched link queries with current positions, antennas, modem state, jammers
  v
Online Sionna RT radio provider
  |
  | RSS, pathloss, SINR, J/S, service tier, PER/rate inputs
  v
ns-3 packet result, queues, PCAP, FlowMonitor
  |
  v
UAV radio endpoint bridge
  |
  v
ArduPilot SITL or external flight controller through serial/Ethernet
```

The architecture is accepted only if all three layers are active together:

- Real endpoint traffic.
- ns-3 packet simulation.
- Online Sionna RT channel/interference state.

## Required Directory Layout

The agent must create or update this layout:

```text
Ardupilot_Multiagent_Simulation/
  network/
    README.md
    DECISIONS.md
    PROGRESS.md
    VALIDATION_REPORT.md
    config/
      scenario_5uav.yaml
      endpoints.yaml
      radio_24ghz.yaml
      service_tiers.yaml
      jammers.yaml
      validation_matrix.yaml
    bridge/
      README.md
      ...
    hitl/
      README.md
      ...
    ns3/
      README.md
      ...
    position_tracker/
      README.md
      ...
    radio_provider/
      README.md
      ...
    scripts/
      check_deps.sh
      run_network_demo.sh
      run_validation.sh
      collect_artifacts.sh
      clean_runtime.sh
    tests/
      ...
  .external/          # gitignored external dependencies
  runs/              # gitignored generated runs
  artifacts/         # gitignored customer bundles
```

Do not vendor external simulator trees into this repository. External
dependencies must live outside the source tree or under ignored directories.

The source repository contains integration code, configs, tests, and scripts
only. Build products, generated traces, PCAP files, heatmaps, and experiment
outputs belong in ignored run/artifact directories.

## Required Components

### 1. Traffic Isolation Layer

Purpose:

- Ensure MAVLink and payload traffic cannot bypass the simulated network.
- Provide deterministic packet capture points.

Implementation options:

- Linux network namespaces.
- TAP/TUN.
- veth pairs.
- ns-3 TapBridge, FdNetDevice, or equivalent ns-3 emulation bridge.

Required behavior:

- Separate ground-side endpoint namespace from UAV-side endpoints.
- Prevent direct loopback reachability from ground control to SITL/HITL ports.
- Capture ingress and egress PCAP at predictable interfaces.
- Work for all 5 UAVs.

Acceptance:

- When ns-3 is stopped, the ground side cannot reach the UAV side.
- PCAP proves packets traverse the modeled path.
- The same isolation model works for all 5 UAVs.
- `move_drone.py` or its replacement cannot reach a UAV through a direct
  localhost shortcut.

### 2. Radio Endpoint Bridge

Purpose:

- Connect real or simulated MAVLink endpoints to ns-3.
- Preserve timing behavior as much as practical.

Required behavior:

- Serial byte pacing.
- Baud rate limits.
- Bounded buffers.
- Deadline/drop policy.
- Per-channel queues for control, payload, and additional data.
- Priority of control over payload.
- MAVLink system/component ID routing for 5 UAVs.
- Ethernet endpoint mode for HitL.
- Structured logs for queue depth, delay, drops, and channel assignment.

Acceptance:

- Control and payload channels can be independently loaded.
- Payload congestion cannot silently starve critical control traffic.
- Queue depth, delay, jitter, drops, and byte pacing are logged.
- Each UAV route is traceable by endpoint name and MAVLink system ID.

### 3. ns-3 Real-Time Packet Core

Purpose:

- Own packet-level network behavior.
- Model point-to-point and point-to-multipoint communication.
- Implement shared-medium effects for multiple UAVs competing for radio
  resources.

Required behavior:

- Real-time simulator mode.
- 5 UAV plus 1 command-post topology.
- Shared-medium or point-to-multipoint radio resource model.
- Priority classes for control, payload, and additional data.
- Configurable modem rates and service tiers.
- Packet loss, delay, and rate behavior driven by online Sionna RT results.
- FlowMonitor and PCAP outputs.

Acceptance:

- Simultaneous transmissions from multiple UAVs produce arbitration, queueing,
  collision, contention, or scheduling effects in ns-3.
- Control traffic remains measurable separately from payload traffic.
- Degrading one link affects real MAVLink behavior, not only dashboard metrics.
- FlowMonitor and PCAP files are stored under the current `runs/<run_id>/`.

### 4. Online Sionna RT Radio Provider

Purpose:

- Provide runtime radio propagation and interference results to ns-3.

Inputs:

- Current UAV positions and orientations.
- Current command-post position and antenna orientation.
- Current jammer/interferer positions and orientations.
- Carrier frequency, bandwidth, power, antenna patterns, and receiver
  parameters.
- Terrain and building scene.

Outputs:

- RSS/pathloss.
- SINR.
- J/S.
- Link state.
- Available service tier.
- Inputs for packet error probability and rate selection.
- Heatmap layers for analysis.
- Staleness flag when a query misses its deadline.

Required interferer model:

- Each jammer is a separate EM emitter.
- Configurable position and height.
- Configurable power.
- Configurable center frequency and bandwidth.
- Configurable antenna pattern and orientation.
- Optional time behavior such as duty cycle or sweep.

Acceptance:

- Sionna is queried during runtime with current state.
- Moving a UAV, command post, or jammer changes ns-3 packet behavior.
- Adding a jammer changes SINR/J/S and packet outcomes.
- Heatmaps can be generated for RSS, SINR, J/S, degradation zone, and service
  tier.
- Terrain and building geometry are included in the Sionna scene used by the
  runtime provider.

### 5. Time And Synchronization Supervisor

Purpose:

- Keep Gazebo/ROS 2, ArduPilot, ns-3, Sionna, and HitL adapters synchronized
  enough for real-time SITL/HITL behavior.

Required behavior:

- Wall-clock operation for HitL.
- Real-time ns-3 execution.
- Runtime delay accounting for Sionna calls.
- Deadline policy when channel updates are late.
- Logging of wall-clock delay, simulation time, queue delay, and channel query
  latency.
- Clear degraded-mode behavior when Sionna updates are late.

Acceptance:

- The system reports whether it is keeping real time.
- Late Sionna/ns-3 updates are visible in logs.
- HitL timeout/failsafe events can be correlated with network and channel
  timing.

### 6. Position Tracker

Purpose:

- Convert existing Gazebo/ROS 2 drone state into radio node state.

Required behavior:

- Track all five UAV names from scenario config.
- Preserve existing ROS namespaces.
- Include position, orientation, timestamp, and source topic.
- Provide command-post and jammer state from config or a runtime override.
- Feed the Sionna provider at a configurable rate.

Acceptance:

- Moving a drone in Gazebo changes the state sent to Sionna.
- Missing or stale pose data is logged and surfaced in validation.
- The position tracker does not change the existing flight-control path.

## Sionna Provider IPC Contract

Use JSON lines over TCP unless a documented replacement is selected.

Request:

```json
{
  "type": "link_query",
  "time_s": 123.456,
  "deadline_ms": 50,
  "radio": {
    "carrier_hz": 2400000000,
    "bandwidth_hz": 1000000,
    "tx_power_dbm": 33.0
  },
  "nodes": [
    {
      "id": "cp",
      "role": "command_post",
      "position_m": [0.0, 0.0, 20.0],
      "orientation_quat_xyzw": [0.0, 0.0, 0.0, 1.0],
      "antenna": "omni"
    },
    {
      "id": "uav1",
      "role": "uav",
      "position_m": [1000.0, 400.0, 80.0],
      "orientation_quat_xyzw": [0.0, 0.0, 0.0, 1.0],
      "antenna": "omni"
    }
  ],
  "emitters": [
    {
      "id": "jammer1",
      "position_m": [2500.0, 2500.0, 35.0],
      "center_hz": 2400000000,
      "bandwidth_hz": 1000000,
      "power_dbm": 40.0,
      "duty_cycle": 1.0
    }
  ],
  "links": [
    {
      "tx": "cp",
      "rx": "uav1",
      "traffic_class": "control"
    }
  ]
}
```

Response:

```json
{
  "type": "link_state",
  "time_s": 123.456,
  "provider_latency_ms": 18.2,
  "scene_id": "engineering_10km_v1",
  "links": [
    {
      "tx": "cp",
      "rx": "uav1",
      "traffic_class": "control",
      "pathloss_db": 113.0,
      "rssi_dbm": -80.0,
      "sinr_db": 15.4,
      "js_db": -5.2,
      "service_tier_bps": 100000,
      "per_input": 0.01,
      "link_state": "good",
      "stale": false
    }
  ]
}
```

The provider must log every request and response in a compact JSONL file under:

```text
runs/<run_id>/logs/sionna_link_queries.jsonl
```

## Validation Matrix

The validation suite must be executable through:

```bash
./network/scripts/run_validation.sh
```

It must create:

```text
runs/<run_id>/validation_report.md
runs/<run_id>/metrics/summary.json
```

### P0 Gates

P0 gates must pass for customer-ready status.

| Gate | Required proof |
| --- | --- |
| Dependency check | `check_deps.sh` reports all required runtime dependencies or exact missing items. |
| Five UAV launch | Gazebo/ROS 2/ArduPilot launches five configured UAVs with distinct names and system IDs. |
| No bypass | With ns-3 stopped, ground-side control cannot reach UAV endpoints. |
| Packet path | PCAP files show control, payload, and additional data crossing the modeled ns-3 path. |
| Online Sionna | Runtime logs show Sionna queries using current node and jammer state. |
| Sionna affects packets | Moving a UAV or enabling a jammer changes ns-3 packet loss, delay, rate, or service tier. |
| Shared-medium behavior | Concurrent UAV traffic produces measurable contention, queueing, collision, arbitration, or scheduling effects. |
| Priority | Payload load does not silently starve control traffic; control metrics are reported separately. |
| Jamming | A configured jammer changes SINR/J/S and packet outcomes. |
| Heatmaps | RSS, SINR, J/S, degradation zone, and service tier heatmaps are generated. |
| Artifacts | PCAP, FlowMonitor, logs, metrics, heatmaps, and customer report are collected under `runs/` and bundled under `artifacts/`. |
| Repeatability | The documented launch and validation commands work from a clean shell after dependency setup. |

### P1 Gates

P1 gates should pass if hardware and time allow. Failure must be documented but
does not block the SITL customer demo.

| Gate | Required proof |
| --- | --- |
| HitL serial live endpoint | External or loopback serial endpoint traverses the same modeled path with byte pacing and timing logs. |
| HitL Ethernet live endpoint | External or loopback Ethernet endpoint traverses the same modeled path with timing logs. |
| Long run | A 30-minute run completes without unbounded queue growth or process crashes. |
| Customer map | A customer-provided terrain/building asset is converted and loaded by Sionna. |

### P2 Gates

P2 gates are useful but not required for the first customer-ready demo.

| Gate | Required proof |
| --- | --- |
| Web UI | Scenario parameters can be edited through a UI. |
| Advanced cyber models | Non-radio cyberattack models are implemented and validated. |
| Alternate simulator | AirSim/Unreal/CARLA integration exists as an optional adapter. |

## Required Test Scenarios

### Scenario A: Baseline Connectivity

- Five UAVs and one command post.
- No jammer.
- Moderate distances.
- Expected result: MAVLink commands and telemetry work, payload is delivered,
  PCAP and FlowMonitor show normal traffic.

### Scenario B: ns-3 Stopped

- Start endpoints.
- Stop or do not start ns-3.
- Expected result: ground side cannot reach UAV side. No direct localhost or
  host-network bypass is allowed.

### Scenario C: Payload Congestion

- Generate high-rate payload and additional data.
- Send control commands during load.
- Expected result: payload queues and drops may grow, but control remains
  separately prioritized and measured.

### Scenario D: Multi-UAV Contention

- Trigger simultaneous transmissions from several UAVs.
- Expected result: ns-3 reports contention, queueing, collision, arbitration, or
  scheduling effects.

### Scenario E: Jammer Enabled

- Enable at least one jammer near a useful link.
- Expected result: Sionna reports changed SINR/J/S, ns-3 packet outcomes change,
  and MAVLink behavior degrades or recovers when the jammer is disabled.

### Scenario F: Mobility And Terrain Shadowing

- Move one UAV behind terrain or building obstruction.
- Expected result: Sionna link state changes and ns-3 packet behavior changes.

### Scenario G: Heatmap Generation

- Generate RSS, SINR, J/S, degradation zone, and service tier heatmaps for the
  scenario.
- Expected result: heatmap files are stored in `runs/<run_id>/heatmaps/` and
  referenced from the validation report.

### Scenario H: HitL Adapter Loopback

- Run serial pseudo-terminal loopback and Ethernet loopback through the same
  bridge and ns-3 path.
- Expected result: byte pacing, queueing, delay, loss, and endpoint status logs
  are collected.

## One-Week Autonomous Execution Plan

The agent must maintain:

```text
network/PROGRESS.md
network/DECISIONS.md
network/VALIDATION_REPORT.md
```

At the end of every workday, update `PROGRESS.md` with:

- Completed work.
- Validation commands run.
- Current P0/P1 gate status.
- Blockers.
- Next actions.

The agent must not wait for clarification unless a P0 gate is impossible
without external information or hardware. Use the defaults in this document and
keep moving.

### Day 1: Baseline, Skeleton, And Isolation

Goals:

- Confirm the base simulator launches.
- Create `network/` layout.
- Add five-UAV scenario config.
- Add dependency checks.
- Establish the no-bypass isolation path.

Deliverables:

- `network/README.md`.
- `network/config/scenario_5uav.yaml`.
- `network/config/endpoints.yaml`.
- `network/scripts/check_deps.sh`.
- `network/scripts/run_network_demo.sh` skeleton.
- Initial namespace/TAP/veth setup.
- `network/DECISIONS.md` with dependency and IPC choices.

Acceptance for the day:

- Five UAV names and system IDs are defined.
- The intended full-loop command exists, even if later components fail with
  clear diagnostics.
- A first no-bypass test exists and fails loudly if traffic can skip ns-3.

### Day 2: Position Tracker And Sionna Provider

Goals:

- Implement the online Sionna provider service.
- Implement or stub only for tests the JSONL IPC contract.
- Load a Sionna scene.
- Feed current UAV state from ROS/Gazebo into radio queries.
- Generate first heatmaps.

Deliverables:

- `network/radio_provider/`.
- `network/position_tracker/`.
- `network/config/radio_24ghz.yaml`.
- `network/config/jammers.yaml`.
- Sionna query logs.
- Heatmap generation command.

Acceptance for the day:

- Runtime query returns pathloss/RSS/SINR/J/S/service tier for at least one
  command-post-to-UAV link.
- Moving a configured node changes provider output.
- Enabling a jammer changes provider output.

### Day 3: ns-3 Real-Time Packet Core

Goals:

- Build the ns-3 real-time topology for five UAVs plus command post.
- Integrate Sionna link-state queries.
- Add service tiers, priorities, queues, PCAP, and FlowMonitor.

Deliverables:

- `network/ns3/`.
- ns-3 build/run script.
- PCAP output.
- FlowMonitor output.
- Link-state integration.

Acceptance for the day:

- ns-3 runs in real-time mode.
- Packets cross the simulated topology.
- Sionna output changes ns-3 packet behavior.
- Multi-UAV load creates measurable shared-medium effects.

### Day 4: MAVLink Packet-In-The-Loop

Goals:

- Route MAVLink control and payload channels through the modeled network.
- Eliminate direct bypass from ground-side tools to SITL.
- Make `move_drone.py` or a wrapper configurable for isolated endpoints.
- Add additional data channel traffic.

Deliverables:

- Ground endpoint bridge.
- UAV endpoint bridge.
- MAVLink endpoint mapping for five UAVs.
- Payload traffic generator.
- Additional data channel generator.
- PCAP proof for all traffic classes.

Acceptance for the day:

- A real command succeeds through ns-3 when the link is good.
- The same command fails or degrades when the modeled link is down or jammed.
- Stopping ns-3 breaks connectivity.

### Day 5: HitL Adapters, Timing, And Jamming

Goals:

- Add serial and Ethernet HitL endpoint modes.
- Add timing supervisor logs.
- Harden jammer configuration and service-tier selection.

Deliverables:

- `network/hitl/`.
- Serial pseudo-terminal mode.
- Ethernet endpoint mode.
- Timing supervisor logs.
- Jammer validation scenario.

Acceptance for the day:

- Serial loopback traverses the modeled path.
- Ethernet loopback traverses the modeled path.
- Timing logs correlate endpoint delay, ns-3 delay, queue delay, and Sionna
  query latency.

### Day 6: Validation, Customer Report, And Hardening

Goals:

- Run the full validation matrix.
- Fix P0 failures.
- Collect artifacts.
- Produce customer handoff package.

Deliverables:

- `network/scripts/run_validation.sh`.
- `network/scripts/collect_artifacts.sh`.
- `runs/<run_id>/validation_report.md`.
- `runs/<run_id>/metrics/summary.json`.
- `artifacts/customer_delivery_<date>.tar.gz`.

Acceptance for the day:

- All P0 gates pass or any failure is explicitly marked as not customer-ready.
- Customer can reproduce the demo from documented commands.
- Artifacts contain proof, not just screenshots.

### Day 7: Buffer And Final Polish

Goals:

- Re-run from a clean shell.
- Verify docs.
- Remove accidental generated files from source.
- Produce final status.

Deliverables:

- Final `network/README.md`.
- Final `network/VALIDATION_REPORT.md`.
- Final artifact bundle.
- Short operator demo script.
- Known limitations list.

Acceptance for the day:

- The agent can hand off the repository and artifact bundle without oral
  context.

## Dependency Strategy

Do not vendor external simulator trees into this repository.

Allowed external dependencies:

- ns-3.
- Sionna RT.
- 5G-LENA only if the selected modem/network model requires NR behavior.
- ns3-cosim only if gateway pieces help the runtime path.
- Existing ns-3/Sionna RT integration branches only if they help the online
  provider.
- mavlink-router or other maintained MAVLink routing tooling when useful.

The agent must create dependency checks that report:

- Missing executable.
- Missing Python package.
- Missing ROS 2 package.
- Missing GPU/CUDA capability for Sionna RT.
- Missing Linux networking privilege.
- Missing ns-3 build artifacts.

Dependency failures must be actionable. A customer should not see a Python stack
trace as the first explanation.

## Artifact Contract

Every full demo run must create:

```text
runs/<run_id>/
  command.txt
  environment.txt
  logs/
    launch.log
    bridge.jsonl
    ns3.log
    sionna_link_queries.jsonl
    timing.jsonl
    validation.log
  pcap/
    control.pcap
    payload.pcap
    additional_data.pcap
  flowmon/
    flowmon.xml
  heatmaps/
    rss.png
    sinr.png
    js.png
    degradation_zone.png
    service_tier.png
  metrics/
    summary.json
    queues.csv
    links.csv
    traffic_classes.csv
  validation_report.md
```

Customer bundle:

```text
artifacts/customer_delivery_<date>/
  README.md
  run_instructions.md
  validation_report.md
  architecture.md
  known_limitations.md
  selected_logs/
  selected_pcap/
  heatmaps/
  metrics/
```

The final archive must be generated by:

```bash
./network/scripts/collect_artifacts.sh runs/<run_id>
```

## Metrics Schema

At minimum, `runs/<run_id>/metrics/summary.json` must include:

```json
{
  "run_id": "2026-07-02T120000Z",
  "scenario": "scenario_5uav",
  "p0_passed": false,
  "duration_s": 600,
  "uav_count": 5,
  "traffic_classes": ["control", "payload", "additional_data"],
  "packets": {
    "control_tx": 0,
    "control_rx": 0,
    "payload_tx": 0,
    "payload_rx": 0,
    "additional_tx": 0,
    "additional_rx": 0
  },
  "latency_ms": {
    "control_p50": null,
    "control_p95": null,
    "payload_p50": null,
    "payload_p95": null
  },
  "loss_rate": {
    "control": null,
    "payload": null,
    "additional_data": null
  },
  "radio": {
    "min_sinr_db": null,
    "max_js_db": null,
    "late_sionna_queries": 0
  },
  "validation": {
    "no_bypass": false,
    "online_sionna": false,
    "jamming_effect": false,
    "priority": false,
    "heatmaps": false
  }
}
```

Use real values in runs. `null` is allowed only before a scenario has produced
that measurement.

## Risk Register

| Risk | Impact | Default mitigation | No-go condition |
| --- | --- | --- | --- |
| Sionna RT is too slow for per-packet calls | Runtime falls behind wall clock | Batch link queries, cache link state, query at 2-10 Hz, mark stale updates | Demo claims online behavior while using offline-only data |
| Scene conversion is blocked | No terrain/building radio effects | Generate deterministic engineering 10 km scene and document limitation | Provider ignores geometry entirely in accepted demo |
| ns-3/Sionna direct integration is too large | Week slips into framework work | Use JSONL TCP provider adapter first | Packet behavior cannot be affected by Sionna output |
| Linux namespace/TAP privileges fail in Docker | No isolation | Use privileged container, host scripts, and explicit checks | Ground side can reach UAV side without ns-3 |
| MAVLink endpoint confusion | Commands bypass model or hit wrong UAV | Centralize endpoint mapping and make GCS ports configurable | Hardcoded localhost reaches SITL directly |
| Five UAV simulation is heavy | Demo unstable | Run headless, disable optional cameras, reduce sensor load, keep network active | P0 five-UAV scenario cannot launch |
| HitL hardware absent | Live hardware validation impossible | Provide serial and Ethernet loopback, document live hardware steps | Claim live hardware validation without hardware evidence |
| External dependency install fails | Agent stalls | Produce actionable dependency report and continue with components that can be validated | Customer-ready claim without runnable dependency path |

## Reference Sources

Use these as reference material first. Do not vendor their code or replace the
target architecture with their architecture without an explicit decision.

### ArduPilot, MAVLink, ROS, And Gazebo

- `https://ardupilot.org/dev/docs/sitl-simulator-software-in-the-loop.html`
- `https://ardupilot.org/dev/docs/sitl-with-airsim.html`
- `https://mavlink.io/en/`
- `https://github.com/sotomotocross/UAV_simulator_ArduCopter`
- `https://github.com/engcang/mavros-gazebo-application`
- `https://github.com/hltdal/Gazebo-mavlink`
- `https://classic.gazebosim.org/tutorials?tut=dem`

### AirSim, Cosys-AirSim, And Unreal

- `https://microsoft.github.io/AirSim/`
- `https://github.com/microsoft/AirSim`
- `https://github.com/Cosys-Lab/Cosys-AirSim`
- `https://microsoft.github.io/AirSim/mavlinkcom/`
- `https://github.com/microsoft/AirSim/blob/main/docs/gazebo_drone.md`
- `https://discuss.px4.io/t/comparison-between-gazebo-and-airsim-for-hitl/7304`

### ns-3, Sionna, And Network Co-Simulation

- `https://github.com/nps-ros2/ns3_gazebo`
- `https://github.com/usnistgov/ns3-cosim`
- `https://github.com/usnistgov/siolena`
- `https://github.com/wineslab/sionna-channel-generator`
- `https://gitlab.com/nsnam/ns-3-dev/-/merge_requests/2608`

## Non-Goals

- No offline trace pipeline as the primary plan.
- No metrics-only co-simulation as an accepted simulator.
- No cached-only radio map as a replacement for online Sionna RT.
- No replacement of existing ns-3, Sionna, MAVLink, ROS 2, Gazebo, or
  ArduPilot functionality with speculative local subsystems.
- No custom technical model based only on intuition when papers, upstream
  examples, or maintained implementations can be researched first.
- No direct dependency on `AFETZ/bas`.
- No vendoring of external ns-3/Sionna/5G-LENA trees into this repository.
- No claim of realism until packet routing, online radio propagation,
  interference, MAC behavior, and timing are validated together.
- No customer-ready claim if any P0 gate fails.

## Final Agent Instructions

Start here:

```bash
cd /home/afetz/bas0.1/Ardupilot_Multiagent_Simulation
sed -n '1,220p' AGENTS.md
sed -n '1,260p' README.md
sed -n '1,1400p' doc/network_radio_integration_plan.md
git status --short
```

Then:

1. Do not overwrite unrelated user changes.
2. Create the `network/` implementation area.
3. Keep external dependencies outside source or under ignored directories.
4. Preserve the existing base simulator launch model.
5. Make every endpoint configurable.
6. Prove no bypass before calling packet-in-the-loop complete.
7. Keep `PROGRESS.md`, `DECISIONS.md`, and `VALIDATION_REPORT.md` current.
8. Run validation before handoff.
9. Package artifacts.
10. If P0 fails, say exactly which gate failed and do not claim
    customer-ready status.
