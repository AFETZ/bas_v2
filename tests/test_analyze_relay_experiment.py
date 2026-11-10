"""Recorded-artefact and aggregation fixtures; no flight, RF or hardware proof."""
import copy
import csv
import json
from pathlib import Path
import tempfile
import unittest

from scripts.product.analyze_relay_experiment import (
    ENGINEERING_DELIVERY_V1, MISSION_FIELDS, SERVICE_FIELDS,
    aggregate_campaign, analyze_run, campaign_record, engineering_delivery, runtime_timing_evidence, analyze_payload_lineage,
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
            self.assertEqual(result['service_class_qualification']['status'], 'not_measured')
            self.assertEqual(result['run_status'], 'completed')
            self.assertTrue((output / 'metrics/service_class_qualification.json').is_file())
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


class PayloadLineageAnalysisTests(unittest.TestCase):
    """Controlled artifact clocks and actual MAVLink CRC; not a radio run."""
    def setUp(self):
        from pymavlink.dialects.v20 import ardupilotmega as mavlink
        from network.scripts.serial_transport import mavlink_payload_lineage
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.run=Path(temporary.name)
        self.ground=self.run/'payload/logs/scenario_events.jsonl'
        self.uart=self.run/'logs/payload_uart_uav1.jsonl'
        self.rows={self.ground:[],self.uart:[]}
        tx=mavlink.MAVLink(None,srcSystem=1,srcComponent=1)
        message=tx.attitude_encode(12345,.1,.2,.3,0.,0.,0.)
        frame=bytes(message.pack(tx,force_mavlink1=False))
        parsed=mavlink.MAVLink(None).parse_buffer(frame)[0]
        self.meta=mavlink_payload_lineage(parsed,[((0,200_000),0,len(frame))],
            run_id=self.run.name,uav_id=1,direction='uav_to_gcs')
        for path,epoch in [(self.ground,'gcs:1'),(self.uart,'uart:1')]:
            for event,stamp in [('payload_lineage_started',100_000),('payload_lineage_closed',2_000_000_000)]:
                self.rows[path].append(dict(schema=1,event=event,run_id=self.run.name,process_epoch=epoch,
                    clock_domain='host_monotonic',observed_host_monotonic_ns=stamp,close_error=None))
        self.offer=dict(self.meta,schema=1,event='mavlink_frame',payload_lineage_stage='offered',
            payload_direction='uav_to_gcs',run_id=self.run.name,uav_id=1,channel='payload',process_epoch='uart:1',
            clock_domain='host_monotonic',observed_host_monotonic_ns=200_000,
            created_host_monotonic_ns=200_000,deadline_host_monotonic_ns=1_000_200_000)
        self.tx=dict(schema=1,event='payload_record_sent',uav_id=1,sequence=0,producer_host_monotonic_ns=200_000,
            fragment_index=0,fragment_count=1,record_crc32=123,record_bytes=self.meta['frame_bytes'],
            transmitted_host_monotonic_ns=300_000)
        self.receipt=dict(self.meta,schema=1,event='payload_frame_accepted',direction='uav_to_gcs',
            run_id=self.run.name,uav_id=1,channel='payload',process_epoch='gcs:1',clock_domain='host_monotonic',
            observed_host_monotonic_ns=500_000,accepted_host_monotonic_ns=500_000)

    def analyze(self):
        for path,rows in self.rows.items():
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text(''.join(json.dumps(row)+'\n' for row in rows),encoding='utf-8')
        return analyze_payload_lineage(self.run,100_000,1_000_000,complete=True)

    def test_unique_payload_body_delivery_has_deadline_and_duplicate_safe_denominator(self):
        self.rows[self.uart]+=[self.offer,self.tx]
        self.rows[self.ground]+=[self.receipt,copy.deepcopy(self.receipt)]
        result=self.analyze()
        self.assertTrue(result['measurement_eligible'],result)
        self.assertEqual(result['records'][0]['outcome'],'timely')
        self.assertEqual(result['records'][0]['duplicate_receipts'],1)
        stream=next(iter(result['per_stream'].values()))
        self.assertEqual(stream['offered'],1)
        self.assertEqual(stream['delivered_unique'],1)
        self.assertEqual(stream['mavlink_body_goodput_bps'],self.meta['mavlink_body_bytes']*8/.0009)
        self.assertEqual(result['service_class_availability'],'not_evaluated_requires_qualified_load_and_series')

    def test_missing_tail_and_incomplete_transmission_do_not_become_delivered(self):
        self.rows[self.uart]+=[self.offer,self.tx]
        result=self.analyze()
        self.assertEqual(result['records'][0]['outcome'],'missing')
        self.assertEqual(next(iter(result['per_stream'].values()))['timely_per_offered'],0.)
        self.rows[self.uart].remove(self.tx)
        self.assertEqual(self.analyze()['records'][0]['outcome'],'not_fully_transmitted')

    def test_postwindow_drain_is_a_deadline_outcome_not_inwindow_goodput(self):
        self.rows[self.uart]+=[self.offer,self.tx]
        self.receipt['accepted_host_monotonic_ns']=2_000_000
        self.rows[self.ground].append(self.receipt)
        result=self.analyze()
        self.assertEqual(result['records'][0]['outcome'],'timely')
        self.assertEqual(next(iter(result['per_stream'].values()))['mavlink_body_goodput_bps'],0.)

    def test_censored_missing_or_default_absent_trace_cannot_qualify(self):
        self.assertEqual(analyze_payload_lineage(self.run)['status'],'not_evaluated_missing_trace')
        self.rows[self.uart]+=[self.offer,self.tx]
        self.rows[self.ground][1]['observed_host_monotonic_ns']=900_000
        result=self.analyze()
        self.assertEqual(result['records'][0]['outcome'],'right_censored')
        self.assertFalse(result['measurement_eligible'])

    def test_target_restart_gap_cannot_join_one_epoch_start_to_another_epoch_close(self):
        self.rows[self.uart]+=[self.offer,self.tx]
        for first_error in ('crashed',None):
            with self.subTest(first_error=first_error):
                self.rows[self.ground][1].update(observed_host_monotonic_ns=250_000,close_error=first_error)
                self.rows[self.ground]=self.rows[self.ground][:2]
                for event,stamp in [('payload_lineage_started',1_500_000_000),('payload_lineage_closed',2_000_000_000)]:
                    self.rows[self.ground].append(dict(schema=1,event=event,run_id=self.run.name,process_epoch='gcs:2',
                        clock_domain='host_monotonic',observed_host_monotonic_ns=stamp,close_error=None))
                result=self.analyze()
                self.assertIn(result['records'][0]['outcome'],('unverifiable','right_censored'))
                self.assertFalse(result['measurement_eligible'])

    def test_accepted_frame_uses_its_own_receiver_epoch_coverage(self):
        self.rows[self.uart]+=[self.offer,self.tx]
        for event,stamp in [('payload_lineage_started',400_000),('payload_lineage_closed',2_000_000_000)]:
            self.rows[self.ground].append(dict(schema=1,event=event,run_id=self.run.name,process_epoch='gcs:2',
                clock_domain='host_monotonic',observed_host_monotonic_ns=stamp,close_error=None))
        self.receipt['process_epoch']='gcs:2'
        self.rows[self.ground].append(self.receipt)
        result=self.analyze()
        self.assertEqual(result['records'][0]['outcome'],'unverifiable')
        self.assertFalse(result['measurement_eligible'])
        self.rows[self.ground][2]['observed_host_monotonic_ns']=100_000
        self.assertTrue(self.analyze()['measurement_eligible'])

    def test_downlink_artifact_ids_are_portable_and_match_the_actual_uart_endpoint(self):
        from pymavlink.dialects.v20 import ardupilotmega as mavlink
        from network.scripts.serial_transport import mavlink_payload_lineage
        tx=mavlink.MAVLink(None,srcSystem=255,srcComponent=190)
        frame=bytes(tx.heartbeat_encode(mavlink.MAV_TYPE_GCS,mavlink.MAV_AUTOPILOT_INVALID,
            0,0,mavlink.MAV_STATE_ACTIVE).pack(tx,force_mavlink1=False))
        message=mavlink.MAVLink(None).parse_buffer(frame)[0]
        meta=mavlink_payload_lineage(message,[((0,200_000),0,len(frame))],
            run_id=self.run.name,uav_id=1,direction='gcs_to_uav')
        common=dict(meta,schema=1,run_id=self.run.name,uav_id=1,channel='payload',clock_domain='host_monotonic')
        offered=dict(common,event='payload_frame_offered',process_epoch='gcs:1',direction='gcs_to_uav',
            created_host_monotonic_ns=200_000,deadline_host_monotonic_ns=1_000_200_000,
            observed_host_monotonic_ns=200_000)
        self.rows[self.ground]+=[offered,dict(offered,event='payload_frame_sent',
            expected_fragments=1,sent_fragments=1,observed_host_monotonic_ns=300_000)]
        self.rows[self.uart].append(dict(common,event='mavlink_frame',payload_lineage_stage='accepted',
            payload_direction='gcs_to_uav',process_epoch='uart:1',endpoint='uart_os_write_end',
            accepted_host_monotonic_ns=500_000,observed_host_monotonic_ns=500_000))
        result=self.analyze()
        self.assertTrue(result['measurement_eligible'],result)
        self.assertEqual(result['records'][0]['outcome'],'timely')
        self.assertEqual(result['records'][0]['target_process_epoch'],'uart:1')
        self.assertEqual({row['path'] for row in result['trace_files']},
            {'payload/logs/scenario_events.jsonl','logs/payload_uart_uav1.jsonl'})

    def test_duplicate_offer_or_corrupt_proof_is_unverifiable_not_new_goodput(self):
        self.rows[self.uart]+=[self.offer,self.tx,copy.deepcopy(self.offer)]
        self.rows[self.ground]+=[self.receipt]
        result=self.analyze()
        self.assertEqual(len(result['records']),2)
        self.assertTrue(all(row['outcome']=='unverifiable' for row in result['records']))
        self.assertFalse(result['measurement_eligible'])
        self.rows[self.uart].pop()
        self.rows[self.ground][-1]['frame_hex']='00'+self.receipt['frame_hex'][2:]
        result=self.analyze()
        self.assertFalse(result['measurement_eligible'])
        self.assertGreater(result['invalid_trace_rows'],0)


if __name__ == '__main__':
    unittest.main()
