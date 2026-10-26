#!/usr/bin/env python3
"""Copy actual run inputs and upstream parameter exports before they can change.

This helper does not query or invent simulator internals. The runner supplies
the files it actually used and exports obtained from upstream interfaces. The
manifest distinguishes configured inputs from observed effective parameters.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any


EXPECTED = ("world", "scenario", "radio", "ns3_attributes", "ros_topics", "ros_qos",
            *(f"ardupilot_uav{number}" for number in range(1, 6)))


def named_file(value: str) -> tuple[str, Path]:
    label, separator, raw_path = value.partition("=")
    if not separator or not re.fullmatch(r"[A-Za-z0-9_.-]+", label) or not raw_path:
        raise argparse.ArgumentTypeError("expected a unique label=path, with a simple filename label")
    return label, Path(raw_path)


def source_state(repo_root: Path) -> dict[str, Any]:
    def git(*args: str) -> str:
        return subprocess.check_output(["git", "-C", str(repo_root), *args], text=True,
                                       stderr=subprocess.DEVNULL, timeout=5).rstrip("\n")
    try:
        return {"repo_root": str(repo_root.resolve()), "head": git("rev-parse", "HEAD"),
                "dirty_paths": git("status", "--porcelain=v1", "--untracked-files=normal").splitlines(),
                "scope": "revision/status only; uncommitted source contents are not captured"}
    except (OSError, subprocess.SubprocessError):
        return {"repo_root": str(repo_root.resolve()), "head": None,
                "status": "not_recorded", "reason": "git metadata unavailable"}


def capture_snapshot(
    run_dir: Path, *, stage: str, repo_root: Path,
    configs: list[tuple[str, Path]], observed: list[tuple[str, Path]],
    not_enabled: list[str] = (), image_id: str | None = None,
) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", stage):
        raise ValueError("stage must be a simple identifier")
    run_dir = run_dir.resolve()
    if not run_dir.is_dir():
        raise ValueError("run directory must already exist")
    # These files are authored by the actual run, unlike YAML references to a
    # mutable checkout. Include them verbatim, without claiming full coverage.
    inputs = [("environment", run_dir / "environment.txt", "run_metadata"),
              ("command", run_dir / "command.txt", "run_metadata"),
              ("native_stats", run_dir / "metrics/native_radio_stats.json", "partial_native_export"),
              ("native_sources", run_dir / "logs/native_sources.json", "configured_input")]
    inputs += [(label, path, "configured_input") for label, path in configs]
    inputs += [(label, path, "effective_parameter_export") for label, path in observed]
    labels = [item[0] for item in inputs]
    if any(not re.fullmatch(r"[A-Za-z0-9_.-]+", label) for label in labels):
        raise ValueError("invalid input label")
    if len(set(labels)) != len(labels):
        raise ValueError("input labels must be unique")
    if set(labels) & set(not_enabled):
        raise ValueError("a provided input cannot also be declared not enabled")
    parent = run_dir / "configuration"
    destination = parent / stage
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"snapshot already exists; choose another stage: {destination}")
    parent.mkdir(exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{stage}-", dir=parent))
    try:
        records = []
        for label, raw_path, kind in inputs:
            source = raw_path.resolve()
            record = {"label": label, "source_path": str(source), "kind": kind,
                      "status": "not_recorded", "copied_path": None}
            if source.is_file():
                name = f"inputs/{label}/value{source.suffix}"
                try:
                    (temporary / name).parent.mkdir(parents=True)
                    shutil.copyfile(source, temporary / name)
                except OSError as error:
                    record["reason"] = type(error).__name__
                else:
                    record.update(status="observed", copied_path=name,
                                  bytes=(temporary / name).stat().st_size)
            else:
                record["reason"] = "file_not_found"
            records.append(record)
        by_label = {row["label"]: row for row in records}
        coverage = {}
        for label in EXPECTED:
            row = by_label.get(label)
            coverage[label] = (
                {"status": "not_enabled", "scope": "explicit runner declaration"}
                if label in not_enabled else
                {"status": "not_recorded", "scope": "no upstream export supplied"}
                if row is None or row["status"] != "observed" else
                {"status": "observed", "scope": row["kind"], "file": row["copied_path"]}
            )
        for label in not_enabled:
            coverage.setdefault(label, {"status": "not_enabled", "scope": "explicit runner declaration"})
        memory = None
        try:
            match = re.search(r"^MemTotal:\s+(\d+) kB", Path("/proc/meminfo").read_text(), re.M)
            memory = int(match[1]) * 1024 if match else None
        except OSError:
            pass
        manifest = {
            "stage": stage, "run_dir": str(run_dir),
            "captured_host_monotonic_ns": time.monotonic_ns(),
            "captured_unix_ns": time.time_ns(),
            "source": source_state(repo_root), "container_image_id": image_id,
            "machine": {"platform": platform.platform(), "cpu_count": os.cpu_count(),
                        "memory_total_bytes": memory, "python": platform.python_version()},
            "files": records, "coverage": coverage,
            "limits": [
                "Configured-input copies record bytes used by the runner, not runtime read-back.",
                "Only explicit effective_parameter_export inputs contain upstream parameter read-back.",
                "Native stats can expose selected settings; they do not export all ns-3 attributes.",
                "Snapshot capture time is recorded; callers must capture before inputs are modified.",
                "This snapshot does not infer flight, radio delivery or experiment success.",
            ],
        }
        (temporary / "manifest.json").write_text(
            json.dumps(manifest, allow_nan=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.rename(destination)
    except BaseException:
        shutil.rmtree(temporary)
        raise
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--stage", default="initial")
    parser.add_argument("--config", type=named_file, action="append", default=[],
                        help="Actual used input, e.g. world=/run/final.sdf; copied verbatim")
    parser.add_argument("--observed-params", type=named_file, action="append", default=[],
                        help="Upstream read-back, e.g. ardupilot_uav1=/run/uav1.parm")
    parser.add_argument("--not-enabled", action="append", default=[],
                        help="Explicitly inapplicable component/export, e.g. sionna")
    parser.add_argument("--image-id", help="Actual immutable image ID supplied by the launcher")
    args = parser.parse_args()
    try:
        result = capture_snapshot(args.run_dir, stage=args.stage, repo_root=args.repo_root,
                                  configs=args.config, observed=args.observed_params,
                                  not_enabled=args.not_enabled, image_id=args.image_id)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps({"snapshot": str(args.run_dir.resolve() / "configuration" / args.stage),
                      "coverage": result["coverage"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
