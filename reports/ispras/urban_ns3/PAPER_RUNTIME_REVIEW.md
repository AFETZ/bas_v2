# Independent runtime review of the urban manuscript

Reviewed manuscript/builder commit: `37f311b36b74150b68d1cba9224fd136818a1142`
in `/home/bas/bas-urban-mission`. Runtime reference:
`4d96d196e29792e8fd508f97994b78251efaee12` in the integration worktree.
This is a source-level, read-only review during the frozen series. It does not
report, predict or approve final main-series effects. The reviewer implemented
the native radio adapter, but did not author the manuscript or its presentation
transforms; this review is independent of the manuscript author, not an
independent reimplementation of the radio model.

## Findings requiring the paper owner's correction

1. **Final-document coverage gate:** `paper/urban/build_document.py:75–83`
   checks observed/planned attempts only if `self.study` exists. A manually
   supplied `kind=frozen_series` manifest can omit `study_json`, provide one
   valid analysis plus the real freeze/source commit, and bypass that check.
   Require a study aggregate for `--final`, then check every planned attempt
   and freeze identity unconditionally. The recommended `prepare_manifest.py`
   path already includes an aggregate; this concerns the public builder gate,
   not the currently running study. No runtime change is needed.
2. **Clearance wording:** manuscript §3.3, line46, and the matching supplement
   sentence say margins account for braking and vehicle size. The implemented
   preflight check validates straight planned centre segments against boxes
   expanded by a fixed3m (`prepare_urban_gazebo.py:79–84`). That is not a dynamic
   stopping-envelope proof. State the actual static clearance check; qualify
   physical safety by measured trajectories/contact evidence for observed runs.

Both findings were sent directly to the manuscript owner and root. Their fixes
belong to the paper owner's branch. No integration or foreign worktree was
edited by this reviewer.

## Source correspondence accepted

- The stated Building intersection classifier plus selected upstream
  LogDistance models matches `native-geometry-loss.h`. The adapter returns one
  scalar loss; `upstream-sionna-tap-spike.cc:1729–1754` installs it without a
  second FriisSpectrum base loss. Actual10dBm/2.412GHz, isotropic elements and
  zero position offset are described consistently.
- Exponents2/3 and2.7/3.3 sensitivity are explicitly hypotheses. Identical A2G
  and A2A coefficients, no field calibration, no material-specific penetration,
  edge diffraction or multipath reconstruction are disclosed. NLOS is not
  equated with packet loss. Geometry-loss ablation keeps physical buildings and
  routes; it does not turn NLOS-labelled poses into physical LOS.
- The source-time poses feed radio geometry; planned waypoints are not used as
  actual mobility. Five real SITL and Gazebo/DART remain responsible for flight.
  Source, ns-3, autopilot and host clocks are kept distinct in text/figure rules.
- The frozen north mission has27 planned points, relay70m preparation,
  south60m staging, horizontal60m entry behind A, and later12m facade stage.
  Followers are described as mostly holding during the critical event. The
  pilot-informed high-relay/barrier design is disclosed rather than portrayed
  as a mission chosen before pilot observation. No radio/lease tuning is
  attributed to the route change.
- Only radio-delivered telemetry advances route/barrier state; Gazebo ground
  truth is limited to radio geometry and independent physical verification.
  Route3s telemetry dwell and physical2s dwell are correctly separated.
  `WP_SPD=3` is a parameter, with measured overshoot acknowledged; old10m/s
  engineering pilots are not relabelled or pooled.
- The relay is an appointed command/telemetry forwarder, not autonomous
  distributed consensus. The sole issuer, all-four-peer condition, lease/epoch
  admission, running-sidecar assumption and absence of a universal4s guarantee
  are stated without adding guarantees beyond this implementation.
- Historical Friis/injected-loss cohorts and new geometric runs remain separate.
  Packets/UAVs/episodes are not declared independent experimental replicates.
  Full-window failed cleanup remains failed; crashes/missing data are distinct
  from administrative censoring. The text does not claim complete post-run
  parameter captures after failed cleanup.
- The figure contract preselects the first frozen block regardless of outcome,
  separates actual from planned paths, preserves data gaps, requests real
  timeline/clock evidence, and limits all-four-follower native packet proof to
  the inspected pilot. Application receive is not called PHY receive or an ACK.

## Checks performed and remaining presentation checks

Read the complete manuscript/supplement, figure contract, builder and pure
presentation transforms against the native adapter, channel wiring, scene
generator, and frozen `urban_north_inspection.json`. Four existing focused
presentation tests passed with:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover \
  -s /home/bas/bas-urban-mission/paper/urban -p test_study_inputs.py -v
```

No Docker, native build, flight, full PCAP rescan or paper render was run.
Final56-attempt data, actual generated figures, DOCX/PDF pagination/anonymity
and the final numerical discussion still require review after the series.
`measured:true` and passing a builder are not scientific validation.
