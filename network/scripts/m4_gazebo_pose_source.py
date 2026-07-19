#!/usr/bin/env python3
"""Strict native Gazebo ``Pose_V`` source for M4 world entities.

The Gazebo Python bindings are intentionally imported only when a source is
constructed.  Pure decoding therefore remains usable in qualification tests
and on hosts that do not have Gazebo installed.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, TypeAlias

from network.validation.m4_common import M4ValidationError


GAZEBO_WORLD_POSE_TOPIC = "/world/map/pose/info"
GAZEBO_WORLD_POSE_TRANSPORT = "gazebo_transport_pose_v"
GAZEBO_WORLD_POSE_STAMP_SCOPE = "pose_v_top_level_header"
GAZEBO_WORLD_POSE_SOURCE_FRAME = "world"
GAZEBO_WORLD_POSE_TRANSFORM_VERSION = "enu-identity-v1"
_RECOGNIZED_ENTITIES = (
    "cp",
    "uav1",
    "uav2",
    "uav3",
    "uav4",
    "uav5",
    "jammer_m4",
)
_REQUIRED_ENTITIES = ("cp", "jammer_m4")


@dataclass(frozen=True, slots=True)
class GazeboWorldPoseObservation:
    """One callback-time pose with its complete native-source lineage."""

    entity_id: str
    source_callback_monotonic_ns: int
    sim_stamp_ns: int
    source_topic: str
    source_transport: str
    source_stamp_scope: str
    source_frame: str
    transform_version: str
    source_header_frame: str
    source_child_frame: str
    position_m: tuple[float, float, float]
    orientation_quat_xyzw: tuple[float, float, float, float]


GazeboWorldPoseBatch: TypeAlias = tuple[GazeboWorldPoseObservation, ...]


def _present_message_field(value: Any, field: str) -> bool:
    """Honor protobuf message presence while supporting simple test doubles."""

    has_field = getattr(value, "HasField", None)
    if callable(has_field):
        try:
            return bool(has_field(field))
        except (TypeError, ValueError):
            # A non-protobuf test double, or a scalar proto3 field, has no
            # useful presence API for this field.  Attribute presence remains
            # the strict fallback.
            pass
    return hasattr(value, field) and getattr(value, field) is not None


def _strict_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise M4ValidationError(f"{label} must be an integer")
    return value


def _top_level_stamp_ns(message: Any) -> int:
    if not _present_message_field(message, "header"):
        raise M4ValidationError("Pose_V top-level header is missing")
    header = message.header
    if not _present_message_field(header, "stamp"):
        raise M4ValidationError("Pose_V top-level header.stamp is missing")
    stamp = header.stamp
    if not hasattr(stamp, "sec") or not hasattr(stamp, "nsec"):
        raise M4ValidationError(
            "Pose_V top-level header.stamp must expose sec and nsec"
        )
    sec = _strict_integer(stamp.sec, "Pose_V top-level header.stamp.sec")
    nsec = _strict_integer(stamp.nsec, "Pose_V top-level header.stamp.nsec")
    if sec < 0 or not 0 <= nsec < 1_000_000_000:
        raise M4ValidationError("Pose_V top-level header.stamp is out of range")
    if not hasattr(header, "data"):
        raise M4ValidationError("Pose_V top-level header.data is missing")
    try:
        data_count = len(header.data)
    except TypeError as exc:
        raise M4ValidationError(
            "Pose_V top-level header.data is not a repeated field"
        ) from exc
    if data_count != 0:
        raise M4ValidationError(
            "Pose_V top-level header.data must match the live empty contract"
        )
    return sec * 1_000_000_000 + nsec


def _empty_pose_header(header: Any) -> bool:
    """Return whether a per-pose header carries no independent lineage."""

    if header is None:
        return True
    list_fields = getattr(header, "ListFields", None)
    if callable(list_fields):
        try:
            populated = {descriptor.name: value for descriptor, value in list_fields()}
        except (TypeError, ValueError):
            return False
        if set(populated) - {"stamp", "data"}:
            return False
        if "data" in populated and len(populated["data"]) != 0:
            return False
        if "stamp" not in populated:
            return True
        stamp = populated["stamp"]
        return (
            isinstance(getattr(stamp, "sec", None), int)
            and not isinstance(stamp.sec, bool)
            and isinstance(getattr(stamp, "nsec", None), int)
            and not isinstance(stamp.nsec, bool)
            and stamp.sec == 0
            and stamp.nsec == 0
            and not (
                callable(getattr(stamp, "ListFields", None))
                and any(
                    descriptor.name not in {"sec", "nsec"}
                    for descriptor, _value in stamp.ListFields()
                )
            )
        )

    values = vars(header) if hasattr(header, "__dict__") else None
    if values is None:
        return False
    if set(values) - {"stamp", "data"}:
        return False
    if "data" in values:
        try:
            if len(values["data"]) != 0:
                return False
        except TypeError:
            return False
    if "stamp" not in values or values["stamp"] is None:
        return True
    stamp = values["stamp"]
    if not hasattr(stamp, "sec") or not hasattr(stamp, "nsec"):
        return False
    sec = getattr(stamp, "sec")
    nsec = getattr(stamp, "nsec")
    return (
        isinstance(sec, int)
        and not isinstance(sec, bool)
        and isinstance(nsec, int)
        and not isinstance(nsec, bool)
        and sec == 0
        and nsec == 0
    )


def _require_empty_pose_header(pose: Any, entity_id: str) -> None:
    if not _present_message_field(pose, "header"):
        return
    if not _empty_pose_header(pose.header):
        raise M4ValidationError(
            f"Pose_V {entity_id} per-pose header must be absent or empty"
        )


def _finite_tuple(value: Any, fields: tuple[str, ...], label: str) -> tuple[float, ...]:
    output: list[float] = []
    for field in fields:
        if not hasattr(value, field):
            raise M4ValidationError(f"{label}.{field} is missing")
        component = getattr(value, field)
        if isinstance(component, bool) or not isinstance(component, (int, float)):
            raise M4ValidationError(f"{label}.{field} is not numeric")
        converted = float(component)
        if not math.isfinite(converted):
            raise M4ValidationError(f"{label}.{field} is not finite")
        output.append(converted)
    return tuple(output)


def decode_pose_v(
    message: Any, *, callback_monotonic_ns: int | None = None
) -> GazeboWorldPoseBatch:
    """Decode recognized world entities in one canonical deterministic order."""

    callback_ns = (
        time.monotonic_ns()
        if callback_monotonic_ns is None
        else _strict_integer(callback_monotonic_ns, "callback_monotonic_ns")
    )
    if callback_ns <= 0:
        raise M4ValidationError("callback_monotonic_ns must be positive")
    sim_stamp_ns = _top_level_stamp_ns(message)

    if not hasattr(message, "pose"):
        raise M4ValidationError("Pose_V pose field is missing")
    try:
        poses = tuple(message.pose)
    except TypeError as exc:
        raise M4ValidationError("Pose_V pose field is not repeated") from exc

    selected: dict[str, Any] = {}
    for pose in poses:
        name = getattr(pose, "name", None)
        if name not in _RECOGNIZED_ENTITIES:
            continue
        if name in selected:
            raise M4ValidationError(f"Pose_V has duplicate {name} pose")
        selected[name] = pose
    missing = [entity for entity in _REQUIRED_ENTITIES if entity not in selected]
    if missing:
        raise M4ValidationError(
            f"Pose_V is missing required pose entities: {','.join(missing)}"
        )

    observations: list[GazeboWorldPoseObservation] = []
    for entity_id in _RECOGNIZED_ENTITIES:
        if entity_id not in selected:
            continue
        pose = selected[entity_id]
        _require_empty_pose_header(pose, entity_id)
        if not hasattr(pose, "position") or not hasattr(pose, "orientation"):
            raise M4ValidationError(f"Pose_V {entity_id} pose geometry is missing")
        position = _finite_tuple(
            pose.position, ("x", "y", "z"), f"Pose_V {entity_id} position"
        )
        orientation = _finite_tuple(
            pose.orientation,
            ("x", "y", "z", "w"),
            f"Pose_V {entity_id} orientation",
        )
        observations.append(
            GazeboWorldPoseObservation(
                entity_id=entity_id,
                source_callback_monotonic_ns=callback_ns,
                sim_stamp_ns=sim_stamp_ns,
                source_topic=GAZEBO_WORLD_POSE_TOPIC,
                source_transport=GAZEBO_WORLD_POSE_TRANSPORT,
                source_stamp_scope=GAZEBO_WORLD_POSE_STAMP_SCOPE,
                source_frame=GAZEBO_WORLD_POSE_SOURCE_FRAME,
                transform_version=GAZEBO_WORLD_POSE_TRANSFORM_VERSION,
                source_header_frame="",
                source_child_frame=entity_id,
                position_m=(position[0], position[1], position[2]),
                orientation_quat_xyzw=(
                    orientation[0],
                    orientation[1],
                    orientation[2],
                    orientation[3],
                ),
            )
        )
    return tuple(observations)


def _load_gazebo_bindings() -> tuple[type[Any], type[Any]]:
    try:
        from gz.msgs10.pose_v_pb2 import Pose_V
        from gz.transport13 import Node
    except Exception as exc:
        raise M4ValidationError(
            f"native Gazebo Pose_V transport bindings are required: {exc}"
        ) from exc
    return Node, Pose_V


class GazeboPoseVSource:
    """Subscribe to the canonical native Pose_V topic and fail closed."""

    def __init__(self, callback: Callable[[GazeboWorldPoseBatch], None]) -> None:
        if not callable(callback):
            raise TypeError("callback must be callable")
        self._callback = callback
        self._lock = threading.RLock()
        self._failure: Exception | None = None
        self._closed = False
        node_type, pose_v_type = _load_gazebo_bindings()
        try:
            self._node = node_type()
            subscribed = self._node.subscribe(
                pose_v_type, GAZEBO_WORLD_POSE_TOPIC, self._on_message
            )
        except Exception as exc:
            raise M4ValidationError(
                f"failed to subscribe to {GAZEBO_WORLD_POSE_TOPIC}: {exc}"
            ) from exc
        # gz.transport13.Node.subscribe() returns None on success in the
        # qualification image.  Some test/older bindings return bool instead.
        if subscribed is False:
            raise M4ValidationError(
                f"Gazebo rejected subscription to {GAZEBO_WORLD_POSE_TOPIC}"
            )

    def _on_message(self, message: Any) -> None:
        callback_ns = time.monotonic_ns()
        with self._lock:
            if self._closed or self._failure is not None:
                return
            try:
                observations = decode_pose_v(
                    message, callback_monotonic_ns=callback_ns
                )
                self._callback(observations)
            except Exception as exc:
                self._failure = exc

    def raise_if_failed(self) -> None:
        with self._lock:
            failure = self._failure
        if failure is not None:
            raise failure

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            unsubscribed = self._node.unsubscribe(GAZEBO_WORLD_POSE_TOPIC)
        except Exception as exc:
            raise M4ValidationError(
                f"failed to unsubscribe from {GAZEBO_WORLD_POSE_TOPIC}: {exc}"
            ) from exc
        if unsubscribed is False:
            raise M4ValidationError(
                f"Gazebo rejected unsubscribe from {GAZEBO_WORLD_POSE_TOPIC}"
            )
