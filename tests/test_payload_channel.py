"""Actual UDP/BSF1/MAVLink integration; run with target pymavlink dependencies."""
import json
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import patch

from network.coordination.payload import PayloadChannel
from network.scripts.serial_transport import Encoder, Reassembler
from scripts.product import town01_full_stack_scenario as town


class PayloadChannelTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.addCleanup(patch.stopall)
        patch.object(town, 'GCS_IP', '127.0.0.1').start()
        patch.object(town, 'endpoint_ip', lambda index: '127.0.0.1').start()

    def helper(self, ids=(1,)):
        helper = PayloadChannel(self.folder, self.folder / 'node_state.json', ids)
        self.addCleanup(helper.close)
        return helper

    def test_payload_only_binds_no_control_socket_and_reports_absence(self):
        helper = self.helper((1, 3))
        self.assertEqual(set(helper.harness.sockets), {'payload'})
        self.assertEqual(set(helper.harness.transport_encoders), {('payload', 1), ('payload', 3)})
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
            control.bind(('127.0.0.1', 14600))
        self.assertFalse(helper.snapshot()['all_uavs_bidirectional_observed'])
        result = helper.close()
        self.assertEqual(result['hardware_validation'], 'not_performed')
        self.assertIsNone(result['uavs']['1']['last_attitude_age_s'])
        self.assertEqual(helper.close(), result)

    def test_real_udp_bsf1_mavlink_requests_and_observations(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as peer:
            peer.bind(('127.0.0.1', 14701))
            peer.settimeout(1)
            helper = self.helper()
            from pymavlink import mavutil
            parser = mavutil.mavlink.MAVLink(None)
            transmitter = mavutil.mavlink.MAVLink(None)
            transmitter.srcSystem, transmitter.srcComponent = 1, 1
            receiver = Reassembler(channel='payload', uav_id=1, direction='gcs_to_uart')
            # Force fragmentation in the returning path, with real CRC and parser.
            encoder = Encoder(channel='payload', uav_id=1, direction='uart_to_gcs', max_payload=8)
            helper.pump()
            received_types = []
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline and 'COMMAND_LONG' not in received_types:
                data, source = peer.recvfrom(65535)
                for record in receiver.ingest(data):
                    for message in parser.parse_buffer(record) or []:
                        received_types.append(message.get_type())
                        if message.get_type() == 'COMMAND_LONG':
                            self.assertEqual(message.command, mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL)
                            self.assertEqual(message.param1, mavutil.mavlink.MAVLINK_MSG_ID_ATTITUDE)
                            self.assertEqual(message.target_system, 1)
            self.assertEqual(set(received_types), {'HEARTBEAT', 'COMMAND_LONG'})
            replies = [transmitter.heartbeat_encode(mavutil.mavlink.MAV_TYPE_QUADROTOR,
                           mavutil.mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA, 0, 0, mavutil.mavlink.MAV_STATE_STANDBY),
                       transmitter.command_ack_encode(mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL,
                           mavutil.mavlink.MAV_RESULT_ACCEPTED),
                       transmitter.attitude_encode(12345, .1, .2, .3, 0., 0., 0.)]
            for reply in replies:
                frame = reply.pack(transmitter, force_mavlink1=False)
                for chunk in encoder.encode(frame):
                    peer.sendto(chunk, source)
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                helper.pump()
                if helper.snapshot()['all_uavs_bidirectional_observed']:
                    break
                time.sleep(.001)
            metrics = helper.close()
            self.assertTrue(metrics['all_uavs_bidirectional_observed'])
            self.assertEqual(metrics['uavs']['1']['attitudes_received'], 1)
            self.assertEqual(metrics['uavs']['1']['last_attitude_autopilot_time_boot_ms'], 12345)
            self.assertGreater(metrics['transport']['payload:uav1']['uart_output_bytes'], 0)
            saved = json.loads((self.folder / 'metrics/payload_channel.json').read_text())
            self.assertEqual(saved, metrics)

    def test_wrong_roster_frame_cannot_count_as_payload_telemetry(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as peer:
            peer.bind(('127.0.0.1', 14702))
            helper = self.helper((1,))
            from pymavlink import mavutil
            transmitter = mavutil.mavlink.MAVLink(None)
            transmitter.srcSystem = 2
            frame = transmitter.heartbeat_encode(mavutil.mavlink.MAV_TYPE_QUADROTOR,
                mavutil.mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA, 0, 0, mavutil.mavlink.MAV_STATE_STANDBY).pack(transmitter)
            encoder = Encoder(channel='payload', uav_id=2, direction='uart_to_gcs')
            for chunk in encoder.encode(frame):
                peer.sendto(chunk, ('127.0.0.1', 14700))
            helper.pump()
            metrics = helper.close()
            self.assertFalse(metrics['all_uavs_bidirectional_observed'])
            self.assertGreater(metrics['transport']['malformed_datagrams'], 0)

    def test_ack_received_before_our_request_cannot_prove_downlink(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as peer:
            peer.bind(('127.0.0.1', 14701))
            helper = self.helper()
            from pymavlink import mavutil
            transmitter = mavutil.mavlink.MAVLink(None)
            transmitter.srcSystem = 1
            frame = transmitter.command_ack_encode(mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL,
                mavutil.mavlink.MAV_RESULT_ACCEPTED).pack(transmitter)
            encoder = Encoder(channel='payload', uav_id=1, direction='uart_to_gcs')
            for chunk in encoder.encode(frame):
                peer.sendto(chunk, ('127.0.0.1', 14700))
            helper.pump()
            metrics = helper.close()
            self.assertEqual(metrics['uavs']['1']['accepted_interval_acknowledgements'], 0)
            self.assertFalse(metrics['all_uavs_bidirectional_observed'])

    def test_invalid_roster_and_timing_are_rejected_before_binding(self):
        for ids in ((), (1, 1), (0,), (6,), (True,)):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                self.helper(ids)
        with self.assertRaises(ValueError):
            PayloadChannel(self.folder, self.folder / 'state.json', (1,), interval_s=float('nan'))


if __name__ == '__main__':
    unittest.main()
