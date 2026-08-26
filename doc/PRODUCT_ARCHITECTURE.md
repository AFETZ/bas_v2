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
- The exact 10 km by 10 km shared Gazebo/Sionna scene is not yet implemented.
- Live physical HitL serial/Ethernet gateways are not yet complete.
