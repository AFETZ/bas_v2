"""Focused unit tests for the neutral actual-SITL control executable."""

from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from network.scripts import actual_sitl_control_probe as probe
from network.validation import validate_m3_external_matrix as validator


class NonceAndCodecTests(unittest.TestCase):
    def test_m3_nonce_is_identity_and_wrong_length_fails(self) -> None:
        nonce = "12" * 16
        self.assertEqual(
            probe.transport_nonce32("m3", nonce),
            (nonce, "identity/full_run_nonce32"),
        )
        with self.assertRaises(probe.ControlProbeError):
            probe.transport_nonce32("m3", "12" * 32)

    def test_m4_nonce_uses_frozen_sha256_derivation_and_wrong_length_fails(self) -> None:
        nonce = "34" * 32
        derived, label = probe.transport_nonce32("m4_capacity", nonce)
        self.assertEqual(len(derived), 32)
        self.assertEqual(label, "sha256(raw_full_run_nonce64)[:32]")
        self.assertEqual(
            derived,
            probe.hashlib.sha256(bytes.fromhex(nonce)).hexdigest()[:32],
        )
        with self.assertRaises(probe.ControlProbeError):
            probe.transport_nonce32("m4_causality", "34" * 16)

    def test_request_encoder_is_only_marker_plus_request_message(self) -> None:
        nonce = "56" * 16
        request = probe.encode_actual_control_request(
            run_nonce=nonce,
            transport_nonce=nonce,
            phase_code=1,
            uav=3,
            sequence=7,
            mavlink=probe.MavlinkSequencer(),
        )
        marker = validator.parse_actual_mavlink_frame(request["marker_frame"])
        command = validator.parse_actual_mavlink_frame(request["command_frame"])
        self.assertEqual((marker["message_id"], command["message_id"]), (253, 76))
        self.assertEqual((command["system_id"], command["component_id"]), (255, 190))
        self.assertEqual(request["full_run_nonce"], nonce)
        self.assertEqual(request["transport_nonce32"], nonce)


class SchedulingAndStateTests(unittest.TestCase):
    @staticmethod
    def recovery_policy(window_id: str, start_ns: int) -> probe.WindowPolicy:
        return probe.WindowPolicy(
            window_id=window_id,
            transport_phase_code=5,
            start_monotonic_ns=start_ns,
            end_monotonic_ns=start_ns + 10_000_000_000,
            offered_per_uav=1,
            send_span_ms=0,
            expected_engine_state="up",
            response_policies={uav: "ack_required" for uav in range(1, 6)},
            minimum_quiet_drain_ns_by_uav={
                uav: 10_000_000_000 for uav in range(1, 6)
            },
            flow_group_ids={uav: f"flow-{uav}" for uav in range(1, 6)},
        )

    def test_declared_span_drives_exact_capacity_slots(self) -> None:
        policy = probe.WindowPolicy(
            window_id="capacity_measurement",
            transport_phase_code=4,
            start_monotonic_ns=10_000_000_000,
            end_monotonic_ns=650_000_000_000,
            offered_per_uav=421,
            send_span_ms=629_000,
            expected_engine_state="up_epoch_1",
            response_policies={uav: "ack_required" for uav in range(1, 6)},
            minimum_quiet_drain_ns_by_uav={
                uav: 0 for uav in range(1, 6)
            },
            flow_group_ids={uav: f"flow-{uav}" for uav in range(1, 6)},
        )
        slots = [policy.slot_monotonic_ns(index) for index in range(1, 422)]
        self.assertEqual(slots[0], policy.start_monotonic_ns)
        self.assertEqual(slots[-1], policy.start_monotonic_ns + 629_000_000_000)
        self.assertEqual(slots, sorted(set(slots)))

    def test_mixed_per_uav_policy_accepts_background_ack_and_rejects_target_ack(
        self,
    ) -> None:
        instance = probe.ActualSitlControlProbe.__new__(
            probe.ActualSitlControlProbe
        )
        instance.writer = mock.Mock()
        instance.pending = {}
        instance.guarded_uavs = set()
        instance.active_phase = "causal_down"
        instance.heartbeat_totals = {uav: 0 for uav in range(1, 6)}
        instance.expected_peers = {
            (f"10.71.{uav}.10", 14600 + uav): uav for uav in range(1, 6)
        }

        def pending(uav: int, policy: str) -> probe.PendingRequest:
            return probe.PendingRequest(
                phase="causal_down",
                uav=uav,
                sequence=1,
                sent_monotonic_ns=1,
                scheduled_send_monotonic_ns=1,
                send_lateness_ns=0,
                marker_frame_sha256="1" * 64,
                command_frame_sha256="2" * 64,
                record_nonce="3" * 64,
                full_run_nonce="4" * 64,
                transport_nonce32="5" * 32,
                transport_nonce_derivation="sha256(raw_full_run_nonce64)[:32]",
                window_id="causal_down",
                transport_phase_code=6,
                flow_group_id=f"group-{uav}",
                ordinal_send_slot=1,
                transaction_id=f"txn-{uav}",
                response_policy=policy,
            )

        instance.pending[1] = pending(1, "timeout_required")
        instance.pending[4] = pending(4, "ack_required")

        def ack_message(uav: int) -> mock.Mock:
            message = mock.Mock()
            message.get_msgbuf.return_value = probe.mavlink_v2_frame(
                77,
                probe.struct.pack("<HB", 512, 0),
                sequence=1,
                system_id=uav,
                component_id=1,
            )
            message.get_type.return_value = "COMMAND_ACK"
            message.get_msgId.return_value = 77
            message.get_srcSystem.return_value = uav
            message.get_srcComponent.return_value = 1
            message.command = 512
            message.result = 0
            return message

        instance._handle_message(
            ack_message(4),
            peer=("10.71.4.10", 14604),
            received_ns=10,
            datagram_sha256="6" * 64,
        )
        self.assertIsNotNone(instance.pending[4].ack)
        with self.assertRaises(probe.ControlProbeError):
            instance._handle_message(
                ack_message(1),
                peer=("10.71.1.10", 14601),
                received_ns=11,
                datagram_sha256="7" * 64,
            )
        self.assertEqual(
            instance.writer.emit.call_args.args[0],
            "forbidden_stopped_control_response",
        )

        instance.pending.pop(1)
        instance.guarded_uavs = {1}
        with self.assertRaises(probe.ControlProbeError):
            instance._handle_message(
                ack_message(1),
                peer=("10.71.1.10", 14601),
                received_ns=12,
                datagram_sha256="8" * 64,
            )
        self.assertEqual(
            instance.writer.emit.call_args.args[0], "late_stopped_control_response"
        )

    def test_m4_window_command_freezes_per_uav_policy_and_drain_maps(self) -> None:
        instance = probe.ActualSitlControlProbe.__new__(
            probe.ActualSitlControlProbe
        )
        instance.args = argparse.Namespace(profile="m4_causality")
        command = {
            "action": "window",
            "endpoint": "actual-control",
            "run_id": "run",
            "runtime_id": "11" * 16,
            "run_nonce": "22" * 32,
            "profile": "m4_causality",
            "window_id": "target_down_uav1",
            "transport_phase_code": 6,
            "start_monotonic_ns": 10_000_000_000,
            "end_monotonic_ns": 400_000_000_000,
            "offered_per_uav": 100,
            "send_span_ms": 303_000,
            "expected_engine_state": "causal_target_down",
            "response_policies": {
                **{f"uav{uav}": "ack_required" for uav in range(2, 6)},
                "uav1": "timeout_required",
            },
            "minimum_quiet_drain_ns_by_uav": {
                f"uav{uav}": 0 for uav in range(1, 6)
            },
            "flow_group_ids": {
                f"uav{uav}": f"causal-group-{uav}" for uav in range(1, 6)
            },
        }
        policy = instance.normalize_window(command)
        self.assertEqual(policy.response_policy_for(1), "timeout_required")
        self.assertEqual(policy.response_policy_for(4), "ack_required")
        self.assertEqual(policy.response_policy_label, "mixed_per_uav")

    def test_first_send_populates_every_pending_identity_field(self) -> None:
        instance = probe.ActualSitlControlProbe.__new__(probe.ActualSitlControlProbe)
        instance.args = argparse.Namespace(
            run_nonce="78" * 16,
            transport_nonce32="78" * 16,
            transport_nonce_derivation="identity/full_run_nonce32",
        )
        instance.pending = {}
        instance.quarantined_uavs = set()
        instance.sequencer = probe.MavlinkSequencer()
        instance.sock = mock.Mock()
        instance.sock.sendto.side_effect = lambda payload, _destination: len(payload)
        instance.writer = mock.Mock()
        policy = probe.WindowPolicy(
            window_id="positive",
            transport_phase_code=1,
            start_monotonic_ns=1,
            end_monotonic_ns=10_000_000_000,
            offered_per_uav=1,
            send_span_ms=0,
            expected_engine_state="up_epoch_1",
            response_policies={uav: "ack_required" for uav in range(1, 6)},
            minimum_quiet_drain_ns_by_uav={
                uav: 0 for uav in range(1, 6)
            },
            flow_group_ids={uav: f"uav{uav}.control.downlink" for uav in range(1, 6)},
        )
        with mock.patch.object(probe.time, "monotonic_ns", return_value=2):
            instance.send_request(policy, 1, 1)
        pending = instance.pending[1]
        self.assertEqual(pending.window_id, "positive")
        self.assertEqual(pending.transport_phase_code, 1)
        self.assertEqual(pending.ordinal_send_slot, 1)
        self.assertEqual(pending.response_policy, "ack_required")
        self.assertIn("positive:uav1.control.downlink:1:1", pending.transaction_id)
        self.assertEqual(instance.sock.sendto.call_count, 2)

    def test_two_timeout_recovery_pairs_keep_history_but_reset_active_batch(
        self,
    ) -> None:
        instance = probe.ActualSitlControlProbe.__new__(
            probe.ActualSitlControlProbe
        )
        instance.pump = mock.Mock()
        instance.writer = mock.Mock()
        instance.expired_stopped_attempts = {
            uav: [{"batch": 1, "uav": uav}] for uav in range(1, 6)
        }
        instance.active_expired_stopped_attempts = {
            uav: [{"batch": 1, "uav": uav}] for uav in range(1, 6)
        }
        instance.guarded_uavs = set(range(1, 6))
        instance.last_stopped_timeout_monotonic_ns_by_uav = {
            uav: 10_000_000_000 for uav in range(1, 6)
        }
        instance.expected_expired_per_uav = {uav: 1 for uav in range(1, 6)}
        instance.consume_stopped_drain_guard(
            self.recovery_policy("recovery_one", 20_000_000_000)
        )
        self.assertTrue(
            all(not values for values in instance.active_expired_stopped_attempts.values())
        )
        self.assertTrue(
            all(len(values) == 1 for values in instance.expired_stopped_attempts.values())
        )

        for uav in range(1, 6):
            second = {"batch": 2, "uav": uav}
            instance.expired_stopped_attempts[uav].append(second)
            instance.active_expired_stopped_attempts[uav].append(second)
        instance.guarded_uavs = set(range(1, 6))
        instance.last_stopped_timeout_monotonic_ns_by_uav = {
            uav: 30_000_000_000 for uav in range(1, 6)
        }
        instance.expected_expired_per_uav = {uav: 1 for uav in range(1, 6)}
        instance.consume_stopped_drain_guard(
            self.recovery_policy("recovery_two", 40_000_000_000)
        )
        calls = instance.writer.emit.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            calls[0].kwargs["expired_attempt_history_counts"],
            {f"uav{uav}": 1 for uav in range(1, 6)},
        )
        self.assertEqual(
            calls[1].kwargs["expired_attempt_history_counts"],
            {f"uav{uav}": 2 for uav in range(1, 6)},
        )

    def test_event_writer_has_complete_final_hash_chain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            args = argparse.Namespace(
                run_id="run",
                runtime_id="11" * 16,
                run_nonce="22" * 16,
                profile="m3",
                transport_nonce32="22" * 16,
                transport_nonce_derivation="identity/full_run_nonce32",
            )
            writer = probe.EventWriter(path, args)
            writer.emit("one", value=1)
            writer.emit("two", value=2)
            writer.close()
            payload = path.read_bytes()
            self.assertTrue(payload.endswith(b"\n"))
            lines = payload.splitlines(keepends=True)
            records = [json.loads(line) for line in lines]
            self.assertIsNone(records[0]["previous_record_sha256"])
            self.assertEqual(
                records[1]["previous_record_sha256"], probe.sha256_bytes(lines[0])
            )


if __name__ == "__main__":
    unittest.main()
