#!/usr/bin/env python3
"""Loss-aware framing for opaque serial bytes transported in UDP packets.

The header is outside the serial/MAVLink byte stream.  A receiver writes only
complete, CRC-valid records and emits records in source sequence order.
"""

from __future__ import annotations

import binascii
import dataclasses
import struct
import time
from collections import deque
from typing import Iterable


MAGIC = b"BSF1"
VERSION = 1
CHANNEL_IDS = {"control": 1, "payload": 2}
DIRECTION_IDS = {"uart_to_gcs": 1, "gcs_to_uart": 2}
HEADER = struct.Struct("!4sBBBBIHHHIQII")
MAX_RECORD_BYTES = 1 << 20
MAX_FRAGMENTS = 8192
SEQUENCE_MODULUS = 1 << 32
SEQUENCE_MASK = SEQUENCE_MODULUS - 1
SEQUENCE_HALF_RANGE = SEQUENCE_MODULUS >> 1


def sequence_forward_distance(reference: int, candidate: int) -> int:
    """Return the unsigned 32-bit distance from reference to candidate."""

    return (candidate - reference) & SEQUENCE_MASK


def sequence_is_ahead(candidate: int, reference: int) -> bool:
    """Order sequence numbers within the unambiguous half of their ring."""

    distance = sequence_forward_distance(reference, candidate)
    return 0 < distance < SEQUENCE_HALF_RANGE


class FramingError(ValueError):
    """A transport datagram is malformed or belongs to another path."""


@dataclasses.dataclass(frozen=True)
class Chunk:
    channel_id: int
    uav_id: int
    direction_id: int
    sequence: int
    fragment_index: int
    fragment_count: int
    total_length: int
    sent_monotonic_ns: int
    record_crc32: int
    payload: bytes


@dataclasses.dataclass
class TransportCounters:
    uart_input_bytes: int = 0
    ns3_input_bytes: int = 0
    ns3_output_bytes: int = 0
    uart_output_bytes: int = 0
    records_encoded: int = 0
    records_reassembled: int = 0
    chunks_encoded: int = 0
    chunks_received: int = 0
    frames: int = 0
    incomplete_frames: int = 0
    discarded_frames: int = 0
    sequence_gaps: int = 0
    duplicate_chunks: int = 0
    reordered_chunks: int = 0
    reassembly_failures: int = 0
    malformed_chunks: int = 0
    crc_failures: int = 0
    maximum_ingress_queue_age_ms: float = 0.0
    ingress_queue_age_total_ms: float = 0.0
    ingress_queue_age_samples: int = 0

    def as_dict(self) -> dict[str, int | float]:
        result = dataclasses.asdict(self)
        samples = self.ingress_queue_age_samples
        result["average_ingress_queue_age_ms"] = (
            self.ingress_queue_age_total_ms / samples if samples else 0.0
        )
        return result


class Encoder:
    """Turn each serial read/write record into bounded transport datagrams."""

    def __init__(
        self,
        *,
        channel: str,
        uav_id: int,
        direction: str,
        max_payload: int = 192,
        initial_sequence: int = 0,
    ) -> None:
        if channel not in CHANNEL_IDS:
            raise ValueError(f"unsupported serial channel: {channel}")
        if direction not in DIRECTION_IDS:
            raise ValueError(f"unsupported serial direction: {direction}")
        if not 1 <= uav_id <= 255:
            raise ValueError("uav_id must be in 1..255")
        if not 1 <= max_payload <= 65535:
            raise ValueError("max_payload must be in 1..65535")
        self.channel_id = CHANNEL_IDS[channel]
        self.uav_id = uav_id
        self.direction_id = DIRECTION_IDS[direction]
        self.max_payload = max_payload
        self.sequence = initial_sequence & 0xFFFFFFFF

    def encode(self, data: bytes, sent_monotonic_ns: int | None = None) -> list[bytes]:
        payload = bytes(data)
        if not payload:
            return []
        if len(payload) > MAX_RECORD_BYTES:
            raise ValueError(f"serial record exceeds {MAX_RECORD_BYTES} bytes")
        sent_ns = time.monotonic_ns() if sent_monotonic_ns is None else sent_monotonic_ns
        count = (len(payload) + self.max_payload - 1) // self.max_payload
        if count > MAX_FRAGMENTS:
            raise ValueError("serial record requires too many fragments")
        sequence = self.sequence
        self.sequence = (self.sequence + 1) & 0xFFFFFFFF
        record_crc = binascii.crc32(payload) & 0xFFFFFFFF
        result: list[bytes] = []
        for index in range(count):
            fragment = payload[index * self.max_payload : (index + 1) * self.max_payload]
            fragment_crc = binascii.crc32(fragment) & 0xFFFFFFFF
            result.append(
                HEADER.pack(
                    MAGIC,
                    VERSION,
                    self.channel_id,
                    self.uav_id,
                    self.direction_id,
                    sequence,
                    index,
                    count,
                    len(fragment),
                    len(payload),
                    sent_ns,
                    record_crc,
                    fragment_crc,
                )
                + fragment
            )
        return result


def decode_chunk(data: bytes) -> Chunk:
    if len(data) < HEADER.size:
        raise FramingError("transport datagram is shorter than its header")
    (
        magic,
        version,
        channel_id,
        uav_id,
        direction_id,
        sequence,
        fragment_index,
        fragment_count,
        payload_length,
        total_length,
        sent_ns,
        record_crc,
        fragment_crc,
    ) = HEADER.unpack_from(data)
    payload = data[HEADER.size:]
    if magic != MAGIC or version != VERSION:
        raise FramingError("transport magic/version mismatch")
    if channel_id not in CHANNEL_IDS.values() or direction_id not in DIRECTION_IDS.values():
        raise FramingError("unknown channel or direction")
    if not 1 <= uav_id <= 255:
        raise FramingError("invalid UAV ID")
    if fragment_count < 1 or fragment_count > MAX_FRAGMENTS:
        raise FramingError("invalid fragment count")
    if fragment_index >= fragment_count:
        raise FramingError("fragment index is outside the record")
    if payload_length != len(payload):
        raise FramingError("fragment payload length mismatch")
    if total_length < 1 or total_length > MAX_RECORD_BYTES or payload_length > total_length:
        raise FramingError("invalid record length")
    if (binascii.crc32(payload) & 0xFFFFFFFF) != fragment_crc:
        raise FramingError("fragment CRC mismatch")
    return Chunk(
        channel_id=channel_id,
        uav_id=uav_id,
        direction_id=direction_id,
        sequence=sequence,
        fragment_index=fragment_index,
        fragment_count=fragment_count,
        total_length=total_length,
        sent_monotonic_ns=sent_ns,
        record_crc32=record_crc,
        payload=payload,
    )


@dataclasses.dataclass
class _Record:
    fragment_count: int
    total_length: int
    sent_monotonic_ns: int
    record_crc32: int
    first_seen_ns: int
    fragments: dict[int, bytes] = dataclasses.field(default_factory=dict)


class Reassembler:
    """Reassemble fragments and restore ordered serial stream records."""

    def __init__(
        self,
        *,
        channel: str,
        uav_id: int,
        direction: str,
        timeout_ms: int = 500,
        initial_sequence: int = 0,
        counters: TransportCounters | None = None,
        max_buffer_records: int = 256,
        max_buffer_bytes: int = 1048576,
        max_age_ms: float | None = None,
    ) -> None:
        if channel not in CHANNEL_IDS or direction not in DIRECTION_IDS:
            raise ValueError("unknown serial channel or direction")
        if timeout_ms < 1:
            raise ValueError("timeout_ms must be positive")
        self.channel_id = CHANNEL_IDS[channel]
        self.uav_id = uav_id
        self.direction_id = DIRECTION_IDS[direction]
        self.timeout_ns = timeout_ms * 1_000_000
        if max_buffer_records < 1 or max_buffer_bytes < 1 or (max_age_ms is not None and max_age_ms <= 0):
            raise ValueError("reassembly bounds must be positive")
        self.max_buffer_records = max_buffer_records
        self.max_buffer_bytes = max_buffer_bytes
        self.max_age_ms = max_age_ms
        self.expected_sequence = initial_sequence & 0xFFFFFFFF
        self.counters = counters or TransportCounters()
        self._records: dict[int, _Record] = {}
        self._complete: dict[int, tuple[bytes, int, int]] = {}
        self._recent_delivered: deque[int] = deque(maxlen=4096)
        self._recent_set: set[int] = set()

    def ingest(self, datagram: bytes, now_ns: int | None = None) -> list[bytes]:
        observed_ns = time.monotonic_ns() if now_ns is None else now_ns
        self.counters.ns3_output_bytes += len(datagram)
        try:
            chunk = decode_chunk(datagram)
        except FramingError as exc:
            self.counters.malformed_chunks += 1
            if "CRC" in str(exc):
                self.counters.crc_failures += 1
            return self.expire(observed_ns)
        if (
            chunk.channel_id != self.channel_id
            or chunk.uav_id != self.uav_id
            or chunk.direction_id != self.direction_id
        ):
            self.counters.malformed_chunks += 1
            return self.expire(observed_ns)
        self.counters.chunks_received += 1
        buffered_bytes = sum(len(p) for r in self._records.values() for p in r.fragments.values())
        buffered_bytes += sum(len(p) for p, _, _ in self._complete.values())
        new_record = chunk.sequence not in self._records and chunk.sequence not in self._complete
        if (chunk.total_length > self.max_buffer_bytes or buffered_bytes + len(chunk.payload) > self.max_buffer_bytes
                or (new_record and len(self._records)+len(self._complete) >= self.max_buffer_records)):
            self.counters.discarded_frames += 1
            return self.expire(observed_ns)
        if chunk.sequence in self._recent_set or (
            chunk.sequence != self.expected_sequence
            and not sequence_is_ahead(chunk.sequence, self.expected_sequence)
        ):
            self.counters.duplicate_chunks += 1
            return self.expire(observed_ns)
        if sequence_is_ahead(chunk.sequence, self.expected_sequence):
            self.counters.reordered_chunks += 1
        record = self._records.get(chunk.sequence)
        if record is None:
            record = _Record(
                fragment_count=chunk.fragment_count,
                total_length=chunk.total_length,
                sent_monotonic_ns=chunk.sent_monotonic_ns,
                record_crc32=chunk.record_crc32,
                first_seen_ns=observed_ns,
            )
            self._records[chunk.sequence] = record
        elif (
            record.fragment_count != chunk.fragment_count
            or record.total_length != chunk.total_length
            or record.record_crc32 != chunk.record_crc32
            or record.sent_monotonic_ns != chunk.sent_monotonic_ns
        ):
            self.counters.reassembly_failures += 1
            self.counters.discarded_frames += 1
            del self._records[chunk.sequence]
            return self.expire(observed_ns)
        previous = record.fragments.get(chunk.fragment_index)
        if previous is not None:
            self.counters.duplicate_chunks += 1
            if previous != chunk.payload:
                self.counters.reassembly_failures += 1
            return self.expire(observed_ns)
        if chunk.fragment_index != len(record.fragments):
            self.counters.reordered_chunks += 1
        record.fragments[chunk.fragment_index] = chunk.payload
        if len(record.fragments) == record.fragment_count:
            payload = b"".join(record.fragments[index] for index in range(record.fragment_count))
            del self._records[chunk.sequence]
            if len(payload) != record.total_length or (
                binascii.crc32(payload) & 0xFFFFFFFF
            ) != record.record_crc32:
                self.counters.crc_failures += 1
                self.counters.reassembly_failures += 1
                self.counters.discarded_frames += 1
            else:
                self._complete[chunk.sequence] = (
                    payload,
                    record.sent_monotonic_ns,
                    record.first_seen_ns,
                )
        return self._drain(observed_ns) + self.expire(observed_ns)

    def expire(self, now_ns: int | None = None, *, force: bool = False) -> list[bytes]:
        observed_ns = time.monotonic_ns() if now_ns is None else now_ns
        output: list[bytes] = []
        while True:
            output.extend(self._drain(observed_ns))
            if self.expected_sequence in self._records:
                record = self._records[self.expected_sequence]
                if not force and observed_ns - record.first_seen_ns < self.timeout_ns:
                    break
                del self._records[self.expected_sequence]
                self.counters.incomplete_frames += 1
                self.counters.discarded_frames += 1
                self.counters.reassembly_failures += 1
                self._advance_expected()
                continue
            higher_times = [
                seen
                for sequence, (_payload, _sent, seen) in self._complete.items()
                if sequence_is_ahead(sequence, self.expected_sequence)
            ] + [
                record.first_seen_ns
                for sequence, record in self._records.items()
                if sequence_is_ahead(sequence, self.expected_sequence)
            ]
            if not higher_times or (not force and observed_ns - min(higher_times) < self.timeout_ns):
                break
            next_sequence = min(
                (
                    sequence
                    for sequence in (*self._complete.keys(), *self._records.keys())
                    if sequence_is_ahead(sequence, self.expected_sequence)
                ),
                key=lambda sequence: sequence_forward_distance(
                    self.expected_sequence, sequence
                ),
            )
            gap = sequence_forward_distance(self.expected_sequence, next_sequence)
            self.counters.sequence_gaps += gap
            self.counters.reassembly_failures += gap
            self.counters.discarded_frames += gap
            self.expected_sequence = next_sequence
        if force:
            while self._records:
                next_sequence = min(
                    self._records,
                    key=lambda sequence: sequence_forward_distance(
                        self.expected_sequence, sequence
                    ),
                )
                if next_sequence != self.expected_sequence:
                    gap = sequence_forward_distance(self.expected_sequence, next_sequence)
                    self.counters.sequence_gaps += gap
                    self.counters.reassembly_failures += gap
                    self.counters.discarded_frames += gap
                    self.expected_sequence = next_sequence
                record = self._records.pop(next_sequence)
                if len(record.fragments) != record.fragment_count:
                    self.counters.incomplete_frames += 1
                    self.counters.discarded_frames += 1
                    self.counters.reassembly_failures += 1
                self._advance_expected()
                output.extend(self._drain(observed_ns))
        return output

    def _drain(self, now_ns: int) -> list[bytes]:
        result: list[bytes] = []
        while self.expected_sequence in self._complete:
            payload, sent_ns, _first_seen = self._complete.pop(self.expected_sequence)
            age_ms = max(0.0, (now_ns - sent_ns) / 1e6)
            if self.max_age_ms is not None and age_ms > self.max_age_ms:
                self.counters.discarded_frames += 1
                self._advance_expected()
                continue
            self.counters.records_reassembled += 1
            self.counters.maximum_ingress_queue_age_ms = max(
                self.counters.maximum_ingress_queue_age_ms, age_ms
            )
            self.counters.ingress_queue_age_total_ms += age_ms
            self.counters.ingress_queue_age_samples += 1
            result.append(payload)
            self._advance_expected()
        return result

    def _advance_expected(self) -> None:
        sequence = self.expected_sequence
        if len(self._recent_delivered) == self._recent_delivered.maxlen:
            self._recent_set.discard(self._recent_delivered[0])
        self._recent_delivered.append(sequence)
        self._recent_set.add(sequence)
        self.expected_sequence = (sequence + 1) & 0xFFFFFFFF


class MavlinkStreamCounter:
    """Count intact MAVLink v1/v2 frames without changing the byte stream."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self.frames = 0
        self.discarded_frames = 0
        self.discarded_bytes = 0

    @property
    def incomplete_frames(self) -> int:
        return int(bool(self._buffer))

    def feed(self, data: bytes) -> int:
        self._buffer.extend(data)
        before = self.frames
        while self._buffer:
            try:
                offset = min(
                    value
                    for value in (
                        self._buffer.find(b"\xfe"),
                        self._buffer.find(b"\xfd"),
                    )
                    if value >= 0
                )
            except ValueError:
                self.discarded_bytes += len(self._buffer)
                self.discarded_frames += 1
                self._buffer.clear()
                break
            if offset:
                del self._buffer[:offset]
                self.discarded_bytes += offset
                self.discarded_frames += 1
            if len(self._buffer) < 2:
                break
            if self._buffer[0] == 0xFE:
                frame_length = 6 + self._buffer[1] + 2
            else:
                if len(self._buffer) < 3:
                    break
                frame_length = 10 + self._buffer[1] + 2 + (13 if self._buffer[2] & 1 else 0)
            if len(self._buffer) < frame_length:
                break
            del self._buffer[:frame_length]
            self.frames += 1
        return self.frames - before

    def snapshot(self) -> dict[str, int]:
        return {
            "frames": self.frames,
            "incomplete_frames": self.incomplete_frames,
            "discarded_frames": self.discarded_frames,
            "discarded_bytes": self.discarded_bytes,
        }


def encode_records(encoder: Encoder, records: Iterable[bytes]) -> list[bytes]:
    """Test/helper API that preserves record order."""

    return [chunk for record in records for chunk in encoder.encode(record)]
