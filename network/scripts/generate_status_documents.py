#!/usr/bin/env python3
"""Render one exact post-receipt status-only v1..v5 document set.

The command refuses a dirty checkout, a non-technical HEAD, mutable/noncanonical
receipts, and any receipt set other than the exact cumulative M0..M(N-1)
prefix.  It can render into a review directory or replace exactly the three
canonical mutable status paths when ``--write-canonical`` is explicit.
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
import tempfile
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from network.scripts.validate_status_documents import (  # noqa: E402
    M0_RECEIPT_NAME,
    M1_NEXT_COMMAND_ARGV,
    M1_RECEIPT_NAME,
    M2_BLOCKING_PREREQUISITES,
    PLAN_PATH,
    POLICY_PATH,
    STATUS_METADATA_CONTRACTS,
    STATUS_PATHS,
    _component_status_next_command,
    _component_status_next_sequence,
    _git_blob_record,
    canonical_status_metadata_block,
)
from network.validation.component_profiles import load_profiles  # noqa: E402


SHA1 = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")
SAFE_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
SAFE_RECEIPT_KEY = re.compile(r"m[0-4]")
MAX_RECEIPT_BYTES = 64 * 1024 * 1024


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["/usr/bin/git", *arguments],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env={
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
        },
    )
    if result.returncode != 0:
        raise ValueError(
            f"git {' '.join(arguments)} failed: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def parse_receipt_arguments(values: list[str], version: int) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        key, separator, raw_path = value.partition("=")
        if (
            not separator
            or SAFE_RECEIPT_KEY.fullmatch(key) is None
            or not raw_path
            or key in result
        ):
            raise ValueError("--receipt must be one unique mN=/canonical/path pair")
        result[key] = Path(raw_path)
    expected = {f"m{index}" for index in range(version)}
    if set(result) != expected:
        raise ValueError(
            f"status v{version} requires exactly receipts {sorted(expected)}"
        )
    return result


def receipt_name(index: int, profiles: dict[str, dict[str, Any]]) -> str:
    if index == 0:
        return M0_RECEIPT_NAME
    if index == 1:
        return M1_RECEIPT_NAME
    return str(profiles[f"m{index}_component"]["receipt_name"])


def read_receipts(
    root: Path,
    paths: dict[str, Path],
    profiles: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, bytes]]:
    receipts: dict[str, dict[str, Any]] = {}
    payloads: dict[str, bytes] = {}
    for key, supplied in sorted(paths.items()):
        index = int(key[1:])
        path = supplied if supplied.is_absolute() else root / supplied
        if path.is_symlink():
            raise ValueError(f"{key} receipt may not be a symlink")
        info = path.stat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_mode & 0o222
            or info.st_size < 2
            or info.st_size > MAX_RECEIPT_BYTES
        ):
            raise ValueError(f"{key} receipt is not one bounded read-only file")
        payload = path.read_bytes()
        try:
            receipt = json.loads(
                payload.decode("utf-8"),
                parse_constant=lambda token: (_ for _ in ()).throw(
                    ValueError(f"non-finite JSON: {token}")
                ),
                object_pairs_hook=lambda pairs: _unique_pairs(pairs),
            )
        except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"{key} receipt is invalid JSON: {exc}") from exc
        if not isinstance(receipt, dict) or payload != (
            json.dumps(receipt, allow_nan=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8"):
            raise ValueError(f"{key} receipt bytes are not canonical pretty JSON")
        run_id = receipt.get("run_id")
        if not isinstance(run_id, str) or SAFE_RUN_ID.fullmatch(run_id) is None:
            raise ValueError(f"{key} receipt run_id is unsafe")
        expected_relative = f"runs/{run_id}/metrics/{receipt_name(index, profiles)}"
        expected_path = (root / expected_relative).resolve(strict=True)
        if path.resolve(strict=True) != expected_path:
            raise ValueError(f"{key} receipt is not at {expected_relative}")
        expected_contract = (
            "ams.m0.host-final-receipt/v1"
            if index == 0
            else "ams.m1.host-final-receipt/v1"
            if index == 1
            else profiles[f"m{index}_component"]["receipt_contract"]
        )
        expected_nodes = [f"Q{value}" for value in range(index + 1)]
        identity_valid = (
            receipt.get("schema_version") == (3 if index == 0 else 1)
            and receipt.get("contract") == expected_contract
            and receipt.get("consumed_nodes") == expected_nodes
            and (
                receipt.get("milestone") == f"M{index}"
                if index < 2
                else receipt.get("profile") == f"m{index}_component"
            )
        )
        if (
            not identity_valid
            or receipt.get("receipt_path") != expected_relative
            or receipt.get("formal_accepted") is not True
            or receipt.get("passed") is not True
            or receipt.get("failures") != []
            or SHA256.fullmatch(
                str(receipt.get("qualification_contract_sha256") or "")
            )
            is None
        ):
            raise ValueError(f"{key} receipt lacks exact passing host-final authority")
        receipts[key] = receipt
        payloads[key] = payload
    return receipts, payloads


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key!r}")
        result[key] = value
    return result


def receipt_citation(
    key: str,
    receipt: dict[str, Any],
    payload: bytes,
    profiles: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    index = int(key[1:])
    return {
        "kind": (
            "m0_host_final_receipt"
            if index == 0
            else "m1_host_final_receipt"
            if index == 1
            else f"m{index}_host_final_receipt"
        ),
        "milestone": f"M{index}",
        "run_id": receipt["run_id"],
        "receipt_path": (
            f"runs/{receipt['run_id']}/metrics/{receipt_name(index, profiles)}"
        ),
        "receipt_sha256": sha256(payload),
        "qualification_contract_sha256": receipt["qualification_contract_sha256"],
    }


def latest_source_commit(version: int, receipts: dict[str, dict[str, Any]]) -> str:
    latest = receipts[f"m{version - 1}"]
    if version == 1:
        vector = latest.get("qualification_content_vector")
        value = vector.get("git_commit") if isinstance(vector, dict) else None
    else:
        value = latest.get("source_commit")
    if not isinstance(value, str) or SHA1.fullmatch(value) is None:
        raise ValueError("latest receipt source commit is not exact")
    return value


def build_metadata(
    root: Path,
    version: int,
    receipts: dict[str, dict[str, Any]],
    payloads: dict[str, bytes],
    profiles: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    technical_base = latest_source_commit(version, receipts)
    head = git(root, "rev-parse", "HEAD")
    if head != technical_base:
        raise ValueError(
            f"HEAD {head} is not latest receipt technical source {technical_base}"
        )
    if git(root, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("technical checkout must be clean before status rendering")
    plan_payload = (root / PLAN_PATH).read_bytes()
    plan_at_base = subprocess.run(
        ["/usr/bin/git", "show", f"{technical_base}:{PLAN_PATH}"],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout
    if plan_payload != plan_at_base:
        raise ValueError("authoritative plan differs from latest technical source")

    latest = receipts[f"m{version - 1}"]
    vector = latest.get("qualification_content_vector")
    if not isinstance(vector, dict):
        raise ValueError("latest receipt qualification vector is missing")
    expected_nodes = [f"Q{index}" for index in range(version)]
    if latest.get("consumed_nodes") != expected_nodes:
        raise ValueError("latest receipt consumed-node prefix is not exact")

    citations = {
        key: receipt_citation(key, receipts[key], payloads[key], profiles)
        for key in sorted(receipts)
    }
    evidence: Any = citations["m0"] if version == 1 else citations
    next_sequence: dict[str, Any] | None = None
    if version == 1:
        argv = list(M1_NEXT_COMMAND_ARGV)
        next_command = {
            "milestone": "M1",
            "argv": argv,
            "argv_sha256": sha256(canonical(argv)),
            "tracked_inputs": [
                _git_blob_record(root, technical_base, path)
                for path in (
                    "network/scripts/run_five_uav_health.sh",
                    "scripts/run_acceptance_container.sh",
                )
            ],
        }
    elif version == 2:
        next_command = {
            "milestone": "M2",
            "eligible": False,
            "blocking_prerequisites": list(M2_BLOCKING_PREREQUISITES),
            "argv": [],
            "argv_sha256": sha256(canonical([])),
            "tracked_inputs": [],
        }
    else:
        next_command = _component_status_next_command(
            version,
            root=root,
            technical_base=technical_base,
            profiles=profiles,
        )
    if version in {2, 4}:
        next_sequence = _component_status_next_sequence(
            version,
            root=root,
            technical_base=technical_base,
            profiles=profiles,
        )
    metadata = {
        "schema_version": version,
        "contract": STATUS_METADATA_CONTRACTS[version],
        "plan_contract": {"path": PLAN_PATH, "sha256": sha256(plan_payload)},
        "technical_base_commit": technical_base,
        "execution_commit": technical_base,
        "evidence": evidence,
        "qualification": {
            "policy_id": vector.get("policy_id"),
            "policy_path": POLICY_PATH,
            "policy_sha256": vector.get("policy_sha256"),
            "vector_commit": vector.get("git_commit"),
            "vector_sha256": vector.get("vector_sha256"),
            "consumed_nodes": expected_nodes,
        },
        "state": {
            "active_milestone": f"M{version}",
            "customer_ready": False,
            "fully_closed_sequential_milestones": version,
        },
        "next_command": next_command,
    }
    if next_sequence is not None:
        metadata["next_sequence"] = next_sequence
    return metadata


def render_documents(version: int, metadata: dict[str, Any]) -> dict[str, str]:
    rows = "".join(
        f"| M{index} | `{'passed' if index < version else 'not_started'}` | "
        f"{'Closed by cited immutable host-final receipt.' if index < version else 'Sequentially pending.'} |\n"
        for index in range(9)
    )
    table = (
        "| Milestone | Formal status | Authority |\n"
        "| --- | --- | --- |\n"
        + rows
    )
    summary = (
        "Customer-ready: **false**.\n\n"
        f"Fully closed sequential milestones: **{version}**.\n\n"
        f"Active milestone: **M{version}**.\n"
    )
    block = canonical_status_metadata_block(metadata)
    next_instruction = (
        "Follow the canonical ordered `next_sequence`; it supersedes the "
        "single-step `next_command` and its resume policy forbids rerunning a "
        "successful auxiliary step."
        if "next_sequence" in metadata
        else "Execute only the canonical `next_command` encoded below."
    )
    return {
        "network/PROGRESS.md": (
            "# Network/Radio Progress\n\n"
            f"Authoritative contract: `{PLAN_PATH}`.\n\n"
            + summary
            + "\n"
            + table
            + "\nOnly `passed` closes a milestone.\n\n"
            + block
            + "\n"
        ),
        "network/VALIDATION_REPORT.md": (
            "# Network/Radio Validation Report\n\n"
            f"Authoritative contract: `{PLAN_PATH}`.\n\n"
            + summary
            + "\n"
            + table
            + "\nEvery passed row is bound to the shared metadata below.\n\n"
            + block
            + "\n"
        ),
        "network/NEXT_TASK.md": (
            "# Next Task\n\n"
            f"Authoritative contract: `{PLAN_PATH}`.\n\n"
            + summary
            + f"\n{next_instruction}\n\n"
            + block
            + "\n"
        ),
    }


def write_documents(output_root: Path, documents: dict[str, str]) -> None:
    for relative, text in documents.items():
        path = output_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=f".{path.name}.",
            dir=path.parent,
            delete=False,
        )
        temp_path = Path(temporary.name)
        try:
            with temporary:
                temporary.write(text)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.chmod(temp_path, 0o644, follow_symlinks=False)
            os.replace(temp_path, path)
        finally:
            temp_path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", type=int, choices=range(1, 6), required=True)
    parser.add_argument("--receipt", action="append", default=[])
    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument("--output-dir", type=Path)
    destination.add_argument("--write-canonical", action="store_true")
    args = parser.parse_args(argv)
    try:
        root = ROOT_DIR.resolve(strict=True)
        paths = parse_receipt_arguments(args.receipt, args.version)
        profiles = load_profiles()
        receipts, payloads = read_receipts(root, paths, profiles)
        metadata = build_metadata(
            root, args.version, receipts, payloads, profiles
        )
        documents = render_documents(args.version, metadata)
        output_root = root if args.write_canonical else args.output_dir.resolve()
        if output_root == root and not args.write_canonical:
            raise ValueError("canonical writes require --write-canonical")
        write_documents(output_root, documents)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"FAIL status document generation: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "schema_version": args.version,
                "contract": STATUS_METADATA_CONTRACTS[args.version],
                "output_root": str(output_root),
                "written_paths": list(STATUS_PATHS),
                "technical_base_commit": metadata["technical_base_commit"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
