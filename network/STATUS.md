# Product Status

Updated: 2026-09-04. A status is supported only by the named runtime artifact;
an implemented path or a validator result is not a readiness claim.

| Stage | Status | Evidence and current boundary | Next one task |
| --- | --- | --- | --- |
| P0 Process reset | done | Product-first rules and changed-path checks are present. | Maintain only as needed. |
| P1 Five-UAV baseline | done | Five live SITLs, Gazebo models and ROS odometry completed the lifecycle in `runs/town01-full-20260827T064100Z`. | Preserve this baseline. |
| P2 Communication vertical slice | done | Five independent dual-UART paths, endpoint diagnostics, native P2P/P2MP and stop proof passed in `runs/town01-communication-20260827T120000Z`. | Preserve the communication plane. |
| P3 Shared 10 km scene | deferred | Town01 remains the scoped development scene. | Scope the expansion separately. |
| P4 Interference and medium access | done | `runs/town01-qos-final-20260904` passed bounded overload; `native-wifi-five-product-final-v3-20260904` passed the existing TAP/UART boundary with a native 802.11n QoS `SpectrumWifiPhy` BSS and in-process Sionna. | Preserve the versioned QoS and native-Wi-Fi contracts. |
| P5 HitL and real time | software_ready_hitl_limited | Two full five-SITL Town01 Wi-Fi/Sionna runs passed at RTF 1. Primary steady scheduler-lag p95 was 0.084 ms and Gazebo mean RTF was 0.994559; hardware HitL is still unproved. | Scope hardware HitL separately. |
| P6 Scalability and hybrid propagation | todo | This first cache-backed operating-envelope sample is not a capacity matrix. | Measure the declared age/position-threshold matrix after P5 succeeds. |
| P7 Integrated demo | scoped_ready | The current five-UAV Town01 scope passed 15/15 functional checks, flight lifecycle, ten UART paths, P2P/P2MP, live screenshots, realtime and stop-based no-bypass. P3/P6 expansion is not claimed. | Do not begin another product feature under this closure. |

## Bounded overload

The audited accounting fix is commit `26fa99c`; the control-protection implementation
is commit `28c52f5` and its hardening is `dbe1046`. The exact old scheduler backlog
was unbounded offered traffic entering ingress/Sionna/queue lifecycle work before
admission. The legacy backoff argument mismatch allowed 45,927 rich retry rows, and
every callback reparsed and hashed the frame, serialized about 2 KiB JSON, and
synchronously flushed it. BSF1 was one fragment per record and was not causal.

Controlled overload offered 33.8048 Mbit/s and admitted 12.8608 Mbit/s. Control PDR
was 1.0 with 1.335 ms p95 latency; lower classes delivered 12.637333 Mbit/s total,
scheduler-lag p95 was 6.125 ms, Gazebo mean RTF was 1.000414, and pending was zero.
All 18,600 packet IDs have terminal accounting. Before/after lag was
7,594.223/6.125 ms, events per delivered logical packet 12.8266/7.5254, and pending
15,013/0. The shaping-disabled meltdown point offered/admitted 33.8048 Mbit/s and is
characterization-only, not pass/fail or a saturation sweep. Compact evidence is in
`metrics/controlled_overload_summary.json`, `metrics/meltdown_characterization.json`,
`metrics/event_profile.json`, and `doc/results/town01_bounded_overload_v2.md`.

## Native Wi-Fi/Sionna Town01 product path

The exact upstream base is `d2add90b452d600cfb4859baed8e9ea633519447`. The product
runtime uses six native Wi-Fi PHY/MAC pairs, one shared `MultiModelSpectrumChannel`,
and only `SionnaRtSpectrumPropagationLossModel`; there is no scalar propagation
fallback or application bypass. Five real SITLs and Gazebo vehicles passed lifecycle,
real control/payload ACKs, ten SERIAL1/SERIAL2 paths, 100/100 P2P deliveries, one-root
P2MP with 100 receiver deliveries, and simultaneous uplink Jain fairness 1.0.

The initial fixed cache was not repeatable because several synchronous
`CalculatePaths` calls expired at the same simulation timestamp, creating 180–260 ms
backlog. Deterministic per-pair threshold spread with configured maxima 20 s/10 m
removed the burst. Final repeat lag p95 values were 0.084 and 0.105 ms. The primary
run applied 1,956 live-pose snapshots, had zero stale samples, and passed the 10.5 s
stop-based no-bypass test. Versioned evidence and details are in
`network/ns3/evidence/native_wifi_80211n_spectrum_product_v1.json` and
`doc/results/native_wifi_five_uav_product_v1.md`.

## Current boundary

Hardware HitL, jammer campaigns, and a cache/scalability matrix remain outside this
result. The selected Wi-Fi traces do not export per-MPDU RSSI, SNR, SINR,
interference power, or BLER; unavailable values are not synthesized. The 20 s/10 m
cache settings are the measured envelope, and meltdown is only one tested point.
Logical pending is proved zero after drain; physical queue emptiness at that exact
accounting cutoff was not independently sampled.
