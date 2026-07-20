"""Focused throughput and fail-closed tests for the AF_PACKET capture tool."""

from __future__ import annotations

import io
import math
import socket
import struct
import unittest
from collections import deque
from unittest import mock

from network.scripts import raw_packet_capture as capture


class QueuedPacketSocket:
    def __init__(self, frames: list[bytes]):
        self.frames = deque(frames)
        self.recv_calls = 0

    def recv(self, size: int) -> bytes:
        self.recv_calls += 1
        if not self.frames:
            raise BlockingIOError
        return self.frames.popleft()[:size]


def decode_batch(payload: bytes) -> list[bytes]:
    frames: list[bytes] = []
    offset = 0
    while offset < len(payload):
        if len(payload) - offset < 16:
            raise AssertionError("truncated PCAP packet header")
        _seconds, _microseconds, captured_size, wire_size = struct.unpack_from(
            "<IIII", payload, offset
        )
        offset += 16
        frame = payload[offset : offset + captured_size]
        if len(frame) != captured_size or wire_size != captured_size:
            raise AssertionError("invalid untruncated PCAP packet record")
        frames.append(frame)
        offset += captured_size
    return frames


def execute_cbpf(program: tuple[capture.SockFilter, ...], frame: bytes) -> int:
    """Small interpreter for the exact classic-BPF subset emitted by the tool."""

    accumulator = 0
    index_register = 0
    instruction_index = 0
    while instruction_index < len(program):
        instruction = program[instruction_index]
        code = instruction.code
        if code == 0x28:  # ld h [k]
            accumulator = int.from_bytes(
                frame[instruction.k : instruction.k + 2], "big"
            )
        elif code == 0x30:  # ld b [k]
            accumulator = frame[instruction.k]
        elif code == 0x48:  # ld h [x + k]
            offset = index_register + instruction.k
            accumulator = int.from_bytes(frame[offset : offset + 2], "big")
        elif code == 0xB1:  # ldx 4 * ([k] & 0xf)
            index_register = 4 * (frame[instruction.k] & 0x0F)
        elif code == 0x15:  # jeq #k
            instruction_index += (
                instruction.jt if accumulator == instruction.k else instruction.jf
            )
        elif code == 0x06:  # ret #k
            return instruction.k
        else:
            raise AssertionError(f"unsupported test cBPF opcode: {code:#x}")
        instruction_index += 1
    raise AssertionError("cBPF program fell off its end")


def ethernet_udp_frame(
    *, version: int, source_port: int, destination_port: int, protocol: int = 17
) -> bytes:
    if version == 4:
        frame = bytearray(14 + 20 + 8)
        frame[12:14] = (0x0800).to_bytes(2, "big")
        frame[14] = 0x45
        frame[23] = protocol
        udp_offset = 34
    elif version == 6:
        frame = bytearray(14 + 40 + 8)
        frame[12:14] = (0x86DD).to_bytes(2, "big")
        frame[14] = 0x60
        frame[20] = protocol
        udp_offset = 54
    else:
        raise AssertionError("unsupported test IP version")
    frame[udp_offset : udp_offset + 2] = source_port.to_bytes(2, "big")
    frame[udp_offset + 2 : udp_offset + 4] = destination_port.to_bytes(2, "big")
    return bytes(frame)


class ReceiveBufferContractTests(unittest.TestCase):
    def test_regular_receive_buffer_is_accepted_only_at_exact_effective_size(
        self,
    ) -> None:
        packet_socket = mock.Mock()
        packet_socket.getsockopt.return_value = capture.RECEIVE_BUFFER_EFFECTIVE_BYTES

        setter, effective = capture.configure_receive_buffer(packet_socket)

        self.assertEqual(setter, "SO_RCVBUF")
        self.assertEqual(effective, capture.RECEIVE_BUFFER_EFFECTIVE_BYTES)
        packet_socket.setsockopt.assert_called_once_with(
            socket.SOL_SOCKET,
            socket.SO_RCVBUF,
            capture.RECEIVE_BUFFER_REQUESTED_BYTES,
        )

    def test_receive_buffer_uses_bounded_force_fallback_when_sysctl_caps_regular(
        self,
    ) -> None:
        packet_socket = mock.Mock()
        packet_socket.getsockopt.side_effect = [
            425_984,
            capture.RECEIVE_BUFFER_EFFECTIVE_BYTES,
        ]

        setter, effective = capture.configure_receive_buffer(packet_socket)

        self.assertEqual(setter, "SO_RCVBUFFORCE")
        self.assertEqual(effective, capture.RECEIVE_BUFFER_EFFECTIVE_BYTES)
        self.assertEqual(
            packet_socket.setsockopt.call_args_list,
            [
                mock.call(
                    socket.SOL_SOCKET,
                    socket.SO_RCVBUF,
                    capture.RECEIVE_BUFFER_REQUESTED_BYTES,
                ),
                mock.call(
                    socket.SOL_SOCKET,
                    capture.SO_RCVBUFFORCE,
                    capture.RECEIVE_BUFFER_REQUESTED_BYTES,
                ),
            ],
        )

    def test_receive_buffer_fails_closed_when_force_capability_is_absent(self) -> None:
        packet_socket = mock.Mock()
        packet_socket.getsockopt.return_value = 425_984
        packet_socket.setsockopt.side_effect = [None, PermissionError("denied")]

        with self.assertRaisesRegex(
            capture.CaptureError, "cannot establish exact packet receive buffer"
        ):
            capture.configure_receive_buffer(packet_socket)

    def test_receive_buffer_fails_closed_when_force_result_is_not_exact(self) -> None:
        packet_socket = mock.Mock()
        packet_socket.getsockopt.side_effect = [425_984, 8 * 1024 * 1024]

        with self.assertRaisesRegex(
            capture.CaptureError,
            "packet receive buffer differs from the finite contract",
        ):
            capture.configure_receive_buffer(packet_socket)

    def test_frozen_capture_configuration_is_finite_and_filterless(self) -> None:
        self.assertEqual(capture.STATS_CONTRACT, "ams.raw-packet-capture-stats/v2")
        self.assertEqual(capture.CAPTURE_PROTOCOL, "ETH_P_ALL")
        self.assertEqual(capture.PACKET_FILTER, "none")
        self.assertEqual(capture.RECEIVE_BUFFER_REQUESTED_BYTES, 8 * 1024 * 1024)
        self.assertEqual(
            capture.RECEIVE_BUFFER_EFFECTIVE_BYTES,
            2 * capture.RECEIVE_BUFFER_REQUESTED_BYTES,
        )
        self.assertGreater(capture.DRAIN_BATCH_PACKET_LIMIT, 1)
        self.assertLessEqual(capture.DRAIN_BATCH_PACKET_LIMIT, 1_024)
        self.assertGreater(capture.DRAIN_BATCH_BYTE_LIMIT, capture.SNAPLEN)
        self.assertLessEqual(capture.DRAIN_BATCH_BYTE_LIMIT, 8 * 1024 * 1024)


class UdpPortFilterTests(unittest.TestCase):
    def test_port_contract_requires_canonical_sorted_unique_decimal(self) -> None:
        self.assertEqual(capture.parse_udp_port_filter(None), ())
        self.assertEqual(capture.parse_udp_port_filter("14550,15300"), (14550, 15300))
        self.assertEqual(
            capture.udp_port_filter_contract((14550, 15300)),
            "udp-ports:v1:14550,15300",
        )
        for malformed in ("", "15300,14550", "14550,14550", "0", "65536", "x"):
            with self.subTest(malformed=malformed), self.assertRaises(
                capture.CaptureError
            ):
                capture.parse_udp_port_filter(malformed)

    def test_filter_retains_either_udp_port_for_ipv4_and_ipv6_only(self) -> None:
        program = capture.compile_udp_port_filter((14550, 15300))
        accepted = (
            ethernet_udp_frame(version=4, source_port=14550, destination_port=40000),
            ethernet_udp_frame(version=4, source_port=40000, destination_port=15300),
            ethernet_udp_frame(version=6, source_port=15300, destination_port=40000),
            ethernet_udp_frame(version=6, source_port=40000, destination_port=14550),
        )
        rejected = (
            ethernet_udp_frame(version=4, source_port=40000, destination_port=40001),
            ethernet_udp_frame(
                version=4,
                source_port=14550,
                destination_port=15300,
                protocol=6,
            ),
            ethernet_udp_frame(version=6, source_port=40000, destination_port=40001),
            ethernet_udp_frame(
                version=6,
                source_port=14550,
                destination_port=15300,
                protocol=6,
            ),
            bytes(64),
        )
        self.assertTrue(all(execute_cbpf(program, frame) for frame in accepted))
        self.assertTrue(
            all(execute_cbpf(program, frame) == 0 for frame in rejected)
        )
        self.assertLessEqual(len(program), 4_096)


class BoundedDrainTests(unittest.TestCase):
    def test_bounded_batch_preserves_a_32000_pps_burst_without_filtering(self) -> None:
        frames = [
            struct.pack("<I", sequence) + bytes([sequence % 251]) * 60
            for sequence in range(32_017)
        ]
        packet_socket = QueuedPacketSocket(frames.copy())
        output = io.BytesIO()
        batch_counts: list[int] = []

        while packet_socket.frames:
            batch_counts.append(capture.drain_packet_batch(packet_socket, output))

        self.assertEqual(decode_batch(output.getvalue()), frames)
        self.assertEqual(sum(batch_counts), len(frames))
        self.assertEqual(
            len(batch_counts),
            math.ceil(len(frames) / capture.DRAIN_BATCH_PACKET_LIMIT),
        )
        self.assertTrue(
            all(0 < count <= capture.DRAIN_BATCH_PACKET_LIMIT for count in batch_counts)
        )

    def test_batch_byte_budget_bounds_temporary_capture_payload(self) -> None:
        frames = [bytes([sequence]) * capture.SNAPLEN for sequence in range(128)]
        packet_socket = QueuedPacketSocket(frames.copy())
        output = io.BytesIO()

        drained = capture.drain_packet_batch(packet_socket, output)

        encoded_size = len(output.getvalue())
        self.assertGreater(encoded_size, capture.DRAIN_BATCH_BYTE_LIMIT)
        self.assertLessEqual(
            encoded_size,
            capture.DRAIN_BATCH_BYTE_LIMIT + capture.SNAPLEN + 16,
        )
        self.assertLess(drained, capture.DRAIN_BATCH_PACKET_LIMIT)
        self.assertEqual(decode_batch(output.getvalue()), frames[:drained])

    def test_empty_nonblocking_socket_is_not_reported_as_a_packet(self) -> None:
        packet_socket = QueuedPacketSocket([])
        output = io.BytesIO()

        self.assertEqual(capture.drain_packet_batch(packet_socket, output), 0)
        self.assertEqual(output.getvalue(), b"")

    def test_short_batch_write_fails_closed(self) -> None:
        packet_socket = QueuedPacketSocket([b"ethernet-frame"])
        output = mock.Mock()
        output.write.return_value = 1

        with self.assertRaisesRegex(capture.CaptureError, "short write"):
            capture.drain_packet_batch(packet_socket, output)


if __name__ == "__main__":
    unittest.main()
