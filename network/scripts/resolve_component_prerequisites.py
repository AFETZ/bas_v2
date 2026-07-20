#!/usr/bin/python3.10
"""Resolve already linted live-status receipts for a component acceptance run."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
from pathlib import Path, PurePosixPath
from typing import Any

from network.validation.component_profiles import load_profiles


STATUS_PATHS = (
    "network/PROGRESS.md",
    "network/VALIDATION_REPORT.md",
    "network/NEXT_TASK.md",
)
BEGIN = "<!-- AMS_LIVE_STATUS_METADATA_BEGIN\n"
END = "\nAMS_LIVE_STATUS_METADATA_END -->"
SHA1 = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")
SAFE_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def strict_json(payload: bytes, label: str) -> Any:
    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=unique_pairs,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON: {token}")
            ),
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"{label} is not strict JSON: {exc}") from exc


def read_regular(path: Path, *, maximum: int, read_only: bool = False) -> bytes:
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_size < 2
        or info.st_size > maximum
        or (read_only and info.st_mode & 0o222)
    ):
        raise ValueError(f"not one bounded{' read-only' if read_only else ''} file: {path}")
    payload = path.read_bytes()
    after = path.lstat()
    if (
        len(payload) != info.st_size
        or (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_mode, after.st_size, after.st_mtime_ns)
    ):
        raise ValueError(f"file changed while read: {path}")
    return payload


def metadata_from_text(payload: bytes, label: str) -> dict[str, Any]:
    text = payload.decode("utf-8")
    if text.count(BEGIN) != 1 or text.count(END) != 1:
        raise ValueError(f"{label} does not contain one status metadata block")
    encoded = text.split(BEGIN, 1)[1].split(END, 1)[0].encode("utf-8")
    value = strict_json(encoded, f"{label} metadata")
    if not isinstance(value, dict):
        raise ValueError(f"{label} metadata is not an object")
    return value


def safe_receipt(root: Path, citation: dict[str, Any], milestone: str) -> dict[str, Any]:
    expected_keys = {
        "kind",
        "milestone",
        "run_id",
        "receipt_path",
        "receipt_sha256",
        "qualification_contract_sha256",
    }
    if not isinstance(citation, dict) or not expected_keys.issubset(citation):
        raise ValueError(f"{milestone} citation is incomplete")
    run_id = citation.get("run_id")
    relative = citation.get("receipt_path")
    if (
        citation.get("milestone") != milestone
        or not isinstance(run_id, str)
        or SAFE_RUN_ID.fullmatch(run_id) is None
        or not isinstance(relative, str)
        or PurePosixPath(relative).is_absolute()
        or any(part in {"", ".", ".."} for part in PurePosixPath(relative).parts)
        or not relative.startswith(f"runs/{run_id}/metrics/")
        or not relative.endswith("_host_final_receipt.json")
    ):
        raise ValueError(f"{milestone} receipt path/run identity is invalid")
    path = root / relative
    payload = read_regular(path, maximum=64 * 1024 * 1024, read_only=True)
    digest = hashlib.sha256(payload).hexdigest()
    receipt = strict_json(payload, f"{milestone} receipt")
    if (
        not isinstance(receipt, dict)
        or citation.get("receipt_sha256") != digest
        or receipt.get("receipt_path") != relative
        or receipt.get("run_id") != run_id
        or receipt.get("passed") is not True
        or receipt.get("formal_accepted") is not True
    ):
        raise ValueError(f"{milestone} receipt bytes/claim are invalid")
    return {
        "milestone": milestone,
        "canonical_path": relative,
        "host_path": str(path.resolve(strict=True)),
        "sha256": digest,
        "contract": receipt.get("contract"),
        "run_id": run_id,
    }


def required_component_receipt(
    root: Path,
    *,
    profile_name: str,
    profile: dict[str, Any],
    source_commit: str,
) -> dict[str, Any]:
    receipt_name = profile["receipt_name"]
    candidates: list[dict[str, Any]] = []
    runs_root = root / "runs"
    if runs_root.is_symlink() or not runs_root.is_dir():
        raise ValueError("canonical runs root is unavailable")
    for path in sorted(runs_root.glob(f"*/metrics/{receipt_name}")):
        run_dir = path.parent.parent
        if (
            run_dir.is_symlink()
            or path.parent.is_symlink()
            or SAFE_RUN_ID.fullmatch(run_dir.name) is None
        ):
            raise ValueError(f"unsafe required component receipt path: {path}")
        payload = read_regular(
            path, maximum=64 * 1024 * 1024, read_only=True
        )
        receipt = strict_json(payload, f"{profile_name} component receipt")
        canonical_path = f"runs/{run_dir.name}/metrics/{receipt_name}"
        if not isinstance(receipt, dict):
            raise ValueError(f"{profile_name} component receipt is not an object")
        if receipt.get("source_commit") != source_commit:
            continue
        if (
            payload
            != json.dumps(
                receipt, allow_nan=False, indent=2, sort_keys=True
            ).encode("utf-8")
            + b"\n"
            or receipt.get("schema_version") != 1
            or receipt.get("contract") != profile["receipt_contract"]
            or receipt.get("profile") != profile_name
            or receipt.get("run_id") != run_dir.name
            or receipt.get("receipt_path") != canonical_path
            or receipt.get("consumed_nodes") != profile["consumed_nodes"]
            or receipt.get("result_contract") != profile["result_contract"]
            or receipt.get("formal_accepted") is not True
            or receipt.get("passed") is not True
            or receipt.get("failures") != []
        ):
            raise ValueError(
                f"{profile_name} current component receipt authority is invalid"
            )
        candidates.append(
            {
                "profile": profile_name,
                "canonical_path": canonical_path,
                "host_path": str(path.resolve(strict=True)),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "contract": receipt["contract"],
                "run_id": run_dir.name,
            }
        )
    if len(candidates) != 1:
        raise ValueError(
            f"required component profile {profile_name} has {len(candidates)} "
            "current immutable receipts; exactly one is required"
        )
    return candidates[0]


def resolve(root: Path, profile_name: str, status_result_path: Path) -> dict[str, Any]:
    root = root.resolve(strict=True)
    profiles = load_profiles(root / "network/config/component_acceptance_profiles.json")
    if profile_name not in profiles:
        raise ValueError("unknown component profile")
    profile = profiles[profile_name]
    source_commit = os.environ.get("AMS_COMPONENT_SOURCE_COMMIT")
    if not isinstance(source_commit, str) or SHA1.fullmatch(source_commit) is None:
        raise ValueError("AMS_COMPONENT_SOURCE_COMMIT is not exact")
    status_payload = read_regular(status_result_path, maximum=4 * 1024 * 1024)
    status = strict_json(status_payload, "live-status lint result")
    if (
        not isinstance(status, dict)
        or status.get("schema_version") != 1
        or status.get("contract") != "ams.live-status-lint/v1"
        or status.get("passed") is not True
        or status.get("failures") != []
        or status.get("report_commit") != source_commit
        or status.get("status_paths") != list(STATUS_PATHS)
    ):
        raise ValueError("live-status lint result is not passing/current/exact")
    metadata_values = [
        metadata_from_text(
            read_regular(root / relative, maximum=4 * 1024 * 1024), relative
        )
        for relative in STATUS_PATHS
    ]
    if any(value != metadata_values[0] for value in metadata_values[1:]):
        raise ValueError("live status metadata differs between canonical documents")
    metadata = metadata_values[0]
    count = profile["prerequisite_status_count"]
    expected_contract = profile["prerequisite_status_contract"]
    state = metadata.get("state")
    if (
        metadata.get("contract") != expected_contract
        or metadata.get("schema_version") != count
        or not isinstance(state, dict)
        or state.get("fully_closed_sequential_milestones") != count
        or state.get("customer_ready") is not False
    ):
        raise ValueError("status metadata does not authorize the component profile")
    evidence = metadata.get("evidence")
    expected_milestones = [f"M{index}" for index in range(count)]
    expected_keys = {milestone.lower() for milestone in expected_milestones}
    if not isinstance(evidence, dict) or set(evidence) != expected_keys:
        raise ValueError("status evidence map does not exactly cover prerequisite milestones")
    receipts = {
        milestone.lower(): safe_receipt(root, evidence[milestone.lower()], milestone)
        for milestone in expected_milestones
    }
    component_receipts = {
        required_name: required_component_receipt(
            root,
            profile_name=required_name,
            profile=profiles[required_name],
            source_commit=source_commit,
        )
        for required_name in profile["required_component_profiles"]
    }
    return {
        "schema_version": 1,
        "contract": "ams.component-prerequisites/v1",
        "profile": profile_name,
        "source_commit": source_commit,
        "status": {
            "contract": expected_contract,
            "closed_count": count,
            "result_path": str(status_result_path.resolve(strict=True)),
            "result_sha256": hashlib.sha256(status_payload).hexdigest(),
            "report_commit": source_commit,
        },
        "receipts": receipts,
        "component_receipts": component_receipts,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--status-result", type=Path, required=True)
    args = parser.parse_args(argv)
    result = resolve(args.root, args.profile, args.status_result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
