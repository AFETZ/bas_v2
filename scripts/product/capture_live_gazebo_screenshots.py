#!/usr/bin/env python3
"""Persist selected frames from live Gazebo camera sensors during a native run."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np
import rclpy
import yaml
from rclpy.node import Node
from sensor_msgs.msg import Image
from inject_native_radio_runtime_cameras import TOUR_CAMERAS


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SCENARIO_CONFIG = ROOT / "network/config/scenario_5uav_town01_native_product.yaml"

PALETTE = {
    "cp": (255, 80, 20),
    "uav1": (40, 40, 255),
    "uav2": (40, 210, 40),
    "uav3": (255, 80, 255),
    "uav4": (20, 210, 255),
    "uav5": (255, 180, 30),
}


def load_capture_config(path: Path) -> dict[str, object]:
    value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    evidence = value.get("evidence") or {}
    cameras = evidence.get("cameras") or {}
    screenshots = evidence.get("screenshots") or []
    if not isinstance(cameras, dict) or not cameras:
        raise RuntimeError(f"scenario has no evidence cameras: {path}")
    if not isinstance(screenshots, list) or not screenshots:
        raise RuntimeError(f"scenario has no evidence screenshots: {path}")
    for name, camera in cameras.items():
        if not isinstance(camera, dict) or len(camera.get("pose", [])) != 6:
            raise RuntimeError(f"invalid camera {name!r} in {path}")
        camera["horizontal_fov"] = float(camera["horizontal_fov_rad"])
    captures: dict[str, dict[str, object]] = {}
    for item in screenshots:
        if not isinstance(item, dict):
            raise RuntimeError(f"invalid screenshot entry in {path}")
        phase = str(item.get("phase", ""))
        if not phase or item.get("camera") not in cameras or not item.get("stem"):
            raise RuntimeError(f"invalid screenshot specification in {path}: {item!r}")
        captures[phase] = item
    robots = {
        str(robot["name"]): [float(component) for component in robot["position"][:3]]
        for robot in value.get("robots", [])
    }
    mission_targets: dict[str, dict[str, list[float]]] = {}
    for uav, mission in ((value.get("flight") or {}).get("missions") or {}).items():
        for waypoint in mission:
            mission_targets.setdefault(str(waypoint["name"]), {})[str(uav)] = [
                float(component) for component in waypoint["position_m"]
            ]
    return {
        "scenario_name": str((value.get("scenario") or {}).get("name", path.stem)),
        "map_id": str(((value.get("scenario") or {}).get("map") or {}).get("id", "unknown")),
        "cameras": cameras,
        "captures": captures,
        "phase_aliases": {
            str(key): str(alias)
            for key, alias in (evidence.get("phase_aliases") or {}).items()
        },
        "robots": robots,
        "mission_targets": mission_targets,
        "mission_tolerance_m": float(
            (value.get("flight") or {}).get("mission_position_tolerance_m", 8.0)
        ),
        "airborne_clearance_m": float(evidence.get("airborne_clearance_m", 6.0)),
        "landed_altitude_tolerance_m": float(
            evidence.get("landed_altitude_tolerance_m", 3.0)
        ),
    }


def canonical_phase(value: str, aliases: dict[str, str]) -> str:
    return aliases.get(value, value)


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
    position: list[float],
    camera: str,
    width: int,
    height: int,
    cameras: dict[str, dict[str, object]],
) -> dict[str, float | int | bool]:
    spec = cameras[camera]
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
    image: np.ndarray,
    camera: str,
    phase: str,
    positions: dict[str, list[float]],
    cameras: dict[str, dict[str, object]],
) -> tuple[np.ndarray, dict[str, dict[str, float | int | bool]]]:
    annotated = image.copy()
    projections = {
        name: project_position(
            position, camera, image.shape[1], image.shape[0], cameras
        )
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
        self.config = load_capture_config(args.scenario_config)
        self.cameras = self.config["cameras"]
        if os.environ.get("BAS_DEMO_TOUR") == "1":
            self.cameras.update({name: {**spec, "topic": f"/native_radio/{name}/image"}
                                 for name, spec in TOUR_CAMERAS.items()})
        self.captures = self.config["captures"]
        if args.source_state:
            for phase, stem in (("jammer_active", "07_jammer_active"), ("jammer_recovery", "08_jammer_recovery")):
                self.captures[phase] = dict(phase=phase, stem=stem, camera="uav_focus")
        self.phase_aliases = self.config["phase_aliases"]
        self.frames: dict[str, tuple[Image, int]] = {}
        self.done: set[str] = set()
        self.last_phase: str | None = None
        self.phase_seen_monotonic_ns = 0
        self.video_writers = {}
        self.video_counts = {}
        self.video_log = None
        self.runtime_log = None
        if args.video_directory:
            args.video_directory.mkdir(parents=True, exist_ok=True)
            if os.geteuid() == 0:
                os.chown(args.video_directory, int(os.environ.get("BAS_NATIVE_FIVE_HOST_UID", 0)),
                         int(os.environ.get("BAS_NATIVE_FIVE_HOST_GID", 0)))
            self.video_log = (args.video_directory / "frames.jsonl").open("w", buffering=1)
            self.runtime_log = (args.video_directory / "runtime_snapshots.jsonl").open("w", buffering=1)
        for name, camera in self.cameras.items():
            self.create_subscription(
                Image,
                str(camera["topic"]),
                lambda message, camera_name=name: self.frame(camera_name, message),
                1,
            )
        self.create_timer(0.2, self.poll)

    def frame(self, camera: str, message: Image) -> None:
        received = time.monotonic_ns()
        self.frames[camera] = (message, received)
        if self.video_log:
            if message.encoding.lower() not in {"rgb8", "bgr8"}:
                raise RuntimeError(f"Unsupported video encoding: {message.encoding}")
            pixels = np.frombuffer(message.data, dtype=np.uint8).reshape(message.height, message.step)
            pixels = pixels[:, :message.width * 3].reshape(message.height, message.width, 3)
            if message.encoding.lower() == "rgb8":
                pixels = cv2.cvtColor(pixels, cv2.COLOR_RGB2BGR)
            if camera not in self.video_writers:
                writer = cv2.VideoWriter(str(self.args.video_directory / f"{camera}.avi"),
                                         cv2.VideoWriter_fourcc(*"MJPG"), self.args.video_fps,
                                         (message.width, message.height))
                if not writer.isOpened():
                    raise RuntimeError("Cannot open continuous Gazebo video writer")
                self.video_writers[camera] = writer
                self.video_counts[camera] = 0
            self.video_writers[camera].write(pixels)
            try:
                phase = self.args.phase_file.read_text().strip()
            except OSError:
                phase = "нет данных"
            positions, _, _, tracker_time = load_positions(self.args.node_state)
            self.video_log.write(json.dumps(dict(run_id=self.args.run_id, camera=camera,
                frame=self.video_counts[camera], monotonic_ns=received, wall_time_s=time.time(),
                simulation_time_s=message.header.stamp.sec + message.header.stamp.nanosec / 1e9,
                phase=phase, positions=positions, tracker_wall_time_s=tracker_time)) + "\n")
            self.video_counts[camera] += 1

    def close_video(self):
        for writer in self.video_writers.values():
            writer.release()
        if self.video_log:
            self.video_log.close()
        if self.runtime_log:
            self.runtime_log.close()

    def poll(self) -> None:
        if self.runtime_log:
            run_dir = self.args.output.parent
            heartbeat = run_dir / "logs/ns3.ready.heartbeat"
            snapshot = dict(monotonic_ns=time.monotonic_ns(), wall_time_s=time.time(),
                native_heartbeat_wall_age_s=time.time()-heartbeat.stat().st_mtime if heartbeat.exists() else None)
            endpoint = run_dir / "external_endpoint/metrics.json"
            if endpoint.exists():
                try: snapshot['external_endpoint'] = json.loads(endpoint.read_text())
                except (OSError, ValueError): pass
            self.runtime_log.write(json.dumps(snapshot) + "\n")
        if self.args.stop_file.exists():
            self.destroy_node()
            rclpy.shutdown()
            return
        try:
            phase = canonical_phase(
                self.args.phase_file.read_text(encoding="utf-8").strip(),
                self.phase_aliases,
            )
        except OSError:
            return
        source_state = None
        if self.args.source_state and self.args.source_state.is_file():
            try:
                source_state = json.loads(self.args.source_state.read_text())
                source_phase = "jammer_active" if source_state["enabled_sources"] else "jammer_recovery"
                if self.captures[source_phase]["stem"] not in self.done:
                    phase = source_phase
            except (OSError, ValueError, KeyError):
                pass
        if phase != self.last_phase:
            self.last_phase = phase
            self.phase_seen_monotonic_ns = time.monotonic_ns()
        capture = self.captures.get(phase)
        if not capture:
            return
        stem, camera = str(capture["stem"]), str(capture["camera"])
        if stem in self.done or camera not in self.frames:
            return
        positions, fresh_uavs, command_post, tracker_time = load_positions(self.args.node_state)
        tracker_snapshot_age_s = time.time() - tracker_time if tracker_time is not None else math.inf
        if len(fresh_uavs) != 5 or command_post is None or tracker_snapshot_age_s > 1.0:
            return
        if not self.spatial_gate_valid(capture, positions):
            return
        message, frame_received_monotonic_ns = self.frames[camera]
        if frame_received_monotonic_ns < self.phase_seen_monotonic_ns:
            return
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
            self.cameras,
        )
        if not cv2.imwrite(str(raw_output), image) or not cv2.imwrite(str(output), annotated):
            return
        projected_uavs = sorted(
            name for name in fresh_uavs if projections.get(name, {}).get("in_frame")
        )
        metadata = {
            "run_id": self.args.run_id,
            "wall_timestamp": datetime.now(timezone.utc).isoformat(),
            "frame_received_monotonic_ns": frame_received_monotonic_ns,
            "capture_monotonic_ns": time.monotonic_ns(),
            "tracker_snapshot_wall_age_s": tracker_snapshot_age_s,
            "simulation_timestamp": message.header.stamp.sec + message.header.stamp.nanosec / 1e9,
            "scenario_phase": phase,
            "scenario_config": str(self.args.scenario_config),
            "scenario_name": self.config["scenario_name"],
            "map_id": self.config["map_id"],
            "camera_name": camera,
            "camera_pose": self.cameras[camera]["pose"],
            "camera_horizontal_fov_rad": self.cameras[camera]["horizontal_fov"],
            "fresh_uavs": fresh_uavs,
            "projected_uavs": projected_uavs,
            "uav_positions": {name: positions[name] for name in fresh_uavs},
            "command_post_position": command_post,
            "source": "live_gazebo_runtime",
            "image_kind": "annotated_live_frame",
            "annotation": "labels are pinhole projections of the simultaneous live ROS odometry snapshot",
            "raw_image": raw_output.name,
            "radio_source_state": source_state,
            "raw_image_sha256": sha256(raw_output),
            "annotated_image_sha256": sha256(output),
            "projections": projections,
        }
        (self.args.output / f"{stem}.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        self.done.add(stem)

    def spatial_gate_valid(
        self, capture: dict[str, object], positions: dict[str, list[float]]
    ) -> bool:
        mission_phase = capture.get("mission_phase")
        if mission_phase:
            targets = self.config["mission_targets"].get(str(mission_phase), {})
            tolerance = float(self.config["mission_tolerance_m"])
            if not targets or any(
                math.dist(positions.get(uav, [math.inf] * 3), target) > tolerance
                for uav, target in targets.items()
            ):
                return False
        altitude_state = capture.get("altitude_state")
        initial = self.config["robots"]
        if altitude_state == "airborne":
            clearance = float(self.config["airborne_clearance_m"])
            return all(
                uav in positions and positions[uav][2] >= origin[2] + clearance
                for uav, origin in initial.items()
            )
        if altitude_state == "landed":
            tolerance = float(self.config["landed_altitude_tolerance_m"])
            return all(
                uav in positions and abs(positions[uav][2] - origin[2]) <= tolerance
                for uav, origin in initial.items()
            )
        return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--node-state", type=Path, required=True)
    parser.add_argument("--phase-file", type=Path, required=True)
    parser.add_argument("--stop-file", type=Path, required=True)
    parser.add_argument("--source-state", type=Path)
    parser.add_argument("--scenario-config", type=Path, default=DEFAULT_SCENARIO_CONFIG)
    parser.add_argument("--video-directory", type=Path)
    parser.add_argument("--video-fps", type=int, default=25)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    rclpy.init()
    capture = Capture(args)
    signal.signal(signal.SIGTERM, lambda *_: args.stop_file.touch())
    try:
        rclpy.spin(capture)
    finally:
        capture.close_video()
        if rclpy.ok():
            capture.destroy_node()
            rclpy.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
