#!/usr/bin/env python3
"""Produce external-endpoint M3 traffic without declaring acceptance.

Companion endpoint agents carry only payload and additional-data traffic.  A
separate ground-side actual-control process owns the sole GCS control socket,
sends valid requests, and records only MAVLink replies emitted by the five
live ArduPilot SITLs through the strict M2-derived adapters.  Acceptance belongs
exclusively to ``validate_m3_external_matrix.py``.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import select
import socket
import struct
import sys
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MATRIX = ROOT / "network/config/endpoint_matrix_5uav.json"
RUN_CONTRACT = "ams.m3.external_matrix_run/v1"
PHASE_CONTRACT = "ams.m3.external_matrix_phase/v1"
ENDPOINT_EVENT_SCHEMA = "ams.m3.endpoint_event/v1"
ACTUAL_CONTROL_EVENT_SCHEMA = "ams.m3.actual_control_event/v1"
ACTUAL_CONTROL_LINEAGE_CONTRACT = "ams.m3.actual_control_lineage/v1"
ACTUAL_CONTROL_ENDPOINT_FORM = "actual_sitl_mavproxy_udp_tail"
RESOLVED_FLIGHT_CONTRACT = "ams.m3.resolved_flight_scenario/v1"
LIFECYCLE_EVENT_SCHEMA = "ams.m3.lifecycle_event/v1"
FORBIDDEN_CONTRACT = "ams.m3.forbidden_canary_contract/v1"
FORBIDDEN_RESULT_CONTRACT = "ams.m3.forbidden_canary_observation/v1"
FORBIDDEN_LISTENER_SCHEMA = "ams.m3.forbidden_listener_event/v1"
M2_RECEIPT_CONTRACT = "ams.m2.host-final-receipt/v1"
M2_RESULT_CONTRACT = "ams.m2.vertical-slice-validation/v2"
M2_EVIDENCE_CONTRACT = "ams.m2.vertical_slice/v2"
M2_EXTENSION_CONTRACT = "ams.m2-to-m3.shared-packet-core/v1"
ENGINE_LIFECYCLE_SCHEMA = "ams.ns3.lifecycle/v1"
ENGINE_LIFECYCLE_MANIFEST_CONTRACT = "ams.m3.packet-engine-lifecycle-manifest/v1"
ENGINE_LIFECYCLE_EVENTS = (
    "ready",
    "stop_observed",
    "queues_terminal",
    "stopped",
)
M2_REQUIRED_GATES = frozenset(
    {
        "metadata",
        "probe_transactions",
        "lifecycle",
        "lifecycle_monitor",
        "endpoint_contract",
        "ns3_build_receipt",
        "packet_engine",
        "packet_engine_lifecycle",
        "packet_captures",
        "capture_accounting",
        "adapter_path",
        "process_identity",
        "critical_logs",
        "provenance",
        "manifest",
    }
)
ENDPOINTS = ("gcs", "uav1", "uav2", "uav3", "uav4", "uav5")
NAMESPACES = {
    "gcs": "ams-gcs",
    **{f"uav{index}": f"ams-uav{index}" for index in range(1, 6)},
}
TRAFFIC_CLASSES = ("control", "payload", "additional_data")
COMPANION_TRAFFIC_CLASSES = ("payload", "additional_data")
DIRECTIONS = ("downlink", "uplink")
TOS_BY_CLASS = {"control": 184, "payload": 40, "additional_data": 0}
PHASE_CODES = {"positive": 1, "stopped": 2, "recovery": 3, "p2mp": 4}
CLASS_CODES = {name: index + 1 for index, name in enumerate(TRAFFIC_CLASSES)}
DIRECTION_CODES = {"downlink": 1, "uplink": 2}
P2MP_GROUP = "239.71.0.1"
P2MP_PORT = 14900
MAVLINK_CRC_EXTRA = {
    0: 50,
    33: 104,
    76: 152,
    77: 143,
    148: 178,
    253: 83,
    385: 147,
}
STREAM_RECORD = struct.Struct(">4sBBBBBBH16sQ16s16s")
FORBIDDEN_RECORD = struct.Struct(">4sBBH16s32s")
STREAM_MAGIC = b"AMU1"
ADDITIONAL_MAGIC = b"AMSD"
FORBIDDEN_MAGIC = b"AMFC"
HEX32 = re.compile(r"^[0-9a-f]{32}$")
SAFE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
FORBIDDEN_KIND_CODES = {
    "loopback_ipv4": 1,
    "loopback_ipv6": 2,
    "legacy_direct_port": 3,
    "unreachable_ipv4": 4,
}
ENDPOINT_PUMP_DATAGRAM_LIMIT = 64


class ProbeError(RuntimeError):
    """The producer cannot safely continue."""


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def strict_json(path: Path) -> dict[str, Any]:
    def reject_duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ProbeError(f"duplicate JSON key {key!r} in {path}")
            result[key] = value
        return result

    try:
        value = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicate
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProbeError(f"cannot read strict JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ProbeError(f"JSON root is not an object: {path}")
    return value


def write_exclusive(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(canonical_json(value))
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


def write_bytes_exclusive(path: Path, payload: bytes, *, mode: int = 0o400) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
        os.fchmod(descriptor, mode)
    finally:
        os.close(descriptor)


def load_m2_predecessor(path: Path) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o222:
        raise ProbeError("M2 prerequisite receipt is absent, linked, or writable")
    payload = path.read_bytes()
    receipt = strict_json(path)
    result = receipt.get("result")
    gates = result.get("gates") if isinstance(result, dict) else None
    if (
        receipt.get("schema_version") != 1
        or receipt.get("contract") != M2_RECEIPT_CONTRACT
        or receipt.get("profile") != "m2_component"
        or receipt.get("formal_accepted") is not True
        or receipt.get("passed") is not True
        or receipt.get("failures") != []
        or receipt.get("result_contract") != M2_RESULT_CONTRACT
        or not isinstance(result, dict)
        or result.get("contract") != M2_RESULT_CONTRACT
        or result.get("validation_contract") != M2_EVIDENCE_CONTRACT
        or result.get("passed") is not True
        or result.get("failures") != []
        or not isinstance(gates, dict)
        or set(gates) != M2_REQUIRED_GATES
        or any(
            not isinstance(gate, dict)
            or gate.get("status") != "passed"
            or gate.get("failures") != []
            for gate in gates.values()
        )
        or not isinstance(result.get("packet_engine"), dict)
        or not isinstance(result.get("endpoint_transaction"), dict)
    ):
        raise ProbeError("M2 prerequisite receipt/result is not formally accepted")
    result_payload = (
        json.dumps(result, allow_nan=False, indent=2, sort_keys=True) + "\n"
    ).encode()
    if sha256_bytes(result_payload) != receipt.get("result_sha256"):
        raise ProbeError("embedded M2 result does not match its receipt hash")
    summary = {
        "contract": M2_EXTENSION_CONTRACT,
        "receipt": {
            "external_path": str(path),
            "raw_copy_path": "raw/m2_component_host_final_receipt.json",
            "canonical_path": receipt.get("receipt_path"),
            "sha256": sha256_bytes(payload),
            "run_id": receipt.get("run_id"),
            "source_commit": receipt.get("source_commit"),
            "result_sha256": receipt.get("result_sha256"),
        },
        "packet_engine": result["packet_engine"],
        "endpoint_transaction": result["endpoint_transaction"],
    }
    return summary, payload


def engine_lifecycle_manifest() -> dict[str, Any]:
    """Return the immutable M3 declaration for both raw engine lifecycles."""

    return {
        "contract": ENGINE_LIFECYCLE_MANIFEST_CONTRACT,
        "schema": ENGINE_LIFECYCLE_SCHEMA,
        "events": list(ENGINE_LIFECYCLE_EVENTS),
        "epochs": [
            {
                "event_epoch": epoch,
                "path": f"logs/ns3_epoch{epoch}.lifecycle.jsonl",
            }
            for epoch in (1, 2)
        ],
    }


def endpoint_ip(endpoint: str) -> str:
    return "10.71.0.10" if endpoint == "gcs" else f"10.71.{int(endpoint[3:])}.10"


def endpoint_uav(endpoint: str) -> int:
    return 0 if endpoint == "gcs" else int(endpoint[3:])


def flow_id(uav: int, traffic_class: str, direction: str, *, p2mp: bool = False) -> str:
    if p2mp:
        return "p2mp.additional_data.downlink"
    return f"uav{uav}.{traffic_class}.{direction}"


def record_nonce(
    run_nonce: str,
    phase: str,
    identity: str,
    sequence: int,
    sent_monotonic_ns: int,
) -> bytes:
    material = (
        bytes.fromhex(run_nonce)
        + bytes([PHASE_CODES[phase]])
        + identity.encode("ascii")
        + sequence.to_bytes(2, "big")
        + sent_monotonic_ns.to_bytes(8, "big")
    )
    return hashlib.sha256(material).digest()[:16]


def make_stream_record(
    *,
    run_nonce: str,
    phase: str,
    traffic_class: str,
    direction: str,
    uav: int,
    sequence: int,
    sent_monotonic_ns: int,
    p2mp: bool = False,
) -> bytes:
    identity = flow_id(uav, traffic_class, direction, p2mp=p2mp)
    return STREAM_RECORD.pack(
        STREAM_MAGIC,
        1,
        PHASE_CODES[phase],
        CLASS_CODES[traffic_class],
        DIRECTION_CODES[direction],
        uav,
        1 if p2mp else 0,
        sequence,
        bytes.fromhex(run_nonce),
        sent_monotonic_ns,
        hashlib.sha256(identity.encode("ascii")).digest()[:16],
        record_nonce(run_nonce, phase, identity, sequence, sent_monotonic_ns),
    )


def make_forbidden_payload(
    *, run_nonce: str, canary_id: str, kind: str, sequence: int
) -> bytes:
    try:
        kind_code = FORBIDDEN_KIND_CODES[kind]
    except KeyError as exc:
        raise ProbeError(f"unknown forbidden canary kind: {kind}") from exc
    body = FORBIDDEN_RECORD.pack(
        FORBIDDEN_MAGIC,
        1,
        kind_code,
        sequence,
        bytes.fromhex(run_nonce),
        hashlib.sha256(canary_id.encode("ascii")).digest(),
    )
    return body + zlib.crc32(body).to_bytes(4, "big")


def forbidden_canaries(run_nonce: str) -> list[dict[str, Any]]:
    capture_names = {
        *(f"endpoint-{endpoint}.pcap" for endpoint in ENDPOINTS),
        *(f"ns3-external-{endpoint}.pcap" for endpoint in ENDPOINTS),
        *(f"loopback-{endpoint}.pcap" for endpoint in ENDPOINTS),
        *(f"tail-uav{index}.pcap" for index in range(1, 6)),
        *(f"tail-root-uav{index}.pcap" for index in range(1, 6)),
        "loopback-container-root.pcap",
    }
    canaries: list[dict[str, Any]] = []

    def add(
        *,
        canary_id: str,
        kind: str,
        source_endpoint: str,
        source_namespace: str,
        address_family: str,
        source_ip: str,
        source_port: int,
        destination_ip: str,
        destination_port: int,
        listener_endpoint: str,
        expected_capture_names: list[str],
    ) -> None:
        sequence = len(canaries) + 1
        payload = make_forbidden_payload(
            run_nonce=run_nonce,
            canary_id=canary_id,
            kind=kind,
            sequence=sequence,
        )
        expected = sorted(expected_capture_names)
        canaries.append(
            {
                "canary_id": canary_id,
                "kind": kind,
                "sequence": sequence,
                "source_endpoint": source_endpoint,
                "source_namespace": source_namespace,
                "address_family": address_family,
                "source_ip": source_ip,
                "source_udp_port": source_port,
                "destination_ip": destination_ip,
                "destination_udp_port": destination_port,
                "listener_endpoint": listener_endpoint,
                "listener_namespace": NAMESPACES[listener_endpoint],
                "tos": 0,
                "transport_payload_hex": payload.hex(),
                "transport_payload_sha256": sha256_bytes(payload),
                "transport_payload_size": len(payload),
                "expected_capture_names": expected,
                "forbidden_capture_names": sorted(capture_names - set(expected)),
                "expected_send_return_size": len(payload),
                "remote_application_delivery": "forbidden_zero",
            }
        )

    loopback_sources = (
        ("container-root", "container-root", "container-root"),
        *((endpoint, NAMESPACES[endpoint], endpoint) for endpoint in ENDPOINTS),
    )
    for index, (source, namespace, capture_suffix) in enumerate(loopback_sources):
        listener = "uav1" if source == "gcs" else "gcs"
        add(
            canary_id=f"loopback_ipv4.{source}",
            kind="loopback_ipv4",
            source_endpoint=source,
            source_namespace=namespace,
            address_family="ipv4",
            source_ip="127.0.0.1",
            source_port=15400 + index,
            destination_ip="127.0.0.1",
            destination_port=15500 + index,
            listener_endpoint=listener,
            expected_capture_names=[f"loopback-{capture_suffix}.pcap"],
        )
        add(
            canary_id=f"loopback_ipv6.{source}",
            kind="loopback_ipv6",
            source_endpoint=source,
            source_namespace=namespace,
            address_family="ipv6",
            source_ip="::1",
            source_port=15600 + index,
            destination_ip="::1",
            destination_port=15700 + index,
            listener_endpoint=listener,
            expected_capture_names=[f"loopback-{capture_suffix}.pcap"],
        )
    for index in range(1, 6):
        add(
            canary_id=f"legacy_direct_port.uav{index}",
            kind="legacy_direct_port",
            source_endpoint="gcs",
            source_namespace="ams-gcs",
            address_family="ipv4",
            source_ip="10.71.0.10",
            source_port=15200 + index,
            destination_ip=f"10.71.{index}.10",
            destination_port=14550,
            listener_endpoint=f"uav{index}",
            expected_capture_names=[
                "endpoint-gcs.pcap",
                "ns3-external-gcs.pcap",
            ],
        )
    add(
        canary_id="unreachable_ipv4.gcs",
        kind="unreachable_ipv4",
        source_endpoint="gcs",
        source_namespace="ams-gcs",
        address_family="ipv4",
        source_ip="10.71.0.10",
        source_port=15301,
        destination_ip="198.18.0.1",
        destination_port=15300,
        listener_endpoint="uav1",
        expected_capture_names=["endpoint-gcs.pcap", "ns3-external-gcs.pcap"],
    )
    return canaries


def decode_stream_record(payload: bytes) -> dict[str, Any]:
    if len(payload) != STREAM_RECORD.size:
        raise ProbeError(
            f"stream record length {len(payload)} is not {STREAM_RECORD.size}"
        )
    (
        magic,
        version,
        phase_code,
        class_code,
        direction_code,
        uav,
        flags,
        sequence,
        nonce_bytes,
        sent_ns,
        observed_flow_hash,
        observed_record_nonce,
    ) = STREAM_RECORD.unpack(payload)
    reverse_phase = {value: key for key, value in PHASE_CODES.items()}
    reverse_class = {value: key for key, value in CLASS_CODES.items()}
    reverse_direction = {value: key for key, value in DIRECTION_CODES.items()}
    if magic != STREAM_MAGIC or version != 1:
        raise ProbeError("stream record magic/version mismatch")
    if phase_code not in reverse_phase or class_code not in reverse_class:
        raise ProbeError("stream record phase/class code is unknown")
    if direction_code not in reverse_direction or flags not in (0, 1):
        raise ProbeError("stream record direction/flags is invalid")
    phase = reverse_phase[phase_code]
    traffic_class = reverse_class[class_code]
    direction = reverse_direction[direction_code]
    p2mp = flags == 1
    identity = flow_id(uav, traffic_class, direction, p2mp=p2mp)
    run_nonce = nonce_bytes.hex()
    if observed_flow_hash != hashlib.sha256(identity.encode("ascii")).digest()[:16]:
        raise ProbeError("stream record flow hash mismatch")
    expected_nonce = record_nonce(run_nonce, phase, identity, sequence, sent_ns)
    if observed_record_nonce != expected_nonce:
        raise ProbeError("stream record nonce mismatch")
    return {
        "phase": phase,
        "traffic_class": traffic_class,
        "direction": direction,
        "uav": uav,
        "sequence": sequence,
        "run_nonce": run_nonce,
        "sent_monotonic_ns": sent_ns,
        "flow_id": identity,
        "record_nonce": observed_record_nonce.hex(),
        "p2mp": p2mp,
        "application_unit_sha256": sha256_bytes(payload),
    }


def x25_crc(payload: bytes) -> int:
    crc = 0xFFFF
    for byte in payload:
        temporary = byte ^ (crc & 0xFF)
        temporary ^= (temporary << 4) & 0xFF
        crc = (
            (crc >> 8)
            ^ ((temporary << 8) & 0xFFFF)
            ^ ((temporary << 3) & 0xFFFF)
            ^ (temporary >> 4)
        ) & 0xFFFF
    return crc


def mavlink_v2_frame(
    message_id: int,
    payload: bytes,
    *,
    sequence: int,
    system_id: int,
    component_id: int,
) -> bytes:
    if message_id not in MAVLINK_CRC_EXTRA or len(payload) > 255:
        raise ProbeError(f"unsupported MAVLink message {message_id}")
    header = bytes(
        [len(payload), 0, 0, sequence & 0xFF, system_id, component_id]
    ) + message_id.to_bytes(3, "little")
    checksum = x25_crc(header + payload + bytes([MAVLINK_CRC_EXTRA[message_id]]))
    return b"\xfd" + header + payload + checksum.to_bytes(2, "little")


def parse_mavlink_v2(payload: bytes) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    offset = 0
    while offset < len(payload):
        if len(payload) - offset < 12 or payload[offset] != 0xFD:
            raise ProbeError("transport payload is not contiguous unsigned MAVLink v2")
        length = payload[offset + 1]
        frame_length = 12 + length
        if offset + frame_length > len(payload):
            raise ProbeError("truncated MAVLink v2 frame")
        frame = payload[offset : offset + frame_length]
        if frame[2] != 0 or frame[3] != 0:
            raise ProbeError("signed/flagged MAVLink frame is outside the M3 contract")
        message_id = int.from_bytes(frame[7:10], "little")
        if message_id not in MAVLINK_CRC_EXTRA:
            raise ProbeError(f"unexpected MAVLink message id {message_id}")
        expected = x25_crc(frame[1:-2] + bytes([MAVLINK_CRC_EXTRA[message_id]]))
        if int.from_bytes(frame[-2:], "little") != expected:
            raise ProbeError("MAVLink checksum mismatch")
        frames.append(
            {
                "message_id": message_id,
                "mavlink_sequence": frame[4],
                "system_id": frame[5],
                "component_id": frame[6],
                "payload": frame[10:-2],
                "mavlink_frame_sha256": sha256_bytes(frame),
            }
        )
        offset += frame_length
    return frames


def marker_text(
    *, run_nonce: str, phase: str, direction: str, uav: int, sequence: int
) -> str:
    base = (
        f"AMS3{run_nonce}{PHASE_CODES[phase]:x}{CLASS_CODES['control']:x}"
        f"{DIRECTION_CODES[direction]:x}{uav:x}{sequence:04x}"
    )
    return base + hashlib.sha256(base.encode("ascii")).hexdigest()[:6]


def decode_marker(value: str) -> dict[str, Any]:
    if len(value) != 50 or not value.startswith("AMS3"):
        raise ProbeError("control marker has wrong length/prefix")
    base, checksum = value[:44], value[44:]
    if hashlib.sha256(base.encode("ascii")).hexdigest()[:6] != checksum:
        raise ProbeError("control marker checksum mismatch")
    run_nonce = value[4:36]
    if not HEX32.fullmatch(run_nonce):
        raise ProbeError("control marker run nonce is invalid")
    reverse_phase = {value: key for key, value in PHASE_CODES.items()}
    reverse_direction = {value: key for key, value in DIRECTION_CODES.items()}
    phase_code = int(value[36], 16)
    class_code = int(value[37], 16)
    direction_code = int(value[38], 16)
    uav = int(value[39], 16)
    sequence = int(value[40:44], 16)
    if phase_code not in reverse_phase or class_code != CLASS_CODES["control"]:
        raise ProbeError("control marker phase/class is invalid")
    if direction_code not in reverse_direction or not 1 <= uav <= 5:
        raise ProbeError("control marker direction/UAV is invalid")
    phase = reverse_phase[phase_code]
    direction = reverse_direction[direction_code]
    return {
        "phase": phase,
        "traffic_class": "control",
        "direction": direction,
        "uav": uav,
        "sequence": sequence,
        "run_nonce": run_nonce,
        "flow_id": flow_id(uav, "control", direction),
        "record_nonce": sha256_bytes(value.encode("ascii")),
        "p2mp": False,
        "application_unit_sha256": sha256_bytes(value.encode("ascii")),
    }


@dataclass
class MavlinkSequencer:
    value: int = 0

    def frame(
        self, message_id: int, payload: bytes, system_id: int, component_id: int
    ) -> bytes:
        frame = mavlink_v2_frame(
            message_id,
            payload,
            sequence=self.value,
            system_id=system_id,
            component_id=component_id,
        )
        self.value = (self.value + 1) & 0xFF
        return frame


def encode_transport_unit(
    *,
    run_nonce: str,
    phase: str,
    cell: dict[str, Any] | None,
    sequence: int,
    sent_monotonic_ns: int,
    mavlink: MavlinkSequencer,
    p2mp: bool = False,
) -> tuple[bytes, dict[str, Any]]:
    if p2mp:
        traffic_class, direction, uav = "additional_data", "downlink", 0
        record = make_stream_record(
            run_nonce=run_nonce,
            phase=phase,
            traffic_class=traffic_class,
            direction=direction,
            uav=uav,
            sequence=sequence,
            sent_monotonic_ns=sent_monotonic_ns,
            p2mp=True,
        )
        payload = ADDITIONAL_MAGIC + len(record).to_bytes(2, "big") + record
        payload += zlib.crc32(record).to_bytes(4, "big")
        decoded = decode_stream_record(record)
        decoded["protocol_family"] = "AMS_ADDITIONAL_DATA_V1"
    else:
        if cell is None:
            raise ProbeError("unicast transport unit has no matrix cell")
        traffic_class = str(cell["traffic_class"])
        direction = str(cell["direction"])
        uav = int(cell["uav"]["system_id"])
        source_system = cell["source"]["mavlink_system_id"]
        source_component = cell["source"]["mavlink_component_id"]
        target_system = cell["destination"]["mavlink_system_id"]
        target_component = cell["destination"]["mavlink_component_id"]
        if traffic_class == "control":
            marker = marker_text(
                run_nonce=run_nonce,
                phase=phase,
                direction=direction,
                uav=uav,
                sequence=sequence,
            )
            marker_payload = struct.pack("<B50s", 6, marker.encode("ascii"))
            frames = [
                mavlink.frame(253, marker_payload, source_system, source_component)
            ]
            if direction == "downlink":
                command = struct.pack(
                    "<7fHBBB",
                    33.0,
                    0.0,
                    0.0,
                    0.0,
                    0.0,
                    0.0,
                    0.0,
                    512,
                    target_system,
                    target_component,
                    0,
                )
                frames.append(
                    mavlink.frame(76, command, source_system, source_component)
                )
            else:
                ack = struct.pack("<HBBiBB", 512, 0, 100, sequence, 255, 190)
                heartbeat = struct.pack("<IBBBBB", 0, 2, 3, 0, 4, 3)
                position = struct.pack(
                    "<IiiiihhhH", sequence, 0, 0, 0, 0, 0, 0, 0, 65535
                )
                frames.extend(
                    (
                        mavlink.frame(77, ack, source_system, source_component),
                        mavlink.frame(0, heartbeat, source_system, source_component),
                        mavlink.frame(33, position, source_system, source_component),
                    )
                )
            payload = b"".join(frames)
            decoded = decode_marker(marker)
            decoded["sent_monotonic_ns"] = sent_monotonic_ns
            decoded["protocol_family"] = (
                "STATUSTEXT_MARKER_PLUS_COMMAND_LONG"
                if direction == "downlink"
                else "COMMAND_ACK_HEARTBEAT_REQUESTED_TELEMETRY"
            )
        else:
            record = make_stream_record(
                run_nonce=run_nonce,
                phase=phase,
                traffic_class=traffic_class,
                direction=direction,
                uav=uav,
                sequence=sequence,
                sent_monotonic_ns=sent_monotonic_ns,
            )
            decoded = decode_stream_record(record)
            if traffic_class == "payload":
                tunnel = struct.pack(
                    "<HBBB128s",
                    20000,
                    target_system,
                    target_component,
                    len(record),
                    record.ljust(128, b"\0"),
                )
                payload = mavlink.frame(385, tunnel, source_system, source_component)
                decoded["protocol_family"] = "MAVLINK_TUNNEL_V2"
            else:
                payload = ADDITIONAL_MAGIC + len(record).to_bytes(2, "big") + record
                payload += zlib.crc32(record).to_bytes(4, "big")
                decoded["protocol_family"] = "AMS_ADDITIONAL_DATA_V1"
    decoded["transport_payload_sha256"] = sha256_bytes(payload)
    decoded["transport_payload_size"] = len(payload)
    decoded["transport_payload_hex"] = payload.hex()
    if payload.startswith(b"\xfd"):
        decoded["mavlink_frame_sha256"] = [
            frame["mavlink_frame_sha256"] for frame in parse_mavlink_v2(payload)
        ]
    else:
        decoded["mavlink_frame_sha256"] = []
    return payload, decoded


def decode_transport_unit(payload: bytes) -> dict[str, Any]:
    if payload.startswith(ADDITIONAL_MAGIC):
        if len(payload) < 10:
            raise ProbeError("additional-data frame is truncated")
        length = int.from_bytes(payload[4:6], "big")
        if len(payload) != 10 + length:
            raise ProbeError("additional-data declared length mismatch")
        record = payload[6 : 6 + length]
        if zlib.crc32(record) != int.from_bytes(payload[-4:], "big"):
            raise ProbeError("additional-data CRC32 mismatch")
        result = decode_stream_record(record)
        if result["traffic_class"] != "additional_data":
            raise ProbeError("additional-data record carries another class")
        result["protocol_family"] = "AMS_ADDITIONAL_DATA_V1"
        frames: list[dict[str, Any]] = []
    else:
        frames = parse_mavlink_v2(payload)
        message_ids = [frame["message_id"] for frame in frames]
        if message_ids == [253, 76] or message_ids == [253, 77, 0, 33]:
            marker_payload = frames[0]["payload"]
            if len(marker_payload) != 51 or marker_payload[0] != 6:
                raise ProbeError("STATUSTEXT marker payload is invalid")
            marker = marker_payload[1:].rstrip(b"\0").decode("ascii")
            result = decode_marker(marker)
            result["protocol_family"] = (
                "STATUSTEXT_MARKER_PLUS_COMMAND_LONG"
                if message_ids == [253, 76]
                else "COMMAND_ACK_HEARTBEAT_REQUESTED_TELEMETRY"
            )
        elif message_ids == [385]:
            tunnel = frames[0]["payload"]
            if len(tunnel) != 133:
                raise ProbeError("MAVLink TUNNEL payload has wrong length")
            _payload_type, _target_system, _target_component, length = struct.unpack(
                "<HBBB", tunnel[:5]
            )
            if not 1 <= length <= 128 or any(tunnel[5 + length :]):
                raise ProbeError("MAVLink TUNNEL length/padding mismatch")
            result = decode_stream_record(tunnel[5 : 5 + length])
            if result["traffic_class"] != "payload":
                raise ProbeError("MAVLink TUNNEL carries another traffic class")
            result["protocol_family"] = "MAVLINK_TUNNEL_V2"
        else:
            raise ProbeError(f"MAVLink message family {message_ids} is not accepted")
    result["transport_payload_sha256"] = sha256_bytes(payload)
    result["transport_payload_size"] = len(payload)
    result["transport_payload_hex"] = payload.hex()
    result["mavlink_frame_sha256"] = [frame["mavlink_frame_sha256"] for frame in frames]
    result["mavlink_frames"] = frames
    return result


class EventWriter:
    def __init__(
        self,
        path: Path,
        *,
        schema: str,
        run_id: str,
        runtime_id: str,
        run_nonce: str,
        endpoint: str | None = None,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = path.open("x", encoding="utf-8")
        self.schema = schema
        self.run_id = run_id
        self.runtime_id = runtime_id
        self.run_nonce = run_nonce
        self.endpoint = endpoint
        self.sequence = 0

    def emit(self, event: str, **fields: Any) -> None:
        self.sequence += 1
        record = {
            "schema": self.schema,
            "run_id": self.run_id,
            "runtime_id": self.runtime_id,
            "run_nonce": self.run_nonce,
            "event_sequence": self.sequence,
            "monotonic_ns": time.monotonic_ns(),
            "event": event,
            **({"endpoint": self.endpoint} if self.endpoint else {}),
            **fields,
        }
        self.handle.write(
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        )
        self.handle.flush()

    def close(self) -> None:
        self.handle.flush()
        os.fsync(self.handle.fileno())
        self.handle.close()


def load_matrix(path: Path) -> dict[str, Any]:
    matrix = strict_json(path)
    try:
        from network.validation.endpoint_transaction import validate_matrix_data
    except ImportError:
        sys.path.insert(0, str(ROOT))
        from network.validation.endpoint_transaction import validate_matrix_data
    failures = validate_matrix_data(matrix)
    if failures:
        raise ProbeError("endpoint matrix is invalid: " + "; ".join(failures))
    return matrix


def socket_for(ip: str, port: int, tos: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 262144)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 262144)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, tos)
    if hasattr(socket, "IP_RECVTOS"):
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_RECVTOS, 1)
    sock.bind((ip, port))
    sock.setblocking(False)
    return sock


class EndpointAgent:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.endpoint = args.endpoint
        self.namespace = NAMESPACES[self.endpoint]
        self.ip = endpoint_ip(self.endpoint)
        self.matrix = load_matrix(args.matrix)
        self.run_contract = strict_json(args.run_dir / "raw/run_contract.json")
        for key, expected in (
            ("run_id", args.run_id),
            ("runtime_id", args.runtime_id),
            ("run_nonce", args.run_nonce),
        ):
            if self.run_contract.get(key) != expected:
                raise ProbeError(f"run contract {key} mismatch")
        self.cells = [
            cell
            for cell in self.matrix["cells"]
            if cell["source"]["namespace"] == self.namespace
            and cell["traffic_class"] in COMPANION_TRAFFIC_CLASSES
        ]
        expected_cells = 10 if self.endpoint == "gcs" else 2
        if len(self.cells) != expected_cells:
            raise ProbeError(
                f"{self.endpoint} has {len(self.cells)} source cells, expected {expected_cells}"
            )
        self.cells.sort(key=lambda cell: cell["cell_id"])
        self.writer = EventWriter(
            args.run_dir / f"raw/endpoints/{self.endpoint}.jsonl",
            schema=ENDPOINT_EVENT_SCHEMA,
            run_id=args.run_id,
            runtime_id=args.runtime_id,
            run_nonce=args.run_nonce,
            endpoint=self.endpoint,
        )
        ports = {
            traffic_class: next(
                int(cell["source"]["udp_port"])
                for cell in self.cells
                if cell["traffic_class"] == traffic_class
            )
            for traffic_class in COMPANION_TRAFFIC_CLASSES
        }
        self.sockets = {
            traffic_class: socket_for(self.ip, port, TOS_BY_CLASS[traffic_class])
            for traffic_class, port in ports.items()
        }
        self.socket_class = {sock.fileno(): name for name, sock in self.sockets.items()}
        self.p2mp_socket: socket.socket | None = None
        if self.endpoint != "gcs":
            group = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            group.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            group.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 262144)
            group.bind(("0.0.0.0", P2MP_PORT))
            group.setsockopt(
                socket.IPPROTO_IP,
                socket.IP_ADD_MEMBERSHIP,
                socket.inet_aton(P2MP_GROUP) + socket.inet_aton(self.ip),
            )
            if hasattr(socket, "IP_RECVTOS"):
                group.setsockopt(socket.IPPROTO_IP, socket.IP_RECVTOS, 1)
            group.setblocking(False)
            self.p2mp_socket = group
            self.socket_class[group.fileno()] = "additional_data"
        else:
            additional = self.sockets["additional_data"]
            additional.setsockopt(
                socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(self.ip)
            )
            additional.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 8)
            additional.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
        self.mavlink = MavlinkSequencer()
        self.processed_commands: set[str] = set()
        # M3 carries its 128-bit run nonce directly.  Later profiles may bind a
        # wider run nonce in their immutable contract and provide a separately
        # derived 128-bit wire nonce without changing the accepted byte format.
        self.transport_run_nonce = str(
            getattr(args, "transport_run_nonce", args.run_nonce)
        )
        if HEX32.fullmatch(self.transport_run_nonce) is None:
            raise ProbeError("transport_run_nonce must be exact 128-bit lowercase hex")
        self._pump_socket_cursor = 0

    def all_sockets(self) -> list[socket.socket]:
        return [
            *self.sockets.values(),
            *([self.p2mp_socket] if self.p2mp_socket else []),
        ]

    def _receive_one(self, sock: socket.socket) -> bool:
        try:
            payload, ancillary, _flags, peer = sock.recvmsg(65535, 128)
        except BlockingIOError:
            return False
        received_ns = time.monotonic_ns()
        rx_tos: int | None = None
        for level, kind, data in ancillary:
            if level == socket.IPPROTO_IP and kind == getattr(
                socket, "IP_TOS", 1
            ):
                rx_tos = int.from_bytes(data, sys.byteorder)
        base = {
            "namespace": self.namespace,
            "local_ip": self.ip,
            "local_udp_port": int(sock.getsockname()[1]),
            "peer_ip": peer[0],
            "peer_udp_port": int(peer[1]),
            "rx_tos": rx_tos,
            "transport_payload_hex": payload.hex(),
            "transport_payload_sha256": sha256_bytes(payload),
            "transport_payload_size": len(payload),
            "received_monotonic_ns": received_ns,
        }
        try:
            decoded = decode_transport_unit(payload)
            if decoded["run_nonce"] != self.transport_run_nonce:
                self.writer.emit("foreign_receive", reason="run_nonce", **base)
                return True
            self.writer.emit(
                "remote_receive",
                socket_class=self.socket_class[sock.fileno()],
                phase=decoded["phase"],
                flow_id=decoded["flow_id"],
                cell_id=None if decoded["p2mp"] else decoded["flow_id"],
                traffic_class=decoded["traffic_class"],
                direction=decoded["direction"],
                uav=decoded["uav"],
                sequence=decoded["sequence"],
                record_nonce=decoded["record_nonce"],
                application_unit_sha256=decoded["application_unit_sha256"],
                protocol_family=decoded["protocol_family"],
                p2mp=decoded["p2mp"],
                mavlink_frame_sha256=decoded["mavlink_frame_sha256"],
                sent_monotonic_ns=decoded.get("sent_monotonic_ns"),
                **base,
            )
        except (ProbeError, UnicodeError, ValueError) as exc:
            self.writer.emit("foreign_receive", reason=str(exc), **base)
        return True

    def pump(self, timeout_s: float) -> None:
        sockets = self.all_sockets()
        readable, _writable, _exceptional = select.select(
            sockets, [], [], timeout_s
        )
        if not readable:
            return
        start = self._pump_socket_cursor % len(sockets)
        ordered = sockets[start:] + sockets[:start]
        readable_fds = {sock.fileno() for sock in readable}
        active = [sock for sock in ordered if sock.fileno() in readable_fds]
        self._pump_socket_cursor = (start + 1) % len(sockets)
        remaining = ENDPOINT_PUMP_DATAGRAM_LIMIT
        while active and remaining > 0:
            next_round: list[socket.socket] = []
            for sock in active:
                if remaining <= 0:
                    break
                if self._receive_one(sock):
                    remaining -= 1
                    next_round.append(sock)
            active = next_round

    def pump_until(self, deadline_ns: int) -> None:
        while True:
            remaining_ns = deadline_ns - time.monotonic_ns()
            if remaining_ns <= 0:
                return
            self.pump(min(0.05, remaining_ns / 1_000_000_000))

    def send(
        self,
        command: dict[str, Any],
        cell: dict[str, Any] | None,
        sequence: int,
        *,
        p2mp: bool,
    ) -> None:
        sent_ns = time.monotonic_ns()
        payload, decoded = encode_transport_unit(
            run_nonce=self.transport_run_nonce,
            phase=command["phase"],
            cell=cell,
            sequence=sequence,
            sent_monotonic_ns=sent_ns,
            mavlink=self.mavlink,
            p2mp=p2mp,
        )
        if p2mp:
            traffic_class = "additional_data"
            destination = (P2MP_GROUP, P2MP_PORT)
            cell_id: str | None = None
        else:
            assert cell is not None
            traffic_class = str(cell["traffic_class"])
            destination = (
                str(cell["destination"]["ip"]),
                int(cell["destination"]["udp_port"]),
            )
            cell_id = str(cell["cell_id"])
        sock = self.sockets[traffic_class]
        try:
            sent_size = sock.sendto(payload, destination)
        except OSError as exc:
            self.writer.emit(
                "send_error",
                phase=command["phase"],
                cell_id=cell_id,
                sequence=sequence,
                error=f"{type(exc).__name__}: {exc}",
            )
            return
        self.writer.emit(
            "offered",
            namespace=self.namespace,
            phase=command["phase"],
            flow_id=decoded["flow_id"],
            cell_id=cell_id,
            traffic_class=decoded["traffic_class"],
            direction=decoded["direction"],
            uav=decoded["uav"],
            sequence=decoded["sequence"],
            record_nonce=decoded["record_nonce"],
            application_unit_sha256=decoded["application_unit_sha256"],
            protocol_family=decoded["protocol_family"],
            p2mp=decoded["p2mp"],
            mavlink_frame_sha256=decoded["mavlink_frame_sha256"],
            source_ip=self.ip,
            source_udp_port=int(sock.getsockname()[1]),
            destination_ip=destination[0],
            destination_udp_port=destination[1],
            tos=TOS_BY_CLASS[traffic_class],
            sent_monotonic_ns=sent_ns,
            send_return_size=sent_size,
            transport_payload_hex=payload.hex(),
            transport_payload_sha256=decoded["transport_payload_sha256"],
            transport_payload_size=decoded["transport_payload_size"],
        )

    def execute_phase(self, command: dict[str, Any], command_hash: str) -> None:
        phase = command["phase"]
        start_ns = int(command["start_monotonic_ns"])
        end_ns = int(command["end_monotonic_ns"])
        count = int(command["offered_per_cell"])
        p2mp_roots = int(command["p2mp_roots"])
        send_span_ns = int(command["send_span_ms"]) * 1_000_000
        if end_ns <= start_ns or send_span_ns >= end_ns - start_ns:
            raise ProbeError(f"unsafe phase timing in {phase}")
        self.pump_until(start_ns)
        self.writer.emit(
            "phase_start",
            phase=phase,
            command_sha256=command_hash,
            declared_start_monotonic_ns=start_ns,
            declared_end_monotonic_ns=end_ns,
            expected_engine_state=command["expected_engine_state"],
        )
        offer_count = (
            p2mp_roots if phase == "p2mp" and self.endpoint == "gcs" else count
        )
        for sequence in range(1, offer_count + 1):
            slot = start_ns + ((sequence - 1) * send_span_ns // max(offer_count, 1))
            self.pump_until(slot)
            if phase == "p2mp":
                self.send(command, None, sequence, p2mp=True)
            else:
                for cell in self.cells:
                    self.send(command, cell, sequence, p2mp=False)
                    self.pump(0)
        self.pump_until(end_ns)
        self.writer.emit(
            "phase_complete",
            phase=phase,
            command_sha256=command_hash,
            expected_engine_state=command["expected_engine_state"],
        )
        done = self.args.run_dir / f"raw/state/{self.endpoint}.{phase}.done.json"
        write_exclusive(
            done,
            {
                "endpoint": self.endpoint,
                "phase": phase,
                "command_sha256": command_hash,
                "completed_monotonic_ns": time.monotonic_ns(),
            },
        )

    def run(self) -> None:
        process_ticks = None
        try:
            stat = Path(f"/proc/{os.getpid()}/stat").read_text(encoding="utf-8")
            process_ticks = int(stat[stat.rfind(")") + 2 :].split()[19])
        except (OSError, ValueError, IndexError):
            pass
        self.writer.emit(
            "agent_ready",
            namespace=self.namespace,
            pid=os.getpid(),
            process_start_ticks=process_ticks,
            bound_sockets={
                name: [sock.getsockname()[0], sock.getsockname()[1]]
                for name, sock in self.sockets.items()
            },
            p2mp_membership=(
                None if self.endpoint == "gcs" else [P2MP_GROUP, P2MP_PORT, self.ip]
            ),
            receive_buffer_bytes={
                str(sock.getsockname()[1]): sock.getsockopt(
                    socket.SOL_SOCKET, socket.SO_RCVBUF
                )
                for sock in self.all_sockets()
            },
            send_buffer_bytes={
                str(sock.getsockname()[1]): sock.getsockopt(
                    socket.SOL_SOCKET, socket.SO_SNDBUF
                )
                for sock in self.sockets.values()
            },
        )
        write_exclusive(
            self.args.run_dir / f"raw/state/{self.endpoint}.ready.json",
            {
                "endpoint": self.endpoint,
                "pid": os.getpid(),
                "monotonic_ns": time.monotonic_ns(),
            },
        )
        command_dir = self.args.run_dir / f"raw/control/{self.endpoint}"
        while True:
            commands = sorted(command_dir.glob("*.json"))
            pending = [
                path for path in commands if path.name not in self.processed_commands
            ]
            if not pending:
                self.pump(0.05)
                continue
            path = pending[0]
            command = strict_json(path)
            command_hash = sha256_file(path)
            self.processed_commands.add(path.name)
            for key in ("run_id", "runtime_id", "run_nonce"):
                if command.get(key) != getattr(self.args, key):
                    raise ProbeError(f"command {path} has wrong {key}")
            if command.get("endpoint") != self.endpoint:
                raise ProbeError(f"command {path} targets another endpoint")
            if command.get("action") == "shutdown":
                self.pump_until(int(command["not_before_monotonic_ns"]))
                self.writer.emit("agent_shutdown", command_sha256=command_hash)
                return
            if (
                command.get("action") != "phase"
                or command.get("phase") not in PHASE_CODES
            ):
                raise ProbeError(f"command {path} action/phase is invalid")
            self.execute_phase(command, command_hash)

    def close(self) -> None:
        for sock in self.all_sockets():
            sock.close()
        self.writer.close()


class ForbiddenListener:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        contract = strict_json(args.run_dir / "raw/forbidden_canary_contract.json")
        if (
            contract.get("contract") != FORBIDDEN_CONTRACT
            or contract.get("run_id") != args.run_id
            or contract.get("runtime_id") != args.runtime_id
            or contract.get("run_nonce") != args.run_nonce
            or contract.get("canaries") != forbidden_canaries(args.run_nonce)
        ):
            raise ProbeError("forbidden listener contract identity mismatch")
        self.canaries = [
            canary
            for canary in contract["canaries"]
            if canary["listener_endpoint"] == args.endpoint
        ]
        if not self.canaries:
            raise ProbeError("forbidden listener endpoint has no declared bindings")
        self.writer = EventWriter(
            args.run_dir / f"raw/forbidden/listener-{args.endpoint}.jsonl",
            schema=FORBIDDEN_LISTENER_SCHEMA,
            run_id=args.run_id,
            runtime_id=args.runtime_id,
            run_nonce=args.run_nonce,
            endpoint=args.endpoint,
        )
        self.sockets: dict[int, tuple[socket.socket, dict[str, Any]]] = {}
        for canary in self.canaries:
            family = (
                socket.AF_INET6
                if canary["address_family"] == "ipv6"
                else socket.AF_INET
            )
            sock = socket.socket(family, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if canary["kind"] == "unreachable_ipv4":
                sock.setsockopt(socket.IPPROTO_IP, 15, 1)  # IP_FREEBIND
            sock.bind((canary["destination_ip"], canary["destination_udp_port"]))
            sock.setblocking(False)
            self.sockets[sock.fileno()] = (sock, canary)

    def run(self) -> None:
        stat_payload = Path("/proc/self/stat").read_text(encoding="utf-8")
        close = stat_payload.rfind(")")
        start_ticks = int(stat_payload[close + 2 :].split()[19])
        executable_path = Path("/proc/self/exe")
        identity = {
            "pid": os.getpid(),
            "start_ticks": start_ticks,
            "executable": os.readlink(executable_path),
            "executable_sha256": sha256_file(executable_path),
            "bindings": [
                {
                    "canary_id": canary["canary_id"],
                    "address_family": canary["address_family"],
                    "ip": canary["destination_ip"],
                    "udp_port": canary["destination_udp_port"],
                }
                for canary in self.canaries
            ],
        }
        self.writer.emit("listener_ready", **identity)
        write_exclusive(
            self.args.run_dir
            / f"raw/state/forbidden-listener-{self.args.endpoint}.ready.json",
            {
                "contract": FORBIDDEN_LISTENER_SCHEMA,
                "run_id": self.args.run_id,
                "runtime_id": self.args.runtime_id,
                "run_nonce": self.args.run_nonce,
                "endpoint": self.args.endpoint,
                **identity,
                "ready_monotonic_ns": time.monotonic_ns(),
            },
        )
        stop_file = self.args.run_dir / "raw/forbidden/listeners.stop"
        while not stop_file.exists():
            ready, _writable, _exceptional = select.select(
                [item[0] for item in self.sockets.values()], [], [], 0.2
            )
            for sock in ready:
                while True:
                    try:
                        payload, peer = sock.recvfrom(65_535)
                    except BlockingIOError:
                        break
                    canary = self.sockets[sock.fileno()][1]
                    self.writer.emit(
                        "forbidden_receive",
                        canary_id=canary["canary_id"],
                        peer_ip=str(peer[0]),
                        peer_udp_port=int(peer[1]),
                        transport_payload_hex=payload.hex(),
                        transport_payload_sha256=sha256_bytes(payload),
                        transport_payload_size=len(payload),
                    )
        self.writer.emit("listener_shutdown", **identity)

    def close(self) -> None:
        for sock, _canary in self.sockets.values():
            sock.close()
        self.writer.close()


def resolve_five_uav_flight_scenario(source: Path) -> tuple[bytes, dict[str, Any]]:
    """Freeze the Q1 scenario with the five declared M2-style MAVProxy tails."""

    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - dependency gate
        raise ProbeError(f"PyYAML is required to resolve the flight scenario: {exc}") from exc
    try:
        document = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ProbeError(f"cannot load five-UAV flight scenario: {exc}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("robots"), list):
        raise ProbeError("five-UAV flight scenario has no robots list")
    robots = document["robots"]
    if len(robots) != 5:
        raise ProbeError("flight scenario must contain exactly five UAVs")
    resolved_endpoints: dict[str, str] = {}
    for index, robot in enumerate(robots, start=1):
        expected = {
            "name": f"uav{index}",
            "instance": index - 1,
            "system_id": index,
        }
        if not isinstance(robot, dict) or any(
            robot.get(key) != value for key, value in expected.items()
        ):
            raise ProbeError(f"flight scenario UAV {index} identity is not exact")
        mavproxy_out = f"10.72.{index}.2:{14559 + index}"
        existing = robot.get("mavproxy_out")
        if existing not in (None, mavproxy_out):
            raise ProbeError(f"flight scenario {robot['name']} mavproxy_out conflicts")
        robot["mavproxy_out"] = mavproxy_out
        resolved_endpoints[robot["name"]] = mavproxy_out
    payload = (
        json.dumps(
            document,
            allow_nan=False,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode()
    return payload, {
        "contract": RESOLVED_FLIGHT_CONTRACT,
        "robot_count": 5,
        "mavproxy_out": resolved_endpoints,
        "payload_sha256": sha256_bytes(payload),
    }


def initialize_run(args: argparse.Namespace) -> None:
    if not SAFE_ID.fullmatch(args.run_id) or not HEX32.fullmatch(args.runtime_id):
        raise ProbeError("run_id/runtime_id is invalid")
    if not HEX32.fullmatch(args.run_nonce):
        raise ProbeError(
            "run_nonce must be exactly 32 lowercase hexadecimal characters"
        )
    matrix = load_matrix(args.matrix)
    endpoint_schema_path = args.endpoint_schema.resolve(strict=True)
    endpoint_schema = strict_json(endpoint_schema_path)
    if endpoint_schema.get("$id") != (
        "https://ams.local/schemas/endpoint-transaction-v1.json"
    ):
        raise ProbeError("endpoint transaction schema identity is not exact")
    endpoint_schema_payload = endpoint_schema_path.read_bytes()
    endpoint_schema_copy = args.run_dir / "raw/endpoint_transaction_schema.json"
    flight_scenario = args.flight_scenario.resolve(strict=True)
    resolved_flight_payload, resolved_flight = resolve_five_uav_flight_scenario(
        flight_scenario
    )
    resolved_flight_path = args.run_dir / "raw/resolved_flight_scenario.yaml"
    binary = args.engine_binary.resolve(strict=True)
    ns3_dir = args.ns3_dir.resolve(strict=True)
    receipt = args.build_receipt.resolve(strict=True)
    if args.technical_smoke:
        if args.m2_receipt is not None:
            raise ProbeError("technical smoke must not import a formal M2 receipt")
        m2_predecessor = None
        m2_receipt_payload: bytes | None = None
    else:
        if args.m2_receipt is None or args.m2_receipt.is_symlink():
            raise ProbeError("formal M3 requires a non-symlink M2 receipt")
        m2_receipt_path = args.m2_receipt.resolve(strict=True)
        m2_predecessor, m2_receipt_payload = load_m2_predecessor(m2_receipt_path)
    copied_source = ns3_dir / "scratch/ams-tap-packet-engine.cc"
    if binary.parent.parent != ns3_dir / "build":
        raise ProbeError(
            "packet-engine executable is outside the declared ns-3 build tree"
        )
    if not copied_source.is_file():
        raise ProbeError("copied packet-engine source is absent from ns-3 scratch")
    sources = (
        args.matrix,
        ROOT / "network/config/endpoint_transaction_schema.json",
        ROOT / "network/validation/endpoint_transaction.py",
        ROOT / "network/ns3/scratch/ams-tap-packet-engine.cc",
        ROOT / "network/ns3/tap_packet_engine_config.py",
        ROOT / "network/ns3/run_ns3_tap_packet_engine.sh",
        ROOT / "network/ns3/build_ns3_tap_packet_engine.sh",
        ROOT / "network/ns3/ns3_build_receipt.py",
        ROOT / "network/scripts/raw_packet_capture.py",
        ROOT / "network/bridge/opaque_udp_relay.py",
        ROOT / "network/bridge/runtime_clock_beacon.py",
        ROOT / "network/bridge/actual_sitl_mavlink_endpoint.py",
        ROOT / "network/scripts/actual_sitl_endpoint_orchestrator.py",
        ROOT / "network/scripts/actual_sitl_control_probe.py",
        ROOT / "network/scripts/m3_topology_monitor.py",
        ROOT / "network/scripts/write_run_provenance.py",
        ROOT / "network/scripts/m3_external_matrix_probe.py",
        ROOT / "network/scripts/run_m3_external_matrix.sh",
        ROOT / "network/scripts/validate_m3_external_matrix.py",
        ROOT / "network/validation/validate_m3_external_matrix.py",
        flight_scenario,
        ROOT / "src/multiagent_simulation/launch/multiagent_simulation.launch.py",
    )
    missing = [str(path) for path in sources if not path.is_file()]
    if missing:
        raise ProbeError(f"required M3 sources are absent: {missing}")
    contract = {
        "contract": RUN_CONTRACT,
        "run_id": args.run_id,
        "runtime_id": args.runtime_id,
        "run_nonce": args.run_nonce,
        "created_monotonic_ns": time.monotonic_ns(),
        "execution": {
            "mode": "technical_smoke" if args.technical_smoke else "formal",
            "acceptance_eligible": not args.technical_smoke,
            "formal_m2_predecessor_bound": not args.technical_smoke,
        },
        "matrix": {
            "path": args.matrix.resolve().relative_to(ROOT).as_posix(),
            "sha256": sha256_file(args.matrix),
            "resolved_cells_sha256": matrix["resolved_cells_sha256"],
            "cell_count": len(matrix["cells"]),
            "profile": "m3_full",
            "endpoint_schema": {
                "path": endpoint_schema_path.relative_to(ROOT).as_posix(),
                "sha256": sha256_file(endpoint_schema_path),
                "$id": endpoint_schema["$id"],
                "matrix_contract": matrix["contract"],
                "raw_copy_path": endpoint_schema_copy.relative_to(
                    args.run_dir
                ).as_posix(),
            },
        },
        "endpoint_namespaces": NAMESPACES,
        "ns3_namespace": "ams-ns3",
        "flight_runtime": {
            **resolved_flight,
            "control_endpoint_form": ACTUAL_CONTROL_ENDPOINT_FORM,
            "control_process_role_ids": {
                "gcs": "gcs_control_probe",
                "adapters": {
                    f"uav{index}": f"uav_control_adapter_uav{index}"
                    for index in range(1, 6)
                },
                "supervisor": "actual_endpoint_supervisor",
            },
            "source_path": flight_scenario.relative_to(ROOT).as_posix(),
            "source_sha256": sha256_file(flight_scenario),
            "resolved_path": resolved_flight_path.relative_to(args.run_dir).as_posix(),
        },
        "packet_engine": {
            "program": "ams-tap-packet-engine",
            "path": str(binary),
            "sha256": sha256_file(binary),
            "size": binary.stat().st_size,
            "contract": "ams.tap_packet_engine/v1",
            "event_schema": "ams.ns3.packet_event/v1",
            "uav_count": 5,
            "ns3_dir": str(ns3_dir),
            "copied_source": str(copied_source),
            "required_modules": [
                "applications",
                "bridge",
                "core",
                "csma",
                "flow-monitor",
                "internet",
                "mobility",
                "network",
                "stats",
                "tap-bridge",
                "traffic-control",
            ],
            "build_receipt": {"path": str(receipt), "sha256": sha256_file(receipt)},
            "lifecycle_manifest": engine_lifecycle_manifest(),
        },
        "m2_predecessor": m2_predecessor,
        "p2mp": {
            "group": P2MP_GROUP,
            "udp_port": P2MP_PORT,
            "root_nonce_domain": "ams/v1/p2mp/additional_data/downlink",
            "intended_receivers": [f"uav{index}" for index in range(1, 6)],
        },
        "source_sha256": {
            path.resolve().relative_to(ROOT).as_posix(): sha256_file(path)
            for path in sources
        },
    }
    canary_contract = {
        "contract": FORBIDDEN_CONTRACT,
        "run_id": args.run_id,
        "runtime_id": args.runtime_id,
        "run_nonce": args.run_nonce,
        "created_monotonic_ns": time.monotonic_ns(),
        "canaries": forbidden_canaries(args.run_nonce),
    }
    for directory in (
        "raw/endpoints",
        "raw/control",
        "raw/state",
        "raw/topology",
        "raw/topology_monitor",
        "raw/forbidden",
        "raw/actual_control",
        "raw/actual_sitl",
        "logs",
        "pcap",
        "validation",
    ):
        (args.run_dir / directory).mkdir(parents=True, exist_ok=True)
    for endpoint in ENDPOINTS:
        (args.run_dir / f"raw/control/{endpoint}").mkdir(parents=True, exist_ok=True)
    (args.run_dir / "raw/control/actual-control").mkdir(parents=True, exist_ok=True)
    write_bytes_exclusive(resolved_flight_path, resolved_flight_payload)
    write_bytes_exclusive(endpoint_schema_copy, endpoint_schema_payload)
    if m2_receipt_payload is not None:
        write_bytes_exclusive(
            args.run_dir / "raw/m2_component_host_final_receipt.json",
            m2_receipt_payload,
        )
    write_exclusive(args.run_dir / "raw/run_contract.json", contract)
    write_exclusive(
        args.run_dir / "raw/forbidden_canary_contract.json", canary_contract
    )


def create_schedule(args: argparse.Namespace) -> None:
    run_contract = strict_json(args.run_dir / "raw/run_contract.json")
    base = args.positive_start_monotonic_ns
    positive = {
        "phase": "positive",
        "start_monotonic_ns": base,
        # M3 control transactions are deliberately non-overlapping because
        # legacy ACK/telemetry replies do not carry a transaction token.  Give
        # all 20 ordinals their full three-second outcome interval plus five
        # seconds of host-scheduling reserve; the payload workload remains in
        # the frozen first 25 seconds.
        "end_monotonic_ns": base + 65_000_000_000,
        "offered_per_cell": 20,
        "p2mp_roots": 0,
        "send_span_ms": 25_000,
        "expected_engine_state": "up_epoch_1",
    }
    p2mp = {
        "phase": "p2mp",
        "start_monotonic_ns": positive["end_monotonic_ns"] + 500_000_000,
        "end_monotonic_ns": positive["end_monotonic_ns"] + 2_500_000_000,
        "offered_per_cell": 0,
        "p2mp_roots": 20,
        "send_span_ms": 1_000,
        "expected_engine_state": "up_epoch_1",
    }
    stop_request = p2mp["end_monotonic_ns"] + 500_000_000
    stopped = {
        "phase": "stopped",
        "start_monotonic_ns": stop_request + 1_500_000_000,
        "end_monotonic_ns": stop_request + 21_500_000_000,
        "offered_per_cell": 5,
        "p2mp_roots": 0,
        "send_span_ms": 15_000,
        "expected_engine_state": "stopped",
    }
    restart_request = stopped["end_monotonic_ns"] + 500_000_000
    # The M3 validator reserves the final ten seconds before recovery for a
    # fully ready epoch-2 engine.  Keep two bounded seconds after the restart
    # request for ns-3 readiness, rather than relying on the former 500 ms
    # scheduling edge under normal host jitter.
    recovery = {
        "phase": "recovery",
        "start_monotonic_ns": restart_request + 12_000_000_000,
        "end_monotonic_ns": restart_request + 77_000_000_000,
        "offered_per_cell": 20,
        "p2mp_roots": 0,
        "send_span_ms": 25_000,
        "expected_engine_state": "up_epoch_2",
    }
    windows = [positive, p2mp, stopped, recovery]
    contract = {
        "contract": PHASE_CONTRACT,
        "run_id": run_contract["run_id"],
        "runtime_id": run_contract["runtime_id"],
        "run_nonce": run_contract["run_nonce"],
        "matrix_sha256": run_contract["matrix"]["sha256"],
        "created_monotonic_ns": time.monotonic_ns(),
        "stop_request_monotonic_ns": stop_request,
        "restart_request_monotonic_ns": restart_request,
        "windows": windows,
    }
    write_exclusive(args.run_dir / "raw/phase_contract.json", contract)
    for endpoint in ENDPOINTS:
        for index, window in enumerate(windows, start=1):
            command = {
                "action": "phase",
                "endpoint": endpoint,
                "run_id": contract["run_id"],
                "runtime_id": contract["runtime_id"],
                "run_nonce": contract["run_nonce"],
                **window,
            }
            write_exclusive(
                args.run_dir
                / f"raw/control/{endpoint}/{index:03d}-{window['phase']}.json",
                command,
            )
        write_exclusive(
            args.run_dir / f"raw/control/{endpoint}/999-shutdown.json",
            {
                "action": "shutdown",
                "endpoint": endpoint,
                "run_id": contract["run_id"],
                "runtime_id": contract["runtime_id"],
                "run_nonce": contract["run_nonce"],
                "not_before_monotonic_ns": recovery["end_monotonic_ns"] + 500_000_000,
            },
        )
    actual_control_dir = args.run_dir / "raw/control/actual-control"
    for index, window in enumerate(
        (positive, stopped, recovery), start=1
    ):
        write_exclusive(
            actual_control_dir / f"{index:03d}-{window['phase']}.json",
            {
                "action": "phase",
                "endpoint": "actual-control",
                "run_id": contract["run_id"],
                "runtime_id": contract["runtime_id"],
                "run_nonce": contract["run_nonce"],
                **window,
            },
        )
    write_exclusive(
        actual_control_dir / "999-shutdown.json",
        {
            "action": "shutdown",
            "endpoint": "actual-control",
            "run_id": contract["run_id"],
            "runtime_id": contract["runtime_id"],
            "run_nonce": contract["run_nonce"],
            "not_before_monotonic_ns": recovery["end_monotonic_ns"]
            + 500_000_000,
        },
    )


def run_forbidden_canaries(args: argparse.Namespace) -> None:
    contract = strict_json(args.run_dir / "raw/forbidden_canary_contract.json")
    if (
        contract.get("contract") != FORBIDDEN_CONTRACT
        or contract.get("run_id") != args.run_id
        or contract.get("runtime_id") != args.runtime_id
        or contract.get("run_nonce") != args.run_nonce
        or contract.get("canaries") != forbidden_canaries(args.run_nonce)
    ):
        raise ProbeError("forbidden canary contract identity/content mismatch")
    selected = [
        canary
        for canary in contract["canaries"]
        if canary["source_endpoint"] == args.source_endpoint
    ]
    if not selected:
        raise ProbeError("forbidden canary source has no declared probes")
    observations: list[dict[str, Any]] = []
    started_ns = time.monotonic_ns()
    for canary in selected:
        family = (
            socket.AF_INET6 if canary["address_family"] == "ipv6" else socket.AF_INET
        )
        sock = socket.socket(family, socket.SOCK_DGRAM)
        try:
            if family == socket.AF_INET:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, canary["tos"])
            sock.bind((canary["source_ip"], canary["source_udp_port"]))
            sent_ns = time.monotonic_ns()
            payload = bytes.fromhex(canary["transport_payload_hex"])
            sent = sock.sendto(
                payload,
                (canary["destination_ip"], canary["destination_udp_port"]),
            )
        finally:
            sock.close()
        observations.append(
            {
                "canary_id": canary["canary_id"],
                "sequence": canary["sequence"],
                "sent_monotonic_ns": sent_ns,
                "send_return_size": sent,
                "transport_payload_sha256": canary["transport_payload_sha256"],
            }
        )
        time.sleep(0.025)
    write_exclusive(
        args.run_dir / f"raw/forbidden/{args.source_endpoint}.json",
        {
            "contract": FORBIDDEN_RESULT_CONTRACT,
            "run_id": args.run_id,
            "runtime_id": args.runtime_id,
            "run_nonce": args.run_nonce,
            "source_endpoint": args.source_endpoint,
            "started_monotonic_ns": started_ns,
            "completed_monotonic_ns": time.monotonic_ns(),
            "observations": observations,
        },
    )


def append_lifecycle(args: argparse.Namespace) -> None:
    run_contract = strict_json(args.run_dir / "raw/run_contract.json")
    path = args.run_dir / "raw/lifecycle.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    details = json.loads(args.details_json)
    if not isinstance(details, dict):
        raise ProbeError("lifecycle details must be a JSON object")
    flags = os.O_RDWR | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.seek(0)
        sequence = sum(1 for line in handle if line.strip()) + 1
        handle.seek(0, os.SEEK_END)
        record = {
            "schema": LIFECYCLE_EVENT_SCHEMA,
            "run_id": run_contract["run_id"],
            "runtime_id": run_contract["runtime_id"],
            "run_nonce": run_contract["run_nonce"],
            "event_sequence": sequence,
            "monotonic_ns": time.monotonic_ns(),
            "event": args.event,
            "details": details,
        }
        handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    initialize = subparsers.add_parser("initialize")
    initialize.add_argument("--run-dir", type=Path, required=True)
    initialize.add_argument("--run-id", required=True)
    initialize.add_argument("--runtime-id", required=True)
    initialize.add_argument("--run-nonce", required=True)
    initialize.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    initialize.add_argument(
        "--endpoint-schema",
        type=Path,
        default=ROOT / "network/config/endpoint_transaction_schema.json",
    )
    initialize.add_argument(
        "--flight-scenario",
        type=Path,
        default=ROOT / "network/config/scenario_5uav.yaml",
    )
    initialize.add_argument("--engine-binary", type=Path, required=True)
    initialize.add_argument("--ns3-dir", type=Path, required=True)
    initialize.add_argument("--build-receipt", type=Path, required=True)
    initialize.add_argument("--m2-receipt", type=Path)
    initialize.add_argument("--technical-smoke", action="store_true")

    schedule = subparsers.add_parser("schedule")
    schedule.add_argument("--run-dir", type=Path, required=True)
    schedule.add_argument("--positive-start-monotonic-ns", type=int, required=True)

    lifecycle = subparsers.add_parser("lifecycle")
    lifecycle.add_argument("--run-dir", type=Path, required=True)
    lifecycle.add_argument("--event", required=True)
    lifecycle.add_argument("--details-json", default="{}")

    canary = subparsers.add_parser("forbidden-canaries")
    canary.add_argument("--run-dir", type=Path, required=True)
    canary.add_argument("--run-id", required=True)
    canary.add_argument("--runtime-id", required=True)
    canary.add_argument("--run-nonce", required=True)
    canary.add_argument(
        "--source-endpoint", choices=("container-root", *ENDPOINTS), required=True
    )

    agent = subparsers.add_parser("agent")
    agent.add_argument("--run-dir", type=Path, required=True)
    agent.add_argument("--run-id", required=True)
    agent.add_argument("--runtime-id", required=True)
    agent.add_argument("--run-nonce", required=True)
    agent.add_argument("--endpoint", choices=ENDPOINTS, required=True)
    agent.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)

    listener = subparsers.add_parser("forbidden-listener")
    listener.add_argument("--run-dir", type=Path, required=True)
    listener.add_argument("--run-id", required=True)
    listener.add_argument("--runtime-id", required=True)
    listener.add_argument("--run-nonce", required=True)
    listener.add_argument("--endpoint", choices=ENDPOINTS, required=True)

    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        if args.command == "initialize":
            initialize_run(args)
        elif args.command == "schedule":
            create_schedule(args)
        elif args.command == "lifecycle":
            append_lifecycle(args)
        elif args.command == "forbidden-canaries":
            run_forbidden_canaries(args)
        elif args.command == "forbidden-listener":
            listener_process = ForbiddenListener(args)
            try:
                listener_process.run()
            finally:
                listener_process.close()
        else:
            endpoint = EndpointAgent(args)
            try:
                endpoint.run()
            finally:
                endpoint.close()
    except (ProbeError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"FAIL M3 external producer: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
