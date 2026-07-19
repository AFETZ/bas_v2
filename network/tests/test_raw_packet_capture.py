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
