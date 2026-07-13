#!/usr/bin/env python3
"""Fail-closed validator for the component-only five-UAV M1 milestone."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any, Callable


ROOT_DIR = Path(__file__).resolve().parents[2]
SAFE_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
SCENE_RECORD = "metrics/m1_scene_provenance.json"
RAW_HEALTH_LOG = "logs/five_uav_health_events.jsonl"
INSTALLED_SHARE = "install/multiagent_simulation/share/multiagent_simulation"
SOURCE_WORLDS = "src/multiagent_simulation/worlds"
M1_CONTRACT_ID = "ams.m1.health/v3"
M1_PLAN_PATH = "doc/network_radio_integration_plan_v3.md"
MAX_SCENE_RECORD_BYTES = 16 * 1024 * 1024

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from network.scripts.write_m1_scene_provenance import (  # noqa: E402
    installed_bundle_manifest,
    manifest_sha256,
    scenario_world_file,
    sdf_world_name,
    sha256_file,
    source_bundle_manifest,
)
from network.validation.evidence import (  # noqa: E402
    five_uav_health_status,
    load_json,
    load_jsonl,
    provenance_status,
    raw_event_envelope_failures,
)


def gate(status: str, proof: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"status": status, "proof": proof}
    if details is not None:
        result["details"] = details
    return result


def _safe_run_directory(run_dir: Path) -> tuple[Path | None, str | None]:
    run_root = ROOT_DIR / "runs"
    try:
        if run_root.is_symlink():
            return None, "runs directory is a symbolic link"
        root = run_root.resolve(strict=True)
        candidate = run_dir if run_dir.is_absolute() else Path.cwd() / run_dir
        if candidate.is_symlink():
            return None, "run directory is a symbolic link"
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        return None, f"run directory is missing or invalid: {exc}"
    if not resolved.is_dir() or resolved.parent != root:
        return None, "run directory must be a direct child of this checkout's runs directory"
    if SAFE_RUN_ID.fullmatch(resolved.name) is None:
        return None, "run directory name is not a safe RUN_ID"
    return resolved, None


def _immutable_json(path: Path) -> tuple[dict[str, Any], str | None]:
    try:
        item_stat = path.lstat()
    except OSError as exc:
        return {}, f"{path.name} is missing or unreadable: {exc}"
    if (
        not stat.S_ISREG(item_stat.st_mode)
        or item_stat.st_nlink != 1
        or item_stat.st_size < 2
        or item_stat.st_size > MAX_SCENE_RECORD_BYTES
    ):
        return {}, f"{path.name} is not a bounded single-link regular file"
    if item_stat.st_mode & 0o222:
        return {}, f"{path.name} is not immutable (write bits are set)"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {}, f"{path.name} is not valid UTF-8 JSON: {exc}"
    if not isinstance(value, dict):
        return {}, f"{path.name} JSON root is not an object"
    return value, None


def _call_gate(name: str, function: Callable[[Path], dict[str, Any]], run_dir: Path) -> dict[str, Any]:
    try:
        result = function(run_dir)
    except Exception as exc:
        return gate("failed", f"{name} raised an exception", {"failures": [str(exc)]})
    if not isinstance(result, dict) or result.get("status") != "passed":
        return gate(
            "failed",
            f"{name} did not pass",
            {"independent_status": result},
        )
    return result


def _gazebo_world_arguments(arguments: str) -> list[str]:
    if re.search(r"(?:^|\s)(?:\S*/)?gz\s+sim(?:\s|$)", arguments) is None:
        return []
    return re.findall(r"(?<!\S)(/[^\s'\"]+\.sdf)(?!\S)", arguments)


def scene_status(run_dir: Path) -> dict[str, Any]:
    failures: list[str] = []
    record, record_error = _immutable_json(run_dir / SCENE_RECORD)
    if record_error:
        return gate("failed", "immutable M1 scene provenance is unavailable", {"failures": [record_error]})

    provenance = load_json(run_dir / "metrics/provenance.json")
    health = load_json(run_dir / "metrics/five_uav_health.json")
    if record.get("schema_version") != 1:
        failures.append("scene provenance schema_version is not 1")
    config_hashes = provenance.get("config_hashes") if isinstance(provenance.get("config_hashes"), dict) else {}
    current_contract_hash = (
        sha256_file(ROOT_DIR / M1_PLAN_PATH) if (ROOT_DIR / M1_PLAN_PATH).is_file() else None
    )
    if (
        record.get("contract") != M1_CONTRACT_ID
        or record.get("plan_version") != 3
        or record.get("contract_path") != M1_PLAN_PATH
        or record.get("contract_sha256") != current_contract_hash
        or config_hashes.get(M1_PLAN_PATH) != current_contract_hash
    ):
        failures.append("M1 scene provenance is not bound to the current v3 contract")
    if (
        health.get("contract") != M1_CONTRACT_ID
        or health.get("plan_version") != 3
        or health.get("contract_sha256") != current_contract_hash
    ):
        failures.append("five-UAV health summary is not bound to the current v3 contract")
    if record.get("run_id") != run_dir.name:
        failures.append("scene provenance run_id does not match")
    if record.get("runtime_id") != health.get("runtime_id"):
        failures.append("scene runtime_id does not match raw-health identity")
    if record.get("source_hash") != provenance.get("source_hash"):
        failures.append("scene source_hash does not match provenance")
    if record.get("component_only") is not True or record.get("p0_eligible") is not False:
        failures.append("scene provenance is not labeled component-only/non-P0")

    scenario = record.get("scenario") if isinstance(record.get("scenario"), dict) else {}
    gazebo = record.get("gazebo") if isinstance(record.get("gazebo"), dict) else {}
    source = record.get("source") if isinstance(record.get("source"), dict) else {}
    installed = record.get("installed") if isinstance(record.get("installed"), dict) else {}
    scenario_relative = scenario.get("path")
    if not isinstance(scenario_relative, str):
        failures.append("scene scenario path is missing")
        scenario_relative = "invalid"
    try:
        scenario_path = ROOT_DIR / scenario_relative
        world_file, canonical_scenario_relative = scenario_world_file(
            scenario_path, root=ROOT_DIR
        )
        if scenario_relative != canonical_scenario_relative:
            failures.append("scene scenario path is not canonical")
    except Exception as exc:
        failures.append(f"canonical scenario/world resolution failed: {exc}")
        world_file = "invalid.sdf"
        scenario_path = ROOT_DIR / "invalid"

    if scenario.get("world_file") != world_file:
        failures.append("recorded world_file differs from scenario.scenario.map.world_file")
    if scenario_path.is_file() and scenario.get("sha256") != sha256_file(scenario_path):
        failures.append("recorded scenario hash differs from canonical scenario")

    try:
        current_world_name = sdf_world_name(ROOT_DIR / SOURCE_WORLDS / world_file)
    except Exception as exc:
        failures.append(f"canonical Gazebo world-name resolution failed: {exc}")
        current_world_name = None
    dependency_versions = (
        provenance.get("dependency_versions")
        if isinstance(provenance.get("dependency_versions"), dict)
        else {}
    )
    if gazebo.get("version") != dependency_versions.get("gazebo") or not isinstance(
        gazebo.get("version"), str
    ):
        failures.append("recorded Gazebo version differs from accepted runtime provenance")
    if gazebo.get("world_name") != current_world_name:
        failures.append("recorded Gazebo world name differs from canonical SDF")
    if health.get("gazebo_world_name") != current_world_name:
        failures.append("five-UAV health summary uses a different Gazebo world name")

    try:
        current_source = source_bundle_manifest(world_file, root=ROOT_DIR)
    except Exception as exc:
        failures.append(f"canonical source world bundle failed validation: {exc}")
        current_source = {}
    expected_source_path = f"{SOURCE_WORLDS}/{world_file}"
    if source.get("worlds_root") != SOURCE_WORLDS:
        failures.append("source worlds root is not canonical")
    if source.get("active_world_path") != expected_source_path:
        failures.append("source active-world path is not canonical")
    if source.get("bundle_files") != current_source:
        failures.append("recorded source world bundle differs from canonical source")
    if source.get("bundle_sha256") != manifest_sha256(current_source):
        failures.append("recorded source world bundle hash is invalid")
    if source.get("active_world_sha256") != current_source.get(world_file):
        failures.append("recorded source active-world hash is invalid")
    source_manifest = provenance.get("source_manifest") if isinstance(provenance.get("source_manifest"), dict) else {}
    if source_manifest.get(expected_source_path) != current_source.get(world_file):
        failures.append("active world is not bound by the accepted source manifest")

    runtime_root_text = record.get("runtime_checkout_path")
    try:
        runtime_root = Path(runtime_root_text)
        if not isinstance(runtime_root_text, str) or not runtime_root.is_absolute():
            raise ValueError("runtime root is not absolute")
    except (TypeError, ValueError) as exc:
        failures.append(f"runtime checkout path is invalid: {exc}")
        runtime_root = ROOT_DIR
    expected_installed_world = f"{INSTALLED_SHARE}/worlds/{world_file}"
    if installed.get("package_share_path") != INSTALLED_SHARE:
        failures.append("installed package-share path is not canonical")
    if installed.get("active_world_path") != expected_installed_world:
        failures.append("installed active-world path is not canonical")
    expected_runtime_world = str(runtime_root / expected_installed_world)
    if installed.get("runtime_active_world_path") != expected_runtime_world:
        failures.append("runtime active-world path is inconsistent")
    try:
        current_installed, resolved_paths = installed_bundle_manifest(
            world_file,
            set(current_source),
            installed_worlds=ROOT_DIR / INSTALLED_SHARE / "worlds",
            local_root=ROOT_DIR,
            runtime_root=runtime_root,
        )
    except Exception as exc:
        failures.append(f"active installed world bundle failed validation: {exc}")
        current_installed, resolved_paths = {}, {}
    if current_installed != current_source:
        failures.append("active installed world bundle differs from canonical source")
    if installed.get("bundle_files") != current_installed:
        failures.append("recorded installed world bundle differs from active install")
    if installed.get("bundle_sha256") != manifest_sha256(current_installed):
        failures.append("recorded installed world bundle hash is invalid")
    if installed.get("active_world_sha256") != current_installed.get(world_file):
        failures.append("recorded installed active-world hash is invalid")
    if installed.get("resolved_bundle_paths") != resolved_paths:
        failures.append("recorded installed world resolution differs from active install")

    raw_path = run_dir / RAW_HEALTH_LOG
    records: list[dict[str, Any]] = []
    try:
        raw_stat = raw_path.lstat()
        if not stat.S_ISREG(raw_stat.st_mode) or raw_stat.st_nlink != 1:
            raise ValueError("raw health log is not a single-link regular file")
        records, raw_failures = load_jsonl(raw_path)
        failures.extend(raw_failures)
    except (OSError, ValueError) as exc:
        failures.append(f"raw process evidence is unavailable: {exc}")
    failures.extend(
        raw_event_envelope_failures(
            records,
            run_id=run_dir.name,
            runtime_id=record.get("runtime_id"),
            source_hash=record.get("source_hash"),
        )
    )
    for index, raw in enumerate(records):
        if (
            raw.get("contract") != M1_CONTRACT_ID
            or raw.get("plan_version") != 3
            or raw.get("contract_sha256") != current_contract_hash
        ):
            failures.append(f"raw[{index}] is not bound to the current v3 contract")
    if health.get("raw_event_log") != RAW_HEALTH_LOG or health.get("raw_event_sha256") != (
        sha256_file(raw_path) if raw_path.is_file() else None
    ):
        failures.append("health summary does not bind the fixed raw process log")

    process_samples = [item for item in records if item.get("event") == "process_sample"]
    if not process_samples:
        failures.append("raw health log contains no process samples")
    launch_assignment = f"world_file:={world_file}"
    for index, sample in enumerate(process_samples):
        processes = sample.get("processes")
        if not isinstance(processes, list) or not processes:
            failures.append(f"process_sample[{index}] lacks raw process arguments")
            continue
        arguments = [
            process.get("arguments")
            for process in processes
            if isinstance(process, dict) and isinstance(process.get("arguments"), str)
        ]
        if not any(launch_assignment in value.split() for value in arguments):
            failures.append(f"process_sample[{index}] lacks explicit launch world_file assignment")
        gazebo_worlds = [
            world
            for value in arguments
            for world in _gazebo_world_arguments(value)
        ]
        if not gazebo_worlds:
            failures.append(f"process_sample[{index}] lacks a Gazebo world argument")
        elif set(gazebo_worlds) != {expected_runtime_world}:
            failures.append(
                f"process_sample[{index}] Gazebo world differs from active installed world: {sorted(set(gazebo_worlds))}"
            )

    scene_probes = [item for item in records if item.get("event") == "gazebo_scene_probe"]
    expected_models = [f"uav{index}" for index in range(1, 6)]
    if len(scene_probes) != 1:
        failures.append("raw health log must contain exactly one live Gazebo scene probe")
    else:
        scene_probe = scene_probes[0]
        if scene_probe.get("exit_code") != 0:
            failures.append("live Gazebo scene probe did not exit zero")
        if scene_probe.get("world_name") != current_world_name:
            failures.append("live Gazebo transport world name differs from canonical SDF")
        if scene_probe.get("model_names") != expected_models:
            failures.append("live Gazebo entity inventory is not exactly uav1..uav5")

    return gate(
        "passed" if not failures else "failed",
        "scenario, source/install bundle, and raw Gazebo argv were independently evaluated",
        {
            "failures": failures,
            "world_file": world_file,
            "runtime_world_path": expected_runtime_world,
            "process_samples": len(process_samples),
            "bundle_files": len(current_source),
            "world_name": current_world_name,
        },
    )


def evaluate_m1(run_dir: Path) -> dict[str, Any]:
    safe_run, input_error = _safe_run_directory(run_dir)
    if safe_run is None:
        failure = input_error or "run directory validation failed"
        gates = {
            name: gate("failed", "unsafe M1 input", {"failures": [failure]})
            for name in ("provenance", "five_uav_health", "scene")
        }
        run_id, run_path = run_dir.name, str(run_dir)
    else:
        gates = {
            "provenance": _call_gate("provenance_status", provenance_status, safe_run),
            "five_uav_health": _call_gate(
                "five_uav_health_status", five_uav_health_status, safe_run
            ),
            "scene": _call_gate("scene_status", scene_status, safe_run),
        }
        run_id, run_path = safe_run.name, str(safe_run)
    failures = [
        f"{name}: {value.get('proof', 'gate failed')}"
        for name, value in gates.items()
        if value.get("status") != "passed"
    ]
    return {
        "schema_version": 1,
        "contract": M1_CONTRACT_ID,
        "plan_version": 3,
        "contract_path": M1_PLAN_PATH,
        "contract_sha256": (
            sha256_file(ROOT_DIR / M1_PLAN_PATH)
            if (ROOT_DIR / M1_PLAN_PATH).is_file()
            else None
        ),
        "validator": "m1_five_uav_component_health",
        "milestone": "M1",
        "run_id": run_id,
        "run_dir": run_path,
        "component_only": True,
        "p0_eligible": False,
        "scope": {
            "provenance": True,
            "five_uav_health": True,
            "scene_binding": True,
            "packet_path": False,
            "sealing": False,
            "attestation": False,
        },
        "passed": not failures,
        "failures": failures,
        "gates": gates,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="Re-evaluate without creating or replacing the immutable milestone result.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = evaluate_m1(args.run_dir)
    safe_run, _ = _safe_run_directory(args.run_dir)
    if safe_run is not None and not args.no_write:
        result_path = safe_run / "metrics/m1_result.json"
        try:
            with result_path.open("x", encoding="utf-8") as handle:
                handle.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            result_path.chmod(0o444)
        except Exception as exc:
            result["passed"] = False
            result["failures"].append(f"immutable M1 result could not be written: {exc}")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("passed") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
