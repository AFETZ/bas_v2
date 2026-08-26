# Product Development Plan

Work on the first incomplete stage only. A stage advances when its product
behavior is observable through the command and artifact listed in
`network/STATUS.md`; documentary flags alone do not complete a stage.

## P0 Process reset

- Use the product-first workflow and short active sources of truth.
- Keep the old formal process as a historical archive.
- Provide minimal build, run, stop, status, and changed-test commands.

## P1 Five-UAV baseline

- Start five ArduPilot SITL instances and five Gazebo UAVs.
- Assign unique MAVLink system IDs, FDM ports, and DDS ports.
- Observe live odometry for all five UAVs.
- Place one GCS on the surface.
- Demonstrate arm, takeoff, hold, movement, and landing.
- Provide one start command and one stop command.

## P2 Communication vertical slice

- Connect two separate MAVLink UARTs per UAV: `control` and `payload`.
- Carry a separate `additional_data` channel.
- Move real endpoint bytes through ns-3.
- Support point-to-point and point-to-multipoint data.
- Demonstrate command/ACK and returning telemetry.
- Do not require cryptographic evidence.

## P3 Shared 10 km by 10 km scene

- Add a terrain heightmap or mesh with up to 200 m relief.
- Add at least one urban-type settlement and buildings up to 15 floors.
- Derive Gazebo and Sionna geometry from one geometric basis.
- Add a coordinate-alignment behavior test.
- Add a LOS/NLOS transition test driven by motion.

## P4 Interference and medium access

- Implement complete jammer configuration, timing, and directional patterns.
- Generate baseline, jammer, and delta heatmaps.
- Exercise ns-3 contention with simultaneous transmitters.
- Give control traffic priority over payload traffic.
- Record channel utilization.

## P5 HitL and real time

- Implement live serial and Ethernet gateways.
- Route hardware bytes through real ns-3 processing.
- Use live Sionna or the declared hybrid channel service.
- Synchronize to wall-clock time.
- Monitor real-time factor and deadlines.
- Enforce timeout and stale-state policies.

## P6 Scalability and hybrid propagation

- Benchmark the five-UAV baseline.
- Benchmark larger node counts.
- Vary Sionna update rates and scene fidelity.
- Benchmark declared long-range fallback models.
- Document the measured operating envelope.

## P7 Integrated demo

One script starts the complete scenario and writes:

- concise summary JSON;
- CSV metrics;
- PCAP;
- heatmaps;
- run log;
- short Markdown report.

## Commands not yet exposed

Do not add Make targets for capabilities that do not have a runnable path.
The integrated P7 demo and benchmark commands remain planned until their
implementations exist.
