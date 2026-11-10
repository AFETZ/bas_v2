"""Reject scene declarations that the existing ENU/native adapters cannot apply."""
import copy
import json
from pathlib import Path
import tempfile
import shutil
import unittest
from unittest.mock import patch

import bas
from scripts.product.operator_ui import UIState
from scripts.product import prepare_urban_gazebo as gazebo
from scripts.product.urban_mission import UrbanScenario


class SceneConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.scene = json.loads((bas.ROOT / 'network/config/link_test_mission.json').read_text())
        self.config = json.loads((bas.ROOT / 'runtime-link-test.json').read_text())
        self.config['environment']['scene'] = 'scene.json'
        target = self.root / 'network/config'
        target.mkdir(parents=True)
        for name in ('scenario_5uav_town01_native_product.yaml', 'town01_sitl.parm'):
            shutil.copyfile(bas.ROOT / 'network/config' / name, target / name)

    def load(self, scene):
        (self.root / 'scene.json').write_text(json.dumps(scene))
        (self.root / 'runtime.json').write_text(json.dumps(self.config))
        return bas.load_config(self.root / 'runtime.json', root=self.root)

    def test_declared_ned_cannot_be_accepted_as_unchanged_enu(self):
        self.scene['frame']['coordinate_frame'] = 'NED'
        with self.assertRaisesRegex(ValueError, 'ENU'):
            self.load(self.scene)

    def test_nonzero_flat_ground_is_rejected_before_generating_zero_ground(self):
        self.scene['frame']['ground_surface_z_m'] = 25
        path = self.root / 'scene.json'
        path.write_text(json.dumps(self.scene))
        output = self.root / 'generated'
        with patch.object(gazebo, 'ROOT', self.root), self.assertRaisesRegex(ValueError, 'ground_surface'):
            gazebo.prepare(path, output)
        self.assertFalse(output.exists())

    def test_ui_uses_same_scene_validation(self):
        self.scene['mission']['failsafe'] = {'mode': 'LAND'}
        with self.assertRaisesRegex(ValueError, 'unknown.*mission'):
            UIState(root=self.root).validate({'config': self.config, 'scene': self.scene})

    def test_unknown_scene_frame_mission_radio_and_barrier_fields_are_rejected(self):
        mutations = [
            lambda s: s.update(physics={'solver_step_s': .02}),
            lambda s: s['frame'].update(yaw_offset_rad=1),
            lambda s: s['mission'].update(failsafe={'mode': 'LAND'}),
            lambda s: s['radio'].update(baud_rate=57600),
            lambda s: s['radio']['los'].update(exponnent=3),
            lambda s: s['buildings'][0].update(material='glass'),
            lambda s: s['mission'].update(barriers={'1:1': {'minimum_reached': {'2': 0}, 'timeout_s': 5}}),
        ]
        for mutate in mutations:
            value = copy.deepcopy(self.scene)
            mutate(value)
            with self.subTest(scene=value), self.assertRaisesRegex(ValueError, 'unknown'):
                self.load(value)

    def test_unsupported_schema_and_transform_are_rejected(self):
        changes = [
            lambda s: s.update(schema_version=99),
            lambda s: s.update(schema_version=True),
            lambda s: s['frame'].update(world_enu_to_local_ned='N=x,E=y,D=z'),
        ]
        for change in changes:
            value = copy.deepcopy(self.scene)
            change(value)
            with self.subTest(scene=value), self.assertRaises(ValueError):
                self.load(value)

    def test_scene_json_duplicates_are_not_silently_last_value_wins(self):
        path = self.root / 'scene.json'
        text = json.dumps(self.scene).replace('"speed_mps": 3.0', '"speed_mps": 99, "speed_mps": 3.0')
        self.assertIn('"speed_mps": 99', text)
        path.write_text(text)
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            UrbanScenario.load(path)

    def test_numeric_strings_and_booleans_are_not_json_coordinate_units(self):
        mutations = [
            lambda s: s['spawns']['1'].__setitem__(0, True),
            lambda s: s['mission'].update(speed_mps='3'),
            lambda s: s['frame'].update(spherical_heading_deg=True),
            lambda s: s['mission'].update(barriers={'1:1': {'minimum_reached': {'2': .9}}}),
        ]
        for change in mutations:
            value = copy.deepcopy(self.scene)
            change(value)
            with self.subTest(scene=value), self.assertRaises(ValueError):
                self.load(value)

    def test_existing_metadata_and_all_public_scene_files_remain_accepted(self):
        for name in ('urban_low.json', 'link_test_mission.json'):
            value = json.loads((bas.ROOT / 'network/config' / name).read_text())
            scenario = self.load(value)
            self.assertEqual(scenario['drone']['count'], 5)

    def test_control_only_legacy_scene_defaults_remain_accepted(self):
        raw = dict(spawns={'1': [0, 0, 2]}, routes={'1': [[0, 0, 10]]},
                   mission=dict(world_ground_reference_z_m=.195, telemetry_max_age_s=.5,
                                speed_mps=3, waypoint_dwell_s=2))
        path = self.root / 'legacy.json'
        path.write_text(json.dumps(raw))
        scenario = UrbanScenario.load(path)
        self.assertEqual(scenario.uav_ids, (1,))
        self.assertEqual(scenario.speed_mps, 3)


if __name__ == '__main__':
    unittest.main()
