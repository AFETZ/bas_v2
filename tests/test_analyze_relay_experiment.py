"""Recorded-artefact and aggregation fixtures; no flight, RF or hardware proof."""
import copy
import csv
import json
from pathlib import Path
import tempfile
import unittest

from scripts.product.analyze_relay_experiment import (
    ENGINEERING_DELIVERY_V1, MISSION_FIELDS, SERVICE_FIELDS,
    aggregate_campaign, analyze_run, campaign_record, engineering_delivery, runtime_timing_evidence,
)


def samples(delays, *, epoch=1):
    offers, commands = [], []
    for slot, delay in enumerate(delays):
        cid = f'c{slot}'
        offers.append(dict(request_id=f'r{slot}', command_id=cid, uav_id=1,
            host_monotonic_ns=slot * 10**9, scheduled_host_monotonic_ns=slot * 10**9))
        if delay == 'unavailable':
            continue
        commands.append(dict(command_id=cid, uav_id=1, leader_id=1, epoch=epoch,
            created_ns=slot * 10**9,
            admitted_ns=None if delay is None else slot * 10**9 + delay * 10**6,
            latency_ms=delay, lease_status='valid', lineage_status='valid',
            application_chain_complete=True, policy_allowed=True))
    return offers, commands


def timing_samples(start=10**18 + 13, duration_s=1):
    meta = dict(measurement_start_host_monotonic_ns=start, measurement_duration_wall_s=duration_s)
    rows = [dict(event='realtime_lag', wall_monotonic_ns=str(start + i * 50_000_000),
                 time_s=str(10 + i * .05), value='1') for i in range(-1, int(duration_s * 20) + 1)]
    return meta, rows


class DeliveryAnalysisTests(unittest.TestCase):
    def test_missing_unavailable_late_and_invalid_are_retained_and_break_jitter(self):
        offers, commands = samples([50, 70, None, 90, 100, 'unavailable', 300, 20])
        commands[-1]['lineage_status'] = 'unverifiable_or_mismatched'
        row = engineering_delivery(offers, commands, [1], 1.)['1']
        self.assertEqual([row[k] for k in ('offered', 'timely', 'late', 'missing', 'unavailable', 'unverifiable')],
                         [8, 4, 1, 1, 1, 1])
        self.assertEqual(row['timely_per_offered'], .5)
        self.assertEqual(row['created_to_uart_latency_ms']['max'], 300)
        self.assertEqual(row['created_to_uart_latency_ms']['p99'], 292)
        jitter = row['consecutive_delay_variation_ms']
        self.assertEqual(jitter['samples'], 2)
        self.assertEqual(jitter['max'], 20)
        self.assertEqual(jitter['p95'], 19.5)

    def test_flow_change_schedule_gap_and_reused_cid_cannot_create_jitter_or_delivery(self):
        offers, commands = samples([20, 30, 40, 50, 60])
        commands[1]['epoch'] = 2
        offers[3]['scheduled_host_monotonic_ns'] += 10**9
        offers[4]['scheduled_host_monotonic_ns'] += 10**9
        offers[4]['command_id'] = offers[3]['command_id']
        row = engineering_delivery(offers, commands, [1], 1.)['1']
        self.assertEqual(row['unverifiable'], 1)
        self.assertEqual(row['timely'], 4)
        self.assertEqual(row['consecutive_delay_variation_ms']['samples'], 0)
        self.assertEqual(row['excluded_jitter_slots_or_pairs']['changed_flow'], 2)
        self.assertEqual(row['excluded_jitter_slots_or_pairs']['nonconsecutive_offered_slots'], 1)

    def test_deadline_censoring_is_not_counted_as_observed_packet_loss(self):
        offers, commands = samples([None, None])
        row = engineering_delivery(offers, commands, [1], 1., observation_end_ns=1_100_000_000)['1']
        self.assertEqual(row['missing'], 1)
        self.assertEqual(row['unverifiable'], 1)
        self.assertEqual(row['right_censored_deadline_slots'], 1)
        self.assertEqual(row['offered'], 2)

    def test_real_logged_lineage_and_csv_offer_join_uses_fixed_engineering_deadline(self):
        with tempfile.TemporaryDirectory() as temporary:
            raw, output = Path(temporary) / 'raw', Path(temporary) / 'derived'
            for folder in ('logs', 'metrics'):
                (raw / folder).mkdir(parents=True)
            meta = dict(run_id='fixture', status='completed', policy='fixed', fault_profile='none',
                uav_ids=[1], measurement_validity='valid', measurement_start_host_monotonic_ns=10**9,
                measurement_duration_wall_s=5, measurement_duration_source_s=5)
            config = dict(initial_master=1, duration_s=5, command_period_s=1,
                          effective_service_args=dict(command_max_age_ms=250))
            (raw / 'metrics/relay_experiment_summary.json').write_text(json.dumps(meta))
            (raw / 'metrics/relay_experiment_config.json').write_text(json.dumps(config))
            _, timing = timing_samples(10**9, 5)
            native_path = raw / 'logs/native_radio_events.csv'
            with native_path.open('w', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=list(timing[0]))
                writer.writeheader()
                writer.writerows(timing)
            events = [dict(stage='grant_issued', host_monotonic_ns=0, grant_sequence=1,
                           epoch=1, leader_id=1, valid_from_ns=0, valid_until_ns=10**10)]
            offers = []
            for slot, delay in enumerate((30, 70, None, 'unavailable'), 1):
                created, cid = slot * 10**9, f'c{slot}'
                offers.append(dict(request_id=f'r{slot}', command_id=cid if delay != 'unavailable' else '',
                    uav_id=1, host_monotonic_ns=created, scheduled_host_monotonic_ns=created))
                if delay == 'unavailable':
                    continue
                base = dict(command_id=cid, epoch=1, leader_id=1, uav_id=1,
                    command_created_host_monotonic_ns=created, payload_sha256='same-payload',
                    target_frame='ENU', target_position_m=[0, 0, 10])
                for stage, node in (('gcs_send', 0), ('master_receive', 1), ('follower_receive', 1)):
                    events.append(dict(base, stage=stage, node_id=node, host_monotonic_ns=created))
                if delay is not None:
                    end = created + delay * 10**6
                    events.append(dict(base, stage='uart_admitted', node_id=1, host_monotonic_ns=end + 1,
                        grant_sequence=1, valid_from_ns=0, valid_until_ns=10**10,
                        uart_write_started_host_monotonic_ns=created + 1,
                        uart_write_end_host_monotonic_ns=end, admission_checked_host_monotonic_ns=end + 1))
            (raw / 'logs/coordination_events.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in events))
            with (raw / 'metrics/relay_control_refresh.csv').open('w', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=list(offers[0]))
                writer.writeheader()
                writer.writerows(offers)
            before = {str(p.relative_to(raw)): p.read_bytes() for p in raw.rglob('*') if p.is_file()}
            result = analyze_run(raw, output, deadline_ms=50, plots=False, live_export=False)
            engineering = result['engineering_delivery_v1']
            self.assertTrue(engineering['measurement_eligible'])
            self.assertEqual(engineering['runtime_timing']['status'], 'passed')
            self.assertEqual(engineering['status'], 'observed_limits_failed')
            self.assertEqual(engineering['per_uav']['1']['timely'], 2)  # Fixed250, not exploratory50.
            self.assertEqual(result['rates']['uav1']['timely'], 1)
            self.assertEqual(engineering['per_uav']['1']['missing'], 1)
            self.assertEqual(engineering['per_uav']['1']['unavailable'], 1)
            self.assertEqual(engineering['per_uav']['1']['created_to_uart_latency_ms']['p99'], 69.6)
            self.assertEqual(json.loads((output / 'metrics/relay_analysis.json').read_text())['engineering_delivery_v1'], engineering)
            self.assertEqual(before, {str(p.relative_to(raw)): p.read_bytes() for p in raw.rglob('*') if p.is_file()})
            native_bytes = native_path.read_bytes()
            native_path.unlink()
            missing_timing = analyze_run(raw, output, plots=False, live_export=False)
            self.assertFalse(missing_timing['engineering_delivery_v1']['measurement_eligible'])
            self.assertEqual(missing_timing['engineering_delivery_v1']['runtime_timing']['status'], 'not_evaluated')
            self.assertEqual(missing_timing['run_status'], 'completed')  # Qualification does not rewrite runtime outcome.
            native_path.write_bytes(native_bytes)
            # Fully observed short happy path still cannot establish a series SLA.
            with (raw / 'metrics/relay_control_refresh.csv').open('w', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=list(offers[0]))
                writer.writeheader()
                writer.writerows(offers[:2])
            short = analyze_run(raw, output, plots=False, live_export=False)['engineering_delivery_v1']
            self.assertTrue(all(short['observed_limit_checks']['1'].values()))
            self.assertEqual(short['status'], 'insufficient_evidence')

    def record(self, seed, run=1):
        offers, commands = samples([40] * 1001)
        metrics = engineering_delivery(offers, commands, [1], 1.)
        cfg = {key: 1 for key in MISSION_FIELDS}
        cfg['effective_service_args'] = {key: 1 for key in SERVICE_FIELDS}
        cfg['effective_service_args']['command_max_age_ms'] = 250
        stats = dict(rng_seed=seed, rng_run=run,
            wifi_explicit_bar_after_missed_block_ack=False, wifi_aggregation_enabled=False)
        env = dict(git_head='frozen-head', source_dirty_paths='0',
            wifi_explicit_bar_after_missed_block_ack_requested='0', wifi_aggregation_requested='0')
        result = dict(run_id=f's{seed}r{run}', output_dir='derived', policy='fixed', fault_profile='none',
            pilot=False, run_status='completed', observation_window=dict(complete=True),
            data_quality_errors=[], unverifiable_admissions=0, rates={}, definitions={},
            engineering_delivery_v1=dict(limits=ENGINEERING_DELIVERY_V1,
                status='insufficient_evidence', measurement_eligible=True, per_uav=metrics,
                runtime_timing=runtime_timing_evidence(*timing_samples(), dict(runtime_lag_max_ms=250))),
            metadata=dict(config=cfg, environment=env, native_stats=stats,
                configuration_snapshot=dict(container_image_id='sha256:' + '0' * 64)))
        return campaign_record(dict(run_id=result['run_id'], pair_id=result['run_id'], policy='fixed',
            fault_profile='none', rng_seed=seed, rng_run=run), result)

    def test_series_requires_ten_actual_seeds_and_samples_per_uav(self):
        one = self.record(1)
        self.assertTrue(one['network_eligible'])
        self.assertEqual(aggregate_campaign([one])['engineering_delivery_v1'][0]['status'], 'insufficient_evidence')
        repeated_seed = [self.record(1, i) for i in range(1, 11)]
        same = aggregate_campaign(repeated_seed)['engineering_delivery_v1'][0]
        self.assertEqual(same['distinct_actual_rng_seeds'], [1.])
        self.assertEqual(same['status'], 'insufficient_evidence')
        independent = [self.record(seed) for seed in range(1, 11)]
        result = aggregate_campaign(independent)['engineering_delivery_v1'][0]
        self.assertEqual(result['per_uav']['1']['jitter_samples'], 10000)
        self.assertEqual(result['status'], 'passed')

    def test_series_cannot_hide_bad_run_or_pool_changed_frozen_source(self):
        records = [self.record(seed) for seed in range(1, 11)]
        bad = copy.deepcopy(records)
        bad[0]['engineering_delivery_v1']['per_uav']['1']['created_to_uart_latency_ms']['p99'] = 151
        self.assertEqual(aggregate_campaign(bad)['engineering_delivery_v1'][0]['status'], 'observed_limits_failed')
        changed = copy.deepcopy(records)
        changed[0]['comparability']['source_head'] = 'other-head'
        result = aggregate_campaign(changed)
        self.assertFalse(result['global_frozen_configuration_consistent'])
        self.assertEqual(result['engineering_delivery_v1'][0]['status'], 'insufficient_evidence')
        changed_sources = copy.deepcopy(records)
        changed_sources[0]['comparability']['native_sources'] = dict(clock='measurement_elapsed_seconds', power_w=1)
        self.assertEqual(aggregate_campaign(changed_sources)['engineering_delivery_v1'][0]['status'], 'insufficient_evidence')
        missing = copy.deepcopy(records)
        missing[0]['network_eligible'] = False
        self.assertEqual(aggregate_campaign(missing)['engineering_delivery_v1'][0]['status'], 'insufficient_evidence')

    def test_series_needs_timing_evidence_even_if_legacy_measurement_flag_is_true(self):
        records = [self.record(seed) for seed in range(1, 11)]
        records[0]['engineering_delivery_v1']['runtime_timing']['eligible'] = False
        result = aggregate_campaign(records)['engineering_delivery_v1'][0]
        self.assertFalse(result['runtime_timing_eligible'])
        self.assertFalse(result['comparable_measurements'])
        self.assertEqual(result['status'], 'insufficient_evidence')
        del records[0]['engineering_delivery_v1']['runtime_timing']
        self.assertEqual(aggregate_campaign(records)['engineering_delivery_v1'][0]['status'], 'insufficient_evidence')


class RuntimeTimingEvidenceTests(unittest.TestCase):
    def evidence(self, meta, rows):
        return runtime_timing_evidence(meta, rows, dict(runtime_lag_max_ms=250))

    def test_exact_integer_host_window_is_half_open_and_excludes_external_lag(self):
        meta, rows = timing_samples()
        rows[0]['value'] = '999'
        rows[-1]['value'] = '999'
        result = self.evidence(meta, rows)
        self.assertEqual(result['status'], 'passed')
        self.assertTrue(result['eligible'])
        self.assertEqual(result['window']['start_host_monotonic_ns'], 10**18 + 13)
        self.assertEqual(result['window']['end_host_monotonic_ns'], 10**18 + 13 + 10**9)
        self.assertEqual(result['selected_sample_rows'], 20)
        self.assertEqual(result['observed_max_lag_ms'], 1)
        self.assertEqual(result['host_sample_gaps_ms']['max'], 50)
        self.assertEqual(result['recorded_runtime_hard_abort_ms'], 250)
        self.assertIn('no continuous maximum', result['scope'])

    def test_lag_p99_and_observed_max_are_independent_of_250ms_hard_abort(self):
        for value, checks in (('21', dict(p99=False, observed_max=True)),
                              ('51', dict(p99=False, observed_max=False))):
            meta, rows = timing_samples()
            for row in rows:
                row['value'] = value
            result = self.evidence(meta, rows)
            self.assertEqual(result['status'], 'observed_limits_failed')
            self.assertEqual(result['checks'], checks)
            self.assertFalse(result['eligible'])
            self.assertEqual(result['recorded_runtime_hard_abort_ms'], 250)

    def test_missing_or_invalid_data_never_substitute_zero_or_qualify(self):
        meta, rows = timing_samples()
        for modify in (lambda values: values.clear(),
                       lambda values: values[4].update(value='nan'),
                       lambda values: values[4].update(value='-1'),
                       lambda values: values[4].update(wall_monotonic_ns='1000000000.5'),
                       lambda values: values[4].update(wall_monotonic_ns=True),
                       lambda values: values[4].update(time_s='invalid')):
            values = copy.deepcopy(rows)
            modify(values)
            with self.subTest(values=values):
                result = self.evidence(meta, values)
                self.assertEqual(result['status'], 'not_evaluated')
                self.assertEqual(result['checks'], dict(p99=None, observed_max=None))
                self.assertFalse(result['eligible'])
                self.assertTrue(result['coverage_problems'])
                if not values:
                    self.assertIsNone(result['lag_ms']['p99'])
                    self.assertIsNone(result['observed_max_lag_ms'])

    def test_sampler_gaps_duplicates_resets_and_censored_boundaries_are_unqualified(self):
        meta, rows = timing_samples()
        gap = copy.deepcopy(rows)
        del gap[5]
        self.assertIn('sampler_simulation_period_gap_or_reset', self.evidence(meta, gap)['coverage_problems'])
        duplicate = copy.deepcopy(rows)
        duplicate.insert(5, duplicate[5])
        self.assertIn('sampler_host_clock_not_strictly_increasing', self.evidence(meta, duplicate)['coverage_problems'])
        reset = copy.deepcopy(rows)
        reset[5]['time_s'] = '0'
        self.assertFalse(self.evidence(meta, reset)['eligible'])
        stalled = copy.deepcopy(rows)
        for row in stalled[8:]:
            row['wall_monotonic_ns'] = str(int(row['wall_monotonic_ns']) + 120_000_000)
        self.assertIn('sampler_host_gap_exceeded', self.evidence(meta, stalled)['coverage_problems'])
        for values in (rows[2:], rows[:-1]):
            result = self.evidence(meta, values)
            self.assertIn('measurement_boundary_coverage_censored', result['coverage_problems'])
            self.assertEqual(result['status'], 'not_evaluated')

    def test_inferred_or_inconsistent_measurement_window_does_not_qualify(self):
        meta, rows = timing_samples()
        for value in ({}, dict(meta, measurement_start_host_monotonic_ns=float(10**18 + 13)),
                      dict(meta, measurement_duration_wall_s='1'),
                      dict(meta, measurement_end_host_monotonic_ns=10**18 + 13 + 2 * 10**9)):
            result = self.evidence(value, rows)
            self.assertEqual(result['status'], 'not_evaluated')
            self.assertIn('exact_recorded_host_window_missing_or_invalid', result['coverage_problems'])


if __name__ == '__main__':
    unittest.main()
