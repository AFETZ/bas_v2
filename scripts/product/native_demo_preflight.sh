#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE="${BAS_CONTAINER_IMAGE:-multiagent_simulation:latest}"
NS3_DIR="$ROOT_DIR/.external/ns-3-sionna-native"
NS3_REPOSITORY="https://gitlab.com/nsnam/ns-3-dev.git"
NS3_TAG="ns-3.48"
NS3_SHA="d2add90b452d600cfb4859baed8e9ea633519447"
PYTHON_DEPS="$NS3_DIR/.python-deps-py310"
PYTHON_TOOLING="$NS3_DIR/.tooling-py310"
NATIVE_SOURCE="$ROOT_DIR/network/ns3/scratch/upstream-sionna-tap-spike.cc"
NATIVE_COPY="$NS3_DIR/scratch/upstream-sionna-tap-spike.cc"
NATIVE_BINARY="$NS3_DIR/build/scratch/ns3.48-upstream-sionna-tap-spike-default"
NATIVE_RUNNER="$ROOT_DIR/network/ns3/run_native_radio_five_uav.sh"
NATIVE_SCENARIO_DRIVER="$ROOT_DIR/scripts/product/native_radio_five_uav_scenario.py"
PATCHES=(
  "$ROOT_DIR/network/ns3/patches/mr2608-spike-compatibility.patch"
  "$ROOT_DIR/network/ns3/patches/mr2608-realtime-scene-cache.patch"
  "$ROOT_DIR/network/ns3/patches/mr2608-spectrumwifi-phased-array-adapter.patch"
)
EXPECTED_PATCHED_FILES=(
  src/spectrum/model/half-duplex-ideal-phy.cc
  src/spectrum/model/half-duplex-ideal-phy.h
  src/spectrum/model/multi-model-spectrum-channel.cc
  src/spectrum/model/sionna-rt-channel-model.cc
  src/spectrum/model/sionna-rt-channel-model.h
)

SCENARIO="town01"
BOOTSTRAP=0
GUI=0
failures=0
warnings=0
staging_paths=()

usage() {
  cat <<'EOF'
Usage: scripts/product/native_demo_preflight.sh [options]

Validate the complete native five-UAV demo runtime without changing it.  The
explicit bootstrap mode creates only missing ignored dependencies; it never
resets or replaces an existing checkout or dependency directory.

Options:
  --scenario town01|rock_demo  Map profile to check (default: town01)
  --gui                        Also require a usable local X11 display
  --bootstrap                  Create missing image, workspace, ns-3 checkout,
                               patches, and pinned Python 3.10 dependencies
  -h, --help                   Show this help

Town01 assets are not downloaded automatically.  If they are absent, place the
official archive under CAVISE_MAPS_DIR and rerun with --bootstrap.
EOF
}

while (($#)); do
  case "$1" in
    --scenario)
      shift
      (($#)) || { printf 'ERROR: --scenario requires a value.\n' >&2; exit 2; }
      SCENARIO="$1"
      ;;
    --gui)
      GUI=1
      ;;
    --bootstrap)
      BOOTSTRAP=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'ERROR: unknown option: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

case "$SCENARIO" in
  town01|rock_demo) ;;
  *)
    printf 'ERROR: unsupported demo scenario: %s\n' "$SCENARIO" >&2
    exit 2
    ;;
esac

pass() {
  printf 'PASS %-24s %s\n' "$1" "$2"
}

warn() {
  printf 'WARN %-24s %s\n' "$1" "$2"
  warnings=$((warnings + 1))
}

fail() {
  printf 'FAIL %-24s %s\n' "$1" "$2" >&2
  failures=$((failures + 1))
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 2
}

cleanup() {
  local path
  for path in "${staging_paths[@]}"; do
    case "$path" in
      "$ROOT_DIR/.external/".native-demo-*) rm -rf -- "$path" ;;
    esac
  done
}
trap cleanup EXIT

has_command() {
  command -v "$1" >/dev/null 2>&1
}

image_id() {
  docker image inspect --format '{{.Id}}' "$IMAGE" 2>/dev/null
}

ensure_image() {
  image_id >/dev/null && return 0
  ((BOOTSTRAP == 1)) || return 1
  [[ "$IMAGE" == multiagent_simulation:latest ]] || die \
    "custom image $IMAGE is absent; build or pull it explicitly before bootstrap"
  printf 'BOOTSTRAP building missing runtime image %s\n' "$IMAGE"
  "$ROOT_DIR/scripts/build_container.sh"
  image_id >/dev/null || die "runtime image build did not create $IMAGE"
}

ensure_town01_assets() {
  local scene="$ROOT_DIR/.external/cavise_maps/Town01/map/scene.xml"
  local world="$ROOT_DIR/.external/cavise_maps/Town01/gazebo/town01.sdf"
  [[ -f "$scene" ]] || {
    ((BOOTSTRAP == 1)) || return 1
    [[ -n "${CAVISE_MAPS_DIR:-}" ]] || die \
      "Town01 is absent; set CAVISE_MAPS_DIR to the official bundle directory"
    "$ROOT_DIR/scripts/product/prepare_cavise_map.sh" \
      --prepare-selected --allow-large-extract
  }
  if [[ ! -f "$world" ]]; then
    ((BOOTSTRAP == 1)) || return 1
    python3 "$ROOT_DIR/scripts/product/prepare_town01_gazebo.py"
  fi
  [[ -f "$scene" && -f "$world" ]]
}

ensure_ns3_checkout() {
  if [[ -d "$NS3_DIR/.git" ]]; then
    return 0
  fi
  [[ ! -e "$NS3_DIR" && ! -L "$NS3_DIR" ]] || die \
    "refusing to replace non-Git path: $NS3_DIR"
  ((BOOTSTRAP == 1)) || return 1

  mkdir -p "$ROOT_DIR/.external"
  local stage
  stage="$(mktemp -d "$ROOT_DIR/.external/.native-demo-ns3.XXXXXX")"
  staging_paths+=("$stage")
  printf 'BOOTSTRAP cloning official ns-3 tag %s\n' "$NS3_TAG"
  git clone --filter=blob:none --branch "$NS3_TAG" --single-branch \
    "$NS3_REPOSITORY" "$stage/checkout"
  local actual
  actual="$(git -C "$stage/checkout" rev-parse HEAD)"
  [[ "$actual" == "$NS3_SHA" ]] || die \
    "official $NS3_TAG resolved to $actual, expected $NS3_SHA"
  mv "$stage/checkout" "$NS3_DIR"
  rmdir "$stage"
  staging_paths=()
}

patch_applied() {
  local patch="$1"
  git -c safe.directory="$NS3_DIR" -C "$NS3_DIR" \
    apply --reverse --check "$patch" >/dev/null 2>&1
}

ensure_patches() {
  local patch
  for patch in "${PATCHES[@]}"; do
    [[ -f "$patch" ]] || die "required patch is missing: $patch"
    if patch_applied "$patch"; then
      continue
    fi
    ((BOOTSTRAP == 1)) || return 1
    git -c safe.directory="$NS3_DIR" -C "$NS3_DIR" \
      apply --check "$patch" >/dev/null 2>&1 || die \
      "patch neither applies nor matches the checkout: $patch"
    printf 'BOOTSTRAP applying %s\n' "$(basename "$patch")"
    git -c safe.directory="$NS3_DIR" -C "$NS3_DIR" apply "$patch"
  done
}

container_path() {
  local host_path="$1"
  [[ "$host_path" == "$ROOT_DIR"/* ]] || die "path is outside the repository: $host_path"
  printf '/workspace/multiagent_simulation/%s\n' "${host_path#"$ROOT_DIR/"}"
}

python_deps_ok() {
  local path="${1:-$PYTHON_DEPS}"
  [[ -d "$path" ]] || return 1
  local runtime_path
  runtime_path="$(container_path "$path")"
  docker run --rm -i \
    -v "$ROOT_DIR:/workspace/multiagent_simulation" \
    -w /workspace/multiagent_simulation \
    -e "PYTHONPATH=$runtime_path" \
    "$(image_id)" python3 - >/dev/null 2>&1 <<'PY'
from importlib.metadata import version

expected = {
    "sionna": "1.2.0",
    "sionna-rt": "1.2.0",
    "mitsuba": "3.7.1",
    "drjit": "1.2.0",
    "tensorflow": "2.21.0",
    "numpy": "1.26.4",
    "scipy": "1.15.3",
    "matplotlib": "3.10.9",
    "importlib-resources": "7.1.0",
    "ipywidgets": "8.1.9",
    "pythreejs": "2.4.2",
    "typing-extensions": "4.16.0",
    "pybind11": "2.11.1",
    "cppyy": "3.5.0",
}
assert all(version(name) == wanted for name, wanted in expected.items())
import cppyy  # noqa: E402,F401
import pybind11  # noqa: E402,F401
import sionna.rt  # noqa: E402,F401
PY
}

tooling_ok() {
  local path="${1:-$PYTHON_TOOLING}"
  [[ -x "$path/bin/cmake" ]] || return 1
  local runtime_path
  runtime_path="$(container_path "$path")"
  docker run --rm \
    -v "$ROOT_DIR:/workspace/multiagent_simulation" \
    -w /workspace/multiagent_simulation \
    -e "PYTHONPATH=$runtime_path" \
    "$(image_id)" "$runtime_path/bin/cmake" --version 2>/dev/null \
    | head -n1 | grep -Fxq 'cmake version 3.31.6'
}

install_python_target() {
  local destination="$1"
  shift
  [[ ! -e "$destination" && ! -L "$destination" ]] || die \
    "refusing to replace existing dependency path: $destination"
  local stage target runtime_target
  stage="$(mktemp -d "$ROOT_DIR/.external/.native-demo-python.XXXXXX")"
  staging_paths+=("$stage")
  target="$stage/target"
  mkdir -p "$target"
  runtime_target="$(container_path "$target")"
  docker run --rm \
    --user "$(id -u):$(id -g)" \
    -e HOME=/tmp/bas-native-demo-home \
    -v "$ROOT_DIR:/workspace/multiagent_simulation" \
    -w /workspace/multiagent_simulation \
    "$(image_id)" python3 -m pip install --disable-pip-version-check \
      --target "$runtime_target" "$@"
  mv "$target" "$destination"
  rmdir "$stage"
  staging_paths=()
}

ensure_python_dependencies() {
  if python_deps_ok; then
    return 0
  fi
  ((BOOTSTRAP == 1)) || return 1
  [[ ! -e "$PYTHON_DEPS" && ! -L "$PYTHON_DEPS" ]] || die \
    "Python dependency directory exists but is not the pinned runtime: $PYTHON_DEPS"
  printf 'BOOTSTRAP installing pinned Python 3.10 Sionna runtime\n'
  install_python_target "$PYTHON_DEPS" \
    'sionna==1.2.0' 'sionna-rt==1.2.0' \
    'mitsuba==3.7.1' 'drjit==1.2.0' \
    'tensorflow==2.21.0' 'numpy==1.26.4' 'scipy==1.15.3' \
    'matplotlib==3.10.9' 'importlib-resources==7.1.0' \
    'ipywidgets==8.1.9' 'pythreejs==2.4.2' 'typing-extensions==4.16.0' \
    'pybind11==2.11.1' 'cppyy==3.5.0'
  python_deps_ok || die "pinned Python dependency verification failed"
}

ensure_python_tooling() {
  if tooling_ok; then
    return 0
  fi
  ((BOOTSTRAP == 1)) || return 1
  [[ ! -e "$PYTHON_TOOLING" && ! -L "$PYTHON_TOOLING" ]] || die \
    "Python tooling directory exists but is not the pinned runtime: $PYTHON_TOOLING"
  printf 'BOOTSTRAP installing pinned CMake tooling\n'
  install_python_target "$PYTHON_TOOLING" 'cmake==3.31.6'
  tooling_ok || die "pinned CMake verification failed"
}

workspace_ready() {
  [[ -f "$ROOT_DIR/install/setup.bash" ]] || return 1
  docker run --rm \
    -v "$ROOT_DIR:/workspace/multiagent_simulation" \
    -w /workspace/multiagent_simulation \
    "$(image_id)" bash -lc '
      set -eo pipefail
      source /opt/ros/humble/setup.bash
      source /workspace/ardu_ws/install/setup.bash
      source /workspace/multiagent_simulation/install/setup.bash
      ros2 pkg prefix multiagent_simulation >/dev/null
    ' >/dev/null 2>&1
}

ensure_workspace() {
  workspace_ready && return 0
  ((BOOTSTRAP == 1)) || return 1
  printf 'BOOTSTRAP building the project ROS workspace\n'
  docker run --rm \
    --user "$(id -u):$(id -g)" \
    -e HOME=/tmp/bas-native-demo-home \
    -v "$ROOT_DIR:/workspace/multiagent_simulation" \
    -w /workspace/multiagent_simulation \
    "$(image_id)" bash -lc '
      set -eo pipefail
      mkdir -p "$HOME"
      source /opt/ros/humble/setup.bash
      source /workspace/ardu_ws/install/setup.bash
      colcon build --symlink-install
    '
  workspace_ready || die "ROS workspace build completed without a usable package"
}

check_patched_files() {
  local modified unexpected=()
  while IFS= read -r modified; do
    [[ -n "$modified" ]] || continue
    local allowed=0 expected
    for expected in "${EXPECTED_PATCHED_FILES[@]}"; do
      [[ "$modified" == "$expected" ]] && allowed=1
    done
    ((allowed == 1)) || unexpected+=("$modified")
  done < <(git -c safe.directory="$NS3_DIR" -C "$NS3_DIR" diff --name-only)
  ((${#unexpected[@]} == 0)) || {
    fail "ns3:tracked-changes" "unexpected paths: ${unexpected[*]}"
    return 1
  }
  pass "ns3:tracked-changes" "only the three product patches modify tracked files"
}

check_demo_inputs() {
  local path
  local missing=()
  local required=(
    "$NATIVE_RUNNER"
    "$NATIVE_SCENARIO_DRIVER"
    "$NATIVE_SOURCE"
    "$ROOT_DIR/scripts/product/inject_native_radio_runtime_cameras.py"
    "$ROOT_DIR/network/config/town01_sitl.parm"
    "${PATCHES[@]}"
  )
  if [[ "$SCENARIO" == town01 ]]; then
    required+=(
      "$ROOT_DIR/network/config/scenario_5uav_town01_native_product.yaml"
      "$ROOT_DIR/network/config/native_wifi_80211n_spectrum_product.yaml"
      "$ROOT_DIR/network/ns3/runtime_live_cameras.sdf.inc"
    )
  else
    required+=(
      "$ROOT_DIR/network/config/scenario_5uav_rock_demo_native_product.yaml"
      "$ROOT_DIR/network/config/native_wifi_rugged_village_product.yaml"
      "$ROOT_DIR/network/ns3/runtime_rugged_village_cameras.sdf.inc"
    )
  fi
  for path in "${required[@]}"; do
    [[ -f "$path" ]] || missing+=("${path#"$ROOT_DIR/"}")
  done
  if ((${#missing[@]} == 0)) && [[ -x "$NATIVE_RUNNER" ]]; then
    pass "demo:inputs" "native runner and $SCENARIO product inputs are present"
  else
    [[ -x "$NATIVE_RUNNER" ]] || missing+=("network/ns3/run_native_radio_five_uav.sh (not executable)")
    fail "demo:inputs" "missing or unusable: ${missing[*]}"
  fi
}

if ((BOOTSTRAP == 1)); then
  has_command docker || die "Docker is required for bootstrap"
  has_command git || die "Git is required for bootstrap"
  has_command python3 || die "Python 3 is required for Town01 preparation"
  docker info >/dev/null 2>&1 || die "Docker daemon is unavailable or access is denied"
  ensure_image || die "runtime image is unavailable after bootstrap"
  if [[ "$SCENARIO" == town01 ]]; then
    ensure_town01_assets || die "Town01 preparation did not produce both runtime scenes"
  fi
  ensure_ns3_checkout || die "native ns-3 checkout is unavailable after bootstrap"
  [[ "$(git -c safe.directory="$NS3_DIR" -C "$NS3_DIR" rev-parse HEAD)" == "$NS3_SHA" ]] || die \
    "existing native ns-3 checkout is not exact $NS3_SHA; it was left untouched"
  ensure_patches || die "native ns-3 patches are incomplete"
  ensure_python_dependencies || die "native Python dependencies are incomplete"
  ensure_python_tooling || die "native build tooling is incomplete"
  ensure_workspace || die "project ROS workspace is incomplete"
fi

for command_name in docker git python3; do
  if has_command "$command_name"; then
    pass "command:$command_name" "$(command -v "$command_name")"
  else
    fail "command:$command_name" "required on the host"
  fi
done

check_demo_inputs

runtime_image_id=""
docker_ready=0
if has_command docker && docker info >/dev/null 2>&1; then
  docker_ready=1
  pass "container:daemon" "Docker daemon is reachable"
elif has_command docker; then
  fail "container:daemon" "Docker daemon is unavailable or access is denied"
fi

if ((docker_ready == 1)) && runtime_image_id="$(image_id)"; then
  pass "container:image" "$IMAGE ($runtime_image_id)"
elif ((docker_ready == 1)); then
  fail "container:image" "$IMAGE is absent; rerun with --bootstrap"
else
  fail "container:image" "cannot inspect $IMAGE until Docker is reachable"
fi

if has_command nvidia-smi && nvidia-smi -L >/dev/null 2>&1; then
  pass "gpu:host" "$(nvidia-smi -L | head -n1)"
else
  fail "gpu:host" "a working NVIDIA GPU is required; no propagation fallback is used"
fi

if [[ -n "$runtime_image_id" ]]; then
  if docker run --rm --gpus all "$runtime_image_id" nvidia-smi -L >/dev/null 2>&1; then
    pass "gpu:container" "NVIDIA container runtime is usable"
  else
    fail "gpu:container" "docker --gpus all cannot access the NVIDIA GPU"
  fi
  if docker run --rm \
    -v "$ROOT_DIR:/workspace/multiagent_simulation" \
    -w /workspace/multiagent_simulation \
    "$runtime_image_id" bash -lc '
      set -eo pipefail
      for name in c++ gz ip nproc python3 ros2 socat ss stdbuf taskset tcpdump; do
        command -v "$name" >/dev/null
      done
      [[ "$(python3 -c "import sys; print(f\"{sys.version_info.major}.{sys.version_info.minor}\")")" == 3.10 ]]
    ' >/dev/null 2>&1
  then
    pass "container:commands" "native runtime commands and Python 3.10 are present"
  else
    fail "container:commands" "runtime image lacks a required command or Python 3.10"
  fi
fi

if ((GUI == 1)); then
  if [[ "${DISPLAY:-}" =~ :([0-9]+)(\.[0-9]+)?$ ]] \
    && [[ -S "/tmp/.X11-unix/X${BASH_REMATCH[1]}" ]]; then
    pass "gui:x11" "DISPLAY=$DISPLAY"
  else
    fail "gui:x11" "DISPLAY and its X11 socket must be available"
  fi
  if [[ -f "${XAUTHORITY:-${HOME}/.Xauthority}" ]]; then
    pass "gui:xauthority" "${XAUTHORITY:-${HOME}/.Xauthority}"
  else
    warn "gui:xauthority" "no Xauthority file; local xhost policy must permit the container"
  fi
fi

if [[ "$SCENARIO" == town01 ]]; then
  if ensure_town01_assets; then
    pass "map:town01" "canonical Sionna scene and Gazebo derivative are present"
  else
    fail "map:town01" "assets absent; set CAVISE_MAPS_DIR and rerun with --bootstrap"
  fi
else
  rugged_files=(
    "$ROOT_DIR/network/config/scenario_5uav_rock_demo_native_product.yaml"
    "$ROOT_DIR/network/config/native_wifi_rugged_village_product.yaml"
    "$ROOT_DIR/network/ns3/runtime_rugged_village_cameras.sdf.inc"
    "$ROOT_DIR/src/multiagent_simulation/worlds/rock_demo/rock_demo.sdf"
    "$ROOT_DIR/src/multiagent_simulation/worlds/rock_demo/sionna_scene.xml"
    "$ROOT_DIR/src/multiagent_simulation/worlds/rock_demo/engineering_terrain.obj"
    "$ROOT_DIR/src/multiagent_simulation/worlds/rock_demo/engineering_buildings.obj"
    "$ROOT_DIR/src/multiagent_simulation/worlds/rock_demo/radio_blocker.obj"
  )
  missing_rugged=()
  for path in "${rugged_files[@]}"; do
    [[ -f "$path" ]] || missing_rugged+=("$path")
  done
  if ((${#missing_rugged[@]} == 0)); then
    pass "map:rock_demo" "aligned checked-in Gazebo/Sionna engineering geometry is present"
  else
    fail "map:rock_demo" "missing ${#missing_rugged[@]} required file(s)"
  fi
fi

if [[ -d "$NS3_DIR/.git" ]]; then
  actual_sha="$(git -c safe.directory="$NS3_DIR" -C "$NS3_DIR" rev-parse HEAD 2>/dev/null || true)"
  if [[ "$actual_sha" == "$NS3_SHA" ]]; then
    pass "ns3:revision" "$actual_sha"
  else
    fail "ns3:revision" "got ${actual_sha:-unreadable}; expected $NS3_SHA"
  fi
  patches_ready=1
  for patch in "${PATCHES[@]}"; do
    if patch_applied "$patch"; then
      pass "ns3:patch" "$(basename "$patch")"
    else
      fail "ns3:patch" "not exactly applied: $(basename "$patch")"
      patches_ready=0
    fi
  done
  check_patched_files || true
else
  fail "ns3:checkout" "$NS3_DIR is absent; rerun with --bootstrap"
fi

if [[ -n "$runtime_image_id" ]] && python_deps_ok; then
  pass "python:pins" "Sionna 1.2.0, Sionna RT 1.2.0, pybind11 2.11.1, cppyy 3.5.0"
else
  fail "python:pins" "pinned target is absent or invalid; rerun with --bootstrap"
fi

if [[ -n "$runtime_image_id" ]] && tooling_ok; then
  pass "cmake:pin" "3.31.6"
else
  fail "cmake:pin" "pinned CMake target is absent or invalid; rerun with --bootstrap"
fi

if [[ -n "$runtime_image_id" ]] && workspace_ready; then
  pass "ros:workspace" "multiagent_simulation is discoverable in the mounted install"
else
  fail "ros:workspace" "mounted workspace is not built; rerun with --bootstrap"
fi

if [[ -x "$NATIVE_BINARY" && -f "$NATIVE_COPY" ]] \
  && cmp -s "$NATIVE_SOURCE" "$NATIVE_COPY"; then
  pass "ns3:target" "focused native target is built and source-synchronized"
else
  warn "ns3:target" "the demo runner will rebuild the focused target"
fi

if docker inspect --format '{{.State.Running}}' \
  "${BAS_NATIVE_FIVE_CONTAINER_NAME:-bas-v2-native-radio-five-uav}" 2>/dev/null \
  | grep -Fxq true; then
  fail "runtime:exclusive" "native five-UAV demo container is already running"
else
  pass "runtime:exclusive" "no conflicting native demo container"
fi

printf 'Native demo preflight (%s): %d failure(s), %d warning(s).\n' \
  "$SCENARIO" "$failures" "$warnings"
((failures == 0))
