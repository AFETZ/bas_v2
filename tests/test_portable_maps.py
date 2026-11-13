"""Portable map entry lifecycle, ownership, lock and dependency boundary checks."""
from contextlib import contextmanager
import json
from pathlib import Path
import shutil
import signal
import tempfile
import unittest
from unittest.mock import patch

import bas
from scripts.product import bootstrap_native_runtime as native
from scripts.product import compute_radio_maps as mapper
import test_radio_maps as rf_fixture


class PortableMapTests(unittest.TestCase):
    def setUp(self):
        self.fixture=rf_fixture.RadioMapTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root=self.fixture.root
        (self.root/'.devcontainer').mkdir()
        (self.root/'.devcontainer/runtime-assets.json').write_text(json.dumps(dict(image_id='sha256:'+'a'*64)))
        (self.root/'map.json').write_text(json.dumps(self.fixture.config))
        script=self.root/'scripts/product/compute_radio_maps.py'
        script.parent.mkdir(parents=True)
        script.write_text('# fixture source identity\n')
        source=self.root/'snapshot'
        source.mkdir()
        (source/'metadata.json').write_text(json.dumps(dict(status='completed')))
        (source/'maps.npz').write_bytes(b'fixed-input')
        (self.root/'evidence.json').write_text('{}')
        shutil.copyfile(bas.ROOT/'network/config/service_profiles_v1.json',
                        self.root/'network/config/service_profiles_v1.json')
        self.patch=patch.object(bas,'ROOT',self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.output=Path('runs/maps/new-attempt')

    def execute(self, overlay=True, *, consumer=None, **options):
        def completed(*args,**kwargs):
            out=kwargs['output']
            (out/'maps-dependencies.json').write_text(json.dumps(dict(numpy='1.26.4',PyYAML='6.0.2')))
            if not options.get('validate_only'):
                data=out/'data';data.mkdir()
                name='service_metadata.json' if overlay else 'metadata.json'
                (data/name).write_text(json.dumps(dict(status='completed')))
        with patch.object(native,'image_id'), patch.object(native,'container',side_effect=consumer or completed) as invoke, patch.object(bas.subprocess,'check_output',return_value='b'*40+'\n'):
            code=bas.maps(None if overlay else Path('map.json'),self.output,
                service_map_from=Path('snapshot') if overlay else None,
                service_evidence=Path('evidence.json') if overlay else None,**options)
        return code,invoke

    def test_overlay_uses_readonly_cpu_container_without_taking_rf_lock(self):
        with patch.object(native,'runtime_lock',side_effect=AssertionError('RF lock must not be used')):
            code,invoke=self.execute()
        self.assertEqual(code,0)
        kwargs=invoke.call_args.kwargs
        self.assertTrue(kwargs['readonly'])
        self.assertTrue(kwargs['workspace_output'])
        self.assertFalse(kwargs['gpu'])
        self.assertEqual(kwargs['supplementary_groups'],(1000,))
        receipt=json.loads((self.root/self.output/'maps-attempt.json').read_text())
        self.assertEqual(receipt['status'],'completed')
        self.assertEqual(receipt['mode'],'service_overlay')
        self.assertEqual(receipt['output_files'][0]['path'],'data/service_metadata.json')

    def test_rf_compute_is_inside_existing_lock_and_gpu_is_explicit_variant_choice(self):
        for variant,wants_gpu in [('cuda_ad_mono_polarized',True),('llvm_ad_mono_polarized',False)]:
            held=[]
            @contextmanager
            def lease():
                held.append(True)
                try:yield
                finally:held.pop()
            def completed(*args,**kwargs):
                self.assertEqual(held,[True])
                self.assertEqual(kwargs['gpu'],wants_gpu)
                out=kwargs['output']
                (out/'maps-dependencies.json').write_text('{}')
                (out/'data').mkdir()
                (out/'data/metadata.json').write_text('{"status":"completed"}')
            self.output=Path('runs/maps/'+variant)
            with patch.object(native,'runtime_lock',lease):
                self.execute(False,consumer=completed,variant=variant)
            self.assertFalse(held)

    def test_rf_validation_does_not_start_gpu_or_acquire_runtime_lock(self):
        with patch.object(native,'runtime_lock',side_effect=AssertionError('validation should stay CPU-only')):
            _,invoke=self.execute(False,validate_only=True)
        self.assertFalse(invoke.call_args.kwargs['gpu'])
        receipt=json.loads((self.root/self.output/'maps-attempt.json').read_text())
        self.assertTrue(receipt['validation_only'])
        self.assertEqual(receipt['output_files'],[])

    def test_busy_runtime_rejects_compute_before_creating_attempt(self):
        with patch.object(native,'runtime_lock',side_effect=ValueError('BAS runtime lock is held')):
            with self.assertRaisesRegex(ValueError,'held'):
                self.execute(False)
        self.assertFalse((self.root/self.output).exists())

    def test_output_is_new_scoped_and_completed_snapshot_is_required(self):
        for output in [Path('../outside'),Path('scripts/rewrite'),Path('runs/maps')]:
            self.output=output
            with self.assertRaises(ValueError):
                self.execute()
        self.output=Path('runs/maps/new-attempt')
        (self.root/'snapshot/metadata.json').write_text('{"status":"running"}')
        with self.assertRaisesRegex(ValueError,'completed RF'):
            self.execute()
        self.assertFalse((self.root/self.output).exists())

    def test_symlinked_maps_root_is_rejected_before_writing_source_files(self):
        (self.root/'runs').mkdir()
        (self.root/'scripts').mkdir(exist_ok=True)
        (self.root/'runs/maps').symlink_to(self.root/'scripts',target_is_directory=True)
        with self.assertRaisesRegex(ValueError,'canonical runs/maps'):
            self.execute()
        self.assertFalse((self.root/'scripts/new-attempt').exists())

    def test_sidecar_symlinks_outside_workspace_are_rejected_before_container(self):
        with tempfile.TemporaryDirectory() as external:
            for relative,overlay in [('snapshot/metadata.json',True),
                                     ('prepared/rt/scene-manifest.json',False)]:
                with self.subTest(relative=relative):
                    sidecar=self.root/relative
                    data=sidecar.read_bytes()
                    outside=Path(external)/sidecar.name
                    outside.write_bytes(data)
                    sidecar.unlink()
                    sidecar.symlink_to(outside)
                    try:
                        with patch.object(native,'container') as invoke:
                            with self.assertRaises(ValueError):
                                self.execute(overlay)
                            invoke.assert_not_called()
                        self.assertFalse((self.root/self.output).exists())
                    finally:
                        sidecar.unlink()
                        sidecar.write_bytes(data)

    def test_input_change_or_container_failure_retains_failed_attempt_and_partial_files(self):
        def changed(*args,**kwargs):
            (self.root/'evidence.json').write_text('{"changed":true}')
        with self.assertRaisesRegex(ValueError,'input changed'):
            self.execute(consumer=changed)
        receipt=json.loads((self.root/self.output/'maps-attempt.json').read_text())
        self.assertEqual(receipt['status'],'failed')
        self.output=Path('runs/maps/failed-container')
        def failure(*args,**kwargs):
            (kwargs['output']/'partial.txt').write_text('retained')
            raise RuntimeError('probe consumer failure')
        with self.assertRaisesRegex(RuntimeError,'consumer failure'):
            self.execute(consumer=failure)
        self.assertTrue((self.root/self.output/'partial.txt').exists())
        self.assertEqual(json.loads((self.root/self.output/'maps-attempt.json').read_text())['status'],'failed')

    def test_cancellation_retains_partial_attempt_without_rewriting_inputs(self):
        def cancelled(*args,**kwargs):
            (kwargs['output']/'partial.txt').write_text('retained')
            raise KeyboardInterrupt(signal.SIGTERM)
        before=(self.root/'evidence.json').read_bytes()
        with self.assertRaises(KeyboardInterrupt):
            self.execute(consumer=cancelled)
        receipt=json.loads((self.root/self.output/'maps-attempt.json').read_text())
        self.assertEqual(receipt['status'],'cancelled')
        self.assertEqual((self.root/'evidence.json').read_bytes(),before)
        self.assertTrue((self.root/self.output/'partial.txt').is_file())

    def test_sigterm_unwinds_real_signal_and_removes_only_its_owned_container(self):
        previous=signal.getsignal(signal.SIGTERM)
        if previous != signal.SIG_DFL:
            self.skipTest('requires the default SIGTERM policy')
        cid='a'*64
        calls=[]
        def command(argv,**kwargs):
            calls.append(argv)
            if argv[1]=='run':
                Path(argv[argv.index('--cidfile')+1]).write_text(cid)
                self.assertNotEqual(signal.getsignal(signal.SIGTERM),signal.SIG_DFL)
                signal.raise_signal(signal.SIGTERM)
            return ''
        with patch.object(native,'command',side_effect=command):
            with self.assertRaises(KeyboardInterrupt) as raised:
                native.container(self.root,'sha256:'+'a'*64,'sleep 30')
        self.assertEqual(raised.exception.args,(signal.SIGTERM,))
        self.assertEqual(calls[-1],['docker','container','rm','--force',cid])
        self.assertEqual(signal.getsignal(signal.SIGTERM),previous)

    def test_existing_runner_signal_policy_is_preserved(self):
        previous=signal.getsignal(signal.SIGTERM)
        seen=[]
        def handler(signum,frame):seen.append(signum)
        try:
            signal.signal(signal.SIGTERM,handler)
            with native.container_interrupts():
                self.assertIs(signal.getsignal(signal.SIGTERM),handler)
                signal.raise_signal(signal.SIGTERM)
            self.assertEqual(seen,[signal.SIGTERM])
            self.assertIs(signal.getsignal(signal.SIGTERM),handler)
        finally:
            signal.signal(signal.SIGTERM,previous)

    def test_shared_docker_builder_preserves_only_owned_workspace_output_and_gpu_opt_in(self):
        out=self.root/self.output;out.mkdir(parents=True)
        args=native.docker_command(self.root,'sha256:'+'a'*64,'true',readonly=True,
                                  output=out,workspace_output=True,supplementary_groups=(1000,))
        self.assertEqual(args[args.index('--mount')+1],
            f'type=bind,src={out},dst={native.MOUNT}/runs/maps/new-attempt')
        self.assertNotIn('--gpus',args)
        gpu=native.docker_command(self.root,'sha256:'+'a'*64,'true',readonly=True,
                                 output=out,workspace_output=True,gpu=True)
        self.assertIn('--gpus',gpu)
        self.assertEqual(gpu[gpu.index('--gpus')+1],'all')
        for path in [self.root,self.root/'runs',self.root/'scripts']:
            path.mkdir(exist_ok=True)
            with self.assertRaises(ValueError):
                native.docker_command(self.root,'sha256:'+'a'*64,'true',readonly=True,
                                     output=path,workspace_output=True)


if __name__=='__main__':
    unittest.main()
