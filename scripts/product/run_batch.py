#!/usr/bin/env python3
"""Sequential batches using the existing BAS configuration and run controller."""
from __future__ import annotations

import argparse
import copy
import hashlib
import itertools
import json
from pathlib import Path
import signal
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import bas

MAX_CASES = 256
# RngStream requires the common seed below both MRG32k3a component moduli.
MAX_SEED = 4294944442
MAX_RNG_RUN = (1 << 64) - 1


def numbers(text, *, minimum, maximum, label):
    parts = text.split(',') if isinstance(text, str) else text
    if not parts or len(parts) > MAX_CASES:
        raise ValueError(f'{label} requires 1..{MAX_CASES} values')
    values = []
    for part in parts:
        if type(part) is int:
            number = part
        elif isinstance(part, str) and part.strip().isdigit():
            number = int(part.strip())
        else:
            raise ValueError(f'{label} must be comma-separated integers')
        if not minimum <= number <= maximum:
            raise ValueError(f'{label} values must be in {minimum}..{maximum}')
        values.append(number)
    if len(set(values)) != len(values):
        raise ValueError(f'{label} has duplicates; use --repeats for repeated cases')
    return values


def plan_batch(config, batch_id, seeds, rng_runs, repeats=1, duration=None, *, root=None):
    root = bas.ROOT if root is None else Path(root)
    bas.identifier(batch_id)
    seeds = numbers(seeds, minimum=1, maximum=MAX_SEED, label='seeds')
    rng_runs = numbers(rng_runs, minimum=0, maximum=MAX_RNG_RUN, label='rng-runs')
    if type(repeats) is not int or not 1 <= repeats <= MAX_CASES:
        raise ValueError(f'repeats must be an integer in 1..{MAX_CASES}')
    count = len(seeds) * len(rng_runs) * repeats
    if count > MAX_CASES:
        raise ValueError(f'batch has {count} cases; maximum is {MAX_CASES}')
    config = copy.deepcopy(config)
    if duration is not None:
        config['experiment']['duration_s'] = bas.validate_duration(duration)
    bas.validate_capture_storage(config)
    paths = dict(scene=config['environment']['scene'])
    if config['propagation']['model'] == 'sionna':
        paths['radio'] = config['propagation']['radio_config']
    sources = config.get('interference', {}).get('sources')
    if sources is not None:
        paths['sources'] = sources
    if config.get('hitl') is not None:
        paths.update(hitl_request=config['hitl']['request'],
                     hitl_profiles='network/config/hitl_reference_devices.json')
    input_hashes = {label: dict(path=name, sha256=hashlib.sha256((root / name).read_bytes()).hexdigest())
                    for label, name in paths.items()}
    scene_sha = input_hashes['scene']['sha256']
    cases = []
    for order, (seed, rng_run, repeat) in enumerate(itertools.product(seeds, rng_runs, range(1, repeats + 1)), 1):
        run_id = bas.identifier(f'{batch_id}-c{order:04d}')
        cases.append(dict(order=order, run_id=run_id, seed=seed, rng_run=rng_run,
                          repeat=repeat, status='not_started',
                          run_record=f'runs/control/{run_id}/run.json'))
    return dict(schema_version=1, batch_id=batch_id, status='planned', config=config,
                scene_sha256=scene_sha, input_sha256=input_hashes, cases=cases, planned_cases=count,
                repeat_semantics='same declared seed/RngRun; repeats are not new independent PRNG substreams')


def _preflight(plan, root):
    folder = root / 'runs/control' / plan['batch_id']
    if folder.exists():
        raise ValueError('batch already exists; choose a new batch ID')
    for case in plan['cases']:
        if (root / 'runs/control' / case['run_id']).exists() or (root / 'runs/native-radio-realtime' / case['run_id']).exists():
            raise ValueError(f'existing attempt is immutable: {case["run_id"]}')
    return folder


def execute(plan, *, root=None, run_fn=None, fail_fast=False):
    root = bas.ROOT if root is None else Path(root)
    run_fn = bas.run if run_fn is None else run_fn
    folder = _preflight(plan, root)
    folder.mkdir(parents=True, exist_ok=False)
    record = copy.deepcopy(plan)
    record.update(status='running', started_host_monotonic_ns=time.monotonic_ns())
    ledger = folder / 'batch.json'
    bas.write_json(ledger, record)
    cancelled = False
    def cancel(_signum, _frame):
        nonlocal cancelled
        cancelled = True
    previous = {sig: signal.signal(sig, cancel) for sig in (signal.SIGINT, signal.SIGTERM)}
    failed_cases = 0
    exit_code = 0
    try:
        for case in record['cases']:
            if cancelled:
                record.update(status='cancelled', stop_reason='operator_cancelled_between_cases')
                exit_code = 130
                break
            # Preserve the legacy scene guard when executing an older plan.
            required_inputs = record.get('input_sha256', dict(scene=dict(
                path=record['config']['environment']['scene'], sha256=record['scene_sha256'])))
            for label, entry in required_inputs.items():
                if hashlib.sha256((root / entry['path']).read_bytes()).hexdigest() != entry['sha256']:
                    record.update(status='failed', stop_reason=f'{label}_changed_after_batch_planning')
                    break
            if record['status'] == 'failed':
                exit_code = 2
                break
            config = copy.deepcopy(record['config'])
            config['experiment'].update(seed=case['seed'], run=case['rng_run'])
            case.update(status='attempted', started_host_monotonic_ns=time.monotonic_ns())
            bas.write_json(ledger, record)
            try:
                code = run_fn(config, case['run_id'])
            except KeyboardInterrupt:
                code = 130
            except Exception as error:
                case.update(error=f'{type(error).__name__}: {error}', outcome='setup_or_runtime_failure')
                record.update(status='failed', stop_reason='case_raised_exception')
                exit_code = 2
                break
            finally:
                case['ended_host_monotonic_ns'] = time.monotonic_ns()
            case['returncode'] = code
            path = root / case['run_record']
            outcome = json.loads(path.read_text()) if path.exists() else {}
            case.update({key: outcome.get(key, 'not_evaluated') for key in
                         ('runtime_status', 'mission_outcome', 'measurement_validity')})
            for key in ('scene_sha256', 'radio_sha256', 'sources_sha256',
                        'hitl_request_sha256', 'hitl_profiles_sha256', 'hitl_bundle_sha256'):
                if key in outcome:
                    case[key] = outcome[key]
            # Close the check→snapshot race using actual hashes from bas.run.
            # Historical records without these fields remain explicitly unknown.
            comparisons = [outcome[label + '_sha256'] == entry['sha256']
                           for label, entry in required_inputs.items() if label + '_sha256' in outcome]
            case['inputs_match_batch_plan'] = (all(comparisons)
                if len(comparisons) == len(required_inputs) else None)
            if code == 130 or cancelled:
                case['outcome'] = 'cancelled'
                record.update(status='cancelled', stop_reason='current_case_stopped')
                exit_code = 130
                break
            if False in comparisons:
                case['outcome'] = 'input_snapshot_differs_from_plan'
                record.update(status='failed', stop_reason='case_input_snapshot_differs_from_batch_planning')
                exit_code = 2
                break
            if case['runtime_status'] != 'completed' or case['measurement_validity'] != 'valid':
                case['outcome'] = 'setup_or_runtime_failure'
                record.update(status='failed', stop_reason='case_runtime_or_measurements_not_valid')
                exit_code = 2
                break
            bench_passed = (config.get('hitl') is not None and case['mission_outcome'] == 'not_applicable'
                            and outcome.get('radio_hitl_verification', {}).get('passed') is True)
            if code == 0 and (case['mission_outcome'] == 'succeeded' or bench_passed):
                case['outcome'] = 'succeeded'
            elif case['mission_outcome'] in ('failed', 'incomplete'):
                failed_cases += 1
                case['outcome'] = 'mission_failed_or_incomplete'
                if fail_fast:
                    record.update(status='stopped_on_failure', stop_reason='fail_fast')
                    exit_code = 1
                    break
            else:
                case['outcome'] = 'setup_or_runtime_failure'
                record.update(status='failed', stop_reason='inconsistent_or_missing_case_outcome')
                exit_code = 2
                break
            bas.write_json(ledger, record)
        else:
            record['status'] = 'completed_with_failures' if failed_cases else 'completed'
            exit_code = 1 if failed_cases else 0
    except Exception as error:
        record.update(status='failed', stop_reason='batch_controller_error', error=f'{type(error).__name__}: {error}')
        exit_code = 2
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        record.update(returncode=exit_code, ended_host_monotonic_ns=time.monotonic_ns(),
                      attempted_cases=sum(case['status'] == 'attempted' for case in record['cases']))
        bas.write_json(ledger, record)
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=bas.ROOT / 'runtime.json')
    parser.add_argument('--batch-id', required=True)
    parser.add_argument('--seeds')
    parser.add_argument('--rng-runs')
    parser.add_argument('--repeats', type=int, default=1)
    parser.add_argument('--duration', type=float)
    parser.add_argument('--fail-fast', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    config = bas.load_config(args.config, root=bas.ROOT)
    plan = plan_batch(config, args.batch_id,
                      args.seeds if args.seeds is not None else [config['experiment']['seed']],
                      args.rng_runs if args.rng_runs is not None else [config['experiment']['run']],
                      args.repeats, args.duration)
    plan['config_path'] = str(args.config.resolve())
    _preflight(plan, bas.ROOT)
    if args.dry_run:
        print(json.dumps(plan, indent=2, allow_nan=False))
        return 0
    result = execute(plan, fail_fast=args.fail_fast)
    print(json.dumps({key: result[key] for key in ('batch_id', 'status', 'attempted_cases', 'planned_cases', 'returncode')}))
    return result['returncode']


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as error:
        print(f'BAS batch: {error}', file=sys.stderr)
        raise SystemExit(2)
