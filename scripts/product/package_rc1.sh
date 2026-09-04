#!/usr/bin/env bash
# Assemble a local review package. Large assets are never uploaded by this script.
set -Eeuo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"
[[ $# == 1 ]] || { printf 'Usage: %s /absolute/new/output-directory\n' "$0" >&2; exit 2; }
OUT="$1"
[[ "$OUT" == /* && "$OUT" != "$ROOT_DIR" && "$OUT" != "$ROOT_DIR/"* && ! -e "$OUT" ]] || {
  printf 'Output must be a new absolute directory outside the source checkout.\n' >&2; exit 2;
}
IMAGE="${BAS_CONTAINER_IMAGE:-multiagent_simulation:latest}"
IMAGE_ID="$(docker image inspect --format '{{.Id}}' "$IMAGE")"
BRANCH="$(git branch --show-current)"
[[ "$BRANCH" == release/bas-v2-rc1 ]] || { printf 'Use the RC1 branch.\n' >&2; exit 2; }
for run in rc1-customer-final-01 rc1-customer-final-02; do
  [[ -f "runs/native-radio-realtime/$run/report.md" ]] || { printf 'Missing final run: %s\n' "$run" >&2; exit 2; }
done
mkdir -p "$OUT/artifacts"
printf 'Saving pinned runtime image...\n'
docker image save "$IMAGE" > "$OUT/runtime-image.tar"
printf 'Packing native dependencies and their relative symlink target...\n'
tar -cf - .external/ns-3-sionna-native .external/upstream_integrations/ns3-mr2608/.python-deps .external/customer-geometry-tools | gzip -1 > "$OUT/native-dependencies.tar.gz"
printf 'Packing local scene assets...\n'
tar -cf - .external/cavise_maps/Town01 .external/customer_10km | gzip -1 > "$OUT/scene-assets.tar.gz"
printf 'Copying complete RC1 run artifacts, including failed diagnostics...\n'
shopt -s nullglob
RUNS=(runs/native-radio-realtime/rc1-* runs/rc1-*)
tar -cf - "${RUNS[@]}" | tar -xf - -C "$OUT/artifacts"
git bundle create "$OUT/source.bundle" "$BRANCH"
git archive --format=tar.gz -o "$OUT/source.tar.gz" HEAD
printf 'source_head=%s\nbranch=%s\nruntime_image=%s\nruntime_image_id=%s\n' \
  "$(git rev-parse HEAD)" "$BRANCH" "$IMAGE" "$IMAGE_ID" > "$OUT/versions.txt"
cat > "$OUT/README.txt" <<'EOF'
BAS v2 RC1 — local software review package
Start with source/doc/VALIDATION_REPORT.md and source/doc/USER_GUIDE.md after clone.

Offline source restore:
  git clone --branch release/bas-v2-rc1 source.bundle source
  docker load -i runtime-image.tar
  tar -xzf native-dependencies.tar.gz -C source
  tar -xzf scene-assets.tar.gz -C source
  cd source
  make demo-preflight DEMO_GUI=0 DEMO_BOOTSTRAP=1
  make prepare-customer

Use USER_GUIDE.md to run the customer scenario or connect the existing MAVProxy.
The runtime image still requires a compatible host NVIDIA driver and Docker GPU support.
artifacts/runs contains original raw images, native radiotap, CSV, logs and reports.
Run environment.txt records the runtime commit; report_reprocessing.txt records later
offline analysis. No failed diagnostic was relabelled as a successful runtime.

Physical FC validation is blocked_external. This is not flight-HIL certification.
CAVISE/ CARLA redistribution terms were not supplied; included assets stay in this
authorized local workspace. Confirm source terms before third-party asset transfer.
No runtime image or scene assets are uploaded by the packaging script.
EOF
printf 'Checking archive readability...\n'
tar -tf "$OUT/runtime-image.tar" > /dev/null
tar -tzf "$OUT/native-dependencies.tar.gz" > /dev/null
tar -tzf "$OUT/scene-assets.tar.gz" > /dev/null
tar -tzf "$OUT/source.tar.gz" > /dev/null
git bundle verify "$OUT/source.bundle" > "$OUT/bundle-check.txt" 2>&1
printf 'Local package prepared: %s\n' "$OUT"
