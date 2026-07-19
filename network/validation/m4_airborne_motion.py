#!/usr/bin/env python3
"""Independent nonzero-motion gate for the M4 five-UAV flight interval."""

from __future__ import annotations

import bisect
import math
from typing import Any, Iterable, Mapping

from network.validation.m4_common import M4ValidationError


MOTION_CONTRACT = "ams.m4.capacity-gazebo-motion/v1"
EXPECTED_UAVS = tuple(range(1, 6))
MAXIMUM_ODOMETRY_AGE_NS = 1_000_000_000
MINIMUM_MEASUREMENT_PATH_M = 0.50
MINIMUM_MAXIMUM_DISPLACEMENT_M = 0.05
MINIMUM_PEAK_SPEED_MPS = 0.02
MINIMUM_MOVING_SAMPLE_COUNT = 3
VELOCITY_PATH_MAX_RELATIVE_ERROR = 0.35
VELOCITY_PATH_MAX_ABS_ERROR_M = 0.50
ODOMETRY_SOURCE_FRAME = "ros_odometry_world_enu"
COORDINATE_TRANSFORM_VERSION = "ams-m4-coordinate-frames-v1"


def motion_requirements() -> dict[str, Any]:
    """Return the exact contract fields independently recomputed by validation."""

    return {
        "contract": MOTION_CONTRACT,
        "maximum_odometry_age_ns": MAXIMUM_ODOMETRY_AGE_NS,
        "minimum_measurement_path_m": MINIMUM_MEASUREMENT_PATH_M,
        "minimum_maximum_displacement_m": MINIMUM_MAXIMUM_DISPLACEMENT_M,
        "minimum_peak_speed_mps": MINIMUM_PEAK_SPEED_MPS,
        "minimum_moving_sample_count": MINIMUM_MOVING_SAMPLE_COUNT,
        "velocity_path_max_relative_error": VELOCITY_PATH_MAX_RELATIVE_ERROR,
        "velocity_path_max_abs_error_m": VELOCITY_PATH_MAX_ABS_ERROR_M,
        "measurement_interval": "[measurement_start,measurement_end)",
        "source_event": "odometry_sample",
        "source_topic_pattern": "/uavN/odometry",
        "source_frame": ODOMETRY_SOURCE_FRAME,
        "transform_version": COORDINATE_TRANSFORM_VERSION,
    }


def _finite_vector(value: Any, *, size: int, label: str) -> tuple[float, ...]:
    if not isinstance(value, list) or len(value) != size:
        raise M4ValidationError(f"{label} is not an exact {size}-vector")
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in value):
        raise M4ValidationError(f"{label} contains a nonnumeric value")
    result = tuple(float(item) for item in value)
    if not all(math.isfinite(item) for item in result):
        raise M4ValidationError(f"{label} contains a non-finite value")
    return result


def validate_measurement_motion(
    records: Iterable[Mapping[str, Any]],
    *,
    start_ns: int,
    end_ns: int,
    declared_requirements: Mapping[str, Any],
) -> dict[str, Any]:
    """Derive per-UAV motion from the collector's raw odometry stream.

    Position change and reported linear velocity are both required, so neither
    a frozen elevated pose stream nor fabricated nonzero twist alone can pass.
    """

    if (
        isinstance(start_ns, bool)
        or not isinstance(start_ns, int)
        or isinstance(end_ns, bool)
        or not isinstance(end_ns, int)
        or start_ns <= 0
        or end_ns <= start_ns
        or dict(declared_requirements) != motion_requirements()
    ):
        raise M4ValidationError("capacity Gazebo motion contract differs")

    common_keys = {
        "schema",
        "event_sequence",
        "run_id",
        "runtime_id",
        "host_monotonic_ns",
        "host_realtime_ns",
        "event",
        "uav",
        "source_topic",
        "source_frame",
        "transform_version",
        "source_callback_monotonic_ns",
        "sim_stamp_ns",
        "position_m",
        "orientation_quat_xyzw",
        "linear_velocity_mps",
        "angular_velocity_radps",
    }
    histories: dict[int, list[dict[str, Any]]] = {
        uav: [] for uav in EXPECTED_UAVS
    }
    for record in records:
        if record.get("event") != "odometry_sample":
            continue
        uav_label = record.get("uav")
        if not isinstance(uav_label, str) or not uav_label.startswith("uav"):
            raise M4ValidationError("odometry sample UAV identity differs")
        try:
            uav = int(uav_label[3:])
        except ValueError as exc:
            raise M4ValidationError("odometry sample UAV identity differs") from exc
        callback_ns = record.get("source_callback_monotonic_ns")
        host_ns = record.get("host_monotonic_ns")
        sim_ns = record.get("sim_stamp_ns")
        if (
            set(record) != common_keys
            or uav not in EXPECTED_UAVS
            or record.get("source_topic") != f"/uav{uav}/odometry"
            or record.get("source_frame") != ODOMETRY_SOURCE_FRAME
            or record.get("transform_version")
            != COORDINATE_TRANSFORM_VERSION
            or isinstance(callback_ns, bool)
            or not isinstance(callback_ns, int)
            or isinstance(host_ns, bool)
            or not isinstance(host_ns, int)
            or isinstance(sim_ns, bool)
            or not isinstance(sim_ns, int)
            or callback_ns <= 0
            or sim_ns < 0
            or not callback_ns <= host_ns <= callback_ns + 100_000_000
        ):
            raise M4ValidationError("odometry sample identity/clock/keys differ")
        histories[uav].append(
            {
                "callback_ns": callback_ns,
                "sim_ns": sim_ns,
                "position": _finite_vector(
                    record.get("position_m"), size=3, label="odometry position"
                ),
                "orientation": _finite_vector(
                    record.get("orientation_quat_xyzw"),
                    size=4,
                    label="odometry orientation",
                ),
                "linear_velocity": _finite_vector(
                    record.get("linear_velocity_mps"),
                    size=3,
                    label="odometry linear velocity",
                ),
                "angular_velocity": _finite_vector(
                    record.get("angular_velocity_radps"),
                    size=3,
                    label="odometry angular velocity",
                ),
            }
        )

    per_uav: dict[str, dict[str, Any]] = {}
    for uav in EXPECTED_UAVS:
        history = histories[uav]
        times = [int(item["callback_ns"]) for item in history]
        sim_times = [int(item["sim_ns"]) for item in history]
        if (
            not history
            or times != sorted(times)
            or len(times) != len(set(times))
            or any(right < left for left, right in zip(sim_times, sim_times[1:]))
        ):
            raise M4ValidationError(f"uav{uav} odometry stream is absent/nonmonotonic")
        baseline_index = bisect.bisect_right(times, start_ns) - 1
        interval_start_index = bisect.bisect_left(times, start_ns)
        end_index = bisect.bisect_left(times, end_ns)
        if baseline_index < 0:
            raise M4ValidationError(f"uav{uav} odometry lacks measurement-start bracket")
        # A sample immediately before the half-open measurement interval is
        # continuity evidence only.  Excluding it from the distance metrics
        # prevents pre-window motion from being credited to the 600-s gate.
        interval = history[interval_start_index:end_index]
        interval_times = [int(item["callback_ns"]) for item in interval]
        if (
            not interval
            or times[baseline_index] > start_ns
            or start_ns - times[baseline_index] > MAXIMUM_ODOMETRY_AGE_NS
            or interval_times[0] - start_ns > MAXIMUM_ODOMETRY_AGE_NS
            or end_ns - interval_times[-1] > MAXIMUM_ODOMETRY_AGE_NS
            or any(
                not 0 < right - left <= MAXIMUM_ODOMETRY_AGE_NS
                for left, right in zip(interval_times, interval_times[1:])
            )
        ):
            raise M4ValidationError(
                f"uav{uav} odometry does not continuously cover measurement"
            )
        positions = [item["position"] for item in interval]
        speeds = [math.dist((0.0, 0.0, 0.0), item["linear_velocity"]) for item in interval]
        path_m = sum(
            math.dist(left, right) for left, right in zip(positions, positions[1:])
        )
        velocity_integral_m = sum(
            0.5 * (left_speed + right_speed) * ((right_ns - left_ns) / 1e9)
            for left_speed, right_speed, left_ns, right_ns in zip(
                speeds,
                speeds[1:],
                interval_times,
                interval_times[1:],
            )
        )
        velocity_path_error_m = abs(velocity_integral_m - path_m)
        velocity_path_tolerance_m = max(
            VELOCITY_PATH_MAX_ABS_ERROR_M,
            VELOCITY_PATH_MAX_RELATIVE_ERROR
            * max(path_m, velocity_integral_m),
        )
        maximum_displacement_m = max(
            math.dist(positions[0], position) for position in positions
        )
        peak_speed_mps = max(speeds)
        moving_samples = sum(
            speed >= MINIMUM_PEAK_SPEED_MPS for speed in speeds
        )
        if (
            path_m < MINIMUM_MEASUREMENT_PATH_M
            or maximum_displacement_m < MINIMUM_MAXIMUM_DISPLACEMENT_M
            or peak_speed_mps < MINIMUM_PEAK_SPEED_MPS
            or moving_samples < MINIMUM_MOVING_SAMPLE_COUNT
            or velocity_path_error_m > velocity_path_tolerance_m
        ):
            raise M4ValidationError(f"uav{uav} lacks contract-bound nonzero flight motion")
        per_uav[f"uav{uav}"] = {
            "sample_count": len(interval),
            "maximum_sample_gap_ns": max(
                (right - left for left, right in zip(interval_times, interval_times[1:])),
                default=0,
            ),
            "measurement_path_m": round(path_m, 6),
            "maximum_displacement_m": round(maximum_displacement_m, 6),
            "peak_speed_mps": round(peak_speed_mps, 6),
            "moving_sample_count": moving_samples,
            "velocity_integral_m": round(velocity_integral_m, 6),
            "velocity_path_error_m": round(velocity_path_error_m, 6),
        }

    return {
        "contract": MOTION_CONTRACT,
        "uav_count": len(per_uav),
        "measurement_start_monotonic_ns": start_ns,
        "measurement_end_monotonic_ns": end_ns,
        "per_uav": per_uav,
    }
