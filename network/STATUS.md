# Product Status

Updated: 2026-09-05. Current branch: release/bas-v2-rc1.
Original native-radio-wifi worktree and its user edits were preserved.

software_release_status=verified (local software RC, measured reference envelope)
full_tz_status=blocked_external
hardware_validation_status=blocked_external

| Requirement | Status | Actual verification |
| --- | --- | --- |
| R1 five SITL/Gazebo | verified | customer-final-01/02: flight, LAND, auto-disarm, real odometry and cleanup |
| R2 UART/P2P/P2MP | verified | Ten UART; 50/50 parallel one-shot; P2P 100/100; P2MP 20 roots/100 deliveries; native stop proof |
| R3 native radio | verified | ns-3.48 Wi-Fi PHY/MAC/ARQ/queues and in-process Sionna, no bypass/custom PER |
| R4 sources | verified | Eight WaveformGenerator cases, packet impact, pulse/sweep/orientation/multiple/non-overlap, two integrated on/off runs |
| R5 observability | verified | Native S/N and predecode power; derived RSSI/J/S, application PDR/goodput/IPDV, queue/airtime, raw Gazebo, radiotap and map point comparison |
| R6 external FC | blocked_external | Serial/UDP/TCP software tests, real SITL UART gateway/MAVProxy ACK; no physical FC |
| R7 timing/cache/scale | verified | 1/5 SITL, 16 radio-only STA, three cache profiles, explicit native Friis/hybrid studies |
| R8 shared scene | verified | 10×10 km field/Town01, 188.791 m terrain relief, explicit 15-storey addition, shared meshes |
| R9 operator | verified | Existing MAVProxy, SYSID selection and commands through modeled path; Makefile/CLI |
| R10 delivery | verified | Two full runs, pinned source/image/dependencies, local package and clean restored bootstrap |

## Final runtime measurements

runs/native-radio-realtime/rc1-customer-final-01 and ...-02 passed all 20 gates.
Both: five flight/LAND/auto-disarm, ten UART, P2P/P2MP/shared delivery and 10.5 s no-bypass.
Steady ns-3 lag p95/max: 0.336842/58.01054 ms and 16.076663/108.432918 ms.
Gazebo RTF mean: .996636/.995554; cold start reported separately.
Actual sampled channel age p95: 13.928/13.870 s; maximum 19.880 s.
Runtime HEADs: 547a536 and 2fb157b; later report-only additions retain raw data.
Focused tests: 40 passed; latest reporting tests: 21 passed.

## Limits that remain

- No physical FC or ttyUSB/ttyACM/serial-by-id device. PTY is software validation,
  not hardware or flight-HIL. Needed: FC, serial/COM or Ethernet access and safe bench.
- Cache 20 s/10 m delayed path disappearance by 1 s and missed a 1 s recovery;
  four no-path mismatches remain visible. Do not infer accuracy from low scheduler lag.
- 16 STA test is radio-only; its 11.63 wall s / 8 sim s does not establish 16-SITL real time.
- At 10 dBm, 500/1000/2000 m native reference links delivered 0/100; no power retuning.
- RSSI/J/S energy sums use native arrivals plus configured thermal floor. Decoder S/N
  has decoded-frame sampling bias. Airtime is not CCA busy-state fraction. Application
  PDR and PHY decoder PER have different denominators. Unattributable values stay null.
- Terrain/tower are synthetic; original Town01 collision proxies are approximate.
  CAVISE third-party redistribution terms are not supplied. Assets stay local.
- Software RC is ready for repeatable bench review, not a hard-RT guarantee or full-TZ claim.

## Delivery and commands

Package: /home/bas/bas_v2-delivery/rc1-2026-09-05-final
Archive readability and source bundle verification passed. A separate checkout restored
source/dependencies/scenes, built its ROS workspace, and passed bootstrap with 0 errors/warnings.
Source bundle is refreshed to the final delivery commit; runtime image remains pinned.
Publishing target: release/bas-v2-rc1 → native-radio-wifi, draft review; no main merge.

make demo-preflight DEMO_GUI=0 DEMO_BOOTSTRAP=1
make prepare-customer
BAS_NATIVE_FIVE_RUN_ID=my-demo BAS_NATIVE_SOURCES=network/config/native_jammers_town01.yaml make demo-customer DEMO_GUI=0
make operator DEMO_GUI=0
make gcs
make stop

Use doc/USER_GUIDE.md for endpoints, maps/cache/matrix and troubleshooting.
Use doc/VALIDATION_REPORT.md and doc/DELIVERY_SCOPE.md for scopes and failed diagnostics.
Next hardware step: connect an authorized safe FC bench and run REQUEST_MESSAGE through
the same gateway; retain hardware_validation_status=blocked_external until actually tested.
