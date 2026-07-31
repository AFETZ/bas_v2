#!/usr/bin/env python3
"""Concurrency and loopback-TCP tests for the async Sionna v1 service."""

from __future__ import annotations

import copy
import hashlib
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from network.radio_provider.sionna_async import (  # noqa: E402
    ProtocolIdentity,
    ProtocolLimits,
    ProtocolStateError,
    decode_message,
    encode_message,
    node_state_sha256,
)
from network.radio_provider.sionna_async_service import (  # noqa: E402
    AsyncServiceError,
    AsyncSionnaWorker,
    ComputeCompletion,
    ExactWireLog,
    ProviderServiceConfig,
    RealSionnaBackend,
    SionnaAsyncTCPService,
    WorkerFault,
    create_production_worker,
)
from network.radio_provider.provider import RuntimeFiles  # noqa: E402
from network.scripts import m4_runtime_orchestrator  # noqa: E402
from network.tests.test_sionna_async_protocol import (  # noqa: E402
    HASH_A,
    HASH_C,
    HASH_D,
    disconnect,
    hello,
    physical,
    query,
    ready,
)


def live_query(
    query_id: str = "query-live-1",
    node_state_seq: int = 1,
    *,
    wire_sequence: int = 3,
    deadline_offset_ns: int = 2_000_000_000,
) -> dict:
    message = query(query_id, node_state_seq, wire_sequence=wire_sequence)
    now = time.monotonic_ns()
    pose_time = now - 20_000_000
    snapshot_time = now - 10_000_000
    generated_time = now - 5_000_000
    sent_time = now - 1_000_000
    for pose in [*message["nodes"], *message["jammers"]]:
        pose["pose_monotonic_ns"] = pose_time
        pose["freshness_age_ns"] = sent_time - pose_time
    message["node_state_snapshot_monotonic_ns"] = snapshot_time
    message["source_pose_monotonic_ns"] = pose_time
    message["request_generated_monotonic_ns"] = generated_time
    message["request_sent_monotonic_ns"] = sent_time
    message["emitted_monotonic_ns"] = sent_time
    message["deadline_monotonic_ns"] = now + deadline_offset_ns
    message["node_state_sha256"] = node_state_sha256(
        node_state_seq=message["node_state_seq"],
        snapshot_monotonic_ns=snapshot_time,
        source_frame=message["source_frame"],
        transform_version=message["transform_version"],
        nodes=message["nodes"],
        jammers=message["jammers"],
    )
    return message


def service_config(*, test_only: bool = True) -> ProviderServiceConfig:
    identity = ProtocolIdentity.from_message(query())
    return ProviderServiceConfig(
        identity=identity,
        phase_id="phase-main",
        sender_id="provider-a",
        clock_domain="host-monotonic",
        executable_path="/opt/ams/sionna-provider",
        executable_sha256=HASH_C,
        scene_path="/opt/ams/scenes/rock-v2.xml",
        scene_manifest_sha256=HASH_D,
        scene_material_manifest_sha256=HASH_D,
        provider_id="sionna-gpu-0",
        sionna_rt_version="test-version" if test_only else "1.2.0",
        mitsuba_version="test-version" if test_only else "3.6.4",
        provider_mode="deterministic_test_injection" if test_only else "real_sionna",
        acceptance_eligible=not test_only,
    )


def wait_until(predicate, timeout_s: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.002)
    return bool(predicate())


def recv_message(stream) -> tuple[dict, bytes]:
    raw = stream.readline()
    if not raw:
        raise AssertionError("connection closed before a JSONL frame arrived")
    return dict(decode_message(raw)), raw


class WorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.identity = ProtocolIdentity.from_message(query())
        self.workers: list[AsyncSionnaWorker] = []

    def make_worker(
        self, compute, limits: ProtocolLimits | None = None
    ) -> AsyncSionnaWorker:
        worker = AsyncSionnaWorker.for_unit_tests(
            compute,
            self.identity,
            HASH_D,
            limits=limits,
        )
        worker.start()
        self.workers.append(worker)
        return worker

    def tearDown(self) -> None:
        for worker in self.workers:
            worker.shutdown(wait=True, timeout_s=0.2)

    def test_slow_compute_never_blocks_submit_or_poll_completed(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def slow(_query):
            entered.set()
            release.wait(1.0)
            return physical()

        worker = self.make_worker(slow)
        before = time.perf_counter()
        decision = worker.submit(live_query(), connection_id="conn-slow")
        submit_elapsed = time.perf_counter() - before
        self.assertTrue(decision.accepted)
        self.assertLess(submit_elapsed, 0.05)
        self.assertTrue(entered.wait(0.5))

        before = time.perf_counter()
        self.assertEqual(worker.poll_completed(), ())
        poll_elapsed = time.perf_counter() - before
        self.assertLess(poll_elapsed, 0.02)

        release.set()
        items: list = []

        def completed() -> bool:
            items.extend(worker.poll_completed())
            return bool(items)

        self.assertTrue(wait_until(completed))
        self.assertIsInstance(items[0], ComputeCompletion)
        self.assertEqual(items[0].status, "ok")

    def test_request_queue_overflow_returns_immediate_failed_completion(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def slow(_query):
            entered.set()
            release.wait(1.0)
            return physical()

        limits = ProtocolLimits(request_queue_capacity=1, completion_queue_capacity=8)
        worker = self.make_worker(slow, limits)
        self.assertTrue(
            worker.submit(live_query("query-1", 1), connection_id="c").accepted
        )
        self.assertTrue(entered.wait(0.5))
        self.assertTrue(
            worker.submit(live_query("query-2", 2), connection_id="c").accepted
        )
        overflow = worker.submit(live_query("query-3", 3), connection_id="c")
        self.assertFalse(overflow.accepted)
        self.assertEqual(overflow.reason, "request_queue_overflow")
        completed = worker.poll_completed()
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0].status, "provider_error")
        self.assertIn("request queue overflow", completed[0].detail)
        release.set()

    def test_completion_queue_overflow_latches_fatal_fault_without_blocking(
        self,
    ) -> None:
        limits = ProtocolLimits(
            request_queue_capacity=4,
            completion_queue_capacity=1,
            max_poll_batch=4,
        )
        worker = self.make_worker(lambda _query: physical(), limits)
        worker.submit(live_query("query-1", 1), connection_id="c")
        worker.submit(live_query("query-2", 2), connection_id="c")
        self.assertTrue(wait_until(lambda: worker.fatal_reason is not None))
        self.assertEqual(worker.fatal_reason, "completion_queue_overflow")
        items = worker.poll_completed(max_items=4)
        self.assertTrue(any(isinstance(item, ComputeCompletion) for item in items))
        self.assertTrue(any(isinstance(item, WorkerFault) for item in items))
        rejected = worker.submit(live_query("query-3", 3), connection_id="c")
        self.assertFalse(rejected.accepted)

    def test_deadline_stale_pose_and_scene_mismatch_fail_before_compute(self) -> None:
        calls = 0

        def compute(_query):
            nonlocal calls
            calls += 1
            return physical()

        worker = self.make_worker(compute)
        expired = live_query("expired", 1)
        expired["deadline_monotonic_ns"] = (
            expired["request_sent_monotonic_ns"] + 1
        )
        stale = live_query("stale", 2)
        stale["nodes"][0]["stale"] = True
        mismatch = live_query("mismatch", 3)
        mismatch["material_assumptions"]["scene_material_manifest_sha256"] = HASH_C
        decisions = [
            worker.submit(
                expired,
                connection_id="c",
                received_monotonic_ns=expired["deadline_monotonic_ns"],
            ),
            worker.submit(stale, connection_id="c"),
            worker.submit(mismatch, connection_id="c"),
        ]
        self.assertEqual(
            [decision.reason for decision in decisions],
            ["deadline_missed", "stale_pose", "scene_mismatch"],
        )
        completions = worker.poll_completed(max_items=4)
        self.assertEqual(
            [item.status for item in completions],
            ["deadline_missed", "stale_pose", "scene_mismatch"],
        )
        self.assertEqual(calls, 0)
        self.assertTrue(all(item.physical is None for item in completions))
        wrong_clock = live_query("wrong-clock", 4)
        wrong_clock["sender_clock_domain"] = "foreign-monotonic"
        with self.assertRaisesRegex(ProtocolStateError, "clock domain mismatch"):
            worker.submit(wrong_clock, connection_id="c")

    def test_compute_finishing_after_deadline_is_fail_closed(self) -> None:
        def slow(_query):
            time.sleep(0.03)
            return physical()

        worker = self.make_worker(slow)
        worker.submit(
            live_query("deadline-during-compute", 1, deadline_offset_ns=5_000_000),
            connection_id="c",
        )
        items: list = []

        def completed() -> bool:
            items.extend(worker.poll_completed())
            return bool(items)

        self.assertTrue(wait_until(completed))
        self.assertEqual(items[0].status, "deadline_missed")
        self.assertIsNone(items[0].physical)

    def test_provider_recomputes_pose_age_at_receive_time(self) -> None:
        worker = self.make_worker(
            lambda _query: physical(),
            ProtocolLimits(max_pose_age_ns=25_000_000),
        )
        fresh_when_sent = live_query("receive-age", 1)
        self.assertLess(
            fresh_when_sent["nodes"][0]["freshness_age_ns"],
            worker.limits.max_pose_age_ns,
        )
        received = (
            fresh_when_sent["nodes"][0]["pose_monotonic_ns"]
            + worker.limits.max_pose_age_ns
            + 1
        )
        decision = worker.submit(
            fresh_when_sent,
            connection_id="c",
            received_monotonic_ns=received,
        )
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "stale_pose")
        completion = worker.poll_completed()[0]
        self.assertEqual(completion.status, "stale_pose")
        self.assertIn("provider receive time", completion.detail)

    def test_shutdown_drains_queued_work_as_failures(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def slow(_query):
            entered.set()
            release.wait(1.0)
            return physical()

        worker = self.make_worker(slow, ProtocolLimits(completion_queue_capacity=8))
        worker.submit(live_query("running", 1), connection_id="c")
        self.assertTrue(entered.wait(0.5))
        worker.submit(live_query("queued", 2), connection_id="c")
        before = time.perf_counter()
        worker.shutdown(wait=False)
        self.assertLess(time.perf_counter() - before, 0.05)
        first = worker.poll_completed()
        self.assertEqual(first[0].query["query_id"], "queued")
        self.assertEqual(first[0].status, "provider_error")
        release.set()
        self.assertTrue(
            wait_until(
                lambda: any(
                    isinstance(item, ComputeCompletion)
                    and item.query["query_id"] == "running"
                    and item.status == "provider_error"
                    for item in worker.poll_completed()
                )
            )
        )

    def test_production_constructors_reject_non_real_backend(self) -> None:
        class FakeBackend:
            provider_mode = "test_free_space"
            acceptance_eligible = False

            def compute(self, _query):
                return physical()

        with self.assertRaisesRegex(AsyncServiceError, "real_sionna"):
            AsyncSionnaWorker(FakeBackend(), self.identity, HASH_D)

        class FakeProvider:
            acceptance_eligible = False

            class Settings:
                mode = "test_free_space"

            settings = Settings()

        with self.assertRaisesRegex(AsyncServiceError, "real_sionna"):
            RealSionnaBackend(FakeProvider())

    def test_real_backend_requires_and_preserves_exact_path_evidence(self) -> None:
        link = {
            "pathloss_db": 89.0,
            "propagation_delay_ns": 166.8,
            "path_count": 1,
            "path_type_counts": {
                "los": 1,
                "specular": 0,
                "diffuse": 0,
                "refracted": 0,
                "diffracted": 0,
                "mixed": 0,
            },
            "geometry_state": "los",
            "rssi_dbm": -66.0,
            "sinr_db": 18.5,
            "js_db": -27.0,
            "stale": False,
        }

        class FakeProvider:
            settings = SimpleNamespace(mode="real_sionna")
            acceptance_eligible = True

            def query(self, _request):
                return {"links": [copy.deepcopy(link)]}

        backend = RealSionnaBackend(FakeProvider())
        output = backend.compute(query())
        self.assertEqual(output["geometry_state"], "los")
        self.assertEqual(output["path_count"], 1)
        self.assertEqual(output["path_type_counts"], link["path_type_counts"])
        self.assertEqual(output["propagation_delay_ns"], 166.8)

        for mutation in (
            {"geometry_state": "unclassified"},
            {"path_count": 2},
            {"geometry_state": "blocked_no_path"},
        ):
            bad_link = copy.deepcopy(link)
            bad_link.update(mutation)

            class BadProvider:
                settings = SimpleNamespace(mode="real_sionna")
                acceptance_eligible = True

                def query(self, _request):
                    return {"links": [bad_link]}

            with self.subTest(mutation=mutation):
                with self.assertRaises(AsyncServiceError):
                    RealSionnaBackend(BadProvider()).compute(query())

    def test_real_backend_warm_up_requires_fresh_real_links(self) -> None:
        response = {"type": "link_state", "links": [{"stale": False}]}
        provider = SimpleNamespace(
            settings=SimpleNamespace(mode="real_sionna"),
            acceptance_eligible=True,
            query=mock.Mock(return_value=response),
        )
        backend = RealSionnaBackend(provider)
        request = {"type": "link_query", "links": [{"tx": "cp", "rx": "uav1"}]}
        self.assertEqual(backend.warm_up(request), response)
        provider.query.assert_called_once_with(request)

        for invalid in (
            {"type": "link_state", "links": []},
            {"type": "link_state", "links": [{"stale": True}]},
        ):
            provider.query.reset_mock(return_value=True)
            provider.query.return_value = invalid
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                AsyncServiceError, "warm-up"
            ):
                backend.warm_up(request)

    def test_m4_starts_service_only_after_real_backend_warm_up(self) -> None:
        events: list[object] = []
        response = {"type": "link_state", "links": [{"stale": False}]}
        provider = SimpleNamespace(
            settings=SimpleNamespace(mode="real_sionna"),
            acceptance_eligible=True,
            query=mock.Mock(
                side_effect=lambda request: events.append(("warm", request))
                or response
            ),
        )
        backend = RealSionnaBackend(provider)

        class Service:
            worker = SimpleNamespace(backend=backend)

            def start(self) -> None:
                events.append("start")

        files = RuntimeFiles(
            scenario=Path("scenario"),
            radio=Path("radio"),
            jammers=Path("jammers"),
            service_tiers=Path("tiers"),
        )
        with mock.patch.object(
            m4_runtime_orchestrator,
            "build_sample_request",
            return_value={"type": "link_query", "deadline_ms": 1},
        ) as sample:
            m4_runtime_orchestrator._start_warmed_provider_service(Service(), files)
        sample.assert_called_once_with(
            files,
            include_jammers=True,
            all_uavs=False,
            traffic_class="control",
        )
        self.assertEqual(
            events,
            [("warm", {"type": "link_query", "deadline_ms": 30_000}), "start"],
        )

    def test_production_factory_calls_only_real_provider_path(self) -> None:
        from network.radio_provider import provider as provider_module

        real_settings = SimpleNamespace(mode="real_sionna")
        real_instance = SimpleNamespace(
            settings=real_settings,
            acceptance_eligible=True,
            query=lambda _request: {},
        )
        with (
            mock.patch.object(
                provider_module, "load_settings", return_value=real_settings
            ) as load_settings,
            mock.patch.object(
                provider_module, "SionnaRadioProvider", return_value=real_instance
            ) as provider_class,
        ):
            worker = create_production_worker(
                runtime_files=object(),
                identity=self.identity,
                scene_material_manifest_sha256=HASH_D,
            )
        load_settings.assert_called_once()
        self.assertEqual(load_settings.call_args.args[1], "real_sionna")
        provider_class.assert_called_once_with(real_settings)
        self.assertFalse(worker.test_only)


class ExactWireLogTests(unittest.TestCase):
    def test_log_preserves_exact_bytes_offsets_and_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log = ExactWireLog(Path(temp_dir))
            frames = [b'{"b":2,"a":1}\n', b"raw\x00bytes\xff\n"]
            records = [
                log.record("inbound", "conn-1", frame, 100 + index)
                for index, frame in enumerate(frames)
            ]
            self.assertEqual(tuple(records), log.read_records())
            for frame, record in zip(frames, records):
                self.assertEqual(log.read_exact(record), frame)
                self.assertEqual(record["sha256"], hashlib.sha256(frame).hexdigest())
            self.assertEqual(records[1]["offset"], len(frames[0]))


class TCPServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.services: list[SionnaAsyncTCPService] = []

    def tearDown(self) -> None:
        for service in self.services:
            service.stop(timeout_s=0.5)
        self.temp.cleanup()

    def make_service(self, compute=lambda _query: physical()):
        config = service_config()
        worker = AsyncSionnaWorker.for_unit_tests(
            compute,
            config.identity,
            HASH_D,
            limits=ProtocolLimits(
                request_queue_capacity=8,
                completion_queue_capacity=8,
                validity_ttl_ns=1_000_000_000,
            ),
        )
        log = ExactWireLog(Path(self.temp.name) / f"log-{len(self.services)}")
        service = SionnaAsyncTCPService.for_unit_tests(config, worker, log)
        service.start()
        self.services.append(service)
        return service, log

    def connect(self, service: SionnaAsyncTCPService):
        sock = socket.create_connection(("127.0.0.1", service.port), timeout=1.0)
        sock.settimeout(1.0)
        return sock, sock.makefile("rb")

    def send_adapter_handshake(
        self,
        sock: socket.socket,
        *,
        generation: int = 0,
        first_sequence: int = 1,
    ) -> tuple[bytes, bytes]:
        first = hello(wire_sequence=first_sequence, generation=generation)
        second = ready(wire_sequence=first_sequence + 1, generation=generation)
        first_raw = encode_message(first)
        second_raw = encode_message(second)
        sock.sendall(first_raw + second_raw)
        return first_raw, second_raw

    def test_tcp_handshake_query_result_and_exact_wire_log(self) -> None:
        service, log = self.make_service()
        sock, stream = self.connect(service)
        try:
            provider_hello, provider_hello_raw = recv_message(stream)
            provider_ready, provider_ready_raw = recv_message(stream)
            self.assertEqual(provider_hello["message_type"], "hello")
            self.assertEqual(provider_ready["message_type"], "ready")
            self.assertFalse(provider_hello["provider_identity"]["acceptance_eligible"])
            adapter_raw = self.send_adapter_handshake(sock)
            q = live_query()
            query_raw = encode_message(q)
            sock.sendall(query_raw)
            response, response_raw = recv_message(stream)
            self.assertEqual(response["message_type"], "result")
            self.assertEqual(response["status"], "ok")
            self.assertEqual(response["query_id"], q["query_id"])
            self.assertGreater(
                response["expires_monotonic_ns"],
                response["validity_start_monotonic_ns"],
            )

            expected_frames = {
                provider_hello_raw,
                provider_ready_raw,
                adapter_raw[0],
                adapter_raw[1],
                query_raw,
                response_raw,
            }
            self.assertTrue(wait_until(lambda: len(log.read_records()) >= 6))
            recorded_frames = {log.read_exact(record) for record in log.read_records()}
            self.assertTrue(expected_frames <= recorded_frames)
            for record in log.read_records():
                raw = log.read_exact(record)
                self.assertEqual(record["sha256"], hashlib.sha256(raw).hexdigest())
        finally:
            stream.close()
            sock.close()

    def test_provider_wire_sequence_never_resets_across_reconnect(self) -> None:
        service, _log = self.make_service()
        first_sock, first_stream = self.connect(service)
        first_hello, _ = recv_message(first_stream)
        first_ready, _ = recv_message(first_stream)
        self.send_adapter_handshake(first_sock, generation=0, first_sequence=1)
        time.sleep(0.03)
        first_stream.close()
        first_sock.close()

        second_sock, second_stream = self.connect(service)
        try:
            second_hello, _ = recv_message(second_stream)
            second_ready, _ = recv_message(second_stream)
            self.assertEqual(first_hello["reconnect_generation"], 0)
            self.assertEqual(second_hello["reconnect_generation"], 1)
            self.assertGreater(
                second_hello["wire_sequence"], first_ready["wire_sequence"]
            )
            self.assertGreater(
                second_ready["wire_sequence"], second_hello["wire_sequence"]
            )
            self.send_adapter_handshake(second_sock, generation=1, first_sequence=3)
            second_sock.sendall(
                encode_message(
                    disconnect(
                        wire_sequence=5,
                        generation=1,
                        owned_links=[],
                    )
                )
            )
            provider_disconnect, _ = recv_message(second_stream)
            self.assertEqual(provider_disconnect["message_type"], "disconnect")
            self.assertGreater(
                provider_disconnect["wire_sequence"], second_ready["wire_sequence"]
            )
        finally:
            second_stream.close()
            second_sock.close()

    def test_wrong_identity_yields_error_then_fail_closed_disconnect(self) -> None:
        calls = 0

        def compute(_query):
            nonlocal calls
            calls += 1
            return physical()

        service, _log = self.make_service(compute)
        sock, stream = self.connect(service)
        try:
            recv_message(stream)
            recv_message(stream)
            wrong = hello()
            wrong["config_hash"] = HASH_A
            wrong["accepted_config_hash"] = HASH_A
            sock.sendall(encode_message(wrong))
            error_message, _ = recv_message(stream)
            disconnected, _ = recv_message(stream)
            self.assertEqual(error_message["message_type"], "error")
            self.assertIn("identity mismatch", error_message["reason"])
            self.assertEqual(disconnected["message_type"], "disconnect")
            self.assertEqual(calls, 0)
        finally:
            stream.close()
            sock.close()

    def test_slow_compute_does_not_block_network_disconnect(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def slow(_query):
            entered.set()
            release.wait(1.0)
            return physical()

        service, _log = self.make_service(slow)
        sock, stream = self.connect(service)
        try:
            recv_message(stream)
            recv_message(stream)
            self.send_adapter_handshake(sock)
            sock.sendall(encode_message(live_query()))
            self.assertTrue(entered.wait(0.5))
            before = time.perf_counter()
            sock.sendall(encode_message(disconnect(wire_sequence=4, owned_links=[])))
            response, _ = recv_message(stream)
            self.assertEqual(response["message_type"], "disconnect")
            self.assertLess(time.perf_counter() - before, 0.2)
        finally:
            release.set()
            stream.close()
            sock.close()

    def test_slow_compute_emits_deadline_missed_without_physical_state(self) -> None:
        def slow(_query):
            time.sleep(0.03)
            return physical()

        service, _log = self.make_service(slow)
        sock, stream = self.connect(service)
        try:
            recv_message(stream)
            recv_message(stream)
            self.send_adapter_handshake(sock)
            sock.sendall(
                encode_message(
                    live_query(
                        "deadline-tcp",
                        1,
                        deadline_offset_ns=5_000_000,
                    )
                )
            )
            response, _ = recv_message(stream)
            self.assertEqual(response["status"], "deadline_missed")
            self.assertNotIn("physical", response)
            self.assertEqual(response["error_body"]["code"], "deadline_missed")
        finally:
            stream.close()
            sock.close()

    def test_tcp_stale_pose_and_scene_mismatch_are_link_failures(self) -> None:
        service, _log = self.make_service()
        sock, stream = self.connect(service)
        try:
            recv_message(stream)
            recv_message(stream)
            self.send_adapter_handshake(sock)
            stale = live_query("stale-tcp", 1, wire_sequence=3)
            stale["nodes"][0]["stale"] = True
            mismatch = live_query("scene-tcp", 2, wire_sequence=4)
            mismatch["material_assumptions"]["scene_material_manifest_sha256"] = HASH_C
            sock.sendall(encode_message(stale) + encode_message(mismatch))
            first, _ = recv_message(stream)
            second, _ = recv_message(stream)
            self.assertEqual(
                {first["status"], second["status"]},
                {"stale_pose", "scene_mismatch"},
            )
            for response in (first, second):
                self.assertNotIn("physical", response)
                self.assertEqual(response["error_body"]["code"], response["status"])
        finally:
            stream.close()
            sock.close()


if __name__ == "__main__":
    unittest.main()
