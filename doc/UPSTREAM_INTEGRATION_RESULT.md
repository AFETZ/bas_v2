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

## Native five-UAV realtime operating envelope

The realtime branch retains official ns-3.48 commit
`d2add90b452d600cfb4859baed8e9ea633519447`, Sionna/Sionna RT 1.2.0, Town01, the
minimal MR !2608 compatibility patch, and the same generic native channel/PHY/MAC
chain. A narrow cache extension to upstream `SionnaRtChannelModel` reuses one loaded
Town01 scene and updates the current transmitter/receiver poses before each genuine
per-pair Sionna solve. It invalidates a pair on the declared maximum age (2 s) or
position displacement threshold (1 m); it changes no propagation equation, packet
error decision, PHY/MAC decision, traffic schedule, or radio topology. The external
model remains a single shared six-node spectrum medium with the 15 physically required
unordered endpoint pairs.

The partial RTF-1 evidence run is
`runs/native-radio-realtime/native-realtime-five-20260831T233000Z`. Its cold scene
initialisation/path solve/full channel operation were 394.362/494.250/888.944 ms.
After warm-up, genuine per-pair Sionna path solve/full-channel p95 were
30.640/30.845 ms (3,883 samples); there was one scene initialisation, 15 initial
pair misses, 31,595 cache hits and 3,869 age invalidations. Gazebo RTF mean/p5 were
0.99783/0.99931. Native process RSS peaked at 888 MB, GPU memory at 2.743 GB and
native one-core-normalized CPU p95 at about 36%; fresh live tracker position p95
reached at most 55.088 ms across the five UAVs. Steady ns-3 scheduler lag p50/p95
was 0.072/0.082 ms, excluding cold start.

This is an operating-envelope observation, not a pass. The real five-UAV Aloha
control path accumulated severe MAVLink response latency; the run was manually stopped
before its flight and application scenario completed. Consequently
`functional_five_uav_native_path=failed` and `realtime_readiness=limited` in its
generated report. The capture process records only unmodified frames from real Gazebo
camera sensors, with sidecar metadata for run, phase, camera pose, all five fresh UAV
positions and the command post. The incomplete run captured only the shared-medium
frame, and its six required lifecycle images are correctly marked failed rather than
being substituted or inferred. Camera placement is now explicitly aimed at the command
post/five-UAV formation and UAV1's obstacle crossing; a complete RTF-1 run is still
needed to validate those live frames.

The generic metrics follow the native availability contract. Received PSD would support
Rx power/RSSI where a public upstream API provides it; the selected PHY does not, so
RSSI/Rx power is `unavailable`. SNR/SINR is likewise `unavailable` unless the native
interference model exports it. PHY `RxEndOk`/`RxEndError` give empirical PER (for
example, the partial run observed CP-to-UAV1 93 successful end events and zero error
end events among 94 start events). BLER is deliberately `unavailable`: the current
HalfDuplexIdealPhy/ShannonSpectrumErrorModel reference has no user-facing
transport-block abstraction. No unavailable quantity is synthesized.

## Five-UAV control-latency decision gate

The RTF-1 stationary decision gate was run on 2026-09-01 with the same Town01 scene,
solver profile (depth 1, LOS plus specular only, seed 42), one shared generic native
medium and five real SITL UART endpoints. It uses only safe one-shot
`MAV_CMD_REQUEST_MESSAGE(AUTOPILOT_VERSION)` requests. Every operation has a separate
attempt record; no more than one request of that command ID is outstanding for a given
UAV. Each run has `report.md`, `metrics/mavlink_latency_chain.csv`, UART load/stream
composition, public queue/PHY summaries and the observer record in its own run
directory.

`control-latency-A-20260901T002000Z` is the one-UAV control point with both UARTs:
23/23 ACKs, all 23 post-write UART deliveries, first-attempt RTT p95 4.997 ms, no
RxAbort and no queue drop. Real SITL did not return a matching MAVLink PING, so PING
is explicitly `supported: false` after one non-synthetic probe per UAV; no made-up
20-sample PING statistic appears.

The five-UAV results are below. `parallel` is the ten rounds of five back-to-back
one-shot requests, not a sequential loop. RTT statistics include only ACKs received
within the one-second one-shot window.

| Run | Active UARTs | PHY rate | ACKs / attempts | Parallel ACKs / 50 | RTT p95 (ms) | RxAbort | Queue drops | RxEndError |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `control-latency-B2-20260901T003000Z` | control | 1 Mbit/s | 78 / 115 | 13 | 726.256 | 353 | 0 | 0 |
| `control-latency-C-20260901T004000Z` | control,payload | 1 Mbit/s | 78 / 115 | 13 | 821.383 | 348 | 0 | 0 |
| `control-latency-D2-20260901T005000Z` | control,payload | 2 Mbit/s | 79 / 115 | 14 | 663.748 | 344 | 0 | 0 |
| `control-latency-D5-20260901T006000Z` | control,payload | 5 Mbit/s | 78 / 115 | 13 | 713.044 | 352 | 0 | 1 |
| `control-latency-D10-20260901T007000Z` | control,payload | 10 Mbit/s | 78 / 115 | 13 | 757.514 | 396 | 0 | 28 |

For the 1-Mbit/s control-only gate, public `Queue` traces show the CP FIFO did accept
and dequeue the four immediately queued requests: depth reached 4, queue-residence p95
was 4.106 ms and drop count was zero. Adapter frame accounting shows all 115
`COMMAND_LONG` records were handed to real UAV UARTs and all 115 corresponding
`COMMAND_ACK` records were observed leaving those UARTs, while only 78 ACKs reached
the GCS scenario boundary. Thus neither GCS dispatch, the Aloha FIFO, UART delivery,
nor the UAV command application is the primary loss boundary.

The native return trace identifies the selected upstream model's boundary. During the
diagnostic there were 509 UAV-to-GCS `MacTx` packets but only 156 CP `RxStart`
candidates; 353 signals arrived while the CP was already RX/TX and therefore had no
second public `RxEndOk`/`RxEndError` outcome. This is the documented behavior of
`HalfDuplexIdealPhy::StartRx`: in `RX` or `TX` it adds interference but does not begin
another receive candidate. Concurrent command replies therefore compete in an
unacknowledged ALOHA medium; missing return candidates and UAV `RxAbort` dominate the
failure. The zero queue drops and low queue residence exclude a FIFO backlog as the
multi-second cause. At 10 Mbit/s the same collision boundary remains and genuine PHY
errors increase to 28, so rate alone is not a remedy.

The observed multi-second effect is at the real framed GCS transport boundary, not an
invented command timestamp. In Case B2 the affected control paths had 70--72 sequence
gaps/reassembly failures each; maximum ingress-record ages were 4,771.084 ms (UAV2),
4,019.394 ms (UAV4) and 3,145.550 ms (UAV5). `serial_transport.Reassembler` correctly
holds later complete records until a missing sequence expires, then records the gap; it
does not retransmit or create an ACK. The physical no-candidate losses above therefore
propagate into these measured multi-second application-facing waits.

`control-latency-E-20260901T008000Z` repeated Case C in `metrics_only` mode. It again
received 78 ACKs from 115 attempts and 13/50 parallel ACKs, while native event rows
dropped from 11,998 to 5,163. Its RTT p95 changed from 821.383 to 631.950 ms, so
latency percentiles are treated as workload-sensitive; the invariant ACK-loss outcome
rules out per-packet tracing as the root cause. Batched mode flushes at 256 events or
25 ms and never flushes per packet.

Decision: `generic_native_spectrum_aloha_reference` is unsuitable as a reliable
five-UAV MAVLink control plane under this offered parallel load. This is a valid
negative result, not a reason to add a custom provider, synthetic ACK, custom PER,
scheduler, retry daemon, database, dashboard or telemetry framework. A full flight was
intentionally not run, because no project-side bug or retry/stream-configuration fix
was established; consequently no new full-flight screenshots are claimed. RSSI, SNR,
SINR and BLER retain the availability contract above: RSSI/SNR/SINR are unavailable in
the selected public native API and BLER is unavailable for this PHY abstraction.

## Native Wi-Fi GATE 1 blocker

Run: `runs/native-radio-wifi/gate1-20260901T000000Z` on exact ns-3.48 revision
`d2add90b452d600cfb4859baed8e9ea633519447`, in the pinned product container. The
unchanged official `wifi-spectrum-per-example` built and completed with
`SpectrumWifiPhy`, 741 received packets, signal `-79.75 dBm`, noise `-93.97 dBm`,
and signal-minus-noise `14.22 dB`.

The required unchanged official `sionna-rt-channel-example` built and ran its two
scheduled computations (`34.49 dB/-87.96 dBm` and `27.68 dB/-94.77 dBm` SNR/Rx
power), printed its success message, then died with `SIGABRT`: `PyThreadState_Get:
the function must be called with the GIL held` while Python was finalizing. Therefore
GATE 1 is failed, `sionna_spectrumwifi_compatibility` is `blocked`, and no minimal
Wi-Fi spike or product topology was created. No Wi-Fi MAC, PHY, error, propagation,
antenna, TAP, UART, or scenario code was changed.
