#!/usr/bin/env python3
"""Bind a native run to executable, Wi-Fi and Spectrum mappings actually used.

Capture runs during warmup, outside the measurement window. Closure checks the
same files again; it does not claim continuous sampling of the process mappings.
The offline verifier reads only immutable run artifacts, never the current SDK.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import time


LEGACY_MODE = 'proc_maps_sha256_v1'
MODE = 'proc_maps_sha256_v2'
EXPECTED = 'logs/native_library_expected.json'
OBSERVED = 'logs/native_library_observed.json'
CLOSED = 'logs/native_library_closed.json'
MAPS = 'logs/native_process.maps'


def read_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f'duplicate field: {key}')
            result[key] = value
        return result
    def reject(value):
        raise ValueError(f'non-finite JSON constant: {value}')
    return json.loads(Path(path).read_text(encoding='utf-8'), object_pairs_hook=unique,
                      parse_constant=reject)


def write_new(path, value):
    with Path(path).open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(json.dumps(value, indent=2, allow_nan=False) + '\n')


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def file_identity(path):
    path = Path(path).resolve(strict=True)
    with path.open('rb') as stream:
        before = os.fstat(stream.fileno())
        value = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
        after = os.fstat(stream.fileno())
    fields = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    if fields(before) != fields(after) or fields(after) != fields(path.stat()):
        raise ValueError('file changed while hashing')
    return dict(path=str(path), sha256=value.hexdigest(), bytes=after.st_size,
                device_major=os.major(after.st_dev), device_minor=os.minor(after.st_dev),
                inode=after.st_ino, mtime_ns=after.st_mtime_ns, ctime_ns=after.st_ctime_ns)


def parse_maps(data):
    rows = []
    for line in data.decode('utf-8', errors='strict').splitlines():
        fields = line.split(None, 5)
        if len(fields) < 5:
            raise ValueError('malformed proc maps record')
        address, permissions, offset, device, inode = fields[:5]
        if not re.fullmatch(r'[0-9a-f]+-[0-9a-f]+', address) or not re.fullmatch(r'[r-][w-][x-][ps]', permissions):
            raise ValueError('invalid proc maps address or permissions')
        major, minor = device.split(':')
        rows.append(dict(address=address, permissions=permissions, offset=int(offset, 16),
                         device_major=int(major, 16), device_minor=int(minor, 16), inode=int(inode),
                         path=fields[5] if len(fields) == 6 else ''))
    if not rows:
        raise ValueError('empty process maps')
    return rows


def mapped(rows, identity):
    selected = [row for row in rows if row['path'] == identity['path']]
    if not selected or not any('x' in row['permissions'] for row in selected):
        raise ValueError('expected executable mapping is absent')
    for row in selected:
        if any(row[key] != identity[key] for key in ('device_major', 'device_minor', 'inode')):
            raise ValueError('mapped device/inode differs from hashed file')
    return selected


def library_paths(rows, name):
    return {row['path'] for row in rows if re.search(
        r'/libns3[^/]*-' + re.escape(name) + r'[^/]*\.so(?:\.[0-9.]+)?(?: \(deleted\))?$', row['path'])}


def wifi_paths(rows):
    return library_paths(rows, 'wifi')


def required_libraries(mode):
    if mode == LEGACY_MODE:
        return ('wifi',)
    if mode == MODE:
        return ('wifi', 'spectrum')
    raise ValueError('unsupported native library mode')


def expected_libraries(expected):
    if not isinstance(expected, dict):
        raise ValueError('expected native library evidence must be an object')
    names = required_libraries(expected.get('mode'))
    if not isinstance(expected.get('files'), dict) or set(expected['files']) != {'executable', *names}:
        raise ValueError('expected native library identities are missing or unexpected')
    return names


def process_identity(pid):
    proc = Path('/proc') / str(pid)
    # comm may contain spaces and ')'; starttime is field 22 after its final ')'.
    fields = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
    return dict(pid=pid, start_ticks=int(fields[19]), uid=proc.stat().st_uid,
                executable=os.readlink(proc / 'exe'),
                argv=(proc / 'cmdline').read_bytes().decode().rstrip('\0').split('\0'))


def prepare(run, executable, wifi, spectrum):
    run = Path(run).resolve()
    identities = dict(executable=file_identity(executable), wifi=file_identity(wifi),
                      spectrum=file_identity(spectrum))
    for name in required_libraries(MODE):
        if not library_paths([dict(path=identities[name]['path'])], name):
            raise ValueError(f'expected {name} library has an unexpected filename')
    value = dict(schema_version=1, mode=MODE, run_id=run.name, run_dir=str(run),
                 prepared_host_monotonic_ns=time.monotonic_ns(), files=identities)
    write_new(run / EXPECTED, value)
    return value


def observe(run, pid, timeout_s=15):
    run = Path(run).resolve()
    expected = read_json(run / EXPECTED)
    libraries = expected_libraries(expected)
    deadline = time.monotonic() + timeout_s
    birth = None
    # setsid/ip/env/taskset/stdbuf exec in-place. Wait for the owned PID to become
    # the exact executable and required libraries; do not select another PID.
    while True:
        try:
            before = process_identity(pid)
            current_birth = (before['pid'], before['start_ticks'], before['uid'])
            if birth is None:
                birth = current_birth
            elif current_birth != birth:
                raise ValueError('native PID was reused while awaiting its executable')
            maps = (Path('/proc') / str(pid) / 'maps').read_bytes()
            rows = parse_maps(maps)
            if before['executable'] != expected['files']['executable']['path']:
                raise ValueError('native PID has not execed the expected binary')
            if f'--statsFile={run}/metrics/native_radio_stats.json' not in before['argv']:
                raise ValueError('native process is not bound to this run')
            for name in libraries:
                if library_paths(rows, name) != {expected['files'][name]['path']}:
                    raise ValueError(f'expected {name} mapping is absent, deleted or ambiguous')
            for identity in expected['files'].values():
                mapped(rows, identity)
            break
        except (OSError, ValueError, IndexError) as error:
            if time.monotonic() >= deadline:
                raise ValueError(f'native mapping capture timed out: {error}') from error
            time.sleep(.01)
    actual = {name: file_identity(value['path']) for name, value in expected['files'].items()}
    after = process_identity(pid)
    final_rows = parse_maps((Path('/proc') / str(pid) / 'maps').read_bytes())
    if before != after or actual != expected['files']:
        raise ValueError('native process or mapped file changed during capture')
    for name in libraries:
        if library_paths(final_rows, name) != {actual[name]['path']}:
            raise ValueError(f'{name} mappings changed during capture')
    for identity in actual.values():
        mapped(final_rows, identity)
    with (run / MAPS).open('xb') as stream:
        stream.write(maps)
    value = dict(schema_version=1, mode=expected['mode'], run_id=run.name, status='observed',
                 expected_sha256=digest(run / EXPECTED), files=actual,
                 process_before=before, process_after=after,
                 captured_host_monotonic_ns=time.monotonic_ns(),
                 maps=dict(path=MAPS, bytes=len(maps), sha256=hashlib.sha256(maps).hexdigest()))
    write_new(run / OBSERVED, value)
    return value


def close(run):
    run = Path(run).resolve()
    expected, observed = read_json(run / EXPECTED), read_json(run / OBSERVED)
    expected_libraries(expected)
    files = {name: file_identity(value['path']) for name, value in expected['files'].items()}
    if files != expected['files'] or observed.get('files') != files:
        raise ValueError('native executable or library changed before closure')
    value = dict(schema_version=1, mode=expected['mode'], run_id=run.name, status='closed', files=files,
                 expected_sha256=digest(run / EXPECTED), observed_sha256=digest(run / OBSERVED),
                 closed_host_monotonic_ns=time.monotonic_ns())
    write_new(run / CLOSED, value)
    return value


def verify(run):
    """Portable closed-artifact verification, including original proc maps bytes."""
    run = Path(run)
    result = dict(status='invalid_native_library_identity', passed=False)
    try:
        environment_path = run / 'environment.txt'
        text = environment_path.read_text(encoding='utf-8') if environment_path.exists() else ''
        declarations = re.findall(r'^native_library_identity_mode=(.*)$', text, re.M)
        evidence = any((run / name).exists() for name in (EXPECTED, OBSERVED, CLOSED, MAPS))
        frozen = any((run / f'configuration/{stage}/inputs/native_library_expected/value.json').exists()
                     for stage in ('initial', 'final'))
        if not declarations and not evidence and not frozen:
            return dict(status='not_requested_legacy', passed=None)
        if len(declarations) != 1 or declarations[0] not in (LEGACY_MODE, MODE):
            raise ValueError('missing, duplicate or unsupported native library mode')
        mode = declarations[0]
        libraries = required_libraries(mode)
        artifact_names = [EXPECTED, OBSERVED, CLOSED, MAPS, 'environment.txt']
        for stage in ('initial', 'final'):
            artifact_names.extend((f'configuration/{stage}/manifest.json',
                                   f'configuration/{stage}/inputs/native_library_expected/value.json'))
        for name in artifact_names:
            path = run / name
            if path.is_symlink() or not path.resolve().is_relative_to(run.resolve()):
                raise ValueError('native library evidence must stay inside its run')
        expected, observed, closed = (read_json(run / name) for name in (EXPECTED, OBSERVED, CLOSED))
        if not all(isinstance(item, dict) for item in (expected, observed, closed)):
            raise ValueError('native library evidence must be objects')
        identities = expected.get('files')
        if not isinstance(identities, dict) or set(identities) != {'executable', *libraries}:
            raise ValueError('expected executable and declared library identities are missing')
        for identity in identities.values():
            if (not isinstance(identity, dict) or not isinstance(identity.get('path'), str)
                    or not PurePosixPath(identity['path']).is_absolute() or '..' in PurePosixPath(identity['path']).parts
                    or not re.fullmatch(r'[0-9a-f]{64}', str(identity.get('sha256')))
                    or any(type(identity.get(key)) is not int or identity[key] <= 0 for key in ('bytes', 'inode', 'mtime_ns', 'ctime_ns'))
                    or any(type(identity.get(key)) is not int or identity[key] < 0 for key in ('device_major', 'device_minor'))):
                raise ValueError('invalid file identity')
        proc = observed.get('process_before')
        if not isinstance(proc, dict):
            raise ValueError('native process identity is missing')
        maps = (run / MAPS).read_bytes()
        rows = parse_maps(maps)
        for identity in identities.values():
            mapped(rows, identity)
        run_dir = expected.get('run_dir')
        if (not isinstance(run_dir, str) or PurePosixPath(run_dir).name != run.name
                or not PurePosixPath(run_dir).is_absolute() or '..' in PurePosixPath(run_dir).parts):
            raise ValueError('expected identity belongs to a different run')
        checks = dict(
            schema=all(type(item.get('schema_version')) is int and item['schema_version'] == 1
                       and item.get('mode') == mode and item.get('run_id') == run.name for item in (expected, observed, closed)),
            files=observed.get('files') == closed.get('files') == identities,
            process=(proc == observed.get('process_after') and all(type(proc.get(key)) is int and proc[key] > 0
                      for key in ('pid', 'start_ticks')) and type(proc.get('uid')) is int and proc['uid'] >= 0
                      and proc.get('executable') == identities['executable']['path']
                      and isinstance(proc.get('argv'), list) and all(isinstance(arg, str) for arg in proc['argv'])
                      and f'--statsFile={run_dir}/metrics/native_radio_stats.json' in proc['argv']),
            mapped_wifi=wifi_paths(rows) == {identities['wifi']['path']},
            maps=observed.get('maps') == dict(path=MAPS, bytes=len(maps), sha256=hashlib.sha256(maps).hexdigest()),
            closed=observed.get('status') == 'observed' and closed.get('status') == 'closed',
            lineage=(observed.get('expected_sha256') == closed.get('expected_sha256') == digest(run / EXPECTED)
                     and closed.get('observed_sha256') == digest(run / OBSERVED)))
        prepared, captured, ended = (expected.get('prepared_host_monotonic_ns'),
                                    observed.get('captured_host_monotonic_ns'), closed.get('closed_host_monotonic_ns'))
        checks['time_order'] = all(type(value) is int and value > 0 for value in (prepared, captured, ended)) and prepared <= captured <= ended
        for summary_path, keys in (('metrics/relay_experiment_summary.json', ('measurement_start_host_monotonic_ns',)),
                                   ('metrics/scenario_summary.json', ('latency_diagnostic', 'bench_window', 'started_host_monotonic_ns'))):
            if (run / summary_path).is_file():
                start = read_json(run / summary_path)
                for key in keys:
                    start = start.get(key) if isinstance(start, dict) else None
                if start is not None:
                    checks['time_order'] &= type(start) is int and checks['time_order'] and captured <= start
        # Frozen copies and their manifest records must bind the actual expected
        # bytes at both boundaries; no fallback to a mutable SDK checkout.
        for stage in ('initial', 'final'):
            directory = run / 'configuration' / stage
            manifest = read_json(directory / 'manifest.json')
            records = [row for row in manifest.get('files', []) if isinstance(row, dict) and row.get('label') == 'native_library_expected']
            relative = 'inputs/native_library_expected/value.json'
            data = (directory / relative).read_bytes()
            checks[f'frozen_{stage}'] = (manifest.get('stage') == stage and len(records) == 1
                and records[0].get('status') == 'observed' and records[0].get('copied_path') == relative
                and records[0].get('kind') == 'configured_input' and records[0].get('bytes') == len(data)
                and data == (run / EXPECTED).read_bytes())
        binary_hashes = re.findall(r'^native_binary_sha256=(.*)$', text, re.M)
        checks['binary_environment'] = binary_hashes == [identities['executable']['sha256']]
        if 'spectrum' in libraries:
            checks['mapped_spectrum'] = library_paths(rows, 'spectrum') == {identities['spectrum']['path']}
        result.update(checks=checks, expected_sha256=digest(run / EXPECTED), observed_sha256=digest(run / OBSERVED),
                      closed_sha256=digest(run / CLOSED), maps_sha256=hashlib.sha256(maps).hexdigest(),
                      executable_sha256=identities['executable']['sha256'], wifi_sha256=identities['wifi']['sha256'],
                      process=proc, paths=dict(expected=EXPECTED, observed=OBSERVED, closed=CLOSED, maps=MAPS))
        if 'spectrum' in libraries:
            result['spectrum_sha256'] = identities['spectrum']['sha256']
        if all(checks.values()):
            result.update(status='verified_native_library_identity', passed=True)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError) as error:
        result['error'] = str(error)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('prepare', 'observe', 'close'))
    parser.add_argument('--run-dir', required=True, type=Path)
    parser.add_argument('--executable', type=Path)
    parser.add_argument('--wifi', type=Path)
    parser.add_argument('--spectrum', type=Path)
    parser.add_argument('--pid', type=int)
    parser.add_argument('--timeout-s', type=float, default=15)
    args = parser.parse_args()
    try:
        if args.operation == 'prepare':
            if args.executable is None or args.wifi is None or args.spectrum is None:
                parser.error('prepare requires --executable, --wifi and --spectrum')
            result = prepare(args.run_dir, args.executable, args.wifi, args.spectrum)
        elif args.operation == 'observe':
            if args.pid is None or args.pid <= 0 or not 0 < args.timeout_s <= 30:
                parser.error('observe requires a positive --pid and timeout at most 30 s')
            result = observe(args.run_dir, args.pid, args.timeout_s)
        else:
            result = close(args.run_dir)
    except (OSError, ValueError, TypeError, KeyError, IndexError) as error:
        parser.exit(1, f'native library identity: {error}\n')
    print(json.dumps(dict(operation=args.operation, run_id=result['run_id'], status=result.get('status', 'prepared'))))


if __name__ == '__main__':
    main()
