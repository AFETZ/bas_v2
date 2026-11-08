"""Observed native execution evidence; these fixtures are not RF acceptance runs."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import yaml

from scripts.product.native_rt_workers import VERSIONS
from scripts.product.capture_effective_config import capture_snapshot
from scripts.product.prepare_native_sources import prepare, materialize
from scripts.product.summarize_native_radio_five_uav import (
    ROOT, native_product_runtime_contract, native_rt_worker_contract)


class NativeRadioContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.scene = self.root / 'scene.xml'
        self.scene.write_text('<scene version="3.0.0"/>', encoding='utf-8')
        self.profile = yaml.safe_load((ROOT / 'network/config/native_wifi_80211n_spectrum_product.yaml').read_text())

    def evidence(self, count=5, pool=False, full_mesh=False):
        scenario = copy.deepcopy(self.profile)
        scenario.update(path=str(self.root / 'scenario.yaml'), map={'scene_xml': str(self.scene)},
                        ground_positions={f'uav{i}': [i, 0, 1] for i in range(1, count + 1)})
        radio, rt = scenario['radio'], scenario['sionna']
        rt.update(execution_mode='worker_pool' if pool else 'in_process', worker_count=3)
        if pool:
            rt.update(receiver_batch_mode='full_mesh' if full_mesh else 'receiver_batch', cache_expiry_jitter_fraction=0)
        stats = dict(uav_count=count, radio_node_count=count + 1, shared_spectrum_channel_count=1,
            native_ns3_phy=True, native_ns3_mac=True, custom_packet_error_model=False, custom_scheduler=False,
            radio_backend='wifi', profile=radio['profile'], technology_specific_modem=False,
            neighbor_discovery_mode=radio['neighbor_discovery_mode'], wifi_data_mode=radio['data_mode'],
            wifi_control_mode=radio['control_mode'], wifi_ssid=radio['ssid'],
            wifi_channel_number=radio['channel_number'], wifi_actual_channel_number=radio['channel_number'],
            phased_array_spectrum_propagation_model_count=1, scalar_base_propagation_model_count=0,
            rx_psd_propagation_model='ns3::SionnaRtSpectrumPropagationLossModel', sionna_in_process=not pool,
            sionna_drives_rx_psd=True, packet_outcome_affected=True, pose_snapshots=10, stale_pose_samples=0,
            solver_profile=rt['solver_profile'], selected_propagation_profile='sionna', tx_power_w=radio['tx_power_w'],
            carrier_hz=radio['carrier_hz'], wifi_actual_center_frequency_hz=radio['carrier_hz'],
            sionna_receiver_batch_mode=rt.get('receiver_batch_mode', 'pairwise'),
            wifi_channel_width_mhz=radio['channel_width_mhz'], sionna_rt_execution_mode=rt['execution_mode'])
        for key in ('channel_state_max_age_s', 'endpoint_displacement_threshold_m', 'readiness_lag_max_ms'):
            stats[key] = rt[key]
        stats['sionna_solver'] = {key: rt[key] for key in ('max_depth', 'los', 'specular_reflection',
            'diffuse_reflection', 'diffraction', 'edge_diffraction', 'refraction', 'synthetic_array', 'seed',
            'max_number_of_paths', 'samples_per_src', 'max_num_paths_per_src', 'cache_expiry_jitter_fraction')}
        if not pool:
            return stats, scenario
        cohort = ['cp'] + list(scenario['ground_positions'])
        applied = dict(schema=1, root=str(self.root), scene=dict(path=str(self.scene),
            sha256=hashlib.sha256(self.scene.read_bytes()).hexdigest()), normalize_delays=True,
            cohort=[{'id': name} for name in cohort], radio=dict(frequency_hz=radio['carrier_hz'],
            samples_per_source=rt['samples_per_src'], max_paths_per_source=rt['max_num_paths_per_src'],
            max_number_of_paths=rt['max_number_of_paths'], merge_shapes=True,
            solver={key: rt[key] for key in ('max_depth', 'los', 'specular_reflection', 'diffuse_reflection',
                'diffraction', 'edge_diffraction', 'refraction', 'synthetic_array', 'seed')}))
        workers = []
        if full_mesh:
            applied['desired_mode'] = 'full_mesh'
        for identity in range(3):
            ready = dict(schema=1, kind='ready', worker_id=identity, pid=100 + identity,
                versions=dict(VERSIONS), mitsuba_variant='cuda_ad_mono_polarized', gpu_solve_verified=False,
                applied_config=copy.deepcopy(applied), worker_cache_update_period_ns=1, render_images=False,
                scope='public_native_sdk_rt_producer_probe')
            workers.append(dict(worker_id=identity, pid=100 + identity, failed=None, completed=12,
                                busy=False, request_id=None, ready=ready))
        native_ns, host_ns = 100_000_000, 1_000_000_000_000
        pairs = [dict(first=a, second=b, valid=True, due=False, reason='valid', source_request_id=1,
            sample_native_ns=0, sample_host_monotonic_ns=host_ns - 50_000_000, native_age_ns=native_ns,
            host_age_ns=50_000_000, displacement_m=0., velocity_delta_m_s=0., orientation_delta_rad=0.,
            normalized_risk=.2) for a in range(len(cohort)) for b in range(a + 1, len(cohort))]
        accepted = 2 * len(pairs)
        cache = dict(hits=100, misses=0, accepted=accepted, rejected=2, maximum_hit_native_age_ns=100_000_000,
            maximum_hit_host_age_ns=50_000_000, rejection_reasons={'out_of_order': 2}, miss_reasons={}, pairs=pairs)
        stats['sionna_rt_workers'] = dict(schema=1, execution_mode='worker_pool', started=True, closed=True,
            cache_snapshot_stage='before_close', failed=False, error=None, worker_count=3, submitted_jobs=39,
            completed_jobs=36, cold_warmup_jobs=3, published_results=accepted, published_pairs=accepted,
            discarded_pairs=5, discard_reasons={'cold_warmup_not_served': 3, 'out_of_order': 2},
            poll_calls=300, max_poll_wall_ns=10_000, cache_snapshot_native_ns=native_ns,
            cache_snapshot_host_monotonic_ns=host_ns, cache=cache,
            pool=dict(schema=1, scope='native_rt_worker_pool', closed=True, workers=workers),
            log_path='/owned-run/logs/native_rt_workers/results.jsonl')
        if full_mesh:
            n = len(cohort)
            matrix = [3 if tx == rx else 2 for rx in range(n) for tx in range(n)]
            for identity, worker in enumerate(workers):
                worker['full_mesh_completed_jobs'] = worker['completed']
                worker['last_full_mesh_stats'] = dict(source_mode='full_mesh', registered_endpoints=n,
                    configured_max_paths_per_source=rt['max_num_paths_per_src'],
                    configured_samples_per_source=rt['samples_per_src'],
                    configured_max_number_of_paths=rt['max_number_of_paths'],
                    attempted_calls=12, completed_calls=12, generated_links=12*n*(n-1)//2,
                    candidate_capacity_reached_calls=0, last_transmitter_count=n, last_receiver_count=n,
                    last_per_tx_candidate_cap=(n-1)*rt['max_num_paths_per_src'],
                    last_candidate_count=20*n, last_candidate_buffer_size=n*(n-1)*rt['max_num_paths_per_src'],
                    last_candidate_counts_per_tx=[20]*n, last_self_candidate_counts_per_tx=[10]*n,
                    last_retained_paths_per_pair=list(matrix), last_retained_diagonal_paths=[3]*n,
                    last_generated_ns=40_000_000+identity*20_000_000, scene_prepare_ns=100, solver_ns=5000,
                    extraction_ns=1000, cache_publish_ns=100, batch_ns=10000, max_batch_ns=12000)
            stats['sionna_rt_workers'].update(desired_mode='full_mesh', full_mesh_completed_jobs=36,
                last_full_mesh_stats=copy.deepcopy(workers[-1]['last_full_mesh_stats']),
                pose_commit_policy='with_full_mesh_result', committed_pose_frames=accepted)
            stats['pose_application'] = dict(mode='coherent_rt', observed_snapshots=100,
                applied_snapshots=accepted, maximum_applied_pose_age_ns=50_000_000,
                state_max_age_ns=round(scenario['coupling']['state_max_age_s']*1e9))
        return stats, scenario

    def source_evidence(self, aliases=1):
        """Transport/metadata fixture only; no RT solve or RF effect is synthesized."""
        stats, scenario = self.evidence(2, pool=True, full_mesh=True)
        run = self.root / f'source-run-{aliases}'
        (run / 'logs').mkdir(parents=True)
        sources = [dict(id='interferer'+str(i), mode='continuous', spectral_shape='rectangular',
            position_m=[5.+i, 0., 5.], orientation_rad=[.3, .1], pattern='tr38901', gain_dbi=0.,
            power_w=.01, bandwidth_hz=20000000., center_hz=scenario['radio']['carrier_hz'],
            start_s=0., stop_s=.1, period_s=.01, duty_cycle=1.) for i in range(aliases)]
        configured = prepare(dict(time_reference='measurement_start', sources=sources))
        actual = materialize(configured, 10_000_000, 123456789, 'experiment_normal')
        for name, value in [('native_sources.json', configured), ('native_sources_actual.json', actual)]:
            (run / 'logs' / name).write_text(json.dumps(value), encoding='utf-8')
        capture_snapshot(run, stage='final', repo_root=ROOT, configs=[], observed=[])
        snapshot = stats['sionna_rt_workers']
        n, native_ns, host_ns = 3, 100_000_000, snapshot['cache_snapshot_host_monotonic_ns']
        sample_ns, sample_host = 80_000_000, host_ns-40_000_000
        profiles = [dict(id=f'source_segment_{i}', frequency_hz=source['center_hz'], pattern=source['pattern'])
                    for i, source in enumerate(sources)]
        source_ids = [f"{source['id']}:{i}" for i, source in enumerate(sources)]
        for worker in snapshot['pool']['workers']:
            worker.update(completed=11 if worker['worker_id']==0 else 11*aliases if worker['worker_id']==1 else 0,
                full_mesh_completed_jobs=11 if worker['worker_id']==0 else 0,
                emitter_batch_completed_jobs=11*aliases if worker['worker_id']==1 else 0)
            if worker['worker_id']==0:
                worker['last_full_mesh_stats'].update(attempted_calls=11, completed_calls=11,
                    generated_links=11*n*(n-1)//2, last_generated_ns=sample_ns)
            else:
                worker['last_full_mesh_stats']=None
            worker['ready']['applied_config']['source_profiles']=copy.deepcopy(profiles)
            worker['ready'].update(source_profile_count=aliases,
                source_model_frequencies_hz=[sources[0]['center_hz']], source_profile_contexts=[dict(
                    frequency_hz=sources[0]['center_hz'], pattern='tr38901', profile_ids=[p['id'] for p in profiles],
                    source_node_id=99, source_antenna_id=99, source_modes=['pairwise','emitter_batch'])])
        snapshot.update(submitted_jobs=11, completed_jobs=11, cold_warmup_jobs=1, full_mesh_completed_jobs=11,
            total_submitted_jobs=11*(aliases+1), total_completed_jobs=11*(aliases+1),
            published_results=10, published_pairs=10*n*(n-1)//2, committed_pose_frames=10,
            pose_commit_policy='with_full_mesh_and_required_emitters',
            last_full_mesh_stats=copy.deepcopy(snapshot['pool']['workers'][0]['last_full_mesh_stats']))
        snapshot['prefetch']=dict(method='coherent_pose_and_source_bundles',lead_ns=100_000_000)
        stats['pose_application']['applied_snapshots']=10
        snapshot['cache']['accepted']=snapshot['published_pairs']
        for pair in snapshot['cache']['pairs']:
            pair.update(source_request_id=80, sample_native_ns=sample_ns, sample_host_monotonic_ns=sample_host,
                        native_age_ns=native_ns-sample_ns)
        snapshot['source_bindings']=[]
        for i, source in enumerate(sources):
            counts=11*aliases
            emitter=dict(source_mode='emitter_batch', pattern=source['pattern'], bound=True,
                has_published_batch=True, frequency_hz=source['center_hz'], source_node_id=99, source_antenna_id=99,
                receiver_node_ids=list(range(n)), receiver_antenna_ids=list(range(n)), registered_receivers=n,
                configured_samples_per_source=scenario['sionna']['samples_per_src'],
                configured_max_paths_per_source=scenario['sionna']['max_num_paths_per_src'],
                configured_max_number_of_paths=float(scenario['sionna']['max_number_of_paths']),
                candidate_allocation_valid=True, configured_candidate_cap=n*scenario['sionna']['max_num_paths_per_src'],
                attempted_calls=counts, completed_calls=counts, generated_links=counts*n,
                candidate_capacity_reached_calls=0, cache_read_calls=counts*n, cache_read_hits=counts*n, cache_read_misses=0,
                last_transmitter_count=1, last_receiver_count=n,
                last_allocated_candidate_cap=n*scenario['sionna']['max_num_paths_per_src'], last_candidate_count=n,
                last_candidate_buffer_size=n*scenario['sionna']['max_num_paths_per_src'],
                last_retained_paths_per_receiver=[2]*n, last_generated_ns=sample_ns,
                last_source_position_m=source['position_m'], last_source_velocity_m_s=[0.,0.,0.],
                last_source_orientation_rad=[*source['orientation_rad'],0.],
                scene_prepare_ns=100, solver_ns=5000, extraction_ns=1000, cache_publish_ns=100, batch_ns=10000, max_batch_ns=12000)
            pairs=[dict(first=peer, second=n, valid=True, due=False, reason='valid', source_request_id=100+i,
                sample_native_ns=sample_ns, sample_host_monotonic_ns=sample_host, native_age_ns=native_ns-sample_ns,
                host_age_ns=50_000_000, displacement_m=0., velocity_delta_m_s=0., orientation_delta_rad=0.) for peer in range(n)]
            snapshot['source_bindings'].append(dict(id=source_ids[i], profile_id=profiles[i]['id'],
                frequency_hz=source['center_hz'], pattern=source['pattern'], worker_id=1, required_now=True,
                configured_pose=dict(position_m=source['position_m'], velocity_m_s=[0.,0.,0.],
                    orientation_rad=[*source['orientation_rad'],0.], scope='configuration_static'),
                completed_jobs=11, cold_warmup_jobs=1, published_results=10, published_pairs=10*n, live_published_results=9,
                last_emitter_batch_stats=copy.deepcopy(emitter), last_published_emitter_batch_stats=copy.deepcopy(emitter),
                last_published_bundle_id=11, last_published_request_id=100+i, last_published_desired_request_id=80,
                last_published_sample_native_ns=sample_ns, last_published_sample_host_monotonic_ns=sample_host,
                cache_snapshot_native_ns=native_ns, cache_snapshot_host_monotonic_ns=host_ns+100000,
                cache=dict(hits=10, misses=0, accepted=10*n, rejected=0, maximum_hit_native_age_ns=20_000_000,
                    maximum_hit_host_age_ns=50_000_000, rejection_reasons={}, miss_reasons={}, pairs=pairs)))
        snapshot['worker_roles']=[dict(worker_id=0,role='desired_full_mesh',source_ids=[]),
            dict(worker_id=1,role='emitter_batch',frequency_hz=sources[0]['center_hz'],pattern='tr38901',source_ids=source_ids),
            dict(worker_id=2,role='idle',source_ids=[])]
        snapshot['bundle']=dict(enabled=True, policy='desired_and_required_emitters_before_pose_notify',
            atomicity='observable_without_internal_rollback_on_failure', submitted=11, committed=10,
            failed_before_notify=0, pending={}, last_committed_required_source_ids=source_ids,
            last_committed=dict(id=11, sample_native_ns=sample_ns, sample_host_monotonic_ns=sample_host,
                pose_sample_host_ns=[host_ns-50_000_000]*n, desired_request_id=80, required_source_ids=source_ids,
                source_requests=[dict(id=sid,request_id=100+i) for i,sid in enumerate(source_ids)],
                required_lookahead_ns=10_000_000,
                required_observations=[dict(id=sid,required=True,reason='schedule_callback') for sid in source_ids],bootstrap=False))
        return stats, scenario, run

    def test_source_bundle_uses_frozen_inputs_and_shared_rf_aliases(self):
        for aliases in (1,2):
            with self.subTest(aliases=aliases):
                stats, scenario, run = self.source_evidence(aliases)
                report=native_rt_worker_contract(stats,scenario,run_dir=run)
                self.assertTrue(report['passed'],report)
                self.assertIsNone(report['source_bundle']['error'])
                self.assertIn('not WFG activation',report['source_bundle']['live_evidence_scope'])

    def test_source_bundle_rejects_missing_run_input_schedule_or_frozen_copy(self):
        stats, scenario, run = self.source_evidence()
        self.assertIn('worker_source_inputs',native_rt_worker_contract(stats,scenario)['rejection_reasons'])
        for path in [run/'logs/native_sources.json',run/'logs/native_sources_actual.json',
                     run/'configuration/final/inputs/native_sources/value.json']:
            original=path.read_bytes()
            path.unlink()
            self.assertIn('worker_source_inputs',native_rt_worker_contract(stats,scenario,run_dir=run)['rejection_reasons'])
            path.write_bytes(original)
        path=run/'logs/native_sources.json'
        path.write_text(path.read_text()+' ',encoding='utf-8')
        self.assertIn('worker_source_inputs',native_rt_worker_contract(stats,scenario,run_dir=run)['rejection_reasons'])

    def test_source_bundle_rejects_identity_rf_poses_worker_or_missing_binding(self):
        original, scenario, run = self.source_evidence()
        changes=[lambda s:s['source_bindings'].clear(),
            lambda s:s['source_bindings'][0].update(id='other:0'),
            lambda s:s['source_bindings'][0].update(profile_id='other'),
            lambda s:s['source_bindings'][0].update(frequency_hz=2.5e9),
            lambda s:s['source_bindings'][0].update(pattern='iso'),
            lambda s:s['source_bindings'][0]['configured_pose'].update(position_m=[0,0,0]),
            lambda s:s['source_bindings'][0]['configured_pose'].update(orientation_rad=[0,0,0]),
            lambda s:s['source_bindings'][0].update(worker_id=2),
            lambda s:s['worker_roles'][1].update(frequency_hz=2.5e9),
            lambda s:s['pool']['workers'][1]['ready']['applied_config']['source_profiles'][0].update(frequency_hz=2.5e9)]
        for change in changes:
            stats=copy.deepcopy(original);change(stats['sionna_rt_workers'])
            with self.subTest(change=change):
                self.assertFalse(native_rt_worker_contract(stats,scenario,run_dir=run)['passed'])

    def test_source_bundle_rejects_epochs_shape_capacity_and_false_freshness(self):
        original, scenario, run = self.source_evidence()
        changes=[lambda b:b['last_published_emitter_batch_stats'].update(last_generated_ns=0),
            lambda b:b['last_emitter_batch_stats'].update(last_receiver_count=2),
            lambda b:b['last_emitter_batch_stats'].update(candidate_capacity_reached_calls=1),
            lambda b:b['last_emitter_batch_stats'].update(last_candidate_count=b['last_emitter_batch_stats']['last_candidate_buffer_size']),
            lambda b:b['last_emitter_batch_stats'].update(last_retained_paths_per_receiver=[91]*3),
            lambda b:b['cache'].update(misses=1),
            lambda b:b['cache'].update(maximum_hit_host_age_ns=500_000_001),
            lambda b:b['cache']['pairs'][0].update(first=0,second=1),
            lambda b:b['cache']['pairs'][0].update(sample_native_ns=0),
            lambda b:b['cache']['pairs'][0].update(source_request_id=999),
            lambda b:b.update(last_published_sample_host_monotonic_ns=10**20),
            lambda b:b['cache']['pairs'].pop()]
        for change in changes:
            stats=copy.deepcopy(original);change(stats['sionna_rt_workers']['source_bindings'][0])
            with self.subTest(change=change):
                self.assertFalse(native_rt_worker_contract(stats,scenario,run_dir=run)['passed'])

    def test_source_bundle_rejects_partial_commit_wrong_required_window_and_warm_only(self):
        original, scenario, run = self.source_evidence()
        changes=[lambda s:s['bundle'].update(failed_before_notify=1),
            lambda s:s['bundle'].update(committed=9),
            lambda s:s['bundle']['last_committed'].update(required_source_ids=[]),
            lambda s:s['bundle']['last_committed']['required_observations'][0].update(required=False),
            lambda s:s['source_bindings'][0].update(last_published_desired_request_id=79),
            lambda s:s['source_bindings'][0].update(last_published_sample_native_ns=0),
            lambda s:s['bundle']['last_committed'].update(pose_sample_host_ns=[10**20]*3),
            lambda s:s['source_bindings'][0].update(live_published_results=0),
            lambda s:s['source_bindings'][0]['cache'].update(hits=0),
            lambda s:s['pool']['workers'][1].update(full_mesh_completed_jobs=1),
            lambda s:s['source_bindings'][0].update(required_now=False),
            lambda s:s.update(total_completed_jobs=11)]
        for change in changes:
            stats=copy.deepcopy(original);change(stats['sionna_rt_workers'])
            with self.subTest(change=change):
                self.assertFalse(native_rt_worker_contract(stats,scenario,run_dir=run)['passed'])

    def test_source_bundle_requires_its_actual_commit_and_prefetch_modes(self):
        original,scenario,run=self.source_evidence()
        for policy in ('with_full_mesh_result','independent',None):
            stats=copy.deepcopy(original)
            stats['sionna_rt_workers']['pose_commit_policy']=policy
            with self.subTest(policy=policy):
                self.assertIn('worker_full_mesh_pose_commit',
                    native_rt_worker_contract(stats,scenario,run_dir=run)['rejection_reasons'])
        for method in ('coherent_pose_frames','',None):
            stats=copy.deepcopy(original)
            stats['sionna_rt_workers']['prefetch']['method']=method
            with self.subTest(method=method):
                self.assertIn('worker_source_prefetch_method',
                    native_rt_worker_contract(stats,scenario,run_dir=run)['rejection_reasons'])

    def test_pending_source_bundle_is_not_a_partial_commit_or_fabricated_window(self):
        original, scenario, run = self.source_evidence()
        snapshot=original['sionna_rt_workers']
        host=snapshot['cache_snapshot_host_monotonic_ns']
        snapshot.update(submitted_jobs=12,total_submitted_jobs=24)
        snapshot['bundle'].update(submitted=12,pending=dict(id=12,sample_native_ns=90_000_000,
            sample_host_monotonic_ns=host-20_000_000,pose_sample_host_ns=[host-30_000_000]*3,
            required_source_ids=['interferer0:0'],required_lookahead_ns=10_000_000,
            desired_ready=False,source_ready_ids=[],cold_warmup=False,bootstrap=False))
        self.assertTrue(native_rt_worker_contract(original,scenario,run_dir=run)['passed'])
        for change in [lambda p:p.update(source_ready_ids=['unknown:0']),
                       lambda p:p.update(required_source_ids=[]),
                       lambda p:p.pop('required_lookahead_ns'),
                       lambda p:p.update(sample_native_ns=70_000_000),
                       lambda p:p.update(pose_sample_host_ns=[host+1]*3),
                       lambda p:p.update(desired_ready=1)]:
            stats=copy.deepcopy(original);change(stats['sionna_rt_workers']['bundle']['pending'])
            with self.subTest(change=change):
                self.assertFalse(native_rt_worker_contract(stats,scenario,run_dir=run)['passed'])

    def test_source_contract_malformed_metadata_fails_closed_without_raising(self):
        original, scenario, run = self.source_evidence()
        changes=[lambda s:s['source_bindings'][0].update(worker_id=True),
            lambda s:s['source_bindings'][0].update(frequency_hz=float('nan')),
            lambda s:s['source_bindings'][0]['configured_pose'].update(position_m=[True,0,5]),
            lambda s:s['source_bindings'][0]['last_emitter_batch_stats'].update(attempted_calls=True),
            lambda s:s['source_bindings'][0]['last_emitter_batch_stats'].update(cache_read_misses=True),
            lambda s:s['source_bindings'][0]['last_emitter_batch_stats'].update(receiver_node_ids=[1,1,1]),
            lambda s:s['source_bindings'][0]['cache'].update(pairs=[None]),
            lambda s:s['bundle'].update(pending=[]),
            lambda s:s['worker_roles'][1].update(source_ids=[[]]),
            lambda s:s['pool']['workers'][1]['ready']['source_profile_contexts'][0].update(frequency_hz=[]),
            lambda s:s['bundle']['last_committed'].update(source_requests=[None]),
            lambda s:s['bundle']['last_committed'].update(required_observations=[None])]
        for change in changes:
            stats=copy.deepcopy(original);change(stats['sionna_rt_workers'])
            with self.subTest(change=change):
                self.assertFalse(native_rt_worker_contract(stats,scenario,run_dir=run)['passed'])

    def test_desired_only_empty_source_export_keeps_legacy_contract(self):
        stats,scenario=self.evidence(pool=True,full_mesh=True)
        stats['sionna_rt_workers'].update(source_bindings=[],worker_roles=[],bundle=dict(enabled=False))
        report=native_rt_worker_contract(stats,scenario,run_dir=self.root)
        self.assertTrue(report['passed'],report)
        self.assertIsNone(report['source_bundle'])

    def test_full_mesh_requires_observed_coherent_pose_proof(self):
        stats, scenario = self.evidence(pool=True, full_mesh=True)
        del stats['pose_application']
        self.assertIn('worker_full_mesh_pose_commit', native_rt_worker_contract(stats, scenario)['rejection_reasons'])
        for field in ('pose_commit_policy', 'committed_pose_frames'):
            stats, scenario = self.evidence(pool=True, full_mesh=True)
            del stats['sionna_rt_workers'][field]
            self.assertIn('worker_full_mesh_pose_commit', native_rt_worker_contract(stats, scenario)['rejection_reasons'])

    def test_full_mesh_first_hot_commit_counts_one_and_not_cold_jobs(self):
        stats, scenario = self.evidence(pool=True, full_mesh=True)
        snapshot = stats['sionna_rt_workers']
        pair_count = len(snapshot['cache']['pairs'])
        snapshot.update(committed_pose_frames=1, published_results=1, published_pairs=pair_count)
        snapshot['cache']['accepted'] = pair_count
        stats['pose_application'].update(applied_snapshots=1, observed_snapshots=1)
        self.assertGreater(snapshot['cold_warmup_jobs'], 1)
        self.assertTrue(native_rt_worker_contract(stats, scenario)['passed'])

    def test_full_mesh_pose_commits_match_published_and_applied_frames(self):
        changes = [lambda s: s['sionna_rt_workers'].update(pose_commit_policy='independent'),
                   lambda s: s['sionna_rt_workers'].update(committed_pose_frames=0),
                   lambda s: s['sionna_rt_workers'].update(committed_pose_frames=True),
                   lambda s: s['sionna_rt_workers'].update(committed_pose_frames=29),
                   lambda s: s['pose_application'].update(mode='observed_only'),
                   lambda s: s['pose_application'].update(applied_snapshots=29),
                   lambda s: s['pose_application'].update(applied_snapshots=True),
                   lambda s: s['pose_application'].update(observed_snapshots=29),
                   lambda s: s['pose_application'].update(observed_snapshots=True)]
        for change in changes:
            stats, scenario = self.evidence(pool=True, full_mesh=True)
            change(stats)
            with self.subTest(change=change):
                self.assertIn('worker_full_mesh_pose_commit', native_rt_worker_contract(stats, scenario)['rejection_reasons'])

    def test_full_mesh_pose_age_uses_configured_boundary_without_relaxing_it(self):
        for seconds in (.1, .5):
            stats, scenario = self.evidence(pool=True, full_mesh=True)
            scenario['coupling']['state_max_age_s'] = seconds
            boundary = round(seconds*1e9)
            stats['pose_application'].update(state_max_age_ns=boundary, maximum_applied_pose_age_ns=boundary)
            with self.subTest(seconds=seconds):
                self.assertTrue(native_rt_worker_contract(stats, scenario)['passed'])
                stats['pose_application']['maximum_applied_pose_age_ns'] += 1
                self.assertIn('worker_full_mesh_pose_commit', native_rt_worker_contract(stats, scenario)['rejection_reasons'])
        stats, scenario = self.evidence(pool=True, full_mesh=True)
        scenario['coupling']['state_max_age_s'] = .1  # A stale reported 500 ms limit cannot satisfy 100 ms.
        self.assertIn('worker_full_mesh_pose_commit', native_rt_worker_contract(stats, scenario)['rejection_reasons'])

    def test_full_mesh_pose_proof_rejects_missing_malformed_and_unbounded_ages(self):
        for field in ('mode', 'observed_snapshots', 'applied_snapshots', 'maximum_applied_pose_age_ns', 'state_max_age_ns'):
            stats, scenario = self.evidence(pool=True, full_mesh=True)
            del stats['pose_application'][field]
            with self.subTest(missing=field):
                self.assertIn('worker_full_mesh_pose_commit', native_rt_worker_contract(stats, scenario)['rejection_reasons'])
        for field, value in [('maximum_applied_pose_age_ns', -1), ('maximum_applied_pose_age_ns', True),
                             ('state_max_age_ns', 500_000_000.), ('state_max_age_ns', 500_000_001)]:
            stats, scenario = self.evidence(pool=True, full_mesh=True)
            stats['pose_application'][field] = value
            self.assertIn('worker_full_mesh_pose_commit', native_rt_worker_contract(stats, scenario)['rejection_reasons'])
        for value in (None, True, 0, -.1, .50001, float('nan'), float('inf'), '0.1'):
            stats, scenario = self.evidence(pool=True, full_mesh=True)
            scenario['coupling']['state_max_age_s'] = value
            with self.subTest(configured=value):
                self.assertIn('worker_full_mesh_pose_commit', native_rt_worker_contract(stats, scenario)['rejection_reasons'])

    def test_receiver_batch_legacy_proof_does_not_require_full_mesh_pose_fields(self):
        stats, scenario = self.evidence(pool=True)
        self.assertNotIn('pose_application', stats)
        self.assertNotIn('pose_commit_policy', stats['sionna_rt_workers'])
        self.assertTrue(native_rt_worker_contract(stats, scenario)['passed'])

    def test_full_mesh_actual_producer_stats_support_one_to_five_with_monostatic_paths(self):
        for count in range(1, 6):
            stats, scenario = self.evidence(count, pool=True, full_mesh=True)
            report = native_rt_worker_contract(stats, scenario)
            with self.subTest(count=count):
                self.assertTrue(report['passed'], report['rejection_reasons'])
                self.assertEqual(report['expected_desired_mode'], 'full_mesh')
                self.assertEqual(report['full_mesh_sampling_equivalence'], 'not_evaluated')
                self.assertEqual(native_product_runtime_contract(stats, scenario)['passed'], count == 5)

    def test_full_mesh_cannot_be_inferred_from_frozen_field_or_zero_counts(self):
        for field in ('desired_mode', 'full_mesh_completed_jobs', 'last_full_mesh_stats'):
            stats, scenario = self.evidence(pool=True, full_mesh=True)
            del stats['sionna_rt_workers'][field]
            with self.subTest(field=field):
                self.assertFalse(native_rt_worker_contract(stats, scenario)['passed'])
        for field in ('full_mesh_completed_jobs', 'last_full_mesh_stats'):
            stats, scenario = self.evidence(pool=True, full_mesh=True)
            del stats['sionna_rt_workers']['pool']['workers'][0][field]
            with self.subTest(producer_field=field):
                self.assertIn('worker_full_mesh_producers', native_rt_worker_contract(stats, scenario)['rejection_reasons'])

    def test_full_mesh_requires_consistent_actual_and_frozen_modes(self):
        changes = [lambda s: s.pop('sionna_receiver_batch_mode'),
                   lambda s: s.update(sionna_receiver_batch_mode='receiver_batch'),
                   lambda s: s['sionna_rt_workers'].update(desired_mode='receiver_batch'),
                   lambda s: s['sionna_rt_workers']['pool']['workers'][0]['ready']['applied_config'].pop('desired_mode')]
        for change in changes:
            stats, scenario = self.evidence(pool=True, full_mesh=True)
            change(stats)
            self.assertFalse(native_rt_worker_contract(stats, scenario)['passed'])
        stats, scenario = self.evidence(pool=True, full_mesh=True)
        scenario['sionna']['execution_mode'] = 'in_process'
        stats.update(sionna_rt_execution_mode='in_process', sionna_in_process=True)
        self.assertFalse(native_product_runtime_contract(stats, scenario)['passed'])

    def test_full_mesh_counts_are_actual_per_producer_and_match_aggregate(self):
        changes = [lambda s: s.update(full_mesh_completed_jobs=35),
                   lambda s: s.update(full_mesh_completed_jobs=True),
                   lambda s: s['pool']['workers'][0].update(full_mesh_completed_jobs=0),
                   lambda s: s['pool']['workers'][0].update(full_mesh_completed_jobs=13)]
        for change in changes:
            stats, scenario = self.evidence(pool=True, full_mesh=True)
            change(stats['sionna_rt_workers'])
            self.assertFalse(native_rt_worker_contract(stats, scenario)['passed'])

    def test_full_mesh_rejects_malformed_roster_capacity_epochs_and_retained_stats(self):
        def full_candidate_capacity_with_consistent_total(value):
            value['last_candidate_count'] += value['last_per_tx_candidate_cap'] - value['last_candidate_counts_per_tx'][0]
            value['last_candidate_counts_per_tx'][0] = value['last_per_tx_candidate_cap']
        changes = [lambda x: x.update(source_mode='receiver_batch'), lambda x: x.update(registered_endpoints=5),
                   lambda x: x.update(configured_samples_per_source=1),
                   lambda x: x.update(configured_max_number_of_paths=91),
                   lambda x: x.update(attempted_calls=13), lambda x: x.update(completed_calls=True),
                   lambda x: x.update(generated_links=1), lambda x: x.update(candidate_capacity_reached_calls=1),
                   lambda x: x.update(last_receiver_count=5), lambda x: x.update(last_per_tx_candidate_cap=1),
                   lambda x: x.update(last_candidate_buffer_size=1),
                   full_candidate_capacity_with_consistent_total,
                   lambda x: x['last_self_candidate_counts_per_tx'].__setitem__(0, 21),
                   lambda x: x.update(last_candidate_count=0), lambda x: x.update(last_generated_ns=100_000_001),
                   lambda x: x.update(last_generated_ns=-1), lambda x: x['last_retained_paths_per_pair'].pop(),
                   lambda x: x['last_retained_paths_per_pair'].__setitem__(1, 91),
                   lambda x: x['last_retained_diagonal_paths'].__setitem__(0, 0),
                   lambda x: x.update(solver_ns=-1), lambda x: x.update(max_batch_ns=9999)]
        for change in changes:
            stats, scenario = self.evidence(pool=True, full_mesh=True)
            change(stats['sionna_rt_workers']['pool']['workers'][0]['last_full_mesh_stats'])
            with self.subTest(change=change):
                self.assertIn('worker_full_mesh_producers', native_rt_worker_contract(stats, scenario)['rejection_reasons'])
        stats, scenario = self.evidence(pool=True, full_mesh=True)
        for worker in stats['sionna_rt_workers']['pool']['workers']:
            worker['last_full_mesh_stats']['last_generated_ns'] = 0
        stats['sionna_rt_workers']['cache']['pairs'][0].update(sample_native_ns=1, native_age_ns=99_999_999)
        self.assertIn('worker_full_mesh_epoch_coverage', native_rt_worker_contract(stats, scenario)['rejection_reasons'])

    def test_full_mesh_top_level_stats_must_be_an_actual_producer_response(self):
        stats, scenario = self.evidence(pool=True, full_mesh=True)
        stats['sionna_rt_workers']['last_full_mesh_stats']['last_generated_ns'] = 50_000_000
        self.assertIn('worker_full_mesh_snapshot_stats', native_rt_worker_contract(stats, scenario)['rejection_reasons'])

    def test_full_mesh_original_cache_age_and_canonical_pair_guards_remain(self):
        stats, scenario = self.evidence(pool=True, full_mesh=True)
        stats['sionna_rt_workers']['cache']['maximum_hit_host_age_ns'] = 500_000_001
        self.assertIn('worker_cache_hit_ages_within_budget', native_rt_worker_contract(stats, scenario)['rejection_reasons'])
        stats, scenario = self.evidence(pool=True, full_mesh=True)
        stats['sionna_rt_workers']['cache']['pairs'][0].update(first=0, second=0)
        self.assertIn('worker_cache_pair_provenance', native_rt_worker_contract(stats, scenario)['rejection_reasons'])
        stats, scenario = self.evidence(1, pool=True, full_mesh=True)
        snapshot = stats['sionna_rt_workers']
        snapshot['cache_snapshot_native_ns'] = 2_000_000_000
        snapshot['cache']['pairs'][0].update(valid=False, due=True, reason='native_sample_expired',
            native_age_ns=2_000_000_000, host_age_ns=1_000_000_000,
            sample_host_monotonic_ns=snapshot['cache_snapshot_host_monotonic_ns']-1_000_000_000)
        self.assertTrue(native_rt_worker_contract(stats, scenario)['passed'])

    def test_in_process_and_legacy_flags_still_require_consistent_actual_mode(self):
        stats, scenario = self.evidence()
        self.assertTrue(native_product_runtime_contract(stats, scenario)['passed'])
        del stats['sionna_rt_execution_mode']
        del scenario['sionna']['execution_mode']
        result = native_product_runtime_contract(stats, scenario)
        self.assertTrue(result['passed'])
        self.assertEqual(result['sionna_rt_execution_mode'], 'in_process')
        stats['sionna_in_process'] = False
        self.assertFalse(native_product_runtime_contract(stats, scenario)['passed'])
        stats['sionna_in_process'] = True
        stats['sionna_rt_execution_mode'] = 'worker_pool'
        self.assertFalse(native_product_runtime_contract(stats, scenario)['passed'])

    def test_observed_pool_proof_supports_one_to_five_uavs_without_loosening_product_roster(self):
        for count in range(1, 6):
            stats, scenario = self.evidence(count, pool=True)
            report = native_rt_worker_contract(stats, scenario)
            with self.subTest(count=count):
                self.assertTrue(report['passed'], report['rejection_reasons'])
                self.assertEqual(report['expected_cohort'], ['cp'] + [f'uav{i}' for i in range(1, count + 1)])
                self.assertEqual(native_product_runtime_contract(stats, scenario)['passed'], count == 5)

    def test_mode_and_location_cannot_be_inferred_as_pool_from_an_absent_flag(self):
        for field, value in [('sionna_rt_execution_mode', None), ('sionna_rt_execution_mode', 'in_process'),
                             ('sionna_in_process', True)]:
            stats, scenario = self.evidence(pool=True)
            if value is None: del stats[field]
            else: stats[field] = value
            with self.subTest(field=field, value=value):
                self.assertIn('worker_execution_mode', native_rt_worker_contract(stats, scenario)['rejection_reasons'])
                self.assertFalse(native_product_runtime_contract(stats, scenario)['passed'])

    def test_missing_snapshots_and_ready_only_workers_do_not_prove_native_rt(self):
        stats, scenario = self.evidence(pool=True)
        del stats['sionna_rt_workers']
        self.assertFalse(native_rt_worker_contract(stats, scenario)['passed'])
        for field in ('published_results', 'published_pairs', 'completed_jobs'):
            stats, scenario = self.evidence(pool=True)
            stats['sionna_rt_workers'][field] = 0
            with self.subTest(field=field):
                self.assertIn('worker_results_published', native_rt_worker_contract(stats, scenario)['rejection_reasons'])

    def test_native_cache_must_be_used_without_misses_and_within_original_sample_age(self):
        for field, value, reason in [('hits', 0, 'worker_cache_drives_native_psd'),
                                    ('hits', True, 'worker_cache_drives_native_psd'),
                                    ('misses', 1, 'worker_cache_drives_native_psd'),
                                    ('accepted', 0, 'worker_cache_drives_native_psd'),
                                    ('maximum_hit_native_age_ns', 500_000_001, 'worker_cache_hit_ages_within_budget'),
                                    ('maximum_hit_host_age_ns', 500_000_001, 'worker_cache_hit_ages_within_budget')]:
            stats, scenario = self.evidence(pool=True)
            stats['sionna_rt_workers']['cache'][field] = value
            with self.subTest(field=field, value=value):
                self.assertIn(reason, native_rt_worker_contract(stats, scenario)['rejection_reasons'])
        stats, scenario = self.evidence(pool=True)
        stats['scalar_base_propagation_model_count'] = 1
        self.assertFalse(native_rt_worker_contract(stats, scenario)['passed'])

    def test_failed_worker_wrong_applied_radio_scene_or_cohort_is_rejected(self):
        changes = [lambda row: row.update(failed='child died'), lambda row: row['ready'].update(versions={}),
                   lambda row: row['ready']['applied_config']['radio'].update(frequency_hz=2437000000),
                   lambda row: row['ready']['applied_config']['cohort'].pop(),
                   lambda row: row['ready']['applied_config']['scene'].update(sha256='0' * 64)]
        for change in changes:
            stats, scenario = self.evidence(pool=True)
            change(stats['sionna_rt_workers']['pool']['workers'][0])
            self.assertIn('worker_profiles_match', native_rt_worker_contract(stats, scenario)['rejection_reasons'])
        stats, scenario = self.evidence(pool=True)
        self.scene.write_text('<scene version="3.0.0"><changed/></scene>')
        self.assertFalse(native_rt_worker_contract(stats, scenario)['passed'])
        self.scene.unlink()
        self.assertFalse(native_rt_worker_contract(stats, scenario)['passed'])

    def test_pair_provenance_rejects_future_unknown_duplicate_or_unobserved_entries(self):
        changes = [lambda rows: rows[0].update(sample_native_ns=200_000_000),
                   lambda rows: rows[0].update(sample_host_monotonic_ns=2_000_000_000_000),
                   lambda rows: rows[0].update(source_request_id=0),
                   lambda rows: rows[0].update(second=99), lambda rows: rows.append(copy.deepcopy(rows[0])),
                   lambda rows: rows.pop()]
        for change in changes:
            stats, scenario = self.evidence(pool=True)
            change(stats['sionna_rt_workers']['cache']['pairs'])
            self.assertIn('worker_cache_pair_provenance', native_rt_worker_contract(stats, scenario)['rejection_reasons'])

    def test_expired_unused_final_pair_and_discarded_results_do_not_invalidate_recorded_hits(self):
        stats, scenario = self.evidence(1, pool=True)
        snapshot = stats['sionna_rt_workers']
        snapshot['cache_snapshot_native_ns'] = 2_000_000_000
        snapshot['cache']['pairs'][0].update(valid=False, due=True, reason='native_sample_expired',
            native_age_ns=2_000_000_000, host_age_ns=1_000_000_000,
            sample_host_monotonic_ns=snapshot['cache_snapshot_host_monotonic_ns'] - 1_000_000_000)
        self.assertTrue(native_rt_worker_contract(stats, scenario)['passed'])

    def test_geometry_is_not_rt_and_has_no_worker_proof_requirement(self):
        stats, scenario = self.evidence()
        stats.update(selected_propagation_profile='geometry_logdistance', sionna_in_process=False,
            sionna_drives_rx_psd=False, scalar_base_propagation_model_count=1,
            phased_array_spectrum_propagation_model_count=0, rx_psd_propagation_model='bas::GeometryLogDistance',
            geometry_radio_mode='geometry', geometry_parameter_set='explicit-test-reference',
            geometry_packet_evaluations=10, geometry_observation_rows=3)
        report = native_product_runtime_contract(stats, scenario)
        self.assertTrue(report['passed'])
        self.assertIsNone(report['worker_pool'])
        self.assertIsNone(report['sionna_rt_execution_mode'])


if __name__ == '__main__':
    unittest.main()
