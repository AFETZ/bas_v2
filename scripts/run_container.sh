#!/usr/bin/env bash
set -euo pipefail

IMAGE="${IMAGE:-multiagent_simulation:latest}"
if ! IMAGE_DIGEST="$(docker image inspect --format '{{.Id}}' "$IMAGE")"; then
  printf 'FAIL runtime image is unavailable: %s\n' "$IMAGE" >&2
  exit 1
fi
if [[ ! "$IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  printf 'FAIL docker inspect returned a non-SHA256 image ID: %s\n' "$IMAGE_DIGEST" >&2
  exit 1
fi

cd "$(dirname "$0")/.."

XAUTHORITY_FILE="${XAUTHORITY:-${HOME}/.Xauthority}"
XAUTHORITY_ARGS=()
if [[ -f "$XAUTHORITY_FILE" ]]; then
  XAUTHORITY_ARGS=(-e XAUTHORITY=/tmp/.docker.xauth -v "$XAUTHORITY_FILE":/tmp/.docker.xauth:ro)
fi

NVIDIA_EGL_VENDOR_JSON="${NVIDIA_EGL_VENDOR_JSON:-/usr/share/glvnd/egl_vendor.d/10_nvidia.json}"
NVIDIA_EGL_ARGS=()
if [[ -f "$NVIDIA_EGL_VENDOR_JSON" ]]; then
  NVIDIA_EGL_ARGS=(
    -v "$NVIDIA_EGL_VENDOR_JSON":/usr/share/glvnd/egl_vendor.d/10_nvidia.json:ro
    -e __EGL_VENDOR_LIBRARY_FILENAMES=/usr/share/glvnd/egl_vendor.d/10_nvidia.json
  )
fi

docker run -it --rm \
  --privileged \
  --gpus 'all,"capabilities=graphics,utility,compute,display"' \
  --network=host \
  -e DISPLAY="${DISPLAY:-}" \
  -e NVIDIA_VISIBLE_DEVICES=all \
  -e NVIDIA_DRIVER_CAPABILITIES=graphics,utility,compute,display \
  -e AMS_CONTAINER_IMAGE="$IMAGE" \
  -e AMS_CONTAINER_IMAGE_DIGEST="$IMAGE_DIGEST" \
  -e AMS_CONTAINER_IMAGE_DIGEST_SOURCE=docker_image_inspect_host \
  -e QT_X11_NO_MITSHM=1 \
  -v /tmp/.X11-unix:/tmp/.X11-unix \
  "${XAUTHORITY_ARGS[@]}" \
  "${NVIDIA_EGL_ARGS[@]}" \
  -v "$PWD":/workspace/multiagent_simulation \
  -w /workspace/multiagent_simulation \
  "$IMAGE_DIGEST" \
  bash -lc '
    set -eo pipefail
    source /opt/ros/humble/setup.bash
    source /workspace/ardu_ws/install/setup.bash
    if [[ -f install/setup.bash ]]; then
      source install/setup.bash
    fi
    export GZ_VERSION=harmonic
    export GZ_SIM_RESOURCE_PATH="${GZ_SIM_RESOURCE_PATH:-}:$PWD/src/multiagent_simulation/models:$PWD/src/multiagent_simulation/worlds:$PWD/src"
    exec bash
  '
