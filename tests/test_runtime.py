import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import bas
from scripts.product.relay_experiment import RelayExperiment, ScenarioError, StopRequested


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'scene.json').write_bytes((bas.ROOT / 'network/config/urban_low.json').read_bytes())
        self.config = json.loads((bas.ROOT / 'runtime.json').read_text())
        self.config['environment']['scene'] = 'scene.json'

    def load(self, config=...):
        path = self.root / 'runtime.json'
        path.write_text(json.dumps(self.config if config is ... else config))
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

    def test_diagnostic_log_level_is_explicit_and_cannot_inject_nslog_components(self):
        resolved = self.load()
        self.assertEqual(resolved['runtime']['sionna_log_level'], 'info')
        self.assertEqual(bas.environment(resolved, 'log-test')['BAS_NATIVE_SIONNA_LOG_LEVEL'], 'info')
        self.config['runtime']['sionna_log_level'] = 'debug'
        self.assertEqual(self.load()['runtime']['sionna_log_level'], 'debug')
        self.config['runtime']['sionna_log_level'] = 'debug:OtherComponent=all'
        with self.assertRaises(ValueError):
            self.load()

    def test_event_logging_default_enum_environment_and_input_snapshot(self):
        resolved = self.load()
        self.assertEqual(resolved['runtime']['event_logging'], 'metrics_only')
        self.assertEqual(bas.environment(resolved, 'logging', {'BAS_NATIVE_EVENT_LOGGING': 'batched_trace'})
                         ['BAS_NATIVE_EVENT_LOGGING'], 'metrics_only')
        self.config['runtime']['event_logging'] = 'batched_trace'
        resolved = self.load()
        env = bas.environment(resolved, 'logging', {'BAS_NATIVE_EVENT_LOGGING': 'metrics_only'})
        self.assertEqual(env['BAS_NATIVE_EVENT_LOGGING'], 'batched_trace')
        folder = self.root / 'runs/control/logging'
        folder.mkdir(parents=True)
        with patch.object(bas, 'ROOT', self.root):
            bas.snapshot_inputs(resolved, folder, env)
        self.assertEqual(json.loads((folder / 'config.json').read_text())['runtime']['event_logging'], 'batched_trace')
        for value in ('trace', '', None, True, 0, [], {}):
            self.config['runtime']['event_logging'] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'event_logging'):
                self.load()

    def test_invalid_json_shapes_and_field_types_are_rejected(self):
        for config in (None, [], [dict(self.config)], 'runtime', 5, True):
            with self.subTest(config=config), self.assertRaises(ValueError):
                self.load(config)
        for value in (None, [], 'engine', 5, True):
            config = copy.deepcopy(self.config)
            config['environment'] = value
            with self.subTest(section=value), self.assertRaises(ValueError):
                self.load(config)
        for section, key, value in [('drone', 'count', 5.0),
                                    ('runtime', 'image', None),
                                    ('runtime', 'image', 42),
                                    ('environment', 'scene', None),
                                    ('environment', 'scene', ['scene.json']),
                                    ('environment', 'scene', ''),
                                    ('experiment', 'duration_s', True),
                                    ('experiment', 'duration_s', 10 ** 1000),
                                    ('runtime', 'land_timeout_s', float('inf'))]:
            with self.subTest(section=section, key=key, value=value):
                config = copy.deepcopy(self.config)
                config[section][key] = value
                with self.assertRaises(ValueError):
                    self.load(config)

    def test_duplicate_json_field_is_not_silently_overridden(self):
        path = self.root / 'runtime.json'
        text = json.dumps(self.config).replace('"duration_s": 240', '"duration_s": 1, "duration_s": 240')
        self.assertIn('"duration_s": 1, "duration_s": 240', text)
        path.write_text(text)
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            bas.load_config(path, self.root)

    def test_scene_path_is_normalized_for_linux_snapshot(self):
        (self.root / 'maps').mkdir()
        (self.root / 'maps/scene.json').write_bytes((self.root / 'scene.json').read_bytes())
        self.config['environment']['scene'] = r'maps\scene.json'
        self.assertEqual(self.load()['environment']['scene'], 'maps/scene.json')

    def test_inspect_cli_runs_without_linux_runtime(self):
        result = subprocess.run([sys.executable, str(bas.ROOT / 'bas.py'), 'inspect',
                                 '--run-id', 'inspect-test'], cwd=self.root, text=True,
                                capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output['config']['drone']['count'], 5)
        self.assertEqual(output['config']['environment']['scene'], 'network/config/urban_low.json')
        self.assertEqual(output['environment']['BAS_NATIVE_UAV_COUNT'], '5')

    def test_cli_invalid_config_has_actionable_error_without_traceback(self):
        path = self.root / 'runtime.json'
        path.write_text('null')
        result = subprocess.run([sys.executable, str(bas.ROOT / 'bas.py'), 'inspect',
                                 '--config', str(path)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('BAS:', result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    @unittest.skipIf(sys.platform == 'linux', 'non-Linux runtime error only')
    def test_run_cli_explains_linux_requirement(self):
        result = subprocess.run([sys.executable, str(bas.ROOT / 'bas.py'), 'run',
                                 '--run-id', 'unsupported-host-test'], cwd=self.root,
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('requires Linux', result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertFalse((self.root / 'runs').exists())

    def test_scene_must_stay_inside_workspace(self):
        self.config['environment']['scene'] = '../outside.json'
        with self.assertRaises(ValueError):
            self.load()

    def test_run_id_cannot_traverse_paths(self):
        for value in ('../other', '.', '/tmp/x', 'name/child', ''):
            with self.assertRaises(ValueError):
                bas.identifier(value)

    def test_count_requires_an_exact_scene_roster(self):
        self.config['drone']['count'] = 2
        with self.assertRaisesRegex(ValueError, 'roster'):
            self.load()

    def test_missing_or_failed_native_process_is_never_success(self):
        raw = self.root / 'raw'
        (raw / 'metrics').mkdir(parents=True)
        summary = dict(runtime_status='completed', mission_outcome='succeeded', measurement_validity='valid')
        self.assertEqual(bas.recorded_outcome(raw, 0, summary)['runtime_status'], 'failed')
        (raw / 'metrics/native_radio_stats.json').write_text(json.dumps(dict(stop_reason='state_coupling_deadline')))
        result = bas.recorded_outcome(raw, 0, summary)
        self.assertEqual(result['runtime_status'], 'failed')
        self.assertEqual(result['measurement_validity'], 'invalid')

    def test_mission_failure_cannot_hide_capture_failure(self):
        raw = self.root / 'raw'
        for directory in ('metrics', 'logs', 'configuration/final'):
            (raw / directory).mkdir(parents=True)
        (raw / 'metrics/native_radio_stats.json').write_text(json.dumps(dict(stop_reason='sionna_ns3_process_stopped')))
        (raw / 'configuration/final/manifest.json').write_text('{}')
        storage = raw / 'logs/native_capture_storage.json'
        summary = dict(runtime_status='completed', mission_outcome='failed', measurement_validity='valid', error='landing not observed')
        storage.write_text(json.dumps(dict(publication=dict(status='copied', errors=[]))))
        self.assertEqual(bas.recorded_outcome(raw, 1, summary)['runtime_status'], 'completed')
        storage.write_text(json.dumps(dict(publication=dict(status='copy_failed', errors=['disk full']))))
        result = bas.recorded_outcome(raw, 1, summary)
        self.assertEqual(result['runtime_status'], 'failed')
        self.assertEqual(result['measurement_validity'], 'invalid')

    def qualified_source_run(self):
        from scripts.product.prepare_native_sources import materialize, prepare
        from scripts.product.summarize_native_radio_five_uav import summarize_native_sources
        raw = self.root / 'source-raw'
        for directory in ('metrics', 'logs', 'configuration/final'):
            (raw / directory).mkdir(parents=True)
        (raw / 'metrics/native_radio_stats.json').write_text(json.dumps(dict(stop_reason='duration')))
        (raw / 'configuration/final/manifest.json').write_text('{}')
        (raw / 'logs/native_capture_storage.json').write_text(json.dumps(dict(
            publication=dict(status='copied', errors=[]))))
        source = dict(id='outside_window', position_m=[0, -40, 10], orientation_rad=[0, 0],
            pattern='iso', gain_dbi=0, power_w=.01, center_hz=2412000000, bandwidth_hz=20000000,
            start_s=1000, stop_s=1010, period_s=.01, duty_cycle=1, mode='continuous', spectral_shape='rectangular')
        configured = prepare(dict(time_reference='measurement_start', sources=[source]))
        (raw / 'logs/native_sources.json').write_text(json.dumps(configured))
        actual = materialize(configured, 100_000_000_000, 1000, 'experiment_normal')
        (raw / 'logs/native_sources_actual.json').write_text(json.dumps(actual))
        summarize_native_sources(raw, [])
        summary = dict(runtime_status='completed', mission_outcome='succeeded', measurement_validity='valid')
        return raw, summary

    def test_missing_source_export_cannot_be_reported_as_valid_completed_run(self):
        raw, summary = self.qualified_source_run()
        (raw / 'metrics/native_source_summary.json').unlink()
        result = bas.recorded_outcome(raw, 0, summary)
        self.assertEqual(result['runtime_status'], 'failed')
        self.assertEqual(result['measurement_validity'], 'invalid')
        self.assertIn('native_source_summary_missing', result['runtime_verification_errors'])

    def test_anchored_out_of_window_source_needs_no_switches_or_effect(self):
        raw, summary = self.qualified_source_run()
        result = bas.recorded_outcome(raw, 0, summary)
        self.assertEqual(result['runtime_status'], 'completed')
        self.assertEqual(result['measurement_validity'], 'valid')
        self.assertEqual(result['runtime_verification_errors'], [])

    def test_missing_actual_corrupt_stale_or_invalid_source_summary_is_rejected(self):
        raw, summary = self.qualified_source_run()
        actual_path = raw / 'logs/native_sources_actual.json'
        actual_bytes = actual_path.read_bytes()
        actual_path.unlink()
        result = bas.recorded_outcome(raw, 0, summary)
        self.assertEqual(result['measurement_validity'], 'invalid')
        actual_path.write_bytes(actual_bytes)
        path = raw / 'metrics/native_source_summary.json'
        original = json.loads(path.read_text())
        for value in (None, [], '{broken}', dict(original, actual_schedule_status='invalid'),
                      dict(original, configured_sources={}), dict(original, actual_sources={})):
            with self.subTest(value=value):
                path.write_text(value if isinstance(value, str) else json.dumps(value))
                result = bas.recorded_outcome(raw, 0, summary)
                self.assertEqual(result['runtime_status'], 'failed')
                self.assertIn('native_source_export_invalid', result['runtime_verification_errors'])

    def test_configured_or_runner_declared_source_input_cannot_disappear(self):
        raw, summary = self.qualified_source_run()
        (raw / 'logs/native_sources.json').unlink()
        result = bas.recorded_outcome(raw, 0, summary, source_configured=True)
        self.assertIn('native_source_input_missing', result['runtime_verification_errors'])
        (raw / 'environment.txt').write_text('native_sources=runs/control/test/sources.yaml\n')
        result = bas.recorded_outcome(raw, 0, summary)
        self.assertIn('native_source_input_missing', result['runtime_verification_errors'])

    def test_source_export_guard_does_not_expand_failed_or_unmeasured_runs(self):
        raw, summary = self.qualified_source_run()
        (raw / 'metrics/native_source_summary.json').unlink()
        for changes in (dict(runtime_status='failed'), dict(measurement_validity='not_evaluated')):
            with self.subTest(changes=changes):
                result = bas.recorded_outcome(raw, 0, dict(summary, **changes), source_configured=True)
                self.assertEqual(result['runtime_verification_errors'], [])

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

    def stopped_mission(self, landing_error=None):
        mission = object.__new__(RelayExperiment)
        mission.args = SimpleNamespace(parameter_snapshots=False, fault_profile='none', run_dir=self.root)
        (self.root / 'metrics').mkdir()
        mission.summary = {'uavs': {str(i): dict(armed=False, takeoff=False, landed=False, auto_disarm=False)
                                   for i in range(1, 6)}}
        mission.prepare = Mock(side_effect=StopRequested)
        mission.measure = Mock()
        mission.land = Mock(side_effect=landing_error)
        mission.source_s = 0
        mission.observer = SimpleNamespace(completed={})
        mission.urban = None
        mission.phase = Mock()
        mission.emit = Mock()
        mission.csv_handle = Mock()
        mission.gcs = Mock()
        return mission

    def test_requested_stop_lands_and_is_not_a_completed_experiment(self):
        mission = self.stopped_mission()
        result = mission.run()
        mission.land.assert_called_once()
        mission.measure.assert_not_called()
        self.assertEqual(result['status'], 'stopped')
        self.assertFalse(result['criteria']['five_uav_lifecycle'])

    def test_landing_failure_remains_failure_after_stop(self):
        mission = self.stopped_mission(ScenarioError('no disarm observed'))
        with self.assertRaises(ScenarioError):
            mission.run()
        result = json.loads((self.root / 'metrics/relay_experiment_summary.json').read_text())
        self.assertEqual(result['status'], 'failed')
        self.assertIn('no disarm observed', result['error'])


if __name__ == '__main__':
    unittest.main()
