"""Real UDP/BSF1/MAVLink diagnostics; no simulator or hardware qualification."""
import copy
from collections import Counter
import hashlib
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
from scripts.product.test_radio_hitl import CATALOG, resolve


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

    def test_bench_duration_rejects_invalid_or_flight_mode_before_binding(self):
        for duration in (True, 0, float('nan'), float('inf'), 86401):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                native.NativeFiveUavHarness(self.arguments(bench_duration_s=duration))
        with self.assertRaisesRegex(native.ScenarioError, 'latency_diagnostic'):
            native.NativeFiveUavHarness(self.arguments(mode='product', uav_count=5, bench_duration_s=20))

    def test_bench_window_observes_declared_host_time_and_actual_received_counts(self):
        harness = native.NativeFiveUavHarness.__new__(native.NativeFiveUavHarness)
        harness.uav_ids, harness.channels = (1, 2), ('control', 'payload')
        harness.summary, harness.message_counts = {}, Counter({('control', 1, 'HEARTBEAT'): 7})
        phases, calls, now = [], [], [100_000_000_000]
        harness.phase = phases.append
        def pump(duration):
            calls.append(duration)
            now[0] += round(duration * 1e9)
            for channel in harness.channels:
                for i in harness.uav_ids:
                    harness.message_counts[(channel, i, 'HEARTBEAT')] += 1
        harness.pump = pump
        with patch.object(native.time, 'monotonic', side_effect=lambda: now[0] / 1e9), \
             patch.object(native.time, 'monotonic_ns', side_effect=lambda: now[0]), \
             patch.object(native.clock, 'monotonic', side_effect=AssertionError('model clock used for host window')):
            harness.observe_bench_window(.5)
        window = harness.summary['latency_diagnostic']['bench_window']
        self.assertEqual(phases, ['experiment_normal'])
        self.assertEqual(len(calls), 3)
        self.assertEqual(window['ended_host_monotonic_ns'] - window['started_host_monotonic_ns'], 500_000_000)
        self.assertEqual(window['status'], 'completed')
        self.assertEqual(window['message_counts'], {f'{c}:uav{i}': 3 for c in harness.channels for i in harness.uav_ids})

    def test_bench_stop_is_checked_before_existing_socket_pump(self):
        harness = native.NativeFiveUavHarness.__new__(native.NativeFiveUavHarness)
        harness.stop_file = self.root / 'stop.request'
        harness.stop_file.touch()
        with patch.object(town.FlightHarness, 'pump', side_effect=AssertionError('traffic after stop')):
            with self.assertRaisesRegex(native.BenchStopRequested, 'no flight or LAND'):
                harness.pump(0)

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


class NoBypassCounterTests(unittest.TestCase):
    """Counter eligibility only: no synthetic PHY isolation or device pass."""
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.stamp = time.monotonic_ns()
        self.watchdog = '/tmp/owned-native/ns3.ready.heartbeat'
        request = dict(controller='pixhawk_6c', radio_device='microhard_pmddl2450', uav_id=2,
            channels={channel: dict(endpoint=dict(kind='udp', bind=['127.0.0.1', 20001+index],
                peer=['127.0.0.1', 20101+index])) for index, channel in enumerate(('control', 'payload'))})
        self.bundle = resolve(request, json.loads(CATALOG.read_text()), native_uav_count=3)
        self.bundle['reference_profiles_sha256'] = hashlib.sha256(CATALOG.read_bytes()).hexdigest()
        for adapter in self.bundle['adapters'].values():
            adapter['radio_watchdog_file'] = self.watchdog
        self.bundle_path = self.root / 'external_endpoints/bundle.json'
        self.store(self.bundle_path, self.bundle)
        for i in (1, 3):
            for channel in ('control', 'payload'):
                self.store(self.root / f'metrics/{channel}_uart_uav{i}.json', self.sample(i, channel))
        for channel in ('control', 'payload'):
            self.store(self.root / f'external_endpoints/{channel}/metrics.json', self.sample(2, channel, external=True))

    def store(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def sample(self, uav, channel, external=False):
        value = dict(uav_id=uav, channel=channel, sampled_monotonic_ns=self.stamp, reassembly_pending_records=0)
        if external:
            value.update(controller_kind='udp', connected=True, to_device_queue_bytes=0, to_radio_queue_bytes=0,
                         radio_to_endpoint_bytes=25, endpoint_to_radio_bytes=100)
        else:
            value.update(uart_queue_bytes=0, radio_queue_bytes=0, uart_output_bytes=25, uart_input_bytes=100)
        return value

    def read(self, **kwargs):
        values = dict(after_ns=self.stamp, watchdog_file=self.watchdog)
        values.update(kwargs)
        return native._uart_byte_counters(self.root, (1, 2, 3), ('control', 'payload'), 'uart_output_bytes', **values)

    def test_exact_native_pair_and_other_stock_slots_have_identified_drained_counters(self):
        values = self.read()
        self.assertEqual(len(values), 6)
        self.assertTrue(all(value == 25 for value in values.values()))
        source = native._uart_byte_counters(self.root, (1, 2, 3), ('control', 'payload'), 'uart_input_bytes',
            after_ns=self.stamp, watchdog_file=self.watchdog)
        self.assertTrue(all(value == 100 for value in source.values()))

    def test_new_runtime_uses_its_frozen_profiles_instead_of_mutable_checkout(self):
        profiles = self.root / 'external_endpoints/reference-profiles.json'
        profiles.write_bytes(CATALOG.read_bytes())
        with patch('scripts.product.test_radio_hitl.CATALOG', self.root / 'missing-mutable-profiles.json'):
            self.assertEqual(len(self.read()), 6)
        profiles.write_bytes(profiles.read_bytes() + b'\n')
        self.assertIsNone(self.read())

    def test_missing_counter_or_either_queue_or_pending_cannot_default_to_zero(self):
        path = self.root / 'external_endpoints/payload/metrics.json'
        original = self.sample(2, 'payload', external=True)
        for field in original:
            changed = original.copy()
            del changed[field]
            self.store(path, changed)
            with self.subTest(field=field):
                self.assertIsNone(self.read())

    def test_both_external_queues_and_reassembly_must_be_drained(self):
        path = self.root / 'external_endpoints/control/metrics.json'
        for field in ('to_radio_queue_bytes', 'to_device_queue_bytes', 'reassembly_pending_records'):
            value = self.sample(2, 'control', external=True)
            value[field] = 1
            self.store(path, value)
            with self.subTest(field=field):
                self.assertIsNone(self.read())

    def test_stale_future_wrong_uav_or_channel_are_ineligible(self):
        path = self.root / 'external_endpoints/payload/metrics.json'
        for field, changed in (('sampled_monotonic_ns', self.stamp-1), ('sampled_monotonic_ns', 10**30),
                               ('uav_id', 1), ('uav_id', True), ('channel', 'control')):
            value = self.sample(2, 'payload', external=True)
            value[field] = changed
            self.store(path, value)
            with self.subTest(field=field, value=changed):
                self.assertIsNone(self.read())

    def test_wrong_native_bundle_watchdog_or_duplicate_stock_owner_cannot_qualify(self):
        self.assertIsNone(self.read(watchdog_file='/tmp/another-run/heartbeat'))
        altered = copy.deepcopy(self.bundle)
        altered['native_binding']['uav_id'] = 1
        self.store(self.bundle_path, altered)
        self.assertIsNone(self.read())
        self.store(self.bundle_path, self.bundle)
        self.store(self.root / 'metrics/control_uart_uav2.json', self.sample(2, 'control'))
        self.assertIsNone(self.read())

    def test_stock_counter_drain_fields_are_required_as_well(self):
        path = self.root / 'metrics/control_uart_uav1.json'
        for field in ('uart_queue_bytes', 'radio_queue_bytes', 'reassembly_pending_records'):
            value = self.sample(1, 'control')
            del value[field]
            self.store(path, value)
            with self.subTest(field=field):
                self.assertIsNone(self.read())

    def test_legacy_external_slot_requires_its_actual_config_and_current_drain_fields(self):
        root = self.root / 'legacy'
        (root / 'logs').mkdir(parents=True)
        config = dict(uav_id=1, channel='control', radio_namespace='ams-uav1', endpoint=dict(kind='udp'),
                      radio=dict(bind=['10.71.1.10', 14601], peer=['10.71.0.10', 14600]))
        (root / 'logs/external_endpoint.yaml').write_text(yaml.safe_dump(config))
        path = root / 'external_endpoint/metrics.json'
        value = self.sample(1, 'control', external=True)
        self.store(path, value)
        self.assertEqual(native._uart_byte_counters(root, (1,), ('control',), 'uart_output_bytes',
            after_ns=self.stamp), {'control:uav1': 25})
        del value['to_radio_queue_bytes']
        self.store(path, value)
        self.assertIsNone(native._uart_byte_counters(root, (1,), ('control',), 'uart_output_bytes', after_ns=self.stamp))


if __name__ == '__main__':
    unittest.main()
