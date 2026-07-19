#!/usr/bin/env python3
"""Adversarial tests for packet-event to Sionna applied-state IPC."""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from network.radio_provider.sionna_async import (  # noqa: E402
    ProtocolIdentity,
    encode_message,
)
from network.radio_provider.sionna_packet_adapter import (  # noqa: E402
    AppliedStateIPCWriter,
    ClientFault,
    FIXED_QUERY_CELLS,
    FIXED_QUERY_SLOT_COUNT_PER_CELL,
    PacketAdapterConfig,
    PacketAdapterError,
    PacketEffectsPolicy,
    PacketEventTailer,
    PacketSionnaAdapter,
    PoseSnapshot,
    SupervisedResultFaultInjector,
    deterministic_loss_sample,
    packet_delivery_decision,
)
from network.tests.test_sionna_async_protocol import physical, query, result  # noqa: E402
from network.validation.m4_runtime import validate_capacity_freshness  # noqa: E402


class FakeTransport:
    def __init__(self) -> None:
        self.ready = True
        self.sequence = 2
        self.generation = 0
        self.submitted: list[dict] = []
        self.results: list[bytes | ClientFault] = []
        self.accept_submissions = True
        self.armed_hold_links: list[str] = []

    def arm_hold_next(self, directed_link_id: str) -> None:
        self.armed_hold_links.append(directed_link_id)

    def reserve_query_envelope(self):
        if not self.ready:
            return None
        self.sequence += 1
        return self.sequence, self.generation

    def submit_query(self, query_message):
        if not self.accept_submissions:
            return False
        self.submitted.append(copy.deepcopy(query_message))
        return True

    def poll_results(self, max_items=64):
        output = tuple(self.results[:max_items])
        del self.results[:max_items]
        return output


def packet_event(
    sequence: int = 1,
    *,
    link: str = "cp>uav1",
    traffic_class: str = "control",
    event: str = "ingress",
) -> dict:
    causal = hashlib.sha256(f"payload-{sequence}".encode()).hexdigest()
    return {
        "schema": "ams.ns3.packet_event/v1",
        "event_epoch": 7,
        "event_sequence": sequence,
        "sim_time_ns": sequence * 1_000_000,
        "event": event,
        "packet_wire_hash": hashlib.sha256(f"wire-{sequence}".encode()).hexdigest(),
        "packet_wire_size": 128,
        "packet_uid": sequence + 1000,
        "traffic_class": traffic_class,
        "directed_link": link,
        "transport_protocol": 17,
        "source_udp_port": 14000,
        "destination_udp_port": 14001,
        "transport_payload_sha256": causal,
        "transport_payload_size": 64,
    }


def poses(now: int = 1_000_000_000) -> PoseSnapshot:
    nodes = []
    for index, node_id in enumerate(("cp", "uav1", "uav2", "uav3", "uav4", "uav5")):
        nodes.append(
            {
                "node_id": node_id,
                "role": "command_post" if node_id == "cp" else "uav",
                "pose_monotonic_ns": now - 100,
                "source_topic": f"/{node_id}/odometry",
                "source_frame": "enu",
                "transform_version": "enu-to-sionna-v1",
                "position_m": [float(index * 50), 0.0, 20.0],
                "orientation_quat_xyzw": [0.0, 0.0, 0.0, 1.0],
                "freshness_age_ns": 0,
                "stale": False,
            }
        )
    return PoseSnapshot.create(
        snapshot_sequence=max(1, now // 1_000_000_000),
        snapshot_monotonic_ns=now,
        source_frame="enu",
        transform_version="enu-to-sionna-v1",
        nodes=tuple(nodes),
        jammers=(),
    )


def adapter_config() -> PacketAdapterConfig:
    template = query()
    return PacketAdapterConfig(
        identity=ProtocolIdentity.from_message(template),
        phase_id="phase-main",
        sender_id="adapter-a",
        provider_sender_id="provider-a",
        clock_domain="host-monotonic",
        query_deadline_ns=1_000_000,
        mapping_seed=42,
        source_frame="enu",
        transform_version="enu-to-sionna-v1",
        radio_assumptions=copy.deepcopy(template["radio_assumptions"]),
        antenna_assumptions=copy.deepcopy(template["antenna_assumptions"]),
        material_assumptions=copy.deepcopy(template["material_assumptions"]),
        mapping_version="sinr-rate-per-v2",
    )


def provider_result(source_query: dict, wire_sequence: int, now: int) -> dict:
    message = result(source_query, wire_sequence=wire_sequence)
    message["provider_received_monotonic_ns"] = now + 1
    message["provider_started_monotonic_ns"] = now + 2
    message["provider_completed_monotonic_ns"] = now + 10
    message["provider_sent_monotonic_ns"] = now + 11
    message["emitted_monotonic_ns"] = now + 11
    message["validity_start_monotonic_ns"] = now + 10
    message["expires_monotonic_ns"] = now + 1_000_000
    return message


def capacity_slot_evidence(start_ns: int) -> tuple[dict, dict, dict, dict]:
    """Build exact wire/state/audit evidence for all 18,000 slots."""

    provider_identity = {
        "provider_id": "sionna-provider-m4",
        "provider_mode": "real_sionna",
        "acceptance_eligible": True,
        "sionna_rt_version": "1.0",
        "mitsuba_version": "3.0",
    }
    identity = {
        "run_id": "capacity-run",
        "profile": "m4_capacity_prerequisite",
        "phase_id": "m4_continuous_runtime",
        "contract_hash": "c" * 64,
        "config_hash": "d" * 64,
        "bundle_id": "ams-m4-canonical-km-v2",
    }
    messages: list[dict] = [
        {
            "message_type": "hello",
            "sender_id": "provider",
            "wire_sequence": 0,
            "sender_role": "provider",
            "provider_identity": provider_identity,
        }
    ]
    message_by_hash: dict[str, dict] = {}
    states: list[dict] = []
    audits: list[dict] = []
    shared_physical = physical()
    wire_sequence = 0
    for cell_index, (link, traffic_class) in enumerate(FIXED_QUERY_CELLS):
        source, destination = link.split(">", 1)
        for ordinal in range(1, FIXED_QUERY_SLOT_COUNT_PER_CELL + 1):
            scheduled_ns = (
                start_ns
                + (ordinal - 1) * 1_000_000_000
                + cell_index * 33_333_333
            )
            sent_ns = scheduled_ns + 1_000_000
            query_id = (
                f"capacity.{link.replace('>', '-to-')}.{traffic_class}."
                f"q{ordinal}.slot{ordinal}.s{ordinal}"
            )
            poses_for_query = [
                {
                    "node_id": node_id,
                    "pose_monotonic_ns": sent_ns - 100,
                    "freshness_age_ns": 100,
                    "stale": False,
                }
                for node_id in ("cp", "uav1", "uav2", "uav3", "uav4", "uav5")
            ]
            jammer = {
                "jammer_id": "jammer_m4",
                "pose_monotonic_ns": sent_ns - 100,
                "freshness_age_ns": 100,
                "stale": False,
            }
            wire_sequence += 1
            node_hash = hashlib.sha256(
                f"node-state:{link}:{traffic_class}:{ordinal}".encode()
            ).hexdigest()
            directed_link_id = (
                f"{source}-to-{destination}-{traffic_class}"
            )
            query_message = {
                **identity,
                "message_type": "query",
                "sender_id": "adapter",
                "sender_clock_domain": "host-monotonic",
                "wire_sequence": wire_sequence,
                "query_id": query_id,
                "node_state_seq": ordinal,
                "node_state_sha256": node_hash,
                "directed_link_id": directed_link_id,
                "tx_node_id": source,
                "rx_node_id": destination,
                "traffic_class": traffic_class,
                "request_sent_monotonic_ns": sent_ns,
                "deadline_monotonic_ns": sent_ns + 100_000_000,
                "nodes": poses_for_query,
                "jammers": [jammer],
            }
            result_message = {
                **identity,
                "message_type": "result",
                "sender_id": "provider",
                "sender_clock_domain": "host-monotonic",
                "wire_sequence": wire_sequence,
                "query_id": query_id,
                "node_state_seq": ordinal,
                "directed_link_id": directed_link_id,
                "tx_node_id": source,
                "rx_node_id": destination,
                "traffic_class": traffic_class,
                "status": "ok",
                "provider_clock_domain": "host-monotonic",
                "validity_clock_domain": "host-monotonic",
                "provider_received_monotonic_ns": sent_ns + 1,
                "provider_started_monotonic_ns": sent_ns + 1,
                "provider_completed_monotonic_ns": sent_ns + 2,
                "provider_sent_monotonic_ns": sent_ns + 2,
                "emitted_monotonic_ns": sent_ns + 2,
                "validity_start_monotonic_ns": sent_ns + 2,
                "expires_monotonic_ns": sent_ns + 2_000_000_002,
                "physical": shared_physical,
            }
            query_raw = (
                json.dumps(
                    query_message,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode()
            result_raw = (
                json.dumps(
                    result_message,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode()
            query_wire_sha256 = hashlib.sha256(query_raw).hexdigest()
            result_wire_sha256 = hashlib.sha256(result_raw).hexdigest()
            message_by_hash[query_wire_sha256] = query_message
            message_by_hash[result_wire_sha256] = result_message
            messages.extend((query_message, result_message))
            adapter_received_ns = sent_ns + 3
            adapter_applied_ns = sent_ns + 4
            applied_state_id = f"applied-{query_id}"
            states.append(
                {
                    **{
                        key: identity[key]
                        for key in ("run_id", "profile", "phase_id")
                    },
                    "availability": "fresh",
                    "directed_link": link,
                    "traffic_class": traffic_class,
                    "query_id": query_id,
                    "node_state_seq": ordinal,
                    "node_state_sha256": node_hash,
                    "query_wire_sha256": query_wire_sha256,
                    "result_wire_sha256": result_wire_sha256,
                    "applied_state_id": applied_state_id,
                    "validity_start_monotonic_ns": sent_ns + 2,
                    "expires_monotonic_ns": sent_ns + 2_000_000_002,
                    "adapter_applied_monotonic_ns": adapter_applied_ns,
                    "physical": shared_physical,
                }
            )
            slot_common = {
                "directed_link": link,
                "traffic_class": traffic_class,
                "query_id": query_id,
                "query_slot_ordinal": ordinal,
                "query_slot_scheduled_monotonic_ns": scheduled_ns,
                "query_slot_deadline_monotonic_ns": scheduled_ns + 100_000_000,
            }
            audits.extend(
                (
                    {
                        **slot_common,
                        "event": "query_submitted",
                        "decision": "fixed_slot",
                        "query_wire_sha256": query_wire_sha256,
                    },
                    {
                        **slot_common,
                        "event": "result_received",
                        "decision": "pending",
                        "result_wire_sha256": result_wire_sha256,
                        "adapter_received_monotonic_ns": adapter_received_ns,
                    },
                    {
                        **slot_common,
                        "event": "result_applied",
                        "decision": "applied",
                        "result_wire_sha256": result_wire_sha256,
                        "applied_state_id": applied_state_id,
                        "adapter_received_monotonic_ns": adapter_received_ns,
                        "adapter_applied_monotonic_ns": adapter_applied_ns,
                        "validity_start_monotonic_ns": sent_ns + 2,
                        "expires_monotonic_ns": sent_ns + 2_000_000_002,
                    },
                )
            )
    packets = {
        "records": [
            {
                "host_monotonic_ns": start_ns + 1,
                "event": "enqueue",
                "directed_link": link,
                "traffic_class": traffic_class,
                "radio_state_status": "fresh",
                "radio_state_age_ns": 0,
            }
            for link, traffic_class in FIXED_QUERY_CELLS
        ]
    }
    return (
        {"messages": messages, "message_by_hash": message_by_hash},
        {"records": states},
        packets,
        {"records": audits},
    )


class EffectsTests(unittest.TestCase):
    def test_mapping_is_bounded_monotonic_and_deterministic(self) -> None:
        policy = PacketEffectsPolicy.load()
        poor = policy.map_physical(physical(sinr=-5.0), "control")
        good = policy.map_physical(physical(sinr=20.0), "control")
        self.assertGreater(poor.loss_probability, good.loss_probability)
        self.assertEqual(poor.propagation_delay_ns, 167)
        self.assertEqual(poor.service_rate_bps, 1_000)
        self.assertEqual(good.service_rate_bps, 20_000_000)
        self.assertEqual(
            PacketEffectsPolicy.load().map_physical(physical(sinr=-9.0), "control").service_rate_bps,
            0,
        )
        causal_hash = "1" * 64
        sample = deterministic_loss_sample(causal_hash, "applied-state-1", 42)
        self.assertEqual(
            sample, deterministic_loss_sample(causal_hash, "applied-state-1", 42)
        )
        self.assertTrue(0.0 <= sample < 1.0)
        self.assertEqual(
            packet_delivery_decision(
                packet_causal_sha256=causal_hash,
                applied_state_id="applied-state-1",
                mapping_seed=42,
                loss_probability=1.0,
            )[1],
            False,
        )
        self.assertTrue(
            packet_delivery_decision(
                packet_causal_sha256=causal_hash,
                applied_state_id="applied-state-1",
                mapping_seed=42,
                loss_probability=1.0,
                intervention="force_deliver",
            )[1]
        )

    def test_policy_rejects_unknown_keys_nonmonotonic_curve_and_delay_overflow(
        self,
    ) -> None:
        source = json.loads(
            (ROOT / "network/config/sionna_packet_effects_v1.json").read_text()
        )
        mutations = []
        unknown = copy.deepcopy(source)
        unknown["unknown"] = True
        mutations.append(unknown)
        nonmonotonic = copy.deepcopy(source)
        nonmonotonic["traffic_classes"]["control"][2][0] = -3.0
        mutations.append(nonmonotonic)
        bad_rate = copy.deepcopy(source)
        bad_rate["service_rate_mapping"]["tiers_descending"][2][1] = 499_999
        mutations.append(bad_rate)
        bad_boundary = copy.deepcopy(source)
        bad_boundary["service_rate_mapping"]["boundary_policy"] = "interpolate"
        mutations.append(bad_boundary)
        bad_capacity = copy.deepcopy(source)
        bad_capacity["engineering_basis"]["channel_capacity_bps"] = 20_000_001
        mutations.append(bad_capacity)
        bad_limitation = copy.deepcopy(source)
        bad_limitation["engineering_basis"]["limitations"][0] += " altered"
        mutations.append(bad_limitation)
        with tempfile.TemporaryDirectory() as temporary:
            for index, mutation in enumerate(mutations):
                path = Path(temporary) / f"mutation-{index}.json"
                path.write_text(json.dumps(mutation), encoding="utf-8")
                with self.assertRaises(PacketAdapterError):
                    PacketEffectsPolicy.load(path)
        too_slow = physical()
        too_slow["propagation_delay_ns"] = source["max_effect_delay_ns"] + 1
        with self.assertRaisesRegex(PacketAdapterError, "outside configured bound"):
            PacketEffectsPolicy.load().map_physical(too_slow, "control")


class TailerAndIpcTests(unittest.TestCase):
    def test_tailer_is_nonblocking_preserves_partial_line_and_rejects_truncation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            tailer = PacketEventTailer(path)
            before = time.perf_counter()
            self.assertEqual(tailer.poll(), ())
            self.assertLess(time.perf_counter() - before, 0.02)
            raw = json.dumps(packet_event(), separators=(",", ":")).encode()
            path.write_bytes(raw[:20])
            self.assertEqual(tailer.poll(), ())
            with path.open("ab") as stream:
                stream.write(raw[20:] + b"\n")
            self.assertEqual(tailer.poll()[0]["event_sequence"], 1)
            path.write_bytes(b"")
            with self.assertRaisesRegex(PacketAdapterError, "truncated"):
                tailer.poll()

    def test_state_ipc_is_append_only_self_hashed_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            writer = AppliedStateIPCWriter(Path(temporary) / "state.jsonl")
            first = writer.write({"availability": "unavailable", "reason": "test"})
            second = writer.write({"availability": "unavailable", "reason": "test-2"})
            self.assertEqual(
                (first["state_sequence"], second["state_sequence"]), (1, 2)
            )
            for record in (first, second):
                digest = record["state_sha256"]
                without = dict(record)
                del without["state_sha256"]
                canonical = json.dumps(
                    without, sort_keys=True, separators=(",", ":")
                ).encode()
                self.assertEqual(digest, hashlib.sha256(canonical).hexdigest())
            lines = writer.path.read_text().splitlines()
            self.assertEqual(len(lines), 2)
            with self.assertRaisesRegex(PacketAdapterError, "exceeds bounded"):
                AppliedStateIPCWriter(
                    Path(temporary) / "tiny.jsonl", max_line_bytes=32
                ).write({"availability": "x" * 100})

    def test_fault_injector_holds_and_releases_exact_real_result_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            transport = FakeTransport()
            injector = SupervisedResultFaultInjector(
                transport, Path(temporary) / "fault.jsonl"
            )
            source_query = query()
            raw = encode_message(provider_result(source_query, 10, time.monotonic_ns()))
            transport.results.append(raw)
            injector.arm_hold_next(source_query["directed_link_id"])
            self.assertEqual(injector.poll_results(), ())
            self.assertEqual(injector.held_query_ids, (source_query["query_id"],))
            self.assertEqual(
                injector.held_query_ids_for_link(source_query["directed_link_id"]),
                (source_query["query_id"],),
            )
            self.assertEqual(injector.captured_query_ids, (source_query["query_id"],))
            self.assertEqual(
                injector.latest_captured_query_id(source_query["directed_link_id"]),
                source_query["query_id"],
            )
            self.assertIsNone(
                injector.latest_captured_query_id("uav1-to-cp-control")
            )
            injector.release_held(source_query["query_id"])
            self.assertEqual(injector.poll_results(), (raw,))
            injector.inject_duplicate(source_query["query_id"])
            self.assertEqual(injector.poll_results(), (raw,))
            records = [
                json.loads(line)
                for line in (Path(temporary) / "fault.jsonl").read_text().splitlines()
            ]
            self.assertEqual(
                [record["event"] for record in records],
                [
                    "hold_armed",
                    "real_result_held",
                    "held_result_released",
                    "byte_identical_duplicate_released",
                ],
            )
            self.assertEqual(
                {record["result_wire_sha256"] for record in records[1:]},
                {hashlib.sha256(raw).hexdigest()},
            )
            self.assertTrue(
                all(record["schema"] == "ams.sionna.result_fault_event/v2" for record in records)
            )
            self.assertTrue(all("bounded_state" in record for record in records))

    def test_fault_capture_is_a_bounded_ring_and_never_evicts_held_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            transport = FakeTransport()
            audit = Path(temporary) / "fault-bounded.jsonl"
            injector = SupervisedResultFaultInjector(
                transport,
                audit,
                max_held_results=1,
                max_release_queue=2,
                max_captured_results=3,
            )
            first_query = query("bounded-query-1", 1)
            first_raw = encode_message(
                provider_result(first_query, 10, time.monotonic_ns())
            )
            injector.arm_hold_next(first_query["directed_link_id"])
            transport.results.append(first_raw)
            self.assertEqual(injector.poll_results(), ())
            for index in range(2, 6):
                source_query = query(f"bounded-query-{index}", index)
                transport.results.append(
                    encode_message(
                        provider_result(
                            source_query, 9 + index, time.monotonic_ns()
                        )
                    )
                )
                self.assertEqual(len(injector.poll_results()), 1)
            stats = injector.statistics
            self.assertEqual(stats["captured_count"], 3)
            self.assertEqual(stats["captured_high_watermark"], 3)
            self.assertEqual(stats["captured_evictions"], 2)
            self.assertEqual(stats["captured_overflows"], 0)
            self.assertEqual(injector.held_query_ids, ("bounded-query-1",))
            injector.inject_duplicate("bounded-query-1")
            self.assertEqual(injector.poll_results(), (first_raw,))
            records = [json.loads(line) for line in audit.read_text().splitlines()]
            evictions = [
                record for record in records if record["event"] == "captured_result_evicted"
            ]
            self.assertEqual(
                [record["query_id"] for record in evictions],
                ["bounded-query-2", "bounded-query-3"],
            )
            self.assertTrue(
                all(
                    record["bounded_state"]["captured_count"] <= 3
                    and record["bounded_state"]["held_count"] <= 1
                    and record["bounded_state"]["release_queue_size"] <= 2
                    for record in records
                )
            )


class PacketSionnaAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1_000_000_000
        self.transport = FakeTransport()
        self.writer = AppliedStateIPCWriter(Path(self.temp.name) / "states.jsonl")
        self.adapter = PacketSionnaAdapter(
            adapter_config(),
            poses(self.now),
            self.transport,
            self.writer,
            Path(self.temp.name) / "audit.jsonl",
            clock_ns=lambda: self.now,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def complete(self, query_message: dict, wire_sequence: int) -> None:
        response = provider_result(query_message, wire_sequence, self.now)
        self.now += 20
        self.transport.results.append(encode_message(response))

    def test_actual_ingress_query_is_nonblocking_coalesced_and_applied_with_lineage(
        self,
    ) -> None:
        event = packet_event()
        before = time.perf_counter()
        created = self.adapter.process_packet_event(event)
        self.assertLess(time.perf_counter() - before, 0.02)
        self.assertEqual(created["tx_node_id"], "cp")
        self.assertEqual(created["rx_node_id"], "uav1")
        self.assertEqual(created["directed_link_id"], "cp-to-uav1-control")
        self.assertIsNone(self.adapter.process_packet_event(packet_event(2)))
        self.assertEqual(len(self.transport.submitted), 1)
        self.complete(self.transport.submitted[0], 10)
        records = self.adapter.poll_results()
        self.assertEqual(len(records), 1)
        state = records[0]
        self.assertEqual(state["availability"], "fresh")
        self.assertEqual(
            state["source_packet_causal_sha256"], event["transport_payload_sha256"]
        )
        self.assertEqual(state["query_id"], created["query_id"])
        self.assertRegex(state["result_wire_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(state["effects"]["propagation_delay_ns"], 167)
        self.assertEqual(state["effects"]["service_rate_bps"], 2_000_000)
        self.assertIn(state["effects"]["reference_delivery"], {"deliver", "drop"})
        self.assertEqual(len(self.writer.path.read_text().splitlines()), 1)

    def test_transport_not_ready_and_queue_overflow_publish_unavailable(self) -> None:
        self.transport.ready = False
        unavailable = self.adapter.process_packet_event(packet_event())
        self.assertEqual(unavailable["availability"], "unavailable")
        self.assertEqual(unavailable["unavailable_reason"], "transport_not_ready")
        self.transport.ready = True
        self.transport.accept_submissions = False
        overflow = self.adapter.process_packet_event(packet_event(2))
        self.assertEqual(overflow["unavailable_reason"], "query_queue_overflow")

    def test_provider_failure_and_client_fault_are_fail_closed(self) -> None:
        self.adapter.process_packet_event(packet_event())
        source_query = self.transport.submitted[0]
        failed = provider_result(source_query, 10, self.now)
        for key in (
            "validity_clock_domain",
            "validity_start_monotonic_ns",
            "expires_monotonic_ns",
            "physical",
        ):
            del failed[key]
        failed["status"] = "provider_error"
        failed["error_body"] = {
            "code": "provider_error",
            "detail": "injected provider failure",
            "retryable": True,
        }
        self.now += 20
        self.transport.results.append(encode_message(failed))
        record = self.adapter.poll_results()[0]
        self.assertEqual(record["availability"], "unavailable")
        self.assertIn("provider_failure", record["unavailable_reason"])
        self.adapter.process_packet_event(packet_event(2))
        self.transport.results.append(ClientFault("disconnect"))
        fault_records = self.adapter.poll_results()
        self.assertEqual(fault_records[0]["unavailable_reason"], "disconnect")

    def test_all_five_uav_thirty_cells_produce_fresh_applied_states(self) -> None:
        events = []
        sequence = 0
        for uav in range(1, 6):
            for link in (f"cp>uav{uav}", f"uav{uav}>cp"):
                for traffic_class in ("control", "payload", "additional_data"):
                    sequence += 1
                    event = packet_event(
                        sequence, link=link, traffic_class=traffic_class
                    )
                    events.append(event)
                    self.assertIsNotNone(self.adapter.process_packet_event(event))
        self.assertEqual(len(self.transport.submitted), 30)
        for wire_sequence, submitted in enumerate(self.transport.submitted, start=100):
            self.transport.results.append(
                encode_message(provider_result(submitted, wire_sequence, self.now))
            )
        self.now += 20
        states = self.adapter.poll_results(max_items=64)
        self.assertEqual(len(states), 30)
        self.assertEqual({state["availability"] for state in states}, {"fresh"})
        cells = {(state["directed_link"], state["traffic_class"]) for state in states}
        self.assertEqual(len(cells), 30)

    def test_fault_parallel_newer_result_supersedes_held_old_without_hold_last(self) -> None:
        self.adapter = PacketSionnaAdapter(
            dataclasses.replace(adapter_config(), fault_injection_enabled=True),
            poses(self.now),
            self.transport,
            self.writer,
            Path(self.temp.name) / "audit-fault.jsonl",
            clock_ns=lambda: self.now,
        )
        first = self.adapter.process_packet_event(packet_event(1))
        current = poses(self.now)
        self.adapter.update_poses(
            PoseSnapshot.create(
                snapshot_sequence=current.snapshot_sequence + 1,
                snapshot_monotonic_ns=self.now,
                source_frame=current.source_frame,
                transform_version=current.transform_version,
                nodes=current.nodes,
                jammers=current.jammers,
            )
        )
        second = self.adapter.process_fault_exercise_packet_event(packet_event(2))
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert first is not None and second is not None
        newer = provider_result(second, 11, self.now)
        older = provider_result(first, 10, self.now)
        self.now += 20
        newer_raw = encode_message(newer)
        self.transport.results.extend((newer_raw, encode_message(older)))
        records = self.adapter.poll_results()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["query_id"], second["query_id"])
        self.transport.results.append(newer_raw)
        self.assertEqual(self.adapter.poll_results(), ())
        self.assertEqual(len(self.writer.path.read_text().splitlines()), 1)
        audits = [
            json.loads(line)
            for line in (Path(self.temp.name) / "audit-fault.jsonl")
            .read_text()
            .splitlines()
        ]
        discarded = [record for record in audits if record["event"] == "result_discarded"]
        self.assertEqual(
            [record["decision"] for record in discarded],
            ["superseded", "duplicate"],
        )
        for record in discarded:
            self.assertRegex(record["result_wire_sha256"], r"^[0-9a-f]{64}$")

    def test_run_once_consumes_fault_seed_arm_only_on_matching_real_ingress(
        self,
    ) -> None:
        self.adapter = PacketSionnaAdapter(
            dataclasses.replace(adapter_config(), fault_injection_enabled=True),
            poses(self.now),
            self.transport,
            self.writer,
            Path(self.temp.name) / "audit-fault-arm.jsonl",
            clock_ns=lambda: self.now,
        )
        event_path = Path(self.temp.name) / "events-fault-arm.jsonl"
        tailer = PacketEventTailer(event_path)
        arm = {("cp>uav1", "control")}

        event_path.write_text(
            json.dumps(packet_event(1, event="egress"), separators=(",", ":"))
            + "\n",
            encoding="utf-8",
        )
        self.adapter.run_once(tailer, fault_seed_cells=arm)
        self.assertEqual(arm, {("cp>uav1", "control")})
        self.assertEqual(self.transport.submitted, [])

        with event_path.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(packet_event(2), separators=(",", ":")) + "\n"
            )
        self.adapter.run_once(tailer, fault_seed_cells=arm)
        self.assertEqual(arm, set())
        self.assertEqual(len(self.transport.submitted), 1)
        self.assertEqual(self.transport.armed_hold_links, ["cp-to-uav1-control"])
        self.assertEqual(
            [item["directed_link_id"] for item in self.transport.submitted],
            ["cp-to-uav1-control"],
        )
        audits = [
            json.loads(line)
            for line in (Path(self.temp.name) / "audit-fault-arm.jsonl")
            .read_text()
            .splitlines()
        ]
        self.assertEqual(
            [
                item["decision"]
                for item in audits
                if item["event"] == "query_submitted"
            ],
            ["fault_seed"],
        )

    def test_fault_parallel_arm_forces_exact_pair_when_normal_query_is_deferred(
        self,
    ) -> None:
        self.adapter = PacketSionnaAdapter(
            dataclasses.replace(adapter_config(), fault_injection_enabled=True),
            poses(self.now),
            self.transport,
            self.writer,
            Path(self.temp.name) / "audit-fault-deferred.jsonl",
            clock_ns=lambda: self.now,
        )
        cell = ("cp>uav1", "control")
        self.adapter._next_query_due_by_cell[cell] = self.now + 1_000_000_000
        event_path = Path(self.temp.name) / "events-fault-deferred.jsonl"
        event_path.write_text(
            json.dumps(packet_event(1), separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        seed_arm = {cell}

        tailer = PacketEventTailer(event_path)
        self.adapter.run_once(tailer, fault_seed_cells=seed_arm)
        self.assertEqual(seed_arm, set())
        self.assertEqual(len(self.transport.submitted), 1)
        self.assertEqual(len(self.adapter._pending_by_cell[cell]), 1)

        with event_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(packet_event(2), separators=(",", ":")) + "\n")
        parallel_arm = {cell}
        self.adapter.run_once(tailer, fault_parallel_cells=parallel_arm)

        self.assertEqual(parallel_arm, set())
        self.assertEqual(len(self.transport.submitted), 2)
        self.assertEqual(len(self.adapter._pending_by_cell[cell]), 2)
        audits = [
            json.loads(line)
            for line in (Path(self.temp.name) / "audit-fault-deferred.jsonl")
            .read_text()
            .splitlines()
        ]
        self.assertEqual(
            [
                item["decision"]
                for item in audits
                if item["event"] == "query_submitted"
            ],
            ["fault_seed", "fault_parallel"],
        )

    def test_periodic_refresh_uses_latest_atomic_pose_and_real_packet_lineage(self) -> None:
        source = packet_event(1)
        first = self.adapter.process_packet_event(source)
        assert first is not None
        self.complete(first, 10)
        self.assertEqual(self.adapter.poll_results()[0]["availability"], "fresh")

        replacement = poses(self.now + 999_999_900)
        replacement_nodes = [copy.deepcopy(item) for item in replacement.nodes]
        replacement_nodes[1]["position_m"] = [321.0, 4.0, 25.0]
        self.adapter.update_poses(
            PoseSnapshot.create(
                snapshot_sequence=replacement.snapshot_sequence + 1,
                snapshot_monotonic_ns=replacement.snapshot_monotonic_ns,
                source_frame=replacement.source_frame,
                transform_version=replacement.transform_version,
                nodes=tuple(replacement_nodes),
                jammers=replacement.jammers,
            )
        )
        self.now = 2_000_000_000
        refreshed = self.adapter.refresh_due_cells()
        self.assertEqual(len(refreshed), 1)
        self.assertEqual(refreshed[0]["node_state_seq"], 2)
        self.assertEqual(refreshed[0]["nodes"][1]["position_m"], [321.0, 4.0, 25.0])
        self.assertEqual(len(self.transport.submitted), 2)

    def test_absolute_expiry_publishes_one_fail_closed_transition(self) -> None:
        source = packet_event(1)
        created = self.adapter.process_packet_event(source)
        assert created is not None
        self.complete(created, 10)
        fresh = self.adapter.poll_results()[0]
        self.now = fresh["expires_monotonic_ns"]
        expired = self.adapter.expire_states()
        self.assertEqual(len(expired), 1)
        self.assertEqual(expired[0]["availability"], "unavailable")
        self.assertEqual(expired[0]["unavailable_reason"], "state_expired")
        self.assertEqual(self.adapter.expire_states(), ())
        audits = [
            json.loads(line)
            for line in (Path(self.temp.name) / "audit.jsonl").read_text().splitlines()
        ]
        self.assertEqual(
            sum(record["event"] == "state_expired" for record in audits), 1
        )

    def test_global_query_slots_stagger_thirty_cell_provider_load(self) -> None:
        self.adapter = PacketSionnaAdapter(
            dataclasses.replace(adapter_config(), global_query_spacing_ns=33_333_333),
            poses(self.now),
            self.transport,
            self.writer,
            Path(self.temp.name) / "audit-slots.jsonl",
            clock_ns=lambda: self.now,
        )
        first = packet_event(1, link="cp>uav1", traffic_class="control")
        second = packet_event(2, link="cp>uav2", traffic_class="control")
        self.assertIsNotNone(self.adapter.process_packet_event(first))
        self.assertIsNone(self.adapter.process_packet_event(second))
        self.assertEqual(len(self.transport.submitted), 1)
        self.now += 33_333_333
        refreshed = self.adapter.refresh_due_cells(max_cells=1)
        self.assertEqual(len(refreshed), 1)
        self.assertEqual(refreshed[0]["directed_link_id"], "cp-to-uav2-control")

        # Capacity mode owns an immutable 600-ordinal grid.  A five-ms late
        # first send must not move the second ordinal by five ms, and cells
        # without factual lineage still consume (miss) their declared slots.
        self.transport = FakeTransport()
        self.writer = AppliedStateIPCWriter(
            Path(self.temp.name) / "states-fixed-slots.jsonl"
        )
        schedule_start = self.now + 100_000_000
        fixed_audit = Path(self.temp.name) / "audit-fixed-slots.jsonl"
        self.adapter = PacketSionnaAdapter(
            dataclasses.replace(
                adapter_config(),
                query_deadline_ns=100_000_000,
                global_query_spacing_ns=33_333_333,
                fixed_query_schedule_start_ns=schedule_start,
                fixed_query_schedule_end_ns=(
                    schedule_start
                    + FIXED_QUERY_SLOT_COUNT_PER_CELL * 1_000_000_000
                ),
            ),
            poses(self.now),
            self.transport,
            self.writer,
            fixed_audit,
            clock_ns=lambda: self.now,
        )
        first_cell = FIXED_QUERY_CELLS[0]
        lineage = packet_event(
            3, link=first_cell[0], traffic_class=first_cell[1]
        )
        self.assertIsNone(self.adapter.process_packet_event(lineage))
        self.assertEqual(self.transport.submitted, [])

        self.now = schedule_start + 5_000_000
        first_slot = self.adapter.refresh_due_cells(max_cells=1)
        self.assertEqual(len(first_slot), 1)
        self.assertIn(".slot1.", first_slot[0]["query_id"])
        self.assertEqual(
            first_slot[0]["deadline_monotonic_ns"]
            - first_slot[0]["request_sent_monotonic_ns"],
            100_000_000,
        )
        self.complete(first_slot[0], 100)
        self.assertEqual(self.adapter.poll_results()[0]["availability"], "fresh")

        self.now = schedule_start + 1_000_000_000
        second_round = self.adapter.refresh_due_cells(max_cells=30)
        submitted = [
            item for item in second_round if item.get("message_type") == "query"
        ]
        self.assertEqual(len(submitted), 1)
        self.assertIn(".slot2.", submitted[0]["query_id"])
        self.assertEqual(
            submitted[0]["request_sent_monotonic_ns"],
            schedule_start + 1_000_000_000,
        )
        fixed_records = [
            json.loads(line) for line in fixed_audit.read_text().splitlines()
        ]
        slot_submissions = [
            item for item in fixed_records if item["event"] == "query_submitted"
        ]
        self.assertEqual(
            [item["query_slot_ordinal"] for item in slot_submissions], [1, 2]
        )
        self.assertEqual(
            slot_submissions[0]["query_slot_scheduled_monotonic_ns"],
            schedule_start,
        )
        self.assertEqual(
            sum(item["event"] == "query_slot_missed" for item in fixed_records),
            29,
        )

        # Provider completion alone is insufficient: receipt on the half-open
        # boundary must be discarded without producing a fresh state.
        second_query = submitted[0]
        on_time_provider_result = provider_result(second_query, 101, self.now)
        second_cutoff_ns = schedule_start + 1_100_000_000
        on_time_provider_result["expires_monotonic_ns"] = (
            second_cutoff_ns + 2_000_000_000
        )
        self.now = second_cutoff_ns
        self.transport.results.append(encode_message(on_time_provider_result))
        receipt_late = self.adapter.poll_results()
        self.assertEqual(len(receipt_late), 1)
        self.assertEqual(receipt_late[0]["availability"], "unavailable")
        self.assertEqual(
            receipt_late[0]["unavailable_reason"], "fixed_query_slot_late"
        )

        # A result received one ns before cutoff is still rejected if the
        # actual application clock reaches the boundary before apply_latest.
        self.now = schedule_start + 2_000_000_000
        self.adapter.update_poses(poses(self.now))
        third_round = self.adapter.refresh_due_cells(max_cells=30)
        third_submitted = [
            item for item in third_round if item.get("message_type") == "query"
        ]
        self.assertEqual(len(third_submitted), 1)
        self.assertIn(".slot3.", third_submitted[0]["query_id"])
        third_cutoff_ns = schedule_start + 2_100_000_000
        third_provider_result = provider_result(
            third_submitted[0], 102, self.now
        )
        third_provider_result["expires_monotonic_ns"] = (
            third_cutoff_ns + 2_000_000_000
        )
        self.transport.results.append(encode_message(third_provider_result))
        clock_ticks = iter(
            (third_cutoff_ns - 1, third_cutoff_ns - 1, third_cutoff_ns)
        )
        self.adapter._clock_ns = lambda: next(clock_ticks, third_cutoff_ns)
        apply_late = self.adapter.poll_results()
        self.assertEqual(len(apply_late), 1)
        self.assertEqual(apply_late[0]["availability"], "unavailable")
        self.assertEqual(
            apply_late[0]["unavailable_reason"], "fixed_query_slot_late"
        )
        self.adapter._clock_ns = lambda: self.now
        fixed_records = [
            json.loads(line) for line in fixed_audit.read_text().splitlines()
        ]
        discarded = [
            item
            for item in fixed_records
            if item["event"] == "result_discarded"
            and item["reason"] == "fixed_query_slot_late"
        ]
        self.assertEqual(len(discarded), 2)
        self.assertIsNone(discarded[0]["adapter_applied_monotonic_ns"])
        self.assertEqual(
            discarded[1]["adapter_applied_monotonic_ns"], third_cutoff_ns
        )

        # The independent Q4 calculation has a frozen denominator even when
        # no wire query exists; absence can no longer improve the late ratio.
        freshness, failures = validate_capacity_freshness(
            {"messages": []},
            {},
            {"records": []},
            {},
            start_ns=schedule_start,
            end_ns=schedule_start + 600_000_000_000,
        )
        self.assertEqual(freshness["scheduled_query_slot_count"], 18_000)
        self.assertEqual(freshness["missed_slot_count"], 18_000)
        self.assertEqual(freshness["failed_slot_count"], 18_000)
        self.assertEqual(freshness["late_update_ratio"], 1.0)
        self.assertTrue(
            any("successful ordinal slots" in failure for failure in failures)
        )

        (
            nominal_wire,
            nominal_states,
            nominal_packets,
            nominal_adapter,
        ) = capacity_slot_evidence(schedule_start)
        nominal, nominal_failures = validate_capacity_freshness(
            nominal_wire,
            nominal_states,
            nominal_packets,
            nominal_adapter,
            start_ns=schedule_start,
            end_ns=schedule_start + 600_000_000_000,
        )
        self.assertEqual(nominal_failures, [])
        self.assertEqual(nominal["query_count"], 18_000)
        self.assertEqual(nominal["ok_result_count"], 18_000)

        first_result = next(
            item
            for item in nominal_wire["messages"]
            if item.get("message_type") == "result"
        )
        first_query_id = first_result["query_id"]
        first_state = next(
            item
            for item in nominal_states["records"]
            if item.get("query_id") == first_query_id
        )
        receipt_audit = next(
            item
            for item in nominal_adapter["records"]
            if item.get("query_id") == first_query_id
            and item.get("event") == "result_received"
        )
        applied_audit = next(
            item
            for item in nominal_adapter["records"]
            if item.get("query_id") == first_query_id
            and item.get("event") == "result_applied"
        )
        original_receipt_ns = receipt_audit["adapter_received_monotonic_ns"]
        receipt_audit["adapter_received_monotonic_ns"] = (
            schedule_start + 100_000_000
        )
        applied_audit["adapter_received_monotonic_ns"] = (
            schedule_start + 100_000_000
        )
        late_receipt, late_receipt_failures = validate_capacity_freshness(
            nominal_wire,
            nominal_states,
            nominal_packets,
            nominal_adapter,
            start_ns=schedule_start,
            end_ns=schedule_start + 600_000_000_000,
        )
        self.assertEqual(late_receipt_failures, [])
        self.assertEqual(late_receipt["missed_slot_count"], 0)
        self.assertEqual(late_receipt["late_or_invalid_slot_count"], 1)
        self.assertEqual(late_receipt["failed_slot_count"], 1)
        receipt_audit["adapter_received_monotonic_ns"] = original_receipt_ns
        applied_audit["adapter_received_monotonic_ns"] = original_receipt_ns

        original_applied_ns = first_state["adapter_applied_monotonic_ns"]
        first_state["adapter_applied_monotonic_ns"] = (
            schedule_start + 100_000_000
        )
        applied_audit["adapter_applied_monotonic_ns"] = (
            schedule_start + 100_000_000
        )
        late_apply, late_apply_failures = validate_capacity_freshness(
            nominal_wire,
            nominal_states,
            nominal_packets,
            nominal_adapter,
            start_ns=schedule_start,
            end_ns=schedule_start + 600_000_000_000,
        )
        self.assertEqual(late_apply_failures, [])
        self.assertEqual(late_apply["late_or_invalid_slot_count"], 1)
        self.assertEqual(late_apply["failed_slot_count"], 1)
        first_state["adapter_applied_monotonic_ns"] = original_applied_ns
        applied_audit["adapter_applied_monotonic_ns"] = original_applied_ns

        original_node_state_seq = first_result["node_state_seq"]
        original_result_hash = first_state["result_wire_sha256"]
        first_result["node_state_seq"] = original_node_state_seq + 10_000
        first_state["node_state_seq"] = original_node_state_seq + 10_000
        mutated_result_hash = hashlib.sha256(
            (
                json.dumps(
                    first_result,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode()
        ).hexdigest()
        nominal_wire["message_by_hash"].pop(original_result_hash)
        nominal_wire["message_by_hash"][mutated_result_hash] = first_result
        first_state["result_wire_sha256"] = mutated_result_hash
        receipt_audit["result_wire_sha256"] = mutated_result_hash
        applied_audit["result_wire_sha256"] = mutated_result_hash
        mutated, mutation_failures = validate_capacity_freshness(
            nominal_wire,
            nominal_states,
            nominal_packets,
            nominal_adapter,
            start_ns=schedule_start,
            end_ns=schedule_start + 600_000_000_000,
        )
        self.assertEqual(mutated["failed_slot_count"], 1)
        self.assertTrue(
            any(
                "query/result correlation tuple differs" in item
                for item in mutation_failures
            )
        )
        self.assertTrue(
            any(
                "applied-state correlation tuple differs" in item
                for item in mutation_failures
            )
        )
        first_result["node_state_seq"] = original_node_state_seq
        first_state["node_state_seq"] = original_node_state_seq
        nominal_wire["message_by_hash"].pop(mutated_result_hash)
        nominal_wire["message_by_hash"][original_result_hash] = first_result
        first_state["result_wire_sha256"] = original_result_hash
        receipt_audit["result_wire_sha256"] = original_result_hash
        applied_audit["result_wire_sha256"] = original_result_hash

        target_link, target_class = FIXED_QUERY_CELLS[0]
        removed_query_ids = {
            item["query_id"]
            for item in nominal_wire["messages"]
            if item.get("message_type") == "query"
            and item.get("tx_node_id") + ">" + item.get("rx_node_id")
            == target_link
            and item.get("traffic_class") == target_class
            and int(item["query_id"].split(".slot", 1)[1].split(".", 1)[0])
            <= 31
        }
        missing_wire = {
            "messages": [
                item
                for item in nominal_wire["messages"]
                if item.get("query_id") not in removed_query_ids
            ],
            "message_by_hash": {
                digest: item
                for digest, item in nominal_wire["message_by_hash"].items()
                if item.get("query_id") not in removed_query_ids
            },
        }
        missing, missing_failures = validate_capacity_freshness(
            missing_wire,
            {
                "records": [
                    item
                    for item in nominal_states["records"]
                    if item.get("query_id") not in removed_query_ids
                ]
            },
            nominal_packets,
            {
                "records": [
                    item
                    for item in nominal_adapter["records"]
                    if item.get("query_id") not in removed_query_ids
                ]
            },
            start_ns=schedule_start,
            end_ns=schedule_start + 600_000_000_000,
        )
        self.assertEqual(len(removed_query_ids), 31)
        self.assertEqual(missing["missed_slot_count"], 31)
        self.assertEqual(missing["failed_slot_count"], 31)
        self.assertEqual(missing["minimum_successful_slots_in_one_cell"], 569)
        self.assertTrue(
            any("569 < 570 successful ordinal slots" in item for item in missing_failures)
        )


if __name__ == "__main__":
    unittest.main()
