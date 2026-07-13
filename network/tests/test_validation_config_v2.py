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


class ValidationConfigV2Tests(unittest.TestCase):
    def test_mutable_status_documents_use_allowed_states_and_agree(self) -> None:
        allowed = {"not_started", "in_progress", "failed", "blocked_external", "passed"}
        progress = (ROOT_DIR / "network/PROGRESS.md").read_text(encoding="utf-8")
        report = (ROOT_DIR / "network/VALIDATION_REPORT.md").read_text(encoding="utf-8")
        next_task = (ROOT_DIR / "network/NEXT_TASK.md").read_text(encoding="utf-8")

        row_pattern = re.compile(r"(?m)^\| (M\d(?:–M\d)?) \| `([a-z_]+)` \|")

        def expanded_rows(document: str) -> dict[str, str]:
            result: dict[str, str] = {}
            for label, status in row_pattern.findall(document):
                self.assertIn(status, allowed, f"unsupported milestone status: {status}")
                if "–" in label:
                    first, last = (int(part.removeprefix("M")) for part in label.split("–"))
                    labels = (f"M{index}" for index in range(first, last + 1))
                else:
                    labels = (label,)
                for milestone in labels:
                    self.assertNotIn(milestone, result, f"duplicate status row: {milestone}")
                    result[milestone] = status
            return result

        expected_labels = {f"M{index}" for index in range(9)}
        progress_states = expanded_rows(progress)
        report_states = expanded_rows(report)
        self.assertEqual(set(progress_states), expected_labels)
        self.assertEqual(set(report_states), expected_labels)
        self.assertEqual(progress_states, report_states)

        ordered = [progress_states[f"M{index}"] for index in range(9)]
        first_open = next((index for index, status in enumerate(ordered) if status != "passed"), 9)
        self.assertTrue(all(status == "passed" for status in ordered[:first_open]))
        self.assertTrue(all(status == "not_started" for status in ordered[first_open + 1 :]))
        if first_open < 9:
            self.assertIn(
                ordered[first_open], {"not_started", "in_progress", "failed", "blocked_external"}
            )

        count_pattern = re.compile(
            r"Fully closed(?:\s+sequential)?\s+milestones:\s+\*\*(\d+)\*\*",
            re.IGNORECASE,
        )
        ready_pattern = re.compile(r"Customer-ready:\s+\*\*(true|false)\*\*", re.IGNORECASE)
        passed_count = sum(status == "passed" for status in ordered)
        for name, document in (
            ("PROGRESS", progress),
            ("VALIDATION_REPORT", report),
            ("NEXT_TASK", next_task),
        ):
            count = count_pattern.search(document)
            ready = ready_pattern.search(document)
            self.assertIsNotNone(count, f"{name} lacks a closed-milestone count")
            self.assertIsNotNone(ready, f"{name} lacks customer-ready state")
            self.assertEqual(int(count.group(1)), passed_count, f"{name} count disagrees")
            self.assertEqual(ready.group(1).lower() == "true", passed_count == 9)

        if first_open < 9:
            active = f"M{first_open}"
            self.assertRegex(progress, rf"Active milestone:\s+\*\*{active}\b")
            self.assertRegex(next_task, rf"Active milestone:\s*\n?\*\*{active}\b")

    def test_matrix_and_validator_have_exact_same_p0_gate_ids(self) -> None:
        matrix = yaml.safe_load((ROOT_DIR / "network/config/validation_matrix.yaml").read_text())
        matrix_ids = tuple(item["id"] for item in matrix["gates"]["p0"])
        self.assertEqual(matrix_ids, P0_GATE_IDS)

    def test_matrix_points_to_authoritative_v3_plan(self) -> None:
        matrix = yaml.safe_load((ROOT_DIR / "network/config/validation_matrix.yaml").read_text())
        self.assertEqual(matrix["schema_version"], 2)
        self.assertEqual(matrix["plan"], "doc/network_radio_integration_plan_v3.md")
        self.assertEqual(matrix["validation_engine"], "network/validation/validate_run.py")
        profiles = matrix["acceptance_profiles"]
        self.assertEqual(profiles["schema"], "v3_profiled")
        self.assertFalse(profiles["customer_ready_enabled"])
        self.assertEqual(profiles["required_customer_profile"], "m8_customer_handoff")

    def test_every_declared_p0_raw_artifact_is_in_the_sealed_set(self) -> None:
        matrix = yaml.safe_load((ROOT_DIR / "network/config/validation_matrix.yaml").read_text())
        sealed = set(matrix["run_outputs"]["raw_runtime_required"])
        declared = {
            relative
            for item in matrix["gates"]["p0"]
            for relative in item.get("raw_evidence", [])
        }
        self.assertFalse(declared - sealed)
        self.assertIn("logs/five_uav_launch.log", sealed)

    def test_json_schemas_are_valid_json_and_v2(self) -> None:
        provenance = json.loads((ROOT_DIR / "network/config/provenance_schema.json").read_text())
        evidence_manifest = json.loads(
            (ROOT_DIR / "network/config/evidence_manifest_schema.json").read_text()
        )
        summary = json.loads((ROOT_DIR / "network/config/metrics_summary_schema.json").read_text())
        self.assertEqual(provenance["properties"]["schema_version"]["const"], 2)
        self.assertEqual(evidence_manifest["properties"]["schema_version"]["const"], 2)
        required_p0 = tuple(summary["properties"]["gates"]["properties"]["p0"]["required"])
        self.assertEqual(required_p0, P0_GATE_IDS)

    def test_dependency_lock_pins_sources_and_has_valid_transition_state(self) -> None:
        lock = yaml.safe_load((ROOT_DIR / "network/config/dependency_lock.yaml").read_text())
        self.assertEqual(lock["accepted_p0_path"]["radio_provider"], "tcp_jsonl_real_sionna")
        self.assertEqual(lock["dependencies"]["ns3"]["version"], "3.40")
        self.assertIn("tap-bridge", lock["dependencies"]["ns3"]["required_modules"])
        self.assertRegex(lock["dependencies"]["ardupilot"]["revision"], r"^[0-9a-f]{40}$")
        self.assertRegex(lock["dependencies"]["ns3"]["archive_sha256"], r"^[0-9a-f]{64}$")
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
            lock["runtime_policy"]["mitsuba_variant"], "llvm_ad_mono_polarized"
        )
        build_inputs = lock["container_build_inputs"]
        self.assertEqual(build_inputs["platform"], "linux/amd64")
        self.assertEqual(build_inputs["user_uid"], 1000)
        self.assertEqual(build_inputs["user_gid"], 1000)
        for relative, field in (
            (".devcontainer/ardupilot_ros2_exact.repos", "ardupilot_ros_manifest_sha256"),
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
            'install -m 0644 /workspace/mpl_toolkits/__init__.py', dockerfile_text
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
            self.assertRegex(attestation["key_id"], r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
            self.assertRegex(attestation["public_key_sha256"], r"^sha256:[0-9a-f]{64}$")
            self.assertTrue((ROOT_DIR / attestation["public_key_path"]).is_file())
        else:
            self.fail(f"unsupported evidence-attestation status: {attestation['status']!r}")
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
        lock = yaml.safe_load((ROOT_DIR / "network/config/dependency_lock.yaml").read_text())
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
        self.assertIn("--require-hashes -r /workspace/requirements-radio.lock", dockerfile)
        self.assertIn("python3 -m pip check", dockerfile)


if __name__ == "__main__":
    unittest.main()
