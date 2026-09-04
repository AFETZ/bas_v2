#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE="${BAS_CONTAINER_IMAGE:-multiagent_simulation:latest}"
RUN_ID="${BAS_SIONNA_WIFI_RUN_ID:-native-wifi-five-uav-$(date -u +%Y%m%dT%H%M%SZ)}"

run_in_container() {
  command -v docker >/dev/null 2>&1 || { printf 'Docker is required.\n' >&2; return 2; }
  [[ "$RUN_ID" =~ ^[a-zA-Z0-9_.-]+$ ]] || { printf 'Unsafe run ID: %s\n' "$RUN_ID" >&2; return 2; }
  nvidia-smi -L >/dev/null 2>&1 || { printf 'A working NVIDIA GPU is required.\n' >&2; return 2; }
  local image_id
  image_id="$(docker image inspect --format '{{.Id}}' "$IMAGE" 2>/dev/null)" || {
    printf 'Runtime image is unavailable: %s\n' "$IMAGE" >&2
    return 2
  }
  docker run --rm --user 0:0 --gpus all --network host \
    --label bas.product=native-wifi-sionna-five-uav \
    -e NVIDIA_DRIVER_CAPABILITIES=compute,utility,graphics \
    -e BAS_SIONNA_WIFI_IN_CONTAINER=1 \
    -e BAS_SIONNA_WIFI_RUN_ID="$RUN_ID" \
    -e BAS_SOURCE_HEAD="$(git -C "$ROOT_DIR" rev-parse HEAD)" \
    -e BAS_NATIVE_MAP_SOURCES="${BAS_NATIVE_MAP_SOURCES:-network/config/native_jammers_reference.yaml}" \
    -e BAS_NATIVE_MAP_TIME_S="${BAS_NATIVE_MAP_TIME_S:-4}" \
    -e BAS_NATIVE_STUDY="${BAS_NATIVE_STUDY:-}" \
    -e BAS_NATIVE_SOURCES_CAMPAIGN="${BAS_NATIVE_SOURCES_CAMPAIGN:-0}" \
    -e BAS_SIONNA_WIFI_IMAGE_ID="$image_id" \
    -e BAS_SIONNA_WIFI_HOST_UID="$(id -u)" \
    -e BAS_SIONNA_WIFI_HOST_GID="$(id -g)" \
    -e BAS_CONTAINER_IMAGE="$IMAGE" \
    -v "$ROOT_DIR":/workspace/multiagent_simulation \
    -w /workspace/multiagent_simulation \
    "$image_id" ./network/ns3/run_sionna_wifi_five_uav.sh
}

if [[ "${BAS_SIONNA_WIFI_IN_CONTAINER:-0}" != 1 ]]; then
  run_in_container
  exit $?
fi

((EUID == 0)) || { printf 'Root is required in the runtime container.\n' >&2; exit 2; }
NS3_DIR="$ROOT_DIR/.external/ns-3-sionna-native"
PYTHON_DEPS="$NS3_DIR/.python-deps-py310"
PYTHON_TOOLING="$NS3_DIR/.tooling-py310"
RUN_DIR="$ROOT_DIR/runs/$RUN_ID"
SMOKE_SOURCE="$ROOT_DIR/network/ns3/scratch/upstream-sionna-wifi-smoke.cc"
FIVE_SOURCE="$ROOT_DIR/network/ns3/scratch/upstream-sionna-wifi-five-uav.cc"
SMOKE_BINARY="$NS3_DIR/build/scratch/ns3.48-upstream-sionna-wifi-smoke-default"
FIVE_BINARY="$NS3_DIR/build/scratch/ns3.48-upstream-sionna-wifi-five-uav-default"
PATCHES=(
  "$ROOT_DIR/network/ns3/patches/mr2608-spike-compatibility.patch"
  "$ROOT_DIR/network/ns3/patches/mr2608-realtime-scene-cache.patch"
  "$ROOT_DIR/network/ns3/patches/mr2608-spectrumwifi-phased-array-adapter.patch"
)

[[ ! -e "$RUN_DIR" ]] || { printf 'Run directory exists: %s\n' "$RUN_DIR" >&2; exit 2; }
for path in "$NS3_DIR" "$PYTHON_DEPS" "$PYTHON_TOOLING" "$SMOKE_SOURCE" "$FIVE_SOURCE" "${PATCHES[@]}"; do
  [[ -e "$path" ]] || { printf 'Missing required input: %s\n' "$path" >&2; exit 2; }
done
[[ "$(git -c safe.directory="$NS3_DIR" -C "$NS3_DIR" rev-parse HEAD)" == \
   d2add90b452d600cfb4859baed8e9ea633519447 ]] || {
  printf 'Official ns-3.48 exact revision is absent.\n' >&2
  exit 2
}

finish() {
  local status=$?
  if [[ -d "$RUN_DIR" ]]; then
    chown -R "${BAS_SIONNA_WIFI_HOST_UID:-0}:${BAS_SIONNA_WIFI_HOST_GID:-0}" "$RUN_DIR" || true
  fi
  exit "$status"
}
trap finish EXIT

mkdir -p "$RUN_DIR"/{logs,metrics}
printf '%q ' "$0" "$@" > "$RUN_DIR/command.txt"
printf '\n' >> "$RUN_DIR/command.txt"

for patch in "${PATCHES[@]}"; do
  if git -c safe.directory="$NS3_DIR" -C "$NS3_DIR" apply --reverse --check "$patch"; then
    continue
  fi
  git -c safe.directory="$NS3_DIR" -C "$NS3_DIR" apply --check "$patch" || {
    printf 'Patch neither applies nor matches the checkout: %s\n' "$patch" >&2
    exit 2
  }
  git -c safe.directory="$NS3_DIR" -C "$NS3_DIR" apply "$patch"
done

export PATH="$PYTHON_TOOLING/bin:$PATH"
export PYTHONPATH="$PYTHON_TOOLING:$PYTHON_DEPS"
export LD_LIBRARY_PATH="$NS3_DIR/build/lib:${LD_LIBRARY_PATH:-}"
export SIONNA_MITSUBA_VARIANT=cuda_ad_mono_polarized
export MPLCONFIGDIR="$RUN_DIR/matplotlib"
cp "$SMOKE_SOURCE" "$NS3_DIR/scratch/upstream-sionna-wifi-smoke.cc"
cp "$ROOT_DIR/network/ns3/scratch/native-spectrum-sources.h" "$ROOT_DIR/network/ns3/scratch/native-radio-map.h" "$ROOT_DIR/network/ns3/scratch/native-cache-study.h" "$NS3_DIR/scratch/"
cp "$FIVE_SOURCE" "$NS3_DIR/scratch/upstream-sionna-wifi-five-uav.cc"

if [[ ! -f "$NS3_DIR/cmake-cache/CMakeCache.txt" ]]; then
  (
    cd "$NS3_DIR"
    ./ns3 configure --enable-examples --enable-tests --enable-python-bindings
  ) > "$RUN_DIR/logs/configure.log" 2>&1
fi
(
  cd "$NS3_DIR"
  ./ns3 build upstream-sionna-wifi-smoke upstream-sionna-wifi-five-uav
) > "$RUN_DIR/logs/build.log" 2>&1
[[ -x "$SMOKE_BINARY" && -x "$FIVE_BINARY" ]] || {
  printf 'Focused native Wi-Fi targets did not build.\n' >&2
  exit 1
}

{
  printf 'run_id=%s\n' "$RUN_ID"
  printf 'prepared_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'git_head=%s\n' "${BAS_SOURCE_HEAD:-unknown}"
  printf 'container_image=%s\n' "$IMAGE"
  printf 'container_image_id=%s\n' "${BAS_SIONNA_WIFI_IMAGE_ID:-unknown}"
  printf 'ns3_exact_sha=%s\n' "$(git -c safe.directory="$NS3_DIR" -C "$NS3_DIR" rev-parse HEAD)"
  printf 'python=%s\n' "$(python3 --version | awk '{print $2}')"
  PYTHONPATH="$PYTHON_DEPS" python3 - <<'PY'
from importlib.metadata import version
for name in ("sionna", "sionna-rt", "mitsuba", "drjit", "pybind11"):
    print(f"{name}={version(name)}")
PY
} > "$RUN_DIR/environment.txt"

case "${BAS_NATIVE_STUDY:-}" in
  maps)
    python3 "$ROOT_DIR/scripts/product/prepare_native_sources.py" --config "$ROOT_DIR/${BAS_NATIVE_MAP_SOURCES:-network/config/native_jammers_reference.yaml}" --output "$RUN_DIR/sources.json"
    "$SMOKE_BINARY" --scene="$ROOT_DIR/.external/cavise_maps/Town01/map/scene.xml" --sources="$RUN_DIR/sources.json" --heatmapCsv="$RUN_DIR/native_psd_grid.csv" --heatmapTimeS="${BAS_NATIVE_MAP_TIME_S:-4}" --output="$RUN_DIR/map_context.json" > "$RUN_DIR/logs/map.log" 2>&1
    python3 "$ROOT_DIR/scripts/product/town01_heatmaps.py" --run-dir "$RUN_DIR" --native-csv "$RUN_DIR/native_psd_grid.csv"
    exit 0 ;;
  cache)
    "$SMOKE_BINARY" --scene="$ROOT_DIR/.external/cavise_maps/Town01/map/scene.xml" --cacheStudyCsv="$RUN_DIR/cache_samples.csv" --output="$RUN_DIR/cache_context.json" > "$RUN_DIR/logs/cache.log" 2>&1
    exit 0 ;;
  matrix)
    python3 "$ROOT_DIR/scripts/product/native_reference_campaign.py" --run-dir "$RUN_DIR/matrix"
    exit $? ;;
  "") ;;
  *) printf 'Unknown BAS_NATIVE_STUDY\n' >&2; exit 2 ;;
esac

if [[ "${BAS_NATIVE_SOURCES_CAMPAIGN:-0}" == 1 ]]; then
  python3 "$ROOT_DIR/scripts/product/native_source_campaign.py" --binary "$SMOKE_BINARY" \
    --scene "$ROOT_DIR/.external/cavise_maps/Town01/map/scene.xml" --run-dir "$RUN_DIR/campaign"
  exit $?
fi

"$SMOKE_BINARY" --output="$RUN_DIR/metrics/smoke.json" \
  > "$RUN_DIR/logs/smoke.log" 2>&1
"$FIVE_BINARY" --output="$RUN_DIR/metrics/summary.json" \
  > "$RUN_DIR/logs/five_uav.log" 2>&1

python3 "$ROOT_DIR/scripts/product/validate_native_wifi_sionna.py" \
  "$RUN_DIR/metrics/summary.json" \
  > "$RUN_DIR/logs/validation.log"



printf 'Native Wi-Fi/Sionna five-UAV reference passed: %s\n' "$RUN_DIR"
python3 - "$RUN_DIR/metrics/summary.json" <<'PY'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
print(
    f"PDR={value['control_pdr']:.3f} "
    f"RTT_p95_ms={value['control_rtt_p95_ms']:.3f} "
    f"lag_p95_ms={value['scheduler_lag_profile_p95_ms']:.3f} "
    f"fairness={value['jain_fairness']:.3f} "
    f"RTF={value['mean_rtf']:.3f}"
)
PY
