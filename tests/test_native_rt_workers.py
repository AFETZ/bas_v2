"""Real subprocess/AF_UNIX adapter tests using DEVELOPMENT-ONLY stub children.

No Sionna, GPU, radio model, serial hardware, or real-time acceptance is claimed.
"""
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
import queue
import copy

from scripts.product.native_rt_workers import NativeRtWorkerPool, validate_config, validate_job


STUB = r'''
import argparse,json,os,struct,time
from multiprocessing.connection import Client
p=argparse.ArgumentParser()
p.add_argument('--socket');p.add_argument('--config');p.add_argument('--worker-id',type=int)
a=p.parse_args();mode=json.load(open(a.config))['test_mode']
if mode=='never_connect':time.sleep(60)
c=Client(a.socket,family='AF_UNIX',authkey=None)
print('development stub stdout is separate from the result connection',flush=True)
c.send(dict(schema=1,kind='ready',worker_id=a.worker_id,pid=os.getpid(),development_stub=True))
while True:
 j=c.recv()
 if mode=='hang':time.sleep(60)
 if mode=='partial':
  c._send(struct.pack('!i',128)+b'x') # Deliberately incomplete standard Connection frame.
  time.sleep(60)
 if mode=='eof':break
 if mode=='error':c.send(dict(schema=1,kind='error',worker_id=a.worker_id,request_id=j['request_id'],error='injected failure'));break
 c.send(dict(schema=1,kind='result',worker_id=a.worker_id,
  request_id=j['request_id']+(1 if mode=='wrong_id' else 0),echo=j))
'''


def job(identity=1):
    now = time.monotonic_ns()
    pose = dict(position_m=[0., 0., 20.], velocity_m_s=[0., 0., 0.], orientation_rad=[0., 0., 0.], sample_host_ns=now)
    return dict(kind='solve', request_id=identity, sample_native_ns=identity*1_000_000,
        sample_host_monotonic_ns=now, anchor_index=0, poses=[dict(pose), dict(pose)])


def source_config(root):
    scene=root/'scene.xml'; scene.write_text('<scene/>')
    return dict(schema=1,root=str(root),scene=dict(path='scene.xml',
        sha256=hashlib.sha256(scene.read_bytes()).hexdigest()),normalize_delays=True,
        cohort=[{'id':'cp'}]+[{'id':'uav'+str(i)} for i in range(1,6)],
        radio=dict(frequency_hz=2.412e9,samples_per_source=20000,max_paths_per_source=1000,
            max_number_of_paths=90,merge_shapes=True,solver=dict(max_depth=2,seed=42,los=True,
                specular_reflection=True,diffuse_reflection=False,refraction=False,diffraction=True,
                edge_diffraction=True,synthetic_array=True)),
        source_profiles=[dict(id='cochannel_iso',frequency_hz=2.412e9,pattern='iso'),
                         dict(id='offset_directional',frequency_hz=2.422e9,pattern='tr38901')])


def source_job(peer=0,profile='cochannel_iso'):
    value=job(); value.update(profile_id=profile,peer_index=peer,anchor_index=6)
    value['poses']=[copy.deepcopy(value['poses'][0]) for _ in range(7)]
    return value


class SourceStarValidationTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.root=Path(self.directory.name)
        self.config=source_config(self.root)

    def tearDown(self):
        self.directory.cleanup()

    def test_registry_is_optional_and_freezes_exact_source_profiles(self):
        frozen=validate_config(self.config)
        self.assertEqual(frozen['source_profiles'],self.config['source_profiles'])
        self.config['source_profiles'][0]['frequency_hz']=2.5e9
        self.assertEqual(frozen['source_profiles'][0]['frequency_hz'],2.412e9)
        plain=copy.deepcopy(self.config); del plain['source_profiles']
        self.assertNotIn('source_profiles',validate_config(plain))

    def test_registry_rejects_unknown_fields_patterns_carriers_and_duplicate_ids(self):
        invalids=[None,{},[],[dict(id='x',frequency_hz=True,pattern='iso')],
                  [dict(id='x',frequency_hz=float('nan'),pattern='iso')],
                  [dict(id='x',frequency_hz=2.412e9,pattern='indoor')],
                  [dict(id='x',frequency_hz=2.412e9,pattern='tr38901',gain_dbi=8)],
                  [dict(id='x',frequency_hz=2.412e9,pattern='iso')]*2]
        for profiles in invalids:
            value=copy.deepcopy(self.config);value['source_profiles']=profiles
            with self.subTest(profiles=profiles),self.assertRaises(ValueError):validate_config(value)
        value=copy.deepcopy(self.config);value['cohort'][0]['id']='missing_cp'
        with self.assertRaisesRegex(ValueError,'CP|cp'):validate_config(value)

    def test_all_six_star_peers_include_cp_without_allowing_receiver_receiver_jobs(self):
        frozen=validate_config(self.config)
        for peer in range(6):validate_job(source_job(peer),6,frozen)
        for field,value in [('peer_index',6),('peer_index',True),('anchor_index',0),
                            ('profile_id','unknown'),('poses',source_job()['poses'][:6])]:
            item=source_job();item[field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):validate_job(item,6,frozen)
        for field in ('peer_index','profile_id'):
            item=source_job();del item[field]
            with self.subTest(missing=field),self.assertRaises(ValueError):validate_job(item,6,frozen)

    def test_sources_require_a_frozen_registry_and_preserve_the_desired_job_contract(self):
        with self.assertRaises(ValueError):validate_job(source_job(),6)
        desired=source_job();del desired['profile_id'];del desired['peer_index']
        desired['anchor_index']=0;desired['poses']=desired['poses'][:6]
        validate_job(desired,6,validate_config(self.config))
        desired['poses'].append(copy.deepcopy(desired['poses'][0]))
        with self.assertRaises(ValueError):validate_job(desired,6,validate_config(self.config))

    def test_source_rosters_one_to_five_uavs_and_source_pose_are_validated(self):
        for count in range(2,7):
            config=copy.deepcopy(self.config);config['cohort']=config['cohort'][:count]
            value=source_job();value['anchor_index']=count;value['peer_index']=0
            value['poses']=value['poses'][:count+1]
            validate_job(value,count,validate_config(config))
            value['poses'][-1]['orientation_rad']=[0,float('nan'),0]
            with self.subTest(uavs=count-1),self.assertRaises(ValueError):validate_job(value,count,validate_config(config))


class JobValidationTests(unittest.TestCase):
    def test_config_hash_normalization_and_solver_options_are_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scene = root/'scene.xml'; scene.write_text('<scene/>')
            value = dict(schema=1, root=str(root), scene=dict(path='scene.xml',
                sha256=hashlib.sha256(scene.read_bytes()).hexdigest()), normalize_delays=False,
                cohort=[{'id':'cp'}, {'id':'uav1'}], radio=dict(frequency_hz=2.412e9,
                    samples_per_source=20000, max_paths_per_source=1000, max_number_of_paths=90,
                    merge_shapes=True, solver=dict(max_depth=2, seed=42, los=True,
                        specular_reflection=True, diffuse_reflection=False, refraction=False,
                        diffraction=True, edge_diffraction=True, synthetic_array=True)))
            applied = validate_config(value)
            self.assertFalse(applied['normalize_delays'])
            self.assertEqual(applied['scene']['path'], str(scene.resolve()))
            for key in ('normalize_delays', 'scene', 'radio'):
                invalid = dict(value); del invalid[key]
                with self.subTest(key=key), self.assertRaises(ValueError):
                    validate_config(invalid)
            scene.write_text('changed geometry')
            with self.assertRaisesRegex(ValueError, 'SHA-256'):
                validate_config(value)

    def test_bounded_message_rejects_invalid_and_nonfinite_data(self):
        validate_job(job(), 2)
        for key, value in [('request_id', True), ('sample_native_ns', -1), ('anchor_index', 2), ('poses', [])]:
            item = job(); item[key] = value
            with self.subTest(field=key), self.assertRaises(ValueError):
                validate_job(item, 2)
        for invalid in (float('nan'), float('inf'), True, '1'):
            item = job(); item['poses'][0]['position_m'] = [invalid, 0, 1]
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                validate_job(item, 2)
        item = job(); item['poses'][0]['sample_host_ns'] += 1
        with self.assertRaisesRegex(ValueError, 'later'):
            validate_job(item, 2)
        item = job(); item['ignored'] = 123
        with self.assertRaises(ValueError):
            validate_job(item, 2)

    def test_poll_does_not_call_connection_methods_and_rejects_mismatched_identity(self):
        class UnreadableConnection:
            def __getattr__(self, key):
                raise AssertionError(f'poll must not touch the connection: {key}')
        pool = object.__new__(NativeRtWorkerPool)
        worker = SimpleNamespace(failed=None, pending=job(), submitted_ns=time.monotonic_ns(),
            completed=0, connection=UnreadableConnection())
        pool._workers, pool._messages, pool.job_timeout_ns = {0:worker}, queue.Queue(2), 10**9
        for _ in range(100):
            self.assertEqual(pool.poll(), [])
        pool._messages.put((0, dict(schema=1, kind='result', worker_id=0, request_id=2)))
        response = pool.poll()[0]
        self.assertEqual((response['kind'], response['request_id']), ('error', 1))
        self.assertIn('uncorrelated', response['error'])
        self.assertIsNone(worker.pending)

    def test_poll_reports_timeout_once_without_io_or_automatic_retry(self):
        pool = object.__new__(NativeRtWorkerPool)
        worker = SimpleNamespace(failed=None, pending=job(), submitted_ns=0, completed=0)
        pool._workers, pool._messages, pool.job_timeout_ns = {0:worker}, queue.Queue(2), 1
        self.assertIn('timeout', pool.poll()[0]['error'])
        self.assertEqual(pool.poll(), [])

    def test_scalar_request_watermark_rejects_duplicate_and_out_of_order_ids(self):
        class Connection:
            def __init__(self):
                self.sent = []
            def send(self, value):
                self.sent.append(value)
        pool = object.__new__(NativeRtWorkerPool)
        connection = Connection()
        worker = SimpleNamespace(failed=None, pending=None, connection=connection, submitted_ns=0, completed=0)
        pool._workers, pool._messages, pool.job_timeout_ns = {0:worker}, queue.Queue(2), 10**9
        pool._started, pool._closing, pool._last_request_id, pool.count = True, threading.Event(), 0, 2
        first = job(2); first['sample_native_ns'] = 0
        self.assertTrue(pool.submit(0, first))
        pool._messages.put((0, dict(schema=1, kind='result', worker_id=0, request_id=2)))
        self.assertEqual(pool.poll()[0]['request_id'], 2)
        for identity in (2, 1):
            with self.subTest(identity=identity), self.assertRaisesRegex(ValueError, 'increasing'):
                pool.submit(0, job(identity))
        second = job(3); second['sample_native_ns'] = 0
        self.assertTrue(pool.submit(0, second))
        self.assertEqual(pool._last_request_id, 3)
        self.assertEqual([row['request_id'] for row in connection.sent], [2, 3])
        self.assertFalse(hasattr(pool, '_request_ids'))


@unittest.skipUnless(os.name == 'posix', 'AF_UNIX production path requires POSIX; run under local WSL/Linux')
class PoolProcessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='bas-rt-test-')
        self.root = Path(self.temporary.name)
        self.stub = self.root / 'stub.py'
        self.stub.write_text(STUB, encoding='utf-8')
        self.pool = None

    def tearDown(self):
        if self.pool:
            self.pool.close()
        self.temporary.cleanup()

    def create(self, mode='normal', workers=1, **options):
        config = self.root / 'config.json'
        config.write_text(json.dumps(dict(schema=1, cohort=[{'id':'cp'}, {'id':'uav1'}], test_mode=mode)))
        self.pool = NativeRtWorkerPool([sys.executable, '-u', str(self.stub)], config,
            self.root / 'artifacts', workers, startup_timeout_s=options.pop('startup_timeout_s', 3.),
            job_timeout_s=options.pop('job_timeout_s', 1.), allow_development_stub=True, **options)
        return self.pool.start()

    def completed(self, timeout=2.):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            replies = self.pool.poll()
            if replies:
                return replies
            time.sleep(.005)
        self.fail('no completion or explicit failure arrived')

    def test_three_real_children_bound_inflight_and_preserve_identity(self):
        pool = self.create(workers=3)
        self.assertEqual(pool.socket_dir.stat().st_mode & 0o777, 0o700)
        for index in range(3):
            self.assertTrue(pool.submit(index, job(2*index+1)))
            self.assertFalse(pool.submit(index, job(index+10)))
        rows = []
        deadline = time.monotonic()+2
        while len(rows) < 3 and time.monotonic() < deadline:
            rows.extend(pool.poll()); time.sleep(.005)
        self.assertEqual({row['request_id'] for row in rows}, {1, 3, 5})
        self.assertTrue(all(row['kind']=='result' and row['echo']['request_id']==row['request_id'] for row in rows))
        with self.assertRaisesRegex(ValueError, 'increasing'):
            pool.submit(0, job(1))
        with self.assertRaisesRegex(ValueError, 'increasing'):
            pool.submit(1, job(4)) # Previously unused, but behind the watermark.
        self.assertEqual(pool.snapshot()['last_request_id'], 5)
        self.assertTrue(pool.submit(0, job(6)))
        self.assertEqual(self.completed()[0]['request_id'], 6)

    def test_poll_never_reads_partial_frames_or_waits_for_hanging_worker(self):
        for mode in ('partial', 'hang'):
            with self.subTest(mode=mode):
                pool = self.create(mode, job_timeout_s=.15)
                pool.submit(0, job())
                started = time.monotonic()
                for _ in range(100):
                    self.assertEqual(pool.poll(), [])
                self.assertLess(time.monotonic()-started, .1, 'poll waited for an incomplete worker reply')
                failure = self.completed()[0]
                self.assertEqual(failure['kind'], 'error')
                self.assertIn('timeout', failure['error'])
                with self.assertRaises(RuntimeError):
                    pool.submit(0, job(2))
                pool.close(); self.pool = None
                (self.root/'config.json').unlink()
                # The previous attempt's logs remain immutable.
                self.root = self.root / mode; self.root.mkdir()
                self.stub = self.root / 'stub.py'; self.stub.write_text(STUB, encoding='utf-8')

    def test_wrong_request_error_and_eof_are_explicit_and_never_retried(self):
        for mode, expected in [('wrong_id', 'uncorrelated'), ('error', 'injected failure'), ('eof', 'transport_error')]:
            with self.subTest(mode=mode):
                pool = self.create(mode)
                pool.submit(0, job())
                reply = self.completed()[0]
                self.assertEqual((reply['kind'], reply['request_id']), ('error', 1))
                self.assertIn(expected, reply['error'])
                self.assertIsNotNone(pool.snapshot()['workers'][0]['failed'])
                pool.close(); self.pool = None
                self.root = self.root / mode; self.root.mkdir()
                self.stub = self.root/'stub.py'; self.stub.write_text(STUB, encoding='utf-8')

    def test_startup_timeout_closes_listener_accept_thread_and_owned_child(self):
        with self.assertRaises(TimeoutError):
            self.create('never_connect', startup_timeout_s=.15)
        self.assertFalse(self.pool.socket_dir.exists())
        self.assertTrue(all(w.process.poll() is not None and not w.thread.is_alive() for w in self.pool._workers.values()))

    def test_close_preserves_logs_and_does_not_touch_unrelated_child(self):
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'])
        try:
            pool = self.create('partial'); pool.submit(0, job())
            socket_dir = pool.socket_dir
            pool.close(); pool.close()
            self.assertFalse(socket_dir.exists())
            self.assertTrue(all(w.process.poll() is not None and not w.thread.is_alive() for w in pool._workers.values()))
            self.assertIsNone(unrelated.poll())
            self.assertIn('development stub', (pool.artifacts/'worker-0.log').read_text())
        finally:
            unrelated.terminate(); unrelated.wait(timeout=2)


if __name__ == '__main__':
    unittest.main()
