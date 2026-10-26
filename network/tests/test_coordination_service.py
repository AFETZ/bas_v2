"""Protocol/dispatch fixtures; these are not native-radio or flight results."""

from __future__ import annotations

from types import SimpleNamespace
from collections import deque
import json
import importlib.util
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from network.coordination.policy import AdmissionGate, Authority, ProtocolError, encode, make_command
from network.coordination.service import Events, GCSService, Network, UAVService, mavlink_api
from network.scripts.serial_transport import Encoder, Reassembler


class CoordinationServiceTests(unittest.TestCase):
    def test_uart_event_carries_original_absolute_lease_for_offline_audit(self):
        grant = Authority("run").issue(1, 1000, 500)
        command = make_command(grant, destination_id=3, sequence=2, now_ns=1100, payload=b"unit-frame")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            emit = Events(path, "run", 3)
            emit("uart_admitted", command=command)
            emit.stream.close()
            row = json.loads(path.read_text())
        self.assertEqual((row["grant_sequence"], row["valid_from_ns"], row["valid_until_ns"]),
                         (grant.grant_sequence, grant.valid_from_ns, grant.valid_until_ns))

    def test_missing_native_heartbeat_after_readiness_is_failure(self):
        service = Network.__new__(Network)
        service.watchdog_seen_healthy = False
        with tempfile.TemporaryDirectory() as temporary:
            heartbeat = Path(temporary) / "heartbeat.json"
            service.args = SimpleNamespace(radio_watchdog_file=heartbeat, watchdog_s=.3)
            service.guard_radio()  # native has not reached readiness yet
            heartbeat.write_text(json.dumps(dict(healthy=True, monotonic_ns=time.monotonic_ns())))
            service.guard_radio()
            self.assertTrue(service.watchdog_seen_healthy)
            heartbeat.unlink()
            with self.assertRaisesRegex(RuntimeError, "disappeared"):
                service.guard_radio()

    def test_wrong_actual_uart_sysid_is_rejected(self):
        service = GCSService.__new__(GCSService)
        message = SimpleNamespace(get_type=lambda: "HEARTBEAT", get_srcSystem=lambda: 4)
        service.uart_parsers = {3: SimpleNamespace(parse_buffer=lambda raw: [message])}
        service.emit = lambda *args, **kw: None
        now = time.monotonic_ns()
        grant = Authority("run").issue(1, now - 1_000_000, 1_000_000_000)
        service.issued_grants = {grant.grant_sequence: grant}
        service.args = SimpleNamespace(telemetry_max_age_ms=250)
        service.stream_provenance = {3: deque()}
        with self.assertRaisesRegex(ProtocolError, "SYSID"):
            service.deliver(3, b"unit-parser-fixture", producer_ns=now, grant=grant)

    def test_expire_release_cannot_rejuvenate_telemetry_past_original_lease(self):
        grant = Authority("run").issue(1, 1_000_000_000, 1_000_000_000)
        service = GCSService.__new__(GCSService)
        service.args = SimpleNamespace(telemetry_max_age_ms=250)
        service.issued_grants = {grant.grant_sequence: grant}
        service.telemetry_provenance = {3: {1_800_000_000: grant}}
        service.emit = lambda *args, **kw: None
        delivered = []
        service.deliver = lambda *args, **kw: delivered.append(args)
        # Real BSF1 gap reproduces the former expire()->deliver path.
        encoder = Encoder(channel="control", uav_id=3, direction="uart_to_gcs")
        encoder.encode(b"lost seq0", sent_monotonic_ns=1_700_000_000)
        raw = encoder.encode(b"unit serial record", sent_monotonic_ns=1_800_000_000)[0]
        old_receiver = Reassembler(channel="control", uav_id=3, direction="uart_to_gcs")
        self.assertEqual(old_receiver.ingest(raw, 1_810_000_000), [])
        released = old_receiver.expire(2_310_000_000)
        self.assertEqual(released, [b"unit serial record"])
        with patch("network.coordination.service.time.monotonic_ns", return_value=2_310_000_000):
            service.release_telemetry(3, released, old_receiver.released_sent_ns)
        self.assertEqual(delivered, [])
        bounded = Reassembler(channel="control", uav_id=3, direction="uart_to_gcs", max_age_ms=250)
        bounded.ingest(raw, 1_810_000_000)
        self.assertEqual(bounded.expire(2_310_000_000), [])

    def test_new_same_epoch_renewal_cannot_adopt_old_expired_telemetry(self):
        issuer = Authority("run")
        old = issuer.issue(1, 1_000_000_000, 1_000_000_000)
        renewal = issuer.issue(1, 2_010_000_000, 1_000_000_000)
        self.assertEqual(old.epoch, renewal.epoch)
        service = GCSService.__new__(GCSService)
        service.args = SimpleNamespace(telemetry_max_age_ms=250)
        service.issued_grants = {old.grant_sequence: old, renewal.grant_sequence: renewal}
        self.assertFalse(service.telemetry_eligible(old, 1_950_000_000, 2_100_000_000))
        self.assertTrue(service.telemetry_eligible(renewal, 2_050_000_000, 2_100_000_000))

    def test_message_freshness_uses_earliest_uart_fragment_not_release_time(self):
        service = GCSService.__new__(GCSService)
        service.args = SimpleNamespace(telemetry_max_age_ms=250)
        grant = Authority("run").issue(1, 1_000_000_000, 3_000_000_000)
        service.issued_grants = {grant.grant_sequence: grant}
        service.stream_provenance = {3: deque()}
        service.latest_messages, service.latest_message_ns = {3: {}}, {3: {}}
        service.latest_message_received_ns = {3: {}}
        service.client_peers = {}
        message = SimpleNamespace(get_type=lambda: "HEARTBEAT", get_srcSystem=lambda: 3,
                                  get_msgbuf=lambda: b"unit-frame", custom_mode=4, base_mode=0)
        output = iter([[], [message]])
        service.uart_parsers = {3: SimpleNamespace(parse_buffer=lambda raw: next(output))}
        service.emit = lambda *args, **kw: None
        with patch("network.coordination.service.time.monotonic_ns", return_value=2_000_000_000):
            service.deliver(3, b"unit-", producer_ns=1_800_000_000, grant=grant)
            service.deliver(3, b"frame", producer_ns=1_950_000_000, grant=grant)
        self.assertEqual(service.latest_message_ns[3]["HEARTBEAT"], 1_800_000_000)
        self.assertEqual(service.latest_message_received_ns[3]["HEARTBEAT"], 2_000_000_000)

    def test_frame_prefix_from_expired_lease_is_not_adopted_by_live_renewal(self):
        issuer = Authority("run")
        old = issuer.issue(1, 1_000_000_000, 1_000_000_000)
        renewal = issuer.issue(1, 1_500_000_000, 1_000_000_000)
        service = GCSService.__new__(GCSService)
        service.args = SimpleNamespace(telemetry_max_age_ms=250)
        service.issued_grants = {old.grant_sequence: old, renewal.grant_sequence: renewal}
        service.stream_provenance = {3: deque()}
        service.latest_messages, service.latest_message_ns = {3: {}}, {3: {}}
        service.latest_message_received_ns = {3: {}}
        service.client_peers = {}
        message = SimpleNamespace(get_type=lambda: "HEARTBEAT", get_srcSystem=lambda: 3,
                                  get_msgbuf=lambda: b"unit-frame", custom_mode=4, base_mode=0)
        output = iter([[], [message]])
        service.uart_parsers = {3: SimpleNamespace(parse_buffer=lambda raw: next(output))}
        service.emit = lambda *args, **kw: None
        with patch("network.coordination.service.time.monotonic_ns", return_value=1_950_000_000):
            service.deliver(3, b"unit-", producer_ns=1_900_000_000, grant=old)
        with patch("network.coordination.service.time.monotonic_ns", return_value=2_100_000_000):
            service.deliver(3, b"frame", producer_ns=2_050_000_000, grant=renewal)
        self.assertEqual(service.latest_messages[3], {})

    def test_new_epoch_or_lease_gap_discards_parser_and_reassembly_buffers(self):
        issuer = Authority("run")
        old = issuer.issue(1, 1_000_000_000, 1_000_000_000)
        new = issuer.issue(2, 1_500_000_000, 1_000_000_000)
        service = GCSService.__new__(GCSService)
        service.args = SimpleNamespace(telemetry_max_age_ms=250)
        service.telemetry_scope = {3: ((old.epoch, old.leader_id), old.valid_until_ns)}
        service.telemetry_provenance = {3: {1_900_000_000: old}}
        service.stream_provenance = {3: deque([(b"old-prefix", 1_900_000_000, old)])}
        service.receivers, service.uart_parsers = {3: object()}, {3: object()}
        service.latest_messages = {3: {"HEARTBEAT": object()}}
        service.latest_message_ns, service.latest_message_received_ns = {3: {"HEARTBEAT": 1}}, {3: {"HEARTBEAT": 2}}
        service.mavutil = SimpleNamespace(mavlink=SimpleNamespace(MAVLink=lambda unused: SimpleNamespace()))
        service.emit = lambda *args, **kw: None
        service.prepare_telemetry_scope(3, new, new.valid_from_ns, 57)
        self.assertEqual(service.receivers[3].expected_sequence, 57)
        self.assertEqual(list(service.stream_provenance[3]), [])
        self.assertEqual(service.latest_messages[3], {})

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
            1: (now - 2_000_000_000, dict(sent_ns=now - 2_000_000_000, echo_ns=now - 2_000_000_000, peer_echo_ns=fresh)),
            2: (now, dict(sent_ns=now, echo_ns=now - 10, peer_echo_ns=fresh)),
            3: (now, dict(sent_ns=now, echo_ns=now - 10, peer_echo_ns=stale)),
            4: (now, dict(sent_ns=now + 1, echo_ns=now - 10, peer_echo_ns=fresh)),
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
        service.args.detection_s = 1
        service.heartbeat = {}
        service.detected_unavailable_leader = None
        service.emit = lambda *args, **kw: None
        service.available = lambda now: [2]
        service.update_role(2000)
        self.assertEqual(service.authority.leader_id, 1)
        self.assertEqual(service.authority.epoch, 1)

    def test_peer_report_gap_does_not_replace_master_with_live_direct_link(self):
        now = 10_000_000_000
        service = GCSService.__new__(GCSService)
        service.args = SimpleNamespace(detection_s=1, initial_master=1,
                                      policy="failover", renew_s=.5, lease_s=2)
        service.authority = Authority("run")
        old = service.authority.issue(1, now - 500_000_000, 2_000_000_000)
        service.grants_by_epoch, service.issued_grants = {1: old}, {old.grant_sequence: old}
        service.last_renew_ns, service.detected_unavailable_leader = now - 500_000_000, None
        peer = {str(i): now - 390_000_000 for i in range(1, 6)}
        service.heartbeat = {
            1: (now - 187_000_000, dict(sent_ns=now - 191_788_000, echo_ns=now - 193_183_000,
                                      peer_echo_ns=dict(peer, **{"4": now - 1_008_124_000}))),
            2: (now - 10_000_000, dict(sent_ns=now - 11_000_000, echo_ns=now - 12_000_000,
                                     peer_echo_ns=peer)),
        }
        service.peer_rtt_ns = {1: 2_000_000, 2: 1_000_000}
        service.emit = lambda *args, **fields: None
        service.send = lambda *args: True
        self.assertNotIn(1, service.available(now))  # peer4 report really is too old
        service.update_role(now)
        self.assertEqual((service.authority.leader_id, service.authority.epoch), (1, 1))
        # The same threshold now detects a genuinely stale direct GCS probe.
        service.heartbeat[1][1]["echo_ns"] = now - 1_000_000_001
        service.update_role(now)
        self.assertEqual((service.authority.leader_id, service.authority.epoch), (2, 2))


@unittest.skipUnless(importlib.util.find_spec("pymavlink"), "requires pinned runtime pymavlink")
class NativeMavlinkAdapterTests(unittest.TestCase):
    def test_real_mavlink_frame_keeps_first_uart_fragment_timestamp(self):
        mavutil = mavlink_api()
        tx = mavutil.mavlink.MAVLink(None, srcSystem=3, srcComponent=1)
        frame = tx.heartbeat_encode(mavutil.mavlink.MAV_TYPE_QUADROTOR,
                    mavutil.mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA, 0, 4,
                    mavutil.mavlink.MAV_STATE_STANDBY).pack(tx)
        now = time.monotonic_ns()
        grant = Authority("native-api-unit").issue(1, now - 100_000_000, 2_000_000_000)
        service = GCSService.__new__(GCSService)
        service.args = SimpleNamespace(telemetry_max_age_ms=250)
        service.issued_grants = {grant.grant_sequence: grant}
        service.stream_provenance = {3: deque()}
        service.latest_messages, service.latest_message_ns = {3: {}}, {3: {}}
        service.latest_message_received_ns = {3: {}}
        service.client_peers = {}
        parser = mavutil.mavlink.MAVLink(None)
        parser.robust_parsing = True
        service.uart_parsers = {3: parser}
        service.emit = lambda *args, **kw: None
        first, second = now - 30_000_000, now - 10_000_000
        service.deliver(3, frame[:7], producer_ns=first, grant=grant)
        service.deliver(3, frame[7:], producer_ns=second, grant=grant)
        self.assertEqual(service.latest_message_ns[3]["HEARTBEAT"], first)
        self.assertEqual(service.latest_messages[3]["HEARTBEAT"].custom_mode, 4)

    def test_pinned_guided_position_and_brake_frames_use_native_api(self):
        from scripts.product.relay_experiment import Goal, RelayExperiment
        mavutil = mavlink_api()
        scenario = RelayExperiment.__new__(RelayExperiment)
        scenario.mav = mavutil.mavlink
        scenario.tx = scenario.mav.MAVLink(None, srcSystem=255, srcComponent=191)
        scenario.anchors = {3: dict(lat_int=-353632621, lon_int=1491652374)}
        message = scenario.setpoint(3, Goal("native-unit", 3, 1, 1., 16., (0., 0., 16.)))
        decoded = scenario.mav.MAVLink(None).parse_buffer(message.pack(scenario.tx))[0]
        self.assertEqual((decoded.target_system, decoded.coordinate_frame, decoded.type_mask), (3, 6, 3576))
        self.assertEqual(decoded.alt, 16.)
        endpoint = UAVService.__new__(UAVService)
        endpoint.index, endpoint.mavutil = 3, mavutil
        endpoint.mav = scenario.mav.MAVLink(None, srcSystem=255, srcComponent=190)
        endpoint.request_hold()
        brake = scenario.mav.MAVLink(None).parse_buffer(endpoint.hold_pending)[0]
        self.assertEqual((brake.target_system, brake.custom_mode), (3, 17))


if __name__ == "__main__":
    unittest.main()
