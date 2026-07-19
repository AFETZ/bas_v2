#!/usr/bin/env python3
"""Compact lossless M4 pose-observation evidence stream.

The ROS collector receives pose topics much faster than the bounded runtime
health log needs them.  This module keeps every source occurrence in a separate
gzip JSONL stream while allowing validators to retain only exact keys referenced
by Sionna queries.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

from network.scripts.m4_gazebo_pose_source import (
    GAZEBO_WORLD_POSE_STAMP_SCOPE,
    GAZEBO_WORLD_POSE_TRANSPORT,
)
from network.validation.m4_common import (
    M4ValidationError,
    finite_number,
    regular_file,
)


POSE_OBSERVATION_STREAM_PATH = Path("logs/m4_pose_observations.jsonl.gz")
POSE_OBSERVATION_STREAM_SCHEMA = "ams.m4.pose_observation_stream/v1"
POSE_OBSERVATION_STREAM_ENCODING = "gzip-jsonl-compact-v1"
POSE_OBSERVATION_BUFFER_BYTES = 1_048_576
POSE_OBSERVATION_MAX_LINE_BYTES = 8_192
POSE_OBSERVATION_KINDS = {"o", "w"}
POSE_OBSERVATION_UAVS = {f"uav{index}" for index in range(1, 6)}
POSE_OBSERVATION_WORLD_ENTITIES = {"cp", "jammer_m4"}
ODOMETRY_SOURCE_TRANSPORT = "ros2_dds_odometry"
ODOMETRY_SOURCE_STAMP_SCOPE = "ros_header"
GAZEBO_POSE_SOURCE_TRANSPORT = GAZEBO_WORLD_POSE_TRANSPORT
GAZEBO_POSE_SOURCE_STAMP_SCOPE = GAZEBO_WORLD_POSE_STAMP_SCOPE
HEADER_KEYS = {
    "schema",
    "encoding",
    "run_id",
    "runtime_id",
    "created_monotonic_ns",
}
OBSERVATION_KEYS = {
    "q",
    "k",
    "e",
    "n",
    "m",
    "t",
    "r",
    "s",
    "f",
    "v",
    "h",
    "c",
    "p",
    "o",
}
FOOTER_KEYS = {
    "stream_end",
    "observation_count",
    "maximum_source_callback_monotonic_ns",
    "closed_monotonic_ns",
    "content_sha256",
}


def _canonical_line(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _unique_object(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise M4ValidationError(f"duplicate pose-observation key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise M4ValidationError(f"non-finite pose-observation value {value}")


def _decode_line(raw: bytes, label: str) -> dict[str, Any]:
    if not raw or len(raw) > POSE_OBSERVATION_MAX_LINE_BYTES or not raw.endswith(b"\n"):
        raise M4ValidationError(f"{label} framing/size differs")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except M4ValidationError:
        raise
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise M4ValidationError(f"{label} JSON differs: {exc}") from exc
    if not isinstance(value, dict):
        raise M4ValidationError(f"{label} is not an object")
    return value


def _vector(value: Any, size: int, label: str) -> tuple[float, ...]:
    if (
        not isinstance(value, list)
        or len(value) != size
        or any(not finite_number(item) for item in value)
    ):
        raise M4ValidationError(f"{label} vector differs")
    return tuple(float(item) for item in value)


def pose_observation_exact_key(
    *,
    kind: str,
    entity_id: str,
    source_topic: str,
    source_transport: str,
    source_stamp_scope: str,
    source_frame: str,
    transform_version: str,
    source_header_frame: str,
    source_child_frame: str,
    sim_stamp_ns: int,
    position_m: tuple[float, ...],
    orientation_quat_xyzw: tuple[float, ...],
) -> tuple[Any, ...]:
    event = "odometry_sample" if kind == "o" else "world_pose_sample"
    return (
        entity_id,
        event,
        source_topic,
        source_transport,
        source_stamp_scope,
        source_frame,
        transform_version,
        source_header_frame,
        source_child_frame,
        sim_stamp_ns,
        position_m,
        orientation_quat_xyzw,
    )


def _validate_observation(
    record: Mapping[str, Any],
    *,
    expected_sequence: int,
    created_monotonic_ns: int,
) -> tuple[tuple[Any, ...], int]:
    if set(record) != OBSERVATION_KEYS:
        raise M4ValidationError(
            "pose observation keys differ: "
            f"missing={sorted(OBSERVATION_KEYS-set(record))} "
            f"extra={sorted(set(record)-OBSERVATION_KEYS)}"
        )
    sequence = record.get("q")
    kind = record.get("k")
    entity = record.get("e")
    callback_ns = record.get("n")
    sim_stamp_ns = record.get("m")
    topic = record.get("t")
    source_transport = record.get("r")
    source_stamp_scope = record.get("s")
    source_frame = record.get("f")
    transform = record.get("v")
    header_frame = record.get("h")
    child_frame = record.get("c")
    if (
        isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or sequence != expected_sequence
        or kind not in POSE_OBSERVATION_KINDS
        or not isinstance(entity, str)
        or isinstance(callback_ns, bool)
        or not isinstance(callback_ns, int)
        or callback_ns <= 0
        or callback_ns < created_monotonic_ns
        or isinstance(sim_stamp_ns, bool)
        or not isinstance(sim_stamp_ns, int)
        or sim_stamp_ns < 0
        or any(
            not isinstance(value, str) or not value
            for value in (
                topic,
                source_transport,
                source_stamp_scope,
                source_frame,
                transform,
                child_frame,
            )
        )
        or not isinstance(header_frame, str)
    ):
        raise M4ValidationError(
            f"pose observation {expected_sequence} identity/order differs"
        )
    child_parts = [part for part in child_frame.strip("/").split("/") if part]
    if kind == "o":
        if (
            entity not in POSE_OBSERVATION_UAVS
            or topic != f"/{entity}/odometry"
            or source_transport != ODOMETRY_SOURCE_TRANSPORT
            or source_stamp_scope != ODOMETRY_SOURCE_STAMP_SCOPE
            or source_frame != "ros_odometry_world_enu"
            or transform != "ams-m4-coordinate-frames-v1"
            or header_frame != "odom"
            or child_frame != "base_link"
        ):
            raise M4ValidationError(
                f"pose observation {expected_sequence} odometry lineage differs"
            )
    elif (
        entity not in POSE_OBSERVATION_WORLD_ENTITIES
        or topic != "/world/map/pose/info"
        or source_transport != GAZEBO_POSE_SOURCE_TRANSPORT
        or source_stamp_scope != GAZEBO_POSE_SOURCE_STAMP_SCOPE
        or source_frame != "world"
        or transform != "enu-identity-v1"
        or header_frame != ""
        or not child_parts
        or child_parts[-1] != entity
    ):
        raise M4ValidationError(
            f"pose observation {expected_sequence} world lineage differs"
        )
    position = _vector(record.get("p"), 3, f"pose observation {expected_sequence}")
    orientation = _vector(record.get("o"), 4, f"pose observation {expected_sequence}")
    return (
        pose_observation_exact_key(
            kind=str(kind),
            entity_id=entity,
            source_topic=topic,
            source_transport=source_transport,
            source_stamp_scope=source_stamp_scope,
            source_frame=source_frame,
            transform_version=transform,
            source_header_frame=header_frame,
            source_child_frame=child_frame,
            sim_stamp_ns=sim_stamp_ns,
            position_m=position,
            orientation_quat_xyzw=orientation,
        ),
        callback_ns,
    )


class PoseObservationWriter:
    """Buffered exclusive writer with a clean-close completeness footer."""

    def __init__(
        self,
        path: Path,
        run_id: str,
        runtime_id: str,
        *,
        created_monotonic_ns: int | None = None,
    ) -> None:
        if not run_id or not runtime_id:
            raise M4ValidationError("pose-observation stream identity is empty")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.run_id = run_id
        self.runtime_id = runtime_id
        self.sequence = 0
        self.maximum_callback_ns = 0
        self._closed = False
        self._lock = threading.Lock()
        self._raw = path.open("xb", buffering=0)
        self._gzip = gzip.GzipFile(
            filename="",
            mode="wb",
            compresslevel=1,
            fileobj=self._raw,
            mtime=0,
        )
        self._buffer = io.BufferedWriter(
            self._gzip, buffer_size=POSE_OBSERVATION_BUFFER_BYTES
        )
        self._content_digest = hashlib.sha256()
        created_ns = (
            time.monotonic_ns()
            if created_monotonic_ns is None
            else created_monotonic_ns
        )
        if (
            isinstance(created_ns, bool)
            or not isinstance(created_ns, int)
            or created_ns <= 0
        ):
            self._abort()
            raise M4ValidationError("pose-observation creation time differs")
        self.created_monotonic_ns = created_ns
        header = _canonical_line(
            {
                "schema": POSE_OBSERVATION_STREAM_SCHEMA,
                "encoding": POSE_OBSERVATION_STREAM_ENCODING,
                "run_id": run_id,
                "runtime_id": runtime_id,
                "created_monotonic_ns": created_ns,
            }
        )
        self._buffer.write(header)
        self._content_digest.update(header)

    @property
    def observation_count(self) -> int:
        return self.sequence

    @property
    def content_sha256(self) -> str:
        return self._content_digest.hexdigest()

    def emit(
        self,
        *,
        kind: str,
        entity_id: str,
        source_callback_monotonic_ns: int,
        sim_stamp_ns: int,
        source_topic: str,
        source_transport: str,
        source_stamp_scope: str,
        source_frame: str,
        transform_version: str,
        source_header_frame: str,
        source_child_frame: str,
        position_m: list[float],
        orientation_quat_xyzw: list[float],
    ) -> None:
        with self._lock:
            if self._closed:
                raise M4ValidationError("pose-observation stream is already closed")
            record = {
                "q": self.sequence + 1,
                "k": kind,
                "e": entity_id,
                "n": source_callback_monotonic_ns,
                "m": sim_stamp_ns,
                "t": source_topic,
                "r": source_transport,
                "s": source_stamp_scope,
                "f": source_frame,
                "v": transform_version,
                "h": source_header_frame,
                "c": source_child_frame,
                "p": position_m,
                "o": orientation_quat_xyzw,
            }
            _key, callback_ns = _validate_observation(
                record,
                expected_sequence=self.sequence + 1,
                created_monotonic_ns=self.created_monotonic_ns,
            )
            encoded = _canonical_line(record)
            self._buffer.write(encoded)
            self._content_digest.update(encoded)
            self.sequence += 1
            self.maximum_callback_ns = max(self.maximum_callback_ns, callback_ns)

    def close(self, *, closed_monotonic_ns: int) -> None:
        with self._lock:
            if self._closed:
                return
            if (
                isinstance(closed_monotonic_ns, bool)
                or not isinstance(closed_monotonic_ns, int)
                or closed_monotonic_ns <= 0
                or closed_monotonic_ns < self.maximum_callback_ns
                or closed_monotonic_ns < self.created_monotonic_ns
            ):
                raise M4ValidationError("pose-observation close time differs")
            self._buffer.write(
                _canonical_line(
                    {
                        "stream_end": True,
                        "observation_count": self.sequence,
                        "maximum_source_callback_monotonic_ns": self.maximum_callback_ns,
                        "closed_monotonic_ns": closed_monotonic_ns,
                        "content_sha256": self._content_digest.hexdigest(),
                    }
                )
            )
            try:
                self._buffer.close()
                self._raw.flush()
                os.fsync(self._raw.fileno())
            finally:
                self._raw.close()
                self._closed = True

    def _abort(self) -> None:
        try:
            self._buffer.close()
        finally:
            self._raw.close()
            self._closed = True


def scan_pose_observation_stream(
    path: Path,
    *,
    run_id: str,
    runtime_id: str,
    required_keys: set[tuple[Any, ...]],
) -> tuple[
    dict[tuple[Any, ...], list[tuple[int, int]]],
    dict[str, Any],
]:
    """Strictly scan once and retain only occurrences needed by query poses."""

    if not regular_file(path):
        raise M4ValidationError(
            "pose-observation stream is missing/nonregular/hardlinked"
        )
    matches: dict[tuple[Any, ...], list[tuple[int, int]]] = defaultdict(list)
    observation_count = 0
    odometry_count = 0
    world_pose_count = 0
    maximum_callback_ns = 0
    footer: dict[str, Any] | None = None
    content_digest = hashlib.sha256()
    created_monotonic_ns = 0
    try:
        with path.open("rb", buffering=0) as raw:
            with gzip.GzipFile(fileobj=raw, mode="rb") as compressed:
                raw_header = compressed.readline(
                    POSE_OBSERVATION_MAX_LINE_BYTES + 1
                )
                header = _decode_line(
                    raw_header,
                    "pose-observation header",
                )
                if (
                    set(header) != HEADER_KEYS
                    or header.get("schema") != POSE_OBSERVATION_STREAM_SCHEMA
                    or header.get("encoding") != POSE_OBSERVATION_STREAM_ENCODING
                    or header.get("run_id") != run_id
                    or header.get("runtime_id") != runtime_id
                    or isinstance(header.get("created_monotonic_ns"), bool)
                    or not isinstance(header.get("created_monotonic_ns"), int)
                    or int(header["created_monotonic_ns"]) <= 0
                ):
                    raise M4ValidationError(
                        "pose-observation header schema/identity differs"
                    )
                if raw_header != _canonical_line(header):
                    raise M4ValidationError(
                        "pose-observation header encoding is noncanonical"
                    )
                content_digest.update(raw_header)
                created_monotonic_ns = int(header["created_monotonic_ns"])
                while True:
                    raw_line = compressed.readline(
                        POSE_OBSERVATION_MAX_LINE_BYTES + 1
                    )
                    if not raw_line:
                        break
                    record = _decode_line(
                        raw_line,
                        f"pose observation {observation_count + 1}",
                    )
                    if raw_line != _canonical_line(record):
                        raise M4ValidationError(
                            f"pose observation {observation_count + 1} encoding is noncanonical"
                        )
                    if record.get("stream_end") is True:
                        footer = record
                        if compressed.readline(2):
                            raise M4ValidationError(
                                "pose-observation stream has data after footer"
                            )
                        break
                    key, callback_ns = _validate_observation(
                        record,
                        expected_sequence=observation_count + 1,
                        created_monotonic_ns=created_monotonic_ns,
                    )
                    observation_count += 1
                    maximum_callback_ns = max(maximum_callback_ns, callback_ns)
                    content_digest.update(raw_line)
                    if record["k"] == "o":
                        odometry_count += 1
                    else:
                        world_pose_count += 1
                    if key in required_keys:
                        matches[key].append((observation_count, callback_ns))
    except M4ValidationError:
        raise
    except (EOFError, OSError, TypeError, ValueError) as exc:
        raise M4ValidationError(f"pose-observation stream cannot be read: {exc}") from exc
    if (
        footer is None
        or set(footer) != FOOTER_KEYS
        or footer.get("stream_end") is not True
        or isinstance(footer.get("observation_count"), bool)
        or not isinstance(footer.get("observation_count"), int)
        or footer.get("observation_count") != observation_count
        or isinstance(footer.get("maximum_source_callback_monotonic_ns"), bool)
        or not isinstance(footer.get("maximum_source_callback_monotonic_ns"), int)
        or footer.get("maximum_source_callback_monotonic_ns")
        != maximum_callback_ns
        or not isinstance(footer.get("content_sha256"), str)
        or len(footer["content_sha256"]) != 64
        or footer.get("content_sha256") != content_digest.hexdigest()
        or isinstance(footer.get("closed_monotonic_ns"), bool)
        or not isinstance(footer.get("closed_monotonic_ns"), int)
        or int(footer["closed_monotonic_ns"])
        < max(maximum_callback_ns, created_monotonic_ns)
    ):
        raise M4ValidationError(
            "pose-observation clean-close footer/count differs"
        )
    return dict(matches), {
        "stream_schema": POSE_OBSERVATION_STREAM_SCHEMA,
        "stream_encoding": POSE_OBSERVATION_STREAM_ENCODING,
        "created_monotonic_ns": created_monotonic_ns,
        "closed_monotonic_ns": int(footer["closed_monotonic_ns"]),
        "content_sha256": content_digest.hexdigest(),
        "compressed_size_bytes": path.stat().st_size,
        "observation_count": observation_count,
        "odometry_observation_count": odometry_count,
        "world_pose_observation_count": world_pose_count,
        "required_exact_key_count": len(required_keys),
        "matching_observation_count": sum(len(items) for items in matches.values()),
    }


__all__ = [
    "GAZEBO_POSE_SOURCE_STAMP_SCOPE",
    "GAZEBO_POSE_SOURCE_TRANSPORT",
    "ODOMETRY_SOURCE_STAMP_SCOPE",
    "ODOMETRY_SOURCE_TRANSPORT",
    "POSE_OBSERVATION_BUFFER_BYTES",
    "POSE_OBSERVATION_STREAM_ENCODING",
    "POSE_OBSERVATION_STREAM_PATH",
    "POSE_OBSERVATION_STREAM_SCHEMA",
    "PoseObservationWriter",
    "pose_observation_exact_key",
    "scan_pose_observation_stream",
]
