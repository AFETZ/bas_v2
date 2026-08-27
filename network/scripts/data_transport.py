#!/usr/bin/env python3
"""Checksummed logical-message protocol for the non-MAVLink data channel."""

from __future__ import annotations

import binascii
import dataclasses
import struct
import time


MAGIC = b"BDP1"
VERSION = 1
HEADER = struct.Struct("!4sBBBBIQHI")
KINDS = {
    "p2p_downlink": 1,
    "p2p_downlink_ack": 2,
    "p2p_uplink": 3,
    "p2p_uplink_ack": 4,
    "p2mp_downlink": 5,
    "p2mp_ack": 6,
    "simultaneous_uplink": 7,
    "simultaneous_uplink_ack": 8,
}
KIND_NAMES = {value: key for key, value in KINDS.items()}


class DataProtocolError(ValueError):
    """A data-channel logical message is malformed."""


@dataclasses.dataclass(frozen=True)
class DataMessage:
    kind: str
    sender_id: int
    receiver_id: int
    sequence: int
    sent_monotonic_ns: int
    payload: bytes
    checksum: int

    @property
    def logical_id(self) -> str:
        return f"{self.kind}:{self.sender_id}:{self.receiver_id}:{self.sequence}"


def encode(
    kind: str,
    *,
    sender_id: int,
    receiver_id: int,
    sequence: int,
    payload: bytes,
    sent_monotonic_ns: int | None = None,
) -> bytes:
    if kind not in KINDS:
        raise DataProtocolError(f"unknown data message kind: {kind}")
    if not 0 <= sender_id <= 255 or not 0 <= receiver_id <= 255:
        raise DataProtocolError("sender/receiver IDs must be bytes")
    body = bytes(payload)
    if len(body) > 65535:
        raise DataProtocolError("data payload is too large")
    sent_ns = time.monotonic_ns() if sent_monotonic_ns is None else sent_monotonic_ns
    checksum = binascii.crc32(body) & 0xFFFFFFFF
    return HEADER.pack(
        MAGIC,
        VERSION,
        KINDS[kind],
        sender_id,
        receiver_id,
        sequence & 0xFFFFFFFF,
        sent_ns,
        len(body),
        checksum,
    ) + body


def decode(datagram: bytes) -> DataMessage:
    if len(datagram) < HEADER.size:
        raise DataProtocolError("data datagram is shorter than its header")
    magic, version, kind_id, sender, receiver, sequence, sent_ns, length, checksum = (
        HEADER.unpack_from(datagram)
    )
    payload = datagram[HEADER.size:]
    if magic != MAGIC or version != VERSION or kind_id not in KIND_NAMES:
        raise DataProtocolError("data magic/version/kind mismatch")
    if len(payload) != length:
        raise DataProtocolError("data payload length mismatch")
    if (binascii.crc32(payload) & 0xFFFFFFFF) != checksum:
        raise DataProtocolError("data payload checksum mismatch")
    return DataMessage(
        kind=KIND_NAMES[kind_id],
        sender_id=sender,
        receiver_id=receiver,
        sequence=sequence,
        sent_monotonic_ns=sent_ns,
        payload=payload,
        checksum=checksum,
    )
