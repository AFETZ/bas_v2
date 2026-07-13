#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${IMAGE:-multiagent_simulation:latest}"
NAME="${CONTAINER_NAME:-ams-acceptance-$(date -u +%Y%m%dT%H%M%SZ)-$$}"

if (($# == 0)); then
  printf 'Usage: %s COMMAND [ARG ...]\n' "$0" >&2
  printf 'Runs one retained acceptance container by immutable image ID.\n' >&2
  exit 2
fi
if [[ ! "$NAME" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]]; then
  printf 'FAIL unsafe Docker container name: %s\n' "$NAME" >&2
  exit 2
fi
if ! IMAGE_ID="$(docker image inspect --format '{{.Id}}' "$IMAGE")"; then
  printf 'FAIL runtime image is unavailable: %s\n' "$IMAGE" >&2
  exit 1
fi
if [[ ! "$IMAGE_ID" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  printf 'FAIL Docker returned a non-immutable image ID: %s\n' "$IMAGE_ID" >&2
  exit 1
fi

CONTAINER_ID_FILE="$(mktemp /tmp/ams-container-id.XXXXXXXXXX)"
cleanup_identity_file() {
  rm -f "$CONTAINER_ID_FILE"
}
trap cleanup_identity_file EXIT

GPU_ARGS=()
if [[ "${AMS_ENABLE_GPU:-0}" == "1" ]]; then
  GPU_ARGS=(
    --gpus 'all,"capabilities=graphics,utility,compute,display"'
    -e NVIDIA_VISIBLE_DEVICES=all
    -e NVIDIA_DRIVER_CAPABILITIES=graphics,utility,compute,display
  )
fi

CONTAINER_ID="$(docker create \
  --name "$NAME" \
  --privileged \
  --network=host \
  "${GPU_ARGS[@]}" \
  -e AMS_CONTAINER_IMAGE="$IMAGE" \
  -e AMS_CONTAINER_IMAGE_DIGEST="$IMAGE_ID" \
  -e AMS_CONTAINER_IMAGE_DIGEST_SOURCE=docker_image_inspect_host \
  -e AMS_RUNTIME_CONTAINER_ID_FILE=/run/ams/container_id \
  -e GZ_VERSION=harmonic \
  -v "$CONTAINER_ID_FILE":/run/ams/container_id:ro \
  -v "$ROOT_DIR":/workspace/multiagent_simulation \
  -w /workspace/multiagent_simulation \
  "$IMAGE_ID" \
  bash -lc '
    set -eo pipefail
    source /opt/ros/humble/setup.bash
    source /workspace/ardu_ws/install/setup.bash
    if [[ -f install/setup.bash ]]; then
      source install/setup.bash
    fi
    export GZ_VERSION=harmonic
    export GZ_SIM_RESOURCE_PATH="${GZ_SIM_RESOURCE_PATH:-}:$PWD/src/multiagent_simulation/models:$PWD/src/multiagent_simulation/worlds:$PWD/src"
    exec "$@"
  ' bash "$@")"

if [[ ! "$CONTAINER_ID" =~ ^[0-9a-f]{64}$ ]]; then
  printf 'FAIL Docker returned a non-full container ID: %s\n' "$CONTAINER_ID" >&2
  exit 1
fi
printf '%s\n' "$CONTAINER_ID" > "$CONTAINER_ID_FILE"
chmod 0444 "$CONTAINER_ID_FILE"

set +e
docker start --attach "$CONTAINER_ID"
RUN_EXIT=$?
set -e

printf '\nAcceptance container retained for host attestation.\n'
printf 'container_name=%s\n' "$NAME"
printf 'container_id=%s\n' "$CONTAINER_ID"
printf 'image_reference=%s\n' "$IMAGE"
printf 'image_id=%s\n' "$IMAGE_ID"
printf 'run_exit_code=%s\n' "$RUN_EXIT"
printf 'Do not remove this container until attest_run_evidence.py succeeds.\n'
exit "$RUN_EXIT"
