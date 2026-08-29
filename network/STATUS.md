# Product Status

Updated: 2026-08-29. Runtime observations are recorded separately from pending
product behavior; neither file existence nor a validator-only result is proof.

| Stage | Status | What works now | What does not work yet | Next one task | Verification command | Last simple artifact |
| --- | --- | --- | --- | --- | --- | --- |
| P0 Process reset | done | Product-first rules, short sources of truth, one agent profile, archive, Make targets, and changed-path tests are present. | Nothing known within P0 scope. | Start P1; do not extend process infrastructure. | `make test-changed` | `network/STATUS.md` |
| P1 Five-UAV baseline | done | Five live SITLs, five Gazebo models, five odometry streams, unique endpoints, health checks, managed stop, and the arm/takeoff/hold/move/land lifecycle passed together. | Nothing known within P1 scope. | Continue P2 without changing the proven five-UAV lifecycle. | `make run-town01` | `runs/town01-full-20260827T064100Z/metrics/scenario_summary.json` |
| P2 Communication vertical slice | done | Five live UAVs have ten independent 115200-baud SERIAL1/SERIAL2 paths through ns-3, external serial fragmentation/reassembly, isolated MAVLink parsers, real per-UAV command/ACK/telemetry diagnostics, bidirectional checksummed P2P, one-to-five P2MP, and a passing no-bypass stop proof. | Nothing known within the five-UAV software communication-plane scope; physical QGC/HitL remains isolated in P5. | Preserve this communication plane while advancing the later product stages. | `BAS_TOWN01_RUN_ID=town01-communication-20260827T120000Z ./scripts/product/run_town01_full_stack.sh` | `runs/town01-communication-20260827T120000Z/metrics/communication_summary.json` |
| P3 Shared 10 km scene | deferred | Town01 remains the active development scene. | The 10 by 10 km expansion is not the current task. | Do not block P4, P5, or P6 on P3 during the Town01 development phase. | `make run-town01` | `runs/town01-full-20260827T064100Z/metrics/summary.json` |
| P4 Interference and medium access | in_progress | A bounded-overload baseline is available: token buckets, static headroom validation, centralized priority scheduling over the ns-3 CSMA channel, bounded retries, and logical terminal accounting. | Live jammer packet-path integration, dynamic channel-aware airtime admission, and distributed CSMA contention characterization are pending; physical ns-3 queue emptiness is not sampled at logical drain. | Preserve the baseline while addressing the next P4 behavior. | `BAS_TOWN01_PROFILES=controlled_overload BAS_TOWN01_SKIP_HEATMAPS=1 BAS_TOWN01_SKIP_FLIGHT_SCENARIO=1 ./scripts/product/run_town01_full_stack.sh` | `doc/results/town01_bounded_overload_v1.md` |
| P5 HitL and real time | todo | Software serial/UDP loopback and timing-shaped artifacts exist. | Live hardware serial/Ethernet through real ns-3 and Sionna/hybrid state, watchdog behavior, and wall-clock limits are not demonstrated. | Replace one virtual timing stage with the real ns-3 path while keeping bounded queues. | `./network/tests/test_hitl_loopback.sh` (software only) | None from this task |
| P6 Scalability and hybrid propagation | todo | Architecture defines timestamped Sionna state and explicit simple-model fallback. | No benchmark matrix or measured operating envelope exists. | After P5, benchmark the five-UAV baseline at multiple Sionna update rates. | Not implemented | None |
| P7 Integrated demo | in_progress | One lifecycle-managed Town01 command launches five SITLs, Gazebo, ROS odometry, real Sionna RT, ns-3, ten framed UART paths, additional data, runtime monitoring, and gated nominal/contention/controlled-overload profiles, then produces topology, JSON/CSV summaries, class PCAPs, raw event logs, and report. Meltdown is an explicit separate non-gating lifecycle. | The integrated development demo does not yet close P3, P5, or P6 and therefore is not the final customer demo. | Carry the same orchestrator onto the compliant scene and live HitL path after P3-P6. | `BAS_TOWN01_RUN_ID=town01-controlled-global-20260827T214404Z ./scripts/product/run_town01_full_stack.sh` | `runs/town01-controlled-global-20260827T214404Z/report.md` |

Bounded-overload note: at 33.8048 Mbit/s offered, ingress admitted 12.8328
Mbit/s. Control delivered 600/600 with 1.349 ms p95 latency; payload delivered
3,373 and additional data 3,384 packets, with delivered Jain fairness 0.9999995
and 0.9999988. Scheduler-lag p95 fell from 7,594.223 to 6.580 ms, matched ns-3
events per delivered logical packet from 12.8266 to 7.5293, and logical terminal
pending fell from 15,013 to zero; ingress/medium drops were 11,235/8. The mean
Gazebo RTF was run-global, not controlled-profile evidence. No-bypass is full-run
structural evidence, not a profile-local result. Nominal control stayed 300/300
at 1.454 ms p95; contention improved from 798/800 to 800/800 at 1.603 ms p95.
All ten UART paths, real SITL ACK, bidirectional P2P 10/10, one-to-five P2MP,
and Sionna link-state application passed. BSF1 remained one fragment per
serial record, so optional aggregation remains disabled. Meltdown at the one
tested 33.8048 Mbit/s point admitted all offered traffic, delivered 22.8507
Mbit/s offered-window-normalized or 14.0620 Mbit/s wall-interval application
goodput, and left 3,913 medium drops; no saturation sweep was performed.

P4 future item: stock/distributed CSMA contention mode is not yet characterized.
