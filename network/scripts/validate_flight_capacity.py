#!/usr/bin/env python3
"""Independently rederive the five-UAV 300-second capacity prerequisite."""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from network.scripts.validate_m1_health import (  # noqa: E402
    runtime_inputs_status,
    scene_status,
)


PROFILE_PATH = ROOT_DIR / "network/config/flight_capacity_profile.json"
RESULT_CONTRACT = "ams.flight-capacity-validation/v1"
EVENT_CONTRACT = "ams.flight-capacity-raw-event/v1"
OBSERVATION_CONTRACT = "ams.flight-capacity-observation/v1"
IMAGE = re.compile(r"sha256:[0-9a-f]{64}")
SHA256 = re.compile(r"[0-9a-f]{64}")
MAX_EVENT_LOG_BYTES = 512 * 1024 * 1024
MAX_EVENTS = 1_000_000
REQUIRED_PROCESS_COUNTS = {
    "arducopter": 5,
    "mavproxy": 5,
    "micro_ros_agent": 5,
    "gazebo_server": 1,
}


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def strict_loads(payload: bytes, label: str) -> Any:
    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=reject_duplicate_pairs,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON token: {token}")
            ),
        )
    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise ValueError(f"{label} is not strict JSON: {exc}") from exc


def read_regular(path: Path, *, maximum: int) -> bytes:
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_size < 1
        or info.st_size > maximum
    ):
        raise ValueError(f"not one bounded regular file: {path}")
    payload = path.read_bytes()
    after = path.lstat()
    if (
        len(payload) != info.st_size
        or (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_mode, after.st_size, after.st_mtime_ns)
    ):
        raise ValueError(f"file changed while read: {path}")
    return payload


def load_profile(path: Path) -> dict[str, Any]:
    profile = strict_loads(read_regular(path, maximum=128 * 1024), "capacity profile")
    expected_keys = {
        "schema_version",
        "contract",
        "qualification_profile",
        "consumed_nodes",
        "warmup_s",
        "measurement_s",
        "window_s",
        "window_count",
        "window_success_minimum",
        "rtf_min",
        "rtf_max",
        "readiness_stability_s",
        "readiness_timeout_s",
        "resource_period_s",
        "resource_max_gap_s",
        "clock_sample_max_gap_s",
        "clock_topic",
        "scenario_path",
        "uav_names",
        "required_mitsuba_variant",
        "competing_load_policy",
    }
    if not isinstance(profile, dict) or set(profile) != expected_keys:
        raise ValueError("capacity profile schema is not exact")
    exact = {
        "schema_version": 1,
        "contract": "ams.flight-capacity-profile/v1",
        "qualification_profile": "flight_capacity_prerequisite",
        "consumed_nodes": ["Q0", "Q1"],
        "warmup_s": 30,
        "measurement_s": 300,
        "window_s": 1,
        "window_count": 300,
        "window_success_minimum": 285,
        "rtf_min": 0.95,
        "rtf_max": 1.05,
        "readiness_stability_s": 10,
        "readiness_timeout_s": 120,
        "resource_period_s": 1,
        "resource_max_gap_s": 1.5,
        "clock_sample_max_gap_s": 0.25,
        "clock_topic": "/uav1/clock",
        "scenario_path": "network/config/scenario_5uav.yaml",
        "uav_names": [f"uav{index}" for index in range(1, 6)],
        "required_mitsuba_variant": "cuda_ad_mono_polarized",
        "competing_load_policy": "exclusive_simulation_and_gpu",
    }
    if profile != exact:
        raise ValueError("capacity profile values differ from the accepted contract")
    return profile


def load_events(path: Path, *, run_id: str, runtime_id: str) -> list[dict[str, Any]]:
    raw = read_regular(path, maximum=MAX_EVENT_LOG_BYTES)
    lines = raw.splitlines(keepends=True)
    if not lines or len(lines) > MAX_EVENTS or any(not line.endswith(b"\n") for line in lines):
        raise ValueError("event log line framing/count is invalid")
    events: list[dict[str, Any]] = []
    previous_mono = -1
    for index, line in enumerate(lines):
        payload = line[:-1]
        value = strict_loads(payload, f"event line {index}")
        if not isinstance(value, dict) or canonical(value) != payload:
            raise ValueError(f"event line {index} is not one canonical JSON object")
        common = {
            "schema_version": 1,
            "contract": EVENT_CONTRACT,
            "event_index": index,
            "run_id": run_id,
            "runtime_id": runtime_id,
        }
        if any(value.get(key) != expected for key, expected in common.items()):
            raise ValueError(f"event line {index} common identity is invalid")
        if not isinstance(value.get("event"), str) or not value["event"]:
            raise ValueError(f"event line {index} has no event type")
        mono = value.get("host_monotonic_ns")
        realtime = value.get("host_realtime_ns")
        if (
            not isinstance(mono, int)
            or isinstance(mono, bool)
            or mono <= previous_mono
            or not isinstance(realtime, int)
            or isinstance(realtime, bool)
            or realtime <= 0
        ):
            raise ValueError(f"event line {index} clock identity is invalid")
        previous_mono = mono
        events.append(value)
    return events


def one_event(events: list[dict[str, Any]], event: str) -> dict[str, Any]:
    matches = [item for item in events if item.get("event") == event]
    if len(matches) != 1:
        raise ValueError(f"expected exactly one {event} event, got {len(matches)}")
    return matches[0]


def interpolate_sim_ns(
    clock_samples: list[tuple[int, int]], boundary_ns: int, *, maximum_gap_ns: int
) -> float:
    hosts = [item[0] for item in clock_samples]
    position = bisect.bisect_left(hosts, boundary_ns)
    if position == 0 or position >= len(clock_samples):
        raise ValueError("clock samples do not bracket a measurement boundary")
    before_host, before_sim = clock_samples[position - 1]
    after_host, after_sim = clock_samples[position]
    if after_host - before_host <= 0 or after_host - before_host > maximum_gap_ns:
        raise ValueError("clock sample gap exceeds the accepted bound")
    if after_sim < before_sim:
        raise ValueError("simulation clock moved backwards")
    fraction = (boundary_ns - before_host) / (after_host - before_host)
    return before_sim + fraction * (after_sim - before_sim)


def derive_rtf_windows(
    clock_samples: list[tuple[int, int]],
    start_ns: int,
    *,
    window_count: int,
    window_ns: int,
    maximum_gap_ns: int,
) -> list[float]:
    if window_count <= 0 or window_ns <= 0:
        raise ValueError("window geometry is invalid")
    boundaries = [
        interpolate_sim_ns(
            clock_samples,
            start_ns + index * window_ns,
            maximum_gap_ns=maximum_gap_ns,
        )
        for index in range(window_count + 1)
    ]
    return [
        (boundaries[index + 1] - boundaries[index]) / window_ns
        for index in range(window_count)
    ]


def validate_identity(
    root: Path,
    run_dir: Path,
    profile: dict[str, Any],
    provenance: dict[str, Any],
) -> list[str]:
    failures: list[str] = []
    consumption = provenance.get("qualification_consumption")
    vector = provenance.get("qualification_content_vector")
    image = provenance.get("container_image")
    config_hashes = provenance.get("config_hashes")
    inherited = provenance.get("inherited_m0_qualification")
    expected_configs = {
        "network/config/flight_capacity_profile.json": sha256_file(
            root / "network/config/flight_capacity_profile.json"
        ),
        profile["scenario_path"]: sha256_file(root / profile["scenario_path"]),
        "doc/network_radio_integration_plan_v3.md": sha256_file(
            root / "doc/network_radio_integration_plan_v3.md"
        ),
        "network/config/dependency_lock.yaml": sha256_file(
            root / "network/config/dependency_lock.yaml"
        ),
    }
    if (
        provenance.get("schema_version") != 2
        or provenance.get("run_id") != run_dir.name
        or provenance.get("git_dirty") is not False
        or provenance.get("git_status") != []
        or provenance.get("acceptance_eligible") is not True
        or provenance.get("acceptance_blockers") != []
        or not isinstance(consumption, dict)
        or consumption.get("profile") != profile["qualification_profile"]
        or consumption.get("consumed_nodes") != profile["consumed_nodes"]
        or set((consumption.get("consumed_node_sha256") or {}))
        != set(profile["consumed_nodes"])
        or not isinstance(vector, dict)
        or vector.get("available") is not True
        or not isinstance(image, dict)
        or IMAGE.fullmatch(str(image.get("digest") or "")) is None
        or image.get("digest_source") != "docker_image_inspect_host"
    ):
        failures.append("capacity provenance/source/image/Q identity is not exact")
    if not isinstance(inherited, dict) or inherited.get("available") is not True:
        failures.append("capacity provenance does not bind the accepted M0 capability receipt")
    if not isinstance(config_hashes, dict) or any(
        config_hashes.get(path) != digest for path, digest in expected_configs.items()
    ):
        failures.append("capacity provenance does not bind all exact profile inputs")
    return failures


def validate_run(root: Path, run_dir: Path) -> dict[str, Any]:
    failures: list[str] = []
    gates: dict[str, dict[str, Any]] = {}
    metrics: dict[str, Any] = {}
    try:
        profile = load_profile(root / "network/config/flight_capacity_profile.json")
        provenance = strict_loads(
            read_regular(run_dir / "metrics/provenance.json", maximum=64 * 1024 * 1024),
            "provenance",
        )
        observation = strict_loads(
            read_regular(
                run_dir / "metrics/flight_capacity_observation.json",
                maximum=2 * 1024 * 1024,
            ),
            "capacity observation",
        )
        if not isinstance(provenance, dict) or not isinstance(observation, dict):
            raise ValueError("provenance/observation roots are not objects")
        runtime_id = observation.get("runtime_id")
        if not isinstance(runtime_id, str) or len(runtime_id) < 8:
            raise ValueError("observation runtime_id is invalid")
        events_path = run_dir / "logs/flight_capacity_events.jsonl"
        events = load_events(events_path, run_id=run_dir.name, runtime_id=runtime_id)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {
            "schema_version": 1,
            "contract": RESULT_CONTRACT,
            "run_id": run_dir.name,
            "passed": False,
            "failures": [f"capacity evidence cannot be parsed: {exc}"],
            "gates": {},
            "metrics": {},
        }

    identity_failures = validate_identity(root, run_dir, profile, provenance)
    gates["identity"] = {
        "passed": not identity_failures,
        "failures": identity_failures,
    }
    failures.extend(identity_failures)

    for gate_name, gate_function in (
        ("runtime_inputs", runtime_inputs_status),
        ("scene", scene_status),
    ):
        try:
            inherited_gate = gate_function(run_dir)
        except BaseException as exc:
            inherited_gate = {
                "status": "failed",
                "details": {"failures": [f"{type(exc).__name__}: {exc}"]},
            }
        inherited_failures = (
            inherited_gate.get("details", {}).get("failures", [])
            if isinstance(inherited_gate, dict)
            and isinstance(inherited_gate.get("details"), dict)
            else [f"{gate_name} returned a malformed result"]
        )
        if inherited_gate.get("status") != "passed" or inherited_failures != []:
            failures.append(f"capacity inherited {gate_name} gate did not pass")
        gates[gate_name] = {
            "passed": inherited_gate.get("status") == "passed"
            and inherited_failures == [],
            "failures": inherited_failures,
            "source_gate": inherited_gate,
        }

    observation_failures: list[str] = []
    expected_observation_keys = {
        "schema_version",
        "contract",
        "run_id",
        "runtime_id",
        "completed",
        "failures",
        "profile_path",
        "profile_sha256",
        "provenance_sha256",
        "event_log_path",
        "event_log_sha256",
        "event_count",
        "clock_sample_count",
        "resource_sample_count",
        "warmup_start_monotonic_ns",
        "warmup_end_monotonic_ns",
        "measurement_start_monotonic_ns",
        "measurement_end_monotonic_ns",
    }
    if (
        set(observation) != expected_observation_keys
        or observation.get("schema_version") != 1
        or observation.get("contract") != OBSERVATION_CONTRACT
        or observation.get("run_id") != run_dir.name
        or observation.get("completed") is not True
        or observation.get("failures") != []
        or observation.get("profile_path")
        != "network/config/flight_capacity_profile.json"
        or observation.get("profile_sha256")
        != sha256_file(root / "network/config/flight_capacity_profile.json")
        or observation.get("provenance_sha256")
        != sha256_file(run_dir / "metrics/provenance.json")
        or observation.get("event_log_path")
        != "logs/flight_capacity_events.jsonl"
        or observation.get("event_log_sha256")
        != sha256_file(run_dir / "logs/flight_capacity_events.jsonl")
        or observation.get("event_count") != len(events)
    ):
        observation_failures.append("producer observation does not exactly bind the raw files")
    gates["raw_binding"] = {
        "passed": not observation_failures,
        "failures": observation_failures,
    }
    failures.extend(observation_failures)

    schedule_failures: list[str] = []
    try:
        readiness = one_event(events, "readiness_complete")
        warmup_start = one_event(events, "warmup_start")
        warmup_end = one_event(events, "warmup_end")
        measurement_start = one_event(events, "measurement_start")
        measurement_end = one_event(events, "measurement_end")
        warmup_start_ns = warmup_start["host_monotonic_ns"]
        warmup_end_ns = warmup_end["host_monotonic_ns"]
        start_ns = measurement_start["host_monotonic_ns"]
        end_ns = measurement_end.get("target_monotonic_ns")
        if (
            not isinstance(readiness.get("stable_since_monotonic_ns"), int)
            or warmup_start.get("target_duration_ns") != 30_000_000_000
            or warmup_end.get("target_monotonic_ns")
            != warmup_start_ns + 30_000_000_000
            or not 30_000_000_000 <= warmup_end_ns - warmup_start_ns <= 30_250_000_000
            or measurement_start.get("target_end_monotonic_ns")
            != start_ns + 300_000_000_000
            or end_ns != start_ns + 300_000_000_000
            or measurement_end.get("clock_bracket_observed") is not True
            or observation.get("measurement_start_monotonic_ns") != start_ns
            or observation.get("measurement_end_monotonic_ns") != end_ns
        ):
            schedule_failures.append("warmup/measurement schedule is not exact 30+300 seconds")
    except (KeyError, TypeError, ValueError) as exc:
        schedule_failures.append(f"capacity schedule is incomplete: {exc}")
        start_ns = 0
        end_ns = 0
    gates["schedule"] = {
        "passed": not schedule_failures,
        "failures": schedule_failures,
    }
    failures.extend(schedule_failures)

    clock_failures: list[str] = []
    rtf_values: list[float] = []
    aggregate_rtf: float | None = None
    if not schedule_failures:
        try:
            clocks = [
                (item["host_monotonic_ns"], item["sim_time_ns"])
                for item in events
                if item.get("event") == "clock_sample"
            ]
            if (
                len(clocks) < 3000
                or any(
                    not isinstance(host, int)
                    or not isinstance(sim, int)
                    or host <= 0
                    or sim < 0
                    for host, sim in clocks
                )
                or any(
                    clocks[index][0] <= clocks[index - 1][0]
                    or clocks[index][1] < clocks[index - 1][1]
                    for index in range(1, len(clocks))
                )
            ):
                raise ValueError("clock stream is too sparse, malformed, or nonmonotonic")
            rtf_values = derive_rtf_windows(
                clocks,
                start_ns,
                window_count=profile["window_count"],
                window_ns=1_000_000_000,
                maximum_gap_ns=int(profile["clock_sample_max_gap_s"] * 1_000_000_000),
            )
            start_sim = interpolate_sim_ns(
                clocks,
                start_ns,
                maximum_gap_ns=250_000_000,
            )
            end_sim = interpolate_sim_ns(
                clocks,
                end_ns,
                maximum_gap_ns=250_000_000,
            )
            aggregate_rtf = (end_sim - start_sim) / (end_ns - start_ns)
        except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
            clock_failures.append(f"RTF cannot be independently derived: {exc}")
    passing_windows = sum(
        profile["rtf_min"] <= value <= profile["rtf_max"] for value in rtf_values
    )
    if (
        not clock_failures
        and (
            len(rtf_values) != profile["window_count"]
            or passing_windows < profile["window_success_minimum"]
            or aggregate_rtf is None
            or not profile["rtf_min"] <= aggregate_rtf <= profile["rtf_max"]
        )
    ):
        clock_failures.append("aggregate or per-second RTF thresholds did not pass")
    metrics.update(
        {
            "aggregate_realtime_factor": aggregate_rtf,
            "window_count": len(rtf_values),
            "passing_window_count": passing_windows,
            "minimum_window_rtf": min(rtf_values) if rtf_values else None,
            "maximum_window_rtf": max(rtf_values) if rtf_values else None,
        }
    )
    gates["realtime_factor"] = {
        "passed": not clock_failures,
        "failures": clock_failures,
    }
    failures.extend(clock_failures)

    resource_failures: list[str] = []
    resources = [
        item for item in events if item.get("event") == "measurement_resource_sample"
    ]
    resource_hosts = [item.get("host_monotonic_ns") for item in resources]
    if (
        len(resources) != 300
        or [item.get("sample_index") for item in resources] != list(range(300))
        or any(not isinstance(value, int) for value in resource_hosts)
        or any(
            resource_hosts[index] - resource_hosts[index - 1] > 1_500_000_000
            or resource_hosts[index] <= resource_hosts[index - 1]
            for index in range(1, len(resource_hosts))
        )
    ):
        resource_failures.append("resource samples are not one complete ordered 300-second series")
    for index, item in enumerate(resources):
        process = item.get("process_group")
        cgroup = item.get("cgroup")
        gpu = item.get("gpu")
        if (
            not isinstance(process, dict)
            or process.get("counts") != REQUIRED_PROCESS_COUNTS
            or process.get("required_counts") != REQUIRED_PROCESS_COUNTS
            or process.get("roles_exact") is not True
            or not isinstance(process.get("processes"), list)
            or process.get("process_count", 0) < 16
            or not isinstance(cgroup, dict)
            or not isinstance(cgroup.get("cpu_stat"), dict)
            or not isinstance(gpu, dict)
            or gpu.get("available") is not True
            or not isinstance(gpu.get("gpus"), list)
            or len(gpu["gpus"]) != 1
        ):
            resource_failures.append(f"resource/process/GPU sample {index} is incomplete")
            break
    collector_start = [item for item in events if item.get("event") == "collector_start"]
    if len(collector_start) != 1:
        resource_failures.append("collector static runtime identity is missing or duplicated")
    else:
        identity = collector_start[0].get("static_runtime_identity")
        if (
            not isinstance(identity, dict)
            or identity.get("cpu_count") is None
            or not identity.get("cpu_model")
            or not identity.get("cpu_online")
            or not isinstance(identity.get("governors"), dict)
            or not identity.get("clocksource")
            or identity.get("mitsuba_variant") != profile["required_mitsuba_variant"]
            or not isinstance(identity.get("gpu"), dict)
            or identity["gpu"].get("available") is not True
        ):
            resource_failures.append("static host/container/GPU/clock identity is incomplete")
    gates["resources"] = {
        "passed": not resource_failures,
        "failures": resource_failures,
    }
    failures.extend(resource_failures)

    continuity_failures: list[str] = []
    if not schedule_failures:
        measurement_odom = [
            item
            for item in events
            if item.get("event") == "odometry_sample"
            and start_ns <= item["host_monotonic_ns"] <= end_ns
        ]
        counts = {
            name: sum(item.get("uav") == name for item in measurement_odom)
            for name in profile["uav_names"]
        }
        if any(count < 1500 for count in counts.values()):
            continuity_failures.append("one or more UAV odometry streams lack 5 Hz capacity coverage")
        metrics["measurement_odometry_counts"] = counts
    gates["five_uav_continuity"] = {
        "passed": not continuity_failures,
        "failures": continuity_failures,
    }
    failures.extend(continuity_failures)

    return {
        "schema_version": 1,
        "contract": RESULT_CONTRACT,
        "run_id": run_dir.name,
        "passed": not failures,
        "failures": failures,
        "gates": gates,
        "metrics": metrics,
        "identity": {
            "profile_path": "network/config/flight_capacity_profile.json",
            "profile_sha256": sha256_file(
                root / "network/config/flight_capacity_profile.json"
            ),
            "provenance_sha256": sha256_file(run_dir / "metrics/provenance.json"),
            "event_log_sha256": sha256_file(
                run_dir / "logs/flight_capacity_events.jsonl"
            ),
            "observation_sha256": sha256_file(
                run_dir / "metrics/flight_capacity_observation.json"
            ),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)
    run_dir = args.run_dir.resolve()
    result = validate_run(ROOT_DIR, run_dir)
    encoded = json.dumps(result, allow_nan=False, indent=2, sort_keys=True) + "\n"
    if args.no_write:
        sys.stdout.write(encoded)
    else:
        output = run_dir / "metrics/flight_capacity_validation.json"
        if output.exists():
            raise SystemExit(f"refusing to replace existing validation result: {output}")
        with output.open("x", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        sys.stdout.write(encoded)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
