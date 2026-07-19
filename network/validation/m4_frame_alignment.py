#!/usr/bin/env python3
"""Independent runtime Gazebo/ROS/ArduPilot frame-correspondence gate."""

from __future__ import annotations

import bisect
import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from network.validation.m4_common import M4ValidationError


FRAME_CONTRACT = "ams.m4.coordinate-frame-contract/v1"
FRAME_TRANSFORM_VERSION = "ams-m4-coordinate-frames-v1"
EXPECTED_UAVS = tuple(range(1, 6))
ROS_ODOMETRY_SOURCE_FRAME = "ros_odometry_world_enu"
ROS_ODOMETRY_TOPIC_PATTERN = "/uavN/odometry"
NED_DELTA_TO_ENU_DELTA_3X3 = (
    (0.0, 1.0, 0.0),
    (1.0, 0.0, 0.0),
    (0.0, 0.0, -1.0),
)
MAXIMUM_SAMPLE_SKEW_NS = 1_000_000_000
GLOBAL_HORIZONTAL_MAX_ABS_ERROR_M = 5.0
LOCAL_POSITION_MAX_ABS_ERROR_M = 3.0
RELATIVE_ALTITUDE_MAX_ABS_ERROR_M = 3.0
WGS84_ORIGIN = {
    "datum": "WGS84",
    "elevation_m": 0.0,
    "heading_deg": 0.0,
    "latitude_deg": -35.3632621,
    "longitude_deg": 149.1652374,
}
WGS84_SEMI_MAJOR_AXIS_M = 6_378_137.0
WGS84_FLATTENING = 1.0 / 298.257_223_563


def _finite_vector(value: Any, label: str) -> tuple[float, float, float]:
    if not isinstance(value, list) or len(value) != 3:
        raise M4ValidationError(f"{label} is not an exact three-vector")
    if any(
        isinstance(item, bool) or not isinstance(item, (int, float))
        for item in value
    ):
        raise M4ValidationError(f"{label} contains a nonnumeric value")
    converted = tuple(float(item) for item in value)
    if not all(math.isfinite(item) for item in converted):
        raise M4ValidationError(f"{label} contains a non-finite value")
    return converted  # type: ignore[return-value]


def _strict_time(record: Mapping[str, Any], field: str, label: str) -> int:
    value = record.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise M4ValidationError(f"{label} timestamp differs")
    return value


def _latest_at_or_before(
    history: Sequence[Mapping[str, Any]], timestamp_ns: int, *, field: str
) -> Mapping[str, Any] | None:
    times = [int(record[field]) for record in history]
    index = bisect.bisect_right(times, timestamp_ns) - 1
    return history[index] if index >= 0 else None


def _geodetic_surface_to_ecef(
    latitude_deg: float, longitude_deg: float
) -> tuple[float, float, float]:
    """Convert a WGS84 surface coordinate to Earth-centred Cartesian metres."""

    latitude_rad = math.radians(latitude_deg)
    longitude_rad = math.radians(longitude_deg)
    sin_latitude = math.sin(latitude_rad)
    cos_latitude = math.cos(latitude_rad)
    sin_longitude = math.sin(longitude_rad)
    cos_longitude = math.cos(longitude_rad)
    eccentricity_squared = WGS84_FLATTENING * (2.0 - WGS84_FLATTENING)
    prime_vertical_radius = WGS84_SEMI_MAJOR_AXIS_M / math.sqrt(
        1.0 - eccentricity_squared * sin_latitude * sin_latitude
    )
    return (
        prime_vertical_radius * cos_latitude * cos_longitude,
        prime_vertical_radius * cos_latitude * sin_longitude,
        prime_vertical_radius * (1.0 - eccentricity_squared) * sin_latitude,
    )


def _wgs84_surface_to_origin_enu(
    lat_e7: int, lon_e7: int
) -> tuple[float, float]:
    """Project GLOBAL_POSITION_INT WGS84 lat/lon into the frozen ENU tangent plane."""

    if not -900_000_000 <= lat_e7 <= 900_000_000:
        raise M4ValidationError("GLOBAL_POSITION_INT latitude is outside WGS84")
    if not -1_800_000_000 <= lon_e7 <= 1_800_000_000:
        raise M4ValidationError("GLOBAL_POSITION_INT longitude is outside WGS84")
    latitude_deg = lat_e7 * 1.0e-7
    longitude_deg = lon_e7 * 1.0e-7
    point = _geodetic_surface_to_ecef(latitude_deg, longitude_deg)
    origin = _geodetic_surface_to_ecef(
        float(WGS84_ORIGIN["latitude_deg"]),
        float(WGS84_ORIGIN["longitude_deg"]),
    )
    delta_x, delta_y, delta_z = (
        point[axis] - origin[axis] for axis in range(3)
    )
    origin_latitude = math.radians(float(WGS84_ORIGIN["latitude_deg"]))
    origin_longitude = math.radians(float(WGS84_ORIGIN["longitude_deg"]))
    east_m = (
        -math.sin(origin_longitude) * delta_x
        + math.cos(origin_longitude) * delta_y
    )
    north_m = (
        -math.sin(origin_latitude) * math.cos(origin_longitude) * delta_x
        - math.sin(origin_latitude) * math.sin(origin_longitude) * delta_y
        + math.cos(origin_latitude) * delta_z
    )
    return east_m, north_m


def _validate_declared_contract(contract: Mapping[str, Any]) -> None:
    frames = contract.get("frames")
    transforms = contract.get("transforms")
    runtime = contract.get("runtime_correspondence")
    if (
        contract.get("contract") != FRAME_CONTRACT
        or contract.get("transform_version") != FRAME_TRANSFORM_VERSION
        or not isinstance(frames, Mapping)
        or not isinstance(transforms, Mapping)
        or not isinstance(runtime, Mapping)
    ):
        raise M4ValidationError("runtime coordinate-frame contract identity differs")
    ros = frames.get("ros_odometry")
    local = frames.get("ardupilot_local_position_ned")
    global_position = frames.get("ardupilot_global_position_int")
    if (
        not isinstance(ros, Mapping)
        or ros.get("frame_id") != ROS_ODOMETRY_SOURCE_FRAME
        or ros.get("source_topic_pattern") != ROS_ODOMETRY_TOPIC_PATTERN
        or ros.get("axes") != ["east", "north", "up"]
        or ros.get("handedness") != "right"
        or ros.get("position_unit") != "m"
        or ros.get("quaternion_order") != "xyzw"
        or not isinstance(local, Mapping)
        or local.get("mavlink_message") != "LOCAL_POSITION_NED"
        or local.get("mavlink_message_id") != 32
        or local.get("axes") != ["north", "east", "down"]
        or local.get("handedness") != "right"
        or local.get("position_unit") != "m"
        or local.get("linear_velocity_unit") != "m/s"
        or not isinstance(global_position, Mapping)
        or global_position.get("mavlink_message") != "GLOBAL_POSITION_INT"
        or global_position.get("mavlink_message_id") != 33
        or global_position.get("datum") != "WGS84"
        or global_position.get("latitude_longitude_unit") != "1e-7_deg"
        or global_position.get("relative_altitude_unit") != "mm"
    ):
        raise M4ValidationError("runtime coordinate-frame definitions differ")
    local_transform = transforms.get(
        "ardupilot_local_ned_delta_to_gazebo_enu_delta"
    )
    global_transform = transforms.get("gazebo_world_enu_to_wgs84")
    altitude_transform = transforms.get(
        "global_relative_altitude_to_gazebo_enu_up_delta"
    )
    if (
        not isinstance(local_transform, Mapping)
        or local_transform.get("matrix_3x3")
        != [list(row) for row in NED_DELTA_TO_ENU_DELTA_3X3]
        or local_transform.get("version") != FRAME_TRANSFORM_VERSION
        or not isinstance(global_transform, Mapping)
        or global_transform.get("kind") != "gazebo_spherical_coordinates"
        or global_transform.get("surface_model") != "EARTH_WGS84"
        or global_transform.get("origin") != WGS84_ORIGIN
        or global_transform.get("version") != FRAME_TRANSFORM_VERSION
        or not isinstance(altitude_transform, Mapping)
        or altitude_transform.get("scale_m_per_input_unit") != 0.001
        or altitude_transform.get("version") != FRAME_TRANSFORM_VERSION
        or dict(runtime)
        != {
            "comparison_interval": "[measurement_start,measurement_end)",
            "matching_policy": "nearest_at_or_before",
            "maximum_sample_skew_ns": MAXIMUM_SAMPLE_SKEW_NS,
            "baseline_policy": "per_uav_observed_prearm_deltas",
            "global_horizontal_max_abs_error_m": (
                GLOBAL_HORIZONTAL_MAX_ABS_ERROR_M
            ),
            "local_position_max_abs_error_m": LOCAL_POSITION_MAX_ABS_ERROR_M,
            "relative_altitude_max_abs_error_m": (
                RELATIVE_ALTITUDE_MAX_ABS_ERROR_M
            ),
        }
    ):
        raise M4ValidationError("runtime coordinate transform/tolerances differ")


def validate_runtime_frame_correspondence(
    odometry_records: Iterable[Mapping[str, Any]],
    *,
    local_position_histories: Mapping[int, Sequence[Mapping[str, Any]]],
    global_position_histories: Mapping[int, Sequence[Mapping[str, Any]]],
    prearm_monotonic_ns: int,
    measurement_start_ns: int,
    measurement_end_ns: int,
    declared_frame_contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Prove synchronized per-UAV local/global ArduPilot-to-ENU correspondence.

    Absolute ArduPilot estimator origins are vehicle-specific.  The comparison
    consequently subtracts fresh raw pre-arm baselines in all three domains,
    then applies the exact right-handed NED-to-ENU delta transform.  Absolute
    GLOBAL_POSITION_INT latitude/longitude is independently projected from
    WGS84 into the Gazebo origin's ENU tangent plane.
    """

    _validate_declared_contract(declared_frame_contract)
    if (
        isinstance(prearm_monotonic_ns, bool)
        or not isinstance(prearm_monotonic_ns, int)
        or isinstance(measurement_start_ns, bool)
        or not isinstance(measurement_start_ns, int)
        or isinstance(measurement_end_ns, bool)
        or not isinstance(measurement_end_ns, int)
        or not 0 < prearm_monotonic_ns < measurement_start_ns < measurement_end_ns
    ):
        raise M4ValidationError("runtime frame-correspondence interval differs")
    if set(local_position_histories) != set(EXPECTED_UAVS) or set(
        global_position_histories
    ) != set(EXPECTED_UAVS):
        raise M4ValidationError("runtime frame-correspondence UAV set differs")

    odometry: dict[int, list[dict[str, Any]]] = {
        uav: [] for uav in EXPECTED_UAVS
    }
    for record in odometry_records:
        if record.get("event") != "odometry_sample":
            continue
        label = record.get("uav")
        if not isinstance(label, str) or not label.startswith("uav"):
            raise M4ValidationError("runtime odometry UAV label differs")
        try:
            uav = int(label[3:])
        except ValueError as exc:
            raise M4ValidationError("runtime odometry UAV label differs") from exc
        callback_ns = _strict_time(
            record, "source_callback_monotonic_ns", "runtime odometry"
        )
        if (
            uav not in EXPECTED_UAVS
            or record.get("source_topic") != f"/uav{uav}/odometry"
            or record.get("source_frame") != ROS_ODOMETRY_SOURCE_FRAME
            or record.get("transform_version") != FRAME_TRANSFORM_VERSION
        ):
            raise M4ValidationError("runtime odometry frame/topic lineage differs")
        odometry[uav].append(
            {
                "timestamp_ns": callback_ns,
                "position": _finite_vector(
                    record.get("position_m"), "runtime odometry position"
                ),
            }
        )

    per_uav: dict[str, dict[str, Any]] = {}
    for uav in EXPECTED_UAVS:
        poses = odometry[uav]
        local_history = list(local_position_histories[uav])
        global_history = list(global_position_histories[uav])
        for label, history in (
            ("local", local_history),
            ("global", global_history),
        ):
            times = [
                _strict_time(record, "received_monotonic_ns", f"uav{uav} {label}")
                for record in history
            ]
            # Multiple MAVLink frames decoded from one UDP recvmsg legitimately
            # share the same receive timestamp.  Histories must therefore be
            # nondecreasing, not strictly increasing.
            if not times or times != sorted(times):
                raise M4ValidationError(
                    f"uav{uav} {label} frame history is absent/nonmonotonic"
                )
        pose_times = [int(record["timestamp_ns"]) for record in poses]
        if (
            not poses
            or pose_times != sorted(pose_times)
            or len(pose_times) != len(set(pose_times))
        ):
            raise M4ValidationError(
                f"uav{uav} odometry frame history is absent/nonmonotonic"
            )

        pose_baseline = _latest_at_or_before(
            poses, prearm_monotonic_ns, field="timestamp_ns"
        )
        local_baseline = _latest_at_or_before(
            local_history, prearm_monotonic_ns, field="received_monotonic_ns"
        )
        global_baseline = _latest_at_or_before(
            global_history, prearm_monotonic_ns, field="received_monotonic_ns"
        )
        baselines = (pose_baseline, local_baseline, global_baseline)
        baseline_times = (
            pose_baseline.get("timestamp_ns") if pose_baseline else None,
            local_baseline.get("received_monotonic_ns") if local_baseline else None,
            global_baseline.get("received_monotonic_ns") if global_baseline else None,
        )
        if any(
            baseline is None
            or isinstance(timestamp, bool)
            or not isinstance(timestamp, int)
            or not 0 <= prearm_monotonic_ns - timestamp <= MAXIMUM_SAMPLE_SKEW_NS
            for baseline, timestamp in zip(baselines, baseline_times)
        ):
            raise M4ValidationError(f"uav{uav} lacks synchronized pre-arm baselines")
        assert pose_baseline and local_baseline and global_baseline
        pose_baseline_position = tuple(pose_baseline["position"])
        local_baseline_position = (
            float(local_baseline["x_m"]),
            float(local_baseline["y_m"]),
            float(local_baseline["z_down_m"]),
        )
        if not all(math.isfinite(value) for value in local_baseline_position):
            raise M4ValidationError(f"uav{uav} local pre-arm baseline is non-finite")
        global_baseline_alt = global_baseline.get("relative_alt_mm")
        if isinstance(global_baseline_alt, bool) or not isinstance(
            global_baseline_alt, int
        ):
            raise M4ValidationError(f"uav{uav} global pre-arm baseline differs")

        selected_poses = [
            record
            for record in poses
            if measurement_start_ns
            <= int(record["timestamp_ns"])
            < measurement_end_ns
        ]
        selected_times = [int(record["timestamp_ns"]) for record in selected_poses]
        if (
            not selected_poses
            or selected_times[0] - measurement_start_ns > MAXIMUM_SAMPLE_SKEW_NS
            or measurement_end_ns - selected_times[-1] > MAXIMUM_SAMPLE_SKEW_NS
            or any(
                right - left > MAXIMUM_SAMPLE_SKEW_NS
                for left, right in zip(selected_times, selected_times[1:])
            )
        ):
            raise M4ValidationError(
                f"uav{uav} frame correspondence does not cover measurement"
            )

        maximum_local_error = 0.0
        maximum_global_horizontal_error = 0.0
        maximum_altitude_error = 0.0
        for pose in selected_poses:
            timestamp_ns = int(pose["timestamp_ns"])
            local = _latest_at_or_before(
                local_history, timestamp_ns, field="received_monotonic_ns"
            )
            global_position = _latest_at_or_before(
                global_history, timestamp_ns, field="received_monotonic_ns"
            )
            if local is None or global_position is None:
                raise M4ValidationError(f"uav{uav} lacks synchronized flight state")
            local_ns = _strict_time(
                local, "received_monotonic_ns", f"uav{uav} local"
            )
            global_ns = _strict_time(
                global_position, "received_monotonic_ns", f"uav{uav} global"
            )
            if (
                timestamp_ns - local_ns > MAXIMUM_SAMPLE_SKEW_NS
                or timestamp_ns - global_ns > MAXIMUM_SAMPLE_SKEW_NS
            ):
                raise M4ValidationError(
                    f"uav{uav} flight/Gazebo frame sample skew exceeds contract"
                )
            local_position = (
                float(local["x_m"]),
                float(local["y_m"]),
                float(local["z_down_m"]),
            )
            if not all(math.isfinite(value) for value in local_position):
                raise M4ValidationError(f"uav{uav} local position is non-finite")
            ned_delta = tuple(
                value - baseline
                for value, baseline in zip(
                    local_position, local_baseline_position
                )
            )
            expected_enu_delta = tuple(
                sum(
                    row[column] * ned_delta[column] for column in range(3)
                )
                for row in NED_DELTA_TO_ENU_DELTA_3X3
            )
            observed_enu_delta = tuple(
                value - baseline
                for value, baseline in zip(
                    pose["position"], pose_baseline_position
                )
            )
            local_error = max(
                abs(observed - expected)
                for observed, expected in zip(
                    observed_enu_delta, expected_enu_delta
                )
            )
            relative_alt_mm = global_position.get("relative_alt_mm")
            if isinstance(relative_alt_mm, bool) or not isinstance(
                relative_alt_mm, int
            ):
                raise M4ValidationError(f"uav{uav} relative altitude differs")
            expected_up_delta = (
                relative_alt_mm - global_baseline_alt
            ) * 0.001
            altitude_error = abs(observed_enu_delta[2] - expected_up_delta)
            lat_e7 = global_position.get("lat_e7")
            lon_e7 = global_position.get("lon_e7")
            if (
                isinstance(lat_e7, bool)
                or not isinstance(lat_e7, int)
                or isinstance(lon_e7, bool)
                or not isinstance(lon_e7, int)
            ):
                raise M4ValidationError(
                    f"uav{uav} GLOBAL_POSITION_INT WGS84 coordinate differs"
                )
            global_east_m, global_north_m = _wgs84_surface_to_origin_enu(
                lat_e7, lon_e7
            )
            global_horizontal_error = max(
                abs(float(pose["position"][0]) - global_east_m),
                abs(float(pose["position"][1]) - global_north_m),
            )
            maximum_local_error = max(maximum_local_error, local_error)
            maximum_global_horizontal_error = max(
                maximum_global_horizontal_error, global_horizontal_error
            )
            maximum_altitude_error = max(
                maximum_altitude_error, altitude_error
            )
            if local_error > LOCAL_POSITION_MAX_ABS_ERROR_M:
                raise M4ValidationError(
                    f"uav{uav} LOCAL_POSITION_NED/Gazebo correspondence differs"
                )
            if global_horizontal_error > GLOBAL_HORIZONTAL_MAX_ABS_ERROR_M:
                raise M4ValidationError(
                    f"uav{uav} GLOBAL_POSITION_INT/Gazebo horizontal "
                    "correspondence differs"
                )
            if altitude_error > RELATIVE_ALTITUDE_MAX_ABS_ERROR_M:
                raise M4ValidationError(
                    f"uav{uav} GLOBAL_POSITION_INT/Gazebo altitude differs"
                )
        per_uav[f"uav{uav}"] = {
            "sample_count": len(selected_poses),
            "maximum_local_position_abs_error_m": round(
                maximum_local_error, 6
            ),
            "maximum_global_horizontal_abs_error_m": round(
                maximum_global_horizontal_error, 6
            ),
            "maximum_relative_altitude_abs_error_m": round(
                maximum_altitude_error, 6
            ),
        }

    return {
        "contract": "ams.m4.runtime-frame-correspondence/v1",
        "transform_version": FRAME_TRANSFORM_VERSION,
        "uav_count": len(per_uav),
        "measurement_start_monotonic_ns": measurement_start_ns,
        "measurement_end_monotonic_ns": measurement_end_ns,
        "per_uav": per_uav,
    }


__all__ = [
    "FRAME_CONTRACT",
    "FRAME_TRANSFORM_VERSION",
    "GLOBAL_HORIZONTAL_MAX_ABS_ERROR_M",
    "LOCAL_POSITION_MAX_ABS_ERROR_M",
    "MAXIMUM_SAMPLE_SKEW_NS",
    "NED_DELTA_TO_ENU_DELTA_3X3",
    "RELATIVE_ALTITUDE_MAX_ABS_ERROR_M",
    "ROS_ODOMETRY_SOURCE_FRAME",
    "WGS84_ORIGIN",
    "validate_runtime_frame_correspondence",
]
