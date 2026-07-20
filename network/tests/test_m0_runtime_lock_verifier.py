#!/usr/bin/env python3
"""Focused adversarial tests for the independent M0 runtime-lock verifier."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Mapping

import yaml


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in os.sys.path:
    os.sys.path.insert(0, str(ROOT_DIR))

from network.scripts.verify_m0_runtime_lock import (  # noqa: E402
    CommandResult,
    MAX_REPORT_BYTES,
    _json_bytes,
    run_bounded_command,
    verify_runtime_lock,
)


IMAGE_ID = "sha256:" + "a" * 64
SOURCE_NAMES = (
    "ardupilot_standalone",
    "ardupilot_ros2",
    "micro_ros_agent",
    "ardupilot_gazebo",
    "ardupilot_gz",
    "ardupilot_sitl_models",
    "ros_gz",
    "sdformat_urdf",
    "micro_xrce_dds_gen",
)
MANIFEST_OUTPUTS = {
    "pip_freeze": "z-package==2\na-package==1\n",
    "dpkg": "zlib=1\napt=2\n",
    "ros_packages": "ros_gz_sim\nmultiagent_simulation\n",
}


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def manifest_sha256(output: str) -> str:
    lines = sorted(line.strip() for line in output.splitlines() if line.strip())
    return hashlib.sha256(("\n".join(lines) + "\n").encode()).hexdigest()


def independent_tree_sha256(root: Path) -> str:
    files = [root / "VERSION", root / "src/core.cc"]
    digest = hashlib.sha256()
    for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(bytes.fromhex(sha256_file(path)))
    return digest.hexdigest()


class RuntimeFixture:
    def __init__(self, base: Path) -> None:
        self.base = base.resolve()
        self.root = self.base / "project"
        self.root.mkdir()
        self.ns3 = self.root / ".external/ns-3"
        (self.ns3 / "src").mkdir(parents=True)
        (self.ns3 / "VERSION").write_text("3.40\n", encoding="utf-8")
        (self.ns3 / "src/core.cc").write_text("official core\n", encoding="utf-8")
        for category in ("contributing", "manual", "tutorial"):
            (self.ns3 / f"doc/{category}/source").mkdir(parents=True)
            (self.ns3 / f"doc/{category}/figures").mkdir()
            (self.ns3 / f"doc/{category}/source/figures").symlink_to("../figures")

        self.sources: dict[str, Path] = {}
        self.revisions: dict[str, str] = {}
        for index, name in enumerate(SOURCE_NAMES):
            path = self.base / "sources" / name
            path.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", str(path)], check=True)
            subprocess.run(
                ["git", "-C", str(path), "config", "user.email", "m0@example.invalid"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(path), "config", "user.name", "M0 Test"],
                check=True,
            )
            (path / "source.txt").write_text(f"source {index}\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(path), "add", "source.txt"], check=True)
            subprocess.run(
                ["git", "-C", str(path), "commit", "-q", "-m", "fixture"],
                check=True,
            )
            revision = subprocess.run(
                ["git", "-C", str(path), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.sources[name] = path
            self.revisions[name] = revision

        runtime = self.base / "runtime"
        runtime.mkdir()
        self.executables: dict[str, Path] = {}
        for name in ("arducopter", "gazebo", "python", "micro_agent"):
            path = runtime / name
            path.write_bytes(f"#!/bin/sh\n# {name}\nexit 0\n".encode())
            path.chmod(0o755)
            self.executables[name] = path
        self.invoked = runtime / "mavproxy.py"
        self.invoked.write_text("# mavproxy fixture\n", encoding="utf-8")

        lock = {
            "schema_version": 2,
            "status": "complete",
            "runtime_manifest_sha256": {
                name: manifest_sha256(output)
                for name, output in MANIFEST_OUTPUTS.items()
            },
            "m0_execution_policy": {
                "schema_version": 1,
                "container_path": os.environ.get("PATH", ""),
                "host_final_path": "/usr/bin:/bin",
                "allowed_container_executable_roots": [str(runtime)],
                "critical_command_resolution": {"git": "/usr/bin/git"},
                "critical_image_executable_sha256": {
                    str(self.executables["python"]): sha256_file(
                        self.executables["python"]
                    ),
                    "/usr/bin/git": sha256_file(Path("/usr/bin/git")),
                },
                "critical_source_executables": ["scripts/fixture.sh"],
                "host_final_executable_sha256": {
                    str(self.executables["python"]): sha256_file(
                        self.executables["python"]
                    )
                },
                "host_final_python_sys_path": [str(runtime)],
                "host_final_python_imports": {
                    "fixture_module": {
                        "path": str(self.invoked),
                        "bytes": self.invoked.stat().st_size,
                        "sha256": sha256_file(self.invoked),
                    }
                },
                "incidental_transitive_policy": {
                    "require_immutable_allowed_root": True,
                    "require_dpkg_or_pip_manifest_owner": True,
                    "forbid_host_or_run_writable_resolution": True,
                },
            },
            "m0_python_import_policy": {
                "schema_version": 1,
                "mode": "isolated_explicit_path",
                "interpreter": str(self.executables["python"]),
                "interpreter_sha256": sha256_file(self.executables["python"]),
                "parent_flags": ["-S"],
                "exact_base_pythonpath": [str(runtime)],
                "overlay_pythonpath_template": "/tmp/fixture-{run_id}",
                "interpreter_suffix": [str(runtime / "stdlib")],
                "customization": {
                    "parent_sitecustomize_loaded": False,
                    "parent_usercustomize_loaded": False,
                    "child_guard_path": str(runtime / "sitecustomize.py"),
                    "child_guard_sha256": "0" * 64,
                },
                "pth_policy": "inventory_only_not_processed_under_no_site",
                "cleared_environment": ["PYTHONHOME"],
                "python_no_user_site": True,
                "bytecode_root_template": "/tmp/fixture-pycache-{run_id}",
            },
            "m1_runtime_identity": {
                "schema_version": 1,
                "container_image_digest": IMAGE_ID,
                "executable_sha256": {
                    str(path): sha256_file(path)
                    for path in self.executables.values()
                },
                "role_executable_path": {
                    "arducopter": str(self.executables["arducopter"]),
                    "gazebo_server": str(self.executables["gazebo"]),
                    "mavproxy": str(self.executables["python"]),
                    "micro_ros_agent": str(self.executables["micro_agent"]),
                },
                "invoked_file_sha256": {str(self.invoked): sha256_file(self.invoked)},
                "role_invoked_file_path": {"mavproxy": str(self.invoked)},
            },
            "dependencies": {
                "canonical_runtime_source_paths": {
                    name: str(path) for name, path in self.sources.items()
                },
                "ros": {"project_image_digest": IMAGE_ID},
                "ardupilot": {"revision": self.revisions["ardupilot_standalone"]},
                "ardupilot_ros_repos": {
                    "revisions": {
                        "ardupilot": self.revisions["ardupilot_ros2"],
                        "micro_ros_agent": self.revisions["micro_ros_agent"],
                    }
                },
                "ardupilot_gz_repos": {
                    "revisions": {
                        name: self.revisions[name]
                        for name in (
                            "ardupilot_gazebo",
                            "ardupilot_gz",
                            "ardupilot_sitl_models",
                            "ros_gz",
                            "sdformat_urdf",
                        )
                    }
                },
                "micro_xrce_dds_gen": {
                    "revision": self.revisions["micro_xrce_dds_gen"]
                },
                "ns3": {
                    "version": "3.40",
                    "source_kind": "official_release_archive",
                    "core_tree_sha256": independent_tree_sha256(self.ns3),
                    "core_tree_excludes": [
                        "build",
                        "cmake-cache",
                        "scratch",
                        "src/lorawan",
                    ],
                    "path": ".external/ns-3",
                },
            },
        }
        self.lock = self.base / "dependency_lock.yaml"
        self.lock.write_text(yaml.safe_dump(lock, sort_keys=False), encoding="utf-8")

    def runner(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float = 60.0,
    ) -> CommandResult:
        if Path(args[0]).name == "git":
            return run_bounded_command(args, cwd=cwd, env=env, timeout=timeout)
        if "pip" in args:
            output = MANIFEST_OUTPUTS["pip_freeze"]
        elif Path(args[0]).name == "dpkg-query":
            output = MANIFEST_OUTPUTS["dpkg"]
        elif Path(args[0]).name == "ros2":
            output = MANIFEST_OUTPUTS["ros_packages"]
        else:
            return CommandResult(127, "", "unexpected command")
        return CommandResult(0, output, "")

    def verify(self, digest: str = IMAGE_ID) -> dict:
        return verify_runtime_lock(
            self.lock,
            digest,
            root_dir=self.root,
            command_runner=self.runner,
            environment_image_digest=digest,
        )


class M0RuntimeLockVerifierTests(unittest.TestCase):
    def make_fixture(self) -> tuple[tempfile.TemporaryDirectory[str], RuntimeFixture]:
        temporary = tempfile.TemporaryDirectory()
        return temporary, RuntimeFixture(Path(temporary.name))

    def test_recomputes_all_runtime_facts_without_producer_pass_flags(self) -> None:
        temporary, fixture = self.make_fixture()
        with temporary:
            report = fixture.verify()
            self.assertTrue(report["passed"], report["failures"])
            self.assertEqual(report["failures"], [])
            self.assertEqual(report["checks"]["runtime_manifests"]["passes"], 2)
            self.assertEqual(report["checks"]["runtime_identity_files"]["files_checked"], 5)
            self.assertEqual(report["checks"]["m0_execution_policy"]["files_checked"], 2)
            self.assertEqual(report["checks"]["external_sources"]["repositories_checked"], 9)
            self.assertEqual(report["checks"]["ns3_tree"]["files_checked"], 2)
            encoded = _json_bytes(report)
            self.assertLess(len(encoded), MAX_REPORT_BYTES)
            self.assertTrue(json.loads(encoded)["passed"])

    def test_tampered_locked_executable_fails_closed(self) -> None:
        temporary, fixture = self.make_fixture()
        with temporary:
            fixture.executables["arducopter"].write_text("tampered\n", encoding="utf-8")
            fixture.executables["arducopter"].chmod(0o755)
            report = fixture.verify()
            self.assertFalse(report["passed"])
            self.assertEqual(report["checks"]["runtime_identity_files"]["status"], "failed")
            self.assertIn("hash mismatch", "\n".join(report["failures"]))

    def test_forged_m0_critical_executable_hash_fails_closed(self) -> None:
        temporary, fixture = self.make_fixture()
        with temporary:
            document = yaml.safe_load(fixture.lock.read_text(encoding="utf-8"))
            critical = document["m0_execution_policy"][
                "critical_image_executable_sha256"
            ]
            critical[str(fixture.executables["python"])] = "0" * 64
            document["m0_python_import_policy"]["interpreter_sha256"] = "0" * 64
            fixture.lock.write_text(
                yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
            )
            report = fixture.verify()
            self.assertFalse(report["passed"])
            self.assertEqual(report["checks"]["m0_execution_policy"]["status"], "failed")
            self.assertIn("critical image executable hash mismatch", "\n".join(report["failures"]))

    def test_dirty_or_wrong_revision_external_source_fails_closed(self) -> None:
        temporary, fixture = self.make_fixture()
        with temporary:
            (fixture.sources["ros_gz"] / "untracked.txt").write_text(
                "dirty\n", encoding="utf-8"
            )
            report = fixture.verify()
            self.assertFalse(report["passed"])
            self.assertEqual(report["checks"]["external_sources"]["status"], "failed")
            self.assertIn("dirty", "\n".join(report["failures"]))

    def test_ns3_aggregate_tree_mutation_fails_closed(self) -> None:
        temporary, fixture = self.make_fixture()
        with temporary:
            (fixture.ns3 / "src/core.cc").write_text("tampered core\n", encoding="utf-8")
            report = fixture.verify()
            self.assertFalse(report["passed"])
            self.assertEqual(report["checks"]["ns3_tree"]["status"], "failed")
            self.assertIn("aggregate tree", "\n".join(report["failures"]))

    def test_manifest_and_host_image_mismatch_fail_closed(self) -> None:
        temporary, fixture = self.make_fixture()
        with temporary:
            original = fixture.runner

            def changed_runner(
                args: list[str],
                *,
                cwd: Path | None = None,
                env: Mapping[str, str] | None = None,
                timeout: float = 60.0,
            ) -> CommandResult:
                result = original(args, cwd=cwd, env=env, timeout=timeout)
                if "pip" in args:
                    return CommandResult(0, result.stdout + "injected==1\n", "")
                return result

            report = verify_runtime_lock(
                fixture.lock,
                "sha256:" + "b" * 64,
                root_dir=fixture.root,
                command_runner=changed_runner,
                environment_image_digest="sha256:" + "c" * 64,
            )
            self.assertFalse(report["passed"])
            self.assertEqual(report["checks"]["image_digest"]["status"], "failed")
            self.assertEqual(report["checks"]["runtime_manifests"]["status"], "failed")
            failures = "\n".join(report["failures"])
            self.assertIn("environment disagrees", failures)
            self.assertIn("manifest does not match", failures)

    def test_duplicate_yaml_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            lock = Path(temp) / "lock.yaml"
            lock.write_text(
                "schema_version: 2\nstatus: complete\nstatus: complete\n",
                encoding="utf-8",
            )
            report = verify_runtime_lock(
                lock,
                IMAGE_ID,
                root_dir=Path(temp),
                command_runner=lambda *_args, **_kwargs: CommandResult(1, "", "unused"),
            )
            self.assertFalse(report["passed"])
            self.assertEqual(report["checks"]["lock"]["status"], "failed")
            self.assertIn("duplicate", "\n".join(report["failures"]))


if __name__ == "__main__":
    unittest.main()
