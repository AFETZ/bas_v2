#!/usr/bin/env python3
"""Mutation tests for downstream live-status prerequisite resolution."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from network.scripts.resolve_component_prerequisites import resolve


COMMIT = "4" * 40
STATUS_PATHS = (
    "network/PROGRESS.md",
    "network/VALIDATION_REPORT.md",
    "network/NEXT_TASK.md",
)
BEGIN = "<!-- AMS_LIVE_STATUS_METADATA_BEGIN\n"
END = "\nAMS_LIVE_STATUS_METADATA_END -->"


def pretty(value: object) -> bytes:
    return json.dumps(value, indent=2, sort_keys=True).encode("utf-8") + b"\n"


class ComponentPrerequisiteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "network/config").mkdir(parents=True)
        source = (
            Path(__file__).resolve().parents[1]
            / "config/component_acceptance_profiles.json"
        )
        (self.root / "network/config/component_acceptance_profiles.json").write_bytes(
            source.read_bytes()
        )
        self.status_result = self.root / "status_validation.json"
        self.status_result.write_bytes(
            pretty(
                {
                    "schema_version": 1,
                    "contract": "ams.live-status-lint/v1",
                    "passed": True,
                    "failures": [],
                    "report_commit": COMMIT,
                    "status_paths": list(STATUS_PATHS),
                }
            )
        )
        evidence = {
            name: self.make_receipt(name, f"M{index}")
            for index, name in enumerate(("m0", "m1"))
        }
        metadata = {
            "schema_version": 2,
            "contract": "ams.live-status/v2",
            "state": {
                "fully_closed_sequential_milestones": 2,
                "customer_ready": False,
            },
            "evidence": evidence,
        }
        encoded = json.dumps(metadata, indent=2, sort_keys=True)
        for relative in STATUS_PATHS:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"status\n{BEGIN}{encoded}{END}\n", encoding="utf-8")

    def make_receipt(self, name: str, milestone: str) -> dict[str, object]:
        run_id = f"{name}_fixture"
        relative = f"runs/{run_id}/metrics/{name}_host_final_receipt.json"
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        receipt = {
            "schema_version": 1,
            "contract": f"ams.{name}.host-final-receipt/v1",
            "run_id": run_id,
            "receipt_path": relative,
            "formal_accepted": True,
            "passed": True,
            "failures": [],
        }
        payload = pretty(receipt)
        path.write_bytes(payload)
        path.chmod(0o400)
        return {
            "kind": "host_final_receipt",
            "milestone": milestone,
            "run_id": run_id,
            "receipt_path": relative,
            "receipt_sha256": hashlib.sha256(payload).hexdigest(),
            "qualification_contract_sha256": "5" * 64,
        }

    def make_capacity_receipt(self) -> Path:
        run_id = "capacity_fixture"
        receipt_name = "flight_capacity_host_final_receipt.json"
        relative = f"runs/{run_id}/metrics/{receipt_name}"
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        receipt = {
            "schema_version": 1,
            "contract": "ams.flight-capacity-host-final-receipt/v1",
            "profile": "flight_capacity_prerequisite",
            "run_id": run_id,
            "receipt_path": relative,
            "source_commit": COMMIT,
            "consumed_nodes": ["Q0", "Q1"],
            "result_contract": "ams.flight-capacity-validation/v1",
            "formal_accepted": True,
            "passed": True,
            "failures": [],
        }
        path.write_bytes(pretty(receipt))
        path.chmod(0o400)
        return path

    def resolve(self) -> dict[str, object]:
        with mock.patch.dict(
            os.environ, {"AMS_COMPONENT_SOURCE_COMMIT": COMMIT}, clear=False
        ):
            return resolve(
                self.root, "flight_capacity_prerequisite", self.status_result
            )

    def test_exact_two_receipt_status_authority_resolves(self) -> None:
        result = self.resolve()
        self.assertEqual(result["contract"], "ams.component-prerequisites/v1")
        self.assertEqual(result["profile"], "flight_capacity_prerequisite")
        self.assertEqual(set(result["receipts"]), {"m0", "m1"})
        self.assertEqual(result["status"]["closed_count"], 2)

    def test_writable_receipt_is_rejected(self) -> None:
        path = self.root / "runs/m1_fixture/metrics/m1_host_final_receipt.json"
        path.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "read-only"):
            self.resolve()

    def test_stale_lint_commit_is_rejected(self) -> None:
        document = json.loads(self.status_result.read_text(encoding="utf-8"))
        document["report_commit"] = "6" * 40
        self.status_result.write_bytes(pretty(document))
        with self.assertRaisesRegex(ValueError, "passing/current/exact"):
            self.resolve()

    def test_missing_m1_evidence_is_rejected(self) -> None:
        for relative in STATUS_PATHS:
            path = self.root / relative
            text = path.read_text(encoding="utf-8")
            payload = json.loads(text.split(BEGIN, 1)[1].split(END, 1)[0])
            del payload["evidence"]["m1"]
            path.write_text(
                f"status\n{BEGIN}{json.dumps(payload, indent=2, sort_keys=True)}{END}\n",
                encoding="utf-8",
            )
        with self.assertRaisesRegex(ValueError, "evidence map"):
            self.resolve()

    def test_m2_requires_one_current_capacity_receipt(self) -> None:
        with mock.patch.dict(
            os.environ, {"AMS_COMPONENT_SOURCE_COMMIT": COMMIT}, clear=False
        ):
            with self.assertRaisesRegex(ValueError, "flight_capacity_prerequisite"):
                resolve(self.root, "m2_component", self.status_result)
            capacity_path = self.make_capacity_receipt()
            result = resolve(self.root, "m2_component", self.status_result)
        self.assertEqual(
            result["component_receipts"]["flight_capacity_prerequisite"][
                "host_path"
            ],
            str(capacity_path.resolve()),
        )


if __name__ == "__main__":
    unittest.main()
