# Product Status

Updated: 2026-08-31. A status is supported only by the named runtime artifact;
an implemented path or a validator result is not a readiness claim.

| Stage | Status | Evidence and current boundary | Next one task |
| --- | --- | --- | --- |
| P0 Process reset | done | Product-first rules and changed-path checks are present. | Maintain only as needed. |
| P1 Five-UAV baseline | done | Five live SITLs, Gazebo models and ROS odometry completed the lifecycle in `runs/town01-full-20260827T064100Z`. | Preserve this baseline. |
| P2 Communication vertical slice | done | Five independent dual-UART paths, endpoint diagnostics, native P2P/P2MP and stop proof passed in `runs/town01-communication-20260827T120000Z`. | Preserve the communication plane. |
| P3 Shared 10 km scene | deferred | Town01 remains the scoped development scene. | Scope the expansion separately. |
| P4 Interference and medium access | in_progress | The primary generic path is one shared `MultiModelSpectrumChannel -> SionnaRtSpectrumPropagationLossModel -> HalfDuplexIdealPhy/ShannonSpectrumErrorModel -> AlohaNoackNetDevice -> TapBridge`, with five live UAV endpoints. It has no custom provider, error model, or scheduler. | Resolve the real Aloha/MAVLink control-latency limit at RTF 1 before declaring a five-UAV functional result. |
| P5 HitL and real time | limited | In `native-realtime-five-20260831T233000Z`, reusable native Sionna scene state reduced steady path-solve/full-channel p95 to 30.640/30.845 ms and Gazebo RTF p5 was 0.99931. The run was manually stopped after control traffic stalled, so the functional prerequisite and all required live screenshots are not complete. Hardware HitL is unproved. | Complete one uninterrupted RTF-1 five-UAV run with all functional gates and six live Gazebo screenshots. |
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

The previous functional reference run
`native-five-final-20260901T030000Z` used RTF 0.1 and remains evidence of functional
behavior only, not RTF-1 readiness. The old custom provider/PER/scheduler contour is
comparative only and not the primary process.
