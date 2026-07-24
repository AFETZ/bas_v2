#!/usr/bin/env python3
"""Capacity-only five-vehicle flight control over the accepted MAVLink path.

The controller is deliberately a helper, not a second UDP endpoint.  It is
owned by :mod:`actual_sitl_control_probe` and therefore shares that probe's
sole ``10.71.0.10:14600`` socket, MAVLink sequence, parser, and append-only
audit writer.  Every flight command is one UDP datagram containing an actual
``COMMAND_LONG`` or ``COMMAND_INT`` frame followed by a token-bearing
``TIMESYNC`` frame.

This module never claims that a vehicle is airborne.  It records raw command,
ACK, TIMESYNC, and vehicle-state facts; the independent M4 capacity validator
combines those facts with the separately produced Gazebo odometry snapshots.
"""

from __future__ import annotations

import hashlib
import math
import struct
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from network.validation.m4_airborne_motion import motion_requirements


AIRBORNE_GATE_CONTRACT = "ams.m4.capacity-airborne-gate/v1"
EXPECTED_UAVS = tuple(range(1, 6))
TOS_CONTROL = 184
OUTCOME_TIMEOUT_NS = 3_000_000_000
HEARTBEAT_FRESHNESS_NS = 3_000_000_000
HIGH_RATE_STATE_FRESHNESS_NS = 1_000_000_000
POSE_FRESHNESS_NS = 1_000_000_000
TARGET_RELATIVE_ALT_M = 20.0
MOTION_TARGET_RELATIVE_ALT_M = 80.0
MOTION_CLIMB_SPEED_MPS = 1.0
MINIMUM_RELATIVE_ALT_M = 10.0
MINIMUM_GAZEBO_RISE_M = 8.0
MINIMUM_SEPARATION_M = 20.0
PREARM_ALTITUDE_TOLERANCE_M = 2.0
LANDING_TIMEOUT_NS = 120_000_000_000
DISARM_TIMEOUT_NS = 60_000_000_000
POST_MEASUREMENT_CONTROL_NS = 10_000_000_000
AIRBORNE_TIMEOUT_NS = 60_000_000_000
PREARM_STATE_TIMEOUT_NS = 30_000_000_000
MAX_COMMAND_ATTEMPTS = 4
# The first explicit stream command is issued immediately after the passive
# five-heartbeat gate.  It can race the first Sionna state application, then
# needs to survive natural uplink/ACK/TIMESYNC loss.  Give only that bootstrap
# command two additional bounded, token-distinct attempts; later stream and
# flight commands retain the ordinary four-attempt bound.
EXTENDED_SYS_STATE_MAX_COMMAND_ATTEMPTS = 6
PER_STAGE_MAX_NS = (
    MAX_COMMAND_ATTEMPTS * OUTCOME_TIMEOUT_NS
    + (MAX_COMMAND_ATTEMPTS - 1) * OUTCOME_TIMEOUT_NS
)
STAGE_TIMING_BUDGET_CONTRACT = "ams.m4.capacity-flight-stage-budget/v1"

MAV_MODE_FLAG_CUSTOM_MODE_ENABLED = 1
MAV_MODE_FLAG_SAFETY_ARMED = 128
COPTER_MODE_GUIDED = 4
MAV_LANDED_STATE_ON_GROUND = 1
MAV_LANDED_STATE_IN_AIR = 2

MAV_CMD_NAV_LAND = 21
MAV_CMD_NAV_TAKEOFF = 22
MAV_CMD_DO_SET_MODE = 176
MAV_CMD_DO_CHANGE_SPEED = 178
MAV_CMD_DO_REPOSITION = 192
MAV_CMD_COMPONENT_ARM_DISARM = 400
MAV_CMD_SET_MESSAGE_INTERVAL = 511
MAV_RESULT_ACCEPTED = 0
MAV_RESULT_TEMPORARILY_REJECTED = 1
MSG_ID_GLOBAL_POSITION_INT = 33
MSG_ID_LOCAL_POSITION_NED = 32
MSG_ID_EXTENDED_SYS_STATE = 245
MSG_ID_HEARTBEAT = 0
MAV_FRAME_GLOBAL_RELATIVE_ALT_INT = 6

# Five bits are reserved in the exact signed-63 token layout.  Keep these
# capacity-only codes disjoint from the workload/causality phase codes.
STAGE_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {
        "stage": "stream_extended_sys_state",
        "stage_code": 8,
        "command_id": MAV_CMD_SET_MESSAGE_INTERVAL,
        "encoding": "COMMAND_LONG",
        "params": (float(MSG_ID_EXTENDED_SYS_STATE), 200_000.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        "timing": "pre_measurement",
    },
    {
        "stage": "stream_global_position_int",
        "stage_code": 9,
        "command_id": MAV_CMD_SET_MESSAGE_INTERVAL,
        "encoding": "COMMAND_LONG",
        "params": (float(MSG_ID_GLOBAL_POSITION_INT), 200_000.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        "timing": "pre_measurement",
    },
    {
        "stage": "stream_local_position_ned",
        "stage_code": 10,
        "command_id": MAV_CMD_SET_MESSAGE_INTERVAL,
        "encoding": "COMMAND_LONG",
        "params": (float(MSG_ID_LOCAL_POSITION_NED), 200_000.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        "timing": "pre_measurement",
    },
    {
        "stage": "stream_heartbeat",
        "stage_code": 11,
        "command_id": MAV_CMD_SET_MESSAGE_INTERVAL,
        "encoding": "COMMAND_LONG",
        "params": (float(MSG_ID_HEARTBEAT), 200_000.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        "timing": "pre_measurement",
    },
    {
        "stage": "guided",
        "stage_code": 12,
        "command_id": MAV_CMD_DO_SET_MODE,
        "encoding": "COMMAND_LONG",
        "params": (
            float(MAV_MODE_FLAG_CUSTOM_MODE_ENABLED),
            float(COPTER_MODE_GUIDED),
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
        ),
        "timing": "pre_measurement",
    },
    {
        "stage": "arm",
        "stage_code": 13,
        "command_id": MAV_CMD_COMPONENT_ARM_DISARM,
        "encoding": "COMMAND_LONG",
        "params": (1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        "timing": "pre_measurement",
    },
    {
        "stage": "takeoff",
        "stage_code": 14,
        "command_id": MAV_CMD_NAV_TAKEOFF,
        "encoding": "COMMAND_LONG",
        "params": (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, TARGET_RELATIVE_ALT_M),
        "timing": "pre_measurement",
    },
    {
        "stage": "climb_speed",
        "stage_code": 15,
        "command_id": MAV_CMD_DO_CHANGE_SPEED,
        "encoding": "COMMAND_LONG",
        "params": (2.0, MOTION_CLIMB_SPEED_MPS, -1.0, 0.0, 0.0, 0.0, 0.0),
        "timing": "pre_measurement",
    },
    {
        "stage": "reposition",
        "stage_code": 16,
        "command_id": MAV_CMD_DO_REPOSITION,
        "encoding": "COMMAND_INT",
        "frame": MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
        "params": (0.0, 0.0, 0.0, 0.0),
        "target_relative_alt_m": MOTION_TARGET_RELATIVE_ALT_M,
        "timing": "warmup_motion",
    },
    {
        "stage": "land",
        "stage_code": 17,
        "command_id": MAV_CMD_NAV_LAND,
        "encoding": "COMMAND_LONG",
        "params": (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        "timing": "post_measurement",
    },
    {
        "stage": "disarm",
        "stage_code": 18,
        "command_id": MAV_CMD_COMPONENT_ARM_DISARM,
        "encoding": "COMMAND_LONG",
        "params": (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        "timing": "post_measurement",
    },
)
STAGE_BY_NAME = {str(item["stage"]): item for item in STAGE_DEFINITIONS}
PRE_MEASUREMENT_STAGES = tuple(
    str(item["stage"])
    for item in STAGE_DEFINITIONS
    if item["timing"] == "pre_measurement"
)
POST_MEASUREMENT_STAGES = tuple(
    str(item["stage"])
    for item in STAGE_DEFINITIONS
    if item["timing"] == "post_measurement"
)
WARMUP_MOTION_STAGES = tuple(
    str(item["stage"])
    for item in STAGE_DEFINITIONS
    if item["timing"] == "warmup_motion"
)


def maximum_attempts_for_stage(stage: str) -> int:
    """Return the declared bounded retry count for one flight stage."""

    if stage not in STAGE_BY_NAME:
        raise AirborneControlError("capacity flight stage is unknown")
    if stage == "stream_extended_sys_state":
        return EXTENDED_SYS_STATE_MAX_COMMAND_ATTEMPTS
    return MAX_COMMAND_ATTEMPTS


def stage_execution_budget_ns(stage: str) -> int:
    """Return the outcome/retry-drain bound for one declared stage."""

    attempts = maximum_attempts_for_stage(stage)
    return (
        attempts * OUTCOME_TIMEOUT_NS
        + (attempts - 1) * OUTCOME_TIMEOUT_NS
    )


def stage_timing_budget() -> dict[str, Any]:
    """Return the exact worst-case command/state budget used by orchestration."""

    repeated_pre_command_boundaries = sum(
        1
        for previous, following in zip(
            PRE_MEASUREMENT_STAGES, PRE_MEASUREMENT_STAGES[1:]
        )
        if STAGE_BY_NAME[previous]["command_id"]
        == STAGE_BY_NAME[following]["command_id"]
    )
    seen_command_ids = {
        int(STAGE_BY_NAME[stage]["command_id"])
        for stage in (*PRE_MEASUREMENT_STAGES, *WARMUP_MOTION_STAGES)
    }
    repeated_post_command_boundaries = 0
    for stage in POST_MEASUREMENT_STAGES:
        command_id = int(STAGE_BY_NAME[stage]["command_id"])
        if command_id in seen_command_ids:
            repeated_post_command_boundaries += 1
        seen_command_ids.add(command_id)
    extended_sys_state_max_ns = stage_execution_budget_ns(
        "stream_extended_sys_state"
    )
    pre_command_ns = sum(
        stage_execution_budget_ns(stage) for stage in PRE_MEASUREMENT_STAGES
    )
    boundary_ns = repeated_pre_command_boundaries * OUTCOME_TIMEOUT_NS
    bounded_preflight_ns = (
        pre_command_ns
        + boundary_ns
        + PREARM_STATE_TIMEOUT_NS
        + AIRBORNE_TIMEOUT_NS
    )
    return {
        "contract": STAGE_TIMING_BUDGET_CONTRACT,
        "maximum_attempts_per_stage": MAX_COMMAND_ATTEMPTS,
        "extended_sys_state_max_attempts": (
            EXTENDED_SYS_STATE_MAX_COMMAND_ATTEMPTS
        ),
        "outcome_timeout_ns": OUTCOME_TIMEOUT_NS,
        "retry_quiet_drain_ns": OUTCOME_TIMEOUT_NS,
        "per_stage_max_ns": PER_STAGE_MAX_NS,
        "extended_sys_state_max_ns": extended_sys_state_max_ns,
        "pre_measurement_stage_count": len(PRE_MEASUREMENT_STAGES),
        "pre_measurement_command_budget_ns": pre_command_ns,
        "preflight_reused_command_boundary_count": repeated_pre_command_boundaries,
        "preflight_reused_command_boundary_budget_ns": boundary_ns,
        "prearm_state_timeout_ns": PREARM_STATE_TIMEOUT_NS,
        "airborne_state_timeout_ns": AIRBORNE_TIMEOUT_NS,
        "bounded_preflight_ns": bounded_preflight_ns,
        "warmup_motion_stage_count": len(WARMUP_MOTION_STAGES),
        "bounded_warmup_motion_ns": sum(
            stage_execution_budget_ns(stage) for stage in WARMUP_MOTION_STAGES
        ),
        "post_measurement_stage_count": len(POST_MEASUREMENT_STAGES),
        "post_measurement_reused_command_boundary_count": (
            repeated_post_command_boundaries
        ),
        "post_measurement_command_budget_ns": (
            sum(
                stage_execution_budget_ns(stage)
                for stage in POST_MEASUREMENT_STAGES
            )
            + repeated_post_command_boundaries * OUTCOME_TIMEOUT_NS
        ),
        "landing_state_timeout_ns": LANDING_TIMEOUT_NS,
        "disarm_state_timeout_ns": DISARM_TIMEOUT_NS,
    }


class AirborneControlError(RuntimeError):
    """The capacity flight path cannot produce unambiguous evidence."""


class Sequencer(Protocol):
    def frame(self, message_id: int, payload: bytes) -> bytes: ...


class DatagramSocket(Protocol):
    def sendto(self, payload: bytes, destination: tuple[str, int]) -> int: ...


class AuditWriter(Protocol):
    def emit(self, event: str, **fields: Any) -> None: ...


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def flight_timesync_token(
    *, run_nonce: str, stage_code: int, uav: int, ordinal: int = 1
) -> int:
    """Return an injective positive signed-63 token within one capacity run."""

    if (
        len(run_nonce) != 64
        or any(character not in "0123456789abcdef" for character in run_nonce)
        or isinstance(stage_code, bool)
        or not isinstance(stage_code, int)
        or not 8 <= stage_code <= 31
        or isinstance(uav, bool)
        or uav not in EXPECTED_UAVS
        or isinstance(ordinal, bool)
        or not isinstance(ordinal, int)
        or not 1 <= ordinal <= 0xFFFF
    ):
        raise AirborneControlError("capacity flight TIMESYNC identity is invalid")
    # Exact non-overlapping layout: 39-bit run prefix, 5-bit stage, 3-bit UAV,
    # and 16-bit attempt ordinal.  Bit 63 stays clear for MAVLink int64_t.
    run_prefix = int.from_bytes(
        hashlib.sha256(bytes.fromhex(run_nonce)).digest()[:5], "big"
    ) >> 1
    token = (run_prefix << 24) | (stage_code << 19) | (uav << 16) | ordinal
    if not 0 < token < (1 << 63):
        raise AirborneControlError("capacity flight TIMESYNC token is outside signed-63")
    return token


def encode_flight_command_datagram(
    *,
    run_nonce: str,
    stage: str,
    uav: int,
    attempt: int = 1,
    sequencer: Sequencer,
    current_lat_e7: int | None = None,
    current_lon_e7: int | None = None,
) -> dict[str, Any]:
    """Encode one exact flight-command plus TIMESYNC capacity datagram."""

    definition = STAGE_BY_NAME.get(stage)
    if definition is None or uav not in EXPECTED_UAVS:
        raise AirborneControlError("capacity flight command identity is invalid")
    params = definition["params"]
    encoding = str(definition["encoding"])
    if encoding == "COMMAND_LONG":
        if not isinstance(params, tuple) or len(params) != 7:
            raise AirborneControlError("capacity COMMAND_LONG parameters differ")
        if current_lat_e7 is not None or current_lon_e7 is not None:
            raise AirborneControlError("COMMAND_LONG forbids reposition coordinates")
        command_message_id = 76
        command_frame = sequencer.frame(
            command_message_id,
            struct.pack(
                "<7fHBBB",
                *params,
                int(definition["command_id"]),
                uav,
                1,
                attempt - 1,
            ),
        )
        command_fields: dict[str, Any] = {
            "command_params": list(params),
            "command_int_frame": None,
            "command_int_x_e7": None,
            "command_int_y_e7": None,
            "command_int_z_m": None,
        }
    elif encoding == "COMMAND_INT":
        if (
            not isinstance(params, tuple)
            or len(params) != 4
            or isinstance(current_lat_e7, bool)
            or not isinstance(current_lat_e7, int)
            or not -(1 << 31) <= current_lat_e7 < (1 << 31)
            or isinstance(current_lon_e7, bool)
            or not isinstance(current_lon_e7, int)
            or not -(1 << 31) <= current_lon_e7 < (1 << 31)
        ):
            raise AirborneControlError("capacity COMMAND_INT coordinates differ")
        z_m = float(definition["target_relative_alt_m"])
        frame = int(definition["frame"])
        if not math.isfinite(z_m) or z_m <= TARGET_RELATIVE_ALT_M:
            raise AirborneControlError("capacity COMMAND_INT altitude differs")
        command_message_id = 75
        command_frame = sequencer.frame(
            command_message_id,
            struct.pack(
                "<4fiifHBBBBB",
                *params,
                current_lat_e7,
                current_lon_e7,
                z_m,
                int(definition["command_id"]),
                uav,
                1,
                frame,
                0,
                0,
            ),
        )
        command_fields = {
            "command_params": list(params),
            "command_int_frame": frame,
            "command_int_x_e7": current_lat_e7,
            "command_int_y_e7": current_lon_e7,
            "command_int_z_m": z_m,
        }
    else:
        raise AirborneControlError("capacity flight command encoding differs")
    token = flight_timesync_token(
        run_nonce=run_nonce,
        stage_code=int(definition["stage_code"]),
        uav=uav,
        ordinal=attempt,
    )
    timesync_frame = sequencer.frame(111, struct.pack("<qq", 0, token))
    datagram = command_frame + timesync_frame
    return {
        "stage": stage,
        "stage_code": int(definition["stage_code"]),
        "command_id": int(definition["command_id"]),
        "command_encoding": encoding,
        "command_message_id": command_message_id,
        **command_fields,
        "timesync_request_tc1": 0,
        "timesync_request_ts1": token,
        "command_frame": command_frame,
        "command_frame_sha256": sha256_bytes(command_frame),
        "timesync_frame": timesync_frame,
        "timesync_frame_sha256": sha256_bytes(timesync_frame),
        "request_datagram": datagram,
        "request_datagram_sha256": sha256_bytes(datagram),
    }


def airborne_gate_contract(schedule: Mapping[str, Any]) -> dict[str, Any]:
    """Return the immutable capacity flight gate derived from the run schedule."""

    try:
        warmup_start_ns = int(schedule["warmup_start_monotonic_ns"])
        measurement_start_ns = int(schedule["measurement_start_monotonic_ns"])
        measurement_end_ns = int(schedule["measurement_end_monotonic_ns"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AirborneControlError("capacity schedule lacks flight boundaries") from exc
    if not (
        0 < warmup_start_ns < measurement_start_ns < measurement_end_ns
        and measurement_start_ns - warmup_start_ns == 30_000_000_000
        and measurement_end_ns - measurement_start_ns == 600_000_000_000
    ):
        raise AirborneControlError("capacity flight schedule is not exact 30+600 s")
    return {
        "contract": AIRBORNE_GATE_CONTRACT,
        "uav_system_ids": list(EXPECTED_UAVS),
        "pre_measurement_stages": list(PRE_MEASUREMENT_STAGES),
        "warmup_motion_stages": list(WARMUP_MOTION_STAGES),
        "post_measurement_stages": list(POST_MEASUREMENT_STAGES),
        "warmup_start_monotonic_ns": warmup_start_ns,
        "measurement_start_monotonic_ns": measurement_start_ns,
        "measurement_end_monotonic_ns": measurement_end_ns,
        "airborne_ready_deadline_monotonic_ns": warmup_start_ns,
        "warmup_motion_deadline_monotonic_ns": warmup_start_ns
        + len(WARMUP_MOTION_STAGES) * PER_STAGE_MAX_NS,
        "landing_deadline_monotonic_ns": measurement_end_ns
        + POST_MEASUREMENT_CONTROL_NS
        + LANDING_TIMEOUT_NS,
        "disarm_deadline_monotonic_ns": measurement_end_ns
        + POST_MEASUREMENT_CONTROL_NS
        + LANDING_TIMEOUT_NS
        + DISARM_TIMEOUT_NS,
        "target_relative_alt_m": TARGET_RELATIVE_ALT_M,
        "minimum_relative_alt_m": MINIMUM_RELATIVE_ALT_M,
        "minimum_gazebo_rise_m": MINIMUM_GAZEBO_RISE_M,
        "minimum_pair_separation_m": MINIMUM_SEPARATION_M,
        "maximum_heartbeat_age_ns": HEARTBEAT_FRESHNESS_NS,
        "maximum_high_rate_state_age_ns": HIGH_RATE_STATE_FRESHNESS_NS,
        "maximum_pose_age_ns": POSE_FRESHNESS_NS,
        "command_outcome_timeout_ns": OUTCOME_TIMEOUT_NS,
        "pre_measurement_max_attempts": MAX_COMMAND_ATTEMPTS,
        "extended_sys_state_max_attempts": (
            EXTENDED_SYS_STATE_MAX_COMMAND_ATTEMPTS
        ),
        # LAND and DISARM are idempotent ArduPilot commands.  They use the
        # same bounded, token-distinct retry policy as preparation so a lost
        # ACK/TIMESYNC cannot strand armed vehicles after measurement.
        "post_measurement_max_attempts": MAX_COMMAND_ATTEMPTS,
        "warmup_motion_max_attempts": MAX_COMMAND_ATTEMPTS,
        "retry_quiet_drain_ns": OUTCOME_TIMEOUT_NS,
        "post_measurement_control_ns": POST_MEASUREMENT_CONTROL_NS,
        "landing_state_timeout_ns": LANDING_TIMEOUT_NS,
        "disarm_state_timeout_ns": DISARM_TIMEOUT_NS,
        "stage_timing_budget": stage_timing_budget(),
        "motion_requirements": motion_requirements(),
        "flight_command_datagram": "(COMMAND_LONG|COMMAND_INT)||TIMESYNC",
        "timesync_echo_contract": "response.ts1==request.ts1_token",
        "control_source": {
            "host": "10.71.0.10",
            "port": 14600,
            "tos": TOS_CONTROL,
        },
        "control_destinations": {
            f"uav{uav}": {"host": f"10.71.{uav}.10", "port": 14600 + uav}
            for uav in EXPECTED_UAVS
        },
        "vehicle_state_requirements": {
            "heartbeat_armed_flag": MAV_MODE_FLAG_SAFETY_ARMED,
            "heartbeat_custom_mode": COPTER_MODE_GUIDED,
            "extended_sys_state_landed_state": MAV_LANDED_STATE_IN_AIR,
            "global_position_relative_alt_min_mm": int(
                MINIMUM_RELATIVE_ALT_M * 1000
            ),
            "local_position_z_down_max_m": -MINIMUM_RELATIVE_ALT_M,
            "prearm_relative_alt_abs_max_mm": int(
                PREARM_ALTITUDE_TOLERANCE_M * 1000
            ),
            "prearm_local_z_abs_max_m": PREARM_ALTITUDE_TOLERANCE_M,
        },
        "gazebo_ground_z_m": {
            "uav1": 44.25,
            "uav2": 84.25,
            "uav3": 144.25,
            "uav4": 104.25,
            "uav5": 60.25,
        },
    }


@dataclass
class PendingFlightCommand:
    stage: str
    stage_code: int
    command_id: int
    uav: int
    transaction_id: str
    sent_monotonic_ns: int
    request_datagram_sha256: str
    command_frame_sha256: str
    timesync_frame_sha256: str
    timesync_token: int
    attempt: int
    ack: dict[str, Any] | None = None
    timesync: dict[str, Any] | None = None


class CapacityAirborneController:
    """Stateful capacity-only controller owned by the actual control probe."""

    def __init__(
        self,
        *,
        run_nonce: str,
        gate: Mapping[str, Any],
        sock: DatagramSocket,
        sequencer: Sequencer,
        writer: AuditWriter,
        pump: Callable[[float], None],
        now_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        expected_gate = airborne_gate_contract(gate)
        if dict(gate) != expected_gate:
            raise AirborneControlError("capacity airborne gate differs")
        self.run_nonce = run_nonce
        self.gate = expected_gate
        self.sock = sock
        self.sequencer = sequencer
        self.writer = writer
        self.pump = pump
        self.now_ns = now_ns
        self.pending_by_uav: dict[int, PendingFlightCommand] = {}
        self.pending_by_token: dict[tuple[int, int], PendingFlightCommand] = {}
        self.retired_tokens: dict[tuple[int, int], dict[str, Any]] = {}
        self.latest_heartbeat: dict[int, dict[str, Any]] = {}
        self.latest_extended_state: dict[int, dict[str, Any]] = {}
        self.latest_global_position: dict[int, dict[str, Any]] = {}
        self.latest_local_position: dict[int, dict[str, Any]] = {}
        self.started = False
        self.measurement_started = False
        self.measurement_ended = False
        self.airborne_ready_confirmed = False
        self.warmup_motion_started = False
        self.attempt_outcomes: dict[int, str] = {}
        self.quiet_command_guards: dict[int, tuple[int, int]] = {}
        self.quiet_last_response_ns = 0
        self.completed_stage_command_ids: set[int] = set()

    def _send_stage(self, stage: str) -> None:
        if stage in PRE_MEASUREMENT_STAGES:
            attempt_key = "pre_measurement_max_attempts"
        elif stage in WARMUP_MOTION_STAGES:
            attempt_key = "warmup_motion_max_attempts"
        elif stage in POST_MEASUREMENT_STAGES:
            attempt_key = "post_measurement_max_attempts"
        else:
            raise AirborneControlError("capacity flight stage timing class differs")
        maximum_attempts = int(
            self.gate[
                "extended_sys_state_max_attempts"
                if stage == "stream_extended_sys_state"
                else attempt_key
            ]
        )
        remaining = set(EXPECTED_UAVS)
        for attempt in range(1, maximum_attempts + 1):
            remaining = self._send_stage_attempt(stage, remaining, attempt)
            if not remaining:
                return
            if attempt < maximum_attempts:
                self._quiet_drain(
                    command_id=int(STAGE_BY_NAME[stage]["command_id"]),
                    uavs=remaining,
                    reason="bounded_retry",
                )
        raise AirborneControlError(
            f"capacity flight {stage} exhausted bounded attempts for {sorted(remaining)}"
        )

    def _send_stage_with_reuse_guard(self, stage: str) -> None:
        """Send a stage only after draining every reused COMMAND_ACK identity."""

        command_id = int(STAGE_BY_NAME[stage]["command_id"])
        if command_id in self.completed_stage_command_ids:
            self._quiet_drain(
                command_id=command_id,
                uavs=set(EXPECTED_UAVS),
                reason="same_command_id_stage_boundary",
            )
        self._send_stage(stage)
        self.completed_stage_command_ids.add(command_id)

    def _send_stage_attempt(
        self, stage: str, uavs: set[int], attempt: int
    ) -> set[int]:
        if self.pending_by_uav or self.pending_by_token:
            raise AirborneControlError("capacity flight stage overlaps pending commands")
        definition = STAGE_BY_NAME[stage]
        self.attempt_outcomes = {}
        if not uavs or not uavs <= set(EXPECTED_UAVS):
            raise AirborneControlError("capacity flight retry UAV set differs")
        for uav in sorted(uavs):
            sent_ns = self.now_ns()
            current_lat_e7: int | None = None
            current_lon_e7: int | None = None
            if stage in WARMUP_MOTION_STAGES:
                position = self.latest_global_position.get(uav)
                if not self._state_fresh(
                    position, sent_ns, HIGH_RATE_STATE_FRESHNESS_NS
                ):
                    raise AirborneControlError(
                        f"uav{uav} lacks fresh GLOBAL_POSITION_INT for reposition"
                    )
                assert position is not None
                current_lat_e7 = position.get("lat_e7")
                current_lon_e7 = position.get("lon_e7")
            encoded = encode_flight_command_datagram(
                run_nonce=self.run_nonce,
                stage=stage,
                uav=uav,
                attempt=attempt,
                sequencer=self.sequencer,
                current_lat_e7=current_lat_e7,
                current_lon_e7=current_lon_e7,
            )
            destination = (f"10.71.{uav}.10", 14600 + uav)
            size = self.sock.sendto(encoded["request_datagram"], destination)
            if size != len(encoded["request_datagram"]):
                raise AirborneControlError("short capacity flight command datagram send")
            transaction_id = (
                f"m4-capacity-flight:{stage}:uav{uav}:attempt{attempt}"
            )
            pending = PendingFlightCommand(
                stage=stage,
                stage_code=int(definition["stage_code"]),
                command_id=int(definition["command_id"]),
                uav=uav,
                transaction_id=transaction_id,
                sent_monotonic_ns=sent_ns,
                request_datagram_sha256=encoded["request_datagram_sha256"],
                command_frame_sha256=encoded["command_frame_sha256"],
                timesync_frame_sha256=encoded["timesync_frame_sha256"],
                timesync_token=encoded["timesync_request_ts1"],
                attempt=attempt,
            )
            key = (uav, pending.timesync_token)
            if uav in self.pending_by_uav or key in self.pending_by_token:
                raise AirborneControlError("capacity flight transaction identity reused")
            self.pending_by_uav[uav] = pending
            self.pending_by_token[key] = pending
            self.writer.emit(
                "flight_command_offered",
                flight_stage=stage,
                flight_stage_code=pending.stage_code,
                transaction_id=transaction_id,
                attempt=attempt,
                uav=uav,
                command_id=pending.command_id,
                command_encoding=encoded["command_encoding"],
                command_message_id=encoded["command_message_id"],
                command_params=encoded["command_params"],
                command_int_frame=encoded["command_int_frame"],
                command_int_x_e7=encoded["command_int_x_e7"],
                command_int_y_e7=encoded["command_int_y_e7"],
                command_int_z_m=encoded["command_int_z_m"],
                target_system=uav,
                target_component=1,
                source_ip="10.71.0.10",
                source_udp_port=14600,
                destination_ip=destination[0],
                destination_udp_port=destination[1],
                tos=TOS_CONTROL,
                sent_monotonic_ns=sent_ns,
                command_frame_hex=encoded["command_frame"].hex(),
                command_frame_sha256=pending.command_frame_sha256,
                timesync_request_tc1=0,
                timesync_request_ts1=pending.timesync_token,
                timesync_frame_hex=encoded["timesync_frame"].hex(),
                timesync_frame_sha256=pending.timesync_frame_sha256,
                request_transport_payload_hex=encoded["request_datagram"].hex(),
                request_transport_payload_sha256=pending.request_datagram_sha256,
                request_transport_payload_size=len(encoded["request_datagram"]),
                request_transport_send_return_size=size,
            )
        while self.pending_by_uav:
            self.pump(0.05)
            self._complete_ready()
            self._expire_pending(self.now_ns())
        self._complete_ready()
        if set(self.attempt_outcomes) != uavs:
            raise AirborneControlError("capacity flight attempt outcome set differs")
        return {
            uav
            for uav, outcome in self.attempt_outcomes.items()
            if outcome != "accepted"
        }

    def _retire_pending(self, pending: PendingFlightCommand, outcome: str) -> None:
        key = (pending.uav, pending.timesync_token)
        if key in self.retired_tokens:
            raise AirborneControlError("capacity flight token retired twice")
        self.retired_tokens[key] = {
            "outcome": outcome,
            "transaction_id": pending.transaction_id,
            "command_id": pending.command_id,
            # Preserve which independently required response components were
            # already consumed.  A timeout can occur with only one component;
            # its later duplicate must never be relabeled as a first late fact.
            "ack_seen": pending.ack is not None,
            "timesync_seen": pending.timesync is not None,
        }
        self.pending_by_token.pop(key, None)
        self.pending_by_uav.pop(pending.uav, None)
        self.attempt_outcomes[pending.uav] = outcome

    def _expire_pending(self, observed_ns: int) -> None:
        for pending in list(self.pending_by_uav.values()):
            if observed_ns < pending.sent_monotonic_ns + OUTCOME_TIMEOUT_NS:
                continue
            missing = {
                "ack": pending.ack is None,
                "timesync": pending.timesync is None,
            }
            self.writer.emit(
                "flight_command_outcome_timeout",
                flight_stage=pending.stage,
                transaction_id=pending.transaction_id,
                attempt=pending.attempt,
                uav=pending.uav,
                command_id=pending.command_id,
                sent_monotonic_ns=pending.sent_monotonic_ns,
                deadline_monotonic_ns=pending.sent_monotonic_ns
                + OUTCOME_TIMEOUT_NS,
                observed_monotonic_ns=observed_ns,
                missing=missing,
                timeout_ns=OUTCOME_TIMEOUT_NS,
            )
            self._retire_pending(pending, "timeout")
            self.quiet_command_guards[pending.uav] = (
                pending.uav,
                pending.timesync_token,
            )
            self.quiet_last_response_ns = max(
                self.quiet_last_response_ns, observed_ns
            )

    def _quiet_drain(
        self, *, command_id: int, uavs: set[int], reason: str
    ) -> None:
        if self.pending_by_uav or self.pending_by_token:
            raise AirborneControlError("capacity flight quiet drain has pending state")
        guards: dict[int, tuple[int, int]] = {}
        for uav in sorted(uavs):
            candidates = [
                key
                for key, retired in self.retired_tokens.items()
                if key[0] == uav and retired["command_id"] == command_id
            ]
            if not candidates:
                raise AirborneControlError(
                    "capacity flight quiet drain lacks an exact retired ACK owner"
                )
            guards[uav] = max(candidates, key=lambda key: key[1])
        self.quiet_command_guards = guards
        started_ns = self.now_ns()
        self.quiet_last_response_ns = started_ns
        # The declared execution budget owns exactly one three-second drain.
        # A late response that restarts quiet time cannot silently extend the
        # run; fail this attempt and preserve the bounded gate instead.
        hard_deadline_ns = started_ns + OUTCOME_TIMEOUT_NS
        while (
            self.now_ns() - self.quiet_last_response_ns < OUTCOME_TIMEOUT_NS
            and self.now_ns() < hard_deadline_ns
        ):
            self.pump(0.05)
        completed_ns = self.now_ns()
        if completed_ns - self.quiet_last_response_ns < OUTCOME_TIMEOUT_NS:
            raise AirborneControlError("capacity flight quiet drain did not converge")
        self.writer.emit(
            "flight_command_quiet_drain",
            command_id=command_id,
            guarded_uavs=[f"uav{uav}" for uav in sorted(uavs)],
            reason=reason,
            started_monotonic_ns=started_ns,
            last_response_monotonic_ns=self.quiet_last_response_ns,
            completed_monotonic_ns=completed_ns,
            required_quiet_ns=OUTCOME_TIMEOUT_NS,
        )
        self.quiet_command_guards = {}

    def _complete_ready(self) -> None:
        for uav, pending in list(self.pending_by_uav.items()):
            if pending.ack is None or pending.timesync is None:
                continue
            completed_ns = self.now_ns()
            self.writer.emit(
                (
                    "flight_command_complete"
                    if pending.ack["command_result"] == MAV_RESULT_ACCEPTED
                    else "flight_command_retryable_rejection_complete"
                ),
                flight_stage=pending.stage,
                flight_stage_code=pending.stage_code,
                transaction_id=pending.transaction_id,
                uav=uav,
                attempt=pending.attempt,
                command_id=pending.command_id,
                sent_monotonic_ns=pending.sent_monotonic_ns,
                completed_monotonic_ns=completed_ns,
                command_frame_sha256=pending.command_frame_sha256,
                timesync_frame_sha256=pending.timesync_frame_sha256,
                request_transport_payload_sha256=pending.request_datagram_sha256,
                ack=pending.ack,
                timesync_response=pending.timesync,
            )
            outcome = (
                "accepted"
                if pending.ack["command_result"] == MAV_RESULT_ACCEPTED
                else "temporarily_rejected"
            )
            self._retire_pending(pending, outcome)
            if outcome != "accepted":
                self.quiet_command_guards[uav] = (
                    pending.uav,
                    pending.timesync_token,
                )
                self.quiet_last_response_ns = max(
                    self.quiet_last_response_ns, completed_ns
                )

    def handles_response(self, message_type: str, uav: int, message: Any) -> bool:
        """Return true iff ACK/TIMESYNC belongs to an active flight command."""

        if message_type == "COMMAND_ACK":
            pending = self.pending_by_uav.get(uav)
            command = getattr(message, "command", None)
            guard_key = self.quiet_command_guards.get(uav)
            guarded = (
                self.retired_tokens.get(guard_key)
                if guard_key is not None
                else None
            )
            return (
                pending is not None and command == pending.command_id
            ) or (
                guarded is not None and guarded["command_id"] == command
            )
        if message_type == "TIMESYNC":
            token = getattr(message, "ts1", None)
            return (
                isinstance(token, int)
                and not isinstance(token, bool)
                and (
                    (uav, token) in self.pending_by_token
                    or (uav, token) in self.retired_tokens
                )
            )
        return False

    def observe_message(
        self,
        *,
        message_type: str,
        uav: int,
        message: Any,
        received_ns: int,
        common: Mapping[str, Any],
    ) -> bool:
        """Record one raw vehicle message and consume owned ACK/TIMESYNC replies."""

        if uav not in EXPECTED_UAVS:
            raise AirborneControlError("capacity flight response UAV differs")
        self._expire_pending(received_ns)
        guard_key = self.quiet_command_guards.get(uav)
        guarded = (
            self.retired_tokens.get(guard_key)
            if guard_key is not None
            else None
        )
        if (
            message_type == "COMMAND_ACK"
            and guarded is not None
            and guarded["command_id"] == getattr(message, "command", None)
        ):
            if guarded["ack_seen"] is True:
                self.writer.emit(
                    "duplicate_flight_command_ack",
                    transaction_id=guarded["transaction_id"],
                    command_id=getattr(message, "command", None),
                    command_result=getattr(message, "result", None),
                    **common,
                )
                raise AirborneControlError(
                    "duplicate capacity flight COMMAND_ACK"
                )
            guarded["ack_seen"] = True
            self.quiet_last_response_ns = max(
                self.quiet_last_response_ns, received_ns
            )
            self.writer.emit(
                "late_flight_command_ack",
                transaction_id=guarded["transaction_id"],
                command_id=getattr(message, "command", None),
                command_result=getattr(message, "result", None),
                **common,
            )
            return True
        if message_type == "COMMAND_ACK" and self.handles_response(message_type, uav, message):
            pending = self.pending_by_uav[uav]
            result = getattr(message, "result", None)
            retryable = result == MAV_RESULT_TEMPORARILY_REJECTED
            if (
                isinstance(result, bool)
                or not isinstance(result, int)
                or result not in {MAV_RESULT_ACCEPTED, MAV_RESULT_TEMPORARILY_REJECTED}
                or (
                    result == MAV_RESULT_TEMPORARILY_REJECTED
                    and not retryable
                )
            ):
                self.writer.emit(
                    "flight_command_rejected",
                    flight_stage=pending.stage,
                    transaction_id=pending.transaction_id,
                    command_id=pending.command_id,
                    command_result=result,
                    **common,
                )
                raise AirborneControlError("capacity flight COMMAND_ACK rejected")
            if pending.ack is not None:
                raise AirborneControlError("duplicate capacity flight COMMAND_ACK")
            pending.ack = {
                "received_monotonic_ns": received_ns,
                "command_id": pending.command_id,
                "command_result": result,
                "source_system": uav,
                "source_component": 1,
                "transport_payload_sha256": common["transport_payload_sha256"],
                "mavlink_frame_sha256": common["mavlink_frame_sha256"],
            }
            self.writer.emit(
                "flight_command_ack",
                flight_stage=pending.stage,
                transaction_id=pending.transaction_id,
                command_id=pending.command_id,
                command_result=result,
                retryable=retryable,
                **common,
            )
            self._complete_ready()
            return True
        if message_type == "TIMESYNC" and self.handles_response(message_type, uav, message):
            token = int(getattr(message, "ts1"))
            key = (uav, token)
            if key in self.retired_tokens:
                retired = self.retired_tokens[key]
                if retired["timesync_seen"] is True or retired["outcome"] == "accepted":
                    self.writer.emit(
                        "duplicate_flight_timesync_echo",
                        timesync_tc1=getattr(message, "tc1", None),
                        timesync_ts1=token,
                        **common,
                    )
                    raise AirborneControlError(
                        "duplicate capacity flight TIMESYNC echo"
                    )
                retired["timesync_seen"] = True
                self.quiet_last_response_ns = max(
                    self.quiet_last_response_ns, received_ns
                )
                self.writer.emit(
                    "late_flight_timesync_echo",
                    transaction_id=retired["transaction_id"],
                    timesync_tc1=getattr(message, "tc1", None),
                    timesync_ts1=token,
                    **common,
                )
                return True
            pending = self.pending_by_token[key]
            vehicle_clock = getattr(message, "tc1", None)
            if (
                isinstance(vehicle_clock, bool)
                or not isinstance(vehicle_clock, int)
                or vehicle_clock <= 0
                or token != pending.timesync_token
            ):
                raise AirborneControlError("capacity flight TIMESYNC echo fields differ")
            if pending.timesync is not None:
                raise AirborneControlError("duplicate capacity flight TIMESYNC echo")
            pending.timesync = {
                "received_monotonic_ns": received_ns,
                "timesync_tc1": vehicle_clock,
                "timesync_ts1": token,
                "source_system": uav,
                "source_component": 1,
                "transport_payload_sha256": common["transport_payload_sha256"],
                "mavlink_frame_sha256": common["mavlink_frame_sha256"],
            }
            self.writer.emit(
                "flight_command_timesync_echo",
                flight_stage=pending.stage,
                transaction_id=pending.transaction_id,
                timesync_tc1=vehicle_clock,
                timesync_ts1=token,
                **common,
            )
            self._complete_ready()
            return True
        if message_type == "HEARTBEAT":
            base_mode = int(getattr(message, "base_mode", -1))
            custom_mode = int(getattr(message, "custom_mode", -1))
            state = {
                "received_monotonic_ns": received_ns,
                "base_mode": base_mode,
                "custom_mode": custom_mode,
                "system_status": int(getattr(message, "system_status", -1)),
                "mav_type": int(getattr(message, "type", -1)),
                "autopilot": int(getattr(message, "autopilot", -1)),
            }
            self.latest_heartbeat[uav] = state
            self.writer.emit(
                "flight_vehicle_heartbeat",
                base_mode=base_mode,
                custom_mode=custom_mode,
                system_status=state["system_status"],
                mav_type=state["mav_type"],
                autopilot=state["autopilot"],
                source_topic="actual_sitl_mavlink",
                source_frame="ardupilot_body_ned",
                transform_version="ams-m4-coordinate-frames-v1",
                **common,
            )
            return False
        if message_type == "EXTENDED_SYS_STATE":
            state = {
                "received_monotonic_ns": received_ns,
                "landed_state": int(getattr(message, "landed_state", -1)),
                "vtol_state": int(getattr(message, "vtol_state", -1)),
            }
            self.latest_extended_state[uav] = state
            self.writer.emit(
                "flight_vehicle_extended_state",
                landed_state=state["landed_state"],
                vtol_state=state["vtol_state"],
                source_topic="actual_sitl_mavlink",
                source_frame="ardupilot_body_ned",
                transform_version="ams-m4-coordinate-frames-v1",
                **common,
            )
            return True
        if message_type == "GLOBAL_POSITION_INT":
            relative_alt_mm = int(getattr(message, "relative_alt", -2**31))
            state = {
                "received_monotonic_ns": received_ns,
                "relative_alt_mm": relative_alt_mm,
                "lat_e7": int(getattr(message, "lat", 0)),
                "lon_e7": int(getattr(message, "lon", 0)),
                "alt_msl_mm": int(getattr(message, "alt", -2**31)),
                "vx_cms": int(getattr(message, "vx", 0)),
                "vy_cms": int(getattr(message, "vy", 0)),
                "vz_cms": int(getattr(message, "vz", 0)),
            }
            self.latest_global_position[uav] = state
            self.writer.emit(
                "flight_vehicle_global_position",
                relative_alt_mm=relative_alt_mm,
                lat_e7=state["lat_e7"],
                lon_e7=state["lon_e7"],
                alt_msl_mm=state["alt_msl_mm"],
                vx_cms=state["vx_cms"],
                vy_cms=state["vy_cms"],
                vz_cms=state["vz_cms"],
                source_topic="actual_sitl_mavlink",
                source_frame="ardupilot_global_wgs84",
                transform_version="ams-m4-coordinate-frames-v1",
                **common,
            )
            return True
        if message_type == "LOCAL_POSITION_NED":
            state = {
                "received_monotonic_ns": received_ns,
                "x_m": float(getattr(message, "x", math.nan)),
                "y_m": float(getattr(message, "y", math.nan)),
                "z_down_m": float(getattr(message, "z", math.nan)),
                "vx_mps": float(getattr(message, "vx", math.nan)),
                "vy_mps": float(getattr(message, "vy", math.nan)),
                "vz_mps": float(getattr(message, "vz", math.nan)),
            }
            if not all(math.isfinite(value) for key, value in state.items() if key != "received_monotonic_ns"):
                raise AirborneControlError("capacity LOCAL_POSITION_NED is non-finite")
            self.latest_local_position[uav] = state
            self.writer.emit(
                "flight_vehicle_local_position",
                x_m=state["x_m"],
                y_m=state["y_m"],
                z_down_m=state["z_down_m"],
                vx_mps=state["vx_mps"],
                vy_mps=state["vy_mps"],
                vz_mps=state["vz_mps"],
                source_topic="actual_sitl_mavlink",
                source_frame="ardupilot_local_ned",
                transform_version="ams-m4-coordinate-frames-v1",
                **common,
            )
            return True
        return False

    def _state_fresh(
        self,
        state: Mapping[str, Any] | None,
        now_ns: int,
        maximum_age_ns: int,
    ) -> bool:
        if not isinstance(state, Mapping):
            return False
        received = state.get("received_monotonic_ns")
        return (
            isinstance(received, int)
            and not isinstance(received, bool)
            and 0 <= now_ns - received <= maximum_age_ns
        )

    def _all_airborne(self, now_ns: int) -> bool:
        for uav in EXPECTED_UAVS:
            heartbeat = self.latest_heartbeat.get(uav)
            extended = self.latest_extended_state.get(uav)
            position = self.latest_global_position.get(uav)
            local = self.latest_local_position.get(uav)
            if (
                not self._state_fresh(heartbeat, now_ns, HEARTBEAT_FRESHNESS_NS)
                or not self._state_fresh(
                    extended, now_ns, HIGH_RATE_STATE_FRESHNESS_NS
                )
                or not self._state_fresh(
                    position, now_ns, HIGH_RATE_STATE_FRESHNESS_NS
                )
                or not self._state_fresh(
                    local, now_ns, HIGH_RATE_STATE_FRESHNESS_NS
                )
            ):
                return False
            assert (
                heartbeat is not None
                and extended is not None
                and position is not None
                and local is not None
            )
            if (
                int(heartbeat["base_mode"]) & MAV_MODE_FLAG_SAFETY_ARMED == 0
                or int(heartbeat["custom_mode"]) != COPTER_MODE_GUIDED
                or int(extended["landed_state"]) != MAV_LANDED_STATE_IN_AIR
                or int(position["relative_alt_mm"])
                < int(MINIMUM_RELATIVE_ALT_M * 1000)
                or float(local["z_down_m"]) > -MINIMUM_RELATIVE_ALT_M
            ):
                return False
        return True

    def _all_prearm_ground(self, now_ns: int, *, require_guided: bool = False) -> bool:
        for uav in EXPECTED_UAVS:
            heartbeat = self.latest_heartbeat.get(uav)
            extended = self.latest_extended_state.get(uav)
            position = self.latest_global_position.get(uav)
            local = self.latest_local_position.get(uav)
            if (
                not self._state_fresh(heartbeat, now_ns, HEARTBEAT_FRESHNESS_NS)
                or not self._state_fresh(
                    extended, now_ns, HIGH_RATE_STATE_FRESHNESS_NS
                )
                or not self._state_fresh(
                    position, now_ns, HIGH_RATE_STATE_FRESHNESS_NS
                )
                or not self._state_fresh(
                    local, now_ns, HIGH_RATE_STATE_FRESHNESS_NS
                )
            ):
                return False
            assert heartbeat and extended and position and local
            if (
                int(heartbeat["base_mode"]) & MAV_MODE_FLAG_SAFETY_ARMED != 0
                or (
                    require_guided
                    and int(heartbeat["custom_mode"]) != COPTER_MODE_GUIDED
                )
                or int(extended["landed_state"]) != MAV_LANDED_STATE_ON_GROUND
                or abs(int(position["relative_alt_mm"]))
                > int(PREARM_ALTITUDE_TOLERANCE_M * 1000)
                or abs(float(local["z_down_m"])) > PREARM_ALTITUDE_TOLERANCE_M
            ):
                return False
        return True

    def _all_landed(self, now_ns: int) -> bool:
        return all(
            self._state_fresh(
                self.latest_extended_state.get(uav),
                now_ns,
                HIGH_RATE_STATE_FRESHNESS_NS,
            )
            and self.latest_extended_state[uav]["landed_state"]
            == MAV_LANDED_STATE_ON_GROUND
            for uav in EXPECTED_UAVS
        )

    def _all_disarmed(self, now_ns: int) -> bool:
        return all(
            self._state_fresh(
                self.latest_heartbeat.get(uav), now_ns, HEARTBEAT_FRESHNESS_NS
            )
            and int(self.latest_heartbeat[uav]["base_mode"])
            & MAV_MODE_FLAG_SAFETY_ARMED
            == 0
            for uav in EXPECTED_UAVS
        )

    def prepare(self) -> None:
        if self.started:
            raise AirborneControlError("capacity flight plan started twice")
        self.started = True
        self.writer.emit(
            "flight_plan_started",
            airborne_gate_contract=AIRBORNE_GATE_CONTRACT,
            # Preserve the complete independently reconstructible declaration
            # at the point where the first modeled-path command may be sent.
            # A later validator therefore cannot combine commands from one
            # schedule with the run contract of another schedule.
            declared_airborne_gate=self.gate,
            airborne_ready_deadline_monotonic_ns=self.gate[
                "airborne_ready_deadline_monotonic_ns"
            ],
            measurement_start_monotonic_ns=self.gate[
                "measurement_start_monotonic_ns"
            ],
            measurement_end_monotonic_ns=self.gate[
                "measurement_end_monotonic_ns"
            ],
        )
        for stage in PRE_MEASUREMENT_STAGES:
            if stage == "arm":
                deadline = min(
                    int(self.gate["airborne_ready_deadline_monotonic_ns"]),
                    self.now_ns() + 30_000_000_000,
                )
                while (
                    self.now_ns() < deadline
                    and not self._all_prearm_ground(
                        self.now_ns(), require_guided=True
                    )
                ):
                    self.pump(0.05)
                if not self._all_prearm_ground(
                    self.now_ns(), require_guided=True
                ):
                    raise AirborneControlError(
                        "five actual UAVs lack fresh GUIDED/disarmed/on-ground pre-arm state"
                    )
                self.writer.emit(
                    "flight_prearm_boundary",
                    checked_monotonic_ns=self.now_ns(),
                    guarded_uavs=[f"uav{uav}" for uav in EXPECTED_UAVS],
                )
            self._send_stage_with_reuse_guard(stage)
        deadline = min(
            int(self.gate["airborne_ready_deadline_monotonic_ns"]),
            self.now_ns() + AIRBORNE_TIMEOUT_NS,
        )
        while self.now_ns() < deadline and not self._all_airborne(self.now_ns()):
            self.pump(0.05)
        if not self._all_airborne(self.now_ns()):
            raise AirborneControlError("five actual UAVs did not reach fresh airborne state")

    def mark_measurement_started(self) -> None:
        if (
            not self.started
            or not self.airborne_ready_confirmed
            or not self.warmup_motion_started
            or self.measurement_started
        ):
            raise AirborneControlError("capacity flight measurement start order differs")
        self.measurement_started = True
        self.writer.emit(
            "flight_measurement_boundary",
            boundary="start",
            target_monotonic_ns=self.gate["measurement_start_monotonic_ns"],
        )

    def confirm_airborne_ready_boundary(self) -> None:
        if not self.started or self.airborne_ready_confirmed:
            raise AirborneControlError("capacity airborne-ready boundary order differs")
        now_ns = self.now_ns()
        target_ns = int(self.gate["airborne_ready_deadline_monotonic_ns"])
        if not target_ns <= now_ns <= target_ns + 500_000_000:
            raise AirborneControlError("capacity airborne-ready boundary timing differs")
        if not self._all_airborne(now_ns):
            raise AirborneControlError(
                "five actual UAVs lack fresh airborne state at warm-up boundary"
            )
        self.airborne_ready_confirmed = True
        self.writer.emit(
            "flight_airborne_ready_boundary",
            target_monotonic_ns=target_ns,
        )

    def start_warmup_motion(self) -> None:
        """Issue the one bounded vertical reposition during warm-up only."""

        if (
            not self.airborne_ready_confirmed
            or self.warmup_motion_started
            or self.measurement_started
        ):
            raise AirborneControlError("capacity warm-up motion order differs")
        started_ns = self.now_ns()
        warmup_start_ns = int(self.gate["warmup_start_monotonic_ns"])
        deadline_ns = int(self.gate["warmup_motion_deadline_monotonic_ns"])
        if not warmup_start_ns <= started_ns < deadline_ns:
            raise AirborneControlError("capacity warm-up motion start timing differs")
        if not self._all_airborne(started_ns):
            raise AirborneControlError(
                "five actual UAVs lack fresh airborne state before reposition"
            )
        self.writer.emit(
            "flight_warmup_motion_boundary",
            boundary="start",
            target_monotonic_ns=warmup_start_ns,
            deadline_monotonic_ns=deadline_ns,
        )
        self._send_stage_with_reuse_guard("reposition")
        completed_ns = self.now_ns()
        if completed_ns >= deadline_ns:
            raise AirborneControlError("capacity warm-up motion exceeded its bound")
        self.warmup_motion_started = True
        self.writer.emit(
            "flight_warmup_motion_boundary",
            boundary="complete",
            target_monotonic_ns=warmup_start_ns,
            deadline_monotonic_ns=deadline_ns,
            completed_monotonic_ns=completed_ns,
        )

    def mark_measurement_ended(self) -> None:
        if not self.measurement_started or self.measurement_ended:
            raise AirborneControlError("capacity flight measurement end order differs")
        self.measurement_ended = True
        self.writer.emit(
            "flight_measurement_boundary",
            boundary="end",
            target_monotonic_ns=self.gate["measurement_end_monotonic_ns"],
        )

    def land_and_disarm(self) -> None:
        if not self.measurement_ended:
            raise AirborneControlError("capacity flight landing precedes measurement end")
        post_control_boundary_ns = int(
            self.gate["measurement_end_monotonic_ns"]
        ) + int(self.gate["post_measurement_control_ns"])
        if self.now_ns() < post_control_boundary_ns:
            raise AirborneControlError(
                "capacity flight landing precedes post-measurement control completion"
            )
        self._send_stage_with_reuse_guard("land")
        landing_deadline = int(self.gate["landing_deadline_monotonic_ns"])
        while (
            self.now_ns() < landing_deadline
            and not self._all_landed(self.now_ns())
        ):
            self.pump(0.05)
        if not self._all_landed(self.now_ns()):
            raise AirborneControlError("five actual UAVs did not report landed state")
        self._send_stage_with_reuse_guard("disarm")
        disarm_deadline = int(self.gate["disarm_deadline_monotonic_ns"])
        while (
            self.now_ns() < disarm_deadline
            and not self._all_disarmed(self.now_ns())
        ):
            self.pump(0.05)
        if not self._all_disarmed(self.now_ns()):
            raise AirborneControlError("five actual UAVs did not disarm after landing")
        self.writer.emit(
            "flight_plan_shutdown",
            post_control_boundary_monotonic_ns=post_control_boundary_ns,
            landing_deadline_monotonic_ns=landing_deadline,
            disarm_deadline_monotonic_ns=disarm_deadline,
        )


def finite_vector3(value: Any) -> tuple[float, float, float]:
    """Strict finite three-vector helper shared by adversarial validation tests."""

    if not isinstance(value, list) or len(value) != 3:
        raise AirborneControlError("position is not an exact three-vector")
    converted = tuple(float(item) for item in value)
    if not all(math.isfinite(item) for item in converted):
        raise AirborneControlError("position contains a non-finite value")
    return converted
