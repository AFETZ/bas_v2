#!/usr/bin/bash
set -euo pipefail

SCRIPT_DIR="${BASH_SOURCE[0]%/*}"
if [[ "$SCRIPT_DIR" == "${BASH_SOURCE[0]}" ]]; then
  SCRIPT_DIR=.
fi
cd -- "$SCRIPT_DIR/.."
ROOT_DIR="$PWD"
IMAGE="${IMAGE:-multiagent_simulation:latest}"
NAME="${CONTAINER_NAME:-ams-acceptance-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
CONTAINER_WORKDIR=/workspace/multiagent_simulation

mapfile -t LOCKED_RUNTIME_POLICY < <(
  /usr/bin/python3.10 - "$ROOT_DIR/network/config/dependency_lock.yaml" <<'PY'
import pathlib
import sys
import yaml

lock = yaml.safe_load(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
policy = lock.get("runtime_policy") if isinstance(lock, dict) else None
variant = policy.get("mitsuba_variant") if isinstance(policy, dict) else None
gpu_required = policy.get("gpu_required") if isinstance(policy, dict) else None
dependencies = lock.get("dependencies") if isinstance(lock, dict) else None
ros = dependencies.get("ros") if isinstance(dependencies, dict) else None
image_digest = ros.get("project_image_digest") if isinstance(ros, dict) else None
if (
    not isinstance(variant, str)
    or not variant
    or not isinstance(gpu_required, bool)
    or not isinstance(image_digest, str)
    or not image_digest.startswith("sha256:")
    or len(image_digest) != 71
    or any(character not in "0123456789abcdef" for character in image_digest[7:])
):
    raise SystemExit("dependency lock runtime_policy is not exact")
print(variant)
print("true" if gpu_required else "false")
print(image_digest)
PY
)
if ((${#LOCKED_RUNTIME_POLICY[@]} != 3)); then
  printf 'FAIL could not resolve the locked runtime policy\n' >&2
  exit 2
fi
LOCKED_MITSUBA_VARIANT="${LOCKED_RUNTIME_POLICY[0]}"
LOCKED_GPU_REQUIRED="${LOCKED_RUNTIME_POLICY[1]}"
LOCKED_PROJECT_IMAGE_DIGEST="${LOCKED_RUNTIME_POLICY[2]}"

if (($# == 0)); then
  printf 'Usage: %s COMMAND [ARG ...]\n' "$0" >&2
  printf 'Runs one retained acceptance container by immutable image ID.\n' >&2
  exit 2
fi
if [[ ! "$NAME" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]]; then
  printf 'FAIL unsafe Docker container name: %s\n' "$NAME" >&2
  exit 2
fi

M0_MODE=0
M0_RUN_ID=""
M0_SOURCE_COMMIT=""
M0_SOURCE_SNAPSHOT=""
M0_ARTIFACT_STAGING=""
M0_CONTROL_STAGING=""
M1_MODE=0
M1_RUN_ID=""
M1_SOURCE_COMMIT=""
M1_SOURCE_SNAPSHOT=""
M1_ARTIFACT_STAGING=""
M1_CONTROL_STAGING=""
M1_M0_RECEIPT_HOST=""
M1_M0_RECEIPT_CANONICAL=""
M1_M0_RECEIPT_SHA256=""
COMPONENT_MODE=0
COMPONENT_PROFILE=""
COMPONENT_RUN_ID=""
COMPONENT_SOURCE_COMMIT=""
COMPONENT_SOURCE_SNAPSHOT=""
COMPONENT_ARTIFACT_STAGING=""
COMPONENT_CONTROL_STAGING=""
COMPONENT_STATUS_VALIDATION=""
COMPONENT_PREREQUISITES=""
COMPONENT_MAIN_NETWORK=""
COMPONENT_MAIN_CAPS=""
COMPONENT_MAIN_DEVICES=""
COMPONENT_NVIDIA_DRIVER_CAPABILITIES=compute,utility
COMPONENT_MAIN_USER=ubuntu
COMPONENT_CAPABILITY_MODE=inherited_m0_host_final
COMPONENT_VALIDATOR=""
COMPONENT_RECEIPT_CONTRACT=""
COMPONENT_RECEIPT_NAME=""
COMPONENT_M0_RECEIPT_CANONICAL=""
COMPONENT_M0_RECEIPT_SHA256=""
COMPONENT_RECEIPT_MOUNT_ARGS=()
COMPONENT_RECEIPT_FINALIZER_ARGS=()
COMPONENT_VALIDATOR_ARG_TEMPLATES=()
if (($# == 3)) && [[ "$1" == "env" ]] && \
  [[ "$2" =~ ^RUN_ID=([A-Za-z0-9][A-Za-z0-9_.-]{0,127})$ ]] && \
  [[ "$3" == "network/scripts/run_m0_baseline.sh" ]]; then
  M0_MODE=1
  M0_RUN_ID="${2#RUN_ID=}"
  if [[ -L "$ROOT_DIR/runs" || (-e "$ROOT_DIR/runs" && ! -d "$ROOT_DIR/runs") ]]; then
    printf 'FAIL M0 runs root is not a canonical directory\n' >&2
    exit 2
  fi
  mkdir -p "$ROOT_DIR/runs"
  if [[ -e "$ROOT_DIR/runs/$M0_RUN_ID" ]]; then
    printf 'FAIL immutable M0 run already exists: %s\n' \
      "$ROOT_DIR/runs/$M0_RUN_ID" >&2
    exit 2
  fi
  if [[ -L "$ROOT_DIR/.external/ns-3" || ! -d "$ROOT_DIR/.external/ns-3" ]]; then
    printf 'FAIL canonical ns-3 source is unavailable for M0 qualification\n' >&2
    exit 2
  fi
  if [[ -n "$(git -C "$ROOT_DIR" status --porcelain --untracked-files=all)" ]]; then
    printf 'FAIL formal M0 requires a clean committed checkout\n' >&2
    exit 2
  fi
  M0_SOURCE_COMMIT="$(git -C "$ROOT_DIR" rev-parse HEAD)"
  if [[ ! "$M0_SOURCE_COMMIT" =~ ^[0-9a-f]{40}$ ]]; then
    printf 'FAIL formal M0 source commit is malformed\n' >&2
    exit 2
  fi
  M0_SOURCE_SNAPSHOT="$(mktemp -d /tmp/ams-m0-source.XXXXXXXXXX)"
  rmdir "$M0_SOURCE_SNAPSHOT"
  git clone --quiet --no-hardlinks "$ROOT_DIR" "$M0_SOURCE_SNAPSHOT"
  git -C "$M0_SOURCE_SNAPSHOT" checkout --quiet --detach "$M0_SOURCE_COMMIT"
  mkdir -p "$M0_SOURCE_SNAPSHOT/runs" "$M0_SOURCE_SNAPSHOT/.external/ns-3"
  if [[ -n "$(git -C "$M0_SOURCE_SNAPSHOT" status --porcelain --untracked-files=all)" ]]; then
    printf 'FAIL generated M0 source snapshot is not clean\n' >&2
    rm -rf "$M0_SOURCE_SNAPSHOT"
    exit 2
  fi
  chmod -R a-w "$M0_SOURCE_SNAPSHOT"
  M0_ARTIFACT_STAGING="$(mktemp -d "$ROOT_DIR/../.ams-m0-artifacts-$M0_RUN_ID.XXXXXXXXXX")"
  M0_CONTROL_STAGING="$(mktemp -d "$ROOT_DIR/../.ams-m0-control-$M0_RUN_ID.XXXXXXXXXX")"
  M0_ARTIFACT_STAGING="$(cd -- "$M0_ARTIFACT_STAGING" && pwd -P)"
  M0_CONTROL_STAGING="$(cd -- "$M0_CONTROL_STAGING" && pwd -P)"
  setfacl -m u:1000:rwx "$M0_ARTIFACT_STAGING"
  chmod 0700 "$M0_CONTROL_STAGING"
fi
if (($# == 7)) && [[ "$1" == "timeout" ]] && \
  [[ "$2" == "--signal=TERM" ]] && [[ "$3" == "--kill-after=20s" ]] && \
  [[ "$4" == "600s" ]] && [[ "$5" == "env" ]] && \
  [[ "$6" =~ ^RUN_ID=([A-Za-z0-9][A-Za-z0-9_.-]{0,127})$ ]] && \
  [[ "$7" == "network/scripts/run_five_uav_health.sh" ]]; then
  M1_MODE=1
  M1_RUN_ID="${6#RUN_ID=}"
  if [[ -L "$ROOT_DIR/runs" || (-e "$ROOT_DIR/runs" && ! -d "$ROOT_DIR/runs") ]]; then
    printf 'FAIL M1 runs root is not a canonical directory\n' >&2
    exit 2
  fi
  mkdir -p "$ROOT_DIR/runs"
  if [[ -e "$ROOT_DIR/runs/$M1_RUN_ID" ]]; then
    printf 'FAIL immutable M1 run already exists: %s\n' "$ROOT_DIR/runs/$M1_RUN_ID" >&2
    exit 2
  fi
  if [[ -L "$ROOT_DIR/.external/ns-3" || ! -d "$ROOT_DIR/.external/ns-3" ]]; then
    printf 'FAIL canonical ns-3 source is unavailable for M1 provenance\n' >&2
    exit 2
  fi
  if [[ -n "$(git -C "$ROOT_DIR" status --porcelain --untracked-files=all)" ]]; then
    printf 'FAIL formal M1 requires a clean committed checkout\n' >&2
    exit 2
  fi
  M1_SOURCE_COMMIT="$(git -C "$ROOT_DIR" rev-parse HEAD)"
  if [[ ! "$M1_SOURCE_COMMIT" =~ ^[0-9a-f]{40}$ ]]; then
    printf 'FAIL formal M1 source commit is malformed\n' >&2
    exit 2
  fi
  M1_SOURCE_SNAPSHOT="$(mktemp -d /tmp/ams-m1-source.XXXXXXXXXX)"
  rmdir "$M1_SOURCE_SNAPSHOT"
  git clone --quiet --no-hardlinks "$ROOT_DIR" "$M1_SOURCE_SNAPSHOT"
  git -C "$M1_SOURCE_SNAPSHOT" checkout --quiet --detach "$M1_SOURCE_COMMIT"
  mkdir -p "$M1_SOURCE_SNAPSHOT/runs" "$M1_SOURCE_SNAPSHOT/.external/ns-3"
  if [[ -n "$(git -C "$M1_SOURCE_SNAPSHOT" status --porcelain --untracked-files=all)" ]]; then
    printf 'FAIL generated M1 source snapshot is not clean\n' >&2
    exit 2
  fi
  chmod -R a-w "$M1_SOURCE_SNAPSHOT"
  M1_ARTIFACT_STAGING="$(mktemp -d "$ROOT_DIR/runs/.m1-stage-$M1_RUN_ID.XXXXXXXXXX")"
  setfacl -m u:1000:rwx "$M1_ARTIFACT_STAGING"
  M1_CONTROL_STAGING="$(mktemp -d "$ROOT_DIR/../.ams-m1-control-$M1_RUN_ID.XXXXXXXXXX")"
  chmod 0700 "$M1_CONTROL_STAGING"
  if ! network/scripts/run_status_validation.sh \
    > "$M1_CONTROL_STAGING/m0_status_validation.json"; then
    printf 'FAIL formal M1 requires a currently valid formal M0 status authority\n' >&2
    exit 2
  fi
  M1_M0_RECEIPT_CANONICAL="$(/usr/bin/python3.10 -S - \
    "$M1_CONTROL_STAGING/m0_status_validation.json" "$M1_SOURCE_COMMIT" <<'PY'
import json
import pathlib
import re
import sys

document = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
receipt = document.get("receipt_path")
if (
    document.get("schema_version") != 1
    or document.get("contract") != "ams.live-status-lint/v1"
    or document.get("passed") is not True
    or document.get("failures") != []
    or document.get("report_commit") != sys.argv[2]
    or not isinstance(receipt, str)
    or re.fullmatch(
        r"runs/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}/metrics/m0_host_final_receipt\.json",
        receipt,
    ) is None
):
    raise SystemExit("M0 live-status result is not exact/current")
print(receipt)
PY
  )"
  M1_M0_RECEIPT_HOST="$ROOT_DIR/$M1_M0_RECEIPT_CANONICAL"
  if [[ -L "$M1_M0_RECEIPT_HOST" || ! -f "$M1_M0_RECEIPT_HOST" ]] || \
    [[ -w "$M1_M0_RECEIPT_HOST" ]]; then
    printf 'FAIL formal M1 canonical M0 receipt is missing, linked, or writable\n' >&2
    exit 2
  fi
  M1_M0_RECEIPT_SHA256="$(sha256sum "$M1_M0_RECEIPT_HOST" | awk '{print $1}')"
  if [[ ! "$M1_M0_RECEIPT_SHA256" =~ ^[0-9a-f]{64}$ ]]; then
    printf 'FAIL formal M1 canonical M0 receipt hash is malformed\n' >&2
    exit 2
  fi
  chmod 0400 "$M1_CONTROL_STAGING/m0_status_validation.json"
fi
if (($# == 7)) && [[ "$1" == "timeout" ]] && \
  [[ "$2" == "--signal=TERM" ]] && [[ "$3" == "--kill-after=20s" ]] && \
  [[ "$4" =~ ^([0-9]{3,4})s$ ]] && [[ "$5" == "env" ]] && \
  [[ "$6" =~ ^RUN_ID=([A-Za-z0-9][A-Za-z0-9_.-]{0,127})$ ]]; then
  COMPONENT_TIMEOUT_S="${4%s}"
  COMPONENT_PROFILE_INFO=()
  set +e
  mapfile -t COMPONENT_PROFILE_INFO < <(
    /usr/bin/python3.10 - "$7" "$COMPONENT_TIMEOUT_S" <<'PY'
import sys

from network.validation.component_profiles import match_profile

try:
    name, profile = match_profile(sys.argv[1], int(sys.argv[2]))
except (OSError, ValueError):
    raise SystemExit(1)
values = [
    name,
    profile["main_network"],
    ",".join(profile["main_cap_add"]),
    ",".join(profile["main_devices"]),
    profile["nvidia_driver_capabilities"],
    profile["validator"],
    profile["receipt_contract"],
    profile["receipt_name"],
    *profile["validator_arguments"],
]
if any("\n" in value or "\r" in value for value in values):
    raise SystemExit(1)
print(*values, sep="\n")
PY
  )
  PROFILE_MATCH_EXIT=$?
  set -e
  if ((PROFILE_MATCH_EXIT == 0)) && ((${#COMPONENT_PROFILE_INFO[@]} >= 9)); then
    COMPONENT_MODE=1
    COMPONENT_PROFILE="${COMPONENT_PROFILE_INFO[0]}"
    COMPONENT_MAIN_NETWORK="${COMPONENT_PROFILE_INFO[1]}"
    COMPONENT_MAIN_CAPS="${COMPONENT_PROFILE_INFO[2]}"
    COMPONENT_MAIN_DEVICES="${COMPONENT_PROFILE_INFO[3]}"
    COMPONENT_NVIDIA_DRIVER_CAPABILITIES="${COMPONENT_PROFILE_INFO[4]}"
    if [[ -n "$COMPONENT_MAIN_DEVICES" ]]; then
      COMPONENT_MAIN_USER=root:1000
      COMPONENT_CAPABILITY_MODE=bounded_root_in_runtime
    fi
    COMPONENT_VALIDATOR="${COMPONENT_PROFILE_INFO[5]}"
    COMPONENT_RECEIPT_CONTRACT="${COMPONENT_PROFILE_INFO[6]}"
    COMPONENT_RECEIPT_NAME="${COMPONENT_PROFILE_INFO[7]}"
    COMPONENT_VALIDATOR_ARG_TEMPLATES=("${COMPONENT_PROFILE_INFO[@]:8}")
    COMPONENT_RUN_ID="${6#RUN_ID=}"
  fi
fi
if ((COMPONENT_MODE == 1)); then
  if ((M0_MODE == 1 || M1_MODE == 1)); then
    printf 'FAIL command matched more than one formal acceptance mode\n' >&2
    exit 2
  fi
  if [[ -L "$ROOT_DIR/runs" || (-e "$ROOT_DIR/runs" && ! -d "$ROOT_DIR/runs") ]]; then
    printf 'FAIL component runs root is not a canonical directory\n' >&2
    exit 2
  fi
  mkdir -p "$ROOT_DIR/runs"
  if [[ -e "$ROOT_DIR/runs/$COMPONENT_RUN_ID" ]]; then
    printf 'FAIL immutable component run already exists: %s\n' \
      "$ROOT_DIR/runs/$COMPONENT_RUN_ID" >&2
    exit 2
  fi
  if [[ -L "$ROOT_DIR/.external/ns-3" || ! -d "$ROOT_DIR/.external/ns-3" ]]; then
    printf 'FAIL canonical ns-3 source is unavailable for component provenance\n' >&2
    exit 2
  fi
  if [[ -n "$(git -C "$ROOT_DIR" status --porcelain --untracked-files=all)" ]]; then
    printf 'FAIL formal component acceptance requires a clean committed checkout\n' >&2
    exit 2
  fi
  COMPONENT_SOURCE_COMMIT="$(git -C "$ROOT_DIR" rev-parse HEAD)"
  if [[ ! "$COMPONENT_SOURCE_COMMIT" =~ ^[0-9a-f]{40}$ ]]; then
    printf 'FAIL formal component source commit is malformed\n' >&2
    exit 2
  fi
  COMPONENT_SOURCE_SNAPSHOT="$(mktemp -d /tmp/ams-component-source.XXXXXXXXXX)"
  rmdir "$COMPONENT_SOURCE_SNAPSHOT"
  git clone --quiet --no-hardlinks "$ROOT_DIR" "$COMPONENT_SOURCE_SNAPSHOT"
  git -C "$COMPONENT_SOURCE_SNAPSHOT" checkout --quiet --detach \
    "$COMPONENT_SOURCE_COMMIT"
  mkdir -p "$COMPONENT_SOURCE_SNAPSHOT/runs" \
    "$COMPONENT_SOURCE_SNAPSHOT/.external/ns-3"
  if [[ -n "$(git -C "$COMPONENT_SOURCE_SNAPSHOT" status --porcelain --untracked-files=all)" ]]; then
    printf 'FAIL generated component source snapshot is not clean\n' >&2
    exit 2
  fi
  chmod -R a-w "$COMPONENT_SOURCE_SNAPSHOT"
  COMPONENT_ARTIFACT_STAGING="$(mktemp -d \
    "$ROOT_DIR/runs/.component-stage-$COMPONENT_RUN_ID.XXXXXXXXXX")"
  if [[ -n "$COMPONENT_MAIN_DEVICES" ]]; then
    chmod 2770 "$COMPONENT_ARTIFACT_STAGING"
  fi
  COMPONENT_CONTROL_STAGING="$(mktemp -d \
    "$ROOT_DIR/../.ams-component-control-$COMPONENT_RUN_ID.XXXXXXXXXX")"
  chmod 0700 "$COMPONENT_CONTROL_STAGING"
  COMPONENT_STATUS_VALIDATION="$COMPONENT_CONTROL_STAGING/status_validation.json"
  COMPONENT_PREREQUISITES="$COMPONENT_CONTROL_STAGING/prerequisites.json"
  if ! network/scripts/run_status_validation.sh > "$COMPONENT_STATUS_VALIDATION"; then
    printf 'FAIL component acceptance requires a currently valid status authority\n' >&2
    exit 2
  fi
  # Invoke the resolver as a repository module under -S.  A file-path
  # invocation makes Python place network/scripts (not ROOT_DIR) on sys.path
  # and therefore cannot import the sibling network.validation package in a
  # clean shell.
  if ! AMS_COMPONENT_SOURCE_COMMIT="$COMPONENT_SOURCE_COMMIT" \
    /usr/bin/python3.10 -S -m network.scripts.resolve_component_prerequisites \
      --root "$ROOT_DIR" --profile "$COMPONENT_PROFILE" \
      --status-result "$COMPONENT_STATUS_VALIDATION" \
      > "$COMPONENT_PREREQUISITES"; then
    printf 'FAIL component prerequisite receipts are not exact/current\n' >&2
    exit 2
  fi
  mapfile -t COMPONENT_RECEIPT_ROWS < <(
    /usr/bin/python3.10 - "$COMPONENT_PREREQUISITES" <<'PY'
import json
import pathlib
import re
import sys

document = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
receipts = document.get("receipts")
component_receipts = document.get("component_receipts")
if (
    not isinstance(receipts, dict)
    or not receipts
    or not isinstance(component_receipts, dict)
    or set(receipts).intersection(component_receipts)
):
    raise SystemExit("component receipt set is empty")
combined = {**receipts, **component_receipts}
for name, record in sorted(combined.items()):
    values = (
        name,
        record.get("canonical_path"),
        record.get("host_path"),
        record.get("sha256"),
    )
    if (
        re.fullmatch(r"m[0-8]|[a-z][a-z0-9_]{2,63}", str(name)) is None
        or not all(isinstance(value, str) for value in values)
        or any("\t" in value or "\n" in value or "\r" in value for value in values)
        or re.fullmatch(r"[0-9a-f]{64}", values[3]) is None
    ):
        raise SystemExit("component receipt record is malformed")
    print("\t".join(values))
PY
  )
  if ((${#COMPONENT_RECEIPT_ROWS[@]} == 0)); then
    printf 'FAIL component prerequisite receipt set is empty\n' >&2
    exit 2
  fi
  for COMPONENT_RECEIPT_ROW in "${COMPONENT_RECEIPT_ROWS[@]}"; do
    IFS=$'\t' read -r RECEIPT_NAME RECEIPT_CANONICAL RECEIPT_HOST RECEIPT_SHA256 \
      <<< "$COMPONENT_RECEIPT_ROW"
    if [[ -L "$RECEIPT_HOST" || ! -f "$RECEIPT_HOST" || -w "$RECEIPT_HOST" ]] || \
      [[ "$(sha256sum "$RECEIPT_HOST" | awk '{print $1}')" != "$RECEIPT_SHA256" ]]; then
      printf 'FAIL component prerequisite receipt is not immutable/exact: %s\n' \
        "$RECEIPT_NAME" >&2
      exit 2
    fi
    COMPONENT_RECEIPT_MOUNT_ARGS+=(
      -v "$RECEIPT_HOST:/run/ams/prerequisites/$RECEIPT_NAME.json:ro"
    )
    COMPONENT_RECEIPT_FINALIZER_ARGS+=(
      --prerequisite-receipt "$RECEIPT_NAME=$RECEIPT_HOST"
    )
    if [[ "$RECEIPT_NAME" == "m0" ]]; then
      COMPONENT_M0_RECEIPT_CANONICAL="$RECEIPT_CANONICAL"
      COMPONENT_M0_RECEIPT_SHA256="$RECEIPT_SHA256"
    fi
  done
  if [[ -z "$COMPONENT_M0_RECEIPT_CANONICAL" || \
      -z "$COMPONENT_M0_RECEIPT_SHA256" ]]; then
    printf 'FAIL component prerequisite receipt set lacks M0\n' >&2
    exit 2
  fi
  chmod 0400 "$COMPONENT_STATUS_VALIDATION" "$COMPONENT_PREREQUISITES"
fi
if ! IMAGE_ID="$(docker image inspect --format '{{.Id}}' "$IMAGE")"; then
  printf 'FAIL runtime image is unavailable: %s\n' "$IMAGE" >&2
  exit 1
fi
if [[ ! "$IMAGE_ID" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  printf 'FAIL Docker returned a non-immutable image ID: %s\n' "$IMAGE_ID" >&2
  exit 1
fi
if [[ "$IMAGE_ID" != "$LOCKED_PROJECT_IMAGE_DIGEST" ]]; then
  printf 'FAIL runtime image ID differs from dependency-lock project image: %s\n' \
    "$IMAGE_ID" >&2
  exit 2
fi

CONTAINER_ID_FILE="$(mktemp /tmp/ams-container-id.XXXXXXXXXX)"
cleanup_identity_file() {
  if ((M0_MODE == 0)); then
    rm -f "$CONTAINER_ID_FILE"
  fi
}
trap cleanup_identity_file EXIT

if [[ -n "${AMS_ENABLE_GPU+x}" ]]; then
  printf 'FAIL acceptance GPU selection is lock-controlled; AMS_ENABLE_GPU is forbidden\n' >&2
  exit 2
fi
GPU_ARGS=(-e "SIONNA_MITSUBA_VARIANT=$LOCKED_MITSUBA_VARIANT")
if [[ "$LOCKED_GPU_REQUIRED" == "true" ]]; then
  GPU_ARGS=(
    --gpus "all,\"capabilities=$COMPONENT_NVIDIA_DRIVER_CAPABILITIES\""
    -e NVIDIA_VISIBLE_DEVICES=all
    -e "NVIDIA_DRIVER_CAPABILITIES=$COMPONENT_NVIDIA_DRIVER_CAPABILITIES"
    -e "SIONNA_MITSUBA_VARIANT=$LOCKED_MITSUBA_VARIANT"
  )
fi

SOURCE_ARGS=()
M0_ENV_ARGS=()
ISOLATION_ARGS=(--privileged --network=host)
if ((M0_MODE == 1)); then
  SOURCE_ARGS=(
    -v "$M0_SOURCE_SNAPSHOT:$CONTAINER_WORKDIR:ro"
    -v "$M0_ARTIFACT_STAGING:/run/ams/m0-artifacts:rw"
    -v "$ROOT_DIR/.external/ns-3:$CONTAINER_WORKDIR/.external/ns-3:ro"
  )
  M0_ENV_ARGS=(
    -e AMS_M0_SOURCE_MODE=clean_git_clone_ro
    -e "AMS_M0_SOURCE_COMMIT=$M0_SOURCE_COMMIT"
    -e AMS_M0_PROJECT_OVERLAY_MODE=none_q0_source_only
    -e AMS_M0_ARTIFACT_ROOT=/run/ams/m0-artifacts
    -e AMS_M0_COLLECTION_SECURITY=cap_drop_all_no_new_privileges
    -e AMS_M0_CAPABILITY_PROBE_MODE=host_final_isolated_exact_image
    --read-only
    --tmpfs /tmp:rw,nosuid,nodev,exec,size=4g,mode=1777
  )
  # The qualification container must not be able to remount a host bind or
  # replace the suite which is checking it.  Target-runtime namespace/TUN
  # capability is proved later by a separate exact-image host-final probe
  # which has no source or artifact mounts.
  ISOLATION_ARGS=(
    --cap-drop=ALL
    --security-opt=no-new-privileges:true
    --network=none
  )
elif ((COMPONENT_MODE == 1)); then
  SOURCE_ARGS=(
    -v "$COMPONENT_SOURCE_SNAPSHOT:$CONTAINER_WORKDIR:ro"
    -v "$COMPONENT_ARTIFACT_STAGING:$CONTAINER_WORKDIR/runs:rw"
    -v "$ROOT_DIR/.external/ns-3:$CONTAINER_WORKDIR/.external/ns-3:ro"
    -v "$COMPONENT_STATUS_VALIDATION:/run/ams/status-validation.json:ro"
    -v "$COMPONENT_PREREQUISITES:/run/ams/prerequisites.json:ro"
    "${COMPONENT_RECEIPT_MOUNT_ARGS[@]}"
  )
  M0_ENV_ARGS=(
    -e "AMS_COMPONENT_PROFILE=$COMPONENT_PROFILE"
    -e AMS_COMPONENT_SOURCE_MODE=clean_git_clone_ro
    -e "AMS_COMPONENT_SOURCE_COMMIT=$COMPONENT_SOURCE_COMMIT"
    -e "AMS_COMPONENT_RUN_ID=$COMPONENT_RUN_ID"
    -e AMS_COMPONENT_STATUS_RESULT_PATH=/run/ams/status-validation.json
    -e AMS_COMPONENT_PREREQUISITES_PATH=/run/ams/prerequisites.json
    -e AMS_M1_SOURCE_MODE=clean_git_clone_ro
    -e "AMS_M1_SOURCE_COMMIT=$COMPONENT_SOURCE_COMMIT"
    -e AMS_M1_PROJECT_OVERLAY_MODE=fresh_run_overlay
    -e "AMS_M1_RUN_ID=$COMPONENT_RUN_ID"
    -e "AMS_M0_CAPABILITY_PROBE_MODE=$COMPONENT_CAPABILITY_MODE"
    -e AMS_M1_M0_RECEIPT_PATH=/run/ams/prerequisites/m0.json
    -e "AMS_M1_M0_RECEIPT_CANONICAL_PATH=$COMPONENT_M0_RECEIPT_CANONICAL"
    -e "AMS_M1_M0_RECEIPT_SHA256=$COMPONENT_M0_RECEIPT_SHA256"
    -e "AMS_M1_M0_STATUS_COMMIT=$COMPONENT_SOURCE_COMMIT"
    --read-only
    --tmpfs /tmp:rw,nosuid,nodev,exec,size=4g,mode=1777
  )
  ISOLATION_ARGS=(
    --cap-drop=ALL
    --security-opt=no-new-privileges:true
    "--network=$COMPONENT_MAIN_NETWORK"
  )
  if [[ -n "$COMPONENT_MAIN_CAPS" ]]; then
    IFS=',' read -r -a COMPONENT_CAPS <<< "$COMPONENT_MAIN_CAPS"
    for COMPONENT_CAP in "${COMPONENT_CAPS[@]}"; do
      ISOLATION_ARGS+=("--cap-add=$COMPONENT_CAP")
    done
  fi
  if [[ -n "$COMPONENT_MAIN_DEVICES" ]]; then
    M0_ENV_ARGS+=(
      --tmpfs /run/netns:rw,nosuid,nodev,noexec,size=16m,mode=0755
    )
    ISOLATION_ARGS+=(--security-opt=apparmor=unconfined)
    IFS=',' read -r -a COMPONENT_DEVICES <<< "$COMPONENT_MAIN_DEVICES"
    for COMPONENT_DEVICE in "${COMPONENT_DEVICES[@]}"; do
      ISOLATION_ARGS+=("--device=$COMPONENT_DEVICE:$COMPONENT_DEVICE:rwm")
    done
  fi
elif ((M1_MODE == 1)); then
  SOURCE_ARGS=(
    -v "$M1_SOURCE_SNAPSHOT:$CONTAINER_WORKDIR:ro"
    -v "$M1_ARTIFACT_STAGING:$CONTAINER_WORKDIR/runs:rw"
    -v "$ROOT_DIR/.external/ns-3:$CONTAINER_WORKDIR/.external/ns-3:ro"
    -v "$M1_M0_RECEIPT_HOST:/run/ams/m0-receipt.json:ro"
  )
  M0_ENV_ARGS=(
    -e AMS_M1_SOURCE_MODE=clean_git_clone_ro
    -e "AMS_M1_SOURCE_COMMIT=$M1_SOURCE_COMMIT"
    -e AMS_M1_PROJECT_OVERLAY_MODE=fresh_run_overlay
    -e "AMS_M1_RUN_ID=$M1_RUN_ID"
    -e AMS_M0_CAPABILITY_PROBE_MODE=inherited_m0_host_final
    -e AMS_M1_M0_RECEIPT_PATH=/run/ams/m0-receipt.json
    -e "AMS_M1_M0_RECEIPT_CANONICAL_PATH=$M1_M0_RECEIPT_CANONICAL"
    -e "AMS_M1_M0_RECEIPT_SHA256=$M1_M0_RECEIPT_SHA256"
    -e "AMS_M1_M0_STATUS_COMMIT=$M1_SOURCE_COMMIT"
    --read-only
    --tmpfs /tmp:rw,nosuid,nodev,exec,size=4g,mode=1777
  )
  ISOLATION_ARGS=(
    --cap-drop=ALL
    --security-opt=no-new-privileges:true
    --network=host
  )
else
  SOURCE_ARGS=(-v "$ROOT_DIR:$CONTAINER_WORKDIR")
fi

CONTAINER_USER=ubuntu
if ((COMPONENT_MODE == 1)); then
  CONTAINER_USER="$COMPONENT_MAIN_USER"
fi
CONTAINER_ID="$(docker create \
  --name "$NAME" \
  --user "$CONTAINER_USER" \
  --restart=no \
  "${ISOLATION_ARGS[@]}" \
  "${GPU_ARGS[@]}" \
  -e AMS_CONTAINER_IMAGE="$IMAGE" \
  -e AMS_CONTAINER_IMAGE_DIGEST="$IMAGE_ID" \
  -e AMS_CONTAINER_IMAGE_DIGEST_SOURCE=docker_image_inspect_host \
  -e AMS_RUNTIME_CONTAINER_ID_FILE=/run/ams/container_id \
  -e GZ_VERSION=harmonic \
  "${M0_ENV_ARGS[@]}" \
  -v "$CONTAINER_ID_FILE":/run/ams/container_id:ro \
  "${SOURCE_ARGS[@]}" \
  -w "$CONTAINER_WORKDIR" \
  "$IMAGE_ID" \
  scripts/acceptance_entrypoint.sh "$@")"

if [[ ! "$CONTAINER_ID" =~ ^[0-9a-f]{64}$ ]]; then
  printf 'FAIL Docker returned a non-full container ID: %s\n' "$CONTAINER_ID" >&2
  exit 1
fi
printf '%s\n' "$CONTAINER_ID" > "$CONTAINER_ID_FILE"
chmod 0444 "$CONTAINER_ID_FILE"

if ((M0_MODE == 1)); then
  set -o noclobber
  if [[ -n "$(find "$M0_ARTIFACT_STAGING" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
    printf 'FAIL M0 artifact staging ceased to be empty before start\n' >&2
    exit 2
  fi
  docker inspect "$CONTAINER_ID" > "$M0_CONTROL_STAGING/initial_container_inspect.json"
  docker image inspect "$IMAGE_ID" > "$M0_CONTROL_STAGING/initial_image_inspect.json"
  /usr/bin/python3 - "$M0_CONTROL_STAGING" "$CONTAINER_ID" "$IMAGE_ID" \
    "$M0_ARTIFACT_STAGING" <<'PY'
import datetime
import hashlib
import json
import os
import pathlib
import stat
import sys

root = pathlib.Path(sys.argv[1])
container_id, image_id = sys.argv[2:4]
artifact_root = pathlib.Path(sys.argv[4])
if (
    not artifact_root.is_absolute()
    or artifact_root != pathlib.Path(os.path.normpath(str(artifact_root)))
    or artifact_root.resolve(strict=True) != artifact_root
):
    raise SystemExit("artifact staging path is not canonical and symlink-free")
descriptor = os.open(
    artifact_root,
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0),
)
try:
    artifact_info = os.fstat(descriptor)
    opened_root = pathlib.Path(f"/proc/self/fd/{descriptor}").resolve(strict=True)
    if (
        opened_root != artifact_root
        or not stat.S_ISDIR(artifact_info.st_mode)
        or os.listdir(descriptor)
    ):
        raise SystemExit("artifact staging is not one empty real directory")
    artifact_info_after = os.fstat(descriptor)
finally:
    os.close(descriptor)
stable = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_mtime_ns", "st_ctime_ns")
if any(
    getattr(artifact_info, field) != getattr(artifact_info_after, field)
    for field in stable
):
    raise SystemExit("artifact staging changed during prestart inspection")
container_raw = (root / "initial_container_inspect.json").read_bytes()
image_raw = (root / "initial_image_inspect.json").read_bytes()
record = {
    "schema_version": 1,
    "contract": "ams.m0.prestart-inspection/v1",
    "created_utc": datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    ),
    "container_id": container_id,
    "image_id": image_id,
    "artifact_root_initial": {
        "path": str(artifact_root),
        "device": artifact_info.st_dev,
        "inode": artifact_info.st_ino,
        "mode": artifact_info.st_mode & 0o7777,
        "entry_count": 0,
        "content_manifest_sha256": hashlib.sha256(b"[]").hexdigest(),
    },
    "initial_container_inspect": {
        "path": "initial_container_inspect.json",
        "bytes": len(container_raw),
        "sha256": hashlib.sha256(container_raw).hexdigest(),
    },
    "initial_image_inspect": {
        "path": "initial_image_inspect.json",
        "bytes": len(image_raw),
        "sha256": hashlib.sha256(image_raw).hexdigest(),
    },
}
with (root / "prestart_inspection_record.json").open("x", encoding="utf-8") as output:
    json.dump(record, output, indent=2, sort_keys=True)
    output.write("\n")
PY
  chmod 0444 "$M0_CONTROL_STAGING"/*.json
  chmod 0500 "$M0_CONTROL_STAGING"
  set +o noclobber
fi

if ((M1_MODE == 1)); then
  if [[ -n "$(find "$M1_ARTIFACT_STAGING" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
    printf 'FAIL M1 artifact staging ceased to be empty before start\n' >&2
    exit 2
  fi
  docker inspect "$CONTAINER_ID" > "$M1_CONTROL_STAGING/initial_container_inspect.json"
  docker image inspect "$IMAGE_ID" > "$M1_CONTROL_STAGING/initial_image_inspect.json"
  chmod 0444 "$M1_CONTROL_STAGING"/*.json
fi

if ((COMPONENT_MODE == 1)); then
  if [[ -n "$(find "$COMPONENT_ARTIFACT_STAGING" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
    printf 'FAIL component artifact staging ceased to be empty before start\n' >&2
    exit 2
  fi
  docker inspect "$CONTAINER_ID" \
    > "$COMPONENT_CONTROL_STAGING/initial_container_inspect.json"
  docker image inspect "$IMAGE_ID" \
    > "$COMPONENT_CONTROL_STAGING/initial_image_inspect.json"
  chmod 0444 "$COMPONENT_CONTROL_STAGING/initial_container_inspect.json" \
    "$COMPONENT_CONTROL_STAGING/initial_image_inspect.json"
fi

set +e
docker start --attach "$CONTAINER_ID"
RUN_EXIT=$?
set -e

M1_FINAL_EXIT=0
M1_VALIDATION_CONTAINER_ID=""
COMPONENT_FINAL_EXIT=0
COMPONENT_VALIDATION_CONTAINER_ID=""
if ((M1_MODE == 1)); then
  docker inspect "$CONTAINER_ID" > "$M1_CONTROL_STAGING/final_container_inspect.json"
  docker image inspect "$IMAGE_ID" > "$M1_CONTROL_STAGING/final_image_inspect.json"
  if ((RUN_EXIT == 0)); then
    if [[ "$(find "$M1_ARTIFACT_STAGING" -mindepth 1 -maxdepth 1 -printf '%f\n')" != "$M1_RUN_ID" ]] || \
      [[ ! -d "$M1_ARTIFACT_STAGING/$M1_RUN_ID" ]]; then
      printf 'FAIL formal M1 staging does not contain exactly its RUN_ID\n' >&2
      M1_FINAL_EXIT=1
    else
      M1_VALIDATION_CONTAINER_ID="$(docker create \
        --user ubuntu \
        --restart=no \
        --cap-drop=ALL \
        --security-opt=no-new-privileges:true \
        --network=none \
        --read-only \
        --tmpfs /tmp:rw,nosuid,nodev,exec,size=1g,mode=1777 \
        -v "$M1_SOURCE_SNAPSHOT:$CONTAINER_WORKDIR:ro" \
        -v "$M1_ARTIFACT_STAGING:$CONTAINER_WORKDIR/runs:ro" \
        -v "$ROOT_DIR/.external/ns-3:$CONTAINER_WORKDIR/.external/ns-3:ro" \
        -v "$M1_M0_RECEIPT_HOST:/run/ams/m0-receipt.json:ro" \
        -w "$CONTAINER_WORKDIR" \
        "$IMAGE_ID" \
        /usr/bin/python3.10 network/scripts/validate_m1_health.py \
        --run-dir "runs/$M1_RUN_ID" --no-write)"
      if [[ ! "$M1_VALIDATION_CONTAINER_ID" =~ ^[0-9a-f]{64}$ ]]; then
        printf 'FAIL independent M1 validation container ID is malformed\n' >&2
        M1_FINAL_EXIT=1
      else
        docker inspect "$M1_VALIDATION_CONTAINER_ID" \
          > "$M1_CONTROL_STAGING/validation_initial_container_inspect.json"
        docker image inspect "$IMAGE_ID" \
          > "$M1_CONTROL_STAGING/validation_image_inspect.json"
        set +e
        docker start --attach "$M1_VALIDATION_CONTAINER_ID" \
          > "$M1_CONTROL_STAGING/validation_result.json" \
          2> "$M1_CONTROL_STAGING/validation_stderr.txt"
        VALIDATION_EXIT=$?
        set -e
        docker inspect "$M1_VALIDATION_CONTAINER_ID" \
          > "$M1_CONTROL_STAGING/validation_final_container_inspect.json"
        if ((VALIDATION_EXIT != 0)); then
          printf 'FAIL independent exact-image M1 validation failed\n' >&2
          M1_FINAL_EXIT=1
        else
          set +e
          /usr/bin/python3.10 -S "$M1_SOURCE_SNAPSHOT/network/scripts/finalize_m1_host.py" \
            --staging-run-dir "$M1_ARTIFACT_STAGING/$M1_RUN_ID" \
            --publish-run-dir "$ROOT_DIR/runs/$M1_RUN_ID" \
            --source-snapshot "$M1_SOURCE_SNAPSHOT" \
            --project-root "$ROOT_DIR" \
            --source-commit "$M1_SOURCE_COMMIT" \
            --container-id "$CONTAINER_ID" \
            --validation-container-id "$M1_VALIDATION_CONTAINER_ID" \
            --image-reference "$IMAGE" \
            --image-digest "$IMAGE_ID" \
            --initial-control-dir "$M1_CONTROL_STAGING" \
            --independent-result "$M1_CONTROL_STAGING/validation_result.json" \
            --container-identity-file "$CONTAINER_ID_FILE" \
            --m0-status-validation "$M1_CONTROL_STAGING/m0_status_validation.json" \
            --m0-receipt "$M1_M0_RECEIPT_HOST" \
            > "$M1_CONTROL_STAGING/host_final_receipt.stdout.json"
          M1_FINAL_EXIT=$?
          set -e
        fi
      fi
    fi
  else
    M1_FINAL_EXIT="$RUN_EXIT"
  fi
fi

if ((COMPONENT_MODE == 1)); then
  docker inspect "$CONTAINER_ID" \
    > "$COMPONENT_CONTROL_STAGING/final_container_inspect.json"
  docker image inspect "$IMAGE_ID" \
    > "$COMPONENT_CONTROL_STAGING/final_image_inspect.json"
  if ((RUN_EXIT == 0)); then
    if [[ "$(find "$COMPONENT_ARTIFACT_STAGING" -mindepth 1 -maxdepth 1 -printf '%f\n')" \
        != "$COMPONENT_RUN_ID" ]] || \
      [[ ! -d "$COMPONENT_ARTIFACT_STAGING/$COMPONENT_RUN_ID" ]]; then
      printf 'FAIL component staging does not contain exactly its RUN_ID\n' >&2
      COMPONENT_FINAL_EXIT=1
    else
      COMPONENT_VALIDATOR_ARGS=()
      for COMPONENT_ARGUMENT in "${COMPONENT_VALIDATOR_ARG_TEMPLATES[@]}"; do
        if [[ "$COMPONENT_ARGUMENT" == "{run_dir}" ]]; then
          COMPONENT_VALIDATOR_ARGS+=("runs/$COMPONENT_RUN_ID")
        else
          COMPONENT_VALIDATOR_ARGS+=("$COMPONENT_ARGUMENT")
        fi
      done
      COMPONENT_VALIDATION_CONTAINER_ID="$(docker create \
        --user ubuntu \
        --restart=no \
        --cap-drop=ALL \
        --security-opt=no-new-privileges:true \
        --network=none \
        --read-only \
        --tmpfs /tmp:rw,nosuid,nodev,exec,size=1g,mode=1777 \
        -v "$COMPONENT_SOURCE_SNAPSHOT:$CONTAINER_WORKDIR:ro" \
        -v "$COMPONENT_ARTIFACT_STAGING:$CONTAINER_WORKDIR/runs:ro" \
        -v "$ROOT_DIR/.external/ns-3:$CONTAINER_WORKDIR/.external/ns-3:ro" \
        -v "$COMPONENT_STATUS_VALIDATION:/run/ams/status-validation.json:ro" \
        -v "$COMPONENT_PREREQUISITES:/run/ams/prerequisites.json:ro" \
        "${COMPONENT_RECEIPT_MOUNT_ARGS[@]}" \
        -w "$CONTAINER_WORKDIR" \
        "$IMAGE_ID" \
        /usr/bin/python3.10 "$COMPONENT_VALIDATOR" \
        "${COMPONENT_VALIDATOR_ARGS[@]}")"
      if [[ ! "$COMPONENT_VALIDATION_CONTAINER_ID" =~ ^[0-9a-f]{64}$ ]]; then
        printf 'FAIL independent component validation container ID is malformed\n' >&2
        COMPONENT_FINAL_EXIT=1
      else
        docker inspect "$COMPONENT_VALIDATION_CONTAINER_ID" \
          > "$COMPONENT_CONTROL_STAGING/validation_initial_container_inspect.json"
        docker image inspect "$IMAGE_ID" \
          > "$COMPONENT_CONTROL_STAGING/validation_image_inspect.json"
        set +e
        docker start --attach "$COMPONENT_VALIDATION_CONTAINER_ID" \
          > "$COMPONENT_CONTROL_STAGING/validation_result.json" \
          2> "$COMPONENT_CONTROL_STAGING/validation_stderr.txt"
        COMPONENT_VALIDATION_EXIT=$?
        set -e
        docker inspect "$COMPONENT_VALIDATION_CONTAINER_ID" \
          > "$COMPONENT_CONTROL_STAGING/validation_final_container_inspect.json"
        if ((COMPONENT_VALIDATION_EXIT != 0)); then
          printf 'FAIL independent exact-image component validation failed\n' >&2
          COMPONENT_FINAL_EXIT=1
        else
          set +e
          /usr/bin/python3.10 -S \
            "$COMPONENT_SOURCE_SNAPSHOT/network/scripts/finalize_component_host.py" \
            --profile "$COMPONENT_PROFILE" \
            --staging-run-dir \
              "$COMPONENT_ARTIFACT_STAGING/$COMPONENT_RUN_ID" \
            --publish-run-dir "$ROOT_DIR/runs/$COMPONENT_RUN_ID" \
            --source-snapshot "$COMPONENT_SOURCE_SNAPSHOT" \
            --project-root "$ROOT_DIR" \
            --source-commit "$COMPONENT_SOURCE_COMMIT" \
            --container-id "$CONTAINER_ID" \
            --validation-container-id "$COMPONENT_VALIDATION_CONTAINER_ID" \
            --image-reference "$IMAGE" \
            --image-digest "$IMAGE_ID" \
            --control-dir "$COMPONENT_CONTROL_STAGING" \
            --independent-result \
              "$COMPONENT_CONTROL_STAGING/validation_result.json" \
            --container-identity-file "$CONTAINER_ID_FILE" \
            --status-validation "$COMPONENT_STATUS_VALIDATION" \
            --prerequisites "$COMPONENT_PREREQUISITES" \
            "${COMPONENT_RECEIPT_FINALIZER_ARGS[@]}" \
            > "$COMPONENT_CONTROL_STAGING/host_final_receipt.stdout.json"
          COMPONENT_FINAL_EXIT=$?
          set -e
        fi
      fi
    fi
  else
    COMPONENT_FINAL_EXIT="$RUN_EXIT"
  fi
fi

printf '\nAcceptance container retained for independent host validation.\n'
printf 'container_name=%s\n' "$NAME"
printf 'container_id=%s\n' "$CONTAINER_ID"
printf 'image_reference=%s\n' "$IMAGE"
printf 'image_id=%s\n' "$IMAGE_ID"
printf 'run_exit_code=%s\n' "$RUN_EXIT"
if ((M0_MODE == 1)); then
  printf 'm0_source_commit=%s\n' "$M0_SOURCE_COMMIT"
  printf 'm0_source_snapshot=%s\n' "$M0_SOURCE_SNAPSHOT"
  printf 'm0_identity_file=%s\n' "$CONTAINER_ID_FILE"
  printf 'm0_artifact_staging=%s\n' "$M0_ARTIFACT_STAGING"
  printf 'm0_initial_control_dir=%s\n' "$M0_CONTROL_STAGING"
  printf 'host_final_command=network/scripts/run_m0_host_final.sh --run-dir %q --publish-run-dir %q --expected-container-id %q --initial-control-dir %q\n' \
    "$M0_ARTIFACT_STAGING/$M0_RUN_ID" "$ROOT_DIR/runs/$M0_RUN_ID" "$CONTAINER_ID" "$M0_CONTROL_STAGING"
  printf 'M0 snapshot, staging directory, and identity file are retained until host-final validation completes.\n'
fi
if ((M1_MODE == 1)); then
  printf 'm1_source_commit=%s\n' "$M1_SOURCE_COMMIT"
  printf 'm1_source_snapshot=%s\n' "$M1_SOURCE_SNAPSHOT"
  printf 'm1_artifact_staging=%s\n' "$M1_ARTIFACT_STAGING"
  printf 'm1_control_dir=%s\n' "$M1_CONTROL_STAGING"
  printf 'm1_validation_container_id=%s\n' "$M1_VALIDATION_CONTAINER_ID"
  if ((M1_FINAL_EXIT == 0)); then
    printf 'm1_published_run=%s\n' "$ROOT_DIR/runs/$M1_RUN_ID"
  fi
fi
if ((COMPONENT_MODE == 1)); then
  printf 'component_profile=%s\n' "$COMPONENT_PROFILE"
  printf 'component_source_commit=%s\n' "$COMPONENT_SOURCE_COMMIT"
  printf 'component_source_snapshot=%s\n' "$COMPONENT_SOURCE_SNAPSHOT"
  printf 'component_artifact_staging=%s\n' "$COMPONENT_ARTIFACT_STAGING"
  printf 'component_control_dir=%s\n' "$COMPONENT_CONTROL_STAGING"
  printf 'component_validation_container_id=%s\n' \
    "$COMPONENT_VALIDATION_CONTAINER_ID"
  if ((COMPONENT_FINAL_EXIT == 0)); then
    printf 'component_published_run=%s\n' \
      "$ROOT_DIR/runs/$COMPONENT_RUN_ID"
  fi
fi
printf 'Retain it until every validator and any attestation required by the selected profile completes.\n'
if ((M1_MODE == 1)); then
  exit "$M1_FINAL_EXIT"
fi
if ((COMPONENT_MODE == 1)); then
  exit "$COMPONENT_FINAL_EXIT"
fi
exit "$RUN_EXIT"
