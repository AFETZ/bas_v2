from __future__ import annotations

import inspect
import hashlib
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from network.tests.mavlink_vertical_slice_probe import (
    DatagramSequences,
    DatagramWriter,
    EndpointEventWriter,
    JsonlWriter,
    MIN_POSITIVE_HEARTBEATS,
    PERSISTENT_CONTROL_SCHEMA,
    PERSISTENT_ENDPOINT_EVENT_SCHEMA,
    PROBE_RAW_EVENT_SCHEMA,
    PhaseResult,
    PersistentEndpointConfig,
    PersistentGcsEndpoint,
    attempt_nonce,
    criteria_met,
    emit_datagram_tx,
    emit_phase_start,
    execute_phase,
    latency_stats,
    make_marker,
    make_transaction_id,
    mavlink_frame_sequence,
    observe_positive_heartbeats,
    parse_process_reference,
    parse_persistent_args,
    read_cmdline_sha256,
    read_start_ticks,
    receive_messages,
    record_message,
    send_persistent_control_request,
)


class MavlinkVerticalSliceProbeTests(unittest.TestCase):
    def test_attempt_nonce_and_marker_are_deterministic(self) -> None:
        nonce = "m2n-0123456789abcdef01234567"
        self.assertEqual(attempt_nonce(nonce, "recovery", 10), f"{nonce}:recovery:10")
        marker = make_marker(nonce, "recovery", 10)
        self.assertIn(nonce, marker)
        self.assertLessEqual(len(marker.encode("ascii")), 50)
        source = Path(__file__).with_name("mavlink_vertical_slice_probe.py").read_text()
        self.assertIn("MAVLINK_CONTROL_TOS = 184", source)
        self.assertIn(
            "sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, MAVLINK_CONTROL_TOS)",
            source,
        )

    def test_marker_rejects_oversized_nonce(self) -> None:
        with self.assertRaises(ValueError):
            make_marker("n" * 100, "recovery", 10)

    def test_mavlink_sequence_parsing(self) -> None:
        self.assertEqual(mavlink_frame_sequence(bytes([0xFE, 0, 37, 0, 0])), 37)
        self.assertEqual(mavlink_frame_sequence(bytes([0xFD, 0, 0, 0, 91])), 91)
        with self.assertRaises(ValueError):
            mavlink_frame_sequence(b"bad")

    def test_phase_window_opens_before_gcs_socket_can_queue_packets(self) -> None:
        class Recorder:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, object]]] = []

            def emit(self, event: str, **details: object) -> None:
                self.calls.append((event, details))

        args = Namespace(
            attempts=10,
            expected_ack=True,
            gcs_bind=("10.71.0.10", 14600),
            uav_endpoint=("10.71.1.10", 14601),
            target_system=1,
            positive_heartbeat_observation_s=5.0,
        )
        recorder = Recorder()
        emit_phase_start(recorder, args)  # type: ignore[arg-type]
        self.assertEqual(
            recorder.calls,
            [
                (
                    "phase_start",
                    {
                        "attempts": 10,
                        "expected_ack": True,
                        "gcs_bind": ["10.71.0.10", 14600],
                        "uav_endpoint": ["10.71.1.10", 14601],
                        "target_system": 1,
                        "minimum_positive_heartbeats": MIN_POSITIVE_HEARTBEATS,
                        "positive_heartbeat_observation_s": 5.0,
                    },
                )
            ],
        )
        source = inspect.getsource(execute_phase)
        self.assertLess(
            source.index("emit_phase_start(writer, args)"),
            source.index("socket.socket("),
        )

    def test_command_attempt_is_recorded_before_datagram_send(self) -> None:
        source = inspect.getsource(execute_phase)
        attempt_record = source.index('writer.emit(\n                "command_attempt"')
        marker_send = source.index('leg="marker"')
        command_send = source.index('leg="command"')
        self.assertLess(attempt_record, marker_send)
        self.assertLess(marker_send, command_send)

    def test_positive_liveness_observation_records_the_complete_bounded_stream(self) -> None:
        class Recorder:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, object]]] = []

            def emit(self, event: str, **details: object) -> None:
                self.calls.append((event, details))

        class Message:
            def __init__(self, system_id: int) -> None:
                self.system_id = system_id

            def get_type(self) -> str:
                return "HEARTBEAT"

            def get_srcSystem(self) -> int:
                return self.system_id

            def get_srcComponent(self) -> int:
                return 1

        records = [
            (Message(1), ("10.71.1.10", 14601), 1, "a" * 64, 1, 1),
            (Message(2), ("10.71.1.11", 14601), 2, "b" * 64, 2, 1),
            (Message(1), ("10.71.1.10", 14601), 3, "c" * 64, 3, 1),
            (Message(1), ("10.71.1.10", 14601), 4, "d" * 64, 4, 1),
        ]
        recorder = Recorder()
        with mock.patch(
            "network.tests.mavlink_vertical_slice_probe.receive_messages",
            return_value=iter(records),
        ) as receive:
            observed = observe_positive_heartbeats(
                mock.sentinel.sock,
                mock.sentinel.parser,
                recorder,  # type: ignore[arg-type]
                DatagramSequences(),
                target_system=1,
                observation_s=5.0,
            )

        self.assertEqual(observed, MIN_POSITIVE_HEARTBEATS)
        self.assertEqual(receive.call_count, 1)
        heartbeats = [details for event, details in recorder.calls if event == "heartbeat"]
        self.assertEqual(len(heartbeats), len(records))
        self.assertTrue(all(details["liveness_observation"] is True for details in heartbeats))

    def test_positive_criteria_require_three_bounded_observation_heartbeats(self) -> None:
        args = Namespace(expected_ack=True, attempts=10)
        insufficient = PhaseResult(
            10,
            10,
            10,
            8,
            False,
            [],
            [],
            heartbeat_observation_count=MIN_POSITIVE_HEARTBEATS - 1,
            heartbeat_observation_s=5.0,
        )
        sufficient = PhaseResult(
            10,
            10,
            10,
            MIN_POSITIVE_HEARTBEATS,
            False,
            [],
            [],
            heartbeat_observation_count=MIN_POSITIVE_HEARTBEATS,
            heartbeat_observation_s=5.0,
        )
        self.assertFalse(criteria_met(args, insufficient, True))
        self.assertTrue(criteria_met(args, sufficient, True))
        source = inspect.getsource(execute_phase)
        self.assertLess(
            source.index("observe_positive_heartbeats("),
            source.index("for attempt in range(1, args.attempts + 1):"),
        )

    def test_positive_runner_waits_for_readiness_before_opening_each_capture_window(self) -> None:
        source = (
            Path(__file__).parents[1] / "scripts" / "run_one_uav_vertical_slice.sh"
        ).read_text(encoding="utf-8")
        self.assertIn('POSITIVE_READINESS_STABILITY_S="${POSITIVE_READINESS_STABILITY_S:-10}"', source)
        self.assertIn('POSITIVE_HEARTBEAT_OBSERVATION_S="${POSITIVE_HEARTBEAT_OBSERVATION_S:-5}"', source)
        self.assertLess(source.index("\nstart_persistent_captures\n"), source.index("start_ns3 good"))
        for phase in ("good", "recovery"):
            self.assertLess(source.index(f"start_ns3 {phase}"), source.index(f"wait_positive_readiness {phase}"))
            self.assertLess(
                source.index(f"wait_positive_readiness {phase}"),
                source.index(f"run_probe_phase {phase}"),
            )
        self.assertIn("--positive-heartbeat-observation-s \"$POSITIVE_HEARTBEAT_OBSERVATION_S\"", source)
        self.assertIn('local control_timeout_s', source)
        self.assertIn('--timeout-s "$control_timeout_s"', source)
        self.assertIn('attempts * ack', source)

    def test_transaction_id_is_stable_and_binds_every_required_identity_field(self) -> None:
        identity = {
            "run_nonce": "m2n-0123456789abcdef01234567",
            "phase": "recovery",
            "attempt": 7,
            "marker_sha256": "a" * 64,
            "command_sha256": "b" * 64,
            "mavlink_seq": 91,
            "source_system": 255,
            "source_component": 190,
            "target_system": 1,
            "target_component": 1,
            "mavlink_command": 512,
        }
        first = make_transaction_id(**identity)
        second = make_transaction_id(**identity)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)
        self.assertEqual(first, hashlib.sha256(json.dumps({
            "attempt": 7,
            "command_sha256": "b" * 64,
            "marker_sha256": "a" * 64,
            "mavlink_command": 512,
            "mavlink_seq": 91,
            "phase": "recovery",
            "run_nonce": "m2n-0123456789abcdef01234567",
            "source_component": 190,
            "source_system": 255,
            "target_component": 1,
            "target_system": 1,
        }, sort_keys=True, separators=(",", ":")).encode("ascii")).hexdigest())
        changed = dict(identity, attempt=8)
        self.assertNotEqual(first, make_transaction_id(**changed))

    def test_datagram_tx_records_precise_send_boundary_after_a_committed_attempt(self) -> None:
        class Recorder:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, object]]] = []

            def emit(self, event: str, **details: object) -> None:
                self.calls.append((event, details))

        class Socket:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, payload: bytes, destination: tuple[str, int]) -> int:
                self.sent.append((payload, destination))
                return len(payload)

        recorder = Recorder()
        socket_stub = Socket()
        writer = DatagramWriter(socket_stub, ("10.71.1.10", 14601))  # type: ignore[arg-type]
        sequences = DatagramSequences()
        transaction_id = "f" * 64
        with mock.patch(
            "network.tests.mavlink_vertical_slice_probe.time.monotonic_ns",
            side_effect=(100, 101, 102, 103),
        ):
            marker_complete = emit_datagram_tx(
                recorder,  # type: ignore[arg-type]
                writer,
                sequences,
                transaction_id=transaction_id,
                leg="marker",
                attempt=1,
                nonce="nonce:good:1",
                payload=b"marker",
            )
            command_complete = emit_datagram_tx(
                recorder,  # type: ignore[arg-type]
                writer,
                sequences,
                transaction_id=transaction_id,
                leg="command",
                attempt=1,
                nonce="nonce:good:1",
                payload=b"command",
            )

        self.assertEqual((marker_complete, command_complete), (101, 103))
        self.assertEqual([payload for payload, _destination in socket_stub.sent], [b"marker", b"command"])
        self.assertEqual([event for event, _details in recorder.calls], ["datagram_tx", "datagram_tx"])
        marker, command = [details for _event, details in recorder.calls]
        self.assertEqual(marker["event_schema"], PROBE_RAW_EVENT_SCHEMA)
        self.assertEqual(marker["tx_datagram_seq"], 1)
        self.assertEqual(command["tx_datagram_seq"], 2)
        self.assertEqual(marker["leg"], "marker")
        self.assertEqual(command["leg"], "command")
        self.assertEqual(marker["transport_payload_sha256"], hashlib.sha256(b"marker").hexdigest())
        self.assertEqual(command["send_start_monotonic_ns"], 102)
        self.assertEqual(command["send_complete_monotonic_ns"], 103)

    def test_datagram_rx_is_fsynced_before_parsing_and_decoded_messages_link_to_it(self) -> None:
        class Recorder:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, object]]] = []

            def emit(self, event: str, **details: object) -> None:
                self.calls.append((event, details))

        class Socket:
            def recvfrom(self, _size: int) -> tuple[bytes, tuple[str, int]]:
                return b"raw-udp-datagram", ("10.71.1.10", 14601)

        class Message:
            def __init__(self, message_type: str) -> None:
                self.message_type = message_type

            def get_type(self) -> str:
                return self.message_type

            def get_srcSystem(self) -> int:
                return 1

            def get_srcComponent(self) -> int:
                return 1

        recorder = Recorder()
        socket_stub = Socket()

        class Parser:
            def parse_buffer(self, _payload: bytes) -> list[Message]:
                self_outer.assertEqual(recorder.calls[0][0], "datagram_rx")
                return [Message("HEARTBEAT"), Message("HEARTBEAT")]

        self_outer = self
        with mock.patch(
            "network.tests.mavlink_vertical_slice_probe.select.select",
            side_effect=[([socket_stub], [], []), ([], [], [])],
        ):
            received = list(
                receive_messages(
                    socket_stub,  # type: ignore[arg-type]
                    Parser(),
                    recorder,  # type: ignore[arg-type]
                    DatagramSequences(),
                    deadline=time.monotonic() + 1.0,
                )
            )

        self.assertEqual(len(received), 2)
        self.assertEqual([record[4:] for record in received], [(1, 1), (1, 2)])
        event, details = recorder.calls[0]
        self.assertEqual(event, "datagram_rx")
        self.assertEqual(details["event_schema"], PROBE_RAW_EVENT_SCHEMA)
        self.assertEqual(details["rx_datagram_seq"], 1)
        self.assertEqual(details["transport_payload_size"], len(b"raw-udp-datagram"))

    def test_decoded_records_reference_raw_datagram_and_transaction_context(self) -> None:
        class Recorder:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, object]]] = []

            def emit(self, event: str, **details: object) -> None:
                self.calls.append((event, details))

        class Message:
            def __init__(self, message_type: str) -> None:
                self.message_type = message_type
                self.command = 512
                self.result = 0

            def get_type(self) -> str:
                return self.message_type

            def get_srcSystem(self) -> int:
                return 1

            def get_srcComponent(self) -> int:
                return 1

            def get_msgId(self) -> int:
                return 148

        recorder = Recorder()
        transaction_id = "e" * 64
        for index, message_type in enumerate(("HEARTBEAT", "COMMAND_ACK", "AUTOPILOT_VERSION"), start=1):
            record_message(
                recorder,  # type: ignore[arg-type]
                Message(message_type),
                peer=("10.71.1.10", 14601),
                attempt=1,
                nonce="nonce:good:1",
                request_sha256="a" * 64,
                request_mavlink_seq=17,
                packet_sha256="b" * 64,
                rx_datagram_seq=9,
                frame_index=index,
                transaction_id=transaction_id,
            )

        self.assertEqual([event for event, _details in recorder.calls], ["heartbeat", "command_ack", "telemetry"])
        for index, (_event, details) in enumerate(recorder.calls, start=1):
            self.assertEqual(details["rx_datagram_seq"], 9)
            self.assertEqual(details["frame_index"], index)
            self.assertEqual(details["transaction_id"], transaction_id)

    def test_latency_statistics_are_finite_and_nearest_rank(self) -> None:
        result = latency_stats([1.0, 2.0, 3.0, 4.0, 100.0])
        self.assertEqual(result["count"], 5)
        self.assertEqual(result["p50_ms"], 3.0)
        self.assertEqual(result["p95_ms"], 100.0)
        self.assertEqual(latency_stats([])["p95_ms"], None)

    def test_jsonl_writer_appends_one_identity_with_contiguous_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            common = {
                "run_id": "m2-test",
                "runtime_id": "runtime-test",
                "run_nonce": "nonce_0123456789abcdef",
            }
            with JsonlWriter(path, phase="good", **common) as writer:
                writer.emit("phase_start")
            with JsonlWriter(path, phase="down", **common) as writer:
                writer.emit("phase_start")
            records = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([record["event_seq"] for record in records], [1, 2])
            self.assertTrue(all(record["schema_version"] == 2 for record in records))
            with self.assertRaises(ValueError):
                JsonlWriter(
                    path,
                    phase="recovery",
                    run_id="m2-test",
                    runtime_id="another-runtime",
                    run_nonce=common["run_nonce"],
                )

    def test_process_reference_supports_cmdline_hash(self) -> None:
        digest = "a" * 64
        reference = parse_process_reference(f"123:456:{digest}")
        self.assertEqual(reference.pid, 123)
        self.assertEqual(reference.start_ticks, 456)
        self.assertEqual(reference.cmdline_sha256, digest)

    def test_current_process_identity_helpers(self) -> None:
        ticks = read_start_ticks(os.getpid())
        digest = read_cmdline_sha256(os.getpid())
        self.assertIsInstance(ticks, int)
        self.assertIsNotNone(digest)
        self.assertEqual(len(digest), 64)

    def test_down_criteria_rejects_any_heartbeat(self) -> None:
        args = Namespace(expected_ack=False, attempts=5)
        result = PhaseResult(5, 0, 0, 1, True, [], [])
        self.assertFalse(criteria_met(args, result, True))
        result = PhaseResult(5, 0, 0, 0, True, [], [])
        self.assertTrue(criteria_met(args, result, True))
        result = PhaseResult(5, 0, 1, 0, True, [], [])
        self.assertFalse(criteria_met(args, result, True))

    def test_persistent_control_cli_requires_a_phase_only_for_run_phase(self) -> None:
        common = [
            "control",
            "--control-socket",
            "/tmp/m2-gcs.sock",
            "--run-id",
            "m2-test",
            "--runtime-id",
            "runtime-test",
            "--run-nonce",
            "nonce_0123456789abcdef",
        ]
        status = parse_persistent_args([*common, "--operation", "status"])
        self.assertEqual(status.persistent_command, "control")
        self.assertEqual(status.operation, "status")
        process_reference = (
            f"{os.getpid()}:{read_start_ticks(os.getpid())}:{read_cmdline_sha256(os.getpid())}"
        )
        run_phase = parse_persistent_args(
            [
                *common,
                "--phase",
                "good",
                "--expected-ns3-state",
                "up",
                "--ns3-process",
                process_reference,
            ]
        )
        self.assertEqual(run_phase.operation, "run-phase")
        self.assertEqual(run_phase.phase, "good")

    def test_endpoint_event_writer_rejects_duplicate_raw_occurrences(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            event_path = Path(temporary) / "endpoint.jsonl"
            transaction_id = hashlib.sha256(b"transaction").hexdigest()
            with JsonlWriter(
                event_path,
                run_id="m2-test",
                runtime_id="runtime-test",
                run_nonce="nonce_0123456789abcdef",
                phase="endpoint",
            ) as raw_writer:
                writer = EndpointEventWriter(
                    raw_writer,
                    endpoint_instance_id="e" * 64,
                    configuration_fingerprint="c" * 64,
                )
                writer.begin_window("good", "1-good")
                writer.emit("command_attempt", transaction_id=transaction_id)
                writer.emit(
                    "datagram_tx",
                    event_schema=PROBE_RAW_EVENT_SCHEMA,
                    transaction_id=transaction_id,
                    leg="marker",
                    tx_datagram_seq=1,
                )
                with self.assertRaisesRegex(ValueError, "tx_datagram_seq"):
                    writer.emit(
                        "datagram_tx",
                        event_schema=PROBE_RAW_EVENT_SCHEMA,
                        transaction_id=transaction_id,
                        leg="command",
                        tx_datagram_seq=1,
                    )
                writer.close_window(completed=False, reason="test")

    @unittest.skipUnless(
        hasattr(socket, "SOCK_SEQPACKET") and hasattr(socket, "SO_PEERCRED"),
        "persistent endpoint needs Linux AF_UNIX/SOCK_SEQPACKET credentials",
    )
    def test_persistent_endpoint_keeps_one_localhost_udp_identity_across_m2_windows(self) -> None:
        """Exercise the actual UNIX control and localhost UDP transport together."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            control_socket = root / "gcs-control.sock"
            event_log = root / "endpoint.jsonl"
            process_identity = root / "process_identity.json"
            process_event_log = root / "process_events.jsonl"
            receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            receiver.bind(("127.0.0.1", 0))
            receiver.settimeout(1.0)
            received_sources: list[tuple[str, int]] = []
            executor_socket_fds: list[int] = []
            current_ticks = read_start_ticks(os.getpid())
            current_hash = read_cmdline_sha256(os.getpid())
            self.assertIsInstance(current_ticks, int)
            self.assertIsNotNone(current_hash)
            process_identity.write_text(
                json.dumps(
                    {
                        "run_id": "m2-test",
                        "runtime_id": "runtime-test",
                        "run_nonce": "nonce_0123456789abcdef",
                        "processes": [
                            {
                                "role": role,
                                "pid": os.getpid(),
                                "start_ticks": current_ticks,
                                "cmdline_sha256": current_hash,
                            }
                            for role in ("launch", "sitl", "mavproxy", "adapter")
                        ],
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

            def fake_phase_executor(
                args: Namespace,
                writer: EndpointEventWriter,
                gcs_socket: socket.socket,
                sequences: DatagramSequences,
            ) -> PhaseResult:
                executor_socket_fds.append(gcs_socket.fileno())
                transaction_id = hashlib.sha256(
                    f"{args.run_nonce}:{args.phase}:1".encode("ascii")
                ).hexdigest()
                writer.emit("phase_start", attempts=args.attempts, expected_ack=args.expected_ack)
                writer.emit("command_attempt", transaction_id=transaction_id, attempt=1)
                for leg in ("marker", "command"):
                    payload = f"{args.phase}:{leg}".encode("ascii")
                    started = time.monotonic_ns()
                    bytes_sent = gcs_socket.sendto(payload, args.uav_endpoint)
                    completed = time.monotonic_ns()
                    writer.emit(
                        "datagram_tx",
                        event_schema=PROBE_RAW_EVENT_SCHEMA,
                        transaction_id=transaction_id,
                        leg=leg,
                        tx_datagram_seq=sequences.next_tx(),
                        transport_payload_sha256=hashlib.sha256(payload).hexdigest(),
                        transport_payload_size=len(payload),
                        bytes_sent=bytes_sent,
                        destination=list(args.uav_endpoint),
                        send_start_monotonic_ns=started,
                        send_complete_monotonic_ns=completed,
                    )
                    received, source = receiver.recvfrom(65535)
                    self.assertEqual(received, payload)
                    received_sources.append(source)
                    response_payload = b"reply:" + received
                    receiver.sendto(response_payload, source)
                    deadline = time.monotonic() + 1.0
                    while True:
                        try:
                            inbound, peer = gcs_socket.recvfrom(65535)
                            break
                        except BlockingIOError:
                            if time.monotonic() >= deadline:
                                self.fail("persistent endpoint did not retain a receive-capable UDP socket")
                            time.sleep(0.005)
                    writer.emit(
                        "datagram_rx",
                        event_schema=PROBE_RAW_EVENT_SCHEMA,
                        rx_datagram_seq=sequences.next_rx(),
                        transport_payload_sha256=hashlib.sha256(inbound).hexdigest(),
                        transport_payload_size=len(inbound),
                        peer=list(peer),
                        received_monotonic_ns=time.monotonic_ns(),
                    )
                writer.emit(
                    "command_result",
                    transaction_id=transaction_id,
                    attempt=1,
                    ack=args.expected_ack,
                    telemetry=args.expected_ack,
                )
                writer.emit("phase_end", attempts=args.attempts)
                return PhaseResult(
                    attempts=args.attempts,
                    acknowledgements=args.attempts if args.expected_ack else 0,
                    telemetry_responses=args.attempts if args.expected_ack else 0,
                    heartbeat_count=MIN_POSITIVE_HEARTBEATS if args.expected_ack else 0,
                    heartbeat_timeout=not args.expected_ack,
                    ack_latencies_ms=[],
                    telemetry_latencies_ms=[],
                    heartbeat_observation_count=(
                        MIN_POSITIVE_HEARTBEATS if args.expected_ack else 0
                    ),
                    heartbeat_observation_s=(5.0 if args.expected_ack else None),
                )

            endpoint = PersistentGcsEndpoint(
                PersistentEndpointConfig(
                    gcs_bind=("127.0.0.1", 0),
                    uav_endpoint=("127.0.0.1", receiver.getsockname()[1]),
                ),
                control_socket=control_socket,
                event_log=event_log,
                run_id="m2-test",
                runtime_id="runtime-test",
                run_nonce="nonce_0123456789abcdef",
                phase_executor=fake_phase_executor,
                process_identity=process_identity,
                process_event_log=process_event_log,
            )
            endpoint.start()
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as stale_sender:
                stale_sender.sendto(b"stale-before-good", endpoint.bound_gcs)
            server_thread = threading.Thread(target=endpoint.serve_forever, daemon=True)
            server_thread.start()

            def control_request(
                request_id: str,
                operation: str,
                *,
                phase: str | None = None,
                run_id: str = "m2-test",
            ) -> dict[str, object]:
                request: dict[str, object] = {
                    "schema": PERSISTENT_CONTROL_SCHEMA,
                    "run_id": run_id,
                    "runtime_id": "runtime-test",
                    "run_nonce": "nonce_0123456789abcdef",
                    "request_id": request_id,
                    "operation": operation,
                }
                if operation == "run_phase":
                    absent_reference = {
                        "pid": 999999,
                        "start_ticks": 1,
                        "cmdline_sha256": "a" * 64,
                    }
                    live_reference = {
                        "pid": os.getpid(),
                        "start_ticks": current_ticks,
                        "cmdline_sha256": current_hash,
                    }
                    request.update(
                        {
                            "phase": phase,
                            "attempts": 1,
                            "ack_timeout_s": 1.0,
                            "heartbeat_timeout_s": 1.0,
                            "positive_heartbeat_observation_s": 3.0,
                            "expected_ns3_state": "down" if phase == "down" else "up",
                            "ns3_process": (
                                None
                                if phase == "down"
                                else live_reference
                            ),
                            "absent_processes": [absent_reference] if phase != "good" else [],
                            "forbidden_endpoints": [["127.0.0.1", 5760], ["10.72.1.1", 5760]],
                            "forbidden_timeout_s": 0.01,
                        }
                    )
                return send_persistent_control_request(control_socket, request, timeout_s=2.0)

            try:
                with mock.patch(
                    "network.tests.mavlink_vertical_slice_probe.tcp_reachable",
                    return_value=(False, "connection refused"),
                ):
                    wrong_identity = control_request("bad-identity-0001", "status", run_id="other-run")
                    self.assertFalse(wrong_identity["ok"])
                    out_of_order = control_request("down-first-0001", "run_phase", phase="down")
                    self.assertFalse(out_of_order["ok"])

                    good = control_request("good-phase-0001", "run_phase", phase="good")
                    self.assertTrue(good["ok"])
                    self.assertEqual(good["result"]["next_phase"], "down")
                    duplicate = control_request("good-phase-0001", "run_phase", phase="good")
                    self.assertFalse(duplicate["ok"])
                    repeated_phase = control_request("good-again-0001", "run_phase", phase="good")
                    self.assertFalse(repeated_phase["ok"])

                    down = control_request("down-phase-0001", "run_phase", phase="down")
                    recovery = control_request("recovery-phase-0001", "run_phase", phase="recovery")
                    self.assertTrue(down["ok"])
                    self.assertTrue(recovery["ok"])
                    self.assertEqual(recovery["result"]["next_phase"], None)
                    shutdown = control_request("shutdown-0001", "shutdown")
                    self.assertTrue(shutdown["ok"])
                server_thread.join(timeout=2.0)
                self.assertFalse(server_thread.is_alive())
            finally:
                endpoint.close()
                server_thread.join(timeout=2.0)
                receiver.close()

            records = [json.loads(line) for line in event_log.read_text().splitlines()]
            self.assertEqual(sum(record["event"] == "endpoint_started" for record in records), 1)
            self.assertEqual(sum(record["event"] == "endpoint_stopped" for record in records), 1)
            self.assertEqual(
                [record["phase"] for record in records if record["event"] == "endpoint_window_open"],
                ["good", "down", "recovery"],
            )
            self.assertEqual(
                [record["phase"] for record in records if record["event"] == "endpoint_window_close"],
                ["good", "down", "recovery"],
            )
            tx = [record for record in records if record["event"] == "datagram_tx"]
            rx = [record for record in records if record["event"] == "datagram_rx"]
            drained = [record for record in records if record["event"] == "endpoint_pre_window_datagram"]
            self.assertEqual([record["tx_datagram_seq"] for record in tx], list(range(1, 7)))
            self.assertEqual([record["rx_datagram_seq"] for record in drained], [1])
            self.assertEqual([record["rx_datagram_seq"] for record in rx], list(range(2, 8)))
            self.assertEqual(drained[0]["pre_window_for_phase"], "good")
            self.assertEqual(drained[0]["disposition"], "discarded_before_window")
            self.assertEqual(
                [record["pre_window_for_phase"] for record in records if record["event"] == "endpoint_pre_window_quiescent"],
                ["good", "down", "recovery"],
            )
            self.assertEqual(len({record["endpoint_instance_id"] for record in records}), 1)
            self.assertTrue(
                all(record["endpoint_event_schema"] == PERSISTENT_ENDPOINT_EVENT_SCHEMA for record in records)
            )
            self.assertEqual(len(set(executor_socket_fds)), 1)
            self.assertEqual(len({source[1] for source in received_sources}), 1)
            self.assertFalse(control_socket.exists())
            direct = [record for record in records if record["event"] == "direct_endpoint_probe"]
            self.assertEqual(len(direct), 6)
            self.assertTrue(all(record["reachable"] is False for record in direct))
            health = [record for record in records if record["event"] == "endpoint_health"]
            self.assertEqual(len(health), 3)
            self.assertTrue(all(record["all_live"] is True for record in health))
            process_records = [json.loads(line) for line in process_event_log.read_text().splitlines()]
            for phase in ("good", "down", "recovery"):
                roles = {
                    record["role"]
                    for record in process_records
                    if record["phase"] == phase and record["event"] == "process_snapshot"
                }
                self.assertEqual(roles, {"uav_adapter", "mavproxy", "sitl", "gcs_probe", "ns3"})


if __name__ == "__main__":
    unittest.main()
