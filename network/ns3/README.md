# ns-3 Packet Core

This directory contains the Day 3 packet-core adapter for the
network/radio integration plan.

The source here is project glue only. The ns-3 checkout and build products must
remain outside source control under `.external/ns-3/` or another path supplied
with `NS3_DIR`.

## Explicit shared-medium targets

`network/config/communication_qos.yaml` selects one of two independent
targets. The default `stock_ns3_csma` builds `ams-tap-packet-engine-stock`
against a pristine external `.external/ns-3-stock` ns-3.40 checkout. It uses
native `CsmaNetDevice`, its bounded queue, stock backoff/retry behavior,
TapBridge, PCAP, and Sionna-driven receive errors; it has no global scheduler
or ingress shaping. Build it in the same runtime environment used for the
Town01 run:

```bash
./network/ns3/build_ns3_tap_packet_engine_stock.sh
```

`centralized_priority_scheduler_over_csma_channel` remains a separate,
explicitly custom project policy over a patched ns-3 tree. It is not a stock
CSMA result. Both modes use the same real TAP/netns, five-SITL/ten-UART
scenario and live Sionna RT state, but neither is a customer modem model.

Each packet-engine run writes `metrics/medium_access_run.json` with the mode,
source-tree provenance, patch/scheduler/shaping state, and the Sionna mapping
metadata. Select a mode directly for the integrated run:

```bash
BAS_TOWN01_MEDIUM_ACCESS_MODE=stock_ns3_csma ./scripts/product/run_town01_full_stack.sh
```

## Runtime Command

```bash
./network/ns3/run_ns3_core.sh
```

The command creates `runs/<run_id>/`, generates an ns-3 topology file from the
repository YAML configs, copies `scratch/ams-radio-core.cc` into the external
ns-3 scratch directory, and runs the ns-3 program.

The current runtime packet-core mode is explicit:

```bash
./network/ns3/run_ns3_core.sh --packet-core-mode csma_surrogate
```

Leaving the option unset resolves the mode from `network/config/radio_24ghz.yaml`
where `ns3.shared_medium_model: csma` maps to `csma_surrogate`. Each run writes
`metrics/ns3_packet_core_mode.json` and includes a `packet_core` object in
`metrics/summary.json`.

By default, the packet core requires a live Sionna provider at the endpoint in
`network/config/endpoints.yaml`. For dependency smoke tests only, run:

```bash
./network/ns3/run_ns3_core.sh --mock-sionna
```

Mock Sionna mode is not valid P0 proof.

## CTTC/Blog-33 Pybind11 Sionna RT Path

For the manual live RSSI/SNR work, use the upstream ns-3 Sionna RT integration
from MR2608 instead of the legacy TCP JSONL provider. The checkout is external
and ignored:

```bash
./network/ns3/setup_ns3_sionna_rt.sh
```

This creates or updates `.external/ns-3-sionna`, configures ns-3 with Python
bindings, and fails unless configure reports `Sionna-RT support enabled`.

Run the live pybind11 probe against the matched Gazebo/Sionna rock scene:

```bash
./network/ns3/run_ns3_sionna_rt_live.sh --duration 20 --period 1
```

For a deterministic clear-to-shadow proof without a joystick, write a live
node-state trajectory and point the pybind11 probe at it:

```bash
RUN_ID=pybind_rock_shadow_replay
RUN_DIR="$PWD/runs/$RUN_ID"
mkdir -p "$RUN_DIR"/{logs,metrics,plots}

./network/scripts/write_rock_shadow_node_state.py \
  --duration 70 --rate-hz 2 \
  --output-json "$RUN_DIR/logs/node_state.json" \
  --output-jsonl "$RUN_DIR/logs/node_state.jsonl" &

./network/ns3/run_ns3_sionna_rt_live.sh \
  --duration 65 --period 1 --no-setup \
  --node-state "$RUN_DIR/logs/node_state.json"
```

With ROS/Gazebo odometry:

```bash
./network/ns3/run_ns3_sionna_rt_live.sh \
  --node-state runs/<run_id>/logs/node_state.json \
  --tx uav1 --rx uav2 --duration 120 --period 1
```

Outputs:

- `metrics/ns3_sionna_rt_live.csv`
- `plots/ns3_sionna_rt_live.png`
- `logs/ns3_sionna_rt_live.log`

This path uses `SionnaRtChannelModel` and
`SionnaRtSpectrumPropagationLossModel` inside ns-3 via pybind11. It does not
start the Python TCP Sionna provider.

## Model Choice

The first packet-core implementation uses ns-3's built-in CSMA shared-medium
model as a point-to-multipoint packet/MAC surrogate. It is not a customer modem
model and it does not claim Wi-Fi, Ethernet, or proprietary waveform realism.
The reason for this choice is practical Day 3 integration: CSMA provides an
existing shared channel, transmit queues, deterministic contention/backoff,
pcap hooks, FlowMonitor compatibility, and receive error models that can be
driven by Sionna link state without custom MAC/PHY behavior.

Sionna-derived link state is consumed as:

- `service_tier_bps` caps the generated traffic rate for each modeled flow.
- `per_input` is applied to the receiver-side ns-3 `RateErrorModel`.
- `sinr_db`, `js_db`, RSS, and pathloss are logged for validation correlation.

## External TapBridge Ingress

`network/ns3/packet_core_modes.py` is the mode contract and dependency
validator. The dedicated M2 runner now has working namespace/TAP/UseBridge
diagnostics, while the general integrated selector remains fail-closed:

```bash
python3 network/ns3/packet_core_modes.py \
  --mode tap_bridge_external \
  --purpose evaluation \
  --json-output runs/tap_bridge_eval/metrics/ns3_packet_core_mode.json
```

The `tap_bridge_external` mode checks for the upstream ns-3 `tap-bridge`
module, Linux `ip netns` support, and `/dev/net/tun`. `FdNetDevice` is an
alternative ingress API and is not placed in series with `TapBridge`. ICMP and
opaque-UDP good/down/recovery diagnostics have crossed the external path.
General P0 selection remains fail-closed until a sealed real-MAVLink M2 run is
independently validated and the sequential M0/M1 prerequisites pass.

`customer_modem_model` is also named, but it is a future fail-closed mode until
a customer modem packet model and no-hardware evaluation harness exist.

## Outputs

The ns-3 run writes under the selected run directory:

- `logs/ns3.log`
- `logs/ns3_sionna_client.jsonl`
- `pcap/ns3-p2mp-*.pcap`
- `flowmon/flowmon.xml`
- `metrics/ns3_packet_core_mode.json`
- `metrics/ns3_link_states.csv`
- `metrics/ns3_flows.csv`
- `metrics/traffic_classes.csv`
- `metrics/summary.json`

The later bridge worker still needs to connect real MAVLink/payload endpoints
to this packet core before the packet-path P0 gate can pass.
