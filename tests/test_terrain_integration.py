"""Shared mesh/mission contracts; native flight and GPU timing are separate tests."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import yaml

import bas
from scripts.product import prepare_terrain_scene as terrain
from scripts.product import prepare_urban_gazebo as urban
from scripts.product.urban_mission import UrbanScenario


class TerrainIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = terrain.reference_config(cells=8)
        self.bundle = self.root / 'input-terrain'
        self.manifest = terrain.prepare(self.source, self.bundle)
        grid = terrain.terrain_grid(self.source)
        self.raw = json.loads((bas.ROOT / 'network/config/urban_low.json').read_text())
        self.raw['terrain_bundle'] = 'input-terrain'
        self.raw['buildings'] = []
        self.raw['frame']['spherical_heading_deg'] = 0
        self.raw['mission']['model_rest_clearance_m'] = .195
        self.raw['mission']['barriers'] = {}
        self.raw['spawns'], self.raw['routes'] = {}, {}
        for i in (1, 2):
            xy = self.source['sample_sites_xy_m'][f'uav{i}']
            height = terrain.terrain_height(grid, *xy)
            self.raw['spawns'][str(i)] = [*xy, height + 2]
            self.raw['routes'][str(i)] = [[*xy, height + .195 + 8]]
        xy = self.source['sample_sites_xy_m']['gcs']
        self.raw['gcs'] = [*xy, terrain.terrain_height(grid, *xy) + 2]
        self.scene_file = self.root / 'scene.json'
        self.write_scene()
        target = self.root / 'network/config'
        target.mkdir(parents=True)
        for name in ('scenario_5uav_town01_native_product.yaml', 'town01_sitl.parm'):
            shutil.copy2(bas.ROOT / 'network/config' / name, target / name)
        self.radio_file = self.root / 'radio.yaml'
        shutil.copy2(bas.ROOT / 'network/config/native_wifi_80211n_spectrum_product.yaml', self.radio_file)

    def write_scene(self):
        self.scene_file.write_text(json.dumps(self.raw))

    def load(self):
        self.write_scene()
        return UrbanScenario.load(self.scene_file, root=self.root)

    def rehash(self, name):
        path = self.bundle / name
        self.manifest['files'][name] = dict(sha256=hashlib.sha256(path.read_bytes()).hexdigest(), bytes=path.stat().st_size)
        (self.bundle / 'scene-manifest.json').write_text(json.dumps(self.manifest))

    def prepare(self, output='run', **overrides):
        settings = dict(propagation_profile='sionna', radio_config=self.radio_file,
                        ground_material='medium_dry_ground', building_material='concrete', thickness_m=.2)
        settings.update(overrides)
        self.write_scene()
        with patch.object(urban, 'ROOT', self.root):
            urban.prepare(self.scene_file, self.root / output, 2, **settings)
        return self.root / output

    def test_ground_reference_and_ned_use_each_spawn_triangle_height(self):
        scenario = self.load()
        self.assertNotEqual(scenario.ground_for(1), scenario.ground_for(2))
        for i in (1, 2):
            expected = self.manifest['ground_sites'][f'uav{i}']['ground_z_m'] + .195
            self.assertAlmostEqual(scenario.ground_for(i), expected)
            frame = scenario.frame(i, (2, 3, 0))
            x, y, z = scenario.routes[i][0]
            self.assertAlmostEqual(frame.to_ned((x, y, z))[2], -8)
            self.assertEqual(frame.to_ned((x, y, expected)), (2, 3, 0))

    def test_waypoint_uses_terrain_at_waypoint_not_only_spawn(self):
        grid = terrain.terrain_grid(self.source)
        xy = [2500, 2500]
        height = terrain.terrain_height(grid, *xy)
        self.assertGreater(height, self.manifest['ground_sites']['uav1']['ground_z_m'] + 3)
        self.raw['routes']['1'] = [[*xy, height + .195 + 1.99]]
        with self.assertRaisesRegex(ValueError, 'waypoint.*terrain'):
            self.load()
        self.raw['routes']['1'][0][2] = height + .195 + 2
        self.load()

    def test_hash_failure_is_rejected_before_simulation(self):
        with (self.bundle / 'terrain.obj').open('ab') as stream:
            stream.write(b'\n# altered\n')
        with self.assertRaisesRegex(ValueError, 'hash/size mismatch'):
            self.load()

    def test_origin_frequency_sites_and_unknown_terrain_are_not_ignored(self):
        original = copy.deepcopy(self.raw)
        mutations = [lambda r: r['frame'].update(spherical_heading_deg=10),
                     lambda r: r['radio'].update(frequency_hz=2437e6),
                     lambda r: r['spawns']['1'].__setitem__(0, -1799),
                     lambda r: r.update(terrain_grid='invented')]
        for mutate in mutations:
            self.raw = copy.deepcopy(original)
            mutate(self.raw)
            with self.subTest(raw=self.raw), self.assertRaises(ValueError):
                self.load()

    def test_rehashed_transform_or_different_rt_mesh_is_rejected(self):
        path = self.bundle / 'scene.xml'
        original = path.read_bytes()
        tree = ET.parse(path)
        ET.SubElement(tree.getroot().find('shape'), 'transform', name='to_world')
        tree.write(path)
        self.rehash('scene.xml')
        with self.assertRaisesRegex(ValueError, 'geometry/material'):
            self.load()
        path.write_bytes(original)
        tree = ET.parse(path)
        tree.getroot().find('shape/string').set('value', 'buildings.obj')
        tree.write(path)
        self.rehash('scene.xml')
        with self.assertRaisesRegex(ValueError, 'geometry/material'):
            self.load()

    def test_legacy_clearance_has_explicit_derivation_and_wrong_map_rejected(self):
        del self.raw['mission']['model_rest_clearance_m']
        scenario = self.load()
        self.assertAlmostEqual(scenario.model_rest_clearance_m, .195)
        self.assertIn('legacy', scenario.ground_reference_derivation)
        self.raw['mission']['world_ground_reference_z_by_uav_m'] = {'1': .195, '2': .195}
        with self.assertRaisesRegex(ValueError, 'disagree'):
            self.load()

    def test_geometry_profile_and_conflicting_material_cannot_flatten_terrain(self):
        with self.assertRaisesRegex(ValueError, 'requires Sionna'):
            self.prepare(propagation_profile='geometry_logdistance')
        with self.assertRaisesRegex(ValueError, 'materials/thickness'):
            self.prepare(thickness_m=.1)
        runtime = json.loads((bas.ROOT / 'runtime.json').read_text())
        runtime['environment']['scene'] = 'scene.json'
        runtime['drone']['count'] = 2
        path = self.root / 'runtime.json'
        path.write_text(json.dumps(runtime))
        with self.assertRaisesRegex(ValueError, 'requires Sionna'):
            bas.load_config(path, self.root)
        runtime['propagation'] = dict(model='sionna', radio_config='radio.yaml',
                ground_material='medium_dry_ground', building_material='concrete', thickness_m=.1)
        path.write_text(json.dumps(runtime))
        with self.assertRaisesRegex(ValueError, 'materials/thickness'):
            bas.load_config(path, self.root)
        runtime['propagation']['thickness_m'] = .2
        path.write_text(json.dumps(runtime))
        self.assertEqual(bas.load_config(path, self.root)['propagation'], runtime['propagation'])

    def test_generated_world_and_rt_share_frozen_meshes_and_keep_flight_systems(self):
        output = self.prepare()
        generated = yaml.safe_load((output / 'scenario.yaml').read_text())
        world = ET.parse(output / 'world.sdf').getroot().find('world')
        self.assertEqual({m.get('name') for m in world.findall('model')}, {'terrain', 'buildings'})
        plugins = {plugin.get('name') for plugin in world.findall('plugin')}
        for name in ('Physics', 'Imu', 'AirPressure', 'NavSat', 'Sensors', 'Magnetometer'):
            self.assertIn('gz::sim::systems::' + name, plugins)
        self.assertEqual(generated['base_simulation']['sitl_home'], '46.607213,14.278461,446.0,0')
        self.assertEqual(generated['scenario']['map']['kind'], 'shared_triangle_terrain')
        self.assertEqual(self.root / generated['scenario']['map']['scene_xml'], output / 'terrain/scene.xml')
        for uri in world.findall('.//mesh/uri'):
            self.assertIn(uri.text, ('terrain/terrain.obj', 'terrain/buildings.obj'))
            self.assertTrue((output / uri.text).is_file())
        for name in ('terrain.obj', 'buildings.obj', 'scene.xml', 'world.sdf', 'scene-manifest.json'):
            self.assertEqual((output / 'terrain' / name).read_bytes(), (self.bundle / name).read_bytes())
        resolved = UrbanScenario.load(output / 'mission-scene.json', root=self.root)
        self.assertEqual(resolved.terrain_bundle_path, output / 'terrain')
        exported = resolved.raw['mission']
        self.assertIn('explicit model_rest_clearance_m', exported['ground_reference_derivation'])
        for i in (1, 2):
            self.assertEqual(exported['world_ground_reference_z_by_uav_m'][str(i)], resolved.ground_for(i))
            self.assertAlmostEqual(generated['flight']['takeoff_relative_altitude_m'][f'uav{i}'], 8)
        # Mutable source assets cannot change this run after preparation.
        with (self.bundle / 'terrain.obj').open('ab') as stream:
            stream.write(b'\n# next experiment\n')
        UrbanScenario.load(output / 'mission-scene.json', root=self.root)


if __name__ == '__main__':
    unittest.main()
