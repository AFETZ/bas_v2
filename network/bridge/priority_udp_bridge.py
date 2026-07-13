#!/usr/bin/env python3
"""Opaque UDP priority queue adapter for the ns-3 endpoint bridge.

MAVLink parsing and routing are intentionally out of scope here. MAVLink
packets are classified by configured ingress port and forwarded as datagrams
after the class queue has applied priority, deadline, and pacing policy.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - covered by shell preflight.
    yaml = None


ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_ENDPOINTS = ROOT_DIR / "network" / "config" / "endpoints.yaml"
TRAFFIC_CLASSES = ("control", "payload", "additional_data")
PORT_KEYS = {
    "control": "control_udp",
    "payload": "payload_udp",
    "additional_data": "additional_data_udp",
}
NS3_INGRESS_KEYS = {
    "control": "control_ingress_udp",
    "payload": "payload_ingress_udp",
    "additional_data": "additional_data_ingress_udp",
}


@dataclass(frozen=True)
class QueuePolicy:
    traffic_class: str
    priority: int
    max_packets: int
    byte_pacing_bps: int
    deadline_ms: int
    drop_policy: str


@dataclass
class Datagram:
    sequence: int
    traffic_class: str
    endpoint: str
    payload: bytes
    target: tuple[str, int]
    source: tuple[str, int]
    enqueued_at: float


class PriorityDatagramQueue:
    def __init__(self, policies: dict[str, QueuePolicy], log):
        self.policies = policies
        self.queues: dict[str, deque[Datagram]] = {name: deque() for name in policies}
        self.sequence = 0
        self.log = log

    def _emit(self, event: str, **fields: Any) -> None:
        record = {
            "time_s": time.time(),
            "event": event,
            **fields,
            "queue_depth": {name: len(queue) for name, queue in self.queues.items()},
        }
        self.log.write(json.dumps(record, sort_keys=True) + "\n")
        self.log.flush()

    def enqueue(
        self,
        traffic_class: str,
        endpoint: str,
        payload: bytes,
        target: tuple[str, int],
        source: tuple[str, int],
        now: float | None = None,
    ) -> bool:
        now = time.monotonic() if now is None else now
        policy = self.policies[traffic_class]
        queue = self.queues[traffic_class]
        if len(queue) >= policy.max_packets:
            if policy.drop_policy == "deadline_drop_oldest" and queue:
                dropped = queue.popleft()
                self._emit(
                    "drop_oldest",
                    traffic_class=traffic_class,
                    endpoint=dropped.endpoint,
                    sequence=dropped.sequence,
                    age_ms=(now - dropped.enqueued_at) * 1000.0,
                )
            else:
                self._emit(
                    "drop_tail",
                    traffic_class=traffic_class,
                    endpoint=endpoint,
                    bytes=len(payload),
                    reason="queue_full",
                )
                return False

        self.sequence += 1
        queue.append(Datagram(self.sequence, traffic_class, endpoint, payload, target, source, now))
        self._emit(
            "enqueue",
            traffic_class=traffic_class,
            endpoint=endpoint,
            sequence=self.sequence,
            bytes=len(payload),
            source=f"{source[0]}:{source[1]}",
            target=f"{target[0]}:{target[1]}",
        )
        return True

    def dequeue(self, now: float | None = None) -> Datagram | None:
        now = time.monotonic() if now is None else now
        ordered_classes = sorted(self.policies.values(), key=lambda item: item.priority)
        for policy in ordered_classes:
            queue = self.queues[policy.traffic_class]
            while queue:
                candidate = queue[0]
                age_ms = (now - candidate.enqueued_at) * 1000.0
                if age_ms <= policy.deadline_ms:
                    break
                dropped = queue.popleft()
                self._emit(
                    "deadline_drop",
                    traffic_class=policy.traffic_class,
                    endpoint=dropped.endpoint,
                    sequence=dropped.sequence,
                    age_ms=age_ms,
                )
            if queue:
                item = queue.popleft()
                self._emit(
                    "dequeue",
                    traffic_class=item.traffic_class,
                    endpoint=item.endpoint,
                    sequence=item.sequence,
                    age_ms=(now - item.enqueued_at) * 1000.0,
                    bytes=len(item.payload),
                )
                return item
        return None


class IngressProtocol(asyncio.DatagramProtocol):
    def __init__(self, queue: PriorityDatagramQueue, route: dict[str, Any]):
        self.queue = queue
        self.route = route

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.queue.enqueue(
            self.route["traffic_class"],
            self.route["endpoint"],
            data,
            (self.route["target_host"], int(self.route["target_port"])),
            addr,
        )


def load_yaml(path: Path) -> dict[str, Any]:
    if yaml is None:
        raise RuntimeError("PyYAML is required: python3 -m pip install PyYAML")
    return yaml.safe_load(path.read_text()) or {}


def load_policies(config: dict[str, Any]) -> dict[str, QueuePolicy]:
    policies = {}
    for traffic_class, values in config["bridge"]["queues"].items():
        policies[traffic_class] = QueuePolicy(
            traffic_class=traffic_class,
            priority=int(values["priority"]),
            max_packets=int(values["max_packets"]),
            byte_pacing_bps=int(values["byte_pacing_bps"]),
            deadline_ms=int(values["deadline_ms"]),
            drop_policy=str(values["drop_policy"]),
        )
    return policies


def load_routes(config: dict[str, Any]) -> list[dict[str, Any]]:
    routes: list[dict[str, Any]] = []
    bridge = config["bridge"]
    for uav in config["uavs"]:
        for traffic_class in TRAFFIC_CLASSES:
            routes.append(
                {
                    "endpoint": f"{uav['name']}_{traffic_class}",
                    "uav": uav["name"],
                    "system_id": uav["system_id"],
                    "traffic_class": traffic_class,
                    "bind_host": bridge["ground_control"]["bind_host"],
                    "bind_port": int(uav["bridge_ports"][PORT_KEYS[traffic_class]]),
                    "target_host": bridge["ns3"]["ingress_host"],
                    "target_port": int(uav["ns3_ports"][NS3_INGRESS_KEYS[traffic_class]]),
                }
            )
    return routes


async def run_bridge(config: dict[str, Any], log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    loop = asyncio.get_running_loop()
    transports = []
    with log_path.open("a") as log:
        queue = PriorityDatagramQueue(load_policies(config), log)
        for route in load_routes(config):
            transport, _protocol = await loop.create_datagram_endpoint(
                lambda route=route: IngressProtocol(queue, route),
                local_addr=(route["bind_host"], route["bind_port"]),
            )
            transports.append(transport)
            queue._emit("bind", **route)

        output = socket_transport = None
        output, _protocol = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol,
            local_addr=("0.0.0.0", 0),
        )
        socket_transport = output
        try:
            while True:
                item = queue.dequeue()
                if item is None:
                    await asyncio.sleep(0.001)
                    continue
                socket_transport.sendto(item.payload, item.target)
                policy = queue.policies[item.traffic_class]
                pace_s = (len(item.payload) * 8.0) / float(policy.byte_pacing_bps)
                if pace_s > 0:
                    await asyncio.sleep(pace_s)
        finally:
            for transport in transports:
                transport.close()
            if output is not None:
                output.close()


def run_self_test() -> int:
    class MemoryLog:
        def __init__(self):
            self.records: list[str] = []

        def write(self, value: str) -> None:
            self.records.append(value)

        def flush(self) -> None:
            pass

    policies = {
        "control": QueuePolicy("control", 0, 4, 115200, 250, "deadline_drop_oldest"),
        "payload": QueuePolicy("payload", 1, 1, 57600, 1000, "bounded_drop_tail"),
        "additional_data": QueuePolicy("additional_data", 2, 4, 57600, 2000, "bounded_drop_tail"),
    }
    queue = PriorityDatagramQueue(policies, MemoryLog())
    target = ("127.0.0.1", 9)
    source = ("127.0.0.1", 1)
    queue.enqueue("payload", "payload", b"p1", target, source, now=0.0)
    queue.enqueue("additional_data", "additional", b"a1", target, source, now=0.0)
    queue.enqueue("control", "control", b"c1", target, source, now=0.0)

    order = [queue.dequeue(now=0.001).traffic_class for _ in range(3)]
    if order != ["control", "payload", "additional_data"]:
        print(f"FAIL priority order {order}", file=sys.stderr)
        return 1

    queue.enqueue("payload", "payload", b"p2", target, source, now=0.0)
    accepted = queue.enqueue("payload", "payload", b"p3", target, source, now=0.0)
    if accepted:
        print("FAIL bounded payload queue accepted a tail packet after full", file=sys.stderr)
        return 1

    print("PASS priority_udp_bridge self-test")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoints", type=Path, default=DEFAULT_ENDPOINTS)
    parser.add_argument("--log", type=Path, default=ROOT_DIR / "runs" / "bridge_dry_run" / "logs" / "bridge.jsonl")
    parser.add_argument("--dry-run", action="store_true", help="Print configured UDP ingress routes and exit.")
    parser.add_argument("--self-test", action="store_true", help="Run deterministic queue-priority checks and exit.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.self_test:
        return run_self_test()

    try:
        config = load_yaml(args.endpoints)
    except Exception as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        for route in load_routes(config):
            print(
                f"{route['endpoint']} sysid={route['system_id']} "
                f"{route['bind_host']}:{route['bind_port']} -> "
                f"{route['target_host']}:{route['target_port']} "
                f"class={route['traffic_class']}"
            )
        return 0

    try:
        asyncio.run(run_bridge(config, args.log))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
