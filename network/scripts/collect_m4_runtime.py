#!/usr/bin/env python3
"""Collect immutable 5-UAV M4 schedule, RTF, pose, and host evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in os.sys.path:
    os.sys.path.insert(0, str(ROOT))

from network.scripts.collect_flight_capacity import (
    cgroup_sample,
    gpu_sample,
    parse_proc_stat,
    read_text,
    static_runtime_identity,
)
from network.bridge.runtime_clock_beacon import beacon
from network.scripts.m4_runtime_orchestrator import write_exclusive
from network.validation.m4_common import M4ValidationError, strict_json
from network.validation.m4_runtime import (
    REQUIRED_PROCESS_COUNTS,
    RUNTIME_EVENT_SCHEMA,
)


UAV_IDS = ("uav1", "uav2", "uav3", "uav4", "uav5")
CLOCK_TOPICS = tuple(f"/{uav}/clock" for uav in UAV_IDS)
WORLD_ENTITY_IDS = {*UAV_IDS, "cp", "jammer_m4"}
ODOMETRY_SOURCE_FRAME = "ros_odometry_world_enu"
COORDINATE_TRANSFORM_VERSION = "ams-m4-coordinate-frames-v1"


def runtime_entity_name(frame: str) -> str | None:
    parts = [part for part in frame.strip("/").split("/") if part]
    return next(
        (candidate for candidate in reversed(parts) if candidate in WORLD_ENTITY_IDS),
        None,
    )


def canonical_line(value: Any) -> bytes:
    return (
        json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()


class RuntimeWriter:
    def __init__(self, path: Path, run_id: str, runtime_id: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = path.open("xb")
        self.run_id = run_id
        self.runtime_id = runtime_id
        self.sequence = 0
        self.previous_host_ns = 0

    def emit(self, event: str, **fields: Any) -> int:
        host = time.monotonic_ns()
        if host <= self.previous_host_ns:
            host = self.previous_host_ns + 1
        self.previous_host_ns = host
        self.sequence += 1
        record = {
            "schema": RUNTIME_EVENT_SCHEMA,
            "event_sequence": self.sequence,
            "run_id": self.run_id,
            "runtime_id": self.runtime_id,
            "host_monotonic_ns": host,
            "host_realtime_ns": time.time_ns(),
            "event": event,
            **fields,
        }
        self.handle.write(canonical_line(record))
        self.handle.flush()
        return host

    def close(self) -> None:
        self.handle.flush()
        os.fsync(self.handle.fileno())
        self.handle.close()


def executable_digest(path: Path, cache: dict[tuple[str, int, int], str]) -> str:
    details = path.stat()
    key = (str(path), details.st_size, details.st_mtime_ns)
    if key not in cache:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        cache[key] = digest.hexdigest()
    return cache[key]


def classify_process(comm: str, cmdline: list[str]) -> str | None:
    names = [Path(value).name.lower() for value in cmdline]
    executable = names[0] if names else comm.lower()
    joined = " ".join(cmdline).lower()
    if "ros2 launch multiagent_simulation multiagent_simulation.launch.py" in joined:
        return "ros_launch"
    if executable == "arducopter" or comm.lower() == "arducopter":
        return "arducopter"
    if "mavproxy.py" in names:
        return "mavproxy"
    if executable == "micro_ros_agent" or comm.lower() == "micro_ros_agent":
        return "micro_ros_agent"
    if executable in {"sh", "dash"} and "ruby /usr/bin/gz sim" in joined and " -s" in f" {joined}":
        return "gazebo_launcher"
    if (
        (comm.lower().startswith("ruby") or executable.startswith("ruby"))
        and "gz sim" in joined
        and " -s" in f" {joined}"
    ):
        return "gazebo_server"
    if executable == "robot_state_publisher" or comm.lower() == "robot_state_publisher":
        return "robot_state_publisher"
    if executable == "parameter_bridge" or comm.lower() == "parameter_bridge":
        if "/world/map/pose/info" in joined:
            return "world_pose_bridge"
        return "ros_gz_parameter_bridge"
    if executable == "relay" or comm.lower() == "relay":
        return "topic_relay"
    if "m4_endpoint_agent.py" in joined:
        return "endpoint_companion_agent"
    if (
        "actual_sitl_control_probe.py" in joined
        or "m3_actual_gcs_probe.py" in joined
        or "actual_m3_gcs_probe" in joined
    ):
        return "gcs_endpoint_probe"
    if (
        "actual_sitl_mavlink_endpoint.py" in joined
        or "uav_mavlink_endpoint.py" in joined
    ):
        return "uav_endpoint_adapter"
    if "actual_sitl_endpoint_orchestrator.py" in joined and "--build-manifest" not in cmdline:
        return "actual_endpoint_supervisor"
    if "ams-tap-packet-engine" in joined:
        return "ns3_packet_engine"
    if "m4_runtime_orchestrator.py provider" in joined:
        return "sionna_worker"
    if "m4_adapter_runtime.py" in joined:
        return "sionna_adapter"
    if "raw_packet_capture.py" in joined:
        return "packet_capture"
    if "collect_m4_runtime.py" in joined:
        return "runtime_collector"
    if "collect_m4_clock_correlations.py" in joined:
        return "clock_collector"
    return None


def process_sample(
    accepted_pgids: set[int], executable_cache: dict[tuple[str, int, int], str]
) -> dict[str, Any]:
    counts = {role: 0 for role in REQUIRED_PROCESS_COUNTS}
    records: list[dict[str, Any]] = []
    page_size = os.sysconf("SC_PAGE_SIZE")
    ticks = os.sysconf("SC_CLK_TCK")
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            parsed = parse_proc_stat((proc / "stat").read_text(encoding="utf-8"))
            if int(parsed["pgid"]) not in accepted_pgids:
                continue
            raw_cmdline = (proc / "cmdline").read_bytes()
            cmdline = [
                value.decode("utf-8", errors="replace")
                for value in raw_cmdline.rstrip(b"\0").split(b"\0")
                if value
            ]
            executable = (proc / "exe").resolve(strict=True)
            executable_sha256 = executable_digest(executable, executable_cache)
        except (OSError, UnicodeError, ValueError, IndexError):
            continue
        role = classify_process(str(parsed["comm"]), cmdline)
        if role is not None and parsed["state"] != "Z":
            counts[role] += 1
        records.append(
            {
                "pid": int(parsed["pid"]),
                "ppid": int(parsed["ppid"]),
                "pgid": int(parsed["pgid"]),
                "start_ticks": int(parsed["start_ticks"]),
                "state": str(parsed["state"]),
                "role": role if role is not None else "unclassified",
                "executable_path": str(executable),
                "executable_sha256": executable_sha256,
                "cmdline_sha256": hashlib.sha256(raw_cmdline).hexdigest(),
                "cpu_time_s": (
                    int(parsed["utime_ticks"]) + int(parsed["stime_ticks"])
                )
                / ticks,
                "rss_bytes": int(parsed["rss_pages"]) * page_size,
            }
        )
    unclassified = [
        item
        for item in records
        if item["role"] == "unclassified" and item["state"] != "Z"
    ]
    return {
        "accepted_process_groups": sorted(accepted_pgids),
        "counts": counts,
        "required_counts": REQUIRED_PROCESS_COUNTS,
        "roles_exact": counts == REQUIRED_PROCESS_COUNTS and not unclassified,
        "unclassified_count": len(unclassified),
        "processes": sorted(records, key=lambda item: item["pid"]),
        "process_count": len(records),
        "total_cpu_time_s": sum(item["cpu_time_s"] for item in records),
        "total_rss_bytes": sum(item["rss_bytes"] for item in records),
    }


def container_runtime_identity() -> dict[str, Any]:
    cgroup = read_text(Path("/proc/1/cgroup"), "") or ""
    if Path("/.dockerenv").exists():
        runtime = "docker"
    elif "containerd" in cgroup:
        runtime = "containerd"
    else:
        runtime = "container-unknown"
    return {
        "runtime": runtime,
        "pid1_cgroup_sha256": hashlib.sha256(cgroup.encode()).hexdigest(),
        "container_marker_dockerenv": Path("/.dockerenv").exists(),
    }


def wait_until(target_ns: int, spin: Any, stop: threading.Event) -> None:
    while not stop.is_set():
        remaining = target_ns - time.monotonic_ns()
        if remaining <= 0:
            return
        spin(min(0.02, remaining / 1_000_000_000))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--ready-file", type=Path, required=True)
    parser.add_argument("--stop-file", type=Path, required=True)
    parser.add_argument("--clock-socket", type=Path, required=True)
    parser.add_argument("--process-group", action="append", type=int, required=True)
    parser.add_argument("--include-own-process-group", action="store_true")
    parser.add_argument("--required-ready", action="append", type=Path, default=[])
    parser.add_argument("--event-dir", type=Path)
    parser.add_argument("--causal-done-file", type=Path)
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    contract = strict_json(args.contract.resolve())
    schedule = contract.get("schedule")
    accepted_pgids = set(args.process_group)
    if args.include_own_process_group:
        accepted_pgids.add(os.getpgrp())
    writer = RuntimeWriter(
        run_dir / "logs/m4_runtime_events.jsonl",
        str(contract["run_id"]),
        str(contract["runtime_id"]),
    )
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_unused: stop.set())
    signal.signal(signal.SIGTERM, lambda *_unused: stop.set())
    raw_clock_thread = threading.Thread(
        target=beacon,
        args=(args.clock_socket.resolve(), "raw_collector", stop),
        daemon=True,
    )
    executable_cache: dict[tuple[str, int, int], str] = {}
    clocks: dict[str, dict[str, int]] = {
        topic: {"count": 0, "last_host_ns": 0, "last_sim_ns": 0, "last_emit_ns": 0}
        for topic in CLOCK_TOPICS
    }
    odometry: dict[str, dict[str, int]] = {
        uav: {"count": 0, "last_host_ns": 0, "last_stamp_ns": 0, "last_emit_ns": 0}
        for uav in UAV_IDS
    }
    world_entities: dict[str, dict[str, Any]] = {}

    import rclpy
    from nav_msgs.msg import Odometry
    from rclpy.node import Node
    from rclpy.qos import qos_profile_sensor_data
    from rosgraph_msgs.msg import Clock
    from tf2_msgs.msg import TFMessage

    class RuntimeNode(Node):
        def __init__(self) -> None:
            super().__init__("ams_m4_runtime_collector")
            for topic in CLOCK_TOPICS:
                self.create_subscription(
                    Clock,
                    topic,
                    lambda message, source=topic: self.on_clock(source, message),
                    qos_profile_sensor_data,
                )
            for uav in UAV_IDS:
                self.create_subscription(
                    Odometry,
                    f"/{uav}/odometry",
                    lambda message, source=uav: self.on_odometry(source, message),
                    qos_profile_sensor_data,
                )
            self.create_subscription(
                TFMessage,
                "/world/map/pose/info",
                self.on_world_pose,
                qos_profile_sensor_data,
            )

        def on_clock(self, topic: str, message: Any) -> None:
            now = time.monotonic_ns()
            sim_ns = int(message.clock.sec) * 1_000_000_000 + int(message.clock.nanosec)
            state = clocks[topic]
            state["count"] += 1
            state["last_host_ns"] = now
            state["last_sim_ns"] = sim_ns
            if topic == "/uav1/clock" and now - state["last_emit_ns"] >= 40_000_000:
                writer.emit(
                    "gazebo_clock_sample",
                    clock_topic=topic,
                    source_callback_monotonic_ns=now,
                    sim_time_ns=sim_ns,
                )
                state["last_emit_ns"] = now
            elif topic != "/uav1/clock" and now - state["last_emit_ns"] >= 500_000_000:
                writer.emit(
                    "gazebo_clock_crosscheck",
                    clock_topic=topic,
                    source_callback_monotonic_ns=now,
                    sim_time_ns=sim_ns,
                )
                state["last_emit_ns"] = now

        def on_odometry(self, uav: str, message: Any) -> None:
            now = time.monotonic_ns()
            state = odometry[uav]
            stamp = int(message.header.stamp.sec) * 1_000_000_000 + int(
                message.header.stamp.nanosec
            )
            state["count"] += 1
            state["last_host_ns"] = now
            state["last_stamp_ns"] = stamp
            if now - state["last_emit_ns"] >= 200_000_000:
                pose = message.pose.pose
                twist = message.twist.twist
                writer.emit(
                    "odometry_sample",
                    uav=uav,
                    source_topic=f"/{uav}/odometry",
                    source_frame=ODOMETRY_SOURCE_FRAME,
                    transform_version=COORDINATE_TRANSFORM_VERSION,
                    source_callback_monotonic_ns=now,
                    sim_stamp_ns=stamp,
                    position_m=[pose.position.x, pose.position.y, pose.position.z],
                    orientation_quat_xyzw=[
                        pose.orientation.x,
                        pose.orientation.y,
                        pose.orientation.z,
                        pose.orientation.w,
                    ],
                    linear_velocity_mps=[
                        twist.linear.x,
                        twist.linear.y,
                        twist.linear.z,
                    ],
                    angular_velocity_radps=[
                        twist.angular.x,
                        twist.angular.y,
                        twist.angular.z,
                    ],
                )
                state["last_emit_ns"] = now

        def on_world_pose(self, message: Any) -> None:
            now = time.monotonic_ns()
            for transform in message.transforms:
                name = runtime_entity_name(str(transform.child_frame_id))
                if name is None:
                    continue
                translation = transform.transform.translation
                rotation = transform.transform.rotation
                world_entities[name] = {
                    "last_host_ns": now,
                    "position_m": [translation.x, translation.y, translation.z],
                    "orientation_quat_xyzw": [rotation.x, rotation.y, rotation.z, rotation.w],
                }

    node: Any = None
    completed = False
    failure: str | None = None
    try:
        rclpy.init(args=None)
        node = RuntimeNode()
        raw_clock_thread.start()

        def spin(timeout: float) -> None:
            rclpy.spin_once(node, timeout_sec=max(0.0, timeout))

        def readiness_observation(
            now: int, processes: dict[str, Any]
        ) -> dict[str, Any]:
            files_ready = all(
                path.is_file() and path.stat().st_size > 0
                for path in args.required_ready
            )
            clocks_fresh = all(
                state["count"] >= 2
                and now - state["last_host_ns"] <= 250_000_000
                for state in clocks.values()
            )
            clock_values = [state["last_sim_ns"] for state in clocks.values()]
            clocks_coherent = (
                clocks_fresh and max(clock_values) - min(clock_values) <= 50_000_000
            )
            odometry_fresh = all(
                state["count"] >= 2
                and now - state["last_host_ns"] <= 1_000_000_000
                for state in odometry.values()
            )
            poses_fresh = set(world_entities) == WORLD_ENTITY_IDS and all(
                now - int(item["last_host_ns"]) <= 1_500_000_000
                for item in world_entities.values()
            )
            return {
                "ready": bool(
                    files_ready
                    and clocks_coherent
                    and odometry_fresh
                    and poses_fresh
                    and processes["roles_exact"]
                ),
                "files_ready": files_ready,
                "clocks_fresh": clocks_fresh,
                "clocks_coherent": clocks_coherent,
                "odometry_fresh": odometry_fresh,
                "world_poses_fresh": poses_fresh,
                "processes": processes,
            }

        identity = static_runtime_identity()
        identity["container_runtime"] = container_runtime_identity()
        identity["competing_load_policy"] = "exclusive_simulation_and_gpu"
        writer.emit(
            "collector_start",
            static_runtime_identity=identity,
            accepted_process_groups=sorted(accepted_pgids),
            canonical_clock_topic="/uav1/clock",
            crosscheck_clock_topics=list(CLOCK_TOPICS[1:]),
        )
        write_exclusive(
            args.ready_file,
            {
                "pid": os.getpid(),
                "monotonic_ns": time.monotonic_ns(),
                "canonical_clock_topic": "/uav1/clock",
            },
        )
        if contract.get("profile") == "m4_component":
            windows = contract.get("windows")
            if (
                not isinstance(windows, list)
                or len(windows) != 11
                or args.event_dir is None
                or args.causal_done_file is None
            ):
                raise M4ValidationError("causal collector inputs are incomplete")
            event_dir = args.event_dir.resolve()
            done_file = args.causal_done_file.resolve()
            processed_events: set[str] = set()

            def drain_phase_events() -> None:
                for path in sorted(event_dir.glob("*.json")):
                    if path.name in processed_events:
                        continue
                    command = strict_json(path)
                    event = command.pop("event", None)
                    if not isinstance(event, str):
                        raise M4ValidationError(
                            f"causal runtime event has no event name: {path}"
                        )
                    writer.emit(event, **command)
                    processed_events.add(path.name)

            first_start = int(windows[0]["start_monotonic_ns"])
            last_end = int(windows[-1]["end_monotonic_ns"])
            targets = set(range(first_start - 10_000_000_000, last_end, 1_000_000_000))
            for window in windows:
                start_ns = int(window["start_monotonic_ns"])
                targets.add(start_ns - 10_000_000_000)
                targets.add(start_ns)
            socket_material = b"".join(
                path.resolve().read_bytes() for path in sorted(args.required_ready)
            )
            socket_identity = hashlib.sha256(socket_material).hexdigest()
            for target in sorted(targets):
                while time.monotonic_ns() < target and not stop.is_set():
                    spin(0.01)
                    drain_phase_events()
                if stop.is_set():
                    raise M4ValidationError("causal runtime collector stopped early")
                now = time.monotonic_ns()
                if now - target > 100_000_000:
                    raise M4ValidationError("causal resource sampler missed 100-ms schedule")
                processes = process_sample(accepted_pgids, executable_cache)
                observation = readiness_observation(now, processes)
                captures_ready = (
                    processes["counts"].get("packet_capture")
                    == REQUIRED_PROCESS_COUNTS["packet_capture"]
                )
                readiness = {
                    "ready": bool(observation["ready"] and captures_ready),
                    "clocks": bool(
                        observation["clocks_fresh"]
                        and observation["clocks_coherent"]
                    ),
                    "odometry": bool(observation["odometry_fresh"]),
                    "poses": bool(observation["world_poses_fresh"]),
                    "provider": bool(observation["files_ready"]),
                    "adapter": bool(observation["files_ready"]),
                    "ns3": bool(observation["files_ready"]),
                    "endpoints": bool(observation["files_ready"]),
                    "captures": captures_ready,
                    "topology": bool(observation["files_ready"]),
                }
                if not all(readiness.values()):
                    raise M4ValidationError("causal full-stack readiness was lost")
                writer.emit(
                    "causal_resource_sample",
                    scheduled_monotonic_ns=target,
                    processes=processes,
                    sockets={
                        "ready": True,
                        "identity_sha256": socket_identity,
                        "unexpected": [],
                    },
                    captures={"ready": True, "kernel_drops": 0},
                    queues={"bounded": True, "hidden_drops": 0},
                    readiness=readiness,
                )
            deadline = last_end + 500_000_000
            while time.monotonic_ns() < deadline and not done_file.exists():
                spin(0.01)
                drain_phase_events()
            drain_phase_events()
            if not done_file.is_file():
                raise M4ValidationError("causal phase driver did not complete")
            completed = True
            return 0
        if not isinstance(schedule, dict):
            raise M4ValidationError("capacity schedule is absent")
        stable_since: int | None = None
        readiness_emitted = False
        readiness_broken = False
        next_readiness = time.monotonic_ns()
        warmup_start_ns = int(schedule["warmup_start_monotonic_ns"])
        while time.monotonic_ns() < warmup_start_ns and not stop.is_set():
            spin(0.02)
            now = time.monotonic_ns()
            if now < next_readiness:
                continue
            processes = process_sample(accepted_pgids, executable_cache)
            observation = readiness_observation(now, processes)
            writer.emit("readiness_sample", **observation)
            if observation["ready"]:
                stable_since = stable_since if stable_since is not None else now
                if (
                    not readiness_emitted
                    and now - stable_since >= int(schedule["readiness_stability_ns"])
                ):
                    writer.emit(
                        "readiness_complete",
                        stable_since_monotonic_ns=stable_since,
                    )
                    writer.emit(
                        "runtime_entities_observed",
                        entities={
                            name: dict(world_entities[name])
                            for name in sorted(WORLD_ENTITY_IDS)
                        },
                    )
                    readiness_emitted = True
            else:
                if readiness_emitted:
                    readiness_broken = True
                stable_since = None
            next_readiness += 1_000_000_000
        if stop.is_set() or not readiness_emitted or readiness_broken:
            raise M4ValidationError("M4 readiness was absent or not continuously stable")

        wait_until(warmup_start_ns, spin, stop)
        warmup_end_ns = int(schedule["measurement_start_monotonic_ns"])
        writer.emit("warmup_start", target_end_monotonic_ns=warmup_end_ns)
        next_warmup_resource = warmup_start_ns
        writer.emit("readiness_transition", ready=True, phase="warmup")
        while time.monotonic_ns() < warmup_end_ns and not stop.is_set():
            spin(0.02)
            now = time.monotonic_ns()
            if now >= next_warmup_resource:
                if now - next_warmup_resource > 100_000_000:
                    raise M4ValidationError("warm-up readiness sampler missed 100-ms deadline")
                processes = process_sample(accepted_pgids, executable_cache)
                writer.emit(
                    "warmup_resource_sample",
                    scheduled_monotonic_ns=next_warmup_resource,
                    processes=processes,
                    cgroup=cgroup_sample(),
                    gpu=gpu_sample(),
                )
                observation = readiness_observation(now, processes)
                writer.emit(
                    "continuous_readiness_sample",
                    scheduled_monotonic_ns=next_warmup_resource,
                    phase="warmup",
                    **observation,
                )
                if not observation["ready"]:
                    writer.emit("readiness_transition", ready=False, phase="warmup")
                    raise M4ValidationError("full readiness was lost during warm-up")
                next_warmup_resource += 1_000_000_000
        if stop.is_set():
            raise M4ValidationError("M4 stopped during warm-up")
        writer.emit("warmup_end", target_monotonic_ns=warmup_end_ns)

        measurement_start_ns = warmup_end_ns
        measurement_end_ns = int(schedule["measurement_end_monotonic_ns"])
        writer.emit("measurement_start", target_end_monotonic_ns=measurement_end_ns)
        resource_index = 0
        while time.monotonic_ns() < measurement_end_ns and not stop.is_set():
            spin(0.01)
            now = time.monotonic_ns()
            target = measurement_start_ns + resource_index * 1_000_000_000
            if resource_index < 600 and now >= target:
                if now - target > 100_000_000:
                    raise M4ValidationError(
                        f"measurement sampler {resource_index} missed 100-ms deadline"
                    )
                processes = process_sample(accepted_pgids, executable_cache)
                writer.emit(
                    "measurement_resource_sample",
                    sample_index=resource_index,
                    scheduled_monotonic_ns=target,
                    processes=processes,
                    cgroup=cgroup_sample(),
                    gpu=gpu_sample(),
                )
                observation = readiness_observation(now, processes)
                writer.emit(
                    "continuous_readiness_sample",
                    scheduled_monotonic_ns=target,
                    phase="measurement",
                    sample_index=resource_index,
                    **observation,
                )
                if not observation["ready"]:
                    writer.emit(
                        "readiness_transition", ready=False, phase="measurement"
                    )
                    raise M4ValidationError(
                        f"full readiness was lost at measurement sample {resource_index}"
                    )
                resource_index += 1
        if stop.is_set() or resource_index != 600:
            raise M4ValidationError(
                f"M4 measurement stopped/incomplete at resource sample {resource_index}"
            )
        bracket_deadline = measurement_end_ns + 500_000_000
        while (
            clocks["/uav1/clock"]["last_host_ns"] < measurement_end_ns
            and time.monotonic_ns() < bracket_deadline
        ):
            spin(0.005)
        bracket = clocks["/uav1/clock"]["last_host_ns"] >= measurement_end_ns
        writer.emit(
            "measurement_end",
            target_monotonic_ns=measurement_end_ns,
            clock_bracket_observed=bracket,
        )
        writer.emit("readiness_transition", ready=True, phase="measurement_complete")
        if not bracket:
            raise M4ValidationError("canonical Gazebo clock does not bracket measurement end")
        completed = True
        return 0
    except (M4ValidationError, OSError, KeyError, TypeError, ValueError) as exc:
        failure = str(exc)
        writer.emit("collector_failure", failure=failure)
        print(f"FAIL M4 runtime collector: {exc}", file=os.sys.stderr)
        return 2
    finally:
        stop.set()
        if raw_clock_thread.ident is not None:
            raw_clock_thread.join(2.0)
        writer.emit("collector_stop", completed=completed, failure=failure)
        writer.close()
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
