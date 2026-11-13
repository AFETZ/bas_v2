"""Source clock evidence/copying; no native runtime or activation is synthesized."""
import copy
import csv
import hashlib
import json
import sqlite3
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.product.capture_effective_config import capture_snapshot
from scripts.product.prepare_native_sources import materialize, prepare
from scripts.product.summarize_native_radio_five_uav import (
    _native_source_report_lines, _SourceDistributions, distribution, export_live_metrics,
    iter_native_events, native_events, summarize_native_sources,
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

    def test_source_cli_exports_native_observations_without_five_uav_mission_report(self):
        self.actual()
        (self.run / 'logs/native_radio_events.csv').write_text(
            'time_s,wall_monotonic_ns,phase,event,node,peer,bytes,x,y,z,value,details\n'
            '110.000000,997654321,experiment_normal,jammer_on,clock_fixture,,0,0,0,0,1,\n'
            '120.000000,998654321,experiment_normal,jammer_off,clock_fixture,,0,0,0,0,0,\n')
        before = {p.relative_to(self.run): p.read_bytes() for p in self.run.rglob('*') if p.is_file()}
        script = Path(__file__).resolve().parents[1] / 'scripts/product/summarize_native_radio_five_uav.py'
        output = self.run / 'derived'
        result = subprocess.run([sys.executable, str(script), '--run-dir', str(self.run),
                                 '--native-sources-only', '--output-dir', str(output)],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['actual_schedule_status'], 'observed')
        report = json.loads((output / 'metrics/native_source_summary.json').read_text())
        self.assertEqual(report['source_activity_status'], 'switch_on_observed')
        self.assertEqual(report['windows']['active']['start_sim_s'], 110)
        self.assertFalse((self.run / 'metrics/native_source_summary.json').exists())
        self.assertTrue(all((self.run / p).read_bytes() == contents for p, contents in before.items()))

    def test_source_only_filter_preserves_full_summary_and_capture_hash(self):
        self.actual()
        self.logging(native='batched_trace', configured='batched_trace')
        (self.run / 'metrics/native_radio_stats.json').write_text(
            json.dumps(dict(uav_count=1, event_logging='batched_trace')))
        path = self.run / 'logs/native_radio_events.csv'
        fields = 'time_s,wall_monotonic_ns,phase,event,node,peer,bytes,x,y,z,value,details'.split(',')
        # Quoted separators/newlines, malformed numbers, aliases, and unrelated
        # nodes exercise the CSV/parser boundaries rather than synthetic metrics.
        with path.open('w', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(reversed(fields)))
            writer.writeheader()
            for when, event, node, value, detail in (
                    (110, 'jammer_on', 'source', 1, ''),
                    (111, 'live_pose', 'unrelated', 1, 'ignored, bookkeeping\nline'),
                    (111, 'phy_rx_ok', 'uav1', 2, ''),
                    (112, 'phy_rx_error', 'cp', 1, ''),
                    (113, 'spectrum_signal_arrival', 'cp', -75, 'foreign_signal=1'),
                    (114, 'wifi_monitor_rx', 'cp', 1, 'noise_interference_dbm=-85;sinr_db=10'),
                    (115, 'phy_rx_drop', 'uav1', 1, ''),
                    (116, 'phy_rx_ok', 'cp', 'not_a_number', ''),
                    (120, 'jammer_off', 'source', 0, '')):
                writer.writerow(dict(time_s=when, wall_monotonic_ns=int(when * 1e9),
                    phase='experiment_normal', event=event, node=node, peer='',
                    bytes=64, x=0, y=0, z=1, value=value, details=detail))
        baseline = summarize_native_sources(self.run, native_events(path),
                                             output_dir=self.run / 'baseline')
        script = Path(__file__).resolve().parents[1] / 'scripts/product/summarize_native_radio_five_uav.py'
        output = self.run / 'filtered'
        result = subprocess.run([sys.executable, str(script), '--run-dir', str(self.run),
            '--native-sources-only', '--output-dir', str(output)],
            capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((output / 'metrics/native_source_summary.json').read_text()), baseline)
        self.assertEqual(baseline['windows']['active']['decoder_success_attempts'], 1)
        self.assertEqual(baseline['windows']['active']['decoder_failure_attempts'], 1)
        self.assertEqual(baseline['receiver_roster'], ['cp', 'uav1'])

    def test_disk_distributions_preserve_exact_float_definitions_and_clean_tempfiles(self):
        for values in ([-0., 0., -0.], [0., -0.], [5., 5., -7., 1e-200, -1e200, 1e200, 9., 11.],
                       [float('nan'), float('inf'), -float('inf')], []):
            with self.subTest(values=values):
                with _SourceDistributions(self.run) as samples:
                    for value in values:
                        samples.add('same', value)
                    self.assertEqual(json.dumps(samples.distribution('same')),
                                     json.dumps(distribution(values)))
                self.assertEqual(list(self.run.glob('.native-source-*')), [])
        with self.assertRaisesRegex(RuntimeError, 'fixture failure'):
            with _SourceDistributions(self.run) as samples:
                samples.add('same', 1.)
                raise RuntimeError('fixture failure')
        self.assertEqual(list(self.run.glob('.native-source-*')), [])

    def test_explicit_spill_budget_is_applied_and_exhaustion_fails_without_leaks(self):
        with self.assertRaisesRegex(sqlite3.DatabaseError, 'full'):
            with _SourceDistributions(self.run, max_bytes=1024**2) as samples:
                self.assertEqual(samples.connection.execute(
                    'PRAGMA max_page_count').fetchone()[0], 256)
                for index in range(100000):
                    samples.add('bounded', float(index))
        self.assertEqual(list(self.run.glob('.native-source-*')), [])

    def test_source_cli_disk_exhaustion_preserves_raw_capture_and_reports_failure(self):
        self.actual()
        self.logging(native='batched_trace')
        path = self.run / 'logs/native_radio_events.csv'
        with path.open('w') as stream:
            stream.write('time_s,wall_monotonic_ns,phase,event,node,peer,bytes,x,y,z,value,details\n')
            stream.write('110,100,experiment_normal,jammer_on,source,,0,0,0,0,1,\n')
            for index in range(12000):
                stream.write(f'111,{101+index},experiment_normal,wifi_monitor_rx,cp,,20,0,0,0,1,'
                             f'noise_interference_dbm=-85;sinr_db={index/1000}\n')
            stream.write('120,20000,experiment_normal,jammer_off,source,,0,0,0,0,0,\n')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        output = self.run / 'bounded'
        script = Path(__file__).resolve().parents[1] / 'scripts/product/summarize_native_radio_five_uav.py'
        result = subprocess.run([sys.executable, str(script), '--run-dir', str(self.run),
            '--native-sources-only', '--source-spill-max-bytes', '1048576',
            '--output-dir', str(output)], capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('database or disk is full', result.stderr)
        self.assertFalse((output / 'metrics/native_source_summary.json').exists())
        self.assertEqual(list(output.glob('.native-source-*')), [])
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)

    def test_source_spill_cli_rejects_invalid_budgets_and_incompatible_modes(self):
        script = Path(__file__).resolve().parents[1] / 'scripts/product/summarize_native_radio_five_uav.py'
        for options in (['--native-sources-only', '--source-spill-max-bytes', '0'],
                        ['--native-sources-only', '--source-spill-max-bytes', '1048577'],
                        ['--live-metrics-only', '--source-spill-max-bytes', '1048576']):
            result = subprocess.run([sys.executable, str(script), '--run-dir', str(self.run), *options],
                capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn('--source-spill-max-bytes', result.stderr)

    def test_streaming_reducer_keeps_duplicate_switches_and_half_open_time_windows(self):
        self.actual()
        self.logging(native='batched_trace')
        switches = self.switches(110., 120.) + self.switches(110., 120.)
        event = lambda when, kind, **kw: dict(time_s=when, event=kind, node='cp', **kw)
        events = switches + [event(100.1, 'phy_rx_ok'), event(110., 'phy_rx_error'),
            event(119.999, 'wifi_monitor_rx', noise_interference_dbm=-80., sinr_db=10.),
            event(120., 'phy_rx_ok'), event(130., 'phy_rx_error'),
            event(120., 'live_pose', x=1, y=2, z=3)]
        result = summarize_native_sources(self.run, iter(events), source_switches=switches,
                                           spill_samples=True)
        self.assertEqual(len(result['sources']), 4)
        baseline, active, recovery = (result['windows'][name] for name in ('baseline', 'active', 'recovery'))
        self.assertEqual((baseline['start_sim_s'], baseline['stop_sim_s']), (100.000000007, 110.))
        self.assertEqual(baseline['decoder_success_attempts'], 1)
        self.assertEqual(active['decoder_failure_attempts'], 1)
        self.assertEqual(active['decoded_mpdu_samples'], 1)
        self.assertEqual(recovery['decoder_success_attempts'], 1)
        self.assertEqual(recovery['decoder_failure_attempts'], 0)  # t=130 is excluded
        self.assertEqual(result['trace_observations']['decoder']['observed_event_rows'], 4)

    def test_source_only_without_roster_keeps_nodes_from_unrelated_events(self):
        self.actual()
        path = self.run / 'logs/native_radio_events.csv'
        path.write_text('time_s,wall_monotonic_ns,phase,event,node,peer,bytes,x,y,z,value,details\n'
            '110,100,experiment_normal,jammer_on,source,,0,0,0,0,1,\n'
            '111,101,experiment_normal,live_pose,uav3,,0,0,0,0,1,\n'
            '120,102,experiment_normal,jammer_off,source,,0,0,0,0,0,\n')
        script = Path(__file__).resolve().parents[1] / 'scripts/product/summarize_native_radio_five_uav.py'
        output = self.run / 'filtered'
        result = subprocess.run([sys.executable, str(script), '--run-dir', str(self.run),
            '--native-sources-only', '--output-dir', str(output)],
            capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads((output / 'metrics/native_source_summary.json').read_text())
        self.assertEqual(summary['receiver_roster'], ['uav3'])
        self.assertIn('uav3', summary['windows']['active']['foreign_power_dbm_by_receiver'])

    def test_source_cli_cannot_publish_an_empty_report_before_tmpfs_capture_is_copied(self):
        self.actual()
        script = Path(__file__).resolve().parents[1] / 'scripts/product/summarize_native_radio_five_uav.py'
        result = subprocess.run([sys.executable, str(script), '--run-dir', str(self.run),
                                 '--native-sources-only'], capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('publish the closed native event capture', result.stderr)
        self.assertFalse((self.run / 'metrics/native_source_summary.json').exists())

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

    def test_single_uav_summary_does_not_invent_four_additional_receivers(self):
        actual = self.actual()
        (self.run / 'metrics/native_radio_stats.json').write_text(
            json.dumps(dict(uav_count=1, event_logging='batched_trace')))
        interval = actual['segments'][0]
        result = summarize_native_sources(self.run, self.switches(interval['start_s'], interval['stop_s']))
        self.assertEqual(result['receiver_roster'], ['cp', 'uav1'])
        self.assertEqual(set(result['windows']['active']['foreign_power_dbm_by_receiver']), {'cp', 'uav1'})

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
