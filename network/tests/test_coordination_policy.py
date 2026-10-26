"""Focused protocol tests; no simulated ACK or claimed flight outcome."""

from __future__ import annotations

import dataclasses
import unittest

from network.coordination.policy import (
    AdmissionGate, Authority, Grant, ProtocolError, UARTAdmission,
    decode, encode, make_command,
)


class CoordinationPolicyTests(unittest.TestCase):
    def setUp(self):
        self.now = 10_000
        self.holds = []
        self.events = []
        self.authority = Authority("run-1", skew_margin_ns=10)
        self.gate = AdmissionGate("run-1", 3, now_ns=lambda: self.now,
                                  hold=lambda: self.holds.append(self.now))

    def grant(self, leader=1, lease=1000):
        grant = self.authority.issue(leader, self.now, lease)
        self.gate.receive_grant(grant, sender_id=0)
        return grant

    def command(self, grant, seq=1, payload=b"opaque-native-frame"):
        return make_command(grant, destination_id=3, sequence=seq,
                            now_ns=self.now, payload=payload)

    def test_wire_round_trip_preserves_bytes_and_metadata(self):
        grant = self.grant()
        command = dataclasses.replace(self.command(grant, payload=bytes(range(256))),
                                      target_position_m=(1.0, 2.0, 4.0), target_frame="ENU")
        self.assertEqual(decode(encode("grant", 0, grant)), ("grant", 0, grant))
        self.assertEqual(decode(encode("command", 1, command)), ("command", 1, command))

    def test_only_gcs_can_issue_grants(self):
        grant = self.authority.issue(1, self.now, 1000)
        with self.assertRaises(ProtocolError):
            self.gate.receive_grant(grant, sender_id=2)
        with self.assertRaises(ProtocolError):
            decode(encode("grant", 2, grant))
        with self.assertRaises(ProtocolError):
            dataclasses.replace(grant, issuer_id=1)

    def test_former_master_accepts_unmodified_gcs_grant_via_new_master(self):
        former = AdmissionGate("run-1", 1, now_ns=lambda: self.now, hold=lambda: None)
        old = self.authority.issue(1, self.now, 1000)
        former.receive_grant(old, sender_id=0)
        self.now += 100
        new = self.authority.issue(2, self.now, 1000)
        # Direct GCS->UAV1 has failed; original GCS-issued grant takes UAV2->UAV1.
        kind, sender, forwarded = decode(encode("grant", 2, new))
        self.assertEqual(forwarded, new)
        former.receive_grant(forwarded, sender_id=sender)
        self.now = new.valid_from_ns
        command = make_command(new, destination_id=1, sequence=1,
                               now_ns=self.now, payload=b"opaque-native-frame")
        former.check(command, sender_id=2)

    def test_unacknowledged_renewal_delays_new_leader(self):
        first = self.grant()
        self.now += 500
        lost_renewal = self.authority.issue(1, self.now, 1000)
        # Gate never received this renewal; issuer must still account for it.
        self.now += 100
        new = self.authority.issue(2, self.now, 1000)
        self.assertEqual(new.valid_from_ns, lost_renewal.valid_until_ns + 10)
        self.assertGreater(new.valid_from_ns, first.valid_until_ns)
        self.gate.receive_grant(new, sender_id=0)
        self.assertEqual(self.gate.active_grant(), first)
        self.now = new.valid_from_ns
        self.gate.check(self.command(new), sender_id=2)

    def test_future_new_epoch_does_not_prematurely_disable_old_lease(self):
        first = self.grant()
        self.now += 100
        future = self.authority.issue(2, self.now, 1000)
        self.gate.receive_grant(future, sender_id=0)
        self.gate.check(self.command(first), sender_id=1)
        with self.assertRaises(ProtocolError):
            self.gate.check(self.command(future), sender_id=2)

    def test_delayed_old_renewal_is_rejected_after_new_grant_seen(self):
        old = self.grant()
        self.now += 100
        delayed = self.authority.issue(1, self.now, 1000)
        new = self.authority.issue(2, self.now, 1000)
        self.gate.receive_grant(new, sender_id=0)
        with self.assertRaisesRegex(ProtocolError, "old grant epoch"):
            self.gate.receive_grant(delayed, sender_id=0)
        self.now = new.valid_from_ns
        with self.assertRaises(ProtocolError):
            self.gate.check(self.command(old), sender_id=1)

    def test_different_followers_cannot_execute_two_issued_leaders_together(self):
        old = self.grant()
        other = AdmissionGate("run-1", 4, now_ns=lambda: self.now, hold=lambda: None)
        self.now += 100
        lost = self.authority.issue(1, self.now, 1000)
        self.gate.receive_grant(lost, sender_id=0)
        new = self.authority.issue(2, self.now, 1000)
        other.receive_grant(new, sender_id=0)
        # Only one endpoint knows epoch2; safety still follows from issuer intervals.
        for when in (self.now, old.valid_until_ns, lost.valid_until_ns, new.valid_from_ns):
            self.now = when
            old_active = self.gate.active_grant()
            new_active = other.active_grant()
            self.assertFalse(old_active is not None and new_active is not None)

    def test_duplicate_grant_does_not_extend_deadline(self):
        grant = self.grant()
        self.now += 700
        self.assertFalse(self.gate.receive_grant(grant, sender_id=0))
        self.now = grant.valid_until_ns
        self.assertIsNone(self.gate.active_grant())
        with self.assertRaises(ProtocolError):
            self.gate.receive_grant(grant, sender_id=0)

    def test_overlapping_new_leader_is_rejected(self):
        old = self.grant()
        bad = Grant("run-1", 2, 2, 2, self.now, self.now, old.valid_until_ns + 1)
        with self.assertRaisesRegex(ProtocolError, "overlapping"):
            self.gate.receive_grant(bad, sender_id=0)

    def test_duplicate_and_reordered_commands_do_not_reach_uart(self):
        grant = self.grant()
        written = []
        uart = UARTAdmission(self.gate, lambda b: written.append(b) or len(b))
        command = self.command(grant, seq=2)
        uart.enqueue(command, sender_id=1)
        with self.assertRaisesRegex(ProtocolError, "pending"):
            uart.enqueue(command, sender_id=1)
        uart.drain_one()
        for seq in (1, 2):
            with self.assertRaises(ProtocolError):
                uart.enqueue(self.command(grant, seq), sender_id=1)
        self.assertEqual(written, [command.payload])

    def test_queued_command_rechecked_at_actual_uart_boundary(self):
        grant = self.grant()
        written = []
        uart = UARTAdmission(self.gate, lambda b: written.append(b) or len(b),
                             emit=lambda event, **kw: self.events.append(event))
        uart.enqueue(self.command(grant), sender_id=1)
        self.now = grant.valid_until_ns
        uart.drain_one()
        self.assertEqual(written, [])
        self.assertEqual(self.events, ["command_rejected"])

    def test_partial_write_expiry_is_explicit_run_error(self):
        grant = self.grant()
        written = []
        uart = UARTAdmission(self.gate, lambda b: written.append(b[:2]) or 2)
        uart.enqueue(self.command(grant), sender_id=1)
        uart.drain_one()
        self.now = grant.valid_until_ns
        with self.assertRaisesRegex(RuntimeError, "partially written"):
            uart.drain_one()
        self.assertEqual(len(written), 1)

    def test_partial_write_then_complete_emits_exact_payload_once(self):
        grant = self.grant()
        written = []
        uart = UARTAdmission(self.gate, lambda b: written.append(b[:2]) or min(2, len(b)))
        command = self.command(grant, payload=b"abcdef")
        uart.enqueue(command, sender_id=1)
        while uart.drain_one():
            pass
        self.assertEqual(b"".join(written), command.payload)

    def test_expiry_requests_hold_once_and_never_appoints_a_new_leader(self):
        grant = self.grant()
        self.gate.tick()
        self.now = grant.valid_until_ns
        self.gate.tick()
        self.gate.tick()
        self.assertEqual(self.holds, [self.now])
        self.assertEqual(self.gate.highest_epoch, 1)

    def test_restart_with_new_run_rejects_old_buffered_messages(self):
        grant = self.grant()
        restarted = AdmissionGate("run-2", 3, now_ns=lambda: self.now, hold=lambda: None)
        with self.assertRaises(ProtocolError):
            restarted.receive_grant(grant, sender_id=0)
        with self.assertRaises(ProtocolError):
            restarted.check(self.command(grant), sender_id=1)

    def test_wrong_leader_destination_interval_and_future_command_rejected(self):
        grant = self.grant()
        command = self.command(grant)
        bad = (dataclasses.replace(command, destination_id=4),
               dataclasses.replace(command, valid_until_ns=command.valid_until_ns + 1),
               dataclasses.replace(command, created_ns=self.now + 1))
        for item in bad:
            with self.assertRaises(ProtocolError):
                self.gate.check(item, sender_id=1)
        with self.assertRaises(ProtocolError):
            self.gate.check(command, sender_id=2)

    def test_malformed_wire_and_large_payload_rejected(self):
        for raw in (b"null", b"[]", b"not-json", b'{"v":true}', b"x" * 1401):
            with self.assertRaises(ProtocolError):
                decode(raw)
        grant = self.grant()
        with self.assertRaises(ProtocolError):
            self.command(grant, payload=b"x" * 281)


if __name__ == "__main__":
    unittest.main()
