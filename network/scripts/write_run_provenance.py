#!/usr/bin/env python3
"""Write deterministic source/config/dependency provenance for one run."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import platform
import re
import stat
import subprocess
import sys
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Iterable

import yaml


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from network.validation.qualification_identity import (  # noqa: E402
    BOUNDED_ROOT_CAPABILITY_MASK,
    BOUNDED_ROOT_GID,
    BOUNDED_ROOT_IN_RUNTIME_MODE,
    BOUNDED_ROOT_IN_RUNTIME_PROFILES,
    BOUNDED_ROOT_NO_NEW_PRIVS,
    BOUNDED_ROOT_UID,
    DEFERRED_M0_CAPABILITY_MODE,
    MUTABLE_STATUS_OUTPUTS,
    PROFILE_CONSUMED_NODES,
    QUALIFICATION_CONSUMPTION_CONTRACT,
    QUALIFICATION_POLICY_ID,
    QUALIFICATION_VECTOR_CONTRACT,
    expected_consumed_nodes,
    is_exact_bounded_root_capability_mode,
    is_exact_deferred_m0_capability_mode,
    qualification_checkout_identity,
    qualification_consumption,
    qualification_content_vector,
    qualification_prefixes_equal,
)
from network.validation.component_profiles import (  # noqa: E402
    COMPONENT_PYTHON_MODULES,
    COMPONENT_PYTHON_RUNTIME_CONTRACT,
    expected_radio_provider_runtime,
    load_profiles,
    validate_component_python_runtime,
)

UNQUALIFIED_CONFIG_FALLBACK = (
    "doc/network_radio_integration_plan_v3.md",
    "network/config/scenario_5uav.yaml",
    "network/config/endpoints.yaml",
    "network/config/radio_24ghz.yaml",
    "network/config/radio_backend.yaml",
    "network/config/jammers.yaml",
    "network/config/service_tiers.yaml",
    "network/config/validation_matrix.yaml",
    "network/config/flight_capacity_profile.json",
    "network/config/endpoint_transaction_schema.json",
    "network/config/endpoint_matrix_5uav.json",
    "network/config/dependency_lock.yaml",
)
DEFAULT_CONFIG_PATH_PREFIX = "network/config/"
DEFAULT_PLAN_CONFIG = "doc/network_radio_integration_plan_v3.md"
SOURCE_ROOTS = (
    "network",
    "src/multiagent_simulation",
    "scripts",
    ".devcontainer",
)
EXCLUDED_PARTS = {"__pycache__", ".pytest_cache", ".mypy_cache"}
# These three durable reports are the complete status-only exclusion set.
# Every other tracked source/config/test byte remains a technical input.
EXCLUDED_RELATIVE = set(MUTABLE_STATUS_OUTPUTS)
CANONICAL_RUNTIME_SOURCE_PATHS = {
    "ardupilot_standalone": "/workspace/ardupilot",
    "ardupilot_ros2": "/workspace/ardu_ws/src/ardupilot",
    "micro_ros_agent": "/workspace/ardu_ws/src/micro_ros_agent",
    "ardupilot_gazebo": "/workspace/ardu_ws/src/ardupilot_gazebo",
    "ardupilot_gz": "/workspace/ardu_ws/src/ardupilot_gz",
    "ardupilot_sitl_models": "/workspace/ardu_ws/src/ardupilot_sitl_models",
    "ros_gz": "/workspace/ardu_ws/src/ros_gz",
    "sdformat_urdf": "/workspace/ardu_ws/src/sdformat_urdf",
    "micro_xrce_dds_gen": "/workspace/ardu_ws/Micro-XRCE-DDS-Gen",
}
RUNTIME_SOURCE_ENV = {
    "ardupilot_standalone": "AMS_ARDUPILOT_STANDALONE_ROOT",
    "ardupilot_ros2": "AMS_ARDUPILOT_ROS2_ROOT",
    "micro_ros_agent": "AMS_MICRO_ROS_AGENT_ROOT",
    "ardupilot_gazebo": "AMS_ARDUPILOT_GAZEBO_ROOT",
    "ardupilot_gz": "AMS_ARDUPILOT_GZ_ROOT",
    "ardupilot_sitl_models": "AMS_ARDUPILOT_SITL_MODELS_ROOT",
    "ros_gz": "AMS_ROS_GZ_ROOT",
    "sdformat_urdf": "AMS_SDFORMAT_URDF_ROOT",
    "micro_xrce_dds_gen": "AMS_MICRO_XRCE_DDS_GEN_ROOT",
}
INHERITED_M0_CAPABILITY_MODE = "inherited_m0_host_final"
M1_M0_RECEIPT_CONTRACT = "ams.m1.inherited-m0-qualification/v1"
MAX_INHERITED_RECEIPT_BYTES = 64 * 1024 * 1024
INHERITED_M0_RECEIPT_MOUNTS = {
    "m1_component": "/run/ams/m0-receipt.json",
    "flight_capacity_prerequisite": "/run/ams/prerequisites/m0.json",
}


def run_command(args: list[str], cwd: Path = ROOT_DIR) -> str | None:
    try:
        command = list(args)
        if command and Path(command[0]).name == "git":
            command = [
                command[0],
                "-c",
                f"safe.directory={cwd.resolve(strict=True)}",
                *command[1:],
            ]
        result = subprocess.run(
            command,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inherited_m0_qualification(
    *,
    profile: str,
    current_commit: str,
    current_vector: dict,
    current_plan_sha256: str,
    image_digest: str,
) -> tuple[dict | None, str | None]:
    """Bind a Q0/Q1 flight profile to the accepted exact-image M0 receipt."""

    if profile not in {"m1_component", "flight_capacity_prerequisite"}:
        return None, None
    mounted_path = os.environ.get("AMS_M1_M0_RECEIPT_PATH")
    expected_sha256 = os.environ.get("AMS_M1_M0_RECEIPT_SHA256")
    canonical_path = os.environ.get("AMS_M1_M0_RECEIPT_CANONICAL_PATH")
    status_commit = os.environ.get("AMS_M1_M0_STATUS_COMMIT")
    required_mount = INHERITED_M0_RECEIPT_MOUNTS[profile]
    if mounted_path != required_mount:
        return None, "formal M1 inherited M0 receipt mount is not canonical"
    if status_commit != current_commit:
        return None, "formal M1 M0-status authority is not the current source commit"
    if not isinstance(canonical_path, str) or re.fullmatch(
        r"runs/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}/metrics/m0_host_final_receipt\.json",
        canonical_path,
    ) is None:
        return None, "formal M1 canonical M0 receipt path is invalid"
    if not isinstance(expected_sha256, str) or re.fullmatch(
        r"[0-9a-f]{64}", expected_sha256
    ) is None:
        return None, "formal M1 inherited M0 receipt hash is invalid"
    path = Path(mounted_path)
    try:
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size < 2
            or info.st_size > MAX_INHERITED_RECEIPT_BYTES
            or info.st_mode & 0o222
        ):
            raise ValueError("receipt is not one bounded read-only regular file")
        payload = path.read_bytes()
        after = path.lstat()
        if (
            len(payload) != info.st_size
            or (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_mode, after.st_size, after.st_mtime_ns)
        ):
            raise ValueError("receipt changed while read")
        if hashlib.sha256(payload).hexdigest() != expected_sha256:
            raise ValueError("receipt bytes differ from host-validated hash")
        receipt = json.loads(
            payload.decode("utf-8"),
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite receipt JSON: {value}")
            ),
        )
        if not isinstance(receipt, dict):
            raise ValueError("receipt JSON root is not an object")
        canonical = (
            json.dumps(receipt, allow_nan=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        if payload != canonical:
            raise ValueError("receipt bytes are not canonical JSON")
        vector = receipt.get("qualification_content_vector")
        host_final = receipt.get("gates", {}).get("host_final", {})
        details = host_final.get("details", {}) if isinstance(host_final, dict) else {}
        capability = (
            details.get("isolated_target_runtime_capability")
            if isinstance(details, dict)
            else None
        )
        plan = receipt.get("plan_contract")
        if (
            receipt.get("schema_version") != 3
            or receipt.get("contract") != "ams.m0.host-final-receipt/v1"
            or receipt.get("milestone") != "M0"
            or receipt.get("formal_accepted") is not True
            or receipt.get("passed") is not True
            or receipt.get("failures") != []
            or receipt.get("consumed_nodes") != ["Q0"]
            or receipt.get("receipt_path") != canonical_path
            or not qualification_prefixes_equal(vector, current_vector, ["Q0"])
            or not isinstance(plan, dict)
            or plan.get("path") != "doc/network_radio_integration_plan_v3.md"
            or plan.get("contract_sha256") != current_plan_sha256
            or not isinstance(capability, dict)
            or capability.get("contract") != "ams.m0.isolated-capability-probe/v1"
            or capability.get("image_digest") != image_digest
            or capability.get("exit_code") != 0
            or capability.get("no_candidate_mounts") is not True
            or capability.get("tun_device") is not True
            or capability.get("passwordless_sudo") is not True
            or capability.get("unshare_network_namespace") is not True
        ):
            raise ValueError("receipt does not prove the current exact-image M0/Q0 boundary")
        return {
            "schema_version": 1,
            "contract": M1_M0_RECEIPT_CONTRACT,
            "status_report_commit": status_commit,
            "canonical_receipt_path": canonical_path,
            "mounted_receipt_path": mounted_path,
            "receipt_sha256": expected_sha256,
            "receipt_contract": receipt["contract"],
            "receipt_run_id": receipt.get("run_id"),
            "qualification_contract_sha256": receipt.get(
                "qualification_contract_sha256"
            ),
            "qualification_vector_sha256": vector.get("vector_sha256"),
            "qualification_vector_commit": vector.get("git_commit"),
            "image_digest": image_digest,
            "consumed_nodes": ["Q0"],
            "capabilities": {
                name: capability[name]
                for name in (
                    "tun_device",
                    "passwordless_sudo",
                    "unshare_network_namespace",
                )
            },
            "available": True,
        }, None
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return None, f"formal M1 inherited M0 qualification is invalid: {exc}"


def source_files(root: Path = ROOT_DIR) -> list[Path]:
    files: list[Path] = []
    for value in SOURCE_ROOTS:
        path = root / value
        candidates: Iterable[Path]
        if path.is_file():
            candidates = (path,)
        elif path.is_dir():
            candidates = path.rglob("*")
        else:
            continue
        for candidate in candidates:
            if not candidate.is_file() or candidate.is_symlink():
                continue
            relative = candidate.relative_to(root).as_posix()
            if relative in EXCLUDED_RELATIVE:
                continue
            if any(part in EXCLUDED_PARTS for part in candidate.relative_to(root).parts):
                continue
            if candidate.suffix in {".pyc", ".pyo"}:
                continue
            files.append(candidate)
    return sorted(set(files), key=lambda item: item.relative_to(root).as_posix())


def source_files_for_profile(
    vector: dict[str, object], profile: str, root: Path = ROOT_DIR
) -> list[Path]:
    """Resolve only regular source files owned by one consumed Q-prefix."""

    expected_consumed_nodes(profile)
    manifest = vector.get("entry_manifest") if isinstance(vector, dict) else None
    if not isinstance(manifest, list):
        raise ValueError("qualification vector lacks an entry manifest")
    allowed_owners = (
        {f"Q{index}" for index in range(9)}
        if profile == "diagnostic"
        else set(expected_consumed_nodes(profile))
    )
    selected: list[Path] = []
    for entry in manifest:
        if not isinstance(entry, dict):
            raise ValueError("qualification entry manifest is malformed")
        relative = entry.get("path")
        owner = entry.get("owner")
        if not isinstance(relative, str) or not isinstance(owner, str):
            raise ValueError("qualification source ownership is malformed")
        in_source_root = any(
            relative == source_root or relative.startswith(f"{source_root}/")
            for source_root in SOURCE_ROOTS
        )
        if not in_source_root or owner not in allowed_owners:
            continue
        if entry.get("kind") != "regular":
            continue
        path = root / relative
        try:
            info = path.lstat()
        except OSError as exc:
            raise ValueError(f"profile source file is unavailable: {relative}") from exc
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ValueError(f"profile source path is not regular: {relative}")
        selected.append(path)
    result = sorted(set(selected), key=lambda item: item.relative_to(root).as_posix())
    if not result:
        raise ValueError("profile source set is empty")
    return result


def default_configs_for_profile(
    vector: dict[str, object], profile: str
) -> tuple[str, ...]:
    """Derive the exact tracked config set owned by a profile's Q-prefix."""

    expected_consumed_nodes(profile)
    manifest = vector.get("entry_manifest") if isinstance(vector, dict) else None
    if not isinstance(manifest, list):
        raise ValueError("qualification vector lacks an entry manifest")
    allowed_owners = (
        {f"Q{index}" for index in range(9)}
        if profile == "diagnostic"
        else set(expected_consumed_nodes(profile))
    )
    configs: list[str] = []
    for entry in manifest:
        if not isinstance(entry, dict):
            raise ValueError("qualification entry manifest is malformed")
        path = entry.get("path")
        owner = entry.get("owner")
        if not isinstance(path, str) or not isinstance(owner, str):
            raise ValueError("qualification config ownership is malformed")
        if (
            (path == DEFAULT_PLAN_CONFIG or path.startswith(DEFAULT_CONFIG_PATH_PREFIX))
            and owner in allowed_owners
        ):
            if entry.get("kind") != "regular":
                raise ValueError(f"default config is not a regular file: {path}")
            configs.append(path)
    result = tuple(sorted(configs))
    if not result or DEFAULT_PLAN_CONFIG not in result:
        raise ValueError("profile default config set is incomplete")
    return result


def _normalized_config_paths(values: Iterable[str | Path]) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in values:
        path = Path(value)
        if not path.is_absolute():
            path = ROOT_DIR / path
        try:
            resolved = path.resolve(strict=True)
            relative = resolved.relative_to(ROOT_DIR).as_posix()
            info = path.lstat()
        except (OSError, ValueError) as exc:
            raise ValueError(f"required config is unavailable or outside the repository: {path}") from exc
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ValueError(f"required config is not a regular repository file: {path}")
        normalized.append(relative)
    if len(normalized) != len(set(normalized)):
        raise ValueError("required config list contains duplicate repository paths")
    return tuple(normalized)


def deterministic_source_hash(files: list[Path], root: Path = ROOT_DIR) -> str:
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        file_digest = bytes.fromhex(sha256_file(path))
        digest.update(file_digest)
    return digest.hexdigest()


def package_version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "not-installed"


def runtime_container_identity() -> tuple[str, str]:
    """Read the full host-supplied container ID, with diagnostic fallback."""

    identity_file = os.environ.get("AMS_RUNTIME_CONTAINER_ID_FILE")
    if identity_file:
        try:
            value = Path(identity_file).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            return "unknown", "host_bind_mount_unreadable"
        return value, "host_bind_mount"
    return os.environ.get("HOSTNAME", "unknown"), "docker_hostname_diagnostic"


def _runtime_identity_file_sha256(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if value else None


def _process_security_status() -> dict[str, object]:
    required = ("CapPrm", "CapEff", "CapBnd", "NoNewPrivs")
    parsed: dict[str, str] = {}
    try:
        lines = Path("/proc/self/status").read_text(
            encoding="ascii", errors="strict"
        ).splitlines()
    except (OSError, UnicodeError):
        lines = []
    for line in lines:
        key, separator, value = line.partition(":")
        if separator and key in required and key not in parsed:
            parsed[key] = value.strip()
    no_new_privs = parsed.get("NoNewPrivs")
    return {
        "uid": os.getuid(),
        "gid": os.getgid(),
        "CapPrm": parsed.get("CapPrm"),
        "CapEff": parsed.get("CapEff"),
        "CapBnd": parsed.get("CapBnd"),
        "NoNewPrivs": (
            int(no_new_privs)
            if isinstance(no_new_privs, str) and no_new_privs in {"0", "1"}
            else None
        ),
    }


def runtime_capabilities() -> dict[str, object]:
    """Record the concrete host/container capabilities used by network runners."""

    gpu_output = run_command(
        [
            "nvidia-smi",
            "--query-gpu=uuid,name,driver_version",
            "--format=csv,noheader",
        ]
    )
    gpu_devices = sorted(
        line.strip() for line in (gpu_output or "").splitlines() if line.strip()
    )
    return {
        "system": platform.system(),
        "machine": platform.machine(),
        "kernel_release": platform.release(),
        "kernel_version": platform.version(),
        "host": {
            "boot_id_sha256": _runtime_identity_file_sha256(
                Path("/proc/sys/kernel/random/boot_id")
            ),
            "machine_id_sha256": _runtime_identity_file_sha256(Path("/etc/machine-id")),
        },
        "mitsuba_variant": os.environ.get(
            "SIONNA_MITSUBA_VARIANT", "cuda_ad_mono_polarized"
        ),
        "gpu": {
            "available": bool(gpu_devices),
            "devices": gpu_devices,
        },
        "network": {
            "dev_net_tun": Path("/dev/net/tun").is_char_device(),
            "unshare_network_namespace": run_command(
                ["/usr/bin/unshare", "-rn", "true"]
            )
            is not None,
            "passwordless_sudo": run_command(["/usr/bin/sudo", "-n", "true"])
            is not None,
            "qualification_mode": os.environ.get(
                "AMS_M0_CAPABILITY_PROBE_MODE", "in_runtime"
            ),
            **_process_security_status(),
        },
    }


def component_python_runtime(
    qualification_profile: str,
) -> tuple[dict[str, object] | None, str | None]:
    """Observe the exact Sionna Python runtime selected by a component profile."""

    profile = load_profiles().get(qualification_profile)
    if profile is None or profile["python_runtime"] == "base":
        return None, None
    runtime_profile = profile["python_runtime"]
    try:
        pythonpath = os.environ.get("PYTHONPATH")
        if not isinstance(pythonpath, str) or not pythonpath:
            raise ValueError("PYTHONPATH is absent")
        pythonpath_entries = pythonpath.split(os.pathsep)
        executable_path = Path(sys.executable)
        executable_realpath = executable_path.resolve(strict=True)
        if not executable_realpath.is_file():
            raise ValueError("Python executable realpath is not a regular file")
        modules: dict[str, dict[str, object]] = {}
        for module_name, distribution in COMPONENT_PYTHON_MODULES.items():
            module = importlib.import_module(module_name)
            module_file = getattr(module, "__file__", None)
            if not isinstance(module_file, str) or not module_file:
                raise ValueError(f"module origin is unavailable: {module_name}")
            origin = Path(module_file).resolve(strict=True)
            if not origin.is_file():
                raise ValueError(f"module origin is not regular: {module_name}")
            modules[module_name] = {
                "distribution": distribution,
                "origin": str(origin),
                "sha256": sha256_file(origin),
                "size_bytes": origin.stat().st_size,
                "version": metadata.version(distribution),
            }
        record: dict[str, object] = {
            "contract": COMPONENT_PYTHON_RUNTIME_CONTRACT,
            "profile": runtime_profile,
            "status": "passed",
            "python_no_user_site": os.environ.get("PYTHONNOUSERSITE"),
            "pythonpath": pythonpath,
            "pythonpath_entries": pythonpath_entries,
            "executable": {
                "configured_path": str(executable_path),
                "realpath": str(executable_realpath),
                "sha256": sha256_file(executable_realpath),
                "size_bytes": executable_realpath.stat().st_size,
            },
            "modules": modules,
        }
        failures = validate_component_python_runtime(profile, record, {
            module["distribution"]: module["version"] for module in modules.values()
        })
        if failures:
            raise ValueError("; ".join(failures))
        return record, None
    except Exception as exc:
        return {
            "contract": COMPONENT_PYTHON_RUNTIME_CONTRACT,
            "profile": runtime_profile,
            "status": "failed",
            "error": str(exc),
        }, str(exc)


def command_manifest(args: list[str]) -> dict[str, object]:
    output = run_command(args)
    if output is None:
        return {
            "command": args,
            "available": False,
            "entries": 0,
            "sha256": None,
            "lines": [],
        }
    lines = sorted(line.strip() for line in output.splitlines() if line.strip())
    normalized = "\n".join(lines) + ("\n" if lines else "")
    return {
        "command": args,
        "available": True,
        "entries": len(lines),
        "sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        "lines": lines,
    }


def runtime_manifest_commands() -> dict[str, list[str]]:
    """Return the exact commands whose normalized output is dependency-locked.

    Editable source packages are excluded from ``pip freeze`` because their
    output embeds the current project Git commit.  Project and external source
    identity is already bound independently by ``git_commit``,
    ``source_manifest``, and ``external_sources``; including the editable VCS
    line would make committing the dependency lock change its own expected
    hash forever.
    """

    return {
        "pip_freeze": [
            sys.executable,
            "-m",
            "pip",
            "freeze",
            "--all",
            "--exclude-editable",
        ],
        "dpkg": ["dpkg-query", "-W", "-f=${Package}=${Version}\\n"],
        "ros_packages": ["ros2", "pkg", "list"],
    }


def external_git(path: Path) -> dict[str, str | bool | None]:
    git_root = run_command(["git", "rev-parse", "--show-toplevel"], cwd=path) if path.is_dir() else None
    is_git_checkout = bool(git_root and Path(git_root).resolve() == path.resolve())
    commit = run_command(["git", "rev-parse", "HEAD"], cwd=path) if is_git_checkout else None
    status = run_command(["git", "status", "--porcelain"], cwd=path) if is_git_checkout else None
    version_file = path / "VERSION"
    version = version_file.read_text(errors="replace").strip() if version_file.is_file() else None
    return {
        "path": str(path.relative_to(ROOT_DIR)) if path.is_relative_to(ROOT_DIR) else str(path),
        "commit": commit,
        "version": version,
        "dirty": bool(status) if status is not None else None,
        "is_git_checkout": is_git_checkout,
    }


def ns3_core_tree_hash(path: Path) -> tuple[int, str]:
    excluded_parts = {"build", "cmake-cache", "scratch", "__pycache__", ".vscode"}
    files: list[Path] = []
    for candidate in path.rglob("*"):
        if not candidate.is_file() or candidate.is_symlink():
            continue
        relative = candidate.relative_to(path)
        if relative.parts[:2] == ("src", "lorawan"):
            continue
        if any(part in excluded_parts for part in relative.parts):
            continue
        if relative.name.startswith(".lock-") or candidate.suffix in {".pyc", ".pyo"}:
            continue
        files.append(candidate)
    ordered = sorted(files, key=lambda item: item.relative_to(path).as_posix())
    return len(ordered), deterministic_source_hash(ordered, root=path)


def ns3_release(path: Path) -> dict[str, object]:
    version_file = path / "VERSION"
    version = version_file.read_text(errors="replace").strip() if version_file.is_file() else None
    file_count, tree_hash = ns3_core_tree_hash(path) if path.is_dir() else (0, "unknown")
    expected_tree_hash = "0119836a7c79f7470f0c2c866de9c14ddc4f22349bbd194112ff2952713b64e8"
    return {
        "path": str(path.relative_to(ROOT_DIR)),
        "source_kind": "official_release_archive",
        "version": version,
        "archive_url": "https://www.nsnam.org/releases/ns-allinone-3.40.tar.bz2",
        "archive_sha256": "c0ba395b6fcb084c4d43d6117b28932f716b26aebb54498ce2f44c0c39be3e60",
        "core_tree_files": file_count,
        "core_tree_sha256": tree_hash,
        "expected_core_tree_sha256": expected_tree_hash,
        "source_clean": version == "3.40" and tree_hash == expected_tree_hash,
    }


def build_provenance(args: argparse.Namespace) -> dict:
    run_dir = args.run_dir.resolve()
    run_id = run_dir.name
    commit = run_command(["git", "rev-parse", "HEAD"]) or "unknown"
    status_text = run_command(["git", "status", "--porcelain", "--untracked-files=all"])
    status_lines = status_text.splitlines() if status_text else []
    expected_nodes = list(expected_consumed_nodes(args.qualification_profile))
    if args.consumed_node != expected_nodes:
        raise ValueError(
            f"qualification profile {args.qualification_profile!r} must consume "
            f"exactly {expected_nodes!r}"
        )
    qualification_vector_error: str | None = None
    qualification_checkout_error: str | None = None
    try:
        qualification_vector = qualification_content_vector(ROOT_DIR, commit)
        qualification_consumption_record = qualification_consumption(
            qualification_vector, args.qualification_profile
        )
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        qualification_vector_error = str(exc)
        qualification_vector = {
            "schema_version": 1,
            "contract": QUALIFICATION_VECTOR_CONTRACT,
            "policy_id": QUALIFICATION_POLICY_ID,
            "git_commit": commit,
            "available": False,
            "failure": qualification_vector_error,
            "vector_sha256": None,
        }
        qualification_consumption_record = {
            "schema_version": 1,
            "contract": QUALIFICATION_CONSUMPTION_CONTRACT,
            "profile": args.qualification_profile,
            "consumed_nodes": expected_nodes,
            "consumed_node_sha256": {},
            "vector_sha256": None,
            "policy_sha256": None,
            "git_commit": commit,
            "available": False,
        }
    try:
        qualification_checkout = qualification_checkout_identity(ROOT_DIR, commit)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        qualification_checkout_error = str(exc)
        qualification_checkout = {
            "schema_version": 1,
            "expected_commit": commit,
            "available": False,
            "failure": qualification_checkout_error,
            "checkout_equal": False,
        }
    if qualification_vector_error is None:
        files = source_files_for_profile(
            qualification_vector, args.qualification_profile
        )
    elif args.qualification_profile == "diagnostic":
        files = source_files()
    else:
        raise ValueError(
            "acceptance profile source ownership cannot be derived without its "
            "committed qualification vector"
        )
    plan_relative = "doc/network_radio_integration_plan_v3.md"
    plan_contract = {
        "plan_version": 3,
        "path": plan_relative,
        "contract_sha256": sha256_file(ROOT_DIR / plan_relative),
    }

    if qualification_vector_error is None:
        profile_defaults = default_configs_for_profile(
            qualification_vector, args.qualification_profile
        )
    else:
        if args.qualification_profile != "diagnostic":
            raise ValueError(
                "acceptance profile config ownership cannot be derived without "
                "its committed qualification vector"
            )
        # Unqualified diagnostic/error provenance remains writable.
        profile_defaults = UNQUALIFIED_CONFIG_FALLBACK
    if args.config:
        selected_configs = _normalized_config_paths(args.config)
        if (
            args.qualification_profile != "diagnostic"
            and selected_configs != profile_defaults
        ):
            raise ValueError(
                f"qualification profile {args.qualification_profile!r} must hash "
                f"exactly its default configs {list(profile_defaults)!r}"
            )
    else:
        selected_configs = profile_defaults

    config_hashes = {
        relative: sha256_file(ROOT_DIR / relative) for relative in selected_configs
    }

    runtime_source_paths = {
        name: Path(os.environ.get(RUNTIME_SOURCE_ENV[name], canonical))
        for name, canonical in CANONICAL_RUNTIME_SOURCE_PATHS.items()
    }
    python_runtime_record, python_runtime_error = component_python_runtime(
        args.qualification_profile
    )
    dependency_versions = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "kernel_release": platform.release(),
        "runtime_capabilities": runtime_capabilities(),
        "ros_distribution": os.environ.get("ROS_DISTRO", "unknown"),
        "gazebo": run_command(["gz", "sim", "--versions"]) or "unavailable",
        "sionna": package_version("sionna"),
        "sionna-rt": package_version("sionna-rt"),
        "mitsuba": package_version("mitsuba"),
        "numpy": package_version("numpy"),
        "PyYAML": package_version("PyYAML"),
        "matplotlib": package_version("matplotlib"),
        "pymavlink": package_version("pymavlink"),
        "pybind11": package_version("pybind11"),
        "cppyy": package_version("cppyy"),
        "ns3": ns3_release(ROOT_DIR / ".external/ns-3"),
        "ns3_sionna_diagnostic": external_git(ROOT_DIR / ".external/ns-3-sionna"),
        "external_sources": {
            name: external_git(path) for name, path in runtime_source_paths.items()
        },
        "runtime_manifests": {
            name: command_manifest(command)
            for name, command in runtime_manifest_commands().items()
        },
    }
    if python_runtime_record is not None:
        dependency_versions["python_runtime"] = python_runtime_record

    container_reference = args.container_image or os.environ.get("AMS_CONTAINER_IMAGE", "unknown")
    container_digest = args.container_digest or os.environ.get("AMS_CONTAINER_IMAGE_DIGEST", "unknown")
    container_digest_source = args.container_digest_source or os.environ.get(
        "AMS_CONTAINER_IMAGE_DIGEST_SOURCE", "unknown"
    )
    runtime_container_id, runtime_container_id_source = runtime_container_identity()
    inherited_m0, inherited_m0_error = inherited_m0_qualification(
        profile=args.qualification_profile,
        current_commit=commit,
        current_vector=qualification_vector,
        current_plan_sha256=plan_contract["contract_sha256"],
        image_digest=container_digest,
    )
    default_provider_runtime = expected_radio_provider_runtime(
        args.qualification_profile, args.radio_provider_id
    )
    implementation = {
        "packet_ingress_mode": args.packet_ingress_mode,
        "medium_model": args.medium_model,
        "radio_provider_id": args.radio_provider_id,
        "radio_provider_runtime_consumed": (
            default_provider_runtime["radio_provider_runtime_consumed"]
            if args.radio_provider_runtime_consumed is None
            else args.radio_provider_runtime_consumed == "true"
        ),
        "runtime_provider_id": (
            args.runtime_provider_id
            if args.runtime_provider_id is not None
            else default_provider_runtime["runtime_provider_id"]
        ),
        "reason": (
            args.radio_provider_runtime_reason
            if args.radio_provider_runtime_reason is not None
            else default_provider_runtime["reason"]
        ),
    }

    lock_path = ROOT_DIR / "network/config/dependency_lock.yaml"
    lock_structure_errors: list[str] = []
    try:
        loaded_dependency_lock = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
    except Exception as exc:
        lock_structure_errors.append(f"dependency lock could not be loaded: {exc}")
        dependency_lock = {}
    else:
        if isinstance(loaded_dependency_lock, dict):
            dependency_lock = loaded_dependency_lock
        else:
            lock_structure_errors.append("dependency lock root is not a mapping")
            dependency_lock = {}

    def lock_mapping(value: object, label: str) -> dict:
        if isinstance(value, dict):
            return value
        lock_structure_errors.append(f"dependency lock field is not a mapping: {label}")
        return {}

    blockers = lock_structure_errors
    if python_runtime_error is not None:
        blockers.append(
            "required component Python runtime is unavailable: "
            + python_runtime_error
        )
    profile_record = load_profiles().get(args.qualification_profile)
    if profile_record is not None:
        for failure in validate_component_python_runtime(
            profile_record,
            dependency_versions.get("python_runtime"),
            dependency_versions,
        ):
            blockers.append("component Python runtime: " + failure)
    if qualification_vector_error is not None:
        blockers.append(
            "qualification content vector is unavailable: "
            + qualification_vector_error
        )
    if qualification_checkout_error is not None:
        blockers.append(
            "qualification checkout could not be inspected: "
            + qualification_checkout_error
        )
    elif qualification_checkout.get("checkout_equal") is not True:
        blockers.append("qualification checkout does not exactly equal its Git commit")
    if inherited_m0_error is not None:
        blockers.append(inherited_m0_error)
    if status_text is None:
        blockers.append("git status could not be inspected")
    if status_lines:
        blockers.append("source checkout is dirty")
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        blockers.append("git commit is not a full hexadecimal revision")
    if re.fullmatch(r"sha256:[0-9a-f]{64}", container_digest) is None:
        blockers.append("container image digest is not an exact SHA-256 digest")
    if container_digest_source != "docker_image_inspect_host":
        blockers.append("container image digest was not attested by host docker inspect")
    if re.fullmatch(r"[0-9a-f]{64}", runtime_container_id) is None:
        blockers.append("full runtime container ID is unavailable or invalid")
    if runtime_container_id_source != "host_bind_mount":
        blockers.append("runtime container ID was not supplied by the host bind mount")
    if dependency_lock.get("schema_version") != 2 or dependency_lock.get("status") != "complete":
        blockers.append("dependency lock is not complete")
    accepted_path = lock_mapping(
        dependency_lock.get("accepted_p0_path"), "accepted_p0_path"
    )
    expected_implementation = {
        "packet_ingress_mode": accepted_path.get("packet_ingress"),
        "medium_model": accepted_path.get("medium_model"),
        "radio_provider_id": accepted_path.get("radio_provider"),
        **expected_radio_provider_runtime(
            args.qualification_profile, accepted_path.get("radio_provider")
        ),
    }
    if implementation != expected_implementation:
        blockers.append("runtime implementation does not match dependency lock")
    lock_dependencies = lock_mapping(
        dependency_lock.get("dependencies"), "dependencies"
    )
    expected_packages = lock_mapping(
        lock_dependencies.get("python_packages"), "dependencies.python_packages"
    )
    for package, expected in expected_packages.items():
        if dependency_versions.get(package) != str(expected):
            blockers.append(
                f"Python package {package}={dependency_versions.get(package)!r}, expected {expected!r}"
            )
    if dependency_versions["ns3"].get("source_clean") is not True:
        blockers.append("ns-3 source does not match the pinned release tree")
    for name, path in runtime_source_paths.items():
        canonical = Path(CANONICAL_RUNTIME_SOURCE_PATHS[name])
        if path != canonical:
            blockers.append(
                f"external source {name} uses diagnostic path override {path}; "
                f"acceptance requires {canonical}"
            )
    ros2_repos = lock_mapping(
        lock_dependencies.get("ardupilot_ros_repos"),
        "dependencies.ardupilot_ros_repos",
    )
    ros2_revisions = lock_mapping(
        ros2_repos.get("revisions"),
        "dependencies.ardupilot_ros_repos.revisions",
    )
    gz_repos = lock_mapping(
        lock_dependencies.get("ardupilot_gz_repos"),
        "dependencies.ardupilot_gz_repos",
    )
    gz_revisions = lock_mapping(
        gz_repos.get("revisions"),
        "dependencies.ardupilot_gz_repos.revisions",
    )
    ardupilot_lock = lock_mapping(
        lock_dependencies.get("ardupilot"), "dependencies.ardupilot"
    )
    micro_xrce_lock = lock_mapping(
        lock_dependencies.get("micro_xrce_dds_gen"),
        "dependencies.micro_xrce_dds_gen",
    )
    expected_external_sources = {
        "ardupilot_standalone": ardupilot_lock.get("revision"),
        "ardupilot_ros2": ros2_revisions.get("ardupilot"),
        "micro_ros_agent": ros2_revisions.get("micro_ros_agent"),
        "ardupilot_gazebo": gz_revisions.get("ardupilot_gazebo"),
        "ardupilot_gz": gz_revisions.get("ardupilot_gz"),
        "ardupilot_sitl_models": gz_revisions.get("ardupilot_sitl_models"),
        "ros_gz": gz_revisions.get("ros_gz"),
        "sdformat_urdf": gz_revisions.get("sdformat_urdf"),
        "micro_xrce_dds_gen": micro_xrce_lock.get("revision"),
    }
    for name, expected_commit in expected_external_sources.items():
        record = dependency_versions["external_sources"][name]
        if (
            re.fullmatch(r"[0-9a-f]{40}", str(expected_commit or "")) is None
            or record.get("is_git_checkout") is not True
            or record.get("commit") != expected_commit
            or record.get("dirty") is not False
        ):
            blockers.append(f"external source {name} does not match its clean locked commit")
    python_parts = tuple(int(part) for part in platform.python_version_tuple()[:2])
    if not (python_parts >= (3, 10) and python_parts < (3, 13)):
        blockers.append("Python runtime is outside the pinned >=3.10,<3.13 range")
    if dependency_versions["ros_distribution"] != "humble":
        blockers.append("ROS distribution is not humble")
    runtime_policy = lock_mapping(dependency_lock.get("runtime_policy"), "runtime_policy")
    capabilities = dependency_versions["runtime_capabilities"]
    if capabilities.get("system") != runtime_policy.get("system"):
        blockers.append("runtime operating system does not match dependency lock")
    if capabilities.get("machine") != runtime_policy.get("machine"):
        blockers.append("runtime machine architecture does not match dependency lock")
    if capabilities.get("mitsuba_variant") != runtime_policy.get("mitsuba_variant"):
        blockers.append("Mitsuba variant does not match dependency lock")
    gpu = capabilities.get("gpu") if isinstance(capabilities.get("gpu"), dict) else {}
    if runtime_policy.get("gpu_required") is True and gpu.get("available") is not True:
        blockers.append("dependency lock requires a visible GPU")
    required_network = lock_mapping(
        runtime_policy.get("required_network_capabilities"),
        "runtime_policy.required_network_capabilities",
    )
    observed_network = (
        capabilities.get("network")
        if isinstance(capabilities.get("network"), dict)
        else {}
    )
    qualification_mode = observed_network.get("qualification_mode")
    deferred_m0_capabilities = is_exact_deferred_m0_capability_mode(
        qualification_consumption_record, qualification_mode
    )
    bounded_root_capabilities = is_exact_bounded_root_capability_mode(
        qualification_consumption_record, qualification_mode
    )
    bounded_root_facts = (
        observed_network.get("uid") == BOUNDED_ROOT_UID
        and observed_network.get("gid") == BOUNDED_ROOT_GID
        and observed_network.get("CapPrm") == BOUNDED_ROOT_CAPABILITY_MASK
        and observed_network.get("CapEff") == BOUNDED_ROOT_CAPABILITY_MASK
        and observed_network.get("CapBnd") == BOUNDED_ROOT_CAPABILITY_MASK
        and observed_network.get("NoNewPrivs") == BOUNDED_ROOT_NO_NEW_PRIVS
        and observed_network.get("dev_net_tun") is True
        and observed_network.get("unshare_network_namespace") is True
        and observed_network.get("passwordless_sudo") is False
    )
    inherited_flight_capabilities = (
        args.qualification_profile
        in {"m1_component", "flight_capacity_prerequisite"}
        and qualification_mode == INHERITED_M0_CAPABILITY_MODE
        and isinstance(inherited_m0, dict)
        and inherited_m0.get("available") is True
    )
    if qualification_mode not in {
        "in_runtime",
        DEFERRED_M0_CAPABILITY_MODE,
        INHERITED_M0_CAPABILITY_MODE,
        BOUNDED_ROOT_IN_RUNTIME_MODE,
    }:
        blockers.append("network capability qualification mode is invalid")
    elif (
        qualification_mode == DEFERRED_M0_CAPABILITY_MODE
        and not deferred_m0_capabilities
    ):
        blockers.append(
            "isolated host-final capability qualification is allowed only for M0/Q0"
        )
    elif (
        qualification_mode == INHERITED_M0_CAPABILITY_MODE
        and not inherited_flight_capabilities
    ):
        blockers.append(
            "inherited host-final capability qualification is allowed only for "
            "a Q0/Q1 flight profile with a valid current M0 receipt"
        )
    elif qualification_mode == BOUNDED_ROOT_IN_RUNTIME_MODE and (
        not bounded_root_capabilities or not bounded_root_facts
    ):
        blockers.append(
            "bounded-root in-runtime capability qualification is not the exact "
            "M2--M4 uid/gid/capability/no-new-privileges contract"
        )
    elif (
        args.qualification_profile in BOUNDED_ROOT_IN_RUNTIME_PROFILES
        and qualification_mode != BOUNDED_ROOT_IN_RUNTIME_MODE
    ):
        blockers.append(
            "M2--M4 TUN qualification requires bounded_root_in_runtime mode"
        )
    for capability, required in required_network.items():
        if (
            required is True
            and observed_network.get(capability) is not True
            and not deferred_m0_capabilities
            and not inherited_flight_capabilities
            and not (
                bounded_root_capabilities
                and bounded_root_facts
                and capability == "passwordless_sudo"
            )
        ):
            blockers.append(f"required network capability is unavailable: {capability}")
    gazebo_lock = lock_mapping(lock_dependencies.get("gazebo"), "dependencies.gazebo")
    if dependency_versions["gazebo"] != str(gazebo_lock.get("version")):
        blockers.append("Gazebo runtime version does not match dependency lock")
    for name, record in dependency_versions["runtime_manifests"].items():
        lines = record.get("lines")
        valid_lines = isinstance(lines, list) and all(isinstance(line, str) for line in lines)
        normalized = (
            "\n".join(lines) + ("\n" if lines else "")
            if valid_lines
            else None
        )
        if dependency_lock.get("status") == "complete" and (
            record.get("available") is not True
            or not isinstance(record.get("sha256"), str)
            or int(record.get("entries", 0)) < 1
            or not valid_lines
            or (valid_lines and lines != sorted(set(lines)))
            or len(lines) != record.get("entries")
            or normalized is None
            or hashlib.sha256(normalized.encode("utf-8")).hexdigest()
            != record.get("sha256")
        ):
            blockers.append(f"runtime dependency manifest is unavailable: {name}")
    expected_runtime_hashes = lock_mapping(
        dependency_lock.get("runtime_manifest_sha256"), "runtime_manifest_sha256"
    )
    if dependency_lock.get("status") == "complete":
        for name, record in dependency_versions["runtime_manifests"].items():
            if record.get("sha256") != expected_runtime_hashes.get(name):
                blockers.append(f"runtime dependency manifest does not match lock: {name}")
    ros_lock = lock_mapping(lock_dependencies.get("ros"), "dependencies.ros")
    expected_reference = ros_lock.get("project_image_reference")
    if expected_reference and container_reference != expected_reference:
        blockers.append("container image reference does not match dependency lock")
    expected_digest = ros_lock.get("project_image_digest")
    if dependency_lock.get("status") == "complete" and container_digest != expected_digest:
        blockers.append("container image digest does not match the completed dependency lock")

    diff_text = run_command(["git", "diff", "--binary", "HEAD"])
    if diff_text is None:
        blockers.append("git diff could not be inspected")
    diff_payload = diff_text if diff_text is not None else "<git-diff-unavailable>"
    diff_hash = hashlib.sha256(diff_payload.encode("utf-8")).hexdigest()
    return {
        "schema_version": 2,
        "run_id": run_id,
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "git_commit": commit,
        "git_dirty": bool(status_lines),
        "git_status": status_lines,
        "git_diff_sha256": diff_hash,
        "source_hash": deterministic_source_hash(files),
        "source_files": len(files),
        "source_manifest": {
            path.relative_to(ROOT_DIR).as_posix(): sha256_file(path) for path in files
        },
        "qualification_content_vector": qualification_vector,
        "qualification_consumption": qualification_consumption_record,
        "qualification_checkout": qualification_checkout,
        "inherited_m0_qualification": inherited_m0,
        "plan_contract": plan_contract,
        "config_hashes": config_hashes,
        "dependency_versions": dependency_versions,
        "container_image": {
            "reference": container_reference,
            "digest": container_digest,
            "digest_source": container_digest_source,
            "runtime_container_id": runtime_container_id,
            "runtime_container_id_source": runtime_container_id_source,
        },
        "implementation": implementation,
        "dependency_lock_status": dependency_lock.get("status", "missing_or_invalid"),
        "acceptance_blockers": blockers,
        "acceptance_eligible": not blockers,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--config", action="append", default=[])
    parser.add_argument("--container-image")
    parser.add_argument("--container-digest")
    parser.add_argument("--container-digest-source")
    parser.add_argument("--packet-ingress-mode", default="tap_bridge_external")
    parser.add_argument("--medium-model", default="csma_surrogate")
    parser.add_argument("--radio-provider-id", default="tcp_jsonl_real_sionna")
    parser.add_argument(
        "--radio-provider-runtime-consumed", choices=("true", "false")
    )
    parser.add_argument("--runtime-provider-id")
    parser.add_argument("--radio-provider-runtime-reason")
    parser.add_argument(
        "--qualification-profile",
        default="diagnostic",
        choices=sorted(PROFILE_CONSUMED_NODES),
    )
    parser.add_argument(
        "--consumed-node",
        action="append",
        default=[],
        choices=[f"Q{index}" for index in range(9)],
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        data = build_provenance(args)
    except Exception as exc:
        print(f"FAIL provenance generation: {exc}", file=sys.stderr)
        return 2
    output = args.run_dir.resolve() / "metrics" / "provenance.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, output)
    except FileExistsError:
        print(f"FAIL provenance output already exists: {output}", file=sys.stderr)
        return 2
    finally:
        temporary.unlink(missing_ok=True)
    print(f"Provenance: {output}")
    print(f"Acceptance eligible: {str(data['acceptance_eligible']).lower()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
