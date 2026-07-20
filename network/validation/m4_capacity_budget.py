#!/usr/bin/env python3
"""Independent execution-budget and ns-3 launch binding for M4 capacity.

The capacity contract is useful only if its 30+600 second flight interval can
finish before both the packet engine and the outer component wrapper expire.
This module deliberately re-derives that timing from frozen, human-reviewable
units and then binds the declaration to the repository runner and the live
packet-engine evidence.  It does not import the runtime orchestrator.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping

from network.ns3.tap_packet_engine_config import CONTRACT as ENGINE_CONTRACT
from network.ns3.tap_packet_engine_config import from_repository
from network.validation.m4_common import (
    M4ValidationError,
    exact_keys,
    regular_file,
    strict_json,
    strict_jsonl,
)


ROOT = Path(__file__).resolve().parents[2]
EXECUTION_BUDGET_CONTRACT = "ams.m4.capacity-execution-budget/v1"
STAGE_TIMING_BUDGET_CONTRACT = "ams.m4.capacity-flight-stage-budget/v1"

NS_PER_SECOND = 1_000_000_000
PRE_MEASUREMENT_STAGE_COUNT = 8
MAX_STAGE_EXECUTION_NS = 15 * NS_PER_SECOND
REUSED_COMMAND_GUARD_COUNT = 3
REUSED_COMMAND_GUARD_NS = 3 * NS_PER_SECOND
PREARM_STATE_WAIT_NS = 30 * NS_PER_SECOND
AIRBORNE_STATE_WAIT_NS = 60 * NS_PER_SECOND

READINESS_RUNWAY_NS = 720 * NS_PER_SECOND
DECLARED_SEQUENTIAL_READINESS_WAITS_NS = 415_500_000_000
BOUNDED_PREFLIGHT_NS = (
    PRE_MEASUREMENT_STAGE_COUNT * MAX_STAGE_EXECUTION_NS
    + REUSED_COMMAND_GUARD_COUNT * REUSED_COMMAND_GUARD_NS
    + PREARM_STATE_WAIT_NS
    + AIRBORNE_STATE_WAIT_NS
)
READINESS_RESERVE_NS = (
    READINESS_RUNWAY_NS
    - DECLARED_SEQUENTIAL_READINESS_WAITS_NS
    - BOUNDED_PREFLIGHT_NS
)

WARMUP_NS = 30 * NS_PER_SECOND
BOUNDED_WARMUP_MOTION_NS = 15 * NS_PER_SECOND
WARMUP_AFTER_MOTION_RESERVE_NS = WARMUP_NS - BOUNDED_WARMUP_MOTION_NS
MEASUREMENT_NS = 600 * NS_PER_SECOND
POST_MEASUREMENT_CONTROL_NS = 10 * NS_PER_SECOND
LANDING_STATE_NS = 120 * NS_PER_SECOND
DISARM_NS = 60 * NS_PER_SECOND
CONTRACT_TO_CLEAN_SHUTDOWN_NS = (
    READINESS_RUNWAY_NS
    + WARMUP_NS
    + MEASUREMENT_NS
    + POST_MEASUREMENT_CONTROL_NS
    + LANDING_STATE_NS
    + DISARM_NS
)

NS3_ENGINE_DURATION_NS = 1_600 * NS_PER_SECOND
NS3_UNALLOCATED_MARGIN_NS = NS3_ENGINE_DURATION_NS - CONTRACT_TO_CLEAN_SHUTDOWN_NS
WRAPPER_TIMEOUT_NS = 1_800 * NS_PER_SECOND
WRAPPER_PRECONTRACT_AND_FINALIZATION_RESERVE_NS = (
    WRAPPER_TIMEOUT_NS - CONTRACT_TO_CLEAN_SHUTDOWN_NS
)

EXPECTED_EXECUTION_BUDGET = {
    "contract": EXECUTION_BUDGET_CONTRACT,
    "readiness_runway_ns": READINESS_RUNWAY_NS,
    "declared_sequential_readiness_waits_ns": (
        DECLARED_SEQUENTIAL_READINESS_WAITS_NS
    ),
    "bounded_preflight_ns": BOUNDED_PREFLIGHT_NS,
    "readiness_reserve_ns": READINESS_RESERVE_NS,
    "warmup_ns": WARMUP_NS,
    "bounded_warmup_motion_ns": BOUNDED_WARMUP_MOTION_NS,
    "warmup_after_motion_reserve_ns": WARMUP_AFTER_MOTION_RESERVE_NS,
    "measurement_ns": MEASUREMENT_NS,
    "post_measurement_control_ns": POST_MEASUREMENT_CONTROL_NS,
    "landing_state_ns": LANDING_STATE_NS,
    "disarm_ns": DISARM_NS,
    "contract_to_clean_shutdown_bound_ns": CONTRACT_TO_CLEAN_SHUTDOWN_NS,
    "ns3_engine_duration_ns": NS3_ENGINE_DURATION_NS,
    "ns3_unallocated_margin_ns": NS3_UNALLOCATED_MARGIN_NS,
    "wrapper_timeout_ns": WRAPPER_TIMEOUT_NS,
    "wrapper_precontract_and_finalization_reserve_ns": (
        WRAPPER_PRECONTRACT_AND_FINALIZATION_RESERVE_NS
    ),
}
EXPECTED_STAGE_TIMING_BUDGET = {
    "contract": STAGE_TIMING_BUDGET_CONTRACT,
    "maximum_attempts_per_stage": 3,
    "outcome_timeout_ns": 3 * NS_PER_SECOND,
    "retry_quiet_drain_ns": 3 * NS_PER_SECOND,
    "per_stage_max_ns": MAX_STAGE_EXECUTION_NS,
    "pre_measurement_stage_count": PRE_MEASUREMENT_STAGE_COUNT,
    "pre_measurement_command_budget_ns": (
        PRE_MEASUREMENT_STAGE_COUNT * MAX_STAGE_EXECUTION_NS
    ),
    "preflight_reused_command_boundary_count": REUSED_COMMAND_GUARD_COUNT,
    "preflight_reused_command_boundary_budget_ns": (
        REUSED_COMMAND_GUARD_COUNT * REUSED_COMMAND_GUARD_NS
    ),
    "prearm_state_timeout_ns": PREARM_STATE_WAIT_NS,
    "airborne_state_timeout_ns": AIRBORNE_STATE_WAIT_NS,
    "bounded_preflight_ns": BOUNDED_PREFLIGHT_NS,
    "warmup_motion_stage_count": 1,
    "bounded_warmup_motion_ns": BOUNDED_WARMUP_MOTION_NS,
    "post_measurement_stage_count": 2,
    "post_measurement_reused_command_boundary_count": 1,
    "post_measurement_command_budget_ns": (
        2 * MAX_STAGE_EXECUTION_NS + REUSED_COMMAND_GUARD_NS
    ),
    "landing_state_timeout_ns": LANDING_STATE_NS,
    "disarm_state_timeout_ns": DISARM_NS,
}

PROFILE_PATH = ROOT / "network/config/component_acceptance_profiles.json"
RUNNER_PATH = ROOT / "network/scripts/run_m4_capacity.sh"
ENDPOINTS_PATH = ROOT / "network/config/endpoints.yaml"
RADIO_PATH = ROOT / "network/config/radio_24ghz.yaml"
RUNTIME_EVENTS_PATH = Path("logs/m4_runtime_events.jsonl")
ENGINE_CONFIG_PATH = Path("logs/ns3_packet_engine_config.json")
ENGINE_ARGV_PATH = Path("logs/ns3_packet_engine.argv")
ENGINE_READY_PATH = Path("raw/state/ns3-engine.ready.json")
ENGINE_STOP_PATH = Path("raw/control/ns3-engine.stop")

_CAPACITY_DURATION_ASSIGNMENT = re.compile(
    r"^[ \t]*(?:export[ \t]+)?CAPACITY_NS3_DURATION_MS=([^\r\n#]+?)[ \t]*$",
    re.MULTILINE,
)
_CAPACITY_DURATION_REFERENCE = re.compile(
    r"(?<![A-Za-z0-9_])DURATION_MS=\"\$CAPACITY_NS3_DURATION_MS\"(?![A-Za-z0-9_])"
)
_STACK_READINESS_DEADLINE = re.compile(
    r"^[ \t]*stack_deadline=\$\(\(SECONDS \+ ([0-9]+)\)\)[ \t]*$",
    re.MULTILINE,
)
_WAIT_FOR_FILES_CALL = re.compile(
    r"^[ \t]*wait_for_files[ \t]+([0-9]+)[ \t]+[^\r\n]+$", re.MULTILINE
)
_CAPTURE_STARTUP_SETTLE = re.compile(
    r"^sleep[ \t]+([0-9]+(?:\.[0-9]+)?)[ \t]*$", re.MULTILINE
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _positive_int(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value > 0


def expected_execution_budget() -> dict[str, Any]:
    """Return a fresh copy of the exact independently derived declaration."""

    return dict(EXPECTED_EXECUTION_BUDGET)


def execution_budget_derivation() -> dict[str, Any]:
    """Expose the arithmetic units used by the independent validator."""

    return {
        "pre_measurement_stage_count": PRE_MEASUREMENT_STAGE_COUNT,
        "maximum_stage_execution_ns": MAX_STAGE_EXECUTION_NS,
        "pre_measurement_stage_execution_ns": (
            PRE_MEASUREMENT_STAGE_COUNT * MAX_STAGE_EXECUTION_NS
        ),
        "reused_command_guard_count": REUSED_COMMAND_GUARD_COUNT,
        "reused_command_guard_ns": REUSED_COMMAND_GUARD_NS,
        "reused_command_guard_total_ns": (
            REUSED_COMMAND_GUARD_COUNT * REUSED_COMMAND_GUARD_NS
        ),
        "prearm_state_wait_ns": PREARM_STATE_WAIT_NS,
        "airborne_state_wait_ns": AIRBORNE_STATE_WAIT_NS,
        "bounded_preflight_ns": BOUNDED_PREFLIGHT_NS,
        "readiness_reserve_ns": READINESS_RESERVE_NS,
        "warmup_after_motion_reserve_ns": WARMUP_AFTER_MOTION_RESERVE_NS,
        "contract_to_clean_shutdown_bound_ns": CONTRACT_TO_CLEAN_SHUTDOWN_NS,
        "ns3_unallocated_margin_ns": NS3_UNALLOCATED_MARGIN_NS,
        "wrapper_precontract_and_finalization_reserve_ns": (
            WRAPPER_PRECONTRACT_AND_FINALIZATION_RESERVE_NS
        ),
    }


def _validate_budget_and_schedule(run: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    budget = run.get("execution_budget")
    failures.extend(
        exact_keys(
            budget,
            set(EXPECTED_EXECUTION_BUDGET),
            "M4 capacity execution budget",
        )
    )
    if budget != EXPECTED_EXECUTION_BUDGET:
        failures.append("M4 capacity execution budget differs")

    created = run.get("created_monotonic_ns")
    schedule = run.get("schedule")
    required_schedule = {
        "readiness_deadline_monotonic_ns",
        "warmup_start_monotonic_ns",
        "measurement_start_monotonic_ns",
        "measurement_end_monotonic_ns",
        "warmup_ns",
        "measurement_ns",
    }
    if not _positive_int(created) or not isinstance(schedule, Mapping):
        failures.append("M4 capacity budget cannot bind the run schedule")
        return failures
    if not required_schedule.issubset(schedule):
        failures.append("M4 capacity schedule lacks execution-budget boundaries")
        return failures

    expected_warmup_start = int(created) + READINESS_RUNWAY_NS
    expected_measurement_start = expected_warmup_start + WARMUP_NS
    expected_measurement_end = expected_measurement_start + MEASUREMENT_NS
    observed = {
        key: schedule.get(key)
        for key in required_schedule
    }
    if any(
        isinstance(value, bool) or not isinstance(value, int)
        for value in observed.values()
    ):
        failures.append("M4 capacity schedule budget fields are not exact integers")
        return failures
    if (
        schedule.get("readiness_deadline_monotonic_ns") != expected_warmup_start
        or schedule.get("warmup_start_monotonic_ns") != expected_warmup_start
        or schedule.get("measurement_start_monotonic_ns")
        != expected_measurement_start
        or schedule.get("measurement_end_monotonic_ns") != expected_measurement_end
        or schedule.get("warmup_ns") != WARMUP_NS
        or schedule.get("measurement_ns") != MEASUREMENT_NS
    ):
        failures.append("M4 capacity schedule differs from the 720+30+600 s budget")

    gate = run.get("airborne_gate")
    if not isinstance(gate, Mapping):
        failures.append("M4 capacity airborne gate is absent from budget binding")
    else:
        for key, expected in {
            "warmup_start_monotonic_ns": expected_warmup_start,
            "measurement_start_monotonic_ns": expected_measurement_start,
            "measurement_end_monotonic_ns": expected_measurement_end,
            "airborne_ready_deadline_monotonic_ns": expected_warmup_start,
        }.items():
            if gate.get(key) != expected:
                failures.append(
                    f"M4 capacity airborne gate boundary differs: {key}"
                )
        if gate.get("stage_timing_budget") != EXPECTED_STAGE_TIMING_BUDGET:
            failures.append("M4 capacity flight-stage timing budget differs")
    return failures


def _validate_repository_budget_sources(
    *, profiles_path: Path, runner_path: Path
) -> list[str]:
    failures: list[str] = []
    try:
        profiles = strict_json(profiles_path)
        profile_map = profiles.get("profiles")
        profile = (
            profile_map.get("m4_capacity_prerequisite")
            if isinstance(profile_map, Mapping)
            else None
        )
        if not isinstance(profile, dict):
            raise M4ValidationError("m4_capacity_prerequisite profile is absent")
        if (
            profile.get("runner") != "network/scripts/run_m4_capacity.sh"
            or isinstance(profile.get("timeout_s"), bool)
            or profile.get("timeout_s") != WRAPPER_TIMEOUT_NS // NS_PER_SECOND
        ):
            raise M4ValidationError(
                "m4_capacity_prerequisite wrapper timeout/runner differs"
            )
    except (OSError, TypeError, M4ValidationError) as exc:
        failures.append(f"M4 capacity component profile budget is invalid: {exc}")

    try:
        if not regular_file(runner_path):
            raise M4ValidationError("runner source is missing/nonregular/hardlinked")
        source = runner_path.read_text(encoding="utf-8")
        assignments = _CAPACITY_DURATION_ASSIGNMENT.findall(source)
        references = _CAPACITY_DURATION_REFERENCE.findall(source)
        if assignments != [str(NS3_ENGINE_DURATION_NS // 1_000_000)]:
            raise M4ValidationError(
                "runner must assign the exact 1600000-ms ns-3 duration once"
            )
        if len(references) != 1:
            raise M4ValidationError(
                "runner must inject its frozen ns-3 duration exactly once"
            )
        stack_waits = _STACK_READINESS_DEADLINE.findall(source)
        file_waits = _WAIT_FOR_FILES_CALL.findall(source)
        settle_waits = _CAPTURE_STARTUP_SETTLE.findall(source)
        if stack_waits != ["150"]:
            raise M4ValidationError("runner stack readiness wait differs")
        # The final five-second wait observes shutdown and is not readiness.
        if file_waits != ["10", "10", "30", "20", "20", "120", "45", "10", "5"]:
            raise M4ValidationError("runner readiness wait inventory differs")
        if settle_waits != ["0.5"]:
            raise M4ValidationError("runner capture readiness settle differs")
        derived_wait_ns = int(
            (
                int(stack_waits[0])
                + sum(int(value) for value in file_waits[:-1])
                + float(settle_waits[0])
            )
            * NS_PER_SECOND
        )
        if derived_wait_ns != DECLARED_SEQUENTIAL_READINESS_WAITS_NS:
            raise M4ValidationError(
                "runner readiness waits do not derive the declared budget"
            )
    except (OSError, UnicodeError, M4ValidationError) as exc:
        failures.append(f"M4 capacity runner duration source is invalid: {exc}")
    return failures


def _expected_engine_evidence(
    run_dir: Path,
    run: Mapping[str, Any],
    *,
    endpoints_path: Path,
    radio_path: Path,
) -> tuple[dict[str, Any], list[str], list[str], str]:
    runtime_id = run.get("runtime_id")
    if not isinstance(runtime_id, str) or not runtime_id:
        raise M4ValidationError("runtime_id is absent from engine configuration binding")
    state_path = run_dir / "logs/sionna_applied_states.jsonl"
    events_path = run_dir / "logs/ns3_packet_events.jsonl"
    pcap_prefix = run_dir / "pcap/ns3-packet-engine"
    clock_socket = f"/tmp/ams-m4-clock-{runtime_id}.sock"
    config = from_repository(
        uav_count=5,
        duration_ms=NS3_ENGINE_DURATION_NS // 1_000_000,
        seed=42,
        run=1,
        event_epoch=1,
        self_test=False,
        self_test_burst=1,
        self_test_unknown_tos=False,
        tap_gcs="tap-gcs",
        tap_uavs=tuple(f"tap-uav{index}" for index in range(1, 6)),
        sionna_ipc_enabled=True,
        sionna_state_file=str(state_path),
        sionna_poll_interval_ms=1,
        sionna_max_updates_per_poll=64,
        sionna_max_state_ttl_ms=2000,
        sionna_intervention="natural",
        clock_datagram_socket=clock_socket,
        endpoints_path=endpoints_path,
        radio_path=radio_path,
    )
    engine_argv = config.engine_argv(
        events_file=str(events_path), pcap_prefix=str(pcap_prefix)
    )
    report = {
        "contract": ENGINE_CONTRACT,
        "config_sha256": config.sha256(),
        "canonical_config": config.canonical_text(),
        "resolved": {**asdict(config), "tap_uavs": list(config.tap_uavs)},
        "engine_argv": engine_argv,
        "source_sha256": {
            str(endpoints_path): _sha256_file(endpoints_path),
            str(radio_path): _sha256_file(radio_path),
        },
    }
    engine_tail_argv = [
        *engine_argv,
        f"--readyFile={run_dir / ENGINE_READY_PATH}",
        f"--stopFile={run_dir / ENGINE_STOP_PATH}",
    ]
    return report, engine_argv, engine_tail_argv, config.sha256()


def _read_argv_file(path: Path) -> list[str]:
    if not regular_file(path):
        raise M4ValidationError(f"missing/nonregular/hardlinked argv file: {path}")
    payload = path.read_bytes()
    if (
        not payload
        or len(payload) > 65_536
        or not payload.endswith(b"\n")
        or b"\0" in payload
        or b"\r" in payload
    ):
        raise M4ValidationError("ns-3 argv evidence has invalid bounded framing")
    try:
        lines = payload.decode("utf-8").splitlines()
    except UnicodeError as exc:
        raise M4ValidationError("ns-3 argv evidence is not UTF-8") from exc
    if not lines or any(not line for line in lines):
        raise M4ValidationError("ns-3 argv evidence has an empty argument")
    return lines


def _validate_engine_artifacts(
    run_dir: Path,
    run: Mapping[str, Any],
    *,
    endpoints_path: Path,
    radio_path: Path,
) -> tuple[dict[str, Any], list[str], list[str]]:
    failures: list[str] = []
    details: dict[str, Any] = {}
    engine_tail_argv: list[str] = []
    try:
        expected, engine_argv, engine_tail_argv, config_sha256 = (
            _expected_engine_evidence(
                run_dir,
                run,
                endpoints_path=endpoints_path,
                radio_path=radio_path,
            )
        )
        observed = strict_json(run_dir / ENGINE_CONFIG_PATH)
        if observed != expected:
            raise M4ValidationError(
                "packet-engine report differs from independently rebuilt EngineConfig"
            )
        argv = _read_argv_file(run_dir / ENGINE_ARGV_PATH)
        if argv != engine_argv or argv != observed.get("engine_argv"):
            raise M4ValidationError(
                "packet-engine argv log differs from independently rebuilt argv"
            )
        ready = strict_json(run_dir / ENGINE_READY_PATH)
        expected_ready = {
            "status": "ready",
            "contract": ENGINE_CONTRACT,
            "config_sha256": config_sha256,
            "event_epoch": 1,
            "uav_count": 5,
        }
        if ready != expected_ready:
            raise M4ValidationError(
                "packet-engine readiness does not bind the rebuilt config hash"
            )
        details.update(
            {
                "config_sha256": config_sha256,
                "duration_ms": NS3_ENGINE_DURATION_NS // 1_000_000,
                "engine_argument_count": len(engine_argv),
                "ready_config_bound": True,
            }
        )
    except (OSError, TypeError, ValueError, M4ValidationError) as exc:
        failures.append(f"M4 capacity ns-3 engine evidence is invalid: {exc}")
    return details, failures, engine_tail_argv


def _validate_sampled_engine_process(
    run_dir: Path,
    run: Mapping[str, Any],
    engine_tail_argv: list[str],
) -> tuple[dict[str, Any], list[str]]:
    failures: list[str] = []
    details: dict[str, Any] = {}
    try:
        identity_document = run.get("identity")
        manifest = (
            identity_document.get("executable_manifest")
            if isinstance(identity_document, Mapping)
            else None
        )
        binary = (
            manifest.get("ns3_packet_engine")
            if isinstance(manifest, Mapping)
            else None
        )
        if not isinstance(binary, Mapping):
            raise M4ValidationError("ns-3 executable manifest entry is absent")
        binary_path = binary.get("path")
        binary_sha256 = binary.get("sha256")
        if (
            not isinstance(binary_path, str)
            or not Path(binary_path).is_absolute()
            or "\0" in binary_path
            or not isinstance(binary_sha256, str)
            or len(binary_sha256) != 64
            or any(character not in "0123456789abcdef" for character in binary_sha256)
            or not engine_tail_argv
        ):
            raise M4ValidationError("ns-3 executable/argv identity is incomplete")
        full_argv = [binary_path, *engine_tail_argv]
        raw_cmdline = b"\0".join(
            argument.encode("utf-8") for argument in full_argv
        ) + b"\0"
        expected_cmdline_sha256 = hashlib.sha256(raw_cmdline).hexdigest()

        events = strict_jsonl(
            run_dir / RUNTIME_EVENTS_PATH, max_line_bytes=2 * 1024 * 1024
        )
        samples = [
            event
            for event in events
            if event.get("event")
            in {"warmup_resource_sample", "measurement_resource_sample"}
        ]
        if not samples:
            raise M4ValidationError("no live warmup/measurement process sample exists")
        frozen_identity: tuple[Any, ...] | None = None
        for ordinal, sample in enumerate(samples, start=1):
            process_set = sample.get("processes")
            records = (
                process_set.get("processes")
                if isinstance(process_set, Mapping)
                else None
            )
            if not isinstance(records, list):
                raise M4ValidationError(
                    f"runtime process sample {ordinal} has no process records"
                )
            engines = [
                record
                for record in records
                if isinstance(record, Mapping)
                and record.get("role") == "ns3_packet_engine"
            ]
            if len(engines) != 1:
                raise M4ValidationError(
                    f"runtime process sample {ordinal} has ambiguous ns-3 identity"
                )
            engine = engines[0]
            identity = (
                engine.get("pid"),
                engine.get("start_ticks"),
                engine.get("pgid"),
                engine.get("executable_path"),
                engine.get("executable_sha256"),
                engine.get("cmdline_sha256"),
            )
            if (
                not _positive_int(engine.get("pid"))
                or not _positive_int(engine.get("start_ticks"))
                or not _positive_int(engine.get("pgid"))
                or engine.get("executable_path") != binary_path
                or engine.get("executable_sha256") != binary_sha256
                or engine.get("cmdline_sha256") != expected_cmdline_sha256
            ):
                raise M4ValidationError(
                    f"runtime process sample {ordinal} ns-3 cmdline/executable differs"
                )
            if frozen_identity is None:
                frozen_identity = identity
            elif identity != frozen_identity:
                raise M4ValidationError(
                    f"runtime process sample {ordinal} ns-3 identity changed"
                )
        details.update(
            {
                "sample_count": len(samples),
                "pid": frozen_identity[0] if frozen_identity else None,
                "start_ticks": frozen_identity[1] if frozen_identity else None,
                "cmdline_sha256": expected_cmdline_sha256,
                "cmdline_argument_count": len(full_argv),
            }
        )
    except (OSError, UnicodeError, TypeError, ValueError, M4ValidationError) as exc:
        failures.append(f"M4 capacity sampled ns-3 process is invalid: {exc}")
    return details, failures


def validate_capacity_execution_budget(
    run_dir: Path,
    run: Mapping[str, Any],
    *,
    profiles_path: Path = PROFILE_PATH,
    runner_path: Path = RUNNER_PATH,
    endpoints_path: Path = ENDPOINTS_PATH,
    radio_path: Path = RADIO_PATH,
    inspect_runtime_processes: bool = True,
) -> tuple[dict[str, Any], list[str]]:
    """Validate the declared timing and bind it to static and live evidence."""

    resolved_run_dir = run_dir.resolve()
    failures = _validate_budget_and_schedule(run)
    failures.extend(
        _validate_repository_budget_sources(
            profiles_path=profiles_path, runner_path=runner_path
        )
    )
    engine_details, engine_failures, engine_tail_argv = _validate_engine_artifacts(
        resolved_run_dir,
        run,
        endpoints_path=endpoints_path,
        radio_path=radio_path,
    )
    failures.extend(engine_failures)
    process_details: dict[str, Any] = {"inspected": False}
    if inspect_runtime_processes:
        process_details, process_failures = _validate_sampled_engine_process(
            resolved_run_dir, run, engine_tail_argv
        )
        process_details["inspected"] = True
        failures.extend(process_failures)

    details = {
        "execution_budget": expected_execution_budget(),
        "derivation": execution_budget_derivation(),
        "engine": engine_details,
        "sampled_engine_process": process_details,
    }
    return details, failures


__all__ = [
    "BOUNDED_PREFLIGHT_NS",
    "CONTRACT_TO_CLEAN_SHUTDOWN_NS",
    "DECLARED_SEQUENTIAL_READINESS_WAITS_NS",
    "EXPECTED_EXECUTION_BUDGET",
    "NS3_ENGINE_DURATION_NS",
    "READINESS_RESERVE_NS",
    "READINESS_RUNWAY_NS",
    "WRAPPER_TIMEOUT_NS",
    "execution_budget_derivation",
    "expected_execution_budget",
    "validate_capacity_execution_budget",
]
