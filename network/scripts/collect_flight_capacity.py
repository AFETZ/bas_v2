#!/usr/bin/env python3
"""Collect write-once raw timing/resource evidence for the five-UAV capacity prerequisite."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[2]
PROFILE_PATH = ROOT_DIR / "network/config/flight_capacity_profile.json"
EVENT_CONTRACT = "ams.flight-capacity-raw-event/v1"
OBSERVATION_CONTRACT = "ams.flight-capacity-observation/v1"
SHA256 = re.compile(r"[0-9a-f]{64}")
REQUIRED_PROCESS_COUNTS = {
    "arducopter": 5,
    "mavproxy": 5,
    "micro_ros_agent": 5,
    "gazebo_server": 1,
}


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def strict_json(path: Path) -> dict[str, Any]:
    value = json.loads(
        path.read_text(encoding="utf-8"),
        parse_constant=lambda token: (_ for _ in ()).throw(
            ValueError(f"non-finite JSON token: {token}")
        ),
    )
    if not isinstance(value, dict):
        raise ValueError(f"JSON root is not an object: {path}")
    return value


def read_text(path: Path, default: str | None = None) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return default


def read_int(path: Path) -> int | None:
    value = read_text(path)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def parse_proc_stat(text: str) -> dict[str, Any]:
    opening = text.find("(")
    closing = text.rfind(")")
    if opening < 1 or closing <= opening:
        raise ValueError("invalid /proc stat")
    fields = text[closing + 1 :].strip().split()
    if len(fields) < 22:
        raise ValueError("short /proc stat")
    return {
        "pid": int(text[:opening].strip()),
        "comm": text[opening + 1 : closing],
        "state": fields[0],
        "ppid": int(fields[1]),
        "pgid": int(fields[2]),
        "utime_ticks": int(fields[11]),
        "stime_ticks": int(fields[12]),
        "start_ticks": int(fields[19]),
        "rss_pages": int(fields[21]),
    }


def classify_process(comm: str, cmdline: list[str]) -> str | None:
    names = [Path(value).name.lower() for value in cmdline]
    executable = names[0] if names else comm.lower()
    if executable == "arducopter" or comm.lower() == "arducopter":
        return "arducopter"
    if "mavproxy.py" in names:
        return "mavproxy"
    if executable == "micro_ros_agent" or comm.lower() == "micro_ros_agent":
        return "micro_ros_agent"
    joined = " ".join(cmdline).lower()
    if (comm.lower().startswith("ruby") or executable.startswith("ruby")) and "gz sim" in joined and " -s" in f" {joined}":
        return "gazebo_server"
    return None


def process_group_sample(process_group: int) -> dict[str, Any]:
    counts = {key: 0 for key in REQUIRED_PROCESS_COUNTS}
    records: list[dict[str, Any]] = []
    page_size = os.sysconf("SC_PAGE_SIZE")
    clock_ticks = os.sysconf("SC_CLK_TCK")
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            parsed = parse_proc_stat((proc / "stat").read_text(encoding="utf-8"))
            if parsed["pgid"] != process_group:
                continue
            raw_cmdline = (proc / "cmdline").read_bytes()
            cmdline = [
                item.decode("utf-8", errors="replace")
                for item in raw_cmdline.rstrip(b"\0").split(b"\0")
                if item
            ]
        except (OSError, UnicodeError, ValueError):
            continue
        role = classify_process(parsed["comm"], cmdline)
        if role is not None:
            counts[role] += 1
        records.append(
            {
                "pid": parsed["pid"],
                "ppid": parsed["ppid"],
                "pgid": parsed["pgid"],
                "start_ticks": parsed["start_ticks"],
                "state": parsed["state"],
                "role": role,
                "cpu_time_s": (parsed["utime_ticks"] + parsed["stime_ticks"])
                / clock_ticks,
                "rss_bytes": parsed["rss_pages"] * page_size,
                "cmdline_sha256": hashlib.sha256(raw_cmdline).hexdigest(),
            }
        )
    return {
        "process_group": process_group,
        "counts": counts,
        "required_counts": REQUIRED_PROCESS_COUNTS,
        "roles_exact": counts == REQUIRED_PROCESS_COUNTS,
        "processes": sorted(records, key=lambda item: item["pid"]),
        "process_count": len(records),
        "total_cpu_time_s": sum(item["cpu_time_s"] for item in records),
        "total_rss_bytes": sum(item["rss_bytes"] for item in records),
    }


def current_cgroup_paths() -> dict[str, str]:
    paths: dict[str, str] = {}
    text = read_text(Path("/proc/self/cgroup"), "") or ""
    for line in text.splitlines():
        hierarchy, controllers, path = line.split(":", 2)
        paths[controllers or "unified"] = path
        paths[f"hierarchy_{hierarchy}"] = path
    return dict(sorted(paths.items()))


def cgroup_sample() -> dict[str, Any]:
    root = Path("/sys/fs/cgroup")
    cpu_stat: dict[str, int] = {}
    for line in (read_text(root / "cpu.stat", "") or "").splitlines():
        key, _, value = line.partition(" ")
        if key and value.isdigit():
            cpu_stat[key] = int(value)
    return {
        "paths": current_cgroup_paths(),
        "cpu_max": read_text(root / "cpu.max"),
        "cpu_weight": read_text(root / "cpu.weight"),
        "cpuset_cpus_effective": read_text(root / "cpuset.cpus.effective"),
        "memory_current": read_int(root / "memory.current"),
        "memory_max": read_text(root / "memory.max"),
        "memory_swap_max": read_text(root / "memory.swap.max"),
        "pids_current": read_int(root / "pids.current"),
        "pids_max": read_text(root / "pids.max"),
        "cpu_stat": cpu_stat,
    }


def gpu_sample() -> dict[str, Any]:
    command = [
        "/usr/bin/nvidia-smi",
        "--query-gpu=uuid,name,driver_version,memory.total,memory.used,utilization.gpu,temperature.gpu",
        "--format=csv,noheader,nounits",
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "error": f"{type(exc).__name__}: {exc}"}
    rows: list[dict[str, Any]] = []
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            values = [item.strip() for item in line.split(",")]
            if len(values) != 7:
                continue
            try:
                rows.append(
                    {
                        "uuid": values[0],
                        "name": values[1],
                        "driver_version": values[2],
                        "memory_total_mib": int(values[3]),
                        "memory_used_mib": int(values[4]),
                        "utilization_percent": int(values[5]),
                        "temperature_c": int(values[6]),
                    }
                )
            except ValueError:
                continue
    return {
        "available": result.returncode == 0 and bool(rows),
        "exit_code": result.returncode,
        "stderr": result.stderr.strip()[:512],
        "gpus": rows,
    }


def static_runtime_identity() -> dict[str, Any]:
    governors: dict[str, str | None] = {}
    for path in sorted(Path("/sys/devices/system/cpu").glob("cpu[0-9]*/cpufreq/scaling_governor")):
        governors[path.parent.parent.name] = read_text(path)
    cpu_model = None
    for line in (read_text(Path("/proc/cpuinfo"), "") or "").splitlines():
        if line.lower().startswith("model name"):
            cpu_model = line.partition(":")[2].strip()
            break
    status: dict[str, str] = {}
    for line in (read_text(Path("/proc/self/status"), "") or "").splitlines():
        key, separator, value = line.partition(":")
        if separator and key in {"CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb", "NoNewPrivs"}:
            status[key] = value.strip()
    return {
        "cpu_model": cpu_model,
        "cpu_count": os.cpu_count(),
        "cpu_online": read_text(Path("/sys/devices/system/cpu/online")),
        "cpu_possible": read_text(Path("/sys/devices/system/cpu/possible")),
        "governors": governors,
        "meminfo_sha256": hashlib.sha256(
            (read_text(Path("/proc/meminfo"), "") or "").encode("utf-8")
        ).hexdigest(),
        "kernel": {
            "sysname": os.uname().sysname,
            "release": os.uname().release,
            "version": os.uname().version,
            "machine": os.uname().machine,
        },
        "clocksource": read_text(
            Path("/sys/devices/system/clocksource/clocksource0/current_clocksource")
        ),
        "available_clocksources": read_text(
            Path("/sys/devices/system/clocksource/clocksource0/available_clocksource")
        ),
        "capabilities": status,
        "cgroup": cgroup_sample(),
        "gpu": gpu_sample(),
        "mitsuba_variant": os.environ.get("SIONNA_MITSUBA_VARIANT"),
    }


class EventWriter:
    def __init__(self, path: Path, *, run_id: str, runtime_id: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.run_id = run_id
        self.runtime_id = runtime_id
        self.index = 0
        self.handle = path.open("xb")

    def emit(self, event: str, **fields: Any) -> None:
        record = {
            "schema_version": 1,
            "contract": EVENT_CONTRACT,
            "event_index": self.index,
            "event": event,
            "run_id": self.run_id,
            "runtime_id": self.runtime_id,
            "host_monotonic_ns": time.monotonic_ns(),
            "host_realtime_ns": time.time_ns(),
            **fields,
        }
        self.handle.write(canonical(record) + b"\n")
        self.index += 1

    def close(self) -> None:
        if not self.handle.closed:
            self.handle.flush()
            os.fsync(self.handle.fileno())
            self.handle.close()


def load_profile(path: Path) -> dict[str, Any]:
    profile = strict_json(path)
    expected = {
        "schema_version": 1,
        "contract": "ams.flight-capacity-profile/v1",
        "qualification_profile": "flight_capacity_prerequisite",
        "consumed_nodes": ["Q0", "Q1"],
        "warmup_s": 30,
        "measurement_s": 300,
        "window_s": 1,
        "window_count": 300,
        "window_success_minimum": 285,
        "rtf_min": 0.95,
        "rtf_max": 1.05,
        "readiness_stability_s": 10,
        "readiness_timeout_s": 120,
        "resource_period_s": 1,
        "resource_max_gap_s": 1.5,
        "clock_sample_max_gap_s": 0.25,
        "clock_topic": "/uav1/clock",
        "scenario_path": "network/config/scenario_5uav.yaml",
        "uav_names": [f"uav{index}" for index in range(1, 6)],
        "required_mitsuba_variant": "cuda_ad_mono_polarized",
        "competing_load_policy": "exclusive_simulation_and_gpu",
    }
    if profile != expected:
        raise ValueError("flight capacity profile is not the exact accepted schema")
    return profile


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--runtime-id", required=True)
    parser.add_argument("--launch-process-group", type=int, required=True)
    parser.add_argument("--profile", type=Path, default=PROFILE_PATH)
    args = parser.parse_args(argv)

    run_dir = args.run_dir.resolve()
    profile_path = args.profile.resolve()
    try:
        profile = load_profile(profile_path)
        provenance_path = run_dir / "metrics/provenance.json"
        provenance = strict_json(provenance_path)
        consumption = provenance.get("qualification_consumption")
        if (
            provenance.get("run_id") != run_dir.name
            or provenance.get("acceptance_eligible") is not True
            or not isinstance(consumption, dict)
            or consumption.get("profile") != profile["qualification_profile"]
            or consumption.get("consumed_nodes") != profile["consumed_nodes"]
        ):
            raise ValueError("capacity provenance/profile binding is not accepted")
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        print(f"FAIL capacity inputs: {exc}", file=sys.stderr)
        return 2

    events_path = run_dir / "logs/flight_capacity_events.jsonl"
    observation_path = run_dir / "metrics/flight_capacity_observation.json"
    writer = EventWriter(events_path, run_id=run_dir.name, runtime_id=args.runtime_id)
    completed = False
    failures: list[str] = []
    clock_samples = 0
    last_clock_host_ns: int | None = None
    last_clock_sim_ns: int | None = None
    odometry: dict[str, dict[str, int]] = {
        name: {"count": 0, "last_host_ns": 0, "last_stamp_ns": 0}
        for name in profile["uav_names"]
    }
    measurement_start_ns: int | None = None
    measurement_end_ns: int | None = None
    warmup_start_ns: int | None = None
    warmup_end_ns: int | None = None
    resource_count = 0

    try:
        import rclpy
        from nav_msgs.msg import Odometry
        from rclpy.node import Node
        from rclpy.qos import qos_profile_sensor_data
        from rosgraph_msgs.msg import Clock

        class CapacityNode(Node):
            def __init__(self) -> None:
                super().__init__("ams_flight_capacity_probe")
                self.create_subscription(
                    Clock, profile["clock_topic"], self.on_clock, qos_profile_sensor_data
                )
                for name in profile["uav_names"]:
                    self.create_subscription(
                        Odometry,
                        f"/{name}/odometry",
                        lambda message, uav=name: self.on_odometry(uav, message),
                        qos_profile_sensor_data,
                    )

            def on_clock(self, message: Any) -> None:
                nonlocal clock_samples, last_clock_host_ns, last_clock_sim_ns
                host_ns = time.monotonic_ns()
                sim_ns = int(message.clock.sec) * 1_000_000_000 + int(message.clock.nanosec)
                writer.emit("clock_sample", sim_time_ns=sim_ns)
                clock_samples += 1
                last_clock_host_ns = host_ns
                last_clock_sim_ns = sim_ns

            def on_odometry(self, uav: str, message: Any) -> None:
                host_ns = time.monotonic_ns()
                stamp_ns = int(message.header.stamp.sec) * 1_000_000_000 + int(
                    message.header.stamp.nanosec
                )
                record = odometry[uav]
                record["count"] += 1
                record["last_host_ns"] = host_ns
                record["last_stamp_ns"] = stamp_ns
                writer.emit(
                    "odometry_sample",
                    uav=uav,
                    sequence=record["count"],
                    sim_stamp_ns=stamp_ns,
                )

        rclpy.init(args=None)
        node = CapacityNode()
        identity = static_runtime_identity()
        writer.emit(
            "collector_start",
            profile_sha256=sha256_file(profile_path),
            provenance_sha256=sha256_file(provenance_path),
            static_runtime_identity=identity,
        )

        readiness_deadline = time.monotonic_ns() + int(
            profile["readiness_timeout_s"] * 1_000_000_000
        )
        stable_since_ns: int | None = None
        next_readiness_sample_ns = time.monotonic_ns()
        while time.monotonic_ns() < readiness_deadline:
            rclpy.spin_once(node, timeout_sec=0.05)
            now_ns = time.monotonic_ns()
            if now_ns < next_readiness_sample_ns:
                continue
            next_readiness_sample_ns = now_ns + 1_000_000_000
            process = process_group_sample(args.launch_process_group)
            clock_ready = (
                clock_samples >= 2
                and last_clock_host_ns is not None
                and now_ns - last_clock_host_ns <= 500_000_000
                and last_clock_sim_ns is not None
                and last_clock_sim_ns > 0
            )
            odometry_ready = all(
                item["count"] >= 2 and now_ns - item["last_host_ns"] <= 1_000_000_000
                for item in odometry.values()
            )
            ready = clock_ready and odometry_ready and process["roles_exact"]
            writer.emit(
                "readiness_sample",
                ready=ready,
                clock_ready=clock_ready,
                odometry_ready=odometry_ready,
                process_group=process,
            )
            if ready:
                stable_since_ns = stable_since_ns or now_ns
                if now_ns - stable_since_ns >= int(
                    profile["readiness_stability_s"] * 1_000_000_000
                ):
                    break
            else:
                stable_since_ns = None
        else:
            failures.append("full five-UAV readiness did not become stable before timeout")

        if not failures:
            writer.emit("readiness_complete", stable_since_monotonic_ns=stable_since_ns)
            warmup_start_ns = time.monotonic_ns()
            warmup_target_ns = warmup_start_ns + int(profile["warmup_s"] * 1_000_000_000)
            writer.emit(
                "warmup_start",
                target_duration_ns=int(profile["warmup_s"] * 1_000_000_000),
            )
            next_resource_ns = warmup_start_ns
            while time.monotonic_ns() < warmup_target_ns:
                rclpy.spin_once(node, timeout_sec=0.05)
                now_ns = time.monotonic_ns()
                if now_ns >= next_resource_ns:
                    writer.emit(
                        "warmup_resource_sample",
                        process_group=process_group_sample(args.launch_process_group),
                        cgroup=cgroup_sample(),
                        gpu=gpu_sample(),
                    )
                    next_resource_ns += 1_000_000_000
            warmup_end_ns = time.monotonic_ns()
            writer.emit("warmup_end", target_monotonic_ns=warmup_target_ns)

            measurement_start_ns = time.monotonic_ns()
            measurement_end_target_ns = measurement_start_ns + int(
                profile["measurement_s"] * 1_000_000_000
            )
            writer.emit(
                "measurement_start",
                target_end_monotonic_ns=measurement_end_target_ns,
            )
            next_resource_ns = measurement_start_ns
            while time.monotonic_ns() < measurement_end_target_ns:
                rclpy.spin_once(node, timeout_sec=0.02)
                now_ns = time.monotonic_ns()
                if now_ns >= next_resource_ns:
                    writer.emit(
                        "measurement_resource_sample",
                        sample_index=resource_count,
                        process_group=process_group_sample(args.launch_process_group),
                        cgroup=cgroup_sample(),
                        gpu=gpu_sample(),
                    )
                    resource_count += 1
                    next_resource_ns += 1_000_000_000
            measurement_end_ns = measurement_end_target_ns
            bracket_deadline = time.monotonic_ns() + 500_000_000
            while (
                (last_clock_host_ns is None or last_clock_host_ns < measurement_end_ns)
                and time.monotonic_ns() < bracket_deadline
            ):
                rclpy.spin_once(node, timeout_sec=0.01)
            writer.emit(
                "measurement_end",
                target_monotonic_ns=measurement_end_ns,
                clock_bracket_observed=(
                    last_clock_host_ns is not None
                    and last_clock_host_ns >= measurement_end_ns
                ),
            )
            completed = True
        node.destroy_node()
        rclpy.shutdown()
    except BaseException as exc:
        failures.append(f"collector exception: {type(exc).__name__}: {exc}")
        try:
            writer.emit("collector_exception", error=failures[-1])
        except BaseException:
            pass
    finally:
        writer.close()

    observation = {
        "schema_version": 1,
        "contract": OBSERVATION_CONTRACT,
        "run_id": run_dir.name,
        "runtime_id": args.runtime_id,
        "completed": completed,
        "failures": failures,
        "profile_path": profile_path.relative_to(ROOT_DIR).as_posix(),
        "profile_sha256": sha256_file(profile_path),
        "provenance_sha256": sha256_file(provenance_path),
        "event_log_path": "logs/flight_capacity_events.jsonl",
        "event_log_sha256": sha256_file(events_path),
        "event_count": writer.index,
        "clock_sample_count": clock_samples,
        "resource_sample_count": resource_count,
        "warmup_start_monotonic_ns": warmup_start_ns,
        "warmup_end_monotonic_ns": warmup_end_ns,
        "measurement_start_monotonic_ns": measurement_start_ns,
        "measurement_end_monotonic_ns": measurement_end_ns,
    }
    observation_path.parent.mkdir(parents=True, exist_ok=True)
    with observation_path.open("xb") as handle:
        handle.write(json.dumps(observation, indent=2, sort_keys=True).encode("utf-8") + b"\n")
        handle.flush()
        os.fsync(handle.fileno())
    if failures or not completed:
        print("FAIL flight capacity collection: " + "; ".join(failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
