"""Source clock evidence/copying; no native runtime or activation is synthesized."""
import copy
import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.product.capture_effective_config import capture_snapshot
from scripts.product.prepare_native_sources import materialize, prepare
from scripts.product.summarize_native_radio_five_uav import (
    _native_source_report_lines, export_live_metrics, summarize_native_sources,
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

    def logging(self, native=None, configured=None):
        if native is not None:
            (self.run / 'metrics/native_radio_stats.json').write_text(json.dumps(
                dict(event_logging=native, phy_rx_ok_total=42, phy_rx_error_total=7)))
        if configured is not None:
            (self.run / 'environment.txt').write_text('event_logging=' + configured + '\n')

    def test_metrics_only_explicitly_disables_phase_decoder_and_foreign_measurements(self):
        self.actual()
        self.logging(native='metrics_only', configured='metrics_only')
        result = summarize_native_sources(self.run, self.switches(110, 120))
        observation = result['trace_observations']
        self.assertEqual(observation['event_logging'], 'metrics_only')
        self.assertEqual(observation['mode_source'], 'metrics/native_radio_stats.json:event_logging')
        self.assertEqual(observation['decoder']['status'], 'unavailable_logging_disabled')
        self.assertEqual(observation['foreign_signal']['status'], 'unavailable_logging_disabled')
        active = result['windows']['active']
        for field in ('decoded_mpdu_samples', 'decoder_success_attempts', 'decoder_failure_attempts',
                      'decoder_attempt_per', 'other_phy_drops'):
            self.assertIsNone(active[field], field)
        self.assertIsNone(active['application_pdr'])
        self.assertIn('unavailable_logging_disabled', '\n'.join(_native_source_report_lines(result)))

    def test_enabled_empty_trace_is_explicitly_unobserved_not_disabled_or_per_zero(self):
        self.actual()
        self.logging(native='batched_trace', configured='batched_trace')
        result = summarize_native_sources(self.run, self.switches(110, 120))
        self.assertEqual(result['trace_observations']['decoder']['status'], 'unavailable_no_observations')
        self.assertEqual(result['trace_observations']['foreign_signal']['status'], 'unavailable_no_observations')
        self.assertEqual(result['windows']['active']['decoder_success_attempts'], 0)
        self.assertIsNone(result['windows']['active']['decoder_attempt_per'])

    def test_actual_logging_conflict_is_visible_and_does_not_produce_phase_measurements(self):
        self.actual()
        self.logging(native='metrics_only', configured='batched_trace')
        event = dict(event='phy_rx_error', time_s=111)
        result = summarize_native_sources(self.run, self.switches(110, 120) + [event])
        self.assertEqual(result['trace_observations']['metadata_status'], 'conflict')
        self.assertEqual(result['trace_observations']['decoder']['observed_event_rows'], 1)
        self.assertEqual(result['trace_observations']['decoder']['status'], 'unavailable_logging_metadata_conflict')
        self.assertIsNone(result['windows']['active']['decoder_failure_attempts'])

    def test_batched_native_events_are_exported_with_live_metrics_into_separate_output(self):
        self.actual()
        self.logging(native='batched_trace', configured='batched_trace')
        fields = 'time_s,wall_monotonic_ns,phase,event,node,peer,bytes,x,y,z,value,details'.split(',')
        path = self.run / 'logs/native_radio_events.csv'
        with path.open('w', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for when, event, value, details in (
                    (110, 'jammer_on', '', ''), (111, 'phy_rx_ok', 2, ''),
                    (112, 'phy_rx_error', 1, ''), (113, 'spectrum_signal_arrival', -75, 'foreign_signal=1'),
                    (114, 'wifi_monitor_rx', 1, 'noise_interference_dbm=-85;sinr_db=10'),
                    (120, 'jammer_off', '', '')):
                writer.writerow(dict(time_s=when, wall_monotonic_ns=int(when * 1e9),
                    phase='experiment_normal', event=event, node='cp', peer='', bytes=64,
                    x=0, y=0, z=1, value=value, details=details))
        before = {str(p.relative_to(self.run)): p.read_bytes() for p in self.run.rglob('*') if p.is_file()}
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            result = export_live_metrics(self.run, output, plots=False)
            summary = json.loads((output / 'metrics/native_source_summary.json').read_text())
            self.assertEqual(result['native_sources'], summary)
            active = summary['windows']['active']
            self.assertEqual((active['decoder_success_attempts'], active['decoder_failure_attempts']), (1, 1))
            self.assertEqual(active['decoder_attempt_per'], .5)
            self.assertEqual(active['decoded_mpdu_samples'], 1)
            self.assertEqual(active['foreign_power_dbm_by_receiver']['cp']['mean'], -75)
            self.assertEqual(summary['trace_observations']['decoder']['status'], 'observed')
            self.assertEqual(summary['trace_observations']['foreign_signal']['status'], 'observed')
            report = (output / 'live_metrics.md').read_text()
            self.assertIn('metrics/native_source_summary.json', report)
            self.assertIn('native_sources_actual.json', report)
        after = {str(p.relative_to(self.run)): p.read_bytes() for p in self.run.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_live_export_in_run_writes_source_summary_with_disabled_reason(self):
        self.actual()
        self.logging(native='metrics_only')
        result = export_live_metrics(self.run, plots=False)
        saved = json.loads((self.run / 'metrics/native_source_summary.json').read_text())
        self.assertEqual(result['native_sources'], saved)
        self.assertEqual(saved['trace_observations']['decoder']['status'], 'unavailable_logging_disabled')


if __name__ == '__main__':
    unittest.main()
