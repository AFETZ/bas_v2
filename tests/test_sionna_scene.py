"""Geometry preparation checks; native Sionna loading/RT is a separate target probe."""
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from scripts.product import prepare_urban_gazebo as gazebo
from scripts.product.prepare_sionna_scene import prepare, static_boxes


class SionnaSceneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = json.loads((gazebo.ROOT / 'network/config/urban_low.json').read_text())
        target = self.root / 'network/config'
        target.mkdir(parents=True)
        for name in ('scenario_5uav_town01_native_product.yaml', 'town01_sitl.parm'):
            shutil.copy2(gazebo.ROOT / 'network/config' / name, target / name)
        self.config = self.root / 'scene.json'
        self.config.write_text(json.dumps(self.data))
        self.generated = self.root / 'gazebo'
        with patch.object(gazebo, 'ROOT', self.root):
            gazebo.prepare(self.config, self.generated)
        self.world = self.generated / 'world.sdf'

    def prepare(self, **overrides):
        options = dict(ground_material='medium_dry_ground', building_material='concrete', thickness_m=.1)
        options.update(overrides)
        return prepare(self.config, self.world, self.root / 'radio', **options)

    def test_cube_bounds_match_actual_gazebo_collision_geometry(self):
        scene = ET.parse(self.prepare()).getroot()
        boxes = static_boxes(self.world)
        observed = {}
        for shape in scene.findall('shape'):
            self.assertEqual(shape.get('type'), 'cube')
            scale = shape.find('transform/scale')
            translation = shape.find('transform/translate')
            half = [float(scale.get(axis)) for axis in 'xyz']
            centre = [float(translation.get(axis)) for axis in 'xyz']
            observed[shape.get('id').removeprefix('mesh-')] = {
                'min': [c - h for c, h in zip(centre, half)],
                'max': [c + h for c, h in zip(centre, half)]}
        self.assertEqual(observed, boxes)
        self.assertEqual(observed['urban_ground']['max'][2], self.data['frame']['ground_surface_z_m'])

    def test_materials_are_explicit_and_manifest_identifies_reference_profile(self):
        scene_file = self.prepare()
        scene = ET.parse(scene_file).getroot()
        self.assertEqual({m.find('string').get('value') for m in scene.findall('bsdf')},
                         {'medium_dry_ground', 'concrete'})
        manifest = json.loads((scene_file.parent / 'scene-manifest.json').read_text())
        self.assertEqual(manifest['materials']['status'], 'uncalibrated_itu_reference')
        self.assertEqual(manifest['frequency_hz'], self.data['radio']['frequency_hz'])
        self.assertEqual(manifest['target_api'], {'sionna_rt': '1.2.0', 'mitsuba': '3.7.1'})
        self.assertEqual(len(manifest['scene_sha256']), 64)

    def test_json_and_world_mismatch_is_rejected_before_output(self):
        self.data['buildings'][0]['max'][2] += 1
        self.config.write_text(json.dumps(self.data))
        with self.assertRaisesRegex(ValueError, 'bounds disagree'):
            self.prepare()
        self.assertFalse((self.root / 'radio').exists())

    def test_missing_or_additional_collision_obstacle_is_rejected(self):
        tree = ET.parse(self.world)
        world = tree.getroot().find('world')
        world.remove(world.findall('model')[-1])
        tree.write(self.world)
        with self.assertRaisesRegex(ValueError, 'roster differs'):
            self.prepare()

    def test_rotated_geometry_cannot_silently_be_axis_aligned(self):
        tree = ET.parse(self.world)
        tree.getroot().find('world/model/pose').text = '0 0 0 0 0 0.1'
        tree.write(self.world)
        with self.assertRaisesRegex(ValueError, 'rotated boxes'):
            self.prepare()

    def test_unknown_or_out_of_frequency_material_and_bad_thickness_rejected(self):
        for overrides in ({'building_material': 'invented'}, {'building_material': 'floorboard'},
                          {'thickness_m': 0}, {'thickness_m': True}, {'thickness_m': float('nan')}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.prepare(**overrides)


if __name__ == '__main__':
    unittest.main()
