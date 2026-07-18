#!/usr/bin/env python3
"""Committed-tree qualification identity and checkout/reuse verification.

The content vector in this module is deliberately a pure function of a Git
commit and its objects.  It never reads the index or working-tree file bytes.
Live checkout equality is a separate gate so a dirty checkout can still emit
honest diagnostic provenance without changing the identity attributed to its
recorded commit.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any


QUALIFICATION_POLICY_PATH = "network/config/qualification_path_ownership.json"
QUALIFICATION_POLICY_ID = "q0_q1_q2_granular/v1"
QUALIFICATION_VECTOR_CONTRACT = "ams.qualification-content-vector/v1"
QUALIFICATION_CHECKOUT_CONTRACT = "ams.qualification-checkout/v1"
QUALIFICATION_CONSUMPTION_CONTRACT = "ams.qualification-consumption/v1"
DEFERRED_M0_CAPABILITY_MODE = "host_final_isolated_exact_image"
BOUNDED_ROOT_IN_RUNTIME_MODE = "bounded_root_in_runtime"
BOUNDED_ROOT_IN_RUNTIME_PROFILES = frozenset(
    {
        "m2_component",
        "m3_component",
        "m4_capacity_prerequisite",
        "m4_component",
    }
)
BOUNDED_ROOT_UID = 0
BOUNDED_ROOT_GID = 1000
BOUNDED_ROOT_CAPABILITY_MASK = "0000000000203005"
BOUNDED_ROOT_NO_NEW_PRIVS = 1
QUALIFICATION_NODES = tuple(f"Q{index}" for index in range(9))
MUTABLE_STATUS_OUTPUTS = frozenset(
    {
        "network/NEXT_TASK.md",
        "network/PROGRESS.md",
        "network/VALIDATION_REPORT.md",
    }
)
PROFILE_CONSUMED_NODES: dict[str, tuple[str, ...]] = {
    "diagnostic": (),
    "m0": ("Q0",),
    "m1_component": ("Q0", "Q1"),
    "flight_capacity_prerequisite": ("Q0", "Q1"),
    "m2_component": ("Q0", "Q1", "Q2"),
    "m3_component": ("Q0", "Q1", "Q2", "Q3"),
    "m4_capacity_prerequisite": ("Q0", "Q1", "Q2", "Q3", "Q4"),
    "m4_component": ("Q0", "Q1", "Q2", "Q3", "Q4"),
}
_OBJECT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_FULL_SHA256 = re.compile(r"[0-9a-f]{64}")
_GIT_EXECUTABLE = "/usr/bin/git"


class QualificationIdentityError(ValueError):
    """Raised when committed identity or live-checkout equality fails closed."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _domain_hash(domain: str, payload: bytes) -> str:
    prefix = domain.encode("ascii")
    return hashlib.sha256(
        len(prefix).to_bytes(4, "big")
        + prefix
        + len(payload).to_bytes(8, "big")
        + payload
    ).hexdigest()


def _git_environment() -> dict[str, str]:
    # Inherited Git redirection/configuration variables must not be able to
    # replace the repository, index, object database, worktree, or diff
    # implementation used for an acceptance identity.
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment.update(
        {
            "LC_ALL": "C",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_LITERAL_PATHSPECS": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
        }
    )
    return environment


def _git_completed(
    root: Path,
    arguments: list[str],
    *,
    timeout: int = 30,
) -> subprocess.CompletedProcess[bytes]:
    safe_root = root.resolve(strict=True)
    try:
        return subprocess.run(
            [
                _GIT_EXECUTABLE,
                "-c",
                f"safe.directory={safe_root}",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.untrackedCache=false",
                "-C",
                str(root),
                *arguments,
            ],
            check=False,
            capture_output=True,
            timeout=timeout,
            env=_git_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise QualificationIdentityError(
            f"Git command could not execute: {' '.join(arguments)}: {exc}"
        ) from exc


def _git(root: Path, arguments: list[str], *, timeout: int = 30) -> bytes:
    result = _git_completed(root, arguments, timeout=timeout)
    if result.returncode != 0:
        diagnostic = result.stderr.decode("utf-8", errors="replace").strip()
        raise QualificationIdentityError(
            f"Git command failed ({result.returncode}): {' '.join(arguments)}"
            + (f": {diagnostic[:500]}" if diagnostic else "")
        )
    return result.stdout


def _repository_root(root: Path) -> Path:
    if not isinstance(root, Path):
        raise QualificationIdentityError("repository root must be a pathlib.Path")
    try:
        candidate = root.resolve(strict=True)
    except OSError as exc:
        raise QualificationIdentityError(f"repository root is unavailable: {exc}") from exc
    top_level_raw = _git(candidate, ["rev-parse", "--show-toplevel"]).strip()
    try:
        top_level = Path(top_level_raw.decode("utf-8", errors="strict")).resolve(
            strict=True
        )
    except (OSError, UnicodeDecodeError) as exc:
        raise QualificationIdentityError(f"Git top-level is invalid: {exc}") from exc
    if top_level != candidate:
        raise QualificationIdentityError(
            f"qualification root {candidate} is not the Git top-level {top_level}"
        )
    return candidate


def _resolve_commit(root: Path, commit: str) -> str:
    if not isinstance(commit, str) or (
        commit != "HEAD"
        and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit) is None
    ):
        raise QualificationIdentityError("commit must be HEAD or one full lowercase object ID")
    resolved_raw = _git(root, ["rev-parse", "--verify", f"{commit}^{{commit}}"]).strip()
    try:
        resolved = resolved_raw.decode("ascii", errors="strict")
    except UnicodeDecodeError as exc:
        raise QualificationIdentityError("resolved commit is not ASCII") from exc
    if _OBJECT_ID.fullmatch(resolved) is None:
        raise QualificationIdentityError("resolved commit is not one full Git object ID")
    return resolved


def _validate_git_path(raw_path: bytes) -> str:
    try:
        value = raw_path.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise QualificationIdentityError(
            "tracked Git path is not canonical UTF-8 and cannot be JSON-bound"
        ) from exc
    pure = PurePosixPath(value)
    if (
        not value
        or value.startswith("/")
        or "\x00" in value
        or pure.is_absolute()
        or any(part in {"", ".", ".."} for part in pure.parts)
        or pure.as_posix() != value
    ):
        raise QualificationIdentityError(f"tracked Git path is unsafe: {value!r}")
    return value


def _committed_entries(root: Path, commit: str) -> tuple[str, list[dict[str, Any]]]:
    resolved = _resolve_commit(root, commit)
    raw_tree = _git(
        root,
        ["ls-tree", "-r", "-z", "--full-tree", resolved],
        timeout=60,
    )
    records = raw_tree.split(b"\0")
    if records and records[-1] == b"":
        records.pop()
    entries: list[dict[str, Any]] = []
    previous_path: str | None = None
    for raw_record in records:
        header, separator, raw_path = raw_record.partition(b"\t")
        fields = header.split(b" ")
        if not separator or len(fields) != 3:
            raise QualificationIdentityError("git ls-tree returned a malformed record")
        try:
            mode = fields[0].decode("ascii", errors="strict")
            object_type = fields[1].decode("ascii", errors="strict")
            object_id = fields[2].decode("ascii", errors="strict")
        except UnicodeDecodeError as exc:
            raise QualificationIdentityError("git ls-tree header is not ASCII") from exc
        path = _validate_git_path(raw_path)
        if previous_path is not None and path <= previous_path:
            raise QualificationIdentityError("committed Git paths are duplicate or unordered")
        previous_path = path
        if mode not in {"100644", "100755", "120000", "160000"}:
            raise QualificationIdentityError(
                f"unsupported committed Git mode {mode!r} for {path}"
            )
        if _OBJECT_ID.fullmatch(object_id) is None:
            raise QualificationIdentityError(f"invalid Git object ID for {path}")
        if mode == "160000":
            if object_type != "commit":
                raise QualificationIdentityError(f"gitlink is not a commit object: {path}")
            entries.append(
                {
                    "path": path,
                    "git_mode": mode,
                    "object_type": object_type,
                    "git_object_id": object_id,
                    "kind": "gitlink",
                    "blob_size": None,
                    "blob_sha256": None,
                }
            )
            continue
        if object_type != "blob":
            raise QualificationIdentityError(f"non-gitlink entry is not a blob: {path}")
        blob = _git(root, ["cat-file", "blob", object_id], timeout=60)
        entries.append(
            {
                "path": path,
                "git_mode": mode,
                "object_type": object_type,
                "git_object_id": object_id,
                "kind": "symlink" if mode == "120000" else "regular",
                "blob_size": len(blob),
                "blob_sha256": hashlib.sha256(blob).hexdigest(),
            }
        )
    if not entries:
        raise QualificationIdentityError("committed Git tree is empty")
    return resolved, entries


def _validate_policy_path(value: Any) -> str:
    if not isinstance(value, str):
        raise QualificationIdentityError("qualification owner path is not a string")
    try:
        return _validate_git_path(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as exc:
        raise QualificationIdentityError(
            "qualification owner path is not canonical UTF-8"
        ) from exc


def _granular_policy_assignments(
    policy: Any,
    committed_paths: set[str],
) -> dict[str, str]:
    """Validate the exact-path policy and assign every technical Git entry.

    Unlisted paths deliberately fall back to Q0.  A new tracked input therefore
    cannot silently land later than its actual milestone impact; assigning it
    to Q1 or Q2 requires a sorted explicit entry in this Q0-owned policy.
    """

    expected_keys = {
        "schema_version",
        "contract",
        "policy_id",
        "mutable_status_exclusions",
        "selective_descendant_reuse_allowed",
        "profile_consumption",
        "default_owner",
        "explicit_owners",
    }
    if not isinstance(policy, dict) or set(policy) != expected_keys:
        raise QualificationIdentityError(
            "qualification path-ownership policy schema is not exact"
        )
    if (
        policy.get("schema_version") != 2
        or policy.get("contract") != QUALIFICATION_POLICY_ID
        or policy.get("policy_id") != QUALIFICATION_POLICY_ID
        or policy.get("mutable_status_exclusions")
        != sorted(MUTABLE_STATUS_OUTPUTS)
        or policy.get("selective_descendant_reuse_allowed") is not True
        or policy.get("default_owner") != "Q0"
        or policy.get("profile_consumption")
        != {
            profile: list(nodes)
            for profile, nodes in PROFILE_CONSUMED_NODES.items()
        }
    ):
        raise QualificationIdentityError(
            "qualification path-ownership policy contract is not exact"
        )

    explicit = policy.get("explicit_owners")
    if not isinstance(explicit, dict) or set(explicit) != set(QUALIFICATION_NODES):
        raise QualificationIdentityError(
            "qualification explicit-owner node map is not exact"
        )
    if explicit.get("Q0") != []:
        raise QualificationIdentityError(
            "explicit Q0 paths are forbidden because Q0 is the fail-closed default"
        )

    explicit_assignments: dict[str, str] = {}
    for node in QUALIFICATION_NODES:
        values = explicit.get(node)
        if not isinstance(values, list) or any(
            not isinstance(value, str) for value in values
        ):
            raise QualificationIdentityError(
                f"qualification explicit-owner list for {node} is malformed"
            )
        paths = [_validate_policy_path(value) for value in values]
        if paths != sorted(paths) or len(paths) != len(set(paths)):
            raise QualificationIdentityError(
                f"qualification explicit-owner list for {node} is not sorted and unique"
            )
        for path in paths:
            if path in MUTABLE_STATUS_OUTPUTS:
                raise QualificationIdentityError(
                    f"mutable status path cannot have a Q owner: {path}"
                )
            if path == QUALIFICATION_POLICY_PATH and node != "Q0":
                raise QualificationIdentityError(
                    "qualification policy must remain owned by Q0"
                )
            if path not in committed_paths:
                raise QualificationIdentityError(
                    f"explicitly owned path is absent from the committed tree: {path}"
                )
            previous = explicit_assignments.get(path)
            if previous is not None:
                raise QualificationIdentityError(
                    f"tracked path has multiple explicit owners: {path}: {previous}, {node}"
                )
            explicit_assignments[path] = node

    assignments = {
        path: explicit_assignments.get(path, "Q0")
        for path in sorted(committed_paths - set(MUTABLE_STATUS_OUTPUTS))
    }
    if set(assignments) != committed_paths - set(MUTABLE_STATUS_OUTPUTS):
        raise QualificationIdentityError(
            "qualification policy did not assign every non-status tracked path"
        )
    if assignments.get(QUALIFICATION_POLICY_PATH) != "Q0":
        raise QualificationIdentityError("qualification policy is not a Q0 input")
    return assignments


def qualification_content_vector(
    root: Path,
    commit: str = "HEAD",
) -> dict[str, Any]:
    """Compute the exact Q0..Q8 vector from committed Git objects only."""

    repository = _repository_root(root)
    resolved, committed_entries = _committed_entries(repository, commit)
    by_path = {entry["path"]: entry for entry in committed_entries}
    if set(MUTABLE_STATUS_OUTPUTS) - set(by_path):
        missing = sorted(set(MUTABLE_STATUS_OUTPUTS) - set(by_path))
        raise QualificationIdentityError(
            "the three tracked status exclusions are incomplete: " + ", ".join(missing)
        )
    policy_entry = by_path.get(QUALIFICATION_POLICY_PATH)
    if not isinstance(policy_entry, dict) or policy_entry.get("object_type") != "blob":
        raise QualificationIdentityError("qualification policy is absent from the committed tree")
    policy_raw = _git(
        repository,
        ["cat-file", "blob", str(policy_entry["git_object_id"])],
    )
    try:
        policy = json.loads(policy_raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QualificationIdentityError(f"qualification policy is invalid JSON: {exc}") from exc
    assignments = _granular_policy_assignments(policy, set(by_path))

    entry_manifest: list[dict[str, Any]] = []
    for committed in committed_entries:
        if committed["path"] in MUTABLE_STATUS_OUTPUTS:
            continue
        entry_manifest.append(
            {**committed, "owner": assignments[committed["path"]]}
        )
    if not entry_manifest:
        raise QualificationIdentityError("qualification entry manifest is empty")
    if QUALIFICATION_POLICY_PATH not in {entry["path"] for entry in entry_manifest}:
        raise QualificationIdentityError("qualification policy is not a Q0 input")

    manifest_assignments = {
        entry["path"]: entry["owner"] for entry in entry_manifest
    }
    if manifest_assignments != assignments:
        raise QualificationIdentityError(
            "qualification entry manifest does not exactly match policy assignments"
        )
    node_hashes: dict[str, str] = {}
    records: list[dict[str, Any]] = []
    for node in QUALIFICATION_NODES:
        owned = [entry for entry in entry_manifest if entry["owner"] == node]
        manifest_hash = _domain_hash(
            f"{QUALIFICATION_VECTOR_CONTRACT}/node/{node}",
            _canonical_json(owned),
        )
        node_hashes[node] = manifest_hash
        records.append(
            {
                "owner": node,
                "path_count": len(owned),
                "content_sha256": manifest_hash,
            }
        )
    policy_sha256 = hashlib.sha256(policy_raw).hexdigest()
    assignments_sha256 = _domain_hash(
        f"{QUALIFICATION_VECTOR_CONTRACT}/assignments",
        _canonical_json(manifest_assignments),
    )
    vector_payload = {
        "algorithm": QUALIFICATION_VECTOR_CONTRACT,
        "policy_id": QUALIFICATION_POLICY_ID,
        "policy_sha256": policy_sha256,
        "assignments_sha256": assignments_sha256,
        "node_hashes": node_hashes,
    }
    return {
        "schema_version": 1,
        "contract": QUALIFICATION_VECTOR_CONTRACT,
        "git_commit": resolved,
        "policy_id": QUALIFICATION_POLICY_ID,
        "policy_path": QUALIFICATION_POLICY_PATH,
        "policy_sha256": policy_sha256,
        "mutable_status_exclusions": sorted(MUTABLE_STATUS_OUTPUTS),
        "default_owner": "Q0",
        "selective_reuse": True,
        "assignments_sha256": assignments_sha256,
        "entry_manifest": entry_manifest,
        "assignments": manifest_assignments,
        "records": records,
        "node_hashes": node_hashes,
        "vector_sha256": _domain_hash(
            f"{QUALIFICATION_VECTOR_CONTRACT}/vector",
            _canonical_json(vector_payload),
        ),
    }


def expected_consumed_nodes(profile: str) -> tuple[str, ...]:
    try:
        return PROFILE_CONSUMED_NODES[profile]
    except (KeyError, TypeError) as exc:
        raise QualificationIdentityError(
            f"unknown qualification profile: {profile!r}"
        ) from exc


def qualification_consumption(
    vector: dict[str, Any],
    profile: str,
) -> dict[str, Any]:
    """Build the only valid consumption declaration for one known profile."""

    if not isinstance(vector, dict):
        raise QualificationIdentityError("qualification vector is malformed")
    nodes = expected_consumed_nodes(profile)
    node_hashes = vector.get("node_hashes")
    if (
        vector.get("schema_version") != 1
        or vector.get("contract") != QUALIFICATION_VECTOR_CONTRACT
        or not isinstance(node_hashes, dict)
        or set(node_hashes) != set(QUALIFICATION_NODES)
        or not all(_FULL_SHA256.fullmatch(str(value or "")) for value in node_hashes.values())
        or _OBJECT_ID.fullmatch(str(vector.get("git_commit") or "")) is None
        or _FULL_SHA256.fullmatch(str(vector.get("vector_sha256") or "")) is None
        or _FULL_SHA256.fullmatch(str(vector.get("policy_sha256") or "")) is None
    ):
        raise QualificationIdentityError("qualification vector is malformed")
    return {
        "schema_version": 1,
        "contract": QUALIFICATION_CONSUMPTION_CONTRACT,
        "profile": profile,
        "consumed_nodes": list(nodes),
        "consumed_node_sha256": {node: node_hashes[node] for node in nodes},
        "vector_sha256": vector["vector_sha256"],
        "policy_sha256": vector["policy_sha256"],
        "git_commit": vector["git_commit"],
    }


def validate_qualification_consumption(
    record: Any,
    vector: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(record, dict) or not isinstance(record.get("profile"), str):
        raise QualificationIdentityError(
            "qualification consumption record is missing or malformed"
        )
    expected = qualification_consumption(vector, record["profile"])
    if record != expected:
        raise QualificationIdentityError(
            "qualification consumption does not exactly bind its profile and node hashes"
        )
    return expected


def qualification_prefix_identity(
    vector: Any,
    consumed_nodes: Any,
) -> dict[str, Any]:
    """Return the reusable identity of exactly one consumed Q-prefix.

    Full-vector hashes deliberately include future nodes.  They are therefore
    useful for identifying one complete commit, but they must not be used to
    decide whether an already-qualified prefix can be reused after a future
    node changes.  Policy bytes remain part of Q0 and are intentionally bound:
    changing ownership semantics invalidates every prefix.
    """

    if not isinstance(vector, dict):
        raise QualificationIdentityError("qualification vector is malformed")
    if not isinstance(consumed_nodes, (list, tuple)) or any(
        not isinstance(node, str) for node in consumed_nodes
    ):
        raise QualificationIdentityError("consumed qualification nodes are malformed")
    nodes = tuple(consumed_nodes)
    if nodes not in set(PROFILE_CONSUMED_NODES.values()) or nodes == ():
        raise QualificationIdentityError(
            "consumed qualification nodes are not one exact non-empty profile prefix"
        )
    node_hashes = vector.get("node_hashes")
    if (
        vector.get("schema_version") != 1
        or vector.get("contract") != QUALIFICATION_VECTOR_CONTRACT
        or vector.get("policy_id") != QUALIFICATION_POLICY_ID
        or vector.get("policy_path") != QUALIFICATION_POLICY_PATH
        or _FULL_SHA256.fullmatch(str(vector.get("policy_sha256") or "")) is None
        or vector.get("mutable_status_exclusions") != sorted(MUTABLE_STATUS_OUTPUTS)
        or vector.get("default_owner") != "Q0"
        or vector.get("selective_reuse") is not True
        or not isinstance(node_hashes, dict)
        or set(node_hashes) != set(QUALIFICATION_NODES)
        or not all(
            _FULL_SHA256.fullmatch(str(node_hashes.get(node) or ""))
            for node in QUALIFICATION_NODES
        )
    ):
        raise QualificationIdentityError("qualification vector prefix identity is malformed")
    return {
        "schema_version": 1,
        "contract": QUALIFICATION_VECTOR_CONTRACT,
        "policy_id": QUALIFICATION_POLICY_ID,
        "policy_path": QUALIFICATION_POLICY_PATH,
        "policy_sha256": vector["policy_sha256"],
        "mutable_status_exclusions": sorted(MUTABLE_STATUS_OUTPUTS),
        "default_owner": "Q0",
        "selective_reuse": True,
        "consumed_nodes": list(nodes),
        "consumed_node_sha256": {node: node_hashes[node] for node in nodes},
    }


def qualification_prefixes_equal(
    left: Any,
    right: Any,
    consumed_nodes: Any,
) -> bool:
    """Compare only the policy-bound hashes consumed by one qualified prefix."""

    return qualification_prefix_identity(
        left, consumed_nodes
    ) == qualification_prefix_identity(right, consumed_nodes)


def _safe_checkout_path(root: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    current = root
    for part in pure.parts[:-1]:
        current = current / part
        try:
            info = current.lstat()
        except OSError as exc:
            raise QualificationIdentityError(
                f"tracked parent is unavailable for {relative}: {exc}"
            ) from exc
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise QualificationIdentityError(
                f"tracked parent is not a real directory for {relative}"
            )
    return root.joinpath(*pure.parts)


def _regular_file_sha256(path: Path) -> tuple[int, int, str]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise QualificationIdentityError(f"tracked regular file cannot be opened: {path}: {exc}") from exc
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise QualificationIdentityError(
                f"tracked path is not a single-link regular file: {path}"
            )
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise QualificationIdentityError(f"short read from tracked file: {path}")
            digest.update(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise QualificationIdentityError(f"tracked file grew while hashing: {path}")
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    stable_fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_nlink",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
        raise QualificationIdentityError(f"tracked file changed while hashing: {path}")
    return before.st_mode, before.st_size, digest.hexdigest()


def qualification_checkout_identity(
    root: Path,
    commit: str = "HEAD",
) -> dict[str, Any]:
    """Inspect live checkout equality separately from the committed vector."""

    repository = _repository_root(root)
    resolved, entries = _committed_entries(repository, commit)
    failures: list[str] = []
    head = _resolve_commit(repository, "HEAD")
    if head != resolved:
        failures.append("checkout HEAD differs from the expected commit")

    status_raw = _git(
        repository,
        [
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignore-submodules=none",
        ],
        timeout=60,
    )
    raw_status_records = status_raw.split(b"\0")
    if raw_status_records and raw_status_records[-1] == b"":
        raw_status_records.pop()
    try:
        status_records = [
            item.decode("utf-8", errors="strict") for item in raw_status_records
        ]
    except UnicodeDecodeError as exc:
        raise QualificationIdentityError("Git status contains a non-UTF-8 path") from exc
    if status_records:
        failures.append("checkout has staged, unstaged, untracked, or dirty-submodule state")

    entry_failures: list[str] = []
    submodule_failures: list[str] = []
    submodule_count = 0
    for entry in entries:
        relative = str(entry["path"])
        try:
            path = _safe_checkout_path(repository, relative)
            if entry["kind"] == "regular":
                mode, size, digest = _regular_file_sha256(path)
                executable = bool(mode & 0o111)
                expected_executable = entry["git_mode"] == "100755"
                if (
                    size != entry["blob_size"]
                    or digest != entry["blob_sha256"]
                    or executable != expected_executable
                ):
                    raise QualificationIdentityError(
                        f"working-tree regular-file identity differs: {relative}"
                    )
            elif entry["kind"] == "symlink":
                info = path.lstat()
                if not stat.S_ISLNK(info.st_mode):
                    raise QualificationIdentityError(
                        f"working-tree symlink type differs: {relative}"
                    )
                target = os.readlink(os.fsencode(path))
                if (
                    len(target) != entry["blob_size"]
                    or hashlib.sha256(target).hexdigest() != entry["blob_sha256"]
                ):
                    raise QualificationIdentityError(
                        f"working-tree symlink target differs: {relative}"
                    )
            elif entry["kind"] == "gitlink":
                submodule_count += 1
                info = path.lstat()
                if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    raise QualificationIdentityError(
                        f"working-tree gitlink is not a real directory: {relative}"
                    )
                submodule_top = Path(
                    _git(path, ["rev-parse", "--show-toplevel"])
                    .decode("utf-8", errors="strict")
                    .strip()
                ).resolve(strict=True)
                if submodule_top != path.resolve(strict=True):
                    raise QualificationIdentityError(
                        f"gitlink is not a standalone submodule checkout: {relative}"
                    )
                submodule_head = _resolve_commit(path, "HEAD")
                if submodule_head != entry["git_object_id"]:
                    raise QualificationIdentityError(
                        f"submodule commit differs: {relative}"
                    )
                submodule_status = _git(
                    path,
                    [
                        "status",
                        "--porcelain=v1",
                        "-z",
                        "--untracked-files=all",
                        "--ignore-submodules=none",
                    ],
                    timeout=60,
                )
                if submodule_status:
                    raise QualificationIdentityError(
                        f"submodule checkout is dirty: {relative}"
                    )
            else:
                raise QualificationIdentityError(
                    f"unknown committed entry kind: {entry['kind']!r}"
                )
        except (OSError, UnicodeDecodeError, QualificationIdentityError) as exc:
            message = str(exc)
            entry_failures.append(message)
            if entry["kind"] == "gitlink":
                submodule_failures.append(message)
    failures.extend(entry_failures)
    return {
        "schema_version": 1,
        "contract": QUALIFICATION_CHECKOUT_CONTRACT,
        "expected_commit": resolved,
        "head_commit": head,
        "git_status": status_records,
        "checked_entry_count": len(entries),
        "submodule_count": submodule_count,
        "tree_objects_equal": not entry_failures,
        "submodules_clean": not submodule_failures,
        "checkout_equal": not failures,
        "failures": failures,
    }


def require_qualification_checkout_equal(
    root: Path,
    commit: str = "HEAD",
) -> dict[str, Any]:
    record = qualification_checkout_identity(root, commit)
    if record.get("checkout_equal") is not True:
        raise QualificationIdentityError(
            "qualification checkout does not equal its commit: "
            + "; ".join(str(value) for value in record.get("failures", []))
        )
    return record


def validate_recorded_checkout_identity(
    record: Any,
    vector: dict[str, Any],
) -> dict[str, Any]:
    """Require an acceptance record proving execution from its exact checkout."""

    if not isinstance(vector, dict):
        raise QualificationIdentityError("qualification vector entry manifest is malformed")
    manifest = vector.get("entry_manifest")
    if not isinstance(manifest, list):
        raise QualificationIdentityError("qualification vector entry manifest is malformed")
    expected_keys = {
        "schema_version",
        "contract",
        "expected_commit",
        "head_commit",
        "git_status",
        "checked_entry_count",
        "submodule_count",
        "tree_objects_equal",
        "submodules_clean",
        "checkout_equal",
        "failures",
    }
    if not isinstance(record, dict) or set(record) != expected_keys:
        raise QualificationIdentityError(
            "qualification checkout record is missing or malformed"
        )
    commit = vector.get("git_commit")
    expected_entry_count = len(manifest) + len(MUTABLE_STATUS_OUTPUTS)
    expected_submodules = sum(
        isinstance(entry, dict) and entry.get("kind") == "gitlink"
        for entry in manifest
    )
    if (
        record.get("schema_version") != 1
        or record.get("contract") != QUALIFICATION_CHECKOUT_CONTRACT
        or record.get("expected_commit") != commit
        or record.get("head_commit") != commit
        or record.get("git_status") != []
        or record.get("checked_entry_count") != expected_entry_count
        or record.get("submodule_count") != expected_submodules
        or record.get("tree_objects_equal") is not True
        or record.get("submodules_clean") is not True
        or record.get("checkout_equal") is not True
        or record.get("failures") != []
    ):
        raise QualificationIdentityError(
            "recorded qualification checkout was not exact and clean"
        )
    return record


def _changed_paths(root: Path, base: str, descendant: str) -> list[str]:
    raw = _git(
        root,
        [
            "diff",
            "--no-ext-diff",
            "--name-only",
            "--no-renames",
            "-z",
            base,
            descendant,
            "--",
        ],
        timeout=60,
    )
    records = raw.split(b"\0")
    if records and records[-1] == b"":
        records.pop()
    paths = [_validate_git_path(value) for value in records]
    if len(paths) != len(set(paths)):
        raise QualificationIdentityError("descendant diff contains duplicate paths")
    return sorted(paths)


def verify_recorded_qualification(
    root: Path,
    recorded_vector: Any,
    recorded_consumption: Any,
) -> dict[str, Any]:
    """Verify recorded identity and the current clean checkout relationship.

    A different current commit is accepted only as an exact three-file
    status-only descendant.  The current vector is recomputed from that commit
    and every profile-consumed node hash must remain equal.
    """

    if not isinstance(recorded_vector, dict):
        raise QualificationIdentityError("qualification vector is missing or malformed")
    recorded_commit = recorded_vector.get("git_commit")
    if not isinstance(recorded_commit, str) or _OBJECT_ID.fullmatch(recorded_commit) is None:
        raise QualificationIdentityError("qualification vector commit is malformed")
    expected_vector = qualification_content_vector(root, recorded_commit)
    if recorded_vector != expected_vector:
        raise QualificationIdentityError(
            "recorded qualification vector differs from its recorded commit"
        )
    expected_consumption = validate_qualification_consumption(
        recorded_consumption, expected_vector
    )
    current_checkout = require_qualification_checkout_equal(root, "HEAD")
    current_commit = str(current_checkout["head_commit"])
    if current_commit == recorded_commit:
        relationship = "same_commit"
        changed_paths: list[str] = []
        current_vector = expected_vector
    else:
        ancestor = _git_completed(
            _repository_root(root),
            ["merge-base", "--is-ancestor", recorded_commit, current_commit],
        )
        if ancestor.returncode != 0:
            raise QualificationIdentityError(
                "recorded run commit is not an ancestor of the validator checkout"
            )
        changed_paths = _changed_paths(
            _repository_root(root), recorded_commit, current_commit
        )
        if set(changed_paths) != set(MUTABLE_STATUS_OUTPUTS):
            raise QualificationIdentityError(
                "descendant is not an exact three-path status-only commit"
            )
        current_vector = qualification_content_vector(root, current_commit)
        relationship = "status_only_descendant"

    consumed_nodes = expected_consumption["consumed_nodes"]
    recorded_hashes = expected_vector["node_hashes"]
    current_hashes = current_vector["node_hashes"]
    if any(recorded_hashes[node] != current_hashes[node] for node in consumed_nodes):
        raise QualificationIdentityError(
            "one or more profile-consumed qualification nodes changed"
        )
    if relationship == "status_only_descendant" and (
        expected_vector["vector_sha256"] != current_vector["vector_sha256"]
        or expected_vector["policy_sha256"] != current_vector["policy_sha256"]
    ):
        raise QualificationIdentityError(
            "status-only descendant changed the non-status qualification vector"
        )
    return {
        "schema_version": 1,
        "recorded_commit": recorded_commit,
        "current_commit": current_commit,
        "relationship": relationship,
        "changed_paths": changed_paths,
        "profile": expected_consumption["profile"],
        "consumed_nodes": list(consumed_nodes),
        "consumed_node_sha256": dict(
            expected_consumption["consumed_node_sha256"]
        ),
        "checkout": current_checkout,
    }


def is_exact_deferred_m0_capability_mode(
    consumption: Any,
    qualification_mode: Any,
) -> bool:
    """Return true only for M0/Q0's isolated host-final capability probe."""

    consumed_hashes = (
        consumption.get("consumed_node_sha256")
        if isinstance(consumption, dict)
        else None
    )
    return (
        qualification_mode == DEFERRED_M0_CAPABILITY_MODE
        and isinstance(consumption, dict)
        and consumption.get("profile") == "m0"
        and consumption.get("consumed_nodes") == ["Q0"]
        and isinstance(consumed_hashes, dict)
        and set(consumed_hashes) == {"Q0"}
    )


def is_exact_bounded_root_capability_mode(
    consumption: Any,
    qualification_mode: Any,
) -> bool:
    """Return true only for exact M2--M4 TUN acceptance profiles."""

    if qualification_mode != BOUNDED_ROOT_IN_RUNTIME_MODE or not isinstance(
        consumption, dict
    ):
        return False
    profile = consumption.get("profile")
    consumed_hashes = consumption.get("consumed_node_sha256")
    if profile not in BOUNDED_ROOT_IN_RUNTIME_PROFILES:
        return False
    expected = list(expected_consumed_nodes(profile))
    return (
        consumption.get("consumed_nodes") == expected
        and isinstance(consumed_hashes, dict)
        and set(consumed_hashes) == set(expected)
        and all(
            _FULL_SHA256.fullmatch(str(consumed_hashes.get(node) or ""))
            for node in expected
        )
    )
