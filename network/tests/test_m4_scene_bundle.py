#!/usr/bin/env python3
"""Adversarial tests for the independently derived M4 scene contract."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable

from network.validation.validate_m4_scene_bundle import (
    DEFAULT_BUNDLE,
    EXPECTED_ASSETS,
    ROOT,
    ZERO_SHA256,
    canonical_json,
    validate_scene_bundle,
)
from network.validation.m4_runtime import FROZEN_BUNDLE_SHA256


class M4SceneBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        for relative in EXPECTED_ASSETS:
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, target)
        self.bundle_path = self.root / "network/config/m4_canonical_scene_bundle.json"
        self.bundle_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(DEFAULT_BUNDLE, self.bundle_path)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def load_bundle(self) -> dict[str, Any]:
        return json.loads(self.bundle_path.read_text(encoding="utf-8"))

    def refresh_and_write(self, bundle: dict[str, Any], *, refresh_assets: bool = False) -> None:
        if refresh_assets:
            for record in bundle["assets"]:
                payload = (self.root / record["path"]).read_bytes()
                record["sha256"] = hashlib.sha256(payload).hexdigest()
                record["size_bytes"] = len(payload)
            bundle["asset_manifest_sha256"] = hashlib.sha256(canonical_json(bundle["assets"])).hexdigest()
            material = self.root / "src/multiagent_simulation/worlds/m4_canonical/material_manifest.json"
            bundle["scene_material_manifest_sha256"] = hashlib.sha256(material.read_bytes()).hexdigest()
            for profile in ("low", "medium"):
                path = self.root / bundle["agl_paths"][profile]["path"]
                bundle["agl_paths"][profile]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        bundle["bundle_sha256"] = ZERO_SHA256
        bundle["bundle_sha256"] = hashlib.sha256(canonical_json(bundle)).hexdigest()
        self.bundle_path.write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def mutate_bundle(self, mutation: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        bundle = self.load_bundle()
        mutation(bundle)
        self.refresh_and_write(bundle)
        return validate_scene_bundle(self.bundle_path, self.root)

    def assert_failed_gate(self, result: dict[str, Any], gate: str) -> None:
        self.assertEqual("FAIL", result["status"])
        self.assertFalse(result["gates"][gate]["passed"], result)

    def test_tracked_bundle_passes_all_machine_gates(self) -> None:
        result = validate_scene_bundle(self.bundle_path, self.root)
        self.assertEqual("PASS", result["status"], result)
        self.assertTrue(all(gate["passed"] for gate in result["gates"].values()))

    def test_tracked_bundle_matches_frozen_runtime_identity(self) -> None:
        self.assertEqual(
            self.load_bundle()["bundle_sha256"],
            FROZEN_BUNDLE_SHA256,
            "a regenerated canonical bundle must also rebind the frozen M4 runtime identity",
        )

    def test_generator_check_proves_deterministic_tracked_bytes(self) -> None:
        completed = subprocess.run(
            ["python3", "network/scripts/generate_m4_canonical_scene.py", "--check"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)

    def test_m4_physics_cannot_fall_below_ardupilot_prearm_rate(self) -> None:
        path = (
            self.root
            / "src/multiagent_simulation/worlds/m4_canonical/m4_canonical.sdf"
        )
        path.write_text(
            path.read_text(encoding="utf-8")
            .replace(
                "<max_step_size>0.00125</max_step_size>",
                "<max_step_size>0.004</max_step_size>",
            )
            .replace(
                "<real_time_update_rate>800</real_time_update_rate>",
                "<real_time_update_rate>250</real_time_update_rate>",
            ),
            encoding="utf-8",
        )
        bundle = self.load_bundle()
        self.refresh_and_write(bundle, refresh_assets=True)
        self.assert_failed_gate(
            validate_scene_bundle(self.bundle_path, self.root), "gazebo_physics"
        )

    def test_m4_physics_cannot_revert_to_dantzig_solver(self) -> None:
        path = (
            self.root
            / "src/multiagent_simulation/worlds/m4_canonical/m4_canonical.sdf"
        )
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "<solver_type>pgs</solver_type>",
                "<solver_type>dantzig</solver_type>",
            ),
            encoding="utf-8",
        )
        bundle = self.load_bundle()
        self.refresh_and_write(bundle, refresh_assets=True)
        self.assert_failed_gate(
            validate_scene_bundle(self.bundle_path, self.root), "gazebo_physics"
        )

    def test_m4_physics_requires_bullet_collision_detector(self) -> None:
        path = (
            self.root
            / "src/multiagent_simulation/worlds/m4_canonical/m4_canonical.sdf"
        )
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "<collision_detector>bullet</collision_detector>",
                "<collision_detector>fcl</collision_detector>",
            ),
            encoding="utf-8",
        )
        bundle = self.load_bundle()
        self.refresh_and_write(bundle, refresh_assets=True)
        self.assert_failed_gate(
            validate_scene_bundle(self.bundle_path, self.root), "gazebo_physics"
        )

    def test_capacity_baseline_requires_jammer_disabled(self) -> None:
        jammer_path = self.root / "network/config/jammers_m4_canonical.yaml"
        jammer_path.write_text(
            jammer_path.read_text(encoding="utf-8").replace(
                "enabled: false", "enabled: true", 1
            ),
            encoding="utf-8",
        )
        bundle = self.load_bundle()
        self.refresh_and_write(bundle, refresh_assets=True)
        self.assert_failed_gate(
            validate_scene_bundle(self.bundle_path, self.root), "runtime_configs"
        )

    def test_bundle_self_hash_substitution_is_rejected(self) -> None:
        bundle = self.load_bundle()
        bundle["bundle_id"] = "substituted"
        self.bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
        self.assert_failed_gate(validate_scene_bundle(self.bundle_path, self.root), "identity")

    def test_duplicate_bundle_key_is_rejected_before_semantics(self) -> None:
        payload = self.bundle_path.read_text(encoding="utf-8")
        self.bundle_path.write_text(payload.replace("{", '{"contract":"duplicate",', 1), encoding="utf-8")
        result = validate_scene_bundle(self.bundle_path, self.root)
        self.assertEqual("FAIL", result["status"])
        self.assertIn("duplicate JSON key", result["failures"][0])

    def test_asset_byte_substitution_is_rejected(self) -> None:
        path = self.root / "src/multiagent_simulation/worlds/m4_canonical/terrain.obj"
        path.write_bytes(path.read_bytes() + b"# mutation\n")
        self.assert_failed_gate(validate_scene_bundle(self.bundle_path, self.root), "asset_closure")

    def test_symlinked_transitive_asset_is_rejected(self) -> None:
        path = self.root / "src/multiagent_simulation/worlds/m4_canonical/terrain.obj"
        saved = self.root / "terrain.saved"
        shutil.copy2(path, saved)
        path.unlink()
        path.symlink_to(saved)
        self.assert_failed_gate(validate_scene_bundle(self.bundle_path, self.root), "asset_closure")

    def test_relief_below_150m_fails_even_with_valid_new_bundle_hash(self) -> None:
        result = self.mutate_bundle(lambda bundle: bundle["relief"].update({"delta_m": 140.0}))
        self.assert_failed_gate(result, "terrain_relief")

    def test_landmark_error_above_one_metre_fails_with_rebound_bundle(self) -> None:
        def mutation(bundle: dict[str, Any]) -> None:
            bundle["landmarks"][0]["sionna_sample_m"][0] += 1.01

        self.assert_failed_gate(self.mutate_bundle(mutation), "landmark_alignment")

    def test_ros_frame_handedness_or_quaternion_mutation_is_rejected(self) -> None:
        def mutation(bundle: dict[str, Any]) -> None:
            frame = bundle["frame_contract"]["frames"]["ros_odometry"]
            frame["handedness"] = "left"
            frame["quaternion_order"] = "wxyz"

        self.assert_failed_gate(self.mutate_bundle(mutation), "frames_and_bounds")

    def test_ned_to_enu_axis_sign_mutation_is_rejected(self) -> None:
        def mutation(bundle: dict[str, Any]) -> None:
            transform = bundle["frame_contract"]["transforms"][
                "ardupilot_local_ned_delta_to_gazebo_enu_delta"
            ]
            transform["matrix_3x3"][2][2] = 1.0
            transform["equation"] = "enu_delta=[ned_y,ned_x,ned_z]"
            bundle["frame_contract"]["fixtures"][0][
                "expected_gazebo_enu_m"
            ][2] = -30.0

        self.assert_failed_gate(self.mutate_bundle(mutation), "frames_and_bounds")

    def test_global_position_unit_or_correspondence_tolerance_mutation_fails(self) -> None:
        def mutation(bundle: dict[str, Any]) -> None:
            global_frame = bundle["frame_contract"]["frames"][
                "ardupilot_global_position_int"
            ]
            global_frame["relative_altitude_unit"] = "m"
            bundle["frame_contract"]["runtime_correspondence"][
                "relative_altitude_max_abs_error_m"
            ] = 30.0

        self.assert_failed_gate(self.mutate_bundle(mutation), "frames_and_bounds")

    def test_wgs84_origin_must_match_actual_gazebo_spherical_coordinates(self) -> None:
        path = (
            self.root
            / "src/multiagent_simulation/worlds/m4_canonical/m4_canonical.sdf"
        )
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "<latitude_deg>-35.3632621</latitude_deg>",
                "<latitude_deg>-35.0</latitude_deg>",
            ),
            encoding="utf-8",
        )
        bundle = self.load_bundle()
        self.refresh_and_write(bundle, refresh_assets=True)
        self.assert_failed_gate(
            validate_scene_bundle(self.bundle_path, self.root), "frames_and_bounds"
        )

    def test_sitl_home_must_match_gazebo_wgs84_origin(self) -> None:
        path = self.root / "network/config/scenario_m4_canonical.yaml"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                'sitl_home: "-35.3632621,149.1652374,0,0"',
                'sitl_home: "-35.0,149.0,0,0"',
            ),
            encoding="utf-8",
        )
        bundle = self.load_bundle()
        self.refresh_and_write(bundle, refresh_assets=True)
        self.assert_failed_gate(
            validate_scene_bundle(self.bundle_path, self.root), "runtime_configs"
        )

    def test_highrise_floor_convention_mutation_is_rejected(self) -> None:
        def mutation(bundle: dict[str, Any]) -> None:
            bundle["building_clusters"][1]["buildings"][1]["floors"] = 11

        self.assert_failed_gate(self.mutate_bundle(mutation), "building_clusters")

    def test_range_distance_claim_not_derived_from_endpoints_is_rejected(self) -> None:
        def mutation(bundle: dict[str, Any]) -> None:
            bundle["range_fixtures"][3]["distance_m"] = 19999.0

        self.assert_failed_gate(self.mutate_bundle(mutation), "range_geometry")

    def test_terrain_down_pose_moved_clear_is_rejected(self) -> None:
        def mutation(bundle: dict[str, Any]) -> None:
            bundle["causal_scenarios"]["terrain_shadow"]["pose_sets"]["terrain_down"]["uav1"][2] = 400.0

        self.assert_failed_gate(self.mutate_bundle(mutation), "causal_geometry")

    def test_agl_under_20m_is_rejected_after_all_hashes_are_rebound(self) -> None:
        relative = "src/multiagent_simulation/worlds/m4_canonical/low_agl_path.csv"
        path = self.root / relative
        rows = list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8"))))
        rows[10]["altitude_z_m"] = str(float(rows[10]["terrain_z_m"]) + 19.0)
        rows[10]["agl_m"] = "19.0"
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        path.write_text(stream.getvalue(), encoding="utf-8")
        bundle = self.load_bundle()
        self.refresh_and_write(bundle, refresh_assets=True)
        self.assert_failed_gate(validate_scene_bundle(self.bundle_path, self.root), "agl_corridors")

    def test_sionna_mesh_reference_removal_fails_after_hash_rebind(self) -> None:
        path = self.root / "src/multiagent_simulation/worlds/m4_canonical/sionna_scene.xml"
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if "mesh-canonical-landmarks" not in line]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        bundle = self.load_bundle()
        self.refresh_and_write(bundle, refresh_assets=True)
        self.assert_failed_gate(validate_scene_bundle(self.bundle_path, self.root), "shared_scene_references")

    def test_runtime_config_cannot_select_a_different_scene(self) -> None:
        path = self.root / "network/config/radio_m4_canonical.yaml"
        path.write_text(path.read_text(encoding="utf-8").replace("ams-m4-canonical-km-v2", "other-scene"), encoding="utf-8")
        bundle = self.load_bundle()
        self.refresh_and_write(bundle, refresh_assets=True)
        self.assert_failed_gate(validate_scene_bundle(self.bundle_path, self.root), "runtime_configs")

    def test_service_tier_boundary_mutation_fails_after_hash_rebind(self) -> None:
        path = self.root / "network/config/radio_m4_canonical.yaml"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "min_sinr_db: 11.0", "min_sinr_db: 10.5"
            ),
            encoding="utf-8",
        )
        bundle = self.load_bundle()
        self.refresh_and_write(bundle, refresh_assets=True)
        self.assert_failed_gate(
            validate_scene_bundle(self.bundle_path, self.root), "runtime_configs"
        )

    def test_surface_epsilon_mutation_fails_after_hash_rebind(self) -> None:
        path = self.root / "network/config/radio_m4_canonical.yaml"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "surface_epsilon_m: 0.05", "surface_epsilon_m: 0.051"
            ),
            encoding="utf-8",
        )
        bundle = self.load_bundle()
        self.refresh_and_write(bundle, refresh_assets=True)
        self.assert_failed_gate(
            validate_scene_bundle(self.bundle_path, self.root), "runtime_configs"
        )

    def test_uav_spawn_without_collision_clearance_fails_after_hash_rebind(self) -> None:
        path = self.root / "network/config/scenario_m4_canonical.yaml"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "[-4000.0, -2000.0, 84.25,", "[-4000.0, -2000.0, 84.0,"
            ),
            encoding="utf-8",
        )
        bundle = self.load_bundle()
        self.refresh_and_write(bundle, refresh_assets=True)
        self.assert_failed_gate(
            validate_scene_bundle(self.bundle_path, self.root), "runtime_configs"
        )

    def test_missing_gazebo_jammer_entity_fails_after_hash_rebind(self) -> None:
        path = self.root / "src/multiagent_simulation/worlds/m4_canonical/m4_canonical.sdf"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                '<model name="jammer_m4">', '<model name="wrong_jammer">'
            ),
            encoding="utf-8",
        )
        bundle = self.load_bundle()
        self.refresh_and_write(bundle, refresh_assets=True)
        self.assert_failed_gate(
            validate_scene_bundle(self.bundle_path, self.root),
            "gazebo_radio_entities",
        )


if __name__ == "__main__":
    unittest.main()
