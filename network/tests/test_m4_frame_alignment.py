from __future__ import annotations

import copy
import math
import unittest

from network.validation.m4_common import M4ValidationError
from network.validation.m4_frame_alignment import (
    FRAME_TRANSFORM_VERSION,
    ROS_ODOMETRY_SOURCE_FRAME,
    validate_runtime_frame_correspondence,
)
from network.validation.validate_m4_scene_bundle import (
    expected_coordinate_frame_contract,
)


class M4RuntimeFrameAlignmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.prearm_ns = 5_000_000_000
        self.start_ns = 10_000_000_000
        self.end_ns = 14_000_000_000
        self.odometry, self.local, self.global_position = self._records()

    @staticmethod
    def _enu_to_wgs84_e7(east_m: float, north_m: float) -> tuple[int, int]:
        """Independent small-distance WGS84 curvature conversion for fixtures."""

        latitude_deg = -35.3632621
        longitude_deg = 149.1652374
        latitude_rad = math.radians(latitude_deg)
        semi_major_axis_m = 6_378_137.0
        flattening = 1.0 / 298.257_223_563
        eccentricity_squared = flattening * (2.0 - flattening)
        denominator = 1.0 - eccentricity_squared * math.sin(latitude_rad) ** 2
        meridian_radius_m = (
            semi_major_axis_m
            * (1.0 - eccentricity_squared)
            / denominator**1.5
        )
        prime_vertical_radius_m = semi_major_axis_m / math.sqrt(denominator)
        latitude = latitude_deg + math.degrees(north_m / meridian_radius_m)
        longitude = longitude_deg + math.degrees(
            east_m / (prime_vertical_radius_m * math.cos(latitude_rad))
        )
        return round(latitude * 1.0e7), round(longitude * 1.0e7)

    def _records(self) -> tuple[
        list[dict[str, object]],
        dict[int, list[dict[str, object]]],
        dict[int, list[dict[str, object]]],
    ]:
        odometry: list[dict[str, object]] = []
        local: dict[int, list[dict[str, object]]] = {
            uav: [] for uav in range(1, 6)
        }
        global_position: dict[int, list[dict[str, object]]] = {
            uav: [] for uav in range(1, 6)
        }
        sample_times = [self.prearm_ns, *range(self.start_ns, self.end_ns, 500_000_000)]
        for sample, timestamp_ns in enumerate(sample_times):
            delta = 0.0 if sample == 0 else float(sample)
            for uav in range(1, 6):
                baseline = (uav * 100.0, uav * 50.0, uav * 10.0)
                # ENU delta [east, north, up] corresponds exactly to NED
                # delta [north, east, down] under [y, x, -z].
                enu_delta = (2.0 * delta, delta, 3.0 * delta)
                ned_delta = (delta, 2.0 * delta, -3.0 * delta)
                odometry.append(
                    {
                        "event": "odometry_sample",
                        "uav": f"uav{uav}",
                        "source_callback_monotonic_ns": timestamp_ns,
                        "source_topic": f"/uav{uav}/odometry",
                        "source_frame": ROS_ODOMETRY_SOURCE_FRAME,
                        "transform_version": FRAME_TRANSFORM_VERSION,
                        "position_m": [
                            baseline[axis] + enu_delta[axis] for axis in range(3)
                        ],
                    }
                )
                local[uav].append(
                    {
                        "received_monotonic_ns": timestamp_ns,
                        "x_m": ned_delta[0],
                        "y_m": ned_delta[1],
                        "z_down_m": ned_delta[2],
                    }
                )
                global_position[uav].append(
                    {
                        "received_monotonic_ns": timestamp_ns,
                        "lat_e7": self._enu_to_wgs84_e7(
                            baseline[0] + enu_delta[0],
                            baseline[1] + enu_delta[1],
                        )[0],
                        "lon_e7": self._enu_to_wgs84_e7(
                            baseline[0] + enu_delta[0],
                            baseline[1] + enu_delta[1],
                        )[1],
                        "relative_alt_mm": round(enu_delta[2] * 1000.0),
                    }
                )
        return odometry, local, global_position

    def _validate(
        self,
        *,
        odometry: list[dict[str, object]] | None = None,
        local: dict[int, list[dict[str, object]]] | None = None,
        global_position: dict[int, list[dict[str, object]]] | None = None,
        contract: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return validate_runtime_frame_correspondence(
            self.odometry if odometry is None else odometry,
            local_position_histories=self.local if local is None else local,
            global_position_histories=(
                self.global_position if global_position is None else global_position
            ),
            prearm_monotonic_ns=self.prearm_ns,
            measurement_start_ns=self.start_ns,
            measurement_end_ns=self.end_ns,
            declared_frame_contract=(
                expected_coordinate_frame_contract()
                if contract is None
                else contract
            ),
        )

    def test_exact_five_uav_synchronized_frame_correspondence_passes(self) -> None:
        result = self._validate()
        self.assertEqual(result["uav_count"], 5)
        self.assertEqual(
            set(result["per_uav"]), {f"uav{uav}" for uav in range(1, 6)}
        )
        self.assertTrue(
            all(
                item["maximum_local_position_abs_error_m"] == 0.0
                and item["maximum_relative_altitude_abs_error_m"] == 0.0
                and item["maximum_global_horizontal_abs_error_m"] < 0.1
                for item in result["per_uav"].values()
            )
        )

    def test_duplicate_receive_timestamp_from_one_datagram_is_allowed(self) -> None:
        local = copy.deepcopy(self.local)
        global_position = copy.deepcopy(self.global_position)
        local[1].insert(2, copy.deepcopy(local[1][1]))
        global_position[1].insert(2, copy.deepcopy(global_position[1][1]))
        result = self._validate(local=local, global_position=global_position)
        self.assertEqual(result["uav_count"], 5)

    def test_local_ned_axis_swap_is_rejected(self) -> None:
        local = copy.deepcopy(self.local)
        for record in local[3][1:]:
            record["x_m"], record["y_m"] = record["y_m"], record["x_m"]
        with self.assertRaisesRegex(
            M4ValidationError, "LOCAL_POSITION_NED/Gazebo"
        ):
            self._validate(local=local)

    def test_relative_altitude_disagreement_is_rejected(self) -> None:
        global_position = copy.deepcopy(self.global_position)
        for record in global_position[4][1:]:
            record["relative_alt_mm"] = int(record["relative_alt_mm"]) + 10_000
        with self.assertRaisesRegex(
            M4ValidationError, "GLOBAL_POSITION_INT/Gazebo"
        ):
            self._validate(global_position=global_position)

    def test_absolute_wgs84_horizontal_disagreement_is_rejected(self) -> None:
        global_position = copy.deepcopy(self.global_position)
        for record in global_position[2][1:]:
            record["lon_e7"] = int(record["lon_e7"]) + 10_000
        with self.assertRaisesRegex(
            M4ValidationError, "GLOBAL_POSITION_INT/Gazebo horizontal"
        ):
            self._validate(global_position=global_position)

    def test_stale_flight_state_is_rejected(self) -> None:
        local = copy.deepcopy(self.local)
        local[2] = [local[2][0]]
        with self.assertRaisesRegex(M4ValidationError, "sample skew"):
            self._validate(local=local)

    def test_wrong_odometry_frame_or_topic_is_rejected(self) -> None:
        odometry = copy.deepcopy(self.odometry)
        record = next(item for item in odometry if item["uav"] == "uav5")
        record["source_frame"] = "unversioned_world"
        with self.assertRaisesRegex(M4ValidationError, "frame/topic lineage"):
            self._validate(odometry=odometry)

    def test_declared_tolerance_or_transform_mutation_is_rejected(self) -> None:
        contract = expected_coordinate_frame_contract()
        contract["runtime_correspondence"][
            "local_position_max_abs_error_m"
        ] = 30.0
        with self.assertRaisesRegex(M4ValidationError, "transform/tolerances"):
            self._validate(contract=contract)


if __name__ == "__main__":
    unittest.main()
