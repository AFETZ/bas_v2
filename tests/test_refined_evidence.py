"""Acceptance parser fixtures; simulator and hardware observations are not fabricated as results."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from scripts.product.analyze_relay_experiment import native_analysis_event, read_csv, refined_case_evidence


class RefinedEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.raw = self.root / 'runs/native-radio-realtime/refined-fixture'
        self.control = self.root / 'runs/control/refined-fixture'
        self.bundle = self.control / 'generated/terrain'
        self.bundle.mkdir(parents=True)
        self.grid = dict(x_m=np.array([-5000., 5000.]), y_m=np.array([-5000., 5000.]), z_m=np.zeros((2, 2)))
        self.scenario = SimpleNamespace(uav_ids=(1, 2, 3, 4, 5),
            routes={i: [(i * 10., 0., 30.)] for i in range(1, 6)}, terrain_grid=self.grid,
            terrain_manifest=dict(extent_m=[10000, 10000], relief_m=200,
                building_bounds={'b': dict(storeys=15, bounds_m=[[100, 100, 0], [110, 110, 45]])}))
        scene = dict(scenario='terrain_10km_five_uav_refined_case_v1',
            terrain_bundle=self.bundle.relative_to(self.root).as_posix(), gcs=[0., 0., 2.])
        self.write(self.raw / 'logs/urban_scenario.json', scene)
        self.write(self.bundle / 'scene-manifest.json', self.scenario.terrain_manifest)
        for stage in ('initial', 'final'):
            self.write(self.raw / f'configuration/{stage}/inputs/geometry_source/value.json', scene)
            self.write(self.raw / f'configuration/{stage}/inputs/sionna_geometry_manifest/value.json', self.scenario.terrain_manifest)
        record = dict(run_id=self.raw.name, runtime_status='completed', mission_outcome='succeeded', measurement_validity='valid',
                      runtime_verification_errors=[])
        self.write(self.control / 'run.json', record)
        self.meta = dict(record, measurement_window_completed=True, measurement_start_host_monotonic_ns=100,
            measurement_end_host_monotonic_ns=300, measurement_start_source_sim_time_s=0,
            measurement_end_source_sim_time_s=60, uavs={str(i): dict(armed=True, takeoff=True, landed=True, auto_disarm=True) for i in range(1, 6)})
        self.stats = dict(stop_reason='duration', uav_count=5, radio_node_count=6, shared_spectrum_channel_count=1,
                          wifi_topology='adhoc', sionna_drives_rx_psd=True)
        self.events = []
        for i in range(1, 6):
            for stage in ('takeoff_observed', 'landing_disarm_observed', 'waypoint_reached_telemetry'):
                self.events.append(dict(uav_id=i, stage=stage, waypoint_index=0,
                    target_position_m=list(self.scenario.routes[i][0]), progression_source='radio_delivered_LOCAL_POSITION_NED'))
        self.commands = [dict(uav_id=i, lease_status='valid', lineage_status='valid', application_chain_complete=True,
                         policy_allowed=True, admitted_ns=200) for i in range(1, 6)]
        self.payload = dict(measurement_eligible=False, status='recorded', audit_errors=[],
            invalid_trace_rows=0, orphan_receipts=0, trace_files=[dict(status='observed')],
            records=[dict(uav_id=i, direction='gcs_to_uav', outcome='timely') for i in range(1, 6)])
        self.nodes = [dict(id='cp', role='command_post', position_m=[0., 0., 2.])]
        self.nodes += [dict(id=f'uav{i}', role='uav', stale=False, source_sim_time_s=1, position_m=list(self.scenario.routes[i][0])) for i in range(1, 6)]
        self.write(self.raw / 'logs/node_state.json', dict(session_id='fixture'))
        self.positions()
        self.capture = dict(sha256='abc', bytes=123, rows=3, malformed_rows=0, stable=True)
        self.write(self.raw / 'metrics/native_source_summary.json', dict(event_capture=dict(status='observed', sha256='abc', bytes=123)))
        self.segment = dict(id='source1', start_s=20, stop_s=40, pattern='tr38901', center_hz=2412000000., bandwidth_hz=20e6, power_w=1.)
        self.schedule = dict(actual_schedule_status='observed', actual_sources=dict(segments=[self.segment]))
        for label in ('native_sources', 'native_sources_actual'):
            self.write(self.raw / f'logs/{label}.json', self.schedule)
            for stage in ('initial', 'final'):
                self.write(self.raw / f'configuration/{stage}/inputs/{label}/value.json', self.schedule)
        params = 'pattern=tr38901;center_hz=2412000000;bandwidth_hz=20000000;power_w=1'
        self.native = [dict(node='source1', event=event, time_s=str(at), details=params) for event, at in [('jammer_on', 20), ('jammer_off', 40)]]
        self.native.append(dict(node='uav1', event='spectrum_signal_arrival', time_s='25', details='foreign_signal=1', value='-50'))

    @staticmethod
    def write(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8')

    def positions(self):
        self.write(self.raw / 'logs/node_state.jsonl', dict(published_monotonic_ns=200, source='gazebo_odometry',
            type='node_state', schema_version=2, coordinate_frame='ENU', session_id='fixture', nodes=self.nodes))

    def evaluate(self):
        with patch('scripts.product.urban_mission.UrbanScenario.load', return_value=self.scenario), \
                patch('bas.node_state_journal_verification', return_value=dict(passed=True)), \
                patch('scripts.product.summarize_native_radio_five_uav._native_source_schedule', return_value=self.schedule):
            return refined_case_evidence(self.raw, self.meta, self.stats, {'active_uart_channels': 'control,payload'},
                self.events, self.native, self.commands, self.payload, [], self.capture)

    def test_complete_fixture_observes_flight_but_never_claims_hardware(self):
        result = self.evaluate()
        self.assertTrue(result['flight_case_passed'], result)
        self.assertEqual(result['hitl'], 'separate_bench_required')
        self.assertEqual(result['observations']['agl_m_by_uav']['5']['min'], 30)
        # The stricter service/SLA eligibility is intentionally separate.
        self.assertFalse(self.payload['measurement_eligible'])

    def test_summary_success_cannot_replace_route_radio_channel_or_payload_evidence(self):
        for mutate, gate in [
            (lambda: self.events.pop(), 'five_uav_flight_lifecycle'),
            (lambda: self.stats.update(shared_spectrum_channel_count=2), 'one_shared_radio_channel'),
            (lambda: self.payload['records'].pop(), 'two_mavlink_uart_channels'),
            (lambda: self.capture.update(malformed_rows=1), 'native_event_capture'),
            (lambda: self.capture.update(sha256='changed'), 'native_event_capture')]:
            with self.subTest(gate=gate):
                saved = copy.deepcopy((self.events, self.stats, self.payload, self.capture))
                mutate()
                result = self.evaluate()
                self.assertFalse(result['checks'][gate])
                self.assertFalse(result['flight_case_passed'])
                self.events, self.stats, self.payload, self.capture = saved

    def test_runtime_failure_and_missing_frozen_inputs_cannot_pass(self):
        self.meta['measurement_validity'] = 'invalid'
        self.assertFalse(self.evaluate()['flight_case_passed'])
        self.meta['measurement_validity'] = 'valid'
        (self.raw / 'configuration/initial/inputs/geometry_source/value.json').unlink()
        result = self.evaluate()
        self.assertFalse(result['checks']['frozen_terrain'])
        self.assertTrue(result['evidence_errors'])

    def test_mutable_geometry_fallback_is_rejected(self):
        path = self.raw / 'logs/urban_scenario.json'
        value = json.loads(path.read_text())
        value['terrain_bundle'] = '.external/terrain-reference'
        self.write(path, value)
        result = self.evaluate()
        self.assertFalse(result['checks']['frozen_terrain'])
        self.assertIn('not the frozen bundle', result['evidence_errors'][0])

    def test_low_flight_missing_member_and_collision_are_reported_from_samples(self):
        for mutate in (lambda: self.nodes[-1]['position_m'].__setitem__(2, 5),
                       lambda: self.nodes.pop(),
                       lambda: self.nodes[-1].update(position_m=[105., 105., 30.])):
            saved = copy.deepcopy(self.nodes)
            mutate()
            self.positions()
            self.assertFalse(self.evaluate()['checks']['gazebo_positions_and_agl'])
            self.nodes = saved

    def test_declared_source_without_matching_observed_switches_and_rf_does_not_pass(self):
        for mutate in (lambda: self.native.pop(),
                       lambda: self.native[0].update(details='pattern=tr38901;center_hz=2447000000;bandwidth_hz=20000000;power_w=1'),
                       lambda: self.native[1].update(time_s='41')):
            saved = copy.deepcopy(self.native)
            mutate()
            self.assertFalse(self.evaluate()['checks']['separate_em_source_observed'])
            self.native = saved

    def test_no_source_is_partial_and_old_scenario_not_applicable(self):
        self.schedule = dict(actual_schedule_status='not_recorded', actual_sources=None)
        result = self.evaluate()
        self.assertFalse(result['flight_case_passed'])
        self.assertTrue(result['checks']['runtime_and_complete_window'])
        self.write(self.raw / 'logs/urban_scenario.json', dict(scenario='legacy'))
        self.assertEqual(self.evaluate()['status'], 'not_applicable')

    def test_changed_source_snapshot_and_old_pose_clock_cannot_supply_evidence(self):
        path = self.raw / 'configuration/initial/inputs/native_sources/value.json'
        self.write(path, dict(changed=True))
        self.assertFalse(self.evaluate()['checks']['separate_em_source_observed'])
        self.nodes[-1]['source_sim_time_s'] = -1
        self.positions()
        self.assertFalse(self.evaluate()['checks']['gazebo_positions_and_agl'])

    def test_conflicting_duplicate_pose_or_unknown_schema_is_not_ignored(self):
        path = self.raw / 'logs/node_state.jsonl'
        first = path.read_text()
        row = json.loads(first)
        row['nodes'][-1]['position_m'][2] += 1
        path.write_text(first + '\n' + json.dumps(row))
        self.assertFalse(self.evaluate()['checks']['gazebo_positions_and_agl'])
        row['schema_version'] = 3
        self.write(path, row)
        self.assertFalse(self.evaluate()['checks']['gazebo_positions_and_agl'])

    def test_csv_fingerprint_uses_original_bytes_including_quoted_linebreaks(self):
        path = self.root / 'trace.csv'
        data = b'event,details\r\njammer_on,"one\r\ntwo"\r\n'
        path.write_bytes(data)
        evidence = {}
        rows = read_csv(path, evidence=evidence)
        self.assertEqual(rows, [dict(event='jammer_on', details='one\r\ntwo')])
        self.assertEqual(evidence, dict(sha256=hashlib.sha256(data).hexdigest(), bytes=len(data), rows=1, retained_rows=1, malformed_rows=0, stable=True))
        path.write_bytes(b'event,details\njammer_on\n')
        read_csv(path, evidence=evidence)
        self.assertEqual(evidence['malformed_rows'], 1)

    def test_streaming_selection_hashes_and_counts_even_discarded_malformed_rows(self):
        path = self.root / 'mixed.csv'
        data = (b'event,details\nrealtime_lag,value=1\nphy_rx_ok,ordinary\n'
                b'spectrum_signal_arrival,foreign_signal=0\n'
                b'spectrum_signal_arrival,foreign_signal=10\n'
                b'spectrum_signal_arrival,foreign_signal=1\njammer_on,source=ns3\n'
                b'phy_rx_ok\n')
        path.write_bytes(data)
        full_evidence, selected_evidence = {}, {}
        full = read_csv(path, evidence=full_evidence)
        selected = read_csv(path, evidence=selected_evidence, keep=native_analysis_event)
        self.assertEqual(selected, [row for row in full if native_analysis_event(row)])
        self.assertEqual([row['event'] for row in selected], ['realtime_lag', 'spectrum_signal_arrival', 'jammer_on'])
        self.assertEqual(selected_evidence['rows'], 7)
        self.assertEqual(selected_evidence['retained_rows'], 3)
        self.assertEqual(selected_evidence['malformed_rows'], 1)
        self.assertEqual(selected_evidence['sha256'], hashlib.sha256(data).hexdigest())
        self.assertEqual({k: v for k, v in selected_evidence.items() if k != 'retained_rows'},
                         {k: v for k, v in full_evidence.items() if k != 'retained_rows'})
        self.capture = selected_evidence
        self.write(self.raw / 'metrics/native_source_summary.json', dict(event_capture=dict(
            status='observed', sha256=selected_evidence['sha256'], bytes=selected_evidence['bytes'])))
        result = self.evaluate()
        self.assertFalse(result['checks']['native_event_capture'])
        self.assertFalse(result['flight_case_passed'])


if __name__ == '__main__':
    unittest.main()
