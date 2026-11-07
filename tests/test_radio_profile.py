"""Explicit RT configuration and preparation; packet/flight qualification runs separately."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import yaml

import bas
from scripts.product import prepare_urban_gazebo as generator


class RadioProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.scene = json.loads((bas.ROOT / 'network/config/urban_low.json').read_text())
        self.scene['spawns'] = {'1': self.scene['spawns']['1']}
        self.scene['routes'] = {'1': self.scene['routes']['1']}
        self.scene['mission']['barriers'] = {}
        self.scene_file = self.root / 'scene.json'
        self.scene_file.write_text(json.dumps(self.scene))
        self.radio = yaml.safe_load((bas.ROOT / 'network/config/native_wifi_80211n_spectrum_product.yaml').read_text())
        self.radio['sionna'].update(samples_per_src=20000, max_num_paths_per_src=10000,
                                    diffraction=True, edge_diffraction=True)
        self.radio_file = self.root / 'radio.yaml'
        self.write_radio()
        self.config = json.loads((bas.ROOT / 'runtime.json').read_text())
        self.config['environment']['scene'] = 'scene.json'
        self.config['drone']['count'] = 1
        self.config['propagation'] = dict(model='sionna', radio_config='radio.yaml',
                                          ground_material='medium_dry_ground', building_material='concrete', thickness_m=.1)
        target = self.root / 'network/config'
        target.mkdir(parents=True)
        for name in ('scenario_5uav_town01_native_product.yaml', 'town01_sitl.parm'):
            shutil.copy2(bas.ROOT / 'network/config' / name, target / name)

    def write_radio(self):
        self.radio_file.write_text(yaml.safe_dump(self.radio))

    def load(self, config=None):
        path = self.root / 'runtime.json'
        path.write_text(json.dumps(self.config if config is None else config))
        return bas.load_config(path, self.root)

    def test_rt_profile_requires_every_explicit_argument_and_rejects_hybrid(self):
        for field in ('radio_config', 'ground_material', 'building_material', 'thickness_m'):
            config = copy.deepcopy(self.config)
            del config['propagation'][field]
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.load(config)
        self.config['propagation'] = {'model': 'hybrid'}
        with self.assertRaisesRegex(ValueError, 'not qualified'):
            self.load()

    def test_all_runtime_arguments_are_forwarded_from_selected_profile(self):
        config = self.load()
        env = bas.environment(config, 'rt-test', inherited={'BAS_NATIVE_PROPAGATION_PROFILE': 'friis'})
        self.assertEqual(env['BAS_NATIVE_PROPAGATION_PROFILE'], 'sionna')
        self.assertEqual(env['BAS_NATIVE_RADIO_CONFIG'], 'radio.yaml')
        self.assertEqual(env['BAS_NATIVE_RT_GROUND_MATERIAL'], 'medium_dry_ground')
        self.assertEqual(env['BAS_NATIVE_RT_BUILDING_MATERIAL'], 'concrete')
        self.assertEqual(env['BAS_NATIVE_RT_THICKNESS_M'], '0.1')
        self.assertEqual(env['BAS_NATIVE_UART_CHANNELS'], 'control,payload')
        self.assertEqual(env['BAS_NATIVE_URBAN_OUTPUT'], 'runs/control/rt-test/generated')

    def test_receiver_batch_is_explicit_optional_and_rejects_incompatible_arrays(self):
        self.radio['sionna'].pop('receiver_batch_mode', None)
        self.write_radio()
        parsed = generator.read_rt_radio_profile(self.radio_file, self.scene)
        self.assertEqual(parsed['sionna']['receiver_batch_mode'], 'pairwise')
        self.radio['sionna']['receiver_batch_mode'] = 'receiver_batch'
        self.write_radio()
        self.assertEqual(generator.read_rt_radio_profile(self.radio_file, self.scene)['sionna']['receiver_batch_mode'], 'receiver_batch')
        self.radio['sionna']['synthetic_array'] = False
        self.write_radio()
        with self.assertRaisesRegex(ValueError, 'synthetic'):
            generator.read_rt_radio_profile(self.radio_file, self.scene)

    def test_execution_mode_defaults_are_resolved_without_changing_input(self):
        self.radio['sionna'].pop('execution_mode', None)
        self.radio['sionna'].pop('worker_count', None)
        self.write_radio()
        source = self.radio_file.read_bytes()
        parsed = generator.read_rt_radio_profile(self.radio_file, self.scene)
        self.assertEqual(parsed['sionna']['execution_mode'], 'in_process')
        self.assertEqual(parsed['sionna']['worker_count'], 3)
        self.assertEqual(self.radio_file.read_bytes(), source)

    def test_execution_mode_and_worker_count_reject_invalid_public_values(self):
        for field, bad_values in [('execution_mode', ['', 'threads', True, 1, None, [], {}]),
                                  ('worker_count', [True, False, 0, 4, -1, 1.0, '2', None])]:
            for invalid in bad_values:
                self.radio_file.write_text(yaml.safe_dump(self.radio | {
                    'sionna': self.radio['sionna'] | {field: invalid}}))
                with self.subTest(field=field, invalid=invalid), self.assertRaisesRegex(ValueError, field):
                    generator.read_rt_radio_profile(self.radio_file, self.scene)
        for mode in (None, 'in_process'):
            for count in (1, 2):
                rt = self.radio['sionna'] | {'worker_count': count}
                if mode is not None:
                    rt['execution_mode'] = mode
                self.radio_file.write_text(yaml.safe_dump(self.radio | {'sionna': rt}))
                with self.subTest(mode=mode, count=count), self.assertRaisesRegex(ValueError, 'worker_count'):
                    generator.read_rt_radio_profile(self.radio_file, self.scene)

    def test_worker_pool_requires_the_verified_batch_cache_contract(self):
        pool = self.radio['sionna'] | dict(execution_mode='worker_pool', receiver_batch_mode='receiver_batch',
                                          cache_expiry_jitter_fraction=0, max_number_of_paths=90)
        for count in (None, 1, 2, 3):
            rt = pool if count is None else pool | {'worker_count': count}
            self.radio_file.write_text(yaml.safe_dump(self.radio | {'sionna': rt}))
            parsed = generator.read_rt_radio_profile(self.radio_file, self.scene)
            self.assertEqual(parsed['sionna']['execution_mode'], 'worker_pool')
            self.assertEqual(parsed['sionna']['worker_count'], 3 if count is None else count)
        incompatible = [('receiver_batch_mode', 'pairwise'), ('synthetic_array', False),
                        ('max_number_of_paths', 91), ('cache_expiry_jitter_fraction', .5),
                        ('channel_state_max_age_s', .500001), ('endpoint_displacement_threshold_m', .500001)]
        for field, invalid in incompatible:
            self.radio_file.write_text(yaml.safe_dump(self.radio | {'sionna': pool | {field: invalid}}))
            with self.subTest(field=field), self.assertRaises(ValueError):
                generator.read_rt_radio_profile(self.radio_file, self.scene)
        # Existing in-process policy has no new 90-path/zero-jitter restriction.
        self.radio_file.write_text(yaml.safe_dump(self.radio | {
            'sionna': self.radio['sionna'] | {'max_number_of_paths': 91}}))
        self.assertEqual(generator.read_rt_radio_profile(self.radio_file, self.scene)['sionna']['max_number_of_paths'], 91)

    def test_worker_pool_preparation_freezes_the_resolved_radio_selection(self):
        self.radio['sionna'].update(execution_mode='worker_pool', worker_count=2,
                                    receiver_batch_mode='receiver_batch', cache_expiry_jitter_fraction=0)
        self.write_radio()
        source = self.radio_file.read_bytes()
        output = self.root / 'worker-run'
        with patch.object(generator, 'ROOT', self.root):
            generator.prepare(self.scene_file, output, 1, propagation_profile='sionna', radio_config=self.radio_file,
                              ground_material='medium_dry_ground', building_material='concrete', thickness_m=.1)
        scenario = yaml.safe_load((output / 'scenario.yaml').read_text())
        resolved_path = self.root / scenario['radio']['config']
        self.assertEqual(resolved_path, output / 'radio-resolved.yaml')
        resolved = yaml.safe_load(resolved_path.read_text())
        self.assertEqual(resolved['sionna']['execution_mode'], 'worker_pool')
        self.assertEqual(resolved['sionna']['worker_count'], 2)
        self.assertEqual(resolved['coupling'], self.radio['coupling'])
        self.assertEqual(resolved['sionna']['channel_state_max_age_s'], self.radio['sionna']['channel_state_max_age_s'])
        self.assertEqual(resolved['sionna']['endpoint_displacement_threshold_m'], self.radio['sionna']['endpoint_displacement_threshold_m'])
        self.assertEqual(self.radio_file.read_bytes(), source)

    def test_worker_pool_with_sources_is_explicitly_unqualified_before_source_loading(self):
        self.radio['sionna'].update(execution_mode='worker_pool', receiver_batch_mode='receiver_batch',
                                    cache_expiry_jitter_fraction=0)
        self.write_radio()
        self.config['interference'] = {'sources': 'not-opened-unqualified-sources.yaml'}
        with self.assertRaisesRegex(ValueError, 'worker_pool with directed sources is not yet qualified'):
            self.load()

    def test_snapshot_and_generator_consume_the_frozen_radio_and_geometry(self):
        config = self.load()
        env = bas.environment(config, 'rt-test')
        folder = self.root / 'runs/control/rt-test'
        folder.mkdir(parents=True)
        original_radio = self.radio_file.read_bytes()
        with patch.object(bas, 'ROOT', self.root):
            hashes = bas.snapshot_inputs(config, folder, env)
        self.radio['radio']['tx_power_w'] = .5
        self.write_radio()
        self.assertEqual((self.root / env['BAS_NATIVE_RADIO_CONFIG']).read_bytes(), original_radio)
        self.assertEqual(hashes['radio_sha256'], hashlib.sha256(original_radio).hexdigest())
        output = self.root / env['BAS_NATIVE_URBAN_OUTPUT']
        with patch.object(generator, 'ROOT', self.root):
            generator.prepare(self.root / env['BAS_NATIVE_URBAN_CONFIG'], output, 1,
                              propagation_profile='sionna', radio_config=self.root / env['BAS_NATIVE_RADIO_CONFIG'],
                              ground_material=env['BAS_NATIVE_RT_GROUND_MATERIAL'],
                              building_material=env['BAS_NATIVE_RT_BUILDING_MATERIAL'], thickness_m=.1)
        scenario = yaml.safe_load((output / 'scenario.yaml').read_text())
        resolved_radio_path = self.root / scenario['radio']['config']
        self.assertEqual(resolved_radio_path, output / 'radio-resolved.yaml')
        resolved_radio = yaml.safe_load(resolved_radio_path.read_text())
        self.assertEqual(resolved_radio['sionna']['execution_mode'], 'in_process')
        self.assertEqual(resolved_radio['sionna']['worker_count'], 3)
        self.assertEqual(resolved_radio['radio']['tx_power_w'], yaml.safe_load(original_radio)['radio']['tx_power_w'])
        self.assertEqual((self.root / env['BAS_NATIVE_RADIO_CONFIG']).read_bytes(), original_radio)
        scene = self.root / scenario['scenario']['map']['scene_xml']
        self.assertTrue(scene.is_file())
        self.assertEqual(scene, output / 'rt/scene.xml')
        manifest = json.loads((scene.parent / 'scene-manifest.json').read_text())
        self.assertEqual(manifest['source_json_sha256'], hashes['scene_sha256'])

    def test_scene_carrier_and_actual_native_channel_must_agree(self):
        for field, number in [('carrier_hz', 2437e6), ('channel_number', 6)]:
            radio = copy.deepcopy(self.radio)
            radio['radio'][field] = number
            self.radio_file.write_text(yaml.safe_dump(radio))
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'disagree'):
                self.load()

    def test_bad_budgets_flags_and_unknown_fields_cannot_be_ignored(self):
        for field, value in [('samples_per_src', 0), ('max_num_paths_per_src', '10000'),
                             ('diffraction', 'true'), ('max_depth', True), ('invented', 10)]:
            radio = copy.deepcopy(self.radio)
            radio['sionna'][field] = value
            self.radio_file.write_text(yaml.safe_dump(radio))
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.load()

    def test_radio_path_materials_and_thickness_are_checked(self):
        for field, value in [('radio_config', '../outside.yaml'), ('ground_material', 'invented'),
                             ('building_material', 'floorboard'), ('thickness_m', 0), ('thickness_m', float('inf'))]:
            config = copy.deepcopy(self.config)
            config['propagation'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.load(config)

    def test_radio_duplicate_and_malformed_yaml_are_rejected(self):
        self.radio_file.write_text(self.radio_file.read_text() + '\nschema_version: 1\n')
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            self.load()
        self.radio_file.write_text('radio: [broken')
        with self.assertRaisesRegex(ValueError, 'invalid radio profile YAML'):
            self.load()

    def test_geometry_profile_remains_compatible_and_does_not_enable_rt(self):
        self.config['propagation'] = {'model': 'geometry_logdistance'}
        config = self.load()
        env = bas.environment(config, 'geometry')
        self.assertEqual(env['BAS_NATIVE_PROPAGATION_PROFILE'], 'geometry_logdistance')
        self.assertNotIn('BAS_NATIVE_RADIO_CONFIG', env)
        with patch.object(generator, 'ROOT', self.root):
            generator.prepare(self.scene_file, self.root / 'geometry', 1)
        scenario = yaml.safe_load((self.root / 'geometry/scenario.yaml').read_text())
        self.assertEqual(scenario['scenario']['map']['scene_xml'], 'not_used_no_rt.xml')


if __name__ == '__main__':
    unittest.main()
