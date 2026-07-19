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
from network.validation.m4_common import M4ValidationError  # noqa: E402
from network.validation.m4_runtime import (  # noqa: E402
    CLOCK_PRODUCER_PROCESS_ROLES,
    MANDATORY_CAPTURE_ROLES,
    REQUIRED_CLOCK_PRODUCERS,
    REQUIRED_PROCESS_COUNTS,
    _consume_ordered_occurrence,
    validate_clock_process_binding,
    validate_external_captures,
)
from network.validation.validate_m4_capacity import (  # noqa: E402
    _accepted_m3_actual_control_api,
    _actual_control_event_audit,
    _expected_actual_control_api,
    _runtime_process_samples,
    _tail_capture_evidence,
    _tail_topology_evidence,
)
from network.scripts import actual_sitl_control_probe as control_probe  # noqa: E402
from network.scripts import m4_capacity_airborne as airborne  # noqa: E402
from network.scripts import raw_packet_capture  # noqa: E402


def canonical_bytes(value: object) -> bytes:
    return (json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def write_capture_stats_v2_fixture(
    run_dir: Path,
    *,
    name: str,
    interface: str,
    setter: str = "SO_RCVBUF",
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
                "packets_written": 1,
                "packets_received_kernel": 1,
                "packets_dropped_kernel": 0,
            }
        )
    )
    (logs / f"capture-{name}.stderr").write_bytes(b"")


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
        self, *, drop_first_uav1: bool = False, exact_deadline_uav1: bool = False
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

        def pump(_timeout_s: float) -> None:
            controller = holder["controller"]
            clock.advance()
            for uav, pending in list(controller.pending_by_uav.items()):
                if drop_first_uav1 and uav == 1 and pending.attempt == 1:
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
            for attempt in range(1, 4)
        }
        self.assertEqual(
            len(tokens), len(airborne.STAGE_DEFINITIONS) * 5 * 3
        )
        self.assertTrue(all(0 < token < 1 << 63 for token in tokens))

    def test_gate_has_separate_warmup_landing_and_disarm_budgets(self) -> None:
        gate = self.gate()
        self.assertEqual(gate["warmup_motion_stages"], ["reposition"])
        self.assertEqual(
            gate["warmup_motion_deadline_monotonic_ns"]
            - gate["warmup_start_monotonic_ns"],
            15_000_000_000,
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


if __name__ == "__main__":
    unittest.main()
