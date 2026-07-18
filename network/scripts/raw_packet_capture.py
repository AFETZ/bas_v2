#!/usr/bin/env python3
"""Capture Ethernet frames with bounded NET_RAW and exact drop accounting."""

from __future__ import annotations

import argparse
import json
import os
import select
import signal
import socket
import struct
import sys
import time
from pathlib import Path
from types import FrameType
from typing import Any


STATS_CONTRACT = "ams.raw-packet-capture-stats/v1"
SNAPLEN = 65_535
SOL_PACKET = 263
PACKET_STATISTICS = 6
ETH_P_ALL = 0x0003


class CaptureError(RuntimeError):
    """Raw capture cannot produce trustworthy evidence."""


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def write_exclusive(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        payload = canonical_json(value)
        if os.write(descriptor, payload) != len(payload):
            raise CaptureError("short write while sealing capture statistics")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def capture(interface: str, pcap: Path, stats: Path) -> None:
    """Write one Ethernet PCAP without any privilege-changing helper.

    The formal component profile intentionally omits SETUID/SETGID. Debian's
    tcpdump build always tries to change to its configured capture user and
    therefore cannot run in that profile. AF_PACKET consumes only the already
    bounded NET_RAW capability and exposes kernel packet/drop counters.
    """

    stop_signal: int | None = None

    def request_stop(signum: int, _frame: FrameType | None) -> None:
        nonlocal stop_signal
        stop_signal = signum

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    pcap.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(pcap, flags, 0o600)
    packet_socket = socket.socket(
        socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL)
    )
    packet_socket.bind((interface, 0))
    packet_socket.setblocking(False)
    started_ns = time.monotonic_ns()
    written = 0
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as output:
            output.write(
                struct.pack(
                    "<IHHIIII",
                    0xA1B2C3D4,
                    2,
                    4,
                    0,
                    0,
                    SNAPLEN,
                    1,
                )
            )
            while stop_signal is None:
                ready, _, _ = select.select([packet_socket], [], [], 0.2)
                if not ready:
                    continue
                try:
                    frame = packet_socket.recv(SNAPLEN)
                except BlockingIOError:
                    continue
                captured = frame[:SNAPLEN]
                realtime_ns = time.time_ns()
                output.write(
                    struct.pack(
                        "<IIII",
                        realtime_ns // 1_000_000_000,
                        (realtime_ns % 1_000_000_000) // 1_000,
                        len(captured),
                        len(frame),
                    )
                )
                output.write(captured)
                written += 1
            output.flush()
            os.fsync(output.fileno())
        raw_stats = packet_socket.getsockopt(SOL_PACKET, PACKET_STATISTICS, 12)
        if len(raw_stats) < 8:
            raise CaptureError("AF_PACKET statistics are truncated")
        kernel_packets, kernel_drops = struct.unpack("=II", raw_stats[:8])
    finally:
        packet_socket.close()
        os.close(descriptor)
    if stop_signal is None:
        raise CaptureError("raw capture exited without a stop signal")
    write_exclusive(
        stats,
        {
            "contract": STATS_CONTRACT,
            "interface": interface,
            "pcap_path": pcap.name,
            "pcap_bytes": pcap.stat().st_size,
            "linktype": 1,
            "snaplen": SNAPLEN,
            "started_monotonic_ns": started_ns,
            "stopped_monotonic_ns": time.monotonic_ns(),
            "stop_signal": signal.Signals(stop_signal).name,
            "packets_written": written,
            "packets_received_kernel": kernel_packets,
            "packets_dropped_kernel": kernel_drops,
        },
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interface", required=True)
    parser.add_argument("--pcap", type=Path, required=True)
    parser.add_argument("--stats", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        capture(args.interface, args.pcap, args.stats)
    except (CaptureError, OSError, ValueError) as exc:
        print(f"FAIL raw packet capture: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
