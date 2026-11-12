"""Run-owned Linux RAM staging and closed, byte-verified publication.

The host tmpfs mount is the actual limit; a directory is not a memory quota.
Failed/interrupted publications retain their RAM source and partial disk copy.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile
import time

MODE = 'host_tmpfs_run_v1'
DESCRIPTOR_SCHEMA = 2
SDFORMAT_CONTAINER_PATH = '/home/ubuntu/.sdformat'
INITIAL = 'logs/runtime_storage_initial.json'
MANIFEST = 'logs/runtime_storage_manifest.json'
RECEIPT = 'logs/runtime_storage_publication.json'
EXCLUDED = {MANIFEST, RECEIPT}
MIN_FREE_BYTES = 8 * 1024**3
MIN_AVAILABLE_MEMORY_BYTES = 12 * 1024**3
HANDOVER = 'logs/runtime_storage_handover.json'
HANDOVER_PROGRAM = r'''
import hashlib,json,os,stat,sys
from pathlib import Path
root=Path(sys.argv[1]); expected=sys.argv[2]
owner_path=root/'owner.json'
with os.fdopen(os.open(owner_path,os.O_RDONLY|os.O_NOFOLLOW),'rb') as stream:
    if os.fstat(stream.fileno()).st_nlink!=1: raise ValueError('hardlinked owner descriptor')
    data=stream.read()
if hashlib.sha256(data).hexdigest()!=expected: raise ValueError('spool owner descriptor changed')
owner=json.loads(data); uid,gid=owner['owner_uid'],owner['owner_gid']
if root.is_symlink() or root.parent!=Path('/dev/shm') or str(root)!=owner['spool'] or root.stat().st_uid!=uid:
    raise ValueError('refusing unowned spool')
for directory,dirs,files in os.walk(root,followlinks=False):
    for path in [Path(directory),*(Path(directory)/name for name in dirs+files)]:
        info=path.lstat()
        if stat.S_ISLNK(info.st_mode):
            if info.st_nlink!=1: raise ValueError('hardlinked runtime symlink')
            os.chown(path,uid,gid,follow_symlinks=False)
        elif stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode):
            fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
            try:
                actual=os.fstat(fd)
                if (actual.st_ino,actual.st_dev)!=(info.st_ino,info.st_dev): raise ValueError('spool inode changed')
                if stat.S_ISREG(actual.st_mode) and actual.st_nlink!=1: raise ValueError('hardlinked runtime evidence')
                os.fchown(fd,uid,gid)
                os.fchmod(fd,stat.S_IMODE(actual.st_mode)|(0o700 if stat.S_ISDIR(actual.st_mode) else 0o600))
            finally: os.close(fd)
        else: raise ValueError('unexpected special file in stopped runtime spool')
print(json.dumps(dict(owner_uid=uid,owner_gid=gid,owner_sha256=expected)))
'''


def read(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate storage JSON field')
            result[key] = value
        return result
    def constant(value):
        raise ValueError('nonfinite storage JSON number: '+value)
    return json.loads(Path(path).read_text(encoding='utf-8'), object_pairs_hook=pairs, parse_constant=constant)


def write(path, value):
    path = Path(path)
    temporary = path.with_name(path.name+'.tmp')
    with temporary.open('w', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write('\n')
    temporary.replace(path)


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while chunk := stream.read(1024*1024):
            digest.update(chunk)
    return digest.hexdigest()


def sync_owned(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def exists(path):
    return Path(path).exists() or Path(path).is_symlink()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9_.-]+', value) or value in ('.', '..'):
        raise ValueError('invalid run identity')
    return value


def validate_descriptor(value, run_id):
    """Portable original-path relationships, independent of a deleted RAM source."""
    if (not isinstance(value, dict) or type(value.get('schema')) is not int or value['schema'] not in (1, DESCRIPTOR_SCHEMA)
            or value.get('mode') != MODE or value.get('run_id') != identifier(run_id)
            or type(value.get('owner_uid')) is not int or value['owner_uid'] < 0
            or type(value.get('owner_gid')) is not int or value['owner_gid'] < 0):
        raise ValueError('invalid prospective runtime storage identity')
    def absolute(key):
        text = value.get(key)
        if not isinstance(text, str):
            raise ValueError('missing runtime storage path')
        path = PurePosixPath(text)
        if not path.is_absolute() or '..' in path.parts or str(path) != text:
            raise ValueError('runtime storage path is not a normalized absolute POSIX path')
        return path
    spool = absolute('spool')
    if spool.parent != PurePosixPath('/dev/shm') or not spool.name.startswith(f'bas-runtime-{value["owner_uid"]}-{run_id}-'):
        raise ValueError('runtime spool is outside its owned private tmpfs prefix')
    if (absolute('raw_source') != spool/'raw'/run_id
            or absolute('runtime_log_source') != spool/'controller/runtime.log'):
        raise ValueError('runtime source path escaped its owned spool')
    destination = absolute('raw_destination')
    if (destination.parts[-3:] != ('runs', 'native-radio-realtime', run_id)
            or absolute('runtime_log_destination') != destination.parents[2]/'runs/control'/run_id/'runtime.log'):
        raise ValueError('runtime publication destinations do not belong to one workspace/run')
    expected = dict(raw=spool/'raw'/run_id, runtime_log_parent=spool/'controller',
        stop_request_parent=spool, drjit_cache=spool/'caches/drjit', cuda_cache=spool/'caches/cuda',
        xdg_cache=spool/'caches/xdg', gazebo_cache=spool/'caches/gz')
    if value['schema'] >= 2:
        expected['sdformat_log'] = spool/'raw'/run_id/'logs/sdformat'
    locations = value.get('live_paths', {})
    mount = value.get('mount', {})
    if (set(locations) != set(expected) or mount.get('filesystem') != 'tmpfs'
            or mount.get('directory_quota_bytes') is not None
            or type(mount.get('total_bytes')) is not int or mount['total_bytes'] <= 0):
        raise ValueError('runtime tmpfs locations or mount capacity are missing')
    for name, path in expected.items():
        observed = locations[name]
        if (observed.get('path') != str(path) or observed.get('mount', {}).get('filesystem') != 'tmpfs'
                or any(observed['mount'].get(key) != mount.get(key)
                       for key in ('device_major', 'device_minor', 'total_bytes'))):
            raise ValueError('runtime live path was not observed on the owned tmpfs mount')
    return value


def tmpfs_observation(path):
    path = Path(path).resolve(strict=True)
    candidates = []
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        left, right = line.split(' - ', 1)
        fields = left.split()
        mount = Path(re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), fields[4]))
        if path.is_relative_to(mount):
            candidates.append((len(mount.parts), mount, right.split()[0]))
    if not candidates:
        raise ValueError('cannot identify runtime RAM mount')
    _, mount, filesystem = max(candidates)
    if filesystem != 'tmpfs':
        raise ValueError('runtime spool must reside on an observed tmpfs mount')
    space = os.statvfs(path)
    memory = re.search(r'^MemAvailable:\s+(\d+) kB$', Path('/proc/meminfo').read_text(), re.M)
    if memory is None:
        raise ValueError('host MemAvailable is unavailable')
    device = path.stat().st_dev
    return dict(mount_point=str(mount), filesystem=filesystem, device_major=os.major(device), device_minor=os.minor(device),
                total_bytes=space.f_blocks*space.f_frsize, available_bytes=space.f_bavail*space.f_frsize,
                memory_available_bytes=int(memory[1])*1024,
                minimum_free_bytes=MIN_FREE_BYTES, minimum_available_memory_bytes=MIN_AVAILABLE_MEMORY_BYTES,
                directory_quota_bytes=None, limit_scope='actual shared tmpfs mount, not a per-directory quota')


def prepare(root, run_id, container_name, *, shm=Path('/dev/shm'), image=None):
    root, run_id = Path(root).resolve(), identifier(run_id)
    raw = root/'runs/native-radio-realtime'/run_id
    control = root/'runs/control'/run_id
    descriptor = control/'runtime_spool.json'
    if any(exists(path) for path in (raw, control/'runtime.log', control/'stop.request', descriptor)):
        raise ValueError('run storage already exists, including broken links')
    observation = tmpfs_observation(shm)
    if (observation['available_bytes'] < MIN_FREE_BYTES
            or observation['memory_available_bytes'] < MIN_AVAILABLE_MEMORY_BYTES):
        raise ValueError('insufficient free tmpfs or available host memory for runtime staging')
    spool = Path(tempfile.mkdtemp(prefix=f'bas-runtime-{os.getuid()}-{run_id}-', dir=shm)).resolve()
    raw_source = spool/'raw'/run_id
    raw_source.mkdir(parents=True)
    (raw_source/'logs').mkdir()
    (raw_source/'logs/sdformat').mkdir()
    for name in ('controller', 'caches/drjit', 'caches/cuda', 'caches/xdg', 'caches/gz'):
        (spool/name).mkdir(parents=True, exist_ok=True)
    if image is not None and not re.fullmatch(r'sha256:[a-f0-9]{64}', image):
        raise ValueError('runtime ownership helper requires an immutable image digest')
    value = dict(schema=DESCRIPTOR_SCHEMA, mode=MODE, run_id=run_id, owner_uid=os.getuid(), owner_gid=os.getgid(), runtime_image=image,
                 spool=str(spool), raw_source=str(raw_source), raw_destination=str(raw),
                 runtime_log_source=str(spool/'controller/runtime.log'),
                 runtime_log_destination=str(control/'runtime.log'),
                 container_name=container_name, prepared_host_monotonic_ns=time.monotonic_ns(), mount=observation)
    value['live_paths'] = {name: dict(path=str(path), mount=tmpfs_observation(path)) for name, path in {
        'raw': raw_source, 'runtime_log_parent': spool/'controller', 'stop_request_parent': spool,
        'drjit_cache': spool/'caches/drjit', 'cuda_cache': spool/'caches/cuda',
        'xdg_cache': spool/'caches/xdg', 'gazebo_cache': spool/'caches/gz',
        'sdformat_log': raw_source/'logs/sdformat'}.items()}
    control.mkdir(parents=True, exist_ok=True)
    raw.parent.mkdir(parents=True, exist_ok=True)
    write(spool/'owner.json', value)
    write(descriptor, value)  # Independent request marker, even if raw later disappears.
    write(raw_source/INITIAL, value)
    raw.symlink_to(raw_source, target_is_directory=True)
    (control/'runtime.log').symlink_to(value['runtime_log_source'])
    (control/'stop.request').symlink_to(spool/'stop.request')
    return value


def validate_live(root, run_id, spool, *, allow_partial_publication=False):
    root, spool = Path(root).resolve(), Path(spool).resolve(strict=True)
    value = read(spool/'owner.json')
    validate_descriptor(value, run_id)
    if (value.get('mode') != MODE or value.get('run_id') != identifier(run_id)
            or value.get('spool') != str(spool) or spool.parent != Path('/dev/shm')
            or not spool.name.startswith(f"bas-runtime-{value.get('owner_uid')}-{run_id}-")
            or spool.stat().st_uid != value.get('owner_uid')):
        raise ValueError('runtime spool ownership mismatch')
    raw = root/'runs/native-radio-realtime'/run_id
    log = root/'runs/control'/run_id/'runtime.log'
    raw_owned = raw.is_symlink() and os.readlink(raw) == value['raw_source']
    log_owned = log.is_symlink() and os.readlink(log) == value['runtime_log_source']
    if allow_partial_publication:
        if raw.is_dir() and not raw.is_symlink():
            raw_owned = read(raw/INITIAL) == value and read(raw/RECEIPT).get('source_run_dir') == value['raw_source']
        if log.is_file() and not log.is_symlink():
            original = Path(value['runtime_log_source'])
            log_owned = log.stat().st_size == original.stat().st_size and sha(log) == sha(original)
    if (not raw_owned or not log_owned or read(root/'runs/control'/run_id/'runtime_spool.json') != value):
        raise ValueError('runtime public links or descriptor do not belong to this spool')
    actual = tmpfs_observation(spool)
    if any(actual[key] != value['mount'][key] for key in ('filesystem', 'device_major', 'device_minor', 'total_bytes')):
        raise ValueError('runtime tmpfs mount identity changed')
    return value


def observe_live_paths(value, *, container=False):
    observation = dict(schema=1, mode=MODE, run_id=value['run_id'],
        observed_host_monotonic_ns=time.monotonic_ns(),
        owner_sha256=sha(Path(value['spool'])/'owner.json'),
        paths={name: dict(path=row['path'], mount=tmpfs_observation(row['path']))
               for name, row in value['live_paths'].items()})
    if container and value['schema'] >= 2:
        source = Path(value['live_paths']['sdformat_log']['path'])
        alias = Path(SDFORMAT_CONTAINER_PATH)
        if alias.is_symlink() or not alias.is_dir() or not os.path.samefile(source, alias):
            raise ValueError('sdformat log directory is not the owned runtime bind')
        observation['sdformat_binding'] = dict(source_path=str(source), container_path=str(alias),
            source_inode=source.stat().st_ino, container_inode=alias.stat().st_ino,
            mount=tmpfs_observation(alias))
    return observation


def container_closure(value):
    """Successful Docker listing is mandatory; daemon failure is not closure."""
    process = subprocess.run(['docker', 'ps', '-a', '--no-trunc', '--filter',
        'name=^/'+value['container_name']+'$', '--format', '{{json .}}'], capture_output=True, text=True, check=True)
    rows = [json.loads(line) for line in process.stdout.splitlines() if line.strip()]
    cid_file = Path(value['spool'])/'container.id'
    identity = cid_file.read_text().strip() if cid_file.exists() else None
    if identity is not None and not re.fullmatch(r'[a-f0-9]{64}', identity):
        raise ValueError('invalid owned container ID')
    if rows:
        if len(rows) != 1 or identity is None or rows[0].get('ID') != identity:
            raise ValueError('unexpected container still owns this run name')
        observed = json.loads(subprocess.check_output(['docker', 'inspect', '--format', '{{json .State}}', identity], text=True))
        if observed.get('Running') is not False or observed.get('Pid') != 0:
            raise ValueError('runtime producers have not stopped')
    return dict(verified=True, container_id=identity, container_name=value['container_name'],
                container_absent=not rows, captured_host_monotonic_ns=time.monotonic_ns())


def ownership_handover(value):
    spool = Path(value['spool'])
    image = value['runtime_image']
    if image is None:  # Local non-container fixtures still must own every inode.
        for directory, dirs, files in os.walk(spool, followlinks=False):
            if any((Path(directory)/name).lstat().st_uid != value['owner_uid'] for name in dirs+files):
                raise ValueError('foreign runtime ownership requires the pinned image handover')
        receipt = dict(status='already_owned', owner_uid=value['owner_uid'], owner_gid=value['owner_gid'],
                       owner_sha256=sha(spool/'owner.json'))
    else:
        if not re.fullmatch(r'sha256:[a-f0-9]{64}', image):
            raise ValueError('invalid ownership handover image')
        argv = ['docker', 'run', '--rm', '--network=none', '--read-only', '--user', '0:0',
                '--mount', f'type=bind,src={spool},dst={spool}', image, 'python3', '-c', HANDOVER_PROGRAM,
                str(spool), sha(spool/'owner.json')]
        process = subprocess.run(argv, capture_output=True, text=True, check=True)
        receipt = dict(status='ownership_handed_over', image=image, argv=argv, returncode=process.returncode,
            stdout_sha256=hashlib.sha256(process.stdout.encode()).hexdigest(),
            stderr_sha256=hashlib.sha256(process.stderr.encode()).hexdigest(), **json.loads(process.stdout))
        if (receipt.get('owner_uid') != value['owner_uid'] or receipt.get('owner_gid') != value['owner_gid']
                or receipt.get('owner_sha256') != sha(spool/'owner.json')):
            raise ValueError('runtime ownership helper receipt mismatch')
    return receipt


def tree_members(source):
    source, members = Path(source), {}
    for directory, dirs, files in os.walk(source, followlinks=False):
        for name in sorted(dirs+files):
            path = Path(directory)/name
            relative = path.relative_to(source).as_posix()
            if relative in EXCLUDED:
                continue
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                target = path.resolve(strict=True)
                if not target.is_relative_to(source.resolve()):
                    raise ValueError('runtime evidence link escapes its run: '+relative)
                members[relative] = dict(kind='symlink', target=os.path.relpath(target, path.parent),
                                        original_target=os.readlink(path))
            elif stat.S_ISREG(info.st_mode):
                if info.st_nlink != 1:
                    raise ValueError('runtime evidence hardlink is not owned exclusively: '+relative)
                digest = sha(path)
                after = path.stat()
                if (info.st_size, info.st_mtime_ns, info.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                    raise ValueError('runtime evidence changed while hashing: '+relative)
                members[relative] = dict(kind='file', bytes=info.st_size, sha256=digest)
            elif stat.S_ISDIR(info.st_mode):
                members[relative] = dict(kind='directory')
            else:
                raise ValueError('runtime evidence contains a special file: '+relative)
    return members


def exchange(left, right):
    """Atomic dir/symlink swap on the destination filesystem; neither is lost."""
    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, 'renameat2', None)
    if function is None:
        raise OSError('renameat2 is required for atomic runtime publication')
    function.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    function.restype = ctypes.c_int
    if function(-100, os.fsencode(left), -100, os.fsencode(right), 2):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def publish(root, value, controller_returncode):
    root = Path(root).resolve()
    if type(controller_returncode) is not int:
        raise ValueError('publication requires the exited controller return code')
    value = validate_live(root, value['run_id'], value['spool'], allow_partial_publication=True)
    source, destination = Path(value['raw_source']), Path(value['raw_destination'])
    control = root/'runs/control'/value['run_id']
    partial = None
    def finish():
        result = _verify(destination, allow_pending_sync=True)
        if result.get('passed') is not True:
            raise ValueError('published runtime verification failed: '+str(result))
        # Drain only this run's own copied writes before a following case can
        # start. Never sync the host filesystem or unrelated writers globally.
        for relative, member in result['members'].items():
            if member['kind'] == 'file':
                sync_owned(destination/relative)
        sync_owned(destination/MANIFEST)
        directories = [destination] + [destination/path for path, member in result['members'].items()
                                       if member['kind'] == 'directory']
        for path in sorted(directories, key=lambda p: len(p.parts), reverse=True):
            sync_owned(path)
        sync_owned(control/'runtime.log')
        sync_owned(destination.parent)
        sync_owned(control)
        receipt = read(destination/RECEIPT)
        receipt.update(status='verified_closed_copy', owned_files_synchronized=True,
                       owned_directories_synchronized=True)
        write(destination/RECEIPT, receipt)
        sync_owned(destination/RECEIPT)
        sync_owned((destination/RECEIPT).parent)
        failure = control/'runtime_spool_publication_failure.json'
        if failure.exists():
            failure.unlink()
            sync_owned(control)
        result = verify(destination)
        if result.get('passed') is not True:
            raise ValueError('completed runtime verification failed: '+str(result))
        stop = control/'stop.request'
        source_stop = Path(value['spool'])/'stop.request'
        if stop.is_symlink():
            if os.readlink(stop) != str(source_stop):
                raise ValueError('stop marker no longer belongs to this runtime spool')
            if stop.exists():
                stop_copy = control/'.stop.request.publishing'
                shutil.copy2(stop, stop_copy)
                sync_owned(stop_copy)
                stop_copy.replace(stop)
            else:
                stop.unlink()
        elif exists(stop) != source_stop.exists() or (stop.exists() and sha(stop) != sha(source_stop)):
            raise ValueError('published stop marker differs from its owned source')
        sync_owned(control)
        shutil.rmtree(Path(value['spool']))  # Exact private ownership was validated above.
        return result
    try:
        closed = container_closure(value)
        if closed.get('verified') is not True:
            raise ValueError('runtime producers have not stopped')
        if not destination.is_symlink():
            return finish()  # Recover an interrupted completion after the atomic swap.
        handover = ownership_handover(value)
        write(source/HANDOVER, handover)
        runtime_source = Path(value['runtime_log_source'])
        log_info = dict(path=(control/'runtime.log').relative_to(root).as_posix(),
                        bytes=runtime_source.stat().st_size, sha256=sha(runtime_source))
        members = tree_members(source)
        partial = Path(tempfile.mkdtemp(prefix='.'+value['run_id']+'.publishing-', dir=destination.parent))
        for relative, member in members.items():
            target = partial/relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if member['kind'] == 'file':
                shutil.copy2(source/relative, target)
                if target.stat().st_size != member['bytes'] or sha(target) != member['sha256']:
                    raise ValueError('runtime copy failed byte verification: '+relative)
                sync_owned(target)
            elif member['kind'] == 'symlink':
                target.symlink_to(member['target'])
            else:
                target.mkdir(exist_ok=True)
        if tree_members(source) != members:
            raise ValueError('runtime source changed during closed publication')
        manifest = dict(schema=1, run_id=value['run_id'], mode=MODE, members=members)
        write(partial/MANIFEST, manifest)
        log_copy = control/('.runtime.log.publishing-'+partial.name.rsplit('-', 1)[-1])
        shutil.copy2(runtime_source, log_copy)
        if log_copy.stat().st_size != log_info['bytes'] or sha(log_copy) != log_info['sha256']:
            raise ValueError('runtime controller log copy failed byte verification')
        sync_owned(log_copy)
        receipt = dict(schema=1, mode=MODE, run_id=value['run_id'], status='copy_verified_pending_directory_sync',
            source_run_dir=str(source), destination_run_dir=str(destination), mount=value['mount'],
            controller_returncode=controller_returncode, producers_closed=closed, runtime_log=log_info,
            ownership_handover_sha256=sha(source/HANDOVER),
            owned_files_synchronized=True, owned_directories_synchronized=False,
            manifest=dict(path=MANIFEST, sha256=sha(partial/MANIFEST), members=len(members),
                          bytes=sum(row.get('bytes', 0) for row in members.values())),
            initial_sha256=sha(source/INITIAL), completed_host_monotonic_ns=time.monotonic_ns(), errors=[])
        write(partial/RECEIPT, receipt)
        validate_live(root, value['run_id'], value['spool'], allow_partial_publication=True)
        # Parent has already closed its log stream; these swaps expose only
        # verified bytes. Source RAM is retained until both swaps succeed.
        log_copy.replace(control/'runtime.log')
        exchange(partial, destination)
        if not partial.is_symlink() or os.readlink(partial) != str(source):
            raise ValueError('atomic publication did not return the owned source link')
        partial.unlink()
        return finish()
    except BaseException as error:
        write(control/'runtime_spool_publication_failure.json', dict(mode=MODE, run_id=value['run_id'],
            status='failed', error=f'{type(error).__name__}: {error}', spool=value['spool'],
            partial=str(partial) if partial is not None else None, source_retained=source.is_dir()))
        raise


def _verify(run, *, allow_pending_sync=False):
    """Fresh portable verification; missing raw cannot hide a prospective request."""
    run = Path(run).absolute()
    result = dict(status='invalid_runtime_spool_publication', passed=False)
    try:
        root = run.parents[2]
        descriptor = root/'runs/control'/run.name/'runtime_spool.json'
        failure = descriptor.with_name('runtime_spool_publication_failure.json')
        environment = run/'environment.txt'
        declared = re.findall(r'^runtime_storage_mode=(.*)$', environment.read_text() if environment.exists() else '', re.M)
        if not descriptor.exists() and not declared and not any((run/name).exists() for name in (INITIAL, MANIFEST, RECEIPT)):
            return dict(status='not_requested_legacy', passed=None)
        if failure.exists() and not allow_pending_sync:
            raise ValueError('runtime publication has an unresolved failure receipt')
        for path in (descriptor, run/INITIAL, run/MANIFEST, run/RECEIPT):
            if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
                raise ValueError('runtime storage metadata must be an owned regular file')
        value, initial, receipt = read(descriptor), read(run/INITIAL), read(run/RECEIPT)
        validate_descriptor(value, run.name)
        config = descriptor.with_name('config.json')
        if config.exists() and value['runtime_image'] != read(config).get('runtime', {}).get('image'):
            raise ValueError('runtime spool image differs from its prospective config')
        if (value != initial or declared != [MODE] or value.get('mode') != MODE
                or value.get('run_id') != run.name or receipt.get('mode') != MODE
                or receipt.get('run_id') != run.name or receipt.get('status') not in (
                    ('verified_closed_copy', 'copy_verified_pending_directory_sync') if allow_pending_sync else ('verified_closed_copy',))
                or receipt.get('owned_files_synchronized') is not True
                or (not allow_pending_sync and receipt.get('owned_directories_synchronized') is not True)
                or receipt.get('errors') != [] or receipt.get('initial_sha256') != sha(run/INITIAL)
                or receipt.get('source_run_dir') != value.get('raw_source')
                or receipt.get('destination_run_dir') != value.get('raw_destination')
                or receipt.get('mount') != value.get('mount') or run.is_symlink()):
            raise ValueError('runtime storage request/publication identity mismatch')
        locations = value.get('live_paths', {})
        expected_locations = {'raw', 'runtime_log_parent', 'stop_request_parent', 'drjit_cache',
                              'cuda_cache', 'xdg_cache', 'gazebo_cache'}
        if value['schema'] >= 2:
            expected_locations.add('sdformat_log')
        if (set(locations) != expected_locations or value['mount'].get('filesystem') != 'tmpfs'
                or value['mount'].get('directory_quota_bytes') is not None
                or any(row.get('mount', {}).get('filesystem') != 'tmpfs' for row in locations.values())):
            raise ValueError('runtime live paths were not verified on tmpfs')
        observed_path = run/'logs/runtime_storage_container.json'
        if value['runtime_image'] is not None:
            observed = read(observed_path)
            if (observed.get('mode') != MODE or observed.get('run_id') != run.name
                    or observed.get('owner_sha256') != sha(descriptor)
                    or set(observed.get('paths', {})) != set(locations)
                    or type(observed.get('observed_host_monotonic_ns')) is not int
                    or observed['observed_host_monotonic_ns'] < value['prepared_host_monotonic_ns']):
                raise ValueError('live container runtime storage observation is missing or unrelated')
            for name, expected in locations.items():
                actual = observed['paths'][name]
                if (actual.get('path') != expected['path'] or any(actual.get('mount', {}).get(key) != expected['mount'][key]
                        for key in ('filesystem', 'device_major', 'device_minor', 'total_bytes'))):
                    raise ValueError('live container path was not on the prospective tmpfs mount')
            if value['schema'] >= 2:
                binding = observed.get('sdformat_binding', {})
                expected = locations['sdformat_log']
                if (binding.get('source_path') != expected['path']
                        or binding.get('container_path') != SDFORMAT_CONTAINER_PATH
                        or type(binding.get('source_inode')) is not int or binding['source_inode'] <= 0
                        or binding.get('container_inode') != binding['source_inode']
                        or type(binding.get('container_inode')) is not int
                        or any(binding.get('mount', {}).get(key) != expected['mount'][key]
                               for key in ('filesystem', 'device_major', 'device_minor', 'total_bytes'))):
                    raise ValueError('sdformat container log bind was not observed on the owned tmpfs inode')
        closed = receipt.get('producers_closed', {})
        if (closed.get('verified') is not True or closed.get('container_name') != value.get('container_name')
                or type(receipt.get('controller_returncode')) is not int
                or type(closed.get('captured_host_monotonic_ns')) is not int
                or closed['captured_host_monotonic_ns'] < value['prepared_host_monotonic_ns']
                or receipt.get('completed_host_monotonic_ns', 0) < closed['captured_host_monotonic_ns']):
            raise ValueError('runtime producer closure is not verified')
        handover = read(run/HANDOVER)
        if (sha(run/HANDOVER) != receipt.get('ownership_handover_sha256')
                or handover.get('owner_uid') != value['owner_uid'] or handover.get('owner_gid') != value['owner_gid']
                or (value['runtime_image'] is not None and (handover.get('status') != 'ownership_handed_over'
                    or handover.get('image') != value['runtime_image'] or handover.get('returncode') != 0))):
            raise ValueError('runtime ownership handover is not verified')
        manifest = receipt['manifest']
        if manifest.get('path') != MANIFEST or manifest.get('sha256') != sha(run/MANIFEST):
            raise ValueError('runtime publication manifest hash mismatch')
        inventory = read(run/MANIFEST)
        if inventory.get('schema') != 1 or inventory.get('run_id') != run.name or inventory.get('mode') != MODE:
            raise ValueError('runtime publication manifest identity mismatch')
        actual, members = tree_members(run), inventory['members']
        # An absolute internal link is normalized to a portable relative target
        # during copy; compare its declared destination rather than old spelling.
        normalized = lambda rows: {key: {k: v for k, v in row.items() if k != 'original_target'} for key, row in rows.items()}
        if (normalized(actual) != normalized(members) or manifest.get('members') != len(members)
                or manifest.get('bytes') != sum(row.get('bytes', 0) for row in members.values())):
            raise ValueError('published runtime members differ from the closed source manifest')
        log = receipt['runtime_log']
        expected_log = f'runs/control/{run.name}/runtime.log'
        if (log.get('path') != expected_log or (root/expected_log).is_symlink()
                or (root/expected_log).stat().st_size != log.get('bytes') or sha(root/expected_log) != log.get('sha256')):
            raise ValueError('runtime controller log publication mismatch')
        result.update(status='verified_closed_runtime_spool', passed=True, mode=MODE, run_id=run.name,
            source_run_dir=value['raw_source'], destination_run_dir=value['raw_destination'],
            manifest_sha256=manifest['sha256'], member_count=len(members), bytes=manifest['bytes'],
            runtime_log=log, members=members, receipt_sha256=sha(run/RECEIPT), descriptor_sha256=sha(descriptor))
        if value['schema'] >= 2:
            result['descriptor_schema'] = value['schema']
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError) as error:
        result['error'] = str(error)
    return result


def verify(run):
    return _verify(run)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--spool', required=True, type=Path)
    parser.add_argument('--container', action='store_true', help='also verify fixed log bind inside the runtime container')
    args = parser.parse_args()
    print(json.dumps(observe_live_paths(validate_live(args.root, args.run_id, args.spool), container=args.container)))
