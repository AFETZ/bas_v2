from __future__ import annotations

import copy
import math
import unittest

from network.scripts.collect_m4_runtime import (
    angular_velocity_from_quaternion_delta,
)
from network.validation.m4_airborne_motion import (
    ANGULAR_VELOCITY_METHOD,
    motion_requirements,
    validate_measurement_motion,
)
from network.validation.m4_common import M4ValidationError


class M4AirborneMotionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.start_ns = 10_000_000_000
        self.end_ns = self.start_ns + 600_000_000_000
        self.records = self._records()

    def _records(self) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        sequence = 0
        for sample in range(1200):
            callback_ns = self.start_ns + sample * 500_000_000
            for uav in range(1, 6):
                sequence += 1
                records.append(
                    {
                        "schema": "ams.m4.runtime-event/v1",
                        "event_sequence": sequence,
                        "run_id": "motion-test",
                        "runtime_id": "runtime-test",
                        "host_monotonic_ns": callback_ns + 1_000_000,
                        "host_realtime_ns": 1_000_000_000_000 + callback_ns,
                        "event": "odometry_sample",
                        "uav": f"uav{uav}",
                        "source_topic": f"/uav{uav}/odometry",
                        "source_frame": "ros_odometry_world_enu",
                        "transform_version": "ams-m4-coordinate-frames-v1",
                        "source_header_frame": "odom",
                        "source_child_frame": "base_link",
                        "source_callback_monotonic_ns": callback_ns,
                        "sim_stamp_ns": (sample + 1) * 500_000_000,
                        "position_m": [
                            # 0.01 m per 0.5 s is exactly the declared
                            # 0.02 m/s velocity; the validator deliberately
                            # binds displacement to raw twist evidence.
                            float(uav * 100) + sample * 0.01,
                            float(uav * 50),
                            20.0,
                        ],
                        "orientation_quat_xyzw": [0.0, 0.0, 0.0, 1.0],
                        "linear_velocity_mps": [0.02, 0.0, 0.0],
                        "angular_velocity_radps": [0.0, 0.0, 0.0],
                        "angular_velocity_method": ANGULAR_VELOCITY_METHOD,
                        "angular_velocity_from_sim_stamp_ns": sample
                        * 500_000_000,
                        "angular_velocity_dt_ns": 500_000_000,
                    }
                )
        return records

    def _validate(self, records: list[dict[str, object]] | None = None) -> dict[str, object]:
        return validate_measurement_motion(
            self.records if records is None else records,
            start_ns=self.start_ns,
            end_ns=self.end_ns,
            declared_requirements=motion_requirements(),
        )

    def test_five_continuous_moving_odometry_streams_pass(self) -> None:
        result = self._validate()
        self.assertEqual(result["uav_count"], 5)
        self.assertEqual(set(result["per_uav"]), {f"uav{uav}" for uav in range(1, 6)})
        self.assertTrue(
            all(item["measurement_path_m"] > 1.0 for item in result["per_uav"].values())
        )

    def test_frozen_elevated_positions_fail_even_with_reported_velocity(self) -> None:
        records = copy.deepcopy(self.records)
        for record in records:
            record["position_m"] = [100.0, 100.0, 20.0]
        with self.assertRaisesRegex(M4ValidationError, "nonzero flight motion"):
            self._validate(records)

    def test_position_changes_fail_when_raw_velocity_is_zero(self) -> None:
        records = copy.deepcopy(self.records)
        for record in records:
            record["linear_velocity_mps"] = [0.0, 0.0, 0.0]
        with self.assertRaisesRegex(M4ValidationError, "nonzero flight motion"):
            self._validate(records)

    def test_measurement_gap_or_missing_uav_fails(self) -> None:
        gap_start = self.start_ns + 200_000_000_000
        gap_end = gap_start + 2_000_000_000
        records = [
            record
            for record in self.records
            if not (
                record["uav"] == "uav3"
                and gap_start
                <= int(record["source_callback_monotonic_ns"])
                < gap_end
            )
        ]
        with self.assertRaisesRegex(M4ValidationError, "continuously cover"):
            self._validate(records)
        with self.assertRaisesRegex(M4ValidationError, "absent"):
            self._validate([record for record in self.records if record["uav"] != "uav5"])

    def test_nonfinite_or_extra_odometry_fact_fails(self) -> None:
        self.assertEqual(
            angular_velocity_from_quaternion_delta(
                [0.0, 0.0, 0.0, 1.0],
                [0.0, 0.0, 0.0, 1.0],
                20_000_000,
            ),
            [0.0, 0.0, 0.0],
        )
        self.assertEqual(
            angular_velocity_from_quaternion_delta(
                [0.0, 0.0, 0.0, 1.0],
                [0.0, 0.0, 0.0, -1.0],
                20_000_000,
            ),
            [0.0, 0.0, 0.0],
        )
        quarter_turn = angular_velocity_from_quaternion_delta(
            [0.0, 0.0, 0.0, 1.0],
            [0.0, 0.0, math.sqrt(0.5), math.sqrt(0.5)],
            1_000_000_000,
        )
        self.assertAlmostEqual(quarter_turn[0], 0.0)
        self.assertAlmostEqual(quarter_turn[1], 0.0)
        self.assertAlmostEqual(quarter_turn[2], math.pi / 2.0)
        body_turn = angular_velocity_from_quaternion_delta(
            [-0.5, 0.5, 0.5, 0.5],
            [0.0, 0.0, math.sqrt(0.5), math.sqrt(0.5)],
            1_000_000_000,
        )
        self.assertAlmostEqual(body_turn[0], 0.0)
        self.assertAlmostEqual(body_turn[1], -math.pi / 2.0)
        self.assertAlmostEqual(body_turn[2], 0.0)
        with self.assertRaisesRegex(M4ValidationError, "elapsed time"):
            angular_velocity_from_quaternion_delta(
                [0.0, 0.0, 0.0, 1.0],
                [0.0, 0.0, 0.0, 1.0],
                0,
            )
        with self.assertRaisesRegex(M4ValidationError, "zero norm"):
            angular_velocity_from_quaternion_delta(
                [0.0, 0.0, 0.0, 1.0],
                [0.0, 0.0, 0.0, 0.0],
                1,
            )
        records = copy.deepcopy(self.records)
        records[0]["linear_velocity_mps"] = [math.nan, 0.0, 0.0]
        with self.assertRaisesRegex(M4ValidationError, "non-finite"):
            self._validate(records)
        records = copy.deepcopy(self.records)
        records[0]["producer_passed"] = True
        with self.assertRaisesRegex(M4ValidationError, "keys differ"):
            self._validate(records)
        records = copy.deepcopy(self.records)
        records[0]["angular_velocity_method"] = "raw_odometry_twist/v1"
        with self.assertRaisesRegex(M4ValidationError, "identity/clock/keys differ"):
            self._validate(records)
        records = copy.deepcopy(self.records)
        records[0]["angular_velocity_dt_ns"] = 1
        with self.assertRaisesRegex(M4ValidationError, "identity/clock/keys differ"):
            self._validate(records)

    def test_odometry_topic_frame_or_transform_mutation_fails(self) -> None:
        for field, replacement in (
            ("source_topic", "/uav2/odometry"),
            ("source_frame", "ardupilot_local_ned"),
            ("transform_version", "unversioned"),
            ("source_header_frame", "map"),
            ("source_child_frame", "uav1/base_link"),
        ):
            records = copy.deepcopy(self.records)
            records[0][field] = replacement
            with self.subTest(field=field):
                with self.assertRaisesRegex(
                    M4ValidationError, "identity/clock/keys differ"
                ):
                    self._validate(records)

    def test_declared_threshold_mutation_fails(self) -> None:
        requirements = motion_requirements()
        requirements["minimum_measurement_path_m"] = 0.0
        with self.assertRaisesRegex(M4ValidationError, "motion contract differs"):
            validate_measurement_motion(
                self.records,
                start_ns=self.start_ns,
                end_ns=self.end_ns,
                declared_requirements=requirements,
            )

    def test_pre_window_motion_is_not_credited_to_measurement(self) -> None:
        records: list[dict[str, object]] = []
        sequence = 0
        sample_times = [self.start_ns - 500_000_000]
        sample_times.extend(
            self.start_ns + sample * 500_000_000 for sample in range(1200)
        )
        for sample, callback_ns in enumerate(sample_times):
            for uav in range(1, 6):
                sequence += 1
                records.append(
                    {
                        "schema": "ams.m4.runtime-event/v1",
                        "event_sequence": sequence,
                        "run_id": "motion-test",
                        "runtime_id": "runtime-test",
                        "host_monotonic_ns": callback_ns + 1_000_000,
                        "host_realtime_ns": 1_000_000_000_000 + callback_ns,
                        "event": "odometry_sample",
                        "uav": f"uav{uav}",
                        "source_topic": f"/uav{uav}/odometry",
                        "source_frame": "ros_odometry_world_enu",
                        "transform_version": "ams-m4-coordinate-frames-v1",
                        "source_header_frame": "odom",
                        "source_child_frame": "base_link",
                        "source_callback_monotonic_ns": callback_ns,
                        "sim_stamp_ns": (sample + 1) * 500_000_000,
                        # The only position change is between the continuity
                        # sample before start and the first in-window sample.
                        "position_m": [
                            float(uav * 100) if sample == 0 else float(uav * 100 + 10),
                            float(uav * 50),
                            20.0,
                        ],
                        "orientation_quat_xyzw": [0.0, 0.0, 0.0, 1.0],
                        "linear_velocity_mps": [0.02, 0.0, 0.0],
                        "angular_velocity_radps": [0.0, 0.0, 0.0],
                        "angular_velocity_method": ANGULAR_VELOCITY_METHOD,
                        "angular_velocity_from_sim_stamp_ns": sample
                        * 500_000_000,
                        "angular_velocity_dt_ns": 500_000_000,
                    }
                )
        with self.assertRaisesRegex(M4ValidationError, "nonzero flight motion"):
            self._validate(records)


if __name__ == "__main__":
    unittest.main()
