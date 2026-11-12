"""Root-container to non-root host handover; explicit pinned image, no GPU."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid

from scripts.product import runtime_storage as storage


@unittest.skipUnless(sys.platform == 'linux' and hasattr(os, 'geteuid') and os.geteuid() != 0
    and shutil.which('docker') and os.environ.get('BAS_TEST_RUNTIME_STORAGE_IMAGE'),
    'explicit pinned image and a non-root Docker-capable Linux host are required')
class RuntimeStorageDockerTests(unittest.TestCase):
    def test_actual_root_700_producer_publishes_via_owned_handover_and_removes_caches(self):
        artifact = os.environ.get('BAS_RUNTIME_STORAGE_TEST_ARTIFACT_DIR')
        if artifact:
            root = Path(artifact)/'workspace'
            root.mkdir(parents=True, exist_ok=False)
        else:
            temporary = tempfile.TemporaryDirectory()
            self.addCleanup(temporary.cleanup)
            root = Path(temporary.name)/'workspace'
        image = os.environ['BAS_TEST_RUNTIME_STORAGE_IMAGE']
        run_id = 'root-handover-fixture'
        name = 'bas-spool-fixture-'+uuid.uuid4().hex
        value = storage.prepare(root, run_id, name, image=image)
        spool = Path(value['spool'])
        producer = r'''
import importlib.util,os,sys
from pathlib import Path
spool=Path(sys.argv[1]); raw=spool/'raw/root-handover-fixture'
(raw/'environment.txt').write_text('runtime_storage_mode=host_tmpfs_run_v1\n')
private=raw/'logs/root-only'; private.mkdir(mode=0o700)
payload=private/'actual.bin'; payload.write_bytes(bytes(range(256))); payload.chmod(0o600)
cache=spool/'caches/drjit/root-cache'; cache.mkdir(mode=0o755)
(cache/'kernel.bin').write_bytes(b'actual root-owned fixture bytes')
(cache/'kernel.bin').chmod(0o600)
spec=importlib.util.spec_from_file_location('storage_observer','/runtime-storage-observer.py')
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
module.write(raw/'logs/runtime_storage_container.json',module.observe_live_paths(module.read(spool/'owner.json')))
print('root producer complete',flush=True)
'''
        command = ['docker', 'run', '--rm', '--name', name, '--log-driver=none', '--network=none',
                   '--read-only', '--user', '0:0', '--cidfile', str(spool/'container.id'),
                   '--mount', f'type=bind,src={spool},dst={spool}', image,
                   'python3', '-c', producer, str(spool)]
        command[command.index(image):command.index(image)] = ['--mount',
            f'type=bind,src={Path(storage.__file__).resolve()},dst=/runtime-storage-observer.py,readonly']
        with Path(value['runtime_log_source']).open('xb') as stream:
            result = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, timeout=30)
        self.assertEqual(result.returncode, 0)
        private = Path(value['raw_source'])/'logs/root-only'
        self.assertEqual(private.stat().st_uid, 0)
        self.assertEqual(private.stat().st_mode & 0o777, 0o700)
        with self.assertRaises(PermissionError):
            (private/'actual.bin').read_bytes()
        proof = storage.publish(root, value, result.returncode)
        raw = Path(value['raw_destination'])
        self.assertTrue(proof['passed'], proof)
        self.assertFalse(spool.exists())
        self.assertEqual((raw/'logs/root-only/actual.bin').read_bytes(), bytes(range(256)))
        self.assertEqual((raw/'logs/root-only/actual.bin').stat().st_uid, os.getuid())
        handover = storage.read(raw/storage.HANDOVER)
        self.assertEqual((handover['status'], handover['returncode']), ('ownership_handed_over', 0))
        self.assertEqual(handover['image'], image)
        self.assertEqual(storage.verify(raw), proof)
        observed_path = raw/'logs/runtime_storage_container.json'
        original_observed = observed_path.read_bytes()
        original_manifest = (raw/storage.MANIFEST).read_bytes()
        original_receipt = (raw/storage.RECEIPT).read_bytes()
        for change in ('mode', 'run_id', 'owner_sha', 'raw_path', 'mount', 'missing_cache'):
            with self.subTest(change=change):
                observed = json.loads(original_observed)
                if change == 'mode': observed['mode'] = 'unknown'
                elif change == 'run_id': observed['run_id'] = 'another-run'
                elif change == 'owner_sha': observed['owner_sha256'] = '0'*64
                elif change == 'raw_path': observed['paths']['raw']['path'] = '/dev/shm/unowned/raw/'+run_id
                elif change == 'mount': observed['paths']['raw']['mount']['filesystem'] = 'ext4'
                else: observed['paths'].pop('cuda_cache')
                storage.write(observed_path, observed)
                manifest = json.loads(original_manifest)
                manifest['members']['logs/runtime_storage_container.json'].update(
                    bytes=observed_path.stat().st_size, sha256=storage.sha(observed_path))
                storage.write(raw/storage.MANIFEST, manifest)
                receipt = json.loads(original_receipt)
                receipt['manifest'].update(sha256=storage.sha(raw/storage.MANIFEST),
                    bytes=sum(member.get('bytes', 0) for member in manifest['members'].values()))
                storage.write(raw/storage.RECEIPT, receipt)
                self.assertFalse(storage.verify(raw)['passed'])
        observed_path.write_bytes(original_observed)
        (raw/storage.MANIFEST).write_bytes(original_manifest)
        (raw/storage.RECEIPT).write_bytes(original_receipt)
        self.assertEqual(storage.verify(raw), proof)
        if artifact:
            storage.write(Path(artifact)/'result.json', dict(passed=True, image=image, host_uid=os.getuid(),
                checks=dict(actual_root_700_producer=True, original_read_denied=True,
                    exact_raw_bytes=True, actual_pinned_handover=True, canonical_fresh_verify=True,
                    root_owned_cache_removed=True, rehashed_invalid_container_observations_rejected=True), proof=proof))


if __name__ == '__main__':
    unittest.main()
