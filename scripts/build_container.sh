#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

docker build \
  --platform linux/amd64 \
  --build-arg USER_UID=1000 \
  --build-arg USER_GID=1000 \
  -t multiagent_simulation:latest \
  .devcontainer
