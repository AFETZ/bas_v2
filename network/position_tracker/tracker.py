#!/usr/bin/env python3
"""Publish measured Gazebo/ROS radio state; config snapshots are offline only."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import uuid
import threading
import signal
from contextlib import contextmanager
from types import SimpleNamespace
from collections import deque, Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - dependency check covers this
    raise SystemExit("PyYAML is required: python3 -m pip install PyYAML") from exc


ROOT_DIR = Path(__file__).resolve().parents[2]
# The runner invokes this file directly with only the Gazebo site-packages in
# PYTHONPATH. Resolve the shared project writer without altering that environment.
if __package__ in (None, ''):
    sys.path.insert(0, str(ROOT_DIR))
from network.jsonl import AsyncJsonlWriter

DEFAULT_SCENARIO = ROOT_DIR / "network/config/scenario_5uav.yaml"
DEFAULT_JAMMERS = ROOT_DIR / "network/config/jammers.yaml"
JOURNAL_MODE = 'bounded_async_jsonl_v1'


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        data = yaml.safe_load(stream) or {}
    if not isinstance(data, dict):
        raise ValueError(f"YAML file must contain a mapping: {path}")
    return data


def normalize_position(value: Any, fallback: list[float] | None = None) -> list[float]:
    if value is None:
        if fallback is None:
            raise ValueError("position is missing")
        value = fallback
    if not isinstance(value, list) or len(value) < 3:
        raise ValueError(f"position must have at least three elements: {value!r}")
    return [float(value[0]), float(value[1]), float(value[2])]


def command_post_node(scenario: dict[str, Any], scenario_source: str) -> dict[str, Any]:
    cp = dict(scenario.get("command_post") or {})
    return {
        "id": cp.get("id", "cp"),
        "role": cp.get("role", "command_post"),
        "position_m": normalize_position(cp.get("position_m"), [0.0, 0.0, 20.0]),
        "orientation_quat_xyzw": cp.get("orientation_quat_xyzw", [0.0, 0.0, 0.0, 1.0]),
        "velocity_enu_mps": [0.0, 0.0, 0.0],
        "angular_velocity_enu_radps": [0.0, 0.0, 0.0],
        "antenna": cp.get("antenna", "omni"),
        "source_topic": f"{scenario_source}:command_post",
        "stale": False,
    }


def jammer_emitters(
    jammers: dict[str, Any], enabled_only: bool = True, jammers_source: str = "jammers_config"
) -> list[dict[str, Any]]:
    emitters: list[dict[str, Any]] = []
    for jammer in jammers.get("jammers", []):
        if enabled_only and not bool(jammer.get("enabled", False)):
            continue
        emitters.append(
            {
                "id": jammer["id"],
                "position_m": normalize_position(jammer.get("position_m")),
                "orientation_quat_xyzw": jammer.get("orientation_quat_xyzw", [0.0, 0.0, 0.0, 1.0]),
                "center_hz": float(jammer.get("center_hz", 2.4e9)),
                "bandwidth_hz": float(jammer.get("bandwidth_hz", 1e6)),
                "power_dbm": float(jammer.get("power_dbm", 40.0)),
                "duty_cycle": float(jammer.get("duty_cycle", 1.0)),
                "antenna": jammer.get("antenna", "omni"),
                "source_topic": jammers_source,
            }
        )
    return emitters


def config_state(
    scenario: dict[str, Any],
    jammers: dict[str, Any],
    scenario_source: str,
    jammers_source: str,
) -> dict[str, Any]:
    nodes = [command_post_node(scenario, scenario_source)]
    for robot in scenario.get("robots", []):
        launch_position = robot.get("position", [0.0, 0.0, 0.0])
        nodes.append(
            {
                "id": robot["name"],
                "role": robot.get("role", "uav"),
                "position_m": normalize_position(
                    robot.get("nominal_radio_position_m"),
                    [launch_position[0], launch_position[1], launch_position[2]],
                ),
                "orientation_quat_xyzw": [0.0, 0.0, 0.0, 1.0],
                "antenna": robot.get("antenna", "omni"),
                "source_topic": f"{scenario_source}:nominal_radio_position_m",
                "stale": False,
            }
        )
    return {
        "type": "node_state",
        "time_s": time.time(),
        "wall_time": datetime.now(timezone.utc).isoformat(),
        "source": "scenario_config",
        "nodes": nodes,
        "emitters": jammer_emitters(jammers, enabled_only=True, jammers_source=jammers_source),
        "missing_nodes": [],
        "stale_nodes": [],
    }


def write_state(output_json: Path, output_jsonl: Path | None, state: dict[str, Any]) -> None:
    output_json.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_json.with_suffix(output_json.suffix + ".tmp")
    # This atomic file is a frequently read machine interface with bounded pose
    # history. Whitespace adds reader CPU cost without changing its contract.
    temp_path.write_text(json.dumps(state, separators=(",", ":"), sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temp_path, output_json)
    if output_jsonl is not None:
        output_jsonl.parent.mkdir(parents=True, exist_ok=True)
        with output_jsonl.open("a", encoding="utf-8") as stream:
            # Live readers need a bounded history; archive each latest sample once.
            logged = _archive_state(state)
            stream.write(json.dumps(logged, separators=(",", ":"), allow_nan=False) + "\n")


def _archive_state(state):
    return dict(state, nodes=[{k:v for k,v in node.items() if k != 'history'} for node in state['nodes']])


def _publication_timing():
    return dict(completed_publications=0,
        scope='host_monotonic_wall_and_thread_cpu; live rows contain previous completed stage observations')


def _observe_publication_stage(timing, stage, wall, cpu):
    for kind, elapsed in (('wall', time.monotonic_ns()-wall), ('thread_cpu', time.thread_time_ns()-cpu)):
        timing[f'last_{stage}_{kind}_ns'] = elapsed
        name = f'maximum_{stage}_{kind}_ns'
        timing[name] = max(timing.get(name, 0), elapsed)
    timing[f'{stage}_calls'] = timing.get(f'{stage}_calls', 0)+1


def _publish_live_state(output_json, state, journal, timing):
    # Snapshotting is complete before this function; no odometry/state mutex is
    # held across atomic-file I/O, encoding or the bounded archive enqueue.
    if journal is not None:
        journal.check()
    state['journal_mode'] = JOURNAL_MODE if journal is not None else 'disabled'
    state['publication_timing'] = dict(timing)
    wall, cpu = time.monotonic_ns(), time.thread_time_ns()
    try:
        write_state(output_json, None, state)
    finally:
        _observe_publication_stage(timing, 'atomic_publish', wall, cpu)
    if journal is not None:
        wall, cpu = time.monotonic_ns(), time.thread_time_ns()
        try:
            journal.write(json.dumps(_archive_state(state), separators=(',', ':'), allow_nan=False))
        finally:
            _observe_publication_stage(timing, 'archive_enqueue', wall, cpu)
    timing['completed_publications'] += 1


@contextmanager
def _live_journal(output_json, output_jsonl, state, timing):
    if output_jsonl is None:
        yield None
        return
    output_jsonl.parent.mkdir(parents=True, exist_ok=True)
    audit_path = Path(str(output_jsonl)+'.writer.json')
    if audit_path.exists():
        raise FileExistsError(f'Position journal audit already exists: {audit_path}')
    journal = AsyncJsonlWriter(output_jsonl)
    producer_error = close_error = None
    audit_owned = False

    def audit(status, *, initial=False):
        nonlocal audit_owned
        value = dict(journal.snapshot(), schema=1, journal_mode=JOURNAL_MODE,
            source=state.source, session_id=state.session, status=status,
            live_state_path=str(output_json), producer_error=producer_error,
            close_error=close_error, publication_timing=dict(timing))
        encoded = json.dumps(value, indent=2, allow_nan=False)+'\n'
        if initial:
            with audit_path.open('x', encoding='utf-8') as stream:
                audit_owned = True
                stream.write(encoded)
        else:
            temporary = Path(str(audit_path)+'.tmp')
            temporary.write_text(encoded, encoding='utf-8')
            os.replace(temporary, audit_path)

    try:
        # An interrupted/killed producer leaves recording, never a successful
        # closed artifact. The archive itself is exclusively owned by the writer.
        audit('recording', initial=True)
        yield journal
    except BaseException as error:
        producer_error = f'{type(error).__name__}: {error}'
        raise
    finally:
        failure = None
        try:
            journal.close()
        except Exception as error:
            failure = error
            close_error = f'{type(error).__name__}: {error}'
        if audit_owned:
            audit('failed' if producer_error is not None or close_error is not None else 'closed')
        if failure is not None and producer_error is None:
            raise failure


def run_config_once(args: argparse.Namespace) -> int:
    scenario_path = Path(args.scenario)
    jammers_path = Path(args.jammers_config)
    scenario = load_yaml(scenario_path)
    jammers = load_yaml(jammers_path)
    state = config_state(scenario, jammers, scenario_path.name, jammers_path.name)
    output_json = Path(args.output_json).resolve()
    output_jsonl = Path(args.output_jsonl).resolve() if args.output_jsonl else None
    write_state(output_json, output_jsonl, state)
    print(json.dumps(state, separators=(",", ":"), allow_nan=False))
    return 0


def rotate_vector(q: list[float], vector: list[float]) -> list[float]:
    """ROS Odometry twist is in child_frame_id; radio velocity is world ENU."""
    x, y, z, w = q
    vx, vy, vz = vector
    tx, ty, tz = 2*(y*vz-z*vy), 2*(z*vx-x*vz), 2*(x*vy-y*vx)
    return [vx+w*tx+y*tz-z*ty, vy+w*ty+z*tx-x*tz, vz+w*tz+x*ty-y*tx]


class LiveState:
    """Bounded measured history; clocks reset by restarting the whole run."""
    def __init__(self, scenario: dict[str, Any], stale_after_s: float = .5, simulation_mode: str = "realtime"):
        self.simulation_mode = simulation_mode
        self.scenario = scenario
        self.stale_ns = int(stale_after_s*1e9)
        self.session = uuid.uuid4().hex
        self.history = {r['name']: deque(maxlen=32) for r in scenario['robots']}
        self.clock_s: float | None = None
        self.clock_rx_ns = 0
        self.fault: str | None = None
        self.rejected = 0
        self.rejection_reasons = Counter()
        self.source = 'ros_odometry'

    def clock(self, sim_s: float, now_ns: int) -> None:
        if not math.isfinite(sim_s) or sim_s < 0:
            self.fault = 'invalid_gazebo_clock'
        elif self.clock_s is not None and sim_s < self.clock_s:
            self.fault = 'gazebo_clock_reset_restart_required'
        elif self.clock_s is None or sim_s > self.clock_s:
            self.clock_s, self.clock_rx_ns = sim_s, now_ns

    def odometry(self, name: str, msg: Any, now_ns: int) -> bool:
        pose, twist = msg.pose.pose, msg.twist.twist
        stamp = msg.header.stamp.sec + msg.header.stamp.nanosec/1e9
        q = [float(getattr(pose.orientation, k)) for k in ('x','y','z','w')]
        p = [float(getattr(pose.position,k)) for k in ('x','y','z')]
        v = [float(getattr(twist.linear,k)) for k in ('x','y','z')]
        omega = [float(getattr(twist.angular,k)) for k in ('x','y','z')]
        history = self.history[name]
        valid_frames = str(msg.header.frame_id).strip('/') in ('odom', f'{name}/odom')
        valid_frames &= str(msg.child_frame_id).strip('/') in ('base_link', f'{name}/base_link')
        reason = None
        if not valid_frames:
            reason = 'frame'
        elif not all(math.isfinite(x) for x in [stamp,*q,*p,*v]) or not 1e-12 <= sum(x*x for x in q) < float("inf"):
            reason = 'nonfinite_or_orientation'
        elif self.clock_s is None:
            reason = 'clock_missing'
        elif abs(stamp-self.clock_s) > self.stale_ns/1e9:
            reason = 'source_clock_skew'
        elif self.simulation_mode == "realtime" and now_ns-self.clock_rx_ns > self.stale_ns:
            reason = 'clock_stale'
        elif history and stamp <= history[-1]['source_sim_time_s']:
            reason = 'out_of_order'
        if reason:
            self.rejected += 1
            self.rejection_reasons[reason] += 1
            return False
        norm = math.sqrt(sum(x*x for x in q))
        q = [x/norm for x in q]
        # Age also includes source-clock lag at reception. Never label old samples
        # with the publication time, or extrapolate physical state in this adapter.
        sample_ns = now_ns - int(max(0., self.clock_s-stamp)*1e9)
        history.append(dict(id=name, role='uav', position_m=p, orientation_quat_xyzw=q,
            velocity_enu_mps=rotate_vector(q,v), angular_velocity_enu_radps=rotate_vector(q,omega) if all(math.isfinite(x) for x in omega) else None,
            source_sim_time_s=stamp, received_monotonic_ns=now_ns, sample_monotonic_ns=sample_ns,
            source_frame=msg.header.frame_id, child_frame=msg.child_frame_id,
            source_topic=f'/{name}/odometry', antenna='omni', stale=False))
        return True

    def snapshot(self, now_ns: int) -> dict[str, Any]:
        nodes = [command_post_node(self.scenario, 'scenario_config')]
        missing, stale = [], []
        for name, history in self.history.items():
            if not history:
                missing.append(name)
                continue
            node = dict(history[-1])
            node['sample_age_ms'] = ((self.clock_s-node['source_sim_time_s'])*1000
                if self.simulation_mode == 'lockstep' else (now_ns-node['sample_monotonic_ns'])/1e6)
            node['stale'] = node['sample_age_ms'] > self.stale_ns/1e6
            if node['stale']:
                stale.append(name)
            node['history'] = list(history)
            nodes.append(node)
        return dict(type='node_state', schema_version=2, session_id=self.session,
            simulation_mode=self.simulation_mode, coordinate_frame='ENU', source=self.source, time_s=time.time(),
            published_monotonic_ns=now_ns, source_sim_time_s=self.clock_s,
            clock_received_monotonic_ns=self.clock_rx_ns, fault=self.fault,
            nodes=nodes, missing_nodes=missing, stale_nodes=stale, rejected_samples=self.rejected,
            rejection_reasons=dict(self.rejection_reasons))


def _publish_periodically(stop, period_ns: int, publish) -> None:
    """Publish on absolute host deadlines; missed ticks are skipped, not replayed."""
    deadline_ns = time.monotonic_ns()
    while not stop.is_set():
        publish()
        deadline_ns += period_ns
        now_ns = time.monotonic_ns()
        if deadline_ns <= now_ns:
            deadline_ns += ((now_ns - deadline_ns) // period_ns + 1) * period_ns
        if stop.wait((deadline_ns - now_ns) / 1e9):
            break


def run_gazebo_tracker(args: argparse.Namespace) -> int:
    """Read the same native odometry without a DDS bridge in the clock path."""
    rate = args.rate_hz
    if type(rate) not in (int, float) or not 0 < rate <= sys.float_info.max:
        raise ValueError('rate_hz must be a finite positive number')
    period_s = 1 / rate
    if not 0 < period_s <= threading.TIMEOUT_MAX:
        raise ValueError('rate_hz period exceeds the interruptible wait range')
    period_ns = round(period_s * 1e9)
    if period_ns < 1:
        raise ValueError('rate_hz period must be at least one monotonic nanosecond')
    from gz.transport13 import Node
    from gz.msgs10.clock_pb2 import Clock
    from gz.msgs10.odometry_pb2 import Odometry
    state = LiveState(load_yaml(Path(args.scenario)), args.stale_after_s,
                      os.environ.get('BAS_SIMULATION_MODE', 'realtime'))
    state.source = 'gazebo_odometry'
    mutex, stop = threading.Lock(), threading.Event()
    output_json = Path(args.output_json).resolve()
    output_jsonl = Path(args.output_jsonl).resolve() if args.output_jsonl else None
    timing = _publication_timing()
    def receive_clock(msg):
        with mutex:
            state.clock(msg.sim.sec + msg.sim.nsec / 1e9, time.monotonic_ns())
    def receive_pose(name, msg):
        fields = {item.key: list(item.value) for item in msg.header.data}
        adapted = SimpleNamespace(
            header=SimpleNamespace(stamp=SimpleNamespace(sec=msg.header.stamp.sec,
                nanosec=msg.header.stamp.nsec), frame_id=fields.get('frame_id', [''])[0]),
            child_frame_id=fields.get('child_frame_id', [''])[0],
            pose=SimpleNamespace(pose=msg.pose), twist=SimpleNamespace(twist=msg.twist))
        with mutex:
            if state.odometry(name, adapted, time.monotonic_ns()):
                state.history[name][-1]['source_topic'] = f'/model/{name}/odometry'
    with _live_journal(output_json, output_jsonl, state, timing) as journal:
        node = Node()
        try:
            if not node.subscribe(Clock, f'/world/{args.world_name}/clock', receive_clock):
                raise RuntimeError('cannot subscribe to Gazebo world clock')
            for name in state.history:
                if not node.subscribe(Odometry, f'/model/{name}/odometry',
                                      lambda msg, name=name: receive_pose(name, msg)):
                    raise RuntimeError(f'cannot subscribe to Gazebo odometry: {name}')
            signal.signal(signal.SIGTERM, lambda *_: stop.set())
            signal.signal(signal.SIGINT, lambda *_: stop.set())
            def publish():
                with mutex:
                    value = state.snapshot(time.monotonic_ns())
                _publish_live_state(output_json, value, journal, timing)
            _publish_periodically(stop, period_ns, publish)
        finally:
            # Release native callbacks before archive draining and interpreter
            # teardown, including failed subscriptions and writer errors.
            for topic in node.subscribed_topics():
                node.unsubscribe(topic)
            del node
    return 0


def run_ros_tracker(args: argparse.Namespace) -> int:
    try:
        import rclpy
        from nav_msgs.msg import Odometry
        from rclpy.node import Node
        from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
        from rosgraph_msgs.msg import Clock
    except ImportError as exc:
        print(
            "ERROR ROS 2 Python packages are required for live tracking. "
            "Source the ROS 2 workspace or run inside the project container. "
            "Use --from-config-once only for offline smoke tests.",
            file=sys.stderr,
        )
        return 2

    scenario_path = Path(args.scenario)
    jammers_path = Path(args.jammers_config)
    scenario = load_yaml(scenario_path)
    jammers = load_yaml(jammers_path)
    scenario_source = scenario_path.name
    jammers_source = jammers_path.name
    output_json = Path(args.output_json).resolve()
    output_jsonl = Path(args.output_jsonl).resolve() if args.output_jsonl else None
    stale_after_s = float(args.stale_after_s)
    rate_hz = float(args.rate_hz)
    live_state = LiveState(scenario, stale_after_s, os.environ.get("BAS_SIMULATION_MODE", "realtime"))
    timing = _publication_timing()

    class RadioPositionTracker(Node):
        def __init__(self) -> None:
            super().__init__("network_radio_position_tracker")
            self.state = live_state
            self.robot_names = [robot["name"] for robot in scenario.get("robots", [])]
            lockstep = self.state.simulation_mode == 'lockstep'
            qos = QoSProfile(depth=32 if lockstep else 1,
                             reliability=ReliabilityPolicy.RELIABLE if lockstep else ReliabilityPolicy.BEST_EFFORT,
                             history=HistoryPolicy.KEEP_LAST)
            self.create_subscription(Clock, f'/{self.robot_names[0]}/clock',
                lambda msg: self.state.clock(msg.clock.sec+msg.clock.nanosec/1e9, time.monotonic_ns()), qos)
            for name in self.robot_names:
                topic = f"/{name}/odometry"
                self.create_subscription(
                    Odometry,
                    topic,
                    lambda msg, robot_name=name, source_topic=topic: self._on_odometry(
                        robot_name, source_topic, msg
                    ),
                    qos,
                )
                self.get_logger().info(f"tracking {name} from {topic}")
            self.create_timer(1.0 / max(rate_hz, 0.1), self._publish_state)

        def _on_odometry(self, robot_name: str, source_topic: str, msg: Any) -> None:
            self.state.odometry(robot_name, msg, time.monotonic_ns())

        def _publish_state(self) -> None:
            state = self.state.snapshot(time.monotonic_ns())
            state['emitters'] = jammer_emitters(jammers, enabled_only=True, jammers_source=jammers_source)
            _publish_live_state(output_json, state, journal, timing)
            if state['missing_nodes'] or state['stale_nodes'] or state['fault']:
                self.get_logger().warn(
                    f"missing={state['missing_nodes']} stale={state['stale_nodes']} fault={state['fault']}"
                )

    rclpy.init(args=None)
    try:
        with _live_journal(output_json, output_jsonl, live_state, timing) as journal:
            node = RadioPositionTracker()
            try:
                rclpy.spin(node)
            finally:
                node.destroy_node()
    except KeyboardInterrupt:
        return 130
    finally:
        rclpy.try_shutdown()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", default=str(DEFAULT_SCENARIO))
    parser.add_argument("--jammers-config", default=str(DEFAULT_JAMMERS))
    parser.add_argument("--output-json", default=str(ROOT_DIR / "runs/latest/logs/node_state.json"))
    parser.add_argument("--output-jsonl", default=str(ROOT_DIR / "runs/latest/logs/node_state.jsonl"))
    parser.add_argument("--rate-hz", type=float, default=5.0)
    parser.add_argument('--transport', choices=('ros', 'gazebo'), default='ros')
    parser.add_argument('--world-name', default='map')
    parser.add_argument("--stale-after-s", type=float, default=.5)
    parser.add_argument(
        "--from-config-once",
        action="store_true",
        help="Write nominal positions from config once. Test/setup helper only.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.from_config_once:
        return run_config_once(args)
    if args.transport == 'gazebo':
        return run_gazebo_tracker(args)
    return run_ros_tracker(args)


if __name__ == "__main__":
    raise SystemExit(main())
