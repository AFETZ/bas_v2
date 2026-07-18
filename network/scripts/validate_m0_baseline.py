#!/usr/bin/env python3
"""Validate only the M0 dependency/provenance qualification probe.

This validator deliberately makes no packet-path, sealing, attestation, or P0
claim.  Full acceptance remains the responsibility of the normal evidence
validator and host-side attestation workflow.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


ROOT_DIR = Path(__file__).resolve().parents[2]
SAFE_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
MAX_EXIT_CODE_BYTES = 32
MAX_LOG_BYTES = 4 * 1024 * 1024
MAX_PROVENANCE_BYTES = 32 * 1024 * 1024
MAX_TEST_RESULT_BYTES = 8 * 1024 * 1024
MAX_TEST_LOG_BYTES = 16 * 1024 * 1024
MAX_BUILD_LOG_BYTES = 32 * 1024 * 1024
MAX_RUNTIME_LOCK_BYTES = 128 * 1024
SHA256 = re.compile(r"[0-9a-f]{64}")
IMAGE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
UTC_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
CONTAINER_WORKDIR = "/workspace/multiagent_simulation"
M0_RUNNER_TOKEN = "network/scripts/run_m0_baseline.sh"
UTC_MONOTONIC_TOLERANCE_S = 2.0
EXPECTED_GPU_DEVICE_REQUESTS = [
    {
        "Driver": "",
        "Count": -1,
        "DeviceIDs": None,
        "Capabilities": [["compute", "utility", "gpu"]],
        "Options": {},
    }
]

# These IDs are an executable coverage contract, not a count heuristic.  Every
# class explicitly required by v3 M0 must remain present and pass both in the
# captured suite and in the independent host-final re-execution.
REQUIRED_M0_COVERAGE: dict[str, tuple[str, ...]] = {
    "historical_false_positive": (
        "test_validation_v2.ValidationV2Tests.test_historical_false_positive_run_is_rejected_when_available",
        "test_validation_v2.ValidationV2Tests.test_postprocess_does_not_fabricate_pcaps_or_active_proof",
    ),
    "arp_only": (
        "test_validation_v2.ValidationV2Tests.test_arp_only_pcap_fails_even_when_nonempty",
    ),
    "zero_rx_full_loss_null_metric": (
        "test_validation_v2.ValidationV2Tests.test_zero_rx_full_loss_and_null_latency_fail",
    ),
    "summary_boolean": (
        "test_validation_v2.ValidationV2Tests.test_self_reported_true_flags_do_not_pass_p0",
    ),
    "note_only_no_bypass": (
        "test_validation_v2.ValidationV2Tests.test_synthesized_no_bypass_text_is_ignored",
    ),
    "raw_mutation": (
        "test_evidence_attestation_v2.EvidenceAttestationV2Tests.test_manifest_listed_raw_mutation_fails_closed",
    ),
    "cross_run_substitution": (
        "test_m0_baseline_probe.M0BaselineProbeTests.test_cross_run_provenance_run_id_fails_closed",
    ),
    "signature_mismatch": (
        "test_evidence_attestation_v2.EvidenceAttestationV2Tests.test_manifest_or_signature_mutation_fails_closed",
    ),
    "ledger_mismatch": (
        "test_evidence_attestation_v2.EvidenceAttestationV2Tests.test_existing_outputs_and_external_ledger_forbid_resigning",
    ),
    "producer_pass": (
        "test_m0_baseline_probe.M0BaselineProbeTests.test_nonpassing_test_cannot_be_hidden_by_producer_pass",
    ),
}

# This is the complete ordered record emitted by check_deps.sh in the accepted
# exact container.  Checking every record and the terminal warning count makes
# a truncated log, an omitted check, or a forged final PASS insufficient.
EXPECTED_DEPENDENCY_RECORDS = (
    "config:service_tiers",
    "config:radio_24ghz",
    "config:jammers",
    "config:hitl_loopback",
    "config:validation_matrix",
    "config:metrics_schema",
    "component:live_sinr_monitor",
    "component:position_tracker",
    "component:ns3_core",
    "component:bridge",
    "component:hitl",
    "cmd:sionna_provider",
    "cmd:live_sinr_demo",
    "cmd:radio_heatmaps",
    "cmd:position_tracker",
    "cmd:hitl_loopback",
    "cmd:validation",
    "cmd:artifact_collection",
    "cmd:bash",
    "cmd:python3",
    "cmd:ip",
    "cmd:bridge",
    "cmd:ss",
    "cmd:nft",
    "cmd:iptables-save",
    "cmd:ip6tables-save",
    "cmd:unshare",
    "cmd:tc",
    "cmd:tcpdump",
    "cmd:ros2",
    "cmd:colcon",
    "cmd:gz",
    "bridge:priority_udp",
    "docker:runtime",
    "netns:privilege",
    "cuda:gpu",
    "ns3:launcher",
    "python:PyYAML",
    "python:NumPy",
    "python:matplotlib",
    "python:pymavlink",
    "python:SionnaRT",
    "lock.read",
    "lock.schema_version",
    "lock.ros_distribution",
    "lock.mitsuba_variant",
    "lock.no_sionna_meta_or_tensorflow",
    "lock.pin.numpy",
    "lock.pin.sionna-rt",
    "lock.pin.mitsuba",
    "lock.numpy_ros_humble_abi",
    "import.numpy",
    "import.cv2",
    "import.cv_bridge",
    "import.sionna.rt",
    "import.mitsuba",
    "import.mpl_toolkits.mplot3d",
    "version.numpy",
    "version.sionna-rt",
    "version.mitsuba",
    "version.numpy_module",
    "runtime.mitsuba_variant",
    "runtime.no_tensorflow",
    "python:runtime_compat",
    "ros2:ardupilot_sitl",
    "ros2:ros_gz_sim",
    "ros2:ros_gz_bridge",
    "ros2:ros_gz_image",
)
DEPENDENCY_STATUS_LINE = re.compile(r"^(PASS|WARN|FAIL) +([^ ]+) +(.+)$")
TEST_SUCCESS_LINE = re.compile(
    r"^(test_[A-Za-z0-9_]+) \(([A-Za-z0-9_.]+)\) \.\.\. ok$"
)
OUTCOME_NAMES = (
    "passed",
    "failed",
    "error",
    "skipped",
    "expected_failure",
    "unexpected_success",
    "not_completed",
)

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from network.validation.evidence import provenance_status  # noqa: E402
from network.scripts.run_m0_validation_suite import suite_source_bindings  # noqa: E402
from network.scripts.run_m0_validation_suite import (  # noqa: E402
    M0_TEST_MANIFEST_PATH,
    MUTABLE_STATUS_OUTPUTS,
    expected_m0_sys_path,
    load_frozen_test_manifest,
    load_m0_import_policy,
    qualification_content_vector,
    suite_external_bindings,
    validate_m0_import_trace_record,
)
from network.validation.qualification_identity import (  # noqa: E402
    qualification_consumption,
)
from network.scripts.qualification_suite import (  # noqa: E402
    discover_owned_test_suite,
)
from network.scripts.host_finalization_common import (  # noqa: E402
    M0_CAPABILITY_COMMAND_SCRIPT,
    M0_CAPABILITY_STDOUT,
)


def gate(status: str, proof: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"status": status, "proof": proof}
    if details is not None:
        result["details"] = details
    return result


def _inside_docker_runtime() -> bool:
    """Return the host-final trust-boundary marker through a testable seam."""

    return Path("/.dockerenv").exists()


def _m0_run_root() -> Path:
    value = os.environ.get("AMS_M0_ARTIFACT_ROOT")
    return Path(value) if value else ROOT_DIR / "runs"


def _staging_root_for(candidate: Path) -> Path | None:
    parent = candidate.parent
    expected_prefix = f".ams-m0-artifacts-{candidate.name}."
    try:
        workspace_parent = ROOT_DIR.parent.resolve(strict=True)
        if (
            parent.parent.resolve(strict=True) == workspace_parent
            and parent.name.startswith(expected_prefix)
            and re.fullmatch(
                rf"\.ams-m0-artifacts-{re.escape(candidate.name)}\.[A-Za-z0-9]{{8,}}",
                parent.name,
            )
        ):
            return parent
    except (OSError, RuntimeError):
        pass
    return None


def _safe_run_directory(
    run_dir: Path, *, allow_staging: bool = False
) -> tuple[Path | None, str | None]:
    """Resolve one direct, non-symlink run child at an accepted trust boundary."""

    candidate = run_dir if run_dir.is_absolute() else Path.cwd() / run_dir
    normalized = Path(os.path.normpath(str(candidate)))
    if candidate != normalized:
        return None, "run directory path is not canonical"
    run_root = _m0_run_root()
    if allow_staging and _staging_root_for(candidate) is not None:
        run_root = candidate.parent
    try:
        root_info = run_root.lstat()
        if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode):
            return None, "runs path is not a canonical non-symlink directory"
        resolved_root = run_root.resolve(strict=True)
        if candidate != normalized or candidate.parent != run_root:
            return None, "run directory path is not the canonical direct runs child"
        candidate_info = candidate.lstat()
        if not stat.S_ISDIR(candidate_info.st_mode) or stat.S_ISLNK(
            candidate_info.st_mode
        ):
            return None, "run directory is not a canonical non-symlink directory"
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        return None, f"run directory is missing or invalid: {exc}"
    if not resolved.is_dir():
        return None, "run path is not a directory"
    if resolved.parent != resolved_root:
        return None, "run directory must be a direct child of this checkout's runs directory"
    if SAFE_RUN_ID.fullmatch(resolved.name) is None:
        return None, "run directory name is not a safe RUN_ID"
    for name in ("logs", "metrics"):
        directory = resolved / name
        try:
            info = directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                return None, f"{name} is not a canonical non-symlink directory"
            if directory.resolve(strict=True).parent != resolved:
                return None, f"{name} directory escapes the run directory"
        except (OSError, RuntimeError) as exc:
            return None, f"{name} directory is missing or invalid: {exc}"
    return resolved, None


def _path_has_symlink_component(
    path: Path, *, additional_root: Path | None = None
) -> str | None:
    try:
        normalized = Path(os.path.normpath(str(path)))
        if path != normalized:
            return f"{path.name} path is not canonical"
        allowed_roots = [ROOT_DIR.resolve(strict=True), _m0_run_root().resolve(strict=True)]
        if additional_root is not None:
            allowed_roots.append(additional_root.resolve(strict=True))
        for possible_run in (path, *path.parents):
            candidate_staging = _staging_root_for(possible_run)
            if candidate_staging is not None:
                allowed_roots.append(candidate_staging.resolve(strict=True))
                break
        root = next(
            (candidate for candidate in allowed_roots if path.is_relative_to(candidate)),
            None,
        )
        if root is None:
            return f"{path.name} path is outside the accepted source/artifact roots"
        relative = path.relative_to(root)
        current = root
        for part in relative.parts:
            current = current / part
            component = current.lstat()
            if stat.S_ISLNK(component.st_mode):
                return f"{path.name} path has a symbolic-link component"
    except (OSError, RuntimeError, ValueError) as exc:
        return f"{path.name} is missing or unreadable: {exc}"
    return None


def _read_bytes_fd(
    path: Path,
    *,
    maximum_bytes: int,
    allow_empty: bool = False,
    additional_root: Path | None = None,
) -> tuple[bytes | None, os.stat_result | None, str | None]:
    component_error = _path_has_symlink_component(path, additional_root=additional_root)
    if component_error:
        return None, None, component_error
    try:
        roots = [ROOT_DIR.resolve(strict=True), _m0_run_root().resolve(strict=True)]
        if additional_root is not None:
            roots.append(additional_root.resolve(strict=True))
        for possible_run in (path, *path.parents):
            staging = _staging_root_for(possible_run)
            if staging is not None:
                roots.append(staging.resolve(strict=True))
                break
        trusted_root = next(root for root in roots if path.is_relative_to(root))
        relative = path.relative_to(trusted_root)
        if not relative.parts:
            return None, None, f"{path.name} does not identify a leaf file"
    except (OSError, RuntimeError, StopIteration, ValueError) as exc:
        return None, None, f"{path.name} has no trusted dirfd root: {exc}"
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_flags = flags | getattr(os, "O_DIRECTORY", 0)
    root_descriptor = -1
    parent_descriptors: list[int] = []
    try:
        root_descriptor = os.open(trusted_root, directory_flags)
        root_before = os.fstat(root_descriptor)
        parent = root_descriptor
        for part in relative.parts[:-1]:
            child = os.open(part, directory_flags, dir_fd=parent)
            parent_descriptors.append(child)
            parent = child
        descriptor = os.open(relative.parts[-1], flags, dir_fd=parent)
    except OSError as exc:
        for opened in reversed(parent_descriptors):
            os.close(opened)
        if root_descriptor >= 0:
            os.close(root_descriptor)
        return None, None, f"{path.name} is missing or unreadable: {exc}"
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            return None, None, f"{path.name} is not a regular file"
        if before.st_nlink != 1:
            return None, None, f"{path.name} must have exactly one hard link"
        if before.st_size < (0 if allow_empty else 1):
            return None, None, f"{path.name} is empty"
        if before.st_size > maximum_bytes:
            return None, None, f"{path.name} exceeds the probe size limit"
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                return None, None, f"{path.name} was truncated while being read"
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            return None, None, f"{path.name} grew while being read"
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
        for opened in reversed(parent_descriptors):
            os.close(opened)
        root_after = os.fstat(root_descriptor)
        os.close(root_descriptor)
    stable = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")
    if any(getattr(before, name) != getattr(after, name) for name in stable):
        return None, None, f"{path.name} changed while being read"
    root_stable = ("st_dev", "st_ino", "st_mode", "st_mtime_ns", "st_ctime_ns")
    if any(getattr(root_before, name) != getattr(root_after, name) for name in root_stable):
        return None, None, f"{path.name} trust root changed while being read"
    return b"".join(chunks), before, None


def _regular_file(path: Path, *, maximum_bytes: int) -> tuple[os.stat_result | None, str | None]:
    _, info, error = _read_bytes_fd(path, maximum_bytes=maximum_bytes)
    return info, error


def _read_text(path: Path, *, maximum_bytes: int) -> tuple[str | None, str | None]:
    payload, _, error = _read_bytes_fd(path, maximum_bytes=maximum_bytes)
    if error:
        return None, error
    try:
        return (payload or b"").decode("utf-8", errors="strict"), None
    except UnicodeError as exc:
        return None, f"{path.name} is not readable UTF-8: {exc}"


def _read_json(
    path: Path, *, maximum_bytes: int
) -> tuple[dict[str, Any] | None, str | None]:
    text, error = _read_text(path, maximum_bytes=maximum_bytes)
    if error:
        return None, error
    try:
        document = json.loads(text or "")
    except (json.JSONDecodeError, RecursionError) as exc:
        return None, f"{path.name} is not valid bounded JSON: {exc}"
    if not isinstance(document, dict):
        return None, f"{path.name} must contain one JSON object"
    return document, None


def _discover_validation_test_ids() -> list[str]:
    vector = qualification_content_vector(ROOT_DIR, "HEAD")
    _suite, test_ids, _modules = discover_owned_test_suite(
        ROOT_DIR, "Q0", vector
    )
    if not test_ids or len(test_ids) != len(set(test_ids)):
        raise ValueError("current unittest discovery is empty or contains duplicate IDs")
    return test_ids


def _sha256_path(
    path: Path, *, additional_root: Path | None = None
) -> tuple[int, str]:
    payload, info, error = _read_bytes_fd(
        path,
        maximum_bytes=MAX_PROVENANCE_BYTES,
        allow_empty=False,
        additional_root=additional_root,
    )
    if error or info is None or payload is None:
        raise ValueError(error or f"not a regular file: {path}")
    return info.st_size, hashlib.sha256(payload).hexdigest()


def _required_coverage_failures(
    discovered_ids: list[str], passing_ids: list[str], *, context: str
) -> list[str]:
    discovered = set(discovered_ids)
    passing = set(passing_ids)
    failures: list[str] = []
    for coverage_class, required_ids in REQUIRED_M0_COVERAGE.items():
        missing = [test_id for test_id in required_ids if test_id not in discovered]
        nonpassing = [test_id for test_id in required_ids if test_id not in passing]
        if missing:
            failures.append(
                f"{context} missing required M0 coverage {coverage_class}: "
                + ", ".join(missing)
            )
        elif nonpassing:
            failures.append(
                f"{context} did not pass required M0 coverage {coverage_class}: "
                + ", ".join(nonpassing)
            )
    return failures


def _parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or UTC_TIMESTAMP.fullmatch(value) is None:
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None
    return parsed


def _runtime_executable_identity(
    *, container_id: str, image_digest: str, executable_path: str
) -> tuple[int | None, str | None, str | None]:
    """Hash the executable in the active container or its immutable image."""

    try:
        candidate = Path(executable_path)
        if (
            not candidate.is_absolute()
            or candidate != Path(os.path.normpath(executable_path))
            or not executable_path.startswith("/usr/bin/python3.")
        ):
            return None, None, "recorded Python executable path is not canonical"
        identity_file_value = os.environ.get("AMS_RUNTIME_CONTAINER_ID_FILE", "")
        if identity_file_value:
            identity_file = Path(identity_file_value)
            if identity_file.is_file():
                active_id = identity_file.read_text(encoding="ascii").strip()
                if active_id == container_id:
                    if (
                        os.environ.get("AMS_CONTAINER_IMAGE_DIGEST") != image_digest
                        or os.environ.get("AMS_CONTAINER_IMAGE_DIGEST_SOURCE")
                        != "docker_image_inspect_host"
                    ):
                        return (
                            None,
                            None,
                            "active container image identity differs from suite evidence",
                        )
                    current = Path(sys.executable).resolve(strict=True)
                    if str(current) != executable_path:
                        return (
                            None,
                            None,
                            "active-container Python path differs from recorded path",
                        )
                    size, digest = _sha256_path(
                        current, additional_root=Path("/usr/bin")
                    )
                    return size, digest, None

        inspection = subprocess.run(
            ["docker", "inspect", "--format", "{{.Image}}", container_id],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if inspection.returncode != 0 or inspection.stdout.strip() != image_digest:
            return None, None, "runtime container is not an instance of the exact image"

        command = [
            "docker",
            "run",
            "--rm",
            "--network=none",
            "--entrypoint",
            "/bin/bash",
            image_digest,
            "-c",
            'stat -Lc "%s" -- "$1"; sha256sum -- "$1"',
            "bash",
            executable_path,
        ]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        lines = result.stdout.splitlines()
        if result.returncode != 0 or len(lines) != 2:
            return None, None, "could not inspect Python executable in exact image"
        if re.fullmatch(r"[1-9][0-9]*", lines[0]) is None:
            return None, None, "exact-image Python executable size is malformed"
        hash_match = re.fullmatch(r"([0-9a-f]{64})  (.+)", lines[1])
        if hash_match is None or hash_match.group(2) != executable_path:
            return None, None, "exact-image Python executable hash is malformed"
        return int(lines[0]), hash_match.group(1), None
    except (OSError, UnicodeError, subprocess.SubprocessError, ValueError) as exc:
        return None, None, f"could not independently inspect Python executable: {exc}"


def _producer_exit_code(path: Path) -> tuple[int | None, str | None]:
    text, error = _read_text(path, maximum_bytes=MAX_EXIT_CODE_BYTES)
    if error:
        return None, error
    if text is None or re.fullmatch(r"(?:0|[1-9][0-9]*)\n?", text) is None:
        return None, f"{path.name} is not a canonical non-negative exit code"
    return int(text.strip()), None


def _parse_dependency_output(
    log_text: str,
) -> tuple[list[str], list[tuple[str, str]], int, str]:
    failures: list[str] = []
    observed_records: list[tuple[str, str]] = []
    warning_count = 0
    raw_sha256 = hashlib.sha256(log_text.encode("utf-8")).hexdigest()
    lines = log_text.splitlines()
    allowed_information = {
        "Network/radio dependency check",
        "Python runtime compatibility check",
        "Python runtime compatibility passed.",
    }
    for line in lines:
        match = DEPENDENCY_STATUS_LINE.fullmatch(line)
        if match:
            status, label, _ = match.groups()
            observed_records.append((label, status))
            warning_count += status == "WARN"
            continue
        if (
            not line
            or line in allowed_information
            or line.startswith("Repository: /")
            or line.startswith("Dependency lock: /")
            or re.fullmatch(r"Dependency check passed with [0-9]+ warning\(s\)\.", line)
        ):
            continue
        failures.append(f"unexpected dependency-log line: {line[:160]}")

    if tuple(label for label, _ in observed_records) != EXPECTED_DEPENDENCY_RECORDS:
        failures.append("dependency log does not contain the complete ordered check record")
    for label, status in observed_records:
        if label == "cuda:gpu":
            if status not in {"PASS", "WARN"}:
                failures.append(f"{label} recorded disallowed status {status}")
        elif status != "PASS":
            failures.append(f"{label} did not pass (status={status})")

    expected_terminal = f"Dependency check passed with {warning_count} warning(s)."
    if not lines or lines[-1] != expected_terminal:
        failures.append("dependency log terminal count does not match complete raw records")
    if lines.count("Network/radio dependency check") != 1:
        failures.append("dependency log header is missing or duplicated")
    if lines.count("Python runtime compatibility check") != 1:
        failures.append("runtime compatibility raw section is missing or duplicated")
    if lines.count("Python runtime compatibility passed.") != 1:
        failures.append("runtime compatibility completion is missing or duplicated")
    return failures, observed_records, warning_count, raw_sha256


def dependency_gate(run_dir: Path) -> dict[str, Any]:
    exit_path = run_dir / "logs/check_deps.log.exit_code"
    log_path = run_dir / "logs/check_deps.log"
    exit_code, exit_error = _producer_exit_code(exit_path)
    log_text, log_error = _read_text(log_path, maximum_bytes=MAX_LOG_BYTES)
    failures = [error for error in (exit_error, log_error) if error]
    if exit_code is not None and exit_code != 0:
        failures.append(f"check_deps exited with {exit_code}")

    observed_records: list[tuple[str, str]] = []
    warning_count = 0
    raw_sha256: str | None = None
    if log_text is not None:
        parsed_failures, observed_records, warning_count, raw_sha256 = (
            _parse_dependency_output(log_text)
        )
        failures.extend(parsed_failures)
    if failures:
        return gate(
            "failed",
            "dependency qualification did not pass",
            {
                "failures": failures,
                "raw_log_sha256": raw_sha256,
                "observed_record_count": len(observed_records),
                "observed_records": [
                    {"label": label, "status": status}
                    for label, status in observed_records
                ],
            },
        )
    return gate(
        "passed",
        "complete bounded dependency output independently contains every passing check",
        {
            "exit_code": 0,
            "raw_log_sha256": raw_sha256,
            "observed_record_count": len(observed_records),
            "observed_records": [
                {"label": label, "status": status}
                for label, status in observed_records
            ],
            "warning_count": warning_count,
        },
    )


def runtime_lock_gate(run_dir: Path) -> dict[str, Any]:
    failures: list[str] = []
    exit_code, exit_error = _producer_exit_code(
        run_dir / "logs/m0_runtime_lock_producer.log.exit_code"
    )
    stderr_payload, _, stderr_error = _read_bytes_fd(
        run_dir / "logs/m0_runtime_lock_producer.log",
        maximum_bytes=MAX_LOG_BYTES,
        allow_empty=True,
    )
    report, report_error = _read_json(
        run_dir / "metrics/m0_runtime_lock.json", maximum_bytes=MAX_RUNTIME_LOCK_BYTES
    )
    failures.extend(error for error in (exit_error, stderr_error, report_error) if error)
    if exit_code is not None and exit_code != 0:
        failures.append(f"runtime-lock verifier exited with {exit_code}")
    if stderr_payload not in (None, b""):
        failures.append("runtime-lock verifier wrote unexpected stderr")
    if report is not None:
        if set(report) != {
            "schema_version", "contract", "passed", "observed_image_digest",
            "lock_sha256", "checks", "failures",
        }:
            failures.append("runtime-lock report schema is not exact")
        if (
            report.get("schema_version") != 1
            or report.get("contract") != "ams.m0.runtime-lock-verification/v1"
            or report.get("passed") is not True
            or report.get("failures") != []
        ):
            failures.append("runtime-lock report did not pass without findings")
        checks = report.get("checks") if isinstance(report.get("checks"), dict) else {}
        required = {
            "lock", "image_digest", "runtime_manifests", "runtime_identity_files",
            "m0_execution_policy", "external_sources", "ns3_tree",
        }
        if set(checks) != required or any(
            not isinstance(checks.get(name), dict)
            or checks[name].get("status") != "passed"
            for name in required
        ):
            failures.append("runtime-lock required live check set is not exact/all-pass")
        _lock_size, lock_sha256 = _sha256_path(
            ROOT_DIR / "network/config/dependency_lock.yaml"
        )
        if report.get("lock_sha256") != lock_sha256:
            failures.append("runtime-lock report does not bind the current lock bytes")
    return gate(
        "failed" if failures else "passed",
        "live runtime-lock recomputation did not qualify" if failures else
        "all manifests, runtime bytes, external revisions and ns-3 tree were recomputed live",
        {"failures": failures, "exit_code": exit_code},
    )


def validation_suite_gate(run_dir: Path) -> dict[str, Any]:
    failures: list[str] = []
    parsed_outcomes: list[tuple[str, str]] = []
    producer_exit, exit_error = _producer_exit_code(
        run_dir / "logs/m0_validation_suite_producer.log.exit_code"
    )
    _, producer_log_error = _read_text(
        run_dir / "logs/m0_validation_suite_producer.log",
        maximum_bytes=MAX_LOG_BYTES,
    )
    document, document_error = _read_json(
        run_dir / "metrics/m0_validation_suite.json",
        maximum_bytes=MAX_TEST_RESULT_BYTES,
    )
    provenance_document, provenance_record_error = _read_json(
        run_dir / "metrics/provenance.json",
        maximum_bytes=MAX_PROVENANCE_BYTES,
    )
    raw_text, raw_error = _read_text(
        run_dir / "logs/m0_validation_suite.log", maximum_bytes=MAX_TEST_LOG_BYTES
    )
    failures.extend(
        error
        for error in (
            exit_error,
            producer_log_error,
            document_error,
            provenance_record_error,
            raw_error,
        )
        if error
    )
    if producer_exit is not None and producer_exit != 0:
        failures.append(f"validation-suite producer exited with {producer_exit}")

    frozen_manifest_sha256: str | None = None
    try:
        frozen_manifest, frozen_manifest_sha256 = load_frozen_test_manifest(ROOT_DIR)
        expected_ids = list(frozen_manifest["ordered_test_ids"])
    except Exception as exc:
        expected_ids = []
        failures.append(f"frozen M0 test manifest failed: {exc}")

    try:
        live_ids = _discover_validation_test_ids()
        if live_ids != expected_ids:
            failures.append("current unittest discovery differs from frozen exact manifest")
    except Exception as exc:
        failures.append(f"independent unittest discovery failed: {exc}")

    try:
        current_source_bindings = suite_source_bindings(ROOT_DIR)
        current_external_bindings = suite_external_bindings(ROOT_DIR)
        for test_id in expected_ids:
            module = test_id.split(".", 1)[0]
            if f"network/tests/{module}.py" not in current_source_bindings:
                raise ValueError(f"discovered source is not bound: {module}")
    except (OSError, ValueError) as exc:
        current_source_bindings = {}
        current_external_bindings = {}
        failures.append(f"could not hash current validation-suite sources: {exc}")

    raw_sha256: str | None = None
    raw_bytes = 0
    raw_ids: list[str] = []
    if raw_text is not None:
        encoded = raw_text.encode("utf-8")
        raw_bytes = len(encoded)
        raw_sha256 = hashlib.sha256(encoded).hexdigest()
        for line in raw_text.splitlines():
            if not line.startswith("test_"):
                continue
            match = TEST_SUCCESS_LINE.fullmatch(line)
            if match is None:
                failures.append(
                    f"raw unittest log contains a non-passing test record: {line[:200]}"
                )
                continue
            method, owner = match.groups()
            raw_ids.append(f"{owner}.{method}")
        terminal = re.search(
            r"\nRan ([0-9]+) tests in ([0-9]+(?:\.[0-9]+)?)s\n\nOK\n\Z",
            raw_text,
        )
        if terminal is None:
            failures.append("raw unittest log lacks an exact all-pass terminal record")
        elif int(terminal.group(1)) != len(expected_ids):
            failures.append("raw unittest terminal count differs from current discovery")
        if raw_ids != expected_ids:
            failures.append("raw unittest passing IDs differ from current complete discovery")

    if document is not None:
        expected_top_keys = {
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
        if set(document) != expected_top_keys:
            failures.append("validation-suite JSON has an unexpected schema shape")
        if (
            isinstance(document.get("schema_version"), bool)
            or not isinstance(document.get("schema_version"), int)
            or document.get("schema_version") != 5
        ):
            failures.append("validation-suite JSON schema_version is not exact integer 5")
        if document.get("suite") != "complete_network_validation_adversarial_unittest":
            failures.append("validation-suite identity is incorrect")
        started_utc = _parse_utc(document.get("started_utc"))
        completed_utc = _parse_utc(document.get("completed_utc"))
        if started_utc is None:
            failures.append("validation-suite started_utc is not a real canonical UTC timestamp")
        if completed_utc is None:
            failures.append("validation-suite completed_utc is not a real canonical UTC timestamp")
        if (
            started_utc is not None
            and completed_utc is not None
            and completed_utc < started_utc
        ):
            failures.append("validation-suite UTC interval is reversed")

        executable = document.get("python_executable")
        recorded_executable_path: str | None = None
        recorded_executable_hash: str | None = None
        recorded_executable_bytes: int | None = None
        if not isinstance(executable, dict) or set(executable) != {
            "resolved_path",
            "bytes",
            "sha256",
        }:
            failures.append("validation-suite Python executable record is malformed")
        else:
            path_value = executable.get("resolved_path")
            hash_value = executable.get("sha256")
            bytes_value = executable.get("bytes")
            if not isinstance(path_value, str):
                failures.append("validation-suite Python executable path is missing")
            else:
                recorded_executable_path = path_value
            if not isinstance(hash_value, str) or SHA256.fullmatch(hash_value) is None:
                failures.append("validation-suite Python executable hash is malformed")
            else:
                recorded_executable_hash = hash_value
            if (
                isinstance(bytes_value, bool)
                or not isinstance(bytes_value, int)
                or bytes_value < 1
            ):
                failures.append("validation-suite Python executable size is malformed")
            else:
                recorded_executable_bytes = bytes_value

        invocation = document.get("invocation")
        if not isinstance(invocation, dict) or set(invocation) != {
            "producer_command",
            "working_directory",
            "unittest_loader_call",
        }:
            failures.append("validation-suite invocation record is malformed")
        else:
            expected_command = [
                recorded_executable_path,
                "-S",
                "network/scripts/run_m0_validation_suite.py",
                "--run-dir",
                f"/run/ams/m0-artifacts/{run_dir.name}",
            ]
            if invocation.get("producer_command") != expected_command:
                failures.append("validation-suite producer command is not canonical")
            if invocation.get("working_directory") != "repository_root":
                failures.append("validation-suite working directory is not canonical")
            if invocation.get("unittest_loader_call") != {
                "api": "qualification_suite.discover_owned_test_suite",
                "node": "Q0",
                "manifest_path": M0_TEST_MANIFEST_PATH,
                "start_directory": "network/tests",
                "pattern": "test_*.py",
                "verbosity": 2,
                "buffer": True,
                "failfast": False,
            }:
                failures.append("validation-suite discovery invocation is not canonical")

        source_bindings = document.get("source_bindings")
        if source_bindings != current_source_bindings:
            failures.append("validation-suite source bindings differ from current source")
        source_bindings_after = document.get("source_bindings_after")
        if source_bindings_after != source_bindings:
            failures.append("validation-suite source bytes changed during execution")
        try:
            import_policy, import_policy_sha256 = load_m0_import_policy(ROOT_DIR)
            if (
                recorded_executable_path != import_policy.get("interpreter")
                or recorded_executable_hash
                != import_policy.get("interpreter_sha256")
            ):
                failures.append(
                    "validation-suite Python executable differs from locked import policy"
                )
            failures.extend(
                validate_m0_import_trace_record(
                    document.get("python_import_trace"),
                    import_policy,
                    import_policy_sha256,
                    run_dir.name,
                    current_source_bindings,
                )
            )
        except (OSError, ValueError, yaml.YAMLError) as exc:
            import_policy = None
            failures.append(f"M0 Python import policy could not be rederived: {exc}")
        try:
            expected_vector = qualification_content_vector(ROOT_DIR, "HEAD")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            expected_vector = None
            failures.append(f"qualification content vector could not be recomputed: {exc}")
        if document.get("qualification_content_vector") != expected_vector:
            failures.append("validation-suite Q0..Q8 content vector is not exact")
        expected_plan_contract = {
            "plan_version": 3,
            "path": "doc/network_radio_integration_plan_v3.md",
            "contract_sha256": current_source_bindings.get(
                "doc/network_radio_integration_plan_v3.md"
            ),
        }
        if document.get("plan_contract") != expected_plan_contract:
            failures.append("validation-suite plan_version/contract hash is not exact")
        external_bindings = document.get("external_input_bindings")
        if external_bindings != current_external_bindings:
            failures.append("validation-suite external inputs differ from current runtime inputs")
        if document.get("external_input_bindings_after") != external_bindings:
            failures.append("validation-suite external inputs changed during execution")
        frozen_record = document.get("frozen_test_manifest")
        if frozen_record != {
            "path": M0_TEST_MANIFEST_PATH,
            "sha256": frozen_manifest_sha256,
        }:
            failures.append("validation-suite did not use the frozen exact test manifest")
        provenance_manifest = (
            provenance_document.get("source_manifest")
            if isinstance(provenance_document, dict)
            and isinstance(provenance_document.get("source_manifest"), dict)
            else {}
        )
        provenance_configs = (
            provenance_document.get("config_hashes")
            if isinstance(provenance_document, dict)
            and isinstance(provenance_document.get("config_hashes"), dict)
            else {}
        )
        combined_provenance = provenance_manifest | provenance_configs
        if any(
            current_source_bindings.get(relative) != digest
            for relative, digest in combined_provenance.items()
            if relative not in MUTABLE_STATUS_OUTPUTS
        ):
            failures.append(
                "provenance source/config records disagree with the technical source snapshot"
            )
        if isinstance(provenance_document, dict) and provenance_document.get(
            "qualification_content_vector"
        ) != expected_vector:
            failures.append("provenance Q0..Q8 content vector differs from the suite")
        expected_consumption = (
            qualification_consumption(expected_vector, "m0")
            if isinstance(expected_vector, dict)
            else None
        )
        if isinstance(provenance_document, dict) and provenance_document.get(
            "qualification_consumption"
        ) != expected_consumption:
            failures.append("M0 provenance did not consume exactly Q0")
        if isinstance(provenance_document, dict) and provenance_document.get(
            "plan_contract"
        ) != expected_plan_contract:
            failures.append("provenance v3 plan contract differs from the suite")

        discovery = document.get("discovery")
        if not isinstance(discovery, dict):
            failures.append("validation-suite discovery record is missing")
        else:
            if set(discovery) != {
                "start_directory",
                "pattern",
                "test_count",
                "test_ids",
            }:
                failures.append("validation-suite discovery schema is not exact")
            if discovery.get("start_directory") != "network/tests":
                failures.append("validation-suite used a different start directory")
            if discovery.get("pattern") != "test_*.py":
                failures.append("validation-suite used a different discovery pattern")
            discovered_ids = discovery.get("test_ids")
            if (
                not isinstance(discovered_ids, list)
                or not all(isinstance(item, str) for item in discovered_ids)
                or discovered_ids != expected_ids
            ):
                failures.append("recorded discovered test IDs differ from current source")
            test_count = discovery.get("test_count")
            if (
                isinstance(test_count, bool)
                or not isinstance(test_count, int)
                or test_count != len(expected_ids)
            ):
                failures.append("recorded discovery count differs from current source")

        execution = document.get("execution")
        if not isinstance(execution, dict):
            failures.append("validation-suite execution record is missing")
        else:
            if set(execution) != {
                "started_monotonic_ns",
                "completed_monotonic_ns",
                "started_test_ids",
                "tests_run",
                "outcome_counts",
                "outcomes",
            }:
                failures.append("validation-suite execution schema is not exact")
            started_ns = execution.get("started_monotonic_ns")
            completed_ns = execution.get("completed_monotonic_ns")
            if (
                isinstance(started_ns, bool)
                or not isinstance(started_ns, int)
                or started_ns < 1
                or isinstance(completed_ns, bool)
                or not isinstance(completed_ns, int)
                or completed_ns <= started_ns
            ):
                failures.append("validation-suite monotonic interval is invalid")
            elif started_utc is not None and completed_utc is not None:
                monotonic_duration_s = (completed_ns - started_ns) / 1_000_000_000
                utc_duration_s = (completed_utc - started_utc).total_seconds()
                if abs(monotonic_duration_s - utc_duration_s) > UTC_MONOTONIC_TOLERANCE_S:
                    failures.append(
                        "validation-suite UTC and monotonic durations are inconsistent"
                    )
            if execution.get("started_test_ids") != expected_ids:
                failures.append("not every discovered test was started in exact order")
            tests_run = execution.get("tests_run")
            if (
                isinstance(tests_run, bool)
                or not isinstance(tests_run, int)
                or tests_run != len(expected_ids)
            ):
                failures.append("executed test count differs from current discovery")

            outcomes = execution.get("outcomes")
            if not isinstance(outcomes, list):
                failures.append("validation-suite outcomes are missing")
            else:
                for index, outcome in enumerate(outcomes):
                    if not isinstance(outcome, dict) or set(outcome) != {
                        "test_id",
                        "outcome",
                    }:
                        failures.append(f"outcome {index} is malformed")
                        continue
                    test_id = outcome.get("test_id")
                    status = outcome.get("outcome")
                    if not isinstance(test_id, str) or status not in OUTCOME_NAMES:
                        failures.append(f"outcome {index} has invalid values")
                        continue
                    parsed_outcomes.append((test_id, status))
                if parsed_outcomes != [(test_id, "passed") for test_id in expected_ids]:
                    failures.append("not every discovered test has one passing outcome")

            counts = execution.get("outcome_counts")
            derived_counts = {
                name: sum(status == name for _, status in parsed_outcomes)
                for name in OUTCOME_NAMES
            }
            if not isinstance(counts, dict) or set(counts) != set(OUTCOME_NAMES):
                failures.append("validation-suite outcome counts are malformed")
            elif any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in counts.values()
            ):
                failures.append("validation-suite outcome counts are not integers")
            elif counts != derived_counts:
                failures.append("validation-suite outcome counts do not match raw outcomes")
            if derived_counts.get("passed") != len(expected_ids) or any(
                derived_counts.get(name, 0) != 0 for name in OUTCOME_NAMES if name != "passed"
            ):
                failures.append("validation-suite contains a non-passing outcome")

        raw_record = document.get("raw_log")
        if not isinstance(raw_record, dict) or set(raw_record) != {
            "path",
            "bytes",
            "sha256",
        }:
            failures.append("validation-suite raw-log record is malformed")
        else:
            if raw_record.get("path") != "logs/m0_validation_suite.log":
                failures.append("validation-suite raw-log path is not fixed")
            recorded_bytes = raw_record.get("bytes")
            if (
                isinstance(recorded_bytes, bool)
                or not isinstance(recorded_bytes, int)
                or recorded_bytes != raw_bytes
            ):
                failures.append("validation-suite raw-log byte count does not match")
            recorded_hash = raw_record.get("sha256")
            if (
                not isinstance(recorded_hash, str)
                or SHA256.fullmatch(recorded_hash) is None
                or recorded_hash != raw_sha256
            ):
                failures.append("validation-suite raw-log hash does not match")

        observation = document.get("producer_observation")
        if not isinstance(observation, dict) or set(observation) != {"passed"}:
            failures.append("validation-suite producer observation is malformed")
        elif observation.get("passed") is not True:
            failures.append("validation-suite producer reported a contradictory result")

        execution_identity = document.get("execution_identity")
        identity_keys = {
            "container_image_digest",
            "container_image_digest_source",
            "runtime_container_id",
            "runtime_container_id_source",
            "source_mode",
            "source_commit",
            "source_mount_read_only",
            "project_overlay_mode",
            "python_no_site",
            "python_pycache_prefix",
            "python_sys_path",
            "sitecustomize_loaded",
            "usercustomize_loaded",
            "child_python_guard",
        }
        if not isinstance(execution_identity, dict) or set(execution_identity) != identity_keys:
            failures.append("validation-suite execution identity is malformed")
        else:
            digest = execution_identity.get("container_image_digest")
            container_id = execution_identity.get("runtime_container_id")
            if not isinstance(digest, str) or IMAGE_DIGEST.fullmatch(digest) is None:
                failures.append("validation-suite image digest is not immutable")
            if not isinstance(container_id, str) or SHA256.fullmatch(container_id) is None:
                failures.append("validation-suite container ID is not full length")
            if (
                execution_identity.get("container_image_digest_source")
                != "docker_image_inspect_host"
                or execution_identity.get("runtime_container_id_source")
                != "host_bind_mount"
            ):
                failures.append("validation-suite identity sources are not accepted")
            if (
                execution_identity.get("source_mode") != "clean_git_clone_ro"
                or re.fullmatch(
                    r"[0-9a-f]{40}", str(execution_identity.get("source_commit") or "")
                )
                is None
                or execution_identity.get("source_mount_read_only") is not True
                or execution_identity.get("project_overlay_mode")
                != "none_q0_source_only"
                or execution_identity.get("python_no_site") is not True
                or execution_identity.get("sitecustomize_loaded") is not False
                or execution_identity.get("usercustomize_loaded") is not False
                or not isinstance(execution_identity.get("python_sys_path"), list)
                or not str(execution_identity.get("python_pycache_prefix") or "").startswith(
                    "/tmp/ams-m0-pycache-"
                )
            ):
                failures.append("validation-suite source/Python isolation identity is not exact")
            child_guard = execution_identity.get("child_python_guard")
            guard_relative = "network/scripts/m0_python_guard/sitecustomize.py"
            expected_guard_hash = current_source_bindings.get(guard_relative)
            if child_guard != {
                "guard_marker": True,
                "no_site": 0,
                "sitecustomize_path": (
                    "/workspace/multiagent_simulation/"
                    "network/scripts/m0_python_guard/sitecustomize.py"
                ),
                "usercustomize_loaded": False,
                "sitecustomize_sha256": expected_guard_hash,
            }:
                failures.append("M0 child Python did not use only the tracked inert guard")
            python_sys_path = execution_identity.get("python_sys_path")
            if (
                not isinstance(import_policy, dict)
                or python_sys_path
                != expected_m0_sys_path(import_policy, run_dir.name)
            ):
                failures.append("validation-suite Python path is not the exact locked order")

            if (
                isinstance(container_id, str)
                and isinstance(digest, str)
                and isinstance(recorded_executable_path, str)
            ):
                inspected_bytes, inspected_hash, inspect_error = (
                    _runtime_executable_identity(
                        container_id=container_id,
                        image_digest=digest,
                        executable_path=recorded_executable_path,
                    )
                )
                if inspect_error:
                    failures.append(inspect_error)
                elif (
                    inspected_bytes != recorded_executable_bytes
                    or inspected_hash != recorded_executable_hash
                ):
                    failures.append(
                        "recorded Python executable differs from the exact qualified image"
                    )

            if provenance_document is not None:
                container = provenance_document.get("container_image")
                if not isinstance(container, dict):
                    failures.append("provenance container identity is missing")
                else:
                    expected_identity = {
                        "container_image_digest": container.get("digest"),
                        "container_image_digest_source": container.get("digest_source"),
                        "runtime_container_id": container.get("runtime_container_id"),
                        "runtime_container_id_source": container.get(
                            "runtime_container_id_source"
                        ),
                    }
                    if any(
                        execution_identity.get(key) != value
                        for key, value in expected_identity.items()
                    ):
                        failures.append(
                            "validation-suite did not run in the provenance-qualified container"
                        )

    passing_outcome_ids = [
        test_id for test_id, status in parsed_outcomes if status == "passed"
    ]
    failures.extend(
        _required_coverage_failures(
            expected_ids, passing_outcome_ids, context="captured suite"
        )
    )
    failures.extend(
        _required_coverage_failures(expected_ids, raw_ids, context="captured raw log")
    )

    details = {
        "failures": failures,
        "expected_test_count": len(expected_ids),
        "raw_log_sha256": raw_sha256,
        "raw_passing_test_count": len(raw_ids),
        "required_coverage": {
            name: list(test_ids) for name, test_ids in REQUIRED_M0_COVERAGE.items()
        },
    }
    if failures:
        return gate(
            "failed",
            "complete validation/adversarial suite did not independently qualify",
            details,
        )
    return gate(
        "passed",
        "all currently discovered validation/adversarial tests ran and passed in the exact qualified container",
        details,
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


def _run_host_command(args: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    if not args:
        raise ValueError("host command is empty")
    executable_map = {
        "docker": "/usr/bin/docker",
        "git": "/usr/bin/git",
    }
    executable = executable_map.get(args[0], args[0])
    if executable not in executable_map.values():
        raise ValueError(f"host command is outside the locked launcher set: {args[0]}")
    environment = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C",
        "HOME": "/nonexistent",
        "DOCKER_HOST": "unix:///var/run/docker.sock",
        "DOCKER_CONFIG": "/nonexistent",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    return subprocess.run(
        [executable, *args[1:]],
        cwd=ROOT_DIR,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        env=environment,
    )


def _host_file_identity(
    path: Path, *, executable: bool = True
) -> dict[str, Any]:
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError(f"host executable path is not canonical/non-symlink: {path}")
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size < 1
            or (executable and before.st_mode & 0o111 == 0)
        ):
            expected_kind = "executable regular file" if executable else "regular file"
            raise ValueError(f"host file is not one {expected_kind}: {path}")
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError(f"host executable was truncated: {path}")
            digest.update(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ValueError(f"host executable grew while read: {path}")
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    stable = (
        "st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns"
    )
    if any(getattr(before, key) != getattr(after, key) for key in stable):
        raise ValueError(f"host executable changed while read: {path}")
    return {"bytes": before.st_size, "sha256": digest.hexdigest()}


def _host_execution_identity(
    source: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[str]]:
    failures: list[str] = []
    try:
        lock = yaml.safe_load(
            (ROOT_DIR / "network/config/dependency_lock.yaml").read_text(
                encoding="utf-8"
            )
        )
        policy = lock.get("m0_execution_policy") if isinstance(lock, dict) else None
        if not isinstance(policy, dict) or policy.get("schema_version") != 1:
            raise ValueError("M0 execution policy is unavailable")
        policy_sha256 = hashlib.sha256(
            json.dumps(policy, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        expected_path = policy.get("host_final_path")
        if os.environ.get("PATH") != expected_path:
            failures.append("host-final PATH differs from the execution policy")
        expected_host = policy.get("host_final_executable_sha256")
        if not isinstance(expected_host, dict) or not expected_host:
            raise ValueError("host-final executable hash policy is empty")
        host_executables: dict[str, dict[str, Any]] = {}
        for path_text, expected_sha256 in sorted(expected_host.items()):
            identity = _host_file_identity(Path(path_text))
            if identity["sha256"] != expected_sha256:
                failures.append(f"host-final executable hash mismatch: {path_text}")
            host_executables[path_text] = identity
        expected_sys_path = policy.get("host_final_python_sys_path")
        if not isinstance(expected_sys_path, list) or not expected_sys_path:
            raise ValueError("host-final Python sys.path policy is empty")
        root_text = str(ROOT_DIR)
        normalized_sys_path = [
            "<repository_root>" + value[len(root_text) :]
            if value == root_text or value.startswith(root_text + "/")
            else value
            for value in sys.path
        ]
        if normalized_sys_path != expected_sys_path:
            failures.append("host-final Python sys.path differs from execution policy")
        expected_imports = policy.get("host_final_python_imports")
        if not isinstance(expected_imports, dict) or not expected_imports:
            raise ValueError("host-final Python import policy is empty")
        host_python_imports: dict[str, dict[str, Any]] = {}
        for module_name, expected in sorted(expected_imports.items()):
            module = sys.modules.get(module_name)
            module_file = getattr(module, "__file__", None)
            if not isinstance(module_file, str) or not isinstance(expected, dict):
                failures.append(f"host-final locked Python module is absent: {module_name}")
                continue
            path = Path(module_file).resolve(strict=True)
            identity = {
                "path": str(path),
                **_host_file_identity(path, executable=False),
            }
            if identity != expected:
                failures.append(f"host-final Python import identity mismatch: {module_name}")
            host_python_imports[module_name] = identity
        vector = source.get("qualification_content_vector") if isinstance(source, dict) else None
        entries = vector.get("entry_manifest") if isinstance(vector, dict) else None
        by_path = (
            {
                entry.get("path"): entry
                for entry in entries
                if isinstance(entry, dict) and isinstance(entry.get("path"), str)
            }
            if isinstance(entries, list)
            else {}
        )
        source_paths = policy.get("critical_source_executables")
        if not isinstance(source_paths, list) or not source_paths:
            raise ValueError("critical source executable policy is empty")
        source_executables: dict[str, dict[str, Any]] = {}
        for relative in source_paths:
            entry = by_path.get(relative)
            if (
                not isinstance(entry, dict)
                or entry.get("owner") != "Q0"
                or entry.get("object_type") != "blob"
                or entry.get("git_mode") != "100755"
                or SHA256.fullmatch(str(entry.get("blob_sha256") or "")) is None
            ):
                failures.append(
                    f"critical source executable is not an executable Q0 blob: {relative}"
                )
                continue
            source_executables[relative] = {
                "git_mode": entry["git_mode"],
                "sha256": entry["blob_sha256"],
            }
        return {
            "schema_version": 1,
            "contract": "ams.m0.host-execution-identity/v1",
            "execution_policy_sha256": policy_sha256,
            "host_path": expected_path,
            "host_executables": host_executables,
            "host_python": {
                "sys_path": normalized_sys_path,
                "third_party_imports": host_python_imports,
            },
            "source_executables": source_executables,
        }, failures
    except (OSError, TypeError, ValueError, yaml.YAMLError) as exc:
        return {}, [f"host-final execution identity failed: {exc}"]


def _source_binding_digest(bindings: dict[str, str]) -> str:
    payload = json.dumps(bindings, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(payload).hexdigest()


def _legacy_host_checkout_snapshot_unused() -> tuple[dict[str, Any] | None, str | None]:
    try:
        status = _run_host_command(
            ["git", "status", "--porcelain", "--untracked-files=all"], timeout=15
        )
        commit = _run_host_command(["git", "rev-parse", "HEAD"], timeout=15)
        if status.returncode != 0 or commit.returncode != 0:
            return None, "host Git identity could not be inspected"
        if status.stdout.strip():
            return None, "host checkout is dirty"
        commit_value = commit.stdout.strip()
        if re.fullmatch(r"[0-9a-f]{40}", commit_value) is None:
            return None, "host checkout commit is malformed"
        bindings = suite_source_bindings(ROOT_DIR)
        return {
            "git_commit": commit_value,
            "source_file_count": len(bindings),
            "source_binding_sha256": _source_binding_digest(bindings),
        }, None
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        return None, f"host checkout snapshot failed: {exc}"


def _legacy_inspect_retained_container_unused(
    *, expected_container_id: str, expected_image: str, run_id: str
) -> tuple[dict[str, Any], list[str]]:
    failures: list[str] = []
    details: dict[str, Any] = {
        "container_id": expected_container_id,
        "image_digest": expected_image,
    }
    result = _run_host_command(
        ["docker", "inspect", expected_container_id], timeout=20
    )
    if result.returncode != 0:
        return details, ["retained container could not be inspected from the host"]
    try:
        documents = json.loads(result.stdout)
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        return details, [f"docker inspect did not return bounded JSON: {exc}"]
    if not isinstance(documents, list) or len(documents) != 1 or not isinstance(
        documents[0], dict
    ):
        return details, ["docker inspect did not return exactly one container"]
    document = documents[0]
    config = document.get("Config") if isinstance(document.get("Config"), dict) else {}
    host = (
        document.get("HostConfig")
        if isinstance(document.get("HostConfig"), dict)
        else {}
    )
    state = document.get("State") if isinstance(document.get("State"), dict) else {}
    mounts = document.get("Mounts") if isinstance(document.get("Mounts"), list) else []
    command = config.get("Cmd") if isinstance(config.get("Cmd"), list) else []
    details.update(
        {
            "container_image": document.get("Image"),
            "command": command,
            "working_directory": config.get("WorkingDir"),
            "network_mode": host.get("NetworkMode"),
            "privileged": host.get("Privileged"),
            "state": {
                key: state.get(key)
                for key in (
                    "Status",
                    "Running",
                    "Paused",
                    "Restarting",
                    "OOMKilled",
                    "Dead",
                    "ExitCode",
                )
            },
            "restart_count": document.get("RestartCount"),
        }
    )
    if document.get("Id") != expected_container_id:
        failures.append("docker inspect full container ID differs from host expectation")
    if document.get("Image") != expected_image:
        failures.append("retained container is not an instance of the exact qualified image")
    if config.get("Image") != expected_image:
        failures.append("retained container Config.Image is not the immutable image ID")
    if config.get("WorkingDir") != CONTAINER_WORKDIR:
        failures.append("retained container working directory is not canonical")
    if host.get("Privileged") is not True:
        failures.append("retained acceptance container was not privileged")
    if host.get("NetworkMode") != "host":
        failures.append("retained acceptance container did not use host networking")
    if command.count(f"RUN_ID={run_id}") != 1 or command.count(M0_RUNNER_TOKEN) != 1:
        failures.append("retained container command is not the exact M0 RUN_ID/runner command")
    elif command.index(f"RUN_ID={run_id}") > command.index(M0_RUNNER_TOKEN):
        failures.append("retained container RUN_ID occurs after the M0 runner")

    expected_source = str(ROOT_DIR.resolve(strict=True))
    matching_mounts = [
        mount
        for mount in mounts
        if isinstance(mount, dict) and mount.get("Destination") == CONTAINER_WORKDIR
    ]
    if len(matching_mounts) != 1:
        failures.append("retained container does not have exactly one repository bind mount")
    else:
        mount = matching_mounts[0]
        details["repository_mount"] = {
            key: mount.get(key) for key in ("Type", "Source", "Destination", "RW")
        }
        if (
            mount.get("Type") != "bind"
            or mount.get("Source") != expected_source
            or mount.get("RW") is not True
        ):
            failures.append("retained container repository mount identity is incorrect")

    expected_state = {
        "Status": "exited",
        "Running": False,
        "Paused": False,
        "Restarting": False,
        "OOMKilled": False,
        "Dead": False,
        "ExitCode": 0,
    }
    for key, expected in expected_state.items():
        if state.get(key) != expected:
            failures.append(f"retained container state {key}={state.get(key)!r}, expected {expected!r}")
    if document.get("RestartCount") != 0:
        failures.append("retained container restart count is nonzero")
    return details, failures


def _legacy_fresh_container_command_unused(image_digest: str, command: list[str]) -> list[str]:
    bootstrap = """
set -eo pipefail
source /opt/ros/humble/setup.bash
source /workspace/ardu_ws/install/setup.bash
if [[ -f install/setup.bash ]]; then source install/setup.bash; fi
export GZ_VERSION=harmonic
exec "$@"
""".strip()
    return [
        "docker",
        "run",
        "--rm",
        "--privileged",
        "--network=host",
        "--entrypoint",
        "/bin/bash",
        "-v",
        f"{ROOT_DIR.resolve(strict=True)}:{CONTAINER_WORKDIR}",
        "-w",
        CONTAINER_WORKDIR,
        image_digest,
        "-lc",
        bootstrap,
        "bash",
        *command,
    ]


def _legacy_run_in_fresh_exact_image_unused(
    image_digest: str, command: list[str], *, timeout: int
) -> subprocess.CompletedProcess[str]:
    return _run_host_command(
        _legacy_fresh_container_command_unused(image_digest, command), timeout=timeout
    )


def _parse_host_unittest_output(
    raw_text: str, expected_ids: list[str]
) -> tuple[list[str], list[str], str]:
    failures: list[str] = []
    passing_ids: list[str] = []
    for line in raw_text.splitlines():
        if not line.startswith("test_"):
            continue
        match = TEST_SUCCESS_LINE.fullmatch(line)
        if match is None:
            failures.append(f"host re-execution contains a non-passing test: {line[:200]}")
            continue
        method, owner = match.groups()
        passing_ids.append(f"{owner}.{method}")
    terminal = re.search(
        r"\nRan ([0-9]+) tests in ([0-9]+(?:\.[0-9]+)?)s\n\nOK\n\Z",
        raw_text,
    )
    if terminal is None:
        failures.append("host re-execution lacks the exact unittest all-pass terminal")
    elif int(terminal.group(1)) != len(expected_ids):
        failures.append("host re-execution terminal count differs from current discovery")
    if passing_ids != expected_ids:
        failures.append("host re-execution passing IDs differ from current discovery")
    failures.extend(
        _required_coverage_failures(
            expected_ids, passing_ids, context="host re-execution"
        )
    )
    return failures, passing_ids, hashlib.sha256(raw_text.encode("utf-8")).hexdigest()


def _legacy_host_final_gate_unused(run_dir: Path, expected_container_id: str) -> dict[str, Any]:
    failures: list[str] = []
    if _inside_docker_runtime():
        return gate(
            "failed",
            "host-final validation must execute on the Docker host",
            {"failures": ["/.dockerenv is present"]},
        )
    if CONTAINER_ID.fullmatch(expected_container_id) is None:
        return gate(
            "failed",
            "host-final expected container ID is malformed",
            {"failures": ["expected container ID must be 64 lowercase hex characters"]},
        )
    provenance, provenance_error = _read_json(
        run_dir / "metrics/provenance.json", maximum_bytes=MAX_PROVENANCE_BYTES
    )
    suite_record, suite_error = _read_json(
        run_dir / "metrics/m0_validation_suite.json",
        maximum_bytes=MAX_TEST_RESULT_BYTES,
    )
    failures.extend(error for error in (provenance_error, suite_error) if error)
    container = (
        provenance.get("container_image")
        if isinstance(provenance, dict)
        and isinstance(provenance.get("container_image"), dict)
        else {}
    )
    execution_identity = (
        suite_record.get("execution_identity")
        if isinstance(suite_record, dict)
        and isinstance(suite_record.get("execution_identity"), dict)
        else {}
    )
    image_digest = container.get("digest")
    if not isinstance(image_digest, str) or IMAGE_DIGEST.fullmatch(image_digest) is None:
        failures.append("host-final exact image digest is unavailable")
    if container.get("runtime_container_id") != expected_container_id:
        failures.append("provenance container ID differs from explicit host expectation")
    if execution_identity.get("runtime_container_id") != expected_container_id:
        failures.append("suite container ID differs from explicit host expectation")

    before, before_error = _host_checkout_snapshot()
    if before_error:
        failures.append(before_error)
    expected_ids: list[str] = []
    try:
        expected_ids = _discover_validation_test_ids()
    except Exception as exc:
        failures.append(f"host-final unittest discovery failed: {exc}")

    inspect_details: dict[str, Any] = {}
    if isinstance(image_digest, str) and IMAGE_DIGEST.fullmatch(image_digest):
        try:
            inspect_details, inspect_failures = _inspect_retained_container(
                expected_container_id=expected_container_id,
                expected_image=image_digest,
                run_id=run_dir.name,
            )
            failures.extend(inspect_failures)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            failures.append(f"retained container inspection failed: {exc}")

    dependency_details: dict[str, Any] = {}
    suite_details: dict[str, Any] = {}
    if not failures and isinstance(image_digest, str):
        try:
            dependency_result = _run_in_fresh_exact_image(
                image_digest,
                [
                    "network/scripts/check_deps.sh",
                    "--qualification-profile",
                    "m0",
                ],
                timeout=240,
            )
            dependency_failures, records, warnings, dependency_hash = (
                _parse_dependency_output(dependency_result.stdout)
            )
            if dependency_result.returncode != 0:
                dependency_failures.append(
                    f"host re-executed check_deps exited with {dependency_result.returncode}"
                )
            if dependency_result.stderr:
                dependency_failures.append("host re-executed check_deps wrote stderr")
            failures.extend(f"host dependency: {item}" for item in dependency_failures)
            dependency_details = {
                "command": [
                    "network/scripts/check_deps.sh",
                    "--qualification-profile",
                    "m0",
                ],
                "exit_code": dependency_result.returncode,
                "raw_stdout_sha256": dependency_hash,
                "record_count": len(records),
                "warning_count": warnings,
            }

            suite_command = [
                "python3",
                "network/scripts/qualification_suite.py",
                "--node",
                "Q0",
                "--qualification-profile",
                "m0",
            ]
            suite_result = _run_in_fresh_exact_image(
                image_digest, suite_command, timeout=900
            )
            suite_failures, passing_ids, suite_hash = _parse_host_unittest_output(
                suite_result.stderr, expected_ids
            )
            if suite_result.returncode != 0:
                suite_failures.append(
                    f"host re-executed unittest suite exited with {suite_result.returncode}"
                )
            try:
                suite_summary = json.loads(suite_result.stdout)
            except (json.JSONDecodeError, TypeError):
                suite_summary = {}
                suite_failures.append(
                    "host re-executed qualification suite summary is not JSON"
                )
            if (
                not isinstance(suite_summary, dict)
                or suite_summary.get("passed") is not True
                or suite_summary.get("node") != "Q0"
                or suite_summary.get("profile") != "m0"
                or suite_summary.get("ordered_test_ids") != expected_ids
            ):
                suite_failures.append(
                    "host re-executed qualification suite summary is not exact"
                )
            failures.extend(f"host suite: {item}" for item in suite_failures)
            suite_details = {
                "command": suite_command,
                "exit_code": suite_result.returncode,
                "raw_stderr_sha256": suite_hash,
                "passing_test_count": len(passing_ids),
            }
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            failures.append(f"fresh exact-image re-execution failed: {exc}")

    after, after_error = _host_checkout_snapshot()
    if after_error:
        failures.append(after_error)
    if before is not None and after is not None and after != before:
        failures.append("host source identity changed during host-final re-execution")

    details = {
        "failures": failures,
        "expected_container_id": expected_container_id,
        "image_digest": image_digest,
        "source_before": before,
        "source_after": after,
        "retained_container": inspect_details,
        "dependency_reexecution": dependency_details,
        "suite_reexecution": suite_details,
        "required_coverage": {
            name: list(test_ids) for name, test_ids in REQUIRED_M0_COVERAGE.items()
        },
    }
    if failures:
        return gate("failed", "host-final exact-runtime qualification failed", details)
    return gate(
        "passed",
        "host inspected the retained container and independently re-executed dependencies and all tests in the exact image",
        details,
    )


HOST_RECEIPT_CONTRACT = "ams.m0.host-final-receipt/v1"
PRESTART_INSPECTION_CONTRACT = "ams.m0.prestart-inspection/v1"
FORMAL_GATE_NAMES = (
    "dependency_check",
    "runtime_lock",
    "validation_adversarial_suite",
    "provenance",
    "host_final",
)


def _host_checkout_snapshot() -> tuple[dict[str, Any] | None, str | None]:
    """Bind host-final to one clean commit and its complete technical vector."""

    try:
        status = _run_host_command(
            ["git", "status", "--porcelain", "--untracked-files=all"], timeout=20
        )
        commit = _run_host_command(["git", "rev-parse", "HEAD"], timeout=20)
        if status.returncode != 0 or commit.returncode != 0:
            return None, "host Git identity could not be inspected"
        if status.stdout.strip():
            return None, "host checkout is dirty"
        commit_value = commit.stdout.strip()
        if re.fullmatch(r"[0-9a-f]{40}", commit_value) is None:
            return None, "host checkout commit is malformed"
        bindings = suite_source_bindings(ROOT_DIR)
        vector = qualification_content_vector(ROOT_DIR, commit_value)
        manifest, manifest_sha256 = load_frozen_test_manifest(ROOT_DIR)
        plan_path = ROOT_DIR / "doc/network_radio_integration_plan_v3.md"
        _plan_bytes, plan_sha256 = _sha256_path(plan_path)
        return {
            "git_commit": commit_value,
            "source_file_count": len(bindings),
            "source_binding_sha256": _source_binding_digest(bindings),
            "qualification_content_vector": vector,
            "frozen_test_manifest_sha256": manifest_sha256,
            "frozen_test_count": len(manifest["ordered_test_ids"]),
            "plan_path": "doc/network_radio_integration_plan_v3.md",
            "plan_sha256": plan_sha256,
        }, None
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        return None, f"host checkout snapshot failed: {exc}"


def _checkout_snapshot(
    root: Path, expected_commit: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Recompute a clean detached checkout identity from its live bytes."""

    try:
        commit = _run_host_command(
            ["git", "-C", str(root), "rev-parse", "HEAD"], timeout=20
        )
        status_result = _run_host_command(
            [
                "git", "-C", str(root), "status", "--porcelain",
                "--untracked-files=all",
            ],
            timeout=20,
        )
        if (
            commit.returncode != 0
            or commit.stdout.strip() != expected_commit
            or status_result.returncode != 0
            or status_result.stdout.strip()
        ):
            raise ValueError("checkout is not the exact clean expected commit")
        bindings = suite_source_bindings(root)
        vector = qualification_content_vector(root, expected_commit)
        manifest, manifest_sha256 = load_frozen_test_manifest(root)
        plan_path = root / "doc/network_radio_integration_plan_v3.md"
        _plan_bytes, plan_sha256 = _sha256_path(
            plan_path, additional_root=root
        )
        return {
            "git_commit": expected_commit,
            "source_file_count": len(bindings),
            "source_binding_sha256": _source_binding_digest(bindings),
            "qualification_content_vector": vector,
            "frozen_test_manifest_sha256": manifest_sha256,
            "frozen_test_count": len(manifest["ordered_test_ids"]),
            "plan_path": "doc/network_radio_integration_plan_v3.md",
            "plan_sha256": plan_sha256,
        }, None
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        return None, f"checkout snapshot failed: {exc}"


def _create_fresh_host_source_snapshot(
    expected_commit: str,
) -> tuple[Path | None, dict[str, Any] | None, str | None]:
    """Create a clone never exposed to the producer container."""

    destination = Path(
        tempfile.mkdtemp(prefix=f"ams-m0-host-source-{expected_commit[:12]}.")
    )
    try:
        destination.rmdir()
        clone = _run_host_command(
            [
                "git", "clone", "--quiet", "--no-hardlinks", "--no-checkout",
                str(ROOT_DIR), str(destination),
            ],
            timeout=120,
        )
        checkout = _run_host_command(
            ["git", "-C", str(destination), "checkout", "--quiet", "--detach", expected_commit],
            timeout=60,
        )
        if clone.returncode != 0 or checkout.returncode != 0:
            raise ValueError("fresh host source clone/checkout failed")
        (destination / "runs").mkdir()
        (destination / ".external/ns-3").mkdir(parents=True)
        identity, identity_error = _checkout_snapshot(destination, expected_commit)
        if identity_error or identity is None:
            raise ValueError(identity_error or "fresh source identity is unavailable")
        for current_root, directory_names, file_names in os.walk(
            destination, topdown=False, followlinks=False
        ):
            current = Path(current_root)
            for name in file_names:
                candidate = current / name
                if not candidate.is_symlink():
                    candidate.chmod(stat.S_IMODE(candidate.stat().st_mode) & ~0o222)
            for name in directory_names:
                candidate = current / name
                if not candidate.is_symlink():
                    candidate.chmod(stat.S_IMODE(candidate.stat().st_mode) & ~0o222)
        destination.chmod(stat.S_IMODE(destination.stat().st_mode) & ~0o222)
        return destination, identity, None
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        shutil.rmtree(destination, ignore_errors=True)
        return None, None, f"fresh host source creation failed: {exc}"


def _snapshot_artifact_tree(root: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Snapshot a tree using only dirfd-relative, no-follow, stable reads."""

    flags_dir = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    flags_file = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    entries: dict[str, dict[str, Any]] = {}
    total_bytes = 0

    def walk(directory_fd: int, prefix: str) -> None:
        nonlocal total_bytes
        directory_before = os.fstat(directory_fd)
        if not stat.S_ISDIR(directory_before.st_mode):
            raise ValueError(f"artifact directory is not a directory: {prefix or '.'}")
        names = sorted(os.listdir(directory_fd))
        if len(names) != len(set(names)):
            raise ValueError("artifact directory contains duplicate names")
        for name in names:
            if not name or name in {".", ".."} or "/" in name or "\x00" in name:
                raise ValueError("artifact tree contains an unsafe name")
            relative = f"{prefix}/{name}" if prefix else name
            descriptor = os.open(name, flags_file, dir_fd=directory_fd)
            try:
                before = os.fstat(descriptor)
                if stat.S_ISDIR(before.st_mode):
                    os.close(descriptor)
                    descriptor = os.open(name, flags_dir, dir_fd=directory_fd)
                    before = os.fstat(descriptor)
                    entries[relative] = {
                        "kind": "directory",
                        "mode": stat.S_IMODE(before.st_mode),
                        "device": before.st_dev,
                        "inode": before.st_ino,
                        "links": before.st_nlink,
                        "mtime_ns": before.st_mtime_ns,
                        "ctime_ns": before.st_ctime_ns,
                    }
                    walk(descriptor, relative)
                elif stat.S_ISREG(before.st_mode):
                    if before.st_nlink != 1:
                        raise ValueError(f"artifact has multiple hard links: {relative}")
                    if before.st_size > MAX_PROVENANCE_BYTES:
                        raise ValueError(f"artifact is individually oversized: {relative}")
                    digest = hashlib.sha256()
                    remaining = before.st_size
                    while remaining:
                        chunk = os.read(descriptor, min(1024 * 1024, remaining))
                        if not chunk:
                            raise ValueError(f"artifact was truncated: {relative}")
                        digest.update(chunk)
                        remaining -= len(chunk)
                    if os.read(descriptor, 1):
                        raise ValueError(f"artifact grew while read: {relative}")
                    total_bytes += before.st_size
                    if total_bytes > 256 * 1024 * 1024:
                        raise ValueError("artifact tree exceeds the host-final size bound")
                    entries[relative] = {
                        "kind": "file",
                        "mode": stat.S_IMODE(before.st_mode),
                        "device": before.st_dev,
                        "inode": before.st_ino,
                        "links": before.st_nlink,
                        "mtime_ns": before.st_mtime_ns,
                        "ctime_ns": before.st_ctime_ns,
                        "bytes": before.st_size,
                        "sha256": digest.hexdigest(),
                    }
                else:
                    raise ValueError(f"artifact is not a regular file/directory: {relative}")
                after = os.fstat(descriptor)
                stable = (
                    "st_dev", "st_ino", "st_mode", "st_nlink", "st_size",
                    "st_mtime_ns", "st_ctime_ns",
                )
                if any(getattr(before, key) != getattr(after, key) for key in stable):
                    raise ValueError(f"artifact changed while read: {relative}")
            finally:
                os.close(descriptor)
        directory_after = os.fstat(directory_fd)
        stable_dir = ("st_dev", "st_ino", "st_mode", "st_mtime_ns", "st_ctime_ns")
        if any(
            getattr(directory_before, key) != getattr(directory_after, key)
            for key in stable_dir
        ):
            raise ValueError(f"artifact directory changed while read: {prefix or '.'}")

    try:
        root_fd = os.open(root, flags_dir)
        try:
            root_info_before = os.fstat(root_fd)
            walk(root_fd, "")
            root_info_after = os.fstat(root_fd)
        finally:
            os.close(root_fd)
        root_fields = ("st_dev", "st_ino", "st_mode", "st_mtime_ns", "st_ctime_ns")
        if any(
            getattr(root_info_before, key) != getattr(root_info_after, key)
            for key in root_fields
        ):
            raise ValueError("artifact root changed while read")
        payload = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        return {
            "root_identity": {
                "device": root_info_before.st_dev,
                "inode": root_info_before.st_ino,
                "mode": stat.S_IMODE(root_info_before.st_mode),
                "mtime_ns": root_info_before.st_mtime_ns,
                "ctime_ns": root_info_before.st_ctime_ns,
            },
            "entries": entries,
            "entry_count": len(entries),
            "total_file_bytes": total_bytes,
            "tree_sha256": hashlib.sha256(payload).hexdigest(),
        }, None
    except (OSError, RuntimeError, ValueError) as exc:
        return None, f"secure artifact snapshot failed: {exc}"


def _write_host_validation_file(root: Path, relative: str, payload: bytes) -> None:
    """Write one host-only raw record exactly once and fsync it."""

    pure = Path(relative)
    if (
        not relative
        or pure.is_absolute()
        or ".." in pure.parts
        or "." in pure.parts
        or "\x00" in relative
    ):
        raise ValueError(f"unsafe host-validation relative path: {relative!r}")
    destination = root / pure
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(
        destination,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written < 1:
                raise OSError(f"short host-validation write: {relative}")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _host_validation_content_manifest(root: Path) -> dict[str, Any]:
    snapshot, error = _snapshot_artifact_tree(root)
    if error or snapshot is None:
        raise ValueError(error or "host-validation snapshot is unavailable")
    files = {
        relative: {
            "bytes": entry["bytes"],
            "sha256": entry["sha256"],
            "published_mode": 0o400,
        }
        for relative, entry in sorted(snapshot["entries"].items())
        if entry.get("kind") == "file"
    }
    if not files:
        raise ValueError("host-validation raw tree is empty")
    payload = json.dumps(files, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return {
        "schema_version": 1,
        "contract": "ams.m0.host-validation-content/v1",
        "files": files,
        "file_count": len(files),
        "content_sha256": hashlib.sha256(payload).hexdigest(),
    }


def _portable_content_manifest(snapshot: dict[str, Any]) -> dict[str, Any]:
    entries: dict[str, dict[str, Any]] = {}
    for relative, entry in sorted(snapshot.get("entries", {}).items()):
        if entry.get("kind") == "directory":
            entries[relative] = {
                "kind": "directory",
                "mode": 0o500,
            }
        elif entry.get("kind") == "file":
            entries[relative] = {
                "kind": "file",
                "mode": 0o400,
                "bytes": entry.get("bytes"),
                "sha256": entry.get("sha256"),
            }
        else:
            raise ValueError(f"unsupported operational snapshot entry: {relative}")
    payload = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return {
        "schema_version": 1,
        "contract": "ams.m0.portable-content-manifest/v1",
        "entries": entries,
        "entry_count": len(entries),
        "content_sha256": hashlib.sha256(payload).hexdigest(),
    }


def _remove_host_temp_tree(path: Path | None) -> None:
    if path is None or not path.exists():
        return
    for current_root, directory_names, file_names in os.walk(
        path, topdown=False, followlinks=False
    ):
        current = Path(current_root)
        for name in file_names:
            candidate = current / name
            if not candidate.is_symlink():
                try:
                    candidate.chmod(stat.S_IMODE(candidate.stat().st_mode) | 0o600)
                except OSError:
                    pass
        for name in directory_names:
            candidate = current / name
            if not candidate.is_symlink():
                try:
                    candidate.chmod(stat.S_IMODE(candidate.stat().st_mode) | 0o700)
                except OSError:
                    pass
    try:
        path.chmod(stat.S_IMODE(path.stat().st_mode) | 0o700)
    except OSError:
        pass
    shutil.rmtree(path, ignore_errors=True)


def _read_control_json(
    control_dir: Path, name: str, maximum: int = MAX_PROVENANCE_BYTES
) -> tuple[Any | None, bytes | None, str | None]:
    if control_dir.is_symlink() or not control_dir.is_dir():
        return None, None, "prestart control path is not a real directory"
    payload, _, error = _read_bytes_fd(
        control_dir / name, maximum_bytes=maximum, additional_root=control_dir
    )
    if error:
        return None, None, error
    try:
        return json.loads((payload or b"").decode("utf-8")), payload, None
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        return None, payload, f"{name} is not strict JSON: {exc}"


def _environment_map(values: Any) -> tuple[dict[str, str], list[str]]:
    failures: list[str] = []
    result: dict[str, str] = {}
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        return {}, ["container Config.Env is not a string list"]
    for value in values:
        name, separator, content = value.partition("=")
        if not separator or not name or name in result:
            failures.append("container Config.Env has malformed or duplicate names")
            continue
        result[name] = content
    return result, failures


def _container_immutable_fingerprint(document: dict[str, Any]) -> dict[str, Any]:
    config = document.get("Config") if isinstance(document.get("Config"), dict) else {}
    host = document.get("HostConfig") if isinstance(document.get("HostConfig"), dict) else {}
    mounts = document.get("Mounts") if isinstance(document.get("Mounts"), list) else []
    normalized_mounts = sorted(
        [
            {
                key: mount.get(key)
                for key in ("Type", "Source", "Destination", "Mode", "RW", "Propagation")
            }
            for mount in mounts
            if isinstance(mount, dict)
        ],
        key=lambda record: (
            str(record.get("Destination")),
            str(record.get("Source")),
        ),
    )
    return {
        "Image": document.get("Image"),
        "Config": {
            key: config.get(key)
            for key in ("Image", "User", "Entrypoint", "Cmd", "WorkingDir", "Env")
        },
        "HostConfig": {
            "Privileged": host.get("Privileged"),
            "NetworkMode": host.get("NetworkMode"),
            "ReadonlyRootfs": host.get("ReadonlyRootfs"),
            "RestartPolicy": host.get("RestartPolicy"),
            "Tmpfs": host.get("Tmpfs"),
            "CapAdd": host.get("CapAdd"),
            "CapDrop": host.get("CapDrop"),
            "SecurityOpt": host.get("SecurityOpt"),
            "Devices": host.get("Devices"),
            "DeviceRequests": host.get("DeviceRequests"),
        },
        "Mounts": normalized_mounts,
    }


def _inspect_retained_container(
    *,
    expected_container_id: str,
    expected_image: str,
    run_id: str,
    run_dir: Path,
    source_commit: str,
    image_reference: str,
    initial_control_dir: Path,
    host_validation_dir: Path | None = None,
) -> tuple[dict[str, Any], list[str]]:
    failures: list[str] = []
    details: dict[str, Any] = {}
    record, record_raw, record_error = _read_control_json(
        initial_control_dir, "prestart_inspection_record.json", MAX_TEST_RESULT_BYTES
    )
    initial_docs, container_raw, container_error = _read_control_json(
        initial_control_dir, "initial_container_inspect.json"
    )
    image_docs, image_raw, image_error = _read_control_json(
        initial_control_dir, "initial_image_inspect.json"
    )
    failures.extend(
        error for error in (record_error, container_error, image_error) if error
    )
    if not isinstance(record, dict) or set(record) != {
        "schema_version", "contract", "created_utc", "container_id", "image_id",
        "artifact_root_initial", "initial_container_inspect", "initial_image_inspect",
    }:
        failures.append("prestart inspection record schema is not exact")
        record = {}
    if (
        record.get("schema_version") != 1
        or record.get("contract") != PRESTART_INSPECTION_CONTRACT
        or record.get("container_id") != expected_container_id
        or record.get("image_id") != expected_image
        or _parse_utc(record.get("created_utc")) is None
    ):
        failures.append("prestart inspection record identity is invalid")
    for key, name, raw in (
        ("initial_container_inspect", "initial_container_inspect.json", container_raw),
        ("initial_image_inspect", "initial_image_inspect.json", image_raw),
    ):
        expected = {
            "path": name,
            "bytes": len(raw or b""),
            "sha256": hashlib.sha256(raw or b"").hexdigest(),
        }
        if record.get(key) != expected:
            failures.append(f"prestart {key} raw binding is invalid")
    artifact_initial = record.get("artifact_root_initial")
    try:
        artifact_root_info = run_dir.parent.stat(follow_symlinks=False)
    except OSError:
        artifact_root_info = None
    expected_empty_hash = hashlib.sha256(b"[]").hexdigest()
    if (
        not isinstance(artifact_initial, dict)
        or set(artifact_initial) != {
            "path", "device", "inode", "mode", "entry_count",
            "content_manifest_sha256",
        }
        or artifact_root_info is None
        or artifact_initial.get("path") != str(run_dir.parent.resolve(strict=True))
        or artifact_initial.get("device") != artifact_root_info.st_dev
        or artifact_initial.get("inode") != artifact_root_info.st_ino
        or artifact_initial.get("mode") != stat.S_IMODE(artifact_root_info.st_mode)
        or artifact_initial.get("entry_count") != 0
        or artifact_initial.get("content_manifest_sha256") != expected_empty_hash
    ):
        failures.append("prestart initially-empty artifact-root binding is invalid")
    initial = (
        initial_docs[0]
        if isinstance(initial_docs, list)
        and len(initial_docs) == 1
        and isinstance(initial_docs[0], dict)
        else {}
    )
    image_doc = (
        image_docs[0]
        if isinstance(image_docs, list)
        and len(image_docs) == 1
        and isinstance(image_docs[0], dict)
        else {}
    )
    if not initial:
        failures.append("prestart container inspect did not contain one document")
    if not image_doc or image_doc.get("Id") != expected_image:
        failures.append("prestart image inspect identity is invalid")

    final_result = _run_host_command(["docker", "inspect", expected_container_id], timeout=30)
    try:
        final_docs = json.loads(final_result.stdout)
    except (json.JSONDecodeError, RecursionError):
        final_docs = None
    final = (
        final_docs[0]
        if final_result.returncode == 0
        and isinstance(final_docs, list)
        and len(final_docs) == 1
        and isinstance(final_docs[0], dict)
        else {}
    )
    if not final:
        failures.append("final retained-container inspect did not contain one document")
    final_image_result = _run_host_command(
        ["docker", "image", "inspect", expected_image], timeout=30
    )
    try:
        final_image_docs = json.loads(final_image_result.stdout)
    except (json.JSONDecodeError, RecursionError):
        final_image_docs = None
    final_image = (
        final_image_docs[0]
        if final_image_result.returncode == 0
        and isinstance(final_image_docs, list)
        and len(final_image_docs) == 1
        and isinstance(final_image_docs[0], dict)
        else {}
    )
    if not final_image or final_image.get("Id") != expected_image:
        failures.append("final image inspection identity is invalid")
    if image_doc and final_image and image_doc != final_image:
        failures.append("qualified image inspection changed during collection")

    expected_command = [
        "scripts/acceptance_entrypoint.sh",
        "env",
        f"RUN_ID={run_id}",
        M0_RUNNER_TOKEN,
    ]
    config = final.get("Config") if isinstance(final.get("Config"), dict) else {}
    host = final.get("HostConfig") if isinstance(final.get("HostConfig"), dict) else {}
    env, env_failures = _environment_map(config.get("Env"))
    failures.extend(env_failures)
    expected_env = {
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
        "AMS_CONTAINER_IMAGE": image_reference,
        "AMS_CONTAINER_IMAGE_DIGEST": expected_image,
        "AMS_CONTAINER_IMAGE_DIGEST_SOURCE": "docker_image_inspect_host",
        "AMS_RUNTIME_CONTAINER_ID_FILE": "/run/ams/container_id",
        "AMS_M0_SOURCE_MODE": "clean_git_clone_ro",
        "AMS_M0_SOURCE_COMMIT": source_commit,
        "AMS_M0_PROJECT_OVERLAY_MODE": "none_q0_source_only",
        "AMS_M0_ARTIFACT_ROOT": "/run/ams/m0-artifacts",
        "AMS_M0_COLLECTION_SECURITY": "cap_drop_all_no_new_privileges",
        "AMS_M0_CAPABILITY_PROBE_MODE": "host_final_isolated_exact_image",
    }
    if env != expected_env:
        failures.append("retained container environment is not the exact M0 environment")
    if (
        final.get("Id") != expected_container_id
        or final.get("Image") != expected_image
        or config.get("Image") != expected_image
        or config.get("User") != "ubuntu"
        or config.get("Entrypoint") != ["/ros_entrypoint.sh"]
        or config.get("Cmd") != expected_command
        or config.get("WorkingDir") != CONTAINER_WORKDIR
    ):
        failures.append("retained container Config identity is not exact")
    if (
        host.get("Privileged") is not False
        or host.get("NetworkMode") != "none"
        or host.get("ReadonlyRootfs") is not True
        or host.get("RestartPolicy") != {"Name": "no", "MaximumRetryCount": 0}
        or host.get("Tmpfs") != {
            "/tmp": "rw,nosuid,nodev,exec,size=4g,mode=1777"
        }
        or host.get("CapAdd") is not None
        or host.get("CapDrop") != ["ALL"]
        or host.get("SecurityOpt") not in (
            ["no-new-privileges"],
            ["no-new-privileges:true"],
            ["label=disable", "no-new-privileges"],
            ["label=disable", "no-new-privileges:true"],
        )
        or host.get("Devices") != []
        or host.get("DeviceRequests") != EXPECTED_GPU_DEVICE_REQUESTS
    ):
        failures.append("retained container HostConfig is not exact")

    mounts = final.get("Mounts") if isinstance(final.get("Mounts"), list) else []
    by_destination = {
        mount.get("Destination"): mount for mount in mounts if isinstance(mount, dict)
    }
    if len(mounts) != 4 or len(by_destination) != 4 or set(by_destination) != {
        "/run/ams/container_id",
        "/run/ams/m0-artifacts",
        CONTAINER_WORKDIR,
        f"{CONTAINER_WORKDIR}/.external/ns-3",
    }:
        failures.append("retained container mount destinations are not exact")
    expected_mounts = {
        "/run/ams/m0-artifacts": (str(run_dir.parent.resolve(strict=True)), True),
        f"{CONTAINER_WORKDIR}/.external/ns-3": (
            str((ROOT_DIR / ".external/ns-3").resolve(strict=True)), False
        ),
    }
    for destination, (source, writable) in expected_mounts.items():
        mount = by_destination.get(destination, {})
        if (
            mount.get("Type") != "bind"
            or mount.get("Source") != source
            or mount.get("RW") is not writable
            or mount.get("Mode") != ("rw" if writable else "ro")
            or mount.get("Propagation") != "rprivate"
        ):
            failures.append(f"retained mount is invalid: {destination}")
    source_mount = by_destination.get(CONTAINER_WORKDIR, {})
    identity_mount = by_destination.get("/run/ams/container_id", {})
    source_path = Path(str(source_mount.get("Source", "")))
    if (
        source_mount.get("Type") != "bind"
        or source_mount.get("RW") is not False
        or source_mount.get("Mode") != "ro"
        or source_mount.get("Propagation") != "rprivate"
        or not str(source_path).startswith("/tmp/ams-m0-source.")
    ):
        failures.append("retained immutable source mount is invalid")
    if (
        identity_mount.get("Type") != "bind"
        or identity_mount.get("RW") is not False
        or identity_mount.get("Mode") != "ro"
        or identity_mount.get("Propagation") != "rprivate"
        or not str(identity_mount.get("Source", "")).startswith(
            "/tmp/ams-container-id."
        )
    ):
        failures.append("retained container identity mount is invalid")

    initial_state = initial.get("State") if isinstance(initial.get("State"), dict) else {}
    final_state = final.get("State") if isinstance(final.get("State"), dict) else {}
    if not (
        initial_state.get("Status") == "created"
        and initial_state.get("Running") is False
        and initial.get("RestartCount") == 0
    ):
        failures.append("prestart container state is not exactly created")
    expected_final_state = {
        "Status": "exited", "Running": False, "Paused": False,
        "Restarting": False, "OOMKilled": False, "Dead": False, "ExitCode": 0,
    }
    if any(final_state.get(key) != value for key, value in expected_final_state.items()):
        failures.append("final retained-container state is not exact exited/zero")
    if final.get("RestartCount") != 0:
        failures.append("retained container restart count is nonzero")
    if initial and final and _container_immutable_fingerprint(initial) != _container_immutable_fingerprint(final):
        failures.append("retained container immutable configuration changed after collection")

    raw_values = {
        "retained/prestart_inspection_record.json": record_raw or b"",
        "retained/initial_container_inspect.json": container_raw or b"",
        "retained/initial_image_inspect.json": image_raw or b"",
        "retained/final_container_inspect.json": final_result.stdout.encode("utf-8"),
        "retained/final_image_inspect.json": final_image_result.stdout.encode("utf-8"),
    }
    if host_validation_dir is not None:
        for relative, payload in raw_values.items():
            _write_host_validation_file(host_validation_dir, relative, payload)

    details.update(
        {
            "container_id": expected_container_id,
            "image_digest": expected_image,
            "source_snapshot": str(source_path),
            "prestart_record_sha256": hashlib.sha256(record_raw or b"").hexdigest(),
            "initial_container_inspect_sha256": hashlib.sha256(container_raw or b"").hexdigest(),
            "initial_image_inspect_sha256": hashlib.sha256(image_raw or b"").hexdigest(),
            "final_container_inspect_sha256": hashlib.sha256(
                final_result.stdout.encode("utf-8")
            ).hexdigest(),
            "final_image_inspect_sha256": hashlib.sha256(
                final_image_result.stdout.encode("utf-8")
            ).hexdigest(),
            "immutable_fingerprint_sha256": hashlib.sha256(
                json.dumps(
                    _container_immutable_fingerprint(final),
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
            "mount_sources": sorted(
                str(mount.get("Source")) for mount in mounts if isinstance(mount, dict)
            ),
            "raw_sha256": {
                relative: hashlib.sha256(payload).hexdigest()
                for relative, payload in sorted(raw_values.items())
            },
        }
    )
    return details, failures


def _run_isolated_capability_probe(
    image_digest: str,
    *,
    host_validation_dir: Path,
) -> tuple[dict[str, Any], list[str]]:
    """Prove target netns/TUN/sudo capability without candidate mounts."""

    failures: list[str] = []
    container_id = ""
    command_script = M0_CAPABILITY_COMMAND_SCRIPT
    create_args = [
        "docker", "create",
        "--hostname", "ams-m0-capability",
        "--add-host", "ams-m0-capability:127.0.1.1",
        "--user", "ubuntu",
        "--restart=no",
        "--cap-drop=ALL",
        "--cap-add=ALL",
        "--device", "/dev/net/tun:/dev/net/tun:rwm",
        "--network=none",
        "--read-only",
        "--tmpfs", "/tmp:rw,nosuid,nodev,exec,size=64m,mode=1777",
        image_digest,
        "/bin/bash", "-c", command_script,
    ]
    try:
        create = _run_host_command(create_args, timeout=60)
        container_id = create.stdout.strip()
        if create.returncode != 0 or CONTAINER_ID.fullmatch(container_id) is None:
            return {}, ["isolated capability container could not be created"]
        initial = _run_host_command(["docker", "inspect", container_id], timeout=30)
        image = _run_host_command(
            ["docker", "image", "inspect", image_digest], timeout=30
        )
        start = _run_host_command(
            ["docker", "start", "--attach", container_id], timeout=120
        )
        final = _run_host_command(["docker", "inspect", container_id], timeout=30)
        try:
            initial_docs = json.loads(initial.stdout)
            final_docs = json.loads(final.stdout)
            image_docs = json.loads(image.stdout)
            initial_doc = initial_docs[0]
            final_doc = final_docs[0]
            image_doc = image_docs[0]
        except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"isolated capability raw inspection is invalid: {exc}") from exc
        initial_state = initial_doc.get("State", {})
        final_state = final_doc.get("State", {})
        host = final_doc.get("HostConfig", {})
        config = final_doc.get("Config", {})
        if (
            initial_doc.get("Id") != container_id
            or initial_doc.get("Image") != image_digest
            or initial_state.get("Status") != "created"
            or initial_state.get("Running") is not False
            or final_doc.get("Id") != container_id
            or final_doc.get("Image") != image_digest
            or final_state.get("Status") != "exited"
            or final_state.get("ExitCode") != 0
            or final_state.get("OOMKilled") is not False
            or final_doc.get("RestartCount") != 0
        ):
            failures.append("isolated capability container lifecycle is invalid")
        if (
            image_doc.get("Id") != image_digest
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
            or initial_doc.get("Mounts") != []
            or final_doc.get("Mounts") != []
        ):
            failures.append("isolated capability container configuration is not exact")
        devices = host.get("Devices")
        if (
            not isinstance(devices, list)
            or len(devices) != 1
            or devices[0].get("PathOnHost") != "/dev/net/tun"
            or devices[0].get("PathInContainer") != "/dev/net/tun"
            or devices[0].get("CgroupPermissions") != "rwm"
        ):
            failures.append("isolated capability TUN device mapping is not exact")
        if (
            start.returncode != 0
            or start.stdout.encode("utf-8") != M0_CAPABILITY_STDOUT
            or start.stderr
        ):
            failures.append("isolated target-runtime capability command did not pass exactly")
        raw_values = {
            "capability/initial_container_inspect.json": initial.stdout.encode("utf-8"),
            "capability/final_container_inspect.json": final.stdout.encode("utf-8"),
            "capability/image_inspect.json": image.stdout.encode("utf-8"),
            "capability/stdout.txt": start.stdout.encode("utf-8"),
            "capability/stderr.txt": start.stderr.encode("utf-8"),
            "capability/command.json": (
                json.dumps(
                    {
                        "schema_version": 1,
                        "contract": "ams.m0.isolated-capability-probe/v1",
                        "container_id": container_id,
                        "image_digest": image_digest,
                        "create_argv": create_args,
                        "command": ["/bin/bash", "-c", command_script],
                        "source_or_artifact_mounts": False,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8"),
        }
        for relative, payload in raw_values.items():
            _write_host_validation_file(host_validation_dir, relative, payload)
        details = {
            "contract": "ams.m0.isolated-capability-probe/v1",
            "container_id": container_id,
            "image_digest": image_digest,
            "exit_code": final_state.get("ExitCode"),
            "no_candidate_mounts": initial_doc.get("Mounts") == final_doc.get("Mounts") == [],
            "tun_device": True,
            "passwordless_sudo": True,
            "unshare_network_namespace": True,
            "raw_sha256": {
                relative: hashlib.sha256(payload).hexdigest()
                for relative, payload in sorted(raw_values.items())
            },
        }
        return details, failures
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        return {}, [f"isolated target-runtime capability probe failed: {exc}"]
    finally:
        if container_id:
            _run_host_command(["docker", "rm", "-f", container_id], timeout=30)


def _run_in_fresh_exact_image(
    image_digest: str,
    *,
    run_id: str,
    source_snapshot: str,
    source_commit: str,
    host_validation_dir: Path,
) -> tuple[dict[str, Any], list[str]]:
    """Build and rerun every frozen Q0 check in a second exact-image container."""

    failures: list[str] = []
    details: dict[str, Any] = {}
    output_root = Path(tempfile.mkdtemp(prefix=f"ams-m0-reexec-{run_id}."))
    identity_path = Path(tempfile.mkstemp(prefix="ams-m0-reexec-id.")[1])
    container_id = ""
    try:
        create = _run_host_command(
            [
                "docker", "create",
                "--gpus", 'all,"capabilities=compute,utility"',
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges:true", "--network=none",
                "--user", "ubuntu", "--restart=no", "--read-only",
                "--tmpfs", "/tmp:rw,nosuid,nodev,exec,size=4g,mode=1777",
                "-e", f"AMS_CONTAINER_IMAGE={image_digest}",
                "-e", f"AMS_CONTAINER_IMAGE_DIGEST={image_digest}",
                "-e", "AMS_CONTAINER_IMAGE_DIGEST_SOURCE=docker_image_inspect_host",
                "-e", "AMS_RUNTIME_CONTAINER_ID_FILE=/run/ams/container_id",
                "-e", "AMS_M0_SOURCE_MODE=clean_git_clone_ro",
                "-e", f"AMS_M0_SOURCE_COMMIT={source_commit}",
                "-e", "AMS_M0_PROJECT_OVERLAY_MODE=none_q0_source_only",
                "-e", "AMS_M0_ARTIFACT_ROOT=/run/ams/m0-artifacts",
                "-e", "AMS_M0_COLLECTION_SECURITY=cap_drop_all_no_new_privileges",
                "-e", "AMS_M0_CAPABILITY_PROBE_MODE=host_final_isolated_exact_image",
                "-e", "NVIDIA_VISIBLE_DEVICES=all",
                "-e", "NVIDIA_DRIVER_CAPABILITIES=compute,utility",
                "-e", "SIONNA_MITSUBA_VARIANT=cuda_ad_mono_polarized",
                "-e", "GZ_VERSION=harmonic",
                "-v", f"{identity_path}:/run/ams/container_id:ro",
                "-v", f"{source_snapshot}:{CONTAINER_WORKDIR}:ro",
                "-v", f"{output_root}:/run/ams/m0-artifacts:rw",
                "-v", f"{(ROOT_DIR / '.external/ns-3').resolve(strict=True)}:{CONTAINER_WORKDIR}/.external/ns-3:ro",
                "-w", CONTAINER_WORKDIR,
                image_digest,
                "scripts/acceptance_entrypoint.sh",
                "network/scripts/run_m0_host_reexecution.sh",
                run_id,
            ],
            timeout=60,
        )
        container_id = create.stdout.strip()
        if create.returncode != 0 or CONTAINER_ID.fullmatch(container_id) is None:
            return details, ["fresh exact-image container could not be created"]
        identity_path.write_text(container_id + "\n", encoding="ascii")
        identity_path.chmod(0o444)
        prestart_inspect = _run_host_command(
            ["docker", "inspect", container_id], timeout=30
        )
        image_inspect = _run_host_command(
            ["docker", "image", "inspect", image_digest], timeout=30
        )
        start = _run_host_command(["docker", "start", "--attach", container_id], timeout=1200)
        inspect = _run_host_command(["docker", "inspect", container_id], timeout=30)
        try:
            initial_docs = json.loads(prestart_inspect.stdout)
            docs = json.loads(inspect.stdout)
            image_docs = json.loads(image_inspect.stdout)
            initial_doc = initial_docs[0]
            final_doc = docs[0]
            image_doc = image_docs[0]
            initial_state = initial_doc["State"]
            state = final_doc["State"]
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"fresh exact-image raw inspection is invalid: {exc}") from exc
        if (
            start.returncode != 0
            or initial_doc.get("Id") != container_id
            or initial_doc.get("Image") != image_digest
            or initial_state.get("Status") != "created"
            or initial_state.get("Running") is not False
            or initial_doc.get("RestartCount") != 0
            or final_doc.get("Id") != container_id
            or final_doc.get("Image") != image_digest
            or state.get("Status") != "exited"
            or state.get("Running") is not False
            or state.get("Paused") is not False
            or state.get("Restarting") is not False
            or state.get("OOMKilled") is not False
            or state.get("Dead") is not False
            or state.get("ExitCode") != 0
            or final_doc.get("RestartCount") != 0
            or image_doc.get("Id") != image_digest
        ):
            failures.append("fresh exact-image re-execution lifecycle/image is invalid")
        config = final_doc.get("Config") if isinstance(final_doc.get("Config"), dict) else {}
        host = final_doc.get("HostConfig") if isinstance(final_doc.get("HostConfig"), dict) else {}
        environment, environment_failures = _environment_map(config.get("Env"))
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
        if (
            environment != expected_environment
            or config.get("Image") != image_digest
            or config.get("User") != "ubuntu"
            or config.get("Entrypoint") != ["/ros_entrypoint.sh"]
            or config.get("Cmd")
            != [
                "scripts/acceptance_entrypoint.sh",
                "network/scripts/run_m0_host_reexecution.sh",
                run_id,
            ]
            or config.get("WorkingDir") != CONTAINER_WORKDIR
            or host.get("Privileged") is not False
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
            failures.append("fresh exact-image Config/HostConfig is not exact")
        mounts = final_doc.get("Mounts") if isinstance(final_doc.get("Mounts"), list) else []
        by_destination = {
            mount.get("Destination"): mount
            for mount in mounts
            if isinstance(mount, dict)
        }
        expected_mounts = {
            "/run/ams/container_id": (str(identity_path.resolve(strict=True)), False),
            "/run/ams/m0-artifacts": (str(output_root.resolve(strict=True)), True),
            CONTAINER_WORKDIR: (str(Path(source_snapshot).resolve(strict=True)), False),
            f"{CONTAINER_WORKDIR}/.external/ns-3": (
                str((ROOT_DIR / ".external/ns-3").resolve(strict=True)),
                False,
            ),
        }
        if len(mounts) != 4 or set(by_destination) != set(expected_mounts):
            failures.append("fresh exact-image mount destination set is not exact")
        for destination, (expected_source, writable) in expected_mounts.items():
            mount = by_destination.get(destination, {})
            if (
                mount.get("Type") != "bind"
                or mount.get("Source") != expected_source
                or mount.get("RW") is not writable
                or mount.get("Mode") != ("rw" if writable else "ro")
                or mount.get("Propagation") != "rprivate"
            ):
                failures.append(f"fresh exact-image mount is invalid: {destination}")
        if _container_immutable_fingerprint(initial_doc) != _container_immutable_fingerprint(final_doc):
            failures.append("fresh exact-image immutable configuration changed")

        snapshot_before, snapshot_before_error = _snapshot_artifact_tree(output_root)
        if snapshot_before_error or snapshot_before is None:
            raise ValueError(snapshot_before_error or "fresh output snapshot is unavailable")

        def read_output(
            relative: str, *, maximum: int = MAX_TEST_LOG_BYTES, allow_empty: bool = False
        ) -> bytes:
            payload, info, error = _read_bytes_fd(
                output_root / relative,
                maximum_bytes=maximum,
                allow_empty=allow_empty,
                additional_root=output_root,
            )
            expected = snapshot_before["entries"].get(relative)
            if error or payload is None or info is None or not isinstance(expected, dict):
                raise ValueError(error or f"fresh output is absent from snapshot: {relative}")
            if (
                expected.get("kind") != "file"
                or expected.get("device") != info.st_dev
                or expected.get("inode") != info.st_ino
                or expected.get("links") != info.st_nlink
                or expected.get("bytes") != len(payload)
                or expected.get("sha256") != hashlib.sha256(payload).hexdigest()
            ):
                raise ValueError(f"fresh output identity differs from snapshot: {relative}")
            return payload

        dep_out = read_output("check_deps.stdout").decode("utf-8")
        dep_err = read_output("check_deps.stderr", allow_empty=True).decode("utf-8")
        dep_exit = read_output("check_deps.exit_code", maximum=32).decode("ascii")
        dep_failures, records, warnings, dep_hash = _parse_dependency_output(dep_out)
        if dep_err or dep_exit != "0\n":
            dep_failures.append("fresh dependency command output/exit is not exact")
        failures.extend(f"fresh dependency: {item}" for item in dep_failures)

        runtime_raw = read_output("runtime_lock.json", maximum=MAX_RUNTIME_LOCK_BYTES)
        runtime = json.loads(runtime_raw.decode("utf-8"))
        if (
            read_output("runtime_lock.stderr", allow_empty=True)
            or read_output("runtime_lock.exit_code", maximum=32).decode("ascii") != "0\n"
            or runtime.get("passed") is not True
            or runtime.get("failures") != []
        ):
            failures.append("fresh runtime-lock verification did not pass exactly")

        guard = json.loads(
            read_output("python_guard.json", maximum=MAX_TEST_RESULT_BYTES).decode("utf-8")
        )
        expected_guard = {
            "guard_marker": True,
            "no_site": 0,
            "sitecustomize_path": f"{CONTAINER_WORKDIR}/network/scripts/m0_python_guard/sitecustomize.py",
            "usercustomize_loaded": False,
        }
        if guard != expected_guard:
            failures.append("fresh child-Python guard trace is not exact")

        suite_stdout = read_output("suite_runner.stdout").decode("utf-8")
        suite_stderr = read_output("suite_runner.stderr", allow_empty=True).decode(
            "utf-8"
        )
        suite_exit = read_output("suite_runner.exit_code", maximum=32).decode("ascii")
        manifest, _manifest_hash = load_frozen_test_manifest(ROOT_DIR)
        expected_ids = list(manifest["ordered_test_ids"])
        suite_document_raw = read_output(
            f"{run_id}/metrics/m0_validation_suite.json",
            maximum=MAX_TEST_RESULT_BYTES,
        )
        suite_log_raw = read_output(
            f"{run_id}/logs/m0_validation_suite.log", maximum=MAX_TEST_LOG_BYTES
        )
        suite_document = json.loads(suite_document_raw.decode("utf-8"))
        suite_failures, raw_passing_ids, suite_hash = _parse_host_unittest_output(
            suite_log_raw.decode("utf-8"), expected_ids
        )
        outcomes = suite_document.get("execution", {}).get("outcomes", [])
        passing_ids = [
            record.get("test_id")
            for record in outcomes
            if isinstance(record, dict) and record.get("outcome") == "passed"
        ]
        try:
            import_policy, import_policy_sha256 = load_m0_import_policy(ROOT_DIR)
            trace_failures = validate_m0_import_trace_record(
                suite_document.get("python_import_trace"),
                import_policy,
                import_policy_sha256,
                run_id,
                suite_source_bindings(ROOT_DIR),
            )
        except (OSError, ValueError, yaml.YAMLError) as exc:
            trace_failures = [f"fresh Python import trace could not be rederived: {exc}"]
        suite_failures.extend(trace_failures)
        expected_stdout = (
            f"M0 validation/adversarial suite recorded {len(expected_ids)} tests; "
            "passed=true\n"
        )
        if (
            suite_stdout != expected_stdout
            or suite_stderr
            or suite_exit != "0\n"
            or suite_document.get("schema_version") != 5
            or suite_document.get("producer_observation") != {"passed": True}
            or suite_document.get("discovery", {}).get("test_ids") != expected_ids
            or passing_ids != expected_ids
            or raw_passing_ids != expected_ids
        ):
            suite_failures.append("fresh frozen suite record/output/exit is not exact")
        failures.extend(f"fresh suite: {item}" for item in suite_failures)
        snapshot_after, snapshot_after_error = _snapshot_artifact_tree(output_root)
        if snapshot_after_error or snapshot_after != snapshot_before:
            failures.append(
                snapshot_after_error or "fresh output tree changed while host-final read it"
            )
        raw_values = {
            "fresh/initial_container_inspect.json": prestart_inspect.stdout.encode("utf-8"),
            "fresh/final_container_inspect.json": inspect.stdout.encode("utf-8"),
            "fresh/image_inspect.json": image_inspect.stdout.encode("utf-8"),
            "fresh/container_stdout.txt": start.stdout.encode("utf-8"),
            "fresh/container_stderr.txt": start.stderr.encode("utf-8"),
            "fresh/operational_snapshot_before.json": (
                json.dumps(snapshot_before, indent=2, sort_keys=True) + "\n"
            ).encode("utf-8"),
            "fresh/operational_snapshot_after.json": (
                json.dumps(snapshot_after, indent=2, sort_keys=True) + "\n"
            ).encode("utf-8"),
        }
        for relative, payload in raw_values.items():
            _write_host_validation_file(host_validation_dir, relative, payload)
        for relative, entry in sorted(snapshot_before["entries"].items()):
            if entry.get("kind") != "file":
                continue
            payload = read_output(
                relative,
                maximum=max(MAX_PROVENANCE_BYTES, int(entry.get("bytes", 0))),
                allow_empty=entry.get("bytes") == 0,
            )
            _write_host_validation_file(
                host_validation_dir, f"fresh/output/{relative}", payload
            )
        details = {
            "container_id": container_id,
            "image_digest": image_digest,
            "exit_code": state.get("ExitCode"),
            "dependency_record_count": len(records),
            "dependency_warning_count": warnings,
            "dependency_stdout_sha256": dep_hash,
            "runtime_lock_sha256": hashlib.sha256(
                runtime_raw
            ).hexdigest(),
            "passing_test_count": len(passing_ids),
            "unittest_stderr_sha256": suite_hash,
            "python_import_trace": suite_document.get("python_import_trace"),
            "python_import_trace_sha256": hashlib.sha256(
                json.dumps(
                    suite_document.get("python_import_trace"),
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
            "python_guard": guard,
            "artifact_snapshot_before": snapshot_before,
            "artifact_snapshot_after": snapshot_after,
            "prestart_container_inspect_sha256": hashlib.sha256(
                prestart_inspect.stdout.encode("utf-8")
            ).hexdigest(),
            "final_container_inspect_sha256": hashlib.sha256(
                inspect.stdout.encode("utf-8")
            ).hexdigest(),
            "container_stdout_sha256": hashlib.sha256(
                start.stdout.encode("utf-8")
            ).hexdigest(),
            "container_stderr_sha256": hashlib.sha256(
                start.stderr.encode("utf-8")
            ).hexdigest(),
            "raw_sha256": {
                relative: hashlib.sha256(payload).hexdigest()
                for relative, payload in sorted(raw_values.items())
            },
        }
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        failures.append(f"fresh exact-image re-execution failed: {exc}")
    finally:
        if container_id:
            _run_host_command(["docker", "rm", "-f", container_id], timeout=30)
        try:
            identity_path.chmod(0o600)
            identity_path.unlink()
        except OSError:
            pass
        shutil.rmtree(output_root, ignore_errors=True)
    return details, failures


def host_final_gate(
    run_dir: Path,
    expected_container_id: str,
    *,
    initial_control_dir: Path,
    host_validation_dir: Path | None = None,
) -> dict[str, Any]:
    failures: list[str] = []
    owned_host_validation = False
    fresh_source_path: Path | None = None
    if _inside_docker_runtime():
        return gate("failed", "host-final must execute on the Docker host", {"failures": ["/.dockerenv is present"]})
    if CONTAINER_ID.fullmatch(expected_container_id) is None:
        return gate("failed", "host-final container ID is malformed", {"failures": ["full lowercase container ID required"]})
    if host_validation_dir is None:
        host_validation_dir = Path(
            tempfile.mkdtemp(prefix=f"ams-m0-host-validation-{run_dir.name}.")
        )
        owned_host_validation = True
    try:
        if (
            host_validation_dir.is_symlink()
            or not host_validation_dir.is_dir()
            or any(host_validation_dir.iterdir())
        ):
            failures.append("host-validation staging is not one initially empty real directory")
    except OSError as exc:
        failures.append(f"host-validation staging is invalid: {exc}")
    try:
        expected_parent = ROOT_DIR.parent.resolve(strict=True)
        if (
            initial_control_dir.parent.resolve(strict=True) != expected_parent
            or re.fullmatch(
                rf"\.ams-m0-control-{re.escape(run_dir.name)}\.[A-Za-z0-9]{{8,}}",
                initial_control_dir.name,
            )
            is None
        ):
            failures.append("prestart control directory is not the canonical never-mounted path")
    except (OSError, RuntimeError):
        failures.append("prestart control directory is missing")
    before_source, source_error = _host_checkout_snapshot()
    if source_error:
        failures.append(source_error)
    try:
        artifact_parent_names = sorted(os.listdir(run_dir.parent))
        if artifact_parent_names != [run_dir.name]:
            failures.append("captured artifact root does not contain exactly one run child")
    except OSError as exc:
        failures.append(f"captured artifact root cannot be enumerated: {exc}")
    artifact_before, artifact_error = _snapshot_artifact_tree(run_dir)
    if artifact_error:
        failures.append(artifact_error)
    external_before = suite_external_bindings(ROOT_DIR)

    rederived_captured_gates = {
        "dependency_check": dependency_gate(run_dir),
        "runtime_lock": runtime_lock_gate(run_dir),
        "validation_adversarial_suite": validation_suite_gate(run_dir),
        "provenance": provenance_gate(run_dir),
    }
    for name, record in rederived_captured_gates.items():
        if record.get("status") != "passed":
            failures.append(f"host-final rederived captured gate failed: {name}")
    provenance, provenance_error = _read_json(
        run_dir / "metrics/provenance.json", maximum_bytes=MAX_PROVENANCE_BYTES
    )
    suite, suite_error = _read_json(
        run_dir / "metrics/m0_validation_suite.json", maximum_bytes=MAX_TEST_RESULT_BYTES
    )
    failures.extend(error for error in (provenance_error, suite_error) if error)
    container = provenance.get("container_image", {}) if isinstance(provenance, dict) else {}
    identity = suite.get("execution_identity", {}) if isinstance(suite, dict) else {}
    image_digest = container.get("digest")
    source_commit = provenance.get("git_commit") if isinstance(provenance, dict) else None
    image_reference = container.get("reference")
    if (
        not isinstance(image_digest, str)
        or IMAGE_DIGEST.fullmatch(image_digest) is None
        or source_commit != (before_source or {}).get("git_commit")
        or container.get("runtime_container_id") != expected_container_id
        or identity.get("runtime_container_id") != expected_container_id
        or identity.get("source_commit") != source_commit
    ):
        failures.append("captured provenance/suite/source/container identities are not coherent")

    host_execution_identity, host_execution_failures = _host_execution_identity(
        before_source
    )
    failures.extend(host_execution_failures)

    retained: dict[str, Any] = {}
    retained_source_identity: dict[str, Any] | None = None
    if not failures:
        retained, inspect_failures = _inspect_retained_container(
            expected_container_id=expected_container_id,
            expected_image=image_digest,
            run_id=run_dir.name,
            run_dir=run_dir,
            source_commit=source_commit,
            image_reference=str(image_reference),
            initial_control_dir=initial_control_dir,
        )
        failures.extend(inspect_failures)
        retained_source_identity, retained_source_error = _checkout_snapshot(
            Path(retained.get("source_snapshot", "")), str(source_commit)
        )
        if retained_source_error:
            failures.append(retained_source_error)
        if (
            before_source is not None
            and retained_source_identity is not None
            and retained_source_identity.get("source_binding_sha256")
            != before_source.get("source_binding_sha256")
        ):
            failures.append("producer-exposed source snapshot differs from technical source")

    fresh_source_before: dict[str, Any] | None = None
    if not failures:
        fresh_source_path, fresh_source_before, fresh_source_error = (
            _create_fresh_host_source_snapshot(str(source_commit))
        )
        if fresh_source_error or fresh_source_path is None:
            failures.append(fresh_source_error or "fresh host source is unavailable")
        elif (
            before_source is not None
            and fresh_source_before is not None
            and fresh_source_before.get("source_binding_sha256")
            != before_source.get("source_binding_sha256")
        ):
            failures.append("fresh host source snapshot differs from technical source")
    fresh: dict[str, Any] = {}
    if not failures and fresh_source_path is not None:
        fresh, fresh_failures = _run_in_fresh_exact_image(
            image_digest,
            run_id=run_dir.name,
            source_snapshot=str(fresh_source_path),
            source_commit=source_commit,
            host_validation_dir=host_validation_dir,
        )
        failures.extend(fresh_failures)
        if (
            isinstance(suite, dict)
            and suite.get("python_import_trace")
            != fresh.get("python_import_trace")
        ):
            failures.append(
                "captured and fresh exact-image Python import traces differ"
            )

    fresh_source_after: dict[str, Any] | None = None
    if fresh_source_path is not None:
        fresh_source_after, fresh_source_after_error = _checkout_snapshot(
            fresh_source_path, str(source_commit)
        )
        if fresh_source_after_error:
            failures.append(fresh_source_after_error)
        if fresh_source_before != fresh_source_after:
            failures.append("fresh host source identity changed during re-execution")

    capability: dict[str, Any] = {}
    if isinstance(image_digest, str) and not failures:
        capability, capability_failures = _run_isolated_capability_probe(
            image_digest, host_validation_dir=host_validation_dir
        )
        failures.extend(capability_failures)

    # Re-inspect both mutable trust boundaries after the independent run.
    retained_after: dict[str, Any] = {}
    if isinstance(image_digest, str) and not failures:
        retained_after, after_failures = _inspect_retained_container(
            expected_container_id=expected_container_id,
            expected_image=image_digest,
            run_id=run_dir.name,
            run_dir=run_dir,
            source_commit=str(source_commit),
            image_reference=str(image_reference),
            initial_control_dir=initial_control_dir,
            host_validation_dir=host_validation_dir,
        )
        failures.extend(after_failures)
        if retained_after.get("immutable_fingerprint_sha256") != retained.get(
            "immutable_fingerprint_sha256"
        ):
            failures.append("retained container changed during host-final re-execution")
    artifact_after, after_artifact_error = _snapshot_artifact_tree(run_dir)
    after_source, after_source_error = _host_checkout_snapshot()
    external_after = suite_external_bindings(ROOT_DIR)
    failures.extend(
        error for error in (after_artifact_error, after_source_error) if error
    )
    if artifact_before is not None and artifact_after != artifact_before:
        failures.append("captured artifact tree changed during host-final")
    if before_source is not None and after_source != before_source:
        failures.append("host technical source identity changed during host-final")
    if external_after != external_before:
        failures.append("external ns-3 source identity changed during host-final")
    try:
        if sorted(os.listdir(run_dir.parent)) != [run_dir.name]:
            failures.append("captured artifact root gained an unexpected sibling")
    except OSError as exc:
        failures.append(f"captured artifact root postcondition failed: {exc}")

    artifact_content_manifest: dict[str, Any] | None = None
    if artifact_after is not None:
        try:
            artifact_content_manifest = _portable_content_manifest(artifact_after)
        except ValueError as exc:
            failures.append(str(exc))
    source_raw = {
        "schema_version": 1,
        "contract": "ams.m0.host-source-reexecution/v1",
        "technical_source_before": before_source,
        "technical_source_after": after_source,
        "producer_source": retained_source_identity,
        "fresh_source_before": fresh_source_before,
        "fresh_source_after": fresh_source_after,
        "external_before": external_before,
        "external_after": external_after,
    }
    try:
        _write_host_validation_file(
            host_validation_dir,
            "source/identity.json",
            (json.dumps(source_raw, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )
        _write_host_validation_file(
            host_validation_dir,
            "execution/host_identity.json",
            (
                json.dumps(host_execution_identity, indent=2, sort_keys=True)
                + "\n"
            ).encode("utf-8"),
        )
        pre_manifest = _host_validation_content_manifest(host_validation_dir)
        _write_host_validation_file(
            host_validation_dir,
            "content_manifest.json",
            (json.dumps(pre_manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )
        host_validation_manifest = _host_validation_content_manifest(
            host_validation_dir
        )
    except (OSError, RuntimeError, ValueError) as exc:
        failures.append(f"host-validation raw manifest failed: {exc}")
        host_validation_manifest = None
    details = {
        "failures": failures,
        "expected_container_id": expected_container_id,
        "image_digest": image_digest,
        "source_before": before_source,
        "source_after": after_source,
        "artifact_before": artifact_before,
        "artifact_after": artifact_after,
        "artifact_content_manifest": artifact_content_manifest,
        "rederived_captured_gates": rederived_captured_gates,
        "external_before": external_before,
        "external_after": external_after,
        "producer_source_identity": retained_source_identity,
        "fresh_source_before": fresh_source_before,
        "fresh_source_after": fresh_source_after,
        "retained_container_initial_final": retained,
        "retained_container_reinspection": retained_after,
        "fresh_exact_image_reexecution": fresh,
        "isolated_target_runtime_capability": capability,
        "host_validation_content_manifest": host_validation_manifest,
        "host_execution_identity": host_execution_identity,
    }
    result = gate(
        "failed" if failures else "passed",
        "host-final exact-runtime qualification failed" if failures else
        "unprivileged exact-runtime re-execution and isolated capability proof passed",
        details,
    )
    _remove_host_temp_tree(fresh_source_path)
    if owned_host_validation:
        _remove_host_temp_tree(host_validation_dir)
    return result


def _rename_noreplace(directory_fd: int, source_name: str, target_name: str) -> None:
    if (
        not source_name
        or not target_name
        or "/" in source_name
        or "/" in target_name
        or "\x00" in source_name + target_name
    ):
        raise ValueError("atomic publish names are unsafe")
    libc = ctypes.CDLL(None, use_errno=True)
    syscall = getattr(libc, "syscall", None)
    if syscall is None:
        raise OSError(errno.ENOSYS, "renameat2 syscall is unavailable")
    syscall.restype = ctypes.c_long
    if syscall(
        316,  # SYS_renameat2 on the dependency-locked Linux x86_64 ABI.
        directory_fd,
        ctypes.c_char_p(os.fsencode(source_name)),
        directory_fd,
        ctypes.c_char_p(os.fsencode(target_name)),
        1,  # RENAME_NOREPLACE
    ) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), target_name)


def _publish_host_receipt(
    run_dir: Path,
    publish_run_dir: Path,
    receipt: dict[str, Any],
    *,
    host_validation_dir: Path,
) -> tuple[Path | None, str | None]:
    """Publish only after every fallible validation/readback step completes."""

    temporary: Path | None = None
    try:
        expected = (ROOT_DIR / "runs" / run_dir.name).resolve(strict=False)
        if publish_run_dir.resolve(strict=False) != expected:
            raise ValueError("publish run directory is not the canonical runs/RUN_ID path")
        runs_root = ROOT_DIR / "runs"
        if runs_root.is_symlink() or not runs_root.is_dir():
            raise ValueError("runs root is not a real directory")
        if publish_run_dir.exists() or publish_run_dir.is_symlink():
            raise FileExistsError("published run already exists")
        source_snapshot, source_error = _snapshot_artifact_tree(run_dir)
        if source_error or source_snapshot is None:
            raise ValueError(source_error or "source artifact snapshot unavailable")
        host_details = receipt.get("gates", {}).get("host_final", {}).get(
            "details", {}
        )
        if source_snapshot != host_details.get("artifact_after"):
            raise ValueError("publish input differs from the host-final artifact snapshot")
        expected_artifact_content = host_details.get("artifact_content_manifest")
        if _portable_content_manifest(source_snapshot) != expected_artifact_content:
            raise ValueError("validated portable artifact manifest is inconsistent")
        expected_host_manifest = host_details.get(
            "host_validation_content_manifest"
        )
        current_host_manifest = _host_validation_content_manifest(
            host_validation_dir
        )
        if current_host_manifest != expected_host_manifest:
            raise ValueError("host-validation raw tree changed before publication")

        temporary = Path(
            tempfile.mkdtemp(prefix=f".{run_dir.name}.publish-", dir=runs_root)
        )
        try:
            entries = source_snapshot["entries"]
            for relative, entry in sorted(
                entries.items(), key=lambda item: (item[0].count("/"), item[0])
            ):
                destination = temporary / relative
                if entry["kind"] == "directory":
                    destination.mkdir(mode=0o700)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                payload, _, read_error = _read_bytes_fd(
                    run_dir / relative,
                    maximum_bytes=max(MAX_PROVENANCE_BYTES, int(entry["bytes"])),
                    allow_empty=entry["bytes"] == 0,
                    additional_root=run_dir,
                )
                if read_error or payload is None:
                    raise ValueError(read_error or f"could not copy {relative}")
                if (
                    len(payload) != entry["bytes"]
                    or hashlib.sha256(payload).hexdigest() != entry["sha256"]
                ):
                    raise ValueError(f"artifact changed while publishing: {relative}")
                descriptor = os.open(
                    destination,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
                    0o600,
                )
                try:
                    view = memoryview(payload)
                    while view:
                        written = os.write(descriptor, view)
                        if written < 1:
                            raise OSError(f"short publish write: {relative}")
                        view = view[written:]
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)

            # Reproduce and verify the accepted technical tree before adding
            # any host-final outputs.
            copied_snapshot, copied_error = _snapshot_artifact_tree(temporary)
            if copied_error or copied_snapshot is None:
                raise ValueError(copied_error or "copied technical snapshot failed")
            if _portable_content_manifest(copied_snapshot) != expected_artifact_content:
                raise ValueError("published technical content differs from validated content")

            host_validation = temporary / "host_validation"
            host_validation.mkdir(mode=0o700)
            host_snapshot, host_snapshot_error = _snapshot_artifact_tree(
                host_validation_dir
            )
            if host_snapshot_error or host_snapshot is None:
                raise ValueError(host_snapshot_error or "host raw snapshot unavailable")
            for relative, entry in sorted(
                host_snapshot["entries"].items(),
                key=lambda item: (item[0].count("/"), item[0]),
            ):
                destination = host_validation / relative
                if entry["kind"] == "directory":
                    destination.mkdir(mode=0o700)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                payload, _, error = _read_bytes_fd(
                    host_validation_dir / relative,
                    maximum_bytes=max(MAX_PROVENANCE_BYTES, int(entry["bytes"])),
                    allow_empty=entry["bytes"] == 0,
                    additional_root=host_validation_dir,
                )
                if error or payload is None:
                    raise ValueError(error or f"host raw copy failed: {relative}")
                if (
                    len(payload) != entry["bytes"]
                    or hashlib.sha256(payload).hexdigest() != entry["sha256"]
                ):
                    raise ValueError(f"host raw changed while copying: {relative}")
                descriptor = os.open(
                    destination,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
                    0o600,
                )
                try:
                    view = memoryview(payload)
                    while view:
                        written = os.write(descriptor, view)
                        if written < 1:
                            raise OSError(f"short host raw publish write: {relative}")
                        view = view[written:]
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)

            receipt_path = temporary / "metrics/m0_host_final_receipt.json"
            receipt["published_run_dir"] = f"runs/{run_dir.name}"
            receipt["run_dir"] = receipt["published_run_dir"]
            receipt["receipt_path"] = (
                f"runs/{run_dir.name}/metrics/m0_host_final_receipt.json"
            )
            receipt["host_validation_raw"] = {
                "path": f"runs/{run_dir.name}/host_validation",
                "never_mounted": True,
                "contract": current_host_manifest["contract"],
                "file_count": current_host_manifest["file_count"],
                "content_sha256": current_host_manifest["content_sha256"],
                "files": current_host_manifest["files"],
            }
            payload = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8")
            receipt_temporary = receipt_path.with_name(".m0_host_final_receipt.tmp")
            descriptor = os.open(
                receipt_temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o400,
            )
            try:
                view = memoryview(payload)
                while view:
                    written = os.write(descriptor, view)
                    if written < 1:
                        raise OSError("short host receipt write")
                    view = view[written:]
                os.fsync(descriptor)
                os.fchmod(descriptor, 0o400)
            finally:
                os.close(descriptor)
            os.rename(receipt_temporary, receipt_path)

            # Finish modes, file fsync and every nested directory fsync before
            # the one no-replace acceptance rename.
            for path in sorted(temporary.rglob("*"), reverse=True):
                if path.is_file():
                    path.chmod(0o400)
                elif path.is_dir():
                    path.chmod(0o500)
            temporary.chmod(0o500)
            for path in sorted(temporary.rglob("*")):
                if path.is_file():
                    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
            for directory in sorted(
                [path for path in temporary.rglob("*") if path.is_dir()],
                key=lambda path: len(path.parts),
                reverse=True,
            ) + [temporary]:
                directory_fd = os.open(
                    directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                )
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)

            copied_host_manifest = _host_validation_content_manifest(host_validation)
            if copied_host_manifest != current_host_manifest:
                raise ValueError("published host-validation content failed readback")
            source_after, after_error = _snapshot_artifact_tree(run_dir)
            if after_error or source_after != source_snapshot:
                raise ValueError(after_error or "captured artifact changed during publish")
            host_after = _host_validation_content_manifest(host_validation_dir)
            if host_after != current_host_manifest:
                raise ValueError("host-validation staging changed during publish")
            receipt_raw, _, receipt_error = _read_bytes_fd(
                receipt_path,
                maximum_bytes=MAX_PROVENANCE_BYTES,
                additional_root=temporary,
            )
            if receipt_error or receipt_raw != payload:
                raise ValueError(receipt_error or "host receipt readback differs")

            root_fd = os.open(
                runs_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            try:
                os.fsync(root_fd)
                _rename_noreplace(root_fd, temporary.name, publish_run_dir.name)
                try:
                    os.fsync(root_fd)
                except OSError:
                    quarantine = f".{run_dir.name}.failed-{os.getpid()}"
                    os.rename(
                        publish_run_dir.name,
                        quarantine,
                        src_dir_fd=root_fd,
                        dst_dir_fd=root_fd,
                    )
                    os.fsync(root_fd)
                    raise
            finally:
                os.close(root_fd)
            temporary = None
        except Exception:
            _remove_host_temp_tree(temporary)
            raise
        return publish_run_dir / "metrics/m0_host_final_receipt.json", None
    except (OSError, RuntimeError, ValueError) as exc:
        _remove_host_temp_tree(temporary)
        return None, f"atomic host receipt publication failed: {exc}"


def evaluate_m0_baseline(
    run_dir: Path,
    *,
    require_host_final: bool = False,
    expected_container_id: str | None = None,
    initial_control_dir: Path | None = None,
    host_validation_dir: Path | None = None,
) -> dict[str, Any]:
    safe_run_dir, input_error = _safe_run_directory(
        run_dir, allow_staging=require_host_final
    )
    base_names = (
        "dependency_check",
        "runtime_lock",
        "validation_adversarial_suite",
        "provenance",
    )
    if input_error or safe_run_dir is None:
        input_failure = input_error or "run directory validation failed"
        gates = {
            name: gate("failed", "unsafe probe input", {"failures": [input_failure]})
            for name in base_names
        }
        run_id = run_dir.name
        resolved_text = str(run_dir)
    else:
        gates = {
            "dependency_check": dependency_gate(safe_run_dir),
            "runtime_lock": runtime_lock_gate(safe_run_dir),
            "validation_adversarial_suite": validation_suite_gate(safe_run_dir),
            "provenance": provenance_gate(safe_run_dir),
        }
        run_id = safe_run_dir.name
        resolved_text = str(safe_run_dir)

    if require_host_final:
        base_passed = all(record.get("status") == "passed" for record in gates.values())
        if not base_passed or safe_run_dir is None:
            gates["host_final"] = gate(
                "failed",
                "host-final validation requires all captured M0 gates to pass first",
            )
        elif expected_container_id is None or initial_control_dir is None:
            gates["host_final"] = gate(
                "failed",
                "host-final requires --expected-container-id and --initial-control-dir",
            )
        else:
            host_kwargs: dict[str, Any] = {
                "initial_control_dir": initial_control_dir,
            }
            if host_validation_dir is not None:
                host_kwargs["host_validation_dir"] = host_validation_dir
            gates["host_final"] = host_final_gate(
                safe_run_dir, expected_container_id, **host_kwargs
            )
            host_record = gates["host_final"]
            rederived = host_record.get("details", {}).get(
                "rederived_captured_gates"
            )
            initially_derived = {name: gates[name] for name in base_names}
            if host_record.get("status") == "passed" and rederived != initially_derived:
                details = host_record.get("details")
                if not isinstance(details, dict):
                    details = {}
                    host_record["details"] = details
                host_failures = details.get("failures")
                if not isinstance(host_failures, list):
                    host_failures = []
                    details["failures"] = host_failures
                host_failures.append(
                    "captured M0 gates changed between initial and host-final derivation"
                )
                host_record["status"] = "failed"
                host_record["proof"] = (
                    "captured M0 gates were not stable across host-final"
                )

    failures = [
        f"{name}: {record.get('proof', 'gate failed')}"
        for name, record in gates.items()
        if record.get("status") != "passed"
    ]
    captured_qualified = all(
        gates.get(name, {}).get("status") == "passed" for name in base_names
    )
    formal_accepted = (
        require_host_final
        and captured_qualified
        and gates.get("host_final", {}).get("status") == "passed"
    )
    source_record = (
        gates.get("host_final", {}).get("details", {}).get("source_after", {})
        if formal_accepted
        else {}
    )
    contract_payload = {
        "run_id": run_id,
        "source": source_record,
        "image_digest": gates.get("host_final", {}).get("details", {}).get(
            "image_digest"
        ),
        "artifact_content_sha256": gates.get("host_final", {})
        .get("details", {})
        .get("artifact_content_manifest", {})
        .get("content_sha256"),
        "host_validation_content_sha256": gates.get("host_final", {})
        .get("details", {})
        .get("host_validation_content_manifest", {})
        .get("content_sha256"),
        "isolated_capability_contract": gates.get("host_final", {})
        .get("details", {})
        .get("isolated_target_runtime_capability", {})
        .get("contract"),
        "host_execution_identity_sha256": hashlib.sha256(
            json.dumps(
                gates.get("host_final", {})
                .get("details", {})
                .get("host_execution_identity"),
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
        "consumed_nodes": ["Q0"],
    }
    result = {
        "schema_version": 3,
        "contract": HOST_RECEIPT_CONTRACT if require_host_final else "ams.m0.captured-qualification/v1",
        "probe": "m0_dependency_provenance",
        "milestone": "M0",
        "run_id": run_id,
        "run_dir": resolved_text,
        "scope": {
            "dependency_check": True,
            "runtime_lock": True,
            "validation_adversarial_suite": True,
            "provenance": True,
            "host_final": require_host_final,
            "packet_path": False,
            "sealing": False,
            "attestation": False,
        },
        "p0_eligible": False,
        "captured_qualified": captured_qualified,
        "formal_accepted": formal_accepted,
        "passed": formal_accepted,
        "consumed_nodes": ["Q0"],
        "qualification_content_vector": source_record.get(
            "qualification_content_vector"
        ),
        "plan_contract": {
            "plan_version": 3,
            "path": source_record.get("plan_path"),
            "contract_sha256": source_record.get("plan_sha256"),
        }
        if formal_accepted
        else None,
        "qualification_contract_sha256": hashlib.sha256(
            json.dumps(contract_payload, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        if formal_accepted
        else None,
        "failures": failures,
        "gates": gates,
    }
    if not formal_accepted and not failures:
        result["failures"] = ["host-final qualification has not executed"]
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument(
        "--require-host-final",
        action="store_true",
        help="require retained-container inspection and fresh exact-image host re-execution",
    )
    parser.add_argument(
        "--expected-container-id",
        help="full host-observed retained Docker container ID (required with --require-host-final)",
    )
    parser.add_argument("--initial-control-dir", type=Path)
    parser.add_argument("--publish-run-dir", type=Path)
    parser.add_argument(
        "--captured-producer-mode",
        action="store_true",
        help="emit a non-formal captured receipt; passed/formal_accepted remain false",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    host_validation_staging: Path | None = None
    try:
        if args.require_host_final:
            host_validation_staging = Path(
                tempfile.mkdtemp(
                    prefix=f".ams-m0-host-validation-{args.run_dir.name}.",
                    dir=ROOT_DIR.parent,
                )
            )
        result = evaluate_m0_baseline(
            args.run_dir,
            require_host_final=args.require_host_final,
            expected_container_id=args.expected_container_id,
            initial_control_dir=args.initial_control_dir,
            host_validation_dir=host_validation_staging,
        )
        if args.require_host_final and result.get("formal_accepted") is True:
            if args.publish_run_dir is None:
                raise ValueError("host-final requires --publish-run-dir")
            receipt_path, publish_error = _publish_host_receipt(
                args.run_dir,
                args.publish_run_dir,
                result,
                host_validation_dir=host_validation_staging,
            )
            if publish_error or receipt_path is None:
                raise ValueError(publish_error or "host receipt was not published")
            # The dict is mutated by _publish_host_receipt before serialization.
            result["run_dir"] = result["published_run_dir"]
        elif args.publish_run_dir is not None and not args.require_host_final:
            raise ValueError("--publish-run-dir requires --require-host-final")
    except Exception as exc:  # Preserve a machine-readable fail-closed result.
        result = {
            "schema_version": 3,
            "contract": HOST_RECEIPT_CONTRACT if args.require_host_final else "ams.m0.captured-qualification/v1",
            "probe": "m0_dependency_provenance",
            "milestone": "M0",
            "run_id": args.run_dir.name,
            "run_dir": str(args.run_dir),
            "scope": {
                "dependency_check": True,
                "validation_adversarial_suite": True,
                "provenance": True,
                "host_final": args.require_host_final,
                "packet_path": False,
                "sealing": False,
                "attestation": False,
            },
            "p0_eligible": False,
            "captured_qualified": False,
            "formal_accepted": False,
            "passed": False,
            "failures": [f"validator exception: {exc}"],
            "gates": {},
        }
    finally:
        _remove_host_temp_tree(host_validation_staging)
    print(json.dumps(result, indent=2, sort_keys=True))
    if args.captured_producer_mode:
        if args.require_host_final:
            return 1
        return 0 if result.get("captured_qualified") is True and result.get("passed") is False else 1
    return 0 if result.get("passed") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
