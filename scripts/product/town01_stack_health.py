#!/usr/bin/env python3
"""Check a configured live Gazebo/SITL/ROS baseline before network flight."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import yaml


def process_arducopters() -> list[dict[str, int]]:
    result: list[dict[str, int]] = []
    for path in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            argv = [part.decode(errors="replace") for part in path.read_bytes().split(b"\0") if part]
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if not argv or not any(Path(part).name == "arducopter" for part in argv):
            continue
        values: dict[str, str] = {}
        for index, part in enumerate(argv):
            for flag in ("--instance", "--sysid"):
                if part == flag and index + 1 < len(argv):
                    values[flag] = argv[index + 1]
                elif part.startswith(flag + "="):
                    values[flag] = part.split("=", 1)[1]
        if set(values) == {"--instance", "--sysid"}:
            result.append(
                {
                    "pid": int(path.parent.name),
                    "instance": int(values["--instance"]),
                    "system_id": int(values["--sysid"]),
                }
            )
    return sorted(result, key=lambda item: item["instance"])


def gazebo_models() -> list[str]:
    try:
        completed = subprocess.run(
            ["gz", "model", "--list"], text=True, capture_output=True, timeout=10, check=False
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    return sorted(
        match.group(1)
        for line in completed.stdout.splitlines()
        if (match := re.match(r"\s*-\s+(\S+)\s*$", line))
    )


def tracker_state(path: Path, names: list[str]) -> tuple[bool, dict[str, Any]]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, {}
    nodes = {str(node.get("id")): node for node in state.get("nodes", []) if isinstance(node, dict)}
    ready = (
        state.get("type") == "node_state"
        and state.get("source") == "ros_odometry"
        and not state.get("missing_nodes")
        and not state.get("stale_nodes")
        and all(
            name in nodes
            and nodes[name].get("source_topic") == f"/{name}/odometry"
            and not nodes[name].get("stale", True)
            for name in names
        )
    )
    return ready, state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", type=Path, required=True)
    parser.add_argument("--tracker-state", type=Path, required=True)
    parser.add_argument("--tracker-events", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-s", type=float, default=180.0)
    args = parser.parse_args()
    scenario = yaml.safe_load(args.scenario.read_text(encoding="utf-8"))
    names = [str(robot["name"]) for robot in scenario["robots"]]
    scene_map = scenario.get("scenario", {}).get("map", {})
    expected_world_models = [str(name) for name in scene_map.get("gazebo_models", [])]
    if not expected_world_models:
        raise SystemExit("scenario.map.gazebo_models must declare the expected live map models")
    command_post_model = str(
        scenario.get("command_post", {}).get("gazebo_model_name", "command_post")
    )
    expected_system_ids = [int(robot["system_id"]) for robot in scenario["robots"]]
    expected_instances = [int(robot["instance"]) for robot in scenario["robots"]]
    started = time.monotonic()
    models: list[str] = []
    sitl: list[dict[str, int]] = []
    state: dict[str, Any] = {}
    samples = {name: 0 for name in names}
    consumed_lines = 0
    ready = False
    while time.monotonic() - started < args.timeout_s:
        models = gazebo_models()
        sitl = process_arducopters()
        tracker_ready, state = tracker_state(args.tracker_state, names)
        try:
            lines = args.tracker_events.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        for line in lines[consumed_lines:]:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            nodes = {str(node.get("id")): node for node in item.get("nodes", []) if isinstance(node, dict)}
            for name in names:
                if name in nodes and not nodes[name].get("stale", True):
                    samples[name] += 1
        consumed_lines = len(lines)
        ready = all(
            (
                [item["system_id"] for item in sitl] == expected_system_ids,
                [item["instance"] for item in sitl] == expected_instances,
                all(name in models for name in names),
                all(name in models for name in expected_world_models),
                command_post_model in models,
                tracker_ready,
                all(samples[name] >= 5 for name in names),
            )
        )
        if ready:
            break
        time.sleep(0.2)
    nodes = {str(node.get("id")): node for node in state.get("nodes", []) if isinstance(node, dict)}
    summary = {
        "status": "healthy" if ready else "unhealthy",
        "elapsed_s": round(time.monotonic() - started, 3),
        "sitl": sitl,
        "gazebo_models": models,
        "odometry": {
            name: {
                "samples": samples[name],
                "last_position_m": nodes.get(name, {}).get("position_m"),
            }
            for name in names
        },
        "world_models": expected_world_models,
        "command_post_model": command_post_model,
        "command_post": nodes.get("cp"),
        "errors": [] if ready else ["Configured Gazebo/SITL/ROS baseline did not become healthy before timeout"],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        f"STACK HEALTH {summary['status']}: sitl={len(sitl)} "
        f"models={sum(name in models for name in names)}/5 "
        f"odometry={sum(samples[name] >= 5 for name in names)}/5",
        flush=True,
    )
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
