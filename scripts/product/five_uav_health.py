#!/usr/bin/env python3
"""Write a compact health summary for the live five-UAV baseline."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import yaml
from pymavlink import mavutil


def process_records(executable: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for cmdline_path in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            argv = [
                item.decode(errors="replace")
                for item in cmdline_path.read_bytes().split(b"\0")
                if item
            ]
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if not any(Path(item).name == executable for item in argv):
            continue
        records.append({"pid": int(cmdline_path.parent.name), "argv": argv})
    return records


def option(argv: list[str], name: str) -> str | None:
    prefix = name + "="
    for index, item in enumerate(argv):
        if item == name and index + 1 < len(argv):
            return argv[index + 1]
        if item.startswith(prefix):
            return item[len(prefix) :]
    return None


def local_ports(path: str, state: str | None = None) -> dict[int, int]:
    counts: dict[int, int] = {}
    try:
        lines = Path(path).read_text(encoding="ascii").splitlines()[1:]
    except FileNotFoundError:
        return counts
    for line in lines:
        fields = line.split()
        if state is not None and fields[3] != state:
            continue
        port = int(fields[1].rsplit(":", 1)[1], 16)
        counts[port] = counts.get(port, 0) + 1
    return counts


def gazebo_models() -> list[str]:
    try:
        result = subprocess.run(
            ["gz", "model", "--list"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    return sorted(
        match.group(1)
        for line in result.stdout.splitlines()
        if (match := re.match(r"\s*-\s+(\S+)\s*$", line))
    )


def load_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def write_summary(path: Path, summary: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", type=Path, required=True)
    parser.add_argument("--tracker-state", type=Path, required=True)
    parser.add_argument("--tracker-events", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-s", type=float, default=120.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    scenario = yaml.safe_load(args.scenario.read_text(encoding="utf-8"))
    robots = scenario["robots"]
    names = [robot["name"] for robot in robots]
    expected_sysids = {robot["name"]: int(robot["system_id"]) for robot in robots}
    mavlink_ports = {
        robot["name"]: int(str(robot["mavproxy_out"]).rsplit(":", 1)[1])
        for robot in robots
    }
    connections: dict[str, Any] = {}
    bind_errors: list[str] = []
    for name, port in mavlink_ports.items():
        try:
            connections[name] = mavutil.mavlink_connection(
                f"udpin:127.0.0.1:{port}", autoreconnect=False
            )
        except OSError as exc:
            bind_errors.append(f"{name} MAVLink {port}: {exc}")

    heartbeats = {name: 0 for name in names}
    observed_sysids: dict[str, set[int]] = {name: set() for name in names}
    odometry_samples = {name: 0 for name in names}
    last_positions: dict[str, list[float]] = {}
    last_tracker_mtime = -1
    tracker_state: dict[str, Any] | None = None
    models: list[str] = []
    last_model_probe = 0.0
    started = time.monotonic()
    ready = False

    while time.monotonic() - started < args.timeout_s:
        for name, connection in connections.items():
            while (message := connection.recv_match(blocking=False)) is not None:
                if message.get_type() == "HEARTBEAT":
                    heartbeats[name] += 1
                    observed_sysids[name].add(int(message.get_srcSystem()))

        try:
            tracker_mtime = args.tracker_state.stat().st_mtime_ns
        except FileNotFoundError:
            tracker_mtime = -1
        if tracker_mtime != -1 and tracker_mtime != last_tracker_mtime:
            last_tracker_mtime = tracker_mtime
            tracker_state = load_json(args.tracker_state)
            if tracker_state and tracker_state.get("source") == "ros_odometry":
                nodes = {node.get("id"): node for node in tracker_state.get("nodes", [])}
                for name in names:
                    node = nodes.get(name, {})
                    if node.get("source_topic") == f"/{name}/odometry" and not node.get("stale", True):
                        odometry_samples[name] += 1
                        last_positions[name] = node.get("position_m", [])

        if time.monotonic() - last_model_probe >= 1.0:
            models = gazebo_models()
            last_model_probe = time.monotonic()

        sitl = []
        for record in process_records("arducopter"):
            sysid = option(record["argv"], "--sysid")
            instance = option(record["argv"], "--instance")
            if sysid is not None and instance is not None:
                sitl.append(
                    {"pid": record["pid"], "system_id": int(sysid), "instance": int(instance)}
                )
        sitl.sort(key=lambda item: item["instance"])

        mavproxy = []
        for record in process_records("mavproxy.py"):
            out = option(record["argv"], "--out")
            master = option(record["argv"], "--master")
            sitl_endpoint = option(record["argv"], "--sitl")
            if out and master and sitl_endpoint:
                mavproxy.append(
                    {"pid": record["pid"], "out": out, "master": master, "sitl": sitl_endpoint}
                )
        mavproxy.sort(key=lambda item: int(item["out"].rsplit(":", 1)[1]))

        udp_counts = local_ports("/proc/net/udp")
        tcp_listeners = local_ports("/proc/net/tcp", state="0A")
        expected_udp = {
            "dds": [int(robot["dds_udp_port"]) for robot in robots],
            "sitl": [int(robot["sitl_udp_port"]) for robot in robots],
            "fdm": [int(robot["fdm_udp_port"]) for robot in robots],
            "mavlink": list(mavlink_ports.values()),
        }
        expected_tcp = [int(robot["master_tcp_port"]) for robot in robots]
        port_errors = [
            f"udp:{kind}:{port}:listeners={udp_counts.get(port, 0)}"
            for kind, ports in expected_udp.items()
            for port in ports
            if udp_counts.get(port, 0) != 1
        ] + [
            f"tcp:master:{port}:listeners={tcp_listeners.get(port, 0)}"
            for port in expected_tcp
            if tcp_listeners.get(port, 0) != 1
        ]

        command_post = scenario["command_post"]
        command_post_model = command_post.get("gazebo_model_name", "command_post")
        tracker_nodes = {
            node.get("id"): node for node in (tracker_state or {}).get("nodes", [])
        }
        tracked_cp = tracker_nodes.get(command_post["id"], {})
        cp_ok = (
            command_post_model in models
            and tracked_cp.get("position_m") == command_post["position_m"]
            and not tracked_cp.get("stale", True)
        )
        expected_mavproxy = sorted(
            (
                str(robot["mavproxy_out"]),
                f"tcp:127.0.0.1:{robot['master_tcp_port']}",
                f"127.0.0.1:{robot['sitl_udp_port']}",
            )
            for robot in robots
        )
        actual_mavproxy = sorted(
            (item["out"], item["master"], item["sitl"]) for item in mavproxy
        )

        ready = all(
            (
                not bind_errors,
                [item["system_id"] for item in sitl] == list(expected_sysids.values()),
                [item["instance"] for item in sitl] == list(range(5)),
                actual_mavproxy == expected_mavproxy,
                all(heartbeats[name] >= 2 for name in names),
                all(observed_sysids[name] == {expected_sysids[name]} for name in names),
                all(name in models for name in names),
                all(odometry_samples[name] >= 5 for name in names),
                tracker_state is not None,
                not (tracker_state or {}).get("missing_nodes", names),
                not (tracker_state or {}).get("stale_nodes", names),
                cp_ok,
                not port_errors,
            )
        )
        if ready:
            break
        time.sleep(0.1)

    for connection in connections.values():
        connection.close()

    summary = {
        "status": "healthy" if ready else "unhealthy",
        "elapsed_s": round(time.monotonic() - started, 3),
        "sitl": sitl,
        "mavproxy": mavproxy,
        "mavlink": {
            name: {
                "endpoint": f"127.0.0.1:{mavlink_ports[name]}",
                "heartbeat_count": heartbeats[name],
                "observed_system_ids": sorted(observed_sysids[name]),
            }
            for name in names
        },
        "gazebo_models": models,
        "odometry": {
            name: {"samples": odometry_samples[name], "last_position_m": last_positions.get(name)}
            for name in names
        },
        "command_post": {
            "model": scenario["command_post"].get("gazebo_model_name", "command_post"),
            "position_m": scenario["command_post"]["position_m"],
            "surface_position_confirmed": cp_ok,
        },
        "ports": {
            "udp": expected_udp,
            "tcp_master": expected_tcp,
            "collisions_or_missing": port_errors,
        },
        "errors": bind_errors + ([] if ready else ["baseline did not become healthy before timeout"]),
    }
    write_summary(args.output, summary)
    print(
        f"BASE HEALTH {summary['status']}: sitl={len(sitl)} "
        f"models={sum(name in models for name in names)}/5 "
        f"odometry={sum(odometry_samples[name] >= 5 for name in names)}/5 "
        f"summary={args.output}"
    )
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
