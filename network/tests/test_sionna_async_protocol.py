#!/usr/bin/env python3
"""Adversarial tests for the dependency-free Sionna async v1 boundary."""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from network.radio_provider.sionna_async import (  # noqa: E402
    DEFAULT_PROTOCOL_CONFIG_PATH,
    DEFAULT_SCHEMA_PATH,
    DirectedLinkStateManager,
    ProtocolIdentity,
    ProtocolLimits,
    ProtocolStateError,
    ProtocolValidationError,
    WireSequenceTracker,
    decode_message,
    encode_message,
    load_protocol_limits,
    message_sha256,
    node_state_sha256,
    validate_message,
)


HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64


def common(
    message_type: str,
    wire_sequence: int,
    *,
    sender_id: str,
    emitted: int,
    generation: int = 0,
) -> dict:
    return {
        "schema_version": 1,
        "message_type": message_type,
        "wire_sequence": wire_sequence,
        "sender_id": sender_id,
        "run_id": "run-m4-test",
        "profile": "sionna-async-v1",
        "phase_id": "phase-main",
        "contract_hash": HASH_A,
        "config_hash": HASH_B,
        "bundle_id": "bundle-rock-v2",
        "reconnect_generation": generation,
        "sender_clock_domain": "host-monotonic",
        "emitted_monotonic_ns": emitted,
    }


def hello(
    *,
    wire_sequence: int = 1,
    sender_id: str = "adapter-a",
    role: str = "adapter",
    generation: int = 0,
    emitted: int = 10,
) -> dict:
    message = common(
        "hello",
        wire_sequence,
        sender_id=sender_id,
        emitted=emitted,
        generation=generation,
    )
    message.update(
        {
            "protocol_name": "sionna_async",
            "protocol_version": 1,
            "sender_role": role,
            "executable_identity": {"path": "/opt/ams/adapter", "sha256": HASH_C},
            "capabilities": {
                "supported_message_types": [
                    "hello",
                    "ready",
                    "query",
                    "result",
                    "error",
                    "disconnect",
                ],
                "max_message_bytes": 1_048_576,
            },
            "accepted_run_id": message["run_id"],
            "accepted_config_hash": message["config_hash"],
            "accepted_bundle_id": message["bundle_id"],
            "readiness_state": "initializing",
        }
    )
    if role == "provider":
        message["provider_identity"] = {
            "provider_id": "sionna-gpu-0",
            "provider_mode": "real_sionna",
            "acceptance_eligible": True,
            "sionna_rt_version": "1.2.0",
            "mitsuba_version": "3.6.4",
        }
    return message


def ready(
    *,
    wire_sequence: int = 2,
    sender_id: str = "adapter-a",
    role: str = "adapter",
    generation: int = 0,
    emitted: int = 20,
) -> dict:
    message = hello(
        wire_sequence=wire_sequence,
        sender_id=sender_id,
        role=role,
        generation=generation,
        emitted=emitted,
    )
    message["message_type"] = "ready"
    message["readiness_state"] = "ready"
    message["scene_identity"] = {
        "bundle_id": message["bundle_id"],
        "scene_manifest_sha256": HASH_D,
        "scene_path": "/opt/ams/scenes/rock-v2.xml",
    }
    return message


def entity(node_id: str, pose_time: int = 100, *, x: float = 0.0) -> dict:
    return {
        "node_id": node_id,
        "role": "uav",
        "pose_monotonic_ns": pose_time,
        "source_topic": f"/{node_id}/odometry",
        "source_frame": "enu",
        "transform_version": "enu-to-sionna-v1",
        "position_m": [x, 0.0, 10.0],
        "orientation_quat_xyzw": [0.0, 0.0, 0.0, 1.0],
        "freshness_age_ns": 120 - pose_time,
        "stale": False,
    }


def jammer(jammer_id: str = "jammer-1", pose_time: int = 100) -> dict:
    return {
        "jammer_id": jammer_id,
        "pose_monotonic_ns": pose_time,
        "source_topic": f"/{jammer_id}/pose",
        "source_frame": "enu",
        "transform_version": "enu-to-sionna-v1",
        "position_m": [20.0, 10.0, 2.0],
        "orientation_quat_xyzw": [0.0, 0.0, 0.0, 1.0],
        "freshness_age_ns": 120 - pose_time,
        "stale": False,
        "enabled": True,
        "center_frequency_hz": 5.9e9,
        "bandwidth_hz": 20e6,
        "power_dbm": 10.0,
        "duty_cycle": 0.5,
        "antenna_pattern": "isotropic",
    }


def query(
    query_id: str = "query-1",
    node_state_seq: int = 1,
    *,
    link_id: str = "uav1-to-uav2-control",
    tx: str = "uav1",
    rx: str = "uav2",
    wire_sequence: int = 3,
    sender_id: str = "adapter-a",
) -> dict:
    message = common("query", wire_sequence, sender_id=sender_id, emitted=120)
    message.update(
        {
            "query_id": query_id,
            "node_state_seq": node_state_seq,
            "node_state_snapshot_monotonic_ns": 105,
            "directed_link_id": link_id,
            "deadline_monotonic_ns": 1000,
            "traffic_class": "control",
            "tx_node_id": tx,
            "rx_node_id": rx,
            "source_pose_monotonic_ns": 100,
            "source_frame": "enu",
            "transform_version": "enu-to-sionna-v1",
            "request_generated_monotonic_ns": 110,
            "request_sent_monotonic_ns": 120,
            "nodes": [entity(tx, x=0.0), entity(rx, x=50.0)],
            "jammers": [jammer()],
            "radio_assumptions": {
                "carrier_frequency_hz": 5.9e9,
                "bandwidth_hz": 20e6,
                "tx_power_dbm": 23.0,
                "receiver_noise_figure_db": 7.0,
                "receiver_sensitivity_dbm": -96.0,
                "units": {
                    "carrier_frequency": "Hz",
                    "bandwidth": "Hz",
                    "tx_power": "dBm",
                    "receiver_noise_figure": "dB",
                    "receiver_sensitivity": "dBm",
                },
            },
            "antenna_assumptions": {
                "tx_pattern": "isotropic",
                "rx_pattern": "isotropic",
                "polarization": "vertical",
                "orientation_effects_claimed": True,
            },
            "material_assumptions": {
                "material_model_id": "itu-v1",
                "scene_material_manifest_sha256": HASH_D,
            },
            "mapping_version": "sinr-to-per-v1",
            "provider_seed": 42,
        }
    )
    message["node_state_sha256"] = node_state_sha256(
        node_state_seq=node_state_seq,
        snapshot_monotonic_ns=message["node_state_snapshot_monotonic_ns"],
        source_frame=message["source_frame"],
        transform_version=message["transform_version"],
        nodes=message["nodes"],
        jammers=message["jammers"],
    )
    return message


def physical(*, sinr: float = 18.5) -> dict:
    return {
        "pathloss_db": 89.0,
        "propagation_delay_ns": 166.8,
        "rssi_dbm": -66.0,
        "signal_power_dbm": -66.0,
        "interference_power_dbm": -93.0,
        "noise_power_dbm": -101.0,
        "sinr_db": sinr,
        "js_db": -27.0,
        "geometry_state": "los",
        "path_count": 1,
        "path_type_counts": {
            "los": 1,
            "specular": 0,
            "diffuse": 0,
            "refracted": 0,
            "diffracted": 0,
            "mixed": 0,
        },
        "units": {
            "pathloss": "dB",
            "propagation_delay": "ns",
            "rssi": "dBm",
            "signal_power": "dBm",
            "interference_power": "dBm",
            "noise_power": "dBm",
            "sinr": "dB",
            "j_over_s": "dB",
        },
    }


def result(
    source_query: dict | None = None,
    *,
    wire_sequence: int = 3,
    status: str = "ok",
    validity_start: int = 140,
    expiry: int = 200,
    sinr: float = 18.5,
) -> dict:
    source_query = source_query or query()
    message = common("result", wire_sequence, sender_id="provider-a", emitted=145)
    message.update(
        {
            "query_id": source_query["query_id"],
            "node_state_seq": source_query["node_state_seq"],
            "directed_link_id": source_query["directed_link_id"],
            "traffic_class": source_query["traffic_class"],
            "tx_node_id": source_query["tx_node_id"],
            "rx_node_id": source_query["rx_node_id"],
            "provider_clock_domain": "host-monotonic",
            "provider_received_monotonic_ns": 125,
            "provider_started_monotonic_ns": 126,
            "provider_completed_monotonic_ns": 140,
            "provider_sent_monotonic_ns": 145,
            "status": status,
        }
    )
    if status == "ok":
        message.update(
            {
                "validity_clock_domain": "host-monotonic",
                "validity_start_monotonic_ns": validity_start,
                "expires_monotonic_ns": expiry,
                "physical": physical(sinr=sinr),
            }
        )
    else:
        message["error_body"] = {
            "code": status,
            "detail": f"provider reported {status}",
            "retryable": status in {"provider_error", "deadline_missed"},
        }
    return message


def error(*, wire_sequence: int = 3, sender_id: str = "adapter-a") -> dict:
    message = common("error", wire_sequence, sender_id=sender_id, emitted=30)
    message.update(
        {
            "error_kind": "invalid_request",
            "reason": "request hash did not validate",
            "lifecycle_monotonic_ns": 29,
            "rejected_wire_sequence": 2,
            "rejected_request_sha256": HASH_D,
        }
    )
    return message


def disconnect(
    *,
    wire_sequence: int = 4,
    sender_id: str = "adapter-a",
    generation: int = 0,
    owned_links: list[str] | None = None,
) -> dict:
    message = common(
        "disconnect",
        wire_sequence,
        sender_id=sender_id,
        emitted=160,
        generation=generation,
    )
    message.update(
        {
            "disconnect_kind": "disconnected",
            "reason": "provider connection closed",
            "lifecycle_monotonic_ns": 159,
        }
    )
    if owned_links is not None:
        message["owned_directed_link_ids"] = owned_links
    return message


class CodecAndSchemaTests(unittest.TestCase):
    def test_checked_in_config_and_schema_are_versioned_and_bounded(self) -> None:
        limits = load_protocol_limits(DEFAULT_PROTOCOL_CONFIG_PATH)
        self.assertEqual(limits.max_message_bytes, 1_048_576)
        self.assertEqual(limits.max_pending_results_per_link, 8)
        self.assertEqual(limits.validity_ttl_ns, 2_000_000_000)
        self.assertEqual(limits.max_pose_age_ns, 1_500_000_000)
        schema = json.loads(DEFAULT_SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertEqual(
            schema["$schema"], "https://json-schema.org/draft/2020-12/schema"
        )
        self.assertEqual(len(schema["oneOf"]), 6)
        self.assertEqual(
            set(schema["$defs"]["result"]["oneOf"][0].values()),
            {"#/$defs/result_ok"},
        )
        self.assertNotIn(
            "applied_state_id", DEFAULT_SCHEMA_PATH.read_text(encoding="utf-8")
        )

    def test_all_six_message_types_round_trip_canonically(self) -> None:
        samples = [hello(), ready(), query(), result(), error(), disconnect()]
        for sample in samples:
            with self.subTest(message_type=sample["message_type"]):
                encoded = encode_message(sample)
                self.assertTrue(encoded.endswith(b"\n"))
                self.assertEqual(decode_message(encoded), sample)
                self.assertEqual(encode_message(decode_message(encoded)), encoded)
                self.assertEqual(len(message_sha256(encoded)), 64)

    def test_duplicate_json_keys_and_nonfinite_numbers_are_rejected(self) -> None:
        for raw in (
            b'{"schema_version":1,"schema_version":1}\n',
            b'{"message_type":"result","x":NaN}\n',
            b'{"message_type":"result","x":Infinity}\n',
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(ProtocolValidationError):
                    decode_message(raw)

    def test_invalid_framing_encoding_and_size_are_rejected(self) -> None:
        good = encode_message(hello())
        bad_frames = [good + good, b"\xef\xbb\xbf" + good, b"\xff\n", b"", b"{}\r\n"]
        for raw in bad_frames:
            with self.subTest(raw=raw[:20]):
                with self.assertRaises(ProtocolValidationError):
                    decode_message(raw)
        with self.assertRaises(ProtocolValidationError):
            decode_message(good, max_bytes=len(good) - 1)
        with self.assertRaises(ProtocolValidationError):
            encode_message(hello(), max_bytes=10)

    def test_unknown_missing_and_wrong_primitive_types_are_rejected(self) -> None:
        mutations = []
        unknown = hello()
        unknown["surprise"] = True
        mutations.append(unknown)
        missing = query()
        del missing["mapping_version"]
        mutations.append(missing)
        boolean_sequence = hello()
        boolean_sequence["wire_sequence"] = True
        mutations.append(boolean_sequence)
        upper_hash = hello()
        upper_hash["contract_hash"] = "A" * 64
        mutations.append(upper_hash)
        applied_on_provider_result = result()
        applied_on_provider_result["applied_state_id"] = "forbidden-provider-state"
        mutations.append(applied_on_provider_result)
        for mutation in mutations:
            with self.subTest(keys=sorted(mutation)):
                with self.assertRaises(ProtocolValidationError):
                    validate_message(mutation)

    def test_handshake_identities_and_acceptance_mode_are_strict(self) -> None:
        cases = []
        mismatched = hello()
        mismatched["accepted_run_id"] = "different-run"
        cases.append(mismatched)
        missing_provider = hello(role="provider")
        del missing_provider["provider_identity"]
        cases.append(missing_provider)
        diagnostic_claim = hello(role="provider")
        diagnostic_claim["provider_identity"]["provider_mode"] = "analytic_fallback"
        cases.append(diagnostic_claim)
        adapter_provider_identity = hello()
        adapter_provider_identity["provider_identity"] = copy.deepcopy(
            hello(role="provider")["provider_identity"]
        )
        cases.append(adapter_provider_identity)
        for case in cases:
            with self.subTest(role=case["sender_role"]):
                with self.assertRaises(ProtocolValidationError):
                    validate_message(case)

    def test_query_pose_snapshot_and_physical_units_are_semantic(self) -> None:
        cases = []
        duplicate_node = query()
        duplicate_node["nodes"][1]["node_id"] = "uav1"
        cases.append(duplicate_node)
        missing_endpoint = query()
        missing_endpoint["nodes"][1]["node_id"] = "uav3"
        cases.append(missing_endpoint)
        wrong_freshness = query()
        wrong_freshness["nodes"][0]["freshness_age_ns"] = 19
        cases.append(wrong_freshness)
        bad_quaternion = query()
        bad_quaternion["nodes"][0]["orientation_quat_xyzw"] = [0.0, 0.0, 0.0, 2.0]
        cases.append(bad_quaternion)
        late_pose = query()
        late_pose["nodes"][0]["pose_monotonic_ns"] = 121
        late_pose["nodes"][0]["freshness_age_ns"] = -1
        cases.append(late_pose)
        wrong_units = result()
        wrong_units["physical"]["units"]["sinr"] = "dBm"
        cases.append(wrong_units)
        nonfinite = result()
        nonfinite["physical"]["sinr_db"] = float("inf")
        cases.append(nonfinite)
        unclassified = result()
        unclassified["physical"]["geometry_state"] = "unclassified"
        cases.append(unclassified)
        count_mismatch = result()
        count_mismatch["physical"]["path_count"] = 2
        cases.append(count_mismatch)
        blocked_with_path = result()
        blocked_with_path["physical"]["geometry_state"] = "blocked_no_path"
        cases.append(blocked_with_path)
        nlos_with_los_path = result()
        nlos_with_los_path["physical"]["geometry_state"] = "nlos"
        cases.append(nlos_with_los_path)
        for case in cases:
            with self.subTest(message_type=case["message_type"]):
                with self.assertRaises(ProtocolValidationError):
                    validate_message(case)

    def test_result_status_union_timestamps_and_validity_are_strict(self) -> None:
        cases = []
        bad_order = result()
        bad_order["provider_started_monotonic_ns"] = 141
        cases.append(bad_order)
        bad_window = result()
        bad_window["expires_monotonic_ns"] = bad_window["validity_start_monotonic_ns"]
        cases.append(bad_window)
        early_validity = result()
        early_validity["validity_start_monotonic_ns"] = 139
        cases.append(early_validity)
        failure_with_physical = result(status="provider_error")
        failure_with_physical["physical"] = physical()
        cases.append(failure_with_physical)
        mismatch_code = result(status="stale_pose")
        mismatch_code["error_body"]["code"] = "provider_error"
        cases.append(mismatch_code)
        oversized_reason = result(status="scene_mismatch")
        oversized_reason["error_body"]["detail"] = "x" * 1025
        cases.append(oversized_reason)
        for case in cases:
            with self.subTest(status=case["status"]):
                with self.assertRaises(ProtocolValidationError):
                    validate_message(case)


class WireSequenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.identity = ProtocolIdentity.from_message(hello())
        self.tracker = WireSequenceTracker(self.identity)

    def test_complete_lifecycle_and_reconnect_preserve_sequence(self) -> None:
        messages = [
            hello(),
            ready(),
            query(),
            disconnect(),
            hello(wire_sequence=5, generation=1, emitted=170),
            ready(wire_sequence=6, generation=1, emitted=180),
        ]
        for message in messages:
            self.tracker.observe(message)

    def test_first_message_must_be_hello_and_state_is_not_mutated_on_failure(
        self,
    ) -> None:
        with self.assertRaisesRegex(ProtocolStateError, "first message"):
            self.tracker.observe(ready())
        self.tracker.observe(hello())
        self.tracker.observe(ready())

    def test_duplicate_and_out_of_order_sequences_are_rejected(self) -> None:
        self.tracker.observe(hello(wire_sequence=2))
        with self.assertRaisesRegex(ProtocolStateError, "duplicate"):
            self.tracker.observe(ready(wire_sequence=2))
        with self.assertRaisesRegex(ProtocolStateError, "out-of-order"):
            self.tracker.observe(ready(wire_sequence=1))
        self.tracker.observe(ready(wire_sequence=3))

    def test_reconnect_cannot_reset_skip_or_continue_without_hello(self) -> None:
        self.tracker.observe(hello())
        self.tracker.observe(ready())
        self.tracker.observe(disconnect())
        invalid = [
            hello(wire_sequence=1, generation=1, emitted=170),
            hello(wire_sequence=5, generation=2, emitted=170),
            ready(wire_sequence=5, generation=1, emitted=170),
        ]
        for message in invalid:
            with self.subTest(
                sequence=message["wire_sequence"],
                generation=message["reconnect_generation"],
            ):
                with self.assertRaises(ProtocolStateError):
                    self.tracker.observe(message)
        self.tracker.observe(hello(wire_sequence=5, generation=1, emitted=170))

    def test_query_before_ready_and_identity_drift_are_rejected(self) -> None:
        self.tracker.observe(hello())
        with self.assertRaisesRegex(ProtocolStateError, "requires a ready"):
            self.tracker.observe(query())
        drift = ready()
        drift["config_hash"] = HASH_C
        drift["accepted_config_hash"] = HASH_C
        with self.assertRaisesRegex(ProtocolStateError, "identity mismatch"):
            self.tracker.observe(drift)
        self.tracker.observe(ready())


class DirectedLinkStateManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.identity = ProtocolIdentity.from_message(query())
        self.manager = DirectedLinkStateManager(self.identity, "host-monotonic")

    def register(self, q: dict | None = None) -> dict:
        q = q or query()
        self.manager.register_query(q)
        return q

    def ingest(self, r: dict, now: int = 150):
        return self.manager.ingest_result_wire(encode_message(r), now)

    def apply(
        self,
        link: str = "uav1-to-uav2-control",
        now: int = 150,
        state_id: str = "applied-1",
    ):
        return self.manager.apply_latest(link, now, state_id)

    def test_happy_path_applies_adapter_owned_state_only_inside_validity(self) -> None:
        q = self.register()
        r = result(q)
        self.assertEqual(self.ingest(r).kind, "pending")
        decision = self.apply()
        self.assertEqual(decision.kind, "applied")
        self.assertEqual(decision.applied_state_id, "applied-1")
        state = self.manager.state_for_packet(q["directed_link_id"], 175)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state.query_id, q["query_id"])
        self.assertEqual(state.physical["sinr_db"], 18.5)
        self.assertIsNone(self.manager.state_for_packet(q["directed_link_id"], 200))
        self.assertEqual(
            self.manager.link_status(q["directed_link_id"]),
            (False, "applied_state_expired"),
        )

    def test_identical_duplicate_is_idempotent_and_preserves_fresh_active_state(
        self,
    ) -> None:
        q = self.register()
        r = result(q)
        self.ingest(r)
        self.apply()
        duplicate = self.ingest(r, now=160)
        self.assertEqual(duplicate.kind, "duplicate")
        self.assertIsNotNone(self.manager.state_for_packet(q["directed_link_id"], 170))

    def test_conflicting_second_result_for_query_fails_closed(self) -> None:
        q = self.register()
        first = result(q)
        self.ingest(first)
        self.apply()
        conflicting = result(q, sinr=-5.0)
        decision = self.ingest(conflicting, now=160)
        self.assertEqual(decision.kind, "invalid")
        self.assertIsNone(self.manager.state_for_packet(q["directed_link_id"], 170))
        self.assertEqual(
            self.manager.link_status(q["directed_link_id"])[1],
            "conflicting_duplicate_result",
        )

    def test_out_of_order_late_result_is_superseded_without_destroying_new_state(
        self,
    ) -> None:
        q2 = self.register(query("query-2", 2, wire_sequence=4))
        q3 = self.register(query("query-3", 3, wire_sequence=5))
        self.ingest(result(q3, wire_sequence=8))
        self.apply(state_id="applied-3")
        late = self.ingest(result(q2, wire_sequence=7), now=160)
        self.assertEqual(late.kind, "superseded")
        state = self.manager.state_for_packet(q3["directed_link_id"], 170)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state.node_state_seq, 3)

    def test_expiry_preserves_completed_watermark_for_old_release_and_duplicate(
        self,
    ) -> None:
        old_query = self.register(query("query-old", 2, wire_sequence=4))
        newer_query = self.register(query("query-newer", 3, wire_sequence=5))
        newer_result = result(newer_query, wire_sequence=9)
        self.assertEqual(self.ingest(newer_result).kind, "pending")
        self.assertEqual(
            self.apply(state_id="applied-newer").query_id,
            newer_query["query_id"],
        )
        self.assertEqual(len(self.manager.expire(200)), 1)
        self.assertIsNone(
            self.manager.state_for_packet(newer_query["directed_link_id"], 201)
        )

        late_old = self.ingest(result(old_query, wire_sequence=8), now=201)
        self.assertEqual(late_old.kind, "superseded")
        duplicate_newer = self.ingest(newer_result, now=202)
        self.assertEqual(duplicate_newer.kind, "duplicate")
        self.assertIsNone(
            self.manager.state_for_packet(newer_query["directed_link_id"], 202)
        )

    def test_selection_uses_newest_node_state_then_completion_then_wire_sequence(
        self,
    ) -> None:
        q2 = self.register(query("query-2", 2, wire_sequence=4))
        q3 = self.register(query("query-3", 3, wire_sequence=5))
        self.ingest(result(q2, wire_sequence=10))
        self.ingest(result(q3, wire_sequence=9))
        selected = self.apply(state_id="applied-newest")
        self.assertEqual(selected.query_id, "query-3")
        self.assertEqual(selected.node_state_seq, 3)

    def test_future_result_waits_and_expired_result_never_applies(self) -> None:
        q = self.register()
        self.ingest(result(q, validity_start=180, expiry=200))
        self.assertEqual(
            self.apply(now=170, state_id="future-state").kind, "unavailable"
        )
        self.assertEqual(self.apply(now=180, state_id="future-state").kind, "applied")

        other = query(
            "query-other",
            1,
            link_id="uav2-to-uav1-control",
            tx="uav2",
            rx="uav1",
            wire_sequence=4,
        )
        self.register(other)
        expired = self.ingest(result(other, wire_sequence=4, expiry=149), now=150)
        self.assertEqual(expired.kind, "expired")
        self.assertEqual(self.manager.link_status(other["directed_link_id"])[0], False)

    def test_provider_failure_targets_only_correlated_directed_link(self) -> None:
        qa = self.register()
        qb = self.register(
            query(
                "query-b",
                1,
                link_id="uav2-to-uav1-control",
                tx="uav2",
                rx="uav1",
                wire_sequence=4,
            )
        )
        self.ingest(result(qa))
        self.apply(qa["directed_link_id"], state_id="active-a")
        self.ingest(result(qb, wire_sequence=4))
        self.apply(qb["directed_link_id"], state_id="active-b")
        failure_query = self.register(query("query-a2", 2, wire_sequence=5))
        failure = self.ingest(
            result(failure_query, status="provider_error", wire_sequence=5), now=160
        )
        self.assertEqual(failure.kind, "provider_failure")
        self.assertIsNone(self.manager.state_for_packet(qa["directed_link_id"], 170))
        self.assertIsNotNone(self.manager.state_for_packet(qb["directed_link_id"], 170))

    def test_late_failure_is_superseded_by_newer_completed_result(self) -> None:
        q2 = self.register(query("query-2", 2, wire_sequence=4))
        q3 = self.register(query("query-3", 3, wire_sequence=5))
        self.assertEqual(self.ingest(result(q3, wire_sequence=9)).kind, "pending")
        late_failure = self.ingest(
            result(q2, wire_sequence=8, status="stale_pose"), now=160
        )
        self.assertEqual(late_failure.kind, "superseded")
        self.assertEqual(self.apply(state_id="newer-state").query_id, q3["query_id"])

    def test_invalid_unidentified_frame_marks_all_owned_links_unavailable(self) -> None:
        qa = self.register()
        qb = self.register(
            query(
                "query-b",
                1,
                link_id="uav2-to-uav1-control",
                tx="uav2",
                rx="uav1",
                wire_sequence=4,
            )
        )
        for q, state_id in ((qa, "active-a"), (qb, "active-b")):
            self.ingest(result(q, wire_sequence=q["wire_sequence"]))
            self.apply(q["directed_link_id"], state_id=state_id)
        decision = self.manager.ingest_result_wire(
            b'{"message_type":"result","message_type":"result"}\n', 160
        )
        self.assertEqual(decision.kind, "invalid")
        for link in (qa["directed_link_id"], qb["directed_link_id"]):
            self.assertEqual(
                self.manager.link_status(link), (False, "invalid_unidentified_result")
            )

    def test_fail_closed_event_clears_previously_pending_results(self) -> None:
        q = self.register()
        self.assertEqual(self.ingest(result(q)).kind, "pending")
        self.manager.ingest_result_wire(b"not-json\n", 160)
        self.assertEqual(
            self.apply(now=170, state_id="must-not-resurrect").kind,
            "unavailable",
        )
        self.assertEqual(
            self.manager.link_status(q["directed_link_id"]),
            (False, "invalid_unidentified_result"),
        )

    def test_provider_cannot_receive_query_before_adapter_sent_it(self) -> None:
        q = self.register()
        impossible = result(q)
        impossible["provider_received_monotonic_ns"] = 119
        impossible["provider_started_monotonic_ns"] = 126
        decision = self.ingest(impossible)
        self.assertEqual(decision.kind, "invalid")
        self.assertEqual(
            self.manager.link_status(q["directed_link_id"])[1],
            "provider_received_before_query_sent",
        )

    def test_correlation_tuple_mismatch_fails_expected_and_declared_links(self) -> None:
        qa = self.register()
        qb = self.register(
            query(
                "query-b",
                1,
                link_id="uav2-to-uav1-control",
                tx="uav2",
                rx="uav1",
                wire_sequence=4,
            )
        )
        malformed = result(qa)
        malformed["directed_link_id"] = qb["directed_link_id"]
        malformed["tx_node_id"] = "uav2"
        malformed["rx_node_id"] = "uav1"
        decision = self.ingest(malformed)
        self.assertEqual(decision.kind, "invalid")
        self.assertEqual(
            self.manager.link_status(qa["directed_link_id"])[1],
            "result_correlation_mismatch",
        )
        self.assertEqual(
            self.manager.link_status(qb["directed_link_id"])[1],
            "result_correlation_mismatch",
        )

    def test_bounded_pending_queue_overflow_is_fail_closed_and_clears_queue(
        self,
    ) -> None:
        manager = DirectedLinkStateManager(
            self.identity,
            "host-monotonic",
            ProtocolLimits(max_pending_results_per_link=2),
        )
        queries = [
            query(f"query-{index}", index, wire_sequence=index + 2)
            for index in (1, 2, 3)
        ]
        for q in queries:
            manager.register_query(q)
        self.assertEqual(
            manager.ingest_result_wire(
                encode_message(result(queries[0], wire_sequence=11)), 150
            ).kind,
            "pending",
        )
        self.assertEqual(
            manager.ingest_result_wire(
                encode_message(result(queries[1], wire_sequence=12)), 150
            ).kind,
            "pending",
        )
        overflow = manager.ingest_result_wire(
            encode_message(result(queries[2], wire_sequence=13)), 150
        )
        self.assertEqual(overflow.kind, "queue_overflow")
        self.assertEqual(
            manager.link_status(queries[0]["directed_link_id"]),
            (False, "pending_result_queue_overflow"),
        )
        self.assertEqual(
            manager.apply_latest(
                queries[0]["directed_link_id"], 160, "never-applied"
            ).kind,
            "unavailable",
        )

    def test_disconnect_targets_declared_links_or_all_when_ownership_is_absent(
        self,
    ) -> None:
        qa = self.register()
        qb = self.register(
            query(
                "query-b",
                1,
                link_id="uav2-to-uav1-control",
                tx="uav2",
                rx="uav1",
                wire_sequence=4,
            )
        )
        for q, state_id in ((qa, "active-a"), (qb, "active-b")):
            self.ingest(result(q, wire_sequence=q["wire_sequence"]))
            self.apply(q["directed_link_id"], state_id=state_id)
        targeted = disconnect(
            sender_id="provider-a", owned_links=[qa["directed_link_id"]]
        )
        decisions = self.manager.handle_disconnect_wire(encode_message(targeted), 170)
        self.assertEqual(
            [item.directed_link_id for item in decisions], [qa["directed_link_id"]]
        )
        self.assertIsNotNone(self.manager.state_for_packet(qb["directed_link_id"], 175))
        all_decisions = self.manager.handle_disconnect_wire(
            encode_message(disconnect(sender_id="provider-a", wire_sequence=5)), 170
        )
        self.assertEqual(
            {item.directed_link_id for item in all_decisions},
            {qa["directed_link_id"], qb["directed_link_id"]},
        )
        self.assertIsNone(self.manager.state_for_packet(qb["directed_link_id"], 175))

    def test_disconnect_with_unknown_ownership_is_invalid_and_fails_all(self) -> None:
        q = self.register()
        self.ingest(result(q))
        self.apply()
        bad = disconnect(sender_id="provider-a", owned_links=["unknown-link"])
        decisions = self.manager.handle_disconnect_wire(encode_message(bad), 170)
        self.assertEqual(decisions[0].kind, "invalid")
        self.assertEqual(
            self.manager.link_status(q["directed_link_id"]),
            (False, "disconnect_ownership_mismatch"),
        )

    def test_expire_reports_transition_and_hold_last_is_forbidden(self) -> None:
        q = self.register()
        self.ingest(result(q))
        self.apply()
        decisions = self.manager.expire(200)
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].kind, "expired")
        self.assertIsNone(self.manager.state_for_packet(q["directed_link_id"], 201))

    def test_duplicate_applied_state_id_and_history_bounds_fail_closed(self) -> None:
        qa = self.register()
        self.ingest(result(qa))
        self.apply(state_id="shared-state")
        qb = self.register(query("query-2", 2, wire_sequence=4))
        self.ingest(result(qb, wire_sequence=4))
        with self.assertRaisesRegex(ProtocolStateError, "globally unique"):
            self.apply(state_id="shared-state")
        self.assertEqual(self.manager.link_status(qa["directed_link_id"])[0], False)

        bounded = DirectedLinkStateManager(
            self.identity,
            "host-monotonic",
            ProtocolLimits(max_query_history=1),
        )
        bounded.register_query(query("bounded-1", 1))
        with self.assertRaisesRegex(ProtocolStateError, "history bound"):
            bounded.register_query(query("bounded-2", 2, wire_sequence=4))
        self.assertEqual(bounded.link_status("uav1-to-uav2-control")[0], False)


if __name__ == "__main__":
    unittest.main()
