#!/usr/bin/env python3
"""Fetch/check immutable delivery dependencies; reuse the existing build/terrain tools.

The image is a dependency, not a prebuilt BAS checkout. Native binaries and the
ROS package must still be built in the receiving clone with bootstrap_native_runtime.
SPDX-License-Identifier: GPL-3.0
"""
import argparse
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.product import bootstrap_native_runtime as bootstrap

MANIFEST = ROOT / '.devcontainer/runtime-assets.json'


def verified(path, item):
    return (path.is_file() and not path.is_symlink()
            and path.stat().st_size == item['bytes']
            and bootstrap.sha256(path) == item['sha256'])


def fetch(item, repository, release, cache):
    """Download into an owned temporary file, verify before exposing to consumers."""
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / item['name']
    if verified(target, item):
        return target
    if target.exists() or target.is_symlink():
        raise ValueError(f'Existing asset has a wrong hash/size; left untouched: {target}')
    if Path(item['name']).name != item['name']:
        raise ValueError('Asset name must be a basename')
    url = f"https://github.com/{repository}/releases/download/{release}/{item['name']}"
    # gh handles authenticated private Releases without putting credentials in
    # command arguments, logs or URLs. urllib covers public assets without gh.
    with tempfile.TemporaryDirectory(prefix='.asset-', dir=cache) as temporary:
        stage = Path(temporary) / item['name']
        if repository.startswith('HSE-Intelligent-Unmanned-Systems-Lab/'):
            subprocess.run(['gh', 'release', 'download', release, '--repo', repository,
                            '--pattern', item['name'], '--dir', temporary], check=True)
        else:
            with urllib.request.urlopen(url, timeout=60) as response, stage.open('xb') as output:
                shutil.copyfileobj(response, output, length=1 << 20)
        if not verified(stage, item):
            raise ValueError(f'Download hash/size mismatch: {item["name"]}')
        stage.rename(target)
    return target


def get_image(manifest, cache):
    image = manifest['image_id']
    present = subprocess.run(['docker', 'image', 'inspect', image],
                             capture_output=True).returncode == 0
    if not present:
        parts = [fetch(p, manifest['repository'], manifest['release'], cache)
                 for p in manifest['parts']]
        script = ('cat ' + shlex.join(map(str, parts)) + ' | zstd -dc | docker image load')
        subprocess.run(['bash', '-o', 'pipefail', '-c', script], check=True)
    return bootstrap.image_id(image)


def get_wheels(manifest, cache):
    item = manifest['native_wheels']
    target = bootstrap.workspace_path(ROOT, item['output'])
    if not target.exists():
        archive = fetch(item, item['repository'], item['release'], cache)
        with tempfile.TemporaryDirectory(prefix='.wheels-', dir=target.parent) as temporary:
            stage = Path(temporary)
            with tarfile.open(archive) as package:
                members = package.getmembers()
                # This dependency bundle contains only directories and regular
                # wheels/locks; reject links, traversal and device entries.
                for member in members:
                    path = Path(member.name)
                    if (not path.parts or path.parts[0] != 'native-runtime-wheels'
                            or path.is_absolute() or '..' in path.parts
                            or not (member.isdir() or member.isfile())):
                        raise ValueError('Unsafe wheel bundle member')
                package.extractall(stage, members=members)
            (stage / 'native-runtime-wheels').rename(target)
    if bootstrap.sha256(target / 'manifest.json') != item['manifest_sha256']:
        raise ValueError('Wheel manifest differs from the published dependency bundle')
    bootstrap.validate_bundle(ROOT, target, manifest['image_id'])
    return str(target.relative_to(ROOT))


def check_terrain(manifest):
    terrain = manifest['terrain']
    source = bootstrap.workspace_path(ROOT, terrain['source'])
    if bootstrap.sha256(source) != terrain['source_sha256']:
        raise ValueError('Reference terrain input changed; use a new qualified resource manifest')
    output = bootstrap.workspace_path(ROOT, terrain['output'])
    if json.loads((output / 'source.json').read_text()) != json.loads(source.read_text()):
        raise ValueError('Terrain was prepared from different configuration')
    for name, digest in terrain['deterministic_files'].items():
        if bootstrap.sha256(output / name) != digest:
            raise ValueError(f'Reference terrain differs: {name}')
    # NPZ archive/manifest include build metadata. Validate their actual file
    # hashes internally rather than comparing timestamp-dependent archive bytes.
    snapshot = json.loads((output / 'scene-manifest.json').read_text())
    for name, value in snapshot['files'].items():
        path = bootstrap.workspace_path(output, name)
        if not verified(path, {'bytes': value['bytes'], 'sha256': value['sha256']}):
            raise ValueError(f'Terrain manifest file mismatch: {name}')
    return str(output.relative_to(ROOT))


def prepare_terrain(manifest):
    terrain = manifest['terrain']
    target = bootstrap.workspace_path(ROOT, terrain['output'])
    if target.exists():
        return check_terrain(manifest)
    deps = bootstrap.workspace_path(ROOT, '.external/terrain-deps')
    if deps.exists():
        raise ValueError('Terrain dependency directory already exists without prepared terrain; '
                         'use a fresh clone or preserve/rename that directory manually')
    deps.parent.mkdir(parents=True, exist_ok=True)
    bundle = bootstrap.workspace_path(ROOT, manifest['native_wheels']['output'])
    if bootstrap.sha256(bundle / 'manifest.json') != manifest['native_wheels']['manifest_sha256']:
        raise ValueError('Fetch the published --native-wheels bundle before preparing terrain')
    wheels = bootstrap.validate_bundle(ROOT, bundle, manifest['image_id'])
    numpy = next(w for w in wheels['wheels'] if w['name'] == 'numpy')
    if numpy['version'] != terrain['dependencies']['numpy']:
        raise ValueError('Terrain requires the declared NumPy version')
    with tempfile.TemporaryDirectory(prefix='.terrain-deps-', dir=deps.parent) as temporary:
        stage = Path(temporary) / 'target'
        lock = Path(temporary) / 'numpy.lock'
        lock.write_text(f'numpy=={numpy["version"]} --hash=sha256:{numpy["sha256"]}\n')
        target_arg = shlex.quote(str(stage.relative_to(ROOT)))
        bootstrap.container(ROOT, manifest['image_id'],
            'python3 -m pip install --no-deps --no-index --require-hashes --find-links '
            + shlex.quote(str(bundle.relative_to(ROOT) / 'wheels')) + ' --target '
            + target_arg + ' -r ' + shlex.quote(str(lock.relative_to(ROOT))) + '\n'
            'python3 -m pip install --no-deps --require-hashes --target '
            + target_arg + ' -r requirements-terrain.txt', network=True)
        stage.rename(deps)
    bootstrap.container(ROOT, manifest['image_id'],
        'export OPENBLAS_NUM_THREADS=1\nexport PYTHONPATH="$PWD/.external/terrain-deps"\n'
        'python3 -c \'import numpy, trimesh; assert numpy.__version__ == "1.26.4"; '
        'assert trimesh.__version__ == "4.10.1"\'\n'
        'python3 scripts/product/prepare_terrain_scene.py --config '
        + shlex.quote(terrain['source']) + ' --output ' + shlex.quote(terrain['output']))
    return check_terrain(manifest)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', action='store_true', help='Fetch/load exact base image if absent')
    parser.add_argument('--native-wheels', action='store_true', help='Fetch and hash-check published wheel bundle')
    parser.add_argument('--terrain', action='store_true', help='Generate reference geometry with pinned tools')
    parser.add_argument('--check-only', action='store_true', help='Check image, wheels and terrain without mutation/network')
    args = parser.parse_args(argv)
    if not any(vars(args).values()):
        parser.error('Select --image, --native-wheels, --terrain or --check-only')
    manifest = json.loads(MANIFEST.read_text())
    cache = bootstrap.workspace_path(ROOT, '.external/runtime-assets')
    result = {'schema_version': 1, 'manifest_sha256': bootstrap.sha256(MANIFEST),
              'native_build': 'not_performed', 'clean_machine_acceptance': 'not_performed'}
    try:
        if args.check_only:
            result['image_id'] = bootstrap.image_id(manifest['image_id'])
            item = manifest['native_wheels']
            bundle = bootstrap.workspace_path(ROOT, item['output'])
            if bootstrap.sha256(bundle / 'manifest.json') != item['manifest_sha256']:
                raise ValueError('Wheel bundle differs from published manifest')
            bootstrap.validate_bundle(ROOT, bundle, manifest['image_id'])
            result['native_wheels'] = item['output']
            result['terrain'] = check_terrain(manifest)
        else:
            with bootstrap.runtime_lock():
                if args.image:
                    result['image_id'] = get_image(manifest, cache)
                if args.native_wheels:
                    result['native_wheels'] = get_wheels(manifest, cache)
                if args.terrain:
                    result['terrain'] = prepare_terrain(manifest)
        result['status'] = 'verified'
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError, tarfile.TarError) as error:
        result.update(status='failed', error=str(error))
        print(json.dumps(result, indent=2), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
