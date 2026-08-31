# Upstream one-UAV spike result

Run: `runs/upstream-radio-integration/proof_20260831_d` on 2026-08-31. Command:
`RUN_ID=proof_20260831_d network/ns3/run_upstream_sionna_tap_spike.sh`.
Configuration was declared before outcomes: CP `[5,0,2]`, LOS `[80,0,15]`, NLOS
`[80,160,15]`, 2.4 GHz, 5 MHz, `0.00001 W`, 1 Mbit/s, depth 1. No parameter or geometry
was tuned after the observation.

## Causal chain

1. Existing ROS tracker emitted fresh `source=ros_odometry`; native event CSV records 65
   applied poses and both exact UAV positions.
2. MR !2608 log records `Scene created` with CP/UAV coordinates, proving the live
   `MobilityModel` values entered Sionna.
3. Sionna 1.2.0 `PathSolver` completed: LOS generated 2 paths, NLOS 0, recovery 2.
4. Upstream `SionnaRtSpectrumPropagationLossModel` was the sole loss model on native
   `MultiModelSpectrumChannel`; there is no other loss/error adapter in the program.
5. Native `AlohaNoackNetDevice` produced 12 `MacTx` events for the UDP 5-tuple
   `10.71.0.10:41000 -> 10.71.1.10:5000`.
6. Native `HalfDuplexIdealPhy` produced 8 `RxEndOk` and 4 `RxEndError` decisions.
7. LOS delivered 4/4 datagrams; NLOS generated four native PHY errors and delivered 0/4;
   restored LOS delivered 4/4.
8. SIGTERM stopped the combined in-process Sionna/ns-3 runtime with
   `stop_reason=sionna_ns3_process_stopped`; `AFTER_STOP` delivered 0/1, so no fallback ran.
9. The only network route crosses the two isolated TAP bridges and native radio device.
   Boundary and reconstructed native-radio PCAPs both contain the fixed UDP flow; no direct
   GCS/UAV namespace link exists.

## Observable artifacts

- `metrics/summary.json`: `PASS`, per-phase UDP counts and native counters.
- `logs/ns3_sionna.log`: coordinates, solver calls and 2 -> 0 -> 2 path counts.
- `logs/native_radio_events.csv`: live poses and MAC/PHY outcomes.
- `logs/udp_received.csv`: LOS/recovery payloads; no NLOS/after-stop payload.
- `pcap/native_radio.pcap`: native radio Tx plus successful Rx copies.
- `pcap/tap_boundary.pcap`: real Linux TAP-boundary traffic.

Forbidden custom JSON/PER/provider/scheduler files were neither imported nor invoked.
This proves only the requested one-UAV spike, not five-UAV product migration or MR merge
readiness.
