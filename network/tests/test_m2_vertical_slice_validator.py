#!/usr/bin/env python3
"""Positive and adversarial fixtures for the fail-closed M2 validator."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import struct
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from network.validation.validate_m2_vertical_slice import (  # noqa: E402
    CAPTURE_POINTS,
    ENGINE_CONTRACT,
    ENGINE_EVENT_SCHEMA,
    ENGINE_PHASES,
    ENGINE_PROGRAM,
    EVIDENCE_CONTRACT,
    MANIFEST_CONTRACT,
    RESULT_CONTRACT,
    derive_endpoint_subset_contract,
    evaluate_m2_vertical_slice,
    main,
)
from network.ns3.tap_packet_engine_config import from_repository  # noqa: E402
from network.ns3.ns3_build_receipt import (  # noqa: E402
    EXPECTED_CORE_TREE_FILES,
    EXPECTED_CORE_TREE_SHA256,
    subject_digest,
)


RUN_ID = "m2_fixture"
RUNTIME_ID = "m2-runtime-fixture"
RUN_NONCE = "m2nonce0123456789abcdef"
SOURCE_HASH = "c" * 64
UTC_BASE = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def utc_for(sequence: int) -> str:
    return (UTC_BASE + timedelta(microseconds=sequence)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def udp_frame(payload: bytes, *, identification: int) -> bytes:
    ethernet = (
        b"\x02\x71\x01\x00\x10\x10"
        + b"\x02\x71\x00\x00\x10\x10"
        + struct.pack("!H", 0x0800)
    )
    total_length = 20 + 8 + len(payload)
    ipv4 = struct.pack(
        "!BBHHHBBH4s4s",
        0x45,
        0,
        total_length,
        identification,
        0,
        64,
        17,
        0,
        b"\x0a\x47\x00\x0a",
        b"\x0a\x47\x01\x0a",
    )
    udp = struct.pack("!HHHH", 14600, 14601, 8 + len(payload), 0)
    return ethernet + ipv4 + udp + payload


def write_pcap(path: Path, payloads: list[bytes], *, timestamp_seed: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.write(struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
        for index, payload in enumerate(payloads, start=1):
            frame = udp_frame(payload, identification=(timestamp_seed + index) % 65535)
            handle.write(
                struct.pack(
                    "<IIII",
                    1_700_000_000 + timestamp_seed,
                    index,
                    len(frame),
                    len(frame),
                )
            )
            handle.write(frame)


def pcap_packet_count(path: Path) -> int:
    payload = path.read_bytes()
    offset = 24
    count = 0
    while offset < len(payload):
        captured = struct.unpack("<I", payload[offset + 8 : offset + 12])[0]
        offset += 16 + captured
        count += 1
    if offset != len(payload):
        raise AssertionError(f"fixture PCAP is truncated: {path}")
    return count


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def resequence(records: list[dict[str, Any]]) -> None:
    for sequence, record in enumerate(records, start=1):
        record["event_seq"] = sequence


class FixtureBuilder:
    def __init__(self, run_dir: Path, *, down_ack: bool = False) -> None:
        self.run_dir = run_dir
        self.down_ack = down_ack
        self.probe_records: list[dict[str, Any]] = []
        self.adapter_records: list[dict[str, Any]] = []
        self.process_records: list[dict[str, Any]] = []
        self.phase_payloads: dict[str, dict[str, list[bytes]]] = {}
        self.probe_ns = 1_000_000_000
        self.adapter_sequence = 0
        self.process_sequence = 0

    def _probe(self, phase: str, event: str, **fields: Any) -> dict[str, Any]:
        self.probe_ns += 1_000_000
        sequence = len(self.probe_records) + 1
        record = {
            "schema_version": 2,
            "run_id": RUN_ID,
            "runtime_id": RUNTIME_ID,
            "run_nonce": RUN_NONCE,
            "phase": phase,
            "event_seq": sequence,
            "event": event,
            "monotonic_ns": self.probe_ns,
            "wall_utc": utc_for(sequence),
            **fields,
        }
        self.probe_records.append(record)
        return record

    def _adapter(self, event: str, monotonic_ns: int, **fields: Any) -> dict[str, Any]:
        self.adapter_sequence += 1
        record = {
            "schema_version": 2,
            "run_id": RUN_ID,
            "runtime_id": RUNTIME_ID,
            "run_nonce": RUN_NONCE,
            "event_seq": self.adapter_sequence,
            "event": event,
            "monotonic_ns": monotonic_ns,
            "wall_utc": utc_for(1_000 + self.adapter_sequence),
            **fields,
        }
        self.adapter_records.append(record)
        return record

    def _process(self, phase: str, role: str, *, alive: bool, pid: int, ticks: int, command: str) -> None:
        self.process_sequence += 1
        self.process_records.append(
            {
                "schema_version": 2,
                "run_id": RUN_ID,
                "runtime_id": RUNTIME_ID,
                "run_nonce": RUN_NONCE,
                "phase": phase,
                "event_seq": self.process_sequence,
                "event": "process_snapshot",
                "monotonic_ns": self.probe_ns + self.process_sequence * 1_000_000,
                "wall_utc": utc_for(2_000 + self.process_sequence),
                "role": role,
                "alive": alive,
                "pid": pid,
                "start_ticks": ticks,
                "cmdline_sha256": digest(command.encode()),
            }
        )

    def _phase(self, phase: str, attempts: int, successful: bool) -> None:
        payloads = {"requests": [], "responses": [], "heartbeats": []}
        self.phase_payloads[phase] = payloads
        for endpoint in (("127.0.0.1", 5760), ("10.72.1.1", 5760)):
            self._probe(
                phase,
                "direct_endpoint_probe",
                endpoint=list(endpoint),
                reachable=False,
                error="connection refused",
            )
        phase_start = self._probe(
            phase,
            "phase_start",
            attempts=attempts,
            expected_ack=successful,
            target_system=1,
        )
        if successful:
            heartbeat_payload = f"heartbeat:{phase}".encode()
            payloads["heartbeats"].append(heartbeat_payload)
            self._probe(
                phase,
                "heartbeat",
                attempt=None,
                nonce=None,
                packet_sha256=digest(heartbeat_payload),
                source_system=1,
                source_component=1,
                message_type="HEARTBEAT",
            )

        attempt_records: list[tuple[dict[str, Any], bytes, bytes, bytes | None, bytes | None]] = []
        for attempt in range(1, attempts + 1):
            nonce = f"{RUN_NONCE}:{phase}:{attempt}"
            marker = f"MAVLINK2-STATUSTEXT:AMS-M2:{nonce}".encode()
            command = f"MAVLINK2-COMMAND_LONG:{phase}:{attempt}".encode()
            ack = f"MAVLINK2-COMMAND_ACK:{phase}:{attempt}".encode() if successful else None
            telemetry = f"MAVLINK2-AUTOPILOT_VERSION:{phase}:{attempt}".encode() if successful else None
            payloads["requests"].extend((marker, command))
            sequence = (attempt * 2) % 256
            attempt_record = self._probe(
                phase,
                "command_attempt",
                attempt=attempt,
                nonce=nonce,
                marker_sha256=digest(marker),
                command_sha256=digest(command),
                packet_sha256=digest(command),
                mavlink_seq=sequence,
                target_system=1,
                target_component=1,
                mavlink_command=512,
                # Deliberately untrusted producer claims; raw events decide.
                expected_ack=not successful,
            )
            if successful or (self.down_ack and phase == "down" and attempt == 1):
                ack = ack or b"FORGED-DOWN-ACK"
                payloads["responses"].append(ack)
                self._probe(
                    phase,
                    "command_ack",
                    attempt=attempt,
                    nonce=nonce,
                    request_sha256=digest(command),
                    request_mavlink_seq=sequence,
                    packet_sha256=digest(ack),
                    source_system=1,
                    source_component=1,
                    mavlink_command=512,
                    mavlink_result=0,
                )
            if successful:
                assert telemetry is not None
                payloads["responses"].append(telemetry)
                self._probe(
                    phase,
                    "telemetry",
                    attempt=attempt,
                    nonce=nonce,
                    request_sha256=digest(command),
                    request_mavlink_seq=sequence,
                    packet_sha256=digest(telemetry),
                    source_system=1,
                    source_component=1,
                    message_id=148,
                )
            self._probe(
                phase,
                "command_result",
                attempt=attempt,
                nonce=nonce,
                request_sha256=digest(command),
                request_mavlink_seq=sequence,
                passed=False,
                ack=True,  # ignored even in down
                telemetry=True,
                ack_latency_ms=1.0,
            )
            attempt_records.append((attempt_record, marker, command, ack, telemetry))

        if not successful:
            self._probe(phase, "heartbeat_timeout", timed_out=True, timeout_s=5.0)
        phase_end = self._probe(
            phase,
            "phase_end",
            attempts=attempts,
            acknowledgements=attempts if successful else 0,
            telemetry_responses=attempts if successful else 0,
            heartbeat_count=1 if successful else 0,
            heartbeat_timeout=not successful,
        )
        self._probe(
            phase,
            "endpoint_health",
            expected_ns3_state="up" if successful else "down",
            all_live=True,
            endpoint_roles=["gcs_probe", "mavproxy", "sitl", "uav_adapter"],
            ns3_alive=successful,
        )

        # Adapter times use the same host monotonic clock and sit strictly in
        # the probe transaction interval.
        adapter_time = phase_start["monotonic_ns"] + 1
        if successful:
            for heartbeat in payloads["heartbeats"]:
                self._adapter(
                    "forward",
                    adapter_time,
                    direction="tail_to_gcs",
                    bytes=len(heartbeat),
                    sha256=digest(heartbeat),
                )
                adapter_time += 1
            for _record, marker, command, ack, telemetry in attempt_records:
                for request in (marker, command):
                    self._adapter(
                        "forward",
                        adapter_time,
                        direction="gcs_to_tail",
                        bytes=len(request),
                        sha256=digest(request),
                    )
                    adapter_time += 1
                for response in (ack, telemetry):
                    assert response is not None
                    self._adapter(
                        "forward",
                        adapter_time,
                        direction="tail_to_gcs",
                        bytes=len(response),
                        sha256=digest(response),
                    )
                    adapter_time += 1
        self.phase_payloads[phase]["window"] = [
            phase_start["monotonic_ns"],
            phase_end["monotonic_ns"],
        ]

    def build(self) -> None:
        for directory in ("logs", "metrics", "pcap"):
            (self.run_dir / directory).mkdir(parents=True, exist_ok=True)
        endpoint_contract = derive_endpoint_subset_contract()
        (self.run_dir / "metrics/m2_endpoint_contract.json").write_text(
            json.dumps(endpoint_contract, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self._phase("good", 10, True)
        self._phase("down", 5, False)
        self._phase("recovery", 10, True)

        self._adapter(
            "adapter_start",
            self.phase_payloads["good"]["window"][0] - 10,
            pid=200,
        )
        # Move adapter_start to the beginning while preserving the actual
        # monotonic order and contiguous event sequence.
        self.adapter_records.insert(0, self.adapter_records.pop())
        for sequence, record in enumerate(self.adapter_records, start=1):
            record["event_seq"] = sequence
            record["wall_utc"] = utc_for(1_000 + sequence)
        gcs_count = sum(
            1
            for record in self.adapter_records
            if record.get("event") == "forward" and record.get("direction") == "gcs_to_tail"
        )
        tail_count = sum(
            1
            for record in self.adapter_records
            if record.get("event") == "forward" and record.get("direction") == "tail_to_gcs"
        )
        self._adapter(
            "adapter_stop",
            self.phase_payloads["recovery"]["window"][1] + 10,
            counters={
                "gcs_to_tail": gcs_count,
                "tail_to_gcs": tail_count,
                "dropped_no_peer": 0,
                "dropped_unexpected_peer": 0,
            },
        )

        stable = {
            "uav_adapter": (200, 20_000, "adapter"),
            "mavproxy": (300, 30_000, "mavproxy"),
            "sitl": (400, 40_000, "arducopter"),
        }
        for phase_index, phase in enumerate(("good", "down", "recovery")):
            for role, (pid, ticks, command) in stable.items():
                self._process(phase, role, alive=True, pid=pid, ticks=ticks, command=command)
            self._process(
                phase,
                "gcs_probe",
                alive=True,
                pid=500 + phase_index,
                ticks=50_000 + phase_index,
                command=f"probe-{phase}",
            )
            if phase == "good":
                self._process(phase, "ns3", alive=True, pid=600, ticks=60_000, command="ns3-good")
            elif phase == "down":
                self._process(phase, "ns3", alive=False, pid=600, ticks=60_000, command="ns3-good")
            else:
                self._process(
                    phase,
                    "ns3",
                    alive=True,
                    pid=601,
                    ticks=61_000,
                    command="ns3-recovery",
                )

        write_jsonl(self.run_dir / "logs/m2_probe_events.jsonl", self.probe_records)
        write_jsonl(self.run_dir / "logs/uav_adapter.jsonl", self.adapter_records)
        write_jsonl(self.run_dir / "logs/m2_process_events.jsonl", self.process_records)
        (self.run_dir / "logs/m2_runner.log").write_text(
            "2026-07-12T12:00:00Z runtime complete\n", encoding="utf-8"
        )

        timestamp_seed = 1
        for phase in ("good", "recovery"):
            payloads = (
                self.phase_payloads[phase]["requests"]
                + self.phase_payloads[phase]["responses"]
                + self.phase_payloads[phase]["heartbeats"]
            )
            for point in CAPTURE_POINTS:
                write_pcap(
                    self.run_dir / f"pcap/{point}_{phase}.pcap",
                    payloads,
                    timestamp_seed=timestamp_seed,
                )
                timestamp_seed += 1
        write_pcap(
            self.run_dir / "pcap/gcs_ingress_down.pcap",
            self.phase_payloads["down"]["requests"],
            timestamp_seed=timestamp_seed,
        )
        write_pcap(
            self.run_dir / "pcap/ns3_external_ingress_down.pcap",
            self.phase_payloads["down"]["requests"],
            timestamp_seed=timestamp_seed + 1,
        )
        write_pcap(
            self.run_dir / "pcap/uav_egress_down.pcap",
            [],
            timestamp_seed=timestamp_seed + 2,
        )
        tail_payloads = (
            self.phase_payloads["good"]["requests"]
            + self.phase_payloads["good"]["responses"]
            + self.phase_payloads["good"]["heartbeats"]
        )
        write_pcap(
            self.run_dir / "pcap/uav_tail.pcap",
            tail_payloads,
            timestamp_seed=timestamp_seed + 3,
        )
        self.write_capture_stats()
        self.write_provenance()
        self.write_ns3_build_receipt()
        packet_engine = self.write_packet_engine_evidence()
        metadata = {
            "schema_version": 2,
            "contract": EVIDENCE_CONTRACT,
            "run_id": RUN_ID,
            "runtime_id": RUNTIME_ID,
            "run_nonce": RUN_NONCE,
            "source_hash": SOURCE_HASH,
            "started_utc": UTC_BASE.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "component_only": True,
            "p0_eligible": False,
            "passed": True,  # ignored
            "endpoint_transaction": endpoint_contract,
            "packet_engine": packet_engine,
        }
        (self.run_dir / "metrics/m2_run.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.seal()

    def write_capture_stats(self) -> None:
        captures = {
            "tail": ("ams-tail0", "pcap/uav_tail.pcap"),
            "gcs_good": ("eth0", "pcap/gcs_ingress_good.pcap"),
            "ns3_external_good": (
                "v-gcs-ns3",
                "pcap/ns3_external_ingress_good.pcap",
            ),
            "uav_good": ("eth0", "pcap/uav_egress_good.pcap"),
            "gcs_down": ("eth0", "pcap/gcs_ingress_down.pcap"),
            "ns3_external_down": (
                "v-gcs-ns3",
                "pcap/ns3_external_ingress_down.pcap",
            ),
            "uav_down": ("eth0", "pcap/uav_egress_down.pcap"),
            "gcs_recovery": ("eth0", "pcap/gcs_ingress_recovery.pcap"),
            "ns3_external_recovery": (
                "v-gcs-ns3",
                "pcap/ns3_external_ingress_recovery.pcap",
            ),
            "uav_recovery": ("eth0", "pcap/uav_egress_recovery.pcap"),
        }
        for index, (key, (interface, relative)) in enumerate(
            captures.items(), start=1
        ):
            pcap = self.run_dir / relative
            packets = pcap_packet_count(pcap)
            stats = {
                "contract": "ams.raw-packet-capture-stats/v1",
                "interface": interface,
                "pcap_path": pcap.name,
                "pcap_bytes": pcap.stat().st_size,
                "linktype": 1,
                "snaplen": 65_535,
                "started_monotonic_ns": index * 1_000_000,
                "stopped_monotonic_ns": index * 1_000_000 + 500_000,
                "stop_signal": "SIGINT",
                "packets_written": packets,
                "packets_received_kernel": packets,
                "packets_dropped_kernel": 0,
            }
            (self.run_dir / f"logs/capture_{key}_stats.json").write_text(
                json.dumps(stats, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            (self.run_dir / f"logs/capture_{key}.stdout").write_bytes(b"")
            (self.run_dir / f"logs/capture_{key}.stderr").write_bytes(b"")

    def _engine_event_records(
        self, phase: str, epoch: int, config_sha256: str
    ) -> list[dict[str, Any]]:
        downlink = self.phase_payloads[phase]["requests"]
        uplink = (
            self.phase_payloads[phase]["responses"]
            + self.phase_payloads[phase]["heartbeats"]
        )
        records: list[dict[str, Any]] = []
        packet_uid = 0
        for direction, payloads in (("downlink", downlink), ("uplink", uplink)):
            unique_payloads = list(dict.fromkeys(payloads))
            for payload in unique_payloads:
                packet_uid += 1
                link = "cp>uav1" if direction == "downlink" else "uav1>cp"
                source_ip, destination_ip = (
                    ("10.71.0.10", "10.71.1.10")
                    if direction == "downlink"
                    else ("10.71.1.10", "10.71.0.10")
                )
                source_port, destination_port = (
                    (14600, 14601) if direction == "downlink" else (14601, 14600)
                )
                for stage in ("ingress", "enqueue", "dequeue", "channel", "egress"):
                    if stage == "ingress":
                        device = (
                            "cp.tap.ingress"
                            if direction == "downlink"
                            else "uav1.tap.ingress"
                        )
                    elif stage == "egress":
                        device = (
                            "uav1.tap.egress"
                            if direction == "downlink"
                            else "cp.tap.egress"
                        )
                    else:
                        device = (
                            "cp.radio" if direction == "downlink" else "uav1.radio"
                        )
                    queue_depth = 1 if stage == "enqueue" else (0 if stage == "dequeue" else None)
                    queue_limit = 256 if stage in ("enqueue", "dequeue") else None
                    records.append(
                        {
                            "schema": ENGINE_EVENT_SCHEMA,
                            "event_epoch": epoch,
                            "event_sequence": len(records) + 1,
                            "sim_time_ns": (len(records) + 1) * 1_000,
                            "event": stage,
                            "packet_wire_hash_algorithm": "sha256",
                            "packet_wire_hash": digest(b"wire:" + payload),
                            "packet_wire_size": len(payload) + 42,
                            "packet_uid": packet_uid,
                            "tos": 184,
                            "dscp": 46,
                            "traffic_class": "control",
                            "directed_link": link,
                            "queue_id": f"{link}.control.q0",
                            "device_id": device,
                            "source_mac": "02:71:00:00:10:10",
                            "destination_mac": "02:71:01:00:10:10",
                            "source_ip": source_ip,
                            "destination_ip": destination_ip,
                            "transport_protocol": 17,
                            "source_udp_port": source_port,
                            "destination_udp_port": destination_port,
                            "transport_payload_sha256": digest(payload),
                            "transport_payload_size": len(payload),
                            "p2mp": False,
                            "root_transmission": False,
                            "queue_depth_packets": queue_depth,
                            "queue_limit_packets": queue_limit,
                            "drop_reason": None,
                            "config_sha256": config_sha256,
                            "seed": 42,
                            "run": 1,
                        }
                    )
        return records

    def _raw_record(self, relative: str) -> dict[str, Any]:
        path = self.run_dir / relative
        return {
            "path": relative,
            "sha256": digest(path.read_bytes()),
            "size_bytes": path.stat().st_size,
        }

    def write_packet_engine_evidence(self) -> dict[str, Any]:
        phase_identities: dict[str, Any] = {}
        config_hashes: dict[str, str] = {}
        for phase, epoch in ENGINE_PHASES.items():
            config = from_repository(
                uav_count=1,
                duration_ms=3_600_000,
                seed=42,
                run=1,
                event_epoch=epoch,
                self_test=False,
                self_test_burst=1,
                self_test_unknown_tos=False,
                tap_gcs="tap-gcs",
                tap_uavs=("tap-uav",),
            )
            events_relative = f"logs/ns3_{phase}_packet_events.jsonl"
            config_relative = f"logs/ns3_{phase}_config.json"
            argv_relative = f"logs/ns3_{phase}.argv"
            ready_relative = f"logs/ns3_{phase}.ready"
            stop_relative = f"logs/ns3_{phase}.stop"
            engine_argv = config.engine_argv(
                events_file=str(self.run_dir / events_relative),
                pcap_prefix=str(self.run_dir / f"pcap/ns3_{phase}"),
            )
            config_payload = {
                "contract": ENGINE_CONTRACT,
                "config_sha256": config.sha256(),
                "canonical_config": config.canonical_text(),
                "resolved": {**asdict(config), "tap_uavs": list(config.tap_uavs)},
                "engine_argv": engine_argv,
                "source_sha256": {
                    str(ROOT_DIR / "network/config/endpoints.yaml"): digest(
                        (ROOT_DIR / "network/config/endpoints.yaml").read_bytes()
                    ),
                    str(ROOT_DIR / "network/config/radio_24ghz.yaml"): digest(
                        (ROOT_DIR / "network/config/radio_24ghz.yaml").read_bytes()
                    ),
                },
            }
            (self.run_dir / config_relative).write_text(
                json.dumps(config_payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            write_jsonl(
                self.run_dir / events_relative,
                self._engine_event_records(phase, epoch, config.sha256()),
            )
            (self.run_dir / argv_relative).write_text(
                "".join(f"{argument}\n" for argument in engine_argv),
                encoding="utf-8",
            )
            (self.run_dir / ready_relative).write_text(
                json.dumps(
                    {
                        "status": "ready",
                        "contract": ENGINE_CONTRACT,
                        "config_sha256": config.sha256(),
                        "event_epoch": epoch,
                        "uav_count": 1,
                    },
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            (self.run_dir / stop_relative).write_text("stop\n", encoding="utf-8")
            config_hashes[phase] = config.sha256()
            phase_identities[phase] = {
                "event_epoch": epoch,
                "config_sha256": config.sha256(),
                "config": self._raw_record(config_relative),
                "events": self._raw_record(events_relative),
                "argv": self._raw_record(argv_relative),
                "ready": self._raw_record(ready_relative),
                "stop": self._raw_record(stop_relative),
            }
        receipt = json.loads(
            (self.run_dir / "metrics/ns3_tap_build_receipt.json").read_text(
                encoding="utf-8"
            )
        )
        receipt_record = self._raw_record("metrics/ns3_tap_build_receipt.json")
        executable = receipt["subject"]["executable"]
        source_sha256 = digest(
            (ROOT_DIR / "network/ns3/scratch/ams-tap-packet-engine.cc").read_bytes()
        )
        config_tool_sha256 = digest(
            (ROOT_DIR / "network/ns3/tap_packet_engine_config.py").read_bytes()
        )
        runner_sha256 = digest(
            (ROOT_DIR / "network/ns3/run_ns3_tap_packet_engine.sh").read_bytes()
        )
        return {
            "contract": ENGINE_CONTRACT,
            "program": ENGINE_PROGRAM,
            "uav_count": 1,
            "source_sha256": source_sha256,
            "binary_sha256": executable["sha256"],
            "build_receipt_sha256": receipt_record["sha256"],
            "config_contract": ENGINE_CONTRACT,
            "config_sha256": config_hashes,
            "config_tool_sha256": config_tool_sha256,
            "runner_sha256": runner_sha256,
            "event_schema": ENGINE_EVENT_SCHEMA,
            "config_tool": {
                "path": "network/ns3/tap_packet_engine_config.py",
                "sha256": config_tool_sha256,
            },
            "runner": {
                "path": "network/ns3/run_ns3_tap_packet_engine.sh",
                "sha256": runner_sha256,
            },
            "build_receipt": receipt_record,
            "executable": executable,
            "phases": phase_identities,
        }

    def write_provenance(self) -> None:
        source_files = (
            "network/validation/validate_m2_vertical_slice.py",
            "network/validation/endpoint_transaction.py",
            "network/bridge/opaque_udp_relay.py",
            "network/bridge/uav_mavlink_endpoint.py",
            "network/ns3/scratch/ams-tap-packet-engine.cc",
            "network/ns3/tap_packet_engine_config.py",
            "network/scripts/setup_one_uav_netns.sh",
            "network/scripts/raw_packet_capture.py",
            "network/ns3/run_ns3_tap_packet_engine.sh",
            "network/scripts/run_one_uav_vertical_slice.sh",
            "network/tests/mavlink_vertical_slice_probe.py",
            "network/config/endpoint_transaction_schema.json",
            "network/config/endpoint_matrix_5uav.json",
        )
        source_manifest = {
            relative: digest((ROOT_DIR / relative).read_bytes()) for relative in source_files
        }
        config_files = (
            "network/config/scenario_1uav_vertical_slice.yaml",
            "network/config/endpoints.yaml",
            "network/config/radio_24ghz.yaml",
            "network/config/endpoint_transaction_schema.json",
            "network/config/endpoint_matrix_5uav.json",
        )
        provenance = {
            "schema_version": 2,
            "run_id": RUN_ID,
            "source_hash": SOURCE_HASH,
            "git_commit": "a" * 40,
            "git_dirty": False,
            "acceptance_eligible": False,  # not trusted by the M2 component gate
            "container_image": {"reference": "fixture", "digest": "sha256:" + "b" * 64},
            "implementation": {
                "packet_ingress_mode": "tap_bridge_external",
                "medium_model": "csma_surrogate",
                "radio_provider_id": "tcp_jsonl_real_sionna",
                "radio_provider_runtime_consumed": False,
                "runtime_provider_id": "not_applicable_pre_m4",
                "reason": "profile_pre_m4",
            },
            "config_hashes": {
                relative: digest((ROOT_DIR / relative).read_bytes())
                for relative in config_files
            },
            "source_manifest": source_manifest,
        }
        (self.run_dir / "metrics/provenance.json").write_text(
            json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    def write_ns3_build_receipt(self) -> None:
        lock = yaml.safe_load(
            (ROOT_DIR / "network/config/dependency_lock.yaml").read_text(encoding="utf-8")
        )
        modules = sorted(lock["dependencies"]["ns3"]["required_modules"])
        source_hash = digest(
            (ROOT_DIR / "network/ns3/scratch/ams-tap-packet-engine.cc").read_bytes()
        )
        subject = {
            "program": ENGINE_PROGRAM,
            "official_source": {
                "root": "/workspace/multiagent_simulation/.external/ns-3",
                "version": "3.40",
                "expected_version": "3.40",
                "core_tree_files": EXPECTED_CORE_TREE_FILES,
                "core_tree_sha256": EXPECTED_CORE_TREE_SHA256,
                "expected_core_tree_files": EXPECTED_CORE_TREE_FILES,
                "expected_core_tree_sha256": EXPECTED_CORE_TREE_SHA256,
                "excludes": ["build", "cmake-cache", "scratch", "src/lorawan"],
            },
            "scratch_source": {
                "project": {
                    "path": "/workspace/multiagent_simulation/network/ns3/scratch/ams-tap-packet-engine.cc",
                    "sha256": source_hash,
                },
                "copied": {
                    "path": "/workspace/multiagent_simulation/.external/ns-3/scratch/ams-tap-packet-engine.cc",
                    "sha256": source_hash,
                },
                "byte_identical": True,
            },
            "build": {
                "enabled_modules": modules,
                "required_modules": modules,
            },
            "executable": {
                "path": "/workspace/multiagent_simulation/.external/ns-3/build/scratch/ns3.40-ams-tap-packet-engine-default",
                "sha256": "d" * 64,
                "size_bytes": 4096,
                "mode": 0o755,
            },
        }
        receipt = {
            "schema_version": 1,
            "contract": "ams.ns3.build-receipt/v1",
            "created_utc": "2026-07-12T12:00:00Z",
            "subject_sha256": subject_digest(subject),
            "subject": subject,
        }
        path = self.run_dir / "metrics/ns3_tap_build_receipt.json"
        path.write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        path.chmod(0o444)

    def seal(self) -> None:
        manifest_path = self.run_dir / "metrics/m2_evidence_manifest.json"
        manifest_path.unlink(missing_ok=True)
        selected = {
            "metrics/m2_run.json",
            "metrics/m2_endpoint_contract.json",
            "metrics/provenance.json",
            "metrics/ns3_tap_build_receipt.json",
        }
        selected.update(
            candidate.relative_to(self.run_dir).as_posix()
            for root in (self.run_dir / "logs", self.run_dir / "pcap")
            for candidate in root.rglob("*")
            if candidate.is_file()
        )
        files = {}
        for relative in sorted(selected):
            path = self.run_dir / relative
            files[relative] = {"sha256": digest(path.read_bytes()), "size_bytes": path.stat().st_size}
        manifest = {
            "schema_version": 2,
            "contract": MANIFEST_CONTRACT,
            "run_id": RUN_ID,
            "runtime_id": RUNTIME_ID,
            "run_nonce": RUN_NONCE,
            "source_hash": SOURCE_HASH,
            "sealed_utc": "2026-07-12T12:30:00Z",
            "files": files,
        }
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class M2VerticalSliceValidatorTests(unittest.TestCase):
    def make_fixture(self, *, down_ack: bool = False) -> tuple[tempfile.TemporaryDirectory[str], Path, FixtureBuilder]:
        temporary = tempfile.TemporaryDirectory()
        run_dir = Path(temporary.name) / RUN_ID
        builder = FixtureBuilder(run_dir, down_ack=down_ack)
        builder.build()
        return temporary, run_dir, builder

    def test_formal_m2_scripts_use_bounded_root_without_sudo(self) -> None:
        runner = (ROOT_DIR / "network/scripts/run_one_uav_vertical_slice.sh").read_text(
            encoding="utf-8"
        )
        setup = (ROOT_DIR / "network/scripts/setup_one_uav_netns.sh").read_text(
            encoding="utf-8"
        )
        ns3_runner = (
            ROOT_DIR / "network/ns3/run_ns3_tap_packet_engine.sh"
        ).read_text(
            encoding="utf-8"
        )
        raw_capture = (
            ROOT_DIR / "network/scripts/raw_packet_capture.py"
        ).read_text(encoding="utf-8")
        for text in (runner, setup, ns3_runner, raw_capture):
            executable_tokens = " ".join(
                line for line in text.splitlines() if not line.lstrip().startswith("#")
            ).split()
            self.assertNotIn("sudo", executable_tokens)
        for text in (runner, setup, ns3_runner):
            executable_tokens = " ".join(
                line for line in text.splitlines() if not line.lstrip().startswith("#")
            ).split()
            self.assertNotIn("tcpdump", executable_tokens)
        self.assertIn('NS3_PROGRAM="ams-tap-packet-engine"', runner)
        self.assertIn(
            'MAVPROXY_SCRIPT="/home/ubuntu/.local/bin/mavproxy.py"', runner
        )
        self.assertIn("generate_sensor_models:=false", runner)
        self.assertIn(
            '[[ ! -f "$MAVPROXY_SCRIPT" || ! -x "$MAVPROXY_SCRIPT" ]]', runner
        )
        self.assertIn('MAVLINK_PYTHON="/usr/bin/python3.10"', runner)
        self.assertIn(
            'MAVLINK_PYTHON_SITE="/home/ubuntu/.local/lib/python3.10/site-packages"',
            runner,
        )
        self.assertIn("controlled M2 Python/pymavlink runtime is unavailable", runner)
        self.assertNotIn("/etc/hosts", runner)
        self.assertIn('UAV_COUNT=1 EVENT_EPOCH="$event_epoch"', runner)
        self.assertIn("SELF_TEST=0", runner)
        self.assertIn("SIONNA_IPC_ENABLED=0", runner)
        self.assertIn('"$NS3_NS" v-gcs-ns3', runner)
        self.assertIn("ns3_external_ingress_${phase}.pcap", runner)
        self.assertNotIn("run_ns3_tap_slice.sh", runner)
        self.assertIn("--json-output", runner)
        self.assertIn('chown -R 1000:1000 "$RUN_DIR"', runner)

    def test_complete_raw_fixture_passes_despite_producer_passed_flags(self) -> None:
        temporary, run_dir, _builder = self.make_fixture()
        with temporary:
            result = evaluate_m2_vertical_slice(run_dir)
            self.assertTrue(result["passed"], result)
            self.assertEqual(result["contract"], RESULT_CONTRACT)
            self.assertEqual(result["failures"], [])
            self.assertTrue(all(gate["status"] == "passed" for gate in result["gates"].values()))
            self.assertEqual(result["gates"]["ns3_build_receipt"]["status"], "passed")
            self.assertEqual(
                result["packet_engine"]["program"], "ams-tap-packet-engine"
            )
            self.assertEqual(result["packet_engine"]["uav_count"], 1)
            self.assertEqual(
                result["packet_engine"]["event_schema"],
                "ams.ns3.packet_event/v1",
            )
            self.assertEqual(
                set(result["packet_engine"]["config_sha256"]),
                {"good", "recovery"},
            )
            self.assertEqual(
                result["endpoint_transaction"]["subset_cell_ids"],
                [
                    f"uav1.{traffic_class}.{direction}"
                    for traffic_class in ("control", "payload", "additional_data")
                    for direction in ("downlink", "uplink")
                ],
            )
            tail_contract = result["gates"]["endpoint_contract"]["details"][
                "actual_control_tail_capture"
            ]
            self.assertEqual(tail_contract["interface"], "ams-tail0")
            self.assertEqual(tail_contract["pcap_path"], "pcap/uav_tail.pcap")
            self.assertEqual(
                tail_contract["downlink"],
                {
                    "cell_id": "uav1.control.downlink",
                    "capture_point_role": "remote_after_adapter",
                    "capture_point_id": "uav1.mavproxy.tail",
                },
            )
            self.assertEqual(
                tail_contract["uplink"],
                {
                    "cell_id": "uav1.control.uplink",
                    "capture_point_role": "source_before_adapter",
                    "capture_point_id": "uav1.mavproxy.tail",
                },
            )
            self.assertGreater(
                result["gates"]["capture_accounting"]["details"]["captures"]["tail"]
                ["packets_written"],
                0,
            )

    def test_producer_file_and_no_write_revalidation_are_identical(self) -> None:
        temporary, run_dir, _builder = self.make_fixture()
        with temporary:
            output = run_dir / "metrics/m2_validation_results.json"
            producer_stdout = io.StringIO()
            with redirect_stdout(producer_stdout):
                self.assertEqual(
                    main(
                        [
                            "--run-dir",
                            str(run_dir),
                            "--json-output",
                            str(output),
                        ]
                    ),
                    0,
                )
            independent_stdout = io.StringIO()
            with redirect_stdout(independent_stdout):
                self.assertEqual(
                    main(["--run-dir", str(run_dir), "--no-write"]), 0
                )
            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8")),
                json.loads(independent_stdout.getvalue()),
            )

    def test_missing_ns3_build_receipt_fails_receipt_and_manifest_gates(self) -> None:
        temporary, run_dir, _builder = self.make_fixture()
        with temporary:
            (run_dir / "metrics/ns3_tap_build_receipt.json").unlink()
            result = evaluate_m2_vertical_slice(run_dir)
            self.assertFalse(result["passed"])
            self.assertEqual(result["gates"]["ns3_build_receipt"]["status"], "failed")
            self.assertEqual(result["gates"]["manifest"]["status"], "failed")

    def test_tampered_ns3_build_receipt_fails_independent_receipt_gate(self) -> None:
        temporary, run_dir, builder = self.make_fixture()
        with temporary:
            path = run_dir / "metrics/ns3_tap_build_receipt.json"
            document = json.loads(path.read_text(encoding="utf-8"))
            document["subject"]["executable"]["sha256"] = "e" * 64
            path.chmod(0o644)
            path.write_text(
                json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            path.chmod(0o444)
            builder.seal()
            result = evaluate_m2_vertical_slice(run_dir)
            self.assertFalse(result["passed"])
            self.assertEqual(result["gates"]["manifest"]["status"], "passed")
            self.assertEqual(result["gates"]["ns3_build_receipt"]["status"], "failed")

    def test_old_vertical_slice_receipt_cannot_substitute_shared_engine(self) -> None:
        temporary, run_dir, builder = self.make_fixture()
        with temporary:
            receipt_path = run_dir / "metrics/ns3_tap_build_receipt.json"
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["subject"]["program"] = "ams-tap-vertical-slice"
            receipt["subject_sha256"] = subject_digest(receipt["subject"])
            receipt_path.chmod(0o644)
            receipt_path.write_text(
                json.dumps(receipt, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            receipt_path.chmod(0o444)
            metadata_path = run_dir / "metrics/m2_run.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            receipt_record = builder._raw_record(
                "metrics/ns3_tap_build_receipt.json"
            )
            metadata["packet_engine"]["build_receipt"] = receipt_record
            metadata["packet_engine"]["build_receipt_sha256"] = receipt_record[
                "sha256"
            ]
            metadata_path.write_text(
                json.dumps(metadata, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            builder.seal()
            result = evaluate_m2_vertical_slice(run_dir)
            self.assertFalse(result["passed"])
            self.assertEqual(result["gates"]["manifest"]["status"], "passed")
            self.assertIn(
                "not for ams-tap-packet-engine",
                "\n".join(result["gates"]["ns3_build_receipt"]["failures"]),
            )

    def test_endpoint_subset_hash_mutation_fails_after_consistent_reseal(self) -> None:
        temporary, run_dir, builder = self.make_fixture()
        with temporary:
            contract_path = run_dir / "metrics/m2_endpoint_contract.json"
            contract = json.loads(contract_path.read_text(encoding="utf-8"))
            contract["subset"]["resolved_cells_sha256"] = "f" * 64
            contract_path.write_text(
                json.dumps(contract, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            metadata_path = run_dir / "metrics/m2_run.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["endpoint_transaction"] = contract
            metadata_path.write_text(
                json.dumps(metadata, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            builder.seal()
            result = evaluate_m2_vertical_slice(run_dir)
            self.assertFalse(result["passed"])
            self.assertEqual(result["gates"]["manifest"]["status"], "passed")
            self.assertEqual(result["gates"]["endpoint_contract"]["status"], "failed")

    def test_endpoint_subset_cannot_relabel_actual_tail_as_uav_eth0(self) -> None:
        temporary, run_dir, builder = self.make_fixture()
        with temporary:
            contract_path = run_dir / "metrics/m2_endpoint_contract.json"
            contract = json.loads(contract_path.read_text(encoding="utf-8"))
            contract["subset"]["actual_control_tail_capture"]["downlink"][
                "capture_point_id"
            ] = "uav1.sink.eth0"
            contract_path.write_text(
                json.dumps(contract, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            metadata_path = run_dir / "metrics/m2_run.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["endpoint_transaction"] = contract
            metadata_path.write_text(
                json.dumps(metadata, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            builder.seal()
            result = evaluate_m2_vertical_slice(run_dir)
            self.assertFalse(result["passed"])
            self.assertEqual(result["gates"]["manifest"]["status"], "passed")
            self.assertEqual(result["gates"]["endpoint_contract"]["status"], "failed")

    def test_packet_engine_event_schema_mutation_fails_after_reseal(self) -> None:
        temporary, run_dir, builder = self.make_fixture()
        with temporary:
            events_path = run_dir / "logs/ns3_good_packet_events.jsonl"
            records = read_jsonl(events_path)
            records[0]["schema"] = "ams.tap_packet_event/v1"
            write_jsonl(events_path, records)
            metadata_path = run_dir / "metrics/m2_run.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["packet_engine"]["phases"]["good"]["events"] = (
                builder._raw_record("logs/ns3_good_packet_events.jsonl")
            )
            metadata_path.write_text(
                json.dumps(metadata, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            builder.seal()
            result = evaluate_m2_vertical_slice(run_dir)
            self.assertFalse(result["passed"])
            self.assertEqual(result["gates"]["manifest"]["status"], "passed")
            self.assertIn(
                "schema identity mismatch",
                "\n".join(result["gates"]["packet_engine"]["failures"]),
            )

    def test_packet_engine_config_mutation_fails_after_reseal(self) -> None:
        temporary, run_dir, builder = self.make_fixture()
        with temporary:
            config_path = run_dir / "logs/ns3_recovery_config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["resolved"]["uav_count"] = 5
            config_path.write_text(
                json.dumps(config, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            metadata_path = run_dir / "metrics/m2_run.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["packet_engine"]["phases"]["recovery"]["config"] = (
                builder._raw_record("logs/ns3_recovery_config.json")
            )
            metadata_path.write_text(
                json.dumps(metadata, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            builder.seal()
            result = evaluate_m2_vertical_slice(run_dir)
            self.assertFalse(result["passed"])
            self.assertIn(
                "config/hash is not exact",
                "\n".join(result["gates"]["packet_engine"]["failures"]),
            )

    def test_capture_kernel_drop_counter_fails_after_reseal(self) -> None:
        temporary, run_dir, builder = self.make_fixture()
        with temporary:
            stats_path = run_dir / "logs/capture_gcs_good_stats.json"
            stats = json.loads(stats_path.read_text(encoding="utf-8"))
            stats["packets_dropped_kernel"] = 1
            stats_path.write_text(
                json.dumps(stats, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            builder.seal()
            result = evaluate_m2_vertical_slice(run_dir)
            self.assertFalse(result["passed"])
            self.assertEqual(result["gates"]["manifest"]["status"], "passed")
            self.assertEqual(result["gates"]["capture_accounting"]["status"], "failed")

    def test_command_result_true_cannot_replace_raw_ack_events(self) -> None:
        temporary, run_dir, builder = self.make_fixture()
        with temporary:
            path = run_dir / "logs/m2_probe_events.jsonl"
            records = [
                record
                for record in read_jsonl(path)
                if not (record.get("phase") == "good" and record.get("event") == "command_ack")
            ]
            for record in records:
                if record.get("phase") == "good" and record.get("event") == "command_result":
                    record["passed"] = True
                    record["ack"] = True
            resequence(records)
            write_jsonl(path, records)
            builder.seal()
            result = evaluate_m2_vertical_slice(run_dir)
            self.assertFalse(result["passed"])
            self.assertEqual(result["gates"]["probe_transactions"]["status"], "failed")
            self.assertIn("ACK attempts", "\n".join(result["gates"]["probe_transactions"]["failures"]))

    def test_any_down_ack_fails_even_if_summary_claims_zero(self) -> None:
        temporary, run_dir, _builder = self.make_fixture(down_ack=True)
        with temporary:
            result = evaluate_m2_vertical_slice(run_dir)
            self.assertFalse(result["passed"])
            text = "\n".join(result["gates"]["probe_transactions"]["failures"])
            self.assertIn("expected zero", text)

    def test_mixed_runtime_record_fails_closed(self) -> None:
        temporary, run_dir, builder = self.make_fixture()
        with temporary:
            path = run_dir / "logs/m2_probe_events.jsonl"
            records = read_jsonl(path)
            records[10]["runtime_id"] = "other-runtime"
            write_jsonl(path, records)
            builder.seal()
            result = evaluate_m2_vertical_slice(run_dir)
            self.assertFalse(result["passed"])
            self.assertIn(
                "runtime_id does not match",
                "\n".join(result["gates"]["probe_transactions"]["failures"]),
            )

    def test_missing_payload_at_one_capture_point_fails(self) -> None:
        temporary, run_dir, builder = self.make_fixture()
        with temporary:
            payloads = (
                builder.phase_payloads["good"]["requests"][1:]
                + builder.phase_payloads["good"]["responses"]
                + builder.phase_payloads["good"]["heartbeats"]
            )
            write_pcap(run_dir / "pcap/ns3_egress_good.pcap", payloads, timestamp_seed=99)
            builder.seal()
            result = evaluate_m2_vertical_slice(run_dir)
            self.assertFalse(result["passed"])
            self.assertEqual(result["gates"]["packet_captures"]["status"], "failed")
            self.assertIn("expected at least", "\n".join(result["gates"]["packet_captures"]["failures"]))

    def test_down_command_reaching_uav_capture_fails(self) -> None:
        temporary, run_dir, builder = self.make_fixture()
        with temporary:
            write_pcap(
                run_dir / "pcap/uav_egress_down.pcap",
                builder.phase_payloads["down"]["requests"],
                timestamp_seed=101,
            )
            builder.seal()
            result = evaluate_m2_vertical_slice(run_dir)
            self.assertFalse(result["passed"])
            self.assertIn(
                "reached the UAV side",
                "\n".join(result["gates"]["packet_captures"]["failures"]),
            )

    def test_down_offer_must_reach_persistent_ns3_external_ingress(self) -> None:
        temporary, run_dir, builder = self.make_fixture()
        with temporary:
            write_pcap(
                run_dir / "pcap/ns3_external_ingress_down.pcap",
                [],
                timestamp_seed=101,
            )
            builder.write_capture_stats()
            builder.seal()
            result = evaluate_m2_vertical_slice(run_dir)
            self.assertFalse(result["passed"])
            failures = "\n".join(result["gates"]["packet_captures"]["failures"])
            self.assertIn("ns3_external_ingress_down.pcap", failures)
            self.assertIn("down-attempt payload", failures)

    def test_copied_capture_point_pcap_fails(self) -> None:
        temporary, run_dir, builder = self.make_fixture()
        with temporary:
            shutil.copyfile(
                run_dir / "pcap/gcs_ingress_good.pcap",
                run_dir / "pcap/ns3_ingress_good.pcap",
            )
            builder.seal()
            result = evaluate_m2_vertical_slice(run_dir)
            self.assertFalse(result["passed"])
            self.assertIn(
                "byte-identical",
                "\n".join(result["gates"]["packet_captures"]["failures"]),
            )

    def test_stable_adapter_identity_change_fails(self) -> None:
        temporary, run_dir, builder = self.make_fixture()
        with temporary:
            path = run_dir / "logs/m2_process_events.jsonl"
            records = read_jsonl(path)
            for record in records:
                if record.get("phase") == "recovery" and record.get("role") == "uav_adapter":
                    record["pid"] = 999
                    record["start_ticks"] = 99_999
            write_jsonl(path, records)
            builder.seal()
            result = evaluate_m2_vertical_slice(run_dir)
            self.assertFalse(result["passed"])
            self.assertIn(
                "identity changed",
                "\n".join(result["gates"]["process_identity"]["failures"]),
            )

    def test_m2_cannot_forge_runtime_sionna_consumption(self) -> None:
        mutations = {
            "consumed": True,
            "runtime_id": "tcp_jsonl_real_sionna",
            "selected_id": "not_used_m2",
        }
        for name, replacement in mutations.items():
            with self.subTest(mutation=name):
                temporary, run_dir, builder = self.make_fixture()
                with temporary:
                    path = run_dir / "metrics/provenance.json"
                    provenance = json.loads(path.read_text(encoding="utf-8"))
                    implementation = provenance["implementation"]
                    if name == "consumed":
                        implementation["radio_provider_runtime_consumed"] = replacement
                    elif name == "runtime_id":
                        implementation["runtime_provider_id"] = replacement
                    else:
                        implementation["radio_provider_id"] = replacement
                    path.write_text(
                        json.dumps(provenance, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )
                    builder.seal()
                    result = evaluate_m2_vertical_slice(run_dir)
                    self.assertFalse(result["passed"])
                    self.assertIn(
                        "provider-consumption",
                        "\n".join(result["gates"]["provenance"]["failures"]),
                    )

    def test_critical_log_is_manifested_but_still_fails(self) -> None:
        temporary, run_dir, builder = self.make_fixture()
        with temporary:
            (run_dir / "logs/ns3_recovery.log").write_text(
                "Segmentation fault (core dumped)\n", encoding="utf-8"
            )
            builder.seal()
            result = evaluate_m2_vertical_slice(run_dir)
            self.assertFalse(result["passed"])
            self.assertEqual(result["gates"]["manifest"]["status"], "passed")
            self.assertEqual(result["gates"]["critical_logs"]["status"], "failed")

    def test_manifest_detects_post_seal_mutation(self) -> None:
        temporary, run_dir, _builder = self.make_fixture()
        with temporary:
            with (run_dir / "pcap/gcs_ingress_good.pcap").open("ab") as handle:
                handle.write(b"tamper")
            result = evaluate_m2_vertical_slice(run_dir)
            self.assertFalse(result["passed"])
            self.assertIn(
                "mismatch",
                "\n".join(result["gates"]["manifest"]["failures"]),
            )

    def test_nonstandard_nan_fails_without_crashing(self) -> None:
        temporary, run_dir, builder = self.make_fixture()
        with temporary:
            path = run_dir / "logs/m2_probe_events.jsonl"
            records = read_jsonl(path)
            timeout = next(record for record in records if record.get("event") == "heartbeat_timeout")
            timeout["timeout_s"] = float("nan")
            write_jsonl(path, records)
            builder.seal()
            result = evaluate_m2_vertical_slice(run_dir)
            self.assertFalse(result["passed"])
            self.assertIn(
                "non-standard JSON numeric constant",
                "\n".join(result["gates"]["probe_transactions"]["failures"]),
            )


if __name__ == "__main__":
    unittest.main()
