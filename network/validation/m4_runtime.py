#!/usr/bin/env python3
"""Independent M4 scene, time, workload, and freshness derivations."""

from __future__ import annotations

import bisect
import hashlib
import json
import math
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
from network.radio_provider.sionna_packet_adapter import deterministic_loss_sample


ROOT = Path(__file__).resolve().parents[2]
FROZEN_BUNDLE_PATH = ROOT / "network/config/m4_canonical_scene_bundle.json"
FROZEN_BUNDLE_ID = "ams-m4-canonical-km-v2"
FROZEN_BUNDLE_SHA256 = (
    "be74a467aa91cc5c7e0f0d4b510b94b6c04763099931911377d5940a9e9ee0c4"
)
QUERY_PERIOD_NS = 1_000_000_000
VALIDITY_TTL_NS = 2_000_000_000
MAX_POSE_AGE_NS = 1_500_000_000
QUERY_DEADLINE_NS = 100_000_000
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
    "world_pose_bridge": 1,
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


def validate_capacity_runtime(
    records: list[dict[str, Any]],
    *,
    schedule: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    failures: list[str] = []
    details: dict[str, Any] = {}
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

    readiness = [
        record
        for record in records
        if record.get("event") == "continuous_readiness_sample"
    ]
    readiness_hosts = [record.get("host_monotonic_ns") for record in readiness]
    if (
        len(readiness) < 630
        or not readiness_hosts
        or readiness_hosts[0] > int(schedule.get("warmup_start_monotonic_ns", 0)) + 100_000_000
        or readiness_hosts[-1] < end_ns - 1_500_000_000
        or any(record.get("ready") is not True for record in readiness)
        or any(
            not 0 < readiness_hosts[index] - readiness_hosts[index - 1] <= 1_500_000_000
            for index in range(1, len(readiness_hosts))
        )
    ):
        failures.append("continuous full readiness does not cover warm-up and measurement")
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
        expected_entities = {"cp", "jammer_m4", *[f"uav{i}" for i in range(1, 6)]}
        if not isinstance(entities, dict) or set(entities) != expected_entities:
            raise M4ValidationError("active Gazebo entity set differs")
        for name, value in entities.items():
            if (
                not isinstance(value, dict)
                or not isinstance(value.get("last_host_ns"), int)
                or not isinstance(value.get("position_m"), list)
                or len(value["position_m"]) != 3
                or not all(finite_number(item) for item in value["position_m"])
                or not isinstance(value.get("orientation_quat_xyzw"), list)
                or len(value["orientation_quat_xyzw"]) != 4
                or not all(finite_number(item) for item in value["orientation_quat_xyzw"])
            ):
                raise M4ValidationError(f"active Gazebo entity evidence differs: {name}")
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
            "continuous_readiness_sample_count": len(readiness),
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
    *,
    start_ns: int,
    end_ns: int,
) -> tuple[dict[str, Any], list[str]]:
    failures: list[str] = []
    try:
        messages = unique_wire_messages(wire.get("messages", []))
    except (M4ValidationError, TypeError, ValueError) as exc:
        return {}, [str(exc)]
    queries = {
        str(message.get("query_id")): message
        for message in messages
        if message.get("message_type") == "query"
        and start_ns <= int(message.get("request_sent_monotonic_ns", -1)) < end_ns
    }
    results_by_query: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for message in messages:
        if message.get("message_type") == "result" and str(message.get("query_id")) in queries:
            results_by_query[str(message["query_id"])].append(message)
    cell_counts: dict[tuple[str, str], int] = defaultdict(int)
    stale_pose = 0
    provider_receive_stale_pose = 0
    late = 0
    ok = 0
    for query_id, query in queries.items():
        cell = (
            f"{query.get('tx_node_id')}>{query.get('rx_node_id')}",
            str(query.get("traffic_class")),
        )
        if cell not in EXPECTED_CELLS:
            failures.append(f"measurement query {query_id} uses undeclared cell {cell}")
            continue
        cell_counts[cell] += 1
        sent = query.get("request_sent_monotonic_ns")
        deadline = query.get("deadline_monotonic_ns")
        if not isinstance(sent, int) or not isinstance(deadline, int) or deadline - sent != QUERY_DEADLINE_NS:
            failures.append(f"query {query_id} changes the frozen 100-ms deadline")
        nodes = query.get("nodes")
        jammers = query.get("jammers")
        if (
            not isinstance(nodes, list)
            or {node.get("node_id") for node in nodes} != {"cp", "uav1", "uav2", "uav3", "uav4", "uav5"}
            or not isinstance(jammers, list)
            or {item.get("jammer_id") for item in jammers} != {"jammer_m4"}
        ):
            failures.append(f"query {query_id} does not carry exact six-node/jammer poses")
        for pose in [*(nodes or []), *(jammers or [])]:
            age = pose.get("freshness_age_ns")
            if (
                pose.get("stale") is not False
                or isinstance(age, bool)
                or not isinstance(age, int)
                or not 0 <= age <= MAX_POSE_AGE_NS
            ):
                stale_pose += 1
        results = results_by_query.get(query_id, [])
        if len(results) != 1:
            late += 1
            if len(results) > 1:
                failures.append(f"query {query_id} has conflicting multiple result frames")
            continue
        result = results[0]
        completed = result.get("provider_completed_monotonic_ns")
        received = result.get("provider_received_monotonic_ns")
        for pose in [*(nodes or []), *(jammers or [])]:
            pose_ns = pose.get("pose_monotonic_ns")
            if (
                isinstance(received, bool)
                or not isinstance(received, int)
                or isinstance(pose_ns, bool)
                or not isinstance(pose_ns, int)
                or not 0 <= received - pose_ns <= MAX_POSE_AGE_NS
            ):
                provider_receive_stale_pose += 1
        if (
            result.get("status") != "ok"
            or not isinstance(completed, int)
            or not isinstance(deadline, int)
            or completed > deadline
        ):
            late += 1
        else:
            ok += 1
            start = result.get("validity_start_monotonic_ns")
            expiry = result.get("expires_monotonic_ns")
            if not isinstance(start, int) or not isinstance(expiry, int) or expiry - start != VALIDITY_TTL_NS:
                failures.append(f"result {query_id} changes the frozen 2-s validity TTL")
    minimum_queries_per_cell = 570
    for cell in EXPECTED_CELLS:
        if cell_counts.get(cell, 0) < minimum_queries_per_cell:
            failures.append(f"cell {cell} has {cell_counts.get(cell, 0)} < 570 queries")
    query_count = len(queries)
    late_ratio = late / query_count if query_count else 1.0
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
        "query_count": query_count,
        "ok_result_count": ok,
        "late_result_count": late,
        "late_update_ratio": late_ratio,
        "stale_pose_count": stale_pose,
        "provider_receive_stale_pose_count": provider_receive_stale_pose,
        "query_cells": len(cell_counts),
        "minimum_queries_in_one_cell": min(cell_counts.values()) if cell_counts else 0,
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
            for record in records:
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
                expected = {str(record["transport_payload_sha256"]) for record in expected_records}
                matched = {
                    str(record["transport_payload_sha256"])
                    for record in expected_records
                    if indexes[capture].get(key(record))
                }
                if matched != expected:
                    failures.append(
                        f"{cell_id}/{role} external capture set differs: "
                        f"missing={len(expected-matched)}"
                    )
                cell_counts[role] = len(matched)
            for capture in sorted(all_capture_names - permitted):
                if any(indexes[capture].get(key(record)) for record in source_records):
                    failures.append(f"{cell_id} payload leaked to unrelated capture {capture}")
            role_counts[cell_id] = cell_counts
        return {
            "capture_count": len(capture_specs),
            "packet_counts": packet_counts,
            "cell_role_counts": role_counts,
        }, failures
    except (OSError, KeyError, TypeError, ValueError, M4ValidationError) as exc:
        failures.append(f"external capture evidence cannot be validated: {exc}")
        return {}, failures


__all__ = [
    "CLOCK_SAMPLE_SCHEMA",
    "CLOCK_PRODUCER_PROCESS_ROLES",
    "ENDPOINTS",
    "FROZEN_BUNDLE_ID",
    "FROZEN_BUNDLE_PATH",
    "FROZEN_BUNDLE_SHA256",
    "MAX_POSE_AGE_NS",
    "MANDATORY_CAPTURE_ROLES",
    "QUERY_DEADLINE_NS",
    "QUERY_PERIOD_NS",
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
    "validate_external_captures",
    "validate_clock_correlations",
    "validate_clock_process_binding",
    "validate_scene_prerequisite",
]
