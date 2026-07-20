#!/usr/bin/env python3
"""Deterministic per-node qualification unittest manifests and runner.

Each tracked ``network/tests/test_*.py`` module belongs to exactly one Q node
through the committed qualification ownership vector.  A node manifest freezes
every test ID discovered from exactly those modules.  Running a later-node
suite never imports or reads an earlier/later node manifest as a discovery
input, so changing Q2 tests cannot silently change the frozen Q0 suite.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import unittest
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, TextIO


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from network.validation.qualification_identity import (  # noqa: E402
    PROFILE_CONSUMED_NODES,
    QUALIFICATION_NODES,
    qualification_content_vector,
    require_qualification_checkout_equal,
)


QUALIFICATION_TEST_MANIFEST_CONTRACT = "ams.qualification-test-manifest/v1"
QUALIFICATION_SUITE_RESULT_CONTRACT = "ams.qualification-suite-result/v1"
QUALIFICATION_TEST_DISCOVERY = {
    "start_directory": "network/tests",
    "pattern": "test_*.py",
}
QUALIFICATION_TEST_MANIFEST_PATHS = {
    "Q0": "network/config/m0_test_manifest.json",
    **{
        f"Q{index}": f"network/config/qualification_test_manifest_q{index}.json"
        for index in range(1, 9)
    },
}
TEST_MODULE = re.compile(r"test_[A-Za-z0-9_]+")
TEST_ID = re.compile(
    r"test_[A-Za-z0-9_]+(?:\.[A-Za-z_][A-Za-z0-9_]*){2,}"
)
SHA256 = re.compile(r"[0-9a-f]{64}")


class QualificationSuiteError(ValueError):
    """A node suite, its manifest, or its source boundary is not exact."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_pretty_json(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _strict_json(payload: bytes, label: str) -> Any:
    def reject_constant(value: str) -> None:
        raise QualificationSuiteError(f"{label} contains non-finite JSON: {value}")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise QualificationSuiteError(
                    f"{label} contains a duplicate JSON key: {key}"
                )
            result[key] = value
        return result

    try:
        return json.loads(
            payload.decode("utf-8", errors="strict"),
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicates,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise QualificationSuiteError(f"{label} is not strict UTF-8 JSON: {exc}") from exc


def _node(value: Any) -> str:
    if not isinstance(value, str) or value not in QUALIFICATION_NODES:
        raise QualificationSuiteError(f"invalid qualification node: {value!r}")
    return value


def manifest_path(node: str) -> str:
    return QUALIFICATION_TEST_MANIFEST_PATHS[_node(node)]


def validate_test_manifest(document: Any, node: str) -> dict[str, Any]:
    node = _node(node)
    expected_keys = {
        "schema_version",
        "contract",
        "node",
        "discovery",
        "test_modules",
        "ordered_test_ids",
    }
    if not isinstance(document, dict) or set(document) != expected_keys:
        raise QualificationSuiteError(f"{node} test manifest schema is not exact")
    modules = document.get("test_modules")
    test_ids = document.get("ordered_test_ids")
    if (
        document.get("schema_version") != 1
        or document.get("contract") != QUALIFICATION_TEST_MANIFEST_CONTRACT
        or document.get("node") != node
        or document.get("discovery") != QUALIFICATION_TEST_DISCOVERY
        or not isinstance(modules, list)
        or modules != sorted(modules)
        or len(modules) != len(set(modules))
        or not all(isinstance(value, str) and TEST_MODULE.fullmatch(value) for value in modules)
        or not isinstance(test_ids, list)
        or test_ids != sorted(test_ids)
        or len(test_ids) != len(set(test_ids))
        or not all(isinstance(value, str) and TEST_ID.fullmatch(value) for value in test_ids)
    ):
        raise QualificationSuiteError(f"{node} test manifest contract is not exact")
    id_modules = {value.split(".", 1)[0] for value in test_ids}
    if id_modules != set(modules):
        raise QualificationSuiteError(
            f"{node} test manifest module/ID coverage is not exact"
        )
    return document


def _vector_assignments(vector: Any) -> dict[str, str]:
    assignments = vector.get("assignments") if isinstance(vector, dict) else None
    manifest = vector.get("entry_manifest") if isinstance(vector, dict) else None
    if (
        not isinstance(assignments, dict)
        or not assignments
        or not all(
            isinstance(path, str) and isinstance(owner, str) and owner in QUALIFICATION_NODES
            for path, owner in assignments.items()
        )
        or not isinstance(manifest, list)
    ):
        raise QualificationSuiteError("qualification vector assignments are malformed")
    manifest_assignments: dict[str, str] = {}
    for entry in manifest:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("path"), str)
            or entry.get("owner") not in QUALIFICATION_NODES
            or entry["path"] in manifest_assignments
        ):
            raise QualificationSuiteError("qualification entry manifest is malformed")
        manifest_assignments[entry["path"]] = entry["owner"]
    if manifest_assignments != assignments:
        raise QualificationSuiteError(
            "qualification assignments differ from the entry manifest"
        )
    return assignments


def owned_test_modules(vector: Any, node: str) -> list[str]:
    """Return the exact direct test modules assigned to one node."""

    node = _node(node)
    assignments = _vector_assignments(vector)
    entries = {
        entry["path"]: entry
        for entry in vector["entry_manifest"]
        if isinstance(entry, dict) and isinstance(entry.get("path"), str)
    }
    modules: list[str] = []
    for relative, owner in assignments.items():
        pure = PurePosixPath(relative)
        if (
            len(pure.parts) >= 3
            and pure.parts[:2] == ("network", "tests")
            and pure.name.startswith("test_")
            and pure.suffix == ".py"
        ):
            if len(pure.parts) != 3 or TEST_MODULE.fullmatch(pure.stem) is None:
                raise QualificationSuiteError(
                    f"nested or noncanonical qualification test path: {relative}"
                )
            entry = entries.get(relative, {})
            if (
                entry.get("kind") != "regular"
                or entry.get("git_mode") not in {"100644", "100755"}
            ):
                raise QualificationSuiteError(
                    f"qualification test source is not a regular Git blob: {relative}"
                )
            if owner == node:
                modules.append(pure.stem)
    if len(modules) != len(set(modules)):
        raise QualificationSuiteError(f"{node} owns duplicate test module names")
    return sorted(modules)


def _manifest_entry(vector: dict[str, Any], relative: str) -> dict[str, Any]:
    entries = vector.get("entry_manifest")
    if not isinstance(entries, list):
        raise QualificationSuiteError("qualification entry manifest is malformed")
    matches = [
        entry
        for entry in entries
        if isinstance(entry, dict) and entry.get("path") == relative
    ]
    if len(matches) != 1:
        raise QualificationSuiteError(
            f"qualification vector does not contain exactly one entry for {relative}"
        )
    return matches[0]


def _regular_payload(path: Path, maximum_bytes: int = 32 * 1024 * 1024) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise QualificationSuiteError(f"cannot open regular file {path}: {exc}") from exc
    chunks: list[bytes] = []
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > maximum_bytes
        ):
            raise QualificationSuiteError(
                f"file is not one bounded single-link regular file: {path}"
            )
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise QualificationSuiteError(f"short read from {path}")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise QualificationSuiteError(f"file grew while reading: {path}")
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    stable = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_nlink",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    if any(getattr(before, field) != getattr(after, field) for field in stable):
        raise QualificationSuiteError(f"file changed while reading: {path}")
    return b"".join(chunks)


def load_node_test_manifest(
    root: Path,
    node: str,
    vector: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    """Load one node manifest and bind it to that node's committed entry."""

    node = _node(node)
    relative = manifest_path(node)
    assignments = _vector_assignments(vector)
    if assignments.get(relative) != node:
        raise QualificationSuiteError(
            f"{node} test manifest path is not owned by {node}: {relative}"
        )
    entry = _manifest_entry(vector, relative)
    if entry.get("kind") != "regular" or entry.get("git_mode") != "100644":
        raise QualificationSuiteError(f"{node} test manifest is not a 100644 Git blob")
    payload = _regular_payload(root / relative)
    digest = hashlib.sha256(payload).hexdigest()
    if digest != entry.get("blob_sha256"):
        raise QualificationSuiteError(
            f"{node} test manifest bytes differ from the qualification vector"
        )
    document = _strict_json(payload, f"{node} test manifest")
    validate_test_manifest(document, node)
    modules = owned_test_modules(vector, node)
    if document["test_modules"] != modules:
        raise QualificationSuiteError(
            f"{node} test manifest does not list its exact owned test modules"
        )
    if payload != canonical_pretty_json(document):
        raise QualificationSuiteError(f"{node} test manifest is not canonical JSON")
    return document, digest


def flatten_suite(suite: unittest.TestSuite) -> Iterator[unittest.TestCase]:
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten_suite(item)
        else:
            yield item


def _module_origin(module: str) -> Path | None:
    loaded = sys.modules.get(module)
    origin = getattr(loaded, "__file__", None) if loaded is not None else None
    if not isinstance(origin, str):
        return None
    path = Path(origin)
    if path.suffix in {".pyc", ".pyo"}:
        try:
            path = Path(importlib.util.source_from_cache(str(path)))
        except ValueError:
            return None
    try:
        return path.resolve(strict=True)
    except OSError:
        return None


def discover_owned_test_suite(
    root: Path,
    node: str,
    vector: dict[str, Any],
) -> tuple[unittest.TestSuite, list[str], list[str]]:
    """Import only tests owned by ``node`` and return their exact ordered IDs."""

    node = _node(node)
    modules = owned_test_modules(vector, node)
    test_root = (root / QUALIFICATION_TEST_DISCOVERY["start_directory"]).resolve(
        strict=True
    )
    combined = unittest.TestSuite()
    discovered_ids: list[str] = []
    loader = unittest.TestLoader()
    sys_path_before = list(sys.path)
    try:
        for module in modules:
            expected_path = (test_root / f"{module}.py").resolve(strict=True)
            preloaded_origin = _module_origin(module)
            if preloaded_origin is not None and preloaded_origin != expected_path:
                raise QualificationSuiteError(
                    f"preloaded test module has the wrong origin: {module}: {preloaded_origin}"
                )
            discovered = loader.discover(
                str(test_root),
                pattern=f"{module}.py",
                top_level_dir=str(test_root),
            )
            module_tests = list(flatten_suite(discovered))
            module_ids = [test.id() for test in module_tests]
            if not module_ids or any(
                value.split(".", 1)[0] != module for value in module_ids
            ):
                raise QualificationSuiteError(
                    f"{node} discovery for {module} is empty, failed, or cross-module"
                )
            if _module_origin(module) != expected_path:
                raise QualificationSuiteError(
                    f"discovered test module has the wrong origin: {module}"
                )
            combined.addTests(discovered)
            discovered_ids.extend(module_ids)
    finally:
        sys.path[:] = sys_path_before
    if (
        discovered_ids != sorted(discovered_ids)
        or len(discovered_ids) != len(set(discovered_ids))
        or not all(TEST_ID.fullmatch(value) for value in discovered_ids)
    ):
        raise QualificationSuiteError(
            f"{node} discovery returned unordered, duplicate, or malformed test IDs"
        )
    if {value.split(".", 1)[0] for value in discovered_ids} != set(modules):
        raise QualificationSuiteError(f"{node} discovery module coverage is not exact")
    return combined, discovered_ids, modules


def prepare_node_test_suite(
    root: Path,
    node: str,
    vector: dict[str, Any],
) -> tuple[unittest.TestSuite, dict[str, Any], str]:
    manifest, digest = load_node_test_manifest(root, node, vector)
    suite, discovered_ids, modules = discover_owned_test_suite(root, node, vector)
    if manifest["test_modules"] != modules:
        raise QualificationSuiteError(f"{node} manifest module list changed during discovery")
    if manifest["ordered_test_ids"] != discovered_ids:
        raise QualificationSuiteError(
            f"{node} discovery differs from its frozen ordered test manifest"
        )
    return suite, manifest, digest


def worktree_test_assignments(root: Path) -> dict[str, str]:
    """Resolve current direct test files through the tracked ownership policy.

    This helper is used only to mechanically regenerate manifests before a
    clean qualification commit.  Formal execution always uses the committed
    vector path above.
    """

    policy_path = root / "network/config/qualification_path_ownership.json"
    policy = _strict_json(
        _regular_payload(policy_path), "worktree qualification ownership policy"
    )
    explicit = policy.get("explicit_owners") if isinstance(policy, dict) else None
    if not isinstance(explicit, dict) or set(explicit) != set(QUALIFICATION_NODES):
        raise QualificationSuiteError("worktree explicit-owner map is not exact")
    explicit_test_owners: dict[str, str] = {}
    for node in QUALIFICATION_NODES:
        paths = explicit.get(node)
        if not isinstance(paths, list) or paths != sorted(paths) or len(paths) != len(set(paths)):
            raise QualificationSuiteError(
                f"worktree explicit-owner list for {node} is not sorted and unique"
            )
        for relative in paths:
            if not isinstance(relative, str):
                raise QualificationSuiteError("worktree explicit owner path is not a string")
            pure = PurePosixPath(relative)
            if (
                len(pure.parts) == 3
                and pure.parts[:2] == ("network", "tests")
                and pure.name.startswith("test_")
                and pure.suffix == ".py"
            ):
                if relative in explicit_test_owners:
                    raise QualificationSuiteError(
                        f"worktree test path has multiple owners: {relative}"
                    )
                explicit_test_owners[relative] = node
    test_root = root / QUALIFICATION_TEST_DISCOVERY["start_directory"]
    direct_candidates = list(test_root.glob("test_*.py"))
    unsafe_direct: list[Path] = []
    actual: set[str] = set()
    for path in direct_candidates:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            unsafe_direct.append(path)
        else:
            actual.add(path.relative_to(root).as_posix())
    if unsafe_direct:
        raise QualificationSuiteError(
            "qualification test paths must be single-link regular files: "
            + ", ".join(
                path.relative_to(root).as_posix() for path in sorted(unsafe_direct)
            )
        )
    nested = [path for path in test_root.rglob("test_*.py") if path.parent != test_root]
    if nested:
        raise QualificationSuiteError(
            "nested qualification test paths are forbidden: "
            + ", ".join(path.relative_to(root).as_posix() for path in sorted(nested))
        )
    missing = set(explicit_test_owners) - actual
    if missing:
        raise QualificationSuiteError(
            "explicitly owned worktree tests are absent: " + ", ".join(sorted(missing))
        )
    return {
        relative: explicit_test_owners.get(relative, "Q0")
        for relative in sorted(actual)
    }


def generate_node_test_manifests(root: Path) -> dict[str, Any]:
    """Mechanically rewrite all nine node manifests from the current worktree."""

    root = root.resolve(strict=True)
    assignments = worktree_test_assignments(root)
    synthetic_vector = {
        "assignments": assignments,
        "entry_manifest": [
            {
                "path": relative,
                "owner": owner,
                "kind": "regular",
                "git_mode": (
                    "100755"
                    if (root / relative).stat().st_mode & 0o111
                    else "100644"
                ),
            }
            for relative, owner in assignments.items()
        ],
    }
    summary: dict[str, Any] = {}
    documents: dict[str, dict[str, Any]] = {}
    for node in QUALIFICATION_NODES:
        _suite, test_ids, modules = discover_owned_test_suite(
            root, node, synthetic_vector
        )
        document = {
            "schema_version": 1,
            "contract": QUALIFICATION_TEST_MANIFEST_CONTRACT,
            "node": node,
            "discovery": dict(QUALIFICATION_TEST_DISCOVERY),
            "test_modules": modules,
            "ordered_test_ids": test_ids,
        }
        validate_test_manifest(document, node)
        documents[node] = document
        summary[node] = {
            "manifest_path": manifest_path(node),
            "test_module_count": len(modules),
            "ordered_test_count": len(test_ids),
        }
    if not documents["Q0"]["ordered_test_ids"]:
        raise QualificationSuiteError("generated Q0 manifest would be empty")
    for node, document in documents.items():
        path = root / manifest_path(node)
        temporary = path.with_name(f".{path.name}.qualification-suite.tmp")
        if temporary.exists():
            raise QualificationSuiteError(f"manifest temporary path already exists: {temporary}")
        try:
            with temporary.open("xb") as output:
                output.write(canonical_pretty_json(document))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()
    return summary


def exact_consumed_nodes(
    node: str,
    *,
    profile: str | None = None,
    consumed_nodes: list[str] | tuple[str, ...] | None = None,
) -> tuple[str, ...]:
    node = _node(node)
    if profile is not None:
        expected = PROFILE_CONSUMED_NODES.get(profile)
        if expected is None:
            raise QualificationSuiteError(f"unknown qualification profile: {profile!r}")
        if consumed_nodes not in (None, [], (), expected, list(expected)):
            raise QualificationSuiteError(
                f"profile {profile} consumed-node override is not exact"
            )
        resolved = expected
    else:
        if not isinstance(consumed_nodes, (list, tuple)):
            raise QualificationSuiteError("explicit consumed nodes are required without a profile")
        resolved = tuple(consumed_nodes)
    target_index = int(node[1:])
    required_prefix = QUALIFICATION_NODES[: target_index + 1]
    if tuple(resolved) != required_prefix:
        raise QualificationSuiteError(
            f"{node} suite must consume exactly {list(required_prefix)}"
        )
    return tuple(resolved)


def _safe_tracked_path(root: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise QualificationSuiteError(f"unsafe tracked path: {relative!r}")
    current = root
    for part in pure.parts[:-1]:
        current = current / part
        info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise QualificationSuiteError(
                f"tracked path has a non-directory/symlink parent: {relative}"
            )
    return root.joinpath(*pure.parts)


def qualification_source_bindings(
    root: Path,
    vector: dict[str, Any],
    consumed_nodes: list[str] | tuple[str, ...],
) -> dict[str, str]:
    """Re-hash exactly the committed entries in the consumed Q nodes."""

    nodes = tuple(consumed_nodes)
    if (
        not nodes
        or len(nodes) != len(set(nodes))
        or any(node not in QUALIFICATION_NODES for node in nodes)
    ):
        raise QualificationSuiteError("consumed source-binding nodes are malformed")
    assignments = _vector_assignments(vector)
    entries = vector["entry_manifest"]
    result: dict[str, str] = {}
    for entry in entries:
        relative = entry["path"]
        if assignments[relative] not in nodes:
            continue
        path = _safe_tracked_path(root, relative)
        kind = entry.get("kind")
        if kind == "regular":
            payload = _regular_payload(path, maximum_bytes=512 * 1024 * 1024)
            digest = hashlib.sha256(payload).hexdigest()
            info = path.lstat()
            expected_executable = entry.get("git_mode") == "100755"
            if (
                digest != entry.get("blob_sha256")
                or len(payload) != entry.get("blob_size")
                or bool(info.st_mode & 0o111) != expected_executable
            ):
                raise QualificationSuiteError(
                    f"consumed regular-file identity differs: {relative}"
                )
            result[relative] = digest
        elif kind == "symlink":
            info = path.lstat()
            if not stat.S_ISLNK(info.st_mode):
                raise QualificationSuiteError(f"consumed symlink type differs: {relative}")
            target = os.readlink(os.fsencode(path))
            digest = hashlib.sha256(target).hexdigest()
            if digest != entry.get("blob_sha256") or len(target) != entry.get("blob_size"):
                raise QualificationSuiteError(
                    f"consumed symlink identity differs: {relative}"
                )
            result[relative] = digest
        elif kind == "gitlink":
            completed = subprocess.run(
                [
                    "/usr/bin/git",
                    "-c",
                    f"safe.directory={path.resolve(strict=True)}",
                    "-C",
                    str(path),
                    "rev-parse",
                    "HEAD",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            object_id = completed.stdout.strip()
            if completed.returncode != 0 or object_id != entry.get("git_object_id"):
                raise QualificationSuiteError(
                    f"consumed gitlink identity differs: {relative}"
                )
            result[relative] = hashlib.sha256(
                b"gitlink\0" + object_id.encode("ascii")
            ).hexdigest()
        else:
            raise QualificationSuiteError(f"unsupported consumed entry kind: {kind!r}")
    expected = {
        relative for relative, owner in assignments.items() if owner in nodes
    }
    if set(result) != expected:
        raise QualificationSuiteError(
            "source bindings do not cover exactly the consumed qualification nodes"
        )
    return dict(sorted(result.items()))


def repository_module_scope_failures(
    root: Path,
    vector: dict[str, Any],
    consumed_nodes: list[str] | tuple[str, ...],
) -> list[str]:
    """Report loaded repository Python modules outside the consumed Q prefix.

    The source binding protects bytes, while this check protects the execution
    boundary: a Q0/Q1 test cannot import a Python implementation owned by a
    later node and still claim that only the earlier-node prefix was tested.
    """

    root = root.resolve(strict=True)
    nodes = tuple(consumed_nodes)
    if (
        not nodes
        or len(nodes) != len(set(nodes))
        or any(node not in QUALIFICATION_NODES for node in nodes)
    ):
        raise QualificationSuiteError("consumed module-scope nodes are malformed")
    assignments = _vector_assignments(vector)
    failures: set[str] = set()
    for module_name, module in sorted(sys.modules.items()):
        origin = getattr(module, "__file__", None)
        if not isinstance(origin, str):
            continue
        path = _module_origin(module_name)
        if path is None:
            continue
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            continue
        owner = assignments.get(relative)
        if owner is None:
            failures.add(
                f"loaded repository module is absent from the qualification vector: "
                f"{module_name}={relative}"
            )
        elif owner not in nodes:
            failures.add(
                f"loaded repository module {module_name} is owned by unconsumed "
                f"{owner}: {relative}"
            )
    return sorted(failures)


class RecordingResult(unittest.TextTestResult):
    def startTest(self, test: unittest.TestCase) -> None:  # noqa: N802
        self.outcomes[test.id()] = "not_completed"
        super().startTest(test)

    def addSuccess(self, test: unittest.TestCase) -> None:  # noqa: N802
        self.outcomes[test.id()] = "passed"
        super().addSuccess(test)

    def addFailure(self, test: unittest.TestCase, err: Any) -> None:  # noqa: N802
        self.outcomes[test.id()] = "failed"
        super().addFailure(test, err)

    def addError(self, test: unittest.TestCase, err: Any) -> None:  # noqa: N802
        self.outcomes[test.id()] = "error"
        super().addError(test, err)

    def addSkip(self, test: unittest.TestCase, reason: str) -> None:  # noqa: N802
        self.outcomes[test.id()] = "skipped"
        super().addSkip(test, reason)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.outcomes: dict[str, str] = {}
        super().__init__(*args, **kwargs)


def run_node_test_suite(
    root: Path,
    node: str,
    *,
    profile: str | None,
    consumed_nodes: list[str] | tuple[str, ...] | None,
    stream: TextIO,
) -> dict[str, Any]:
    root = root.resolve(strict=True)
    nodes = exact_consumed_nodes(
        node,
        profile=profile,
        consumed_nodes=consumed_nodes,
    )
    vector = qualification_content_vector(root, "HEAD")
    require_qualification_checkout_equal(root, vector["git_commit"])
    bindings_before = qualification_source_bindings(root, vector, nodes)
    suite, manifest, manifest_sha256 = prepare_node_test_suite(root, node, vector)
    discovery_scope_failures = repository_module_scope_failures(root, vector, nodes)
    if discovery_scope_failures:
        raise QualificationSuiteError("; ".join(discovery_scope_failures))
    with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
        runner = unittest.TextTestRunner(
            stream=stream,
            verbosity=2,
            buffer=True,
            failfast=False,
            resultclass=RecordingResult,
        )
        result = runner.run(suite)
    module_scope_failures = repository_module_scope_failures(root, vector, nodes)
    bindings_after = qualification_source_bindings(root, vector, nodes)
    require_qualification_checkout_equal(root, vector["git_commit"])
    ordered_ids = manifest["ordered_test_ids"]
    outcomes = [
        {"test_id": test_id, "outcome": result.outcomes.get(test_id, "not_completed")}
        for test_id in ordered_ids
    ]
    passed = (
        result.wasSuccessful()
        and bool(ordered_ids)
        and bindings_before == bindings_after
        and not module_scope_failures
        and all(record["outcome"] == "passed" for record in outcomes)
    )
    return {
        "schema_version": 1,
        "contract": QUALIFICATION_SUITE_RESULT_CONTRACT,
        "node": node,
        "profile": profile,
        "consumed_nodes": list(nodes),
        "manifest_path": manifest_path(node),
        "manifest_sha256": manifest_sha256,
        "test_modules": list(manifest["test_modules"]),
        "ordered_test_ids": list(ordered_ids),
        "outcomes": outcomes,
        "source_binding_count": len(bindings_before),
        "source_bindings_sha256": hashlib.sha256(
            canonical_json(bindings_before)
        ).hexdigest(),
        "source_bindings_stable": bindings_before == bindings_after,
        "repository_module_scope_failures": module_scope_failures,
        "qualification_vector_sha256": vector["vector_sha256"],
        "passed": passed,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", choices=QUALIFICATION_NODES)
    parser.add_argument(
        "--qualification-profile",
        choices=sorted(PROFILE_CONSUMED_NODES),
    )
    parser.add_argument(
        "--consumed-node",
        action="append",
        default=[],
        choices=QUALIFICATION_NODES,
    )
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--generate-manifests", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.generate_manifests:
        if (
            args.node is not None
            or args.qualification_profile is not None
            or args.consumed_node
            or args.run_dir is not None
        ):
            raise ValueError("--generate-manifests cannot be combined with run options")
        summary = generate_node_test_manifests(ROOT_DIR)
        sys.stdout.buffer.write(canonical_pretty_json(summary))
        return 0
    if args.node is None:
        raise ValueError("--node is required when running a qualification suite")
    if args.run_dir is None:
        stream: TextIO = sys.stderr
        result_path: Path | None = None
    else:
        logs = args.run_dir / "logs"
        metrics = args.run_dir / "metrics"
        logs.mkdir(parents=True, exist_ok=True)
        metrics.mkdir(parents=True, exist_ok=True)
        result_path = metrics / f"qualification_suite_{args.node.lower()}.json"
        if result_path.exists():
            raise FileExistsError(f"qualification result already exists: {result_path}")
        stream = (logs / f"qualification_suite_{args.node.lower()}.log").open(
            "x", encoding="utf-8", buffering=1
        )
    try:
        result = run_node_test_suite(
            ROOT_DIR,
            args.node,
            profile=args.qualification_profile,
            consumed_nodes=args.consumed_node,
            stream=stream,
        )
    except Exception as exc:
        result = {
            "schema_version": 1,
            "contract": QUALIFICATION_SUITE_RESULT_CONTRACT,
            "node": args.node,
            "passed": False,
            "failures": [str(exc)],
        }
    finally:
        if args.run_dir is not None:
            stream.close()
    payload = canonical_pretty_json(result)
    if result_path is None:
        sys.stdout.buffer.write(payload)
    else:
        with result_path.open("xb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    return 0 if result.get("passed") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
