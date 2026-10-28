# Independent final system and claims review

29 October 2026. Reviewer did not author the measured implementation, analyzers,
manuscript or figures. Measured source: `4d96d196e29792e8fd508f97994b78251efaee12`.
Review uses the immutable 56 attempts, not historical main30/sensitivity18.
Machine-readable checks: [FINAL_SYSTEM_REVIEW.json](FINAL_SYSTEM_REVIEW.json).
Statistical reimplementation belongs to the separate final analysis review.

## Findings and corrections

1. **Documented physical limitation:** in the first frozen geometry/failover
   case (`r03`, order 4), UAV1 enters observed BRAKE at NLOS+1.705216622s and
   GUIDED at+3.231163001s. Actual velocity during that interval is1.070–3.014m/s;
   no complete stop is demonstrated. The manuscript author incorporated this
   distinction; “continued motion after BRAKE” is supported, stationary HOLD
   followed by restart is not.
2. **Incomplete-trigger qualification:** absence of an observed NLOS transition
   in interrupted order 29 does not establish endpoint nonapplicability over the
   planned horizon. Methods must distinguish full-window no-trigger from unknown
   future trigger/applicability. Root corrected the reviewed aggregate; the same
   distinction was checked in the actual first rendered manuscript.
3. **Presentation:** actual initial `urban_first_pair.png` had overlapping XY
   x-label and altitude subtitle. Corrected `publication-figures/urban_first_pair.png`
   was visually checked: resolved. Other three actual PNGs are unchanged and legible.

## Checks performed

- Eight runtime/config files are byte-identical to measured source, including
  native geometry/channel wiring, scene generator, mission driver and authority.
  `native-geometry-loss.h:95–107` selects upstream Buildings/LogDistance using
  actual positions. `upstream-sionna-tap-spike.cc:1597–1607,1729–1756` excludes
  injected faults/sources and applies one scalar base loss without another full
  Friis or Sionna propagation path. Native PHY/MAC/PER owns packet outcomes.
- All 56 controller/summary records independently give36 completed / 20 failed,
  55 full 240-source-s windows and55 final native-stat files. All 56 controller source
  IDs and raw scenario hashes match freeze. All 55 recorded native-stat sets have
  ad-hoc, geometry_logdistance, scalar1/spectrum0, native PHY/MAC, no custom PER,
  no Sionna solver and fault=none, with expected seeds/modes. Missing order 29
  native stats are not inferred from neighbouring runs.
- Nineteen failed attempts retain complete windows and the recorded unsuccessful
  LAND/disarm cleanup. Order29 alone lacks a full window; its native log reports
  `NS_FATAL`/jitter 31790122927 ns. No geometric NLOS appears in its saved geometry
  rows. This is a failure signature, not an identified cause; no recovery time is
  manufactured. Before-capture rc0 is 56; after-capture rc0/present summary is 36.
  This review checks capture records/presence, not all large parameter tables.
- `relay_experiment.py:682–757` advances routes/barriers from fresh radio-delivered
  LOCAL_POSITION_NED; Gazebo truth serves geometry/assessment. `service.py:350–400`
  selects using delivered direct/all-four-peer probes/RTT. No scenario-selected
  successor, direct executable GCS→follower bypass or geometry-fed mission gate
  was found in these inspected paths.
- `policy.py:167–184,242–294,325–374` preserves maximum issued expiry even without
  ACK, checks lease/epoch/age/dedup at final writes and treats invalid partial
  writes as failure. `service.py:188–215,217–233` uses real serial writes and native
  BRAKE requests. These are trusted one-host processes, not Byzantine consensus
  or a guarantee that HOLD survives sidecar/host failure.
- Raw order 4 gives all five actual horizontal ranges (X 35.1–65.1 m,
  Y 80.1–100.1 m), and BRAKE→GUIDED observations on all five. Selected post-NLOS
  UAV1/waypoint4 traces goal creation→CID `2:1:2354`→UART→physical completion:
  18.393202566+0.830501819+0.016934973+34.701696610=53.942335968 host s.
  GCS/UART payload hashes match; actual issued grant covers write/check, final
  command age 16.948679 ms. Independent raw-pose replay gives 2.0 source-s dwell,
  87 deduplicated samples, max gap 40 ms. The completion refresh is a different CID;
  this is chronology and observed attainment, not proof of continuous authority
  or a unique command causing the entire movement.
- Negative raw sample: order3 fixed, UAV5 CID `1:5:2046` is received immediately
  before `no active received grant` rejection (`coordination_uav5.jsonl:3010`),
  and never appears in that UAV's UART admissions. No new injected stale replay
  or full-series UART payload decoding was performed.
- Reused the existing 8+4 native component probes, independent axes qualification,
  staged barrier/physical replay and pilot four-follower two-hop/return proof.
  These establish their recorded scopes; they do not become all 56 PHY decoding.
  Source and aggregate evidence retain idealized non-Town01 boxes, uncalibrated
  n 2/3 and 2.7/3.3, native WP_SPD 3, 27 nominal points, relay 70→60→12 m and mostly
  holding followers at the critical event. Fixed Z=.195 m and axes-only residual
  .1513 m do not establish exact contact datum in every run. Inspection quality,
  camera coverage and absence of all possible collisions are not measured.

## Final presentation and limits

All four publication PNGs and all 12 actual final PDF page PNGs were inspected.
Tables on pages 6–7 and figures/captions on pages 9–10 are legible, without clipping
or orphan captions; page 12 references fit. Two main figures and four repository
figures retain distinct scopes. Source/clock/failed-attempt qualifications survive.
Final PDF: `paper/urban/build/manuscript_anonymous.pdf`, SHA256
`6ae2eddd9d11142014068ca72891df42c1f0b10967254b81d94a3c48b3bac73d`.
Final DOCX SHA256: `70f2458efa629c81ba4705365f6167de579af90864b69378419a639a5aa8b7e7`.
Metadata have no author/last-modifier; DOCX has no comments/tracked edits. No byline,
workspace paths or unresolved template markers were found in the rendered text.
No unresolved blocking system, claims or rendering discrepancy was found within
this reviewed scope. Full-series PHY decoding, parameter-table replay and formal
safety/physical-hardware validation are outside it. Artifact hashes are in the JSON.

Reproduce the lightweight read-only check (no Docker/build/simulator/PCAP scan):

```bash
python3 reports/ispras/urban_ns3/tools/review_final_system.py \
 --integration /home/bas/bas_v2-paper-live --output /tmp/final-system-review.json
```
