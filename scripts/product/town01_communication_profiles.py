#!/usr/bin/env python3
"""Run nominal/contention/overload traffic through the live Town01 ns-3 medium."""

from __future__ import annotations

import argparse
import binascii
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
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from network.scripts.communication_qos import (  # noqa: E402
    CLASS_NAMES,
    PROFILE_NAMES,
    load_qos,
)


MAGIC = b"BQO1"
VERSION = 1
HEADER = struct.Struct("!4sBBBBIQHI")
PROFILE_IDS = {name: index + 1 for index, name in enumerate(PROFILE_NAMES)}
CLASS_IDS = {name: index + 1 for index, name in enumerate(CLASS_NAMES)}
PROFILE_BY_ID = {value: key for key, value in PROFILE_IDS.items()}
CLASS_BY_ID = {value: key for key, value in CLASS_IDS.items()}
PORTS = {"control": 14600, "payload": 14700, "additional_data": 14800}
GCS_IP = "10.71.0.10"
UAV_IDS = tuple(range(1, 6))


class ProfileError(RuntimeError):
    """A live profile process could not complete."""


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


def packet_id(profile: str, traffic_class: str, uav_id: int, sequence: int) -> str:
    return f"{profile}:{traffic_class}:uav{uav_id}:{sequence}"


def encode_packet(
    profile: str,
    traffic_class: str,
    uav_id: int,
    sequence: int,
    packet_bytes: int,
    sent_ns: int,
) -> bytes:
    if packet_bytes < HEADER.size:
        raise ValueError("profile packet is smaller than its header")
    payload_length = packet_bytes - HEADER.size
    payload = bytes((uav_id, CLASS_IDS[traffic_class])) * ((payload_length + 1) // 2)
    payload = payload[:payload_length]
    checksum = binascii.crc32(payload) & 0xFFFFFFFF
    return HEADER.pack(
        MAGIC,
        VERSION,
        PROFILE_IDS[profile],
        CLASS_IDS[traffic_class],
        uav_id,
        sequence,
        sent_ns,
        payload_length,
        checksum,
    ) + payload


def decode_packet(data: bytes) -> dict[str, Any]:
    if len(data) < HEADER.size:
        raise ValueError("short profile packet")
    magic, version, profile_id, class_id, uav_id, sequence, sent_ns, length, checksum = (
        HEADER.unpack_from(data)
    )
    payload = data[HEADER.size:]
    if magic != MAGIC or version != VERSION:
        raise ValueError("profile magic/version mismatch")
    if profile_id not in PROFILE_BY_ID or class_id not in CLASS_BY_ID or uav_id not in UAV_IDS:
        raise ValueError("profile/class/UAV identifier mismatch")
    if len(payload) != length or (binascii.crc32(payload) & 0xFFFFFFFF) != checksum:
        raise ValueError("profile payload length/checksum mismatch")
    profile = PROFILE_BY_ID[profile_id]
    traffic_class = CLASS_BY_ID[class_id]
    return {
        "profile": profile,
        "traffic_class": traffic_class,
        "uav": f"uav{uav_id}",
        "uav_id": uav_id,
        "sequence": sequence,
        "sent_monotonic_ns": sent_ns,
        "packet_id": packet_id(profile, traffic_class, uav_id, sequence),
        "transport_payload_sha256": hashlib.sha256(data).hexdigest(),
    }


def run_sender(args: argparse.Namespace) -> int:
    qos = load_qos(Path(args.qos))
    profile = qos["profiles"][args.profile]
    traffic = profile[args.traffic_class]
    rate = int(traffic["packets_per_second_per_uav"])
    count = rate * int(profile["duration_s"])
    packet_bytes = int(traffic["packet_bytes"])
    tos = int(qos["classes"][args.traffic_class]["tos"])
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, tos)
    sock.bind((f"10.71.{args.uav_id}.10", 0))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    interval_ns = 1_000_000_000 / rate
    start_ns = int(args.start_ns)
    while time.monotonic_ns() < start_ns:
        remaining = start_ns - time.monotonic_ns()
        time.sleep(min(0.002, max(0.0, remaining / 1e9)))
    with output.open("w", encoding="utf-8") as stream:
        for sequence in range(count):
            target_ns = start_ns + int(sequence * interval_ns)
            while time.monotonic_ns() < target_ns:
                remaining = target_ns - time.monotonic_ns()
                time.sleep(min(0.001, max(0.0, remaining / 1e9)))
            sent_ns = time.monotonic_ns()
            datagram = encode_packet(
                args.profile,
                args.traffic_class,
                args.uav_id,
                sequence,
                packet_bytes,
                sent_ns,
            )
            sock.sendto(datagram, (GCS_IP, PORTS[args.traffic_class]))
            record = {
                "event": "attempt",
                "profile": args.profile,
                "traffic_class": args.traffic_class,
                "uav": f"uav{args.uav_id}",
                "direction": "uplink",
                "sequence": sequence,
                "packet_id": packet_id(
                    args.profile, args.traffic_class, args.uav_id, sequence
                ),
                "attempted_monotonic_ns": sent_ns,
                "packet_bytes": len(datagram),
                "fragment_hashes": [hashlib.sha256(datagram).hexdigest()],
            }
            stream.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
        stream.flush()
    sock.close()
    return 0


def run_receiver(args: argparse.Namespace) -> int:
    selector = selectors.DefaultSelector()
    sockets: list[socket.socket] = []
    for traffic_class in CLASS_NAMES:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((GCS_IP, PORTS[traffic_class]))
        sock.setblocking(False)
        selector.register(sock, selectors.EVENT_READ, traffic_class)
        sockets.append(sock)
    write_json(Path(args.ready), {"status": "ready", "pid": os.getpid(), "profile": args.profile})
    deadline_ns = int(args.end_ns)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as stream:
        while time.monotonic_ns() < deadline_ns:
            remaining_s = max(0.0, (deadline_ns - time.monotonic_ns()) / 1e9)
            for key, _mask in selector.select(min(0.1, remaining_s)):
                data, source = key.fileobj.recvfrom(65535)
                received_ns = time.monotonic_ns()
                # The real dual-UART adapters continue returning MAVLink
                # telemetry to the same class ports between diagnostics and
                # load profiles.  It is valid background traffic, not a
                # malformed BQO1 logical packet.
                if not data.startswith(MAGIC):
                    stream.write(
                        json.dumps(
                            {
                                "event": "background_delivery",
                                "profile": args.profile,
                                "traffic_class": str(key.data),
                                "background_serial_datagram": True,
                                "bytes": len(data),
                                "transport_payload_sha256": hashlib.sha256(data).hexdigest(),
                                "source": f"{source[0]}:{source[1]}",
                                "received_monotonic_ns": received_ns,
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
                    continue
                try:
                    record = decode_packet(data)
                    if record["profile"] != args.profile or record["traffic_class"] != key.data:
                        raise ValueError("packet arrived on the wrong profile/class socket")
                    record.update(
                        {
                            "event": "delivery",
                            "direction": "uplink",
                            "received_monotonic_ns": received_ns,
                            "latency_ms": max(
                                0.0, (received_ns - int(record["sent_monotonic_ns"])) / 1e6
                            ),
                            "source": f"{source[0]}:{source[1]}",
                        }
                    )
                except ValueError as exc:
                    record = {
                        "event": "delivery",
                        "profile": args.profile,
                        "traffic_class": str(key.data),
                        "malformed": True,
                        "error": str(exc),
                        "received_monotonic_ns": received_ns,
                    }
                stream.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
            stream.flush()
    selector.close()
    for sock in sockets:
        sock.close()
    return 0


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    result: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            result.append(value)
    return result


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def profile_snapshot(
    profile: str, attempts: list[dict[str, Any]], deliveries: list[dict[str, Any]]
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for traffic_class in CLASS_NAMES:
        class_attempts = [item for item in attempts if item["traffic_class"] == traffic_class]
        class_deliveries = [
            item
            for item in deliveries
            if not item.get("malformed") and item.get("traffic_class") == traffic_class
        ]
        unique = {item["packet_id"] for item in class_deliveries}
        latencies = [float(item["latency_ms"]) for item in class_deliveries]
        per_uav = {
            f"uav{uav_id}": len(
                {
                    item["packet_id"]
                    for item in class_deliveries
                    if item.get("uav_id") == uav_id
                }
            )
            for uav_id in UAV_IDS
        }
        result[traffic_class] = {
            "packets_attempted": len(class_attempts),
            "packets_delivered_unique": len(unique),
            "pdr": len(unique) / len(class_attempts) if class_attempts else 0.0,
            "latency_ms": {
                "average": statistics.fmean(latencies) if latencies else None,
                "p50": percentile(latencies, 0.50),
                "p95": percentile(latencies, 0.95),
                "p99": percentile(latencies, 0.99),
            },
            "per_uav_delivered_unique": per_uav,
        }
    return {"profile": profile, "classes": result}


def netns_command(namespace: str, *arguments: str) -> list[str]:
    return ["ip", "netns", "exec", namespace, sys.executable, "-u", str(Path(__file__).resolve()), *arguments]


def wait_ready(path: Path, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.is_file() and path.stat().st_size:
            return
        time.sleep(0.05)
    raise ProfileError(f"receiver did not become ready: {path}")


def run_profiles(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    qos_path = Path(args.qos).resolve()
    qos = load_qos(qos_path)
    attempts_all = run_dir / "logs/communication_attempts.jsonl"
    deliveries_all = run_dir / "logs/communication_deliveries.jsonl"
    windows = run_dir / "logs/profile_windows.jsonl"
    for path in (attempts_all, deliveries_all, windows):
        path.unlink(missing_ok=True)
    combined: dict[str, Any] = {}
    for profile_name in PROFILE_NAMES:
        profile = qos["profiles"][profile_name]
        duration_s = int(profile["duration_s"])
        profile_dir = run_dir / "logs" / "profiles" / profile_name
        profile_dir.mkdir(parents=True, exist_ok=True)
        ready = profile_dir / "receiver.ready"
        deliveries_path = profile_dir / "deliveries.jsonl"
        ready.unlink(missing_ok=True)
        deliveries_path.unlink(missing_ok=True)
        start_ns = time.monotonic_ns() + 2_000_000_000
        end_ns = start_ns + (duration_s + 3) * 1_000_000_000
        receiver = subprocess.Popen(
            netns_command(
                "ams-gcs",
                "receiver",
                "--profile",
                profile_name,
                "--end-ns",
                str(end_ns),
                "--output",
                str(deliveries_path),
                "--ready",
                str(ready),
            ),
            stdout=subprocess.DEVNULL,
            stderr=(profile_dir / "receiver.log").open("wb"),
        )
        wait_ready(ready)
        senders: list[tuple[subprocess.Popen[bytes], Path, Any]] = []
        for uav_id in UAV_IDS:
            for traffic_class in CLASS_NAMES:
                attempt_path = profile_dir / f"attempts_{traffic_class}_uav{uav_id}.jsonl"
                log_handle = (profile_dir / f"sender_{traffic_class}_uav{uav_id}.log").open("wb")
                process = subprocess.Popen(
                    netns_command(
                        f"ams-uav{uav_id}",
                        "sender",
                        "--profile",
                        profile_name,
                        "--traffic-class",
                        traffic_class,
                        "--uav-id",
                        str(uav_id),
                        "--start-ns",
                        str(start_ns),
                        "--qos",
                        str(qos_path),
                        "--output",
                        str(attempt_path),
                    ),
                    stdout=subprocess.DEVNULL,
                    stderr=log_handle,
                )
                senders.append((process, attempt_path, log_handle))
        append_jsonl(
            windows,
            {
                "event": "profile_start",
                "profile": profile_name,
                "scheduled_start_monotonic_ns": start_ns,
                "duration_s": duration_s,
                "offered_load_bps": sum(
                    int(profile[name]["packets_per_second_per_uav"])
                    * int(profile[name]["packet_bytes"])
                    * 8
                    * len(UAV_IDS)
                    for name in CLASS_NAMES
                ),
            },
        )
        failures: list[str] = []
        for process, attempt_path, log_handle in senders:
            status = process.wait(timeout=duration_s + 20)
            log_handle.close()
            if status:
                failures.append(f"{attempt_path.name}:exit={status}")
        receiver_status = receiver.wait(timeout=duration_s + 20)
        if receiver.stderr:
            receiver.stderr.close()
        if receiver_status:
            failures.append(f"receiver:exit={receiver_status}")
        if failures:
            raise ProfileError(f"profile {profile_name} failed: {failures}")
        attempts = [item for _process, path, _log in senders for item in read_jsonl(path)]
        deliveries = read_jsonl(deliveries_path)
        with attempts_all.open("a", encoding="utf-8") as output:
            for item in attempts:
                output.write(json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n")
        with deliveries_all.open("a", encoding="utf-8") as output:
            for item in deliveries:
                output.write(json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n")
        snapshot = profile_snapshot(profile_name, attempts, deliveries)
        write_json(run_dir / "metrics" / f"profile_{profile_name}.json", snapshot)
        combined[profile_name] = snapshot
        append_jsonl(
            windows,
            {
                "event": "profile_end",
                "profile": profile_name,
                "monotonic_ns": time.monotonic_ns(),
            },
        )
        time.sleep(2.0)
    write_json(run_dir / "metrics/traffic_profiles.json", combined)
    print(json.dumps(combined, sort_keys=True))
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    sender = commands.add_parser("sender")
    sender.add_argument("--profile", choices=PROFILE_NAMES, required=True)
    sender.add_argument("--traffic-class", choices=CLASS_NAMES, required=True)
    sender.add_argument("--uav-id", type=int, choices=UAV_IDS, required=True)
    sender.add_argument("--start-ns", type=int, required=True)
    sender.add_argument("--qos", default=str(ROOT / "network/config/communication_qos.yaml"))
    sender.add_argument("--output", required=True)
    sender.set_defaults(function=run_sender)
    receiver = commands.add_parser("receiver")
    receiver.add_argument("--profile", choices=PROFILE_NAMES, required=True)
    receiver.add_argument("--end-ns", type=int, required=True)
    receiver.add_argument("--output", required=True)
    receiver.add_argument("--ready", required=True)
    receiver.set_defaults(function=run_receiver)
    run = commands.add_parser("run")
    run.add_argument("--run-dir", required=True)
    run.add_argument("--qos", default=str(ROOT / "network/config/communication_qos.yaml"))
    run.set_defaults(function=run_profiles)
    return root


def main() -> int:
    try:
        args = parser().parse_args()
        return int(args.function(args))
    except (OSError, ProfileError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"FAIL communication profiles: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
