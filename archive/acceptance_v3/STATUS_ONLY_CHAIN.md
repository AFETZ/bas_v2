# Status-only chain procedure (v1 through v5)

`network/scripts/generate_status_documents.py` is the canonical renderer for
the three mutable status files. It derives every citation, qualification
prefix, next-command hash and Git blob identity from immutable receipts and the
exact technical `HEAD`; do not hand-edit its metadata block.

## Version/receipt matrix

| Status version | Newly closed milestone | Required cumulative receipts | Active milestone |
| --- | --- | --- | --- |
| v1 | M0 | `m0` | M1 |
| v2 | M1 | `m0`, `m1` | M2 |
| v3 | M2 | `m0`, `m1`, `m2` | M3 |
| v4 | M3 | `m0`, `m1`, `m2`, `m3` | M4 |
| v5 | M4 | `m0`, `m1`, `m2`, `m3`, `m4` | M5 (not implemented) |

The checkout must be clean and `HEAD` must equal the `source_commit` of the
newest receipt (for v1, the M0 qualification-vector commit). The renderer
rejects writable, noncanonical, misplaced, missing or extra receipts.

## Review render

Before changing the live status files, render into `/tmp` and inspect the
three outputs. Example for v3:

```bash
rm -rf /tmp/ams-status-v3-review
network/scripts/generate_status_documents.py \
  --version 3 \
  --receipt m0=runs/<m0-run>/metrics/m0_host_final_receipt.json \
  --receipt m1=runs/<m1-run>/metrics/m1_host_final_receipt.json \
  --receipt m2=runs/<m2-run>/metrics/m2_host_final_receipt.json \
  --output-dir /tmp/ams-status-v3-review

diff -u network/PROGRESS.md \
  /tmp/ams-status-v3-review/network/PROGRESS.md
diff -u network/VALIDATION_REPORT.md \
  /tmp/ams-status-v3-review/network/VALIDATION_REPORT.md
diff -u network/NEXT_TASK.md \
  /tmp/ams-status-v3-review/network/NEXT_TASK.md
```

Use the same cumulative prefix for other versions; the receipt filenames are:

- `m0_host_final_receipt.json`
- `m1_host_final_receipt.json`
- `m2_host_final_receipt.json`
- `m3_host_final_receipt.json`
- `m4_host_final_receipt.json`

## Canonical status-only commit

Repeat the reviewed command with `--write-canonical` instead of
`--output-dir`. Then prove that exactly the three authorized paths changed:

```bash
git diff --name-only
git diff --check

git add network/PROGRESS.md \
  network/VALIDATION_REPORT.md \
  network/NEXT_TASK.md

git diff --cached --name-only
git commit -m "status: close M2 from immutable receipt"
network/scripts/run_status_validation.sh
```

`git diff --name-only` and `git diff --cached --name-only` must contain exactly:

```text
network/NEXT_TASK.md
network/PROGRESS.md
network/VALIDATION_REPORT.md
```

Do not combine a status commit with technical code, configuration, tests,
plans, manifests or receipts. The live validator recursively checks the prior
status authority embedded in the newest component's source commit, so each
technical milestone commit must descend from the preceding passing status-only
commit.

After v2 passes, the separate `flight_capacity_prerequisite` component receipt
must be produced on the same v2 technical source commit before M2 can run.
After v4 passes, produce `m4_capacity_prerequisite` on the same v4 source
commit before the M4 causality component. These direct component receipts are
not replacements for milestone receipts and are not extra status versions.

Status v2 and v4 encode those operations as an exact machine-readable
`next_sequence`. Each sequence has two ordered wrapper argv vectors and two
different `RUN_ID` placeholders. With no successful current-epoch auxiliary
receipt, execute both steps in order. With exactly one, never rerun the first
step: execute or retry only the second step with a fresh second-step `RUN_ID`.
More than one successful auxiliary receipt for the same status-report commit
fails live status validation. A successful second-step receipt forbids another
second-step execution and requires advancing to the next status version; only a
failed second step (which cannot publish a host-final receipt) may be retried.
As soon as one successful second-step receipt exists, live validation marks the
old status sequence stale and rejects every further component launch from that
epoch; generate and commit the next status version instead.
