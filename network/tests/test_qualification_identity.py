#!/usr/bin/env python3
"""Adversarial tests for committed-tree qualification identity."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Callable
from unittest import mock

import yaml


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

EXPECTED_REPOSITORY_Q0_CRITICAL = {
    "network/config/component_acceptance_profiles.json",
    "network/scripts/finalize_component_host.py",
    "network/scripts/host_finalization_common.py",
    "network/scripts/resolve_component_prerequisites.py",
    "network/tests/test_component_finalizer.py",
    "network/tests/test_component_prerequisites.py",
    "network/tests/test_component_profiles.py",
    "network/validation/component_profiles.py",
}
EXPECTED_REPOSITORY_Q1 = {
    "network/config/flight_capacity_profile.json",
    "network/config/qualification_test_manifest_q1.json",
    "network/config/scenario_5uav.yaml",
    "network/scripts/collect_flight_capacity.py",
    "network/scripts/finalize_m1_host.py",
    "network/scripts/run_five_uav_capacity.sh",
    "network/scripts/run_five_uav_health.sh",
    "network/scripts/validate_flight_capacity.py",
    "network/scripts/validate_m1_health.py",
    "network/scripts/write_m1_scene_provenance.py",
    "network/tests/check_five_uav_micro_ros_ports.sh",
    "network/tests/collect_five_uav_health.py",
    "network/tests/test_five_uav_health_v2.py",
    "network/tests/test_flight_capacity.py",
    "network/tests/test_m1_health_validator.py",
    "network/tests/test_m1_host_finalizer.py",
    "src/multiagent_simulation/config/gazebo-iris.parm",
    "src/multiagent_simulation/launch/multiagent_simulation.launch.py",
    "src/multiagent_simulation/models/iris/meshes/iris.dae",
    "src/multiagent_simulation/models/iris/meshes/iris_prop_ccw.dae",
    "src/multiagent_simulation/models/iris/meshes/iris_prop_cw.dae",
    "src/multiagent_simulation/models/iris/model.config",
    "src/multiagent_simulation/models/iris/model.sdf",
    "src/multiagent_simulation/models/iris_radio_headless/model.config",
    "src/multiagent_simulation/models/iris_radio_headless/model.sdf",
    "src/multiagent_simulation/package.xml",
    "src/multiagent_simulation/resource/multiagent_simulation",
    "src/multiagent_simulation/setup.cfg",
    "src/multiagent_simulation/setup.py",
    "src/multiagent_simulation/worlds/modelflughafen/heightmap.npz",
    "src/multiagent_simulation/worlds/modelflughafen/model.sdf",
    "src/multiagent_simulation/worlds/modelflughafen/modelflughafen.dae",
    "src/multiagent_simulation/worlds/modelflughafen/odm_textured_model_geo_material0000_map_Kd.png",
    "src/multiagent_simulation/worlds/modelflughafen/odm_textured_model_geo_material0001_map_Kd.png",
    "src/multiagent_simulation/worlds/modelflughafen/odm_textured_model_geo_material0002_map_Kd.png",
}
EXPECTED_REPOSITORY_Q2 = {
    "network/bridge/opaque_udp_relay.py",
    "network/bridge/uav_mavlink_endpoint.py",
    "network/config/endpoint_matrix_5uav.json",
    "network/config/endpoint_transaction_schema.json",
    "network/config/endpoints.yaml",
    "network/config/qualification_test_manifest_q2.json",
    "network/config/radio_backend.yaml",
    "network/config/scenario_1uav_vertical_slice.yaml",
    "network/ns3/build_ns3_tap.sh",
    "network/ns3/build_ns3_tap_packet_engine.sh",
    "network/ns3/packet_core_modes.py",
    "network/ns3/run_ns3_tap_packet_engine.sh",
    "network/ns3/run_ns3_tap_slice.sh",
    "network/ns3/scratch/ams-tap-packet-engine.cc",
    "network/ns3/scratch/ams-tap-vertical-slice.cc",
    "network/ns3/tap_packet_engine_config.py",
    "network/scripts/raw_packet_capture.py",
    "network/scripts/m2_lifecycle_event.py",
    "network/scripts/m2_lifecycle_monitor.py",
    "network/scripts/run_one_uav_vertical_slice.sh",
    "network/scripts/setup_one_uav_netns.sh",
    "network/tests/check_no_bypass.sh",
    "network/tests/mavlink_vertical_slice_probe.py",
    "network/tests/test_endpoint_transaction.py",
    "network/tests/test_m2_vertical_slice_validator.py",
    "network/tests/test_m2_lifecycle_event.py",
    "network/tests/test_m2_lifecycle_monitor.py",
    "network/tests/test_mavlink_vertical_slice_probe.py",
    "network/tests/test_ns3_build_receipt.py",
    "network/tests/test_ns3_tap_packet_engine.py",
    "network/tests/test_p0_config_consistency.py",
    "network/tests/test_provenance_v2.py",
    "network/tests/test_raw_packet_capture.py",
    "network/tests/test_uav_mavlink_endpoint.py",
    "network/tests/test_validator_hardening_v2.py",
    "network/tests/udp_vertical_slice_smoke.py",
    "network/validation/endpoint_transaction.py",
    "network/validation/validate_m2_vertical_slice.py",
}
EXPECTED_REPOSITORY_Q3 = {
    "network/bridge/actual_sitl_mavlink_endpoint.py",
    "network/bridge/runtime_clock_beacon.py",
    "network/config/qualification_test_manifest_q3.json",
    "network/scripts/actual_sitl_control_probe.py",
    "network/scripts/actual_sitl_endpoint_orchestrator.py",
    "network/scripts/actual_sitl_stack_orchestrator.sh",
    "network/scripts/m3_external_matrix_probe.py",
    "network/scripts/m3_topology_monitor.py",
    "network/scripts/run_m3_external_matrix.sh",
    "network/scripts/validate_m3_external_matrix.py",
    "network/tests/test_actual_sitl_control_probe.py",
    "network/tests/test_actual_sitl_mavlink_endpoint.py",
    "network/tests/test_m3_external_matrix_validator.py",
    "network/validation/validate_m3_external_matrix.py",
}
EXPECTED_REPOSITORY_LATER_MANIFESTS = {
    "Q4": {
        "network/config/jammers_m4_canonical.yaml",
        "network/config/m4_canonical_scene_bundle.json",
        "network/config/qualification_test_manifest_q4.json",
        "network/config/radio_m4_canonical.yaml",
        "network/config/scenario_m4_canonical.yaml",
        "network/config/sionna_async_protocol_v1.json",
        "network/config/sionna_async_schema_v1.json",
        "network/config/sionna_packet_effects_v1.json",
        "network/ns3/run_ns3_sionna_rt_live.sh",
        "network/radio_provider/provider.py",
        "network/radio_provider/sionna_async.py",
        "network/radio_provider/sionna_async_service.py",
        "network/radio_provider/sionna_packet_adapter.py",
        "network/scripts/check_m4_canonical_scene_runtime.py",
        "network/scripts/collect_m4_clock_correlations.py",
        "network/scripts/collect_m4_runtime.py",
        "network/scripts/generate_m4_canonical_scene.py",
        "network/scripts/m4_adapter_runtime.py",
        "network/scripts/m4_capacity_airborne.py",
        "network/scripts/m4_causal_phase_driver.py",
        "network/scripts/m4_endpoint_agent.py",
        "network/scripts/m4_gazebo_pose_source.py",
        "network/scripts/m4_runtime_orchestrator.py",
        "network/scripts/run_m4_capacity.sh",
        "network/scripts/run_m4_causality.sh",
        "network/scripts/validate_m4_capacity.py",
        "network/scripts/validate_m4_causality.py",
        "network/tests/test_m4_airborne_motion.py",
        "network/tests/test_m4_capacity_budget.py",
        "network/tests/test_m4_capacity_validator.py",
        "network/tests/test_m4_causality_validator.py",
        "network/tests/test_m4_frame_alignment.py",
        "network/tests/test_m4_gazebo_pose_source.py",
        "network/tests/test_m4_pose_lineage.py",
        "network/tests/test_m4_scene_bundle.py",
        "network/tests/test_ns3_sionna_packet_engine.py",
        "network/tests/test_sionna_async_protocol.py",
        "network/tests/test_sionna_async_service.py",
        "network/tests/test_sionna_packet_adapter.py",
        "network/tests/test_sionna_provider.py",
        "network/validation/m4_airborne_motion.py",
        "network/validation/m4_capacity_budget.py",
        "network/validation/m4_common.py",
        "network/validation/m4_frame_alignment.py",
        "network/validation/m4_pose_observations.py",
        "network/validation/m4_runtime.py",
        "network/validation/validate_m4_capacity.py",
        "network/validation/validate_m4_causality.py",
        "network/validation/validate_m4_scene_bundle.py",
        "src/multiagent_simulation/worlds/m4_canonical/buildings.obj",
        "src/multiagent_simulation/worlds/m4_canonical/landmarks.obj",
        "src/multiagent_simulation/worlds/m4_canonical/low_agl_path.csv",
        "src/multiagent_simulation/worlds/m4_canonical/m4_canonical.sdf",
        "src/multiagent_simulation/worlds/m4_canonical/material_manifest.json",
        "src/multiagent_simulation/worlds/m4_canonical/medium_agl_path.csv",
        "src/multiagent_simulation/worlds/m4_canonical/sionna_scene.xml",
        "src/multiagent_simulation/worlds/m4_canonical/terrain.obj",
    },
    "Q5": {"network/config/qualification_test_manifest_q5.json"},
    "Q6": {"network/config/qualification_test_manifest_q6.json"},
    "Q7": {"network/config/qualification_test_manifest_q7.json"},
    "Q8": {"network/config/qualification_test_manifest_q8.json"},
}

from network.validation.qualification_identity import (  # noqa: E402
    BOUNDED_ROOT_IN_RUNTIME_MODE,
    DEFERRED_M0_CAPABILITY_MODE,
    MUTABLE_STATUS_OUTPUTS,
    PROFILE_CONSUMED_NODES,
    QUALIFICATION_NODES,
    QualificationIdentityError,
    is_exact_bounded_root_capability_mode,
    is_exact_deferred_m0_capability_mode,
    qualification_checkout_identity,
    qualification_consumption,
    qualification_content_vector,
    qualification_prefixes_equal,
    validate_qualification_consumption,
    validate_recorded_checkout_identity,
    verify_recorded_qualification,
)
from network.validation import qualification_identity as identity_module  # noqa: E402


def _run(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["/usr/bin/git", "-C", str(root), *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(arguments)} failed: {result.stdout}\n{result.stderr}"
        )
    return result.stdout.strip()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _policy() -> dict[str, object]:
    explicit = {f"Q{index}": [] for index in range(9)}
    explicit["Q1"] = ["src/nested/value.txt"]
    explicit["Q2"] = ["scripts/run.sh"]
    return {
        "schema_version": 2,
        "contract": "q0_q1_q2_granular/v1",
        "policy_id": "q0_q1_q2_granular/v1",
        "mutable_status_exclusions": sorted(MUTABLE_STATUS_OUTPUTS),
        "selective_descendant_reuse_allowed": True,
        "profile_consumption": {
            "diagnostic": [],
            "m0": ["Q0"],
            "m1_component": ["Q0", "Q1"],
            "flight_capacity_prerequisite": ["Q0", "Q1"],
            "m2_component": ["Q0", "Q1", "Q2"],
            "m3_component": ["Q0", "Q1", "Q2", "Q3"],
            "m4_capacity_prerequisite": ["Q0", "Q1", "Q2", "Q3", "Q4"],
            "m4_component": ["Q0", "Q1", "Q2", "Q3", "Q4"],
        },
        "default_owner": "Q0",
        "explicit_owners": explicit,
    }


class QualificationIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "repository"
        self.root.mkdir()
        _run(self.root, "init", "--quiet")
        _run(self.root, "config", "user.name", "Qualification Test")
        _run(self.root, "config", "user.email", "qualification@example.invalid")
        _write(
            self.root / "network/config/qualification_path_ownership.json",
            json.dumps(_policy(), indent=2, sort_keys=True) + "\n",
        )
        for relative in MUTABLE_STATUS_OUTPUTS:
            _write(self.root / relative, f"initial {relative}\n")
        _write(self.root / "src/nested/value.txt", "committed nested bytes\n")
        _write(self.root / "scripts/run.sh", "#!/bin/sh\nexit 0\n")
        (self.root / "scripts/run.sh").chmod(0o755)
        (self.root / "links").mkdir()
        (self.root / "links/current").symlink_to("../src/nested/value.txt")
        _run(self.root, "add", "--all")
        _run(self.root, "commit", "--quiet", "-m", "technical base")
        self.base_commit = _run(self.root, "rev-parse", "HEAD")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_git_identity_uses_command_scoped_exact_safe_directory(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"", stderr=b""
        )
        with mock.patch.object(
            identity_module.subprocess, "run", return_value=completed
        ) as runner:
            identity_module._git_completed(self.root, ["rev-parse", "HEAD"])
        argv = runner.call_args.args[0]
        self.assertEqual(argv[0], "/usr/bin/git")
        self.assertIn(
            f"safe.directory={self.root.resolve(strict=True)}",
            argv,
        )
        self.assertNotIn("safe.directory=*", argv)
        environment = runner.call_args.kwargs["env"]
        self.assertNotIn("GIT_CONFIG_COUNT", environment)

    def commit_all(self, message: str) -> str:
        _run(self.root, "add", "--all")
        _run(self.root, "commit", "--quiet", "-m", message)
        return _run(self.root, "rev-parse", "HEAD")

    def commit_policy_mutation(
        self,
        mutation: Callable[[dict[str, object]], None],
        message: str,
    ) -> str:
        policy_path = self.root / "network/config/qualification_path_ownership.json"
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        mutation(policy)
        _write(policy_path, json.dumps(policy, indent=2, sort_keys=True) + "\n")
        return self.commit_all(message)

    def assert_policy_mutation_rejected(
        self,
        mutation: Callable[[dict[str, object]], None],
        expected_message: str,
        commit_message: str,
    ) -> None:
        commit = self.commit_policy_mutation(mutation, commit_message)
        with self.assertRaisesRegex(QualificationIdentityError, expected_message):
            qualification_content_vector(self.root, commit)

    def test_every_nonstatus_committed_path_has_exactly_one_owner(self) -> None:
        vector = qualification_content_vector(self.root, self.base_commit)
        committed = set(_run(self.root, "ls-files").splitlines())
        expected = committed - set(MUTABLE_STATUS_OUTPUTS)
        self.assertEqual(set(vector["assignments"]), expected)
        self.assertEqual(
            {entry["path"] for entry in vector["entry_manifest"]},
            expected,
        )
        self.assertEqual(len(vector["assignments"]), len(expected))
        self.assertEqual(vector["assignments"]["src/nested/value.txt"], "Q1")
        self.assertEqual(vector["assignments"]["scripts/run.sh"], "Q2")
        for path in expected - {"src/nested/value.txt", "scripts/run.sh"}:
            self.assertEqual(vector["assignments"][path], "Q0", path)

    def test_q1_and_q2_edits_change_only_their_owned_node_hash(self) -> None:
        before = qualification_content_vector(self.root, self.base_commit)

        _write(self.root / "src/nested/value.txt", "new Q1 bytes\n")
        q1_commit = self.commit_all("change Q1 input")
        after_q1 = qualification_content_vector(self.root, q1_commit)
        self.assertNotEqual(before["node_hashes"]["Q1"], after_q1["node_hashes"]["Q1"])
        for node in set(QUALIFICATION_NODES) - {"Q1"}:
            self.assertEqual(before["node_hashes"][node], after_q1["node_hashes"][node])

        _write(self.root / "scripts/run.sh", "#!/bin/sh\nexit 7\n")
        (self.root / "scripts/run.sh").chmod(0o755)
        q2_commit = self.commit_all("change Q2 input")
        after_q2 = qualification_content_vector(self.root, q2_commit)
        self.assertNotEqual(
            after_q1["node_hashes"]["Q2"], after_q2["node_hashes"]["Q2"]
        )
        for node in set(QUALIFICATION_NODES) - {"Q2"}:
            self.assertEqual(
                after_q1["node_hashes"][node], after_q2["node_hashes"][node]
            )

    def test_m1_through_m4_profiles_are_exact(self) -> None:
        vector = qualification_content_vector(self.root, self.base_commit)
        capacity = qualification_consumption(vector, "flight_capacity_prerequisite")
        m2 = qualification_consumption(vector, "m2_component")
        m3 = qualification_consumption(vector, "m3_component")
        m4_capacity = qualification_consumption(vector, "m4_capacity_prerequisite")
        m4 = qualification_consumption(vector, "m4_component")
        self.assertEqual(capacity["consumed_nodes"], ["Q0", "Q1"])
        self.assertEqual(set(capacity["consumed_node_sha256"]), {"Q0", "Q1"})
        self.assertEqual(m2["consumed_nodes"], ["Q0", "Q1", "Q2"])
        self.assertEqual(set(m2["consumed_node_sha256"]), {"Q0", "Q1", "Q2"})
        self.assertEqual(m3["consumed_nodes"], ["Q0", "Q1", "Q2", "Q3"])
        self.assertEqual(
            set(m3["consumed_node_sha256"]), {"Q0", "Q1", "Q2", "Q3"}
        )
        for record in (m4_capacity, m4):
            self.assertEqual(
                record["consumed_nodes"], ["Q0", "Q1", "Q2", "Q3", "Q4"]
            )
            self.assertEqual(
                set(record["consumed_node_sha256"]),
                {"Q0", "Q1", "Q2", "Q3", "Q4"},
            )

    def test_prefix_reuse_ignores_only_future_node_bytes(self) -> None:
        vector = qualification_content_vector(self.root, self.base_commit)
        future = copy.deepcopy(vector)
        future["node_hashes"]["Q2"] = "f" * 64
        self.assertTrue(qualification_prefixes_equal(vector, future, ["Q0"]))
        self.assertTrue(
            qualification_prefixes_equal(vector, future, ["Q0", "Q1"])
        )
        self.assertFalse(
            qualification_prefixes_equal(vector, future, ["Q0", "Q1", "Q2"])
        )

        changed_policy = copy.deepcopy(future)
        changed_policy["policy_sha256"] = "e" * 64
        self.assertFalse(
            qualification_prefixes_equal(vector, changed_policy, ["Q0"])
        )

        changed_q0 = copy.deepcopy(vector)
        changed_q0["node_hashes"]["Q0"] = "d" * 64
        self.assertFalse(qualification_prefixes_equal(vector, changed_q0, ["Q0"]))

    def test_policy_rejects_a_path_owned_by_multiple_nodes(self) -> None:
        def duplicate_across_nodes(policy: dict[str, object]) -> None:
            explicit = policy["explicit_owners"]
            assert isinstance(explicit, dict)
            explicit["Q2"] = ["scripts/run.sh", "src/nested/value.txt"]

        self.assert_policy_mutation_rejected(
            duplicate_across_nodes,
            "multiple explicit owners",
            "forge duplicate owner",
        )

    def test_policy_rejects_an_explicit_path_absent_from_commit(self) -> None:
        def add_absent(policy: dict[str, object]) -> None:
            explicit = policy["explicit_owners"]
            assert isinstance(explicit, dict)
            explicit["Q3"] = ["missing/input.txt"]

        self.assert_policy_mutation_rejected(
            add_absent,
            "absent from the committed tree",
            "forge absent owner",
        )

    def test_policy_rejects_unsafe_explicit_paths(self) -> None:
        def add_unsafe(policy: dict[str, object]) -> None:
            explicit = policy["explicit_owners"]
            assert isinstance(explicit, dict)
            explicit["Q3"] = ["../escape.txt"]

        self.assert_policy_mutation_rejected(
            add_unsafe,
            "unsafe",
            "forge unsafe owner",
        )

    def test_policy_rejects_an_explicit_status_path(self) -> None:
        def add_status(policy: dict[str, object]) -> None:
            explicit = policy["explicit_owners"]
            assert isinstance(explicit, dict)
            explicit["Q3"] = ["network/PROGRESS.md"]

        self.assert_policy_mutation_rejected(
            add_status,
            "mutable status path cannot have a Q owner",
            "forge status owner",
        )

    def test_policy_rejects_reassigning_the_policy_itself(self) -> None:
        def reassign_policy(policy: dict[str, object]) -> None:
            explicit = policy["explicit_owners"]
            assert isinstance(explicit, dict)
            explicit["Q3"] = ["network/config/qualification_path_ownership.json"]

        self.assert_policy_mutation_rejected(
            reassign_policy,
            "policy must remain owned by Q0",
            "forge policy owner",
        )

    def test_policy_rejects_explicit_q0_paths(self) -> None:
        def add_q0(policy: dict[str, object]) -> None:
            explicit = policy["explicit_owners"]
            assert isinstance(explicit, dict)
            explicit["Q0"] = ["links/current"]

        self.assert_policy_mutation_rejected(
            add_q0,
            "explicit Q0 paths are forbidden",
            "forge explicit Q0 owner",
        )

    def test_policy_rejects_unordered_owner_lists(self) -> None:
        def add_unordered(policy: dict[str, object]) -> None:
            explicit = policy["explicit_owners"]
            assert isinstance(explicit, dict)
            explicit["Q3"] = ["src/nested/value.txt", "scripts/run.sh"]

        self.assert_policy_mutation_rejected(
            add_unordered,
            "not sorted and unique",
            "forge unordered owners",
        )

    def test_policy_rejects_duplicate_paths_within_one_owner_list(self) -> None:
        def add_duplicate(policy: dict[str, object]) -> None:
            explicit = policy["explicit_owners"]
            assert isinstance(explicit, dict)
            explicit["Q3"] = ["src/nested/value.txt", "src/nested/value.txt"]

        self.assert_policy_mutation_rejected(
            add_duplicate,
            "not sorted and unique",
            "forge duplicate owner list",
        )

    def test_policy_rejects_an_incomplete_node_map(self) -> None:
        def remove_node(policy: dict[str, object]) -> None:
            explicit = policy["explicit_owners"]
            assert isinstance(explicit, dict)
            del explicit["Q8"]

        self.assert_policy_mutation_rejected(
            remove_node,
            "node map is not exact",
            "forge incomplete node map",
        )

    def test_policy_rejects_nonexact_profiles(self) -> None:
        def remove_profile(policy: dict[str, object]) -> None:
            profiles = policy["profile_consumption"]
            assert isinstance(profiles, dict)
            del profiles["m2_component"]

        self.assert_policy_mutation_rejected(
            remove_profile,
            "contract is not exact",
            "forge incomplete profiles",
        )

    def test_policy_rejects_disabled_selective_reuse(self) -> None:
        self.assert_policy_mutation_rejected(
            lambda policy: policy.__setitem__(
                "selective_descendant_reuse_allowed", False
            ),
            "contract is not exact",
            "forge disabled selective reuse",
        )

    def test_policy_rejects_legacy_schema(self) -> None:
        self.assert_policy_mutation_rejected(
            lambda policy: policy.__setitem__("schema_version", 1),
            "contract is not exact",
            "forge legacy schema",
        )

    def test_vector_uses_recursive_committed_objects_not_dirty_checkout(self) -> None:
        before = qualification_content_vector(self.root, self.base_commit)
        nested = next(
            entry
            for entry in before["entry_manifest"]
            if entry["path"] == "src/nested/value.txt"
        )
        executable = next(
            entry
            for entry in before["entry_manifest"]
            if entry["path"] == "scripts/run.sh"
        )
        symlink = next(
            entry
            for entry in before["entry_manifest"]
            if entry["path"] == "links/current"
        )
        self.assertEqual(nested["git_mode"], "100644")
        self.assertEqual(nested["object_type"], "blob")
        self.assertEqual(
            nested["blob_sha256"],
            hashlib.sha256(b"committed nested bytes\n").hexdigest(),
        )
        self.assertEqual(executable["git_mode"], "100755")
        self.assertEqual(symlink["kind"], "symlink")
        self.assertEqual(
            symlink["blob_sha256"],
            hashlib.sha256(b"../src/nested/value.txt").hexdigest(),
        )
        self.assertEqual(nested["owner"], "Q1")
        self.assertEqual(executable["owner"], "Q2")
        self.assertEqual(symlink["owner"], "Q0")
        self.assertTrue(
            all(relative not in before["assignments"] for relative in MUTABLE_STATUS_OUTPUTS)
        )
        self.assertEqual(before["assignments"]["network/config/qualification_path_ownership.json"], "Q0")
        self.assertTrue(before["selective_reuse"])

        self.assertEqual(before["records"][1]["path_count"], 1)
        self.assertEqual(before["records"][2]["path_count"], 1)
        empty_hashes = [before["node_hashes"][f"Q{index}"] for index in range(3, 9)]
        self.assertEqual(len(empty_hashes), len(set(empty_hashes)))
        self.assertTrue(all(record["path_count"] == 0 for record in before["records"][3:]))

        _write(self.root / "src/nested/value.txt", "dirty replacement\n")
        (self.root / "links/current").unlink()
        (self.root / "links/current").symlink_to("../scripts/run.sh")
        after = qualification_content_vector(self.root, self.base_commit)
        self.assertEqual(after, before)
        checkout = qualification_checkout_identity(self.root, self.base_commit)
        self.assertFalse(checkout["checkout_equal"])
        self.assertFalse(checkout["tree_objects_equal"])

    def test_inherited_git_redirection_cannot_change_vector(self) -> None:
        expected = qualification_content_vector(self.root, self.base_commit)
        redirect = Path(self.temporary.name) / "redirect"
        redirect.mkdir()
        _run(redirect, "init", "--quiet")
        with mock.patch.dict(
            os.environ,
            {
                "GIT_DIR": str(redirect / ".git"),
                "GIT_WORK_TREE": str(redirect),
                "GIT_INDEX_FILE": str(redirect / "hostile-index"),
                "GIT_OBJECT_DIRECTORY": str(redirect / ".git/objects"),
                "GIT_CONFIG_GLOBAL": str(redirect / "hostile-config"),
            },
            clear=False,
        ):
            observed = qualification_content_vector(self.root, self.base_commit)
        self.assertEqual(observed, expected)

    def test_profiles_and_consumed_hashes_are_exact(self) -> None:
        self.assertEqual(
            PROFILE_CONSUMED_NODES,
            {
                "diagnostic": (),
                "m0": ("Q0",),
                "m1_component": ("Q0", "Q1"),
                "flight_capacity_prerequisite": ("Q0", "Q1"),
                "m2_component": ("Q0", "Q1", "Q2"),
                "m3_component": ("Q0", "Q1", "Q2", "Q3"),
                "m4_capacity_prerequisite": ("Q0", "Q1", "Q2", "Q3", "Q4"),
                "m4_component": ("Q0", "Q1", "Q2", "Q3", "Q4"),
            },
        )
        vector = qualification_content_vector(self.root, self.base_commit)
        for profile, nodes in PROFILE_CONSUMED_NODES.items():
            record = qualification_consumption(vector, profile)
            self.assertEqual(record["consumed_nodes"], list(nodes))
            self.assertEqual(set(record["consumed_node_sha256"]), set(nodes))
            self.assertEqual(validate_qualification_consumption(record, vector), record)

        forged = qualification_consumption(vector, "m1_component")
        forged["consumed_node_sha256"]["Q1"] = "0" * 64
        with self.assertRaises(QualificationIdentityError):
            validate_qualification_consumption(forged, vector)
        missing = qualification_consumption(vector, "m0")
        del missing["consumed_node_sha256"]
        with self.assertRaises(QualificationIdentityError):
            validate_qualification_consumption(missing, vector)
        with self.assertRaises(QualificationIdentityError):
            qualification_consumption(vector, "m2_invented")

    def test_deferred_capability_mode_is_narrowly_m0_q0_only(self) -> None:
        vector = qualification_content_vector(self.root, self.base_commit)
        m0 = qualification_consumption(vector, "m0")
        self.assertTrue(
            is_exact_deferred_m0_capability_mode(m0, DEFERRED_M0_CAPABILITY_MODE)
        )
        self.assertFalse(
            is_exact_deferred_m0_capability_mode(
                qualification_consumption(vector, "diagnostic"),
                DEFERRED_M0_CAPABILITY_MODE,
            )
        )
        self.assertFalse(
            is_exact_deferred_m0_capability_mode(
                qualification_consumption(vector, "m1_component"),
                DEFERRED_M0_CAPABILITY_MODE,
            )
        )
        forged = dict(m0)
        forged["consumed_nodes"] = []
        self.assertFalse(
            is_exact_deferred_m0_capability_mode(forged, DEFERRED_M0_CAPABILITY_MODE)
        )
        malformed = dict(m0)
        malformed["consumed_node_sha256"] = 7
        self.assertFalse(
            is_exact_deferred_m0_capability_mode(
                malformed, DEFERRED_M0_CAPABILITY_MODE
            )
        )
        self.assertFalse(is_exact_deferred_m0_capability_mode(m0, "in_runtime"))

    def test_bounded_root_mode_is_narrowly_m2_through_m4_only(self) -> None:
        vector = qualification_content_vector(self.root, self.base_commit)
        for profile in (
            "m2_component",
            "m3_component",
            "m4_capacity_prerequisite",
            "m4_component",
        ):
            consumption = qualification_consumption(vector, profile)
            self.assertTrue(
                is_exact_bounded_root_capability_mode(
                    consumption, BOUNDED_ROOT_IN_RUNTIME_MODE
                )
            )
        self.assertFalse(
            is_exact_bounded_root_capability_mode(
                qualification_consumption(vector, "m1_component"),
                BOUNDED_ROOT_IN_RUNTIME_MODE,
            )
        )
        forged = qualification_consumption(vector, "m2_component")
        forged["consumed_node_sha256"]["Q2"] = "invalid"
        self.assertFalse(
            is_exact_bounded_root_capability_mode(
                forged, BOUNDED_ROOT_IN_RUNTIME_MODE
            )
        )
        self.assertFalse(
            is_exact_bounded_root_capability_mode(
                qualification_consumption(vector, "m2_component"), "in_runtime"
            )
        )

    def test_checkout_gate_rejects_hardlinks_even_when_bytes_match(self) -> None:
        vector = qualification_content_vector(self.root, self.base_commit)
        checkout = qualification_checkout_identity(self.root, self.base_commit)
        self.assertTrue(checkout["checkout_equal"])
        self.assertEqual(
            validate_recorded_checkout_identity(checkout, vector), checkout
        )

        original = self.root / "src/nested/value.txt"
        second_link = self.root / "src/nested/second-link.txt"
        os.link(original, second_link)
        dirty = qualification_checkout_identity(self.root, self.base_commit)
        self.assertFalse(dirty["checkout_equal"])
        self.assertFalse(dirty["tree_objects_equal"])

    def test_recorded_vector_is_recomputed_at_its_recorded_commit(self) -> None:
        vector = qualification_content_vector(self.root, self.base_commit)
        consumption = qualification_consumption(vector, "m0")
        forged = copy.deepcopy(vector)
        forged["entry_manifest"][0]["blob_sha256"] = "0" * 64
        with self.assertRaisesRegex(
            QualificationIdentityError, "differs from its recorded commit"
        ):
            verify_recorded_qualification(self.root, forged, consumption)

    def test_exact_three_status_file_descendant_preserves_consumed_hashes(self) -> None:
        vector = qualification_content_vector(self.root, self.base_commit)
        consumption = qualification_consumption(vector, "m1_component")
        checkout = qualification_checkout_identity(self.root, self.base_commit)
        validate_recorded_checkout_identity(checkout, vector)
        for relative in MUTABLE_STATUS_OUTPUTS:
            _write(self.root / relative, f"passed status in {relative}\n")
        descendant = self.commit_all("status-only result")

        relationship = verify_recorded_qualification(
            self.root, vector, consumption
        )
        self.assertEqual(relationship["relationship"], "status_only_descendant")
        self.assertEqual(
            set(relationship["changed_paths"]), set(MUTABLE_STATUS_OUTPUTS)
        )
        current = qualification_content_vector(self.root, descendant)
        self.assertNotEqual(current["git_commit"], vector["git_commit"])
        self.assertEqual(current["vector_sha256"], vector["vector_sha256"])
        self.assertEqual(
            relationship["consumed_node_sha256"],
            consumption["consumed_node_sha256"],
        )

    def test_partial_status_or_nonstatus_descendant_cannot_reuse(self) -> None:
        vector = qualification_content_vector(self.root, self.base_commit)
        consumption = qualification_consumption(vector, "m0")
        _write(self.root / "network/PROGRESS.md", "only one status changed\n")
        self.commit_all("partial status")
        with self.assertRaisesRegex(
            QualificationIdentityError, "exact three-path status-only"
        ):
            verify_recorded_qualification(self.root, vector, consumption)

        for relative in MUTABLE_STATUS_OUTPUTS:
            _write(self.root / relative, f"all status {relative}\n")
        _write(self.root / "src/nested/value.txt", "technical change\n")
        self.commit_all("mixed technical and status")
        with self.assertRaisesRegex(
            QualificationIdentityError, "exact three-path status-only"
        ):
            verify_recorded_qualification(self.root, vector, consumption)

    def test_gitlink_identity_and_dirty_submodule_are_checked(self) -> None:
        submodule_source = Path(self.temporary.name) / "submodule-source"
        submodule_source.mkdir()
        _run(submodule_source, "init", "--quiet")
        _run(submodule_source, "config", "user.name", "Qualification Test")
        _run(submodule_source, "config", "user.email", "qualification@example.invalid")
        _write(submodule_source / "tracked.txt", "submodule bytes\n")
        _run(submodule_source, "add", "--all")
        _run(submodule_source, "commit", "--quiet", "-m", "submodule base")
        subprocess.run(
            [
                "/usr/bin/git",
                "-C",
                str(self.root),
                "-c",
                "protocol.file.allow=always",
                "submodule",
                "add",
                "--quiet",
                str(submodule_source),
                "deps/submodule",
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        commit = self.commit_all("add pinned submodule")
        vector = qualification_content_vector(self.root, commit)
        gitlink = next(
            entry
            for entry in vector["entry_manifest"]
            if entry["path"] == "deps/submodule"
        )
        self.assertEqual(gitlink["git_mode"], "160000")
        self.assertEqual(gitlink["object_type"], "commit")
        self.assertIsNone(gitlink["blob_sha256"])
        self.assertTrue(qualification_checkout_identity(self.root, commit)["checkout_equal"])

        _write(self.root / "deps/submodule/untracked.txt", "dirty submodule\n")
        dirty = qualification_checkout_identity(self.root, commit)
        self.assertFalse(dirty["checkout_equal"])
        self.assertFalse(dirty["submodules_clean"])


class RepositoryQualificationPolicyTests(unittest.TestCase):
    def test_repository_policy_has_the_reviewed_exact_q1_q2_boundary(self) -> None:
        policy_path = ROOT_DIR / "network/config/qualification_path_ownership.json"
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        explicit = policy["explicit_owners"]
        self.assertEqual(policy["schema_version"], 2)
        self.assertEqual(policy["contract"], "q0_q1_q2_granular/v1")
        self.assertEqual(policy["policy_id"], "q0_q1_q2_granular/v1")
        self.assertEqual(policy["default_owner"], "Q0")
        self.assertTrue(policy["selective_descendant_reuse_allowed"])
        self.assertEqual(
            policy["profile_consumption"],
            {
                "diagnostic": [],
                "m0": ["Q0"],
                "m1_component": ["Q0", "Q1"],
                "flight_capacity_prerequisite": ["Q0", "Q1"],
                "m2_component": ["Q0", "Q1", "Q2"],
                "m3_component": ["Q0", "Q1", "Q2", "Q3"],
                "m4_capacity_prerequisite": ["Q0", "Q1", "Q2", "Q3", "Q4"],
                "m4_component": ["Q0", "Q1", "Q2", "Q3", "Q4"],
            },
        )
        self.assertEqual(set(explicit["Q1"]), EXPECTED_REPOSITORY_Q1)
        self.assertEqual(set(explicit["Q2"]), EXPECTED_REPOSITORY_Q2)
        self.assertEqual(set(explicit["Q3"]), EXPECTED_REPOSITORY_Q3)
        self.assertEqual(explicit["Q0"], [])
        for node, expected in EXPECTED_REPOSITORY_LATER_MANIFESTS.items():
            self.assertEqual(set(explicit[node]), expected, node)

        seen: dict[str, str] = {}
        for node in QUALIFICATION_NODES:
            paths = explicit[node]
            self.assertEqual(paths, sorted(paths), node)
            self.assertEqual(len(paths), len(set(paths)), node)
            for relative in paths:
                self.assertNotIn(relative, MUTABLE_STATUS_OUTPUTS)
                self.assertTrue((ROOT_DIR / relative).exists(), relative)
                self.assertNotIn(relative, seen, relative)
                seen[relative] = node

        tracked = set(_run(ROOT_DIR, "ls-files").splitlines())
        inventory = tracked | set(seen) | EXPECTED_REPOSITORY_Q0_CRITICAL | {
            "network/config/qualification_path_ownership.json"
        }
        technical = inventory - set(MUTABLE_STATUS_OUTPUTS)
        assignments = {relative: seen.get(relative, "Q0") for relative in technical}
        self.assertEqual(set(assignments), technical)
        self.assertEqual(len(assignments), len(technical))
        self.assertEqual(assignments["network/config/qualification_path_ownership.json"], "Q0")
        for relative in EXPECTED_REPOSITORY_Q0_CRITICAL:
            self.assertEqual(assignments.get(relative), "Q0", relative)
        self.assertEqual(
            {path for path, owner in assignments.items() if owner == "Q1"},
            EXPECTED_REPOSITORY_Q1,
        )
        self.assertEqual(
            {path for path, owner in assignments.items() if owner == "Q2"},
            EXPECTED_REPOSITORY_Q2,
        )
        self.assertEqual(
            {path for path, owner in assignments.items() if owner == "Q3"},
            EXPECTED_REPOSITORY_Q3,
        )
        for node, expected in EXPECTED_REPOSITORY_LATER_MANIFESTS.items():
            self.assertEqual(
                {path for path, owner in assignments.items() if owner == node},
                expected,
            )

        dependency_lock = yaml.safe_load(
            (ROOT_DIR / "network/config/dependency_lock.yaml").read_text(
                encoding="utf-8"
            )
        )
        critical_executables = dependency_lock["m0_execution_policy"][
            "critical_source_executables"
        ]
        self.assertTrue(critical_executables)
        self.assertEqual(len(critical_executables), len(set(critical_executables)))
        for relative in critical_executables:
            self.assertEqual(assignments.get(relative), "Q0", relative)


if __name__ == "__main__":
    unittest.main()
