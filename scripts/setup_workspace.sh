#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

docker run --rm \
  --privileged \
  --network=host \
  -e DISPLAY="${DISPLAY:-}" \
  -e QT_X11_NO_MITSHM=1 \
  -v /tmp/.X11-unix:/tmp/.X11-unix \
  -v "$PWD":/workspace/multiagent_simulation \
  -w /workspace/multiagent_simulation \
  multiagent_simulation \
  bash /workspace/setup.sh
