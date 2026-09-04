#!/usr/bin/env bash
set -Eeuo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
python3 "$ROOT_DIR/scripts/product/prepare_town01_gazebo.py"
docker run --rm --user 0:0 -v "$ROOT_DIR:/workspace/multiagent_simulation" \
  -w /workspace/multiagent_simulation \
  -e PYTHONPATH=/workspace/multiagent_simulation/.external/customer-geometry-tools:/workspace/multiagent_simulation/.external/ns-3-sionna-native/.python-deps-py310 \
  "${BAS_CONTAINER_IMAGE:-multiagent_simulation:latest}" bash -c '
    set -e
    if [[ ! -d .external/customer-geometry-tools ]]; then
      python3 -m pip install --no-deps --target .external/customer-geometry-tools shapely==2.1.1
    fi
    python3 -c "import shapely; assert shapely.__version__ == \"2.1.1\""
    python3 scripts/product/prepare_customer_scene.py
  '
