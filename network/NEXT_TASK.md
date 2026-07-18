# Next Task

Authoritative contract: `doc/network_radio_integration_plan_v3.md`.

Customer-ready: **false**. Fully closed sequential milestones: **0**. Active
milestone: **M0**.

## Exact Critical Path

1. Complete the independent audit of the current M0 boundary.
2. Rerun the exact-image runtime lock after the final manifest/policy edits.
3. Verify 178/178 frozen Q0 IDs, shell/Python syntax and `git diff --check`.
4. Commit a clean technical base.
5. Allocate one new M0 run ID, execute the unprivileged exact-image collector,
   perform host-final fresh-source re-execution and isolated capability probe,
   and atomically publish the canonical receipt.
6. Change exactly the three mutable status files, cite that receipt, commit the
   status-only descendant and pass `validate_status_documents.py`.
7. Only then allocate a new M1 run ID and execute:

```bash
scripts/run_acceptance_container.sh \
  timeout --signal=TERM --kill-after=20s 600s \
  env RUN_ID=<allocate-once-before-execution> \
  network/scripts/run_five_uav_health.sh
```

8. Independently revalidate the M1 run and inspect the stopped container. M1
   closes only if the complete 300-second five-UAV evidence passes without any
   caveat.

The target after this checkpoint is strict M1 closure, not M2 work or visual
polish.
