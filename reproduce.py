#!/usr/bin/env python3
"""Download the article artifacts, recalculate results, or start a new experiment."""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parent
STUDY = ROOT / 'runs/urban-ns3-20261028'
IMAGE = 'sha256:89d78eff9914b1644a1b01b793612e9ee8b19916c5a7bf5f578ba6d8bfbfafe5'


def run(*args, **kwargs):
    subprocess.run(list(map(str, args)), cwd=ROOT, check=True, **kwargs)


def unpack_source():
    with tarfile.open(ROOT / 'source.tar.gz') as archive:
        for member in archive.getmembers():
            target = (ROOT / member.name).resolve()
            if ROOT not in target.parents or member.issym() or member.islnk():
                raise ValueError('Unexpected source archive member')
        archive.extractall(ROOT)


def fetch(group):
    manifest = json.loads((ROOT / 'artifacts.json').read_text())
    cache = ROOT / 'downloads'
    cache.mkdir(exist_ok=True)
    for artifact in manifest['artifacts']:
        if artifact['group'] != group:
            continue
        parts = []
        for part in artifact['parts']:
            path = cache / part['name']
            if not path.exists():
                print('Downloading', part['name'], flush=True)
                temp = path.with_suffix(path.suffix + '.partial')
                with urllib.request.urlopen(part['url']) as source, temp.open('wb') as target:
                    shutil.copyfileobj(source, target)
                temp.rename(path)
            with path.open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest() if hasattr(hashlib, 'file_digest') else None
            if digest is None:
                h = hashlib.sha256()
                with path.open('rb') as stream:
                    for block in iter(lambda: stream.read(8 * 1024 * 1024), b''):
                        h.update(block)
                digest = h.hexdigest()
            if path.stat().st_size != part['bytes'] or digest != part['sha256']:
                raise ValueError('Download differs from the published file: ' + path.name)
            parts.append(path)
        print('Unpacking', artifact['name'], flush=True)
        decoder = subprocess.Popen(['zstd', '-dc'], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        command = ['docker', 'image', 'load'] if artifact['name'] == 'runtime-image' else ['tar', '-xf', '-', '-C', str(ROOT)]
        consumer = subprocess.Popen(command, stdin=decoder.stdout)
        decoder.stdout.close()
        try:
            for part in parts:
                with part.open('rb') as stream:
                    shutil.copyfileobj(stream, decoder.stdin)
        finally:
            decoder.stdin.close()
        if consumer.wait() or decoder.wait():
            raise RuntimeError('Archive extraction failed')


def rows(path):
    with path.open(newline='', encoding='utf-8') as stream:
        return list(csv.DictReader(stream))


def tables():
    saved = STUDY / 'results-final'
    pairs = rows(saved / 'pairs.csv')
    measurements = {r['run_id']: r for r in rows(saved / 'tables/descriptive_runs.csv')}
    print('radio,policy,n,q250_percent,waypoints_percent,O,C,A,U,T')
    for mode in ('geometry', 'los_only'):
        selected = [r for r in pairs if r['cohort'] == 'main' and r['radio_mode'] == mode
                    and r['metric'] == 'timely_per_offered' and r['complete'] == 'True']
        for policy in ('fixed', 'failover'):
            ids = [r[policy + '_run_id'] for r in selected]
            m = [measurements[i] for i in ids]
            q = statistics.mean(float(r[policy]) for r in selected) * 100
            w = statistics.mean(float(r['physical_waypoints']) / float(r['planned_waypoints']) for r in m) * 100
            stages = [sum(int(float(r[k])) for r in m) for k in ('offered', 'target_CIDs_created_in_window',
                      'follower_application_receive_CIDs', 'UART_write_end_CIDs', 'timely')]
            print(','.join(map(str, [mode, policy, len(ids), f'{q:.3f}', f'{w:.3f}', *stages])))


def replay(raw):
    unpack_source()
    analysis = STUDY / 'analysis'
    output = ROOT / 'recalculated'
    if output.exists():
        raise ValueError('recalculated/ already exists; move it before another calculation')
    if raw:
        analysis = output / 'analysis'
        frozen = json.loads((STUDY / 'freeze.json').read_text())
        for case in frozen['cases']:
            run(sys.executable, 'scripts/product/analyze_urban_experiment.py', 'run',
                ROOT / 'runs/native-radio-realtime' / case['run_id'], '--output', analysis / case['run_id'])
    run(sys.executable, 'scripts/product/analyze_urban_study.py', '--freeze', STUDY / 'freeze.json',
        '--analysis-root', analysis, '--controllers-root', STUDY / 'controllers',
        '--runs-root', ROOT / 'runs/native-radio-realtime', '--output', output / 'results')


def repeat(name):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', name):
        raise ValueError('Use letters, numbers, underscores and hyphens in the study ID')
    directory = ROOT / 'runs' / name
    if directory.exists():
        raise ValueError('This study ID already exists')
    unpack_source()
    frozen = json.loads((STUDY / 'freeze.json').read_text())
    # ROS installation paths are generated on the receiving host, not copied from PC4.
    run('docker', 'run', '--rm', '--network', 'none', '--user', '0:0',
        '-v', str(ROOT) + ':/workspace/multiagent_simulation',
        '-w', '/workspace/multiagent_simulation', IMAGE, 'bash', '-lc',
        'source /opt/ros/humble/setup.bash && source /workspace/ardu_ws/install/setup.bash && '
        'colcon build --packages-select multiagent_simulation')
    baseline = ROOT / 'runs/paper-live-20261026/campaign/freeze.json'
    if not baseline.exists():
        baseline.parent.mkdir(parents=True, exist_ok=True)
        baseline.write_text(json.dumps({'base_environment': frozen['base_environment']}))
    frozen['source_commit'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    frozen['study_id'] = name
    native = ROOT / '.external/ns-3-sionna-native/build/scratch/ns3.48-upstream-sionna-tap-spike-default'
    if hashlib.sha256(native.read_bytes()).hexdigest() != frozen['native_binary']['sha256']:
        raise ValueError('The native executable differs from the measured version')
    for case in frozen['cases']:
        case['run_id'] = name + '-' + case['case_id']
        case['config'] = 'network/config/' + Path(case['config']).name
    directory.mkdir(parents=True)
    path = directory / 'freeze.json'
    path.write_text(json.dumps(frozen, indent=2) + '\n')
    run(sys.executable, 'scripts/product/run_frozen_urban_study.py', '--freeze', path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('source', help='Unpack the recorded source files')
    download = sub.add_parser('fetch', help='Download and unpack release artifacts')
    download.add_argument('group', choices=['journals', 'runtime'])
    sub.add_parser('tables', help='Print article tables 1 and 3 from saved per-run results')
    calculate = sub.add_parser('replay', help='Recalculate the statistical aggregate')
    calculate.add_argument('--raw', action='store_true', help='First recalculate each run from its journals')
    experiment = sub.add_parser('repeat', help='Start all 56 new simulation runs on a prepared Linux host')
    experiment.add_argument('study_id')
    args = parser.parse_args()
    if args.command == 'source': unpack_source()
    elif args.command == 'fetch': fetch(args.group)
    elif args.command == 'tables': tables()
    elif args.command == 'replay': replay(args.raw)
    elif args.command == 'repeat': repeat(args.study_id)


if __name__ == '__main__':
    main()
