#!/usr/bin/env python3
"""Build the native delivery runtime without starting simulators or using a GPU.

Staging and exact-pin checks reuse the approach of the earlier BAS
native_demo_preflight.sh (mother working copy, SHA256
90cfef4e777517415bcce50a3f0853945ffa9f9d98d094478179a66a3d730b1b).
pip builds/resolves wheels; pip --require-hashes installs the frozen result.
This tool does not rebuild the base image or qualify a physical installation.
SPDX-License-Identifier: GPL-3.0
"""
import argparse
from contextlib import contextmanager
from email.parser import BytesParser
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import zipfile

ROOT = Path(__file__).resolve().parents[2]
MOUNT = '/workspace/multiagent_simulation'
SDK_PATH = '.external/ns-3-sionna-native'
NS3_SHA = 'd2add90b452d600cfb4859baed8e9ea633519447'
NS3_REPOSITORY = 'https://gitlab.com/nsnam/ns-3-dev.git'
PATCH_NAMES = (
    'mr2608-spike-compatibility.patch', 'mr2608-realtime-scene-cache.patch',
    'mr2608-spectrumwifi-phased-array-adapter.patch', 'mr2608-lockstep-tap-read.patch',
    'mr2608-tap-device-queue-selection.patch', 'mr2608-solver-budget.patch',
    'mr2608-baseband-spectrum-phase.patch', 'mr2608-doppler-epoch.patch',
    'mr2608-receiver-batch.patch',
    'mr2608-dynamic-rt-inputs.patch',
)
SCRATCH_FILES = (
    'upstream-sionna-tap-spike.cc', 'native-geometry-loss.h', 'native-spectrum-sources.h',
    'native-live-state.h', 'native-lockstep.h', 'native-rt-cache.h',
    'native-rt-pool.h', 'native-rt-worker.cc',
)
INPUTS = ('.devcontainer/requirements-native.in', '.devcontainer/requirements-native-tooling.in')
LOCK_NAMES = ('requirements-native.lock', 'requirements-native-tooling.lock')
RECEIPT = '.external/native-bootstrap.json'
ACTIVE_LOG = None  # Set only within an explicit mutating CLI attempt.
HISTORICAL_MISSING_NON_RUNTIME = frozenset((
    '.vscode/.gitignore', '.vscode/launch.json', '.vscode/tasks.json', 'AGENTS.md',
))
HISTORICAL_EXPANDED_DOC_SYMLINKS = frozenset((
    'doc/manual/source/figures', 'doc/tutorial/source/figures',
))


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def environment_hash(path, *, allow_links=False):
    digest = hashlib.sha256()
    for file in sorted(path.rglob('*')):
        if '__pycache__' in file.parts or file.suffix == '.pyc':
            continue
        if file.is_symlink():
            if not allow_links:
                raise ValueError(f'Symlink in private environment: {file}')
            digest.update(file.relative_to(path).as_posix().encode() + b'\0link:'
                          + os.readlink(file).encode() + b'\n')
            continue
        if not file.is_file():
            continue
        digest.update(file.relative_to(path).as_posix().encode() + b'\0' + sha256(file).encode() + b'\n')
    return digest.hexdigest()


def canonical(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def workspace_path(root, value):
    path = Path(value)
    if path.is_absolute() or '..' in path.parts or not path.parts:
        raise ValueError('Expected a path relative to the workspace')
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root.resolve()) or resolved == root.resolve():
        raise ValueError('Path resolves outside the workspace')
    return resolved


def command(argv, *, cwd=None, env=None, input=None, log=None, check=True):
    log = log if log is not None else ACTIVE_LOG
    argv = [str(v) for v in argv]
    if log is None:
        result = subprocess.run(argv, cwd=cwd, env=env, input=input, text=True, capture_output=True)
        stdout, stderr = result.stdout, result.stderr
    else:
        started = time.monotonic_ns()
        with Path(log).open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(dict(event='command_start', argv=argv, cwd=str(cwd) if cwd else None,
                source_sha256=sha256(Path(__file__)), wall_time_ns=time.time_ns(),
                host_monotonic_ns=started)) + '\n')
        result, error = None, None
        # File descriptors preserve complete partial output when run() raises.
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            try:
                result = subprocess.run(argv, cwd=cwd, env=env, input=input, text=True,
                                        stdout=output, stderr=errors)
            except BaseException as exception:
                error = type(exception).__name__
                raise
            finally:
                output.seek(0)
                errors.seek(0)
                stdout = output.read().decode('utf-8', errors='replace')
                stderr = errors.read().decode('utf-8', errors='replace')
                ended = time.monotonic_ns()
                with Path(log).open('a', encoding='utf-8') as stream:
                    stream.write('[stdout]\n' + stdout + '\n[stderr]\n' + stderr + '\n')
                    stream.write(json.dumps(dict(event='command_end', argv=argv,
                        returncode=result.returncode if result is not None else None, exception=error,
                        host_monotonic_ns=ended, elapsed_ns=ended - started)) + '\n')
    if result.returncode and check:
        raise RuntimeError(f'Command failed ({result.returncode}): {argv[0]}\n'
                           + stderr[-6000:] + stdout[-6000:])
    return stdout.strip()


def docker_command(root, image, script, *, network=False, readonly=False, cidfile=None):
    args = ['docker', 'run', '--rm', '-i', '--network', 'bridge' if network else 'none']
    if cidfile is not None:
        args += ['--cidfile', str(cidfile)]
    if hasattr(os, 'getuid'):
        args += ['--user', f'{os.getuid()}:{os.getgid()}']
    if readonly:
        args += ['--read-only', '--tmpfs', '/tmp:rw,nosuid,mode=1777']
    args += ['-e', 'HOME=/tmp/bas-native-build-home', '-e', 'PYTHONDONTWRITEBYTECODE=1',
             '-e', 'PIP_DISABLE_PIP_VERSION_CHECK=1', '-e', 'PIP_NO_CACHE_DIR=1',
             '-v', f'{root.resolve()}:{MOUNT}' + (':ro' if readonly else ''),
             '-w', MOUNT, image, 'bash', '-euc', script]
    return args


def container(root, image, script, *, network=False, readonly=False, log=None):
    with tempfile.TemporaryDirectory(prefix='bas-native-docker-') as temporary:
        cidfile = Path(temporary) / 'container.cid'
        try:
            return command(docker_command(root, image, 'mkdir -p "$HOME"\n' + script,
                           network=network, readonly=readonly, cidfile=cidfile), log=log)
        finally:
            if cidfile.exists():
                cid = cidfile.read_text().strip()
                if not re.fullmatch(r'[a-f0-9]{64}', cid):
                    raise ValueError('Invalid owned Docker cidfile; refusing unchecked cleanup')
                # --rm may already have removed it; retain the actual cleanup exit
                # code in the attempt log without hiding the primary build error.
                command(['docker', 'container', 'rm', '--force', cid], log=log, check=False)


def image_id(image):
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', image):
        raise ValueError('Use an exact sha256 image ID, not a mutable tag')
    actual = command(['docker', 'image', 'inspect', '--format', '{{.Id}}', image])
    if actual != image:
        raise ValueError('Docker image ID does not match the requested image')
    return actual


def direct_pins(root):
    pins = {}
    for name in INPUTS:
        for line in (root / name).read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            match = re.fullmatch(r'([\w.-]+)==([\w.+-]+)', line)
            if not match:
                raise ValueError(f'Not an exact dependency pin: {name}: {line}')
            key, version = canonical(match[1]), match[2]
            if key in pins:
                raise ValueError(f'Duplicate direct dependency: {key}')
            pins[key] = version
    return pins


def wheel_record(path):
    with zipfile.ZipFile(path) as archive:
        names = [n for n in archive.namelist() if n.endswith('.dist-info/METADATA')]
        if len(names) != 1:
            raise ValueError(f'Wheel needs exactly one METADATA: {path.name}')
        metadata = BytesParser().parsebytes(archive.read(names[0]))
    name, version = metadata['Name'], metadata['Version']
    if not name or not version or not re.fullmatch(r'[\w.+-]+', version):
        raise ValueError(f'Invalid wheel metadata: {path.name}')
    return dict(name=canonical(name), version=version, filename=path.name, sha256=sha256(path))


def lock_text(records):
    return ''.join(f"{r['name']}=={r['version']} --hash=sha256:{r['sha256']}\n"
                   for r in sorted(records, key=lambda r: r['name']))


def freeze_wheels(root, stage, image, python_info):
    records = [wheel_record(p) for p in sorted((stage / 'wheels').glob('*.whl'))]
    names = [r['name'] for r in records]
    if len(set(names)) != len(names) or not records:
        raise ValueError('Expected one wheel per resolved distribution')
    versions = {r['name']: r['version'] for r in records}
    if any(versions.get(k) != v for k, v in direct_pins(root).items()):
        raise ValueError('Resolved wheels do not match every direct pin')
    if python_info.get('major_minor') != '3.10' or python_info.get('machine') != 'x86_64':
        raise ValueError('Resolve wheels in the Linux amd64 Python 3.10 runtime')
    if python_info.get('platform') != 'linux':
        raise ValueError('Native wheel bundle must target Linux')
    for name in LOCK_NAMES:
        tooling = name == LOCK_NAMES[1]
        selected = [r for r in records if (r['name'] == 'cmake') == tooling]
        (stage / name).write_text(lock_text(selected), encoding='utf-8')
    manifest = dict(schema_version=1, image_id=image, python=python_info,
                    inputs={n: sha256(root / n) for n in INPUTS}, wheels=records,
                    locks={n: sha256(stage / n) for n in LOCK_NAMES},
                    scope='Replay of these built wheels; online resolution/build is not bitwise reproducible')
    (stage / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


def validate_bundle(root, bundle, image):
    manifest = json.loads((bundle / 'manifest.json').read_text())
    if manifest.get('schema_version') != 1 or manifest.get('image_id') != image:
        raise ValueError('Wheel bundle schema/image does not match')
    info = manifest.get('python', {})
    if info.get('major_minor') != '3.10' or info.get('machine') != 'x86_64' or info.get('platform') != 'linux':
        raise ValueError('Wheel bundle is not Linux amd64 Python 3.10')
    if manifest.get('inputs') != {n: sha256(root / n) for n in INPUTS}:
        raise ValueError('Wheel bundle direct-pin input hashes changed')
    records = [wheel_record(p) for p in sorted((bundle / 'wheels').glob('*.whl'))]
    if records != manifest.get('wheels'):
        raise ValueError('Wheel bundle contents/hash mismatch')
    versions = {r['name']: r['version'] for r in records}
    if len(versions) != len(records) or any(versions.get(k) != v for k, v in direct_pins(root).items()):
        raise ValueError('Wheel bundle has missing/duplicate/wrong direct dependencies')
    for name in LOCK_NAMES:
        selected = [r for r in records if (r['name'] == 'cmake') == (name == LOCK_NAMES[1])]
        if (bundle / name).read_text() != lock_text(selected):
            raise ValueError('Wheel lock content does not match the wheelhouse')
        if sha256(bundle / name) != manifest.get('locks', {}).get(name):
            raise ValueError('Wheel lock hash mismatch')
    return manifest


def resolve_lock(root, bundle, image):
    if bundle.exists():
        raise ValueError('Refusing to replace an existing wheel bundle')
    bundle.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.native-lock-', dir=bundle.parent) as temporary:
        stage = Path(temporary)
        relative = stage.relative_to(root).as_posix()
        script = ('mkdir -p "$HOME"\n'
                  'python3 -c \'import sys; assert sys.version_info[:2] == (3, 10)\'\n'
                  f'python3 -m pip wheel --wheel-dir {shlex.quote(relative + "/wheels")} '
                  '-r .devcontainer/requirements-native.in '
                  '-r .devcontainer/requirements-native-tooling.in\n'
                  'python3 -c \'import sys,platform,json; from importlib.metadata import version; '
                  'print(json.dumps(dict(major_minor="3.10",version=platform.python_version(),'
                  'machine=platform.machine(),platform=sys.platform,pip=version("pip"))))\'')
        output = container(root, image, script, network=True)
        info = json.loads(output.splitlines()[-1])
        freeze_wheels(root, stage, image, info)
        # A real offline resolver/install verifies transitive closure and ABI tags.
        container(root, image, install_script(relative, f'{relative}/probe-runtime', LOCK_NAMES[0])
                  + '\n' + install_script(relative, f'{relative}/probe-tooling', LOCK_NAMES[1]))
        shutil.rmtree(stage / 'probe-runtime')
        shutil.rmtree(stage / 'probe-tooling')
        stage.rename(bundle)


def install_script(bundle, target, lock):
    # All values come from validated workspace paths or fixed filenames.
    q = shlex.quote
    return (f'python3 -m pip install --no-index --ignore-installed '
            f'--require-hashes --only-binary=:all: --find-links {q(bundle + "/wheels")} '
            f'--target {q(target)} -r {q(bundle + "/" + lock)}')


def environment_script(target, expected):
    return ('python3 - <<\'PY\'\nimport sys,re,importlib.metadata as m\n'
            'assert sys.version_info[:2] == (3,10)\n'
            f'p={target!r}\nrows=list(m.distributions(path=[p]))\n'
            'd={re.sub(r"[-_.]+","-",x.metadata["Name"]).lower():x.version for x in rows}\n'
            'assert len(d)==len(rows), "Duplicate installed distributions"\n'
            f'assert d == {expected!r}, "Private environment differs from the wheel lock"\nPY')


def environment_versions(manifest, tooling):
    return {r['name']: r['version'] for r in manifest['wheels']
            if (r['name'] == 'cmake') == tooling}


def patches(root):
    return [root / 'network/ns3/patches' / name for name in PATCH_NAMES]


def verify_sdk(root, sdk):
    if command(['git', '-c', f'safe.directory={sdk}', '-C', sdk, 'rev-parse', 'HEAD']) != NS3_SHA:
        raise ValueError('Existing ns-3 checkout is not the pinned revision; left untouched')
    # A temporary Git index gives the exact HEAD+patches tree without changing
    # the user's index/objects or trusting reverse-check alone to exclude edits.
    with tempfile.TemporaryDirectory(prefix='bas-native-index-') as temporary:
        git = ['git', '-c', f'safe.directory={sdk}', '-C', sdk]
        original_objects = Path(command(git + ['rev-parse', '--git-path', 'objects']))
        if not original_objects.is_absolute():
            original_objects = sdk / original_objects
        objects = Path(temporary) / 'objects'
        objects.mkdir()
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(temporary) / 'index'), GIT_OPTIONAL_LOCKS='0',
                   GIT_OBJECT_DIRECTORY=str(objects),
                   GIT_ALTERNATE_OBJECT_DIRECTORIES=str(original_objects.resolve()))
        command(git + ['read-tree', NS3_SHA], env=env)
        for patch in patches(root):
            command(git + ['apply', '--cached', patch], env=env)
        def entries(output):
            fields = output.split('\0')
            if fields[-1] == '':
                fields.pop()
            if len(fields) % 2:
                raise RuntimeError('Malformed NUL-delimited Git name/status output')
            return list(zip(fields[::2], fields[1::2]))

        def allowed_layout(status, name):
            path = sdk / name
            if status != 'D' or path.is_symlink():
                return None
            if name in HISTORICAL_MISSING_NON_RUNTIME and not path.exists():
                return 'missing'
            if name in HISTORICAL_EXPANDED_DOC_SYMLINKS and path.is_dir():
                # This archive expanded two documentation links. Verify the
                # pinned original type; do not claim identity of their contents.
                entry = command(git + ['ls-tree', '-z', NS3_SHA, '--', name], env=env).rstrip('\0')
                metadata, separator, original_name = entry.partition('\t')
                if separator and original_name == name and metadata.split()[0] == '120000':
                    return 'expanded_doc_symlink'
            return None

        differences = entries(command(git + ['diff', '--name-status', '-z', '--no-renames', '--'], env=env))
        layouts = {}
        for status, name in differences:
            layout = allowed_layout(status, name)
            if not layout:
                raise RuntimeError(f'Existing SDK differs from the exact patched tree: {status} {name}; left untouched')
            layouts[name] = layout

        # A staged-only user change must not disappear behind the temporary
        # index. Staged exact approved patches/deletions remain acceptable.
        original_index = Path(command(git + ['rev-parse', '--git-path', 'index']))
        if not original_index.is_absolute():
            original_index = sdk / original_index
        original_env = dict(env, GIT_INDEX_FILE=str(original_index.resolve()))
        staged = entries(command(git + ['diff', '--cached', '--name-status', '-z', '--no-renames', NS3_SHA, '--'],
                                 env=original_env))
        if staged:
            expected_tree = command(git + ['write-tree'], env=env)
            for status, name in staged:
                if not allowed_layout(status, name):
                    command(git + ['diff', '--cached', '--quiet', '--exit-code', expected_tree, '--', name],
                            env=original_env)
        missing = sorted(name for name, layout in layouts.items() if layout == 'missing')
        expanded = sorted(name for name, layout in layouts.items() if layout == 'expanded_doc_symlink')
        return dict(ns3_sha=NS3_SHA,
                    verification_scope=('exact_patched_tree_except_listed_non_runtime_packaging' if expanded
                                        else 'exact_patched_tree_except_listed_missing_non_runtime_files'
                                        if missing else 'exact_patched_tree'),
                    allowed_missing_non_runtime=missing,
                    expanded_non_runtime_doc_symlinks=expanded)


def ensure_sdk(root):
    sdk = workspace_path(root, SDK_PATH)
    if (root / SDK_PATH).is_symlink():
        raise ValueError('Refusing a symlinked SDK; use a separate clean workspace')
    if sdk.exists() or sdk.is_symlink():
        verify_sdk(root, sdk)
        return sdk
    sdk.parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.native-ns3-', dir=sdk.parent) as temporary:
        stage = Path(temporary) / 'checkout'
        command(['git', 'clone', '--filter=blob:none', '--branch', 'ns-3.48', '--single-branch',
                 NS3_REPOSITORY, stage])
        if command(['git', '-C', stage, 'rev-parse', 'HEAD']) != NS3_SHA:
            raise ValueError('Official ns-3.48 fetch resolved to a different revision')
        for patch in patches(root):
            command(['git', '-C', stage, 'apply', '--check', patch])
            command(['git', '-C', stage, 'apply', patch])
        stage.rename(sdk)
    return sdk


def input_identity(root, bundle, image):
    sources = [*patches(root), *(root / 'network/ns3/scratch' / n for n in SCRATCH_FILES),
               *(p for p in (root / 'src').rglob('*') if p.is_file() and '__pycache__' not in p.parts)]
    return dict(image_id=image, ns3_sha=NS3_SHA, bundle_sha256=sha256(bundle / 'manifest.json'),
                files={p.relative_to(root).as_posix(): sha256(p) for p in sorted(sources)})


def verify_cache(root):
    path = root / SDK_PATH / 'cmake-cache/CMakeCache.txt'
    if not path.is_file():
        raise ValueError('Native CMake cache is absent')
    required = 'CMAKE_HOME_DIRECTORY:INTERNAL=' + MOUNT + '/' + SDK_PATH
    if required not in path.read_text().splitlines():
        raise ValueError('CMake cache points outside the canonical SDK mount; left untouched')


def inspect(root, image, bundle):
    gaps = []
    sdk_verification = {}
    def check(name, fn):
        try:
            fn()
        except (ValueError, RuntimeError, OSError, KeyError, json.JSONDecodeError, zipfile.BadZipFile) as error:
            gaps.append(dict(component=name, reason=str(error)))
    check('image', lambda: image_id(image))
    check('wheel_bundle', lambda: validate_bundle(root, bundle, image))
    check('ns3', lambda: sdk_verification.update(verify_sdk(root, root / SDK_PATH)))
    check('cmake_cache', lambda: verify_cache(root))
    for name in SCRATCH_FILES:
        src, dst = root / 'network/ns3/scratch' / name, root / SDK_PATH / 'scratch' / name
        if not dst.is_file() or sha256(src) != sha256(dst):
            gaps.append(dict(component='scratch', reason=f'Not synchronized: {name}'))
    def receipt_check():
        receipt = json.loads((root / RECEIPT).read_text())
        if receipt.get('inputs') != input_identity(root, bundle, image) or receipt.get('status') != 'built':
            raise ValueError('Missing current successful build receipt; existing install is not qualified')
        for path, digest in receipt['outputs'].items():
            if sha256(root / path) != digest:
                raise ValueError(f'Built output changed: {path}')
        for directory in ['.python-deps-py310', '.tooling-py310']:
            if environment_hash(root / SDK_PATH / directory) != receipt.get('environments', {}).get(directory):
                raise ValueError(f'Private environment files changed or not recorded: {directory}')
        for directory in ['build', 'install']:
            if environment_hash(root / directory, allow_links=True) != receipt.get('ros_trees', {}).get(directory):
                raise ValueError(f'ROS build/install files changed or not recorded: {directory}')
    check('build_receipt', receipt_check)
    if not any(g['component'] == 'image' for g in gaps):
        check('python_abi', lambda: container(root, image,
              'python3 -c \'import sys; assert sys.version_info[:2] == (3, 10)\'', readonly=True))
        if not any(g['component'] == 'wheel_bundle' for g in gaps):
            manifest = validate_bundle(root, bundle, image)
            for directory, tooling in [('.python-deps-py310', False), ('.tooling-py310', True)]:
                check(directory, lambda d=directory, t=tooling: container(root, image,
                      environment_script(f'{SDK_PATH}/{d}', environment_versions(manifest, t)), readonly=True))
    return dict(schema_version=1, mode='check_only', image_id=image, ns3_sha=NS3_SHA,
                native_sdk_source_verification=sdk_verification or None,
                ready=not gaps, gaps=gaps, gpu_checked=False, runtime_started=False,
                clean_machine_acceptance='not_performed')


def bootstrap(root, image, bundle):
    receipt_path = workspace_path(root, RECEIPT)
    receipt = json.loads(receipt_path.read_text()) if receipt_path.exists() else None
    if any((root / p).is_symlink() for p in ['build', 'install', 'log']):
        raise ValueError('Refusing symlinked ROS output directories; use a separate clean workspace')
    if not receipt and any((root / p).exists() for p in ['build', 'install', 'log']):
        raise ValueError('Unmanaged build/install/log exists; use a separate clean workspace')
    if not receipt and any((root / SDK_PATH / p).exists() for p in ['.python-deps-py310', '.tooling-py310']):
        raise ValueError('Unmanaged private environment exists; use a separate clean workspace')
    validate_bundle(root, bundle, image)
    container(root, image, 'python3 -c \'import sys,platform; '
              'assert sys.version_info[:2] == (3,10) and platform.machine() == "x86_64"\'', readonly=True)
    identity = input_identity(root, bundle, image)
    if receipt and receipt.get('inputs') != identity:
        raise ValueError('Build inputs changed; use a separate clean workspace, existing outputs left untouched')
    if receipt:
        for path, digest in receipt.get('outputs', {}).items():
            output = workspace_path(root, path)
            if not output.is_file() or sha256(output) != digest:
                raise ValueError(f'Existing built output changed; left untouched: {path}')
        for directory in ['build', 'install']:
            if (root / directory).exists() and environment_hash(root / directory, allow_links=True) != receipt.get('ros_trees', {}).get(directory):
                raise ValueError(f'Existing ROS outputs changed or incomplete; left untouched: {directory}')
    sdk = ensure_sdk(root)
    sdk_verification = verify_sdk(root, sdk)
    if (sdk / 'cmake-cache/CMakeCache.txt').exists():
        verify_cache(root)
    for name in SCRATCH_FILES:
        src, dst = root / 'network/ns3/scratch' / name, sdk / 'scratch' / name
        if dst.is_symlink() or (dst.exists() and dst.read_bytes() != src.read_bytes()):
            raise ValueError(f'Existing scratch file differs; left untouched: {dst}')
        if not dst.exists():
            shutil.copyfile(src, dst)
    record = receipt or dict(status='installing', inputs=identity, environments={})
    record['native_sdk_source_verification'] = sdk_verification
    receipt_path.write_text(json.dumps(record, indent=2) + '\n')
    for directory, lock in [('.python-deps-py310', LOCK_NAMES[0]), ('.tooling-py310', LOCK_NAMES[1])]:
        target = sdk / directory
        if target.is_symlink():
            raise ValueError(f'Refusing a symlinked private environment: {target}')
        expected = environment_versions(validate_bundle(root, bundle, image), lock == LOCK_NAMES[1])
        if target.exists():
            if environment_hash(target) != record.get('environments', {}).get(directory):
                raise ValueError(f'Existing private environment files changed; left untouched: {target}')
            # Read metadata from the target only, never from fallback image packages.
            container(root, image, environment_script(target.relative_to(root).as_posix(), expected), readonly=True)
        else:
            with tempfile.TemporaryDirectory(prefix='.native-python-', dir=sdk) as temporary:
                stage = Path(temporary) / 'target'
                container(root, image, install_script(bundle.relative_to(root).as_posix(),
                          stage.relative_to(root).as_posix(), lock))
                container(root, image, environment_script(stage.relative_to(root).as_posix(), expected), readonly=True)
                stage.rename(target)
            record['environments'][directory] = environment_hash(target)
            receipt_path.write_text(json.dumps(record, indent=2) + '\n')
    record['status'] = 'building'
    receipt_path.write_text(json.dumps(record, indent=2) + '\n')
    log = root / '.external/native-bootstrap.log'
    container(root, image, 'set +u\nsource /opt/ros/humble/setup.bash\n'
              'source /workspace/ardu_ws/install/setup.bash\n'
              'colcon build --packages-select multiagent_simulation --symlink-install\n'
              'source install/setup.bash\nros2 pkg prefix multiagent_simulation', log=log)
    container(root, image, f'cd {SDK_PATH}\n'
              'export PATH="$PWD/.tooling-py310/bin:$PATH"\n'
              'export PYTHONPATH="$PWD/.tooling-py310:$PWD/.python-deps-py310"\n'
              'if [[ ! -f cmake-cache/CMakeCache.txt ]]; then\n'
              '  ./ns3 configure --enable-examples --enable-tests --enable-python-bindings\nfi\n'
              './ns3 build upstream-sionna-tap-spike\n./ns3 build native-rt-worker', log=log)
    verify_cache(root)
    outputs = ['install/setup.bash',
               SDK_PATH + '/cmake-cache/CMakeCache.txt',
               SDK_PATH + '/build/scratch/ns3.48-upstream-sionna-tap-spike-default',
               SDK_PATH + '/build/scratch/ns3.48-native-rt-worker-default',
               SDK_PATH + '/build/lib/libns3.48-spectrum-default.so',
               SDK_PATH + '/build/lib/libns3.48-tap-bridge-default.so']
    result = dict(schema_version=1, status='built', inputs=identity,
                  native_sdk_source_verification=sdk_verification,
                  environments={d: environment_hash(sdk / d) for d in ['.python-deps-py310', '.tooling-py310']},
                  ros_trees={d: environment_hash(root / d, allow_links=True) for d in ['build', 'install']},
                  outputs={p: sha256(root / p) for p in outputs}, runtime_started=False,
                  gpu_checked=False, clean_machine_acceptance='not_performed')
    receipt_path.write_text(json.dumps(result, indent=2) + '\n')
    return result


@contextmanager
def mutating_attempt(root, options):
    global ACTIVE_LOG
    previous_log = ACTIVE_LOG
    log = workspace_path(root, '.external/native-bootstrap.log')
    log.parent.mkdir(parents=True, exist_ok=True)
    ACTIVE_LOG = log
    started = time.monotonic_ns()
    previous_term = signal.getsignal(signal.SIGTERM)

    def interrupted(signum, frame):
        raise KeyboardInterrupt(signum)

    status, error = 'completed', None
    try:
        with log.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(dict(event='attempt_start', options=options,
                source_sha256=sha256(Path(__file__)), ns3_declared_sha=NS3_SHA,
                direct_input_hashes={p: sha256(root / p) for p in INPUTS},
                wall_time_ns=time.time_ns(), host_monotonic_ns=started)) + '\n')
        signal.signal(signal.SIGTERM, interrupted)
        yield
    except BaseException as exception:
        status = 'cancelled' if isinstance(exception, KeyboardInterrupt) else 'failed'
        error = type(exception).__name__
        raise
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        ACTIVE_LOG = previous_log
        ended = time.monotonic_ns()
        with log.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(dict(event='attempt_end', status=status, exception=error,
                host_monotonic_ns=ended, elapsed_ns=ended - started)) + '\n')


@contextmanager
def runtime_lock():
    import fcntl
    # Same lock as bas.RuntimeLease: build cannot race a running native stack.
    with open('/tmp/bas-paper-runtime.lock', 'a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('BAS runtime/build lock is held; no build work performed') from None
        yield


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--check-only', action='store_true')
    action.add_argument('--resolve-lock', action='store_true', help='Online wheel resolution in the pinned image')
    action.add_argument('--bootstrap', action='store_true', help='Hashed wheel installation/build; fetch SDK only if absent')
    parser.add_argument('--image', help='Exact local image ID; defaults to runtime.json')
    parser.add_argument('--lock-bundle', default='.external/native-runtime-wheels')
    args = parser.parse_args(argv)
    try:
        image = args.image or json.loads((ROOT / 'runtime.json').read_text())['runtime']['image']
        bundle = workspace_path(ROOT, args.lock_bundle)
        if args.check_only:
            result = inspect(ROOT, image, bundle)
            print(json.dumps(result, indent=2))
            return 0 if result['ready'] else 1
        if sys.platform != 'linux' or not hasattr(os, 'getuid') or os.getuid() == 0:
            raise ValueError('Build/lock resolution requires a non-root Linux host user')
        options = dict(vars(args), image=image, argv=list(argv) if argv is not None else sys.argv[1:])
        with mutating_attempt(ROOT, options), runtime_lock():
            image_id(image)
            if args.resolve_lock:
                resolve_lock(ROOT, bundle, image)
                result = dict(status='lock_resolved', bundle=str(bundle),
                              manifest_sha256=sha256(bundle / 'manifest.json'), runtime_started=False)
            else:
                result = bootstrap(ROOT, image, bundle)
        print(json.dumps(result, indent=2))
        return 0
    except KeyboardInterrupt as error:
        signum = error.args[0] if error.args and error.args[0] in (signal.SIGINT, signal.SIGTERM) else signal.SIGINT
        print(json.dumps(dict(status='cancelled', signal=int(signum), runtime_started=False)), file=sys.stderr)
        return 128 + int(signum)
    except (ValueError, RuntimeError, OSError, KeyError, json.JSONDecodeError, zipfile.BadZipFile) as error:
        print(json.dumps(dict(status='failed', error=str(error), runtime_started=False)), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
