#!/usr/bin/env python3
"""Tests for deterministic post-receipt status-only document rendering."""

from __future__ import annotations

import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from network.scripts.generate_status_documents import (
    build_metadata,
    parse_receipt_arguments,
    read_receipts,
    render_documents,
    write_documents,
)
from network.scripts.validate_status_documents import (
    FORMAL_GATE_NAMES,
    NEXT_SEQUENCE_CONTRACT,
    NEXT_SEQUENCE_PROFILES,
    STATUS_PATHS,
    _component_status_next_sequence,
    _extract_metadata,
    _validate_status_next_sequence,
    status_documents_status,
)
from network.tests.test_status_documents_validator import LiveStatusFixture
from network.validation.component_profiles import load_profiles


def minimum_m0() -> dict:
    gates = {
        name: {"status": "passed", "details": {"failures": []}}
        for name in FORMAL_GATE_NAMES
    }
    return {
        "schema_version": 3,
        "contract": "ams.m0.host-final-receipt/v1",
        "formal_accepted": True,
        "passed": True,
        "failures": [],
        "consumed_nodes": ["Q0"],
        "qualification_contract_sha256": "a" * 64,
        "gates": gates,
    }


def minimum_m1() -> dict:
    return {
        "schema_version": 1,
        "contract": "ams.m1.host-final-receipt/v1",
        "milestone": "M1",
        "formal_accepted": True,
        "passed": True,
        "failures": [],
        "consumed_nodes": ["Q0", "Q1"],
        "qualification_contract_sha256": "b" * 64,
    }


def minimum_component(index: int) -> dict:
    return {
        "schema_version": 1,
        "profile": f"m{index}_component",
        "formal_accepted": True,
        "passed": True,
        "failures": [],
        "consumed_nodes": [f"Q{value}" for value in range(index + 1)],
        "qualification_contract_sha256": f"{index}" * 64,
    }


class StatusDocumentGeneratorTests(unittest.TestCase):
    def test_v2_v4_next_sequences_are_exact_ordered_and_resumable(self) -> None:
        profiles = load_profiles()
        with tempfile.TemporaryDirectory(prefix="ams-next-sequence-") as temporary:
            root = Path(temporary)
            required_paths = {
                "scripts/run_acceptance_container.sh",
                *(
                    profiles[name]["runner"]
                    for names in NEXT_SEQUENCE_PROFILES.values()
                    for name in names
                ),
            }
            for relative in sorted(required_paths):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
                path.chmod(0o755)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "sequence@example.invalid"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Sequence Test"],
                cwd=root,
                check=True,
            )
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "sequence inputs"], cwd=root, check=True
            )
            commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            for version in (2, 4):
                with self.subTest(version=version):
                    sequence = _component_status_next_sequence(
                        version,
                        root=root,
                        technical_base=commit,
                        profiles=profiles,
                    )
                    self.assertEqual(sequence["contract"], NEXT_SEQUENCE_CONTRACT)
                    self.assertEqual(
                        sequence["ordered_profiles"],
                        list(NEXT_SEQUENCE_PROFILES[version]),
                    )
                    self.assertEqual(
                        [step["position"] for step in sequence["steps"]], [1, 2]
                    )
                    self.assertEqual(
                        [step["role"] for step in sequence["steps"]],
                        ["auxiliary_prerequisite", "milestone_closure"],
                    )
                    placeholders = [
                        step["run_id_placeholder"] for step in sequence["steps"]
                    ]
                    self.assertEqual(len(placeholders), len(set(placeholders)))
                    for step in sequence["steps"]:
                        profile = profiles[step["profile"]]
                        self.assertEqual(
                            step["argv"],
                            [
                                "scripts/run_acceptance_container.sh",
                                "timeout",
                                "--signal=TERM",
                                "--kill-after=20s",
                                f"{profile['timeout_s']}s",
                                "env",
                                f"RUN_ID={step['run_id_placeholder']}",
                                profile["runner"],
                            ],
                        )
                        self.assertTrue(
                            all(
                                item["git_mode"] == "100755"
                                for item in step["tracked_inputs"]
                            )
                        )
                    policy = sequence["resume_policy"]
                    self.assertEqual(
                        policy["successful_auxiliary_receipts_per_source_epoch"],
                        {"required_before_step_2": 1, "maximum": 1},
                    )
                    self.assertTrue(policy["successful_step_1_reexecution_forbidden"])
                    self.assertTrue(policy["successful_step_2_reexecution_forbidden"])
                    self.assertEqual(
                        policy["when_step_2_successful"],
                        "advance_status_do_not_execute_sequence",
                    )
                    self.assertEqual(
                        _validate_status_next_sequence(
                            sequence,
                            version=version,
                            root=root,
                            technical_base=commit,
                            report_commit=commit,
                            profiles=profiles,
                        ),
                        [],
                    )
                    reordered = copy.deepcopy(sequence)
                    reordered["steps"].reverse()
                    failures = _validate_status_next_sequence(
                        reordered,
                        version=version,
                        root=root,
                        technical_base=commit,
                        report_commit=commit,
                        profiles=profiles,
                    )
                    self.assertTrue(any("not exact" in item for item in failures))

            version = 2
            auxiliary_name = NEXT_SEQUENCE_PROFILES[version][0]
            closure_name = NEXT_SEQUENCE_PROFILES[version][1]

            def write_component(profile_name: str, run_id: str) -> Path:
                profile = profiles[profile_name]
                relative = f"runs/{run_id}/metrics/{profile['receipt_name']}"
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                receipt = {
                    "schema_version": 1,
                    "contract": profile["receipt_contract"],
                    "profile": profile_name,
                    "run_id": run_id,
                    "receipt_path": relative,
                    "source_commit": commit,
                    "consumed_nodes": profile["consumed_nodes"],
                    "result_contract": profile["result_contract"],
                    "formal_accepted": True,
                    "passed": True,
                    "failures": [],
                }
                path.write_text(
                    json.dumps(receipt, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                path.chmod(0o444)
                return path

            sequence = _component_status_next_sequence(
                version, root=root, technical_base=commit, profiles=profiles
            )
            next_task = render_documents(
                version, {"schema_version": version, "next_sequence": sequence}
            )["network/NEXT_TASK.md"]
            self.assertIn("next_sequence", next_task)
            self.assertIn("forbids rerunning a successful auxiliary step", next_task)
            first_auxiliary = write_component(auxiliary_name, "capacity_first")
            self.assertEqual(
                _validate_status_next_sequence(
                    sequence,
                    version=version,
                    root=root,
                    technical_base=commit,
                    report_commit=commit,
                    profiles=profiles,
                ),
                [],
            )
            duplicate_auxiliary = write_component(
                auxiliary_name, "capacity_duplicate"
            )
            failures = _validate_status_next_sequence(
                sequence,
                version=version,
                root=root,
                technical_base=commit,
                report_commit=commit,
                profiles=profiles,
            )
            self.assertTrue(
                any("multiple successful auxiliary receipts" in item for item in failures)
            )
            duplicate_auxiliary.chmod(0o600)
            duplicate_auxiliary.unlink()
            write_component(closure_name, "m2_first")
            failures = _validate_status_next_sequence(
                sequence,
                version=version,
                root=root,
                technical_base=commit,
                report_commit=commit,
                profiles=profiles,
            )
            self.assertTrue(
                any(
                    "already has a successful closure receipt" in item
                    for item in failures
                )
            )
            self.assertEqual(
                _validate_status_next_sequence(
                    sequence,
                    version=version,
                    root=root,
                    technical_base=commit,
                    report_commit=commit,
                    profiles=profiles,
                    closure_receipt_consumed=True,
                ),
                [],
            )
            duplicate_closure = write_component(closure_name, "m2_duplicate")
            failures = _validate_status_next_sequence(
                sequence,
                version=version,
                root=root,
                technical_base=commit,
                report_commit=commit,
                profiles=profiles,
            )
            self.assertTrue(
                any("multiple successful closure receipts" in item for item in failures)
            )
            consumed_failures = _validate_status_next_sequence(
                sequence,
                version=version,
                root=root,
                technical_base=commit,
                report_commit=commit,
                profiles=profiles,
                closure_receipt_consumed=True,
            )
            self.assertTrue(
                any(
                    "multiple successful closure receipts" in item
                    for item in consumed_failures
                )
            )
            duplicate_closure.chmod(0o600)
            duplicate_closure.unlink()
            first_auxiliary.chmod(0o600)
            first_auxiliary.unlink()
            failures = _validate_status_next_sequence(
                sequence,
                version=version,
                root=root,
                technical_base=commit,
                report_commit=commit,
                profiles=profiles,
            )
            self.assertTrue(
                any("lacks exactly one auxiliary receipt" in item for item in failures)
            )

    def test_v1_metadata_is_derived_exactly_from_realistic_receipt_fixture(self) -> None:
        fixture = LiveStatusFixture()
        try:
            with self.assertRaisesRegex(ValueError, "HEAD"):
                build_metadata(
                    fixture.root,
                    1,
                    {"m0": fixture.receipt},
                    {"m0": fixture.receipt_path.read_bytes()},
                    {},
                )
            fixture._git("switch", "-q", "--detach", fixture.base)
            receipts, payloads = read_receipts(
                fixture.root, {"m0": fixture.receipt_path}, {}
            )
            metadata = build_metadata(
                fixture.root, 1, receipts, payloads, {}
            )
            self.assertEqual(metadata, fixture.metadata)
            documents = render_documents(1, metadata)
            self.assertEqual(set(documents), set(STATUS_PATHS))
            extracted = [
                _extract_metadata(documents[path], path) for path in STATUS_PATHS
            ]
            self.assertEqual(extracted, [metadata, metadata, metadata])
            state = status_documents_status(
                documents[STATUS_PATHS[0]],
                documents[STATUS_PATHS[1]],
                documents[STATUS_PATHS[2]],
                m0_receipt=fixture.receipt,
            )
            self.assertTrue(state["passed"], state)
        finally:
            fixture.close()

    def test_every_supported_version_renders_exact_cumulative_state(self) -> None:
        receipts = {
            "m0": minimum_m0(),
            "m1": minimum_m1(),
            "m2": minimum_component(2),
            "m3": minimum_component(3),
            "m4": minimum_component(4),
        }
        for version in range(1, 6):
            with self.subTest(version=version):
                metadata = {
                    "schema_version": version,
                    "contract": f"ams.live-status/v{version}",
                }
                documents = render_documents(version, metadata)
                state = status_documents_status(
                    documents[STATUS_PATHS[0]],
                    documents[STATUS_PATHS[1]],
                    documents[STATUS_PATHS[2]],
                    m0_receipt=receipts["m0"],
                    m1_receipt=receipts["m1"],
                    component_receipts={
                        key: value
                        for key, value in receipts.items()
                        if key in {"m2", "m3", "m4"}
                    },
                )
                self.assertTrue(state["passed"], state)
                self.assertEqual(
                    state["fully_closed_sequential_milestones"], version
                )
                self.assertEqual(state["active_milestone"], f"M{version}")

    def test_receipt_argument_set_is_exact_and_output_is_only_three_paths(self) -> None:
        parsed = parse_receipt_arguments(
            ["m0=/tmp/m0.json", "m1=/tmp/m1.json"], 2
        )
        self.assertEqual(set(parsed), {"m0", "m1"})
        for invalid in (
            ["m0=/tmp/m0.json"],
            ["m0=/tmp/m0.json", "m0=/tmp/duplicate.json"],
            ["../m0=/tmp/m0.json", "m1=/tmp/m1.json"],
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                parse_receipt_arguments(invalid, 2)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            documents = render_documents(3, {"schema_version": 3})
            write_documents(root, documents)
            actual = {
                path.relative_to(root).as_posix()
                for path in root.rglob("*")
                if path.is_file()
            }
            self.assertEqual(actual, set(STATUS_PATHS))


if __name__ == "__main__":
    unittest.main()
