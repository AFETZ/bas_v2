#!/usr/bin/env python3
"""Validate and render Day 4 MAVLink endpoint bridge configuration.

This module does not route MAVLink itself. It generates configuration for
`mavlink-routerd` and the local opaque UDP queue adapter so MAVLink routing
stays with an upstream tool.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - exercised by shell preflight instead.
    yaml = None


ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_ENDPOINTS = ROOT_DIR / "network" / "config" / "endpoints.yaml"
DEFAULT_SERVICE_TIERS = ROOT_DIR / "network" / "config" / "service_tiers.yaml"
TRAFFIC_CLASSES = ("control", "payload", "additional_data")
PORT_KEYS = {
    "control": "control_udp",
    "payload": "payload_udp",
    "additional_data": "additional_data_udp",
}
NS3_INGRESS_KEYS = {
    "control": "control_ingress_udp",
    "payload": "payload_ingress_udp",
    "additional_data": "additional_data_ingress_udp",
}
NS3_EGRESS_KEYS = {
    "control": "control_egress_udp",
    "payload": "payload_egress_udp",
    "additional_data": "additional_data_egress_udp",
}


class ConfigError(ValueError):
    """Raised when the bridge endpoint contract is internally inconsistent."""


def load_yaml(path: Path) -> dict[str, Any]:
    if yaml is None:
        raise ConfigError("PyYAML is required: python3 -m pip install PyYAML")
    try:
        return yaml.safe_load(path.read_text()) or {}
    except FileNotFoundError as exc:
        raise ConfigError(f"missing config file: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc


def normalize_mavlink_endpoint(value: str) -> tuple[str, str, int] | None:
    match = re.match(r"^(?P<scheme>udp|udpin|udpout|tcp|tcpin|tcpout):(?P<host>[^:]+):(?P<port>[0-9]+)$", value)
    if not match:
        return None
    return (
        match.group("scheme").lower(),
        match.group("host").lower(),
        int(match.group("port")),
    )


def forbidden_direct_endpoints(config: dict[str, Any]) -> set[tuple[str, str, int]]:
    forbidden: set[tuple[str, str, int]] = set()
    for endpoint in config.get("no_bypass", {}).get("forbidden_direct_endpoints", []):
        protocol = str(endpoint.get("protocol", "")).lower()
        host = str(endpoint.get("host", "")).lower()
        try:
            port = int(endpoint["port"])
        except (KeyError, TypeError, ValueError):
            continue
        forbidden.add((protocol, host, port))
    return forbidden


def is_forbidden_mavlink_connection(value: str, forbidden: set[tuple[str, str, int]]) -> bool:
    parsed = normalize_mavlink_endpoint(value)
    if parsed is None:
        return False
    scheme, host, port = parsed
    protocol = "tcp" if scheme.startswith("tcp") else "udp"
    hosts = {host}
    if host == "localhost":
        hosts.add("127.0.0.1")
    return any((protocol, item_host, port) in forbidden for item_host in hosts)


def validate(config: dict[str, Any], service_tiers: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    uavs = config.get("uavs", [])
    bridge = config.get("bridge", {})

    if len(uavs) != 5:
        errors.append(f"expected 5 UAV endpoint mappings, found {len(uavs)}")

    names = [uav.get("name") for uav in uavs]
    system_ids = [uav.get("system_id") for uav in uavs]
    if names != [f"uav{i}" for i in range(1, 6)]:
        errors.append(f"expected ordered UAV names uav1..uav5, found {names}")
    if system_ids != [1, 2, 3, 4, 5]:
        errors.append(f"expected MAVLink system IDs 1..5, found {system_ids}")

    bridge_default = str(bridge.get("ground_control", {}).get("default_connection", ""))
    forbidden = forbidden_direct_endpoints(config)
    if not bridge_default:
        errors.append("bridge.ground_control.default_connection is required")
    elif is_forbidden_mavlink_connection(bridge_default, forbidden):
        errors.append(f"bridge default endpoint is forbidden: {bridge_default}")

    class_policy = service_tiers.get("traffic_class_policy", {})
    queue_policy = bridge.get("queues", {})
    pcap_hooks = bridge.get("pcap_hooks", {})
    priorities: list[int] = []

    for traffic_class in TRAFFIC_CLASSES:
        if traffic_class not in config.get("traffic_classes", {}):
            errors.append(f"missing endpoints traffic class: {traffic_class}")
        if traffic_class not in class_policy:
            errors.append(f"missing service tier policy: {traffic_class}")
        queue = queue_policy.get(traffic_class, {})
        if not queue:
            errors.append(f"missing bridge queue policy: {traffic_class}")
        else:
            try:
                priorities.append(int(queue["priority"]))
                if int(queue["max_packets"]) <= 0:
                    errors.append(f"{traffic_class} max_packets must be positive")
                if int(queue["byte_pacing_bps"]) <= 0:
                    errors.append(f"{traffic_class} byte_pacing_bps must be positive")
            except (KeyError, TypeError, ValueError):
                errors.append(f"{traffic_class} queue policy has invalid numeric fields")
        if traffic_class not in pcap_hooks:
            errors.append(f"missing PCAP hook: {traffic_class}")

    if priorities != sorted(priorities):
        errors.append(f"traffic class priorities must be ordered control, payload, additional_data; found {priorities}")
    if len(set(priorities)) != len(priorities):
        errors.append(f"traffic class priorities must be unique; found {priorities}")

    seen_ports: dict[int, str] = {}
    for uav in uavs:
        name = str(uav.get("name"))
        for traffic_class in TRAFFIC_CLASSES:
            bridge_port = uav.get("bridge_ports", {}).get(PORT_KEYS[traffic_class])
            ingress_port = uav.get("ns3_ports", {}).get(NS3_INGRESS_KEYS[traffic_class])
            egress_port = uav.get("ns3_ports", {}).get(NS3_EGRESS_KEYS[traffic_class])
            for label, port in (
                (f"{name}.{traffic_class}.bridge", bridge_port),
                (f"{name}.{traffic_class}.ns3_ingress", ingress_port),
                (f"{name}.{traffic_class}.ns3_egress", egress_port),
            ):
                try:
                    port_int = int(port)
                except (TypeError, ValueError):
                    errors.append(f"missing or invalid UDP port for {label}: {port}")
                    continue
                previous = seen_ports.get(port_int)
                if previous:
                    errors.append(f"UDP port collision: {label} and {previous} both use {port_int}")
                seen_ports[port_int] = label

    return errors


def build_routes(config: dict[str, Any]) -> list[dict[str, Any]]:
    bridge = config["bridge"]
    routes: list[dict[str, Any]] = []
    for uav in config.get("uavs", []):
        for traffic_class in TRAFFIC_CLASSES:
            routes.append(
                {
                    "uav": uav["name"],
                    "system_id": uav["system_id"],
                    "traffic_class": traffic_class,
                    "priority": bridge["queues"][traffic_class]["priority"],
                    "endpoint_bind": {
                        "host": bridge["ground_control"]["bind_host"],
                        "port": uav["bridge_ports"][PORT_KEYS[traffic_class]],
                    },
                    "ns3_ingress": {
                        "host": bridge["ns3"]["ingress_host"],
                        "port": uav["ns3_ports"][NS3_INGRESS_KEYS[traffic_class]],
                    },
                    "ns3_egress": {
                        "host": bridge["ns3"]["uav_egress_host"],
                        "port": uav["ns3_ports"][NS3_EGRESS_KEYS[traffic_class]],
                    },
                    "direct_sitl": uav["direct_sitl"],
                }
            )
    return routes


def render_ground_router(config: dict[str, Any]) -> str:
    bridge = config["bridge"]
    lines = [
        "# Generated by network/bridge/bridge_config.py",
        "# Ground-side MAVLink router. Run inside the ground namespace.",
        "[General]",
        "TcpServerPort = 0",
        "ReportStats = true",
        "MavlinkDialect = ardupilotmega",
        "",
    ]
    for traffic_class in ("control", "payload"):
        local_port = bridge["ground_control"]["ports"][PORT_KEYS[traffic_class]]
        lines.extend(
            [
                f"[UdpEndpoint gcs_{traffic_class}_local]",
                "Mode = Server",
                f"Address = {bridge['ground_control']['bind_host']}",
                f"Port = {local_port}",
                "",
            ]
        )
        for uav in config["uavs"]:
            ns3_port = uav["ns3_ports"][NS3_INGRESS_KEYS[traffic_class]]
            lines.extend(
                [
                    f"[UdpEndpoint {uav['name']}_{traffic_class}_to_ns3]",
                    "Mode = Normal",
                    f"Address = {bridge['ns3']['ingress_host']}",
                    f"Port = {ns3_port}",
                    f"Group = {uav['name']}_{traffic_class}",
                    "",
                ]
            )
    return "\n".join(lines).rstrip() + "\n"


def render_uav_router(config: dict[str, Any], uav: dict[str, Any]) -> str:
    lines = [
        "# Generated by network/bridge/bridge_config.py",
        f"# UAV-side MAVLink router for {uav['name']} sysid={uav['system_id']}.",
        "# Run inside the UAV namespace; the SITL localhost port is forbidden on the ground side only.",
        "[General]",
        "TcpServerPort = 0",
        "ReportStats = true",
        "MavlinkDialect = ardupilotmega",
        f"LogSystemId = {uav['system_id']}",
        "",
        f"[TcpEndpoint {uav['name']}_sitl_master]",
        f"Address = {uav['direct_sitl']['master_tcp']['host']}",
        f"Port = {uav['direct_sitl']['master_tcp']['port']}",
        "",
    ]
    for traffic_class in ("control", "payload"):
        egress_port = uav["ns3_ports"][NS3_EGRESS_KEYS[traffic_class]]
        lines.extend(
            [
                f"[UdpEndpoint {uav['name']}_{traffic_class}_from_ns3]",
                "Mode = Server",
                "Address = 0.0.0.0",
                f"Port = {egress_port}",
                f"Group = {uav['name']}_{traffic_class}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def render_pcap_manifest(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "note": "Use these hooks on the ns-3/TAP-facing interfaces; they are proof points for the P0 packet-path gate.",
        "traffic_classes": config["bridge"]["pcap_hooks"],
    }


def render(run_dir: Path, config: dict[str, Any]) -> list[Path]:
    bridge_dir = run_dir / "bridge"
    router_dir = bridge_dir / "mavlink-router"
    router_dir.mkdir(parents=True, exist_ok=True)

    written = []
    topology_path = bridge_dir / "topology.json"
    topology_path.write_text(json.dumps({"routes": build_routes(config)}, indent=2) + "\n")
    written.append(topology_path)

    pcap_path = bridge_dir / "pcap_manifest.json"
    pcap_path.write_text(json.dumps(render_pcap_manifest(config), indent=2) + "\n")
    written.append(pcap_path)

    ground_path = router_dir / "ground.conf"
    ground_path.write_text(render_ground_router(config))
    written.append(ground_path)

    for uav in config.get("uavs", []):
        uav_path = router_dir / f"{uav['name']}.conf"
        uav_path.write_text(render_uav_router(config, uav))
        written.append(uav_path)

    return written


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoints", type=Path, default=DEFAULT_ENDPOINTS)
    parser.add_argument("--service-tiers", type=Path, default=DEFAULT_SERVICE_TIERS)
    parser.add_argument("--run-dir", type=Path, default=ROOT_DIR / "runs" / "bridge_dry_run")
    parser.add_argument("--check", action="store_true", help="Validate only.")
    parser.add_argument("--render", action="store_true", help="Render topology, PCAP, and mavlink-router configs.")
    parser.add_argument("--print-move-drone-endpoint", action="store_true", help="Print the safe default GCS endpoint.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        endpoints = load_yaml(args.endpoints)
        service_tiers = load_yaml(args.service_tiers)
        errors = validate(endpoints, service_tiers)
        if errors:
            for error in errors:
                print(f"FAIL {error}", file=sys.stderr)
            return 1

        if args.print_move_drone_endpoint:
            print(endpoints["bridge"]["ground_control"]["default_connection"])

        if args.render:
            written = render(args.run_dir, endpoints)
            for path in written:
                print(path)
        elif args.check:
            print("PASS bridge endpoint config validates")
    except ConfigError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
