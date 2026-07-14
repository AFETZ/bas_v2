# Next Task

Authoritative contract: `doc/network_radio_integration_plan_v3.md`.

Customer-ready: **false**. Fully closed milestones: **1**. Active milestone:
**M1**.

## Next Exact Action

Close the gaps exposed by strict inspection of
`m1_v3_candidate_20260714T072723Z` before running another candidate:

- stop configuring the optional `ttyROS*` serial endpoints for the M1
  component launch and fail on any post-warm-up open error;
- record PID, start ticks, executable path/hash, command-line hash, process
  state, namespaces, profile, scenario/phase, and correlated wall/monotonic
  clocks in raw evidence;
- independently require exactly five stable live ArduCopter, MAVProxy, and
  micro-ROS identities plus the stable Gazebo server for the complete window;
- reject any matching extra, zombie, missing, replaced, or incompletely
  identified process and any undeclared restart; and
- add adversarial fixtures for identity mutation, extra/zombie processes,
  missing runtime binding, and post-warm-up serial-open failures.

Run the focused adversarial suite and full regression suite, commit a clean M1
implementation, requalify affected M0 evidence, and only then execute the
exact-image formal candidate. Allocate the run ID immediately before execution:

```bash
RUN_ID="m1_v3_candidate_$(date -u +%Y%m%dT%H%M%SZ)"
export RUN_ID
CONTAINER_NAME="ams-m1-v3-${RUN_ID#m1_v3_candidate_}" \
./scripts/run_acceptance_container.sh \
  timeout --signal=TERM --kill-after=20s 420s \
  env RUN_ID="$RUN_ID" network/scripts/run_five_uav_health.sh

python3 network/scripts/validate_m1_health.py \
  --run-dir "runs/$RUN_ID" --no-write
```

Expected raw health evidence is
`runs/$RUN_ID/logs/five_uav_health_events.jsonl`; the runtime timeout is `420 s`
and the required uninterrupted observation is at least `300 s`. Retain and
inspect the stopped container. Close M1 only if the independent provenance,
health, and active-scene gates all pass without qualifications and direct raw
inspection agrees.

Only after accepted M1 is the next target M2: complete raw-derived transaction
metrics, current topology, exact runtime identity, four-point byte correlation,
and good/down/recovery no-bypass on the one-UAV external packet path.

Do not divert the critical path to video/dashboard polish, replay-only radio
proof, physical modem hardware, or customer-map presentation.

The only accepted v3 milestone evidence is M0 run
`m0_v3_baseline_20260713T130710Z`. The rejected M1 diagnostic must not be used
as a closure identity. The exact accepted image remains
`sha256:2aad1f25789fc1e5c23c3a4b05c91927198ad42ff6e97cde2c26cb2f18979afb`.
Full evidence sealing and external attestation apply to the later integrated P0
profiles. The private key remains outside the repository and must never enter
a run container.
