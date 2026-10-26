#!/usr/bin/env python3
"""Copy actual run inputs and upstream parameter exports before they can change.

This helper does not query or invent simulator internals. The runner supplies
the files it actually used and exports obtained from upstream interfaces. The
manifest distinguishes configured inputs from observed effective parameters.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import json
import math
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


def export_dataflash_parameters(run_dir: Path, output_dir: Path) -> dict[str, Any]:
    """Decode observed PARM records with upstream pymavlink, outside hot loops."""
    from pymavlink import DFReader

    run_dir, output_dir = run_dir.resolve(), output_dir.resolve()
    if not run_dir.is_dir():
        raise ValueError("raw run directory does not exist")
    output_dir.mkdir(parents=True, exist_ok=True)
    fields = ["uav", "source_log", "parm_record_index", "name", "value", "default_value",
              "autopilot_time_us", "autopilot_time_s", "clock_domain", "value_status"]
    result: dict[str, Any] = {
        "decoder": "upstream pymavlink.DFReader.DFReader_binary",
        "raw_run_dir": str(run_dir), "uavs": {},
        "limits": ["Only PARM records physically present in DataFlash are exported.",
                   "Completeness is not verified against live PARAM_COUNT; missing parameters are not invented.",
                   "DataFlash boot time is separate from Gazebo and host clocks; no implicit alignment.",
                   "Last observed values can precede shutdown when logging stops early; inspect source log coverage."],
    }

    def number(value: Any) -> float | None:
        try:
            value = float(value)
            return value if math.isfinite(value) else None
        except (TypeError, ValueError):
            return None

    for instance in range(5):
        uav = f"uav{instance + 1}"
        paths = sorted((run_dir / "sitl" / str(instance) / "logs").glob("*.BIN"))
        initial, latest = {}, {}
        logs = []
        changes = 0
        total = 0
        csv_name = f"{uav}_parameters.csv"
        with (output_dir / csv_name).open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for path in paths:
                coverage: dict[str, Any] = {"source_log": str(path), "bytes": path.stat().st_size,
                                            "parm_records": 0, "status": "not_recorded"}
                names = set()
                reader = None
                warning_name = f"{uav}_{path.stem}_decoder.log"
                with (output_dir / warning_name).open("w", encoding="utf-8") as warnings:
                    try:
                        # Upstream decode warnings remain inspectable; stdout
                        # is not flooded with parser diagnostics in a campaign.
                        with contextlib.redirect_stdout(warnings), contextlib.redirect_stderr(warnings):
                            reader = DFReader.DFReader_binary(str(path))
                            while True:
                                message = reader.recv_match(type="PARM")
                                if message is None:
                                    break
                                name = str(message.Name).rstrip("\0")
                                value = number(message.Value)
                                boot_us = number(getattr(message, "TimeUS", None))
                                if boot_us is None:
                                    boot_ms = number(getattr(message, "TimeMS", None))
                                    boot_us = boot_ms * 1000 if boot_ms is not None else None
                                coverage["parm_records"] += 1
                                record = {"uav": uav, "source_log": str(path),
                                          "parm_record_index": coverage["parm_records"],
                                          "name": name, "value": value,
                                          "default_value": number(getattr(message, "Default", None)),
                                          "autopilot_time_us": boot_us,
                                          "autopilot_time_s": boot_us / 1e6 if boot_us is not None else None,
                                          "clock_domain": "ardupilot_boot",
                                          "value_status": "observed" if value is not None else "invalid_nonfinite_value"}
                                writer.writerow(record)
                                total += 1
                                names.add(name)
                                initial.setdefault(name, record)
                                if name in latest and latest[name]["value"] != value:
                                    changes += 1
                                latest[name] = record
                        coverage["status"] = "observed" if coverage["parm_records"] else "not_recorded"
                    except Exception as error:
                        coverage["status"] = "partial_decode_error"
                        coverage["decode_error"] = type(error).__name__ + ": " + str(error)
                    finally:
                        if reader is not None:
                            reader.close()
                coverage.update(unique_parameters=len(names), decoder_log=warning_name,
                                decoder_log_bytes=(output_dir / warning_name).stat().st_size)
                logs.append(coverage)
        values_name = f"{uav}_observed_values.json"
        (output_dir / values_name).write_text(json.dumps({"first_observed": initial, "last_observed": latest},
            allow_nan=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        status = "partial_decode_error" if any(row["status"] == "partial_decode_error" for row in logs) else "observed" if total else "not_recorded"
        result["uavs"][uav] = {"status": status,
                               "parm_records": total, "unique_parameters": len(latest),
                               "observed_value_changes": changes, "csv": csv_name,
                               "observed_values_json": values_name, "logs": logs,
                               "completeness": "observed_only_not_verified_against_live_parameter_count"}
    (output_dir / "dataflash_parameter_summary.json").write_text(
        json.dumps(result, allow_nan=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


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
        launcher_head = os.environ.get("BAS_SOURCE_HEAD", "")
        if re.fullmatch(r"[0-9a-fA-F]{40,64}", launcher_head):
            try:
                dirty_count = int(os.environ["BAS_SOURCE_DIRTY"])
            except (KeyError, ValueError):
                dirty_count = None
            return {"repo_root": str(repo_root.resolve()), "head": launcher_head,
                    "dirty_count": dirty_count, "status": "observed_launcher_metadata",
                    "source": "host launcher env BAS_SOURCE_HEAD/BAS_SOURCE_DIRTY",
                    "full_git_snapshot_available": False,
                    "scope": "launcher-observed revision/count only; dirty contents require a host patch"}
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
            "source": source_state(repo_root),
            "container_image_id": image_id or os.environ.get("BAS_CONTAINER_IMAGE_ID"),
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
    parser.add_argument("--dataflash-params", action="store_true",
                        help="Decode actual PARM records from sitl/0..4/logs/*.BIN into the snapshot inputs")
    parser.add_argument("--export-dataflash-only", action="store_true",
                        help="Only export DataFlash parameters; allows read-only historical inputs")
    parser.add_argument("--output-dir", type=Path,
                        help="Output directory required for --export-dataflash-only")
    args = parser.parse_args()
    try:
        if args.export_dataflash_only:
            if args.output_dir is None:
                parser.error("--export-dataflash-only requires --output-dir")
            result = export_dataflash_parameters(args.run_dir, args.output_dir)
            print(json.dumps({uav: {key: row[key] for key in ("status", "parm_records", "unique_parameters")}
                              for uav, row in result["uavs"].items()}, sort_keys=True))
            return 0
        if args.output_dir is not None:
            parser.error("--output-dir requires --export-dataflash-only")
        if args.dataflash_params:
            dataflash_dir = args.run_dir / "metrics/dataflash_parameters"
            parameter_export = export_dataflash_parameters(args.run_dir, dataflash_dir)
            args.observed_params += [("ardupilot_" + uav, dataflash_dir / row["csv"])
                                     for uav, row in parameter_export["uavs"].items() if row["parm_records"]]
            args.config += [("ardupilot_dataflash_index", dataflash_dir / "dataflash_parameter_summary.json")]
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
