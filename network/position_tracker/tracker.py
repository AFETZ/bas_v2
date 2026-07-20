#!/usr/bin/env python3
"""Publish normalized radio node state from ROS odometry or scenario config."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - dependency check covers this
    raise SystemExit("PyYAML is required: python3 -m pip install PyYAML") from exc


ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_SCENARIO = ROOT_DIR / "network/config/scenario_5uav.yaml"
DEFAULT_JAMMERS = ROOT_DIR / "network/config/jammers.yaml"


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
    cp = dict(scenario.get("command_post") or {})
    return {
        "id": cp.get("id", "cp"),
        "role": cp.get("role", "command_post"),
        "position_m": normalize_position(cp.get("position_m"), [0.0, 0.0, 20.0]),
        "orientation_quat_xyzw": cp.get("orientation_quat_xyzw", [0.0, 0.0, 0.0, 1.0]),
        "antenna": cp.get("antenna", "omni"),
        "source_topic": f"{scenario_source}:command_post",
        "stale": False,
    }


def jammer_emitters(
    jammers: dict[str, Any], enabled_only: bool = True, jammers_source: str = "jammers_config"
) -> list[dict[str, Any]]:
    emitters: list[dict[str, Any]] = []
    for jammer in jammers.get("jammers", []):
        if enabled_only and not bool(jammer.get("enabled", False)):
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
                "source_topic": jammers_source,
            }
        )
    return emitters


def config_state(
    scenario: dict[str, Any],
    jammers: dict[str, Any],
    scenario_source: str,
    jammers_source: str,
) -> dict[str, Any]:
    nodes = [command_post_node(scenario, scenario_source)]
    for robot in scenario.get("robots", []):
        launch_position = robot.get("position", [0.0, 0.0, 0.0])
        nodes.append(
            {
                "id": robot["name"],
                "role": robot.get("role", "uav"),
                "position_m": normalize_position(
                    robot.get("nominal_radio_position_m"),
                    [launch_position[0], launch_position[1], launch_position[2]],
                ),
                "orientation_quat_xyzw": [0.0, 0.0, 0.0, 1.0],
                "antenna": robot.get("antenna", "omni"),
                "source_topic": f"{scenario_source}:nominal_radio_position_m",
                "stale": False,
            }
        )
    return {
        "type": "node_state",
        "time_s": time.time(),
        "wall_time": datetime.now(timezone.utc).isoformat(),
        "source": "scenario_config",
        "nodes": nodes,
        "emitters": jammer_emitters(jammers, enabled_only=True, jammers_source=jammers_source),
        "missing_nodes": [],
        "stale_nodes": [],
    }


def write_state(output_json: Path, output_jsonl: Path | None, state: dict[str, Any]) -> None:
    output_json.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_json.with_suffix(output_json.suffix + ".tmp")
    temp_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp_path, output_json)
    if output_jsonl is not None:
        output_jsonl.parent.mkdir(parents=True, exist_ok=True)
        with output_jsonl.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(state, separators=(",", ":"), allow_nan=False) + "\n")


def run_config_once(args: argparse.Namespace) -> int:
    scenario_path = Path(args.scenario)
    jammers_path = Path(args.jammers_config)
    scenario = load_yaml(scenario_path)
    jammers = load_yaml(jammers_path)
    state = config_state(scenario, jammers, scenario_path.name, jammers_path.name)
    output_json = Path(args.output_json).resolve()
    output_jsonl = Path(args.output_jsonl).resolve() if args.output_jsonl else None
    write_state(output_json, output_jsonl, state)
    print(json.dumps(state, separators=(",", ":"), allow_nan=False))
    return 0


def run_ros_tracker(args: argparse.Namespace) -> int:
    try:
        import rclpy
        from nav_msgs.msg import Odometry
        from rclpy.node import Node
    except ImportError as exc:
        print(
            "ERROR ROS 2 Python packages are required for live tracking. "
            "Source the ROS 2 workspace or run inside the project container. "
            "Use --from-config-once only for offline smoke tests.",
            file=sys.stderr,
        )
        return 2

    scenario_path = Path(args.scenario)
    jammers_path = Path(args.jammers_config)
    scenario = load_yaml(scenario_path)
    jammers = load_yaml(jammers_path)
    scenario_source = scenario_path.name
    jammers_source = jammers_path.name
    output_json = Path(args.output_json).resolve()
    output_jsonl = Path(args.output_jsonl).resolve() if args.output_jsonl else None
    stale_after_s = float(args.stale_after_s)
    rate_hz = float(args.rate_hz)

    class RadioPositionTracker(Node):
        def __init__(self) -> None:
            super().__init__("network_radio_position_tracker")
            self.records: dict[str, dict[str, Any]] = {}
            self.last_seen: dict[str, float] = {}
            self.robot_names = [robot["name"] for robot in scenario.get("robots", [])]
            for name in self.robot_names:
                topic = f"/{name}/odometry"
                self.create_subscription(
                    Odometry,
                    topic,
                    lambda msg, robot_name=name, source_topic=topic: self._on_odometry(
                        robot_name, source_topic, msg
                    ),
                    10,
                )
                self.get_logger().info(f"tracking {name} from {topic}")
            self.create_timer(1.0 / max(rate_hz, 0.1), self._publish_state)

        def _on_odometry(self, robot_name: str, source_topic: str, msg: Any) -> None:
            pose = msg.pose.pose
            self.records[robot_name] = {
                "id": robot_name,
                "role": "uav",
                "position_m": [
                    float(pose.position.x),
                    float(pose.position.y),
                    float(pose.position.z),
                ],
                "orientation_quat_xyzw": [
                    float(pose.orientation.x),
                    float(pose.orientation.y),
                    float(pose.orientation.z),
                    float(pose.orientation.w),
                ],
                "antenna": "omni",
                "source_topic": source_topic,
                "stale": False,
            }
            self.last_seen[robot_name] = time.time()

        def _publish_state(self) -> None:
            now = time.time()
            nodes = [command_post_node(scenario, scenario_source)]
            missing_nodes: list[str] = []
            stale_nodes: list[str] = []
            for robot in scenario.get("robots", []):
                name = robot["name"]
                if name not in self.records:
                    launch_position = robot.get("position", [0.0, 0.0, 0.0])
                    fallback_position = normalize_position(
                        robot.get("nominal_radio_position_m"),
                        [launch_position[0], launch_position[1], launch_position[2]],
                    )
                    missing_nodes.append(name)
                    nodes.append(
                        {
                            "id": name,
                            "role": robot.get("role", "uav"),
                            "position_m": fallback_position,
                            "orientation_quat_xyzw": [0.0, 0.0, 0.0, 1.0],
                            "antenna": robot.get("antenna", "omni"),
                            "source_topic": f"fallback:{Path(args.scenario).name}",
                            "stale": True,
                        }
                    )
                    continue
                record = dict(self.records[name])
                if now - self.last_seen[name] > stale_after_s:
                    record["stale"] = True
                    stale_nodes.append(name)
                nodes.append(record)

            state = {
                "type": "node_state",
                "time_s": now,
                "wall_time": datetime.now(timezone.utc).isoformat(),
                "source": "ros_odometry",
                "nodes": nodes,
                "emitters": jammer_emitters(
                    jammers, enabled_only=True, jammers_source=jammers_source
                ),
                "missing_nodes": missing_nodes,
                "stale_nodes": stale_nodes,
            }
            write_state(output_json, output_jsonl, state)
            if missing_nodes or stale_nodes:
                self.get_logger().warn(
                    f"missing_nodes={missing_nodes} stale_nodes={stale_nodes}"
                )

    rclpy.init(args=None)
    node = RadioPositionTracker()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        return 130
    finally:
        node.destroy_node()
        rclpy.shutdown()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", default=str(DEFAULT_SCENARIO))
    parser.add_argument("--jammers-config", default=str(DEFAULT_JAMMERS))
    parser.add_argument("--output-json", default=str(ROOT_DIR / "runs/latest/logs/node_state.json"))
    parser.add_argument("--output-jsonl", default=str(ROOT_DIR / "runs/latest/logs/node_state.jsonl"))
    parser.add_argument("--rate-hz", type=float, default=5.0)
    parser.add_argument("--stale-after-s", type=float, default=2.0)
    parser.add_argument(
        "--from-config-once",
        action="store_true",
        help="Write nominal positions from config once. Test/setup helper only.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.from_config_once:
        return run_config_once(args)
    return run_ros_tracker(args)


if __name__ == "__main__":
    raise SystemExit(main())
