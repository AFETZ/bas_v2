#!/usr/bin/env python3
"""Write the immutable scenario/world binding used by the M1 health gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath
from typing import Any

import yaml


ROOT_DIR = Path(__file__).resolve().parents[2]
SOURCE_WORLDS_RELATIVE = PurePosixPath("src/multiagent_simulation/worlds")
SOURCE_PACKAGE_RELATIVE = PurePosixPath("src/multiagent_simulation")
INSTALLED_PACKAGE_RELATIVE = PurePosixPath(
    "install/multiagent_simulation/share/multiagent_simulation"
)
LAUNCH_SOURCE_RELATIVE = PurePosixPath(
    "src/multiagent_simulation/launch/multiagent_simulation.launch.py"
)
M1_SCENE_RECORD = "metrics/m1_scene_provenance.json"
M1_CONTRACT_ID = "ams.m1.health/v3"
M1_PLAN_PATH = "doc/network_radio_integration_plan_v3.md"
SHA256 = re.compile(r"[0-9a-f]{64}")
SAFE_WORLD_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}")
SAFE_MODEL_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}")
MAX_TRANSITIVE_RESOURCES = 256
ROBOT_DESCRIPTION_PORT_TOKEN = "<fdm_port_in>9002</fdm_port_in>"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_sha256(files: dict[str, str]) -> str:
    """Hash an ordered path/content manifest without ambiguous concatenation."""

    digest = hashlib.sha256()
    for relative, file_hash in sorted(files.items()):
        encoded = relative.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(bytes.fromhex(file_hash))
    return digest.hexdigest()


def canonical_relative(value: Any, *, label: str, suffix: str | None = None) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ValueError(f"{label} is not a non-empty POSIX relative path")
    if any(character.isspace() for character in value):
        raise ValueError(f"{label} may not contain whitespace")
    pure = PurePosixPath(value)
    if (
        pure.is_absolute()
        or pure.as_posix() != value
        or any(part in ("", ".", "..") for part in pure.parts)
    ):
        raise ValueError(f"{label} is not a canonical POSIX relative path")
    if suffix is not None and pure.suffix.lower() != suffix.lower():
        raise ValueError(f"{label} must end in {suffix}")
    return value


def canonical_robot_model(value: Any) -> str:
    model = canonical_relative(value, label="robot model")
    if len(PurePosixPath(model).parts) != 1 or SAFE_MODEL_NAME.fullmatch(model) is None:
        raise ValueError("robot model must be one canonical model-directory name")
    return model


def _lexical_under(path: Path, root: Path, *, label: str) -> Path:
    root = Path(os.path.abspath(root))
    candidate = Path(os.path.abspath(path if path.is_absolute() else root / path))
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} escapes the runtime checkout") from exc
    return candidate


def regular_source_file(path: Path, root: Path, *, label: str) -> Path:
    candidate = _lexical_under(path, root, label=label)
    current = Path(os.path.abspath(root))
    try:
        relative = candidate.relative_to(current)
        for part in relative.parts:
            current = current / part
            item_stat = current.lstat()
            if stat.S_ISLNK(item_stat.st_mode):
                raise ValueError(f"{label} contains a symbolic-link component")
        item_stat = candidate.lstat()
    except OSError as exc:
        raise ValueError(f"{label} is missing or unreadable: {exc}") from exc
    if not stat.S_ISREG(item_stat.st_mode) or item_stat.st_nlink != 1:
        raise ValueError(f"{label} must be a single-link regular file")
    return candidate


def scenario_world_file(scenario_path: Path, *, root: Path = ROOT_DIR) -> tuple[str, str]:
    scenario_file = regular_source_file(scenario_path, root, label="scenario")
    try:
        document = yaml.safe_load(scenario_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"scenario is not readable YAML: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError("scenario YAML root is not a mapping")
    scenario = document.get("scenario")
    mapping = scenario.get("map") if isinstance(scenario, dict) else None
    world_file = mapping.get("world_file") if isinstance(mapping, dict) else None
    return (
        canonical_relative(world_file, label="scenario.scenario.map.world_file", suffix=".sdf"),
        scenario_file.relative_to(Path(os.path.abspath(root))).as_posix(),
    )


def _map_runtime_target(target: Path, *, runtime_root: Path, local_root: Path) -> Path:
    if not target.is_absolute():
        return target
    try:
        relative = target.relative_to(runtime_root)
    except ValueError:
        return target
    return local_root / relative


def resolve_runtime_leaf(
    lexical_path: Path,
    *,
    local_root: Path,
    runtime_root: Path,
    label: str,
) -> Path:
    """Resolve install/build symlink chains, remapping the container checkout on a host."""

    local_root = Path(os.path.abspath(local_root))
    runtime_root = Path(os.path.abspath(runtime_root))
    pending = _lexical_under(lexical_path, local_root, label=label)
    seen: set[str] = set()
    for _ in range(64):
        marker = str(pending)
        if marker in seen:
            raise ValueError(f"{label} has a symbolic-link cycle")
        seen.add(marker)
        try:
            relative = pending.relative_to(local_root)
        except ValueError as exc:
            raise ValueError(f"{label} resolves outside the runtime checkout") from exc
        current = local_root
        parts = list(relative.parts)
        followed = False
        for index, part in enumerate(parts):
            current = current / part
            try:
                item_stat = current.lstat()
            except OSError as exc:
                raise ValueError(f"{label} is missing or unreadable: {exc}") from exc
            if stat.S_ISLNK(item_stat.st_mode):
                target = Path(os.readlink(current))
                if target.is_absolute():
                    target = _map_runtime_target(
                        target, runtime_root=runtime_root, local_root=local_root
                    )
                else:
                    target = current.parent / target
                pending = Path(os.path.abspath(target.joinpath(*parts[index + 1 :])))
                followed = True
                break
        if followed:
            continue
        try:
            item_stat = pending.lstat()
        except OSError as exc:
            raise ValueError(f"{label} is missing or unreadable: {exc}") from exc
        if not stat.S_ISREG(item_stat.st_mode):
            raise ValueError(f"{label} does not resolve to a regular file")
        return pending
    raise ValueError(f"{label} has too many symbolic-link components")


def source_bundle_manifest(world_file: str, *, root: Path = ROOT_DIR) -> dict[str, str]:
    root = Path(os.path.abspath(root))
    worlds_root = root / SOURCE_WORLDS_RELATIVE
    pure = PurePosixPath(world_file)
    if len(pure.parts) == 1:
        candidates = [worlds_root / world_file]
    else:
        bundle_root = worlds_root / pure.parts[0]
        candidates = []
        for directory, names, files in os.walk(bundle_root, followlinks=False):
            directory_path = Path(directory)
            for name in names:
                if (directory_path / name).is_symlink():
                    raise ValueError("source world bundle contains a symbolic-link directory")
            candidates.extend(directory_path / name for name in files)
    if not candidates:
        raise ValueError("source world bundle is empty")
    manifest: dict[str, str] = {}
    for candidate in sorted(candidates):
        source_file = regular_source_file(candidate, root, label="source world bundle file")
        relative = source_file.relative_to(worlds_root).as_posix()
        manifest[relative] = sha256_file(source_file)
    if world_file not in manifest:
        raise ValueError("active source world is absent from its bundle")
    return manifest


def sdf_world_name(path: Path) -> str:
    try:
        document = ET.parse(path)
    except (OSError, ET.ParseError) as exc:
        raise ValueError(f"active world SDF is invalid XML: {exc}") from exc
    worlds = document.getroot().findall("world")
    if len(worlds) != 1:
        raise ValueError("active world SDF must contain exactly one world")
    name = worlds[0].get("name")
    if not isinstance(name, str) or SAFE_WORLD_NAME.fullmatch(name) is None:
        raise ValueError("active world SDF has an invalid world name")
    return name


def installed_bundle_manifest(
    world_file: str,
    expected_files: set[str],
    *,
    installed_worlds: Path,
    local_root: Path,
    runtime_root: Path,
) -> tuple[dict[str, str], dict[str, str]]:
    """Hash every installed bundle leaf and return its resolved runtime paths."""

    pure = PurePosixPath(world_file)
    lexical_files: set[str] = set()
    if len(pure.parts) == 1:
        lexical_files.add(world_file)
    else:
        bundle_root = installed_worlds / pure.parts[0]
        try:
            for directory, names, files in os.walk(bundle_root, followlinks=False):
                directory_path = Path(directory)
                for name in names:
                    if (directory_path / name).is_symlink():
                        raise ValueError("installed world bundle contains a symbolic-link directory")
                for name in files:
                    lexical_files.add(
                        (directory_path / name).relative_to(installed_worlds).as_posix()
                    )
        except OSError as exc:
            raise ValueError(f"installed world bundle is unreadable: {exc}") from exc
    if lexical_files != expected_files:
        missing = sorted(expected_files - lexical_files)
        extra = sorted(lexical_files - expected_files)
        raise ValueError(f"installed world bundle membership differs: missing={missing}, extra={extra}")
    manifest: dict[str, str] = {}
    resolved_paths: dict[str, str] = {}
    local_root = Path(os.path.abspath(local_root))
    for relative in sorted(expected_files):
        resolved = resolve_runtime_leaf(
            installed_worlds / relative,
            local_root=local_root,
            runtime_root=runtime_root,
            label=f"installed world bundle file {relative}",
        )
        manifest[relative] = sha256_file(resolved)
        resolved_paths[relative] = resolved.relative_to(local_root).as_posix()
    return manifest, resolved_paths


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _resource_target(origin: PurePosixPath, uri: str) -> PurePosixPath:
    if not isinstance(uri, str):
        raise ValueError(f"resource URI in {origin} is not text")
    value = uri.strip()
    if value != uri or not value:
        raise ValueError(f"resource URI in {origin} is empty or non-canonical")
    if value.startswith("model://"):
        tail = canonical_relative(value.removeprefix("model://"), label="model URI")
        parts = PurePosixPath(tail).parts
        if SAFE_MODEL_NAME.fullmatch(parts[0]) is None:
            raise ValueError(f"resource URI in {origin} has an invalid model name")
        if len(parts) == 1:
            return PurePosixPath("models") / parts[0] / "model.config"
        return PurePosixPath("models").joinpath(*parts)
    if value.startswith("package://"):
        tail = canonical_relative(
            value.removeprefix("package://"), label="package resource URI"
        )
        parts = PurePosixPath(tail).parts
        if len(parts) < 2 or parts[0] != "multiagent_simulation":
            raise ValueError(
                f"resource URI in {origin} names an external or incomplete package"
            )
        return PurePosixPath(*parts[1:])
    if "://" in value or value.startswith("/"):
        raise ValueError(f"resource URI in {origin} uses an unsupported scheme or absolute path")
    relative = canonical_relative(value, label=f"relative resource URI in {origin}")
    return origin.parent / PurePosixPath(relative)


def _validate_resource_target(target: PurePosixPath, *, origin: PurePosixPath) -> str:
    value = target.as_posix()
    canonical_relative(value, label=f"resolved resource from {origin}")
    if target.parts[0] not in {"models", "worlds"}:
        raise ValueError(f"resource from {origin} resolves outside models/worlds")
    return value


def _xml_resource_dependencies(
    logical_path: str, physical_path: Path
) -> list[tuple[str, str]]:
    """Return every local file dependency referenced by one scene resource."""

    origin = PurePosixPath(logical_path)
    suffix = origin.suffix.lower()
    dependencies: list[tuple[str, str]] = []
    if suffix in {".sdf", ".config"}:
        try:
            document = ET.parse(physical_path)
        except (OSError, ET.ParseError) as exc:
            raise ValueError(f"scene resource {logical_path} is invalid XML: {exc}") from exc
        root = document.getroot()
        if suffix == ".config":
            sdf_entries = [
                element
                for element in root
                if _xml_local_name(element.tag) == "sdf"
                and isinstance(element.text, str)
                and element.text.strip()
            ]
            if len(sdf_entries) != 1:
                raise ValueError(
                    f"model config {logical_path} must select exactly one SDF file"
                )
            uri_values = [sdf_entries[0].text.strip()]
        else:
            uri_values = [
                element.text.strip()
                for element in root.iter()
                if _xml_local_name(element.tag) == "uri"
                and isinstance(element.text, str)
                and element.text.strip()
            ]
        for uri in uri_values:
            target = _resource_target(origin, uri)
            dependencies.append((uri, _validate_resource_target(target, origin=origin)))
    elif suffix == ".dae":
        # COLLADA uses many internal <init_from> identifiers.  Only values under
        # <library_images>/<image> are filesystem resources.
        try:
            for _event, element in ET.iterparse(physical_path, events=("end",)):
                if _xml_local_name(element.tag) != "image":
                    continue
                for child in element.iter():
                    if (
                        _xml_local_name(child.tag) == "init_from"
                        and isinstance(child.text, str)
                        and child.text.strip()
                    ):
                        uri = child.text.strip()
                        if uri.startswith("#"):
                            continue
                        target = _resource_target(origin, uri)
                        dependencies.append(
                            (uri, _validate_resource_target(target, origin=origin))
                        )
                element.clear()
        except (OSError, ET.ParseError) as exc:
            raise ValueError(f"COLLADA resource {logical_path} is invalid XML: {exc}") from exc
    return sorted(set(dependencies))


def _transitive_manifest(
    roots: list[str],
    *,
    resolve_leaf: Any,
) -> tuple[dict[str, str], list[dict[str, str]], dict[str, str]]:
    pending = sorted(set(roots))
    manifest: dict[str, str] = {}
    edges: list[dict[str, str]] = []
    resolved_paths: dict[str, str] = {}
    while pending:
        logical_path = pending.pop(0)
        if logical_path in manifest:
            continue
        if len(manifest) >= MAX_TRANSITIVE_RESOURCES:
            raise ValueError("scene resource closure exceeds its fail-closed size bound")
        physical_path, resolved_relative = resolve_leaf(logical_path)
        manifest[logical_path] = sha256_file(physical_path)
        resolved_paths[logical_path] = resolved_relative
        for uri, target in _xml_resource_dependencies(logical_path, physical_path):
            edges.append({"from": logical_path, "uri": uri, "to": target})
            if target not in manifest and target not in pending:
                pending.append(target)
        pending.sort()
    return dict(sorted(manifest.items())), sorted(
        edges, key=lambda item: (item["from"], item["uri"], item["to"])
    ), dict(sorted(resolved_paths.items()))


def source_scene_resource_manifest(
    world_file: str,
    robot_model: str,
    *,
    root: Path = ROOT_DIR,
) -> tuple[dict[str, str], list[dict[str, str]]]:
    root = Path(os.path.abspath(root))
    robot_model = canonical_robot_model(robot_model)
    roots = [
        f"worlds/{world_file}",
        f"models/{robot_model}/model.sdf",
    ]

    def resolve(logical_path: str) -> tuple[Path, str]:
        canonical_relative(logical_path, label="source scene resource")
        physical = regular_source_file(
            root / SOURCE_PACKAGE_RELATIVE / logical_path,
            root,
            label=f"source scene resource {logical_path}",
        )
        return physical, (SOURCE_PACKAGE_RELATIVE / logical_path).as_posix()

    files, edges, _resolved = _transitive_manifest(roots, resolve_leaf=resolve)
    return files, edges


def installed_scene_resource_manifest(
    world_file: str,
    robot_model: str,
    *,
    installed_package_share: Path,
    local_root: Path,
    runtime_root: Path,
) -> tuple[dict[str, str], list[dict[str, str]], dict[str, str]]:
    robot_model = canonical_robot_model(robot_model)
    local_root = Path(os.path.abspath(local_root))
    installed_share = _lexical_under(
        installed_package_share, local_root, label="installed package share"
    )
    roots = [
        f"worlds/{world_file}",
        f"models/{robot_model}/model.sdf",
    ]

    def resolve(logical_path: str) -> tuple[Path, str]:
        canonical_relative(logical_path, label="installed scene resource")
        physical = resolve_runtime_leaf(
            installed_share / logical_path,
            local_root=local_root,
            runtime_root=runtime_root,
            label=f"installed scene resource {logical_path}",
        )
        return physical, physical.relative_to(local_root).as_posix()

    return _transitive_manifest(roots, resolve_leaf=resolve)


def _scenario_robots(scenario_path: Path, *, root: Path) -> list[dict[str, Any]]:
    scenario_file = regular_source_file(scenario_path, root, label="scenario")
    try:
        document = yaml.safe_load(scenario_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"scenario is not readable YAML: {exc}") from exc
    robots = document.get("robots") if isinstance(document, dict) else None
    if not isinstance(robots, list) or len(robots) != 5:
        raise ValueError("M1 scenario must contain exactly five robots")
    expected_names = [f"uav{index}" for index in range(1, 6)]
    normalized: list[dict[str, Any]] = []
    for index, robot in enumerate(robots):
        if not isinstance(robot, dict):
            raise ValueError(f"scenario robot[{index}] is not a mapping")
        name = robot.get("name")
        instance = robot.get("instance")
        if name != expected_names[index] or instance != index:
            raise ValueError("M1 scenario robot names/instances are not exactly uav1..uav5/0..4")
        normalized.append({"name": name, "instance": instance})
    return normalized


def resolved_robot_descriptions(
    scenario_path: Path,
    robot_model: str,
    *,
    template_path: Path,
    root: Path,
) -> list[dict[str, Any]]:
    robot_model = canonical_robot_model(robot_model)
    try:
        template = template_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"installed robot SDF template is unreadable: {exc}") from exc
    if template.count(ROBOT_DESCRIPTION_PORT_TOKEN) != 1:
        raise ValueError("robot SDF template must contain exactly one canonical FDM port token")
    robots = _scenario_robots(scenario_path, root=root)
    descriptions: list[dict[str, Any]] = []
    for index, robot in enumerate(robots):
        port = 9002 + 10 * index
        description = template.replace(
            ROBOT_DESCRIPTION_PORT_TOKEN,
            f"<fdm_port_in>{port}</fdm_port_in>",
        )
        try:
            document = ET.fromstring(description)
        except ET.ParseError as exc:
            raise ValueError(f"resolved robot description for {robot['name']} is invalid: {exc}") from exc
        plugins = [
            element
            for element in document.iter()
            if _xml_local_name(element.tag) == "plugin"
            and (
                element.attrib.get("name") == "ArduPilotPlugin"
                or element.attrib.get("filename") == "ArduPilotPlugin"
            )
        ]
        if len(plugins) != 1:
            raise ValueError(
                f"resolved robot description for {robot['name']} must contain one ArduPilotPlugin"
            )
        addresses = [
            element.text.strip()
            for element in plugins[0]
            if _xml_local_name(element.tag) == "fdm_addr"
            and isinstance(element.text, str)
        ]
        ports = [
            element.text.strip()
            for element in plugins[0]
            if _xml_local_name(element.tag) == "fdm_port_in"
            and isinstance(element.text, str)
        ]
        if addresses != ["127.0.0.1"] or ports != [str(port)]:
            raise ValueError(
                f"resolved robot description for {robot['name']} has unexpected FDM endpoint"
            )
        descriptions.append(
            {
                "name": robot["name"],
                "instance": robot["instance"],
                "fdm_addr": "127.0.0.1",
                "fdm_port_in": port,
                "robot_description_sha256": hashlib.sha256(
                    description.encode("utf-8")
                ).hexdigest(),
            }
        )
    return descriptions


def build_scene_record(
    *,
    run_dir: Path,
    scenario_path: Path,
    robot_model: str,
    runtime_id: str,
    installed_package_share: Path,
    root: Path = ROOT_DIR,
) -> dict[str, Any]:
    root = Path(os.path.abspath(root))
    run_dir = _lexical_under(run_dir, root, label="run directory")
    if run_dir.name == "" or run_dir.parent != root / "runs":
        raise ValueError("run directory must be a direct child of the checkout runs directory")
    if not isinstance(runtime_id, str) or len(runtime_id) < 8:
        raise ValueError("runtime_id is missing or too short")
    provenance_path = run_dir / "metrics/provenance.json"
    try:
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"run provenance is missing or invalid: {exc}") from exc
    source_hash = provenance.get("source_hash") if isinstance(provenance, dict) else None
    if not isinstance(source_hash, str) or SHA256.fullmatch(source_hash) is None:
        raise ValueError("run provenance source_hash is not SHA-256")
    config_hashes = (
        provenance.get("config_hashes")
        if isinstance(provenance.get("config_hashes"), dict)
        else {}
    )
    plan_hash = config_hashes.get(M1_PLAN_PATH)
    plan_path = regular_source_file(root / M1_PLAN_PATH, root, label="v3 contract")
    if not isinstance(plan_hash, str) or plan_hash != sha256_file(plan_path):
        raise ValueError("generic provenance does not bind the current v3 contract hash")

    robot_model = canonical_robot_model(robot_model)
    world_file, scenario_relative = scenario_world_file(scenario_path, root=root)
    scenario_file = root / scenario_relative
    installed_share = _lexical_under(
        installed_package_share, root, label="installed package share"
    )
    expected_share = Path("install/multiagent_simulation/share/multiagent_simulation")
    if installed_share.relative_to(root) != expected_share:
        raise ValueError("installed package share is not the canonical project install path")
    installed_worlds = installed_share / "worlds"
    source_manifest = source_bundle_manifest(world_file, root=root)
    installed_manifest, resolved_paths = installed_bundle_manifest(
        world_file,
        set(source_manifest),
        installed_worlds=installed_worlds,
        local_root=root,
        runtime_root=root,
    )
    if installed_manifest != source_manifest:
        raise ValueError("installed world bundle content differs from canonical source")

    source_resources, source_resource_edges = source_scene_resource_manifest(
        world_file, robot_model, root=root
    )
    (
        installed_resources,
        installed_resource_edges,
        installed_resource_paths,
    ) = installed_scene_resource_manifest(
        world_file,
        robot_model,
        installed_package_share=installed_share,
        local_root=root,
        runtime_root=root,
    )
    if installed_resources != source_resources or installed_resource_edges != source_resource_edges:
        raise ValueError("installed transitive scene resources differ from canonical source")

    provenance_source_manifest = (
        provenance.get("source_manifest")
        if isinstance(provenance.get("source_manifest"), dict)
        else {}
    )
    bound_source_files: dict[str, str] = {
        scenario_relative: sha256_file(scenario_file),
        LAUNCH_SOURCE_RELATIVE.as_posix(): sha256_file(
            regular_source_file(
                root / LAUNCH_SOURCE_RELATIVE,
                root,
                label="multiagent launch source",
            )
        ),
    }
    bound_source_files.update(
        {
            (SOURCE_PACKAGE_RELATIVE / logical).as_posix(): file_hash
            for logical, file_hash in source_resources.items()
        }
    )
    bound_source_files.update(
        {
            (SOURCE_WORLDS_RELATIVE / logical).as_posix(): file_hash
            for logical, file_hash in source_manifest.items()
        }
    )
    bound_source_files = dict(sorted(bound_source_files.items()))
    for relative, expected_hash in bound_source_files.items():
        if provenance_source_manifest.get(relative) != expected_hash:
            raise ValueError(
                f"generic provenance source manifest does not bind scene input {relative}"
            )

    template_logical = f"models/{robot_model}/model.sdf"
    template_path = resolve_runtime_leaf(
        installed_share / template_logical,
        local_root=root,
        runtime_root=root,
        label="installed robot SDF template",
    )
    robot_descriptions = resolved_robot_descriptions(
        scenario_path,
        robot_model,
        template_path=template_path,
        root=root,
    )

    world_name = sdf_world_name(root / SOURCE_WORLDS_RELATIVE / world_file)
    dependency_versions = (
        provenance.get("dependency_versions")
        if isinstance(provenance.get("dependency_versions"), dict)
        else {}
    )
    gazebo_version = dependency_versions.get("gazebo")
    if not isinstance(gazebo_version, str) or not gazebo_version:
        raise ValueError("generic provenance does not record the Gazebo version")

    source_world_relative = (SOURCE_WORLDS_RELATIVE / world_file).as_posix()
    installed_world_relative = (
        PurePosixPath(expected_share.as_posix()) / "worlds" / world_file
    ).as_posix()
    bundle_root = PurePosixPath(world_file).parts[0] if "/" in world_file else world_file
    return {
        "schema_version": 2,
        "contract": M1_CONTRACT_ID,
        "plan_version": 3,
        "contract_path": M1_PLAN_PATH,
        "contract_sha256": plan_hash,
        "run_id": run_dir.name,
        "runtime_id": runtime_id,
        "source_hash": source_hash,
        "component_only": True,
        "p0_eligible": False,
        "robot_model": robot_model,
        "scenario": {
            "path": scenario_relative,
            "sha256": sha256_file(scenario_file),
            "world_file": world_file,
        },
        "gazebo": {
            "version": gazebo_version,
            "world_name": world_name,
        },
        "runtime_checkout_path": str(root),
        "source_manifest_binding": {
            "files": bound_source_files,
            "sha256": manifest_sha256(bound_source_files),
        },
        "resources": {
            "roots": sorted(
                [
                    f"worlds/{world_file}",
                    template_logical,
                ]
            ),
            "source_package_root": SOURCE_PACKAGE_RELATIVE.as_posix(),
            "installed_package_share_path": expected_share.as_posix(),
            "source_files": source_resources,
            "installed_files": installed_resources,
            "uri_edges": source_resource_edges,
            "source_sha256": manifest_sha256(source_resources),
            "installed_sha256": manifest_sha256(installed_resources),
            "resolved_installed_paths": installed_resource_paths,
        },
        "robot_descriptions": {
            "template_path": template_logical,
            "template_sha256": installed_resources[template_logical],
            "substitution_token": ROBOT_DESCRIPTION_PORT_TOKEN,
            "port_formula": "9002 + 10 * instance",
            "instances": robot_descriptions,
        },
        "source": {
            "worlds_root": SOURCE_WORLDS_RELATIVE.as_posix(),
            "active_world_path": source_world_relative,
            "active_world_sha256": source_manifest[world_file],
            "bundle_root": bundle_root,
            "bundle_files": source_manifest,
            "bundle_sha256": manifest_sha256(source_manifest),
        },
        "installed": {
            "package_share_path": expected_share.as_posix(),
            "worlds_root": (PurePosixPath(expected_share.as_posix()) / "worlds").as_posix(),
            "active_world_path": installed_world_relative,
            "runtime_active_world_path": str(root / installed_world_relative),
            "resolved_active_world_path": resolved_paths[world_file],
            "active_world_sha256": installed_manifest[world_file],
            "bundle_root": bundle_root,
            "bundle_files": installed_manifest,
            "bundle_sha256": manifest_sha256(installed_manifest),
            "resolved_bundle_paths": resolved_paths,
        },
    }


def write_scene_record(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(record, indent=2, sort_keys=True) + "\n"
    with path.open("x", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    path.chmod(0o444)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--scenario", type=Path, required=True)
    parser.add_argument("--robot-model", required=True)
    parser.add_argument("--runtime-id", required=True)
    parser.add_argument("--installed-package-share", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    installed_share = args.installed_package_share
    if installed_share is None:
        try:
            from ament_index_python.packages import get_package_share_directory

            installed_share = Path(get_package_share_directory("multiagent_simulation"))
        except Exception as exc:
            print(f"FAIL cannot locate installed multiagent_simulation package: {exc}", file=os.sys.stderr)
            return 2
    try:
        record = build_scene_record(
            run_dir=args.run_dir,
            scenario_path=args.scenario,
            robot_model=args.robot_model,
            runtime_id=args.runtime_id,
            installed_package_share=installed_share,
        )
        write_scene_record(args.run_dir / M1_SCENE_RECORD, record)
    except Exception as exc:
        print(f"FAIL M1 scene provenance: {exc}", file=os.sys.stderr)
        return 1
    print(record["scenario"]["world_file"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
