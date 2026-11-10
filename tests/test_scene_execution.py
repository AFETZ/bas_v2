"""Scene controls must reach existing Gazebo/SITL boundaries without silent overrides."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import bas
from scripts.product import prepare_urban_gazebo as gazebo
from scripts.product.operator_ui import UIState
from scripts.product.urban_mission import UrbanScenario


class SceneExecutionTests(unittest.TestCase):
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

    def prepare(self, scene, name='generated'):
        path = self.root / 'scene.json'
        path.write_text(json.dumps(scene))
        output = self.root / name
        with patch.object(gazebo, 'ROOT', self.root):
            gazebo.prepare(path, output)
        return output

    def test_each_supported_step_reaches_world_and_frozen_mission(self):
        for step in (.00025, .0005, .001):
            with self.subTest(step=step):
                raw = dict(self.scene, physics=dict(max_step_size_s=step))
                output = self.prepare(raw, str(step))
                physics = ET.parse(output/'world.sdf').getroot().find('world/physics')
                self.assertEqual(float(physics.findtext('max_step_size')), step)
                self.assertEqual(float(physics.findtext('real_time_update_rate')), 1/step)
                self.assertEqual(float(physics.findtext('real_time_factor')), 1)
                frozen = UrbanScenario.load(output/'mission-scene.json', root=self.root)
                self.assertEqual(frozen.raw['physics']['max_step_size_s'], step)
                settings = json.loads((output/'runtime-settings.json').read_text())
                self.assertEqual(settings['physics']['imu_substeps'], round(.001/step))
                self.assertEqual(settings['lifecycle'], 'new_run_only')
                self.assertEqual(settings['generated_inputs']['world_sdf_sha256'],
                                 hashlib.sha256((output/'world.sdf').read_bytes()).hexdigest())

    def test_absent_failsafe_preserves_default_parameter_bytes_and_declares_no_readback(self):
        output = self.prepare(self.scene)
        expected = (self.root/'network/config/town01_sitl.parm').read_text()
        expected += '\n# Pinned ArduCopter4.8 native waypoint speed, m/s\nWP_SPD 3\n'
        self.assertEqual((output/'urban_sitl.parm').read_text(), expected)
        settings = json.loads((output/'runtime-settings.json').read_text())
        self.assertFalse(settings['failsafe']['configured'])
        self.assertEqual(settings['failsafe']['parameter_overrides'], {})
        self.assertEqual(settings['failsafe']['upstream_readback_status'], 'not_recorded')
        self.assertEqual(settings['failsafe']['pinned_default_expectations']['FS_GCS_TIMEOUT'], 5)

    def test_explicit_actions_write_current_upstream_names_and_keep_fs_options_inherited(self):
        for action, native, timeout in [('disabled',0,2),('rtl',1,5),('land',5,120)]:
            raw = dict(self.scene, failsafe=dict(gcs_action=action,gcs_timeout_s=timeout))
            output = self.prepare(raw, action)
            values = {parts[0]:float(parts[1]) for line in (output/'urban_sitl.parm').read_text().splitlines()
                      if (parts:=line.split()) and not line.startswith('#')}
            self.assertEqual({name:values[name] for name in ('FS_GCS_ENABLE','FS_GCS_TIMEOUT','MAV_GCS_SYSID','MAV_GCS_SYSID_HI')},
                             dict(FS_GCS_ENABLE=native,FS_GCS_TIMEOUT=timeout,MAV_GCS_SYSID=255,MAV_GCS_SYSID_HI=0))
            self.assertNotIn('FS_OPTIONS', values)
            self.assertNotIn('SYSID_MYGCS', values)
            settings = json.loads((output/'runtime-settings.json').read_text())
            self.assertTrue(settings['failsafe']['configured'])
            self.assertEqual(settings['failsafe']['upstream_readback_status'], 'not_recorded')
            self.assertEqual(settings['generated_inputs']['sitl_defaults_sha256'],
                             hashlib.sha256((output/'urban_sitl.parm').read_bytes()).hexdigest())

    def test_wrong_units_values_and_unknown_fields_fail_before_creating_output(self):
        for field, value in [
            ('physics',None), ('physics',{'max_step_size_s':True}),
            ('physics',{'max_step_size_s':'0.001'}), ('physics',{'max_step_size_s':.002}),
            ('physics',{'max_step_size_s':.0003}), ('physics',{'real_time_factor':2}),
            ('failsafe',None), ('failsafe',{'gcs_action':'LAND'}),
            ('failsafe',{'gcs_action':True}), ('failsafe',{'gcs_timeout_s':True}),
            ('failsafe',{'gcs_timeout_s':'5'}), ('failsafe',{'gcs_timeout_s':1.99}),
            ('failsafe',{'gcs_timeout_s':120.01}), ('failsafe',{'FS_GCS_ENABLE':5})]:
            with self.subTest(field=field,value=value):
                raw = copy.deepcopy(self.scene)
                raw[field] = value
                with self.assertRaises(ValueError):
                    self.prepare(raw)
                self.assertFalse((self.root/'generated').exists())

    def test_explicit_settings_reject_other_images_and_enabled_external_bench(self):
        self.scene['failsafe'] = dict(gcs_action='land')
        (self.root/'scene.json').write_text(json.dumps(self.scene))
        self.config['runtime']['image'] = 'sha256:'+'a'*64
        path=self.root/'runtime.json'
        path.write_text(json.dumps(self.config))
        with self.assertRaisesRegex(ValueError,'pinned qualified'):
            bas.load_config(path,root=self.root)
        self.config=json.loads((bas.ROOT/'runtime-link-test.json').read_text())
        self.config['environment']['scene']='scene.json'
        self.config['hitl']=dict(mode='radio_bench',request='missing-hardware.json')
        self.config['experiment']['policy']='off'
        path.write_text(json.dumps(self.config))
        with self.assertRaisesRegex(ValueError,'virtual relay'):
            bas.load_config(path,root=self.root)

    def test_ui_and_cli_validation_accept_same_supported_controls(self):
        self.scene.update(physics=dict(max_step_size_s=.0005),
                          failsafe=dict(gcs_action='land',gcs_timeout_s=3))
        (self.root/'scene.json').write_text(json.dumps(self.scene))
        (self.root/'runtime.json').write_text(json.dumps(self.config))
        validated = bas.load_config(self.root/'runtime.json', root=self.root)
        ui = UIState(root=self.root).validate(dict(config=self.config,scene=self.scene))
        self.assertEqual(validated['drone']['count'],5)
        self.assertEqual(ui['drone']['count'],5)


if __name__ == '__main__':
    unittest.main()
