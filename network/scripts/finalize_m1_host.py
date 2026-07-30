#!/usr/bin/python3.10
"""Independently bind, freeze, and atomically publish one formal M1 run."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any


CODE_ROOT = Path(__file__).resolve().parents[2]
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

from network.scripts.host_finalization_common import (  # noqa: E402
    exact_mounts,
    one_inspect,
    read_regular,
    rename_noreplace,
    strict_json,
    tree_manifest,
    validate_source_snapshot,
)
from network.validation.qualification_identity import (  # noqa: E402
    qualification_prefixes_equal,
)


SAFE_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
SHA1 = re.compile(r"[0-9a-f]{40}")
SHA256_IMAGE = re.compile(r"sha256:[0-9a-f]{64}")
CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
CONTRACT = "ams.m1.host-final-receipt/v1"
RESULT_CONTRACT = "ams.m1.health/v3"
M0_RECEIPT_CONTRACT = "ams.m0.host-final-receipt/v1"
M0_STATUS_CONTRACT = "ams.live-status-lint/v1"
M0_INHERITANCE_CONTRACT = "ams.m1.inherited-m0-qualification/v1"
EXPECTED_GPU_DEVICE_REQUESTS = [
    {
        "Driver": "",
        "Count": -1,
        "DeviceIDs": None,
        "Capabilities": [["compute", "utility", "gpu"]],
        "Options": {},
    }
]


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def immutable_container_configuration(
    initial: dict[str, Any], final: dict[str, Any]
) -> None:
    """Compare immutable fields while treating Docker Mounts as an unordered set."""

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
        if host.get("OomKillDisable") in (None, False):
            host["OomKillDisable"] = False
    if normalized_initial_host != normalized_final_host:
        raise ValueError("Docker container immutable field changed: HostConfig")
    initial_mounts = initial.get("Mounts")
    final_mounts = final.get("Mounts")
    if (
        not isinstance(initial_mounts, list)
        or not isinstance(final_mounts, list)
        or not all(isinstance(item, dict) for item in [*initial_mounts, *final_mounts])
        or sorted(canonical(item) for item in initial_mounts)
        != sorted(canonical(item) for item in final_mounts)
    ):
        raise ValueError("Docker container immutable field changed: Mounts")


def environment_map(values: Any) -> dict[str, str]:
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError("Docker Config.Env is not a string list")
    result: dict[str, str] = {}
    for value in values:
        name, separator, content = value.partition("=")
        if not separator or not name or name in result:
            raise ValueError("Docker Config.Env is malformed or duplicate")
        result[name] = content
    return result


def validate_main_container(
    initial: dict[str, Any],
    final: dict[str, Any],
    *,
    run_id: str,
    container_id: str,
    image_digest: str,
    image_reference: str,
    source_commit: str,
    source_snapshot: Path,
    artifact_staging: Path,
    container_identity_file: Path,
    m0_receipt_path: Path,
    m0_receipt_canonical: str,
    m0_receipt_sha256: str,
    project_root: Path,
) -> None:
    immutable_container_configuration(initial, final)
    initial_state = initial.get("State", {})
    final_state = final.get("State", {})
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
    ):
        raise ValueError("formal M1 container lifecycle is not exact exited/zero")
    config = final.get("Config", {})
    host = final.get("HostConfig", {})
    environment = environment_map(config.get("Env"))
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
        "AMS_M1_M0_RECEIPT_CANONICAL_PATH": m0_receipt_canonical,
        "AMS_M1_M0_RECEIPT_SHA256": m0_receipt_sha256,
        "AMS_M1_M0_STATUS_COMMIT": source_commit,
        "GZ_VERSION": "harmonic",
        "NVIDIA_VISIBLE_DEVICES": "all",
        "NVIDIA_DRIVER_CAPABILITIES": "compute,utility",
        "SIONNA_MITSUBA_VARIANT": "cuda_ad_mono_polarized",
    }
    if any(environment.get(name) != value for name, value in required_environment.items()) or {
        name for name in environment if name.startswith("AMS_")
    } != {name for name in required_environment if name.startswith("AMS_")}:
        raise ValueError("formal M1 container environment is not exact")
    expected_command = [
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
        config.get("Image") != image_digest
        or config.get("User") != "ubuntu"
        or config.get("Entrypoint") != ["/ros_entrypoint.sh"]
        or config.get("Cmd") != expected_command
        or config.get("WorkingDir") != "/workspace/multiagent_simulation"
        or host.get("Privileged") is not False
        or host.get("NetworkMode") != "host"
        or host.get("ReadonlyRootfs") is not True
        or host.get("RestartPolicy") != {"Name": "no", "MaximumRetryCount": 0}
        or host.get("CapAdd") is not None
        or host.get("CapDrop") != ["ALL"]
        or host.get("Tmpfs")
        != {"/tmp": "rw,nosuid,nodev,exec,size=4g,mode=1777"}
        or host.get("Devices") not in (None, [])
        or host.get("SecurityOpt")
        not in (
            ["no-new-privileges"],
            ["no-new-privileges:true"],
            ["label=disable", "no-new-privileges"],
            ["label=disable", "no-new-privileges:true"],
        )
        or host.get("DeviceRequests") != EXPECTED_GPU_DEVICE_REQUESTS
    ):
        raise ValueError("formal M1 Config/HostConfig is not exact")
    required_sources = {
        "/workspace/multiagent_simulation": (
            str(source_snapshot.resolve(strict=True)),
            False,
        ),
        "/workspace/multiagent_simulation/runs": (
            str(artifact_staging.resolve(strict=True)),
            True,
        ),
        "/workspace/multiagent_simulation/.external/ns-3": (
            str((project_root / ".external/ns-3").resolve(strict=True)),
            False,
        ),
        "/run/ams/container_id": (
            str(container_identity_file.resolve(strict=True)),
            False,
        ),
        "/run/ams/m0-receipt.json": (
            str(m0_receipt_path.resolve(strict=True)),
            False,
        ),
    }
    exact_mounts(final, required_sources)


def validate_validation_container(
    initial: dict[str, Any],
    final: dict[str, Any],
    image: dict[str, Any],
    *,
    run_id: str,
    container_id: str,
    main_container_id: str,
    image_digest: str,
    source_snapshot: Path,
    artifact_staging: Path,
    m0_receipt_path: Path,
    project_root: Path,
) -> None:
    if container_id == main_container_id:
        raise ValueError("M1 producer and independent validator containers are identical")
    immutable_container_configuration(initial, final)
    initial_state = initial.get("State", {})
    final_state = final.get("State", {})
    config = final.get("Config", {})
    host = final.get("HostConfig", {})
    image_config = image.get("Config", {}) if isinstance(image.get("Config"), dict) else {}
    expected_command = [
        "/usr/bin/python3.10",
        "network/scripts/validate_m1_health.py",
        "--run-dir",
        f"runs/{run_id}",
        "--no-write",
    ]
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
        or final_state.get("ExitCode") != 0
        or final_state.get("OOMKilled") is not False
        or final.get("RestartCount") != 0
        or image.get("Id") != image_digest
    ):
        raise ValueError("independent M1 validation lifecycle is not exact")
    if (
        config.get("Image") != image_digest
        or config.get("User") != "ubuntu"
        or config.get("Entrypoint") != ["/ros_entrypoint.sh"]
        or config.get("Cmd") != expected_command
        or config.get("WorkingDir") != "/workspace/multiagent_simulation"
        or config.get("Env") != image_config.get("Env")
        or host.get("Privileged") is not False
        or host.get("NetworkMode") != "none"
        or host.get("ReadonlyRootfs") is not True
        or host.get("RestartPolicy") != {"Name": "no", "MaximumRetryCount": 0}
        or host.get("CapAdd") is not None
        or host.get("CapDrop") != ["ALL"]
        or host.get("Devices") not in (None, [])
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
        raise ValueError("independent M1 validation Config/HostConfig is not exact")
    exact_mounts(
        final,
        {
            "/workspace/multiagent_simulation": (
                str(source_snapshot.resolve(strict=True)),
                False,
            ),
            "/workspace/multiagent_simulation/runs": (
                str(artifact_staging.resolve(strict=True)),
                False,
            ),
            "/workspace/multiagent_simulation/.external/ns-3": (
                str((project_root / ".external/ns-3").resolve(strict=True)),
                False,
            ),
            "/run/ams/m0-receipt.json": (
                str(m0_receipt_path.resolve(strict=True)),
                False,
            ),
        },
    )


def write_fsynced(path: Path, payload: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
        0o444,
    )
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written < 1:
                raise OSError(f"short M1 publication write: {path}")
            view = view[written:]
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o444)
    finally:
        os.close(descriptor)


def fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def freeze_and_fsync_tree(run_dir: Path) -> None:
    for path in sorted(run_dir.rglob("*"), reverse=True):
        if path.is_symlink():
            continue
        # The final receipt is independently re-derived by the unprivileged
        # image user.  Preserve immutability while keeping the published
        # evidence traversable and readable through its read-only bind mount.
        path.chmod(0o444 if path.is_file() else 0o555)
    run_dir.chmod(0o555)
    for path in sorted(run_dir.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    directories = [path for path in run_dir.rglob("*") if path.is_dir() and not path.is_symlink()]
    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        fsync_directory(directory)
    fsync_directory(run_dir)


def make_tree_removable(root: Path) -> None:
    directories = [
        path for path in root.rglob("*") if path.is_dir() and not path.is_symlink()
    ]
    for directory in sorted(directories, key=lambda item: len(item.parts)):
        if directory.lstat().st_uid == os.geteuid():
            directory.chmod(0o700)
    if root.lstat().st_uid == os.geteuid():
        root.chmod(0o700)


def publish_durable(run_dir: Path, destination: Path, staging_root: Path, runs_root: Path) -> None:
    if sorted(staging_root.iterdir()) != [run_dir]:
        raise ValueError("M1 staging parent contains unexpected entries")
    publish_source = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.publish-", dir=runs_root)
    )
    renamed = False
    try:
        shutil.copytree(
            run_dir,
            publish_source,
            dirs_exist_ok=True,
            symlinks=True,
            copy_function=shutil.copy2,
        )
        freeze_and_fsync_tree(publish_source)
        if tree_manifest(run_dir) != tree_manifest(publish_source):
            raise ValueError("direct-parent M1 publication copy differs from frozen source")
        make_tree_removable(staging_root)
        shutil.rmtree(staging_root)
        fsync_directory(runs_root)
        rename_noreplace(publish_source, destination)
        renamed = True
        fsync_directory(runs_root)
    except Exception:
        if publish_source.exists():
            make_tree_removable(publish_source)
            shutil.rmtree(publish_source, ignore_errors=True)
        if not renamed:
            raise
        quarantine = runs_root / f".{destination.name}.failed-{os.getpid()}"
        try:
            rename_noreplace(destination, quarantine)
            fsync_directory(runs_root)
        finally:
            pass
        raise


def build_m1_receipt(
    *,
    run_id: str,
    source_commit: str,
    image_reference: str,
    image_digest: str,
    runtime_container_id: str,
    validation_container_id: str,
    qualification_content_vector: dict[str, Any],
    inherited_m0_qualification: dict[str, Any],
    m0_status_authority: dict[str, Any],
    component_result: dict[str, Any],
    component_result_sha256: str,
    artifact_content_manifest: dict[str, Any],
    host_validation_content_manifest: dict[str, Any],
    m0_receipt_sha256: str,
    m0_status_validation_sha256: str,
) -> dict[str, Any]:
    """Build the exact M1 receipt/status hand-off schema."""

    receipt_path = f"runs/{run_id}/metrics/m1_host_final_receipt.json"
    qualification_contract = {
        "run_id": run_id,
        "receipt_path": receipt_path,
        "source_commit": source_commit,
        "image_digest": image_digest,
        "vector_sha256": qualification_content_vector.get("vector_sha256"),
        "component_result_sha256": component_result_sha256,
        "artifact_content_sha256": artifact_content_manifest["content_sha256"],
        "host_validation_content_sha256": host_validation_content_manifest[
            "content_sha256"
        ],
        "m0_receipt_sha256": m0_receipt_sha256,
        "m0_status_validation_sha256": m0_status_validation_sha256,
        "consumed_nodes": ["Q0", "Q1"],
    }
    return {
        "schema_version": 1,
        "contract": CONTRACT,
        "milestone": "M1",
        "run_id": run_id,
        "run_dir": f"runs/{run_id}",
        "receipt_path": receipt_path,
        "source_commit": source_commit,
        "image_reference": image_reference,
        "image_digest": image_digest,
        "runtime_container_id": runtime_container_id,
        "validation_container_id": validation_container_id,
        "consumed_nodes": ["Q0", "Q1"],
        "qualification_content_vector": qualification_content_vector,
        "inherited_m0_qualification": inherited_m0_qualification,
        "m0_status_authority": m0_status_authority,
        "component_result": component_result,
        "artifact_content_manifest": artifact_content_manifest,
        "host_validation_content_manifest": host_validation_content_manifest,
        "qualification_contract_sha256": sha256(canonical(qualification_contract)),
        "formal_accepted": True,
        "passed": True,
        "failures": [],
    }


def finalize(args: argparse.Namespace) -> dict[str, Any]:
    run_id = args.staging_run_dir.name
    if SAFE_RUN_ID.fullmatch(run_id) is None or args.publish_run_dir.name != run_id:
        raise ValueError("M1 run identity is unsafe or inconsistent")
    if SHA1.fullmatch(args.source_commit) is None:
        raise ValueError("M1 source commit is malformed")
    if (
        CONTAINER_ID.fullmatch(args.container_id) is None
        or CONTAINER_ID.fullmatch(args.validation_container_id) is None
        or args.validation_container_id == args.container_id
        or SHA256_IMAGE.fullmatch(args.image_digest) is None
    ):
        raise ValueError("M1 runtime container/image identity is malformed")
    project_root = args.project_root.resolve(strict=True)
    if project_root.is_symlink() or not project_root.is_dir():
        raise ValueError("M1 host project root is not one real directory")
    staging_root = args.staging_run_dir.parent.resolve(strict=True)
    runs_root = (project_root / "runs").resolve(strict=True)
    if staging_root.parent != runs_root or not staging_root.name.startswith(f".m1-stage-{run_id}."):
        raise ValueError("M1 staging root is not canonical")
    if args.publish_run_dir.parent.resolve(strict=True) != runs_root or args.publish_run_dir.exists():
        raise ValueError("M1 publication destination is not empty/canonical")
    source_snapshot = args.source_snapshot.resolve(strict=True)
    if re.fullmatch(r"/tmp/ams-m1-source\.[A-Za-z0-9]+", str(source_snapshot)) is None:
        raise ValueError("M1 source snapshot path is not canonical")
    if CODE_ROOT != source_snapshot:
        raise ValueError("M1 host finalizer is not executing from the read-only source snapshot")
    validate_source_snapshot(source_snapshot, args.source_commit)
    container_identity_file = args.container_identity_file.resolve(strict=True)
    if re.fullmatch(r"/tmp/ams-container-id\.[A-Za-z0-9]+", str(container_identity_file)) is None:
        raise ValueError("M1 host container-identity file path is not canonical")
    if read_regular(container_identity_file).decode("ascii").strip() != args.container_id:
        raise ValueError("M1 host container-identity file differs from producer")

    m0_status_raw = read_regular(args.m0_status_validation)
    m0_status = strict_json(m0_status_raw, "M0 live-status authority")
    m0_canonical = m0_status.get("receipt_path") if isinstance(m0_status, dict) else None
    if (
        not isinstance(m0_status, dict)
        or m0_status.get("schema_version") != 1
        or m0_status.get("contract") != M0_STATUS_CONTRACT
        or m0_status.get("passed") is not True
        or m0_status.get("failures") != []
        or m0_status.get("report_commit") != args.source_commit
        or not isinstance(m0_canonical, str)
        or re.fullmatch(
            r"runs/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}/metrics/m0_host_final_receipt\.json",
            m0_canonical,
        )
        is None
    ):
        raise ValueError("formal M1 does not inherit one current passing M0 status authority")
    m0_receipt_path = args.m0_receipt.resolve(strict=True)
    if m0_receipt_path != (project_root / m0_canonical).resolve(strict=True):
        raise ValueError("formal M1 M0 receipt path differs from live-status authority")
    m0_receipt_info = m0_receipt_path.lstat()
    if m0_receipt_info.st_mode & 0o222:
        raise ValueError("formal M1 inherited M0 receipt is writable")
    m0_receipt_raw = read_regular(m0_receipt_path)
    m0_receipt = strict_json(m0_receipt_raw, "inherited M0 host-final receipt")
    if m0_receipt_raw != (
        json.dumps(m0_receipt, allow_nan=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8"):
        raise ValueError("inherited M0 host-final receipt is not canonical JSON")
    m0_receipt_sha256 = sha256(m0_receipt_raw)
    m0_vector = (
        m0_receipt.get("qualification_content_vector")
        if isinstance(m0_receipt, dict)
        else None
    )
    m0_host_final = (
        m0_receipt.get("gates", {}).get("host_final", {})
        if isinstance(m0_receipt, dict)
        else {}
    )
    m0_capability = (
        m0_host_final.get("details", {}).get("isolated_target_runtime_capability")
        if isinstance(m0_host_final, dict)
        else None
    )
    if (
        not isinstance(m0_receipt, dict)
        or m0_receipt.get("schema_version") != 3
        or m0_receipt.get("contract") != M0_RECEIPT_CONTRACT
        or m0_receipt.get("milestone") != "M0"
        or m0_receipt.get("receipt_path") != m0_canonical
        or m0_receipt.get("formal_accepted") is not True
        or m0_receipt.get("passed") is not True
        or m0_receipt.get("failures") != []
        or m0_receipt.get("consumed_nodes") != ["Q0"]
        or not isinstance(m0_vector, dict)
        or not isinstance(m0_capability, dict)
        or m0_capability.get("contract") != "ams.m0.isolated-capability-probe/v1"
        or m0_capability.get("image_digest") != args.image_digest
        or m0_capability.get("exit_code") != 0
        or m0_capability.get("no_candidate_mounts") is not True
        or m0_capability.get("tun_device") is not True
        or m0_capability.get("passwordless_sudo") is not True
        or m0_capability.get("unshare_network_namespace") is not True
    ):
        raise ValueError("inherited M0 receipt does not prove exact-image Q0 capabilities")

    result_raw = read_regular(args.staging_run_dir / "metrics/m1_result.json")
    independent_raw = read_regular(args.independent_result)
    result = strict_json(result_raw, "M1 component result")
    independent = strict_json(independent_raw, "independent M1 result")
    if result != independent or not isinstance(result, dict) or result.get("passed") is not True:
        raise ValueError("independent exact-image M1 result differs or did not pass")
    result_gates = result.get("gates") if isinstance(result.get("gates"), dict) else {}
    if (
        result.get("schema_version") != 2
        or result.get("contract") != RESULT_CONTRACT
        or result.get("run_id") != run_id
        or result.get("run_dir") != f"runs/{run_id}"
        or result.get("component_qualified") is not True
        or result.get("formal_accepted") is not False
        or result.get("component_only") is not True
        or result.get("p0_eligible") is not False
        or result.get("failures") != []
        or set(result_gates)
        != {"provenance", "five_uav_health", "scene", "runtime_inputs"}
        or any(gate.get("status") != "passed" for gate in result_gates.values())
    ):
        raise ValueError("M1 result identity is invalid")
    provenance_raw = read_regular(args.staging_run_dir / "metrics/provenance.json")
    provenance = strict_json(provenance_raw, "M1 provenance")
    vector = provenance.get("qualification_content_vector") if isinstance(provenance, dict) else None
    consumption = provenance.get("qualification_consumption") if isinstance(provenance, dict) else None
    container = provenance.get("container_image") if isinstance(provenance, dict) else None
    inherited_m0 = (
        provenance.get("inherited_m0_qualification")
        if isinstance(provenance, dict)
        else None
    )
    if (
        provenance.get("acceptance_eligible") is not True
        or provenance.get("acceptance_blockers") != []
        or provenance.get("git_commit") != args.source_commit
        or not isinstance(vector, dict)
        or vector.get("git_commit") != args.source_commit
        or not isinstance(consumption, dict)
        or consumption.get("profile") != "m1_component"
        or consumption.get("consumed_nodes") != ["Q0", "Q1"]
        or not isinstance(container, dict)
        or container.get("digest") != args.image_digest
        or container.get("runtime_container_id") != args.container_id
        or not qualification_prefixes_equal(m0_vector, vector, ["Q0"])
        or not isinstance(inherited_m0, dict)
        or inherited_m0.get("schema_version") != 1
        or inherited_m0.get("contract") != M0_INHERITANCE_CONTRACT
        or inherited_m0.get("status_report_commit") != args.source_commit
        or inherited_m0.get("canonical_receipt_path") != m0_canonical
        or inherited_m0.get("mounted_receipt_path") != "/run/ams/m0-receipt.json"
        or inherited_m0.get("receipt_sha256") != m0_receipt_sha256
        or inherited_m0.get("receipt_contract") != M0_RECEIPT_CONTRACT
        or inherited_m0.get("receipt_run_id") != m0_receipt.get("run_id")
        or inherited_m0.get("qualification_contract_sha256")
        != m0_receipt.get("qualification_contract_sha256")
        or inherited_m0.get("qualification_vector_sha256")
        != m0_vector.get("vector_sha256")
        or inherited_m0.get("qualification_vector_commit") != m0_vector.get("git_commit")
        or inherited_m0.get("image_digest") != args.image_digest
        or inherited_m0.get("consumed_nodes") != ["Q0"]
        or inherited_m0.get("capabilities")
        != {
            "tun_device": True,
            "passwordless_sudo": True,
            "unshare_network_namespace": True,
        }
        or inherited_m0.get("available") is not True
    ):
        raise ValueError("M1 provenance/source/Q-vector/runtime identity is invalid")

    initial, initial_raw = one_inspect(args.initial_control_dir / "initial_container_inspect.json", "M1 initial container")
    initial_image, initial_image_raw = one_inspect(args.initial_control_dir / "initial_image_inspect.json", "M1 initial image")
    final, final_raw = one_inspect(args.initial_control_dir / "final_container_inspect.json", "M1 final container")
    final_image, final_image_raw = one_inspect(args.initial_control_dir / "final_image_inspect.json", "M1 final image")
    if initial_image.get("Id") != args.image_digest or final_image.get("Id") != args.image_digest or initial_image != final_image:
        raise ValueError("M1 exact image inspection is invalid or changed")
    validate_main_container(
        initial,
        final,
        run_id=run_id,
        container_id=args.container_id,
        image_digest=args.image_digest,
        image_reference=args.image_reference,
        source_commit=args.source_commit,
        source_snapshot=source_snapshot,
        artifact_staging=staging_root,
        container_identity_file=container_identity_file,
        m0_receipt_path=m0_receipt_path,
        m0_receipt_canonical=m0_canonical,
        m0_receipt_sha256=m0_receipt_sha256,
        project_root=project_root,
    )
    validation_initial, validation_initial_raw = one_inspect(
        args.initial_control_dir / "validation_initial_container_inspect.json",
        "M1 validation initial container",
    )
    validation_final, validation_final_raw = one_inspect(
        args.initial_control_dir / "validation_final_container_inspect.json",
        "M1 validation final container",
    )
    validation_image, validation_image_raw = one_inspect(
        args.initial_control_dir / "validation_image_inspect.json",
        "M1 validation image",
    )
    validate_validation_container(
        validation_initial,
        validation_final,
        validation_image,
        run_id=run_id,
        container_id=args.validation_container_id,
        main_container_id=args.container_id,
        image_digest=args.image_digest,
        source_snapshot=source_snapshot,
        artifact_staging=staging_root,
        m0_receipt_path=m0_receipt_path,
        project_root=project_root,
    )
    if read_regular(args.initial_control_dir / "validation_stderr.txt", allow_empty=True) != b"":
        raise ValueError("independent M1 validation emitted stderr")

    artifact_manifest = tree_manifest(
        args.staging_run_dir,
        excluded={"host_validation", "metrics/m1_host_final_receipt.json"},
    )
    host_validation = args.staging_run_dir / "host_validation"
    host_validation.mkdir(mode=0o700)
    raw_values = {
        "main/initial_container_inspect.json": initial_raw,
        "main/final_container_inspect.json": final_raw,
        "main/initial_image_inspect.json": initial_image_raw,
        "main/final_image_inspect.json": final_image_raw,
        "validation/initial_container_inspect.json": validation_initial_raw,
        "validation/final_container_inspect.json": validation_final_raw,
        "validation/image_inspect.json": validation_image_raw,
        "validation/result.json": independent_raw,
        "validation/stderr.txt": b"",
        "m0/status_validation.json": m0_status_raw,
        "m0/host_final_receipt.json": m0_receipt_raw,
    }
    raw_records: dict[str, dict[str, Any]] = {}
    for relative, payload in sorted(raw_values.items()):
        destination = host_validation / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        write_fsynced(destination, payload)
        raw_records[relative] = {"bytes": len(payload), "sha256": sha256(payload)}
    host_manifest = {
        "schema_version": 1,
        "contract": "ams.m1.host-validation-content/v1",
        "files": raw_records,
        "file_count": len(raw_records),
        "content_sha256": sha256(canonical(raw_records)),
    }
    manifest_path = host_validation / "content_manifest.json"
    write_fsynced(
        manifest_path,
        (json.dumps(host_manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    receipt = build_m1_receipt(
        run_id=run_id,
        source_commit=args.source_commit,
        image_reference=args.image_reference,
        image_digest=args.image_digest,
        runtime_container_id=args.container_id,
        validation_container_id=args.validation_container_id,
        qualification_content_vector=vector,
        inherited_m0_qualification=inherited_m0,
        m0_status_authority={
            "contract": M0_STATUS_CONTRACT,
            "report_commit": args.source_commit,
            "receipt_path": m0_canonical,
            "receipt_sha256": m0_receipt_sha256,
            "status_validation_sha256": sha256(m0_status_raw),
        },
        component_result={
            "path": f"runs/{run_id}/metrics/m1_result.json",
            "bytes": len(result_raw),
            "sha256": sha256(result_raw),
        },
        component_result_sha256=sha256(result_raw),
        artifact_content_manifest=artifact_manifest,
        host_validation_content_manifest=host_manifest,
        m0_receipt_sha256=m0_receipt_sha256,
        m0_status_validation_sha256=sha256(m0_status_raw),
    )
    receipt_path = args.staging_run_dir / "metrics/m1_host_final_receipt.json"
    write_fsynced(
        receipt_path,
        (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    publish_durable(
        args.staging_run_dir,
        args.publish_run_dir,
        staging_root,
        runs_root,
    )
    return receipt


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staging-run-dir", type=Path, required=True)
    parser.add_argument("--publish-run-dir", type=Path, required=True)
    parser.add_argument("--source-snapshot", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--container-id", required=True)
    parser.add_argument("--validation-container-id", required=True)
    parser.add_argument("--image-reference", required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--initial-control-dir", type=Path, required=True)
    parser.add_argument("--independent-result", type=Path, required=True)
    parser.add_argument("--container-identity-file", type=Path, required=True)
    parser.add_argument("--m0-status-validation", type=Path, required=True)
    parser.add_argument("--m0-receipt", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    try:
        receipt = finalize(parse_args())
    except Exception as exc:
        print(json.dumps({"passed": False, "failures": [str(exc)]}, indent=2))
        return 1
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
