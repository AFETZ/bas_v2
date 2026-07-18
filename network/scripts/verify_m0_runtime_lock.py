#!/usr/bin/env python3
"""Independently verify the M0 dependency/runtime lock in a fresh image.

This verifier consumes no producer-written PASS flag or provenance payload.  A
host validator is expected to launch it in a fresh container created by the
immutable image ID returned by ``docker image inspect`` and pass that same ID
via ``--observed-image-digest``.  Everything else is recomputed from the live
container filesystem and commands.

The report intentionally contains hashes and bounded diagnostics rather than
the (potentially very large) package manifests.  Verification is performed
twice for mutable runtime inputs so a change during the audit fails closed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import resource
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import yaml


ROOT_DIR = Path(__file__).resolve().parents[2]
CONTRACT = "ams.m0.runtime-lock-verification/v1"
HEX40 = re.compile(r"[0-9a-f]{40}")
HEX64 = re.compile(r"[0-9a-f]{64}")
IMAGE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
MAX_LOCK_BYTES = 1024 * 1024
MAX_LOCKED_FILE_BYTES = 512 * 1024 * 1024
MAX_COMMAND_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_FAILURES = 64
MAX_FAILURE_LENGTH = 320
MAX_REPORT_BYTES = 64 * 1024
COMMAND_TIMEOUT_S = 60.0

REQUIRED_EXTERNAL_SOURCES = (
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
REQUIRED_ROLE_EXECUTABLES = {
    "arducopter",
    "gazebo_server",
    "mavproxy",
    "micro_ros_agent",
}
REQUIRED_ROLE_INVOKED_FILES = {"mavproxy"}
MANIFEST_NAMES = ("pip_freeze", "dpkg", "ros_packages")
NS3_EXCLUDES_IN_LOCK = ("build", "cmake-cache", "scratch", "src/lorawan")
NS3_EXCLUDED_DIRECTORY_NAMES = {
    "build",
    "cmake-cache",
    "scratch",
    "__pycache__",
    ".vscode",
}
NS3_ALLOWED_SYMLINKS = {
    "doc/contributing/source/figures": "../figures",
    "doc/manual/source/figures": "../figures",
    "doc/tutorial/source/figures": "../figures",
}


class VerificationError(RuntimeError):
    """A required runtime fact is unavailable or malformed."""


class StrictSafeLoader(yaml.SafeLoader):
    """Safe YAML loader which rejects duplicate mapping keys."""


def _strict_mapping(
    loader: StrictSafeLoader, node: yaml.nodes.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as exc:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "mapping key is not hashable",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"duplicate mapping key: {key!r}",
                key_node.start_mark,
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


StrictSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _strict_mapping
)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


CommandRunner = Callable[..., CommandResult]


def _truncate(value: object, limit: int = MAX_FAILURE_LENGTH) -> str:
    text = str(value).replace("\x00", "\\0").replace("\r", "\\r").replace("\n", "\\n")
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


class Failures:
    def __init__(self) -> None:
        self._values: list[str] = []
        self._omitted = 0

    def add(self, value: object) -> None:
        text = _truncate(value)
        if len(self._values) < MAX_FAILURES:
            self._values.append(text)
        else:
            self._omitted += 1

    def values(self) -> list[str]:
        values = list(self._values)
        if self._omitted:
            values.append(f"{self._omitted} additional failure(s) omitted")
        return values

    def __len__(self) -> int:
        return len(self._values) + self._omitted


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise VerificationError(f"{label} must be a string-keyed mapping")
    return value


def _canonical_absolute_path(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise VerificationError(f"{label} is not a non-empty filesystem string")
    path = Path(value)
    if not path.is_absolute() or os.path.normpath(value) != value:
        raise VerificationError(f"{label} is not a canonical absolute path: {value!r}")
    return path


def _read_regular_file(path: Path, *, maximum: int, label: str) -> bytes:
    """Read one non-symlink regular file and reject an in-flight replacement."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise VerificationError(f"cannot open {label} {path}: {exc}") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise VerificationError(f"{label} is not a regular file: {path}")
        if before.st_size <= 0:
            raise VerificationError(f"{label} is empty: {path}")
        if before.st_size > maximum:
            raise VerificationError(
                f"{label} exceeds the {maximum}-byte inspection bound: {path}"
            )
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                raise VerificationError(f"short read while inspecting {label}: {path}")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(fd, 1):
            raise VerificationError(f"{label} grew while being inspected: {path}")
        after = os.fstat(fd)
    finally:
        os.close(fd)
    stable_fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    if any(getattr(before, name) != getattr(after, name) for name in stable_fields):
        raise VerificationError(f"{label} changed while being inspected: {path}")
    return b"".join(chunks)


def _sha256_regular_file(
    path: Path,
    *,
    label: str,
    executable: bool = False,
    allow_empty: bool = False,
) -> tuple[str, int]:
    try:
        if path.resolve(strict=True) != path:
            raise VerificationError(f"{label} path contains a symlink: {path}")
    except OSError as exc:
        raise VerificationError(f"cannot resolve {label} {path}: {exc}") from exc
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise VerificationError(f"cannot open {label} {path}: {exc}") from exc
    digest = hashlib.sha256()
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise VerificationError(f"{label} is not a regular file: {path}")
        if before.st_size < 0 or (before.st_size == 0 and not allow_empty):
            raise VerificationError(f"{label} is empty: {path}")
        if before.st_size > MAX_LOCKED_FILE_BYTES:
            raise VerificationError(
                f"{label} exceeds the {MAX_LOCKED_FILE_BYTES}-byte inspection bound: {path}"
            )
        if executable and before.st_mode & 0o111 == 0:
            raise VerificationError(f"{label} is not executable: {path}")
        remaining = before.st_size
        while remaining:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                raise VerificationError(f"short read while hashing {label}: {path}")
            digest.update(chunk)
            remaining -= len(chunk)
        if os.read(fd, 1):
            raise VerificationError(f"{label} grew while being hashed: {path}")
        after = os.fstat(fd)
    finally:
        os.close(fd)
    stable_fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    if any(getattr(before, name) != getattr(after, name) for name in stable_fields):
        raise VerificationError(f"{label} changed while being hashed: {path}")
    return digest.hexdigest(), before.st_size


def _load_lock(path: Path) -> tuple[dict[str, Any], str]:
    try:
        if path.resolve(strict=True) != path:
            raise VerificationError(f"dependency lock path contains a symlink: {path}")
    except OSError as exc:
        raise VerificationError(f"cannot resolve dependency lock {path}: {exc}") from exc
    payload = _read_regular_file(path, maximum=MAX_LOCK_BYTES, label="dependency lock")
    try:
        text = payload.decode("utf-8", errors="strict")
        loaded = yaml.load(text, Loader=StrictSafeLoader)
    except (UnicodeError, yaml.YAMLError) as exc:
        raise VerificationError(f"dependency lock is not strict YAML: {_truncate(exc)}") from exc
    lock = _mapping(loaded, "dependency lock root")
    if lock.get("schema_version") != 2 or lock.get("status") != "complete":
        raise VerificationError("dependency lock must have schema_version 2 and status complete")
    return lock, hashlib.sha256(payload).hexdigest()


def _limit_child_file_size() -> None:
    resource.setrlimit(
        resource.RLIMIT_FSIZE,
        (MAX_COMMAND_OUTPUT_BYTES, MAX_COMMAND_OUTPUT_BYTES),
    )


def run_bounded_command(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float = COMMAND_TIMEOUT_S,
) -> CommandResult:
    """Run without a shell and bound captured stdout/stderr on disk and in RAM."""

    command_env = os.environ.copy()
    if env:
        command_env.update(env)
    command_env["LC_ALL"] = "C"
    command_env["LANG"] = "C"
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        try:
            process = subprocess.Popen(
                args,
                cwd=cwd,
                env=command_env,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                shell=False,
                close_fds=True,
                preexec_fn=_limit_child_file_size,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise VerificationError(f"cannot execute {_truncate(args[0])}: {exc}") from exc
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.wait()
            raise VerificationError(f"command timed out: {_truncate(args[0])}") from exc
        stdout_size = stdout_file.tell()
        stderr_size = stderr_file.tell()
        if stdout_size >= MAX_COMMAND_OUTPUT_BYTES or stderr_size >= MAX_COMMAND_OUTPUT_BYTES:
            raise VerificationError(f"command exceeded output bound: {_truncate(args[0])}")
        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout = stdout_file.read().decode("utf-8", errors="replace")
        stderr = stderr_file.read().decode("utf-8", errors="replace")
    return CommandResult(returncode=returncode, stdout=stdout, stderr=stderr)


def _normalized_manifest(output: str, label: str) -> tuple[int, str]:
    lines = sorted(line.strip() for line in output.splitlines() if line.strip())
    if not lines:
        raise VerificationError(f"{label} manifest is empty")
    if len(lines) != len(set(lines)):
        raise VerificationError(f"{label} manifest contains duplicate normalized lines")
    normalized = "\n".join(lines) + "\n"
    return len(lines), hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _manifest_commands(identity: dict[str, Any]) -> dict[str, list[str]]:
    roles = _mapping(identity.get("role_executable_path"), "role_executable_path")
    python_path = _canonical_absolute_path(roles.get("mavproxy"), "MAVProxy Python path")
    return {
        "pip_freeze": [
            str(python_path),
            "-m",
            "pip",
            "freeze",
            "--all",
            "--exclude-editable",
        ],
        "dpkg": ["/usr/bin/dpkg-query", "-W", "-f=${Package}=${Version}\\n"],
        "ros_packages": ["/opt/ros/humble/bin/ros2", "pkg", "list"],
    }


def _verify_manifests(
    lock: dict[str, Any], identity: dict[str, Any], runner: CommandRunner
) -> dict[str, Any]:
    expected = _mapping(lock.get("runtime_manifest_sha256"), "runtime_manifest_sha256")
    if set(expected) != set(MANIFEST_NAMES):
        raise VerificationError("runtime_manifest_sha256 does not have the exact required keys")
    for name, digest in expected.items():
        if HEX64.fullmatch(str(digest or "")) is None:
            raise VerificationError(f"locked runtime manifest hash is invalid: {name}")
    commands = _manifest_commands(identity)
    passes: list[dict[str, tuple[int, str]]] = []
    for _pass_number in range(2):
        observed: dict[str, tuple[int, str]] = {}
        for name in MANIFEST_NAMES:
            result = runner(commands[name], timeout=COMMAND_TIMEOUT_S)
            if result.returncode != 0:
                raise VerificationError(
                    f"{name} command failed with exit {result.returncode}: "
                    f"{_truncate(result.stderr, 160)}"
                )
            observed[name] = _normalized_manifest(result.stdout, name)
        passes.append(observed)
    if passes[0] != passes[1]:
        raise VerificationError("runtime dependency manifests changed between audit passes")
    records: dict[str, Any] = {}
    for name in MANIFEST_NAMES:
        entries, digest = passes[0][name]
        records[name] = {
            "entries": entries,
            "expected_sha256": expected[name],
            "observed_sha256": digest,
            "status": "passed" if digest == expected[name] else "failed",
        }
        if digest != expected[name]:
            raise VerificationError(f"runtime dependency manifest does not match lock: {name}")
    return records


def _validate_runtime_identity(lock: dict[str, Any]) -> dict[str, Any]:
    identity = _mapping(lock.get("m1_runtime_identity"), "m1_runtime_identity")
    if identity.get("schema_version") != 1:
        raise VerificationError("m1_runtime_identity schema_version is not 1")
    executable_hashes = _mapping(
        identity.get("executable_sha256"), "m1_runtime_identity.executable_sha256"
    )
    invoked_hashes = _mapping(
        identity.get("invoked_file_sha256"), "m1_runtime_identity.invoked_file_sha256"
    )
    role_executables = _mapping(
        identity.get("role_executable_path"), "m1_runtime_identity.role_executable_path"
    )
    role_invoked = _mapping(
        identity.get("role_invoked_file_path"), "m1_runtime_identity.role_invoked_file_path"
    )
    if not executable_hashes or not invoked_hashes:
        raise VerificationError("M1 runtime identity hash maps must be non-empty")
    if set(role_executables) != REQUIRED_ROLE_EXECUTABLES:
        raise VerificationError("M1 runtime executable role set is not exact")
    if set(role_invoked) != REQUIRED_ROLE_INVOKED_FILES:
        raise VerificationError("M1 runtime invoked-file role set is not exact")
    all_paths: list[str] = []
    for label, hashes in (
        ("executable_sha256", executable_hashes),
        ("invoked_file_sha256", invoked_hashes),
    ):
        for path_text, digest in hashes.items():
            _canonical_absolute_path(path_text, f"{label} path")
            if HEX64.fullmatch(str(digest or "")) is None:
                raise VerificationError(f"invalid {label} digest: {path_text!r}")
            all_paths.append(path_text)
    if len(all_paths) != len(set(all_paths)):
        raise VerificationError("M1 executable and invoked-file hash maps overlap")
    for role, path_text in role_executables.items():
        if path_text not in executable_hashes:
            raise VerificationError(f"M1 role {role} refers to an unlocked executable")
    for role, path_text in role_invoked.items():
        if path_text not in invoked_hashes:
            raise VerificationError(f"M1 role {role} refers to an unlocked invoked file")
    return identity


def _validate_m0_execution_policy(lock: dict[str, Any]) -> dict[str, Any]:
    policy = _mapping(lock.get("m0_execution_policy"), "m0_execution_policy")
    expected_keys = {
        "schema_version",
        "container_path",
        "host_final_path",
        "allowed_container_executable_roots",
        "critical_command_resolution",
        "critical_image_executable_sha256",
        "critical_source_executables",
        "host_final_executable_sha256",
        "host_final_python_sys_path",
        "host_final_python_imports",
        "incidental_transitive_policy",
    }
    if set(policy) != expected_keys or policy.get("schema_version") != 1:
        raise VerificationError("m0_execution_policy schema is not exact")
    for key in ("container_path", "host_final_path"):
        value = policy.get(key)
        if not isinstance(value, str) or not value or "\x00" in value:
            raise VerificationError(f"m0_execution_policy {key} is invalid")
    roots = policy.get("allowed_container_executable_roots")
    sources = policy.get("critical_source_executables")
    if (
        not isinstance(roots, list)
        or not roots
        or len(roots) != len(set(roots))
        or not all(isinstance(value, str) for value in roots)
        or not isinstance(sources, list)
        or not sources
        or len(sources) != len(set(sources))
        or not all(
            isinstance(value, str)
            and value
            and not value.startswith("/")
            and ".." not in Path(value).parts
            for value in sources
        )
    ):
        raise VerificationError("M0 executable roots/source command list is invalid")
    for value in roots:
        _canonical_absolute_path(value, "allowed container executable root")
    for mapping_name in (
        "critical_image_executable_sha256",
        "host_final_executable_sha256",
    ):
        values = _mapping(policy.get(mapping_name), f"m0_execution_policy.{mapping_name}")
        if not values:
            raise VerificationError(f"{mapping_name} is empty")
        for path_text, digest in values.items():
            _canonical_absolute_path(path_text, f"{mapping_name} path")
            if HEX64.fullmatch(str(digest or "")) is None:
                raise VerificationError(f"{mapping_name} has an invalid hash")
    host_python_sys_path = policy.get("host_final_python_sys_path")
    if (
        not isinstance(host_python_sys_path, list)
        or not host_python_sys_path
        or len(host_python_sys_path) != len(set(host_python_sys_path))
        or not all(isinstance(value, str) and value for value in host_python_sys_path)
    ):
        raise VerificationError("host-final Python sys.path policy is invalid")
    host_python_imports = _mapping(
        policy.get("host_final_python_imports"),
        "m0_execution_policy.host_final_python_imports",
    )
    if not host_python_imports:
        raise VerificationError("host-final Python import policy is empty")
    for module_name, record_value in host_python_imports.items():
        record = _mapping(
            record_value,
            f"m0_execution_policy.host_final_python_imports.{module_name}",
        )
        if (
            not module_name
            or set(record) != {"path", "bytes", "sha256"}
            or isinstance(record.get("bytes"), bool)
            or not isinstance(record.get("bytes"), int)
            or record.get("bytes", 0) < 1
            or HEX64.fullmatch(str(record.get("sha256") or "")) is None
        ):
            raise VerificationError(
                f"host-final Python import identity is invalid: {module_name!r}"
            )
        _canonical_absolute_path(
            record.get("path"), f"host-final Python import path {module_name}"
        )
    resolutions = _mapping(
        policy.get("critical_command_resolution"),
        "m0_execution_policy.critical_command_resolution",
    )
    if not resolutions:
        raise VerificationError("critical command-resolution policy is empty")
    image_hashes = _mapping(
        policy.get("critical_image_executable_sha256"),
        "critical_image_executable_sha256",
    )
    for command, path_text in resolutions.items():
        if (
            re.fullmatch(r"[A-Za-z0-9_.+-]+", command) is None
            or path_text not in image_hashes
        ):
            raise VerificationError("critical command resolution is invalid/unlocked")
    incidental = _mapping(
        policy.get("incidental_transitive_policy"),
        "m0_execution_policy.incidental_transitive_policy",
    )
    if incidental != {
        "require_immutable_allowed_root": True,
        "require_dpkg_or_pip_manifest_owner": True,
        "forbid_host_or_run_writable_resolution": True,
    }:
        raise VerificationError("M0 incidental executable policy is not fail-closed")
    python_policy = _mapping(
        lock.get("m0_python_import_policy"), "m0_python_import_policy"
    )
    python_keys = {
        "schema_version",
        "mode",
        "interpreter",
        "interpreter_sha256",
        "parent_flags",
        "exact_base_pythonpath",
        "overlay_pythonpath_template",
        "interpreter_suffix",
        "customization",
        "pth_policy",
        "cleared_environment",
        "python_no_user_site",
        "bytecode_root_template",
    }
    if (
        set(python_policy) != python_keys
        or python_policy.get("schema_version") != 1
        or python_policy.get("mode") != "isolated_explicit_path"
        or python_policy.get("parent_flags") != ["-S"]
        or python_policy.get("pth_policy")
        != "inventory_only_not_processed_under_no_site"
        or python_policy.get("python_no_user_site") is not True
    ):
        raise VerificationError("m0_python_import_policy schema/mode is not exact")
    interpreter = _canonical_absolute_path(
        python_policy.get("interpreter"), "M0 Python interpreter"
    )
    image_hashes = _mapping(
        policy["critical_image_executable_sha256"],
        "critical_image_executable_sha256",
    )
    if (
        image_hashes.get(str(interpreter))
        != python_policy.get("interpreter_sha256")
        or HEX64.fullmatch(str(python_policy.get("interpreter_sha256") or "")) is None
    ):
        raise VerificationError("M0 Python interpreter lock is incoherent")
    for key in ("exact_base_pythonpath", "interpreter_suffix", "cleared_environment"):
        values = python_policy.get(key)
        if (
            not isinstance(values, list)
            or not values
            or len(values) != len(set(values))
            or not all(isinstance(value, str) and value for value in values)
        ):
            raise VerificationError(f"M0 Python {key} is invalid")
    customization = _mapping(
        python_policy.get("customization"), "m0_python_import_policy.customization"
    )
    if (
        set(customization)
        != {
            "parent_sitecustomize_loaded",
            "parent_usercustomize_loaded",
            "child_guard_path",
            "child_guard_sha256",
        }
        or customization.get("parent_sitecustomize_loaded") is not False
        or customization.get("parent_usercustomize_loaded") is not False
        or HEX64.fullmatch(str(customization.get("child_guard_sha256") or "")) is None
    ):
        raise VerificationError("M0 Python customization policy is invalid")
    return policy


def _verify_m0_execution_policy(policy: dict[str, Any]) -> dict[str, Any]:
    expected = _mapping(
        policy.get("critical_image_executable_sha256"),
        "critical_image_executable_sha256",
    )
    if os.environ.get("PATH") != policy.get("container_path"):
        raise VerificationError("live M0 PATH differs from the execution policy")
    resolutions = _mapping(
        policy.get("critical_command_resolution"), "critical_command_resolution"
    )
    for command, expected_path in resolutions.items():
        observed_path = shutil.which(command, path=str(policy["container_path"]))
        if observed_path is None or str(Path(observed_path).resolve(strict=True)) != expected_path:
            raise VerificationError(
                f"critical command resolved unexpectedly: {command}: {observed_path}"
            )
    passes: list[dict[str, tuple[str, int]]] = []
    for _pass_number in range(2):
        observed: dict[str, tuple[str, int]] = {}
        for path_text, digest in sorted(expected.items()):
            path = _canonical_absolute_path(path_text, "critical image executable")
            actual, size = _sha256_regular_file(
                path, label="critical image executable", executable=True
            )
            if actual != digest:
                raise VerificationError(f"critical image executable hash mismatch: {path}")
            observed[path_text] = (actual, size)
        passes.append(observed)
    if passes[0] != passes[1]:
        raise VerificationError("critical image executables changed during verification")
    observed = passes[0]
    return {
        "files_checked": len(observed),
        "total_size_bytes": sum(size for _digest, size in observed.values()),
        "path_sha256": hashlib.sha256(
            str(policy["container_path"]).encode("utf-8")
        ).hexdigest(),
        "passes": 2,
        "status": "passed",
    }


def _identity_pass(identity: dict[str, Any]) -> dict[str, tuple[str, int]]:
    records: dict[str, tuple[str, int]] = {}
    for mapping_name, executable in (
        ("executable_sha256", True),
        ("invoked_file_sha256", False),
    ):
        hashes = _mapping(identity[mapping_name], mapping_name)
        for path_text in sorted(hashes):
            path = _canonical_absolute_path(path_text, f"{mapping_name} path")
            digest, size = _sha256_regular_file(
                path, label=f"locked {mapping_name} file", executable=executable
            )
            if digest != hashes[path_text]:
                raise VerificationError(f"locked runtime file hash mismatch: {path_text}")
            records[path_text] = (digest, size)
    return records


def _verify_identity_files(identity: dict[str, Any]) -> dict[str, Any]:
    first = _identity_pass(identity)
    second = _identity_pass(identity)
    if first != second:
        raise VerificationError("locked runtime files changed between audit passes")
    return {
        "files_checked": len(first),
        "total_size_bytes": sum(size for _digest, size in first.values()),
        "status": "passed",
    }


def _expected_external_revisions(dependencies: dict[str, Any]) -> dict[str, str]:
    ros2 = _mapping(dependencies.get("ardupilot_ros_repos"), "ardupilot_ros_repos")
    ros2_revisions = _mapping(ros2.get("revisions"), "ardupilot_ros_repos.revisions")
    gz = _mapping(dependencies.get("ardupilot_gz_repos"), "ardupilot_gz_repos")
    gz_revisions = _mapping(gz.get("revisions"), "ardupilot_gz_repos.revisions")
    ardupilot = _mapping(dependencies.get("ardupilot"), "ardupilot")
    micro_xrce = _mapping(dependencies.get("micro_xrce_dds_gen"), "micro_xrce_dds_gen")
    revisions = {
        "ardupilot_standalone": ardupilot.get("revision"),
        "ardupilot_ros2": ros2_revisions.get("ardupilot"),
        "micro_ros_agent": ros2_revisions.get("micro_ros_agent"),
        "ardupilot_gazebo": gz_revisions.get("ardupilot_gazebo"),
        "ardupilot_gz": gz_revisions.get("ardupilot_gz"),
        "ardupilot_sitl_models": gz_revisions.get("ardupilot_sitl_models"),
        "ros_gz": gz_revisions.get("ros_gz"),
        "sdformat_urdf": gz_revisions.get("sdformat_urdf"),
        "micro_xrce_dds_gen": micro_xrce.get("revision"),
    }
    if set(revisions) != set(REQUIRED_EXTERNAL_SOURCES):
        raise VerificationError("internal external-source revision map is incomplete")
    for name, revision in revisions.items():
        if HEX40.fullmatch(str(revision or "")) is None:
            raise VerificationError(f"external source revision is invalid: {name}")
    return revisions  # type: ignore[return-value]


def _git_command(
    runner: CommandRunner, path: Path, arguments: list[str]
) -> CommandResult:
    git_env = {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    result = runner(
        [
            "/usr/bin/git",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "-C",
            str(path),
            *arguments,
        ],
        env=git_env,
        timeout=COMMAND_TIMEOUT_S,
    )
    if result.returncode != 0:
        raise VerificationError(
            f"Git inspection failed for {path.name}: exit {result.returncode}: "
            f"{_truncate(result.stderr, 160)}"
        )
    return result


def _git_state(runner: CommandRunner, path: Path) -> tuple[str, str]:
    root = _git_command(runner, path, ["rev-parse", "--show-toplevel"]).stdout.strip()
    try:
        if Path(root).resolve(strict=True) != path:
            raise VerificationError(f"external source is not its own Git root: {path}")
    except OSError as exc:
        raise VerificationError(f"cannot resolve Git root for {path}: {exc}") from exc
    commit = _git_command(runner, path, ["rev-parse", "HEAD^{commit}"]).stdout.strip()
    if HEX40.fullmatch(commit) is None:
        raise VerificationError(f"Git returned an invalid commit for {path}")
    status_output = _git_command(
        runner,
        path,
        [
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignore-submodules=none",
        ],
    ).stdout
    return commit, status_output


def _verify_external_sources(
    dependencies: dict[str, Any], runner: CommandRunner
) -> dict[str, Any]:
    paths = _mapping(
        dependencies.get("canonical_runtime_source_paths"),
        "dependencies.canonical_runtime_source_paths",
    )
    if set(paths) != set(REQUIRED_EXTERNAL_SOURCES):
        raise VerificationError("canonical runtime source path set is not exact")
    canonical: dict[str, Path] = {}
    for name, value in paths.items():
        path = _canonical_absolute_path(value, f"canonical source path {name}")
        try:
            if path.resolve(strict=True) != path or not path.is_dir():
                raise VerificationError(f"canonical source is missing, symlinked, or not a directory: {path}")
        except OSError as exc:
            raise VerificationError(f"cannot resolve canonical source {path}: {exc}") from exc
        canonical[name] = path
    if len(set(canonical.values())) != len(canonical):
        raise VerificationError("canonical runtime source paths are not unique")
    revisions = _expected_external_revisions(dependencies)
    first = {name: _git_state(runner, canonical[name]) for name in REQUIRED_EXTERNAL_SOURCES}
    second = {name: _git_state(runner, canonical[name]) for name in REQUIRED_EXTERNAL_SOURCES}
    if first != second:
        raise VerificationError("external Git source state changed between audit passes")
    checked: dict[str, str] = {}
    for name in REQUIRED_EXTERNAL_SOURCES:
        commit, status_output = first[name]
        if commit != revisions[name]:
            raise VerificationError(f"external source revision mismatch: {name}")
        if status_output:
            raise VerificationError(f"external source checkout is dirty: {name}")
        checked[name] = commit
    return {
        "repositories_checked": len(checked),
        "revisions": checked,
        "status": "passed",
    }


def _ns3_excluded(relative: Path) -> bool:
    if relative.parts[:2] == ("src", "lorawan"):
        return True
    return any(part in NS3_EXCLUDED_DIRECTORY_NAMES for part in relative.parts)


def _ns3_tree_pass(root: Path) -> tuple[int, str, dict[str, str]]:
    files: list[Path] = []
    symlinks: dict[str, str] = {}
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        current = Path(directory)
        current_relative = current.relative_to(root)
        kept_directories: list[str] = []
        for name in sorted(directory_names):
            candidate = current / name
            relative = candidate.relative_to(root)
            if _ns3_excluded(relative):
                continue
            try:
                metadata = candidate.lstat()
            except OSError as exc:
                raise VerificationError(f"cannot inspect ns-3 tree entry {relative}: {exc}") from exc
            if stat.S_ISLNK(metadata.st_mode):
                symlinks[relative.as_posix()] = os.readlink(candidate)
            elif stat.S_ISDIR(metadata.st_mode):
                kept_directories.append(name)
            else:
                raise VerificationError(f"ns-3 tree contains a special directory entry: {relative}")
        directory_names[:] = kept_directories
        for name in sorted(file_names):
            candidate = current / name
            relative = candidate.relative_to(root)
            if _ns3_excluded(relative):
                continue
            if relative.name.startswith(".lock-") or candidate.suffix in {".pyc", ".pyo"}:
                continue
            try:
                metadata = candidate.lstat()
            except OSError as exc:
                raise VerificationError(f"cannot inspect ns-3 tree file {relative}: {exc}") from exc
            if stat.S_ISLNK(metadata.st_mode):
                symlinks[relative.as_posix()] = os.readlink(candidate)
            elif stat.S_ISREG(metadata.st_mode):
                files.append(candidate)
            else:
                raise VerificationError(f"ns-3 tree contains a special file: {relative}")
        if current_relative != Path(".") and _ns3_excluded(current_relative):
            raise VerificationError(f"internal ns-3 walker entered excluded path: {current_relative}")
    if symlinks != NS3_ALLOWED_SYMLINKS:
        raise VerificationError("ns-3 release symlink inventory does not match the canonical tree")
    digest = hashlib.sha256()
    for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        file_hash, _size = _sha256_regular_file(
            path, label="ns-3 source file", allow_empty=True
        )
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(bytes.fromhex(file_hash))
    return len(files), digest.hexdigest(), symlinks


def _verify_ns3(dependencies: dict[str, Any], root_dir: Path) -> dict[str, Any]:
    ns3 = _mapping(dependencies.get("ns3"), "dependencies.ns3")
    if ns3.get("source_kind") != "official_release_archive":
        raise VerificationError("ns-3 source_kind is not official_release_archive")
    version = ns3.get("version")
    expected_hash = ns3.get("core_tree_sha256")
    excludes = ns3.get("core_tree_excludes")
    if not isinstance(version, str) or not version:
        raise VerificationError("locked ns-3 version is invalid")
    if HEX64.fullmatch(str(expected_hash or "")) is None:
        raise VerificationError("locked ns-3 core tree hash is invalid")
    if not isinstance(excludes, list) or tuple(excludes) != NS3_EXCLUDES_IN_LOCK:
        raise VerificationError("locked ns-3 core tree exclusions are not exact")
    path_value = ns3.get("path")
    if not isinstance(path_value, str) or not path_value or "\x00" in path_value:
        raise VerificationError("locked ns-3 path is invalid")
    relative = Path(path_value)
    if relative.is_absolute() or os.path.normpath(path_value) != path_value or path_value.startswith("../"):
        raise VerificationError("locked ns-3 path is not a canonical repository-relative path")
    ns3_root = root_dir / relative
    try:
        if ns3_root.resolve(strict=True) != ns3_root or not ns3_root.is_dir():
            raise VerificationError("ns-3 root is missing, symlinked, or not a directory")
    except OSError as exc:
        raise VerificationError(f"cannot resolve ns-3 root: {exc}") from exc
    version_payload = _read_regular_file(
        ns3_root / "VERSION", maximum=1024, label="ns-3 VERSION"
    )
    try:
        observed_version = version_payload.decode("utf-8", errors="strict").strip()
    except UnicodeError as exc:
        raise VerificationError("ns-3 VERSION is not UTF-8") from exc
    if observed_version != version:
        raise VerificationError("ns-3 VERSION does not match dependency lock")
    first = _ns3_tree_pass(ns3_root)
    second = _ns3_tree_pass(ns3_root)
    if first != second:
        raise VerificationError("ns-3 aggregate tree changed between audit passes")
    file_count, digest, symlinks = first
    if file_count < 1 or digest != expected_hash:
        raise VerificationError("ns-3 aggregate tree does not match dependency lock")
    return {
        "files_checked": file_count,
        "expected_sha256": expected_hash,
        "observed_sha256": digest,
        "symlinks_checked": len(symlinks),
        "version": observed_version,
        "status": "passed",
    }


def _failed_check(proof: object) -> dict[str, str]:
    return {"status": "failed", "proof": _truncate(proof)}


def verify_runtime_lock(
    lock_path: Path,
    observed_image_digest: str,
    *,
    root_dir: Path = ROOT_DIR,
    command_runner: CommandRunner = run_bounded_command,
    environment_image_digest: str | None = None,
) -> dict[str, Any]:
    """Return a bounded independent report; never raise on an ordinary mismatch."""

    lock_path = Path(os.path.abspath(os.fspath(lock_path)))
    root_dir = Path(os.path.abspath(os.fspath(root_dir)))
    failures = Failures()
    checks: dict[str, Any] = {}
    lock: dict[str, Any]
    lock_sha256: str | None = None
    try:
        lock, lock_sha256 = _load_lock(lock_path)
        checks["lock"] = {"sha256": lock_sha256, "status": "passed"}
    except Exception as exc:  # The report must remain machine-readable on malformed input.
        failures.add(f"dependency lock: {exc}")
        checks["lock"] = _failed_check(exc)
        lock = {}

    identity: dict[str, Any] = {}
    m0_execution_policy: dict[str, Any] = {}
    dependencies: dict[str, Any] = {}
    if lock:
        try:
            dependencies = _mapping(lock.get("dependencies"), "dependencies")
            ros = _mapping(dependencies.get("ros"), "dependencies.ros")
            identity = _validate_runtime_identity(lock)
            m0_execution_policy = _validate_m0_execution_policy(lock)
            expected_ros_digest = ros.get("project_image_digest")
            expected_identity_digest = identity.get("container_image_digest")
            image_errors: list[str] = []
            if IMAGE_DIGEST.fullmatch(observed_image_digest or "") is None:
                image_errors.append("host-observed image digest is not an immutable SHA-256 ID")
            if expected_ros_digest != observed_image_digest:
                image_errors.append("host-observed image digest differs from dependencies.ros lock")
            if expected_identity_digest != observed_image_digest:
                image_errors.append("host-observed image digest differs from M1 runtime identity lock")
            if environment_image_digest is not None and environment_image_digest != observed_image_digest:
                image_errors.append("runtime image digest environment disagrees with host argument")
            if image_errors:
                raise VerificationError("; ".join(image_errors))
            checks["image_digest"] = {
                "expected": expected_ros_digest,
                "observed": observed_image_digest,
                "status": "passed",
            }
        except Exception as exc:
            failures.add(f"image/runtime identity structure: {exc}")
            checks["image_digest"] = _failed_check(exc)

    if m0_execution_policy:
        try:
            checks["m0_execution_policy"] = _verify_m0_execution_policy(
                m0_execution_policy
            )
        except Exception as exc:
            failures.add(f"M0 execution policy: {exc}")
            checks["m0_execution_policy"] = _failed_check(exc)
    else:
        checks["m0_execution_policy"] = _failed_check(
            "M0 execution/import policy lock unavailable"
        )

    if identity:
        try:
            checks["runtime_manifests"] = {
                "manifests": _verify_manifests(lock, identity, command_runner),
                "passes": 2,
                "status": "passed",
            }
        except Exception as exc:
            failures.add(f"runtime manifests: {exc}")
            checks["runtime_manifests"] = _failed_check(exc)
        try:
            checks["runtime_identity_files"] = _verify_identity_files(identity)
            checks["runtime_identity_files"]["passes"] = 2
        except Exception as exc:
            failures.add(f"runtime identity files: {exc}")
            checks["runtime_identity_files"] = _failed_check(exc)
    else:
        checks.setdefault("runtime_manifests", _failed_check("runtime identity lock unavailable"))
        checks.setdefault("runtime_identity_files", _failed_check("runtime identity lock unavailable"))

    if dependencies:
        try:
            checks["external_sources"] = _verify_external_sources(
                dependencies, command_runner
            )
            checks["external_sources"]["passes"] = 2
        except Exception as exc:
            failures.add(f"external sources: {exc}")
            checks["external_sources"] = _failed_check(exc)
        try:
            checks["ns3_tree"] = _verify_ns3(dependencies, root_dir)
            checks["ns3_tree"]["passes"] = 2
        except Exception as exc:
            failures.add(f"ns-3 tree: {exc}")
            checks["ns3_tree"] = _failed_check(exc)
    else:
        checks.setdefault("external_sources", _failed_check("dependencies lock unavailable"))
        checks.setdefault("ns3_tree", _failed_check("dependencies lock unavailable"))

    # The lock is the root of every comparison and the executable inventory is
    # read early, before the slower Git/ns-3 audit.  Re-read both at the end so
    # a coordinated replacement during verification cannot create a mixed
    # snapshot which nevertheless reports success.
    if lock:
        try:
            _final_lock, final_lock_sha256 = _load_lock(lock_path)
            if final_lock_sha256 != lock_sha256:
                raise VerificationError("dependency lock changed during verification")
            checks["lock"]["passes"] = 2
        except Exception as exc:
            failures.add(f"dependency lock final recheck: {exc}")
            checks["lock"] = _failed_check(exc)
    if identity and checks.get("runtime_identity_files", {}).get("status") == "passed":
        try:
            final_identity = _identity_pass(identity)
            if (
                len(final_identity)
                != checks["runtime_identity_files"].get("files_checked")
                or sum(size for _digest, size in final_identity.values())
                != checks["runtime_identity_files"].get("total_size_bytes")
            ):
                raise VerificationError("locked runtime inventory changed during verification")
            checks["runtime_identity_files"]["passes"] = 3
        except Exception as exc:
            failures.add(f"runtime identity final recheck: {exc}")
            checks["runtime_identity_files"] = _failed_check(exc)

    for required in (
        "lock",
        "image_digest",
        "runtime_manifests",
        "runtime_identity_files",
        "m0_execution_policy",
        "external_sources",
        "ns3_tree",
    ):
        if checks.get(required, {}).get("status") != "passed" and not any(
            required in failure for failure in failures.values()
        ):
            failures.add(f"required check did not pass: {required}")
    passed = len(failures) == 0 and all(
        check.get("status") == "passed" for check in checks.values()
    )
    return {
        "schema_version": 1,
        "contract": CONTRACT,
        "passed": passed,
        "observed_image_digest": observed_image_digest,
        "lock_sha256": lock_sha256,
        "checks": checks,
        "failures": failures.values(),
    }


def _json_bytes(report: dict[str, Any]) -> bytes:
    try:
        payload = (
            json.dumps(
                report,
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise VerificationError(f"cannot serialize verification report: {exc}") from exc
    if len(payload) > MAX_REPORT_BYTES:
        raise VerificationError("verification report exceeds its strict size bound")
    return payload


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lock",
        type=Path,
        default=ROOT_DIR / "network/config/dependency_lock.yaml",
        help="schema-2 dependency lock to verify",
    )
    parser.add_argument(
        "--root-dir",
        type=Path,
        default=ROOT_DIR,
        help="mounted project root containing the locked ns-3 tree",
    )
    parser.add_argument(
        "--observed-image-digest",
        default="",
        help="immutable sha256:<64> image ID independently supplied by the host",
    )
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    # Normalize lexically without resolving symlinks; the verification routines
    # must see and reject a symlink supplied at either trust boundary.
    lock_path = Path(os.path.abspath(os.fspath(args.lock)))
    root_dir = Path(os.path.abspath(os.fspath(args.root_dir)))
    report = verify_runtime_lock(
        lock_path,
        args.observed_image_digest,
        root_dir=root_dir,
        environment_image_digest=os.environ.get("AMS_CONTAINER_IMAGE_DIGEST"),
    )
    try:
        payload = _json_bytes(report)
    except VerificationError as exc:
        payload = _json_bytes(
            {
                "schema_version": 1,
                "contract": CONTRACT,
                "passed": False,
                "checks": {},
                "failures": [_truncate(exc)],
            }
        )
        sys.stdout.buffer.write(payload)
        return 1
    sys.stdout.buffer.write(payload)
    return 0 if report["passed"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
