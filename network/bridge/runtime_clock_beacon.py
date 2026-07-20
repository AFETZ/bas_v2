#!/usr/bin/env python3
"""Bounded per-process CLOCK_MONOTONIC evidence emitter for joint runtimes."""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path


DATAGRAM_SCHEMA = "ams.m4.clock_datagram/v1"


def beacon(socket_path: Path, producer: str, stop: threading.Event) -> None:
    """Emit one nonblocking, identity-bearing sample per second.

    This neutral helper contains no endpoint traffic producer and never opens a
    network or MAVLink socket.  The owning process supplies its own PID and
    monotonic clock; the independent collector timestamps datagram receipt.
    """

    index = 0
    next_sample = time.monotonic_ns()
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
        channel.setblocking(False)
        while not stop.is_set():
            now = time.monotonic_ns()
            if now >= next_sample:
                raw = json.dumps(
                    {
                        "schema": DATAGRAM_SCHEMA,
                        "sample_index": index,
                        "producer": producer,
                        "producer_monotonic_ns": now,
                        "producer_pid": os.getpid(),
                    },
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
                try:
                    channel.sendto(raw, str(socket_path))
                except (BlockingIOError, FileNotFoundError, ConnectionRefusedError):
                    stop.wait(0.02)
                    continue
                index += 1
                next_sample = now + 1_000_000_000
            stop.wait(0.02)


__all__ = ["DATAGRAM_SCHEMA", "beacon"]
