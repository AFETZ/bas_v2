#!/usr/bin/env python3
"""Collect structured five-UAV Gazebo/SITL/heartbeat/odometry health evidence.

This is an M1 component-only probe. Direct MAVProxy telemetry on UDP 14550 is
allowed only for base-runtime health and is explicitly ineligible as packet-path
or no-bypass proof.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
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
    "bind failed",
    "failed to bind",
    "address already in use",
    "segmentation fault",
    "core dumped",
    "process has died",
    "error while starting ipvx agent",
    "failed to open (",
    "traceback (most recent call last)",
    "failed to download /srtm",
    "failed to download /srtm3",
)
OBSERVATION_WINDOW_PATTERNS = (
    "link 1 down",
    "no link",
)
M1_PROFILE = "m1_component"
M1_PHASES = ("readiness", "measurement", "finalization")
REQUIRED_PROCESS_COUNTS = {
    "arducopter": 5,
    "mavproxy": 5,
    "micro_ros_agent": 5,
    "gazebo_server": 1,
}
MAVPROXY_OFFLINE_DEFAULT_MODULES = (
    "log,signing,wp,rally,fence,ftp,param,relay,tuneopt,arm,mode,calibration,"
    "rc,auxopt,misc,cmdlong,battery,output,adsb,layout"
)
PROCESS_NAMESPACES = ("cgroup", "ipc", "mnt", "net", "pid", "user", "uts")
CAPABILITY_STATUS_FIELDS = {
    "inheritable": "CapInh",
    "permitted": "CapPrm",
    "effective": "CapEff",
    "bounding": "CapBnd",
    "ambient": "CapAmb",
}
ALLOWED_PROCESS_STATES = {"R", "S", "D", "I"}
READINESS_HEARTBEAT_MAX_AGE_S = 3.0
READINESS_POSITION_MAX_AGE_S = 3.0
READINESS_PROCESS_SAMPLE_INTERVAL_S = 1.0
M1_PROCESS_SAMPLE_MAX_GAP_S = 1.5
_EXECUTABLE_HASH_CACHE: dict[tuple[int, int, int, int], str] = {}


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_from_epoch_seconds(value: float) -> str:
    """Preserve sub-second wall-clock evidence in canonical UTC form."""

    return (
        datetime.fromtimestamp(value, timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


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


def _parse_proc_stat(text: str) -> dict[str, Any]:
    """Parse identity fields without assuming that the comm field has no spaces."""
    closing = text.rfind(")")
    opening = text.find("(")
    if opening < 1 or closing <= opening:
        raise ValueError("malformed /proc stat record")
    try:
        pid = int(text[:opening].strip())
        fields = text[closing + 1 :].strip().split()
        return {
            "pid": pid,
            "comm": text[opening + 1 : closing],
            "state": fields[0],
            "ppid": int(fields[1]),
            "pgid": int(fields[2]),
            "session_id": int(fields[3]),
            "start_ticks": int(fields[19]),
        }
    except (IndexError, ValueError) as exc:
        raise ValueError(f"malformed /proc stat fields: {exc}") from exc


def _cached_executable_sha256(path: Path) -> str:
    item_stat = path.stat()
    cache_key = (
        int(item_stat.st_dev),
        int(item_stat.st_ino),
        int(item_stat.st_size),
        int(item_stat.st_mtime_ns),
    )
    cached = _EXECUTABLE_HASH_CACHE.get(cache_key)
    if cached is None:
        cached = sha256_file(path)
        _EXECUTABLE_HASH_CACHE[cache_key] = cached
    return cached


def _decode_cmdline(raw: bytes) -> list[str]:
    if not raw:
        return []
    return [part.decode("utf-8", errors="replace") for part in raw.rstrip(b"\0").split(b"\0")]


def classify_process_role(process: dict[str, Any]) -> str | None:
    """Classify required roles only when the actual executable and argv agree."""
    cmdline = process.get("cmdline") if isinstance(process.get("cmdline"), list) else []
    argv = [value for value in cmdline if isinstance(value, str)]
    exe_name = Path(str(process.get("exe_path") or "")).name.lower()
    argv_names = [Path(value).name.lower() for value in argv]
    argv0 = argv_names[0] if argv_names else ""
    if exe_name == "arducopter" and argv0 == "arducopter":
        return "arducopter"

    python_executable = re.fullmatch(r"python(?:3(?:\.[0-9]+)?)?", exe_name) is not None
    mavproxy_script = (
        argv0 == "mavproxy.py"
        or (
            re.fullmatch(r"python(?:3(?:\.[0-9]+)?)?", argv0) is not None
            and len(argv_names) >= 2
            and argv_names[1] == "mavproxy.py"
        )
    )
    if python_executable and mavproxy_script:
        return "mavproxy"
    if exe_name == "micro_ros_agent" and argv0 == "micro_ros_agent":
        return "micro_ros_agent"

    launcher_is_server = exe_name.startswith("ruby")
    has_gz_sim = any(
        argv_names[index] == "gz" and argv[index + 1].lower() == "sim"
        for index in range(max(0, len(argv) - 1))
    )
    try:
        process_title = shlex.split(argv[0]) if len(argv) == 1 else []
    except ValueError:
        process_title = []
    has_gz_sim_title = (
        len(process_title) >= 2
        and Path(process_title[0]).name.lower() == "gz"
        and process_title[1].lower() == "sim"
    )
    if launcher_is_server and ((has_gz_sim and "-s" in argv) or (has_gz_sim_title and "-s" in process_title)):
        return "gazebo_server"
    return None


def _mavproxy_invoked_path(argv: list[str]) -> str | None:
    names = [Path(value).name.lower() for value in argv]
    if names and names[0] == "mavproxy.py":
        return argv[0]
    if (
        len(names) >= 2
        and re.fullmatch(r"python(?:3(?:\.[0-9]+)?)?", names[0]) is not None
        and names[1] == "mavproxy.py"
    ):
        return argv[1]
    return None


def _read_process_identity(
    pid: int,
    process_group: int,
    ps_stat: str,
    ps_arguments: str,
) -> dict[str, Any]:
    proc = Path("/proc") / str(pid)
    errors: list[str] = []
    try:
        first_stat = _parse_proc_stat((proc / "stat").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        first_stat = {
            "pid": pid,
            "comm": "",
            "state": ps_stat[:1],
            "ppid": None,
            "pgid": process_group,
            "session_id": None,
            "start_ticks": None,
        }
        errors.append(f"initial stat: {exc}")

    try:
        raw_cmdline = (proc / "cmdline").read_bytes()
    except OSError as exc:
        raw_cmdline = b""
        errors.append(f"cmdline: {exc}")
    cmdline = _decode_cmdline(raw_cmdline)
    arguments = ps_arguments or shlex.join(cmdline)

    try:
        exe_path = os.readlink(proc / "exe")
        exe_sha256 = _cached_executable_sha256(proc / "exe")
    except OSError as exc:
        exe_path = None
        exe_sha256 = None
        errors.append(f"executable: {exc}")

    namespaces: dict[str, str | None] = {}
    for name in PROCESS_NAMESPACES:
        try:
            namespaces[name] = os.readlink(proc / "ns" / name)
        except OSError as exc:
            namespaces[name] = None
            errors.append(f"namespace {name}: {exc}")

    status_values: dict[str, str] = {}
    try:
        for line in (proc / "status").read_text(encoding="utf-8").splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                status_values[key] = value.strip()
    except (OSError, UnicodeError) as exc:
        errors.append(f"status: {exc}")
    capabilities = {
        name: status_values.get(status_key, "").lower()
        for name, status_key in CAPABILITY_STATUS_FIELDS.items()
    }
    uid_text = status_values.get("Uid", "").split()
    uid = int(uid_text[0]) if uid_text and uid_text[0].isdigit() else None
    if uid is None:
        errors.append("status: real UID is unavailable")
    for name, value in capabilities.items():
        if re.fullmatch(r"[0-9a-f]{16}", value) is None:
            errors.append(f"capability {name} is unavailable")

    try:
        second_stat = _parse_proc_stat((proc / "stat").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        errors.append(f"final stat: {exc}")
        second_stat = None
    if second_stat is not None and any(
        first_stat.get(key) != second_stat.get(key)
        for key in ("pid", "ppid", "pgid", "session_id", "start_ticks")
    ):
        errors.append("process identity changed while the sample was collected")

    process = {
        "pid": first_stat.get("pid"),
        "ppid": first_stat.get("ppid"),
        "pgid": first_stat.get("pgid"),
        "session_id": first_stat.get("session_id"),
        "state": first_stat.get("state"),
        "stat": ps_stat,
        "start_ticks": first_stat.get("start_ticks"),
        "command": first_stat.get("comm") or "",
        "arguments": arguments,
        "cmdline": cmdline,
        "cmdline_b64": base64.b64encode(raw_cmdline).decode("ascii"),
        "cmdline_sha256": hashlib.sha256(raw_cmdline).hexdigest(),
        "exe_path": exe_path,
        "exe_sha256": exe_sha256,
        "uid": uid,
        "namespaces": namespaces,
        "capabilities": capabilities,
        "invoked_files": {},
        "identity_errors": errors,
    }
    process["role"] = classify_process_role(process)
    if process["role"] == "mavproxy":
        invoked_path = _mavproxy_invoked_path(cmdline)
        if not isinstance(invoked_path, str) or not Path(invoked_path).is_absolute():
            errors.append("MAVProxy invoked script path is unavailable or non-absolute")
        else:
            try:
                process["invoked_files"][invoked_path] = _cached_executable_sha256(
                    Path(invoked_path)
                )
            except OSError as exc:
                errors.append(f"MAVProxy invoked script: {exc}")
    return process


def process_identity_digest(process: dict[str, Any]) -> str:
    payload = {
        key: process.get(key)
        for key in (
            "pid",
            "ppid",
            "pgid",
            "session_id",
            "start_ticks",
            "cmdline_sha256",
            "exe_path",
            "exe_sha256",
            "uid",
            "namespaces",
            "capabilities",
            "invoked_files",
        )
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def identity_set_sha256(identity_digests: Any) -> str:
    canonical = json.dumps(sorted(identity_digests), separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _argv_option(argv: list[str], name: str) -> str | None:
    values: list[str] = []
    for index, value in enumerate(argv):
        if value == name and index + 1 < len(argv):
            values.append(argv[index + 1])
        if value.startswith(f"{name}="):
            values.append(value.split("=", 1)[1])
    if len(values) > 1:
        raise ValueError(f"duplicate option {name}")
    return values[0] if values else None


def derive_runtime_endpoints(
    processes: list[dict[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    """Derive the live five-UAV endpoint matrix from exact process argv."""
    failures: list[str] = []
    endpoints: dict[str, list[dict[str, Any]]] = {
        role: [] for role in REQUIRED_PROCESS_COUNTS
    }
    for process in processes:
        if not isinstance(process, dict):
            continue
        role = classify_process_role(process)
        argv = process.get("cmdline") if isinstance(process.get("cmdline"), list) else []
        argv = [value for value in argv if isinstance(value, str)]
        pid = process.get("pid")
        try:
            if role == "arducopter":
                instance = int(str(_argv_option(argv, "--instance")))
                endpoints[role].append(
                    {
                        "pid": pid,
                        "instance": instance,
                        "system_id": int(str(_argv_option(argv, "--sysid"))),
                        "sim_address": _argv_option(argv, "--sim-address"),
                        "fdm_port_in": 9002 + 10 * instance,
                    }
                )
            elif role == "mavproxy":
                endpoints[role].append(
                    {
                        "pid": pid,
                        "master": _argv_option(argv, "--master"),
                        "sitl": _argv_option(argv, "--sitl"),
                        "out": _argv_option(argv, "--out"),
                        "default_modules": _argv_option(argv, "--default-modules"),
                    }
                )
            elif role == "micro_ros_agent":
                namespace = next(
                    (
                        value.split(":=", 1)[1]
                        for value in argv
                        if value.startswith("__ns:=")
                    ),
                    None,
                )
                endpoints[role].append(
                    {
                        "pid": pid,
                        "transport": argv[1] if len(argv) > 1 else None,
                        "port": int(str(_argv_option(argv, "--port"))),
                        "namespace": namespace,
                    }
                )
            elif role == "gazebo_server":
                endpoints[role].append({"pid": pid})
        except (TypeError, ValueError):
            failures.append(f"process {pid} has malformed {role} endpoint arguments")

    for role in endpoints:
        endpoints[role].sort(
            key=lambda item: (
                int(item.get("instance", item.get("port", item.get("pid", -1))) or -1),
                int(item.get("pid") or -1),
            )
        )
    arducopter = endpoints["arducopter"]
    if [item.get("instance") for item in arducopter] != list(range(5)):
        failures.append("ArduCopter instances are not exactly 0..4")
    if [item.get("system_id") for item in arducopter] != list(range(1, 6)):
        failures.append("ArduCopter system IDs are not exactly 1..5")
    if {item.get("sim_address") for item in arducopter} != {"127.0.0.1"}:
        failures.append("ArduCopter simulation address is not the declared loopback")

    mavproxy = endpoints["mavproxy"]
    expected_master = {f"tcp:127.0.0.1:{5760 + 10 * index}" for index in range(5)}
    expected_sitl = {f"127.0.0.1:{5501 + 10 * index}" for index in range(5)}
    if {item.get("master") for item in mavproxy} != expected_master:
        failures.append("MAVProxy master endpoints are not the five unique SITL TCP endpoints")
    if {item.get("sitl") for item in mavproxy} != expected_sitl:
        failures.append("MAVProxy SITL endpoints are not the five unique simulator endpoints")
    if {(item.get("master"), item.get("sitl")) for item in mavproxy} != {
        (
            f"tcp:127.0.0.1:{5760 + 10 * index}",
            f"127.0.0.1:{5501 + 10 * index}",
        )
        for index in range(5)
    }:
        failures.append("MAVProxy master/SITL endpoint pairing does not match instances")
    if {item.get("out") for item in mavproxy} != {"127.0.0.1:14550"}:
        failures.append("MAVProxy M1 health output is not the declared aggregator endpoint")
    if {item.get("default_modules") for item in mavproxy} != {
        MAVPROXY_OFFLINE_DEFAULT_MODULES
    }:
        failures.append("MAVProxy is not using the declared offline module set")

    agents = endpoints["micro_ros_agent"]
    if [item.get("port") for item in agents] != list(range(2019, 2024)):
        failures.append("micro-ROS DDS ports are not exactly 2019..2023")
    if [item.get("namespace") for item in agents] != [f"/uav{index}" for index in range(1, 6)]:
        failures.append("micro-ROS namespaces are not exactly /uav1../uav5")
    if {item.get("transport") for item in agents} != {"udp4"}:
        failures.append("micro-ROS agents are not using the declared UDP transport")
    return endpoints, failures


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
    counts = {name: 0 for name in REQUIRED_PROCESS_COUNTS}
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
        stat, _command, arguments = fields[2:]
        process = _read_process_identity(pid, process_group, stat, arguments)
        processes.append(process)
        role = process.get("role")
        if role in counts:
            counts[role] += 1
    processes.sort(key=lambda process: int(process.get("pid") or -1))
    return counts, processes, None


def readiness_status(
    expected_names: list[str],
    odometry: dict[str, dict[str, Any]],
    heartbeats: dict[int, dict[str, Any]],
    mavlink_positions: dict[int, dict[str, Any]],
    *,
    now_monotonic_s: float | None = None,
    odometry_max_age_s: float = 1.0,
    heartbeat_max_age_s: float = READINESS_HEARTBEAT_MAX_AGE_S,
    position_max_age_s: float = READINESS_POSITION_MAX_AGE_S,
) -> tuple[bool, dict[str, Any]]:
    """Require current, finite odometry, heartbeat, and valid geodetic pose."""
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
    def ages(records: dict[Any, dict[str, Any]], keys: list[Any], field: str) -> dict[str, float | None]:
        result: dict[str, float | None] = {}
        for key in keys:
            timestamp = (records.get(key) or {}).get(field)
            result[str(key)] = (
                None
                if now_monotonic_s is None
                or not finite_number(timestamp)
                else max(0.0, now_monotonic_s - float(timestamp))
            )
        return result

    odometry_ages = ages(odometry, expected_names, "last_monotonic_s")
    heartbeat_ages = ages(heartbeats, list(range(1, 6)), "last_monotonic_s")
    valid_position_ages = ages(
        mavlink_positions, list(range(1, 6)), "last_valid_home_monotonic_s"
    )
    details = {
        "odometry_counts": odometry_counts,
        "heartbeat_counts": heartbeat_counts,
        "mavlink_position_counts": mavlink_position_counts,
        "mavlink_valid_home_position_counts": valid_home_position_counts,
        "odometry_age_s": odometry_ages,
        "heartbeat_age_s": heartbeat_ages,
        "mavlink_valid_home_position_age_s": valid_position_ages,
        "freshness_limits_s": {
            "odometry": odometry_max_age_s,
            "heartbeat": heartbeat_max_age_s,
            "valid_home_position": position_max_age_s,
        },
    }
    ready = (
        all(count >= 2 for count in odometry_counts.values())
        and all(count >= 1 for count in heartbeat_counts.values())
        and all(count >= 2 for count in mavlink_position_counts.values())
        and all(count >= 2 for count in valid_home_position_counts.values())
        and (
            now_monotonic_s is None
            or (
                all(finite_number(age) and float(age) <= odometry_max_age_s for age in odometry_ages.values())
                and all(finite_number(age) and float(age) <= heartbeat_max_age_s for age in heartbeat_ages.values())
                and all(finite_number(age) and float(age) <= position_max_age_s for age in valid_position_ages.values())
            )
        )
    )
    return ready, details


def selected_measurement_duration(ready: bool, requested_duration_s: float) -> float:
    """Avoid a long observation after readiness has already failed."""
    return requested_duration_s if ready else 0.0


def process_window_metrics(
    samples: list[dict[str, Any]], observed_duration_s: float
) -> dict[str, float | bool | None]:
    """Derive producer-side measurement coverage under the M1 continuity bound."""

    offsets = [
        float(sample["offset_s"])
        for sample in samples
        if finite_number(sample.get("offset_s"))
    ]
    maximum_gap_s = max(
        (current - previous for previous, current in zip(offsets, offsets[1:])),
        default=0.0,
    )
    first_delay_s = offsets[0] if offsets else None
    last_age_s = observed_duration_s - offsets[-1] if offsets else None
    covered = (
        len(offsets) == len(samples)
        and first_delay_s is not None
        and 0.0 <= first_delay_s <= M1_PROCESS_SAMPLE_MAX_GAP_S
        and last_age_s is not None
        and 0.0 <= last_age_s <= M1_PROCESS_SAMPLE_MAX_GAP_S
        and maximum_gap_s <= M1_PROCESS_SAMPLE_MAX_GAP_S
    )
    return {
        "first_sample_delay_s": first_delay_s,
        "last_sample_age_s": last_age_s,
        "maximum_sample_gap_s": maximum_gap_s,
        "covered": covered,
    }


def run_process_monitor(
    process_group: int,
    started_mono: float,
    stop_event: threading.Event,
    samples: list[dict[str, Any]],
    event_log: Any,
    *,
    interval_s: float = 1.0,
    measurement_closed: threading.Event | None = None,
    measurement_lock: Any | None = None,
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
        if measurement_lock is None:
            if measurement_closed is not None and measurement_closed.is_set():
                break
            sample = {
                "offset_s": sampled_mono - started_mono,
                "counts": counts,
                "processes": processes,
                "error": process_error,
            }
            samples.append(sample)
            event_log.emit("process_sample", phase="measurement", **sample)
        else:
            # The end-of-measurement boundary holds this lock while it seals
            # the stream.  A process sample is therefore either completely
            # inside the observation window or omitted from both raw evidence
            # and its summary.
            with measurement_lock:
                if measurement_closed is not None and measurement_closed.is_set():
                    break
                sample = {
                    "offset_s": sampled_mono - started_mono,
                    "counts": counts,
                    "processes": processes,
                    "error": process_error,
                }
                samples.append(sample)
                event_log.emit("process_sample", phase="measurement", **sample)
        next_sample += interval_s
        stop_event.wait(max(0.0, next_sample - time.monotonic()))


def summarize_process_samples(
    samples: list[dict[str, Any]],
    *,
    expected_process_group: int | None,
) -> tuple[dict[str, dict[str, int]], dict[str, str], str | None, list[str]]:
    """Derive exact counts and continuity from the raw process identities."""
    failures: list[str] = []
    counts_by_role: dict[str, list[int]] = {
        role: [] for role in REQUIRED_PROCESS_COUNTS
    }
    baseline_by_role: dict[str, set[str]] | None = None
    baseline_all: set[str] | None = None

    for sample_index, sample in enumerate(samples):
        processes = sample.get("processes")
        if not isinstance(processes, list) or not processes:
            failures.append(f"process sample {sample_index} has no process identities")
            for role in counts_by_role:
                counts_by_role[role].append(0)
            continue
        pids = [process.get("pid") for process in processes if isinstance(process, dict)]
        if len(pids) != len(processes) or len(set(pids)) != len(pids):
            failures.append(f"process sample {sample_index} contains duplicate/invalid PIDs")

        role_digests: dict[str, set[str]] = {
            role: set() for role in REQUIRED_PROCESS_COUNTS
        }
        all_digests: set[str] = set()
        derived_counts = {role: 0 for role in REQUIRED_PROCESS_COUNTS}
        for process_index, process in enumerate(processes):
            context = f"process sample {sample_index} identity {process_index}"
            if not isinstance(process, dict):
                failures.append(f"{context} is not an object")
                continue
            role = classify_process_role(process)
            if process.get("role") != role:
                failures.append(f"{context} producer role does not match executable identity")
            if role in derived_counts:
                derived_counts[role] += 1
            state = process.get("state")
            if state not in ALLOWED_PROCESS_STATES:
                failures.append(f"{context} has unhealthy state {state!r}")
            if process.get("identity_errors") != []:
                failures.append(f"{context} has incomplete identity: {process.get('identity_errors')!r}")
            for key, minimum in (
                ("pid", 1),
                ("ppid", 0),
                ("pgid", 1),
                ("session_id", 1),
                ("start_ticks", 1),
                ("uid", 0),
            ):
                value = process.get(key)
                if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                    failures.append(f"{context}.{key} is invalid")
            if expected_process_group is not None and process.get("pgid") != expected_process_group:
                failures.append(f"{context}.pgid left the supervised launch group")
            for key in ("cmdline_sha256", "exe_sha256"):
                if re.fullmatch(r"[0-9a-f]{64}", str(process.get(key) or "")) is None:
                    failures.append(f"{context}.{key} is not SHA-256")
            if not isinstance(process.get("exe_path"), str) or not str(
                process.get("exe_path")
            ).startswith("/"):
                failures.append(f"{context}.exe_path is not absolute")
            try:
                cmdline_raw = base64.b64decode(
                    str(process.get("cmdline_b64") or ""), validate=True
                )
            except (ValueError, TypeError):
                failures.append(f"{context}.cmdline_b64 is invalid")
                cmdline_raw = b""
            if hashlib.sha256(cmdline_raw).hexdigest() != process.get("cmdline_sha256"):
                failures.append(f"{context}.cmdline hash does not match raw bytes")
            if _decode_cmdline(cmdline_raw) != process.get("cmdline"):
                failures.append(f"{context}.cmdline does not match raw bytes")
            namespaces = process.get("namespaces")
            if not isinstance(namespaces, dict) or set(namespaces) != set(PROCESS_NAMESPACES):
                failures.append(f"{context}.namespaces is incomplete")
            else:
                for name in PROCESS_NAMESPACES:
                    if re.fullmatch(rf"{name}:\[[0-9]+\]", str(namespaces.get(name) or "")) is None:
                        failures.append(f"{context}.namespaces.{name} is invalid")
            capabilities = process.get("capabilities")
            if not isinstance(capabilities, dict) or set(capabilities) != set(
                CAPABILITY_STATUS_FIELDS
            ):
                failures.append(f"{context}.capabilities is incomplete")
            elif any(
                re.fullmatch(r"[0-9a-f]{16}", str(value or "")) is None
                for value in capabilities.values()
            ):
                failures.append(f"{context}.capabilities is invalid")
            identity_digest = process_identity_digest(process)
            all_digests.add(identity_digest)
            if role in role_digests:
                role_digests[role].add(identity_digest)

        producer_counts = sample.get("counts")
        if producer_counts != derived_counts:
            failures.append(
                f"process sample {sample_index} producer counts differ from raw identities"
            )
        for role, required in REQUIRED_PROCESS_COUNTS.items():
            observed = derived_counts[role]
            counts_by_role[role].append(observed)
            if observed != required:
                failures.append(
                    f"process sample {sample_index} has {observed} {role}, expected exactly {required}"
                )
        if baseline_by_role is None:
            baseline_by_role = role_digests
            baseline_all = all_digests
        else:
            for role in REQUIRED_PROCESS_COUNTS:
                if role_digests[role] != baseline_by_role[role]:
                    failures.append(f"process sample {sample_index} changed {role} identity set")
            if all_digests != baseline_all:
                failures.append(f"process sample {sample_index} changed launch process identity set")

    count_ranges = {
        role: {
            "minimum": min(values, default=0),
            "maximum": max(values, default=0),
        }
        for role, values in counts_by_role.items()
    }
    role_hashes = {
        role: identity_set_sha256((baseline_by_role or {}).get(role, set()))
        for role in REQUIRED_PROCESS_COUNTS
    }
    all_hash = identity_set_sha256(baseline_all) if baseline_all is not None else None
    return count_ranges, role_hashes, all_hash, failures


class EventLog:
    def __init__(
        self,
        path: Path,
        *,
        run_id: str,
        runtime_id: str,
        source_hash: str,
        contract_sha256: str,
        provenance_sha256: str,
        scenario_id: str,
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = path.open("x", encoding="utf-8")
        self._lock = threading.Lock()
        self._run_id = run_id
        self._runtime_id = runtime_id
        self._source_hash = source_hash
        self._contract_sha256 = contract_sha256
        self._provenance_sha256 = provenance_sha256
        self._scenario_id = scenario_id
        self._phase = "readiness"
        self._event_seq = 0

    def set_phase(self, phase: str) -> None:
        if phase not in M1_PHASES:
            raise ValueError(f"unknown M1 phase: {phase}")
        with self._lock:
            self._phase = phase

    def emit(self, event: str, *, phase: str | None = None, **fields: Any) -> None:
        with self._lock:
            selected_phase = self._phase if phase is None else phase
            if selected_phase not in M1_PHASES:
                raise ValueError(f"unknown M1 event phase: {selected_phase}")
            if M1_PHASES.index(selected_phase) < M1_PHASES.index(self._phase):
                # A telemetry callback may have sampled the old phase just
                # before a locked lifecycle transition.  Discard that stale
                # callback instead of writing a phase-regressing raw event.
                return
            self._event_seq += 1
            wall_time_ns = time.time_ns()
            monotonic_ns = time.monotonic_ns()
            record = {
                **fields,
                "schema_version": 2,
                "run_id": self._run_id,
                "runtime_id": self._runtime_id,
                "source_hash": self._source_hash,
                "provenance_sha256": self._provenance_sha256,
                "contract": M1_CONTRACT_ID,
                "plan_version": 3,
                "contract_sha256": self._contract_sha256,
                "profile": M1_PROFILE,
                "scenario_id": self._scenario_id,
                "phase": selected_phase,
                "event_seq": self._event_seq,
                "event": event,
                "wall_utc": datetime.fromtimestamp(
                    wall_time_ns / 1_000_000_000, timezone.utc
                ).isoformat().replace("+00:00", "Z"),
                "wall_time_ns": wall_time_ns,
                "monotonic_ns": monotonic_ns,
            }
            self._handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
            self._handle.flush()

    def close(self) -> None:
        with self._lock:
            self._handle.close()


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_robot_description(
    name: str, raw_description: bytes
) -> tuple[dict[str, Any] | None, str | None]:
    """Extract the single live ArduPilot FDM endpoint from exact parameter bytes."""
    try:
        root = ET.fromstring(raw_description.decode("utf-8"))
    except (UnicodeDecodeError, ET.ParseError) as exc:
        return None, f"{name} robot_description is invalid XML/UTF-8: {exc}"
    plugins = [
        element
        for element in root.iter()
        if _xml_local_name(element.tag) == "plugin"
        and (
            str(element.attrib.get("name", "")) == "ArduPilotPlugin"
            or str(element.attrib.get("filename", "")) == "ArduPilotPlugin"
        )
    ]
    if len(plugins) != 1:
        return None, f"{name} robot_description has {len(plugins)} ArduPilotPlugin elements"
    children = {
        _xml_local_name(child.tag): (child.text or "").strip() for child in plugins[0]
    }
    try:
        fdm_addr = children["fdm_addr"]
        fdm_port_in = int(children["fdm_port_in"])
    except (KeyError, ValueError) as exc:
        return None, f"{name} ArduPilotPlugin FDM endpoint is invalid: {exc}"
    return {
        "name": name,
        "namespace": f"/{name}",
        "fdm_addr": fdm_addr,
        "fdm_port_in": fdm_port_in,
        "robot_description_b64": base64.b64encode(raw_description).decode("ascii"),
        "robot_description_sha256": hashlib.sha256(raw_description).hexdigest(),
    }, None


def probe_robot_descriptions(
    node: Any,
    clients: dict[str, Any],
    expected_names: list[str],
    rclpy_module: Any,
    request_type: Any,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Read exact live robot_description parameters through ROS services."""
    robots: list[dict[str, Any]] = []
    failures: list[str] = []
    for name in expected_names:
        client = clients[name]
        if not client.wait_for_service(timeout_sec=0.25):
            failures.append(f"/{name}/robot_state_publisher parameter service is unavailable")
            continue
        request = request_type.Request()
        request.names = ["robot_description"]
        future = client.call_async(request)
        rclpy_module.spin_until_future_complete(node, future, timeout_sec=1.0)
        response = future.result() if future.done() else None
        values = getattr(response, "values", None)
        if not isinstance(values, (list, tuple)) or len(values) != 1:
            failures.append(f"{name} robot_description parameter response is missing")
            continue
        description = getattr(values[0], "string_value", None)
        if not isinstance(description, str) or not description:
            failures.append(f"{name} robot_description parameter is not a nonempty string")
            continue
        record, error = parse_robot_description(name, description.encode("utf-8"))
        if error or record is None:
            failures.append(error or f"{name} robot_description could not be parsed")
        else:
            robots.append(record)
    return robots, failures


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
        result = subprocess.run(command, capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        event_log.emit("gazebo_scene_probe_failed", error=str(exc))
        return set(), str(exc)
    stdout = bytes(result.stdout)
    stderr = bytes(result.stderr)
    text = stdout.decode(errors="replace") + "\n" + stderr.decode(errors="replace")
    names = set(re.findall(r'\bname:\s*"([^"]+)"', text))
    event_log.emit(
        "gazebo_scene_probe",
        exit_code=result.returncode,
        command=command,
        world_name=world,
        model_names=sorted(name for name in names if name.startswith("uav")),
        stdout_b64=base64.b64encode(stdout).decode("ascii"),
        stdout_sha256=hashlib.sha256(stdout).hexdigest(),
        stderr_b64=base64.b64encode(stderr).decode("ascii"),
        stderr_sha256=hashlib.sha256(stderr).hexdigest(),
    )
    if result.returncode != 0:
        return names, f"gz scene/info exited {result.returncode}"
    return names, None


def launch_log_findings(
    path: Path, observation_offset: int = 0, observation_end_offset: int | None = None
) -> list[str]:
    if not path.is_file():
        return [f"launch log is missing: {path}"]
    raw = path.read_bytes()
    safe_end = len(raw) if observation_end_offset is None else min(max(observation_end_offset, 0), len(raw))
    safe_offset = min(max(observation_offset, 0), safe_end)
    full_text = raw[:safe_end].decode(errors="replace").lower()
    observation_text = raw[safe_offset:safe_end].decode(errors="replace").lower()
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
    parser.add_argument("--readiness-timeout-s", type=float, default=90.0)
    parser.add_argument("--readiness-stability-s", type=float, default=5.0)
    parser.add_argument("--heartbeat-endpoint", default="udpin:0.0.0.0:14550")
    parser.add_argument("--minimum-heartbeat-hz", type=float, default=0.8)
    parser.add_argument("--maximum-wall-heartbeat-gap-s", type=float, default=15.0)
    parser.add_argument("--minimum-odometry-hz", type=float, default=5.0)
    parser.add_argument("--maximum-freshness-age-s", type=float, default=1.0)
    parser.add_argument("--world", default="map")
    parser.add_argument("--launch-log", type=Path)
    parser.add_argument("--runtime-id", default=os.environ.get("AMS_RUNTIME_ID"))
    parser.add_argument("--launch-process-group", type=int)
    args = parser.parse_args(argv)

    run_dir = args.run_dir.resolve()
    if not math.isfinite(args.readiness_timeout_s) or not 1.0 <= args.readiness_timeout_s <= 120.0:
        print("FAIL --readiness-timeout-s must be within [1, 120] seconds", file=sys.stderr)
        return 2
    if not math.isfinite(args.readiness_stability_s) or not 1.0 <= args.readiness_stability_s <= 30.0:
        print("FAIL --readiness-stability-s must be within [1, 30] seconds", file=sys.stderr)
        return 2
    provenance_path = run_dir / "metrics/provenance.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    provenance_sha256 = sha256_file(provenance_path)
    if not args.runtime_id or len(args.runtime_id) < 8:
        print("FAIL --runtime-id is required", file=sys.stderr)
        return 2
    config_hashes = provenance.get("config_hashes") if isinstance(provenance.get("config_hashes"), dict) else {}
    contract_sha256 = config_hashes.get(M1_PLAN_PATH)
    if not isinstance(contract_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", contract_sha256) is None:
        print("FAIL provenance does not bind the v3 M1 contract hash", file=sys.stderr)
        return 2
    scenario = yaml.safe_load(args.scenario.read_text()) or {}
    scenario_metadata = scenario.get("scenario") if isinstance(scenario.get("scenario"), dict) else {}
    scenario_id = scenario_metadata.get("name")
    if scenario_id != "scenario_5uav":
        print(f"FAIL scenario identity must be 'scenario_5uav', got {scenario_id!r}", file=sys.stderr)
        return 2
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
        provenance_sha256=provenance_sha256,
        scenario_id=scenario_id,
    )
    dependency_versions = (
        provenance.get("dependency_versions")
        if isinstance(provenance.get("dependency_versions"), dict)
        else {}
    )
    runtime_capabilities = (
        dependency_versions.get("runtime_capabilities")
        if isinstance(dependency_versions.get("runtime_capabilities"), dict)
        else {}
    )
    container_image = (
        provenance.get("container_image")
        if isinstance(provenance.get("container_image"), dict)
        else {}
    )
    event_log.emit(
        "health_probe_start",
        component_only=True,
        packet_path_eligible=False,
        expected_uavs=expected_names,
        duration_s=args.duration_s,
        readiness_timeout_s=args.readiness_timeout_s,
        readiness_stability_s=args.readiness_stability_s,
        readiness_freshness_limits_s={
            "odometry": args.maximum_freshness_age_s,
            "heartbeat": READINESS_HEARTBEAT_MAX_AGE_S,
            "valid_home_position": READINESS_POSITION_MAX_AGE_S,
        },
        run_id=run_dir.name,
        runtime_id=args.runtime_id,
        source_hash=provenance.get("source_hash"),
        runtime_identity={
            "git_commit": provenance.get("git_commit"),
            "container_id": container_image.get("runtime_container_id"),
            "container_image_digest": container_image.get("digest"),
            "host": runtime_capabilities.get("host"),
            "kernel": {
                "system": runtime_capabilities.get("system"),
                "machine": runtime_capabilities.get("machine"),
                "release": runtime_capabilities.get("kernel_release"),
                "version": runtime_capabilities.get("kernel_version"),
            },
            "gpu": runtime_capabilities.get("gpu"),
        },
    )

    try:
        import rclpy
        from nav_msgs.msg import Odometry
        from rcl_interfaces.srv import GetParameters
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
            "last_monotonic_s": None,
            "last_valid_home_monotonic_s": None,
        }
    )
    stop_event = threading.Event()
    measurement_active = threading.Event()
    measurement_closed = threading.Event()
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
                if measurement_closed.is_set():
                    return
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
                # Keep evidence emission in the same critical section as the
                # boundary decision.  Otherwise a callback accepted just
                # before the boundary could receive a raw-event timestamp
                # after it.
                event_log.emit(
                    "odometry" if sample_is_active else "readiness_odometry",
                    phase="measurement" if sample_is_active else "readiness",
                    uav=name,
                    source_topic=topic,
                    sequence=sequence,
                    stamp_ns=stamp_ns,
                    valid=valid,
                    position_m=position_m,
                    linear_speed_mps=speed,
                    orientation_xyzw=[
                        float(orientation.x),
                        float(orientation.y),
                        float(orientation.z),
                        float(orientation.w),
                    ],
                    linear_velocity_mps=[float(linear.x), float(linear.y), float(linear.z)],
                    angular_velocity_rad_s=[
                        float(angular.x),
                        float(angular.y),
                        float(angular.z),
                    ],
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
                    now_mono = time.monotonic()
                    lat_deg = float(message.lat) / 10_000_000
                    lon_deg = float(message.lon) / 10_000_000
                    relative_alt_m = float(message.relative_alt) / 1000
                    north_m = (lat_deg - home_lat) * 111_320.0
                    east_m = (lon_deg - home_lon) * 111_320.0 * math.cos(math.radians(home_lat))
                    home_distance_m = math.hypot(north_m, east_m)
                    with data_lock:
                        if measurement_closed.is_set():
                            continue
                        position_record = mavlink_positions[system_id]
                        position_record["count"] += 1
                        position_record["last_relative_alt_m"] = relative_alt_m
                        position_record["last_lat_deg"] = lat_deg
                        position_record["last_lon_deg"] = lon_deg
                        position_record["last_monotonic_s"] = now_mono
                        if abs(lat_deg) >= 1.0 and abs(lon_deg) >= 1.0:
                            position_record["valid_home_position_count"] += 1
                            position_record["last_valid_home_monotonic_s"] = now_mono
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
                            phase="measurement" if sample_is_active else "readiness",
                            system_id=system_id,
                            component_id=int(message.get_srcComponent()),
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
                    if measurement_closed.is_set():
                        continue
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
                        event_log.emit(
                            "heartbeat_unstamped",
                            phase="measurement" if measurement_active.is_set() else "readiness",
                            system_id=system_id,
                        )
                        continue
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
                        phase="measurement" if sample_is_active else "readiness",
                        system_id=system_id,
                        component_id=int(message.get_srcComponent()),
                        mav_type=int(message.type),
                        autopilot=int(message.autopilot),
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
    robot_description_clients = {
        name: node.create_client(
            GetParameters, f"/{name}/robot_state_publisher/get_parameters"
        )
        for name in expected_names
    }
    heartbeat_thread = threading.Thread(target=heartbeat_worker, daemon=True)
    heartbeat_thread.start()
    launch_log = args.launch_log.resolve() if args.launch_log else run_dir / "logs/five_uav_launch.log"
    readiness_started_mono = time.monotonic()
    readiness_deadline = readiness_started_mono + args.readiness_timeout_s
    readiness_log_scan_offset = launch_log.stat().st_size if launch_log.is_file() else 0
    readiness_launch_log_end_offset = readiness_log_scan_offset
    ready = False
    readiness_details: dict[str, Any] = {}
    readiness_process_samples: list[dict[str, Any]] = []
    stability_process_samples: list[dict[str, Any]] = []
    stability_started_mono: float | None = None
    stability_completed_mono: float | None = None
    next_readiness_sample_mono = readiness_started_mono
    robot_description_records: list[dict[str, Any]] = []
    robot_description_errors: list[str] = ["live robot descriptions have not been read"]
    robot_description_emitted = False
    last_readiness_reasons: list[str] = []
    while time.monotonic() < readiness_deadline:
        rclpy.spin_once(node, timeout_sec=0.1)
        now_mono = time.monotonic()
        if now_mono < next_readiness_sample_mono:
            continue
        next_readiness_sample_mono = now_mono + READINESS_PROCESS_SAMPLE_INTERVAL_S
        with data_lock:
            streams_ready, readiness_details = readiness_status(
                expected_names,
                odometry,
                heartbeats,
                mavlink_positions,
                now_monotonic_s=now_mono,
                odometry_max_age_s=args.maximum_freshness_age_s,
            )
        counts, processes, process_error = (
            process_group_counts(args.launch_process_group)
            if args.launch_process_group
            else ({}, [], "launch process group was not supplied")
        )
        sample = {
            "offset_s": now_mono - readiness_started_mono,
            "counts": counts,
            "processes": processes,
            "error": process_error,
        }
        candidate_samples = stability_process_samples + [sample]
        _, _, _, process_failures = summarize_process_samples(
            candidate_samples, expected_process_group=args.launch_process_group
        )
        _, endpoint_failures = derive_runtime_endpoints(processes)
        process_failures.extend(endpoint_failures)
        process_healthy = process_error is None and not process_failures

        current_log_end = launch_log.stat().st_size if launch_log.is_file() else 0
        current_log_start = readiness_log_scan_offset
        appended_log = b""
        if launch_log.is_file() and current_log_end >= readiness_log_scan_offset:
            with launch_log.open("rb") as launch_handle:
                launch_handle.seek(readiness_log_scan_offset)
                appended_log = launch_handle.read(current_log_end - readiness_log_scan_offset)
        readiness_log_scan_offset = current_log_end
        readiness_launch_log_end_offset = current_log_end
        appended_text = appended_log.decode(errors="replace").lower()
        link_down_detected = any(
            pattern in appended_text for pattern in OBSERVATION_WINDOW_PATTERNS
        )
        fatal_detected = any(
            pattern in (
                launch_log.read_bytes()[:current_log_end].decode(errors="replace").lower()
                if launch_log.is_file()
                else ""
            )
            for pattern in FATAL_LAUNCH_PATTERNS
        )

        robot_probe_attempted = False
        if streams_ready and process_healthy and not link_down_detected and not fatal_detected and not robot_description_records:
            robot_probe_attempted = True
            robot_description_records, robot_description_errors = probe_robot_descriptions(
                node,
                robot_description_clients,
                expected_names,
                rclpy,
                GetParameters,
            )
            expected_fdm = [9002 + 10 * index for index in range(5)]
            if (
                [record.get("name") for record in robot_description_records] != expected_names
                or [record.get("fdm_addr") for record in robot_description_records]
                != ["127.0.0.1"] * 5
                or [record.get("fdm_port_in") for record in robot_description_records]
                != expected_fdm
            ):
                robot_description_errors.append(
                    "live robot descriptions do not expose the canonical five FDM endpoints"
                )
                robot_description_records = []
            elif not robot_description_emitted:
                event_log.emit("robot_description_probe", robots=robot_description_records)
                robot_description_emitted = True

        # Parameter RPCs can take measurable time.  Begin (or resume) the
        # stability dwell only from the next freshly sampled iteration.
        if robot_probe_attempted:
            continue

        reasons: list[str] = []
        if not streams_ready:
            reasons.append("streams are missing or stale")
        if not process_healthy:
            reasons.append("required process snapshot is not exact and healthy")
        if link_down_detected:
            reasons.append("new MAVProxy link-down/no-link marker")
        if fatal_detected:
            reasons.append("fatal launch marker")
        if not robot_description_records:
            reasons.append("live robot descriptions are unavailable or invalid")
        qualifying = not reasons
        if qualifying:
            if stability_started_mono is None:
                stability_started_mono = now_mono
                stability_process_samples = []
            stability_process_samples.append(sample)
        else:
            stability_started_mono = None
            stability_process_samples = []
        stability_elapsed_s = (
            0.0 if stability_started_mono is None else now_mono - stability_started_mono
        )
        sample.update(
            {
                "sampled_monotonic_ns": int(now_mono * 1_000_000_000),
                "streams_ready": streams_ready,
                "stream_details": readiness_details,
                "process_healthy": process_healthy,
                "process_failures": process_failures,
                "robot_descriptions_ready": bool(robot_description_records),
                "link_down_detected": link_down_detected,
                "fatal_launch_marker_detected": fatal_detected,
                "qualifying": qualifying,
                "reasons": reasons,
                "stability_elapsed_s": stability_elapsed_s,
                "required_stability_s": args.readiness_stability_s,
                "launch_log_scanned_end_offset": current_log_end,
                "launch_log_scanned_start_offset": current_log_start,
            }
        )
        readiness_process_samples.append(sample)
        event_log.emit("readiness_process_sample", **sample)
        last_readiness_reasons = reasons
        if qualifying and stability_elapsed_s >= args.readiness_stability_s:
            stability_completed_mono = now_mono
            ready = True
            break
    readiness_failure = (
        None
        if ready
        else (
            "five-UAV streams did not become ready within "
            f"{args.readiness_timeout_s:g} seconds: {', '.join(last_readiness_reasons)}"
        )
    )
    readiness_elapsed_s = time.monotonic() - readiness_started_mono
    event_log.emit(
        "readiness",
        ready=ready,
        error=readiness_failure,
        elapsed_s=readiness_elapsed_s,
        timeout_s=args.readiness_timeout_s,
        stability_s=args.readiness_stability_s,
        stability_started_monotonic_ns=(
            None
            if stability_started_mono is None
            else int(stability_started_mono * 1_000_000_000)
        ),
        stability_completed_monotonic_ns=(
            None
            if stability_completed_mono is None
            else int(stability_completed_mono * 1_000_000_000)
        ),
        qualifying_process_samples=len(stability_process_samples),
        readiness_process_samples=len(readiness_process_samples),
        robot_description_errors=robot_description_errors,
        launch_log_readiness_end_offset=readiness_launch_log_end_offset,
        **readiness_details,
    )
    measurement_launch_log_offset: int | None = (
        readiness_launch_log_end_offset if ready else None
    )
    interrupted = False
    process_samples: list[dict[str, Any]] = []
    process_thread: threading.Thread | None = None
    if ready:
        event_log.set_phase("measurement")
        event_log.emit(
            "clock_correlation",
            correlation_point="measurement_start",
            monotonic_clock="CLOCK_MONOTONIC",
            wall_clock="CLOCK_REALTIME",
        )
        started_wall = time.time()
        started_mono = time.monotonic()
        event_log.emit(
            "measurement_start",
            run_id=run_dir.name,
            runtime_id=args.runtime_id,
            source_hash=provenance.get("source_hash"),
            measurement_started_monotonic_ns=int(started_mono * 1_000_000_000),
            launch_log_observation_offset=measurement_launch_log_offset,
        )
    else:
        started_wall = time.time()
        started_mono = time.monotonic()
    with data_lock:
        odometry.clear()
        heartbeats.clear()
        mavlink_positions.clear()
        if ready:
            measurement_active.set()
    if ready and args.launch_process_group:
        process_thread = threading.Thread(
            target=run_process_monitor,
            args=(
                args.launch_process_group,
                started_mono,
                stop_event,
                process_samples,
                event_log,
            ),
            kwargs={
                "measurement_closed": measurement_closed,
                "measurement_lock": data_lock,
            },
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
        # Seal every raw measurement stream under the same lock used by their
        # callbacks.  This makes the declared endpoint an actual upper bound
        # for raw heartbeat, odometry, MAVLink, and process-sample timestamps.
        with data_lock:
            measurement_closed.set()
            measurement_active.clear()
            measurement_ended_mono = time.monotonic()
            measurement_ended_wall = time.time()
            if ready:
                event_log.emit(
                    "clock_correlation",
                    correlation_point="measurement_end",
                    monotonic_clock="CLOCK_MONOTONIC",
                    wall_clock="CLOCK_REALTIME",
                )
            stop_event.set()
        measurement_launch_log_end_offset = (
            launch_log.stat().st_size if launch_log.is_file() else 0
        )
        launch_log_prefix = (
            launch_log.read_bytes()[:measurement_launch_log_end_offset]
            if launch_log.is_file()
            else b""
        )
        launch_log_observation_sha256 = hashlib.sha256(launch_log_prefix).hexdigest()
        heartbeat_thread.join(timeout=2.0)
        if process_thread is not None:
            process_thread.join(timeout=6.0)
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()

    event_log.set_phase("finalization")

    if heartbeat_thread.is_alive():
        heartbeat_errors.append("heartbeat worker did not stop")
    if process_thread is not None and process_thread.is_alive():
        heartbeat_errors.append("process monitor did not stop")

    observed_duration = measurement_ended_mono - started_mono
    models, gazebo_probe_error = discover_gazebo_models(args.world, event_log)
    critical = launch_log_findings(
        launch_log,
        (
            measurement_launch_log_end_offset
            if measurement_launch_log_offset is None
            else measurement_launch_log_offset
        ),
        measurement_launch_log_end_offset,
    )
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
    if [item["system_id"] for item in results] != list(range(1, 6)):
        all_failures.append("system IDs are not exactly 1..5 in UAV order")
    if [item["dds_udp_port"] for item in results] != list(range(2019, 2024)):
        all_failures.append("DDS UDP ports are not exactly 2019..2023 in UAV order")
    if gazebo_probe_error:
        all_failures.append(gazebo_probe_error)
    if critical:
        all_failures.extend(f"launch log contains {item!r}" for item in critical)
    if heartbeat_errors:
        all_failures.extend(f"heartbeat worker: {item}" for item in heartbeat_errors)
    if not args.launch_process_group:
        all_failures.append("launch process group was not supplied")
    if not process_samples:
        all_failures.append("no process-health samples were recorded")
    if any(sample.get("error") for sample in process_samples):
        all_failures.append("process-health sampling reported errors")
    (
        process_count_ranges,
        stable_identity_hashes,
        all_process_identity_hash,
        process_identity_failures,
    ) = summarize_process_samples(
        process_samples,
        expected_process_group=args.launch_process_group,
    )
    all_failures.extend(process_identity_failures)
    runtime_endpoints, endpoint_failures = derive_runtime_endpoints(
        process_samples[0].get("processes", []) if process_samples else []
    )
    all_failures.extend(endpoint_failures)
    process_window = process_window_metrics(process_samples, observed_duration)
    process_start_delay_s = process_window["first_sample_delay_s"]
    process_end_age_s = process_window["last_sample_age_s"]
    process_max_gap_s = process_window["maximum_sample_gap_s"]
    if process_window["covered"] is not True:
        all_failures.append(
            "process-health samples exceed the "
            f"{M1_PROCESS_SAMPLE_MAX_GAP_S:g}-second measurement continuity bound"
        )

    summary = {
        "schema_version": 2,
        "contract": M1_CONTRACT_ID,
        "plan_version": 3,
        "contract_sha256": contract_sha256,
        "run_id": run_dir.name,
        "runtime_id": args.runtime_id,
        "source_hash": provenance.get("source_hash"),
        "provenance_sha256": provenance_sha256,
        "profile": M1_PROFILE,
        "scenario_id": scenario_id,
        "phases": list(M1_PHASES),
        "clock_domains": {
            "monotonic": "CLOCK_MONOTONIC",
            "wall": "CLOCK_REALTIME",
            "correlation_event": "clock_correlation",
        },
        "component_only": True,
        "packet_path_eligible": False,
        "started_utc": utc_from_epoch_seconds(started_wall),
        "ended_utc": utc_from_epoch_seconds(measurement_ended_wall),
        "observed_duration_s": round(observed_duration, 6),
        "minimum_duration_s": args.minimum_duration_s,
        "minimum_heartbeat_hz": args.minimum_heartbeat_hz,
        "minimum_odometry_hz": args.minimum_odometry_hz,
        "maximum_freshness_age_s": args.maximum_freshness_age_s,
        "readiness": {
            "ready": ready,
            "elapsed_s": round(readiness_elapsed_s, 6),
            "timeout_s": args.readiness_timeout_s,
            "stability_s": args.readiness_stability_s,
            "stability_started_monotonic_ns": (
                None
                if stability_started_mono is None
                else int(stability_started_mono * 1_000_000_000)
            ),
            "stability_completed_monotonic_ns": (
                None
                if stability_completed_mono is None
                else int(stability_completed_mono * 1_000_000_000)
            ),
            "qualifying_process_samples": len(stability_process_samples),
            "readiness_process_samples": len(readiness_process_samples),
            "robot_description_errors": robot_description_errors,
            "launch_log_readiness_end_offset": readiness_launch_log_end_offset,
            **readiness_details,
        },
        "robot_descriptions": [
            {
                key: record[key]
                for key in (
                    "name",
                    "namespace",
                    "fdm_addr",
                    "fdm_port_in",
                    "robot_description_sha256",
                )
            }
            for record in robot_description_records
        ],
        "uavs": results,
        "process_health": {
            "process_group": args.launch_process_group,
            "samples": len(process_samples),
            "required_exact_counts": REQUIRED_PROCESS_COUNTS,
            "observed_count_ranges": process_count_ranges,
            "stable_identity_set_sha256": stable_identity_hashes,
            "all_process_identity_set_sha256": all_process_identity_hash,
            "first_sample_delay_s": process_start_delay_s,
            "last_sample_age_s": process_end_age_s,
            "maximum_sample_gap_s": process_max_gap_s,
            "runtime_endpoints": runtime_endpoints,
        },
        "gazebo_model_names": sorted(name for name in models if name.startswith("uav")),
        "gazebo_world_name": args.world,
        "launch_log": str(launch_log.relative_to(run_dir)),
        "launch_log_observation_offset": measurement_launch_log_offset,
        "launch_log_observation_end_offset": measurement_launch_log_end_offset,
        "launch_log_observation_sha256": launch_log_observation_sha256,
        "errors": all_failures,
        "passed": not all_failures,
    }
    event_log.emit(
        "health_probe_complete",
        passed=summary["passed"],
        errors=all_failures,
        observed_duration_s=observed_duration,
        measurement_ended_monotonic_ns=int(measurement_ended_mono * 1_000_000_000),
        launch_log_observation_offset=measurement_launch_log_offset,
        launch_log_observation_end_offset=measurement_launch_log_end_offset,
        launch_log_observation_sha256=launch_log_observation_sha256,
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
