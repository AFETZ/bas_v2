#!/usr/bin/env python3
"""Persist selected frames from live Gazebo camera sensors during a native run."""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Image


CAPTURES = {
    "takeoff": ("01_five_uav_takeoff", "overview"),
    "five_uav_hold": ("02_five_uav_hold", "overview"),
    "los_observation": ("03_uav1_los", "overview"),
    "obstructed_observation": ("04_uav1_obstructed", "obstacle"),
    "p2mp": ("05_p2mp_or_shared_medium", "overview"),
    "simultaneous_uplink": ("05_p2mp_or_shared_medium", "overview"),
    "landing": ("06_landing", "overview"),
}

CAMERA_POSES = {
    "overview": [5.0, -35.0, 35.0, 0.0, 0.55, 0.0],
    "obstacle": [35.0, 45.0, 40.0, 0.0, 0.48, 0.95],
    "uav_focus": [35.0, -25.0, 32.0, 0.0, 0.55, 0.35],
}


def canonical_phase(value: str) -> str:
    if value.startswith("takeoff_") or value.startswith("arm_"):
        return "takeoff"
    return {
        "hold_all": "five_uav_hold",
        "los": "los_observation",
        "obstructed_candidate": "obstructed_observation",
        "land_all": "landing",
    }.get(value, value)


def load_positions(path: Path) -> tuple[dict[str, list[float]], list[str], list[float] | None]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, [], None
    positions: dict[str, list[float]] = {}
    visible: list[str] = []
    command_post: list[float] | None = None
    for node in state.get("nodes", []):
        name = str(node.get("id", ""))
        position = node.get("position_m")
        if not isinstance(position, list) or len(position) != 3:
            continue
        positions[name] = [float(value) for value in position]
        if name.startswith("uav") and not node.get("stale"):
            visible.append(name)
        if name == "cp":
            command_post = positions[name]
    return positions, sorted(visible), command_post


class Capture(Node):
    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__("native_radio_live_screenshot_capture")
        self.args = args
        self.frames: dict[str, Image] = {}
        self.done: set[str] = set()
        self.create_subscription(Image, "/native_radio/overview/image", lambda msg: self.frame("overview", msg), 1)
        self.create_subscription(Image, "/native_radio/obstacle/image", lambda msg: self.frame("obstacle", msg), 1)
        self.create_subscription(Image, "/native_radio/uav_focus/image", lambda msg: self.frame("uav_focus", msg), 1)
        self.create_timer(0.2, self.poll)

    def frame(self, camera: str, message: Image) -> None:
        self.frames[camera] = message

    def poll(self) -> None:
        if self.args.stop_file.exists():
            self.destroy_node()
            rclpy.shutdown()
            return
        try:
            phase = canonical_phase(self.args.phase_file.read_text(encoding="utf-8").strip())
        except OSError:
            return
        capture = CAPTURES.get(phase)
        if not capture:
            return
        stem, camera = capture
        if stem in self.done or camera not in self.frames:
            return
        positions, visible, command_post = load_positions(self.args.node_state)
        if len(visible) != 5 or command_post is None:
            return
        message = self.frames[camera]
        channels = 3 if message.encoding.lower() in {"rgb8", "bgr8"} else 4
        image = np.frombuffer(message.data, dtype=np.uint8)
        expected = message.height * message.width * channels
        if image.size < expected:
            return
        image = image[:expected].reshape((message.height, message.width, channels))
        if message.encoding.lower() == "rgb8":
            image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
        elif message.encoding.lower() == "rgba8":
            image = cv2.cvtColor(image, cv2.COLOR_RGBA2BGR)
        elif message.encoding.lower() == "bgra8":
            image = cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
        output = self.args.output / f"{stem}.png"
        if not cv2.imwrite(str(output), image):
            return
        metadata = {
            "run_id": self.args.run_id,
            "wall_timestamp": datetime.now(timezone.utc).isoformat(),
            "simulation_timestamp": message.header.stamp.sec + message.header.stamp.nanosec / 1e9,
            "scenario_phase": phase,
            "camera_name": camera,
            "camera_pose": CAMERA_POSES[camera],
            "visible_uavs": visible,
            "uav_positions": {name: positions[name] for name in visible},
            "command_post_position": command_post,
            "source": "live_gazebo_runtime",
        }
        (self.args.output / f"{stem}.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        self.done.add(stem)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--node-state", type=Path, required=True)
    parser.add_argument("--phase-file", type=Path, required=True)
    parser.add_argument("--stop-file", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    rclpy.init()
    capture = Capture(args)
    try:
        rclpy.spin(capture)
    finally:
        if rclpy.ok():
            capture.destroy_node()
            rclpy.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
