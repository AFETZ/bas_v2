"""Emitter preparation contracts; actual SpectrumWifi reception runs separately."""
import csv
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

from scripts.product.prepare_native_sources import prepare
from scripts.product.summarize_native_radio_five_uav import native_events, summarize_native_sources


ROOT = Path(__file__).resolve().parents[1]


def source(**overrides):
    item = dict(id='smoke_iso', position_m=[0, -40, 10], orientation_rad=[0, 0],
                pattern='iso', gain_dbi=0, center_hz=2412000000, bandwidth_hz=20000000,
                power_w=.01, start_s=10, stop_s=20, period_s=.01, duty_cycle=1,
                mode='continuous', spectral_shape='rectangular')
    item.update(overrides)
    if item['mode'] == 'sweep' and 'center_hz' not in overrides:
        del item['center_hz']
    return item


class NativeSourceTests(unittest.TestCase):
    def test_unknown_missing_root_and_source_fields_are_rejected(self):
        for value in (None, [], {}, {'sources': []}, {'sources': {}},
                      {'sources': [source()], 'unknown': 1},
                      {'sources': [source()], 'schema_version': True},
                      {'sources': [source()], 'schema_version': 2},
                      {'sources': [None]}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                prepare(value)
        for key in source():
            item = source()
            del item[key]
            with self.subTest(missing=key), self.assertRaises(ValueError):
                prepare({'sources': [item]})
        with self.assertRaises(ValueError):
            prepare({'sources': [source(invented_threshold=10)]})
        self.assertEqual(prepare({'sources': [source()], 'schema_version': 1})['schema_version'], 1)

    def test_numeric_fields_reject_bool_strings_none_and_nonfinite(self):
        for key in ('gain_dbi', 'power_w', 'center_hz', 'bandwidth_hz', 'start_s',
                    'stop_s', 'period_s', 'duty_cycle'):
            for value in (True, False, '1', None, float('nan'), float('inf'), 10**400):
                with self.subTest(key=key, value=repr(value)), self.assertRaises(ValueError):
                    prepare({'sources': [source(**{key: value})]})

    def test_vectors_ids_and_native_upa_orientation_bounds_are_checked(self):
        for key, value in [('position_m', None), ('position_m', '123'), ('position_m', [0, 1]),
                           ('position_m', [0, True, 2]), ('orientation_rad', [0, '1']),
                           ('orientation_rad', [0, math.pi+.01]), ('orientation_rad', [-math.pi-.01, 0]),
                           ('id', 1), ('id', True), ('id', ''), ('id', 'bad,id')]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                prepare({'sources': [source(**{key: value})]})
        self.assertEqual(prepare({'sources': [source(orientation_rad=[-math.pi, math.pi])]} )['segments'][0]['orientation_rad'], [-math.pi, math.pi])
        with self.assertRaisesRegex(ValueError, 'unique'):
            prepare({'sources': [source(), source()]})

    def test_mode_specific_fields_cannot_be_silently_ignored(self):
        for mode in ('continuous', 'pulsed'):
            for extra in ({'sweep_hz': [2412000000]}, {'dwell_s': 1}):
                with self.subTest(mode=mode, extra=extra), self.assertRaises(ValueError):
                    prepare({'sources': [source(mode=mode, **extra)]})
        for value in ([], '2412000000', [2412000000, '2437000000'], [2412000000, True],
                      [2412000000, float('nan')], [2412000000, 1]):
            with self.subTest(sweep=value), self.assertRaises(ValueError):
                prepare({'sources': [source(mode='sweep', sweep_hz=value, dwell_s=20)]})
        with self.assertRaises(ValueError):
            prepare({'sources': [source(mode='sweep', sweep_hz=[2412000000], dwell_s=1, center_hz=2412000000)]})

    def test_empty_or_underflowing_schedule_and_derived_power_fail(self):
        for overrides in ({'start_s': 0, 'stop_s': 1e-10}, {'power_w': 0},
                          {'gain_dbi': 1e300}, {'gain_dbi': -1e300},
                          {'stop_s': 1e308}, {'bandwidth_hz': 1.7e308, 'center_hz': 1.7e308},
                          {'start_s': 0, 'stop_s': 1.000000001, 'period_s': .0001000000001},
                          {'mode': 'pulsed', 'period_s': .0001, 'duty_cycle': .000001}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                prepare({'sources': [source(**overrides)]})

    def test_continuous_segment_preserves_physical_inputs_and_absolute_schedule(self):
        configured = source()
        value = prepare({'sources': [configured]})
        self.assertEqual(value['clock'], 'ns3_simulation_seconds')
        self.assertEqual(len(value['segments']), 1)
        segment = value['segments'][0]
        for field in ('id', 'position_m', 'orientation_rad', 'pattern', 'gain_dbi', 'center_hz',
                      'bandwidth_hz', 'power_w', 'start_s', 'stop_s', 'period_s', 'duty_cycle', 'mode'):
            self.assertEqual(segment[field], configured[field])

    def test_fractional_period_is_rejected_even_when_relative_error_is_small(self):
        for period, stop in ((.0001, 2.000000001), (.1, 2.000000001), (.0001, 28800.00000001)):
            with self.subTest(period=period, stop=stop), self.assertRaisesRegex(ValueError, 'whole number'):
                prepare({'sources': [source(start_s=0, stop_s=stop, period_s=period)]})

    def test_long_whole_period_schedules_keep_valid_floating_arithmetic(self):
        for start, duration, period in ((0, 28800, .0001), (12345.678, 28800, .1),
                                         (.1, .3, .1), (100000, 28800, .01)):
            stop = start + duration
            with self.subTest(start=start, duration=duration, period=period):
                segment = prepare({'sources': [source(start_s=start, stop_s=stop, period_s=period)]})['segments'][0]
                self.assertEqual((segment['start_s'], segment['stop_s']), (start, stop))

    def test_sweep_rejects_fractional_dwell_before_generator_installation(self):
        for period in (.0001, .1):
            item = source(start_s=0, stop_s=4, period_s=period, mode='sweep',
                          sweep_hz=[2412000000, 2437000000], dwell_s=1.000000001)
            with self.subTest(period=period), self.assertRaisesRegex(ValueError, 'whole number'):
                prepare({'sources': [item]})

    def test_valid_sweep_uses_ordered_explicit_center_frequencies(self):
        item = source(start_s=10, stop_s=30, mode='sweep', dwell_s=5,
                      sweep_hz=[2412000000, 2437000000], duty_cycle=.25)
        rows = prepare({'sources': [item]})['segments']
        self.assertEqual([(r['start_s'], r['stop_s'], r['center_hz']) for r in rows],
                         [(10, 15, 2412000000), (15, 20, 2437000000),
                          (20, 25, 2412000000), (25, 30, 2437000000)])
        self.assertTrue(all(row['duty_cycle'] == .25 and row['power_w'] == .01 for row in rows))

    def test_pulsed_schedule_does_not_replace_instantaneous_power_with_average(self):
        row = prepare({'sources': [source(mode='pulsed', duty_cycle=.2, power_w=2)]})['segments'][0]
        self.assertEqual(row['power_w'], 2)
        self.assertEqual(row['duty_cycle'], .2)

    def test_cli_resolves_yaml_to_native_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            config = folder / 'source.yaml'
            output = folder / 'resolved.json'
            config.write_text(yaml.safe_dump({'sources': [source()]}))
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/product/prepare_native_sources.py'),
                                     '--config', str(config), '--output', str(output)],
                                    cwd=ROOT, env=os.environ.copy(), capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(output.read_text()), prepare({'sources': [source()]}))

    def test_cli_rejects_duplicate_unknown_malformed_and_wrong_type_yaml(self):
        wrong = yaml.safe_dump({'sources': [source(power_w=True)]})
        unknown = yaml.safe_dump({'sources': [source()], 'ignored': 1})
        duplicate = yaml.safe_dump({'sources': [source()]}) + '\nsources: []\n'
        for text in (wrong, unknown, duplicate, 'sources: [invalid'):
            with self.subTest(text=text), tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary)
                config, output = folder / 'input.yaml', folder / 'output.json'
                config.write_text(text)
                result = subprocess.run([sys.executable, str(ROOT / 'scripts/product/prepare_native_sources.py'),
                                         '--config', str(config), '--output', str(output)],
                                        cwd=ROOT, env=os.environ.copy(), capture_output=True, text=True)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertNotIn('Traceback', result.stderr)
                self.assertIn('Native sources:', result.stderr)
                self.assertFalse(output.exists())

    def test_actual_native_drop_csv_is_aliased_and_counted_in_source_windows(self):
        # Exact header and event spelling emitted by LogEvent/WifiPhyRxDrop;
        # production loads these through native_events before source summary.
        fields = 'time_s,wall_monotonic_ns,phase,event,node,peer,bytes,x,y,z,value,details'.split(',')
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            (run / 'logs').mkdir()
            (run / 'metrics').mkdir()
            path = run / 'logs/native_radio_events.csv'
            with path.open('w', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                for when, event in ((8, 'phy_rx_drop'), (10, 'jammer_on'),
                                    (11, 'phy_rx_drop'), (12, 'phy_rx_ok'),
                                    (13, 'phy_rx_error'), (19.9, 'phy_rx_drop'),
                                    (20, 'jammer_off'), (20, 'phy_rx_drop')):
                    writer.writerow(dict(time_s=when, wall_monotonic_ns=int(when * 1e9),
                        phase='stationary_communication_smoke', event=event, node='cp', peer='',
                        bytes=64, x=5, y=0, z=2, value=1,
                        details='packet_uid=42;reason_code=1;source=WifiPhy.PhyRxDrop'
                                if event == 'phy_rx_drop' else 'source=native'))
            events = native_events(path)
            drops = [row for row in events if row['raw_event'] == 'phy_rx_drop']
            self.assertEqual(len(drops), 4)
            self.assertTrue(all(row['event'] == 'wifi_phy_rx_drop' and row['reason_code'] == 1 for row in drops))
            result = summarize_native_sources(run, events)
            self.assertEqual([result['windows'][name]['other_phy_drops']
                              for name in ('baseline', 'active', 'recovery')], [1, 2, 1])
            active = result['windows']['active']
            self.assertEqual((active['decoder_success_attempts'], active['decoder_failure_attempts']), (1, 1))
            self.assertEqual(active['decoder_attempt_per'], .5)
            self.assertEqual(json.loads((run / 'metrics/native_source_summary.json').read_text()), result)


if __name__ == '__main__':
    unittest.main()
