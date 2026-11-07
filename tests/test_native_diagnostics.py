"""Real UDP/BSF1/MAVLink diagnostics; no simulator or hardware qualification."""
import copy
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml

from network.scripts.serial_transport import Encoder, Reassembler
from scripts.product import native_radio_five_uav_scenario as native
from scripts.product import town01_full_stack_scenario as town


class NativeDiagnosticTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = yaml.safe_load(native.DEFAULT_SCENARIO_CONFIG.read_text())
        self.scenario = self.root / 'scenario.yaml'
        self.write(self.source)
        self.addCleanup(patch.stopall)
        patch.object(town, 'GCS_IP', '127.0.0.1').start()
        patch.object(town, 'endpoint_ip', lambda i: '127.0.0.1').start()
        patch.object(native, 'endpoint_ip', lambda i: '127.0.0.1').start()

    def write(self, config):
        self.scenario.write_text(yaml.safe_dump(config), encoding='utf-8')

    def arguments(self, **overrides):
        values = dict(run_dir=str(self.root / 'run'), node_state=str(self.root / 'node-state.json'),
            phase_file=str(self.root / 'phase.txt'), schedule_file=str(self.root / 'schedule.json'),
            timeout_scale=1., scenario_config=str(self.scenario), uav_count=1, mode='latency_diagnostic',
            channels='control,payload', radio_profile='native_wifi_80211n_spectrum_reference_v1')
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_only_explicit_nonflight_load_ignores_inapplicable_flight_plan(self):
        changed = copy.deepcopy(self.source)
        changed['flight']['takeoff_relative_altitude_m'] = {'uav1': 8}
        changed['flight']['missions'] = {'uav1': [dict(name='urban_waypoint', position_m=[20, 0, 8])]}
        self.write(changed)  # Retain legacy observations, which do not match urban waypoint names.
        original = self.scenario.read_bytes()
        with self.assertRaises(native.ScenarioError):
            native.load_flight_plan(self.scenario)
        parsed = native.load_flight_plan(self.scenario, include_flight=False)
        self.assertFalse(parsed['flight_included'])
        self.assertEqual(parsed['observations'], [])
        self.assertEqual(parsed['traffic']['diagnostic_retry_interval_s'], 20)
        self.assertEqual(self.scenario.read_bytes(), original)
        with self.assertRaises(native.ScenarioError):
            native.load_flight_plan(self.scenario)
        self.write(self.source)
        self.assertEqual(len(native.load_flight_plan(self.scenario)['observations']), 3)

    def test_diagnostic_constructor_binds_exact_roster_and_selected_channels(self):
        self.source['flight'] = {'observations': [{'name': 'unrelated_legacy_flight_point'}]}
        self.write(self.source)
        for count in range(1, 6):
            for channels in ('control', 'control,payload'):
                with self.subTest(count=count, channels=channels):
                    harness = native.NativeFiveUavHarness(self.arguments(uav_count=count, channels=channels))
                    try:
                        self.assertEqual(harness.uav_ids, tuple(range(1, count + 1)))
                        self.assertEqual(set(harness.sockets), set(channels.split(',')))
                        self.assertEqual(set(harness.transport_encoders), {(channel, i)
                            for channel in channels.split(',') for i in range(1, count + 1)})
                        self.assertEqual(harness.summary['mission_outcome'], 'not_applicable')
                        self.assertFalse(harness.summary['technology_specific_modem'])
                    finally:
                        harness.close()

    def test_full_product_still_rejects_partial_roster_and_invalid_flight_before_binding(self):
        with self.assertRaisesRegex(native.ScenarioError, 'all five'):
            native.NativeFiveUavHarness(self.arguments(mode='product', uav_count=2))
        self.source['flight']['observations'] = [{'name': 'not_a_mission_point'}]
        self.write(self.source)
        with self.assertRaisesRegex(native.ScenarioError, 'no matching mission point'):
            native.NativeFiveUavHarness(self.arguments(mode='product', uav_count=5))
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(('127.0.0.1', 14600))

    def test_legacy_product_keeps_five_uavs_all_channels_and_flight_observations(self):
        harness = native.NativeFiveUavHarness(self.arguments(mode='product', uav_count=5))
        try:
            self.assertEqual(harness.uav_ids, (1, 2, 3, 4, 5))
            self.assertEqual(set(harness.sockets), {'control', 'payload', 'additional_data'})
            self.assertEqual(len(harness.flight_observations), 3)
            self.assertEqual(set(harness.takeoff_altitudes), {1, 2, 3, 4, 5})
            self.assertNotEqual(harness.summary.get('mission_outcome'), 'not_applicable')
        finally:
            harness.close()

    def test_no_bypass_error_releases_exact_roster_sockets(self):
        args = self.arguments(run_dir=str(self.root), uav_count=2)
        args.radio_heartbeat = str(self.root / 'absent-heartbeat.json')
        args.duration_s, args.output = .1, str(self.root / 'probe.json')
        with self.assertRaises(FileNotFoundError):
            native.run_no_bypass_probe(args)
        for port in (14600, 14700, 14800):
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.bind(('127.0.0.1', port))

    def payload_exchange(self, behavior):
        harness = native.NativeFiveUavHarness(self.arguments())
        self.addCleanup(harness.close)
        from pymavlink import mavutil
        mav = mavutil.mavlink
        transmitter = mav.MAVLink(None, srcSystem=1, srcComponent=100 if behavior == 'foreign_component' else 1)
        encoder = Encoder(channel='payload', uav_id=1, direction='uart_to_gcs', max_payload=16)
        version = transmitter.autopilot_version_encode(0, 1, 0, 0, 0, [0]*8, [0]*8, [0]*8, 1, 1, 1)
        result = mav.MAV_RESULT_DENIED if behavior == 'rejected' else mav.MAV_RESULT_ACCEPTED
        ack = transmitter.command_ack_encode(mav.MAV_CMD_REQUEST_MESSAGE, result)
        errors, requests = [], []
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as peer:
            peer.bind(('127.0.0.1', 14701))
            peer.settimeout(2)
            def send(message):
                for frame in encoder.encode(message.pack(transmitter, force_mavlink1=False)):
                    peer.sendto(frame, ('127.0.0.1', 14700))
            if behavior == 'preflight_only':
                send(ack)
                send(version)
                deadline = time.monotonic() + 1
                while ('payload', 1, 'AUTOPILOT_VERSION') not in harness.latest and time.monotonic() < deadline:
                    harness.pump(.01)
                self.assertIn(('payload', 1, 'AUTOPILOT_VERSION'), harness.latest)
            def respond():
                try:
                    decoder = Reassembler(channel='payload', uav_id=1, direction='gcs_to_uart')
                    parser = mav.MAVLink(None)
                    while not requests:
                        for record in decoder.ingest(peer.recvfrom(65535)[0]):
                            requests.extend(parser.parse_buffer(record) or [])
                    request = requests[0]
                    if request.get_type() != 'COMMAND_LONG' or request.command != mav.MAV_CMD_REQUEST_MESSAGE or request.param1 != mav.MAVLINK_MSG_ID_AUTOPILOT_VERSION:
                        raise AssertionError('diagnostic sent an unexpected or flight-control command')
                    if behavior != 'preflight_only':
                        if behavior != 'missing_ack':
                            send(ack)
                        if behavior != 'missing_response':
                            send(version)
                except Exception as error:
                    errors.append(error)
            worker = threading.Thread(target=respond, daemon=True)
            worker.start()
            if behavior == 'complete':
                report = harness.payload_version_diagnostics((1,), timeout_s=.5)
                self.assertEqual(report['status'], 'passed')
            else:
                with self.assertRaisesRegex(native.ScenarioError, 'payload read-only AUTOPILOT_VERSION'):
                    harness.payload_version_diagnostics((1,), timeout_s=.5)
                report = harness.summary['latency_diagnostic']['payload_version_check']
                self.assertEqual(report['status'], 'failed')
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(len(requests), 1)
        self.assertEqual(set(harness.sockets), {'control', 'payload'})
        self.assertEqual(harness.summary['mission_outcome'], 'not_applicable')
        return report['uavs']['1']

    def test_real_payload_request_accepted_ack_and_version_response_are_required(self):
        row = self.payload_exchange('complete')
        self.assertTrue(row['accepted_ack_received'])
        self.assertTrue(row['response_received'])
        self.assertTrue(row['response_frame_hex'])
        self.assertGreaterEqual(row['response_received_monotonic_ns'], row['request_send_monotonic_ns'])

    def test_ack_alone_cannot_qualify_payload_downlink_response(self):
        row = self.payload_exchange('missing_response')
        self.assertTrue(row['accepted_ack_received'])
        self.assertFalse(row['response_received'])

    def test_version_alone_cannot_replace_accepted_ack(self):
        row = self.payload_exchange('missing_ack')
        self.assertFalse(row['accepted_ack_received'])
        self.assertTrue(row['response_received'])

    def test_preflight_packets_cannot_qualify_new_exchange(self):
        row = self.payload_exchange('preflight_only')
        self.assertFalse(row['accepted_ack_received'])
        self.assertFalse(row['response_received'])

    def test_rejected_ack_cannot_qualify_exchange(self):
        self.assertFalse(self.payload_exchange('rejected')['accepted_ack_received'])

    def test_non_autopilot_component_cannot_qualify_exchange(self):
        row = self.payload_exchange('foreign_component')
        self.assertFalse(row['accepted_ack_received'])
        self.assertFalse(row['response_received'])


if __name__ == '__main__':
    unittest.main()
