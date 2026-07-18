#!/usr/bin/env python3
"""Strict, dependency-free Sionna asynchronous protocol v1 primitives.

This module deliberately contains no sockets and no ns-3 bindings.  It is the
testable boundary between the wire protocol and an eventual simulator adapter:

* canonical JSONL encoding and strict decoding;
* semantic validation beyond what JSON Schema can express;
* reconnect/wire-sequence lifecycle tracking;
* bounded, directed-link result selection with fail-closed expiry semantics.

Provider results never contain ``applied_state_id``.  That identity is created
by the adapter exactly when a result is selected for packet processing.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
)


PROTOCOL_NAME = "sionna_async"
SCHEMA_VERSION = 1
MESSAGE_TYPES = frozenset({"hello", "ready", "query", "result", "error", "disconnect"})
FAILURE_STATUSES = frozenset(
    {"stale_pose", "scene_mismatch", "provider_error", "deadline_missed"}
)
DEFAULT_MAX_MESSAGE_BYTES = 1_048_576
DEFAULT_MAX_LINKS = 64
DEFAULT_MAX_PENDING_RESULTS_PER_LINK = 8
DEFAULT_MAX_QUERY_HISTORY = 100_000
DEFAULT_REQUEST_QUEUE_CAPACITY = 64
DEFAULT_COMPLETION_QUEUE_CAPACITY = 64
DEFAULT_MAX_POLL_BATCH = 64
DEFAULT_VALIDITY_TTL_NS = 2_000_000_000
DEFAULT_MAX_POSE_AGE_NS = 1_500_000_000

MODULE_DIR = Path(__file__).resolve().parent
REPOSITORY_ROOT = MODULE_DIR.parents[1]
DEFAULT_PROTOCOL_CONFIG_PATH = (
    REPOSITORY_ROOT / "network/config/sionna_async_protocol_v1.json"
)
DEFAULT_SCHEMA_PATH = REPOSITORY_ROOT / "network/config/sionna_async_schema_v1.json"

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+\-]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_COMMON_KEYS = frozenset(
    {
        "schema_version",
        "message_type",
        "wire_sequence",
        "sender_id",
        "run_id",
        "profile",
        "phase_id",
        "contract_hash",
        "config_hash",
        "bundle_id",
        "reconnect_generation",
        "sender_clock_domain",
        "emitted_monotonic_ns",
    }
)

_HELLO_KEYS = frozenset(
    {
        "protocol_name",
        "protocol_version",
        "sender_role",
        "executable_identity",
        "capabilities",
        "accepted_run_id",
        "accepted_config_hash",
        "accepted_bundle_id",
        "readiness_state",
    }
)
_READY_KEYS = _HELLO_KEYS | {"scene_identity"}
_QUERY_KEYS = frozenset(
    {
        "query_id",
        "node_state_seq",
        "node_state_sha256",
        "node_state_snapshot_monotonic_ns",
        "directed_link_id",
        "deadline_monotonic_ns",
        "traffic_class",
        "tx_node_id",
        "rx_node_id",
        "source_pose_monotonic_ns",
        "source_frame",
        "transform_version",
        "request_generated_monotonic_ns",
        "request_sent_monotonic_ns",
        "nodes",
        "jammers",
        "radio_assumptions",
        "antenna_assumptions",
        "material_assumptions",
        "mapping_version",
        "provider_seed",
    }
)
_RESULT_COMMON_KEYS = frozenset(
    {
        "query_id",
        "node_state_seq",
        "directed_link_id",
        "traffic_class",
        "tx_node_id",
        "rx_node_id",
        "provider_clock_domain",
        "provider_received_monotonic_ns",
        "provider_started_monotonic_ns",
        "provider_completed_monotonic_ns",
        "provider_sent_monotonic_ns",
        "status",
    }
)
_RESULT_OK_KEYS = _RESULT_COMMON_KEYS | {
    "validity_clock_domain",
    "validity_start_monotonic_ns",
    "expires_monotonic_ns",
    "physical",
}
_RESULT_FAILURE_KEYS = _RESULT_COMMON_KEYS | {"error_body"}
_ERROR_KEYS = frozenset(
    {
        "error_kind",
        "reason",
        "lifecycle_monotonic_ns",
        "rejected_wire_sequence",
        "rejected_request_sha256",
    }
)
_DISCONNECT_KEYS = frozenset(
    {
        "disconnect_kind",
        "reason",
        "lifecycle_monotonic_ns",
        "owned_directed_link_ids",
    }
)


class ProtocolValidationError(ValueError):
    """A frame is not a valid Sionna asynchronous protocol v1 message."""


class ProtocolStateError(RuntimeError):
    """A valid message violates lifecycle, correlation, or bounded-state rules."""


@dataclass(frozen=True)
class ProtocolLimits:
    max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES
    max_links: int = DEFAULT_MAX_LINKS
    max_pending_results_per_link: int = DEFAULT_MAX_PENDING_RESULTS_PER_LINK
    max_query_history: int = DEFAULT_MAX_QUERY_HISTORY
    request_queue_capacity: int = DEFAULT_REQUEST_QUEUE_CAPACITY
    completion_queue_capacity: int = DEFAULT_COMPLETION_QUEUE_CAPACITY
    max_poll_batch: int = DEFAULT_MAX_POLL_BATCH
    validity_ttl_ns: int = DEFAULT_VALIDITY_TTL_NS
    max_pose_age_ns: int = DEFAULT_MAX_POSE_AGE_NS


@dataclass(frozen=True)
class ProtocolIdentity:
    run_id: str
    profile: str
    contract_hash: str
    config_hash: str
    bundle_id: str

    @classmethod
    def from_message(cls, message: Mapping[str, Any]) -> "ProtocolIdentity":
        return cls(
            run_id=str(message["run_id"]),
            profile=str(message["profile"]),
            contract_hash=str(message["contract_hash"]),
            config_hash=str(message["config_hash"]),
            bundle_id=str(message["bundle_id"]),
        )

    def matches(self, message: Mapping[str, Any]) -> bool:
        return all(
            getattr(self, key) == message.get(key) for key in self.__dataclass_fields__
        )


def _fail(path: str, detail: str) -> None:
    raise ProtocolValidationError(f"{path}: {detail}")


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        _fail(path, "must be an object")
    return value


def _exact(
    value: Any,
    path: str,
    required: Iterable[str],
    optional: Iterable[str] = (),
) -> Mapping[str, Any]:
    obj = _mapping(value, path)
    required_set = set(required)
    allowed = required_set | set(optional)
    missing = sorted(required_set - set(obj))
    unknown = sorted(set(obj) - allowed)
    if missing:
        _fail(path, f"missing required keys: {', '.join(missing)}")
    if unknown:
        _fail(path, f"unknown keys: {', '.join(unknown)}")
    return obj


def _integer(value: Any, path: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(path, "must be an integer (boolean is not an integer)")
    if value < minimum:
        _fail(path, f"must be >= {minimum}")
    return value


def _number(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(path, "must be a number (boolean is not a number)")
    result = float(value)
    if not math.isfinite(result):
        _fail(path, "must be finite")
    return result


def _text(value: Any, path: str, minimum: int = 1, maximum: int = 256) -> str:
    if not isinstance(value, str):
        _fail(path, "must be a string")
    if not minimum <= len(value) <= maximum:
        _fail(path, f"length must be in [{minimum}, {maximum}]")
    return value


def _safe_id(value: Any, path: str) -> str:
    result = _text(value, path, maximum=128)
    if not _SAFE_ID_RE.fullmatch(result):
        _fail(path, "must match the safe identifier grammar")
    return result


def _sha256(value: Any, path: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        _fail(path, "must be a lowercase 64-character SHA-256 hex digest")
    return value


def _boolean(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        _fail(path, "must be a boolean")
    return value


def _enum(value: Any, path: str, allowed: Iterable[str]) -> str:
    result = _text(value, path)
    allowed_set = set(allowed)
    if result not in allowed_set:
        _fail(path, f"must be one of {sorted(allowed_set)}")
    return result


def _const(value: Any, path: str, expected: Any) -> None:
    if isinstance(expected, bool):
        matches = type(value) is bool and value is expected
    elif isinstance(expected, int):
        matches = not isinstance(value, bool) and value == expected
    else:
        matches = value == expected
    if not matches:
        _fail(path, f"must equal {expected!r}")


def _vector(value: Any, path: str, length: int) -> List[float]:
    if not isinstance(value, list) or len(value) != length:
        _fail(path, f"must be an array of exactly {length} numbers")
    return [_number(item, f"{path}[{index}]") for index, item in enumerate(value)]


def _validate_common(message: Mapping[str, Any]) -> None:
    _const(message["schema_version"], "schema_version", SCHEMA_VERSION)
    _enum(message["message_type"], "message_type", MESSAGE_TYPES)
    _integer(message["wire_sequence"], "wire_sequence", minimum=1)
    for key in (
        "sender_id",
        "run_id",
        "profile",
        "phase_id",
        "bundle_id",
        "sender_clock_domain",
    ):
        _safe_id(message[key], key)
    for key in ("contract_hash", "config_hash"):
        _sha256(message[key], key)
    _integer(message["reconnect_generation"], "reconnect_generation")
    _integer(message["emitted_monotonic_ns"], "emitted_monotonic_ns")


def _validate_executable(value: Any, path: str) -> None:
    obj = _exact(value, path, {"path", "sha256"})
    executable_path = _text(obj["path"], f"{path}.path", maximum=4096)
    if not executable_path.startswith("/"):
        _fail(f"{path}.path", "must be absolute")
    _sha256(obj["sha256"], f"{path}.sha256")


def _validate_capabilities(value: Any, path: str) -> None:
    obj = _exact(value, path, {"supported_message_types", "max_message_bytes"})
    types = obj["supported_message_types"]
    if not isinstance(types, list) or not 1 <= len(types) <= len(MESSAGE_TYPES):
        _fail(f"{path}.supported_message_types", "must contain 1..6 message types")
    validated = [
        _enum(item, f"{path}.supported_message_types[{index}]", MESSAGE_TYPES)
        for index, item in enumerate(types)
    ]
    if len(set(validated)) != len(validated):
        _fail(f"{path}.supported_message_types", "must not contain duplicates")
    maximum = _integer(obj["max_message_bytes"], f"{path}.max_message_bytes", minimum=1)
    if maximum > DEFAULT_MAX_MESSAGE_BYTES:
        _fail(f"{path}.max_message_bytes", f"must be <= {DEFAULT_MAX_MESSAGE_BYTES}")


def _validate_provider_identity(value: Any, path: str) -> None:
    obj = _exact(
        value,
        path,
        {
            "provider_id",
            "provider_mode",
            "acceptance_eligible",
            "sionna_rt_version",
            "mitsuba_version",
        },
    )
    _safe_id(obj["provider_id"], f"{path}.provider_id")
    mode = _safe_id(obj["provider_mode"], f"{path}.provider_mode")
    eligible = _boolean(obj["acceptance_eligible"], f"{path}.acceptance_eligible")
    _text(obj["sionna_rt_version"], f"{path}.sionna_rt_version")
    _text(obj["mitsuba_version"], f"{path}.mitsuba_version")
    if eligible and mode != "real_sionna":
        _fail(path, "only provider_mode=real_sionna may be acceptance eligible")


def _validate_handshake(message: Mapping[str, Any], ready: bool) -> None:
    required = _COMMON_KEYS | (_READY_KEYS if ready else _HELLO_KEYS)
    optional = {"provider_identity"}
    _exact(message, "message", required, optional)
    _validate_common(message)
    expected_type = "ready" if ready else "hello"
    _const(message["message_type"], "message_type", expected_type)
    _const(message["protocol_name"], "protocol_name", PROTOCOL_NAME)
    _const(message["protocol_version"], "protocol_version", SCHEMA_VERSION)
    role = _enum(
        message["sender_role"], "sender_role", {"adapter", "provider", "fault_injector"}
    )
    _validate_executable(message["executable_identity"], "executable_identity")
    _validate_capabilities(message["capabilities"], "capabilities")
    if message["accepted_run_id"] != message["run_id"]:
        _fail("accepted_run_id", "must equal run_id")
    if message["accepted_config_hash"] != message["config_hash"]:
        _fail("accepted_config_hash", "must equal config_hash")
    if message["accepted_bundle_id"] != message["bundle_id"]:
        _fail("accepted_bundle_id", "must equal bundle_id")
    _safe_id(message["accepted_run_id"], "accepted_run_id")
    _sha256(message["accepted_config_hash"], "accepted_config_hash")
    _safe_id(message["accepted_bundle_id"], "accepted_bundle_id")
    if ready:
        _const(message["readiness_state"], "readiness_state", "ready")
        scene = _exact(
            message["scene_identity"],
            "scene_identity",
            {"bundle_id", "scene_manifest_sha256", "scene_path"},
        )
        if scene["bundle_id"] != message["bundle_id"]:
            _fail("scene_identity.bundle_id", "must equal bundle_id")
        _safe_id(scene["bundle_id"], "scene_identity.bundle_id")
        _sha256(scene["scene_manifest_sha256"], "scene_identity.scene_manifest_sha256")
        scene_path = _text(
            scene["scene_path"], "scene_identity.scene_path", maximum=4096
        )
        if not scene_path.startswith("/"):
            _fail("scene_identity.scene_path", "must be absolute")
    else:
        _enum(
            message["readiness_state"],
            "readiness_state",
            {"initializing", "not_ready", "ready"},
        )
    if role == "provider":
        if "provider_identity" not in message:
            _fail("provider_identity", "is required for sender_role=provider")
        _validate_provider_identity(message["provider_identity"], "provider_identity")
    elif "provider_identity" in message:
        _fail("provider_identity", "is forbidden unless sender_role=provider")


def _validate_pose_common(
    value: Any, path: str, jammer: bool = False
) -> Tuple[str, int]:
    base = {
        "pose_monotonic_ns",
        "source_topic",
        "source_frame",
        "transform_version",
        "position_m",
        "orientation_quat_xyzw",
        "freshness_age_ns",
        "stale",
    }
    entity = {"node_id", "role"}
    jammer_keys = {
        "jammer_id",
        "enabled",
        "center_frequency_hz",
        "bandwidth_hz",
        "power_dbm",
        "duty_cycle",
        "antenna_pattern",
    }
    obj = _exact(value, path, base | (jammer_keys if jammer else entity))
    identity_key = "jammer_id" if jammer else "node_id"
    identity = _safe_id(obj[identity_key], f"{path}.{identity_key}")
    if not jammer:
        _safe_id(obj["role"], f"{path}.role")
    pose_time = _integer(obj["pose_monotonic_ns"], f"{path}.pose_monotonic_ns")
    _text(obj["source_topic"], f"{path}.source_topic")
    _safe_id(obj["source_frame"], f"{path}.source_frame")
    _safe_id(obj["transform_version"], f"{path}.transform_version")
    _vector(obj["position_m"], f"{path}.position_m", 3)
    quat = _vector(obj["orientation_quat_xyzw"], f"{path}.orientation_quat_xyzw", 4)
    norm = math.sqrt(sum(component * component for component in quat))
    if not 0.99 <= norm <= 1.01:
        _fail(f"{path}.orientation_quat_xyzw", "must be normalized within [0.99, 1.01]")
    _integer(obj["freshness_age_ns"], f"{path}.freshness_age_ns")
    _boolean(obj["stale"], f"{path}.stale")
    if jammer:
        _boolean(obj["enabled"], f"{path}.enabled")
        for key in ("center_frequency_hz", "bandwidth_hz"):
            if _number(obj[key], f"{path}.{key}") <= 0:
                _fail(f"{path}.{key}", "must be > 0")
        _number(obj["power_dbm"], f"{path}.power_dbm")
        duty = _number(obj["duty_cycle"], f"{path}.duty_cycle")
        if not 0 <= duty <= 1:
            _fail(f"{path}.duty_cycle", "must be in [0, 1]")
        _safe_id(obj["antenna_pattern"], f"{path}.antenna_pattern")
    return identity, pose_time


def _validate_radio(value: Any, path: str) -> None:
    keys = {
        "carrier_frequency_hz",
        "bandwidth_hz",
        "tx_power_dbm",
        "receiver_noise_figure_db",
        "receiver_sensitivity_dbm",
        "units",
    }
    obj = _exact(value, path, keys)
    for key in ("carrier_frequency_hz", "bandwidth_hz"):
        if _number(obj[key], f"{path}.{key}") <= 0:
            _fail(f"{path}.{key}", "must be > 0")
    for key in ("tx_power_dbm", "receiver_noise_figure_db", "receiver_sensitivity_dbm"):
        _number(obj[key], f"{path}.{key}")
    expected = {
        "carrier_frequency": "Hz",
        "bandwidth": "Hz",
        "tx_power": "dBm",
        "receiver_noise_figure": "dB",
        "receiver_sensitivity": "dBm",
    }
    units = _exact(obj["units"], f"{path}.units", expected)
    for key, unit in expected.items():
        _const(units[key], f"{path}.units.{key}", unit)


def _validate_query(message: Mapping[str, Any]) -> None:
    _exact(message, "message", _COMMON_KEYS | _QUERY_KEYS)
    _validate_common(message)
    _const(message["message_type"], "message_type", "query")
    for key in (
        "query_id",
        "directed_link_id",
        "traffic_class",
        "tx_node_id",
        "rx_node_id",
        "source_frame",
        "transform_version",
        "mapping_version",
    ):
        _safe_id(message[key], key)
    _integer(message["node_state_seq"], "node_state_seq")
    _sha256(message["node_state_sha256"], "node_state_sha256")
    snapshot_time = _integer(
        message["node_state_snapshot_monotonic_ns"],
        "node_state_snapshot_monotonic_ns",
    )
    _integer(message["provider_seed"], "provider_seed")
    if message["tx_node_id"] == message["rx_node_id"]:
        _fail("rx_node_id", "must differ from tx_node_id for a directed link")
    source_time = _integer(
        message["source_pose_monotonic_ns"], "source_pose_monotonic_ns"
    )
    generated = _integer(
        message["request_generated_monotonic_ns"], "request_generated_monotonic_ns"
    )
    sent = _integer(message["request_sent_monotonic_ns"], "request_sent_monotonic_ns")
    deadline = _integer(message["deadline_monotonic_ns"], "deadline_monotonic_ns")
    emitted = message["emitted_monotonic_ns"]
    if not source_time <= snapshot_time <= generated <= sent <= emitted:
        _fail(
            "message",
            "requires source_pose <= node_state_snapshot <= request_generated "
            "<= request_sent <= emitted",
        )
    if deadline <= sent:
        _fail("deadline_monotonic_ns", "must be > request_sent_monotonic_ns")

    nodes = message["nodes"]
    if not isinstance(nodes, list) or not 2 <= len(nodes) <= 64:
        _fail("nodes", "must contain 2..64 entity poses")
    node_ids: List[str] = []
    pose_times: List[int] = []
    for index, node in enumerate(nodes):
        node_id, pose_time = _validate_pose_common(node, f"nodes[{index}]")
        node_ids.append(node_id)
        pose_times.append(pose_time)
        if node["source_frame"] != message["source_frame"]:
            _fail(f"nodes[{index}].source_frame", "must equal query source_frame")
        if node["transform_version"] != message["transform_version"]:
            _fail(
                f"nodes[{index}].transform_version",
                "must equal query transform_version",
            )
        if node["freshness_age_ns"] != sent - pose_time:
            _fail(
                f"nodes[{index}].freshness_age_ns",
                "must equal request_sent_monotonic_ns - pose_monotonic_ns",
            )
    if len(set(node_ids)) != len(node_ids):
        _fail("nodes", "node_id values must be unique")
    if message["tx_node_id"] not in node_ids or message["rx_node_id"] not in node_ids:
        _fail("nodes", "must contain both tx_node_id and rx_node_id")

    jammers = message["jammers"]
    if not isinstance(jammers, list) or len(jammers) > 16:
        _fail("jammers", "must be an array with at most 16 entries")
    jammer_ids: List[str] = []
    for index, jammer in enumerate(jammers):
        jammer_id, pose_time = _validate_pose_common(
            jammer, f"jammers[{index}]", jammer=True
        )
        jammer_ids.append(jammer_id)
        pose_times.append(pose_time)
        if jammer["source_frame"] != message["source_frame"]:
            _fail(f"jammers[{index}].source_frame", "must equal query source_frame")
        if jammer["transform_version"] != message["transform_version"]:
            _fail(
                f"jammers[{index}].transform_version",
                "must equal query transform_version",
            )
        if jammer["freshness_age_ns"] != sent - pose_time:
            _fail(
                f"jammers[{index}].freshness_age_ns",
                "must equal request_sent_monotonic_ns - pose_monotonic_ns",
            )
    if len(set(jammer_ids)) != len(jammer_ids):
        _fail("jammers", "jammer_id values must be unique")
    if set(jammer_ids) & set(node_ids):
        _fail("jammers", "jammer_id values must not collide with node_id values")
    if pose_times and max(pose_times) > source_time:
        _fail("source_pose_monotonic_ns", "must not precede any included entity pose")

    _validate_radio(message["radio_assumptions"], "radio_assumptions")
    antenna = _exact(
        message["antenna_assumptions"],
        "antenna_assumptions",
        {"tx_pattern", "rx_pattern", "polarization", "orientation_effects_claimed"},
    )
    _safe_id(antenna["tx_pattern"], "antenna_assumptions.tx_pattern")
    _safe_id(antenna["rx_pattern"], "antenna_assumptions.rx_pattern")
    _text(antenna["polarization"], "antenna_assumptions.polarization")
    _boolean(
        antenna["orientation_effects_claimed"],
        "antenna_assumptions.orientation_effects_claimed",
    )
    material = _exact(
        message["material_assumptions"],
        "material_assumptions",
        {"material_model_id", "scene_material_manifest_sha256"},
    )
    _safe_id(material["material_model_id"], "material_assumptions.material_model_id")
    _sha256(
        material["scene_material_manifest_sha256"],
        "material_assumptions.scene_material_manifest_sha256",
    )
    expected_node_state_hash = node_state_sha256(
        node_state_seq=int(message["node_state_seq"]),
        snapshot_monotonic_ns=snapshot_time,
        source_frame=str(message["source_frame"]),
        transform_version=str(message["transform_version"]),
        nodes=message["nodes"],
        jammers=message["jammers"],
    )
    if message["node_state_sha256"] != expected_node_state_hash:
        _fail("node_state_sha256", "does not match the exact immutable pose snapshot")


def _validate_physical(value: Any, path: str) -> None:
    numeric = {
        "pathloss_db",
        "propagation_delay_ns",
        "rssi_dbm",
        "signal_power_dbm",
        "interference_power_dbm",
        "noise_power_dbm",
        "sinr_db",
        "js_db",
    }
    obj = _exact(
        value,
        path,
        numeric | {"geometry_state", "path_count", "path_type_counts", "units"},
    )
    for key in numeric:
        number = _number(obj[key], f"{path}.{key}")
        if key == "propagation_delay_ns" and number < 0:
            _fail(f"{path}.{key}", "must be >= 0")
    _enum(
        obj["geometry_state"],
        f"{path}.geometry_state",
        {"los", "nlos", "blocked_no_path"},
    )
    path_count = _integer(obj["path_count"], f"{path}.path_count")
    type_names = {
        "los",
        "specular",
        "diffuse",
        "refracted",
        "diffracted",
        "mixed",
    }
    type_counts = _exact(
        obj["path_type_counts"], f"{path}.path_type_counts", type_names
    )
    for name in type_names:
        _integer(type_counts[name], f"{path}.path_type_counts.{name}")
    if sum(type_counts.values()) != path_count:
        _fail(f"{path}.path_type_counts", "must sum exactly to path_count")
    geometry = obj["geometry_state"]
    if (path_count == 0) != (geometry == "blocked_no_path"):
        _fail(path, "blocked_no_path must be equivalent to path_count=0")
    if geometry == "los" and type_counts["los"] == 0:
        _fail(path, "los geometry requires at least one LOS path")
    if geometry == "nlos" and type_counts["los"] != 0:
        _fail(path, "nlos geometry cannot contain an LOS path")
    expected = {
        "pathloss": "dB",
        "propagation_delay": "ns",
        "rssi": "dBm",
        "signal_power": "dBm",
        "interference_power": "dBm",
        "noise_power": "dBm",
        "sinr": "dB",
        "j_over_s": "dB",
    }
    units = _exact(obj["units"], f"{path}.units", expected)
    for key, unit in expected.items():
        _const(units[key], f"{path}.units.{key}", unit)


def _validate_result(message: Mapping[str, Any]) -> None:
    status = message.get("status")
    keys = _RESULT_OK_KEYS if status == "ok" else _RESULT_FAILURE_KEYS
    _exact(message, "message", _COMMON_KEYS | keys)
    _validate_common(message)
    _const(message["message_type"], "message_type", "result")
    for key in (
        "query_id",
        "directed_link_id",
        "traffic_class",
        "tx_node_id",
        "rx_node_id",
        "provider_clock_domain",
    ):
        _safe_id(message[key], key)
    if message["tx_node_id"] == message["rx_node_id"]:
        _fail("rx_node_id", "must differ from tx_node_id")
    _integer(message["node_state_seq"], "node_state_seq")
    timestamps = [
        _integer(
            message["provider_received_monotonic_ns"], "provider_received_monotonic_ns"
        ),
        _integer(
            message["provider_started_monotonic_ns"], "provider_started_monotonic_ns"
        ),
        _integer(
            message["provider_completed_monotonic_ns"],
            "provider_completed_monotonic_ns",
        ),
        _integer(message["provider_sent_monotonic_ns"], "provider_sent_monotonic_ns"),
        message["emitted_monotonic_ns"],
    ]
    if timestamps != sorted(timestamps):
        _fail(
            "message",
            "requires provider_received <= started <= completed <= sent <= emitted",
        )
    if message["provider_clock_domain"] != message["sender_clock_domain"]:
        _fail("provider_clock_domain", "must equal sender_clock_domain")
    if status == "ok":
        _const(status, "status", "ok")
        _safe_id(message["validity_clock_domain"], "validity_clock_domain")
        if message["validity_clock_domain"] != message["provider_clock_domain"]:
            _fail("validity_clock_domain", "must equal provider_clock_domain")
        start = _integer(
            message["validity_start_monotonic_ns"], "validity_start_monotonic_ns"
        )
        expiry = _integer(message["expires_monotonic_ns"], "expires_monotonic_ns")
        if start < message["provider_completed_monotonic_ns"]:
            _fail(
                "validity_start_monotonic_ns",
                "must be >= provider_completed_monotonic_ns",
            )
        if expiry <= start:
            _fail("expires_monotonic_ns", "must be > validity_start_monotonic_ns")
        _validate_physical(message["physical"], "physical")
    else:
        failure = _enum(status, "status", FAILURE_STATUSES)
        body = _exact(
            message["error_body"], "error_body", {"code", "detail", "retryable"}
        )
        if body["code"] != failure:
            _fail("error_body.code", "must equal result status")
        _enum(body["code"], "error_body.code", FAILURE_STATUSES)
        _text(body["detail"], "error_body.detail", maximum=1024)
        _boolean(body["retryable"], "error_body.retryable")


def _validate_error(message: Mapping[str, Any]) -> None:
    required = _COMMON_KEYS | {"error_kind", "reason", "lifecycle_monotonic_ns"}
    optional = {"rejected_wire_sequence", "rejected_request_sha256"}
    _exact(message, "message", required, optional)
    _validate_common(message)
    _const(message["message_type"], "message_type", "error")
    _const(message["error_kind"], "error_kind", "invalid_request")
    _text(message["reason"], "reason", maximum=1024)
    lifecycle = _integer(message["lifecycle_monotonic_ns"], "lifecycle_monotonic_ns")
    if lifecycle > message["emitted_monotonic_ns"]:
        _fail("lifecycle_monotonic_ns", "must be <= emitted_monotonic_ns")
    if "rejected_wire_sequence" in message:
        _integer(message["rejected_wire_sequence"], "rejected_wire_sequence", minimum=1)
    if "rejected_request_sha256" in message:
        _sha256(message["rejected_request_sha256"], "rejected_request_sha256")


def _validate_disconnect(message: Mapping[str, Any]) -> None:
    required = _COMMON_KEYS | {"disconnect_kind", "reason", "lifecycle_monotonic_ns"}
    optional = {"owned_directed_link_ids"}
    _exact(message, "message", required, optional)
    _validate_common(message)
    _const(message["message_type"], "message_type", "disconnect")
    _const(message["disconnect_kind"], "disconnect_kind", "disconnected")
    _text(message["reason"], "reason", maximum=1024)
    lifecycle = _integer(message["lifecycle_monotonic_ns"], "lifecycle_monotonic_ns")
    if lifecycle > message["emitted_monotonic_ns"]:
        _fail("lifecycle_monotonic_ns", "must be <= emitted_monotonic_ns")
    if "owned_directed_link_ids" in message:
        links = message["owned_directed_link_ids"]
        if not isinstance(links, list) or len(links) > DEFAULT_MAX_LINKS:
            _fail("owned_directed_link_ids", "must contain at most 64 link IDs")
        values = [
            _safe_id(item, f"owned_directed_link_ids[{index}]")
            for index, item in enumerate(links)
        ]
        if len(values) != len(set(values)):
            _fail("owned_directed_link_ids", "must not contain duplicates")


def validate_message(message: Any) -> Mapping[str, Any]:
    """Validate structure and cross-field semantics; return ``message`` unchanged."""

    obj = _mapping(message, "message")
    message_type = obj.get("message_type")
    if message_type == "hello":
        _validate_handshake(obj, ready=False)
    elif message_type == "ready":
        _validate_handshake(obj, ready=True)
    elif message_type == "query":
        _validate_query(obj)
    elif message_type == "result":
        _validate_result(obj)
    elif message_type == "error":
        _validate_error(obj)
    elif message_type == "disconnect":
        _validate_disconnect(obj)
    else:
        _fail("message_type", f"must be one of {sorted(MESSAGE_TYPES)}")
    return obj


def _reject_duplicate_keys(pairs: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolValidationError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_nonfinite_json(token: str) -> None:
    raise ProtocolValidationError(f"non-finite JSON number is forbidden: {token}")


def encode_message(
    message: Mapping[str, Any], max_bytes: int = DEFAULT_MAX_MESSAGE_BYTES
) -> bytes:
    """Validate and encode one canonical UTF-8 JSONL frame."""

    _integer(max_bytes, "max_bytes", minimum=1)
    validate_message(message)
    try:
        payload = (
            json.dumps(
                message,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError) as exc:
        raise ProtocolValidationError(f"cannot encode message: {exc}") from exc
    if len(payload) > max_bytes:
        raise ProtocolValidationError(
            f"encoded frame is {len(payload)} bytes; limit is {max_bytes}"
        )
    return payload


def decode_message(
    frame: Union[bytes, bytearray, memoryview, str],
    max_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
) -> Mapping[str, Any]:
    """Decode exactly one strict JSONL frame and validate it."""

    _integer(max_bytes, "max_bytes", minimum=1)
    if isinstance(frame, str):
        try:
            raw = frame.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ProtocolValidationError(f"frame is not valid UTF-8: {exc}") from exc
    elif isinstance(frame, (bytes, bytearray, memoryview)):
        raw = bytes(frame)
    else:
        raise ProtocolValidationError("frame must be bytes-like or str")
    if len(raw) > max_bytes:
        raise ProtocolValidationError(
            f"frame is {len(raw)} bytes; limit is {max_bytes}"
        )
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ProtocolValidationError(f"frame is not valid UTF-8: {exc}") from exc
    if text.startswith("\ufeff"):
        raise ProtocolValidationError("UTF-8 BOM is forbidden")
    if text.endswith("\n"):
        text = text[:-1]
    if not text:
        raise ProtocolValidationError("empty JSONL frame")
    if "\n" in text or "\r" in text:
        raise ProtocolValidationError(
            "frame must contain exactly one compact JSON line"
        )
    try:
        message = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite_json,
        )
    except ProtocolValidationError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ProtocolValidationError(f"invalid JSON frame: {exc}") from exc
    return validate_message(message)


def message_sha256(
    frame_or_message: Union[bytes, bytearray, memoryview, str, Mapping[str, Any]],
) -> str:
    """Return the SHA-256 of canonical wire bytes (including the JSONL newline)."""

    if isinstance(frame_or_message, Mapping):
        raw = encode_message(frame_or_message)
    else:
        message = decode_message(frame_or_message)
        raw = encode_message(message)
    return hashlib.sha256(raw).hexdigest()


def node_state_sha256(
    *,
    node_state_seq: int,
    snapshot_monotonic_ns: int,
    source_frame: str,
    transform_version: str,
    nodes: Sequence[Mapping[str, Any]],
    jammers: Sequence[Mapping[str, Any]],
) -> str:
    """Hash one immutable pose snapshot independently of derived age fields."""

    def immutable_entity(source: Mapping[str, Any]) -> Mapping[str, Any]:
        item = copy.deepcopy(dict(source))
        item.pop("freshness_age_ns", None)
        item.pop("stale", None)
        return item

    payload = {
        "schema": "ams.sionna.node_state_snapshot/v1",
        "node_state_seq": node_state_seq,
        "snapshot_monotonic_ns": snapshot_monotonic_ns,
        "source_frame": source_frame,
        "transform_version": transform_version,
        "nodes": [immutable_entity(item) for item in nodes],
        "jammers": [immutable_entity(item) for item in jammers],
    }
    try:
        encoded = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProtocolValidationError(f"invalid node-state snapshot: {exc}") from exc
    return hashlib.sha256(encoded).hexdigest()


def load_protocol_limits(path: Path = DEFAULT_PROTOCOL_CONFIG_PATH) -> ProtocolLimits:
    """Load and strictly validate the checked-in protocol bounds/configuration."""

    try:
        config = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ProtocolValidationError(
            f"cannot load protocol config {path}: {exc}"
        ) from exc
    root = _exact(
        config,
        "protocol_config",
        {
            "acceptance_provider_modes",
            "failure_policy",
            "protocol_name",
            "schema_version",
            "selection",
            "transport",
            "wire_schema",
            "worker",
        },
    )
    _const(root["protocol_name"], "protocol_config.protocol_name", PROTOCOL_NAME)
    _const(root["schema_version"], "protocol_config.schema_version", SCHEMA_VERSION)
    if root["acceptance_provider_modes"] != ["real_sionna"]:
        _fail("protocol_config.acceptance_provider_modes", "must equal ['real_sionna']")
    failure = _exact(
        root["failure_policy"],
        "protocol_config.failure_policy",
        {
            "disconnect",
            "expired_state",
            "hold_last_beyond_expiry",
            "invalid_unidentified_result",
            "queue_overflow",
        },
    )
    expected_failure = {
        "disconnect": "owned_links_unavailable",
        "expired_state": "directed_link_unavailable",
        "hold_last_beyond_expiry": False,
        "invalid_unidentified_result": "all_owned_links_unavailable",
        "queue_overflow": "directed_link_unavailable",
    }
    if dict(failure) != expected_failure:
        _fail(
            "protocol_config.failure_policy",
            "does not match mandatory fail-closed policy",
        )
    transport = _exact(
        root["transport"],
        "protocol_config.transport",
        {"encoding", "framing", "max_message_bytes"},
    )
    _const(transport["encoding"], "protocol_config.transport.encoding", "utf-8")
    _const(transport["framing"], "protocol_config.transport.framing", "json_lines")
    max_bytes = _integer(
        transport["max_message_bytes"],
        "protocol_config.transport.max_message_bytes",
        minimum=1,
    )
    selection = _exact(
        root["selection"],
        "protocol_config.selection",
        {
            "max_links",
            "max_pending_results_per_link",
            "max_query_history",
            "newest_key",
            "require_generation_supersedes_applied",
        },
    )
    if selection["newest_key"] != [
        "node_state_seq",
        "provider_completed_monotonic_ns",
        "wire_sequence",
    ]:
        _fail(
            "protocol_config.selection.newest_key",
            "must use the specified deterministic selection order",
        )
    _const(
        selection["require_generation_supersedes_applied"],
        "protocol_config.selection.require_generation_supersedes_applied",
        True,
    )
    _const(
        root["wire_schema"],
        "protocol_config.wire_schema",
        "network/config/sionna_async_schema_v1.json",
    )
    worker = _exact(
        root["worker"],
        "protocol_config.worker",
        {
            "request_queue_capacity",
            "completion_queue_capacity",
            "max_poll_batch",
            "validity_ttl_ns",
            "max_pose_age_ns",
        },
    )
    return ProtocolLimits(
        max_message_bytes=max_bytes,
        max_links=_integer(
            selection["max_links"], "protocol_config.selection.max_links", minimum=1
        ),
        max_pending_results_per_link=_integer(
            selection["max_pending_results_per_link"],
            "protocol_config.selection.max_pending_results_per_link",
            minimum=1,
        ),
        max_query_history=_integer(
            selection["max_query_history"],
            "protocol_config.selection.max_query_history",
            minimum=1,
        ),
        request_queue_capacity=_integer(
            worker["request_queue_capacity"],
            "protocol_config.worker.request_queue_capacity",
            minimum=1,
        ),
        completion_queue_capacity=_integer(
            worker["completion_queue_capacity"],
            "protocol_config.worker.completion_queue_capacity",
            minimum=1,
        ),
        max_poll_batch=_integer(
            worker["max_poll_batch"], "protocol_config.worker.max_poll_batch", minimum=1
        ),
        validity_ttl_ns=_integer(
            worker["validity_ttl_ns"],
            "protocol_config.worker.validity_ttl_ns",
            minimum=1,
        ),
        max_pose_age_ns=_integer(
            worker["max_pose_age_ns"],
            "protocol_config.worker.max_pose_age_ns",
            minimum=1,
        ),
    )


@dataclass
class _PeerLifecycle:
    generation: int
    last_wire_sequence: int
    state: str


class WireSequenceTracker:
    """Enforce handshake ordering and non-resetting sequence numbers per sender."""

    def __init__(self, identity: ProtocolIdentity):
        self.identity = identity
        self._peers: Dict[str, _PeerLifecycle] = {}

    def observe(self, message: Mapping[str, Any]) -> None:
        validate_message(message)
        if not self.identity.matches(message):
            raise ProtocolStateError("protocol identity mismatch")
        sender = str(message["sender_id"])
        sequence = int(message["wire_sequence"])
        generation = int(message["reconnect_generation"])
        kind = str(message["message_type"])
        previous = self._peers.get(sender)
        if previous is None:
            if kind != "hello":
                raise ProtocolStateError("first message from a sender must be hello")
            next_state = "hello"
        else:
            if sequence == previous.last_wire_sequence:
                raise ProtocolStateError("duplicate wire_sequence")
            if sequence < previous.last_wire_sequence:
                raise ProtocolStateError("out-of-order wire_sequence")
            if generation < previous.generation:
                raise ProtocolStateError("reconnect_generation decreased")
            if generation > previous.generation:
                if generation != previous.generation + 1:
                    raise ProtocolStateError(
                        "reconnect_generation must increase by exactly one"
                    )
                if kind != "hello":
                    raise ProtocolStateError(
                        "a new reconnect generation must begin with hello"
                    )
                next_state = "hello"
            else:
                if kind == "hello":
                    raise ProtocolStateError(
                        "duplicate hello in the same reconnect generation"
                    )
                if previous.state == "closed":
                    raise ProtocolStateError(
                        "closed generation cannot emit more messages"
                    )
                if kind == "ready":
                    if previous.state != "hello":
                        raise ProtocolStateError(
                            "ready must immediately follow the hello lifecycle state"
                        )
                    next_state = "ready"
                elif kind in {"query", "result"}:
                    if previous.state != "ready":
                        raise ProtocolStateError(f"{kind} requires a ready peer")
                    next_state = "ready"
                elif kind == "error":
                    if previous.state not in {"hello", "ready"}:
                        raise ProtocolStateError("error requires an open generation")
                    next_state = previous.state
                elif kind == "disconnect":
                    if previous.state not in {"hello", "ready"}:
                        raise ProtocolStateError(
                            "disconnect requires an open generation"
                        )
                    next_state = "closed"
                else:
                    raise ProtocolStateError(f"unexpected message_type {kind}")
        self._peers[sender] = _PeerLifecycle(generation, sequence, next_state)


@dataclass(frozen=True)
class StateDecision:
    kind: str
    reason: str
    directed_link_id: Optional[str] = None
    query_id: Optional[str] = None
    node_state_seq: Optional[int] = None
    wire_sha256: Optional[str] = None
    applied_state_id: Optional[str] = None


@dataclass(frozen=True)
class AppliedLinkState:
    applied_state_id: str
    directed_link_id: str
    query_id: str
    node_state_seq: int
    result_wire_sequence: int
    provider_completed_monotonic_ns: int
    validity_start_monotonic_ns: int
    expires_monotonic_ns: int
    applied_monotonic_ns: int
    wire_sha256: str
    physical: Mapping[str, Any]


@dataclass(frozen=True)
class _QueryRecord:
    query_id: str
    node_state_seq: int
    directed_link_id: str
    traffic_class: str
    tx_node_id: str
    rx_node_id: str
    phase_id: str
    request_sent_monotonic_ns: int


@dataclass(frozen=True)
class _PendingResult:
    message: Mapping[str, Any]
    wire_sha256: str

    @property
    def newest_key(self) -> Tuple[int, int, int]:
        return (
            int(self.message["node_state_seq"]),
            int(self.message["provider_completed_monotonic_ns"]),
            int(self.message["wire_sequence"]),
        )


@dataclass
class _LinkRecord:
    descriptor: Tuple[str, str, str]
    last_registered_node_state_seq: int = -1
    pending: Dict[str, _PendingResult] = field(default_factory=dict)
    active: Optional[AppliedLinkState] = None
    available: bool = False
    unavailable_reason: str = "no_applied_state"


class DirectedLinkStateManager:
    """Bounded pure state machine for directed per-link Sionna results.

    The caller registers every emitted query, ingests provider frames, and calls
    :meth:`apply_latest` at deterministic adapter boundaries.  Packet processing
    may only use :meth:`state_for_packet`; expired state is never returned.
    """

    def __init__(
        self,
        identity: ProtocolIdentity,
        clock_domain: str,
        limits: Optional[ProtocolLimits] = None,
        provider_sender_id: Optional[str] = None,
    ) -> None:
        _safe_id(clock_domain, "clock_domain")
        if provider_sender_id is not None:
            _safe_id(provider_sender_id, "provider_sender_id")
        self.identity = identity
        self.clock_domain = clock_domain
        self.limits = limits or ProtocolLimits()
        self.provider_sender_id = provider_sender_id
        for name, value in (
            ("max_message_bytes", self.limits.max_message_bytes),
            ("max_links", self.limits.max_links),
            ("max_pending_results_per_link", self.limits.max_pending_results_per_link),
            ("max_query_history", self.limits.max_query_history),
            ("request_queue_capacity", self.limits.request_queue_capacity),
            ("completion_queue_capacity", self.limits.completion_queue_capacity),
            ("max_poll_batch", self.limits.max_poll_batch),
            ("validity_ttl_ns", self.limits.validity_ttl_ns),
            ("max_pose_age_ns", self.limits.max_pose_age_ns),
        ):
            _integer(value, name, minimum=1)
        self._queries: Dict[str, _QueryRecord] = {}
        self._links: Dict[str, _LinkRecord] = {}
        self._result_fingerprints: Dict[str, str] = {}
        self._applied_state_ids: Set[str] = set()

    @property
    def owned_directed_link_ids(self) -> Tuple[str, ...]:
        return tuple(sorted(self._links))

    def register_query(self, query: Mapping[str, Any]) -> StateDecision:
        validate_message(query)
        if query["message_type"] != "query":
            raise ProtocolStateError("register_query requires message_type=query")
        if not self.identity.matches(query):
            raise ProtocolStateError("query protocol identity mismatch")
        if query["sender_clock_domain"] != self.clock_domain:
            self._mark_all_unavailable("query_clock_domain_mismatch")
            raise ProtocolStateError(
                "query sender_clock_domain does not match adapter clock"
            )
        query_id = str(query["query_id"])
        link_id = str(query["directed_link_id"])
        seq = int(query["node_state_seq"])
        if query_id in self._queries:
            self._fail_targets(
                {link_id, self._queries[query_id].directed_link_id},
                "duplicate_query_id",
            )
            raise ProtocolStateError("query_id must be globally unique")
        if len(self._queries) >= self.limits.max_query_history:
            self._mark_all_unavailable("query_history_overflow")
            raise ProtocolStateError(
                "query history bound reached; refusing silent eviction"
            )
        descriptor = (
            str(query["traffic_class"]),
            str(query["tx_node_id"]),
            str(query["rx_node_id"]),
        )
        link = self._links.get(link_id)
        if link is None:
            if len(self._links) >= self.limits.max_links:
                self._mark_all_unavailable("link_bound_exceeded")
                raise ProtocolStateError("directed link bound reached")
            link = _LinkRecord(descriptor=descriptor)
            self._links[link_id] = link
        elif link.descriptor != descriptor:
            self._mark_unavailable(link_id, "link_descriptor_changed")
            raise ProtocolStateError("directed_link_id descriptor changed")
        if seq < link.last_registered_node_state_seq:
            self._mark_unavailable(link_id, "query_node_state_out_of_order")
            raise ProtocolStateError(
                "node_state_seq must not decrease per directed link"
            )
        record = _QueryRecord(
            query_id,
            seq,
            link_id,
            *descriptor,
            str(query["phase_id"]),
            int(query["request_sent_monotonic_ns"]),
        )
        self._queries[query_id] = record
        link.last_registered_node_state_seq = seq
        return StateDecision(
            "query_registered", "query correlation recorded", link_id, query_id, seq
        )

    def ingest_result_wire(
        self,
        frame: Union[bytes, bytearray, memoryview, str],
        received_monotonic_ns: int,
    ) -> StateDecision:
        now = _integer(received_monotonic_ns, "received_monotonic_ns")
        try:
            message = decode_message(frame, max_bytes=self.limits.max_message_bytes)
        except ProtocolValidationError as exc:
            self._mark_all_unavailable("invalid_unidentified_result")
            return StateDecision("invalid", f"invalid/unidentified frame: {exc}")
        canonical_hash = message_sha256(message)
        if message["message_type"] != "result":
            self._mark_all_unavailable("unexpected_message_type")
            return StateDecision(
                "invalid",
                "ingest_result_wire requires a result frame",
                wire_sha256=canonical_hash,
            )
        link_id = str(message["directed_link_id"])
        query_id = str(message["query_id"])
        seq = int(message["node_state_seq"])
        if not self.identity.matches(message):
            self._fail_targets({link_id}, "result_identity_mismatch")
            return StateDecision(
                "invalid",
                "result protocol identity mismatch",
                link_id,
                query_id,
                seq,
                canonical_hash,
            )
        if (
            self.provider_sender_id is not None
            and message["sender_id"] != self.provider_sender_id
        ):
            self._fail_targets({link_id}, "provider_sender_id_mismatch")
            return StateDecision(
                "invalid",
                "unexpected provider sender_id",
                link_id,
                query_id,
                seq,
                canonical_hash,
            )
        result_clock = (
            message["validity_clock_domain"]
            if message["status"] == "ok"
            else message["provider_clock_domain"]
        )
        if result_clock != self.clock_domain:
            self._fail_targets({link_id}, "clock_domain_mismatch")
            return StateDecision(
                "invalid",
                "provider/validity clock domain mismatch",
                link_id,
                query_id,
                seq,
                canonical_hash,
            )
        if int(message["emitted_monotonic_ns"]) > now:
            self._fail_targets({link_id}, "result_received_before_emission")
            return StateDecision(
                "invalid",
                "adapter received result before provider emission",
                link_id,
                query_id,
                seq,
                canonical_hash,
            )
        expected = self._queries.get(query_id)
        if expected is None:
            self._fail_targets({link_id}, "unknown_query_id")
            return StateDecision(
                "invalid", "unknown query_id", link_id, query_id, seq, canonical_hash
            )
        declared_correlation = (
            query_id,
            seq,
            link_id,
            str(message["traffic_class"]),
            str(message["tx_node_id"]),
            str(message["rx_node_id"]),
            str(message["phase_id"]),
        )
        expected_correlation = (
            expected.query_id,
            expected.node_state_seq,
            expected.directed_link_id,
            expected.traffic_class,
            expected.tx_node_id,
            expected.rx_node_id,
            expected.phase_id,
        )
        if declared_correlation != expected_correlation:
            self._fail_targets(
                {link_id, expected.directed_link_id}, "result_correlation_mismatch"
            )
            return StateDecision(
                "invalid",
                "result correlation tuple mismatch",
                expected.directed_link_id,
                query_id,
                seq,
                canonical_hash,
            )
        if (
            int(message["provider_received_monotonic_ns"])
            < expected.request_sent_monotonic_ns
        ):
            self._mark_unavailable(link_id, "provider_received_before_query_sent")
            return StateDecision(
                "invalid",
                "provider received result query before adapter sent it",
                link_id,
                query_id,
                seq,
                canonical_hash,
            )
        seen = self._result_fingerprints.get(query_id)
        if seen is not None:
            if seen == canonical_hash:
                return StateDecision(
                    "duplicate",
                    "identical result already observed",
                    link_id,
                    query_id,
                    seq,
                    canonical_hash,
                )
            self._mark_unavailable(link_id, "conflicting_duplicate_result")
            return StateDecision(
                "invalid",
                "conflicting result for an existing query_id",
                link_id,
                query_id,
                seq,
                canonical_hash,
            )
        if len(self._result_fingerprints) >= self.limits.max_query_history:
            self._mark_all_unavailable("result_history_overflow")
            return StateDecision(
                "invalid",
                "result history bound reached",
                link_id,
                query_id,
                seq,
                canonical_hash,
            )
        self._result_fingerprints[query_id] = canonical_hash
        link = self._links[link_id]
        newest_existing_seq = max(
            ([link.active.node_state_seq] if link.active is not None else [-1])
            + [int(item.message["node_state_seq"]) for item in link.pending.values()]
        )
        if seq < newest_existing_seq:
            return StateDecision(
                "superseded",
                "result is older than a completed link state",
                link_id,
                query_id,
                seq,
                canonical_hash,
            )
        if message["status"] != "ok":
            self._mark_unavailable(link_id, f"provider_{message['status']}")
            return StateDecision(
                "provider_failure",
                str(message["status"]),
                link_id,
                query_id,
                seq,
                canonical_hash,
            )
        expiry = int(message["expires_monotonic_ns"])
        if expiry <= now:
            if link.active is None or link.active.expires_monotonic_ns <= now:
                self._mark_unavailable(
                    link_id,
                    "result_expired_before_receipt",
                    clear_pending=False,
                )
            return StateDecision(
                "expired",
                "result expired before adapter receipt",
                link_id,
                query_id,
                seq,
                canonical_hash,
            )
        candidate_key = (
            seq,
            int(message["provider_completed_monotonic_ns"]),
            int(message["wire_sequence"]),
        )
        active_key = (
            (
                link.active.node_state_seq,
                link.active.provider_completed_monotonic_ns,
                link.active.result_wire_sequence,
            )
            if link.active is not None
            else None
        )
        if active_key is not None and candidate_key <= active_key:
            return StateDecision(
                "superseded",
                "result cannot supersede the applied snapshot/result order",
                link_id,
                query_id,
                seq,
                canonical_hash,
            )
        if len(link.pending) >= self.limits.max_pending_results_per_link:
            link.pending.clear()
            self._mark_unavailable(link_id, "pending_result_queue_overflow")
            return StateDecision(
                "queue_overflow",
                "bounded pending-result queue overflow",
                link_id,
                query_id,
                seq,
                canonical_hash,
            )
        link.pending[query_id] = _PendingResult(copy.deepcopy(message), canonical_hash)
        return StateDecision(
            "pending",
            "validated result queued for deterministic selection",
            link_id,
            query_id,
            seq,
            canonical_hash,
        )

    def apply_latest(
        self,
        directed_link_id: str,
        now_monotonic_ns: int,
        applied_state_id: str,
    ) -> StateDecision:
        link_id = _safe_id(directed_link_id, "directed_link_id")
        now = _integer(now_monotonic_ns, "now_monotonic_ns")
        state_id = _safe_id(applied_state_id, "applied_state_id")
        link = self._links.get(link_id)
        if link is None:
            raise ProtocolStateError("directed link is not owned")
        self._expire_link(link_id, now)
        if state_id in self._applied_state_ids:
            self._mark_unavailable(link_id, "duplicate_applied_state_id")
            raise ProtocolStateError("applied_state_id must be globally unique")
        if len(self._applied_state_ids) >= self.limits.max_query_history:
            self._mark_all_unavailable("applied_state_history_overflow")
            raise ProtocolStateError("applied-state ID history bound reached")

        eligible: List[_PendingResult] = []
        for query_id, pending in list(link.pending.items()):
            message = pending.message
            if int(message["expires_monotonic_ns"]) <= now:
                del link.pending[query_id]
                continue
            if int(message["validity_start_monotonic_ns"]) > now:
                continue
            pending_key = pending.newest_key
            active_key = (
                (
                    link.active.node_state_seq,
                    link.active.provider_completed_monotonic_ns,
                    link.active.result_wire_sequence,
                )
                if link.active is not None
                else None
            )
            if active_key is not None and pending_key <= active_key:
                del link.pending[query_id]
                continue
            eligible.append(pending)
        if not eligible:
            if link.active is None:
                link.available = False
                if link.unavailable_reason in {"", "no_applied_state"}:
                    link.unavailable_reason = "no_fresh_applicable_result"
            return StateDecision("unavailable", link.unavailable_reason, link_id)

        selected = max(eligible, key=lambda item: item.newest_key)
        selected_query = str(selected.message["query_id"])
        selected_key = selected.newest_key
        for query_id, pending in list(link.pending.items()):
            if pending is selected or (
                int(pending.message["validity_start_monotonic_ns"]) <= now
                and pending.newest_key <= selected_key
            ):
                del link.pending[query_id]
        message = selected.message
        active = AppliedLinkState(
            applied_state_id=state_id,
            directed_link_id=link_id,
            query_id=selected_query,
            node_state_seq=int(message["node_state_seq"]),
            result_wire_sequence=int(message["wire_sequence"]),
            provider_completed_monotonic_ns=int(
                message["provider_completed_monotonic_ns"]
            ),
            validity_start_monotonic_ns=int(message["validity_start_monotonic_ns"]),
            expires_monotonic_ns=int(message["expires_monotonic_ns"]),
            applied_monotonic_ns=now,
            wire_sha256=selected.wire_sha256,
            physical=copy.deepcopy(message["physical"]),
        )
        link.active = active
        link.available = True
        link.unavailable_reason = ""
        self._applied_state_ids.add(state_id)
        return StateDecision(
            "applied",
            "newest fresh completed result selected",
            link_id,
            selected_query,
            active.node_state_seq,
            active.wire_sha256,
            state_id,
        )

    def state_for_packet(
        self, directed_link_id: str, packet_monotonic_ns: int
    ) -> Optional[AppliedLinkState]:
        link_id = _safe_id(directed_link_id, "directed_link_id")
        now = _integer(packet_monotonic_ns, "packet_monotonic_ns")
        link = self._links.get(link_id)
        if link is None:
            return None
        self._expire_link(link_id, now)
        active = link.active
        if active is None or not link.available:
            return None
        if not active.validity_start_monotonic_ns <= now < active.expires_monotonic_ns:
            self._mark_unavailable(link_id, "state_outside_validity_window")
            return None
        return active

    def expire(self, now_monotonic_ns: int) -> Tuple[StateDecision, ...]:
        now = _integer(now_monotonic_ns, "now_monotonic_ns")
        decisions: List[StateDecision] = []
        for link_id, link in self._links.items():
            was_active = link.active
            self._expire_link(link_id, now)
            if was_active is not None and link.active is None:
                decisions.append(
                    StateDecision(
                        "expired",
                        "applied state expired; hold-last forbidden",
                        link_id,
                        was_active.query_id,
                        was_active.node_state_seq,
                        was_active.wire_sha256,
                        was_active.applied_state_id,
                    )
                )
        return tuple(decisions)

    def handle_disconnect_wire(
        self,
        frame: Union[bytes, bytearray, memoryview, str],
        received_monotonic_ns: int,
    ) -> Tuple[StateDecision, ...]:
        now = _integer(received_monotonic_ns, "received_monotonic_ns")
        try:
            message = decode_message(frame, max_bytes=self.limits.max_message_bytes)
        except ProtocolValidationError as exc:
            affected = self._mark_all_unavailable("invalid_disconnect")
            return tuple(
                StateDecision("invalid", f"invalid disconnect frame: {exc}", link)
                for link in affected
            )
        if message["message_type"] != "disconnect" or not self.identity.matches(
            message
        ):
            affected = self._mark_all_unavailable(
                "disconnect_identity_or_type_mismatch"
            )
            return tuple(
                StateDecision("invalid", "disconnect identity/type mismatch", link)
                for link in affected
            )
        if int(message["emitted_monotonic_ns"]) > now:
            affected = self._mark_all_unavailable("disconnect_received_before_emission")
            return tuple(
                StateDecision("invalid", "disconnect received before emission", link)
                for link in affected
            )
        declared = message.get("owned_directed_link_ids")
        if declared is not None and not set(declared) <= set(self._links):
            affected = self._mark_all_unavailable("disconnect_ownership_mismatch")
            return tuple(
                StateDecision("invalid", "disconnect declared unowned link IDs", link)
                for link in affected
            )
        targets = set(self._links) if declared is None else set(declared)
        decisions: List[StateDecision] = []
        for link_id in sorted(targets):
            self._mark_unavailable(link_id, "provider_disconnected")
            decisions.append(
                StateDecision("disconnected", str(message["reason"]), link_id)
            )
        return tuple(decisions)

    def link_status(self, directed_link_id: str) -> Tuple[bool, str]:
        link = self._links.get(directed_link_id)
        if link is None:
            return False, "unowned_link"
        return link.available, link.unavailable_reason

    def _expire_link(self, link_id: str, now: int) -> None:
        link = self._links[link_id]
        for query_id, pending in list(link.pending.items()):
            if int(pending.message["expires_monotonic_ns"]) <= now:
                del link.pending[query_id]
        if link.active is not None and link.active.expires_monotonic_ns <= now:
            link.active = None
            link.available = False
            link.unavailable_reason = "applied_state_expired"

    def _mark_unavailable(
        self, link_id: str, reason: str, *, clear_pending: bool = True
    ) -> None:
        link = self._links.get(link_id)
        if link is None:
            return
        link.active = None
        if clear_pending:
            link.pending.clear()
        link.available = False
        link.unavailable_reason = reason

    def _mark_all_unavailable(self, reason: str) -> Tuple[str, ...]:
        affected = tuple(sorted(self._links))
        for link_id in affected:
            self._mark_unavailable(link_id, reason)
        return affected

    def _fail_targets(self, targets: Set[str], reason: str) -> None:
        owned_targets = targets & set(self._links)
        if not owned_targets:
            self._mark_all_unavailable(reason)
            return
        for link_id in owned_targets:
            self._mark_unavailable(link_id, reason)


__all__ = [
    "AppliedLinkState",
    "DEFAULT_PROTOCOL_CONFIG_PATH",
    "DEFAULT_SCHEMA_PATH",
    "DirectedLinkStateManager",
    "ProtocolIdentity",
    "ProtocolLimits",
    "ProtocolStateError",
    "ProtocolValidationError",
    "SCHEMA_VERSION",
    "StateDecision",
    "WireSequenceTracker",
    "decode_message",
    "encode_message",
    "load_protocol_limits",
    "message_sha256",
    "node_state_sha256",
    "validate_message",
]
