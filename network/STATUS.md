# Product Status

Updated: 2026-09-04. A status is supported only by the named runtime artifact;
an implemented path or a validator result is not a readiness claim.

| Stage | Status | Evidence and current boundary | Next one task |
| --- | --- | --- | --- |
| P0 Process reset | done | Product-first rules and changed-path checks are present. | Maintain only as needed. |
| P1 Five-UAV baseline | done | Five live SITLs, Gazebo models and ROS odometry completed the lifecycle in `runs/town01-full-20260827T064100Z`. | Preserve this baseline. |
| P2 Communication vertical slice | done | Five independent dual-UART paths, endpoint diagnostics, native P2P/P2MP and stop proof passed in `runs/town01-communication-20260827T120000Z`. | Preserve the communication plane. |
| P3 Shared 10 km scene | deferred | Town01 remains the scoped development scene. | Scope the expansion separately. |
| P4 Interference and medium access | in_progress | The versioned `native_wifi_80211n_spectrum_reference_v1` standalone gate passed: five 802.11n QoS STAs returned 50/50 control responses through `SpectrumWifiPhy` plus in-process Sionna, with 15.304 ms RTT p95, 0.474 ms steady scheduler-lag p95, Jain fairness 1.0, RTF 1.0 and zero MAC drops. The prior ALOHA result remains a comparative-only negative reference. | Put the proven Wi-Fi/Sionna chain behind the existing TAP/UART boundary and run real five-SITL control plus no-bypass proof before a flight claim. |
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

## Native Wi-Fi reference

The GATE 1 shutdown blocker is closed on exact upstream
`d2add90b452d600cfb4859baed8e9ea633519447`. GDB identified the abort as the cached
Sionna scene's `py::object` attempting `Py_DECREF` from `DoDispose()` after the local
`py::scoped_interpreter` had finalized Python; the official example keeps its loss
model in a static `Ptr` that outlives that interpreter. Disposal now clears the scene
normally only while Python and its GIL are available and otherwise detaches the dead
handle during process teardown. The unchanged official example again computed two SNR
samples and exited 0. The unchanged official `wifi-spectrum-per-example` retained 741
received packets, signal/noise `-79.75/-93.97 dBm` and 14.22 dB difference.

`runs/native-wifi-five-uav-verification-20260904` then passed the reproducible
`network/ns3/run_sionna_wifi_five_uav.sh` gate. A standard 802.11n infrastructure BSS
with QoS carried ten simultaneous request/response rounds for each of five UAV-side
STAs. `MultiModelSpectrumChannel` contained only
`SionnaRtSpectrumPropagationLossModel`; no scalar propagation model or application
bypass was present. All UAVs returned 10/10 responses: total PDR 1.0, RTT p95
15.304 ms, steady scheduler-lag p95 0.474 ms, Jain fairness 1.0, mean RTF 1.0 and
zero MAC drops. The run also observed 38 native Wi-Fi data-retry events and 21,636
ns-3 events (432.72 per returned logical packet). Versioned evidence is in
`network/ns3/evidence/native_wifi_80211n_spectrum_reference_v1.json`.

This is a bounded standalone ns-3 control reference using the built-in Sionna scene,
not a Gazebo/Town01/SITL flight proof. Its 12-second association/cache warm-up had
279.749 ms scheduler-lag p95, which is reported separately from the traffic interval.
It does not yet prove the ten UART paths, real SITL MAVLink ACKs, P2P/P2MP, moving
Town01 positions, Gazebo RTF, jammer behavior, or stop-based no-bypass at the TAP
boundary. P5 therefore remains limited.

The previous functional reference run
`native-five-final-20260901T030000Z` used RTF 0.1 and remains evidence of functional
behavior only, not RTF-1 readiness. The old custom provider/PER/scheduler contour is
comparative only and not the primary process.
