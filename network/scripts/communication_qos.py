#!/usr/bin/env python3
"""Strict loader for the single Town01 communication/QoS product config."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = ROOT / "network/config/communication_qos.yaml"
CLASS_NAMES = ("control", "payload", "additional_data")
PROFILE_NAMES = ("nominal", "contention", "overload")


class QosConfigError(ValueError):
    """The communication QoS configuration is incomplete or unsafe."""


class _UniqueLoader(yaml.SafeLoader):
    pass


def _construct_mapping(
    loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise QosConfigError(f"duplicate YAML key: {key!r}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise QosConfigError(f"{label} must be a mapping")
    return value


def _positive_int(mapping: dict[str, Any], key: str, label: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise QosConfigError(f"{label}.{key} must be a positive integer")
    return value


def load_qos(path: Path = DEFAULT_PATH) -> dict[str, Any]:
    if not path.is_file():
        raise QosConfigError(f"missing communication QoS config: {path}")
    document = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueLoader)
    root = _mapping(document, "root")
    if root.get("schema_version") != 1:
        raise QosConfigError("schema_version must be 1")
    serial = _mapping(root.get("serial_transport"), "serial_transport")
    _positive_int(serial, "chunk_payload_bytes", "serial_transport")
    _positive_int(serial, "reassembly_timeout_ms", "serial_transport")
    _positive_int(serial, "metrics_period_ms", "serial_transport")
    if serial.get("protocol_version") != 1:
        raise QosConfigError("serial_transport.protocol_version must be 1")
    scheduler = _mapping(root.get("scheduler"), "scheduler")
    burst = _positive_int(scheduler, "control_burst_limit", "scheduler")
    if burst > 1024:
        raise QosConfigError("scheduler.control_burst_limit must be in 1..1024")
    state = _mapping(root.get("channel_state"), "channel_state")
    maximum_age = _positive_int(state, "maximum_age_ms", "channel_state")
    if maximum_age > 60000 or state.get("stale_policy") != "drop":
        raise QosConfigError("channel state must use a <=60000 ms fail-closed age")

    classes = _mapping(root.get("classes"), "classes")
    if set(classes) != set(CLASS_NAMES):
        raise QosConfigError(f"classes must be exactly {CLASS_NAMES}")
    priorities: list[int] = []
    tos_values: list[int] = []
    for name in CLASS_NAMES:
        item = _mapping(classes[name], f"classes.{name}")
        priority = item.get("priority")
        tos = item.get("tos")
        if not isinstance(priority, int) or isinstance(priority, bool) or priority < 0:
            raise QosConfigError(f"classes.{name}.priority must be a non-negative integer")
        if not isinstance(tos, int) or isinstance(tos, bool) or not 0 <= tos <= 255:
            raise QosConfigError(f"classes.{name}.tos must be in 0..255")
        priorities.append(priority)
        tos_values.append(tos)
        deadline = _positive_int(item, "deadline_ms", f"classes.{name}")
        max_age = _positive_int(item, "max_queue_age_ms", f"classes.{name}")
        _positive_int(item, "queue_limit_packets", f"classes.{name}")
        if max_age > deadline:
            raise QosConfigError(f"classes.{name}.max_queue_age_ms exceeds deadline_ms")
        if name != "additional_data":
            _positive_int(item, "baud_rate", f"classes.{name}")
    if priorities != sorted(set(priorities)):
        raise QosConfigError("class priorities must be unique and control < payload < additional_data")
    if len(set(tos_values)) != len(tos_values):
        raise QosConfigError("class TOS values must be unique")
    control = classes["control"]
    pdr = control.get("required_pdr")
    latency = control.get("max_p95_latency_ms")
    if not isinstance(pdr, (int, float)) or isinstance(pdr, bool) or not 0 < pdr <= 1:
        raise QosConfigError("classes.control.required_pdr must be in (0,1]")
    if not isinstance(latency, (int, float)) or isinstance(latency, bool) or latency <= 0:
        raise QosConfigError("classes.control.max_p95_latency_ms must be positive")

    profiles = _mapping(root.get("profiles"), "profiles")
    if set(profiles) != set(PROFILE_NAMES):
        raise QosConfigError(f"profiles must be exactly {PROFILE_NAMES}")
    offered_bps: dict[str, int] = {}
    for profile_name in PROFILE_NAMES:
        profile = _mapping(profiles[profile_name], f"profiles.{profile_name}")
        _positive_int(profile, "duration_s", f"profiles.{profile_name}")
        total = 0
        for class_name in CLASS_NAMES:
            traffic = _mapping(
                profile.get(class_name), f"profiles.{profile_name}.{class_name}"
            )
            pps = _positive_int(
                traffic, "packets_per_second_per_uav", f"profiles.{profile_name}.{class_name}"
            )
            size = _positive_int(traffic, "packet_bytes", f"profiles.{profile_name}.{class_name}")
            if size > 1400:
                raise QosConfigError("profile packet_bytes must fit without IPv4 fragmentation")
            total += pps * size * 8 * 5
        offered_bps[profile_name] = total
    if not offered_bps["nominal"] < offered_bps["contention"] < offered_bps["overload"]:
        raise QosConfigError("profile offered loads must increase nominal < contention < overload")
    return root


if __name__ == "__main__":
    load_qos()
