#!/usr/bin/env python3
"""Seal the complete P0 raw-evidence set without including validator outputs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml


ROOT_DIR = Path(__file__).resolve().parents[2]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def checked_regular_file(
    run_dir: Path, relative: object
) -> tuple[Path, os.stat_result]:
    if not isinstance(relative, str) or not relative or "\x00" in relative:
        raise ValueError(f"raw evidence path is missing or invalid: {relative!r}")
    relative_path = Path(relative)
    if relative_path.is_absolute() or any(
        part in ("", ".", "..") for part in relative_path.parts
    ):
        raise ValueError(f"raw evidence path is not canonical and relative: {relative!r}")
    current = run_dir
    for part in relative_path.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(
                f"raw evidence path has a symbolic-link component: {relative}"
            )
    resolved = current.resolve(strict=True)
    resolved.relative_to(run_dir)
    file_stat = current.stat(follow_symlinks=False)
    if not stat.S_ISREG(file_stat.st_mode):
        raise ValueError(f"raw evidence is not a regular file: {relative}")
    if file_stat.st_nlink != 1:
        raise ValueError(
            f"raw evidence has {file_stat.st_nlink} hard links: {relative}"
        )
    return current, file_stat


def checked_output_path(run_dir: Path, relative: object) -> Path:
    if not isinstance(relative, str) or not relative or "\x00" in relative:
        raise ValueError(f"manifest path is missing or invalid: {relative!r}")
    relative_path = Path(relative)
    if relative_path.is_absolute() or any(
        part in ("", ".", "..") for part in relative_path.parts
    ):
        raise ValueError(f"manifest path is not canonical and relative: {relative!r}")
    current = run_dir
    for part in relative_path.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise ValueError(
                f"manifest path has a symbolic-link parent component: {relative}"
            )
    output = run_dir / relative_path
    output.resolve(strict=False).relative_to(run_dir)
    return output


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument(
        "--matrix",
        type=Path,
        default=ROOT_DIR / "network/config/validation_matrix.yaml",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    run_dir = args.run_dir.resolve()
    try:
        authoritative_matrix = (ROOT_DIR / "network/config/validation_matrix.yaml").resolve()
        matrix_path = args.matrix.resolve()
        if matrix_path.read_bytes() != authoritative_matrix.read_bytes():
            raise ValueError(
                "acceptance matrix is not byte-identical to network/config/validation_matrix.yaml"
            )
        matrix = yaml.safe_load(matrix_path.read_text(encoding="utf-8"))
        raw_files = list(matrix["run_outputs"]["raw_runtime_required"])
        validator_outputs = set(matrix["run_outputs"]["validator_outputs"])
        manifest_relative = str(matrix["run_outputs"]["raw_seal"])
        output = checked_output_path(run_dir, manifest_relative)
        if output.exists():
            raise FileExistsError(f"evidence manifest already exists: {output}")
        if len(raw_files) != len(set(raw_files)):
            raise ValueError("raw evidence list contains duplicate paths")
        overlap = validator_outputs.intersection(raw_files)
        if overlap:
            raise ValueError(f"validator outputs appear in raw evidence list: {sorted(overlap)}")
        files = {}
        raw_paths: dict[str, Path] = {}
        raw_modes: dict[Path, int] = {}
        inode_owner: dict[tuple[int, int], str] = {}
        for relative in raw_files:
            path, file_stat = checked_regular_file(run_dir, relative)
            inode = (file_stat.st_dev, file_stat.st_ino)
            previous = inode_owner.get(inode)
            if previous is not None:
                raise ValueError(
                    f"raw evidence paths share one inode: {previous}, {relative}"
                )
            inode_owner[inode] = relative
            if file_stat.st_size == 0:
                raise FileNotFoundError(
                    f"required raw evidence is empty: {relative}"
                )
            raw_paths[relative] = path
            raw_modes[path] = stat.S_IMODE(file_stat.st_mode)
            files[relative] = {
                "sha256": sha256_file(path),
                "size_bytes": file_stat.st_size,
            }
        provenance = load_json(raw_paths["metrics/provenance.json"])
        joint_runtime = load_json(raw_paths["metrics/joint_runtime.json"])
        runtime_id = joint_runtime.get("runtime_id")
        source_hash = provenance.get("source_hash")
        if not isinstance(runtime_id, str) or len(runtime_id) < 8:
            raise ValueError("joint_runtime.runtime_id is missing")
        if not isinstance(source_hash, str) or len(source_hash) != 64:
            raise ValueError("provenance.source_hash is missing")
        manifest = {
            "schema_version": 2,
            "run_id": run_dir.name,
            "runtime_id": runtime_id,
            "source_hash": source_hash,
            "sealed_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "matrix_sha256": sha256_file(matrix_path),
            "files": files,
        }
    except Exception as exc:
        print(f"FAIL could not seal evidence: {exc}", file=sys.stderr)
        return 2
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    linked = False
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.parent.is_symlink():
            raise ValueError(f"manifest parent may not be a symbolic link: {output.parent}")
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, output)
        linked = True
        for path, original_mode in raw_modes.items():
            os.chmod(path, original_mode & ~0o222, follow_symlinks=False)
        os.chmod(output, output.stat(follow_symlinks=False).st_mode & ~0o222)
    except Exception as exc:
        rollback_failures: list[str] = []
        for path, original_mode in raw_modes.items():
            try:
                os.chmod(path, original_mode, follow_symlinks=False)
            except OSError as rollback_exc:
                rollback_failures.append(f"{path}: {rollback_exc}")
        if linked:
            try:
                os.chmod(output, 0o600, follow_symlinks=False)
                output.unlink()
            except OSError as rollback_exc:
                rollback_failures.append(f"{output}: {rollback_exc}")
        suffix = (
            "; rollback failures: " + "; ".join(rollback_failures)
            if rollback_failures
            else ""
        )
        print(f"FAIL could not publish evidence seal: {exc}{suffix}", file=sys.stderr)
        return 2
    finally:
        temporary.unlink(missing_ok=True)
    print(f"Evidence manifest: {output}")
    print(f"Raw files sealed: {len(files)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
