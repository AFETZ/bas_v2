"""Focused tests for checksummed non-MAVLink logical messages."""

from __future__ import annotations

import unittest

from network.scripts.data_transport import DataProtocolError, decode, encode


class DataTransportTests(unittest.TestCase):
    def test_sequence_timestamp_sender_and_checksum_round_trip(self) -> None:
        wire = encode(
            "p2mp_downlink",
            sender_id=0,
            receiver_id=0,
            sequence=7,
            payload=b"same logical message",
            sent_monotonic_ns=123,
        )
        message = decode(wire)
        self.assertEqual(message.sequence, 7)
        self.assertEqual(message.sent_monotonic_ns, 123)
        self.assertEqual(message.sender_id, 0)
        self.assertEqual(message.logical_id, "p2mp_downlink:0:0:7")

    def test_corrupt_payload_fails_checksum(self) -> None:
        wire = bytearray(
            encode(
                "p2p_uplink",
                sender_id=3,
                receiver_id=0,
                sequence=1,
                payload=b"payload",
            )
        )
        wire[-1] ^= 1
        with self.assertRaises(DataProtocolError):
            decode(bytes(wire))


if __name__ == "__main__":
    unittest.main()
