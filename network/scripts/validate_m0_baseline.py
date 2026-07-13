#!/usr/bin/env python3
"""Validate only the M0 dependency/provenance qualification probe.

This validator deliberately makes no packet-path, sealing, attestation, or P0
claim.  Full acceptance remains the responsibility of the normal evidence
validator and host-side attestation workflow.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[2]
SAFE_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
MAX_EXIT_CODE_BYTES = 32
MAX_LOG_BYTES = 4 * 1024 * 1024
MAX_PROVENANCE_BYTES = 32 * 1024 * 1024

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from network.validation.evidence import provenance_status  # noqa: E402


def gate(status: str, proof: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"status": status, "proof": proof}
    if details is not None:
        result["details"] = details
    return result


def _safe_run_directory(run_dir: Path) -> tuple[Path | None, str | None]:
    """Resolve one direct, non-symlink child of this checkout's runs directory."""

    run_root = ROOT_DIR / "runs"
    try:
        if run_root.is_symlink():
            return None, "runs directory is a symbolic link"
        resolved_root = run_root.resolve(strict=True)
        candidate = run_dir if run_dir.is_absolute() else Path.cwd() / run_dir
        if candidate.is_symlink():
            return None, "run directory is a symbolic link"
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        return None, f"run directory is missing or invalid: {exc}"
    if not resolved.is_dir():
        return None, "run path is not a directory"
    if resolved.parent != resolved_root:
        return None, "run directory must be a direct child of this checkout's runs directory"
    if SAFE_RUN_ID.fullmatch(resolved.name) is None:
        return None, "run directory name is not a safe RUN_ID"
    return resolved, None


def _regular_file(path: Path, *, maximum_bytes: int) -> tuple[os.stat_result | None, str | None]:
    try:
        file_stat = path.lstat()
    except OSError as exc:
        return None, f"{path.name} is missing or unreadable: {exc}"
    if not stat.S_ISREG(file_stat.st_mode):
        return None, f"{path.name} is not a regular file"
    if file_stat.st_nlink != 1:
        return None, f"{path.name} must have exactly one hard link"
    if file_stat.st_size < 1:
        return None, f"{path.name} is empty"
    if file_stat.st_size > maximum_bytes:
        return None, f"{path.name} exceeds the probe size limit"
    return file_stat, None


def _read_text(path: Path, *, maximum_bytes: int) -> tuple[str | None, str | None]:
    _, error = _regular_file(path, maximum_bytes=maximum_bytes)
    if error:
        return None, error
    try:
        return path.read_text(encoding="utf-8"), None
    except (OSError, UnicodeError) as exc:
        return None, f"{path.name} is not readable UTF-8: {exc}"


def _producer_exit_code(path: Path) -> tuple[int | None, str | None]:
    text, error = _read_text(path, maximum_bytes=MAX_EXIT_CODE_BYTES)
    if error:
        return None, error
    if text is None or re.fullmatch(r"(?:0|[1-9][0-9]*)\n?", text) is None:
        return None, f"{path.name} is not a canonical non-negative exit code"
    return int(text.strip()), None


def dependency_gate(run_dir: Path) -> dict[str, Any]:
    exit_path = run_dir / "logs/check_deps.log.exit_code"
    log_path = run_dir / "logs/check_deps.log"
    exit_code, exit_error = _producer_exit_code(exit_path)
    log_text, log_error = _read_text(log_path, maximum_bytes=MAX_LOG_BYTES)
    failures = [error for error in (exit_error, log_error) if error]
    if exit_code is not None and exit_code != 0:
        failures.append(f"check_deps exited with {exit_code}")
    if log_text is not None and re.search(
        r"^Dependency check passed with [0-9]+ warning\(s\)\.$",
        log_text,
        flags=re.MULTILINE,
    ) is None:
        failures.append("check_deps log lacks its successful completion record")
    if failures:
        return gate("failed", "dependency qualification did not pass", {"failures": failures})
    return gate(
        "passed",
        "check_deps exited zero and recorded successful completion",
        {"exit_code": 0},
    )


def provenance_gate(run_dir: Path) -> dict[str, Any]:
    failures: list[str] = []
    producer_exit, exit_error = _producer_exit_code(
        run_dir / "logs/provenance.log.exit_code"
    )
    _, log_error = _read_text(
        run_dir / "logs/provenance.log", maximum_bytes=MAX_LOG_BYTES
    )
    _, record_error = _regular_file(
        run_dir / "metrics/provenance.json", maximum_bytes=MAX_PROVENANCE_BYTES
    )
    failures.extend(error for error in (exit_error, log_error, record_error) if error)
    if producer_exit is not None and producer_exit != 0:
        failures.append(f"write_run_provenance exited with {producer_exit}")

    try:
        independent = provenance_status(run_dir)
    except Exception as exc:  # Fail closed on any independent-validator defect.
        independent = gate("failed", "provenance_status raised an exception")
        failures.append(f"independent provenance validation failed: {exc}")
    if not isinstance(independent, dict) or independent.get("status") != "passed":
        failures.append("provenance_status did not pass")
    if failures:
        return gate(
            "failed",
            "exact-image provenance qualification did not pass",
            {"failures": failures, "provenance_status": independent},
        )
    return gate(
        "passed",
        "existing provenance_status independently accepted source, config, dependency, and container identity",
        {"provenance_status": independent},
    )


def evaluate_m0_baseline(run_dir: Path) -> dict[str, Any]:
    safe_run_dir, input_error = _safe_run_directory(run_dir)
    if input_error or safe_run_dir is None:
        input_failure = input_error or "run directory validation failed"
        gates = {
            "dependency_check": gate("failed", "unsafe probe input", {"failures": [input_failure]}),
            "provenance": gate("failed", "unsafe probe input", {"failures": [input_failure]}),
        }
        run_id = run_dir.name
        resolved_text = str(run_dir)
    else:
        gates = {
            "dependency_check": dependency_gate(safe_run_dir),
            "provenance": provenance_gate(safe_run_dir),
        }
        run_id = safe_run_dir.name
        resolved_text = str(safe_run_dir)

    failures = [
        f"{name}: {record.get('proof', 'gate failed')}"
        for name, record in gates.items()
        if record.get("status") != "passed"
    ]
    return {
        "schema_version": 1,
        "probe": "m0_dependency_provenance",
        "milestone": "M0",
        "run_id": run_id,
        "run_dir": resolved_text,
        "scope": {
            "dependency_check": True,
            "provenance": True,
            "packet_path": False,
            "sealing": False,
            "attestation": False,
        },
        "p0_eligible": False,
        "passed": not failures,
        "failures": failures,
        "gates": gates,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = evaluate_m0_baseline(args.run_dir)
    except Exception as exc:  # Preserve a machine-readable fail-closed result.
        result = {
            "schema_version": 1,
            "probe": "m0_dependency_provenance",
            "milestone": "M0",
            "run_id": args.run_dir.name,
            "run_dir": str(args.run_dir),
            "scope": {
                "dependency_check": True,
                "provenance": True,
                "packet_path": False,
                "sealing": False,
                "attestation": False,
            },
            "p0_eligible": False,
            "passed": False,
            "failures": [f"validator exception: {exc}"],
            "gates": {},
        }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("passed") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
