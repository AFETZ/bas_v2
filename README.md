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

During development:

```bash
make test-changed
make status
```

## Current state

The five-UAV configuration and launch mechanics exist, but this process-reset
change does not claim a live five-aircraft flight. The checked-in baseline
terrain is about 200 m by 150 m, not the required 10 km by 10 km shared scene.
The ns-3, Sionna, bridge, and HitL directories contain partial runtime paths;
the fully integrated real-byte and live-hardware path remains unfinished.

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
