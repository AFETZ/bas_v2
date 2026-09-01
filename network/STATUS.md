# Product Status

Updated: 2026-09-01. A status is supported only by the named runtime artifact;
an implemented path or a validator result is not a readiness claim.

| Stage | Status | Evidence and current boundary | Next one task |
| --- | --- | --- | --- |
| P0 Process reset | done | Product-first rules and changed-path checks are present. | Maintain only as needed. |
| P1 Five-UAV baseline | done | Five live SITLs, Gazebo models and ROS odometry completed the lifecycle in `runs/town01-full-20260827T064100Z`. | Preserve this baseline. |
| P2 Communication vertical slice | done | Five independent dual-UART paths, endpoint diagnostics, native P2P/P2MP and stop proof passed in `runs/town01-communication-20260827T120000Z`. | Preserve the communication plane. |
| P3 Shared 10 km scene | deferred | Town01 remains the scoped development scene. | Scope the expansion separately. |
| P4 Interference and medium access | blocked | ALOHA remains a comparative-only negative reference: the RTF-1 gate found 13/50 parallel ACKs. The native Wi-Fi attempt stopped at GATE 1 because the required official Sionna example aborts during Python finalization after two SNR samples, although the official SpectrumWifiPhy example passed. | Diagnose the official Sionna example shutdown blocker without changing Wi-Fi MAC/PHY or adding a fallback. |
| P5 HitL and real time | limited | Cache-backed RTF-1 path-solve/full-channel p95 remained 30.640/30.845 ms in `native-realtime-five-20260831T233000Z`, but the new stationary gate proves the control limit before flight: RTT p95 was 726.256 ms at five UAVs/control-only and 821.383 ms with both UARTs. No full flight was run after the negative gate, so no lifecycle screenshot set is claimed. Hardware HitL is unproved. | Do not schedule another generic-ALOHA full flight; validate a separately scoped control-plane replacement, including its required live Gazebo screenshots. |
| P6 Scalability and hybrid propagation | todo | This first cache-backed operating-envelope sample is not a capacity matrix. | Measure the declared age/position-threshold matrix after P5 succeeds. |
| P7 Integrated demo | in_progress | The lifecycle orchestrator exists, but P5 remains limited and P3/P6 are unfinished. | Do not present it as a customer-ready integrated demo. |

## Native generic-metric contract

`generic_native_spectrum_aloha_reference` reports only measurements the selected
upstream stack exposes. Public received PSD may yield Rx power/RSSI; the current
`HalfDuplexIdealPhy` path does not expose it, so RSSI is `unavailable`. Native
SNR/SINR is reported only if the upstream interference model exposes it; it is
currently `unavailable`. `RxEndOk` and `RxEndError` support empirical PER. BLER is
`unavailable`: this HalfDuplexIdealPhy/Shannon reference has no user-facing
transport-block abstraction. Missing quantities are never synthesized.

## Native Wi-Fi reference attempt

`native_wifi_80211n_spectrum_reference_v1` is not created. On exact upstream
`d2add90b452d600cfb4859baed8e9ea633519447`, the unchanged official
`wifi-spectrum-per-example` with `SpectrumWifiPhy` completed with 741 received
packets, signal/noise `-79.75/-93.97 dBm` and 14.22 dB difference. The required
unchanged official `sionna-rt-channel-example` computed two SNR samples but then
aborted with `PyThreadState_Get` while Python was finalizing. Per GATE 1, no
Sionna+SpectrumWifiPhy spike, Wi-Fi BSS/TAP runtime, UART change, or product flight
was attempted. Jammer and HitL remain pending.

The previous functional reference run
`native-five-final-20260901T030000Z` used RTF 0.1 and remains evidence of functional
behavior only, not RTF-1 readiness. The old custom provider/PER/scheduler contour is
comparative only and not the primary process.
