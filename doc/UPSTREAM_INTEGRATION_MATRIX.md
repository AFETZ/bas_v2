# Upstream integration matrix

Проверка выполнена 2026-08-31 до изменения product runtime. Все checkout находятся в
`.external/upstream_integrations/`; ветка `product-first-workflow` не изменялась. Даты —
timestamps exact commits, а статусы — фактически выполненные команды, не README claims.

## Ревизии и официальные запуски

| Кандидат | Exact source / последний update | Лицензия / версии | Фактически выполненный официальный путь |
| --- | --- | --- | --- |
| [ns-3 MR !2608](https://gitlab.com/nsnam/ns-3-dev/-/merge_requests/2608) / [AAshtari branch](https://gitlab.com/AAshtari/ns-3-dev/-/tree/SionnaChannelModelIntegration) | `ns3-mr2608`, `3d0643e7858edcf22da3deebb0d2e423ecfe2961`, 2026-05-14; MR ref и branch совпадают | GPL-2.0-only; ns-3 `3-dev`; documented/tested `sionna==1.2.0`, `sionna-rt==1.2.0` | `./ns3 configure --enable-examples --enable-tests --enable-python-bindings`; полный `./ns3 build` 2007/2007 PASS; неизменённый `./ns3 run sionna-rt-channel-example` PASS: 2 SNR samples, average 29.26 dB. |
| [robpegurri/ns3-rt](https://github.com/robpegurri/ns3-rt) | `ns3-rt`, `ac4ede85ef7c3d1852c8bb72cbe1c37734068000`, 2025-05-30 | GPL-2.0; fork около ns-3.40-dev; Sionna RT >=1.0.1, в run 1.2.0 | `./ns3 configure --disable-python --enable-examples`; полный build 1559/1559 PASS; неизменённые upstream server и `./ns3 run simple-sionna-example` PASS. TensorFlow 2.21 падал; TensorFlow 2.15.1 прошёл. |
| [DriveX-devs/VaN3Twin](https://github.com/DriveX-devs/VaN3Twin) | `VaN3Twin`, `0e70bb3650685b68306dc1436e70c8ef91b99bf1`, 2026-08-02; builder взял tag `ns-3-dev-v2x-v0.2`=`80b8e3109c0f654bea332d63676e21b03a271758` и незакреплённый NR head=`321566011c1a49ed8e722c3edc9a659604fe1dda` | GPL-2.0; ns-3.36.1 lineage; заявлены Sionna 0.19.0/1.0 | Неизменённый `sandbox_builder.sh` PASS. С optional CARLA disabled documented configure PASS; `./ns3 build` FAIL на отсутствующем exported `ns3/los_nlos.h`. Официальный 802.11p/NR/Sionna example поэтому не исполним без upstream patch. |
| [TomWang233/GazeboNS3](https://github.com/TomWang233/GazeboNS3) | `GazeboNS3`, `db0be75295086e41ce32adefe0f34c9742791d7e`, 2025-05-24 | В exact revision нет LICENSE/SPDX; ns-3 и Sionna не закреплены, Sionna отсутствует | Официальный `docker build -t new-gazebo .` FAIL на недоступном HTTP OSRF repo и отсутствующем `gz-harmonic`; отдельный CMake configure FAIL на недокументированном MAVSDK. `docker compose up`/example не мог стартовать. |
| Существующие assets | project `f12db444af9a50d5dd767dfc8f076d018a34aa05`, 2026-08-29; ns-3.40 archive SHA-256 `c0ba395b6fcb084c4d43d6117b28932f716b26aebb54498ce2f44c0c39be3e60`; Town01 XML `497a543b5a6ce85e0e5b48b1021111a4b6850ad66beea0a635144d0da7e606b2` | Project license + GPL-2.0 ns-3.40; external Sionna 1.2.0 path | Чистый official ns-allinone-3.40 archive: configure PASS, target build PASS, неизменённый `adhoc-aloha-ideal-phy` PASS. Это Spectrum PHY/MAC baseline, не direct Sionna integration. |

## Проверенные архитектурные свойства

| Свойство | MR !2608 | ns3-rt | VaN3Twin | GazeboNS3 | Existing assets |
| --- | --- | --- | --- | --- | --- |
| Sionna architecture | In-process Python через pybind11 | Отдельный Python UDP server, local/remote | Отдельный Python UDP server, local/remote | Нет Sionna | Project Python service + custom adapter |
| Ray-tracer output | Complex matrices, delays, AoA/AoD, Doppler | Scalar gain, delay, LOS | Scalar gain/delay/LOS; velocity в protocol | Нет | Scalar per-link state |
| ns-3 mapping | `PhasedArraySpectrumPropagationLossModel` -> native `SpectrumChannel` | Generic scalar `PropagationLossModel`/delay; zero silently uses stock result | Scalar model; 802.11p Yans и отдельный NR path | GPS -> mobility -> stock LogDistance | External state -> custom packet policy/CSMA surrogate |
| Native PHY/MAC | Official example только PSD; public API совместим со Spectrum PHY | Official example LTE/EPC | 802.11p OCB и NR-V2X present, build blocked | YansWifiPhy + AdhocWifiMac | Stock ALOHA/half-duplex PASS; product path CSMA |
| Dynamic mobility / links | `MobilityModel` per cache update; many pair keys | Position/velocity per query; many links | SUMO/CARLA mobility; many links | MAVSDK GPS polling; per-vehicle threads | ROS odometry tracker; five links |
| Interference | Native overlapping `SpectrumInterference` | Native radio after scalar gain | Native radio after scalar gain | Stock Wi-Fi | Shared CSMA contention, not RF Spectrum |
| Directional / Doppler | Phased arrays + patterns / yes | No / no | Orientation absent; Doppler variant present but disabled in exact script after kernel crash | No / stock only | Omni / no direct Spectrum Doppler |
| Realtime | ns-3 realtime core builds; Sionna synchronous | Realtime core builds; separate service | Options/code present; runtime blocked by build | Realtime simulator selected | Existing realtime TapBridge runtime |
| TAP / Emu | Standard TapBridge/Fd modules build; public composition proved by spike | Modules build, official Sionna example does not use them | Modules configure ON; example blocked | No TAP/Emu | Existing one-/five-UAV netns/TAP + PCAP |
| P2P / P2MP | Yes / yes through SpectrumChannel | Yes / yes | Yes / yes | Yes / yes | Yes / yes |
| Five-node scalability | API/cache supports it; not benchmarked here | API supports it; server serial query cost unmeasured | Fleet design claims it; exact run blocked | Multi-process design, no successful build | Existing five-node orchestration, not connected in this task |
| External serial/Ethernet | Standard TAP/Emu can carry it; UDP/TAP proved | Possible via standard modules, not example-proved | Emulation code separate from blocked Sionna path | Internal queues only | Ten UART bridges and Ethernet TAP retained unchanged |
| Fail-closed on Sionna failure | In-process exception/abort; no stock propagation fallback | No: scalar zero selects stock ns-3 result | No: inherited scalar fallback | N/A | Custom path has policy; forbidden in spike |
| Required modification | Sionna 1.2 XML loader compatibility; phased-array handle; thin TAP/tracker scratch | Remove fallback and prove packet/TAP lifecycle | Fix header export, pin nested revisions, remove fallback | Repair dependencies, add license and Sionna/TAP | Replace primary custom outcome mapping |
| Blocking limitation | Open MR, synchronous Python/JIT cost, upstream PHY lacks direct phased-array setter | Old invasive fork, scalar only, unpinned TensorFlow, silent fallback | Official exact build broken; Doppler disabled | No license, broken build, no Sionna/TAP | Current primary path is indirect JSON/PER |

## Phase A decision

MR !2608 выбран как единственный фактически запущенный direct
Sionna -> phased Spectrum path без scalar policy/fallback. `ns3-rt` оставлен reference для
service lifecycle, VaN3Twin — для mobility/V2X patterns, GazeboNS3 отклонён. Из проекта
переиспользуются Town01, ROS tracker и TAP/netns, но не propagation/outcome logic.
