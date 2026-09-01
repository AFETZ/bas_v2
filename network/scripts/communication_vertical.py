#!/usr/bin/env python3
"""Small runtime probe for the product communication vertical slice."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import selectors
import signal
import socket
import statistics
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path
from typing import Any

from serial_transport import Encoder, MavlinkStreamCounter, Reassembler, TransportCounters


DATA_HEADER = struct.Struct("!4sBBIQ")
DATA_MAGIC = b"BAS2"
PHASE_CODES = {"p2p": 1, "p2mp": 2, "contention": 3}


def endpoint(value: str) -> tuple[str, int]:
    host, port = value.rsplit(":", 1)
    return host, int(port)


def write_json(path: str | Path, value: Any) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def append_jsonl(handle: Any, value: dict[str, Any]) -> None:
    handle.write(json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def configure_tty_baud(descriptor: int, baud_rate: int) -> int:
    speeds = {
        57600: termios.B57600,
        115200: termios.B115200,
        230400: termios.B230400,
    }
    if baud_rate not in speeds:
        raise ValueError(f"unsupported UART baud rate: {baud_rate}")
    attributes = termios.tcgetattr(descriptor)
    attributes[4] = speeds[baud_rate]
    attributes[5] = speeds[baud_rate]
    termios.tcsetattr(descriptor, termios.TCSANOW, attributes)
    observed = termios.tcgetattr(descriptor)
    if observed[4] != speeds[baud_rate] or observed[5] != speeds[baud_rate]:
        raise RuntimeError(f"UART did not retain requested baud rate {baud_rate}")
    return speeds[baud_rate]


def run_uart_adapter(args: argparse.Namespace) -> int:
    os.environ.setdefault("MAVLINK20", "1")
    from pymavlink import mavutil

    bind_address = endpoint(args.bind)
    peer_address = endpoint(args.peer)
    uart = os.open(args.tty, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    speed_constant = configure_tty_baud(uart, args.baud_rate)
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tos_by_channel = {"control": 184, "payload": 40}
    tos = int(args.tos if args.tos is not None else tos_by_channel.get(args.channel, 0))
    if not 0 <= tos <= 255:
        raise ValueError("UART adapter TOS must be in 0..255")
    udp.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, tos)
    if udp.getsockopt(socket.IPPROTO_IP, socket.IP_TOS) != tos:
        raise RuntimeError(f"UART adapter did not retain requested TOS {tos}")
    udp.bind(bind_address)
    udp.setblocking(False)
    selector = selectors.DefaultSelector()
    selector.register(uart, selectors.EVENT_READ, "uart")
    selector.register(udp, selectors.EVENT_READ, "udp")
    running = True

    def stop(_signum: int, _frame: Any) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    counters = TransportCounters()
    encoder = (
        Encoder(
            channel=args.channel,
            uav_id=args.uav_id,
            direction="uart_to_gcs",
            max_payload=args.chunk_payload_bytes,
        )
        if args.framed
        else None
    )
    reassembler = (
        Reassembler(
            channel=args.channel,
            uav_id=args.uav_id,
            direction="gcs_to_uart",
            timeout_ms=args.reassembly_timeout_ms,
            counters=counters,
        )
        if args.framed
        else None
    )
    input_frames = MavlinkStreamCounter()
    output_frames = MavlinkStreamCounter()
    input_parser = mavutil.mavlink.MAVLink(None)
    output_parser = mavutil.mavlink.MAVLink(None)
    input_parser.robust_parsing = True
    output_parser.robust_parsing = True
    ready = {
        "status": "ready",
        "pid": os.getpid(),
        "channel": args.channel,
        "uav_id": args.uav_id,
        "tty": args.tty,
        "tty_realpath": os.path.realpath(args.tty),
        "baud_rate": args.baud_rate,
        "termios_speed_constant": speed_constant,
        "bind": args.bind,
        "peer": args.peer,
        "tos": tos,
        "transport_framing": "serial_chunk_v1" if args.framed else "raw_datagram",
    }
    write_json(args.ready_file, ready)
    last_metrics_ns = 0

    def metrics_payload() -> dict[str, Any]:
        values = counters.as_dict()
        input_snapshot = input_frames.snapshot()
        output_snapshot = output_frames.snapshot()
        values.update(
            {
                "pid": os.getpid(),
                "uav_id": args.uav_id,
                "channel": args.channel,
                "tty": args.tty,
                "tty_realpath": os.path.realpath(args.tty),
                "baud_rate": args.baud_rate,
                "bind": args.bind,
                "peer": args.peer,
                "transport_framing": ready["transport_framing"],
                "mavlink_input": input_snapshot,
                "mavlink_output": output_snapshot,
            }
        )
        values["frames"] = input_snapshot["frames"] + output_snapshot["frames"]
        values["incomplete_frames"] += (
            input_snapshot["incomplete_frames"] + output_snapshot["incomplete_frames"]
        )
        values["discarded_frames"] += (
            input_snapshot["discarded_frames"] + output_snapshot["discarded_frames"]
        )
        return values

    def publish_metrics(now_ns: int, *, force: bool = False) -> None:
        nonlocal last_metrics_ns
        if not args.metrics_output:
            return
        if force or now_ns - last_metrics_ns >= args.metrics_period_ms * 1_000_000:
            write_json(args.metrics_output, metrics_payload())
            last_metrics_ns = now_ns

    with Path(args.event_log).open("a", encoding="utf-8") as events:
        def emit_event(value: dict[str, Any]) -> None:
            if args.event_logging == "batched_trace":
                append_jsonl(events, value)

        def emit_mavlink_frames(direction: str, parser: Any, record: bytes, observed_ns: int) -> None:
            if args.event_logging != "batched_trace":
                return
            for message in parser.parse_buffer(record) or []:
                if message.get_type() == "BAD_DATA":
                    continue
                emit_event(
                    {
                        "event": "mavlink_frame",
                        "channel": args.channel,
                        "uav_id": args.uav_id,
                        "direction": direction,
                        "sysid": int(message.get_srcSystem()),
                        "compid": int(message.get_srcComponent()),
                        "msgid": int(message.get_msgId()),
                        "message_name": str(message.get_type()),
                        "message_bytes": len(message.get_msgbuf()),
                        "command": int(message.command) if hasattr(message, "command") else None,
                        "confirmation": int(message.confirmation) if hasattr(message, "confirmation") else None,
                        "target_system": int(message.target_system) if hasattr(message, "target_system") else None,
                        "target_component": int(message.target_component) if hasattr(message, "target_component") else None,
                        "monotonic_ns": observed_ns,
                    },
                )
        try:
            while running:
                now_ns = time.monotonic_ns()
                if reassembler is not None:
                    for record in reassembler.expire(now_ns):
                        output_frames.feed(record)
                        view = memoryview(record)
                        while view and running:
                            try:
                                written = os.write(uart, view)
                                counters.uart_output_bytes += written
                                view = view[written:]
                            except BlockingIOError:
                                select_write = selectors.DefaultSelector()
                                select_write.register(uart, selectors.EVENT_WRITE)
                                select_write.select(0.25)
                                select_write.close()
                publish_metrics(now_ns)
                for key, _mask in selector.select(0.25):
                    if key.data == "uart":
                        try:
                            data = os.read(uart, 4096)
                        except BlockingIOError:
                            continue
                        if not data:
                            continue
                        observed_ns = time.monotonic_ns()
                        counters.uart_input_bytes += len(data)
                        input_frames.feed(data)
                        emit_mavlink_frames("uart_to_ns3", input_parser, data, observed_ns)
                        datagrams = encoder.encode(data, observed_ns) if encoder else [data]
                        if encoder:
                            counters.records_encoded += 1
                            counters.chunks_encoded += len(datagrams)
                        for fragment_index, datagram in enumerate(datagrams):
                            udp.sendto(datagram, peer_address)
                            counters.ns3_input_bytes += len(datagram)
                            emit_event(
                                {
                                    "event": "serial_chunk_tx" if encoder else "serial_tx",
                                    "channel": args.channel,
                                    "uav_id": args.uav_id,
                                    "direction": "uart_to_ns3" if encoder else "uart_to_udp",
                                    "bytes": len(datagram),
                                    "uart_record_bytes": len(data),
                                    "network_bytes": len(datagram),
                                    "fragment_index": fragment_index,
                                    "fragment_count": len(datagrams),
                                    "sha256": sha256(datagram),
                                    "monotonic_ns": observed_ns,
                                },
                            )
                    else:
                        try:
                            data, source = udp.recvfrom(65535)
                        except BlockingIOError:
                            continue
                        observed_ns = time.monotonic_ns()
                        records = reassembler.ingest(data, observed_ns) if reassembler else [data]
                        if not reassembler:
                            counters.ns3_output_bytes += len(data)
                        for record in records:
                            output_frames.feed(record)
                            view = memoryview(record)
                            while view and running:
                                try:
                                    written = os.write(uart, view)
                                    counters.uart_output_bytes += written
                                    view = view[written:]
                                except BlockingIOError:
                                    select_write = selectors.DefaultSelector()
                                    select_write.register(uart, selectors.EVENT_WRITE)
                                    select_write.select(0.25)
                                    select_write.close()
                            if not view:
                                # This is the receiver-side UART hand-off after the
                                # whole MAVLink record has reached the real PTY.
                                emit_mavlink_frames("ns3_to_uart", output_parser, record, time.monotonic_ns())
                        emit_event(
                            {
                                "event": "serial_chunk_rx" if reassembler else "serial_rx",
                                "channel": args.channel,
                                "uav_id": args.uav_id,
                                "direction": "ns3_to_uart" if reassembler else "udp_to_uart",
                                "bytes": len(data),
                                "network_bytes": len(data),
                                "uart_records_released": len(records),
                                "uart_bytes_released": sum(len(record) for record in records),
                                "sha256": sha256(data),
                                "source": f"{source[0]}:{source[1]}",
                                "monotonic_ns": observed_ns,
                            },
                        )
        finally:
            if reassembler is not None:
                reassembler.expire(time.monotonic_ns(), force=True)
            publish_metrics(time.monotonic_ns(), force=True)
            selector.close()
            udp.close()
            os.close(uart)
    return 0


def mavlink_parser(mavutil: Any) -> Any:
    parser = mavutil.mavlink.MAVLink(None)
    parser.robust_parsing = True
    return parser


def build_command(mavutil: Any, command: int, params: list[float]) -> bytes:
    transmitter = mavutil.mavlink.MAVLink(None)
    transmitter.srcSystem = 255
    transmitter.srcComponent = 190
    message = transmitter.command_long_encode(
        1,
        1,
        command,
        0,
        *params,
    )
    frame = message.pack(transmitter, force_mavlink1=False)
    if not frame or frame[0] != 0xFD:
        raise RuntimeError("pymavlink did not encode a MAVLink2 frame")
    return frame


def receive_mavlink(
    selector: selectors.BaseSelector,
    parsers: dict[str, Any],
    deadline: float,
) -> list[tuple[str, Any, tuple[str, int]]]:
    messages: list[tuple[str, Any, tuple[str, int]]] = []
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return messages
    for key, _mask in selector.select(min(0.25, remaining)):
        channel = str(key.data)
        data, source = key.fileobj.recvfrom(65535)
        for message in parsers[channel].parse_buffer(data) or []:
            if message.get_type() != "BAD_DATA":
                messages.append((channel, message, source))
    return messages


def run_mavlink_probe(args: argparse.Namespace) -> int:
    os.environ.setdefault("MAVLINK20", "1")
    from pymavlink import mavutil

    sockets: dict[str, socket.socket] = {}
    parsers = {"control": mavlink_parser(mavutil), "payload": mavlink_parser(mavutil)}
    selector = selectors.DefaultSelector()
    for channel, bind_value in (
        ("control", args.control_bind),
        ("payload", args.payload_bind),
    ):
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.bind(endpoint(bind_value))
        udp.setblocking(False)
        sockets[channel] = udp
        selector.register(udp, selectors.EVENT_READ, channel)

    heartbeats: dict[str, dict[str, Any]] = {}
    counts = {"control": 0, "payload": 0}
    deadline = time.monotonic() + args.timeout_s
    while len(heartbeats) < 2 and time.monotonic() < deadline:
        for channel, message, source in receive_mavlink(selector, parsers, deadline):
            counts[channel] += 1
            if message.get_type() == "HEARTBEAT" and message.get_srcSystem() == 1:
                heartbeats[channel] = {
                    "system_id": message.get_srcSystem(),
                    "component_id": message.get_srcComponent(),
                    "source": f"{source[0]}:{source[1]}",
                }
    if len(heartbeats) != 2:
        raise RuntimeError(f"missing UART heartbeat(s): {sorted(set(sockets) - set(heartbeats))}")

    request_message = int(mavutil.mavlink.MAV_CMD_REQUEST_MESSAGE)
    control_frame = build_command(
        mavutil,
        request_message,
        [float(mavutil.mavlink.MAVLINK_MSG_ID_AUTOPILOT_VERSION), 0, 0, 0, 0, 0, 0],
    )
    set_interval = int(mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL)
    payload_frame = build_command(
        mavutil,
        set_interval,
        [float(mavutil.mavlink.MAVLINK_MSG_ID_ATTITUDE), 200000.0, 0, 0, 0, 0, 0],
    )
    control_sent_ns = time.monotonic_ns()
    sockets["control"].sendto(control_frame, endpoint(args.control_target))
    payload_sent_ns = time.monotonic_ns()
    sockets["payload"].sendto(payload_frame, endpoint(args.payload_target))

    control_ack: dict[str, Any] | None = None
    payload_ack: dict[str, Any] | None = None
    payload_telemetry: dict[str, Any] | None = None
    control_response: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        for channel, message, source in receive_mavlink(selector, parsers, deadline):
            counts[channel] += 1
            if message.get_srcSystem() != 1:
                continue
            message_type = message.get_type()
            now_ns = time.monotonic_ns()
            if channel == "control" and message_type == "COMMAND_ACK" \
                    and int(message.command) == request_message:
                control_ack = {
                    "command": int(message.command),
                    "result": int(message.result),
                    "system_id": message.get_srcSystem(),
                    "source": f"{source[0]}:{source[1]}",
                    "latency_ms": (now_ns - control_sent_ns) / 1e6,
                }
            elif channel == "payload" and message_type == "COMMAND_ACK" \
                    and int(message.command) == set_interval:
                payload_ack = {
                    "command": int(message.command),
                    "result": int(message.result),
                    "system_id": message.get_srcSystem(),
                    "source": f"{source[0]}:{source[1]}",
                    "latency_ms": (now_ns - payload_sent_ns) / 1e6,
                }
            elif channel == "payload" and message_type == "ATTITUDE":
                payload_telemetry = {
                    "message": message_type,
                    "system_id": message.get_srcSystem(),
                    "source": f"{source[0]}:{source[1]}",
                    "latency_ms": (now_ns - payload_sent_ns) / 1e6,
                }
            elif channel == "control" and message_type == "AUTOPILOT_VERSION":
                control_response = {
                    "message": message_type,
                    "system_id": message.get_srcSystem(),
                    "source": f"{source[0]}:{source[1]}",
                }
        if control_ack and payload_ack and payload_telemetry:
            break

    selector.close()
    for udp in sockets.values():
        udp.close()
    result = {
        "status": "healthy" if control_ack and payload_ack and payload_telemetry else "failed",
        "heartbeats": heartbeats,
        "message_counts": counts,
        "control": {
            "command": request_message,
            "frame_bytes": len(control_frame),
            "frame_hex": control_frame.hex(),
            "frame_sha256": sha256(control_frame),
            "ack": control_ack,
            "response": control_response,
        },
        "payload": {
            "command": set_interval,
            "frame_bytes": len(payload_frame),
            "frame_sha256": sha256(payload_frame),
            "ack": payload_ack,
            "telemetry": payload_telemetry,
        },
    }
    write_json(args.output, result)
    if result["status"] != "healthy":
        raise RuntimeError("real SITL did not return control ACK and payload telemetry")
    return 0


def run_down_probe(args: argparse.Namespace) -> int:
    os.environ.setdefault("MAVLINK20", "1")
    from pymavlink import mavutil

    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(endpoint(args.bind))
    udp.setblocking(False)
    command = int(mavutil.mavlink.MAV_CMD_REQUEST_MESSAGE)
    frame = build_command(
        mavutil,
        command,
        [float(mavutil.mavlink.MAVLINK_MSG_ID_AUTOPILOT_VERSION), 0, 0, 0, 0, 0, 0],
    )
    datagrams = (
        Encoder(
            channel=args.channel,
            uav_id=args.uav_id,
            direction="gcs_to_uart",
            max_payload=args.chunk_payload_bytes,
        ).encode(frame)
        if args.framed
        else [frame]
    )
    for datagram in datagrams:
        udp.sendto(datagram, endpoint(args.target))
    received = 0
    deadline = time.monotonic() + args.timeout_s
    while time.monotonic() < deadline:
        readable, _, _ = __import__("select").select([udp], [], [], 0.1)
        if readable:
            udp.recvfrom(65535)
            received += 1
    udp.close()
    result = {
        "command_frame_sha256": sha256(frame),
        "command_network_datagrams": len(datagrams),
        "transport_framing": "serial_chunk_v1" if args.framed else "raw_datagram",
        "received_datagrams": received,
        "exchange_stopped": received == 0,
        "new_command_response_stopped": received == 0,
        "reverse_telemetry_stopped": received == 0,
    }
    write_json(args.output, result)
    return 0 if received == 0 else 1


def run_data_sender(args: argparse.Namespace) -> int:
    code = PHASE_CODES[args.phase]
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind((args.source_ip, 0))
    if args.broadcast:
        udp.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    padding_size = max(0, args.packet_bytes - DATA_HEADER.size)
    for sequence in range(args.count):
        header = DATA_HEADER.pack(
            DATA_MAGIC,
            code,
            args.sender_id,
            sequence,
            time.monotonic_ns(),
        )
        udp.sendto(header + bytes([args.sender_id]) * padding_size, (args.destination, args.port))
        if args.interval_ms:
            time.sleep(args.interval_ms / 1000.0)
    udp.close()
    write_json(
        args.output,
        {
            "phase": args.phase,
            "sender_id": args.sender_id,
            "packets_sent": args.count,
            "packet_bytes": args.packet_bytes,
            "destination": f"{args.destination}:{args.port}",
        },
    )
    return 0


def run_data_receiver(args: argparse.Namespace) -> int:
    code = PHASE_CODES[args.phase]
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(("0.0.0.0", args.port))
    udp.setblocking(False)
    Path(args.ready_file).write_text("ready\n")
    unique: set[tuple[int, int]] = set()
    latencies: list[float] = []
    sources: dict[str, int] = {}
    invalid = 0
    deadline = time.monotonic() + args.duration_s
    while time.monotonic() < deadline:
        readable, _, _ = __import__("select").select([udp], [], [], 0.1)
        if not readable:
            continue
        data, _source = udp.recvfrom(65535)
        if len(data) < DATA_HEADER.size:
            invalid += 1
            continue
        magic, packet_phase, sender_id, sequence, sent_ns = DATA_HEADER.unpack_from(data)
        if magic != DATA_MAGIC or packet_phase != code:
            invalid += 1
            continue
        unique.add((sender_id, sequence))
        sources[str(sender_id)] = sources.get(str(sender_id), 0) + 1
        latencies.append((time.monotonic_ns() - sent_ns) / 1e6)
    udp.close()
    result = {
        "phase": args.phase,
        "receiver": args.receiver,
        "packets_received": len(unique),
        "source_counts": sources,
        "invalid_packets": invalid,
        "latency_ms": {
            "min": min(latencies) if latencies else None,
            "median": statistics.median(latencies) if latencies else None,
            "max": max(latencies) if latencies else None,
        },
    }
    write_json(args.output, result)
    return 0


def netns_command(namespace: str, *arguments: str) -> list[str]:
    return ["ip", "netns", "exec", namespace, sys.executable, "-u", str(Path(__file__).resolve()), *arguments]


def wait_ready(paths: list[Path], timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if all(path.is_file() for path in paths):
            return
        time.sleep(0.05)
    raise RuntimeError(f"data receiver readiness timed out: {[str(path) for path in paths]}")


def run_data_phase(
    run_dir: Path,
    phase: str,
    receivers: list[tuple[str, str]],
    senders: list[tuple[str, int, str, bool, int, int]],
    duration_s: float,
) -> dict[str, Any]:
    processes: list[subprocess.Popen[bytes]] = []
    ready_files: list[Path] = []
    receiver_outputs: list[Path] = []
    for namespace, receiver in receivers:
        output = run_dir / "metrics" / f"data_{phase}_{receiver}.json"
        ready = run_dir / "logs" / f"data_{phase}_{receiver}.ready"
        ready.unlink(missing_ok=True)
        ready_files.append(ready)
        receiver_outputs.append(output)
        processes.append(
            subprocess.Popen(
                netns_command(
                    namespace,
                    "data-receiver",
                    "--phase", phase,
                    "--receiver", receiver,
                    "--port", "14800",
                    "--duration-s", str(duration_s),
                    "--ready-file", str(ready),
                    "--output", str(output),
                ),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        )
    wait_ready(ready_files)
    sender_processes: list[subprocess.Popen[bytes]] = []
    for namespace, sender_id, destination, broadcast, count, packet_bytes in senders:
        output = run_dir / "metrics" / f"data_{phase}_sender{sender_id}.json"
        command = netns_command(
            namespace,
            "data-sender",
            "--phase", phase,
            "--sender-id", str(sender_id),
            "--source-ip", f"10.71.1.{9 + sender_id}",
            "--destination", destination,
            "--port", "14800",
            "--count", str(count),
            "--packet-bytes", str(packet_bytes),
            "--output", str(output),
        )
        if broadcast:
            command.append("--broadcast")
        sender_processes.append(subprocess.Popen(command))
    for process in sender_processes:
        if process.wait(timeout=10) != 0:
            raise RuntimeError(f"{phase} data sender failed")
    for process in processes:
        try:
            status = process.wait(timeout=duration_s + 3)
        except subprocess.TimeoutExpired:
            process.terminate()
            raise RuntimeError(f"{phase} data receiver timed out")
        if status != 0:
            stderr = (process.stderr.read() if process.stderr else b"").decode(errors="replace")
            raise RuntimeError(f"{phase} data receiver failed: {stderr}")
    return {
        "receivers": [json.loads(path.read_text()) for path in receiver_outputs],
        "senders": [
            json.loads((run_dir / "metrics" / f"data_{phase}_sender{sender_id}.json").read_text())
            for _namespace, sender_id, _destination, _broadcast, _count, _bytes in senders
        ],
    }


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def matching_uart_event(path: Path, digest: str, size: int) -> bool:
    if not path.is_file():
        return False
    for line in path.read_text().splitlines():
        event = json.loads(line)
        if event.get("direction") == "udp_to_uart" \
                and event.get("sha256") == digest and event.get("bytes") == size:
            return True
    return False


def find_sitl(control_tty: str, payload_tty: str) -> dict[str, Any] | None:
    for command_file in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            raw = command_file.read_bytes()
        except (OSError, PermissionError):
            continue
        command = raw.replace(b"\0", b" ").decode(errors="replace")
        if "arducopter" in command and control_tty in command and payload_tty in command:
            return {"pid": int(command_file.parent.name), "command": command.strip()}
    return None


def write_summary_csv(path: Path, summary: dict[str, Any]) -> None:
    rows = [
        ("control_ack_latency_ms", summary.get("control_ack_latency_ms")),
        ("mavlink_packet_count", summary.get("mavlink_packet_count")),
        ("additional_data_packets_sent", summary.get("additional_data_packets_sent")),
        ("additional_data_packets_received", summary.get("additional_data_packets_received")),
        ("additional_data_drop_count", summary.get("additional_data_drop_count")),
        ("ns3_backoff_events", summary.get("ns3_backoff_events")),
        ("ns3_drop_events", summary.get("ns3_drop_events")),
        ("pcap_count", summary.get("pcap_count")),
    ]
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.writer(output)
        writer.writerow(["metric", "value"])
        writer.writerows(rows)


def run_vertical(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    metrics = run_dir / "metrics"
    errors: list[str] = []
    mavlink_path = metrics / "mavlink.json"
    down_path = metrics / "ns3_stopped.json"
    data: dict[str, Any] = {}
    try:
        subprocess.run(
            netns_command(
                args.gcs_namespace,
                "mavlink-probe",
                "--control-bind", "10.71.0.10:14600",
                "--control-target", "10.71.1.10:14601",
                "--payload-bind", "10.71.0.10:14700",
                "--payload-target", "10.71.1.10:14701",
                "--timeout-s", str(args.mavlink_timeout_s),
                "--output", str(mavlink_path),
            ),
            check=True,
            timeout=args.mavlink_timeout_s + 5,
        )
        data["p2p"] = run_data_phase(
            run_dir,
            "p2p",
            [(args.uav2_namespace, "uav2")],
            [(args.uav1_namespace, 1, "10.71.1.11", False, 12, 256)],
            3.0,
        )
        data["p2mp"] = run_data_phase(
            run_dir,
            "p2mp",
            [(args.uav2_namespace, "uav2"), (args.uav3_namespace, "uav3")],
            [(args.uav1_namespace, 1, "10.71.1.255", True, 12, 256)],
            3.0,
        )
        data["contention"] = run_data_phase(
            run_dir,
            "contention",
            [(args.uav1_namespace, "uav1")],
            [
                (args.uav2_namespace, 2, "10.71.1.10", False, 80, 512),
                (args.uav3_namespace, 3, "10.71.1.10", False, 80, 512),
            ],
            10.0,
        )
    except (RuntimeError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        errors.append(str(exc))
    finally:
        Path(args.ns3_stop_file).touch()

    stats_path = Path(args.ns3_stats_file)
    stats_deadline = time.monotonic() + 10
    while time.monotonic() < stats_deadline and not stats_path.is_file():
        time.sleep(0.1)
    if not stats_path.is_file():
        errors.append("ns-3 did not write its shutdown counters")
    try:
        subprocess.run(
            netns_command(
                args.gcs_namespace,
                "down-probe",
                "--bind", "10.71.0.10:14600",
                "--target", "10.71.1.10:14601",
                "--timeout-s", "2.0",
                "--output", str(down_path),
            ),
            check=True,
            timeout=5,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        errors.append(f"traffic still crossed the stopped ns-3 path: {exc}")

    mavlink = load_json(mavlink_path) if mavlink_path.is_file() else {}
    ns3 = load_json(stats_path) if stats_path.is_file() else {}
    stopped = load_json(down_path) if down_path.is_file() else {}
    control = mavlink.get("control", {})
    command_matches = matching_uart_event(
        Path(args.control_event_log),
        str(control.get("frame_sha256", "")),
        int(control.get("frame_bytes", -1)),
    )
    if not command_matches:
        errors.append("control MAVLink frame does not match UART adapter input")
    sitl = find_sitl(args.control_tty, args.payload_tty)
    if sitl is None:
        errors.append("real arducopter process does not own both UART paths")

    p2p_received = sum(
        receiver["packets_received"] for receiver in data.get("p2p", {}).get("receivers", [])
    )
    p2mp_receivers = data.get("p2mp", {}).get("receivers", [])
    p2mp_received = sum(receiver["packets_received"] for receiver in p2mp_receivers)
    contention_received = sum(
        receiver["packets_received"]
        for receiver in data.get("contention", {}).get("receivers", [])
    )
    sent = 12 + 12 * 2 + 160
    received = p2p_received + p2mp_received + contention_received
    if p2p_received == 0:
        errors.append("point-to-point data channel delivered no packets")
    if len(p2mp_receivers) != 2 or any(item["packets_received"] == 0 for item in p2mp_receivers):
        errors.append("point-to-multipoint data did not reach both receivers")
    if int(ns3.get("backoff_events", 0)) <= 0:
        errors.append("shared-medium contention produced no ns-3 backoff event")
    if not stopped.get("exchange_stopped", False):
        errors.append("exchange did not stop with ns-3")

    pcaps = sorted((run_dir / "pcap").glob("*.pcap"))
    nonempty_pcaps = [path for path in pcaps if path.stat().st_size > 24]
    if not nonempty_pcaps:
        errors.append("ns-3 produced no non-empty PCAP")
    summary = {
        "status": "healthy" if not errors else "failed",
        "errors": errors,
        "actual_sitl": sitl,
        "system_id": mavlink.get("heartbeats", {}).get("control", {}).get("system_id"),
        "control_uart": {"ardupilot_serial": "SERIAL1", "tty": args.control_tty},
        "payload_uart": {"ardupilot_serial": "SERIAL2", "tty": args.payload_tty},
        "command_frame_sha256": control.get("frame_sha256"),
        "command_payload_matches_uart": command_matches,
        "control_ack": control.get("ack"),
        "control_ack_latency_ms": (control.get("ack") or {}).get("latency_ms"),
        "payload_ack": mavlink.get("payload", {}).get("ack"),
        "payload_telemetry": mavlink.get("payload", {}).get("telemetry"),
        "mavlink_packet_count": sum(mavlink.get("message_counts", {}).values()),
        "additional_data": data,
        "additional_data_packets_sent": sent,
        "additional_data_packets_received": received,
        "additional_data_drop_count": max(0, sent - received),
        "ns3_backoff_events": ns3.get("backoff_events"),
        "ns3_drop_events": ns3.get("drop_events"),
        "ns3_devices": ns3.get("radio_devices", []),
        "ns3_stop_breaks_exchange": stopped.get("exchange_stopped", False),
        "pcaps": [str(path) for path in nonempty_pcaps],
        "pcap_count": len(nonempty_pcaps),
        "direct_localhost_shortcut": False,
    }
    summary_path = metrics / "communication_summary.json"
    write_json(summary_path, summary)
    write_summary_csv(metrics / "communication_summary.csv", summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["status"] == "healthy" else 1


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    commands = root.add_subparsers(dest="command", required=True)

    adapter = commands.add_parser("uart-adapter")
    adapter.add_argument("--channel", required=True)
    adapter.add_argument("--tty", required=True)
    adapter.add_argument("--bind", required=True)
    adapter.add_argument("--peer", required=True)
    adapter.add_argument("--event-log", required=True)
    adapter.add_argument("--ready-file", required=True)
    adapter.add_argument("--tos", type=int)
    adapter.add_argument("--uav-id", type=int, default=1)
    adapter.add_argument("--baud-rate", type=int, default=115200)
    adapter.add_argument("--framed", action="store_true")
    adapter.add_argument("--chunk-payload-bytes", type=int, default=192)
    adapter.add_argument("--reassembly-timeout-ms", type=int, default=500)
    adapter.add_argument("--metrics-period-ms", type=int, default=1000)
    adapter.add_argument("--metrics-output")
    adapter.add_argument("--event-logging", choices=("metrics_only", "batched_trace"), default="batched_trace")
    adapter.set_defaults(function=run_uart_adapter)

    mavlink = commands.add_parser("mavlink-probe")
    mavlink.add_argument("--control-bind", required=True)
    mavlink.add_argument("--control-target", required=True)
    mavlink.add_argument("--payload-bind", required=True)
    mavlink.add_argument("--payload-target", required=True)
    mavlink.add_argument("--timeout-s", type=float, default=60)
    mavlink.add_argument("--output", required=True)
    mavlink.set_defaults(function=run_mavlink_probe)

    down = commands.add_parser("down-probe")
    down.add_argument("--bind", required=True)
    down.add_argument("--target", required=True)
    down.add_argument("--timeout-s", type=float, default=2)
    down.add_argument("--output", required=True)
    down.add_argument("--framed", action="store_true")
    down.add_argument("--channel", choices=("control", "payload"), default="control")
    down.add_argument("--uav-id", type=int, default=1)
    down.add_argument("--chunk-payload-bytes", type=int, default=192)
    down.set_defaults(function=run_down_probe)

    sender = commands.add_parser("data-sender")
    sender.add_argument("--phase", choices=sorted(PHASE_CODES), required=True)
    sender.add_argument("--sender-id", type=int, required=True)
    sender.add_argument("--source-ip", required=True)
    sender.add_argument("--destination", required=True)
    sender.add_argument("--port", type=int, required=True)
    sender.add_argument("--count", type=int, required=True)
    sender.add_argument("--packet-bytes", type=int, required=True)
    sender.add_argument("--interval-ms", type=float, default=0)
    sender.add_argument("--broadcast", action="store_true")
    sender.add_argument("--output", required=True)
    sender.set_defaults(function=run_data_sender)

    receiver = commands.add_parser("data-receiver")
    receiver.add_argument("--phase", choices=sorted(PHASE_CODES), required=True)
    receiver.add_argument("--receiver", required=True)
    receiver.add_argument("--port", type=int, required=True)
    receiver.add_argument("--duration-s", type=float, required=True)
    receiver.add_argument("--ready-file", required=True)
    receiver.add_argument("--output", required=True)
    receiver.set_defaults(function=run_data_receiver)

    vertical = commands.add_parser("run")
    vertical.add_argument("--run-dir", required=True)
    vertical.add_argument("--gcs-namespace", default="ams-gcs")
    vertical.add_argument("--uav1-namespace", default="ams-uav1")
    vertical.add_argument("--uav2-namespace", default="ams-uav2")
    vertical.add_argument("--uav3-namespace", default="ams-uav3")
    vertical.add_argument("--ns3-stop-file", required=True)
    vertical.add_argument("--ns3-stats-file", required=True)
    vertical.add_argument("--control-event-log", required=True)
    vertical.add_argument("--control-tty", required=True)
    vertical.add_argument("--payload-tty", required=True)
    vertical.add_argument("--mavlink-timeout-s", type=float, default=90)
    vertical.set_defaults(function=run_vertical)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        return int(args.function(args))
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
