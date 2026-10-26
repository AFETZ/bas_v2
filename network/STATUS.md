# Product Status

Updated: 2026-10-27. This status describes the user-authorized paper/live worktree `/home/bas/bas_v2-paper-live`, not every historical BAS profile.
Measured implementation: `8e16ec223e895c11891e6e7d7c39dff8cee39cac`, pinned image `sha256:89d78eff9914b1644a1b01b793612e9ee8b19916c5a7bf5f578ba6d8bfbfafe5`.

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
- Town01 collision geometry uses approximated building/surface boxes; vegetation has no collision. Rock/Bullet lift remains unverified; GUI/Sionna/accelerated batch are outside these measurements.
- Resource figures cover three simulator process categories, not all helpers. tmpfs raw files add memory outside RSS. DataFlash PARAM_COUNT completeness is unverified; optional native_sources has generic not_recorded coverage while disabled.
- One de-duplicated publication contains four unique poses; it was not filled. Ten pair_exclusions are expected no-fault N/A recovery endpoints, not excluded runs.
- Failed developmental runs are retained, including old rock takeoff failures, BAR starvation, logging overload and `paper-live600-01` jitter261.329 ms. Exact cause of that isolated stall was not measured; no causal HDD-overhead estimate is claimed.
- Original `/home/bas/bas_v2-main` HEAD/status/diff and saved untracked bytes still match preparation. Its local changes were not reset. All owned simulators are stopped; no repository publication or paper submission was performed.
