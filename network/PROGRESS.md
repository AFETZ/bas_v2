# Network/Radio Progress

Updated: 2026-07-17 UTC.

Authoritative contract: `doc/network_radio_integration_plan_v3.md`.

## Acceptance Status

- Customer-ready: **false**.
- Fully closed sequential milestones: **0**.
- Active milestone: **M0 — Truthful Validation and Exact Runtime Qualification**.
- Earlier M0/M1 runs are diagnostic history only: the current v3
  qualification boundary is stronger and does not grandfather them.

| Milestone | Formal status | Current position |
| --- | --- | --- |
| M0 | `in_progress` | Technical hardening and the complete frozen suite pass locally; a new atomic host-final receipt has not yet been published. |
| M1 | `not_started` | The hardened five-UAV runner/validator exists, but M1 cannot count before the replacement M0 receipt and a new clean 300-second run. |
| M2–M8 | `not_started` | No later milestone has caveat-free current sequential evidence. |

Only `passed` closes a milestone. Code, unit tests, historical runs and
diagnostics are foundation evidence, not milestone closure.

## Implemented M0 Foundation

- Formal suite production and independent re-execution are unprivileged:
  empty capability bounding set, `no-new-privileges`, `network=none`,
  read-only source/rootfs, isolated writable overlay and initially empty
  artifact mount.
- Target TUN/network-namespace/sudo capability is tested separately in the
  same immutable image without candidate source, artifact or receipt mounts.
- Q0–Q8 identity is derived from committed Git objects; the conservative
  bootstrap assigns every tracked technical byte to Q0 and excludes only the
  three mutable status documents.
- The exact ordered M0 suite contains **182 unique test IDs** and is bound by
  `network/config/dependency_lock.yaml`.
- Python execution records exact ordered `sys.path`, `.pth` inventory,
  customization/plugin state, every loaded module origin and byte hash, and
  rejects host/writable/unlocked imports.
- Critical image, source and host-final executables are path/hash locked;
  command resolution and runtime package manifests are recomputed live.
- Host finalization rederives captured gates, checks the retained container,
  uses a fresh never-producer-mounted source clone and second exact-image
  execution, retains recursive raw host evidence, and publishes with one
  fsynced no-replace rename.
- Live status authority is separate from technical evidence and accepts only a
  clean descendant changing exactly `PROGRESS.md`, `VALIDATION_REPORT.md` and
  `NEXT_TASK.md` while citing the canonical receipt.

## Current Verification

```text
python3 -m unittest discover -b -s network/tests -p 'test_*.py'
  -> 182/182 passed across the complete focused preflight set

Focused M0 runtime/host/status suites
  -> 53/53 passed

Exact image live runtime checks
  -> image/external/ns-3/executable identities passed;
     stale pip/ROS manifest hashes were found and replaced only after two
     identical live recomputations; final full exact-image rerun is pending.
```

## M1 Foundation Already Implemented

- Five expected ArduCopter, MAVProxy and micro-ROS identities plus one Gazebo
  server are sampled at no more than 1.5-second gaps using PID, start ticks,
  executable path/hash, command line, namespace/cgroup and parent identity.
- Readiness and measurement use one continuous baseline; extra, zombie,
  stopped, missing, replaced or unallowlisted processes fail closed.
- Five unique system IDs, DDS/SITL/FDM endpoints, fresh heartbeat/odometry,
  live world/entity inventory and transitive scene assets are independently
  validated.
- Disabled optional serial endpoints may not be opened; fatal launch markers
  or post-readiness endpoint errors fail the candidate.

These implementation facts do not close M1. A new clean exact-image run must
remain healthy for at least 300 seconds and pass independent revalidation after
M0 closes.

## Immediate Critical Path

1. Finish independent M0 audit and exact-image runtime-lock rerun.
2. Commit one clean technical base containing the 182-ID manifest.
3. Execute captured M0, independent exact-image re-execution, isolated
   capability probe and atomic host-final publication.
4. Create the exact three-file status-only descendant and pass live-status
   lint; only then count M0 as one closed milestone.
5. Execute and independently validate the new 300-second five-UAV M1 run.
