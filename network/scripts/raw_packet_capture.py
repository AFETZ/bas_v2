#!/usr/bin/env python3
"""Capture Ethernet frames with bounded NET_RAW and exact drop accounting."""

from __future__ import annotations

import argparse
import ctypes
import errno
import json
import os
import select
import signal
import socket
import struct
import sys
import time
from io import BufferedWriter
from pathlib import Path
from types import FrameType
from typing import Any


STATS_CONTRACT = "ams.raw-packet-capture-stats/v2"
SNAPLEN = 65_535
SOL_PACKET = 263
PACKET_STATISTICS = 6
ETH_P_ALL = 0x0003
SO_RCVBUFFORCE = 33
SO_ATTACH_FILTER = 26
CAPTURE_PROTOCOL = "ETH_P_ALL"
PACKET_FILTER = "none"
UDP_PORT_FILTER_PREFIX = "udp-ports:v1:"

# Linux accounts twice the requested SO_RCVBUF value and reports that doubled
# value through getsockopt().  Keep both sides of that contract exact: the
# request is a finite 8 MiB per capture process and the observable effective
# value is exactly 16 MiB.  The component profiles already grant NET_ADMIN; the
# force option is used only when the namespace's finite rmem_max would otherwise
# cap the regular request.
RECEIVE_BUFFER_REQUESTED_BYTES = 8 * 1024 * 1024
RECEIVE_BUFFER_EFFECTIVE_BYTES = 2 * RECEIVE_BUFFER_REQUESTED_BYTES

# Drain enough queued frames to amortize select() and buffered-file overhead,
# while bounding both work and temporary memory before returning to the outer
# loop (and therefore to signal handling).
DRAIN_BATCH_PACKET_LIMIT = 256
DRAIN_BATCH_BYTE_LIMIT = 4 * 1024 * 1024
SELECT_TIMEOUT_SECONDS = 0.2


class CaptureError(RuntimeError):
    """Raw capture cannot produce trustworthy evidence."""


class SockFilter(ctypes.Structure):
    """Linux classic-BPF instruction layout from ``linux/filter.h``."""

    _fields_ = [
        ("code", ctypes.c_ushort),
        ("jt", ctypes.c_ubyte),
        ("jf", ctypes.c_ubyte),
        ("k", ctypes.c_uint32),
    ]


class SockFprog(ctypes.Structure):
    """Linux classic-BPF program descriptor copied by ``setsockopt``."""

    _fields_ = [
        ("length", ctypes.c_ushort),
        ("filters", ctypes.POINTER(SockFilter)),
    ]


def parse_udp_port_filter(value: str | None) -> tuple[int, ...]:
    """Parse one canonical, finite UDP-port allowlist."""

    if value is None:
        return ()
    if not value:
        raise CaptureError("UDP port filter must not be empty")
    try:
        ports = tuple(sorted({int(item) for item in value.split(",")}))
    except ValueError as exc:
        raise CaptureError("UDP port filter contains a non-integer") from exc
    if not ports or any(port < 1 or port > 65_535 for port in ports):
        raise CaptureError("UDP port filter contains an out-of-range port")
    canonical = ",".join(str(port) for port in ports)
    if value != canonical:
        raise CaptureError("UDP port filter is not sorted, unique, canonical decimal")
    return ports


def udp_port_filter_contract(ports: tuple[int, ...]) -> str:
    return PACKET_FILTER if not ports else UDP_PORT_FILTER_PREFIX + ",".join(
        str(port) for port in ports
    )


def compile_udp_port_filter(ports: tuple[int, ...]) -> tuple[SockFilter, ...]:
    """Compile an Ethernet IPv4/IPv6 UDP-port cBPF allowlist.

    Both source and destination ports are checked.  That is important for the
    M3 root-loopback proof: every declared canary is retained even if a buggy
    path reverses or otherwise leaks it, while unrelated ROS/DDS traffic never
    enters the packet-socket receive queue.
    """

    if not ports:
        raise CaptureError("refusing to compile an empty UDP port filter")

    # Classic-BPF opcodes used below.
    ld_h_abs = 0x28
    ld_b_abs = 0x30
    ld_h_ind = 0x48
    ldx_b_msh = 0xB1
    jmp_jeq_k = 0x15
    ret_k = 0x06

    # Instructions are assembled with symbolic true/false targets so all jump
    # distances are independently range-checked rather than hand-counted.
    assembled: list[tuple[int, int | str, int | str, int]] = []
    labels: dict[str, int] = {}

    def label(name: str) -> None:
        labels[name] = len(assembled)

    def emit(code: int, k: int, jt: int | str = 0, jf: int | str = 0) -> None:
        assembled.append((code, jt, jf, k))

    def emit_port_checks(*, load_code: int, offset: int, accept: str) -> None:
        emit(load_code, offset)
        for port in ports:
            emit(jmp_jeq_k, port, accept, 0)

    emit(ld_h_abs, 12)
    emit(jmp_jeq_k, 0x0800, "ipv4", 0)
    emit(jmp_jeq_k, 0x86DD, "ipv6", "reject_non_ip")
    label("reject_non_ip")
    emit(ret_k, 0)

    label("ipv4")
    emit(ld_b_abs, 23)
    emit(jmp_jeq_k, socket.IPPROTO_UDP, 0, "reject_ipv4")
    emit(ldx_b_msh, 14)
    emit_port_checks(load_code=ld_h_ind, offset=14, accept="accept_ipv4")
    emit_port_checks(load_code=ld_h_ind, offset=16, accept="accept_ipv4")
    label("reject_ipv4")
    emit(ret_k, 0)
    label("accept_ipv4")
    emit(ret_k, SNAPLEN)

    label("ipv6")
    emit(ld_b_abs, 20)
    emit(jmp_jeq_k, socket.IPPROTO_UDP, 0, "reject_ipv6")
    emit_port_checks(load_code=ld_h_abs, offset=54, accept="accept_ipv6")
    emit_port_checks(load_code=ld_h_abs, offset=56, accept="accept_ipv6")
    label("reject_ipv6")
    emit(ret_k, 0)
    label("accept_ipv6")
    emit(ret_k, SNAPLEN)

    program: list[SockFilter] = []
    for index, (code, raw_jt, raw_jf, k) in enumerate(assembled):
        jumps: list[int] = []
        for target in (raw_jt, raw_jf):
            if isinstance(target, str):
                if target not in labels:
                    raise CaptureError(f"unknown cBPF target: {target}")
                distance = labels[target] - index - 1
                if distance < 0 or distance > 255:
                    raise CaptureError("UDP port filter cBPF jump is out of range")
                jumps.append(distance)
            else:
                jumps.append(target)
        program.append(SockFilter(code, jumps[0], jumps[1], k))
    if len(program) > 4_096:
        raise CaptureError("UDP port filter exceeds Linux cBPF instruction limit")
    return tuple(program)


def attach_udp_port_filter(
    packet_socket: socket.socket, ports: tuple[int, ...]
) -> str:
    """Attach the compiled filter directly to the owned AF_PACKET socket."""

    if not ports:
        return PACKET_FILTER
    instructions = compile_udp_port_filter(ports)
    array_type = SockFilter * len(instructions)
    array = array_type(*instructions)
    descriptor = SockFprog(
        len(instructions), ctypes.cast(array, ctypes.POINTER(SockFilter))
    )
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.setsockopt(
        packet_socket.fileno(),
        socket.SOL_SOCKET,
        SO_ATTACH_FILTER,
        ctypes.byref(descriptor),
        ctypes.sizeof(descriptor),
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise CaptureError(
            "cannot attach exact UDP port cBPF filter: "
            + os.strerror(error_number or errno.EINVAL)
        )
    return udp_port_filter_contract(ports)


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


def configure_receive_buffer(packet_socket: socket.socket) -> tuple[str, int]:
    """Apply and verify one finite Linux packet-socket receive-buffer contract."""

    packet_socket.setsockopt(
        socket.SOL_SOCKET,
        socket.SO_RCVBUF,
        RECEIVE_BUFFER_REQUESTED_BYTES,
    )
    effective = packet_socket.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
    setter = "SO_RCVBUF"
    if effective != RECEIVE_BUFFER_EFFECTIVE_BYTES:
        try:
            packet_socket.setsockopt(
                socket.SOL_SOCKET,
                SO_RCVBUFFORCE,
                RECEIVE_BUFFER_REQUESTED_BYTES,
            )
        except OSError as exc:
            raise CaptureError(
                "cannot establish exact packet receive buffer: "
                f"requested={RECEIVE_BUFFER_REQUESTED_BYTES} "
                f"regular_effective={effective}"
            ) from exc
        effective = packet_socket.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
        setter = "SO_RCVBUFFORCE"
    if effective != RECEIVE_BUFFER_EFFECTIVE_BYTES:
        raise CaptureError(
            "packet receive buffer differs from the finite contract: "
            f"requested={RECEIVE_BUFFER_REQUESTED_BYTES} "
            f"expected_effective={RECEIVE_BUFFER_EFFECTIVE_BYTES} "
            f"observed_effective={effective} setter={setter}"
        )
    return setter, effective


def drain_packet_batch(
    packet_socket: socket.socket,
    output: BufferedWriter,
) -> int:
    """Drain one bounded FIFO batch and append every frame to the PCAP."""

    encoded = bytearray()
    drained = 0
    while drained < DRAIN_BATCH_PACKET_LIMIT and len(encoded) < DRAIN_BATCH_BYTE_LIMIT:
        try:
            frame = packet_socket.recv(SNAPLEN)
        except BlockingIOError:
            break
        captured = frame[:SNAPLEN]
        realtime_ns = time.time_ns()
        encoded.extend(
            struct.pack(
                "<IIII",
                realtime_ns // 1_000_000_000,
                (realtime_ns % 1_000_000_000) // 1_000,
                len(captured),
                len(frame),
            )
        )
        encoded.extend(captured)
        drained += 1
    if encoded and output.write(encoded) != len(encoded):
        raise CaptureError("short write while appending packet capture batch")
    return drained


def capture(
    interface: str,
    pcap: Path,
    stats: Path,
    *,
    udp_port_filter: tuple[int, ...] = (),
) -> None:
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
    receive_buffer_setter = ""
    receive_buffer_effective = 0
    packet_filter = PACKET_FILTER
    started_ns = 0
    written = 0
    try:
        receive_buffer_setter, receive_buffer_effective = configure_receive_buffer(
            packet_socket
        )
        packet_filter = attach_udp_port_filter(packet_socket, udp_port_filter)
        packet_socket.bind((interface, 0))
        packet_socket.setblocking(False)
        started_ns = time.monotonic_ns()
        with os.fdopen(descriptor, "wb", closefd=False) as output:
            global_header = struct.pack(
                "<IHHIIII",
                0xA1B2C3D4,
                2,
                4,
                0,
                0,
                SNAPLEN,
                1,
            )
            if output.write(global_header) != len(global_header):
                raise CaptureError("short write while creating packet capture")
            while stop_signal is None:
                ready, _, _ = select.select(
                    [packet_socket], [], [], SELECT_TIMEOUT_SECONDS
                )
                if not ready:
                    continue
                written += drain_packet_batch(packet_socket, output)
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
            "capture_protocol": CAPTURE_PROTOCOL,
            "packet_filter": packet_filter,
            "pcap_path": pcap.name,
            "pcap_bytes": pcap.stat().st_size,
            "linktype": 1,
            "snaplen": SNAPLEN,
            "receive_buffer_requested_bytes": RECEIVE_BUFFER_REQUESTED_BYTES,
            "receive_buffer_effective_bytes": receive_buffer_effective,
            "receive_buffer_setter": receive_buffer_setter,
            "drain_batch_packet_limit": DRAIN_BATCH_PACKET_LIMIT,
            "drain_batch_byte_limit": DRAIN_BATCH_BYTE_LIMIT,
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
    parser.add_argument(
        "--udp-port-filter",
        help="canonical comma-separated UDP source/destination port allowlist",
    )
    args = parser.parse_args(argv)
    try:
        capture(
            args.interface,
            args.pcap,
            args.stats,
            udp_port_filter=parse_udp_port_filter(args.udp_port_filter),
        )
    except (CaptureError, OSError, ValueError) as exc:
        print(f"FAIL raw packet capture: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
