"""Adversarial tests for the independently decoded M3 external matrix."""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import io
import json
import os
import shutil
import struct
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from network.bridge import actual_sitl_mavlink_endpoint as actual_endpoint
from network.scripts import m3_external_matrix_probe as producer
from network.scripts import m3_topology_monitor as topology_monitor
from network.scripts import raw_packet_capture
from network.scripts import actual_sitl_control_probe as control_probe
from network.scripts import actual_sitl_endpoint_orchestrator as actual_orchestrator
from network.validation import validate_m3_external_matrix as validator


ROOT = Path(__file__).resolve().parents[2]
MATRIX_PATH = ROOT / "network/config/endpoint_matrix_5uav.json"
RUN_ID = "m3_fixture"
RUNTIME_ID = "11" * 16
RUN_NONCE = "22" * 16
ENDPOINTS = producer.ENDPOINTS
NAMESPACES = producer.NAMESPACES


def dump(path: Path, value: object, *, mode: int = 0o664) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
    path.chmod(mode)


def dump_jsonl(path: Path, values: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
            for value in values
        )
    )


def dump_hash_chain(
    path: Path,
    values: list[dict[str, object]],
    *,
    identity: dict[str, object],
    sequence_key: str,
) -> None:
    """Write the same canonical full-record hash chain as the live producers."""

    path.parent.mkdir(parents=True, exist_ok=True)
    previous: str | None = None
    payloads: list[bytes] = []
    for sequence, value in enumerate(values, start=1):
        record = {
            **identity,
            sequence_key: sequence,
            "previous_record_sha256": previous,
            **value,
        }
        payload = validator.canonical_json(record)
        payloads.append(payload)
        previous = hashlib.sha256(payload).hexdigest()
    path.write_bytes(b"".join(payloads))


def endpoint_for_namespace(namespace: str) -> str:
    return "gcs" if namespace == "ams-gcs" else namespace.removeprefix("ams-")


def common_event(
    endpoint: str, event: str, monotonic_ns: int, **fields: object
) -> dict[str, object]:
    return {
        "schema": producer.ENDPOINT_EVENT_SCHEMA,
        "run_id": RUN_ID,
        "runtime_id": RUNTIME_ID,
        "run_nonce": RUN_NONCE,
        "event_sequence": 0,
        "monotonic_ns": monotonic_ns,
        "event": event,
        "endpoint": endpoint,
        **fields,
    }


def offered_event(
    endpoint: str,
    phase: str,
    cell: dict[str, object] | None,
    sequence: int,
    sent_ns: int,
    sequencer: producer.MavlinkSequencer,
    *,
    p2mp: bool = False,
) -> tuple[dict[str, object], dict[str, object], bytes]:
    payload, decoded = producer.encode_transport_unit(
        run_nonce=RUN_NONCE,
        phase=phase,
        cell=cell,
        sequence=sequence,
        sent_monotonic_ns=sent_ns,
        mavlink=sequencer,
        p2mp=p2mp,
    )
    if p2mp:
        source_ip, source_port = "10.71.0.10", 14800
        destination_ip, destination_port = producer.P2MP_GROUP, producer.P2MP_PORT
        cell_id = None
    else:
        assert cell is not None
        source = cell["source"]
        destination = cell["destination"]
        source_ip, source_port = source["ip"], source["udp_port"]
        destination_ip, destination_port = destination["ip"], destination["udp_port"]
        cell_id = cell["cell_id"]
    offered = common_event(
        endpoint,
        "offered",
        sent_ns + 100,
        namespace=NAMESPACES[endpoint],
        phase=phase,
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
        source_ip=source_ip,
        source_udp_port=source_port,
        destination_ip=destination_ip,
        destination_udp_port=destination_port,
        tos=producer.TOS_BY_CLASS[decoded["traffic_class"]],
        sent_monotonic_ns=sent_ns,
        send_return_size=len(payload),
        transport_payload_hex=payload.hex(),
        transport_payload_sha256=hashlib.sha256(payload).hexdigest(),
        transport_payload_size=len(payload),
    )
    return offered, decoded, payload


def receive_event(
    endpoint: str,
    phase: str,
    cell: dict[str, object] | None,
    decoded: dict[str, object],
    payload: bytes,
    received_ns: int,
    *,
    p2mp: bool = False,
) -> dict[str, object]:
    if p2mp:
        peer_ip, peer_port = "10.71.0.10", 14800
        local_port = producer.P2MP_PORT
        cell_id = None
    else:
        assert cell is not None
        peer_ip, peer_port = cell["source"]["ip"], cell["source"]["udp_port"]
        local_port = cell["destination"]["udp_port"]
        cell_id = cell["cell_id"]
    return common_event(
        endpoint,
        "remote_receive",
        received_ns,
        namespace=NAMESPACES[endpoint],
        socket_class=decoded["traffic_class"],
        phase=phase,
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
        local_ip=producer.endpoint_ip(endpoint),
        local_udp_port=local_port,
        peer_ip=peer_ip,
        peer_udp_port=peer_port,
        rx_tos=producer.TOS_BY_CLASS[decoded["traffic_class"]],
        transport_payload_hex=payload.hex(),
        transport_payload_sha256=hashlib.sha256(payload).hexdigest(),
        transport_payload_size=len(payload),
        received_monotonic_ns=received_ns,
        sent_monotonic_ns=decoded.get("sent_monotonic_ns"),
    )


def engine_events(
    epoch: int,
    config_hash: str,
    offered: dict[str, object],
    cell: dict[str, object] | None,
    sim_base: int,
    *,
    p2mp: bool = False,
) -> list[dict[str, object]]:
    traffic_class = str(offered["traffic_class"])
    if p2mp:
        link = "cp>p2mp"
        queue = "cp>p2mp.additional_data.q2"
        source_ip, destination_ip = "10.71.0.10", producer.P2MP_GROUP
        source_port, destination_port = 14800, producer.P2MP_PORT
        ingress_device = "cp.tap.ingress"
        egress_devices = [f"uav{index}.tap.egress" for index in range(1, 6)]
    else:
        assert cell is not None
        link = cell["ns3_path"]["directed_link_id"]
        queue = cell["ns3_path"]["queue_id"]
        source_ip, destination_ip = cell["source"]["ip"], cell["destination"]["ip"]
        source_port, destination_port = (
            cell["source"]["udp_port"],
            cell["destination"]["udp_port"],
        )
        ingress_device = cell["ns3_path"]["ingress_device_id"]
        egress_devices = [cell["ns3_path"]["egress_device_id"]]

    def event(
        stage: str, offset: int, device: str, root: bool = False
    ) -> dict[str, object]:
        return {
            "schema": validator.ENGINE_SCHEMA,
            "event_epoch": epoch,
            "event_sequence": 0,
            "sim_time_ns": sim_base + offset,
            "event": stage,
            "packet_wire_hash_algorithm": "sha256",
            "packet_wire_hash": hashlib.sha256(
                f"{stage}:{sim_base}:{device}".encode()
            ).hexdigest(),
            "packet_wire_size": int(offered["transport_payload_size"]) + 46,
            "packet_uid": sim_base,
            "tos": producer.TOS_BY_CLASS[traffic_class],
            "dscp": producer.TOS_BY_CLASS[traffic_class] >> 2,
            "traffic_class": traffic_class,
            "directed_link": link,
            "queue_id": queue,
            "device_id": device,
            "source_mac": "02:00:00:00:00:01",
            "destination_mac": "02:00:00:00:00:02",
            "source_ip": source_ip,
            "destination_ip": destination_ip,
            "transport_protocol": 17,
            "source_udp_port": source_port,
            "destination_udp_port": destination_port,
            "transport_payload_sha256": offered["transport_payload_sha256"],
            "transport_payload_size": offered["transport_payload_size"],
            "p2mp": p2mp,
            "root_transmission": root,
            "queue_depth_packets": 0 if stage in {"enqueue", "dequeue"} else None,
            "queue_limit_packets": 128 if stage in {"enqueue", "dequeue"} else None,
            "drop_reason": None,
            "config_sha256": config_hash,
            "seed": 42,
            "run": epoch,
        }

    records = [
        event("ingress", 0, ingress_device),
        event("enqueue", 1, f"{link.split('>')[0]}.radio"),
        event("dequeue", 2, f"{link.split('>')[0]}.radio"),
        event("channel", 3, f"{link.split('>')[0]}.radio", root=p2mp),
    ]
    records.extend(
        event("egress", 4 + index, device)
        for index, device in enumerate(egress_devices)
    )
    return records


def internet_checksum(payload: bytes) -> int:
    if len(payload) % 2:
        payload += b"\0"
    total = sum(
        int.from_bytes(payload[offset : offset + 2], "big")
        for offset in range(0, len(payload), 2)
    )
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


def ethernet_udp_frame(
    *,
    source_ip: str,
    destination_ip: str,
    source_port: int,
    destination_port: int,
    tos: int,
    payload: bytes,
) -> bytes:
    import ipaddress

    udp = (
        struct.pack(">HHHH", source_port, destination_port, 8 + len(payload), 0)
        + payload
    )
    if ":" in source_ip or ":" in destination_ip:
        version_traffic_flow = (6 << 28) | (tos << 20)
        header = (
            version_traffic_flow.to_bytes(4, "big")
            + len(udp).to_bytes(2, "big")
            + bytes((17, 64))
            + ipaddress.IPv6Address(source_ip).packed
            + ipaddress.IPv6Address(destination_ip).packed
        )
        ethernet = bytes.fromhex("02000000000202000000000186dd")
        return ethernet + header + udp
    header = bytearray(20)
    header[0] = 0x45
    header[1] = tos
    header[2:4] = (20 + len(udp)).to_bytes(2, "big")
    header[4:6] = (1).to_bytes(2, "big")
    header[6:8] = (0x4000).to_bytes(2, "big")
    header[8] = 64
    header[9] = 17
    header[12:16] = ipaddress.IPv4Address(source_ip).packed
    header[16:20] = ipaddress.IPv4Address(destination_ip).packed
    header[10:12] = internet_checksum(bytes(header)).to_bytes(2, "big")
    ethernet = bytes.fromhex("0200000000020200000000010800")
    return ethernet + bytes(header) + udp


def pcap_payload(frames: list[bytes]) -> bytes:
    global_header = bytes.fromhex("d4c3b2a1020004000000000000000000ffff000001000000")
    records = []
    for index, frame in enumerate(frames, start=1):
        records.append(struct.pack("<IIII", index, 0, len(frame), len(frame)) + frame)
    return global_header + b"".join(records)


def pcap_frames(path: Path) -> list[bytes]:
    data = path.read_bytes()
    frames: list[bytes] = []
    offset = 24
    while offset < len(data):
        included = int.from_bytes(data[offset + 8 : offset + 12], "little")
        frames.append(data[offset + 16 : offset + 16 + included])
        offset += 16 + included
    return frames


class Fixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.matrix = json.loads(MATRIX_PATH.read_text())
        self.cells = {cell["cell_id"]: cell for cell in self.matrix["cells"]}
        self.windows = {
            "positive": {
                "phase": "positive",
                "start_monotonic_ns": 20_000_000_000,
                "end_monotonic_ns": 50_000_000_000,
                "offered_per_cell": 20,
                "p2mp_roots": 0,
                "send_span_ms": 25_000,
                "expected_engine_state": "up_epoch_1",
            },
            "p2mp": {
                "phase": "p2mp",
                "start_monotonic_ns": 50_500_000_000,
                "end_monotonic_ns": 52_500_000_000,
                "offered_per_cell": 0,
                "p2mp_roots": 20,
                "send_span_ms": 1_000,
                "expected_engine_state": "up_epoch_1",
            },
            "stopped": {
                "phase": "stopped",
                "start_monotonic_ns": 54_500_000_000,
                "end_monotonic_ns": 74_500_000_000,
                "offered_per_cell": 5,
                "p2mp_roots": 0,
                "send_span_ms": 15_000,
                "expected_engine_state": "stopped",
            },
            "recovery": {
                "phase": "recovery",
                "start_monotonic_ns": 85_500_000_000,
                "end_monotonic_ns": 115_500_000_000,
                "offered_per_cell": 20,
                "p2mp_roots": 0,
                "send_span_ms": 25_000,
                "expected_engine_state": "up_epoch_2",
            },
        }
        self.endpoint_records: dict[str, list[dict[str, object]]] = {
            name: [] for name in ENDPOINTS
        }
        self.engine_records: dict[int, list[dict[str, object]]] = {1: [], 2: []}
        self.config_hashes: dict[int, str] = {}
        self._build()

    def _build(self) -> None:
        binary = (
            self.root / "fake-ns3/build/scratch/ns3.40-ams-tap-packet-engine-default"
        )
        copied = self.root / "fake-ns3/scratch/ams-tap-packet-engine.cc"
        receipt = self.root / "fake-ns3/build/ams-build-receipts/packet.json"
        binary.parent.mkdir(parents=True)
        copied.parent.mkdir(parents=True)
        receipt.parent.mkdir(parents=True)
        binary.write_bytes(b"fixture packet engine")
        binary.chmod(0o755)
        copied.write_bytes(
            (ROOT / "network/ns3/scratch/ams-tap-packet-engine.cc").read_bytes()
        )
        receipt.write_bytes(b'{"fixture":"receipt"}\n')
        receipt.chmod(0o444)
        run_receipt = self.root / "raw/ns3_build_receipt.json"
        run_receipt.parent.mkdir(parents=True)
        run_receipt.write_bytes(receipt.read_bytes())
        run_receipt.chmod(0o444)
        uav1_cells = [
            cell for cell in self.matrix["cells"] if cell["uav"]["name"] == "uav1"
        ]
        subset_payload = json.dumps(
            uav1_cells,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        m2_packet_engine = {
            "contract": "ams.tap_packet_engine/v1",
            "program": "ams-tap-packet-engine",
            "uav_count": 1,
            "source_sha256": validator.sha256_file(
                ROOT / "network/ns3/scratch/ams-tap-packet-engine.cc"
            ),
            "binary_sha256": validator.sha256_file(binary),
            "build_receipt_sha256": validator.sha256_file(receipt),
            "config_contract": "ams.tap_packet_engine/v1",
            "config_sha256": {"good": "a" * 64, "recovery": "b" * 64},
            "config_tool_sha256": validator.sha256_file(
                ROOT / "network/ns3/tap_packet_engine_config.py"
            ),
            "runner_sha256": validator.sha256_file(
                ROOT / "network/ns3/run_ns3_tap_packet_engine.sh"
            ),
            "event_schema": validator.ENGINE_SCHEMA,
        }
        m2_endpoint = {
            "schema_version": 1,
            "schema_sha256": validator.sha256_file(
                ROOT / "network/config/endpoint_transaction_schema.json"
            ),
            "matrix_sha256": validator.sha256_file(MATRIX_PATH),
            "subset_cell_ids": [cell["cell_id"] for cell in uav1_cells],
            "subset_cells_sha256": hashlib.sha256(subset_payload).hexdigest(),
        }
        m2_result = {
            "schema_version": 2,
            "contract": validator.M2_RESULT_CONTRACT,
            "validation_contract": validator.M2_EVIDENCE_CONTRACT,
            "run_id": "m2_fixture",
            "runtime_id": "33" * 16,
            "packet_engine": m2_packet_engine,
            "endpoint_transaction": m2_endpoint,
            "passed": True,
            "failures": [],
            "gates": {
                gate_name: {
                    "status": "passed",
                    "failures": [],
                    "details": {},
                }
                for gate_name in sorted(validator.M2_REQUIRED_GATES)
            },
        }
        m2_result_payload = (
            json.dumps(m2_result, allow_nan=False, indent=2, sort_keys=True) + "\n"
        ).encode()
        self.m2_receipt = self.root.parent / "m2_host_final_receipt.json"
        m2_receipt = {
            "schema_version": 1,
            "contract": validator.M2_RECEIPT_CONTRACT,
            "profile": "m2_component",
            "run_id": "m2_fixture",
            "receipt_path": "runs/m2_fixture/metrics/m2_host_final_receipt.json",
            "source_commit": "1" * 40,
            "image_reference": "fixture",
            "image_digest": "sha256:" + "2" * 64,
            "container_id": "3" * 64,
            "validation_container_id": "4" * 64,
            "consumed_nodes": ["Q0", "Q1", "Q2"],
            "qualification_content_vector": {},
            "qualification_consumption": {},
            "qualification_contract_sha256": "5" * 64,
            "formal_accepted": True,
            "passed": True,
            "failures": [],
            "result_contract": validator.M2_RESULT_CONTRACT,
            "result_sha256": hashlib.sha256(m2_result_payload).hexdigest(),
            "result": m2_result,
            "component_content_manifest": {},
            "host_validation_manifest": {},
            "status_authority": {},
            "prerequisite_receipts": {},
            "required_component_receipts": {},
        }
        m2_receipt_payload = (
            json.dumps(m2_receipt, allow_nan=False, indent=2, sort_keys=True) + "\n"
        ).encode()
        self.m2_receipt.write_bytes(m2_receipt_payload)
        self.m2_receipt.chmod(0o444)
        raw_m2_receipt = self.root / "raw/m2_component_host_final_receipt.json"
        raw_m2_receipt.write_bytes(m2_receipt_payload)
        raw_m2_receipt.chmod(0o444)
        m2_predecessor = {
            "contract": validator.M2_EXTENSION_CONTRACT,
            "receipt": {
                "external_path": str(self.m2_receipt.resolve()),
                "raw_copy_path": "raw/m2_component_host_final_receipt.json",
                "canonical_path": "runs/m2_fixture/metrics/m2_host_final_receipt.json",
                "sha256": hashlib.sha256(m2_receipt_payload).hexdigest(),
                "run_id": "m2_fixture",
                "source_commit": "1" * 40,
                "result_sha256": hashlib.sha256(m2_result_payload).hexdigest(),
            },
            "packet_engine": m2_packet_engine,
            "endpoint_transaction": m2_endpoint,
        }
        endpoint_schema_path = ROOT / "network/config/endpoint_transaction_schema.json"
        endpoint_schema_payload = endpoint_schema_path.read_bytes()
        endpoint_schema = json.loads(endpoint_schema_payload)
        endpoint_schema_copy = self.root / "raw/endpoint_transaction_schema.json"
        endpoint_schema_copy.write_bytes(endpoint_schema_payload)
        flight_source = ROOT / "network/config/scenario_5uav.yaml"
        resolved_flight_payload, resolved_flight = (
            producer.resolve_five_uav_flight_scenario(flight_source)
        )
        (self.root / "raw/resolved_flight_scenario.yaml").write_bytes(
            resolved_flight_payload
        )
        run = {
            "contract": producer.RUN_CONTRACT,
            "run_id": RUN_ID,
            "runtime_id": RUNTIME_ID,
            "run_nonce": RUN_NONCE,
            "created_monotonic_ns": 1,
            "execution": {
                "mode": "formal",
                "acceptance_eligible": True,
                "formal_m2_predecessor_bound": True,
            },
            "matrix": {
                "path": "network/config/endpoint_matrix_5uav.json",
                "sha256": validator.sha256_file(MATRIX_PATH),
                "resolved_cells_sha256": self.matrix["resolved_cells_sha256"],
                "cell_count": 30,
                "profile": "m3_full",
                "endpoint_schema": {
                    "path": "network/config/endpoint_transaction_schema.json",
                    "sha256": hashlib.sha256(endpoint_schema_payload).hexdigest(),
                    "$id": endpoint_schema["$id"],
                    "matrix_contract": self.matrix["contract"],
                    "raw_copy_path": "raw/endpoint_transaction_schema.json",
                },
            },
            "endpoint_namespaces": NAMESPACES,
            "ns3_namespace": "ams-ns3",
            "flight_runtime": {
                **resolved_flight,
                "control_endpoint_form": control_probe.ENDPOINT_FORM,
                "control_process_role_ids": {
                    "gcs": "gcs_control_probe",
                    "adapters": {
                        f"uav{index}": f"uav_control_adapter_uav{index}"
                        for index in range(1, 6)
                    },
                    "supervisor": "actual_endpoint_supervisor",
                },
                "source_path": "network/config/scenario_5uav.yaml",
                "source_sha256": validator.sha256_file(flight_source),
                "resolved_path": "raw/resolved_flight_scenario.yaml",
            },
            "packet_engine": {
                "program": "ams-tap-packet-engine",
                "path": str(binary),
                "sha256": validator.sha256_file(binary),
                "size": binary.stat().st_size,
                "contract": "ams.tap_packet_engine/v1",
                "event_schema": validator.ENGINE_SCHEMA,
                "uav_count": 5,
                "ns3_dir": str(self.root / "fake-ns3"),
                "copied_source": str(copied),
                "required_modules": validator.REQUIRED_NS3_MODULES.split(","),
                "build_receipt": {
                    "path": str(receipt),
                    "sha256": validator.sha256_file(receipt),
                },
                "lifecycle_manifest": validator.expected_engine_lifecycle_manifest(),
            },
            "m2_predecessor": m2_predecessor,
            "p2mp": {
                "group": producer.P2MP_GROUP,
                "udp_port": producer.P2MP_PORT,
                "root_nonce_domain": "ams/v1/p2mp/additional_data/downlink",
                "intended_receivers": [f"uav{index}" for index in range(1, 6)],
            },
            "source_sha256": {
                relative: validator.sha256_file(ROOT / relative)
                for relative in (
                    "network/config/endpoint_matrix_5uav.json",
                    "network/config/endpoint_transaction_schema.json",
                    "network/validation/endpoint_transaction.py",
                    "network/ns3/scratch/ams-tap-packet-engine.cc",
                    "network/ns3/tap_packet_engine_config.py",
                    "network/ns3/run_ns3_tap_packet_engine.sh",
                    "network/ns3/build_ns3_tap_packet_engine.sh",
                    "network/ns3/ns3_build_receipt.py",
                    "network/scripts/raw_packet_capture.py",
                    "network/bridge/opaque_udp_relay.py",
                    "network/bridge/runtime_clock_beacon.py",
                    "network/bridge/actual_sitl_mavlink_endpoint.py",
                    "network/scripts/actual_sitl_endpoint_orchestrator.py",
                    "network/scripts/actual_sitl_control_probe.py",
                    "network/scripts/m3_topology_monitor.py",
                    "network/scripts/write_run_provenance.py",
                    "network/scripts/m3_external_matrix_probe.py",
                    "network/scripts/run_m3_external_matrix.sh",
                    "network/scripts/validate_m3_external_matrix.py",
                    "network/validation/validate_m3_external_matrix.py",
                    "network/config/scenario_5uav.yaml",
                    "src/multiagent_simulation/launch/multiagent_simulation.launch.py",
                )
            },
        }
        dump(self.root / "raw/run_contract.json", run)
        dump(
            self.root / "raw/phase_contract.json",
            {
                "contract": producer.PHASE_CONTRACT,
                "run_id": RUN_ID,
                "runtime_id": RUNTIME_ID,
                "run_nonce": RUN_NONCE,
                "matrix_sha256": validator.sha256_file(MATRIX_PATH),
                "created_monotonic_ns": 2,
                "stop_request_monotonic_ns": 53_000_000_000,
                "restart_request_monotonic_ns": 75_000_000_000,
                "windows": list(self.windows.values()),
            },
        )
        self.command_hashes: dict[tuple[str, str], str] = {}
        for endpoint in ENDPOINTS:
            for index, window in enumerate(self.windows.values(), start=1):
                command_path = self.root / (
                    f"raw/control/{endpoint}/{index:03d}-{window['phase']}.json"
                )
                dump(
                    command_path,
                    {
                        "action": "phase",
                        "endpoint": endpoint,
                        "run_id": RUN_ID,
                        "runtime_id": RUNTIME_ID,
                        "run_nonce": RUN_NONCE,
                        **window,
                    },
                )
                self.command_hashes[(endpoint, window["phase"])] = (
                    validator.sha256_file(command_path)
                )
            dump(
                self.root / f"raw/control/{endpoint}/999-shutdown.json",
                {
                    "action": "shutdown",
                    "endpoint": endpoint,
                    "run_id": RUN_ID,
                    "runtime_id": RUNTIME_ID,
                    "run_nonce": RUN_NONCE,
                    "not_before_monotonic_ns": self.windows["recovery"][
                        "end_monotonic_ns"
                    ]
                    + 500_000_000,
                },
            )
        for index, phase in enumerate(("positive", "stopped", "recovery"), start=1):
            window = self.windows[phase]
            dump(
                self.root
                / f"raw/control/actual-control/{index:03d}-{phase}.json",
                {
                    "action": "phase",
                    "endpoint": "actual-control",
                    "run_id": RUN_ID,
                    "runtime_id": RUNTIME_ID,
                    "run_nonce": RUN_NONCE,
                    **window,
                },
            )
        dump(
            self.root / "raw/control/actual-control/999-shutdown.json",
            {
                "action": "shutdown",
                "endpoint": "actual-control",
                "run_id": RUN_ID,
                "runtime_id": RUNTIME_ID,
                "run_nonce": RUN_NONCE,
                "not_before_monotonic_ns": self.windows["recovery"][
                    "end_monotonic_ns"
                ]
                + 500_000_000,
            },
        )
        for epoch in (1, 2):
            canonical = f"fixture_epoch={epoch}\n"
            config_hash = hashlib.sha256(canonical.encode()).hexdigest()
            self.config_hashes[epoch] = config_hash
            dump(
                self.root / f"logs/ns3_epoch{epoch}_config.json",
                {
                    "contract": "ams.tap_packet_engine/v1",
                    "canonical_config": canonical,
                    "config_sha256": config_hash,
                    "resolved": {
                        "uav_count": 5,
                        "event_epoch": epoch,
                        "self_test": False,
                        "tap_gcs": "tap-gcs",
                        "tap_uavs": [f"tap-uav{index}" for index in range(1, 6)],
                    },
                },
            )
        for endpoint in ENDPOINTS:
            self.endpoint_records[endpoint].append(
                common_event(
                    endpoint,
                    "agent_ready",
                    5_000_000_000,
                    namespace=NAMESPACES[endpoint],
                )
            )
            for window in self.windows.values():
                self.endpoint_records[endpoint].append(
                    common_event(
                        endpoint,
                        "phase_start",
                        window["start_monotonic_ns"],
                        phase=window["phase"],
                        command_sha256=self.command_hashes[(endpoint, window["phase"])],
                        declared_start_monotonic_ns=window["start_monotonic_ns"],
                        declared_end_monotonic_ns=window["end_monotonic_ns"],
                        expected_engine_state=window["expected_engine_state"],
                    )
                )
                self.endpoint_records[endpoint].append(
                    common_event(
                        endpoint,
                        "phase_complete",
                        window["end_monotonic_ns"],
                        phase=window["phase"],
                        command_sha256=self.command_hashes[(endpoint, window["phase"])],
                        expected_engine_state=window["expected_engine_state"],
                    )
                )
            self.endpoint_records[endpoint].append(
                common_event(endpoint, "agent_shutdown", 116_300_000_000)
            )
        sequencers = {endpoint: producer.MavlinkSequencer() for endpoint in ENDPOINTS}
        sim_counter = {1: 1_000, 2: 1_000}
        for phase, count, epoch in (
            ("positive", 20, 1),
            ("stopped", 5, None),
            ("recovery", 20, 2),
        ):
            start = self.windows[phase]["start_monotonic_ns"]
            for cell in self.matrix["cells"]:
                if cell["traffic_class"] == "control":
                    continue
                source_endpoint = endpoint_for_namespace(cell["source"]["namespace"])
                destination_endpoint = endpoint_for_namespace(
                    cell["destination"]["namespace"]
                )
                for sequence in range(1, count + 1):
                    sent = (
                        start
                        + sequence * 100_000_000
                        + int(cell["uav"]["system_id"]) * 1_000
                    )
                    offered, decoded, payload = offered_event(
                        source_endpoint,
                        phase,
                        cell,
                        sequence,
                        sent,
                        sequencers[source_endpoint],
                    )
                    self.endpoint_records[source_endpoint].append(offered)
                    if phase != "stopped":
                        self.endpoint_records[destination_endpoint].append(
                            receive_event(
                                destination_endpoint,
                                phase,
                                cell,
                                decoded,
                                payload,
                                sent + 1_000_000,
                            )
                        )
                        assert epoch is not None
                        self.engine_records[epoch].extend(
                            engine_events(
                                epoch,
                                self.config_hashes[epoch],
                                offered,
                                cell,
                                sim_counter[epoch],
                            )
                        )
                        sim_counter[epoch] += 20
        start = self.windows["p2mp"]["start_monotonic_ns"]
        for sequence in range(1, 21):
            sent = start + sequence * 50_000_000
            offered, decoded, payload = offered_event(
                "gcs", "p2mp", None, sequence, sent, sequencers["gcs"], p2mp=True
            )
            self.endpoint_records["gcs"].append(offered)
            for endpoint in ENDPOINTS[1:]:
                self.endpoint_records[endpoint].append(
                    receive_event(
                        endpoint,
                        "p2mp",
                        None,
                        decoded,
                        payload,
                        sent + 1_000_000,
                        p2mp=True,
                    )
                )
            self.engine_records[1].extend(
                engine_events(
                    1, self.config_hashes[1], offered, None, sim_counter[1], p2mp=True
                )
            )
            sim_counter[1] += 20
        self._actual_control(sim_counter)
        self.write_endpoint_records()
        self._forbidden()
        self.write_engine_records()
        self._lifecycle()
        self._engine_lifecycle()
        self._topology()
        self._captures()
        self._continuous_topology()

    def write_endpoint_records(self) -> None:
        for endpoint, records in self.endpoint_records.items():
            records.sort(
                key=lambda item: (
                    int(item["monotonic_ns"]),
                    str(item["event"]),
                    str(item.get("cell_id")),
                )
            )
            for sequence, record in enumerate(records, start=1):
                record["event_sequence"] = sequence
            dump_jsonl(self.root / f"raw/endpoints/{endpoint}.jsonl", records)

    def write_engine_records(self) -> None:
        for epoch, records in self.engine_records.items():
            records.sort(
                key=lambda item: (int(item["sim_time_ns"]), str(item["event"]))
            )
            for sequence, record in enumerate(records, start=1):
                record["event_sequence"] = sequence
            dump_jsonl(self.root / f"logs/ns3_epoch{epoch}_events.jsonl", records)

    @staticmethod
    def _fixture_process_identity(
        pid: int,
        *,
        role: str,
        pgid: int,
        netns_inode: int,
    ) -> dict[str, object]:
        return {
            "pid": pid,
            "start_ticks": pid * 100,
            "pgid": pgid,
            "session_id": pgid,
            "cmdline_sha256": hashlib.sha256(role.encode()).hexdigest(),
            "exe_path": f"/fixture/{role}",
            "exe_sha256": hashlib.sha256(f"exe:{role}".encode()).hexdigest(),
            "exe_dev": 1,
            "exe_inode": pid,
            "exe_size": 4096,
            "netns_inode": netns_inode,
        }

    def _actual_control(self, sim_counter: dict[int, int]) -> None:
        """Build independent five-SITL evidence for the real control cells."""

        launch_pgid = 6_000
        root_inode = 90_000
        namespace_inodes = {
            f"uav{index}": 90_000
            + validator.MONITORED_NAMESPACES.index(f"ams-uav{index}")
            for index in range(1, 6)
        }
        channels: list[dict[str, object]] = []
        for index in range(1, 6):
            channels.append(
                {
                    "uav": f"uav{index}",
                    "instance": index - 1,
                    "system_id": index,
                    "namespace": f"ams-uav{index}",
                    "namespace_inode": namespace_inodes[f"uav{index}"],
                    "radio_bind": {
                        "host": f"10.71.{index}.10",
                        "port": 14600 + index,
                    },
                    "gcs_peer": {"host": "10.71.0.10", "port": 14600},
                    "tail_bind": {
                        "host": f"10.72.{index}.2",
                        "port": 14559 + index,
                    },
                    "tail_peer_host": f"10.72.{index}.1",
                    "tail_pcap_roles": {
                        "root": f"tail-root-uav{index}",
                        "uav": f"tail-uav{index}",
                    },
                    "master": {
                        "host": "127.0.0.1",
                        "port": 5760 + 10 * (index - 1),
                    },
                    "launch_pgid": launch_pgid,
                    "mavproxy": self._fixture_process_identity(
                        6_000 + index * 2 - 1,
                        role=f"mavproxy-uav{index}",
                        pgid=launch_pgid,
                        netns_inode=root_inode,
                    ),
                    "sitl": self._fixture_process_identity(
                        6_000 + index * 2,
                        role=f"arducopter-uav{index}",
                        pgid=launch_pgid,
                        netns_inode=root_inode,
                    ),
                }
            )
        manifest = {
            "schema_version": 1,
            "contract": actual_endpoint.MANIFEST_CONTRACT,
            "run_id": RUN_ID,
            "runtime_id": RUNTIME_ID,
            "run_nonce": RUN_NONCE,
            "adapter_source_sha256": validator.sha256_file(
                ROOT / "network/bridge/actual_sitl_mavlink_endpoint.py"
            ),
            "relay_core_source_sha256": validator.sha256_file(
                ROOT / "network/bridge/opaque_udp_relay.py"
            ),
            "peer_lease_ms": 5_000,
            "lineage_check_ms": 500,
            "authorization_timeout_ms": 30_000,
            "channels": channels,
        }
        actual_endpoint.validate_manifest(manifest)
        manifest_path = self.root / "raw/actual_sitl_endpoint_manifest.json"
        dump(manifest_path, manifest)
        manifest_hash = actual_endpoint.document_sha256(manifest)

        supervisor = actual_endpoint.read_process_identity(os.getpid())
        # The independent validator runs only after the formal stack has
        # stopped.  Preserve a structurally valid issuer snapshot whose PID is
        # deliberately not live, and bind it through aggregate/topology data.
        supervisor["pid"] = 2_000_000_000
        supervisor["start_ticks"] += 1
        supervisor_argv = [
            str(Path(sys.executable).resolve()),
            str(ROOT / "network/scripts/actual_sitl_endpoint_orchestrator.py"),
            "--run-dir",
            str(self.root),
            "--manifest",
            str(manifest_path),
        ]
        supervisor_cmdline = b"\0".join(
            value.encode("utf-8") for value in supervisor_argv
        ) + b"\0"
        supervisor["argv"] = supervisor_argv
        supervisor["cmdline_b64"] = base64.b64encode(supervisor_cmdline).decode(
            "ascii"
        )
        supervisor["cmdline_sha256"] = hashlib.sha256(
            supervisor_cmdline
        ).hexdigest()
        python_hash = validator.sha256_file(Path(sys.executable).resolve())
        adapter_identities: dict[str, dict[str, object]] = {}
        aggregate_channels: dict[str, object] = {}
        adapter_events: dict[str, list[dict[str, object]]] = {}
        for index, channel in enumerate(channels, start=1):
            uav = f"uav{index}"
            adapter_pid = 6_100 + index
            adapter = {
                "pid": adapter_pid,
                "start_ticks": adapter_pid * 100,
                "pgid": adapter_pid,
                "session_id": adapter_pid,
                "cmdline_sha256": hashlib.sha256(
                    f"adapter:{uav}".encode()
                ).hexdigest(),
                "exe_path": str(Path(sys.executable).resolve()),
                "exe_sha256": python_hash,
                "exe_dev": 1,
                "exe_inode": adapter_pid,
                "exe_size": Path(sys.executable).resolve().stat().st_size,
                "netns_inode": namespace_inodes[uav],
            }
            adapter_identities[uav] = adapter
            lineage = {
                "mavproxy": channel["mavproxy"],
                "sitl": channel["sitl"],
            }
            radio_socket = {
                "host": f"10.71.{index}.10",
                "port": 14600 + index,
                "inode": 70_000 + index,
            }
            tail_socket = {
                "host": f"10.72.{index}.2",
                "port": 14559 + index,
                "inode": 71_000 + index,
            }
            mavproxy_peer = {
                "host": f"10.72.{index}.1",
                "port": 15559 + index,
            }
            initial_heartbeat = control_probe.mavlink_v2_frame(
                0,
                b"\0" * 9,
                sequence=0,
                system_id=index,
                component_id=1,
            )
            candidate = {
                "schema_version": 1,
                "contract": actual_endpoint.CANDIDATE_CONTRACT,
                "status": "awaiting_external_authorization",
                "run_id": RUN_ID,
                "runtime_id": RUNTIME_ID,
                "run_nonce": RUN_NONCE,
                "uav": uav,
                "system_id": index,
                "manifest_sha256": manifest_hash,
                "created_wall_utc": "2026-01-01T00:00:00Z",
                "created_monotonic_ns": 6_300_000_000 + index,
                "adapter": adapter,
                "radio_socket": radio_socket,
                "tail_socket": tail_socket,
                "mavproxy_peer": mavproxy_peer,
                "first_tail_datagram": {
                    "bytes": len(initial_heartbeat),
                    "sha256": hashlib.sha256(initial_heartbeat).hexdigest(),
                    "mavlink_source_system_ids": [index],
                },
                "lineage": lineage,
            }
            candidate_hash = actual_endpoint.document_sha256(candidate)
            authorization = {
                "schema_version": 1,
                "contract": actual_endpoint.AUTHORIZATION_CONTRACT,
                "status": "authorized",
                "run_id": RUN_ID,
                "runtime_id": RUNTIME_ID,
                "run_nonce": RUN_NONCE,
                "uav": uav,
                "manifest_sha256": manifest_hash,
                "candidate_sha256": candidate_hash,
                "verified_candidate_lineage_sha256": actual_endpoint.document_sha256(
                    lineage
                ),
                "issuer": supervisor,
                "authorized_wall_utc": "2026-01-01T00:00:01Z",
                "authorized_monotonic_ns": 6_400_000_000 + index,
            }
            authorization_hash = actual_endpoint.document_sha256(authorization)
            ready = {
                "schema_version": 1,
                "contract": actual_endpoint.READY_CONTRACT,
                "status": "ready",
                "run_id": RUN_ID,
                "runtime_id": RUNTIME_ID,
                "run_nonce": RUN_NONCE,
                "uav": uav,
                "system_id": index,
                "manifest_sha256": manifest_hash,
                "candidate_sha256": candidate_hash,
                "authorization_sha256": authorization_hash,
                "ready_wall_utc": "2026-01-01T00:00:02Z",
                "ready_monotonic_ns": 6_500_000_000 + index,
                "adapter": adapter,
                "radio_socket": radio_socket,
                "tail_socket": tail_socket,
                "mavproxy_peer": mavproxy_peer,
                "lineage": lineage,
            }
            dump(self.root / f"raw/actual_sitl/{uav}.peer-candidate.json", candidate)
            dump(
                self.root / f"raw/actual_sitl/{uav}.authorization.json",
                authorization,
            )
            dump(self.root / f"raw/actual_sitl/{uav}.ready.json", ready)
            aggregate_channels[uav] = {
                "system_id": index,
                "candidate_sha256": candidate_hash,
                "authorization_sha256": authorization_hash,
                "ready_sha256": actual_endpoint.document_sha256(ready),
                "radio_socket": radio_socket,
                "tail_socket": tail_socket,
                "mavproxy_peer": mavproxy_peer,
                "tail_pcap_roles": channel["tail_pcap_roles"],
            }
            adapter_events[uav] = [
                {
                    "event": "adapter_bound_not_ready",
                    "wall_utc": "2026-01-01T00:00:00Z",
                    "monotonic_ns": 6_200_000_000 + index,
                },
                {
                    "event": "peer_candidate_published_not_ready",
                    "wall_utc": "2026-01-01T00:00:01Z",
                    "monotonic_ns": 6_300_000_000 + index,
                },
                {
                    "event": "adapter_ready",
                    "wall_utc": "2026-01-01T00:00:02Z",
                    "monotonic_ns": 6_500_000_000 + index,
                },
            ]

        aggregate = {
            "schema_version": 1,
            "contract": actual_orchestrator.AGGREGATE_READY_CONTRACT,
            "status": "ready",
            "run_id": RUN_ID,
            "runtime_id": RUNTIME_ID,
            "run_nonce": RUN_NONCE,
            "manifest_sha256": manifest_hash,
            "ready_wall_utc": "2026-01-01T00:00:03Z",
            "ready_monotonic_ns": 6_600_000_000,
            "supervisor": supervisor,
            "channels": aggregate_channels,
        }
        dump(self.root / "raw/state/actual-sitl-endpoints.ready.json", aggregate)

        control_pid = 6_200
        control_start_ticks = control_pid * 100
        ready_identity = {
            "run_id": RUN_ID,
            "runtime_id": RUNTIME_ID,
            "run_nonce": RUN_NONCE,
            "profile": "m3",
            "transport_nonce32": RUN_NONCE,
            "transport_nonce_derivation": "identity/full_run_nonce32",
            "role_subject": control_probe.ROLE_SUBJECT,
        }
        dump(
            self.root / "raw/state/actual-control.socket-ready.json",
            {
                "contract": "ams.actual-sitl.control-socket-ready/v1",
                **ready_identity,
                "pid": control_pid,
                "start_ticks": control_start_ticks,
                "bound_socket": ["10.71.0.10", 14600],
                "monotonic_ns": 6_700_000_000,
            },
        )
        dump(
            self.root / "raw/state/actual-control.link-ready.json",
            {
                "contract": "ams.actual-sitl.control-link-ready/v1",
                **ready_identity,
                "pid": control_pid,
                "heartbeat_counts": {
                    f"uav{index}": 3 for index in range(1, 6)
                },
                "monotonic_ns": 9_200_000_000,
            },
        )
        control_events: list[dict[str, object]] = [
            {
                "event": "actual_control_socket_ready",
                "monotonic_ns": 6_700_000_000,
                "pid": control_pid,
                "process_start_ticks": control_start_ticks,
                "namespace": "ams-gcs",
                "bound_socket": ["10.71.0.10", 14600],
                "full_run_nonce": RUN_NONCE,
                "receive_buffer_bytes": 262_144,
                "send_buffer_bytes": 262_144,
            },
            {
                "event": "actual_control_link_ready",
                "monotonic_ns": 9_200_000_000,
                "heartbeat_counts": {
                    f"uav{index}": 3 for index in range(1, 6)
                },
            },
        ]
        actual_frames: dict[str, list[bytes]] = {
            f"{prefix}-{endpoint}.pcap": []
            for prefix in ("endpoint", "ns3-external")
            for endpoint in ENDPOINTS
        }
        actual_frames.update(
            {
                name: []
                for index in range(1, 6)
                for name in (
                    f"tail-uav{index}.pcap",
                    f"tail-root-uav{index}.pcap",
                )
            }
        )
        mavlink = control_probe.MavlinkSequencer()
        response_sequence = {index: 1 for index in range(1, 6)}
        last_stopped_timeout = 0

        def external_frame(cell_id: str, payload: bytes) -> bytes:
            cell = self.cells[cell_id]
            return ethernet_udp_frame(
                source_ip=str(cell["source"]["ip"]),
                destination_ip=str(cell["destination"]["ip"]),
                source_port=int(cell["source"]["udp_port"]),
                destination_port=int(cell["destination"]["udp_port"]),
                tos=producer.TOS_BY_CLASS["control"],
                payload=payload,
            )

        def tail_frame(uav: int, direction: str, payload: bytes) -> bytes:
            source, destination = (
                (f"10.72.{uav}.2", f"10.72.{uav}.1")
                if direction == "downlink"
                else (f"10.72.{uav}.1", f"10.72.{uav}.2")
            )
            return ethernet_udp_frame(
                source_ip=source,
                destination_ip=destination,
                source_port=14559 + uav,
                destination_port=14559 + uav,
                tos=producer.TOS_BY_CLASS["control"],
                payload=payload,
            )

        for phase in ("positive", "stopped", "recovery"):
            window = self.windows[phase]
            count = int(window["offered_per_cell"])
            command_path = self.root / (
                "raw/control/actual-control/"
                f"{('positive', 'stopped', 'recovery').index(phase) + 1:03d}-{phase}.json"
            )
            command_hash = validator.sha256_file(command_path)
            if phase == "recovery":
                control_events.append(
                    {
                        "event": "recovery_drain_guard_passed",
                        "monotonic_ns": int(window["start_monotonic_ns"]) - 1,
                        "phase": phase,
                        "window_id": phase,
                        "endpoint_form": control_probe.ENDPOINT_FORM,
                        "expired_attempt_counts": {
                            f"uav{index}": 5 for index in range(1, 6)
                        },
                        "last_stopped_timeout_monotonic_ns": last_stopped_timeout,
                        "recovery_start_monotonic_ns": window[
                            "start_monotonic_ns"
                        ],
                        "quiet_drain_ns": int(window["start_monotonic_ns"])
                        - last_stopped_timeout,
                        "required_quiet_drain_ns": 10_000_000_000,
                        "expired_attempts": {
                            f"uav{index}": [] for index in range(1, 6)
                        },
                    }
                )
            control_events.append(
                {
                    "event": "actual_control_phase_start",
                    "monotonic_ns": window["start_monotonic_ns"],
                    "phase": phase,
                    "window_id": phase,
                    "transport_phase_code": control_probe.PHASE_CODES[phase],
                    "command_sha256": command_hash,
                    "offered_per_downlink_cell": count,
                    "declared_start_monotonic_ns": window["start_monotonic_ns"],
                    "declared_end_monotonic_ns": window["end_monotonic_ns"],
                    "send_span_ms": window["send_span_ms"],
                    "response_policy": (
                        "timeout_required" if phase == "stopped" else "ack_required"
                    ),
                    "expected_engine_state": window["expected_engine_state"],
                    "flow_group_ids": {
                        f"uav{index}": f"uav{index}.control.downlink"
                        for index in range(1, 6)
                    },
                }
            )
            for sequence in range(1, count + 1):
                slot_offset = (
                    (sequence - 1) * int(window["send_span_ms"]) * 1_000_000
                ) // max(1, count - 1)
                for uav in range(1, 6):
                    sent_ns = (
                        int(window["start_monotonic_ns"])
                        + slot_offset
                        + uav * 1_000
                    )
                    request = control_probe.encode_actual_control_request(
                        run_nonce=RUN_NONCE,
                        transport_nonce=RUN_NONCE,
                        phase_code=control_probe.PHASE_CODES[phase],
                        uav=uav,
                        sequence=sequence,
                        mavlink=mavlink,
                    )
                    command_frame = request["command_frame"]
                    command_sha = str(request["command_frame_sha256"])
                    control_events.append(
                        {
                            "event": "real_command_offered",
                            "monotonic_ns": sent_ns,
                            "phase": phase,
                            "window_id": phase,
                            "transport_phase_code": control_probe.PHASE_CODES[phase],
                            "flow_group_id": f"uav{uav}.control.downlink",
                            "ordinal_send_slot": sequence,
                            "transaction_id": (
                                f"{phase}:uav{uav}.control.downlink:{uav}:{sequence}"
                            ),
                            "uav": uav,
                            "sequence": sequence,
                            "endpoint_form": control_probe.ENDPOINT_FORM,
                            "cell_id": f"uav{uav}.control.downlink",
                            "flow_id": f"uav{uav}.control.downlink",
                            "record_nonce": request["record_nonce"],
                            "full_run_nonce": RUN_NONCE,
                            "marker_text": request["marker_text"],
                            "marker_frame_hex": request["marker_frame"].hex(),
                            "marker_frame_sha256": request[
                                "marker_frame_sha256"
                            ],
                            "marker_send_return_size": len(request["marker_frame"]),
                            "command_frame_hex": command_frame.hex(),
                            "command_frame_sha256": command_sha,
                            "command_send_return_size": len(command_frame),
                            "source_ip": "10.71.0.10",
                            "source_udp_port": 14600,
                            "destination_ip": f"10.71.{uav}.10",
                            "destination_udp_port": 14600 + uav,
                            "tos": producer.TOS_BY_CLASS["control"],
                            "sent_monotonic_ns": sent_ns,
                            "scheduled_send_monotonic_ns": sent_ns,
                            "send_lateness_ns": 0,
                            "requested_message_id": 148,
                            "mavlink_command": 512,
                            "target_system": uav,
                            "target_component": 1,
                        }
                    )
                    downlink_cell = f"uav{uav}.control.downlink"
                    downlink_wire = external_frame(downlink_cell, command_frame)
                    actual_frames["endpoint-gcs.pcap"].append(downlink_wire)
                    actual_frames["ns3-external-gcs.pcap"].append(downlink_wire)
                    downlink_record = {
                        "record_nonce": request["record_nonce"],
                        "transport_payload_sha256": command_sha,
                        "transport_payload_size": len(command_frame),
                        "sent_monotonic_ns": sent_ns,
                        "sequence": sequence,
                        "traffic_class": "control",
                    }
                    if phase == "stopped":
                        completed_ns = sent_ns + 3_100_000_000
                        last_stopped_timeout = max(
                            last_stopped_timeout, completed_ns
                        )
                        result = {
                            "event": "transaction_result",
                            "monotonic_ns": completed_ns,
                            "phase": phase,
                            "window_id": phase,
                            "transport_phase_code": control_probe.PHASE_CODES[phase],
                            "flow_group_id": downlink_cell,
                            "ordinal_send_slot": sequence,
                            "transaction_id": (
                                f"{phase}:{downlink_cell}:{uav}:{sequence}"
                            ),
                            "uav": uav,
                            "sequence": sequence,
                            "endpoint_form": control_probe.ENDPOINT_FORM,
                            "downlink_cell_id": downlink_cell,
                            "uplink_cell_id": f"uav{uav}.control.uplink",
                            "record_nonce": request["record_nonce"],
                            "full_run_nonce": RUN_NONCE,
                            "sent_monotonic_ns": sent_ns,
                            "scheduled_send_monotonic_ns": sent_ns,
                            "send_lateness_ns": 0,
                            "completed_monotonic_ns": completed_ns,
                            "command_frame_sha256": command_sha,
                            "marker_frame_sha256": request[
                                "marker_frame_sha256"
                            ],
                            "ack": None,
                            "requested_telemetry": None,
                            "timed_out": True,
                            "timeout_elapsed_ms": 3100.0,
                            "timeout_contract_satisfied": True,
                            "success": False,
                        }
                        control_events.append(result)
                        control_events.append(
                            {
                                "event": "stopped_attempt_quarantined",
                                "monotonic_ns": completed_ns + 1,
                                "uav": uav,
                                "sequence": sequence,
                                "record_nonce": request["record_nonce"],
                                "marker_frame_sha256": request[
                                    "marker_frame_sha256"
                                ],
                                "command_frame_sha256": command_sha,
                                "sent_monotonic_ns": sent_ns,
                                "expired_monotonic_ns": completed_ns,
                                "timeout_elapsed_ms": 3100.0,
                            }
                        )
                        continue

                    adapter_events[f"uav{uav}"].append(
                        {
                            "event": "forward",
                            "wall_utc": "2026-01-01T00:00:10Z",
                            "monotonic_ns": sent_ns + 100_000,
                            "direction": "gcs_to_tail",
                            "bytes": len(command_frame),
                            "sha256": command_sha,
                        }
                    )
                    actual_frames[f"endpoint-uav{uav}.pcap"].append(downlink_wire)
                    actual_frames[f"ns3-external-uav{uav}.pcap"].append(
                        downlink_wire
                    )
                    downlink_tail = tail_frame(uav, "downlink", command_frame)
                    for name in (
                        f"tail-uav{uav}.pcap",
                        f"tail-root-uav{uav}.pcap",
                    ):
                        actual_frames[name].append(downlink_tail)
                    self.engine_records[1 if phase == "positive" else 2].extend(
                        engine_events(
                            1 if phase == "positive" else 2,
                            self.config_hashes[1 if phase == "positive" else 2],
                            downlink_record,
                            self.cells[downlink_cell],
                            sim_counter[1 if phase == "positive" else 2],
                        )
                    )
                    sim_counter[1 if phase == "positive" else 2] += 20

                    ack_sequence = response_sequence[uav]
                    if phase == "recovery" and uav == 1 and sequence == 1:
                        # A MAVLink sequence can wrap/repeat.  This exact ACK
                        # intentionally duplicates positive/uav1/1 bytes and
                        # must correlate by peer and transaction time.
                        ack_sequence = 1
                    ack_frame = control_probe.mavlink_v2_frame(
                        77,
                        struct.pack("<HB", 512, 0),
                        sequence=ack_sequence,
                        system_id=uav,
                        component_id=1,
                    )
                    response_sequence[uav] += 1
                    telemetry_frame = control_probe.mavlink_v2_frame(
                        148,
                        b"\0" * 60,
                        sequence=response_sequence[uav],
                        system_id=uav,
                        component_id=1,
                    )
                    response_sequence[uav] += 1
                    ack_hash = hashlib.sha256(ack_frame).hexdigest()
                    telemetry_hash = hashlib.sha256(telemetry_frame).hexdigest()
                    ack_receive_ns = sent_ns + 2_000_000
                    telemetry_receive_ns = sent_ns + 2_500_000
                    for payload, payload_hash, received_ns in (
                        (ack_frame, ack_hash, ack_receive_ns),
                        (telemetry_frame, telemetry_hash, telemetry_receive_ns),
                    ):
                        control_events.append(
                            {
                                "event": "control_datagram_receive",
                                "monotonic_ns": received_ns + 100_000,
                                "peer_ip": f"10.71.{uav}.10",
                                "peer_udp_port": 14600 + uav,
                                "received_monotonic_ns": received_ns,
                                "rx_tos": producer.TOS_BY_CLASS["control"],
                                "transport_payload_hex": payload.hex(),
                                "transport_payload_sha256": payload_hash,
                                "transport_payload_size": len(payload),
                                "decoded_message_count": 1,
                            }
                        )
                    adapter_events[f"uav{uav}"].extend(
                        [
                            {
                                "event": "forward",
                                "wall_utc": "2026-01-01T00:00:11Z",
                                "monotonic_ns": sent_ns + 1_000_000,
                                "direction": "tail_to_gcs",
                                "bytes": len(ack_frame),
                                "sha256": ack_hash,
                            },
                            {
                                "event": "forward",
                                "wall_utc": "2026-01-01T00:00:11Z",
                                "monotonic_ns": sent_ns + 1_500_000,
                                "direction": "tail_to_gcs",
                                "bytes": len(telemetry_frame),
                                "sha256": telemetry_hash,
                            },
                        ]
                    )
                    ack_tail = tail_frame(uav, "uplink", ack_frame)
                    telemetry_tail = tail_frame(uav, "uplink", telemetry_frame)
                    for name in (
                        f"tail-uav{uav}.pcap",
                        f"tail-root-uav{uav}.pcap",
                    ):
                        actual_frames[name].extend([ack_tail, telemetry_tail])
                    uplink_cell = f"uav{uav}.control.uplink"
                    ack_wire = external_frame(uplink_cell, ack_frame)
                    telemetry_wire = external_frame(uplink_cell, telemetry_frame)
                    for name in (
                        f"endpoint-uav{uav}.pcap",
                        f"ns3-external-uav{uav}.pcap",
                        "ns3-external-gcs.pcap",
                        "endpoint-gcs.pcap",
                    ):
                        actual_frames[name].extend([ack_wire, telemetry_wire])
                    uplink_record = {
                        "record_nonce": validator.sha256_bytes(
                            f"{request['record_nonce']}:real-ack".encode("ascii")
                        ),
                        "transport_payload_sha256": ack_hash,
                        "transport_payload_size": len(ack_frame),
                        "sent_monotonic_ns": sent_ns + 1_000_000,
                        "sequence": sequence,
                        "traffic_class": "control",
                    }
                    self.engine_records[1 if phase == "positive" else 2].extend(
                        engine_events(
                            1 if phase == "positive" else 2,
                            self.config_hashes[1 if phase == "positive" else 2],
                            uplink_record,
                            self.cells[uplink_cell],
                            sim_counter[1 if phase == "positive" else 2],
                        )
                    )
                    sim_counter[1 if phase == "positive" else 2] += 20
                    ack = {
                        "message_type": "COMMAND_ACK",
                        "message_id": 77,
                        "source_system": uav,
                        "source_component": 1,
                        "mavlink_frame_hex": ack_frame.hex(),
                        "mavlink_frame_sha256": ack_hash,
                        "mavlink_frame_size": len(ack_frame),
                        "peer_ip": f"10.71.{uav}.10",
                        "peer_udp_port": 14600 + uav,
                        "received_monotonic_ns": ack_receive_ns,
                        "transport_payload_sha256": ack_hash,
                        "uav": uav,
                        "phase": phase,
                        "sequence": sequence,
                        "request_command_frame_sha256": command_sha,
                        "mavlink_command": 512,
                        "mavlink_result": 0,
                    }
                    telemetry = {
                        "message_type": "AUTOPILOT_VERSION",
                        "message_id": 148,
                        "source_system": uav,
                        "source_component": 1,
                        "mavlink_frame_hex": telemetry_frame.hex(),
                        "mavlink_frame_sha256": telemetry_hash,
                        "mavlink_frame_size": len(telemetry_frame),
                        "peer_ip": f"10.71.{uav}.10",
                        "peer_udp_port": 14600 + uav,
                        "received_monotonic_ns": telemetry_receive_ns,
                        "transport_payload_sha256": telemetry_hash,
                        "uav": uav,
                        "phase": phase,
                        "sequence": sequence,
                        "request_command_frame_sha256": command_sha,
                    }
                    completed_ns = sent_ns + 3_000_000
                    control_events.append(
                        {
                            "event": "transaction_result",
                            "monotonic_ns": completed_ns,
                            "phase": phase,
                            "window_id": phase,
                            "transport_phase_code": control_probe.PHASE_CODES[phase],
                            "flow_group_id": downlink_cell,
                            "ordinal_send_slot": sequence,
                            "transaction_id": (
                                f"{phase}:{downlink_cell}:{uav}:{sequence}"
                            ),
                            "uav": uav,
                            "sequence": sequence,
                            "endpoint_form": control_probe.ENDPOINT_FORM,
                            "downlink_cell_id": downlink_cell,
                            "uplink_cell_id": uplink_cell,
                            "record_nonce": request["record_nonce"],
                            "full_run_nonce": RUN_NONCE,
                            "sent_monotonic_ns": sent_ns,
                            "scheduled_send_monotonic_ns": sent_ns,
                            "send_lateness_ns": 0,
                            "completed_monotonic_ns": completed_ns,
                            "command_frame_sha256": command_sha,
                            "marker_frame_sha256": request[
                                "marker_frame_sha256"
                            ],
                            "ack": ack,
                            "requested_telemetry": telemetry,
                            "timed_out": False,
                            "timeout_elapsed_ms": 3.0,
                            "timeout_contract_satisfied": True,
                            "success": True,
                        }
                    )

            if phase == "stopped":
                for uav in range(1, 6):
                    for sequence in range(1, 6):
                        heartbeat = control_probe.mavlink_v2_frame(
                            0,
                            b"\0" * 9,
                            sequence=100 + sequence,
                            system_id=uav,
                            component_id=1,
                        )
                        heartbeat_hash = hashlib.sha256(heartbeat).hexdigest()
                        heartbeat_time = (
                            int(window["start_monotonic_ns"])
                            + sequence * 2_000_000_000
                            + uav * 1_000
                        )
                        adapter_events[f"uav{uav}"].append(
                            {
                                "event": "forward",
                                "wall_utc": "2026-01-01T00:00:20Z",
                                "monotonic_ns": heartbeat_time,
                                "direction": "tail_to_gcs",
                                "bytes": len(heartbeat),
                                "sha256": heartbeat_hash,
                            }
                        )
                        heartbeat_tail = tail_frame(uav, "uplink", heartbeat)
                        for name in (
                            f"tail-uav{uav}.pcap",
                            f"tail-root-uav{uav}.pcap",
                        ):
                            actual_frames[name].append(heartbeat_tail)
                        heartbeat_wire = external_frame(
                            f"uav{uav}.control.uplink", heartbeat
                        )
                        actual_frames[f"endpoint-uav{uav}.pcap"].append(
                            heartbeat_wire
                        )
                        actual_frames[f"ns3-external-uav{uav}.pcap"].append(
                            heartbeat_wire
                        )
            control_events.append(
                {
                    "event": "actual_control_phase_complete",
                    "monotonic_ns": window["end_monotonic_ns"],
                    "phase": phase,
                    "window_id": phase,
                    "transport_phase_code": control_probe.PHASE_CODES[phase],
                    "command_sha256": command_hash,
                    "expected_engine_state": window["expected_engine_state"],
                    "response_policy": (
                        "timeout_required" if phase == "stopped" else "ack_required"
                    ),
                    "heartbeat_counts": {
                        f"uav{index}": (0 if phase == "stopped" else 3)
                        for index in range(1, 6)
                    },
                    "offered_counts": {
                        f"uav{index}": count for index in range(1, 6)
                    },
                    "quarantined_uavs": [],
                }
            )

        control_events.append(
            {
                "event": "actual_control_shutdown",
                "monotonic_ns": 116_100_000_000,
                "command_sha256": validator.sha256_file(
                    self.root / "raw/control/actual-control/999-shutdown.json"
                ),
            }
        )
        control_events.sort(key=lambda item: int(item["monotonic_ns"]))
        dump_hash_chain(
            self.root / "raw/actual_control/events.jsonl",
            control_events,
            identity={
                "schema": control_probe.EVENT_SCHEMA,
                **ready_identity,
            },
            sequence_key="event_sequence",
        )

        for uav, events in adapter_events.items():
            events.append(
                {
                    "event": "adapter_stop",
                    "wall_utc": "2026-01-01T00:02:00Z",
                    "monotonic_ns": 116_800_000_000,
                    "reason": "stop_file",
                }
            )
            events.sort(key=lambda item: int(item["monotonic_ns"]))
            dump_hash_chain(
                self.root / f"logs/actual_sitl_{uav}.jsonl",
                events,
                identity={
                    "schema_version": 1,
                    "run_id": RUN_ID,
                    "runtime_id": RUNTIME_ID,
                    "run_nonce": RUN_NONCE,
                    "uav": uav,
                },
                sequence_key="event_seq",
            )

        supervisor_events: list[dict[str, object]] = [
            {
                "event": "supervisor_start_not_ready",
                "wall_utc": "2026-01-01T00:00:00Z",
                "monotonic_ns": 6_100_000_000,
                "pid": supervisor["pid"],
                "manifest_sha256": manifest_hash,
                "expected_uavs": list(actual_endpoint.EXPECTED_UAVS),
            },
            *(
                {
                    "event": "endpoint_authorized_not_aggregate_ready",
                    "wall_utc": "2026-01-01T00:00:01Z",
                    "monotonic_ns": 6_400_000_000 + index,
                    "endpoint_uav": f"uav{index}",
                    "candidate_sha256": aggregate_channels[f"uav{index}"][
                        "candidate_sha256"
                    ],
                    "authorization_sha256": aggregate_channels[f"uav{index}"][
                        "authorization_sha256"
                    ],
                    "mavproxy_peer": aggregate_channels[f"uav{index}"][
                        "mavproxy_peer"
                    ],
                }
                for index in range(1, 6)
            ),
            *(
                {
                    "event": "endpoint_ready_not_aggregate_ready",
                    "wall_utc": "2026-01-01T00:00:02Z",
                    "monotonic_ns": 6_500_000_000 + index,
                    "endpoint_uav": f"uav{index}",
                    "ready_sha256": aggregate_channels[f"uav{index}"][
                        "ready_sha256"
                    ],
                }
                for index in range(1, 6)
            ),
            {
                "event": "aggregate_ready",
                "wall_utc": "2026-01-01T00:00:03Z",
                "monotonic_ns": 6_600_000_000,
                "ready_path": "raw/state/actual-sitl-endpoints.ready.json",
                "ready_sha256": actual_endpoint.document_sha256(aggregate),
            },
        ]
        for sample_sequence, timestamp in enumerate(
            range(19_000_000_000, 116_000_000_001, 1_000_000_000), start=1
        ):
            supervisor_events.append(
                {
                    "event": "lineage_sample_pass",
                    "wall_utc": "2026-01-01T00:01:00Z",
                    "monotonic_ns": timestamp,
                    "sample_seq": sample_sequence,
                    "channel_lineage_sha256": {
                        f"uav{index}": hashlib.sha256(
                            f"lineage:{index}:{sample_sequence}".encode()
                        ).hexdigest()
                        for index in range(1, 6)
                    },
                }
            )
        supervisor_events.append(
            {
                "event": "supervisor_stop",
                "wall_utc": "2026-01-01T00:02:00Z",
                "monotonic_ns": 116_900_000_000,
                "reason": "stop_file",
            }
        )
        dump_hash_chain(
            self.root / "logs/actual_sitl_supervisor.jsonl",
            supervisor_events,
            identity={
                "schema_version": 1,
                "run_id": RUN_ID,
                "runtime_id": RUNTIME_ID,
                "run_nonce": RUN_NONCE,
                "uav": "all",
            },
            sequence_key="event_seq",
        )
        self.actual_capture_frames = actual_frames
        self.actual_process_identity = {
            "channels": channels,
            "adapters": adapter_identities,
            "supervisor": supervisor,
            "control": {
                "pid": control_pid,
                "start_ticks": control_start_ticks,
                "exe_sha256": python_hash,
            },
        }

    def _lifecycle(self) -> None:
        values = [
            ("run_initialized", 1_000_000_000, {"runner_pid": 1_800}),
            (
                "topology_ready",
                2_000_000_000,
                {
                    "namespace_count": 7,
                    "external_segment_count": 11,
                    "root_tail_count": 5,
                },
            ),
            (
                "captures_start_requested",
                2_800_000_000,
                {"capture_processes": 29, "tail_capture_processes": 10},
            ),
            (
                "captures_started",
                3_000_000_000,
                {"capture_processes": 29, "tail_capture_processes": 10},
            ),
            (
                "forbidden_listeners_start_requested",
                4_300_000_000,
                {"listener_processes": 6, "active_bindings": 20},
            ),
            (
                "forbidden_listeners_started",
                4_500_000_000,
                {"listener_processes": 6, "active_bindings": 20},
            ),
            (
                "endpoint_agents_start_requested",
                5_300_000_000,
                {"endpoint_agents": 6},
            ),
            (
                "endpoint_agents_started",
                5_500_000_000,
                {"endpoint_agents": 6},
            ),
            (
                "flight_stack_started",
                6_000_000_000,
                {
                    "launch_pid": 6_000,
                    "launch_pgid": 6_000,
                    "sitl_processes": 5,
                    "mavproxy_processes": 5,
                },
            ),
            (
                "actual_sitl_manifest_frozen",
                6_100_000_000,
                {
                    "channels": 5,
                    "actual_sitl_processes": 10,
                    "tail_segments": 5,
                },
            ),
            (
                "actual_sitl_adapters_ready",
                6_600_000_000,
                {
                    "adapter_processes": 5,
                    "authorized_channels": 5,
                    "tail_segments": 5,
                },
            ),
            (
                "actual_control_start_requested",
                6_650_000_000,
                {"control_socket": "10.71.0.10:14600"},
            ),
            (
                "actual_control_started",
                6_700_000_000,
                {"pid": 6_200, "control_socket": "10.71.0.10:14600"},
            ),
            (
                "engine_started",
                8_000_000_000,
                {"event_epoch": 1, "pid": 5_001},
            ),
            (
                "engine_ready",
                9_000_000_000,
                {"event_epoch": 1, "pid": 5_001},
            ),
            (
                "actual_control_link_ready",
                9_200_000_000,
                {"uav_links": 5, "minimum_real_heartbeats_per_uav": 3},
            ),
            (
                "schedule_committed",
                9_300_000_000,
                {"windows": 4, "positive_cells": 30},
            ),
            (
                "forbidden_canaries_completed",
                9_600_000_000,
                {"canary_count": 20, "remote_application_delivery": 0},
            ),
            ("engine_stop_requested", 52_900_000_000, {"event_epoch": 1}),
            ("engine_stopped", 53_100_000_000, {"event_epoch": 1, "exit_code": 0}),
            (
                "engine_restarted",
                75_000_000_000,
                {"event_epoch": 2, "pid": 5_002},
            ),
            (
                "engine_ready",
                75_100_000_000,
                {"event_epoch": 2, "pid": 5_002},
            ),
            (
                "actual_control_stop_requested",
                115_900_000_000,
                {"actual_control_processes": 1},
            ),
            (
                "endpoint_agents_stop_requested",
                116_000_000_000,
                {"endpoint_agents": 6},
            ),
            (
                "actual_control_stopped",
                116_200_000_000,
                {"exit_code": 0, "actual_control_processes": 1},
            ),
            (
                "endpoint_agents_stopped",
                116_300_000_000,
                {"exit_code": 0, "endpoint_agents": 6},
            ),
            (
                "forbidden_listeners_stop_requested",
                116_350_000_000,
                {"listener_processes": 6},
            ),
            (
                "forbidden_listeners_stopped",
                116_400_000_000,
                {"exit_code": 0, "listener_processes": 6},
            ),
            (
                "engine_final_stop_requested",
                116_450_000_000,
                {"event_epoch": 2},
            ),
            (
                "engine_final_stop",
                116_500_000_000,
                {"event_epoch": 2, "exit_code": 0},
            ),
            (
                "actual_sitl_stack_stop_requested",
                116_600_000_000,
                {
                    "adapter_processes": 5,
                    "supervisor_processes": 1,
                    "flight_process_groups": 1,
                },
            ),
            (
                "actual_sitl_stack_stopped",
                117_000_000_000,
                {
                    "adapter_exit_code": 0,
                    "supervisor_exit_code": 0,
                    "flight_exit_code": 0,
                },
            ),
            (
                "captures_stop_requested",
                117_100_000_000,
                {"capture_processes": 29, "tail_capture_processes": 10},
            ),
            (
                "captures_stopped",
                117_200_000_000,
                {
                    "exit_code": 0,
                    "capture_processes": 29,
                    "tail_capture_processes": 10,
                },
            ),
        ]
        records = [
            {
                "schema": producer.LIFECYCLE_EVENT_SCHEMA,
                "run_id": RUN_ID,
                "runtime_id": RUNTIME_ID,
                "run_nonce": RUN_NONCE,
                "event_sequence": sequence,
                "monotonic_ns": timestamp,
                "event": event,
                "details": details,
            }
            for sequence, (event, timestamp, details) in enumerate(values, start=1)
        ]
        dump_jsonl(self.root / "raw/lifecycle.jsonl", records)

    def _engine_lifecycle(self) -> None:
        """Emit the C++-owned write-once lifecycle artifact for both epochs."""

        queue_devices = ["gcs", *(f"uav{index}" for index in range(1, 6))]
        zero_depths = {
            "control_packets": 0,
            "payload_packets": 0,
            "additional_data_packets": 0,
            "total_packets": 0,
        }
        times = {
            1: (8_500_000_000, 52_950_000_000, 52_960_000_000, 52_970_000_000),
            2: (75_050_000_000, 116_460_000_000, 116_470_000_000, 116_480_000_000),
        }
        for epoch, host_times in times.items():
            config_hash = self.config_hashes[epoch]
            records: list[dict[str, object]] = []
            for sequence, (event, host_time) in enumerate(
                zip(validator.ENGINE_LIFECYCLE_EVENTS, host_times, strict=True),
                start=1,
            ):
                record: dict[str, object] = {
                    "schema": validator.ENGINE_LIFECYCLE_SCHEMA,
                    "event": event,
                    "event_sequence": sequence,
                    "event_epoch": epoch,
                    "config_sha256": config_hash,
                    "host_monotonic_ns": host_time,
                    "sim_time_ns": sequence * 1_000_000,
                }
                if event == "ready":
                    record["registered_queue_count"] = len(queue_devices)
                elif event in {"stop_observed", "stopped"}:
                    record["stop_reason"] = "stop_file"
                else:
                    record.update(
                        {
                            "stop_reason": "stop_file",
                            "queues": [
                                {
                                    "device_id": device,
                                    "before_depths": dict(zero_depths),
                                    "after_depths": dict(zero_depths),
                                    "flushed_packets": 0,
                                }
                                for device in queue_devices
                            ],
                            "all_queues_empty": True,
                        }
                    )
                records.append(record)
            dump_jsonl(
                self.root / f"logs/ns3_epoch{epoch}.lifecycle.jsonl", records
            )

    def _forbidden(self) -> None:
        canaries = producer.forbidden_canaries(RUN_NONCE)
        self.canaries = canaries
        dump(
            self.root / "raw/forbidden_canary_contract.json",
            {
                "contract": producer.FORBIDDEN_CONTRACT,
                "run_id": RUN_ID,
                "runtime_id": RUNTIME_ID,
                "run_nonce": RUN_NONCE,
                "created_monotonic_ns": 2_500_000_000,
                "canaries": canaries,
            },
        )
        sent_times = {
            canary["canary_id"]: 9_400_000_000 + canary["sequence"] * 1_000_000
            for canary in canaries
        }
        for source in ("container-root", *ENDPOINTS):
            selected = [
                canary for canary in canaries if canary["source_endpoint"] == source
            ]
            source_times = [sent_times[canary["canary_id"]] for canary in selected]
            dump(
                self.root / f"raw/forbidden/{source}.json",
                {
                    "contract": producer.FORBIDDEN_RESULT_CONTRACT,
                    "run_id": RUN_ID,
                    "runtime_id": RUNTIME_ID,
                    "run_nonce": RUN_NONCE,
                    "source_endpoint": source,
                    "started_monotonic_ns": min(source_times) - 100_000,
                    "completed_monotonic_ns": max(source_times) + 100_000,
                    "observations": [
                        {
                            "canary_id": canary["canary_id"],
                            "sequence": canary["sequence"],
                            "sent_monotonic_ns": sent_times[canary["canary_id"]],
                            "send_return_size": canary["expected_send_return_size"],
                            "transport_payload_sha256": canary[
                                "transport_payload_sha256"
                            ],
                        }
                        for canary in selected
                    ],
                },
            )

        for canary in canaries:
            if canary["kind"] not in {"legacy_direct_port", "unreachable_ipv4"}:
                continue
            base_time = 1_000_000 + canary["sequence"] * 10
            for offset, event_name in enumerate(("ingress", "drop")):
                self.engine_records[1].append(
                    {
                        "schema": validator.ENGINE_SCHEMA,
                        "event_epoch": 1,
                        "event_sequence": 0,
                        "sim_time_ns": base_time + offset,
                        "event": event_name,
                        "packet_wire_hash_algorithm": "sha256",
                        "packet_wire_hash": hashlib.sha256(
                            f"{canary['canary_id']}:{event_name}".encode()
                        ).hexdigest(),
                        "packet_wire_size": canary["transport_payload_size"] + 42,
                        "packet_uid": 900_000 + canary["sequence"],
                        "tos": canary["tos"],
                        "dscp": canary["tos"] >> 2,
                        "traffic_class": "additional_data",
                        "directed_link": "cp>unknown",
                        "queue_id": "cp>unknown.additional_data.q2",
                        "device_id": (
                            "cp.tap.ingress" if event_name == "ingress" else "cp.radio"
                        ),
                        "source_mac": "02:71:00:00:10:10",
                        "destination_mac": "02:71:00:00:00:01",
                        "source_ip": canary["source_ip"],
                        "destination_ip": canary["destination_ip"],
                        "transport_protocol": 17,
                        "source_udp_port": canary["source_udp_port"],
                        "destination_udp_port": canary["destination_udp_port"],
                        "transport_payload_sha256": canary["transport_payload_sha256"],
                        "transport_payload_size": canary["transport_payload_size"],
                        "p2mp": False,
                        "root_transmission": False,
                        "queue_depth_packets": (
                            0
                            if event_name == "drop"
                            and canary["kind"] == "legacy_direct_port"
                            else None
                        ),
                        "queue_limit_packets": (
                            128
                            if event_name == "drop"
                            and canary["kind"] == "legacy_direct_port"
                            else None
                        ),
                        "drop_reason": (
                            (
                                "udp_destination_port_not_in_endpoint_matrix"
                                if canary["kind"] == "legacy_direct_port"
                                else "ipv4_no_route"
                            )
                            if event_name == "drop"
                            else None
                        ),
                        "config_sha256": self.config_hashes[1],
                        "seed": 42,
                        "run": 1,
                    }
                )

        executable = Path(sys.executable).resolve()
        executable_sha256 = validator.sha256_file(executable)
        for index, endpoint in enumerate(ENDPOINTS):
            bindings = [
                {
                    "canary_id": canary["canary_id"],
                    "address_family": canary["address_family"],
                    "ip": canary["destination_ip"],
                    "udp_port": canary["destination_udp_port"],
                }
                for canary in canaries
                if canary["listener_endpoint"] == endpoint
            ]
            identity = {
                "pid": 4_200 + index,
                "start_ticks": (4_200 + index) * 100,
                "executable": str(executable),
                "executable_sha256": executable_sha256,
                "bindings": bindings,
            }
            listener_events = [
                {
                    "schema": producer.FORBIDDEN_LISTENER_SCHEMA,
                    "run_id": RUN_ID,
                    "runtime_id": RUNTIME_ID,
                    "run_nonce": RUN_NONCE,
                    "event_sequence": 1,
                    "monotonic_ns": 4_000_000_000,
                    "event": "listener_ready",
                    "endpoint": endpoint,
                    **identity,
                },
                {
                    "schema": producer.FORBIDDEN_LISTENER_SCHEMA,
                    "run_id": RUN_ID,
                    "runtime_id": RUNTIME_ID,
                    "run_nonce": RUN_NONCE,
                    "event_sequence": 2,
                    "monotonic_ns": 116_350_000_000,
                    "event": "listener_shutdown",
                    "endpoint": endpoint,
                    **identity,
                },
            ]
            dump_jsonl(
                self.root / f"raw/forbidden/listener-{endpoint}.jsonl",
                listener_events,
            )
            dump(
                self.root / f"raw/state/forbidden-listener-{endpoint}.ready.json",
                {
                    "contract": producer.FORBIDDEN_LISTENER_SCHEMA,
                    "run_id": RUN_ID,
                    "runtime_id": RUNTIME_ID,
                    "run_nonce": RUN_NONCE,
                    "endpoint": endpoint,
                    **identity,
                    "ready_monotonic_ns": 4_100_000_000,
                },
            )

    def _topology(self) -> None:
        for endpoint, namespace in NAMESPACES.items():
            index = 0 if endpoint == "gcs" else int(endpoint[3:])
            links = [
                {
                    "ifname": "lo",
                    "operstate": "UNKNOWN",
                    "address": "00:00:00:00:00:00",
                },
                {
                    "ifname": "eth0",
                    "operstate": "UP",
                    "address": f"02:71:{index:02x}:00:10:10",
                },
            ]
            addresses = [
                {"ifname": "lo", "addr_info": []},
                {
                    "ifname": "eth0",
                    "addr_info": [
                        {
                            "family": "inet",
                            "local": f"10.71.{index}.10",
                            "prefixlen": 24,
                        }
                    ],
                },
            ]
            routes = [
                {"dst": "default", "gateway": f"10.71.{index}.1", "dev": "eth0"},
                {"dst": f"10.71.{index}.0/24", "dev": "eth0"},
            ]
            if endpoint != "gcs":
                links.append(
                    {
                        "ifname": "tail0",
                        "operstate": "UP",
                        "address": f"02:72:{index:02x}:00:00:02",
                    }
                )
                addresses.append(
                    {
                        "ifname": "tail0",
                        "addr_info": [
                            {
                                "family": "inet",
                                "local": f"10.72.{index}.2",
                                "prefixlen": 30,
                            }
                        ],
                    }
                )
                routes.append({"dst": f"10.72.{index}.0/30", "dev": "tail0"})
            dump(
                self.root / f"raw/topology/{namespace}.link.json",
                links,
            )
            dump(
                self.root / f"raw/topology/{namespace}.addr.json",
                addresses,
            )
            dump(
                self.root / f"raw/topology/{namespace}.route.json",
                routes,
            )
        dump(
            self.root / "raw/topology/container-root.link.json",
            [
                {"ifname": "lo"},
                *({"ifname": f"ams-tail{index}"} for index in range(1, 6)),
            ],
        )
        dump(
            self.root / "raw/topology/container-root.addr.json",
            [
                {
                    "ifname": "lo",
                    "addr_info": [
                        {"family": "inet", "local": "127.0.0.1", "prefixlen": 8}
                    ],
                },
                *(
                    {
                        "ifname": f"ams-tail{index}",
                        "addr_info": [
                            {
                                "family": "inet",
                                "local": f"10.72.{index}.1",
                                "prefixlen": 30,
                            }
                        ],
                    }
                    for index in range(1, 6)
                ),
            ],
        )
        dump(
            self.root / "raw/topology/container-root.route.json",
            [
                {"dst": f"10.72.{index}.0/30", "dev": f"ams-tail{index}"}
                for index in range(1, 6)
            ],
        )
        ns3_names = (
            ["lo"]
            + [f"br-{name}" for name in ENDPOINTS]
            + [f"tap-{name}" for name in ENDPOINTS]
            + [f"vp-{name}" for name in ENDPOINTS]
        )
        dump(
            self.root / "raw/topology/ams-ns3.link.json",
            [{"ifname": name} for name in ns3_names],
        )
        dump(
            self.root / "raw/topology/ams-ns3.addr.json",
            [{"ifname": "lo", "addr_info": []}],
        )
        dump(self.root / "raw/topology/ams-ns3.route.json", [])

    def _captures(self) -> None:
        frames_by_capture: dict[str, list[bytes]] = {
            f"{prefix}-{endpoint}.pcap": []
            for prefix in ("endpoint", "ns3-external", "loopback")
            for endpoint in ENDPOINTS
        }
        frames_by_capture["loopback-container-root.pcap"] = []
        for index in range(1, 6):
            frames_by_capture[f"tail-uav{index}.pcap"] = []
            frames_by_capture[f"tail-root-uav{index}.pcap"] = []
        for endpoint, records in self.endpoint_records.items():
            for record in records:
                if record.get("event") == "offered":
                    source_ip = str(record["source_ip"])
                    destination_ip = str(record["destination_ip"])
                    source_port = int(record["source_udp_port"])
                    destination_port = int(record["destination_udp_port"])
                    tos = int(record["tos"])
                elif record.get("event") == "remote_receive":
                    source_ip = str(record["peer_ip"])
                    destination_ip = (
                        producer.P2MP_GROUP
                        if record.get("p2mp") is True
                        else str(record["local_ip"])
                    )
                    source_port = int(record["peer_udp_port"])
                    destination_port = int(record["local_udp_port"])
                    tos = int(record["rx_tos"])
                else:
                    continue
                frame = ethernet_udp_frame(
                    source_ip=source_ip,
                    destination_ip=destination_ip,
                    source_port=source_port,
                    destination_port=destination_port,
                    tos=tos,
                    payload=bytes.fromhex(str(record["transport_payload_hex"])),
                )
                frames_by_capture[f"endpoint-{endpoint}.pcap"].append(frame)
                frames_by_capture[f"ns3-external-{endpoint}.pcap"].append(frame)

        for name, frames in self.actual_capture_frames.items():
            frames_by_capture[name].extend(frames)

        for canary in self.canaries:
            frame = ethernet_udp_frame(
                source_ip=str(canary["source_ip"]),
                destination_ip=str(canary["destination_ip"]),
                source_port=int(canary["source_udp_port"]),
                destination_port=int(canary["destination_udp_port"]),
                tos=int(canary["tos"]),
                payload=bytes.fromhex(str(canary["transport_payload_hex"])),
            )
            for capture_name in canary["expected_capture_names"]:
                frames_by_capture[str(capture_name)].append(frame)

        fallback_frame = next(
            frame for frames in frames_by_capture.values() for frame in frames
        )
        capture_specs = [
            *((f"endpoint-{endpoint}", "eth0") for endpoint in ENDPOINTS),
            *((f"ns3-external-{endpoint}", f"vp-{endpoint}") for endpoint in ENDPOINTS),
            *((f"loopback-{endpoint}", "lo") for endpoint in ENDPOINTS),
            *((f"tail-uav{index}", "tail0") for index in range(1, 6)),
            *(
                (f"tail-root-uav{index}", f"ams-tail{index}")
                for index in range(1, 6)
            ),
            ("loopback-container-root", "lo"),
        ]
        for name, interface in capture_specs:
            path = self.root / f"pcap/{name}.pcap"
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = pcap_payload(frames_by_capture[path.name])
            path.write_bytes(payload)
            dump(
                self.root / f"logs/capture-{name}.json",
                {
                    "contract": raw_packet_capture.STATS_CONTRACT,
                    "interface": interface,
                    "capture_protocol": raw_packet_capture.CAPTURE_PROTOCOL,
                    "packet_filter": (
                        validator.expected_root_loopback_packet_filter(RUN_NONCE)
                        if name == "loopback-container-root"
                        else raw_packet_capture.PACKET_FILTER
                    ),
                    "pcap_path": path.name,
                    "pcap_bytes": len(payload),
                    "linktype": 1,
                    "snaplen": raw_packet_capture.SNAPLEN,
                    "receive_buffer_requested_bytes": (
                        raw_packet_capture.RECEIVE_BUFFER_REQUESTED_BYTES
                    ),
                    "receive_buffer_effective_bytes": (
                        raw_packet_capture.RECEIVE_BUFFER_EFFECTIVE_BYTES
                    ),
                    "receive_buffer_setter": "SO_RCVBUF",
                    "drain_batch_packet_limit": (
                        raw_packet_capture.DRAIN_BATCH_PACKET_LIMIT
                    ),
                    "drain_batch_byte_limit": (
                        raw_packet_capture.DRAIN_BATCH_BYTE_LIMIT
                    ),
                    "started_monotonic_ns": 3_000_000_000,
                    "stopped_monotonic_ns": 117_200_000_000,
                    "stop_signal": "SIGINT",
                    "packets_written": len(frames_by_capture[path.name]),
                    "packets_received_kernel": len(frames_by_capture[path.name]),
                    "packets_dropped_kernel": 0,
                },
            )
            (self.root / f"logs/capture-{name}.stderr").write_text("")
        for endpoint in ENDPOINTS:
            for epoch in (1, 2):
                path = self.root / f"pcap/ns3-epoch{epoch}-radio-{endpoint}.pcap"
                path.write_bytes(pcap_payload([fallback_frame]))

    def _continuous_topology(self) -> None:
        base = self.root / "raw/topology_monitor"
        netlink_dir = base / "netlink"
        netlink_dir.mkdir(parents=True)
        command_paths = {
            command: str(
                Path(shutil.which(command) or f"/usr/bin/{command}").absolute()
            )
            for command in (
                "ip",
                "bridge",
                "ss",
                "nft",
                "iptables-save",
                "ip6tables-save",
            )
        }
        default_ipv4_rules = [
            {"priority": 0, "src": "all", "table": "local"},
            {"priority": 32766, "src": "all", "table": "main"},
            {"priority": 32767, "src": "all", "table": "default"},
        ]
        default_ipv6_rules = default_ipv4_rules[:2]
        empty_firewall = {
            "nftables": {
                "nftables": [
                    {
                        "metainfo": {
                            "json_schema_version": 1,
                            "release_name": "fixture",
                            "version": "1",
                        }
                    }
                ]
            },
            "iptables_ipv4": [],
            "iptables_ipv6": [],
        }

        def namespace_record(namespace: str) -> dict[str, object]:
            loopback_local_routes = [
                {
                    "type": "local",
                    "dst": "127.0.0.0/8",
                    "dev": "lo",
                    "table": "local",
                },
                {
                    "type": "local",
                    "dst": "127.0.0.1",
                    "dev": "lo",
                    "table": "local",
                },
                {
                    "type": "broadcast",
                    "dst": "127.255.255.255",
                    "dev": "lo",
                    "table": "local",
                },
            ]
            common: dict[str, object] = {
                "present": True,
                "namespace_inode": {
                    name: 90_000 + index
                    for index, name in enumerate(validator.MONITORED_NAMESPACES)
                }[namespace],
                "routes_ipv6": [],
                "rules_ipv4": default_ipv4_rules,
                "rules_ipv6": default_ipv6_rules,
                "neighbours_ipv4": [],
                "neighbours_ipv6": [],
                "bridge_links": [],
                "sockets": [],
                **empty_firewall,
            }
            if namespace == "container-root":
                return {
                    **common,
                    "links": [
                        {"ifname": "lo"},
                        *(
                            {"ifname": f"ams-tail{index}"}
                            for index in range(1, 6)
                        ),
                    ],
                    "addresses": [
                        {
                            "ifname": "lo",
                            "addr_info": [
                                {
                                    "family": "inet",
                                    "local": "127.0.0.1",
                                    "prefixlen": 8,
                                }
                            ],
                        },
                        *(
                            {
                                "ifname": f"ams-tail{index}",
                                "addr_info": [
                                    {
                                        "family": "inet",
                                        "local": f"10.72.{index}.1",
                                        "prefixlen": 30,
                                    }
                                ],
                            }
                            for index in range(1, 6)
                        ),
                    ],
                    "routes_ipv4": [
                        {
                            "dst": f"10.72.{index}.0/30",
                            "dev": f"ams-tail{index}",
                        }
                        for index in range(1, 6)
                    ]
                    + [
                        route
                        for index in range(1, 6)
                        for route in (
                            {
                                "type": "local",
                                "dst": f"10.72.{index}.1",
                                "dev": f"ams-tail{index}",
                                "table": "local",
                            },
                            {
                                "type": "broadcast",
                                "dst": f"10.72.{index}.3",
                                "dev": f"ams-tail{index}",
                                "table": "local",
                            },
                        )
                    ]
                    + loopback_local_routes,
                }
            if namespace == "ams-ns3":
                names = (
                    ["lo"]
                    + [f"br-{name}" for name in ENDPOINTS]
                    + [f"tap-{name}" for name in ENDPOINTS]
                    + [f"vp-{name}" for name in ENDPOINTS]
                )
                return {
                    **common,
                    "links": [{"ifname": name} for name in names],
                    "addresses": [{"ifname": "lo", "addr_info": []}],
                    "routes_ipv4": loopback_local_routes,
                    "bridge_links": [
                        *(
                            {"ifname": f"br-{name}", "master": f"br-{name}"}
                            for name in ENDPOINTS
                        ),
                        *(
                            {"ifname": f"tap-{name}", "master": f"br-{name}"}
                            for name in ENDPOINTS
                        ),
                        *(
                            {"ifname": f"vp-{name}", "master": f"br-{name}"}
                            for name in ENDPOINTS
                        ),
                    ],
                }
            endpoint = (
                "gcs" if namespace == "ams-gcs" else namespace.removeprefix("ams-")
            )
            index = 0 if endpoint == "gcs" else int(endpoint[3:])
            links = [
                {"ifname": "lo", "address": "00:00:00:00:00:00"},
                {
                    "ifname": "eth0",
                    "address": f"02:71:{index:02x}:00:10:10",
                },
            ]
            addresses = [
                {"ifname": "lo", "addr_info": []},
                {
                    "ifname": "eth0",
                    "addr_info": [
                        {
                            "family": "inet",
                            "local": f"10.71.{index}.10",
                            "prefixlen": 24,
                        }
                    ],
                },
            ]
            routes = [
                {
                    "dst": "default",
                    "gateway": f"10.71.{index}.1",
                    "dev": "eth0",
                },
                {"dst": f"10.71.{index}.0/24", "dev": "eth0"},
                {
                    "type": "local",
                    "dst": f"10.71.{index}.10",
                    "dev": "eth0",
                    "table": "local",
                },
                {
                    "type": "broadcast",
                    "dst": f"10.71.{index}.255",
                    "dev": "eth0",
                    "table": "local",
                },
                *loopback_local_routes,
            ]
            if endpoint != "gcs":
                links.append(
                    {
                        "ifname": "tail0",
                        "address": f"02:72:{index:02x}:00:00:02",
                    }
                )
                addresses.append(
                    {
                        "ifname": "tail0",
                        "addr_info": [
                            {
                                "family": "inet",
                                "local": f"10.72.{index}.2",
                                "prefixlen": 30,
                            }
                        ],
                    }
                )
                routes.append({"dst": f"10.72.{index}.0/30", "dev": "tail0"})
                routes.extend(
                    [
                        {
                            "type": "local",
                            "dst": f"10.72.{index}.2",
                            "dev": "tail0",
                            "table": "local",
                        },
                        {
                            "type": "broadcast",
                            "dst": f"10.72.{index}.3",
                            "dev": "tail0",
                            "table": "local",
                        },
                    ]
                )
            return {
                **common,
                "links": links,
                "addresses": addresses,
                "routes_ipv4": routes,
            }

        namespace_inodes = {
            name: int(namespace_record(name)["namespace_inode"])
            for name in validator.MONITORED_NAMESPACES
        }

        def process(
            *,
            pid: int,
            namespace: str,
            cmdline: list[str],
            executable: str = "/usr/bin/python3.10",
            start_ticks: int | None = None,
            executable_sha256: str = "a" * 64,
        ) -> dict[str, object]:
            return {
                "pid": pid,
                "start_ticks": pid * 100 if start_ticks is None else start_ticks,
                "namespace": namespace,
                "namespace_inode": namespace_inodes[namespace],
                "executable": executable,
                "executable_sha256": executable_sha256,
                "cmdline": cmdline,
                "cap_eff": "0000000000000000",
                "cgroup": ["0::/fixture"],
            }

        lifecycle = validator.strict_jsonl(self.root / "raw/lifecycle.jsonl")
        transitions = [
            (int(record["monotonic_ns"]), str(record["event"])) for record in lifecycle
        ]
        transition_events = [event for _timestamp, event in transitions]
        periodic_times = list(
            range(1_000_000_000, 117_500_000_001, 500_000_000)
        )
        scheduled = [
            *((timestamp, "transition", event) for timestamp, event in transitions),
            *((timestamp, "periodic", None) for timestamp in periodic_times),
        ]
        scheduled.sort(key=lambda item: (item[0], item[1] != "transition"))
        records: list[dict[str, object]] = []
        transition_sequence = 0
        for sample_sequence, (timestamp, reason, event) in enumerate(
            scheduled, start=1
        ):
            if reason == "transition":
                transition_sequence += 1
            configured = timestamp >= 2_000_000_000
            namespaces = {
                name: (
                    namespace_record(name)
                    if configured or name == "container-root"
                    else {
                        "present": False,
                        "namespace_inode": None,
                        "links": [],
                        "addresses": [],
                        "routes_ipv4": [],
                        "routes_ipv6": [],
                        "rules_ipv4": [],
                        "rules_ipv6": [],
                        "neighbours_ipv4": [],
                        "neighbours_ipv6": [],
                        "bridge_links": [],
                        "sockets": [],
                        "nftables": {},
                        "iptables_ipv4": [],
                        "iptables_ipv6": [],
                    }
                )
                for name in validator.MONITORED_NAMESPACES
            }
            processes: list[dict[str, object]] = []
            monitors: dict[str, dict[str, object]] = {}
            monitored_names = (
                validator.MONITORED_NAMESPACES if configured else ("container-root",)
            )
            for index, namespace in enumerate(monitored_names):
                pid = 2_000 + index
                monitors[namespace] = {
                    "pid": pid,
                    "start_ticks": pid * 100,
                    "alive": True,
                }
                processes.append(
                    process(
                        pid=pid,
                        namespace=namespace,
                        cmdline=[command_paths["ip"], "-ts", "monitor", "all"],
                        executable=command_paths["ip"],
                    )
                )
            capture_active = 3_000_000_000 <= timestamp < 117_200_000_000
            listeners_active = 4_500_000_000 <= timestamp < 116_400_000_000
            agents_active = 5_500_000_000 <= timestamp < 116_300_000_000
            if capture_active:
                processes.append(
                    process(
                        pid=3_199,
                        namespace="container-root",
                        cmdline=[
                            "/usr/bin/python3",
                            "network/scripts/raw_packet_capture.py",
                            "--interface",
                            "lo",
                        ],
                    )
                )
                for index, endpoint in enumerate(ENDPOINTS):
                    namespace = NAMESPACES[endpoint]
                    processes.append(
                        process(
                            pid=3_000 + index,
                            namespace=namespace,
                            cmdline=[
                                "/usr/bin/python3",
                                "network/scripts/raw_packet_capture.py",
                                "--interface",
                                "eth0",
                            ],
                        )
                    )
                    if endpoint != "gcs":
                        uav_index = int(endpoint[3:])
                        processes.append(
                            process(
                                pid=3_300 + uav_index,
                                namespace=namespace,
                                cmdline=[
                                    "/usr/bin/python3",
                                    "network/scripts/raw_packet_capture.py",
                                    "--interface",
                                    "tail0",
                                ],
                            )
                        )
                        processes.append(
                            process(
                                pid=3_400 + uav_index,
                                namespace="container-root",
                                cmdline=[
                                    "/usr/bin/python3",
                                    "network/scripts/raw_packet_capture.py",
                                    "--interface",
                                    f"ams-tail{uav_index}",
                                ],
                            )
                        )
                    processes.append(
                        process(
                            pid=3_200 + index,
                            namespace=namespace,
                            cmdline=[
                                "/usr/bin/python3",
                                "network/scripts/raw_packet_capture.py",
                                "--interface",
                                "lo",
                            ],
                        )
                    )
                    processes.append(
                        process(
                            pid=3_100 + index,
                            namespace="ams-ns3",
                            cmdline=[
                                "/usr/bin/python3",
                                "network/scripts/raw_packet_capture.py",
                                "--interface",
                                f"vp-{endpoint}",
                            ],
                        )
                    )
            if listeners_active:
                for index, endpoint in enumerate(ENDPOINTS):
                    namespace = NAMESPACES[endpoint]
                    processes.append(
                        process(
                            pid=4_200 + index,
                            namespace=namespace,
                            cmdline=[
                                str(Path(sys.executable).resolve()),
                                "network/scripts/m3_external_matrix_probe.py",
                                "forbidden-listener",
                                "--endpoint",
                                endpoint,
                            ],
                            executable=str(Path(sys.executable).resolve()),
                        )
                    )
                    listener_bindings = [
                        canary
                        for canary in self.canaries
                        if canary["listener_endpoint"] == endpoint
                    ]
                    namespaces[namespace]["sockets"].extend(
                        f"UNCONN 0 0 {canary['destination_ip']}:{canary['destination_udp_port']} 0.0.0.0:*"
                        for canary in listener_bindings
                    )
            if agents_active:
                for index, endpoint in enumerate(ENDPOINTS):
                    namespace = NAMESPACES[endpoint]
                    processes.append(
                        process(
                            pid=4_000 + index,
                            namespace=namespace,
                            cmdline=[
                                "/usr/bin/python3",
                                "network/scripts/m3_external_matrix_probe.py",
                                "agent",
                                "--endpoint",
                                endpoint,
                            ],
                        )
                    )
                    ports = [14700 + index, 14800 + index]
                    if endpoint != "gcs":
                        ports.append(producer.P2MP_PORT)
                    namespaces[namespace]["sockets"].extend(
                        f"UNCONN 0 0 10.71.{index}.10:{port} 0.0.0.0:*"
                        for port in ports
                    )
            if 8_000_000_000 <= timestamp < 53_100_000_000:
                processes.append(
                    process(
                        pid=5_001,
                        namespace="ams-ns3",
                        cmdline=[
                            "/fixture/ams-tap-packet-engine",
                            "--eventEpoch=1",
                        ],
                        executable="/fixture/ams-tap-packet-engine",
                    )
                )
            if 75_000_000_000 <= timestamp < 116_500_000_000:
                processes.append(
                    process(
                        pid=5_002,
                        namespace="ams-ns3",
                        cmdline=[
                            "/fixture/ams-tap-packet-engine",
                            "--eventEpoch=2",
                        ],
                        executable="/fixture/ams-tap-packet-engine",
                    )
                )
            if 6_000_000_000 < timestamp < 117_000_000_000:
                for channel in self.actual_process_identity["channels"]:
                    for role in ("mavproxy", "sitl"):
                        identity = channel[role]
                        processes.append(
                            process(
                                pid=int(identity["pid"]),
                                start_ticks=int(identity["start_ticks"]),
                                namespace="container-root",
                                cmdline=[
                                    str(identity["exe_path"]),
                                    f"--fixture-role={role}",
                                    str(channel["uav"]),
                                ],
                                executable=str(identity["exe_path"]),
                                executable_sha256=str(identity["exe_sha256"]),
                            )
                        )
            if 6_100_000_000 < timestamp < 117_000_000_000:
                supervisor = self.actual_process_identity["supervisor"]
                processes.append(
                    process(
                        pid=int(supervisor["pid"]),
                        start_ticks=int(supervisor["start_ticks"]),
                        namespace="container-root",
                        cmdline=[
                            str(supervisor["exe_path"]),
                            "network/scripts/actual_sitl_endpoint_orchestrator.py",
                        ],
                        executable=str(supervisor["exe_path"]),
                        executable_sha256=str(supervisor["exe_sha256"]),
                    )
                )
            if 6_100_000_000 < timestamp < 117_000_000_000:
                for uav, identity in self.actual_process_identity[
                    "adapters"
                ].items():
                    processes.append(
                        process(
                            pid=int(identity["pid"]),
                            start_ticks=int(identity["start_ticks"]),
                            namespace=f"ams-{uav}",
                            cmdline=[
                                str(identity["exe_path"]),
                                "network/bridge/actual_sitl_mavlink_endpoint.py",
                                "--uav",
                                uav,
                            ],
                            executable=str(identity["exe_path"]),
                            executable_sha256=str(identity["exe_sha256"]),
                        )
                    )
            if 6_700_000_000 <= timestamp < 116_200_000_000:
                control = self.actual_process_identity["control"]
                processes.append(
                    process(
                        pid=int(control["pid"]),
                        start_ticks=int(control["start_ticks"]),
                        namespace="ams-gcs",
                        cmdline=[
                            "/usr/bin/python3",
                            "network/scripts/actual_sitl_control_probe.py",
                            "--profile",
                            "m3",
                        ],
                        executable_sha256=str(control["exe_sha256"]),
                    )
                )
            records.append(
                {
                    "schema": validator.TOPOLOGY_SAMPLE_SCHEMA,
                    "run_id": RUN_ID,
                    "runtime_id": RUNTIME_ID,
                    "run_nonce": RUN_NONCE,
                    "sample_sequence": sample_sequence,
                    "monotonic_ns": timestamp,
                    "reason": reason,
                    "transition_sequence": (
                        transition_sequence if reason == "transition" else None
                    ),
                    "transition_event": event,
                    "command_sha256": "b" * 64 if reason == "transition" else None,
                    "namespaces": namespaces,
                    "processes": sorted(processes, key=lambda item: int(item["pid"])),
                    "netlink_monitors": monitors,
                }
            )
        dump_jsonl(base / "samples.jsonl", records)
        sample_times = [int(record["monotonic_ns"]) for record in records]
        maximum_gap = max(
            right - left for left, right in zip(sample_times, sample_times[1:])
        )
        monitor_summary: dict[str, object] = {}
        for namespace in validator.MONITORED_NAMESPACES:
            stdout_path = netlink_dir / f"{namespace}.jsonl.txt"
            stderr_path = netlink_dir / f"{namespace}.stderr"
            stdout_path.write_text("")
            stderr_path.write_text("")
            monitor_summary[namespace] = {
                "pid": 2_000 + validator.MONITORED_NAMESPACES.index(namespace),
                "returncode": -2,
                "stdout_path": stdout_path.relative_to(self.root).as_posix(),
                "stdout_bytes": 0,
                "stderr_path": stderr_path.relative_to(self.root).as_posix(),
                "stderr_bytes": 0,
            }
        dump(
            base / "ready.json",
            {
                "contract": validator.TOPOLOGY_SUMMARY_CONTRACT,
                "run_id": RUN_ID,
                "runtime_id": RUNTIME_ID,
                "run_nonce": RUN_NONCE,
                "pid": 1_900,
                "start_ticks": 190_000,
                "interval_ms": 500,
                "command_paths": command_paths,
                "ready_monotonic_ns": 500_000_000,
            },
        )
        dump(
            base / "summary.json",
            {
                "contract": validator.TOPOLOGY_SUMMARY_CONTRACT,
                "run_id": RUN_ID,
                "runtime_id": RUNTIME_ID,
                "run_nonce": RUN_NONCE,
                "interval_ms": 500,
                "sample_count": len(records),
                "first_sample_monotonic_ns": sample_times[0],
                "last_sample_monotonic_ns": sample_times[-1],
                "maximum_sample_gap_ns": maximum_gap,
                "transition_events": transition_events,
                "command_paths": command_paths,
                "netlink_monitors": monitor_summary,
                "stopped_monotonic_ns": 117_500_000_000,
            },
        )
        (self.root / "logs/topology-monitor.stdout").write_text("")
        (self.root / "logs/topology-monitor.stderr").write_text("")


class M3ExternalMatrixValidatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.temporary.name) / "run"
        self.fixture = Fixture(self.run_dir)
        self.receipt_patches = (
            mock.patch(
                "network.ns3.ns3_build_receipt.build_subject",
                return_value={"fixture": True},
            ),
            mock.patch(
                "network.ns3.ns3_build_receipt.validate_receipt_file",
                return_value={"fixture": True},
            ),
        )
        for patcher in self.receipt_patches:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in reversed(self.receipt_patches):
            patcher.stop()
        self.temporary.cleanup()

    def evaluate(self) -> dict[str, object]:
        return validator.validate(
            self.run_dir,
            MATRIX_PATH,
            self.fixture.m2_receipt,
        )

    def rewrite_capture(self, name: str, frames: list[bytes]) -> None:
        path = self.run_dir / f"pcap/{name}.pcap"
        payload = pcap_payload(frames)
        path.write_bytes(payload)
        stats_path = self.run_dir / f"logs/capture-{name}.json"
        stats = json.loads(stats_path.read_text())
        stats["pcap_bytes"] = len(payload)
        stats["packets_written"] = len(frames)
        stats["packets_received_kernel"] = len(frames)
        dump(stats_path, stats)

    def rewrite_m2_receipt(self, receipt: dict[str, object]) -> None:
        result = receipt["result"]
        assert isinstance(result, dict)
        result_payload = (
            json.dumps(result, allow_nan=False, indent=2, sort_keys=True) + "\n"
        ).encode()
        receipt["result_sha256"] = hashlib.sha256(result_payload).hexdigest()
        receipt_payload = (
            json.dumps(receipt, allow_nan=False, indent=2, sort_keys=True) + "\n"
        ).encode()
        for path in (
            self.fixture.m2_receipt,
            self.run_dir / "raw/m2_component_host_final_receipt.json",
        ):
            path.chmod(0o644)
            path.write_bytes(receipt_payload)
            path.chmod(0o444)
        contract_path = self.run_dir / "raw/run_contract.json"
        contract = json.loads(contract_path.read_text())
        contract["m2_predecessor"]["receipt"]["sha256"] = hashlib.sha256(
            receipt_payload
        ).hexdigest()
        contract["m2_predecessor"]["receipt"]["result_sha256"] = receipt[
            "result_sha256"
        ]
        dump(contract_path, contract)

    def rewrite_actual_control_events(
        self, records: list[dict[str, object]]
    ) -> None:
        records.sort(key=lambda item: int(item["monotonic_ns"]))
        common_keys = {
            "schema",
            "run_id",
            "runtime_id",
            "run_nonce",
            "profile",
            "transport_nonce32",
            "transport_nonce_derivation",
            "role_subject",
            "event_sequence",
            "previous_record_sha256",
        }
        dump_hash_chain(
            self.run_dir / "raw/actual_control/events.jsonl",
            [
                {key: value for key, value in item.items() if key not in common_keys}
                for item in records
            ],
            identity={
                "schema": control_probe.EVENT_SCHEMA,
                "run_id": RUN_ID,
                "runtime_id": RUNTIME_ID,
                "run_nonce": RUN_NONCE,
                "profile": "m3",
                "transport_nonce32": RUN_NONCE,
                "transport_nonce_derivation": "identity/full_run_nonce32",
                "role_subject": control_probe.ROLE_SUBJECT,
            },
            sequence_key="event_sequence",
        )

    def mutate_positive_uav1_ack_datagram_time(
        self, timestamp: int
    ) -> None:
        records = validator.strict_jsonl(
            self.run_dir / "raw/actual_control/events.jsonl"
        )
        result = next(
            item
            for item in records
            if item.get("event") == "transaction_result"
            and item.get("phase") == "positive"
            and item.get("uav") == 1
            and item.get("sequence") == 1
        )
        ack = result["ack"]
        datagram = next(
            item
            for item in records
            if item.get("event") == "control_datagram_receive"
            and item.get("transport_payload_sha256")
            == ack["transport_payload_sha256"]
            and item.get("received_monotonic_ns")
            == ack["received_monotonic_ns"]
            and item.get("peer_ip") == ack["peer_ip"]
            and item.get("peer_udp_port") == ack["peer_udp_port"]
        )
        datagram["monotonic_ns"] = timestamp
        self.rewrite_actual_control_events(records)

    def test_complete_30_cell_external_fixture_passes(self) -> None:
        result = self.evaluate()
        self.assertTrue(result["passed"], "\n".join(result["failures"][:20]))
        capture_stats = validator.strict_json(
            self.run_dir / "logs/capture-loopback-container-root.json"
        )
        self.assertEqual(capture_stats["contract"], raw_packet_capture.STATS_CONTRACT)
        self.assertEqual(
            capture_stats["capture_protocol"], raw_packet_capture.CAPTURE_PROTOCOL
        )
        self.assertEqual(
            capture_stats["packet_filter"],
            validator.expected_root_loopback_packet_filter(RUN_NONCE),
        )
        self.assertEqual(
            capture_stats["receive_buffer_requested_bytes"],
            raw_packet_capture.RECEIVE_BUFFER_REQUESTED_BYTES,
        )
        self.assertEqual(
            capture_stats["receive_buffer_effective_bytes"],
            raw_packet_capture.RECEIVE_BUFFER_EFFECTIVE_BYTES,
        )
        self.assertIn(
            capture_stats["receive_buffer_setter"],
            {"SO_RCVBUF", "SO_RCVBUFFORCE"},
        )
        self.assertEqual(
            capture_stats["drain_batch_packet_limit"],
            raw_packet_capture.DRAIN_BATCH_PACKET_LIMIT,
        )
        self.assertEqual(
            capture_stats["drain_batch_byte_limit"],
            raw_packet_capture.DRAIN_BATCH_BYTE_LIMIT,
        )
        self.assertEqual(len(result["metrics"]["cells"]["positive"]), 30)
        rate_vector = result["metrics"]["nominal_rate_vector"]
        self.assertEqual(set(rate_vector), set(self.fixture.cells))
        actual_events = validator.strict_jsonl(
            self.run_dir / "raw/actual_control/events.jsonl"
        )
        successful_results = [
            item
            for item in actual_events
            if item.get("event") == "transaction_result"
            and item.get("success") is True
        ]
        ack_hashes = [
            str(item["ack"]["transport_payload_sha256"])
            for item in successful_results
        ]
        self.assertLess(len(set(ack_hashes)), len(ack_hashes))
        self.assertFalse(
            Path(
                f"/proc/{self.fixture.actual_process_identity['supervisor']['pid']}"
            ).exists()
        )
        for cell_id, record in rate_vector.items():
            offers = [
                item
                for item in self.fixture.endpoint_records[
                    "gcs"
                    if self.fixture.cells[cell_id]["direction"] == "downlink"
                    else self.fixture.cells[cell_id]["uav"]["name"]
                ]
                if item.get("event") == "offered"
                and item.get("phase") == "positive"
                and item.get("cell_id") == cell_id
            ]
            if self.fixture.cells[cell_id]["traffic_class"] == "control":
                uav = int(self.fixture.cells[cell_id]["uav"]["system_id"])
                if self.fixture.cells[cell_id]["direction"] == "downlink":
                    offered_bytes = sum(
                        len(bytes.fromhex(str(item["command_frame_hex"])))
                        for item in actual_events
                        if item.get("event") == "real_command_offered"
                        and item.get("phase") == "positive"
                        and item.get("uav") == uav
                    )
                else:
                    offered_bytes = sum(
                        len(bytes.fromhex(str(item["ack"]["mavlink_frame_hex"])))
                        for item in actual_events
                        if item.get("event") == "transaction_result"
                        and item.get("phase") == "positive"
                        and item.get("uav") == uav
                    )
            else:
                offered_bytes = sum(
                    int(item["transport_payload_size"]) for item in offers
                )
            self.assertEqual(
                record,
                {
                    "offered_units": 20,
                    "offered_bytes": offered_bytes,
                    "duration_ns": 30_000_000_000,
                    "unit_rate_hz": round(20 / 30, 9),
                    "byte_rate_bps": round(offered_bytes * 8 / 30, 9),
                },
            )
        self.assertEqual(result["metrics"]["p2mp"]["root_records"], 20)
        self.assertEqual(len(result["metrics"]["forbidden_paths"]), 20)
        self.assertTrue(result["gates"]["forbidden_paths"]["passed"])
        shared = result["shared_core_identity"]
        self.assertEqual(shared["packet_engine"]["m2_uav_count"], 1)
        self.assertEqual(shared["packet_engine"]["m3_uav_count"], 5)
        self.assertEqual(
            shared["packet_engine"]["binary_sha256"],
            json.loads((self.run_dir / "raw/run_contract.json").read_text())[
                "packet_engine"
            ]["sha256"],
        )
        self.assertEqual(
            set(shared["packet_engine"]["m3_config_sha256"]),
            {"epoch1", "epoch2"},
        )
        self.assertTrue(all(gate["passed"] for gate in result["gates"].values()))

        duplicate_recovery = next(
            item
            for item in actual_events
            if item.get("event") == "transaction_result"
            and item.get("phase") == "recovery"
            and item.get("uav") == 1
            and item.get("sequence") == 1
        )
        duplicate_recovery["ack"]["received_monotonic_ns"] += 1
        common_keys = {
            "schema",
            "run_id",
            "runtime_id",
            "run_nonce",
            "profile",
            "transport_nonce32",
            "transport_nonce_derivation",
            "role_subject",
            "event_sequence",
            "previous_record_sha256",
        }
        dump_hash_chain(
            self.run_dir / "raw/actual_control/events.jsonl",
            [
                {key: value for key, value in item.items() if key not in common_keys}
                for item in actual_events
            ],
            identity={
                "schema": control_probe.EVENT_SCHEMA,
                "run_id": RUN_ID,
                "runtime_id": RUNTIME_ID,
                "run_nonce": RUN_NONCE,
                "profile": "m3",
                "transport_nonce32": RUN_NONCE,
                "transport_nonce_derivation": "identity/full_run_nonce32",
                "role_subject": control_probe.ROLE_SUBJECT,
            },
            sequence_key="event_sequence",
        )
        adversarial = self.evaluate()
        self.assertFalse(adversarial["passed"])
        self.assertIn(
            "recovery/uav1/1 ACK datagram lineage differs",
            "\n".join(adversarial["gates"]["actual_sitl_control"]["failures"]),
        )

    def test_ack_datagram_event_before_receive_fails_closed(self) -> None:
        records = validator.strict_jsonl(
            self.run_dir / "raw/actual_control/events.jsonl"
        )
        result = next(
            item
            for item in records
            if item.get("event") == "transaction_result"
            and item.get("phase") == "positive"
            and item.get("uav") == 1
            and item.get("sequence") == 1
        )
        self.mutate_positive_uav1_ack_datagram_time(
            int(result["ack"]["received_monotonic_ns"]) - 1
        )

        evaluation = self.evaluate()

        self.assertFalse(evaluation["passed"])
        self.assertIn(
            "positive/uav1/1 ACK datagram lineage differs",
            "\n".join(evaluation["gates"]["actual_sitl_control"]["failures"]),
        )

    def test_ack_datagram_event_after_completion_fails_closed(self) -> None:
        records = validator.strict_jsonl(
            self.run_dir / "raw/actual_control/events.jsonl"
        )
        result = next(
            item
            for item in records
            if item.get("event") == "transaction_result"
            and item.get("phase") == "positive"
            and item.get("uav") == 1
            and item.get("sequence") == 1
        )
        self.mutate_positive_uav1_ack_datagram_time(
            int(result["monotonic_ns"]) + 1
        )

        evaluation = self.evaluate()

        self.assertFalse(evaluation["passed"])
        self.assertIn(
            "positive/uav1/1 ACK datagram lineage differs",
            "\n".join(evaluation["gates"]["actual_sitl_control"]["failures"]),
        )

    def test_omitted_runtime_source_hash_fails_exact_identity(self) -> None:
        path = self.run_dir / "raw/run_contract.json"
        contract = json.loads(path.read_text())
        contract["source_sha256"].pop("network/scripts/m3_topology_monitor.py")
        dump(path, contract)
        result = self.evaluate()
        self.assertFalse(result["passed"])
        self.assertIn(
            "source hash map key set is not exact",
            "\n".join(result["gates"]["run_identity"]["failures"]),
        )

    def test_forged_canary_lifecycle_summary_fails(self) -> None:
        path = self.run_dir / "raw/lifecycle.jsonl"
        records = validator.strict_jsonl(path)
        completed = next(
            record
            for record in records
            if record["event"] == "forbidden_canaries_completed"
        )
        completed["details"]["remote_application_delivery"] = 1
        dump_jsonl(path, records)
        result = self.evaluate()
        self.assertFalse(result["passed"])
        self.assertIn(
            "lifecycle static event details are not exact",
            "\n".join(result["gates"]["lifecycle"]["failures"]),
        )

    def test_two_missing_positive_deliveries_fail_per_cell_ratio(self) -> None:
        records = self.fixture.endpoint_records["uav1"]
        removed = 0
        retained = []
        for record in records:
            if (
                removed < 2
                and record.get("event") == "remote_receive"
                and record.get("phase") == "positive"
                and record.get("cell_id") == "uav1.payload.downlink"
            ):
                removed += 1
                continue
            retained.append(record)
        self.fixture.endpoint_records["uav1"] = retained
        self.fixture.write_endpoint_records()
        result = self.evaluate()
        self.assertFalse(result["passed"])
        self.assertIn(
            "positive/uav1.payload.downlink received 18 < 19",
            "\n".join(result["failures"]),
        )

    def test_payload_mutation_fails_independent_decode(self) -> None:
        record = next(
            item
            for item in self.fixture.endpoint_records["gcs"]
            if item.get("event") == "offered" and item.get("phase") == "positive"
        )
        raw = bytearray.fromhex(str(record["transport_payload_hex"]))
        raw[-1] ^= 1
        record["transport_payload_hex"] = raw.hex()
        self.fixture.write_endpoint_records()
        result = self.evaluate()
        self.assertFalse(result["passed"])
        self.assertIn(
            "CRC mismatch",
            "\n".join(result["gates"]["decoded_endpoint_matrix"]["failures"]),
        )

    def test_mavlink_target_system_misroute_fails_even_with_rehashed_bytes(
        self,
    ) -> None:
        path = self.run_dir / "raw/actual_control/events.jsonl"
        records = validator.strict_jsonl(path)
        record = next(
            item
            for item in records
            if item.get("event") == "real_command_offered"
            and item.get("phase") == "positive"
            and item.get("uav") == 1
            and item.get("sequence") == 1
        )
        command_frame = bytearray.fromhex(str(record["command_frame_hex"]))
        command_frame[10 + 30] = 2
        checksum = control_probe.x25_crc(
            bytes(command_frame[1:-2])
            + bytes([control_probe.MAVLINK_CRC_EXTRA[76]])
        )
        command_frame[-2:] = checksum.to_bytes(2, "little")
        record["command_frame_hex"] = command_frame.hex()
        record["command_frame_sha256"] = hashlib.sha256(command_frame).hexdigest()
        values = [
            {
                key: value
                for key, value in item.items()
                if key
                not in {
                    "schema",
                    "run_id",
                    "runtime_id",
                    "run_nonce",
                    "profile",
                    "transport_nonce32",
                    "transport_nonce_derivation",
                    "role_subject",
                    "event_sequence",
                    "previous_record_sha256",
                }
            }
            for item in records
        ]
        dump_hash_chain(
            path,
            values,
            identity={
                "schema": control_probe.EVENT_SCHEMA,
                "run_id": RUN_ID,
                "runtime_id": RUNTIME_ID,
                "run_nonce": RUN_NONCE,
                "profile": "m3",
                "transport_nonce32": RUN_NONCE,
                "transport_nonce_derivation": "identity/full_run_nonce32",
                "role_subject": control_probe.ROLE_SUBJECT,
            },
            sequence_key="event_sequence",
        )
        result = self.evaluate()
        self.assertFalse(result["passed"])
        self.assertIn(
            "positive/uav1/1 command bytes differ",
            "\n".join(result["gates"]["actual_sitl_control"]["failures"]),
        )

    def test_any_stopped_remote_delivery_fails_closed(self) -> None:
        offered = next(
            item
            for item in self.fixture.endpoint_records["gcs"]
            if item.get("event") == "offered"
            and item.get("phase") == "stopped"
            and item.get("cell_id") == "uav1.additional_data.downlink"
        )
        cell = self.fixture.cells["uav1.additional_data.downlink"]
        decoded = producer.decode_transport_unit(
            bytes.fromhex(str(offered["transport_payload_hex"]))
        )
        injected = receive_event(
            "uav1",
            "stopped",
            cell,
            decoded,
            bytes.fromhex(str(offered["transport_payload_hex"])),
            int(offered["sent_monotonic_ns"]) + 1_000_000,
        )
        self.fixture.endpoint_records["uav1"].append(injected)
        self.fixture.write_endpoint_records()
        result = self.evaluate()
        self.assertFalse(result["passed"])
        self.assertIn("delivered during stopped window", "\n".join(result["failures"]))

    def test_engine_lifecycle_requires_terminal_empty_queues(self) -> None:
        path = self.run_dir / "logs/ns3_epoch1.lifecycle.jsonl"
        records = validator.strict_jsonl(path)
        terminal = next(record for record in records if record["event"] == "queues_terminal")
        queue = terminal["queues"][0]
        queue["after_depths"] = {
            "control_packets": 1,
            "payload_packets": 0,
            "additional_data_packets": 0,
            "total_packets": 1,
        }
        queue["flushed_packets"] = -1
        dump_jsonl(path, records)

        result = self.evaluate()

        self.assertFalse(result["passed"])
        self.assertIn(
            "queue is nonempty after terminal flush",
            "\n".join(result["gates"]["packet_engine_lifecycle"]["failures"]),
        )

    def test_engine_lifecycle_final_stop_must_follow_recovery_window(self) -> None:
        path = self.run_dir / "logs/ns3_epoch2.lifecycle.jsonl"
        records = validator.strict_jsonl(path)
        shifted_times = {
            "stop_observed": 115_400_000_000,
            "queues_terminal": 115_410_000_000,
            "stopped": 115_420_000_000,
        }
        for record in records:
            if record["event"] in shifted_times:
                record["host_monotonic_ns"] = shifted_times[record["event"]]
        dump_jsonl(path, records)

        result = self.evaluate()

        self.assertFalse(result["passed"])
        self.assertIn(
            "epoch 2 raw terminal lifecycle precedes recovery end",
            "\n".join(result["gates"]["packet_engine_lifecycle"]["failures"]),
        )

    def test_duplicate_p2mp_shared_service_fails_single_root_gate(self) -> None:
        duplicate = next(
            dict(item)
            for item in self.fixture.engine_records[1]
            if item.get("event") == "channel" and item.get("p2mp") is True
        )
        duplicate["sim_time_ns"] = int(duplicate["sim_time_ns"]) + 1
        self.fixture.engine_records[1].append(duplicate)
        self.fixture.write_engine_records()
        result = self.evaluate()
        self.assertFalse(result["passed"])
        self.assertIn("stage counts are not 1/1/1/1/5", "\n".join(result["failures"]))

    def test_run_local_receipt_byte_mutation_fails(self) -> None:
        receipt = self.run_dir / "raw/ns3_build_receipt.json"
        receipt.chmod(0o644)
        receipt.write_bytes(b'{"fixture":"mutated"}\n')
        receipt.chmod(0o444)
        result = self.evaluate()
        self.assertFalse(result["passed"])
        self.assertFalse(result["gates"]["ns3_build_receipt"]["passed"])

    def test_m2_predecessor_receipt_byte_mutation_fails(self) -> None:
        receipt = self.run_dir / "raw/m2_component_host_final_receipt.json"
        receipt.chmod(0o644)
        receipt.write_bytes(receipt.read_bytes() + b" ")
        receipt.chmod(0o444)
        result = self.evaluate()
        self.assertFalse(result["passed"])
        self.assertFalse(result["gates"]["m2_extension"]["passed"])
        self.assertIn("not byte-identical", "\n".join(result["failures"]))

    def test_m2_predecessor_requires_v2_evidence_contract(self) -> None:
        receipt = json.loads(self.fixture.m2_receipt.read_text())
        receipt["result"]["validation_contract"] = "ams.m2.vertical_slice/v1"
        self.rewrite_m2_receipt(receipt)

        result = self.evaluate()

        self.assertFalse(result["passed"])
        self.assertIn(
            "current-gate-set is not passing exact",
            "\n".join(result["gates"]["m2_extension"]["failures"]),
        )

    def test_m2_predecessor_requires_complete_current_gate_set(self) -> None:
        receipt = json.loads(self.fixture.m2_receipt.read_text())
        receipt["result"]["gates"].pop("packet_engine_lifecycle")
        self.rewrite_m2_receipt(receipt)

        result = self.evaluate()

        self.assertFalse(result["passed"])
        self.assertIn(
            "current-gate-set is not passing exact",
            "\n".join(result["gates"]["m2_extension"]["failures"]),
        )

    def test_rehashed_m2_shared_source_substitution_fails(self) -> None:
        receipt = json.loads(self.fixture.m2_receipt.read_text())
        receipt["result"]["packet_engine"]["source_sha256"] = "f" * 64
        result_payload = (
            json.dumps(receipt["result"], allow_nan=False, indent=2, sort_keys=True)
            + "\n"
        ).encode()
        receipt["result_sha256"] = hashlib.sha256(result_payload).hexdigest()
        receipt_payload = (
            json.dumps(receipt, allow_nan=False, indent=2, sort_keys=True) + "\n"
        ).encode()
        for path in (
            self.fixture.m2_receipt,
            self.run_dir / "raw/m2_component_host_final_receipt.json",
        ):
            path.chmod(0o644)
            path.write_bytes(receipt_payload)
            path.chmod(0o444)
        contract_path = self.run_dir / "raw/run_contract.json"
        contract = json.loads(contract_path.read_text())
        contract["m2_predecessor"]["receipt"]["sha256"] = hashlib.sha256(
            receipt_payload
        ).hexdigest()
        contract["m2_predecessor"]["receipt"]["result_sha256"] = receipt[
            "result_sha256"
        ]
        contract["m2_predecessor"]["packet_engine"] = receipt["result"]["packet_engine"]
        dump(contract_path, contract)
        result = self.evaluate()
        self.assertFalse(result["passed"])
        self.assertIn(
            "does not bind the current shared engine",
            "\n".join(result["failures"]),
        )

    def test_truncated_capture_fails_capture_gate(self) -> None:
        (self.run_dir / "pcap/ns3-external-uav3.pcap").write_bytes(b"bad")
        result = self.evaluate()
        self.assertFalse(result["passed"])
        self.assertIn(
            "PCAP ns3-external-uav3.pcap is truncated",
            "\n".join(result["failures"]),
        )

    def test_nonzero_kernel_capture_drop_fails_capture_gate(self) -> None:
        path = self.run_dir / "logs/capture-ns3-external-uav3.json"
        stats = json.loads(path.read_text())
        stats["packets_dropped_kernel"] = 1
        dump(path, stats)
        result = self.evaluate()
        self.assertFalse(result["passed"])
        self.assertIn("drop accounting mismatch", "\n".join(result["failures"]))

    def test_root_loopback_filter_must_cover_every_declared_canary_port(self) -> None:
        path = self.run_dir / "logs/capture-loopback-container-root.json"
        stats = json.loads(path.read_text())
        stats["packet_filter"] = raw_packet_capture.PACKET_FILTER
        dump(path, stats)

        result = self.evaluate()

        self.assertFalse(result["passed"])
        self.assertIn("drop accounting mismatch", "\n".join(result["failures"]))

    def test_missing_matching_pcap_payload_fails_byte_correlation(self) -> None:
        offered = next(
            item
            for item in validator.strict_jsonl(
                self.run_dir / "raw/actual_control/events.jsonl"
            )
            if item.get("event") == "real_command_offered"
            and item.get("phase") == "positive"
            and item.get("uav") == 1
            and item.get("sequence") == 1
        )
        target_hash = str(offered["command_frame_sha256"])
        pcap_path = self.run_dir / "pcap/endpoint-gcs.pcap"
        frames = pcap_frames(pcap_path)
        retained = []
        removed = 0
        for index, frame in enumerate(frames, start=1):
            decoded, error = validator._decode_ethernet_udp(
                frame, frame_index=index, timestamp_ns=index * 1_000_000_000
            )
            self.assertIsNone(error)
            if (
                decoded is not None
                and decoded["transport_payload_sha256"] == target_hash
            ):
                removed += 1
                continue
            retained.append(frame)
        self.assertEqual(removed, 1)
        payload = pcap_payload(retained)
        pcap_path.write_bytes(payload)
        stats_path = self.run_dir / "logs/capture-endpoint-gcs.json"
        stats = json.loads(stats_path.read_text())
        stats["pcap_bytes"] = len(payload)
        stats["packets_written"] = len(retained)
        stats["packets_received_kernel"] = len(retained)
        dump(stats_path, stats)
        result = self.evaluate()
        self.assertFalse(result["passed"])
        self.assertIn(
            "positive/uav1.control.downlink/source_before_adapter decoded payload set differs",
            "\n".join(result["gates"]["pcap_transport"]["failures"]),
        )

    def test_missing_expected_forbidden_canary_capture_fails(self) -> None:
        canary = next(
            item
            for item in self.fixture.canaries
            if item["canary_id"] == "loopback_ipv4.gcs"
        )
        name = "loopback-gcs"
        retained: list[bytes] = []
        removed = 0
        for index, frame in enumerate(
            pcap_frames(self.run_dir / f"pcap/{name}.pcap"), start=1
        ):
            decoded, error = validator._decode_ethernet_udp(
                frame, frame_index=index, timestamp_ns=index * 1_000_000_000
            )
            self.assertIsNone(error)
            if (
                decoded
                and decoded["transport_payload_sha256"]
                == canary["transport_payload_sha256"]
            ):
                removed += 1
            else:
                retained.append(frame)
        self.assertGreaterEqual(removed, 1)
        self.rewrite_capture(name, retained)
        result = self.evaluate()
        self.assertFalse(result["passed"])
        self.assertIn(
            "loopback_ipv4.gcs is absent from expected captures",
            "\n".join(result["gates"]["forbidden_paths"]["failures"]),
        )

    def test_forbidden_canary_leak_to_uav_capture_fails(self) -> None:
        canary = next(
            item
            for item in self.fixture.canaries
            if item["canary_id"] == "legacy_direct_port.uav1"
        )
        source_frames = pcap_frames(self.run_dir / "pcap/endpoint-gcs.pcap")
        leaked_frame = next(
            frame
            for index, frame in enumerate(source_frames, start=1)
            if (
                validator._decode_ethernet_udp(
                    frame, frame_index=index, timestamp_ns=index * 1_000_000_000
                )[0]
                or {}
            ).get("transport_payload_sha256")
            == canary["transport_payload_sha256"]
        )
        name = "endpoint-uav1"
        frames = pcap_frames(self.run_dir / f"pcap/{name}.pcap")
        self.rewrite_capture(name, [*frames, leaked_frame])
        result = self.evaluate()
        self.assertFalse(result["passed"])
        self.assertIn(
            "legacy_direct_port.uav1 appeared on forbidden captures",
            "\n".join(result["gates"]["forbidden_paths"]["failures"]),
        )

    def test_forbidden_listener_receive_event_fails(self) -> None:
        path = self.run_dir / "raw/forbidden/listener-uav1.jsonl"
        events = validator.strict_jsonl(path)
        received = {
            **events[0],
            "event_sequence": 2,
            "monotonic_ns": 9_250_000_000,
            "event": "forbidden_receive",
        }
        events[1]["event_sequence"] = 3
        dump_jsonl(path, [events[0], received, events[1]])
        result = self.evaluate()
        self.assertFalse(result["passed"])
        self.assertIn(
            "forbidden listener received traffic/restarted: uav1",
            "\n".join(result["gates"]["forbidden_paths"]["failures"]),
        )

    def test_forbidden_listener_pid_restart_fails_continuous_identity(self) -> None:
        path = self.run_dir / "raw/topology_monitor/samples.jsonl"
        samples = validator.strict_jsonl(path)
        sample = next(
            item for item in samples if item["monotonic_ns"] == 30_000_000_000
        )
        listener = next(
            process
            for process in sample["processes"]
            if process["namespace"] == "ams-uav2"
            and "forbidden-listener" in process["cmdline"]
        )
        listener["pid"] = 44_445
        listener["start_ticks"] = 55_556
        dump_jsonl(path, samples)
        result = self.evaluate()
        self.assertFalse(result["passed"])
        self.assertIn(
            "forbidden listener PID/start_ticks differs from ready evidence: ams-uav2",
            "\n".join(result["gates"]["continuous_topology"]["failures"]),
        )

    def test_forbidden_ns3_drop_reason_mutation_fails(self) -> None:
        record = next(
            item
            for item in self.fixture.engine_records[1]
            if item.get("event") == "drop" and item.get("destination_udp_port") == 14550
        )
        record["drop_reason"] = "queue_limit_additional_data"
        self.fixture.write_engine_records()
        result = self.evaluate()
        self.assertFalse(result["passed"])
        self.assertIn(
            "ns3 terminal drop reason is not the endpoint-port allowlist",
            "\n".join(result["gates"]["forbidden_paths"]["failures"]),
        )

    def test_unreachable_ns3_requires_explicit_ipv4_no_route_drop(self) -> None:
        record = next(
            item
            for item in self.fixture.engine_records[1]
            if item.get("event") == "drop"
            and item.get("destination_ip") == "198.18.0.1"
        )
        self.assertEqual(record["drop_reason"], "ipv4_no_route")
        self.assertEqual(record["source_udp_port"], 15301)
        self.assertEqual(record["destination_udp_port"], 15300)
        record["drop_reason"] = "udp_destination_port_not_in_endpoint_matrix"
        self.fixture.write_engine_records()
        result = self.evaluate()
        self.assertFalse(result["passed"])
        self.assertIn(
            "unreachable_ipv4.gcs ns3 terminal drop reason is not explicit IPv4 no-route",
            "\n".join(result["gates"]["forbidden_paths"]["failures"]),
        )

    def test_duplicate_jsonl_key_fails_closed(self) -> None:
        path = self.run_dir / "raw/endpoints/gcs.jsonl"
        lines = path.read_text().splitlines()
        lines[0] = lines[0].replace(
            '"event":"agent_ready"',
            '"event":"agent_ready","event":"agent_ready"',
        )
        path.write_text("\n".join(lines) + "\n")
        result = self.evaluate()
        self.assertFalse(result["passed"])
        self.assertIn("duplicate key 'event'", "\n".join(result["failures"]))

    def test_ns3_namespace_ipv4_route_fails_topology_gate(self) -> None:
        path = self.run_dir / "raw/topology/ams-ns3.route.json"
        routes = json.loads(path.read_text())
        routes.append({"dst": "10.71.0.0/16", "dev": "br-gcs"})
        dump(path, routes)
        result = self.evaluate()
        self.assertFalse(result["passed"])
        self.assertIn("routed IP bypass", "\n".join(result["failures"]))

    def test_continuous_topology_gap_fails_even_with_rewritten_summary(self) -> None:
        samples_path = self.run_dir / "raw/topology_monitor/samples.jsonl"
        samples = validator.strict_jsonl(samples_path)
        samples = [
            sample
            for sample in samples
            if not 30_000_000_000 <= sample["monotonic_ns"] <= 32_000_000_000
        ]
        for sequence, sample in enumerate(samples, start=1):
            sample["sample_sequence"] = sequence
        dump_jsonl(samples_path, samples)
        summary_path = self.run_dir / "raw/topology_monitor/summary.json"
        summary = json.loads(summary_path.read_text())
        times = [int(sample["monotonic_ns"]) for sample in samples]
        summary["sample_count"] = len(samples)
        summary["first_sample_monotonic_ns"] = times[0]
        summary["last_sample_monotonic_ns"] = times[-1]
        summary["maximum_sample_gap_ns"] = max(
            right - left for left, right in zip(times, times[1:])
        )
        dump(summary_path, summary)
        result = self.evaluate()
        self.assertFalse(result["passed"])
        self.assertIn(
            "summary/cadence does not match raw samples",
            "\n".join(result["gates"]["continuous_topology"]["failures"]),
        )

    def test_continuous_nft_rule_fails_firewall_gate(self) -> None:
        path = self.run_dir / "raw/topology_monitor/samples.jsonl"
        samples = validator.strict_jsonl(path)
        sample = next(
            item for item in samples if item["monotonic_ns"] == 30_000_000_000
        )
        sample["namespaces"]["ams-gcs"]["nftables"]["nftables"].append(
            {"rule": {"family": "ip", "table": "nat", "chain": "OUTPUT"}}
        )
        dump_jsonl(path, samples)
        result = self.evaluate()
        self.assertFalse(result["passed"])
        self.assertIn(
            "nftables ruleset is not empty",
            "\n".join(result["gates"]["continuous_topology"]["failures"]),
        )

    def test_continuous_table_all_local_routes_are_exact(self) -> None:
        path = self.run_dir / "raw/topology_monitor/samples.jsonl"
        samples = validator.strict_jsonl(path)
        sample = next(
            item for item in samples if item["monotonic_ns"] == 30_000_000_000
        )
        routes = sample["namespaces"]["ams-uav2"]["routes_ipv4"]
        self.assertTrue(any(route.get("table") == "local" for route in routes))
        routes.append(
            {
                "type": "local",
                "dst": "192.0.2.44",
                "dev": "eth0",
                "table": "local",
            }
        )
        dump_jsonl(path, samples)
        result = self.evaluate()
        self.assertFalse(result["passed"])
        self.assertIn(
            "endpoint local IPv4 route set is not exact",
            "\n".join(result["gates"]["continuous_topology"]["failures"]),
        )

    def test_continuous_undeclared_route_table_fails_closed(self) -> None:
        path = self.run_dir / "raw/topology_monitor/samples.jsonl"
        samples = validator.strict_jsonl(path)
        sample = next(
            item for item in samples if item["monotonic_ns"] == 30_000_000_000
        )
        sample["namespaces"]["ams-gcs"]["routes_ipv4"].append(
            {"dst": "192.0.2.0/24", "dev": "eth0", "table": 100}
        )
        dump_jsonl(path, samples)
        result = self.evaluate()
        self.assertFalse(result["passed"])
        self.assertIn(
            "has a route in an undeclared IPv4 table",
            "\n".join(result["gates"]["continuous_topology"]["failures"]),
        )

    def test_continuous_bridge_self_rows_are_ignored_but_extra_port_fails(
        self,
    ) -> None:
        path = self.run_dir / "raw/topology_monitor/samples.jsonl"
        samples = validator.strict_jsonl(path)
        sample = next(
            item for item in samples if item["monotonic_ns"] == 30_000_000_000
        )
        bridge_links = sample["namespaces"]["ams-ns3"]["bridge_links"]
        self.assertTrue(
            any(item.get("ifname") == item.get("master") for item in bridge_links)
        )
        bridge_links.append({"ifname": "rogue0", "master": "br-gcs"})
        dump_jsonl(path, samples)
        result = self.evaluate()
        self.assertFalse(result["passed"])
        self.assertIn(
            "ns3 bridge membership is not exact",
            "\n".join(result["gates"]["continuous_topology"]["failures"]),
        )

    def test_expected_incomplete_gateway_neighbour_is_accepted(self) -> None:
        path = self.run_dir / "raw/topology_monitor/samples.jsonl"
        samples = validator.strict_jsonl(path)
        sample = next(
            item for item in samples if item["monotonic_ns"] == 30_000_000_000
        )
        sample["namespaces"]["ams-uav2"]["neighbours_ipv4"].append(
            {"dst": "10.71.2.1", "dev": "eth0", "state": ["INCOMPLETE"]}
        )
        dump_jsonl(path, samples)
        result = self.evaluate()
        self.assertTrue(result["passed"], "\n".join(result["failures"][:20]))

    def test_expected_failed_gateway_neighbour_is_accepted(self) -> None:
        path = self.run_dir / "raw/topology_monitor/samples.jsonl"
        samples = validator.strict_jsonl(path)
        sample = next(
            item for item in samples if item["monotonic_ns"] == 30_000_000_000
        )
        sample["namespaces"]["ams-uav2"]["neighbours_ipv4"].append(
            {"dst": "10.71.2.1", "dev": "eth0", "state": ["FAILED"]}
        )
        dump_jsonl(path, samples)
        result = self.evaluate()
        self.assertTrue(result["passed"], "\n".join(result["failures"][:20]))

    def test_incomplete_unknown_gateway_neighbour_fails_closed(self) -> None:
        path = self.run_dir / "raw/topology_monitor/samples.jsonl"
        samples = validator.strict_jsonl(path)
        sample = next(
            item for item in samples if item["monotonic_ns"] == 30_000_000_000
        )
        sample["namespaces"]["ams-uav2"]["neighbours_ipv4"].append(
            {"dst": "10.71.2.99", "dev": "eth0", "state": ["INCOMPLETE"]}
        )
        dump_jsonl(path, samples)
        result = self.evaluate()
        self.assertFalse(result["passed"])
        self.assertIn(
            "endpoint has an undeclared IPv4 neighbour",
            "\n".join(result["gates"]["continuous_topology"]["failures"]),
        )

    def test_failed_unknown_gateway_neighbour_fails_closed(self) -> None:
        path = self.run_dir / "raw/topology_monitor/samples.jsonl"
        samples = validator.strict_jsonl(path)
        sample = next(
            item for item in samples if item["monotonic_ns"] == 30_000_000_000
        )
        sample["namespaces"]["ams-uav2"]["neighbours_ipv4"].append(
            {"dst": "10.71.2.99", "dev": "eth0", "state": ["FAILED"]}
        )
        dump_jsonl(path, samples)
        result = self.evaluate()
        self.assertFalse(result["passed"])
        self.assertIn(
            "endpoint has an undeclared IPv4 neighbour",
            "\n".join(result["gates"]["continuous_topology"]["failures"]),
        )

    def test_gcs_cannot_claim_an_incomplete_tail_gateway_neighbour(self) -> None:
        path = self.run_dir / "raw/topology_monitor/samples.jsonl"
        samples = validator.strict_jsonl(path)
        sample = next(
            item for item in samples if item["monotonic_ns"] == 30_000_000_000
        )
        sample["namespaces"]["ams-gcs"]["neighbours_ipv4"].append(
            {"dst": "10.72.0.1", "dev": "tail0", "state": ["INCOMPLETE"]}
        )
        dump_jsonl(path, samples)
        result = self.evaluate()
        self.assertFalse(result["passed"])
        self.assertIn(
            "endpoint has an undeclared IPv4 neighbour",
            "\n".join(result["gates"]["continuous_topology"]["failures"]),
        )

    def test_bounded_capture_stop_transition_tolerates_partial_inventory(
        self,
    ) -> None:
        path = self.run_dir / "raw/topology_monitor/samples.jsonl"
        samples = validator.strict_jsonl(path)
        sample = next(
            item
            for item in samples
            if item.get("transition_event") == "captures_stop_requested"
        )
        sample["processes"] = [
            process
            for process in sample["processes"]
            if "raw_packet_capture.py"
            not in " ".join(str(token) for token in process.get("cmdline", []))
        ]
        dump_jsonl(path, samples)
        result = self.evaluate()
        self.assertTrue(result["passed"], "\n".join(result["failures"][:20]))

    def test_capture_transition_rejects_more_than_declared_processes(self) -> None:
        path = self.run_dir / "raw/topology_monitor/samples.jsonl"
        samples = validator.strict_jsonl(path)
        sample = next(
            item
            for item in samples
            if item.get("transition_event") == "captures_stop_requested"
        )
        duplicate = next(
            process
            for process in sample["processes"]
            if process["namespace"] == "ams-gcs"
            and "raw_packet_capture.py"
            in " ".join(str(token) for token in process.get("cmdline", []))
        )
        sample["processes"].append(dict(duplicate))
        dump_jsonl(path, samples)
        result = self.evaluate()
        self.assertFalse(result["passed"])
        self.assertIn(
            "/ams-gcs capture count is 3, expected 0",
            "\n".join(result["gates"]["continuous_topology"]["failures"]),
        )

    def test_continuous_agent_pid_restart_fails_identity_gate(self) -> None:
        path = self.run_dir / "raw/topology_monitor/samples.jsonl"
        samples = validator.strict_jsonl(path)
        sample = next(
            item for item in samples if item["monotonic_ns"] == 30_000_000_000
        )
        agent = next(
            process
            for process in sample["processes"]
            if process["namespace"] == "ams-uav2"
            and "m3_external_matrix_probe.py" in " ".join(process["cmdline"])
        )
        original_agent_identity = (agent["pid"], agent["start_ticks"])
        agent["pid"] = 44_444
        agent["start_ticks"] = 55_555
        dump_jsonl(path, samples)
        result = self.evaluate()
        self.assertFalse(result["passed"])
        self.assertIn(
            "endpoint agent PID/start_ticks changed: ams-uav2",
            "\n".join(result["gates"]["continuous_topology"]["failures"]),
        )

        agent["pid"], agent["start_ticks"] = original_agent_identity
        supervisor = next(
            process
            for process in sample["processes"]
            if any(
                str(token).endswith("actual_sitl_endpoint_orchestrator.py")
                for token in process.get("cmdline", [])
            )
        )
        supervisor["start_ticks"] += 1
        dump_jsonl(path, samples)
        result = self.evaluate()
        self.assertFalse(result["passed"])
        self.assertIn(
            "critical actual process vanished/restarted",
            "\n".join(result["gates"]["actual_sitl_control"]["failures"]),
        )

    def test_continuous_tail_capture_pid_restart_fails_identity_gate(self) -> None:
        path = self.run_dir / "raw/topology_monitor/samples.jsonl"
        samples = validator.strict_jsonl(path)
        sample = next(
            item for item in samples if item["monotonic_ns"] == 30_000_000_000
        )
        capture = next(
            process
            for process in sample["processes"]
            if process["namespace"] == "ams-uav3"
            and "raw_packet_capture.py" in " ".join(process["cmdline"])
            and "tail0" in process["cmdline"]
        )
        capture["pid"] = 48_001
        capture["start_ticks"] = 48_001_00
        dump_jsonl(path, samples)
        result = self.evaluate()
        self.assertFalse(result["passed"])
        self.assertIn(
            "actual tail capture PID/start_ticks changed: ams-uav3",
            "\n".join(result["gates"]["continuous_topology"]["failures"]),
        )

    def test_missing_root_tail_capture_process_fails_continuous_gate(self) -> None:
        path = self.run_dir / "raw/topology_monitor/samples.jsonl"
        samples = validator.strict_jsonl(path)
        sample = next(
            item for item in samples if item["monotonic_ns"] == 30_000_000_000
        )
        sample["processes"] = [
            process
            for process in sample["processes"]
            if not (
                process["namespace"] == "container-root"
                and "raw_packet_capture.py" in " ".join(process["cmdline"])
                and "ams-tail4" in process["cmdline"]
            )
        ]
        dump_jsonl(path, samples)
        result = self.evaluate()
        self.assertFalse(result["passed"])
        self.assertIn(
            "container-root capture count is 5, expected 6",
            "\n".join(result["gates"]["continuous_topology"]["failures"]),
        )

    def test_extra_root_nonloopback_ipv4_fails_topology_gate(self) -> None:
        path = self.run_dir / "raw/topology/container-root.addr.json"
        addresses = json.loads(path.read_text())
        tail = next(item for item in addresses if item["ifname"] == "ams-tail1")
        tail["addr_info"].append(
            {"family": "inet", "local": "192.0.2.1", "prefixlen": 24}
        )
        dump(path, addresses)
        result = self.evaluate()
        self.assertFalse(result["passed"])
        self.assertIn(
            "container-root tail address set differs",
            "\n".join(result["gates"]["topology_isolation"]["failures"]),
        )

    def test_continuous_netlink_monitor_exit_fails_closed(self) -> None:
        path = self.run_dir / "raw/topology_monitor/samples.jsonl"
        samples = validator.strict_jsonl(path)
        sample = next(
            item for item in samples if item["monotonic_ns"] == 30_000_000_000
        )
        sample["netlink_monitors"]["ams-ns3"]["alive"] = False
        dump_jsonl(path, samples)
        result = self.evaluate()
        self.assertFalse(result["passed"])
        self.assertIn(
            "continuous netlink monitor identity invalid: ams-ns3",
            "\n".join(result["gates"]["continuous_topology"]["failures"]),
        )

    def test_no_write_requires_byte_exact_independent_equality(self) -> None:
        output = self.run_dir / validator.DEFAULT_OUTPUT
        result = self.evaluate()
        self.assertTrue(result["passed"], "\n".join(result["failures"][:10]))
        dump(output, result)
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(
                validator.main(
                    [
                        "--run-dir",
                        str(self.run_dir),
                        "--m2-receipt",
                        str(self.fixture.m2_receipt),
                        "--no-write",
                    ]
                ),
                0,
            )
        document = json.loads(output.read_text())
        document["passed"] = False
        output.write_text(
            json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
        )
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(
                validator.main(
                    [
                        "--run-dir",
                        str(self.run_dir),
                        "--m2-receipt",
                        str(self.fixture.m2_receipt),
                        "--no-write",
                    ]
                ),
                2,
            )


class M3ExternalMatrixStaticTests(unittest.TestCase):
    def test_codec_covers_every_resolved_matrix_cell_and_p2mp(self) -> None:
        matrix = json.loads(MATRIX_PATH.read_text())
        observed = set()
        for cell in matrix["cells"]:
            payload, _metadata = producer.encode_transport_unit(
                run_nonce=RUN_NONCE,
                phase="positive",
                cell=cell,
                sequence=1,
                sent_monotonic_ns=123456789,
                mavlink=producer.MavlinkSequencer(),
            )
            decoded = producer.decode_transport_unit(payload)
            observed.add(decoded["flow_id"])
        self.assertEqual(observed, {cell["cell_id"] for cell in matrix["cells"]})
        root, _metadata = producer.encode_transport_unit(
            run_nonce=RUN_NONCE,
            phase="p2mp",
            cell=None,
            sequence=1,
            sent_monotonic_ns=123456789,
            mavlink=producer.MavlinkSequencer(),
            p2mp=True,
        )
        self.assertTrue(producer.decode_transport_unit(root)["p2mp"])

    def test_endpoint_agent_pump_is_bounded_and_round_robin(self) -> None:
        class BusySocket:
            def __init__(self, descriptor: int) -> None:
                self.descriptor = descriptor
                self.receive_count = 0

            def fileno(self) -> int:
                return self.descriptor

            def getsockname(self) -> tuple[str, int]:
                return ("10.71.0.10", 14000 + self.descriptor)

            def recvmsg(
                self, _payload_size: int, _ancillary_size: int
            ) -> tuple[bytes, list[tuple[int, int, bytes]], int, tuple[str, int]]:
                self.receive_count += 1
                return (b"invalid", [], 0, ("10.71.1.10", 15000))

        sockets = [BusySocket(index) for index in (1, 2, 3)]
        agent = producer.EndpointAgent.__new__(producer.EndpointAgent)
        agent.namespace = "ams-gcs"
        agent.ip = "10.71.0.10"
        agent.sockets = {"payload": sockets[0], "additional_data": sockets[1]}
        agent.p2mp_socket = sockets[2]
        agent.socket_class = {
            sock.fileno(): "additional_data" for sock in sockets
        }
        agent.transport_run_nonce = RUN_NONCE
        agent.writer = mock.Mock()
        agent._pump_socket_cursor = 0

        with mock.patch.object(
            producer.select, "select", return_value=(sockets, [], [])
        ):
            agent.pump(0.01)
            self.assertEqual(
                [sock.receive_count for sock in sockets], [22, 21, 21]
            )
            agent.pump(0.01)

        self.assertEqual(
            [sock.receive_count for sock in sockets], [43, 43, 42]
        )
        self.assertEqual(
            agent.writer.emit.call_count,
            2 * producer.ENDPOINT_PUMP_DATAGRAM_LIMIT,
        )

    def test_hot_path_evidence_writers_sync_only_on_close(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "endpoint.jsonl"
            with mock.patch.object(producer.os, "fsync") as final_sync:
                writer = producer.EventWriter(
                    path,
                    schema=producer.ENDPOINT_EVENT_SCHEMA,
                    run_id=RUN_ID,
                    runtime_id=RUNTIME_ID,
                    run_nonce=RUN_NONCE,
                    endpoint="gcs",
                )
                writer.emit("one")
                writer.emit("two")
                final_sync.assert_not_called()
                writer.close()
                final_sync.assert_called_once()
            self.assertEqual(
                [json.loads(line)["event_sequence"] for line in path.read_text().splitlines()],
                [1, 2],
            )

        monitor = topology_monitor.TopologyMonitor.__new__(
            topology_monitor.TopologyMonitor
        )
        monitor.args = argparse.Namespace(
            run_id=RUN_ID,
            runtime_id=RUNTIME_ID,
            run_nonce=RUN_NONCE,
            run_dir=Path("/unused"),
            interval_ms=500,
        )
        monitor.commands = {}
        monitor.base = Path("/unused/raw/topology_monitor")
        monitor.samples = mock.Mock()
        monitor.samples.fileno.return_value = 321
        monitor.hasher = mock.Mock()
        monitor.sample_sequence = 0
        monitor.sample_times = []
        monitor.transition_events = []
        monitor.netlink_processes = {}
        monitor.netlink_handles = {}
        monitor.ensure_netlink_monitors = mock.Mock()
        with (
            mock.patch.object(
                topology_monitor,
                "collect_namespace",
                side_effect=lambda name, _commands: {"name": name},
            ),
            mock.patch.object(topology_monitor, "collect_processes", return_value=[]),
            mock.patch.object(topology_monitor.os, "fsync") as final_sync,
        ):
            monitor.sample(reason="periodic")
            final_sync.assert_not_called()
            with mock.patch.object(topology_monitor, "write_exclusive"):
                monitor.close()
            final_sync.assert_called_once_with(321)
        monitor.samples.flush.assert_called()
        monitor.samples.close.assert_called_once()

    def test_netlink_monitors_start_in_independent_process_groups(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            monitor = topology_monitor.TopologyMonitor.__new__(
                topology_monitor.TopologyMonitor
            )
            monitor.commands = {"ip": "/usr/bin/ip"}
            monitor.netlink = Path(temporary)
            monitor.netlink_processes = {}
            monitor.netlink_handles = {}
            processes = [
                mock.Mock(pid=20_000 + index)
                for index, _namespace in enumerate(topology_monitor.NAMESPACE_NAMES)
            ]
            with (
                mock.patch.object(
                    topology_monitor, "namespace_inode", return_value=12345
                ),
                mock.patch.object(
                    topology_monitor.subprocess,
                    "Popen",
                    side_effect=processes,
                ) as popen,
            ):
                monitor.ensure_netlink_monitors()

            self.assertEqual(popen.call_count, len(topology_monitor.NAMESPACE_NAMES))
            self.assertTrue(
                all(
                    call.kwargs.get("start_new_session") is True
                    for call in popen.call_args_list
                )
            )
            for stdout, stderr in monitor.netlink_handles.values():
                stdout.close()
                stderr.close()

    def test_netlink_process_groups_are_signalled_before_any_wait(self) -> None:
        class FakeProcess:
            def __init__(
                self,
                pid: int,
                events: list[tuple[str, int, object]],
                wait_barrier: threading.Barrier,
            ) -> None:
                self.pid = pid
                self.returncode: int | None = None
                self.events = events
                self.wait_barrier = wait_barrier

            def poll(self) -> int | None:
                return self.returncode

            def wait(self, timeout: float) -> int:
                self.events.append(("wait", self.pid, timeout))
                self.wait_barrier.wait(timeout=0.5)
                if self.returncode is None:
                    raise AssertionError("wait occurred before group termination")
                return self.returncode

        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            base = run_dir / "raw/topology_monitor"
            netlink = base / "netlink"
            netlink.mkdir(parents=True)
            events: list[tuple[str, int, object]] = []
            wait_barrier = threading.Barrier(2)
            processes = {
                namespace: FakeProcess(21_000 + index, events, wait_barrier)
                for index, namespace in enumerate(("container-root", "ams-ns3"))
            }
            monitor = topology_monitor.TopologyMonitor.__new__(
                topology_monitor.TopologyMonitor
            )
            monitor.args = argparse.Namespace(
                run_id=RUN_ID,
                runtime_id=RUNTIME_ID,
                run_nonce=RUN_NONCE,
                run_dir=run_dir,
                interval_ms=500,
            )
            monitor.base = base
            monitor.netlink = netlink
            monitor.samples = (base / "samples.jsonl").open("x", encoding="utf-8")
            monitor.sample_times = [1_000_000_000, 1_500_000_000]
            monitor.sample_sequence = 2
            monitor.transition_events = []
            monitor.commands = {"ip": "/usr/bin/ip"}
            monitor.netlink_processes = processes
            monitor.netlink_handles = {
                namespace: (
                    (netlink / f"{namespace}.jsonl.txt").open("xb"),
                    (netlink / f"{namespace}.stderr").open("xb"),
                )
                for namespace in processes
            }

            by_pid = {process.pid: process for process in processes.values()}

            def kill_group(pid: int, sig: object) -> None:
                events.append(("signal", pid, sig))
                if sig == topology_monitor.signal.SIGINT:
                    by_pid[pid].returncode = -int(topology_monitor.signal.SIGINT)

            with mock.patch.object(
                topology_monitor.os, "killpg", side_effect=kill_group
            ):
                monitor.close()

            signal_events = [event for event in events if event[0] == "signal"]
            wait_events = [event for event in events if event[0] == "wait"]
            self.assertEqual(
                signal_events,
                [
                    ("signal", process.pid, topology_monitor.signal.SIGINT)
                    for process in processes.values()
                ],
            )
            self.assertEqual(len(wait_events), len(processes))
            self.assertLess(
                max(events.index(event) for event in signal_events),
                min(events.index(event) for event in wait_events),
            )

    def test_runner_is_root_wrapper_native_and_uses_independent_equality(self) -> None:
        runner = (ROOT / "network/scripts/run_m3_external_matrix.sh").read_text()
        self.assertIn("umask 0002", runner)
        self.assertNotIn("sudo", runner)
        self.assertIn(
            'MAVPROXY_SCRIPT="/home/ubuntu/.local/bin/mavproxy.py"', runner
        )
        self.assertIn(
            '[[ ! -f "$MAVPROXY_SCRIPT" || ! -x "$MAVPROXY_SCRIPT" ]]', runner
        )
        self.assertIn('MAVLINK_PYTHON="/usr/bin/python3.10"', runner)
        self.assertIn(
            'MAVLINK_PYTHON_SITE="/home/ubuntu/.local/lib/python3.10/site-packages"',
            runner,
        )
        self.assertIn("controlled M3 Python/pymavlink runtime is unavailable", runner)
        self.assertNotIn("${2:-{}}", runner)
        self.assertIn("details_json='{}'", runner)
        self.assertLess(
            runner.index("link set eth0 up"),
            runner.index("route add default via"),
        )
        self.assertIn('neigh replace "10.71.$index.1"', runner)
        self.assertIn(
            'lladdr "02:71:$(printf \'%02x\' "$index"):00:00:01" '
            "nud permanent dev eth0",
            runner,
        )
        self.assertIn('chown -R 1000:1000 "$RUN_DIR"', runner)
        self.assertNotIn(" tcpdump ", runner)
        self.assertIn('python3 -u "$CAPTURE_TOOL"', runner)
        m2_runner = (ROOT / "network/scripts/run_one_uav_vertical_slice.sh").read_text()
        for source in (m2_runner, runner):
            self.assertIn("ams-tap-packet-engine", source)
            self.assertIn('"$NS3_RUNNER"', source)
            self.assertIn("tap_packet_engine_config.py", source)
            self.assertIn("SIONNA_IPC_ENABLED=0", source)
        self.assertIn("UAV_COUNT=5", runner)
        self.assertIn("UAV_COUNT=1", m2_runner)
        self.assertIn("generate_sensor_models:=false", runner)
        self.assertIn("generate_sensor_models:=false", m2_runner)
        self.assertIn("--no-write", runner)
        self.assertIn('RESULT_PATH="$RUN_DIR/metrics/m3_validation_results.json"', runner)
        self.assertIn('cmp "$RESULT_PATH" "$INDEPENDENT_RESULT"', runner)
        self.assertIn(
            'local engine_lifecycle="$RUN_DIR/logs/ns3_epoch${epoch}.lifecycle.jsonl"',
            runner,
        )
        self.assertIn('LIFECYCLE_FILE="$engine_lifecycle"', runner)
        self.assertIn('lifecycle engine_final_stop_requested', runner)
        for event in (
            "captures_start_requested",
            "forbidden_listeners_start_requested",
            "endpoint_agents_start_requested",
            "actual_control_start_requested",
            "actual_control_stop_requested",
            "endpoint_agents_stop_requested",
            "forbidden_listeners_stop_requested",
            "actual_sitl_stack_stop_requested",
            "captures_stop_requested",
        ):
            self.assertIn(f"lifecycle {event} ", runner)
        self.assertIn("phase_window_field()", runner)
        probe_source = (
            ROOT / "network/scripts/m3_external_matrix_probe.py"
        ).read_text()
        self.assertIn('"offered_per_cell": 20', probe_source)
        self.assertIn('"p2mp_roots": 20', probe_source)
        self.assertIn('"end_monotonic_ns": base + 65_000_000_000', probe_source)
        self.assertIn(
            '"end_monotonic_ns": restart_request + 75_500_000_000',
            probe_source,
        )
        launch_source = (
            ROOT / "src/multiagent_simulation/launch/multiagent_simulation.launch.py"
        ).read_text()
        self.assertIn('"mavproxy_streamrate"', launch_source)
        self.assertIn('mavproxy_cmd.extend(["--streamrate", mavproxy_streamrate])', launch_source)
        self.assertIn("mavproxy_streamrate:=1", runner)

    def test_validator_has_an_independent_decoder_not_a_producer_import(self) -> None:
        source = (
            ROOT / "network/validation/validate_m3_external_matrix.py"
        ).read_text()
        self.assertNotIn("from network.scripts import m3_external_matrix_probe", source)
        self.assertNotIn("import network.scripts.m3_external_matrix_probe", source)
        self.assertIn("def decode_transport(", source)
        self.assertIn("producer result differs byte-for-byte", source)

    def test_cli_contract_and_result_path_match_component_profile(self) -> None:
        profiles = json.loads(
            (ROOT / "network/config/component_acceptance_profiles.json").read_text()
        )
        profile = profiles["profiles"]["m3_component"]
        self.assertEqual(
            profile["validator"], "network/scripts/validate_m3_external_matrix.py"
        )
        self.assertEqual(profile["result_contract"], validator.RESULT_CONTRACT)
        self.assertEqual(profile["result_path"], validator.DEFAULT_OUTPUT.as_posix())


if __name__ == "__main__":
    unittest.main()
