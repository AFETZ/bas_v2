#!/usr/bin/env python3
"""Resolve and validate the v3 endpoint transaction matrix.

The tracked matrix is a build input, not runtime evidence.  This module derives
it deterministically from the authoritative endpoint, five-UAV scenario, and
radio-class configuration, then validates both the complete M3 matrix and the
M2 control-only subset without trusting labels supplied by a producer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml


ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_ENDPOINTS = ROOT_DIR / "network/config/endpoints.yaml"
DEFAULT_SCENARIO = ROOT_DIR / "network/config/scenario_5uav.yaml"
DEFAULT_RADIO = ROOT_DIR / "network/config/radio_24ghz.yaml"
DEFAULT_SCHEMA = ROOT_DIR / "network/config/endpoint_transaction_schema.json"
DEFAULT_MATRIX = ROOT_DIR / "network/config/endpoint_matrix_5uav.json"

CONTRACT = "endpoint_transaction_schema=1"
MATRIX_ID = "ams.endpoint_matrix.5uav/v1"
M2_PROFILE = "m2_one_uav_control"
M3_PROFILE = "m3_full"
TRAFFIC_CLASSES = ("control", "payload", "additional_data")
DIRECTIONS = ("downlink", "uplink")
HASH_DOMAINS = (
    "mavlink_frame_sha256",
    "application_unit_sha256",
    "transport_payload_sha256",
    "wire_frame_sha256",
)
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
PROTOCOL_FAMILY = {
    ("control", "downlink"): "STATUSTEXT_MARKER_PLUS_COMMAND_LONG",
    ("control", "uplink"): "COMMAND_ACK_HEARTBEAT_REQUESTED_TELEMETRY",
    ("payload", "downlink"): "MAVLINK_TUNNEL_V2",
    ("payload", "uplink"): "MAVLINK_TUNNEL_V2",
    ("additional_data", "downlink"): "AMS_ADDITIONAL_DATA_V1",
    ("additional_data", "uplink"): "AMS_ADDITIONAL_DATA_V1",
}
RESPONSE_KIND = {
    ("control", "downlink"): "command_ack_and_requested_telemetry",
    ("control", "uplink"): "correlated_delivery_at_command_post",
    ("payload", "downlink"): "decoded_tunnel_delivery_at_uav_companion",
    ("payload", "uplink"): "decoded_tunnel_delivery_at_command_post",
    ("additional_data", "downlink"): "decoded_record_delivery_at_uav",
    ("additional_data", "uplink"): "decoded_record_delivery_at_command_post",
}
TOP_LEVEL_KEYS = {
    "schema_version",
    "contract",
    "matrix_id",
    "source_configs",
    "traffic_classes",
    "directions",
    "profiles",
    "resolved_cells_sha256",
    "cells",
}
CELL_KEYS = {
    "cell_id",
    "uav",
    "traffic_class",
    "direction",
    "protocol",
    "source",
    "destination",
    "transport_ports",
    "ns3_path",
    "capture_points",
    "identity",
    "outcome",
}
SAFE_ID = re.compile(r"[a-z0-9][a-z0-9_.>/=-]*")


class MatrixError(ValueError):
    """The endpoint matrix or one of its source configurations is invalid."""


class UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader which rejects duplicate mapping keys."""


def _construct_mapping(
    loader: UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise MatrixError(f"duplicate YAML key: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def pretty_json(value: Any) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        indent=2,
        sort_keys=True,
    ) + "\n"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def load_yaml_mapping(path: Path) -> dict[str, Any]:
    try:
        loaded = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
    except (OSError, UnicodeError, yaml.YAMLError, MatrixError) as exc:
        raise MatrixError(f"cannot load strict YAML {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise MatrixError(f"YAML top level is not a mapping: {path}")
    return loaded


def load_strict_json(path: Path) -> dict[str, Any]:
    def reject_constant(value: str) -> None:
        raise MatrixError(f"non-standard JSON constant: {value}")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise MatrixError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        loaded = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicates,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, MatrixError) as exc:
        raise MatrixError(f"cannot load strict JSON {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise MatrixError(f"JSON top level is not an object: {path}")
    return loaded


def _source_record(path: Path) -> dict[str, Any]:
    try:
        relative = path.resolve(strict=True).relative_to(ROOT_DIR.resolve()).as_posix()
        payload = path.read_bytes()
    except (OSError, ValueError) as exc:
        raise MatrixError(f"source config is unavailable or outside repository: {path}") from exc
    return {"path": relative, "sha256": sha256_bytes(payload)}


def _require_int(value: Any, label: str, minimum: int = 0, maximum: int = 65535) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise MatrixError(f"{label} must be an integer in {minimum}..{maximum}")
    return value


def _expected_uavs(
    endpoints: dict[str, Any], scenario: dict[str, Any]
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    endpoint_uavs = endpoints.get("uavs")
    scenario_uavs = scenario.get("robots")
    if not isinstance(endpoint_uavs, list) or not isinstance(scenario_uavs, list):
        raise MatrixError("endpoints.uavs and scenario.robots must be lists")
    if len(endpoint_uavs) != 5 or len(scenario_uavs) != 5:
        raise MatrixError("endpoint matrix requires exactly five UAVs")
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for index, (endpoint, robot) in enumerate(zip(endpoint_uavs, scenario_uavs), start=1):
        if not isinstance(endpoint, dict) or not isinstance(robot, dict):
            raise MatrixError("UAV entries must be mappings")
        expected_name = f"uav{index}"
        if endpoint.get("name") != expected_name or robot.get("name") != expected_name:
            raise MatrixError(f"UAV order/name mismatch at {expected_name}")
        if endpoint.get("system_id") != index or robot.get("system_id") != index:
            raise MatrixError(f"system ID mismatch for {expected_name}")
        if endpoint.get("instance") != index - 1 or robot.get("instance") != index - 1:
            raise MatrixError(f"instance mismatch for {expected_name}")
        if endpoint.get("isolation_namespace") != f"ams-{expected_name}":
            raise MatrixError(f"isolation namespace mismatch for {expected_name}")
        pairs.append((endpoint, robot))
    return pairs


def _class_contract(
    endpoints: dict[str, Any], radio: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    endpoint_classes = endpoints.get("traffic_classes")
    tos = radio.get("priority_tos")
    if not isinstance(endpoint_classes, dict) or set(endpoint_classes) != set(TRAFFIC_CLASSES):
        raise MatrixError("endpoints traffic classes are not exactly the v3 three-class set")
    if not isinstance(tos, dict) or set(tos) != set(TRAFFIC_CLASSES):
        raise MatrixError("radio priority_tos is not exactly the v3 three-class set")
    result: dict[str, dict[str, Any]] = {}
    priorities: set[int] = set()
    dscp_values: set[int] = set()
    for traffic_class in TRAFFIC_CLASSES:
        record = endpoint_classes[traffic_class]
        if not isinstance(record, dict):
            raise MatrixError(f"traffic class {traffic_class} is not a mapping")
        priority = _require_int(record.get("priority"), f"{traffic_class}.priority", 0, 2)
        dscp_tos = _require_int(tos.get(traffic_class), f"{traffic_class}.dscp_tos", 0, 255)
        channel = record.get("channel")
        if not isinstance(channel, str) or SAFE_ID.fullmatch(channel) is None:
            raise MatrixError(f"traffic class {traffic_class} channel is invalid")
        priorities.add(priority)
        dscp_values.add(dscp_tos)
        result[traffic_class] = {
            "channel": channel,
            "priority": priority,
            "dscp_tos": dscp_tos,
        }
    if priorities != {0, 1, 2} or len(dscp_values) != 3:
        raise MatrixError("class priorities and DSCP/TOS values must be unique")
    return result


def _mavlink_identity(
    *, traffic_class: str, direction: str, system_id: int
) -> tuple[int | None, int | None, int | None, int | None]:
    if traffic_class == "additional_data":
        return None, None, None, None
    uav_component = 1 if traffic_class == "control" else 191
    if direction == "downlink":
        return 255, 190, system_id, uav_component
    return system_id, uav_component, 255, 190


def _cell(
    *,
    endpoint: dict[str, Any],
    command_post: dict[str, Any],
    traffic_class: str,
    direction: str,
    class_contract: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    name = str(endpoint["name"])
    system_id = int(endpoint["system_id"])
    namespace = str(endpoint["isolation_namespace"])
    cp_namespace = str(command_post["namespace"])
    cp_ip = str(command_post["node_ip"])
    uav_ip = str(endpoint["node_ip"])
    port_key = PORT_KEYS[traffic_class]
    ingress_key = NS3_INGRESS_KEYS[traffic_class]
    egress_key = NS3_EGRESS_KEYS[traffic_class]
    cp_port = int(command_post["ports"][f"{traffic_class}_udp_base"])
    uav_port = int(endpoint["bridge_ports"][port_key])
    ground_handoff = int(endpoint["ns3_ports"][ingress_key])
    uav_handoff = int(endpoint["ns3_ports"][egress_key])
    source_system, source_component, target_system, target_component = _mavlink_identity(
        traffic_class=traffic_class,
        direction=direction,
        system_id=system_id,
    )
    downlink = direction == "downlink"
    source = {
        "role": "command_post" if downlink else name,
        "namespace": cp_namespace if downlink else namespace,
        "interface": "eth0",
        "ip": cp_ip if downlink else uav_ip,
        "udp_port": cp_port if downlink else uav_port,
        "mavlink_system_id": source_system,
        "mavlink_component_id": source_component,
    }
    destination = {
        "role": name if downlink else "command_post",
        "namespace": namespace if downlink else cp_namespace,
        "interface": "eth0",
        "ip": uav_ip if downlink else cp_ip,
        "udp_port": uav_port if downlink else cp_port,
        "mavlink_system_id": target_system,
        "mavlink_component_id": target_component,
    }
    directed_link = f"cp>{name}" if downlink else f"{name}>cp"
    ingress_device = "cp.tap.ingress" if downlink else f"{name}.tap.ingress"
    egress_device = f"{name}.tap.egress" if downlink else "cp.tap.egress"
    # Control is not produced/consumed by a companion process on UAV eth0.
    # The actual endpoint is MAVProxy across the dedicated tail veth; the
    # radio-side adapter is the byte-opaque boundary between that tail and
    # the ns-3-facing UAV socket.  Payload/additional data remain companion
    # eth0 flows.
    uav_source_capture = (
        f"{name}.mavproxy.tail" if traffic_class == "control" else f"{name}.source.eth0"
    )
    uav_sink_capture = (
        f"{name}.mavproxy.tail" if traffic_class == "control" else f"{name}.sink.eth0"
    )
    capture_points = {
        "source_before_adapter": "cp.source.eth0" if downlink else uav_source_capture,
        "ns3_external_ingress": "ns3.cp.ingress" if downlink else f"ns3.{name}.ingress",
        "ns3_external_egress": f"ns3.{name}.egress" if downlink else "ns3.cp.egress",
        "remote_after_adapter": uav_sink_capture if downlink else "cp.sink.eth0",
    }
    class_values = class_contract[traffic_class]
    return {
        "cell_id": f"{name}.{traffic_class}.{direction}",
        "uav": {
            "name": name,
            "instance": int(endpoint["instance"]),
            "system_id": system_id,
        },
        "traffic_class": traffic_class,
        "direction": direction,
        "protocol": {
            "kind": "udp_framed" if traffic_class == "additional_data" else "mavlink_v2",
            "channel": class_values["channel"],
            "message_family": PROTOCOL_FAMILY[(traffic_class, direction)],
            "classification_source": "decoded_bytes_port_nonce",
        },
        "source": source,
        "destination": destination,
        "transport_ports": {
            "command_post_udp": cp_port,
            "uav_udp": uav_port,
            "ns3_ground_handoff_udp": ground_handoff,
            "ns3_uav_handoff_udp": uav_handoff,
        },
        "ns3_path": {
            "directed_link_id": directed_link,
            "ingress_device_id": ingress_device,
            "egress_device_id": egress_device,
            "queue_id": f"{directed_link}.{traffic_class}.q{class_values['priority']}",
            "priority": class_values["priority"],
            "dscp_tos": class_values["dscp_tos"],
            "ingress_udp_port": ground_handoff if downlink else uav_handoff,
            "egress_udp_port": uav_handoff if downlink else ground_handoff,
        },
        "capture_points": capture_points,
        "identity": {
            "nonce_domain": f"ams/v1/{name}/{traffic_class}/{direction}",
            "flow_id": f"{name}.{traffic_class}.{direction}",
            "sequence_policy": "strictly_monotonic_per_flow",
            "hash_domains": {
                domain: (traffic_class != "additional_data" if domain == "mavlink_frame_sha256" else True)
                for domain in HASH_DOMAINS
            },
        },
        "outcome": {
            "response_kind": RESPONSE_KIND[(traffic_class, direction)],
            "timeout_ms": 3000,
            "correlation": (
                "request_hash_command_source_target_window"
                if traffic_class == "control"
                else "run_nonce_flow_direction_sequence_payload_hash"
            ),
        },
    }


def _profiles(cells: list[dict[str, Any]]) -> dict[str, Any]:
    all_ids = [str(cell["cell_id"]) for cell in cells]
    m2_ids = [
        f"uav1.control.{direction}"
        for direction in DIRECTIONS
    ]
    return {
        M2_PROFILE: {
            "profile_id": M2_PROFILE,
            "expected_cell_count": 2,
            "cell_ids": m2_ids,
            "phase_contract": {
                "good": {"offered_exact": 10, "correlated_receive_exact": 10, "fresh_heartbeats_min": 3},
                "stopped": {"offered_exact": 5, "remote_receive_exact": 0, "loss_exact": 1.0},
                "recovery": {"offered_exact": 10, "correlated_receive_exact": 10, "fresh_heartbeats_min": 3},
            },
        },
        M3_PROFILE: {
            "profile_id": M3_PROFILE,
            "expected_cell_count": 30,
            "cell_ids": all_ids,
            "phase_contract": {
                "positive": {
                    "window_min_s": 30,
                    "offered_unique_per_cell_min": 20,
                    "delivery_ratio_per_cell_min": 0.95,
                },
                "stopped": {
                    "offered_unique_per_cell_min": 5,
                    "remote_receive_per_cell_exact": 0,
                    "loss_per_cell_exact": 1.0,
                },
                "point_to_multipoint": {
                    "traffic_class": "additional_data",
                    "direction": "downlink",
                    "root_nonce_domain": "ams/v1/p2mp/additional_data/downlink",
                    "root_records_min": 20,
                    "ns3_ingress_per_root_exact": 1,
                    "shared_medium_service_per_root_exact": 1,
                    "intended_receivers": [f"uav{index}" for index in range(1, 6)],
                    "delivery_ratio_per_receiver_min": 0.95,
                },
            },
        },
    }


def build_resolved_matrix(
    *,
    endpoints_path: Path = DEFAULT_ENDPOINTS,
    scenario_path: Path = DEFAULT_SCENARIO,
    radio_path: Path = DEFAULT_RADIO,
    schema_path: Path = DEFAULT_SCHEMA,
) -> dict[str, Any]:
    endpoints = load_yaml_mapping(endpoints_path)
    scenario = load_yaml_mapping(scenario_path)
    radio = load_yaml_mapping(radio_path)
    schema = load_strict_json(schema_path)
    if schema.get("$id") != "https://ams.local/schemas/endpoint-transaction-v1.json":
        raise MatrixError("endpoint transaction schema $id is not the v1 identity")
    contract_schema = schema.get("properties", {}).get("contract", {})
    if not isinstance(contract_schema, dict) or contract_schema.get("const") != CONTRACT:
        raise MatrixError("endpoint transaction schema does not freeze the v1 contract")
    pairs = _expected_uavs(endpoints, scenario)
    class_contract = _class_contract(endpoints, radio)
    command_post = endpoints.get("command_post")
    if not isinstance(command_post, dict):
        raise MatrixError("endpoints.command_post must be a mapping")
    if command_post.get("id") != "cp" or command_post.get("namespace") != "ams-gcs":
        raise MatrixError("command post identity must be cp in ams-gcs")
    cells = [
        _cell(
            endpoint=endpoint,
            command_post=command_post,
            traffic_class=traffic_class,
            direction=direction,
            class_contract=class_contract,
        )
        for endpoint, _robot in pairs
        for traffic_class in TRAFFIC_CLASSES
        for direction in DIRECTIONS
    ]
    return {
        "schema_version": 1,
        "contract": CONTRACT,
        "matrix_id": MATRIX_ID,
        "source_configs": {
            "schema": _source_record(schema_path),
            "endpoints": _source_record(endpoints_path),
            "scenario": _source_record(scenario_path),
            "radio": _source_record(radio_path),
        },
        "traffic_classes": list(TRAFFIC_CLASSES),
        "directions": list(DIRECTIONS),
        "profiles": _profiles(cells),
        "resolved_cells_sha256": sha256_bytes(canonical_json(cells)),
        "cells": cells,
    }


def _exact_keys(value: Any, expected: set[str], label: str, failures: list[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        failures.append(f"{label} is not an object")
        return {}
    keys = set(value)
    if keys != expected:
        failures.append(
            f"{label} keys are not exact: missing={sorted(expected - keys)} extra={sorted(keys - expected)}"
        )
    return value


def _valid_port(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and 1 <= value <= 65535


def validate_matrix_data(data: dict[str, Any]) -> list[str]:
    """Return all independently detected structural and semantic failures."""

    failures: list[str] = []
    _exact_keys(data, TOP_LEVEL_KEYS, "matrix", failures)
    if data.get("schema_version") != 1:
        failures.append("schema_version is not exactly 1")
    if data.get("contract") != CONTRACT:
        failures.append(f"contract is not {CONTRACT}")
    if data.get("matrix_id") != MATRIX_ID:
        failures.append(f"matrix_id is not {MATRIX_ID}")
    if data.get("traffic_classes") != list(TRAFFIC_CLASSES):
        failures.append("traffic classes are not exact or are not in canonical order")
    if data.get("directions") != list(DIRECTIONS):
        failures.append("directions are not exact or are not in canonical order")

    cells = data.get("cells")
    if not isinstance(cells, list):
        return failures + ["cells is not an array"]
    if len(cells) != 30:
        failures.append(f"matrix has {len(cells)} cells, expected exactly 30")

    expected_tuples = {
        (f"uav{index}", traffic_class, direction)
        for index in range(1, 6)
        for traffic_class in TRAFFIC_CLASSES
        for direction in DIRECTIONS
    }
    observed_tuples: list[tuple[Any, Any, Any]] = []
    cell_ids: list[str] = []
    nonce_domains: list[str] = []
    queue_ids: list[str] = []
    pair_allocations: dict[tuple[str, str], list[dict[str, Any]]] = {}

    endpoint_keys = {
        "role", "namespace", "interface", "ip", "udp_port",
        "mavlink_system_id", "mavlink_component_id",
    }
    for index, raw_cell in enumerate(cells):
        label = f"cells[{index}]"
        cell = _exact_keys(raw_cell, CELL_KEYS, label, failures)
        uav = _exact_keys(cell.get("uav"), {"name", "instance", "system_id"}, f"{label}.uav", failures)
        name = uav.get("name")
        traffic_class = cell.get("traffic_class")
        direction = cell.get("direction")
        observed_tuples.append((name, traffic_class, direction))
        expected_cell_id = f"{name}.{traffic_class}.{direction}"
        cell_id = cell.get("cell_id")
        if not isinstance(cell_id, str) or cell_id != expected_cell_id:
            failures.append(f"{label} cell_id does not match exact tuple")
        else:
            cell_ids.append(cell_id)
        expected_system = int(name[3:]) if isinstance(name, str) and re.fullmatch(r"uav[1-5]", name) else None
        if uav.get("system_id") != expected_system:
            failures.append(f"{label} has wrong MAVLink system ID for {name}")
        if expected_system is not None and uav.get("instance") != expected_system - 1:
            failures.append(f"{label} has wrong instance for {name}")
        if traffic_class not in TRAFFIC_CLASSES:
            failures.append(f"{label} has nonexact traffic class {traffic_class!r}")
        if direction not in DIRECTIONS:
            failures.append(f"{label} has nonexact direction {direction!r}")

        protocol = _exact_keys(
            cell.get("protocol"),
            {"kind", "channel", "message_family", "classification_source"},
            f"{label}.protocol",
            failures,
        )
        if protocol.get("classification_source") != "decoded_bytes_port_nonce":
            failures.append(f"{label} classification does not derive from bytes/port/nonce")
        source = _exact_keys(cell.get("source"), endpoint_keys, f"{label}.source", failures)
        destination = _exact_keys(cell.get("destination"), endpoint_keys, f"{label}.destination", failures)
        ports = _exact_keys(
            cell.get("transport_ports"),
            {"command_post_udp", "uav_udp", "ns3_ground_handoff_udp", "ns3_uav_handoff_udp"},
            f"{label}.transport_ports",
            failures,
        )
        if any(not _valid_port(value) for value in ports.values()):
            failures.append(f"{label} contains an invalid transport port")
        if direction == "downlink":
            if source.get("udp_port") != ports.get("command_post_udp") or destination.get("udp_port") != ports.get("uav_udp"):
                failures.append(f"{label} endpoint ports do not match downlink allocation")
            expected_source_system = None if traffic_class == "additional_data" else 255
            expected_target_system = None if traffic_class == "additional_data" else expected_system
        elif direction == "uplink":
            if source.get("udp_port") != ports.get("uav_udp") or destination.get("udp_port") != ports.get("command_post_udp"):
                failures.append(f"{label} endpoint ports do not match uplink allocation")
            expected_source_system = None if traffic_class == "additional_data" else expected_system
            expected_target_system = None if traffic_class == "additional_data" else 255
        else:
            expected_source_system = expected_target_system = None
        if source.get("mavlink_system_id") != expected_source_system:
            failures.append(f"{label} source MAVLink system ID is wrong")
        if destination.get("mavlink_system_id") != expected_target_system:
            failures.append(f"{label} destination MAVLink system ID is wrong")

        ns3_path = _exact_keys(
            cell.get("ns3_path"),
            {"directed_link_id", "ingress_device_id", "egress_device_id", "queue_id", "priority", "dscp_tos", "ingress_udp_port", "egress_udp_port"},
            f"{label}.ns3_path",
            failures,
        )
        queue_id = ns3_path.get("queue_id")
        if isinstance(queue_id, str):
            queue_ids.append(queue_id)
        if traffic_class in TRAFFIC_CLASSES and ns3_path.get("priority") != TRAFFIC_CLASSES.index(traffic_class):
            failures.append(f"{label} ns-3 priority does not match class")
        if direction == "downlink":
            expected_ingress = ports.get("ns3_ground_handoff_udp")
            expected_egress = ports.get("ns3_uav_handoff_udp")
        else:
            expected_ingress = ports.get("ns3_uav_handoff_udp")
            expected_egress = ports.get("ns3_ground_handoff_udp")
        if ns3_path.get("ingress_udp_port") != expected_ingress or ns3_path.get("egress_udp_port") != expected_egress:
            failures.append(f"{label} ns-3 stage ports do not match direction")

        capture_points = _exact_keys(
            cell.get("capture_points"),
            {"source_before_adapter", "ns3_external_ingress", "ns3_external_egress", "remote_after_adapter"},
            f"{label}.capture_points",
            failures,
        )
        if isinstance(name, str) and re.fullmatch(r"uav[1-5]", name):
            uav_source_capture = (
                f"{name}.mavproxy.tail"
                if traffic_class == "control"
                else f"{name}.source.eth0"
            )
            uav_sink_capture = (
                f"{name}.mavproxy.tail"
                if traffic_class == "control"
                else f"{name}.sink.eth0"
            )
            expected_capture_points = {
                "source_before_adapter": (
                    "cp.source.eth0" if direction == "downlink" else uav_source_capture
                ),
                "ns3_external_ingress": (
                    "ns3.cp.ingress" if direction == "downlink" else f"ns3.{name}.ingress"
                ),
                "ns3_external_egress": (
                    f"ns3.{name}.egress" if direction == "downlink" else "ns3.cp.egress"
                ),
                "remote_after_adapter": (
                    uav_sink_capture if direction == "downlink" else "cp.sink.eth0"
                ),
            }
            if capture_points != expected_capture_points:
                failures.append(
                    f"{label} capture points do not match the actual endpoint adapter sides"
                )
        identity = _exact_keys(
            cell.get("identity"),
            {"nonce_domain", "flow_id", "sequence_policy", "hash_domains"},
            f"{label}.identity",
            failures,
        )
        nonce_domain = identity.get("nonce_domain")
        if not isinstance(nonce_domain, str) or not nonce_domain:
            failures.append(f"{label} nonce domain is missing")
        else:
            nonce_domains.append(nonce_domain)
        if identity.get("flow_id") != cell_id:
            failures.append(f"{label} flow_id differs from cell_id")
        if identity.get("sequence_policy") != "strictly_monotonic_per_flow":
            failures.append(f"{label} sequence policy is not strict per-flow monotonic")
        hash_domains = _exact_keys(identity.get("hash_domains"), set(HASH_DOMAINS), f"{label}.identity.hash_domains", failures)
        for domain, enabled in hash_domains.items():
            if not isinstance(enabled, bool):
                failures.append(f"{label} hash domain {domain} is not boolean")
        if traffic_class == "additional_data" and hash_domains.get("mavlink_frame_sha256") is not False:
            failures.append(f"{label} additional-data MAVLink hash applicability is wrong")
        if traffic_class in ("control", "payload") and hash_domains.get("mavlink_frame_sha256") is not True:
            failures.append(f"{label} MAVLink frame hash is not required")
        _exact_keys(cell.get("outcome"), {"response_kind", "timeout_ms", "correlation"}, f"{label}.outcome", failures)
        pair_allocations.setdefault((str(name), str(traffic_class)), []).append(ports)

    observed_set = set(observed_tuples)
    duplicates = sorted({item for item in observed_tuples if observed_tuples.count(item) > 1}, key=str)
    if duplicates:
        failures.append(f"duplicate matrix cells: {duplicates}")
    missing = sorted(expected_tuples - observed_set)
    extra = sorted(observed_set - expected_tuples, key=str)
    if missing or extra:
        failures.append(f"matrix tuple set is not exact: missing={missing} extra={extra}")
    expected_order = [
        (f"uav{index}", traffic_class, direction)
        for index in range(1, 6)
        for traffic_class in TRAFFIC_CLASSES
        for direction in DIRECTIONS
    ]
    if observed_tuples != expected_order:
        failures.append("matrix cell order is not canonical")
    for label, values in (("cell_id", cell_ids), ("nonce domain", nonce_domains), ("queue_id", queue_ids)):
        if len(values) != len(set(values)):
            failures.append(f"reused {label} in endpoint matrix")

    allocations: dict[tuple[int, int, int], tuple[str, str]] = {}
    for pair, rows in pair_allocations.items():
        if len(rows) != 2:
            failures.append(f"{pair} does not have exactly two direction rows")
            continue
        normalized = {
            (
                row.get("uav_udp"),
                row.get("ns3_ground_handoff_udp"),
                row.get("ns3_uav_handoff_udp"),
            )
            for row in rows
        }
        if len(normalized) != 1:
            failures.append(f"{pair} direction rows disagree on port allocation")
            continue
        allocation = next(iter(normalized))
        previous = allocations.get(allocation)
        if previous is not None and previous != pair:
            failures.append(f"reused port allocation between {previous} and {pair}")
        allocations[allocation] = pair

    profiles = _exact_keys(data.get("profiles"), {M2_PROFILE, M3_PROFILE}, "profiles", failures)
    for profile_id, expected_count in ((M2_PROFILE, 2), (M3_PROFILE, 30)):
        profile = _exact_keys(
            profiles.get(profile_id),
            {"profile_id", "expected_cell_count", "cell_ids", "phase_contract"},
            f"profiles.{profile_id}",
            failures,
        )
        profile_cells = profile.get("cell_ids")
        if profile.get("profile_id") != profile_id or profile.get("expected_cell_count") != expected_count:
            failures.append(f"profile {profile_id} identity/count is wrong")
        if not isinstance(profile_cells, list) or len(profile_cells) != expected_count or len(profile_cells) != len(set(profile_cells)):
            failures.append(f"profile {profile_id} cell list is not exact/unique")
        elif any(cell_id not in set(cell_ids) for cell_id in profile_cells):
            failures.append(f"profile {profile_id} references an unknown cell")
    m2_expected = [f"uav1.control.{direction}" for direction in DIRECTIONS]
    if isinstance(profiles.get(M2_PROFILE), dict) and profiles[M2_PROFILE].get("cell_ids") != m2_expected:
        failures.append("M2 profile is not the exact uav1/control/two-direction subset")
    if isinstance(profiles.get(M3_PROFILE), dict) and profiles[M3_PROFILE].get("cell_ids") != cell_ids:
        failures.append("M3 profile is not the complete ordered 30-cell matrix")

    expected_cells_hash = sha256_bytes(canonical_json(cells))
    if data.get("resolved_cells_sha256") != expected_cells_hash:
        failures.append("resolved_cells_sha256 does not match canonical cell bytes")
    source_configs = _exact_keys(data.get("source_configs"), {"schema", "endpoints", "scenario", "radio"}, "source_configs", failures)
    for name, record in source_configs.items():
        record = _exact_keys(record, {"path", "sha256"}, f"source_configs.{name}", failures)
        if not isinstance(record.get("path"), str) or not isinstance(record.get("sha256"), str) or re.fullmatch(r"[0-9a-f]{64}", str(record.get("sha256"))) is None:
            failures.append(f"source config record {name} is invalid")
    return failures


def validate_matrix_file(
    matrix_path: Path = DEFAULT_MATRIX,
    *,
    endpoints_path: Path = DEFAULT_ENDPOINTS,
    scenario_path: Path = DEFAULT_SCENARIO,
    radio_path: Path = DEFAULT_RADIO,
    schema_path: Path = DEFAULT_SCHEMA,
) -> dict[str, Any]:
    data = load_strict_json(matrix_path)
    failures = validate_matrix_data(data)
    expected = build_resolved_matrix(
        endpoints_path=endpoints_path,
        scenario_path=scenario_path,
        radio_path=radio_path,
        schema_path=schema_path,
    )
    if data != expected:
        failures.append("tracked matrix differs from deterministic source resolution")
    if failures:
        raise MatrixError("; ".join(dict.fromkeys(failures)))
    return data


def select_profile(data: dict[str, Any], profile_id: str) -> dict[str, Any]:
    failures = validate_matrix_data(data)
    if failures:
        raise MatrixError("cannot select from invalid matrix: " + "; ".join(failures))
    profiles = data["profiles"]
    if profile_id not in profiles:
        raise MatrixError(f"unknown endpoint profile: {profile_id}")
    selected_ids = set(profiles[profile_id]["cell_ids"])
    selected_cells = [cell for cell in data["cells"] if cell["cell_id"] in selected_ids]
    if [cell["cell_id"] for cell in selected_cells] != profiles[profile_id]["cell_ids"]:
        raise MatrixError(f"profile {profile_id} selection order differs from declared order")
    return {
        "schema_version": 1,
        "contract": CONTRACT,
        "matrix_id": data["matrix_id"],
        "matrix_cells_sha256": data["resolved_cells_sha256"],
        "profile": profiles[profile_id],
        "cells": selected_cells,
        "selected_cells_sha256": sha256_bytes(canonical_json(selected_cells)),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("render", "validate", "select"))
    parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    parser.add_argument("--endpoints", type=Path, default=DEFAULT_ENDPOINTS)
    parser.add_argument("--scenario", type=Path, default=DEFAULT_SCENARIO)
    parser.add_argument("--radio", type=Path, default=DEFAULT_RADIO)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--profile", choices=(M2_PROFILE, M3_PROFILE), default=M3_PROFILE)
    args = parser.parse_args(argv)
    try:
        if args.command == "render":
            result = build_resolved_matrix(
                endpoints_path=args.endpoints,
                scenario_path=args.scenario,
                radio_path=args.radio,
                schema_path=args.schema,
            )
        else:
            matrix = validate_matrix_file(
                args.matrix,
                endpoints_path=args.endpoints,
                scenario_path=args.scenario,
                radio_path=args.radio,
                schema_path=args.schema,
            )
            result = select_profile(matrix, args.profile) if args.command == "select" else {
                "status": "passed",
                "contract": CONTRACT,
                "matrix_id": matrix["matrix_id"],
                "resolved_cells_sha256": matrix["resolved_cells_sha256"],
                "cell_count": len(matrix["cells"]),
                "profile": args.profile,
            }
    except MatrixError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    rendered = pretty_json(result)
    if args.output is not None:
        try:
            with args.output.open("x", encoding="utf-8") as handle:
                handle.write(rendered)
        except OSError as exc:
            print(f"FAIL cannot create output {args.output}: {exc}", file=sys.stderr)
            return 1
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
