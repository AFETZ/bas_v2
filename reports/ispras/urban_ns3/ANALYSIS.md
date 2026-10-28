# Urban ns-3 analysis tools

Status: implementation and synthetic unit checks; **no new urban flight results**
are asserted by this document. The study order is a design until root freezes
version/configuration and validates integrated pilots.

The exporter reads immutable raw and writes to a separate output directory:

```bash
python3 scripts/product/analyze_urban_experiment.py run RAW_RUN \
  --output ANALYSIS_DIR --scenario SCENARIO_SNAPSHOT_JSON
# In the pinned analysis environment, add --plots for PNG/SVG/PDF.
python3 scripts/product/analyze_urban_experiment.py plan \
  --repeats 10 --order-seed 20261028 --rng-seed 1 --output PLANNED_ORDER_JSON
```

Input contract:

- Existing `metrics/relay_experiment_{config,summary}.json`,
  `metrics/relay_control_refresh.csv`, and `logs/coordination_*.jsonl`.
  A combined `coordination_events.jsonl` takes precedence when present.
- `logs/geometry_links.csv`: native `time_s`, actual `wall_monotonic_ns`,
  `from_id/to_id` (0=CP), `geom_los`, selected `radio_state`, `blocker_ids`,
  `pathloss_db`, and `parameter_set`. Geometry and selected radio condition
  remain distinct in the LOS-only ablation. Positions/frequency stay in raw.
- `logs/node_state.jsonl`: actual ENU Gazebo odometry, node source simulation
  timestamps, publication/received clocks, velocities and stale flags.
- An immutable `logs/urban_scenario.json` snapshot (legacy configuration/ fallback)
  with five `routes` and physical building boxes;
  explicit `--scenario` is supported if the snapshot is elsewhere. The path and
  SHA256 are included in provenance; absent snapshots give null denominators.

Outputs include JSON definitions/results, offered opportunities, geometry
transitions with observation brackets, actual trajectories with gap markers,
and independently replayed physical dwell checks. Optional figure shows actual
XY paths, ENU Z, CP↔initial relay visibility/loss and selection times.

Command lineage, payload, issued grant and UART boundary checks reuse the
existing reviewed `analyze_relay_experiment.py`; **the exporter does not reuse
its vertical goal-recovery or artificial fault endpoints**. Native PCAP proof
is separate from application-stage lineage. The general product summarizer
recognizes the geometry scalar chain and rejects a second spectrum pathloss;
this alone is not evidence of physical model calibration or experiment success.

`urban_target_CID_stages.csv` separately records unique target CID stages:
master receive/forward, follower application receipt, UART write-end and audit.
Each stage checks actor, creation, target, payload, UAV/epoch/leader identity.
`rates_alias_note` explicitly preserves legacy `delivered=UART admitted` naming;
application receipt is not PHY Rx/ACK, and unique physical goals are separate.

The primary trigger is the first observed CP↔initial relay LOS→NLOS transition
after measurement start. Starting in NLOS is not an invented transition.
Without packet/application impairment the endpoint is called post-NLOS UART
latency, not restored control. Per-UAV first updates and all-five under one
leader/epoch are separate. No actual handover means handover-only endpoint N/A.

Physical completion requires configured waypoint index/XYZ membership,
goal/CID/target/epoch/relay consistency, live authority at completion and replay
of actual pose samples within the held region for the requested dwell. Repeated
publications cannot extend dwell; stale intervals/session changes/source gaps
break it. Freshness is checked from actual sample/publication clocks, not only
the stale flag. Dwell starts after the first valid same-goal/epoch admission;
latest refresh need not start a new dwell. Continuous authority between refreshes
is not inferred. Actual per-goal and per-UAV horizontal displacement is exported.
A post-event refresh of a previously admitted goal does not establish
causation of movement. BRAKE→Guided and motion need their own observations.

An observed endpoint remains observed even if later teardown fails. Valid
nonrecovery at a complete administrative horizon is right-censored. Crashed or
incompletely recorded windows are interrupted/unverifiable, not silently treated
as ordinary censoring. Raw attempt status and observation completeness coexist.
After-LOS-return admission ratios require a complete predeclared window;
no return is N/A, partial windows do not generate a full-window rate.

Study-plan output consists of independent seed blocks, four randomized cases
per block: fixed/failover × geometry/LOS-only. It is not a freeze or execution
record. Actual flight/source/config signatures and pilot review belong to root.
Seeds do not ensure equal realized random packet outcomes after policies diverge.

`scripts/product/prepare_urban_study.py plan --output NEW_PATH --repeats 10`
prepares all56 cases (40 main,12 sensitivity,4 engineering); repeats5 gives36.
Its `freeze --help` describes the required existing pilot/review/config inputs.
Freeze refuses overwrite, incomplete/invalid pilots, dirty tracked source and
nonpinned/missing images. Output includes exact case IDs/config hashes compatible
with `run_urban_case.py`. It does not run cases or generate scientific approval.

Focused tests: `network/tests/test_urban_analysis.py` uses explicitly synthetic
fixtures to verify missing delivery, conflicting authority, epoch mixing, false
geometry triggers, interrupted observations, stale/gapped dwell, immutable raw,
factorial order and double-pathloss rejection. These tests never represent
integrated flight evidence. Production data must be independently reviewed.
