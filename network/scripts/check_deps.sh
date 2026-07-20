#!/usr/bin/env bash
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REQUIRED_FAILURES=0
WARNINGS=0
QUALIFICATION_PROFILE="diagnostic"

if (($# == 2)) && [[ "$1" == "--qualification-profile" ]] && [[ "$2" == "m0" ]]; then
  QUALIFICATION_PROFILE="m0"
elif (($# != 0)); then
  printf 'Usage: %s [--qualification-profile m0]\n' "$0" >&2
  exit 2
fi

declare -A QUALIFICATION_PATH_OWNERS=()
if [[ "$QUALIFICATION_PROFILE" == "m0" ]]; then
  if ! QUALIFICATION_OWNER_LINES="$(
    /usr/bin/python3.10 - "$ROOT_DIR/network/config/qualification_path_ownership.json" <<'PY'
import json
import re
import sys
from pathlib import PurePosixPath


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate policy key: {key}")
        result[key] = value
    return result


with open(sys.argv[1], "rb") as source:
    policy = json.loads(
        source.read().decode("utf-8", errors="strict"),
        object_pairs_hook=unique_pairs,
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError(f"non-finite policy value: {value}")
        ),
    )
nodes = [f"Q{index}" for index in range(9)]
explicit = policy.get("explicit_owners") if isinstance(policy, dict) else None
if (
    policy.get("schema_version") != 2
    or policy.get("contract") != "q0_q1_q2_granular/v1"
    or policy.get("policy_id") != "q0_q1_q2_granular/v1"
    or policy.get("default_owner") != "Q0"
    or not isinstance(explicit, dict)
    or set(explicit) != set(nodes)
    or explicit.get("Q0") != []
):
    raise SystemExit("qualification ownership policy is not exact")
seen = set()
for node in nodes[1:]:
    paths = explicit[node]
    if (
        not isinstance(paths, list)
        or paths != sorted(paths)
        or len(paths) != len(set(paths))
        or not all(isinstance(path, str) for path in paths)
    ):
        raise SystemExit(f"qualification owner list is not exact: {node}")
    for path in paths:
        pure = PurePosixPath(path)
        if (
            pure.is_absolute()
            or any(part in {"", ".", ".."} for part in pure.parts)
            or re.fullmatch(r"[A-Za-z0-9_./+@-]+", path) is None
            or path in seen
        ):
            raise SystemExit(f"unsafe or duplicate qualification path: {path!r}")
        seen.add(path)
        print(f"{node}\t{path}")
PY
  )"; then
    printf 'FAIL qualification ownership policy could not be resolved\n' >&2
    exit 2
  fi
  while IFS=$'\t' read -r owner relative; do
    if [[ -n "$owner" && -n "$relative" ]]; then
      QUALIFICATION_PATH_OWNERS["$relative"]="$owner"
    fi
  done <<< "$QUALIFICATION_OWNER_LINES"
fi

pass() {
  printf 'PASS %-28s %s\n' "$1" "$2"
}

fail() {
  printf 'FAIL %-28s %s\n' "$1" "$2"
  REQUIRED_FAILURES=$((REQUIRED_FAILURES + 1))
}

warn() {
  printf 'WARN %-28s %s\n' "$1" "$2"
  WARNINGS=$((WARNINGS + 1))
}

check_command() {
  local name="$1"
  local required="$2"
  local hint="$3"

  if command -v "$name" >/dev/null 2>&1; then
    pass "cmd:$name" "$(command -v "$name")"
  elif [[ "$required" == "required" ]]; then
    fail "cmd:$name" "$hint"
  else
    warn "cmd:$name" "$hint"
  fi
}

check_mavlink_bridge_runtime() {
  if command -v mavlink-routerd >/dev/null 2>&1; then
    pass "cmd:mavlink-routerd" "$(command -v mavlink-routerd)"
    return
  fi

  if [[ -f "$ROOT_DIR/network/bridge/priority_udp_bridge.py" ]] &&
    python3 "$ROOT_DIR/network/bridge/priority_udp_bridge.py" --self-test >/dev/null 2>&1; then
    pass "bridge:priority_udp" "mavlink-routerd unavailable; using accepted priority UDP bridge runtime"
    return
  fi

  fail "cmd:mavlink-routerd" "Install mavlink-router, or keep network/bridge/priority_udp_bridge.py passing as the documented bridge runtime."
}

check_python_package() {
  local module="$1"
  local label="$2"
  local required="$3"
  local hint="$4"

  if ! command -v python3 >/dev/null 2>&1; then
    fail "python:$label" "python3 is missing; cannot check Python package $module."
    return
  fi

  if python3 - "$module" <<'PY' >/dev/null 2>&1
import importlib.util
import sys

sys.exit(0 if importlib.util.find_spec(sys.argv[1]) else 1)
PY
  then
    pass "python:$label" "module '$module' importable"
  elif [[ "$required" == "required" ]]; then
    fail "python:$label" "$hint"
  else
    warn "python:$label" "$hint"
  fi
}

check_ros_package() {
  local package="$1"

  if ! command -v ros2 >/dev/null 2>&1; then
    fail "ros2:$package" "ros2 is missing; source ROS 2 and the workspace setup, or run inside the project container."
    return
  fi

  if ros2 pkg prefix "$package" >/dev/null 2>&1; then
    pass "ros2:$package" "$(ros2 pkg prefix "$package")"
  else
    fail "ros2:$package" "ROS package '$package' not found; run rosdep/colcon build and source install/setup.bash."
  fi
}

check_path() {
  local label="$1"
  local path="$2"
  local required="$3"
  local hint="$4"

  if [[ -e "$path" ]]; then
    pass "$label" "$path"
  elif [[ "$required" == "required" ]]; then
    fail "$label" "$hint"
  else
    warn "$label" "$hint"
  fi
}

check_project_path() {
  local label="$1"
  local relative="$2"
  local required="$3"
  local hint="$4"
  if [[ "$QUALIFICATION_PROFILE" == "m0" ]] && \
    [[ "${QUALIFICATION_PATH_OWNERS[$relative]:-Q0}" != "Q0" ]]; then
    return
  fi
  check_path "$label" "$ROOT_DIR/$relative" "$required" "$hint"
}

check_ns3() {
  if [[ -x "$ROOT_DIR/.external/ns-3/ns3" ]]; then
    pass "ns3:launcher" "$ROOT_DIR/.external/ns-3/ns3"
  elif [[ -x "$ROOT_DIR/.external/ns-3/waf" ]]; then
    pass "ns3:waf" "$ROOT_DIR/.external/ns-3/waf"
  else
    fail "ns3" "Missing ns-3 checkout/build under .external/ns-3; do not vendor it into source."
  fi
}

check_docker() {
  if [[ -f /.dockerenv ]]; then
    pass "docker:runtime" "Already running inside the project runtime container; Docker daemon is not required here"
    return
  fi

  if ! command -v docker >/dev/null 2>&1; then
    fail "docker" "Install Docker or run from an environment with the ROS/Gazebo/ArduPilot dependencies already sourced."
    return
  fi

  if docker info >/dev/null 2>&1; then
    pass "docker:daemon" "Docker daemon reachable"
    if docker image inspect multiagent_simulation >/dev/null 2>&1; then
      pass "docker:image" "multiagent_simulation"
    else
      fail "docker:image" "Build the project image with: docker build -t multiagent_simulation .devcontainer"
    fi
  else
    fail "docker:daemon" "Docker is installed but the daemon is not reachable or this user lacks permission."
    fail "docker:image" "Cannot inspect multiagent_simulation until the Docker daemon is reachable."
  fi
}

check_network_privilege() {
  if [[ "${AMS_M0_CAPABILITY_PROBE_MODE:-}" == \
      "host_final_isolated_exact_image" ]]; then
    pass "netns:privilege" \
      "deferred to isolated host-final exact-image capability probe without source/artifact mounts"
    return
  fi
  if ! command -v unshare >/dev/null 2>&1; then
    fail "netns:unshare" "Install util-linux for unshare; namespace checks require it."
    return
  fi

  if unshare -rn true >/dev/null 2>&1; then
    pass "netns:privilege" "Able to create a temporary network namespace"
  else
    fail "netns:privilege" "Need CAP_SYS_ADMIN/CAP_NET_ADMIN or a privileged container for namespaces, veth, TAP, and ns-3 isolation."
  fi
}

check_gpu() {
  local required
  required="$(python3 - "$ROOT_DIR/network/config/dependency_lock.yaml" <<'PY'
import sys
import yaml

try:
    lock = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
    value = lock["runtime_policy"]["gpu_required"]
except Exception:
    raise SystemExit(2)
if not isinstance(value, bool):
    raise SystemExit(2)
print("true" if value else "false")
PY
)" || {
    fail "cuda:policy" "Cannot read runtime_policy.gpu_required from dependency lock."
    return
  }
  if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
    pass "cuda:gpu" "nvidia-smi reports at least one GPU"
  elif [[ "$required" == "true" ]]; then
    fail "cuda:gpu" "The dependency lock requires a CUDA-capable GPU, but none is visible."
  else
    warn "cuda:gpu" "No GPU is visible; the accepted locked Mitsuba variant is CPU-based."
  fi
}

printf 'Network/radio dependency check\n'
printf 'Repository: %s\n\n' "$ROOT_DIR"

check_project_path "config:scenario" "network/config/scenario_5uav.yaml" required "Missing network/config/scenario_5uav.yaml."
check_project_path "config:endpoints" "network/config/endpoints.yaml" required "Missing network/config/endpoints.yaml."
check_project_path "config:service_tiers" "network/config/service_tiers.yaml" required "Missing network/config/service_tiers.yaml."
check_project_path "config:radio_backend" "network/config/radio_backend.yaml" required "Missing network/config/radio_backend.yaml."
check_project_path "config:radio_24ghz" "network/config/radio_24ghz.yaml" required "Missing network/config/radio_24ghz.yaml."
check_project_path "config:jammers" "network/config/jammers.yaml" required "Missing network/config/jammers.yaml."
check_project_path "config:hitl_loopback" "network/config/hitl_loopback.yaml" required "Missing network/config/hitl_loopback.yaml."
check_project_path "config:validation_matrix" "network/config/validation_matrix.yaml" required "Missing network/config/validation_matrix.yaml."
check_project_path "config:metrics_schema" "network/config/metrics_summary_schema.json" required "Missing network/config/metrics_summary_schema.json."
check_project_path "component:sionna_provider" "network/radio_provider/provider.py" required "Missing network/radio_provider/provider.py."
check_project_path "component:live_sinr_monitor" "network/radio_provider/live_sinr_monitor.py" required "Missing network/radio_provider/live_sinr_monitor.py."
check_project_path "component:position_tracker" "network/position_tracker/tracker.py" required "Missing network/position_tracker/tracker.py."
check_project_path "component:ns3_core" "network/ns3/scratch/ams-radio-core.cc" required "Missing network/ns3/scratch/ams-radio-core.cc."
check_project_path "component:bridge" "network/bridge/bridge_config.py" required "Missing network/bridge/bridge_config.py."
check_project_path "component:hitl" "network/hitl/hitl_loopback.py" required "Missing network/hitl/hitl_loopback.py."
check_project_path "cmd:sionna_provider" "network/scripts/run_sionna_provider.sh" required "Missing network/scripts/run_sionna_provider.sh."
check_project_path "cmd:live_sinr_demo" "network/scripts/run_live_sinr_demo.sh" required "Missing network/scripts/run_live_sinr_demo.sh."
check_project_path "cmd:radio_heatmaps" "network/scripts/generate_radio_heatmaps.sh" required "Missing network/scripts/generate_radio_heatmaps.sh."
check_project_path "cmd:position_tracker" "network/scripts/run_position_tracker.sh" required "Missing network/scripts/run_position_tracker.sh."
check_project_path "cmd:hitl_loopback" "network/scripts/run_hitl_loopback.sh" required "Missing network/scripts/run_hitl_loopback.sh."
check_project_path "cmd:validation" "network/scripts/run_validation.sh" required "Missing network/scripts/run_validation.sh."
check_project_path "cmd:artifact_collection" "network/scripts/collect_artifacts.sh" required "Missing network/scripts/collect_artifacts.sh."

check_command bash required "Install bash."
check_command python3 required "Install python3."
check_command ip required "Install iproute2 for namespaces/veth/TAP diagnostics."
check_command bridge required "Install iproute2 for bridge membership diagnostics."
check_command ss required "Install iproute2 for socket inventory diagnostics."
check_command nft required "Install nftables for fail-closed firewall/NAT evidence."
check_command iptables-save required "Install iptables for IPv4 firewall/NAT evidence."
check_command ip6tables-save required "Install iptables for IPv6 firewall/NAT evidence."
check_command unshare required "Install util-linux for network namespace diagnostics."
check_command tc required "Install iproute2 traffic-control tools."
check_command tcpdump required "Install tcpdump for PCAP capture."
check_command ros2 required "Install/source ROS 2 Humble or use the project container."
check_command colcon required "Install colcon via ros-dev-tools or python3-colcon-common-extensions."
check_command gz required "Install/source Gazebo Harmonic tools or use the project container."
check_mavlink_bridge_runtime

check_docker
check_network_privilege
check_gpu
check_ns3

check_python_package yaml PyYAML required "Install PyYAML, for example: python3 -m pip install PyYAML"
check_python_package numpy NumPy required "Install NumPy in the Sionna provider environment."
check_python_package matplotlib matplotlib required "Install matplotlib for radio heatmap generation."
check_python_package pymavlink pymavlink required "Install pymavlink for MAVLink endpoint diagnostics."
check_python_package sionna.rt SionnaRT required "Install Sionna RT in an external environment; do not vendor it into this repo."
if python3 "$ROOT_DIR/network/scripts/check_python_runtime_compat.py" \
  --lock "$ROOT_DIR/network/config/dependency_lock.yaml"; then
  pass "python:runtime_compat" "accepted pins and ROS/Sionna ABI imports verified"
else
  fail "python:runtime_compat" "Python ABI/package compatibility gate failed; rebuild the pinned runtime image."
fi

if [[ "$QUALIFICATION_PROFILE" == "diagnostic" ]]; then
  check_ros_package multiagent_simulation
fi
check_ros_package ardupilot_sitl
check_ros_package ros_gz_sim
check_ros_package ros_gz_bridge
check_ros_package ros_gz_image

printf '\n'
if (( REQUIRED_FAILURES > 0 )); then
  printf 'Dependency check failed: %d required item(s) missing or unusable, %d warning(s).\n' "$REQUIRED_FAILURES" "$WARNINGS"
  printf 'The full packet-in-the-loop demo must not be launched until these items are fixed.\n'
  exit 1
fi

printf 'Dependency check passed with %d warning(s).\n' "$WARNINGS"
