#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

XAUTHORITY_FILE="${XAUTHORITY:-${HOME}/.Xauthority}"
XAUTHORITY_ARGS=()
if [[ -f "$XAUTHORITY_FILE" ]]; then
  XAUTHORITY_ARGS=(-e XAUTHORITY=/tmp/.docker.xauth -v "$XAUTHORITY_FILE":/tmp/.docker.xauth:ro)
fi

docker run -it --rm \
  --privileged \
  --gpus all \
  --network=host \
  -e DISPLAY="${DISPLAY:-}" \
  -e NVIDIA_VISIBLE_DEVICES=all \
  -e NVIDIA_DRIVER_CAPABILITIES=all \
  -e QT_X11_NO_MITSHM=1 \
  -v /tmp/.X11-unix:/tmp/.X11-unix \
  "${XAUTHORITY_ARGS[@]}" \
  -v "$PWD":/workspace/multiagent_simulation \
  -w /workspace/multiagent_simulation \
  multiagent_simulation \
  bash -lc '
    set -eo pipefail
    source /opt/ros/humble/setup.bash
    source /workspace/ardu_ws/install/setup.bash
    source install/setup.bash
    export GZ_VERSION=harmonic
    export GZ_SIM_RESOURCE_PATH="${GZ_SIM_RESOURCE_PATH:-}:$PWD/src/multiagent_simulation/models:$PWD/src/multiagent_simulation/worlds:$PWD/src"
    ros2 launch multiagent_simulation multiagent_simulation.launch.py "$@"
  ' bash "$@"
