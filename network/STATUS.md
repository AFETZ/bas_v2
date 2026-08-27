# Product Status

Updated: 2026-08-27. Runtime observations are recorded separately from pending
product behavior; neither file existence nor a validator-only result is proof.

| Stage | Status | What works now | What does not work yet | Next one task | Verification command | Last simple artifact |
| --- | --- | --- | --- | --- | --- | --- |
| P0 Process reset | done | Product-first rules, short sources of truth, one agent profile, archive, Make targets, and changed-path tests are present. | Nothing known within P0 scope. | Start P1; do not extend process infrastructure. | `make test-changed` | `network/STATUS.md` |
| P1 Five-UAV baseline | done | Five live SITLs, five Gazebo models, five odometry streams, unique endpoints, health checks, managed stop, and the arm/takeoff/hold/move/land lifecycle passed together. | Nothing known within P1 scope. | Continue P2 without changing the proven five-UAV lifecycle. | `make run-town01` | `runs/town01-full-20260827T064100Z/metrics/scenario_summary.json` |
| P2 Communication vertical slice | done | Five live UAVs have ten independent 115200-baud SERIAL1/SERIAL2 paths through ns-3, external serial fragmentation/reassembly, isolated MAVLink parsers, real per-UAV command/ACK/telemetry diagnostics, bidirectional checksummed P2P, one-to-five P2MP, and a passing no-bypass stop proof. | Nothing known within the five-UAV software communication-plane scope; physical QGC/HitL remains isolated in P5. | Preserve this communication plane while advancing the later product stages. | `BAS_TOWN01_RUN_ID=town01-communication-20260827T120000Z ./scripts/product/run_town01_full_stack.sh` | `runs/town01-communication-20260827T120000Z/metrics/communication_summary.json` |
| P3 Shared 10 km scene | in_progress | The canonical Town01 Sionna scene and source-coordinate Gazebo derivative ran together; 867,887 source vertices were preserved with identity transform and zero measured vertex delta. | Town01 is only 3.191 by 3.191 km with a 493.406 m global Z span, so it does not satisfy the 10 by 10 km/up-to-200 m product requirement; Gazebo collisions are axis-aligned approximations with vegetation omitted, and the flight route observed LOS only. | Select or build a compliant 10 by 10 km scene while retaining the proven alignment checks. | `make run-town01` | `runs/town01-full-20260827T064100Z/metrics/summary.json` |
| P4 Interference and medium access | in_progress | One checked QoS configuration now drives class marking, bounded queues, deadlines, priority scheduling, and metrics. Simultaneous five-UAV nominal, contention, and overload runs record queue delay/depth, deadline/tail/PHY drops, backoff/retry, utilization, per-UAV fairness, and show control priority over payload/additional traffic under overload. | The shared medium is still the documented CSMA surrogate, and a timed/directional jammer runtime is not implemented. | Implement and measure one timed directional jammer through live Sionna link state. | `BAS_TOWN01_RUN_ID=town01-communication-20260827T120000Z ./scripts/product/run_town01_full_stack.sh` | `runs/town01-communication-20260827T120000Z/metrics/qos_summary.json` |
| P5 HitL and real time | todo | Software serial/UDP loopback and timing-shaped artifacts exist. | Live hardware serial/Ethernet through real ns-3 and Sionna/hybrid state, watchdog behavior, and wall-clock limits are not demonstrated. | Replace one virtual timing stage with the real ns-3 path while keeping bounded queues. | `./network/tests/test_hitl_loopback.sh` (software only) | None from this task |
| P6 Scalability and hybrid propagation | todo | Architecture defines timestamped Sionna state and explicit simple-model fallback. | No benchmark matrix or measured operating envelope exists. | After P5, benchmark the five-UAV baseline at multiple Sionna update rates. | Not implemented | None |
| P7 Integrated demo | in_progress | One lifecycle-managed Town01 command launches five SITLs, Gazebo, ROS odometry, real Sionna RT, ns-3, ten framed UART paths, additional data, runtime monitoring, and three communication load profiles, then produces the required topology, JSON/CSV summaries, class PCAPs, raw event logs, and report. | The integrated development demo does not yet close P3, P5, or P6 and therefore is not the final customer demo. | Carry the same orchestrator onto the compliant scene and live HitL path after P3-P6. | `BAS_TOWN01_RUN_ID=town01-communication-20260827T120000Z ./scripts/product/run_town01_full_stack.sh` | `runs/town01-communication-20260827T120000Z/report.md` |

Town01 communication-run metric note: the nominal control PDR was 0.996667 with
5.399 ms p95 latency; contention control PDR was 0.9975. Under overload,
control PDR was 0.475 versus 0.269467 for payload, with all five UAVs served.
The logical packet invariant was `46350 = 24186 delivered + 9549 dropped +
12615 pending`; retry/backoff and drop events are reported separately. One
P2MP root reached all five independently counted receivers, bidirectional P2P
completed 10/10 messages each way, and stopping ns-3 stopped both new commands
and reverse telemetry. The run made 31 real-Sionna queries, measured a 0.99332
mean Gazebo real-time factor, and recorded the overload scheduler-lag limit
rather than masking it. Multicast fanout and non-profile serial datagrams are
kept outside logical profile delivery accounting.
