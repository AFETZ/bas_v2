"""Pure RF arithmetic/config checks; real Sionna/GPU qualification is separate."""
import copy
import json
import math
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

import bas
from scripts.product import compute_radio_maps as maps
from scripts.product import prepare_urban_gazebo as urban
from scripts.product.prepare_native_sources import prepare as prepare_sources


class RadioMapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        raw = json.loads((bas.ROOT / 'network/config/urban_low.json').read_text())
        raw['spawns'] = {'1': raw['spawns']['1']}
        raw['routes'] = {'1': raw['routes']['1']}
        raw['mission']['barriers'] = {}
        (self.root / 'scene.json').write_text(json.dumps(raw))
        target = self.root / 'network/config'
        target.mkdir(parents=True)
        for name in ('scenario_5uav_town01_native_product.yaml', 'town01_sitl.parm', 'native_wifi_80211n_spectrum_product.yaml'):
            shutil.copy2(bas.ROOT / 'network/config' / name, target / name)
        self.radio = 'network/config/native_wifi_80211n_spectrum_product.yaml'
        with patch.object(urban, 'ROOT', self.root):
            urban.prepare(self.root / 'scene.json', self.root / 'prepared', 1,
                propagation_profile='sionna', radio_config=self.root / self.radio,
                ground_material='medium_dry_ground', building_material='concrete', thickness_m=.1)
        self.config = dict(schema_version=1, mission_scene='prepared/mission-scene.json',
            scene_xml='prepared/rt/scene.xml', radio_config=self.radio, sources=None,
            desired=dict(id='cp', position_m=[0, 0, 2], orientation_rad=[0, 0], pattern='iso',
                         gain_dbi=0, center_hz=2412000000, bandwidth_hz=20000000, power_w=.01),
            receiver=dict(center_hz=2412000000, bandwidth_hz=20000000, temperature_k=290, noise_figure_db=7),
            map=dict(method='point_grid', center_m=[20, 0, 10], size_m=[20, 20], cell_size_m=[10, 10],
                     time_s=15, direction='gcs_to_air_isotropic_probe', samples_per_tx=20000, point_cells=[[0, 0]]))

    def test_existing_prepared_scene_and_strict_profile_validate_without_sionna(self):
        paths, mission, profile, sources = maps.validate(self.config, self.root)
        self.assertEqual(paths['scene_xml'], self.root / 'prepared/rt/scene.xml')
        self.assertEqual(mission.uav_ids, (1,))
        self.assertEqual(profile['radio']['carrier_hz'], 2412000000)
        self.assertEqual(sources, [])

    def test_unknown_parameters_nonfinite_noise_and_invalid_grid_fail(self):
        cases = [('receiver', 'noise_figure_db', -1), ('receiver', 'temperature_k', float('nan')),
                 ('receiver', 'noise_figure_db', True), ('receiver', 'noise_figure_db', 1e300),
                 ('desired', 'gain_dbi', 1e300), ('map', 'method', 'invented'),
                 ('map', 'size_m', [21, 20]), ('map', 'samples_per_tx', True),
                 ('map', 'point_cells', [[2, 0]]), ('desired', 'invented_gain', 2)]
        for section, field, value in cases:
            raw = copy.deepcopy(self.config)
            raw[section][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                maps.validate(raw, self.root)

    def test_point_grid_has_explicit_software_limit(self):
        self.config['map'].update(size_m=[170, 170])
        with self.assertRaisesRegex(ValueError, 'software limit'):
            maps.validate(self.config, self.root)
        self.config['map']['method'] = 'radio_map'
        maps.validate(self.config, self.root)

    def test_world_cell_centers_have_correct_orientation_and_height(self):
        np.testing.assert_equal(maps.grid_centers(self.config['map']),
            [[[15, -5, 10], [25, -5, 10]], [[15, 5, 10], [25, 5, 10]]])

    def test_flat_psd_overlap_full_half_outside_and_bandwidth_normalization(self):
        tx, rx = self.config['desired'], self.config['receiver']
        self.assertEqual(maps.overlap_fraction(tx, rx), 1)
        self.assertEqual(maps.overlap_fraction(dict(tx, center_hz=tx['center_hz']+10e6), rx), .5)
        self.assertEqual(maps.overlap_fraction(dict(tx, center_hz=tx['center_hz']+20e6), rx), 0)
        self.assertEqual(maps.overlap_fraction(dict(tx, bandwidth_hz=40e6), rx), .5)

    def test_resolved_native_sources_reuse_and_instantaneous_pulse_boundaries(self):
        source = dict(self.config['desired'], id='jam', mode='pulsed', spectral_shape='rectangular',
                      start_s=10, stop_s=20, period_s=.01, duty_cycle=.25)
        resolved = prepare_sources({'sources': [source]})
        (self.root / 'sources.json').write_text(json.dumps(resolved))
        self.config['sources'] = 'sources.json'
        rows = maps.validate(self.config, self.root)[3]
        segment = rows[0]
        self.assertEqual(segment['power_w'], source['power_w'])
        for when, expected in [(9.99, False), (10, True), (10.002499999, True),
                               (10.0025, False), (10.01, True), (20, False)]:
            with self.subTest(when=when):
                self.assertEqual(maps.active_at(segment, when), expected)

    def test_power_sums_are_linear_and_mc_zero_is_masked_not_outage(self):
        result = maps.combine([[1., 0]], [[2.+3., 1]], 1.)
        np.testing.assert_equal(result['sinr_linear'], [[1/6, 0]])
        np.testing.assert_equal(result['js_linear'], [[5, 0]])
        np.testing.assert_equal(result['js_defined'], [[True, False]])
        self.assertTrue(all(np.isfinite(array).all() for array in result.values()))
        for invalid in (float('nan'), -1., 0.):
            with self.assertRaises(ValueError):
                maps.combine([1], [2], invalid)

    def test_two_delayed_paths_show_legacy_double_carrier_phase_error(self):
        # Analytic fixture using the official1.2 CIR/CFR equations. This is a
        # formula regression, not a replacement for running actual Paths.cfr.
        fc = 2.4e9
        delays = np.array([0, 1/(2*fc)])
        passband = np.array([1., .5])
        class AnalyticPaths:
            def cfr(self, *, frequencies, normalize_delays, normalize, out_type):
                assert normalize_delays and not normalize and out_type == 'numpy'
                baseband = passband * np.exp(-2j*math.pi*fc*delays)
                h = np.sum(baseband * np.exp(-2j*math.pi*frequencies[0]*delays))
                return np.array([h]).reshape((1,)*6)
        physical = maps.cfr_power(AnalyticPaths(), 0)[0]
        legacy = maps.cfr_power(AnalyticPaths(), fc)[0]
        self.assertAlmostEqual(physical, .25)
        self.assertAlmostEqual(legacy, 2.25)
        self.assertAlmostEqual(legacy/physical, 9)

    def test_scene_tamper_and_paths_outside_workspace_fail(self):
        changed = copy.deepcopy(self.config)
        changed['scene_xml'] = '../outside.xml'
        with self.assertRaises(ValueError):
            maps.validate(changed, self.root)
        with (self.root / self.config['scene_xml']).open('ab') as stream:
            stream.write(b'\n<!-- changed -->')
        with self.assertRaisesRegex(ValueError, 'hash'):
            maps.validate(self.config, self.root)

    def test_runtime_mismatch_preserves_failed_attempt_and_rerunnable_frozen_config(self):
        output = self.root / 'map-attempt'
        with patch.object(maps.importlib.metadata, 'version', return_value='unsupported'):
            with self.assertRaisesRegex(ValueError, 'unsupported map runtime'):
                maps.execute(self.config, output, root=self.root)
        record = json.loads((output / 'metadata.json').read_text())
        self.assertEqual(record['status'], 'failed')
        self.assertIn('unsupported map runtime', record['error'])
        frozen = json.loads((output / 'config.json').read_text())
        self.assertEqual(frozen['scene_xml'], 'map-attempt/scene/scene.xml')
        self.assertEqual(frozen['radio_config'], 'map-attempt/radio_config.yaml')
        with (self.root / self.config['scene_xml']).open('ab') as stream:
            stream.write(b'\n<!-- next experiment -->')
        maps.validate(frozen, self.root)


if __name__ == '__main__':
    unittest.main()
