"""Real pymavlink parsing of routed UART traffic; no simulated flight results."""
from collections import Counter, deque
import select
import socket
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pymavlink.dialects.v20 import ardupilotmega as mavlink

from network.coordination.policy import Authority
from network.coordination.service import GCSService
from network.scripts.serial_transport import Encoder, Reassembler

NOW = 10_000_000_000


def heartbeat(system, component=1, custom_mode=4):
    encoder = mavlink.MAVLink(None, srcSystem=system, srcComponent=component)
    message = encoder.heartbeat_encode(mavlink.MAV_TYPE_QUADROTOR,
        mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA, mavlink.MAV_MODE_FLAG_SAFETY_ARMED,
        custom_mode, mavlink.MAV_STATE_ACTIVE)
    return bytes(message.pack(encoder, force_mavlink1=False))


class TelemetryRoutingTests(unittest.TestCase):
    def setUp(self):
        self.receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.receiver.bind(('127.0.0.1', 0))
        self.addCleanup(self.receiver.close)
        self.sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(self.sender.close)
        self.gcs = GCSService.__new__(GCSService)
        self.gcs.args = SimpleNamespace(telemetry_max_age_ms=250)
        self.grant = Authority('routing-test').issue(1, NOW - 1_000_000_000, 2_000_000_000)
        self.gcs.issued_grants = {self.grant.grant_sequence: self.grant}
        parser = mavlink.MAVLink(None)
        parser.robust_parsing = True
        self.gcs.uart_parsers = {3: parser}
        self.gcs.stream_provenance = {3: deque()}
        self.gcs.telemetry_provenance = {3: {}}
        self.gcs.telemetry_rejections = {3: Counter()}
        self.gcs.client_peers = {3: self.receiver.getsockname()}
        self.gcs.clients = {3: self.sender}
        self.gcs.latest_messages = {3: {}}
        self.gcs.latest_message_ns = {3: {}}
        self.gcs.latest_message_received_ns = {3: {}}
        self.events = []
        self.gcs.emit = lambda event, **fields: self.events.append((event, fields))

    def deliver(self, raw, producer_ns=NOW - 50_000_000):
        with patch('network.coordination.service.time.monotonic_ns', return_value=NOW):
            self.gcs.deliver(3, raw, producer_ns=producer_ns, grant=self.grant)

    def operator_packets(self):
        packets = []
        while select.select([self.receiver], [], [], .01)[0]:
            packets.append(self.receiver.recv(65535))
        return packets

    def test_routed_gcs_heartbeat_is_rejected_then_own_frame_is_delivered(self):
        foreign, own = heartbeat(255, 190), heartbeat(3)
        self.deliver(foreign + own)
        self.assertEqual(self.operator_packets(), [own])
        self.assertEqual(self.gcs.latest_messages[3]['HEARTBEAT'].get_srcSystem(), 3)
        self.assertEqual(self.gcs.telemetry_rejections[3]['foreign_sysid'], 1)
        self.assertEqual(list(self.gcs.stream_provenance[3]), [])
        rejection = [fields for event, fields in self.events if event == 'telemetry_source_rejected']
        self.assertEqual(rejection[0]['observed_system_id'], 255)
        self.assertEqual(rejection[0]['rejected_total'], 1)

    def test_wrong_uav_sysid_is_never_promoted_to_the_uart_origin(self):
        self.deliver(heartbeat(2))
        self.assertEqual(self.operator_packets(), [])
        self.assertEqual(self.gcs.latest_messages[3], {})
        self.assertEqual(self.gcs.latest_message_ns[3], {})
        self.assertEqual(self.gcs.telemetry_rejections[3]['foreign_sysid'], 1)
        self.assertEqual(list(self.gcs.stream_provenance[3]), [])
        own = heartbeat(3, custom_mode=9)
        self.deliver(own)
        self.assertEqual(self.operator_packets(), [own])
        self.assertEqual(self.gcs.latest_messages[3]['HEARTBEAT'].custom_mode, 9)

    def test_fragmented_bsf1_and_mavlink_keep_the_own_frame_producer_time(self):
        foreign, own = heartbeat(255, 190), heartbeat(3)
        raw_records = [foreign[:5], foreign[5:] + own[:7], own[7:]]
        producers = [NOW - 200_000_000, NOW - 100_000_000, NOW - 50_000_000]
        encoder = Encoder(channel='control', uav_id=3, direction='uart_to_gcs', max_payload=7)
        receiver = Reassembler(channel='control', uav_id=3, direction='uart_to_gcs', max_age_ms=250)
        with patch('network.coordination.service.time.monotonic_ns', return_value=NOW):
            for record, producer in zip(raw_records, producers):
                self.gcs.telemetry_provenance[3][producer] = self.grant
                fragments = encoder.encode(record, sent_monotonic_ns=producer)
                for fragment in reversed(fragments):
                    released = receiver.ingest(fragment, NOW)
                    self.gcs.release_telemetry(3, released, receiver.released_sent_ns)
        self.assertEqual(self.operator_packets(), [own])
        self.assertEqual(self.gcs.latest_message_ns[3]['HEARTBEAT'], producers[1])
        self.assertEqual(list(self.gcs.stream_provenance[3]), [])
        self.assertEqual(self.gcs.telemetry_provenance[3], {})

    def test_bad_data_and_foreign_frames_do_not_accumulate_provenance(self):
        corrupt = bytearray(heartbeat(3))
        corrupt[-1] ^= 1
        own = heartbeat(3, custom_mode=9)
        self.deliver(b'bad-prefix' + bytes(corrupt) + heartbeat(255, 190) + own)
        self.assertEqual(self.operator_packets(), [own])
        self.assertGreater(self.gcs.telemetry_rejections[3]['invalid_mavlink'], 0)
        self.assertEqual(self.gcs.telemetry_rejections[3]['foreign_sysid'], 1)
        self.assertEqual(list(self.gcs.stream_provenance[3]), [])

    def test_expired_record_stays_rejected_before_parsing(self):
        self.deliver(heartbeat(3), producer_ns=NOW - 251_000_000)
        self.assertEqual(self.operator_packets(), [])
        self.assertEqual(self.gcs.latest_messages[3], {})
        self.assertEqual(list(self.gcs.stream_provenance[3]), [])
        self.assertEqual(self.events[-1][0], 'telemetry_record_rejected')

    def test_fresh_suffix_cannot_make_an_expired_frame_prefix_eligible(self):
        own = heartbeat(3)
        self.deliver(own[:5], producer_ns=NOW - 100_000_000)
        with patch('network.coordination.service.time.monotonic_ns', return_value=NOW + 200_000_000):
            self.gcs.deliver(3, own[5:], producer_ns=NOW + 100_000_000, grant=self.grant)
        self.assertEqual(self.operator_packets(), [])
        self.assertEqual(self.gcs.latest_messages[3], {})
        self.assertEqual(list(self.gcs.stream_provenance[3]), [])
        self.assertEqual(self.events[-1][1]['reason'], 'expired_frame_prefix')
        with patch('network.coordination.service.time.monotonic_ns', return_value=NOW + 200_000_000):
            self.gcs.deliver(3, own, producer_ns=NOW + 150_000_000, grant=self.grant)
        self.assertEqual(self.operator_packets(), [own])


if __name__ == '__main__':
    unittest.main()
