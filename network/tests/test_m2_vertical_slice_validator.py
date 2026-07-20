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
    ENGINE_CAPTURE_POINTS,
    ENGINE_CONTRACT,
    ENGINE_EVENT_SCHEMA,
    ENGINE_PHASES,
    ENGINE_PROGRAM,
    EVIDENCE_CONTRACT,
    MANIFEST_CONTRACT,
    PERSISTENT_CAPTURE_SPECS,
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
from network.scripts import raw_packet_capture  # noqa: E402


RUN_ID = "m2_fixture"
RUNTIME_ID = "m2-runtime-fixture"
RUN_NONCE = "m2nonce0123456789abcdef"
SOURCE_HASH = "c" * 64
UTC_BASE = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def transaction_id(
    *,
    phase: str,
    attempt: int,
    marker_sha256: str,
    command_sha256: str,
    mavlink_seq: int,
) -> str:
    identity = {
        "attempt": attempt,
        "command_sha256": command_sha256,
        "marker_sha256": marker_sha256,
        "mavlink_command": 512,
        "mavlink_seq": mavlink_seq,
        "phase": phase,
        "run_nonce": RUN_NONCE,
        "source_component": 190,
        "source_system": 255,
        "target_component": 1,
        "target_system": 1,
    }
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()


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


def resequence_adapter(records: list[dict[str, Any]]) -> None:
    """Restore the adapter log's monotonic/event-sequence invariants after a mutation."""
    records.sort(key=lambda record: record["monotonic_ns"])
    for sequence, record in enumerate(records, start=1):
        record["event_seq"] = sequence
        record["wall_utc"] = utc_for(1_000 + sequence)


def refresh_adapter_stop_counters(records: list[dict[str, Any]]) -> None:
    """Keep the fixture's independently checked adapter counters truthful."""
    counters = {
        "gcs_to_tail": 0,
        "tail_to_gcs": 0,
        "dropped_no_peer": 0,
        "dropped_unexpected_peer": 0,
    }
    for record in records:
        if record.get("event") == "forward" and record.get("direction") in counters:
            counters[record["direction"]] += 1
        elif record.get("event") == "drop":
            if record.get("reason") == "mavproxy_peer_unknown":
                counters["dropped_no_peer"] += 1
            elif record.get("reason") in (
                "unexpected_tail_peer",
                "unexpected_gcs_peer",
            ):
                counters["dropped_unexpected_peer"] += 1
    stops = [record for record in records if record.get("event") == "adapter_stop"]
    if len(stops) != 1:
        raise AssertionError("fixture must contain exactly one adapter_stop")
    stops[0]["counters"] = counters


class FixtureBuilder:
    def __init__(
        self,
        run_dir: Path,
        *,
        down_ack: bool = False,
        positive_heartbeat_count: int = 3,
    ) -> None:
        self.run_dir = run_dir
        self.down_ack = down_ack
        self.positive_heartbeat_count = positive_heartbeat_count
        self.probe_records: list[dict[str, Any]] = []
        self.adapter_records: list[dict[str, Any]] = []
        self.process_records: list[dict[str, Any]] = []
        self.phase_payloads: dict[str, dict[str, list[bytes]]] = {}
        # Keep large real-time gaps between transaction windows.  The M2
        # lifecycle contract deliberately requires independent ten-second
        # readiness/pre-stop dwells, so a positive fixture must not collapse
        # them into a few synthetic milliseconds.
        self.probe_ns = 100_000_000_000
        self.adapter_sequence = 0
        self.process_sequence = 0
        self.lifecycle_points: dict[str, int] = {}
        self.endpoint_configuration = {
            "gcs_bind": ["10.71.0.10", 14600],
            "uav_endpoint": ["10.71.1.10", 14601],
            "target_system": 1,
            "target_component": 1,
            "source_system": 255,
            "source_component": 190,
        }
        self.endpoint_configuration_sha256 = digest(
            json.dumps(
                self.endpoint_configuration, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        )
        self.endpoint_instance_id = digest(b"m2-fixture-persistent-gcs-endpoint")
        self.endpoint_pid = 500

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
        if event == "forward":
            adapter_datagram_seq = sum(
                1 for record in self.adapter_records if record.get("event") == "forward"
            ) + 1
            payload_hash = fields.get("sha256")
            payload_size = fields.get("bytes")
            fields.setdefault("adapter_datagram_seq", adapter_datagram_seq)
            fields.setdefault("transport_payload_sha256", payload_hash)
            fields.setdefault("transport_payload_size", payload_size)
            fields.setdefault("received_monotonic_ns", monotonic_ns - 300)
            fields.setdefault("send_start_monotonic_ns", monotonic_ns - 200)
            fields.setdefault("send_complete_monotonic_ns", monotonic_ns - 100)
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
        tx_datagram_seq = 0
        rx_datagram_seq = 0

        def raw_rx(payload: bytes) -> dict[str, Any]:
            nonlocal rx_datagram_seq
            rx_datagram_seq += 1
            next_monotonic = self.probe_ns + 1_000_000
            return self._probe(
                phase,
                "datagram_rx",
                event_schema="ams.m2.probe-event/v2",
                rx_datagram_seq=rx_datagram_seq,
                transport_payload_sha256=digest(payload),
                transport_payload_size=len(payload),
                peer=["10.71.1.10", 14601],
                received_monotonic_ns=next_monotonic - 750_000,
            )

        def raw_tx(
            *,
            payload: bytes,
            transaction: str,
            leg: str,
            attempt: int,
            nonce: str,
        ) -> dict[str, Any]:
            nonlocal tx_datagram_seq
            tx_datagram_seq += 1
            next_monotonic = self.probe_ns + 1_000_000
            return self._probe(
                phase,
                "datagram_tx",
                event_schema="ams.m2.probe-event/v2",
                transaction_id=transaction,
                leg=leg,
                attempt=attempt,
                nonce=nonce,
                tx_datagram_seq=tx_datagram_seq,
                transport_payload_sha256=digest(payload),
                transport_payload_size=len(payload),
                bytes_sent=len(payload),
                destination=["10.71.1.10", 14601],
                send_start_monotonic_ns=next_monotonic - 750_000,
                send_complete_monotonic_ns=next_monotonic - 500_000,
            )

        heartbeat_occurrences: list[tuple[bytes, dict[str, Any], dict[str, Any]]] = []
        if successful:
            for index in range(1, self.positive_heartbeat_count + 1):
                heartbeat_payload = f"heartbeat:{phase}:{index}".encode()
                payloads["heartbeats"].append(heartbeat_payload)
                raw = raw_rx(heartbeat_payload)
                heartbeat = self._probe(
                    phase,
                    "heartbeat",
                    attempt=None,
                    nonce=None,
                    packet_sha256=digest(heartbeat_payload),
                    source_system=1,
                    source_component=1,
                    message_type="HEARTBEAT",
                    peer=["10.71.1.10", 14601],
                    rx_datagram_seq=raw["rx_datagram_seq"],
                    frame_index=1,
                    liveness_observation=True,
                )
                heartbeat_occurrences.append((heartbeat_payload, raw, heartbeat))

        attempt_records: list[dict[str, Any]] = []
        for attempt in range(1, attempts + 1):
            nonce = f"{RUN_NONCE}:{phase}:{attempt}"
            marker = f"MAVLINK2-STATUSTEXT:AMS-M2:{nonce}".encode()
            command = f"MAVLINK2-COMMAND_LONG:{phase}:{attempt}".encode()
            ack = f"MAVLINK2-COMMAND_ACK:{phase}:{attempt}".encode() if successful else None
            telemetry = f"MAVLINK2-AUTOPILOT_VERSION:{phase}:{attempt}".encode() if successful else None
            payloads["requests"].extend((marker, command))
            sequence = (attempt * 2) % 256
            transaction = transaction_id(
                phase=phase,
                attempt=attempt,
                marker_sha256=digest(marker),
                command_sha256=digest(command),
                mavlink_seq=sequence,
            )
            attempt_record = self._probe(
                phase,
                "command_attempt",
                attempt=attempt,
                nonce=nonce,
                transaction_id=transaction,
                marker_sha256=digest(marker),
                command_sha256=digest(command),
                packet_sha256=digest(command),
                mavlink_seq=sequence,
                source_system=255,
                source_component=190,
                target_system=1,
                target_component=1,
                mavlink_command=512,
                # Deliberately untrusted producer claims; raw events decide.
                expected_ack=not successful,
            )
            marker_tx = raw_tx(
                payload=marker,
                transaction=transaction,
                leg="marker",
                attempt=attempt,
                nonce=nonce,
            )
            command_tx = raw_tx(
                payload=command,
                transaction=transaction,
                leg="command",
                attempt=attempt,
                nonce=nonce,
            )
            ack_record: dict[str, Any] | None = None
            telemetry_record: dict[str, Any] | None = None
            ack_raw: dict[str, Any] | None = None
            telemetry_raw: dict[str, Any] | None = None
            if successful or (self.down_ack and phase == "down" and attempt == 1):
                ack = ack or b"FORGED-DOWN-ACK"
                payloads["responses"].append(ack)
                ack_raw = raw_rx(ack)
                ack_record = self._probe(
                    phase,
                    "command_ack",
                    attempt=attempt,
                    nonce=nonce,
                    transaction_id=transaction,
                    request_sha256=digest(command),
                    request_mavlink_seq=sequence,
                    packet_sha256=digest(ack),
                    source_system=1,
                    source_component=1,
                    mavlink_command=512,
                    mavlink_result=0,
                    peer=["10.71.1.10", 14601],
                    rx_datagram_seq=ack_raw["rx_datagram_seq"],
                    frame_index=1,
                )
            if successful:
                assert telemetry is not None
                payloads["responses"].append(telemetry)
                telemetry_raw = raw_rx(telemetry)
                telemetry_record = self._probe(
                    phase,
                    "telemetry",
                    attempt=attempt,
                    nonce=nonce,
                    transaction_id=transaction,
                    request_sha256=digest(command),
                    request_mavlink_seq=sequence,
                    packet_sha256=digest(telemetry),
                    source_system=1,
                    source_component=1,
                    message_id=148,
                    peer=["10.71.1.10", 14601],
                    rx_datagram_seq=telemetry_raw["rx_datagram_seq"],
                    frame_index=1,
                )
            self._probe(
                phase,
                "command_result",
                attempt=attempt,
                nonce=nonce,
                transaction_id=transaction,
                request_sha256=digest(command),
                request_mavlink_seq=sequence,
                passed=False,
                ack=True,  # ignored even in down
                telemetry=True,
                ack_latency_ms=1.0,
            )
            attempt_records.append(
                {
                    "attempt": attempt_record,
                    "marker": marker,
                    "command": command,
                    "marker_tx": marker_tx,
                    "command_tx": command_tx,
                    "ack": ack,
                    "telemetry": telemetry,
                    "ack_record": ack_record,
                    "telemetry_record": telemetry_record,
                    "ack_raw": ack_raw,
                    "telemetry_raw": telemetry_raw,
                }
            )

        if not successful:
            self._probe(phase, "heartbeat_timeout", timed_out=True, timeout_s=5.0)
        phase_end = self._probe(
            phase,
            "phase_end",
            attempts=attempts,
            acknowledgements=attempts if successful else 0,
            telemetry_responses=attempts if successful else 0,
            heartbeat_count=self.positive_heartbeat_count if successful else 0,
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

        # Adapter events use the same host monotonic clock.  Requests occur
        # after their raw command_attempt record, and each response arrives at
        # the adapter before its probe-side decode record.  This fixture makes
        # the transaction causality checks independent of producer summaries.
        if successful:
            for heartbeat, raw, _decoded in heartbeat_occurrences:
                self._adapter(
                    "forward",
                    raw["received_monotonic_ns"] - 100,
                    direction="tail_to_gcs",
                    bytes=len(heartbeat),
                    sha256=digest(heartbeat),
                )
            for item in attempt_records:
                attempt_record = item["attempt"]
                marker = item["marker"]
                command = item["command"]
                marker_tx = item["marker_tx"]
                command_tx = item["command_tx"]
                ack = item["ack"]
                telemetry = item["telemetry"]
                ack_record = item["ack_record"]
                telemetry_record = item["telemetry_record"]
                ack_raw = item["ack_raw"]
                telemetry_raw = item["telemetry_raw"]
                self._adapter(
                    "forward",
                    marker_tx["send_complete_monotonic_ns"] + 1_000,
                    direction="gcs_to_tail",
                    bytes=len(marker),
                    sha256=digest(marker),
                )
                self._adapter(
                    "forward",
                    command_tx["send_complete_monotonic_ns"] + 1_000,
                    direction="gcs_to_tail",
                    bytes=len(command),
                    sha256=digest(command),
                )
                assert ack is not None and ack_record is not None and ack_raw is not None
                self._adapter(
                    "forward",
                    ack_raw["received_monotonic_ns"] - 1_000,
                    direction="tail_to_gcs",
                    bytes=len(ack),
                    sha256=digest(ack),
                )
                assert telemetry is not None and telemetry_record is not None and telemetry_raw is not None
                self._adapter(
                    "forward",
                    telemetry_raw["received_monotonic_ns"] - 1_000,
                    direction="tail_to_gcs",
                    bytes=len(telemetry),
                    sha256=digest(telemetry),
                )
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
        self.probe_ns += 25_000_000_000
        self._phase("down", 5, False)
        self.probe_ns += 25_000_000_000
        self._phase("recovery", 10, True)
        self.decorate_persistent_endpoint_evidence()
        self.write_lifecycle_evidence()

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
                pid=self.endpoint_pid,
                ticks=50_000,
                command="persistent-gcs-endpoint",
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
        (self.run_dir / "logs/m2_gcs_endpoint.stdout").write_text(
            json.dumps(
                {
                    "schema": "ams.m2.persistent-gcs-endpoint/v1",
                    "event": "endpoint_ready",
                    "endpoint_instance_id": self.endpoint_instance_id,
                    "control_socket": "logs/m2_gcs_endpoint.sock",
                    "gcs_bind": ["10.71.0.10", 14600],
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        (self.run_dir / "logs/m2_gcs_endpoint.stderr").write_bytes(b"")
        (self.run_dir / "logs/m2_gcs_endpoint_shutdown.log").write_text(
            json.dumps({"ok": True, "operation": "shutdown"}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (self.run_dir / "logs/m2_runner.log").write_text(
            "2026-07-12T12:00:00Z runtime complete\n", encoding="utf-8"
        )

        timestamp_seed = 1
        positive_payloads = self.persistent_positive_payloads()
        persistent_payloads = {
            "pcap/uav_tail.pcap": positive_payloads,
            "pcap/gcs_ingress.pcap": (
                positive_payloads + self.phase_payloads["down"]["requests"]
            ),
            "pcap/ns3_external_ingress.pcap": (
                positive_payloads + self.phase_payloads["down"]["requests"]
            ),
            "pcap/ns3_external_egress.pcap": positive_payloads,
            "pcap/uav_egress.pcap": positive_payloads,
        }
        for relative, payloads in persistent_payloads.items():
            write_pcap(
                self.run_dir / relative,
                payloads,
                timestamp_seed=timestamp_seed,
            )
            timestamp_seed += 1
        for phase in ("good", "recovery"):
            payloads = self.phase_positive_payloads(phase)
            for point in ENGINE_CAPTURE_POINTS:
                write_pcap(
                    self.run_dir / f"pcap/{point}_{phase}.pcap",
                    payloads,
                    timestamp_seed=timestamp_seed,
                )
                timestamp_seed += 1
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

    def phase_positive_payloads(self, phase: str) -> list[bytes]:
        return (
            self.phase_payloads[phase]["requests"]
            + self.phase_payloads[phase]["responses"]
            + self.phase_payloads[phase]["heartbeats"]
        )

    def persistent_positive_payloads(self) -> list[bytes]:
        return self.phase_positive_payloads("good") + self.phase_positive_payloads(
            "recovery"
        )

    def decorate_persistent_endpoint_evidence(self) -> None:
        """Turn phase fixture records into one service-owned endpoint journal."""

        source = list(self.probe_records)
        by_phase = {
            phase: [record for record in source if record["phase"] == phase]
            for phase in ("good", "down", "recovery")
        }
        records: list[dict[str, Any]] = []

        def synthetic(phase: str, event: str, monotonic_ns: int, **fields: Any) -> dict[str, Any]:
            return {
                "schema_version": 2,
                "run_id": RUN_ID,
                "runtime_id": RUNTIME_ID,
                "run_nonce": RUN_NONCE,
                "phase": phase,
                "event": event,
                "monotonic_ns": monotonic_ns,
                **fields,
            }

        first_good = by_phase["good"][0]["monotonic_ns"]
        records.append(
            synthetic(
                "good",
                "endpoint_started",
                first_good - 2_000_000,
                endpoint_configuration=self.endpoint_configuration,
                gcs_bind=self.endpoint_configuration["gcs_bind"],
                endpoint_pid=self.endpoint_pid,
                endpoint_uid=0,
                control_socket="logs/m2_gcs_endpoint.sock",
                pre_window_quiet_s=0.1,
                pre_window_max_wait_s=10.0,
            )
        )
        for index, phase in enumerate(("good", "down", "recovery"), start=1):
            phase_records = by_phase[phase]
            phase_start = next(record for record in phase_records if record["event"] == "phase_start")
            phase_health = next(record for record in phase_records if record["event"] == "endpoint_health")
            direct = [record for record in phase_records if record["event"] == "direct_endpoint_probe"]
            if len(direct) != 2:
                raise AssertionError("fixture phase must contain two direct endpoint probes")
            records.append(
                synthetic(
                    phase,
                    "endpoint_pre_window_quiescent",
                    direct[0]["monotonic_ns"] - 500_000,
                    pre_window_for_phase=phase,
                    quiet_s=0.1,
                    max_wait_s=10.0,
                    discarded_datagrams=0,
                    waited_s=0.1,
                )
            )
            for record in phase_records:
                records.append(dict(record))
                if record is direct[-1]:
                    records.append(
                        synthetic(
                            phase,
                            "endpoint_window_open",
                            phase_start["monotonic_ns"] - 500_000,
                            window_id=f"{index}-{phase}",
                        )
                    )
            records.append(
                synthetic(
                    phase,
                    "endpoint_window_close",
                    phase_health["monotonic_ns"] + 500_000,
                    window_id=f"{index}-{phase}",
                    completed=True,
                    reason=None,
                    unfinished_transaction_ids=[],
                )
            )
        records.append(
            synthetic(
                "recovery",
                "endpoint_stopped",
                records[-1]["monotonic_ns"] + 1_000_000,
                completed_phases=["good", "down", "recovery"],
                lifecycle_complete=True,
                phase_failed=False,
            )
        )

        window_id_by_phase = {
            phase: f"{index}-{phase}"
            for index, phase in enumerate(("good", "down", "recovery"), start=1)
        }
        raw_rx_lookup: dict[tuple[str, int], int] = {}
        tx_sequence = 0
        rx_sequence = 0
        active_window: str | None = None
        active_phase: str | None = None
        for sequence, record in enumerate(records, start=1):
            record["event_seq"] = sequence
            record["wall_utc"] = utc_for(sequence)
            event = record["event"]
            phase = record["phase"]
            record["endpoint_event_schema"] = "ams.m2.persistent-gcs-endpoint/v1"
            record["endpoint_instance_id"] = self.endpoint_instance_id
            record["endpoint_generation"] = 1
            record["endpoint_configuration_sha256"] = self.endpoint_configuration_sha256
            if event in {"datagram_tx", "datagram_rx"}:
                record["event_schema"] = "ams.m2.probe-event/v2"
            else:
                record["event_schema"] = "ams.m2.persistent-gcs-endpoint/v1"
            if event == "endpoint_window_open":
                active_window = window_id_by_phase[phase]
                active_phase = phase
                record["endpoint_window_id"] = active_window
                record["tx_datagram_seq_before"] = tx_sequence
                record["rx_datagram_seq_before"] = rx_sequence
            elif event == "endpoint_window_close":
                record["endpoint_window_id"] = active_window
                record["tx_datagram_seq_after"] = tx_sequence
                record["rx_datagram_seq_after"] = rx_sequence
                active_window = None
                active_phase = None
            elif event in {
                "phase_start",
                "phase_end",
                "command_attempt",
                "command_result",
                "command_ack",
                "telemetry",
                "heartbeat",
                "heartbeat_timeout",
                "endpoint_health",
                "datagram_tx",
                "datagram_rx",
            }:
                if active_phase != phase or active_window is None:
                    raise AssertionError(f"fixture event {event} is outside its persistent window")
                record["endpoint_window_id"] = active_window
            if event == "datagram_tx":
                tx_sequence += 1
                record["tx_datagram_seq"] = tx_sequence
            elif event == "datagram_rx":
                old_sequence = record.get("rx_datagram_seq")
                if not isinstance(old_sequence, int):
                    raise AssertionError("fixture raw RX has no original sequence")
                rx_sequence += 1
                raw_rx_lookup[(phase, old_sequence)] = rx_sequence
                record["rx_datagram_seq"] = rx_sequence

        for record in records:
            if record["event"] in {"heartbeat", "command_ack", "telemetry"}:
                old_sequence = record.get("rx_datagram_seq")
                if not isinstance(old_sequence, int):
                    raise AssertionError("fixture decoded event has no original RX sequence")
                record["rx_datagram_seq"] = raw_rx_lookup[(record["phase"], old_sequence)]
        records[-1]["tx_datagram_seq"] = tx_sequence
        records[-1]["rx_datagram_seq"] = rx_sequence
        self.probe_records = records
        for phase in ("good", "down", "recovery"):
            starts = [
                record for record in records if record["phase"] == phase and record["event"] == "phase_start"
            ]
            ends = [
                record for record in records if record["phase"] == phase and record["event"] == "phase_end"
            ]
            self.phase_payloads[phase]["window"] = [starts[0]["monotonic_ns"], ends[0]["monotonic_ns"]]

    def write_capture_stats(self) -> None:
        captures_ready = self.lifecycle_points["captures_ready"]
        engine2_stopped = self.lifecycle_points["engine2_stopped"]
        for index, (key, interface, relative) in enumerate(
            PERSISTENT_CAPTURE_SPECS, start=1
        ):
            pcap = self.run_dir / relative
            packets = pcap_packet_count(pcap)
            stats = {
                "contract": raw_packet_capture.STATS_CONTRACT,
                "interface": interface,
                "capture_protocol": raw_packet_capture.CAPTURE_PROTOCOL,
                "packet_filter": raw_packet_capture.PACKET_FILTER,
                "pcap_path": pcap.name,
                "pcap_bytes": pcap.stat().st_size,
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
                "started_monotonic_ns": captures_ready - (10_000_000 + index),
                "stopped_monotonic_ns": engine2_stopped + (10_000_000 + index),
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

    def write_lifecycle_evidence(self) -> None:
        """Create an independent long-lived lifecycle/monitor evidence stream.

        The transaction generator intentionally advances in very small steps;
        this fixture establishes realistic, non-overlapping lifecycle epochs
        around those windows so the validator proves actual dwell boundaries
        instead of trusting a producer-side boolean.
        """

        good_start, good_end = self.phase_payloads["good"]["window"]
        down_start, down_end = self.phase_payloads["down"]["window"]
        recovery_start, recovery_end = self.phase_payloads["recovery"]["window"]
        points = {
            "captures_ready": good_start - 30_000_000_000,
            "endpoints_ready": good_start - 29_000_000_000,
            "engine1_ready": good_start - 25_000_000_000,
            "good_dwell_start": good_start - 20_000_000_000,
            "good_dwell_complete": good_start - 10_000_000_000,
            "good_start": good_start - 1_000_000,
            "good_terminal": good_end + 1_000_000,
            "prestop_dwell_start": good_end + 1_000_000_000,
            "prestop_dwell_complete": good_end + 11_000_000_000,
            "stop_requested": good_end + 12_000_000_000,
            "engine1_stopped": good_end + 13_000_000_000,
            "stopped_drain_start": good_end + 14_000_000_000,
            "stopped_drain_complete": good_end + 15_000_000_000,
            "down_start": down_start - 1_000_000,
            "down_terminal": down_end + 1_000_000,
            "engine2_ready": recovery_start - 20_000_000_000,
            "recovery_dwell_start": recovery_start - 18_000_000_000,
            "recovery_dwell_complete": recovery_start - 8_000_000_000,
            "recovery_start": recovery_start - 1_000_000,
            "recovery_terminal": recovery_end + 1_000_000,
            "recovery_prestop_dwell_start": recovery_end + 1_000_000_000,
            "recovery_prestop_dwell_complete": recovery_end + 11_000_000_000,
            "recovery_stop_requested": recovery_end + 12_000_000_000,
            "engine2_stopped": recovery_end + 13_000_000_000,
        }
        ordered_events = (
            "captures_ready",
            "endpoints_ready",
            "engine1_ready",
            "good_dwell_start",
            "good_dwell_complete",
            "good_start",
            "good_terminal",
            "prestop_dwell_start",
            "prestop_dwell_complete",
            "stop_requested",
            "engine1_stopped",
            "stopped_drain_start",
            "stopped_drain_complete",
            "down_start",
            "down_terminal",
            "engine2_ready",
            "recovery_dwell_start",
            "recovery_dwell_complete",
            "recovery_start",
            "recovery_terminal",
            "recovery_prestop_dwell_start",
            "recovery_prestop_dwell_complete",
            "recovery_stop_requested",
            "engine2_stopped",
        )
        if [points[event] for event in ordered_events] != sorted(points[event] for event in ordered_events):
            raise AssertionError("fixture lifecycle timestamps must be strictly ordered")
        lifecycle_records = []
        for sequence, event in enumerate(ordered_events, start=1):
            fields: dict[str, Any] = {}
            if event == "captures_ready":
                fields["capture_count"] = 5
            elif event == "endpoints_ready":
                fields["persistent_capture_count"] = 5
                fields["stable_process_count"] = 5
            elif event.endswith("dwell_start") or event.endswith("dwell_complete"):
                fields["duration_s"] = 10
            elif event in ("stopped_drain_start", "stopped_drain_complete"):
                fields["duration_s"] = 1
            lifecycle_records.append(
                {
                    "schema": "ams.m2.lifecycle/v1",
                    "run_id": RUN_ID,
                    "runtime_id": RUNTIME_ID,
                    "run_nonce": RUN_NONCE,
                    "event_seq": sequence,
                    "event": event,
                    "wall_utc": utc_for(5_000 + sequence),
                    "monotonic_ns": points[event],
                    **fields,
                }
            )
        write_jsonl(self.run_dir / "logs/m2_lifecycle.jsonl", lifecycle_records)

        role_names = (
            "launch",
            "sitl",
            "mavproxy",
            "adapter",
            "gcs_endpoint",
            "capture_tail",
            "capture_gcs",
            "capture_ns3_external_gcs",
            "capture_ns3_external_uav",
            "capture_uav",
        )
        roles = {
            role: {
                "expected_pid": 700 + index,
                "expected_start_ticks": 70_000 + index,
                "expected_cmdline_sha256": digest(f"monitor:{role}".encode("ascii")),
                "pid_present": True,
                "alive": True,
                "identity_match": True,
                "start_ticks": 70_000 + index,
                "cmdline_sha256": digest(f"monitor:{role}".encode("ascii")),
                "mismatches": [],
            }
            for index, role in enumerate(role_names, start=1)
        }
        monitor_records: list[dict[str, Any]] = []
        monitor_start = points["engine1_ready"] - 1_000_000_000
        monitor_records.append(
            {
                "schema": "ams.m2.monitor/v1",
                "schema_version": 1,
                "run_id": RUN_ID,
                "runtime_id": RUNTIME_ID,
                "run_nonce": RUN_NONCE,
                "event_seq": 1,
                "event": "monitor_start",
                "wall_utc": utc_for(6_000),
                "monotonic_ns": monitor_start,
            }
        )
        final_stop = points["engine2_stopped"] + 500_000_000
        sample_ns = monitor_start + 500_000_000
        while sample_ns < final_stop:
            sequence = len(monitor_records) + 1
            monitor_records.append(
                {
                    "schema": "ams.m2.monitor/v1",
                    "schema_version": 1,
                    "run_id": RUN_ID,
                    "runtime_id": RUNTIME_ID,
                    "run_nonce": RUN_NONCE,
                    "event_seq": sequence,
                    "event": "sample",
                    "wall_utc": utc_for(6_000 + sequence),
                    "monotonic_ns": sample_ns,
                    "roles": roles,
                    "all_roles_alive": True,
                    "topology": {
                        "exists": True,
                        "regular": True,
                        "matches_declared": True,
                    },
                }
            )
            sample_ns += 1_000_000_000
        sequence = len(monitor_records) + 1
        monitor_records.append(
            {
                "schema": "ams.m2.monitor/v1",
                "schema_version": 1,
                "run_id": RUN_ID,
                "runtime_id": RUNTIME_ID,
                "run_nonce": RUN_NONCE,
                "event_seq": sequence,
                "event": "monitor_stop",
                "wall_utc": utc_for(6_000 + sequence),
                "monotonic_ns": final_stop,
                "reason": "stop_file",
                "sample_count": len(monitor_records) - 1,
                "all_roles_alive": True,
            }
        )
        write_jsonl(self.run_dir / "logs/m2_monitor.jsonl", monitor_records)
        (self.run_dir / "logs/m2_monitor.stop").write_text("stop\n", encoding="utf-8")
        (self.run_dir / "logs/m2_lifecycle_monitor.stdout").write_bytes(b"")
        (self.run_dir / "logs/m2_lifecycle_monitor.stderr").write_bytes(b"")
        self.lifecycle_points = points

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
            lifecycle_relative = f"logs/ns3_{phase}.lifecycle.jsonl"
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
            if phase == "good":
                runner_ready = self.lifecycle_points["engine1_ready"]
                runner_stop = self.lifecycle_points["stop_requested"]
                runner_stopped = self.lifecycle_points["engine1_stopped"]
            else:
                runner_ready = self.lifecycle_points["engine2_ready"]
                runner_stop = self.lifecycle_points["recovery_stop_requested"]
                runner_stopped = self.lifecycle_points["engine2_stopped"]
            lifecycle_records = [
                {
                    "schema": "ams.ns3.lifecycle/v1",
                    "event": "ready",
                    "event_sequence": 1,
                    "event_epoch": epoch,
                    "config_sha256": config.sha256(),
                    "host_monotonic_ns": runner_ready - 1_000,
                    "sim_time_ns": 1_000_000,
                    "registered_queue_count": 2,
                },
                {
                    "schema": "ams.ns3.lifecycle/v1",
                    "event": "stop_observed",
                    "event_sequence": 2,
                    "event_epoch": epoch,
                    "config_sha256": config.sha256(),
                    "host_monotonic_ns": runner_stop + 1_000,
                    "sim_time_ns": 2_000_000,
                    "stop_reason": "stop_file",
                },
                {
                    "schema": "ams.ns3.lifecycle/v1",
                    "event": "queues_terminal",
                    "event_sequence": 3,
                    "event_epoch": epoch,
                    "config_sha256": config.sha256(),
                    "host_monotonic_ns": runner_stop + 2_000,
                    "sim_time_ns": 3_000_000,
                    "stop_reason": "stop_file",
                    "queues": [
                        {
                            "device_id": "radio-gcs",
                            "before_depths": {
                                "control_packets": 1,
                                "payload_packets": 0,
                                "additional_data_packets": 0,
                                "total_packets": 1,
                            },
                            "after_depths": {
                                "control_packets": 0,
                                "payload_packets": 0,
                                "additional_data_packets": 0,
                                "total_packets": 0,
                            },
                            "flushed_packets": 1,
                        },
                        {
                            "device_id": "radio-uav1",
                            "before_depths": {
                                "control_packets": 0,
                                "payload_packets": 0,
                                "additional_data_packets": 0,
                                "total_packets": 0,
                            },
                            "after_depths": {
                                "control_packets": 0,
                                "payload_packets": 0,
                                "additional_data_packets": 0,
                                "total_packets": 0,
                            },
                            "flushed_packets": 0,
                        },
                    ],
                    "all_queues_empty": True,
                },
                {
                    "schema": "ams.ns3.lifecycle/v1",
                    "event": "stopped",
                    "event_sequence": 4,
                    "event_epoch": epoch,
                    "config_sha256": config.sha256(),
                    "host_monotonic_ns": runner_stop + 3_000,
                    "sim_time_ns": 4_000_000,
                    "stop_reason": "stop_file",
                },
            ]
            if lifecycle_records[-1]["host_monotonic_ns"] > runner_stopped:
                raise AssertionError("fixture runner stop must follow C++ lifecycle terminal event")
            write_jsonl(self.run_dir / lifecycle_relative, lifecycle_records)
            config_hashes[phase] = config.sha256()
            phase_identities[phase] = {
                "event_epoch": epoch,
                "config_sha256": config.sha256(),
                "config": self._raw_record(config_relative),
                "events": self._raw_record(events_relative),
                "argv": self._raw_record(argv_relative),
                "ready": self._raw_record(ready_relative),
                "stop": self._raw_record(stop_relative),
                "lifecycle": self._raw_record(lifecycle_relative),
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
            "network/scripts/m2_lifecycle_event.py",
            "network/scripts/m2_lifecycle_monitor.py",
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
    def make_fixture(
        self,
        *,
        down_ack: bool = False,
        positive_heartbeat_count: int = 3,
    ) -> tuple[tempfile.TemporaryDirectory[str], Path, FixtureBuilder]:
        temporary = tempfile.TemporaryDirectory()
        run_dir = Path(temporary.name) / RUN_ID
        builder = FixtureBuilder(
            run_dir,
            down_ack=down_ack,
            positive_heartbeat_count=positive_heartbeat_count,
        )
        builder.build()
        return temporary, run_dir, builder

    def move_adapter_forward_before_phase_start(
        self,
        run_dir: Path,
        builder: FixtureBuilder,
        *,
        phase: str,
        payload: bytes,
    ) -> None:
        """Make one otherwise valid uplink forward pre-window but still causal."""
        path = run_dir / "logs/uav_adapter.jsonl"
        records = read_jsonl(path)
        payload_hash = digest(payload)
        target = next(
            record
            for record in records
            if record.get("event") == "forward"
            and record.get("direction") == "tail_to_gcs"
            and record.get("sha256") == payload_hash
        )
        target["monotonic_ns"] = builder.phase_payloads[phase]["window"][0] - 1
        target["received_monotonic_ns"] = target["monotonic_ns"] - 300
        target["send_start_monotonic_ns"] = target["monotonic_ns"] - 200
        target["send_complete_monotonic_ns"] = target["monotonic_ns"] - 100
        resequence_adapter(records)
        write_jsonl(path, records)

    def move_adapter_forward_before_attempt(
        self,
        run_dir: Path,
        builder: FixtureBuilder,
        *,
        phase: str,
        attempt: int,
        payload: bytes,
    ) -> None:
        """Keep a forward in-window but make it precede its transaction."""
        path = run_dir / "logs/uav_adapter.jsonl"
        records = read_jsonl(path)
        payload_hash = digest(payload)
        target = next(
            record
            for record in records
            if record.get("event") == "forward"
            and record.get("direction") == "tail_to_gcs"
            and record.get("sha256") == payload_hash
        )
        attempt_record = next(
            record
            for record in builder.probe_records
            if record.get("phase") == phase
            and record.get("event") == "command_attempt"
            and record.get("attempt") == attempt
        )
        target["monotonic_ns"] = attempt_record["monotonic_ns"] - 1
        resequence_adapter(records)
        write_jsonl(path, records)

    def move_adapter_forward_to_phase_end(
        self,
        run_dir: Path,
        builder: FixtureBuilder,
        *,
        phase: str,
        payload: bytes,
    ) -> None:
        """Place a forward on the exclusive interval end boundary."""
        path = run_dir / "logs/uav_adapter.jsonl"
        records = read_jsonl(path)
        payload_hash = digest(payload)
        target = next(
            record
            for record in records
            if record.get("event") == "forward"
            and record.get("sha256") == payload_hash
        )
        target["monotonic_ns"] = builder.phase_payloads[phase]["window"][1]
        resequence_adapter(records)
        write_jsonl(path, records)

    def duplicate_adapter_forward_before_phase_start(
        self,
        run_dir: Path,
        builder: FixtureBuilder,
        *,
        phase: str,
        payload: bytes,
    ) -> None:
        """Create an otherwise indistinguishable stale/fresh occurrence pair."""
        path = run_dir / "logs/uav_adapter.jsonl"
        records = read_jsonl(path)
        payload_hash = digest(payload)
        original = next(
            record
            for record in records
            if record.get("event") == "forward"
            and record.get("direction") == "tail_to_gcs"
            and record.get("sha256") == payload_hash
        )
        stale = dict(original)
        stale["monotonic_ns"] = builder.phase_payloads[phase]["window"][0] - 1
        records.append(stale)
        refresh_adapter_stop_counters(records)
        resequence_adapter(records)
        write_jsonl(path, records)

    def remove_adapter_forward(
        self,
        run_dir: Path,
        *,
        payload: bytes,
    ) -> None:
        """Remove precisely one forwarded response while retaining truthful counters."""
        path = run_dir / "logs/uav_adapter.jsonl"
        payload_hash = digest(payload)
        records = read_jsonl(path)
        removed = False
        retained: list[dict[str, Any]] = []
        for record in records:
            if (
                not removed
                and record.get("event") == "forward"
                and record.get("direction") == "tail_to_gcs"
                and record.get("sha256") == payload_hash
            ):
                removed = True
                continue
            retained.append(record)
        if not removed:
            raise AssertionError(f"fixture adapter forward for {payload_hash} was not found")
        refresh_adapter_stop_counters(retained)
        resequence_adapter(retained)
        write_jsonl(path, retained)

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
        self.assertIn('neigh replace 10.71.0.1', setup)
        self.assertIn('lladdr 02:71:00:00:00:01 nud permanent dev eth0', setup)
        self.assertIn('neigh replace 10.71.1.1', setup)
        self.assertIn('lladdr 02:71:01:00:00:01 nud permanent dev eth0', setup)
        self.assertIn('neigh show to 10.71.0.1 dev eth0', setup)
        self.assertIn('neigh show to 10.71.1.1 dev eth0', setup)
        self.assertLess(
            setup.index('link set eth0 up'),
            setup.index('neigh replace 10.71.0.1'),
        )
        self.assertLess(
            setup.index('neigh replace 10.71.0.1'),
            setup.index('route add default via 10.71.0.1'),
        )
        self.assertIn("start_persistent_captures", runner)
        self.assertIn('"$RUN_DIR/pcap/ns3_external_ingress.pcap"', runner)
        self.assertIn('"$RUN_DIR/pcap/ns3_external_egress.pcap"', runner)
        self.assertNotIn("ns3_external_ingress_${phase}.pcap", runner)
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
            capture_stats = json.loads(
                (run_dir / "logs/capture_tail_stats.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                capture_stats["contract"], raw_packet_capture.STATS_CONTRACT
            )
            self.assertEqual(
                capture_stats["capture_protocol"],
                raw_packet_capture.CAPTURE_PROTOCOL,
            )
            self.assertEqual(
                capture_stats["packet_filter"], raw_packet_capture.PACKET_FILTER
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

    def test_cross_phase_command_hash_reuse_fails_after_reseal(self) -> None:
        temporary, run_dir, builder = self.make_fixture()
        with temporary:
            path = run_dir / "logs/m2_probe_events.jsonl"
            records = read_jsonl(path)
            down_attempt = next(
                record
                for record in records
                if record.get("phase") == "down"
                and record.get("event") == "command_attempt"
                and record.get("attempt") == 1
            )
            recovery_attempt = next(
                record
                for record in records
                if record.get("phase") == "recovery"
                and record.get("event") == "command_attempt"
                and record.get("attempt") == 1
            )
            down_attempt["command_sha256"] = recovery_attempt["command_sha256"]
            write_jsonl(path, records)
            builder.seal()
            result = evaluate_m2_vertical_slice(run_dir)
            self.assertFalse(result["passed"])
            self.assertEqual(result["gates"]["manifest"]["status"], "passed")
            self.assertIn(
                "command frame hashes are not globally unique across M2 phases",
                "\n".join(result["gates"]["probe_transactions"]["failures"]),
            )

    def test_prewindow_heartbeat_with_three_fresh_forwards_passes(self) -> None:
        temporary, run_dir, builder = self.make_fixture(positive_heartbeat_count=4)
        with temporary:
            stale_heartbeat = builder.phase_payloads["recovery"]["heartbeats"][0]
            self.move_adapter_forward_before_phase_start(
                run_dir,
                builder,
                phase="recovery",
                payload=stale_heartbeat,
            )
            builder.seal()
            result = evaluate_m2_vertical_slice(run_dir)
            self.assertTrue(result["passed"], result)
            self.assertEqual(
                result["gates"]["adapter_path"]["details"]["heartbeat_forwards"][
                    "recovery"
                ],
                {"observed": 4, "causal": 4, "fresh": 3, "stale": 1},
            )

    def test_stale_heartbeat_does_not_satisfy_fresh_minimum(self) -> None:
        temporary, run_dir, builder = self.make_fixture()
        with temporary:
            stale_heartbeat = builder.phase_payloads["recovery"]["heartbeats"][0]
            self.move_adapter_forward_before_phase_start(
                run_dir,
                builder,
                phase="recovery",
                payload=stale_heartbeat,
            )
            builder.seal()
            result = evaluate_m2_vertical_slice(run_dir)
            self.assertFalse(result["passed"])
            adapter = result["gates"]["adapter_path"]
            self.assertEqual(adapter["status"], "failed")
            self.assertIn(
                "adapter/recovery: fresh heartbeat forwards 2, expected at least 3",
                "\n".join(adapter["failures"]),
            )
            self.assertEqual(
                adapter["details"]["heartbeat_forwards"]["recovery"],
                {"observed": 3, "causal": 3, "fresh": 2, "stale": 1},
            )

    def test_missing_raw_marker_send_fails_even_when_legacy_hashes_remain(self) -> None:
        temporary, run_dir, _builder = self.make_fixture()
        with temporary:
            path = run_dir / "logs/m2_probe_events.jsonl"
            records = read_jsonl(path)
            removed = False
            retained: list[dict[str, Any]] = []
            for record in records:
                if (
                    not removed
                    and record.get("phase") == "good"
                    and record.get("event") == "datagram_tx"
                    and record.get("attempt") == 1
                    and record.get("leg") == "marker"
                ):
                    removed = True
                    continue
                retained.append(record)
            self.assertTrue(removed)
            resequence(retained)
            write_jsonl(path, retained)
            FixtureBuilder(run_dir).seal()
            result = evaluate_m2_vertical_slice(run_dir)
            self.assertFalse(result["passed"])
            self.assertIn(
                "marker/command raw sends are incomplete",
                "\n".join(result["gates"]["probe_transactions"]["failures"]),
            )

    def test_decoded_ack_cannot_reference_a_forged_raw_rx_sequence(self) -> None:
        temporary, run_dir, _builder = self.make_fixture()
        with temporary:
            path = run_dir / "logs/m2_probe_events.jsonl"
            records = read_jsonl(path)
            ack = next(
                record
                for record in records
                if record.get("phase") == "recovery"
                and record.get("event") == "command_ack"
                and record.get("attempt") == 1
            )
            ack["rx_datagram_seq"] = 999
            write_jsonl(path, records)
            FixtureBuilder(run_dir).seal()
            result = evaluate_m2_vertical_slice(run_dir)
            self.assertFalse(result["passed"])
            self.assertIn(
                "does not reference one raw datagram_rx occurrence",
                "\n".join(result["gates"]["probe_transactions"]["failures"]),
            )

    def test_adapter_occurrence_timestamps_cannot_postdate_durable_forward(self) -> None:
        temporary, run_dir, _builder = self.make_fixture()
        with temporary:
            path = run_dir / "logs/uav_adapter.jsonl"
            records = read_jsonl(path)
            forward = next(record for record in records if record.get("event") == "forward")
            forward["send_complete_monotonic_ns"] = forward["monotonic_ns"] + 1
            write_jsonl(path, records)
            FixtureBuilder(run_dir).seal()
            result = evaluate_m2_vertical_slice(run_dir)
            self.assertFalse(result["passed"])
            self.assertIn(
                "receive/send/durable timestamps are not ordered",
                "\n".join(result["gates"]["adapter_path"]["failures"]),
            )

    def test_heartbeat_requires_causal_adapter_forward(self) -> None:
        temporary, run_dir, builder = self.make_fixture()
        with temporary:
            missing_heartbeat = builder.phase_payloads["recovery"]["heartbeats"][0]
            self.remove_adapter_forward(run_dir, payload=missing_heartbeat)
            builder.seal()
            result = evaluate_m2_vertical_slice(run_dir)
            self.assertFalse(result["passed"])
            adapter = result["gates"]["adapter_path"]
            self.assertEqual(adapter["status"], "failed")
            self.assertIn(
                "adapter/recovery: heartbeat payload "
                f"{digest(missing_heartbeat)} has no causally prior tail_to_gcs forward",
                "\n".join(adapter["failures"]),
            )

    def test_stale_command_response_is_not_accepted_outside_phase(self) -> None:
        temporary, run_dir, builder = self.make_fixture()
        with temporary:
            stale_ack = builder.phase_payloads["recovery"]["responses"][0]
            self.assertTrue(stale_ack.startswith(b"MAVLINK2-COMMAND_ACK:"))
            self.move_adapter_forward_before_phase_start(
                run_dir,
                builder,
                phase="recovery",
                payload=stale_ack,
            )
            builder.seal()
            result = evaluate_m2_vertical_slice(run_dir)
            self.assertFalse(result["passed"])
            adapter = result["gates"]["adapter_path"]
            self.assertEqual(adapter["status"], "failed")
            self.assertIn(
                "adapter/recovery: response payload "
                f"{digest(stale_ack)} forwarded 0 time(s), expected 1",
                "\n".join(adapter["failures"]),
            )

    def test_response_forward_before_its_command_is_not_accepted(self) -> None:
        temporary, run_dir, builder = self.make_fixture()
        with temporary:
            stale_ack = builder.phase_payloads["recovery"]["responses"][0]
            self.move_adapter_forward_before_attempt(
                run_dir,
                builder,
                phase="recovery",
                attempt=1,
                payload=stale_ack,
            )
            builder.seal()
            result = evaluate_m2_vertical_slice(run_dir)
            self.assertFalse(result["passed"])
            adapter = result["gates"]["adapter_path"]
            self.assertEqual(adapter["status"], "failed")
            self.assertIn(
                "adapter/recovery/1: response payload "
                f"{digest(stale_ack)} has no forward between command_attempt and probe receive",
                "\n".join(adapter["failures"]),
            )

    def test_phase_end_forward_is_outside_half_open_window(self) -> None:
        temporary, run_dir, builder = self.make_fixture()
        with temporary:
            boundary_ack = builder.phase_payloads["good"]["responses"][0]
            self.move_adapter_forward_to_phase_end(
                run_dir,
                builder,
                phase="good",
                payload=boundary_ack,
            )
            builder.seal()
            result = evaluate_m2_vertical_slice(run_dir)
            self.assertFalse(result["passed"])
            adapter = result["gates"]["adapter_path"]
            self.assertEqual(adapter["status"], "failed")
            self.assertIn(
                "adapter/good: response payload "
                f"{digest(boundary_ack)} forwarded 0 time(s), expected 1",
                "\n".join(adapter["failures"]),
            )

    def test_ambiguous_stale_and_fresh_heartbeat_occurrences_fail_closed(self) -> None:
        temporary, run_dir, builder = self.make_fixture()
        with temporary:
            heartbeat = builder.phase_payloads["recovery"]["heartbeats"][0]
            self.duplicate_adapter_forward_before_phase_start(
                run_dir,
                builder,
                phase="recovery",
                payload=heartbeat,
            )
            builder.seal()
            result = evaluate_m2_vertical_slice(run_dir)
            self.assertFalse(result["passed"])
            adapter = result["gates"]["adapter_path"]
            self.assertEqual(adapter["status"], "failed")
            self.assertIn(
                "adapter/recovery: heartbeat payload "
                f"{digest(heartbeat)} has ambiguous stale/fresh adapter-forward occurrences",
                "\n".join(adapter["failures"]),
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

    def test_packet_engine_terminal_queue_must_be_zero_after_reseal(self) -> None:
        temporary, run_dir, builder = self.make_fixture()
        with temporary:
            lifecycle_path = run_dir / "logs/ns3_good.lifecycle.jsonl"
            records = read_jsonl(lifecycle_path)
            records[2]["queues"][0]["after_depths"]["control_packets"] = 1
            records[2]["queues"][0]["after_depths"]["total_packets"] = 1
            write_jsonl(lifecycle_path, records)
            metadata_path = run_dir / "metrics/m2_run.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["packet_engine"]["phases"]["good"]["lifecycle"] = (
                builder._raw_record("logs/ns3_good.lifecycle.jsonl")
            )
            metadata_path.write_text(
                json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            builder.seal()
            result = evaluate_m2_vertical_slice(run_dir)
            self.assertFalse(result["passed"])
            self.assertEqual(result["gates"]["manifest"]["status"], "passed")
            self.assertEqual(result["gates"]["packet_engine"]["status"], "passed")
            self.assertIn(
                "after_depths are not terminal zeroes",
                "\n".join(result["gates"]["packet_engine_lifecycle"]["failures"]),
            )

    def test_capture_kernel_drop_counter_fails_after_reseal(self) -> None:
        temporary, run_dir, builder = self.make_fixture()
        with temporary:
            stats_path = run_dir / "logs/capture_gcs_stats.json"
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

    def test_persistent_capture_gap_before_good_fails_after_reseal(self) -> None:
        temporary, run_dir, builder = self.make_fixture()
        with temporary:
            stats_path = run_dir / "logs/capture_ns3_external_gcs_stats.json"
            stats = json.loads(stats_path.read_text(encoding="utf-8"))
            stats["started_monotonic_ns"] = builder.phase_payloads["good"][
                "window"
            ][0]
            stats_path.write_text(
                json.dumps(stats, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            builder.seal()
            result = evaluate_m2_vertical_slice(run_dir)
            self.assertFalse(result["passed"])
            self.assertEqual(result["gates"]["manifest"]["status"], "passed")
            failures = "\n".join(result["gates"]["capture_accounting"]["failures"])
            self.assertIn("does not start before the good phase", failures)

    def test_lifecycle_short_prestop_dwell_fails_after_reseal(self) -> None:
        temporary, run_dir, builder = self.make_fixture()
        with temporary:
            path = run_dir / "logs/m2_lifecycle.jsonl"
            records = read_jsonl(path)
            by_event = {record["event"]: record for record in records}
            by_event["prestop_dwell_complete"]["monotonic_ns"] = (
                by_event["prestop_dwell_start"]["monotonic_ns"] + 9_999_999_999
            )
            write_jsonl(path, records)
            builder.seal()
            result = evaluate_m2_vertical_slice(run_dir)
            self.assertFalse(result["passed"])
            self.assertEqual(result["gates"]["manifest"]["status"], "passed")
            self.assertIn(
                "prestop_dwell_start->prestop_dwell_complete is shorter",
                "\n".join(result["gates"]["lifecycle"]["failures"]),
            )

    def test_lifecycle_monitor_sample_gap_fails_after_reseal(self) -> None:
        temporary, run_dir, builder = self.make_fixture()
        with temporary:
            path = run_dir / "logs/m2_monitor.jsonl"
            records = read_jsonl(path)
            removed = False
            seen_samples = 0
            retained = []
            for record in records:
                if record.get("event") == "sample":
                    seen_samples += 1
                    if seen_samples == 2:
                        removed = True
                        continue
                retained.append(record)
            self.assertTrue(removed)
            for sequence, record in enumerate(retained, start=1):
                record["event_seq"] = sequence
                record["wall_utc"] = utc_for(6_000 + sequence)
            write_jsonl(path, retained)
            builder.seal()
            result = evaluate_m2_vertical_slice(run_dir)
            self.assertFalse(result["passed"])
            self.assertEqual(result["gates"]["manifest"]["status"], "passed")
            self.assertIn(
                "gap exceeds 1.5 seconds",
                "\n".join(result["gates"]["lifecycle_monitor"]["failures"]),
            )

    def test_phase_local_persistent_capture_filename_is_rejected(self) -> None:
        temporary, run_dir, builder = self.make_fixture()
        with temporary:
            shutil.copyfile(
                run_dir / "pcap/gcs_ingress.pcap",
                run_dir / "pcap/gcs_ingress_good.pcap",
            )
            builder.seal()
            result = evaluate_m2_vertical_slice(run_dir)
            self.assertFalse(result["passed"])
            self.assertEqual(result["gates"]["manifest"]["status"], "failed")
            self.assertIn(
                "phase-local persistent capture filenames are forbidden",
                "\n".join(result["gates"]["manifest"]["failures"]),
            )

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

    def test_persistent_endpoint_generation_restart_fails_after_reseal(self) -> None:
        temporary, run_dir, builder = self.make_fixture()
        with temporary:
            path = run_dir / "logs/m2_probe_events.jsonl"
            records = read_jsonl(path)
            for record in records:
                if record.get("phase") == "recovery":
                    record["endpoint_generation"] = 2
            write_jsonl(path, records)
            builder.seal()
            result = evaluate_m2_vertical_slice(run_dir)
            self.assertFalse(result["passed"])
            self.assertEqual(result["gates"]["manifest"]["status"], "passed")
            self.assertIn(
                "generation must remain exactly one",
                "\n".join(result["gates"]["probe_transactions"]["failures"]),
            )

    def test_persistent_endpoint_raw_sequence_reset_fails_after_reseal(self) -> None:
        temporary, run_dir, builder = self.make_fixture()
        with temporary:
            path = run_dir / "logs/m2_probe_events.jsonl"
            records = read_jsonl(path)
            first_recovery_tx = next(
                record
                for record in records
                if record.get("phase") == "recovery" and record.get("event") == "datagram_tx"
            )
            first_recovery_tx["tx_datagram_seq"] = 1
            write_jsonl(path, records)
            builder.seal()
            result = evaluate_m2_vertical_slice(run_dir)
            self.assertFalse(result["passed"])
            self.assertEqual(result["gates"]["manifest"]["status"], "passed")
            self.assertIn(
                "TX occurrence sequence is not globally contiguous from one",
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
                run_dir / "pcap/uav_egress.pcap",
                builder.persistent_positive_payloads()
                + builder.phase_payloads["down"]["requests"],
                timestamp_seed=101,
            )
            builder.write_capture_stats()
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
                run_dir / "pcap/ns3_external_ingress.pcap",
                builder.persistent_positive_payloads(),
                timestamp_seed=101,
            )
            builder.write_capture_stats()
            builder.seal()
            result = evaluate_m2_vertical_slice(run_dir)
            self.assertFalse(result["passed"])
            failures = "\n".join(result["gates"]["packet_captures"]["failures"])
            self.assertIn("ns3_external_ingress.pcap", failures)
            self.assertIn("down-attempt payload", failures)

    def test_copied_capture_point_pcap_fails(self) -> None:
        temporary, run_dir, builder = self.make_fixture()
        with temporary:
            shutil.copyfile(
                run_dir / "pcap/gcs_ingress.pcap",
                run_dir / "pcap/ns3_external_ingress.pcap",
            )
            builder.write_capture_stats()
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
            with (run_dir / "pcap/gcs_ingress.pcap").open("ab") as handle:
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
