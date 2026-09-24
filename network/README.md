# Network Runtime

This directory contains the active packet, radio-channel, position, bridge, and
HitL runtime for the five-UAV product. The implementation is incomplete; use
`network/STATUS.md` for the next product task rather than treating validation
documents as readiness proof.

## Active components

- `config/scenario_5uav.yaml`: five launch-compatible UAV definitions with
  unique system IDs and DDS ports. Its current scene is only about 200 m by
  150 m.
- `config/endpoints.yaml`: GCS, UAV, traffic-class, UART-facing, Ethernet, and
  ns-3 endpoint mapping.
- `position_tracker/`: converts live ROS odometry to normalized, timestamped
  radio node state.
- `radio_provider/`: Sionna RT channel service for LOS/NLOS, path loss, RSSI,
  SINR, J/S, and jammer effects.
- `ns3/`: packet path, bounded queues, contention, priority, shared-medium
  arbitration, PCAP, and flow metrics. Its current CSMA model is an engineering
  surrogate, not a customer waveform.
- `bridge/`: endpoint adapters and traffic-class queues.
- `hitl/`: software loopback and timing adapters; live physical serial and
  Ethernet gateways remain unfinished.
- `scripts/`: component and integration entrypoints. Legacy acceptance scripts
  listed in `LEGACY_ACCEPTANCE.md` are inactive.

Sionna supplies physical link state and does not perform MAC arbitration. ns-3
performs arbitration and keeps processing packets between channel-state
updates. Every consumed link state must have a timestamp and bounded maximum
age.

## Product commands

From the repository root:

```bash
make check-env
make build
make run-base
make run-network
make stop
make test-changed
make status
```

`make run-base` launches the five-UAV Gazebo/SITL configuration without running
the network stack. `make run-network` starts the existing modeled 2.4 GHz
integration entrypoint and fails with a dependency diagnostic rather than
silently bypassing ns-3. `make stop` targets only process groups started by the
product wrappers.

Useful component-level development commands include:

```bash
python3 network/position_tracker/tracker.py --from-config-once
python3 network/radio_provider/provider.py sample-request --include-jammers
python3 network/bridge/priority_udp_bridge.py --self-test
./network/ns3/run_ns3_core.sh --mock-sionna
./network/tests/test_hitl_loopback.sh
```

Mock/free-space/provider samples and software HitL loopback are development
checks, not evidence that the integrated product works.

## Honest gaps

- No checked-in exactly 10 km by 10 km common Gazebo/Sionna scene exists.
- A live five-UAV arm/takeoff/hold/move/land run has not been verified by this
  process-reset change.
- Two separate real MAVLink UART byte paths per UAV are not yet demonstrated
  end to end through ns-3.
- The additional data channel is configured but its point-to-point and
  point-to-multipoint behavior is not yet demonstrated end to end.
- Complete jammer time profiles, directional patterns, and required heatmap
  families remain unfinished.
- Live external-controller serial/Ethernet HitL through ns-3 and Sionna/hybrid
  propagation remains unfinished.
- No real-time or scalability benchmark results are claimed.

See [requirements](../doc/PRODUCT_REQUIREMENTS.md),
[architecture](../doc/PRODUCT_ARCHITECTURE.md),
[development plan](../doc/DEVELOPMENT_PLAN.md),
[real-time plan](../doc/REALTIME_AND_SCALABILITY.md),
[status](STATUS.md), and the [minimal test matrix](TEST_MATRIX.md).

The former formal workflow is stored under `archive/acceptance_v3/` and the
remaining location-sensitive legacy files are catalogued in
[LEGACY_ACCEPTANCE.md](LEGACY_ACCEPTANCE.md). Neither is an active prerequisite.
