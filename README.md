# BAS Five-UAV Simulation Stand

This repository develops a working simulation stand for five ArduPilot SITL
UAVs, one ground control station, Gazebo motion, ns-3 packet/shared-medium
behavior, Sionna RT propagation, interference analysis, and serial/Ethernet
hardware-in-the-loop integration.

The product target is an exactly 10 km by 10 km scene with up to 200 m relief,
an urban-type settlement, buildings up to 15 floors, separate MAVLink control
and payload UARTs, an additional point-to-point/point-to-multipoint channel,
wall-clock pacing, heatmaps, and measurable real-time performance.

## Architecture

```text
Gazebo + ArduPilot SITL -> ROS odometry -> Position Tracker
                                             |
                                      Channel Model Service
                                      /                   \
                              Sionna RT              Simple models
                                      \                   /
                                  timestamped link state
                                             |
                                            ns-3
                                             |
                           UART / Ethernet / GCS / data endpoints
```

Sionna computes propagation and channel state. ns-3 owns packet forwarding,
queues, contention, priority, shared-medium access, and MAC arbitration. Their
update rates are independent; ns-3 continues processing packets between
timestamped channel-state updates.

See [product requirements](doc/PRODUCT_REQUIREMENTS.md) and
[product architecture](doc/PRODUCT_ARCHITECTURE.md) for the complete contract.

## Quick start

Run from a sourced ROS/ArduPilot environment or the existing project container:

```bash
make check-env
make build
make run-base
```

Stop product processes from another terminal:

```bash
make stop
```

The network vertical slice is exposed separately and fails with diagnostics if
its external dependencies are unavailable:

```bash
make run-network
```

Run the complete five-UAV Town01 development scenario, including Gazebo,
ArduPilot SITL, ROS odometry, real Sionna RT, ns-3, dual UARTs, additional data,
flight lifecycle, PCAP capture, and heatmaps:

```bash
make run-town01
```

The command writes its self-contained result under `runs/town01-full-*` and
cleans up its container, namespaces, TAP devices, and child processes.

For the native Wi-Fi/Sionna presentation path, first run the non-mutating
preflight. On a fresh workstation, the explicit bootstrap mode installs only
missing ignored dependencies and refuses to overwrite an incompatible setup:

```bash
make demo-preflight
make demo-preflight DEMO_BOOTSTRAP=1
make demo-town01
```

The aligned rugged engineering scene is selected separately with
`make demo-rugged`. Both demo commands enable the live GUI by default; use
`DEMO_GUI=0` for a headless evidence run. Stop either demo from another
terminal with `make demo-stop`. `DEMO_BOOTSTRAP=1` can also be supplied directly
to either demo command on a new machine. Town01 remains an external licensed
asset; set `CAVISE_MAPS_DIR` to its official bundle directory before bootstrap
when it is not already prepared.

During development:

```bash
make test-changed
make status
```

## Current state

The five-UAV Town01 development scenario has passed one integrated runtime:
all five vehicles completed arm, takeoff, hold, movement, landing, and disarm
while their control, payload, and additional-data traffic traversed ns-3 using
live Sionna RT link state. This is not completion of the product target:
Town01 measures about 3.191 km by 3.191 km rather than 10 km by 10 km, the ns-3
medium remains the documented CSMA engineering surrogate, and live-hardware
HitL plus scalability characterization remain unfinished.

Continue from the first incomplete stage in the
[development plan](doc/DEVELOPMENT_PLAN.md) and verify the live state in
[network status](network/STATUS.md). Minimal checks are defined by the
[test matrix](network/TEST_MATRIX.md).

## Repository layout

- `src/multiagent_simulation/`: Gazebo/ROS 2 launch, models, worlds, and UAV tools.
- `network/`: packet path, channel service, tracking, bridge, HitL, configs, and tests.
- `scripts/product/`: product-first environment, run, stop, and changed-test commands.
- `doc/`: active product requirements, architecture, plan, and real-time guidance.
- `archive/acceptance_v3/`: historical formal workflow; inactive by default.

Docker locks and pinned dependencies remain part of reproducible development.
The image is rebuilt only when its Dockerfile, lock files, or system dependency
inputs change.

The previous formal acceptance/requalification workflow is archived for
historical analysis and is not a prerequisite for product development.

Original simulation maintainer credit: Gilbert Tanner.
