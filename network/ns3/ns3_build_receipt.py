#!/usr/bin/env python3
"""Create and verify fail-closed receipts for pinned ns-3 scratch builds.

The receipt is deliberately derived from the files which will be used at
runtime.  It does not trust a previous build command or a producer-written
"success" flag.  The content-addressed receipt name permits later builds to
coexist without overwriting earlier receipts.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


CONTRACT = "ams.ns3.build-receipt/v1"
EXPECTED_NS3_VERSION = "3.40"
EXPECTED_CORE_TREE_FILES = 3764
EXPECTED_CORE_TREE_SHA256 = (
    "0119836a7c79f7470f0c2c866de9c14ddc4f22349bbd194112ff2952713b64e8"
)
CORE_TREE_EXCLUDED_PARTS = {
    "build",
    "cmake-cache",
    "scratch",
    "__pycache__",
    ".vscode",
}
RECEIPT_DIRECTORY = "build/ams-build-receipts"
HEX64 = re.compile(r"[0-9a-f]{64}")
PROGRAM_NAME = re.compile(r"[a-zA-Z0-9_.-]+")


class ReceiptError(RuntimeError):
    """A build cannot be attested or a receipt cannot be verified."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(data: Any) -> bytes:
    return json.dumps(
        data,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def deterministic_tree_hash(files: Iterable[Path], root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(bytes.fromhex(sha256_file(path)))
    return digest.hexdigest()


def ns3_core_tree_hash(root: Path) -> tuple[int, str]:
    files: list[Path] = []
    for candidate in root.rglob("*"):
        if not candidate.is_file() or candidate.is_symlink():
            continue
        relative = candidate.relative_to(root)
        if relative.parts[:2] == ("src", "lorawan"):
            continue
        if any(part in CORE_TREE_EXCLUDED_PARTS for part in relative.parts):
            continue
        if relative.name.startswith(".lock-") or candidate.suffix in {".pyc", ".pyo"}:
            continue
        files.append(candidate)
    return len(files), deterministic_tree_hash(files, root)


def strict_json(path: Path) -> dict[str, Any]:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-standard JSON constant: {value}")

    def reject_duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        data = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicate,
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ReceiptError(f"receipt is not strict JSON: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ReceiptError(f"receipt top level is not an object: {path}")
    return data


def require_regular_file(path: Path, label: str, *, executable: bool = False) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ReceiptError(f"{label} is missing: {path}: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ReceiptError(f"{label} must be a non-symlink regular file: {path}")
    if metadata.st_size <= 0:
        raise ReceiptError(f"{label} is empty: {path}")
    if executable and metadata.st_mode & 0o111 == 0:
        raise ReceiptError(f"{label} is not executable: {path}")
    return metadata


def file_record(path: Path, label: str, *, executable: bool = False) -> dict[str, Any]:
    metadata = require_regular_file(path, label, executable=executable)
    return {
        "path": str(path.resolve()),
        "sha256": sha256_file(path),
        "size_bytes": metadata.st_size,
        "mode": stat.S_IMODE(metadata.st_mode),
    }


def absolute_path(path: Path) -> Path:
    """Normalize ``.``/``..`` without silently accepting a symlink input."""

    return Path(os.path.abspath(os.fspath(path)))


def parse_cmake_cache(path: Path) -> dict[str, str]:
    require_regular_file(path, "CMake cache")
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ReceiptError(f"cannot read CMake cache {path}: {exc}") from exc
    for line in lines:
        if not line or line.startswith(("#", "//")) or "=" not in line or ":" not in line:
            continue
        key_and_type, value = line.split("=", 1)
        key, _kind = key_and_type.split(":", 1)
        if key in values:
            raise ReceiptError(f"duplicate CMake cache key: {key}")
        values[key] = value
    return values


def parse_ns3_lock(path: Path) -> dict[str, Any]:
    """Parse the ns-3 wrapper lock without executing its Python contents."""

    require_regular_file(path, "ns-3 wrapper lock")
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeError, SyntaxError) as exc:
        raise ReceiptError(f"cannot parse ns-3 wrapper lock {path}: {exc}") from exc
    values: dict[str, Any] = {}
    for statement in tree.body:
        if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
            raise ReceiptError("ns-3 wrapper lock contains a non-assignment statement")
        target = statement.targets[0]
        if not isinstance(target, ast.Name) or target.id in values:
            raise ReceiptError("ns-3 wrapper lock contains an invalid/duplicate assignment")
        try:
            values[target.id] = ast.literal_eval(statement.value)
        except (ValueError, TypeError) as exc:
            raise ReceiptError(
                f"ns-3 wrapper lock value is not a literal: {target.id}"
            ) from exc
    return values


def command_output(command: list[str], label: str) -> str:
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReceiptError(f"cannot inspect {label}: {exc}") from exc
    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise ReceiptError(f"cannot inspect {label}: exit {result.returncode}: {stderr}")
    output = result.stdout.strip()
    if not output:
        raise ReceiptError(f"{label} identity output is empty")
    return output


def tool_identity(path_text: str, label: str, *, compiler: bool = False) -> dict[str, Any]:
    path = absolute_path(Path(path_text))
    try:
        configured_metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ReceiptError(f"{label} is missing: {path}: {exc}") from exc
    if not (stat.S_ISREG(configured_metadata.st_mode) or stat.S_ISLNK(configured_metadata.st_mode)):
        raise ReceiptError(f"{label} path is neither a regular file nor a symlink: {path}")
    record = file_record(resolved, f"resolved {label}", executable=True)
    record["configured_path"] = str(path)
    record["configured_symlink"] = os.readlink(path) if path.is_symlink() else None
    record["resolved_path"] = str(resolved)
    record["version_output"] = command_output([str(path), "--version"], label)
    if compiler:
        record["target"] = command_output([str(path), "-dumpmachine"], label)
        record["version"] = command_output([str(path), "-dumpfullversion"], label)
    return record


def split_modules(raw: str, label: str) -> list[str]:
    values = [item.strip() for item in raw.replace(";", ",").split(",") if item.strip()]
    if not values or any(PROGRAM_NAME.fullmatch(item) is None for item in values):
        raise ReceiptError(f"{label} is empty or invalid: {raw!r}")
    if len(values) != len(set(values)):
        raise ReceiptError(f"{label} contains duplicates: {raw!r}")
    return sorted(values)


def build_subject(args: argparse.Namespace) -> dict[str, Any]:
    ns3_dir = absolute_path(args.ns3_dir)
    if not ns3_dir.is_dir() or ns3_dir.is_symlink():
        raise ReceiptError(f"ns-3 root must be a non-symlink directory: {ns3_dir}")
    if PROGRAM_NAME.fullmatch(args.program) is None:
        raise ReceiptError(f"invalid program name: {args.program!r}")

    version_path = ns3_dir / "VERSION"
    require_regular_file(version_path, "ns-3 VERSION")
    version = version_path.read_text(encoding="utf-8").strip()
    if version != args.expected_version:
        raise ReceiptError(
            f"ns-3 version mismatch: expected {args.expected_version!r}, observed {version!r}"
        )
    file_count, tree_hash = ns3_core_tree_hash(ns3_dir)
    if file_count != args.expected_core_tree_files:
        raise ReceiptError(
            "official ns-3 core tree file-count mismatch: "
            f"expected {args.expected_core_tree_files}, observed {file_count}"
        )
    if tree_hash != args.expected_core_tree_sha256:
        raise ReceiptError(
            "official ns-3 core tree hash mismatch: "
            f"expected {args.expected_core_tree_sha256}, observed {tree_hash}"
        )

    project_source = absolute_path(args.project_source)
    copied_source = absolute_path(args.copied_source)
    executable = absolute_path(args.executable)
    project_record = file_record(project_source, "project scratch source")
    copied_record = file_record(copied_source, "copied ns-3 scratch source")
    if (
        project_record["sha256"] != copied_record["sha256"]
        or project_record["size_bytes"] != copied_record["size_bytes"]
    ):
        raise ReceiptError("project and copied ns-3 scratch sources are not byte-identical")

    cache_path = ns3_dir / "cmake-cache/CMakeCache.txt"
    cache = parse_cmake_cache(cache_path)
    lock_path = ns3_dir / f".lock-ns3_{sys.platform}_build"
    wrapper_lock = parse_ns3_lock(lock_path)
    required_cache_keys = {
        "CMAKE_BUILD_TYPE",
        "CMAKE_COMMAND",
        "CMAKE_CXX_COMPILER",
        "CMAKE_EXE_LINKER_FLAGS",
        "CMAKE_GENERATOR",
        "CMAKE_MAKE_PROGRAM",
        "NS3_ENABLED_MODULES",
        "NS3_EXAMPLES",
        "NS3_SOURCE_DIR",
        "NS3_TESTS",
    }
    missing = sorted(required_cache_keys - set(cache))
    if missing:
        raise ReceiptError(f"CMake cache lacks required identity/options: {missing}")
    enabled_modules = split_modules(cache["NS3_ENABLED_MODULES"], "NS3_ENABLED_MODULES")
    required_modules = split_modules(args.required_modules, "required modules")
    if enabled_modules != required_modules:
        raise ReceiptError(
            "enabled ns-3 module set differs from the exact required set: "
            f"required={required_modules}, observed={enabled_modules}"
        )
    if cache["NS3_EXAMPLES"] != "OFF" or cache["NS3_TESTS"] != "OFF":
        raise ReceiptError("ns-3 examples/tests must both be OFF")
    if Path(cache["NS3_SOURCE_DIR"]).resolve() != ns3_dir:
        raise ReceiptError("CMake cache NS3_SOURCE_DIR does not match the attested tree")

    required_lock_keys = {
        "APPNAME",
        "BUILD_PROFILE",
        "ENABLE_EXAMPLES",
        "ENABLE_TESTS",
        "NS3_ENABLED_CONTRIBUTED_MODULES",
        "NS3_ENABLED_MODULES",
        "VERSION",
        "out_dir",
        "run_dir",
        "top_dir",
        "ns3_runnable_programs",
    }
    missing_lock = sorted(required_lock_keys - set(wrapper_lock))
    if missing_lock:
        raise ReceiptError(f"ns-3 wrapper lock lacks required fields: {missing_lock}")
    raw_lock_modules = wrapper_lock["NS3_ENABLED_MODULES"]
    contributed_modules = wrapper_lock["NS3_ENABLED_CONTRIBUTED_MODULES"]
    if not isinstance(raw_lock_modules, list) or not all(
        isinstance(item, str) and item.startswith("ns3-") for item in raw_lock_modules
    ):
        raise ReceiptError("ns-3 wrapper lock module list is invalid")
    if contributed_modules != []:
        raise ReceiptError("contributed ns-3 modules are forbidden in the pinned build")
    lock_modules = sorted(item.removeprefix("ns3-") for item in raw_lock_modules)
    if lock_modules != enabled_modules:
        raise ReceiptError(
            "ns-3 wrapper lock modules differ from CMakeCache: "
            f"lock={lock_modules}, cache={enabled_modules}"
        )
    if wrapper_lock["VERSION"] != version:
        raise ReceiptError("ns-3 wrapper lock version differs from VERSION")
    if wrapper_lock["BUILD_PROFILE"] != cache["CMAKE_BUILD_TYPE"]:
        raise ReceiptError("ns-3 wrapper lock build profile differs from CMakeCache")
    if wrapper_lock["ENABLE_EXAMPLES"] is not False or wrapper_lock["ENABLE_TESTS"] is not False:
        raise ReceiptError("ns-3 wrapper lock enables examples/tests")
    if wrapper_lock["APPNAME"] != "ns":
        raise ReceiptError("ns-3 wrapper lock APPNAME is invalid")
    expected_lock_paths = {
        "top_dir": ns3_dir,
        "run_dir": ns3_dir,
        "out_dir": ns3_dir / "build",
    }
    for key, expected_path in expected_lock_paths.items():
        value = wrapper_lock[key]
        if not isinstance(value, str) or Path(value).resolve() != expected_path:
            raise ReceiptError(f"ns-3 wrapper lock {key} does not match the attested tree")
    runnable_programs = wrapper_lock["ns3_runnable_programs"]
    if not isinstance(runnable_programs, list) or not all(
        isinstance(value, str) for value in runnable_programs
    ):
        raise ReceiptError("ns-3 wrapper lock runnable-program list is invalid")
    runnable_paths = [Path(value).resolve() for value in runnable_programs]
    if executable.resolve() not in runnable_paths:
        raise ReceiptError("attested executable is absent from the ns-3 wrapper lock")

    # The whole cache hash rejects any unlisted option change.  The selected
    # map keeps the compiler, generator, flags, and all ns-3 switches directly
    # reviewable in the receipt.
    selected_options = {
        key: value
        for key, value in sorted(cache.items())
        if key.startswith("NS3_")
        or key.startswith("CMAKE_CXX_FLAGS")
        or key.startswith("CMAKE_EXE_LINKER_FLAGS")
        or key
        in {
            "CMAKE_BUILD_TYPE",
            "CMAKE_GENERATOR",
            "CMAKE_GENERATOR_INSTANCE",
            "CMAKE_GENERATOR_PLATFORM",
            "CMAKE_GENERATOR_TOOLSET",
            "CMAKE_MAKE_PROGRAM",
        }
    }
    executable_record = file_record(executable, "ns-3 scratch executable", executable=True)
    if args.program not in executable.name:
        raise ReceiptError(
            f"executable basename does not contain program name {args.program!r}: {executable.name}"
        )

    return {
        "program": args.program,
        "official_source": {
            "root": str(ns3_dir),
            "version": version,
            "expected_version": args.expected_version,
            "core_tree_files": file_count,
            "core_tree_sha256": tree_hash,
            "expected_core_tree_files": args.expected_core_tree_files,
            "expected_core_tree_sha256": args.expected_core_tree_sha256,
            "excludes": ["build", "cmake-cache", "scratch", "src/lorawan"],
        },
        "scratch_source": {
            "project": project_record,
            "copied": copied_record,
            "byte_identical": True,
        },
        "build": {
            "cmake_cache": file_record(cache_path, "CMake cache"),
            "ns3_wrapper_lock": {
                **file_record(lock_path, "ns-3 wrapper lock"),
                "version": wrapper_lock["VERSION"],
                "build_profile": wrapper_lock["BUILD_PROFILE"],
                "enabled_modules": lock_modules,
                "enabled_contributed_modules": contributed_modules,
                "examples": wrapper_lock["ENABLE_EXAMPLES"],
                "tests": wrapper_lock["ENABLE_TESTS"],
                "attested_executable_listed": True,
            },
            "enabled_modules": enabled_modules,
            "required_modules": required_modules,
            "options": selected_options,
            "cmake": tool_identity(cache["CMAKE_COMMAND"], "CMake"),
            "cxx_compiler": tool_identity(
                cache["CMAKE_CXX_COMPILER"], "C++ compiler", compiler=True
            ),
        },
        "executable": executable_record,
    }


def subject_digest(subject: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(subject)).hexdigest()


def default_receipt_path(ns3_dir: Path, program: str, digest: str) -> Path:
    return ns3_dir / RECEIPT_DIRECTORY / f"{program}-{digest}.json"


def receipt_document(subject: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "contract": CONTRACT,
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        ),
        "subject_sha256": subject_digest(subject),
        "subject": subject,
    }


def validate_receipt_file(path: Path, expected_subject: dict[str, Any]) -> dict[str, Any]:
    metadata = require_regular_file(path, "ns-3 build receipt")
    if metadata.st_mode & 0o222:
        raise ReceiptError(f"ns-3 build receipt remains writable: {path}")
    data = strict_json(path)
    if set(data) != {"schema_version", "contract", "created_utc", "subject_sha256", "subject"}:
        raise ReceiptError("ns-3 build receipt fields differ from the v1 contract")
    if data.get("schema_version") != 1 or data.get("contract") != CONTRACT:
        raise ReceiptError("ns-3 build receipt contract/schema mismatch")
    try:
        created = datetime.fromisoformat(str(data["created_utc"]).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReceiptError("ns-3 build receipt created_utc is invalid") from exc
    if created.tzinfo is None or created.utcoffset() is None:
        raise ReceiptError("ns-3 build receipt created_utc is not timezone-aware")
    digest = subject_digest(expected_subject)
    if data.get("subject_sha256") != digest or not HEX64.fullmatch(
        str(data.get("subject_sha256", ""))
    ):
        raise ReceiptError("ns-3 build receipt subject digest mismatch")
    if data.get("subject") != expected_subject:
        raise ReceiptError("ns-3 build receipt does not match current build inputs/output")
    return data


def atomic_write_once(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise ReceiptError(f"receipt destination directory may not be a symlink: {path.parent}")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), 0o444)
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise ReceiptError(f"write-once destination already exists: {path}") from exc
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def create(args: argparse.Namespace) -> Path:
    subject = build_subject(args)
    digest = subject_digest(subject)
    path = args.receipt or default_receipt_path(absolute_path(args.ns3_dir), args.program, digest)
    path = absolute_path(path)
    if path.exists() or path.is_symlink():
        validate_receipt_file(path, subject)
        return path
    document = receipt_document(subject)
    atomic_write_once(path, json.dumps(document, indent=2, sort_keys=True).encode("utf-8") + b"\n")
    validate_receipt_file(path, subject)
    return path


def verify(args: argparse.Namespace) -> Path:
    subject = build_subject(args)
    digest = subject_digest(subject)
    path = args.receipt or default_receipt_path(absolute_path(args.ns3_dir), args.program, digest)
    path = absolute_path(path)
    validate_receipt_file(path, subject)
    if args.copy_to is not None:
        payload = path.read_bytes()
        copy_path = absolute_path(args.copy_to)
        atomic_write_once(copy_path, payload)
        validate_receipt_file(copy_path, subject)
    return path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("create", "verify"))
    parser.add_argument("--ns3-dir", required=True, type=Path)
    parser.add_argument("--program", required=True)
    parser.add_argument("--project-source", required=True, type=Path)
    parser.add_argument("--copied-source", required=True, type=Path)
    parser.add_argument("--executable", required=True, type=Path)
    parser.add_argument("--required-modules", required=True)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--copy-to", type=Path)
    parser.add_argument("--expected-version", default=EXPECTED_NS3_VERSION)
    parser.add_argument("--expected-core-tree-files", type=int, default=EXPECTED_CORE_TREE_FILES)
    parser.add_argument(
        "--expected-core-tree-sha256", default=EXPECTED_CORE_TREE_SHA256
    )
    args = parser.parse_args(argv)
    if args.mode == "create" and args.copy_to is not None:
        parser.error("--copy-to is valid only in verify mode")
    if args.expected_core_tree_files < 1:
        parser.error("--expected-core-tree-files must be positive")
    if HEX64.fullmatch(args.expected_core_tree_sha256) is None:
        parser.error("--expected-core-tree-sha256 must be 64 lowercase hex characters")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        path = create(args) if args.mode == "create" else verify(args)
    except (ReceiptError, OSError, UnicodeError, ValueError) as exc:
        print(f"FAIL ns-3 build receipt: {exc}", file=sys.stderr)
        return 2
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
