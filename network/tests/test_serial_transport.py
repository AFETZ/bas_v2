"""Focused tests for the Town01 serial-over-packet transport."""

from __future__ import annotations

import unittest

from network.scripts.serial_transport import (
    Encoder,
    HEADER,
    MavlinkStreamCounter,
    Reassembler,
    TransportCounters,
)


def mavlink2(payload: bytes, sequence: int = 0) -> bytes:
    return bytes((0xFD, len(payload), 0, 0, sequence, 1, 1, 0, 0, 0)) + payload + b"\x00\x00"


class SerialTransportTests(unittest.TestCase):
    def pair(self, max_payload: int = 16, timeout_ms: int = 10):
        counters = TransportCounters()
        return (
            Encoder(
                channel="control",
                uav_id=3,
                direction="uart_to_gcs",
                max_payload=max_payload,
            ),
            Reassembler(
                channel="control",
                uav_id=3,
                direction="uart_to_gcs",
                timeout_ms=timeout_ms,
                counters=counters,
            ),
            counters,
        )

    def test_one_mavlink_frame_split_across_serial_reads(self) -> None:
        encoder, receiver, _ = self.pair()
        frame = mavlink2(b"split-across-reads")
        output = []
        for part in (frame[:3], frame[3:11], frame[11:]):
            for datagram in encoder.encode(part, sent_monotonic_ns=1):
                output.extend(receiver.ingest(datagram, now_ns=2))
        self.assertEqual(b"".join(output), frame)
        parser = MavlinkStreamCounter()
        for part in output:
            parser.feed(part)
        self.assertEqual(parser.snapshot()["frames"], 1)

    def test_multiple_mavlink_frames_in_one_serial_read(self) -> None:
        encoder, receiver, _ = self.pair(max_payload=128)
        stream = mavlink2(b"one") + mavlink2(b"two", sequence=1)
        output = receiver.ingest(encoder.encode(stream, sent_monotonic_ns=1)[0], now_ns=2)
        self.assertEqual(output, [stream])
        parser = MavlinkStreamCounter()
        parser.feed(output[0])
        self.assertEqual(parser.frames, 2)

    def test_record_larger_than_network_payload_is_exactly_reassembled(self) -> None:
        encoder, receiver, counters = self.pair(max_payload=17)
        record = bytes(range(256)) * 3
        chunks = encoder.encode(record, sent_monotonic_ns=1)
        self.assertGreater(len(chunks), 1)
        output = []
        for chunk in chunks:
            self.assertLessEqual(len(chunk), HEADER.size + 17)
            output.extend(receiver.ingest(chunk, now_ns=2))
        self.assertEqual(output, [record])
        self.assertEqual(counters.records_reassembled, 1)

    def test_lost_fragment_discards_only_that_record_and_recovers(self) -> None:
        encoder, receiver, counters = self.pair(max_payload=8, timeout_ms=1)
        first = encoder.encode(b"record-with-a-lost-fragment", sent_monotonic_ns=1)
        second = encoder.encode(b"next-intact-record", sent_monotonic_ns=2)
        output = []
        for chunk in first[:1] + first[2:] + second:
            output.extend(receiver.ingest(chunk, now_ns=10))
        output.extend(receiver.expire(now_ns=2_000_010))
        self.assertEqual(output, [b"next-intact-record"])
        self.assertEqual(counters.incomplete_frames, 1)
        self.assertEqual(counters.reassembly_failures, 1)

    def test_out_of_order_and_duplicate_chunks_do_not_duplicate_serial_bytes(self) -> None:
        encoder, receiver, counters = self.pair(max_payload=5)
        chunks = encoder.encode(b"out-of-order-record", sent_monotonic_ns=1)
        order = [1, 0, 1, *range(2, len(chunks))]
        output = []
        for index in order:
            output.extend(receiver.ingest(chunks[index], now_ns=2))
        self.assertEqual(output, [b"out-of-order-record"])
        self.assertGreater(counters.reordered_chunks, 0)
        self.assertEqual(counters.duplicate_chunks, 1)

    def test_missing_sequence_is_reported_then_following_record_is_released(self) -> None:
        encoder, receiver, counters = self.pair(timeout_ms=1)
        encoder.encode(b"lost-record", sent_monotonic_ns=1)
        later = encoder.encode(b"record-two", sent_monotonic_ns=2)
        output = []
        for chunk in later:
            output.extend(receiver.ingest(chunk, now_ns=10))
        output.extend(receiver.expire(now_ns=2_000_010))
        self.assertEqual(output, [b"record-two"])
        self.assertEqual(counters.sequence_gaps, 1)

    def test_mavlink_counter_recovers_after_corrupt_prefix(self) -> None:
        parser = MavlinkStreamCounter()
        parser.feed(b"\x00\x01corrupt")
        parser.feed(mavlink2(b"valid"))
        snapshot = parser.snapshot()
        self.assertEqual(snapshot["frames"], 1)
        self.assertGreaterEqual(snapshot["discarded_frames"], 1)


if __name__ == "__main__":
    unittest.main()
