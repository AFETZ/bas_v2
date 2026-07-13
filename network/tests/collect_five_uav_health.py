#!/usr/bin/env python3
"""Collect structured five-UAV Gazebo/SITL/heartbeat/odometry health evidence.

This is an M1 component-only probe. Direct MAVProxy telemetry on UDP 14550 is
allowed only for base-runtime health and is explicitly ineligible as packet-path
or no-bypass proof.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml


ROOT_DIR = Path(__file__).resolve().parents[2]
M1_CONTRACT_ID = "ams.m1.health/v3"
M1_PLAN_PATH = "doc/network_radio_integration_plan_v3.md"
FATAL_LAUNCH_PATTERNS = (
    "bind error",
    "address already in use",
    "segmentation fault",
    "core dumped",
    "process has died",
    "error while starting ipvx agent",
)
OBSERVATION_WINDOW_PATTERNS = (
    "link 1 down",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def rate_hz(record: dict[str, Any]) -> float:
    count = int(record.get("count", 0))
    first = record.get("first_monotonic_s")
    last = record.get("last_monotonic_s")
    if count < 2 or not isinstance(first, (int, float)) or not isinstance(last, (int, float)) or last <= first:
        return 0.0
    return (count - 1) / (last - first)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def update_sample_record(record: dict[str, Any], now_mono: float, now_wall: float) -> None:
    previous = record.get("last_monotonic_s")
    if record.get("first_monotonic_s") is None:
        record["first_monotonic_s"] = now_mono
    if isinstance(previous, (int, float)):
        record["max_gap_s"] = max(float(record.get("max_gap_s", 0.0)), now_mono - previous)
    record["last_monotonic_s"] = now_mono
    record["last_wall_s"] = now_wall
    record["count"] += 1


def finite_number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def rate_from_ns(record: dict[str, Any], first_key: str, last_key: str) -> float:
    count = int(record.get("count", 0))
    first = record.get(first_key)
    last = record.get(last_key)
    if count < 2 or not finite_number(first) or not finite_number(last) or last <= first:
        return 0.0
    return (count - 1) / ((last - first) / 1_000_000_000)


def process_group_counts(process_group: int) -> tuple[dict[str, int], list[dict[str, Any]], str | None]:
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid=,pgid=,stat=,comm=,args="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {}, [], str(exc)
    if result.returncode != 0:
        return {}, [], f"ps exited {result.returncode}: {result.stderr.strip()}"
    processes: list[dict[str, Any]] = []
    counts = {"arducopter": 0, "mavproxy": 0, "micro_ros_agent": 0, "gazebo": 0}
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 4)
        if len(fields) < 5:
            continue
        try:
            pid = int(fields[0])
            pgid = int(fields[1])
        except ValueError:
            continue
        if pgid != process_group:
            continue
        stat, command, arguments = fields[2:]
        processes.append({"pid": pid, "stat": stat, "command": command, "arguments": arguments})
        lowered = f"{command} {arguments}".lower()
        if "arducopter" in lowered:
            counts["arducopter"] += 1
        if "mavproxy.py" in lowered:
            counts["mavproxy"] += 1
        if "micro_ros_agent" in lowered:
            counts["micro_ros_agent"] += 1
        if "gz sim" in lowered or command in {"gz", "ruby"} and "gz_sim" in lowered:
            counts["gazebo"] += 1
    return counts, processes, None


def readiness_status(
    expected_names: list[str],
    odometry: dict[str, dict[str, Any]],
    heartbeats: dict[int, dict[str, Any]],
    mavlink_positions: dict[int, dict[str, Any]],
) -> tuple[bool, dict[str, dict[str, int]]]:
    """Return readiness only after every UAV has odometry, heartbeat, and GPS."""
    odometry_counts = {
        name: int((odometry.get(name) or {}).get("count", 0)) for name in expected_names
    }
    heartbeat_counts = {
        str(system_id): int((heartbeats.get(system_id) or {}).get("count", 0))
        for system_id in range(1, 6)
    }
    mavlink_position_counts = {
        str(system_id): int((mavlink_positions.get(system_id) or {}).get("count", 0))
        for system_id in range(1, 6)
    }
    valid_home_position_counts = {
        str(system_id): int(
            (mavlink_positions.get(system_id) or {}).get("valid_home_position_count", 0)
        )
        for system_id in range(1, 6)
    }
    details = {
        "odometry_counts": odometry_counts,
        "heartbeat_counts": heartbeat_counts,
        "mavlink_position_counts": mavlink_position_counts,
        "mavlink_valid_home_position_counts": valid_home_position_counts,
    }
    ready = (
        all(count >= 2 for count in odometry_counts.values())
        and all(count >= 1 for count in heartbeat_counts.values())
        and all(count >= 2 for count in mavlink_position_counts.values())
        and all(count >= 2 for count in valid_home_position_counts.values())
    )
    return ready, details


def selected_measurement_duration(ready: bool, requested_duration_s: float) -> float:
    """Avoid a long observation after readiness has already failed."""
    return requested_duration_s if ready else 0.0


def run_process_monitor(
    process_group: int,
    started_mono: float,
    stop_event: threading.Event,
    samples: list[dict[str, Any]],
    event_log: Any,
    *,
    interval_s: float = 1.0,
    sampler: Callable[
        [int], tuple[dict[str, int], list[dict[str, Any]], str | None]
    ] = process_group_counts,
) -> None:
    """Sample launch processes off the ROS executor thread."""
    next_sample = started_mono
    while not stop_event.is_set():
        counts, processes, process_error = sampler(process_group)
        sampled_mono = time.monotonic()
        if stop_event.is_set():
            break
        sample = {
            "offset_s": sampled_mono - started_mono,
            "counts": counts,
            "processes": processes,
            "error": process_error,
        }
        samples.append(sample)
        event_log.emit("process_sample", **sample)
        next_sample += interval_s
        stop_event.wait(max(0.0, next_sample - time.monotonic()))


class EventLog:
    def __init__(
        self,
        path: Path,
        *,
        run_id: str,
        runtime_id: str,
        source_hash: str,
        contract_sha256: str,
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = path.open("x", encoding="utf-8")
        self._lock = threading.Lock()
        self._run_id = run_id
        self._runtime_id = runtime_id
        self._source_hash = source_hash
        self._contract_sha256 = contract_sha256
        self._event_seq = 0

    def emit(self, event: str, **fields: Any) -> None:
        with self._lock:
            self._event_seq += 1
            record = {
                **fields,
                "schema_version": 2,
                "run_id": self._run_id,
                "runtime_id": self._runtime_id,
                "source_hash": self._source_hash,
                "contract": M1_CONTRACT_ID,
                "plan_version": 3,
                "contract_sha256": self._contract_sha256,
                "event_seq": self._event_seq,
                "event": event,
                "wall_utc": utc_now(),
                "monotonic_ns": time.monotonic_ns(),
            }
            self._handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
            self._handle.flush()

    def close(self) -> None:
        with self._lock:
            self._handle.close()


def discover_gazebo_models(world: str, event_log: EventLog) -> tuple[set[str], str | None]:
    command = [
        "gz",
        "service",
        "-s",
        f"/world/{world}/scene/info",
        "--reqtype",
        "gz.msgs.Empty",
        "--reptype",
        "gz.msgs.Scene",
        "--timeout",
        "5000",
        "--req",
        "",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        event_log.emit("gazebo_scene_probe_failed", error=str(exc))
        return set(), str(exc)
    text = result.stdout + "\n" + result.stderr
    names = set(re.findall(r'\bname:\s*"([^"]+)"', text))
    event_log.emit(
        "gazebo_scene_probe",
        exit_code=result.returncode,
        world_name=world,
        model_names=sorted(name for name in names if name.startswith("uav")),
    )
    if result.returncode != 0:
        return names, f"gz scene/info exited {result.returncode}"
    return names, None


def launch_log_findings(path: Path, observation_offset: int = 0) -> list[str]:
    if not path.is_file():
        return [f"launch log is missing: {path}"]
    raw = path.read_bytes()
    full_text = raw.decode(errors="replace").lower()
    safe_offset = min(max(observation_offset, 0), len(raw))
    observation_text = raw[safe_offset:].decode(errors="replace").lower()
    findings = [pattern for pattern in FATAL_LAUNCH_PATTERNS if pattern in full_text]
    findings.extend(
        pattern for pattern in OBSERVATION_WINDOW_PATTERNS if pattern in observation_text
    )
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", type=Path, default=ROOT_DIR / "network/config/scenario_5uav.yaml")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--duration-s", type=float, default=300.0)
    parser.add_argument("--minimum-duration-s", type=float, default=300.0)
    parser.add_argument("--heartbeat-endpoint", default="udpin:0.0.0.0:14550")
    parser.add_argument("--minimum-heartbeat-hz", type=float, default=0.8)
    parser.add_argument("--maximum-wall-heartbeat-gap-s", type=float, default=15.0)
    parser.add_argument("--minimum-odometry-hz", type=float, default=5.0)
    parser.add_argument("--maximum-freshness-age-s", type=float, default=1.0)
    parser.add_argument("--world", default="map")
    parser.add_argument("--launch-log", type=Path)
    parser.add_argument("--runtime-id", default=os.environ.get("AMS_RUNTIME_ID"))
    parser.add_argument("--launch-process-group", type=int)
    parser.add_argument(
        "--launch-log-observation-offset",
        type=int,
        default=0,
        help="Byte offset after warm-up; link-down is evaluated only after this point.",
    )
    args = parser.parse_args(argv)

    run_dir = args.run_dir.resolve()
    provenance = json.loads((run_dir / "metrics/provenance.json").read_text(encoding="utf-8"))
    if not args.runtime_id or len(args.runtime_id) < 8:
        print("FAIL --runtime-id is required", file=sys.stderr)
        return 2
    config_hashes = provenance.get("config_hashes") if isinstance(provenance.get("config_hashes"), dict) else {}
    contract_sha256 = config_hashes.get(M1_PLAN_PATH)
    if not isinstance(contract_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", contract_sha256) is None:
        print("FAIL provenance does not bind the v3 M1 contract hash", file=sys.stderr)
        return 2
    scenario = yaml.safe_load(args.scenario.read_text()) or {}
    robots = scenario.get("robots") or []
    sitl_home_text = str((scenario.get("base_simulation") or {}).get("sitl_home", ""))
    try:
        home_lat, home_lon, home_alt, _home_heading = (
            float(value) for value in sitl_home_text.split(",")
        )
    except (TypeError, ValueError):
        print(f"FAIL scenario base_simulation.sitl_home is invalid: {sitl_home_text!r}", file=sys.stderr)
        return 2
    expected_names = [str(robot["name"]) for robot in robots]
    if expected_names != [f"uav{i}" for i in range(1, 6)]:
        print(f"FAIL scenario must define uav1..uav5 in order, got {expected_names}", file=sys.stderr)
        return 2

    event_log = EventLog(
        run_dir / "logs/five_uav_health_events.jsonl",
        run_id=run_dir.name,
        runtime_id=args.runtime_id,
        source_hash=str(provenance.get("source_hash")),
        contract_sha256=contract_sha256,
    )
    event_log.emit(
        "health_probe_start",
        component_only=True,
        packet_path_eligible=False,
        expected_uavs=expected_names,
        duration_s=args.duration_s,
        run_id=run_dir.name,
        runtime_id=args.runtime_id,
        source_hash=provenance.get("source_hash"),
    )

    try:
        import rclpy
        from nav_msgs.msg import Odometry
        from rclpy.node import Node
        from rclpy.qos import qos_profile_sensor_data
    except ImportError as exc:
        event_log.emit("health_probe_failed", error=f"ROS import failed: {exc}")
        event_log.close()
        print(f"FAIL ROS 2 Python environment is required: {exc}", file=sys.stderr)
        return 2

    try:
        from pymavlink import mavutil
    except ImportError as exc:
        event_log.emit("health_probe_failed", error=f"pymavlink import failed: {exc}")
        event_log.close()
        print(f"FAIL pymavlink is required: {exc}", file=sys.stderr)
        return 2

    odometry: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "count": 0,
            "first_monotonic_s": None,
            "last_monotonic_s": None,
            "last_wall_s": None,
            "max_gap_s": 0.0,
            "invalid_samples": 0,
            "first_stamp_ns": None,
            "last_stamp_ns": None,
            "nonadvancing_stamps": 0,
            "first_position_m": None,
            "last_position_m": None,
            "max_displacement_m": 0.0,
            "max_speed_mps": 0.0,
        }
    )
    heartbeats: dict[int, dict[str, Any]] = defaultdict(
        lambda: {
            "count": 0,
            "first_monotonic_s": None,
            "last_monotonic_s": None,
            "last_wall_s": None,
            "max_gap_s": 0.0,
            "first_sim_time_ns": None,
            "last_sim_time_ns": None,
            "max_sim_gap_s": 0.0,
        }
    )
    mavlink_positions: dict[int, dict[str, Any]] = defaultdict(
        lambda: {
            "count": 0,
            "minimum_relative_alt_m": None,
            "maximum_relative_alt_m": None,
            "last_relative_alt_m": None,
            "last_lat_deg": None,
            "last_lon_deg": None,
            "maximum_home_distance_m": 0.0,
            "valid_home_position_count": 0,
        }
    )
    stop_event = threading.Event()
    measurement_active = threading.Event()
    data_lock = threading.Lock()
    heartbeat_errors: list[str] = []

    class HealthNode(Node):
        def __init__(self) -> None:
            super().__init__("network_five_uav_health_probe")
            for name in expected_names:
                topic = f"/{name}/odometry"
                self.create_subscription(
                    Odometry,
                    topic,
                    lambda msg, robot_name=name, source_topic=topic: self.on_odometry(
                        robot_name, source_topic, msg
                    ),
                    qos_profile_sensor_data,
                )

        def on_odometry(self, name: str, topic: str, message: Any) -> None:
            now_mono = time.monotonic()
            now_wall = time.time()
            position = message.pose.pose.position
            orientation = message.pose.pose.orientation
            linear = message.twist.twist.linear
            angular = message.twist.twist.angular
            values = (
                position.x,
                position.y,
                position.z,
                orientation.x,
                orientation.y,
                orientation.z,
                orientation.w,
                linear.x,
                linear.y,
                linear.z,
                angular.x,
                angular.y,
                angular.z,
            )
            quaternion_norm = math.sqrt(
                orientation.x**2 + orientation.y**2 + orientation.z**2 + orientation.w**2
            )
            stamp_ns = int(message.header.stamp.sec) * 1_000_000_000 + int(
                message.header.stamp.nanosec
            )
            valid = all(math.isfinite(float(value)) for value in values) and 0.5 <= quaternion_norm <= 1.5
            with data_lock:
                record = odometry[name]
                if not valid:
                    record["invalid_samples"] += 1
                previous_stamp = record.get("last_stamp_ns")
                if previous_stamp is not None and stamp_ns <= previous_stamp:
                    record["nonadvancing_stamps"] += 1
                if record.get("first_stamp_ns") is None:
                    record["first_stamp_ns"] = stamp_ns
                record["last_stamp_ns"] = stamp_ns
                position_m = [float(position.x), float(position.y), float(position.z)]
                if record.get("first_position_m") is None:
                    record["first_position_m"] = position_m
                first_position = record["first_position_m"]
                displacement = math.sqrt(
                    sum((position_m[index] - first_position[index]) ** 2 for index in range(3))
                )
                speed = math.sqrt(linear.x**2 + linear.y**2 + linear.z**2)
                record["last_position_m"] = position_m
                record["max_displacement_m"] = max(record["max_displacement_m"], displacement)
                record["max_speed_mps"] = max(record["max_speed_mps"], speed)
                update_sample_record(record, now_mono, now_wall)
                sequence = record["count"]
                sample_is_active = measurement_active.is_set()
            event_log.emit(
                "odometry" if sample_is_active else "readiness_odometry",
                uav=name,
                source_topic=topic,
                sequence=sequence,
                stamp_ns=stamp_ns,
                valid=valid,
                position_m=position_m,
                linear_speed_mps=speed,
            )

    def heartbeat_worker() -> None:
        try:
            connection = mavutil.mavlink_connection(args.heartbeat_endpoint, autoreconnect=True)
        except Exception as exc:
            heartbeat_errors.append(str(exc))
            event_log.emit("heartbeat_endpoint_failed", error=str(exc))
            return
        try:
            while not stop_event.is_set():
                message = connection.recv_match(blocking=True, timeout=0.5)
                if message is None:
                    continue
                system_id = int(message.get_srcSystem())
                if system_id not in range(1, 6):
                    continue
                message_type = message.get_type()
                if message_type == "GLOBAL_POSITION_INT":
                    lat_deg = float(message.lat) / 10_000_000
                    lon_deg = float(message.lon) / 10_000_000
                    relative_alt_m = float(message.relative_alt) / 1000
                    north_m = (lat_deg - home_lat) * 111_320.0
                    east_m = (lon_deg - home_lon) * 111_320.0 * math.cos(math.radians(home_lat))
                    home_distance_m = math.hypot(north_m, east_m)
                    with data_lock:
                        position_record = mavlink_positions[system_id]
                        position_record["count"] += 1
                        position_record["last_relative_alt_m"] = relative_alt_m
                        position_record["last_lat_deg"] = lat_deg
                        position_record["last_lon_deg"] = lon_deg
                        if abs(lat_deg) >= 1.0 and abs(lon_deg) >= 1.0:
                            position_record["valid_home_position_count"] += 1
                            position_record["maximum_home_distance_m"] = max(
                                position_record["maximum_home_distance_m"], home_distance_m
                            )
                        minimum = position_record.get("minimum_relative_alt_m")
                        maximum = position_record.get("maximum_relative_alt_m")
                        position_record["minimum_relative_alt_m"] = (
                            relative_alt_m if minimum is None else min(minimum, relative_alt_m)
                        )
                        position_record["maximum_relative_alt_m"] = (
                            relative_alt_m if maximum is None else max(maximum, relative_alt_m)
                        )
                        position_sequence = position_record["count"]
                        sample_is_active = measurement_active.is_set()
                    event_log.emit(
                        "mavlink_global_position"
                        if sample_is_active
                        else "readiness_mavlink_global_position",
                        system_id=system_id,
                        sequence=position_sequence,
                        lat_deg=lat_deg,
                        lon_deg=lon_deg,
                        relative_alt_m=relative_alt_m,
                        home_distance_m=home_distance_m,
                    )
                    continue
                if message_type != "HEARTBEAT":
                    continue
                now_mono = time.monotonic()
                now_wall = time.time()
                with data_lock:
                    record = heartbeats[system_id]
                    sim_candidates = sorted(
                        int(item["last_stamp_ns"])
                        for item in odometry.values()
                        if finite_number(item.get("last_stamp_ns"))
                    )
                    sim_time_ns = (
                        sim_candidates[len(sim_candidates) // 2] if sim_candidates else None
                    )
                if sim_time_ns is None:
                    event_log.emit("heartbeat_unstamped", system_id=system_id)
                    continue
                with data_lock:
                    record = heartbeats[system_id]
                    update_sample_record(record, now_mono, now_wall)
                    previous_sim = record.get("last_sim_time_ns")
                    if record.get("first_sim_time_ns") is None:
                        record["first_sim_time_ns"] = sim_time_ns
                    if finite_number(previous_sim):
                        record["max_sim_gap_s"] = max(
                            float(record.get("max_sim_gap_s", 0.0)),
                            (sim_time_ns - int(previous_sim)) / 1_000_000_000,
                        )
                    record["last_sim_time_ns"] = sim_time_ns
                    heartbeat_sequence = record["count"]
                    sample_is_active = measurement_active.is_set()
                event_log.emit(
                    "heartbeat" if sample_is_active else "readiness_heartbeat",
                    system_id=system_id,
                    sequence=heartbeat_sequence,
                    sim_time_ns=sim_time_ns,
                )
        except Exception as exc:
            heartbeat_errors.append(str(exc))
            event_log.emit("heartbeat_worker_failed", error=str(exc))
        finally:
            try:
                connection.close()
            except Exception:
                pass

    rclpy.init(args=None)
    node = HealthNode()
    heartbeat_thread = threading.Thread(target=heartbeat_worker, daemon=True)
    heartbeat_thread.start()
    readiness_started_mono = time.monotonic()
    readiness_deadline = readiness_started_mono + 30.0
    ready = False
    readiness_details: dict[str, dict[str, int]] = {}
    while time.monotonic() < readiness_deadline:
        rclpy.spin_once(node, timeout_sec=0.1)
        with data_lock:
            ready, readiness_details = readiness_status(
                expected_names, odometry, heartbeats, mavlink_positions
            )
        if ready:
            break
    readiness_failure = None if ready else "five-UAV streams did not become ready within 30 seconds"
    readiness_elapsed_s = time.monotonic() - readiness_started_mono
    event_log.emit(
        "readiness",
        ready=ready,
        error=readiness_failure,
        elapsed_s=readiness_elapsed_s,
        **readiness_details,
    )
    started_wall = time.time()
    started_mono = time.monotonic()
    event_log.emit(
        "measurement_start",
        run_id=run_dir.name,
        runtime_id=args.runtime_id,
        source_hash=provenance.get("source_hash"),
        measurement_started_monotonic_ns=int(started_mono * 1_000_000_000),
    )
    with data_lock:
        odometry.clear()
        heartbeats.clear()
        mavlink_positions.clear()
        measurement_active.set()
    interrupted = False
    process_samples: list[dict[str, Any]] = []
    process_thread: threading.Thread | None = None
    if args.launch_process_group:
        process_thread = threading.Thread(
            target=run_process_monitor,
            args=(
                args.launch_process_group,
                started_mono,
                stop_event,
                process_samples,
                event_log,
            ),
            daemon=True,
        )
        process_thread.start()
    measurement_duration_s = selected_measurement_duration(ready, args.duration_s)
    try:
        while time.monotonic() - started_mono < measurement_duration_s:
            rclpy.spin_once(node, timeout_sec=0.1)
    except KeyboardInterrupt:
        interrupted = True
        event_log.emit("health_probe_interrupted")
    finally:
        measurement_ended_mono = time.monotonic()
        measurement_ended_wall = time.time()
        stop_event.set()
        heartbeat_thread.join(timeout=2.0)
        if process_thread is not None:
            process_thread.join(timeout=6.0)
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()

    if heartbeat_thread.is_alive():
        heartbeat_errors.append("heartbeat worker did not stop")
    if process_thread is not None and process_thread.is_alive():
        heartbeat_errors.append("process monitor did not stop")

    observed_duration = measurement_ended_mono - started_mono
    models, gazebo_probe_error = discover_gazebo_models(args.world, event_log)
    launch_log = args.launch_log.resolve() if args.launch_log else run_dir / "logs/five_uav_launch.log"
    critical = launch_log_findings(launch_log, args.launch_log_observation_offset)
    results = []
    all_failures: list[str] = []
    for robot in robots:
        name = str(robot["name"])
        system_id = int(robot.get("system_id", len(results) + 1))
        dds_port = int(robot.get("dds_udp_port", 2019 + len(results)))
        odom = dict(odometry[name])
        heartbeat = dict(heartbeats[system_id])
        mavlink_position = dict(mavlink_positions[system_id])
        odom_rate = rate_hz(odom)
        odom_sim_span_s = (
            0.0
            if not finite_number(odom.get("first_stamp_ns"))
            or not finite_number(odom.get("last_stamp_ns"))
            else (int(odom["last_stamp_ns"]) - int(odom["first_stamp_ns"])) / 1_000_000_000
        )
        odom_wall_span_s = (
            0.0
            if not finite_number(odom.get("first_monotonic_s"))
            or not finite_number(odom.get("last_monotonic_s"))
            else float(odom["last_monotonic_s"]) - float(odom["first_monotonic_s"])
        )
        odom_realtime_factor = odom_sim_span_s / odom_wall_span_s if odom_wall_span_s > 0 else 0.0
        heartbeat_wall_rate = rate_hz(heartbeat)
        heartbeat_sim_rate = rate_from_ns(
            heartbeat, "first_sim_time_ns", "last_sim_time_ns"
        )
        odom_age = (
            None
            if odom.get("last_wall_s") is None
            else measurement_ended_mono - float(odom["last_monotonic_s"])
        )
        heartbeat_age = (
            None
            if heartbeat.get("last_wall_s") is None
            else measurement_ended_mono - float(heartbeat["last_monotonic_s"])
        )
        odom_start_delay = (
            None
            if odom.get("first_monotonic_s") is None
            else float(odom["first_monotonic_s"]) - started_mono
        )
        heartbeat_start_delay = (
            None
            if heartbeat.get("first_monotonic_s") is None
            else float(heartbeat["first_monotonic_s"]) - started_mono
        )
        heartbeat_sim_age = (
            None
            if not finite_number(heartbeat.get("last_sim_time_ns"))
            or not finite_number(odom.get("last_stamp_ns"))
            else (int(odom["last_stamp_ns"]) - int(heartbeat["last_sim_time_ns"]))
            / 1_000_000_000
        )
        heartbeat_sim_start_delay = (
            None
            if not finite_number(heartbeat.get("first_sim_time_ns"))
            or not finite_number(odom.get("first_stamp_ns"))
            else (int(heartbeat["first_sim_time_ns"]) - int(odom["first_stamp_ns"]))
            / 1_000_000_000
        )
        failures = []
        gazebo_model = name in models
        heartbeat_ok = (
            heartbeat_sim_rate >= args.minimum_heartbeat_hz
            and heartbeat_sim_age is not None
            and 0.0 <= heartbeat_sim_age <= 3.0
            and heartbeat_sim_start_delay is not None
            and -0.1 <= heartbeat_sim_start_delay <= 3.0
            and float(heartbeat.get("max_sim_gap_s", float("inf"))) <= 3.0
            and heartbeat_age is not None
            and heartbeat_age <= args.maximum_wall_heartbeat_gap_s
            and float(heartbeat.get("max_gap_s", float("inf")))
            <= args.maximum_wall_heartbeat_gap_s
        )
        mavlink_pose_ok = (
            int(mavlink_position.get("count", 0)) >= 2
            and finite_number(mavlink_position.get("minimum_relative_alt_m"))
            and float(mavlink_position["minimum_relative_alt_m"]) >= -20.0
            and finite_number(mavlink_position.get("maximum_relative_alt_m"))
            and float(mavlink_position["maximum_relative_alt_m"]) <= 100.0
            and finite_number(mavlink_position.get("maximum_home_distance_m"))
            and float(mavlink_position["maximum_home_distance_m"]) <= 500.0
            and int(mavlink_position.get("valid_home_position_count", 0)) >= 2
        )
        odometry_fresh = (
            odom_rate >= args.minimum_odometry_hz
            and odom_age is not None
            and odom_age <= args.maximum_freshness_age_s
            and odom_start_delay is not None
            and odom_start_delay <= args.maximum_freshness_age_s
            and float(odom.get("max_gap_s", float("inf"))) <= args.maximum_freshness_age_s
            and int(odom.get("invalid_samples", 0)) == 0
            and int(odom.get("nonadvancing_stamps", 0)) == 0
            and int(odom.get("last_stamp_ns") or 0) > int(odom.get("first_stamp_ns") or 0)
            and float(odom.get("max_displacement_m", float("inf"))) <= 20.0
            and float(odom.get("max_speed_mps", float("inf"))) <= 100.0
            and 0.1 <= odom_realtime_factor <= 1.1
        )
        if not gazebo_model:
            failures.append("Gazebo model not found in scene/info")
        if not heartbeat_ok:
            failures.append(
                "heartbeat simulated-time coverage failed: "
                f"rate={heartbeat_sim_rate:.3f} age={heartbeat_sim_age} "
                f"max_gap={heartbeat.get('max_sim_gap_s')}"
            )
        if not mavlink_pose_ok:
            failures.append(f"MAVLink pose is missing or out of bounds: {mavlink_position}")
        if not odometry_fresh:
            failures.append(f"odometry rate/age failed: rate={odom_rate:.3f} age={odom_age}")
        all_failures.extend(f"{name}: {item}" for item in failures)
        results.append(
            {
                "name": name,
                "system_id": system_id,
                "dds_udp_port": dds_port,
                "gazebo_model": gazebo_model,
                "sitl_healthy": heartbeat_ok and mavlink_pose_ok,
                "heartbeat": heartbeat_ok,
                "heartbeat_count": heartbeat.get("count", 0),
                "heartbeat_rate_hz": round(heartbeat_sim_rate, 6),
                "heartbeat_time_basis": "odometry_sim_stamp",
                "heartbeat_wall_rate_hz": round(heartbeat_wall_rate, 6),
                "heartbeat_age_s": None if heartbeat_age is None else round(heartbeat_age, 6),
                "heartbeat_sim_age_s": None
                if heartbeat_sim_age is None
                else round(heartbeat_sim_age, 6),
                "heartbeat_sim_start_delay_s": None
                if heartbeat_sim_start_delay is None
                else round(heartbeat_sim_start_delay, 6),
                "heartbeat_sim_max_gap_s": round(
                    float(heartbeat.get("max_sim_gap_s", 0.0)), 6
                ),
                "heartbeat_start_delay_s": None
                if heartbeat_start_delay is None
                else round(heartbeat_start_delay, 6),
                "heartbeat_max_gap_s": round(float(heartbeat.get("max_gap_s", 0.0)), 6),
                "odometry_fresh": odometry_fresh,
                "odometry_count": odom.get("count", 0),
                "odometry_rate_hz": round(odom_rate, 6),
                "odometry_realtime_factor": round(odom_realtime_factor, 6),
                "odometry_age_s": None if odom_age is None else round(odom_age, 6),
                "odometry_start_delay_s": None
                if odom_start_delay is None
                else round(odom_start_delay, 6),
                "odometry_max_gap_s": round(float(odom.get("max_gap_s", 0.0)), 6),
                "odometry_invalid_samples": int(odom.get("invalid_samples", 0)),
                "odometry_nonadvancing_stamps": int(odom.get("nonadvancing_stamps", 0)),
                "odometry_first_position_m": odom.get("first_position_m"),
                "odometry_last_position_m": odom.get("last_position_m"),
                "odometry_max_displacement_m": round(
                    float(odom.get("max_displacement_m", 0.0)), 6
                ),
                "odometry_max_speed_mps": round(float(odom.get("max_speed_mps", 0.0)), 6),
                "mavlink_pose": mavlink_pose_ok,
                "mavlink_position_count": int(mavlink_position.get("count", 0)),
                "mavlink_minimum_relative_alt_m": mavlink_position.get(
                    "minimum_relative_alt_m"
                ),
                "mavlink_maximum_relative_alt_m": mavlink_position.get(
                    "maximum_relative_alt_m"
                ),
                "mavlink_maximum_home_distance_m": mavlink_position.get(
                    "maximum_home_distance_m"
                ),
                "mavlink_valid_home_position_count": int(
                    mavlink_position.get("valid_home_position_count", 0)
                ),
                "failures": failures,
            }
        )

    if observed_duration < args.minimum_duration_s:
        all_failures.append(
            f"observed duration {observed_duration:.3f}s is below {args.minimum_duration_s:.3f}s"
        )
    if readiness_failure:
        all_failures.append(readiness_failure)
    if interrupted:
        all_failures.append("health observation was interrupted")
    if len({item["system_id"] for item in results}) != 5:
        all_failures.append("system IDs are not unique")
    if len({item["dds_udp_port"] for item in results}) != 5:
        all_failures.append("DDS UDP ports are not unique")
    if gazebo_probe_error:
        all_failures.append(gazebo_probe_error)
    if critical:
        all_failures.extend(f"launch log contains {item!r}" for item in critical)
    if heartbeat_errors:
        all_failures.extend(f"heartbeat worker: {item}" for item in heartbeat_errors)
    required_process_counts = {"arducopter": 5, "mavproxy": 5, "micro_ros_agent": 5, "gazebo": 1}
    if not args.launch_process_group:
        all_failures.append("launch process group was not supplied")
    if not process_samples:
        all_failures.append("no process-health samples were recorded")
    for name, minimum in required_process_counts.items():
        observed = [sample.get("counts", {}).get(name, 0) for sample in process_samples]
        if not observed or min(observed) < minimum:
            all_failures.append(f"process {name} count fell below {minimum}: {observed}")
    if any(sample.get("error") for sample in process_samples):
        all_failures.append("process-health sampling reported errors")

    summary = {
        "schema_version": 2,
        "contract": M1_CONTRACT_ID,
        "plan_version": 3,
        "contract_sha256": contract_sha256,
        "run_id": run_dir.name,
        "runtime_id": args.runtime_id,
        "source_hash": provenance.get("source_hash"),
        "component_only": True,
        "packet_path_eligible": False,
        "started_utc": datetime.fromtimestamp(started_wall, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ended_utc": datetime.fromtimestamp(measurement_ended_wall, timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "observed_duration_s": round(observed_duration, 6),
        "minimum_duration_s": args.minimum_duration_s,
        "minimum_heartbeat_hz": args.minimum_heartbeat_hz,
        "minimum_odometry_hz": args.minimum_odometry_hz,
        "maximum_freshness_age_s": args.maximum_freshness_age_s,
        "readiness": {
            "ready": ready,
            "elapsed_s": round(readiness_elapsed_s, 6),
            **readiness_details,
        },
        "uavs": results,
        "process_health": {
            "process_group": args.launch_process_group,
            "samples": len(process_samples),
            "required_minimums": required_process_counts,
            "observed_minimums": {
                name: min(
                    (sample.get("counts", {}).get(name, 0) for sample in process_samples),
                    default=0,
                )
                for name in required_process_counts
            },
        },
        "gazebo_model_names": sorted(name for name in models if name.startswith("uav")),
        "gazebo_world_name": args.world,
        "launch_log": str(launch_log.relative_to(run_dir)),
        "launch_log_observation_offset": args.launch_log_observation_offset,
        "errors": all_failures,
        "passed": not all_failures,
    }
    event_log.emit(
        "health_probe_complete",
        passed=summary["passed"],
        errors=all_failures,
        observed_duration_s=observed_duration,
        measurement_ended_monotonic_ns=int(measurement_ended_mono * 1_000_000_000),
    )
    event_log.close()
    raw_event_log = run_dir / "logs/five_uav_health_events.jsonl"
    summary["raw_event_log"] = str(raw_event_log.relative_to(run_dir))
    summary["raw_event_sha256"] = sha256_file(raw_event_log)
    output = run_dir / "metrics/five_uav_health.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(summary, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    print(f"Five-UAV health: {output}")
    print(f"Passed: {str(summary['passed']).lower()}")
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
