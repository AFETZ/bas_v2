#!/usr/bin/env bash
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
failures=0
warnings=0

pass() {
  printf 'PASS %-24s %s\n' "$1" "$2"
}

fail() {
  printf 'FAIL %-24s %s\n' "$1" "$2" >&2
  failures=$((failures + 1))
}

warn() {
  printf 'WARN %-24s %s\n' "$1" "$2"
  warnings=$((warnings + 1))
}

require_command() {
  local command_name="$1"
  local hint="$2"
  if command -v "$command_name" >/dev/null 2>&1; then
    pass "command:$command_name" "$(command -v "$command_name")"
  else
    fail "command:$command_name" "$hint"
  fi
}

optional_command() {
  local command_name="$1"
  local hint="$2"
  if command -v "$command_name" >/dev/null 2>&1; then
    pass "command:$command_name" "$(command -v "$command_name")"
  else
    warn "command:$command_name" "$hint"
  fi
}

require_command git "Install Git."
require_command bash "Install Bash."
require_command python3 "Install Python 3."
require_command colcon "Source the ROS environment and install colcon."
require_command ros2 "Source ROS 2 and the ArduPilot workspace."
require_command setsid "Install util-linux for managed product process groups."
optional_command gz "Install/source Gazebo Harmonic before running the base simulation."

for required_path in \
  network/config/scenario_5uav.yaml \
  network/config/endpoints.yaml \
  src/multiagent_simulation/launch/multiagent_simulation.launch.py; do
  if [[ -f "$ROOT_DIR/$required_path" ]]; then
    pass "file:$required_path" "present"
  else
    fail "file:$required_path" "Required product file is missing."
  fi
done

if command -v python3 >/dev/null 2>&1 && python3 - <<'PY' >/dev/null 2>&1
import yaml
PY
then
  pass "python:yaml" "PyYAML importable"
else
  fail "python:yaml" "Install PyYAML in the product environment."
fi

if command -v ros2 >/dev/null 2>&1; then
  for package in multiagent_simulation ardupilot_sitl ros_gz_sim; do
    if ros2 pkg prefix "$package" >/dev/null 2>&1; then
      pass "ros2:$package" "$(ros2 pkg prefix "$package")"
    else
      fail "ros2:$package" "Build the workspace and source install/setup.bash."
    fi
  done
fi

if [[ -x "$ROOT_DIR/.external/ns-3/ns3" || -x "$ROOT_DIR/.external/ns-3/waf" ]]; then
  pass "network:ns-3" "external checkout available"
else
  warn "network:ns-3" "Network runs need a built external ns-3 checkout."
fi

if command -v python3 >/dev/null 2>&1 && python3 - <<'PY' >/dev/null 2>&1
import sionna.rt
PY
then
  pass "network:sionna-rt" "Python module importable"
else
  warn "network:sionna-rt" "High-fidelity network runs need sionna.rt."
fi

printf 'Environment check: %d failure(s), %d warning(s).\n' "$failures" "$warnings"
((failures == 0))
