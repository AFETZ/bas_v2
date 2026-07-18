#!/usr/bin/env python3
"""Fail-closed validator for the component-only five-UAV M1 milestone."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shlex
import stat
import sys
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath
from typing import Any, Callable


ROOT_DIR = Path(__file__).resolve().parents[2]
SAFE_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
SHA1 = re.compile(r"[0-9a-f]{40}")
SCENE_RECORD = "metrics/m1_scene_provenance.json"
RAW_HEALTH_LOG = "logs/five_uav_health_events.jsonl"
SOURCE_WORLDS = "src/multiagent_simulation/worlds"
M1_CONTRACT_ID = "ams.m1.health/v3"
M1_PLAN_PATH = "doc/network_radio_integration_plan_v3.md"
MAX_SCENE_RECORD_BYTES = 16 * 1024 * 1024
MAX_RUNTIME_TEXT_BYTES = 64 * 1024 * 1024

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from network.scripts.write_m1_scene_provenance import (  # noqa: E402
    LAUNCH_SOURCE_RELATIVE,
    RUNTIME_OVERLAY_PACKAGE_SUFFIX,
    ROBOT_DESCRIPTION_PORT_TOKEN,
    SOURCE_PACKAGE_RELATIVE,
    SOURCE_WORLDS_RELATIVE,
    canonical_robot_model,
    installed_bundle_manifest,
    installed_scene_resource_manifest,
    manifest_sha256,
    resolve_runtime_leaf,
    resolved_robot_descriptions,
    scenario_world_file,
    sdf_world_name,
    sha256_file,
    source_bundle_manifest,
    source_scene_resource_manifest,
)
from network.validation.evidence import (  # noqa: E402
    five_uav_health_status,
    gazebo_top_level_model_names,
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


def _decoded_process_argv(process: dict[str, Any]) -> tuple[list[str], str | None]:
    encoded = process.get("cmdline_b64")
    if not isinstance(encoded, str) or not encoded:
        return [], "cmdline_b64 is missing"
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        return [], f"cmdline_b64 is invalid: {exc}"
    if not raw or b"\0" not in raw:
        return [], "raw cmdline is empty or not NUL-delimited"
    trimmed = raw.rstrip(b"\0")
    if not trimmed:
        return [], "raw cmdline has no arguments"
    fields = trimmed.split(b"\0")
    if any(not field for field in fields):
        return [], "raw cmdline contains an empty interior argument"
    try:
        argv = [field.decode("utf-8", errors="strict") for field in fields]
    except UnicodeDecodeError as exc:
        return [], f"raw cmdline is not UTF-8: {exc}"
    if process.get("cmdline_sha256") != hashlib.sha256(raw).hexdigest():
        return [], "cmdline_sha256 does not match cmdline_b64"
    return argv, None


def _gazebo_server_argv(argv: list[str]) -> list[str]:
    if len(argv) == 1:
        try:
            tokens = shlex.split(argv[0])
        except ValueError:
            return []
    else:
        tokens = list(argv)
    if len(tokens) >= 2 and Path(tokens[0]).name == "gz" and tokens[1] == "sim":
        return ["gz", *tokens[1:]]
    return []


def scene_status(run_dir: Path) -> dict[str, Any]:
    failures: list[str] = []
    installed_share = (
        PurePosixPath("runs")
        / run_dir.name
        / RUNTIME_OVERLAY_PACKAGE_SUFFIX
    ).as_posix()
    record, record_error = _immutable_json(run_dir / SCENE_RECORD)
    if record_error:
        return gate("failed", "immutable M1 scene provenance is unavailable", {"failures": [record_error]})

    provenance = load_json(run_dir / "metrics/provenance.json")
    health = load_json(run_dir / "metrics/five_uav_health.json")
    if record.get("schema_version") != 2:
        failures.append("scene provenance schema_version is not 2")
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

    try:
        robot_model = canonical_robot_model(record.get("robot_model"))
    except Exception as exc:
        failures.append(f"recorded robot_model is invalid: {exc}")
        robot_model = "invalid"

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
    expected_installed_world = f"{installed_share}/worlds/{world_file}"
    if installed.get("package_share_path") != installed_share:
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
            installed_worlds=ROOT_DIR / installed_share / "worlds",
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
    expected_launch_relative = (
        PurePosixPath(installed_share)
        / "launch"
        / "multiagent_simulation.launch.py"
    ).as_posix()
    try:
        installed_launch = resolve_runtime_leaf(
            ROOT_DIR / expected_launch_relative,
            local_root=ROOT_DIR,
            runtime_root=runtime_root,
            label="installed multiagent launch file",
        )
        installed_launch_sha256 = sha256_file(installed_launch)
    except Exception as exc:
        failures.append(f"installed multiagent launch resolution failed: {exc}")
        installed_launch = ROOT_DIR / "invalid"
        installed_launch_sha256 = None
    source_launch = ROOT_DIR / LAUNCH_SOURCE_RELATIVE
    source_launch_sha256 = sha256_file(source_launch) if source_launch.is_file() else None
    if (
        installed.get("launch_file_path") != expected_launch_relative
        or installed.get("runtime_launch_file_path")
        != str(runtime_root / expected_launch_relative)
        or installed.get("resolved_launch_file_path") != str(installed_launch)
        or installed.get("launch_file_sha256") != installed_launch_sha256
        or installed_launch_sha256 != source_launch_sha256
    ):
        failures.append("installed launch file is not exactly bound to committed source")

    resources = record.get("resources") if isinstance(record.get("resources"), dict) else {}
    expected_resource_roots = sorted(
        [
            f"worlds/{world_file}",
            f"models/{robot_model}/model.sdf",
        ]
    )
    try:
        current_source_resources, current_resource_edges = source_scene_resource_manifest(
            world_file, robot_model, root=ROOT_DIR
        )
    except Exception as exc:
        failures.append(f"canonical transitive source resources failed validation: {exc}")
        current_source_resources, current_resource_edges = {}, []
    try:
        (
            current_installed_resources,
            current_installed_edges,
            current_installed_resource_paths,
        ) = installed_scene_resource_manifest(
            world_file,
            robot_model,
            installed_package_share=ROOT_DIR / installed_share,
            local_root=ROOT_DIR,
            runtime_root=runtime_root,
        )
    except Exception as exc:
        failures.append(f"active installed transitive resources failed validation: {exc}")
        current_installed_resources, current_installed_edges = {}, []
        current_installed_resource_paths = {}
    if current_installed_resources != current_source_resources:
        failures.append("installed transitive scene resources differ from canonical source")
    if current_installed_edges != current_resource_edges:
        failures.append("installed transitive scene URI graph differs from canonical source")
    if resources.get("roots") != expected_resource_roots:
        failures.append("recorded transitive scene roots are not canonical")
    if resources.get("source_package_root") != SOURCE_PACKAGE_RELATIVE.as_posix():
        failures.append("recorded source package root is not canonical")
    if resources.get("installed_package_share_path") != installed_share:
        failures.append("recorded installed resource root is not canonical")
    if resources.get("source_files") != current_source_resources:
        failures.append("recorded transitive source resource manifest differs from checkout")
    if resources.get("installed_files") != current_installed_resources:
        failures.append("recorded transitive installed resource manifest differs from install")
    if resources.get("uri_edges") != current_resource_edges:
        failures.append("recorded transitive URI graph differs from checkout")
    if resources.get("source_sha256") != manifest_sha256(current_source_resources):
        failures.append("recorded transitive source resource hash is invalid")
    if resources.get("installed_sha256") != manifest_sha256(current_installed_resources):
        failures.append("recorded transitive installed resource hash is invalid")
    if resources.get("resolved_installed_paths") != current_installed_resource_paths:
        failures.append("recorded installed transitive resource resolution differs")

    binding = (
        record.get("source_manifest_binding")
        if isinstance(record.get("source_manifest_binding"), dict)
        else {}
    )
    expected_binding_files: dict[str, str] = {
        scenario_relative: sha256_file(scenario_path) if scenario_path.is_file() else "invalid",
    }
    launch_path = ROOT_DIR / LAUNCH_SOURCE_RELATIVE
    if launch_path.is_file():
        expected_binding_files[LAUNCH_SOURCE_RELATIVE.as_posix()] = sha256_file(launch_path)
    else:
        failures.append("canonical multiagent launch source is missing")
    expected_binding_files.update(
        {
            (SOURCE_PACKAGE_RELATIVE / logical).as_posix(): file_hash
            for logical, file_hash in current_source_resources.items()
        }
    )
    expected_binding_files.update(
        {
            (SOURCE_WORLDS_RELATIVE / logical).as_posix(): file_hash
            for logical, file_hash in current_source.items()
        }
    )
    expected_binding_files = dict(sorted(expected_binding_files.items()))
    if binding.get("files") != expected_binding_files:
        failures.append("recorded scene source-manifest binding differs from active inputs")
    if binding.get("sha256") != manifest_sha256(expected_binding_files):
        failures.append("recorded scene source-manifest binding hash is invalid")
    for relative, expected_hash in expected_binding_files.items():
        if source_manifest.get(relative) != expected_hash:
            failures.append(f"accepted source manifest does not bind scene input {relative}")

    robot_description_record = (
        record.get("robot_descriptions")
        if isinstance(record.get("robot_descriptions"), dict)
        else {}
    )
    template_logical = f"models/{robot_model}/model.sdf"
    try:
        template_path = resolve_runtime_leaf(
            ROOT_DIR / installed_share / template_logical,
            local_root=ROOT_DIR,
            runtime_root=runtime_root,
            label="installed robot SDF template",
        )
        expected_robot_descriptions = resolved_robot_descriptions(
            scenario_path,
            robot_model,
            template_path=template_path,
            root=ROOT_DIR,
        )
    except Exception as exc:
        failures.append(f"canonical per-UAV robot descriptions failed validation: {exc}")
        expected_robot_descriptions = []
    if robot_description_record.get("template_path") != template_logical:
        failures.append("recorded robot-description template path is not canonical")
    if robot_description_record.get("template_sha256") != current_installed_resources.get(
        template_logical
    ):
        failures.append("recorded robot-description template hash is invalid")
    if robot_description_record.get("substitution_token") != ROBOT_DESCRIPTION_PORT_TOKEN:
        failures.append("recorded robot-description substitution token is invalid")
    if robot_description_record.get("port_formula") != "9002 + 10 * instance":
        failures.append("recorded robot-description port formula is invalid")
    if robot_description_record.get("instances") != expected_robot_descriptions:
        failures.append("recorded per-UAV robot descriptions differ from deterministic launch inputs")

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
    expected_launch_assignments = {
        "robots_config_file": (
            f"robots_config_file:={runtime_root / scenario_relative}"
        ),
        "world_file": f"world_file:={world_file}",
        "robot_model": f"robot_model:={robot_model}",
        "enable_serial2": "enable_serial2:=false",
        "generate_sensor_models": "generate_sensor_models:=false",
        "gui": "gui:=false",
        "rviz": "rviz:=false",
        "headless_rendering": "headless_rendering:=false",
        "use_mapping_camera": "use_mapping_camera:=false",
        "use_navigation_camera": "use_navigation_camera:=false",
        "use_zed_camera": "use_zed_camera:=false",
    }
    expected_gazebo_argv = ["gz", "sim", "-v4", "-s", "-r", expected_runtime_world]
    expected_robot_names = [f"uav{index}" for index in range(1, 6)]
    for index, sample in enumerate(process_samples):
        processes = sample.get("processes")
        if not isinstance(processes, list) or not processes:
            failures.append(f"process_sample[{index}] lacks raw process identities")
            continue
        decoded: list[tuple[dict[str, Any], list[str]]] = []
        for process_index, process in enumerate(processes):
            if not isinstance(process, dict):
                failures.append(
                    f"process_sample[{index}].processes[{process_index}] is not an object"
                )
                continue
            argv, argv_error = _decoded_process_argv(process)
            if argv_error:
                failures.append(
                    f"process_sample[{index}].processes[{process_index}] {argv_error}"
                )
                continue
            decoded.append((process, argv))

        launch_argvs = [
            argv
            for _process, argv in decoded
            if any(
                argv[position : position + 3]
                == ["launch", "multiagent_simulation", "multiagent_simulation.launch.py"]
                for position in range(max(0, len(argv) - 2))
            )
        ]
        if len(launch_argvs) != 1:
            failures.append(
                f"process_sample[{index}] must contain exactly one raw multiagent launch argv"
            )
        else:
            launch_argv = launch_argvs[0]
            for assignment_name, expected_assignment in expected_launch_assignments.items():
                observed = [
                    value for value in launch_argv if value.startswith(f"{assignment_name}:=")
                ]
                if observed != [expected_assignment]:
                    failures.append(
                        f"process_sample[{index}] raw launch {assignment_name} assignment differs: {observed}"
                    )

        gazebo_argvs = [
            normalized
            for _process, argv in decoded
            if (normalized := _gazebo_server_argv(argv))
        ]
        if len(gazebo_argvs) != 1:
            failures.append(
                f"process_sample[{index}] must contain exactly one raw Gazebo server argv"
            )
        elif gazebo_argvs[0] != expected_gazebo_argv:
            failures.append(
                f"process_sample[{index}] raw Gazebo server argv differs: {gazebo_argvs[0]}"
            )

        robot_state_names: list[str] = []
        for process, argv in decoded:
            executable_names = {
                Path(str(process.get("exe_path") or "")).name,
                Path(argv[0]).name if argv else "",
            }
            if "robot_state_publisher" not in executable_names:
                continue
            namespaces = [
                value.removeprefix("__ns:=/")
                for value in argv
                if value.startswith("__ns:=/")
            ]
            if len(namespaces) != 1:
                failures.append(
                    f"process_sample[{index}] robot_state_publisher has non-exact namespace argv"
                )
            else:
                robot_state_names.append(namespaces[0])
        if sorted(robot_state_names) != expected_robot_names:
            failures.append(
                f"process_sample[{index}] robot_state_publisher namespaces are not exactly uav1..uav5"
            )

    scene_probes = [item for item in records if item.get("event") == "gazebo_scene_probe"]
    if len(scene_probes) != 1:
        failures.append("raw health log must contain exactly one live Gazebo scene probe")
    else:
        scene_probe = scene_probes[0]
        if scene_probe.get("exit_code") != 0:
            failures.append("live Gazebo scene probe did not exit zero")
        if scene_probe.get("world_name") != current_world_name:
            failures.append("live Gazebo transport world name differs from canonical SDF")
        expected_scene_command = [
            "gz",
            "service",
            "-s",
            f"/world/{current_world_name}/scene/info",
            "--reqtype",
            "gz.msgs.Empty",
            "--reptype",
            "gz.msgs.Scene",
            "--timeout",
            "5000",
            "--req",
            "",
        ]
        if scene_probe.get("command") != expected_scene_command:
            failures.append("live Gazebo scene probe command is not canonical")
        response_b64 = scene_probe.get("stdout_b64", scene_probe.get("response_b64"))
        response_hash = scene_probe.get(
            "stdout_sha256", scene_probe.get("response_sha256")
        )
        try:
            if not isinstance(response_b64, str) or not response_b64:
                raise ValueError("raw Gazebo response is missing")
            response = base64.b64decode(response_b64, validate=True)
            if hashlib.sha256(response).hexdigest() != response_hash:
                raise ValueError("raw Gazebo response hash does not match")
            response_text = response.decode("utf-8", errors="strict")
            derived_models = sorted(
                name
                for name in gazebo_top_level_model_names(response_text)
                if name.startswith("uav")
            )
        except (ValueError, TypeError, UnicodeDecodeError) as exc:
            failures.append(f"raw Gazebo scene response is invalid: {exc}")
            derived_models = []
        if derived_models != expected_robot_names:
            failures.append("raw Gazebo entity response is not exactly uav1..uav5")
        if scene_probe.get("model_names") != derived_models:
            failures.append("summarized Gazebo entity names differ from raw response")
        try:
            stderr_b64 = scene_probe.get("stderr_b64")
            if not isinstance(stderr_b64, str):
                raise ValueError("raw Gazebo stderr is missing")
            raw_stderr = base64.b64decode(stderr_b64, validate=True)
            if hashlib.sha256(raw_stderr).hexdigest() != scene_probe.get("stderr_sha256"):
                raise ValueError("raw Gazebo stderr hash does not match")
        except (ValueError, TypeError) as exc:
            failures.append(f"raw Gazebo scene stderr is invalid: {exc}")

    robot_probes = [
        item for item in records if item.get("event") == "robot_description_probe"
    ]
    if len(robot_probes) != 1:
        failures.append("raw health log must contain exactly one robot-description probe")
    else:
        probed_robots = robot_probes[0].get("robots")
        if not isinstance(probed_robots, list) or len(probed_robots) != 5:
            failures.append("robot-description probe must contain exactly five robots")
            probed_robots = []
        for index, expected in enumerate(expected_robot_descriptions):
            if index >= len(probed_robots) or not isinstance(probed_robots[index], dict):
                failures.append(f"robot-description probe lacks uav{index + 1}")
                continue
            observed = probed_robots[index]
            if observed.get("name") != expected["name"] or observed.get("namespace") != (
                f"/{expected['name']}"
            ):
                failures.append(
                    f"robot-description probe[{index}] name/namespace is not canonical"
                )
            try:
                encoded_description = observed.get("robot_description_b64")
                if not isinstance(encoded_description, str) or not encoded_description:
                    raise ValueError("robot_description_b64 is missing")
                description_bytes = base64.b64decode(encoded_description, validate=True)
                description_hash = hashlib.sha256(description_bytes).hexdigest()
                description_xml = ET.fromstring(description_bytes)
                plugins = [
                    element
                    for element in description_xml.iter()
                    if element.tag.rsplit("}", 1)[-1] == "plugin"
                    and (
                        element.attrib.get("name") == "ArduPilotPlugin"
                        or element.attrib.get("filename") == "ArduPilotPlugin"
                    )
                ]
                if len(plugins) != 1:
                    raise ValueError("live robot-description lacks one exact ArduPilotPlugin")
                addresses = [
                    element.text.strip()
                    for element in plugins[0]
                    if element.tag.rsplit("}", 1)[-1] == "fdm_addr"
                    and isinstance(element.text, str)
                ]
                ports = [
                    element.text.strip()
                    for element in plugins[0]
                    if element.tag.rsplit("}", 1)[-1] == "fdm_port_in"
                    and isinstance(element.text, str)
                ]
                if description_hash != expected["robot_description_sha256"]:
                    raise ValueError("live robot-description hash differs from deterministic input")
                if addresses != [expected["fdm_addr"]] or ports != [
                    str(expected["fdm_port_in"])
                ]:
                    raise ValueError("live robot-description FDM endpoint differs")
                if observed.get("robot_description_sha256") not in (
                    None,
                    description_hash,
                ):
                    raise ValueError("reported robot-description hash differs from raw bytes")
            except (ValueError, TypeError, ET.ParseError) as exc:
                failures.append(f"robot-description probe[{index}] is invalid: {exc}")

    return gate(
        "passed" if not failures else "failed",
        "scenario, source/install bundle, and raw Gazebo argv were independently evaluated",
        {
            "failures": failures,
            "world_file": world_file,
            "runtime_world_path": expected_runtime_world,
            "process_samples": len(process_samples),
            "bundle_files": len(current_source),
            "transitive_resource_files": len(current_source_resources),
            "robot_model": robot_model,
            "world_name": current_world_name,
        },
    )


def _bounded_runtime_text(path: Path, *, allow_empty: bool = False) -> str:
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_size > MAX_RUNTIME_TEXT_BYTES
        or (not allow_empty and info.st_size < 1)
    ):
        raise ValueError(f"{path.name} is not one bounded regular file")
    payload = path.read_bytes()
    after = path.lstat()
    if (
        len(payload) != info.st_size
        or (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_mode, after.st_size, after.st_mtime_ns)
    ):
        raise ValueError(f"{path.name} changed while it was read")
    try:
        return payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path.name} is not UTF-8: {exc}") from exc


def _unique_environment(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in _bounded_runtime_text(path).splitlines():
        name, separator, value = line.partition("=")
        if not separator or not name or name in values:
            raise ValueError("environment.txt contains a malformed or duplicate assignment")
        values[name] = value
    return values


def _package_runtime_inputs(package_root: Path) -> dict[str, str]:
    candidates: list[Path] = [package_root / "package.xml"]
    for directory in ("config", "launch", "models", "worlds", "rviz"):
        root = package_root / directory
        root_info = root.lstat()
        if not stat.S_ISDIR(root_info.st_mode):
            raise ValueError(f"package runtime-input directory is unavailable: {directory}")
        for path in root.rglob("*"):
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                continue
            relative = path.relative_to(package_root).as_posix()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError(
                    f"package runtime input is linked or special: {relative}"
                )
            candidates.append(path)
    result: dict[str, str] = {}
    for path in sorted(candidates, key=lambda item: item.relative_to(package_root).as_posix()):
        relative = path.relative_to(package_root).as_posix()
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError(f"package runtime input is linked or special: {relative}")
        if relative in result:
            raise ValueError(f"duplicate package runtime input: {relative}")
        result[relative] = sha256_file(path)
    return result


def runtime_inputs_status(run_dir: Path) -> dict[str, Any]:
    """Prove that M1 used a fresh, run-local install from the accepted source."""

    failures: list[str] = []
    overlay = run_dir / "runtime_overlay"
    installed_share = (
        overlay / "install/multiagent_simulation/share/multiagent_simulation"
    )
    expected_command = [
        "/usr/bin/colcon",
        "--log-base",
        str(overlay / "log"),
        "build",
        "--base-paths",
        str(ROOT_DIR / "src/multiagent_simulation"),
        "--build-base",
        str(overlay / "build"),
        "--install-base",
        str(overlay / "install"),
    ]
    command_path = run_dir / "logs/m1_runtime_overlay_build.command"
    log_path = run_dir / "logs/m1_runtime_overlay_build.log"
    exit_path = run_dir / "logs/m1_runtime_overlay_build.exit_code"
    try:
        command_text = _bounded_runtime_text(command_path)
        command = shlex.split(command_text, posix=True)
        if command != expected_command:
            failures.append("runtime-overlay build command is not canonical")
    except (OSError, ValueError) as exc:
        command = []
        failures.append(f"runtime-overlay build command is unavailable: {exc}")
    try:
        build_log = _bounded_runtime_text(log_path, allow_empty=True)
    except (OSError, ValueError) as exc:
        build_log = ""
        failures.append(f"runtime-overlay build log is unavailable: {exc}")
    try:
        if _bounded_runtime_text(exit_path) != "0\n":
            failures.append("runtime-overlay build exit code is not exact zero")
    except (OSError, ValueError) as exc:
        failures.append(f"runtime-overlay build exit code is unavailable: {exc}")

    try:
        environment = _unique_environment(run_dir / "environment.txt")
    except (OSError, ValueError) as exc:
        environment = {}
        failures.append(f"M1 environment record is invalid: {exc}")
    source_commit = environment.get("source_commit")
    provenance = load_json(run_dir / "metrics/provenance.json")
    if environment.get("source_mode") != "clean_git_clone_ro":
        failures.append("M1 source mode is not the formal clean read-only checkout")
    if not isinstance(source_commit, str) or SHA1.fullmatch(source_commit) is None:
        failures.append("M1 source commit is not one exact Git commit")
    elif provenance.get("git_commit") != source_commit:
        failures.append("M1 source commit differs from accepted provenance")
    if environment.get("runtime_overlay") != str(overlay):
        failures.append("M1 environment does not bind the run-local runtime overlay")
    if environment.get("installed_package_share") != str(installed_share):
        failures.append("M1 environment does not bind the run-local package share")
    expected_resource_path = (
        f"{installed_share}/models:{installed_share}/worlds:{installed_share}"
    )
    if environment.get("gz_sim_resource_path") != expected_resource_path:
        failures.append("Gazebo resources are not restricted to the run-local install")
    if environment.get("generate_sensor_models") != "false":
        failures.append("M1 launch did not disable source-tree sensor generation")
    if environment.get("python_dont_write_bytecode") != "1":
        failures.append("M1 launch did not disable runtime bytecode writes")
    if environment.get("python_pycache_prefix") != "/tmp/ams-m1-pycache":
        failures.append("M1 launch did not redirect any runtime bytecode cache to tmpfs")
    if environment.get("python_executable") != "/usr/bin/python3.10":
        failures.append("M1 collector did not use the locked Python interpreter")
    if environment.get("python_no_user_site") != "1":
        failures.append("M1 collector did not disable implicit user-site loading")
    if environment.get("pymavlink_origin") != (
        "/home/ubuntu/.local/lib/python3.10/site-packages/pymavlink/__init__.py"
    ):
        failures.append("M1 collector pymavlink origin is not the controlled image path")

    if overlay.is_symlink() or not overlay.is_dir():
        failures.append("run-local runtime overlay is missing or symbolic")
    if (overlay / "build").exists() or (overlay / "log").exists():
        failures.append("runtime-overlay build intermediates were not removed before validation")
    if installed_share.is_symlink() or not installed_share.is_dir():
        failures.append("run-local installed package share is missing or symbolic")
    installed_launch = installed_share / "launch/multiagent_simulation.launch.py"
    source_launch = ROOT_DIR / LAUNCH_SOURCE_RELATIVE
    try:
        if (
            installed_launch.is_symlink()
            or not installed_launch.is_file()
            or sha256_file(installed_launch) != sha256_file(source_launch)
        ):
            failures.append("run-local installed launch differs from committed source")
    except OSError as exc:
        failures.append(f"run-local installed launch cannot be hashed: {exc}")
    recorded_inputs: dict[str, str] = {}
    qualified_inputs: dict[str, str] = {}
    current_qualified_inputs: dict[str, str] = {}
    installed_inputs: dict[str, str] = {}
    try:
        source_package = ROOT_DIR / "src/multiagent_simulation"
        installed_inputs = _package_runtime_inputs(installed_share)

        qualification = (
            provenance.get("qualification_consumption")
            if isinstance(provenance.get("qualification_consumption"), dict)
            else {}
        )
        expected_consumed_nodes = ["Q0", "Q1"]
        supported_profiles = {"m1_component", "flight_capacity_prerequisite"}
        if (
            qualification.get("profile") not in supported_profiles
            or qualification.get("consumed_nodes") != expected_consumed_nodes
        ):
            failures.append(
                "accepted provenance does not consume exactly Q0,Q1 for a supported "
                "five-UAV qualification profile"
            )

        vector = (
            provenance.get("qualification_content_vector")
            if isinstance(provenance.get("qualification_content_vector"), dict)
            else {}
        )
        raw_entries = vector.get("entry_manifest")
        if not isinstance(raw_entries, list):
            failures.append("qualification vector entry manifest is unavailable")
            raw_entries = []
        package_prefix = "src/multiagent_simulation/"
        package_directories = {"config", "launch", "models", "worlds", "rviz"}
        seen_paths: set[str] = set()
        recorded_owners: dict[str, str] = {}
        for entry in raw_entries:
            if not isinstance(entry, dict):
                failures.append("qualification vector contains a malformed entry")
                continue
            tracked = entry.get("path")
            if not isinstance(tracked, str) or not tracked:
                failures.append("qualification vector contains a malformed path")
                continue
            if tracked in seen_paths:
                failures.append(f"qualification vector contains duplicate path {tracked}")
                continue
            seen_paths.add(tracked)
            if not tracked.startswith(package_prefix):
                continue
            relative = tracked[len(package_prefix) :]
            try:
                relative_path = PurePosixPath(relative)
            except ValueError:
                failures.append(f"qualification vector package path is invalid: {tracked}")
                continue
            relevant = relative == "package.xml" or (
                len(relative_path.parts) >= 2
                and relative_path.parts[0] in package_directories
            )
            if not relevant:
                continue
            if (
                not relative
                or relative_path.is_absolute()
                or any(part in {"", ".", ".."} for part in relative_path.parts)
                or relative_path.as_posix() != relative
            ):
                failures.append(f"qualification vector package path is invalid: {tracked}")
                continue
            owner = entry.get("owner")
            if owner not in {f"Q{index}" for index in range(9)}:
                failures.append(
                    f"qualification vector has invalid owner for package input {tracked}"
                )
                continue
            if entry.get("kind") != "regular":
                failures.append(
                    f"qualification vector package input is not regular: {tracked}"
                )
                continue
            recorded_hash = entry.get("blob_sha256")
            if (
                not isinstance(recorded_hash, str)
                or re.fullmatch(r"[0-9a-f]{64}", recorded_hash) is None
            ):
                failures.append(
                    f"qualification vector hash is invalid for package input {tracked}"
                )
                continue
            recorded_inputs[relative] = recorded_hash
            recorded_owners[relative] = owner

        source_manifest = (
            provenance.get("source_manifest")
            if isinstance(provenance.get("source_manifest"), dict)
            else {}
        )
        if not isinstance(provenance.get("source_manifest"), dict):
            failures.append("accepted provenance source manifest is unavailable")

        consumed_owners = set(expected_consumed_nodes)
        qualified_inputs = {
            relative: recorded_hash
            for relative, recorded_hash in recorded_inputs.items()
            if recorded_owners.get(relative) in consumed_owners
        }
        selective_manifest_inputs: dict[str, str] = {}
        for tracked, recorded_hash in source_manifest.items():
            if not isinstance(tracked, str) or not tracked.startswith(package_prefix):
                continue
            relative = tracked[len(package_prefix) :]
            relative_path = PurePosixPath(relative)
            relevant = relative == "package.xml" or (
                len(relative_path.parts) >= 2
                and relative_path.parts[0] in package_directories
            )
            if relevant:
                selective_manifest_inputs[relative] = recorded_hash
        if selective_manifest_inputs != qualified_inputs:
            failures.append(
                "accepted provenance does not exactly bind qualified package inputs"
            )

        for relative, recorded_hash in qualified_inputs.items():
            path = source_package / relative
            try:
                info = path.lstat()
            except OSError as exc:
                failures.append(
                    f"qualified current package input is unavailable: {relative}: {exc}"
                )
                continue
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or info.st_nlink != 1
            ):
                failures.append(
                    f"qualified current package input is linked or special: {relative}"
                )
                continue
            current_hash = sha256_file(path)
            current_qualified_inputs[relative] = current_hash
            if current_hash != recorded_hash:
                failures.append(
                    f"qualified current package input differs from recorded vector: {relative}"
                )
        if current_qualified_inputs != qualified_inputs:
            failures.append(
                "qualified current package inputs differ from the recorded M1 subset"
            )

        if installed_inputs != recorded_inputs:
            failures.append(
                "run-local installed package inputs differ from the recorded qualification vector"
            )
    except (OSError, ValueError) as exc:
        failures.append(f"complete run-local package input comparison failed: {exc}")

    return gate(
        "passed" if not failures else "failed",
        "fresh run-local M1 build and installed launch were independently evaluated",
        {
            "failures": failures,
            "source_commit": source_commit,
            "runtime_overlay": f"runs/{run_dir.name}/runtime_overlay",
            "installed_package_share": (
                f"runs/{run_dir.name}/{RUNTIME_OVERLAY_PACKAGE_SUFFIX.as_posix()}"
            ),
            "build_command": command,
            "build_log_bytes": len(build_log.encode("utf-8")),
            "source_package_input_count": len(recorded_inputs),
            "recorded_package_input_count": len(recorded_inputs),
            "qualified_package_input_count": len(qualified_inputs),
            "current_qualified_package_input_count": len(current_qualified_inputs),
            "installed_package_input_count": len(installed_inputs),
            "python_dont_write_bytecode": environment.get(
                "python_dont_write_bytecode"
            ),
            "python_pycache_prefix": environment.get("python_pycache_prefix"),
            "python_executable": environment.get("python_executable"),
            "python_no_user_site": environment.get("python_no_user_site"),
            "pymavlink_origin": environment.get("pymavlink_origin"),
            "package_inputs_sha256": hashlib.sha256(
                json.dumps(recorded_inputs, sort_keys=True, separators=(",", ":")).encode(
                    "utf-8"
                )
            ).hexdigest(),
            "qualified_package_inputs_sha256": hashlib.sha256(
                json.dumps(qualified_inputs, sort_keys=True, separators=(",", ":")).encode(
                    "utf-8"
                )
            ).hexdigest(),
        },
    )


def evaluate_m1(run_dir: Path) -> dict[str, Any]:
    safe_run, input_error = _safe_run_directory(run_dir)
    if safe_run is None:
        failure = input_error or "run directory validation failed"
        gates = {
            name: gate("failed", "unsafe M1 input", {"failures": [failure]})
            for name in ("provenance", "five_uav_health", "scene", "runtime_inputs")
        }
        run_id, run_path = run_dir.name, str(run_dir)
    else:
        gates = {
            "provenance": _call_gate("provenance_status", provenance_status, safe_run),
            "five_uav_health": _call_gate(
                "five_uav_health_status", five_uav_health_status, safe_run
            ),
            "scene": _call_gate("scene_status", scene_status, safe_run),
            "runtime_inputs": _call_gate(
                "runtime_inputs_status", runtime_inputs_status, safe_run
            ),
        }
        run_id, run_path = safe_run.name, str(safe_run)
    failures = [
        f"{name}: {value.get('proof', 'gate failed')}"
        for name, value in gates.items()
        if value.get("status") != "passed"
    ]
    return {
        "schema_version": 2,
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
        "run_dir": f"runs/{run_id}",
        "component_qualified": not failures,
        "formal_accepted": False,
        "component_only": True,
        "p0_eligible": False,
        "scope": {
            "provenance": True,
            "five_uav_health": True,
            "scene_binding": True,
            "runtime_inputs": True,
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
