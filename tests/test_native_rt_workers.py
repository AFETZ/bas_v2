"""Real subprocess/AF_UNIX adapter tests using DEVELOPMENT-ONLY stub children.

No Sionna, GPU, radio model, serial hardware, or real-time acceptance is claimed.
"""
import json
import hashlib
import gc
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import queue
import copy

from scripts.product.native_rt_workers import (NativeRtWorkerPool, validate_config, validate_job,
    validate_path_storage_stats, PATH_STORAGE_SOURCE_HASHES, _GcObserver, NativeResultLog)


STUB = r'''
import argparse,hashlib,json,os,struct,time
from multiprocessing.connection import Client
p=argparse.ArgumentParser()
p.add_argument('--socket');p.add_argument('--config');p.add_argument('--worker-id',type=int)
a=p.parse_args();mode=json.load(open(a.config))['test_mode']
if mode=='never_connect':time.sleep(60)
c=Client(a.socket,family='AF_UNIX',authkey=None)
print('development stub stdout is separate from the result connection',flush=True)
trace=mode.startswith('stage_')
c.send(dict(schema=1,kind='ready',worker_id=a.worker_id,pid=os.getpid(),development_stub=True,job_stage_trace_enabled=trace))
while True:
 j=c.recv()
 if trace:
  encode=lambda x:json.dumps(x,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
  fingerprints=dict(job_sha256=hashlib.sha256(encode(j)).hexdigest(),
                    geometry_sha256=hashlib.sha256(encode(j['poses'])).hexdigest())
  def stage(label):
   c.send(dict(schema=1,kind='progress',worker_id=a.worker_id,request_id=j['request_id'],stage=label,
               host_monotonic_ns=time.monotonic_ns(),thread_cpu_ns=time.thread_time_ns(),**fingerprints))
  if mode=='stage_wrong_hash':fingerprints['geometry_sha256']='0'*64
  stage('job_received')
  stage('solver_call_end' if mode=='stage_bad_order' else 'preparation_done')
  stage('solver_call_start')
  if mode=='stage_hang':time.sleep(60)
  if mode=='stage_partial':
   c._send(struct.pack('!i',128)+b'x');time.sleep(60)
  if mode=='stage_delayed':time.sleep(.08)
  stage('solver_call_end');stage('result_send_start')
 if mode=='hang':time.sleep(60)
 if mode=='partial':
  c._send(struct.pack('!i',128)+b'x') # Deliberately incomplete standard Connection frame.
  time.sleep(60)
 if mode=='eof':break
 if mode=='error':c.send(dict(schema=1,kind='error',worker_id=a.worker_id,request_id=j['request_id'],error='injected failure'));break
 c.send(dict(schema=1,kind='result',worker_id=a.worker_id,
  request_id=j['request_id']+(1 if mode=='wrong_id' else 0),echo=j))
 if trace:stage('result_send_end')
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


def emitter_job():
    value=source_job();del value['peer_index'];value['source_mode']='emitter_batch'
    return value


def storage_stats(*, solved=False, empty=False, calls=1):
    return dict(enabled=True,capacity=90,source_hashes_verified=solved,
        sionna_version='1.2.0' if solved else '',drjit_version='1.2.0' if solved else '',
        **(PATH_STORAGE_SOURCE_HASHES if solved else {key:'' for key in PATH_STORAGE_SOURCE_HASHES}),
        attempted_calls=calls if solved else 0,completed_calls=calls if solved else 0,
        export_calls=calls if solved else 0,verified_trim_calls=calls if solved else 0,
        official_empty_calls=1 if solved and empty else 0,original_axis=0 if empty or not solved else 17,
        storage_axis=0 if empty or not solved else 90,exported_axis=0 if empty or not solved else 17,
        verified_trim=solved,official_empty=empty if solved else False)


def emitter_reply(value,config):
    """Transport validation fixture only: it does not simulate an RF channel."""
    count=len(config['cohort']);radio=config['radio'];cap=radio['max_paths_per_source']*count
    profile=next(item for item in config['source_profiles'] if item['id']==value['profile_id'])
    pairs=[]
    for peer in range(count):
        pairs.append(dict(pair=[count,peer],direction=[count,peer],coefficient_shape=[1,1,1],
            generated_ns=value['sample_native_ns'],sample_native_ns=value['sample_native_ns'],
            source_request_id=value['request_id'],sample_host_monotonic_ns=value['sample_host_monotonic_ns'],
            poses=copy.deepcopy(value['poses']),fresh_for_request=True))
    return dict(schema=1,kind='result',worker_id=0,request_id=value['request_id'],
        path_storage=storage_stats(solved=True),
        channel_mode='source_star',source_mode='emitter_batch',profile_id=value['profile_id'],
        frequency_hz=profile['frequency_hz'],new_emitter_batch_calls=1,pairs=pairs,
        allocated_candidate_cap=cap,candidate_count=count,candidate_buffer_size=cap,
        candidate_count_scope='actual_public_emitter_batch_api',
        emitter_batch_stats=dict(source_mode='emitter_batch',pattern=profile['pattern'],bound=True,
            has_published_batch=True,candidate_allocation_valid=True,frequency_hz=float(profile['frequency_hz']),
            registered_receivers=count,configured_samples_per_source=radio['samples_per_source'],
            configured_max_paths_per_source=radio['max_paths_per_source'],
            configured_max_number_of_paths=float(radio['max_number_of_paths']),configured_candidate_cap=cap,
            last_transmitter_count=1,last_receiver_count=count,last_allocated_candidate_cap=cap,
            last_candidate_count=count,last_candidate_buffer_size=cap,candidate_capacity_reached_calls=0,
            last_generated_ns=value['sample_native_ns'],attempted_calls=1,completed_calls=1,generated_links=count,
            cache_read_hits=count,cache_read_misses=0,source_node_id=100,source_antenna_id=200,
            receiver_node_ids=list(range(count)),receiver_antenna_ids=list(range(count)),
            last_source_position_m=value['poses'][count]['position_m'],
            last_source_velocity_m_s=value['poses'][count]['velocity_m_s'],
            last_source_orientation_rad=value['poses'][count]['orientation_rad'],
            last_retained_paths_per_receiver=[1]*count))


class SourceStarValidationTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.root=Path(self.directory.name)
        self.config=source_config(self.root)

    def tearDown(self):
        self.directory.cleanup()

    def test_fixed_storage_default_is_explicit_in_frozen_config_and_cannot_be_disabled(self):
        self.assertNotIn('fixed_path_storage',self.config)
        self.assertIs(validate_config(self.config)['fixed_path_storage'],True)
        self.assertNotIn('fixed_path_storage',self.config)
        for value in (False,None,1,0,'true',{}):
            with self.subTest(value=value),self.assertRaises(ValueError):validate_config(self.config|{'fixed_path_storage':value})
        bad=copy.deepcopy(self.config);bad['radio']['max_number_of_paths']=1.5
        with self.assertRaisesRegex(ValueError,'integer'):validate_config(bad)
        integral=copy.deepcopy(self.config);integral['radio']['max_number_of_paths']=90.0
        self.assertIs(validate_config(integral)['fixed_path_storage'],True)

    def test_storage_proof_is_pinned_typed_preserves_original_axis_and_labels_empty(self):
        validate_path_storage_stats(storage_stats(),90)
        with self.assertRaises(ValueError):validate_path_storage_stats(storage_stats(),90,require_solve=True)
        validate_path_storage_stats(storage_stats(solved=True,calls=3),90.0,require_solve=True)
        validate_path_storage_stats(storage_stats(solved=True,empty=True,calls=3),90,require_solve=True)
        for key,value in [('enabled',1),('capacity',True),('source_hashes_verified',False),
                ('paths_source_sha256','0'*64),('path_solver_source_sha256','f'*64),('sionna_version','1.1.0'),
                ('drjit_version','1.3.0'),('exported_axis',90),('storage_axis',17),('original_axis',91),
                ('verified_trim',False),('verified_trim_calls',2),('completed_calls',True),('official_empty',True)]:
            item=storage_stats(solved=True,calls=3);item[key]=value
            with self.subTest(key=key,value=value),self.assertRaises(ValueError):validate_path_storage_stats(item,90,require_solve=True)
        item=storage_stats(solved=True,empty=True);item['official_empty']=False
        with self.assertRaises(ValueError):validate_path_storage_stats(item,90,require_solve=True)
        item=storage_stats();item['paths_source_sha256']=PATH_STORAGE_SOURCE_HASHES['paths_source_sha256']
        with self.assertRaises(ValueError):validate_path_storage_stats(item,90)

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

    def test_emitter_batch_is_explicit_and_includes_the_entire_cp_uav_roster(self):
        for count in range(2,7):
            config=copy.deepcopy(self.config);config['cohort']=config['cohort'][:count]
            value=emitter_job();value['anchor_index']=count;value['poses']=value['poses'][:count+1]
            validate_job(value,count,validate_config(config))
        for mode in (True,None,'pairwise','full_mesh','all'):
            value=emitter_job();value['source_mode']=mode
            with self.subTest(mode=mode),self.assertRaises(ValueError):validate_job(value,6,validate_config(self.config))
        value=emitter_job();value['peer_index']=0
        with self.assertRaises(ValueError):validate_job(value,6,validate_config(self.config))
        value=emitter_job();del value['profile_id']
        with self.assertRaises(ValueError):validate_job(value,6,validate_config(self.config))
        validate_job(source_job(),6,validate_config(self.config))

    def test_emitter_capacity_guard_does_not_break_legacy_pairwise_configs(self):
        config=copy.deepcopy(self.config);config['radio']['max_paths_per_source']=800_000_000
        applied=validate_config(config) #5*800M is valid;6*800M would overflow.
        validate_job(source_job(),6,applied)
        with self.assertRaisesRegex(ValueError,'emitter-batch.*uint32'):validate_job(emitter_job(),6,applied)

    def test_emitter_results_require_real_sdk_proof_original_epochs_and_full_coverage(self):
        value=emitter_job();config=validate_config(self.config)
        def receive(message):
            pool=object.__new__(NativeRtWorkerPool);pool.stage_trace_enabled=False
            worker=SimpleNamespace(failed=None,pending=copy.deepcopy(value),submitted_ns=time.monotonic_ns(),
                completed=0,emitter_batch_completed_jobs=0,last_emitter_batch_stats=None)
            pool._workers,pool._messages,pool.job_timeout_ns={0:worker},queue.Queue(2),10**9
            pool.count,pool.applied_config=6,config
            pool._messages.put((0,message,time.monotonic_ns()))
            return pool.poll()[0],worker
        response,worker=receive(emitter_reply(value,config))
        self.assertEqual((response['kind'],worker.completed,worker.emitter_batch_completed_jobs),('result',1,1))
        response['emitter_batch_stats']['completed_calls']=999
        self.assertEqual(worker.last_emitter_batch_stats['completed_calls'],1)
        self.assertEqual(worker.last_path_storage_stats['original_axis'],17)
        response['path_storage']['original_axis']=89
        self.assertEqual(worker.last_path_storage_stats['original_axis'],17)
        mutations=[lambda r:r.pop('emitter_batch_stats'),
            lambda r:r.update(new_emitter_batch_calls=True),lambda r:r.update(new_pairwise_calls=1),
            lambda r:r['emitter_batch_stats'].update(last_candidate_count=6000),
            lambda r:r['emitter_batch_stats'].update(frequency_hz=2.422e9),
            lambda r:r['emitter_batch_stats'].update(cache_read_misses=1),
            lambda r:r['emitter_batch_stats'].update(receiver_node_ids=[0]*6),
            lambda r:r['emitter_batch_stats'].update(last_source_position_m=[1,2,3]),
            lambda r:r['pairs'].pop(0),lambda r:r['pairs'][0].update(pair=[0,1]),
            lambda r:r['pairs'][5].update(generated_ns=value['sample_native_ns']+1),
            lambda r:r['pairs'][0].update(sample_host_monotonic_ns=value['sample_host_monotonic_ns']+1),
            lambda r:r['pairs'][0].update(source_request_id=999)]
        for mutate in mutations:
            reply=emitter_reply(value,config);mutate(reply)
            with self.subTest(mutation=mutate):
                response,worker=receive(reply)
                self.assertEqual((response['kind'],worker.completed,worker.emitter_batch_completed_jobs),('error',0,0))
                self.assertIn('emitter-batch producer proof',response['error'])
        bad=emitter_reply(value,config);bad['path_storage']['verified_trim']=False
        response,worker=receive(bad)
        self.assertEqual(response['kind'],'error');self.assertIn('path_storage',response['error'])

    def test_source_rosters_one_to_five_uavs_and_source_pose_are_validated(self):
        for count in range(2,7):
            config=copy.deepcopy(self.config);config['cohort']=config['cohort'][:count]
            value=source_job();value['anchor_index']=count;value['peer_index']=0
            value['poses']=value['poses'][:count+1]
            validate_job(value,count,validate_config(config))
            value['poses'][-1]['orientation_rad']=[0,float('nan'),0]
            with self.subTest(uavs=count-1),self.assertRaises(ValueError):validate_job(value,count,validate_config(config))

    def test_full_mesh_is_explicit_and_cannot_be_served_as_an_anchor_batch(self):
        config=copy.deepcopy(self.config);config['desired_mode']='full_mesh'
        for count in range(2,7):
            config['cohort']=self.config['cohort'][:count]
            applied=validate_config(config)
            value=job();value['poses']=[copy.deepcopy(value['poses'][0]) for _ in range(count)]
            value['channel_mode']='full_mesh'
            validate_job(value,count,applied)
            missing=copy.deepcopy(value);del missing['channel_mode']
            with self.assertRaises(ValueError):validate_job(missing,count,applied)
            wrong=copy.deepcopy(value);wrong['anchor_index']=1
            with self.assertRaises(ValueError):validate_job(wrong,count,applied)
            legacy=copy.deepcopy(applied);del legacy['desired_mode']
            with self.assertRaises(ValueError):validate_job(value,count,legacy)
        validate_job(source_job(),6,validate_config(self.config | {'desired_mode':'full_mesh'}))

    def test_full_mesh_rejects_unknown_modes_and_total_native_buffer_overflow(self):
        for mode in (True,None,{},'all','FullMesh'):
            with self.subTest(mode=mode),self.assertRaises(ValueError):
                validate_config(self.config | {'desired_mode':mode})
        for field,value in [('samples_per_source',2**31),('max_paths_per_source',200_000_000)]:
            config=copy.deepcopy(self.config);config['desired_mode']='full_mesh';config['radio'][field]=value
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,'full.mesh|capacity|rays'):
                validate_config(config)


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
        pool = object.__new__(NativeRtWorkerPool); pool.stage_trace_enabled = False
        worker = SimpleNamespace(failed=None, pending=job(), submitted_ns=time.monotonic_ns(),
            completed=0, connection=UnreadableConnection())
        pool._workers, pool._messages, pool.job_timeout_ns = {0:worker}, queue.Queue(2), 10**9
        for _ in range(100):
            self.assertEqual(pool.poll(), [])
        pool._messages.put((0, dict(schema=1, kind='result', worker_id=0, request_id=2),time.monotonic_ns()))
        response = pool.poll()[0]
        self.assertEqual((response['kind'], response['request_id']), ('error', 1))
        self.assertIn('uncorrelated', response['error'])
        self.assertIsNone(worker.pending)

    def test_poll_reports_timeout_once_without_io_or_automatic_retry(self):
        pool = object.__new__(NativeRtWorkerPool); pool.stage_trace_enabled = False
        worker = SimpleNamespace(failed=None, pending=job(), submitted_ns=0, completed=0)
        pool._workers, pool._messages, pool.job_timeout_ns = {0:worker}, queue.Queue(2), 1
        self.assertIn('timeout', pool.poll()[0]['error'])
        self.assertEqual(pool.poll(), [])

    def test_receiver_receipt_precedes_delayed_poll_and_remains_unvalidated_transport(self):
        incoming=queue.Queue()
        class Connection:
            def recv(self):
                value=incoming.get(timeout=2)
                if isinstance(value,Exception):raise value
                return value
        pool=object.__new__(NativeRtWorkerPool);pool.stage_trace_enabled=False;connection=Connection();pending=job()
        worker=SimpleNamespace(identity=0,listener=SimpleNamespace(accept=lambda:connection),
            process=SimpleNamespace(pid=4321),ready=None,failed=None,pending=pending,
            submitted_ns=time.monotonic_ns(),completed=0,full_mesh_completed_jobs=0,last_full_mesh_stats=None,
            emitter_batch_completed_jobs=0,last_emitter_batch_stats=None,last_transport_receive=None,
            stage_requests={},stage_records=[],stage_records_overwritten=0)
        pool._workers,pool._messages,pool.job_timeout_ns={0:worker},queue.Queue(2),10**9
        pool._closing,pool.socket_dir,pool._last_request_id=threading.Event(),None,1
        pool.allow_development_stub=True
        pool._gc_observer=_GcObserver()
        thread=threading.Thread(target=pool._receive,args=(worker,))
        def queued():
            deadline=time.monotonic()+1
            while pool._messages.empty():
                if time.monotonic()>deadline:self.fail('Receiver did not enqueue a complete fixture frame')
                time.sleep(.001)
        thread.start()
        try:
            original_host=pending['sample_host_monotonic_ns']
            incoming.put(dict(schema=1,kind='result',worker_id=0,request_id=1,
                sample_host_monotonic_ns=original_host,receiver_enqueued_host_monotonic_ns=1))
            queued();state=pool.snapshot()['workers'][0]
            receipt=state['last_transport_receive']
            self.assertEqual((receipt['kind'],receipt['request_id'],state['busy'],state['completed']),('result',1,True,0))
            self.assertIsNone(state['failed'])
            time.sleep(.02) # Deliberately leave the full response queued before poll.
            before_poll=time.monotonic_ns();response=pool.poll()[0]
            self.assertLess(receipt['host_monotonic_ns'],before_poll)
            self.assertEqual(response['receiver_enqueued_host_monotonic_ns'],receipt['host_monotonic_ns'])
            self.assertGreaterEqual(response['received_host_monotonic_ns'],before_poll)
            self.assertEqual(response['sample_host_monotonic_ns'],original_host)
            # A later receipt with a wrong ID stays unvalidated until poll rejects it.
            worker.pending=job(2);worker.submitted_ns=time.monotonic_ns()
            incoming.put(dict(schema=1,kind='result',worker_id=0,request_id=999))
            queued();state=pool.snapshot()['workers'][0]
            self.assertEqual(state['last_transport_receive']['request_id'],999)
            self.assertEqual(state['completed'],1);self.assertIsNone(state['failed'])
            self.assertEqual(pool.poll()[0]['kind'],'error')
        finally:
            pool._closing.set();incoming.put(EOFError('fixture stop'));thread.join(timeout=1)
            self.assertFalse(thread.is_alive())

    def test_full_mesh_reply_proof_is_correlated_and_frozen_per_producer(self):
        pool=object.__new__(NativeRtWorkerPool);pool.stage_trace_enabled=False
        pending=job();pending['channel_mode']='full_mesh'
        worker=SimpleNamespace(failed=None,pending=pending,submitted_ns=time.monotonic_ns(),completed=0,
                               full_mesh_completed_jobs=0,last_full_mesh_stats=None)
        pool._workers,pool._messages,pool.job_timeout_ns={0:worker},queue.Queue(2),10**9
        stats=dict(source_mode='full_mesh',completed_calls=1)
        pool._messages.put((0,dict(schema=1,kind='result',worker_id=0,request_id=1,
                                  channel_mode='full_mesh',new_full_mesh_calls=1,full_mesh_stats=stats),time.monotonic_ns()))
        result=pool.poll()
        self.assertEqual((worker.completed,worker.full_mesh_completed_jobs),(1,1))
        result[0]['full_mesh_stats']['completed_calls']=999
        self.assertEqual(worker.last_full_mesh_stats['completed_calls'],1)
        worker.pending=job(2);worker.pending['channel_mode']='full_mesh'
        pool._messages.put((0,dict(schema=1,kind='result',worker_id=0,request_id=2,new_batch_calls=1),time.monotonic_ns()))
        self.assertEqual(pool.poll()[0]['kind'],'error')
        self.assertEqual((worker.completed,worker.full_mesh_completed_jobs),(1,1))

    def test_scalar_request_watermark_rejects_duplicate_and_out_of_order_ids(self):
        class Connection:
            def __init__(self):
                self.sent = []
            def send(self, value):
                self.sent.append(value)
        pool = object.__new__(NativeRtWorkerPool); pool.stage_trace_enabled = False
        connection = Connection()
        worker = SimpleNamespace(failed=None, pending=None, connection=connection, submitted_ns=0, completed=0)
        pool._workers, pool._messages, pool.job_timeout_ns = {0:worker}, queue.Queue(2), 10**9
        pool._started, pool._closing, pool._last_request_id, pool.count = True, threading.Event(), 0, 2
        first = job(2); first['sample_native_ns'] = 0
        self.assertTrue(pool.submit(0, first))
        pool._messages.put((0, dict(schema=1, kind='result', worker_id=0, request_id=2),time.monotonic_ns()))
        self.assertEqual(pool.poll()[0]['request_id'], 2)
        for identity in (2, 1):
            with self.subTest(identity=identity), self.assertRaisesRegex(ValueError, 'increasing'):
                pool.submit(0, job(identity))
        second = job(3); second['sample_native_ns'] = 0
        self.assertTrue(pool.submit(0, second))
        self.assertEqual(pool._last_request_id, 3)
        self.assertEqual([row['request_id'] for row in connection.sent], [2, 3])
        self.assertFalse(hasattr(pool, '_request_ids'))


class NativeResultLogTests(unittest.TestCase):
    class StreamProbe:
        """Real temporary file behind deterministic event-controlled I/O faults."""
        def __init__(self, stream, failure=None):
            self.stream, self.failure = stream, failure
            self.entered, self.release = threading.Event(), threading.Event()

        def write(self, value):
            self.entered.set()
            if not self.release.wait(3):
                raise OSError('test writer gate was not released')
            if self.failure == 'write':
                raise OSError('injected file write failure')
            return self.stream.write(value)

        def flush(self):
            if self.failure == 'flush':
                raise OSError('injected file flush failure')
            return self.stream.flush()

        def close(self):
            self.stream.close()

        def __getattr__(self, name):
            return getattr(self.stream, name)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.logs, self.probes = [], []
        self.addCleanup(self.close_logs)

    def close_logs(self):
        for probe in self.probes:
            probe.release.set()
        for log in self.logs:
            try:
                log.close()
            except RuntimeError:
                pass  # Failure is asserted by each causal test, never a passing close.
            self.assertFalse(log.snapshot()['listener_alive'], 'Owned listener leaked from a test')

    def log(self, **options):
        log = NativeResultLog(self.root/f'results-{len(self.logs)}.jsonl', **options)
        self.logs.append(log)
        return log

    def gate(self, log, failure=None):
        probe = self.StreamProbe(log._file_handler.stream, failure)
        log._file_handler.stream = probe
        self.probes.append(probe)
        return probe

    def test_slow_file_io_does_not_block_enqueue_and_all_original_bytes_drain(self):
        before_handlers, before_names = list(logging.root.handlers), set(logging.Logger.manager.loggerDict)
        log = self.log(capacity=8)
        probe = self.gate(log)
        records = ['{"id":0,"text":"я"}', '{"id":1}', '{"id":2}']
        log.write(records[0]); self.assertTrue(probe.entered.wait(1))
        # The writer is still blocked. These calls must enqueue without entering it.
        for record in records[1:]:
            log.write(record)
        self.assertEqual(log.snapshot()['accepted'], 3)
        self.assertEqual(log.snapshot()['written'], 0)
        self.assertFalse(probe.release.is_set())
        probe.release.set(); log.close(); log.close(); log.check()
        self.assertEqual(Path(log.path).read_bytes(), ''.join(row+'\n' for row in records).encode('utf-8'))
        state = log.snapshot()
        self.assertEqual(state['sha256'], hashlib.sha256(Path(log.path).read_bytes()).hexdigest())
        self.assertEqual((state['accepted'], state['written'], state['failed'], state['error_count']), (3,3,0,0))
        self.assertTrue(state['closed'] and state['drained'])
        self.assertFalse(state['listener_alive'])
        self.assertEqual(state['producer']['pid'], os.getpid())
        self.assertEqual(state['writer']['pid'], os.getpid())
        self.assertNotEqual(state['producer']['native_thread_id'], state['writer']['native_thread_id'])
        self.assertGreaterEqual(state['maximum_write_wall_ns'], 0)
        self.assertGreaterEqual(state['maximum_write_thread_cpu_ns'], 0)
        self.assertEqual(logging.root.handlers, before_handlers)
        self.assertEqual(set(logging.Logger.manager.loggerDict), before_names)
        # Expected digest comes from the producer's completed writes, not a later
        # re-read that would silently bless a same-length external modification.
        original = Path(log.path).read_bytes()
        Path(log.path).write_bytes(original.replace(b'"id":1', b'"id":9', 1))
        self.assertEqual(log.snapshot()['sha256'], state['sha256'])
        self.assertNotEqual(log.snapshot()['sha256'], hashlib.sha256(Path(log.path).read_bytes()).hexdigest())

    def test_full_queue_is_explicit_even_if_logging_normally_hides_errors(self):
        log = self.log(capacity=2); probe = self.gate(log)
        records = ['{"id":%d}' % value for value in range(4)]
        with patch.object(logging, 'raiseExceptions', False):
            log.write(records[0]); self.assertTrue(probe.entered.wait(1))
            log.write(records[1]); log.write(records[2])
            with self.assertRaisesRegex(RuntimeError, 'not accepted'):
                log.write(records[3])
        state = log.snapshot()
        self.assertEqual((state['accepted'],state['rejected'],state['max_queue']), (3,1,2))
        self.assertEqual(state['first_exception']['stage'], 'queue_full')
        probe.release.set()
        with self.assertRaises(RuntimeError):log.close()
        self.assertEqual(Path(log.path).read_bytes(), ''.join(row+'\n' for row in records[:3]).encode())
        state = log.snapshot()
        self.assertEqual((state['accepted'],state['written']), (3,3))
        self.assertEqual(state['rejected_record'], records[3])
        self.assertFalse(state['listener_alive'])
        with self.assertRaises(RuntimeError):log.check()

    def test_write_and_flush_failures_retain_unwritten_records_and_never_claim_success(self):
        for failure in ('write','flush'):
            with self.subTest(failure=failure), patch.object(logging, 'raiseExceptions', False):
                log = self.log(capacity=4); probe = self.gate(log, failure)
                records = ['{"id":%d}' % value for value in range(3)]
                log.write(records[0]); self.assertTrue(probe.entered.wait(1))
                log.write(records[1]); log.write(records[2]); probe.release.set()
                with self.assertRaises(RuntimeError):log.close()
                with self.assertRaises(RuntimeError):log.check()
                state = log.snapshot()
                self.assertEqual((state['accepted'],state['written'],state['failed']), (3,0,3))
                self.assertEqual(state['unwritten_records'], records)
                self.assertEqual(state['sha256'], hashlib.sha256(b'').hexdigest())
                self.assertEqual(state['first_exception']['stage'], 'file_write_or_flush')
                self.assertIn('injected file', state['first_exception']['message'])
                self.assertTrue(state['closed'] and state['drained'])
                self.assertFalse(state['listener_alive'])
                self.assertEqual(Path(log.path).read_bytes(), b'' if failure == 'write' else (records[0]+'\n').encode())

    def test_finite_close_handles_full_sentinel_and_join_timeouts_without_false_success(self):
        for fill in (0,2):
            with self.subTest(queued=fill):
                log = self.log(capacity=2); probe = self.gate(log)
                records = ['{"id":%d}' % value for value in range(fill+1)]
                log.write(records[0]); self.assertTrue(probe.entered.wait(1))
                for record in records[1:]:log.write(record)
                with self.assertRaisesRegex(RuntimeError, 'did not close'):log.close(timeout_s=.01)
                state = log.snapshot()
                self.assertFalse(state['closed'] or state['drained'])
                self.assertTrue(state['listener_alive'])
                with self.assertRaises(RuntimeError):log.write('{"late":true}')
                probe.release.set()
                with self.assertRaises(RuntimeError):log.close()
                state = log.snapshot()
                self.assertFalse(state['listener_alive'])
                self.assertTrue(state['closed'] and state['drained'])
                self.assertEqual((state['accepted'],state['written']), (fill+1,fill+1))
                self.assertEqual(log._queue.unfinished_tasks, 0)
                self.assertEqual(Path(log.path).read_bytes(), ''.join(row+'\n' for row in records).encode())
                self.assertGreater(state['error_count'], 0)  # Retry cleanup cannot erase a timeout failure.

    def test_encoded_utf8_limit_includes_the_single_unchanged_line_terminator(self):
        record = '{"text":"я"}'
        limit = len(record.encode('utf-8'))+1
        log = self.log(max_record_bytes=limit)
        log.write(record); log.close()
        self.assertEqual(Path(log.path).read_bytes(), (record+'\n').encode('utf-8'))
        self.assertEqual(log.snapshot()['written_bytes'], limit)
        self.assertEqual(log.snapshot()['sha256'], hashlib.sha256((record+'\n').encode('utf-8')).hexdigest())
        small = self.log(max_record_bytes=limit-1)
        with self.assertRaisesRegex(RuntimeError, 'byte limit'):small.write(record)
        with self.assertRaises(RuntimeError):small.close()
        self.assertEqual((small.snapshot()['accepted'],small.snapshot()['written']), (0,0))

    def test_existing_file_is_never_overwritten_and_invalid_limits_create_no_artifact(self):
        path = self.root/'existing.jsonl'; path.write_bytes(b'original\n')
        with self.assertRaises(FileExistsError):NativeResultLog(path)
        self.assertEqual(path.read_bytes(), b'original\n')
        for options in ({'capacity':True},{'capacity':0},{'capacity':513},
                        {'max_record_bytes':0},{'max_record_bytes':65537}):
            with self.subTest(options=options), self.assertRaises(ValueError):NativeResultLog(self.root/'absent', **options)
        self.assertFalse((self.root/'absent').exists())


class GcObservationTests(unittest.TestCase):
    """Actual collection callbacks/clocks; no native-worker or real-time claims."""
    def setUp(self):
        self.observer = _GcObserver()
        self.addCleanup(self.observer.close)
        self.thresholds, self.enabled = gc.get_threshold(), gc.isenabled()

    def tearDown(self):
        self.assertEqual(gc.get_threshold(), self.thresholds)
        self.assertEqual(gc.isenabled(), self.enabled)

    def test_real_collection_reports_actual_callback_bounds_counts_and_thread_cpu(self):
        self.observer.start()
        before_host, before_cpu = time.monotonic_ns(), time.thread_time_ns()
        cycle = []; cycle.append(cycle); del cycle
        collected = gc.collect(2)
        after_host, after_cpu = time.monotonic_ns(), time.thread_time_ns()
        state = self.observer.snapshot()
        record = next(row for row in reversed(state['collections']) if row['generation'] == 2)
        self.assertGreaterEqual(collected, 1)
        self.assertGreaterEqual(record['collected'], 1)
        self.assertGreaterEqual(record['uncollectable'], 0)
        self.assertLessEqual(before_host, record['start_host_monotonic_ns'])
        self.assertLessEqual(record['end_host_monotonic_ns'], after_host)
        self.assertLessEqual(before_cpu, record['start_thread_cpu_ns'])
        self.assertLessEqual(record['end_thread_cpu_ns'], after_cpu)
        self.assertEqual(record['wall_ns'], record['end_host_monotonic_ns']-record['start_host_monotonic_ns'])
        self.assertEqual(record['thread_cpu_ns'], record['end_thread_cpu_ns']-record['start_thread_cpu_ns'])
        self.assertGreaterEqual(record['wall_ns'], 0)
        self.assertGreaterEqual(record['thread_cpu_ns'], 0)
        self.assertEqual((record['thread_id'], record['end_thread_id']), (threading.get_ident(),)*2)
        self.assertEqual((record['native_thread_id'], record['end_native_thread_id']), (threading.get_native_id(),)*2)
        self.assertEqual(len(record['allocation_counts_start']), 3)
        self.assertEqual(len(record['allocation_counts_stop']), 3)
        self.assertGreaterEqual(state['maximum_wall_ns'], record['wall_ns'])
        self.assertGreaterEqual(state['maximum_thread_cpu_ns'], record['thread_cpu_ns'])
        self.assertGreaterEqual(state['total_wall_ns'], record['wall_ns'])
        self.assertGreaterEqual(state['total_thread_cpu_ns'], record['thread_cpu_ns'])

    def test_actual_collections_on_another_thread_keep_that_threads_cpu_domain(self):
        self.observer.start()
        thread_info = {}
        def collect():
            thread_info.update(identity=threading.get_ident(), native_id=threading.get_native_id(),
                               before_cpu=time.thread_time_ns())
            gc.collect(2)
            thread_info['after_cpu'] = time.thread_time_ns()
        thread = threading.Thread(target=collect)
        thread.start(); thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        record = next(row for row in self.observer.snapshot()['collections']
                      if row['thread_id'] == thread_info['identity'] and row['generation'] == 2)
        self.assertEqual(record['native_thread_id'], thread_info['native_id'])
        self.assertLessEqual(thread_info['before_cpu'], record['start_thread_cpu_ns'])
        self.assertLessEqual(record['end_thread_cpu_ns'], thread_info['after_cpu'])

    def test_retention_is_bounded_and_overwritten_collections_are_explicit(self):
        self.observer.start()
        for _ in range(self.observer.records.maxlen+3):
            gc.collect(0)
        state = self.observer.snapshot()
        self.assertLessEqual(len(state['collections']), state['record_limit'])
        self.assertGreaterEqual(state['overwritten_records'], 3)
        self.assertGreaterEqual(state['completed_by_generation'][0], state['record_limit']+3)
        self.assertEqual(state['unmatched_stop_callbacks'], 0)
        # Exported records are independent of later caller edits.
        state['collections'][-1]['collected'] = -1
        self.assertGreaterEqual(self.observer.snapshot()['collections'][-1]['collected'], 0)

    def test_pool_close_removes_only_its_callback_and_stops_recording(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); config = root/'config.json'
            config.write_text(json.dumps(dict(schema=1, cohort=[{'id':'cp'}, {'id':'uav1'}])))
            pool = NativeRtWorkerPool([sys.executable], config, root/'artifacts', allow_development_stub=True)
            self.addCleanup(pool.close)
            def unrelated_callback(phase, info):
                pass
            before = list(gc.callbacks)
            gc.callbacks.append(unrelated_callback)
            self.addCleanup(lambda: gc.callbacks.remove(unrelated_callback))
            # Portable observer/close regression. Actual start() registration is
            # exercised separately by the POSIX subprocess integration below.
            pool._gc_observer.start()
            gc.collect(0)
            state = pool.snapshot()['garbage_collection']
            self.assertTrue(state['registered'])
            pool.close(); pool.close()
            self.assertEqual(gc.callbacks, before+[unrelated_callback])
            completed = pool.snapshot()['garbage_collection']['completed_collections']
            gc.collect(2)
            after = pool.snapshot()['garbage_collection']
            self.assertFalse(after['registered'])
            self.assertEqual(after['completed_collections'], completed)


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

    def test_stage_trace_keeps_delayed_solver_separate_from_result_transport(self):
        with patch.dict(os.environ, BAS_NATIVE_RT_STAGE_TRACE='1'):
            pool = self.create('stage_delayed')
            original = job()
            pool.submit(0, original)
            reply = self.completed()[0]
            self.assertEqual(reply['kind'], 'result')
            trace = reply['job_stage_trace']
            self.assertEqual(trace['geometry_sha256'], hashlib.sha256(json.dumps(
                original['poses'], sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest())
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                stages = pool.snapshot()['workers'][0]['job_stage_trace']['requests'][-1]['stages']
                if len(stages) == 6:
                    break
                time.sleep(.001)
            self.assertEqual([s['stage'] for s in stages],
                             ['job_received', 'preparation_done', 'solver_call_start', 'solver_call_end',
                              'result_send_start', 'result_send_end'])
            self.assertGreaterEqual(stages[3]['host_monotonic_ns'] - stages[2]['host_monotonic_ns'], 60_000_000)
            self.assertTrue(all(s['host_monotonic_ns'] <= s['receiver_host_monotonic_ns'] for s in stages))
            self.assertTrue(all(s['request_id'] == 1 for s in stages))

    def test_partial_or_hung_result_retains_incomplete_stage_after_shutdown(self):
        for mode in ('stage_partial', 'stage_hang'):
            with self.subTest(mode=mode), patch.dict(os.environ, BAS_NATIVE_RT_STAGE_TRACE='1'):
                pool = self.create(mode, job_timeout_s=.15)
                pool.submit(0, job())
                started = time.monotonic()
                for _ in range(100):
                    self.assertEqual(pool.poll(), [])
                self.assertLess(time.monotonic()-started, .1)
                result = self.completed()[0]
                self.assertEqual(result['kind'], 'error')
                self.assertIn('timeout', result['error'])
                before = pool.snapshot()['workers'][0]['job_stage_trace']
                self.assertEqual(before['requests'][-1]['stages'][-1]['stage'], 'solver_call_start')
                pool.close()
                self.assertEqual(pool.snapshot()['workers'][0]['job_stage_trace'], before)
                self.assertEqual(len(result['job_stage_trace']['stages']), 3)
                self.pool = None
                self.root = self.root / mode
                self.root.mkdir()
                self.stub = self.root / 'stub.py'
                self.stub.write_text(STUB, encoding='utf-8')

    def test_stage_trace_is_bounded_and_progress_does_not_fill_result_queue(self):
        with patch.dict(os.environ, BAS_NATIVE_RT_STAGE_TRACE='1'):
            pool = self.create('stage_normal')
            for identity in range(1, 31):
                self.assertTrue(pool.submit(0, job(identity)))
                self.assertEqual(self.completed()[0]['request_id'], identity)
            deadline = time.monotonic()+1
            while time.monotonic()<deadline:
                trace = pool.snapshot()['workers'][0]['job_stage_trace']
                if len(trace['requests'][-1]['stages']) == 6:
                    break
                time.sleep(.001)
            self.assertEqual(len(trace['requests']), 2)
            self.assertEqual(len(trace['records']), 64)
            self.assertEqual(trace['overwritten_records'], 30*6-64)
            self.assertLessEqual(pool._messages.qsize(), 1)

    def test_wrong_geometry_or_out_of_order_stage_is_an_explicit_error(self):
        for mode in ('stage_wrong_hash', 'stage_bad_order'):
            with self.subTest(mode=mode), patch.dict(os.environ, BAS_NATIVE_RT_STAGE_TRACE='1'):
                pool = self.create(mode)
                pool.submit(0, job())
                reply = self.completed()[0]
                self.assertEqual(reply['kind'], 'error')
                self.assertIn('stage', reply['error'])
                self.assertIsNotNone(pool.snapshot()['workers'][0]['failed'])
                pool.close()
                self.pool = None
                self.root = self.root / mode
                self.root.mkdir()
                self.stub = self.root / 'stub.py'
                self.stub.write_text(STUB, encoding='utf-8')

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
        self.assertNotIn(self.pool._gc_observer, gc.callbacks)

    def test_real_pool_start_registers_gc_observation_until_close(self):
        before = list(gc.callbacks)
        pool = self.create()
        self.assertIn(pool._gc_observer, gc.callbacks)
        gc.collect(2)
        state = pool.snapshot()['garbage_collection']
        self.assertTrue(state['registered'])
        self.assertGreaterEqual(state['completed_by_generation'][2], 1)
        pool.close()
        self.assertEqual(gc.callbacks, before)
        self.assertFalse(pool.snapshot()['garbage_collection']['registered'])

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
