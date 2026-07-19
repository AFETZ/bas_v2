#!/usr/bin/env python3
"""Run the live ROS/Gazebo -> async Sionna -> ns-3 M4 adapter.

Only factual ns-3 ingress records create cells.  ROS callbacks atomically
replace the six-node/jammer pose snapshot, while all provider I/O remains in a
bounded background transport and the foreground loop never waits on Sionna.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import threading
import time
from pathlib import Path
from typing import Any

import yaml

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in os.sys.path:
    os.sys.path.insert(0, str(ROOT_DIR))

from network.radio_provider.sionna_async import load_protocol_limits
from network.radio_provider.sionna_async_service import ExactWireLog
from network.radio_provider.sionna_packet_adapter import (
    AdapterClientConfig,
    AppliedStateIPCWriter,
    PacketAdapterConfig,
    PacketAdapterError,
    PacketEventTailer,
    PacketSionnaAdapter,
    PoseSnapshot,
    SionnaAsyncTCPClient,
    SupervisedResultFaultInjector,
)
from network.bridge.runtime_clock_beacon import beacon
from network.scripts.m4_runtime_orchestrator import (
    ROOT,
    identity_for_contract,
    write_exclusive,
)
from network.validation.m4_common import M4ValidationError, strict_json
from network.validation.m4_runtime import (
    MAX_POSE_AGE_NS,
    QUERY_DEADLINE_NS,
    QUERY_PERIOD_NS,
    sha256_file,
)


POSE_SCHEMA = "ams.m4.pose_snapshot/v2"
CONTROL_SCHEMA = "ams.m4.adapter_control_event/v1"
NODE_IDS = ("cp", "uav1", "uav2", "uav3", "uav4", "uav5")
UAV_IDS = NODE_IDS[1:]
TRANSFORM_VERSION = "enu-identity-v1"
SOURCE_FRAME = "world"
ODOMETRY_HEADER_FRAME = "odom"
ODOMETRY_CHILD_FRAME = "base_link"
DIRECTED_LINK = re.compile(r"^(cp>uav[1-5]|uav[1-5]>cp)$")
TRAFFIC_CLASSES = {"control", "payload", "additional_data"}


def canonical_line(value: Any) -> bytes:
    return (
        json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()


def load_yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise M4ValidationError(f"YAML root is not an object: {path}")
    return value


def entity_name(frame: str) -> str | None:
    clean = frame.strip("/")
    parts = [part for part in clean.split("/") if part]
    for candidate in reversed(parts):
        if candidate in {"cp", "jammer_m4"}:
            return candidate
    return None


class PoseTracker:
    """Collect callback-time poses and publish complete atomic snapshots."""

    def __init__(self, run_dir: Path, jammer: dict[str, Any]):
        self._lock = threading.Lock()
        self._poses: dict[str, dict[str, Any]] = {}
        self._jammer_enabled = bool(jammer["enabled"])
        self._jammer = jammer
        self._sequence = 0
        self._latest_snapshot: PoseSnapshot | None = None
        self._log_path = run_dir / "logs/m4_pose_snapshots.jsonl"
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log = self._log_path.open("xb")
        self._last_logged_ns = 0

    def close(self) -> None:
        self._log.flush()
        os.fsync(self._log.fileno())
        self._log.close()

    def set_jammer_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._jammer_enabled = bool(enabled)

    def update_uav(self, node_id: str, message: Any) -> None:
        if node_id not in UAV_IDS:
            raise PacketAdapterError(f"unknown UAV pose source: {node_id}")
        header_frame = str(message.header.frame_id)
        child_frame = str(message.child_frame_id)
        if (
            header_frame != ODOMETRY_HEADER_FRAME
            or child_frame != ODOMETRY_CHILD_FRAME
        ):
            raise PacketAdapterError(
                f"{node_id} odometry header frame differs: "
                f"{header_frame!r}/{child_frame!r}"
            )
        now = time.monotonic_ns()
        pose = message.pose.pose
        self._update(
            node_id,
            now,
            f"/{node_id}/odometry",
            int(message.header.stamp.sec) * 1_000_000_000
            + int(message.header.stamp.nanosec),
            header_frame,
            child_frame,
            pose.position,
            pose.orientation,
        )

    def update_world(self, message: Any) -> None:
        now = time.monotonic_ns()
        for transform in message.transforms:
            name = entity_name(str(transform.child_frame_id))
            if name is None:
                continue
            self._update(
                name,
                now,
                "/world/map/pose/info",
                int(transform.header.stamp.sec) * 1_000_000_000
                + int(transform.header.stamp.nanosec),
                str(transform.header.frame_id),
                str(transform.child_frame_id),
                transform.transform.translation,
                transform.transform.rotation,
            )

    def _update(
        self,
        node_id: str,
        now: int,
        source_topic: str,
        source_stamp_ns: int,
        source_header_frame: str,
        source_child_frame: str,
        position: Any,
        orientation: Any,
    ) -> None:
        record = {
            "pose_monotonic_ns": now,
            "source_topic": source_topic,
            "source_header_stamp_ns": source_stamp_ns,
            "source_header_frame": source_header_frame,
            "source_child_frame": source_child_frame,
            "source_frame": SOURCE_FRAME,
            "transform_version": TRANSFORM_VERSION,
            "position_m": [float(position.x), float(position.y), float(position.z)],
            "orientation_quat_xyzw": [
                float(orientation.x),
                float(orientation.y),
                float(orientation.z),
                float(orientation.w),
            ],
            "freshness_age_ns": 0,
            "stale": False,
        }
        with self._lock:
            self._poses[node_id] = record

    def complete_and_fresh(self, now: int) -> bool:
        with self._lock:
            return set(self._poses) == {*NODE_IDS, "jammer_m4"} and all(
                0 <= now - int(item["pose_monotonic_ns"]) <= MAX_POSE_AGE_NS
                for item in self._poses.values()
            )

    def snapshot(self, now: int) -> PoseSnapshot | None:
        with self._lock:
            if set(self._poses) != {*NODE_IDS, "jammer_m4"}:
                return None
            if (
                self._latest_snapshot is not None
                and now - self._last_logged_ns < 100_000_000
            ):
                return self._latest_snapshot
            nodes = []
            raw_nodes = []
            for node_id in NODE_IDS:
                raw_value = dict(self._poses[node_id])
                age = now - int(raw_value["pose_monotonic_ns"])
                raw_value["freshness_age_ns"] = age
                raw_value["stale"] = age < 0 or age > MAX_POSE_AGE_NS
                raw_value["node_id"] = node_id
                raw_value["role"] = "command_post" if node_id == "cp" else "uav"
                raw_nodes.append(raw_value)
                value = {
                    key: item
                    for key, item in raw_value.items()
                    if key
                    not in {
                        "source_header_stamp_ns",
                        "source_header_frame",
                        "source_child_frame",
                    }
                }
                value.update(
                    {
                        "node_id": node_id,
                        "role": "command_post" if node_id == "cp" else "uav",
                    }
                )
                nodes.append(value)
            raw_jammer = dict(self._poses["jammer_m4"])
            jammer_age = now - int(raw_jammer["pose_monotonic_ns"])
            raw_jammer["freshness_age_ns"] = jammer_age
            raw_jammer["stale"] = jammer_age < 0 or jammer_age > MAX_POSE_AGE_NS
            jammer = {
                key: item
                for key, item in raw_jammer.items()
                if key
                not in {
                    "source_header_stamp_ns",
                    "source_header_frame",
                    "source_child_frame",
                }
            }
            jammer.update(
                {
                    "jammer_id": "jammer_m4",
                    "enabled": self._jammer_enabled,
                    "center_frequency_hz": float(self._jammer["center_hz"]),
                    "bandwidth_hz": float(self._jammer["bandwidth_hz"]),
                    "power_dbm": float(self._jammer["power_dbm"]),
                    "duty_cycle": float(self._jammer["duty_cycle"]),
                    "antenna_pattern": str(self._jammer["antenna"]),
                }
            )
            snapshot = PoseSnapshot.create(
                snapshot_sequence=self._sequence + 1,
                snapshot_monotonic_ns=now,
                source_frame=SOURCE_FRAME,
                transform_version=TRANSFORM_VERSION,
                nodes=tuple(nodes),
                jammers=(jammer,),
            )
            self._sequence = snapshot.snapshot_sequence
            self._latest_snapshot = snapshot
            self._log.write(
                canonical_line(
                    {
                        "schema": POSE_SCHEMA,
                        "pose_sequence": self._sequence,
                        "node_state_seq": snapshot.snapshot_sequence,
                        "node_state_sha256": snapshot.snapshot_sha256,
                        "snapshot_monotonic_ns": snapshot.snapshot_monotonic_ns,
                        "host_monotonic_ns": now,
                        "source_frame": SOURCE_FRAME,
                        "transform_version": TRANSFORM_VERSION,
                        "nodes": raw_nodes,
                        "jammers": [
                            {
                                **raw_jammer,
                                "jammer_id": "jammer_m4",
                                "enabled": self._jammer_enabled,
                                "center_frequency_hz": float(
                                    self._jammer["center_hz"]
                                ),
                                "bandwidth_hz": float(self._jammer["bandwidth_hz"]),
                                "power_dbm": float(self._jammer["power_dbm"]),
                                "duty_cycle": float(self._jammer["duty_cycle"]),
                                "antenna_pattern": str(self._jammer["antenna"]),
                            }
                        ],
                    }
                )
            )
            self._log.flush()
            self._last_logged_ns = now
            return snapshot


class ControlReader:
    """Read each predeclared control file once in lexical order."""

    def __init__(self, directory: Path, output: Path):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self._seen: set[Path] = set()
        self._sequence = 0
        self._output = output.open("xb")

    def close(self) -> None:
        self._output.flush()
        os.fsync(self._output.fileno())
        self._output.close()

    def poll(self) -> list[tuple[Path, dict[str, Any]]]:
        records: list[tuple[Path, dict[str, Any]]] = []
        for path in sorted(self.directory.glob("*.json")):
            if path in self._seen:
                continue
            value = strict_json(path)
            self._seen.add(path)
            records.append((path, value))
        return records

    def record(self, path: Path, action: str, detail: dict[str, Any]) -> None:
        self._sequence += 1
        self._output.write(
            canonical_line(
                {
                    "schema": CONTROL_SCHEMA,
                    "control_sequence": self._sequence,
                    "host_monotonic_ns": time.monotonic_ns(),
                    "source_file": path.name,
                    "action": action,
                    "detail": detail,
                }
            )
        )
        self._output.flush()


def build_adapter(
    args: argparse.Namespace, tracker: PoseTracker, initial: PoseSnapshot
) -> tuple[
    PacketSionnaAdapter,
    SionnaAsyncTCPClient,
    SupervisedResultFaultInjector | None,
]:
    contract = strict_json(args.contract)
    identity, _contract_hash, _config_hash = identity_for_contract(args.contract)
    bundle = strict_json(ROOT / "network/config/m4_canonical_scene_bundle.json")
    radio = load_yaml(ROOT / "network/config/radio_m4_canonical.yaml")["radio"]
    executable = Path(__file__).resolve()
    limits = load_protocol_limits()
    client_config = AdapterClientConfig(
        identity=identity,
        sender_id="sionna-adapter-m4",
        phase_id="m4_continuous_runtime",
        clock_domain="host-monotonic",
        executable_path=str(executable),
        executable_sha256=sha256_file(executable),
        scene_path=str((ROOT / bundle["sionna_scene_xml"]).resolve()),
        scene_manifest_sha256=str(bundle["bundle_sha256"]),
    )
    client = SionnaAsyncTCPClient(
        args.provider_host,
        args.provider_port,
        client_config,
        ExactWireLog(args.run_dir / "logs", fsync=False),
        limits=limits,
    )
    transport: Any = client
    injector = None
    if args.fault_enabled:
        injector = SupervisedResultFaultInjector(
            client,
            args.run_dir / "logs/sionna_result_faults.jsonl",
            max_held_results=int(contract["limits"]["max_fault_pending_per_cell"]),
            max_release_queue=int(contract["limits"]["max_fault_release_queue"]),
            max_captured_results=int(
                contract["limits"]["max_fault_captured_results"]
            ),
        )
        transport = injector
    adapter_config = PacketAdapterConfig(
        identity=identity,
        phase_id="m4_continuous_runtime",
        sender_id="sionna-adapter-m4",
        provider_sender_id="sionna-provider-m4",
        clock_domain="host-monotonic",
        query_deadline_ns=QUERY_DEADLINE_NS,
        mapping_seed=int(load_yaml(ROOT / "network/config/radio_m4_canonical.yaml")["sionna"]["solver"]["seed"]),
        source_frame=SOURCE_FRAME,
        transform_version=TRANSFORM_VERSION,
        radio_assumptions={
            "carrier_frequency_hz": float(radio["carrier_hz"]),
            "bandwidth_hz": float(radio["bandwidth_hz"]),
            "tx_power_dbm": float(radio["tx_power_dbm"]),
            "receiver_noise_figure_db": float(radio["receiver_noise_figure_db"]),
            "receiver_sensitivity_dbm": float(radio["receiver_sensitivity_dbm"]),
            "units": {
                "carrier_frequency": "Hz",
                "bandwidth": "Hz",
                "tx_power": "dBm",
                "receiver_noise_figure": "dB",
                "receiver_sensitivity": "dBm",
            },
        },
        antenna_assumptions={
            "tx_pattern": "isotropic",
            "rx_pattern": "isotropic",
            "polarization": "vertical",
            "orientation_effects_claimed": False,
        },
        material_assumptions={
            "material_model_id": "m4-canonical-material-v1",
            "scene_material_manifest_sha256": bundle["scene_material_manifest_sha256"],
        },
        mapping_version="sinr-rate-per-v2",
        fault_injection_enabled=args.fault_enabled,
        max_fault_pending_per_cell=int(contract["limits"]["max_fault_pending_per_cell"]),
        query_period_ns=QUERY_PERIOD_NS,
        global_query_spacing_ns=33_333_333,
    )
    adapter = PacketSionnaAdapter(
        adapter_config,
        initial,
        transport,
        AppliedStateIPCWriter(
            args.state_file,
            max_line_bytes=int(contract["limits"]["max_state_line_bytes"]),
        ),
        args.run_dir / "logs/sionna_packet_adapter.jsonl",
        limits=limits,
    )
    # Keep an explicit reference so the callback-owned tracker cannot be
    # accidentally discarded while the adapter loop is live.
    adapter._m4_pose_tracker = tracker  # type: ignore[attr-defined]
    return adapter, client, injector


def apply_control(
    path: Path,
    command: dict[str, Any],
    tracker: PoseTracker,
    injector: SupervisedResultFaultInjector | None,
    fault_seed_cells: set[tuple[str, str]],
    fault_parallel_cells: set[tuple[str, str]],
) -> tuple[str, dict[str, Any]]:
    action = command.get("action")
    not_before = command.get("not_before_monotonic_ns", 0)
    if isinstance(not_before, bool) or not isinstance(not_before, int):
        raise PacketAdapterError(f"invalid control timestamp: {path}")
    if time.monotonic_ns() < not_before:
        return "deferred", {}
    if action == "set_jammer_enabled":
        enabled = command.get("enabled")
        if not isinstance(enabled, bool):
            raise PacketAdapterError("jammer control enabled must be Boolean")
        tracker.set_jammer_enabled(enabled)
        return str(action), {"enabled": enabled}
    if injector is None:
        raise PacketAdapterError("fault control used while fault injector is disabled")
    if action == "arm_hold_next":
        directed_link_id = command.get("directed_link_id")
        link = command.get("directed_link")
        traffic_class = command.get("traffic_class")
        if (
            not isinstance(directed_link_id, str)
            or not isinstance(link, str)
            or DIRECTED_LINK.fullmatch(link) is None
            or traffic_class not in TRAFFIC_CLASSES
            or directed_link_id
            != f"{link.replace('>', '-to-')}-{traffic_class}"
        ):
            raise PacketAdapterError("fault-seed control identity is invalid")
        cell = (link, str(traffic_class))
        if cell in fault_seed_cells or cell in fault_parallel_cells:
            raise PacketAdapterError("fault-seed control cell is already armed")
        if len(fault_seed_cells) >= 30:
            raise PacketAdapterError("fault-seed armed-cell bound exceeded")
        fault_seed_cells.add(cell)
        return str(action), {
            "directed_link_id": directed_link_id,
            "directed_link": link,
            "traffic_class": traffic_class,
        }
    if action == "arm_fault_parallel_next":
        link = command.get("directed_link")
        traffic_class = command.get("traffic_class")
        directed_link_id = command.get("directed_link_id")
        if (
            not isinstance(link, str)
            or DIRECTED_LINK.fullmatch(link) is None
            or traffic_class not in TRAFFIC_CLASSES
            or not isinstance(directed_link_id, str)
            or directed_link_id
            != f"{link.replace('>', '-to-')}-{traffic_class}"
        ):
            raise PacketAdapterError("fault-parallel control cell is invalid")
        cell = (link, str(traffic_class))
        held = injector.held_query_ids_for_link(directed_link_id)
        if len(held) != 1:
            raise PacketAdapterError(
                "fault-parallel control requires one confirmed held real result"
            )
        if cell in fault_seed_cells or cell in fault_parallel_cells:
            raise PacketAdapterError("fault-parallel control is already armed")
        if len(fault_parallel_cells) >= 30:
            raise PacketAdapterError("fault-parallel armed-cell bound exceeded")
        fault_parallel_cells.add(cell)
        return str(action), {
            "directed_link_id": directed_link_id,
            "directed_link": link,
            "traffic_class": traffic_class,
            "held_query_id": held[0],
        }
    if action == "release_held":
        requested = command.get("query_id")
        requested_link = command.get("directed_link_id")
        if requested_link is not None and not isinstance(requested_link, str):
            raise PacketAdapterError("release_held directed_link_id is invalid")
        held = (
            injector.held_query_ids_for_link(requested_link)
            if isinstance(requested_link, str)
            else injector.held_query_ids
        )
        query_id = str(requested) if requested else (held[0] if len(held) == 1 else "")
        if not query_id or query_id not in held:
            raise PacketAdapterError("release_held has no unique held query")
        injector.release_held(query_id)
        return str(action), {
            "query_id": query_id,
            "directed_link_id": requested_link,
        }
    if action == "inject_duplicate":
        requested = command.get("query_id")
        requested_link = command.get("directed_link_id")
        if requested is not None and not isinstance(requested, str):
            raise PacketAdapterError("duplicate query_id is invalid")
        if requested_link is not None and not isinstance(requested_link, str):
            raise PacketAdapterError("duplicate directed_link_id is invalid")
        query_id = requested
        if query_id is None:
            if not isinstance(requested_link, str):
                raise PacketAdapterError(
                    "duplicate control requires query_id or directed_link_id"
                )
            query_id = injector.latest_captured_query_id(requested_link)
        if not query_id or query_id not in injector.captured_query_ids:
            raise PacketAdapterError("duplicate has no captured real result")
        injector.inject_duplicate(query_id)
        return str(action), {
            "query_id": query_id,
            "directed_link_id": requested_link,
        }
    raise PacketAdapterError(f"unsupported adapter control action: {action}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--packet-events", type=Path, required=True)
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--ready-file", type=Path, required=True)
    parser.add_argument("--stop-file", type=Path, required=True)
    parser.add_argument("--control-dir", type=Path, required=True)
    parser.add_argument("--clock-socket", type=Path, required=True)
    parser.add_argument("--provider-host", default="127.0.0.1")
    parser.add_argument("--provider-port", type=int, default=5090)
    parser.add_argument("--fault-enabled", action="store_true")
    args = parser.parse_args()
    args.run_dir = args.run_dir.resolve()
    args.contract = args.contract.resolve()
    args.packet_events = args.packet_events.resolve()
    args.state_file = args.state_file.resolve()
    args.ready_file = args.ready_file.resolve()
    args.stop_file = args.stop_file.resolve()
    args.control_dir = args.control_dir.resolve()
    args.clock_socket = args.clock_socket.resolve()
    contract = strict_json(args.contract)
    jammer_config = load_yaml(ROOT / "network/config/jammers_m4_canonical.yaml")
    jammer = jammer_config["jammers"][0]
    tracker = PoseTracker(args.run_dir, jammer)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_unused: stop.set())
    signal.signal(signal.SIGTERM, lambda *_unused: stop.set())
    clock_thread = threading.Thread(
        target=beacon,
        args=(args.clock_socket, "sionna_adapter", stop),
        daemon=True,
    )
    tracker_clock_thread = threading.Thread(
        target=beacon,
        args=(
            args.clock_socket,
            "ros_gazebo_tracker",
            stop,
        ),
        daemon=True,
    )

    import rclpy
    from nav_msgs.msg import Odometry
    from rclpy.node import Node
    from rclpy.qos import qos_profile_sensor_data
    from tf2_msgs.msg import TFMessage

    class TrackerNode(Node):
        def __init__(self) -> None:
            super().__init__("ams_m4_sionna_pose_tracker")
            for node_id in UAV_IDS:
                self.create_subscription(
                    Odometry,
                    f"/{node_id}/odometry",
                    lambda message, identity=node_id: tracker.update_uav(identity, message),
                    qos_profile_sensor_data,
                )
            self.create_subscription(
                TFMessage,
                "/world/map/pose/info",
                tracker.update_world,
                qos_profile_sensor_data,
            )

    client: SionnaAsyncTCPClient | None = None
    control: ControlReader | None = None
    node: Any = None
    try:
        rclpy.init(args=None)
        node = TrackerNode()
        clock_thread.start()
        tracker_clock_thread.start()
        deadline = time.monotonic_ns() + 120_000_000_000
        initial = None
        while time.monotonic_ns() < deadline and not stop.is_set():
            rclpy.spin_once(node, timeout_sec=0.05)
            now = time.monotonic_ns()
            if tracker.complete_and_fresh(now):
                initial = tracker.snapshot(now)
                break
        if initial is None:
            raise M4ValidationError("six-node/jammer ROS/Gazebo pose readiness timed out")
        adapter, client, injector = build_adapter(args, tracker, initial)
        tailer = PacketEventTailer(
            args.packet_events,
            max_line_bytes=int(contract["limits"]["max_packet_event_line_bytes"]),
        )
        control = ControlReader(
            args.control_dir, args.run_dir / "logs/m4_adapter_controls.jsonl"
        )
        client.start()
        client_deadline = time.monotonic_ns() + 10_000_000_000
        while not client.ready and time.monotonic_ns() < client_deadline:
            rclpy.spin_once(node, timeout_sec=0.02)
        if not client.ready:
            raise M4ValidationError("real Sionna TCP handshake did not become ready")
        write_exclusive(
            args.ready_file,
            {
                "pid": os.getpid(),
                "monotonic_ns": time.monotonic_ns(),
                "run_id": contract["run_id"],
                "runtime_id": contract["runtime_id"],
                "provider_mode": "real_sionna",
                "pose_entities": [*NODE_IDS, "jammer_m4"],
            },
        )
        deferred: dict[Path, dict[str, Any]] = {}
        fault_seed_cells: set[tuple[str, str]] = set()
        fault_parallel_cells: set[tuple[str, str]] = set()
        while not stop.is_set() and not args.stop_file.exists():
            started = time.monotonic_ns()
            rclpy.spin_once(node, timeout_sec=0.0)
            snapshot = tracker.snapshot(started)
            if snapshot is not None:
                adapter.update_poses(snapshot)
            for path, command in [*deferred.items(), *control.poll()]:
                action, detail = apply_control(
                    path,
                    command,
                    tracker,
                    injector,
                    fault_seed_cells,
                    fault_parallel_cells,
                )
                if action == "deferred":
                    deferred[path] = command
                else:
                    deferred.pop(path, None)
                    control.record(path, action, detail)
            adapter.run_once(
                tailer,
                max_packet_events=64,
                max_results=64,
                fault_seed_cells=fault_seed_cells,
                fault_parallel_cells=fault_parallel_cells,
            )
            remaining = 5_000_000 - (time.monotonic_ns() - started)
            if remaining > 0:
                stop.wait(remaining / 1_000_000_000)
        return 0
    except (M4ValidationError, PacketAdapterError, OSError, KeyError, TypeError, ValueError) as exc:
        print(f"FAIL M4 adapter: {exc}", file=os.sys.stderr)
        return 2
    finally:
        stop.set()
        if client is not None:
            client.stop(5.0)
        if clock_thread.ident is not None:
            clock_thread.join(2.0)
        if tracker_clock_thread.ident is not None:
            tracker_clock_thread.join(2.0)
        if control is not None:
            control.close()
        tracker.close()
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
