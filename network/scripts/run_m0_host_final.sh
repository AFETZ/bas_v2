#!/usr/bin/bash
set -euo pipefail

# Host-side launcher for the formal receipt.  -S and an explicit system-only
# path prevent the host user site, sitecustomize, and adjacent bytecode from
# influencing the validator itself.
SCRIPT_DIR="${BASH_SOURCE[0]%/*}"
if [[ "$SCRIPT_DIR" == "${BASH_SOURCE[0]}" ]]; then
  SCRIPT_DIR=.
fi
cd -- "$SCRIPT_DIR/../.."
ROOT_DIR="$PWD"
cd "$ROOT_DIR"
exec /usr/bin/env -i \
  PATH=/usr/bin:/bin \
  LANG=C.UTF-8 \
  LC_ALL=C \
  HOME=/nonexistent \
  PYTHONNOUSERSITE=1 \
  PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH="$ROOT_DIR:/usr/local/lib/python3.10/dist-packages:/usr/lib/python3/dist-packages" \
  DOCKER_HOST=unix:///var/run/docker.sock \
  DOCKER_CONFIG=/nonexistent \
  GIT_CONFIG_NOSYSTEM=1 \
  GIT_CONFIG_GLOBAL=/dev/null \
  GIT_NO_REPLACE_OBJECTS=1 \
  GIT_OPTIONAL_LOCKS=0 \
  /usr/bin/python3.10 -S network/scripts/validate_m0_baseline.py \
  --require-host-final "$@"
