#!/usr/bin/env python3
"""Publish normalized radio node state from ROS odometry or scenario config."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import uuid
from collections import deque
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
        "velocity_enu_mps": [0.0, 0.0, 0.0],
        "angular_velocity_enu_radps": [0.0, 0.0, 0.0],
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
    temp_path.write_text(json.dumps(state, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temp_path, output_json)
    if output_jsonl is not None:
        output_jsonl.parent.mkdir(parents=True, exist_ok=True)
        with output_jsonl.open("a", encoding="utf-8") as stream:
            # Live readers need a bounded history; archive each latest sample once.
            logged = dict(state, nodes=[{k:v for k,v in node.items() if k != 'history'}
                                        for node in state['nodes']])
            stream.write(json.dumps(logged, separators=(",", ":"), allow_nan=False) + "\n")


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


def rotate_vector(q: list[float], vector: list[float]) -> list[float]:
    """ROS Odometry twist is in child_frame_id; radio velocity is world ENU."""
    x, y, z, w = q
    vx, vy, vz = vector
    tx, ty, tz = 2*(y*vz-z*vy), 2*(z*vx-x*vz), 2*(x*vy-y*vx)
    return [vx+w*tx+y*tz-z*ty, vy+w*ty+z*tx-x*tz, vz+w*tz+x*ty-y*tx]


class LiveState:
    """Bounded measured history; clocks reset by restarting the whole run."""
    def __init__(self, scenario: dict[str, Any], stale_after_s: float = .5):
        self.scenario = scenario
        self.stale_ns = int(stale_after_s*1e9)
        self.session = uuid.uuid4().hex
        self.history = {r['name']: deque(maxlen=32) for r in scenario['robots']}
        self.clock_s: float | None = None
        self.clock_rx_ns = 0
        self.fault: str | None = None
        self.rejected = 0

    def clock(self, sim_s: float, now_ns: int) -> None:
        if not math.isfinite(sim_s) or sim_s < 0:
            self.fault = 'invalid_gazebo_clock'
        elif self.clock_s is not None and sim_s < self.clock_s:
            self.fault = 'gazebo_clock_reset_restart_required'
        elif self.clock_s is None or sim_s > self.clock_s:
            self.clock_s, self.clock_rx_ns = sim_s, now_ns

    def odometry(self, name: str, msg: Any, now_ns: int) -> bool:
        pose, twist = msg.pose.pose, msg.twist.twist
        stamp = msg.header.stamp.sec + msg.header.stamp.nanosec/1e9
        q = [float(getattr(pose.orientation, k)) for k in ('x','y','z','w')]
        p = [float(getattr(pose.position,k)) for k in ('x','y','z')]
        v = [float(getattr(twist.linear,k)) for k in ('x','y','z')]
        omega = [float(getattr(twist.angular,k)) for k in ('x','y','z')]
        history = self.history[name]
        valid_frames = str(msg.header.frame_id).strip('/') in ('odom', f'{name}/odom')
        valid_frames &= str(msg.child_frame_id).strip('/') in ('base_link', f'{name}/base_link')
        if (not valid_frames or not all(math.isfinite(x) for x in [stamp,*q,*p,*v,*omega])
                or not 1e-12 <= sum(x*x for x in q) < float("inf") or self.clock_s is None
                or abs(stamp-self.clock_s) > self.stale_ns/1e9
                or now_ns-self.clock_rx_ns > self.stale_ns
                or (history and stamp <= history[-1]['source_sim_time_s'])):
            self.rejected += 1
            return False
        norm = math.sqrt(sum(x*x for x in q))
        q = [x/norm for x in q]
        # Age also includes source-clock lag at reception. Never label old samples
        # with the publication time, or extrapolate physical state in this adapter.
        sample_ns = now_ns - int(max(0., self.clock_s-stamp)*1e9)
        history.append(dict(id=name, role='uav', position_m=p, orientation_quat_xyzw=q,
            velocity_enu_mps=rotate_vector(q,v), angular_velocity_enu_radps=rotate_vector(q,omega),
            source_sim_time_s=stamp, received_monotonic_ns=now_ns, sample_monotonic_ns=sample_ns,
            source_frame=msg.header.frame_id, child_frame=msg.child_frame_id,
            source_topic=f'/{name}/odometry', antenna='omni', stale=False))
        return True

    def snapshot(self, now_ns: int) -> dict[str, Any]:
        nodes = [command_post_node(self.scenario, 'scenario_config')]
        missing, stale = [], []
        for name, history in self.history.items():
            if not history:
                missing.append(name)
                continue
            node = dict(history[-1])
            node['sample_age_ms'] = (now_ns-node['sample_monotonic_ns'])/1e6
            node['stale'] = now_ns-node['sample_monotonic_ns'] > self.stale_ns
            if node['stale']:
                stale.append(name)
            node['history'] = list(history)
            nodes.append(node)
        return dict(type='node_state', schema_version=2, session_id=self.session,
            coordinate_frame='ENU', source='ros_odometry', time_s=time.time(),
            published_monotonic_ns=now_ns, source_sim_time_s=self.clock_s,
            clock_received_monotonic_ns=self.clock_rx_ns, fault=self.fault,
            nodes=nodes, missing_nodes=missing, stale_nodes=stale, rejected_samples=self.rejected)


def run_ros_tracker(args: argparse.Namespace) -> int:
    try:
        import rclpy
        from nav_msgs.msg import Odometry
        from rclpy.node import Node
        from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
        from rosgraph_msgs.msg import Clock
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
            self.state = LiveState(scenario, stale_after_s)
            self.robot_names = [robot["name"] for robot in scenario.get("robots", [])]
            qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT,
                             history=HistoryPolicy.KEEP_LAST)
            self.create_subscription(Clock, f'/{self.robot_names[0]}/clock',
                lambda msg: self.state.clock(msg.clock.sec+msg.clock.nanosec/1e9, time.monotonic_ns()), qos)
            for name in self.robot_names:
                topic = f"/{name}/odometry"
                self.create_subscription(
                    Odometry,
                    topic,
                    lambda msg, robot_name=name, source_topic=topic: self._on_odometry(
                        robot_name, source_topic, msg
                    ),
                    qos,
                )
                self.get_logger().info(f"tracking {name} from {topic}")
            self.create_timer(1.0 / max(rate_hz, 0.1), self._publish_state)

        def _on_odometry(self, robot_name: str, source_topic: str, msg: Any) -> None:
            self.state.odometry(robot_name, msg, time.monotonic_ns())

        def _publish_state(self) -> None:
            state = self.state.snapshot(time.monotonic_ns())
            state['emitters'] = jammer_emitters(jammers, enabled_only=True, jammers_source=jammers_source)
            write_state(output_json, output_jsonl, state)
            if state['missing_nodes'] or state['stale_nodes'] or state['fault']:
                self.get_logger().warn(
                    f"missing={state['missing_nodes']} stale={state['stale_nodes']} fault={state['fault']}"
                )

    rclpy.init(args=None)
    node = RadioPositionTracker()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        return 130
    finally:
        node.destroy_node()
        rclpy.try_shutdown()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", default=str(DEFAULT_SCENARIO))
    parser.add_argument("--jammers-config", default=str(DEFAULT_JAMMERS))
    parser.add_argument("--output-json", default=str(ROOT_DIR / "runs/latest/logs/node_state.json"))
    parser.add_argument("--output-jsonl", default=str(ROOT_DIR / "runs/latest/logs/node_state.jsonl"))
    parser.add_argument("--rate-hz", type=float, default=5.0)
    parser.add_argument("--stale-after-s", type=float, default=.5)
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
