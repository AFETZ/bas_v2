#!/usr/bin/env python3
"""Collect the factual live five-UAV dual-UART topology without provenance metadata."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any


CLASSES = ("control", "payload")
UAV_IDS = tuple(range(1, 6))


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    result: list[dict[str, Any]] = []
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            result.append(item)
    return result


def socket_snapshot() -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for namespace in ("ams-gcs", "ams-ns3", *(f"ams-uav{index}" for index in UAV_IDS)):
        command = ["ip", "netns", "exec", namespace, "ss", "-H", "-anup"]
        completed = subprocess.run(
            command, text=True, capture_output=True, check=False, timeout=5
        )
        result[namespace] = [line for line in completed.stdout.splitlines() if line.strip()]
    return result


def pty_pair(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    devices = re.findall(r"PTY is (/dev/pts/\d+)", text)
    pid_match = re.search(r"socat\[(\d+)\]", text)
    return {
        "sitl_pty": devices[0] if len(devices) > 0 else None,
        "adapter_pty": devices[1] if len(devices) > 1 else None,
        "socat_pid": int(pid_match.group(1)) if pid_match else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    health = read_json(run_dir / "metrics/health.json")
    scenario = read_json(run_dir / "metrics/scenario_summary.json")
    ns3_ready = read_json(run_dir / "logs/ns3_packet_engine.ready")
    events = read_jsonl(run_dir / "logs/ns3_packet_events.jsonl")
    event_counts: Counter[tuple[str, str, str]] = Counter()
    for event in events:
        event_counts[
            (
                str(event.get("traffic_class")),
                str(event.get("directed_link")),
                str(event.get("event")),
            )
        ] += 1
    sitl_by_id = {
        int(item.get("system_id")): item
        for item in health.get("sitl", [])
        if isinstance(item, dict) and isinstance(item.get("system_id"), int)
    }
    diagnostics = scenario.get("dual_uart_diagnostics", {}).get("sequential", {})
    paths: list[dict[str, Any]] = []
    uavs: list[dict[str, Any]] = []
    for uav_id in UAV_IDS:
        diagnostic = diagnostics.get(f"uav{uav_id}", {})
        sitl = sitl_by_id.get(uav_id, {})
        uav_paths: list[str] = []
        for channel in CLASSES:
            adapter = read_json(run_dir / f"logs/{channel}_uart_uav{uav_id}.ready")
            counters = read_json(run_dir / f"metrics/{channel}_uart_uav{uav_id}.json")
            ptys = pty_pair(run_dir / f"logs/{channel}_socat_uav{uav_id}.log")
            serial_number = 1 if channel == "control" else 2
            link_down = f"cp>uav{uav_id}"
            link_up = f"uav{uav_id}>cp"
            path_id = f"uav{uav_id}:{channel}"
            uav_paths.append(path_id)
            paths.append(
                {
                    "id": path_id,
                    "uav": f"uav{uav_id}",
                    "channel": channel,
                    "ardupilot_serial": f"SERIAL{serial_number}",
                    "sitl_pty_link": adapter.get("tty", "").replace(
                        f"{channel}-adapter-{uav_id - 1}", f"{channel}-sitl-{uav_id - 1}"
                    ),
                    "sitl_pty": ptys.get("sitl_pty"),
                    "adapter_pty_link": adapter.get("tty"),
                    "adapter_pty": adapter.get("tty_realpath") or ptys.get("adapter_pty"),
                    "socat_pid": ptys.get("socat_pid"),
                    "adapter_pid": adapter.get("pid"),
                    "adapter_pid_live": Path(f"/proc/{adapter.get('pid', -1)}").is_dir(),
                    "baud_rate": adapter.get("baud_rate"),
                    "serial_protocol_parameter": diagnostic.get("parameters", {}).get(
                        f"SERIAL{serial_number}_PROTOCOL"
                    ),
                    "serial_baud_parameter": diagnostic.get("parameters", {}).get(
                        f"SERIAL{serial_number}_BAUD"
                    ),
                    "uav_endpoint": adapter.get("bind"),
                    "gcs_endpoint": adapter.get("peer"),
                    "transport_framing": adapter.get("transport_framing"),
                    "bytes": {
                        name: counters.get(name, 0)
                        for name in (
                            "uart_input_bytes",
                            "ns3_input_bytes",
                            "ns3_output_bytes",
                            "uart_output_bytes",
                        )
                    },
                    "mavlink_source_system_id": diagnostic.get(channel, {}).get(
                        "ack_from_system_id"
                    ),
                    "real_ack_latency_ms": diagnostic.get(channel, {}).get("ack_latency_ms"),
                    "ns3": {
                        "downlink_ingress": event_counts[(channel, link_down, "ingress")],
                        "downlink_egress": event_counts[(channel, link_down, "egress")],
                        "uplink_ingress": event_counts[(channel, link_up, "ingress")],
                        "uplink_egress": event_counts[(channel, link_up, "egress")],
                    },
                    "reverse_telemetry_path": [
                        f"SERIAL{serial_number}",
                        "PTY",
                        f"uav{uav_id} adapter",
                        "ns-3 shared CSMA medium",
                        f"GCS UDP {14600 if channel == 'control' else 14700}",
                    ],
                }
            )
        uavs.append(
            {
                "id": f"uav{uav_id}",
                "instance": sitl.get("instance"),
                "system_id": sitl.get("system_id"),
                "ardupilot_pid": sitl.get("pid"),
                "ardupilot_pid_live": Path(f"/proc/{sitl.get('pid', -1)}").is_dir(),
                "serial0": {
                    "protocol": diagnostic.get("parameters", {}).get("SERIAL0_PROTOCOL"),
                    "baud_parameter": diagnostic.get("parameters", {}).get("SERIAL0_BAUD"),
                    "product_transport": "none",
                },
                "uart_paths": uav_paths,
            }
        )
    sockets = socket_snapshot()
    gcs_text = "\n".join(sockets.get("ams-gcs", []))
    output = {
        "run_id": run_dir.name,
        "scene": "Town01",
        "captured_while_runtime_active": True,
        "ns3": {
            "pid": ns3_ready.get("pid"),
            "pid_live": Path(f"/proc/{ns3_ready.get('pid', -1)}").is_dir(),
            "uav_count": ns3_ready.get("uav_count"),
            "medium": "single shared CSMA engineering surrogate",
            "tap_endpoints": ["tap-gcs", *(f"tap-uav{index}" for index in UAV_IDS)],
        },
        "uavs": uavs,
        "uart_paths": paths,
        "uart_path_count": len(paths),
        "all_uart_paths_independent": len(paths) == 10
        and len({item.get("adapter_pty") for item in paths}) == 10
        and len({item.get("uav_endpoint") for item in paths}) == 10,
        "sockets": sockets,
        "gcs_direct_sitl_ports_present": any(
            f":{port}" in gcs_text for port in (14550, 5760, 5770, 5780, 5790, 5800)
        ),
    }
    destination = run_dir / "metrics/runtime_topology.json"
    destination.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(output, sort_keys=True))
    return 0 if output["all_uart_paths_independent"] and not output["gcs_direct_sitl_ports_present"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
