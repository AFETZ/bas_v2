"""Source clock evidence/copying; no native runtime or activation is synthesized."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.product.capture_effective_config import capture_snapshot
from scripts.product.prepare_native_sources import materialize, prepare
from scripts.product.summarize_native_radio_five_uav import (
    _native_source_report_lines, summarize_native_sources,
)


class SourceArtifactTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.run = Path(temporary.name)
        (self.run / 'logs').mkdir()
        (self.run / 'metrics').mkdir()
        source = dict(id='clock_fixture', position_m=[0, -40, 10], orientation_rad=[0, 0],
            pattern='iso', gain_dbi=0, power_w=.01, center_hz=2412000000, bandwidth_hz=20000000,
            start_s=10, stop_s=20, period_s=.01, duty_cycle=1, mode='continuous', spectral_shape='rectangular')
        self.configured = prepare(dict(time_reference='measurement_start', sources=[source]))
        self.input_path = self.run / 'logs/native_sources.json'
        self.actual_path = self.run / 'logs/native_sources_actual.json'
        self.input_path.write_text(json.dumps(self.configured))

    def actual(self):
        actual = materialize(self.configured, 100_000_000_007, 987654321, 'experiment_normal')
        self.actual_path.write_text(json.dumps(actual, indent=2))
        return actual

    @staticmethod
    def switches(start, stop):
        return [dict(event='jammer_on', time_s=start), dict(event='jammer_off', time_s=stop)]

    def test_snapshot_preserves_relative_input_and_absent_actual_before_anchor(self):
        raw = self.input_path.read_bytes()
        snapshot = capture_snapshot(self.run, stage='initial', repo_root=Path(__file__).resolve().parents[1],
                                    configs=[], observed=[])
        files = {row['label']: row for row in snapshot['files']}
        self.assertEqual(files['native_sources_actual']['status'], 'not_recorded')
        self.assertIsNone(files['native_sources_actual']['copied_path'])
        copied = self.run / 'configuration/initial' / files['native_sources']['copied_path']
        self.assertEqual(copied.read_bytes(), raw)
        self.assertEqual(json.loads(copied.read_text())['clock'], 'measurement_elapsed_seconds')

    def test_snapshot_copies_native_actual_as_distinct_runtime_evidence(self):
        actual = self.actual()
        raw = self.actual_path.read_bytes()
        snapshot = capture_snapshot(self.run, stage='final', repo_root=Path(__file__).resolve().parents[1],
                                    configs=[], observed=[])
        files = {row['label']: row for row in snapshot['files']}
        self.assertEqual(files['native_sources']['kind'], 'configured_input')
        self.assertEqual(files['native_sources_actual']['kind'], 'observed_runtime_schedule')
        copied = self.run / 'configuration/final' / files['native_sources_actual']['copied_path']
        self.assertEqual(copied.read_bytes(), raw)
        self.assertEqual(json.loads(copied.read_text()), actual)
        self.assertEqual(json.loads(self.input_path.read_text()), self.configured)

    def test_measurement_input_without_anchor_cannot_label_switches_active(self):
        result = summarize_native_sources(self.run, self.switches(10, 20))
        self.assertEqual(result['configured_sources'], self.configured)
        self.assertIsNone(result['actual_sources'])
        self.assertIsNone(result['time_anchor'])
        self.assertEqual(result['source_activity_status'], 'unavailable_measurement_anchor')
        self.assertEqual(result['windows'], {})
        self.assertEqual(len(result['sources']), 2)  # Preserve observed rows, including inconsistencies.
        self.assertIn('unavailable_measurement_anchor', '\n'.join(_native_source_report_lines(result)))

    def test_observed_anchor_is_not_source_activation_without_switch_events(self):
        actual = self.actual()
        result = summarize_native_sources(self.run, [])
        self.assertEqual(result['actual_schedule_status'], 'observed')
        self.assertEqual(result['source_activity_status'], 'not_observed')
        self.assertEqual(result['windows'], {})
        self.assertEqual(result['time_anchor'], actual['anchor'])
        self.assertEqual(result['configured_sources']['segments'][0]['start_s'], 10)
        self.assertEqual(result['actual_sources']['segments'][0]['start_ns'], 110_000_000_007)
        report = '\n'.join(_native_source_report_lines(result))
        self.assertIn('native_sources_actual.json', report)
        self.assertIn('committed intervals alone do not prove source activation', report)

    def test_standalone_script_can_reuse_native_producer_from_another_working_directory(self):
        self.actual()
        script = Path(__file__).resolve().parents[1] / 'scripts/product/summarize_native_radio_five_uav.py'
        command = ('import json, runpy, sys; from pathlib import Path; '
                   'module=runpy.run_path(sys.argv[1]); '
                   'result=module["summarize_native_sources"](Path(sys.argv[2]), []); '
                   'print(json.dumps(result["actual_schedule_status"]))')
        result = subprocess.run([sys.executable, '-c', command, str(script), str(self.run)], cwd=self.run,
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), 'observed')

    def test_anchored_native_switches_use_absolute_time_and_keep_relative_input(self):
        actual = self.actual()
        start, stop = (actual['segments'][0][key] for key in ('start_s', 'stop_s'))
        result = summarize_native_sources(self.run, self.switches(start, stop))
        self.assertEqual(result['windows']['active']['start_sim_s'], start)
        self.assertEqual(result['windows']['active']['stop_sim_s'], stop)
        self.assertGreaterEqual(result['windows']['baseline']['start_sim_s'], actual['anchor']['simulation_s'])
        self.assertEqual(result['configured_sources'], self.configured)
        self.assertEqual(result['actual_sources'], actual)
        self.assertEqual(json.loads((self.run / 'metrics/native_source_summary.json').read_text()), result)

    def test_pre_anchor_switches_remain_visible_but_cannot_create_measurement_windows(self):
        self.actual()
        result = summarize_native_sources(self.run, self.switches(10, 20))
        self.assertEqual(result['switch_events_before_anchor'], 2)
        self.assertEqual(len(result['sources']), 2)
        self.assertEqual(result['windows'], {})
        self.assertEqual(result['source_activity_status'], 'not_observed')

    def test_wrong_anchor_or_native_ticks_cannot_replace_verified_actual_schedule(self):
        original = self.actual()
        for mutate in (lambda a: a['anchor'].update(phase='experiment_prepare'),
                       lambda a: a['anchor'].update(simulation_s=1),
                       lambda a: a['segments'][0].update(start_ns=True),
                       lambda a: a['segments'][0].update(start_ns=1)):
            value = copy.deepcopy(original)
            mutate(value)
            self.actual_path.write_text(json.dumps(value))
            with self.subTest(actual=value):
                result = summarize_native_sources(self.run, self.switches(110, 120))
                self.assertEqual(result['actual_schedule_status'], 'invalid')
                self.assertTrue(result['actual_schedule_error'])
                self.assertEqual(result['windows'], {})


if __name__ == '__main__':
    unittest.main()
