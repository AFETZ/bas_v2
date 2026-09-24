#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CONTAINER_NAME="${BAS_NETWORK_CONTAINER_NAME:-bas-v2-network}"

run_in_container() {
  command -v docker >/dev/null 2>&1 || {
    printf 'Docker is required for the isolated ns-3 communication runtime.\n' >&2
    return 2
  }
  local image="${BAS_CONTAINER_IMAGE:-multiagent_simulation:latest}"
  local image_id
  image_id="$(docker image inspect --format '{{.Id}}' "$image" 2>/dev/null)" || {
    printf 'Runtime image is unavailable: %s (no rebuild was attempted).\n' "$image" >&2
    return 2
  }
  if [[ "$(docker inspect --format '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null || true)" == "true" ]]; then
    printf 'Communication runtime container is already running: %s\n' "$CONTAINER_NAME" >&2
    return 3
  fi

  local -a gpu_args=()
  if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
    gpu_args=(
      --gpus all
      -e NVIDIA_VISIBLE_DEVICES=all
      -e NVIDIA_DRIVER_CAPABILITIES=graphics,utility,compute
    )
  fi

  printf 'Starting communication vertical slice in existing image %s.\n' "$image_id"
  set +e
  docker run --rm \
    --name "$CONTAINER_NAME" \
    --label bas.product=network \
    --privileged \
    --network=host \
    --user 0:0 \
    "${gpu_args[@]}" \
    -e BAS_NETWORK_IN_CONTAINER=1 \
    -e HOME=/tmp/bas-network-home \
    -e XDG_RUNTIME_DIR=/tmp/bas-network-xdg \
    -e PYTHONPATH=/home/ubuntu/.local/lib/python3.10/site-packages \
    -v "$ROOT_DIR":/workspace/multiagent_simulation \
    -w /workspace/multiagent_simulation \
    "$image_id" \
    bash -lc '
      set -eo pipefail
      mkdir -p "$HOME" "$XDG_RUNTIME_DIR"
      chmod 700 "$HOME" "$XDG_RUNTIME_DIR"
      set +u
      source /opt/ros/humble/setup.bash
      source /workspace/ardu_ws/install/setup.bash
      source /workspace/multiagent_simulation/install/setup.bash
      export PATH="/home/ubuntu/.local/bin:$PATH"
      export GZ_VERSION=harmonic
      export GZ_SIM_RESOURCE_PATH="${GZ_SIM_RESOURCE_PATH:-}:$PWD/src/multiagent_simulation/models:$PWD/src/multiagent_simulation/worlds:$PWD/src"
      set -u
      cmp -s network/ns3/scratch/ams-tap-vertical-slice.cc .external/ns-3/scratch/ams-tap-vertical-slice.cc || \
        install -m 0644 network/ns3/scratch/ams-tap-vertical-slice.cc .external/ns-3/scratch/ams-tap-vertical-slice.cc
      (cd .external/ns-3 && ./ns3 build scratch/ams-tap-vertical-slice)
      exec ./scripts/product/run_network.sh
    '
  local status=$?
  set -e
  if [[ "$status" -eq 137 || "$status" -eq 143 ]]; then
    return 0
  fi
  return "$status"
}

if [[ "${BAS_NETWORK_IN_CONTAINER:-0}" != "1" ]]; then
  run_in_container
  exit $?
fi

exec "$ROOT_DIR/network/scripts/run_communication_vertical_slice.sh" "$@"
