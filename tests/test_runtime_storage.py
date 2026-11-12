"""Actual Linux tmpfs, files, links and atomic exchange; no simulator or GPU."""
import errno
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from scripts.product import runtime_storage as storage


@unittest.skipUnless(sys.platform == 'linux' and Path('/dev/shm').is_dir(), 'Linux tmpfs/renameat2 contract')
class RuntimeStorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)/'workspace'
        self.run_id = 'runtime-storage-fixture'
        self.control = self.root/'runs/control'/self.run_id
        self.raw = self.root/'runs/native-radio-realtime'/self.run_id
        # Real mount/capacity observation stays enabled; fixtures store only KB.
        with patch.object(storage, 'MIN_FREE_BYTES', 1), patch.object(storage, 'MIN_AVAILABLE_MEMORY_BYTES', 1):
            self.value = storage.prepare(self.root, self.run_id, 'fixture-owned-container')
        self.spool = Path(self.value['spool'])
        self.addCleanup(lambda: shutil.rmtree(self.spool) if self.spool.exists() else None)
        self.source = Path(self.value['raw_source'])
        (self.source/'environment.txt').write_text('runtime_storage_mode='+storage.MODE+'\n')
        (self.source/'logs/native_radio_events.csv').write_bytes(b'closed,raw\n1,\x00\xff\n')
        (self.source/'empty').mkdir()
        Path(self.value['runtime_log_source']).write_bytes(b'actual runtime stdout\n')
        self.closed = dict(verified=True, container_name=self.value['container_name'],
            container_id='a'*64, container_absent=True, captured_host_monotonic_ns=time.monotonic_ns())

    def publish(self, code=0):
        with patch.object(storage, 'container_closure', return_value=self.closed):
            return storage.publish(self.root, self.value, code)

    def test_actual_atomic_publication_preserves_live_readers_bytes_and_empty_directories(self):
        content = (self.raw/'logs/native_radio_events.csv').read_bytes()
        with (self.raw/'logs/native_radio_events.csv').open('rb') as existing_reader:
            proof = self.publish()
            self.assertEqual(existing_reader.read(), content)
        self.assertTrue(proof['passed'], proof)
        self.assertFalse(self.raw.is_symlink())
        self.assertFalse(self.spool.exists())
        self.assertEqual((self.raw/'logs/native_radio_events.csv').read_bytes(), content)
        self.assertTrue((self.raw/'empty').is_dir())
        self.assertEqual(storage.verify(self.raw), proof)
        self.assertFalse((self.control/'runtime.log').is_symlink())
        self.assertEqual((self.control/'runtime.log').read_bytes(), b'actual runtime stdout\n')
        self.assertFalse(os.path.lexists(self.control/'stop.request'))

    def test_blocked_disk_copy_keeps_complete_ram_source_and_live_canonical_reader(self):
        entered, release = threading.Event(), threading.Event()
        actual = shutil.copy2
        failures = []
        def blocked(source, destination, **kwargs):
            entered.set()
            if not release.wait(3):
                raise TimeoutError('fixture publication was not released')
            return actual(source, destination, **kwargs)
        def publish():
            try:
                self.publish()
            except BaseException as error:
                failures.append(error)
        with patch.object(storage.shutil, 'copy2', side_effect=blocked):
            thread = threading.Thread(target=publish)
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertTrue(self.raw.is_symlink())
                self.assertEqual((self.raw/'logs/native_radio_events.csv').read_bytes(), b'closed,raw\n1,\x00\xff\n')
                self.assertFalse(storage.verify(self.raw)['passed'])
            finally:
                release.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertTrue(storage.verify(self.raw)['passed'])

    def test_enospc_keeps_recovery_source_and_a_retry_publishes_exact_bytes(self):
        with patch.object(storage.shutil, 'copy2', side_effect=OSError(errno.ENOSPC, 'fixture disk full')):
            with self.assertRaises(OSError):
                self.publish()
        self.assertTrue(self.raw.is_symlink())
        self.assertTrue(self.spool.is_dir())
        self.assertEqual(storage.read(self.control/'runtime_spool_publication_failure.json')['source_retained'], True)
        self.assertTrue(self.publish()['passed'])

    def test_exchange_failure_after_runtime_log_swap_is_recoverable(self):
        with patch.object(storage, 'exchange', side_effect=OSError('fixture exchange interrupted')):
            with self.assertRaises(OSError):
                self.publish()
        self.assertFalse((self.control/'runtime.log').is_symlink())
        self.assertTrue(self.raw.is_symlink())
        self.assertTrue(self.spool.exists())
        self.assertTrue(self.publish()['passed'])

    def test_directory_fsync_failure_after_swap_retains_ram_and_can_be_recovered(self):
        actual = storage.sync_owned
        def fail_directory(path):
            if Path(path) == self.raw.parent:
                raise OSError(errno.EIO, 'fixture publication directory fsync failed')
            return actual(path)
        with patch.object(storage, 'sync_owned', side_effect=fail_directory):
            with self.assertRaisesRegex(OSError, 'fsync failed'):
                self.publish()
        self.assertFalse(self.raw.is_symlink())
        self.assertTrue(self.spool.exists())
        self.assertFalse(storage.verify(self.raw)['passed'])
        self.assertTrue(self.publish()['passed'])
        self.assertFalse(self.spool.exists())

    def test_interrupted_producer_cannot_publish_until_owned_container_is_stopped(self):
        with patch.object(storage, 'container_closure', side_effect=ValueError('runtime producers have not stopped')):
            with self.assertRaisesRegex(ValueError, 'not stopped'):
                storage.publish(self.root, self.value, 1)
        self.assertTrue(self.raw.is_symlink())
        self.assertTrue(self.spool.exists())
        self.assertTrue(self.publish(code=1)['passed'])  # Evidence closure, not mission success.
        self.assertEqual(storage.read(self.raw/storage.RECEIPT)['controller_returncode'], 1)

    def test_stop_request_uses_ram_during_runtime_and_preserves_requested_stop(self):
        stop = self.control/'stop.request'
        self.assertTrue(stop.is_symlink())
        self.assertFalse(stop.exists())
        stop.touch()
        self.assertTrue((self.spool/'stop.request').is_file())
        self.publish(130)
        self.assertTrue(stop.is_file())
        self.assertFalse(stop.is_symlink())

    def test_existing_and_broken_raw_links_never_get_clobbered(self):
        with self.assertRaisesRegex(ValueError, 'already exists'):
            storage.prepare(self.root, self.run_id, 'fixture')
        other = self.raw.with_name('broken')
        other.symlink_to(self.root/'does-not-exist')
        with self.assertRaisesRegex(ValueError, 'already exists'):
            storage.prepare(self.root, 'broken', 'fixture')
        self.assertEqual(os.readlink(other), str(self.root/'does-not-exist'))

    def test_internal_symlink_survives_publication_but_escape_and_hardlink_are_rejected(self):
        linked = self.source/'linked.csv'
        linked.symlink_to(self.source/'logs/native_radio_events.csv')
        self.publish()
        self.assertTrue((self.raw/'linked.csv').is_symlink())
        self.assertEqual((self.raw/'linked.csv').read_bytes(), (self.raw/'logs/native_radio_events.csv').read_bytes())
        outside = self.root/'outside.txt'
        outside.write_text('outside')
        for kind in ('symlink', 'hardlink'):
            candidate = self.raw/'outside.txt'
            if kind == 'symlink':
                candidate.symlink_to(outside)
            else:
                os.link(outside, candidate)
            with self.assertRaises(ValueError):
                storage.tree_members(self.raw)
            self.assertFalse(storage.verify(self.raw)['passed'])
            candidate.unlink()

    def test_missing_raw_cannot_downgrade_a_prospective_request_to_legacy(self):
        self.raw.unlink()
        self.assertFalse(storage.verify(self.raw)['passed'])
        self.assertEqual(storage.verify(self.raw.with_name('never-requested')),
                         dict(status='not_requested_legacy', passed=None))

    def test_same_length_tamper_missing_member_and_extra_file_never_verify(self):
        self.publish()
        path = self.raw/'logs/native_radio_events.csv'
        original = path.read_bytes()
        path.write_bytes(original.replace(b'closed', b'forged'))
        self.assertFalse(storage.verify(self.raw)['passed'])
        path.unlink()
        self.assertFalse(storage.verify(self.raw)['passed'])
        path.write_bytes(original)
        extra = self.raw/'extra.log'
        extra.write_text('unexpected')
        self.assertFalse(storage.verify(self.raw)['passed'])

    def test_escaped_source_metadata_cannot_authorize_physical_publication_destination(self):
        self.publish()
        for changes in (dict(raw_source='/unowned/runtime-storage-fixture'),
                        dict(runtime_log_source='/unowned/runtime.log'),
                        dict(spool='/dev/shm/unowned'), dict(owner_uid=True)):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                storage.validate_descriptor(dict(self.value, **changes), self.run_id)

    def test_new_sdformat_log_is_owned_raw_evidence_and_cannot_be_omitted_or_redirected(self):
        self.assertEqual(self.value['schema'], 2)
        location = self.value['live_paths']['sdformat_log']
        self.assertEqual(location['path'], str(self.source/'logs/sdformat'))
        self.assertEqual(location['mount']['filesystem'], 'tmpfs')
        content = b'actual sdformat diagnostics\n\x00\xff\n'
        (Path(location['path'])/'sdformat.log').write_bytes(content)
        for change in ('missing', 'escaped'):
            changed = json.loads(json.dumps(self.value))
            if change == 'missing':
                changed['live_paths'].pop('sdformat_log')
            else:
                changed['live_paths']['sdformat_log']['path'] = '/home/ubuntu/.sdformat'
            with self.subTest(change=change), self.assertRaises(ValueError):
                storage.validate_descriptor(changed, self.run_id)
        proof = self.publish()
        self.assertEqual(proof['descriptor_schema'], 2)
        self.assertEqual((self.raw/'logs/sdformat/sdformat.log').read_bytes(), content)
        self.assertEqual(proof['members']['logs/sdformat/sdformat.log']['sha256'],
                         storage.sha(self.raw/'logs/sdformat/sdformat.log'))
        self.assertFalse(self.spool.exists())

    def test_historical_seven_path_schema_retains_its_original_verification_shape(self):
        self.value['schema'] = 1
        self.value['live_paths'].pop('sdformat_log')
        for path in (self.spool/'owner.json', self.control/'runtime_spool.json', self.source/storage.INITIAL):
            storage.write(path, self.value)
        storage.validate_descriptor(self.value, self.run_id)
        proof = self.publish()
        self.assertTrue(proof['passed'])
        self.assertNotIn('descriptor_schema', proof)
        self.assertEqual(storage.verify(self.raw), proof)

    def test_malformed_or_nonfinite_storage_metadata_returns_invalid_instead_of_crashing(self):
        self.publish()
        receipt = self.raw/storage.RECEIPT
        original = receipt.read_bytes()
        for malformed in ('[]', '{"mode": NaN}', '{"mode": Infinity}', '{"manifest": []}'):
            with self.subTest(malformed=malformed):
                receipt.write_text(malformed)
                self.assertFalse(storage.verify(self.raw)['passed'])
        receipt.write_bytes(original)

    def test_actual_container_closure_fails_closed_on_running_or_unreadable_docker(self):
        (self.spool/'container.id').write_text('a'*64)
        listed = subprocess.CompletedProcess([], 0, json.dumps(dict(ID='a'*64))+'\n', '')
        with patch.object(storage.subprocess, 'run', return_value=listed), \
             patch.object(storage.subprocess, 'check_output', return_value=json.dumps(dict(Running=True, Pid=123))):
            with self.assertRaisesRegex(ValueError, 'not stopped'):
                storage.container_closure(self.value)
        with patch.object(storage.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'docker')):
            with self.assertRaises(subprocess.CalledProcessError):
                storage.container_closure(self.value)

    def test_handover_rejects_descriptor_mismatch_and_external_hardlink_before_inode_mutation(self):
        owner_sha = storage.sha(self.spool/'owner.json')
        command = [sys.executable, '-c', storage.HANDOVER_PROGRAM, str(self.spool)]
        original = (self.spool/'owner.json').stat()
        rejected = subprocess.run(command+['0'*64], capture_output=True)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual((self.spool/'owner.json').stat().st_ctime_ns, original.st_ctime_ns)
        descriptor, filename = tempfile.mkstemp(prefix='bas-spool-outside-', dir='/dev/shm')
        os.close(descriptor)
        outside = Path(filename)
        self.addCleanup(lambda: outside.unlink() if outside.exists() else None)
        outside.write_bytes(b'outside private spool')
        outside.chmod(0o444)
        inside = self.spool/'unowned-hardlink'
        os.link(outside, inside)
        before = outside.stat()
        rejected = subprocess.run(command+[owner_sha], capture_output=True)
        self.assertNotEqual(rejected.returncode, 0)
        after = outside.stat()
        self.assertEqual((before.st_uid, before.st_gid, before.st_mode, before.st_ctime_ns),
                         (after.st_uid, after.st_gid, after.st_mode, after.st_ctime_ns))
        inside.unlink()
        external_link = outside.with_name(outside.name+'.link')
        external_link.symlink_to(outside)
        self.addCleanup(lambda: external_link.unlink() if external_link.is_symlink() else None)
        os.link(external_link, inside, follow_symlinks=False)
        before = external_link.lstat()
        rejected = subprocess.run(command+[owner_sha], capture_output=True)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(external_link.lstat().st_ctime_ns, before.st_ctime_ns)
        inside.unlink()
        inside.symlink_to(outside)
        before = outside.stat()
        allowed = subprocess.run(command+[owner_sha], capture_output=True)
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertEqual(outside.stat().st_ctime_ns, before.st_ctime_ns)
        with self.assertRaises(ValueError):
            storage.tree_members(self.spool)


if __name__ == '__main__':
    unittest.main()
