#!/usr/bin/env python3
"""Receive producer clocks over bounded AF_UNIX datagrams in real time."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import socket
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in os.sys.path:
    os.sys.path.insert(0, str(ROOT))

from network.scripts.m4_runtime_orchestrator import write_exclusive
from network.bridge.runtime_clock_beacon import beacon
from network.validation.m4_common import M4ValidationError, strict_json
from network.validation.m4_runtime import CLOCK_SAMPLE_SCHEMA, REQUIRED_CLOCK_PRODUCERS


DATAGRAM_SCHEMA = "ams.m4.clock_datagram/v1"
MAX_DATAGRAM_BYTES = 4096


def decode_unique(raw: bytes) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for key, value in pairs:
            if key in output:
                raise M4ValidationError(f"duplicate producer key: {key}")
            output[key] = value
        return output

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise M4ValidationError(f"invalid clock datagram JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise M4ValidationError("clock datagram is not an object")
    return value


class EvidenceWriter:
    def __init__(self, run_dir: Path, run_id: str, runtime_id: str):
        logs = run_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        self.correlation = (logs / "m4_clock_correlations.jsonl").open("xb")
        self.raw = (logs / "m4_clock_datagrams.bin").open("xb")
        self.index = (logs / "m4_clock_datagram_index.jsonl").open("xb")
        self.run_id = run_id
        self.runtime_id = runtime_id
        self.event_sequence = 0
        self.offset = 0
        self.counts = {producer: 0 for producer in REQUIRED_CLOCK_PRODUCERS}
        self.pids: dict[str, int] = {}

    def emit(self, raw: bytes, received_ns: int) -> None:
        message = decode_unique(raw)
        expected_keys = {
            "schema",
            "sample_index",
            "producer",
            "producer_monotonic_ns",
            "producer_pid",
        }
        producer = message.get("producer")
        producer_ns = message.get("producer_monotonic_ns")
        producer_pid = message.get("producer_pid")
        if (
            set(message) != expected_keys
            or message.get("schema") != DATAGRAM_SCHEMA
            or producer not in REQUIRED_CLOCK_PRODUCERS
            or isinstance(producer_ns, bool)
            or not isinstance(producer_ns, int)
            or producer_ns <= 0
            or producer_ns > received_ns
            or isinstance(producer_pid, bool)
            or not isinstance(producer_pid, int)
            or producer_pid <= 0
        ):
            raise M4ValidationError("clock producer envelope is invalid")
        producer = str(producer)
        expected_index = self.counts[producer]
        if message.get("sample_index") != expected_index:
            raise M4ValidationError(
                f"{producer} clock sequence differs: {message.get('sample_index')} != {expected_index}"
            )
        previous_pid = self.pids.setdefault(producer, producer_pid)
        if previous_pid != producer_pid:
            raise M4ValidationError(f"{producer} PID changed during one runtime")
        digest = hashlib.sha256(raw).hexdigest()
        self.raw.write(raw)
        self.index.write(
            (
                json.dumps(
                    {
                        "datagram_sequence": self.event_sequence + 1,
                        "offset": self.offset,
                        "length": len(raw),
                        "sha256": digest,
                        "collector_received_monotonic_ns": received_ns,
                        "producer": producer,
                        "producer_pid": producer_pid,
                        "producer_sample_index": expected_index,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode()
        )
        self.offset += len(raw)
        self.counts[producer] += 1
        self.event_sequence += 1
        self.correlation.write(
            (
                json.dumps(
                    {
                        "schema": CLOCK_SAMPLE_SCHEMA,
                        "event_sequence": self.event_sequence,
                        "run_id": self.run_id,
                        "runtime_id": self.runtime_id,
                        "producer": producer,
                        "sample_index": expected_index,
                        "producer_monotonic_ns": producer_ns,
                        "collector_host_monotonic_ns": received_ns,
                    },
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode()
        )
        self.raw.flush()
        self.index.flush()
        self.correlation.flush()

    def close(self) -> None:
        for stream in (self.raw, self.index, self.correlation):
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--ready-file", type=Path, required=True)
    parser.add_argument("--stop-file", type=Path, required=True)
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    contract = strict_json(args.contract.resolve())
    socket_path = args.socket.resolve()
    if len(os.fsencode(socket_path)) >= 100:
        print("FAIL M4 clock collector: AF_UNIX path is too long", file=os.sys.stderr)
        return 2
    if socket_path.exists() or socket_path.is_symlink():
        print("FAIL M4 clock collector: socket path already exists", file=os.sys.stderr)
        return 2
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    writer = EvidenceWriter(
        run_dir, str(contract["run_id"]), str(contract["runtime_id"])
    )
    stopped = False
    stop_event = threading.Event()

    def request_stop(*_unused: Any) -> None:
        nonlocal stopped
        stopped = True
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    beacon_thread: threading.Thread | None = None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
            channel.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1_048_576)
            channel.bind(str(socket_path))
            os.chmod(socket_path, 0o666)
            channel.settimeout(0.05)
            write_exclusive(
                args.ready_file,
                {
                    "pid": os.getpid(),
                    "monotonic_ns": time.monotonic_ns(),
                    "socket_path": str(socket_path),
                    "transport": "AF_UNIX/SOCK_DGRAM",
                    "max_datagram_bytes": MAX_DATAGRAM_BYTES,
                },
            )
            beacon_thread = threading.Thread(
                target=beacon,
                args=(socket_path, "clock_collector", stop_event),
                daemon=True,
            )
            beacon_thread.start()
            while not stopped and not args.stop_file.exists():
                try:
                    raw = channel.recv(MAX_DATAGRAM_BYTES + 1)
                    received = time.clock_gettime_ns(time.CLOCK_MONOTONIC)
                except socket.timeout:
                    continue
                if not raw or len(raw) > MAX_DATAGRAM_BYTES:
                    raise M4ValidationError("clock datagram is empty or oversized")
                writer.emit(raw, received)
        summary = {
            "schema": "ams.m4.clock_collection_summary/v1",
            "run_id": contract["run_id"],
            "runtime_id": contract["runtime_id"],
            "completed_monotonic_ns": time.monotonic_ns(),
            "counts": writer.counts,
            "producer_pids": writer.pids,
            "producer_count": len(writer.counts),
            "independent_receive_process_pid": os.getpid(),
            "transport": "AF_UNIX/SOCK_DGRAM",
        }
        write_exclusive(run_dir / "raw/m4_clock_collection_summary.json", summary)
        missing = {
            producer: count for producer, count in writer.counts.items() if count < 600
        }
        if missing:
            raise M4ValidationError(f"clock collection has <600 samples: {missing}")
        return 0
    except (M4ValidationError, OSError, KeyError, TypeError, ValueError) as exc:
        print(f"FAIL M4 clock collector: {exc}", file=os.sys.stderr)
        return 2
    finally:
        stop_event.set()
        if beacon_thread is not None:
            beacon_thread.join(2.0)
        writer.close()
        try:
            socket_path.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
