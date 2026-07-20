# MAVLink And Endpoint Bridge

This directory contains the Day 4 bridge adapters for the packet-in-the-loop
path. The bridge keeps MAVLink routing in `mavlink-routerd` and only adds local
configuration, opaque UDP class queues, load generation, and PCAP proof hooks.

Source of truth:

- `network/config/endpoints.yaml` defines the five UAV endpoint mapping,
  MAVLink system IDs, ground-control endpoint, queue policy, ns-3 handoff
  ports, and PCAP hooks.
- `network/config/service_tiers.yaml` defines class priorities and deadline
  policy consumed by later ns-3 work.

Commands:

```bash
python3 network/bridge/bridge_config.py --check
python3 network/bridge/bridge_config.py --render --run-dir runs/bridge_dry_run
python3 network/bridge/priority_udp_bridge.py --self-test
python3 network/bridge/priority_udp_bridge.py --dry-run
python3 network/bridge/manual_gcs_bridge.py --self-test
python3 network/bridge/manual_gcs_bridge.py --dry-run --run-dir runs/manual_gcs_dry_run
python3 network/bridge/traffic_generator.py --traffic-class payload --uav uav1 --dry-run
```

Runtime shape:

1. Ground control uses the configurable endpoint from
   `AMS_MAVLINK_ENDPOINT`, defaulting to `udp:127.0.0.1:14600`.
2. `mavlink-routerd` routes MAVLink control/payload packets using generated
   configs under `runs/<run_id>/bridge/mavlink-router/`.
3. `priority_udp_bridge.py` classifies packets by configured UDP ingress port
   and forwards them toward ns-3 with bounded queues and control priority.
4. ns-3 owns packet behavior and writes PCAP/FlowMonitor artifacts.
5. UAV-side generated `mavlink-routerd` configs connect ns-3 egress packets to
   the correct SITL master TCP endpoint inside the UAV-side namespace.

The direct legacy `udp:127.0.0.1:14550` endpoint and direct SITL TCP ports are
forbidden on the ground side. `move_drone.py` must use the configurable bridge
endpoint and reject those direct bypass endpoints.

## Manual GCS Rock-Demo Path

`network/scripts/run_manual_rock_radio_demo.sh` starts
`manual_gcs_bridge.py` by default and prints the operator-facing GCS endpoint:

```text
tcp:127.0.0.1:14600
```

External GCS/QGC/manual tools should connect to that bridge endpoint. The
helper mirrors manual MAVLink byte chunks into `priority_udp_bridge.py`
control ingress and gates forwarding to the internal SITL TCP tail on fresh
ns-3/Sionna live trace samples. If the priority bridge is not bound, the live
ns-3/Sionna trace is stale, or the modeled SNR is down, forwarding fails
closed and the event is logged in `logs/manual_gcs_bridge.jsonl`.

This is intentionally guard-only hardening. It prevents accidental direct-port
P0 claims and provides checkable bridge/GCS config output, but it keeps
`p0_packet_path_eligible=false` until the manual command tail is fully replaced
with a bidirectional modeled ns-3 egress path.

Direct SITL master ports may be printed only with `--allow-direct-gcs`
(`--allow-direct-sitl-ports` is a compatibility alias); that flag is an
explicitly labeled NON-P0 convenience path and disables any manual
no-bypass/packet-path claim for that run.
