from __future__ import annotations

import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from network.radio_provider.sionna_packet_adapter import PacketAdapterError
from network.scripts.m4_adapter_runtime import PoseTracker
from network.validation.m4_common import validate_pose_snapshots


def namespace(**values: object) -> SimpleNamespace:
    return SimpleNamespace(**values)


def vector(x: float, y: float, z: float) -> SimpleNamespace:
    return namespace(x=x, y=y, z=z)


def quaternion() -> SimpleNamespace:
    return namespace(x=0.0, y=0.0, z=0.0, w=1.0)


class M4RawPoseLineageTests(unittest.TestCase):
    @staticmethod
    def jammer() -> dict[str, object]:
        return {
            "enabled": False,
            "center_hz": 2_437_000_000.0,
            "bandwidth_hz": 20_000_000.0,
            "power_dbm": 20.0,
            "duty_cycle": 1.0,
            "antenna": "isotropic",
        }

    @staticmethod
    def odometry(
        uav: int, *, header_frame: str = "odom", child_frame: str = "base_link"
    ) -> SimpleNamespace:
        return namespace(
            header=namespace(
                stamp=namespace(sec=10 + uav, nanosec=0),
                frame_id=header_frame,
            ),
            child_frame_id=child_frame,
            pose=namespace(
                pose=namespace(
                    position=vector(float(uav), float(uav * 2), 20.0),
                    orientation=quaternion(),
                )
            ),
        )

    @staticmethod
    def world_poses() -> tuple[SimpleNamespace, ...]:
        observations = []
        callback_ns = time.monotonic_ns()
        for name, position in (
            ("cp", vector(-8_000.0, -2_500.0, 300.0)),
            ("jammer_m4", vector(2_000.0, -3_000.0, 100.0)),
        ):
            observations.append(
                namespace(
                    entity_id=name,
                    source_callback_monotonic_ns=callback_ns,
                    sim_stamp_ns=10_000_000_000,
                    source_topic="/world/map/pose/info",
                    source_transport="gazebo_transport_pose_v",
                    source_stamp_scope="pose_v_top_level_header",
                    source_frame="world",
                    transform_version="enu-identity-v1",
                    source_header_frame="",
                    source_child_frame=name,
                    position_m=(position.x, position.y, position.z),
                    orientation_quat_xyzw=(0.0, 0.0, 0.0, 1.0),
                )
            )
        return tuple(observations)

    def make_evidence(
        self, root: Path
    ) -> tuple[Path, dict[str, list[dict[str, object]]]]:
        tracker = PoseTracker(root, self.jammer())
        try:
            for uav in range(1, 6):
                tracker.update_uav(f"uav{uav}", self.odometry(uav))
            tracker.update_world(self.world_poses())
            snapshot = tracker.snapshot(time.monotonic_ns())
            self.assertIsNotNone(snapshot)
            assert snapshot is not None
        finally:
            tracker.close()
        pose_path = root / "logs/m4_pose_snapshots.jsonl"
        raw = json.loads(pose_path.read_text(encoding="utf-8"))
        wire = {
            "messages": [
                {
                    "message_type": "query",
                    "query_id": "pose-lineage-test",
                    "request_sent_monotonic_ns": raw["snapshot_monotonic_ns"],
                    "node_state_seq": raw["node_state_seq"],
                    "node_state_sha256": raw["node_state_sha256"],
                    "node_state_snapshot_monotonic_ns": raw[
                        "snapshot_monotonic_ns"
                    ],
                    "source_frame": snapshot.source_frame,
                    "transform_version": snapshot.transform_version,
                    "nodes": [dict(item) for item in snapshot.nodes],
                    "jammers": [dict(item) for item in snapshot.jammers],
                }
            ]
        }
        return pose_path, wire

    def test_world_pose_snapshot_preserves_exact_raw_ros_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path, wire = self.make_evidence(Path(directory))
            details, failures = validate_pose_snapshots(path, wire)
            self.assertEqual(failures, [])
            self.assertEqual(details["valid_snapshot_count"], 1)
            raw = json.loads(path.read_text(encoding="utf-8"))
            uav_nodes = [
                item for item in raw["nodes"] if item["node_id"].startswith("uav")
            ]
            self.assertTrue(
                all(
                    item["source_header_frame"] == "odom"
                    and item["source_child_frame"] == "base_link"
                    and item["source_transport"] == "ros2_dds_odometry"
                    and item["source_stamp_scope"] == "ros_header"
                    and item["source_frame"] == "world"
                    and item["transform_version"] == "enu-identity-v1"
                    for item in uav_nodes
                )
            )
            world_records = [
                raw["nodes"][0],
                raw["jammers"][0],
            ]
            self.assertTrue(
                all(
                    item["source_header_frame"] == ""
                    and item["source_transport"] == "gazebo_transport_pose_v"
                    and item["source_stamp_scope"] == "pose_v_top_level_header"
                    and item["source_header_stamp_ns"] == 10_000_000_000
                    for item in world_records
                )
            )

    def test_mutated_raw_header_cannot_reuse_immutable_pose_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path, wire = self.make_evidence(Path(directory))
            raw = json.loads(path.read_text(encoding="utf-8"))
            mutated = copy.deepcopy(raw)
            mutated["nodes"][1]["source_header_frame"] = "map"
            path.write_text(
                json.dumps(mutated, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            _details, failures = validate_pose_snapshots(path, wire)
            self.assertTrue(
                any("raw uav1 odometry lineage differs" in item for item in failures),
                failures,
            )

    def test_tracker_rejects_wrong_ros_header_before_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tracker = PoseTracker(Path(directory), self.jammer())
            try:
                with self.assertRaisesRegex(
                    PacketAdapterError, "odometry header frame differs"
                ):
                    tracker.update_uav(
                        "uav1", self.odometry(1, header_frame="map")
                    )
            finally:
                tracker.close()

    def test_snapshot_boundary_follows_all_callback_poses(self) -> None:
        """A caller timestamp from before a callback cannot predate its pose."""
        with tempfile.TemporaryDirectory() as directory:
            tracker = PoseTracker(Path(directory), self.jammer())
            try:
                for uav in range(1, 6):
                    tracker.update_uav(f"uav{uav}", self.odometry(uav))
                observations = self.world_poses()
                future_callback_ns = time.monotonic_ns() + 1_000_000
                for observation in observations:
                    observation.source_callback_monotonic_ns = future_callback_ns
                tracker.update_world(observations)
                snapshot = tracker.snapshot(0)
                self.assertIsNotNone(snapshot)
                assert snapshot is not None
                pose_times = [
                    int(item["pose_monotonic_ns"])
                    for item in [*snapshot.nodes, *snapshot.jammers]
                ]
                self.assertGreaterEqual(snapshot.snapshot_monotonic_ns, max(pose_times))
            finally:
                tracker.close()


if __name__ == "__main__":
    unittest.main()
