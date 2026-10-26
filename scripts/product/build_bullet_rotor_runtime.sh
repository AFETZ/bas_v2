#!/usr/bin/env bash
# Build only the pinned upstream Bullet Featherstone engine with a declared
# coordinate-velocity configuration. Run from the integration runtime slot.
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE="${BAS_CONTAINER_IMAGE:-multiagent_simulation:latest}"
REVISION=189471c86fa06ffad9c9bdb199f6928eb2fc02f3
ENGINE_DIR="$ROOT_DIR/.external/gz-physics7-rotor"
SOURCE_DIR="$ENGINE_DIR/source"
PATCH="$ROOT_DIR/scripts/product/patches/gz-physics7-iris-coordinate-velocity.patch"
mkdir -p "$ENGINE_DIR"

if [[ ! -d "$SOURCE_DIR/.git" ]]; then
  [[ ! -e "$SOURCE_DIR" ]] || { printf 'Refusing to replace %s\n' "$SOURCE_DIR" >&2; exit 2; }
  git clone --filter=blob:none --single-branch --branch gz-physics7_7.8.0 \
    https://github.com/gazebosim/gz-physics.git "$SOURCE_DIR"
fi
[[ "$(git -C "$SOURCE_DIR" rev-parse HEAD)" == "$REVISION" ]] || {
  printf 'Existing physics checkout differs from pinned 7.8.0 revision.\n' >&2; exit 2;
}
if ! git -C "$SOURCE_DIR" apply --reverse --check "$PATCH" >/dev/null 2>&1; then
  git -C "$SOURCE_DIR" apply --check "$PATCH"
  git -C "$SOURCE_DIR" apply "$PATCH"
fi
[[ "$(git -C "$SOURCE_DIR" diff --name-only)" == bullet-featherstone/src/SDFFeatures.cc ]] || {
  printf 'Unexpected physics source changes; refusing an untracked build variant.\n' >&2; exit 2;
}

IMAGE_ID="$(docker image inspect --format '{{.Id}}' "$IMAGE")"
docker run --rm --user 0:0 \
  -v "$ROOT_DIR:/workspace/multiagent_simulation" \
  -w /workspace/multiagent_simulation "$IMAGE_ID" bash -c '
    set -Eeuo pipefail
    [[ "$(dpkg-query -W -f="\${Version}" libgz-physics7 2>/dev/null)" == 7.8.0-* ]]
    engine_dir="$PWD/.external/gz-physics7-rotor"
    cmake -S "$engine_dir/source" -B "$engine_dir/build" \
      -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF
    cmake --build "$engine_dir/build" \
      --target gz-physics7-bullet-featherstone-plugin --parallel 4
    mapfile -t plugins < <(find "$engine_dir/build" -type f \
      -name libgz-physics7-bullet-featherstone-plugin.so.7.8.0)
    ((${#plugins[@]} == 1))
    mkdir -p "$engine_dir/lib"
    cp "${plugins[0]}" "$engine_dir/lib/"
    cd "$engine_dir/lib"
    ln -sfn libgz-physics7-bullet-featherstone-plugin.so.7.8.0 \
      libgz-physics7-bullet-featherstone-plugin.so.7
    ln -sfn libgz-physics7-bullet-featherstone-plugin.so.7.8.0 \
      libgz-physics7-bullet-featherstone-plugin.so
    ln -sfn libgz-physics7-bullet-featherstone-plugin.so.7.8.0 \
      libgz-physics-bullet-featherstone-plugin.so
  ' > "$ENGINE_DIR/build.log" 2>&1

python3 - "$ENGINE_DIR" "$PATCH" "$IMAGE_ID" "$REVISION" <<'PY'
import hashlib
import json
import sys
from pathlib import Path
directory, patch = map(Path, sys.argv[1:3])
library = directory / "lib/libgz-physics7-bullet-featherstone-plugin.so.7.8.0"
value = dict(provider="upstream_gz_physics_bullet_featherstone", version="7.8.0",
             source_revision=sys.argv[4], image_id=sys.argv[3],
             patch_sha256=hashlib.sha256(patch.read_bytes()).hexdigest(),
             library_sha256=hashlib.sha256(library.read_bytes()).hexdigest(),
             default_max_coordinate_velocity_rad_s=100,
             selected_max_coordinate_velocity_rad_s=1000,
             scope="engine configuration; native dynamics and rotor LiftDrag unchanged")
(directory / "build_manifest.json").write_text(json.dumps(value, indent=2) + "\n")
print(json.dumps(value, indent=2))
PY
