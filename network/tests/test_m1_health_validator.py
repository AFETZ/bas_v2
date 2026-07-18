#!/usr/bin/env python3
"""Mutation-focused tests for the v3 M1 scene and component validator."""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
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
            "scenario:\n  map:\n    world_file: fixture/model.sdf\n"
            "robots:\n"
            + "".join(
                f"  - name: uav{index + 1}\n    instance: {index}\n"
                for index in range(5)
            ),
            encoding="utf-8",
        )
        source_bundle = root / "src/multiagent_simulation/worlds/fixture"
        source_bundle.mkdir(parents=True)
        (source_bundle / "model.sdf").write_text(
            "<sdf version='1.9'><world name='fixture_world'><model name='ground'>"
            "<link name='ground'><visual name='ground'><geometry><mesh>"
            "<uri>fixture.dae</uri></mesh></geometry></visual></link>"
            "</model></world></sdf>\n",
            encoding="utf-8",
        )
        (source_bundle / "fixture.dae").write_text(
            "<COLLADA xmlns='http://www.collada.org/2005/11/COLLADASchema'>"
            "<library_images><image id='texture'><init_from>texture.png</init_from>"
            "</image></library_images></COLLADA>\n",
            encoding="utf-8",
        )
        (source_bundle / "texture.png").write_bytes(b"fixture-texture")
        (source_bundle / "terrain.bin").write_bytes(b"canonical-terrain")

        source_models = root / "src/multiagent_simulation/models"
        headless = source_models / "iris_radio_headless"
        iris = source_models / "iris"
        (iris / "meshes").mkdir(parents=True)
        headless.mkdir(parents=True)
        (headless / "model.config").write_text(
            "<model><name>headless</name><sdf version='1.9'>model.sdf</sdf></model>\n",
            encoding="utf-8",
        )
        (headless / "model.sdf").write_text(
            "<sdf version='1.9'><model name='iris_radio_headless'>"
            "<include merge='true'><uri>model://iris</uri></include>"
            "<plugin name='ArduPilotPlugin' filename='ArduPilotPlugin'>"
            "<fdm_addr>127.0.0.1</fdm_addr><fdm_port_in>9002</fdm_port_in>"
            "</plugin></model></sdf>\n",
            encoding="utf-8",
        )
        (iris / "model.config").write_text(
            "<model><name>iris</name><sdf version='1.9'>model.sdf</sdf></model>\n",
            encoding="utf-8",
        )
        (iris / "model.sdf").write_text(
            "<sdf version='1.9'><model name='iris'><link name='base'>"
            "<visual name='body'><geometry><mesh><uri>meshes/iris.dae</uri>"
            "</mesh></geometry></visual></link></model></sdf>\n",
            encoding="utf-8",
        )
        (iris / "meshes/iris.dae").write_text(
            "<COLLADA xmlns='http://www.collada.org/2005/11/COLLADASchema'/>\n",
            encoding="utf-8",
        )
        launch = root / producer.LAUNCH_SOURCE_RELATIVE
        launch.parent.mkdir(parents=True)
        launch.write_text("# fixture launch\n", encoding="utf-8")
        package_root = root / "src/multiagent_simulation"
        (package_root / "config").mkdir()
        (package_root / "rviz").mkdir()
        (package_root / "config/gazebo-iris.parm").write_text(
            "FRAME_CLASS 1\n", encoding="utf-8"
        )
        (package_root / "config/multiagent_lidar_camera_bridge.yaml").write_text(
            "- ros_topic_name: /<robot_name>/tf\n", encoding="utf-8"
        )
        (package_root / "rviz/fixture.rviz").write_text(
            "Panels: []\n", encoding="utf-8"
        )
        (package_root / "package.xml").write_text(
            "<package format='3'><name>multiagent_simulation</name>"
            "<version>0.0.0</version><description>fixture</description>"
            "<maintainer email='fixture@example.test'>Fixture</maintainer>"
            "<license>GPL-3.0</license></package>\n",
            encoding="utf-8",
        )

        run_dir = root / "runs/m1_fixture"
        (run_dir / "metrics").mkdir(parents=True)
        (run_dir / "logs").mkdir()
        installed_share = (
            run_dir
            / "runtime_overlay/install/multiagent_simulation/share/multiagent_simulation"
        )
        shutil.copytree(source_bundle, installed_share / "worlds/fixture")
        shutil.copytree(source_models, installed_share / "models")
        (installed_share / "launch").mkdir()
        shutil.copy2(launch, installed_share / "launch/multiagent_simulation.launch.py")
        shutil.copytree(package_root / "config", installed_share / "config")
        shutil.copytree(package_root / "rviz", installed_share / "rviz")
        shutil.copy2(package_root / "package.xml", installed_share / "package.xml")
        source_hash = "a" * 64
        source_inputs = [
            scenario,
            launch,
            package_root / "package.xml",
            *sorted(path for path in (package_root / "config").rglob("*") if path.is_file()),
            *sorted(path for path in (package_root / "rviz").rglob("*") if path.is_file()),
            *sorted(path for path in source_bundle.rglob("*") if path.is_file()),
            *sorted(path for path in source_models.rglob("*") if path.is_file()),
        ]
        provenance = {
            "source_hash": source_hash,
            "git_commit": "a" * 40,
            "config_hashes": {producer.M1_PLAN_PATH: producer.sha256_file(plan)},
            "dependency_versions": {"gazebo": "8.14.0"},
            "source_manifest": {
                path.relative_to(root).as_posix(): producer.sha256_file(path)
                for path in source_inputs
            },
        }
        (run_dir / "metrics/provenance.json").write_text(
            json.dumps(provenance), encoding="utf-8"
        )
        runtime_id = "runtime-fixture"
        record = producer.build_scene_record(
            run_dir=run_dir,
            scenario_path=scenario,
            robot_model="iris_radio_headless",
            runtime_id=runtime_id,
            installed_package_share=installed_share,
            root=root,
        )
        producer.write_scene_record(run_dir / producer.M1_SCENE_RECORD, record)
        self.write_raw(run_dir, runtime_id, source_hash, record["installed"]["runtime_active_world_path"])
        overlay = run_dir / "runtime_overlay"
        command = [
            "/usr/bin/colcon",
            "--log-base",
            str(overlay / "log"),
            "build",
            "--base-paths",
            str(root / "src/multiagent_simulation"),
            "--build-base",
            str(overlay / "build"),
            "--install-base",
            str(overlay / "install"),
        ]
        (run_dir / "logs/m1_runtime_overlay_build.command").write_text(
            " ".join(command) + "\n", encoding="utf-8"
        )
        (run_dir / "logs/m1_runtime_overlay_build.log").write_text(
            "fixture build passed\n", encoding="utf-8"
        )
        (run_dir / "logs/m1_runtime_overlay_build.exit_code").write_text(
            "0\n", encoding="utf-8"
        )
        resource_path = (
            f"{installed_share}/models:{installed_share}/worlds:{installed_share}"
        )
        (run_dir / "environment.txt").write_text(
            "source_mode=clean_git_clone_ro\n"
            f"source_commit={'a' * 40}\n"
            f"runtime_overlay={overlay}\n"
            f"installed_package_share={installed_share}\n"
            f"gz_sim_resource_path={resource_path}\n"
            "generate_sensor_models=false\n"
            "python_dont_write_bytecode=1\n"
            "python_pycache_prefix=/tmp/ams-m1-pycache\n",
            encoding="utf-8",
        )
        return run_dir

    def write_raw(
        self,
        run_dir: Path,
        runtime_id: str,
        source_hash: str,
        world_path: str,
        *,
        raw_robot_model: str = "iris_radio_headless",
        forged_human_launch_arguments: str | None = None,
        robot_description_port_override: int | None = None,
        launch_assignment_overrides: dict[str, str] | None = None,
        gazebo_response_override: bytes | None = None,
    ) -> None:
        contract_hash = producer.sha256_file(run_dir.parents[1] / producer.M1_PLAN_PATH)
        provenance_hash = producer.sha256_file(run_dir / "metrics/provenance.json")
        wall_time_base_ns = 1_700_000_000_000_000_000

        def process(argv: list[str], *, arguments: str | None = None, exe: str = "") -> dict:
            raw_cmdline = b"\0".join(value.encode("utf-8") for value in argv) + b"\0"
            return {
                "arguments": arguments if arguments is not None else " ".join(argv),
                "cmdline_b64": base64.b64encode(raw_cmdline).decode("ascii"),
                "cmdline_sha256": hashlib.sha256(raw_cmdline).hexdigest(),
                "exe_path": exe or argv[0],
            }

        launch_assignments = {
            "robots_config_file": str(
                run_dir.parents[1] / "network/config/scenario.yaml"
            ),
            "world_file": "fixture/model.sdf",
            "robot_model": raw_robot_model,
            "enable_serial2": "false",
            "generate_sensor_models": "false",
            "gui": "false",
            "rviz": "false",
            "headless_rendering": "false",
            "use_mapping_camera": "false",
            "use_navigation_camera": "false",
            "use_zed_camera": "false",
        }
        launch_assignments.update(launch_assignment_overrides or {})
        launch_argv = [
            "/usr/bin/python3",
            "/opt/ros/humble/bin/ros2",
            "launch",
            "multiagent_simulation",
            "multiagent_simulation.launch.py",
            *[
                f"{name}:={value}"
                for name, value in launch_assignments.items()
            ],
        ]
        gazebo_argv = [f"gz sim -v4 -s -r {world_path}"]
        process_rows = [
            process(
                launch_argv,
                arguments=forged_human_launch_arguments,
                exe="/usr/bin/python3",
            ),
            process(gazebo_argv, exe="/usr/bin/ruby"),
        ]
        for index in range(1, 6):
            process_rows.append(
                process(
                    [
                        "/opt/ros/humble/lib/robot_state_publisher/robot_state_publisher",
                        "--ros-args",
                        "-r",
                        f"__ns:=/uav{index}",
                    ],
                    exe="/opt/ros/humble/lib/robot_state_publisher/robot_state_publisher",
                )
            )

        records = []
        for sequence in range(1, 3):
            records.append(
                {
                    "schema_version": 2,
                    "run_id": run_dir.name,
                    "runtime_id": runtime_id,
                    "source_hash": source_hash,
                    "profile": "m1_component",
                    "scenario_id": "scenario_5uav",
                    "phase": "measurement",
                    "provenance_sha256": provenance_hash,
                    "contract": producer.M1_CONTRACT_ID,
                    "plan_version": 3,
                    "contract_sha256": contract_hash,
                    "event_seq": sequence,
                    "event": "process_sample",
                    "wall_utc": datetime.fromtimestamp(
                        (wall_time_base_ns + sequence * 1_000_000) / 1_000_000_000,
                        timezone.utc,
                    ).isoformat(),
                    "wall_time_ns": wall_time_base_ns + sequence * 1_000_000,
                    "monotonic_ns": sequence,
                    "processes": process_rows,
                }
            )
        gazebo_response = gazebo_response_override or "\n".join(
            f'model {{ name: "uav{index}" }}' for index in range(1, 6)
        ).encode("utf-8")
        records.append(
            {
                "schema_version": 2,
                "run_id": run_dir.name,
                "runtime_id": runtime_id,
                "source_hash": source_hash,
                "profile": "m1_component",
                "scenario_id": "scenario_5uav",
                "phase": "measurement",
                "provenance_sha256": provenance_hash,
                "contract": producer.M1_CONTRACT_ID,
                "plan_version": 3,
                "contract_sha256": contract_hash,
                "event_seq": 3,
                "event": "gazebo_scene_probe",
                "wall_utc": datetime.fromtimestamp(
                    (wall_time_base_ns + 3_000_000) / 1_000_000_000,
                    timezone.utc,
                ).isoformat(),
                "wall_time_ns": wall_time_base_ns + 3_000_000,
                "monotonic_ns": 3,
                "exit_code": 0,
                "command": [
                    "gz",
                    "service",
                    "-s",
                    "/world/fixture_world/scene/info",
                    "--reqtype",
                    "gz.msgs.Empty",
                    "--reptype",
                    "gz.msgs.Scene",
                    "--timeout",
                    "5000",
                    "--req",
                    "",
                ],
                "world_name": "fixture_world",
                "model_names": [f"uav{index}" for index in range(1, 6)],
                "stdout_b64": base64.b64encode(gazebo_response).decode("ascii"),
                "stdout_sha256": hashlib.sha256(gazebo_response).hexdigest(),
                "stderr_b64": "",
                "stderr_sha256": hashlib.sha256(b"").hexdigest(),
            }
        )
        template = (
            run_dir
            / "runtime_overlay/install/multiagent_simulation/share/multiagent_simulation/models/iris_radio_headless/model.sdf"
        ).read_text(encoding="utf-8")
        probed_robots = []
        for index in range(5):
            port = 9002 + 10 * index
            effective_port = (
                robot_description_port_override
                if index == 0 and robot_description_port_override is not None
                else port
            )
            description = template.replace(
                producer.ROBOT_DESCRIPTION_PORT_TOKEN,
                f"<fdm_port_in>{effective_port}</fdm_port_in>",
            ).encode("utf-8")
            probed_robots.append(
                {
                    "name": f"uav{index + 1}",
                    "namespace": f"/uav{index + 1}",
                    "robot_description_b64": base64.b64encode(description).decode("ascii"),
                    "robot_description_sha256": hashlib.sha256(description).hexdigest(),
                }
            )
        records.append(
            {
                "schema_version": 2,
                "run_id": run_dir.name,
                "runtime_id": runtime_id,
                "source_hash": source_hash,
                "profile": "m1_component",
                "scenario_id": "scenario_5uav",
                "phase": "measurement",
                "provenance_sha256": provenance_hash,
                "contract": producer.M1_CONTRACT_ID,
                "plan_version": 3,
                "contract_sha256": contract_hash,
                "event_seq": 4,
                "event": "robot_description_probe",
                "wall_utc": datetime.fromtimestamp(
                    (wall_time_base_ns + 4_000_000) / 1_000_000_000,
                    timezone.utc,
                ).isoformat(),
                "wall_time_ns": wall_time_base_ns + 4_000_000,
                "monotonic_ns": 4,
                "robots": probed_robots,
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

    def test_fresh_run_local_runtime_inputs_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = self.make_fixture(root)
            with mock.patch.object(validator, "ROOT_DIR", root):
                result = validator.runtime_inputs_status(run_dir)
        self.assertEqual(result["status"], "passed", result)

    def test_launch_import_bytecode_cannot_pollute_installed_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = self.make_fixture(root)
            cache = (
                run_dir
                / "runtime_overlay/install/multiagent_simulation/share/"
                "multiagent_simulation/launch/__pycache__"
            )
            cache.mkdir()
            (cache / "multiagent_simulation.launch.cpython-310.pyc").write_bytes(
                b"runtime-generated-bytecode"
            )
            with mock.patch.object(validator, "ROOT_DIR", root):
                result = validator.runtime_inputs_status(run_dir)
        self.assertEqual(result["status"], "failed")
        self.assertIn(
            "installed package inputs differ",
            "\n".join(result["details"]["failures"]),
        )

    def test_runner_disables_installed_launch_bytecode_before_overlay_build(self) -> None:
        runner = (
            Path(__file__).resolve().parents[1]
            / "scripts/run_five_uav_health.sh"
        ).read_text(encoding="utf-8")
        dont_write = "export PYTHONDONTWRITEBYTECODE=1"
        cache_prefix = "export PYTHONPYCACHEPREFIX=/tmp/ams-m1-pycache"
        build = "BUILD_COMMAND=("
        self.assertEqual(runner.count(dont_write), 1)
        self.assertEqual(runner.count(cache_prefix), 1)
        self.assertLess(runner.index(dont_write), runner.index(build))
        self.assertLess(runner.index(cache_prefix), runner.index(build))

    def test_mutated_installed_runtime_config_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = self.make_fixture(root)
            installed = (
                run_dir
                / "runtime_overlay/install/multiagent_simulation/share/"
                "multiagent_simulation/config/gazebo-iris.parm"
            )
            installed.write_text("FRAME_CLASS 99\n", encoding="utf-8")
            with mock.patch.object(validator, "ROOT_DIR", root):
                result = validator.runtime_inputs_status(run_dir)
        self.assertEqual(result["status"], "failed")
        self.assertIn(
            "installed package inputs differ",
            "\n".join(result["details"]["failures"]),
        )

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
        self.assertIn("raw Gazebo server argv differs", "\n".join(result["details"]["failures"]))

    def test_forged_human_arguments_cannot_hide_wrong_raw_launch_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = self.make_fixture(root)
            provenance = json.loads((run_dir / "metrics/provenance.json").read_text())
            health = json.loads((run_dir / "metrics/five_uav_health.json").read_text())
            record = json.loads((run_dir / producer.M1_SCENE_RECORD).read_text())
            self.write_raw(
                run_dir,
                health["runtime_id"],
                provenance["source_hash"],
                record["installed"]["runtime_active_world_path"],
                raw_robot_model="forged_model",
                forged_human_launch_arguments=(
                    "ros2 launch multiagent_simulation multiagent_simulation.launch.py "
                    "world_file:=fixture/model.sdf robot_model:=iris_radio_headless"
                ),
            )
            with mock.patch.object(validator, "ROOT_DIR", root):
                result = validator.scene_status(run_dir)
        self.assertEqual(result["status"], "failed")
        self.assertIn(
            "raw launch robot_model assignment differs",
            "\n".join(result["details"]["failures"]),
        )

    def test_raw_launch_must_use_canonical_robots_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = self.make_fixture(root)
            provenance = json.loads((run_dir / "metrics/provenance.json").read_text())
            health = json.loads((run_dir / "metrics/five_uav_health.json").read_text())
            record = json.loads((run_dir / producer.M1_SCENE_RECORD).read_text())
            self.write_raw(
                run_dir,
                health["runtime_id"],
                provenance["source_hash"],
                record["installed"]["runtime_active_world_path"],
                launch_assignment_overrides={
                    "robots_config_file": "/tmp/forged_robots.yaml"
                },
            )
            with mock.patch.object(validator, "ROOT_DIR", root):
                result = validator.scene_status(run_dir)
        self.assertEqual(result["status"], "failed")
        self.assertIn(
            "raw launch robots_config_file assignment differs",
            "\n".join(result["details"]["failures"]),
        )

    def test_raw_launch_feature_flags_and_serial2_must_be_false(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = self.make_fixture(root)
            provenance = json.loads((run_dir / "metrics/provenance.json").read_text())
            health = json.loads((run_dir / "metrics/five_uav_health.json").read_text())
            record = json.loads((run_dir / producer.M1_SCENE_RECORD).read_text())
            self.write_raw(
                run_dir,
                health["runtime_id"],
                provenance["source_hash"],
                record["installed"]["runtime_active_world_path"],
                launch_assignment_overrides={"enable_serial2": "true", "gui": "true"},
            )
            with mock.patch.object(validator, "ROOT_DIR", root):
                result = validator.scene_status(run_dir)
        self.assertEqual(result["status"], "failed")
        failures = "\n".join(result["details"]["failures"])
        self.assertIn("raw launch enable_serial2 assignment differs", failures)
        self.assertIn("raw launch gui assignment differs", failures)

    def test_nested_entity_names_cannot_impersonate_top_level_models(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = self.make_fixture(root)
            provenance = json.loads((run_dir / "metrics/provenance.json").read_text())
            health = json.loads((run_dir / "metrics/five_uav_health.json").read_text())
            record = json.loads((run_dir / producer.M1_SCENE_RECORD).read_text())
            forged_scene = (
                "model { name: \"carrier\" "
                + " ".join(
                    f'link {{ name: "uav{index}" }}' for index in range(1, 6)
                )
                + " }"
            ).encode("utf-8")
            self.write_raw(
                run_dir,
                health["runtime_id"],
                provenance["source_hash"],
                record["installed"]["runtime_active_world_path"],
                gazebo_response_override=forged_scene,
            )
            with mock.patch.object(validator, "ROOT_DIR", root):
                result = validator.scene_status(run_dir)
        self.assertEqual(result["status"], "failed")
        self.assertIn(
            "raw Gazebo entity response is not exactly uav1..uav5",
            "\n".join(result["details"]["failures"]),
        )

    def test_commented_model_names_cannot_impersonate_top_level_models(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = self.make_fixture(root)
            provenance = json.loads((run_dir / "metrics/provenance.json").read_text())
            health = json.loads((run_dir / "metrics/five_uav_health.json").read_text())
            record = json.loads((run_dir / producer.M1_SCENE_RECORD).read_text())
            forged_scene = "\n".join(
                f'# model {{ name: "uav{index}" }}' for index in range(1, 6)
            ).encode("utf-8")
            self.write_raw(
                run_dir,
                health["runtime_id"],
                provenance["source_hash"],
                record["installed"]["runtime_active_world_path"],
                gazebo_response_override=forged_scene,
            )
            with mock.patch.object(validator, "ROOT_DIR", root):
                result = validator.scene_status(run_dir)
        self.assertEqual(result["status"], "failed")
        self.assertIn(
            "raw Gazebo entity response is not exactly uav1..uav5",
            "\n".join(result["details"]["failures"]),
        )

    def test_installed_bundle_mutation_fails_after_producer_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = self.make_fixture(root)
            installed_asset = (
                run_dir
                / "runtime_overlay/install/multiagent_simulation/share/multiagent_simulation/worlds/fixture/terrain.bin"
            )
            installed_asset.write_bytes(b"mutated-after-producer")
            with mock.patch.object(validator, "ROOT_DIR", root):
                result = validator.scene_status(run_dir)
        self.assertEqual(result["status"], "failed")
        self.assertIn(
            "active installed world bundle differs",
            "\n".join(result["details"]["failures"]),
        )

    def test_transitive_model_mesh_mutation_fails_after_producer_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = self.make_fixture(root)
            installed_mesh = (
                run_dir
                / "runtime_overlay/install/multiagent_simulation/share/multiagent_simulation/models/iris/meshes/iris.dae"
            )
            installed_mesh.write_text("<COLLADA>mutated</COLLADA>\n", encoding="utf-8")
            with mock.patch.object(validator, "ROOT_DIR", root):
                result = validator.scene_status(run_dir)
        self.assertEqual(result["status"], "failed")
        self.assertIn(
            "installed transitive scene resources differ",
            "\n".join(result["details"]["failures"]),
        )

    def test_transitive_collada_texture_mutation_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = self.make_fixture(root)
            source_texture = (
                root
                / "src/multiagent_simulation/worlds/fixture/texture.png"
            )
            source_texture.write_bytes(b"mutated-transitive-texture")
            with mock.patch.object(validator, "ROOT_DIR", root):
                result = validator.scene_status(run_dir)
        self.assertEqual(result["status"], "failed")
        self.assertIn(
            "transitive source resource manifest differs",
            "\n".join(result["details"]["failures"]),
        )

    def test_live_robot_description_wrong_fdm_port_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = self.make_fixture(root)
            provenance = json.loads((run_dir / "metrics/provenance.json").read_text())
            health = json.loads((run_dir / "metrics/five_uav_health.json").read_text())
            record = json.loads((run_dir / producer.M1_SCENE_RECORD).read_text())
            self.write_raw(
                run_dir,
                health["runtime_id"],
                provenance["source_hash"],
                record["installed"]["runtime_active_world_path"],
                robot_description_port_override=9999,
            )
            with mock.patch.object(validator, "ROOT_DIR", root):
                result = validator.scene_status(run_dir)
        self.assertEqual(result["status"], "failed")
        self.assertIn(
            "live robot-description hash differs",
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
            ), mock.patch.object(
                validator, "scene_status", return_value=passed
            ), mock.patch.object(
                validator, "runtime_inputs_status", return_value=passed
            ):
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
