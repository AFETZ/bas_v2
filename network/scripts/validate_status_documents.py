#!/usr/bin/env python3
"""Validate the three post-evidence mutable status records.

The command-line interface deliberately has no path overrides.  A live result
is meaningful only for the canonical files and canonical receipt in this Git
worktree.  The pure ``status_documents_status`` function remains available for
fixture tests of the milestone state machine.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any

import yaml


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from network.scripts.host_finalization_common import (  # noqa: E402
    M0_CAPABILITY_COMMAND_SCRIPT,
    M0_CAPABILITY_STDOUT,
)

STATUS_PATHS = (
    "network/PROGRESS.md",
    "network/VALIDATION_REPORT.md",
    "network/NEXT_TASK.md",
)
PLAN_PATH = "doc/network_radio_integration_plan_v3.md"
POLICY_PATH = "network/config/qualification_path_ownership.json"
M0_RECEIPT_NAME = "m0_host_final_receipt.json"
M1_RECEIPT_NAME = "m1_host_final_receipt.json"
STATUS_METADATA_CONTRACT = "ams.live-status/v1"
STATUS_METADATA_CONTRACT_V2 = "ams.live-status/v2"
STATUS_METADATA_CONTRACTS = {
    1: STATUS_METADATA_CONTRACT,
    2: STATUS_METADATA_CONTRACT_V2,
    3: "ams.live-status/v3",
    4: "ams.live-status/v4",
    5: "ams.live-status/v5",
}
STATUS_LINT_CONTRACT = "ams.live-status-lint/v1"
RECEIPT_CONTRACT = "ams.m0.host-final-receipt/v1"
M1_RECEIPT_CONTRACT = "ams.m1.host-final-receipt/v1"
M1_RESULT_CONTRACT = "ams.m1.health/v3"
METADATA_BEGIN = "<!-- AMS_LIVE_STATUS_METADATA_BEGIN\n"
METADATA_END = "\nAMS_LIVE_STATUS_METADATA_END -->"
MAX_STATUS_BYTES = 4 * 1024 * 1024
MAX_RECEIPT_BYTES = 64 * 1024 * 1024
MAX_CONTROL_BYTES = 32 * 1024 * 1024
MAX_M1_ARTIFACT_BYTES = 128 * 1024 * 1024
GIT_BINARY = "/usr/bin/git"

ALLOWED = {"not_started", "in_progress", "failed", "blocked_external", "passed"}
SHA1 = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")
IMAGE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
SAFE_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
ROW_PATTERN = re.compile(r"(?m)^\| (M\d(?:–M\d)?) \| `([a-z_]+)` \|")
COUNT_PATTERN = re.compile(
    r"Fully closed(?:\s+sequential)?\s+milestones:\s+\*\*(\d+)\*\*",
    re.IGNORECASE,
)
READY_PATTERN = re.compile(r"Customer-ready:\s+\*\*(true|false)\*\*", re.IGNORECASE)
UTC_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z"
)
FORMAL_GATE_NAMES = {
    "dependency_check",
    "runtime_lock",
    "validation_adversarial_suite",
    "provenance",
    "host_final",
}
RETAINED_CONTROL_FILE_NAMES = (
    "retained/prestart_inspection_record.json",
    "retained/initial_container_inspect.json",
    "retained/initial_image_inspect.json",
    "retained/final_container_inspect.json",
    "retained/final_image_inspect.json",
)
M1_NEXT_COMMAND_ARGV = [
    "scripts/run_acceptance_container.sh",
    "timeout",
    "--signal=TERM",
    "--kill-after=20s",
    "600s",
    "env",
    "RUN_ID=<allocate-once-before-execution>",
    "network/scripts/run_five_uav_health.sh",
]
M2_BLOCKING_PREREQUISITES = [
    "granular_qualification_ownership_and_suite_split",
    "m0_requalification_after_granular_ownership",
    "m1_requalification_after_granular_ownership",
    "five_uav_capacity_prerequisite_receipt",
]
NEXT_SEQUENCE_CONTRACT = "ams.live-status-next-sequence/v1"
NEXT_SEQUENCE_PROFILES = {
    2: ("flight_capacity_prerequisite", "m2_component"),
    4: ("m4_capacity_prerequisite", "m4_component"),
}
NEXT_SEQUENCE_RUN_ID_PLACEHOLDERS = {
    "flight_capacity_prerequisite": "<allocate-once-flight-capacity-run-id>",
    "m2_component": "<allocate-once-m2-run-id>",
    "m4_capacity_prerequisite": "<allocate-once-m4-capacity-run-id>",
    "m4_component": "<allocate-once-m4-causality-run-id>",
}
M1_RESULT_GATE_NAMES = {
    "provenance",
    "five_uav_health",
    "scene",
    "runtime_inputs",
}
M1_HOST_RAW_FILES = {
    "main/initial_container_inspect.json",
    "main/final_container_inspect.json",
    "main/initial_image_inspect.json",
    "main/final_image_inspect.json",
    "validation/initial_container_inspect.json",
    "validation/final_container_inspect.json",
    "validation/image_inspect.json",
    "validation/result.json",
    "validation/stderr.txt",
    "m0/status_validation.json",
    "m0/host_final_receipt.json",
}
M1_RECEIPT_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "contract",
        "milestone",
        "run_id",
        "run_dir",
        "receipt_path",
        "source_commit",
        "image_reference",
        "image_digest",
        "runtime_container_id",
        "validation_container_id",
        "consumed_nodes",
        "qualification_content_vector",
        "inherited_m0_qualification",
        "m0_status_authority",
        "component_result",
        "artifact_content_manifest",
        "host_validation_content_manifest",
        "qualification_contract_sha256",
        "formal_accepted",
        "passed",
        "failures",
    }
)
EXPECTED_DEPENDENCY_RECORD_COUNT = 68
EXPECTED_GPU_DEVICE_REQUESTS = [
    {
        "Driver": "",
        "Count": -1,
        "DeviceIDs": None,
        "Capabilities": [["compute", "utility", "gpu"]],
        "Options": {},
    }
]
EXPECTED_DEPENDENCY_LABELS_SHA256 = (
    "d06fedf1ab3cfd158bb03d46bf06a065e66e1178eb31e3eee41db65e8a7e41de"
)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def canonical_status_metadata_block(metadata: dict[str, Any]) -> str:
    """Render the one accepted machine-readable status metadata block."""

    return METADATA_BEGIN + _canonical_json(metadata).decode("utf-8") + METADATA_END


def _safe_relative(value: str) -> bool:
    try:
        path = PurePosixPath(value)
    except (TypeError, ValueError):
        return False
    return (
        bool(value)
        and not path.is_absolute()
        and path.as_posix() == value
        and all(part not in {"", ".", ".."} for part in path.parts)
        and "\x00" not in value
    )


def _secure_read_relative(
    root: Path,
    relative: str,
    *,
    maximum_bytes: int,
    require_read_only: bool = False,
    allow_empty: bool = False,
) -> bytes:
    """Read a stable, single-link regular file through held no-follow dirfds."""

    if not _safe_relative(relative):
        raise ValueError(f"unsafe relative path: {relative!r}")
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    held: list[int] = []
    try:
        current = os.open(root, directory_flags)
        held.append(current)
        parts = PurePosixPath(relative).parts
        for component in parts[:-1]:
            current = os.open(component, directory_flags, dir_fd=current)
            held.append(current)
        descriptor = os.open(parts[-1], file_flags, dir_fd=current)
        held.append(descriptor)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"not a regular file: {relative}")
        if before.st_nlink != 1:
            raise ValueError(f"not a single-link file: {relative}")
        if before.st_size < (0 if allow_empty else 1) or before.st_size > maximum_bytes:
            raise ValueError(f"file size is outside bounds: {relative}")
        if require_read_only and stat.S_IMODE(before.st_mode) & 0o222:
            raise ValueError(f"file is not read-only: {relative}")
        payload = bytearray()
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError(f"file truncated during read: {relative}")
            payload.extend(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ValueError(f"file grew during read: {relative}")
        after = os.fstat(descriptor)
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
            raise ValueError(f"file changed during read: {relative}")
        return bytes(payload)
    except OSError as exc:
        raise ValueError(f"secure read failed for {relative}: {exc}") from exc
    finally:
        for descriptor in reversed(held):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _secure_directory_names(root: Path, relative: str) -> tuple[list[str], int]:
    if not _safe_relative(relative):
        raise ValueError(f"unsafe directory path: {relative!r}")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    held: list[int] = []
    try:
        current = os.open(root, flags)
        held.append(current)
        for component in PurePosixPath(relative).parts:
            current = os.open(component, flags, dir_fd=current)
            held.append(current)
        before = os.fstat(current)
        names = sorted(os.listdir(current))
        after = os.fstat(current)
        stable = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, field) != getattr(after, field) for field in stable):
            raise ValueError(f"directory changed while listed: {relative}")
        if len(names) != len(set(names)):
            raise ValueError(f"directory has duplicate names: {relative}")
        return names, stat.S_IMODE(before.st_mode)
    except OSError as exc:
        raise ValueError(f"secure directory inspection failed for {relative}: {exc}") from exc
    finally:
        for descriptor in reversed(held):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _secure_tree_files(root: Path, relative: str) -> list[str]:
    """Enumerate one immutable no-follow tree and reject special/hardlinked entries."""

    if not _safe_relative(relative):
        raise ValueError(f"unsafe directory path: {relative!r}")
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    entry_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    held: list[int] = []
    files: list[str] = []

    def walk(directory_fd: int, prefix: str) -> None:
        before = os.fstat(directory_fd)
        if not stat.S_ISDIR(before.st_mode) or stat.S_IMODE(before.st_mode) & 0o222:
            raise ValueError(f"host-validation directory is not immutable: {prefix or '.'}")
        names = sorted(os.listdir(directory_fd))
        if len(names) != len(set(names)):
            raise ValueError("host-validation directory contains duplicate names")
        for name in names:
            if not name or name in {".", ".."} or "/" in name or "\x00" in name:
                raise ValueError("host-validation tree contains an unsafe name")
            child_relative = f"{prefix}/{name}" if prefix else name
            descriptor = os.open(name, entry_flags, dir_fd=directory_fd)
            try:
                info = os.fstat(descriptor)
                mode = stat.S_IMODE(info.st_mode)
                if mode & 0o222:
                    raise ValueError(
                        f"host-validation entry is writable: {child_relative}"
                    )
                if stat.S_ISDIR(info.st_mode):
                    os.close(descriptor)
                    descriptor = os.open(name, directory_flags, dir_fd=directory_fd)
                    walk(descriptor, child_relative)
                elif stat.S_ISREG(info.st_mode):
                    if info.st_nlink != 1:
                        raise ValueError(
                            f"host-validation file is hardlinked: {child_relative}"
                        )
                    files.append(child_relative)
                else:
                    raise ValueError(
                        f"host-validation entry has a special type: {child_relative}"
                    )
            finally:
                os.close(descriptor)
        after = os.fstat(directory_fd)
        stable = (
            "st_dev", "st_ino", "st_mode", "st_nlink", "st_mtime_ns", "st_ctime_ns"
        )
        if any(getattr(before, key) != getattr(after, key) for key in stable):
            raise ValueError(f"host-validation directory changed: {prefix or '.'}")

    try:
        current = os.open(root, directory_flags)
        held.append(current)
        for component in PurePosixPath(relative).parts:
            current = os.open(component, directory_flags, dir_fd=current)
            held.append(current)
        walk(current, "")
        if not files:
            raise ValueError("host-validation raw tree is empty")
        return sorted(files)
    except OSError as exc:
        raise ValueError(f"secure host-validation enumeration failed: {exc}") from exc
    finally:
        for descriptor in reversed(held):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _strict_json(payload: bytes, label: str) -> Any:
    def no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{label} contains duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        document = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=no_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"{label} contains non-finite JSON constant: {value}")
            ),
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValueError(f"{label} is not strict UTF-8 JSON: {exc}") from exc
    return document


def _strict_yaml(payload: bytes, label: str) -> Any:
    class UniqueLoader(yaml.SafeLoader):
        pass

    def construct_mapping(
        loader: yaml.SafeLoader, node: yaml.nodes.MappingNode, deep: bool = False
    ) -> dict[Any, Any]:
        loader.flatten_mapping(node)
        result: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in result:
                raise ValueError(f"{label} contains duplicate YAML key: {key!r}")
            result[key] = loader.construct_object(value_node, deep=deep)
        return result

    UniqueLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping
    )
    try:
        return yaml.load(payload.decode("utf-8"), Loader=UniqueLoader)
    except (UnicodeError, yaml.YAMLError, ValueError, RecursionError) as exc:
        raise ValueError(f"{label} is not strict unique-key UTF-8 YAML: {exc}") from exc


def _git_environment() -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": "/nonexistent",
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_LITERAL_PATHSPECS": "1",
    }


def _git_command(arguments: list[str]) -> list[str]:
    return [
        GIT_BINARY,
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
        "-c",
        "diff.external=",
        *arguments,
    ]


def _git(root: Path, arguments: list[str], *, timeout: int = 30) -> bytes:
    result = subprocess.run(
        _git_command(arguments),
        cwd=root,
        capture_output=True,
        check=False,
        timeout=timeout,
        env=_git_environment(),
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"git {' '.join(arguments)} failed: {stderr}")
    return result.stdout


def _commit_exists(root: Path, commit: str) -> bool:
    if SHA1.fullmatch(commit) is None:
        return False
    result = subprocess.run(
        _git_command(["cat-file", "-e", f"{commit}^{{commit}}"]),
        cwd=root,
        capture_output=True,
        check=False,
        timeout=15,
        env=_git_environment(),
    )
    return result.returncode == 0


def _is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    result = subprocess.run(
        _git_command(["merge-base", "--is-ancestor", ancestor, descendant]),
        cwd=root,
        capture_output=True,
        check=False,
        timeout=15,
        env=_git_environment(),
    )
    return result.returncode == 0


def _expanded_rows(document: str, failures: list[str], name: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for label, status_value in ROW_PATTERN.findall(document):
        if status_value not in ALLOWED:
            failures.append(f"{name} has unsupported milestone status: {status_value}")
        if "–" in label:
            first, last = (int(part.removeprefix("M")) for part in label.split("–"))
            labels = [f"M{index}" for index in range(first, last + 1)]
        else:
            labels = [label]
        for milestone in labels:
            if milestone in result:
                failures.append(f"{name} has duplicate status row: {milestone}")
            result[milestone] = status_value
    return result


def _receipt_has_minimum_host_authority(receipt: dict[str, Any]) -> bool:
    """Fixture-level check; live mode performs complete receipt verification."""

    gates = receipt.get("gates") if isinstance(receipt.get("gates"), dict) else {}
    host = gates.get("host_final") if isinstance(gates.get("host_final"), dict) else {}
    host_details = host.get("details") if isinstance(host.get("details"), dict) else {}
    return (
        receipt.get("schema_version") == 3
        and receipt.get("contract") == RECEIPT_CONTRACT
        and receipt.get("formal_accepted") is True
        and receipt.get("passed") is True
        and receipt.get("failures") == []
        and receipt.get("consumed_nodes") == ["Q0"]
        and set(gates) == FORMAL_GATE_NAMES
        and all(
            isinstance(record, dict) and record.get("status") == "passed"
            for record in gates.values()
        )
        and host_details.get("failures") == []
        and SHA256.fullmatch(str(receipt.get("qualification_contract_sha256") or ""))
        is not None
    )


def _m1_receipt_has_minimum_host_authority(receipt: dict[str, Any]) -> bool:
    """Fixture-level M1 check; live mode rederives every cited byte."""

    return (
        receipt.get("schema_version") == 1
        and receipt.get("contract") == M1_RECEIPT_CONTRACT
        and receipt.get("milestone") == "M1"
        and receipt.get("formal_accepted") is True
        and receipt.get("passed") is True
        and receipt.get("failures") == []
        and receipt.get("consumed_nodes") == ["Q0", "Q1"]
        and SHA256.fullmatch(
            str(receipt.get("qualification_contract_sha256") or "")
        )
        is not None
    )


def _component_receipt_has_minimum_host_authority(
    receipt: dict[str, Any], milestone_index: int
) -> bool:
    """Fixture-level M2--M4 check; live mode fully rederives the receipt."""

    profile_name = f"m{milestone_index}_component"
    expected_nodes = [f"Q{index}" for index in range(milestone_index + 1)]
    return (
        receipt.get("schema_version") == 1
        and receipt.get("profile") == profile_name
        and receipt.get("formal_accepted") is True
        and receipt.get("passed") is True
        and receipt.get("failures") == []
        and receipt.get("consumed_nodes") == expected_nodes
        and SHA256.fullmatch(
            str(receipt.get("qualification_contract_sha256") or "")
        )
        is not None
    )


def status_documents_status(
    progress: str,
    report: str,
    next_task: str,
    *,
    m0_receipt: dict[str, Any] | None = None,
    m1_receipt: dict[str, Any] | None = None,
    component_receipts: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Check only the status state machine, using caller-supplied fixture text."""

    failures: list[str] = []
    expected_labels = {f"M{index}" for index in range(9)}
    progress_states = _expanded_rows(progress, failures, "PROGRESS")
    report_states = _expanded_rows(report, failures, "VALIDATION_REPORT")
    if set(progress_states) != expected_labels:
        failures.append("PROGRESS milestone rows are not exactly M0..M8")
    if set(report_states) != expected_labels:
        failures.append("VALIDATION_REPORT milestone rows are not exactly M0..M8")
    if progress_states != report_states:
        failures.append("PROGRESS and VALIDATION_REPORT milestone states disagree")

    ordered = [progress_states.get(f"M{index}", "missing") for index in range(9)]
    first_open = next((index for index, state in enumerate(ordered) if state != "passed"), 9)
    if any(state != "not_started" for state in ordered[first_open + 1 :]):
        failures.append("milestones after the active milestone are not all not_started")
    if first_open < 9 and ordered[first_open] not in ALLOWED - {"passed"}:
        failures.append("active milestone has an invalid open state")

    passed_count = sum(state == "passed" for state in ordered)
    for name, document in (
        ("PROGRESS", progress),
        ("VALIDATION_REPORT", report),
        ("NEXT_TASK", next_task),
    ):
        counts = COUNT_PATTERN.findall(document)
        ready_values = READY_PATTERN.findall(document)
        if len(counts) != 1 or int(counts[0]) != passed_count:
            failures.append(f"{name} closed-milestone count disagrees")
        if (
            len(ready_values) != 1
            or (ready_values[0].lower() == "true") != (passed_count == 9)
        ):
            failures.append(f"{name} customer-ready state disagrees")
    if first_open < 9:
        active = f"M{first_open}"
        progress_active = re.findall(
            r"Active milestone:\s*\n?\*\*(M\d)\b", progress
        )
        next_active = re.findall(
            r"Active milestone:\s*\n?\*\*(M\d)\b", next_task
        )
        report_active = re.findall(
            r"Active milestone:\s*\n?\*\*(M\d)\b", report
        )
        if progress_active != [active]:
            failures.append("PROGRESS active milestone disagrees")
        if next_active != [active]:
            failures.append("NEXT_TASK active milestone disagrees")
        if report_active not in ([], [active]):
            failures.append("VALIDATION_REPORT active milestone disagrees")
    elif any(
        "Active milestone:" in document for document in (progress, report, next_task)
    ):
        failures.append("customer-ready records must not name an active milestone")

    if progress_states.get("M0") == "passed" and (
        not isinstance(m0_receipt, dict) or not _receipt_has_minimum_host_authority(m0_receipt)
    ):
        failures.append("M0 may be marked passed only from a passing host-final receipt")
    if progress_states.get("M1") == "passed" and (
        not isinstance(m1_receipt, dict)
        or not _m1_receipt_has_minimum_host_authority(m1_receipt)
    ):
        failures.append("M1 may be marked passed only from a passing host-final receipt")
    component_receipts = (
        component_receipts if isinstance(component_receipts, dict) else {}
    )
    for index in range(2, 5):
        if progress_states.get(f"M{index}") == "passed" and (
            not isinstance(component_receipts.get(f"m{index}"), dict)
            or not _component_receipt_has_minimum_host_authority(
                component_receipts[f"m{index}"], index
            )
        ):
            failures.append(
                f"M{index} may be marked passed only from its passing "
                "component host-final receipt"
            )
    if any(progress_states.get(f"M{index}") == "passed" for index in range(5, 9)):
        failures.append("this live-status validator has no authority for M5-M8 closure")
    return {
        "schema_version": 1,
        "passed": not failures,
        "failures": failures,
        "states": progress_states,
        "active_milestone": f"M{first_open}" if first_open < 9 else None,
        "fully_closed_sequential_milestones": passed_count,
        "customer_ready": passed_count == 9,
    }


def _extract_metadata(document: str, name: str) -> dict[str, Any]:
    if document.count(METADATA_BEGIN) != 1 or document.count(METADATA_END) != 1:
        raise ValueError(f"{name} does not contain exactly one status metadata block")
    start = document.index(METADATA_BEGIN) + len(METADATA_BEGIN)
    end = document.index(METADATA_END, start)
    payload_text = document[start:end]
    try:
        metadata = json.loads(
            payload_text,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"{name} metadata contains non-finite constant: {value}")
            ),
        )
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError(f"{name} metadata is not strict JSON: {exc}") from exc
    if not isinstance(metadata, dict) or payload_text.encode("utf-8") != _canonical_json(metadata):
        raise ValueError(f"{name} metadata is not canonical compact JSON")
    return metadata


def _git_blob_record(root: Path, commit: str, relative: str) -> dict[str, str]:
    if not _safe_relative(relative):
        raise ValueError(f"next-command input path is unsafe: {relative!r}")
    raw = _git(root, ["ls-tree", "-z", commit, "--", relative])
    records = [record for record in raw.rstrip(b"\0").split(b"\0") if record]
    if len(records) != 1:
        raise ValueError(f"next-command input is not exactly one tracked entry: {relative}")
    header, separator, raw_path = records[0].partition(b"\t")
    fields = header.decode("ascii").split(" ")
    if not separator or raw_path.decode("utf-8") != relative or len(fields) != 3:
        raise ValueError(f"malformed Git identity for next-command input: {relative}")
    mode, object_type, object_id = fields
    if mode not in {"100644", "100755"} or object_type != "blob" or SHA1.fullmatch(object_id) is None:
        raise ValueError(f"next-command input is not a regular tracked blob: {relative}")
    blob = _git(root, ["cat-file", "blob", object_id])
    return {
        "path": relative,
        "git_mode": mode,
        "git_object_id": object_id,
        "sha256": _sha256(blob),
    }


def _validate_metadata(
    metadata: dict[str, Any],
    *,
    root: Path,
    state: dict[str, Any],
    report_commit: str,
    plan_sha256: str,
    receipt: dict[str, Any],
) -> list[str]:
    failures: list[str] = []
    expected_top = {
        "schema_version",
        "contract",
        "plan_contract",
        "technical_base_commit",
        "execution_commit",
        "evidence",
        "qualification",
        "state",
        "next_command",
    }
    if set(metadata) != expected_top:
        failures.append("shared status metadata top-level schema is not exact")
        return failures
    if metadata.get("schema_version") != 1 or metadata.get("contract") != STATUS_METADATA_CONTRACT:
        failures.append("shared status metadata contract/version is invalid")
    if (
        state.get("fully_closed_sequential_milestones") != 1
        or state.get("active_milestone") != "M1"
        or state.get("customer_ready") is not False
    ):
        failures.append(
            "live-status v1 authorizes only post-receipt M0 closure with M1 active; "
            "M1-M8 require their own independently verified evidence schemas"
        )

    plan = metadata.get("plan_contract")
    expected_plan = {"path": PLAN_PATH, "sha256": plan_sha256}
    if plan != expected_plan:
        failures.append("shared status metadata plan citation is not current and exact")

    technical_base = metadata.get("technical_base_commit")
    execution_commit = metadata.get("execution_commit")
    for label, commit in (
        ("technical base", technical_base),
        ("execution", execution_commit),
    ):
        if not isinstance(commit, str) or not _commit_exists(root, commit):
            failures.append(f"{label} commit is not an exact 40-hex Git commit")
        elif not _is_ancestor(root, commit, report_commit):
            failures.append(f"{label} commit is not an ancestor of report HEAD")

    status_record = metadata.get("state")
    expected_state = {
        "active_milestone": state.get("active_milestone"),
        "customer_ready": state.get("customer_ready"),
        "fully_closed_sequential_milestones": state.get(
            "fully_closed_sequential_milestones"
        ),
    }
    if status_record != expected_state:
        failures.append("shared status metadata state disagrees with the human records")

    evidence = metadata.get("evidence")
    expected_evidence_keys = {
        "kind",
        "milestone",
        "run_id",
        "receipt_path",
        "receipt_sha256",
        "qualification_contract_sha256",
    }
    if not isinstance(evidence, dict) or set(evidence) != expected_evidence_keys:
        failures.append("shared status metadata evidence schema is not exact")
        evidence = {}
    if (
        evidence.get("kind") != "m0_host_final_receipt"
        or evidence.get("milestone") != "M0"
        or evidence.get("run_id") != receipt.get("run_id")
        or evidence.get("receipt_path") != receipt.get("receipt_path")
        or evidence.get("qualification_contract_sha256")
        != receipt.get("qualification_contract_sha256")
        or SHA256.fullmatch(str(evidence.get("receipt_sha256") or "")) is None
    ):
        failures.append("shared status metadata evidence citation is invalid")

    vector = receipt.get("qualification_content_vector")
    qualification = metadata.get("qualification")
    expected_qualification = {
        "policy_id": vector.get("policy_id") if isinstance(vector, dict) else None,
        "policy_path": POLICY_PATH,
        "policy_sha256": vector.get("policy_sha256") if isinstance(vector, dict) else None,
        "vector_commit": vector.get("git_commit") if isinstance(vector, dict) else None,
        "vector_sha256": vector.get("vector_sha256") if isinstance(vector, dict) else None,
        "consumed_nodes": ["Q0"],
    }
    if qualification != expected_qualification:
        failures.append("shared status metadata qualification citation is invalid")
    if execution_commit != (vector.get("git_commit") if isinstance(vector, dict) else None):
        failures.append("execution commit is not the receipt Q-vector commit")

    next_command = metadata.get("next_command")
    expected_next_keys = {"milestone", "argv", "argv_sha256", "tracked_inputs"}
    if not isinstance(next_command, dict) or set(next_command) != expected_next_keys:
        failures.append("shared status metadata next-command schema is not exact")
        next_command = {}
    active = state.get("active_milestone")
    argv = next_command.get("argv")
    tracked_inputs = next_command.get("tracked_inputs")
    if active is None:
        if (
            next_command.get("milestone") is not None
            or argv != []
            or next_command.get("argv_sha256") != _sha256(_canonical_json([]))
            or tracked_inputs != []
        ):
            failures.append("customer-ready status must have the canonical empty next command")
    else:
        if next_command.get("milestone") != active:
            failures.append("next-command milestone is not the active milestone")
        if (
            not isinstance(argv, list)
            or not argv
            or not all(
                isinstance(token, str)
                and token
                and "\x00" not in token
                and "\n" not in token
                and "\r" not in token
                for token in argv
            )
        ):
            failures.append("next-command argv is not one exact nonempty string vector")
            argv = []
        if active == "M1" and argv != M1_NEXT_COMMAND_ARGV:
            failures.append("M1 next-command argv is not the exact canonical formal command")
        if next_command.get("argv_sha256") != _sha256(_canonical_json(argv)):
            failures.append("next-command argv hash is invalid")
        referenced: set[str] = set()
        for token in argv:
            candidate = token[2:] if token.startswith("./") else token
            if _safe_relative(candidate):
                probe = subprocess.run(
                    _git_command(["cat-file", "-e", f"{technical_base}:{candidate}"]),
                    cwd=root,
                    capture_output=True,
                    check=False,
                    timeout=10,
                    env=_git_environment(),
                )
                if probe.returncode == 0:
                    referenced.add(candidate)
        expected_inputs: list[dict[str, str]] = []
        if isinstance(technical_base, str) and _commit_exists(root, technical_base):
            try:
                expected_inputs = [
                    _git_blob_record(root, technical_base, path) for path in sorted(referenced)
                ]
            except ValueError as exc:
                failures.append(str(exc))
        if not expected_inputs:
            failures.append("next-command argv does not directly name a tracked command input")
        if tracked_inputs != expected_inputs:
            failures.append("next-command tracked-input identities are not exact")
        if any(record.get("git_mode") != "100755" for record in expected_inputs):
            failures.append("every tracked next-command input must be executable in Git")
    return failures


def _validate_metadata_v2(
    metadata: dict[str, Any],
    *,
    root: Path,
    state: dict[str, Any],
    report_commit: str,
    plan_sha256: str,
    m0_receipt: dict[str, Any],
    m1_receipt: dict[str, Any],
) -> list[str]:
    """Validate the exact count-two M0+M1 live-status contract."""

    failures: list[str] = []
    expected_top = {
        "schema_version",
        "contract",
        "plan_contract",
        "technical_base_commit",
        "execution_commit",
        "evidence",
        "qualification",
        "state",
        "next_command",
        "next_sequence",
    }
    if set(metadata) != expected_top:
        return ["shared status metadata v2 top-level schema is not exact"]
    if (
        metadata.get("schema_version") != 2
        or metadata.get("contract") != STATUS_METADATA_CONTRACT_V2
    ):
        failures.append("shared status metadata v2 contract/version is invalid")
    if (
        state.get("fully_closed_sequential_milestones") != 2
        or state.get("active_milestone") != "M2"
        or state.get("customer_ready") is not False
        or state.get("states", {}).get("M0") != "passed"
        or state.get("states", {}).get("M1") != "passed"
        or state.get("states", {}).get("M2") != "not_started"
        or any(
            state.get("states", {}).get(f"M{index}") != "not_started"
            for index in range(3, 9)
        )
    ):
        failures.append(
            "live-status v2 authorizes exactly M0/M1 passed with M2 not_started active"
        )

    if metadata.get("plan_contract") != {"path": PLAN_PATH, "sha256": plan_sha256}:
        failures.append("shared status metadata v2 plan citation is not current and exact")

    technical_base = metadata.get("technical_base_commit")
    execution_commit = metadata.get("execution_commit")
    source_commit = m1_receipt.get("source_commit")
    if technical_base != execution_commit or execution_commit != source_commit:
        failures.append(
            "status v2 technical/execution commits are not the M1 source commit"
        )
    for label, commit in (
        ("technical base", technical_base),
        ("execution", execution_commit),
    ):
        if not isinstance(commit, str) or not _commit_exists(root, commit):
            failures.append(f"status v2 {label} commit is invalid")
        elif not _is_ancestor(root, commit, report_commit):
            failures.append(f"status v2 {label} commit is not an ancestor of report HEAD")

    expected_state = {
        "active_milestone": "M2",
        "customer_ready": False,
        "fully_closed_sequential_milestones": 2,
    }
    if metadata.get("state") != expected_state:
        failures.append("shared status metadata v2 state disagrees with human records")

    evidence = metadata.get("evidence")
    if not isinstance(evidence, dict) or set(evidence) != {"m0", "m1"}:
        failures.append("shared status metadata v2 evidence map is not exact")
        evidence = {}
    expected_citation_keys = {
        "kind",
        "milestone",
        "run_id",
        "receipt_path",
        "receipt_sha256",
        "qualification_contract_sha256",
    }
    for key, kind, milestone, receipt, receipt_name in (
        ("m0", "m0_host_final_receipt", "M0", m0_receipt, M0_RECEIPT_NAME),
        ("m1", "m1_host_final_receipt", "M1", m1_receipt, M1_RECEIPT_NAME),
    ):
        citation = evidence.get(key)
        run_id = receipt.get("run_id")
        expected_path = (
            f"runs/{run_id}/metrics/{receipt_name}"
            if isinstance(run_id, str)
            else None
        )
        if (
            not isinstance(citation, dict)
            or set(citation) != expected_citation_keys
            or citation.get("kind") != kind
            or citation.get("milestone") != milestone
            or citation.get("run_id") != run_id
            or citation.get("receipt_path") != expected_path
            or citation.get("qualification_contract_sha256")
            != receipt.get("qualification_contract_sha256")
            or SHA256.fullmatch(str(citation.get("receipt_sha256") or "")) is None
        ):
            failures.append(f"shared status metadata v2 {milestone} citation is invalid")

    vector = m1_receipt.get("qualification_content_vector")
    expected_qualification = {
        "policy_id": vector.get("policy_id") if isinstance(vector, dict) else None,
        "policy_path": POLICY_PATH,
        "policy_sha256": vector.get("policy_sha256") if isinstance(vector, dict) else None,
        "vector_commit": vector.get("git_commit") if isinstance(vector, dict) else None,
        "vector_sha256": vector.get("vector_sha256") if isinstance(vector, dict) else None,
        "consumed_nodes": ["Q0", "Q1"],
    }
    if metadata.get("qualification") != expected_qualification:
        failures.append("shared status metadata v2 qualification citation is invalid")

    next_command = metadata.get("next_command")
    expected_next_command = {
        "milestone": "M2",
        "eligible": False,
        "blocking_prerequisites": M2_BLOCKING_PREREQUISITES,
        "argv": [],
        "argv_sha256": _sha256(_canonical_json([])),
        "tracked_inputs": [],
    }
    if next_command != expected_next_command:
        failures.append(
            "M2 next-command record is not the canonical ineligible prerequisite state"
        )
    try:
        from network.validation.component_profiles import load_profiles  # noqa: PLC0415

        profiles = load_profiles(
            root / "network/config/component_acceptance_profiles.json"
        )
    except ValueError as exc:
        failures.append(str(exc))
    else:
        failures.extend(
            _validate_status_next_sequence(
                metadata.get("next_sequence"),
                version=2,
                root=root,
                technical_base=str(technical_base),
                report_commit=report_commit,
                profiles=profiles,
            )
        )
    return failures


def _component_status_next_command(
    version: int,
    *,
    root: Path,
    technical_base: str,
    profiles: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    profile_name = {3: "m3_component", 4: "m4_capacity_prerequisite"}.get(
        version
    )
    if profile_name is None:
        argv: list[str] = []
        tracked_inputs: list[dict[str, str]] = []
        return {
            "milestone": "M5",
            "profile": None,
            "eligible": False,
            "blocking_prerequisites": ["m5_component_profile_not_implemented"],
            "argv": argv,
            "argv_sha256": _sha256(_canonical_json(argv)),
            "tracked_inputs": tracked_inputs,
        }
    profile = profiles[profile_name]
    argv = [
        "scripts/run_acceptance_container.sh",
        "timeout",
        "--signal=TERM",
        "--kill-after=20s",
        f"{profile['timeout_s']}s",
        "env",
        "RUN_ID=<allocate-once-before-execution>",
        profile["runner"],
    ]
    tracked_inputs = [
        _git_blob_record(root, technical_base, relative)
        for relative in sorted(
            {"scripts/run_acceptance_container.sh", profile["runner"]}
        )
    ]
    if any(record.get("git_mode") != "100755" for record in tracked_inputs):
        raise ValueError("component next-command input is not executable in Git")
    return {
        "milestone": f"M{version}",
        "profile": profile_name,
        "eligible": True,
        "blocking_prerequisites": [],
        "argv": argv,
        "argv_sha256": _sha256(_canonical_json(argv)),
        "tracked_inputs": tracked_inputs,
    }


def _component_status_next_sequence(
    version: int,
    *,
    root: Path,
    technical_base: str,
    profiles: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Build the exact resumable two-component closure sequence for M2/M4.

    The status-only commit is created before its own Git identity is known, so
    auxiliary receipts bind symbolically to ``status_report_commit``.  Live
    validation resolves that symbol to the current status commit and enforces
    at most one successful auxiliary receipt for that source epoch.
    """

    ordered = NEXT_SEQUENCE_PROFILES.get(version)
    if ordered is None:
        raise ValueError("status version has no canonical component sequence")
    auxiliary_name, closure_name = ordered
    if auxiliary_name not in profiles or closure_name not in profiles:
        raise ValueError("canonical next-sequence profile is unavailable")
    auxiliary = profiles[auxiliary_name]
    closure = profiles[closure_name]
    if (
        auxiliary["prerequisite_status_count"] != version
        or closure["prerequisite_status_count"] != version
        or auxiliary["prerequisite_status_contract"]
        != STATUS_METADATA_CONTRACTS[version]
        or closure["prerequisite_status_contract"]
        != STATUS_METADATA_CONTRACTS[version]
        or auxiliary["required_component_profiles"] != []
        or closure["required_component_profiles"] != [auxiliary_name]
    ):
        raise ValueError("canonical next-sequence profile graph/status epoch differs")

    steps: list[dict[str, Any]] = []
    placeholders: set[str] = set()
    for position, (profile_name, profile, role) in enumerate(
        (
            (auxiliary_name, auxiliary, "auxiliary_prerequisite"),
            (closure_name, closure, "milestone_closure"),
        ),
        start=1,
    ):
        placeholder = NEXT_SEQUENCE_RUN_ID_PLACEHOLDERS.get(profile_name)
        if not isinstance(placeholder, str) or not placeholder:
            raise ValueError("canonical next-sequence RUN_ID placeholder is unavailable")
        if placeholder in placeholders:
            raise ValueError("canonical next-sequence RUN_ID placeholders are not distinct")
        placeholders.add(placeholder)
        argv = [
            "scripts/run_acceptance_container.sh",
            "timeout",
            "--signal=TERM",
            "--kill-after=20s",
            f"{profile['timeout_s']}s",
            "env",
            f"RUN_ID={placeholder}",
            profile["runner"],
        ]
        tracked_inputs = [
            _git_blob_record(root, technical_base, relative)
            for relative in sorted(
                {"scripts/run_acceptance_container.sh", profile["runner"]}
            )
        ]
        if any(record.get("git_mode") != "100755" for record in tracked_inputs):
            raise ValueError("component next-sequence input is not executable in Git")
        steps.append(
            {
                "position": position,
                "profile": profile_name,
                "role": role,
                "run_id_placeholder": placeholder,
                "argv": argv,
                "argv_sha256": _sha256(_canonical_json(argv)),
                "tracked_inputs": tracked_inputs,
                "completion_receipt": {
                    "contract": profile["receipt_contract"],
                    "name": profile["receipt_name"],
                    "source_commit_binding": "status_report_commit",
                },
            }
        )
    return {
        "contract": NEXT_SEQUENCE_CONTRACT,
        "milestone": f"M{version}",
        "ordered_profiles": list(ordered),
        "steps": steps,
        "resume_policy": {
            "source_commit_binding": "status_report_commit",
            "auxiliary_profile": auxiliary_name,
            "successful_auxiliary_receipts_per_source_epoch": {
                "required_before_step_2": 1,
                "maximum": 1,
            },
            "when_zero": "execute_step_1_then_step_2",
            "when_one": "skip_step_1_execute_or_retry_step_2",
            "when_multiple": "fail_closed",
            "successful_step_1_reexecution_forbidden": True,
            "failed_step_2_retry": "new_step_2_run_id_step_2_only",
            "successful_step_2_reexecution_forbidden": True,
            "when_step_2_successful": "advance_status_do_not_execute_sequence",
        },
    }


def _current_successful_component_receipts(
    root: Path,
    *,
    profile_name: str,
    profile: dict[str, Any],
    source_commit: str,
) -> list[str]:
    """Return exact successful component receipts for one status source epoch."""

    runs_root = root / "runs"
    try:
        info = runs_root.lstat()
    except FileNotFoundError:
        return []
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ValueError("canonical runs root is unavailable for next-sequence resume")
    receipts: list[str] = []
    for path in sorted(runs_root.glob(f"*/metrics/{profile['receipt_name']}")):
        run_id = path.parent.parent.name
        if SAFE_RUN_ID.fullmatch(run_id) is None:
            raise ValueError("next-sequence component receipt has an unsafe run ID")
        relative = f"runs/{run_id}/metrics/{profile['receipt_name']}"
        payload = _secure_read_relative(
            root,
            relative,
            maximum_bytes=MAX_RECEIPT_BYTES,
            require_read_only=True,
        )
        receipt = _strict_json(payload, f"{profile_name} next-sequence receipt")
        if not isinstance(receipt, dict):
            raise ValueError("next-sequence component receipt is not an object")
        if receipt.get("source_commit") != source_commit:
            continue
        if (
            payload != _canonical_pretty_json(receipt)
            or receipt.get("schema_version") != 1
            or receipt.get("contract") != profile["receipt_contract"]
            or receipt.get("profile") != profile_name
            or receipt.get("run_id") != run_id
            or receipt.get("receipt_path") != relative
            or receipt.get("consumed_nodes") != profile["consumed_nodes"]
            or receipt.get("result_contract") != profile["result_contract"]
            or receipt.get("formal_accepted") is not True
            or receipt.get("passed") is not True
            or receipt.get("failures") != []
        ):
            raise ValueError(
                "current next-sequence component receipt authority is invalid"
            )
        receipts.append(relative)
    return receipts


def _validate_status_next_sequence(
    sequence: Any,
    *,
    version: int,
    root: Path,
    technical_base: str,
    report_commit: str,
    profiles: dict[str, dict[str, Any]],
) -> list[str]:
    """Validate exact sequence bytes and its dynamic auxiliary cardinality."""

    try:
        expected = _component_status_next_sequence(
            version,
            root=root,
            technical_base=technical_base,
            profiles=profiles,
        )
    except (OSError, ValueError) as exc:
        return [str(exc)]
    failures: list[str] = []
    if sequence != expected:
        failures.append(f"shared status metadata v{version} next sequence is not exact")
    auxiliary_name = NEXT_SEQUENCE_PROFILES[version][0]
    try:
        auxiliary = _current_successful_component_receipts(
            root,
            profile_name=auxiliary_name,
            profile=profiles[auxiliary_name],
            source_commit=report_commit,
        )
        closure_name = NEXT_SEQUENCE_PROFILES[version][1]
        closure = _current_successful_component_receipts(
            root,
            profile_name=closure_name,
            profile=profiles[closure_name],
            source_commit=report_commit,
        )
    except (OSError, ValueError) as exc:
        failures.append(str(exc))
    else:
        if len(auxiliary) > 1:
            failures.append(
                f"status v{version} has multiple successful auxiliary receipts "
                "for one source epoch"
            )
        if len(closure) > 1:
            failures.append(
                f"status v{version} has multiple successful closure receipts "
                "for one source epoch"
            )
        elif len(closure) == 1:
            failures.append(
                f"status v{version} already has a successful closure receipt; "
                "advance status before executing another component"
            )
        if closure and len(auxiliary) != 1:
            failures.append(
                f"status v{version} successful closure lacks exactly one "
                "auxiliary receipt for its source epoch"
            )
    return failures


def _validate_metadata_component_version(
    metadata: dict[str, Any],
    *,
    version: int,
    root: Path,
    state: dict[str, Any],
    report_commit: str,
    plan_sha256: str,
    receipts: dict[str, dict[str, Any]],
    receipt_payloads: dict[str, bytes],
    profiles: dict[str, dict[str, Any]],
) -> list[str]:
    """Validate cumulative post-M2/post-M3/post-M4 status authority."""

    failures: list[str] = []
    expected_top = {
        "schema_version",
        "contract",
        "plan_contract",
        "technical_base_commit",
        "execution_commit",
        "evidence",
        "qualification",
        "state",
        "next_command",
    }
    if version == 4:
        expected_top.add("next_sequence")
    if set(metadata) != expected_top:
        return [f"shared status metadata v{version} top-level schema is not exact"]
    if (
        metadata.get("schema_version") != version
        or metadata.get("contract") != STATUS_METADATA_CONTRACTS[version]
    ):
        failures.append(f"shared status metadata v{version} contract/version is invalid")
    states = state.get("states") if isinstance(state.get("states"), dict) else {}
    if (
        state.get("fully_closed_sequential_milestones") != version
        or state.get("active_milestone") != f"M{version}"
        or state.get("customer_ready") is not False
        or any(states.get(f"M{index}") != "passed" for index in range(version))
        or any(
            states.get(f"M{index}") != "not_started"
            for index in range(version, 9)
        )
    ):
        failures.append(
            f"live-status v{version} authorizes exactly M0..M{version - 1} "
            f"passed with M{version} active/not_started"
        )
    if metadata.get("plan_contract") != {"path": PLAN_PATH, "sha256": plan_sha256}:
        failures.append(f"shared status metadata v{version} plan citation is invalid")

    latest_key = f"m{version - 1}"
    latest = receipts.get(latest_key, {})
    source_commit = latest.get("source_commit")
    technical_base = metadata.get("technical_base_commit")
    execution_commit = metadata.get("execution_commit")
    if technical_base != source_commit or execution_commit != source_commit:
        failures.append(
            f"status v{version} execution/base are not the latest component source commit"
        )
    if (
        not isinstance(source_commit, str)
        or not _commit_exists(root, source_commit)
        or not _is_ancestor(root, source_commit, report_commit)
    ):
        failures.append(f"status v{version} source commit/history is invalid")
    expected_state = {
        "active_milestone": f"M{version}",
        "customer_ready": False,
        "fully_closed_sequential_milestones": version,
    }
    if metadata.get("state") != expected_state:
        failures.append(f"shared status metadata v{version} state is invalid")

    evidence = metadata.get("evidence")
    expected_evidence_keys = {f"m{index}" for index in range(version)}
    if not isinstance(evidence, dict) or set(evidence) != expected_evidence_keys:
        failures.append(f"shared status metadata v{version} evidence map is not exact")
        evidence = {}
    citation_keys = {
        "kind",
        "milestone",
        "run_id",
        "receipt_path",
        "receipt_sha256",
        "qualification_contract_sha256",
    }
    for index in range(version):
        key = f"m{index}"
        receipt = receipts.get(key, {})
        citation = evidence.get(key)
        if index == 0:
            receipt_name = M0_RECEIPT_NAME
            kind = "m0_host_final_receipt"
        elif index == 1:
            receipt_name = M1_RECEIPT_NAME
            kind = "m1_host_final_receipt"
        else:
            profile = profiles[f"m{index}_component"]
            receipt_name = profile["receipt_name"]
            kind = f"m{index}_host_final_receipt"
        expected_path = f"runs/{receipt.get('run_id')}/metrics/{receipt_name}"
        if (
            not isinstance(citation, dict)
            or set(citation) != citation_keys
            or citation.get("kind") != kind
            or citation.get("milestone") != f"M{index}"
            or citation.get("run_id") != receipt.get("run_id")
            or citation.get("receipt_path") != expected_path
            or citation.get("receipt_sha256")
            != _sha256(receipt_payloads.get(key, b""))
            or citation.get("qualification_contract_sha256")
            != receipt.get("qualification_contract_sha256")
        ):
            failures.append(
                f"shared status metadata v{version} M{index} citation is invalid"
            )

    vector = latest.get("qualification_content_vector")
    expected_nodes = [f"Q{index}" for index in range(version)]
    expected_qualification = {
        "policy_id": vector.get("policy_id") if isinstance(vector, dict) else None,
        "policy_path": POLICY_PATH,
        "policy_sha256": vector.get("policy_sha256") if isinstance(vector, dict) else None,
        "vector_commit": vector.get("git_commit") if isinstance(vector, dict) else None,
        "vector_sha256": vector.get("vector_sha256") if isinstance(vector, dict) else None,
        "consumed_nodes": expected_nodes,
    }
    if metadata.get("qualification") != expected_qualification:
        failures.append(f"shared status metadata v{version} qualification is invalid")
    try:
        expected_next = _component_status_next_command(
            version,
            root=root,
            technical_base=str(technical_base),
            profiles=profiles,
        )
    except ValueError as exc:
        failures.append(str(exc))
    else:
        if metadata.get("next_command") != expected_next:
            failures.append(
                f"shared status metadata v{version} next command is not exact"
            )
    if version == 4:
        failures.extend(
            _validate_status_next_sequence(
                metadata.get("next_sequence"),
                version=4,
                root=root,
                technical_base=str(technical_base),
                report_commit=report_commit,
                profiles=profiles,
            )
        )
    return failures


def _validate_snapshot(snapshot: Any, label: str) -> list[str]:
    failures: list[str] = []
    keys = {"root_identity", "entries", "entry_count", "total_file_bytes", "tree_sha256"}
    if not isinstance(snapshot, dict) or set(snapshot) != keys:
        return [f"{label} artifact snapshot schema is not exact"]
    entries = snapshot.get("entries")
    root_identity = snapshot.get("root_identity")
    if (
        not isinstance(entries, dict)
        or not isinstance(root_identity, dict)
        or set(root_identity) != {"device", "inode", "mode", "mtime_ns", "ctime_ns"}
        or any(isinstance(value, bool) or not isinstance(value, int) for value in root_identity.values())
        or not entries
        or snapshot.get("entry_count") != len(entries)
        or isinstance(snapshot.get("total_file_bytes"), bool)
        or not isinstance(snapshot.get("total_file_bytes"), int)
        or snapshot.get("total_file_bytes", -1) < 0
        or snapshot.get("tree_sha256") != _sha256(_canonical_json(entries))
    ):
        failures.append(f"{label} artifact snapshot content is invalid")
        return failures
    total_bytes = 0
    directories: set[str] = set()
    for relative, entry in entries.items():
        if not isinstance(relative, str) or not _safe_relative(relative):
            failures.append(f"{label} artifact snapshot has an unsafe path")
            continue
        if not isinstance(entry, dict):
            failures.append(f"{label} artifact snapshot has a malformed entry: {relative}")
            continue
        common = {"kind", "mode", "device", "inode", "links", "mtime_ns", "ctime_ns"}
        numeric = {"mode", "device", "inode", "links", "mtime_ns", "ctime_ns"}
        if any(
            isinstance(entry.get(key), bool) or not isinstance(entry.get(key), int)
            for key in numeric
        ):
            failures.append(f"{label} artifact entry identity is malformed: {relative}")
            continue
        if entry.get("kind") == "directory":
            if set(entry) != common or entry.get("links", 0) < 1:
                failures.append(f"{label} artifact directory schema is invalid: {relative}")
            directories.add(relative)
        elif entry.get("kind") == "file":
            if (
                set(entry) != common | {"bytes", "sha256"}
                or entry.get("links") != 1
                or isinstance(entry.get("bytes"), bool)
                or not isinstance(entry.get("bytes"), int)
                or entry.get("bytes", -1) < 0
                or SHA256.fullmatch(str(entry.get("sha256") or "")) is None
            ):
                failures.append(f"{label} artifact file schema is invalid: {relative}")
            else:
                total_bytes += entry["bytes"]
        else:
            failures.append(f"{label} artifact entry kind is invalid: {relative}")
    for relative in entries:
        parent = PurePosixPath(relative).parent.as_posix()
        if parent != "." and parent not in directories:
            failures.append(f"{label} artifact entry parent is absent: {relative}")
    if total_bytes != snapshot.get("total_file_bytes"):
        failures.append(f"{label} artifact total byte count is invalid")
    return failures


def _validate_published_artifacts(
    root: Path, run_id: str, snapshot: dict[str, Any]
) -> list[str]:
    """Compare captured portable artifact paths/bytes with the published run."""

    failures: list[str] = []
    entries = snapshot.get("entries") if isinstance(snapshot, dict) else None
    if not isinstance(entries, dict):
        return ["published artifact comparison lacks a captured entry manifest"]
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    entry_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    held: list[int] = []
    actual: dict[str, str] = {}

    def walk(directory_fd: int, prefix: str) -> None:
        directory_before = os.fstat(directory_fd)
        if stat.S_IMODE(directory_before.st_mode) & 0o222:
            raise ValueError(f"published artifact directory is writable: {prefix or '.'}")
        names = sorted(os.listdir(directory_fd))
        if len(names) != len(set(names)):
            raise ValueError("published artifact directory contains duplicate names")
        for name in names:
            if not name or name in {".", ".."} or "/" in name or "\x00" in name:
                raise ValueError("published artifact tree contains an unsafe name")
            relative = f"{prefix}/{name}" if prefix else name
            if relative == "host_validation":
                continue
            if relative == f"metrics/{M0_RECEIPT_NAME}":
                continue
            descriptor = os.open(name, entry_flags, dir_fd=directory_fd)
            try:
                info = os.fstat(descriptor)
                if stat.S_ISDIR(info.st_mode):
                    if stat.S_IMODE(info.st_mode) & 0o222:
                        raise ValueError(f"published artifact directory is writable: {relative}")
                    os.close(descriptor)
                    descriptor = os.open(name, directory_flags, dir_fd=directory_fd)
                    actual[relative] = "directory"
                    walk(descriptor, relative)
                elif stat.S_ISREG(info.st_mode):
                    if info.st_nlink != 1:
                        raise ValueError(f"published artifact is hardlinked: {relative}")
                    if stat.S_IMODE(info.st_mode) & 0o222:
                        raise ValueError(f"published artifact is writable: {relative}")
                    actual[relative] = "file"
                else:
                    raise ValueError(f"published artifact has a special type: {relative}")
            finally:
                os.close(descriptor)
        directory_after = os.fstat(directory_fd)
        stable = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_mtime_ns", "st_ctime_ns")
        if any(
            getattr(directory_before, field) != getattr(directory_after, field)
            for field in stable
        ):
            raise ValueError(f"published artifact directory changed: {prefix or '.'}")

    try:
        root_fd = os.open(root, directory_flags)
        held.append(root_fd)
        runs_fd = os.open("runs", directory_flags, dir_fd=root_fd)
        held.append(runs_fd)
        run_fd = os.open(run_id, directory_flags, dir_fd=runs_fd)
        held.append(run_fd)
        walk(run_fd, "")
    except (OSError, ValueError) as exc:
        failures.append(f"published artifact enumeration failed: {exc}")
    finally:
        for descriptor in reversed(held):
            try:
                os.close(descriptor)
            except OSError:
                pass
    expected_kinds = {
        relative: "directory" if entry.get("kind") == "directory" else "file"
        for relative, entry in entries.items()
        if isinstance(entry, dict)
    }
    if actual != expected_kinds:
        failures.append("published portable artifact path/type set differs from host receipt")
        return failures
    for relative, entry in entries.items():
        if not isinstance(entry, dict) or entry.get("kind") != "file":
            continue
        try:
            payload = _secure_read_relative(
                root,
                f"runs/{run_id}/{relative}",
                maximum_bytes=MAX_RECEIPT_BYTES,
                require_read_only=True,
            )
        except ValueError as exc:
            failures.append(str(exc))
            continue
        if len(payload) != entry.get("bytes") or _sha256(payload) != entry.get("sha256"):
            failures.append(f"published artifact bytes differ from receipt: {relative}")
    return failures


def _portable_manifest_from_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    entries: dict[str, dict[str, Any]] = {}
    for relative, entry in sorted(snapshot.get("entries", {}).items()):
        if entry.get("kind") == "directory":
            entries[relative] = {"kind": "directory", "mode": 0o500}
        elif entry.get("kind") == "file":
            entries[relative] = {
                "kind": "file",
                "mode": 0o400,
                "bytes": entry.get("bytes"),
                "sha256": entry.get("sha256"),
            }
        else:
            raise ValueError(f"unsupported portable artifact entry: {relative}")
    return {
        "schema_version": 1,
        "contract": "ams.m0.portable-content-manifest/v1",
        "entries": entries,
        "entry_count": len(entries),
        "content_sha256": _sha256(_canonical_json(entries)),
    }


def _derive_execution_contract(
    root: Path, execution_commit: str, vector: dict[str, Any]
) -> dict[str, Any]:
    entries = vector.get("entry_manifest")
    if not isinstance(entries, list) or not entries:
        raise ValueError("execution Q-vector entry manifest is unavailable")
    bindings: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise ValueError("execution Q-vector contains a malformed source entry")
        if entry.get("owner") != "Q0":
            continue
        if entry.get("kind") == "gitlink":
            object_id = entry.get("git_object_id")
            if not isinstance(object_id, str) or SHA1.fullmatch(object_id) is None:
                raise ValueError("execution Q-vector gitlink identity is malformed")
            digest = _sha256(b"gitlink\0" + object_id.encode("ascii"))
        else:
            digest = entry.get("blob_sha256")
            if not isinstance(digest, str) or SHA256.fullmatch(digest) is None:
                raise ValueError("execution Q-vector blob identity is malformed")
        bindings[entry["path"]] = digest
    expected_q0_count = sum(
        isinstance(entry, dict) and entry.get("owner") == "Q0" for entry in entries
    )
    if list(bindings) != sorted(bindings) or len(bindings) != expected_q0_count:
        raise ValueError("execution Q-vector source paths are duplicate or unordered")

    manifest_path = "network/config/m0_test_manifest.json"
    manifest_raw = _git(root, ["show", f"{execution_commit}:{manifest_path}"])
    manifest = _strict_json(manifest_raw, "frozen M0 test manifest")
    manifest_modules = (
        manifest.get("test_modules") if isinstance(manifest, dict) else None
    )
    manifest_ids = (
        manifest.get("ordered_test_ids") if isinstance(manifest, dict) else None
    )
    if (
        not isinstance(manifest, dict)
        or set(manifest)
        != {
            "schema_version",
            "contract",
            "node",
            "discovery",
            "test_modules",
            "ordered_test_ids",
        }
        or manifest.get("schema_version") != 1
        or manifest.get("contract") != "ams.qualification-test-manifest/v1"
        or manifest.get("node") != "Q0"
        or manifest.get("discovery")
        != {"start_directory": "network/tests", "pattern": "test_*.py"}
        or not isinstance(manifest_modules, list)
        or manifest_modules != sorted(manifest_modules)
        or len(manifest_modules) != len(set(manifest_modules))
        or not all(
            isinstance(value, str)
            and re.fullmatch(r"test_[A-Za-z0-9_]+", value)
            for value in manifest_modules
        )
        or not isinstance(manifest_ids, list)
        or not manifest_ids
        or manifest_ids != sorted(manifest_ids)
        or len(manifest_ids) != len(set(manifest_ids))
        or not all(isinstance(value, str) and value for value in manifest_ids)
        or {value.split(".", 1)[0] for value in manifest_ids}
        != set(manifest_modules)
        or manifest_raw != _canonical_pretty_json(manifest)
    ):
        raise ValueError("frozen M0 test manifest schema/content is not exact")

    lock_raw = _git(
        root, ["show", f"{execution_commit}:network/config/dependency_lock.yaml"]
    )
    lock = _strict_yaml(lock_raw, "execution dependency lock")
    if not isinstance(lock, dict):
        raise ValueError("execution dependency lock is not a mapping")
    manifest_sha256 = _sha256(manifest_raw)
    expected_manifest_binding = {
        "path": manifest_path,
        "sha256": manifest_sha256,
        "ordered_test_count": len(manifest_ids),
    }
    if lock.get("m0_test_manifest") != expected_manifest_binding:
        raise ValueError("dependency lock does not exactly bind the frozen M0 test manifest")
    dependencies = lock.get("dependencies") if isinstance(lock.get("dependencies"), dict) else {}
    ros = dependencies.get("ros") if isinstance(dependencies.get("ros"), dict) else {}
    runtime_identity = (
        lock.get("m1_runtime_identity")
        if isinstance(lock.get("m1_runtime_identity"), dict)
        else {}
    )
    image_digest = ros.get("project_image_digest")
    image_reference = ros.get("project_image_reference")
    if (
        not isinstance(image_digest, str)
        or IMAGE_DIGEST.fullmatch(image_digest) is None
        or runtime_identity.get("container_image_digest") != image_digest
        or not isinstance(image_reference, str)
        or not image_reference
    ):
        raise ValueError("execution dependency lock image identity is not exact/coherent")
    execution_policy = lock.get("m0_execution_policy")
    if (
        not isinstance(execution_policy, dict)
        or execution_policy.get("schema_version") != 1
        or not isinstance(execution_policy.get("host_final_path"), str)
        or not execution_policy.get("host_final_path")
        or not isinstance(
            execution_policy.get("host_final_executable_sha256"), dict
        )
        or not execution_policy.get("host_final_executable_sha256")
        or not isinstance(
            execution_policy.get("critical_source_executables"), list
        )
        or not execution_policy.get("critical_source_executables")
        or not isinstance(execution_policy.get("host_final_python_sys_path"), list)
        or not execution_policy.get("host_final_python_sys_path")
        or not isinstance(execution_policy.get("host_final_python_imports"), dict)
        or not execution_policy.get("host_final_python_imports")
    ):
        raise ValueError("execution dependency lock M0 execution policy is incomplete")
    host_hashes = execution_policy["host_final_executable_sha256"]
    if any(
        not isinstance(path, str)
        or not path.startswith("/")
        or SHA256.fullmatch(str(digest or "")) is None
        for path, digest in host_hashes.items()
    ):
        raise ValueError("M0 host executable hash policy is malformed")
    if any(
        not isinstance(name, str)
        or not name
        or not isinstance(record, dict)
        or set(record) != {"path", "bytes", "sha256"}
        or not isinstance(record.get("path"), str)
        or not record.get("path", "").startswith("/")
        or isinstance(record.get("bytes"), bool)
        or not isinstance(record.get("bytes"), int)
        or record.get("bytes", 0) < 1
        or SHA256.fullmatch(str(record.get("sha256") or "")) is None
        for name, record in execution_policy["host_final_python_imports"].items()
    ):
        raise ValueError("M0 host Python import policy is malformed")
    source_executables = execution_policy["critical_source_executables"]
    if (
        len(source_executables) != len(set(source_executables))
        or any(
            not isinstance(path, str)
            or not _safe_relative(path)
            or path not in bindings
            for path in source_executables
        )
    ):
        raise ValueError("M0 critical source executable policy is malformed")
    import_policy = lock.get("m0_python_import_policy")
    if not isinstance(import_policy, dict) or import_policy.get("schema_version") != 1:
        raise ValueError("execution dependency lock M0 Python import policy is incomplete")
    return {
        "source_file_count": len(bindings),
        "source_binding_sha256": _sha256(_canonical_json(bindings)),
        "frozen_test_manifest_sha256": manifest_sha256,
        "frozen_test_count": len(manifest["ordered_test_ids"]),
        "frozen_test_ids": list(manifest["ordered_test_ids"]),
        "dependency_lock_sha256": _sha256(lock_raw),
        "image_digest": image_digest,
        "image_reference": image_reference,
        "m0_execution_policy": execution_policy,
        "m0_execution_policy_sha256": _sha256(_canonical_json(execution_policy)),
        "m0_python_import_policy": import_policy,
        "m0_python_import_policy_sha256": _sha256(_canonical_json(import_policy)),
    }


def _validate_host_execution_identity(
    identity: Any,
    *,
    execution_contract: dict[str, Any],
    expected_vector: dict[str, Any],
) -> list[str]:
    """Rebuild the receipt's host execution identity from committed policy/Q0."""

    expected_keys = {
        "schema_version",
        "contract",
        "execution_policy_sha256",
        "host_path",
        "host_executables",
        "host_python",
        "source_executables",
    }
    if not isinstance(identity, dict) or set(identity) != expected_keys:
        return ["host-final execution identity schema is not exact"]
    policy = execution_contract.get("m0_execution_policy")
    if not isinstance(policy, dict):
        return ["committed M0 execution policy is unavailable"]
    failures: list[str] = []
    if (
        identity.get("schema_version") != 1
        or identity.get("contract") != "ams.m0.host-execution-identity/v1"
        or identity.get("execution_policy_sha256")
        != execution_contract.get("m0_execution_policy_sha256")
        or identity.get("host_path") != policy.get("host_final_path")
    ):
        failures.append("host-final execution identity policy binding is invalid")

    expected_host_hashes = policy.get("host_final_executable_sha256")
    observed_host = identity.get("host_executables")
    if (
        not isinstance(expected_host_hashes, dict)
        or not isinstance(observed_host, dict)
        or set(observed_host) != set(expected_host_hashes)
    ):
        failures.append("host-final executable identity set differs from policy")
    else:
        for path, expected_sha256 in expected_host_hashes.items():
            record = observed_host.get(path)
            if (
                not isinstance(record, dict)
                or set(record) != {"bytes", "sha256"}
                or isinstance(record.get("bytes"), bool)
                or not isinstance(record.get("bytes"), int)
                or record.get("bytes", 0) < 1
                or record.get("sha256") != expected_sha256
            ):
                failures.append(f"host-final executable identity is invalid: {path}")
    expected_host_python = {
        "sys_path": policy.get("host_final_python_sys_path"),
        "third_party_imports": policy.get("host_final_python_imports"),
    }
    if identity.get("host_python") != expected_host_python:
        failures.append("host-final Python import identity differs from locked policy")

    entries = expected_vector.get("entry_manifest")
    by_path = (
        {
            entry.get("path"): entry
            for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("path"), str)
        }
        if isinstance(entries, list)
        else {}
    )
    expected_source: dict[str, dict[str, str]] = {}
    source_paths = policy.get("critical_source_executables")
    if not isinstance(source_paths, list):
        failures.append("committed critical source executable policy is unavailable")
    else:
        for relative in source_paths:
            entry = by_path.get(relative)
            if (
                not isinstance(entry, dict)
                or entry.get("object_type") != "blob"
                or entry.get("git_mode") != "100755"
                or SHA256.fullmatch(str(entry.get("blob_sha256") or "")) is None
            ):
                failures.append(
                    f"critical source executable is not an executable Q0 blob: {relative}"
                )
                continue
            expected_source[relative] = {
                "git_mode": entry["git_mode"],
                "sha256": entry["blob_sha256"],
            }
    if identity.get("source_executables") != expected_source:
        failures.append("host-final source executable identities differ from committed Q0")
    return failures


def _validate_source_snapshot(
    source: Any,
    *,
    execution_commit: str,
    expected_vector: dict[str, Any],
    plan_sha256: str,
    execution_contract: dict[str, Any],
) -> list[str]:
    expected_keys = {
        "git_commit",
        "source_file_count",
        "source_binding_sha256",
        "qualification_content_vector",
        "frozen_test_manifest_sha256",
        "frozen_test_count",
        "plan_path",
        "plan_sha256",
    }
    if not isinstance(source, dict) or set(source) != expected_keys:
        return ["host source snapshot schema is not exact"]
    failures: list[str] = []
    if (
        source.get("git_commit") != execution_commit
        or source.get("qualification_content_vector") != expected_vector
        or source.get("plan_path") != PLAN_PATH
        or source.get("plan_sha256") != plan_sha256
        or source.get("source_file_count") != execution_contract["source_file_count"]
        or source.get("source_binding_sha256")
        != execution_contract["source_binding_sha256"]
        or source.get("frozen_test_manifest_sha256")
        != execution_contract["frozen_test_manifest_sha256"]
        or source.get("frozen_test_count") != execution_contract["frozen_test_count"]
    ):
        failures.append("host source snapshot identities are invalid")
    return failures


def _container_immutable_fingerprint(document: dict[str, Any]) -> dict[str, Any]:
    config = document.get("Config") if isinstance(document.get("Config"), dict) else {}
    host = document.get("HostConfig") if isinstance(document.get("HostConfig"), dict) else {}
    mounts = document.get("Mounts") if isinstance(document.get("Mounts"), list) else []
    return {
        "Image": document.get("Image"),
        "Config": {
            key: config.get(key)
            for key in ("Image", "User", "Entrypoint", "Cmd", "WorkingDir", "Env")
        },
        "HostConfig": {
            key: host.get(key)
            for key in (
                "Privileged",
                "NetworkMode",
                "ReadonlyRootfs",
                "RestartPolicy",
                "Tmpfs",
                "CapAdd",
                "CapDrop",
                "SecurityOpt",
                "Devices",
                "DeviceRequests",
            )
        },
        "Mounts": [
            {
                key: mount.get(key)
                for key in ("Type", "Source", "Destination", "Mode", "RW", "Propagation")
            }
            for mount in mounts
            if isinstance(mount, dict)
        ],
    }


def _docker_environment(values: Any) -> tuple[dict[str, str], list[str]]:
    failures: list[str] = []
    result: dict[str, str] = {}
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        return {}, ["raw retained-container Config.Env is not a string list"]
    for value in values:
        name, separator, content = value.partition("=")
        if not separator or not name or name in result:
            failures.append("raw retained-container Config.Env has malformed/duplicate names")
            continue
        result[name] = content
    return result, failures


def _one_raw_json(
    payloads: dict[str, bytes], name: str, label: str
) -> tuple[Any, list[str]]:
    payload = payloads.get(name)
    if payload is None:
        return None, [f"{label} raw file is missing: {name}"]
    try:
        return _strict_json(payload, label), []
    except ValueError as exc:
        return None, [str(exc)]


def _validate_source_raw(
    payloads: dict[str, bytes], details: dict[str, Any]
) -> list[str]:
    document, failures = _one_raw_json(
        payloads, "source/identity.json", "host-final source identity"
    )
    expected = {
        "schema_version": 1,
        "contract": "ams.m0.host-source-reexecution/v1",
        "technical_source_before": details.get("source_before"),
        "technical_source_after": details.get("source_after"),
        "producer_source": details.get("producer_source_identity"),
        "fresh_source_before": details.get("fresh_source_before"),
        "fresh_source_after": details.get("fresh_source_after"),
        "external_before": details.get("external_before"),
        "external_after": details.get("external_after"),
    }
    if document != expected:
        failures.append("host-final source raw record differs from receipt identities")
    return failures


def _validate_capability_raw(
    payloads: dict[str, bytes],
    capability: Any,
    *,
    image_digest: str,
) -> list[str]:
    failures: list[str] = []
    if not isinstance(capability, dict):
        return ["isolated capability receipt details are unavailable"]
    parsed: dict[str, Any] = {}
    for name in (
        "capability/command.json",
        "capability/initial_container_inspect.json",
        "capability/final_container_inspect.json",
        "capability/image_inspect.json",
    ):
        document, errors = _one_raw_json(payloads, name, name)
        failures.extend(errors)
        parsed[name] = document
    command = parsed.get("capability/command.json")
    initial_docs = parsed.get("capability/initial_container_inspect.json")
    final_docs = parsed.get("capability/final_container_inspect.json")
    image_docs = parsed.get("capability/image_inspect.json")
    initial = (
        initial_docs[0]
        if isinstance(initial_docs, list)
        and len(initial_docs) == 1
        and isinstance(initial_docs[0], dict)
        else {}
    )
    final = (
        final_docs[0]
        if isinstance(final_docs, list)
        and len(final_docs) == 1
        and isinstance(final_docs[0], dict)
        else {}
    )
    image = (
        image_docs[0]
        if isinstance(image_docs, list)
        and len(image_docs) == 1
        and isinstance(image_docs[0], dict)
        else {}
    )
    container_id = capability.get("container_id")
    command_script = M0_CAPABILITY_COMMAND_SCRIPT
    expected_create = [
        "docker",
        "create",
        "--hostname",
        "ams-m0-capability",
        "--add-host",
        "ams-m0-capability:127.0.1.1",
        "--user",
        "ubuntu",
        "--restart=no",
        "--cap-drop=ALL",
        "--cap-add=ALL",
        "--device",
        "/dev/net/tun:/dev/net/tun:rwm",
        "--network=none",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,exec,size=64m,mode=1777",
        image_digest,
        "/bin/bash",
        "-c",
        command_script,
    ]
    if command != {
        "schema_version": 1,
        "contract": "ams.m0.isolated-capability-probe/v1",
        "container_id": container_id,
        "image_digest": image_digest,
        "create_argv": expected_create,
        "command": ["/bin/bash", "-c", command_script],
        "source_or_artifact_mounts": False,
    }:
        failures.append("isolated capability command raw record is not exact")
    initial_state = initial.get("State") if isinstance(initial.get("State"), dict) else {}
    final_state = final.get("State") if isinstance(final.get("State"), dict) else {}
    config = final.get("Config") if isinstance(final.get("Config"), dict) else {}
    host = final.get("HostConfig") if isinstance(final.get("HostConfig"), dict) else {}
    devices = host.get("Devices")
    if (
        initial.get("Id") != container_id
        or initial.get("Image") != image_digest
        or initial_state.get("Status") != "created"
        or initial_state.get("Running") is not False
        or final.get("Id") != container_id
        or final.get("Image") != image_digest
        or final_state.get("Status") != "exited"
        or final_state.get("Running") is not False
        or final_state.get("ExitCode") != 0
        or final_state.get("OOMKilled") is not False
        or final.get("RestartCount") != 0
    ):
        failures.append("isolated capability raw lifecycle is invalid")
    if (
        image.get("Id") != image_digest
        or config.get("User") != "ubuntu"
        or config.get("Entrypoint") != ["/ros_entrypoint.sh"]
        or config.get("Cmd") != ["/bin/bash", "-c", command_script]
        or host.get("Privileged") is not False
        or host.get("NetworkMode") != "none"
        or host.get("ReadonlyRootfs") is not True
        or host.get("CapDrop") != ["ALL"]
        or host.get("CapAdd") != ["ALL"]
        or host.get("RestartPolicy") != {"Name": "no", "MaximumRetryCount": 0}
        or host.get("Tmpfs")
        != {"/tmp": "rw,nosuid,nodev,exec,size=64m,mode=1777"}
        or host.get("ExtraHosts") != ["ams-m0-capability:127.0.1.1"]
        or initial.get("Mounts") != []
        or final.get("Mounts") != []
        or not isinstance(devices, list)
        or len(devices) != 1
        or devices[0].get("PathOnHost") != "/dev/net/tun"
        or devices[0].get("PathInContainer") != "/dev/net/tun"
        or devices[0].get("CgroupPermissions") != "rwm"
    ):
        failures.append("isolated capability raw configuration is not exact")
    if _container_immutable_fingerprint(initial) != _container_immutable_fingerprint(final):
        failures.append("isolated capability container configuration changed")
    stdout = payloads.get("capability/stdout.txt")
    stderr = payloads.get("capability/stderr.txt")
    if stdout != M0_CAPABILITY_STDOUT or stderr != b"":
        failures.append("isolated capability raw command output is not exact")
    expected_summary = {
        "contract": "ams.m0.isolated-capability-probe/v1",
        "container_id": container_id,
        "image_digest": image_digest,
        "exit_code": 0,
        "no_candidate_mounts": True,
        "tun_device": True,
        "passwordless_sudo": True,
        "unshare_network_namespace": True,
        "raw_sha256": {
            name: _sha256(payloads[name])
            for name in (
                "capability/initial_container_inspect.json",
                "capability/final_container_inspect.json",
                "capability/image_inspect.json",
                "capability/stdout.txt",
                "capability/stderr.txt",
                "capability/command.json",
            )
            if name in payloads
        },
    }
    if capability != expected_summary:
        failures.append("isolated capability receipt does not rederive from raw evidence")
    return failures


def _validate_fresh_raw(
    root: Path,
    payloads: dict[str, bytes],
    fresh: Any,
    *,
    run_id: str,
    image_digest: str,
    source_commit: str,
    execution_contract: dict[str, Any],
    expected_vector: dict[str, Any],
    plan_sha256: str,
) -> list[str]:
    failures: list[str] = []
    if not isinstance(fresh, dict):
        return ["fresh exact-image receipt details are unavailable"]
    parsed: dict[str, Any] = {}
    for name in (
        "fresh/initial_container_inspect.json",
        "fresh/final_container_inspect.json",
        "fresh/image_inspect.json",
        "fresh/operational_snapshot_before.json",
        "fresh/operational_snapshot_after.json",
    ):
        document, errors = _one_raw_json(payloads, name, name)
        failures.extend(errors)
        parsed[name] = document

    def one(name: str) -> dict[str, Any]:
        documents = parsed.get(name)
        return (
            documents[0]
            if isinstance(documents, list)
            and len(documents) == 1
            and isinstance(documents[0], dict)
            else {}
        )

    initial = one("fresh/initial_container_inspect.json")
    final = one("fresh/final_container_inspect.json")
    image = one("fresh/image_inspect.json")
    before = parsed.get("fresh/operational_snapshot_before.json")
    after = parsed.get("fresh/operational_snapshot_after.json")
    container_id = fresh.get("container_id")
    initial_state = initial.get("State") if isinstance(initial.get("State"), dict) else {}
    final_state = final.get("State") if isinstance(final.get("State"), dict) else {}
    if (
        initial.get("Id") != container_id
        or initial.get("Image") != image_digest
        or initial_state.get("Status") != "created"
        or initial_state.get("Running") is not False
        or initial.get("RestartCount") != 0
        or final.get("Id") != container_id
        or final.get("Image") != image_digest
        or final_state.get("Status") != "exited"
        or final_state.get("Running") is not False
        or final_state.get("Paused") is not False
        or final_state.get("Restarting") is not False
        or final_state.get("OOMKilled") is not False
        or final_state.get("Dead") is not False
        or final_state.get("ExitCode") != 0
        or final.get("RestartCount") != 0
        or image.get("Id") != image_digest
    ):
        failures.append("fresh exact-image raw lifecycle/image identity is invalid")
    config = final.get("Config") if isinstance(final.get("Config"), dict) else {}
    host = final.get("HostConfig") if isinstance(final.get("HostConfig"), dict) else {}
    environment, environment_failures = _docker_environment(config.get("Env"))
    failures.extend(environment_failures)
    expected_environment = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "ROS_DISTRO": "humble",
        "DEBIAN_FRONTEND": "noninteractive",
        "GZ_VERSION": "harmonic",
        "USER": "ubuntu",
        "LOGNAME": "ubuntu",
        "HOME": "/home/ubuntu",
        "NVIDIA_VISIBLE_DEVICES": "all",
        "NVIDIA_DRIVER_CAPABILITIES": "compute,utility",
        "SIONNA_MITSUBA_VARIANT": "cuda_ad_mono_polarized",
        "AMS_CONTAINER_IMAGE": image_digest,
        "AMS_CONTAINER_IMAGE_DIGEST": image_digest,
        "AMS_CONTAINER_IMAGE_DIGEST_SOURCE": "docker_image_inspect_host",
        "AMS_RUNTIME_CONTAINER_ID_FILE": "/run/ams/container_id",
        "AMS_M0_SOURCE_MODE": "clean_git_clone_ro",
        "AMS_M0_SOURCE_COMMIT": source_commit,
        "AMS_M0_PROJECT_OVERLAY_MODE": "none_q0_source_only",
        "AMS_M0_ARTIFACT_ROOT": "/run/ams/m0-artifacts",
        "AMS_M0_COLLECTION_SECURITY": "cap_drop_all_no_new_privileges",
        "AMS_M0_CAPABILITY_PROBE_MODE": "host_final_isolated_exact_image",
    }
    expected_command = [
        "scripts/acceptance_entrypoint.sh",
        "network/scripts/run_m0_host_reexecution.sh",
        run_id,
    ]
    if (
        environment != expected_environment
        or config.get("Image") != image_digest
        or config.get("User") != "ubuntu"
        or config.get("Entrypoint") != ["/ros_entrypoint.sh"]
        or config.get("Cmd") != expected_command
        or config.get("WorkingDir") != "/workspace/multiagent_simulation"
    ):
        failures.append("fresh exact-image raw Config/environment is not exact")
    if (
        host.get("Privileged") is not False
        or host.get("NetworkMode") != "none"
        or host.get("ReadonlyRootfs") is not True
        or host.get("RestartPolicy") != {"Name": "no", "MaximumRetryCount": 0}
        or host.get("Tmpfs")
        != {"/tmp": "rw,nosuid,nodev,exec,size=4g,mode=1777"}
        or host.get("CapAdd") is not None
        or host.get("CapDrop") != ["ALL"]
        or host.get("SecurityOpt")
        not in (
            ["no-new-privileges"],
            ["no-new-privileges:true"],
            ["label=disable", "no-new-privileges"],
            ["label=disable", "no-new-privileges:true"],
        )
        or host.get("Devices") != []
        or host.get("DeviceRequests") != EXPECTED_GPU_DEVICE_REQUESTS
    ):
        failures.append("fresh exact-image raw HostConfig isolation is not exact")
    mounts = final.get("Mounts") if isinstance(final.get("Mounts"), list) else []
    by_destination = {
        record.get("Destination"): record
        for record in mounts
        if isinstance(record, dict)
    }
    expected_destinations = {
        "/run/ams/container_id",
        "/run/ams/m0-artifacts",
        "/workspace/multiagent_simulation",
        "/workspace/multiagent_simulation/.external/ns-3",
    }
    if len(mounts) != 4 or len(by_destination) != 4 or set(by_destination) != expected_destinations:
        failures.append("fresh exact-image raw mount destination set is not exact")
    patterns = {
        "/run/ams/container_id": r"/tmp/ams-m0-reexec-id\.[A-Za-z0-9_]+",
        "/run/ams/m0-artifacts": (
            rf"/tmp/ams-m0-reexec-{re.escape(run_id)}\.[A-Za-z0-9_]+"
        ),
        "/workspace/multiagent_simulation": (
            r"/tmp/ams-m0-host-source-[0-9a-f]{12}\.[A-Za-z0-9_]+"
        ),
    }
    for destination, pattern in patterns.items():
        record = by_destination.get(destination, {})
        writable = destination == "/run/ams/m0-artifacts"
        if (
            record.get("Type") != "bind"
            or re.fullmatch(pattern, str(record.get("Source") or "")) is None
            or record.get("RW") is not writable
            or record.get("Mode") != ("rw" if writable else "ro")
            or record.get("Propagation") != "rprivate"
        ):
            failures.append(f"fresh exact-image raw mount is invalid: {destination}")
    external_mount = by_destination.get(
        "/workspace/multiagent_simulation/.external/ns-3", {}
    )
    if (
        external_mount.get("Type") != "bind"
        or external_mount.get("Source")
        != str((root / ".external/ns-3").resolve(strict=False))
        or external_mount.get("RW") is not False
        or external_mount.get("Mode") != "ro"
        or external_mount.get("Propagation") != "rprivate"
    ):
        failures.append("fresh exact-image external-source mount is invalid")
    if _container_immutable_fingerprint(initial) != _container_immutable_fingerprint(final):
        failures.append("fresh exact-image immutable container configuration changed")
    failures.extend(_validate_snapshot(before, "fresh-raw-before"))
    failures.extend(_validate_snapshot(after, "fresh-raw-after"))
    if before != after:
        failures.append("fresh exact-image operational snapshots differ")
    if before != fresh.get("artifact_snapshot_before") or after != fresh.get(
        "artifact_snapshot_after"
    ):
        failures.append("fresh receipt snapshots differ from raw operational snapshots")
    entries = before.get("entries") if isinstance(before, dict) else None
    expected_output_files = {
        relative: entry
        for relative, entry in (entries or {}).items()
        if isinstance(entry, dict) and entry.get("kind") == "file"
    }
    actual_output_names = {
        name.removeprefix("fresh/output/")
        for name in payloads
        if name.startswith("fresh/output/")
    }
    if actual_output_names != set(expected_output_files):
        failures.append("fresh raw output file set differs from operational snapshot")
    for relative, entry in expected_output_files.items():
        payload = payloads.get(f"fresh/output/{relative}")
        if (
            payload is None
            or entry.get("bytes") != len(payload)
            or entry.get("sha256") != _sha256(payload)
        ):
            failures.append(f"fresh raw output differs from snapshot: {relative}")

    def output(relative: str) -> bytes | None:
        return payloads.get(f"fresh/output/{relative}")

    try:
        from network.scripts.validate_m0_baseline import (
            _parse_dependency_output,
            _parse_host_unittest_output,
        )
        from network.scripts.run_m0_validation_suite import (
            load_m0_import_policy,
            suite_external_bindings,
            suite_source_bindings,
            validate_m0_import_trace_record,
        )

        dependency_stdout_raw = output("check_deps.stdout")
        dependency_stderr = output("check_deps.stderr")
        dependency_exit = output("check_deps.exit_code")
        if dependency_stdout_raw is None:
            raise ValueError("fresh dependency stdout is missing")
        dependency_stdout = dependency_stdout_raw.decode("utf-8", errors="strict")
        dependency_failures, dependency_records, warnings, dependency_hash = (
            _parse_dependency_output(dependency_stdout)
        )
        failures.extend(f"fresh dependency raw: {item}" for item in dependency_failures)
        if dependency_stderr != b"" or dependency_exit != b"0\n":
            failures.append("fresh dependency raw stderr/exit is not exact")
        if (
            fresh.get("dependency_record_count") != len(dependency_records)
            or fresh.get("dependency_warning_count") != warnings
            or fresh.get("dependency_stdout_sha256") != dependency_hash
        ):
            failures.append("fresh dependency receipt does not rederive from raw output")

        runtime_raw = output("runtime_lock.json")
        runtime_stderr = output("runtime_lock.stderr")
        runtime_exit = output("runtime_lock.exit_code")
        if runtime_raw is None:
            raise ValueError("fresh runtime-lock JSON is missing")
        runtime = _strict_json(runtime_raw, "fresh runtime-lock report")
        required_runtime_checks = {
            "lock",
            "image_digest",
            "runtime_manifests",
            "runtime_identity_files",
            "m0_execution_policy",
            "external_sources",
            "ns3_tree",
        }
        runtime_checks = (
            runtime.get("checks") if isinstance(runtime, dict) else None
        )
        if (
            not isinstance(runtime, dict)
            or set(runtime)
            != {
                "schema_version",
                "contract",
                "passed",
                "observed_image_digest",
                "lock_sha256",
                "checks",
                "failures",
            }
            or runtime.get("schema_version") != 1
            or runtime.get("contract") != "ams.m0.runtime-lock-verification/v1"
            or runtime.get("passed") is not True
            or runtime.get("failures") != []
            or runtime.get("observed_image_digest") != image_digest
            or runtime.get("lock_sha256")
            != execution_contract.get("dependency_lock_sha256")
            or not isinstance(runtime_checks, dict)
            or set(runtime_checks) != required_runtime_checks
            or any(
                not isinstance(runtime_checks.get(name), dict)
                or runtime_checks[name].get("status") != "passed"
                for name in required_runtime_checks
            )
            or runtime_stderr != b""
            or runtime_exit != b"0\n"
        ):
            failures.append("fresh runtime-lock raw report did not pass exactly")
        if fresh.get("runtime_lock_sha256") != _sha256(runtime_raw):
            failures.append("fresh runtime-lock receipt hash differs from raw report")

        guard_raw = output("python_guard.json")
        guard = (
            _strict_json(guard_raw, "fresh Python guard")
            if guard_raw is not None
            else None
        )
        expected_guard = {
            "guard_marker": True,
            "no_site": 0,
            "sitecustomize_path": (
                "/workspace/multiagent_simulation/network/scripts/"
                "m0_python_guard/sitecustomize.py"
            ),
            "usercustomize_loaded": False,
        }
        if guard != expected_guard or fresh.get("python_guard") != expected_guard:
            failures.append("fresh Python guard raw record is not exact")

        suite_stdout = output("suite_runner.stdout")
        suite_stderr = output("suite_runner.stderr")
        suite_exit = output("suite_runner.exit_code")
        suite_log = output(f"{run_id}/logs/m0_validation_suite.log")
        suite_document_raw = output(f"{run_id}/metrics/m0_validation_suite.json")
        if suite_log is None or suite_document_raw is None:
            raise ValueError("fresh validation-suite raw records are missing")
        expected_ids = execution_contract.get("frozen_test_ids")
        if not isinstance(expected_ids, list) or not expected_ids:
            raise ValueError("frozen test identities are unavailable")
        suite_log_text = suite_log.decode("utf-8", errors="strict")
        suite_failures, raw_passing_ids, suite_hash = _parse_host_unittest_output(
            suite_log_text, expected_ids
        )
        failures.extend(f"fresh suite raw: {item}" for item in suite_failures)
        suite_document = _strict_json(
            suite_document_raw, "fresh validation-suite result"
        )
        if not isinstance(suite_document, dict):
            raise ValueError("fresh validation-suite result is not an object")
        expected_suite_keys = {
            "schema_version",
            "suite",
            "started_utc",
            "completed_utc",
            "execution_identity",
            "invocation",
            "python_executable",
            "python_import_trace",
            "source_bindings",
            "source_bindings_after",
            "qualification_content_vector",
            "plan_contract",
            "external_input_bindings",
            "external_input_bindings_after",
            "frozen_test_manifest",
            "discovery",
            "execution",
            "raw_log",
            "producer_observation",
        }
        discovery = suite_document.get("discovery")
        execution = suite_document.get("execution")
        outcomes = execution.get("outcomes") if isinstance(execution, dict) else None
        passing_ids = (
            [
                record.get("test_id")
                for record in outcomes
                if isinstance(record, dict) and record.get("outcome") == "passed"
            ]
            if isinstance(outcomes, list)
            else []
        )
        expected_stdout = (
            f"M0 validation/adversarial suite recorded {len(expected_ids)} tests; "
            "passed=true\n"
        ).encode("utf-8")
        if (
            set(suite_document) != expected_suite_keys
            or suite_document.get("schema_version") != 5
            or suite_document.get("suite")
            != "complete_network_validation_adversarial_unittest"
            or suite_document.get("producer_observation") != {"passed": True}
            or not isinstance(discovery, dict)
            or discovery.get("start_directory") != "network/tests"
            or discovery.get("pattern") != "test_*.py"
            or discovery.get("test_count") != len(expected_ids)
            or discovery.get("test_ids") != expected_ids
            or not isinstance(execution, dict)
            or execution.get("started_test_ids") != expected_ids
            or execution.get("tests_run") != len(expected_ids)
            or passing_ids != expected_ids
            or raw_passing_ids != expected_ids
            or suite_stdout != expected_stdout
            or suite_stderr != b""
            or suite_exit != b"0\n"
            or suite_document.get("raw_log")
            != {
                "path": "logs/m0_validation_suite.log",
                "bytes": len(suite_log),
                "sha256": _sha256(suite_log),
            }
        ):
            failures.append("fresh validation-suite raw result/output is not exact all-pass")
        current_source_bindings = suite_source_bindings(root)
        current_external_bindings = suite_external_bindings(root)
        if (
            suite_document.get("source_bindings") != current_source_bindings
            or suite_document.get("source_bindings_after") != current_source_bindings
            or suite_document.get("external_input_bindings")
            != current_external_bindings
            or suite_document.get("external_input_bindings_after")
            != current_external_bindings
            or suite_document.get("qualification_content_vector") != expected_vector
            or suite_document.get("plan_contract")
            != {
                "plan_version": 3,
                "path": PLAN_PATH,
                "contract_sha256": plan_sha256,
            }
            or suite_document.get("frozen_test_manifest")
            != {
                "path": "network/config/m0_test_manifest.json",
                "sha256": execution_contract.get("frozen_test_manifest_sha256"),
            }
        ):
            failures.append("fresh validation-suite committed/external inputs are invalid")
        identity = suite_document.get("execution_identity")
        import_policy = execution_contract.get("m0_python_import_policy")
        expected_pycache = f"/tmp/ams-m0-pycache-{run_id}"
        if (
            not isinstance(identity, dict)
            or identity.get("container_image_digest") != image_digest
            or identity.get("container_image_digest_source")
            != "docker_image_inspect_host"
            or identity.get("runtime_container_id") != container_id
            or identity.get("runtime_container_id_source") != "host_bind_mount"
            or identity.get("source_mode") != "clean_git_clone_ro"
            or identity.get("source_commit") != source_commit
            or identity.get("source_mount_read_only") is not True
            or identity.get("project_overlay_mode") != "none_q0_source_only"
            or identity.get("python_no_site") is not True
            or identity.get("python_pycache_prefix") != expected_pycache
            or identity.get("sitecustomize_loaded") is not False
            or identity.get("usercustomize_loaded") is not False
        ):
            failures.append("fresh validation-suite execution identity is invalid")
        import_policy_live, import_policy_sha256 = load_m0_import_policy(root)
        if import_policy != import_policy_live:
            failures.append("fresh committed Python import policy binding is invalid")
        trace_failures = validate_m0_import_trace_record(
            suite_document.get("python_import_trace"),
            import_policy_live,
            import_policy_sha256,
            run_id,
            current_source_bindings,
        )
        failures.extend(f"fresh import trace: {item}" for item in trace_failures)
        trace = suite_document.get("python_import_trace")
        if (
            fresh.get("passing_test_count") != len(passing_ids)
            or fresh.get("unittest_stderr_sha256") != suite_hash
            or fresh.get("python_import_trace") != trace
            or fresh.get("python_import_trace_sha256")
            != _sha256(_canonical_json(trace))
        ):
            failures.append("fresh validation-suite receipt does not rederive from raw evidence")
    except (ImportError, UnicodeError, ValueError, yaml.YAMLError) as exc:
        failures.append(f"fresh raw semantic derivation failed: {exc}")

    core_raw_names = (
        "fresh/initial_container_inspect.json",
        "fresh/final_container_inspect.json",
        "fresh/image_inspect.json",
        "fresh/container_stdout.txt",
        "fresh/container_stderr.txt",
        "fresh/operational_snapshot_before.json",
        "fresh/operational_snapshot_after.json",
    )
    expected_raw_hashes = {
        name: _sha256(payloads[name]) for name in core_raw_names if name in payloads
    }
    if (
        fresh.get("exit_code") != 0
        or fresh.get("image_digest") != image_digest
        or fresh.get("prestart_container_inspect_sha256")
        != _sha256(payloads.get("fresh/initial_container_inspect.json", b""))
        or fresh.get("final_container_inspect_sha256")
        != _sha256(payloads.get("fresh/final_container_inspect.json", b""))
        or fresh.get("container_stdout_sha256")
        != _sha256(payloads.get("fresh/container_stdout.txt", b""))
        or fresh.get("container_stderr_sha256")
        != _sha256(payloads.get("fresh/container_stderr.txt", b""))
        or fresh.get("raw_sha256") != expected_raw_hashes
    ):
        failures.append("fresh exact-image receipt hashes/results do not rederive from raw")
    return failures


def _validate_control_raw(
    root: Path,
    receipt: dict[str, Any],
    *,
    run_id: str,
    container_id: str,
    image_digest: str,
    image_reference: str,
    source_commit: str,
    execution_contract: dict[str, Any],
    expected_vector: dict[str, Any],
    plan_sha256: str,
) -> list[str]:
    failures: list[str] = []
    host_raw = receipt.get("host_validation_raw")
    expected_path = f"runs/{run_id}/host_validation"
    if (
        not isinstance(host_raw, dict)
        or set(host_raw)
        != {
            "path",
            "never_mounted",
            "contract",
            "file_count",
            "content_sha256",
            "files",
        }
        or host_raw.get("path") != expected_path
        or host_raw.get("never_mounted") is not True
        or host_raw.get("contract") != "ams.m0.host-validation-content/v1"
        or not isinstance(host_raw.get("files"), dict)
        or isinstance(host_raw.get("file_count"), bool)
        or not isinstance(host_raw.get("file_count"), int)
        or host_raw.get("file_count", 0) < len(RETAINED_CONTROL_FILE_NAMES)
        or SHA256.fullmatch(str(host_raw.get("content_sha256") or "")) is None
    ):
        return ["host-validation raw manifest schema/path is not exact"]
    try:
        control_names = _secure_tree_files(root, expected_path)
        if control_names != sorted(host_raw["files"]):
            failures.append(
                "host-validation directory has extra/missing files relative to its manifest"
            )
    except ValueError as exc:
        failures.append(str(exc))
        control_names = []
    if (
        host_raw.get("file_count") != len(host_raw["files"])
        or host_raw.get("file_count") != len(control_names)
        or _sha256(_canonical_json(host_raw["files"]))
        != host_raw.get("content_sha256")
        or not set(RETAINED_CONTROL_FILE_NAMES).issubset(host_raw["files"])
    ):
        failures.append("host-validation content manifest identity is invalid")
    payloads: dict[str, bytes] = {}
    for name in sorted(host_raw["files"]):
        if not isinstance(name, str) or not _safe_relative(name):
            failures.append("host-validation raw manifest contains an unsafe file path")
            continue
        relative = f"{expected_path}/{name}"
        try:
            payload = _secure_read_relative(
                root,
                relative,
                maximum_bytes=MAX_CONTROL_BYTES,
                require_read_only=True,
                allow_empty=True,
            )
            payloads[name] = payload
        except ValueError as exc:
            failures.append(str(exc))
            continue
        record = host_raw["files"].get(name)
        expected = {
            "bytes": len(payload),
            "sha256": _sha256(payload),
            "published_mode": 0o400,
        }
        if record != expected:
            failures.append(f"host-validation raw binding is invalid: {name}")
    if len(payloads) != len(host_raw["files"]):
        return failures
    content_manifest_payload = payloads.get("content_manifest.json")
    if content_manifest_payload is None:
        failures.append("host-validation content_manifest.json is missing")
    else:
        try:
            embedded_manifest = _strict_json(
                content_manifest_payload, "host-validation content manifest"
            )
            embedded_files = {
                name: record
                for name, record in host_raw["files"].items()
                if name != "content_manifest.json"
            }
            expected_embedded = {
                "schema_version": 1,
                "contract": "ams.m0.host-validation-content/v1",
                "files": embedded_files,
                "file_count": len(embedded_files),
                "content_sha256": _sha256(_canonical_json(embedded_files)),
            }
            if embedded_manifest != expected_embedded:
                failures.append("embedded host-validation content manifest is invalid")
        except ValueError as exc:
            failures.append(str(exc))

    host_details = receipt.get("gates", {}).get("host_final", {}).get("details", {})
    raw_bindings = (
        (
            "retained",
            host_details.get("retained_container_reinspection", {}).get("raw_sha256")
            if isinstance(host_details, dict)
            and isinstance(host_details.get("retained_container_reinspection"), dict)
            else None,
            set(RETAINED_CONTROL_FILE_NAMES),
        ),
        (
            "fresh",
            host_details.get("fresh_exact_image_reexecution", {}).get("raw_sha256")
            if isinstance(host_details, dict)
            and isinstance(host_details.get("fresh_exact_image_reexecution"), dict)
            else None,
            {
                "fresh/initial_container_inspect.json",
                "fresh/final_container_inspect.json",
                "fresh/image_inspect.json",
                "fresh/container_stdout.txt",
                "fresh/container_stderr.txt",
                "fresh/operational_snapshot_before.json",
                "fresh/operational_snapshot_after.json",
            },
        ),
        (
            "capability",
            host_details.get("isolated_target_runtime_capability", {}).get("raw_sha256")
            if isinstance(host_details, dict)
            and isinstance(host_details.get("isolated_target_runtime_capability"), dict)
            else None,
            {
                "capability/initial_container_inspect.json",
                "capability/final_container_inspect.json",
                "capability/image_inspect.json",
                "capability/stdout.txt",
                "capability/stderr.txt",
                "capability/command.json",
            },
        ),
    )
    for label, binding, expected_names in raw_bindings:
        if not isinstance(binding, dict) or set(binding) != expected_names:
            failures.append(f"{label} raw-hash binding schema is not exact")
            continue
        if any(
            name not in payloads or digest != _sha256(payloads[name])
            for name, digest in binding.items()
        ):
            failures.append(f"{label} raw hashes do not bind published host evidence")
    required_fresh_outputs = {
        "check_deps.stdout",
        "check_deps.stderr",
        "check_deps.exit_code",
        "runtime_lock.json",
        "runtime_lock.stderr",
        "runtime_lock.exit_code",
        "python_guard.json",
        "suite_runner.stdout",
        "suite_runner.stderr",
        "suite_runner.exit_code",
        f"{run_id}/logs/m0_validation_suite.log",
        f"{run_id}/metrics/m0_validation_suite.json",
    }
    if not {
        name.removeprefix("fresh/output/")
        for name in payloads
        if name.startswith("fresh/output/")
    }.issuperset(required_fresh_outputs):
        failures.append("fresh raw output package is incomplete")
    if "source/identity.json" not in payloads:
        failures.append("host-final source identity raw record is missing")
    if "execution/host_identity.json" not in payloads:
        failures.append("host-final execution identity raw record is missing")
    else:
        expected_host_identity = host_details.get("host_execution_identity")
        expected_host_payload = (
            json.dumps(expected_host_identity, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        if payloads["execution/host_identity.json"] != expected_host_payload:
            failures.append("host-final execution identity raw record differs from receipt")
    documents: dict[str, Any] = {}
    for name in RETAINED_CONTROL_FILE_NAMES:
        payload = payloads.get(name)
        if payload is None:
            continue
        try:
            documents[name] = _strict_json(payload, name)
        except ValueError as exc:
            failures.append(str(exc))
    if len(documents) != len(RETAINED_CONTROL_FILE_NAMES):
        return failures

    prestart = documents["retained/prestart_inspection_record.json"]
    expected_prestart_keys = {
        "schema_version",
        "contract",
        "created_utc",
        "container_id",
        "image_id",
        "artifact_root_initial",
        "initial_container_inspect",
        "initial_image_inspect",
    }
    if not isinstance(prestart, dict) or set(prestart) != expected_prestart_keys:
        failures.append("prestart inspection record schema is not exact")
        prestart = {}
    if (
        prestart.get("schema_version") != 1
        or prestart.get("contract") != "ams.m0.prestart-inspection/v1"
        or prestart.get("container_id") != container_id
        or prestart.get("image_id") != image_digest
        or UTC_TIMESTAMP.fullmatch(str(prestart.get("created_utc") or "")) is None
    ):
        failures.append("prestart inspection record identity is invalid")
    artifact_initial = prestart.get("artifact_root_initial")
    if (
        not isinstance(artifact_initial, dict)
        or set(artifact_initial)
        != {
            "path",
            "device",
            "inode",
            "mode",
            "entry_count",
            "content_manifest_sha256",
        }
        or not isinstance(artifact_initial.get("path"), str)
        or not Path(artifact_initial.get("path", "")).is_absolute()
        or Path(artifact_initial.get("path", "")).parent != root.parent
        or re.fullmatch(
            rf"\.ams-m0-artifacts-{re.escape(run_id)}\.[A-Za-z0-9]{{10}}",
            Path(artifact_initial.get("path", "")).name,
        )
        is None
        or any(
            isinstance(artifact_initial.get(key), bool)
            or not isinstance(artifact_initial.get(key), int)
            for key in ("device", "inode", "mode", "entry_count")
        )
        or artifact_initial.get("entry_count") != 0
        or artifact_initial.get("mode") != 0o700
        or artifact_initial.get("device", 0) < 1
        or artifact_initial.get("inode", 0) < 1
        or artifact_initial.get("content_manifest_sha256") != _sha256(b"[]")
    ):
        failures.append("prestart empty artifact-root identity is invalid")
    for key, name in (
        ("initial_container_inspect", "retained/initial_container_inspect.json"),
        ("initial_image_inspect", "retained/initial_image_inspect.json"),
    ):
        producer_name = name.removeprefix("retained/")
        expected = {
            "path": producer_name,
            "bytes": len(payloads[name]),
            "sha256": _sha256(payloads[name]),
        }
        if prestart.get(key) != expected:
            failures.append(f"prestart raw binding is invalid: {key}")

    def one_document(name: str) -> dict[str, Any]:
        value = documents.get(name)
        return value[0] if isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict) else {}

    initial_container = one_document("retained/initial_container_inspect.json")
    final_container = one_document("retained/final_container_inspect.json")
    initial_image = one_document("retained/initial_image_inspect.json")
    final_image = one_document("retained/final_image_inspect.json")
    initial_state = initial_container.get("State") if isinstance(initial_container.get("State"), dict) else {}
    final_state = final_container.get("State") if isinstance(final_container.get("State"), dict) else {}
    if (
        initial_container.get("Id") != container_id
        or initial_container.get("Image") != image_digest
        or initial_state.get("Status") != "created"
        or initial_state.get("Running") is not False
        or initial_container.get("RestartCount") != 0
    ):
        failures.append("raw initial container inspection is invalid")
    expected_final_state = {
        "Status": "exited",
        "Running": False,
        "Paused": False,
        "Restarting": False,
        "OOMKilled": False,
        "Dead": False,
        "ExitCode": 0,
    }
    if (
        final_container.get("Id") != container_id
        or final_container.get("Image") != image_digest
        or final_container.get("RestartCount") != 0
        or any(final_state.get(key) != value for key, value in expected_final_state.items())
    ):
        failures.append("raw final container inspection is not exact exited/zero")
    if initial_image.get("Id") != image_digest or final_image.get("Id") != image_digest:
        failures.append("raw image inspections do not bind the exact image digest")
    if initial_image != final_image:
        failures.append("raw exact image inspection changed during M0 finalization")

    config = final_container.get("Config") if isinstance(final_container.get("Config"), dict) else {}
    host = (
        final_container.get("HostConfig")
        if isinstance(final_container.get("HostConfig"), dict)
        else {}
    )
    environment, environment_failures = _docker_environment(config.get("Env"))
    failures.extend(environment_failures)
    expected_environment = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "ROS_DISTRO": "humble",
        "DEBIAN_FRONTEND": "noninteractive",
        "GZ_VERSION": "harmonic",
        "NVIDIA_VISIBLE_DEVICES": "all",
        "NVIDIA_DRIVER_CAPABILITIES": "compute,utility",
        "SIONNA_MITSUBA_VARIANT": "cuda_ad_mono_polarized",
        "USER": "ubuntu",
        "LOGNAME": "ubuntu",
        "HOME": "/home/ubuntu",
        "AMS_CONTAINER_IMAGE": image_reference,
        "AMS_CONTAINER_IMAGE_DIGEST": image_digest,
        "AMS_CONTAINER_IMAGE_DIGEST_SOURCE": "docker_image_inspect_host",
        "AMS_RUNTIME_CONTAINER_ID_FILE": "/run/ams/container_id",
        "AMS_M0_SOURCE_MODE": "clean_git_clone_ro",
        "AMS_M0_SOURCE_COMMIT": source_commit,
        "AMS_M0_PROJECT_OVERLAY_MODE": "none_q0_source_only",
        "AMS_M0_ARTIFACT_ROOT": "/run/ams/m0-artifacts",
        "AMS_M0_COLLECTION_SECURITY": "cap_drop_all_no_new_privileges",
        "AMS_M0_CAPABILITY_PROBE_MODE": "host_final_isolated_exact_image",
    }
    if (
        environment != expected_environment
    ):
        failures.append("raw retained-container environment is not the exact M0 environment")
    expected_command = [
        "scripts/acceptance_entrypoint.sh",
        "env",
        f"RUN_ID={run_id}",
        "network/scripts/run_m0_baseline.sh",
    ]
    if (
        config.get("Image") != image_digest
        or config.get("User") != "ubuntu"
        or config.get("Entrypoint") != ["/ros_entrypoint.sh"]
        or config.get("Cmd") != expected_command
        or config.get("WorkingDir") != "/workspace/multiagent_simulation"
    ):
        failures.append("raw retained-container Config identity is not exact")
    if (
        host.get("Privileged") is not False
        or host.get("NetworkMode") != "none"
        or host.get("ReadonlyRootfs") is not True
        or host.get("RestartPolicy") != {"Name": "no", "MaximumRetryCount": 0}
        or host.get("Tmpfs")
        != {"/tmp": "rw,nosuid,nodev,exec,size=4g,mode=1777"}
        or host.get("CapAdd") is not None
        or host.get("CapDrop") != ["ALL"]
        or host.get("SecurityOpt")
        not in (
            ["no-new-privileges"],
            ["no-new-privileges:true"],
            ["label=disable", "no-new-privileges"],
            ["label=disable", "no-new-privileges:true"],
        )
        or host.get("Devices") != []
    ):
        failures.append("raw retained-container HostConfig isolation is not exact")

    mounts = final_container.get("Mounts") if isinstance(final_container.get("Mounts"), list) else []
    by_destination = {
        mount.get("Destination"): mount for mount in mounts if isinstance(mount, dict)
    }
    expected_destinations = {
        "/run/ams/container_id",
        "/run/ams/m0-artifacts",
        "/workspace/multiagent_simulation",
        "/workspace/multiagent_simulation/.external/ns-3",
    }
    if len(mounts) != 4 or len(by_destination) != 4 or set(by_destination) != expected_destinations:
        failures.append("raw retained-container mount destination set is not exact")
    artifact_path = (
        artifact_initial.get("path") if isinstance(artifact_initial, dict) else None
    )
    source_path = next(
        (
            value
            for value in (
                mount.get("Source") for mount in mounts if isinstance(mount, dict)
            )
            if isinstance(value, str)
            and re.fullmatch(r"/tmp/ams-m0-source\.[A-Za-z0-9]{10}", value)
        ),
        None,
    )
    identity_path = next(
        (
            value
            for value in (
                mount.get("Source") for mount in mounts if isinstance(mount, dict)
            )
            if isinstance(value, str)
            and re.fullmatch(r"/tmp/ams-container-id\.[A-Za-z0-9]{10}", value)
        ),
        None,
    )
    expected_mounts = {
        "/run/ams/container_id": (identity_path, False),
        "/run/ams/m0-artifacts": (artifact_path, True),
        "/workspace/multiagent_simulation": (source_path, False),
        "/workspace/multiagent_simulation/.external/ns-3": (
            str((root / ".external/ns-3").resolve(strict=False)),
            False,
        ),
    }
    for destination, (source, writable) in expected_mounts.items():
        mount = by_destination.get(destination, {})
        if (
            not source
            or mount.get("Type") != "bind"
            or mount.get("Source") != source
            or mount.get("RW") is not writable
            or mount.get("Mode") != ("rw" if writable else "ro")
            or mount.get("Propagation") != "rprivate"
        ):
            failures.append(f"raw retained-container mount is invalid: {destination}")
    if _container_immutable_fingerprint(initial_container) != _container_immutable_fingerprint(
        final_container
    ):
        failures.append("raw retained-container immutable configuration changed")
    fingerprint_sha256 = _sha256(_canonical_json(_container_immutable_fingerprint(final_container)))
    host_details = receipt.get("gates", {}).get("host_final", {}).get("details", {})
    retained_records = (
        host_details.get("retained_container_initial_final"),
        host_details.get("retained_container_reinspection"),
    )
    raw_hashes = {
        "prestart_record_sha256": _sha256(
            payloads["retained/prestart_inspection_record.json"]
        ),
        "initial_container_inspect_sha256": _sha256(
            payloads["retained/initial_container_inspect.json"]
        ),
        "initial_image_inspect_sha256": _sha256(
            payloads["retained/initial_image_inspect.json"]
        ),
        "final_container_inspect_sha256": _sha256(
            payloads["retained/final_container_inspect.json"]
        ),
        "final_image_inspect_sha256": _sha256(
            payloads["retained/final_image_inspect.json"]
        ),
    }
    expected_mount_sources = sorted(
        str(mount.get("Source")) for mount in mounts if isinstance(mount, dict)
    )
    for retained in retained_records:
        if not isinstance(retained, dict):
            continue
        if (
            any(retained.get(key) != value for key, value in raw_hashes.items())
            or retained.get("immutable_fingerprint_sha256") != fingerprint_sha256
            or retained.get("mount_sources") != expected_mount_sources
            or retained.get("source_snapshot") != source_path
            or retained.get("raw_sha256")
            != {
                name: _sha256(payloads[name])
                for name in RETAINED_CONTROL_FILE_NAMES
            }
        ):
            failures.append("retained-container receipt hashes do not rederive from raw inspect")
    if isinstance(host_details, dict):
        failures.extend(_validate_source_raw(payloads, host_details))
        failures.extend(
            _validate_fresh_raw(
                root,
                payloads,
                host_details.get("fresh_exact_image_reexecution"),
                run_id=run_id,
                image_digest=image_digest,
                source_commit=source_commit,
                execution_contract=execution_contract,
                expected_vector=expected_vector,
                plan_sha256=plan_sha256,
            )
        )
        failures.extend(
            _validate_capability_raw(
                payloads,
                host_details.get("isolated_target_runtime_capability"),
                image_digest=image_digest,
            )
        )
    for name, original in payloads.items():
        try:
            final_payload = _secure_read_relative(
                root,
                f"{expected_path}/{name}",
                maximum_bytes=MAX_CONTROL_BYTES,
                require_read_only=True,
                allow_empty=True,
            )
            if final_payload != original:
                failures.append(f"host-validation raw changed during lint: {name}")
        except ValueError as exc:
            failures.append(str(exc))
    return failures


def _rederive_published_m0(
    root: Path, run_id: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Re-run the committed captured-artifact validator on the published run."""

    command = [
        "/usr/bin/python3.10",
        "-S",
        "network/scripts/validate_m0_baseline.py",
        "--run-dir",
        f"runs/{run_id}",
        "--captured-producer-mode",
    ]
    environment = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C",
        "HOME": "/nonexistent",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": (
            f"{root}:/usr/local/lib/python3.10/dist-packages:"
            "/usr/lib/python3/dist-packages"
        ),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    try:
        result = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            check=False,
            timeout=300,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"published M0 revalidation could not execute: {exc}"
    if result.returncode != 0 or result.stderr:
        diagnostic = result.stderr.decode("utf-8", errors="replace").strip()
        return None, (
            "published M0 captured-artifact revalidation failed"
            + (f": {diagnostic[:500]}" if diagnostic else "")
        )
    try:
        document = _strict_json(result.stdout, "published M0 revalidation")
    except ValueError as exc:
        return None, str(exc)
    expected_top = {
        "schema_version",
        "contract",
        "probe",
        "milestone",
        "run_id",
        "run_dir",
        "scope",
        "p0_eligible",
        "captured_qualified",
        "formal_accepted",
        "passed",
        "consumed_nodes",
        "qualification_content_vector",
        "plan_contract",
        "qualification_contract_sha256",
        "failures",
        "gates",
    }
    gates = document.get("gates") if isinstance(document, dict) else None
    if (
        not isinstance(document, dict)
        or set(document) != expected_top
        or document.get("schema_version") != 3
        or document.get("contract") != "ams.m0.captured-qualification/v1"
        or document.get("probe") != "m0_dependency_provenance"
        or document.get("milestone") != "M0"
        or document.get("run_id") != run_id
        or document.get("captured_qualified") is not True
        or document.get("formal_accepted") is not False
        or document.get("passed") is not False
        or document.get("failures") != ["host-final qualification has not executed"]
        or not isinstance(gates, dict)
        or set(gates) != FORMAL_GATE_NAMES - {"host_final"}
        or any(
            not isinstance(record, dict) or record.get("status") != "passed"
            for record in gates.values()
        )
    ):
        return None, "published M0 revalidation result is not exact/all-pass"
    return gates, None


def _validate_receipt(
    root: Path,
    receipt: dict[str, Any],
    *,
    execution_commit: str,
    expected_vector: dict[str, Any],
    plan_sha256: str,
) -> list[str]:
    failures: list[str] = []
    expected_top = {
        "schema_version",
        "contract",
        "probe",
        "milestone",
        "run_id",
        "run_dir",
        "published_run_dir",
        "receipt_path",
        "scope",
        "p0_eligible",
        "captured_qualified",
        "formal_accepted",
        "passed",
        "consumed_nodes",
        "qualification_content_vector",
        "plan_contract",
        "qualification_contract_sha256",
        "failures",
        "gates",
        "host_validation_raw",
    }
    if set(receipt) != expected_top:
        failures.append("host-final receipt top-level schema is not exact")
        return failures
    run_id = receipt.get("run_id")
    if not isinstance(run_id, str) or SAFE_RUN_ID.fullmatch(run_id) is None:
        failures.append("host-final receipt run ID is unsafe")
        return failures
    canonical_run = f"runs/{run_id}"
    canonical_receipt = f"{canonical_run}/metrics/{M0_RECEIPT_NAME}"
    try:
        execution_contract = _derive_execution_contract(
            root, execution_commit, expected_vector
        )
    except ValueError as exc:
        failures.append(str(exc))
        return failures
    exact_scope = {
        "dependency_check": True,
        "runtime_lock": True,
        "validation_adversarial_suite": True,
        "provenance": True,
        "host_final": True,
        "packet_path": False,
        "sealing": False,
        "attestation": False,
    }
    if (
        receipt.get("schema_version") != 3
        or receipt.get("contract") != RECEIPT_CONTRACT
        or receipt.get("probe") != "m0_dependency_provenance"
        or receipt.get("milestone") != "M0"
        or receipt.get("run_dir") != canonical_run
        or receipt.get("published_run_dir") != canonical_run
        or receipt.get("receipt_path") != canonical_receipt
        or receipt.get("scope") != exact_scope
        or receipt.get("p0_eligible") is not False
        or receipt.get("captured_qualified") is not True
        or receipt.get("formal_accepted") is not True
        or receipt.get("passed") is not True
        or receipt.get("failures") != []
        or receipt.get("consumed_nodes") != ["Q0"]
        or receipt.get("qualification_content_vector") != expected_vector
        or receipt.get("plan_contract")
        != {"plan_version": 3, "path": PLAN_PATH, "contract_sha256": plan_sha256}
    ):
        failures.append("host-final receipt identity/scope/result is not exact")

    gates = receipt.get("gates")
    if not isinstance(gates, dict) or set(gates) != FORMAL_GATE_NAMES:
        failures.append("host-final receipt gate set is not exact")
        return failures
    for name, record in gates.items():
        if (
            not isinstance(record, dict)
            or set(record) != {"status", "proof", "details"}
            or record.get("status") != "passed"
            or not isinstance(record.get("proof"), str)
            or not record.get("proof")
            or not isinstance(record.get("details"), dict)
        ):
            failures.append(f"host-final receipt gate record is not exact/passing: {name}")
    runtime_details = gates.get("runtime_lock", {}).get("details")
    if runtime_details != {"failures": [], "exit_code": 0}:
        failures.append("runtime-lock gate details are not exact/passing")
    dependency_details = gates.get("dependency_check", {}).get("details")
    expected_dependency_keys = {
        "exit_code",
        "raw_log_sha256",
        "observed_record_count",
        "observed_records",
        "warning_count",
    }
    if not isinstance(dependency_details, dict) or set(dependency_details) != expected_dependency_keys:
        failures.append("dependency gate detail schema is not exact")
    else:
        observed = dependency_details.get("observed_records")
        labels = (
            [record.get("label") for record in observed]
            if isinstance(observed, list)
            and all(
                isinstance(record, dict) and set(record) == {"label", "status"}
                for record in observed
            )
            else []
        )
        statuses = (
            [record.get("status") for record in observed]
            if isinstance(observed, list)
            and all(
                isinstance(record, dict) and set(record) == {"label", "status"}
                for record in observed
            )
            else []
        )
        warnings = sum(status == "WARN" for status in statuses)
        if (
            dependency_details.get("exit_code") != 0
            or SHA256.fullmatch(str(dependency_details.get("raw_log_sha256") or ""))
            is None
            or dependency_details.get("observed_record_count")
            != EXPECTED_DEPENDENCY_RECORD_COUNT
            or len(labels) != EXPECTED_DEPENDENCY_RECORD_COUNT
            or _sha256(_canonical_json(labels)) != EXPECTED_DEPENDENCY_LABELS_SHA256
            or any(status not in {"PASS", "WARN"} for status in statuses)
            or any(
                status == "WARN" and label != "cuda:gpu"
                for label, status in zip(labels, statuses)
            )
            or dependency_details.get("warning_count") != warnings
        ):
            failures.append("dependency gate raw-derived identities/results are invalid")
    suite_details = gates.get("validation_adversarial_suite", {}).get("details")
    if (
        not isinstance(suite_details, dict)
        or set(suite_details)
        != {
            "failures",
            "expected_test_count",
            "raw_log_sha256",
            "raw_passing_test_count",
            "required_coverage",
        }
        or suite_details.get("failures") != []
        or suite_details.get("expected_test_count")
        != execution_contract["frozen_test_count"]
        or suite_details.get("raw_passing_test_count")
        != execution_contract["frozen_test_count"]
        or SHA256.fullmatch(str(suite_details.get("raw_log_sha256") or "")) is None
        or not isinstance(suite_details.get("required_coverage"), dict)
        or not suite_details.get("required_coverage")
    ):
        failures.append("validation/adversarial-suite gate details are invalid")
    provenance_details = gates.get("provenance", {}).get("details")
    provenance_status = (
        provenance_details.get("provenance_status")
        if isinstance(provenance_details, dict)
        else None
    )
    if (
        not isinstance(provenance_details, dict)
        or set(provenance_details) != {"provenance_status"}
        or not isinstance(provenance_status, dict)
        or set(provenance_status) != {"status", "proof"}
        or provenance_status.get("status") != "passed"
        or not isinstance(provenance_status.get("proof"), str)
        or not provenance_status.get("proof")
    ):
        failures.append("provenance gate details are not independently passing")
    published_gates, published_error = _rederive_published_m0(root, str(run_id))
    if published_error:
        failures.append(published_error)
    elif published_gates != {
        name: gates.get(name) for name in FORMAL_GATE_NAMES if name != "host_final"
    }:
        failures.append(
            "published M0 captured gates differ from independent artifact revalidation"
        )
    host = gates.get("host_final", {})
    details = host.get("details") if isinstance(host, dict) else {}
    expected_detail_keys = {
        "failures",
        "expected_container_id",
        "image_digest",
        "source_before",
        "source_after",
        "artifact_before",
        "artifact_after",
        "artifact_content_manifest",
        "rederived_captured_gates",
        "external_before",
        "external_after",
        "producer_source_identity",
        "fresh_source_before",
        "fresh_source_after",
        "retained_container_initial_final",
        "retained_container_reinspection",
        "fresh_exact_image_reexecution",
        "isolated_target_runtime_capability",
        "host_validation_content_manifest",
        "host_execution_identity",
    }
    if not isinstance(details, dict) or set(details) != expected_detail_keys:
        failures.append("host-final gate detail schema is not exact")
        return failures
    container_id = details.get("expected_container_id")
    image_digest = details.get("image_digest")
    if (
        details.get("failures") != []
        or not isinstance(container_id, str)
        or CONTAINER_ID.fullmatch(container_id) is None
        or not isinstance(image_digest, str)
        or IMAGE_DIGEST.fullmatch(image_digest) is None
        or image_digest != execution_contract["image_digest"]
    ):
        failures.append("host-final gate did not retain exact passing runtime identities")
    failures.extend(
        _validate_host_execution_identity(
            details.get("host_execution_identity"),
            execution_contract=execution_contract,
            expected_vector=expected_vector,
        )
    )
    failures.extend(
        _validate_source_snapshot(
            details.get("source_before"),
            execution_commit=execution_commit,
            expected_vector=expected_vector,
            plan_sha256=plan_sha256,
            execution_contract=execution_contract,
        )
    )
    failures.extend(
        _validate_source_snapshot(
            details.get("source_after"),
            execution_commit=execution_commit,
            expected_vector=expected_vector,
            plan_sha256=plan_sha256,
            execution_contract=execution_contract,
        )
    )
    if details.get("source_before") != details.get("source_after"):
        failures.append("host source identity changed during host-final")
    for label in (
        "producer_source_identity",
        "fresh_source_before",
        "fresh_source_after",
    ):
        failures.extend(
            _validate_source_snapshot(
                details.get(label),
                execution_commit=execution_commit,
                expected_vector=expected_vector,
                plan_sha256=plan_sha256,
                execution_contract=execution_contract,
            )
        )
    source_identities = [
        details.get(label)
        for label in (
            "source_before",
            "source_after",
            "producer_source_identity",
            "fresh_source_before",
            "fresh_source_after",
        )
    ]
    if any(value != source_identities[0] for value in source_identities[1:]):
        failures.append("technical, producer and fresh source identities differ")
    if (
        not isinstance(details.get("external_before"), dict)
        or details.get("external_before") != details.get("external_after")
    ):
        failures.append("external source identities changed during host-final")
    rederived = details.get("rederived_captured_gates")
    expected_rederived = {
        name: gates.get(name)
        for name in FORMAL_GATE_NAMES
        if name != "host_final"
    }
    if rederived != expected_rederived:
        failures.append("host-final rederived gate records differ from the receipt gates")
    failures.extend(_validate_snapshot(details.get("artifact_before"), "before"))
    failures.extend(_validate_snapshot(details.get("artifact_after"), "after"))
    if details.get("artifact_before") != details.get("artifact_after"):
        failures.append("captured artifact identity changed during host-final")
    if isinstance(details.get("artifact_after"), dict):
        try:
            expected_artifact_content = _portable_manifest_from_snapshot(
                details["artifact_after"]
            )
            if details.get("artifact_content_manifest") != expected_artifact_content:
                failures.append("portable artifact content manifest is invalid")
        except ValueError as exc:
            failures.append(str(exc))
        failures.extend(
            _validate_published_artifacts(root, run_id, details["artifact_after"])
        )

    retained_keys = {
        "container_id",
        "image_digest",
        "source_snapshot",
        "prestart_record_sha256",
        "initial_container_inspect_sha256",
        "initial_image_inspect_sha256",
        "final_container_inspect_sha256",
        "final_image_inspect_sha256",
        "immutable_fingerprint_sha256",
        "mount_sources",
        "raw_sha256",
    }
    retained_values: list[dict[str, Any]] = []
    for label in ("retained_container_initial_final", "retained_container_reinspection"):
        retained = details.get(label)
        if not isinstance(retained, dict) or set(retained) != retained_keys:
            failures.append(f"{label} schema is not exact")
            continue
        retained_values.append(retained)
        mount_sources = retained.get("mount_sources")
        source_snapshot = retained.get("source_snapshot")
        expected_external = str((root / ".external/ns-3").resolve(strict=False))
        mount_shape_valid = (
            isinstance(mount_sources, list)
            and len(mount_sources) == 4
            and mount_sources == sorted(mount_sources)
            and source_snapshot in mount_sources
            and expected_external in mount_sources
            and sum(
                isinstance(value, str)
                and re.fullmatch(r"/tmp/ams-container-id\.[A-Za-z0-9]{10}", value)
                is not None
                for value in mount_sources
            )
            == 1
            and sum(
                isinstance(value, str)
                and Path(value).parent == root.parent
                and re.fullmatch(
                    rf"\.ams-m0-artifacts-{re.escape(str(run_id))}\.[A-Za-z0-9]{{10}}",
                    Path(value).name,
                )
                is not None
                for value in mount_sources
            )
            == 1
        )
        if (
            retained.get("container_id") != container_id
            or retained.get("image_digest") != image_digest
            or not isinstance(source_snapshot, str)
            or re.fullmatch(r"/tmp/ams-m0-source\.[A-Za-z0-9]{10}", source_snapshot)
            is None
            or not mount_shape_valid
            or any(
                SHA256.fullmatch(str(retained.get(key) or "")) is None
                for key in (
                    "prestart_record_sha256",
                    "initial_container_inspect_sha256",
                    "initial_image_inspect_sha256",
                    "final_container_inspect_sha256",
                    "final_image_inspect_sha256",
                    "immutable_fingerprint_sha256",
                )
            )
            or not isinstance(retained.get("raw_sha256"), dict)
        ):
            failures.append(f"{label} identities are invalid")
    if len(retained_values) == 2 and retained_values[0] != retained_values[1]:
        failures.append("retained container changed across host-final inspection")

    fresh = details.get("fresh_exact_image_reexecution")
    fresh_keys = {
        "container_id",
        "image_digest",
        "exit_code",
        "dependency_record_count",
        "dependency_warning_count",
        "dependency_stdout_sha256",
        "runtime_lock_sha256",
        "passing_test_count",
        "unittest_stderr_sha256",
        "python_import_trace",
        "python_import_trace_sha256",
        "python_guard",
        "artifact_snapshot_before",
        "artifact_snapshot_after",
        "prestart_container_inspect_sha256",
        "final_container_inspect_sha256",
        "container_stdout_sha256",
        "container_stderr_sha256",
        "raw_sha256",
    }
    if not isinstance(fresh, dict) or set(fresh) != fresh_keys:
        failures.append("fresh exact-image re-execution schema is not exact")
    else:
        guard = fresh.get("python_guard")
        expected_guard = {
            "guard_marker": True,
            "no_site": 0,
            "sitecustomize_path": (
                "/workspace/multiagent_simulation/network/scripts/"
                "m0_python_guard/sitecustomize.py"
            ),
            "usercustomize_loaded": False,
        }
        if (
            CONTAINER_ID.fullmatch(str(fresh.get("container_id") or "")) is None
            or fresh.get("image_digest") != image_digest
            or fresh.get("exit_code") != 0
            or isinstance(fresh.get("dependency_record_count"), bool)
            or not isinstance(fresh.get("dependency_record_count"), int)
            or fresh.get("dependency_record_count") != EXPECTED_DEPENDENCY_RECORD_COUNT
            or isinstance(fresh.get("dependency_warning_count"), bool)
            or not isinstance(fresh.get("dependency_warning_count"), int)
            or fresh.get("dependency_warning_count", -1) < 0
            or fresh.get("dependency_warning_count")
            != (
                dependency_details.get("warning_count")
                if isinstance(dependency_details, dict)
                else None
            )
            or isinstance(fresh.get("passing_test_count"), bool)
            or not isinstance(fresh.get("passing_test_count"), int)
            or fresh.get("passing_test_count") != execution_contract["frozen_test_count"]
            or fresh.get("container_id") == container_id
            or any(
                SHA256.fullmatch(str(fresh.get(key) or "")) is None
                for key in (
                    "dependency_stdout_sha256",
                    "runtime_lock_sha256",
                    "unittest_stderr_sha256",
                    "prestart_container_inspect_sha256",
                    "final_container_inspect_sha256",
                    "container_stdout_sha256",
                    "container_stderr_sha256",
                    "python_import_trace_sha256",
                )
            )
            or guard != expected_guard
            or not isinstance(fresh.get("raw_sha256"), dict)
            or fresh.get("python_import_trace_sha256")
            != _sha256(_canonical_json(fresh.get("python_import_trace")))
        ):
            failures.append("fresh exact-image re-execution identities/results are invalid")
        failures.extend(
            _validate_snapshot(fresh.get("artifact_snapshot_before"), "fresh-before")
        )
        failures.extend(
            _validate_snapshot(fresh.get("artifact_snapshot_after"), "fresh-after")
        )
        if fresh.get("artifact_snapshot_before") != fresh.get("artifact_snapshot_after"):
            failures.append("fresh exact-image output tree changed during host-final")

    capability = details.get("isolated_target_runtime_capability")
    capability_keys = {
        "contract",
        "container_id",
        "image_digest",
        "exit_code",
        "no_candidate_mounts",
        "tun_device",
        "passwordless_sudo",
        "unshare_network_namespace",
        "raw_sha256",
    }
    if (
        not isinstance(capability, dict)
        or set(capability) != capability_keys
        or capability.get("contract") != "ams.m0.isolated-capability-probe/v1"
        or CONTAINER_ID.fullmatch(str(capability.get("container_id") or "")) is None
        or capability.get("container_id") in {container_id, fresh.get("container_id")}
        or capability.get("image_digest") != image_digest
        or capability.get("exit_code") != 0
        or any(
            capability.get(name) is not True
            for name in (
                "no_candidate_mounts",
                "tun_device",
                "passwordless_sudo",
                "unshare_network_namespace",
            )
        )
        or not isinstance(capability.get("raw_sha256"), dict)
    ):
        failures.append("isolated capability proof is not exact/passing")

    host_manifest = details.get("host_validation_content_manifest")
    host_raw = receipt.get("host_validation_raw")
    if (
        not isinstance(host_manifest, dict)
        or set(host_manifest)
        != {"schema_version", "contract", "files", "file_count", "content_sha256"}
        or host_manifest.get("schema_version") != 1
        or not isinstance(host_raw, dict)
        or host_manifest.get("contract") != host_raw.get("contract")
        or host_manifest.get("files") != host_raw.get("files")
        or host_manifest.get("file_count") != host_raw.get("file_count")
        or host_manifest.get("content_sha256") != host_raw.get("content_sha256")
    ):
        failures.append("host-validation content manifest does not bind the receipt")

    source_after = details.get("source_after") if isinstance(details.get("source_after"), dict) else {}
    contract_payload = {
        "run_id": run_id,
        "source": source_after,
        "image_digest": image_digest,
        "artifact_content_sha256": (
            details.get("artifact_content_manifest", {}).get("content_sha256")
            if isinstance(details.get("artifact_content_manifest"), dict)
            else None
        ),
        "host_validation_content_sha256": (
            details.get("host_validation_content_manifest", {}).get("content_sha256")
            if isinstance(details.get("host_validation_content_manifest"), dict)
            else None
        ),
        "isolated_capability_contract": (
            capability.get("contract") if isinstance(capability, dict) else None
        ),
        "host_execution_identity_sha256": _sha256(
            _canonical_json(details.get("host_execution_identity"))
        ),
        "consumed_nodes": ["Q0"],
    }
    expected_contract_sha = _sha256(_canonical_json(contract_payload))
    if receipt.get("qualification_contract_sha256") != expected_contract_sha:
        failures.append("host-final qualification contract hash does not rederive")
    if isinstance(container_id, str) and isinstance(image_digest, str):
        failures.extend(
            _validate_control_raw(
                root,
                receipt,
                run_id=run_id,
                container_id=container_id,
                image_digest=image_digest,
                image_reference=execution_contract["image_reference"],
                source_commit=execution_commit,
                execution_contract=execution_contract,
                expected_vector=expected_vector,
                plan_sha256=plan_sha256,
            )
        )
    return failures


def _canonical_pretty_json(document: Any) -> bytes:
    return (
        json.dumps(document, allow_nan=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _m1_portable_manifest(
    root: Path,
    run_id: str,
    *,
    receipt_name: str = M1_RECEIPT_NAME,
    extra_excluded: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Recompute the finalizer's portable M1 tree from immutable published bytes."""

    run_relative = f"runs/{run_id}"
    run_root = root / run_relative
    root_info = run_root.lstat()
    if (
        not stat.S_ISDIR(root_info.st_mode)
        or stat.S_ISLNK(root_info.st_mode)
        or stat.S_IMODE(root_info.st_mode) & 0o222
    ):
        raise ValueError("published M1 run root is not one immutable real directory")
    canonical_root = run_root.resolve(strict=True)
    entries: dict[str, dict[str, Any]] = {}

    def walk(directory: Path, prefix: str) -> None:
        before = directory.lstat()
        if (
            not stat.S_ISDIR(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or stat.S_IMODE(before.st_mode) & 0o222
        ):
            raise ValueError(f"published M1 directory is not immutable: {prefix or '.'}")
        names = sorted(os.listdir(directory))
        if len(names) != len(set(names)):
            raise ValueError("published M1 tree has duplicate names")
        for name in names:
            if not name or name in {".", ".."} or "/" in name or "\x00" in name:
                raise ValueError("published M1 tree has an unsafe name")
            relative = f"{prefix}/{name}" if prefix else name
            if (
                relative == "host_validation"
                or relative == f"metrics/{receipt_name}"
                or relative in extra_excluded
            ):
                continue
            path = directory / name
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                if stat.S_IMODE(info.st_mode) & 0o222:
                    raise ValueError(f"published M1 directory is writable: {relative}")
                entries[relative] = {"kind": "directory"}
                walk(path, relative)
            elif stat.S_ISREG(info.st_mode):
                if info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o222:
                    raise ValueError(f"published M1 file is mutable/hardlinked: {relative}")
                payload = _secure_read_relative(
                    root,
                    f"{run_relative}/{relative}",
                    maximum_bytes=MAX_M1_ARTIFACT_BYTES,
                    require_read_only=True,
                    allow_empty=True,
                )
                entries[relative] = {
                    "kind": "file",
                    "bytes": len(payload),
                    "sha256": _sha256(payload),
                }
            elif stat.S_ISLNK(info.st_mode):
                target = os.readlink(path)
                resolved = path.resolve(strict=True)
                try:
                    resolved.relative_to(canonical_root)
                except ValueError as exc:
                    raise ValueError(
                        f"published M1 symlink escapes the run: {relative}"
                    ) from exc
                after = path.lstat()
                if (
                    info.st_dev,
                    info.st_ino,
                    info.st_mode,
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                ) != (
                    after.st_dev,
                    after.st_ino,
                    after.st_mode,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ):
                    raise ValueError(f"published M1 symlink changed: {relative}")
                entries[relative] = {
                    "kind": "symlink",
                    "target": target,
                    "target_sha256": _sha256(target.encode("utf-8")),
                }
            else:
                raise ValueError(f"published M1 tree has a special entry: {relative}")
        after = directory.lstat()
        stable = ("st_dev", "st_ino", "st_mode", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, field) != getattr(after, field) for field in stable):
            raise ValueError(f"published M1 directory changed: {prefix or '.'}")

    walk(run_root, "")
    return {
        "schema_version": 1,
        "contract": "ams.m1.portable-content-manifest/v1",
        "entries": entries,
        "entry_count": len(entries),
        "content_sha256": _sha256(_canonical_json(entries)),
    }


def _m1_host_validation_payloads(
    root: Path, run_id: str
) -> tuple[dict[str, bytes], dict[str, Any], list[str]]:
    failures: list[str] = []
    relative_root = f"runs/{run_id}/host_validation"
    try:
        names = _secure_tree_files(root, relative_root)
    except ValueError as exc:
        return {}, {}, [str(exc)]
    expected_names = sorted(M1_HOST_RAW_FILES | {"content_manifest.json"})
    if names != expected_names:
        failures.append("M1 host-validation raw path set is not exact")
    payloads: dict[str, bytes] = {}
    for relative in names:
        try:
            payloads[relative] = _secure_read_relative(
                root,
                f"{relative_root}/{relative}",
                maximum_bytes=MAX_M1_ARTIFACT_BYTES,
                require_read_only=True,
                allow_empty=relative == "validation/stderr.txt",
            )
        except ValueError as exc:
            failures.append(str(exc))
    manifest_payload = payloads.get("content_manifest.json")
    if manifest_payload is None:
        return payloads, {}, failures + ["M1 host-validation manifest is missing"]
    try:
        manifest = _strict_json(manifest_payload, "M1 host-validation manifest")
    except ValueError as exc:
        return payloads, {}, failures + [str(exc)]
    if not isinstance(manifest, dict) or manifest_payload != _canonical_pretty_json(manifest):
        failures.append("M1 host-validation manifest bytes are not canonical")
        manifest = manifest if isinstance(manifest, dict) else {}
    raw_records: dict[str, dict[str, Any]] = {}
    for relative in sorted(M1_HOST_RAW_FILES):
        payload = payloads.get(relative)
        if payload is not None:
            raw_records[relative] = {"bytes": len(payload), "sha256": _sha256(payload)}
    expected_manifest = {
        "schema_version": 1,
        "contract": "ams.m1.host-validation-content/v1",
        "files": raw_records,
        "file_count": len(raw_records),
        "content_sha256": _sha256(_canonical_json(raw_records)),
    }
    if manifest != expected_manifest or set(raw_records) != M1_HOST_RAW_FILES:
        failures.append("M1 host-validation manifest does not rederive from raw files")
    return payloads, manifest, failures


def _m1_one_inspect(
    payloads: dict[str, bytes], relative: str, label: str
) -> tuple[dict[str, Any], list[str]]:
    document, failures = _one_raw_json(payloads, relative, label)
    if (
        not isinstance(document, list)
        or len(document) != 1
        or not isinstance(document[0], dict)
    ):
        failures.append(f"{label} is not exactly one Docker inspection")
        return {}, failures
    return document[0], failures


def _m1_mount_map(
    document: dict[str, Any], label: str
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    failures: list[str] = []
    mounts = document.get("Mounts")
    if not isinstance(mounts, list) or not all(isinstance(item, dict) for item in mounts):
        return {}, [f"{label} Mounts is not a mapping list"]
    result: dict[str, dict[str, Any]] = {}
    for item in mounts:
        destination = item.get("Destination")
        if not isinstance(destination, str) or destination in result:
            failures.append(f"{label} has an invalid/duplicate mount destination")
            continue
        result[destination] = item
    return result, failures


def _m1_security_options(value: Any) -> bool:
    return value in (
        ["no-new-privileges"],
        ["no-new-privileges:true"],
        ["label=disable", "no-new-privileges"],
        ["label=disable", "no-new-privileges:true"],
    )


def _validate_m1_docker_raw(
    root: Path,
    run_id: str,
    receipt: dict[str, Any],
    payloads: dict[str, bytes],
) -> list[str]:
    failures: list[str] = []
    parsed: dict[str, dict[str, Any]] = {}
    for relative, label in (
        ("main/initial_container_inspect.json", "M1 initial container"),
        ("main/final_container_inspect.json", "M1 final container"),
        ("main/initial_image_inspect.json", "M1 initial image"),
        ("main/final_image_inspect.json", "M1 final image"),
        ("validation/initial_container_inspect.json", "M1 validation initial container"),
        ("validation/final_container_inspect.json", "M1 validation final container"),
        ("validation/image_inspect.json", "M1 validation image"),
    ):
        parsed[relative], local = _m1_one_inspect(payloads, relative, label)
        failures.extend(local)
    if failures:
        return failures

    image_digest = receipt.get("image_digest")
    runtime_id = receipt.get("runtime_container_id")
    validation_id = receipt.get("validation_container_id")
    source_commit = receipt.get("source_commit")
    image_reference = receipt.get("image_reference")
    m0_authority = (
        receipt.get("m0_status_authority")
        if isinstance(receipt.get("m0_status_authority"), dict)
        else {}
    )
    m0_receipt_path = m0_authority.get("receipt_path")
    main_initial = parsed["main/initial_container_inspect.json"]
    main_final = parsed["main/final_container_inspect.json"]
    validation_initial = parsed["validation/initial_container_inspect.json"]
    validation_final = parsed["validation/final_container_inspect.json"]
    main_initial_state = main_initial.get("State", {})
    main_final_state = main_final.get("State", {})
    validation_initial_state = validation_initial.get("State", {})
    validation_final_state = validation_final.get("State", {})
    if (
        main_initial.get("Id") != runtime_id
        or main_final.get("Id") != runtime_id
        or main_initial.get("Image") != image_digest
        or main_final.get("Image") != image_digest
        or main_initial_state.get("Status") != "created"
        or main_initial_state.get("Running") is not False
        or main_initial.get("RestartCount") != 0
        or main_final_state.get("Status") != "exited"
        or main_final_state.get("Running") is not False
        or main_final_state.get("OOMKilled") is not False
        or main_final_state.get("ExitCode") != 0
        or main_final.get("RestartCount") != 0
    ):
        failures.append("M1 main-container raw lifecycle is not exact")
    if _container_immutable_fingerprint(main_initial) != _container_immutable_fingerprint(
        main_final
    ):
        failures.append("M1 main-container immutable configuration changed")

    main_config = main_final.get("Config") if isinstance(main_final.get("Config"), dict) else {}
    main_host = (
        main_final.get("HostConfig")
        if isinstance(main_final.get("HostConfig"), dict)
        else {}
    )
    main_environment, local = _docker_environment(main_config.get("Env"))
    failures.extend(local)
    required_environment = {
        "AMS_CONTAINER_IMAGE": image_reference,
        "AMS_CONTAINER_IMAGE_DIGEST": image_digest,
        "AMS_CONTAINER_IMAGE_DIGEST_SOURCE": "docker_image_inspect_host",
        "AMS_RUNTIME_CONTAINER_ID_FILE": "/run/ams/container_id",
        "AMS_M1_SOURCE_MODE": "clean_git_clone_ro",
        "AMS_M1_SOURCE_COMMIT": source_commit,
        "AMS_M1_PROJECT_OVERLAY_MODE": "fresh_run_overlay",
        "AMS_M1_RUN_ID": run_id,
        "AMS_M0_CAPABILITY_PROBE_MODE": "inherited_m0_host_final",
        "AMS_M1_M0_RECEIPT_PATH": "/run/ams/m0-receipt.json",
        "AMS_M1_M0_RECEIPT_CANONICAL_PATH": m0_receipt_path,
        "AMS_M1_M0_RECEIPT_SHA256": m0_authority.get("receipt_sha256"),
        "AMS_M1_M0_STATUS_COMMIT": source_commit,
        "GZ_VERSION": "harmonic",
    }
    if any(main_environment.get(key) != value for key, value in required_environment.items()):
        failures.append("M1 main-container environment is not exact")
    expected_main_command = [
        "scripts/acceptance_entrypoint.sh",
        "timeout",
        "--signal=TERM",
        "--kill-after=20s",
        "600s",
        "env",
        f"RUN_ID={run_id}",
        "network/scripts/run_five_uav_health.sh",
    ]
    if (
        main_config.get("Image") != image_digest
        or main_config.get("User") != "ubuntu"
        or main_config.get("Entrypoint") != ["/ros_entrypoint.sh"]
        or main_config.get("Cmd") != expected_main_command
        or main_config.get("WorkingDir") != "/workspace/multiagent_simulation"
        or main_host.get("Privileged") is not False
        or main_host.get("NetworkMode") != "host"
        or main_host.get("ReadonlyRootfs") is not True
        or main_host.get("RestartPolicy") != {"Name": "no", "MaximumRetryCount": 0}
        or main_host.get("CapAdd") is not None
        or main_host.get("CapDrop") != ["ALL"]
        or not _m1_security_options(main_host.get("SecurityOpt"))
        or main_host.get("Tmpfs")
        != {"/tmp": "rw,nosuid,nodev,exec,size=4g,mode=1777"}
        or main_host.get("Devices") not in (None, [])
        or main_host.get("DeviceRequests") != EXPECTED_GPU_DEVICE_REQUESTS
    ):
        failures.append("M1 main-container Config/HostConfig is not exact")

    main_mounts, local = _m1_mount_map(main_final, "M1 main container")
    failures.extend(local)
    expected_main_destinations = {
        "/run/ams/container_id",
        "/run/ams/m0-receipt.json",
        "/workspace/multiagent_simulation",
        "/workspace/multiagent_simulation/runs",
        "/workspace/multiagent_simulation/.external/ns-3",
    }
    if set(main_mounts) != expected_main_destinations:
        failures.append("M1 main-container mount destination set is not exact")
    source_pattern = re.compile(r"/tmp/ams-m1-source\.[A-Za-z0-9]+")
    staging_pattern = re.compile(
        re.escape(str(root / "runs"))
        + rf"/\.m1-stage-{re.escape(run_id)}\.[A-Za-z0-9]+"
    )
    identity_pattern = re.compile(r"/tmp/ams-container-id\.[A-Za-z0-9]+")
    expected_mounts = {
        "/run/ams/container_id": (identity_pattern, False, "ro"),
        "/workspace/multiagent_simulation": (source_pattern, False, "ro"),
        "/workspace/multiagent_simulation/runs": (staging_pattern, True, "rw"),
        "/workspace/multiagent_simulation/.external/ns-3": (
            re.compile(re.escape(str((root / ".external/ns-3").resolve()))),
            False,
            "ro",
        ),
        "/run/ams/m0-receipt.json": (
            re.compile(
                re.escape(str(root / str(m0_receipt_path)))
                if isinstance(m0_receipt_path, str)
                else r"(?!)"
            ),
            False,
            "ro",
        ),
    }
    for destination, (source_regex, writable, mode) in expected_mounts.items():
        item = main_mounts.get(destination, {})
        if (
            item.get("Type") != "bind"
            or source_regex.fullmatch(str(item.get("Source") or "")) is None
            or item.get("RW") is not writable
            or item.get("Mode") != mode
            or item.get("Propagation") != "rprivate"
        ):
            failures.append(f"M1 main-container mount is invalid: {destination}")

    if (
        validation_initial.get("Id") != validation_id
        or validation_final.get("Id") != validation_id
        or validation_initial.get("Image") != image_digest
        or validation_final.get("Image") != image_digest
        or validation_initial_state.get("Status") != "created"
        or validation_initial_state.get("Running") is not False
        or validation_initial.get("RestartCount") != 0
        or validation_final_state.get("Status") != "exited"
        or validation_final_state.get("Running") is not False
        or validation_final_state.get("OOMKilled") is not False
        or validation_final_state.get("ExitCode") != 0
        or validation_final.get("RestartCount") != 0
    ):
        failures.append("M1 validation-container raw lifecycle is not exact")
    if _container_immutable_fingerprint(
        validation_initial
    ) != _container_immutable_fingerprint(validation_final):
        failures.append("M1 validation-container immutable configuration changed")
    validation_config = (
        validation_final.get("Config")
        if isinstance(validation_final.get("Config"), dict)
        else {}
    )
    validation_host = (
        validation_final.get("HostConfig")
        if isinstance(validation_final.get("HostConfig"), dict)
        else {}
    )
    expected_validation_command = [
        "/usr/bin/python3.10",
        "network/scripts/validate_m1_health.py",
        "--run-dir",
        f"runs/{run_id}",
        "--no-write",
    ]
    if (
        validation_config.get("Image") != image_digest
        or validation_config.get("User") != "ubuntu"
        or validation_config.get("Entrypoint") != ["/ros_entrypoint.sh"]
        or validation_config.get("Cmd") != expected_validation_command
        or validation_config.get("WorkingDir") != "/workspace/multiagent_simulation"
        or validation_host.get("Privileged") is not False
        or validation_host.get("NetworkMode") != "none"
        or validation_host.get("ReadonlyRootfs") is not True
        or validation_host.get("RestartPolicy")
        != {"Name": "no", "MaximumRetryCount": 0}
        or validation_host.get("CapAdd") is not None
        or validation_host.get("CapDrop") != ["ALL"]
        or not _m1_security_options(validation_host.get("SecurityOpt"))
        or validation_host.get("Tmpfs")
        != {"/tmp": "rw,nosuid,nodev,exec,size=1g,mode=1777"}
        or validation_host.get("Devices") not in (None, [])
    ):
        failures.append("M1 validation-container Config/HostConfig is not exact")
    validation_mounts, local = _m1_mount_map(
        validation_final, "M1 validation container"
    )
    failures.extend(local)
    expected_validation_destinations = {
        "/workspace/multiagent_simulation",
        "/workspace/multiagent_simulation/runs",
        "/workspace/multiagent_simulation/.external/ns-3",
        "/run/ams/m0-receipt.json",
    }
    if set(validation_mounts) != expected_validation_destinations:
        failures.append("M1 validation-container mount destination set is not exact")
    for destination, (source_regex, _writable, _mode) in {
        key: value
        for key, value in expected_mounts.items()
        if key != "/run/ams/container_id"
    }.items():
        item = validation_mounts.get(destination, {})
        if (
            item.get("Type") != "bind"
            or source_regex.fullmatch(str(item.get("Source") or "")) is None
            or item.get("RW") is not False
            or item.get("Mode") != "ro"
            or item.get("Propagation") != "rprivate"
        ):
            failures.append(f"M1 validation-container mount is invalid: {destination}")

    images = [
        parsed["main/initial_image_inspect.json"],
        parsed["main/final_image_inspect.json"],
        parsed["validation/image_inspect.json"],
    ]
    if any(image.get("Id") != image_digest for image in images) or any(
        image != images[0] for image in images[1:]
    ):
        failures.append("M1 image inspections are not exact and identical")
    return failures


def _rederive_published_m1(
    root: Path, run_id: str, image_digest: str, m0_receipt_path: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Re-evaluate immutable M1 evidence inside the exact qualified image."""

    command = [
        "/usr/bin/docker",
        "run",
        "--rm",
        "--network=none",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,exec,size=1g,mode=1777",
        "-e",
        "GZ_VERSION=harmonic",
        "-v",
        f"{root}:/workspace/multiagent_simulation:ro",
        "-v",
        (
            f"{root / m0_receipt_path}:/run/ams/m0-receipt.json:ro"
        ),
        "-w",
        "/workspace/multiagent_simulation",
        image_digest,
        "/usr/bin/python3.10",
        "network/scripts/validate_m1_health.py",
        "--run-dir",
        f"runs/{run_id}",
        "--no-write",
    ]
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "DOCKER_CONFIG": "/nonexistent",
    }
    try:
        result = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            check=False,
            timeout=600,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"published M1 exact-image revalidation could not execute: {exc}"
    if result.returncode != 0 or result.stderr:
        diagnostic = result.stderr.decode("utf-8", errors="replace").strip()
        return None, (
            "published M1 exact-image revalidation failed"
            + (f": {diagnostic[:500]}" if diagnostic else "")
        )
    try:
        document = _strict_json(result.stdout, "published M1 exact-image revalidation")
    except ValueError as exc:
        return None, str(exc)
    if not isinstance(document, dict):
        return None, "published M1 exact-image revalidation is not a JSON object"
    return document, None


def _validate_m1_receipt(
    root: Path,
    receipt: dict[str, Any],
    *,
    execution_commit: str,
    expected_vector: dict[str, Any],
    plan_sha256: str,
) -> list[str]:
    failures: list[str] = []
    if set(receipt) != M1_RECEIPT_TOP_LEVEL_KEYS:
        return ["M1 host-final receipt top-level schema is not exact"]
    run_id = receipt.get("run_id")
    if not isinstance(run_id, str) or SAFE_RUN_ID.fullmatch(run_id) is None:
        return ["M1 host-final receipt run ID is unsafe"]
    try:
        execution_contract = _derive_execution_contract(
            root, execution_commit, expected_vector
        )
    except ValueError as exc:
        return [str(exc)]
    image_digest = receipt.get("image_digest")
    runtime_id = receipt.get("runtime_container_id")
    validation_id = receipt.get("validation_container_id")
    if (
        receipt.get("schema_version") != 1
        or receipt.get("contract") != M1_RECEIPT_CONTRACT
        or receipt.get("milestone") != "M1"
        or receipt.get("run_dir") != f"runs/{run_id}"
        or receipt.get("receipt_path")
        != f"runs/{run_id}/metrics/m1_host_final_receipt.json"
        or receipt.get("source_commit") != execution_commit
        or receipt.get("image_reference") != execution_contract["image_reference"]
        or image_digest != execution_contract["image_digest"]
        or IMAGE_DIGEST.fullmatch(str(image_digest or "")) is None
        or CONTAINER_ID.fullmatch(str(runtime_id or "")) is None
        or CONTAINER_ID.fullmatch(str(validation_id or "")) is None
        or runtime_id == validation_id
        or receipt.get("consumed_nodes") != ["Q0", "Q1"]
        or receipt.get("qualification_content_vector") != expected_vector
        or receipt.get("formal_accepted") is not True
        or receipt.get("passed") is not True
        or receipt.get("failures") != []
    ):
        failures.append("M1 host-final receipt authority/identity is invalid")

    component = receipt.get("component_result")
    expected_component_path = f"runs/{run_id}/metrics/m1_result.json"
    if (
        not isinstance(component, dict)
        or set(component) != {"path", "bytes", "sha256"}
        or component.get("path") != expected_component_path
        or isinstance(component.get("bytes"), bool)
        or not isinstance(component.get("bytes"), int)
        or component.get("bytes", -1) < 1
        or SHA256.fullmatch(str(component.get("sha256") or "")) is None
    ):
        failures.append("M1 component-result receipt binding is invalid")
        component = {}
    try:
        result_payload = _secure_read_relative(
            root,
            expected_component_path,
            maximum_bytes=MAX_CONTROL_BYTES,
            require_read_only=True,
        )
        result = _strict_json(result_payload, "M1 component result")
    except ValueError as exc:
        failures.append(str(exc))
        result_payload = b""
        result = {}
    if (
        not isinstance(result, dict)
        or result_payload != _canonical_pretty_json(result)
        or component.get("bytes") != len(result_payload)
        or component.get("sha256") != _sha256(result_payload)
    ):
        failures.append("M1 component result bytes do not match the receipt")
    expected_result_top = {
        "schema_version",
        "contract",
        "plan_version",
        "contract_path",
        "contract_sha256",
        "validator",
        "milestone",
        "run_id",
        "run_dir",
        "component_qualified",
        "formal_accepted",
        "component_only",
        "p0_eligible",
        "scope",
        "passed",
        "failures",
        "gates",
    }
    gates = result.get("gates") if isinstance(result, dict) else None
    exact_scope = {
        "provenance": True,
        "five_uav_health": True,
        "scene_binding": True,
        "runtime_inputs": True,
        "packet_path": False,
        "sealing": False,
        "attestation": False,
    }
    if (
        not isinstance(result, dict)
        or set(result) != expected_result_top
        or result.get("schema_version") != 2
        or result.get("contract") != M1_RESULT_CONTRACT
        or result.get("plan_version") != 3
        or result.get("contract_path") != PLAN_PATH
        or result.get("contract_sha256") != plan_sha256
        or result.get("validator") != "m1_five_uav_component_health"
        or result.get("milestone") != "M1"
        or result.get("run_id") != run_id
        or result.get("run_dir") != f"runs/{run_id}"
        or result.get("component_qualified") is not True
        or result.get("formal_accepted") is not False
        or result.get("component_only") is not True
        or result.get("p0_eligible") is not False
        or result.get("scope") != exact_scope
        or result.get("passed") is not True
        or result.get("failures") != []
        or not isinstance(gates, dict)
        or set(gates) != M1_RESULT_GATE_NAMES
        or any(
            not isinstance(record, dict)
            or record.get("status") != "passed"
            or not isinstance(record.get("proof"), str)
            or not record.get("proof")
            or (
                isinstance(record.get("details"), dict)
                and record["details"].get("failures") not in (None, [])
            )
            for record in (gates.values() if isinstance(gates, dict) else [])
        )
    ):
        failures.append("M1 component result is not the exact four-gate all-pass result")

    try:
        artifact_manifest = _m1_portable_manifest(root, run_id)
    except (OSError, ValueError) as exc:
        failures.append(f"M1 portable artifact rederivation failed: {exc}")
        artifact_manifest = {}
    if receipt.get("artifact_content_manifest") != artifact_manifest:
        failures.append("M1 portable artifact manifest does not rederive")

    payloads, host_manifest, local = _m1_host_validation_payloads(root, run_id)
    failures.extend(local)
    if receipt.get("host_validation_content_manifest") != host_manifest:
        failures.append("M1 host-validation manifest does not bind the receipt")
    if payloads.get("validation/result.json") != result_payload:
        failures.append("M1 independent validation result differs from component result")
    if payloads.get("validation/stderr.txt") != b"":
        failures.append("M1 independent validation stderr is not empty")
    failures.extend(_validate_m1_docker_raw(root, run_id, receipt, payloads))

    m0_authority = receipt.get("m0_status_authority")
    inherited_m0 = receipt.get("inherited_m0_qualification")
    m0_status_raw = payloads.get("m0/status_validation.json", b"")
    m0_receipt_raw = payloads.get("m0/host_final_receipt.json", b"")
    try:
        m0_status = _strict_json(m0_status_raw, "M1 inherited M0 status authority")
        m0_receipt = _strict_json(m0_receipt_raw, "M1 inherited M0 receipt")
    except ValueError as exc:
        failures.append(str(exc))
        m0_status = {}
        m0_receipt = {}
    m0_receipt_hash = _sha256(m0_receipt_raw)
    m0_vector = (
        m0_receipt.get("qualification_content_vector")
        if isinstance(m0_receipt, dict)
        else None
    )
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from network.validation.qualification_identity import (  # noqa: PLC0415
        qualification_prefixes_equal,
    )
    expected_m0_path = m0_receipt.get("receipt_path") if isinstance(m0_receipt, dict) else None
    if (
        not isinstance(m0_status, dict)
        or m0_status.get("schema_version") != 1
        or m0_status.get("contract") != STATUS_LINT_CONTRACT
        or m0_status.get("passed") is not True
        or m0_status.get("failures") != []
        or m0_status.get("report_commit") != execution_commit
        or m0_status.get("receipt_path") != expected_m0_path
        or m0_status_raw != _canonical_pretty_json(m0_status)
        or not isinstance(m0_receipt, dict)
        or m0_receipt.get("schema_version") != 3
        or m0_receipt.get("contract") != RECEIPT_CONTRACT
        or m0_receipt.get("formal_accepted") is not True
        or m0_receipt.get("passed") is not True
        or m0_receipt.get("failures") != []
        or m0_receipt.get("consumed_nodes") != ["Q0"]
        or m0_receipt_raw != _canonical_pretty_json(m0_receipt)
        or not qualification_prefixes_equal(m0_vector, expected_vector, ["Q0"])
        or not isinstance(m0_authority, dict)
        or m0_authority
        != {
            "contract": STATUS_LINT_CONTRACT,
            "report_commit": execution_commit,
            "receipt_path": expected_m0_path,
            "receipt_sha256": m0_receipt_hash,
            "status_validation_sha256": _sha256(m0_status_raw),
        }
        or not isinstance(inherited_m0, dict)
        or inherited_m0.get("schema_version") != 1
        or inherited_m0.get("contract")
        != "ams.m1.inherited-m0-qualification/v1"
        or inherited_m0.get("status_report_commit") != execution_commit
        or inherited_m0.get("canonical_receipt_path") != expected_m0_path
        or inherited_m0.get("mounted_receipt_path") != "/run/ams/m0-receipt.json"
        or inherited_m0.get("receipt_sha256") != m0_receipt_hash
        or inherited_m0.get("receipt_contract") != RECEIPT_CONTRACT
        or inherited_m0.get("receipt_run_id") != m0_receipt.get("run_id")
        or inherited_m0.get("qualification_contract_sha256")
        != m0_receipt.get("qualification_contract_sha256")
        or inherited_m0.get("qualification_vector_sha256")
        != m0_vector.get("vector_sha256")
        or inherited_m0.get("qualification_vector_commit") != m0_vector.get("git_commit")
        or inherited_m0.get("image_digest") != image_digest
        or inherited_m0.get("consumed_nodes") != ["Q0"]
        or inherited_m0.get("capabilities")
        != {
            "tun_device": True,
            "passwordless_sudo": True,
            "unshare_network_namespace": True,
        }
        or inherited_m0.get("available") is not True
    ):
        failures.append("M1 inherited M0 status/receipt qualification is not exact")

    contract_payload = {
        "run_id": run_id,
        "receipt_path": f"runs/{run_id}/metrics/m1_host_final_receipt.json",
        "source_commit": execution_commit,
        "image_digest": image_digest,
        "vector_sha256": expected_vector.get("vector_sha256"),
        "component_result_sha256": _sha256(result_payload),
        "artifact_content_sha256": artifact_manifest.get("content_sha256"),
        "host_validation_content_sha256": host_manifest.get("content_sha256"),
        "m0_receipt_sha256": m0_receipt_hash,
        "m0_status_validation_sha256": _sha256(m0_status_raw),
        "consumed_nodes": ["Q0", "Q1"],
    }
    if receipt.get("qualification_contract_sha256") != _sha256(
        _canonical_json(contract_payload)
    ):
        failures.append("M1 qualification contract hash does not rederive")

    if isinstance(expected_m0_path, str):
        rederived, error = _rederive_published_m1(
            root, run_id, str(image_digest), expected_m0_path
        )
    else:
        rederived, error = None, "M1 inherited M0 receipt path is unavailable"
    if error is not None:
        failures.append(error)
    elif rederived != result:
        failures.append("published M1 exact-image revalidation differs from stored result")
    return failures


def _status_only_diff_failures(root: Path, base: str, report: str) -> list[str]:
    failures: list[str] = []
    try:
        name_status = _parse_name_status(
            _git(
                root,
                [
                    "diff",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--name-status",
                    "-z",
                    "--no-renames",
                    base,
                    report,
                ],
            )
        )
    except ValueError as exc:
        return [str(exc)]
    if sorted(name_status) != sorted(("M", path) for path in STATUS_PATHS):
        failures.append("historical status-only diff is not exactly three modified status paths")
    for relative in STATUS_PATHS:
        try:
            if (
                _git_blob_record(root, base, relative).get("git_mode") != "100644"
                or _git_blob_record(root, report, relative).get("git_mode") != "100644"
            ):
                failures.append(f"historical status path mode is invalid: {relative}")
        except ValueError as exc:
            failures.append(str(exc))
    return failures


def _component_host_validation_payloads(
    root: Path,
    run_id: str,
    receipt: dict[str, Any],
) -> tuple[dict[str, bytes], list[str]]:
    failures: list[str] = []
    relative_root = f"runs/{run_id}/host_validation"
    try:
        names = _secure_tree_files(root, relative_root)
    except ValueError as exc:
        return {}, [str(exc)]
    prerequisite_names = set()
    for field in ("prerequisite_receipts", "required_component_receipts"):
        records = receipt.get(field)
        if not isinstance(records, dict):
            failures.append(f"component receipt {field} is malformed")
        else:
            prerequisite_names.update(records)
    expected = {
        "content_manifest.json",
        "main/initial_container_inspect.json",
        "main/final_container_inspect.json",
        "main/initial_image_inspect.json",
        "main/final_image_inspect.json",
        "validation/initial_container_inspect.json",
        "validation/final_container_inspect.json",
        "validation/image_inspect.json",
        "validation/result.json",
        "validation/stderr.txt",
        "status/validation.json",
        "status/prerequisites.json",
        *(f"status/receipts/{name}.json" for name in prerequisite_names),
    }
    if set(names) != expected:
        failures.append("component host-validation raw path set is not exact")
    payloads: dict[str, bytes] = {}
    for relative in names:
        try:
            payloads[relative] = _secure_read_relative(
                root,
                f"{relative_root}/{relative}",
                maximum_bytes=MAX_M1_ARTIFACT_BYTES,
                require_read_only=True,
                allow_empty=relative == "validation/stderr.txt",
            )
        except ValueError as exc:
            failures.append(str(exc))
    records = {
        relative: {"bytes": len(payload), "sha256": _sha256(payload)}
        for relative, payload in sorted(payloads.items())
        if relative != "content_manifest.json"
    }
    expected_manifest = {
        "schema_version": 1,
        "contract": "ams.component-host-validation-manifest/v1",
        "files": records,
        "file_count": len(records),
        "content_sha256": _sha256(_canonical_json(records)),
    }
    try:
        manifest = _strict_json(
            payloads.get("content_manifest.json", b""),
            "component host-validation manifest",
        )
    except ValueError as exc:
        failures.append(str(exc))
        manifest = None
    if (
        manifest != expected_manifest
        or receipt.get("host_validation_manifest") != expected_manifest
    ):
        failures.append("component host-validation manifest does not rederive")
    return payloads, failures


def _one_docker_inspect(payload: bytes, label: str) -> dict[str, Any]:
    value = _strict_json(payload, label)
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise ValueError(f"{label} is not exactly one Docker inspection")
    return value[0]


def _validate_component_main_container(
    root: Path,
    payloads: dict[str, bytes],
    *,
    profile_name: str,
    profile: dict[str, Any],
    run_id: str,
    receipt: dict[str, Any],
) -> list[str]:
    """Independently rederive the retained component producer boundary."""

    failures: list[str] = []
    try:
        initial = _one_docker_inspect(
            payloads.get("main/initial_container_inspect.json", b""),
            "component producer initial container",
        )
        final = _one_docker_inspect(
            payloads.get("main/final_container_inspect.json", b""),
            "component producer final container",
        )
        initial_image = _one_docker_inspect(
            payloads.get("main/initial_image_inspect.json", b""),
            "component producer initial image",
        )
        final_image = _one_docker_inspect(
            payloads.get("main/final_image_inspect.json", b""),
            "component producer final image",
        )
    except ValueError as exc:
        return [str(exc)]

    for field in ("Config", "HostConfig", "Mounts", "Path", "Args", "Image"):
        if initial.get(field) != final.get(field):
            failures.append(f"component producer immutable field changed: {field}")
    initial_state = initial.get("State") if isinstance(initial.get("State"), dict) else {}
    final_state = final.get("State") if isinstance(final.get("State"), dict) else {}
    config = final.get("Config") if isinstance(final.get("Config"), dict) else {}
    host = final.get("HostConfig") if isinstance(final.get("HostConfig"), dict) else {}
    image_digest = receipt.get("image_digest")
    container_id = receipt.get("container_id")
    source_commit = receipt.get("source_commit")
    if (
        initial.get("Id") != container_id
        or initial.get("Image") != image_digest
        or initial_state.get("Status") != "created"
        or initial_state.get("Running") is not False
        or initial.get("RestartCount") != 0
        or final.get("Id") != container_id
        or final.get("Image") != image_digest
        or final_state.get("Status") != "exited"
        or final_state.get("Running") is not False
        or final_state.get("OOMKilled") is not False
        or final_state.get("ExitCode") != 0
        or final.get("RestartCount") != 0
        or initial_image != final_image
        or initial_image.get("Id") != image_digest
    ):
        failures.append("component producer lifecycle/image identity is not exact")

    environment, local = _docker_environment(config.get("Env"))
    failures.extend(local)
    prerequisite_records: dict[str, Any] = {}
    for field in ("prerequisite_receipts", "required_component_receipts"):
        value = receipt.get(field)
        if not isinstance(value, dict) or set(prerequisite_records).intersection(value):
            failures.append(f"component producer {field} is malformed/overlapping")
        else:
            prerequisite_records.update(value)
    m0_record = prerequisite_records.get("m0")
    if not isinstance(m0_record, dict):
        failures.append("component producer prerequisite set lacks M0")
        m0_record = {}
    required_ams = {
        "AMS_CONTAINER_IMAGE": receipt.get("image_reference"),
        "AMS_CONTAINER_IMAGE_DIGEST": image_digest,
        "AMS_CONTAINER_IMAGE_DIGEST_SOURCE": "docker_image_inspect_host",
        "AMS_RUNTIME_CONTAINER_ID_FILE": "/run/ams/container_id",
        "AMS_COMPONENT_PROFILE": profile_name,
        "AMS_COMPONENT_SOURCE_MODE": "clean_git_clone_ro",
        "AMS_COMPONENT_SOURCE_COMMIT": source_commit,
        "AMS_COMPONENT_RUN_ID": run_id,
        "AMS_COMPONENT_STATUS_RESULT_PATH": "/run/ams/status-validation.json",
        "AMS_COMPONENT_PREREQUISITES_PATH": "/run/ams/prerequisites.json",
        "AMS_M1_SOURCE_MODE": "clean_git_clone_ro",
        "AMS_M1_SOURCE_COMMIT": source_commit,
        "AMS_M1_PROJECT_OVERLAY_MODE": "fresh_run_overlay",
        "AMS_M1_RUN_ID": run_id,
        "AMS_M0_CAPABILITY_PROBE_MODE": (
            "bounded_root_in_runtime"
            if profile["main_devices"]
            else "inherited_m0_host_final"
        ),
        "AMS_M1_M0_RECEIPT_PATH": "/run/ams/prerequisites/m0.json",
        "AMS_M1_M0_RECEIPT_CANONICAL_PATH": m0_record.get("canonical_path"),
        "AMS_M1_M0_RECEIPT_SHA256": m0_record.get("sha256"),
        "AMS_M1_M0_STATUS_COMMIT": source_commit,
    }
    if (
        any(environment.get(name) != value for name, value in required_ams.items())
        or {name for name in environment if name.startswith("AMS_")} != set(required_ams)
        or environment.get("NVIDIA_VISIBLE_DEVICES") != "all"
        or environment.get("NVIDIA_DRIVER_CAPABILITIES")
        != profile["nvidia_driver_capabilities"]
        or environment.get("SIONNA_MITSUBA_VARIANT") != "cuda_ad_mono_polarized"
        or environment.get("GZ_VERSION") != "harmonic"
    ):
        failures.append("component producer environment is not exact")

    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from network.validation.component_profiles import (  # noqa: PLC0415
        expected_gpu_device_requests,
    )

    expected_cap_add = [f"CAP_{value}" for value in profile["main_cap_add"]] or None
    expected_devices = (
        []
        if not profile["main_devices"]
        else [
            {
                "PathOnHost": "/dev/net/tun",
                "PathInContainer": "/dev/net/tun",
                "CgroupPermissions": "rwm",
            }
        ]
    )
    expected_tmpfs = {"/tmp": "rw,nosuid,nodev,exec,size=4g,mode=1777"}
    expected_security = {"no-new-privileges"}
    if profile["main_devices"]:
        expected_tmpfs["/run/netns"] = "rw,nosuid,nodev,noexec,size=16m,mode=0755"
        expected_security.add("apparmor=unconfined")
    security = host.get("SecurityOpt")
    normalized_security = (
        {
            "no-new-privileges" if item == "no-new-privileges:true" else item
            for item in security
        }
        if isinstance(security, list) and all(isinstance(item, str) for item in security)
        else set()
    )
    normalized_security.discard("label=disable")
    expected_command = [
        "scripts/acceptance_entrypoint.sh",
        "timeout",
        "--signal=TERM",
        "--kill-after=20s",
        f"{profile['timeout_s']}s",
        "env",
        f"RUN_ID={run_id}",
        profile["runner"],
    ]
    if (
        config.get("Image") != image_digest
        or config.get("User") != ("root:1000" if profile["main_devices"] else "ubuntu")
        or config.get("Entrypoint") != ["/ros_entrypoint.sh"]
        or config.get("Cmd") != expected_command
        or config.get("WorkingDir") != "/workspace/multiagent_simulation"
        or final.get("Path") != "/ros_entrypoint.sh"
        or final.get("Args") != expected_command
        or host.get("Privileged") is not False
        or host.get("NetworkMode") != profile["main_network"]
        or host.get("ReadonlyRootfs") is not True
        or host.get("RestartPolicy") != {"Name": "no", "MaximumRetryCount": 0}
        or host.get("CapAdd") != expected_cap_add
        or host.get("CapDrop") != ["ALL"]
        or host.get("Devices") != expected_devices
        or host.get("DeviceRequests")
        != expected_gpu_device_requests(profile["nvidia_driver_capabilities"])
        or host.get("Tmpfs") != expected_tmpfs
        or normalized_security != expected_security
    ):
        failures.append("component producer Config/HostConfig is not exact")

    mounts = final.get("Mounts")
    by_destination: dict[str, dict[str, Any]] = {}
    if isinstance(mounts, list):
        for item in mounts:
            destination = item.get("Destination") if isinstance(item, dict) else None
            if not isinstance(destination, str) or destination in by_destination:
                failures.append("component producer mounts are malformed/duplicate")
                continue
            by_destination[destination] = item
    else:
        failures.append("component producer mounts are not a list")
    expected_destinations = {
        "/workspace/multiagent_simulation",
        "/workspace/multiagent_simulation/runs",
        "/workspace/multiagent_simulation/.external/ns-3",
        "/run/ams/container_id",
        "/run/ams/status-validation.json",
        "/run/ams/prerequisites.json",
        *(f"/run/ams/prerequisites/{name}.json" for name in prerequisite_records),
    }
    if set(by_destination) != expected_destinations:
        failures.append("component producer mount destination set is not exact")
    for destination, item in by_destination.items():
        writable = destination == "/workspace/multiagent_simulation/runs"
        if (
            item.get("Type") != "bind"
            or item.get("RW") is not writable
            or item.get("Mode") != ("rw" if writable else "ro")
            or item.get("Propagation") != "rprivate"
            or not isinstance(item.get("Source"), str)
            or not Path(str(item.get("Source"))).is_absolute()
        ):
            failures.append(f"component producer mount policy is invalid: {destination}")
    source_snapshot = by_destination.get("/workspace/multiagent_simulation", {}).get("Source")
    staging_source = by_destination.get("/workspace/multiagent_simulation/runs", {}).get("Source")
    ns3_source = by_destination.get(
        "/workspace/multiagent_simulation/.external/ns-3", {}
    ).get("Source")
    identity_source = by_destination.get("/run/ams/container_id", {}).get("Source")
    status_source = by_destination.get("/run/ams/status-validation.json", {}).get("Source")
    prerequisites_source = by_destination.get("/run/ams/prerequisites.json", {}).get("Source")
    control_pattern = re.compile(
        re.escape(str(root.parent))
        + rf"/\.ams-component-control-{re.escape(run_id)}\.[A-Za-z0-9]+"
    )
    if (
        not isinstance(source_snapshot, str)
        or re.fullmatch(r"/tmp/ams-component-source\.[A-Za-z0-9]+", source_snapshot) is None
        or not isinstance(staging_source, str)
        or re.fullmatch(
            re.escape(str(root / "runs"))
            + rf"/\.component-stage-{re.escape(run_id)}\.[A-Za-z0-9]+",
            staging_source,
        )
        is None
        or ns3_source != str(root / ".external/ns-3")
        or not isinstance(identity_source, str)
        or re.fullmatch(r"/tmp/ams-container-id\.[A-Za-z0-9]+", identity_source) is None
        or not isinstance(status_source, str)
        or not isinstance(prerequisites_source, str)
        or Path(status_source).name != "status_validation.json"
        or Path(prerequisites_source).name != "prerequisites.json"
        or Path(status_source).parent != Path(prerequisites_source).parent
        or control_pattern.fullmatch(str(Path(status_source).parent)) is None
    ):
        failures.append("component producer mount source lineage is not exact")
    for name, record in prerequisite_records.items():
        mounted_source = by_destination.get(
            f"/run/ams/prerequisites/{name}.json", {}
        ).get("Source")
        if not isinstance(record, dict) or mounted_source != record.get("host_path"):
            failures.append(f"component producer prerequisite mount differs: {name}")
    return failures


def _validate_component_validation_container(
    payloads: dict[str, bytes],
    *,
    profile: dict[str, Any],
    run_id: str,
    image_digest: str,
    validation_container_id: str,
) -> list[str]:
    failures: list[str] = []
    try:
        initial = _one_docker_inspect(
            payloads.get("validation/initial_container_inspect.json", b""),
            "component validation initial container",
        )
        final = _one_docker_inspect(
            payloads.get("validation/final_container_inspect.json", b""),
            "component validation final container",
        )
        image = _one_docker_inspect(
            payloads.get("validation/image_inspect.json", b""),
            "component validation image",
        )
    except ValueError as exc:
        return [str(exc)]
    for field in ("Config", "HostConfig", "Mounts", "Path", "Args", "Image"):
        if initial.get(field) != final.get(field):
            failures.append(f"component validation immutable field changed: {field}")
    initial_state = initial.get("State") if isinstance(initial.get("State"), dict) else {}
    final_state = final.get("State") if isinstance(final.get("State"), dict) else {}
    config = final.get("Config") if isinstance(final.get("Config"), dict) else {}
    host = final.get("HostConfig") if isinstance(final.get("HostConfig"), dict) else {}
    image_config = image.get("Config") if isinstance(image.get("Config"), dict) else {}
    expected_args = [
        value.replace("{run_dir}", f"runs/{run_id}")
        for value in profile["validator_arguments"]
    ]
    expected_command = ["/usr/bin/python3.10", profile["validator"], *expected_args]
    if (
        initial.get("Id") != validation_container_id
        or initial.get("Image") != image_digest
        or initial_state.get("Status") != "created"
        or initial_state.get("Running") is not False
        or initial.get("RestartCount") != 0
        or final.get("Id") != validation_container_id
        or final.get("Image") != image_digest
        or final_state.get("Status") != "exited"
        or final_state.get("Running") is not False
        or final_state.get("OOMKilled") is not False
        or final_state.get("ExitCode") != 0
        or final.get("RestartCount") != 0
        or image.get("Id") != image_digest
        or config.get("Image") != image_digest
        or config.get("User") != "ubuntu"
        or config.get("Entrypoint") != ["/ros_entrypoint.sh"]
        or config.get("Cmd") != expected_command
        or "--no-write" not in expected_command
        or config.get("WorkingDir") != "/workspace/multiagent_simulation"
        or config.get("Env") != image_config.get("Env")
        or host.get("Privileged") is not False
        or host.get("NetworkMode") != "none"
        or host.get("ReadonlyRootfs") is not True
        or host.get("RestartPolicy") != {"Name": "no", "MaximumRetryCount": 0}
        or host.get("CapAdd") is not None
        or host.get("CapDrop") != ["ALL"]
        or host.get("Devices") not in (None, [])
        or host.get("DeviceRequests") not in (None, [])
        or host.get("Tmpfs")
        != {"/tmp": "rw,nosuid,nodev,exec,size=1g,mode=1777"}
        or host.get("SecurityOpt")
        not in (
            ["no-new-privileges"],
            ["no-new-privileges:true"],
            ["label=disable", "no-new-privileges"],
            ["label=disable", "no-new-privileges:true"],
        )
    ):
        failures.append("component exact-image --no-write validation boundary is invalid")
    mounts = final.get("Mounts")
    by_destination = {
        item.get("Destination"): item
        for item in mounts
        if isinstance(item, dict) and isinstance(item.get("Destination"), str)
    } if isinstance(mounts, list) else {}
    required_destinations = {
        "/workspace/multiagent_simulation",
        "/workspace/multiagent_simulation/runs",
        "/workspace/multiagent_simulation/.external/ns-3",
        "/run/ams/status-validation.json",
        "/run/ams/prerequisites.json",
    }
    required_destinations.update(
        f"/run/ams/prerequisites/{name.removeprefix('status/receipts/').removesuffix('.json')}.json"
        for name in payloads
        if name.startswith("status/receipts/") and name.endswith(".json")
    )
    if set(by_destination) != required_destinations or any(
        item.get("Type") != "bind"
        or item.get("RW") is not False
        or item.get("Mode") != "ro"
        or item.get("Propagation") != "rprivate"
        or not isinstance(item.get("Source"), str)
        or not Path(item["Source"]).is_absolute()
        for item in by_destination.values()
    ):
        failures.append("component validation read-only mount boundary is not exact")
    return failures


def _validate_component_prerequisite_authority(
    root: Path,
    payloads: dict[str, bytes],
    *,
    prerequisites: Any,
    profile_name: str,
    profile: dict[str, Any],
    execution_commit: str,
    expected_vector: dict[str, Any],
) -> list[str]:
    """Rebind copied prerequisite bytes to live immutable authorities."""

    failures: list[str] = []
    expected_top = {
        "schema_version",
        "contract",
        "profile",
        "source_commit",
        "status",
        "receipts",
        "component_receipts",
    }
    if not isinstance(prerequisites, dict) or set(prerequisites) != expected_top:
        return [f"{profile_name} prerequisite manifest schema is not exact"]
    status = prerequisites.get("status")
    milestone_records = prerequisites.get("receipts")
    component_records = prerequisites.get("component_receipts")
    if (
        prerequisites.get("schema_version") != 1
        or prerequisites.get("contract") != "ams.component-prerequisites/v1"
        or prerequisites.get("profile") != profile_name
        or prerequisites.get("source_commit") != execution_commit
        or not isinstance(status, dict)
        or not isinstance(milestone_records, dict)
        or not isinstance(component_records, dict)
        or set(milestone_records).intersection(component_records)
        or set(component_records) != set(profile["required_component_profiles"])
    ):
        failures.append(f"{profile_name} prerequisite manifest identity is invalid")
        milestone_records = milestone_records if isinstance(milestone_records, dict) else {}
        component_records = component_records if isinstance(component_records, dict) else {}
        status = status if isinstance(status, dict) else {}

    expected_milestones = {
        f"m{index}" for index in range(profile["prerequisite_status_count"])
    }
    if set(milestone_records) != expected_milestones:
        failures.append(f"{profile_name} milestone prerequisite set is not exact")
    status_payload = payloads.get("status/validation.json", b"")
    try:
        status_result = _strict_json(
            status_payload, f"{profile_name} copied status validation"
        )
    except ValueError as exc:
        failures.append(str(exc))
        status_result = None
    if (
        not isinstance(status_result, dict)
        or status_payload != _canonical_pretty_json(status_result)
        or status_result.get("schema_version") != 1
        or status_result.get("contract") != STATUS_LINT_CONTRACT
        or status_result.get("passed") is not True
        or status_result.get("failures") != []
        or status_result.get("report_commit") != execution_commit
        or status_result.get("status_paths") != list(STATUS_PATHS)
        or status.get("contract") != profile["prerequisite_status_contract"]
        or status.get("closed_count") != profile["prerequisite_status_count"]
        or status.get("report_commit") != execution_commit
        or status.get("result_sha256") != _sha256(status_payload)
    ):
        failures.append(f"{profile_name} copied status authority is invalid")

    expected_raw_names = {
        *(f"status/receipts/{name}.json" for name in milestone_records),
        *(f"status/receipts/{name}.json" for name in component_records),
    }
    observed_raw_names = {
        name
        for name in payloads
        if name.startswith("status/receipts/") and name.endswith(".json")
    }
    if observed_raw_names != expected_raw_names:
        failures.append(f"{profile_name} copied prerequisite receipt set is not exact")

    for name, record in sorted(milestone_records.items()):
        raw = payloads.get(f"status/receipts/{name}.json", b"")
        canonical_path = record.get("canonical_path") if isinstance(record, dict) else None
        run_id = record.get("run_id") if isinstance(record, dict) else None
        expected_record_keys = {
            "milestone",
            "canonical_path",
            "host_path",
            "sha256",
            "contract",
            "run_id",
        }
        canonical = b""
        if isinstance(canonical_path, str):
            try:
                canonical = _secure_read_relative(
                    root,
                    canonical_path,
                    maximum_bytes=MAX_RECEIPT_BYTES,
                    require_read_only=True,
                )
            except ValueError as exc:
                failures.append(str(exc))
        try:
            parsed = _strict_json(raw, f"{profile_name} copied {name} receipt")
        except ValueError as exc:
            failures.append(str(exc))
            parsed = None
        if (
            not isinstance(record, dict)
            or set(record) != expected_record_keys
            or name not in expected_milestones
            or record.get("milestone") != name.upper()
            or not isinstance(run_id, str)
            or SAFE_RUN_ID.fullmatch(run_id) is None
            or not isinstance(canonical_path, str)
            or not canonical_path.startswith(f"runs/{run_id}/metrics/")
            or record.get("host_path") != str(root / str(canonical_path))
            or raw != canonical
            or record.get("sha256") != _sha256(raw)
            or not isinstance(parsed, dict)
            or raw != _canonical_pretty_json(parsed)
            or parsed.get("contract") != record.get("contract")
            or parsed.get("run_id") != run_id
            or parsed.get("receipt_path") != canonical_path
            or parsed.get("formal_accepted") is not True
            or parsed.get("passed") is not True
            or parsed.get("failures") != []
        ):
            failures.append(f"{profile_name} copied milestone receipt differs: {name}")

    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from network.validation.component_profiles import load_profiles  # noqa: PLC0415

    try:
        profiles = load_profiles(
            root / "network/config/component_acceptance_profiles.json"
        )
    except ValueError as exc:
        failures.append(str(exc))
        profiles = {}
    for required_name, record in sorted(component_records.items()):
        required_profile = profiles.get(required_name)
        raw = payloads.get(f"status/receipts/{required_name}.json", b"")
        canonical_path = record.get("canonical_path") if isinstance(record, dict) else None
        run_id = record.get("run_id") if isinstance(record, dict) else None
        expected_record_keys = {
            "profile",
            "canonical_path",
            "host_path",
            "sha256",
            "contract",
            "run_id",
        }
        canonical = b""
        if isinstance(canonical_path, str):
            try:
                canonical = _secure_read_relative(
                    root,
                    canonical_path,
                    maximum_bytes=MAX_RECEIPT_BYTES,
                    require_read_only=True,
                )
            except ValueError as exc:
                failures.append(str(exc))
        try:
            parsed = _strict_json(raw, f"{profile_name} copied {required_name} receipt")
        except ValueError as exc:
            failures.append(str(exc))
            parsed = None
        if (
            not isinstance(record, dict)
            or set(record) != expected_record_keys
            or not isinstance(required_profile, dict)
            or record.get("profile") != required_name
            or not isinstance(run_id, str)
            or SAFE_RUN_ID.fullmatch(run_id) is None
            or canonical_path
            != f"runs/{run_id}/metrics/{required_profile.get('receipt_name') if isinstance(required_profile, dict) else ''}"
            or record.get("host_path") != str(root / str(canonical_path))
            or raw != canonical
            or record.get("sha256") != _sha256(raw)
            or not isinstance(parsed, dict)
            or raw != _canonical_pretty_json(parsed)
            or parsed.get("contract") != record.get("contract")
            or parsed.get("contract")
            != (required_profile.get("receipt_contract") if isinstance(required_profile, dict) else None)
            or parsed.get("profile") != required_name
            or parsed.get("run_id") != run_id
            or parsed.get("receipt_path") != canonical_path
            or parsed.get("source_commit") != execution_commit
            or parsed.get("formal_accepted") is not True
            or parsed.get("passed") is not True
            or parsed.get("failures") != []
        ):
            failures.append(
                f"{profile_name} copied component prerequisite differs: {required_name}"
            )
        elif isinstance(parsed, dict) and isinstance(required_profile, dict):
            failures.extend(
                f"{profile_name} prerequisite {required_name}: {failure}"
                for failure in _validate_component_receipt(
                    root,
                    parsed,
                    profile_name=required_name,
                    profile=required_profile,
                    execution_commit=execution_commit,
                    expected_vector=expected_vector,
                )
            )
    return failures


def _validate_component_receipt(
    root: Path,
    receipt: dict[str, Any],
    *,
    profile_name: str,
    profile: dict[str, Any],
    execution_commit: str,
    expected_vector: dict[str, Any],
) -> list[str]:
    failures: list[str] = []
    expected_top = {
        "schema_version",
        "contract",
        "profile",
        "run_id",
        "receipt_path",
        "source_commit",
        "image_reference",
        "image_digest",
        "container_id",
        "validation_container_id",
        "consumed_nodes",
        "qualification_content_vector",
        "qualification_consumption",
        "qualification_contract_sha256",
        "formal_accepted",
        "passed",
        "failures",
        "result_contract",
        "result_sha256",
        "result",
        "component_content_manifest",
        "host_validation_manifest",
        "status_authority",
        "prerequisite_receipts",
        "required_component_receipts",
    }
    if set(receipt) != expected_top:
        return [f"{profile_name} host-final receipt schema is not exact"]
    run_id = receipt.get("run_id")
    image_digest = receipt.get("image_digest")
    validation_id = receipt.get("validation_container_id")
    if (
        not isinstance(run_id, str)
        or SAFE_RUN_ID.fullmatch(run_id) is None
        or receipt.get("schema_version") != 1
        or receipt.get("contract") != profile["receipt_contract"]
        or receipt.get("profile") != profile_name
        or receipt.get("receipt_path")
        != f"runs/{run_id}/metrics/{profile['receipt_name']}"
        or receipt.get("source_commit") != execution_commit
        or receipt.get("consumed_nodes") != profile["consumed_nodes"]
        or receipt.get("formal_accepted") is not True
        or receipt.get("passed") is not True
        or receipt.get("failures") != []
        or receipt.get("result_contract") != profile["result_contract"]
        or IMAGE_DIGEST.fullmatch(str(image_digest or "")) is None
        or CONTAINER_ID.fullmatch(str(receipt.get("container_id") or "")) is None
        or CONTAINER_ID.fullmatch(str(validation_id or "")) is None
        or validation_id == receipt.get("container_id")
    ):
        failures.append(f"{profile_name} host-final identity/result is invalid")

    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from network.validation.qualification_identity import (  # noqa: PLC0415
        qualification_consumption,
    )

    consumption = receipt.get("qualification_consumption")
    try:
        expected_consumption = qualification_consumption(expected_vector, profile_name)
    except Exception as exc:
        failures.append(f"{profile_name} qualification consumption failed: {exc}")
        expected_consumption = None
    if (
        receipt.get("qualification_content_vector") != expected_vector
        or consumption != expected_consumption
    ):
        failures.append(f"{profile_name} qualification vector/consumption is invalid")

    run_relative = f"runs/{run_id}"
    result_relative = f"{run_relative}/{profile['result_path']}"
    try:
        result_payload = _secure_read_relative(
            root,
            result_relative,
            maximum_bytes=MAX_M1_ARTIFACT_BYTES,
            require_read_only=True,
        )
        result = _strict_json(result_payload, f"{profile_name} component result")
    except ValueError as exc:
        failures.append(str(exc))
        result_payload = b""
        result = None
    observed_contract = (
        result.get("contract", result.get("validation_contract"))
        if isinstance(result, dict)
        else None
    )
    gates = result.get("gates") if isinstance(result, dict) else None
    if (
        result != receipt.get("result")
        or receipt.get("result_sha256") != _sha256(result_payload)
        or observed_contract != profile["result_contract"]
        or not isinstance(result, dict)
        or result.get("passed") is not True
        or result.get("failures", []) != []
        or not isinstance(gates, dict)
        or not gates
        or any(
            not isinstance(gate, dict)
            or not (gate.get("passed") is True or gate.get("status") == "passed")
            or gate.get("failures", []) != []
            for gate in (gates.values() if isinstance(gates, dict) else [])
        )
    ):
        failures.append(f"{profile_name} component result does not rederive")

    expected_contract_sha = _sha256(
        _canonical_json(
            {
                "contract": "ams.component-qualification/v1",
                "profile": profile_name,
                "source_commit": execution_commit,
                "image_digest": image_digest,
                "consumed_nodes": profile["consumed_nodes"],
                "consumed_node_sha256": (
                    expected_consumption.get("consumed_node_sha256")
                    if isinstance(expected_consumption, dict)
                    else None
                ),
                "result_contract": profile["result_contract"],
                "result_sha256": _sha256(result_payload),
            }
        )
    )
    if receipt.get("qualification_contract_sha256") != expected_contract_sha:
        failures.append(f"{profile_name} qualification contract hash does not rederive")
    try:
        portable = _m1_portable_manifest(
            root,
            run_id,
            receipt_name=profile["receipt_name"],
            extra_excluded=("metrics/component_publication_manifest.json",),
        )
    except ValueError as exc:
        failures.append(str(exc))
    else:
        if receipt.get("component_content_manifest") != portable:
            failures.append(f"{profile_name} component content manifest does not rederive")

    payloads, payload_failures = _component_host_validation_payloads(
        root, run_id, receipt
    )
    failures.extend(payload_failures)
    if (
        payloads.get("validation/result.json") != result_payload
        or payloads.get("validation/stderr.txt") != b""
    ):
        failures.append(f"{profile_name} independent validator result/stderr is invalid")
    failures.extend(
        _validate_component_main_container(
            root,
            payloads,
            profile_name=profile_name,
            profile=profile,
            run_id=run_id,
            receipt=receipt,
        )
    )
    failures.extend(
        _validate_component_validation_container(
            payloads,
            profile=profile,
            run_id=run_id,
            image_digest=str(image_digest),
            validation_container_id=str(validation_id),
        )
    )
    try:
        prerequisites = _strict_json(
            payloads.get("status/prerequisites.json", b""),
            f"{profile_name} prerequisites",
        )
    except ValueError as exc:
        failures.append(str(exc))
        prerequisites = None
    if (
        not isinstance(prerequisites, dict)
        or prerequisites.get("status") != receipt.get("status_authority")
        or prerequisites.get("receipts") != receipt.get("prerequisite_receipts")
        or prerequisites.get("component_receipts")
        != receipt.get("required_component_receipts")
    ):
        failures.append(f"{profile_name} prerequisite authority does not rederive")
    failures.extend(
        _validate_component_prerequisite_authority(
            root,
            payloads,
            prerequisites=prerequisites,
            profile_name=profile_name,
            profile=profile,
            execution_commit=execution_commit,
            expected_vector=expected_vector,
        )
    )
    return failures


def _validate_historical_m0_status(
    root: Path,
    m1_source_commit: str,
    *,
    plan_payload: bytes,
    plan_sha256: str,
    m0_receipt: dict[str, Any],
    m0_citation: dict[str, Any],
) -> list[str]:
    """Re-run v1 status authority on the exact commit consumed by formal M1."""

    failures: list[str] = []
    texts: dict[str, str] = {}
    records: list[dict[str, Any]] = []
    for relative in STATUS_PATHS:
        try:
            payload = _git(root, ["show", f"{m1_source_commit}:{relative}"])
            text = payload.decode("utf-8", errors="strict")
            texts[relative] = text
            records.append(_extract_metadata(text, f"historical {relative}"))
        except (UnicodeError, ValueError) as exc:
            failures.append(str(exc))
    if len(records) != len(STATUS_PATHS):
        return failures
    if not all(record == records[0] for record in records[1:]):
        return failures + ["historical M0 status documents do not share exact metadata"]
    metadata = records[0]
    if (
        metadata.get("schema_version") != 1
        or metadata.get("contract") != STATUS_METADATA_CONTRACT
    ):
        failures.append("M1 source commit does not contain v1 M0 status authority")
    if metadata.get("evidence") != m0_citation:
        failures.append("historical M0 status citation differs from current M0 citation")
    base = metadata.get("technical_base_commit")
    execution = metadata.get("execution_commit")
    if not isinstance(base, str) or not _commit_exists(root, base):
        failures.append("historical M0 technical base is invalid")
    else:
        failures.extend(_status_only_diff_failures(root, base, m1_source_commit))
    if not isinstance(execution, str) or not _commit_exists(root, execution):
        failures.append("historical M0 execution commit is invalid")
    else:
        try:
            if (
                _git(root, ["show", f"{base}:{PLAN_PATH}"]) != plan_payload
                or _git(root, ["show", f"{execution}:{PLAN_PATH}"]) != plan_payload
            ):
                failures.append("historical M0 status changed the authoritative plan")
        except ValueError as exc:
            failures.append(str(exc))
    state = status_documents_status(
        texts[STATUS_PATHS[0]],
        texts[STATUS_PATHS[1]],
        texts[STATUS_PATHS[2]],
        m0_receipt=m0_receipt,
    )
    failures.extend(state["failures"])
    failures.extend(
        _validate_metadata(
            metadata,
            root=root,
            state=state,
            report_commit=m1_source_commit,
            plan_sha256=plan_sha256,
            receipt=m0_receipt,
        )
    )
    return failures


def _read_cited_receipt(
    root: Path,
    citation: Any,
    *,
    receipt_name: str,
    label: str,
) -> tuple[dict[str, Any], str, bytes]:
    if not isinstance(citation, dict):
        raise ValueError(f"{label} citation is not a mapping")
    run_id = citation.get("run_id")
    if not isinstance(run_id, str) or SAFE_RUN_ID.fullmatch(run_id) is None:
        raise ValueError(f"{label} citation run ID is unsafe")
    canonical_path = f"runs/{run_id}/metrics/{receipt_name}"
    if citation.get("receipt_path") != canonical_path:
        raise ValueError(f"{label} citation receipt path is not canonical")
    payload = _secure_read_relative(
        root,
        canonical_path,
        maximum_bytes=MAX_RECEIPT_BYTES,
        require_read_only=True,
    )
    receipt = _strict_json(payload, label)
    if not isinstance(receipt, dict) or payload != _canonical_pretty_json(receipt):
        raise ValueError(f"{label} bytes are not one canonical JSON object")
    if citation.get("receipt_sha256") != _sha256(payload):
        raise ValueError(f"{label} citation hash differs from canonical receipt bytes")
    return receipt, canonical_path, payload


def _validate_live_v2(
    root: Path,
    texts: dict[str, str],
    metadata: dict[str, Any],
    *,
    report_commit: str,
    plan_payload: bytes,
    plan_sha256: str,
) -> tuple[list[str], list[tuple[str, bytes]], dict[str, Any] | None]:
    failures: list[str] = []
    evidence = metadata.get("evidence")
    if not isinstance(evidence, dict) or set(evidence) != {"m0", "m1"}:
        return (
            ["shared status metadata v2 evidence map is not exact"],
            [],
            None,
        )
    try:
        m0_receipt, m0_path, m0_payload = _read_cited_receipt(
            root,
            evidence.get("m0"),
            receipt_name=M0_RECEIPT_NAME,
            label="M0 host-final receipt",
        )
        m1_receipt, m1_path, m1_payload = _read_cited_receipt(
            root,
            evidence.get("m1"),
            receipt_name=M1_RECEIPT_NAME,
            label="M1 host-final receipt",
        )
    except ValueError as exc:
        return [str(exc)], [], None
    receipt_records = [(m0_path, m0_payload), (m1_path, m1_payload)]
    m1_execution = metadata.get("execution_commit")
    m0_vector_record = m0_receipt.get("qualification_content_vector")
    m0_execution = (
        m0_vector_record.get("git_commit")
        if isinstance(m0_vector_record, dict)
        else None
    )
    if not isinstance(m0_execution, str) or not _commit_exists(root, m0_execution):
        failures.append("M0 receipt execution commit is invalid")
        return failures, receipt_records, None
    if not isinstance(m1_execution, str) or not _commit_exists(root, m1_execution):
        failures.append("M1 execution commit is invalid")
        return failures, receipt_records, None
    if not _is_ancestor(root, m0_execution, m1_execution):
        failures.append("M0 execution commit is not an ancestor of M1 execution")
    try:
        if _git(root, ["show", f"{m0_execution}:{PLAN_PATH}"]) != plan_payload:
            failures.append("plan bytes differ at M0 execution commit")
    except ValueError as exc:
        failures.append(str(exc))

    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from network.validation.qualification_identity import (  # noqa: PLC0415
        qualification_content_vector,
    )

    try:
        m0_vector = qualification_content_vector(root, m0_execution)
        m1_vector = qualification_content_vector(root, m1_execution)
        report_vector = qualification_content_vector(root, report_commit)
        base_vector = qualification_content_vector(
            root, str(metadata.get("technical_base_commit"))
        )
    except Exception as exc:
        failures.append(f"status v2 qualification vectors could not be derived: {exc}")
        return failures, receipt_records, None
    for comparison, label in ((report_vector, "report"), (base_vector, "technical base")):
        if {key: value for key, value in comparison.items() if key != "git_commit"} != {
            key: value for key, value in m1_vector.items() if key != "git_commit"
        }:
            failures.append(f"status v2 {label} qualification content differs from M1")
    m0_nodes = m0_vector.get("node_hashes") if isinstance(m0_vector, dict) else {}
    m1_nodes = m1_vector.get("node_hashes") if isinstance(m1_vector, dict) else {}
    if (
        m0_vector.get("policy_id") != m1_vector.get("policy_id")
        or m0_vector.get("policy_sha256") != m1_vector.get("policy_sha256")
        or not isinstance(m0_nodes, dict)
        or not isinstance(m1_nodes, dict)
        or m0_nodes.get("Q0") != m1_nodes.get("Q0")
    ):
        failures.append("M0 Q0 identity is not reusable at the M1 execution commit")

    failures.extend(
        _validate_receipt(
            root,
            m0_receipt,
            execution_commit=m0_execution,
            expected_vector=m0_vector,
            plan_sha256=plan_sha256,
        )
    )
    failures.extend(
        _validate_historical_m0_status(
            root,
            m1_execution,
            plan_payload=plan_payload,
            plan_sha256=plan_sha256,
            m0_receipt=m0_receipt,
            m0_citation=evidence["m0"],
        )
    )
    failures.extend(
        _validate_m1_receipt(
            root,
            m1_receipt,
            execution_commit=m1_execution,
            expected_vector=m1_vector,
            plan_sha256=plan_sha256,
        )
    )
    state = status_documents_status(
        texts[STATUS_PATHS[0]],
        texts[STATUS_PATHS[1]],
        texts[STATUS_PATHS[2]],
        m0_receipt=m0_receipt,
        m1_receipt=m1_receipt,
    )
    failures.extend(state["failures"])
    failures.extend(
        _validate_metadata_v2(
            metadata,
            root=root,
            state=state,
            report_commit=report_commit,
            plan_sha256=plan_sha256,
            m0_receipt=m0_receipt,
            m1_receipt=m1_receipt,
        )
    )
    return failures, receipt_records, state


def _historical_status_bundle(
    root: Path, commit: str
) -> tuple[dict[str, str], dict[str, Any]]:
    texts: dict[str, str] = {}
    metadata_values: list[dict[str, Any]] = []
    for relative in STATUS_PATHS:
        try:
            text = _git(root, ["show", f"{commit}:{relative}"]).decode(
                "utf-8", errors="strict"
            )
        except UnicodeError as exc:
            raise ValueError(f"historical status path is not UTF-8: {relative}") from exc
        texts[relative] = text
        metadata_values.append(_extract_metadata(text, f"historical {relative}"))
    if any(value != metadata_values[0] for value in metadata_values[1:]):
        raise ValueError("historical status documents do not share exact metadata")
    return texts, metadata_values[0]


def _validate_live_component_version(
    root: Path,
    texts: dict[str, str],
    metadata: dict[str, Any],
    *,
    version: int,
    report_commit: str,
    plan_payload: bytes,
    plan_sha256: str,
) -> tuple[list[str], list[tuple[str, bytes]], dict[str, Any] | None]:
    """Recursively validate cumulative v3/v4/v5 milestone authority."""

    failures: list[str] = []
    if version not in {3, 4, 5}:
        return ["unsupported cumulative component status version"], [], None
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from network.validation.component_profiles import load_profiles  # noqa: PLC0415
    from network.validation.qualification_identity import (  # noqa: PLC0415
        qualification_content_vector,
        qualification_prefixes_equal,
    )

    try:
        profiles = load_profiles(
            root / "network/config/component_acceptance_profiles.json"
        )
    except ValueError as exc:
        return [str(exc)], [], None
    evidence = metadata.get("evidence")
    expected_keys = {f"m{index}" for index in range(version)}
    if not isinstance(evidence, dict) or set(evidence) != expected_keys:
        return [f"shared status metadata v{version} evidence map is not exact"], [], None

    receipts: dict[str, dict[str, Any]] = {}
    receipt_payloads: dict[str, bytes] = {}
    receipt_records: list[tuple[str, bytes]] = []
    try:
        for index in range(version):
            key = f"m{index}"
            if index == 0:
                receipt_name = M0_RECEIPT_NAME
            elif index == 1:
                receipt_name = M1_RECEIPT_NAME
            else:
                receipt_name = profiles[f"m{index}_component"]["receipt_name"]
            receipt, path, payload = _read_cited_receipt(
                root,
                evidence[key],
                receipt_name=receipt_name,
                label=f"M{index} host-final receipt",
            )
            receipts[key] = receipt
            receipt_payloads[key] = payload
            receipt_records.append((path, payload))
    except ValueError as exc:
        return [str(exc)], receipt_records, None

    latest_key = f"m{version - 1}"
    latest = receipts[latest_key]
    latest_source = latest.get("source_commit")
    if not isinstance(latest_source, str) or not _commit_exists(root, latest_source):
        return [f"M{version - 1} component source commit is invalid"], receipt_records, None
    try:
        if _git(root, ["show", f"{latest_source}:{PLAN_PATH}"]) != plan_payload:
            failures.append(
                f"plan bytes differ at M{version - 1} component execution commit"
            )
        historical_texts, historical_metadata = _historical_status_bundle(
            root, latest_source
        )
    except ValueError as exc:
        return failures + [str(exc)], receipt_records, None
    if (
        historical_metadata.get("schema_version") != version - 1
        or historical_metadata.get("contract")
        != STATUS_METADATA_CONTRACTS[version - 1]
    ):
        failures.append(
            f"M{version - 1} source does not contain v{version - 1} status authority"
        )
    if version - 1 == 2:
        previous_failures, _, _ = _validate_live_v2(
            root,
            historical_texts,
            historical_metadata,
            report_commit=latest_source,
            plan_payload=plan_payload,
            plan_sha256=plan_sha256,
        )
    else:
        previous_failures, _, _ = _validate_live_component_version(
            root,
            historical_texts,
            historical_metadata,
            version=version - 1,
            report_commit=latest_source,
            plan_payload=plan_payload,
            plan_sha256=plan_sha256,
        )
    failures.extend(
        f"historical v{version - 1}: {failure}" for failure in previous_failures
    )
    failures.extend(_status_only_diff_failures(root, latest_source, report_commit))

    try:
        latest_vector = qualification_content_vector(root, latest_source)
        report_vector = qualification_content_vector(root, report_commit)
        base_vector = qualification_content_vector(
            root, str(metadata.get("technical_base_commit"))
        )
    except Exception as exc:
        return failures + [f"status v{version} vectors could not be derived: {exc}"], receipt_records, None
    consumed_nodes = [f"Q{index}" for index in range(version)]
    for candidate, label in ((report_vector, "report"), (base_vector, "technical base")):
        try:
            equal = qualification_prefixes_equal(
                latest_vector, candidate, consumed_nodes
            )
        except Exception as exc:
            failures.append(f"status v{version} {label} prefix is invalid: {exc}")
        else:
            if not equal:
                failures.append(
                    f"status v{version} {label} changed a consumed qualification node"
                )
    previous_vector = receipts[f"m{version - 2}"].get(
        "qualification_content_vector"
    )
    previous_nodes = [f"Q{index}" for index in range(version - 1)]
    try:
        reusable = qualification_prefixes_equal(
            previous_vector, latest_vector, previous_nodes
        )
    except Exception as exc:
        failures.append(f"prior milestone prefix identity is invalid: {exc}")
    else:
        if not reusable:
            failures.append(
                f"M0..M{version - 2} prefix is not reusable at M{version - 1}"
            )

    latest_profile_name = f"m{version - 1}_component"
    failures.extend(
        _validate_component_receipt(
            root,
            latest,
            profile_name=latest_profile_name,
            profile=profiles[latest_profile_name],
            execution_commit=latest_source,
            expected_vector=latest_vector,
        )
    )
    component_receipts = {
        f"m{index}": receipts[f"m{index}"] for index in range(2, version)
    }
    state = status_documents_status(
        texts[STATUS_PATHS[0]],
        texts[STATUS_PATHS[1]],
        texts[STATUS_PATHS[2]],
        m0_receipt=receipts["m0"],
        m1_receipt=receipts["m1"],
        component_receipts=component_receipts,
    )
    failures.extend(state["failures"])
    failures.extend(
        _validate_metadata_component_version(
            metadata,
            version=version,
            root=root,
            state=state,
            report_commit=report_commit,
            plan_sha256=plan_sha256,
            receipts=receipts,
            receipt_payloads=receipt_payloads,
            profiles=profiles,
        )
    )
    return failures, receipt_records, state


def _parse_name_status(raw: bytes) -> list[tuple[str, str]]:
    values = raw.rstrip(b"\0").split(b"\0") if raw else []
    if len(values) % 2:
        raise ValueError("Git name-status output is malformed")
    result: list[tuple[str, str]] = []
    for index in range(0, len(values), 2):
        status_value = values[index].decode("ascii")
        path = values[index + 1].decode("utf-8")
        result.append((status_value, path))
    return result


def validate_live_status(root: Path = ROOT_DIR) -> dict[str, Any]:
    """Validate canonical live paths; ``root`` is injectable only for tests."""

    failures: list[str] = []
    report_commit: str | None = None
    metadata: dict[str, Any] | None = None
    receipt_path: str | None = None
    receipt_records: list[tuple[str, bytes]] = []
    try:
        root = root.resolve(strict=True)
        top = Path(_git(root, ["rev-parse", "--show-toplevel"]).decode("utf-8").strip()).resolve(
            strict=True
        )
        if top != root:
            raise ValueError("lint root is not the exact Git worktree root")
        report_commit = _git(root, ["rev-parse", "HEAD"]).decode("ascii").strip()
        if SHA1.fullmatch(report_commit) is None:
            raise ValueError("report HEAD is not an exact 40-hex commit")
        dirty = _git(
            root,
            [
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                "--ignore-submodules=none",
            ],
        )
        if dirty:
            raise ValueError("live status lint requires a clean HEAD")

        texts: dict[str, str] = {}
        metadata_records: list[dict[str, Any]] = []
        for relative in STATUS_PATHS:
            payload = _secure_read_relative(root, relative, maximum_bytes=MAX_STATUS_BYTES)
            try:
                text = payload.decode("utf-8")
            except UnicodeError as exc:
                raise ValueError(f"status document is not UTF-8: {relative}") from exc
            if "\x00" in text:
                raise ValueError(f"status document contains NUL: {relative}")
            texts[relative] = text
            metadata_records.append(_extract_metadata(text, relative))
        if not all(record == metadata_records[0] for record in metadata_records[1:]):
            raise ValueError("the three status documents do not share exact metadata")
        metadata = metadata_records[0]

        technical_base = metadata.get("technical_base_commit")
        execution_commit = metadata.get("execution_commit")
        if not isinstance(technical_base, str) or not _commit_exists(root, technical_base):
            raise ValueError("technical base commit is invalid")
        if not isinstance(execution_commit, str) or not _commit_exists(root, execution_commit):
            raise ValueError("execution commit is invalid")
        if not _is_ancestor(root, technical_base, report_commit):
            raise ValueError("technical base is not an ancestor of report HEAD")
        if not _is_ancestor(root, execution_commit, report_commit):
            raise ValueError("execution commit is not an ancestor of report HEAD")
        name_status = _parse_name_status(
            _git(
                root,
                [
                    "diff",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--name-status",
                    "-z",
                    "--no-renames",
                    technical_base,
                    report_commit,
                ],
            )
        )
        expected_diff = sorted(("M", path) for path in STATUS_PATHS)
        if sorted(name_status) != expected_diff:
            raise ValueError(
                "cumulative technical-base-to-HEAD diff is not exactly three modified status paths"
            )
        name_only = [
            value.decode("utf-8")
            for value in _git(
                root,
                [
                    "diff",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--name-only",
                    "-z",
                    technical_base,
                    report_commit,
                ],
            ).rstrip(b"\0").split(b"\0")
            if value
        ]
        if sorted(name_only) != sorted(STATUS_PATHS):
            raise ValueError("Git name-only diff is not exactly the three status paths")
        for relative in STATUS_PATHS:
            if (
                _git_blob_record(root, technical_base, relative).get("git_mode")
                != "100644"
                or _git_blob_record(root, report_commit, relative).get("git_mode")
                != "100644"
            ):
                raise ValueError(f"status path is not a canonical 100644 Git blob: {relative}")

        plan_payload = _secure_read_relative(root, PLAN_PATH, maximum_bytes=MAX_STATUS_BYTES)
        plan_sha256 = _sha256(plan_payload)
        plan_at_base = _git(root, ["show", f"{technical_base}:{PLAN_PATH}"])
        plan_at_execution = _git(root, ["show", f"{execution_commit}:{PLAN_PATH}"])
        if plan_payload != plan_at_base or plan_payload != plan_at_execution:
            raise ValueError("plan bytes changed across execution/base/report identities")

        metadata_version = metadata.get("schema_version")
        metadata_contract = metadata.get("contract")
        if (
            metadata_version in {3, 4, 5}
            and metadata_contract == STATUS_METADATA_CONTRACTS[metadata_version]
        ):
            local, receipt_records, _state = _validate_live_component_version(
                root,
                texts,
                metadata,
                version=metadata_version,
                report_commit=report_commit,
                plan_payload=plan_payload,
                plan_sha256=plan_sha256,
            )
            failures.extend(local)
            receipt_path = receipt_records[-1][0] if receipt_records else None
        elif (
            metadata.get("schema_version") == 2
            and metadata.get("contract") == STATUS_METADATA_CONTRACT_V2
        ):
            local, receipt_records, _state = _validate_live_v2(
                root,
                texts,
                metadata,
                report_commit=report_commit,
                plan_payload=plan_payload,
                plan_sha256=plan_sha256,
            )
            failures.extend(local)
            receipt_path = receipt_records[-1][0] if receipt_records else None
        elif (
            metadata.get("schema_version") == 1
            and metadata.get("contract") == STATUS_METADATA_CONTRACT
        ):
            evidence = metadata.get("evidence")
            receipt_path = (
                evidence.get("receipt_path") if isinstance(evidence, dict) else None
            )
            run_id = evidence.get("run_id") if isinstance(evidence, dict) else None
            canonical_receipt = (
                f"runs/{run_id}/metrics/{M0_RECEIPT_NAME}"
                if isinstance(run_id, str) and SAFE_RUN_ID.fullmatch(run_id)
                else None
            )
            if receipt_path != canonical_receipt:
                raise ValueError("metadata receipt path is not canonical for its safe run ID")
            receipt_payload = _secure_read_relative(
                root,
                str(receipt_path),
                maximum_bytes=MAX_RECEIPT_BYTES,
                require_read_only=True,
            )
            receipt = _strict_json(receipt_payload, "M0 host-final receipt")
            if not isinstance(receipt, dict):
                raise ValueError("M0 host-final receipt is not a JSON object")
            if receipt_payload != _canonical_pretty_json(receipt):
                raise ValueError("M0 host-final receipt bytes are not canonical")
            if evidence.get("receipt_sha256") != _sha256(receipt_payload):
                raise ValueError("metadata receipt hash does not match canonical receipt bytes")
            receipt_records = [(str(receipt_path), receipt_payload)]

            if str(root) not in sys.path:
                sys.path.insert(0, str(root))
            from network.validation.qualification_identity import (  # noqa: PLC0415
                qualification_content_vector,
            )

            expected_vector = qualification_content_vector(root, execution_commit)
            current_vector = qualification_content_vector(root, report_commit)
            base_vector = qualification_content_vector(root, technical_base)
            for comparison, label in (
                (current_vector, "report"),
                (base_vector, "technical base"),
            ):
                reduced = {
                    key: value for key, value in comparison.items() if key != "git_commit"
                }
                expected_reduced = {
                    key: value
                    for key, value in expected_vector.items()
                    if key != "git_commit"
                }
                if reduced != expected_reduced:
                    raise ValueError(
                        f"{label} qualification vector content differs from execution"
                    )
            failures.extend(
                _validate_receipt(
                    root,
                    receipt,
                    execution_commit=execution_commit,
                    expected_vector=expected_vector,
                    plan_sha256=plan_sha256,
                )
            )

            state = status_documents_status(
                texts[STATUS_PATHS[0]],
                texts[STATUS_PATHS[1]],
                texts[STATUS_PATHS[2]],
                m0_receipt=receipt,
            )
            failures.extend(state["failures"])
            failures.extend(
                _validate_metadata(
                    metadata,
                    root=root,
                    state=state,
                    report_commit=report_commit,
                    plan_sha256=plan_sha256,
                    receipt=receipt,
                )
            )
        else:
            raise ValueError("status metadata contract/version is unsupported or mismatched")
        final_head = _git(root, ["rev-parse", "HEAD"]).decode("ascii").strip()
        final_dirty = _git(
            root,
            [
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                "--ignore-submodules=none",
            ],
        )
        if final_head != report_commit or final_dirty:
            failures.append("Git HEAD/clean state changed during live-status lint")
        for relative, original_text in texts.items():
            final_payload = _secure_read_relative(
                root, relative, maximum_bytes=MAX_STATUS_BYTES
            )
            if final_payload != original_text.encode("utf-8"):
                failures.append(f"status document changed during lint: {relative}")
        if (
            _secure_read_relative(root, PLAN_PATH, maximum_bytes=MAX_STATUS_BYTES)
            != plan_payload
        ):
            failures.append("plan document changed during live-status lint")
        for stable_path, stable_payload in receipt_records:
            if (
                _secure_read_relative(
                    root,
                    stable_path,
                    maximum_bytes=MAX_RECEIPT_BYTES,
                    require_read_only=True,
                )
                != stable_payload
            ):
                failures.append(
                    f"cited host-final receipt changed during live-status lint: {stable_path}"
                )
    except Exception as exc:  # Every malformed/unexpected live input fails as JSON.
        failures.append(str(exc))

    return {
        "schema_version": 1,
        "contract": STATUS_LINT_CONTRACT,
        "passed": not failures,
        "failures": failures,
        "report_commit": report_commit,
        "technical_base_commit": metadata.get("technical_base_commit")
        if isinstance(metadata, dict)
        else None,
        "execution_commit": metadata.get("execution_commit")
        if isinstance(metadata, dict)
        else None,
        "receipt_path": receipt_path,
        "status_paths": list(STATUS_PATHS),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    result = validate_live_status(ROOT_DIR)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("passed") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
