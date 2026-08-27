#!/usr/bin/env python3
"""Drive five real SITLs through Town01 via the ns-3 dual-UART packet path."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import selectors
import signal
import socket
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable


CONTROL_TOS = 184
PAYLOAD_TOS = 40
GCS_IP = "10.71.0.10"
MULTICAST_GROUP = "239.71.0.1"
MULTICAST_PORT = 14900
UAV_IDS = tuple(range(1, 6))


def endpoint_ip(index: int) -> str:
    return f"10.71.{index}.10"


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


def run_additional_agent(args: argparse.Namespace) -> int:
    index = int(args.index)
    local_ip = endpoint_ip(index)
    event_log = Path(args.event_log)
    ready_file = Path(args.ready_file)
    stop = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    p2p = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    p2p.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, 0)
    p2p.bind((local_ip, 14800 + index))
    p2p.setblocking(False)
    multicast = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    multicast.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    multicast.bind(("0.0.0.0", MULTICAST_PORT))
    membership = socket.inet_aton(MULTICAST_GROUP) + socket.inet_aton(local_ip)
    multicast.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
    multicast.setblocking(False)
    selector = selectors.DefaultSelector()
    selector.register(p2p, selectors.EVENT_READ, "p2p")
    selector.register(multicast, selectors.EVENT_READ, "p2mp")
    counters = Counter()
    ready_file.parent.mkdir(parents=True, exist_ok=True)
    ready_file.write_text("ready\n", encoding="utf-8")
    append_jsonl(event_log, {"event": "start", "uav": index, "monotonic_ns": time.monotonic_ns()})
    try:
        while not stop:
            for key, _mask in selector.select(0.25):
                data, source = key.fileobj.recvfrom(65535)
                digest = hashlib.sha256(data).hexdigest()
                counters[key.data] += 1
                append_jsonl(
                    event_log,
                    {
                        "event": "receive",
                        "kind": key.data,
                        "uav": index,
                        "bytes": len(data),
                        "sha256": digest,
                        "source": f"{source[0]}:{source[1]}",
                        "monotonic_ns": time.monotonic_ns(),
                    },
                )
                if key.data == "p2p":
                    response = b"P2P_ACK:" + str(index).encode() + b":" + data
                else:
                    response = b"P2MP_ACK:" + str(index).encode() + b":" + digest.encode()
                p2p.sendto(response, (GCS_IP, 14800))
                append_jsonl(
                    event_log,
                    {
                        "event": "ack",
                        "kind": key.data,
                        "uav": index,
                        "bytes": len(response),
                        "monotonic_ns": time.monotonic_ns(),
                    },
                )
    finally:
        append_jsonl(
            event_log,
            {"event": "stop", "uav": index, "counters": dict(counters), "monotonic_ns": time.monotonic_ns()},
        )
        selector.close()
        p2p.close()
        multicast.close()
    return 0


class ScenarioError(RuntimeError):
    """The observable five-UAV lifecycle did not complete."""


class FlightHarness:
    def __init__(self, run_dir: Path, node_state: Path, timeout_scale: float) -> None:
        os.environ.setdefault("MAVLINK20", "1")
        from pymavlink import mavutil

        self.mavutil = mavutil
        self.run_dir = run_dir
        self.node_state = node_state
        self.timeout_scale = timeout_scale
        self.selector = selectors.DefaultSelector()
        self.sockets: dict[str, socket.socket] = {}
        self.parsers: dict[str, Any] = {}
        self.transmitters: dict[str, Any] = {}
        for channel, port, tos in (
            ("control", 14600, CONTROL_TOS),
            ("payload", 14700, PAYLOAD_TOS),
            ("additional_data", 14800, 0),
        ):
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, tos)
            sock.bind((GCS_IP, port))
            sock.setblocking(False)
            self.sockets[channel] = sock
            self.selector.register(sock, selectors.EVENT_READ, channel)
            if channel != "additional_data":
                parser = mavutil.mavlink.MAVLink(None)
                parser.robust_parsing = True
                self.parsers[channel] = parser
                transmitter = mavutil.mavlink.MAVLink(None)
                transmitter.srcSystem = 255
                transmitter.srcComponent = 190
                self.transmitters[channel] = transmitter
        self.message_counts: Counter[tuple[str, int, str]] = Counter()
        self.latest: dict[tuple[str, int, str], Any] = {}
        self.latest_at_ns: dict[tuple[str, int, str], int] = {}
        self.acks: dict[tuple[str, int, int], tuple[Any, int]] = {}
        self.events_path = run_dir / "logs/scenario_events.jsonl"
        self.flight_csv = run_dir / "metrics/flight_lifecycle.csv"
        self.flight_csv.parent.mkdir(parents=True, exist_ok=True)
        self.flight_handle = self.flight_csv.open("w", encoding="utf-8", newline="")
        self.flight_writer = csv.DictWriter(
            self.flight_handle,
            fieldnames=["elapsed_s", "event", "uav", "x_m", "y_m", "z_m", "detail"],
        )
        self.flight_writer.writeheader()
        self.started = time.monotonic()
        self.summary: dict[str, Any] = {
            "status": "running",
            "uavs": {f"uav{index}": {"system_id": index, "phases": {}} for index in UAV_IDS},
            "command_acks": [],
            "status_texts": {f"uav{index}": [] for index in UAV_IDS},
        }

    def close(self) -> None:
        self.flight_handle.close()
        self.selector.close()
        for sock in self.sockets.values():
            sock.close()

    def event(self, name: str, *, uav: int = 0, detail: str = "") -> None:
        position = self.positions().get(f"uav{uav}") if uav else None
        row = {
            "elapsed_s": round(time.monotonic() - self.started, 3),
            "event": name,
            "uav": f"uav{uav}" if uav else "all",
            "x_m": position[0] if position else "",
            "y_m": position[1] if position else "",
            "z_m": position[2] if position else "",
            "detail": detail,
        }
        self.flight_writer.writerow(row)
        self.flight_handle.flush()
        append_jsonl(self.events_path, {**row, "monotonic_ns": time.monotonic_ns()})
        print(f"SCENARIO {name} uav={row['uav']} {detail}", flush=True)

    def positions(self) -> dict[str, list[float]]:
        try:
            state = json.loads(self.node_state.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        result: dict[str, list[float]] = {}
        for node in state.get("nodes", []):
            if not isinstance(node, dict) or node.get("stale"):
                continue
            position = node.get("position_m")
            if isinstance(position, list) and len(position) == 3:
                result[str(node.get("id"))] = [float(value) for value in position]
        return result

    def pump(self, timeout_s: float = 0.2) -> None:
        for key, _mask in self.selector.select(timeout_s):
            channel = str(key.data)
            data, source = key.fileobj.recvfrom(65535)
            if channel == "additional_data":
                self.latest[(channel, 0, data.decode("utf-8", errors="replace"))] = source
                self.latest_at_ns[(channel, 0, data.decode("utf-8", errors="replace"))] = time.monotonic_ns()
                continue
            now_ns = time.monotonic_ns()
            for message in self.parsers[channel].parse_buffer(data) or []:
                if message.get_type() == "BAD_DATA":
                    continue
                system_id = int(message.get_srcSystem())
                message_type = str(message.get_type())
                self.message_counts[(channel, system_id, message_type)] += 1
                key_name = (channel, system_id, message_type)
                self.latest[key_name] = message
                self.latest_at_ns[key_name] = now_ns
                if message_type == "COMMAND_ACK":
                    self.acks[(channel, system_id, int(message.command))] = (message, now_ns)
                elif message_type == "STATUSTEXT" and system_id in UAV_IDS:
                    raw_text = message.text
                    text = (
                        raw_text.decode("utf-8", errors="replace")
                        if isinstance(raw_text, bytes)
                        else str(raw_text)
                    ).rstrip("\x00")
                    record = {
                        "channel": channel,
                        "severity": int(message.severity),
                        "text": text,
                    }
                    records = self.summary["status_texts"][f"uav{system_id}"]
                    if not records or records[-1]["text"] != text:
                        records.append(record)
                        del records[:-20]
                        print(
                            f"AUTOPILOT uav={system_id} severity={record['severity']} {text}",
                            flush=True,
                        )

    def wait(self, predicate: Callable[[], bool], timeout_s: float, description: str) -> None:
        deadline = time.monotonic() + timeout_s * self.timeout_scale
        while time.monotonic() < deadline:
            if predicate():
                return
            self.pump(min(0.25, deadline - time.monotonic()))
        raise ScenarioError(f"timeout waiting for {description}")

    def send(self, channel: str, system_id: int, message: Any) -> int:
        frame = message.pack(self.transmitters[channel], force_mavlink1=False)
        port = (14600 if channel == "control" else 14700) + system_id
        return self.sockets[channel].sendto(frame, (endpoint_ip(system_id), port))

    def command_all(
        self,
        channel: str,
        command: int,
        params: list[float],
        timeout_s: float,
        label: str,
    ) -> dict[int, float]:
        pending = set(UAV_IDS)
        sent_at: dict[int, int] = {}
        latency: dict[int, float] = {}
        next_send = 0.0
        deadline = time.monotonic() + timeout_s * self.timeout_scale
        accepted = {
            int(self.mavutil.mavlink.MAV_RESULT_ACCEPTED),
            int(self.mavutil.mavlink.MAV_RESULT_IN_PROGRESS),
        }
        while pending and time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_send:
                for system_id in sorted(pending):
                    message = self.transmitters[channel].command_long_encode(
                        system_id, 1, command, 0, *params
                    )
                    self.send(channel, system_id, message)
                    sent_at.setdefault(system_id, time.monotonic_ns())
                next_send = now + 1.0
            self.pump(0.2)
            for system_id in tuple(pending):
                ack = self.acks.get((channel, system_id, command))
                if ack is None or ack[1] < sent_at[system_id]:
                    continue
                result = int(ack[0].result)
                if result in accepted:
                    latency[system_id] = (ack[1] - sent_at[system_id]) / 1e6
                    self.summary["command_acks"].append(
                        {
                            "channel": channel,
                            "uav": f"uav{system_id}",
                            "command": command,
                            "label": label,
                            "result": result,
                            "latency_ms": latency[system_id],
                        }
                    )
                    pending.remove(system_id)
        if pending:
            observed = {
                system_id: int(self.acks[(channel, system_id, command)][0].result)
                for system_id in UAV_IDS
                if (channel, system_id, command) in self.acks
            }
            raise ScenarioError(f"{label} ACK missing/rejected for {sorted(pending)}; observed={observed}")
        return latency

    def wait_heartbeats(self) -> None:
        self.wait(
            lambda: all(
                (channel, system_id, "HEARTBEAT") in self.latest
                for channel in ("control", "payload")
                for system_id in UAV_IDS
            ),
            90,
            "control and payload UART heartbeats from all five SITLs",
        )
        for system_id in UAV_IDS:
            self.summary["uavs"][f"uav{system_id}"]["phases"]["heartbeat"] = True
        self.event("heartbeats_ready")

    def set_mode_all(self, custom_mode: int, label: str, timeout_s: float = 30.0) -> None:
        flag = int(self.mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED)
        pending = set(UAV_IDS)
        deadline = time.monotonic() + timeout_s * self.timeout_scale
        next_send = 0.0
        while pending and time.monotonic() < deadline:
            if time.monotonic() >= next_send:
                for system_id in sorted(pending):
                    message = self.transmitters["control"].set_mode_encode(
                        system_id, flag, custom_mode
                    )
                    self.send("control", system_id, message)
                next_send = time.monotonic() + 1.0
            self.pump(0.2)
            for system_id in tuple(pending):
                heartbeat = self.latest.get(("control", system_id, "HEARTBEAT"))
                if heartbeat is not None and int(heartbeat.custom_mode) == custom_mode:
                    pending.remove(system_id)
        if pending:
            raise ScenarioError(f"{label} mode not observed for {sorted(pending)}")
        self.event(label)

    def set_guided(self) -> None:
        self.set_mode_all(4, "guided_mode")

    def request_streams(self) -> None:
        command = int(self.mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL)
        self.command_all(
            "payload",
            command,
            [float(self.mavutil.mavlink.MAVLINK_MSG_ID_ATTITUDE), 200000.0, 0, 0, 0, 0, 0],
            30,
            "payload_attitude_interval",
        )
        payload_start = time.monotonic_ns()
        self.wait(
            lambda: all(
                self.latest_at_ns.get(("payload", system_id, "ATTITUDE"), 0) >= payload_start
                for system_id in UAV_IDS
            ),
            20,
            "payload ATTITUDE telemetry from all UAVs",
        )
        self.command_all(
            "control",
            command,
            [float(self.mavutil.mavlink.MAVLINK_MSG_ID_LOCAL_POSITION_NED), 200000.0, 0, 0, 0, 0, 0],
            30,
            "control_local_position_interval",
        )
        self.event("dual_uart_telemetry")

    def arm_takeoff_move_land(self) -> None:
        initial = self.positions()
        if set(initial) < {f"uav{index}" for index in UAV_IDS}:
            raise ScenarioError("fresh tracker positions are unavailable before flight")
        self.summary["initial_positions_m"] = initial
        self.set_guided()
        self.command_all(
            "control",
            int(self.mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM),
            [1.0, 0, 0, 0, 0, 0, 0],
            45,
            "arm",
        )
        self.wait(
            lambda: all(
                (int(self.latest[("control", system_id, "HEARTBEAT")].base_mode)
                 & int(self.mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED))
                for system_id in UAV_IDS
            ),
            20,
            "armed heartbeat for all UAVs",
        )
        for system_id in UAV_IDS:
            self.summary["uavs"][f"uav{system_id}"]["phases"]["arm"] = True
        self.event("armed")

        self.command_all(
            "control",
            int(self.mavutil.mavlink.MAV_CMD_NAV_TAKEOFF),
            [0, 0, 0, 0, 0, 0, 15.0],
            30,
            "takeoff",
        )
        self.wait(
            lambda: all(
                self.positions().get(f"uav{system_id}", [0, 0, -1e9])[2]
                >= initial[f"uav{system_id}"][2] + 8.0
                for system_id in UAV_IDS
            ),
            60,
            "all UAVs above takeoff threshold",
        )
        for system_id in UAV_IDS:
            self.summary["uavs"][f"uav{system_id}"]["phases"]["takeoff"] = True
        self.event("takeoff_complete")

        hold_started = time.monotonic()
        while time.monotonic() - hold_started < 4.0:
            self.pump(0.2)
        for system_id in UAV_IDS:
            self.summary["uavs"][f"uav{system_id}"]["phases"]["hold"] = True
        self.event("hold_complete")

        move_start = self.positions()
        type_mask = 3576
        for system_id in UAV_IDS:
            message = self.transmitters["control"].set_position_target_local_ned_encode(
                0,
                system_id,
                1,
                int(self.mavutil.mavlink.MAV_FRAME_LOCAL_OFFSET_NED),
                type_mask,
                4.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
            )
            self.send("control", system_id, message)
        self.wait(
            lambda: all(
                math.hypot(
                    self.positions().get(f"uav{system_id}", move_start[f"uav{system_id}"])[0]
                    - move_start[f"uav{system_id}"][0],
                    self.positions().get(f"uav{system_id}", move_start[f"uav{system_id}"])[1]
                    - move_start[f"uav{system_id}"][1],
                )
                >= 3.0
                for system_id in UAV_IDS
            ),
            45,
            "five-UAV guided displacement",
        )
        for system_id in UAV_IDS:
            self.summary["uavs"][f"uav{system_id}"]["phases"]["movement"] = True
        self.event("movement_complete")

        self.command_all(
            "control",
            int(self.mavutil.mavlink.MAV_CMD_NAV_LAND),
            [0, 0, 0, 0, 0, 0, 0],
            30,
            "land",
        )
        landing_started = self.positions()
        self.set_mode_all(9, "land_mode")
        self.wait(
            lambda: all(
                not (
                    int(self.latest[("control", system_id, "HEARTBEAT")].base_mode)
                    & int(self.mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED)
                )
                for system_id in UAV_IDS
            ),
            90,
            "all UAVs disarmed after landing",
        )
        final = self.positions()
        self.summary["final_positions_m"] = final
        self.summary["landing_descent_m"] = {
            f"uav{system_id}": round(
                landing_started[f"uav{system_id}"][2] - final[f"uav{system_id}"][2], 3
            )
            for system_id in UAV_IDS
        }
        for system_id in UAV_IDS:
            self.summary["uavs"][f"uav{system_id}"]["phases"]["land"] = True
        self.event("landing_complete")

    def additional_data(self) -> None:
        sock = self.sockets["additional_data"]
        p2p_sent: dict[int, list[str]] = {index: [] for index in UAV_IDS}
        for sequence in range(10):
            for index in UAV_IDS:
                payload = f"TOWN01:P2P:{index}:{sequence}".encode().ljust(256, b".")
                sock.sendto(payload, (endpoint_ip(index), 14800 + index))
                p2p_sent[index].append(hashlib.sha256(payload).hexdigest())
        p2p_acks: Counter[int] = Counter()
        deadline = time.monotonic() + 20 * self.timeout_scale
        multicast_acks: set[int] = set()
        multicast_payload = b"TOWN01:P2MP:ALL-FIVE"
        multicast_sent = 0
        next_multicast = 0.0
        while time.monotonic() < deadline and (
            any(p2p_acks[index] < len(p2p_sent[index]) for index in UAV_IDS)
            or len(multicast_acks) < 5
        ):
            if time.monotonic() >= next_multicast and len(multicast_acks) < 5:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(GCS_IP))
                # The packet crosses the GCS endpoint router, the shared ns-3
                # radio, and each UAV endpoint router.  A TTL of one expires at
                # the first routed hop and can never exercise the P2MP path.
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 8)
                sock.sendto(multicast_payload, (MULTICAST_GROUP, MULTICAST_PORT))
                multicast_sent += 1
                next_multicast = time.monotonic() + 1.0
            for key, _mask in self.selector.select(0.2):
                channel = str(key.data)
                data, _source = key.fileobj.recvfrom(65535)
                if channel != "additional_data":
                    now_ns = time.monotonic_ns()
                    for message in self.parsers[channel].parse_buffer(data) or []:
                        if message.get_type() == "BAD_DATA":
                            continue
                        system_id = int(message.get_srcSystem())
                        message_type = str(message.get_type())
                        self.message_counts[(channel, system_id, message_type)] += 1
                        self.latest[(channel, system_id, message_type)] = message
                        self.latest_at_ns[(channel, system_id, message_type)] = now_ns
                        if message_type == "COMMAND_ACK":
                            self.acks[(channel, system_id, int(message.command))] = (message, now_ns)
                    continue
                text = data.decode("utf-8", errors="replace")
                if text.startswith("P2P_ACK:"):
                    p2p_acks[int(text.split(":", 2)[1])] += 1
                elif text.startswith("P2MP_ACK:"):
                    multicast_acks.add(int(text.split(":", 2)[1]))
        if any(p2p_acks[index] < len(p2p_sent[index]) for index in UAV_IDS):
            raise ScenarioError(f"P2P data missing from {dict(p2p_acks)}")
        if multicast_acks != set(UAV_IDS):
            raise ScenarioError(f"P2MP data reached only {sorted(multicast_acks)}")
        self.summary["additional_data"] = {
            "p2p_packets_sent": 50,
            "p2p_ack_counts": {f"uav{index}": p2p_acks[index] for index in UAV_IDS},
            "p2mp_root_packets_sent": multicast_sent,
            "p2mp_receivers": [f"uav{index}" for index in sorted(multicast_acks)],
        }
        self.event("additional_data_complete")

    def run(self) -> dict[str, Any]:
        self.wait_heartbeats()
        self.request_streams()
        self.additional_data()
        self.arm_takeoff_move_land()
        self.summary["status"] = "passed"
        self.summary["duration_s"] = round(time.monotonic() - self.started, 3)
        self.summary["message_counts"] = {
            f"{channel}:uav{system_id}:{message_type}": count
            for (channel, system_id, message_type), count in sorted(self.message_counts.items())
        }
        return self.summary


def run_scenario(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    harness = FlightHarness(run_dir, Path(args.node_state).resolve(), args.timeout_scale)
    summary_path = run_dir / "metrics/scenario_summary.json"
    try:
        summary = harness.run()
    except Exception as exc:
        harness.summary["status"] = "failed"
        harness.summary["error"] = str(exc)
        harness.summary["duration_s"] = round(time.monotonic() - harness.started, 3)
        write_json(summary_path, harness.summary)
        print(f"FAIL scenario: {exc}", file=sys.stderr)
        return 1
    finally:
        harness.close()
    write_json(summary_path, summary)
    print(json.dumps(summary, sort_keys=True))
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    agent = commands.add_parser("additional-agent")
    agent.add_argument("--index", type=int, choices=UAV_IDS, required=True)
    agent.add_argument("--event-log", required=True)
    agent.add_argument("--ready-file", required=True)
    agent.set_defaults(function=run_additional_agent)
    scenario = commands.add_parser("run")
    scenario.add_argument("--run-dir", required=True)
    scenario.add_argument("--node-state", required=True)
    scenario.add_argument("--timeout-scale", type=float, default=1.0)
    scenario.set_defaults(function=run_scenario)
    return root


def main() -> int:
    args = parser().parse_args()
    return int(args.function(args))


if __name__ == "__main__":
    raise SystemExit(main())
