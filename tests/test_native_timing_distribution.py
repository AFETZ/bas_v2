"""Compile the dependency-free production histogram and verify analytic clocks."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class NativeTimingDistributionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which('g++') or shutil.which('clang++')
        if compiler is None:
            raise unittest.SkipTest('native C++17 compiler unavailable; histogram not verified')
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        executable = Path(cls.temporary.name) / 'timing-contract'
        subprocess.run([compiler, '-std=c++17', '-O2', '-Wall', '-Wextra', '-Werror', '-pedantic',
                        '-I', str(ROOT / 'network/ns3/scratch'),
                        str(ROOT / 'tests/native_timing_distribution_contract.cc'),
                        '-o', str(executable)], check=True, capture_output=True, text=True)
        result = subprocess.run([str(executable)], check=True, capture_output=True, text=True)
        cls.data = json.loads(result.stdout)

    def test_every_inclusive_edge_and_overflow_has_its_own_bin(self):
        observed = self.data['known_bins']
        self.assertEqual(observed['counts'], [1] * 16)
        self.assertEqual(observed['samples'], 16)
        self.assertEqual(observed['maximum_ns'], 1_000_000_001)
        self.assertEqual(observed['upper_bounds_ns'][-1], None)
        self.assertEqual(observed['over_limit_samples'], 5)

    def test_exact_limit_is_valid_and_terminal_episode_is_censored(self):
        observed = self.data['spans']
        self.assertEqual(observed['over_limit_samples'], 3)
        self.assertEqual(observed['over_limit_episodes'], 2)
        self.assertEqual(observed['consecutive_over_limit_span_ns'], 10_000_000)
        self.assertEqual(observed['maximum_consecutive_over_limit_span_ns'], 10_000_000)
        self.assertTrue(observed['last_observation_over_limit'])
        self.assertIn('right_censored', observed['duration_scope'])
        self.assertEqual(sum(observed['counts']), 6)

    def test_invalid_clock_or_threshold_does_not_become_fresh_zero_age(self):
        observed = self.data['invalid_and_clock_reset']
        self.assertEqual(observed['invalid_samples'], 3)
        self.assertEqual(observed['samples'], 4)
        self.assertEqual(observed['counts'][0], 0)
        self.assertEqual(observed['maximum_ns'], 110_000_000)

    def test_time_reset_invalid_gap_and_changed_limit_do_not_invent_duration(self):
        observed = self.data['invalid_and_clock_reset']
        self.assertEqual(observed['observation_clock_regressions'], 1)
        self.assertEqual(observed['limit_changes'], 1)
        self.assertEqual(observed['consecutive_over_limit_span_ns'], 0)
        self.assertEqual(observed['over_limit_episodes'], 3)

    def test_empty_observation_has_no_invented_maximum_or_limit(self):
        observed = self.data['empty']
        self.assertEqual(observed['samples'], 0)
        self.assertEqual(sum(observed['counts']), 0)
        self.assertIsNone(observed['maximum_ns'])
        self.assertIsNone(observed['limit_ns'])


if __name__ == '__main__':
    unittest.main()
