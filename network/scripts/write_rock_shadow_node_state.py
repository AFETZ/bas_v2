#!/usr/bin/env python3
"""Write live node_state files for the matched five-UAV rock-shadow demo."""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - dependency check covers this
    raise SystemExit("PyYAML is required: python3 -m pip install PyYAML") from exc


ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_SCENARIO = ROOT_DIR / "network/config/scenario_rock_demo.yaml"
DEFAULT_JAMMERS = ROOT_DIR / "network/config/jammers_rock_demo.yaml"


def interpolate(a: float, b: float, f: float) -> float:
    return a + (b - a) * max(0.0, min(1.0, f))


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        data = yaml.safe_load(stream) or {}
    if not isinstance(data, dict):
        raise ValueError(f"YAML file must contain a mapping: {path}")
    return data


def normalize_position(value: Any, fallback: list[float] | None = None) -> list[float]:
    if value is None:
        if fallback is None:
            raise ValueError("position is missing")
        value = fallback
    if not isinstance(value, list) or len(value) < 3:
        raise ValueError(f"position must have at least three elements: {value!r}")
    return [float(value[0]), float(value[1]), float(value[2])]


def command_post_node(scenario: dict[str, Any], scenario_source: str) -> dict[str, Any]:
    command_post = dict(scenario.get("command_post") or {})
    return {
        "id": command_post.get("id", "cp"),
        "role": command_post.get("role", "command_post"),
        "position_m": normalize_position(command_post.get("position_m"), [-500.0, -220.0, 20.0]),
        "orientation_quat_xyzw": command_post.get("orientation_quat_xyzw", [0.0, 0.0, 0.0, 1.0]),
        "antenna": command_post.get("antenna", "omni"),
        "stale": False,
        "source_topic": f"{scenario_source}:command_post",
    }


def robot_base_position(robot: dict[str, Any]) -> list[float]:
    launch_position = robot.get("position", [0.0, 0.0, 0.0])
    fallback = [launch_position[0], launch_position[1], launch_position[2]]
    return normalize_position(robot.get("nominal_radio_position_m"), fallback)


def moving_position(elapsed_s: float, args: argparse.Namespace, fallback: list[float]) -> list[float]:
    y = fallback[1] if args.shadow_y is None else float(args.shadow_y)
    z = fallback[2] if args.shadow_altitude_m is None else float(args.shadow_altitude_m)
    start_x = float(args.shadow_start_x)
    end_x = float(args.shadow_end_x)
    hold_before_s = float(args.hold_before_s)
    transition_s = max(float(args.transition_s), 0.001)

    if elapsed_s < hold_before_s:
        x = start_x
    elif elapsed_s < hold_before_s + transition_s:
        x = interpolate(start_x, end_x, (elapsed_s - hold_before_s) / transition_s)
    else:
        x = end_x
    return [x, y, z]


def emitters_from_config(jammers: dict[str, Any], source: str) -> list[dict[str, Any]]:
    emitters: list[dict[str, Any]] = []
    for jammer in jammers.get("jammers", []):
        if not bool(jammer.get("enabled", False)):
            continue
        emitters.append(
            {
                "id": jammer["id"],
                "position_m": normalize_position(jammer.get("position_m")),
                "orientation_quat_xyzw": jammer.get("orientation_quat_xyzw", [0.0, 0.0, 0.0, 1.0]),
                "center_hz": float(jammer.get("center_hz", 2.4e9)),
                "bandwidth_hz": float(jammer.get("bandwidth_hz", 1e6)),
                "power_dbm": float(jammer.get("power_dbm", 40.0)),
                "duty_cycle": float(jammer.get("duty_cycle", 1.0)),
                "antenna": jammer.get("antenna", "omni"),
                "source_topic": source,
            }
        )
    return emitters


def node_state(
    elapsed_s: float,
    scenario: dict[str, Any],
    jammers: dict[str, Any],
    args: argparse.Namespace,
    scenario_source: str,
    jammers_source: str,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    nodes = [command_post_node(scenario, scenario_source)]
    moving_seen = False
    for robot in scenario.get("robots", []):
        base_position = robot_base_position(robot)
        name = str(robot["name"])
        source_topic = f"{scenario_source}:nominal_radio_position_m"
        position = base_position
        if name == args.moving_node:
            position = moving_position(elapsed_s, args, base_position)
            source_topic = "scripted_rock_shadow:moving"
            moving_seen = True
        nodes.append(
            {
                "id": name,
                "role": robot.get("role", "uav"),
                "position_m": position,
                "orientation_quat_xyzw": [0.0, 0.0, 0.0, 1.0],
                "antenna": robot.get("antenna", "omni"),
                "stale": False,
                "source_topic": source_topic,
            }
        )
    if not moving_seen:
        raise ValueError(f"moving node {args.moving_node!r} is not present in {scenario_source}")

    scenario_info = dict(scenario.get("scenario") or {})
    return {
        "type": "node_state",
        "source": "scripted_rock_shadow",
        "scenario": scenario_info.get("name", "scenario_rock_demo"),
        "time_s": now.timestamp(),
        "wall_time": now.isoformat(),
        "elapsed_s": round(elapsed_s, 3),
        "moving_node": args.moving_node,
        "uav_count": sum(1 for node in nodes if node.get("role") == "uav"),
        "nodes": nodes,
        "emitters": emitters_from_config(jammers, jammers_source),
        "missing_nodes": [],
        "stale_nodes": [],
    }


def write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", default=str(DEFAULT_SCENARIO))
    parser.add_argument("--jammers-config", default=str(DEFAULT_JAMMERS))
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-jsonl", required=True)
    parser.add_argument("--duration", type=float, default=65.0)
    parser.add_argument("--rate-hz", type=float, default=2.0)
    parser.add_argument("--moving-node", default="uav2")
    parser.add_argument("--shadow-start-x", type=float, default=-300.0)
    parser.add_argument("--shadow-end-x", type=float, default=500.0)
    parser.add_argument("--shadow-y", type=float, default=None)
    parser.add_argument("--shadow-altitude-m", type=float, default=None)
    parser.add_argument("--hold-before-s", type=float, default=15.0)
    parser.add_argument("--transition-s", type=float, default=25.0)
    args = parser.parse_args()

    scenario_path = Path(args.scenario)
    jammers_path = Path(args.jammers_config)
    scenario = load_yaml(scenario_path)
    jammers = load_yaml(jammers_path)
    output_json = Path(args.output_json)
    output_jsonl = Path(args.output_jsonl)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_jsonl.parent.mkdir(parents=True, exist_ok=True)

    period = 1.0 / max(args.rate_hz, 0.1)
    start = time.monotonic()
    with output_jsonl.open("a") as jsonl:
        while True:
            elapsed = time.monotonic() - start
            data = node_state(
                elapsed,
                scenario,
                jammers,
                args,
                scenario_path.name,
                jammers_path.name,
            )
            write_json(output_json, data)
            jsonl.write(json.dumps(data, sort_keys=True) + "\n")
            jsonl.flush()
            if elapsed >= args.duration:
                break
            time.sleep(period)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
