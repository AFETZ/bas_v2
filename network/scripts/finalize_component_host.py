#!/usr/bin/python3.10
"""Independently freeze and atomically publish a downstream component run."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any

CODE_ROOT = Path(__file__).resolve().parents[2]
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

from network.scripts.host_finalization_common import (  # noqa: E402
    exact_mounts,
    immutable_container_configuration,
    one_inspect,
    read_regular,
    rename_noreplace,
    strict_json,
    tree_manifest,
    validate_source_snapshot,
)
from network.validation.component_profiles import (  # noqa: E402
    expected_gpu_device_requests,
    expected_radio_provider_runtime,
    load_profiles,
    validate_component_python_runtime,
)
from network.validation.qualification_identity import (  # noqa: E402
    qualification_consumption,
)


SAFE_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
SHA1 = re.compile(r"[0-9a-f]{40}")
SHA256_IMAGE = re.compile(r"sha256:[0-9a-f]{64}")
CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
EXPECTED_GPU_DEVICE_REQUESTS = expected_gpu_device_requests("compute,utility")


def canonical(value: Any) -> bytes:
    return json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def pretty(value: Any) -> bytes:
    return json.dumps(value, allow_nan=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


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


def write_exclusive(path: Path, payload: bytes, mode: int = 0o400) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    descriptor = os.open(path, flags, mode)
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.chmod(path, mode, follow_symlinks=False)


def copy_exclusive(source: Path, destination: Path, *, allow_empty: bool = False) -> bytes:
    payload = read_regular(source, allow_empty=allow_empty)
    write_exclusive(destination, payload)
    return payload


def one_image(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    document, raw = one_inspect(path, label)
    return document, raw


def expected_cap_add(profile: dict[str, Any]) -> list[str] | None:
    values = [f"CAP_{value}" for value in profile["main_cap_add"]]
    return values or None


def expected_devices(profile: dict[str, Any]) -> list[dict[str, str]]:
    if profile["main_devices"] == []:
        return []
    return [
        {
            "PathOnHost": "/dev/net/tun",
            "PathInContainer": "/dev/net/tun",
            "CgroupPermissions": "rwm",
        }
    ]


def expected_main_user(profile: dict[str, Any]) -> str:
    return "root:1000" if profile["main_devices"] else "ubuntu"


def expected_main_tmpfs(profile: dict[str, Any]) -> dict[str, str]:
    result = {"/tmp": "rw,nosuid,nodev,exec,size=4g,mode=1777"}
    if profile["main_devices"]:
        result["/run/netns"] = "rw,nosuid,nodev,noexec,size=16m,mode=0755"
    return result


def valid_main_security_options(profile: dict[str, Any], value: Any) -> bool:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return False
    normalized = {
        "no-new-privileges" if item == "no-new-privileges:true" else item
        for item in value
    }
    normalized.discard("label=disable")
    expected = {"no-new-privileges"}
    if profile["main_devices"]:
        expected.add("apparmor=unconfined")
    return normalized == expected


def validate_lifecycle(
    initial: dict[str, Any],
    final: dict[str, Any],
    *,
    container_id: str,
    image_digest: str,
    label: str,
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
        raise ValueError(f"{label} container lifecycle is not exact created-to-exited/zero")


def validate_main_container(
    initial: dict[str, Any],
    final: dict[str, Any],
    *,
    profile_name: str,
    profile: dict[str, Any],
    run_id: str,
    container_id: str,
    image_reference: str,
    image_digest: str,
    source_commit: str,
    source_snapshot: Path,
    artifact_staging: Path,
    container_identity_file: Path,
    status_result: Path,
    prerequisites_path: Path,
    prerequisite_receipts: dict[str, Path],
    project_root: Path,
    m0_record: dict[str, Any],
) -> None:
    validate_lifecycle(
        initial,
        final,
        container_id=container_id,
        image_digest=image_digest,
        label="component producer",
    )
    config = final.get("Config", {})
    host = final.get("HostConfig", {})
    environment = environment_map(config.get("Env"))
    required_ams = {
        "AMS_CONTAINER_IMAGE": image_reference,
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
        "AMS_M1_M0_RECEIPT_CANONICAL_PATH": m0_record["canonical_path"],
        "AMS_M1_M0_RECEIPT_SHA256": m0_record["sha256"],
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
        raise ValueError("component producer environment is not exact")
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
        or config.get("User") != expected_main_user(profile)
        or config.get("Entrypoint") != ["/ros_entrypoint.sh"]
        or config.get("Cmd") != expected_command
        or config.get("WorkingDir") != "/workspace/multiagent_simulation"
        or host.get("Privileged") is not False
        or host.get("NetworkMode") != profile["main_network"]
        or host.get("ReadonlyRootfs") is not True
        or host.get("RestartPolicy") != {"Name": "no", "MaximumRetryCount": 0}
        or host.get("CapAdd") != expected_cap_add(profile)
        or host.get("CapDrop") != ["ALL"]
        or host.get("Devices") != expected_devices(profile)
        or host.get("DeviceRequests")
        != expected_gpu_device_requests(profile["nvidia_driver_capabilities"])
        or host.get("Tmpfs") != expected_main_tmpfs(profile)
        or not valid_main_security_options(profile, host.get("SecurityOpt"))
    ):
        raise ValueError("component producer Config/HostConfig is not exact")
    expected_mounts: dict[str, tuple[str, bool]] = {
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
        "/run/ams/status-validation.json": (
            str(status_result.resolve(strict=True)),
            False,
        ),
        "/run/ams/prerequisites.json": (
            str(prerequisites_path.resolve(strict=True)),
            False,
        ),
    }
    for name, path in prerequisite_receipts.items():
        expected_mounts[f"/run/ams/prerequisites/{name}.json"] = (
            str(path.resolve(strict=True)),
            False,
        )
    exact_mounts(final, expected_mounts)


def validate_validation_container(
    initial: dict[str, Any],
    final: dict[str, Any],
    image: dict[str, Any],
    *,
    profile: dict[str, Any],
    run_id: str,
    container_id: str,
    main_container_id: str,
    image_digest: str,
    source_snapshot: Path,
    artifact_staging: Path,
    status_result: Path,
    prerequisites_path: Path,
    prerequisite_receipts: dict[str, Path],
    project_root: Path,
) -> None:
    if container_id == main_container_id:
        raise ValueError("component producer and validator container IDs are identical")
    validate_lifecycle(
        initial,
        final,
        container_id=container_id,
        image_digest=image_digest,
        label="independent component validator",
    )
    config = final.get("Config", {})
    host = final.get("HostConfig", {})
    image_config = image.get("Config", {}) if isinstance(image.get("Config"), dict) else {}
    expected_args = [
        value.replace("{run_dir}", f"runs/{run_id}")
        for value in profile["validator_arguments"]
    ]
    expected_command = ["/usr/bin/python3.10", profile["validator"], *expected_args]
    if (
        image.get("Id") != image_digest
        or config.get("Image") != image_digest
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
        raise ValueError("independent component validator Config/HostConfig is not exact")
    expected_mounts: dict[str, tuple[str, bool]] = {
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
        "/run/ams/status-validation.json": (
            str(status_result.resolve(strict=True)),
            False,
        ),
        "/run/ams/prerequisites.json": (
            str(prerequisites_path.resolve(strict=True)),
            False,
        ),
    }
    for name, path in prerequisite_receipts.items():
        expected_mounts[f"/run/ams/prerequisites/{name}.json"] = (
            str(path.resolve(strict=True)),
            False,
        )
    exact_mounts(final, expected_mounts)


def validate_component_result(
    profile: dict[str, Any], producer_payload: bytes, independent_payload: bytes
) -> dict[str, Any]:
    producer = strict_json(producer_payload, "producer component result")
    independent = strict_json(independent_payload, "independent component result")
    if not isinstance(producer, dict) or not isinstance(independent, dict):
        raise ValueError("component results are not JSON objects")
    if producer != independent:
        raise ValueError("producer and independent component results differ")
    observed_contract = producer.get("contract", producer.get("validation_contract"))
    if observed_contract != profile["result_contract"] or producer.get("passed") is not True:
        raise ValueError("component result contract/pass claim is invalid")
    if "failures" in producer and producer.get("failures") != []:
        raise ValueError("component result retains failures")
    gates = producer.get("gates")
    if not isinstance(gates, dict) or not gates:
        raise ValueError("component result has no independently derived gates")
    for name, gate in gates.items():
        if not isinstance(gate, dict):
            raise ValueError(f"component gate {name} is malformed")
        passed = gate.get("passed") is True or gate.get("status") == "passed"
        if not passed or gate.get("failures", []) != []:
            raise ValueError(f"component gate {name} did not pass exactly")
    return producer


def validate_provenance_python_runtime(
    profile: dict[str, Any], provenance: Any
) -> None:
    dependencies = (
        provenance.get("dependency_versions")
        if isinstance(provenance, dict)
        and isinstance(provenance.get("dependency_versions"), dict)
        else {}
    )
    failures = validate_component_python_runtime(
        profile, dependencies.get("python_runtime"), dependencies
    )
    if failures:
        raise ValueError(
            "component provenance Python runtime identity is not exact: "
            + "; ".join(failures)
        )


def validate_provenance_implementation(
    profile_name: str, provenance: Any
) -> None:
    selected_provider = "tcp_jsonl_real_sionna"
    expected = {
        "packet_ingress_mode": "tap_bridge_external",
        "medium_model": "csma_surrogate",
        "radio_provider_id": selected_provider,
        **expected_radio_provider_runtime(profile_name, selected_provider),
    }
    implementation = (
        provenance.get("implementation") if isinstance(provenance, dict) else None
    )
    if implementation != expected:
        raise ValueError(
            "component provenance implementation/provider-consumption identity is not exact"
        )


def freeze_tree(root: Path) -> None:
    paths = sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True)
    for path in paths:
        info = path.lstat()
        if stat.S_ISREG(info.st_mode):
            os.chmod(path, 0o400, follow_symlinks=False)
        elif stat.S_ISDIR(info.st_mode):
            os.chmod(path, 0o500, follow_symlinks=False)
        elif not stat.S_ISLNK(info.st_mode):
            raise ValueError(f"special component artifact cannot be frozen: {path}")
    os.chmod(root, 0o500, follow_symlinks=False)


def fsync_tree(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink():
            continue
        flags = os.O_RDONLY | os.O_NOFOLLOW
        if path.is_dir():
            flags |= os.O_DIRECTORY
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def parse_receipt_arguments(values: list[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        name, separator, raw_path = value.partition("=")
        if (
            not separator
            or re.fullmatch(r"m[0-8]|[a-z][a-z0-9_]{2,63}", name) is None
            or name in result
            or not raw_path
        ):
            raise ValueError("--prerequisite-receipt must be unique mN=/absolute/path")
        path = Path(raw_path)
        if not path.is_absolute():
            raise ValueError("prerequisite receipt path is not absolute")
        result[name] = path
    return dict(sorted(result.items()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--staging-run-dir", type=Path, required=True)
    parser.add_argument("--publish-run-dir", type=Path, required=True)
    parser.add_argument("--source-snapshot", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--container-id", required=True)
    parser.add_argument("--validation-container-id", required=True)
    parser.add_argument("--image-reference", required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--control-dir", type=Path, required=True)
    parser.add_argument("--independent-result", type=Path, required=True)
    parser.add_argument("--container-identity-file", type=Path, required=True)
    parser.add_argument("--status-validation", type=Path, required=True)
    parser.add_argument("--prerequisites", type=Path, required=True)
    parser.add_argument("--prerequisite-receipt", action="append", default=[])
    args = parser.parse_args(argv)

    project_root = args.project_root.resolve(strict=True)
    profiles = load_profiles(
        project_root / "network/config/component_acceptance_profiles.json"
    )
    if args.profile not in profiles:
        raise SystemExit("unknown component profile")
    profile = profiles[args.profile]
    staging_run = args.staging_run_dir.resolve(strict=True)
    publish_run = args.publish_run_dir.absolute()
    source_snapshot = args.source_snapshot.resolve(strict=True)
    control = args.control_dir.resolve(strict=True)
    identity_file = args.container_identity_file.resolve(strict=True)
    status_validation = args.status_validation.resolve(strict=True)
    prerequisites_path = args.prerequisites.resolve(strict=True)
    prerequisite_receipts = parse_receipt_arguments(args.prerequisite_receipt)
    run_id = staging_run.name
    if (
        SAFE_RUN_ID.fullmatch(run_id) is None
        or publish_run != project_root / "runs" / run_id
        or publish_run.exists()
        or staging_run.is_symlink()
        or not staging_run.is_dir()
        or SHA1.fullmatch(args.source_commit) is None
        or CONTAINER_ID.fullmatch(args.container_id) is None
        or CONTAINER_ID.fullmatch(args.validation_container_id) is None
        or SHA256_IMAGE.fullmatch(args.image_digest) is None
    ):
        raise SystemExit("component publication/source/container identity is invalid")
    validate_source_snapshot(source_snapshot, args.source_commit)

    prerequisite_document = strict_json(
        read_regular(prerequisites_path), "component prerequisites"
    )
    milestone_records = (
        prerequisite_document.get("receipts", {})
        if isinstance(prerequisite_document, dict)
        else {}
    )
    component_records = (
        prerequisite_document.get("component_receipts", {})
        if isinstance(prerequisite_document, dict)
        else {}
    )
    all_prerequisite_records = (
        {**milestone_records, **component_records}
        if isinstance(milestone_records, dict)
        and isinstance(component_records, dict)
        and not set(milestone_records).intersection(component_records)
        else {}
    )
    if (
        not isinstance(prerequisite_document, dict)
        or prerequisite_document.get("schema_version") != 1
        or prerequisite_document.get("contract") != "ams.component-prerequisites/v1"
        or prerequisite_document.get("profile") != args.profile
        or prerequisite_document.get("source_commit") != args.source_commit
        or set(all_prerequisite_records) != set(prerequisite_receipts)
    ):
        raise SystemExit("component prerequisite manifest is not exact")
    status_payload = read_regular(status_validation)
    if prerequisite_document.get("status", {}).get("result_sha256") != sha256(
        status_payload
    ):
        raise SystemExit("component prerequisite manifest does not bind status result")
    for name, path in prerequisite_receipts.items():
        record = all_prerequisite_records[name]
        payload = read_regular(path)
        if (
            not isinstance(record, dict)
            or record.get("host_path") != str(path.resolve(strict=True))
            or record.get("sha256") != sha256(payload)
        ):
            raise SystemExit(f"component prerequisite receipt differs: {name}")
    if "m0" not in prerequisite_receipts:
        raise SystemExit("component prerequisite set does not contain M0")
    m0_record = prerequisite_document["receipts"]["m0"]

    main_initial, main_initial_raw = one_inspect(
        control / "initial_container_inspect.json", "initial component container"
    )
    main_final, main_final_raw = one_inspect(
        control / "final_container_inspect.json", "final component container"
    )
    main_image_initial, main_image_initial_raw = one_image(
        control / "initial_image_inspect.json", "initial component image"
    )
    main_image_final, main_image_final_raw = one_image(
        control / "final_image_inspect.json", "final component image"
    )
    validation_initial, validation_initial_raw = one_inspect(
        control / "validation_initial_container_inspect.json",
        "initial component validation container",
    )
    validation_final, validation_final_raw = one_inspect(
        control / "validation_final_container_inspect.json",
        "final component validation container",
    )
    validation_image, validation_image_raw = one_image(
        control / "validation_image_inspect.json", "component validation image"
    )
    if (
        main_image_initial != main_image_final
        or main_image_initial.get("Id") != args.image_digest
        or validation_image != main_image_final
    ):
        raise SystemExit("component exact image inspections differ")
    validate_main_container(
        main_initial,
        main_final,
        profile_name=args.profile,
        profile=profile,
        run_id=run_id,
        container_id=args.container_id,
        image_reference=args.image_reference,
        image_digest=args.image_digest,
        source_commit=args.source_commit,
        source_snapshot=source_snapshot,
        artifact_staging=staging_run.parent,
        container_identity_file=identity_file,
        status_result=status_validation,
        prerequisites_path=prerequisites_path,
        prerequisite_receipts=prerequisite_receipts,
        project_root=project_root,
        m0_record=m0_record,
    )
    validate_validation_container(
        validation_initial,
        validation_final,
        validation_image,
        profile=profile,
        run_id=run_id,
        container_id=args.validation_container_id,
        main_container_id=args.container_id,
        image_digest=args.image_digest,
        source_snapshot=source_snapshot,
        artifact_staging=staging_run.parent,
        status_result=status_validation,
        prerequisites_path=prerequisites_path,
        prerequisite_receipts=prerequisite_receipts,
        project_root=project_root,
    )

    producer_result_path = staging_run / profile["result_path"]
    producer_result_payload = read_regular(producer_result_path)
    independent_result_payload = read_regular(args.independent_result.resolve(strict=True))
    result = validate_component_result(
        profile, producer_result_payload, independent_result_payload
    )
    provenance_payload = read_regular(staging_run / "metrics/provenance.json")
    provenance = strict_json(provenance_payload, "component run provenance")
    vector = (
        provenance.get("qualification_content_vector")
        if isinstance(provenance, dict)
        else None
    )
    consumption = (
        provenance.get("qualification_consumption")
        if isinstance(provenance, dict)
        else None
    )
    if (
        not isinstance(provenance, dict)
        or provenance.get("git_commit") != args.source_commit
        or provenance.get("acceptance_eligible") is not True
        or provenance.get("acceptance_blockers") != []
        or not isinstance(vector, dict)
        or vector.get("git_commit") != args.source_commit
        or not isinstance(consumption, dict)
        or consumption != qualification_consumption(vector, args.profile)
        or consumption.get("consumed_nodes") != profile["consumed_nodes"]
    ):
        raise SystemExit("component provenance qualification identity is not exact")
    try:
        validate_provenance_python_runtime(profile, provenance)
        validate_provenance_implementation(args.profile, provenance)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    qualification_contract_sha256 = sha256(
        canonical(
            {
                "contract": "ams.component-qualification/v1",
                "profile": args.profile,
                "source_commit": args.source_commit,
                "image_digest": args.image_digest,
                "consumed_nodes": profile["consumed_nodes"],
                "consumed_node_sha256": consumption["consumed_node_sha256"],
                "result_contract": profile["result_contract"],
                "result_sha256": sha256(producer_result_payload),
            }
        )
    )
    pre_host_manifest = tree_manifest(staging_run)

    host_dir = staging_run / "host_validation"
    host_dir.mkdir(mode=0o700)
    raw_files = {
        "main/initial_container_inspect.json": main_initial_raw,
        "main/final_container_inspect.json": main_final_raw,
        "main/initial_image_inspect.json": main_image_initial_raw,
        "main/final_image_inspect.json": main_image_final_raw,
        "validation/initial_container_inspect.json": validation_initial_raw,
        "validation/final_container_inspect.json": validation_final_raw,
        "validation/image_inspect.json": validation_image_raw,
        "validation/result.json": independent_result_payload,
        "validation/stderr.txt": read_regular(
            control / "validation_stderr.txt", allow_empty=True
        ),
        "status/validation.json": read_regular(status_validation),
        "status/prerequisites.json": read_regular(prerequisites_path),
    }
    for name, path in prerequisite_receipts.items():
        raw_files[f"status/receipts/{name}.json"] = read_regular(path)
    raw_records: dict[str, dict[str, Any]] = {}
    for relative, payload in sorted(raw_files.items()):
        write_exclusive(host_dir / relative, payload)
        raw_records[relative] = {"bytes": len(payload), "sha256": sha256(payload)}
    host_manifest = {
        "schema_version": 1,
        "contract": "ams.component-host-validation-manifest/v1",
        "files": raw_records,
        "file_count": len(raw_records),
        "content_sha256": sha256(canonical(raw_records)),
    }
    write_exclusive(host_dir / "content_manifest.json", pretty(host_manifest))

    receipt_path = f"runs/{run_id}/metrics/{profile['receipt_name']}"
    receipt = {
        "schema_version": 1,
        "contract": profile["receipt_contract"],
        "profile": args.profile,
        "run_id": run_id,
        "receipt_path": receipt_path,
        "source_commit": args.source_commit,
        "image_reference": args.image_reference,
        "image_digest": args.image_digest,
        "container_id": args.container_id,
        "validation_container_id": args.validation_container_id,
        "consumed_nodes": profile["consumed_nodes"],
        "qualification_content_vector": vector,
        "qualification_consumption": consumption,
        "qualification_contract_sha256": qualification_contract_sha256,
        "formal_accepted": True,
        "passed": True,
        "failures": [],
        "result_contract": profile["result_contract"],
        "result_sha256": sha256(producer_result_payload),
        "result": result,
        "component_content_manifest": pre_host_manifest,
        "host_validation_manifest": host_manifest,
        "status_authority": prerequisite_document["status"],
        "prerequisite_receipts": prerequisite_document["receipts"],
        "required_component_receipts": prerequisite_document["component_receipts"],
    }
    receipt_file = staging_run / "metrics" / profile["receipt_name"]
    write_exclusive(receipt_file, pretty(receipt))
    publication_manifest = tree_manifest(
        staging_run,
        excluded={"metrics/component_publication_manifest.json"},
    )
    write_exclusive(
        staging_run / "metrics/component_publication_manifest.json",
        pretty(publication_manifest),
    )
    freeze_tree(staging_run)
    fsync_tree(staging_run)
    rename_noreplace(staging_run, publish_run)
    parent_descriptor = os.open(publish_run.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent_descriptor)
    finally:
        os.close(parent_descriptor)
    try:
        staging_run.parent.rmdir()
    except OSError:
        pass
    print(pretty(receipt).decode("utf-8"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
