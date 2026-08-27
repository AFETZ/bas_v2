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

- Inventory existing official CAVISE bundles without extracting large assets.
- Select one town and a measured continuous 10 km by 10 km ROI containing
  terrain and a settlement; do not generate or stitch geometry.
- Keep the existing Sionna `scene.xml`/PLY geometry and Blender master as the
  canonical basis.
- Derive tiled Gazebo visuals and simplified collisions from the selected
  CAVISE geometry without changing its coordinate frame.
- Add a coordinate-alignment behavior test.
- Add a LOS/NLOS transition test driven by motion.

P3A ends after inventory, metadata inspection, selection, ROI, and external
asset preparation. P3B creates the Gazebo derivative and alignment test only
after P3A has a metadata-backed selection. The checked-in synthetic
`m4_canonical` fixture is legacy smoke and is excluded from this path.
Town01 may be used for that development path, but its measured 3.191 km square
footprint cannot close the 10 km by 10 km product criterion.

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

## Command exposure

Do not add Make targets for capabilities that do not have a runnable path.
`make run-town01` exposes the integrated Town01 development path, but it does
not close the compliant-map, physical-HitL, scalability, or final customer-demo
work. Benchmark and final-demo commands remain planned until those runnable
paths exist.
