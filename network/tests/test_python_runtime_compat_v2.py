#!/usr/bin/env python3
"""Tests for the M0 Python runtime compatibility smoke gate."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

import yaml


ROOT_DIR = Path(__file__).resolve().parents[2]
SCRIPT = ROOT_DIR / "network/scripts/check_python_runtime_compat.py"
SPEC = importlib.util.spec_from_file_location("check_python_runtime_compat", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
compat = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(compat)


def accepted_lock(numpy_version: str = "1.26.4") -> dict:
    return {
        "schema_version": 2,
        "status": "complete",
        "runtime_policy": {"mitsuba_variant": "llvm_ad_mono_polarized"},
        "dependencies": {
            "ros": {"distribution": "humble"},
            "numpy": {"version": numpy_version},
            "sionna_rt": {"version": "1.2.2"},
            "mitsuba": {"version": "3.8.0"},
            "python_packages": {
                "numpy": numpy_version,
                "sionna-rt": "1.2.2",
                "mitsuba": "3.8.0",
            },
        },
    }


class FakeRuntime:
    def __init__(self, numpy_version: str = "1.26.4") -> None:
        self.import_calls: list[str] = []
        self.modules = {
            "numpy": SimpleNamespace(__file__="/opt/python/numpy/__init__.py", __version__=numpy_version),
            "cv2": SimpleNamespace(__file__="/opt/python/cv2.abi3.so", __version__="4.5.4"),
            "cv_bridge": SimpleNamespace(__file__="/opt/ros/humble/lib/python3.10/site-packages/cv_bridge/__init__.py"),
            "sionna.rt": SimpleNamespace(__file__="/opt/python/sionna/rt/__init__.py"),
            "mitsuba": SimpleNamespace(
                __file__="/opt/python/mitsuba/__init__.py",
                variants=lambda: ["llvm_ad_mono_polarized", "scalar_rgb"],
            ),
            "mpl_toolkits.mplot3d": SimpleNamespace(
                __file__="/opt/python/mpl_toolkits/mplot3d/__init__.py"
            ),
        }
        self.versions = {
            "numpy": numpy_version,
            "sionna-rt": "1.2.2",
            "mitsuba": "3.8.0",
        }

    def importer(self, name: str):
        self.import_calls.append(name)
        if name in {"sionna", "tensorflow"}:
            raise AssertionError(f"forbidden import attempted: {name}")
        module = self.modules.get(name)
        if module is None:
            raise ModuleNotFoundError(name)
        return module

    def version(self, distribution: str) -> str:
        if distribution not in self.versions:
            raise compat.metadata.PackageNotFoundError(distribution)
        return self.versions[distribution]


class PythonRuntimeCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.lock_path = Path(self.temp_dir.name) / "dependency_lock.yaml"
        self.repo_root = Path(self.temp_dir.name) / "repo"
        self.repo_root.mkdir()

    def write_lock(self, value: dict) -> None:
        self.lock_path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")

    def run_fixture(self, runtime: FakeRuntime):
        return compat.run_checks(
            self.lock_path,
            importer=runtime.importer,
            version_resolver=runtime.version,
            repo_root=self.repo_root,
        )

    def test_accepts_exact_humble_compatible_runtime_without_full_sionna(self) -> None:
        self.write_lock(accepted_lock())
        runtime = FakeRuntime()

        results = self.run_fixture(runtime)

        self.assertTrue(results.passed, results.checks)
        self.assertEqual(
            runtime.import_calls,
            [
                "numpy",
                "cv2",
                "cv_bridge",
                "sionna.rt",
                "mitsuba",
                "mpl_toolkits.mplot3d",
            ],
        )
        self.assertNotIn("sionna", runtime.import_calls)
        self.assertNotIn("tensorflow", runtime.import_calls)

    def test_rejects_numpy_2_even_when_runtime_matches_lock(self) -> None:
        self.write_lock(accepted_lock("2.2.6"))
        runtime = FakeRuntime("2.2.6")

        results = self.run_fixture(runtime)

        failed = {row["name"] for row in results.checks if not row["passed"]}
        self.assertIn("lock.numpy_ros_humble_abi", failed)

    def test_rejects_import_failure_in_cv_bridge(self) -> None:
        self.write_lock(accepted_lock())
        runtime = FakeRuntime()
        del runtime.modules["cv_bridge"]

        results = self.run_fixture(runtime)

        failed = {row["name"] for row in results.checks if not row["passed"]}
        self.assertIn("import.cv_bridge", failed)

    def test_rejects_distribution_version_mismatch(self) -> None:
        self.write_lock(accepted_lock())
        runtime = FakeRuntime()
        runtime.versions["sionna-rt"] = "1.2.1"

        results = self.run_fixture(runtime)

        failed = {row["name"] for row in results.checks if not row["passed"]}
        self.assertIn("version.sionna-rt", failed)

    def test_rejects_unavailable_locked_mitsuba_variant(self) -> None:
        lock = accepted_lock()
        lock["runtime_policy"]["mitsuba_variant"] = "cuda_ad_mono_polarized"
        self.write_lock(lock)

        results = self.run_fixture(FakeRuntime())

        failed = {row["name"] for row in results.checks if not row["passed"]}
        self.assertIn("runtime.mitsuba_variant", failed)

    def test_rejects_sionna_meta_package_or_tensorflow_lock_pin(self) -> None:
        lock = accepted_lock()
        lock["dependencies"]["python_packages"]["sionna"] = "1.2.2"
        lock["dependencies"]["python_packages"]["tensorflow"] = "2.21.0"
        self.write_lock(lock)

        results = self.run_fixture(FakeRuntime())

        failed = {row["name"] for row in results.checks if not row["passed"]}
        self.assertIn("lock.no_sionna_meta_or_tensorflow", failed)

    def test_json_cli_output_is_one_machine_readable_document(self) -> None:
        self.write_lock(accepted_lock())
        runtime = FakeRuntime()
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            exit_code = compat.main(
                ["--lock", str(self.lock_path), "--format", "json"],
                importer=runtime.importer,
                version_resolver=runtime.version,
                repo_root=self.repo_root,
            )

        self.assertEqual(exit_code, 0)
        document = json.loads(output.getvalue())
        self.assertTrue(document["passed"])
        self.assertEqual(document["check"], "python_runtime_compat")
        self.assertEqual(document["failure_count"], 0)

    def test_malformed_lock_fails_without_attempting_runtime_imports(self) -> None:
        self.lock_path.write_text("- not\n- a\n- mapping\n", encoding="utf-8")
        runtime = FakeRuntime()

        results = self.run_fixture(runtime)

        self.assertFalse(results.passed)
        self.assertEqual(runtime.import_calls, [])
        self.assertEqual(results.checks[0]["name"], "lock.read")


if __name__ == "__main__":
    unittest.main()
