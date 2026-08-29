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
from collections import Counter, defaultdict
from collections.abc import Iterator
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from network.ns3.tap_packet_engine_config import (  # noqa: E402
    CONTRACT as ENGINE_CONTRACT,
)
from network.ns3.tap_packet_engine_stock_config import (  # noqa: E402
    CONTRACT as STOCK_ENGINE_CONTRACT,
)
from network.scripts.communication_qos import (  # noqa: E402
    CLASS_NAMES,
    PROFILE_NAMES,
    load_qos,
)
from network.scripts.packet_accounting import terminal_packet_outcomes  # noqa: E402


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


def available_datagrams_until(
    ready_socket: socket.socket, deadline_ns: int
) -> Iterator[tuple[bytes, tuple[str, int], int]]:
    """Yield currently available datagrams without running past a hard deadline."""

    while time.monotonic_ns() < deadline_ns:
        try:
            data, source = ready_socket.recvfrom(65535)
        except BlockingIOError:
            return
        received_ns = time.monotonic_ns()
        if received_ns > deadline_ns:
            return
        yield data, source, received_ns


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
    offered_end_ns = int(args.end_ns)
    if offered_end_ns <= start_ns:
        raise ValueError("sender end must be after its start")
    while time.monotonic_ns() < start_ns:
        remaining = min(start_ns, offered_end_ns) - time.monotonic_ns()
        time.sleep(min(0.002, max(0.0, remaining / 1e9)))
    with output.open("w", encoding="utf-8") as stream:
        for sequence in range(count):
            target_ns = start_ns + int(sequence * interval_ns)
            if target_ns >= offered_end_ns:
                break
            while time.monotonic_ns() < min(target_ns, offered_end_ns):
                remaining = min(target_ns, offered_end_ns) - time.monotonic_ns()
                time.sleep(min(0.001, max(0.0, remaining / 1e9)))
            sent_ns = time.monotonic_ns()
            if sent_ns >= offered_end_ns:
                break
            datagram = encode_packet(
                args.profile,
                args.traffic_class,
                args.uav_id,
                sequence,
                packet_bytes,
                sent_ns,
            )
            if time.monotonic_ns() >= offered_end_ns:
                break
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
    try:
        for traffic_class in CLASS_NAMES:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind((GCS_IP, PORTS[traffic_class]))
            sock.setblocking(False)
            selector.register(sock, selectors.EVENT_READ, traffic_class)
            sockets.append(sock)
        write_json(
            Path(args.ready),
            {"status": "ready", "pid": os.getpid(), "profile": args.profile},
        )
        deadline_ns = int(args.end_ns)
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", encoding="utf-8") as stream:

            def drain_ready_socket(
                ready_socket: socket.socket,
                traffic_class: str,
                drain_deadline_ns: int,
            ) -> int:
                drained = 0
                for data, source, received_ns in available_datagrams_until(
                    ready_socket, drain_deadline_ns
                ):
                    drained += 1
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
                                    "traffic_class": traffic_class,
                                    "background_serial_datagram": True,
                                    "bytes": len(data),
                                    "transport_payload_sha256": hashlib.sha256(
                                        data
                                    ).hexdigest(),
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
                        if (
                            record["profile"] != args.profile
                            or record["traffic_class"] != traffic_class
                        ):
                            raise ValueError(
                                "packet arrived on the wrong profile/class socket"
                            )
                        record.update(
                            {
                                "event": "delivery",
                                "direction": "uplink",
                                "received_monotonic_ns": received_ns,
                                "latency_ms": max(
                                    0.0,
                                    (
                                        received_ns
                                        - int(record["sent_monotonic_ns"])
                                    )
                                    / 1e6,
                                ),
                                "source": f"{source[0]}:{source[1]}",
                            }
                        )
                    except ValueError as exc:
                        record = {
                            "event": "delivery",
                            "profile": args.profile,
                            "traffic_class": traffic_class,
                            "malformed": True,
                            "error": str(exc),
                            "received_monotonic_ns": received_ns,
                        }
                    stream.write(
                        json.dumps(record, sort_keys=True, separators=(",", ":"))
                        + "\n"
                    )
                return drained

            while time.monotonic_ns() < deadline_ns:
                remaining_s = max(
                    0.0, (deadline_ns - time.monotonic_ns()) / 1e9
                )
                for key, _mask in selector.select(min(0.1, remaining_s)):
                    if time.monotonic_ns() >= deadline_ns:
                        break
                    drain_ready_socket(key.fileobj, str(key.data), deadline_ns)
                stream.flush()
            stream.flush()
    finally:
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


def complete_jsonl_end_offset(path: Path) -> int:
    """Return the byte immediately after the last complete JSONL record."""

    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            cursor = size
            while cursor > 0:
                chunk_size = min(65536, cursor)
                cursor -= chunk_size
                stream.seek(cursor)
                chunk = stream.read(chunk_size)
                newline = chunk.rfind(b"\n")
                if newline >= 0:
                    return cursor + newline + 1
    except OSError:
        return 0
    return 0


def wait_for_event_log_flush(max_delay_ms: int, wait_intervals: int) -> None:
    """Wait the configured number of intervals before taking a durable snapshot."""

    if max_delay_ms <= 0 or wait_intervals <= 0:
        raise ValueError("event-log flush delay and wait intervals must be positive")
    time.sleep(wait_intervals * max_delay_ms / 1000.0)


def live_engine_command_line(pid: int) -> list[str]:
    """Return the command line for one live process or fail closed."""

    try:
        os.kill(pid, 0)
    except ProcessLookupError as exc:
        raise ProfileError(f"ns-3 ready PID {pid} is not alive") from exc
    except PermissionError:
        # Existence is established, but the command line below must still be
        # readable so a different live process cannot satisfy readiness.
        pass
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError as exc:
        raise ProfileError(f"cannot verify ns-3 ready PID {pid} command line") from exc
    command = [os.fsdecode(item) for item in raw.split(b"\0") if item]
    if not command:
        raise ProfileError(f"ns-3 ready PID {pid} has an empty command line")
    return command


def validate_engine_shaping_mode(
    run_dir: Path,
    qos_path: Path,
    qos: dict[str, Any],
    selected_profiles: tuple[str, ...],
    *,
    medium_access_mode: str = "centralized_priority_scheduler_over_csma_channel",
) -> bool:
    """Require one config-hashed engine shaping mode for the whole profile set."""

    stock = medium_access_mode == "stock_ns3_csma"
    if medium_access_mode not in {
        "stock_ns3_csma",
        "centralized_priority_scheduler_over_csma_channel",
    }:
        raise ProfileError("unknown medium access mode")
    expected = {False} if stock else {
        bool(qos["profiles"][profile]["shaping_enabled"])
        for profile in selected_profiles
    }
    if len(expected) != 1:
        raise ProfileError(
            "protected profiles and meltdown require separate ns-3 engine lifecycles"
        )
    report_path = run_dir / "logs/ns3_packet_engine_config.json"
    ready_path = run_dir / "logs/ns3_packet_engine.ready"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        resolved = report["resolved"]
        engine_mode = resolved[
            "ingress_shaping_enabled" if stock else "shaping_enabled"
        ]
        config_hash = report["config_sha256"]
        event_epoch = resolved["event_epoch"]
        uav_count = resolved["uav_count"]
        source_hashes = report["source_sha256"]
        engine_argv = report["engine_argv"]
        ready = json.loads(ready_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ProfileError(f"invalid ns-3 engine config report: {report_path}") from exc
    if not isinstance(report, dict) or not isinstance(ready, dict):
        raise ProfileError("ns-3 engine report/readiness must be JSON objects")
    expected_contract = STOCK_ENGINE_CONTRACT if stock else ENGINE_CONTRACT
    if report.get("contract") != expected_contract:
        raise ProfileError("ns-3 engine config report contract mismatch")
    if not isinstance(engine_mode, bool):
        raise ProfileError("ns-3 engine shaping mode is not a boolean")
    if not isinstance(config_hash, str) or len(config_hash) != 64:
        raise ProfileError("ns-3 engine config report hash is invalid")
    if (
        not isinstance(source_hashes, dict)
        or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in source_hashes.items()
        )
    ):
        raise ProfileError("ns-3 engine source hashes are invalid")
    resolved_qos_path = qos_path.resolve()
    reported_qos_hash = next(
        (
            digest
            for source, digest in source_hashes.items()
            if Path(source).resolve() == resolved_qos_path
        ),
        None,
    )
    current_qos_hash = hashlib.sha256(resolved_qos_path.read_bytes()).hexdigest()
    if reported_qos_hash != current_qos_hash:
        raise ProfileError("current QoS source SHA-256 differs from the engine report")
    if (
        not isinstance(event_epoch, int)
        or isinstance(event_epoch, bool)
        or event_epoch <= 0
    ):
        raise ProfileError("ns-3 engine event epoch is invalid")
    if (
        not isinstance(uav_count, int)
        or isinstance(uav_count, bool)
        or uav_count != len(UAV_IDS)
    ):
        raise ProfileError("ns-3 engine UAV count is invalid")
    if not isinstance(engine_argv, list) or not engine_argv or not all(
        isinstance(value, str) and value for value in engine_argv
    ):
        raise ProfileError("ns-3 engine argv report is invalid")
    if (
        ready.get("status") != "ready"
        or ready.get("contract") != expected_contract
        or ready.get("config_sha256") != config_hash
        or ready.get("event_epoch") != event_epoch
        or ready.get("uav_count") != uav_count
    ):
        raise ProfileError("live ns-3 readiness does not match the engine config report")
    ready_pid = ready.get("pid")
    if not isinstance(ready_pid, int) or isinstance(ready_pid, bool) or ready_pid <= 0:
        raise ProfileError("live ns-3 readiness PID is invalid")
    command_line = live_engine_command_line(ready_pid)
    expected_binary = "ams-tap-packet-engine-stock" if stock else "ams-tap-packet-engine"
    if expected_binary not in Path(command_line[0]).name:
        raise ProfileError("live readiness PID is not the ns-3 TAP packet engine")
    observed_argv = command_line[1 : len(engine_argv) + 1]
    if observed_argv != engine_argv:
        raise ProfileError(
            "live ns-3 process does not match the reported engine argv"
        )
    expected_mode = next(iter(expected))
    if engine_mode is not expected_mode:
        raise ProfileError(
            "selected profile shaping mode differs from the startup-authorized ns-3 mode"
        )
    return engine_mode


def selected_profile_names(
    requested: str | None,
    configured_names: tuple[str, ...],
    *,
    default_names: tuple[str, ...] | None = None,
) -> tuple[str, ...]:
    """Resolve a comma-separated subset without changing configured order."""

    if requested is None:
        selected = set(default_names if default_names is not None else configured_names)
        return tuple(name for name in configured_names if name in selected)
    names = [name.strip() for name in requested.split(",")]
    if not names or any(not name for name in names):
        raise ProfileError("--profiles must be a non-empty comma-separated list")
    duplicates = sorted(name for name, count in Counter(names).items() if count > 1)
    if duplicates:
        raise ProfileError(f"--profiles contains duplicates: {duplicates}")
    unknown = sorted(set(names) - set(configured_names))
    if unknown:
        raise ProfileError(f"--profiles contains unknown profiles: {unknown}")
    selected = set(names)
    return tuple(name for name in configured_names if name in selected)


def profile_packet_events(
    path: Path,
    fragment_hashes: set[str],
    packet_ids: set[str],
    *,
    start_offset: int = 0,
    end_offset: int | None = None,
    end_ns: int | None = None,
) -> tuple[list[dict[str, Any]], set[str], set[str]]:
    """Read only compact event fields needed for terminal/admission accounting.

    The packet engine may still have records in its userspace output buffer at
    the end of the bounded drain.  Complete records visible at that point are
    consumed; unresolved logical packets are finalized by terminal accounting.
    """

    events: list[dict[str, Any]] = []
    admit_hashes: set[str] = set()
    enqueue_hashes: set[str] = set()
    try:
        with path.open("rb") as stream:
            if start_offset > 0:
                stream.seek(max(0, start_offset - 1))
                previous = stream.read(1)
                stream.seek(start_offset)
                if previous != b"\n":
                    stream.readline()
            while True:
                if end_offset is None:
                    raw_line = stream.readline()
                else:
                    remaining = end_offset - stream.tell()
                    if remaining <= 0:
                        break
                    raw_line = stream.readline(remaining)
                    # Do not consume a JSON record that was only partially
                    # visible at the deterministic drain snapshot.
                    if raw_line and not raw_line.endswith(b"\n"):
                        break
                if not raw_line:
                    break
                try:
                    event = json.loads(raw_line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(event, dict):
                    continue
                observed_ns = event.get("host_monotonic_ns")
                if end_ns is not None and (
                    not isinstance(observed_ns, int)
                    or observed_ns <= 0
                    or observed_ns > end_ns
                ):
                    continue
                digest = event.get("transport_payload_sha256")
                explicit_packet_id = event.get("packet_id")
                matches_hash = isinstance(digest, str) and digest in fragment_hashes
                matches_packet_id = (
                    isinstance(explicit_packet_id, str) and explicit_packet_id in packet_ids
                )
                if not matches_hash and not matches_packet_id:
                    continue
                event_name = str(event.get("event") or "")
                compact = {
                    "event": event_name,
                    "packet_id": explicit_packet_id,
                    "transport_payload_sha256": digest,
                    "drop_reason": event.get("drop_reason"),
                    "reason": event.get("reason"),
                    "drop_stage": event.get("drop_stage"),
                    "terminal_status": event.get("terminal_status"),
                }
                events.append(compact)
                if isinstance(digest, str):
                    if event_name == "admit":
                        admit_hashes.add(digest)
                    elif event_name == "enqueue":
                        enqueue_hashes.add(digest)
    except OSError as exc:
        raise ProfileError(f"cannot read ns-3 packet events: {path}: {exc}") from exc
    return events, admit_hashes, enqueue_hashes


def _traffic_rates(
    attempts: list[dict[str, Any]], packet_ids: set[str], duration_s: int
) -> dict[str, int | float]:
    selected = [item for item in attempts if item.get("packet_id") in packet_ids]
    packets = len(selected)
    octets = sum(int(item.get("packet_bytes", 0) or 0) for item in selected)
    return {
        "packets": packets,
        "bytes": octets,
        "packets_per_second": packets / duration_s,
        "bits_per_second": octets * 8.0 / duration_s,
    }


def _per_uav_terminal_counts(
    attempts: list[dict[str, Any]],
    outcomes: dict[str, dict[str, Any]],
    admitted_ids: set[str],
    enqueued_ids: set[str],
) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for uav_id in UAV_IDS:
        label = f"uav{uav_id}"
        selected = [item for item in attempts if item.get("uav") == label]
        ids = {str(item["packet_id"]) for item in selected}
        statuses = Counter(str(outcomes[packet_id]["status"]) for packet_id in ids)
        result[label] = {
            "offered": len(ids),
            "admitted_policy_observed": len(ids & admitted_ids),
            "queue_enqueued_observed": len(ids & enqueued_ids),
            "delivered": statuses["delivered"],
            "dropped_at_ingress": statuses["dropped_at_ingress"],
            "dropped_in_medium": statuses["dropped_in_medium"],
            "expired_at_drain": statuses["expired_at_drain"],
            "pending": statuses["pending"],
        }
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
    profile: str,
    attempts: list[dict[str, Any]],
    deliveries: list[dict[str, Any]],
    outcomes: dict[str, dict[str, Any]],
    admitted_ids: set[str],
    enqueued_ids: set[str],
    *,
    duration_s: int,
    shaping_enabled: bool,
    gates_overall_status: bool,
    drain_interval_ms: int,
    terminal_expiry_after_drain: bool,
    observed_ns3_event_count: int,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for traffic_class in CLASS_NAMES:
        class_attempts = [item for item in attempts if item["traffic_class"] == traffic_class]
        class_ids = {str(item["packet_id"]) for item in class_attempts}
        class_admitted = class_ids & admitted_ids
        class_enqueued = class_ids & enqueued_ids
        class_statuses = Counter(
            str(outcomes[packet_id]["status"]) for packet_id in class_ids
        )
        class_deliveries = [
            item
            for item in deliveries
            if item.get("event") == "delivery"
            and not item.get("malformed")
            and item.get("traffic_class") == traffic_class
            and item.get("packet_id") in class_ids
        ]
        unique = {item["packet_id"] for item in class_deliveries}
        latency_by_packet: dict[str, float] = {}
        for item in class_deliveries:
            packet_id_value = str(item["packet_id"])
            if packet_id_value not in latency_by_packet and isinstance(
                item.get("latency_ms"), (int, float)
            ):
                latency_by_packet[packet_id_value] = float(item["latency_ms"])
        latencies = list(latency_by_packet.values())
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
        per_uav_terminal = _per_uav_terminal_counts(
            class_attempts, outcomes, class_admitted, class_enqueued
        )
        result[traffic_class] = {
            "packets_attempted": len(class_attempts),
            "packets_delivered_unique": len(unique),
            "pdr": len(unique) / len(class_attempts) if class_attempts else 0.0,
            "offered": _traffic_rates(class_attempts, class_ids, duration_s),
            "admitted_policy_observed": _traffic_rates(
                class_attempts, class_admitted, duration_s
            ),
            "queue_enqueued_observed": _traffic_rates(
                class_attempts, class_enqueued, duration_s
            ),
            "dropped_at_ingress": class_statuses["dropped_at_ingress"],
            "dropped_in_medium": class_statuses["dropped_in_medium"],
            "expired_at_drain": class_statuses["expired_at_drain"],
            "packets_pending": class_statuses["pending"],
            "terminal_status_counts": dict(sorted(class_statuses.items())),
            "latency_ms": {
                "average": statistics.fmean(latencies) if latencies else None,
                "p50": percentile(latencies, 0.50),
                "p95": percentile(latencies, 0.95),
                "p99": percentile(latencies, 0.99),
            },
            "per_uav_delivered_unique": per_uav,
            "per_uav_terminal_counts": per_uav_terminal,
            "fairness_ready": {
                "delivered_by_uav": per_uav,
                "admitted_by_uav": {
                    uav: values["admitted_policy_observed"]
                    for uav, values in per_uav_terminal.items()
                },
                "all_uavs_delivered": all(value > 0 for value in per_uav.values()),
            },
        }
    all_ids = {str(item["packet_id"]) for item in attempts}
    statuses = Counter(str(outcomes[packet_id]["status"]) for packet_id in all_ids)
    terminal_count = sum(
        1 for packet_id in all_ids if bool(outcomes[packet_id].get("terminal"))
    )
    return {
        "profile": profile,
        "shaping_enabled": shaping_enabled,
        "gates_overall_status": gates_overall_status,
        "drain_interval_ms": drain_interval_ms,
        "terminal_expiry_after_drain": terminal_expiry_after_drain,
        "admission_basis": "unique_logical_packets_with_ns3_admit_observed",
        "observed_ns3_event_count": observed_ns3_event_count,
        "packets_pending": statuses["pending"],
        "terminal_status_counts": dict(sorted(statuses.items())),
        "offered": _traffic_rates(attempts, all_ids, duration_s),
        "admitted_policy_observed": _traffic_rates(
            attempts, admitted_ids, duration_s
        ),
        "queue_enqueued_observed": _traffic_rates(
            attempts, enqueued_ids, duration_s
        ),
        "terminal_accounting": {
            "packets": len(all_ids),
            "terminal_packets": terminal_count,
            "delivered": statuses["delivered"],
            "dropped_at_ingress": statuses["dropped_at_ingress"],
            "dropped_in_medium": statuses["dropped_in_medium"],
            "expired_at_drain": statuses["expired_at_drain"],
            "pending": statuses["pending"],
            "status_counts": dict(sorted(statuses.items())),
            "invariant_holds": len(all_ids) == terminal_count
            and statuses["pending"] == 0,
        },
        "per_uav_terminal_counts": _per_uav_terminal_counts(
            attempts, outcomes, admitted_ids, enqueued_ids
        ),
        "classes": result,
    }


def netns_command(namespace: str, *arguments: str) -> list[str]:
    return ["ip", "netns", "exec", namespace, sys.executable, "-u", str(Path(__file__).resolve()), *arguments]


def wait_ready(path: Path, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.is_file() and path.stat().st_size:
            return
        time.sleep(0.05)
    raise ProfileError(f"receiver did not become ready: {path}")


def terminate_process(process: subprocess.Popen[Any], timeout_s: float = 2.0) -> None:
    """Stop one profile child without allowing it to leak into later checks."""

    if process.poll() is not None:
        return
    try:
        process.terminate()
    except OSError:
        return
    try:
        process.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
            process.wait(timeout=timeout_s)
        except (OSError, subprocess.TimeoutExpired):
            pass
    except OSError:
        pass


def run_profiles(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    qos_path = Path(args.qos).resolve()
    qos = load_qos(qos_path)
    configured_profiles = tuple(qos["profiles"])
    gated_profiles = tuple(
        name
        for name in configured_profiles
        if bool(qos["profiles"][name]["gates_overall_status"])
    )
    selected_profiles = selected_profile_names(
        args.profiles,
        configured_profiles,
        default_names=gated_profiles,
    )
    stock = args.medium_access_mode == "stock_ns3_csma"
    validate_engine_shaping_mode(
        run_dir,
        qos_path,
        qos,
        selected_profiles,
        medium_access_mode=args.medium_access_mode,
    )
    protection = qos["protection"]
    drain_interval_ms = int(protection["drain_interval_ms"])
    event_log_flush_max_delay_ms = int(protection["event_log_flush_max_delay_ms"])
    event_log_snapshot_wait_intervals = int(
        protection["event_log_snapshot_wait_intervals"]
    )
    terminal_expiry_after_drain = bool(
        protection["terminal_expiry_after_drain"]
    )
    attempts_all = run_dir / "logs/communication_attempts.jsonl"
    deliveries_all = run_dir / "logs/communication_deliveries.jsonl"
    terminal_all = run_dir / "logs/communication_terminal_outcomes.jsonl"
    windows = run_dir / "logs/profile_windows.jsonl"
    ns3_events_path = run_dir / "logs/ns3_packet_events.jsonl"
    for path in (attempts_all, deliveries_all, terminal_all, windows):
        path.unlink(missing_ok=True)
    stale_metrics = [run_dir / "metrics/traffic_profiles.json"] + [
        run_dir / "metrics" / f"profile_{name}.json"
        for name in configured_profiles
    ]
    for path in stale_metrics:
        path.unlink(missing_ok=True)
    combined: dict[str, Any] = {}
    for profile_name in selected_profiles:
        profile = qos["profiles"][profile_name]
        duration_s = int(profile["duration_s"])
        shaping_enabled = False if stock else bool(profile["shaping_enabled"])
        gates_overall_status = False if stock else bool(profile["gates_overall_status"])
        profile_dir = run_dir / "logs" / "profiles" / profile_name
        profile_dir.mkdir(parents=True, exist_ok=True)
        ready = profile_dir / "receiver.ready"
        deliveries_path = profile_dir / "deliveries.jsonl"
        ready.unlink(missing_ok=True)
        deliveries_path.unlink(missing_ok=True)
        ns3_event_start_offset = complete_jsonl_end_offset(ns3_events_path)
        start_ns = time.monotonic_ns() + 2_000_000_000
        offered_end_ns = start_ns + duration_s * 1_000_000_000
        end_ns = offered_end_ns + drain_interval_ms * 1_000_000
        receiver: subprocess.Popen[bytes] | None = None
        receiver_log_handle = (profile_dir / "receiver.log").open("wb")
        senders: list[tuple[subprocess.Popen[bytes], Path, Any]] = []
        try:
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
                stderr=receiver_log_handle,
            )
            wait_ready(ready)
            for uav_id in UAV_IDS:
                for traffic_class in CLASS_NAMES:
                    attempt_path = (
                        profile_dir
                        / f"attempts_{traffic_class}_uav{uav_id}.jsonl"
                    )
                    log_handle = (
                        profile_dir / f"sender_{traffic_class}_uav{uav_id}.log"
                    ).open("wb")
                    try:
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
                                "--end-ns",
                                str(offered_end_ns),
                                "--qos",
                                str(qos_path),
                                "--output",
                                str(attempt_path),
                            ),
                            stdout=subprocess.DEVNULL,
                            stderr=log_handle,
                        )
                    except BaseException:
                        log_handle.close()
                        raise
                    senders.append((process, attempt_path, log_handle))
            append_jsonl(
                windows,
                {
                    "event": "profile_start",
                    "profile": profile_name,
                    "scheduled_start_monotonic_ns": start_ns,
                    "scheduled_offered_end_monotonic_ns": offered_end_ns,
                    "scheduled_drain_end_monotonic_ns": end_ns,
                    "duration_s": duration_s,
                    "drain_interval_ms": drain_interval_ms,
                    "event_log_flush_max_delay_ms": event_log_flush_max_delay_ms,
                    "event_log_snapshot_wait_intervals": event_log_snapshot_wait_intervals,
                    "terminal_expiry_after_drain": terminal_expiry_after_drain,
                    "shaping_enabled": shaping_enabled,
                    "gates_overall_status": gates_overall_status,
                    "configured_profile_order": list(configured_profiles),
                    "selected_profiles": list(selected_profiles),
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
            for process, attempt_path, _log_handle in senders:
                status = process.wait(timeout=duration_s + 20)
                if status:
                    failures.append(f"{attempt_path.name}:exit={status}")
            receiver_status = receiver.wait(
                timeout=duration_s + drain_interval_ms / 1000.0 + 20
            )
            if receiver_status:
                failures.append(f"receiver:exit={receiver_status}")
            if failures:
                raise ProfileError(f"profile {profile_name} failed: {failures}")
            drain_completed_ns = time.monotonic_ns()
            wait_for_event_log_flush(
                event_log_flush_max_delay_ms,
                event_log_snapshot_wait_intervals,
            )
            event_snapshot_ns = time.monotonic_ns()
            ns3_event_end_offset = complete_jsonl_end_offset(ns3_events_path)
        finally:
            for process, _attempt_path, log_handle in senders:
                terminate_process(process)
                log_handle.close()
            if receiver is not None:
                terminate_process(receiver)
            receiver_log_handle.close()
        attempts = [item for _process, path, _log in senders for item in read_jsonl(path)]
        deliveries = read_jsonl(deliveries_path)
        attempt_ids = {str(item["packet_id"]) for item in attempts}
        fragment_hashes = {
            digest
            for item in attempts
            for digest in item.get("fragment_hashes", [])
            if isinstance(digest, str) and digest
        }
        hash_to_packet_ids: dict[str, set[str]] = defaultdict(set)
        for item in attempts:
            logical_id = str(item["packet_id"])
            for digest in item.get("fragment_hashes", []):
                if isinstance(digest, str) and digest:
                    hash_to_packet_ids[digest].add(logical_id)
        ns3_events, admit_hashes, enqueue_hashes = profile_packet_events(
            ns3_events_path,
            fragment_hashes,
            attempt_ids,
            start_offset=ns3_event_start_offset,
            end_offset=ns3_event_end_offset,
            end_ns=end_ns,
        )
        if attempt_ids and not ns3_events:
            raise ProfileError(
                f"profile {profile_name} has attempts but no matched ns-3 events"
            )
        admitted_ids = {
            logical_id
            for digest in admit_hashes
            for logical_id in hash_to_packet_ids.get(digest, ())
        }
        admitted_ids.update(
            str(event["packet_id"])
            for event in ns3_events
            if event.get("event") == "admit" and isinstance(event.get("packet_id"), str)
        )
        enqueued_ids = {
            logical_id
            for digest in enqueue_hashes
            for logical_id in hash_to_packet_ids.get(digest, ())
        }
        enqueued_ids.update(
            str(event["packet_id"])
            for event in ns3_events
            if event.get("event") == "enqueue" and isinstance(event.get("packet_id"), str)
        )
        accounting_deliveries = [
            item
            for item in deliveries
            if item.get("event") == "delivery"
            and not item.get("malformed")
            and item.get("packet_id") in attempt_ids
        ]
        outcomes = terminal_packet_outcomes(
            attempts,
            accounting_deliveries,
            ns3_events,
            finalize_pending=terminal_expiry_after_drain,
        )
        if set(outcomes) != attempt_ids:
            missing = sorted(attempt_ids - set(outcomes))[:5]
            extra = sorted(set(outcomes) - attempt_ids)[:5]
            raise ProfileError(
                "terminal accounting did not cover the exact attempt set: "
                f"missing={missing} extra={extra}"
            )
        pending_ids = [
            packet_id
            for packet_id, outcome in outcomes.items()
            if outcome.get("status") == "pending" or not outcome.get("terminal")
        ]
        if pending_ids:
            raise ProfileError(
                f"terminal accounting left {len(pending_ids)} pending after drain"
            )
        with attempts_all.open("a", encoding="utf-8") as output:
            for item in attempts:
                output.write(json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n")
        with deliveries_all.open("a", encoding="utf-8") as output:
            for item in deliveries:
                output.write(json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n")
        attempts_by_id = {str(item["packet_id"]): item for item in attempts}
        with terminal_all.open("a", encoding="utf-8") as output:
            for packet_id in sorted(outcomes):
                attempt = attempts_by_id[packet_id]
                record = {
                    **outcomes[packet_id],
                    "profile": profile_name,
                    "traffic_class": attempt["traffic_class"],
                    "uav": attempt["uav"],
                    "direction": attempt["direction"],
                    "sequence": attempt["sequence"],
                    "packet_bytes": attempt["packet_bytes"],
                    "admitted_policy_observed": packet_id in admitted_ids,
                    "queue_enqueued_observed": packet_id in enqueued_ids,
                    "shaping_enabled": shaping_enabled,
                    "gates_overall_status": gates_overall_status,
                }
                output.write(
                    json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
                )
        snapshot = profile_snapshot(
            profile_name,
            attempts,
            deliveries,
            outcomes,
            admitted_ids,
            enqueued_ids,
            duration_s=duration_s,
            shaping_enabled=shaping_enabled,
            gates_overall_status=gates_overall_status,
            drain_interval_ms=drain_interval_ms,
            terminal_expiry_after_drain=terminal_expiry_after_drain,
            observed_ns3_event_count=len(ns3_events),
        )
        write_json(run_dir / "metrics" / f"profile_{profile_name}.json", snapshot)
        combined[profile_name] = snapshot
        append_jsonl(
            windows,
            {
                "event": "profile_end",
                "profile": profile_name,
                "monotonic_ns": end_ns,
                "drain_completed_monotonic_ns": drain_completed_ns,
                "ns3_event_snapshot_monotonic_ns": event_snapshot_ns,
                "ns3_event_snapshot_end_offset": ns3_event_end_offset,
                "drain_interval_ms": drain_interval_ms,
                "event_log_flush_max_delay_ms": event_log_flush_max_delay_ms,
                "event_log_snapshot_wait_intervals": event_log_snapshot_wait_intervals,
                "terminal_expiry_after_drain": terminal_expiry_after_drain,
                "shaping_enabled": shaping_enabled,
                "gates_overall_status": gates_overall_status,
                "packets_offered": len(attempt_ids),
                "packets_admitted_policy_observed": len(admitted_ids),
                "packets_queue_enqueued_observed": len(enqueued_ids),
                "dropped_at_ingress": snapshot["terminal_accounting"][
                    "dropped_at_ingress"
                ],
                "dropped_in_medium": snapshot["terminal_accounting"][
                    "dropped_in_medium"
                ],
                "expired_at_drain": snapshot["terminal_accounting"][
                    "expired_at_drain"
                ],
                "packets_pending": snapshot["terminal_accounting"]["pending"],
            },
        )
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
    sender.add_argument("--end-ns", type=int, required=True)
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
    run.add_argument(
        "--profiles",
        help=(
            "comma-separated profile subset; defaults to gated profiles and "
            "always follows product-config order"
        ),
    )
    run.add_argument(
        "--medium-access-mode",
        choices=("stock_ns3_csma", "centralized_priority_scheduler_over_csma_channel"),
        default="centralized_priority_scheduler_over_csma_channel",
    )
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
