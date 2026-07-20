#!/usr/bin/python3.10
"""Shared fail-closed primitives for host-side acceptance finalizers."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any


MAX_FILE_BYTES = 128 * 1024 * 1024
M0_CAPABILITY_COMMAND_SCRIPT = (
    "set -euo pipefail; "
    "test -c /dev/net/tun; "
    "/usr/bin/sudo -n /usr/bin/unshare -rn /usr/bin/true; "
    "/usr/bin/sudo -n /usr/bin/true; "
    "printf '%s\\n' capability_probe_passed"
)
M0_CAPABILITY_STDOUT = b"capability_probe_passed\n"


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def read_regular(path: Path, *, allow_empty: bool = False) -> bytes:
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_size > MAX_FILE_BYTES
        or (not allow_empty and info.st_size < 1)
    ):
        raise ValueError(f"not one bounded regular file: {path}")
    payload = path.read_bytes()
    after = path.lstat()
    if (
        len(payload) != info.st_size
        or (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_mode, after.st_size, after.st_mtime_ns)
    ):
        raise ValueError(f"file changed while read: {path}")
    return payload


def strict_json(payload: bytes, label: str) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError(f"{label} contains non-finite JSON: {value}")

    try:
        return json.loads(payload.decode("utf-8"), parse_constant=reject_constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError(f"{label} is not strict JSON: {exc}") from exc


def tree_manifest(root: Path, *, excluded: set[str] | None = None) -> dict[str, Any]:
    excluded = excluded or set()
    if root.is_symlink() or not root.is_dir():
        raise ValueError("M1 tree root is not one real directory")
    entries: dict[str, dict[str, Any]] = {}
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if any(relative == item or relative.startswith(item + "/") for item in excluded):
            continue
        pure = PurePosixPath(relative)
        if any(part in {"", ".", ".."} for part in pure.parts):
            raise ValueError(f"unsafe M1 artifact path: {relative}")
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            entries[relative] = {"kind": "directory"}
        elif stat.S_ISREG(info.st_mode):
            if info.st_nlink != 1 or info.st_size > MAX_FILE_BYTES:
                raise ValueError(f"unsafe M1 artifact file: {relative}")
            payload = read_regular(path, allow_empty=True)
            entries[relative] = {
                "kind": "file",
                "bytes": len(payload),
                "sha256": _sha256(payload),
            }
        elif stat.S_ISLNK(info.st_mode):
            target = os.readlink(path)
            resolved = path.resolve(strict=True)
            resolved.relative_to(root.resolve(strict=True))
            entries[relative] = {
                "kind": "symlink",
                "target": target,
                "target_sha256": _sha256(target.encode("utf-8")),
            }
        else:
            raise ValueError(f"special M1 artifact entry: {relative}")
    return {
        "schema_version": 1,
        "contract": "ams.m1.portable-content-manifest/v1",
        "entries": entries,
        "entry_count": len(entries),
        "content_sha256": _sha256(_canonical(entries)),
    }


def one_inspect(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    raw = read_regular(path)
    documents = strict_json(raw, label)
    if not isinstance(documents, list) or len(documents) != 1 or not isinstance(documents[0], dict):
        raise ValueError(f"{label} must contain one Docker inspection")
    return documents[0], raw


def _run_git(snapshot: Path, arguments: list[str]) -> bytes:
    result = subprocess.run(
        ["/usr/bin/git", "-C", str(snapshot), *arguments],
        check=False,
        capture_output=True,
        timeout=60,
        env={
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C",
            "HOME": "/nonexistent",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
        },
    )
    if result.returncode != 0 or result.stderr:
        raise ValueError(f"read-only M1 source Git inspection failed: {arguments!r}")
    return result.stdout


def validate_source_snapshot(snapshot: Path, source_commit: str) -> None:
    if snapshot.is_symlink() or not snapshot.is_dir():
        raise ValueError("M1 source snapshot is not one real directory")
    for path in [snapshot, *snapshot.rglob("*")]:
        info = path.lstat()
        if not stat.S_ISLNK(info.st_mode) and info.st_mode & 0o222:
            raise ValueError(f"M1 source snapshot contains a writable entry: {path}")
    if _run_git(snapshot, ["rev-parse", "HEAD"]).decode("ascii").strip() != source_commit:
        raise ValueError("M1 source snapshot commit differs")
    if _run_git(
        snapshot,
        [
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignore-submodules=none",
        ],
    ):
        raise ValueError("M1 source snapshot is not a clean committed checkout")


def immutable_container_configuration(initial: dict[str, Any], final: dict[str, Any]) -> None:
    for field in ("Config", "Path", "Args", "Image"):
        if initial.get(field) != final.get(field):
            raise ValueError(f"Docker container immutable field changed: {field}")
    initial_host = initial.get("HostConfig")
    final_host = final.get("HostConfig")
    if not isinstance(initial_host, dict) or not isinstance(final_host, dict):
        raise ValueError("Docker container immutable field changed: HostConfig")
    normalized_initial_host = dict(initial_host)
    normalized_final_host = dict(final_host)
    for host in (normalized_initial_host, normalized_final_host):
        # Docker may re-serialize its default pointer-bool from false to null
        # after the first start/stop.  Both mean that OOM killing is enabled;
        # true is never accepted by this equivalence rule.
        if host.get("OomKillDisable") not in (None, False):
            raise ValueError("Docker container immutable field changed: HostConfig")
        host["OomKillDisable"] = False
    if normalized_initial_host != normalized_final_host:
        raise ValueError("Docker container immutable field changed: HostConfig")
    initial_mounts = initial.get("Mounts")
    final_mounts = final.get("Mounts")
    if (
        not isinstance(initial_mounts, list)
        or not isinstance(final_mounts, list)
        or not all(isinstance(item, dict) for item in [*initial_mounts, *final_mounts])
        or sorted(_canonical(item) for item in initial_mounts)
        != sorted(_canonical(item) for item in final_mounts)
    ):
        raise ValueError("Docker container immutable field changed: Mounts")


def exact_mounts(
    document: dict[str, Any], expected: dict[str, tuple[str, bool]]
) -> None:
    mounts = document.get("Mounts")
    if not isinstance(mounts, list) or len(mounts) != len(expected):
        raise ValueError("Docker mount set is not exact")
    by_destination: dict[str, dict[str, Any]] = {}
    for item in mounts:
        if not isinstance(item, dict) or not isinstance(item.get("Destination"), str):
            raise ValueError("Docker mount record is malformed")
        destination = item["Destination"]
        if destination in by_destination:
            raise ValueError("Docker mount destination is duplicated")
        by_destination[destination] = item
    if set(by_destination) != set(expected):
        raise ValueError("Docker mount destinations are not exact")
    for destination, (source, writable) in expected.items():
        item = by_destination[destination]
        if (
            item.get("Type") != "bind"
            or item.get("Source") != source
            or item.get("RW") is not writable
            or item.get("Mode") != ("rw" if writable else "ro")
            or item.get("Propagation") != "rprivate"
        ):
            raise ValueError(f"Docker mount is invalid: {destination}")


def rename_noreplace(source: Path, destination: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    syscall = getattr(libc, "syscall", None)
    if syscall is None:
        raise OSError(errno.ENOSYS, "renameat2 syscall is unavailable")
    # The completed runtime lock requires Linux x86_64; SYS_renameat2 is 316
    # on that ABI. Calling the syscall directly retains RENAME_NOREPLACE.
    syscall.restype = ctypes.c_long
    if syscall(
        316,
        -100,
        ctypes.c_char_p(os.fsencode(source)),
        -100,
        ctypes.c_char_p(os.fsencode(destination)),
        1,
    ) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))
