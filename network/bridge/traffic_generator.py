#!/usr/bin/env python3
"""Generate UDP load for Day 4 bridge traffic classes.

The generator is intentionally transport-level. MAVLink payload/control routing
is handled by `mavlink-routerd`; this tool supplies repeatable payload and
additional-data pressure against the configured bridge ports.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - covered by dependency checks.
    yaml = None


ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_ENDPOINTS = ROOT_DIR / "network" / "config" / "endpoints.yaml"
TRAFFIC_CLASSES = ("control", "payload", "additional_data")
PORT_KEYS = {
    "control": "control_udp",
    "payload": "payload_udp",
    "additional_data": "additional_data_udp",
}


def load_yaml(path: Path) -> dict[str, Any]:
    if yaml is None:
        raise RuntimeError("PyYAML is required: python3 -m pip install PyYAML")
    return yaml.safe_load(path.read_text()) or {}


def resolve_target(config: dict[str, Any], uav_name: str, traffic_class: str) -> tuple[str, int, int]:
    for uav in config.get("uavs", []):
        if uav.get("name") == uav_name:
            host = config["bridge"]["ground_control"]["bind_host"]
            port = int(uav["bridge_ports"][PORT_KEYS[traffic_class]])
            return host, port, int(uav["system_id"])
    raise ValueError(f"unknown UAV {uav_name!r}")


def make_payload(traffic_class: str, uav: str, sequence: int, size: int) -> bytes:
    header = json.dumps(
        {
            "type": "bridge_load",
            "traffic_class": traffic_class,
            "uav": uav,
            "sequence": sequence,
            "time_s": time.time(),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    if len(header) >= size:
        return header[:size]
    return header + b"\n" + bytes(size - len(header) - 1)


def generate(
    target: tuple[str, int],
    uav: str,
    traffic_class: str,
    rate_bps: int,
    duration_s: float,
    payload_size: int,
) -> int:
    interval_s = (payload_size * 8.0) / float(rate_bps)
    deadline = time.monotonic() + duration_s
    sent = 0
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        while time.monotonic() < deadline:
            sent += 1
            payload = make_payload(traffic_class, uav, sent, payload_size)
            sock.sendto(payload, target)
            print(
                json.dumps(
                    {
                        "event": "send",
                        "traffic_class": traffic_class,
                        "uav": uav,
                        "sequence": sent,
                        "bytes": len(payload),
                        "target": f"{target[0]}:{target[1]}",
                        "time_s": time.time(),
                    },
                    sort_keys=True,
                )
            )
            time.sleep(interval_s)
    return sent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoints", type=Path, default=DEFAULT_ENDPOINTS)
    parser.add_argument("--uav", default="uav1")
    parser.add_argument("--traffic-class", choices=TRAFFIC_CLASSES, default="payload")
    parser.add_argument("--rate-bps", type=int, default=100000)
    parser.add_argument("--duration-s", type=float, default=5.0)
    parser.add_argument("--payload-size", type=int, default=200)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        config = load_yaml(args.endpoints)
        host, port, system_id = resolve_target(config, args.uav, args.traffic_class)
    except Exception as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 2

    if args.rate_bps <= 0:
        print("FAIL --rate-bps must be positive", file=sys.stderr)
        return 2
    if args.payload_size <= 0:
        print("FAIL --payload-size must be positive", file=sys.stderr)
        return 2

    if args.dry_run:
        print(
            json.dumps(
                {
                    "traffic_class": args.traffic_class,
                    "uav": args.uav,
                    "system_id": system_id,
                    "target": f"{host}:{port}",
                    "rate_bps": args.rate_bps,
                    "duration_s": args.duration_s,
                    "payload_size": args.payload_size,
                },
                sort_keys=True,
            )
        )
        return 0

    sent = generate((host, port), args.uav, args.traffic_class, args.rate_bps, args.duration_s, args.payload_size)
    print(json.dumps({"event": "complete", "packets_sent": sent}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
