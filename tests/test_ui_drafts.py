"""Saved scenarios use existing validation/freezer; save never launches simulation."""
import copy
import hashlib
import json
from pathlib import Path
import unittest
from urllib.parse import urlencode

import bas
from tests import test_operator_ui as existing


class UIDraftTests(unittest.TestCase):
    def setUp(self):
        self.fixture=existing.OperatorUITests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root=self.fixture.root

    def test_save_edit_reload_returns_effective_config_and_no_runtime(self):
        payload=self.fixture.payload(scene=copy.deepcopy(self.fixture.scene))
        payload['config']['experiment']['duration_s']=35
        payload['scene']['mission']['speed_mps']=2
        payload['scene']['physics']={'max_step_size_s':.0005}
        payload['scene']['failsafe']={'gcs_action':'land','gcs_timeout_s':3}
        code,data,_=self.fixture.request('/api/save',payload)
        self.assertEqual(code,201,data)
        self.assertFalse(data['runtime_started'])
        self.assertEqual(self.fixture.launches,[])
        self.assertEqual(self.fixture.stops,[])
        saved=self.root/data['config_path']
        self.assertEqual(hashlib.sha256(saved.read_bytes()).hexdigest(),data['config_sha256'])
        code,reloaded,_=self.fixture.request('/api/config?'+urlencode(dict(path=data['config_path'])))
        self.assertEqual(code,200,reloaded)
        self.assertEqual(reloaded['config']['experiment']['duration_s'],35)
        self.assertEqual(reloaded['scene']['mission']['speed_mps'],2)
        self.assertEqual(reloaded['scene']['physics']['max_step_size_s'],.0005)
        self.assertEqual(reloaded['scene']['failsafe']['gcs_action'],'land')
        self.assertEqual(self.fixture.request('/api/validate',dict(config=reloaded['config']))[0],200)
        code,status,_=self.fixture.request('/api/status?run_id=ui-test')
        self.assertEqual(code,200,status)
        self.assertIsNone(status['controller'])
        self.assertFalse(status['draft']['runtime_started'])

    def test_duplicate_and_invalid_ids_cannot_overwrite_saved_or_source_inputs(self):
        source=self.root/self.fixture.config['environment']['scene']
        before=source.read_bytes()
        payload=self.fixture.payload()
        self.assertEqual(self.fixture.request('/api/save',payload)[0],201)
        folder=self.root/'runs/control/ui-inputs/ui-test'
        hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir()}
        self.assertEqual(self.fixture.request('/api/save',payload)[0],400)
        self.assertEqual(hashes,{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir()})
        self.assertEqual(source.read_bytes(),before)
        payload['run_id']='../outside'
        self.assertEqual(self.fixture.request('/api/save',payload)[0],400)
        self.assertFalse((self.root/'runs/control/outside').exists())

    def test_shared_validation_and_http_guard_reject_bad_save_before_snapshot(self):
        payload=self.fixture.payload()
        payload['config']['runtime']['unsupported']=True
        self.assertEqual(self.fixture.request('/api/save',payload)[0],400)
        payload=self.fixture.payload(overwrite=True)
        self.assertEqual(self.fixture.request('/api/save',payload)[0],400)
        payload=self.fixture.payload()
        self.assertEqual(self.fixture.request('/api/save',payload,headers={'X-BAS-Token':''})[0],403)
        raw=json.dumps(payload).replace('"run_id": "ui-test"','"run_id": "first", "run_id": "second"')
        self.assertEqual(self.fixture.request('/api/save',body=raw.encode())[0],400)
        self.assertFalse((self.root/'runs/control/ui-inputs/ui-test').exists())
        self.assertEqual(self.fixture.launches,[])

    def test_save_freezes_existing_radio_source_and_borrowed_scene_without_drift(self):
        radio=self.root/'radio.yaml'
        radio.write_bytes((bas.ROOT/'network/config/native_wifi_fullmesh_100ms.yaml').read_bytes())
        sources=self.root/'sources.yaml'
        sources.write_bytes((bas.ROOT/'network/config/terrain_directional_source.yaml').read_bytes())
        payload=self.fixture.payload()
        payload['config']['propagation']=dict(model='sionna',radio_config='radio.yaml',
            ground_material='medium_dry_ground',building_material='concrete',thickness_m=.2)
        payload['config']['interference']=dict(sources='sources.yaml')
        payload['config']['runtime']['event_logging']='batched_trace'
        original_scene=self.root/payload['config']['environment']['scene']
        code,data,_=self.fixture.request('/api/save',payload)
        self.assertEqual(code,201,data)
        saved=data['config']
        self.assertNotEqual(saved['propagation']['radio_config'],'radio.yaml')
        self.assertNotEqual(saved['interference']['sources'],'sources.yaml')
        self.assertNotEqual(saved['environment']['scene'],payload['config']['environment']['scene'])
        radio.write_text('subsequent original edit\n')
        sources.write_text('subsequent original edit\n')
        scene=json.loads(original_scene.read_text());scene['mission']['speed_mps']=100
        original_scene.write_text(json.dumps(scene))
        code,reloaded,_=self.fixture.request('/api/config?'+urlencode(dict(path=data['config_path'])))
        self.assertEqual(code,200,reloaded)
        self.assertNotEqual(reloaded['scene']['mission']['speed_mps'],100)
        self.assertEqual(hashlib.sha256((self.root/saved['propagation']['radio_config']).read_bytes()).hexdigest(),
                         data['input_sha256']['radio_sha256'])
        self.assertEqual(self.fixture.launches,[])

    def test_hitl_draft_preserves_request_and_catalog_pins_without_opening_endpoints(self):
        profiles=self.root/'network/config/hitl_reference_devices.json'
        profiles.write_bytes((bas.ROOT/'network/config/hitl_reference_devices.json').read_bytes())
        request=dict(controller='pixhawk_6c',radio_device='microhard_p2400',uav_id=3,
            channels={name:dict(endpoint=dict(kind='serial',device=f'/dev/ttyUSB{i}',baud=115200))
                      for i,name in enumerate(('control','payload'))})
        bas.write_json(self.root/'request.json',request)
        payload=self.fixture.payload()
        payload['config']['hitl']=dict(mode='radio_bench',request='request.json')
        payload['config']['experiment']['policy']='off'
        code,data,_=self.fixture.request('/api/save',payload)
        self.assertEqual(code,201,data)
        hitl=data['config']['hitl']
        self.assertEqual(hitl['request_sha256'],data['input_sha256']['hitl_request_sha256'])
        self.assertEqual(hitl['profiles_sha256'],data['input_sha256']['hitl_profiles_sha256'])
        self.assertEqual(hashlib.sha256((self.root/data['config_path']).with_name('hitl-bundle.json').read_bytes()).hexdigest(),data['input_sha256']['hitl_bundle_sha256'])
        self.assertEqual(self.fixture.launches,[])


if __name__=='__main__':
    unittest.main()
