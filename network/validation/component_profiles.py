#!/usr/bin/env python3
"""Strict loader for downstream component acceptance profiles."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_PATH = ROOT_DIR / "network/config/component_acceptance_profiles.json"
SAFE_NAME = re.compile(r"[a-z][a-z0-9_]{2,63}")
SAFE_CONTRACT = re.compile(r"ams\.[a-z0-9_.-]+/v[1-9][0-9]*")
ALLOWED_CAPABILITIES = {
    "CHOWN",
    "DAC_READ_SEARCH",
    "NET_ADMIN",
    "NET_RAW",
    "SYS_ADMIN",
}
ALLOWED_NETWORKS = {"host", "none"}
ALLOWED_PYTHON_RUNTIMES = {"base", "sionna_rt_cuda"}
SIONNA_RUNTIME_SITE = "/home/ubuntu/.local/lib/python3.10/site-packages"
COMPONENT_PYTHON_RUNTIME_CONTRACT = "ams.component-python-runtime/v1"
COMPONENT_PYTHON_EXECUTABLE = "/usr/bin/python3.10"
COMPONENT_PYTHON_MODULES = {
    "sionna.rt": "sionna-rt",
    "mitsuba": "mitsuba",
    "numpy": "numpy",
}


def expected_gpu_device_requests(
    nvidia_driver_capabilities: str,
) -> list[dict[str, Any]]:
    """Return Docker's canonical DeviceRequests record for one profile.

    Docker appends the generic ``gpu`` capability to the explicitly requested
    NVIDIA driver capabilities.  M4 additionally requests ``graphics`` while
    the pre-M4 profiles do not, so this boundary must be derived from the
    selected immutable profile instead of using one global constant.
    """

    if nvidia_driver_capabilities not in {
        "compute,utility",
        "compute,utility,graphics",
    }:
        raise ValueError("NVIDIA driver capabilities are not profile-canonical")
    capabilities = nvidia_driver_capabilities.split(",")
    if "gpu" not in capabilities:
        capabilities.append("gpu")
    return [
        {
            "Driver": "",
            "Count": -1,
            "DeviceIDs": None,
            "Capabilities": [capabilities],
            "Options": {},
        }
    ]
PRE_M4_PROVIDER_PROFILES = {
    "m0",
    "m1_component",
    "flight_capacity_prerequisite",
    "m2_component",
    "m3_component",
}
M4_PROVIDER_PROFILES = {"m4_capacity_prerequisite", "m4_component"}
EXACT_PROFILE_KEYS = {
    "consumed_nodes",
    "main_cap_add",
    "main_devices",
    "main_network",
    "nvidia_driver_capabilities",
    "python_runtime",
    "prerequisite_status_contract",
    "prerequisite_status_count",
    "required_component_profiles",
    "receipt_contract",
    "receipt_name",
    "result_contract",
    "result_path",
    "runner",
    "timeout_s",
    "validator",
    "validator_arguments",
}


def safe_relative(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"{label} is not one non-empty POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{label} is unsafe")
    return path.as_posix()


def expected_radio_provider_runtime(
    qualification_profile: str, selected_provider_id: Any
) -> dict[str, object]:
    """Return the truthful provider-use claim for one qualification profile."""

    if qualification_profile in PRE_M4_PROVIDER_PROFILES:
        return {
            "radio_provider_runtime_consumed": False,
            "runtime_provider_id": "not_applicable_pre_m4",
            "reason": "profile_pre_m4",
        }
    if qualification_profile in M4_PROVIDER_PROFILES:
        return {
            "radio_provider_runtime_consumed": True,
            "runtime_provider_id": selected_provider_id,
            "reason": "profile_m4_runtime",
        }
    return {
        "radio_provider_runtime_consumed": True,
        "runtime_provider_id": selected_provider_id,
        "reason": "diagnostic_full_path",
    }


def load_profiles(path: Path = DEFAULT_PATH) -> dict[str, dict[str, Any]]:
    try:
        payload = path.read_bytes()
        document = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=lambda pairs: _unique_pairs(pairs),
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON: {token}")
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"component profile document is invalid: {exc}") from exc
    if (
        not isinstance(document, dict)
        or set(document) != {"schema_version", "contract", "profiles"}
        or document.get("schema_version") != 1
        or document.get("contract") != "ams.component-acceptance-profiles/v1"
        or not isinstance(document.get("profiles"), dict)
        or not document["profiles"]
    ):
        raise ValueError("component profile root schema is not exact")
    profiles: dict[str, dict[str, Any]] = {}
    runners: set[str] = set()
    receipts: set[str] = set()
    for name, value in sorted(document["profiles"].items()):
        if SAFE_NAME.fullmatch(str(name)) is None:
            raise ValueError(f"unsafe component profile name: {name!r}")
        if not isinstance(value, dict) or set(value) != EXACT_PROFILE_KEYS:
            raise ValueError(f"component profile {name} schema is not exact")
        consumed = value["consumed_nodes"]
        if (
            not isinstance(consumed, list)
            or not consumed
            or consumed != [f"Q{index}" for index in range(len(consumed))]
            or len(consumed) > 9
        ):
            raise ValueError(f"component profile {name} consumed nodes are not a prefix")
        caps = value["main_cap_add"]
        devices = value["main_devices"]
        if (
            not isinstance(caps, list)
            or caps != sorted(set(caps))
            or not set(caps).issubset(ALLOWED_CAPABILITIES)
            or not isinstance(devices, list)
            or devices not in ([], ["/dev/net/tun"])
            or value["main_network"] not in ALLOWED_NETWORKS
        ):
            raise ValueError(f"component profile {name} isolation policy is invalid")
        if devices and set(caps) != ALLOWED_CAPABILITIES:
            raise ValueError(f"component profile {name} TUN policy lacks exact capabilities")
        if value["nvidia_driver_capabilities"] not in {
            "compute,utility",
            "compute,utility,graphics",
        }:
            raise ValueError(
                f"component profile {name} NVIDIA driver capabilities are invalid"
            )
        if value["python_runtime"] not in ALLOWED_PYTHON_RUNTIMES:
            raise ValueError(f"component profile {name} Python runtime is invalid")
        count = value["prerequisite_status_count"]
        if not isinstance(count, int) or isinstance(count, bool) or not 2 <= count <= 4:
            raise ValueError(f"component profile {name} prerequisite count is invalid")
        required_profiles = value["required_component_profiles"]
        if (
            not isinstance(required_profiles, list)
            or required_profiles != sorted(set(required_profiles))
            or any(
                not isinstance(item, str) or SAFE_NAME.fullmatch(item) is None
                for item in required_profiles
            )
        ):
            raise ValueError(
                f"component profile {name} required component profiles are invalid"
            )
        for field in ("prerequisite_status_contract", "receipt_contract", "result_contract"):
            if SAFE_CONTRACT.fullmatch(str(value[field])) is None:
                raise ValueError(f"component profile {name} {field} is invalid")
        timeout = value["timeout_s"]
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 300 <= timeout <= 1800:
            raise ValueError(f"component profile {name} timeout is invalid")
        runner = safe_relative(value["runner"], f"{name}.runner")
        validator = safe_relative(value["validator"], f"{name}.validator")
        result_path = safe_relative(value["result_path"], f"{name}.result_path")
        receipt_name = value["receipt_name"]
        if (
            not isinstance(receipt_name, str)
            or re.fullmatch(r"[a-z0-9_]+\.json", receipt_name) is None
            or not result_path.startswith("metrics/")
        ):
            raise ValueError(f"component profile {name} output paths are invalid")
        arguments = value["validator_arguments"]
        if (
            not isinstance(arguments, list)
            or not arguments
            or not all(isinstance(item, str) and item for item in arguments)
            or arguments.count("{run_dir}") != 1
            or any("{" in item or "}" in item for item in arguments if item != "{run_dir}")
        ):
            raise ValueError(f"component profile {name} validator arguments are invalid")
        if runner in runners or receipt_name in receipts:
            raise ValueError("component profile runner or receipt name is duplicated")
        runners.add(runner)
        receipts.add(receipt_name)
        profiles[name] = {
            **value,
            "runner": runner,
            "validator": validator,
            "result_path": result_path,
        }
    for name, profile in profiles.items():
        for required_name in profile["required_component_profiles"]:
            required = profiles.get(required_name)
            if (
                required is None
                or required_name == name
                # Component receipts are selected by the exact current source
                # commit.  A dependency from another status epoch can therefore
                # never resolve; predecessor milestones use receipts.mN instead.
                or required["prerequisite_status_count"]
                != profile["prerequisite_status_count"]
                or required["prerequisite_status_contract"]
                != profile["prerequisite_status_contract"]
                or not set(required["consumed_nodes"]).issubset(
                    profile["consumed_nodes"]
                )
            ):
                raise ValueError(
                    f"component profile {name} has an invalid required profile: "
                    f"{required_name}"
                )
    expected_nvidia = {
        "flight_capacity_prerequisite": "compute,utility",
        "m2_component": "compute,utility",
        "m3_component": "compute,utility",
        "m4_capacity_prerequisite": "compute,utility,graphics",
        "m4_component": "compute,utility,graphics",
    }
    if set(profiles) != set(expected_nvidia) or any(
        profiles[name]["nvidia_driver_capabilities"] != capabilities
        for name, capabilities in expected_nvidia.items()
    ):
        raise ValueError("component profile NVIDIA capability matrix is not exact")
    expected_requirements = {
        "flight_capacity_prerequisite": [],
        "m2_component": ["flight_capacity_prerequisite"],
        "m3_component": [],
        "m4_capacity_prerequisite": [],
        "m4_component": ["m4_capacity_prerequisite"],
    }
    if any(
        profiles[name]["required_component_profiles"] != required
        for name, required in expected_requirements.items()
    ):
        raise ValueError("component profile prerequisite graph is not exact")
    expected_python_runtime = {
        "flight_capacity_prerequisite": "base",
        "m2_component": "base",
        "m3_component": "base",
        "m4_capacity_prerequisite": "sionna_rt_cuda",
        "m4_component": "sionna_rt_cuda",
    }
    if any(
        profiles[name]["python_runtime"] != runtime
        for name, runtime in expected_python_runtime.items()
    ):
        raise ValueError("component profile Python runtime matrix is not exact")
    return profiles


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key!r}")
        result[key] = value
    return result


def match_profile(
    runner: str, timeout_s: int, path: Path = DEFAULT_PATH
) -> tuple[str, dict[str, Any]]:
    profiles = load_profiles(path)
    matches = [
        (name, profile)
        for name, profile in profiles.items()
        if profile["runner"] == runner and profile["timeout_s"] == timeout_s
    ]
    if len(matches) != 1:
        raise ValueError("command does not match exactly one component acceptance profile")
    return matches[0]


def _inside_posix_path(path: str, prefix: str) -> bool:
    try:
        candidate = PurePosixPath(path)
        root = PurePosixPath(prefix)
    except (TypeError, ValueError):
        return False
    return (
        candidate.is_absolute()
        and not any(part in {"", ".", ".."} for part in candidate.parts)
        and candidate != root
        and root in candidate.parents
    )


def validate_component_python_runtime(
    profile: dict[str, Any],
    record: Any,
    dependency_versions: Any,
) -> list[str]:
    """Validate the recorded Python/module identity required by one profile.

    The record contains observations made by the producer inside the exact
    image.  This function deliberately validates only immutable identities and
    path policy; callers that execute in the image additionally re-hash the
    recorded executable and module origins.
    """

    runtime = profile.get("python_runtime")
    if runtime == "base":
        return [] if record is None else ["base profile recorded an undeclared Python runtime"]
    if runtime != "sionna_rt_cuda":
        return ["component Python runtime profile is unknown"]
    failures: list[str] = []
    if not isinstance(record, dict):
        return ["required Sionna component Python runtime record is missing"]
    expected_keys = {
        "contract",
        "profile",
        "status",
        "python_no_user_site",
        "pythonpath",
        "pythonpath_entries",
        "executable",
        "modules",
    }
    if set(record) != expected_keys:
        failures.append("component Python runtime record schema is not exact")
    if record.get("contract") != COMPONENT_PYTHON_RUNTIME_CONTRACT:
        failures.append("component Python runtime contract differs")
    if record.get("profile") != runtime or record.get("status") != "passed":
        failures.append("component Python runtime did not pass its declared profile")
    if record.get("python_no_user_site") != "1":
        failures.append("component Python runtime did not disable implicit user-site loading")

    pythonpath = record.get("pythonpath")
    entries = record.get("pythonpath_entries")
    if (
        not isinstance(pythonpath, str)
        or not isinstance(entries, list)
        or not all(isinstance(entry, str) and entry for entry in entries)
        or pythonpath.split(":") != entries
        or len(entries) != len(set(entries))
        or len(entries) < 2
        or entries[0] != "/workspace/multiagent_simulation"
        or entries[-1] != SIONNA_RUNTIME_SITE
    ):
        failures.append("component PYTHONPATH is not the exact controlled sequence")
    elif any(
        entry != "/workspace/multiagent_simulation"
        and entry != SIONNA_RUNTIME_SITE
        and not _inside_posix_path(entry, "/workspace/ardu_ws")
        and not _inside_posix_path(entry, "/opt/ros/humble")
        for entry in entries
    ):
        failures.append("component PYTHONPATH contains an uncontrolled entry")

    executable = record.get("executable")
    if not isinstance(executable, dict) or set(executable) != {
        "configured_path",
        "realpath",
        "sha256",
        "size_bytes",
    }:
        failures.append("component Python executable identity schema is not exact")
    else:
        if (
            executable.get("configured_path") not in {
                "/usr/bin/python3",
                COMPONENT_PYTHON_EXECUTABLE,
            }
            or executable.get("realpath") != COMPONENT_PYTHON_EXECUTABLE
            or re.fullmatch(r"[0-9a-f]{64}", str(executable.get("sha256") or ""))
            is None
            or not isinstance(executable.get("size_bytes"), int)
            or isinstance(executable.get("size_bytes"), bool)
            or executable.get("size_bytes", 0) <= 0
        ):
            failures.append("component Python executable identity is invalid")

    dependencies = dependency_versions if isinstance(dependency_versions, dict) else {}
    modules = record.get("modules")
    if not isinstance(modules, dict) or set(modules) != set(COMPONENT_PYTHON_MODULES):
        failures.append("component Python module identity set is not exact")
    else:
        for module_name, distribution in COMPONENT_PYTHON_MODULES.items():
            module = modules.get(module_name)
            if not isinstance(module, dict) or set(module) != {
                "distribution",
                "origin",
                "sha256",
                "size_bytes",
                "version",
            }:
                failures.append(f"component Python module schema is invalid: {module_name}")
                continue
            origin = module.get("origin")
            trusted_origin = isinstance(origin, str) and (
                _inside_posix_path(origin, SIONNA_RUNTIME_SITE)
                if module_name in {"sionna.rt", "mitsuba"}
                else any(
                    _inside_posix_path(origin, prefix)
                    for prefix in (
                        SIONNA_RUNTIME_SITE,
                        "/usr/local/lib/python3.10",
                        "/usr/lib/python3.10",
                        "/usr/lib/python3",
                    )
                )
            )
            version = module.get("version")
            if (
                module.get("distribution") != distribution
                or not trusted_origin
                or re.fullmatch(r"[0-9a-f]{64}", str(module.get("sha256") or ""))
                is None
                or not isinstance(module.get("size_bytes"), int)
                or isinstance(module.get("size_bytes"), bool)
                or module.get("size_bytes", 0) <= 0
                or not isinstance(version, str)
                or not version
                or version in {"unknown", "unavailable"}
                or dependencies.get(distribution) != version
            ):
                failures.append(f"component Python module identity is invalid: {module_name}")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_PATH)
    parser.add_argument("--runner", required=True)
    parser.add_argument("--timeout-s", type=int, required=True)
    parser.add_argument("--field")
    args = parser.parse_args(argv)
    name, profile = match_profile(args.runner, args.timeout_s, args.config)
    if args.field is None:
        print(name)
    else:
        if args.field not in profile:
            raise SystemExit(f"unknown profile field: {args.field}")
        value = profile[args.field]
        print(json.dumps(value, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
