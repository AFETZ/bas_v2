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

## Official ns-3.48 product slice

The follow-up product run is `runs/native-radio-product/native-product-20260831T193000Z`
and passed. It uses official ns-3.48 commit
`d2add90b452d600cfb4859baed8e9ea633519447`. Pristine configure and the official
Sionna example passed, including phased-array creation. Pristine Town01 loading failed
because Sionna RT 1.2.0 removed the `Scene(filename=...)` API, and the project spike then
failed because the upstream antenna API expected `AntennaModel*`. Only after recording
those failures, the existing 3-file, 8-addition/8-deletion compatibility patch was applied;
it changes scene loading and phased-array pointer storage only, not equations, solver,
PHY/MAC, or packet decisions.

The isolated gate used Python 3.12.3, Sionna/Sionna RT 1.2.0, Mitsuba 3.7.1,
Dr.Jit 1.2.0, pybind11 2.11.1, cppyy 3.5.0, GCC 13.3.0 and CMake 3.28.3. The
containerized product runtime used the same Python dependencies with Python 3.10.12,
GCC 11.4.0 and CMake 3.31.6.

The `generic_native_spectrum_aloha_reference` path is
`MultiModelSpectrumChannel -> SionnaRtSpectrumPropagationLossModel ->
HalfDuplexIdealPhy/ShannonSpectrumErrorModel -> AlohaNoackNetDevice -> TapBridge`.
It has `technology_specific_modem: false`, `native_ns3_phy: true`,
`native_ns3_mac: true`, `custom_packet_error_model: false`,
`custom_scheduler: false`, and `sionna_in_process: true`.

Real Gazebo `/uav1/odometry` from `ros_gz_bridge` supplied 1,976 live samples at
10.0 Hz, 146.105 m maximum displacement and 15.345 m maximum altitude. Real sysid-1
SERIAL1 returned AUTOPILOT_VERSION plus COMMAND_ACK; independent SERIAL2 returned its
own ATTITUDE request ACK and response. The additional application originated and received
10 checksummed packets in each direction, with no ns-3 echo. ArduPilot completed
GUIDED takeoff, the preloaded AUTO route, LOS, the obstructed candidate, return, LAND and
auto-disarm. In the 2-second endpoint windows, LOS had 4 Sionna paths and native PHY
5 `RxEndOk`/0 `RxEndError`; the obstructed candidate had 0 paths and 0/4; return had
3 paths and 4/0. No desired NLOS or PDR result was imposed.

Sionna solve time was 332.562 ms p50, 341.488 ms p95 and 863.629 ms max/cold;
steady-state p95 was 340.845 ms. ns-3 realtime lag was 0.075 ms p50, 3.919 ms p95
and 3,539.014 ms max during cold startup. Gazebo RTF mean was 0.99873 and p5 was
0.99918. After terminating the combined ns-3/Sionna process, 10.5 seconds produced no
control, payload, reverse telemetry or additional-data datagrams and no direct GCS SITL
socket. This closes the real one-UAV vertical slice only. Five-UAV native migration,
capacity validation, jammer behavior and hardware HitL remain pending.

## Native five-UAV migration

Run `runs/native-radio-five-uav/native-five-final-20260901T030000Z` passed the
functional five-UAV gate. One ns-3 process contained one
`MultiModelSpectrumChannel`, one in-process `SionnaRtSpectrumPropagationLossModel`,
and six native `HalfDuplexIdealPhy`/`AlohaNoackNetDevice`/antenna/MobilityModel
sets for the command post and UAV1...UAV5. Six standard TAP boundaries connected
GCS `10.71.0.10` and UAV endpoints `10.71.1.10`...`10.71.5.10`; ten existing
BSF1 UART adapters carried real SERIAL1/SERIAL2 MAVLink. The local CP ingress CSMA
segment is explicitly not a radio medium. Preconfigured neighbors work around the
upstream ideal-PHY ARP reentrancy limit and do not change packet outcomes.

All five `/uavN/odometry` publishers were observed at 10 Hz and supplied 19,187
fresh tracker samples per UAV. ns-3 atomically applied 8,380 complete six-node
snapshots with zero fail-closed stale samples; maximum applied position age was
147.784 ms and the maximum per-UAV p95 was 69.603 ms. Each sysid 1...5 returned an
independent control AUTOPILOT_VERSION ACK and post-command telemetry, and an
independent payload ATTITUDE ACK/response with no matching control ACK. The common
parallel safe request also returned five distinct ACKs.

P2P offered exactly 10 packets per UAV in both directions. All 50 downlink packets
arrived; uplink application deliveries were UAV1 5/10, UAV2 10/10, UAV3 10/10,
UAV4 10/10 and UAV5 6/10. No retry or ns-3 echo was used. P2MP used 20 multicast
application roots and exactly 20 command-post native `MacTx`; in this observation
each UAV had 20 `RxEndOk`, 0 `RxEndError`, 20 unique application deliveries and no
duplicates. This is an observed physical result, not a required PDR.

The simultaneous-uplink profile offered 20 independently originated 256-byte
packets per UAV at the same predeclared instant. Native traces recorded 20 `MacTx`
per UAV and 200 overlapping interval pairs. Application/native `RxEndOk` deliveries
for UAV1...UAV5 were 3, 2, 2, 10 and 3, giving PDR 0.15, 0.10, 0.10, 0.50 and 0.15
and Jain fairness 0.634921. No scheduler, shaping or retransmission logic was added.

UAV1 completed the frozen LOS, corridor, obstructed-candidate and return route while
UAV2...UAV5 held their declared positions; all five completed staggered arm/takeoff,
LAND and automatic disarm through the native radio. Stopping the single common
ns-3/Sionna process produced zero control/payload messages and zero additional data
for 10.5 seconds. The exact final implementation also passed
`runs/native-radio-product/native-one-final-regression-20260901T040000Z` with real
Gazebo odometry, dual UART, additional data, flight and no-bypass.

The five-UAV functional run intentionally used Gazebo RTF 0.1 to separate functional
correctness from target realtime readiness. Cold scene load/path solve/channel compute
were 385.754/465.202/851.254 ms. Steady path solve p50/p95/max was
30.568/31.450/52.286 ms, while full per-pair channel compute was
331.627/336.498/372.619 ms. Across the 15 antenna pairs, native logs recorded 15
cache misses, 13,275 hits, 3,304 stale updates and 3,319 path computations. The
final run deliberately made no periodic report-only `GetParams` calls; upstream logs
record actual path counts and `CalculateTauFromPaths` execution but do not export
individual delay values, so none were reconstructed.

Measured ns-3 lag was 607.594 ms p50, 3,880.427 ms p95 and 12,550.080 ms max.
Gazebo RTF was 0.099990 mean and 0.099984 p5. The native process used at most
1.169 GB RSS and 2.743 GB GPU memory; its one-core-normalized CPU was 91.535% p50,
103.305% p95 and 257.783% max. Therefore
`functional_five_uav_native_path=passed`, while `realtime_readiness=limited`; target
RTF 1 is not claimed. The generic profile remains non-technology-specific. The older
custom five-UAV provider/PER/scheduler contour is comparative only and is not in the
primary process. Jammer behavior and hardware HitL remain pending.
