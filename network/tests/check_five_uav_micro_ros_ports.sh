#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

python3 - "$ROOT_DIR" <<'PY'
from pathlib import Path
import sys

import yaml

root = Path(sys.argv[1])
launch_path = root / "src/multiagent_simulation/launch/multiagent_simulation.launch.py"
default_robots_path = root / "src/multiagent_simulation/config/robots.yaml"
scenario_path = root / "network/config/scenario_5uav.yaml"

errors = []

launch_text = launch_path.read_text(encoding="utf-8")
launch_requirements = {
    "reads dds_udp_port from robot config with 2019 + instance fallback": (
        'robot.get("dds_udp_port", 2019 + instance)' in launch_text
    ),
    "passes per-robot port to micro_ros_agent": (
        '"port": f"{dds_udp_port}"' in launch_text
    ),
    "writes a per-instance DDS_UDP_PORT override file": (
        "DDS_UDP_PORT" in launch_text and "create_dds_udp_params_file" in launch_text
    ),
    "appends the DDS override file to ArduPilot defaults": (
        "dds_udp_params" in launch_text and '"defaults": defaults_file' in launch_text
    ),
    "runs SITL from per-instance directories": (
        '"use_instance_dir": "True"' in launch_text
    ),
    "uses absolute tty paths for socat and SITL instance cwd": (
        'dev_dir = Path.cwd() / "dev"' in launch_text
        and 'tty0 = str(dev_dir / f"ttyROS{instance * 10}")' in launch_text
        and 'tty1 = str(dev_dir / f"ttyROS{instance * 10 + 1}")' in launch_text
    ),
}

for description, passed in launch_requirements.items():
    if not passed:
        errors.append(f"launch contract missing: {description}")

if "./dev/ttyROS" in launch_text:
    errors.append("launch still contains relative ./dev/ttyROS paths")

def load_robots(path):
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)["robots"]

default_robots = load_robots(default_robots_path)
default_ports = [robot.get("dds_udp_port") for robot in default_robots]
if default_ports != [2019, 2020]:
    errors.append(f"default robots DDS ports should be [2019, 2020], got {default_ports}")

scenario_robots = load_robots(scenario_path)
scenario_names = [robot.get("name") for robot in scenario_robots]
scenario_instances = [robot.get("instance") for robot in scenario_robots]
scenario_sysids = [robot.get("system_id") for robot in scenario_robots]
scenario_ports = [robot.get("dds_udp_port") for robot in scenario_robots]

if scenario_names != [f"uav{i}" for i in range(1, 6)]:
    errors.append(f"scenario_5uav names are not uav1..uav5: {scenario_names}")
if scenario_instances != list(range(5)):
    errors.append(f"scenario_5uav instances should be 0..4, got {scenario_instances}")
if scenario_sysids != list(range(1, 6)):
    errors.append(f"scenario_5uav system IDs should be 1..5, got {scenario_sysids}")
if scenario_ports != [2019, 2020, 2021, 2022, 2023]:
    errors.append(f"scenario_5uav DDS ports should be 2019..2023, got {scenario_ports}")
if len(scenario_ports) != len(set(scenario_ports)):
    errors.append(f"scenario_5uav DDS ports are not unique: {scenario_ports}")

if errors:
    for error in errors:
        print(f"FAIL {error}", file=sys.stderr)
    raise SystemExit(1)

print("PASS five-UAV micro_ros_agent DDS UDP ports are unique and wired through launch/config")
PY
