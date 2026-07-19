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
        self.assertEqual(
            request["marker_frame"].hex(),
            "fd33000000ffbefd000006414d533335363536353635363536353635363536353635"
            "3635363536353635363536353631313133303030373538343832328b78",
        )
        self.assertEqual(
            request["command_frame"].hex(),
            "fd21000001ffbe4c0000000014430000000000000000000000000000000000000000"
            "00000000000203010066c6",
        )
        self.assertEqual(
            request["marker_frame_sha256"],
            "be97bc6ffc4618c3a799ff9170a4e1846b427aeed9d23496fa2e2c06585f4ea2",
        )
        self.assertEqual(
            request["command_frame_sha256"],
            "d1e5ddf6d66b5056e1ef85793bc122c3b46db606b1af8a0139faa576ca583bff",
        )
        self.assertNotIn("timesync_frame", request)
        self.assertEqual(request["full_run_nonce"], nonce)
        self.assertEqual(request["transport_nonce32"], nonce)

    def test_m4_correlated_encoder_is_one_exact_three_frame_datagram(self) -> None:
        nonce = "ab" * 32
        sequencer = probe.MavlinkSequencer()
        request = probe.encode_m4_correlated_control_request(
            run_nonce=nonce,
            transport_nonce=probe.transport_nonce32("m4_causality", nonce)[0],
            phase_code=7,
            uav=4,
            sequence=99,
            mavlink=sequencer,
        )
        self.assertEqual(
            request["request_datagram"],
            request["marker_frame"]
            + request["command_frame"]
            + request["timesync_frame"],
        )
        marker = validator.parse_actual_mavlink_frame(request["marker_frame"])
        command = validator.parse_actual_mavlink_frame(request["command_frame"])
        timesync_frame = request["timesync_frame"]
        timesync_message_id = int.from_bytes(timesync_frame[7:10], "little")
        timesync_payload = timesync_frame[10 : 10 + timesync_frame[1]]
        self.assertEqual(
            [marker["message_id"], command["message_id"], timesync_message_id],
            [253, 76, 111],
        )
        self.assertEqual(
            probe.struct.unpack("<qq", timesync_payload),
            (0, request["timesync_request_ts1"]),
        )
        self.assertEqual(request["timesync_request_tc1"], 0)
        self.assertEqual(
            request["request_datagram_sha256"],
            probe.sha256_bytes(request["request_datagram"]),
        )
        self.assertEqual(sequencer.value, 3)

    def test_timesync_tokens_are_bounded_deterministic_and_injective(self) -> None:
        nonce = "cd" * 32
        values = {
            probe.timesync_token(
                run_nonce=nonce,
                phase_code=phase,
                uav=uav,
                ordinal=ordinal,
            )
            for phase in range(1, 12)
            for uav in range(1, 6)
            for ordinal in range(1, 101)
        }
        self.assertEqual(len(values), 11 * 5 * 100)
        self.assertTrue(all(0 < value < 1 << 63 for value in values))
        self.assertEqual(
            probe.timesync_token(
                run_nonce=nonce, phase_code=3, uav=2, ordinal=17
            ),
            probe.timesync_token(
                run_nonce=nonce, phase_code=3, uav=2, ordinal=17
            ),
        )
        for kwargs in (
            {"run_nonce": "00" * 16, "phase_code": 1, "uav": 1, "ordinal": 1},
            {"run_nonce": nonce, "phase_code": 0, "uav": 1, "ordinal": 1},
            {"run_nonce": nonce, "phase_code": 1, "uav": 6, "ordinal": 1},
            {"run_nonce": nonce, "phase_code": 1, "uav": 1, "ordinal": 0},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(
                probe.ControlProbeError
            ):
                probe.timesync_token(**kwargs)


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

    @staticmethod
    def correlated_policy(
        *,
        window_id: str = "positive_window",
        start_ns: int = 1_000_000_000,
        mixed_timeout_uav: int | None = None,
    ) -> probe.WindowPolicy:
        if mixed_timeout_uav is None:
            duration_ns = 30_000_000_000
            span_ms = 26_800
        else:
            duration_ns = 310_000_000_000
            span_ms = 306_900
        policies = {
            uav: probe.CORRELATED_TIMESYNC_POLICY for uav in range(1, 6)
        }
        if mixed_timeout_uav is not None:
            policies[mixed_timeout_uav] = "timeout_required"
        return probe.WindowPolicy(
            window_id=window_id,
            transport_phase_code=7,
            start_monotonic_ns=start_ns,
            end_monotonic_ns=start_ns + duration_ns,
            offered_per_uav=100,
            send_span_ms=span_ms,
            expected_engine_state="up_epoch_1",
            response_policies=policies,
            minimum_quiet_drain_ns_by_uav={
                uav: 0 for uav in range(1, 6)
            },
            flow_group_ids={uav: f"flow-{uav}" for uav in range(1, 6)},
        )

    @staticmethod
    def correlated_instance() -> probe.ActualSitlControlProbe:
        instance = probe.ActualSitlControlProbe.__new__(
            probe.ActualSitlControlProbe
        )
        nonce = "9a" * 32
        transport, derivation = probe.transport_nonce32("m4_causality", nonce)
        instance.args = argparse.Namespace(
            profile="m4_causality",
            run_nonce=nonce,
            transport_nonce32=transport,
            transport_nonce_derivation=derivation,
        )
        instance.pending = {}
        instance.correlated_pending = {}
        instance.retired_timesync_tokens = {}
        instance.quarantined_uavs = set()
        instance.guarded_uavs = set()
        instance.sequencer = probe.MavlinkSequencer()
        instance.sock = mock.Mock()
        instance.sock.sendto.side_effect = lambda payload, _destination: len(payload)
        instance.writer = mock.Mock()
        instance.active_phase = None
        instance.active_policy = None
        instance.raw_command_ack_totals = {uav: 0 for uav in range(1, 6)}
        instance.raw_autopilot_version_totals = {
            uav: 0 for uav in range(1, 6)
        }
        instance.raw_command_ack_received_monotonic_ns = {
            uav: [] for uav in range(1, 6)
        }
        instance.raw_autopilot_version_received_monotonic_ns = {
            uav: [] for uav in range(1, 6)
        }
        instance.expected_peers = {
            (f"10.71.{uav}.10", 14600 + uav): uav
            for uav in range(1, 6)
        }
        return instance

    @staticmethod
    def timesync_message(
        uav: int,
        token: int,
        *,
        vehicle_clock_ns: int = 123,
        ambient_request: bool = False,
    ) -> mock.Mock:
        tc1 = 0 if ambient_request else vehicle_clock_ns
        ts1 = token
        message = mock.Mock()
        message.get_msgbuf.return_value = probe.mavlink_v2_frame(
            111,
            probe.struct.pack("<qq", tc1, ts1),
            sequence=1,
            system_id=uav,
            component_id=1,
        )
        message.get_type.return_value = "TIMESYNC"
        message.get_msgId.return_value = 111
        message.get_srcSystem.return_value = uav
        message.get_srcComponent.return_value = 1
        message.tc1 = tc1
        message.ts1 = ts1
        return message

    @staticmethod
    def response_message(uav: int, message_type: str) -> mock.Mock:
        message = mock.Mock()
        message_id = 77 if message_type == "COMMAND_ACK" else 148
        payload = probe.struct.pack("<HB", 512, 0) if message_id == 77 else b""
        message.get_msgbuf.return_value = probe.mavlink_v2_frame(
            message_id,
            payload,
            sequence=1,
            system_id=uav,
            component_id=1,
        )
        message.get_type.return_value = message_type
        message.get_msgId.return_value = message_id
        message.get_srcSystem.return_value = uav
        message.get_srcComponent.return_value = 1
        if message_id == 77:
            message.command = 512
            message.result = 0
        return message

    def test_declared_span_drives_exact_capacity_slots(self) -> None:
        policy = probe.WindowPolicy(
            window_id="capacity_measurement",
            transport_phase_code=4,
            start_monotonic_ns=10_000_000_000,
            end_monotonic_ns=620_000_000_000,
            offered_per_uav=400,
            send_span_ms=598_500,
            expected_engine_state="up_epoch_1",
            response_policies={uav: "ack_required" for uav in range(1, 6)},
            minimum_quiet_drain_ns_by_uav={
                uav: 0 for uav in range(1, 6)
            },
            flow_group_ids={uav: f"flow-{uav}" for uav in range(1, 6)},
        )
        slots = [policy.slot_monotonic_ns(index) for index in range(1, 401)]
        self.assertEqual(slots[0], policy.start_monotonic_ns)
        self.assertEqual(slots[-1], policy.start_monotonic_ns + 598_500_000_000)
        self.assertEqual(slots, sorted(set(slots)))

    def test_m4_combined_send_is_single_and_multi_pending_is_bounded(self) -> None:
        instance = self.correlated_instance()
        policy = self.correlated_policy()
        instance.active_policy = policy
        instance.active_phase = policy.window_id
        self.assertEqual(policy.correlated_pending_bound(), 12)
        with mock.patch.object(probe.time, "monotonic_ns", return_value=1_000_000_000):
            for sequence in range(1, 13):
                instance.send_request(policy, 1, sequence)
        self.assertEqual(instance.sock.sendto.call_count, 12)
        self.assertEqual(len(instance.correlated_pending), 12)
        first_payload = instance.sock.sendto.call_args_list[0].args[0]
        first_offer = instance.writer.emit.call_args_list[0]
        self.assertEqual(first_offer.args[0], "real_command_offered")
        self.assertEqual(
            probe.sha256_bytes(first_payload),
            first_offer.kwargs["request_transport_payload_sha256"],
        )
        self.assertEqual(
            first_payload.hex(), first_offer.kwargs["request_transport_payload_hex"]
        )
        with self.assertRaises(probe.ControlProbeError):
            instance.send_request(policy, 1, 13)

    def test_correlated_token_reuse_fails_before_encoding_or_send(self) -> None:
        instance = self.correlated_instance()
        policy = self.correlated_policy()
        instance.active_policy = policy
        instance.active_phase = policy.window_id
        with mock.patch.object(probe.time, "monotonic_ns", return_value=1_000_000_000):
            instance.send_request(policy, 1, 1)
        token = next(iter(instance.correlated_pending))[1]
        instance._handle_message(
            self.timesync_message(1, token, vehicle_clock_ns=987_654_321),
            peer=("10.71.1.10", 14601),
            received_ns=1_100_000_000,
            datagram_sha256="0" * 64,
        )
        send_calls = instance.sock.sendto.call_count
        event_calls = instance.writer.emit.call_count
        sequencer_value = instance.sequencer.value
        with self.assertRaises(probe.ControlProbeError):
            instance.send_request(policy, 1, 1)
        self.assertEqual(instance.sock.sendto.call_count, send_calls)
        self.assertEqual(instance.writer.emit.call_count, event_calls)
        self.assertEqual(instance.sequencer.value, sequencer_value)

    def test_timesync_reply_correlates_ts1_and_records_tc1_as_vehicle_clock(self) -> None:
        instance = self.correlated_instance()
        policy = self.correlated_policy()
        instance.active_policy = policy
        instance.active_phase = policy.window_id
        with mock.patch.object(probe.time, "monotonic_ns", return_value=1_000_000_000):
            instance.send_request(policy, 1, 1)
        token = next(iter(instance.correlated_pending))[1]
        vehicle_clock_ns = 8_765_432_100
        instance._handle_message(
            self.timesync_message(
                1, token, vehicle_clock_ns=vehicle_clock_ns
            ),
            peer=("10.71.1.10", 14601),
            received_ns=1_100_000_000,
            datagram_sha256="9" * 64,
        )
        result = instance.writer.emit.call_args
        self.assertEqual(result.args[0], "transaction_result")
        self.assertEqual(
            result.kwargs["timesync_response"]["timesync_tc1"],
            vehicle_clock_ns,
        )
        self.assertEqual(
            result.kwargs["timesync_response"]["timesync_ts1"],
            token,
        )
        self.assertEqual(
            instance.retired_timesync_tokens[(1, token)]["outcome"], "success"
        )

    def test_mixed_policy_keeps_timeout_serial_and_other_uavs_correlated(self) -> None:
        instance = self.correlated_instance()
        policy = self.correlated_policy(mixed_timeout_uav=1)
        instance.active_policy = policy
        instance.active_phase = policy.window_id
        with mock.patch.object(probe.time, "monotonic_ns", return_value=1_000_000_000):
            instance.send_request(policy, 1, 1)
            instance.send_request(policy, 2, 1)
        self.assertIn(1, instance.pending)
        self.assertEqual(instance.pending[1].response_policy, "timeout_required")
        self.assertEqual(
            sum(key[0] == 2 for key in instance.correlated_pending), 1
        )
        self.assertEqual(instance.sock.sendto.call_count, 2)
        with self.assertRaises(probe.ControlProbeError):
            instance.send_request(policy, 2, 2)
        with self.assertRaises(probe.ControlProbeError):
            instance.send_request(policy, 1, 2)

    def test_out_of_order_echoes_bind_by_token_and_duplicates_fail(self) -> None:
        instance = self.correlated_instance()
        policy = self.correlated_policy()
        instance.active_policy = policy
        instance.active_phase = policy.window_id
        with mock.patch.object(
            probe.time, "monotonic_ns", side_effect=[1_000_000_000, 1_100_000_000]
        ):
            instance.send_request(policy, 1, 1)
            instance.send_request(policy, 1, 2)
        tokens = {
            pending.sequence: token
            for (uav, token), pending in instance.correlated_pending.items()
            if uav == 1
        }
        instance._handle_message(
            self.timesync_message(1, tokens[2]),
            peer=("10.71.1.10", 14601),
            received_ns=1_200_000_000,
            datagram_sha256="a" * 64,
        )
        self.assertIn((1, tokens[1]), instance.correlated_pending)
        self.assertNotIn((1, tokens[2]), instance.correlated_pending)
        instance._handle_message(
            self.timesync_message(1, tokens[1]),
            peer=("10.71.1.10", 14601),
            received_ns=1_300_000_000,
            datagram_sha256="b" * 64,
        )
        self.assertFalse(instance.correlated_pending)
        self.assertEqual(
            {value["outcome"] for value in instance.retired_timesync_tokens.values()},
            {"success"},
        )
        with self.assertRaises(probe.ControlProbeError):
            instance._handle_message(
                self.timesync_message(1, tokens[1]),
                peer=("10.71.1.10", 14601),
                received_ns=1_400_000_000,
                datagram_sha256="c" * 64,
            )

    def test_timeout_tombstone_allows_next_slot_and_absorbs_one_late_echo(self) -> None:
        instance = self.correlated_instance()
        policy = self.correlated_policy()
        instance.active_policy = policy
        instance.active_phase = policy.window_id
        with mock.patch.object(probe.time, "monotonic_ns", return_value=1_000_000_000):
            instance.send_request(policy, 1, 1)
        old_token = next(iter(instance.correlated_pending))[1]
        with mock.patch.object(probe.time, "monotonic_ns", return_value=4_000_000_000):
            instance._expire_pending()
        self.assertFalse(instance.correlated_pending)
        self.assertEqual(
            instance.retired_timesync_tokens[(1, old_token)]["outcome"], "timeout"
        )
        self.assertNotIn(1, instance.quarantined_uavs)
        with mock.patch.object(probe.time, "monotonic_ns", return_value=4_100_000_000):
            instance.send_request(policy, 1, 2)
        new_key = next(iter(instance.correlated_pending))
        instance._handle_message(
            self.timesync_message(1, old_token),
            peer=("10.71.1.10", 14601),
            received_ns=4_200_000_000,
            datagram_sha256="d" * 64,
        )
        self.assertIn(new_key, instance.correlated_pending)
        self.assertTrue(
            instance.retired_timesync_tokens[(1, old_token)]["late_seen"]
        )
        with self.assertRaises(probe.ControlProbeError):
            instance._handle_message(
                self.timesync_message(1, old_token),
                peer=("10.71.1.10", 14601),
                received_ns=4_300_000_000,
                datagram_sha256="e" * 64,
            )

    def test_deadline_is_half_open_and_ambient_request_is_ignored(self) -> None:
        instance = self.correlated_instance()
        policy = self.correlated_policy()
        instance.active_policy = policy
        instance.active_phase = policy.window_id
        with mock.patch.object(probe.time, "monotonic_ns", return_value=1_000_000_000):
            instance.send_request(policy, 1, 1)
        token = next(iter(instance.correlated_pending))[1]
        instance._handle_message(
            self.timesync_message(1, token),
            peer=("10.71.1.10", 14601),
            received_ns=3_999_999_999,
            datagram_sha256="f" * 64,
        )
        self.assertEqual(
            instance.retired_timesync_tokens[(1, token)]["outcome"], "success"
        )
        ambient = self.timesync_message(1, token + 1, ambient_request=True)
        instance._handle_message(
            ambient,
            peer=("10.71.1.10", 14601),
            received_ns=4_000_000_000,
            datagram_sha256="1" * 64,
        )
        self.assertEqual(
            instance.writer.emit.call_args.args[0], "ambient_timesync_request"
        )

        second = self.correlated_instance()
        second.active_policy = policy
        second.active_phase = policy.window_id
        with mock.patch.object(probe.time, "monotonic_ns", return_value=1_000_000_000):
            second.send_request(policy, 1, 1)
        second_token = next(iter(second.correlated_pending))[1]
        second._handle_message(
            self.timesync_message(1, second_token),
            peer=("10.71.1.10", 14601),
            received_ns=4_000_000_000,
            datagram_sha256="2" * 64,
        )
        self.assertEqual(
            second.retired_timesync_tokens[(1, second_token)]["outcome"],
            "timeout",
        )

    def test_raw_responses_are_liveness_only_and_half_open_counted(self) -> None:
        instance = self.correlated_instance()
        policy = self.correlated_policy(start_ns=10_000)
        instance.active_policy = policy
        instance.active_phase = policy.window_id
        with mock.patch.object(probe.time, "monotonic_ns", return_value=10_000):
            instance.send_request(policy, 1, 1)
        key = next(iter(instance.correlated_pending))
        for message_type, received_ns in (
            ("COMMAND_ACK", 10_000),
            ("AUTOPILOT_VERSION", policy.end_monotonic_ns - 1),
        ):
            instance._handle_message(
                self.response_message(1, message_type),
                peer=("10.71.1.10", 14601),
                received_ns=received_ns,
                datagram_sha256="3" * 64,
            )
        self.assertIn(key, instance.correlated_pending)
        self.assertEqual(
            probe.raw_response_counts_for_window(
                instance.raw_command_ack_received_monotonic_ns,
                policy.start_monotonic_ns,
                policy.end_monotonic_ns,
            )["uav1"],
            1,
        )
        instance.raw_command_ack_received_monotonic_ns[1].append(
            policy.end_monotonic_ns
        )
        self.assertEqual(
            probe.raw_response_counts_for_window(
                instance.raw_command_ack_received_monotonic_ns,
                policy.start_monotonic_ns,
                policy.end_monotonic_ns,
            )["uav1"],
            1,
        )

    def test_m4_liveness_requires_only_correlated_uav_heartbeat_and_raw(self) -> None:
        policy = self.correlated_policy(mixed_timeout_uav=1)
        heartbeats = {f"uav{uav}": 3 for uav in range(1, 6)}
        heartbeats["uav1"] = 0
        ack = {f"uav{uav}": 1 for uav in range(1, 6)}
        telemetry = dict(ack)
        ack["uav1"] = 0
        telemetry["uav1"] = 0
        probe.validate_m4_window_liveness(policy, heartbeats, ack, telemetry)
        heartbeats["uav2"] = 2
        with self.assertRaises(probe.ControlProbeError):
            probe.validate_m4_window_liveness(policy, heartbeats, ack, telemetry)
        heartbeats["uav2"] = 3
        telemetry["uav4"] = 0
        with self.assertRaises(probe.ControlProbeError):
            probe.validate_m4_window_liveness(policy, heartbeats, ack, telemetry)

    def test_startup_responses_are_ambient_only_before_first_control_window(self) -> None:
        instance = self.correlated_instance()
        instance.args.profile = "m3"
        instance.processed_commands = set()
        for message_type in ("COMMAND_ACK", "AUTOPILOT_VERSION"):
            instance._handle_message(
                self.response_message(1, message_type),
                peer=("10.71.1.10", 14601),
                received_ns=1_000_000_000,
                datagram_sha256="7" * 64,
            )
        instance.writer.emit.assert_not_called()

        instance.processed_commands.add("first-window-command")
        with self.assertRaises(probe.ControlProbeError):
            instance._handle_message(
                self.response_message(1, "COMMAND_ACK"),
                peer=("10.71.1.10", 14601),
                received_ns=1_000_000_001,
                datagram_sha256="8" * 64,
            )
        self.assertEqual(
            instance.writer.emit.call_args.args[0],
            "uncorrelated_control_response",
        )

    def test_five_uav_hundred_slots_allow_exact_five_percent_echo_loss(self) -> None:
        instance = self.correlated_instance()
        policy = self.correlated_policy()
        instance.active_policy = policy
        instance.active_phase = policy.window_id
        latest_sent_ns = 0
        for uav in range(1, 6):
            for sequence in range(1, 101):
                sent_ns = policy.slot_monotonic_ns(sequence)
                latest_sent_ns = max(latest_sent_ns, sent_ns)
                with mock.patch.object(
                    probe.time, "monotonic_ns", return_value=sent_ns
                ):
                    instance.send_request(policy, uav, sequence)
                if sequence <= 95:
                    key = next(
                        key
                        for key, pending in instance.correlated_pending.items()
                        if pending.uav == uav and pending.sequence == sequence
                    )
                    instance._handle_message(
                        self.timesync_message(uav, key[1]),
                        peer=(f"10.71.{uav}.10", 14600 + uav),
                        received_ns=sent_ns + 1_000_000,
                        datagram_sha256=f"{uav:x}" * 64,
                    )
        with mock.patch.object(
            probe.time,
            "monotonic_ns",
            return_value=latest_sent_ns + probe.OUTCOME_TIMEOUT_NS,
        ):
            instance._expire_pending()
        outcomes = [
            value["outcome"] for value in instance.retired_timesync_tokens.values()
        ]
        self.assertEqual(outcomes.count("success"), 5 * 95)
        self.assertEqual(outcomes.count("timeout"), 5 * 5)
        self.assertEqual(instance.sock.sendto.call_count, 5 * 100)
        self.assertFalse(instance.correlated_pending)
        self.assertFalse(instance.quarantined_uavs)

    def test_timeout_target_rejects_timesync_ack_and_telemetry(self) -> None:
        policy = self.correlated_policy(mixed_timeout_uav=1)
        for message_type in ("TIMESYNC", "COMMAND_ACK", "AUTOPILOT_VERSION"):
            with self.subTest(message_type=message_type):
                instance = self.correlated_instance()
                instance.active_policy = policy
                instance.active_phase = policy.window_id
                instance.expired_stopped_attempts = {
                    uav: [] for uav in range(1, 6)
                }
                instance.active_expired_stopped_attempts = {
                    uav: [] for uav in range(1, 6)
                }
                instance.last_stopped_timeout_monotonic_ns_by_uav = {
                    uav: None for uav in range(1, 6)
                }
                with mock.patch.object(
                    probe.time, "monotonic_ns", return_value=1_000_000_000
                ):
                    instance.send_request(policy, 1, 1)
                token = instance.pending[1].timesync_token
                message = (
                    self.timesync_message(1, int(token))
                    if message_type == "TIMESYNC"
                    else self.response_message(1, message_type)
                )
                with self.assertRaises(probe.ControlProbeError):
                    instance._handle_message(
                        message,
                        peer=("10.71.1.10", 14601),
                        received_ns=1_100_000_000,
                        datagram_sha256="4" * 64,
                    )

    def test_unknown_token_and_wrong_source_identity_fail_closed(self) -> None:
        instance = self.correlated_instance()
        policy = self.correlated_policy()
        instance.active_policy = policy
        instance.active_phase = policy.window_id
        unknown = probe.timesync_token(
            run_nonce=instance.args.run_nonce,
            phase_code=policy.transport_phase_code,
            uav=1,
            ordinal=77,
        )
        with self.assertRaises(probe.ControlProbeError):
            instance._handle_message(
                self.timesync_message(1, unknown),
                peer=("10.71.1.10", 14601),
                received_ns=1_100_000_000,
                datagram_sha256="5" * 64,
            )
        with self.assertRaises(probe.ControlProbeError):
            instance._handle_message(
                self.timesync_message(1, unknown),
                peer=("10.71.2.10", 14602),
                received_ns=1_100_000_000,
                datagram_sha256="6" * 64,
            )
    def test_mixed_per_uav_policy_accepts_background_ack_and_rejects_target_ack(
        self,
    ) -> None:
        instance = probe.ActualSitlControlProbe.__new__(
            probe.ActualSitlControlProbe
        )
        instance.writer = mock.Mock()
        instance.args = argparse.Namespace(profile="m4_capacity")
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
                **{
                    f"uav{uav}": probe.CORRELATED_TIMESYNC_POLICY
                    for uav in range(2, 6)
                },
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
        self.assertEqual(
            policy.response_policy_for(4), probe.CORRELATED_TIMESYNC_POLICY
        )
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

    def test_m3_and_m4_capacity_keep_exact_legacy_two_datagram_send_path(self) -> None:
        policy = probe.WindowPolicy(
            window_id="positive",
            transport_phase_code=1,
            start_monotonic_ns=1,
            end_monotonic_ns=10_000_000_000,
            offered_per_uav=1,
            send_span_ms=0,
            expected_engine_state="up_epoch_1",
            response_policies={uav: "ack_required" for uav in range(1, 6)},
            minimum_quiet_drain_ns_by_uav={uav: 0 for uav in range(1, 6)},
            flow_group_ids={
                uav: f"uav{uav}.control.downlink" for uav in range(1, 6)
            },
        )
        for profile, run_nonce in (
            ("m3", "78" * 16),
            ("m4_capacity", "78" * 32),
        ):
            with self.subTest(profile=profile):
                transport, derivation = probe.transport_nonce32(profile, run_nonce)
                expected = probe.encode_actual_control_request(
                    run_nonce=run_nonce,
                    transport_nonce=transport,
                    phase_code=1,
                    uav=1,
                    sequence=1,
                    mavlink=probe.MavlinkSequencer(),
                )
                instance = probe.ActualSitlControlProbe.__new__(
                    probe.ActualSitlControlProbe
                )
                instance.args = argparse.Namespace(
                    profile=profile,
                    run_nonce=run_nonce,
                    transport_nonce32=transport,
                    transport_nonce_derivation=derivation,
                )
                instance.pending = {}
                instance.correlated_pending = {}
                instance.retired_timesync_tokens = {}
                instance.quarantined_uavs = set()
                instance.sequencer = probe.MavlinkSequencer()
                instance.sock = mock.Mock()
                instance.sock.sendto.side_effect = (
                    lambda payload, _destination: len(payload)
                )
                instance.writer = mock.Mock()
                with mock.patch.object(probe.time, "monotonic_ns", return_value=2):
                    instance.send_request(policy, 1, 1)
                self.assertEqual(instance.sock.sendto.call_count, 2)
                self.assertEqual(
                    [call.args[0] for call in instance.sock.sendto.call_args_list],
                    [expected["marker_frame"], expected["command_frame"]],
                )
                self.assertEqual(instance.sequencer.value, 2)
                offer = instance.writer.emit.call_args
                self.assertNotIn("timesync_frame_hex", offer.kwargs)
                self.assertNotIn("request_transport_payload_hex", offer.kwargs)

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
