#!/usr/bin/env python3
"""Tests for v2 source/config provenance generation and acceptance checks."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from network.scripts.write_run_provenance import (  # noqa: E402
    build_provenance,
    deterministic_source_hash,
    parse_args,
    ns3_core_tree_hash,
    runtime_container_identity,
    source_files,
)
from network.scripts import write_run_provenance as provenance_module  # noqa: E402
from network.validation import evidence as evidence_module  # noqa: E402
from network.validation.evidence import provenance_status  # noqa: E402


class ProvenanceV2Tests(unittest.TestCase):
    def test_source_hash_is_deterministic(self) -> None:
        files = source_files()
        self.assertGreater(len(files), 10)
        first = deterministic_source_hash(files)
        second = deterministic_source_hash(files)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)

    def test_full_container_id_comes_from_host_bind_mount(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            identity_file = Path(temp) / "container_id"
            identity_file.write_text("a" * 64 + "\n", encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {"AMS_RUNTIME_CONTAINER_ID_FILE": str(identity_file), "HOSTNAME": "b" * 12},
            ):
                identity, source = runtime_container_identity()
        self.assertEqual(identity, "a" * 64)
        self.assertEqual(source, "host_bind_mount")

    def test_ns3_release_tree_uses_relative_path_order(self) -> None:
        ns3 = ROOT_DIR / ".external/ns-3"
        if not ns3.is_dir():
            self.skipTest("ns-3 release tree is not materialized")
        count, digest = ns3_core_tree_hash(ns3)
        self.assertGreater(count, 3000)
        self.assertEqual(
            digest,
            "0119836a7c79f7470f0c2c866de9c14ddc4f22349bbd194112ff2952713b64e8",
        )

    def test_generator_records_current_dirty_state_consistently(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "provenance_dirty"
            args = parse_args(
                [
                    "--run-dir",
                    str(run_dir),
                    "--container-image",
                    "multiagent_simulation:test",
                    "--container-digest",
                    "sha256:test",
                ]
            )
            args.config = ["network/config/validation_matrix.yaml"]
            data = build_provenance(args)
            self.assertEqual(data["run_id"], "provenance_dirty")
            self.assertEqual(data["git_dirty"], bool(data["git_status"]))
            self.assertFalse(data["acceptance_eligible"])
            self.assertIn("dependency lock is not complete", data["acceptance_blockers"])
            self.assertEqual(len(data["git_diff_sha256"]), 64)
            self.assertIn("runtime_manifests", data["dependency_versions"])
            self.assertIn("pip_freeze", data["dependency_versions"]["runtime_manifests"])
            pip_manifest = data["dependency_versions"]["runtime_manifests"]["pip_freeze"]
            self.assertEqual(pip_manifest["entries"], len(pip_manifest["lines"]))
            self.assertEqual(pip_manifest["lines"], sorted(set(pip_manifest["lines"])))
            self.assertIn("external_sources", data["dependency_versions"])
            self.assertIn("ardupilot_ros2", data["dependency_versions"]["external_sources"])
            self.assertIn("network/config/validation_matrix.yaml", data["config_hashes"])
            source_manifest = data["source_manifest"]
            self.assertIn(".devcontainer/Dockerfile", source_manifest)
            self.assertIn(".devcontainer/ardupilot_ros2_exact.repos", source_manifest)
            self.assertIn(".devcontainer/setup.sh", source_manifest)

    def test_cli_refuses_to_overwrite_existing_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "immutable"
            output = run_dir / "metrics/provenance.json"
            output.parent.mkdir(parents=True)
            output.write_text('{"sentinel": true}\n', encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT_DIR / "network/scripts/write_run_provenance.py"),
                    "--run-dir",
                    str(run_dir),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(output.read_text()), {"sentinel": True})
            self.assertIn("already exists", result.stderr)

    def test_generator_fails_closed_when_git_inspection_is_unavailable(self) -> None:
        original = provenance_module.run_command

        def selective_failure(command: list[str], cwd: Path = ROOT_DIR) -> str | None:
            if command[:2] in (["git", "status"], ["git", "diff"]):
                return None
            return original(command, cwd=cwd)

        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            provenance_module, "run_command", side_effect=selective_failure
        ):
            args = parse_args(
                [
                    "--run-dir",
                    str(Path(temp) / "git_unavailable"),
                    "--container-image",
                    "multiagent_simulation:latest",
                    "--container-digest",
                    "sha256:" + "a" * 64,
                ]
            )
            args.config = ["network/config/validation_matrix.yaml"]
            data = build_provenance(args)

        self.assertFalse(data["acceptance_eligible"])
        self.assertIn("git status could not be inspected", data["acceptance_blockers"])
        self.assertIn("git diff could not be inspected", data["acceptance_blockers"])
        self.assertNotEqual(data["git_diff_sha256"], hashlib.sha256(b"").hexdigest())

    def test_cross_run_provenance_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "expected_run"
            (run_dir / "metrics").mkdir(parents=True)
            record = {
                "schema_version": 2,
                "run_id": "different_run",
                "git_commit": "a" * 40,
                "git_dirty": False,
                "source_hash": "b" * 64,
                "config_hashes": {"config": "c" * 64},
                "dependency_versions": {"python": "3.10"},
                "container_image": {"reference": "image", "digest": "sha256:abc"},
                "acceptance_eligible": True,
            }
            (run_dir / "metrics/provenance.json").write_text(json.dumps(record), encoding="utf-8")
            result = provenance_status(run_dir)
            self.assertEqual(result["status"], "failed")
            self.assertIn("run_id", result["proof"])

    def test_structurally_forged_clean_provenance_cannot_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "matching_run"
            (run_dir / "metrics").mkdir(parents=True)
            record = {
                "schema_version": 2,
                "run_id": "matching_run",
                "git_commit": "a" * 40,
                "git_dirty": False,
                "source_hash": "b" * 64,
                "config_hashes": {"config": "c" * 64},
                "dependency_versions": {"python": "3.10"},
                "container_image": {"reference": "image", "digest": "sha256:abc"},
                "acceptance_eligible": True,
            }
            (run_dir / "metrics/provenance.json").write_text(json.dumps(record), encoding="utf-8")
            result = provenance_status(run_dir)
            self.assertEqual(result["status"], "failed")
            failures = "\n".join(result["details"]["failures"])
            self.assertIn("source", failures)
            self.assertIn("dependency lock", failures)

    def test_unknown_container_digest_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "unknown_container"
            (run_dir / "metrics").mkdir(parents=True)
            record = {
                "schema_version": 2,
                "run_id": "unknown_container",
                "git_commit": "a" * 40,
                "git_dirty": False,
                "source_hash": "b" * 64,
                "config_hashes": {"config": "c" * 64},
                "dependency_versions": {"python": "3.10"},
                "container_image": {"reference": "image", "digest": "unknown"},
                "acceptance_eligible": False,
            }
            (run_dir / "metrics/provenance.json").write_text(json.dumps(record), encoding="utf-8")
            result = provenance_status(run_dir)
            self.assertEqual(result["status"], "failed")
            self.assertIn("not acceptance-eligible", result["proof"])

    def test_nul_in_config_path_is_rejected_without_validator_crash(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "nul_config"
            (run_dir / "metrics").mkdir(parents=True)
            record = {
                "schema_version": 2,
                "run_id": "nul_config",
                "git_commit": "a" * 40,
                "git_dirty": False,
                "git_status": [],
                "git_diff_sha256": "b" * 64,
                "source_hash": "c" * 64,
                "source_manifest": {},
                "config_hashes": {"network/config/bad\u0000path.yaml": "d" * 64},
                "dependency_versions": {"python": "3.10"},
                "container_image": {
                    "reference": "image",
                    "digest": "sha256:" + "e" * 64,
                    "digest_source": "docker_image_inspect_host",
                    "runtime_container_id": "f" * 64,
                },
                "acceptance_eligible": True,
            }
            (run_dir / "metrics/provenance.json").write_text(
                json.dumps(record), encoding="utf-8"
            )

            result = provenance_status(run_dir)

            self.assertEqual(result["status"], "failed")
            self.assertIn("config hash path is invalid", "\n".join(result["details"]["failures"]))

    def test_malformed_dependency_lock_mapping_fails_without_crash(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fake_root = Path(temp)
            lock_path = fake_root / "network/config/dependency_lock.yaml"
            lock_path.parent.mkdir(parents=True)
            lock_path.write_text("[]\n", encoding="utf-8")
            run_dir = fake_root / "malformed_lock"
            (run_dir / "metrics").mkdir(parents=True)
            record = {
                "schema_version": 2,
                "run_id": run_dir.name,
                "generated_utc": "2026-07-12T12:00:00Z",
                "git_commit": "a" * 40,
                "git_dirty": False,
                "git_status": [],
                "git_diff_sha256": hashlib.sha256(b"").hexdigest(),
                "source_hash": "b" * 64,
                "source_files": 1,
                "source_manifest": {"source": "c" * 64},
                "config_hashes": {"config": "d" * 64},
                "dependency_versions": {},
                "container_image": {},
                "dependency_lock_status": "complete",
                "acceptance_blockers": [],
                "acceptance_eligible": True,
            }
            (run_dir / "metrics/provenance.json").write_text(
                json.dumps(record), encoding="utf-8"
            )
            with mock.patch.object(evidence_module, "ROOT_DIR", fake_root):
                result = provenance_status(run_dir)

            self.assertEqual(result["status"], "failed")
            self.assertIn(
                "dependency lock root is not a mapping",
                "\n".join(result["details"]["failures"]),
            )

    def test_generator_marks_malformed_dependency_lock_ineligible(self) -> None:
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            provenance_module.yaml, "safe_load", return_value=[]
        ):
            args = parse_args(
                [
                    "--run-dir",
                    str(Path(temp) / "malformed_generator_lock"),
                ]
            )
            args.config = ["network/config/validation_matrix.yaml"]
            data = build_provenance(args)

        self.assertFalse(data["acceptance_eligible"])
        self.assertIn(
            "dependency lock root is not a mapping", data["acceptance_blockers"]
        )


if __name__ == "__main__":
    unittest.main()
