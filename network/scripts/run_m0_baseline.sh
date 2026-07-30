#!/usr/bin/env bash
set -euo pipefail

# This produces captured M0 technical evidence.  It deliberately cannot mark
# M0 accepted: only the later host-final receipt may do that.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUN_ID="${RUN_ID:-m0_baseline_$(date -u +%Y%m%dT%H%M%SZ)}"
RUN_ROOT="${AMS_M0_ARTIFACT_ROOT:-$ROOT_DIR/runs}"
EXPECTED_RUN_DIR="$RUN_ROOT/$RUN_ID"
RUN_DIR="${RUN_DIR:-$EXPECTED_RUN_DIR}"

if (($# != 0)); then
  printf 'Usage: RUN_ID=<safe-id> %s\n' "$0" >&2
  exit 2
fi
if [[ ! "$RUN_ID" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]]; then
  printf 'FAIL RUN_ID contains unsafe characters: %s\n' "$RUN_ID" >&2
  exit 2
fi
if [[ "$RUN_DIR" != "$EXPECTED_RUN_DIR" ]]; then
  printf 'FAIL RUN_DIR must be exactly %s\n' "$EXPECTED_RUN_DIR" >&2
  exit 2
fi
if [[ -L "$RUN_ROOT" ]]; then
  printf 'FAIL runs directory must not be a symbolic link: %s\n' "$RUN_ROOT" >&2
  exit 2
fi
if [[ "${AMS_M0_SOURCE_MODE:-}" != "clean_git_clone_ro" ]] || \
  [[ "${AMS_M0_PROJECT_OVERLAY_MODE:-}" != "none_q0_source_only" ]] || \
  [[ ! "${AMS_M0_SOURCE_COMMIT:-}" =~ ^[0-9a-f]{40}$ ]] || \
  [[ "${AMS_M0_COLLECTION_SECURITY:-}" != \
    "cap_drop_all_no_new_privileges" ]] || \
  [[ "${AMS_M0_CAPABILITY_PROBE_MODE:-}" != \
    "host_final_isolated_exact_image" ]]; then
  printf 'FAIL captured M0 runner requires the immutable snapshot acceptance path\n' >&2
  exit 2
fi
if [[ "$RUN_ROOT" != "/run/ams/m0-artifacts" ]]; then
  printf 'FAIL captured M0 artifacts are not on the isolated acceptance mount\n' >&2
  exit 2
fi
if [[ "$(git -C "$ROOT_DIR" rev-parse HEAD)" != "$AMS_M0_SOURCE_COMMIT" ]] || \
  [[ -n "$(git -C "$ROOT_DIR" status --porcelain --untracked-files=all)" ]]; then
  printf 'FAIL immutable M0 snapshot is not the exact clean committed source\n' >&2
  exit 2
fi
SOURCE_MOUNT_OPTIONS="$(findmnt -n -o OPTIONS -T "$ROOT_DIR")"
if [[ ",$SOURCE_MOUNT_OPTIONS," != *,ro,* ]]; then
  printf 'FAIL immutable M0 source mount is not read-only: %s\n' \
    "$SOURCE_MOUNT_OPTIONS" >&2
  exit 2
fi
ARTIFACT_MOUNT_TARGET="$(findmnt -n -o TARGET -T "$RUN_ROOT")"
ARTIFACT_MOUNT_OPTIONS="$(findmnt -n -o OPTIONS -T "$RUN_ROOT")"
ROOTFS_OPTIONS="$(findmnt -n -o OPTIONS -T /)"
TMPFS_TYPE="$(findmnt -n -o FSTYPE -T /tmp)"
TMPFS_OPTIONS="$(findmnt -n -o OPTIONS -T /tmp)"
if [[ "$ARTIFACT_MOUNT_TARGET" != "$RUN_ROOT" ]] || \
  [[ ",$ARTIFACT_MOUNT_OPTIONS," != *,rw,* ]] || \
  [[ ",$ROOTFS_OPTIONS," != *,ro,* ]] || \
  [[ "$TMPFS_TYPE" != "tmpfs" ]] || \
  [[ ",$TMPFS_OPTIONS," != *,rw,* ]] || \
  [[ ",$TMPFS_OPTIONS," != *,nosuid,* ]] || \
  [[ ",$TMPFS_OPTIONS," != *,nodev,* ]]; then
  printf 'FAIL M0 artifact/rootfs/tmpfs mount contract is not exact\n' >&2
  exit 2
fi
if [[ "${PYTHONNOUSERSITE:-}" != "1" ]] || \
  [[ "${PYTHONPYCACHEPREFIX:-}" != /tmp/ams-m0-pycache-* ]]; then
  printf 'FAIL M0 Python cache/user-site isolation is unavailable\n' >&2
  exit 2
fi
if [[ -e "$RUN_ROOT" && ! -d "$RUN_ROOT" ]]; then
  printf 'FAIL runs path is not a directory: %s\n' "$RUN_ROOT" >&2
  exit 2
fi
if [[ -n "$(find "$RUN_ROOT" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
  printf 'FAIL M0 artifact root was not initially empty\n' >&2
  exit 2
fi
CAP_BOUNDING="$(awk '$1 == "CapBnd:" {print $2}' /proc/self/status)"
NO_NEW_PRIVS="$(awk '$1 == "NoNewPrivs:" {print $2}' /proc/self/status)"
if [[ "$CAP_BOUNDING" != "0000000000000000" ]] || \
  [[ "$NO_NEW_PRIVS" != "1" ]] || sudo -n true >/dev/null 2>&1; then
  printf 'FAIL M0 runner retained capabilities or privilege escalation\n' >&2
  exit 2
fi

umask 007
mkdir -p "$RUN_ROOT"
if ! mkdir "$RUN_DIR"; then
  printf 'FAIL immutable M0 run directory already exists or cannot be created: %s\n' \
    "$RUN_DIR" >&2
  exit 1
fi
mkdir "$RUN_DIR/logs" "$RUN_DIR/metrics"

# Every redirect below creates a new file.  Never replace a file inserted into
# this fresh run while the probe is active.
set -o noclobber
printf '%q ' "$0" "$@" > "$RUN_DIR/command.txt"
printf '\n' >> "$RUN_DIR/command.txt"
{
  printf 'run_id=%s\n' "$RUN_ID"
  printf 'utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'probe=m0_dependency_provenance\n'
  printf 'packet_path_executed=false\n'
  printf 'sealed=false\n'
  printf 'attested=false\n'
  printf 'p0_eligible=false\n'
  printf 'host_final_required=true\n'
  printf 'formal_accepted=false\n'
  printf 'source_mode=%s\n' "$AMS_M0_SOURCE_MODE"
  printf 'source_commit=%s\n' "$AMS_M0_SOURCE_COMMIT"
  printf 'source_mount_read_only=true\n'
  printf 'project_overlay_mode=%s\n' "$AMS_M0_PROJECT_OVERLAY_MODE"
  printf 'python_pycache_prefix=%s\n' "$PYTHONPYCACHEPREFIX"
  printf 'collection_security=%s\n' "$AMS_M0_COLLECTION_SECURITY"
  printf 'capability_probe_mode=%s\n' "$AMS_M0_CAPABILITY_PROBE_MODE"
  printf 'capability_bounding_set=%s\n' "$CAP_BOUNDING"
  printf 'no_new_privileges=%s\n' "$NO_NEW_PRIVS"
} > "$RUN_DIR/environment.txt"

set +e
"$ROOT_DIR/network/scripts/check_deps.sh" --qualification-profile m0 \
  > "$RUN_DIR/logs/check_deps.log" 2>&1
CHECK_DEPS_RC=$?
set -e
printf '%s\n' "$CHECK_DEPS_RC" > "$RUN_DIR/logs/check_deps.log.exit_code"

set +e
/usr/bin/python3.10 "$ROOT_DIR/network/scripts/verify_m0_runtime_lock.py" \
  --lock "$ROOT_DIR/network/config/dependency_lock.yaml" \
  --observed-image-digest "$AMS_CONTAINER_IMAGE_DIGEST" \
  > "$RUN_DIR/metrics/m0_runtime_lock.json" \
  2> "$RUN_DIR/logs/m0_runtime_lock_producer.log"
RUNTIME_LOCK_RC=$?
set -e
printf '%s\n' "$RUNTIME_LOCK_RC" \
  > "$RUN_DIR/logs/m0_runtime_lock_producer.log.exit_code"

set +e
  (
    cd "$ROOT_DIR"
    network/scripts/m0_bin/python3 network/scripts/run_m0_validation_suite.py \
    --run-dir "$RUN_DIR"
) > "$RUN_DIR/logs/m0_validation_suite_producer.log" 2>&1
VALIDATION_SUITE_RC=$?
set -e
printf '%s\n' "$VALIDATION_SUITE_RC" \
  > "$RUN_DIR/logs/m0_validation_suite_producer.log.exit_code"

set +e
/usr/bin/python3.10 "$ROOT_DIR/network/scripts/write_run_provenance.py" \
  --run-dir "$RUN_DIR" --qualification-profile m0 --consumed-node Q0 \
  > "$RUN_DIR/logs/provenance.log" 2>&1
PROVENANCE_RC=$?
set -e
printf '%s\n' "$PROVENANCE_RC" > "$RUN_DIR/logs/provenance.log.exit_code"

VALIDATION_OUTPUT="$RUN_DIR/metrics/m0_baseline_validation.json"
set +e
"$ROOT_DIR/network/scripts/m0_bin/python3" "$ROOT_DIR/network/scripts/validate_m0_baseline.py" \
  --run-dir "$RUN_DIR" --captured-producer-mode > "$VALIDATION_OUTPUT"
VALIDATION_RC=$?
set -e

if [[ -f "$VALIDATION_OUTPUT" ]]; then
  cat "$VALIDATION_OUTPUT"
else
  printf 'FAIL M0 validator did not produce JSON output\n' >&2
  exit 1
fi
printf 'M0 dependency/provenance probe retained at %s (not sealed, attested, or P0-eligible).\n' \
  "$RUN_DIR" >&2
if [[ -n "${AMS_RUNTIME_CONTAINER_ID_FILE:-}" && -f "$AMS_RUNTIME_CONTAINER_ID_FILE" ]]; then
  RUNTIME_CONTAINER_ID="$(<"$AMS_RUNTIME_CONTAINER_ID_FILE")"
  if [[ "$RUNTIME_CONTAINER_ID" =~ ^[0-9a-f]{64}$ ]]; then
    printf 'Required host-final command: network/scripts/run_m0_host_final.sh --run-dir <m0_artifact_staging>/%q --publish-run-dir runs/%q --expected-container-id %q\n' \
      "$RUN_ID" "$RUN_ID" "$RUNTIME_CONTAINER_ID" >&2
  fi
fi
if ((VALIDATION_RC != 0)); then
  printf 'FAIL captured M0 technical qualification did not complete\n' >&2
  exit "$VALIDATION_RC"
fi
exit 0
