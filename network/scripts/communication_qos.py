#!/usr/bin/env python3
"""Strict loader for the single Town01 communication/QoS product config."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = ROOT / "network/config/communication_qos.yaml"
CLASS_NAMES = ("control", "payload", "additional_data")
PROFILE_NAMES = ("nominal", "contention", "controlled_overload", "meltdown")
GATED_PROFILE_NAMES = tuple(name for name in PROFILE_NAMES if name != "meltdown")


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


def _required_bool(mapping: dict[str, Any], key: str, label: str) -> bool:
    value = mapping.get(key)
    if not isinstance(value, bool):
        raise QosConfigError(f"{label}.{key} must be a boolean")
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
    if not _required_bool(scheduler, "strict_control_priority", "scheduler"):
        raise QosConfigError("scheduler.strict_control_priority must remain enabled")
    if not _required_bool(scheduler, "fair_lower_classes_per_uav", "scheduler"):
        raise QosConfigError("scheduler.fair_lower_classes_per_uav must remain enabled")

    medium_access = _mapping(root.get("medium_access"), "medium_access")
    required_medium_access = {
        "mode": "centralized_priority_scheduler_over_csma_channel",
        "arbitration_mode": "centralized_priority_scheduler",
        "transport_medium": "ns3_csma_channel",
        "collisions_expected": False,
        "non_preemptive_current_frame": True,
    }
    if medium_access != required_medium_access:
        raise QosConfigError(
            "medium_access must declare the centralized scheduler over the ns-3 CSMA channel"
        )

    protection = _mapping(root.get("protection"), "protection")
    if not _required_bool(
        protection, "ingress_token_bucket_enabled", "protection"
    ):
        raise QosConfigError("protection.ingress_token_bucket_enabled must remain enabled")
    if not _required_bool(
        protection, "deadline_drop_before_radio_decision", "protection"
    ):
        raise QosConfigError(
            "protection.deadline_drop_before_radio_decision must remain enabled"
        )
    if not _required_bool(
        protection, "terminal_expiry_after_drain", "protection"
    ):
        raise QosConfigError("protection.terminal_expiry_after_drain must remain enabled")
    minimum_control_headroom = _positive_int(
        protection, "minimum_control_headroom_bps", "protection"
    )
    payload_rate = _positive_int(
        protection, "payload_admission_rate_bps", "protection"
    )
    additional_rate = _positive_int(
        protection, "additional_data_admission_rate_bps", "protection"
    )
    _positive_int(protection, "token_bucket_burst_bytes_per_uav", "protection")
    lower_retry_limit = _positive_int(protection, "lower_retry_limit", "protection")
    mac_retry_limit = _positive_int(protection, "mac_retry_limit", "protection")
    if lower_retry_limit > mac_retry_limit:
        raise QosConfigError("protection.lower_retry_limit must not exceed mac_retry_limit")
    flush_every = _positive_int(protection, "event_log_flush_every", "protection")
    if flush_every > 65536:
        raise QosConfigError("protection.event_log_flush_every must be <= 65536")
    flush_max_delay = _positive_int(
        protection, "event_log_flush_max_delay_ms", "protection"
    )
    if flush_max_delay > 1000:
        raise QosConfigError(
            "protection.event_log_flush_max_delay_ms must be <= 1000"
        )
    snapshot_wait_intervals = _positive_int(
        protection, "event_log_snapshot_wait_intervals", "protection"
    )
    if snapshot_wait_intervals > 10:
        raise QosConfigError(
            "protection.event_log_snapshot_wait_intervals must be <= 10"
        )
    drain_interval = _positive_int(protection, "drain_interval_ms", "protection")
    if drain_interval > 60000:
        raise QosConfigError("protection.drain_interval_ms must be <= 60000")
    if minimum_control_headroom + payload_rate + additional_rate <= (
        minimum_control_headroom
    ):
        raise QosConfigError("lower-class admission rates must be non-zero")

    aggregation = _mapping(root.get("serial_aggregation"), "serial_aggregation")
    aggregation_enabled = _required_bool(
        aggregation, "enabled", "serial_aggregation"
    )
    if aggregation_enabled:
        raise QosConfigError(
            "serial aggregation remains unavailable until event profiling authorizes it"
        )
    aggregation_mtu = _positive_int(aggregation, "mtu_bytes", "serial_aggregation")
    _positive_int(aggregation, "max_delay_ms", "serial_aggregation")
    if aggregation_mtu > 1400:
        raise QosConfigError("serial_aggregation.mtu_bytes must avoid IPv4 fragmentation")
    state = _mapping(root.get("channel_state"), "channel_state")
    maximum_age = _positive_int(state, "maximum_age_ms", "channel_state")
    if maximum_age > 60000 or state.get("stale_policy") != "drop":
        raise QosConfigError("channel state must use a <=60000 ms fail-closed age")

    classes = _mapping(root.get("classes"), "classes")
    if set(classes) != set(CLASS_NAMES):
        raise QosConfigError(f"classes must be exactly {CLASS_NAMES}")
    priorities: list[int] = []
    tos_values: list[int] = []
    maximum_class_queue_age_ms = 0
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
        maximum_class_queue_age_ms = max(maximum_class_queue_age_ms, max_age)
        _positive_int(item, "queue_limit_packets", f"classes.{name}")
        if max_age > deadline:
            raise QosConfigError(f"classes.{name}.max_queue_age_ms exceeds deadline_ms")
        if name != "additional_data":
            _positive_int(item, "baud_rate", f"classes.{name}")
    if priorities != sorted(set(priorities)):
        raise QosConfigError("class priorities must be unique and control < payload < additional_data")
    if len(set(tos_values)) != len(tos_values):
        raise QosConfigError("class TOS values must be unique")
    if drain_interval < maximum_class_queue_age_ms:
        raise QosConfigError(
            "protection.drain_interval_ms must cover every class max_queue_age_ms"
        )
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
        shaping_enabled = _required_bool(
            profile, "shaping_enabled", f"profiles.{profile_name}"
        )
        gates = _required_bool(
            profile, "gates_overall_status", f"profiles.{profile_name}"
        )
        if profile_name == "meltdown":
            if shaping_enabled or gates:
                raise QosConfigError("meltdown must disable shaping and overall gating")
        elif not shaping_enabled or not gates:
            raise QosConfigError(f"{profile_name} must enable shaping and overall gating")
        if profile_name == "controlled_overload":
            lag_limit = profile.get("max_scheduler_lag_p95_ms")
            minimum_rtf = profile.get("min_gazebo_mean_rtf")
            if (
                not isinstance(lag_limit, (int, float))
                or isinstance(lag_limit, bool)
                or lag_limit <= 0
            ):
                raise QosConfigError(
                    "profiles.controlled_overload.max_scheduler_lag_p95_ms must be positive"
                )
            if (
                not isinstance(minimum_rtf, (int, float))
                or isinstance(minimum_rtf, bool)
                or not 0 < minimum_rtf <= 1
            ):
                raise QosConfigError(
                    "profiles.controlled_overload.min_gazebo_mean_rtf must be in (0,1]"
                )
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
    if not (
        offered_bps["nominal"]
        < offered_bps["contention"]
        < offered_bps["controlled_overload"]
        == offered_bps["meltdown"]
    ):
        raise QosConfigError(
            "loads must increase nominal < contention < controlled_overload = meltdown"
        )
    return root


if __name__ == "__main__":
    load_qos()
