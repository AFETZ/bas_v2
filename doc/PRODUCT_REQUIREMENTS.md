# Product Requirements

## Product objective

Build a working, observable simulation stand for five unmanned aerial vehicles
(UAVs), one ground control station (GCS), modeled radio propagation and shared
medium behavior, interference analysis, and hardware-in-the-loop operation.

## Scenario

- Exactly five UAVs and one GCS.
- ArduPilot SITL supplies the flight-controller runtime for every UAV.
- Operating altitudes start at tens of metres and may be higher.
- The shared scene covers exactly 10 km by 10 km.
- Terrain relief is up to 200 m.
- The scene contains at least one urban-type settlement.
- Buildings range from low-rise structures to 15 floors.
- Gazebo, Sionna, and network state use one documented coordinate system and
  aligned geometry.

## Communications

- MAVLink control traffic uses a dedicated UART channel for each UAV.
- MAVLink payload traffic uses a second, separate UART channel for each UAV.
- A separate data channel supports both point-to-point and point-to-multipoint
  operation.
- Real endpoint bytes traverse the modeled packet path.
- ns-3 owns packet forwarding, queues, contention, arbitration, shared-medium
  access, and MAC behavior.
- Sionna RT owns propagation and channel state, including LOS/NLOS,
  reflections, path loss, RSSI, SINR, and interference effects.
- Sionna RT is never described as performing MAC arbitration.
- Control command/ACK and returning telemetry must cross the ns-3 path.

## Hardware in the loop

- Support a serial/COM interface to an external flight controller.
- Support an Ethernet UDP or TCP interface.
- External-controller bytes traverse the same ns-3 path as simulated endpoints.
- Execution is paced to wall-clock time.
- Every ingress and traffic-class queue is bounded.
- A watchdog detects stalled processing and stale channel state.
- Measure end-to-end delay and missed deadlines.

## Interference sources

Every jammer definition includes:

- position and orientation;
- center frequency and bandwidth;
- transmit power;
- duty cycle;
- start and stop time;
- continuous, pulsed, or sweep behavior;
- antenna pattern and orientation;
- azimuth, elevation, and gain;
- side lobes when applicable.

## Metrics

Every applicable run records:

- path loss, RSSI, SINR, and jammer-to-signal ratio (J/S);
- packet delivery ratio and packet error rate;
- latency, jitter, goodput, and queue delay;
- channel utilization and deadline misses;
- Gazebo real-time factor;
- Sionna query latency;
- radio-state age.

## Heatmaps

Produce baseline (no jammer), jammer-enabled, and delta heatmaps for:

- RSSI;
- SINR;
- J/S;
- service availability.

## Real-time and scalability

- Five UAVs are the mandatory baseline.
- Benchmark larger node counts.
- Benchmark multiple Sionna channel-state update rates.
- Support hybrid propagation.
- Outside high-fidelity regions, allow an explicitly selected simple
  long-range propagation model.
- Report the real-time operating envelope without inventing benchmark results.

## Run outputs

The integrated demo produces a concise summary JSON, CSV metrics, PCAP,
heatmaps, a run log, and a short Markdown report.
