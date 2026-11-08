import copy
import hashlib
import json
import math
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

    def test_payload_load_is_normalized_frozen_and_forwarded_from_one_public_profile(self):
        self.assertIsNone(self.load()['flows']['payload_load'])
        self.config['runtime']['event_logging'] = 'batched_trace'
        self.config['flows']['payload_load'] = {'class': '10kbps', 'uav_ids': [1]}
        resolved = self.load()
        profile = resolved['flows']['payload_load']
        self.assertEqual(profile, {'class': '10kbps', 'uav_ids': [1], 'direction': 'gcs_to_uav',
                                  'useful_bytes': 64, 'useful_bitrate_bps': 10000})
        env = bas.environment(resolved, 'load-profile')
        self.assertEqual(json.loads(env['BAS_NATIVE_PAYLOAD_LOAD_JSON']), profile)
        folder = self.root / 'snapshot'; folder.mkdir()
        with patch.object(bas, 'ROOT', self.root):
            bas.snapshot_inputs(resolved, folder, env)
        self.assertEqual(json.loads((folder / 'config.json').read_text())['flows']['payload_load'], profile)
        from scripts.product.operator_ui import UIState
        self.assertEqual(UIState(self.root).validate({'config': self.config})['flows']['payload_load'], profile)
        cli = copy.deepcopy(resolved)
        cli['environment']['scene'] = 'network/config/urban_low.json'
        path = self.root / 'cli-load.json'; path.write_text(json.dumps(cli))
        inspected = subprocess.run([sys.executable, str(bas.ROOT/'bas.py'), 'inspect', '--config', str(path)],
                                   text=True, capture_output=True)
        self.assertEqual(inspected.returncode, 0, inspected.stderr)
        view = json.loads(inspected.stdout)
        self.assertEqual(view['config']['flows']['payload_load'], profile)
        self.assertEqual(json.loads(view['environment']['BAS_NATIVE_PAYLOAD_LOAD_JSON']), profile)

    def test_payload_load_rejects_incompatible_roster_direction_types_and_untraced_runs(self):
        self.config['runtime']['event_logging'] = 'batched_trace'
        for value in ({}, [], True, {'class': []}, {'class': '10kbps', 'extra': 1},
                      {'class': '10kbps', 'uav_ids': [True]}, {'class': '10kbps', 'uav_ids': [6]},
                      {'class': '10kbps', 'uav_ids': [1, 1]}, {'class': '10kbps', 'direction': 'uav_to_gcs'},
                      {'class': '10kbps', 'useful_bytes': True}, {'class': '10kbps', 'useful_bytes': 129},
                      {'class': '10kbps', 'useful_bitrate_bps': 10001}, {'class': '20mbps_plus', 'useful_bitrate_bps': '20000000'}):
            self.config['flows']['payload_load'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.load()
        self.config['flows']['payload_load'] = {'class': '1kbps'}
        self.config['runtime']['event_logging'] = 'metrics_only'
        with self.assertRaisesRegex(ValueError, 'batched_trace'):
            self.load()
        self.config['runtime']['event_logging'] = 'batched_trace'
        self.config['flows']['channels'] = ['control']
        with self.assertRaisesRegex(ValueError, 'payload channel'):
            self.load()

    def test_underproduced_payload_load_invalidates_measurement_without_inventing_a_flight_failure(self):
        from scripts.product.relay_experiment import outcome_fields
        summary = dict(experiment_profile='urban', measurement_start_source_sim_time_s=1.,
            criteria=dict(all_uav_lifecycle=True, urban_routes_telemetry_completed=True, live_integral_rtf_target=True),
            payload_channel=dict(offered_load=dict(configured=True, generator_eligible=False)))
        result = outcome_fields(summary, window_complete=True, stopped=False)
        self.assertEqual((result['runtime_status'], result['mission_outcome'], result['measurement_validity']),
                         ('completed', 'succeeded', 'invalid'))
        self.assertIn('payload_load_generator_incomplete', result['measurement_validity_reasons'])

    def test_duration_bounds_and_explicit_long_trace_storage_profile(self):
        self.config['runtime']['capture_storage'] = 'disk'
        for duration in (1e-9, 28800, 86400):
            self.config['experiment']['duration_s'] = duration
            self.assertEqual(self.load()['experiment']['duration_s'], duration)
        for duration in (0, 1e-10, 86400.1, float('inf'), float('nan'), True):
            self.config['experiment']['duration_s'] = duration
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                self.load()
        self.config['experiment']['duration_s'] = 28800
        for mode in ('metrics_only', 'batched_trace'):
            self.config['runtime']['event_logging'] = mode
            self.config['runtime'].pop('capture_storage', None)
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, 'requires.*disk'):
                self.load()
            self.config['runtime']['capture_storage'] = 'disk'
            self.assertEqual(self.load()['runtime']['capture_storage'], 'disk')
        resolved = self.load()
        self.assertEqual(bas.environment(resolved, 'long', {'BAS_NATIVE_CAPTURE_STORAGE': 'tmpfs'})
                         ['BAS_NATIVE_CAPTURE_STORAGE'], 'disk')
        for value in ('ram', '', None, True, [], {}):
            self.config['runtime']['capture_storage'] = value
            with self.subTest(storage=value), self.assertRaisesRegex(ValueError, 'capture_storage'):
                self.load()

    def test_capture_storage_default_and_selection_survive_configuration_snapshot(self):
        self.assertEqual(self.load()['runtime']['capture_storage'], 'tmpfs')
        self.config['runtime']['capture_storage'] = 'disk'
        resolved = self.load()
        folder = self.root / 'snapshot'
        folder.mkdir()
        env = bas.environment(resolved, 'disk')
        with patch.object(bas, 'ROOT', self.root):
            bas.snapshot_inputs(resolved, folder, env)
        self.assertEqual(json.loads((folder / 'config.json').read_text())['runtime']['capture_storage'], 'disk')

    def test_runtime_lifetimes_cover_measurement_and_existing_phases_without_relaxing_guards(self):
        cap = bas.runtime_duration_limits(28800, 120, 5, ['control', 'payload'])
        self.assertEqual(cap['native_duration_s'], 33437)
        self.assertEqual(cap['service_duration_s'], 34802)
        self.assertGreater(cap['service_duration_s'], cap['native_duration_s'])
        self.assertEqual(cap['native_budgets']['landing_s'], 120)
        self.assertEqual(cap['native_budgets']['arm_per_uav_s'], 120)
        self.assertEqual(cap['native_budgets']['native_readiness_s'], 180)
        with_params = bas.runtime_duration_limits(28800, 120, 5, ['control', 'payload'], parameter_snapshots=True)
        self.assertEqual(with_params['native_duration_s'] - cap['native_duration_s'], 1206)
        maximum = bas.runtime_duration_limits(86400, 120, 5, ['control', 'payload'])
        self.assertGreater(maximum['native_duration_s'], math.ceil(86400 * 1.1 + 10 + 120))
        for mode in ('latency_diagnostic', 'flight_diagnostic', 'product'):
            legacy = bas.runtime_duration_limits(86400, 120, 5, ['control', 'payload'], mode=mode)
            self.assertEqual((legacy['native_duration_s'], legacy['service_duration_s']), (2400, 3600))

    def test_runner_budget_python_is_executable_and_bound_to_actual_child_arguments(self):
        script = (bas.ROOT / 'network/ns3/run_native_radio_five_uav.sh').read_text(encoding='utf-8')
        block = script.split("<<'PYRUNTIMEBUDGET'\n", 1)[1].split('\nPYRUNTIMEBUDGET', 1)[0]
        result = subprocess.run([sys.executable, '-X', 'utf8', '-', str(bas.ROOT), 'relay_experiment',
            '28800', '120', '5', 'control,payload', '0'], input=block, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        native, service, raw = result.stdout.splitlines()
        budget = json.loads(raw)
        self.assertEqual((int(native), int(service)), (budget['native_duration_s'], budget['service_duration_s']))
        self.assertIn('--duration="$NATIVE_DURATION_S"', script)
        self.assertIn('--duration-s "$SERVICE_DURATION_S"', script)
        self.assertIn('runtime_duration_budget=%s', script)
        # Bash source retains the exact bounded readiness and clean-stop loops.
        self.assertIn('for _ in $(seq 1 1800); do', script)
        self.assertIn('for _ in $(seq 1 1200); do', script)
        profile = script.split("<<'PYDURATIONPROFILE'\n", 1)[1].split('\nPYDURATIONPROFILE', 1)[0]
        for duration, mode, storage, allowed in (('28800', 'batched_trace', 'disk', True),
                ('28800', 'metrics_only', 'disk', True), ('28800', 'metrics_only', 'tmpfs', False),
                ('28800', 'batched_trace', 'tmpfs', False), ('86401', 'metrics_only', 'disk', False)):
            checked = subprocess.run([sys.executable, '-X', 'utf8', '-', str(bas.ROOT), duration, mode, storage],
                                     input=profile, text=True, capture_output=True)
            with self.subTest(duration=duration, storage=storage):
                self.assertEqual(checked.returncode == 0, allowed)

    def test_inspect_duration_override_cannot_bypass_limit_or_long_capture_storage(self):
        for duration in ('86400.1', '0.0000000001'):
            result = subprocess.run([sys.executable, str(bas.ROOT / 'bas.py'), 'inspect', '--duration', duration],
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn('86400', result.stderr)
        long_default = subprocess.run([sys.executable, str(bas.ROOT / 'bas.py'), 'inspect', '--duration', '28800'],
                                      text=True, capture_output=True)
        self.assertEqual(long_default.returncode, 2)
        self.assertIn('capture_storage=disk', long_default.stderr)
        explicit = json.loads((bas.ROOT / 'runtime.json').read_text())
        explicit['runtime']['capture_storage'] = 'disk'
        path = self.root / 'disk-cli.json'
        for mode in ('metrics_only', 'batched_trace'):
            explicit['runtime']['event_logging'] = mode
            path.write_text(json.dumps(explicit))
            result = subprocess.run([sys.executable, str(bas.ROOT / 'bas.py'), 'inspect',
                '--config', str(path), '--duration', '28800'], text=True, capture_output=True)
            with self.subTest(mode=mode):
                self.assertEqual(result.returncode, 0, result.stderr)
                output = json.loads(result.stdout)
                self.assertEqual(output['config']['experiment']['duration_s'], 28800)
                self.assertEqual(output['environment']['BAS_NATIVE_CAPTURE_STORAGE'], 'disk')
                self.assertEqual(output['environment']['BAS_NATIVE_EVENT_LOGGING'], mode)

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

    def position_journal_evidence(self):
        """Real shared-writer bytes and ledger; no Gazebo or RF claim."""
        from network.jsonl import AsyncJsonlWriter
        raw, summary = self.qualified_source_run()
        path = raw / 'logs/node_state.jsonl'
        state = dict(type='node_state', schema_version=2, journal_mode='bounded_async_jsonl_v1',
                     source='gazebo_odometry', session_id='position-session', nodes=[],
                     published_monotonic_ns=123456789, source_sim_time_s=1.25, label='позиция')
        writer = AsyncJsonlWriter(path)
        try:
            for stamp in (123456700, 123456789):
                writer.write(json.dumps(dict(state, published_monotonic_ns=stamp),
                                        ensure_ascii=False, allow_nan=False, separators=(',', ':')))
        finally:
            writer.close()
        audit = dict(writer.snapshot(), schema=1, journal_mode=state['journal_mode'], source=state['source'],
                     session_id=state['session_id'], status='closed', producer_error=None, close_error=None)
        (raw / 'logs/node_state.json').write_text(json.dumps(state), encoding='utf-8')
        audit_path = raw / 'logs/node_state.jsonl.writer.json'
        audit_path.write_text(json.dumps(audit), encoding='utf-8')
        return raw, summary, path, audit_path, audit

    def test_actual_closed_position_writer_is_verified_without_changing_pose_epochs(self):
        raw, summary, path, audit_path, audit = self.position_journal_evidence()
        original = (raw / 'logs/node_state.json').read_bytes(), path.read_bytes(), audit_path.read_bytes()
        result = bas.recorded_outcome(raw, 0, summary)
        self.assertEqual(result['runtime_status'], 'completed')
        self.assertEqual(result['measurement_validity'], 'valid')
        proof = result['node_state_journal_verification']
        self.assertEqual(proof['status'], 'verified_closed_position_journal')
        self.assertTrue(proof['passed'])
        self.assertEqual((proof['rows'], proof['bytes'], proof['sha256']),
                         (2, len(original[1]), hashlib.sha256(original[1]).hexdigest()))
        self.assertEqual(original, ((raw / 'logs/node_state.json').read_bytes(), path.read_bytes(), audit_path.read_bytes()))
        self.assertNotEqual(audit['producer']['native_thread_id'], audit['writer']['native_thread_id'])

    def test_old_position_journal_is_explicitly_unrequested_not_retroactively_verified(self):
        raw, summary = self.qualified_source_run()
        (raw / 'logs/node_state.json').write_text(json.dumps(dict(source='gazebo_odometry', session_id='old')))
        (raw / 'logs/node_state.jsonl').write_bytes(b'old archive with an incomplete tail')
        result = bas.recorded_outcome(raw, 0, summary)
        self.assertEqual(result['runtime_status'], 'completed')
        self.assertEqual(result['node_state_journal_verification'], dict(status='not_requested_legacy', passed=None))

    def test_runner_advertisement_cannot_downgrade_missing_exports_to_legacy(self):
        raw, summary = self.qualified_source_run()
        environment = raw / 'environment.txt'
        environment.write_text('position_journal_mode=bounded_async_jsonl_v1\n')
        result = bas.recorded_outcome(raw, 0, summary)
        self.assertEqual(result['runtime_status'], 'failed')
        self.assertEqual(result['measurement_validity'], 'invalid')
        self.assertIn('node_state_journal_invalid', result['runtime_verification_errors'])
        (raw / 'logs/node_state.json').write_text(json.dumps(dict(source='gazebo_odometry', session_id='old')))
        self.assertFalse(bas.recorded_outcome(raw, 0, summary)['node_state_journal_verification']['passed'])

    def test_unknown_runner_journal_mode_is_rejected(self):
        raw, summary = self.qualified_source_run()
        environment = raw / 'environment.txt'
        environment.write_text('position_journal_mode=misspelled\n')
        result = bas.recorded_outcome(raw, 0, summary)
        self.assertFalse(result['node_state_journal_verification']['passed'])
        self.assertIn('unsupported advertised', result['node_state_journal_verification']['error'])

    def test_advertised_position_journal_requires_its_final_audit_and_actual_file(self):
        raw, summary, path, audit_path, audit = self.position_journal_evidence()
        for missing in (path, audit_path, raw / 'logs/node_state.json'):
            original = missing.read_bytes()
            missing.unlink()
            with self.subTest(missing=missing.name):
                result = bas.recorded_outcome(raw, 0, summary)
                self.assertEqual((result['runtime_status'], result['measurement_validity']), ('failed', 'invalid'))
                self.assertIn('node_state_journal_invalid', result['runtime_verification_errors'])
            missing.write_bytes(original)

    def test_recording_failed_or_undrained_position_writer_cannot_pass(self):
        raw, summary, path, audit_path, original = self.position_journal_evidence()
        changes = [dict(status='recording'), dict(status='failed'), dict(closed=False), dict(drained=False),
            dict(listener_alive=True), dict(queue_depth=1), dict(error_count=1), dict(failed=1), dict(rejected=1),
            dict(producer_error='snapshot failed'), dict(close_error='drain timed out'),
            dict(first_exception={'message': 'write failed'}), dict(last_exception={'message': 'flush failed'}),
            dict(unwritten_records=['missing']), dict(in_progress_record='writing'), dict(rejected_record='full'),
            dict(accepted=3), dict(written=True), dict(accepted_bytes=True), dict(written_bytes=1),
            dict(schema=True), dict(journal_mode='other'), dict(source='ros_odometry'), dict(session_id='other'),
            dict(capacity=True), dict(capacity=513), dict(max_queue=513), dict(max_record_bytes=1)]
        for change in changes:
            audit_path.write_text(json.dumps(dict(original, **change)), encoding='utf-8')
            with self.subTest(change=change):
                result = bas.recorded_outcome(raw, 0, summary)
                self.assertEqual((result['runtime_status'], result['measurement_validity']), ('failed', 'invalid'))
                self.assertFalse(result['node_state_journal_verification']['passed'])
                self.assertEqual(result['mission_outcome'], 'succeeded')

    def test_invalid_position_audit_or_conflicting_live_mode_cannot_be_legacy(self):
        raw, summary, path, audit_path, audit = self.position_journal_evidence()
        for value in ('null', '[]', '{broken}', '{"schema":1,"schema":1}', '{"value":NaN}'):
            audit_path.write_text(value, encoding='utf-8')
            with self.subTest(audit=value):
                self.assertFalse(bas.recorded_outcome(raw, 0, summary)['node_state_journal_verification']['passed'])
        audit_path.write_text(json.dumps(audit), encoding='utf-8')
        state_path = raw / 'logs/node_state.json'
        original = json.loads(state_path.read_text(encoding='utf-8'))
        for mode in (None, 'synchronous', 'absent'):
            changed = dict(original, journal_mode=mode)
            if mode == 'absent':
                del changed['journal_mode']
            state_path.write_text(json.dumps(changed), encoding='utf-8')
            with self.subTest(mode=mode):
                self.assertFalse(bas.recorded_outcome(raw, 0, summary)['node_state_journal_verification']['passed'])

    def test_failed_position_journal_still_invalidates_an_already_failed_runtime(self):
        raw, summary, path, audit_path, audit = self.position_journal_evidence()
        audit_path.unlink()
        summary.update(runtime_status='failed', measurement_validity='not_evaluated', error='runtime stopped early')
        result = bas.recorded_outcome(raw, 1, summary)
        self.assertEqual((result['runtime_status'], result['measurement_validity']), ('failed', 'invalid'))
        self.assertIn('node_state_journal_invalid', result['runtime_verification_errors'])

    def test_position_journal_tamper_or_missing_tail_is_rejected_against_writer_ledger(self):
        raw, summary, path, audit_path, audit = self.position_journal_evidence()
        original = path.read_bytes()
        altered = original.replace(b'123456789', b'123456788', 1)
        self.assertEqual(len(altered), len(original))
        for candidate in (altered, original[:-1], original.splitlines(keepends=True)[0], original + original):
            path.write_bytes(candidate)
            with self.subTest(bytes=len(candidate), digest=hashlib.sha256(candidate).hexdigest()):
                result = bas.recorded_outcome(raw, 0, summary)
                self.assertEqual(result['measurement_validity'], 'invalid')
                self.assertIn('node_state_journal_invalid', result['runtime_verification_errors'])

    def test_position_writer_identity_and_portable_artifact_path(self):
        raw, summary, path, audit_path, original = self.position_journal_evidence()
        for reported in (f'/workspace/runs/{raw.name}/logs/node_state.jsonl',
                         f'C:\\relocated\\{raw.name}\\logs\\node_state.jsonl'):
            audit_path.write_text(json.dumps(dict(original, path=reported)), encoding='utf-8')
            with self.subTest(reported=reported):
                self.assertEqual(bas.recorded_outcome(raw, 0, summary)['runtime_status'], 'completed')
        changes = [dict(path='/other/run/logs/node_state.jsonl'),
                   dict(path=f'/prefix/../{raw.name}/logs/node_state.jsonl'),
                   dict(writer=dict(original['writer'], pid=original['producer']['pid'] + 1)),
                   dict(writer=dict(original['writer'], native_thread_id=original['producer']['native_thread_id']))]
        for change in changes:
            audit_path.write_text(json.dumps(dict(original, **change)), encoding='utf-8')
            with self.subTest(change=change):
                self.assertEqual(bas.recorded_outcome(raw, 0, summary)['measurement_validity'], 'invalid')

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
        (raw / 'logs/native_radio_events.csv').write_text(
            'time_s,wall_monotonic_ns,phase,event,node,peer,bytes,x,y,z,value,details\n')
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

    def test_declared_or_observed_rt_workers_require_the_closed_run_evidence(self):
        raw, summary = self.qualified_source_run()
        stats_path = raw / 'metrics/native_radio_stats.json'
        original = json.loads(stats_path.read_text())
        for declaration in ('native_stats', 'runner_environment', 'worker_config'):
            with self.subTest(declaration=declaration):
                stats_path.write_text(json.dumps(dict(original, **(
                    {'sionna_rt_execution_mode': 'worker_pool'} if declaration == 'native_stats' else {}))))
                (raw / 'environment.txt').write_text(
                    'sionna_rt_execution_mode=worker_pool\n' if declaration == 'runner_environment' else '')
                worker_config = raw / 'logs/native_rt_worker_config.json'
                if declaration == 'worker_config':
                    worker_config.write_text('{}')
                result = bas.recorded_outcome(raw, 0, summary)
                self.assertEqual(result['runtime_status'], 'failed')
                self.assertEqual(result['measurement_validity'], 'invalid')
                self.assertIn('native_rt_worker_export_invalid', result['runtime_verification_errors'])
                worker_config.unlink(missing_ok=True)

    def test_source_export_before_capture_publication_cannot_qualify_later_capture(self):
        from scripts.product.summarize_native_radio_five_uav import summarize_native_sources
        raw, summary = self.qualified_source_run()
        capture = raw / 'logs/native_radio_events.csv'
        original = capture.read_bytes()
        capture.unlink()
        summarize_native_sources(raw, [])  # Actual old runner order: no published capture yet.
        capture.write_bytes(original)
        result = bas.recorded_outcome(raw, 0, summary)
        self.assertEqual(result['runtime_status'], 'failed')
        self.assertEqual(result['measurement_validity'], 'invalid')
        self.assertIn('native_source_export_invalid', result['runtime_verification_errors'])

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
