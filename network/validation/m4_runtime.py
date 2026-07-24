#!/usr/bin/env python3
"""Independent M4 scene, time, workload, and freshness derivations."""

from __future__ import annotations

import bisect
import hashlib
import json
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from network.validation.m4_common import (
    EXPECTED_CELLS,
    HEX64,
    M4ValidationError,
    finite_number,
    regular_file,
    strict_json,
    strict_jsonl,
)
from network.validation.m4_pose_observations import (
    GAZEBO_POSE_SOURCE_STAMP_SCOPE,
    GAZEBO_POSE_SOURCE_TRANSPORT,
    ODOMETRY_SOURCE_STAMP_SCOPE,
    ODOMETRY_SOURCE_TRANSPORT,
    POSE_OBSERVATION_STREAM_ENCODING,
    POSE_OBSERVATION_STREAM_PATH,
    POSE_OBSERVATION_STREAM_SCHEMA,
    pose_observation_exact_key,
    scan_pose_observation_stream,
)
from network.radio_provider.sionna_packet_adapter import deterministic_loss_sample


ROOT = Path(__file__).resolve().parents[2]
FROZEN_BUNDLE_PATH = ROOT / "network/config/m4_canonical_scene_bundle.json"
FROZEN_BUNDLE_ID = "ams-m4-canonical-km-v2"
FROZEN_BUNDLE_SHA256 = (
    "17a9d5254a0ad680ebd71a446df68c3a4de1ceda7c46efdafe06d73ab5f4c319"
)
QUERY_PERIOD_NS = 1_000_000_000
VALIDITY_TTL_NS = 2_000_000_000
MAX_POSE_AGE_NS = 1_500_000_000
QUERY_DEADLINE_NS = 100_000_000
QUERY_GLOBAL_SPACING_NS = 33_333_333
QUERY_SLOT_COUNT_PER_CELL = 600
QUERY_SLOT_ID_RE = re.compile(r"\.slot([1-9][0-9]*)\.s[1-9][0-9]*$")
POSE_BINDING_CLOCK_MAX_GAP_NS = 250_000_000
POSE_BINDING_CLOCK_FUTURE_TOLERANCE_NS = 50_000_000
POSE_BINDING_EMIT_DELAY_NS = 50_000_000
POSE_BINDING_ENTITIES = (
    "cp",
    "uav1",
    "uav2",
    "uav3",
    "uav4",
    "uav5",
    "jammer_m4",
)
RUNTIME_EVENT_SCHEMA = "ams.m4.runtime_event/v1"
CLOCK_SAMPLE_SCHEMA = "ams.m4.clock_correlation_sample/v1"
CAPTURE_STATS_CONTRACT = "ams.raw-packet-capture-stats/v2"
CAPTURE_PROTOCOL = "ETH_P_ALL"
CAPTURE_PACKET_FILTER = "none"
CAPTURE_RECEIVE_BUFFER_REQUESTED_BYTES = 8_388_608
CAPTURE_RECEIVE_BUFFER_EFFECTIVE_BYTES = 16_777_216
CAPTURE_RECEIVE_BUFFER_SETTERS = {"SO_RCVBUF", "SO_RCVBUFFORCE"}
CAPTURE_DRAIN_BATCH_PACKET_LIMIT = 256
CAPTURE_DRAIN_BATCH_BYTE_LIMIT = 4_194_304
# PCAP timestamps are taken immediately after AF_PACKET recv(). The capture
# process polls for at most 200 ms; 50 ms more covers bounded scheduling jitter
# under the independently enforced 100-ms sampler and realtime-factor gates.
CAPTURE_OCCURRENCE_MATCH_TOLERANCE_NS = 250_000_000
# A CLOCK_REALTIME step/drift over one second during the frozen 600-second
# interval makes interpolation into the PCAP timestamp domain ambiguous.
CAPTURE_REALTIME_SPAN_TOLERANCE_NS = 1_000_000_000
ENDPOINTS = ("gcs", "uav1", "uav2", "uav3", "uav4", "uav5")
M3_CELL_IDS = {
    f"uav{uav}.{traffic_class}.{direction}"
    for uav in range(1, 6)
    for traffic_class in ("control", "payload", "additional_data")
    for direction in ("downlink", "uplink")
}
REQUIRED_CLOCK_PRODUCERS = (
    "clock_collector",
    "endpoint_gcs",
    "endpoint_uav1",
    "endpoint_uav2",
    "endpoint_uav3",
    "endpoint_uav4",
    "endpoint_uav5",
    "gcs_control_probe",
    "uav_control_adapter_uav1",
    "uav_control_adapter_uav2",
    "uav_control_adapter_uav3",
    "uav_control_adapter_uav4",
    "uav_control_adapter_uav5",
    "actual_endpoint_supervisor",
    "ns3_packet_engine",
    "ros_gazebo_tracker",
    "sionna_worker",
    "sionna_adapter",
    "raw_collector",
)
MANDATORY_CAPTURE_ROLES = (
    "endpoint-gcs",
    "endpoint-uav1",
    "endpoint-uav2",
    "endpoint-uav3",
    "endpoint-uav4",
    "endpoint-uav5",
    "ns3-external-gcs",
    "ns3-external-uav1",
    "ns3-external-uav2",
    "ns3-external-uav3",
    "ns3-external-uav4",
    "ns3-external-uav5",
    "tail-root-uav1",
    "tail-uav1",
    "tail-root-uav2",
    "tail-uav2",
    "tail-root-uav3",
    "tail-uav3",
    "tail-root-uav4",
    "tail-uav4",
    "tail-root-uav5",
    "tail-uav5",
)
REQUIRED_PROCESS_COUNTS = {
    "ros_launch": 1,
    "arducopter": 5,
    "mavproxy": 5,
    "micro_ros_agent": 5,
    "gazebo_launcher": 1,
    "gazebo_server": 1,
    "robot_state_publisher": 5,
    "ros_gz_parameter_bridge": 5,
    "topic_relay": 10,
    "endpoint_companion_agent": 6,
    "gcs_endpoint_probe": 1,
    "uav_endpoint_adapter": 5,
    "actual_endpoint_supervisor": 1,
    "ns3_packet_engine": 1,
    "sionna_worker": 1,
    "sionna_adapter": 1,
    "packet_capture": len(MANDATORY_CAPTURE_ROLES),
    "runtime_collector": 1,
    "clock_collector": 1,
}
CLOCK_PRODUCER_PROCESS_ROLES = {
    "clock_collector": "clock_collector",
    "endpoint_gcs": "endpoint_companion_agent",
    "endpoint_uav1": "endpoint_companion_agent",
    "endpoint_uav2": "endpoint_companion_agent",
    "endpoint_uav3": "endpoint_companion_agent",
    "endpoint_uav4": "endpoint_companion_agent",
    "endpoint_uav5": "endpoint_companion_agent",
    "gcs_control_probe": "gcs_endpoint_probe",
    "uav_control_adapter_uav1": "uav_endpoint_adapter",
    "uav_control_adapter_uav2": "uav_endpoint_adapter",
    "uav_control_adapter_uav3": "uav_endpoint_adapter",
    "uav_control_adapter_uav4": "uav_endpoint_adapter",
    "uav_control_adapter_uav5": "uav_endpoint_adapter",
    "actual_endpoint_supervisor": "actual_endpoint_supervisor",
    "ns3_packet_engine": "ns3_packet_engine",
    "ros_gazebo_tracker": "sionna_adapter",
    "sionna_worker": "sionna_worker",
    "sionna_adapter": "sionna_adapter",
    "raw_collector": "runtime_collector",
}


def split_exact_mavlink_datagram(payload: bytes) -> list[dict[str, Any]]:
    """Split one UDP payload into an exact ordered MAVLink v1/v2 frame list.

    This is deliberately independent of pymavlink and has no cross-datagram
    parser state.  Message-specific CRC validation remains the caller's job,
    because validators intentionally lock different accepted dialect subsets.
    """

    if type(payload) is not bytes or not payload or len(payload) > 65_507:  # noqa: E721
        raise M4ValidationError("actual-control MAVLink datagram size/type differs")
    frames: list[dict[str, Any]] = []
    offset = 0
    while offset < len(payload):
        magic = payload[offset]
        if magic == 0xFD:
            if offset + 12 > len(payload):
                raise M4ValidationError("MAVLink v2 datagram frame is truncated")
            body_size = payload[offset + 1]
            incompat_flags = payload[offset + 2]
            if incompat_flags & ~0x01:
                raise M4ValidationError("MAVLink v2 incompatibility flags differ")
            signed_size = 13 if incompat_flags & 0x01 else 0
            frame_size = 12 + body_size + signed_size
            sequence = payload[offset + 4]
            system_id = payload[offset + 5]
            component_id = payload[offset + 6]
            message_id = int.from_bytes(payload[offset + 7 : offset + 10], "little")
            version = 2
        elif magic == 0xFE:
            if offset + 8 > len(payload):
                raise M4ValidationError("MAVLink v1 datagram frame is truncated")
            body_size = payload[offset + 1]
            frame_size = 8 + body_size
            sequence = payload[offset + 2]
            system_id = payload[offset + 3]
            component_id = payload[offset + 4]
            message_id = payload[offset + 5]
            version = 1
        else:
            raise M4ValidationError("non-MAVLink byte occurs in actual-control datagram")
        end = offset + frame_size
        if end > len(payload):
            raise M4ValidationError("MAVLink datagram ends inside a frame")
        frame = payload[offset:end]
        frames.append(
            {
                "version": version,
                "offset": offset,
                "size": frame_size,
                "sequence": sequence,
                "system_id": system_id,
                "component_id": component_id,
                "message_id": message_id,
                "bytes": frame,
                "sha256": hashlib.sha256(frame).hexdigest(),
            }
        )
        offset = end
    if not frames:
        raise M4ValidationError("actual-control datagram contains no MAVLink frame")
    for left, right in zip(frames, frames[1:]):
        if right["sequence"] != (left["sequence"] + 1) & 0xFF:
            raise M4ValidationError(
                "MAVLink frames inside one UDP datagram are not consecutive"
            )
    return frames


def index_actual_control_datagrams(
    records: Iterable[Mapping[str, Any]],
    *,
    expected_peers: Mapping[tuple[str, int], int],
    expected_rx_tos: int,
) -> dict[tuple[int, str, int], list[dict[str, Any]]]:
    """Recompute full inbound UDP parents for later exact frame joins."""

    index: dict[tuple[int, str, int], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        if record.get("event") != "control_datagram_receive":
            continue
        encoded = record.get("transport_payload_hex")
        if not isinstance(encoded, str) or len(encoded) > 131_014:
            raise M4ValidationError("control datagram lacks bounded preserved bytes")
        try:
            payload = bytes.fromhex(encoded)
        except ValueError as exc:
            raise M4ValidationError("control datagram hex differs") from exc
        frames = split_exact_mavlink_datagram(payload)
        peer = (record.get("peer_ip"), record.get("peer_udp_port"))
        uav = expected_peers.get(peer)  # type: ignore[arg-type]
        received_ns = record.get("received_monotonic_ns")
        digest = hashlib.sha256(payload).hexdigest()
        if (
            uav is None
            or isinstance(received_ns, bool)
            or not isinstance(received_ns, int)
            or record.get("rx_tos") != expected_rx_tos
            or record.get("transport_payload_sha256") != digest
            or record.get("transport_payload_size") != len(payload)
            or record.get("decoded_message_count") != len(frames)
            or any(
                frame["system_id"] != uav or frame["component_id"] != 1
                for frame in frames
            )
        ):
            raise M4ValidationError("control datagram parent identity/route differs")
        parent = {
            "event_sequence": record.get("event_sequence"),
            "received_monotonic_ns": received_ns,
            "uav": uav,
            "peer_ip": peer[0],
            "peer_udp_port": peer[1],
            "rx_tos": expected_rx_tos,
            "payload": payload,
            "sha256": digest,
            "frames": frames,
        }
        index[(received_ns, digest, uav)].append(parent)
    return dict(index)


def bind_actual_control_frame(
    record: Mapping[str, Any],
    *,
    expected_message_id: int,
    datagram_index: Mapping[tuple[int, str, int], list[dict[str, Any]]],
    consumed_occurrences: set[tuple[int, int]],
    frame_decoder: Callable[[bytes], Mapping[str, Any]],
) -> tuple[dict[str, Any], Mapping[str, Any]]:
    """Bind a derived message event to one exact frame in one raw UDP parent."""

    received_ns = record.get("received_monotonic_ns")
    digest = record.get("transport_payload_sha256")
    uav = record.get("uav")
    if (
        isinstance(received_ns, bool)
        or not isinstance(received_ns, int)
        or not isinstance(digest, str)
        or isinstance(uav, bool)
        or not isinstance(uav, int)
    ):
        raise M4ValidationError("derived control event parent key differs")
    parents = datagram_index.get((received_ns, digest, uav), [])
    if len(parents) != 1:
        raise M4ValidationError("derived control event lacks one exact UDP parent")
    parent = parents[0]
    parent_sequence = parent.get("event_sequence")
    event_sequence = record.get("event_sequence")
    if (
        isinstance(parent_sequence, bool)
        or not isinstance(parent_sequence, int)
        or isinstance(event_sequence, bool)
        or not isinstance(event_sequence, int)
        or event_sequence <= parent_sequence
        or record.get("peer_ip") != parent["peer_ip"]
        or record.get("peer_udp_port") != parent["peer_udp_port"]
        or record.get("source_system") != uav
        or record.get("source_component") != 1
    ):
        raise M4ValidationError("derived control event/UDP parent lineage differs")
    encoded = record.get("mavlink_frame_hex")
    if not isinstance(encoded, str) or len(encoded) > 1024:
        raise M4ValidationError("derived control event frame bytes differ")
    try:
        frame_bytes = bytes.fromhex(encoded)
    except ValueError as exc:
        raise M4ValidationError("derived control event frame hex differs") from exc
    matching = [
        frame
        for frame in parent["frames"]
        if frame["bytes"] == frame_bytes
        and frame["message_id"] == expected_message_id
    ]
    if len(matching) != 1:
        raise M4ValidationError(
            "derived control frame does not occur exactly once in its UDP parent"
        )
    if (
        record.get("mavlink_frame_sha256") != matching[0]["sha256"]
        or record.get("mavlink_frame_size") != matching[0]["size"]
    ):
        raise M4ValidationError(
            "derived control frame hash/size differs from its raw UDP occurrence"
        )
    occurrence = (parent_sequence, int(matching[0]["offset"]))
    if occurrence in consumed_occurrences:
        raise M4ValidationError("raw control frame occurrence was consumed twice")
    decoded = dict(frame_decoder(frame_bytes))
    if decoded.get("message_id") != expected_message_id:
        raise M4ValidationError("derived control frame message identity differs")
    consumed_occurrences.add(occurrence)
    return decoded, parent


def index_exact_ns3_unicast_deliveries(
    records: Iterable[Mapping[str, Any]],
    *,
    expected_event_epoch: int,
    expected_config_sha256: str,
    states_by_hash: Mapping[str, Mapping[str, Any]],
) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    """Validate the engine envelope and index exact UID-owned deliveries."""

    packet_records = [dict(record) for record in records]
    by_uid: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    by_sequence: dict[int, dict[str, Any]] = {}
    previous_host = -1
    previous_sim = -1
    for ordinal, record in enumerate(packet_records, start=1):
        sequence = record.get("event_sequence")
        host = record.get("host_monotonic_ns")
        sim = record.get("sim_time_ns")
        uid = record.get("packet_uid")
        if (
            record.get("schema") != "ams.ns3.packet_event/v1"
            or record.get("event_epoch") != expected_event_epoch
            or record.get("config_sha256") != expected_config_sha256
            or sequence != ordinal
            or isinstance(host, bool)
            or not isinstance(host, int)
            or host < previous_host
            or isinstance(sim, bool)
            or not isinstance(sim, int)
            or sim < previous_sim
            or isinstance(uid, bool)
            or not isinstance(uid, int)
            or uid < 0
            or record.get("event")
            not in {"ingress", "enqueue", "dequeue", "channel", "egress", "drop"}
        ):
            raise M4ValidationError(
                f"ns-3 packet event envelope differs at record {ordinal}"
            )
        previous_host, previous_sim = host, sim
        by_sequence[sequence] = record
        by_uid[(expected_event_epoch, uid)].append(record)

    indexed: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    stable_radio_fields = {
        "radio_state_sequence",
        "radio_state_sha256",
        "radio_query_id",
        "radio_applied_state_id",
        "radio_result_wire_sha256",
        "radio_mapping_version",
        "radio_mapping_seed",
        "radio_delay_ns",
        "radio_service_rate_bps",
        "radio_loss_probability",
        "radio_loss_sample",
        "radio_intervention",
        "radio_validity_start_monotonic_ns",
        "radio_adapter_applied_monotonic_ns",
        "radio_expires_monotonic_ns",
    }
    immutable_fields = {
        "event_epoch",
        "packet_uid",
        "tos",
        "dscp",
        "traffic_class",
        "directed_link",
        "queue_id",
        "source_ip",
        "destination_ip",
        "transport_protocol",
        "source_udp_port",
        "destination_udp_port",
        "transport_payload_sha256",
        "transport_payload_size",
        "p2mp",
        "root_transmission",
        "config_sha256",
        "seed",
        "run",
    }
    for uid_key, chain in by_uid.items():
        kinds = [str(record["event"]) for record in chain]
        if "egress" not in kinds or any(record.get("p2mp") is True for record in chain):
            continue
        if kinds != ["ingress", "enqueue", "dequeue", "channel", "egress"]:
            raise M4ValidationError(
                f"delivered ns-3 UID chain shape differs: {uid_key}/{kinds}"
            )
        ingress, enqueue, dequeue, channel, egress = chain
        if ingress.get("traffic_class") != "control":
            continue
        if any(
            record.get(field) != ingress.get(field)
            for record in chain[1:]
            for field in immutable_fields
        ):
            raise M4ValidationError(f"ns-3 UID immutable identity differs: {uid_key}")
        link = str(ingress.get("directed_link"))
        if ">" not in link:
            raise M4ValidationError("ns-3 UID directed link differs")
        source, destination = link.split(">", 1)
        if [record.get("device_id") for record in chain] != [
            f"{source}.tap.ingress",
            f"{source}.radio",
            f"{source}.radio",
            f"{source}.radio",
            f"{destination}.tap.egress",
        ]:
            raise M4ValidationError(f"ns-3 UID physical device path differs: {uid_key}")
        if (
            ingress.get("traffic_class") != "control"
            or ingress.get("queue_id") != f"{link}.control.q0"
            or ingress.get("tos") != 184
            or ingress.get("dscp") != 46
            or ingress.get("transport_protocol") != 17
            or any(
                record.get("radio_state_status") != "fresh"
                or record.get("radio_delivery") != "deliver"
                for record in chain[1:]
            )
            or any(
                record.get(field) != enqueue.get(field)
                for record in chain[2:]
                for field in stable_radio_fields
            )
            or enqueue.get("packet_wire_hash") != dequeue.get("packet_wire_hash")
            or enqueue.get("packet_wire_hash") != channel.get("packet_wire_hash")
        ):
            raise M4ValidationError(f"ns-3 UID radio delivery differs: {uid_key}")
        state_hash = enqueue.get("radio_state_sha256")
        state = states_by_hash.get(str(state_hash))
        source_sequence = state.get("source_packet_event_sequence") if state else None
        source_event = by_sequence.get(source_sequence) if isinstance(source_sequence, int) else None
        if (
            not isinstance(state, Mapping)
            or state.get("availability") != "fresh"
            or source_event is None
            or source_event.get("event") != "ingress"
            or source_event.get("event_sequence", 1 << 63)
            >= enqueue.get("event_sequence", -1)
            or state.get("source_packet_event_epoch")
            != source_event.get("event_epoch")
            or state.get("source_packet_uid") != source_event.get("packet_uid")
            or state.get("source_packet_causal_sha256")
            != source_event.get("transport_payload_sha256")
            or state.get("directed_link") != source_event.get("directed_link")
            or state.get("traffic_class") != source_event.get("traffic_class")
            or state.get("directed_link") != link
            or state.get("traffic_class") != "control"
        ):
            raise M4ValidationError(f"ns-3 UID applied-state parent differs: {uid_key}")
        effects = state.get("effects")
        packet_to_state = {
            "radio_state_sequence": "state_sequence",
            "radio_query_id": "query_id",
            "radio_applied_state_id": "applied_state_id",
            "radio_result_wire_sha256": "result_wire_sha256",
        }
        packet_to_effect = {
            "radio_mapping_version": "mapping_version",
            "radio_mapping_seed": "mapping_seed",
            "radio_delay_ns": "propagation_delay_ns",
            "radio_service_rate_bps": "service_rate_bps",
            "radio_loss_probability": "loss_probability",
            "radio_intervention": "intervention",
        }
        digest = ingress.get("transport_payload_sha256")
        observed_loss_sample = enqueue.get("radio_loss_sample")
        observed_loss_probability = enqueue.get("radio_loss_probability")
        intervention = enqueue.get("radio_intervention")
        if (
            not isinstance(effects, Mapping)
            or any(
                enqueue.get(packet_key) != state.get(state_key)
                for packet_key, state_key in packet_to_state.items()
            )
            or any(
                enqueue.get(packet_key) != effects.get(effect_key)
                for packet_key, effect_key in packet_to_effect.items()
            )
            or not isinstance(digest, str)
            or HEX64.fullmatch(digest) is None
            or not finite_number(observed_loss_sample)
            or not finite_number(observed_loss_probability)
            or abs(
                float(observed_loss_sample)
                - deterministic_loss_sample(
                    digest,
                    str(state.get("applied_state_id")),
                    int(effects.get("mapping_seed", -1)),
                )
            )
            > 1e-15
            or intervention not in {"natural", "force_deliver"}
            or (
                intervention == "natural"
                and float(observed_loss_sample) < float(observed_loss_probability)
            )
            or not isinstance(enqueue.get("host_monotonic_ns"), int)
            or not int(state.get("validity_start_monotonic_ns", 1 << 63))
            <= int(enqueue["host_monotonic_ns"])
            < int(state.get("expires_monotonic_ns", -1))
        ):
            raise M4ValidationError(f"ns-3 UID packet/state fields differ: {uid_key}")
        rate = channel.get("radio_service_rate_bps")
        wire_size = channel.get("packet_wire_size")
        if (
            isinstance(rate, bool)
            or not isinstance(rate, int)
            or rate <= 0
            or isinstance(wire_size, bool)
            or not isinstance(wire_size, int)
            or wire_size <= 0
        ):
            raise M4ValidationError(f"ns-3 UID service identity differs: {uid_key}")
        serialization_ns = (wire_size * 8 * 1_000_000_000 + rate - 1) // rate
        base_serialization_ns = (
            wire_size * 8 * 1_000_000_000 + 20_000_000 - 1
        ) // 20_000_000
        if (
            channel.get("radio_serialization_time_ns") != serialization_ns
            or channel.get("radio_base_serialization_time_ns")
            != base_serialization_ns
            or channel.get("radio_service_padding_ns")
            != max(0, serialization_ns - base_serialization_ns)
            or channel.get("radio_base_channel_delay_ns") != 2_000_000
            or channel.get("radio_effective_channel_delay_ns")
            != int(channel.get("radio_delay_ns", -1))
            + max(0, serialization_ns - base_serialization_ns)
            or not isinstance(channel.get("radio_rate_applied_at_monotonic_ns"), int)
            or channel.get("radio_delay_applied_at_monotonic_ns")
            != channel.get("radio_rate_applied_at_monotonic_ns")
            or channel.get("radio_applied_device_id") != f"{source}.radio"
            or channel.get("radio_rate_applied_at_monotonic_ns")
            > channel.get("host_monotonic_ns", -1)
        ):
            raise M4ValidationError(f"ns-3 UID service effects differ: {uid_key}")
        indexed[(link, "control", digest)].append(
            {
                "uid_key": uid_key,
                "ingress": ingress,
                "enqueue": enqueue,
                "dequeue": dequeue,
                "channel": channel,
                "egress": egress,
            }
        )
    for chains in indexed.values():
        chains.sort(
            key=lambda item: (
                int(item["ingress"]["host_monotonic_ns"]),
                int(item["ingress"]["event_sequence"]),
            )
        )
    return dict(indexed)


def index_exact_ns3_unicast_drops(
    records: Iterable[Mapping[str, Any]],
    *,
    expected_event_epoch: int,
    expected_config_sha256: str,
    states_by_hash: Mapping[str, Mapping[str, Any]],
    required_intervention: str | None = None,
) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    """Index exact Sionna-owned unicast drops by ns-3 packet occurrence.

    A packet can be rejected at enqueue by its fresh Sionna outcome, or it can
    be admitted with a fresh state and fail closed when that state expires or
    is superseded while queued.  Both shapes retain one UID and never egress.
    """

    packet_records = [dict(record) for record in records]
    by_uid: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    by_sequence: dict[int, dict[str, Any]] = {}
    for ordinal, record in enumerate(packet_records, start=1):
        uid = record.get("packet_uid")
        if (
            record.get("schema") != "ams.ns3.packet_event/v1"
            or record.get("event_epoch") != expected_event_epoch
            or record.get("config_sha256") != expected_config_sha256
            or record.get("event_sequence") != ordinal
            or isinstance(uid, bool)
            or not isinstance(uid, int)
            or uid < 0
        ):
            raise M4ValidationError(
                f"ns-3 dropped packet envelope differs at record {ordinal}"
            )
        by_sequence[ordinal] = record
        by_uid[(expected_event_epoch, uid)].append(record)

    immutable_fields = {
        "event_epoch",
        "packet_uid",
        "tos",
        "dscp",
        "traffic_class",
        "directed_link",
        "queue_id",
        "source_ip",
        "destination_ip",
        "transport_protocol",
        "source_udp_port",
        "destination_udp_port",
        "transport_payload_sha256",
        "transport_payload_size",
        "p2mp",
        "root_transmission",
        "config_sha256",
        "seed",
        "run",
    }
    stable_radio_fields = {
        "radio_state_sequence",
        "radio_state_sha256",
        "radio_query_id",
        "radio_applied_state_id",
        "radio_result_wire_sha256",
        "radio_mapping_version",
        "radio_mapping_seed",
        "radio_delay_ns",
        "radio_service_rate_bps",
        "radio_loss_probability",
        "radio_loss_sample",
        "radio_intervention",
        "radio_validity_start_monotonic_ns",
        "radio_adapter_applied_monotonic_ns",
        "radio_expires_monotonic_ns",
    }
    indexed: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for uid_key, chain in by_uid.items():
        if any(record.get("p2mp") is True for record in chain):
            continue
        kinds = [record.get("event") for record in chain]
        if not kinds or kinds[-1] != "drop" or "egress" in kinds:
            continue
        ingress = chain[0]
        if ingress.get("traffic_class") != "control":
            continue
        if kinds not in (["ingress", "drop"], ["ingress", "enqueue", "drop"]):
            raise M4ValidationError(
                f"dropped ns-3 UID chain shape differs: {uid_key}/{kinds}"
            )
        drop = chain[-1]
        decision = drop if len(chain) == 2 else chain[1]
        link = ingress.get("directed_link")
        if not isinstance(link, str) or link.count(">") != 1:
            raise M4ValidationError(f"dropped ns-3 UID link differs: {uid_key}")
        source, _destination = link.split(">", 1)
        if (
            any(
                record.get(field) != ingress.get(field)
                for record in chain[1:]
                for field in immutable_fields
            )
            or [record.get("device_id") for record in chain]
            != [f"{source}.tap.ingress", *([f"{source}.radio"] * (len(chain) - 1))]
            or ingress.get("queue_id") != f"{link}.control.q0"
            or ingress.get("tos") != 184
            or ingress.get("dscp") != 46
            or ingress.get("transport_protocol") != 17
        ):
            raise M4ValidationError(f"dropped ns-3 UID identity/path differs: {uid_key}")

        state = states_by_hash.get(str(decision.get("radio_state_sha256")))
        source_sequence = state.get("source_packet_event_sequence") if state else None
        source_event = (
            by_sequence.get(source_sequence)
            if isinstance(source_sequence, int) and not isinstance(source_sequence, bool)
            else None
        )
        effects = state.get("effects") if isinstance(state, Mapping) else None
        digest = ingress.get("transport_payload_sha256")
        packet_to_state = {
            "radio_state_sequence": "state_sequence",
            "radio_query_id": "query_id",
            "radio_applied_state_id": "applied_state_id",
            "radio_result_wire_sha256": "result_wire_sha256",
        }
        packet_to_effect = {
            "radio_mapping_version": "mapping_version",
            "radio_mapping_seed": "mapping_seed",
            "radio_delay_ns": "propagation_delay_ns",
            "radio_service_rate_bps": "service_rate_bps",
            "radio_loss_probability": "loss_probability",
            "radio_intervention": "intervention",
        }
        sample = decision.get("radio_loss_sample")
        probability = decision.get("radio_loss_probability")
        intervention = decision.get("radio_intervention")
        if (
            not isinstance(state, Mapping)
            or state.get("availability") != "fresh"
            or not isinstance(effects, Mapping)
            or source_event is None
            or source_event.get("event") != "ingress"
            or source_event.get("event_sequence", 1 << 63)
            >= decision.get("event_sequence", -1)
            or state.get("source_packet_event_epoch")
            != source_event.get("event_epoch")
            or state.get("source_packet_uid") != source_event.get("packet_uid")
            or state.get("source_packet_causal_sha256")
            != source_event.get("transport_payload_sha256")
            or state.get("directed_link") != source_event.get("directed_link")
            or state.get("traffic_class") != source_event.get("traffic_class")
            or state.get("directed_link") != link
            or state.get("traffic_class") != "control"
            or any(
                decision.get(packet_key) != state.get(state_key)
                for packet_key, state_key in packet_to_state.items()
            )
            or any(
                decision.get(packet_key) != effects.get(effect_key)
                for packet_key, effect_key in packet_to_effect.items()
            )
            or not isinstance(digest, str)
            or HEX64.fullmatch(digest) is None
            or not finite_number(sample)
            or not finite_number(probability)
            or abs(
                float(sample)
                - deterministic_loss_sample(
                    digest,
                    str(state.get("applied_state_id")),
                    int(effects.get("mapping_seed", -1)),
                )
            )
            > 1e-15
            or intervention not in {"natural", "force_drop", "force_deliver"}
            or (
                required_intervention is not None
                and intervention != required_intervention
            )
            or not isinstance(decision.get("host_monotonic_ns"), int)
            or not int(state.get("validity_start_monotonic_ns", 1 << 63))
            <= int(decision["host_monotonic_ns"])
            < int(state.get("expires_monotonic_ns", -1))
        ):
            raise M4ValidationError(
                f"dropped ns-3 UID packet/state lineage differs: {uid_key}"
            )

        if len(chain) == 2:
            rate = int(effects.get("service_rate_bps", -1))
            expected_drop = (
                rate == 0
                or intervention == "force_drop"
                or (
                    intervention == "natural"
                    and float(sample) < float(probability)
                )
            )
            expected_reason = (
                "sionna_service_rate_zero" if rate == 0 else "sionna_loss"
            )
            if (
                not expected_drop
                or drop.get("radio_state_status") != "fresh"
                or drop.get("radio_delivery") != "drop"
                or drop.get("drop_reason") != expected_reason
            ):
                raise M4ValidationError(
                    f"initial Sionna drop outcome differs: {uid_key}"
                )
        else:
            enqueue = chain[1]
            expected_delivery = (
                int(effects.get("service_rate_bps", -1)) > 0
                and intervention != "force_drop"
                and (
                    intervention == "force_deliver"
                    or float(sample) >= float(probability)
                )
            )
            status = drop.get("radio_state_status")
            status_reason = {
                "expired_in_queue": "sionna_state_expired_in_queue",
                "superseded_in_queue": "sionna_state_superseded_in_queue",
                "unavailable_in_queue": "sionna_state_unavailable_in_queue",
            }
            if (
                not expected_delivery
                or enqueue.get("radio_state_status") != "fresh"
                or enqueue.get("radio_delivery") != "deliver"
                or any(
                    drop.get(field) != enqueue.get(field)
                    for field in stable_radio_fields
                )
                or status not in status_reason
                or drop.get("radio_delivery") != "drop"
                or drop.get("drop_reason") != status_reason.get(status)
            ):
                raise M4ValidationError(
                    f"queued Sionna drop outcome differs: {uid_key}"
                )
        indexed[(link, "control", str(digest))].append(
            {
                "uid_key": uid_key,
                "ingress": ingress,
                "decision": decision,
                "drop": drop,
                "egress": None,
            }
        )
    for chains in indexed.values():
        chains.sort(
            key=lambda item: (
                int(item["ingress"]["host_monotonic_ns"]),
                int(item["ingress"]["event_sequence"]),
            )
        )
    return dict(indexed)


def sha256_file(path: Path) -> str:
    if not regular_file(path):
        raise M4ValidationError(f"missing/nonregular/hardlinked file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def nearest_rank(values: Iterable[float], fraction: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered or not 0.0 < fraction <= 1.0:
        raise M4ValidationError("percentile input is empty or invalid")
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def validate_scene_prerequisite(
    bound_bundle_id: Any, bound_bundle_sha256: Any
) -> tuple[dict[str, Any], list[str]]:
    """Re-run the independent scene validator; do not trust a producer PASS."""

    failures: list[str] = []
    try:
        from network.validation.validate_m4_scene_bundle import validate_scene_bundle

        result = validate_scene_bundle(FROZEN_BUNDLE_PATH, ROOT)
        failed_gates = [
            name for name, item in result.get("gates", {}).items() if item.get("passed") is not True
        ]
        if (
            result.get("status") != "PASS"
            or result.get("failures") != []
            or len(result.get("gates", {})) != 13
            or failed_gates
        ):
            failures.append(
                "independent canonical scene validation failed: "
                + "; ".join(result.get("failures", [])[:5])
            )
        if result.get("bundle_id") != FROZEN_BUNDLE_ID:
            failures.append("independent scene bundle_id differs from frozen identity")
        if result.get("bundle_sha256") != FROZEN_BUNDLE_SHA256:
            failures.append("independent scene bundle SHA-256 differs from frozen identity")
        if bound_bundle_id != FROZEN_BUNDLE_ID:
            failures.append("run contract does not bind the frozen bundle_id")
        if bound_bundle_sha256 != FROZEN_BUNDLE_SHA256:
            failures.append("run contract does not bind the frozen bundle SHA-256")
        return result, failures
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {}, [f"independent scene validation cannot run: {exc}"]


def load_runtime_events(
    path: Path, *, run_id: str, runtime_id: str
) -> list[dict[str, Any]]:
    records = strict_jsonl(path, max_line_bytes=2 * 1024 * 1024)
    previous_sequence = 0
    previous_host = -1
    for index, record in enumerate(records, start=1):
        if record.get("schema") != RUNTIME_EVENT_SCHEMA:
            raise M4ValidationError(f"runtime event {index} schema mismatch")
        if record.get("run_id") != run_id or record.get("runtime_id") != runtime_id:
            raise M4ValidationError(f"runtime event {index} identity mismatch")
        sequence = record.get("event_sequence")
        host = record.get("host_monotonic_ns")
        realtime = record.get("host_realtime_ns")
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence != previous_sequence + 1
            or isinstance(host, bool)
            or not isinstance(host, int)
            or host <= previous_host
            or isinstance(realtime, bool)
            or not isinstance(realtime, int)
            or realtime <= 0
            or not isinstance(record.get("event"), str)
        ):
            raise M4ValidationError(f"runtime event {index} ordering/clock is invalid")
        previous_sequence = sequence
        previous_host = host
    return records


def _one_event(records: Iterable[Mapping[str, Any]], event: str) -> Mapping[str, Any]:
    matches = [record for record in records if record.get("event") == event]
    if len(matches) != 1:
        raise M4ValidationError(f"expected exactly one {event}, observed {len(matches)}")
    return matches[0]


def _interpolate_sim_ns(
    clocks: list[tuple[int, int]], boundary_ns: int, *, maximum_gap_ns: int
) -> float:
    hosts = [sample[0] for sample in clocks]
    position = bisect.bisect_left(hosts, boundary_ns)
    if position == 0 or position >= len(clocks):
        raise M4ValidationError("Gazebo clocks do not bracket a measurement boundary")
    before_host, before_sim = clocks[position - 1]
    after_host, after_sim = clocks[position]
    if not 0 < after_host - before_host <= maximum_gap_ns or after_sim < before_sim:
        raise M4ValidationError("Gazebo clock interpolation gap/order is invalid")
    fraction = (boundary_ns - before_host) / (after_host - before_host)
    return before_sim + fraction * (after_sim - before_sim)


def _pose_binding_vector(value: Any, size: int) -> tuple[float, ...] | None:
    if (
        not isinstance(value, list)
        or len(value) != size
        or any(not finite_number(item) for item in value)
    ):
        return None
    return tuple(float(item) for item in value)


def validate_query_pose_runtime_binding(
    pose_records: list[dict[str, Any]],
    wire: Mapping[str, Any],
    runtime_records: list[dict[str, Any]],
    pose_observation_path: Path,
    *,
    run_id: str,
    runtime_id: str,
    start_monotonic_ns: int | None = None,
    end_monotonic_ns: int | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Bind every referenced adapter pose to an independent ROS observation.

    The adapter pose log and the query wire share a process and therefore only
    prove internal consistency.  This check joins each referenced raw entity to
    the collector's separate subscription by exact source header stamp and pose,
    then maps both subscriber callback times onto the canonical Gazebo clock.
    """

    failures: list[str] = []
    referenced_query_count = 0
    references: set[tuple[int, str, int]] = set()
    messages = wire.get("messages")
    if not isinstance(messages, list):
        messages = []
        failures.append("pose/runtime binding wire messages are absent")
    for message in messages:
        if not isinstance(message, Mapping) or message.get("message_type") != "query":
            continue
        sent_ns = message.get("request_sent_monotonic_ns")
        if start_monotonic_ns is not None and (
            isinstance(sent_ns, bool)
            or not isinstance(sent_ns, int)
            or sent_ns < start_monotonic_ns
        ):
            continue
        if end_monotonic_ns is not None and (
            isinstance(sent_ns, bool)
            or not isinstance(sent_ns, int)
            or sent_ns >= end_monotonic_ns
        ):
            continue
        referenced_query_count += 1
        sequence = message.get("node_state_seq")
        digest = message.get("node_state_sha256")
        snapshot_ns = message.get("node_state_snapshot_monotonic_ns")
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence <= 0
            or not isinstance(digest, str)
            or HEX64.fullmatch(digest) is None
            or isinstance(snapshot_ns, bool)
            or not isinstance(snapshot_ns, int)
            or snapshot_ns <= 0
        ):
            failures.append(
                f"query {message.get('query_id')} pose reference is invalid"
            )
            continue
        references.add((sequence, digest, snapshot_ns))
    if referenced_query_count == 0:
        failures.append("pose/runtime binding has no query in the requested window")

    snapshots: dict[
        tuple[int, str, int], list[Mapping[str, Any]]
    ] = defaultdict(list)
    for record in pose_records:
        if not isinstance(record, Mapping):
            continue
        sequence = record.get("node_state_seq")
        digest = record.get("node_state_sha256")
        snapshot_ns = record.get("snapshot_monotonic_ns")
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or not isinstance(digest, str)
            or HEX64.fullmatch(digest) is None
            or isinstance(snapshot_ns, bool)
            or not isinstance(snapshot_ns, int)
        ):
            continue
        snapshots[(sequence, digest, snapshot_ns)].append(record)

    clocks: list[tuple[int, int]] = []
    for number, record in enumerate(runtime_records, start=1):
        if record.get("event") != "gazebo_clock_sample":
            continue
        callback_ns = record.get("source_callback_monotonic_ns")
        emitted_ns = record.get("host_monotonic_ns")
        sim_ns = record.get("sim_time_ns")
        if (
            record.get("clock_topic") != "/uav1/clock"
            or isinstance(callback_ns, bool)
            or not isinstance(callback_ns, int)
            or callback_ns <= 0
            or isinstance(emitted_ns, bool)
            or not isinstance(emitted_ns, int)
            or not 0 <= emitted_ns - callback_ns <= POSE_BINDING_EMIT_DELAY_NS
            or isinstance(sim_ns, bool)
            or not isinstance(sim_ns, int)
            or sim_ns < 0
        ):
            failures.append(f"canonical Gazebo clock sample {number} differs")
            continue
        clocks.append((callback_ns, sim_ns))
    if len(clocks) < 2 or any(
        clocks[index][0] <= clocks[index - 1][0]
        or clocks[index][1] < clocks[index - 1][1]
        for index in range(1, len(clocks))
    ):
        failures.append("canonical Gazebo clock stream is absent/nonmonotonic")

    clock_interpolation_count = 0
    bound_entity_count = 0
    used_pose_observations: set[int] = set()
    clock_hosts = [sample[0] for sample in clocks]

    def clock_consistent(callback_ns: int, source_stamp_ns: int) -> bool:
        nonlocal clock_interpolation_count
        position = bisect.bisect_left(clock_hosts, callback_ns)
        if position == 0 or position >= len(clocks):
            return False
        before_host, before_sim = clocks[position - 1]
        after_host, after_sim = clocks[position]
        host_gap_ns = after_host - before_host
        if (
            not 0 < host_gap_ns <= POSE_BINDING_CLOCK_MAX_GAP_NS
            or after_sim < before_sim
        ):
            return False
        fraction = (callback_ns - before_host) / host_gap_ns
        interpolated_ns = before_sim + fraction * (after_sim - before_sim)
        lag_ns = interpolated_ns - source_stamp_ns
        if not (
            -POSE_BINDING_CLOCK_FUTURE_TOLERANCE_NS
            <= lag_ns
            <= MAX_POSE_AGE_NS
        ):
            return False
        clock_interpolation_count += 1
        return True

    binding_requests: list[dict[str, Any]] = []
    required_observation_keys: set[tuple[Any, ...]] = set()
    for reference in sorted(references):
        candidates = snapshots.get(reference, [])
        if len(candidates) != 1:
            failures.append(
                "referenced pose snapshot cardinality differs: "
                f"seq={reference[0]} observed={len(candidates)}"
            )
            continue
        snapshot = candidates[0]
        snapshot_ns = reference[2]
        nodes = snapshot.get("nodes")
        jammers = snapshot.get("jammers")
        if not isinstance(nodes, list) or not isinstance(jammers, list):
            failures.append(
                f"referenced pose snapshot {reference[0]} entity arrays differ"
            )
            continue
        node_records = [item for item in nodes if isinstance(item, Mapping)]
        jammer_records = [item for item in jammers if isinstance(item, Mapping)]
        node_ids = [item.get("node_id") for item in node_records]
        jammer_ids = [item.get("jammer_id") for item in jammer_records]
        expected_nodes = set(POSE_BINDING_ENTITIES[:6])
        if (
            len(node_records) != 6
            or set(node_ids) != expected_nodes
            or len(set(node_ids)) != len(node_ids)
            or len(jammer_records) != 1
            or jammer_ids != ["jammer_m4"]
        ):
            failures.append(
                f"referenced pose snapshot {reference[0]} exact entity set differs"
            )
            continue
        entities = {
            str(item["node_id"]): item for item in node_records
        }
        entities["jammer_m4"] = jammer_records[0]

        for entity in POSE_BINDING_ENTITIES:
            raw = entities[entity]
            adapter_callback_ns = raw.get("pose_monotonic_ns")
            source_stamp_ns = raw.get("source_header_stamp_ns")
            source_transport = raw.get("source_transport")
            source_stamp_scope = raw.get("source_stamp_scope")
            position = _pose_binding_vector(raw.get("position_m"), 3)
            orientation = _pose_binding_vector(
                raw.get("orientation_quat_xyzw"), 4
            )
            world_entity = entity in {"cp", "jammer_m4"}
            child_parts = (
                [
                    part
                    for part in str(raw.get("source_child_frame", ""))
                    .strip("/")
                    .split("/")
                    if part
                ]
                if world_entity
                else []
            )
            raw_lineage_valid = (
                raw.get("source_frame") == "world"
                and raw.get("transform_version") == "enu-identity-v1"
                and (
                    (
                        world_entity
                        and raw.get("source_topic") == "/world/map/pose/info"
                        and source_transport == GAZEBO_POSE_SOURCE_TRANSPORT
                        and source_stamp_scope == GAZEBO_POSE_SOURCE_STAMP_SCOPE
                        and raw.get("source_header_frame") == ""
                        and child_parts
                        and child_parts[-1] == entity
                    )
                    or (
                        not world_entity
                        and raw.get("source_topic") == f"/{entity}/odometry"
                        and source_transport == ODOMETRY_SOURCE_TRANSPORT
                        and source_stamp_scope == ODOMETRY_SOURCE_STAMP_SCOPE
                        and raw.get("source_header_frame") == "odom"
                        and raw.get("source_child_frame") == "base_link"
                    )
                )
            )
            if (
                not raw_lineage_valid
                or isinstance(adapter_callback_ns, bool)
                or not isinstance(adapter_callback_ns, int)
                or adapter_callback_ns <= 0
                or not 0 <= snapshot_ns - adapter_callback_ns <= MAX_POSE_AGE_NS
                or isinstance(source_stamp_ns, bool)
                or not isinstance(source_stamp_ns, int)
                or source_stamp_ns < 0
                or position is None
                or orientation is None
            ):
                failures.append(
                    f"referenced pose snapshot {reference[0]} {entity} raw lineage differs"
                )
                continue

            adapter_clock_valid = clock_consistent(
                adapter_callback_ns, source_stamp_ns
            )
            if not adapter_clock_valid:
                failures.append(
                    f"{entity} source stamp is inconsistent with Gazebo clock "
                    "at adapter callback"
                )

            expected_event = (
                "world_pose_sample" if world_entity else "odometry_sample"
            )
            expected_runtime_frame = (
                "world" if world_entity else "ros_odometry_world_enu"
            )
            expected_runtime_transform = (
                "enu-identity-v1"
                if world_entity
                else "ams-m4-coordinate-frames-v1"
            )
            observation_key = pose_observation_exact_key(
                kind="w" if world_entity else "o",
                entity_id=entity,
                source_topic=str(raw["source_topic"]),
                source_transport=str(source_transport),
                source_stamp_scope=str(source_stamp_scope),
                source_frame=expected_runtime_frame,
                transform_version=expected_runtime_transform,
                source_header_frame=str(raw["source_header_frame"]),
                source_child_frame=str(raw["source_child_frame"]),
                sim_stamp_ns=source_stamp_ns,
                position_m=position,
                orientation_quat_xyzw=orientation,
            )
            required_observation_keys.add(observation_key)
            binding_requests.append(
                {
                    "entity": entity,
                    "adapter_callback_ns": adapter_callback_ns,
                    "source_stamp_ns": source_stamp_ns,
                    "observation_key": observation_key,
                    "adapter_clock_valid": adapter_clock_valid,
                    "expected_event": expected_event,
                }
            )

    observation_index: dict[
        tuple[Any, ...], list[tuple[int, int]]
    ] = {}
    stream_details: dict[str, Any] = {}
    try:
        observation_index, stream_details = scan_pose_observation_stream(
            pose_observation_path,
            run_id=run_id,
            runtime_id=runtime_id,
            required_keys=required_observation_keys,
        )
    except M4ValidationError as exc:
        failures.append(str(exc))

    collector_starts = [
        record
        for record in runtime_records
        if record.get("event") == "collector_start"
    ]
    collector_stops = [
        record
        for record in runtime_records
        if record.get("event") == "collector_stop"
    ]
    lifecycle_bound = False
    if len(collector_starts) != 1 or len(collector_stops) != 1:
        failures.append(
            "pose-observation collector lifecycle cardinality differs"
        )
    elif stream_details:
        collector_start = collector_starts[0]
        collector_stop = collector_stops[0]
        stream_contract = collector_start.get("pose_observation_stream")
        start_host_ns = collector_start.get("host_monotonic_ns")
        stop_host_ns = collector_stop.get("host_monotonic_ns")
        created_ns = stream_details.get("created_monotonic_ns")
        closed_ns = stream_details.get("closed_monotonic_ns")
        expected_stream_contract = {
            "path": POSE_OBSERVATION_STREAM_PATH.as_posix(),
            "schema": POSE_OBSERVATION_STREAM_SCHEMA,
            "encoding": POSE_OBSERVATION_STREAM_ENCODING,
            "created_monotonic_ns": created_ns,
            "main_odometry_sample_period_ns": 200_000_000,
        }
        if (
            stream_contract != expected_stream_contract
            or isinstance(start_host_ns, bool)
            or not isinstance(start_host_ns, int)
            or isinstance(stop_host_ns, bool)
            or not isinstance(stop_host_ns, int)
            or isinstance(created_ns, bool)
            or not isinstance(created_ns, int)
            or isinstance(closed_ns, bool)
            or not isinstance(closed_ns, int)
            or not created_ns <= start_host_ns < closed_ns <= stop_host_ns
            or collector_stop.get("pose_observation_count")
            != stream_details.get("observation_count")
            or collector_stop.get("pose_observation_content_sha256")
            != stream_details.get("content_sha256")
            or collector_stop.get("pose_observation_closed_monotonic_ns")
            != closed_ns
        ):
            failures.append(
                "pose-observation stream/main collector lifecycle binding differs"
            )
        else:
            lifecycle_bound = True

    for request in binding_requests:
        entity = str(request["entity"])
        adapter_callback_ns = int(request["adapter_callback_ns"])
        source_stamp_ns = int(request["source_stamp_ns"])
        adapter_clock_valid = bool(request["adapter_clock_valid"])
        exact_samples = observation_index.get(request["observation_key"], [])
        if not exact_samples:
            failures.append(
                f"{entity} has no exact independent runtime pose sample"
            )
            continue

        valid_samples: list[tuple[int, int]] = []
        for observation_sequence, collector_callback_ns in exact_samples:
            if (
                abs(collector_callback_ns - adapter_callback_ns)
                > MAX_POSE_AGE_NS
            ):
                continue
            if clock_consistent(collector_callback_ns, source_stamp_ns):
                valid_samples.append(
                    (observation_sequence, collector_callback_ns)
                )
        if not valid_samples or not adapter_clock_valid:
            failures.append(
                f"{entity} exact independent runtime pose sample is not clock-bound"
            )
            if valid_samples and not adapter_clock_valid:
                continue
            if not valid_samples:
                failures.append(
                    f"{entity} source stamp is inconsistent with Gazebo clock "
                    "at collector callback"
                )
                continue
        selected_number, _selected_callback = min(
            valid_samples,
            key=lambda item: abs(item[1] - adapter_callback_ns),
        )
        used_pose_observations.add(selected_number)
        bound_entity_count += 1

    details = {
        **stream_details,
        "referenced_query_count": referenced_query_count,
        "referenced_snapshot_count": len(references),
        "bound_entity_count": bound_entity_count,
        "independent_runtime_sample_count": len(used_pose_observations),
        "independent_pose_observation_count": len(used_pose_observations),
        "canonical_clock_sample_count": len(clocks),
        "clock_interpolation_count": clock_interpolation_count,
        "retained_exact_key_count": len(observation_index),
        "collector_lifecycle_bound": lifecycle_bound,
    }
    expected_bindings = len(references) * len(POSE_BINDING_ENTITIES)
    if references and bound_entity_count != expected_bindings:
        failures.append(
            "query pose/runtime binding cardinality differs: "
            f"expected={expected_bindings} observed={bound_entity_count}"
        )
    return details, failures


def validate_native_world_entity_observations(entities: Any) -> list[str]:
    """Require the exact native Gazebo lineage for every active M4 entity."""

    expected_entities = {"cp", "jammer_m4", *[f"uav{i}" for i in range(1, 6)]}
    if not isinstance(entities, Mapping) or set(entities) != expected_entities:
        return ["active Gazebo entity set differs"]
    expected_keys = {
        "last_host_ns",
        "sim_stamp_ns",
        "source_topic",
        "source_transport",
        "source_stamp_scope",
        "source_frame",
        "transform_version",
        "source_header_frame",
        "source_child_frame",
        "position_m",
        "orientation_quat_xyzw",
    }
    failures: list[str] = []
    for name in sorted(expected_entities):
        value = entities[name]
        if (
            not isinstance(value, Mapping)
            or set(value) != expected_keys
            or isinstance(value.get("last_host_ns"), bool)
            or not isinstance(value.get("last_host_ns"), int)
            or int(value["last_host_ns"]) <= 0
            or isinstance(value.get("sim_stamp_ns"), bool)
            or not isinstance(value.get("sim_stamp_ns"), int)
            or int(value["sim_stamp_ns"]) < 0
            or value.get("source_topic") != "/world/map/pose/info"
            or value.get("source_transport") != GAZEBO_POSE_SOURCE_TRANSPORT
            or value.get("source_stamp_scope")
            != GAZEBO_POSE_SOURCE_STAMP_SCOPE
            or value.get("source_frame") != "world"
            or value.get("transform_version") != "enu-identity-v1"
            or value.get("source_header_frame") != ""
            or value.get("source_child_frame") != name
            or not isinstance(value.get("position_m"), list)
            or len(value["position_m"]) != 3
            or not all(finite_number(item) for item in value["position_m"])
            or not isinstance(value.get("orientation_quat_xyzw"), list)
            or len(value["orientation_quat_xyzw"]) != 4
            or not all(
                finite_number(item) for item in value["orientation_quat_xyzw"]
            )
        ):
            failures.append(f"active Gazebo entity evidence differs: {name}")
    return failures


def validate_continuous_readiness_schedule(
    records: Iterable[Mapping[str, Any]],
    *,
    warmup_start_ns: int,
    measurement_start_ns: int,
    measurement_end_ns: int,
) -> tuple[dict[str, Any], list[str]]:
    """Validate every absolute one-second readiness slot without interpolation."""

    samples = [
        record
        for record in records
        if record.get("event") == "continuous_readiness_sample"
    ]
    failures: list[str] = []
    if (
        isinstance(warmup_start_ns, bool)
        or not isinstance(warmup_start_ns, int)
        or isinstance(measurement_start_ns, bool)
        or not isinstance(measurement_start_ns, int)
        or isinstance(measurement_end_ns, bool)
        or not isinstance(measurement_end_ns, int)
        or measurement_start_ns - warmup_start_ns != 30_000_000_000
        or measurement_end_ns - measurement_start_ns != 600_000_000_000
    ):
        return {
            "sample_count": len(samples),
            "warmup_sample_count": 0,
            "measurement_sample_count": 0,
        }, ["continuous readiness boundaries are not exact 30+600 seconds"]

    expected_count = 630
    if len(samples) != expected_count:
        failures.append(
            "continuous readiness sample count differs: "
            f"observed={len(samples)} expected={expected_count}"
        )

    warmup_count = sum(record.get("phase") == "warmup" for record in samples)
    measurement_count = sum(
        record.get("phase") == "measurement" for record in samples
    )
    for index, record in enumerate(samples):
        if index < 30:
            phase = "warmup"
            phase_index = index
            scheduled_ns = warmup_start_ns + phase_index * 1_000_000_000
            sample_index_valid = "sample_index" not in record
        else:
            phase = "measurement"
            phase_index = index - 30
            scheduled_ns = measurement_start_ns + phase_index * 1_000_000_000
            sample_index = record.get("sample_index")
            sample_index_valid = (
                isinstance(sample_index, int)
                and not isinstance(sample_index, bool)
                and sample_index == phase_index
            )
        host_ns = record.get("host_monotonic_ns")
        complete_readiness = all(
            record.get(field) is True
            for field in (
                "ready",
                "files_ready",
                "clocks_fresh",
                "clocks_coherent",
                "odometry_fresh",
                "world_poses_fresh",
            )
        )
        if (
            record.get("phase") != phase
            or record.get("scheduled_monotonic_ns") != scheduled_ns
            or not sample_index_valid
            or isinstance(host_ns, bool)
            or not isinstance(host_ns, int)
            or not scheduled_ns <= host_ns <= scheduled_ns + 100_000_000
            or not complete_readiness
        ):
            failures.append(
                "continuous readiness absolute slot differs: "
                f"series_index={index} phase={phase} phase_index={phase_index}"
            )

    return {
        "sample_count": len(samples),
        "warmup_sample_count": warmup_count,
        "measurement_sample_count": measurement_count,
    }, failures


def validate_capacity_runtime(
    records: list[dict[str, Any]],
    *,
    schedule: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    failures: list[str] = []
    details: dict[str, Any] = {}
    warmup_start_ns = 0
    start_ns = 0
    end_ns = 0
    try:
        readiness = _one_event(records, "readiness_complete")
        warmup_start = _one_event(records, "warmup_start")
        warmup_end = _one_event(records, "warmup_end")
        measurement_start = _one_event(records, "measurement_start")
        measurement_end = _one_event(records, "measurement_end")
        expected_schedule = {
            "readiness_deadline_monotonic_ns",
            "warmup_start_monotonic_ns",
            "measurement_start_monotonic_ns",
            "measurement_end_monotonic_ns",
            "readiness_stability_ns",
            "warmup_ns",
            "measurement_ns",
            "rtf_window_ns",
            "rtf_window_count",
            "rtf_passing_minimum",
        }
        if set(schedule) != expected_schedule:
            raise M4ValidationError("predeclared capacity schedule shape differs")
        warmup_start_ns = int(schedule["warmup_start_monotonic_ns"])
        warmup_target_ns = warmup_start_ns + 30_000_000_000
        start_ns = int(schedule["measurement_start_monotonic_ns"])
        end_ns = int(schedule["measurement_end_monotonic_ns"])
        stable_since = readiness.get("stable_since_monotonic_ns")
        if (
            schedule.get("readiness_stability_ns") != 10_000_000_000
            or schedule.get("warmup_ns") != 30_000_000_000
            or schedule.get("measurement_ns") != 600_000_000_000
            or schedule.get("rtf_window_ns") != 1_000_000_000
            or schedule.get("rtf_window_count") != 600
            or schedule.get("rtf_passing_minimum") != 570
            or start_ns != warmup_target_ns
            or end_ns != start_ns + 600_000_000_000
            or schedule.get("readiness_deadline_monotonic_ns") != warmup_start_ns
            or isinstance(stable_since, bool)
            or not isinstance(stable_since, int)
            or stable_since > warmup_start_ns - 10_000_000_000
            or readiness["host_monotonic_ns"] > warmup_start_ns
            or not warmup_start_ns
            <= warmup_start["host_monotonic_ns"]
            <= warmup_start_ns + 100_000_000
            or warmup_start.get("target_end_monotonic_ns") != warmup_target_ns
            or warmup_end.get("target_monotonic_ns") != warmup_target_ns
            or not warmup_target_ns
            <= warmup_end["host_monotonic_ns"]
            <= warmup_target_ns + 250_000_000
            or not start_ns
            <= measurement_start["host_monotonic_ns"]
            <= start_ns + 100_000_000
            or measurement_start.get("target_end_monotonic_ns") != end_ns
            or measurement_end.get("target_monotonic_ns") != end_ns
            or measurement_end.get("clock_bracket_observed") is not True
            or not end_ns
            <= measurement_end["host_monotonic_ns"]
            <= end_ns + 500_000_000
        ):
            failures.append("readiness/warm-up/measurement is not exact 10+30+600 seconds")
    except (KeyError, TypeError, ValueError, M4ValidationError) as exc:
        failures.append(f"runtime schedule is incomplete: {exc}")

    rtf_values: list[float] = []
    aggregate: float | None = None
    if not failures:
        try:
            clocks = [
                (int(record["host_monotonic_ns"]), int(record["sim_time_ns"]))
                for record in records
                if record.get("event") == "gazebo_clock_sample"
            ]
            if len(clocks) < 6000 or any(
                clocks[index][0] <= clocks[index - 1][0]
                or clocks[index][1] < clocks[index - 1][1]
                for index in range(1, len(clocks))
            ):
                raise M4ValidationError("Gazebo clock stream is sparse/nonmonotonic")
            boundaries = [
                _interpolate_sim_ns(clocks, start_ns + index * 1_000_000_000, maximum_gap_ns=250_000_000)
                for index in range(601)
            ]
            rtf_values = [
                (boundaries[index + 1] - boundaries[index]) / 1_000_000_000
                for index in range(600)
            ]
            aggregate = (boundaries[-1] - boundaries[0]) / 600_000_000_000
        except (KeyError, TypeError, ValueError, M4ValidationError) as exc:
            failures.append(f"RTF cannot be independently derived: {exc}")
    passing = sum(0.95 <= value <= 1.05 for value in rtf_values)
    if (
        rtf_values
        and (
            len(rtf_values) != 600
            or passing < 570
            or aggregate is None
            or not 0.95 <= aggregate <= 1.05
        )
    ):
        failures.append("aggregate/95-percent one-second RTF gate failed")

    resources = [
        record
        for record in records
        if record.get("event") == "measurement_resource_sample"
    ]
    resource_hosts = [record.get("host_monotonic_ns") for record in resources]
    if (
        len(resources) != 600
        or [record.get("sample_index") for record in resources] != list(range(600))
        or any(not isinstance(value, int) or isinstance(value, bool) for value in resource_hosts)
        or any(
            record.get("scheduled_monotonic_ns") != start_ns + index * 1_000_000_000
            or abs(int(record.get("host_monotonic_ns", -1)) - (start_ns + index * 1_000_000_000))
            > 100_000_000
            for index, record in enumerate(resources)
        )
        or any(
            not 0 < resource_hosts[index] - resource_hosts[index - 1] <= 1_500_000_000
            for index in range(1, len(resource_hosts))
        )
    ):
        failures.append("resource evidence is not one complete bounded 600-s series")
    frozen_process_identities: set[tuple[Any, ...]] | None = None
    for index, record in enumerate(resources):
        process = record.get("processes")
        cgroup = record.get("cgroup")
        gpu = record.get("gpu")
        if (
            not isinstance(process, dict)
            or process.get("counts") != REQUIRED_PROCESS_COUNTS
            or process.get("required_counts") != REQUIRED_PROCESS_COUNTS
            or process.get("roles_exact") is not True
            or process.get("unclassified_count") != 0
            or process.get("process_count") != sum(REQUIRED_PROCESS_COUNTS.values())
            or not isinstance(cgroup, dict)
            or not isinstance(cgroup.get("cpu_stat"), dict)
            or not isinstance(gpu, dict)
            or gpu.get("available") is not True
            or not isinstance(gpu.get("gpus"), list)
            or len(gpu["gpus"]) != 1
        ):
            failures.append(f"resource/process/GPU sample {index} is incomplete")
            break
        process_identities = {
            (
                item.get("pid"),
                item.get("ppid"),
                item.get("pgid"),
                item.get("start_ticks"),
                item.get("role"),
                item.get("executable_path"),
                item.get("executable_sha256"),
                item.get("cmdline_sha256"),
            )
            for item in process.get("processes", [])
        }
        if len(process_identities) != sum(REQUIRED_PROCESS_COUNTS.values()):
            failures.append(f"resource process identity set {index} is ambiguous")
            break
        if frozen_process_identities is None:
            frozen_process_identities = process_identities
        elif process_identities != frozen_process_identities:
            failures.append(f"resource process identity set changed at sample {index}")
            break

    readiness_details, readiness_failures = validate_continuous_readiness_schedule(
        records,
        warmup_start_ns=warmup_start_ns,
        measurement_start_ns=start_ns,
        measurement_end_ns=end_ns,
    )
    failures.extend(readiness_failures)
    try:
        collector = _one_event(records, "collector_start")
        identity = collector.get("static_runtime_identity")
        if (
            not isinstance(identity, dict)
            or not identity.get("cpu_model")
            or not identity.get("cpu_online")
            or not identity.get("clocksource")
            or not identity.get("kernel")
            or not identity.get("container_runtime")
            or not isinstance(identity.get("governors"), dict)
            or identity.get("competing_load_policy") != "exclusive_simulation_and_gpu"
            or not isinstance(identity.get("gpu"), dict)
            or identity["gpu"].get("available") is not True
        ):
            failures.append("acceptance host/container/GPU/clock identity is incomplete")
    except M4ValidationError as exc:
        failures.append(str(exc))
    try:
        entity_event = _one_event(records, "runtime_entities_observed")
        entities = entity_event.get("entities")
        entity_failures = validate_native_world_entity_observations(entities)
        if entity_failures:
            raise M4ValidationError(entity_failures[0])
    except M4ValidationError as exc:
        failures.append(str(exc))

    details.update(
        {
            "measurement_start_monotonic_ns": start_ns,
            "measurement_end_monotonic_ns": end_ns,
            "aggregate_realtime_factor": aggregate,
            "rtf_window_count": len(rtf_values),
            "rtf_passing_window_count": passing,
            "minimum_rtf": min(rtf_values) if rtf_values else None,
            "maximum_rtf": max(rtf_values) if rtf_values else None,
            "resource_sample_count": len(resources),
            "continuous_readiness_sample_count": readiness_details["sample_count"],
            "warmup_readiness_sample_count": readiness_details[
                "warmup_sample_count"
            ],
            "measurement_readiness_sample_count": readiness_details[
                "measurement_sample_count"
            ],
            "frozen_process_identity_count": len(frozen_process_identities or ()),
        }
    )
    return details, failures


def validate_clock_correlations(
    path: Path,
    *,
    run_id: str,
    runtime_id: str,
    start_ns: int,
    end_ns: int,
) -> tuple[dict[str, Any], list[str]]:
    failures: list[str] = []
    try:
        records = strict_jsonl(path, max_line_bytes=16_384)
    except M4ValidationError as exc:
        return {}, [str(exc)]
    producer_pids: dict[str, int] = {}
    try:
        raw_path = path.parent / "m4_clock_datagrams.bin"
        index_path = path.parent / "m4_clock_datagram_index.jsonl"
        summary_path = path.parent.parent / "raw/m4_clock_collection_summary.json"
        if not regular_file(raw_path):
            raise M4ValidationError("raw clock datagram stream is absent")
        raw_stream = raw_path.read_bytes()
        index_records = strict_jsonl(index_path, max_line_bytes=16_384)
        if len(index_records) != len(records):
            raise M4ValidationError("clock raw/index/correlation record counts differ")
        offset = 0
        for position, (index, correlation) in enumerate(
            zip(index_records, records), start=1
        ):
            expected_index_keys = {
                "datagram_sequence",
                "offset",
                "length",
                "sha256",
                "collector_received_monotonic_ns",
                "producer",
                "producer_pid",
                "producer_sample_index",
            }
            length = index.get("length")
            digest = index.get("sha256")
            if (
                set(index) != expected_index_keys
                or index.get("datagram_sequence") != position
                or index.get("offset") != offset
                or isinstance(length, bool)
                or not isinstance(length, int)
                or not 1 <= length <= 4096
                or not isinstance(digest, str)
                or len(digest) != 64
                or offset + length > len(raw_stream)
            ):
                raise M4ValidationError(f"clock datagram index {position} differs")
            raw = raw_stream[offset : offset + length]
            offset += length
            if hashlib.sha256(raw).hexdigest() != digest:
                raise M4ValidationError(f"clock datagram {position} SHA-256 differs")
            try:
                datagram = json.loads(raw.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise M4ValidationError(
                    f"clock datagram {position} is invalid: {exc}"
                ) from exc
            expected_datagram_keys = {
                "schema",
                "sample_index",
                "producer",
                "producer_monotonic_ns",
                "producer_pid",
            }
            producer = datagram.get("producer") if isinstance(datagram, dict) else None
            pid = datagram.get("producer_pid") if isinstance(datagram, dict) else None
            received = index.get("collector_received_monotonic_ns")
            if (
                not isinstance(datagram, dict)
                or set(datagram) != expected_datagram_keys
                or datagram.get("schema") != "ams.m4.clock_datagram/v1"
                or producer != index.get("producer")
                or datagram.get("sample_index") != index.get("producer_sample_index")
                or pid != index.get("producer_pid")
                or not isinstance(pid, int)
                or isinstance(pid, bool)
                or pid <= 0
                or correlation.get("event_sequence") != position
                or correlation.get("producer") != producer
                or correlation.get("sample_index") != datagram.get("sample_index")
                or correlation.get("producer_monotonic_ns")
                != datagram.get("producer_monotonic_ns")
                or correlation.get("collector_host_monotonic_ns") != received
            ):
                raise M4ValidationError(
                    f"clock datagram {position} raw/index/correlation differs"
                )
            previous_pid = producer_pids.setdefault(str(producer), pid)
            if previous_pid != pid:
                raise M4ValidationError(f"clock producer {producer} PID changed")
        if offset != len(raw_stream):
            raise M4ValidationError("clock index does not cover exact raw stream")
        summary = strict_json(summary_path)
        if (
            summary.get("schema") != "ams.m4.clock_collection_summary/v1"
            or summary.get("run_id") != run_id
            or summary.get("runtime_id") != runtime_id
            or summary.get("producer_pids") != producer_pids
            or summary.get("producer_count") != len(REQUIRED_CLOCK_PRODUCERS)
            or summary.get("transport") != "AF_UNIX/SOCK_DGRAM"
        ):
            raise M4ValidationError("clock collection summary differs from raw evidence")
    except (OSError, TypeError, ValueError, M4ValidationError) as exc:
        failures.append(f"raw clock datagram evidence is invalid: {exc}")
    by_producer: dict[str, list[dict[str, Any]]] = defaultdict(list)
    previous_sequence = 0
    for number, record in enumerate(records, start=1):
        expected_keys = {
            "schema",
            "event_sequence",
            "run_id",
            "runtime_id",
            "producer",
            "sample_index",
            "producer_monotonic_ns",
            "collector_host_monotonic_ns",
        }
        sequence = record.get("event_sequence")
        producer = record.get("producer")
        if (
            set(record) != expected_keys
            or record.get("schema") != CLOCK_SAMPLE_SCHEMA
            or record.get("run_id") != run_id
            or record.get("runtime_id") != runtime_id
            or isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence != previous_sequence + 1
            or producer not in REQUIRED_CLOCK_PRODUCERS
        ):
            failures.append(f"clock sample {number} envelope is invalid")
            continue
        previous_sequence = sequence
        by_producer[str(producer)].append(record)
    if set(by_producer) != set(REQUIRED_CLOCK_PRODUCERS):
        failures.append(
            "clock producer set differs: "
            f"missing={sorted(set(REQUIRED_CLOCK_PRODUCERS)-set(by_producer))} "
            f"extra={sorted(set(by_producer)-set(REQUIRED_CLOCK_PRODUCERS))}"
        )
    metrics: dict[str, Any] = {}
    for producer in REQUIRED_CLOCK_PRODUCERS:
        samples = by_producer.get(producer, [])
        producer_times = [record.get("producer_monotonic_ns") for record in samples]
        host_times = [record.get("collector_host_monotonic_ns") for record in samples]
        if (
            len(samples) < 600
            or [record.get("sample_index") for record in samples] != list(range(len(samples)))
            or any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in producer_times + host_times
            )
            or any(
                not 0 < host_times[index] - host_times[index - 1] <= 1_500_000_000
                or producer_times[index] <= producer_times[index - 1]
                for index in range(1, len(samples))
            )
            or not samples
            or host_times[0] > start_ns + 1_500_000_000
            or host_times[-1] < end_ns - 1_500_000_000
        ):
            failures.append(f"{producer} clock samples are missing, sparse, or unordered")
            continue
        x0 = float(producer_times[0])
        y0 = float(host_times[0])
        xs = [float(value) - x0 for value in producer_times]
        ys = [float(value) - y0 for value in host_times]
        mean_x = sum(xs) / len(xs)
        mean_y = sum(ys) / len(ys)
        variance = sum((value - mean_x) ** 2 for value in xs)
        if variance <= 0.0:
            failures.append(f"{producer} affine clock fit is singular")
            continue
        slope = sum(
            (left - mean_x) * (right - mean_y) for left, right in zip(xs, ys)
        ) / variance
        intercept = mean_y - slope * mean_x
        residuals = [right - (intercept + slope * left) for left, right in zip(xs, ys)]
        maximum_residual = max(abs(value) for value in residuals)
        maximum_step = max(
            [abs(right - left) for left, right in zip(residuals, residuals[1:])] or [0.0]
        )
        drift_ppm = abs(slope - 1.0) * 1_000_000.0
        if maximum_residual > 2_000_000.0:
            failures.append(f"{producer} affine residual exceeds 2 ms")
        if drift_ppm > 100.0:
            failures.append(f"{producer} drift exceeds 100 ppm")
        if maximum_step > 5_000_000.0:
            failures.append(f"{producer} unexplained clock step exceeds 5 ms")
        metrics[producer] = {
            "sample_count": len(samples),
            "slope": slope,
            "drift_ppm": drift_ppm,
            "maximum_residual_ns": maximum_residual,
            "maximum_step_ns": maximum_step,
        }
    return {
        "producers": metrics,
        "producer_count": len(metrics),
        "producer_pids": producer_pids,
        "raw_datagram_count": len(records),
    }, failures


def validate_clock_process_binding(
    records: Iterable[Mapping[str, Any]],
    clock_details: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Bind every claimed producer PID to one frozen measured process."""

    failures: list[str] = []
    samples = [
        record
        for record in records
        if record.get("event")
        in {"measurement_resource_sample", "causal_resource_sample"}
    ]
    pids = clock_details.get("producer_pids")
    if not isinstance(pids, dict) or set(pids) != set(CLOCK_PRODUCER_PROCESS_ROLES):
        return {}, ["clock producer PID map differs from frozen process roles"]
    identities: dict[int, tuple[Any, ...]] = {}
    observed_counts: dict[str, int] = defaultdict(int)
    for producer, pid in pids.items():
        expected_role = CLOCK_PRODUCER_PROCESS_ROLES[producer]
        for sample_index, sample in enumerate(samples):
            processes = (sample.get("processes") or {}).get("processes", [])
            matches = [item for item in processes if item.get("pid") == pid]
            if len(matches) != 1 or matches[0].get("role") != expected_role:
                failures.append(
                    f"clock producer {producer} PID/role missing at sample {sample_index}"
                )
                break
            item = matches[0]
            identity = (
                item.get("pid"),
                item.get("start_ticks"),
                item.get("role"),
                item.get("executable_path"),
                item.get("executable_sha256"),
                item.get("cmdline_sha256"),
            )
            previous = identities.setdefault(int(pid), identity)
            if previous != identity:
                failures.append(f"clock producer {producer} process identity changed")
                break
        observed_counts[expected_role] += 1
    endpoint_pids = [pids[f"endpoint_{name}"] for name in ENDPOINTS]
    if len(set(endpoint_pids)) != len(endpoint_pids):
        failures.append("endpoint clock producers do not bind six distinct processes")
    actual_control_pids = [
        pids["gcs_control_probe"],
        *(pids[f"uav_control_adapter_uav{index}"] for index in range(1, 6)),
        pids["actual_endpoint_supervisor"],
    ]
    if len(set(actual_control_pids)) != len(actual_control_pids):
        failures.append("actual-control clock producers do not bind seven distinct processes")
    if set(endpoint_pids) & set(actual_control_pids):
        failures.append("companion and actual-control clock producers share a process")
    if pids.get("ros_gazebo_tracker") != pids.get("sionna_adapter"):
        failures.append("ROS pose tracker clock is not emitted by the adapter process")
    return {
        "bound_producer_count": len(identities),
        "producer_role_counts": dict(sorted(observed_counts.items())),
    }, failures


def unique_wire_messages(messages: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Collapse only exact peer-side observations of one wire identity."""

    unique: dict[tuple[str, int], dict[str, Any]] = {}
    for source in messages:
        key = (str(source.get("sender_id")), int(source.get("wire_sequence", -1)))
        message = dict(source)
        previous = unique.get(key)
        if previous is not None and previous != message:
            raise M4ValidationError(f"conflicting duplicate wire identity: {key}")
        unique[key] = message
    return list(unique.values())


def validate_capacity_freshness(
    wire: Mapping[str, Any],
    states: Mapping[str, Any],
    packets: Mapping[str, Any],
    adapter: Mapping[str, Any],
    *,
    start_ns: int,
    end_ns: int,
) -> tuple[dict[str, Any], list[str]]:
    failures: list[str] = []
    scheduled_slot_count = len(EXPECTED_CELLS) * QUERY_SLOT_COUNT_PER_CELL
    if (
        isinstance(start_ns, bool)
        or not isinstance(start_ns, int)
        or isinstance(end_ns, bool)
        or not isinstance(end_ns, int)
        or end_ns - start_ns != QUERY_SLOT_COUNT_PER_CELL * QUERY_PERIOD_NS
    ):
        return {
            "scheduled_query_slot_count": scheduled_slot_count,
            "query_count": 0,
            "ok_result_count": 0,
            "missed_slot_count": scheduled_slot_count,
            "late_result_count": scheduled_slot_count,
            "failed_slot_count": scheduled_slot_count,
            "late_update_ratio": 1.0,
        }, ["capacity freshness interval is not the frozen 600 seconds"]
    try:
        messages = unique_wire_messages(wire.get("messages", []))
    except (M4ValidationError, TypeError, ValueError) as exc:
        return {}, [str(exc)]

    state_records = states.get("records", [])
    adapter_records = adapter.get("records", [])
    if not isinstance(state_records, list):
        failures.append("capacity state occurrence list is absent")
        state_records = []
    if not isinstance(adapter_records, list):
        failures.append("capacity adapter occurrence list is absent")
        adapter_records = []

    ordered_cells = tuple(sorted(EXPECTED_CELLS))
    cell_offsets = {
        cell: index * QUERY_GLOBAL_SPACING_NS
        for index, cell in enumerate(ordered_cells)
    }
    slot_queries: dict[
        tuple[tuple[str, str], int], list[dict[str, Any]]
    ] = defaultdict(list)
    query_slot_by_id: dict[str, tuple[tuple[str, str], int]] = {}
    unexpected_measurement_queries = 0
    for message in messages:
        if message.get("message_type") != "query":
            continue
        query = dict(message)
        query_id = str(query.get("query_id"))
        sent = query.get("request_sent_monotonic_ns")
        in_measurement = (
            isinstance(sent, int)
            and not isinstance(sent, bool)
            and start_ns <= sent < end_ns
        )
        cell = (
            f"{query.get('tx_node_id')}>{query.get('rx_node_id')}",
            str(query.get("traffic_class")),
        )
        match = QUERY_SLOT_ID_RE.search(query_id)
        ordinal = int(match.group(1)) if match is not None else 0
        if cell in EXPECTED_CELLS and 1 <= ordinal <= QUERY_SLOT_COUNT_PER_CELL:
            slot = (cell, ordinal)
            previous = query_slot_by_id.setdefault(query_id, slot)
            if previous != slot or any(
                query_id == str(candidate.get("query_id"))
                for candidate in slot_queries[slot]
            ):
                failures.append(f"capacity query_id {query_id} is not globally unique")
            slot_queries[slot].append(query)
            continue
        if in_measurement:
            unexpected_measurement_queries += 1
            failures.append(
                f"measurement query {query_id} has no declared cell/slot ordinal"
            )

    results_by_query: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for message in messages:
        if message.get("message_type") == "result":
            results_by_query[str(message.get("query_id"))].append(dict(message))

    wire_hashes_by_query_kind: dict[tuple[str, str], list[str]] = defaultdict(list)
    message_by_hash = wire.get("message_by_hash", {})
    if not isinstance(message_by_hash, dict):
        failures.append("capacity wire hash index is absent")
        message_by_hash = {}
    for wire_sha256, message in message_by_hash.items():
        if not isinstance(message, Mapping):
            continue
        kind = message.get("message_type")
        if kind in {"query", "result"}:
            wire_hashes_by_query_kind[
                (str(message.get("query_id")), str(kind))
            ].append(str(wire_sha256))

    provider_identity_by_sender: dict[str, str] = {}
    for message in messages:
        if (
            message.get("message_type") not in {"hello", "ready"}
            or message.get("sender_role") != "provider"
        ):
            continue
        sender_id = message.get("sender_id")
        provider_identity = message.get("provider_identity")
        if not isinstance(sender_id, str) or not isinstance(
            provider_identity, dict
        ):
            failures.append("capacity provider handshake identity is incomplete")
            continue
        identity_json = json.dumps(
            provider_identity, sort_keys=True, separators=(",", ":")
        )
        previous = provider_identity_by_sender.setdefault(sender_id, identity_json)
        if previous != identity_json:
            failures.append(f"capacity provider identity changed for {sender_id}")
    if len(provider_identity_by_sender) != 1:
        failures.append(
            "capacity freshness requires exactly one provider handshake identity"
        )

    states_by_query: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for record in state_records:
        if isinstance(record, Mapping) and record.get("availability") == "fresh":
            states_by_query[str(record.get("query_id"))].append(record)
    audits_by_event_query: dict[
        tuple[str, str], list[Mapping[str, Any]]
    ] = defaultdict(list)
    for record in adapter_records:
        if not isinstance(record, Mapping):
            continue
        event = record.get("event")
        query_id = record.get("query_id")
        if isinstance(event, str) and isinstance(query_id, str):
            audits_by_event_query[(event, query_id)].append(record)

    cell_counts: dict[tuple[str, str], int] = defaultdict(int)
    successful_cell_counts: dict[tuple[str, str], int] = defaultdict(int)
    stale_pose = 0
    provider_receive_stale_pose = 0
    missed = 0
    late_or_invalid = 0
    failed = 0
    ok = 0
    for cell in ordered_cells:
        for ordinal in range(1, QUERY_SLOT_COUNT_PER_CELL + 1):
            candidates = slot_queries.get((cell, ordinal), [])
            cell_counts[cell] += len(candidates)
            if not candidates:
                missed += 1
                failed += 1
                continue

            slot_failed = len(candidates) != 1
            if len(candidates) != 1:
                failures.append(
                    f"cell {cell} slot {ordinal} has {len(candidates)} query frames"
                )
            query = candidates[0]
            query_id = str(query.get("query_id"))
            scheduled_ns = (
                start_ns
                + (ordinal - 1) * QUERY_PERIOD_NS
                + cell_offsets[cell]
            )
            slot_deadline_ns = scheduled_ns + QUERY_DEADLINE_NS
            sent = query.get("request_sent_monotonic_ns")
            deadline = query.get("deadline_monotonic_ns")
            if (
                isinstance(sent, bool)
                or not isinstance(sent, int)
                or isinstance(deadline, bool)
                or not isinstance(deadline, int)
                or deadline - sent != QUERY_DEADLINE_NS
            ):
                failures.append(f"query {query_id} changes the frozen 100-ms deadline")
                slot_failed = True
            if (
                isinstance(sent, bool)
                or not isinstance(sent, int)
                or not scheduled_ns <= sent < slot_deadline_ns
            ):
                failures.append(
                    f"query {query_id} was not sent inside its absolute ordinal slot"
                )
                slot_failed = True

            query_hashes = wire_hashes_by_query_kind.get((query_id, "query"), [])
            query_wire_sha256 = query_hashes[0] if len(query_hashes) == 1 else None
            if len(query_hashes) != 1:
                failures.append(
                    f"query {query_id} has {len(query_hashes)} exact wire hashes"
                )
                slot_failed = True
            elif hashlib.sha256(
                (
                    json.dumps(
                        query,
                        allow_nan=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode()
            ).hexdigest() != query_wire_sha256:
                failures.append(f"query {query_id} exact wire hash differs")
                slot_failed = True
            submitted_audits = audits_by_event_query.get(
                ("query_submitted", query_id), []
            )
            if len(submitted_audits) != 1:
                failures.append(
                    f"query {query_id} has {len(submitted_audits)} submission audit occurrences"
                )
                slot_failed = True
            else:
                submitted_audit = submitted_audits[0]
                if (
                    submitted_audit.get("decision") != "fixed_slot"
                    or submitted_audit.get("directed_link") != cell[0]
                    or submitted_audit.get("traffic_class") != cell[1]
                    or submitted_audit.get("query_wire_sha256")
                    != query_wire_sha256
                    or submitted_audit.get("query_slot_ordinal") != ordinal
                    or submitted_audit.get(
                        "query_slot_scheduled_monotonic_ns"
                    )
                    != scheduled_ns
                    or submitted_audit.get("query_slot_deadline_monotonic_ns")
                    != slot_deadline_ns
                ):
                    failures.append(
                        f"query {query_id} submission audit tuple differs"
                    )
                    slot_failed = True

            nodes = query.get("nodes")
            jammers = query.get("jammers")
            exact_poses = (
                isinstance(nodes, list)
                and {node.get("node_id") for node in nodes}
                == {"cp", "uav1", "uav2", "uav3", "uav4", "uav5"}
                and isinstance(jammers, list)
                and {item.get("jammer_id") for item in jammers} == {"jammer_m4"}
            )
            if not exact_poses:
                failures.append(
                    f"query {query_id} does not carry exact six-node/jammer poses"
                )
                slot_failed = True
            poses = [
                *(nodes if isinstance(nodes, list) else []),
                *(jammers if isinstance(jammers, list) else []),
            ]
            for pose in poses:
                age = pose.get("freshness_age_ns")
                if (
                    pose.get("stale") is not False
                    or isinstance(age, bool)
                    or not isinstance(age, int)
                    or not 0 <= age <= MAX_POSE_AGE_NS
                ):
                    stale_pose += 1
                    slot_failed = True

            results = results_by_query.get(query_id, [])
            if len(results) != 1:
                slot_failed = True
                if len(results) > 1:
                    failures.append(
                        f"query {query_id} has conflicting multiple result frames"
                    )
            else:
                result = results[0]
                completed = result.get("provider_completed_monotonic_ns")
                provider_received = result.get("provider_received_monotonic_ns")
                provider_started = result.get("provider_started_monotonic_ns")
                provider_sent = result.get("provider_sent_monotonic_ns")
                provider_emitted = result.get("emitted_monotonic_ns")
                cutoff_ns = (
                    min(deadline, slot_deadline_ns)
                    if isinstance(deadline, int) and not isinstance(deadline, bool)
                    else slot_deadline_ns
                )
                correlation_keys = (
                    "query_id",
                    "node_state_seq",
                    "directed_link_id",
                    "traffic_class",
                    "tx_node_id",
                    "rx_node_id",
                    "run_id",
                    "profile",
                    "phase_id",
                    "contract_hash",
                    "config_hash",
                    "bundle_id",
                )
                if any(result.get(key) != query.get(key) for key in correlation_keys):
                    failures.append(
                        f"query {query_id} query/result correlation tuple differs"
                    )
                    slot_failed = True
                if result.get("sender_id") not in provider_identity_by_sender:
                    failures.append(
                        f"query {query_id} result has no exact provider identity"
                    )
                    slot_failed = True
                if (
                    result.get("provider_clock_domain")
                    != query.get("sender_clock_domain")
                    or result.get("validity_clock_domain")
                    != query.get("sender_clock_domain")
                ):
                    failures.append(
                        f"query {query_id} result clock identity differs"
                    )
                    slot_failed = True
                provider_times = (
                    provider_received,
                    provider_started,
                    completed,
                    provider_sent,
                    provider_emitted,
                )
                if (
                    any(
                        isinstance(value, bool) or not isinstance(value, int)
                        for value in provider_times
                    )
                    or list(provider_times) != sorted(provider_times)
                    or not isinstance(sent, int)
                    or isinstance(sent, bool)
                    or provider_received < sent
                ):
                    failures.append(
                        f"query {query_id} provider timing tuple differs"
                    )
                    slot_failed = True
                for pose in poses:
                    pose_ns = pose.get("pose_monotonic_ns")
                    if (
                        isinstance(provider_received, bool)
                        or not isinstance(provider_received, int)
                        or isinstance(pose_ns, bool)
                        or not isinstance(pose_ns, int)
                        or not 0
                        <= provider_received - pose_ns
                        <= MAX_POSE_AGE_NS
                    ):
                        provider_receive_stale_pose += 1
                        slot_failed = True

                result_hashes = wire_hashes_by_query_kind.get(
                    (query_id, "result"), []
                )
                result_wire_sha256 = (
                    result_hashes[0] if len(result_hashes) == 1 else None
                )
                if len(result_hashes) != 1:
                    failures.append(
                        f"query {query_id} has {len(result_hashes)} exact result wire hashes"
                    )
                    slot_failed = True
                elif hashlib.sha256(
                    (
                        json.dumps(
                            result,
                            allow_nan=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        + "\n"
                    ).encode()
                ).hexdigest() != result_wire_sha256:
                    failures.append(
                        f"query {query_id} exact result wire hash differs"
                    )
                    slot_failed = True
                received_audits = audits_by_event_query.get(
                    ("result_received", query_id), []
                )
                adapter_received = None
                if len(received_audits) != 1:
                    failures.append(
                        f"query {query_id} has {len(received_audits)} receipt audit occurrences"
                    )
                    slot_failed = True
                else:
                    received_audit = received_audits[0]
                    adapter_received = received_audit.get(
                        "adapter_received_monotonic_ns"
                    )
                    if (
                        received_audit.get("decision") != "pending"
                        or received_audit.get("directed_link") != cell[0]
                        or received_audit.get("traffic_class") != cell[1]
                        or received_audit.get("result_wire_sha256")
                        != result_wire_sha256
                        or received_audit.get("query_slot_ordinal") != ordinal
                        or received_audit.get(
                            "query_slot_scheduled_monotonic_ns"
                        )
                        != scheduled_ns
                        or received_audit.get(
                            "query_slot_deadline_monotonic_ns"
                        )
                        != slot_deadline_ns
                        or isinstance(adapter_received, bool)
                        or not isinstance(adapter_received, int)
                        or isinstance(provider_emitted, bool)
                        or not isinstance(provider_emitted, int)
                        or provider_emitted > adapter_received
                    ):
                        failures.append(
                            f"query {query_id} receipt audit tuple differs"
                        )
                        slot_failed = True

                validity_start = result.get("validity_start_monotonic_ns")
                expiry = result.get("expires_monotonic_ns")
                if (
                    result.get("status") != "ok"
                    or isinstance(completed, bool)
                    or not isinstance(completed, int)
                    or completed >= cutoff_ns
                    or isinstance(adapter_received, bool)
                    or not isinstance(adapter_received, int)
                    or adapter_received >= cutoff_ns
                ):
                    slot_failed = True
                if result.get("status") == "ok" and (
                    isinstance(validity_start, bool)
                    or not isinstance(validity_start, int)
                    or isinstance(expiry, bool)
                    or not isinstance(expiry, int)
                    or expiry - validity_start != VALIDITY_TTL_NS
                ):
                    failures.append(
                        f"result {query_id} changes the frozen 2-s validity TTL"
                    )
                    slot_failed = True

                state_occurrences = states_by_query.get(query_id, [])
                applied_audits = audits_by_event_query.get(
                    ("result_applied", query_id), []
                )
                if len(state_occurrences) > 1 or len(applied_audits) > 1:
                    failures.append(
                        f"query {query_id} has multiple applied state/audit occurrences"
                    )
                    slot_failed = True
                if len(state_occurrences) != 1 or len(applied_audits) != 1:
                    slot_failed = True
                else:
                    state = state_occurrences[0]
                    applied_audit = applied_audits[0]
                    adapter_applied = state.get("adapter_applied_monotonic_ns")
                    expected_link_id = (
                        f"{query.get('tx_node_id')}-to-{query.get('rx_node_id')}-"
                        f"{query.get('traffic_class')}"
                    )
                    state_tuple_differs = (
                        query.get("directed_link_id") != expected_link_id
                        or state.get("directed_link") != cell[0]
                        or state.get("traffic_class") != cell[1]
                        or state.get("query_id") != query_id
                        or state.get("node_state_seq")
                        != query.get("node_state_seq")
                        or state.get("node_state_seq")
                        != result.get("node_state_seq")
                        or state.get("node_state_sha256")
                        != query.get("node_state_sha256")
                        or state.get("query_wire_sha256")
                        != query_wire_sha256
                        or state.get("result_wire_sha256")
                        != result_wire_sha256
                        or any(
                            state.get(key) != query.get(key)
                            for key in ("run_id", "profile", "phase_id")
                        )
                        or state.get("validity_start_monotonic_ns")
                        != validity_start
                        or state.get("expires_monotonic_ns") != expiry
                        or state.get("physical") != result.get("physical")
                    )
                    audit_tuple_differs = (
                        applied_audit.get("decision") != "applied"
                        or applied_audit.get("directed_link") != cell[0]
                        or applied_audit.get("traffic_class") != cell[1]
                        or applied_audit.get("result_wire_sha256")
                        != result_wire_sha256
                        or applied_audit.get("applied_state_id")
                        != state.get("applied_state_id")
                        or applied_audit.get("adapter_received_monotonic_ns")
                        != adapter_received
                        or applied_audit.get("adapter_applied_monotonic_ns")
                        != adapter_applied
                        or applied_audit.get("validity_start_monotonic_ns")
                        != validity_start
                        or applied_audit.get("expires_monotonic_ns") != expiry
                        or applied_audit.get("query_slot_ordinal") != ordinal
                        or applied_audit.get(
                            "query_slot_scheduled_monotonic_ns"
                        )
                        != scheduled_ns
                        or applied_audit.get(
                            "query_slot_deadline_monotonic_ns"
                        )
                        != slot_deadline_ns
                    )
                    if state_tuple_differs:
                        failures.append(
                            f"query {query_id} applied-state correlation tuple differs"
                        )
                        slot_failed = True
                    if audit_tuple_differs:
                        failures.append(
                            f"query {query_id} application audit tuple differs"
                        )
                        slot_failed = True
                    if (
                        isinstance(adapter_applied, bool)
                        or not isinstance(adapter_applied, int)
                        or adapter_applied >= cutoff_ns
                        or isinstance(validity_start, bool)
                        or not isinstance(validity_start, int)
                        or isinstance(expiry, bool)
                        or not isinstance(expiry, int)
                        or not validity_start <= adapter_applied < expiry
                        or not isinstance(adapter_received, int)
                        or isinstance(adapter_received, bool)
                        or adapter_applied < adapter_received
                    ):
                        slot_failed = True

            if slot_failed:
                late_or_invalid += 1
                failed += 1
            else:
                ok += 1
                successful_cell_counts[cell] += 1

    minimum_queries_per_cell = 570
    for cell in EXPECTED_CELLS:
        successful = successful_cell_counts.get(cell, 0)
        if successful < minimum_queries_per_cell:
            failures.append(
                f"cell {cell} has {successful} < 570 successful ordinal slots"
            )
    query_count = sum(len(candidates) for candidates in slot_queries.values())
    late_ratio = failed / scheduled_slot_count
    if stale_pose != 0:
        failures.append(f"stale pose samples must be exactly zero, observed {stale_pose}")
    if provider_receive_stale_pose != 0:
        failures.append(
            "provider-recomputed stale pose samples must be exactly zero, "
            f"observed {provider_receive_stale_pose}"
        )
    if late_ratio > 0.05:
        failures.append(f"late update ratio {late_ratio:.9f} exceeds 0.05")

    packet_records = [
        record
        for record in packets.get("records", [])
        if start_ns <= int(record.get("host_monotonic_ns", -1)) < end_ns
        and record.get("event") in {"enqueue", "drop"}
        and (record.get("directed_link"), record.get("traffic_class")) in EXPECTED_CELLS
    ]
    ages = [
        int(record["radio_state_age_ns"])
        for record in packet_records
        if record.get("radio_state_status") == "fresh"
        and isinstance(record.get("radio_state_age_ns"), int)
    ]
    decision_cells = {
        (record.get("directed_link"), record.get("traffic_class")) for record in packet_records
    }
    state_age_p95 = nearest_rank(ages, 0.95) if ages else None
    if decision_cells != EXPECTED_CELLS:
        failures.append(f"measurement packet decisions cover {len(decision_cells)} of 30 cells")
    if len(ages) != len(packet_records):
        failures.append("measurement packet decisions include non-fresh or unaged state")
    if state_age_p95 is None or state_age_p95 > 2 * QUERY_PERIOD_NS:
        failures.append("link-state age p95 exceeds two configured update periods")
    return {
        "scheduled_query_slot_count": scheduled_slot_count,
        "scheduled_slots_per_cell": QUERY_SLOT_COUNT_PER_CELL,
        "query_count": query_count,
        "unexpected_measurement_query_count": unexpected_measurement_queries,
        "ok_result_count": ok,
        "missed_slot_count": missed,
        "late_or_invalid_slot_count": late_or_invalid,
        "late_result_count": failed,
        "failed_slot_count": failed,
        "late_update_ratio": late_ratio,
        "stale_pose_count": stale_pose,
        "provider_receive_stale_pose_count": provider_receive_stale_pose,
        "query_cells": sum(cell_counts[cell] > 0 for cell in EXPECTED_CELLS),
        "minimum_queries_in_one_cell": min(
            cell_counts[cell] for cell in EXPECTED_CELLS
        ),
        "minimum_successful_slots_in_one_cell": min(
            successful_cell_counts[cell] for cell in EXPECTED_CELLS
        ),
        "measurement_packet_decisions": len(packet_records),
        "measurement_packet_cells": len(decision_cells),
        "state_age_sample_count": len(ages),
        "state_age_p95_ns": state_age_p95,
    }, failures


def derive_m3_nominal_vector(
    accepted_m3_dir: Path,
) -> tuple[dict[str, dict[str, float | int]], dict[str, Any], list[str]]:
    failures: list[str] = []
    try:
        from network.validation.validate_m3_external_matrix import validate as validate_m3

        validation = validate_m3(accepted_m3_dir)
        if validation.get("passed") is not True or validation.get("failures") != []:
            return {}, validation, ["embedded accepted M3 raw tree does not independently PASS"]
        phase = strict_json(accepted_m3_dir / "raw/phase_contract.json")
        positive = next(item for item in phase["windows"] if item.get("phase") == "positive")
        duration_ns = int(positive["end_monotonic_ns"]) - int(positive["start_monotonic_ns"])
        if duration_ns < 30_000_000_000:
            raise M4ValidationError("accepted M3 positive window is shorter than 30 seconds")
        offered: dict[str, dict[tuple[str, str], dict[str, Any]]] = defaultdict(dict)
        for endpoint in ENDPOINTS:
            for record in strict_jsonl(accepted_m3_dir / f"raw/endpoints/{endpoint}.jsonl"):
                if record.get("event") != "offered" or record.get("phase") != "positive":
                    continue
                cell = str(record.get("cell_id"))
                key = (str(record.get("record_nonce")), str(record.get("transport_payload_sha256")))
                if key in offered[cell]:
                    raise M4ValidationError(f"accepted M3 has duplicate offer {cell}/{key}")
                offered[cell][key] = record
        if set(offered) != {f"uav{uav}.{traffic_class}.{direction}" for uav in range(1, 6) for traffic_class in ("control", "payload", "additional_data") for direction in ("downlink", "uplink")}:
            raise M4ValidationError("accepted M3 nominal vector is not exact 30 cells")
        vector: dict[str, dict[str, float | int]] = {}
        for cell, records in offered.items():
            byte_count = sum(int(record["transport_payload_size"]) for record in records.values())
            vector[cell] = {
                "offered_units": len(records),
                "offered_bytes": byte_count,
                "unit_rate_hz": len(records) * 1_000_000_000 / duration_ns,
                "byte_rate_bps": byte_count * 8 * 1_000_000_000 / duration_ns,
                "source_duration_ns": duration_ns,
            }
        return dict(sorted(vector.items())), validation, failures
    except (OSError, KeyError, StopIteration, TypeError, ValueError, M4ValidationError) as exc:
        return {}, {}, [f"accepted M3 vector cannot be independently derived: {exc}"]


def derive_m3_vector_from_receipt(
    receipt_path: Path,
) -> tuple[dict[str, dict[str, float | int]], dict[str, Any], list[str]]:
    """Validate and extract the host-final M3 rate vector mounted by the wrapper."""

    failures: list[str] = []
    try:
        receipt = strict_json(receipt_path)
        result = receipt.get("result")
        vector = (result.get("metrics") or {}).get("nominal_rate_vector")
        if (
            receipt.get("contract") != "ams.m3.host-final-receipt/v1"
            or receipt.get("profile") != "m3_component"
            or receipt.get("formal_accepted") is not True
            or receipt.get("passed") is not True
            or receipt.get("failures") != []
            or receipt.get("result_contract")
            != "ams.m3.external-matrix-validation/v1"
            or not isinstance(result, dict)
            or result.get("passed") is not True
            or result.get("failures") != []
            or not isinstance(vector, dict)
            or set(vector) != M3_CELL_IDS
        ):
            raise M4ValidationError("M3 host-final receipt/rate-vector identity is not exact")
        normalized: dict[str, dict[str, float | int]] = {}
        for cell, value in vector.items():
            if not isinstance(value, dict) or set(value) != {
                "offered_units",
                "offered_bytes",
                "duration_ns",
                "unit_rate_hz",
                "byte_rate_bps",
            }:
                raise M4ValidationError(f"M3 nominal vector {cell} shape differs")
            units = value.get("offered_units")
            byte_count = value.get("offered_bytes")
            duration = value.get("duration_ns")
            unit_rate = value.get("unit_rate_hz")
            byte_rate = value.get("byte_rate_bps")
            if (
                isinstance(units, bool)
                or not isinstance(units, int)
                or units < 20
                or isinstance(byte_count, bool)
                or not isinstance(byte_count, int)
                or byte_count <= 0
                or isinstance(duration, bool)
                or not isinstance(duration, int)
                or duration < 30_000_000_000
                or not finite_number(unit_rate)
                or not finite_number(byte_rate)
                or not math.isclose(
                    float(unit_rate), units * 1_000_000_000 / duration, rel_tol=0.0, abs_tol=1e-9
                )
                or not math.isclose(
                    float(byte_rate), byte_count * 8 * 1_000_000_000 / duration, rel_tol=0.0, abs_tol=1e-6
                )
            ):
                raise M4ValidationError(f"M3 nominal vector {cell} values are inconsistent")
            normalized[cell] = {
                "offered_units": units,
                "offered_bytes": byte_count,
                "duration_ns": duration,
                "unit_rate_hz": float(unit_rate),
                "byte_rate_bps": float(byte_rate),
            }
        return dict(sorted(normalized.items())), receipt, failures
    except (OSError, TypeError, ValueError, M4ValidationError) as exc:
        return {}, {}, [f"M3 host-final rate vector cannot be validated: {exc}"]


def _consume_ordered_occurrence(
    records: list[dict[str, Any]],
    cursors: dict[tuple[Any, ...], int],
    *,
    cursor_key: tuple[Any, ...],
    timestamp_field: str,
    lower_ns: int,
    upper_ns: int,
) -> dict[str, Any]:
    """Consume one byte occurrence without assuming its SHA-256 is unique."""

    ordered = sorted(records, key=lambda record: int(record.get(timestamp_field, -1)))
    cursor = cursors.get(cursor_key, 0)
    while cursor < len(ordered) and int(ordered[cursor].get(timestamp_field, -1)) < lower_ns:
        cursor += 1
    if cursor >= len(ordered):
        raise M4ValidationError(
            f"no remaining ordered occurrence for {cursor_key} after {lower_ns}"
        )
    selected = ordered[cursor]
    timestamp = selected.get(timestamp_field)
    if (
        isinstance(timestamp, bool)
        or not isinstance(timestamp, int)
        or timestamp > upper_ns
    ):
        raise M4ValidationError(
            f"ordered occurrence for {cursor_key} is outside causal interval"
        )
    cursors[cursor_key] = cursor + 1
    return selected


def _collect_capacity_endpoint_records(
    run_dir: Path,
    *,
    contract: Mapping[str, Any],
    start_ns: int,
    end_ns: int,
) -> tuple[
    dict[str, dict[tuple[str, str], dict[str, Any]]],
    dict[str, dict[tuple[str, str], dict[str, Any]]],
]:
    """Merge companion data traffic with the sole actual-SITL control path.

    The six companion agents are authorized only for payload and
    additional-data.  The ten control cells are normalized from the real
    control probe, byte-opaque adapter forwarding, and ArduPilot ACK evidence;
    accepting control records from a companion would reintroduce the detached
    synthetic endpoint fixture that Q3 explicitly removed.
    """

    from network.validation.validate_m3_external_matrix import decode_transport

    full_nonce = bytes.fromhex(str(contract["run_nonce"]))
    if len(full_nonce) != 32:
        raise M4ValidationError("capacity contract run nonce is not 256-bit")
    expected_transport_nonce = hashlib.sha256(full_nonce).hexdigest()[:32]
    offered: dict[str, dict[tuple[str, str], dict[str, Any]]] = defaultdict(dict)
    received: dict[str, dict[tuple[str, str], dict[str, Any]]] = defaultdict(dict)
    companion_cells = {
        f"uav{uav}.{traffic_class}.{direction}"
        for uav in range(1, 6)
        for traffic_class in ("payload", "additional_data")
        for direction in ("downlink", "uplink")
    }
    control_cells = {
        f"uav{uav}.control.{direction}"
        for uav in range(1, 6)
        for direction in ("downlink", "uplink")
    }
    for endpoint in ENDPOINTS:
        records = strict_jsonl(run_dir / f"raw/endpoints/{endpoint}.jsonl")
        ready = [record for record in records if record.get("event") == "agent_ready"]
        if len(ready) != 1 or set(ready[0].get("bound_sockets", {})) != {
            "payload",
            "additional_data",
        }:
            raise M4ValidationError(
                f"{endpoint} companion socket scope is not payload/additional_data"
            )
        for number, record in enumerate(records, start=1):
            if record.get("run_nonce") != contract.get("run_nonce"):
                raise M4ValidationError(
                    f"{endpoint}:{number} full run nonce binding differs"
                )
            if (
                record.get("traffic_class") == "control"
                or record.get("socket_class") == "control"
                or record.get("cell_id") in control_cells
            ):
                raise M4ValidationError(
                    f"{endpoint}:{number} companion generated/bound control traffic"
                )
            event = record.get("event")
            if event not in {"offered", "remote_receive"} or record.get("phase") != "positive":
                continue
            decoded = decode_transport(record.get("transport_payload_hex"))
            if (
                decoded.get("transport_payload_sha256")
                != record.get("transport_payload_sha256")
                or decoded.get("record_nonce") != record.get("record_nonce")
                or decoded.get("run_nonce") != expected_transport_nonce
                or decoded.get("traffic_class") not in {
                    "payload",
                    "additional_data",
                }
                or decoded.get("flow_id") not in companion_cells
            ):
                raise M4ValidationError(
                    f"{endpoint}:{number} decoded companion bytes/scope differ"
                )
            cell = str(decoded["flow_id"])
            identity = (
                str(decoded["record_nonce"]),
                str(decoded["transport_payload_sha256"]),
            )
            timestamp_field = (
                "sent_monotonic_ns" if event == "offered" else "received_monotonic_ns"
            )
            timestamp = record.get(timestamp_field)
            if not isinstance(timestamp, int) or not start_ns <= timestamp < end_ns:
                continue
            destination = offered if event == "offered" else received
            if identity in destination[cell]:
                raise M4ValidationError(
                    f"duplicate companion capacity {event} {cell}/{identity}"
                )
            destination[cell][identity] = record

    control_events = strict_jsonl(
        run_dir / "raw/actual_control/events.jsonl", max_line_bytes=2 * 1024 * 1024
    )
    results: dict[str, dict[str, Any]] = {}
    datagrams: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in control_events:
        if (
            record.get("run_id") != contract.get("run_id")
            or record.get("runtime_id") != contract.get("runtime_id")
            or record.get("run_nonce") != contract.get("run_nonce")
            or record.get("profile") != "m4_capacity"
            or record.get("transport_nonce32") != expected_transport_nonce
            or record.get("transport_nonce_derivation")
            != "sha256(raw_full_run_nonce64)[:32]"
        ):
            raise M4ValidationError("actual-control capacity identity/nonce differs")
        if record.get("event") == "transaction_result":
            transaction_id = str(record.get("transaction_id"))
            if not transaction_id or transaction_id in results:
                raise M4ValidationError("actual-control transaction result identity differs")
            results[transaction_id] = record
        if record.get("event") == "control_datagram_receive":
            digest = str(record.get("transport_payload_sha256"))
            if HEX64.fullmatch(digest) is None:
                raise M4ValidationError("actual-control received datagram hash is invalid")
            datagrams[digest].append(record)

    adapter_forwards: dict[tuple[int, str, str], list[dict[str, Any]]] = defaultdict(list)
    for uav in range(1, 6):
        for record in strict_jsonl(run_dir / f"logs/actual_sitl_uav{uav}.jsonl"):
            if record.get("event") != "forward":
                continue
            direction = str(record.get("direction"))
            digest = str(record.get("sha256"))
            if direction not in {"gcs_to_tail", "tail_to_gcs"} or HEX64.fullmatch(
                digest
            ) is None:
                raise M4ValidationError(f"uav{uav} actual adapter forward is malformed")
            adapter_forwards[(uav, direction, digest)].append(record)

    selected_offers = sorted(
        [
        record
        for record in control_events
        if record.get("event") == "real_command_offered"
        and isinstance(record.get("sent_monotonic_ns"), int)
        and start_ns <= record["sent_monotonic_ns"] < end_ns
        ],
        key=lambda record: (
            int(record["sent_monotonic_ns"]),
            int(record.get("uav", 0)),
            int(record.get("ordinal_send_slot", 0)),
        ),
    )
    if not selected_offers:
        raise M4ValidationError("capacity interval has no actual-control offers")
    occurrence_cursor: dict[tuple[str, int, str, str], int] = defaultdict(int)

    for request in selected_offers:
        uav = request.get("uav")
        transaction_id = str(request.get("transaction_id"))
        if isinstance(uav, bool) or not isinstance(uav, int) or not 1 <= uav <= 5:
            raise M4ValidationError("actual-control offer UAV differs")
        result = results.get(transaction_id)
        command_hex = request.get("command_frame_hex")
        try:
            command_bytes = bytes.fromhex(str(command_hex))
        except ValueError as exc:
            raise M4ValidationError("actual-control command bytes are invalid") from exc
        command_hash = hashlib.sha256(command_bytes).hexdigest()
        record_nonce = str(request.get("record_nonce"))
        downlink_cell = f"uav{uav}.control.downlink"
        downlink_identity = (record_nonce, command_hash)
        if (
            request.get("endpoint_form") != "actual_sitl_mavproxy_udp_tail"
            or request.get("cell_id") != downlink_cell
            or request.get("command_frame_sha256") != command_hash
            or not isinstance(result, dict)
            or result.get("transaction_id") != transaction_id
            or result.get("record_nonce") != record_nonce
            or result.get("command_frame_sha256") != command_hash
            or result.get("success") is not True
            or result.get("timed_out") is not False
        ):
            raise M4ValidationError(
                f"capacity actual-control request/result differs: {transaction_id}"
            )
        sent_ns = int(request["sent_monotonic_ns"])
        completed_ns = result.get("completed_monotonic_ns")
        if isinstance(completed_ns, bool) or not isinstance(completed_ns, int):
            raise M4ValidationError(
                f"capacity actual-control completion time differs: {transaction_id}"
            )
        down_forward = _consume_ordered_occurrence(
            adapter_forwards[(uav, "gcs_to_tail", command_hash)],
            occurrence_cursor,
            cursor_key=("adapter", uav, "gcs_to_tail", command_hash),
            timestamp_field="monotonic_ns",
            lower_ns=sent_ns,
            upper_ns=completed_ns,
        )
        offered[downlink_cell][downlink_identity] = {
            **request,
            "transport_payload_sha256": command_hash,
            "transport_payload_size": len(command_bytes),
        }
        received[downlink_cell][downlink_identity] = {
            **request,
            "received_monotonic_ns": int(down_forward["monotonic_ns"]),
            "transport_payload_sha256": command_hash,
            "transport_payload_size": len(command_bytes),
        }

        ack = result.get("ack")
        if not isinstance(ack, dict):
            raise M4ValidationError(f"capacity actual-control ACK absent: {transaction_id}")
        ack_hash = str(ack.get("transport_payload_sha256"))
        uplink_cell = f"uav{uav}.control.uplink"
        uplink_nonce = hashlib.sha256(
            f"{record_nonce}:real-ack".encode("ascii")
        ).hexdigest()
        uplink_identity = (uplink_nonce, ack_hash)
        ack_received_ns = ack.get("received_monotonic_ns")
        if isinstance(ack_received_ns, bool) or not isinstance(ack_received_ns, int):
            raise M4ValidationError(
                f"capacity actual-control ACK receive time differs: {transaction_id}"
            )
        uplink_forward = _consume_ordered_occurrence(
            adapter_forwards[(uav, "tail_to_gcs", ack_hash)],
            occurrence_cursor,
            cursor_key=("adapter", uav, "tail_to_gcs", ack_hash),
            timestamp_field="monotonic_ns",
            lower_ns=sent_ns,
            upper_ns=ack_received_ns,
        )
        matching_datagram = _consume_ordered_occurrence(
            datagrams.get(ack_hash, []),
            occurrence_cursor,
            cursor_key=("gcs", uav, "receive", ack_hash),
            timestamp_field="received_monotonic_ns",
            lower_ns=int(uplink_forward["monotonic_ns"]),
            upper_ns=ack_received_ns,
        )
        if matching_datagram.get("received_monotonic_ns") != ack_received_ns:
            raise M4ValidationError(
                f"capacity actual-control ACK datagram time differs: {transaction_id}"
            )
        ack_size = matching_datagram.get("transport_payload_size")
        if (
            isinstance(ack_size, bool)
            or not isinstance(ack_size, int)
            or ack_size <= 0
        ):
            raise M4ValidationError(
                f"capacity actual-control ACK size/time differs: {transaction_id}"
            )
        offered[uplink_cell][uplink_identity] = {
            "record_nonce": uplink_nonce,
            "transport_payload_sha256": ack_hash,
            "transport_payload_size": ack_size,
            "sent_monotonic_ns": int(uplink_forward["monotonic_ns"]),
            "transaction_id": transaction_id,
        }
        received[uplink_cell][uplink_identity] = {
            "record_nonce": uplink_nonce,
            "transport_payload_sha256": ack_hash,
            "transport_payload_size": ack_size,
            "received_monotonic_ns": ack_received_ns,
            "transaction_id": transaction_id,
        }
    if set(offered) != companion_cells | control_cells:
        raise M4ValidationError(
            "merged companion/actual-control capacity offers do not cover exact 30 cells"
        )
    if set(received) != companion_cells | control_cells:
        raise M4ValidationError(
            "merged companion/actual-control capacity deliveries do not cover exact 30 cells"
        )
    return dict(offered), dict(received)


def validate_capacity_workload(
    run_dir: Path,
    *,
    start_ns: int,
    end_ns: int,
) -> tuple[dict[str, Any], list[str]]:
    failures: list[str] = []
    vector, m3_receipt, vector_failures = derive_m3_vector_from_receipt(
        run_dir / "raw/prerequisites/m3.json"
    )
    failures.extend(vector_failures)
    offered: dict[str, dict[tuple[str, str], dict[str, Any]]] = defaultdict(dict)
    received: dict[str, dict[tuple[str, str], dict[str, Any]]] = defaultdict(dict)
    try:
        contract = strict_json(run_dir / "raw/m4_capacity_contract.json")
        merged_offered, merged_received = _collect_capacity_endpoint_records(
            run_dir,
            contract=contract,
            start_ns=start_ns,
            end_ns=end_ns,
        )
        offered.update(merged_offered)
        received.update(merged_received)
        if set(offered) != set(vector):
            failures.append(
                f"capacity workload cell set differs: observed={len(offered)} expected={len(vector)}"
            )
    except (OSError, KeyError, TypeError, ValueError, M4ValidationError) as exc:
        failures.append(f"capacity endpoint evidence cannot be decoded: {exc}")
    duration_ns = end_ns - start_ns
    metrics: dict[str, Any] = {}
    for cell, baseline in vector.items():
        records = offered.get(cell, {})
        delivery_keys = set(received.get(cell, {}))
        if any(identity not in records for identity in delivery_keys):
            failures.append(f"{cell} has remote delivery without a measurement offer")
        byte_count = sum(int(record.get("transport_payload_size", 0)) for record in records.values())
        unit_rate = len(records) * 1_000_000_000 / duration_ns if duration_ns > 0 else 0.0
        byte_rate = byte_count * 8 * 1_000_000_000 / duration_ns if duration_ns > 0 else 0.0
        delivery = len(delivery_keys) / len(records) if records else 0.0
        if unit_rate + 1e-12 < float(baseline["unit_rate_hz"]):
            failures.append(f"{cell} unit rate is below accepted M3 nominal rate")
        if byte_rate + 1e-9 < float(baseline["byte_rate_bps"]):
            failures.append(f"{cell} byte rate is below accepted M3 nominal rate")
        metrics[cell] = {
            "offered_unique": len(records),
            "received_unique": len(delivery_keys),
            "delivery_ratio": delivery,
            "unit_rate_hz": unit_rate,
            "byte_rate_bps": byte_rate,
            "minimum_m3_unit_rate_hz": baseline.get("unit_rate_hz"),
            "minimum_m3_byte_rate_bps": baseline.get("byte_rate_bps"),
        }
    vector_hash = hashlib.sha256(
        json.dumps(vector, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest() if vector else None
    return {
        "cells": metrics,
        "cell_count": len(metrics),
        "m3_run_id": m3_receipt.get("run_id"),
        "m3_nominal_vector_sha256": vector_hash,
    }, failures


def _measurement_capture_realtime_bounds(
    run_dir: Path,
    *,
    start_ns: int,
    end_ns: int,
) -> tuple[int, int]:
    """Map frozen monotonic measurement boundaries into PCAP realtime."""

    records = strict_jsonl(
        run_dir / "logs/m4_runtime_events.jsonl",
        max_line_bytes=2 * 1024 * 1024,
    )
    start_event = _one_event(records, "measurement_start")
    end_event = _one_event(records, "measurement_end")

    def mapped(
        record: Mapping[str, Any],
        target_ns: int,
        *,
        label: str,
        maximum_emit_delay_ns: int,
    ) -> int:
        host_ns = record.get("host_monotonic_ns")
        realtime_ns = record.get("host_realtime_ns")
        if (
            isinstance(host_ns, bool)
            or not isinstance(host_ns, int)
            or isinstance(realtime_ns, bool)
            or not isinstance(realtime_ns, int)
            or realtime_ns <= 0
            or not target_ns <= host_ns <= target_ns + maximum_emit_delay_ns
        ):
            raise M4ValidationError(
                f"{label} runtime event cannot calibrate PCAP realtime"
            )
        return realtime_ns - (host_ns - target_ns)

    start_realtime_ns = mapped(
        start_event,
        start_ns,
        label="measurement start",
        maximum_emit_delay_ns=100_000_000,
    )
    end_realtime_ns = mapped(
        end_event,
        end_ns,
        label="measurement end",
        maximum_emit_delay_ns=500_000_000,
    )
    monotonic_span_ns = end_ns - start_ns
    realtime_span_ns = end_realtime_ns - start_realtime_ns
    if (
        start_realtime_ns <= 0
        or monotonic_span_ns <= 0
        or realtime_span_ns <= 0
        or abs(realtime_span_ns - monotonic_span_ns)
        > CAPTURE_REALTIME_SPAN_TOLERANCE_NS
    ):
        raise M4ValidationError("measurement PCAP realtime interval is invalid")
    return start_realtime_ns, end_realtime_ns


def _monotonic_to_capture_realtime_ns(
    monotonic_ns: int,
    *,
    start_ns: int,
    end_ns: int,
    start_realtime_ns: int,
    end_realtime_ns: int,
) -> int:
    """Linearly map one frozen measurement instant to CLOCK_REALTIME."""

    if (
        isinstance(monotonic_ns, bool)
        or not isinstance(monotonic_ns, int)
        or not start_ns <= monotonic_ns < end_ns
    ):
        raise M4ValidationError(
            "expected capture occurrence timestamp is outside measurement"
        )
    monotonic_span_ns = end_ns - start_ns
    realtime_span_ns = end_realtime_ns - start_realtime_ns
    if monotonic_span_ns <= 0 or realtime_span_ns <= 0:
        raise M4ValidationError("capture clock interpolation interval is invalid")
    return start_realtime_ns + (
        (monotonic_ns - start_ns) * realtime_span_ns // monotonic_span_ns
    )


def _consume_capture_role_occurrences(
    index: Mapping[tuple[Any, ...], list[dict[str, Any]]],
    expected_records: Iterable[Mapping[str, Any]],
    *,
    capture: str,
    key_fn: Callable[[Mapping[str, Any]], tuple[Any, ...]],
    timestamp_field: str,
    start_ns: int,
    end_ns: int,
    start_realtime_ns: int,
    end_realtime_ns: int,
    cursors: dict[tuple[str, tuple[Any, ...]], int],
) -> tuple[int, int]:
    """Consume one time-bound captured frame per expected byte occurrence."""

    prepared: list[dict[str, Any]] = []
    for record in expected_records:
        timestamp = record.get(timestamp_field)
        if (
            isinstance(timestamp, bool)
            or not isinstance(timestamp, int)
            or not start_ns <= timestamp < end_ns
        ):
            raise M4ValidationError(
                f"{capture} expected occurrence timestamp is outside measurement"
            )
        packet_key = key_fn(record)
        prepared.append(
            {
                "record": record,
                "packet_key": packet_key,
                "monotonic_ns": timestamp,
                "realtime_ns": _monotonic_to_capture_realtime_ns(
                    timestamp,
                    start_ns=start_ns,
                    end_ns=end_ns,
                    start_realtime_ns=start_realtime_ns,
                    end_realtime_ns=end_realtime_ns,
                ),
            }
        )

    by_key: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for item in prepared:
        by_key[item["packet_key"]].append(item)
    for packet_key, occurrences in by_key.items():
        occurrences.sort(
            key=lambda item: (
                int(item["monotonic_ns"]),
                str(item["record"].get("record_nonce")),
            )
        )
        if any(
            int(occurrences[index]["monotonic_ns"])
            <= int(occurrences[index - 1]["monotonic_ns"])
            for index in range(1, len(occurrences))
        ):
            raise M4ValidationError(
                f"{capture} repeated expected occurrences have ambiguous order"
            )
        if any(
            int(occurrences[index]["realtime_ns"])
            - int(occurrences[index - 1]["realtime_ns"])
            <= 2 * CAPTURE_OCCURRENCE_MATCH_TOLERANCE_NS
            for index in range(1, len(occurrences))
        ):
            raise M4ValidationError(
                f"{capture} repeated expected occurrence timing windows overlap"
            )
        for occurrence_index, item in enumerate(occurrences):
            realtime_ns = int(item["realtime_ns"])
            lower_ns = realtime_ns - CAPTURE_OCCURRENCE_MATCH_TOLERANCE_NS
            upper_ns = realtime_ns + CAPTURE_OCCURRENCE_MATCH_TOLERANCE_NS
            if occurrence_index:
                previous_ns = int(
                    occurrences[occurrence_index - 1]["realtime_ns"]
                )
                lower_ns = max(lower_ns, (previous_ns + realtime_ns) // 2 + 1)
            if occurrence_index + 1 < len(occurrences):
                next_ns = int(
                    occurrences[occurrence_index + 1]["realtime_ns"]
                )
                upper_ns = min(upper_ns, (realtime_ns + next_ns) // 2)
            if lower_ns > upper_ns:
                raise M4ValidationError(
                    f"{capture} repeated expected occurrence window is empty"
                )
            item["capture_lower_realtime_ns"] = lower_ns
            item["capture_upper_realtime_ns"] = upper_ns

    prepared.sort(
        key=lambda item: (
            int(item["monotonic_ns"]),
            repr(item["packet_key"]),
            str(item["record"].get("record_nonce")),
        )
    )

    matched = 0
    for item in prepared:
        packet_key = item["packet_key"]
        cursor_key = (capture, packet_key)
        cursor = cursors.get(cursor_key, 0)
        candidates = index.get(packet_key, [])
        lower_ns = int(item["capture_lower_realtime_ns"])
        upper_ns = int(item["capture_upper_realtime_ns"])
        while cursor < len(candidates):
            candidate_ns = candidates[cursor].get("timestamp_ns")
            if (
                isinstance(candidate_ns, bool)
                or not isinstance(candidate_ns, int)
            ):
                raise M4ValidationError(
                    f"{capture} captured occurrence realtime is invalid"
                )
            if candidate_ns >= lower_ns:
                break
            cursor += 1
        cursors[cursor_key] = cursor
        if cursor >= len(candidates):
            continue
        candidate_ns = int(candidates[cursor]["timestamp_ns"])
        if candidate_ns <= upper_ns:
            cursors[cursor_key] = cursor + 1
            matched += 1
    return len(prepared), matched


def validate_external_captures(
    run_dir: Path,
    *,
    start_ns: int,
    end_ns: int,
) -> tuple[dict[str, Any], list[str]]:
    """Bind endpoint evidence to both sides of every external TapBridge edge."""

    failures: list[str] = []
    try:
        from network.validation.validate_m3_external_matrix import parse_pcap

        matrix = strict_json(ROOT / "network/config/endpoint_matrix_5uav.json")
        cells = {str(item["cell_id"]): item for item in matrix["cells"]}
        if set(cells) != M3_CELL_IDS:
            raise M4ValidationError("capture validator matrix cell set differs")
        capture_specs = [
            *((f"endpoint-{endpoint}", "eth0") for endpoint in ENDPOINTS),
            *((f"ns3-external-{endpoint}", f"vp-{endpoint}") for endpoint in ENDPOINTS),
        ]
        start_realtime_ns, end_realtime_ns = _measurement_capture_realtime_bounds(
            run_dir,
            start_ns=start_ns,
            end_ns=end_ns,
        )
        capture_records: dict[str, list[dict[str, Any]]] = {}
        packet_counts: dict[str, int] = {}
        stats_keys = {
            "contract",
            "interface",
            "capture_protocol",
            "packet_filter",
            "pcap_path",
            "pcap_bytes",
            "linktype",
            "snaplen",
            "receive_buffer_requested_bytes",
            "receive_buffer_effective_bytes",
            "receive_buffer_setter",
            "drain_batch_packet_limit",
            "drain_batch_byte_limit",
            "started_monotonic_ns",
            "stopped_monotonic_ns",
            "stop_signal",
            "packets_written",
            "packets_received_kernel",
            "packets_dropped_kernel",
        }
        for name, interface in capture_specs:
            pcap_path = run_dir / f"pcap/{name}.pcap"
            count, decoded, errors = parse_pcap(pcap_path)
            failures.extend(errors)
            packet_counts[name] = count
            capture_records[name] = decoded
            stats_path = run_dir / f"logs/capture-{name}.json"
            stats = strict_json(stats_path)
            stderr = run_dir / f"logs/capture-{name}.stderr"
            if (
                set(stats) != stats_keys
                or stats.get("contract") != CAPTURE_STATS_CONTRACT
                or stats.get("interface") != interface
                or stats.get("capture_protocol") != CAPTURE_PROTOCOL
                or stats.get("packet_filter") != CAPTURE_PACKET_FILTER
                or stats.get("pcap_path") != pcap_path.name
                or type(stats.get("pcap_bytes")) is not int
                or stats.get("pcap_bytes") != pcap_path.stat().st_size
                or type(stats.get("linktype")) is not int
                or stats.get("linktype") != 1
                or type(stats.get("snaplen")) is not int
                or stats.get("snaplen") != 65_535
                or type(stats.get("receive_buffer_requested_bytes")) is not int
                or stats.get("receive_buffer_requested_bytes")
                != CAPTURE_RECEIVE_BUFFER_REQUESTED_BYTES
                or type(stats.get("receive_buffer_effective_bytes")) is not int
                or stats.get("receive_buffer_effective_bytes")
                != CAPTURE_RECEIVE_BUFFER_EFFECTIVE_BYTES
                or stats.get("receive_buffer_setter")
                not in CAPTURE_RECEIVE_BUFFER_SETTERS
                or type(stats.get("drain_batch_packet_limit")) is not int
                or stats.get("drain_batch_packet_limit")
                != CAPTURE_DRAIN_BATCH_PACKET_LIMIT
                or type(stats.get("drain_batch_byte_limit")) is not int
                or stats.get("drain_batch_byte_limit")
                != CAPTURE_DRAIN_BATCH_BYTE_LIMIT
                or stats.get("stop_signal") != "SIGINT"
                or type(stats.get("packets_written")) is not int
                or stats.get("packets_written") != count
                or type(stats.get("packets_received_kernel")) is not int
                or stats["packets_received_kernel"] < count
                or type(stats.get("packets_dropped_kernel")) is not int
                or stats.get("packets_dropped_kernel") != 0
                or type(stats.get("started_monotonic_ns")) is not int
                or type(stats.get("stopped_monotonic_ns")) is not int
                or stats["started_monotonic_ns"] >= start_ns
                or stats["stopped_monotonic_ns"] <= end_ns
                or not regular_file(stderr)
                or stderr.stat().st_size != 0
            ):
                failures.append(f"capture accounting differs: {name}")

        indexes: dict[str, dict[tuple[Any, ...], list[dict[str, Any]]]] = {}
        for name, records in capture_records.items():
            index: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
            ordered_records = sorted(
                records,
                key=lambda record: int(record.get("frame_index", -1)),
            )
            previous_frame_index = 0
            previous_timestamp_ns = 0
            for record in ordered_records:
                frame_index = record.get("frame_index")
                timestamp_ns = record.get("timestamp_ns")
                if (
                    isinstance(frame_index, bool)
                    or not isinstance(frame_index, int)
                    or frame_index <= previous_frame_index
                    or isinstance(timestamp_ns, bool)
                    or not isinstance(timestamp_ns, int)
                    or timestamp_ns <= 0
                    or timestamp_ns < previous_timestamp_ns
                ):
                    raise M4ValidationError(
                        f"capture {name} decoded occurrence order differs"
                    )
                previous_frame_index = frame_index
                previous_timestamp_ns = timestamp_ns
                if not start_realtime_ns <= timestamp_ns < end_realtime_ns:
                    continue
                index[
                    (
                        record.get("transport_payload_sha256"),
                        record.get("source_ip"),
                        record.get("destination_ip"),
                        record.get("source_udp_port"),
                        record.get("destination_udp_port"),
                        record.get("tos"),
                        record.get("transport_payload_size"),
                    )
                ].append(record)
            indexes[name] = index

        contract = strict_json(run_dir / "raw/m4_capacity_contract.json")
        merged_offered, merged_received = _collect_capacity_endpoint_records(
            run_dir,
            contract=contract,
            start_ns=start_ns,
            end_ns=end_ns,
        )
        offered = {
            cell: list(records.values()) for cell, records in merged_offered.items()
        }
        received = {
            cell: list(records.values()) for cell, records in merged_received.items()
        }

        role_counts: dict[str, dict[str, int]] = {}
        all_capture_names = set(indexes)
        occurrence_cursors: dict[tuple[str, tuple[Any, ...]], int] = {}
        for cell_id, cell in sorted(cells.items()):
            source_endpoint = (
                "gcs"
                if cell["source"]["namespace"] == "ams-gcs"
                else str(cell["uav"]["name"])
            )
            destination_endpoint = (
                "gcs"
                if cell["destination"]["namespace"] == "ams-gcs"
                else str(cell["uav"]["name"])
            )
            source_records = offered.get(cell_id, [])
            destination_records = received.get(cell_id, [])
            if not source_records:
                failures.append(f"capture path has no measurement offers: {cell_id}")

            def key(record: Mapping[str, Any]) -> tuple[Any, ...]:
                return (
                    record.get("transport_payload_sha256"),
                    cell["source"]["ip"],
                    cell["destination"]["ip"],
                    cell["source"]["udp_port"],
                    cell["destination"]["udp_port"],
                    cell["ns3_path"]["dscp_tos"],
                    record.get("transport_payload_size"),
                )

            roles = {
                "source_endpoint": (f"endpoint-{source_endpoint}", source_records),
                "ns3_ingress": (f"ns3-external-{source_endpoint}", source_records),
                "ns3_egress": (f"ns3-external-{destination_endpoint}", destination_records),
                "destination_endpoint": (
                    f"endpoint-{destination_endpoint}",
                    destination_records,
                ),
            }
            cell_counts: dict[str, int] = {}
            permitted = {name for name, _records in roles.values()}
            for role, (capture, expected_records) in roles.items():
                timestamp_field = (
                    "sent_monotonic_ns"
                    if role in {"source_endpoint", "ns3_ingress"}
                    else "received_monotonic_ns"
                )
                expected_count, matched_count = _consume_capture_role_occurrences(
                    indexes[capture],
                    expected_records,
                    capture=capture,
                    key_fn=key,
                    timestamp_field=timestamp_field,
                    start_ns=start_ns,
                    end_ns=end_ns,
                    start_realtime_ns=start_realtime_ns,
                    end_realtime_ns=end_realtime_ns,
                    cursors=occurrence_cursors,
                )
                if matched_count != expected_count:
                    failures.append(
                        f"{cell_id}/{role} external capture occurrences differ: "
                        f"expected={expected_count} matched={matched_count} "
                        f"missing={expected_count-matched_count}"
                    )
                cell_counts[role] = matched_count
            for capture in sorted(all_capture_names - permitted):
                if any(indexes[capture].get(key(record)) for record in source_records):
                    failures.append(f"{cell_id} payload leaked to unrelated capture {capture}")
            role_counts[cell_id] = cell_counts
        return {
            "capture_count": len(capture_specs),
            "packet_counts": packet_counts,
            "cell_role_counts": role_counts,
            "measurement_start_realtime_ns": start_realtime_ns,
            "measurement_end_realtime_ns": end_realtime_ns,
        }, failures
    except (OSError, KeyError, TypeError, ValueError, M4ValidationError) as exc:
        failures.append(f"external capture evidence cannot be validated: {exc}")
        return {}, failures


__all__ = [
    "CAPTURE_OCCURRENCE_MATCH_TOLERANCE_NS",
    "CAPTURE_REALTIME_SPAN_TOLERANCE_NS",
    "CLOCK_SAMPLE_SCHEMA",
    "CLOCK_PRODUCER_PROCESS_ROLES",
    "ENDPOINTS",
    "FROZEN_BUNDLE_ID",
    "FROZEN_BUNDLE_PATH",
    "FROZEN_BUNDLE_SHA256",
    "MAX_POSE_AGE_NS",
    "MANDATORY_CAPTURE_ROLES",
    "QUERY_DEADLINE_NS",
    "QUERY_GLOBAL_SPACING_NS",
    "QUERY_PERIOD_NS",
    "QUERY_SLOT_COUNT_PER_CELL",
    "REQUIRED_CLOCK_PRODUCERS",
    "REQUIRED_PROCESS_COUNTS",
    "RUNTIME_EVENT_SCHEMA",
    "VALIDITY_TTL_NS",
    "derive_m3_nominal_vector",
    "derive_m3_vector_from_receipt",
    "load_runtime_events",
    "nearest_rank",
    "sha256_file",
    "unique_wire_messages",
    "validate_capacity_freshness",
    "validate_capacity_runtime",
    "validate_capacity_workload",
    "validate_continuous_readiness_schedule",
    "validate_external_captures",
    "validate_clock_correlations",
    "validate_clock_process_binding",
    "validate_query_pose_runtime_binding",
    "validate_native_world_entity_observations",
    "validate_scene_prerequisite",
]
