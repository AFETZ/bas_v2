# HitL Loopback And Timing

This directory contains the current Day 5 HitL readiness work for the
software-modeled 2.4 GHz path. It does not connect to physical radio hardware.

Current runnable path:

```bash
./network/scripts/run_hitl_loopback.sh --mode both
./network/tests/test_hitl_loopback.sh
```

The loopback runner creates:

- a serial pseudo-terminal endpoint using a PTY pair;
- a UDP endpoint shim for Ethernet-style traffic;
- bounded per-class queue logging;
- deadline/drop logging;
- timing logs that correlate endpoint ingress/egress, queue delay, virtual
  ns-3 delay, and virtual Sionna query latency.

Artifacts are written under `runs/<run_id>/`:

- `logs/timing.jsonl`
- `logs/bridge.jsonl`
- `logs/hitl_loopback.jsonl`
- `metrics/hitl_loopback_summary.json`
- `validation_report.md`

The virtual ns-3 and Sionna timing stages are placeholders for log-shape and
adapter validation only. They are marked with `actual_ns3=false` and
`actual_provider=false`. If `network/bridge`, `network/ns3`, or
`network/radio_provider` are missing, the summary records a modeled-path
blocker. This cannot satisfy P0 packet path, online Sionna, PCAP, FlowMonitor,
or customer-ready acceptance by itself.

## Backend Guard

`sim_2_4ghz` is the only current runnable backend:

```bash
AMS_RADIO_BACKEND=sim_2_4ghz ./network/scripts/run_hitl_loopback.sh --mode both
```

Physical modem paths are fail-closed:

```bash
./network/scripts/run_hitl_loopback.sh --mode real-hardware
```

That command exits before opening any serial device, network interface, modem,
or RF hardware. Selecting `AMS_RADIO_BACKEND=real_modem_2_4ghz` is reserved for
a future P1/P2 run after the simulated backend has the same endpoint contract,
PCAP capture, timing logs, and safety checks.

## Future Live-Hardware Instructions

Do not use these steps for the current P0 path. They are blockers and exact
inputs for a later `real_modem_2_4ghz` run:

1. Confirm the simulated `sim_2_4ghz` path is passing with ns-3, online Sionna
   RT, no-bypass proof, PCAP, FlowMonitor, and timing artifacts.
2. Obtain regulatory and lab authorization for the selected 2.4 GHz channel,
   power level, antenna, and physical environment.
3. Record the modem serial device path or Ethernet interface name, firmware
   version, RF channel, bandwidth, transmit power, antenna, and safety limits.
4. Place the physical endpoint behind the same command-post/UAV endpoint
   contract used by `network/config/endpoints.yaml`.
5. Keep traffic classes named `control`, `payload`, and `additional_data`, and
   preserve MAVLink system IDs `1` through `5`.
6. Capture endpoint, queue, packet, Sionna/radio-state substitute, PCAP, and
   timing artifacts under the same `runs/<run_id>/` layout.
7. Update `network/VALIDATION_REPORT.md` with P1 hardware proof and the exact
   modem setup used.

Current blockers for live hardware:

- no physical backend implementation is accepted in this workstream;
- no device identity, RF authorization, safety checklist, or modem
  configuration is present;
- the actual ns-3/Sionna/bridge path is not present in this isolated worktree.
