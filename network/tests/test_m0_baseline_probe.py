#!/usr/bin/env python3
"""Focused tests for the dependency/provenance-only M0 probe."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

import yaml


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from network.scripts import validate_m0_baseline as validator  # noqa: E402
from network.scripts import run_m0_validation_suite as suite_runner  # noqa: E402
from network.validation.qualification_identity import (  # noqa: E402
    qualification_checkout_identity,
    qualification_consumption,
)


class M0BaselineProbeTests(unittest.TestCase):
    TEST_IDS = sorted(
        {
            test_id
            for test_ids in validator.REQUIRED_M0_COVERAGE.values()
            for test_id in test_ids
        }
    )
    PYTHON_BYTES = 12345
    PYTHON_SHA256 = "d" * 64

    def setUp(self) -> None:
        inherited = os.environ.pop("AMS_M0_ARTIFACT_ROOT", None)

        def restore() -> None:
            os.environ.pop("AMS_M0_ARTIFACT_ROOT", None)
            if inherited is not None:
                os.environ["AMS_M0_ARTIFACT_ROOT"] = inherited

        self.addCleanup(restore)

    def test_capability_probe_success_marker_preserves_newline(self) -> None:
        printf_command = validator.M0_CAPABILITY_COMMAND_SCRIPT.rsplit("; ", 1)[1]
        completed = subprocess.run(
            ["/bin/bash", "-c", printf_command],
            check=False,
            capture_output=True,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, validator.M0_CAPABILITY_STDOUT)
        self.assertEqual(completed.stderr, b"")
        from network.scripts import validate_status_documents as status_validator

        self.assertEqual(
            status_validator.M0_CAPABILITY_COMMAND_SCRIPT,
            validator.M0_CAPABILITY_COMMAND_SCRIPT,
        )
        self.assertEqual(
            status_validator.M0_CAPABILITY_STDOUT,
            validator.M0_CAPABILITY_STDOUT,
        )

    def dependency_log(self) -> str:
        lines = ["Network/radio dependency check", "Repository: /workspace/project", ""]
        for label in validator.EXPECTED_DEPENDENCY_RECORDS:
            if label == "lock.read":
                lines.extend(
                    [
                        "Python runtime compatibility check",
                        "Dependency lock: /workspace/project/network/config/dependency_lock.yaml",
                    ]
                )
            status = "WARN" if label == "cuda:gpu" else "PASS"
            lines.append(f"{status} {label:<36} independently observed")
            if label == "runtime.no_tensorflow":
                lines.append("Python runtime compatibility passed.")
        lines.extend(["", "Dependency check passed with 1 warning(s)."])
        return "\n".join(lines) + "\n"

    def suite_log(self, test_ids: list[str] | None = None) -> str:
        ids = test_ids or self.TEST_IDS
        raw_log = "".join(
            f"{test_id.rsplit('.', 1)[1]} ({test_id.rsplit('.', 1)[0]}) ... ok\n"
            for test_id in ids
        )
        return raw_log + (
            "\n----------------------------------------------------------------------\n"
            f"Ran {len(ids)} tests in 0.001s\n\nOK\n"
        )

    def make_run(self, root: Path, run_id: str = "m0_fixture") -> Path:
        policy = {
            "schema_version": 2,
            "contract": "q0_q1_q2_granular/v1",
            "policy_id": "q0_q1_q2_granular/v1",
            "mutable_status_exclusions": sorted(suite_runner.MUTABLE_STATUS_OUTPUTS),
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
            "explicit_owners": {f"Q{index}": [] for index in range(9)},
        }
        manifest = {
            "schema_version": 1,
            "contract": "ams.qualification-test-manifest/v1",
            "node": "Q0",
            "discovery": {
                "start_directory": "network/tests",
                "pattern": "test_*.py",
            },
            "test_modules": sorted(
                {test_id.split(".", 1)[0] for test_id in self.TEST_IDS}
            ),
            "ordered_test_ids": self.TEST_IDS,
        }
        manifest_raw = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
        manifest_sha256 = hashlib.sha256(manifest_raw).hexdigest()
        import_policy = {
            "schema_version": 1,
            "mode": "isolated_explicit_path",
            "interpreter": "/usr/bin/python3.10",
            "interpreter_sha256": self.PYTHON_SHA256,
            "parent_flags": ["-S"],
            "exact_base_pythonpath": [
                "/workspace/multiagent_simulation/network/scripts/m0_python_guard",
                "/workspace/multiagent_simulation",
            ],
            "overlay_pythonpath_template": (
                "/tmp/ams-m0-overlay-{run_id}/install/"
                "multiagent_simulation/lib/python3.10/site-packages"
            ),
            "interpreter_suffix": [
                "/usr/lib/python310.zip",
                "/usr/lib/python3.10",
                "/usr/lib/python3.10/lib-dynload",
            ],
            "customization": {
                "parent_sitecustomize_loaded": False,
                "parent_usercustomize_loaded": False,
                "child_guard_path": (
                    "/workspace/multiagent_simulation/network/scripts/"
                    "m0_python_guard/sitecustomize.py"
                ),
                "child_guard_sha256": hashlib.sha256(
                    b"AMS_M0_INERT_SITECUSTOMIZE = True\n"
                ).hexdigest(),
            },
            "pth_policy": "inventory_only_not_processed_under_no_site",
            "cleared_environment": ["PYTHONHOME", "PYTEST_PLUGINS"],
            "python_no_user_site": True,
            "bytecode_root_template": "/tmp/ams-m0-pycache-{run_id}",
        }
        lock_document = {
            "m0_test_manifest": {
                "path": "network/config/m0_test_manifest.json",
                "sha256": manifest_sha256,
                "ordered_test_count": len(self.TEST_IDS),
            },
            "m0_python_import_policy": import_policy,
        }
        source_contents = {
            "network/scripts/run_m0_baseline.sh": "#!/usr/bin/env bash\n",
            "network/scripts/run_m0_validation_suite.py": "# suite runner\n",
            "network/scripts/validate_m0_baseline.py": "# validator\n",
            "network/scripts/m0_python_guard/sitecustomize.py": (
                "AMS_M0_INERT_SITECUSTOMIZE = True\n"
            ),
            "doc/network_radio_integration_plan_v3.md": "# authoritative v3 contract\n",
            "network/config/qualification_path_ownership.json": (
                json.dumps(policy, indent=2, sort_keys=True) + "\n"
            ),
            "network/config/m0_test_manifest.json": manifest_raw.decode(),
            "network/config/dependency_lock.yaml": yaml.safe_dump(
                lock_document, sort_keys=False
            ),
        }
        for test_id in self.TEST_IDS:
            module = test_id.split(".", 1)[0]
            source_contents.setdefault(
                f"network/tests/{module}.py", f"# bound source for {module}\n"
            )
        for relative, content in source_contents.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        for relative in suite_runner.MUTABLE_STATUS_OUTPUTS:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"fixture status: {relative}\n", encoding="utf-8")
        subprocess.run(["/usr/bin/git", "init", "--quiet", str(root)], check=True)
        subprocess.run(
            ["/usr/bin/git", "-C", str(root), "config", "user.name", "M0 Fixture"],
            check=True,
        )
        subprocess.run(
            [
                "/usr/bin/git", "-C", str(root), "config", "user.email",
                "m0-fixture@example.invalid",
            ],
            check=True,
        )
        subprocess.run(
            ["/usr/bin/git", "-C", str(root), "add", "--all"], check=True
        )
        subprocess.run(
            [
                "/usr/bin/git", "-C", str(root), "commit", "--quiet", "-m",
                "M0 fixture technical base",
            ],
            check=True,
        )
        source_commit = subprocess.run(
            ["/usr/bin/git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        source_bindings = suite_runner.suite_source_bindings(root)
        content_vector = suite_runner.qualification_content_vector(root, source_commit)
        checkout_identity = qualification_checkout_identity(root, source_commit)
        plan_contract = {
            "plan_version": 3,
            "path": "doc/network_radio_integration_plan_v3.md",
            "contract_sha256": source_bindings[
                "doc/network_radio_integration_plan_v3.md"
            ],
        }
        run_dir = root / "runs" / run_id
        (run_dir / "logs").mkdir(parents=True)
        (run_dir / "metrics").mkdir()
        (run_dir / "logs/check_deps.log").write_text(
            self.dependency_log(),
            encoding="utf-8",
        )
        (run_dir / "logs/check_deps.log.exit_code").write_text("0\n", encoding="utf-8")
        (run_dir / "logs/provenance.log").write_text(
            "Provenance generated\nAcceptance eligible: true\n", encoding="utf-8"
        )
        (run_dir / "logs/provenance.log.exit_code").write_text("0\n", encoding="utf-8")
        (run_dir / "logs/m0_runtime_lock_producer.log").write_text("", encoding="utf-8")
        (run_dir / "logs/m0_runtime_lock_producer.log.exit_code").write_text("0\n", encoding="ascii")
        lock_sha = source_bindings["network/config/dependency_lock.yaml"]
        runtime_checks = {
            name: {"status": "passed"}
            for name in (
                "lock", "image_digest", "runtime_manifests",
                "runtime_identity_files", "m0_execution_policy",
                "external_sources", "ns3_tree",
            )
        }
        (run_dir / "metrics/m0_runtime_lock.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "contract": "ams.m0.runtime-lock-verification/v1",
                    "passed": True,
                    "observed_image_digest": "sha256:" + "a" * 64,
                    "lock_sha256": lock_sha,
                    "checks": runtime_checks,
                    "failures": [],
                }
            ) + "\n",
            encoding="utf-8",
        )
        container_identity = {
            "reference": "multiagent_simulation:latest",
            "digest": "sha256:" + "a" * 64,
            "digest_source": "docker_image_inspect_host",
            "runtime_container_id": "b" * 64,
            "runtime_container_id_source": "host_bind_mount",
        }
        provenance_sources = {
            relative: digest
            for relative, digest in source_bindings.items()
            if not relative.startswith("doc/")
        }
        provenance_configs = {
            relative: digest
            for relative, digest in source_bindings.items()
            if relative.startswith("doc/")
        }
        (run_dir / "metrics/provenance.json").write_text(
            json.dumps(
                {
                    "git_commit": source_commit,
                    "container_image": container_identity,
                    "source_manifest": provenance_sources,
                    "config_hashes": provenance_configs,
                    "qualification_content_vector": content_vector,
                    "qualification_consumption": qualification_consumption(
                        content_vector, "m0"
                    ),
                    "qualification_checkout": checkout_identity,
                    "plan_contract": plan_contract,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        raw_log = self.suite_log()
        raw_bytes = raw_log.encode("utf-8")
        (run_dir / "logs/m0_validation_suite.log").write_bytes(raw_bytes)
        (run_dir / "logs/m0_validation_suite_producer.log").write_text(
            f"M0 validation/adversarial suite recorded {len(self.TEST_IDS)} tests; passed=true\n",
            encoding="utf-8",
        )
        (run_dir / "logs/m0_validation_suite_producer.log.exit_code").write_text(
            "0\n", encoding="utf-8"
        )
        outcomes = [
            {"test_id": test_id, "outcome": "passed"} for test_id in self.TEST_IDS
        ]
        expected_python_path = suite_runner.expected_m0_sys_path(import_policy, run_id)
        import_modules = [
            {
                "name": "network.scripts.run_m0_validation_suite",
                "kind": "file",
                "origin": (
                    "/workspace/multiagent_simulation/network/scripts/"
                    "run_m0_validation_suite.py"
                ),
                "resolved_path": (
                    "/workspace/multiagent_simulation/network/scripts/"
                    "run_m0_validation_suite.py"
                ),
                "allowed_root": "/workspace/multiagent_simulation",
                "source_kind": "committed_source",
                "bytes": len(source_contents["network/scripts/run_m0_validation_suite.py"]),
                "sha256": source_bindings[
                    "network/scripts/run_m0_validation_suite.py"
                ],
                "distributions": [],
            },
            {
                "name": "urllib",
                "kind": "file",
                "origin": "/usr/lib/python3.10/urllib/__init__.py",
                "resolved_path": "/usr/lib/python3.10/urllib/__init__.py",
                "allowed_root": "/usr/lib/python3.10",
                "source_kind": "immutable_image",
                "bytes": 0,
                "sha256": hashlib.sha256(b"").hexdigest(),
                "distributions": [],
            },
        ]
        import_trace = {
            "schema_version": 1,
            "contract": "ams.m0.python-import-trace/v1",
            "policy_path": (
                "network/config/dependency_lock.yaml#m0_python_import_policy"
            ),
            "policy_sha256": hashlib.sha256(
                json.dumps(
                    import_policy, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest(),
            "mode": "isolated_explicit_path",
            "sys_path_before": expected_python_path,
            "sys_path_after": expected_python_path,
            "pth_inventory_before": [],
            "pth_inventory_after": [],
            "customization": dict(import_policy["customization"]),
            "environment": {
                "cleared": sorted(import_policy["cleared_environment"]),
                "python_no_user_site": True,
                "bytecode_root": f"/tmp/ams-m0-pycache-{run_id}",
                "pytest_plugin_autoload": False,
            },
            "modules": import_modules,
            "module_count": len(import_modules),
            "modules_sha256": hashlib.sha256(
                json.dumps(
                    import_modules, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest(),
        }
        (run_dir / "metrics/m0_validation_suite.json").write_text(
            json.dumps(
                {
                    "schema_version": 5,
                    "suite": "complete_network_validation_adversarial_unittest",
                    "started_utc": "2026-07-14T10:00:00Z",
                    "completed_utc": "2026-07-14T10:00:01Z",
                    "execution_identity": {
                        "container_image_digest": container_identity["digest"],
                        "container_image_digest_source": container_identity[
                            "digest_source"
                        ],
                        "runtime_container_id": container_identity[
                            "runtime_container_id"
                        ],
                        "runtime_container_id_source": container_identity[
                            "runtime_container_id_source"
                        ],
                        "source_mode": "clean_git_clone_ro",
                        "source_commit": source_commit,
                        "source_mount_read_only": True,
                        "project_overlay_mode": "none_q0_source_only",
                        "python_no_site": True,
                        "python_pycache_prefix": f"/tmp/ams-m0-pycache-{run_id}",
                        "python_sys_path": expected_python_path,
                        "sitecustomize_loaded": False,
                        "usercustomize_loaded": False,
                        "child_python_guard": {
                            "guard_marker": True,
                            "no_site": 0,
                            "sitecustomize_path": "/workspace/multiagent_simulation/network/scripts/m0_python_guard/sitecustomize.py",
                            "usercustomize_loaded": False,
                            "sitecustomize_sha256": source_bindings[
                                "network/scripts/m0_python_guard/sitecustomize.py"
                            ],
                        },
                    },
                    "invocation": {
                        "producer_command": [
                            "/usr/bin/python3.10",
                            "-S",
                            "network/scripts/run_m0_validation_suite.py",
                            "--run-dir",
                            f"/run/ams/m0-artifacts/{run_id}",
                        ],
                        "working_directory": "repository_root",
                        "unittest_loader_call": {
                            "api": "qualification_suite.discover_owned_test_suite",
                            "node": "Q0",
                            "manifest_path": "network/config/m0_test_manifest.json",
                            "start_directory": "network/tests",
                            "pattern": "test_*.py",
                            "verbosity": 2,
                            "buffer": True,
                            "failfast": False,
                        },
                    },
                    "python_executable": {
                        "resolved_path": "/usr/bin/python3.10",
                        "bytes": self.PYTHON_BYTES,
                        "sha256": self.PYTHON_SHA256,
                    },
                    "python_import_trace": import_trace,
                    "source_bindings": source_bindings,
                    "source_bindings_after": source_bindings,
                    "qualification_content_vector": content_vector,
                    "plan_contract": plan_contract,
                    "external_input_bindings": {},
                    "external_input_bindings_after": {},
                    "frozen_test_manifest": {
                        "path": "network/config/m0_test_manifest.json",
                        "sha256": manifest_sha256,
                    },
                    "discovery": {
                        "start_directory": "network/tests",
                        "pattern": "test_*.py",
                        "test_count": len(self.TEST_IDS),
                        "test_ids": self.TEST_IDS,
                    },
                    "execution": {
                        "started_monotonic_ns": 1000000000,
                        "completed_monotonic_ns": 2000000000,
                        "started_test_ids": self.TEST_IDS,
                        "tests_run": len(self.TEST_IDS),
                        "outcome_counts": {
                            "passed": len(self.TEST_IDS),
                            "failed": 0,
                            "error": 0,
                            "skipped": 0,
                            "expected_failure": 0,
                            "unexpected_success": 0,
                            "not_completed": 0,
                        },
                        "outcomes": outcomes,
                    },
                    "raw_log": {
                        "path": "logs/m0_validation_suite.log",
                        "bytes": len(raw_bytes),
                        "sha256": hashlib.sha256(raw_bytes).hexdigest(),
                    },
                    "producer_observation": {"passed": True},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return run_dir

    def evaluate(
        self,
        root: Path,
        run_dir: Path,
        provenance: dict[str, object] | None = None,
    ) -> dict[str, object]:
        independent = provenance or {
            "status": "passed",
            "proof": "independently accepted",
        }
        with mock.patch.object(validator, "ROOT_DIR", root), mock.patch.object(
            validator, "provenance_status", return_value=independent
        ), mock.patch.object(
            validator,
            "_discover_validation_test_ids",
            return_value=self.TEST_IDS,
        ), mock.patch.object(
            validator,
            "_runtime_executable_identity",
            return_value=(self.PYTHON_BYTES, self.PYTHON_SHA256, None),
        ):
            return validator.evaluate_m0_baseline(run_dir)

    def test_pass_requires_all_independent_gates(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = self.make_run(root)
            result = self.evaluate(root, run_dir)

        self.assertFalse(result["passed"])
        self.assertTrue(result["captured_qualified"])
        self.assertFalse(result["formal_accepted"])
        self.assertFalse(result["p0_eligible"])
        self.assertEqual(
            set(result["gates"]),
            {
                "dependency_check", "runtime_lock", "validation_adversarial_suite",
                "provenance",
            },
        )
        self.assertTrue(all(gate["status"] == "passed" for gate in result["gates"].values()))
        self.assertFalse(result["scope"]["packet_path"])
        self.assertFalse(result["scope"]["sealing"])
        self.assertFalse(result["scope"]["attestation"])

    def test_nonzero_dependency_exit_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = self.make_run(root)
            (run_dir / "logs/check_deps.log.exit_code").write_text("3\n", encoding="utf-8")
            result = self.evaluate(root, run_dir)

        self.assertFalse(result["passed"])
        self.assertEqual(result["gates"]["dependency_check"]["status"], "failed")
        self.assertIn(
            "check_deps exited with 3",
            result["gates"]["dependency_check"]["details"]["failures"],
        )

    def test_generator_zero_does_not_override_failed_provenance_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = self.make_run(root)
            independent = {
                "status": "failed",
                "proof": "provenance contains acceptance blockers",
            }
            result = self.evaluate(root, run_dir, independent)

        self.assertFalse(result["passed"])
        provenance = result["gates"]["provenance"]
        self.assertEqual(provenance["status"], "failed")
        self.assertIn("provenance_status did not pass", provenance["details"]["failures"])
        self.assertEqual(provenance["details"]["provenance_status"], independent)

    def test_symlinked_dependency_record_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = self.make_run(root)
            exit_path = run_dir / "logs/check_deps.log.exit_code"
            target = run_dir / "logs/forged.exit_code"
            target.write_text("0\n", encoding="utf-8")
            exit_path.unlink()
            exit_path.symlink_to(target.name)
            result = self.evaluate(root, run_dir)

        failures = result["gates"]["dependency_check"]["details"]["failures"]
        self.assertFalse(result["passed"])
        self.assertTrue(any("symbolic-link component" in failure for failure in failures))

    def test_dependency_success_footer_cannot_hide_a_missing_raw_check(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = self.make_run(root)
            log_path = run_dir / "logs/check_deps.log"
            lines = log_path.read_text(encoding="utf-8").splitlines()
            lines = [line for line in lines if "component:ns3_core" not in line]
            log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            result = self.evaluate(root, run_dir)

        dependency = result["gates"]["dependency_check"]
        self.assertFalse(result["passed"])
        self.assertEqual(dependency["status"], "failed")
        self.assertIn(
            "dependency log does not contain the complete ordered check record",
            dependency["details"]["failures"],
        )

    def test_truncated_dependency_output_fails_despite_zero_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = self.make_run(root)
            log_path = run_dir / "logs/check_deps.log"
            text = log_path.read_text(encoding="utf-8")
            log_path.write_text(text[: len(text) // 2], encoding="utf-8")
            result = self.evaluate(root, run_dir)

        dependency = result["gates"]["dependency_check"]
        self.assertFalse(result["passed"])
        self.assertEqual(dependency["status"], "failed")
        self.assertLess(
            dependency["details"]["observed_record_count"],
            len(validator.EXPECTED_DEPENDENCY_RECORDS),
        )

    def test_raw_suite_mutation_fails_even_with_producer_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = self.make_run(root)
            with (run_dir / "logs/m0_validation_suite.log").open(
                "a", encoding="utf-8"
            ) as output:
                output.write("forged trailing success\n")
            result = self.evaluate(root, run_dir)

        suite = result["gates"]["validation_adversarial_suite"]
        self.assertFalse(result["passed"])
        self.assertEqual(suite["status"], "failed")
        self.assertTrue(
            any("raw unittest" in failure or "raw-log" in failure for failure in suite["details"]["failures"])
        )

    def test_missing_discovered_test_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = self.make_run(root)
            result_path = run_dir / "metrics/m0_validation_suite.json"
            document = json.loads(result_path.read_text(encoding="utf-8"))
            document["discovery"]["test_ids"] = self.TEST_IDS[:-1]
            document["discovery"]["test_count"] = len(self.TEST_IDS) - 1
            result_path.write_text(json.dumps(document) + "\n", encoding="utf-8")
            result = self.evaluate(root, run_dir)

        failures = result["gates"]["validation_adversarial_suite"]["details"][
            "failures"
        ]
        self.assertFalse(result["passed"])
        self.assertIn(
            "recorded discovered test IDs differ from current source", failures
        )

    def test_nonpassing_test_cannot_be_hidden_by_producer_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = self.make_run(root)
            result_path = run_dir / "metrics/m0_validation_suite.json"
            document = json.loads(result_path.read_text(encoding="utf-8"))
            document["execution"]["outcomes"][0]["outcome"] = "failed"
            document["execution"]["outcome_counts"]["passed"] -= 1
            document["execution"]["outcome_counts"]["failed"] = 1
            self.assertTrue(document["producer_observation"]["passed"])
            result_path.write_text(json.dumps(document) + "\n", encoding="utf-8")
            result = self.evaluate(root, run_dir)

        failures = result["gates"]["validation_adversarial_suite"]["details"][
            "failures"
        ]
        self.assertFalse(result["passed"])
        self.assertIn(
            "not every discovered test has one passing outcome", failures
        )

    def test_nonzero_suite_exit_fails_even_when_records_claim_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = self.make_run(root)
            (run_dir / "logs/m0_validation_suite_producer.log.exit_code").write_text(
                "7\n", encoding="utf-8"
            )
            result = self.evaluate(root, run_dir)

        failures = result["gates"]["validation_adversarial_suite"]["details"][
            "failures"
        ]
        self.assertFalse(result["passed"])
        self.assertIn("validation-suite producer exited with 7", failures)

    def test_suite_container_substitution_fails_provenance_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = self.make_run(root)
            result_path = run_dir / "metrics/m0_validation_suite.json"
            document = json.loads(result_path.read_text(encoding="utf-8"))
            document["execution_identity"]["runtime_container_id"] = "c" * 64
            result_path.write_text(json.dumps(document) + "\n", encoding="utf-8")
            result = self.evaluate(root, run_dir)

        failures = result["gates"]["validation_adversarial_suite"]["details"][
            "failures"
        ]
        self.assertFalse(result["passed"])
        self.assertIn(
            "validation-suite did not run in the provenance-qualified container",
            failures,
        )

    def test_suite_invocation_and_monotonic_interval_are_not_trusted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = self.make_run(root)
            result_path = run_dir / "metrics/m0_validation_suite.json"
            document = json.loads(result_path.read_text(encoding="utf-8"))
            document["invocation"]["producer_command"][-1] = "runs/other-run"
            document["execution"]["completed_monotonic_ns"] = document["execution"][
                "started_monotonic_ns"
            ]
            result_path.write_text(json.dumps(document) + "\n", encoding="utf-8")
            result = self.evaluate(root, run_dir)

        failures = result["gates"]["validation_adversarial_suite"]["details"][
            "failures"
        ]
        self.assertFalse(result["passed"])
        self.assertIn("validation-suite producer command is not canonical", failures)
        self.assertIn("validation-suite monotonic interval is invalid", failures)

    def test_python_executable_hash_is_independently_checked(self) -> None:
        executable = Path(sys.executable).resolve(strict=True)
        expected_payload = executable.read_bytes()
        with tempfile.TemporaryDirectory() as identity_temp:
            identity_file = Path(identity_temp) / "container_id"
            identity_file.write_text("b" * 64 + "\n", encoding="ascii")
            with mock.patch.dict(
                os.environ,
                {
                    "AMS_RUNTIME_CONTAINER_ID_FILE": str(identity_file),
                    "AMS_CONTAINER_IMAGE_DIGEST": "sha256:" + "a" * 64,
                    "AMS_CONTAINER_IMAGE_DIGEST_SOURCE": "docker_image_inspect_host",
                },
            ):
                size, digest, error = validator._runtime_executable_identity(
                    container_id="b" * 64,
                    image_digest="sha256:" + "a" * 64,
                    executable_path=str(executable),
                )
        self.assertIsNone(error)
        self.assertEqual(size, len(expected_payload))
        self.assertEqual(digest, hashlib.sha256(expected_payload).hexdigest())

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = self.make_run(root)
            result_path = run_dir / "metrics/m0_validation_suite.json"
            document = json.loads(result_path.read_text(encoding="utf-8"))
            document["python_executable"]["sha256"] = "e" * 64
            result_path.write_text(json.dumps(document) + "\n", encoding="utf-8")
            result = self.evaluate(root, run_dir)

        failures = result["gates"]["validation_adversarial_suite"]["details"][
            "failures"
        ]
        self.assertFalse(result["passed"])
        self.assertIn(
            "recorded Python executable differs from the exact qualified image",
            failures,
        )

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = self.make_run(root)
            result_path = run_dir / "metrics/m0_validation_suite.json"
            document = json.loads(result_path.read_text(encoding="utf-8"))
            document["python_executable"]["resolved_path"] = "/usr/bin/python3.11"
            document["invocation"]["producer_command"][0] = "/usr/bin/python3.11"
            result_path.write_text(json.dumps(document) + "\n", encoding="utf-8")
            result = self.evaluate(root, run_dir)

        failures = result["gates"]["validation_adversarial_suite"]["details"][
            "failures"
        ]
        self.assertFalse(result["passed"])
        self.assertIn(
            "validation-suite Python executable differs from locked import policy",
            failures,
        )

    def test_python_import_path_reordering_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = self.make_run(root)
            path = run_dir / "metrics/m0_validation_suite.json"
            document = json.loads(path.read_text(encoding="utf-8"))
            trace = document["python_import_trace"]
            trace["sys_path_after"] = list(reversed(trace["sys_path_after"]))
            path.write_text(json.dumps(document) + "\n", encoding="utf-8")
            result = self.evaluate(root, run_dir)

        failures = result["gates"]["validation_adversarial_suite"]["details"][
            "failures"
        ]
        self.assertFalse(result["passed"])
        self.assertTrue(any("import trace policy/path" in item for item in failures), failures)

    def test_unbound_loaded_source_module_hash_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = self.make_run(root)
            path = run_dir / "metrics/m0_validation_suite.json"
            document = json.loads(path.read_text(encoding="utf-8"))
            trace = document["python_import_trace"]
            trace["modules"][0]["sha256"] = "f" * 64
            trace["modules_sha256"] = hashlib.sha256(
                json.dumps(
                    trace["modules"], sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest()
            path.write_text(json.dumps(document) + "\n", encoding="utf-8")
            result = self.evaluate(root, run_dir)

        failures = result["gates"]["validation_adversarial_suite"]["details"][
            "failures"
        ]
        self.assertFalse(result["passed"])
        self.assertTrue(any("committed Q0 bytes" in item for item in failures), failures)

    def test_suite_source_binding_must_match_provenance_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = self.make_run(root)
            provenance_path = run_dir / "metrics/provenance.json"
            provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
            target = next(
                relative
                for relative in provenance["source_manifest"]
                if relative.startswith("network/tests/test_")
            )
            provenance["source_manifest"][target] = "f" * 64
            provenance_path.write_text(json.dumps(provenance) + "\n", encoding="utf-8")
            result = self.evaluate(root, run_dir)

        failures = result["gates"]["validation_adversarial_suite"]["details"][
            "failures"
        ]
        self.assertFalse(result["passed"])
        self.assertTrue(
            any("provenance source" in failure for failure in failures), failures
        )

    def test_logs_and_metrics_symlink_parents_are_rejected(self) -> None:
        for directory_name in ("logs", "metrics"):
            with self.subTest(directory=directory_name), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                run_dir = self.make_run(root)
                external = root / f"external_{directory_name}"
                shutil.move(str(run_dir / directory_name), external)
                (run_dir / directory_name).symlink_to(external, target_is_directory=True)
                result = self.evaluate(root, run_dir)

            self.assertFalse(result["passed"])
            self.assertTrue(
                all(record["status"] == "failed" for record in result["gates"].values())
            )
            input_failures = result["gates"]["dependency_check"]["details"][
                "failures"
            ]
            self.assertTrue(
                any("non-symlink directory" in failure for failure in input_failures)
            )

    def test_hardlinked_leaf_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = self.make_run(root)
            source = run_dir / "logs/check_deps.log.exit_code"
            alias = run_dir / "logs/alias.exit_code"
            os.link(source, alias)
            result = self.evaluate(root, run_dir)

        failures = result["gates"]["dependency_check"]["details"]["failures"]
        self.assertFalse(result["passed"])
        self.assertTrue(any("exactly one hard link" in failure for failure in failures))

    def test_utc_interval_requires_real_ordered_correlated_timestamps(self) -> None:
        cases = (
            (
                "9999-99-99T99:99:99Z",
                "2026-07-14T10:00:01Z",
                1_000_000_000,
                2_000_000_000,
                "real canonical UTC",
            ),
            (
                "2026-07-14T10:00:02Z",
                "2026-07-14T10:00:01Z",
                1_000_000_000,
                2_000_000_000,
                "UTC interval is reversed",
            ),
            (
                "2026-07-14T10:00:00Z",
                "2026-07-14T10:00:10Z",
                1_000_000_000,
                2_000_000_000,
                "durations are inconsistent",
            ),
        )
        for started, completed, start_ns, end_ns, expected in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                run_dir = self.make_run(root)
                path = run_dir / "metrics/m0_validation_suite.json"
                document = json.loads(path.read_text(encoding="utf-8"))
                document["started_utc"] = started
                document["completed_utc"] = completed
                document["execution"]["started_monotonic_ns"] = start_ns
                document["execution"]["completed_monotonic_ns"] = end_ns
                path.write_text(json.dumps(document) + "\n", encoding="utf-8")
                result = self.evaluate(root, run_dir)

            failures = result["gates"]["validation_adversarial_suite"]["details"][
                "failures"
            ]
            self.assertFalse(result["passed"])
            self.assertTrue(any(expected in failure for failure in failures), failures)

    def test_source_bindings_must_be_identical_before_and_after_suite(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = self.make_run(root)
            path = run_dir / "metrics/m0_validation_suite.json"
            document = json.loads(path.read_text(encoding="utf-8"))
            relative = next(iter(document["source_bindings_after"]))
            document["source_bindings_after"][relative] = "f" * 64
            path.write_text(json.dumps(document) + "\n", encoding="utf-8")
            result = self.evaluate(root, run_dir)

        failures = result["gates"]["validation_adversarial_suite"]["details"][
            "failures"
        ]
        self.assertFalse(result["passed"])
        self.assertIn("validation-suite source bytes changed during execution", failures)

    def test_required_semantic_coverage_cannot_disappear_with_discovery(self) -> None:
        manifest = json.loads(
            (ROOT_DIR / "network/config/m0_test_manifest.json").read_text(
                encoding="utf-8"
            )
        )
        required_ids = {
            test_id
            for test_ids in validator.REQUIRED_M0_COVERAGE.values()
            for test_id in test_ids
        }
        self.assertLessEqual(required_ids, set(manifest["ordered_test_ids"]))
        removed = validator.REQUIRED_M0_COVERAGE["arp_only"][0]
        discovered = [test_id for test_id in self.TEST_IDS if test_id != removed]
        failures = validator._required_coverage_failures(
            discovered, discovered, context="fixture"
        )
        self.assertTrue(any("arp_only" in failure for failure in failures), failures)

    def test_cross_run_provenance_run_id_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "expected_run"
            metrics = run_dir / "metrics"
            metrics.mkdir(parents=True)
            (metrics / "provenance.json").write_text(
                json.dumps(
                    {
                        "run_id": "substituted_run",
                        "git_commit": "a" * 40,
                        "git_dirty": False,
                        "source_hash": "b" * 64,
                        "config_hashes": {},
                        "dependency_versions": {},
                        "container_image": {},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            result = validator.provenance_status(run_dir)
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(
            result["proof"], "provenance run_id does not match run directory"
        )

    def test_retained_container_inspection_requires_prestart_host_record(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = self.make_run(root)
            (root / ".external/ns-3").mkdir(parents=True)
            control = root / "missing_control"
            with mock.patch.object(validator, "ROOT_DIR", root), mock.patch.object(
                validator,
                "_run_host_command",
                return_value=subprocess.CompletedProcess([], 1, "", "missing"),
            ):
                _, failures = validator._inspect_retained_container(
                    expected_container_id="b" * 64,
                    expected_image="sha256:" + "a" * 64,
                    run_id="m0_fixture",
                    run_dir=run_dir,
                    source_commit="1" * 40,
                    image_reference="multiagent_simulation:latest",
                    initial_control_dir=control,
                )
            joined = "\n".join(failures)
            self.assertIn("prestart control path", joined)
            self.assertIn("prestart inspection record", joined)

    def test_host_final_reexecutes_dependencies_and_full_suite(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = self.make_run(root)
            source_commit = json.loads(
                (run_dir / "metrics/provenance.json").read_text(encoding="utf-8")
            )["git_commit"]
            import_trace = json.loads(
                (run_dir / "metrics/m0_validation_suite.json").read_text(
                    encoding="utf-8"
                )
            )["python_import_trace"]
            snapshot = {
                "git_commit": source_commit,
                "source_file_count": 10,
                "source_binding_sha256": "2" * 64,
            }
            control = root.parent / f".ams-m0-control-m0_fixture.{uuid.uuid4().hex}"
            control.mkdir()
            self.addCleanup(shutil.rmtree, control, True)
            fresh_source = root.parent / f".ams-m0-fresh-source.{uuid.uuid4().hex}"
            fresh_source.mkdir()
            with mock.patch.object(validator, "ROOT_DIR", root), mock.patch.object(
                validator, "_inside_docker_runtime", return_value=False
            ), mock.patch.object(
                validator, "_host_checkout_snapshot", side_effect=[(snapshot, None), (snapshot, None)]
            ), mock.patch.object(
                validator, "_checkout_snapshot", return_value=(snapshot, None)
            ), mock.patch.object(
                validator,
                "_create_fresh_host_source_snapshot",
                return_value=(fresh_source, snapshot, None),
            ), mock.patch.object(
                validator,
                "_inspect_retained_container",
                return_value=(
                    {
                        "container_id": "b" * 64,
                        "source_snapshot": "/tmp/ams-m0-source.fixture",
                        "immutable_fingerprint_sha256": "4" * 64,
                    },
                    [],
                ),
            ), mock.patch.object(
                validator,
                "_run_in_fresh_exact_image",
                return_value=(
                    {
                        "passing_test_count": len(self.TEST_IDS),
                        "python_import_trace": import_trace,
                    },
                    [],
                ),
            ), mock.patch.object(
                validator,
                "_run_isolated_capability_probe",
                return_value=({"status": "passed"}, []),
            ), mock.patch.object(
                validator,
                "_host_execution_identity",
                return_value=({"contract": "fixture"}, []),
            ), mock.patch.object(
                validator, "provenance_status",
                return_value={"status": "passed", "proof": "independent"},
            ), mock.patch.object(
                validator, "_discover_validation_test_ids", return_value=self.TEST_IDS
            ), mock.patch.object(
                validator,
                "_runtime_executable_identity",
                return_value=(self.PYTHON_BYTES, self.PYTHON_SHA256, None),
            ):
                result = validator.host_final_gate(
                    run_dir, "b" * 64, initial_control_dir=control
                )

        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(
            result["details"]["fresh_exact_image_reexecution"]["passing_test_count"],
            len(self.TEST_IDS),
        )

    def test_host_final_rejects_consistent_container_substitution(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = self.make_run(root)
            source_commit = json.loads(
                (run_dir / "metrics/provenance.json").read_text(encoding="utf-8")
            )["git_commit"]
            snapshot = {
                "git_commit": source_commit,
                "source_file_count": 10,
                "source_binding_sha256": "2" * 64,
            }
            control = root.parent / f".ams-m0-control-m0_fixture.{uuid.uuid4().hex}"
            control.mkdir()
            self.addCleanup(shutil.rmtree, control, True)
            with mock.patch.object(validator, "ROOT_DIR", root), mock.patch.object(
                validator, "_inside_docker_runtime", return_value=False
            ), mock.patch.object(
                validator, "_host_checkout_snapshot", return_value=(snapshot, None)
            ), mock.patch.object(
                validator,
                "_host_execution_identity",
                return_value=({"contract": "fixture"}, []),
            ), mock.patch.object(
                validator, "_run_in_fresh_exact_image"
            ) as reexecute:
                result = validator.host_final_gate(
                    run_dir, "c" * 64, initial_control_dir=control
                )

        self.assertEqual(result["status"], "failed")
        failures = result["details"]["failures"]
        self.assertTrue(
            any("identities are not coherent" in failure for failure in failures), failures
        )
        reexecute.assert_not_called()

    def test_host_final_refuses_container_runtime_marker(self) -> None:
        with mock.patch.object(
            validator, "_inside_docker_runtime", return_value=True
        ):
            result = validator.host_final_gate(
                Path("/tmp/not-inspected-run"),
                "b" * 64,
                initial_control_dir=Path("/tmp/not-inspected-control"),
            )
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(result["proof"], "host-final must execute on the Docker host")
        self.assertEqual(result["details"]["failures"], ["/.dockerenv is present"])

    def test_explicit_host_final_gate_is_added_without_recursive_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = self.make_run(root)
            with mock.patch.object(validator, "ROOT_DIR", root), mock.patch.object(
                validator,
                "provenance_status",
                return_value={"status": "passed", "proof": "independent"},
            ), mock.patch.object(
                validator, "_discover_validation_test_ids", return_value=self.TEST_IDS
            ), mock.patch.object(
                validator,
                "_runtime_executable_identity",
                return_value=(self.PYTHON_BYTES, self.PYTHON_SHA256, None),
            ), mock.patch.object(
                validator,
                "host_final_gate",
                return_value={"status": "passed", "proof": "host reexecuted"},
            ) as host_gate:
                ordinary = validator.evaluate_m0_baseline(run_dir)
                host_gate.return_value = {
                    "status": "passed",
                    "proof": "host reexecuted",
                    "details": {
                        "rederived_captured_gates": ordinary["gates"],
                    },
                }
                formal = validator.evaluate_m0_baseline(
                    run_dir,
                    require_host_final=True,
                    expected_container_id="b" * 64,
                    initial_control_dir=root / "control",
                )

        self.assertNotIn("host_final", ordinary["gates"])
        self.assertEqual(formal["gates"]["host_final"]["status"], "passed")
        host_gate.assert_called_once_with(
            run_dir.resolve(), "b" * 64, initial_control_dir=root / "control"
        )

    def test_cli_always_emits_json_and_returns_nonzero_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = self.make_run(root)
            (run_dir / "logs/provenance.log.exit_code").write_text("2\n", encoding="utf-8")
            output = io.StringIO()
            with mock.patch.object(validator, "ROOT_DIR", root), mock.patch.object(
                validator,
                "provenance_status",
                return_value={"status": "passed", "proof": "independently accepted"},
            ), mock.patch.object(
                validator,
                "_discover_validation_test_ids",
                return_value=self.TEST_IDS,
            ), mock.patch.object(
                validator,
                "_runtime_executable_identity",
                return_value=(self.PYTHON_BYTES, self.PYTHON_SHA256, None),
            ), contextlib.redirect_stdout(output):
                return_code = validator.main(["--run-dir", str(run_dir)])

        document = json.loads(output.getvalue())
        self.assertEqual(return_code, 1)
        self.assertFalse(document["passed"])
        self.assertFalse(document["p0_eligible"])

    def test_runner_refuses_to_reuse_existing_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_id = f"m0_existing_{uuid.uuid4().hex}"
            run_root = Path(temp) / "runs"
            run_dir = run_root / run_id
            run_dir.mkdir(parents=True)
            sentinel = run_dir / "sentinel"
            sentinel.write_text("unchanged\n", encoding="utf-8")
            environment = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("AMS_M0_")
            }
            environment.update(
                {
                    "RUN_ID": run_id,
                    "RUN_DIR": str(run_dir),
                    "AMS_M0_ARTIFACT_ROOT": str(run_root),
                }
            )
            result = subprocess.run(
                ["bash", str(ROOT_DIR / "network/scripts/run_m0_baseline.sh")],
                cwd=ROOT_DIR,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("immutable snapshot acceptance path", result.stderr)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged\n")
            self.assertEqual(list(run_dir.iterdir()), [sentinel])

    def test_runner_contains_no_sealing_or_attestation_command(self) -> None:
        text = (ROOT_DIR / "network/scripts/run_m0_baseline.sh").read_text(encoding="utf-8")
        self.assertNotIn("seal_run_evidence.py", text)
        self.assertNotIn("attest_run_evidence.py", text)

    def test_formal_dependency_probe_is_exactly_q0_scoped(self) -> None:
        policy = json.loads(
            (
                ROOT_DIR / "network/config/qualification_path_ownership.json"
            ).read_text(encoding="utf-8")
        )
        explicit = {
            relative: node
            for node, paths in policy["explicit_owners"].items()
            for relative in paths
        }
        path_records = {
            "config:scenario": "network/config/scenario_5uav.yaml",
            "config:endpoints": "network/config/endpoints.yaml",
            "config:service_tiers": "network/config/service_tiers.yaml",
            "config:radio_backend": "network/config/radio_backend.yaml",
            "config:radio_24ghz": "network/config/radio_24ghz.yaml",
            "config:jammers": "network/config/jammers.yaml",
            "config:hitl_loopback": "network/config/hitl_loopback.yaml",
            "config:validation_matrix": "network/config/validation_matrix.yaml",
            "config:metrics_schema": "network/config/metrics_summary_schema.json",
            "component:sionna_provider": "network/radio_provider/provider.py",
            "component:live_sinr_monitor": "network/radio_provider/live_sinr_monitor.py",
            "component:position_tracker": "network/position_tracker/tracker.py",
            "component:ns3_core": "network/ns3/scratch/ams-radio-core.cc",
            "component:bridge": "network/bridge/bridge_config.py",
            "component:hitl": "network/hitl/hitl_loopback.py",
            "cmd:sionna_provider": "network/scripts/run_sionna_provider.sh",
            "cmd:live_sinr_demo": "network/scripts/run_live_sinr_demo.sh",
            "cmd:radio_heatmaps": "network/scripts/generate_radio_heatmaps.sh",
            "cmd:position_tracker": "network/scripts/run_position_tracker.sh",
            "cmd:hitl_loopback": "network/scripts/run_hitl_loopback.sh",
            "cmd:validation": "network/scripts/run_validation.sh",
            "cmd:artifact_collection": "network/scripts/collect_artifacts.sh",
        }
        expected = set(validator.EXPECTED_DEPENDENCY_RECORDS)
        for label, relative in path_records.items():
            owner = explicit.get(relative, "Q0")
            self.assertEqual(label in expected, owner == "Q0", (label, owner))

        scoped_command = 'check_deps.sh" --qualification-profile m0'
        for relative in (
            "network/scripts/run_m0_baseline.sh",
            "network/scripts/run_m0_host_reexecution.sh",
        ):
            text = (ROOT_DIR / relative).read_text(encoding="utf-8")
            self.assertEqual(text.count(scoped_command), 1, relative)
        validator_text = (
            ROOT_DIR / "network/scripts/validate_m0_baseline.py"
        ).read_text(encoding="utf-8")
        self.assertIn('"--qualification-profile",\n                    "m0",', validator_text)


if __name__ == "__main__":
    unittest.main()
