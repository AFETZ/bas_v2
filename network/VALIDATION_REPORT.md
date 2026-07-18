# Network/Radio Validation Report

Updated: 2026-07-17 UTC.

Authoritative contract: `doc/network_radio_integration_plan_v3.md`.

Customer-ready: **false**. Accepted integrated P0 run: **none**. Fully closed
sequential milestones: **0**.

## Strict Milestone Assessment

| Milestone | Formal status | Evidence and blocker |
| --- | --- | --- |
| M0 | `in_progress` | The hardened implementation and frozen 181-test Q0 suite pass in preflight, but no receipt produced by the current qualification boundary exists yet. |
| M1 | `not_started` | Hardened implementation exists; sequential execution waits for M0 and then requires a new clean uninterrupted 300-second five-UAV run. |
| M2 | `not_started` | Existing one-UAV packet-path diagnostics cannot precede M1 closure. |
| M3 | `not_started` | No accepted five-UAV, three-class, bidirectional external ns-3 matrix. |
| M4 | `not_started` | No accepted online Sionna wall/terrain causal packet run. |
| M5 | `not_started` | No accepted real-byte contention/priority run. |
| M6 | `not_started` | No accepted PTY/Ethernet traversal through the same M2–M5 path. |
| M7 | `not_started` | No single sealed A–I integrated run on the kilometre-scale paired scene. |
| M8 | `not_started` | No clean-clone pair, soak, rebuild proof or customer handoff. |

## Why Historical M0 Does Not Count

The old run `m0_v3_baseline_20260713T130710Z` predates the current boundary.
It did not prove the present unprivileged collection model, separate
no-candidate-mount capability probe, committed-object Q vector, frozen exact
suite identity, complete Python import trace, critical executable policy,
fresh-source second execution, recursive durable host raw package and terminal
no-replace publication contract. It remains diagnostic history only.

The strict count therefore returns to zero until a new current receipt and its
separate three-file live-status descendant both pass.

## Current Technical Evidence

- Current frozen Q0 preflight: **181/181 passed** across the complete focused test set; the formal exact-image execution is still pending.
- Focused runtime-lock, M0 host-final and status-lint suites: **53/53 passed**.
- The exact immutable image remains
  `sha256:9456aa370188987f7b21ba16514d5805dc10bbbe26c322ca3241fedde80014f0`.
- Live exact-image checks passed image identity, nine external source
  revisions, ns-3 3.40 aggregate tree and 13 critical image executable
  identities.
- Previously recorded pip and ROS package hashes were stale. Replacement
  hashes were accepted only after two identical live recomputations:
  pip `36941db39413d66f80191197d8df8d771221dce3a440cbe98f9078cca012b70e`;
  ROS `0b1d47e01d3f92b96a85cb04dd867e13579d23caf933092a8113a94fec5503da`.
- A final complete exact-image runtime-lock pass remains required after all
  lock edits.

None of these facts is a formal M0 receipt by itself.

## Historical M1 Diagnostics

Earlier five-UAV runs proved basic feasibility, but they are rejected for
closure. The strongest old run exposed undeclared extra/zombie MAVProxy
processes and repeated configured `ttyROS*` open failures that the then-current
validator missed. The implementation now records and validates exact process
membership/continuity and disabled endpoints, so only a new run can count.

M1 acceptance requires all of the following in one new exact-image candidate:

- five live Gazebo UAV entities;
- exactly five stable ArduCopter, five MAVProxy, five micro-ROS agents and one
  Gazebo server for at least 300 seconds;
- system IDs exactly `1..5`, unique DDS/SITL/FDM endpoints;
- continuously fresh heartbeat and raw odometry for every UAV;
- unchanged readiness-to-measurement process identity with sample gaps no
  greater than 1.5 seconds;
- no extra/zombie/stopped/replaced/unallowlisted process, fatal marker,
  disabled-endpoint attempt, crash, stale pose or undeclared restart; and
- independent provenance, health and active-world/asset gates all passing.

M1 records real-time factor but does not waive the later M7 `0.95..1.05` gate.

## Next Acceptance Commands

After the clean technical commit, the M0 launcher allocates a new run ID and
uses the immutable image. Only after its canonical receipt and live-status lint
pass may the M1 command be executed:

```text
scripts/run_acceptance_container.sh timeout --signal=TERM --kill-after=20s \
  600s env RUN_ID=<allocate-once-before-execution> \
  network/scripts/run_five_uav_health.sh
```

No summary boolean, old run, video, dashboard or partial subsystem result may
substitute for these gates.
