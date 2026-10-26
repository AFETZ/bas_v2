"""Protocol/dispatch fixtures; these are not native-radio or flight results."""

from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from network.coordination.policy import AdmissionGate, Authority, ProtocolError, encode, make_command
from network.coordination.service import GCSService, Network, UAVService


class CoordinationServiceTests(unittest.TestCase):
    def test_old_master_gets_exact_gcs_grant_via_new_master(self):
        now = 1000
        grant = Authority("run").issue(2, now, 100)
        service = UAVService.__new__(UAVService)
        service.index = 2
        service.gate = AdmissionGate("run", 2, now_ns=lambda: now, hold=lambda: None)
        service.emit = lambda *args, **kw: None
        sent = []
        service.send = lambda kind, body, destination: sent.append((kind, body, destination)) or True
        service.handle(("grant", 0, grant))
        self.assertEqual([x[2] for x in sent], [1, 3, 4, 5])
        self.assertTrue(all(x[1] is grant for x in sent))
        # Receipt of the forwarded grant by an ordinary follower never fans out again.
        service.index = 1
        service.gate = AdmissionGate("run", 1, now_ns=lambda: now, hold=lambda: None)
        sent.clear()
        service.handle(("grant", 2, grant))
        self.assertEqual(sent, [])

    def test_source_address_must_match_claimed_sender(self):
        grant = Authority("run").issue(2, 1000, 100)
        service = Network.__new__(Network)
        service.args = SimpleNamespace(port=15000, run_id="run")
        raw = encode("grant", 2, grant)
        service.udp = SimpleNamespace(recvfrom=lambda count: (raw, ("10.71.1.10", 15000)))
        with self.assertRaisesRegex(ProtocolError, "source address"):
            service.receive()

    def test_direct_gcs_command_cannot_execute_on_follower(self):
        now = 1000
        grant = Authority("run").issue(2, now, 100)
        service = UAVService.__new__(UAVService)
        service.index = 1
        service.gate = AdmissionGate("run", 1, now_ns=lambda: now, hold=lambda: None)
        service.gate.receive_grant(grant, sender_id=0)
        command = make_command(grant, destination_id=1, sequence=1, now_ns=now, payload=b"unit-fixture")
        with self.assertRaisesRegex(ProtocolError, "bypass"):
            service.command(0, command)

    def test_forwarding_is_not_ordered_by_coordinators_own_execution_sequence(self):
        now = 1000
        grant = Authority("run").issue(2, now, 100)
        service = UAVService.__new__(UAVService)
        service.index = 2
        service.gate = AdmissionGate("run", 2, now_ns=lambda: now, hold=lambda: None)
        service.gate.receive_grant(grant, sender_id=0)
        local = make_command(grant, destination_id=2, sequence=10, now_ns=now, payload=b"local-unit")
        service.gate.check(local, sender_id=2, reserve=True)
        sent = []
        service.emit = lambda *args, **kw: None
        service.send = lambda kind, body, destination: sent.append(body) or True
        peer = make_command(grant, destination_id=1, sequence=9, now_ns=now, payload=b"peer-unit")
        service.command(0, peer)
        self.assertEqual(sent, [peer])

    def test_candidate_requires_live_bidirectional_peer_reports(self):
        service = GCSService.__new__(GCSService)
        service.args = SimpleNamespace(detection_s=1)
        now = 10_000_000_000
        fresh = {str(i): now - 10 for i in range(1, 6)}
        stale = dict(fresh, **{"1": now - 2_000_000_000})
        service.heartbeat = {
            1: (now - 2_000_000_000, dict(sent_ns=now - 2_000_000_000, peer_echo_ns=fresh)),
            2: (now, dict(sent_ns=now, peer_echo_ns=fresh)),
            3: (now, dict(sent_ns=now, peer_echo_ns=stale)),
            4: (now, dict(sent_ns=now + 1, peer_echo_ns=fresh)),
        }
        service.peer_rtt_ns = {1: 1, 2: 10, 3: 2, 4: 1}
        self.assertEqual(service.available(now), [2])

    def test_no_candidate_means_no_appointment(self):
        service = GCSService.__new__(GCSService)
        service.authority = Authority("run")
        service.args = SimpleNamespace(initial_master=1, policy="failover")
        service.available = lambda now: []
        service.update_role(1000)
        self.assertEqual(service.authority.epoch, 0)
        self.assertIsNone(service.authority.leader_id)

    def test_fixed_policy_does_not_choose_another_master(self):
        service = GCSService.__new__(GCSService)
        service.authority = Authority("run")
        service.authority.issue(1, 1000, 100)
        service.args = SimpleNamespace(initial_master=1, policy="fixed")
        service.detected_unavailable_leader = None
        service.emit = lambda *args, **kw: None
        service.available = lambda now: [2]
        service.update_role(2000)
        self.assertEqual(service.authority.leader_id, 1)
        self.assertEqual(service.authority.epoch, 1)


if __name__ == "__main__":
    unittest.main()
