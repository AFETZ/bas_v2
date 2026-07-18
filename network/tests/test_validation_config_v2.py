#!/usr/bin/env python3
"""Consistency tests for the v3 plan and retained schema-v2 evidence formats."""

from __future__ import annotations

import json
import hashlib
import re
import sys
import unittest
from pathlib import Path

import yaml


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from network.validation.evidence import P0_GATE_IDS  # noqa: E402
from network.scripts.write_run_provenance import CANONICAL_RUNTIME_SOURCE_PATHS  # noqa: E402
from network.scripts.validate_status_documents import status_documents_status  # noqa: E402


class ValidationConfigV2Tests(unittest.TestCase):
    def test_mutable_status_documents_use_allowed_states_and_agree(self) -> None:
        rows = "".join(
            f"| M{index} | `{status}` | fixture |\n"
            for index, status in enumerate(
                ["passed", "in_progress", *("not_started" for _ in range(7))]
            )
        )
        progress = (
            rows
            + "Fully closed sequential milestones: **1**\n"
            + "Customer-ready: **false**\nActive milestone: **M1**\n"
        )
        report = rows + "Fully closed milestones: **1**\nCustomer-ready: **false**\n"
        next_task = (
            "Fully closed sequential milestones: **1**\nCustomer-ready: **false**\n"
            "Active milestone:\n**M1**\n"
        )
        accepted = status_documents_status(
            progress,
            report,
            next_task,
            m0_receipt={
                "formal_accepted": True,
                "passed": True,
                "scope": {"host_final": True},
                "gates": {"host_final": {"status": "passed"}},
            },
        )
        self.assertFalse(accepted["passed"], accepted)
        self.assertTrue(
            any("host-final receipt" in failure for failure in accepted["failures"]),
            accepted,
        )

        forged_rows = rows.replace("| M0 | `passed` |", "| M0 | `in_progress` |")
        rejected = status_documents_status(
            forged_rows + "Fully closed milestones: **1**\nCustomer-ready: **false**\n",
            report,
            next_task,
        )
        self.assertFalse(rejected["passed"])

    def test_matrix_and_validator_have_exact_same_p0_gate_ids(self) -> None:
        matrix = yaml.safe_load(
            (ROOT_DIR / "network/config/validation_matrix.yaml").read_text()
        )
        matrix_ids = tuple(item["id"] for item in matrix["gates"]["p0"])
        self.assertEqual(matrix_ids, P0_GATE_IDS)

    def test_matrix_points_to_authoritative_v3_plan(self) -> None:
        matrix = yaml.safe_load(
            (ROOT_DIR / "network/config/validation_matrix.yaml").read_text()
        )
        self.assertEqual(matrix["schema_version"], 2)
        self.assertEqual(matrix["plan"], "doc/network_radio_integration_plan_v3.md")
        self.assertEqual(
            matrix["validation_engine"], "network/validation/validate_run.py"
        )
        profiles = matrix["acceptance_profiles"]
        self.assertEqual(profiles["schema"], "v3_profiled")
        self.assertFalse(profiles["customer_ready_enabled"])
        self.assertEqual(profiles["required_customer_profile"], "m8_customer_handoff")

    def test_every_declared_p0_raw_artifact_is_in_the_sealed_set(self) -> None:
        matrix = yaml.safe_load(
            (ROOT_DIR / "network/config/validation_matrix.yaml").read_text()
        )
        sealed = set(matrix["run_outputs"]["raw_runtime_required"])
        declared = {
            relative
            for item in matrix["gates"]["p0"]
            for relative in item.get("raw_evidence", [])
        }
        self.assertFalse(declared - sealed)
        self.assertIn("logs/five_uav_launch.log", sealed)

    def test_json_schemas_are_valid_json_and_v2(self) -> None:
        provenance = json.loads(
            (ROOT_DIR / "network/config/provenance_schema.json").read_text()
        )
        evidence_manifest = json.loads(
            (ROOT_DIR / "network/config/evidence_manifest_schema.json").read_text()
        )
        summary = json.loads(
            (ROOT_DIR / "network/config/metrics_summary_schema.json").read_text()
        )
        self.assertEqual(provenance["properties"]["schema_version"]["const"], 2)
        self.assertTrue(
            {
                "qualification_content_vector",
                "qualification_consumption",
                "qualification_checkout",
                "plan_contract",
            }.issubset(provenance["required"])
        )
        vector = provenance["properties"]["qualification_content_vector"]
        self.assertEqual(
            vector["properties"]["contract"]["const"],
            "ams.qualification-content-vector/v1",
        )
        self.assertTrue(vector["properties"]["selective_reuse"]["const"])
        implementation = provenance["properties"]["implementation"]
        self.assertEqual(
            set(implementation["required"]),
            {
                "packet_ingress_mode",
                "medium_model",
                "radio_provider_id",
                "radio_provider_runtime_consumed",
                "runtime_provider_id",
                "reason",
            },
        )
        self.assertFalse(implementation["additionalProperties"])
        self.assertEqual(
            vector["properties"]["policy_id"]["const"],
            "q0_q1_q2_granular/v1",
        )
        self.assertEqual(
            set(vector["properties"]["node_hashes"]["required"]),
            {f"Q{index}" for index in range(9)},
        )
        consumption = provenance["properties"]["qualification_consumption"]
        self.assertEqual(
            consumption["properties"]["profile"]["enum"],
            [
                "diagnostic",
                "m0",
                "m1_component",
                "flight_capacity_prerequisite",
                "m2_component",
                "m3_component",
                "m4_capacity_prerequisite",
                "m4_component",
            ],
        )
        profile_nodes = {
            clause["if"]["properties"]["profile"]["const"]: clause["then"][
                "properties"
            ]["consumed_nodes"].get("const", [])
            for clause in consumption["allOf"]
        }
        self.assertEqual(
            profile_nodes,
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
        for clause in consumption["allOf"]:
            profile = clause["if"]["properties"]["profile"]["const"]
            expected_nodes = profile_nodes[profile]
            hashes = clause["then"]["properties"]["consumed_node_sha256"]
            if expected_nodes:
                self.assertEqual(hashes["required"], expected_nodes)
                self.assertEqual(set(hashes["properties"]), set(expected_nodes))
                self.assertFalse(hashes["additionalProperties"])
            else:
                self.assertEqual(hashes, {"maxProperties": 0})
        self.assertEqual(evidence_manifest["properties"]["schema_version"]["const"], 2)
        required_p0 = tuple(
            summary["properties"]["gates"]["properties"]["p0"]["required"]
        )
        self.assertEqual(required_p0, P0_GATE_IDS)

    def test_dependency_lock_pins_sources_and_has_valid_transition_state(self) -> None:
        lock = yaml.safe_load(
            (ROOT_DIR / "network/config/dependency_lock.yaml").read_text()
        )
        self.assertEqual(
            lock["accepted_p0_path"]["radio_provider"], "tcp_jsonl_real_sionna"
        )
        self.assertEqual(lock["dependencies"]["ns3"]["version"], "3.40")
        self.assertIn("tap-bridge", lock["dependencies"]["ns3"]["required_modules"])
        self.assertRegex(
            lock["dependencies"]["ardupilot"]["revision"], r"^[0-9a-f]{40}$"
        )
        self.assertRegex(
            lock["dependencies"]["ns3"]["archive_sha256"], r"^[0-9a-f]{64}$"
        )
        self.assertIn("sha256:", lock["dependencies"]["ros"]["container_base"])
        self.assertEqual(
            lock["dependencies"]["canonical_runtime_source_paths"],
            CANONICAL_RUNTIME_SOURCE_PATHS,
        )
        self.assertEqual(
            set(lock["runtime_manifest_sha256"]),
            {"pip_freeze", "dpkg", "ros_packages"},
        )
        self.assertEqual(lock["dependencies"]["python_packages"]["numpy"], "1.26.4")
        self.assertNotIn("sionna", lock["dependencies"]["python_packages"])
        self.assertNotIn("pybind11", lock["dependencies"]["python_packages"])
        self.assertNotIn("cppyy", lock["dependencies"]["python_packages"])
        self.assertEqual(lock["dependencies"]["gazebo"]["version"], "8.14.0")
        self.assertEqual(lock["runtime_policy"]["system"], "Linux")
        self.assertEqual(lock["runtime_policy"]["machine"], "x86_64")
        self.assertEqual(
            lock["runtime_policy"]["mitsuba_variant"], "cuda_ad_mono_polarized"
        )
        self.assertIs(lock["runtime_policy"]["gpu_required"], True)
        self.assertEqual(
            lock["runtime_policy"]["required_network_commands"],
            {
                "bridge": "iproute2",
                "ip": "iproute2",
                "ip6tables-save": "iptables",
                "iptables-save": "iptables",
                "nft": "nftables",
                "ss": "iproute2",
            },
        )
        build_inputs = lock["container_build_inputs"]
        self.assertEqual(build_inputs["platform"], "linux/amd64")
        self.assertEqual(build_inputs["user_uid"], 1000)
        self.assertEqual(build_inputs["user_gid"], 1000)
        for relative, field in (
            (
                ".devcontainer/ardupilot_ros2_exact.repos",
                "ardupilot_ros_manifest_sha256",
            ),
            (".devcontainer/ardupilot_gz_exact.repos", "ardupilot_gz_manifest_sha256"),
            (".devcontainer/00-gazebo.list", "osrf_rosdep_list_sha256"),
            (
                ".devcontainer/mpl_toolkits/__init__.py",
                "mpl_toolkits_namespace_shim_sha256",
            ),
        ):
            self.assertEqual(
                hashlib.sha256((ROOT_DIR / relative).read_bytes()).hexdigest(),
                build_inputs[field],
            )
        dockerfile_text = (ROOT_DIR / ".devcontainer/Dockerfile").read_text(
            encoding="utf-8"
        )
        for field in (
            "gazebo_gpg_sha256",
            "gradle_wrapper_jar_sha256",
            "gradle_7_6_distribution_sha256",
        ):
            self.assertIn(build_inputs[field], dockerfile_text)
        self.assertIn(build_inputs["removed_base_file"], dockerfile_text)
        self.assertIn(
            "install -m 0644 /workspace/mpl_toolkits/__init__.py", dockerfile_text
        )
        build_script = (ROOT_DIR / "scripts/build_container.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("--platform linux/amd64", build_script)
        self.assertIn("--build-arg USER_UID=1000", build_script)
        self.assertIn("--build-arg USER_GID=1000", build_script)
        attestation = lock["evidence_attestation"]
        self.assertTrue(attestation["required"])
        self.assertTrue(Path(attestation["ledger_directory"]).is_absolute())
        if attestation["status"] == "provision_required":
            self.assertEqual(attestation["key_id"], "PROVISION_REQUIRED")
            self.assertEqual(attestation["public_key_sha256"], "PROVISION_REQUIRED")
        elif attestation["status"] == "complete":
            self.assertRegex(
                attestation["key_id"], r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
            )
            self.assertRegex(attestation["public_key_sha256"], r"^sha256:[0-9a-f]{64}$")
            self.assertTrue((ROOT_DIR / attestation["public_key_path"]).is_file())
        else:
            self.fail(
                f"unsupported evidence-attestation status: {attestation['status']!r}"
            )
        if lock["status"] == "rebuild_required_before_acceptance":
            self.assertTrue(
                all(
                    value == "REBUILD_REQUIRED"
                    for value in lock["runtime_manifest_sha256"].values()
                )
            )
        elif lock["status"] == "complete":
            for value in lock["runtime_manifest_sha256"].values():
                self.assertRegex(value, r"^[0-9a-f]{64}$")
            self.assertRegex(
                lock["dependencies"]["ros"]["project_image_digest"],
                r"^sha256:[0-9a-f]{64}$",
            )
        else:
            self.fail(f"unsupported dependency lock status: {lock['status']!r}")

    def test_python_closure_is_hash_locked_and_matches_dependency_lock(self) -> None:
        lock = yaml.safe_load(
            (ROOT_DIR / "network/config/dependency_lock.yaml").read_text()
        )
        policy = lock["python_lock"]
        input_path = ROOT_DIR / policy["input"]
        closure_path = ROOT_DIR / policy["lock"]
        self.assertEqual(
            hashlib.sha256(input_path.read_bytes()).hexdigest(), policy["input_sha256"]
        )
        self.assertEqual(
            hashlib.sha256(closure_path.read_bytes()).hexdigest(), policy["lock_sha256"]
        )
        closure = closure_path.read_text(encoding="utf-8")
        self.assertNotRegex(closure.lower(), r"(?m)^tensorflow[^=]*==")
        self.assertNotRegex(closure.lower(), r"(?m)^sionna==")
        requirement_starts = list(re.finditer(r"(?m)^[a-z0-9][a-z0-9_.-]*==", closure))
        self.assertGreater(len(requirement_starts), 40)
        for index, match in enumerate(requirement_starts):
            end = (
                requirement_starts[index + 1].start()
                if index + 1 < len(requirement_starts)
                else len(closure)
            )
            self.assertIn("--hash=sha256:", closure[match.start() : end])
        dockerfile = (ROOT_DIR / ".devcontainer/Dockerfile").read_text(encoding="utf-8")
        self.assertIn(
            "--require-hashes -r /workspace/requirements-radio.lock", dockerfile
        )
        self.assertIn("python3 -m pip check", dockerfile)


if __name__ == "__main__":
    unittest.main()
