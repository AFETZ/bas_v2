"""Controller-only batch tests; injected runs do not qualify simulations."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import bas
from scripts.product.run_batch import MAX_CASES, execute, numbers, plan_batch


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = json.loads((bas.ROOT / 'runtime.json').read_text())
        source = bas.ROOT / self.config['environment']['scene']
        target = self.root / self.config['environment']['scene']
        target.parent.mkdir(parents=True)
        target.write_bytes(source.read_bytes())
        self.calls = []

    def plan(self, **overrides):
        args = dict(config=self.config, batch_id='batch-test', seeds=[1], rng_runs=[0, 1], repeats=1,
                    root=self.root)
        args.update(overrides)
        return plan_batch(**args)

    def runner(self, outcomes):
        def run(config, run_id):
            self.calls.append((copy.deepcopy(config), run_id))
            code, runtime, mission, validity = outcomes[len(self.calls) - 1]
            path = self.root / 'runs/control' / run_id
            path.mkdir(parents=True)
            (path / 'run.json').write_text(json.dumps(dict(runtime_status=runtime,
                mission_outcome=mission, measurement_validity=validity, returncode=code)))
            return code
        return run

    def test_cartesian_plan_has_unique_ids_and_unchanged_base_config(self):
        original = copy.deepcopy(self.config)
        plan = self.plan(seeds=[1, 2], rng_runs=[0, 1], repeats=2, duration=20)
        self.assertEqual(plan['planned_cases'], 8)
        self.assertEqual(len({row['run_id'] for row in plan['cases']}), 8)
        self.assertTrue(all(row['status'] == 'not_started' for row in plan['cases']))
        self.assertEqual(plan['config']['experiment']['duration_s'], 20)
        self.assertEqual(self.config, original)

    def test_sequential_execution_preserves_seed_run_and_records_all_cases(self):
        run = self.runner([(0, 'completed', 'succeeded', 'valid')] * 2)
        result = execute(self.plan(), root=self.root, run_fn=run)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['returncode'], 0)
        self.assertEqual([c[0]['experiment']['run'] for c in self.calls], [0, 1])
        ledger = json.loads((self.root / 'runs/control/batch-test/batch.json').read_text())
        self.assertEqual(ledger['attempted_cases'], 2)
        self.assertTrue(all(row['status'] == 'attempted' for row in ledger['cases']))

    def test_mission_failure_can_continue_but_fail_fast_stops(self):
        outcomes = [(1, 'completed', 'incomplete', 'valid'), (0, 'completed', 'succeeded', 'valid')]
        result = execute(self.plan(), root=self.root, run_fn=self.runner(outcomes))
        self.assertEqual(result['status'], 'completed_with_failures')
        self.assertEqual(len(self.calls), 2)
        self.calls.clear()
        result = execute(self.plan(batch_id='fast'), root=self.root, run_fn=self.runner(outcomes), fail_fast=True)
        self.assertEqual(result['status'], 'stopped_on_failure')
        self.assertEqual(result['cases'][1]['status'], 'not_started')

    def test_current_case_stop_cancels_batch_without_next_attempt(self):
        result = execute(self.plan(), root=self.root,
                         run_fn=self.runner([(130, 'completed', 'cancelled', 'not_evaluated')]))
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(result['returncode'], 130)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(result['cases'][1]['status'], 'not_started')

    def test_runtime_failure_stops_even_without_fail_fast(self):
        result = execute(self.plan(), root=self.root,
                         run_fn=self.runner([(1, 'failed', 'failed', 'invalid')]))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['returncode'], 2)
        self.assertEqual(len(self.calls), 1)

    def test_setup_exception_and_missing_run_record_cannot_be_success(self):
        def failed(config, run_id):
            raise ValueError('missing runtime image')
        result = execute(self.plan(), root=self.root, run_fn=failed)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('missing runtime image', result['cases'][0]['error'])
        result = execute(self.plan(batch_id='no-record'), root=self.root, run_fn=lambda config, run_id: 0)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['returncode'], 2)

    def test_existing_batch_or_case_is_never_overwritten(self):
        plan = self.plan()
        case = self.root / 'runs/control' / plan['cases'][1]['run_id']
        case.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, 'immutable'):
            execute(plan, root=self.root, run_fn=lambda config, run_id: self.fail('must not launch'))
        self.assertFalse((self.root / 'runs/control/batch-test').exists())
        folder = self.root / 'runs/control/another'
        folder.mkdir()
        with self.assertRaisesRegex(ValueError, 'already exists'):
            execute(self.plan(batch_id='another'), root=self.root)

    def test_changed_scene_stops_before_next_case(self):
        run = self.runner([(0, 'completed', 'succeeded', 'valid')])
        def mutate(config, run_id):
            code = run(config, run_id)
            (self.root / config['environment']['scene']).write_text('{"changed":true}')
            return code
        result = execute(self.plan(), root=self.root, run_fn=mutate)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['stop_reason'], 'scene_changed_after_batch_planning')
        self.assertEqual(len(self.calls), 1)

    def test_invalid_numbers_and_large_batches_fail_before_creation(self):
        for text in ('1,,2', '1,1', '-1', 'nan', 'true'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                numbers(text, minimum=1, maximum=100, label='seed')
        for args in (dict(repeats=MAX_CASES + 1), dict(seeds=[1, 2], repeats=MAX_CASES),
                     dict(duration=float('inf')), dict(seeds=[True]), dict(batch_id='x' * 80)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.plan(**args)
        self.assertFalse((self.root / 'runs').exists())

    def test_real_cli_dry_run_on_windows_or_linux_creates_no_results(self):
        batch_id = 'dry-run-unittest-localhost'
        folder = bas.ROOT / 'runs/control' / batch_id
        self.assertFalse(folder.exists())
        result = subprocess.run([sys.executable, str(bas.ROOT / 'scripts/product/run_batch.py'),
            '--config', str(bas.ROOT / 'runtime.json'), '--batch-id', batch_id,
            '--seeds', '1,2', '--rng-runs', '0,1', '--repeats', '2', '--duration', '20', '--dry-run'],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['planned_cases'], 8)
        self.assertFalse(folder.exists())


if __name__ == '__main__':
    unittest.main()
