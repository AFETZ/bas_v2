# Native Wi-Fi/Sionna five-UAV Town01 product v1

## Outcome

`runs/native-radio-realtime/native-wifi-five-product-final-v3-20260904` completed
with exit 0, functional status `passed`, realtime status `ready`, and all 15 product
checks true. `native-wifi-five-product-final-v2-20260904` independently repeated the
same verdict. Compact versioned evidence is in
`network/ns3/evidence/native_wifi_80211n_spectrum_product_v1.json`.

The runtime used five real ArduPilot SITLs and Gazebo UAVs, five live ROS odometry
publishers, ten independent SERIAL1/SERIAL2 BSF1 paths, six ns-3 Wi-Fi PHY/MAC pairs,
one 802.11n QoS infrastructure BSS, one shared `MultiModelSpectrumChannel`, and the
in-process `SionnaRtSpectrumPropagationLossModel`. No scalar propagation fallback,
custom packet scheduler, custom packet-error model, or application bypass was in the
native process.

## Functional result

- Five sequential real-SITL control ACK latencies were 5.941, 4.468, 3.653, 3.720,
  and 32.376 ms (p95 27.089 ms). Payload ACK p95 was 7.131 ms.
- P2P delivered all 100 unique bidirectional messages, ten each way per UAV.
- P2MP used exactly 20 command-post root transmissions and no application unicast
  copies; all five receivers delivered all 20 roots.
- Simultaneous uplink delivered 20/20 for every UAV; Jain fairness was 1.0.
- All five vehicles armed, took off, held, landed, and auto-disarmed. UAV1 traversed
  the declared LOS and obstructed Town01 points.
- Six hash-locked raw plus annotated live Gazebo frames passed position, projection,
  freshness, and spatial-state checks.
- After the combined ns-3/Sionna process stopped, the 10.5-second probe observed no
  control/payload response or reverse telemetry for any UAV.

## Realtime result and root cause

The original fixed 10 s / 5 m channel cache was not repeatable: two complete runs
measured steady scheduler-lag p95 2.754 and 162.386 ms. Profiling showed periodic
clusters of up to six synchronous `CalculatePaths` calls at essentially one ns-3
simulation timestamp. A typical solve was about 30 ms, so clustered cache expiry,
not Wi-Fi queue volume, accumulated 180–260 ms scheduler backlog.

The cache now uses deterministic per-pair expiry spread, configured in the single
product radio YAML. The declared maxima are 20 s / 10 m with fraction 0.5, producing
per-pair thresholds in 10–20 s / 5–10 m: no state exceeds the maxima, while pairs no
longer expire as one burst. The two final runs measured steady lag p95 0.084 and
0.105 ms. The primary run had 517 path solves (2.617/s), solve p95 34.552 ms,
1,956 applied live-pose snapshots, and zero stale-pose samples.

Primary Gazebo RTF mean/p5 was 0.994559/0.999134. Native ns-3/Sionna CPU mean/p95
was 13.787/23.671% of one core, RSS/GPU tracking remained enabled, and peak GPU
memory was 2.739 GB. The primary run's maximum steady lag was 105.783 ms; the repeat
had one 1.067-second Sionna PathSolver outlier and 802.494 ms maximum lag, but p95,
RTF, ACKs, drain, and functional checks remained within bounds.

## Configuration and build

All product radio and Sionna parameters are in
`network/config/native_wifi_80211n_spectrum_product.yaml`; the runner reads and
passes them into the C++ target, which records the resolved values. The target was
rebuilt from exact ns-3 SHA `d2add90b452d600cfb4859baed8e9ea633519447` through
`1008/1008` and linked `scratch_upstream-sionna-tap-spike` before the final runs.

## Remaining limits

Hardware HitL is not covered. The selected public Wi-Fi traces do not export
per-MPDU RSSI, SNR, SINR, interference power, or BLER, so those values remain
explicitly unavailable. The measured 20 s / 10 m cache envelope is not a scalability
matrix. Obstruction caused one recoverable UAV1 deassociation, but directed jammer
behavior is not characterized.
