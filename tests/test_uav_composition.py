"""Composition/transport and onboard failsafe checks; no flight or hardware claims."""
import copy
import json
from pathlib import Path
import shutil
import signal
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import yaml

from network.coordination.policy import ProtocolError
from network.coordination.service import (AUTOPILOT_MODE_MAX_AGE_NS, Network, UAVService,
                                          build_parser, configured_uav_ids)
from scripts.product import prepare_urban_gazebo as generator
from scripts.product.urban_mission import UrbanScenario


class CompositionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.scene = json.loads((generator.ROOT / 'network/config/urban_low.json').read_text())
        target = self.root / 'network/config'
        target.mkdir(parents=True)
        for name in ('scenario_5uav_town01_native_product.yaml', 'town01_sitl.parm'):
            shutil.copy2(generator.ROOT / 'network/config' / name, target / name)

    def scene_for(self, count):
        data = copy.deepcopy(self.scene)
        ids = {str(i) for i in range(1, count + 1)}
        data['spawns'] = {i: p for i, p in data['spawns'].items() if i in ids}
        data['routes'] = {i: p for i, p in data['routes'].items() if i in ids}
        # A new scene declares its barriers explicitly, rather than pruning unknown references at runtime.
        data['mission']['barriers'] = {}
        return data

    def write_scene(self, data):
        path = self.root / 'scene.json'
        path.write_text(json.dumps(data))
        return path

    def test_each_roster_generates_only_declared_nodes_ports_and_missions(self):
        for count in range(1, 6):
            with self.subTest(count=count):
                path = self.write_scene(self.scene_for(count))
                loaded = UrbanScenario.load(path)
                self.assertEqual(loaded.uav_ids, tuple(range(1, count + 1)))
                output = self.root / f'generated-{count}'
                with patch.object(generator, 'ROOT', self.root):
                    generator.prepare(path, output, count)
                launch = yaml.safe_load((output / 'scenario.yaml').read_text())
                names = {f'uav{i}' for i in range(1, count + 1)}
                self.assertEqual({r['name'] for r in launch['robots']}, names)
                self.assertEqual(set(launch['flight']['missions']), names)
                self.assertEqual(set(launch['flight']['takeoff_relative_altitude_m']), names)
                self.assertEqual([r['instance'] for r in launch['robots']], list(range(count)))
                for key in ('system_id', 'dds_udp_port', 'master_tcp_port', 'sitl_udp_port', 'fdm_udp_port'):
                    self.assertEqual(len({r[key] for r in launch['robots']}), count)
                for capture in launch['evidence']['screenshots']:
                    self.assertLessEqual(set(capture['required_projected_uavs']), names)
                self.assertNotIn('\\', launch['scenario']['map']['world_file'])
                self.assertEqual(json.loads((output / 'geometry-source.json').read_text())['config'], loaded.raw)

    def test_unknown_or_incomplete_rosters_are_not_silently_sliced(self):
        examples = []
        data = self.scene_for(2)
        data['routes']['3'] = [[10, 0, 12]]
        examples.append(data)
        data = self.scene_for(2)
        data['spawns']['3'] = data['spawns'].pop('2')
        data['routes']['3'] = data['routes'].pop('2')
        examples.append(data)
        data = self.scene_for(1)
        data['spawns']['01'] = data['spawns'].pop('1')
        data['routes']['01'] = data['routes'].pop('1')
        examples.append(data)
        data = self.scene_for(1)
        data['mission']['barriers'] = {'1:1': {'minimum_reached': {'2': 0}}}
        examples.append(data)
        examples.append({'spawns': {}, 'routes': {}})
        for data in examples:
            with self.subTest(data=data), self.assertRaises(ValueError):
                UrbanScenario.load(self.write_scene(data))

    def test_expected_count_mismatch_fails_before_generating(self):
        output = self.root / 'not-generated'
        with self.assertRaisesRegex(ValueError, 'roster'):
            generator.prepare(self.write_scene(self.scene_for(2)), output, 1)
        self.assertFalse(output.exists())

    def test_service_parser_and_count_reject_invalid_counts(self):
        for value in (0, 6, True, 2.0, '2'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                configured_uav_ids(SimpleNamespace(uav_count=value))
        args = build_parser().parse_args(['gcs', '--run-id', 'test', '--event-log', 'unused.jsonl', '--uav-count', '2'])
        self.assertEqual(configured_uav_ids(args), (1, 2))
        self.assertEqual(configured_uav_ids(SimpleNamespace()), (1, 2, 3, 4, 5))

    def test_real_udp_probe_exchange_uses_each_configured_roster(self):
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        self.addCleanup(lambda: [signal.signal(sig, value) for sig, value in previous.items()])
        for count in range(1, 6):
            with self.subTest(count=count), patch('network.coordination.service.endpoint_ip',
                                                 side_effect=lambda i: f'127.0.0.{i + 1}'):
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reservation:
                    reservation.bind(('127.0.0.1', 0))
                    port = reservation.getsockname()[1]
                args = SimpleNamespace(uav_count=count, port=port, heartbeat_s=.2,
                                       run_id='composition', radio_watchdog_file=None, watchdog_s=.3)
                nodes = []
                try:
                    nodes = [Network(args, i, lambda *a, **kw: None) for i in range(count + 1)]
                    for node in nodes:
                        node.udp.settimeout(1)
                    nodes[0].periodic(1_000_000_000)
                    for node in nodes[1:]:
                        self.assertIsNone(node.receive())  # Actual UDP probe causes an actual UDP response.
                    received = {nodes[0].receive()[1] for _ in range(count)}
                    self.assertEqual(received, set(range(1, count + 1)))
                    self.assertEqual(set(nodes[0].peer_echo_ns), received)
                    with self.assertRaisesRegex(ProtocolError, 'roster'):
                        nodes[0].send('probe', {}, count + 1)
                finally:
                    for node in nodes:
                        node.close()


class LandingFailsafeTests(unittest.TestCase):
    def fixture(self, mode=None, age_ns=0, armed=True):
        node = UAVService.__new__(UAVService)
        node.index = 1
        node.latest_autopilot_mode = None if mode is None else dict(
            mode=mode, custom_mode=9 if mode == 'LAND' else 4, base_mode=128 if armed else 0,
            armed=armed, received_host_monotonic_ns=10_000_000_000 - age_ns)
        node.hold_suppressed_for_land = False
        node.hold_pending, node.last_hold_ns, node.hold_confirmed = None, 0, False
        node.mavutil = SimpleNamespace(mode_mapping_acm={4: 'GUIDED', 9: 'LAND', 17: 'BRAKE'},
                                       mavlink=SimpleNamespace(MAV_MODE_FLAG_CUSTOM_MODE_ENABLED=1,
                                                               MAV_MODE_FLAG_SAFETY_ARMED=128))
        node.mav = Mock(seq=0)
        node.mav.set_mode_encode.return_value.pack.return_value = b'BRAKE'
        node.events = []
        node.emit = lambda event, **fields: node.events.append((event, fields))
        node.admission = SimpleNamespace(queue=[])
        node.write_uart = Mock(return_value=5)
        return node

    def test_fresh_land_survives_lease_expiry_without_claiming_hold(self):
        node = self.fixture('LAND')
        with patch('network.coordination.service.time.monotonic_ns', return_value=10_000_000_000):
            node.request_hold()
            node.request_hold()
        self.assertIsNone(node.hold_pending)
        node.mav.set_mode_encode.assert_not_called()
        self.assertEqual([name for name, _ in node.events], ['hold_suppressed'])
        self.assertEqual(node.events[0][1]['reason'], 'observed_autonomous_landing')
        self.assertFalse(node.hold_confirmed)

    def test_land_observed_after_brake_queued_cancels_the_queued_override(self):
        node = self.fixture('LAND')
        node.hold_pending = b'BRAKE'
        with patch('network.coordination.service.time.monotonic_ns', return_value=10_000_000_000):
            node.write_hold()
        node.write_uart.assert_not_called()
        self.assertIsNone(node.hold_pending)

    def test_guided_unknown_stale_and_future_mode_keep_existing_hold(self):
        for mode, age in [('GUIDED', 0), (None, 0), ('LAND', AUTOPILOT_MODE_MAX_AGE_NS + 1), ('LAND', -1)]:
            with self.subTest(mode=mode, age=age):
                node = self.fixture(mode, age)
                with patch('network.coordination.service.time.monotonic_ns', return_value=10_000_000_000):
                    node.request_hold()
                    node.write_hold()
                node.write_uart.assert_called_once_with(b'BRAKE')
                self.assertEqual(node.events[-1][0], 'hold_uart_written')

    def test_actual_own_heartbeat_records_mode_armed_and_receipt_time(self):
        node = self.fixture()
        message = SimpleNamespace(get_type=lambda: 'HEARTBEAT', get_srcSystem=lambda: 1,
                                  get_srcComponent=lambda: 1, custom_mode=9, base_mode=128)
        foreign = SimpleNamespace(get_type=lambda: 'HEARTBEAT', get_srcSystem=lambda: 255,
                                  get_srcComponent=lambda: 190, custom_mode=0, base_mode=0)
        node.uart_parser = SimpleNamespace(parse_buffer=lambda raw: [message, foreign])
        node.mavutil.mode_string_v10 = lambda msg: 'LAND'
        node.gate = SimpleNamespace(active_grant=lambda: None)
        with patch('network.coordination.service.time.monotonic_ns', return_value=10_000_000_000):
            node.telemetry(b'actual uart parser fixture')
        self.assertTrue(node.latest_autopilot_mode['armed'])
        self.assertEqual(node.latest_autopilot_mode['received_host_monotonic_ns'], 10_000_000_000)
        self.assertEqual([event for event, _ in node.events].count('autopilot_mode_observed'), 1)


if __name__ == '__main__':
    unittest.main()
