import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import bas
from scripts.product.relay_experiment import RelayExperiment, StopRequested


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'scene.json').write_text('{}')
        self.config = json.loads((bas.ROOT / 'runtime.json').read_text())
        self.config['environment']['scene'] = 'scene.json'

    def load(self, config=None):
        path = self.root / 'runtime.json'
        path.write_text(json.dumps(self.config if config is None else config))
        return bas.load_config(path, self.root)

    def test_environment_cannot_activate_old_modes(self):
        env = bas.environment(self.load(), 'test', {
            'PATH': '/bin', 'BAS_NATIVE_EXTERNAL_CONFIG': 'old.json',
            'BAS_NATIVE_FAULT_PROFILE': 'selective', 'BAS_SIMULATION_MODE': 'lockstep',
            'GZ_PARTITION': 'other-run', 'ROS_DOMAIN_ID': '99'})
        self.assertNotIn('BAS_NATIVE_EXTERNAL_CONFIG', env)
        self.assertNotIn('GZ_PARTITION', env)
        self.assertNotIn('ROS_DOMAIN_ID', env)
        self.assertEqual(env['BAS_NATIVE_FAULT_PROFILE'], 'none')
        self.assertEqual(env['BAS_SIMULATION_MODE'], 'realtime')
        self.assertEqual(env['PATH'], '/bin')

    def test_unknown_option_and_unsupported_modes_are_rejected(self):
        for section, key, value in [('runtime', 'clok', 'realtime'),
                                    ('runtime', 'clock', 'lockstep'),
                                    ('propagation', 'model', 'sionna'),
                                    ('experiment', 'duration_s', float('nan')),
                                    ('experiment', 'seed', True)]:
            with self.subTest(key=key):
                config = copy.deepcopy(self.config)
                config[section][key] = value
                with self.assertRaises(ValueError):
                    self.load(config)

    def test_scene_must_stay_inside_workspace(self):
        self.config['environment']['scene'] = '../outside.json'
        with self.assertRaises(ValueError):
            self.load()

    def test_run_id_cannot_traverse_paths(self):
        for value in ('../other', '.', '/tmp/x', 'name/child', ''):
            with self.assertRaises(ValueError):
                bas.identifier(value)

    def test_events_preserve_command_and_clock_fields_across_logs(self):
        paths = [self.root / 'gcs.jsonl', self.root / 'uav.jsonl']
        rows = [dict(host_monotonic_ns=20, command_id='c', event='uart_admitted', source_sim_time_s=2.0),
                dict(host_monotonic_ns=10, command_id='c', event='command_created', source_sim_time_s=None)]
        for path, row in zip(paths, rows):
            path.write_text(json.dumps(row) + '\n')
        result = bas.events(paths, command_id='c')
        self.assertEqual([r['host_monotonic_ns'] for r in result], [10, 20])
        self.assertIsNone(result[0]['source_sim_time_s'])
        self.assertEqual(result[1]['event'], 'uart_admitted')
        self.assertEqual(len(bas.events(paths, command_id='c', limit=1)), 1)
        self.assertEqual(bas.events(paths, command_id='other'), [])

    def test_corrupt_complete_event_is_not_hidden(self):
        path = self.root / 'bad.jsonl'
        path.write_text('{broken}\n')
        with self.assertRaises(json.JSONDecodeError):
            bas.events([path])

    def test_stop_interrupts_work_before_new_commands(self):
        request = self.root / 'stop.request'
        request.touch()
        mission = object.__new__(RelayExperiment)
        mission.args = SimpleNamespace(stop_file=request)
        mission.gcs = Mock()
        with self.assertRaises(StopRequested):
            mission.pump()
        mission.gcs.step.assert_not_called()


if __name__ == '__main__':
    unittest.main()
