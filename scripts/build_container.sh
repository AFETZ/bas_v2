#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

docker build \
  --build-arg USER_UID="$(id -u)" \
  --build-arg USER_GID="$(id -g)" \
  -t multiagent_simulation \
  .devcontainer
