# Upstream radio integration decision

## Decision

Принять ns-3 MR !2608 exact revision
`3d0643e7858edcf22da3deebb0d2e423ecfe2961` (ns-3 `3-dev`) как единственную
propagation implementation минимального spike. Не переносить project-specific
provider/client/cache/error model в новый путь.

Это единственный кандидат, где official example реально выполнил Sionna RT 1.2.0
in-process и `SionnaRtSpectrumPropagationLossModel`, передающий complex channel matrix,
delays, angles и Doppler прямо в ns-3 Spectrum API без stock-propagation fallback.

## What was actually built and run

| Candidate | Build | Official example |
| --- | --- | --- |
| MR !2608 | Full 2007/2007 PASS | Unmodified `sionna-rt-channel-example` PASS, average SNR 29.26 dB |
| ns3-rt `ac4ede...` | Full 1559/1559 PASS | Unmodified server + `simple-sionna-example` PASS |
| VaN3Twin `0e70bb...` | Builder/configure PASS; compile FAIL on `ns3/los_nlos.h` export | Blocked before executable; core was not locally replaced |
| GazeboNS3 `db0be...` | Official Docker FAIL at OSRF/Gazebo dependency; CMake FAIL at MAVSDK | Blocked before executable |
| Clean ns-3.40 baseline | Configure and target build PASS | Unmodified `adhoc-aloha-ideal-phy` PASS |

## Selected native pipeline

- Channel: upstream `SionnaRtChannelModel` +
  `SionnaRtSpectrumPropagationLossModel` + `MultiModelSpectrumChannel`.
- PHY: native `HalfDuplexIdealPhy` with its native Shannon error decision.
- MAC: native `AlohaNoackNetDevice`; no project scheduler or packet simulator.
- Boundary: standard ns-3 `TapBridge` in realtime mode, using existing `tap-gcs` and
  `tap-uav` namespace topology.

## Reuse and thin changes

Unchanged upstream algorithms: channel matrix/path solve, Spectrum propagation, PHY error
decision, MAC queue/transmit, TapBridge. Unchanged project components: Town01 XML/PLY,
ROS position tracker, one-UAV netns/TAP setup and UDP/PCAP observation.

Recorded compatibility patch changes only:

1. custom XML loading from unsupported `Scene(..., merge_shapes=...)` to public Sionna
   1.2 `load_scene(path, merge_shapes=...)`;
2. the ideal PHY antenna handle from `AntennaModel` to `Object`, matching the public
   `SpectrumPhy::GetAntenna()` API so the upstream phased-array model is reachable.

The scratch composition adds no reception formula. Its fixed neighbor entry maps the
existing namespace endpoint MAC and avoids an upstream ideal-PHY ARP receive-callback
reentrancy assertion; it does not bypass the radio. Tracker freshness uses wall clock
because synchronous ray tracing can make realtime ns-3 catch up simulation events.

## Components leaving the primary target path

`network/radio_provider/provider.py`, TCP/JSON, JSONL loss probability,
`abstract_service_tier_v1`, `AmsStockSionnaPacketErrorModel`, custom random PER and
`centralized_priority_scheduler` remain comparative baselines only and are absent from the
spike process/files/PCAP.

## Rejections and exact blockers

- ns3-rt: scalar-only socket integration, silent stock fallback, old invasive fork and
  TensorFlow compatibility risk.
- VaN3Twin: unpinned nested heads; exact build misses exported `ns3/los_nlos.h`; inherited
  scalar fallback; exact Doppler script disables Doppler after a kernel crash.
- GazeboNS3: no reuse license, Sionna or TAP; official build dependencies are broken.
- Existing custom path: indirect scalar policy decides packets instead of native Spectrum.

## Remaining blockers

Before any five-UAV adoption: upstream/maintain the two compatibility fixes; replace the
spike-only fixed neighbor entry with a native-PHY-safe discovery solution; pin/install
Python ABI dependencies reproducibly; benchmark synchronous Sionna update cost; choose a
technology-specific native Spectrum PHY/MAC if required. Five UAVs, jammer, QGC and HitL
were intentionally not implemented or connected here.
