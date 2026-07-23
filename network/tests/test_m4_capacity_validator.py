#!/usr/bin/env python3
"""Adversarial tests for the formal M4 consumer of the frozen Q3 API."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from network.scripts.collect_m4_runtime import classify_process  # noqa: E402
from network.scripts.m4_runtime_orchestrator import (  # noqa: E402
    provider_package_versions,
)
from network.validation.m4_common import (  # noqa: E402
    M4ValidationError,
    validate_wire_log,
)
from network.validation.m4_runtime import (  # noqa: E402
    CAPTURE_OCCURRENCE_MATCH_TOLERANCE_NS,
    CLOCK_PRODUCER_PROCESS_ROLES,
    FROZEN_BUNDLE_ID,
    FROZEN_BUNDLE_PATH,
    MANDATORY_CAPTURE_ROLES,
    REQUIRED_CLOCK_PRODUCERS,
    REQUIRED_PROCESS_COUNTS,
    _consume_capture_role_occurrences,
    _consume_ordered_occurrence,
    validate_clock_process_binding,
    validate_continuous_readiness_schedule,
    validate_external_captures,
)
from network.validation.validate_m4_capacity import (  # noqa: E402
    ADAPTER_SCRIPT_PATH,
    ACTUAL_CONTROL_API_CONTRACT,
    PROVIDER_SCRIPT_PATH,
    REQUIRED_SOURCE_PATHS,
    _accepted_m3_actual_control_api,
    _actual_control_event_audit,
    _exact_wire_occurrences,
    _expected_adapter_cmdline_sha256,
    _expected_actual_control_api,
    _expected_provider_cmdline_sha256,
    _runtime_process_samples,
    _tail_capture_evidence,
    _tail_topology_evidence,
    _validate_real_provider_wire_binding,
)
from network.validation.validate_m3_external_matrix import (  # noqa: E402
    m3_actual_control_api,
)
from network.scripts import actual_sitl_control_probe as control_probe  # noqa: E402
from network.scripts import m4_capacity_airborne as airborne  # noqa: E402
from network.scripts import raw_packet_capture  # noqa: E402


def canonical_bytes(value: object) -> bytes:
    return (json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def write_indexed_wire_fixture(
    directory: Path,
    records: list[tuple[str, str, dict[str, object], int]],
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    data = bytearray()
    index: list[dict[str, object]] = []
    for direction, connection_id, message, monotonic_ns in records:
        raw = canonical_bytes(message)
        index.append(
            {
                "connection_id": connection_id,
                "direction": direction,
                "length": len(raw),
                "monotonic_ns": monotonic_ns,
                "offset": len(data),
                "sha256": sha256_bytes(raw),
            }
        )
        data.extend(raw)
    (directory / "sionna_async_wire.bin").write_bytes(bytes(data))
    (directory / "sionna_async_wire_index.jsonl").write_bytes(
        b"".join(canonical_bytes(record) for record in index)
    )


def write_capture_stats_v2_fixture(
    run_dir: Path,
    *,
    name: str,
    interface: str,
    setter: str = "SO_RCVBUF",
    packet_count: int = 1,
) -> None:
    pcap = run_dir / f"pcap/{name}.pcap"
    pcap.parent.mkdir(parents=True, exist_ok=True)
    pcap.write_bytes(b"fixture-pcap")
    logs = run_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / f"capture-{name}.json").write_bytes(
        canonical_bytes(
            {
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
                "receive_buffer_setter": setter,
                "drain_batch_packet_limit": (
                    raw_packet_capture.DRAIN_BATCH_PACKET_LIMIT
                ),
                "drain_batch_byte_limit": (
                    raw_packet_capture.DRAIN_BATCH_BYTE_LIMIT
                ),
                "started_monotonic_ns": 1_000_000_000,
                "stopped_monotonic_ns": 4_000_000_000,
                "stop_signal": "SIGINT",
                "packets_written": packet_count,
                "packets_received_kernel": packet_count,
                "packets_dropped_kernel": 0,
            }
        )
    )
    (logs / f"capture-{name}.stderr").write_bytes(b"")


class RealProviderWireBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.temporary.name).resolve()
        (self.run_dir / "logs/provider_wire").mkdir(parents=True)
        (self.run_dir / "raw/state").mkdir(parents=True)
        (self.run_dir / "raw/control").mkdir(parents=True)
        self.run_id = "m4-provider-binding-test"
        self.runtime_id = "7" * 32
        self.provider_pid = 4321
        self.adapter_pid = 4322
        self.provider_port = 5090
        bundle = json.loads(FROZEN_BUNDLE_PATH.read_text(encoding="utf-8"))
        provider_script = ROOT / PROVIDER_SCRIPT_PATH
        adapter_script = ROOT / ADAPTER_SCRIPT_PATH
        self.run: dict[str, object] = {
            "run_id": self.run_id,
            "runtime_id": self.runtime_id,
            "profile": "m4_capacity_prerequisite",
            "async_policy": {"query_period_ms": 1000},
            "bundle": {
                "bundle_id": FROZEN_BUNDLE_ID,
                "bundle_sha256": bundle["bundle_sha256"],
            },
            "limits": {"max_message_bytes": 1_048_576},
            "identity": {
                "executable_manifest": {
                    "python": {
                        "path": "/usr/bin/python3.10",
                        "sha256": "8" * 64,
                        "size_bytes": 1,
                    }
                }
            },
            "source_sha256": {
                PROVIDER_SCRIPT_PATH: sha256_bytes(provider_script.read_bytes()),
                ADAPTER_SCRIPT_PATH: sha256_bytes(adapter_script.read_bytes()),
            },
        }
        contract_path = self.run_dir / "raw/m4_capacity_contract.json"
        contract_path.write_bytes(canonical_bytes(self.run))
        self.contract_hash = sha256_bytes(contract_path.read_bytes())
        config_material = {
            "async_policy": self.run["async_policy"],
            "bundle": self.run["bundle"],
            "limits": self.run["limits"],
            "profile": self.run["profile"],
            "radio_sha256": sha256_bytes(
                (ROOT / "network/config/radio_m4_canonical.yaml").read_bytes()
            ),
            "effects_sha256": sha256_bytes(
                (ROOT / "network/config/sionna_packet_effects_v1.json").read_bytes()
            ),
        }
        self.config_hash = sha256_bytes(canonical_bytes(config_material))
        self.scene_identity = {
            "bundle_id": FROZEN_BUNDLE_ID,
            "scene_manifest_sha256": bundle["bundle_sha256"],
            "scene_path": str((ROOT / bundle["sionna_scene_xml"]).resolve()),
        }
        self.provider_executable = {
            "path": str(provider_script.resolve()),
            "sha256": sha256_bytes(provider_script.read_bytes()),
        }
        self.adapter_executable = {
            "path": str(adapter_script.resolve()),
            "sha256": sha256_bytes(adapter_script.read_bytes()),
        }
        self.provider_identity = {
            "provider_id": "sionna-rt-cuda-m4",
            "provider_mode": "real_sionna",
            "acceptance_eligible": True,
            "sionna_rt_version": "1.2.2",
            "mitsuba_version": "3.8.0",
        }
        self.messages = self._messages()
        self._publish_wire()
        (self.run_dir / "raw/state/provider.ready.json").write_bytes(
            canonical_bytes(
                {
                    "pid": self.provider_pid,
                    "port": self.provider_port,
                    "monotonic_ns": 1_000,
                    "provider_mode": "real_sionna",
                    "bundle_sha256": bundle["bundle_sha256"],
                    "run_id": self.run_id,
                }
            )
        )
        (self.run_dir / "raw/state/adapter.ready.json").write_bytes(
            canonical_bytes(
                {
                    "pid": self.adapter_pid,
                    "monotonic_ns": 1_100,
                    "run_id": self.run_id,
                    "runtime_id": self.runtime_id,
                    "provider_mode": "real_sionna",
                    "pose_entities": [
                        "cp",
                        "uav1",
                        "uav2",
                        "uav3",
                        "uav4",
                        "uav5",
                        "jammer_m4",
                    ],
                }
            )
        )
        provider_process = {
            "pid": self.provider_pid,
            "start_ticks": 900,
            "pgid": self.provider_pid,
            "role": "sionna_worker",
            "state": "S",
            "executable_path": "/usr/bin/python3.10",
            "executable_sha256": "8" * 64,
            "cmdline_sha256": _expected_provider_cmdline_sha256(
                self.run_dir,
                port=self.provider_port,
                runtime_id=self.runtime_id,
            ),
        }
        adapter_process = {
            "pid": self.adapter_pid,
            "start_ticks": 901,
            "pgid": self.adapter_pid,
            "role": "sionna_adapter",
            "state": "S",
            "executable_path": "/usr/bin/python3.10",
            "executable_sha256": "8" * 64,
            "cmdline_sha256": _expected_adapter_cmdline_sha256(
                self.run_dir,
                port=self.provider_port,
                runtime_id=self.runtime_id,
            ),
        }
        (self.run_dir / "logs/m4_runtime_events.jsonl").write_bytes(
            canonical_bytes(
                {
                    "event": "measurement_resource_sample",
                    "processes": {
                        "processes": [provider_process, adapter_process]
                    },
                }
            )
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _common(
        self, message_type: str, sequence: int, sender_id: str, emitted_ns: int
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "message_type": message_type,
            "wire_sequence": sequence,
            "sender_id": sender_id,
            "run_id": self.run_id,
            "profile": self.run["profile"],
            "phase_id": "m4_continuous_runtime",
            "contract_hash": self.contract_hash,
            "config_hash": self.config_hash,
            "bundle_id": FROZEN_BUNDLE_ID,
            "reconnect_generation": 0,
            "sender_clock_domain": "host-monotonic",
            "emitted_monotonic_ns": emitted_ns,
        }

    def _handshake(
        self,
        message_type: str,
        sequence: int,
        sender_id: str,
        role: str,
        executable: dict[str, object],
        emitted_ns: int,
    ) -> dict[str, object]:
        message = self._common(message_type, sequence, sender_id, emitted_ns)
        message.update(
            {
                "protocol_name": "sionna_async",
                "protocol_version": 1,
                "sender_role": role,
                "executable_identity": executable,
                "accepted_run_id": self.run_id,
                "accepted_config_hash": self.config_hash,
                "accepted_bundle_id": FROZEN_BUNDLE_ID,
                "readiness_state": (
                    "initializing" if message_type == "hello" else "ready"
                ),
            }
        )
        if role == "provider":
            message["provider_identity"] = copy.deepcopy(self.provider_identity)
        if message_type == "ready":
            message["scene_identity"] = copy.deepcopy(self.scene_identity)
        return message

    def _messages(self) -> dict[str, dict[str, object]]:
        messages = {
            "provider_hello": self._handshake(
                "hello",
                1,
                "sionna-provider-m4",
                "provider",
                copy.deepcopy(self.provider_executable),
                1_010,
            ),
            "provider_ready": self._handshake(
                "ready",
                2,
                "sionna-provider-m4",
                "provider",
                copy.deepcopy(self.provider_executable),
                1_020,
            ),
            "adapter_hello": self._handshake(
                "hello",
                1,
                "sionna-adapter-m4",
                "adapter",
                copy.deepcopy(self.adapter_executable),
                1_030,
            ),
            "adapter_ready": self._handshake(
                "ready",
                2,
                "sionna-adapter-m4",
                "adapter",
                copy.deepcopy(self.adapter_executable),
                1_040,
            ),
            "query": self._common(
                "query", 3, "sionna-adapter-m4", 1_050
            ),
            "result": self._common(
                "result", 3, "sionna-provider-m4", 1_060
            ),
        }
        messages["query"]["query_id"] = "query-fixture-1"
        messages["result"]["query_id"] = "query-fixture-1"
        return messages

    def _publish_wire(
        self,
        *,
        duplicate_client_query: bool = False,
        duplicate_provider_query: bool = False,
        omit_provider_query: bool = False,
        reorder_provider_inbound: bool = False,
    ) -> None:
        client_records = [
            ("inbound", "adapter-0-fixture", self.messages["provider_hello"], 1_011),
            ("inbound", "adapter-0-fixture", self.messages["provider_ready"], 1_021),
            ("outbound", "adapter-0-fixture", self.messages["adapter_hello"], 1_031),
            ("outbound", "adapter-0-fixture", self.messages["adapter_ready"], 1_041),
            ("outbound", "adapter-0-fixture", self.messages["query"], 1_051),
        ]
        if duplicate_client_query:
            client_records.append(
                ("outbound", "adapter-0-fixture", self.messages["query"], 1_052)
            )
        client_records.append(
            ("inbound", "adapter-0-fixture", self.messages["result"], 1_061)
        )
        provider_records = [
            ("outbound", "conn-0-fixture", self.messages["provider_hello"], 1_010),
            ("outbound", "conn-0-fixture", self.messages["provider_ready"], 1_020),
            ("inbound", "conn-0-fixture", self.messages["adapter_hello"], 1_032),
            ("inbound", "conn-0-fixture", self.messages["adapter_ready"], 1_042),
        ]
        if not omit_provider_query:
            provider_records.append(
                ("inbound", "conn-0-fixture", self.messages["query"], 1_053)
            )
            if duplicate_provider_query:
                provider_records.append(
                    ("inbound", "conn-0-fixture", self.messages["query"], 1_054)
                )
        if reorder_provider_inbound:
            provider_records[2], provider_records[3] = (
                provider_records[3],
                provider_records[2],
            )
        provider_records.append(
            ("outbound", "conn-0-fixture", self.messages["result"], 1_060)
        )
        write_indexed_wire_fixture(self.run_dir / "logs", client_records)
        write_indexed_wire_fixture(
            self.run_dir / "logs/provider_wire", provider_records
        )

    def _validate(self) -> tuple[dict[str, object], list[str]]:
        versions = {"sionna-rt": "1.2.2", "mitsuba": "3.8.0"}
        with (
            mock.patch(
                "network.validation.validate_m4_capacity.decode_message",
                side_effect=lambda raw, max_bytes=None: json.loads(raw.decode("utf-8")),
            ),
            mock.patch(
                "network.validation.validate_m4_capacity.importlib.metadata.version",
                side_effect=lambda name: versions[name],
            ),
        ):
            return _validate_real_provider_wire_binding(
                self.run_dir, self.run, {}
            )

    def test_exact_two_sided_wire_and_process_binding_passes(self) -> None:
        details, failures = self._validate()
        self.assertEqual(failures, [])
        self.assertEqual(details["reconnect_generations"], [0])
        self.assertEqual(details["provider_pid"], self.provider_pid)
        self.assertEqual(details["adapter_pid"], self.adapter_pid)
        self.assertEqual(details["client_to_provider_occurrence_count"], 3)
        self.assertEqual(details["provider_to_client_occurrence_count"], 3)

    def test_client_wire_retains_messages_without_duplicate_raw_frames(self) -> None:
        with mock.patch(
            "network.validation.m4_common.decode_message",
            side_effect=lambda raw: json.loads(raw.decode("utf-8")),
        ):
            wire, failures = validate_wire_log(self.run_dir / "logs")
        self.assertEqual(failures, [])
        self.assertIn("messages", wire)
        self.assertIn("message_by_hash", wire)
        self.assertNotIn("raw_by_hash", wire)

    def test_provider_stream_scan_retains_bounded_metadata_for_18000_frames(self) -> None:
        records: list[tuple[str, str, dict[str, object], int]] = []
        for sequence in range(1, 18_001):
            if sequence == 1:
                message_type = "hello"
                sender_id = "sionna-provider-m4"
                direction = "outbound"
            elif sequence == 2:
                message_type = "ready"
                sender_id = "sionna-provider-m4"
                direction = "outbound"
            elif sequence == 18_000:
                message_type = "result"
                sender_id = "sionna-provider-m4"
                direction = "outbound"
            else:
                message_type = "query"
                sender_id = "sionna-adapter-m4"
                direction = "inbound"
            message: dict[str, object] = {
                "message_type": message_type,
                "sender_id": sender_id,
                "wire_sequence": sequence,
                "reconnect_generation": 0,
            }
            if message_type in {"query", "result"}:
                message["query_id"] = (
                    "query-3" if message_type == "result" else f"query-{sequence}"
                )
                message["discarded_large_payload"] = "x" * 512
            records.append(
                (direction, "conn-0-synthetic", message, 10_000 + sequence)
            )
        provider_directory = self.run_dir / "logs/provider_wire"
        write_indexed_wire_fixture(provider_directory, records)
        del records
        with (
            mock.patch.object(
                Path,
                "read_bytes",
                side_effect=AssertionError("stream scan must not call read_bytes"),
            ),
            mock.patch(
                "network.validation.validate_m4_capacity.decode_message",
                side_effect=lambda raw, max_bytes=None: json.loads(raw.decode("utf-8")),
            ),
        ):
            occurrences, scan, failures = _exact_wire_occurrences(
                provider_directory, None, label="provider"
            )
        self.assertEqual(failures, [])
        self.assertEqual(len(occurrences), 18_000)
        self.assertTrue(scan["streamed_binary_and_index"])
        self.assertLess(scan["retained_message_payload_bytes"], 4_000_000)
        self.assertGreater(
            scan["wire_bytes"], 2 * scan["retained_message_payload_bytes"]
        )
        self.assertTrue(all("raw" not in item for item in occurrences))
        query = next(
            item["message"]
            for item in occurrences
            if item["message"].get("message_type") == "query"
        )
        self.assertNotIn("discarded_large_payload", query)

    def test_byte_identical_duplicate_missing_at_provider_fails_cardinality(self) -> None:
        self._publish_wire(duplicate_client_query=True)
        _details, failures = self._validate()
        self.assertTrue(
            any("client-to-provider occurrence count differs" in item for item in failures),
            failures,
        )

    def test_byte_identical_duplicate_mirrored_at_both_peers_fails_uniqueness(self) -> None:
        self._publish_wire(
            duplicate_client_query=True,
            duplicate_provider_query=True,
        )
        _details, failures = self._validate()
        self.assertFalse(
            any(
                item.startswith("client-to-provider")
                and ("count differs" in item or "bytes/order differ" in item)
                for item in failures
            ),
            failures,
        )
        self.assertTrue(
            any("client wire repeats sender/wire_sequence" in item for item in failures),
            failures,
        )
        self.assertTrue(
            any("provider wire repeats sender/wire_sequence" in item for item in failures),
            failures,
        )
        self.assertTrue(
            any("query_id 'query-fixture-1' has 2" in item for item in failures),
            failures,
        )

    def test_missing_provider_side_wire_fails_closed(self) -> None:
        (self.run_dir / "logs/provider_wire/sionna_async_wire.bin").unlink()
        (
            self.run_dir / "logs/provider_wire/sionna_async_wire_index.jsonl"
        ).unlink()
        _details, failures = self._validate()
        self.assertTrue(
            any("provider wire data is missing/nonregular" in item for item in failures),
            failures,
        )

    def test_reordered_peer_occurrence_fails_exact_order(self) -> None:
        self._publish_wire(reorder_provider_inbound=True)
        _details, failures = self._validate()
        self.assertTrue(
            any("client-to-provider occurrence" in item for item in failures), failures
        )

    def test_mirrored_fake_provider_executable_still_fails_binding(self) -> None:
        for key in ("provider_hello", "provider_ready"):
            self.messages[key]["executable_identity"] = {
                "path": str((ROOT / PROVIDER_SCRIPT_PATH).resolve()),
                "sha256": "f" * 64,
            }
        self._publish_wire()
        _details, failures = self._validate()
        self.assertTrue(
            any("provider hello process/package identity differs" in item for item in failures),
            failures,
        )

    def test_mirrored_wrong_package_version_still_fails_binding(self) -> None:
        for key in ("provider_hello", "provider_ready"):
            identity = copy.deepcopy(self.messages[key]["provider_identity"])
            identity["sionna_rt_version"] = "999.0"
            self.messages[key]["provider_identity"] = identity
        self._publish_wire()
        _details, failures = self._validate()
        self.assertTrue(
            any("process/package identity differs" in item for item in failures), failures
        )

    def test_mirrored_wrong_scene_still_fails_binding(self) -> None:
        scene = copy.deepcopy(self.messages["provider_ready"]["scene_identity"])
        scene["scene_path"] = "/tmp/forged-sionna-scene.xml"
        self.messages["provider_ready"]["scene_identity"] = scene
        self._publish_wire()
        _details, failures = self._validate()
        self.assertIn("provider ready canonical scene identity differs", failures)

    def test_self_consistent_foreign_contract_hash_still_fails(self) -> None:
        for message in self.messages.values():
            message["contract_hash"] = "e" * 64
        self._publish_wire()
        _details, failures = self._validate()
        self.assertTrue(
            any("not bound to the current contract/config" in item for item in failures),
            failures,
        )

    def test_provider_ready_pid_not_sampled_worker_fails(self) -> None:
        ready_path = self.run_dir / "raw/state/provider.ready.json"
        ready = json.loads(ready_path.read_text(encoding="utf-8"))
        ready["pid"] += 1
        ready_path.write_bytes(canonical_bytes(ready))
        _details, failures = self._validate()
        self.assertIn(
            "provider handshake is not bound to the exact sampled process", failures
        )

    def test_provider_role_with_forged_cmdline_hash_fails(self) -> None:
        events_path = self.run_dir / "logs/m4_runtime_events.jsonl"
        event = json.loads(events_path.read_text(encoding="utf-8"))
        event["processes"]["processes"][0]["cmdline_sha256"] = "0" * 64
        events_path.write_bytes(canonical_bytes(event))
        _details, failures = self._validate()
        self.assertIn(
            "provider handshake is not bound to the exact sampled process", failures
        )

    def test_adapter_ready_pid_not_sampled_adapter_fails(self) -> None:
        ready_path = self.run_dir / "raw/state/adapter.ready.json"
        ready = json.loads(ready_path.read_text(encoding="utf-8"))
        ready["pid"] += 1
        ready_path.write_bytes(canonical_bytes(ready))
        _details, failures = self._validate()
        self.assertIn(
            "adapter handshake is not bound to the exact sampled process", failures
        )

    def test_adapter_role_with_forged_cmdline_hash_fails(self) -> None:
        events_path = self.run_dir / "logs/m4_runtime_events.jsonl"
        event = json.loads(events_path.read_text(encoding="utf-8"))
        adapter = next(
            item
            for item in event["processes"]["processes"]
            if item["role"] == "sionna_adapter"
        )
        adapter["cmdline_sha256"] = "0" * 64
        events_path.write_bytes(canonical_bytes(event))
        _details, failures = self._validate()
        self.assertIn(
            "adapter handshake is not bound to the exact sampled process", failures
        )

    def test_required_source_manifest_covers_active_transitive_q4_code(self) -> None:
        self.assertTrue(
            {
                "network/config/endpoint_transaction_schema.json",
                "network/ns3/ns3_build_receipt.py",
                "network/scripts/collect_flight_capacity.py",
                "network/scripts/write_run_provenance.py",
                "network/validation/component_profiles.py",
                "network/validation/endpoint_transaction.py",
                "network/validation/qualification_identity.py",
                "network/validation/validate_m3_external_matrix.py",
                "network/validation/validate_m4_causality.py",
            }.issubset(REQUIRED_SOURCE_PATHS)
        )


class ProviderPackageIdentityTests(unittest.TestCase):
    def test_provider_uses_sionna_rt_distribution_metadata(self) -> None:
        versions = {"sionna-rt": "1.2.2", "mitsuba": "3.8.0"}
        with mock.patch(
            "network.scripts.m4_runtime_orchestrator.importlib.metadata.version",
            side_effect=lambda name: versions[name],
        ) as version:
            self.assertEqual(
                provider_package_versions(),
                {"sionna_rt_version": "1.2.2", "mitsuba_version": "3.8.0"},
            )
        self.assertEqual(
            [call.args[0] for call in version.call_args_list],
            ["sionna-rt", "mitsuba"],
        )


class AcceptedActualControlApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.temporary.name)
        (self.run_dir / "raw/prerequisites").mkdir(parents=True)
        self.run = {
            "run_id": "m4-api-test",
            "runtime_id": "1" * 32,
            "run_nonce": "2" * 64,
            "profile": "m4_capacity_prerequisite",
            # The formal M3 evidence commit is a predecessor.  M4 executes at
            # the later status-only v4 authority commit and must not require
            # byte-equal commit IDs.
            "identity": {"source_commit": "4" * 40},
            "workload": {},
        }
        self._publish(self._producer_api())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _publish(self, api: dict[str, object]) -> None:
        receipt = {
            "contract": "ams.m3.host-final-receipt/v1",
            "profile": "m3_component",
            "run_id": "accepted-m3-predecessor",
            "source_commit": "3" * 40,
            "formal_accepted": True,
            "passed": True,
            "failures": [],
            "result_contract": "ams.m3.external-matrix-validation/v1",
            "result": {
                "contract": "ams.m3.external-matrix-validation/v1",
                "passed": True,
                "acceptance_eligible": True,
                "failures": [],
                "actual_control_api": api,
            },
        }
        receipt_payload = canonical_bytes(receipt)
        receipt_path = self.run_dir / "raw/prerequisites/m3.json"
        receipt_path.write_bytes(receipt_payload)
        receipt_sha256 = sha256_bytes(receipt_payload)
        api_sha256 = sha256_bytes(canonical_bytes(api))
        self.run["endpoint_path"] = {
            "mode": "actual_sitl_mavproxy_udp_tail",
            "acceptance_eligible": True,
            "traffic_origin": "actual_ardupilot_mavproxy",
            "accepted_m3_receipt_path": "raw/prerequisites/m3.json",
            "accepted_m3_receipt_sha256": receipt_sha256,
            "actual_control_api_contract": ACTUAL_CONTROL_API_CONTRACT,
            "actual_control_api_sha256": api_sha256,
            "actual_sitl_manifest_path": "raw/actual_sitl_endpoint_manifest.json",
            "actual_sitl_ready_path": "raw/state/actual-sitl-endpoints.ready.json",
            "actual_control_events_path": "raw/actual_control/events.jsonl",
        }
        self.run["workload"] = {
            "accepted_m3_receipt_path": "raw/prerequisites/m3.json",
            "accepted_m3_receipt_sha256": receipt_sha256,
        }
        receipts = {
            name: {
                "milestone": name.upper(),
                "contract": (
                    "ams.m3.host-final-receipt/v1" if name == "m3" else f"ams.{name}.host-final-receipt/v1"
                ),
                "run_id": "accepted-m3-predecessor" if name == "m3" else f"accepted-{name}",
                "sha256": receipt_sha256 if name == "m3" else str(int(name[1:]) + 1) * 64,
            }
            for name in ("m0", "m1", "m2", "m3")
        }
        (self.run_dir / "raw/prerequisites.json").write_bytes(
            canonical_bytes(
                {
                    "contract": "ams.component-prerequisites/v1",
                    "profile": "m4_capacity_prerequisite",
                    "source_commit": self.run["identity"]["source_commit"],
                    "status": {"contract": "ams.live-status/v4", "closed_count": 4},
                    "receipts": receipts,
                }
            )
        )

    def _producer_api(self) -> dict[str, object]:
        matrix_sha256 = _expected_actual_control_api()["matrix_sha256"]
        return m3_actual_control_api(matrix_sha256)

    def test_exact_host_final_api_passes(self) -> None:
        api, details, failures = _accepted_m3_actual_control_api(self.run_dir, self.run)
        self.assertEqual(failures, [])
        self.assertEqual(self._producer_api(), _expected_actual_control_api())
        self.assertEqual(api, _expected_actual_control_api())
        self.assertEqual(details["tail_prefixlen"], 30)
        self.assertEqual(details["tail_ports"], [14560, 14561, 14562, 14563, 14564])
        self.assertNotEqual("3" * 40, self.run["identity"]["source_commit"])

    def test_missing_m4_window_command_fails_even_when_all_outer_hashes_are_recomputed(self) -> None:
        api = self._producer_api()
        api.pop("m4_window_command")
        self._publish(api)
        _api, _details, failures = _accepted_m3_actual_control_api(self.run_dir, self.run)
        self.assertTrue(any("frozen Q3 API" in failure for failure in failures))

    def test_causality_pending_contract_mutation_fails_even_when_all_outer_hashes_are_recomputed(self) -> None:
        api = self._producer_api()
        command = api["m4_window_command"]
        command["pending_per_uav"]["correlated_timesync_required"][
            "maximum_formula"
        ] = "single pending transaction"
        self._publish(api)
        _api, _details, failures = _accepted_m3_actual_control_api(self.run_dir, self.run)
        self.assertTrue(any("frozen Q3 API" in failure for failure in failures))

    def test_uav2_tail_port_mutation_fails_even_when_all_outer_hashes_are_recomputed(self) -> None:
        api = copy.deepcopy(_expected_actual_control_api())
        api["channels"]["uav2"]["tail_uav"]["port"] = 14560
        self._publish(api)
        _api, _details, failures = _accepted_m3_actual_control_api(self.run_dir, self.run)
        self.assertTrue(any("frozen Q3 API" in failure for failure in failures))

    def test_tail_prefix_24_mutation_fails_even_when_all_outer_hashes_are_recomputed(self) -> None:
        api = copy.deepcopy(_expected_actual_control_api())
        api["channels"]["uav2"]["tail_uav"]["prefixlen"] = 24
        self._publish(api)
        _api, _details, failures = _accepted_m3_actual_control_api(self.run_dir, self.run)
        self.assertTrue(any("frozen Q3 API" in failure for failure in failures))

    def test_endpoint_form_relabel_fails(self) -> None:
        api = copy.deepcopy(_expected_actual_control_api())
        api["control_endpoint_form"] = "synthetic_matrix_fixture"
        self._publish(api)
        _api, _details, failures = _accepted_m3_actual_control_api(self.run_dir, self.run)
        self.assertTrue(any("frozen Q3 API" in failure for failure in failures))


class ActualControlEventTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.temporary.name)
        (self.run_dir / "raw/actual_control").mkdir(parents=True)
        self.run = {
            "run_id": "m4-control-test",
            "runtime_id": "4" * 32,
            "run_nonce": "5" * 64,
            "profile": "m4_capacity_prerequisite",
        }
        self.api = _expected_actual_control_api()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _records(self, *, omit_uplink: int | None = None) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []

        def add(event: str, **fields: object) -> None:
            raw_previous = canonical_bytes(records[-1]) if records else None
            records.append(
                {
                    "schema": "ams.actual-sitl.control-event/v1",
                    "run_id": self.run["run_id"],
                    "runtime_id": self.run["runtime_id"],
                    "run_nonce": self.run["run_nonce"],
                    "profile": "m4_capacity",
                    "transport_nonce32": hashlib.sha256(
                        bytes.fromhex(self.run["run_nonce"])
                    ).hexdigest()[:32],
                    "transport_nonce_derivation": "sha256(raw_full_run_nonce64)[:32]",
                    "event_sequence": len(records) + 1,
                    "previous_record_sha256": (
                        sha256_bytes(raw_previous) if raw_previous is not None else None
                    ),
                    "monotonic_ns": 1_000 + len(records),
                    "event": event,
                    "role_subject": "gcs_control_probe",
                    **fields,
                }
            )

        add(
            "actual_control_socket_ready",
            bound_socket=["10.71.0.10", 14600],
            full_run_nonce=self.run["run_nonce"],
            transmit_ip_tos=184,
            receive_ip_tos_enabled=1,
        )
        add("actual_control_link_ready")
        for uav in range(1, 6):
            request_hash = hashlib.sha256(f"request-{uav}".encode()).hexdigest()
            add(
                "real_command_offered",
                uav=uav,
                endpoint_form="actual_sitl_mavproxy_udp_tail",
                cell_id=f"uav{uav}.control.downlink",
                source_ip="10.71.0.10",
                source_udp_port=14600,
                destination_ip=f"10.71.{uav}.10",
                destination_udp_port=14600 + uav,
                tos=184,
                full_run_nonce=self.run["run_nonce"],
                command_frame_sha256=request_hash,
            )
            response_hash = hashlib.sha256(f"response-{uav}".encode()).hexdigest()
            if omit_uplink == uav:
                continue
            add(
                "transaction_result",
                uav=uav,
                endpoint_form="actual_sitl_mavproxy_udp_tail",
                downlink_cell_id=f"uav{uav}.control.downlink",
                uplink_cell_id=f"uav{uav}.control.uplink",
                full_run_nonce=self.run["run_nonce"],
                command_frame_sha256=request_hash,
                success=True,
                ack={
                    "source_system": uav,
                    "source_component": 1,
                    "message_type": "COMMAND_ACK",
                    "mavlink_command": 512,
                    "mavlink_result": 0,
                    "transport_payload_sha256": response_hash,
                },
                requested_telemetry={
                    "source_system": uav,
                    "source_component": 1,
                    "message_type": "AUTOPILOT_VERSION",
                },
            )
        add("actual_control_shutdown")
        return records

    def _write(self, records: list[dict[str, object]]) -> Path:
        path = self.run_dir / "raw/actual_control/events.jsonl"
        path.write_bytes(b"".join(canonical_bytes(record) for record in records))
        return path

    def test_exact_ten_actual_control_cells_pass(self) -> None:
        details, failures = _actual_control_event_audit(
            self._write(self._records()), run=self.run, api=self.api
        )
        self.assertEqual(failures, [])
        self.assertEqual(details["control_cell_count"], 10)

    def test_missing_actual_control_cell_fails(self) -> None:
        _details, failures = _actual_control_event_audit(
            self._write(self._records(omit_uplink=2)), run=self.run, api=self.api
        )
        self.assertTrue(any("ten accepted M3 control cells" in failure for failure in failures))


class TailTopologyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.temporary.name)
        (self.run_dir / "raw/topology_monitor").mkdir(parents=True)
        self.run = {
            "run_id": "m4-topology-test",
            "runtime_id": "6" * 32,
            "run_nonce": "7" * 64,
        }
        (self.run_dir / "raw/actual_sitl").mkdir(parents=True)
        for index in range(1, 6):
            (self.run_dir / f"raw/actual_sitl/uav{index}.ready.json").write_bytes(
                canonical_bytes({
                    "mavproxy_peer": {
                        "host": f"10.72.{index}.1",
                        "port": 43000 + index,
                    }
                })
            )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _sample(self, timestamp: int, *, uav2_prefixlen: int = 30) -> dict[str, object]:
        rules4 = [
            {"priority": 0, "table": "local"},
            {"priority": 32766, "table": "main"},
            {"priority": 32767, "table": "default"},
        ]
        rules6 = rules4[:2]

        def namespace(
            inode: int,
            *,
            links: list[dict[str, object]],
            addresses: list[dict[str, object]],
            routes: list[dict[str, object]],
            sockets: list[str],
            bridge_links: list[dict[str, object]] | None = None,
        ) -> dict[str, object]:
            return {
                "present": True,
                "namespace_inode": inode,
                "links": links,
                "addresses": addresses,
                "routes_ipv4": routes,
                "routes_ipv6": [],
                "rules_ipv4": rules4,
                "rules_ipv6": rules6,
                "neighbours_ipv4": [],
                "neighbours_ipv6": [],
                "bridge_links": bridge_links or [],
                "sockets": sockets,
                "nftables": {"nftables": [{"metainfo": {}}]},
                "iptables_ipv4": [],
                "iptables_ipv6": [],
            }

        root_addresses = [
            {
                "ifname": f"ams-tail{index}",
                "addr_info": [
                    {"family": "inet", "local": f"10.72.{index}.1", "prefixlen": 30}
                ],
            }
            for index in range(1, 6)
        ]
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
        namespaces: dict[str, object] = {
            "container-root": namespace(
                100,
                links=[{"ifname": "lo"}]
                + [{"ifname": f"ams-tail{index}"} for index in range(1, 6)],
                addresses=root_addresses,
                routes=[
                    {"dst": f"10.72.{index}.0/30", "dev": f"ams-tail{index}"}
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
                sockets=[
                    f"UNCONN 0 0 0.0.0.0:{43000 + index} 10.72.{index}.2:{14559 + index}"
                    for index in range(1, 6)
                ],
            ),
            "ams-ns3": namespace(
                101,
                links=[{"ifname": "lo"}]
                + [
                    {"ifname": f"{prefix}-{endpoint}"}
                    for endpoint in ("gcs", "uav1", "uav2", "uav3", "uav4", "uav5")
                    for prefix in ("br", "tap", "vp")
                ],
                addresses=[],
                routes=loopback_local_routes,
                sockets=[],
                bridge_links=[
                    {"ifname": f"{prefix}-{endpoint}", "master": f"br-{endpoint}"}
                    for endpoint in ("gcs", "uav1", "uav2", "uav3", "uav4", "uav5")
                    for prefix in ("tap", "vp")
                ],
            ),
            "ams-gcs": namespace(
                102,
                links=[
                    {"ifname": "lo"},
                    {"ifname": "eth0", "address": "02:71:00:00:10:10"},
                ],
                addresses=[
                    {
                        "ifname": "eth0",
                        "addr_info": [
                            {"family": "inet", "local": "10.71.0.10", "prefixlen": 24}
                        ],
                    }
                ],
                routes=[
                    {"dst": "default", "gateway": "10.71.0.1", "dev": "eth0"},
                    {"dst": "10.71.0.0/24", "dev": "eth0"},
                    {
                        "type": "local",
                        "dst": "10.71.0.10",
                        "dev": "eth0",
                        "table": "local",
                    },
                    {
                        "type": "broadcast",
                        "dst": "10.71.0.255",
                        "dev": "eth0",
                        "table": "local",
                    },
                    *loopback_local_routes,
                ],
                sockets=["UNCONN 0 0 10.71.0.10:14600 0.0.0.0:*"],
            ),
        }
        for index in range(1, 6):
            namespaces[f"ams-uav{index}"] = namespace(
                102 + index,
                links=[
                    {"ifname": "lo"},
                    {
                        "ifname": "eth0",
                        "address": f"02:71:{index:02x}:00:10:10",
                    },
                    {"ifname": "tail0"},
                ],
                addresses=[
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
                    {
                        "ifname": "tail0",
                        "addr_info": [
                            {
                                "family": "inet",
                                "local": f"10.72.{index}.2",
                                "prefixlen": uav2_prefixlen if index == 2 else 30,
                            }
                        ],
                    }
                ],
                routes=[
                    {
                        "dst": "default",
                        "gateway": f"10.71.{index}.1",
                        "dev": "eth0",
                    },
                    {"dst": f"10.71.{index}.0/24", "dev": "eth0"},
                    {"dst": f"10.72.{index}.0/30", "dev": "tail0"},
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
                    *loopback_local_routes,
                ],
                sockets=[
                    f"UNCONN 0 0 10.71.{index}.10:{14600 + index} 0.0.0.0:*",
                    f"UNCONN 0 0 10.72.{index}.2:{14559 + index} 0.0.0.0:*",
                ],
            )
        namespace_order = (
            "container-root",
            "ams-ns3",
            "ams-gcs",
            "ams-uav1",
            "ams-uav2",
            "ams-uav3",
            "ams-uav4",
            "ams-uav5",
        )
        processes: list[dict[str, object]] = []
        next_pid = 200

        def process(
            namespace_name: str,
            command: list[str],
            *,
            executable: str = "/usr/bin/python3",
        ) -> tuple[int, int]:
            nonlocal next_pid
            next_pid += 1
            identity = (next_pid, 10_000 + next_pid)
            processes.append(
                {
                    "pid": identity[0],
                    "start_ticks": identity[1],
                    "namespace": namespace_name,
                    "namespace_inode": namespaces[namespace_name][
                        "namespace_inode"
                    ],
                    "executable": executable,
                    "executable_sha256": "a" * 64,
                    "cmdline": command,
                    "cap_eff": "00000000a80425fb",
                    "cgroup": ["0::/m4-topology-test"],
                }
            )
            return identity

        monitors: dict[str, object] = {}
        for namespace_name in namespace_order:
            pid, start_ticks = process(
                namespace_name,
                ["/usr/sbin/ip", "-ts", "monitor", "all"],
                executable="/usr/sbin/ip",
            )
            monitors[namespace_name] = {
                "pid": pid,
                "start_ticks": start_ticks,
                "alive": True,
            }
        process(
            "ams-ns3",
            ["/workspace/.external/ns-3/ams-tap-packet-engine"],
            executable="/workspace/.external/ns-3/ams-tap-packet-engine",
        )
        for endpoint in ("gcs", "uav1", "uav2", "uav3", "uav4", "uav5"):
            process(
                "ams-ns3",
                [
                    "/usr/bin/python3",
                    "network/scripts/raw_packet_capture.py",
                    "--interface",
                    f"vp-{endpoint}",
                ],
            )
        process(
            "ams-gcs",
            [
                "/usr/bin/python3",
                "network/scripts/raw_packet_capture.py",
                "--interface",
                "eth0",
            ],
        )
        process(
            "ams-gcs",
            [
                "/usr/bin/python3",
                "network/scripts/actual_sitl_control_probe.py",
            ],
        )
        for index in range(1, 6):
            namespace_name = f"ams-uav{index}"
            for interface in ("eth0", "tail0"):
                process(
                    namespace_name,
                    [
                        "/usr/bin/python3",
                        "network/scripts/raw_packet_capture.py",
                        "--interface",
                        interface,
                    ],
                )
            process(
                namespace_name,
                [
                    "/usr/bin/python3",
                    "network/bridge/actual_sitl_mavlink_endpoint.py",
                ],
            )
        return {
            "schema": "ams.m3.topology_sample/v1",
            "run_id": self.run["run_id"],
            "runtime_id": self.run["runtime_id"],
            "run_nonce": self.run["run_nonce"],
            "sample_sequence": 1 if timestamp == 1_000 else 2,
            "monotonic_ns": timestamp,
            "reason": "periodic",
            "transition_sequence": None,
            "transition_event": None,
            "command_sha256": None,
            "namespaces": namespaces,
            "processes": processes,
            "netlink_monitors": monitors,
        }

    def _write(self, *, uav2_prefixlen: int = 30) -> None:
        path = self.run_dir / "raw/topology_monitor/samples.jsonl"
        path.write_bytes(
            canonical_bytes(self._sample(1_000, uav2_prefixlen=uav2_prefixlen))
            + canonical_bytes(self._sample(2_000, uav2_prefixlen=uav2_prefixlen))
        )

    def test_exact_five_tail_30s_pass(self) -> None:
        self._write()
        details, failures = _tail_topology_evidence(
            self.run_dir, run=self.run, start_ns=1_000, end_ns=2_000
        )
        self.assertEqual(failures, [])
        self.assertEqual(details["sample_count"], 2)

    def test_observed_uav2_tail_24_fails(self) -> None:
        self._write(uav2_prefixlen=24)
        _details, failures = _tail_topology_evidence(
            self.run_dir, run=self.run, start_ns=1_000, end_ns=2_000
        )
        self.assertTrue(
            any("uav2 actual MAVProxy tail IPv4" in failure for failure in failures)
        )

    def test_netlink_restart_or_missing_control_process_fails(self) -> None:
        first = self._sample(1_000)
        second = self._sample(2_000)
        monitor = second["netlink_monitors"]["ams-gcs"]
        old_pid = monitor["pid"]
        monitor["pid"] = int(old_pid) + 1_000
        monitor["start_ticks"] = int(monitor["start_ticks"]) + 1_000
        for process in second["processes"]:
            if process["pid"] == old_pid:
                process["pid"] = monitor["pid"]
                process["start_ticks"] = monitor["start_ticks"]
                break
        path = self.run_dir / "raw/topology_monitor/samples.jsonl"
        path.write_bytes(canonical_bytes(first) + canonical_bytes(second))
        _details, failures = _tail_topology_evidence(
            self.run_dir, run=self.run, start_ns=1_000, end_ns=2_000
        )
        self.assertTrue(any("netlink monitor identity changed" in item for item in failures))

        second = self._sample(2_000)
        second["processes"] = [
            process
            for process in second["processes"]
            if "actual_sitl_control_probe.py" not in " ".join(process["cmdline"])
        ]
        path.write_bytes(canonical_bytes(first) + canonical_bytes(second))
        _details, failures = _tail_topology_evidence(
            self.run_dir, run=self.run, start_ns=1_000, end_ns=2_000
        )
        self.assertTrue(any("critical process count differs" in item for item in failures))


class CapacityAirborneControllerTests(unittest.TestCase):
    class Clock:
        def __init__(self, value: int) -> None:
            self.value = value

        def now(self) -> int:
            return self.value

        def advance(self, nanoseconds: int = 50_000_000) -> None:
            self.value += nanoseconds

    class Socket:
        def __init__(self) -> None:
            self.sent: list[tuple[bytes, tuple[str, int]]] = []

        def sendto(self, payload: bytes, destination: tuple[str, int]) -> int:
            self.sent.append((payload, destination))
            return len(payload)

    @staticmethod
    def gate() -> dict[str, object]:
        schedule = {
            "warmup_start_monotonic_ns": 900_000_000_000,
            "measurement_start_monotonic_ns": 930_000_000_000,
            "measurement_end_monotonic_ns": 1_530_000_000_000,
        }
        return airborne.airborne_gate_contract(schedule)

    @staticmethod
    def response_common(uav: int, message_id: int, digest: str) -> dict[str, object]:
        return {
            "uav": uav,
            "peer_ip": f"10.71.{uav}.10",
            "peer_udp_port": 14600 + uav,
            "received_monotonic_ns": 0,
            "transport_payload_sha256": digest,
            "message_type": "COMMAND_ACK" if message_id == 77 else "TIMESYNC",
            "message_id": message_id,
            "source_system": uav,
            "source_component": 1,
            "mavlink_frame_hex": "00",
            "mavlink_frame_sha256": "a" * 64,
            "mavlink_frame_size": 1,
        }

    @staticmethod
    def ack_message(command_id: int, result: int = 0) -> mock.Mock:
        message = mock.Mock()
        message.command = command_id
        message.result = result
        return message

    @staticmethod
    def timesync_message(token: int) -> mock.Mock:
        message = mock.Mock()
        message.tc1 = 123_456_789
        message.ts1 = token
        return message

    def controller_with_pump(
        self,
        *,
        drop_first_uav1: bool = False,
        drop_uav1_attempts: set[int] | None = None,
        exact_deadline_uav1: bool = False,
    ) -> tuple[
        airborne.CapacityAirborneController,
        "CapacityAirborneControllerTests.Clock",
        "CapacityAirborneControllerTests.Socket",
        mock.Mock,
    ]:
        clock = self.Clock(100_000_000_000)
        sock = self.Socket()
        writer = mock.Mock()
        holder: dict[str, airborne.CapacityAirborneController] = {}

        dropped_uav1_attempts = set(drop_uav1_attempts or ())
        if drop_first_uav1:
            dropped_uav1_attempts.add(1)

        def pump(_timeout_s: float) -> None:
            controller = holder["controller"]
            clock.advance()
            for uav, pending in list(controller.pending_by_uav.items()):
                if uav == 1 and pending.attempt in dropped_uav1_attempts:
                    continue
                if exact_deadline_uav1 and uav == 1:
                    clock.value = pending.sent_monotonic_ns + airborne.OUTCOME_TIMEOUT_NS
                common = self.response_common(uav, 77, f"{uav}" * 64)
                common["received_monotonic_ns"] = clock.now()
                controller.observe_message(
                    message_type="COMMAND_ACK",
                    uav=uav,
                    message=self.ack_message(pending.command_id),
                    received_ns=clock.now(),
                    common=common,
                )
                if uav not in controller.pending_by_uav:
                    continue
                pending = controller.pending_by_uav[uav]
                common = self.response_common(uav, 111, f"{uav + 5}" * 64)
                common["received_monotonic_ns"] = clock.now()
                controller.observe_message(
                    message_type="TIMESYNC",
                    uav=uav,
                    message=self.timesync_message(pending.timesync_token),
                    received_ns=clock.now(),
                    common=common,
                )

        controller = airborne.CapacityAirborneController(
            run_nonce="ab" * 32,
            gate=self.gate(),
            sock=sock,
            sequencer=control_probe.MavlinkSequencer(),
            writer=writer,
            pump=pump,
            now_ns=clock.now,
        )
        holder["controller"] = controller
        return controller, clock, sock, writer

    def test_encoder_is_exact_command_long_plus_timesync_with_unique_attempt_token(self) -> None:
        first = airborne.encode_flight_command_datagram(
            run_nonce="ab" * 32,
            stage="takeoff",
            uav=3,
            attempt=1,
            sequencer=control_probe.MavlinkSequencer(),
        )
        second = airborne.encode_flight_command_datagram(
            run_nonce="ab" * 32,
            stage="takeoff",
            uav=3,
            attempt=2,
            sequencer=control_probe.MavlinkSequencer(),
        )
        self.assertEqual(
            first["request_datagram"],
            first["command_frame"] + first["timesync_frame"],
        )
        self.assertEqual(first["command_id"], airborne.MAV_CMD_NAV_TAKEOFF)
        self.assertNotEqual(
            first["timesync_request_ts1"], second["timesync_request_ts1"]
        )
        self.assertEqual(
            control_probe.struct.unpack(
                "<qq", first["timesync_frame"][10:-2]
            ),
            (0, first["timesync_request_ts1"]),
        )

    def test_reposition_is_exact_command_int_and_all_tokens_are_disjoint(self) -> None:
        encoded = airborne.encode_flight_command_datagram(
            run_nonce="ab" * 32,
            stage="reposition",
            uav=4,
            attempt=3,
            sequencer=control_probe.MavlinkSequencer(),
            current_lat_e7=-353_632_621,
            current_lon_e7=1_491_652_374,
        )
        self.assertEqual(encoded["command_message_id"], 75)
        self.assertEqual(encoded["command_encoding"], "COMMAND_INT")
        self.assertEqual(len(encoded["command_frame"]), 47)
        fields = control_probe.struct.unpack(
            "<4fiifHBBBBB", encoded["command_frame"][10:-2]
        )
        self.assertEqual(fields[4:6], (-353_632_621, 1_491_652_374))
        self.assertEqual(fields[6], airborne.MOTION_TARGET_RELATIVE_ALT_M)
        self.assertEqual(
            fields[7:],
            (
                airborne.MAV_CMD_DO_REPOSITION,
                4,
                1,
                airborne.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
                0,
                0,
            ),
        )
        tokens = {
            airborne.flight_timesync_token(
                run_nonce="ab" * 32,
                stage_code=int(definition["stage_code"]),
                uav=uav,
                ordinal=attempt,
            )
            for definition in airborne.STAGE_DEFINITIONS
            for uav in airborne.EXPECTED_UAVS
            for attempt in range(
                1,
                airborne.maximum_attempts_for_stage(str(definition["stage"])) + 1,
            )
        }
        self.assertEqual(
            len(tokens),
            sum(
                airborne.maximum_attempts_for_stage(str(definition["stage"]))
                for definition in airborne.STAGE_DEFINITIONS
            )
            * 5,
        )
        self.assertTrue(all(0 < token < 1 << 63 for token in tokens))

    def test_gate_has_separate_warmup_landing_and_disarm_budgets(self) -> None:
        gate = self.gate()
        self.assertEqual(gate["warmup_motion_stages"], ["reposition"])
        self.assertEqual(
            gate["warmup_motion_deadline_monotonic_ns"]
            - gate["warmup_start_monotonic_ns"],
            airborne.PER_STAGE_MAX_NS,
        )
        self.assertEqual(
            gate["landing_deadline_monotonic_ns"]
            - gate["measurement_end_monotonic_ns"],
            130_000_000_000,
        )
        self.assertEqual(
            gate["disarm_deadline_monotonic_ns"]
            - gate["landing_deadline_monotonic_ns"],
            60_000_000_000,
        )
        self.assertEqual(
            gate["stage_timing_budget"], airborne.stage_timing_budget()
        )

    def test_stage_accepts_only_ack_and_exact_ts1_before_half_open_deadline(self) -> None:
        controller, _clock, sock, writer = self.controller_with_pump()
        remaining = controller._send_stage_attempt("takeoff", set(range(1, 6)), 1)
        self.assertEqual(remaining, set())
        self.assertEqual(len(sock.sent), 5)
        self.assertEqual(
            [call.args[0] for call in writer.emit.call_args_list].count(
                "flight_command_complete"
            ),
            5,
        )
        self.assertEqual(
            {value["outcome"] for value in controller.retired_tokens.values()},
            {"accepted"},
        )

    def test_exact_deadline_response_times_out_and_cannot_complete(self) -> None:
        controller, _clock, _sock, writer = self.controller_with_pump(
            exact_deadline_uav1=True
        )
        remaining = controller._send_stage_attempt("takeoff", {1}, 1)
        self.assertEqual(remaining, {1})
        events = [call.args[0] for call in writer.emit.call_args_list]
        self.assertIn("flight_command_outcome_timeout", events)
        self.assertIn("late_flight_command_ack", events)
        self.assertNotIn("flight_command_complete", events)

    def test_pre_measurement_loss_retries_after_three_second_quiet_drain(self) -> None:
        controller, _clock, sock, writer = self.controller_with_pump(
            drop_first_uav1=True
        )
        controller._send_stage("takeoff")
        self.assertEqual(len(sock.sent), 6)
        offers = [
            call.kwargs
            for call in writer.emit.call_args_list
            if call.args[0] == "flight_command_offered" and call.kwargs["uav"] == 1
        ]
        self.assertEqual([item["attempt"] for item in offers], [1, 2])
        self.assertNotEqual(
            offers[0]["timesync_request_ts1"], offers[1]["timesync_request_ts1"]
        )
        drains = [
            call.kwargs
            for call in writer.emit.call_args_list
            if call.args[0] == "flight_command_quiet_drain"
        ]
        self.assertEqual(len(drains), 1)
        self.assertGreaterEqual(
            drains[0]["completed_monotonic_ns"]
            - drains[0]["last_response_monotonic_ns"],
            airborne.OUTCOME_TIMEOUT_NS,
        )

    def test_pre_measurement_allows_a_fourth_bounded_attempt(self) -> None:
        controller, _clock, sock, writer = self.controller_with_pump(
            drop_uav1_attempts={1, 2, 3}
        )
        controller._send_stage("takeoff")
        self.assertEqual(len(sock.sent), 8)
        offers = [
            call.kwargs
            for call in writer.emit.call_args_list
            if call.args[0] == "flight_command_offered" and call.kwargs["uav"] == 1
        ]
        self.assertEqual([item["attempt"] for item in offers], [1, 2, 3, 4])
        self.assertEqual(len({item["timesync_request_ts1"] for item in offers}), 4)


class FrozenRuntimeContractTests(unittest.TestCase):
    def test_more_than_256_byte_identical_frames_are_correlated_by_occurrence(self) -> None:
        digest = "9" * 64
        records = [
            {
                "monotonic_ns": 10_000 + ordinal,
                "sha256": digest,
                "ordinal": ordinal,
            }
            for ordinal in range(300)
        ]
        cursors: dict[tuple[object, ...], int] = {}
        observed = [
            _consume_ordered_occurrence(
                records,
                cursors,
                cursor_key=("adapter", 1, "gcs_to_tail", digest),
                timestamp_field="monotonic_ns",
                lower_ns=10_000 + ordinal,
                upper_ns=10_000 + ordinal,
            )["ordinal"]
            for ordinal in range(300)
        ]
        self.assertEqual(observed, list(range(300)))
        with self.assertRaisesRegex(M4ValidationError, "no remaining ordered occurrence"):
            _consume_ordered_occurrence(
                records,
                cursors,
                cursor_key=("adapter", 1, "gcs_to_tail", digest),
                timestamp_field="monotonic_ns",
                lower_ns=10_300,
                upper_ns=10_300,
            )

    def test_capture_process_count_is_derived_from_exact_22_role_map(self) -> None:
        self.assertEqual(len(MANDATORY_CAPTURE_ROLES), 22)
        self.assertEqual(len(set(MANDATORY_CAPTURE_ROLES)), 22)
        self.assertEqual(REQUIRED_PROCESS_COUNTS["packet_capture"], 22)
        self.assertEqual(
            {role for role in MANDATORY_CAPTURE_ROLES if role.startswith("tail-")},
            {
                *(f"tail-root-uav{index}" for index in range(1, 6)),
                *(f"tail-uav{index}" for index in range(1, 6)),
            },
        )

        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            control = {
                "delivered_request_hashes": {
                    f"uav{index}": [f"{index:064x}"] for index in range(1, 6)
                },
                "response_hashes": {
                    f"uav{index}": [f"{index + 5:064x}"] for index in range(1, 6)
                },
            }
            all_control_hashes = {
                digest
                for mapping in control.values()
                for hashes in mapping.values()
                for digest in hashes
            }
            for index in range(1, 6):
                write_capture_stats_v2_fixture(
                    run_dir,
                    name=f"tail-root-uav{index}",
                    interface=f"ams-tail{index}",
                    setter="SO_RCVBUF" if index % 2 else "SO_RCVBUFFORCE",
                )
                write_capture_stats_v2_fixture(
                    run_dir,
                    name=f"tail-uav{index}",
                    interface="tail0",
                    setter="SO_RCVBUFFORCE" if index % 2 else "SO_RCVBUF",
                )
            with mock.patch(
                "network.validation.validate_m4_capacity._pcap_udp_payload_hashes",
                return_value=(all_control_hashes, 1),
            ):
                details, failures = _tail_capture_evidence(
                    run_dir,
                    control=control,
                    start_ns=2_000_000_000,
                    end_ns=3_000_000_000,
                )
            self.assertEqual(failures, [])
            self.assertEqual(details["tail_capture_count"], 10)

            tail_stats_path = run_dir / "logs/capture-tail-uav3.json"
            tail_stats = json.loads(tail_stats_path.read_text())
            tail_stats["receive_buffer_effective_bytes"] -= 1
            tail_stats_path.write_bytes(canonical_bytes(tail_stats))
            with mock.patch(
                "network.validation.validate_m4_capacity._pcap_udp_payload_hashes",
                return_value=(all_control_hashes, 1),
            ):
                _details, failures = _tail_capture_evidence(
                    run_dir,
                    control=control,
                    start_ns=2_000_000_000,
                    end_ns=3_000_000_000,
                )
            self.assertTrue(any("capture accounting differs" in item for item in failures))

        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            (run_dir / "raw").mkdir(parents=True)
            (run_dir / "raw/m4_capacity_contract.json").write_bytes(
                canonical_bytes({})
            )
            (run_dir / "logs").mkdir(parents=True)
            (run_dir / "logs/m4_runtime_events.jsonl").write_bytes(
                canonical_bytes(
                    {
                        "event": "measurement_start",
                        "host_monotonic_ns": 2_000_000_001,
                        "host_realtime_ns": 102_000_000_001,
                    }
                )
                + canonical_bytes(
                    {
                        "event": "measurement_end",
                        "host_monotonic_ns": 3_000_000_001,
                        "host_realtime_ns": 103_000_000_001,
                    }
                )
            )
            capture_specs = [
                *((f"endpoint-{endpoint}", "eth0") for endpoint in (
                    "gcs", "uav1", "uav2", "uav3", "uav4", "uav5"
                )),
                *((f"ns3-external-{endpoint}", f"vp-{endpoint}") for endpoint in (
                    "gcs", "uav1", "uav2", "uav3", "uav4", "uav5"
                )),
            ]
            for ordinal, (name, interface) in enumerate(capture_specs):
                write_capture_stats_v2_fixture(
                    run_dir,
                    name=name,
                    interface=interface,
                    setter=(
                        "SO_RCVBUF" if ordinal % 2 else "SO_RCVBUFFORCE"
                    ),
                )
            with (
                mock.patch(
                    "network.validation.validate_m3_external_matrix.parse_pcap",
                    return_value=(1, [], []),
                ),
                mock.patch(
                    "network.validation.m4_runtime._collect_capacity_endpoint_records",
                    return_value=({}, {}),
                ),
            ):
                details, failures = validate_external_captures(
                    run_dir,
                    start_ns=2_000_000_000,
                    end_ns=3_000_000_000,
                )
            self.assertEqual(details["capture_count"], 12)
            self.assertFalse(
                any("capture accounting differs" in item for item in failures),
                failures,
            )

            endpoint_stats_path = run_dir / "logs/capture-endpoint-uav2.json"
            endpoint_stats = json.loads(endpoint_stats_path.read_text())
            endpoint_stats["drain_batch_packet_limit"] = 255
            endpoint_stats_path.write_bytes(canonical_bytes(endpoint_stats))
            with (
                mock.patch(
                    "network.validation.validate_m3_external_matrix.parse_pcap",
                    return_value=(1, [], []),
                ),
                mock.patch(
                    "network.validation.m4_runtime._collect_capacity_endpoint_records",
                    return_value=({}, {}),
                ),
            ):
                _details, failures = validate_external_captures(
                    run_dir,
                    start_ns=2_000_000_000,
                    end_ns=3_000_000_000,
                )
            self.assertIn(
                "capture accounting differs: endpoint-uav2",
                failures,
            )

    def test_actual_control_runtime_roles_are_classified_separately(self) -> None:
        self.assertEqual(
            classify_process(
                "python3",
                ["python3", "/qualified/actual_sitl_control_probe.py", "--run-dir", "/run"],
            ),
            "gcs_endpoint_probe",
        )
        self.assertEqual(
            classify_process(
                "python3",
                ["python3", "/qualified/actual_sitl_mavlink_endpoint.py", "--uav", "uav2"],
            ),
            "uav_endpoint_adapter",
        )
        self.assertEqual(
            classify_process(
                "python3",
                [
                    "python3",
                    "/qualified/actual_sitl_endpoint_orchestrator.py",
                    "--manifest",
                    "/run/m.json",
                ],
            ),
            "actual_endpoint_supervisor",
        )

    def test_supervisor_restart_between_runtime_samples_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            (run_dir / "logs").mkdir()

            def process(pid: int) -> dict[str, object]:
                return {
                    "pid": pid,
                    "start_ticks": pid + 1_000,
                    "pgid": 90,
                    "role": "actual_endpoint_supervisor",
                    "executable_path": "/usr/bin/python3",
                    "executable_sha256": "8" * 64,
                    "cmdline_sha256": f"{pid:064x}",
                }

            events = [
                {
                    "event": "measurement_resource_sample",
                    "processes": {"processes": [process(100)]},
                },
                {
                    "event": "measurement_resource_sample",
                    "processes": {"processes": [process(101)]},
                },
            ]
            (run_dir / "logs/m4_runtime_events.jsonl").write_bytes(
                b"".join(canonical_bytes(event) for event in events)
            )
            with self.assertRaisesRegex(M4ValidationError, "process identity changed"):
                _runtime_process_samples(run_dir)

    def test_missing_actual_clock_producer_fails_closed(self) -> None:
        pids = {
            producer: index + 100
            for index, producer in enumerate(REQUIRED_CLOCK_PRODUCERS)
        }
        del pids["uav_control_adapter_uav2"]
        _details, failures = validate_clock_process_binding([], {"producer_pids": pids})
        self.assertEqual(
            failures, ["clock producer PID map differs from frozen process roles"]
        )

    def test_causal_resource_samples_bind_all_clock_producer_processes(self) -> None:
        pids: dict[str, int] = {}
        next_pid = 500
        for producer in CLOCK_PRODUCER_PROCESS_ROLES:
            if producer == "ros_gazebo_tracker":
                continue
            pids[producer] = next_pid
            next_pid += 1
        pids["ros_gazebo_tracker"] = pids["sionna_adapter"]
        processes = []
        for producer, pid in pids.items():
            if any(item["pid"] == pid for item in processes):
                continue
            processes.append(
                {
                    "pid": pid,
                    "start_ticks": pid + 1_000,
                    "role": CLOCK_PRODUCER_PROCESS_ROLES[producer],
                    "executable_path": "/usr/bin/python3",
                    "executable_sha256": "8" * 64,
                    "cmdline_sha256": f"{pid:064x}",
                }
            )
        details, failures = validate_clock_process_binding(
            [
                {
                    "event": "causal_resource_sample",
                    "processes": {"processes": processes},
                }
            ],
            {"producer_pids": pids},
        )
        self.assertEqual(failures, [])
        self.assertEqual(
            details["bound_producer_count"], len(set(pids.values()))
        )


class ContinuousReadinessScheduleTests(unittest.TestCase):
    warmup_start_ns = 10_000_000_000
    measurement_start_ns = 40_000_000_000
    measurement_end_ns = 640_000_000_000

    def records(self) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        readiness = {
            "ready": True,
            "files_ready": True,
            "clocks_fresh": True,
            "clocks_coherent": True,
            "odometry_fresh": True,
            "world_poses_fresh": True,
        }
        for index in range(30):
            scheduled_ns = self.warmup_start_ns + index * 1_000_000_000
            result.append(
                {
                    "event": "continuous_readiness_sample",
                    "phase": "warmup",
                    "scheduled_monotonic_ns": scheduled_ns,
                    "host_monotonic_ns": scheduled_ns + 10_000_000,
                    **readiness,
                }
            )
        for index in range(600):
            scheduled_ns = self.measurement_start_ns + index * 1_000_000_000
            result.append(
                {
                    "event": "continuous_readiness_sample",
                    "phase": "measurement",
                    "sample_index": index,
                    "scheduled_monotonic_ns": scheduled_ns,
                    "host_monotonic_ns": scheduled_ns + 10_000_000,
                    **readiness,
                }
            )
        return result

    def validate(
        self, records: list[dict[str, object]]
    ) -> tuple[dict[str, object], list[str]]:
        return validate_continuous_readiness_schedule(
            records,
            warmup_start_ns=self.warmup_start_ns,
            measurement_start_ns=self.measurement_start_ns,
            measurement_end_ns=self.measurement_end_ns,
        )

    def test_exact_30_plus_600_absolute_readiness_series_passes(self) -> None:
        details, failures = self.validate(self.records())
        self.assertEqual(failures, [])
        self.assertEqual(details["sample_count"], 630)
        self.assertEqual(details["warmup_sample_count"], 30)
        self.assertEqual(details["measurement_sample_count"], 600)

    def test_missing_duplicate_or_drifted_readiness_slot_fails_closed(self) -> None:
        for mutation in (
            "missing",
            "duplicate",
            "phase",
            "sample_index",
            "scheduled",
            "host_deadline",
            "component_false",
        ):
            with self.subTest(mutation=mutation):
                records = self.records()
                if mutation == "missing":
                    del records[29]
                elif mutation == "duplicate":
                    records.insert(30, copy.deepcopy(records[29]))
                elif mutation == "phase":
                    records[29]["phase"] = "measurement"
                elif mutation == "sample_index":
                    records[30]["sample_index"] = 1
                elif mutation == "scheduled":
                    records[400]["scheduled_monotonic_ns"] = int(
                        records[400]["scheduled_monotonic_ns"]
                    ) + 1
                elif mutation == "host_deadline":
                    records[629]["host_monotonic_ns"] = int(
                        records[629]["scheduled_monotonic_ns"]
                    ) + 100_000_001
                else:
                    records[200]["files_ready"] = False
                _details, failures = self.validate(records)
                self.assertTrue(failures, mutation)
                self.assertTrue(
                    any(
                        token in failure
                        for failure in failures
                        for token in ("sample count differs", "absolute slot differs")
                    ),
                    failures,
                )


class ExternalCaptureOccurrenceTests(unittest.TestCase):
    start_ns = 2_000_000_000
    end_ns = 3_000_000_000
    start_realtime_ns = 100_000_000_000
    end_realtime_ns = 101_000_000_000
    target_cell = "uav1.control.downlink"

    @staticmethod
    def endpoint(cell: dict[str, object], side: str) -> str:
        value = cell[side]
        assert isinstance(value, dict)
        if value["namespace"] == "ams-gcs":
            return "gcs"
        uav = cell["uav"]
        assert isinstance(uav, dict)
        return str(uav["name"])

    @staticmethod
    def packet(cell: dict[str, object], record: dict[str, object]) -> dict[str, object]:
        source = cell["source"]
        destination = cell["destination"]
        ns3_path = cell["ns3_path"]
        assert isinstance(source, dict)
        assert isinstance(destination, dict)
        assert isinstance(ns3_path, dict)
        return {
            "transport_payload_sha256": record["transport_payload_sha256"],
            "source_ip": source["ip"],
            "destination_ip": destination["ip"],
            "source_udp_port": source["udp_port"],
            "destination_udp_port": destination["udp_port"],
            "tos": ns3_path["dscp_tos"],
            "transport_payload_size": record["transport_payload_size"],
        }

    def validate_fixture(
        self,
        *,
        omit_second_target_source: bool,
        add_second_target_outside_measurement: bool,
        add_early_target_extra: bool = False,
    ) -> tuple[dict[str, object], list[str]]:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            (run_dir / "raw").mkdir(parents=True)
            (run_dir / "logs").mkdir(parents=True)
            (run_dir / "raw/m4_capacity_contract.json").write_bytes(
                canonical_bytes({})
            )
            (run_dir / "logs/m4_runtime_events.jsonl").write_bytes(
                canonical_bytes(
                    {
                        "event": "measurement_start",
                        "host_monotonic_ns": self.start_ns + 10_000_000,
                        "host_realtime_ns": self.start_realtime_ns + 10_000_000,
                    }
                )
                + canonical_bytes(
                    {
                        "event": "measurement_end",
                        "host_monotonic_ns": self.end_ns + 10_000_000,
                        "host_realtime_ns": self.end_realtime_ns + 10_000_000,
                    }
                )
            )
            matrix = json.loads(
                (ROOT / "network/config/endpoint_matrix_5uav.json").read_text(
                    encoding="utf-8"
                )
            )
            offered: dict[str, dict[tuple[str, str], dict[str, object]]] = {}
            received: dict[str, dict[tuple[str, str], dict[str, object]]] = {}
            captures: dict[str, list[dict[str, object]]] = {
                **{f"endpoint-{endpoint}": [] for endpoint in (
                    "gcs", "uav1", "uav2", "uav3", "uav4", "uav5"
                )},
                **{f"ns3-external-{endpoint}": [] for endpoint in (
                    "gcs", "uav1", "uav2", "uav3", "uav4", "uav5"
                )},
            }
            for cell_number, cell in enumerate(matrix["cells"], start=1):
                cell_id = str(cell["cell_id"])
                digest = f"{cell_number:064x}"
                occurrence_count = 2 if cell_id == self.target_cell else 1
                offered[cell_id] = {}
                received[cell_id] = {}
                source_endpoint = self.endpoint(cell, "source")
                destination_endpoint = self.endpoint(cell, "destination")
                for occurrence in range(occurrence_count):
                    sent_ns = (
                        self.start_ns
                        + 100_000_000
                        + cell_number * 1_000_000
                        + occurrence * 600_000_000
                    )
                    received_ns = sent_ns + 50_000
                    nonce = f"{cell_number:02d}-{occurrence}"
                    source_record: dict[str, object] = {
                        "record_nonce": nonce,
                        "transport_payload_sha256": digest,
                        "transport_payload_size": 64,
                        "sent_monotonic_ns": sent_ns,
                    }
                    destination_record = {
                        **source_record,
                        "received_monotonic_ns": received_ns,
                    }
                    identity = (nonce, digest)
                    offered[cell_id][identity] = source_record
                    received[cell_id][identity] = destination_record
                    packet = self.packet(cell, source_record)
                    timestamp_ns = (
                        self.start_realtime_ns + (sent_ns - self.start_ns)
                    )
                    roles = (
                        f"endpoint-{source_endpoint}",
                        f"ns3-external-{source_endpoint}",
                        f"ns3-external-{destination_endpoint}",
                        f"endpoint-{destination_endpoint}",
                    )
                    for role in roles:
                        if (
                            omit_second_target_source
                            and cell_id == self.target_cell
                            and occurrence == 1
                            and role == f"endpoint-{source_endpoint}"
                        ):
                            continue
                        captures[role].append(
                            {**packet, "timestamp_ns": timestamp_ns}
                        )
                    if (
                        add_early_target_extra
                        and cell_id == self.target_cell
                        and occurrence == 0
                    ):
                        captures[f"endpoint-{source_endpoint}"].append(
                            {
                                **packet,
                                "timestamp_ns": timestamp_ns - 50_000,
                            }
                        )
                    if (
                        add_second_target_outside_measurement
                        and cell_id == self.target_cell
                        and occurrence == 1
                    ):
                        captures[f"endpoint-{source_endpoint}"].append(
                            {
                                **packet,
                                "timestamp_ns": self.start_realtime_ns - 1,
                            }
                        )

            capture_specs = [
                *((f"endpoint-{endpoint}", "eth0") for endpoint in (
                    "gcs", "uav1", "uav2", "uav3", "uav4", "uav5"
                )),
                *((f"ns3-external-{endpoint}", f"vp-{endpoint}") for endpoint in (
                    "gcs", "uav1", "uav2", "uav3", "uav4", "uav5"
                )),
            ]
            for ordinal, (name, interface) in enumerate(capture_specs):
                captures[name].sort(key=lambda record: int(record["timestamp_ns"]))
                for frame_index, record in enumerate(captures[name], start=1):
                    record["frame_index"] = frame_index
                write_capture_stats_v2_fixture(
                    run_dir,
                    name=name,
                    interface=interface,
                    setter="SO_RCVBUF" if ordinal % 2 else "SO_RCVBUFFORCE",
                    packet_count=len(captures[name]),
                )

            def parse(path: Path) -> tuple[int, list[dict[str, object]], list[str]]:
                records = captures[path.stem]
                return len(records), records, []

            with (
                mock.patch(
                    "network.validation.validate_m3_external_matrix.parse_pcap",
                    side_effect=parse,
                ),
                mock.patch(
                    "network.validation.m4_runtime._collect_capacity_endpoint_records",
                    return_value=(offered, received),
                ),
            ):
                return validate_external_captures(
                    run_dir,
                    start_ns=self.start_ns,
                    end_ns=self.end_ns,
                )

    def test_repeated_payload_requires_one_captured_frame_per_occurrence(self) -> None:
        details, failures = self.validate_fixture(
            omit_second_target_source=False,
            add_second_target_outside_measurement=False,
        )
        self.assertEqual(failures, [])
        self.assertEqual(
            details["cell_role_counts"][self.target_cell]["source_endpoint"],
            2,
        )

        _details, failures = self.validate_fixture(
            omit_second_target_source=True,
            add_second_target_outside_measurement=False,
        )
        self.assertIn(
            f"{self.target_cell}/source_endpoint external capture occurrences differ: "
            "expected=2 matched=1 missing=1",
            failures,
        )

    def test_out_of_measurement_frame_cannot_fill_missing_occurrence(self) -> None:
        details, failures = self.validate_fixture(
            omit_second_target_source=True,
            add_second_target_outside_measurement=True,
        )
        self.assertIn(
            f"{self.target_cell}/source_endpoint external capture occurrences differ: "
            "expected=2 matched=1 missing=1",
            failures,
        )
        self.assertEqual(
            details["measurement_start_realtime_ns"], self.start_realtime_ns
        )
        self.assertEqual(
            details["measurement_end_realtime_ns"], self.end_realtime_ns
        )

    def test_early_extra_cannot_replace_missing_late_occurrence(self) -> None:
        _details, failures = self.validate_fixture(
            omit_second_target_source=True,
            add_second_target_outside_measurement=False,
            add_early_target_extra=True,
        )
        self.assertIn(
            f"{self.target_cell}/source_endpoint external capture occurrences differ: "
            "expected=2 matched=1 missing=1",
            failures,
        )

    def test_occurrence_timing_accepts_only_frozen_tolerance_boundaries(self) -> None:
        packet_key = ("same-byte-occurrence",)
        expected_monotonic_ns = 2_500_000_000
        expected_realtime_ns = 100_500_000_000
        expected = {
            "record_nonce": "expected",
            "transport_payload_sha256": "a" * 64,
            "sent_monotonic_ns": expected_monotonic_ns,
        }
        for delta_ns, expected_count in (
            (-CAPTURE_OCCURRENCE_MATCH_TOLERANCE_NS, 1),
            (CAPTURE_OCCURRENCE_MATCH_TOLERANCE_NS, 1),
            (-CAPTURE_OCCURRENCE_MATCH_TOLERANCE_NS - 1, 0),
            (CAPTURE_OCCURRENCE_MATCH_TOLERANCE_NS + 1, 0),
        ):
            with self.subTest(delta_ns=delta_ns):
                matched = _consume_capture_role_occurrences(
                    {
                        packet_key: [
                            {"timestamp_ns": expected_realtime_ns + delta_ns}
                        ]
                    },
                    [expected],
                    capture="endpoint-gcs",
                    key_fn=lambda _record: packet_key,
                    timestamp_field="sent_monotonic_ns",
                    start_ns=self.start_ns,
                    end_ns=self.end_ns,
                    start_realtime_ns=self.start_realtime_ns,
                    end_realtime_ns=self.end_realtime_ns,
                    cursors={},
                )
                self.assertEqual(matched, (1, expected_count))

        overlapping = {
            **expected,
            "record_nonce": "overlapping",
            "sent_monotonic_ns": (
                expected_monotonic_ns
                + 2 * CAPTURE_OCCURRENCE_MATCH_TOLERANCE_NS
                - 1
            ),
        }
        with self.assertRaisesRegex(
            M4ValidationError, "timing windows overlap"
        ):
            _consume_capture_role_occurrences(
                {packet_key: []},
                [expected, overlapping],
                capture="endpoint-gcs",
                key_fn=lambda _record: packet_key,
                timestamp_field="sent_monotonic_ns",
                start_ns=self.start_ns,
                end_ns=self.end_ns,
                start_realtime_ns=self.start_realtime_ns,
                end_realtime_ns=self.end_realtime_ns,
                cursors={},
            )


if __name__ == "__main__":
    unittest.main()
