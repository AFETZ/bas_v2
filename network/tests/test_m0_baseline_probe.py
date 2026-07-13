#!/usr/bin/env python3
"""Focused tests for the dependency/provenance-only M0 probe."""

from __future__ import annotations

import contextlib
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


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from network.scripts import validate_m0_baseline as validator  # noqa: E402


class M0BaselineProbeTests(unittest.TestCase):
    def make_run(self, root: Path, run_id: str = "m0_fixture") -> Path:
        run_dir = root / "runs" / run_id
        (run_dir / "logs").mkdir(parents=True)
        (run_dir / "metrics").mkdir()
        (run_dir / "logs/check_deps.log").write_text(
            "Network/radio dependency check\n"
            "Dependency check passed with 1 warning(s).\n",
            encoding="utf-8",
        )
        (run_dir / "logs/check_deps.log.exit_code").write_text("0\n", encoding="utf-8")
        (run_dir / "logs/provenance.log").write_text(
            "Provenance generated\nAcceptance eligible: true\n", encoding="utf-8"
        )
        (run_dir / "logs/provenance.log.exit_code").write_text("0\n", encoding="utf-8")
        (run_dir / "metrics/provenance.json").write_text("{}\n", encoding="utf-8")
        return run_dir

    def test_pass_requires_both_independent_gates(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = self.make_run(root)
            with mock.patch.object(validator, "ROOT_DIR", root), mock.patch.object(
                validator,
                "provenance_status",
                return_value={"status": "passed", "proof": "independently accepted"},
            ):
                result = validator.evaluate_m0_baseline(run_dir)

        self.assertTrue(result["passed"])
        self.assertFalse(result["p0_eligible"])
        self.assertEqual(set(result["gates"]), {"dependency_check", "provenance"})
        self.assertTrue(all(gate["status"] == "passed" for gate in result["gates"].values()))
        self.assertFalse(result["scope"]["packet_path"])
        self.assertFalse(result["scope"]["sealing"])
        self.assertFalse(result["scope"]["attestation"])

    def test_nonzero_dependency_exit_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = self.make_run(root)
            (run_dir / "logs/check_deps.log.exit_code").write_text("3\n", encoding="utf-8")
            with mock.patch.object(validator, "ROOT_DIR", root), mock.patch.object(
                validator,
                "provenance_status",
                return_value={"status": "passed", "proof": "independently accepted"},
            ):
                result = validator.evaluate_m0_baseline(run_dir)

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
            with mock.patch.object(validator, "ROOT_DIR", root), mock.patch.object(
                validator, "provenance_status", return_value=independent
            ):
                result = validator.evaluate_m0_baseline(run_dir)

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
            with mock.patch.object(validator, "ROOT_DIR", root), mock.patch.object(
                validator,
                "provenance_status",
                return_value={"status": "passed", "proof": "independently accepted"},
            ):
                result = validator.evaluate_m0_baseline(run_dir)

        failures = result["gates"]["dependency_check"]["details"]["failures"]
        self.assertFalse(result["passed"])
        self.assertTrue(any("not a regular file" in failure for failure in failures))

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
            ), contextlib.redirect_stdout(output):
                return_code = validator.main(["--run-dir", str(run_dir)])

        document = json.loads(output.getvalue())
        self.assertEqual(return_code, 1)
        self.assertFalse(document["passed"])
        self.assertFalse(document["p0_eligible"])

    def test_runner_refuses_to_reuse_existing_run(self) -> None:
        run_id = f"m0_existing_{uuid.uuid4().hex}"
        run_dir = ROOT_DIR / "runs" / run_id
        run_dir.mkdir(parents=True)
        sentinel = run_dir / "sentinel"
        sentinel.write_text("unchanged\n", encoding="utf-8")
        try:
            result = subprocess.run(
                ["bash", str(ROOT_DIR / "network/scripts/run_m0_baseline.sh")],
                cwd=ROOT_DIR,
                env={**os.environ, "RUN_ID": run_id},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("immutable M0 run directory already exists", result.stderr)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged\n")
            self.assertEqual(list(run_dir.iterdir()), [sentinel])
        finally:
            shutil.rmtree(run_dir)

    def test_runner_contains_no_sealing_or_attestation_command(self) -> None:
        text = (ROOT_DIR / "network/scripts/run_m0_baseline.sh").read_text(encoding="utf-8")
        self.assertNotIn("seal_run_evidence.py", text)
        self.assertNotIn("attest_run_evidence.py", text)


if __name__ == "__main__":
    unittest.main()
