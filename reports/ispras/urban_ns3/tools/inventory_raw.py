#!/usr/bin/env python3
"""Inventory frozen run files for transfer; never edit or follow raw symlinks."""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--freeze', type=Path, required=True)
    p.add_argument('--runs-root', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    freeze = json.loads(args.freeze.read_bytes())
    args.output.mkdir(parents=True, exist_ok=True)
    sums, errors = [], []
    with (args.output / 'raw_files.csv').open('w', newline='') as stream:
        fields = ['run_id', 'path_within_run', 'kind', 'bytes', 'sha256', 'symlink_target', 'changed_while_reading']
        w = csv.DictWriter(stream, fields, lineterminator='\n'); w.writeheader()
        for case in freeze['cases']:
            folder = args.runs_root / case['run_id']
            count = size = links = 0
            if not folder.is_dir():
                errors.append(dict(run_id=case['run_id'], error='raw directory absent'))
            for path in sorted(folder.rglob('*')):
                if path.is_symlink():
                    target = os.readlink(path)
                    w.writerow(dict(run_id=case['run_id'], path_within_run=str(path.relative_to(folder)),
                        kind='symlink', symlink_target=target))
                    links += 1
                elif path.is_file():
                    before = path.stat(); digest = hashlib.sha256()
                    with path.open('rb') as raw:
                        for chunk in iter(lambda: raw.read(4 * 1024 * 1024), b''): digest.update(chunk)
                    after = path.stat()
                    changed = (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
                    if changed: errors.append(dict(run_id=case['run_id'], path=str(path), error='changed while hashing'))
                    w.writerow(dict(run_id=case['run_id'], path_within_run=str(path.relative_to(folder)),
                        kind='file', bytes=after.st_size, sha256=digest.hexdigest(), changed_while_reading=changed))
                    count += 1; size += after.st_size
            sums.append(dict(order=case['order'], run_id=case['run_id'], files=count, bytes=size, symlinks=links))
            stream.flush()
            print(json.dumps(dict(order=case['order'], files=count, bytes=size)), flush=True)
    with (args.output / 'raw_runs.csv').open('w', newline='') as stream:
        w = csv.DictWriter(stream, list(sums[0]), lineterminator='\n'); w.writeheader(); w.writerows(sums)
    report = dict(freeze_sha256=hashlib.sha256(args.freeze.read_bytes()).hexdigest(),
        script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        files_manifest_sha256=hashlib.sha256((args.output / 'raw_files.csv').read_bytes()).hexdigest(),
        runs=len(sums), files=sum(r['files'] for r in sums), bytes=sum(r['bytes'] for r in sums),
        symlinks=sum(r['symlinks'] for r in sums), errors=errors,
        scope='Every regular file of the 56 frozen raw directories. Symlink targets are recorded, never followed. No runtime/analysis claims derived from checksums.')
    (args.output / 'raw_inventory.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report), flush=True)


if __name__ == '__main__':
    main()
