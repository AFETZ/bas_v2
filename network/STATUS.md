# Product Status

## Measured urban follow-up, 2026-10-29

- [Results, exact effects and scope](../reports/ispras/urban_ns3/RESULTS.md),
  [independent624 numerical checks](../reports/ispras/urban_ns3/INDEPENDENT_STUDY_REVIEW.md),
  [independent system/12-page PDF review](../reports/ispras/urban_ns3/FINAL_SYSTEM_REVIEW.md).
  This is a new cohort, not a reinterpretation/repeat of main30 or sensitivity18.
- Frozen runtime `4d96d196e29792e8fd508f97994b78251efaee12`: five actual ArduPilot
  SITL, Gazebo/DART, native ns-3.48 ad-hoc/direct peers, shared building boxes and
  upstream BuildingsChannelConditionModel-selected LogDistance. Scalar loss once;
  no custom physics/MAC/PER or injected Matrix/filter outage in this series.
  Sionna RT is not instantiated; installed old image/fork build packages remain.
- Nominal27 configured points, horizontal facade routes, relay70→60m horizontal
  NLOS entry→12m; all five have real XY movement. Route progression/barriers use
  fresh radio-delivered SITL telemetry. Critical followers mostly hold positions;
  no simultaneous-five maneuver or sensory inspection quality is claimed.
- All56 planned attempts processed: main40+sensitivity12+boundary4. **36 completed,
  20 failed,55 eligible complete240-model-s windows**. Nineteen failures are
  post-window LAND/disarm failures. Order29 main geometry failover interrupted
  with native hard realtime jitter31.790s; host-stall cause unknown. No replacement.
- Main geometry9 complete pairs: failover−fixed timely **+61.833pp**95% exploratory
  interval[61.463,62.139]; verified-waypoint effect **+44.856pp**[43.621,46.091].
  LOS-only10 pairs and interaction9 four-case blocks remain distinct. Missing
  primary outcomes get explicit range bounds; interrupted trigger applicability
  is unknown, never successful recovery or full-window N/A.
- Main geometry failover first-post-NLOS all-five UART mean4.234s, all9 >4s;
  after actual probe detection mean3.322s. Conditional new-goal mean53.825s is
  separate. BRAKE→GUIDED was observed, but full physical stop is not established
  in the selected handover. No-candidate and policy-dependent short-occlusion
  boundaries retained; no generic four-second guarantee or calibrated RF map.
- Parameter read-all:280 complete pre+180 complete post tables,1388indices each,
  all460 outside measurement; WP_SPD3/ARMING_SKIPCHK0. Missing100post tables
  stay missing. Overall196290 UART admissions include service/out-of-window frames.
- Full-window RTF0.999763–0.999931; max recorded lag129.271ms. Incomplete29 has
  its own RTF0.648974/31.790s fatal jitter. Headless process CPU/RSS only; GPU
  costs, GUI-flight, distributed clocks and hard real time are not established.
- [Anonymous12-page DOCX/PDF](../paper/urban/README.md), four measured figures and
  compact CSV/JSON in `reports/ispras/urban_ns3/`. Full raw stays local:
  57,325,606,769bytes/10,798files, [inventory/transfer commands](../reports/ispras/urban_ns3/reproduction/PACKAGING.md).
  Original raw/per-run analyses/results retained; analysis v3 only clarifies labels,
  with identical point estimates/CIs. No conference submission. GitHub branch push
  is explicitly user-authorized; original `bas_v2-main` is preserved.

## Article audit, 2026-10-28 — historical baseline

- [Six requested audit reports, raw-derived CSV and timelines](../reports/ispras/README.md). Main30/sensitivity18 were not rerun or rewritten. Algorithm/runtime code unchanged; main selective UART mean independently reproduced as3.9633874144s; all published table means/paired intervals agree.
- Main27000offers=22472sentCID+4528unsent;75058uniqueUART=22254target/window+636target/outside+22335non-target/window+29833non-target/outside.1350created goals/1200sent-admitted-completed; selected-goal creation delay is conditional, not a silent generator. Timely origin is command.created, not offer timestamp.
- New direct-GCS UAV4 reply-unavailability probes:2/5/10/30s ×fixed/failover ×onepair, eight completed plus two retained preflight failures from bidirectional OUTPUT DROP/EPERM. All five physical cycles;40postfault goals independently replayed.14670UART, zero observed age/lease/write/duplicate anomalies. Alternative relaysUAV2/UAV5;5s failover UART5.932s exceeds4s. At2s new lease starts after restored RF/old echo, no observed UART benefit. New n=1 diagnostics remain separate from main30 and sensitivity18; no CI.
- Friis still has no building shadowing. Relay guard forbids RT, goal adapter fixesXY, completion is vertical. [Precise feasibility and implementation gaps](../reports/ispras/OBSTACLE_FEASIBILITY.md); planned two-building/five-route industrial scenario is **not measured**, with actual LOS/NLOS times unavailable. Urban propagation/flight remains future work; no fabricated shadowing result.
- Pinned image/assets/native runtime restart checked; actual five SITL ELF/version recorded (ArduCopter4.8.0-dev,3c073f9a). Clone still requires external image/assets/raw/build manifest. Paper PDF and old raw unchanged; no conference submission.

Updated: 2026-10-27. This status describes the user-authorized paper/live worktree `/home/bas/bas_v2-paper-live`, not every historical BAS profile.
Main30 measured implementation: `8e16ec223e895c11891e6e7d7c39dff8cee39cac`; separate sensitivity18: `e6453c8e2a1ed4f6e4f9b16c224104e62e2df5d1`. Pinned image `sha256:89d78eff9914b1644a1b01b793612e9ee8b19916c5a7bf5f578ba6d8bfbfafe5`.

## Follow-up completed, separate cohort

- [Follow-up report, files and exact start/stop commands](../doc/LIVE_LEADER_FOLLOWUP_RESULTS.md). The main30 was not repeated; source/config/order/seeds of sensitivity18 were frozen before measurement. Same native binary SHA; no physics/MAC/lease threshold retuning.
- True `make operator` → `make gcs` selected UAV3 and completed takeoff16→altitude20→BRAKE→LAND/disarm through the radio/relay/UART path. Six exact-frame chains, observed physical z0.3405→20.3609→0.3405m; other four grounded. Launcher2 after explicit owned Docker stop0 remains recorded.
- Operator pre/post: five complete1388-index tables per stage, independently replayed against upstream tlog/raw/JSONL/CSV;54 matched value changes, no PARAM_SET. Historical main30 DataFlash remains observed-only.
- Sensitivity: nominal selective0.5/2/10s × fixed/failover ×3 pairs: **18 attempted,15 completed,3 failed after complete180s windows**. All18 network windows eligible; failures stay failed. Order10/11 fail during post-parameter reads, order12 during LAND; only UAV1 landing credited in12. No retry/overwrite.
- Mean common UART fixed→failover:0.5s1.261→1.271s(no role change),2s3.602→3.943s,10s11.617→3.939s. At2s all3 activations follow RFend and fresh old-channel echo; potentially unnecessary changes. At10s paired UART effect−7.677s and timely effect+4.593pp, exploratory n=3.
- **Two of six changed-relay recoveries exceed4s:4.123131166 and4.134810747s**; four satisfy the reference. Physical new-goal completion is separate and has no4s SLA. Actual RF exposure/heartbeat/renewal/command phase recorded; one nominal0.5s was0.450658483s.
- All18 pre five-table snapshots complete; post15complete/2partial/1skipped. Independent indexed replay:165complete component tables,10partial,5unrecorded. Missing/unindexed/CSV-null values stay explicit; snapshots are not atomic and were outside measurement.
- Main selective goal decomposition updated: failover14.972628s until goal creation+0.972830s to first command creation+0.010541s command-to-UART+5.346194s movement/hold=21.302193s. First CID differs from latest completion-refresh CID; none of these combined intervals is pure RF latency.
- Separate grounded GUI07: actual Town01/five-model screenshot,960×480 after warmup,60.009wall-s observation,225/225 nonce responses; clock RTF0.998871,p5=0.997549,lag p95/max2.492/6.859ms. Viewer CPU mean74.63%onecore,quota0.75; RSS max2.027GB. No GUI-flight,FPS,graphics-GPU-memory or causal overhead claim.
- Anonymous Russian **12-page DOCX/PDF** in official styles,11 primary sources, real graphs and all18 paired points in CSV; final artifact checks and36 focused analysis tests pass. PDF uses disclosed Liberation replacements for unavailable TNR/Arial.
- [Compact evidence and commands](../paper/evidence/followup/), [data](../paper/data/), [manuscript](../paper/README.md). Full raw remains under `runs/native-radio-realtime/`; no conference submission. The later user instruction explicitly authorizes commit/push of this working branch to GitHub.

## Observed product

- Town01 / stock DART, five real ArduPilot SITL, native ns-3.48 ad-hoc/Friis, headless control UART. Sionna RT is not instantiated; Friis does not run GPU/OptiX preflight.
- Native direct peer routes replace AP-dependent peer routing. GCS executable frames go to one coordinator, which forwards to four followers; actual UART response and physical action are recorded.
- GCS is the sole issuer. Epoch/lease fencing accounts for every issued old permission, including unacknowledged renewals; final UART write checks enforce authority and 250 ms command age.
- Native 802.11n aggregation/Block ACK is explicitly disabled for the coordinator profile using upstream attributes. No custom physics, MAC, PER or autopilot is introduced.
- Native PCAP/event CSV use an explicit observed 4 GiB tmpfs and are published after writers stop. All capture scope is retained. Native stop grace is 120 s.
- Real external operator UDP control was observed in flight: GUIDED→BRAKE→GUIDED on UAV3; 225/225 actual TIMESYNC responses. MAVProxy remains available through `make gcs`.

## Completed measurements

- Engineering `paper-live600-02`: 600.001 model / 600.123 wall s (scenario RTF0.999796); tracker sample interval599.978/600.110 s gives RTF0.999779, p5 of approximately1 s windows0.998672, minimum0.970634; native lag p95/max1.499/28.952 ms. All five LAND/disarm; 150 real height-goal completions.
- Frozen main matrix fixed/failover × none/selective/total_GCS × five paired RngRun: **30 attempted,30 completed,30 comparable**, 15 pairs, one source/image/settings signature, zero excluded runs.
- Each main run:180 model s after airborne readiness, fault45–105 s,900 offered refreshes. All30 completed real five-UAV flight, two target levels, controlled LAND and auto-disarm.
- Selective timely/offered: fixed65.711% vsfailover98.222%; mean paired effect+32.511 pp, exploratory95% interval[32.267,32.756]pp, n=5 whole pairs /3125 exact bootstrap resamples.
- Selective mean all-five UART recovery62.356→3.963 s; real new-goal recovery67.715→21.302 s. Failover UART range3.927–3.982 s. In all five selective failover runs, former master UAV1 remains a reachable peer follower.
- All10 total-GCS runs: real BRAKE/stable HOLD of five, zero new appointments or target UART admissions during isolation; recovery follows restored radio. Issued lost renewals are recorded separately.
- No-fault mean timely99.511% for both policies; recovery is N/A. Equal observed means and saturated total-GCS30 s losses do not establish equivalence.
- Main tracker RTF0.999564–0.999883; worst run p5=0.997355; measurement native lag max7.847 ms. Existing250 ms lag/500 ms pose/100 ms cross-source limits remain unchanged.
- 75058 actual UART admissions audited valid; no observed duplicate/stale/lease/lineage anomalies.27000 offers include unsent/lost commands. Old pre-fault goals completed later are excluded from recovery.
- 240 native files (10,184,179,226 bytes) published without errors;660 effective snapshot files checked;150 DataFlash exports each contain1388 observed parameter names. All observed ARMING_SKIPCHK=0,COMPASS_USE=1,SIM_SPEEDUP=1.
- Native radiotap proof for the first planned main pair verifies both command and return hops for followers1/2/3/5 under master4; the new physical goal completes inside the actual60 s RF outage.
- Grounded master-service SIGSTOP negative control:223/225→0/225→220/225 nonce replies while other followers and simulation continue. This pauses the whole master service; it is not an in-flight process-crash guarantee.

## Reproduction and evidence

- [Russian report, numerical text and start/stop commands](../doc/LIVE_LEADER_RESULTS.md).
- [Frozen plan, environment, source archive and exact commands](../runs/paper-live-20261026/campaign/).
- [All main CSV/PNG/SVG/PDF reports and paired outcomes](../runs/paper-live-20261026/campaign-analysis/relay_campaign_analysis.md).
- [First planned pair illustration](../runs/paper-live-20261026/first-pair-figure/first_planned_selective_pair.png).
- [Independent aggregate review](../runs/paper-live-20261026/campaign-final-review/review.md), [metadata/raw integrity](../runs/paper-live-20261026/campaign-metadata-integrity-review.md), [native hop review](../runs/paper-live-20261026/main-selective-s1-r04-native-path-review.md).
- Raw runs: `runs/native-radio-realtime/paper-relay-*`; engineering/failed runs remain alongside, outside the frozen cohort. No pilot is pooled into the main matrix.
- Focused mission/analysis tests passed37 cases; after capture-signature changes22 analysis cases passed. Native binary was built and exercised by the actual runs; other historical profiles were not fully regressed.

## Scope and remaining limits

- Result covers Town01, vertical missions, one host/shared monotonic clock, native Friis and declared symmetric link losses. No physical HitL, distributed-clock guarantee, hard real time or urban geometric radio shadowing is claimed.
- Town01 collision geometry uses approximated building/surface boxes; vegetation has no collision. Rock/Bullet lift remains unverified; main/sensitivity are headless. GUI has only the separate grounded observation above; Sionna/accelerated batch are outside the measurements.
- Resource figures cover three simulator process categories, not all helpers. tmpfs raw files add memory outside RSS. DataFlash PARAM_COUNT completeness is unverified; optional native_sources has generic not_recorded coverage while disabled.
- One de-duplicated publication contains four unique poses; it was not filled. Ten pair_exclusions are expected no-fault N/A recovery endpoints, not excluded runs.
- Failed developmental runs are retained, including old rock takeoff failures, BAR starvation, logging overload and `paper-live600-01` jitter261.329 ms. Exact cause of that isolated stall was not measured; no causal HDD-overhead estimate is claimed.
- Original `/home/bas/bas_v2-main` HEAD/status/binary and cached diff and saved untracked SHA256 still match the follow-up start snapshot. Its local changes were not reset. All owned simulators are stopped. Paper submission remains unauthorized; the subsequent GitHub push request is authorized separately.
