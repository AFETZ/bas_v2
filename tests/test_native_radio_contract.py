"""Observed native execution evidence; these fixtures are not RF acceptance runs."""
import copy
import hashlib
from pathlib import Path
import tempfile
import unittest

import yaml

from scripts.product.native_rt_workers import VERSIONS
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

    def evidence(self, count=5, pool=False):
        scenario = copy.deepcopy(self.profile)
        scenario.update(path=str(self.root / 'scenario.yaml'), map={'scene_xml': str(self.scene)},
                        ground_positions={f'uav{i}': [i, 0, 1] for i in range(1, count + 1)})
        radio, rt = scenario['radio'], scenario['sionna']
        rt.update(execution_mode='worker_pool' if pool else 'in_process', worker_count=3)
        if pool:
            rt.update(receiver_batch_mode='receiver_batch', cache_expiry_jitter_fraction=0)
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
        return stats, scenario

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
