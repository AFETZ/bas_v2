# Product Architecture

## Supported structure

```text
Gazebo + ArduPilot SITL
        |
 ROS odometry
        |
 Position Tracker
        |
 Channel Model Service
   |              |
Sionna RT    Simple models
        |
 per-link state
        |
       ns-3
        |
 UART / Ethernet / GCS / additional data endpoints
```

Gazebo supplies the physical scene and vehicle motion. Five ArduPilot SITL
instances supply flight-control behavior. ROS odometry is normalized by the
position tracker into a common coordinate frame. The channel model service
selects Sionna RT or a declared engineering model and publishes timestamped
per-link physical state. ns-3 consumes that state while forwarding real
endpoint packets and applying shared-medium behavior.

## Customer map asset flow

Official CAVISE bundles are the canonical customer-map geometry. Sionna loads
the bundle's existing `map/scene.xml` and PLY meshes. The bundle's Blender file
is the editable master; Gazebo visual and simplified collision meshes are
derived from that same geometry and coordinate frame. ZIP, PLY, and Blender
assets stay outside Git and may be loaded by ROI or tile.

`map/transforms.xml` is the transformation authority. Selection records the
source, Sionna, and Gazebo frames, any SUMO offset, and whether static vertices
are already baked before a Gazebo derivative is made. The legacy synthetic
`m4_canonical` scene is a smoke fixture only and cannot satisfy the customer
10 km by 10 km map requirement. See
[CAVISE map integration](CAVISE_MAP_INTEGRATION.md).

## Responsibility boundaries

| Component | Owns | Does not own |
| --- | --- | --- |
| Gazebo + SITL | vehicle dynamics, sensors, flight-control execution | packet arbitration or RF propagation |
| Position tracker | coordinate normalization, position/orientation freshness | propagation or packet delivery |
| Sionna RT | LOS/NLOS, reflections, path loss, RSSI, SINR, jammer effects | queues, contention, or MAC arbitration |
| Simple models | declared low-cost propagation estimates | hidden replacement of Sionna in high-fidelity regions |
| ns-3 | packet path, bounded queues, contention, priority, arbitration, MAC behavior | ray tracing or scene geometry |
| Endpoint adapters | serial/Ethernet framing and bounded pacing | bypass routes around ns-3 |

Sionna never performs MAC arbitration. ns-3 performs arbitration.

## Time and channel-state contract

- Physical/channel state and packet state update at different frequencies.
- ns-3 continues packet processing between Sionna updates.
- Every Sionna-derived link state carries its source timestamp, observation
  timestamp, and configured maximum age.
- Expired state cannot be used silently forever. The consumer records the
  stale event and follows a configured fail-closed or fallback policy.
- High-fidelity Sionna updates may be periodic or event-driven by movement,
  blockage, jammer transitions, or requested packet-path fidelity.
- Queue and packet clocks remain tied to the wall-clock pacing policy during
  real-time and HitL runs.

## Hybrid propagation

The channel service chooses a model per link or region. High-fidelity regions
use Sionna RT. Distant or simple LOS links may use FSPL, log-distance, two-ray,
or another explicitly named engineering model. Each result records its model,
parameters, timestamp, and validity age so fallback behavior is observable.

## Packet and endpoint topology

Each UAV has separate `control` and `payload` MAVLink UART paths plus an
`additional_data` path. Additional data supports point-to-point and
point-to-multipoint destinations. The GCS and HitL serial/Ethernet adapters use
the same ns-3 ingress and egress path. Stopping ns-3 must break that path;
direct SITL/GCS shortcuts are not part of product operation.

## Reproducibility boundary

Docker dependency locking and pinned runtime dependencies remain in place.
Rebuilds occur only when their inputs change. Cryptographic evidence, receipts,
signatures, hash chains, and qualification manifests are not product runtime
components.

## Current implementation facts

- `network/config/scenario_5uav.yaml` defines five unique UAV system IDs and
  DDS ports, but its checked-in terrain is only about 200 m by 150 m.
- The launch file assigns unique SITL/FDM port offsets for the five instances.
- ns-3 currently offers a CSMA shared-medium engineering surrogate.
- The TCP JSON-lines Sionna provider is the current in-repository channel
  service path; a separate pybind path remains diagnostic.
- Town01 is inspected, checksum-verified, externally prepared, and selected as
  the active development map with its measured 3.191 km by 3.191 km footprint.
- Town01 exercises the canonical asset path but does not satisfy the separate
  10 km by 10 km or up-to-200 m product requirements.
- The Town01 Gazebo development derivative preserves 867,887 source PLY
  vertices in the Sionna frame with an identity transform. Its surface and
  building collisions are axis-aligned approximations and vegetation is
  omitted for runtime cost.
- `make run-town01` exercises five SITLs, Gazebo, ROS odometry, real Sionna RT,
  ns-3, dual UARTs, additional data, the flight lifecycle, and run artifacts.
  It is a development integration command, not the final customer demo.
- Live physical HitL serial/Ethernet gateways are not yet complete.
