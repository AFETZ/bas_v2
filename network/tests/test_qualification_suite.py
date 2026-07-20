#!/usr/bin/env python3
"""Adversarial tests for deterministic per-node unittest manifests."""

from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from network.scripts.qualification_suite import (  # noqa: E402
    QUALIFICATION_TEST_DISCOVERY,
    QUALIFICATION_TEST_MANIFEST_CONTRACT,
    QualificationSuiteError,
    canonical_pretty_json,
    discover_owned_test_suite,
    exact_consumed_nodes,
    load_node_test_manifest,
    manifest_path,
    prepare_node_test_suite,
    qualification_source_bindings,
    repository_module_scope_failures,
    validate_test_manifest,
)


def _write(path: Path, payload: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_bytes(payload)


def _manifest(node: str, modules: list[str], test_ids: list[str]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "contract": QUALIFICATION_TEST_MANIFEST_CONTRACT,
        "node": node,
        "discovery": dict(QUALIFICATION_TEST_DISCOVERY),
        "test_modules": modules,
        "ordered_test_ids": test_ids,
    }


class QualificationSuiteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="ams-q-suite-")
        self.root = Path(self.temporary.name)
        (self.root / "network/tests").mkdir(parents=True)
        self.created_modules: set[str] = set()

    def tearDown(self) -> None:
        for module in self.created_modules:
            sys.modules.pop(module, None)
        while str(self.root / "network/tests") in sys.path:
            sys.path.remove(str(self.root / "network/tests"))
        self.temporary.cleanup()

    def add_test_module(self, module: str, body: str) -> str:
        self.created_modules.add(module)
        relative = f"network/tests/{module}.py"
        _write(self.root / relative, body)
        return relative

    def add_manifest(
        self,
        node: str,
        modules: list[str],
        test_ids: list[str],
        *,
        payload: bytes | None = None,
    ) -> str:
        relative = manifest_path(node)
        document = _manifest(node, modules, test_ids)
        _write(
            self.root / relative,
            payload if payload is not None else canonical_pretty_json(document),
        )
        return relative

    def vector(self, owners: dict[str, str]) -> dict[str, Any]:
        entries: list[dict[str, Any]] = []
        for relative, owner in sorted(owners.items()):
            payload = (self.root / relative).read_bytes()
            entries.append(
                {
                    "path": relative,
                    "owner": owner,
                    "kind": "regular",
                    "git_mode": "100644",
                    "object_type": "blob",
                    "git_object_id": "1" * 40,
                    "blob_size": len(payload),
                    "blob_sha256": hashlib.sha256(payload).hexdigest(),
                }
            )
        return {
            "assignments": dict(sorted(owners.items())),
            "entry_manifest": entries,
        }

    def test_manifest_schema_is_exact_and_rejects_cross_module_ids(self) -> None:
        valid = _manifest(
            "Q1",
            ["test_q1_alpha"],
            ["test_q1_alpha.ExampleTests.test_pass"],
        )
        self.assertEqual(validate_test_manifest(valid, "Q1"), valid)

        extra = dict(valid)
        extra["unexpected"] = True
        with self.assertRaisesRegex(QualificationSuiteError, "schema is not exact"):
            validate_test_manifest(extra, "Q1")

        cross_module = dict(valid)
        cross_module["ordered_test_ids"] = [
            "test_q1_other.ExampleTests.test_pass"
        ]
        with self.assertRaisesRegex(QualificationSuiteError, "coverage is not exact"):
            validate_test_manifest(cross_module, "Q1")

    def test_q0_discovery_never_imports_q1_test_module_or_manifest(self) -> None:
        q0_module = "test_q0_only_fixture"
        q1_module = "test_q1_must_not_import"
        q0_path = self.add_test_module(
            q0_module,
            "import unittest\n"
            "class Q0Tests(unittest.TestCase):\n"
            "    def test_pass(self): self.assertTrue(True)\n",
        )
        q1_path = self.add_test_module(q1_module, "raise RuntimeError('Q1 imported')\n")
        q0_manifest = self.add_manifest(
            "Q0", [q0_module], [f"{q0_module}.Q0Tests.test_pass"]
        )
        q1_manifest = self.add_manifest("Q1", [], [], payload=b"not-json\n")
        vector = self.vector(
            {
                q0_path: "Q0",
                q1_path: "Q1",
                q0_manifest: "Q0",
                q1_manifest: "Q1",
            }
        )

        sys_path_before = list(sys.path)
        suite, manifest, _digest = prepare_node_test_suite(self.root, "Q0", vector)
        self.assertEqual(manifest["test_modules"], [q0_module])
        self.assertEqual(suite.countTestCases(), 1)
        self.assertNotIn(q1_module, sys.modules)
        self.assertEqual(sys.path, sys_path_before)

    def test_extra_discovered_method_fails_closed_against_frozen_ids(self) -> None:
        module = "test_q0_extra_method_fixture"
        test_path = self.add_test_module(
            module,
            "import unittest\n"
            "class ExampleTests(unittest.TestCase):\n"
            "    def test_first(self): pass\n"
            "    def test_second(self): pass\n",
        )
        manifest = self.add_manifest(
            "Q0",
            [module],
            [f"{module}.ExampleTests.test_first"],
        )
        vector = self.vector({test_path: "Q0", manifest: "Q0"})
        sys_path_before = list(sys.path)
        with self.assertRaisesRegex(QualificationSuiteError, "frozen ordered"):
            prepare_node_test_suite(self.root, "Q0", vector)
        self.assertEqual(sys.path, sys_path_before)

    def test_new_owned_module_missing_from_manifest_fails_before_import(self) -> None:
        first = "test_q0_manifested_fixture"
        extra = "test_q0_unmanifested_fixture"
        first_path = self.add_test_module(
            first,
            "import unittest\n"
            "class FirstTests(unittest.TestCase):\n"
            "    def test_pass(self): pass\n",
        )
        extra_path = self.add_test_module(extra, "raise RuntimeError('must not import')\n")
        manifest_path_q0 = self.add_manifest(
            "Q0", [first], [f"{first}.FirstTests.test_pass"]
        )
        vector = self.vector(
            {first_path: "Q0", extra_path: "Q0", manifest_path_q0: "Q0"}
        )
        with self.assertRaisesRegex(QualificationSuiteError, "exact owned test modules"):
            load_node_test_manifest(self.root, "Q0", vector)
        self.assertNotIn(extra, sys.modules)

    def test_manifest_must_be_owned_by_the_node_it_freezes(self) -> None:
        q1_manifest = self.add_manifest("Q1", [], [])
        vector = self.vector({q1_manifest: "Q0"})
        with self.assertRaisesRegex(QualificationSuiteError, "not owned by Q1"):
            load_node_test_manifest(self.root, "Q1", vector)

    def test_nonregular_test_source_is_rejected_before_discovery(self) -> None:
        module = "test_q0_symlink_fixture"
        test_path = self.add_test_module(
            module,
            "import unittest\n"
            "class ExampleTests(unittest.TestCase):\n"
            "    def test_pass(self): pass\n",
        )
        manifest = self.add_manifest(
            "Q0", [module], [f"{module}.ExampleTests.test_pass"]
        )
        vector = self.vector({test_path: "Q0", manifest: "Q0"})
        entry = next(
            value for value in vector["entry_manifest"] if value["path"] == test_path
        )
        entry["kind"] = "symlink"
        entry["git_mode"] = "120000"
        with self.assertRaisesRegex(QualificationSuiteError, "regular Git blob"):
            discover_owned_test_suite(self.root, "Q0", vector)

    def test_empty_future_node_manifest_is_valid_and_discovers_nothing(self) -> None:
        q7_manifest = self.add_manifest("Q7", [], [])
        vector = self.vector({q7_manifest: "Q7"})
        document, _digest = load_node_test_manifest(self.root, "Q7", vector)
        suite, ids, modules = discover_owned_test_suite(self.root, "Q7", vector)
        self.assertEqual(document["ordered_test_ids"], [])
        self.assertEqual(ids, [])
        self.assertEqual(modules, [])
        self.assertEqual(suite.countTestCases(), 0)

    def test_source_bindings_cover_only_consumed_nodes_and_detect_mutation(self) -> None:
        _write(self.root / "q0.txt", "Q0 bytes\n")
        _write(self.root / "q1.txt", "Q1 bytes\n")
        vector = self.vector({"q0.txt": "Q0", "q1.txt": "Q1"})
        q0 = qualification_source_bindings(self.root, vector, ["Q0"])
        self.assertEqual(set(q0), {"q0.txt"})

        _write(self.root / "q1.txt", "dirty downstream bytes\n")
        self.assertEqual(
            qualification_source_bindings(self.root, vector, ["Q0"]), q0
        )
        _write(self.root / "q0.txt", "dirty consumed bytes\n")
        with self.assertRaisesRegex(QualificationSuiteError, "identity differs"):
            qualification_source_bindings(self.root, vector, ["Q0"])

    def test_profiles_and_future_explicit_prefixes_are_fail_closed(self) -> None:
        self.assertEqual(exact_consumed_nodes("Q0", profile="m0"), ("Q0",))
        self.assertEqual(
            exact_consumed_nodes("Q1", profile="flight_capacity_prerequisite"),
            ("Q0", "Q1"),
        )
        self.assertEqual(
            exact_consumed_nodes("Q2", profile="m2_component"),
            ("Q0", "Q1", "Q2"),
        )
        self.assertEqual(
            exact_consumed_nodes("Q3", profile="m3_component"),
            ("Q0", "Q1", "Q2", "Q3"),
        )
        self.assertEqual(
            exact_consumed_nodes("Q4", profile="m4_component"),
            ("Q0", "Q1", "Q2", "Q3", "Q4"),
        )
        self.assertEqual(
            exact_consumed_nodes("Q4", profile="m4_capacity_prerequisite"),
            ("Q0", "Q1", "Q2", "Q3", "Q4"),
        )
        self.assertEqual(
            exact_consumed_nodes(
                "Q4", consumed_nodes=["Q0", "Q1", "Q2", "Q3", "Q4"]
            ),
            ("Q0", "Q1", "Q2", "Q3", "Q4"),
        )
        with self.assertRaisesRegex(QualificationSuiteError, "must consume exactly"):
            exact_consumed_nodes("Q2", consumed_nodes=["Q0", "Q2"])
        with self.assertRaisesRegex(QualificationSuiteError, "override is not exact"):
            exact_consumed_nodes(
                "Q1", profile="m1_component", consumed_nodes=["Q0"]
            )

    def test_nested_test_path_is_rejected_instead_of_silently_omitted(self) -> None:
        _write(
            self.root / "network/tests/nested/test_hidden.py",
            "import unittest\n",
        )
        vector = self.vector({"network/tests/nested/test_hidden.py": "Q0"})
        with self.assertRaisesRegex(QualificationSuiteError, "nested or noncanonical"):
            discover_owned_test_suite(self.root, "Q0", vector)

    def test_loaded_downstream_repository_module_is_rejected(self) -> None:
        test_module = "test_q0_import_scope_fixture"
        downstream_module = "q1_import_scope_dependency_fixture"
        self.created_modules.update({test_module, downstream_module})
        test_path = self.add_test_module(
            test_module,
            f"import {downstream_module}\n"
            "import unittest\n"
            "class ScopeTests(unittest.TestCase):\n"
            "    def test_pass(self): pass\n",
        )
        downstream_path = f"network/tests/{downstream_module}.py"
        _write(self.root / downstream_path, "VALUE = 1\n")
        manifest = self.add_manifest(
            "Q0", [test_module], [f"{test_module}.ScopeTests.test_pass"]
        )
        vector = self.vector(
            {test_path: "Q0", downstream_path: "Q1", manifest: "Q0"}
        )

        prepare_node_test_suite(self.root, "Q0", vector)
        failures = repository_module_scope_failures(self.root, vector, ["Q0"])
        self.assertEqual(len(failures), 1)
        self.assertIn("owned by unconsumed Q1", failures[0])


if __name__ == "__main__":
    unittest.main()
