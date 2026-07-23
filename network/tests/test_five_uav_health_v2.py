#!/usr/bin/env python3
"""Focused M1 health-evidence tests."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

import yaml


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from network.tests.collect_five_uav_health import (  # noqa: E402
    M1_PROCESS_SAMPLE_MAX_GAP_S as COLLECTOR_PROCESS_SAMPLE_MAX_GAP_S,
    derive_runtime_endpoints,
    identity_set_sha256,
    launch_log_findings,
    process_identity_digest,
    process_window_metrics,
    rate_hz,
    readiness_status,
    run_process_monitor,
    selected_measurement_duration,
    utc_from_epoch_seconds,
)
from network.validation.evidence import (  # noqa: E402
    M1_PROCESS_SAMPLE_MAX_GAP_S,
    five_uav_health_status,
)


M1_CONTRACT_ID = "ams.m1.health/v3"
M1_PROFILE = "m1_component"
M1_SCENARIO_ID = "scenario_5uav"
M1_PLAN = ROOT_DIR / "doc/network_radio_integration_plan_v3.md"
M1_CONTRACT_SHA256 = hashlib.sha256(M1_PLAN.read_bytes()).hexdigest()
M1_LOCK_PATH = ROOT_DIR / "network/config/dependency_lock.yaml"
M1_LOCK_SHA256 = hashlib.sha256(M1_LOCK_PATH.read_bytes()).hexdigest()
M1_RUNTIME_IDENTITY = yaml.safe_load(M1_LOCK_PATH.read_text(encoding="utf-8"))[
    "m1_runtime_identity"
]
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
NAMESPACE_NAMES = ("cgroup", "ipc", "mnt", "net", "pid", "user", "uts")
CAPABILITY_NAMES = ("inheritable", "permitted", "effective", "bounding", "ambient")


def process_identity(role: str, ordinal: int) -> dict:
    role_index = tuple(REQUIRED_PROCESS_COUNTS).index(role) + 1
    pid = 1_000 + role_index * 100 + ordinal
    if role == "arducopter":
        executable = M1_RUNTIME_IDENTITY["role_executable_path"][role]
        argv = [
            executable,
            "--instance",
            str(ordinal - 1),
            "--sysid",
            str(ordinal),
            "--sim-address=127.0.0.1",
            "--model",
            "JSON",
        ]
        command = "arducopter"
    elif role == "mavproxy":
        executable = M1_RUNTIME_IDENTITY["role_executable_path"][role]
        argv = [
            "/usr/bin/python3",
            M1_RUNTIME_IDENTITY["role_invoked_file_path"]["mavproxy"],
            "--master",
            f"tcp:127.0.0.1:{5750 + ordinal * 10}",
            "--sitl",
            f"127.0.0.1:{5501 + (ordinal - 1) * 10}",
            "--out",
            "127.0.0.1:14550",
            "--default-modules",
            MAVPROXY_OFFLINE_DEFAULT_MODULES,
        ]
        command = "mavproxy.py"
    elif role == "micro_ros_agent":
        executable = M1_RUNTIME_IDENTITY["role_executable_path"][role]
        argv = [
            executable,
            "udp4",
            "--port",
            str(2018 + ordinal),
            "--ros-args",
            "-r",
            f"__ns:=/uav{ordinal}",
        ]
        command = "micro_ros_agent"
    elif role == "gazebo_server":
        executable = M1_RUNTIME_IDENTITY["role_executable_path"][role]
        argv = [
            "gz",
            "sim",
            "-s",
            "-r",
            "/workspace/install/multiagent_simulation/worlds/model.sdf",
        ]
        command = "ruby"
    else:  # pragma: no cover - fixture programming error
        raise ValueError(role)
    cmdline = b"\0".join(item.encode() for item in argv) + b"\0"
    return {
        "pid": pid,
        "ppid": 900,
        "pgid": 900,
        "session_id": 900,
        "state": "S",
        "stat": "S",
        "start_ticks": 50_000 + pid,
        "command": command,
        "arguments": " ".join(argv),
        "cmdline": argv,
        "cmdline_b64": base64.b64encode(cmdline).decode("ascii"),
        "cmdline_sha256": hashlib.sha256(cmdline).hexdigest(),
        "exe_path": executable,
        "exe_sha256": M1_RUNTIME_IDENTITY["executable_sha256"][executable],
        "uid": 1000,
        "namespaces": {
            name: f"{name}:[4026532{role_index:02d}]" for name in NAMESPACE_NAMES
        },
        "capabilities": {name: "0000000000000000" for name in CAPABILITY_NAMES},
        "invoked_files": (
            {
                M1_RUNTIME_IDENTITY["role_invoked_file_path"]["mavproxy"]:
                M1_RUNTIME_IDENTITY["invoked_file_sha256"][
                    M1_RUNTIME_IDENTITY["role_invoked_file_path"]["mavproxy"]
                ]
            }
            if role == "mavproxy"
            else {}
        ),
        "identity_errors": [],
        "role": role,
    }


def stable_processes() -> list[dict]:
    return [
        process_identity(role, ordinal)
        for role, count in REQUIRED_PROCESS_COUNTS.items()
        for ordinal in range(1, count + 1)
    ]


def rewrite_raw_and_rehash(run_dir: Path, mutator) -> list[dict]:
    raw_path = run_dir / "logs/five_uav_health_events.jsonl"
    records = [json.loads(line) for line in raw_path.read_text().splitlines()]
    mutator(records)
    raw = "".join(json.dumps(record, sort_keys=True) + "\n" for record in records)
    raw_path.write_text(raw, encoding="utf-8")
    summary_path = run_dir / "metrics/five_uav_health.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["raw_event_sha256"] = hashlib.sha256(raw.encode()).hexdigest()
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    return records


def rebind_process_health_identity_summary(
    run_dir: Path, records: list[dict]
) -> None:
    """Rebind producer summaries after an adversarial but internally stable edit."""

    samples = [record for record in records if record.get("event") == "process_sample"]
    if not samples:  # pragma: no cover - fixture programming error
        raise AssertionError("fixture has no measurement process samples")
    processes = samples[0]["processes"]
    summary_path = run_dir / "metrics/five_uav_health.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    health = summary["process_health"]
    health["samples"] = len(samples)
    health["stable_identity_set_sha256"] = {
        role: identity_set_sha256(
            process_identity_digest(process)
            for process in processes
            if process.get("role") == role
        )
        for role in REQUIRED_PROCESS_COUNTS
    }
    health["all_process_identity_set_sha256"] = identity_set_sha256(
        process_identity_digest(process) for process in processes
    )
    timestamps = [sample["monotonic_ns"] for sample in samples]
    measurement_start = next(
        record for record in records if record.get("event") == "measurement_start"
    )["measurement_started_monotonic_ns"]
    measurement_end = next(
        record for record in records if record.get("event") == "health_probe_complete"
    )["measurement_ended_monotonic_ns"]
    health["first_sample_delay_s"] = (timestamps[0] - measurement_start) / 1_000_000_000
    health["last_sample_age_s"] = (measurement_end - timestamps[-1]) / 1_000_000_000
    health["maximum_sample_gap_s"] = max(
        (current - previous) / 1_000_000_000
        for previous, current in zip(timestamps, timestamps[1:])
    )
    summary_path.write_text(json.dumps(summary), encoding="utf-8")


def launch_helper_identity(pid: int, start_ticks: int) -> dict:
    helper = process_identity("arducopter", 1)
    executable = "/opt/ros/humble/lib/robot_state_publisher/robot_state_publisher"
    argv = [executable, "--ros-args", "-r", "__ns:=/uav1"]
    raw = b"\0".join(value.encode() for value in argv) + b"\0"
    helper.update(
        {
            "pid": pid,
            "start_ticks": start_ticks,
            "command": "robot_state_publisher",
            "arguments": " ".join(argv),
            "cmdline": argv,
            "cmdline_b64": base64.b64encode(raw).decode("ascii"),
            "cmdline_sha256": hashlib.sha256(raw).hexdigest(),
            "exe_path": executable,
            "exe_sha256": M1_RUNTIME_IDENTITY["executable_sha256"][executable],
            "invoked_files": {},
            "role": None,
        }
    )
    return helper


def rewrite_launch_and_rebind(run_dir: Path, text: str, observation_offset: int) -> None:
    raw_bytes = text.encode()
    end_offset = len(raw_bytes)
    prefix_hash = hashlib.sha256(raw_bytes).hexdigest()
    (run_dir / "logs/five_uav_launch.log").write_bytes(raw_bytes)
    raw_path = run_dir / "logs/five_uav_health_events.jsonl"
    records = [json.loads(line) for line in raw_path.read_text().splitlines()]
    for record in records:
        if record.get("event") == "measurement_start":
            record["launch_log_observation_offset"] = observation_offset
        elif record.get("event") == "readiness":
            record["launch_log_readiness_end_offset"] = observation_offset
        elif record.get("event") == "readiness_process_sample":
            record["launch_log_scanned_start_offset"] = observation_offset
            record["launch_log_scanned_end_offset"] = observation_offset
        elif record.get("event") == "health_probe_complete":
            record["launch_log_observation_offset"] = observation_offset
            record["launch_log_observation_end_offset"] = end_offset
            record["launch_log_observation_sha256"] = prefix_hash
    raw = "".join(json.dumps(record, sort_keys=True) + "\n" for record in records)
    raw_path.write_text(raw, encoding="utf-8")
    summary_path = run_dir / "metrics/five_uav_health.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["launch_log_observation_offset"] = observation_offset
    summary["launch_log_observation_end_offset"] = end_offset
    summary["launch_log_observation_sha256"] = prefix_hash
    summary["readiness"]["launch_log_readiness_end_offset"] = observation_offset
    summary["raw_event_sha256"] = hashlib.sha256(raw.encode()).hexdigest()
    summary_path.write_text(json.dumps(summary), encoding="utf-8")


def health_record(run_id: str, duration_s: float = 300.0) -> dict:
    return {
        "schema_version": 2,
        "run_id": run_id,
        "component_only": True,
        "packet_path_eligible": False,
        "observed_duration_s": duration_s,
        "minimum_duration_s": 300.0,
        "errors": [],
        "uavs": [
            {
                "name": f"uav{index}",
                "system_id": index,
                "dds_udp_port": 2018 + index,
                "gazebo_model": True,
                "sitl_healthy": True,
                "heartbeat": True,
                "heartbeat_rate_hz": 1.0,
                "heartbeat_age_s": 0.2,
                "odometry_fresh": True,
                "odometry_rate_hz": 20.0,
                "odometry_age_s": 0.05,
            }
            for index in range(1, 6)
        ],
    }


def write_complete_health_evidence(run_dir: Path) -> None:
    source_hash = "b" * 64
    runtime_id = "runtime-complete"
    start_ns = 10_000_000_000
    duration_ns = 300_000_000_000
    wall_epoch_ns = 1_800_000_000_000_000_000
    runtime_capabilities = {
        "system": "Linux",
        "machine": "x86_64",
        "kernel_release": "6.8.0-fixture",
        "kernel_version": "#1 fixture",
        "host": {
            "hostname": "fixture-host",
            "boot_id_sha256": "d" * 64,
            "machine_id_sha256": "e" * 64,
        },
        "gpu": {"available": False, "devices": []},
    }
    container_image = {
        "runtime_container_id": "f" * 64,
        "digest": M1_RUNTIME_IDENTITY["container_image_digest"],
    }
    provenance = {
        "source_hash": source_hash,
        "git_commit": "c" * 40,
        "qualification_consumption": {
            "profile": "m1_component",
            "consumed_nodes": ["Q0", "Q1"],
            "consumed_node_sha256": {"Q0": "8" * 64, "Q1": "9" * 64},
        },
        "config_hashes": {
            "doc/network_radio_integration_plan_v3.md": M1_CONTRACT_SHA256,
            "network/config/dependency_lock.yaml": M1_LOCK_SHA256,
        },
        "dependency_versions": {"runtime_capabilities": runtime_capabilities},
        "container_image": container_image,
    }
    provenance_raw = json.dumps(provenance, sort_keys=True) + "\n"
    provenance_sha256 = hashlib.sha256(provenance_raw.encode()).hexdigest()
    processes = stable_processes()
    events = [
        {
            "event": "health_probe_start",
            "monotonic_ns": start_ns - 7_000_000_000,
            "phase": "readiness",
            "run_id": run_dir.name,
            "runtime_id": runtime_id,
            "source_hash": source_hash,
            "component_only": True,
            "packet_path_eligible": False,
            "expected_uavs": [f"uav{index}" for index in range(1, 6)],
            "duration_s": 300.0,
            "readiness_timeout_s": 90.0,
            "readiness_stability_s": 5.0,
            "readiness_freshness_limits_s": {
                "odometry": 1.0,
                "heartbeat": 3.0,
                "valid_home_position": 3.0,
            },
            "runtime_identity": {
                "git_commit": provenance["git_commit"],
                "container_id": container_image["runtime_container_id"],
                "container_image_digest": container_image["digest"],
                "host": runtime_capabilities["host"],
                "kernel": {
                    "system": runtime_capabilities["system"],
                    "machine": runtime_capabilities["machine"],
                    "release": runtime_capabilities["kernel_release"],
                    "version": runtime_capabilities["kernel_version"],
                },
                "gpu": runtime_capabilities["gpu"],
            },
        }
    ]
    robot_xml = (
        '<sdf version="1.9"><model name="iris"><plugin name="ArduPilotPlugin" '
        'filename="ArduPilotPlugin"><fdm_addr>127.0.0.1</fdm_addr>'
        '<fdm_port_in>{port}</fdm_port_in></plugin></model></sdf>'
    )
    robot_probe_records = []
    for index in range(1, 6):
        name = f"uav{index}"
        raw_description = robot_xml.format(port=9002 + (index - 1) * 10).encode()
        robot_probe_records.append(
            {
                "name": name,
                "namespace": f"/{name}",
                "fdm_addr": "127.0.0.1",
                "fdm_port_in": 9002 + (index - 1) * 10,
                "robot_description_b64": base64.b64encode(raw_description).decode(),
                "robot_description_sha256": hashlib.sha256(raw_description).hexdigest(),
            }
        )
    events.append(
        {
            "event": "robot_description_probe",
            "monotonic_ns": start_ns - 5_500_000_000,
            "phase": "readiness",
            "robots": copy.deepcopy(robot_probe_records),
        }
    )
    launch_start_offset = len("startup link 1 down\n")
    for dwell_sample in range(6):
        sample_ns = start_ns - (5 - dwell_sample) * 1_000_000_000 - 10_000
        odometry_count = (dwell_sample + 1) * 2
        heartbeat_count = dwell_sample + 1
        position_count = (dwell_sample + 1) * 2
        for index in range(1, 6):
            name = f"uav{index}"
            for local_sample in range(2):
                sequence = dwell_sample * 2 + local_sample + 1
                events.append(
                    {
                        "event": "readiness_odometry",
                        "monotonic_ns": sample_ns - 900_000 + index * 1_000 + local_sample,
                        "phase": "readiness",
                        "uav": name,
                        "source_topic": f"/{name}/odometry",
                        "sequence": sequence,
                        "stamp_ns": sequence,
                        "valid": True,
                        "position_m": [float(index), 0.0, 0.0],
                        "orientation_xyzw": [0.0, 0.0, 0.0, 1.0],
                        "linear_velocity_mps": [0.0, 0.0, 0.0],
                        "angular_velocity_rad_s": [0.0, 0.0, 0.0],
                        "linear_speed_mps": 0.0,
                    }
                )
            events.append(
                {
                    "event": "readiness_heartbeat",
                    "monotonic_ns": sample_ns - 700_000 + index,
                    "phase": "readiness",
                    "system_id": index,
                    "component_id": 1,
                    "mav_type": 2,
                    "autopilot": 3,
                    "sequence": heartbeat_count,
                    "sim_time_ns": heartbeat_count,
                }
            )
            for local_sample in range(2):
                sequence = dwell_sample * 2 + local_sample + 1
                events.append(
                    {
                        "event": "readiness_mavlink_global_position",
                        "monotonic_ns": sample_ns - 500_000 + index * 1_000 + local_sample,
                        "phase": "readiness",
                        "system_id": index,
                        "component_id": 1,
                        "sequence": sequence,
                        "lat_deg": 46.607213,
                        "lon_deg": 14.278461,
                        "relative_alt_m": 0.0,
                        "home_distance_m": 0.0,
                    }
                )
        stream_details = {
            "odometry_counts": {f"uav{index}": odometry_count for index in range(1, 6)},
            "heartbeat_counts": {str(index): heartbeat_count for index in range(1, 6)},
            "mavlink_position_counts": {str(index): position_count for index in range(1, 6)},
            "mavlink_valid_home_position_counts": {str(index): position_count for index in range(1, 6)},
            "odometry_age_s": {f"uav{index}": 0.001 for index in range(1, 6)},
            "heartbeat_age_s": {str(index): 0.001 for index in range(1, 6)},
            "mavlink_valid_home_position_age_s": {str(index): 0.001 for index in range(1, 6)},
            "freshness_limits_s": {
                "odometry": 1.0,
                "heartbeat": 3.0,
                "valid_home_position": 3.0,
            },
        }
        events.append(
            {
                "event": "readiness_process_sample",
                "monotonic_ns": sample_ns,
                "sampled_monotonic_ns": sample_ns,
                "phase": "readiness",
                "offset_s": 2.0 + dwell_sample,
                "counts": copy.deepcopy(REQUIRED_PROCESS_COUNTS),
                "processes": copy.deepcopy(processes),
                "error": None,
                "streams_ready": True,
                "stream_details": stream_details,
                "process_healthy": True,
                "process_failures": [],
                "robot_descriptions_ready": True,
                "link_down_detected": False,
                "fatal_launch_marker_detected": False,
                "qualifying": True,
                "reasons": [],
                "stability_elapsed_s": float(dwell_sample),
                "required_stability_s": 5.0,
                "launch_log_scanned_start_offset": launch_start_offset,
                "launch_log_scanned_end_offset": launch_start_offset,
            }
        )
    readiness_counts = {
        "ready": True,
        "elapsed_s": 7.0,
        "timeout_s": 90.0,
        "stability_s": 5.0,
        "stability_started_monotonic_ns": start_ns - 5_000_010_000,
        "stability_completed_monotonic_ns": start_ns - 10_000,
        "qualifying_process_samples": 6,
        "readiness_process_samples": 6,
        "robot_description_errors": [],
        "launch_log_readiness_end_offset": launch_start_offset,
        "odometry_counts": {f"uav{index}": 12 for index in range(1, 6)},
        "heartbeat_counts": {str(index): 6 for index in range(1, 6)},
        "mavlink_position_counts": {str(index): 12 for index in range(1, 6)},
        "mavlink_valid_home_position_counts": {
            str(index): 12 for index in range(1, 6)
        },
        "odometry_age_s": {f"uav{index}": 0.001 for index in range(1, 6)},
        "heartbeat_age_s": {str(index): 0.001 for index in range(1, 6)},
        "mavlink_valid_home_position_age_s": {str(index): 0.001 for index in range(1, 6)},
        "freshness_limits_s": {
            "odometry": 1.0,
            "heartbeat": 3.0,
            "valid_home_position": 3.0,
        },
    }
    events.append(
        {
            "event": "readiness",
            "monotonic_ns": start_ns - 5_000,
            "phase": "readiness",
            "error": None,
            **readiness_counts,
        }
    )
    events.append(
        {
            "event": "measurement_start",
            "monotonic_ns": start_ns,
            "phase": "measurement",
            "measurement_started_monotonic_ns": start_ns,
            "run_id": run_dir.name,
            "runtime_id": runtime_id,
            "source_hash": source_hash,
            "launch_log_observation_offset": len("startup link 1 down\n"),
        }
    )
    events.append(
        {
            "event": "clock_correlation",
            "monotonic_ns": start_ns,
            "phase": "measurement",
            "correlation_point": "measurement_start",
            "monotonic_clock": "CLOCK_MONOTONIC",
            "wall_clock": "CLOCK_REALTIME",
        }
    )
    uavs = []
    for index in range(1, 6):
        name = f"uav{index}"
        for sample in range(1501):
            offset_ns = sample * 200_000_000
            events.append(
                {
                    "event": "odometry",
                    "monotonic_ns": start_ns + offset_ns,
                    "phase": "measurement",
                    "uav": name,
                    "source_topic": f"/{name}/odometry",
                    "sequence": sample + 1,
                    "stamp_ns": offset_ns,
                    "valid": True,
                    "position_m": [float(index), 0.0, 0.0],
                    "orientation_xyzw": [0.0, 0.0, 0.0, 1.0],
                    "linear_velocity_mps": [0.0, 0.0, 0.0],
                    "angular_velocity_rad_s": [0.0, 0.0, 0.0],
                    "linear_speed_mps": 0.0,
                }
            )
        for sample in range(301):
            offset_ns = sample * 1_000_000_000
            events.append(
                {
                    "event": "heartbeat",
                    "monotonic_ns": start_ns + offset_ns,
                    "phase": "measurement",
                    "system_id": index,
                    "component_id": 1,
                    "mav_type": 2,
                    "autopilot": 3,
                    "sequence": sample + 1,
                    "sim_time_ns": offset_ns,
                }
            )
        for sample in range(2):
            events.append(
                {
                    "event": "mavlink_global_position",
                    "monotonic_ns": start_ns + sample * duration_ns,
                    "phase": "measurement",
                    "system_id": index,
                    "component_id": 1,
                    "sequence": sample + 1,
                    "lat_deg": 46.607213,
                    "lon_deg": 14.278461,
                    "relative_alt_m": 0.0,
                    "home_distance_m": 0.0,
                }
            )
        uavs.append(
            {
                "name": name,
                "system_id": index,
                "dds_udp_port": 2018 + index,
                "gazebo_model": True,
                "sitl_healthy": True,
                "heartbeat": True,
                "heartbeat_count": 301,
                "heartbeat_rate_hz": 1.0,
                "heartbeat_time_basis": "odometry_sim_stamp",
                "heartbeat_age_s": 0.0,
                "heartbeat_max_gap_s": 1.0,
                "heartbeat_sim_age_s": 0.0,
                "heartbeat_sim_start_delay_s": 0.0,
                "heartbeat_sim_max_gap_s": 1.0,
                "odometry_fresh": True,
                "odometry_count": 1501,
                "odometry_rate_hz": 5.0,
                "odometry_age_s": 0.0,
                "odometry_start_delay_s": 0.0,
                "odometry_max_gap_s": 0.2,
                "odometry_realtime_factor": 1.0,
                "odometry_invalid_samples": 0,
                "odometry_nonadvancing_stamps": 0,
                "odometry_first_position_m": [float(index), 0.0, 0.0],
                "odometry_last_position_m": [float(index), 0.0, 0.0],
                "odometry_max_displacement_m": 0.0,
                "odometry_max_speed_mps": 0.0,
                "mavlink_pose": True,
                "mavlink_position_count": 2,
                "mavlink_valid_home_position_count": 2,
                "mavlink_minimum_relative_alt_m": 0.0,
                "mavlink_maximum_relative_alt_m": 0.0,
                "mavlink_maximum_home_distance_m": 0.0,
            }
        )
    for sample in range(301):
        events.append(
            {
                "event": "process_sample",
                "monotonic_ns": start_ns + sample * 1_000_000_000,
                "phase": "measurement",
                "offset_s": float(sample),
                "counts": {
                    "arducopter": 5,
                    "mavproxy": 5,
                    "micro_ros_agent": 5,
                    "gazebo_server": 1,
                },
                "processes": copy.deepcopy(processes),
            }
        )
    events.append(
        {
            "event": "clock_correlation",
            "monotonic_ns": start_ns + duration_ns,
            "phase": "measurement",
            "correlation_point": "measurement_end",
            "monotonic_clock": "CLOCK_MONOTONIC",
            "wall_clock": "CLOCK_REALTIME",
        }
    )
    gazebo_stdout = b'world { name: "map" }\n' + b"\n".join(
        f'model {{ name: "uav{index}" }}'.encode() for index in range(1, 6)
    )
    gazebo_stderr = b""
    events.append(
        {
            "event": "gazebo_scene_probe",
            "monotonic_ns": start_ns + duration_ns + 1,
            "phase": "finalization",
            "exit_code": 0,
            "command": [
                "gz",
                "service",
                "-s",
                "/world/map/scene/info",
                "--reqtype",
                "gz.msgs.Empty",
                "--reptype",
                "gz.msgs.Scene",
                "--timeout",
                "5000",
                "--req",
                "",
            ],
            "world_name": "map",
            "model_names": [f"uav{index}" for index in range(1, 6)],
            "stdout_b64": base64.b64encode(gazebo_stdout).decode(),
            "stdout_sha256": hashlib.sha256(gazebo_stdout).hexdigest(),
            "stderr_b64": base64.b64encode(gazebo_stderr).decode(),
            "stderr_sha256": hashlib.sha256(gazebo_stderr).hexdigest(),
        }
    )
    launch_text = "startup link 1 down\nmeasurement healthy\n"
    launch_end_offset = len(launch_text.encode())
    launch_sha256 = hashlib.sha256(launch_text.encode()).hexdigest()
    events.append(
        {
            "event": "health_probe_complete",
            "monotonic_ns": start_ns + duration_ns + 2,
            "phase": "finalization",
            "measurement_ended_monotonic_ns": start_ns + duration_ns,
            "observed_duration_s": 300.0,
            "passed": True,
            "errors": [],
            "launch_log_observation_offset": launch_start_offset,
            "launch_log_observation_end_offset": launch_end_offset,
            "launch_log_observation_sha256": launch_sha256,
        }
    )
    def event_priority(event: dict) -> int:
        if event["event"] == "health_probe_start":
            return 0
        if event["event"] == "clock_correlation":
            return 1 if event["correlation_point"] == "measurement_start" else 4
        if event["event"] == "measurement_start":
            return 2
        if event["event"] == "health_probe_complete":
            return 5
        return 3

    events.sort(key=lambda event: (event["monotonic_ns"], event_priority(event)))
    previous_clock = -1
    for event_seq, event in enumerate(events, start=1):
        event["monotonic_ns"] = max(event["monotonic_ns"], previous_clock + 1)
        previous_clock = event["monotonic_ns"]
        event.update(
            {
                "schema_version": 2,
                "run_id": run_dir.name,
                "runtime_id": runtime_id,
                "source_hash": source_hash,
                "profile": M1_PROFILE,
                "scenario_id": M1_SCENARIO_ID,
                "provenance_sha256": provenance_sha256,
                "contract": M1_CONTRACT_ID,
                "plan_version": 3,
                "contract_sha256": M1_CONTRACT_SHA256,
                "event_seq": event_seq,
                "wall_time_ns": wall_epoch_ns + event["monotonic_ns"],
                "wall_utc": datetime.fromtimestamp(
                    (wall_epoch_ns + event["monotonic_ns"]) / 1_000_000_000,
                    timezone.utc,
                ).isoformat(),
            }
        )
    measurement_start_event = next(
        event for event in events if event["event"] == "measurement_start"
    )
    measurement_start_event["measurement_started_monotonic_ns"] = (
        measurement_start_event["monotonic_ns"]
    )
    measurement_end_clock = next(
        event
        for event in events
        if event.get("event") == "clock_correlation"
        and event.get("correlation_point") == "measurement_end"
    )
    next(
        event for event in events if event["event"] == "health_probe_complete"
    )["measurement_ended_monotonic_ns"] = measurement_end_clock["monotonic_ns"] - 1
    raw = "".join(json.dumps(event, sort_keys=True) + "\n" for event in events)
    (run_dir / "logs").mkdir(parents=True)
    (run_dir / "metrics").mkdir(parents=True)
    (run_dir / "logs/five_uav_health_events.jsonl").write_text(raw, encoding="utf-8")
    (run_dir / "logs/five_uav_launch.log").write_text(launch_text, encoding="utf-8")
    (run_dir / "metrics/provenance.json").write_text(provenance_raw, encoding="utf-8")
    role_hashes = {
        role: identity_set_sha256(
            process_identity_digest(process)
            for process in processes
            if process["role"] == role
        )
        for role in REQUIRED_PROCESS_COUNTS
    }
    runtime_endpoints, endpoint_failures = derive_runtime_endpoints(processes)
    if endpoint_failures:  # pragma: no cover - fixture programming error
        raise AssertionError(endpoint_failures)
    summary = {
        "schema_version": 2,
        "contract": M1_CONTRACT_ID,
        "plan_version": 3,
        "contract_sha256": M1_CONTRACT_SHA256,
        "run_id": run_dir.name,
        "runtime_id": runtime_id,
        "source_hash": source_hash,
        "profile": M1_PROFILE,
        "scenario_id": M1_SCENARIO_ID,
        "phases": ["readiness", "measurement", "finalization"],
        "provenance_sha256": provenance_sha256,
        "clock_domains": {
            "monotonic": "CLOCK_MONOTONIC",
            "wall": "CLOCK_REALTIME",
            "correlation_event": "clock_correlation",
        },
        "started_utc": datetime.fromtimestamp(
            (wall_epoch_ns + start_ns) / 1_000_000_000, timezone.utc
        ).isoformat(),
        "ended_utc": datetime.fromtimestamp(
            (wall_epoch_ns + start_ns + duration_ns) / 1_000_000_000,
            timezone.utc,
        ).isoformat(),
        "component_only": True,
        "packet_path_eligible": False,
        "observed_duration_s": 300.0,
        "minimum_duration_s": 300.0,
        "minimum_heartbeat_hz": 0.8,
        "minimum_odometry_hz": 5.0,
        "maximum_freshness_age_s": 1.0,
        "readiness": readiness_counts,
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
            for record in robot_probe_records
        ],
        "gazebo_model_names": [f"uav{index}" for index in range(1, 6)],
        "gazebo_world_name": "map",
        "launch_log": "logs/five_uav_launch.log",
        "launch_log_observation_offset": launch_start_offset,
        "launch_log_observation_end_offset": launch_end_offset,
        "launch_log_observation_sha256": launch_sha256,
        "raw_event_log": "logs/five_uav_health_events.jsonl",
        "raw_event_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        "errors": [],
        "passed": True,
        "uavs": uavs,
        "process_health": {
            "process_group": 900,
            "samples": 301,
            "required_exact_counts": REQUIRED_PROCESS_COUNTS,
            "observed_count_ranges": {
                role: {"minimum": count, "maximum": count}
                for role, count in REQUIRED_PROCESS_COUNTS.items()
            },
            "stable_identity_set_sha256": role_hashes,
            "all_process_identity_set_sha256": identity_set_sha256(
                process_identity_digest(process) for process in processes
            ),
            "first_sample_delay_s": 0.0,
            "last_sample_age_s": 0.0,
            "maximum_sample_gap_s": 1.0,
            "runtime_endpoints": runtime_endpoints,
        },
    }
    (run_dir / "metrics/five_uav_health.json").write_text(
        json.dumps(summary), encoding="utf-8"
    )


class FiveUavHealthV2Tests(unittest.TestCase):
    def test_rate_uses_sample_intervals(self) -> None:
        self.assertEqual(
            rate_hz({"count": 6, "first_monotonic_s": 10.0, "last_monotonic_s": 12.5}),
            2.0,
        )
        self.assertEqual(rate_hz({"count": 1}), 0.0)

    def test_startup_link_down_is_excluded_but_runtime_link_down_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "launch.log"
            startup = b"Waiting for heartbeat\nlink 1 down\n"
            path.write_bytes(startup + b"healthy observation\n")
            self.assertEqual(launch_log_findings(path, len(startup)), [])
            path.write_bytes(startup + b"healthy observation\nlink 1 down\n")
            self.assertIn("link 1 down", launch_log_findings(path, len(startup)))

    def test_fatal_startup_error_is_never_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "launch.log"
            for marker in (
                "process has died",
                "Failed to open (/workspace/runtime/dev/ttyROS1): No such file or directory",
            ):
                with self.subTest(marker=marker):
                    startup = f"{marker}\n".encode()
                    path.write_bytes(startup + b"observation\n")
                    self.assertTrue(launch_log_findings(path, len(startup)))

    def test_readiness_requires_gps_for_every_uav(self) -> None:
        names = [f"uav{index}" for index in range(1, 6)]
        odometry = {name: {"count": 2} for name in names}
        heartbeats = {index: {"count": 1} for index in range(1, 6)}
        positions = {
            index: {"count": 2, "valid_home_position_count": 2}
            for index in range(1, 6)
        }
        ready, details = readiness_status(names, odometry, heartbeats, positions)
        self.assertTrue(ready)
        positions[5] = {"count": 0, "valid_home_position_count": 0}
        ready, details = readiness_status(names, odometry, heartbeats, positions)
        self.assertFalse(ready)
        self.assertEqual(details["mavlink_position_counts"]["5"], 0)

    def test_failed_readiness_is_a_fail_fast_measurement_contract(self) -> None:
        # The runtime collector selects a zero-length measurement when this
        # predicate remains false; it must not spend 300 seconds on a run that
        # is already ineligible at readiness.
        names = [f"uav{index}" for index in range(1, 6)]
        ready, _details = readiness_status(names, {}, {}, {})
        self.assertFalse(ready)
        self.assertEqual(selected_measurement_duration(ready, 300.0), 0.0)
        self.assertEqual(selected_measurement_duration(True, 300.0), 300.0)

    def test_process_monitor_stops_without_ros_executor_work(self) -> None:
        stop_event = threading.Event()
        samples = []
        emitted = []

        class FakeEventLog:
            def emit(self, event: str, **fields: object) -> None:
                emitted.append((event, fields))
                stop_event.set()

        run_process_monitor(
            42,
            time.monotonic(),
            stop_event,
            samples,
            FakeEventLog(),
            interval_s=0.01,
            sampler=lambda process_group: (
                {
                    "arducopter": 5,
                    "mavproxy": 5,
                    "micro_ros_agent": 5,
                    "gazebo_server": 1,
                },
                [{"pid": process_group}],
                None,
            ),
        )
        self.assertEqual(len(samples), 1)
        self.assertEqual(emitted[0][0], "process_sample")
        self.assertEqual(samples[0]["counts"]["arducopter"], 5)

    def test_process_monitor_drops_sample_after_measurement_is_sealed(self) -> None:
        stop_event = threading.Event()
        measurement_closed = threading.Event()
        measurement_closed.set()
        samples = []
        emitted = []

        class FakeEventLog:
            def emit(self, event: str, **fields: object) -> None:
                emitted.append((event, fields))

        run_process_monitor(
            42,
            time.monotonic(),
            stop_event,
            samples,
            FakeEventLog(),
            measurement_closed=measurement_closed,
            measurement_lock=threading.Lock(),
            sampler=lambda process_group: (
                {
                    "arducopter": 5,
                    "mavproxy": 5,
                    "micro_ros_agent": 5,
                    "gazebo_server": 1,
                },
                [{"pid": process_group}],
                None,
            ),
        )
        self.assertEqual(samples, [])
        self.assertEqual(emitted, [])

    def test_short_health_run_cannot_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "short_run"
            (run_dir / "metrics").mkdir(parents=True)
            (run_dir / "metrics/five_uav_health.json").write_text(
                json.dumps(health_record(run_dir.name, duration_s=10.0)), encoding="utf-8"
            )
            result = five_uav_health_status(run_dir)
            self.assertEqual(result["status"], "failed")
            self.assertIn("below 300", "\n".join(result["details"]["failures"]))

    def test_complete_numeric_health_record_can_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "complete_run"
            write_complete_health_evidence(run_dir)
            self.assertEqual(five_uav_health_status(run_dir)["status"], "passed")

    def test_heartbeat_after_measurement_boundary_fails_without_tolerance(self) -> None:
        """The collector must seal its streams instead of accepting clock jitter."""

        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "late_heartbeat"
            write_complete_health_evidence(run_dir)

            def mutate(records: list[dict]) -> None:
                last_heartbeat = next(
                    record
                    for record in reversed(records)
                    if record.get("event") == "heartbeat" and record.get("system_id") == 2
                )
                completion = next(
                    record
                    for record in records
                    if record.get("event") == "health_probe_complete"
                )
                completion["measurement_ended_monotonic_ns"] = (
                    last_heartbeat["monotonic_ns"] - 1
                )

            rewrite_raw_and_rehash(run_dir, mutate)
            result = five_uav_health_status(run_dir)
            self.assertEqual(result["status"], "failed")
            self.assertIn(
                "uav2: raw heartbeat wall age",
                "\n".join(result["details"]["failures"]),
            )

    def test_summary_without_raw_events_cannot_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "summary_only"
            (run_dir / "metrics").mkdir(parents=True)
            record = health_record(run_dir.name)
            record.update({"runtime_id": "forged-runtime", "source_hash": "a" * 64})
            (run_dir / "metrics/provenance.json").write_text(
                json.dumps({"source_hash": "a" * 64}), encoding="utf-8"
            )
            (run_dir / "metrics/five_uav_health.json").write_text(
                json.dumps(record), encoding="utf-8"
            )
            result = five_uav_health_status(run_dir)
            self.assertEqual(result["status"], "failed")
            self.assertIn("raw evidence", "\n".join(result["details"]["failures"]))

    def test_malformed_m1_consumption_fails_without_validator_crash(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "malformed_consumption"
            write_complete_health_evidence(run_dir)
            provenance_path = run_dir / "metrics/provenance.json"
            provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
            provenance["qualification_consumption"]["consumed_node_sha256"] = 7
            provenance_path.write_text(
                json.dumps(provenance, sort_keys=True) + "\n", encoding="utf-8"
            )

            result = five_uav_health_status(run_dir)

        self.assertEqual(result["status"], "failed")
        self.assertIn(
            "M1 provenance does not consume exactly Q0 and Q1",
            result["details"]["failures"],
        )

    def test_mixed_identity_and_broken_event_sequence_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "mixed_raw"
            write_complete_health_evidence(run_dir)

            def mutate(records: list[dict]) -> None:
                records[2]["runtime_id"] = "runtime-from-another-run"
                records[3]["event_seq"] = records[2]["event_seq"]

            rewrite_raw_and_rehash(run_dir, mutate)

            result = five_uav_health_status(run_dir)
            self.assertEqual(result["status"], "failed")
            failures = "\n".join(result["details"]["failures"])
            self.assertIn("runtime_id", failures)
            self.assertIn("event_seq", failures)

    def test_fatal_launch_marker_is_derived_from_log_not_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "fatal_log"
            write_complete_health_evidence(run_dir)
            startup = "startup link 1 down\n"
            rewrite_launch_and_rebind(
                run_dir,
                startup + "process has died despite forged errors=[]\n",
                len(startup.encode()),
            )

            result = five_uav_health_status(run_dir)
            self.assertEqual(result["status"], "failed")
            self.assertIn("process has died", "\n".join(result["details"]["failures"]))

    def test_failed_ttyros_open_before_warmup_is_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "ttyros_failure"
            write_complete_health_evidence(run_dir)
            startup = (
                "[ttyROS1 --sysid 1 -5] Failed to open "
                "(/workspace/runtime/dev/ttyROS1): No such file or directory\n"
                "startup link 1 down\n"
            )
            rewrite_launch_and_rebind(
                run_dir,
                startup + "measurement healthy\n",
                len(startup.encode()),
            )

            result = five_uav_health_status(run_dir)
            self.assertEqual(result["status"], "failed")
            self.assertIn("failed to open", "\n".join(result["details"]["failures"]))

    def test_extra_mavproxy_fails_even_when_producer_counts_claim_five(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "extra_mavproxy"
            write_complete_health_evidence(run_dir)

            def mutate(records: list[dict]) -> None:
                sample = next(
                    record for record in records if record.get("event") == "process_sample"
                )
                extra = process_identity("mavproxy", 6)
                extra["pid"] = 9_993
                extra["start_ticks"] = 99_993
                sample["processes"].append(extra)
                self.assertEqual(sample["counts"]["mavproxy"], 5)

            rewrite_raw_and_rehash(run_dir, mutate)
            result = five_uav_health_status(run_dir)
            self.assertEqual(result["status"], "failed")
            failures = "\n".join(result["details"]["failures"])
            self.assertTrue(
                "mavproxy" in failures and ("exact" in failures or "count" in failures),
                failures,
            )

    def test_zombie_required_process_fails_even_when_count_is_five(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "zombie_mavproxy"
            write_complete_health_evidence(run_dir)

            def mutate(records: list[dict]) -> None:
                sample = next(
                    record for record in records if record.get("event") == "process_sample"
                )
                zombie = next(
                    process
                    for process in sample["processes"]
                    if process.get("role") == "mavproxy"
                )
                zombie["state"] = "Z"
                zombie["stat"] = "Z"

            rewrite_raw_and_rehash(run_dir, mutate)
            result = five_uav_health_status(run_dir)
            self.assertEqual(result["status"], "failed")
            failures = "\n".join(result["details"]["failures"])
            self.assertTrue("zombie" in failures or "state" in failures, failures)

    def test_process_identity_mutation_mid_measurement_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "identity_mutation"
            write_complete_health_evidence(run_dir)

            def mutate(records: list[dict]) -> None:
                samples = [
                    record for record in records if record.get("event") == "process_sample"
                ]
                arducopter = next(
                    process
                    for process in samples[len(samples) // 2]["processes"]
                    if process.get("role") == "arducopter"
                )
                arducopter["pid"] += 50_000
                arducopter["start_ticks"] += 50_000

            rewrite_raw_and_rehash(run_dir, mutate)
            result = five_uav_health_status(run_dir)
            self.assertEqual(result["status"], "failed")
            self.assertIn("identity", "\n".join(result["details"]["failures"]))

    def test_two_second_measurement_process_hole_exceeds_continuity_bound(self) -> None:
        self.assertEqual(M1_PROCESS_SAMPLE_MAX_GAP_S, 1.5)
        self.assertEqual(COLLECTOR_PROCESS_SAMPLE_MAX_GAP_S, 1.5)
        self.assertTrue(
            process_window_metrics(
                [{"offset_s": 0.0}, {"offset_s": 1.5}], 1.5
            )["covered"]
        )
        self.assertFalse(
            process_window_metrics(
                [{"offset_s": 0.0}, {"offset_s": 1.500001}], 1.500001
            )["covered"]
        )
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "measurement_process_hole"
            write_complete_health_evidence(run_dir)

            def mutate(records: list[dict]) -> None:
                samples = [
                    record for record in records if record.get("event") == "process_sample"
                ]
                records.remove(samples[len(samples) // 2])
                for event_seq, record in enumerate(records, start=1):
                    record["event_seq"] = event_seq

            records = rewrite_raw_and_rehash(run_dir, mutate)
            rebind_process_health_identity_summary(run_dir, records)
            result = five_uav_health_status(run_dir)
            self.assertEqual(result["status"], "failed")
            self.assertIn(
                "1.5-second measurement continuity bound",
                "\n".join(result["details"]["failures"]),
            )

    def test_readiness_to_first_measurement_sample_gap_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "readiness_measurement_gap"
            write_complete_health_evidence(run_dir)
            shift_ns = int((M1_PROCESS_SAMPLE_MAX_GAP_S + 0.1) * 1_000_000_000)

            def mutate(records: list[dict]) -> None:
                for record in records:
                    if record.get("phase") != "readiness":
                        continue
                    record["monotonic_ns"] -= shift_ns
                    record["wall_time_ns"] -= shift_ns
                    record["wall_utc"] = datetime.fromtimestamp(
                        record["wall_time_ns"] / 1_000_000_000, timezone.utc
                    ).isoformat()
                    if record.get("event") == "readiness_process_sample":
                        record["sampled_monotonic_ns"] -= shift_ns
                    if record.get("event") == "readiness":
                        record["stability_started_monotonic_ns"] -= shift_ns
                        record["stability_completed_monotonic_ns"] -= shift_ns

            rewrite_raw_and_rehash(run_dir, mutate)
            summary_path = run_dir / "metrics/five_uav_health.json"
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            summary["readiness"]["stability_started_monotonic_ns"] -= shift_ns
            summary["readiness"]["stability_completed_monotonic_ns"] -= shift_ns
            summary_path.write_text(json.dumps(summary), encoding="utf-8")

            result = five_uav_health_status(run_dir)
            self.assertEqual(result["status"], "failed")
            self.assertIn(
                "readiness completion to first measurement process sample exceeds",
                "\n".join(result["details"]["failures"]),
            )

    def test_critical_process_replacement_at_phase_boundary_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "critical_boundary_replacement"
            write_complete_health_evidence(run_dir)

            def mutate(records: list[dict]) -> None:
                for sample in (
                    record for record in records if record.get("event") == "process_sample"
                ):
                    process = next(
                        item
                        for item in sample["processes"]
                        if item.get("role") == "arducopter"
                    )
                    process["pid"] = 9_101
                    process["start_ticks"] = 90_101

            records = rewrite_raw_and_rehash(run_dir, mutate)
            rebind_process_health_identity_summary(run_dir, records)
            result = five_uav_health_status(run_dir)
            self.assertEqual(result["status"], "failed")
            failures = "\n".join(result["details"]["failures"])
            self.assertIn(
                "arducopter identity set differs from final stable readiness", failures
            )
            self.assertIn("full identity set differs from final stable readiness", failures)

    def test_launch_helper_replacement_at_phase_boundary_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "helper_boundary_replacement"
            write_complete_health_evidence(run_dir)
            readiness_helper = launch_helper_identity(8_001, 80_001)
            measurement_helper = launch_helper_identity(8_002, 80_002)

            def mutate(records: list[dict]) -> None:
                for sample in records:
                    if sample.get("event") == "readiness_process_sample":
                        sample["processes"].append(copy.deepcopy(readiness_helper))
                    elif sample.get("event") == "process_sample":
                        sample["processes"].append(copy.deepcopy(measurement_helper))

            records = rewrite_raw_and_rehash(run_dir, mutate)
            rebind_process_health_identity_summary(run_dir, records)
            result = five_uav_health_status(run_dir)
            self.assertEqual(result["status"], "failed")
            failures = "\n".join(result["details"]["failures"])
            self.assertIn("full identity set differs from final stable readiness", failures)
            for role in REQUIRED_PROCESS_COUNTS:
                self.assertNotIn(
                    f"{role} identity set differs from final stable readiness", failures
                )

    def test_missing_executable_hash_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "missing_executable_hash"
            write_complete_health_evidence(run_dir)

            def mutate(records: list[dict]) -> None:
                sample = next(
                    record for record in records if record.get("event") == "process_sample"
                )
                sample["processes"][0].pop("exe_sha256")

            rewrite_raw_and_rehash(run_dir, mutate)
            result = five_uav_health_status(run_dir)
            self.assertEqual(result["status"], "failed")
            failures = "\n".join(result["details"]["failures"])
            self.assertTrue("exe_sha256" in failures or "executable" in failures, failures)

    def test_fake_tmp_executable_with_valid_role_argv_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "fake_tmp_executable"
            write_complete_health_evidence(run_dir)

            def mutate(records: list[dict]) -> None:
                for sample in records:
                    if sample.get("event") not in {
                        "readiness_process_sample",
                        "process_sample",
                    }:
                        continue
                    process = next(
                        item
                        for item in sample["processes"]
                        if item.get("role") == "arducopter"
                    )
                    process["exe_path"] = "/tmp/arducopter"
                    process["exe_sha256"] = hashlib.sha256(b"fake-arducopter").hexdigest()

            rewrite_raw_and_rehash(run_dir, mutate)
            result = five_uav_health_status(run_dir)
            self.assertEqual(result["status"], "failed")
            failures = "\n".join(result["details"]["failures"])
            self.assertIn("absent from the accepted-image manifest", failures)

    def test_fake_mavproxy_script_at_tmp_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "fake_mavproxy_script"
            write_complete_health_evidence(run_dir)

            def mutate(records: list[dict]) -> None:
                for sample in records:
                    if sample.get("event") not in {
                        "readiness_process_sample",
                        "process_sample",
                    }:
                        continue
                    process = next(
                        item
                        for item in sample["processes"]
                        if item.get("role") == "mavproxy"
                    )
                    argv = list(process["cmdline"])
                    argv[1] = "/tmp/mavproxy.py"
                    raw = b"\0".join(value.encode() for value in argv) + b"\0"
                    process["cmdline"] = argv
                    process["cmdline_b64"] = base64.b64encode(raw).decode("ascii")
                    process["cmdline_sha256"] = hashlib.sha256(raw).hexdigest()
                    process["arguments"] = " ".join(argv)
                    process["invoked_files"] = {
                        "/tmp/mavproxy.py": hashlib.sha256(b"fake-mavproxy").hexdigest()
                    }

            rewrite_raw_and_rehash(run_dir, mutate)
            result = five_uav_health_status(run_dir)
            self.assertEqual(result["status"], "failed")
            failures = "\n".join(result["details"]["failures"])
            self.assertIn("MAVProxy script path is not canonical", failures)
            self.assertIn("MAVProxy script bytes differ", failures)

    def test_sleep_decoy_named_mavproxy_does_not_satisfy_required_role(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "sleep_decoy"
            write_complete_health_evidence(run_dir)

            def mutate(records: list[dict]) -> None:
                sample = next(
                    record for record in records if record.get("event") == "process_sample"
                )
                process = next(
                    item for item in sample["processes"] if item.get("role") == "mavproxy"
                )
                argv = ["/usr/bin/sleep", "/tmp/mavproxy.py"]
                raw = b"\0".join(value.encode() for value in argv) + b"\0"
                process.update(
                    {
                        "command": "sleep",
                        "arguments": " ".join(argv),
                        "cmdline": argv,
                        "cmdline_b64": base64.b64encode(raw).decode(),
                        "cmdline_sha256": hashlib.sha256(raw).hexdigest(),
                        "exe_path": "/usr/bin/sleep",
                        "exe_sha256": hashlib.sha256(b"/usr/bin/sleep").hexdigest(),
                    }
                )

            rewrite_raw_and_rehash(run_dir, mutate)
            result = five_uav_health_status(run_dir)
            self.assertEqual(result["status"], "failed")
            failures = "\n".join(result["details"]["failures"])
            self.assertIn("role does not match independently decoded argv", failures)
            self.assertIn("has 4 mavproxy", failures)

    def test_duplicate_endpoint_flag_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "duplicate_flag"
            write_complete_health_evidence(run_dir)

            def mutate(records: list[dict]) -> None:
                sample = next(
                    record for record in records if record.get("event") == "process_sample"
                )
                process = next(
                    item for item in sample["processes"] if item.get("role") == "mavproxy"
                )
                argv = process["cmdline"] + ["--master", "tcp:127.0.0.1:5760"]
                raw = b"\0".join(value.encode() for value in argv) + b"\0"
                process["cmdline"] = argv
                process["cmdline_b64"] = base64.b64encode(raw).decode()
                process["cmdline_sha256"] = hashlib.sha256(raw).hexdigest()
                process["arguments"] = " ".join(argv)

            rewrite_raw_and_rehash(run_dir, mutate)
            result = five_uav_health_status(run_dir)
            self.assertEqual(result["status"], "failed")
            self.assertIn("malformed mavproxy endpoint", "\n".join(result["details"]["failures"]))

    def test_readiness_dwell_requires_every_process_snapshot_to_qualify(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "readiness_dwell_mutation"
            write_complete_health_evidence(run_dir)

            def mutate(records: list[dict]) -> None:
                sample = next(
                    record
                    for record in records
                    if record.get("event") == "readiness_process_sample"
                )
                sample["qualifying"] = False
                sample["reasons"] = ["forged transient failure"]

            rewrite_raw_and_rehash(run_dir, mutate)
            result = five_uav_health_status(run_dir)
            self.assertEqual(result["status"], "failed")
            self.assertIn("not independently eligible", "\n".join(result["details"]["failures"]))

    def test_robot_description_port_is_derived_from_raw_xml(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "robot_description_mutation"
            write_complete_health_evidence(run_dir)

            def mutate(records: list[dict]) -> None:
                probe = next(
                    record for record in records if record.get("event") == "robot_description_probe"
                )
                robot = probe["robots"][0]
                raw = base64.b64decode(robot["robot_description_b64"])
                raw = raw.replace(b"<fdm_port_in>9002</fdm_port_in>", b"<fdm_port_in>9999</fdm_port_in>")
                robot["robot_description_b64"] = base64.b64encode(raw).decode()
                robot["robot_description_sha256"] = hashlib.sha256(raw).hexdigest()
                robot["fdm_port_in"] = 9999

            rewrite_raw_and_rehash(run_dir, mutate)
            result = five_uav_health_status(run_dir)
            self.assertEqual(result["status"], "failed")
            self.assertIn("FDM endpoint", "\n".join(result["details"]["failures"]))

    def test_raw_odometry_quaternion_cannot_hide_behind_valid_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "odometry_vector_mutation"
            write_complete_health_evidence(run_dir)

            def mutate(records: list[dict]) -> None:
                odometry = next(
                    record for record in records if record.get("event") == "odometry"
                )
                odometry["orientation_xyzw"] = [0.0, 0.0, 0.0, 10.0]
                self.assertTrue(odometry["valid"])

            rewrite_raw_and_rehash(run_dir, mutate)
            result = five_uav_health_status(run_dir)
            self.assertEqual(result["status"], "failed")
            self.assertIn("quaternion norm", "\n".join(result["details"]["failures"]))

    def test_post_window_shutdown_noise_is_outside_bounded_log(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "bounded_log"
            write_complete_health_evidence(run_dir)
            with (run_dir / "logs/five_uav_launch.log").open("a", encoding="utf-8") as log:
                log.write("process has died during requested cleanup\nlink 1 down\n")
            self.assertEqual(five_uav_health_status(run_dir)["status"], "passed")

    def test_raw_profile_scenario_and_phase_binding_cannot_be_forged(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "profile_mutation"
            write_complete_health_evidence(run_dir)

            def mutate(records: list[dict]) -> None:
                records[len(records) // 2]["profile"] = "another_component"
                records[len(records) // 2 + 1]["scenario_id"] = "another_scenario"
                records[len(records) // 2 + 2]["phase"] = "undeclared_phase"

            rewrite_raw_and_rehash(run_dir, mutate)
            result = five_uav_health_status(run_dir)
            self.assertEqual(result["status"], "failed")
            failures = "\n".join(result["details"]["failures"])
            self.assertIn("profile", failures)
            self.assertIn("scenario", failures)
            self.assertIn("phase", failures)

    def test_duplicate_sitl_endpoint_fails_even_with_stable_processes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "duplicate_sitl_endpoint"
            write_complete_health_evidence(run_dir)

            def mutate(records: list[dict]) -> None:
                for sample in (
                    record for record in records if record.get("event") == "process_sample"
                ):
                    mavproxy = next(
                        process
                        for process in sample["processes"]
                        if process.get("role") == "mavproxy"
                    )
                    argv = mavproxy["cmdline"]
                    argv[argv.index("tcp:127.0.0.1:5760")] = "tcp:127.0.0.1:5770"
                    raw = b"\0".join(value.encode() for value in argv) + b"\0"
                    mavproxy["cmdline_b64"] = base64.b64encode(raw).decode("ascii")
                    mavproxy["cmdline_sha256"] = hashlib.sha256(raw).hexdigest()
                    mavproxy["arguments"] = " ".join(argv)

            rewrite_raw_and_rehash(run_dir, mutate)
            result = five_uav_health_status(run_dir)
            self.assertEqual(result["status"], "failed")
            self.assertIn("master endpoints", "\n".join(result["details"]["failures"]))

    def test_raw_pose_out_of_bounds_cannot_be_hidden_by_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "raw_pose_out_of_bounds"
            write_complete_health_evidence(run_dir)

            def mutate(records: list[dict]) -> None:
                pose = next(
                    record
                    for record in records
                    if record.get("event") == "mavlink_global_position"
                )
                pose["relative_alt_m"] = 500.0

            rewrite_raw_and_rehash(run_dir, mutate)
            result = five_uav_health_status(run_dir)
            self.assertEqual(result["status"], "failed")
            self.assertIn("raw MAVLink pose", "\n".join(result["details"]["failures"]))

    def test_terrain_module_cannot_be_reintroduced(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "terrain_module"
            write_complete_health_evidence(run_dir)

            def mutate(records: list[dict]) -> None:
                for sample in (
                    record for record in records if record.get("event") == "process_sample"
                ):
                    for process in sample["processes"]:
                        if process.get("role") != "mavproxy":
                            continue
                        argv = process["cmdline"]
                        modules_index = argv.index("--default-modules") + 1
                        argv[modules_index] += ",terrain"
                        raw = b"\0".join(value.encode() for value in argv) + b"\0"
                        process["cmdline_b64"] = base64.b64encode(raw).decode("ascii")
                        process["cmdline_sha256"] = hashlib.sha256(raw).hexdigest()
                        process["arguments"] = " ".join(argv)

            rewrite_raw_and_rehash(run_dir, mutate)
            result = five_uav_health_status(run_dir)
            self.assertEqual(result["status"], "failed")
            self.assertIn("offline module set", "\n".join(result["details"]["failures"]))

    def test_readiness_decision_requires_supporting_raw_streams(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "unsupported_readiness"
            write_complete_health_evidence(run_dir)

            def mutate(records: list[dict]) -> None:
                records[:] = [
                    record
                    for record in records
                    if not (
                        record.get("event") == "readiness_heartbeat"
                        and record.get("system_id") == 5
                    )
                ]
                for sequence, record in enumerate(records, start=1):
                    record["event_seq"] = sequence

            rewrite_raw_and_rehash(run_dir, mutate)
            result = five_uav_health_status(run_dir)
            self.assertEqual(result["status"], "failed")
            self.assertIn("readiness heartbeat", "\n".join(result["details"]["failures"]))

    def test_clock_correlation_labels_are_complete_and_bound(self) -> None:
        self.assertEqual(
            utc_from_epoch_seconds(1.125),
            "1970-01-01T00:00:01.125000Z",
        )
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "clock_mutation"
            write_complete_health_evidence(run_dir)

            def mutate(records: list[dict]) -> None:
                clock_events = [
                    record for record in records if record.get("event") == "clock_correlation"
                ]
                self.assertEqual(len(clock_events), 2)
                clock_events[-1]["correlation_point"] = "unbound_clock"

            rewrite_raw_and_rehash(run_dir, mutate)
            result = five_uav_health_status(run_dir)
            self.assertEqual(result["status"], "failed")
            self.assertIn("clock", "\n".join(result["details"]["failures"]))


if __name__ == "__main__":
    unittest.main()
