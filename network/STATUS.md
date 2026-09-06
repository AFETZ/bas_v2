# Product Status

Updated: 2026-09-06. RC1: release/bas-v2-rc1; handover: feature/bas-v2-rc1-handover.
Original native-radio-wifi worktree and its user edits were preserved.

software_release_status=limited (restored flight completed; 19/20 gates, timing failed)
independent_acceptance=pending
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
| R7 timing/cache/scale | limited | Original studies retained; restored steady lag p95 57.761 ms exceeds 50 ms |
| R8 shared scene | verified | 10×10 km field/Town01, 188.791 m terrain relief, explicit 15-storey addition, shared meshes |
| R9 operator | verified | Existing MAVProxy, SYSID selection and commands through modeled path; Makefile/CLI |
| R10 delivery | limited | Bundle/image/assets restored without hidden checkout inputs; new full run 19/20 gates |

## Final runtime measurements

runs/native-radio-realtime/rc1-customer-final-01 and ...-02 passed all 20 gates.
Both: five flight/LAND/auto-disarm, ten UART, P2P/P2MP/shared delivery and 10.5 s no-bypass.
Steady ns-3 lag p95/max: 0.336842/58.01054 ms and 16.076663/108.432918 ms.
Gazebo RTF mean from packaged summary JSON: .996689/.996043; cold start separate.
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
Archive inventory and bundle checks passed; reused package-origin restore at
/home/bas/bas_v2-restore-check, exact clean 263dfd5b494a3471038af7e79687fd20ae482cfb.
Reloaded packaged image 89d78eff9914; preflight 0 errors/warnings; regenerated scenes.
752 scene references resolve inside restore mount; Python pins use packaged target.
Both source aliases bind the restore; no old checkout supplies runtime inputs.
Original source/image/archive/video artifacts and release/demo refs remain unchanged.
Handover: feature/bas-v2-rc1-handover; no main merge or release tag changes.

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

## Recorded internal demonstration C (separate from original tests A and restore B)

Package: /home/bas/bas_v2-demo/rc1-2026-09-05; source base 263dfd5.
Five actual scenarios recorded as MP4 plus a 13:37.120 combined film: H.264,
1920x1080/25 FPS, five chapters, Russian captions/SRT. Continuous Gazebo and
MAVProxy recordings, source-time mapping, R1-R10 coverage and reports stay outside Git.
Five takeoffs/LAND/auto-disarm, 50/50 parallel ACKs, 20 multicast roots/100 deliveries;
eight native source cases and separate native maps; serial/PTy, TCP and UDP real
SITL UART ACKs, TCP disconnect/reconnect, native stop while SITL/Gazebo/UART remain.
Physical FC remains absent; current software status above accounts for failed run B.
Capture overhead is measured: flight 01 FPS 19.37, RTF .776, steady ns-3 lag
p95/max 76.77/541.25 ms; flight 02 FPS 24.68, RTF .988, lag 71.92/382.96 ms.
Both demo flight runs failed existing real-time gates; these are not new RC1 PASSes.
Other recordings: FPS 24.49-24.79, RTF .982-.993; cold lag is retained separately.
Demo geometry labels use actual geometry_summary bounds: 163.197 m external mesh
z extent; synthetic 10x10 km field/hills and 15-storey tower are explicitly identified.
Focused affected tests: 21 passed. All six videos decoded without errors; beginning,
middle, end and key action/ACK/recovery frames inspected. Commands and limits are in
doc/DEMO_GUIDE.md; CAVISE redistribution remains unresolved and this package is internal.

## Restored package run B

rc1-restored-customer-20260906T100415Z: full flight/LAND/auto-disarm, ten UART,
26 actual ACK result=0, P2P 100/100, P2MP 20 roots/100 deliveries, shared 100/100,
fairness 1, 10.5 s no-bypass and cleanup passed. Summary failed, realtime limited,
make exit 2; 19/20 gates. Only realtime_scheduler_gazebo_and_pose_gates failed.
Steady lag p95/max 57.760693/129.202154 ms; bound p95 <=50 ms; cold max 9197.073 ms.
RTF mean/p5 .993056/.949876 pass .95/.8; pose age p95 30.339 ms passes <=500 ms.
Radio age p95/max 13.880/19.880 s. Source t=100–110 s: decoder errors 0→6→0;
mean decoded SINR 32.06→27.34→30.92 dB; window application PDR remains null.
22/395 steady lag samples exceed 50 ms. Root cause not established; no installation
defect confirmed, no runtime/threshold changes and no full repeat. A stays historical PASS.
No BAS container/process/netns remains. Same-host agent check, not independent acceptance.
HANDOVER: doc/HANDOVER.md; human procedure: doc/HUMAN_ACCEPTANCE.md; FC: doc/FC_BENCH.md.
Viewing/checks: /home/bas/bas_v2-handover/rc1-2026-09-06/{viewing,checks}; full B raw in restore/runs.

## Handover corrections and retained diagnostics

Offline restore commands/SHA checks and portable viewing references clarified.
Ethernet YAML comment fixed: external socket is host-side, radio socket in ams-uav1;
parsed YAML values unchanged. No executable/runtime changes; B tests exact release.
C/03 retains rc=2 and four failed full-run gates (lifecycle, flight, screenshots, RT).
C/05 serial/TCP/UDP retain rc=2: NameError native_sources in write_latency_diagnostic_report.
This report-mode defect is registered, not fixed in frozen RC1; ACK/PTY is not hardware PASS.
C/04 eight source cases rc=0 are not full flight PASSes. See DEMO_GUIDE and archived logs.
C FPS/RTF/lag remain recording_performance.json values; A/B values never label C frames.
Video mtime/size still agree with previous complete decode; no remux/record/decode repeated.
Checks for this handover: local document links, YAML semantic equality, git diff --check.
Independent engineer and physical FC remain external; no people appointed or messages sent.
