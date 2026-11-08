"""Bounded native RT process adapter used by the live bridge and throughput probe.

Only receiver threads call Connection.recv(). poll() drains completed messages
without waiting; it also detects deadlines using the host monotonic clock.
The AF_UNIX sockets are trusted-local pickle endpoints inside an owned 0700 dir.
Ownership covers direct Popen children only. The native worker launches no child
processes; CUDA threads stay inside it. No process-group or unrelated PID kills.
"""
from __future__ import annotations

from dataclasses import dataclass
import copy
import hashlib
import json
import math
from multiprocessing.connection import Client, Listener
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import time


VERSIONS = {'sionna_rt': '1.2.0', 'mitsuba': '3.7.1', 'drjit': '1.2.0'}


def validate_config(value):
    """Explicit frozen input for the public native SDK worker; no defaults."""
    keys = {'schema', 'root', 'scene', 'normalize_delays', 'cohort', 'radio'}
    if (type(value) is not dict or not keys <= set(value) <= keys | {'source_profiles', 'desired_mode'}
            or type(value['schema']) is not int or value['schema'] != 1):
        raise ValueError('invalid native worker config schema/fields')
    mode = value.get('desired_mode', 'receiver_batch')
    if type(mode) is not str or mode not in ('receiver_batch', 'full_mesh'):
        raise ValueError('desired_mode must be receiver_batch or full_mesh')
    root = Path(value['root']).resolve(strict=True)
    if not Path(value['root']).is_absolute() or not root.is_dir():
        raise ValueError('config root must be an existing absolute directory')
    if type(value['scene']) is not dict or set(value['scene']) != {'path', 'sha256'}:
        raise ValueError('explicit prepared scene path/hash required')
    scene = (root / value['scene']['path']).resolve(strict=True)
    scene.relative_to(root)
    if not scene.is_file() or hashlib.sha256(scene.read_bytes()).hexdigest() != value['scene']['sha256']:
        raise ValueError('prepared scene SHA-256 mismatch')
    cohort = value['cohort']
    if type(cohort) is not list or not 2 <= len(cohort) <= 6 or any(
            type(item) is not dict or set(item) != {'id'} or type(item['id']) is not str or not item['id']
            for item in cohort) or len({item['id'] for item in cohort}) != len(cohort):
        raise ValueError('cohort requires 2..6 unique explicit IDs')
    radio = value['radio']
    if type(radio) is not dict or set(radio) != {'frequency_hz', 'samples_per_source',
            'max_paths_per_source', 'max_number_of_paths', 'merge_shapes', 'solver'}:
        raise ValueError('invalid frozen radio fields')
    for key in ('samples_per_source', 'max_paths_per_source'):
        if integer(radio[key], key, 1) >= 2**32:
            raise ValueError(f'{key} exceeds native uint32')
    if radio['max_paths_per_source'] * (len(cohort)-1) >= 2**32:
        raise ValueError('allocated candidate capacity exceeds native uint32')
    if mode == 'full_mesh' and (radio['max_paths_per_source'] * (len(cohort)-1) * len(cohort) >= 2**32
            or radio['samples_per_source'] * len(cohort) >= 2**32):
        raise ValueError('full-mesh total candidate capacity or rays exceed native uint32')
    for key in ('frequency_hz', 'max_number_of_paths'):
        if type(radio[key]) not in (int, float) or not math.isfinite(radio[key]) or radio[key] <= 0:
            raise ValueError(f'{key} must be finite and positive')
    if not 500e6 <= radio['frequency_hz'] <= 100e9:
        raise ValueError('carrier outside the native SDK supported range')
    if 'source_profiles' in value:
        profiles = value['source_profiles']
        if type(profiles) is not list or not profiles:
            raise ValueError('source_profiles must be a nonempty immutable registry')
        if cohort[0]['id'] != 'cp':
            raise ValueError('source STAR requires CP at cohort index0')
        identities = set()
        for profile in profiles:
            if type(profile) is not dict or set(profile) != {'id', 'frequency_hz', 'pattern'}:
                raise ValueError('source profile requires only id, exact frequency_hz and pattern')
            identity = profile['id']
            if type(identity) is not str or not identity or identity in identities:
                raise ValueError('source profile IDs must be nonempty and unique')
            identities.add(identity)
            frequency = profile['frequency_hz']
            if type(frequency) not in (int, float) or not math.isfinite(frequency) or not 500e6 <= frequency <= 100e9:
                raise ValueError('source carrier outside the native SDK supported range')
            if profile['pattern'] not in ('iso', 'tr38901'):
                raise ValueError('source pattern must be iso or fixed TR38901 OUTDOOR V1x1')
    solver = radio['solver']
    flags = {'los', 'specular_reflection', 'diffuse_reflection', 'refraction', 'diffraction',
        'edge_diffraction', 'synthetic_array'}
    if type(solver) is not dict or set(solver) != flags | {'max_depth', 'seed'}:
        raise ValueError('all actual solver flags, seed and depth are required')
    if any(type(solver[key]) is not bool for key in flags) or solver['synthetic_array'] is not True:
        raise ValueError('worker supports only the explicit iso 1x1 synthetic-array reference')
    for key in ('max_depth', 'seed'):
        if integer(solver[key], key) >= 2**31:
            raise ValueError(f'{key} exceeds native signed int')
    if type(value['normalize_delays']) is not bool or type(radio['merge_shapes']) is not bool:
        raise ValueError('NormalizeDelays and merge_shapes must be explicit bools')
    frozen = copy.deepcopy(value)
    frozen['root'], frozen['scene']['path'] = str(root), str(scene)
    return frozen


def integer(value, name, minimum=0):
    if type(value) is not int or not minimum <= value < 2**63:
        raise ValueError(f'{name} must be an integer in {minimum}..2**63-1')
    return value


def validate_job(job, count, config=None):
    expected = {'kind', 'request_id', 'sample_native_ns', 'sample_host_monotonic_ns', 'anchor_index', 'poses'}
    source = type(job) is dict and any(key in job for key in ('profile_id', 'peer_index', 'source_mode'))
    emitter_batch = source and 'source_mode' in job
    full_mesh = not source and type(config) is dict and config.get('desired_mode') == 'full_mesh'
    if source:
        expected |= {'profile_id', 'source_mode'} if emitter_batch else {'profile_id', 'peer_index'}
    elif full_mesh:
        expected.add('channel_mode')
    if type(job) is not dict or set(job) != expected or job['kind'] != 'solve':
        raise ValueError('invalid solve message fields')
    integer(job['request_id'], 'request_id', 1)
    integer(job['sample_native_ns'], 'sample_native_ns')
    host = integer(job['sample_host_monotonic_ns'], 'sample_host_monotonic_ns', 1)
    if source:
        if (type(config) is not dict or type(config.get('source_profiles')) is not list or
                type(job['profile_id']) is not str or not any(
                    profile['id'] == job['profile_id'] for profile in config['source_profiles'])):
            raise ValueError('source job requires a known immutable profile_id')
        if integer(job['anchor_index'], 'anchor_index') != count:
            raise ValueError('source STAR anchor must follow the complete desired cohort')
        if emitter_batch:
            if type(job['source_mode']) is not str or job['source_mode'] != 'emitter_batch':
                raise ValueError('source_mode must explicitly select emitter_batch; legacy jobs use peer_index')
            if config['radio']['max_paths_per_source'] * count >= 2**32:
                raise ValueError('emitter-batch candidate capacity exceeds native uint32')
        elif not 0 <= integer(job['peer_index'], 'peer_index') < count:
            raise ValueError('source STAR peer must be one of CP/UAV receivers')
    elif full_mesh:
        if job['channel_mode'] != 'full_mesh' or integer(job['anchor_index'], 'anchor_index') != 0:
            raise ValueError('full-mesh job requires explicit channel_mode and canonical anchor0')
    elif not 0 <= integer(job['anchor_index'], 'anchor_index') < count:
        raise ValueError('anchor_index outside the cohort')
    if type(job['poses']) is not list or len(job['poses']) != count + source:
        raise ValueError('poses must match the full cohort')
    for pose in job['poses']:
        if type(pose) is not dict or set(pose) != {'position_m', 'velocity_m_s', 'orientation_rad', 'sample_host_ns'}:
            raise ValueError('invalid pose fields')
        if integer(pose['sample_host_ns'], 'pose.sample_host_ns', 1) > host:
            raise ValueError('pose timestamp is later than the sampled job')
        for key in ('position_m', 'velocity_m_s', 'orientation_rad'):
            vector = pose[key]
            if type(vector) is not list or len(vector) != 3 or any(
                    type(v) not in (int, float) or not math.isfinite(v) for v in vector):
                raise ValueError(f'{key} requires three finite numbers')


def _validate_emitter_result(message, job, count, config):
    """Check the actual public SDK12 proof against the immutable submitted job."""
    profile = next(item for item in config['source_profiles'] if item['id'] == job['profile_id'])
    stats, radio = message.get('emitter_batch_stats'), config['radio']
    if (message.get('channel_mode') != 'source_star' or message.get('source_mode') != 'emitter_batch'
            or message.get('profile_id') != job['profile_id'] or message.get('frequency_hz') != profile['frequency_hz']
            or type(message.get('new_emitter_batch_calls')) is not int or message['new_emitter_batch_calls'] != 1
            or type(stats) is not dict or 'new_pairwise_calls' in message):
        raise ValueError('emitter-batch mode/profile/fresh-solve proof missing')
    cap = radio['max_paths_per_source'] * count
    required = dict(source_mode='emitter_batch', pattern=profile['pattern'], bound=True, has_published_batch=True,
        candidate_allocation_valid=True, frequency_hz=float(profile['frequency_hz']), registered_receivers=count,
        configured_samples_per_source=radio['samples_per_source'], configured_max_paths_per_source=radio['max_paths_per_source'],
        configured_max_number_of_paths=float(radio['max_number_of_paths']), configured_candidate_cap=cap,
        last_transmitter_count=1, last_receiver_count=count, last_allocated_candidate_cap=cap,
        last_candidate_buffer_size=cap, candidate_capacity_reached_calls=0, last_generated_ns=job['sample_native_ns'])
    for key, expected in required.items():
        actual = stats.get(key)
        if actual != expected or (type(expected) in (int, bool) and type(actual) is not type(expected)):
            raise ValueError(f'emitter-batch applied {key} differs from the submitted profile')
    candidates = stats.get('last_candidate_count')
    if (type(candidates) is not int or not 0 <= candidates < cap
            or message.get('allocated_candidate_cap') != cap or message.get('candidate_buffer_size') != cap
            or message.get('candidate_count') != candidates
            or message.get('candidate_count_scope') != 'actual_public_emitter_batch_api'):
        raise ValueError('emitter-batch candidate occupancy/allocation proof missing')
    for key in ('attempted_calls', 'completed_calls', 'generated_links', 'cache_read_hits'):
        if type(stats.get(key)) is not int or stats[key] < (count if key in ('generated_links', 'cache_read_hits') else 1):
            raise ValueError(f'emitter-batch actual counter {key} missing')
    if type(stats.get('cache_read_misses')) is not int or stats['cache_read_misses'] != 0:
        raise ValueError('emitter-batch read missed the refreshed STAR')
    for identities, source in (('receiver_node_ids', 'source_node_id'), ('receiver_antenna_ids', 'source_antenna_id')):
        values = stats.get(identities)
        if (type(values) is not list or len(values) != count or any(type(v) is not int or v < 0 for v in values)
                or len(set(values)) != count or type(stats.get(source)) is not int or stats[source] < 0 or stats[source] in values):
            raise ValueError('emitter-batch native endpoint identities are not a complete distinct STAR')
    for output, original in (('last_source_position_m', 'position_m'), ('last_source_velocity_m_s', 'velocity_m_s'),
                             ('last_source_orientation_rad', 'orientation_rad')):
        if stats.get(output) != job['poses'][count][original]:
            raise ValueError('emitter-batch source pose differs from the original sampled input')
    pairs, retained = message.get('pairs'), stats.get('last_retained_paths_per_receiver')
    if type(pairs) is not list or len(pairs) != count or type(retained) is not list or len(retained) != count:
        raise ValueError('emitter-batch did not return every CP/UAV receiver')
    for peer, pair in enumerate(pairs):
        if (type(pair) is not dict or pair.get('pair') != [count, peer] or pair.get('direction') != [count, peer]
                or pair.get('fresh_for_request') is not True or pair.get('poses') != job['poses']):
            raise ValueError('emitter-batch STAR coverage/direction/original poses differ from the request')
        for field, original in (('generated_ns', 'sample_native_ns'), ('sample_native_ns', 'sample_native_ns'),
                                ('source_request_id', 'request_id'), ('sample_host_monotonic_ns', 'sample_host_monotonic_ns')):
            if type(pair.get(field)) is not int or pair[field] != job[original]:
                raise ValueError('emitter-batch replaced the original request identity or sample epoch')
        shape = pair.get('coefficient_shape')
        if (type(shape) is not list or len(shape) != 3 or shape[:2] != [1, 1]
                or type(shape[2]) is not int or not 0 <= shape[2] <= radio['max_number_of_paths']
                or type(retained[peer]) is not int or not 0 <= retained[peer] <= shape[2]):
            raise ValueError('emitter-batch retained paths disagree with the actual native CIR shape')


@dataclass
class _Worker:
    identity: int
    listener: object
    process: subprocess.Popen
    log: object
    thread: threading.Thread | None = None
    connection: object = None
    ready: dict | None = None
    pending: dict | None = None
    submitted_ns: int = 0
    failed: str | None = None
    completed: int = 0
    full_mesh_completed_jobs: int = 0
    last_full_mesh_stats: dict | None = None
    emitter_batch_completed_jobs: int = 0
    last_emitter_batch_stats: dict | None = None
    # Complete Connection.recv/decode observed by the receiver thread. This is
    # transport evidence only; correlation/producer validation stays in poll().
    last_transport_receive: dict | None = None


class NativeRtWorkerPool:
    def __init__(self, command, config, artifacts, workers=1, startup_timeout_s=60., job_timeout_s=10.,
                 *, allow_development_stub=False):
        if not isinstance(command, (list, tuple)) or not command or any(type(v) is not str for v in command):
            raise ValueError('command must be a nonempty argv sequence')
        if type(workers) is not int or not 1 <= workers <= 3:
            raise ValueError('workers must be 1..3')
        if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0
               for v in (startup_timeout_s, job_timeout_s)):
            raise ValueError('timeouts must be finite and positive')
        self.command, self.config = list(command), Path(config).resolve()
        self.artifacts = Path(artifacts).resolve()
        config_value = json.loads(self.config.read_text(encoding='utf-8'))
        if config_value.get('schema') != 1 or type(config_value.get('cohort')) is not list:
            raise ValueError('schema=1 and an explicit cohort are required')
        self.count = len(config_value['cohort'])
        if not 2 <= self.count <= 6:
            raise ValueError('cohort must contain 2..6 endpoints')
        self.applied_config = None if allow_development_stub else validate_config(config_value)
        self.size, self.startup_timeout_s, self.job_timeout_ns = workers, startup_timeout_s, round(job_timeout_s * 1e9)
        self.allow_development_stub = allow_development_stub
        self._messages = queue.Queue(maxsize=2 * workers)
        self._closing = threading.Event()
        self._workers, self._last_request_id = {}, 0
        self.socket_dir = None
        self._started = False

    def _emit(self, identity, message, received_ns=None):
        while not self._closing.is_set():
            try:
                self._messages.put((identity, message, received_ns), timeout=.05)
                return
            except queue.Full:
                pass

    def _receive(self, worker):
        try:
            connection = worker.listener.accept()
            worker.connection = connection
            while not self._closing.is_set():
                message = connection.recv()
                received_ns = time.monotonic_ns()
                # Publish one coherent observation before queue insertion, even
                # when the main thread has not polled a completed result yet.
                worker.last_transport_receive = dict(host_monotonic_ns=received_ns,
                    kind=message.get('kind') if type(message) is dict else None,
                    request_id=message.get('request_id') if type(message) is dict else None)
                self._emit(worker.identity, message, received_ns)
        except Exception as error:
            self._emit(worker.identity, {'kind': 'transport_error', 'error': f'{type(error).__name__}: {error}'})

    def start(self):
        if self._started or self._closing.is_set():
            raise RuntimeError('pool cannot be restarted')
        if os.name != 'posix':
            raise RuntimeError('native RT workers require POSIX AF_UNIX; no transport fallback')
        self._started = True
        self.artifacts.mkdir(parents=True, exist_ok=False)
        self.socket_dir = Path(tempfile.mkdtemp(prefix='bas-rt-'))
        self.socket_dir.chmod(0o700)
        try:
            for identity in range(self.size):
                socket_path = self.socket_dir / f'{identity}.sock'
                listener = Listener(str(socket_path), family='AF_UNIX', authkey=None)
                log = (self.artifacts / f'worker-{identity}.log').open('xb')
                try:
                    process = subprocess.Popen(self.command + ['--socket', str(socket_path), '--config',
                        str(self.config), '--worker-id', str(identity)], stdin=subprocess.DEVNULL,
                        stdout=log, stderr=subprocess.STDOUT, shell=False)
                except BaseException:
                    listener.close(); log.close()
                    raise
                worker = _Worker(identity, listener, process, log)
                self._workers[identity] = worker
                worker.thread = threading.Thread(target=self._receive, args=(worker,),
                    name=f'bas-rt-reader-{identity}', daemon=True)
                worker.thread.start()
            deadline = time.monotonic() + self.startup_timeout_s
            while any(worker.ready is None for worker in self._workers.values()):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('native RT worker startup timeout')
                try:
                    identity, message, received_ns = self._messages.get(timeout=min(.05, remaining))
                except queue.Empty:
                    exited = [w.identity for w in self._workers.values() if w.ready is None and w.process.poll() is not None]
                    if exited:
                        raise RuntimeError(f'workers exited before readiness: {exited}; inspect worker logs')
                    continue
                worker = self._workers[identity]
                if type(message) is not dict or message.get('kind') != 'ready' or message.get('schema') != 1 or (
                        message.get('worker_id') != identity or message.get('pid') != worker.process.pid):
                    raise RuntimeError(f'worker {identity}: invalid ready message: {message}')
                stub = message.get('development_stub') is True
                if not (stub and self.allow_development_stub) and (
                        stub or message.get('versions') != VERSIONS or
                        not str(message.get('mitsuba_variant', '')).startswith('cuda')):
                    raise RuntimeError(f'worker {identity}: pinned CUDA versions not confirmed')
                if not stub and message.get('applied_config') != self.applied_config:
                    raise RuntimeError(f'worker {identity}: applied configuration does not match the frozen input')
                if worker.ready is not None:
                    raise RuntimeError('duplicate worker readiness')
                worker.ready = dict(message, receiver_enqueued_host_monotonic_ns=received_ns)
            return self
        except BaseException:
            self.close()
            raise

    def submit(self, worker_id, job):
        if not self._started or self._closing.is_set():
            raise RuntimeError('pool is not running')
        worker = self._workers[worker_id]
        if worker.failed:
            raise RuntimeError(f'worker {worker_id} failed: {worker.failed}')
        if worker.pending is not None:
            return False
        validate_job(job, self.count, getattr(self, 'applied_config', None))
        if job['request_id'] <= self._last_request_id:
            raise ValueError('request_id must be strictly increasing across the pool lifetime')
        frozen = copy.deepcopy(job)
        self._last_request_id = frozen['request_id']
        worker.pending, worker.submitted_ns = frozen, time.monotonic_ns()
        try:
            # Validated fixed-size 2..7-pose messages and one in-flight job bound
            # the write; no result read or result wait occurs on this path.
            worker.connection.send(frozen)
        except (OSError, EOFError, ValueError) as error:
            worker.failed = f'submit failed: {error}'
        return True

    def poll(self):
        completed = []
        while True:
            try:
                identity, message, received_ns = self._messages.get_nowait()
            except queue.Empty:
                break
            worker = self._workers[identity]
            if worker.failed and worker.pending is None:
                continue
            pending = worker.pending
            valid = (type(message) is dict and message.get('schema') == 1 and
                message.get('worker_id') == identity and message.get('kind') in ('result', 'error') and
                pending is not None and message.get('request_id') == pending['request_id'])
            if not valid:
                worker.failed = f'invalid/uncorrelated worker reply: {message}'
                message = {'schema': 1, 'kind': 'error', 'worker_id': identity,
                    'request_id': pending['request_id'] if pending else None, 'error': worker.failed}
            elif message['kind'] == 'error':
                worker.failed = str(message.get('error', 'native worker error'))
            else:
                full_mesh = pending.get('channel_mode') == 'full_mesh'
                stats = message.get('full_mesh_stats')
                emitter_batch = pending.get('source_mode') == 'emitter_batch'
                emitter_error = None
                if emitter_batch:
                    try:
                        _validate_emitter_result(message, pending, self.count, self.applied_config)
                    except (KeyError, TypeError, ValueError, StopIteration) as error:
                        emitter_error = str(error)
                if full_mesh and (message.get('channel_mode') != 'full_mesh'
                        or type(message.get('new_full_mesh_calls')) is not int or message['new_full_mesh_calls'] != 1
                        or type(stats) is not dict or stats.get('source_mode') != 'full_mesh'):
                    worker.failed = 'full-mesh producer proof missing or incompatible with the submitted job'
                    message = dict(schema=1,kind='error',worker_id=identity,
                                   request_id=pending['request_id'],error=worker.failed)
                elif emitter_error is not None or (not emitter_batch and message.get('source_mode') == 'emitter_batch'):
                    worker.failed = 'emitter-batch producer proof incompatible with the submitted job: ' + (emitter_error or 'unsolicited mode')
                    message = dict(schema=1,kind='error',worker_id=identity,
                                   request_id=pending['request_id'],error=worker.failed)
                else:
                    worker.completed += 1
                    if full_mesh:
                        worker.full_mesh_completed_jobs += 1
                        worker.last_full_mesh_stats = copy.deepcopy(stats)
                    if emitter_batch:
                        worker.emitter_batch_completed_jobs += 1
                        worker.last_emitter_batch_stats = copy.deepcopy(message['emitter_batch_stats'])
            worker.pending = None
            message = dict(message, receiver_enqueued_host_monotonic_ns=received_ns,
                           received_host_monotonic_ns=time.monotonic_ns())
            completed.append(message)
        now = time.monotonic_ns()
        for identity, worker in self._workers.items():
            if worker.pending is not None and (worker.failed or now - worker.submitted_ns > self.job_timeout_ns):
                worker.failed = worker.failed or 'native RT job timeout; no retry or fallback'
                completed.append({'schema': 1, 'kind': 'error', 'worker_id': identity,
                    'request_id': worker.pending['request_id'], 'error': worker.failed,
                    'received_host_monotonic_ns': now})
                worker.pending = None
        return completed

    def snapshot(self):
        return {'schema': 1, 'scope': 'development_stub' if self.allow_development_stub else 'native_rt_worker_pool',
            'closed': self._closing.is_set(), 'socket_dir': str(self.socket_dir) if self.socket_dir else None,
            'last_request_id': self._last_request_id,
            'workers': [{'worker_id': w.identity, 'pid': w.process.pid, 'ready': w.ready,
                'busy': w.pending is not None, 'request_id': w.pending['request_id'] if w.pending else None,
                'failed': w.failed, 'completed': w.completed,
                'full_mesh_completed_jobs': w.full_mesh_completed_jobs,
                'last_full_mesh_stats': copy.deepcopy(w.last_full_mesh_stats),
                'emitter_batch_completed_jobs': w.emitter_batch_completed_jobs,
                'last_emitter_batch_stats': copy.deepcopy(w.last_emitter_batch_stats),
                'last_transport_receive': copy.deepcopy(w.last_transport_receive)} for w in self._workers.values()]}

    def close(self):
        if self._closing.is_set():
            return
        self._closing.set()
        failures = []
        for worker in self._workers.values():
            if worker.process.poll() is None:
                worker.process.terminate()
        for worker in self._workers.values():
            try:
                worker.process.wait(timeout=2.)
            except subprocess.TimeoutExpired:
                worker.process.kill()
                try:
                    worker.process.wait(timeout=2.)
                except subprocess.TimeoutExpired:
                    failures.append(f'owned worker {worker.identity} did not exit after kill')
            if worker.connection is None:
                # Wake an accept() thread when the child never connected.
                try:
                    wake = Client(worker.listener.address, family='AF_UNIX', authkey=None)
                    wake.close()
                except (OSError, ValueError):
                    pass
            if worker.thread:
                worker.thread.join(timeout=2.)
            if worker.connection:
                worker.connection.close()
            worker.listener.close(); worker.log.close()
            if worker.thread and worker.thread.is_alive():
                failures.append(f'owned receiver thread {worker.identity} did not stop')
        if self.socket_dir:
            try:
                self.socket_dir.rmdir()
            except OSError as error:
                failures.append(f'owned socket dir cleanup failed: {error}')
        if failures:
            raise RuntimeError('; '.join(failures))

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.close()


def probe():
    """Stationary replay throughput probe; never presents saved poses as sensors."""
    import argparse
    parser = argparse.ArgumentParser(description=probe.__doc__)
    parser.add_argument('--worker', type=Path, required=True, help='built native-rt-worker binary')
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--mission', type=Path, required=True, help='existing prepared mission-scene.json')
    parser.add_argument('--output', type=Path, required=True, help='new result directory')
    parser.add_argument('--jobs', type=int, default=24, help='jobs per pool size, including first cold job per worker')
    parser.add_argument('--job-timeout-s', type=float, default=10.)
    args = parser.parse_args()
    if args.jobs < 12:
        parser.error('--jobs must be at least 12')
    config = validate_config(json.loads(args.config.read_text(encoding='utf-8')))
    mission = json.loads(args.mission.read_text(encoding='utf-8'))
    count = len(config['cohort'])
    if [node['id'] for node in config['cohort']] != ['cp']+[f'uav{i}' for i in range(1, count)]:
        raise ValueError('driver requires cp,uav1,.. roster; the pool/worker API is not restricted to these names')
    positions = [mission['gcs']] + [mission['spawns'][str(i)] for i in range(1, count)]
    args.output.mkdir(parents=True, exist_ok=False)
    def save(path, value):
        path.write_text(json.dumps(value, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    native_root = next((parent for parent in args.worker.resolve().parents
                        if (parent/'.git').exists()), None)
    if native_root is None:
        raise ValueError('probe requires the native source checkout for dependency provenance')
    native_git = ['git', '-c', f'safe.directory={native_root}', '-C', str(native_root)]
    summary = {'schema': 1, 'scope': 'stationary_saved_pose_replay_throughput; no live flight or hardware',
        'config': config, 'mission': str(args.mission.resolve()), 'poll_period_ns': 20_000_000,
        'source_commit': subprocess.check_output(['git','rev-parse','HEAD'], text=True).strip(),
        'source_dirty': bool(subprocess.check_output(['git','status','--porcelain'], text=True).strip()),
        'native_source_commit': subprocess.check_output(native_git+['rev-parse','HEAD'], text=True).strip(),
        'native_source_dirty': bool(subprocess.check_output(native_git+['status','--porcelain'], text=True).strip()),
        'worker_sha256': hashlib.sha256(args.worker.read_bytes()).hexdigest(),
        'config_sha256': hashlib.sha256(args.config.read_bytes()).hexdigest(),
        'mission_sha256': hashlib.sha256(args.mission.read_bytes()).hexdigest(),
        'probe_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), 'pools': [], 'status': 'started'}
    save(args.output/'metadata.json', summary)
    for size in (1, 3):
        directory = args.output/f'pool-{size}'; directory.mkdir()
        pool = NativeRtWorkerPool([str(args.worker.resolve())], args.config, directory/'workers', size,
            job_timeout_s=args.job_timeout_s)
        replies, poll_wall_ns, poll_lag_ns = [], [], []
        submitted, started_ns, status, error = 0, None, 'failed', None
        try:
            pool.start()
            started_ns = time.monotonic_ns()
            next_poll = started_ns
            while len(replies) < args.jobs:
                now = time.monotonic_ns()
                poll_lag_ns.append(max(0, now-next_poll))
                before = time.monotonic_ns(); received = pool.poll()
                poll_wall_ns.append(time.monotonic_ns()-before)
                replies.extend(received)
                if any(row['kind'] != 'result' for row in received):
                    raise RuntimeError('worker failure; inspect replies.json')
                for worker in pool.snapshot()['workers']:
                    if submitted >= args.jobs:
                        break
                    if worker['busy']:
                        continue
                    sampled = time.monotonic_ns()
                    # These are explicitly imposed stationary replay states,
                    # not retimestamped claims about the live Gazebo source.
                    poses = [dict(position_m=p, velocity_m_s=[0.,0.,0.], orientation_rad=[0.,0.,0.],
                        sample_host_ns=sampled) for p in positions]
                    request = dict(kind='solve', request_id=submitted+1,
                        sample_native_ns=sampled-started_ns, sample_host_monotonic_ns=sampled,
                        anchor_index=submitted % count, poses=poses)
                    if not pool.submit(worker['worker_id'], request):
                        raise RuntimeError('unexpected busy worker')
                    submitted += 1
                next_poll += 20_000_000
                time.sleep(max(0, next_poll-time.monotonic_ns())/1e9)
            status = 'completed'
        except Exception as failure:
            error = f'{type(failure).__name__}: {failure}'
        finally:
            elapsed_ns = time.monotonic_ns()-started_ns if started_ns is not None else None
            snapshot = pool.snapshot()
            try:
                pool.close()
            except Exception as failure:
                status, error = 'failed', f'cleanup failed: {type(failure).__name__}: {failure}'
            save(directory/'replies.json', replies)
            first_workers, hot = set(), []
            for row in replies:
                if row['kind'] != 'result':
                    continue
                if row['worker_id'] in first_workers:
                    hot.append(row)
                first_workers.add(row['worker_id'])
            row = dict(workers=size, status=status, error=error, submitted=submitted, replies=len(replies),
                elapsed_ns=elapsed_ns, jobs_per_wall_s=len(replies)*1e9/elapsed_ns if elapsed_ns else None,
                snapshot=snapshot, poll_wall_ns=poll_wall_ns, poll_lag_ns=poll_lag_ns,
                hot_producer_wall_ns=[r['producer_wall_ns'] for r in hot],
                hot_sample_to_receive_ns=[r['received_host_monotonic_ns']-p['sample_host_monotonic_ns']
                    for r in hot for p in r['pairs']],
                hot_pose_age_ns=[r['received_host_monotonic_ns']-min(pose['sample_host_ns'] for pose in p['poses'])
                    for r in hot for p in r['pairs']])
            save(directory/'metrics.json', row)
            summary['pools'].append(row)
            save(args.output/'metadata.json', summary)
        if status != 'completed':
            summary['status'] = 'failed'; save(args.output/'metadata.json', summary)
            raise RuntimeError(error)
    summary['status'] = 'completed'; save(args.output/'metadata.json', summary)


if __name__ == '__main__':
    probe()
