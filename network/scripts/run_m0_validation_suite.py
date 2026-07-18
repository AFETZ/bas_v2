#!/usr/bin/env python3
"""Run and record the complete M0 validation/adversarial unittest suite.

The ordinary unittest success banner is retained as raw evidence, while a
bounded JSON result records every discovered test ID and its outcome.  The M0
validator deliberately re-derives success from both records; the producer's
``passed`` field is only an observation.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.metadata
import json
import os
import re
import stat
import subprocess
import sys
import time
import traceback
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


ROOT_DIR = Path(__file__).resolve().parents[2]
SAFE_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
IMAGE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
M0_TEST_MANIFEST_LOCK_PATH = "network/config/dependency_lock.yaml"
M0_SOURCE_MODE = "clean_git_clone_ro"
M0_OVERLAY_MODE = "none_q0_source_only"
M0_IMPORT_TRACE_CONTRACT = "ams.m0.python-import-trace/v1"

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from network.validation.qualification_identity import (  # noqa: E402
    MUTABLE_STATUS_OUTPUTS as _MUTABLE_STATUS_OUTPUTS,
    qualification_content_vector,
    require_qualification_checkout_equal,
)
from network.scripts.qualification_suite import (  # noqa: E402
    QUALIFICATION_TEST_MANIFEST_PATHS,
    discover_owned_test_suite,
    load_node_test_manifest,
    qualification_source_bindings,
)


M0_TEST_MANIFEST_PATH = QUALIFICATION_TEST_MANIFEST_PATHS["Q0"]


# Preserve the public set used by the independent M0 validator while keeping
# the exact three-path definition centralized in qualification_identity.
MUTABLE_STATUS_OUTPUTS = set(_MUTABLE_STATUS_OUTPUTS)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_file(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError(f"not a single-link regular file: {path}")
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError(f"short read while hashing: {path}")
            digest.update(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ValueError(f"file grew while hashing: {path}")
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    stable = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")
    if any(getattr(before, name) != getattr(after, name) for name in stable):
        raise ValueError(f"file changed while hashing: {path}")
    return digest.hexdigest()


def _safe_run_dir(candidate: Path) -> Path:
    run_root = Path(os.environ.get("AMS_M0_ARTIFACT_ROOT", str(ROOT_DIR / "runs")))
    if run_root.is_symlink() or not run_root.is_dir():
        raise ValueError("runs directory must be an existing non-symlink directory")
    if candidate.is_symlink():
        raise ValueError("run directory must not be a symbolic link")
    resolved_root = run_root.resolve(strict=True)
    resolved = candidate.resolve(strict=True)
    if Path.cwd().resolve(strict=True) != ROOT_DIR:
        raise ValueError("M0 validation suite must run from the repository root")
    if resolved.parent != resolved_root or SAFE_RUN_ID.fullmatch(resolved.name) is None:
        raise ValueError("run directory must be one safe direct child of runs")
    for name in ("logs", "metrics"):
        directory = resolved / name
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or directory.is_symlink():
            raise ValueError(f"{name} must be a non-symlink directory")
    return resolved


def suite_source_bindings(root: Path = ROOT_DIR) -> dict[str, str]:
    """Re-hash exactly the committed Q0 closure consumed by formal M0."""

    vector = qualification_content_vector(root, "HEAD")
    return qualification_source_bindings(root, vector, ("Q0",))


def _tree_hash(path: Path) -> tuple[int, str]:
    excluded = {"build", "cmake-cache", "scratch", "__pycache__", ".vscode"}
    files: list[Path] = []
    for candidate in path.rglob("*"):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        relative = candidate.relative_to(path)
        if relative.parts[:2] == ("src", "lorawan"):
            continue
        if any(part in excluded for part in relative.parts):
            continue
        if relative.name.startswith(".lock-") or candidate.suffix in {".pyc", ".pyo"}:
            continue
        files.append(candidate)
    digest = hashlib.sha256()
    for candidate in sorted(files, key=lambda value: value.relative_to(path).as_posix()):
        relative = candidate.relative_to(path).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(bytes.fromhex(_sha256_file(candidate)))
    return len(files), digest.hexdigest()


def suite_external_bindings(root: Path = ROOT_DIR) -> dict[str, dict[str, Any]]:
    ns3 = root / ".external/ns-3"
    if not ns3.is_dir() or ns3.is_symlink():
        return {}
    count, digest = _tree_hash(ns3)
    return {
        "ns3_core": {
            "path": ".external/ns-3",
            "file_count": count,
            "tree_sha256": digest,
        }
    }


def load_frozen_test_manifest(root: Path = ROOT_DIR) -> tuple[dict[str, Any], str]:
    vector = qualification_content_vector(root, "HEAD")
    document, digest = load_node_test_manifest(root, "Q0", vector)
    if not document["ordered_test_ids"]:
        raise ValueError("frozen M0 test manifest is empty")
    lock_path = root / M0_TEST_MANIFEST_LOCK_PATH
    lock = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
    binding = lock.get("m0_test_manifest") if isinstance(lock, dict) else None
    if binding != {
        "path": M0_TEST_MANIFEST_PATH,
        "sha256": digest,
        "ordered_test_count": len(document["ordered_test_ids"]),
    }:
        raise ValueError("frozen M0 test manifest is not exactly bound by dependency lock")
    return document, digest


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def load_m0_import_policy(root: Path = ROOT_DIR) -> tuple[dict[str, Any], str]:
    lock = yaml.safe_load(
        (root / M0_TEST_MANIFEST_LOCK_PATH).read_text(encoding="utf-8")
    )
    policy = lock.get("m0_python_import_policy") if isinstance(lock, dict) else None
    if not isinstance(policy, dict) or policy.get("schema_version") != 1:
        raise ValueError("M0 Python import policy is missing or malformed")
    return policy, hashlib.sha256(_canonical_json(policy)).hexdigest()


def expected_m0_sys_path(policy: dict[str, Any], run_id: str) -> list[str]:
    base = policy.get("exact_base_pythonpath")
    suffix = policy.get("interpreter_suffix")
    template = policy.get("overlay_pythonpath_template")
    if (
        policy.get("mode") != "isolated_explicit_path"
        or policy.get("parent_flags") != ["-S"]
        or not isinstance(base, list)
        or not isinstance(suffix, list)
        or not isinstance(template, str)
    ):
        raise ValueError("M0 Python import policy is not the exact isolated mode")
    overlay = template.replace("{run_id}", run_id)
    if "{" in overlay or "}" in overlay:
        raise ValueError("M0 Python overlay path template is unresolved")
    return [
        "/workspace/multiagent_simulation/network/scripts",
        overlay,
        *base,
        *suffix,
    ]


def _pth_inventory(policy: dict[str, Any], run_id: str) -> list[dict[str, Any]]:
    roots = expected_m0_sys_path(policy, run_id)[1:-len(policy["interpreter_suffix"])]
    records: list[dict[str, Any]] = []
    for root_text in roots:
        root = Path(root_text)
        if not root.is_dir() or root.is_symlink():
            continue
        for path in sorted(root.glob("*.pth")):
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError(f"M0 .pth inventory contains a non-regular file: {path}")
            records.append(
                {
                    "path": str(path),
                    "bytes": info.st_size,
                    "sha256": _sha256_file(path),
                    "processed": False,
                }
            )
    return sorted(records, key=lambda record: record["path"])


def _module_import_records(
    policy: dict[str, Any], run_id: str
) -> list[dict[str, Any]]:
    expected_path = expected_m0_sys_path(policy, run_id)
    allowed_root_texts = [
        "/workspace/multiagent_simulation",
        expected_path[1],
        *policy["exact_base_pythonpath"],
        "/usr/lib/python3.10",
        "/usr/lib/python3.10/lib-dynload",
    ]
    allowed_roots: list[tuple[str, Path]] = []
    for value in allowed_root_texts:
        path = Path(value)
        if path.exists():
            allowed_roots.append((value, path.resolve(strict=True)))
    distributions = importlib.metadata.packages_distributions()
    records: list[dict[str, Any]] = []
    for name, module in sorted(sys.modules.items()):
        if module is None:
            continue
        spec = getattr(module, "__spec__", None)
        origin = getattr(spec, "origin", None)
        file_value = getattr(module, "__file__", None)
        top_level = name.partition(".")[0]
        owners = sorted(distributions.get(top_level, []))
        if origin in {"built-in", "frozen"}:
            records.append(
                {
                    "name": name,
                    "kind": origin,
                    "origin": origin,
                    "distributions": owners,
                }
            )
            continue
        if not isinstance(file_value, str):
            records.append(
                {
                    "name": name,
                    "kind": "namespace",
                    "origin": None,
                    "distributions": owners,
                }
            )
            continue
        path = Path(file_value)
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"loaded module origin is unavailable: {name}: {path}") from exc
        selected: tuple[str, Path] | None = None
        for raw_root, resolved_root in sorted(
            allowed_roots, key=lambda item: len(str(item[1])), reverse=True
        ):
            try:
                resolved.relative_to(resolved_root)
                selected = (raw_root, resolved_root)
                break
            except ValueError:
                continue
        if selected is None:
            raise ValueError(f"loaded module is outside the locked import roots: {name}: {resolved}")
        info = resolved.stat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"loaded module origin is not regular: {name}: {resolved}")
        raw_root = selected[0]
        if raw_root == "/workspace/multiagent_simulation":
            source_kind = "committed_source"
        elif raw_root == expected_path[1]:
            source_kind = "fresh_overlay"
        else:
            source_kind = "immutable_image"
        records.append(
            {
                "name": name,
                "kind": "file",
                "origin": file_value,
                "resolved_path": str(resolved),
                "allowed_root": raw_root,
                "source_kind": source_kind,
                "bytes": info.st_size,
                "sha256": _sha256_file(resolved),
                "distributions": owners,
            }
        )
    if not records or len({record["name"] for record in records}) != len(records):
        raise ValueError("M0 loaded-module trace is empty or duplicate")
    return records


def build_m0_import_trace(
    policy: dict[str, Any],
    policy_sha256: str,
    run_id: str,
    *,
    sys_path_before: list[str],
    pth_before: list[dict[str, Any]],
) -> dict[str, Any]:
    expected_path = expected_m0_sys_path(policy, run_id)
    sys_path_after = list(sys.path)
    pth_after = _pth_inventory(policy, run_id)
    if sys_path_before != expected_path or sys_path_after != expected_path:
        raise ValueError("M0 Python sys.path differs from the exact locked order")
    if pth_before != pth_after:
        raise ValueError("M0 .pth inventory changed during collection/execution")
    cleared = policy.get("cleared_environment")
    if not isinstance(cleared, list) or any(name in os.environ for name in cleared):
        raise ValueError("M0 inherited Python/plugin environment was not cleared")
    expected_pycache = str(policy.get("bytecode_root_template", "")).replace(
        "{run_id}", run_id
    )
    if (
        os.environ.get("PYTHONNOUSERSITE") != "1"
        or sys.pycache_prefix != expected_pycache
        or sys.flags.no_site != 1
        or "sitecustomize" in sys.modules
        or "usercustomize" in sys.modules
    ):
        raise ValueError("M0 parent interpreter isolation differs from import policy")
    modules = _module_import_records(policy, run_id)
    return {
        "schema_version": 1,
        "contract": M0_IMPORT_TRACE_CONTRACT,
        "policy_path": "network/config/dependency_lock.yaml#m0_python_import_policy",
        "policy_sha256": policy_sha256,
        "mode": "isolated_explicit_path",
        "sys_path_before": sys_path_before,
        "sys_path_after": sys_path_after,
        "pth_inventory_before": pth_before,
        "pth_inventory_after": pth_after,
        "customization": {
            "parent_sitecustomize_loaded": False,
            "parent_usercustomize_loaded": False,
            "child_guard_path": policy["customization"]["child_guard_path"],
            "child_guard_sha256": policy["customization"]["child_guard_sha256"],
        },
        "environment": {
            "cleared": sorted(cleared),
            "python_no_user_site": True,
            "bytecode_root": expected_pycache,
            "pytest_plugin_autoload": False,
        },
        "modules": modules,
        "module_count": len(modules),
        "modules_sha256": hashlib.sha256(_canonical_json(modules)).hexdigest(),
    }


def validate_m0_import_trace_record(
    record: Any,
    policy: dict[str, Any],
    policy_sha256: str,
    run_id: str,
    source_bindings: dict[str, str],
) -> list[str]:
    """Independently validate a persisted trace without trusting PASS fields."""

    failures: list[str] = []
    expected_keys = {
        "schema_version",
        "contract",
        "policy_path",
        "policy_sha256",
        "mode",
        "sys_path_before",
        "sys_path_after",
        "pth_inventory_before",
        "pth_inventory_after",
        "customization",
        "environment",
        "modules",
        "module_count",
        "modules_sha256",
    }
    if not isinstance(record, dict) or set(record) != expected_keys:
        return ["M0 Python import trace schema is not exact"]
    expected_path = expected_m0_sys_path(policy, run_id)
    if (
        record.get("schema_version") != 1
        or record.get("contract") != M0_IMPORT_TRACE_CONTRACT
        or record.get("policy_path")
        != "network/config/dependency_lock.yaml#m0_python_import_policy"
        or record.get("policy_sha256") != policy_sha256
        or record.get("mode") != "isolated_explicit_path"
        or record.get("sys_path_before") != expected_path
        or record.get("sys_path_after") != expected_path
    ):
        failures.append("M0 Python import trace policy/path identity is invalid")
    pth = record.get("pth_inventory_before")
    if pth != record.get("pth_inventory_after") or not isinstance(pth, list):
        failures.append("M0 .pth inventory is malformed or changed")
    else:
        pth_paths: list[str] = []
        for item in pth:
            if (
                not isinstance(item, dict)
                or set(item) != {"path", "bytes", "sha256", "processed"}
                or not isinstance(item.get("path"), str)
                or isinstance(item.get("bytes"), bool)
                or not isinstance(item.get("bytes"), int)
                or item.get("bytes", -1) < 0
                or re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256") or ""))
                is None
                or item.get("processed") is not False
            ):
                failures.append("M0 .pth inventory entry is invalid")
                continue
            pth_paths.append(item["path"])
        if pth_paths != sorted(pth_paths) or len(pth_paths) != len(set(pth_paths)):
            failures.append("M0 .pth inventory paths are unordered or duplicate")
    if record.get("customization") != {
        "parent_sitecustomize_loaded": False,
        "parent_usercustomize_loaded": False,
        "child_guard_path": policy.get("customization", {}).get("child_guard_path"),
        "child_guard_sha256": policy.get("customization", {}).get(
            "child_guard_sha256"
        ),
    }:
        failures.append("M0 Python customization trace differs from policy")
    expected_environment = {
        "cleared": sorted(policy.get("cleared_environment", [])),
        "python_no_user_site": True,
        "bytecode_root": str(policy.get("bytecode_root_template", "")).replace(
            "{run_id}", run_id
        ),
        "pytest_plugin_autoload": False,
    }
    if record.get("environment") != expected_environment:
        failures.append("M0 Python/plugin environment trace differs from policy")
    modules = record.get("modules")
    if (
        not isinstance(modules, list)
        or not modules
        or record.get("module_count") != len(modules)
        or record.get("modules_sha256")
        != hashlib.sha256(_canonical_json(modules)).hexdigest()
    ):
        failures.append("M0 loaded-module aggregate identity is invalid")
        return failures
    names: list[str] = []
    allowed_roots = {
        "/workspace/multiagent_simulation",
        expected_path[1],
        *policy.get("exact_base_pythonpath", []),
        "/usr/lib/python3.10",
        "/usr/lib/python3.10/lib-dynload",
    }
    for module in modules:
        if not isinstance(module, dict) or not isinstance(module.get("name"), str):
            failures.append("M0 loaded-module record is malformed")
            continue
        names.append(module["name"])
        kind = module.get("kind")
        if kind in {"built-in", "frozen"}:
            if set(module) != {"name", "kind", "origin", "distributions"}:
                failures.append("M0 built-in/frozen module record is not exact")
        elif kind == "namespace":
            if (
                set(module) != {"name", "kind", "origin", "distributions"}
                or module.get("origin") is not None
            ):
                failures.append("M0 namespace module record is not exact")
        elif kind == "file":
            exact = {
                "name",
                "kind",
                "origin",
                "resolved_path",
                "allowed_root",
                "source_kind",
                "bytes",
                "sha256",
                "distributions",
            }
            root_text = module.get("allowed_root")
            path_text = module.get("resolved_path")
            if (
                set(module) != exact
                or root_text not in allowed_roots
                or not isinstance(path_text, str)
                or not path_text.startswith(str(root_text).rstrip("/") + "/")
                or isinstance(module.get("bytes"), bool)
                or not isinstance(module.get("bytes"), int)
                or module.get("bytes", -1) < 1
                or re.fullmatch(r"[0-9a-f]{64}", str(module.get("sha256") or ""))
                is None
                or not isinstance(module.get("distributions"), list)
                or module.get("distributions") != sorted(module.get("distributions"))
            ):
                failures.append(f"M0 file-module record is invalid: {module.get('name')}")
                continue
            if root_text == "/workspace/multiagent_simulation":
                relative = path_text.removeprefix(
                    "/workspace/multiagent_simulation/"
                )
                if (
                    module.get("source_kind") != "committed_source"
                    or source_bindings.get(relative) != module.get("sha256")
                ):
                    failures.append(
                        f"M0 source module is not bound by committed Q0 bytes: {module.get('name')}"
                    )
            elif root_text == expected_path[1]:
                if module.get("source_kind") != "fresh_overlay":
                    failures.append("M0 overlay module classification is invalid")
            elif module.get("source_kind") != "immutable_image":
                failures.append("M0 image module classification is invalid")
        else:
            failures.append("M0 loaded-module kind is invalid")
    if names != sorted(names) or len(names) != len(set(names)):
        failures.append("M0 loaded-module names are unordered or duplicate")
    return failures


def _require_discovered_test_sources(
    test_ids: list[str], source_bindings: dict[str, str]
) -> None:
    for test_id in test_ids:
        module = test_id.split(".", 1)[0]
        if re.fullmatch(r"test_[A-Za-z0-9_]+", module) is None:
            raise ValueError(f"non-canonical discovered test module: {module}")
        relative = f"network/tests/{module}.py"
        if relative not in source_bindings:
            raise ValueError(f"discovered test source is not pre-bound: {relative}")


def _read_execution_identity() -> dict[str, Any]:
    image_digest = os.environ.get("AMS_CONTAINER_IMAGE_DIGEST", "")
    digest_source = os.environ.get("AMS_CONTAINER_IMAGE_DIGEST_SOURCE", "")
    identity_file_value = os.environ.get("AMS_RUNTIME_CONTAINER_ID_FILE", "")
    if IMAGE_DIGEST.fullmatch(image_digest) is None:
        raise ValueError("exact AMS_CONTAINER_IMAGE_DIGEST is unavailable")
    if digest_source != "docker_image_inspect_host":
        raise ValueError("container image digest is not host-inspected")
    if not identity_file_value:
        raise ValueError("AMS_RUNTIME_CONTAINER_ID_FILE is unavailable")
    identity_file = Path(identity_file_value)
    info = identity_file.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 128:
        raise ValueError("runtime container identity is not a bounded regular file")
    container_id = identity_file.read_text(encoding="ascii").strip()
    if CONTAINER_ID.fullmatch(container_id) is None:
        raise ValueError("runtime container identity is not a full container ID")
    source_mode = os.environ.get("AMS_M0_SOURCE_MODE", "")
    source_commit = os.environ.get("AMS_M0_SOURCE_COMMIT", "")
    overlay_mode = os.environ.get("AMS_M0_PROJECT_OVERLAY_MODE", "")
    if source_mode != M0_SOURCE_MODE or overlay_mode != M0_OVERLAY_MODE:
        raise ValueError("M0 did not execute from the immutable Q0-only snapshot path")
    if re.fullmatch(r"[0-9a-f]{40}", source_commit) is None:
        raise ValueError("M0 immutable source commit is malformed")
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT_DIR, capture_output=True, text=True,
        check=False, timeout=10,
    )
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=ROOT_DIR, capture_output=True, text=True, check=False, timeout=10,
    )
    mount = subprocess.run(
        ["findmnt", "-n", "-o", "OPTIONS", "-T", str(ROOT_DIR)],
        capture_output=True, text=True, check=False, timeout=10,
    )
    mount_options = mount.stdout.strip().split(",") if mount.returncode == 0 else []
    if commit.returncode != 0 or commit.stdout.strip() != source_commit or status.stdout:
        raise ValueError("M0 snapshot is not the exact clean source commit")
    if "ro" not in mount_options:
        raise ValueError("M0 source snapshot is not mounted read-only")
    pycache_prefix = sys.pycache_prefix
    if (
        sys.flags.no_site != 1
        or "sitecustomize" in sys.modules
        or "usercustomize" in sys.modules
        or not isinstance(pycache_prefix, str)
        or not pycache_prefix.startswith("/tmp/ams-m0-pycache-")
    ):
        raise ValueError("M0 Python is not isolated from site/customization/cache inputs")
    forbidden = {
        str(ROOT_DIR / "build"),
        str(ROOT_DIR / "install"),
        str(ROOT_DIR / "log"),
    }
    if any(value == prefix or value.startswith(prefix + "/") for value in sys.path for prefix in forbidden):
        raise ValueError("M0 Python path contains a live project build/install output")
    guard_root = Path(os.environ.get("AMS_M0_PYTHON_GUARD", ""))
    expected_guard = ROOT_DIR / "network/scripts/m0_python_guard"
    if guard_root != expected_guard or guard_root.resolve(strict=True) != expected_guard:
        raise ValueError("M0 child-Python guard is not the tracked immutable directory")
    guard_script = guard_root / "sitecustomize.py"
    trace_code = """
import json
import pathlib
import sys
import sitecustomize
print(json.dumps({
    "guard_marker": getattr(sitecustomize, "AMS_M0_INERT_SITECUSTOMIZE", False),
    "no_site": sys.flags.no_site,
    "sitecustomize_path": str(pathlib.Path(sitecustomize.__file__).resolve()),
    "usercustomize_loaded": "usercustomize" in sys.modules,
}, sort_keys=True))
""".strip()
    trace = subprocess.run(
        [sys.executable, "-c", trace_code],
        cwd=ROOT_DIR,
        env=dict(os.environ),
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    try:
        child_guard = json.loads(trace.stdout)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("M0 child-Python guard trace is not strict JSON") from exc
    expected_child_guard = {
        "guard_marker": True,
        "no_site": 0,
        "sitecustomize_path": str(guard_script),
        "usercustomize_loaded": False,
    }
    if trace.returncode != 0 or trace.stderr or child_guard != expected_child_guard:
        raise ValueError("M0 child Python did not load only the tracked inert guard")
    return {
        "container_image_digest": image_digest,
        "container_image_digest_source": digest_source,
        "runtime_container_id": container_id,
        "runtime_container_id_source": "host_bind_mount",
        "source_mode": source_mode,
        "source_commit": source_commit,
        "source_mount_read_only": True,
        "project_overlay_mode": overlay_mode,
        "python_no_site": True,
        "python_pycache_prefix": pycache_prefix,
        "python_sys_path": list(sys.path),
        "sitecustomize_loaded": False,
        "usercustomize_loaded": False,
        "child_python_guard": {
            **expected_child_guard,
            "sitecustomize_sha256": _sha256_file(guard_script),
        },
    }


class RecordingResult(unittest.TextTestResult):
    """Capture one terminal outcome for every top-level unittest ID."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.started_test_ids: list[str] = []
        self.outcomes: dict[str, str] = {}

    def startTest(self, test: unittest.TestCase) -> None:  # noqa: N802
        self.started_test_ids.append(test.id())
        super().startTest(test)

    def addSuccess(self, test: unittest.TestCase) -> None:  # noqa: N802
        self.outcomes[test.id()] = "passed"
        super().addSuccess(test)

    def addError(self, test: unittest.TestCase, err: Any) -> None:  # noqa: N802
        self.outcomes[test.id()] = "error"
        super().addError(test, err)

    def addFailure(self, test: unittest.TestCase, err: Any) -> None:  # noqa: N802
        self.outcomes[test.id()] = "failed"
        super().addFailure(test, err)

    def addSkip(self, test: unittest.TestCase, reason: str) -> None:  # noqa: N802
        self.outcomes[test.id()] = "skipped"
        super().addSkip(test, reason)

    def addExpectedFailure(self, test: unittest.TestCase, err: Any) -> None:  # noqa: N802
        self.outcomes[test.id()] = "expected_failure"
        super().addExpectedFailure(test, err)

    def addUnexpectedSuccess(self, test: unittest.TestCase) -> None:  # noqa: N802
        self.outcomes[test.id()] = "unexpected_success"
        super().addUnexpectedSuccess(test)

    def addSubTest(  # noqa: N802
        self, test: unittest.TestCase, subtest: unittest.TestCase, err: Any
    ) -> None:
        if err is not None:
            self.outcomes[test.id()] = "failed"
        super().addSubTest(test, subtest, err)


def _write_json_exclusive(path: Path, document: dict[str, Any]) -> None:
    with path.open("x", encoding="utf-8") as output:
        json.dump(document, output, indent=2, sort_keys=True)
        output.write("\n")


def run_suite(run_dir: Path) -> dict[str, Any]:
    raw_log = run_dir / "logs/m0_validation_suite.log"
    result_path = run_dir / "metrics/m0_validation_suite.json"
    if raw_log.exists() or result_path.exists():
        raise FileExistsError("M0 validation-suite evidence is write-once")

    execution_identity = _read_execution_identity()
    python_executable = Path(sys.executable).resolve(strict=True)
    executable_info = python_executable.lstat()
    if not stat.S_ISREG(executable_info.st_mode):
        raise ValueError("resolved Python executable is not a regular file")
    content_vector = qualification_content_vector(ROOT_DIR, "HEAD")
    require_qualification_checkout_equal(ROOT_DIR, content_vector["git_commit"])
    source_bindings = suite_source_bindings(ROOT_DIR)
    plan_relative = "doc/network_radio_integration_plan_v3.md"
    plan_contract = {
        "plan_version": 3,
        "path": plan_relative,
        "contract_sha256": source_bindings.get(plan_relative),
    }
    if not isinstance(plan_contract["contract_sha256"], str):
        raise ValueError("authoritative v3 plan is not source-bound")
    external_bindings = suite_external_bindings(ROOT_DIR)
    frozen_manifest, frozen_manifest_sha256 = load_frozen_test_manifest(ROOT_DIR)
    import_policy, import_policy_sha256 = load_m0_import_policy(ROOT_DIR)
    sys_path_before = list(sys.path)
    pth_before = _pth_inventory(import_policy, run_dir.name)
    if sys_path_before != expected_m0_sys_path(import_policy, run_dir.name):
        raise ValueError("M0 Python path is not the exact locked pre-collection order")
    started_utc = _utc_now()
    suite, discovered_ids, discovered_modules = discover_owned_test_suite(
        ROOT_DIR, "Q0", content_vector
    )
    if discovered_modules != frozen_manifest["test_modules"]:
        raise RuntimeError("Q0 test modules differ from the frozen M0 manifest")
    if discovered_ids != frozen_manifest["ordered_test_ids"]:
        raise RuntimeError("unittest discovery differs from the frozen M0 test manifest")
    _require_discovered_test_sources(discovered_ids, source_bindings)

    with raw_log.open("x", encoding="utf-8", buffering=1) as stream:
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
            runner = unittest.TextTestRunner(
                stream=stream,
                verbosity=2,
                buffer=True,
                failfast=False,
                resultclass=RecordingResult,
            )
            started_monotonic_ns = time.monotonic_ns()
            result = runner.run(suite)
            completed_monotonic_ns = time.monotonic_ns()
            completed_utc = _utc_now()

    raw_bytes = raw_log.read_bytes()
    source_bindings_after = suite_source_bindings(ROOT_DIR)
    external_bindings_after = suite_external_bindings(ROOT_DIR)
    python_import_trace = build_m0_import_trace(
        import_policy,
        import_policy_sha256,
        run_dir.name,
        sys_path_before=sys_path_before,
        pth_before=pth_before,
    )
    require_qualification_checkout_equal(ROOT_DIR, content_vector["git_commit"])
    outcomes = [
        {
            "test_id": test_id,
            "outcome": result.outcomes.get(test_id, "not_completed"),
        }
        for test_id in discovered_ids
    ]
    outcome_counts = {
        name: sum(record["outcome"] == name for record in outcomes)
        for name in (
            "passed",
            "failed",
            "error",
            "skipped",
            "expected_failure",
            "unexpected_success",
            "not_completed",
        )
    }
    passed = (
        result.wasSuccessful()
        and result.testsRun == len(discovered_ids)
        and result.started_test_ids == discovered_ids
        and outcome_counts["passed"] == len(discovered_ids)
        and source_bindings_after == source_bindings
        and external_bindings_after == external_bindings
    )
    document: dict[str, Any] = {
        "schema_version": 5,
        "suite": "complete_network_validation_adversarial_unittest",
        "started_utc": started_utc,
        "completed_utc": completed_utc,
        "execution_identity": execution_identity,
        "invocation": {
            "producer_command": [
                str(python_executable),
                "-S",
                "network/scripts/run_m0_validation_suite.py",
                "--run-dir",
                f"/run/ams/m0-artifacts/{run_dir.name}",
            ],
            "working_directory": "repository_root",
            "unittest_loader_call": {
                "api": "qualification_suite.discover_owned_test_suite",
                "node": "Q0",
                "manifest_path": M0_TEST_MANIFEST_PATH,
                "start_directory": "network/tests",
                "pattern": "test_*.py",
                "verbosity": 2,
                "buffer": True,
                "failfast": False,
            },
        },
        "python_executable": {
            "resolved_path": str(python_executable),
            "bytes": executable_info.st_size,
            "sha256": _sha256_file(python_executable),
        },
        "python_import_trace": python_import_trace,
        "source_bindings": source_bindings,
        "source_bindings_after": source_bindings_after,
        "qualification_content_vector": content_vector,
        "plan_contract": plan_contract,
        "external_input_bindings": external_bindings,
        "external_input_bindings_after": external_bindings_after,
        "frozen_test_manifest": {
            "path": M0_TEST_MANIFEST_PATH,
            "sha256": frozen_manifest_sha256,
        },
        "discovery": {
            "start_directory": "network/tests",
            "pattern": "test_*.py",
            "test_count": len(discovered_ids),
            "test_ids": discovered_ids,
        },
        "execution": {
            "started_monotonic_ns": started_monotonic_ns,
            "completed_monotonic_ns": completed_monotonic_ns,
            "started_test_ids": result.started_test_ids,
            "tests_run": result.testsRun,
            "outcome_counts": outcome_counts,
            "outcomes": outcomes,
        },
        "raw_log": {
            "path": "logs/m0_validation_suite.log",
            "bytes": len(raw_bytes),
            "sha256": hashlib.sha256(raw_bytes).hexdigest(),
        },
        "producer_observation": {"passed": passed},
    }
    _write_json_exclusive(result_path, document)
    return document


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        run_dir = _safe_run_dir(args.run_dir)
        result = run_suite(run_dir)
    except Exception as exc:
        print(f"FAIL M0 validation-suite producer: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 1
    passed = result.get("producer_observation", {}).get("passed") is True
    print(
        f"M0 validation/adversarial suite recorded "
        f"{result['discovery']['test_count']} tests; passed={str(passed).lower()}"
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
