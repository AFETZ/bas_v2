from __future__ import annotations

import math
import unittest
from types import SimpleNamespace
from unittest import mock

from network.scripts.m4_gazebo_pose_source import (
    GAZEBO_WORLD_POSE_SOURCE_FRAME,
    GAZEBO_WORLD_POSE_STAMP_SCOPE,
    GAZEBO_WORLD_POSE_TOPIC,
    GAZEBO_WORLD_POSE_TRANSFORM_VERSION,
    GAZEBO_WORLD_POSE_TRANSPORT,
    GazeboPoseVSource,
    decode_pose_v,
)
from network.validation.m4_common import M4ValidationError


def namespace(**values: object) -> SimpleNamespace:
    return SimpleNamespace(**values)


def empty_pose_header() -> SimpleNamespace:
    return namespace(stamp=namespace(sec=0, nsec=0), data=[])


def pose(
    name: str,
    *,
    position: tuple[float, float, float],
    orientation: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0),
    header: object | None = None,
) -> SimpleNamespace:
    return namespace(
        name=name,
        header=empty_pose_header() if header is None else header,
        position=namespace(x=position[0], y=position[1], z=position[2]),
        orientation=namespace(
            x=orientation[0],
            y=orientation[1],
            z=orientation[2],
            w=orientation[3],
        ),
    )


def pose_v(
    *,
    sec: int = 12,
    nsec: int = 345,
    data: list[object] | None = None,
    poses: list[SimpleNamespace] | None = None,
) -> SimpleNamespace:
    return namespace(
        header=namespace(
            stamp=namespace(sec=sec, nsec=nsec),
            data=[] if data is None else data,
        ),
        pose=(
            [
                pose("ground_plane", position=(0.0, 0.0, 0.0)),
                pose("jammer_m4", position=(2_000.0, -3_000.0, 100.0)),
                pose("cp", position=(-8_000.0, -2_500.0, 300.0)),
            ]
            if poses is None
            else poses
        ),
    )


class M4GazeboPoseVDecodeTests(unittest.TestCase):
    def test_happy_path_is_exact_and_deterministically_ordered(self) -> None:
        message = pose_v()
        message.pose.extend(
            [
                pose("uav5", position=(5.0, 0.0, 20.0)),
                pose("uav2", position=(2.0, 0.0, 20.0)),
                pose("uav1", position=(1.0, 0.0, 20.0)),
                pose("uav4", position=(4.0, 0.0, 20.0)),
                pose("uav3", position=(3.0, 0.0, 20.0)),
            ]
        )
        observations = decode_pose_v(message, callback_monotonic_ns=987_654_321)

        self.assertEqual(
            tuple(item.entity_id for item in observations),
            ("cp", "uav1", "uav2", "uav3", "uav4", "uav5", "jammer_m4"),
        )
        self.assertEqual(observations[0].position_m, (-8_000.0, -2_500.0, 300.0))
        self.assertEqual(observations[-1].position_m, (2_000.0, -3_000.0, 100.0))
        for observation in observations:
            self.assertEqual(observation.source_callback_monotonic_ns, 987_654_321)
            self.assertEqual(observation.sim_stamp_ns, 12_000_000_345)
            self.assertEqual(observation.source_topic, GAZEBO_WORLD_POSE_TOPIC)
            self.assertEqual(observation.source_transport, GAZEBO_WORLD_POSE_TRANSPORT)
            self.assertEqual(observation.source_stamp_scope, GAZEBO_WORLD_POSE_STAMP_SCOPE)
            self.assertEqual(observation.source_frame, GAZEBO_WORLD_POSE_SOURCE_FRAME)
            self.assertEqual(
                observation.transform_version, GAZEBO_WORLD_POSE_TRANSFORM_VERSION
            )
            self.assertEqual(observation.source_header_frame, "")
            self.assertEqual(observation.source_child_frame, observation.entity_id)
            self.assertEqual(observation.orientation_quat_xyzw, (0.0, 0.0, 0.0, 1.0))

    def test_zero_top_level_startup_stamp_is_valid(self) -> None:
        observations = decode_pose_v(
            pose_v(sec=0, nsec=0), callback_monotonic_ns=1
        )
        self.assertEqual([item.sim_stamp_ns for item in observations], [0, 0])

    def test_empty_per_pose_stamps_never_replace_top_level_stamp(self) -> None:
        message = pose_v(sec=7, nsec=8)
        observations = decode_pose_v(message, callback_monotonic_ns=10)
        self.assertEqual([item.sim_stamp_ns for item in observations], [7_000_000_008] * 2)

        del message.header
        with self.assertRaisesRegex(M4ValidationError, "top-level header is missing"):
            decode_pose_v(message, callback_monotonic_ns=10)

    def test_duplicate_and_missing_target_entities_are_rejected(self) -> None:
        cp = pose("cp", position=(0.0, 0.0, 0.0))
        jammer = pose("jammer_m4", position=(1.0, 2.0, 3.0))
        with self.subTest("duplicate"):
            with self.assertRaisesRegex(M4ValidationError, "duplicate cp"):
                decode_pose_v(
                    pose_v(poses=[cp, cp, jammer]), callback_monotonic_ns=10
                )
        with self.subTest("missing"):
            with self.assertRaisesRegex(M4ValidationError, "missing required pose"):
                decode_pose_v(pose_v(poses=[cp]), callback_monotonic_ns=10)
        with self.subTest("duplicate optional UAV"):
            uav = pose("uav1", position=(4.0, 5.0, 6.0))
            with self.assertRaisesRegex(M4ValidationError, "duplicate uav1"):
                decode_pose_v(
                    pose_v(poses=[cp, jammer, uav, uav]),
                    callback_monotonic_ns=10,
                )

    def test_nonempty_top_level_header_data_is_rejected(self) -> None:
        with self.assertRaisesRegex(M4ValidationError, "live empty contract"):
            decode_pose_v(
                pose_v(data=[namespace(key="frame_id", value=["world"])]),
                callback_monotonic_ns=10,
            )

    def test_nonempty_per_pose_header_is_rejected(self) -> None:
        cp = pose(
            "cp",
            position=(0.0, 0.0, 0.0),
            header=namespace(stamp=namespace(sec=1, nsec=0), data=[]),
        )
        jammer = pose("jammer_m4", position=(1.0, 2.0, 3.0))
        with self.assertRaisesRegex(M4ValidationError, "per-pose header"):
            decode_pose_v(pose_v(poses=[cp, jammer]), callback_monotonic_ns=10)

    def test_nonfinite_position_and_quaternion_are_rejected(self) -> None:
        for label, bad_pose in (
            (
                "position",
                pose("cp", position=(math.nan, 0.0, 0.0)),
            ),
            (
                "orientation",
                pose(
                    "cp",
                    position=(0.0, 0.0, 0.0),
                    orientation=(0.0, math.inf, 0.0, 1.0),
                ),
            ),
        ):
            with self.subTest(label):
                with self.assertRaisesRegex(M4ValidationError, "not finite"):
                    decode_pose_v(
                        pose_v(
                            poses=[
                                bad_pose,
                                pose("jammer_m4", position=(1.0, 2.0, 3.0)),
                            ]
                        ),
                        callback_monotonic_ns=10,
                    )

    def test_invalid_top_level_stamp_ranges_are_rejected(self) -> None:
        for sec, nsec in ((-1, 0), (0, -1), (0, 1_000_000_000)):
            with self.subTest(sec=sec, nsec=nsec):
                with self.assertRaisesRegex(M4ValidationError, "out of range"):
                    decode_pose_v(
                        pose_v(sec=sec, nsec=nsec), callback_monotonic_ns=10
                    )


class FakePoseV:
    pass


class FakeNode:
    last_instance: "FakeNode | None" = None

    def __init__(self) -> None:
        type(self).last_instance = self
        self.message_type: object | None = None
        self.topic: str | None = None
        self.callback: object | None = None
        self.unsubscribed: list[str] = []

    def subscribe(self, message_type: object, topic: str, callback: object) -> None:
        self.message_type = message_type
        self.topic = topic
        self.callback = callback

    def unsubscribe(self, topic: str) -> None:
        self.unsubscribed.append(topic)

    def emit(self, message: object) -> None:
        assert callable(self.callback)
        self.callback(message)


class M4GazeboPoseVSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeNode.last_instance = None

    @mock.patch(
        "network.scripts.m4_gazebo_pose_source._load_gazebo_bindings",
        return_value=(FakeNode, FakePoseV),
    )
    def test_native_subscription_delivery_and_close(self, _loader: mock.Mock) -> None:
        delivered: list[object] = []
        source = GazeboPoseVSource(delivered.append)
        node = FakeNode.last_instance
        assert node is not None
        self.assertIs(node.message_type, FakePoseV)
        self.assertEqual(node.topic, GAZEBO_WORLD_POSE_TOPIC)

        node.emit(pose_v())
        source.raise_if_failed()
        self.assertEqual(len(delivered), 1)
        self.assertEqual(
            tuple(item.entity_id for item in delivered[0]), ("cp", "jammer_m4")
        )
        source.close()
        source.close()
        self.assertEqual(node.unsubscribed, [GAZEBO_WORLD_POSE_TOPIC])

    @mock.patch(
        "network.scripts.m4_gazebo_pose_source._load_gazebo_bindings",
        return_value=(FakeNode, FakePoseV),
    )
    def test_callback_exception_is_stored_and_source_fails_closed(
        self, _loader: mock.Mock
    ) -> None:
        callback_calls = 0

        def failing_callback(_observations: object) -> None:
            nonlocal callback_calls
            callback_calls += 1
            raise RuntimeError("consumer failed")

        source = GazeboPoseVSource(failing_callback)
        node = FakeNode.last_instance
        assert node is not None
        node.emit(pose_v())
        node.emit(pose_v())
        self.assertEqual(callback_calls, 1)
        with self.assertRaisesRegex(RuntimeError, "consumer failed"):
            source.raise_if_failed()
        source.close()

    @mock.patch(
        "network.scripts.m4_gazebo_pose_source._load_gazebo_bindings",
        return_value=(FakeNode, FakePoseV),
    )
    def test_decode_exception_is_stored_instead_of_escaping_transport_callback(
        self, _loader: mock.Mock
    ) -> None:
        source = GazeboPoseVSource(lambda _observations: None)
        node = FakeNode.last_instance
        assert node is not None
        node.emit(namespace(pose=[]))
        with self.assertRaisesRegex(M4ValidationError, "top-level header is missing"):
            source.raise_if_failed()
        source.close()


if __name__ == "__main__":
    unittest.main()
