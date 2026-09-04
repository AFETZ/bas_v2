#!/usr/bin/env python3
"""Persist selected frames from live Gazebo camera sensors during a native run."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
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
    "five_uav_hold": ("02_five_uav_hold", "uav_focus"),
    "los_observation": ("03_uav1_los", "uav_focus"),
    "obstructed_observation": ("04_uav1_obstructed", "obstacle"),
    "p2mp": ("05_p2mp_or_shared_medium", "overview"),
    "simultaneous_uplink": ("05_p2mp_or_shared_medium", "overview"),
    "landing": ("06_landing", "uav_focus"),
}

CAMERAS = {
    "overview": {"pose": [50.0, 55.0, 140.0, 0.0, 1.5707, 0.0], "horizontal_fov": 1.4},
    "obstacle": {"pose": [80.0, 110.0, 70.0, 0.0, 1.5707, 0.0], "horizontal_fov": 1.0},
    "uav_focus": {
        "pose": [50.0, 0.0, 90.0, 0.0, 1.5707, 1.5708],
        "horizontal_fov": 1.2,
    },
}

POSITION_GATES = {
    "los_observation": ("uav1", [80.0, 0.0, 17.0], 8.0),
    "obstructed_observation": ("uav1", [80.0, 110.0, 17.0], 8.0),
}

PALETTE = {
    "cp": (255, 80, 20),
    "uav1": (40, 40, 255),
    "uav2": (40, 210, 40),
    "uav3": (255, 80, 255),
    "uav4": (20, 210, 255),
    "uav5": (255, 180, 30),
}


def canonical_phase(value: str) -> str:
    if value == "takeoff_complete":
        return "takeoff"
    return {
        "hold_all": "five_uav_hold",
        "los": "los_observation",
        "obstructed_candidate": "obstructed_observation",
        "landing_complete": "landing",
    }.get(value, value)


def rotation_matrix(roll: float, pitch: float, yaw: float) -> np.ndarray:
    """Return the SDFormat Rz(yaw) Ry(pitch) Rx(roll) camera rotation."""

    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    rx = np.array(((1, 0, 0), (0, cr, -sr), (0, sr, cr)), dtype=float)
    ry = np.array(((cp, 0, sp), (0, 1, 0), (-sp, 0, cp)), dtype=float)
    rz = np.array(((cy, -sy, 0), (sy, cy, 0), (0, 0, 1)), dtype=float)
    return rz @ ry @ rx


def project_position(
    position: list[float], camera: str, width: int, height: int
) -> dict[str, float | int | bool]:
    spec = CAMERAS[camera]
    pose = spec["pose"]
    relative_world = np.asarray(position, dtype=float) - np.asarray(pose[:3], dtype=float)
    local = rotation_matrix(*pose[3:]).T @ relative_world
    forward, left, up = (float(value) for value in local)
    focal = width / (2.0 * math.tan(float(spec["horizontal_fov"]) / 2.0))
    if forward <= 0.1:
        return {"in_frame": False, "forward_m": forward}
    pixel_x = width / 2.0 - focal * left / forward
    pixel_y = height / 2.0 - focal * up / forward
    return {
        "in_frame": 0 <= pixel_x < width and 0 <= pixel_y < height,
        "pixel_x": round(pixel_x, 3),
        "pixel_y": round(pixel_y, 3),
        "forward_m": round(forward, 3),
    }


def annotate_frame(
    image: np.ndarray, camera: str, phase: str, positions: dict[str, list[float]]
) -> tuple[np.ndarray, dict[str, dict[str, float | int | bool]]]:
    annotated = image.copy()
    projections = {
        name: project_position(position, camera, image.shape[1], image.shape[0])
        for name, position in positions.items()
        if name == "cp" or name.startswith("uav")
    }
    cv2.rectangle(annotated, (0, 0), (annotated.shape[1], 34), (20, 20, 20), -1)
    cv2.putText(
        annotated,
        f"LIVE GAZEBO | {phase} | telemetry-projected labels",
        (12, 23),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.55,
        (255, 255, 255),
        1,
        cv2.LINE_AA,
    )
    for name, projection in projections.items():
        if not projection.get("in_frame"):
            continue
        x, y = int(round(float(projection["pixel_x"]))), int(round(float(projection["pixel_y"])))
        colour = PALETTE.get(name, (255, 255, 255))
        cv2.circle(annotated, (x, y), 13, (0, 0, 0), 4, cv2.LINE_AA)
        cv2.circle(annotated, (x, y), 13, colour, 2, cv2.LINE_AA)
        cv2.line(annotated, (x - 17, y), (x + 17, y), colour, 2, cv2.LINE_AA)
        cv2.line(annotated, (x, y - 17), (x, y + 17), colour, 2, cv2.LINE_AA)
        cv2.putText(
            annotated,
            name.upper(),
            (x + 17, max(18, y - 8)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.48,
            (0, 0, 0),
            3,
            cv2.LINE_AA,
        )
        cv2.putText(
            annotated,
            name.upper(),
            (x + 17, max(18, y - 8)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.48,
            colour,
            1,
            cv2.LINE_AA,
        )
    return annotated, projections


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_positions(
    path: Path,
) -> tuple[dict[str, list[float]], list[str], list[float] | None, float | None]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, [], None, None
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
    tracker_time = state.get("time_s")
    return (
        positions,
        sorted(visible),
        command_post,
        float(tracker_time) if isinstance(tracker_time, (int, float)) else None,
    )


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
        positions, fresh_uavs, command_post, tracker_time = load_positions(self.args.node_state)
        tracker_snapshot_age_s = time.time() - tracker_time if tracker_time is not None else math.inf
        if len(fresh_uavs) != 5 or command_post is None or tracker_snapshot_age_s > 1.0:
            return
        gate = POSITION_GATES.get(phase)
        if gate and math.dist(positions.get(gate[0], [math.inf] * 3), gate[1]) > gate[2]:
            return
        message = self.frames[camera]
        channels = 3 if message.encoding.lower() in {"rgb8", "bgr8"} else 4
        image = np.frombuffer(message.data, dtype=np.uint8)
        expected = message.height * message.step
        if image.size < expected:
            return
        image = image[:expected].reshape((message.height, message.step))
        image = image[:, : message.width * channels].reshape((message.height, message.width, channels))
        if message.encoding.lower() == "rgb8":
            image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
        elif message.encoding.lower() == "rgba8":
            image = cv2.cvtColor(image, cv2.COLOR_RGBA2BGR)
        elif message.encoding.lower() == "bgra8":
            image = cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
        raw_output = self.args.output / f"{stem}.raw.png"
        output = self.args.output / f"{stem}.png"
        annotated, projections = annotate_frame(
            image,
            camera,
            phase,
            {**{name: positions[name] for name in fresh_uavs}, "cp": command_post},
        )
        if not cv2.imwrite(str(raw_output), image) or not cv2.imwrite(str(output), annotated):
            return
        projected_uavs = sorted(
            name for name in fresh_uavs if projections.get(name, {}).get("in_frame")
        )
        metadata = {
            "run_id": self.args.run_id,
            "wall_timestamp": datetime.now(timezone.utc).isoformat(),
            "tracker_snapshot_wall_age_s": tracker_snapshot_age_s,
            "simulation_timestamp": message.header.stamp.sec + message.header.stamp.nanosec / 1e9,
            "scenario_phase": phase,
            "camera_name": camera,
            "camera_pose": CAMERAS[camera]["pose"],
            "camera_horizontal_fov_rad": CAMERAS[camera]["horizontal_fov"],
            "fresh_uavs": fresh_uavs,
            "projected_uavs": projected_uavs,
            "uav_positions": {name: positions[name] for name in fresh_uavs},
            "command_post_position": command_post,
            "source": "live_gazebo_runtime",
            "image_kind": "annotated_live_frame",
            "annotation": "labels are pinhole projections of the simultaneous live ROS odometry snapshot",
            "raw_image": raw_output.name,
            "raw_image_sha256": sha256(raw_output),
            "annotated_image_sha256": sha256(output),
            "projections": projections,
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
