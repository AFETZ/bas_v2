from __future__ import annotations

import ast
import copy
import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


from network.bridge import actual_sitl_mavlink_endpoint as endpoint
from network.bridge.actual_sitl_mavlink_endpoint import (
    AUTHORIZATION_CONTRACT,
    CONTROL_TOS,
    MANIFEST_CONTRACT,
    EndpointError,
    ForwardLineageGate,
    JsonlAudit,
    LineageError,
    document_sha256,
    expected_process_identity,
    mavlink_source_system_ids,
    parse_proc_inet,
    parse_proc_stat,
    publish_json_exclusive,
    read_process_identity,
    recv_control_datagram,
    strict_json,
    validate_authorization,
    validate_jsonl_audit,
    validate_manifest,
)
from network.bridge.opaque_udp_relay import ByteOpaqueUdpRelay, RelayError


HEX_A = "a" * 64
HEX_B = "b" * 64


def expected_process(pid: int, pgid: int, role: str) -> dict[str, object]:
    return {
        "pid": pid,
        "start_ticks": 100_000 + pid,
        "pgid": pgid,
        "session_id": 7000,
        "cmdline_sha256": HEX_A,
        "exe_path": f"/opt/test/{role}",
        "exe_sha256": HEX_B,
        "exe_dev": 20,
        "exe_inode": 300_000 + pid,
        "exe_size": 10_000,
        "netns_inode": 4026533000,
    }


def valid_manifest() -> dict[str, object]:
    pgid = 7001
    channels = []
    for index in range(1, 6):
        channels.append(
            {
                "uav": f"uav{index}",
                "instance": index - 1,
                "system_id": index,
                "namespace": f"ams-uav{index}",
                "namespace_inode": 4026534000 + index,
                "radio_bind": {"host": f"10.71.{index}.10", "port": 14600 + index},
                "gcs_peer": {"host": "10.71.0.10", "port": 14600},
                "tail_bind": {"host": f"10.72.{index}.2", "port": 14559 + index},
                "tail_peer_host": f"10.72.{index}.1",
                "tail_pcap_roles": {
                    "root": f"tail-root-uav{index}",
                    "uav": f"tail-uav{index}",
                },
                "master": {"host": "127.0.0.1", "port": 5760 + 10 * (index - 1)},
                "launch_pgid": pgid,
                "mavproxy": expected_process(8000 + index, pgid, "mavproxy.py"),
                "sitl": expected_process(9000 + index, pgid, "arducopter"),
            }
        )
    return {
        "schema_version": 1,
        "contract": MANIFEST_CONTRACT,
        "run_id": "m3-test-run",
        "runtime_id": "m3-test-runtime",
        "run_nonce": "0123456789abcdef",
        "adapter_source_sha256": "c" * 64,
        "relay_core_source_sha256": "d" * 64,
        "peer_lease_ms": 5000,
        "lineage_check_ms": 250,
        "authorization_timeout_ms": 60000,
        "channels": channels,
    }


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple[str, int]]] = []

    def sendto(self, payload: bytes, peer: tuple[str, int]) -> int:
        self.sent.append((payload, peer))
        return len(payload)


class FakeRecvmsgSocket:
    def __init__(
        self,
        *,
        ancillary: list[tuple[int, int, bytes]],
        flags: int = 0,
    ) -> None:
        self.ancillary = ancillary
        self.flags = flags

    def recvmsg(
        self, _payload_size: int, _ancillary_size: int
    ) -> tuple[bytes, list[tuple[int, int, bytes]], int, tuple[str, int]]:
        return b"actual-mavlink", self.ancillary, self.flags, ("10.71.1.10", 14601)


class ActualSitlManifestTests(unittest.TestCase):
    def test_exact_five_uav_topology_is_accepted(self) -> None:
        manifest = valid_manifest()
        self.assertIs(validate_manifest(manifest), manifest)
        self.assertEqual(
            [channel["tail_bind"]["port"] for channel in manifest["channels"]],
            [14560, 14561, 14562, 14563, 14564],
        )

    def test_eth0_style_control_tail_is_rejected(self) -> None:
        manifest = valid_manifest()
        manifest["channels"][2]["tail_bind"]["host"] = "10.71.3.10"
        with self.assertRaisesRegex(EndpointError, "tail_bind"):
            validate_manifest(manifest)

    def test_duplicate_process_pid_is_rejected(self) -> None:
        manifest = valid_manifest()
        manifest["channels"][1]["sitl"]["pid"] = manifest["channels"][0]["sitl"]["pid"]
        with self.assertRaisesRegex(EndpointError, "PID is reused"):
            validate_manifest(manifest)

    def test_missing_actual_tail_capture_role_is_rejected(self) -> None:
        manifest = valid_manifest()
        manifest["channels"][4]["tail_pcap_roles"]["root"] = "uav5-eth0"
        with self.assertRaisesRegex(EndpointError, "actual tail capture roles"):
            validate_manifest(manifest)


class OpaqueForwardingTests(unittest.TestCase):
    def test_no_bytes_leave_before_authorization_then_exact_bytes_relay(self) -> None:
        radio = FakeSocket()
        tail = FakeSocket()
        checks: list[str] = []
        forwarder = ByteOpaqueUdpRelay(
            radio,
            tail,
            ("10.71.0.10", 14600),
            tail_peer_host="10.72.1.1",
            strict_tail_peer=True,
            forwarding_enabled=False,
            before_forward=lambda: checks.append("verified"),
        )
        mavproxy_peer = ("10.72.1.1", 43123)
        telemetry = b"\xfd\x03\x00\x00\x01\x01\x01\x00\x00\x00\x00\xff\x7f\x00\x00"
        command = b"\xfe\x02\x09\xff\xbe\x4c\x00\x00\x01\x02"
        self.assertEqual(forwarder.relay_tail(telemetry, mavproxy_peer).action, "held")
        self.assertEqual(
            forwarder.relay_radio(command, ("10.71.0.10", 14600)).action,
            "dropped",
        )
        self.assertEqual(radio.sent, [])
        self.assertEqual(tail.sent, [])
        forwarder.authorize()
        self.assertEqual(forwarder.relay_tail(telemetry, mavproxy_peer).action, "forwarded")
        self.assertEqual(
            forwarder.relay_radio(command, ("10.71.0.10", 14600)).action,
            "forwarded",
        )
        self.assertEqual(radio.sent, [(telemetry, ("10.71.0.10", 14600))])
        self.assertEqual(tail.sent, [(command, mavproxy_peer)])
        self.assertEqual(checks, ["verified", "verified", "verified"])

    def test_dynamic_mavproxy_peer_replacement_fails_closed(self) -> None:
        forwarder = ByteOpaqueUdpRelay(
            FakeSocket(),
            FakeSocket(),
            ("10.71.0.10", 14600),
            tail_peer_host="10.72.2.1",
            strict_tail_peer=True,
            forwarding_enabled=False,
        )
        forwarder.lock_peer(("10.72.2.1", 41000))
        with self.assertRaisesRegex(RelayError, "peer replacement"):
            forwarder.relay_tail(b"real-mavlink", ("10.72.2.1", 41001))

    def test_unexpected_gcs_cannot_reach_tail(self) -> None:
        tail = FakeSocket()
        forwarder = ByteOpaqueUdpRelay(
            FakeSocket(),
            tail,
            ("10.71.0.10", 14600),
            tail_peer_host="10.72.3.1",
            strict_tail_peer=True,
            forwarding_enabled=False,
        )
        forwarder.lock_peer(("10.72.3.1", 42000))
        forwarder.authorize()
        self.assertEqual(
            forwarder.relay_radio(b"command", ("10.71.0.99", 14600)).action,
            "dropped",
        )
        self.assertEqual(tail.sent, [])

    def test_live_mavproxy_or_sitl_pid_kill_prevents_any_next_relay(self) -> None:
        for killed_role in ("mavproxy", "sitl"):
            with self.subTest(killed_role=killed_role):
                processes = {
                    role: subprocess.Popen(
                        ["sleep", "30"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    for role in ("mavproxy", "sitl")
                }
                guard = None
                try:
                    expected = {
                        role: expected_process_identity(read_process_identity(process.pid))
                        for role, process in processes.items()
                    }
                    radio = FakeSocket()
                    clock = [1_000_000_000]
                    full_lineage = mock.Mock()
                    guard = ForwardLineageGate(
                        expected,
                        full_check_interval_ns=250_000_000,
                        full_check=full_lineage,
                        monotonic_ns=lambda: clock[0],
                    )
                    guard.mark_full_check()

                    forwarder = ByteOpaqueUdpRelay(
                        radio,
                        FakeSocket(),
                        ("10.71.0.10", 14600),
                        tail_peer_host="10.72.1.1",
                        strict_tail_peer=True,
                        forwarding_enabled=False,
                        before_forward=guard.check,
                    )
                    peer = ("10.72.1.1", 43000)
                    forwarder.lock_peer(peer)
                    forwarder.authorize()
                    for ordinal in range(3):
                        forwarder.relay_tail(f"frame-{ordinal}".encode(), peer)
                    full_lineage.assert_not_called()
                    clock[0] += 250_000_000
                    forwarder.relay_tail(b"cadence-refresh", peer)
                    full_lineage.assert_called_once_with()
                    sent_before_kill = list(radio.sent)
                    processes[killed_role].kill()
                    processes[killed_role].wait(timeout=3)
                    with self.assertRaisesRegex(LineageError, "exited before relay"):
                        forwarder.relay_tail(b"actual-vehicle-frame", peer)
                    self.assertEqual(radio.sent, sent_before_kill)
                finally:
                    if guard is not None:
                        guard.close()
                    for process in processes.values():
                        if process.poll() is None:
                            process.kill()
                            process.wait(timeout=3)


class ProtocolAndEvidenceTests(unittest.TestCase):
    def test_tcp_master_multiplicity_retry_is_bounded_and_exact(self) -> None:
        candidates = [
            {"inode": 101, "local": {"host": "127.0.0.1", "port": 40001}},
            {"inode": 102, "local": {"host": "127.0.0.1", "port": 40002}},
        ]
        ambiguous = endpoint.TransientSocketMultiplicity(
            "uav1 MAVProxy-to-SITL established TCP master",
            candidates,
        )
        with self.assertRaises(endpoint.TransientSocketMultiplicity):
            endpoint._one(
                [],
                "uav1 MAVProxy-to-SITL established TCP master",
                retry_multiple=True,
            )
        accepted = {"stable": True}
        with (
            mock.patch.object(
                endpoint,
                "_verify_channel_lineage_once",
                side_effect=[ambiguous, accepted],
            ) as snapshot,
            mock.patch.object(endpoint.time, "sleep") as pause,
        ):
            self.assertEqual(
                endpoint.verify_channel_lineage({}, ("10.72.1.1", 40000)),
                accepted,
            )
        self.assertEqual(snapshot.call_count, 2)
        pause.assert_called_once_with(endpoint.LINEAGE_MULTIPLICITY_RETRY_S)

        missing = endpoint.TransientSocketMultiplicity(
            "uav1 MAVProxy-to-SITL established TCP master",
            [],
        )
        with (
            mock.patch.object(
                endpoint,
                "_verify_channel_lineage_once",
                side_effect=[missing, accepted],
            ) as snapshot,
            mock.patch.object(endpoint.time, "sleep") as pause,
        ):
            self.assertEqual(
                endpoint.verify_channel_lineage({}, ("10.72.1.1", 40000)),
                accepted,
            )
        self.assertEqual(snapshot.call_count, 2)
        pause.assert_called_once_with(endpoint.LINEAGE_MULTIPLICITY_RETRY_S)

        with (
            mock.patch.object(
                endpoint,
                "_verify_channel_lineage_once",
                side_effect=[ambiguous] * endpoint.LINEAGE_MULTIPLICITY_ATTEMPTS,
            ) as snapshot,
            mock.patch.object(endpoint.time, "sleep") as pause,
            self.assertRaisesRegex(
                LineageError,
                "multiplicity remained ambiguous across 4 bounded snapshots",
            ),
        ):
            endpoint.verify_channel_lineage({}, ("10.72.1.1", 40000))
        self.assertEqual(snapshot.call_count, endpoint.LINEAGE_MULTIPLICITY_ATTEMPTS)
        self.assertEqual(
            pause.call_count,
            endpoint.LINEAGE_MULTIPLICITY_ATTEMPTS - 1,
        )

    def test_radio_receive_requires_exact_control_tos_ancillary(self) -> None:
        valid = FakeRecvmsgSocket(
            ancillary=[
                (socket.IPPROTO_IP, socket.IP_TOS, bytes([CONTROL_TOS]))
            ]
        )
        payload, peer, tos = recv_control_datagram(valid)  # type: ignore[arg-type]
        self.assertEqual(payload, b"actual-mavlink")
        self.assertEqual(peer, ("10.71.1.10", 14601))
        self.assertEqual(tos, CONTROL_TOS)

        for ancillary in (
            [],
            [(socket.IPPROTO_IP, socket.IP_TOS, bytes([0]))],
            [(socket.IPPROTO_IP, socket.IP_TOS, bytes([CONTROL_TOS, 0]))],
            [
                (socket.IPPROTO_IP, socket.IP_TOS, bytes([CONTROL_TOS])),
                (socket.IPPROTO_IP, socket.IP_TOS, bytes([CONTROL_TOS])),
            ],
        ):
            with self.subTest(ancillary=ancillary):
                with self.assertRaisesRegex(EndpointError, "TOS"):
                    recv_control_datagram(  # type: ignore[arg-type]
                        FakeRecvmsgSocket(ancillary=ancillary)
                    )

    def test_radio_receive_rejects_truncated_datagram_before_forwarding(self) -> None:
        for flag in (
            getattr(socket, "MSG_TRUNC", 0x20),
            getattr(socket, "MSG_CTRUNC", 0x08),
        ):
            with self.subTest(flag=flag):
                truncated = FakeRecvmsgSocket(
                    ancillary=[
                        (socket.IPPROTO_IP, socket.IP_TOS, bytes([CONTROL_TOS]))
                    ],
                    flags=flag,
                )
                with self.assertRaisesRegex(EndpointError, "truncated"):
                    recv_control_datagram(truncated)  # type: ignore[arg-type]

    def test_actual_endpoint_source_locks_transmit_and_receive_tos(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "bridge"
            / "actual_sitl_mavlink_endpoint.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "radio.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, CONTROL_TOS)",
            source,
        )
        self.assertIn(
            "tail.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, CONTROL_TOS)",
            source,
        )
        self.assertIn('radio_ip_tos=CONTROL_TOS', source)
        self.assertIn('received_tos=received_tos', source)

    def test_mavlink_v1_v2_and_signed_v2_sysids_are_recognized(self) -> None:
        v1 = bytes([0xFE, 0, 1, 2, 3, 4, 0, 0])
        v2 = bytes([0xFD, 0, 0, 0, 1, 4, 3, 0, 0, 0, 0, 0])
        signed_v2 = bytes([0xFD, 0, 1, 0, 1, 5, 3, 0, 0, 0, 0, 0]) + bytes(13)
        self.assertEqual(mavlink_source_system_ids(b"noise" + v1 + v2 + signed_v2), [2, 4, 5])
        self.assertEqual(mavlink_source_system_ids(b"not-a-frame"), [])

    def test_proc_stat_parser_handles_spaces_and_parentheses_in_comm(self) -> None:
        tail = ["S", "1", "7001", "7000"] + ["0"] * 15 + ["123456"] + ["0"] * 20
        parsed = parse_proc_stat("4242 (odd worker (uav 1)) " + " ".join(tail))
        self.assertEqual(parsed["pid"], 4242)
        self.assertEqual(parsed["pgid"], 7001)
        self.assertEqual(parsed["start_ticks"], 123456)

    def test_proc_udp_parser_exposes_exact_inode_and_endpoint(self) -> None:
        payload = (
            "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode\n"
            "  0: 0100480A:A861 0200480A:38E0 07 00000000:00000000 00:00000000 "
            "00000000 1000 0 987654 2 0000000000000000 0\n"
        )
        records = parse_proc_inet(payload, "udp")
        self.assertEqual(records[0]["local"], {"host": "10.72.0.1", "port": 43105})
        self.assertEqual(records[0]["remote"], {"host": "10.72.0.2", "port": 14560})
        self.assertEqual(records[0]["inode"], 987654)

    def test_authorization_is_bound_to_candidate_hash_and_live_issuer(self) -> None:
        manifest = valid_manifest()
        channel = manifest["channels"][0]
        candidate = {"lineage": {"actual": "sitl"}}
        candidate_hash = document_sha256(candidate)
        issuer = read_process_identity(os.getpid())
        authorization = {
            "schema_version": 1,
            "contract": AUTHORIZATION_CONTRACT,
            "status": "authorized",
            "run_id": manifest["run_id"],
            "runtime_id": manifest["runtime_id"],
            "run_nonce": manifest["run_nonce"],
            "uav": "uav1",
            "manifest_sha256": document_sha256(manifest),
            "candidate_sha256": candidate_hash,
            "verified_candidate_lineage_sha256": document_sha256(candidate["lineage"]),
            "issuer": issuer,
            "authorized_wall_utc": "2026-07-18T00:00:00Z",
            "authorized_monotonic_ns": 1,
        }
        validate_authorization(
            authorization,
            manifest,
            document_sha256(manifest),
            channel,
            candidate,
            candidate_hash,
        )
        offline_authorization = copy.deepcopy(authorization)
        offline_authorization["issuer"]["pid"] = 2_000_000_000
        offline_authorization["issuer"]["start_ticks"] += 1
        with mock.patch.object(
            endpoint,
            "verify_expected_process",
            side_effect=AssertionError("offline validation consulted a stopped PID"),
        ):
            validate_authorization(
                offline_authorization,
                manifest,
                document_sha256(manifest),
                channel,
                candidate,
                candidate_hash,
                require_live_issuer=False,
            )
        malformed_issuer = copy.deepcopy(offline_authorization)
        malformed_issuer["issuer"]["cmdline_sha256"] = "f" * 64
        with self.assertRaisesRegex(EndpointError, "command-line bytes/hash differ"):
            validate_authorization(
                malformed_issuer,
                manifest,
                document_sha256(manifest),
                channel,
                candidate,
                candidate_hash,
                require_live_issuer=False,
            )
        offline_authorization["candidate_sha256"] = "f" * 64
        with self.assertRaisesRegex(EndpointError, "immutable candidate"):
            validate_authorization(
                offline_authorization,
                manifest,
                document_sha256(manifest),
                channel,
                candidate,
                candidate_hash,
                require_live_issuer=False,
            )

    def test_json_evidence_is_immutable_and_duplicate_keys_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "ready.json"
            publish_json_exclusive(path, {"ready": True})
            self.assertEqual(strict_json(path), {"ready": True})
            with self.assertRaisesRegex(EndpointError, "already exists"):
                publish_json_exclusive(path, {"ready": False})
            duplicate = Path(temp_dir) / "duplicate.json"
            duplicate.write_text('{"x":1,"x":2}\n', encoding="utf-8")
            with self.assertRaisesRegex(EndpointError, "duplicate JSON key"):
                strict_json(duplicate)

    def test_audit_has_contiguous_sequence_hash_chain_and_final_sync_contract(self) -> None:
        manifest = valid_manifest()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "adapter.jsonl"
            with mock.patch.object(endpoint.os, "fsync") as fsync:
                audit = JsonlAudit(path, manifest, "uav1")
                audit.emit("adapter_bound_not_ready")
                audit.emit("forward", direction="tail_to_gcs", sha256="d" * 64)
                audit.emit("adapter_stop")
                fsync.assert_not_called()
                audit.close()
                fsync.assert_called_once()
            records = validate_jsonl_audit(
                path,
                run_id=manifest["run_id"],
                runtime_id=manifest["runtime_id"],
                run_nonce=manifest["run_nonce"],
                uav="uav1",
            )
            self.assertEqual([record["event_seq"] for record in records], [1, 2, 3])
            lines = path.read_bytes().splitlines(keepends=True)
            path.write_bytes(lines[0] + lines[2])
            with self.assertRaisesRegex(EndpointError, "sequence gap"):
                validate_jsonl_audit(
                    path,
                    run_id=manifest["run_id"],
                    runtime_id=manifest["runtime_id"],
                    run_nonce=manifest["run_nonce"],
                    uav="uav1",
                )

    def test_adapter_has_no_protocol_encoder_dependency(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "bridge"
            / "actual_sitl_mavlink_endpoint.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("import pymavlink", source)
        self.assertNotIn("from pymavlink", source)
        self.assertNotIn("mavutil", source)
        self.assertNotIn("encode_mavlink", source)

    def test_m3_is_a_real_extension_of_the_single_m2_relay_core_and_schema(self) -> None:
        root = Path(__file__).resolve().parents[2]
        shared = root / "network/bridge/opaque_udp_relay.py"
        m2_entrypoint = root / "network/bridge/uav_mavlink_endpoint.py"
        m3_entrypoint = root / "network/bridge/actual_sitl_mavlink_endpoint.py"
        m2_runner = root / "network/scripts/run_one_uav_vertical_slice.sh"
        m3_runner = root / "network/scripts/run_m3_external_matrix.sh"
        m3_probe = root / "network/scripts/m3_external_matrix_probe.py"
        m3_validator = root / "network/validation/validate_m3_external_matrix.py"

        def tree(path: Path) -> ast.Module:
            return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

        for entrypoint in (m2_entrypoint, m3_entrypoint):
            parsed = tree(entrypoint)
            imports = {
                node.module
                for node in ast.walk(parsed)
                if isinstance(node, ast.ImportFrom)
            }
            self.assertIn("network.bridge.opaque_udp_relay", imports)
            relay_constructors = [
                node
                for node in ast.walk(parsed)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "ByteOpaqueUdpRelay"
            ]
            self.assertEqual(
                len(relay_constructors),
                1,
                f"{entrypoint.name} does not instantiate the shared relay exactly once",
            )
            wrapper_sends = [
                node
                for node in ast.walk(parsed)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "sendto"
            ]
            self.assertEqual(
                wrapper_sends,
                [],
                f"{entrypoint.name} bypasses the shared byte-opaque relay core",
            )

        shared_tree = tree(shared)
        sends = [
            node
            for node in ast.walk(shared_tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "sendto"
        ]
        self.assertEqual(len(sends), 1)
        self.assertEqual(len(sends[0].args), 2)
        self.assertIsInstance(sends[0].args[0], ast.Name)
        self.assertEqual(sends[0].args[0].id, "payload")

        m2_runner_text = m2_runner.read_text(encoding="utf-8")
        m3_runner_text = m3_runner.read_text(encoding="utf-8")
        self.assertIn("network/bridge/uav_mavlink_endpoint.py", m2_runner_text)
        self.assertIn("network/bridge/actual_sitl_mavlink_endpoint.py", m3_runner_text)
        exact_schema = "network/config/endpoint_transaction_schema.json"
        self.assertIn(exact_schema, m2_runner_text)
        self.assertIn(exact_schema, m3_runner_text)
        self.assertIn('--endpoint-schema "$ENDPOINT_SCHEMA"', m3_runner_text)
        probe_text = m3_probe.read_text(encoding="utf-8")
        self.assertIn('"--endpoint-schema"', probe_text)
        self.assertIn("write_bytes_exclusive(endpoint_schema_copy, endpoint_schema_payload)", probe_text)
        self.assertIn('"raw_copy_path": endpoint_schema_copy.relative_to(', probe_text)
        validator_text = m3_validator.read_text(encoding="utf-8")
        self.assertIn('run_dir / "raw/endpoint_transaction_schema.json"', validator_text)
        self.assertIn("predecessor_schema != endpoint_schema_binding", validator_text)
        exact_matrix = "network/config/endpoint_matrix_5uav.json"
        self.assertIn(exact_matrix, m2_runner_text)
        self.assertIn(exact_matrix, m3_runner_text)


if __name__ == "__main__":
    unittest.main()
