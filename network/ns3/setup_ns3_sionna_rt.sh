#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NS3_SIONNA_DIR="${NS3_SIONNA_DIR:-$ROOT_DIR/.external/ns-3-sionna}"
NS3_SIONNA_REPO="${NS3_SIONNA_REPO:-https://gitlab.com/AAshtari/ns-3-dev.git}"
NS3_SIONNA_REF="${NS3_SIONNA_REF:-SionnaChannelModelIntegration}"
INSTALL_PYTHON_DEPS="${INSTALL_PYTHON_DEPS:-1}"

if [[ ! -d "$NS3_SIONNA_DIR/.git" ]]; then
  mkdir -p "$(dirname "$NS3_SIONNA_DIR")"
  git clone --depth 1 --branch "$NS3_SIONNA_REF" "$NS3_SIONNA_REPO" "$NS3_SIONNA_DIR"
fi

if (( INSTALL_PYTHON_DEPS )); then
  python3 - <<'PY' >/dev/null 2>&1 || python3 -m pip install --user 'pybind11==2.11.1' 'cppyy==3.5.0'
import cppyy
import pybind11
PY
fi

python3 - <<'PY'
import cppyy
import pybind11
import sionna.rt
PY

python3 - "$NS3_SIONNA_DIR/src/spectrum/model/sionna-rt-channel-model.cc" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text()
old = '''        // Load from XML file using Scene constructor
        NS_LOG_DEBUG("Loading scene from XML file: " << m_sceneConfigs.sceneName);
        py::object Scene = rt.attr("Scene");
        scene = Scene(py::arg("filename") = m_sceneConfigs.sceneName,
                      py::arg("merge_shapes") = m_sceneConfigs.mergeShapes);
'''
new = '''        // Load from XML file using the Sionna RT 1.2+ public loader.
        NS_LOG_DEBUG("Loading scene from XML file: " << m_sceneConfigs.sceneName);
        py::object load_scene = rt.attr("load_scene");
        scene = load_scene(m_sceneConfigs.sceneName,
                           py::arg("merge_shapes") = m_sceneConfigs.mergeShapes);
'''
if old in text:
    path.write_text(text.replace(old, new))
PY

PY_SITE="$(python3 - <<'PY'
import site
print(site.getusersitepackages())
PY
)"
export PYTHONPATH="$PY_SITE:${PYTHONPATH:-}"

if [[ -z "${NS3_CMAKE_GENERATOR:-}" ]]; then
  if command -v ninja >/dev/null 2>&1; then
    NS3_CMAKE_GENERATOR="Ninja"
  else
    NS3_CMAKE_GENERATOR="Unix Makefiles"
  fi
fi

cache_file="$NS3_SIONNA_DIR/cmake-cache/CMakeCache.txt"
if [[ -f "$cache_file" ]]; then
  cached_source_dir="$(sed -n 's/^CMAKE_HOME_DIRECTORY:INTERNAL=//p' "$cache_file" | head -n 1)"
  cached_generator="$(sed -n 's/^CMAKE_GENERATOR:INTERNAL=//p' "$cache_file" | head -n 1)"
  current_source_dir="$(cd "$NS3_SIONNA_DIR" && pwd -P)"
  if [[ -n "$cached_source_dir" && "$cached_source_dir" != "$current_source_dir" ]] ||
     [[ -n "$cached_generator" && "$cached_generator" != "$NS3_CMAKE_GENERATOR" ]]; then
    printf 'Removing stale ns-3 CMake cache: cached source %s, current source %s, cached generator %s, current generator %s\n' \
      "${cached_source_dir:-unknown}" "$current_source_dir" "${cached_generator:-unknown}" "$NS3_CMAKE_GENERATOR"
    rm -rf "$NS3_SIONNA_DIR/cmake-cache" "$NS3_SIONNA_DIR/build"
  fi
fi

(
  cd "$NS3_SIONNA_DIR"
  configure_log="$(mktemp)"
  ./ns3 configure -G "$NS3_CMAKE_GENERATOR" \
    --enable-python-bindings \
    --enable-examples \
    --disable-tests \
    --filter-module-examples-and-tests=spectrum \
    >"$configure_log" 2>&1 || {
      cat "$configure_log"
      rm -f "$configure_log"
      exit 1
    }
  cat "$configure_log"
  if ! grep -q 'Sionna-RT support enabled' "$configure_log"; then
    rm -f "$configure_log"
    printf 'FAIL ns-3 Sionna-RT support was not enabled during configure.\n' >&2
    exit 2
  fi
  rm -f "$configure_log"
)

printf 'ns-3 Sionna RT checkout ready: %s\n' "$NS3_SIONNA_DIR"
