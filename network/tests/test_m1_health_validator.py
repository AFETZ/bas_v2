#!/usr/bin/env python3
"""Mutation-focused tests for the v3 M1 scene and component validator."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from network.scripts import validate_m1_health as validator  # noqa: E402
from network.scripts import write_m1_scene_provenance as producer  # noqa: E402


class M1SceneValidatorTests(unittest.TestCase):
    def make_fixture(self, root: Path) -> Path:
        plan = root / producer.M1_PLAN_PATH
        plan.parent.mkdir(parents=True)
        plan.write_text("# v3 fixture\n", encoding="utf-8")
        scenario = root / "network/config/scenario.yaml"
        scenario.parent.mkdir(parents=True)
        scenario.write_text(
            "scenario:\n  map:\n    world_file: fixture/model.sdf\n",
            encoding="utf-8",
        )
        source_bundle = root / "src/multiagent_simulation/worlds/fixture"
        source_bundle.mkdir(parents=True)
        (source_bundle / "model.sdf").write_text(
            "<sdf version='1.9'><world name='fixture_world'/></sdf>\n",
            encoding="utf-8",
        )
        (source_bundle / "terrain.bin").write_bytes(b"canonical-terrain")
        installed_bundle = (
            root
            / "install/multiagent_simulation/share/multiagent_simulation/worlds/fixture"
        )
        installed_bundle.mkdir(parents=True)
        for source in source_bundle.iterdir():
            (installed_bundle / source.name).write_bytes(source.read_bytes())

        run_dir = root / "runs/m1_fixture"
        (run_dir / "metrics").mkdir(parents=True)
        (run_dir / "logs").mkdir()
        source_hash = "a" * 64
        active_relative = "src/multiagent_simulation/worlds/fixture/model.sdf"
        provenance = {
            "source_hash": source_hash,
            "config_hashes": {producer.M1_PLAN_PATH: producer.sha256_file(plan)},
            "dependency_versions": {"gazebo": "8.14.0"},
            "source_manifest": {
                active_relative: producer.sha256_file(source_bundle / "model.sdf")
            },
        }
        (run_dir / "metrics/provenance.json").write_text(
            json.dumps(provenance), encoding="utf-8"
        )
        runtime_id = "runtime-fixture"
        record = producer.build_scene_record(
            run_dir=run_dir,
            scenario_path=scenario,
            runtime_id=runtime_id,
            installed_package_share=(
                root / "install/multiagent_simulation/share/multiagent_simulation"
            ),
            root=root,
        )
        producer.write_scene_record(run_dir / producer.M1_SCENE_RECORD, record)
        self.write_raw(run_dir, runtime_id, source_hash, record["installed"]["runtime_active_world_path"])
        return run_dir

    def write_raw(
        self,
        run_dir: Path,
        runtime_id: str,
        source_hash: str,
        world_path: str,
    ) -> None:
        contract_hash = producer.sha256_file(run_dir.parents[1] / producer.M1_PLAN_PATH)
        records = []
        for sequence in range(1, 3):
            records.append(
                {
                    "schema_version": 2,
                    "run_id": run_dir.name,
                    "runtime_id": runtime_id,
                    "source_hash": source_hash,
                    "contract": producer.M1_CONTRACT_ID,
                    "plan_version": 3,
                    "contract_sha256": contract_hash,
                    "event_seq": sequence,
                    "event": "process_sample",
                    "wall_utc": datetime.now(timezone.utc).isoformat(),
                    "monotonic_ns": sequence,
                    "processes": [
                        {
                            "arguments": (
                                "ros2 launch multiagent_simulation "
                                "multiagent_simulation.launch.py "
                                "world_file:=fixture/model.sdf"
                            )
                        },
                        {"arguments": f"gz sim -v4 -s -r {world_path}"},
                    ],
                }
            )
        records.append(
            {
                "schema_version": 2,
                "run_id": run_dir.name,
                "runtime_id": runtime_id,
                "source_hash": source_hash,
                "contract": producer.M1_CONTRACT_ID,
                "plan_version": 3,
                "contract_sha256": contract_hash,
                "event_seq": 3,
                "event": "gazebo_scene_probe",
                "wall_utc": datetime.now(timezone.utc).isoformat(),
                "monotonic_ns": 3,
                "exit_code": 0,
                "world_name": "fixture_world",
                "model_names": [f"uav{index}" for index in range(1, 6)],
            }
        )
        raw = "".join(json.dumps(item, sort_keys=True) + "\n" for item in records)
        (run_dir / validator.RAW_HEALTH_LOG).write_text(raw, encoding="utf-8")
        health = {
            "runtime_id": runtime_id,
            "contract": producer.M1_CONTRACT_ID,
            "plan_version": 3,
            "contract_sha256": contract_hash,
            "gazebo_world_name": "fixture_world",
            "raw_event_log": validator.RAW_HEALTH_LOG,
            "raw_event_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        }
        (run_dir / "metrics/five_uav_health.json").write_text(
            json.dumps(health), encoding="utf-8"
        )

    def test_exact_source_install_and_raw_gazebo_world_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = self.make_fixture(root)
            with mock.patch.object(validator, "ROOT_DIR", root):
                result = validator.scene_status(run_dir)
        self.assertEqual(result["status"], "passed", result)

    def test_forged_summary_cannot_hide_wrong_raw_gazebo_world(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = self.make_fixture(root)
            provenance = json.loads((run_dir / "metrics/provenance.json").read_text())
            health = json.loads((run_dir / "metrics/five_uav_health.json").read_text())
            self.write_raw(
                run_dir,
                health["runtime_id"],
                provenance["source_hash"],
                str(root / "install/forged/world.sdf"),
            )
            with mock.patch.object(validator, "ROOT_DIR", root):
                result = validator.scene_status(run_dir)
        self.assertEqual(result["status"], "failed")
        self.assertIn("Gazebo world differs", "\n".join(result["details"]["failures"]))

    def test_installed_bundle_mutation_fails_after_producer_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = self.make_fixture(root)
            installed_asset = (
                root
                / "install/multiagent_simulation/share/multiagent_simulation/worlds/fixture/terrain.bin"
            )
            installed_asset.write_bytes(b"mutated-after-producer")
            with mock.patch.object(validator, "ROOT_DIR", root):
                result = validator.scene_status(run_dir)
        self.assertEqual(result["status"], "failed")
        self.assertIn(
            "active installed world bundle differs",
            "\n".join(result["details"]["failures"]),
        )

    def test_traversal_world_file_is_rejected_before_launch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            scenario = root / "scenario.yaml"
            scenario.write_text(
                "scenario:\n  map:\n    world_file: ../outside.sdf\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "canonical POSIX relative path"):
                producer.scenario_world_file(scenario, root=root)

    def test_component_result_requires_every_gate_and_never_claims_p0(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = root / "runs/m1_result"
            run_dir.mkdir(parents=True)
            passed = {"status": "passed", "proof": "independent"}
            with mock.patch.object(validator, "ROOT_DIR", root), mock.patch.object(
                validator, "provenance_status", return_value=passed
            ), mock.patch.object(
                validator, "five_uav_health_status", return_value=passed
            ), mock.patch.object(validator, "scene_status", return_value=passed):
                result = validator.evaluate_m1(run_dir)
        self.assertTrue(result["passed"])
        self.assertEqual(result["contract"], "ams.m1.health/v3")
        self.assertEqual(result["plan_version"], 3)
        self.assertTrue(result["component_only"])
        self.assertFalse(result["p0_eligible"])
        self.assertFalse(result["scope"]["sealing"])
        self.assertFalse(result["scope"]["attestation"])


if __name__ == "__main__":
    unittest.main()
