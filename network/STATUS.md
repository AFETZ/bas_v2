# Product Status

Updated: 2026-10-21. Canonical test baseline consolidated on `main`.

## Current test baseline

- `main` is the only permanent remote branch; fixed test revision:
  `test-baseline-2026-10-21`. This is a component-tested source baseline, not a new full-stack certification.
- Clean Windows checkout prepared through `scripts/setup_dev.ps1` using CPython 3.10.22
  and the unchanged development lock file; all 25 installed dependencies pass `uv pip check`.
- 61 focused tests passed, 4 Linux PTY/flock tests skipped. Files: `test_gcs_lockstep.py`,
  `test_simulation_clock.py`, `test_serial_transport.py`, `test_data_transport.py`,
  `test_native_latency_report.py`, `test_native_wifi_five_uav_product.py` under `network/tests/`.
- Compile, local Markdown references, diff and five-UAV port checks passed.
- ACK caches are invalidated for each command send. GCS reassembly and reception hold the
  I/O barrier; socket selection waits outside it. Windows phase-transition tests use a barrier fixture.
- Recovered the diagnostic source-report fix from `06b2293`: no undefined `native_sources`,
  incomplete switch windows stay unavailable, and failed/limited run verdicts are preserved.
- Product config path assertions now use POSIX paths consistently on Windows and Linux.
- Full Gazebo/SITL/ns-3/Sionna and physical controller tests were not run in this cleanup.
  Local WSL cannot start because its virtual disk is missing; Linux lock/PTY coverage remains unverified here.

## Repository cleanup

- Inspected all 12 remote branches and all four PRs; there were no open PRs or Actions runs.
- Removed 11 obsolete branches: seven already reachable from `main`; four divergent tips
  preserved as annotated remote tags under `archive/2026-10-21/` before deletion.
- Archived branches: `agent/publish-network-radio-milestones`, `demo/bas-v2-rc1`,
  `feature/bas-v2-rc1-handover`, `feature/bas-v2-rc1-stabilization`.
  Demo-recording and old delivery/qualification work remain available in those tags.
- Historical milestone tags are retained. GitHub now deletes merged PR branches automatically.
- README documents the pinned baseline, separate software/runtime setup, focused tests,
  realtime/lockstep commands and result paths. Existing dirty local review copies were preserved.

## Runtime evidence and remaining defects

- Earlier workstation results (September 2026): Windows CUDA Sionna RT 1.2.0 / Mitsuba 3.7.1 /
  Dr.Jit 1.2.0 solved rock_demo; Linux/WSL OptiX failed initialization. These are historical results.
- The previous coupling work reported 43 model-time and 38 realtime focused Python cases,
  native state/clock tests and target builds, shell/port and watchdog checks.
  Gazebo/ArduPilot/ROS + stock ns-3 completed 25 barriers with a 1.2 s host stall;
  a native Wi-Fi/TAP/Friis baseline completed 100 barriers and 3/3 pings, without stale poses.
  Full Sionna lockstep integration was not demonstrated; old logs: `runs/lockstep/`, `runs/coupling/`.
- The shared-uplink gate can accept 95% loss when fairness is equal. A newly received delayed
  same-command MAVLink ACK can still match a later operation; cached ACK reuse is fixed separately.
  Counterexamples: `runs/audit-2026-09-24/`. These are still open product defects.
- Current radio caches use 0.5 s/0.5 m bounds; new full five-UAV GPU performance measurements are needed.
  Sionna remains the default; no silent propagation fallback or fabricated RF/ACK outcomes.

## Historical software RC verification

The results below were reported on 2026-09-05 for release/bas-v2-rc1 on the previous
Linux stand. They are not measurements from this Windows/WSL workstation.
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
- Historical cache 20 s/10 m delayed disappearance and missed a 1 s recovery.
  The new 0.5 s/0.5 m profile has not been profiled in a full five-UAV GPU run.
  Moving blockers/material edits are not synchronized live; scene changes require restart.
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
Historical publishing path: release/bas-v2-rc1 → native-radio-wifi; current baseline is on `main`.

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
