# Product Status

Updated: 2026-09-24. Local checkout: main at 1960947, with workstation setup and simulator coupling changes.

## Current development workstation

development_environment_status=ready
local_integrated_gpu_runtime_status=blocked_wsl_optix

- Checkout: C:\bas, Windows / Ubuntu 22.04 WSL2; i5-12500H, 32 GB RAM,
  RTX 4060 Laptop 8 GB. Docker access from Ubuntu was restored.
- Isolated Python 3.10 environments are installed for Windows and Linux development,
  plus native Windows Sionna RT. Setup commands and portable locks are provided
  in this working tree; see [LOCAL_DEVELOPMENT](../doc/LOCAL_DEVELOPMENT.md).
- Runtime image multiagent_simulation:latest was built from source:
  sha256:8ec5fb705651dfb993084063c354f6c3ee20d66eeff5e3f710eb94d8aff1fa43.
  ROS 2 Humble, Gazebo Harmonic 8.15.0, pinned ArduPilot and the project ROS workspace
  are built. Native ns-3.48 with all three existing patches is built; --PrintHelp exits 0.
- Image pip check, ROS dependency installation and Dev Container setup passed.
  Gazebo completed five headless steps on empty.sdf; ArduCopter --help exits 0.
- Focused tests: 14 Windows tests and 30 Linux tests passed; one Town01 input-reference
  test failed because the external Town01 world is absent. No full regression was run.
- Native Windows Sionna RT 1.2.0 / Mitsuba 3.7.1 / Dr.Jit 1.2.0 executed a real
  rock_demo PathSolver calculation on CUDA: two path coefficients, finite positive total power.
- Final rock_demo preflight: 1 failure, 0 warnings. CUDA is visible, but Linux/WSL
  libnvoptix.so.1 cannot initialize. Preflight now executes a CUDA/OptiX ray intersection
  and reports this failure before simulation. No propagation fallback was introduced.
- Full five-UAV flight was not run here. Use native Linux/NVIDIA for the integrated GPU
  runtime; provide the original CAVISE assets for Town01. Built-in rock_demo is present.
- OpenCV 4.11.0.86 and quantized-mesh-tile 0.6.1 fix reproduced NumPy 2 conflicts in
  fresh upstream prerequisite installs. Other existing runtime lock versions are retained.
  This newly built image is not the historical RC1 image; apt packages have moved on.
- Local command logs are under runs/setup/ (ignored), including preflight-final.log,
  ns3-build-resume.log, runtime-smoke.log and sionna-windows-gpu.log.

## Simulator coupling: 2026-09-24

coupling_implementation_status=implemented_component_tested
integrated_realtime_gpu_status=not_verified_wsl_optix_blocked

The selected policy is real time: bounded state/packet age and stop on overload.
The six simulator pairs, state ownership and unsupported updates are specified
in [PRODUCT_ARCHITECTURE](../doc/PRODUCT_ARCHITECTURE.md).

- Gazebo Odometry/Clock retain source time and a bounded 32-sample history;
  body-frame velocity is rotated to ENU. ns-3 consumes measured XYZ, velocity
  and full attitude with causal sample selection, 500 ms age/drift and 100 ms skew limits.
- After readiness, native HardLimit stops scheduler lag above 250 ms. Healthy
  monotonic heartbeat runs every 50 ms; both gateways close after 300 ms silence.
  The five-UAV runner supervises radio process death and cleans up the whole run.
- Both product RT caches now use 0.5 s/0.5 m maxima; velocity changes >=0.5 m/s
  and rotations >=5 degrees invalidate a pair. Solver depth is never silently reduced.
  This profile needs new performance measurements; old RC results do not validate it.
- UART queues are bounded and nonblocking, preserve original ingress deadlines,
  validate both peer IP/port, and count expiry/overflow. External UDP no longer
  truncates 8192-byte datagrams. Lost records cannot consume the next record's deadline.
- No-bypass probes resume retained BSF1 sequence numbers and check actual UART
  byte deltas as well as GCS silence. The revised full RF probe has not run here.
- 38 focused Python cases passed across affected tests, including real UDP/PTY;
  C++ state/mobility tests cover causal selection, velocity, attitude, stale state,
  tracker reset and clock drift. Native target builds; all three patches apply
  to pristine pinned ns-3 and match the installed source. Shell/port checks passed.
- Real one-UAV Gazebo/ArduPilot -> ROS -> tracker -> ns-3 MobilityModel smoke passed:
  moved body, changed height/velocity/attitude, froze the bridge and rejected stale input.
  This is a component test with a Gazebo pose stimulus, not an armed flight or RF run.
- Real Windows CUDA Sionna solves changed Doppler with velocity, power with height,
  and polarization response with roll. Linux in-process GPU integration remains blocked.
  Logs/results: runs/coupling/ (ignored). Temporary probe containers were removed.

Two prior audit defects remain: the shared-uplink gate accepts 95% packet loss
when fairness is equal, and delayed same-command MAVLink ACK can be attributed
to a new operation. Their fixes are outside this coupling change. The earlier
counterexamples remain in runs/audit-2026-09-24/; current PASS flags are not full readiness.

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
