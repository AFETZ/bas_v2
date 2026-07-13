#!/usr/bin/env python3
"""Virtual HitL endpoint loopback and timing supervisor.

This module intentionally does not open or probe physical modem devices.  It
creates software serial PTY and UDP endpoint traffic, then sends the bytes
through the same queue/deadline/timing stages that future ns-3/Sionna bridge
adapters must report.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import pty
import selectors
import socket
import statistics
import sys
import time
import tty
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    import yaml
except ImportError:  # pragma: no cover - exercised by dependency checks.
    yaml = None


ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT_DIR / "network" / "config" / "hitl_loopback.yaml"
TRAFFIC_CLASSES = ("control", "payload", "additional_data")


def now_ns() -> int:
    return time.perf_counter_ns()


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def ns_to_ms(delta_ns: int) -> float:
    return delta_ns / 1_000_000.0


def load_yaml(path: Path) -> Dict[str, Any]:
    if yaml is None:
        print("PyYAML is required: python3 -m pip install PyYAML", file=sys.stderr)
        sys.exit(2)
    if not path.exists():
        print(f"Config file not found: {path}", file=sys.stderr)
        sys.exit(2)
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        print(f"Config file must contain a mapping: {path}", file=sys.stderr)
        sys.exit(2)
    return data


def resolve_repo_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return ROOT_DIR / path


@dataclasses.dataclass
class TrafficPolicy:
    traffic_class: str
    priority: int
    deadline_ms: float
    max_queue_packets: int
    max_queue_bytes: int
    service_tier_bps: int
    virtual_sionna_latency_ms: float
    virtual_ns3_base_delay_ms: float


@dataclasses.dataclass
class Packet:
    mode: str
    endpoint: str
    traffic_class: str
    sequence: int
    payload: bytes
    created_ns: int

    @property
    def size_bytes(self) -> int:
        return len(self.payload)


class TimingSupervisor:
    def __init__(self, run_dir: Path, run_id: str) -> None:
        self.run_dir = run_dir
        self.run_id = run_id
        self.logs_dir = run_dir / "logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self._timing = (self.logs_dir / "timing.jsonl").open("a", encoding="utf-8")
        self._bridge = (self.logs_dir / "bridge.jsonl").open("a", encoding="utf-8")
        self._hitl = (self.logs_dir / "hitl_loopback.jsonl").open("a", encoding="utf-8")
        self.stage_counts: Dict[str, int] = {}

    def emit(self, event: str, **fields: Any) -> None:
        record = {
            "ts_utc": utc_now(),
            "monotonic_ns": now_ns(),
            "run_id": self.run_id,
            "event": event,
        }
        record.update(fields)
        line = json.dumps(record, sort_keys=True, separators=(",", ":"))
        self._timing.write(line + "\n")
        self._timing.flush()
        self._hitl.write(line + "\n")
        self._hitl.flush()
        if event.startswith(("endpoint_", "queue_", "ns3_", "sionna_")):
            self._bridge.write(line + "\n")
            self._bridge.flush()
        self.stage_counts[event] = self.stage_counts.get(event, 0) + 1

    def close(self) -> None:
        for handle in (self._timing, self._bridge, self._hitl):
            handle.close()


class ModeledSoftwarePath:
    def __init__(
        self,
        logger: TimingSupervisor,
        policies: Dict[str, TrafficPolicy],
        component_status: Dict[str, Any],
        require_components: bool,
    ) -> None:
        self.logger = logger
        self.policies = policies
        self.component_status = component_status
        self.require_components = require_components
        self.queue_packets = {traffic_class: 0 for traffic_class in TRAFFIC_CLASSES}
        self.queue_bytes = {traffic_class: 0 for traffic_class in TRAFFIC_CLASSES}
        self.latencies_ms: Dict[str, List[float]] = {traffic_class: [] for traffic_class in TRAFFIC_CLASSES}
        self.drops: List[Dict[str, Any]] = []
        self.delivered = 0
        self.submitted = 0

        if not component_status["all_available"]:
            self.logger.emit(
                "modeled_path_blocker",
                path_mode="hitl_virtual_loopback",
                actual_modeled_path_available=False,
                missing_components=component_status["missing_components"],
                blocker="Actual ns-3/Sionna/bridge traversal is blocked until the missing components exist.",
            )
            if require_components:
                missing = ", ".join(component_status["missing_components"])
                raise RuntimeError(f"required modeled path components are missing: {missing}")

    def transmit(self, packet: Packet) -> Optional[bytes]:
        self.submitted += 1
        policy = self.policies[packet.traffic_class]
        ingress_ns = now_ns()
        self.logger.emit(
            "endpoint_ingress",
            mode=packet.mode,
            endpoint=packet.endpoint,
            traffic_class=packet.traffic_class,
            sequence=packet.sequence,
            packet_bytes=packet.size_bytes,
        )

        age_ms = ns_to_ms(ingress_ns - packet.created_ns)
        if age_ms > policy.deadline_ms:
            self._drop(packet, "deadline_expired_before_enqueue", age_ms, policy)
            return None

        if (
            self.queue_packets[packet.traffic_class] + 1 > policy.max_queue_packets
            or self.queue_bytes[packet.traffic_class] + packet.size_bytes > policy.max_queue_bytes
        ):
            self._drop(packet, "bounded_queue_full", age_ms, policy)
            return None

        self.queue_packets[packet.traffic_class] += 1
        self.queue_bytes[packet.traffic_class] += packet.size_bytes
        enqueue_ns = now_ns()
        self.logger.emit(
            "queue_enqueue",
            mode=packet.mode,
            endpoint=packet.endpoint,
            traffic_class=packet.traffic_class,
            sequence=packet.sequence,
            priority=policy.priority,
            deadline_ms=policy.deadline_ms,
            age_ms=round(ns_to_ms(enqueue_ns - packet.created_ns), 3),
            queue_depth_packets=self.queue_packets[packet.traffic_class],
            queue_depth_bytes=self.queue_bytes[packet.traffic_class],
        )

        dequeue_ns = now_ns()
        self.queue_packets[packet.traffic_class] -= 1
        self.queue_bytes[packet.traffic_class] -= packet.size_bytes
        queue_delay_ms = ns_to_ms(dequeue_ns - enqueue_ns)
        if ns_to_ms(dequeue_ns - packet.created_ns) > policy.deadline_ms:
            self._drop(packet, "deadline_expired_before_dequeue", ns_to_ms(dequeue_ns - packet.created_ns), policy)
            return None

        self.logger.emit(
            "queue_dequeue",
            mode=packet.mode,
            endpoint=packet.endpoint,
            traffic_class=packet.traffic_class,
            sequence=packet.sequence,
            priority=policy.priority,
            queue_delay_ms=round(queue_delay_ms, 3),
            queue_depth_packets=self.queue_packets[packet.traffic_class],
            queue_depth_bytes=self.queue_bytes[packet.traffic_class],
        )

        self.logger.emit(
            "sionna_query_start",
            mode=packet.mode,
            endpoint=packet.endpoint,
            traffic_class=packet.traffic_class,
            sequence=packet.sequence,
            source="virtual_loopback_shim",
            actual_provider=False,
        )
        sionna_start = now_ns()
        time.sleep(policy.virtual_sionna_latency_ms / 1000.0)
        sionna_latency_ms = ns_to_ms(now_ns() - sionna_start)
        self.logger.emit(
            "sionna_query_result",
            mode=packet.mode,
            endpoint=packet.endpoint,
            traffic_class=packet.traffic_class,
            sequence=packet.sequence,
            provider_latency_ms=round(sionna_latency_ms, 3),
            stale=False,
            source="virtual_loopback_shim",
            link_state="loopback_nominal",
            service_tier_bps=policy.service_tier_bps,
        )

        serialization_ms = packet.size_bytes * 8 * 1000.0 / max(policy.service_tier_bps, 1)
        ns3_delay_ms = policy.virtual_ns3_base_delay_ms + serialization_ms
        self.logger.emit(
            "ns3_packet_start",
            mode=packet.mode,
            endpoint=packet.endpoint,
            traffic_class=packet.traffic_class,
            sequence=packet.sequence,
            source="virtual_loopback_shim",
            actual_ns3=False,
            planned_delay_ms=round(ns3_delay_ms, 3),
        )
        ns3_start = now_ns()
        time.sleep(ns3_delay_ms / 1000.0)
        observed_ns3_delay_ms = ns_to_ms(now_ns() - ns3_start)
        self.logger.emit(
            "ns3_packet_result",
            mode=packet.mode,
            endpoint=packet.endpoint,
            traffic_class=packet.traffic_class,
            sequence=packet.sequence,
            ns3_delay_ms=round(observed_ns3_delay_ms, 3),
            service_tier_bps=policy.service_tier_bps,
            queue_delay_ms=round(queue_delay_ms, 3),
            source="virtual_loopback_shim",
            delivered=True,
        )

        egress_ns = now_ns()
        end_to_end_ms = ns_to_ms(egress_ns - packet.created_ns)
        self.latencies_ms[packet.traffic_class].append(end_to_end_ms)
        self.delivered += 1
        self.logger.emit(
            "endpoint_egress",
            mode=packet.mode,
            endpoint=packet.endpoint,
            traffic_class=packet.traffic_class,
            sequence=packet.sequence,
            packet_bytes=packet.size_bytes,
            endpoint_to_endpoint_ms=round(end_to_end_ms, 3),
            provider_latency_ms=round(sionna_latency_ms, 3),
            ns3_delay_ms=round(observed_ns3_delay_ms, 3),
            queue_delay_ms=round(queue_delay_ms, 3),
            path_mode="hitl_virtual_loopback",
        )
        return packet.payload

    def _drop(self, packet: Packet, reason: str, age_ms: float, policy: TrafficPolicy) -> None:
        drop = {
            "mode": packet.mode,
            "endpoint": packet.endpoint,
            "traffic_class": packet.traffic_class,
            "sequence": packet.sequence,
            "reason": reason,
        }
        self.drops.append(drop)
        self.logger.emit(
            "queue_drop",
            mode=packet.mode,
            endpoint=packet.endpoint,
            traffic_class=packet.traffic_class,
            sequence=packet.sequence,
            reason=reason,
            age_ms=round(age_ms, 3),
            deadline_ms=policy.deadline_ms,
            queue_depth_packets=self.queue_packets[packet.traffic_class],
            queue_depth_bytes=self.queue_bytes[packet.traffic_class],
            dropped=True,
        )


def build_policies(config: Dict[str, Any]) -> Dict[str, TrafficPolicy]:
    service_path = resolve_repo_path(config.get("service_tiers_config", "network/config/service_tiers.yaml"))
    service_data = load_yaml(service_path)
    class_policy = service_data.get("traffic_class_policy", {})
    tiers = {item["name"]: int(item["target_bps"]) for item in service_data.get("service_tiers", [])}

    queue_policy = config.get("queue_policy", {})
    timing_model = config.get("timing_model", {})
    sionna_latencies = timing_model.get("virtual_sionna_latency_ms", {})
    ns3_delays = timing_model.get("virtual_ns3_base_delay_ms", {})
    tier_floor = timing_model.get("service_tier_floor_bps", {})

    policies: Dict[str, TrafficPolicy] = {}
    for traffic_class in TRAFFIC_CLASSES:
        policy = class_policy.get(traffic_class, {})
        tier_name = policy.get("minimum_tier")
        tier_bps = int(tiers.get(tier_name, tier_floor.get(traffic_class, 1000)))
        policies[traffic_class] = TrafficPolicy(
            traffic_class=traffic_class,
            priority=int(policy.get("queue_priority", 99)),
            deadline_ms=float(policy.get("deadline_ms", 1000)),
            max_queue_packets=int(queue_policy.get("max_queue_packets_per_class", 64)),
            max_queue_bytes=int(queue_policy.get("max_queue_bytes_per_class", 65536)),
            service_tier_bps=tier_bps,
            virtual_sionna_latency_ms=float(sionna_latencies.get(traffic_class, 5.0)),
            virtual_ns3_base_delay_ms=float(ns3_delays.get(traffic_class, 10.0)),
        )
    return policies


def detect_modeled_components(config: Dict[str, Any]) -> Dict[str, Any]:
    required = config.get("virtual_loopback", {}).get("actual_modeled_path_required_components", [])
    missing = []
    present = []
    for value in required:
        path = resolve_repo_path(str(value))
        if path.exists():
            present.append(str(value))
        else:
            missing.append(str(value))
    return {
        "required_components": list(required),
        "present_components": present,
        "missing_components": missing,
        "all_available": not missing,
    }


def select_backend(args: argparse.Namespace, config: Dict[str, Any]) -> str:
    selection = config.get("backend_selection", {})
    env_name = selection.get("environment_variable", "AMS_RADIO_BACKEND")
    selected = args.radio_backend or os.environ.get(env_name) or selection.get("default", "sim_2_4ghz")
    radio_backend_path = resolve_repo_path(config.get("radio_backend_config", "network/config/radio_backend.yaml"))
    radio_backend_data = load_yaml(radio_backend_path)
    allowed = radio_backend_data.get("selection", {}).get("allowed_values", ["sim_2_4ghz", "real_modem_2_4ghz"])
    if selected not in allowed:
        allowed_text = ", ".join(allowed)
        print(f"Unsupported radio backend '{selected}'. Allowed values: {allowed_text}", file=sys.stderr)
        sys.exit(64)
    return selected


def guard_hardware_mode(args: argparse.Namespace, config: Dict[str, Any], selected_backend: str) -> None:
    selection = config.get("backend_selection", {})
    simulated = selection.get("current_acceptance_backend", "sim_2_4ghz")
    real = selection.get("future_physical_backend", "real_modem_2_4ghz")

    if args.mode == "real-hardware":
        if selected_backend != real:
            print(
                "Physical HitL hardware mode is disabled by default. "
                f"Current backend is '{selected_backend}'; a future live-hardware run must explicitly select '{real}'. "
                "No hardware was opened or probed.",
                file=sys.stderr,
            )
            sys.exit(64)
        print(
            "The real_modem_2_4ghz backend is selected, but live hardware validation is intentionally not implemented "
            "in this workstream. No serial device, network interface, or modem was opened or probed. "
            "Follow network/hitl/README.md after the simulated 2.4 GHz path is complete.",
            file=sys.stderr,
        )
        sys.exit(64)

    if selected_backend != simulated:
        print(
            f"Virtual loopback refuses to run while backend '{selected_backend}' is selected. "
            f"Use '{simulated}' for current software-modeled validation or run a future real-hardware workflow. "
            "No hardware was opened or probed.",
            file=sys.stderr,
        )
        sys.exit(64)


def make_payload(mode: str, traffic_class: str, sequence: int, payload_bytes: int) -> bytes:
    prefix = f"AMS-HITL|{mode}|{traffic_class}|{sequence}|".encode("ascii")
    fill_len = max(payload_bytes - len(prefix), 0)
    return prefix + (b"x" * fill_len)


def iter_packets(mode: str, packets_per_class: int, payload_bytes: int) -> Iterable[Packet]:
    sequence = 1
    for traffic_class in TRAFFIC_CLASSES:
        for _ in range(packets_per_class):
            yield Packet(
                mode=mode,
                endpoint=f"{mode}_loopback",
                traffic_class=traffic_class,
                sequence=sequence,
                payload=make_payload(mode, traffic_class, sequence, payload_bytes),
                created_ns=now_ns(),
            )
            sequence += 1


def read_exact_fd(fd: int, size: int, timeout_s: float) -> bytes:
    selector = selectors.DefaultSelector()
    selector.register(fd, selectors.EVENT_READ)
    chunks: List[bytes] = []
    remaining = size
    deadline = time.monotonic() + timeout_s
    try:
        while remaining > 0:
            timeout = max(0.0, deadline - time.monotonic())
            if timeout <= 0:
                raise TimeoutError(f"timed out reading {remaining} byte(s) from PTY")
            events = selector.select(timeout)
            if not events:
                raise TimeoutError(f"timed out reading {remaining} byte(s) from PTY")
            chunk = os.read(fd, remaining)
            if not chunk:
                raise EOFError("PTY returned EOF")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        selector.unregister(fd)
        selector.close()


def run_serial_loopback(
    args: argparse.Namespace,
    config: Dict[str, Any],
    path: ModeledSoftwarePath,
    logger: TimingSupervisor,
    run_dir: Path,
) -> Dict[str, Any]:
    serial_cfg = config.get("virtual_loopback", {}).get("serial", {})
    baud_bps = int(args.baud_bps or serial_cfg.get("baud_bps", 57600))
    framing_bits = int(serial_cfg.get("framing_overhead_bits_per_byte", 10))
    timeout_s = float(serial_cfg.get("read_timeout_s", 1.0))
    validation_cfg = config.get("virtual_loopback", {}).get("validation", {})
    packets_per_class = int(args.packets_per_class or validation_cfg.get("packets_per_class", 2))
    payload_bytes = int(args.payload_bytes or validation_cfg.get("payload_bytes", 96))
    inject_deadline_drop = bool(validation_cfg.get("inject_deadline_drop", True)) and not args.no_inject_deadline_drop

    hitl_dir = run_dir / "hitl"
    hitl_dir.mkdir(parents=True, exist_ok=True)
    master_fd, slave_fd = pty.openpty()
    slave_name = os.ttyname(slave_fd)
    symlink_path = hitl_dir / "serial_pty"
    if symlink_path.exists() or symlink_path.is_symlink():
        symlink_path.unlink()
    symlink_path.symlink_to(slave_name)

    packets_sent = 0
    packets_received = 0
    errors: List[str] = []
    logger.emit(
        "serial_endpoint_open",
        mode="serial",
        endpoint="serial_loopback",
        slave_path=slave_name,
        slave_symlink=str(symlink_path),
        baud_bps=baud_bps,
    )

    try:
        tty.setraw(slave_fd)
        os.set_blocking(master_fd, False)
        os.set_blocking(slave_fd, False)

        for packet in iter_packets("serial", packets_per_class, payload_bytes):
            packets_sent += 1
            pace_ms = packet.size_bytes * framing_bits * 1000.0 / max(baud_bps, 1)
            logger.emit(
                "serial_byte_pacing",
                mode="serial",
                endpoint=packet.endpoint,
                traffic_class=packet.traffic_class,
                sequence=packet.sequence,
                packet_bytes=packet.size_bytes,
                baud_bps=baud_bps,
                pacing_ms=round(pace_ms, 3),
            )
            time.sleep(pace_ms / 1000.0)
            os.write(slave_fd, packet.payload)
            ingress = read_exact_fd(master_fd, packet.size_bytes, timeout_s)
            if ingress != packet.payload:
                errors.append(f"serial ingress mismatch seq={packet.sequence}")
                continue
            delivered = path.transmit(dataclasses.replace(packet, payload=ingress))
            if delivered is None:
                continue
            os.write(master_fd, delivered)
            egress = read_exact_fd(slave_fd, len(delivered), timeout_s)
            if egress != delivered:
                errors.append(f"serial egress mismatch seq={packet.sequence}")
                continue
            packets_received += 1

        if inject_deadline_drop:
            drop_policy = path.policies["payload"]
            stale_packet = Packet(
                mode="serial",
                endpoint="serial_loopback",
                traffic_class="payload",
                sequence=999001,
                payload=make_payload("serial", "payload", 999001, payload_bytes),
                created_ns=now_ns() - int((drop_policy.deadline_ms + 100.0) * 1_000_000),
            )
            packets_sent += 1
            os.write(slave_fd, stale_packet.payload)
            ingress = read_exact_fd(master_fd, stale_packet.size_bytes, timeout_s)
            path.transmit(dataclasses.replace(stale_packet, payload=ingress))
    except Exception as exc:  # pragma: no cover - reported in validation output.
        errors.append(str(exc))
    finally:
        try:
            os.close(master_fd)
        finally:
            os.close(slave_fd)

    passed = not errors and packets_received == packets_per_class * len(TRAFFIC_CLASSES)
    logger.emit(
        "serial_loopback_complete",
        mode="serial",
        endpoint="serial_loopback",
        passed=passed,
        packets_sent=packets_sent,
        packets_received=packets_received,
        errors=errors,
    )
    return {
        "passed": passed,
        "packets_sent": packets_sent,
        "packets_received": packets_received,
        "errors": errors,
        "pty_symlink": str(symlink_path),
        "baud_bps": baud_bps,
    }


def run_ethernet_loopback(
    args: argparse.Namespace,
    config: Dict[str, Any],
    path: ModeledSoftwarePath,
    logger: TimingSupervisor,
) -> Dict[str, Any]:
    eth_cfg = config.get("virtual_loopback", {}).get("ethernet", {})
    bind_host = str(eth_cfg.get("bind_host", "127.0.0.1"))
    timeout_s = float(eth_cfg.get("read_timeout_s", 1.0))
    validation_cfg = config.get("virtual_loopback", {}).get("validation", {})
    packets_per_class = int(args.packets_per_class or validation_cfg.get("packets_per_class", 2))
    payload_bytes = int(args.payload_bytes or validation_cfg.get("payload_bytes", 96))
    inject_deadline_drop = bool(validation_cfg.get("inject_deadline_drop", True)) and not args.no_inject_deadline_drop

    bridge_rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    app_rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    app_tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for sock in (bridge_rx, app_rx, app_tx):
        sock.settimeout(timeout_s)
    bridge_rx.bind((bind_host, int(eth_cfg.get("bind_port", 0))))
    app_rx.bind((bind_host, 0))
    bridge_addr = bridge_rx.getsockname()
    app_rx_addr = app_rx.getsockname()

    packets_sent = 0
    packets_received = 0
    errors: List[str] = []
    logger.emit(
        "ethernet_endpoint_open",
        mode="ethernet",
        endpoint="ethernet_udp_loopback",
        protocol="udp",
        bridge_rx={"host": bridge_addr[0], "port": bridge_addr[1]},
        app_rx={"host": app_rx_addr[0], "port": app_rx_addr[1]},
    )

    try:
        for packet in iter_packets("ethernet", packets_per_class, payload_bytes):
            packets_sent += 1
            app_tx.sendto(packet.payload, bridge_addr)
            ingress, _addr = bridge_rx.recvfrom(65535)
            if ingress != packet.payload:
                errors.append(f"ethernet ingress mismatch seq={packet.sequence}")
                continue
            delivered = path.transmit(dataclasses.replace(packet, payload=ingress))
            if delivered is None:
                continue
            bridge_rx.sendto(delivered, app_rx_addr)
            egress, _addr = app_rx.recvfrom(65535)
            if egress != delivered:
                errors.append(f"ethernet egress mismatch seq={packet.sequence}")
                continue
            packets_received += 1

        if inject_deadline_drop:
            drop_policy = path.policies["payload"]
            stale_packet = Packet(
                mode="ethernet",
                endpoint="ethernet_udp_loopback",
                traffic_class="payload",
                sequence=999002,
                payload=make_payload("ethernet", "payload", 999002, payload_bytes),
                created_ns=now_ns() - int((drop_policy.deadline_ms + 100.0) * 1_000_000),
            )
            packets_sent += 1
            app_tx.sendto(stale_packet.payload, bridge_addr)
            ingress, _addr = bridge_rx.recvfrom(65535)
            path.transmit(dataclasses.replace(stale_packet, payload=ingress))
    except Exception as exc:  # pragma: no cover - reported in validation output.
        errors.append(str(exc))
    finally:
        for sock in (bridge_rx, app_rx, app_tx):
            sock.close()

    passed = not errors and packets_received == packets_per_class * len(TRAFFIC_CLASSES)
    logger.emit(
        "ethernet_loopback_complete",
        mode="ethernet",
        endpoint="ethernet_udp_loopback",
        passed=passed,
        packets_sent=packets_sent,
        packets_received=packets_received,
        errors=errors,
    )
    return {
        "passed": passed,
        "packets_sent": packets_sent,
        "packets_received": packets_received,
        "errors": errors,
        "protocol": "udp",
        "bind_host": bind_host,
    }


def percentile(values: List[float], pct: float) -> Optional[float]:
    if not values:
        return None
    if len(values) == 1:
        return round(values[0], 3)
    sorted_values = sorted(values)
    index = min(len(sorted_values) - 1, max(0, round((pct / 100.0) * (len(sorted_values) - 1))))
    return round(sorted_values[index], 3)


def write_summary(
    run_dir: Path,
    run_id: str,
    selected_backend: str,
    component_status: Dict[str, Any],
    path: ModeledSoftwarePath,
    logger: TimingSupervisor,
    results: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    metrics_dir = run_dir / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    required_stages = {
        "endpoint_ingress",
        "queue_enqueue",
        "queue_dequeue",
        "sionna_query_result",
        "ns3_packet_result",
        "endpoint_egress",
    }
    summary = {
        "run_id": run_id,
        "radio_backend": selected_backend,
        "customer_ready": False,
        "path_mode": "hitl_virtual_loopback",
        "actual_modeled_path_available": component_status["all_available"],
        "missing_modeled_path_components": component_status["missing_components"],
        "modeled_path_blocker": None
        if component_status["all_available"]
        else "Actual traversal through network/bridge, network/ns3, and network/radio_provider is not available in this worktree.",
        "serial": results.get("serial"),
        "ethernet": results.get("ethernet"),
        "packets": {
            "submitted": path.submitted,
            "delivered": path.delivered,
            "dropped": len(path.drops),
        },
        "drops": path.drops,
        "latency_ms": {
            traffic_class: {
                "p50": percentile(values, 50),
                "p95": percentile(values, 95),
                "count": len(values),
            }
            for traffic_class, values in path.latencies_ms.items()
        },
        "timing": {
            "log": str(run_dir / "logs" / "timing.jsonl"),
            "bridge_log": str(run_dir / "logs" / "bridge.jsonl"),
            "hitl_log": str(run_dir / "logs" / "hitl_loopback.jsonl"),
            "stage_counts": logger.stage_counts,
            "has_stage_correlation": required_stages.issubset(logger.stage_counts),
        },
    }
    (metrics_dir / "hitl_loopback_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    write_run_validation_report(run_dir, summary)
    return summary


def write_run_validation_report(run_dir: Path, summary: Dict[str, Any]) -> None:
    serial = summary.get("serial") or {}
    ethernet = summary.get("ethernet") or {}
    blocker = summary.get("modeled_path_blocker") or "None."
    report = [
        "# HitL Loopback Validation",
        "",
        f"Run ID: `{summary['run_id']}`",
        "",
        "Customer-ready status: **not ready**.",
        "",
        "| Check | Status | Proof |",
        "| --- | --- | --- |",
        f"| Serial virtual loopback | {'Passed' if serial.get('passed') else 'Failed'} | PTY endpoint traffic sent `{serial.get('packets_sent', 0)}` packet(s), received `{serial.get('packets_received', 0)}` packet(s). |",
        f"| Ethernet virtual loopback | {'Passed' if ethernet.get('passed') else 'Failed'} | UDP endpoint traffic sent `{ethernet.get('packets_sent', 0)}` packet(s), received `{ethernet.get('packets_received', 0)}` packet(s). |",
        f"| Timing correlation | {'Passed' if summary['timing']['has_stage_correlation'] else 'Failed'} | Timing log: `{summary['timing']['log']}`. |",
        f"| Deadline/drop logging | {'Passed' if summary['packets']['dropped'] > 0 else 'Not observed'} | Drop events: `{summary['packets']['dropped']}`. |",
        f"| Actual ns-3/Sionna bridge traversal | {'Available' if summary['actual_modeled_path_available'] else 'Blocked'} | {blocker} |",
        "",
        "The virtual loopback validates endpoint shims and timing log shape only. It does not replace ns-3 packet behavior, online Sionna RT radio state, PCAP, FlowMonitor, or P0 no-bypass proof.",
        "",
    ]
    (run_dir / "validation_report.md").write_text("\n".join(report), encoding="utf-8")


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("serial", "ethernet", "both", "real-hardware"), default="both")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="HitL loopback YAML config.")
    parser.add_argument("--run-id", default=os.environ.get("RUN_ID"))
    parser.add_argument("--run-dir", default=os.environ.get("RUN_DIR"))
    parser.add_argument("--radio-backend", choices=("sim_2_4ghz", "real_modem_2_4ghz"))
    parser.add_argument("--packets-per-class", type=int, default=None)
    parser.add_argument("--payload-bytes", type=int, default=None)
    parser.add_argument("--baud-bps", type=int, default=None)
    parser.add_argument("--require-modeled-components", action="store_true")
    parser.add_argument("--no-inject-deadline-drop", action="store_true")
    return parser.parse_args(argv)


def prepare_run_dir(args: argparse.Namespace) -> Tuple[str, Path]:
    run_id = args.run_id or time.strftime("hitl_loopback_%Y%m%dT%H%M%SZ", time.gmtime())
    run_dir = Path(args.run_dir) if args.run_dir else ROOT_DIR / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "logs").mkdir(exist_ok=True)
    (run_dir / "metrics").mkdir(exist_ok=True)
    (run_dir / "pcap").mkdir(exist_ok=True)
    (run_dir / "flowmon").mkdir(exist_ok=True)
    (run_dir / "heatmaps").mkdir(exist_ok=True)
    (run_dir / "command.txt").write_text(" ".join([Path(sys.argv[0]).as_posix(), *sys.argv[1:]]) + "\n", encoding="utf-8")
    (run_dir / "environment.txt").write_text(
        "\n".join(
            [
                f"run_id={run_id}",
                f"utc={utc_now()}",
                f"root={ROOT_DIR}",
                f"python={sys.version.split()[0]}",
                f"ams_radio_backend={os.environ.get('AMS_RADIO_BACKEND', '')}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return run_id, run_dir


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    config = load_yaml(Path(args.config))
    selected_backend = select_backend(args, config)
    guard_hardware_mode(args, config, selected_backend)

    run_id, run_dir = prepare_run_dir(args)
    logger = TimingSupervisor(run_dir, run_id)
    try:
        policies = build_policies(config)
        component_status = detect_modeled_components(config)
        logger.emit(
            "hitl_loopback_start",
            mode=args.mode,
            radio_backend=selected_backend,
            component_status=component_status,
            require_modeled_components=args.require_modeled_components,
        )
        path = ModeledSoftwarePath(logger, policies, component_status, args.require_modeled_components)
        results: Dict[str, Dict[str, Any]] = {}
        if args.mode in ("serial", "both"):
            results["serial"] = run_serial_loopback(args, config, path, logger, run_dir)
        if args.mode in ("ethernet", "both"):
            results["ethernet"] = run_ethernet_loopback(args, config, path, logger)
        summary = write_summary(run_dir, run_id, selected_backend, component_status, path, logger, results)
        logger.emit("hitl_loopback_complete", mode=args.mode, summary=str(run_dir / "metrics" / "hitl_loopback_summary.json"))
    except RuntimeError as exc:
        logger.emit("hitl_loopback_failed", mode=args.mode, error=str(exc))
        print(f"FAIL {exc}", file=sys.stderr)
        return 5
    finally:
        logger.close()

    failed_modes = [name for name, result in results.items() if not result.get("passed")]
    print(f"Run directory: {run_dir}")
    print(f"Summary: {run_dir / 'metrics' / 'hitl_loopback_summary.json'}")
    print(f"Timing log: {run_dir / 'logs' / 'timing.jsonl'}")
    if summary["modeled_path_blocker"]:
        print(f"Modeled path blocker: {summary['modeled_path_blocker']}")
    if failed_modes:
        print(f"Failed loopback mode(s): {', '.join(failed_modes)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
