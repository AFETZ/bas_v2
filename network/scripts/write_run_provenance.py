#!/usr/bin/env python3
"""Write deterministic source/config/dependency provenance for one run."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Iterable

import yaml


ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_CONFIGS = (
    "doc/network_radio_integration_plan_v3.md",
    "network/config/scenario_5uav.yaml",
    "network/config/endpoints.yaml",
    "network/config/radio_24ghz.yaml",
    "network/config/radio_backend.yaml",
    "network/config/jammers.yaml",
    "network/config/service_tiers.yaml",
    "network/config/validation_matrix.yaml",
    "network/config/dependency_lock.yaml",
)
SOURCE_ROOTS = (
    "network",
    "src/multiagent_simulation",
    "scripts",
    ".devcontainer",
)
EXCLUDED_PARTS = {"__pycache__", ".pytest_cache", ".mypy_cache"}
EXCLUDED_RELATIVE = {
    "network/swarm/.last_run",
    # These are durable human-readable status/report files.  They necessarily
    # change after validation and therefore are not runtime implementation
    # inputs.  The immutable execution contract itself is hashed separately in
    # DEFAULT_CONFIGS.
    "network/PROGRESS.md",
    "network/VALIDATION_REPORT.md",
    "network/NEXT_TASK.md",
    "network/DECISIONS.md",
    "network/README.md",
}
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


def run_command(args: list[str], cwd: Path = ROOT_DIR) -> str | None:
    try:
        result = subprocess.run(
            args,
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
            "SIONNA_MITSUBA_VARIANT", "llvm_ad_mono_polarized"
        ),
        "gpu": {
            "available": bool(gpu_devices),
            "devices": gpu_devices,
        },
        "network": {
            "dev_net_tun": Path("/dev/net/tun").is_char_device(),
            "unshare_network_namespace": run_command(["unshare", "-rn", "true"])
            is not None,
            "passwordless_sudo": run_command(["sudo", "-n", "true"]) is not None,
        },
    }


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
    files = source_files()

    config_hashes = {}
    for value in args.config:
        path = Path(value)
        if not path.is_absolute():
            path = ROOT_DIR / path
        if not path.is_file():
            raise FileNotFoundError(f"required config is missing: {path}")
        config_hashes[path.relative_to(ROOT_DIR).as_posix()] = sha256_file(path)

    runtime_source_paths = {
        name: Path(os.environ.get(RUNTIME_SOURCE_ENV[name], canonical))
        for name, canonical in CANONICAL_RUNTIME_SOURCE_PATHS.items()
    }
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

    container_reference = args.container_image or os.environ.get("AMS_CONTAINER_IMAGE", "unknown")
    container_digest = args.container_digest or os.environ.get("AMS_CONTAINER_IMAGE_DIGEST", "unknown")
    container_digest_source = args.container_digest_source or os.environ.get(
        "AMS_CONTAINER_IMAGE_DIGEST_SOURCE", "unknown"
    )
    runtime_container_id, runtime_container_id_source = runtime_container_identity()
    implementation = {
        "packet_ingress_mode": args.packet_ingress_mode,
        "medium_model": args.medium_model,
        "radio_provider_id": args.radio_provider_id,
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
    for capability, required in required_network.items():
        if required is True and observed_network.get(capability) is not True:
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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.config:
        args.config = list(DEFAULT_CONFIGS)
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
