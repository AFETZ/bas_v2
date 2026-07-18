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


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from network.scripts.collect_m4_runtime import classify_process  # noqa: E402
from network.validation.m4_common import M4ValidationError  # noqa: E402
from network.validation.m4_runtime import (  # noqa: E402
    CLOCK_PRODUCER_PROCESS_ROLES,
    MANDATORY_CAPTURE_ROLES,
    REQUIRED_CLOCK_PRODUCERS,
    REQUIRED_PROCESS_COUNTS,
    _consume_ordered_occurrence,
    validate_clock_process_binding,
)
from network.validation.validate_m4_capacity import (  # noqa: E402
    _accepted_m3_actual_control_api,
    _actual_control_event_audit,
    _expected_actual_control_api,
    _runtime_process_samples,
    _tail_topology_evidence,
)


def canonical_bytes(value: object) -> bytes:
    return (json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


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
        self._publish(_expected_actual_control_api())

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
            "actual_control_api_contract": "ams.m3.actual-control-api/v1",
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

    def test_exact_host_final_api_passes(self) -> None:
        api, details, failures = _accepted_m3_actual_control_api(self.run_dir, self.run)
        self.assertEqual(failures, [])
        self.assertEqual(api, _expected_actual_control_api())
        self.assertEqual(details["tail_prefixlen"], 30)
        self.assertEqual(details["tail_ports"], [14560, 14561, 14562, 14563, 14564])
        self.assertNotEqual("3" * 40, self.run["identity"]["source_commit"])

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

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _sample(self, timestamp: int, *, uav2_prefixlen: int = 30) -> dict[str, object]:
        def namespace(inode: int, addresses: list[dict[str, object]]) -> dict[str, object]:
            return {"present": True, "namespace_inode": inode, "addresses": addresses}

        root_addresses = [
            {
                "ifname": f"ams-tail{index}",
                "addr_info": [
                    {"family": "inet", "local": f"10.72.{index}.1", "prefixlen": 30}
                ],
            }
            for index in range(1, 6)
        ]
        namespaces: dict[str, object] = {
            "container-root": namespace(100, root_addresses),
            "ams-ns3": namespace(101, []),
            "ams-gcs": namespace(102, []),
        }
        for index in range(1, 6):
            namespaces[f"ams-uav{index}"] = namespace(
                102 + index,
                [
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
            )
        return {
            "run_id": self.run["run_id"],
            "runtime_id": self.run["runtime_id"],
            "run_nonce": self.run["run_nonce"],
            "monotonic_ns": timestamp,
            "namespaces": namespaces,
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
        self.assertTrue(any("uav2 actual-SITL /30" in failure for failure in failures))


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


if __name__ == "__main__":
    unittest.main()
