"""Real localhost HTTP tests; injected controllers do not qualify a flight or HitL."""
import copy
import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

import bas
from scripts.product.operator_ui import BODY_LIMIT, UIState, make_server


class Child:
    pid = 4242
    returncode = None

    def poll(self):
        return self.returncode


class OperatorUITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = json.loads((bas.ROOT / 'runtime.json').read_text())
        scene_name = self.config['environment']['scene']
        scene = self.root / scene_name
        scene.parent.mkdir(parents=True)
        scene.write_bytes((bas.ROOT / scene_name).read_bytes())
        self.scene = json.loads(scene.read_text(encoding='utf-8'))
        bas.write_json(self.root / 'runtime.json', self.config)
        self.config = bas.load_config(self.root / 'runtime.json', root=self.root)
        self.launches, self.stops = [], []
        self.child = Child()
        self.state = UIState(self.root, launcher=self.launch, stopper=self.stops.append)
        self.server = make_server(0, state=self.state)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def launch(self, argv, folder):
        self.launches.append((argv, folder))
        return self.child

    def request(self, path, payload=None, *, method=None, headers=None, body=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        self.addCleanup(connection.close)
        values = {}
        if payload is not None:
            body = json.dumps(payload).encode()
        if body is not None:
            values.update({'Origin': f'http://127.0.0.1:{self.server.server_port}',
                           'X-BAS-Token': self.state.token, 'Content-Type': 'application/json'})
        values.update(headers or {})
        connection.request(method or ('POST' if body is not None else 'GET'), path, body, values)
        response = connection.getresponse()
        raw = response.read()
        result = json.loads(raw) if response.getheader('Content-Type', '').startswith('application/json') else raw
        return response.status, result, dict(response.getheaders())

    def payload(self, **changes):
        return dict(config=copy.deepcopy(self.config), run_id='ui-test', **changes)

    def test_hitl_configuration_uses_shared_loader_without_opening_an_endpoint(self):
        profiles = self.root / 'network/config/hitl_reference_devices.json'
        profiles.write_bytes((bas.ROOT / 'network/config/hitl_reference_devices.json').read_bytes())
        request = dict(controller='pixhawk_6c', radio_device='microhard_p2400', uav_id=3,
                       channels={c: dict(endpoint=dict(kind='serial', device=f'/dev/ttyUSB{i}', baud=115200))
                                 for i, c in enumerate(('control', 'payload'))})
        bas.write_json(self.root / 'request.json', request)
        payload = self.payload()
        payload['config'].update(hitl=dict(mode='radio_bench', request='request.json'))
        payload['config']['experiment']['policy'] = 'off'
        code, data, _ = self.request('/api/validate', payload)
        self.assertEqual(code, 200, data)
        self.assertEqual(data['config']['hitl']['mode'], 'radio_bench')
        self.assertEqual(data['config']['hitl']['request'], 'request.json')
        self.assertEqual(data['config']['hitl']['request_sha256'], hashlib.sha256((self.root / 'request.json').read_bytes()).hexdigest())
        self.assertEqual(self.launches, [])
        payload['config']['experiment']['policy'] = 'failover'
        self.assertEqual(self.request('/api/validate', payload)[0], 400)
        self.assertEqual(self.launches, [])

    def test_loopback_bootstrap_and_offline_utf8_page(self):
        self.assertEqual(self.server.server_address[0], '127.0.0.1')
        code, data, _ = self.request('/api/bootstrap')
        self.assertEqual(code, 200)
        self.assertEqual(data['config'], self.config)
        self.assertEqual(data['scene'], self.scene)
        code, page, headers = self.request('/')
        self.assertEqual(code, 200)
        text = page.decode('utf-8')
        self.assertIn('Сценарий и параметры', text)
        self.assertNotIn('__TOKEN__', text)
        self.assertIn(f'nonce="{self.state.token}"', text)
        self.assertIn(f"'nonce-{self.state.token}'", headers['Content-Security-Policy'])
        self.assertNotIn('https://', text)

    def test_cross_origin_missing_token_and_rebinding_hosts_are_rejected(self):
        for headers in ({'X-BAS-Token': ''}, {'Origin': 'http://example.com'}, {'Host': 'example.com'}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request('/api/validate', self.payload(), headers=headers)[0], 403)
        self.assertEqual(self.request('/api/bootstrap', headers={'Host': 'localhost'})[0], 403)
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        self.addCleanup(connection.close)
        connection.putrequest('GET', '/api/bootstrap', skip_host=True)
        host = f'127.0.0.1:{self.server.server_port}'
        connection.putheader('Host', host)
        connection.putheader('Host', host)
        connection.endheaders()
        response = connection.getresponse()
        self.assertEqual(response.status, 403)
        response.read()

    def test_body_limits_framing_and_json_errors(self):
        self.assertEqual(self.request('/api/validate', body=b'{}', headers={'Content-Length': str(BODY_LIMIT + 1)})[0], 413)
        self.assertEqual(self.request('/api/validate', body=b'{}', headers={'Content-Type': 'text/plain'})[0], 415)
        self.assertEqual(self.request('/api/validate', body=b'{}', headers={'Transfer-Encoding': 'chunked'})[0], 400)
        for body in (b'{', b'{"config": {}, "config": {}}', b'{"config": NaN}', b'[]'):
            with self.subTest(body=body):
                self.assertEqual(self.request('/api/validate', body=body)[0], 400)
        self.assertEqual(self.request('/api/start', body=b'[]')[0], 400)
        self.assertEqual(self.request('/api/validate', body=b'[' * 1200 + b']' * 1200)[0], 400)
        self.assertEqual(self.launches, [])

    def test_paths_cannot_read_outside_workspace_or_serve_source_files(self):
        for path in ('../runtime.json', '/runtime.json', 'C:/runtime.json', '.private.json'):
            with self.subTest(path=path):
                query = urlencode({'path': path})
                self.assertEqual(self.request('/api/config?' + query)[0], 400)
        self.assertEqual(self.request('/artifact?' + urlencode({'path': 'runtime.json'}))[0], 400)
        self.assertEqual(self.request('/api/config?path=missing.json')[0], 404)
        self.assertEqual(self.request('/api/status?run_id=../bad')[0], 400)
        self.assertEqual(self.request('/api/status?run_id=unknown')[0], 404)

    def test_real_config_and_scene_validation_without_launch(self):
        payload = self.payload(scene=copy.deepcopy(self.scene))
        code, result, _ = self.request('/api/validate', payload)
        self.assertEqual(code, 200)
        self.assertTrue(result['valid'])
        self.assertEqual(result['config'], self.config)
        self.assertEqual(list((self.root / 'runs/control/ui-inputs').iterdir()), [])
        payload['config']['drone']['count'] = 2
        self.assertEqual(self.request('/api/validate', payload)[0], 400)
        payload = self.payload()
        payload['config']['runtime']['invented'] = True
        self.assertEqual(self.request('/api/validate', payload)[0], 400)
        payload = self.payload(scene=self.scene)
        payload['config']['environment']['scene'] = '../outside.json'
        self.assertEqual(self.request('/api/validate', payload)[0], 400)
        self.assertEqual(self.launches, [])

    def test_interference_parameter_is_validated_and_saved_through_the_real_http_api(self):
        (self.root / 'radio.yaml').write_bytes((bas.ROOT / 'network/config/native_wifi_80211n_spectrum_product.yaml').read_bytes())
        source = dict(id='ui_source', position_m=[0, -40, 10], orientation_rad=[0, 0], pattern='iso',
            gain_dbi=0, power_w=.01, center_hz=2412000000, bandwidth_hz=20000000,
            start_s=10, stop_s=20, period_s=.01, duty_cycle=1, mode='continuous', spectral_shape='rectangular')
        (self.root / 'sources.yaml').write_text(json.dumps(dict(sources=[source])))
        payload = self.payload()
        payload['config']['propagation'] = dict(model='sionna', radio_config='radio.yaml',
            ground_material='medium_dry_ground', building_material='concrete', thickness_m=.2)
        payload['config']['interference'] = dict(sources='sources.yaml')
        code, result, _ = self.request('/api/validate', payload)
        self.assertEqual(code, 200)
        self.assertEqual(result['config']['interference']['sources'], 'sources.yaml')
        self.assertEqual(self.request('/api/start', payload)[0], 202)
        saved = bas.load_config(self.launches[0][1] / 'config.json', root=self.root)
        self.assertEqual(saved['interference']['sources'], 'sources.yaml')

    def test_event_logging_is_resolved_validated_and_saved_through_http(self):
        code, loaded, _ = self.request('/api/bootstrap')
        self.assertEqual(code, 200)
        self.assertEqual(loaded['config']['runtime']['event_logging'], 'metrics_only')
        payload = self.payload()
        payload['config']['runtime']['event_logging'] = 'batched_trace'
        code, result, _ = self.request('/api/validate', payload)
        self.assertEqual(code, 200)
        self.assertEqual(result['config']['runtime']['event_logging'], 'batched_trace')
        self.assertEqual(self.request('/api/start', payload)[0], 202)
        saved = bas.load_config(self.launches[0][1] / 'config.json', root=self.root)
        self.assertEqual(saved['runtime']['event_logging'], 'batched_trace')
        self.assertEqual(bas.environment(saved, 'ui-test')['BAS_NATIVE_EVENT_LOGGING'], 'batched_trace')
        payload['config']['runtime']['event_logging'] = 'full'
        self.assertEqual(self.request('/api/validate', payload)[0], 400)

    def test_long_duration_and_capture_storage_use_real_http_validation_and_frozen_config(self):
        payload = self.payload()
        self.assertEqual(payload['config']['runtime']['capture_storage'], 'tmpfs')
        payload['config']['experiment']['duration_s'] = 28800
        self.assertEqual(self.request('/api/validate', payload)[0], 400)  # metrics_only also needs disk.
        payload['config']['runtime']['event_logging'] = 'batched_trace'
        self.assertEqual(self.request('/api/validate', payload)[0], 400)
        payload['config']['runtime']['capture_storage'] = 'disk'
        code, result, _ = self.request('/api/validate', payload)
        self.assertEqual(code, 200)
        self.assertEqual(result['config']['experiment']['duration_s'], 28800)
        self.assertEqual(self.request('/api/start', payload)[0], 202)
        saved = bas.load_config(self.launches[0][1] / 'config.json', root=self.root)
        self.assertEqual(saved['runtime']['capture_storage'], 'disk')
        self.assertEqual(saved['experiment']['duration_s'], 28800)
        payload['config']['experiment']['duration_s'] = 86401
        self.assertEqual(self.request('/api/validate', payload)[0], 400)

    def test_start_freezes_scene_and_config_and_uses_existing_controller(self):
        original = (self.root / self.config['environment']['scene']).read_bytes()
        code, result, _ = self.request('/api/start', self.payload(scene=self.scene))
        self.assertEqual(code, 202)
        self.assertEqual(result['launcher_pid'], self.child.pid)
        self.assertEqual(len(self.launches), 1)
        argv, folder = self.launches[0]
        self.assertEqual(argv[1:3], [str(self.root / 'bas.py'), 'run'])
        self.assertEqual(argv[-2:], ['--run-id', 'ui-test'])
        saved = bas.load_config(folder / 'config.json', root=self.root)
        self.assertEqual(saved['environment']['scene'], 'runs/control/ui-inputs/ui-test/scene.json')
        self.assertEqual(json.loads((folder / 'scene.json').read_text()), self.scene)
        self.assertEqual((self.root / self.config['environment']['scene']).read_bytes(), original)
        self.assertEqual(self.request('/api/start', self.payload())[0], 400)
        self.child.returncode = 1
        self.assertEqual(self.request('/api/start', self.payload())[0], 400)
        self.assertEqual(len(self.launches), 1)

    def test_status_reads_actual_outcome_log_tail_and_artifacts(self):
        self.request('/api/start', self.payload())
        folder = self.root / 'runs/control/ui-test'
        folder.mkdir()
        record = dict(status='failed', runtime_status='failed', mission_outcome='succeeded',
                      measurement_validity='invalid', returncode=1)
        bas.write_json(folder / 'run.json', record)
        (folder / 'runtime.log').write_bytes(b'x' * 70000 + b'\nlatest line')
        raw = self.root / 'runs/native-radio-realtime/ui-test'
        (raw / 'metrics').mkdir(parents=True)
        bas.write_json(raw / 'metrics/relay_experiment_summary.json', {'status': 'failed', 'fixture': True})
        bas.write_json(raw / 'metrics/native_radio_stats.json', dict(stop_reason='runtime_error', channel_state_max_age_s=.05,
            pose_application=dict(state_max_age_ns=100_000_000),
            sionna_rt_workers=dict(error=None, cache=dict(miss_reasons=dict(host_sample_expired=1)),
                source_bindings=[dict(cache=dict(miss_reasons=dict(endpoint_displaced=2)))])))
        self.child.returncode = 1
        code, result, _ = self.request('/api/status?run_id=ui-test')
        self.assertEqual(code, 200)
        self.assertEqual(result['controller'], record)
        self.assertEqual(result['launcher_returncode'], 1)
        self.assertEqual(result['native_diagnostics'], dict(status='observed', stop_reason='runtime_error',
            rt_error=None, cache_miss_reasons=dict(host_sample_expired=1, endpoint_displaced=2), state_max_age_ms=100., channel_max_age_ms=50.))
        self.assertEqual(len(result['log_tail']), 65536)
        self.assertTrue(result['log_tail'].endswith('latest line'))
        artifact = next(row for row in result['artifacts'] if row['path'].endswith('/run.json'))
        code, content, headers = self.request('/artifact?' + urlencode({'path': artifact['path']}))
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(content), record)
        self.assertIn('attachment;', headers['Content-Disposition'])
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')

    def test_native_diagnostics_missing_or_incomplete_is_unavailable_without_invented_values(self):
        self.request('/api/start', self.payload())
        self.assertIsNone(self.request('/api/status?run_id=ui-test')[1]['native_diagnostics'])
        path = self.root / 'runs/native-radio-realtime/ui-test/metrics/native_radio_stats.json'
        path.parent.mkdir(parents=True)
        for content in ('{', '[]', json.dumps(dict(sionna_rt_workers=dict(cache=dict(miss_reasons=dict(host_sample_expired=True)))))):
            with self.subTest(content=content):
                path.write_text(content)
                code, result, _ = self.request('/api/status?run_id=ui-test')
                self.assertEqual(code, 200)
                self.assertEqual(result['native_diagnostics']['status'], 'unavailable')
                self.assertNotIn('state_max_age_ms', result['native_diagnostics'])

    def test_stop_uses_controller_request_and_rejects_invalid_identifier(self):
        self.assertEqual(self.request('/api/stop', {'run_id': 'ui-test'})[0], 202)
        self.assertEqual(self.stops, ['ui-test'])
        self.assertEqual(self.request('/api/stop', {'run_id': '../../bad'})[0], 400)
        self.assertEqual(self.request('/api/stop', {'run_id': 'ui-test', 'pid': 42})[0], 400)
        self.assertEqual(self.stops, ['ui-test'])

    def test_failed_bootstrap_attempt_remains_visible_after_ui_restart(self):
        self.request('/api/start', self.payload())
        self.state.children.clear()  # A UI restart loses child handles, but not saved inputs/logs.
        code, result, _ = self.request('/api/runs')
        self.assertEqual(code, 200)
        self.assertIn('ui-test', result['run_ids'])
        code, result, _ = self.request('/api/status?run_id=ui-test')
        self.assertEqual(code, 200)
        self.assertIsNone(result['controller'])
        self.assertIsNone(result['launcher_returncode'])

    def test_process_creation_failure_preserves_input_and_failure_reason(self):
        def unavailable(argv, folder):
            raise OSError('runtime executable unavailable')
        self.state.launcher = unavailable
        code, result, _ = self.request('/api/start', self.payload())
        self.assertEqual(code, 400)
        self.assertIn('runtime executable unavailable', result['error'])
        code, result, _ = self.request('/api/status?run_id=ui-test')
        self.assertEqual(code, 200)
        self.assertIn('runtime executable unavailable', result['log_tail'])
        self.assertIsNone(result['controller'])
        self.assertEqual(result['launcher_state'], 'not_observed')
        self.assertTrue((self.root / 'runs/control/ui-inputs/ui-test/config.json').is_file())

    def test_windows_validation_is_available_but_real_launch_is_blocked(self):
        self.state.launcher = None
        with patch('scripts.product.operator_ui.sys.platform', 'win32'):
            self.assertEqual(self.request('/api/validate', self.payload())[0], 200)
            code, result, _ = self.request('/api/start', self.payload())
        self.assertEqual(code, 400)
        self.assertIn('Linux', result['error'])
        self.assertEqual(self.launches, [])


if __name__ == '__main__':
    unittest.main()
