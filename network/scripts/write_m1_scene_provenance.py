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
M1_SCENE_RECORD = "metrics/m1_scene_provenance.json"
M1_CONTRACT_ID = "ams.m1.health/v3"
M1_PLAN_PATH = "doc/network_radio_integration_plan_v3.md"
SHA256 = re.compile(r"[0-9a-f]{64}")
SAFE_WORLD_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}")


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


def build_scene_record(
    *,
    run_dir: Path,
    scenario_path: Path,
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
        "schema_version": 1,
        "contract": M1_CONTRACT_ID,
        "plan_version": 3,
        "contract_path": M1_PLAN_PATH,
        "contract_sha256": plan_hash,
        "run_id": run_dir.name,
        "runtime_id": runtime_id,
        "source_hash": source_hash,
        "component_only": True,
        "p0_eligible": False,
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
