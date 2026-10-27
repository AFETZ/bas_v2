#!/usr/bin/env python3
"""Run the live five-UAV relay experiment inside ams-gcs.

The GCS sees only radio-delivered MAVLink/probe messages for control and leader
selection. Gazebo/ROS ground truth and UART event logs are observation inputs.
Refresh commands are new at 1 Hz; physical goals change at 20 s by default.
Phase times use Gazebo source time; leases/deadlines use host monotonic time.
The phase file requests a declared native fault; it is never a leader input.
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import math
import os
from pathlib import Path
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from network.coordination.policy import Command, ProtocolError
from network.coordination.service import Events, GCSService, build_parser

UAV_IDS = tuple(range(1, 6))


class ScenarioError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class Pose:
    position: tuple[float, float, float]
    velocity: tuple[float, float, float]
    source_s: float
    sample_ns: int


class PoseReader:
    """Observe fresh native odometry; never predict or repair missing samples."""

    def __init__(self, path: Path):
        self.path, self.session = path, None
        self.last_source_s = None

    def read(self) -> tuple[float, dict[int, Pose]]:
        try:
            value = json.loads(self.path.read_text())
            now = time.monotonic_ns()  # after atomic-file read
            if (value["schema_version"] != 2 or value["coordinate_frame"] != "ENU"
                    or value["source"] not in ("gazebo_odometry", "ros_odometry")
                    or value.get("fault") is not None):
                raise ScenarioError("invalid native odometry contract")
            session = value["session_id"]
            if self.session is not None and self.session != session:
                raise ScenarioError("odometry session reset")
            self.session = session
            for field in ("published_monotonic_ns", "clock_received_monotonic_ns"):
                if not 0 <= now - value[field] <= 500_000_000:
                    raise ScenarioError(f"stale native {field}")
            source = float(value["source_sim_time_s"])
            if self.last_source_s is not None and source < self.last_source_s:
                raise ScenarioError("odometry source clock moved backwards")
            self.last_source_s = source
            poses = {}
            for row in value["nodes"]:
                name = str(row.get("id", ""))
                if name not in {f"uav{i}" for i in UAV_IDS}:
                    continue
                # Use only genuine causal samples, as the native mobility reader does.
                history = row.get("history", [row])
                causal = [sample for sample in history if sample["source_sim_time_s"] <= source
                          and sample["sample_monotonic_ns"] <= now]
                if not causal:
                    raise ScenarioError(f"no causal native pose {name}")
                row = max(causal, key=lambda sample: sample["source_sim_time_s"])
                if row.get("stale") or not 0 <= now - row["sample_monotonic_ns"] <= 500_000_000:
                    raise ScenarioError(f"stale native pose {name}")
                point, velocity = tuple(row["position_m"]), tuple(row["velocity_enu_mps"])
                stamp = float(row["source_sim_time_s"])
                if (len(point) != 3 or len(velocity) != 3
                        or not all(math.isfinite(x) for x in (*point, *velocity, stamp, source))
                        or not 0 <= source - stamp <= .5):
                    raise ScenarioError(f"invalid pose/source clock {name}")
                poses[int(name[3:])] = Pose(point, velocity, stamp, row["sample_monotonic_ns"])
            if set(poses) != set(UAV_IDS):
                raise ScenarioError("missing native five-UAV poses")
            if max(p.source_s for p in poses.values()) - min(p.source_s for p in poses.values()) > .1:
                raise ScenarioError("native cross-UAV source skew exceeds 100 ms")
            return source, poses
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ScenarioError(f"native odometry unavailable: {exc}") from exc


class RegionHold:
    def __init__(self, target, hold_s=2., z_tolerance=1.5, xy_tolerance=3., speed_limit=1.):
        self.target = tuple(target)
        self.hold_s, self.z_tolerance = hold_s, z_tolerance
        self.xy_tolerance, self.speed_limit = xy_tolerance, speed_limit
        self.started_s = None
        self.previous_s = None

    def observe(self, pose: Pose) -> bool:
        if self.previous_s is not None and pose.source_s <= self.previous_s:
            return False  # repeated publication cannot advance dwell
        gap = self.previous_s is not None and pose.source_s - self.previous_s > .5
        self.previous_s = pose.source_s
        inside = (math.hypot(pose.position[0] - self.target[0], pose.position[1] - self.target[1]) <= self.xy_tolerance
                  and abs(pose.position[2] - self.target[2]) <= self.z_tolerance
                  and math.sqrt(sum(v * v for v in pose.velocity)) <= self.speed_limit)
        if not inside or gap:
            self.started_s = None
        if not inside:
            return False
        if self.started_s is None:
            self.started_s = pose.source_s
        return pose.source_s - self.started_s >= self.hold_s


class EventTail:
    def __init__(self, path: Path):
        self.path, self.offset = path, 0

    def read(self):
        if not self.path.exists():
            return []
        result = []
        with self.path.open() as stream:
            stream.seek(self.offset)
            while True:
                before = stream.tell()
                line = stream.readline()
                if not line or not line.endswith("\n"):
                    self.offset = before
                    break
                result.append(json.loads(line))
                self.offset = stream.tell()
        return result


@dataclasses.dataclass
class Goal:
    goal_id: str
    uav_id: int
    created_ns: int
    created_source_s: float
    relative_alt_m: float
    target: tuple[float, float, float]


class GoalObserver:
    """A new refresh does not create a new maneuver or restart the same dwell."""

    def __init__(self, emit, args):
        self.emit, self.args = emit, args
        self.commands: dict[str, tuple[Command, Goal]] = {}
        self.active: dict[int, dict[str, Any]] = {}
        self.completed: dict[str, dict[str, Any]] = {}

    def register(self, command: Command, goal: Goal):
        self.commands[command.command_id] = command, goal

    def admission(self, row: dict):
        item = self.commands.get(row.get("command_id"))
        if item is None or row.get("event") != "uart_admitted":
            return
        command, goal = item
        if row.get("epoch") != command.epoch or row.get("uav_id") != command.destination_id:
            raise ScenarioError("UART admission lineage mismatch")
        previous = self.active.get(goal.uav_id)
        if previous is None or previous["goal"].goal_id != goal.goal_id or previous["command"].epoch != command.epoch:
            previous = dict(goal=goal, command=command, admission_ns=row["host_monotonic_ns"],
                            hold=RegionHold(goal.target, self.args.hold_s, self.args.z_tolerance_m,
                                            self.args.xy_tolerance_m, self.args.hold_speed_mps),
                            start_z=None, max_vertical_displacement_m=0.)
            self.active[goal.uav_id] = previous
        previous["command"] = command

    def observe(self, poses: dict[int, Pose], now_ns: int):
        for i, state in self.active.items():
            command, goal = state["command"], state["goal"]
            if goal.goal_id in self.completed:
                continue
            pose = poses[i]
            if now_ns >= command.valid_until_ns or pose.sample_ns < state["admission_ns"]:
                state["hold"].started_s = None
                continue
            if state["start_z"] is None:
                state["start_z"] = pose.position[2]
            state["max_vertical_displacement_m"] = max(state["max_vertical_displacement_m"],
                                                       abs(pose.position[2] - state["start_z"]))
            if state["hold"].observe(pose):
                result = dict(goal_id=goal.goal_id, goal_created_host_monotonic_ns=goal.created_ns,
                              goal_created_source_sim_time_s=goal.created_source_s,
                              source_sim_time_s=pose.source_s, actual_position_m=pose.position,
                              dwell_s=self.args.hold_s, dwell_clock_domain="source_sim_time",
                              target_position_m=goal.target, leader_id=command.leader_id,
                              command_created_host_monotonic_ns=command.created_ns,
                              admitted_command_id=command.command_id, admitted_epoch=command.epoch,
                              max_vertical_displacement_m=state["max_vertical_displacement_m"],
                              maneuver_observed=state["max_vertical_displacement_m"] >= 1.)
                self.completed[goal.goal_id] = dict(result, uav_id=i, epoch=command.epoch,
                                                    command_id=command.command_id)
                self.emit("action_completed", command=command, **result)


class LandingTrace:
    """Require our UART LAND, observed LAND, descent, ground and then disarm."""

    def __init__(self, start: Pose, ground_z: float, *, ground_tolerance=.75, max_descent_mps=3.):
        self.start, self.ground_z = start, ground_z
        self.ground_tolerance, self.max_descent_mps = ground_tolerance, max_descent_mps
        self.admission_ns, self.command_id = None, None
        self.mode_ns, self.descent_ns, self.ground_ns = None, None, None
        self.unsafe_descent, self.completed, self.terminal_reason = False, False, None
        self.events = []

    def admit(self, command_id: str, host_ns: int):
        if self.admission_ns is None:
            self.admission_ns, self.command_id = host_ns, command_id

    def observe(self, pose: Pose, *, heartbeat_ns: int, mode_land: bool, armed: bool):
        if self.completed or self.terminal_reason:
            return
        if (self.admission_ns is not None and heartbeat_ns >= self.admission_ns
                and mode_land and self.mode_ns is None):
            self.mode_ns = heartbeat_ns
            self.events.append("land_mode_observed")
        speed = math.sqrt(sum(v * v for v in pose.velocity))
        near_ground = abs(pose.position[2] - self.ground_z) <= self.ground_tolerance and speed <= .5
        if self.mode_ns is not None and pose.sample_ns >= self.mode_ns:
            self.unsafe_descent |= pose.velocity[2] < -self.max_descent_mps
            if (self.descent_ns is None and self.start.position[2] >= self.ground_z + 2
                    and pose.position[2] <= self.start.position[2] - 2 and pose.velocity[2] < -.1):
                self.descent_ns = pose.sample_ns
                self.events.append("landing_descent_observed")
            if self.descent_ns is not None and near_ground and self.ground_ns is None:
                self.ground_ns = pose.sample_ns
                self.events.append("landing_ground_observed")
        if not armed:
            if self.mode_ns is not None and self.descent_ns is not None and self.ground_ns is not None and not self.unsafe_descent:
                if heartbeat_ns >= self.ground_ns and near_ground:
                    self.completed = True
                    self.events.append("landing_disarm_observed")
            else:
                self.terminal_reason = "terminal_disarm_without_observed_controlled_LAND_chain"
                self.events.append("landing_terminal_outcome")


class RelayExperiment:
    def __init__(self, args):
        self.args = args
        args.run_dir.mkdir(parents=True, exist_ok=True)
        args.phase_file.parent.mkdir(parents=True, exist_ok=True)
        if (args.run_dir / "metrics/relay_experiment_summary.json").exists():
            raise ScenarioError("run already has a summary; use a fresh run directory")
        service_args = build_parser().parse_args(["gcs", "--run-id", args.run_id,
                    "--policy", args.policy, "--initial-master", str(args.initial_master),
                    "--event-log", str(args.run_dir / "logs/coordination_gcs.jsonl")])
        service_args.radio_watchdog_file = args.radio_watchdog_file
        service_args.watchdog_s = args.watchdog_s
        self.emit = Events(service_args.event_log, args.run_id, 0)
        self.gcs = GCSService(service_args, self.emit)
        self.reader = PoseReader(args.node_state)
        self.observer = GoalObserver(self.emit, args)
        self.tails = {i: EventTail(args.run_dir / f"logs/coordination_uav{i}.jsonl") for i in UAV_IDS}
        self.mav = self.gcs.mavutil.mavlink
        self.tx = self.mav.MAVLink(None, srcSystem=255, srcComponent=191)
        self.modes = {name: number for number, name in self.gcs.mavutil.mode_mapping_acm.items()}
        self.initial, self.anchors = {}, {}
        self.mode_request_ns, self.mode_epoch = {}, {}
        self.land_commands, self.landing_traces = {}, {}
        self.last_heartbeat_ns = 0
        self.refresh_counter = 0
        self.onboard_mode, self.brake_regions = {}, {}
        self.source_s, self.poses = None, {}
        self.observation_end_source_s = None
        self.phase_name = None
        self.summary = dict(status="failed", run_id=args.run_id, policy=args.policy,
                fault_profile=args.fault_profile, pilot=args.pilot,
                experiment_profile=args.experiment_profile, phase_clock_domain="source_sim_time",
                lease_clock_domain="host_monotonic", phase_requests=[],
                uavs={str(i): dict(armed=False, takeoff=False, landed=False, auto_disarm=False) for i in UAV_IDS},
                mode_observations=[], failsafe_holds={}, command_refresh_period_s=args.command_period_s,
                physical_goal_period_s=args.task_period_s)
        metrics = args.run_dir / "metrics"
        metrics.mkdir(exist_ok=True)
        self.csv_handle = (metrics / "relay_control_refresh.csv").open("w", newline="")
        self.csv = csv.DictWriter(self.csv_handle, fieldnames=["host_monotonic_ns", "source_sim_time_s",
            "request_id", "scheduled_host_monotonic_ns", "phase", "uav_id", "goal_id", "goal_created_host_monotonic_ns", "command_id", "epoch",
            "leader_id", "target_world_z_m", "target_relative_alt_m", "send_status"])
        self.csv.writeheader()
        frozen = {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()}
        frozen["effective_service_args"] = {k: str(v) if isinstance(v, Path) else v for k, v in vars(service_args).items()}
        (metrics / "relay_experiment_config.json").write_text(json.dumps(frozen, indent=2) + "\n")
        (args.run_dir / "logs/coordination_gcs.ready").write_text(json.dumps(dict(pid=os.getpid())) + "\n")

    def phase(self, name: str):
        if name == self.phase_name:
            return
        self.phase_name = name
        temporary = self.args.phase_file.with_suffix(self.args.phase_file.suffix + ".tmp")
        temporary.write_text(name + "\n")
        temporary.replace(self.args.phase_file)
        now = time.monotonic_ns()
        fresh = self.poses and all(0 <= now - p.sample_ns <= 500_000_000 for p in self.poses.values())
        row = dict(phase=name, host_monotonic_ns=now,
                   source_sim_time_s=self.source_s if fresh else None)
        self.summary["phase_requests"].append(row)
        self.emit("phase_requested", **row)

    def send(self, i, message, **metadata):
        payload = message.pack(self.tx, force_mavlink1=False)
        self.tx.seq = (self.tx.seq + 1) % 256
        return self.gcs.dispatch_frame(i, payload, **metadata)

    def long(self, i, command, params):
        return self.send(i, self.tx.command_long_encode(i, 1, command, 0, *params))

    def pump(self, *, strict_pose=True, observe=True):
        self.gcs.step()
        if not self.gcs.running:
            raise ScenarioError("GCS service interrupted")
        now = time.monotonic_ns()
        if now - self.last_heartbeat_ns >= 1_000_000_000:
            self.last_heartbeat_ns = now
            for i in UAV_IDS:
                try:
                    self.send(i, self.tx.heartbeat_encode(self.mav.MAV_TYPE_GCS,
                              self.mav.MAV_AUTOPILOT_INVALID, 0, 0, self.mav.MAV_STATE_ACTIVE))
                except (ProtocolError, BlockingIOError):
                    pass
        try:
            self.source_s, self.poses = self.reader.read()
        except ScenarioError:
            if strict_pose:
                raise
        for i, tail in self.tails.items():
            for row in tail.read():
                self.observer.admission(row)
                landing_uav = self.land_commands.get(row.get("command_id"))
                if row.get("event") == "uart_admitted" and landing_uav in self.landing_traces:
                    self.landing_traces[landing_uav].admit(row["command_id"], row["host_monotonic_ns"])
                if row.get("event") == "autopilot_mode_observed":
                    self.onboard_mode[i] = row
                    if row.get("mode") in ("BRAKE", "LAND"):
                        self.summary["mode_observations"].append(dict(row))
        within_window = (self.observation_end_source_s is None
                         or (self.source_s is not None and self.source_s < self.observation_end_source_s))
        if observe and self.poses and within_window:
            self.observer.observe(self.poses, now)
            if self.phase_name == "experiment_fault":
                for i, pose in self.poses.items():
                    mode = self.onboard_mode.get(i, {})
                    if (mode.get("mode") != "BRAKE"
                            or not 0 <= now - mode.get("host_monotonic_ns", 0) <= 2_500_000_000):
                        self.brake_regions.pop(i, None)
                        continue
                    hold = self.brake_regions.setdefault(i, RegionHold(pose.position,
                        self.args.hold_s, self.args.z_tolerance_m, self.args.xy_tolerance_m, .5))
                    if str(i) not in self.summary["failsafe_holds"] and hold.observe(pose):
                        row = dict(source_sim_time_s=pose.source_s, actual_position_m=pose.position,
                                   host_monotonic_ns=now, mode="BRAKE", hold_s=self.args.hold_s)
                        self.summary["failsafe_holds"][str(i)] = row
                        self.emit("failsafe_hold_observed", uav_id=i, **row)

    def fresh(self, i, name, age_s=2.5):
        timestamp = self.gcs.latest_message_ns[i].get(name, 0)
        return self.gcs.latest_messages[i].get(name) if 0 <= time.monotonic_ns() - timestamp <= age_s * 1e9 else None

    def wait(self, condition, timeout_s, label, retry=None, retry_s=1., strict_pose=True):
        deadline, next_retry = time.monotonic() + timeout_s, 0.
        while time.monotonic() < deadline:
            self.pump(strict_pose=strict_pose, observe=False)
            if condition():
                return
            if retry and time.monotonic() >= next_retry:
                try:
                    retry()
                except (ProtocolError, BlockingIOError):
                    pass
                next_retry = time.monotonic() + retry_s
        raise ScenarioError(f"timeout: {label}")

    def prepare(self):
        self.phase("experiment_preflight")
        def gps_ready():
            return len(self.poses) == 5 and all(0 <= time.monotonic_ns() - p.sample_ns <= 500_000_000 for p in self.poses.values()) and all(self.fresh(i, "HEARTBEAT") is not None
                and self.fresh(i, "GPS_RAW_INT") is not None and self.fresh(i, "GPS_RAW_INT").fix_type >= 3
                and self.fresh(i, "GLOBAL_POSITION_INT") is not None
                and 1 <= abs(self.fresh(i, "GLOBAL_POSITION_INT").lat / 1e7) <= 90
                and 1 <= abs(self.fresh(i, "GLOBAL_POSITION_INT").lon / 1e7) <= 180 for i in UAV_IDS)
        def streams():
            for i in UAV_IDS:
                for message_id in (self.mav.MAVLINK_MSG_ID_GLOBAL_POSITION_INT, self.mav.MAVLINK_MSG_ID_GPS_RAW_INT):
                    try:
                        self.long(i, self.mav.MAV_CMD_SET_MESSAGE_INTERVAL, [message_id, 200000., 0, 0, 0, 0, 0])
                    except (ProtocolError, BlockingIOError):
                        pass
        self.wait(gps_ready, self.args.ready_timeout_s, "five real heartbeats and GPS fixes", streams, 2., strict_pose=False)
        self.initial = dict(self.poses)
        for i in UAV_IDS:
            gpi = self.fresh(i, "GLOBAL_POSITION_INT")
            self.anchors[i] = dict(lat_int=int(gpi.lat), lon_int=int(gpi.lon),
                world_z_offset_m=self.poses[i].position[2] - gpi.relative_alt / 1000.,
                measured_relative_alt_m=gpi.relative_alt / 1000., world_position_m=self.poses[i].position,
                host_monotonic_ns=time.monotonic_ns(), source_sim_time_s=self.source_s)
        self.summary["anchors"] = self.anchors
        for i in UAV_IDS:
            self.phase(f"experiment_takeoff_uav{i}")
            self.wait(lambda i=i: self.guided_ready(i), 30, f"uav{i} actual GUIDED")
            def arm(i=i):
                if self.guided_ready(i):
                    self.long(i, self.mav.MAV_CMD_COMPONENT_ARM_DISARM, [1, 0, 0, 0, 0, 0, 0])
            self.wait(lambda i=i: self.armed(i) and self.guided_ready(i),
                      self.args.arm_timeout_s, f"uav{i} armed heartbeat", arm)
            self.summary["uavs"][str(i)]["armed"] = True
            relative = self.args.base_altitude_m + (i - 1) * self.args.altitude_separation_m
            target = (*self.initial[i].position[:2], self.anchors[i]["world_z_offset_m"] + relative)
            hold = RegionHold(target, self.args.hold_s, self.args.z_tolerance_m,
                              self.args.xy_tolerance_m, self.args.hold_speed_mps)
            takeoff_goal = Goal(f"takeoff:uav{i}", i, time.monotonic_ns(), self.source_s, relative, target)
            def takeoff(i=i, relative=relative):
                if not self.guided_ready(i):
                    return
                climb = self.poses[i].position[2] - self.initial[i].position[2]
                if not self.armed(i):
                    if climb >= 2:
                        raise ScenarioError(f"uav{i} disarmed during observed takeoff")
                    self.long(i, self.mav.MAV_CMD_COMPONENT_ARM_DISARM, [1, 0, 0, 0, 0, 0, 0])
                elif climb < 2:
                    self.long(i, self.mav.MAV_CMD_NAV_TAKEOFF, [0, 0, 0, 0, 0, 0, relative])
                else:
                    # BRAKE may have interrupted the climb after initial lift.
                    # Restore native GUIDED position control to the same goal.
                    self.send(i, self.setpoint(i, takeoff_goal), target_position_m=target, target_frame="ENU")
            self.wait(lambda i=i: self.armed(i) and self.guided_ready(i) and hold.observe(self.poses[i]),
                      self.args.takeoff_timeout_s, f"uav{i} measured takeoff and hold", takeoff, 2.)
            self.summary["uavs"][str(i)]["takeoff"] = True
            self.emit("takeoff_observed", uav_id=i, actual_position_m=self.poses[i].position,
                      source_sim_time_s=self.poses[i].source_s, target_position_m=target, target_frame="ENU")

    def armed(self, i):
        heartbeat = self.fresh(i, "HEARTBEAT")
        return heartbeat is not None and bool(heartbeat.base_mode & self.mav.MAV_MODE_FLAG_SAFETY_ARMED)

    def guided_ready(self, i: int) -> bool:
        now = time.monotonic_ns()
        grant = self.gcs.grant
        if grant is None or not grant.valid_from_ns <= now < grant.valid_until_ns:
            return False
        heartbeat = self.fresh(i, "HEARTBEAT")
        produced = self.gcs.latest_message_ns[i].get("HEARTBEAT", 0)
        if (self.mode_epoch.get(i) == grant.epoch and heartbeat is not None
                and heartbeat.custom_mode == self.modes["GUIDED"]
                and produced >= self.mode_request_ns.get(i, 1 << 63)):
            return True
        if self.mode_epoch.get(i) != grant.epoch or now - self.mode_request_ns.get(i, 0) >= 1_000_000_000:
            try:
                command = self.send(i, self.tx.set_mode_encode(i,
                    self.mav.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED, self.modes["GUIDED"]))
            except (ProtocolError, BlockingIOError):
                return False
            self.mode_request_ns[i], self.mode_epoch[i] = command.created_ns, grant.epoch
        return False

    def setpoint(self, i: int, goal: Goal):
        anchor = self.anchors[i]
        return self.tx.set_position_target_global_int_encode(
            int(time.monotonic() * 1000) & 0xffffffff, i, 1,
            self.mav.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT, 3576,
            anchor["lat_int"], anchor["lon_int"], goal.relative_alt_m,
            0, 0, 0, 0, 0, 0, 0, 0)

    def refresh(self, goals: dict[int, Goal], *, scheduled_ns: int):
        now = time.monotonic_ns()
        self.refresh_counter += 1
        grant = self.gcs.grant
        for i, goal in goals.items():
            request_id = f"refresh{self.refresh_counter}:uav{i}"
            self.emit("control_refresh_offered", uav_id=i, destination_id=i,
                      request_id=request_id, goal_id=goal.goal_id,
                      request_created_host_monotonic_ns=now,
                      scheduled_host_monotonic_ns=scheduled_ns, source_sim_time_s=self.source_s,
                      goal_created_host_monotonic_ns=goal.created_ns,
                      target_position_m=goal.target, target_frame="ENU")
            command, status = None, "no_active_issued_lease"
            try:
                if grant is None or not grant.valid_from_ns <= now < grant.valid_until_ns:
                    raise ProtocolError(status)
                if not self.guided_ready(i):
                    status = "waiting_fresh_GUIDED_heartbeat"
                else:
                    command = self.send(i, self.setpoint(i, goal), target_position_m=goal.target, target_frame="ENU")
                    self.observer.register(command, goal)
                    status = "sent_to_native_radio"
                    self.emit("control_refresh_created", command=command, goal_id=goal.goal_id,
                              request_id=request_id,
                              source_sim_time_s=self.source_s,
                              goal_created_host_monotonic_ns=goal.created_ns,
                              goal_created_source_sim_time_s=goal.created_source_s,
                              target_relative_alt_m=goal.relative_alt_m)
            except (ProtocolError, BlockingIOError) as exc:
                status = str(exc)
            if command is None:
                self.emit("control_refresh_unavailable", uav_id=i, destination_id=i,
                          request_id=request_id, goal_id=goal.goal_id, reason=status,
                          request_created_host_monotonic_ns=now, source_sim_time_s=self.source_s)
            self.csv.writerow(dict(host_monotonic_ns=now, source_sim_time_s=self.source_s,
                request_id=request_id, scheduled_host_monotonic_ns=scheduled_ns,
                phase=self.phase_name, uav_id=i, goal_id=goal.goal_id, goal_created_host_monotonic_ns=goal.created_ns,
                command_id=command.command_id if command else "", epoch=command.epoch if command else "",
                leader_id=command.leader_id if command else "", target_world_z_m=goal.target[2],
                target_relative_alt_m=goal.relative_alt_m, send_status=status))
        self.csv_handle.flush()

    def measure(self):
        start_source, start_ns = self.source_s, time.monotonic_ns()
        self.summary["measurement_start_source_sim_time_s"] = start_source
        self.summary["measurement_start_host_monotonic_ns"] = start_ns
        self.observation_end_source_s = start_source + self.args.duration_s
        self.summary["measurement_end_source_sim_time_s"] = self.observation_end_source_s
        refresh_end_ns = start_ns + int(self.args.duration_s * 1e9)
        self.summary["measurement_refresh_schedule_end_host_monotonic_ns"] = refresh_end_ns
        current_goal, goals, next_refresh = -1, {}, start_ns
        while self.source_s - start_source < self.args.duration_s:
            self.pump()
            elapsed = self.source_s - start_source
            if elapsed >= self.args.duration_s:
                break  # a new source sample may cross the half-open measurement window
            if time.monotonic_ns() - start_ns > int((self.args.duration_s * 1.1 + 10) * 1e9):
                raise ScenarioError("measurement failed to advance at live rate")
            phase = ("experiment_normal" if elapsed < self.args.fault_start_s else
                     "experiment_fault" if elapsed < self.args.fault_end_s else "experiment_recovery")
            self.phase(phase)
            index = int(elapsed // self.args.task_period_s)
            if index != current_goal:
                current_goal = index
                for i in UAV_IDS:
                    relative = (self.args.base_altitude_m + (i - 1) * self.args.altitude_separation_m
                                + (self.args.altitude_step_m if index % 2 == 0 else 0))
                    goal = Goal(f"goal{index}:uav{i}", i, time.monotonic_ns(), self.source_s, relative,
                        (*self.initial[i].position[:2], self.anchors[i]["world_z_offset_m"] + relative))
                    goals[i] = goal
                    self.emit("goal_created", uav_id=i, goal_id=goal.goal_id,
                              goal_created_host_monotonic_ns=goal.created_ns,
                              source_sim_time_s=self.source_s, target_position_m=goal.target,
                              target_frame="ENU", target_relative_alt_m=relative)
            if next_refresh < refresh_end_ns and time.monotonic_ns() >= next_refresh:
                self.refresh(goals, scheduled_ns=next_refresh)
                next_refresh += int(self.args.command_period_s * 1e9)
        self.summary["measurement_duration_source_s"] = self.source_s - start_source
        self.summary["measurement_duration_wall_s"] = (time.monotonic_ns() - start_ns) / 1e9
        self.summary["measurement_integral_rtf"] = self.summary["measurement_duration_source_s"] / self.summary["measurement_duration_wall_s"]
        self.summary["completed_goals"] = list(self.observer.completed.values())

    def land(self):
        self.phase("experiment_landing")  # native fault is explicitly removed for bounded cleanup
        self.landing_traces = {i: LandingTrace(self.poses[i], self.initial[i].position[2],
                    ground_tolerance=self.args.land_z_tolerance_m,
                    max_descent_mps=self.args.max_landing_descent_mps)
                for i in UAV_IDS if i in self.initial and i in self.poses and self.summary["uavs"][str(i)]["armed"]}
        next_send, deadline = 0., time.monotonic() + self.args.land_timeout_s
        while time.monotonic() < deadline:
            self.pump(strict_pose=False, observe=False)
            if time.monotonic() >= next_send:
                for i in UAV_IDS:
                    try:
                        command = self.send(i, self.tx.set_mode_encode(i, self.mav.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
                                                                     self.modes["LAND"]))
                        self.land_commands[command.command_id] = i
                    except (ProtocolError, BlockingIOError):
                        pass
                next_send = time.monotonic() + 1
            for i in UAV_IDS:
                hb, pose = self.fresh(i, "HEARTBEAT"), self.poses.get(i)
                if hb is None or pose is None:
                    continue
                fresh_pose = 0 <= time.monotonic_ns() - pose.sample_ns <= 500_000_000
                trace = self.landing_traces.get(i)
                if trace is not None and fresh_pose:
                    trace.observe(pose, heartbeat_ns=self.gcs.latest_message_ns[i]["HEARTBEAT"],
                                  mode_land=hb.custom_mode == self.modes["LAND"],
                                  armed=bool(hb.base_mode & self.mav.MAV_MODE_FLAG_SAFETY_ARMED))
                    for event in trace.events:
                        self.emit(event, uav_id=i, actual_position_m=pose.position,
                                  source_sim_time_s=pose.source_s, admitted_command_id=trace.command_id,
                                  reason=trace.terminal_reason)
                    trace.events.clear()
                    if trace.completed:
                        self.summary["uavs"][str(i)].update(landed=True, auto_disarm=True)
                    if trace.terminal_reason:
                        self.summary["uavs"][str(i)]["terminal_outcome"] = trace.terminal_reason
            all_disarmed = all(self.fresh(i, "HEARTBEAT") is not None and not self.armed(i) for i in UAV_IDS)
            if all_disarmed:
                incomplete = [i for i, trace in self.landing_traces.items() if not trace.completed]
                if any(trace.terminal_reason for trace in self.landing_traces.values()):
                    raise ScenarioError(f"terminal disarm without controlled LAND evidence: {incomplete}")
                if not incomplete:
                    return
        raise ScenarioError("landing/automatic disarm not observed for all five")

    def run(self):
        error = None
        try:
            self.prepare()
            self.measure()
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            self.emit("experiment_failed", reason=error)
        try:
            self.land()
        except Exception as exc:
            error = (error + "; " if error else "") + f"cleanup {type(exc).__name__}: {exc}"
        self.summary["completed_goals"] = list(self.observer.completed.values())
        self.summary["error"] = error
        self.summary["status"] = "completed" if error is None else "failed"
        self.summary["task_failures_are_outcomes"] = True
        completed = self.summary["completed_goals"]
        self.summary["criteria"] = dict(
            five_uav_lifecycle=all(all(row[k] for k in ("armed", "takeoff", "landed", "auto_disarm"))
                                   for row in self.summary["uavs"].values()),
            two_completed_goals_each=all(sum(row["uav_id"] == i for row in completed) >= 2 for i in UAV_IDS),
            two_completed_target_levels_each=all(len({round(row["target_position_m"][2], 3)
                                    for row in completed if row["uav_id"] == i}) >= 2 for i in UAV_IDS),
            live_integral_rtf_target=.98 <= self.summary.get("measurement_integral_rtf", 0) <= 1.02,
            total_GCS_hold_all_five=(len(self.summary["failsafe_holds"]) == 5
                                    if self.args.fault_profile == "total_GCS" else None))
        self.phase("experiment_done" if error is None else "experiment_failed")
        output = self.args.run_dir / "metrics/relay_experiment_summary.json"
        output.write_text(json.dumps(self.summary, indent=2, allow_nan=False) + "\n")
        self.csv_handle.close()
        self.gcs.close()
        self.emit.stream.close()
        if error:
            raise ScenarioError(error)
        return self.summary


def validate_experiment_profile(args):
    """Keep the reviewed main mission and the separate sensitivity design explicit."""
    if args.experiment_profile == "sensitivity":
        if args.pilot:
            raise ValueError("sensitivity is a separate frozen study, not --pilot")
        duration = args.fault_end_s - args.fault_start_s
        if (args.fault_profile != "selective" or args.duration_s != 180
                or not 40 <= args.fault_start_s <= 60
                or not any(math.isclose(duration, value, rel_tol=0, abs_tol=1e-9)
                           for value in (.5, 2., 10.))):
            raise ValueError("sensitivity requires selective, 180 s, start 40..60 s, loss 0.5/2/10 s")
    elif not args.pilot and (args.duration_s, args.fault_start_s, args.fault_end_s) != (180, 45, 105):
        raise ValueError("changed phase windows require --pilot; main series is 45/105/180 s")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--node-state", type=Path, required=True)
    parser.add_argument("--phase-file", type=Path, required=True)
    parser.add_argument("--policy", choices=("fixed", "failover"), required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--initial-master", type=int, choices=UAV_IDS, default=1)
    parser.add_argument("--radio-watchdog-file", type=Path)
    parser.add_argument("--watchdog-s", type=float, default=.3)
    parser.add_argument("--fault-profile", choices=("none", "selective", "total_GCS"),
                        default=os.environ.get("BAS_NATIVE_FAULT_PROFILE", "none"))
    parser.add_argument("--duration-s", type=float, default=180)
    parser.add_argument("--fault-start-s", type=float, default=45)
    parser.add_argument("--fault-end-s", type=float, default=105)
    parser.add_argument("--command-period-s", type=float, default=1)
    parser.add_argument("--task-period-s", type=float, default=20)
    parser.add_argument("--pilot", action="store_true")
    parser.add_argument("--experiment-profile", choices=("main", "sensitivity"), default="main")
    parser.add_argument("--base-altitude-m", type=float, default=8)
    parser.add_argument("--altitude-separation-m", type=float, default=4)
    parser.add_argument("--altitude-step-m", type=float, default=4)
    parser.add_argument("--hold-s", type=float, default=2)
    parser.add_argument("--z-tolerance-m", type=float, default=1.5)
    parser.add_argument("--xy-tolerance-m", type=float, default=3)
    parser.add_argument("--hold-speed-mps", type=float, default=1)
    parser.add_argument("--ready-timeout-s", type=float, default=120)
    parser.add_argument("--arm-timeout-s", type=float, default=120)
    parser.add_argument("--takeoff-timeout-s", type=float, default=90)
    parser.add_argument("--land-timeout-s", type=float, default=180)
    parser.add_argument("--land-z-tolerance-m", type=float, default=.75)
    parser.add_argument("--max-landing-descent-mps", type=float, default=3.)
    args = parser.parse_args(argv)
    args.run_id = args.run_id or args.run_dir.name
    if not 0 < args.fault_start_s < args.fault_end_s < args.duration_s:
        parser.error("require 0 < fault-start < fault-end < duration")
    try:
        validate_experiment_profile(args)
    except ValueError as exc:
        parser.error(str(exc))
    if min(args.command_period_s, args.task_period_s, args.hold_s, args.z_tolerance_m,
           args.xy_tolerance_m, args.hold_speed_mps, args.base_altitude_m) <= 0:
        parser.error("periods, altitudes and hold tolerances must be positive")
    if os.environ.get("BAS_SIMULATION_MODE", "realtime") != "realtime":
        parser.error("relay experiment requires realtime host-monotonic mode")
    try:
        print(json.dumps(RelayExperiment(args).run(), indent=2))
    except Exception as exc:
        output = args.run_dir / "metrics/relay_experiment_summary.json"
        if not output.exists():
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(dict(status="failed", run_id=args.run_id,
                stage="execution_or_initialization", error=f"{type(exc).__name__}: {exc}"), indent=2) + "\n")
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
