#!/usr/bin/env python3
"""Flatten saved study endpoints/coverage with input hashes, without rerunning BAS.

This adds no statistical endpoint, fit, threshold, censoring rule or imputation.
Primary effects and intervals remain the frozen analyzer's results. Missing cells
stay empty; five UAV rows are not independent experiment repetitions.
"""
import argparse
import csv
import hashlib
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--study', type=Path, required=True)
    parser.add_argument('--analysis-root', type=Path, required=True)
    parser.add_argument('--runs-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    inputs, missing = {}, []

    def read(path):
        path = path.resolve()
        if not path.is_file():
            missing.append(str(path))
            return {}
        data = path.read_bytes()
        inputs[str(path)] = dict(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
        return json.loads(data)

    def write(name, rows, empty_fields=None):
        fields = list(dict.fromkeys(k for row in rows for k in row)) or empty_fields or ['run_id']
        with (args.output / name).open('w', newline='') as stream:
            writer = csv.DictWriter(stream, fields, lineterminator='\n')
            writer.writeheader()
            writer.writerows({k: json.dumps(v, sort_keys=True, allow_nan=False) if isinstance(v, (dict, list)) else v
                             for k, v in row.items()} for row in rows)

    study = read(args.study)
    records = study['runs']
    if len(records) != study['planned_attempts'] or any(r.get('controller_returncode') is None for r in records):
        raise ValueError('Every planned controller must have finished; attempted is not finished')
    runs, endpoints, parameters, changes = [], [], [], []
    for record in records:
        base = {key: record.get(key) for key in ('order', 'run_id', 'cohort', 'scenario_variant', 'parameter_variant',
                'radio_mode', 'policy', 'rng_seed', 'rng_run', 'recorded_status', 'attempt_failed', 'primary_eligible')}
        result = read(args.analysis_root / record['run_id'] / 'metrics/urban_analysis.json')
        if result and result.get('run_id') != record['run_id']:
            raise ValueError('Study/export identity differs')
        analysis_path = (args.analysis_root / record['run_id'] / 'metrics/urban_analysis.json').resolve()
        if result and inputs[str(analysis_path)]['sha256'] != record.get('input_sha256', {}).get('analysis'):
            raise ValueError('Per-run analysis bytes differ from the aggregate input; reaggregate explicitly')
        progress = result.get('mission_progress', {})
        perf = result.get('performance', {})
        rates = result.get('rates', {}).get('all_five', {})
        authority = result.get('authority', {})
        row = dict(base, **rates, **result.get('target_stage_counts', {}),
            physical_waypoints=sum(r['completed_waypoints'] for r in progress.values()) if progress else None,
            planned_waypoints=sum(r['planned_waypoints'] for r in progress.values()) if progress else None,
            total_UART_admissions=authority.get('total_UART_admissions'), admission_audit=authority.get('admission_audit'),
            overlapping_issued_grants=authority.get('overlapping_issued_grants'),
            unverifiable_admissions=authority.get('unverifiable_admissions'),
            selections=authority.get('selections'), integral_RTF=perf.get('integral_RTF'),
            RTF_approximately_1s_p5=perf.get('approximately_1s_RTF', {}).get('p5'),
            lag_p95_ms=perf.get('scheduler_lag_ms', {}).get('p95'), lag_max_ms=perf.get('scheduler_lag_ms', {}).get('max'))
        runs.append(row)
        for endpoint_name in ('uart_after_NLOS', 'new_goal_after_NLOS'):
            source = result.get(endpoint_name, {})
            values = ({'all_five': source.get('all_five', {}), **source.get('per_uav', {})}
                      if endpoint_name == 'new_goal_after_NLOS' else source)
            for scope in ('all_five', '1', '2', '3', '4', '5'):
                value = values.get(scope if scope == 'all_five' or endpoint_name == 'new_goal_after_NLOS' else 'uav' + scope, {})
                if (result.get('observation_window', {}).get('complete') is not True
                        and result.get('trigger', {}).get('event') is None
                        and str(value.get('status', '')).startswith('not_applicable')):
                    value = dict(value, original_status=value['status'], status='interrupted_before_observed_trigger',
                                 unknown_applicability=True, trigger_kind='CP_initial_relay_LOS_to_NLOS')
                endpoints.append(dict(base, endpoint=endpoint_name, scope=scope,
                    result=value or None, endpoint_status=value.get('status', 'not_recorded'),
                    original_endpoint_status=value.get('original_status', value.get('status')),
                    time_s=value.get('time_s'), censor_time_s=value.get('censor_time_s'),
                    event_host_ns=value.get('event_host_ns'), leader_id=value.get('leader_id'), epoch=value.get('epoch'),
                    goal_id=value.get('goal_id'), command_ids=value.get('command_ids'),
                    first_admitted_goal_CID=value.get('first_admitted_goal_CID'),
                    completion_refresh_CID=value.get('completion_refresh_CID'),
                    stage_decomposition=value.get('stage_decomposition')))
        direct = record.get('after_direct_probe_detection', {})
        endpoints.append(dict(base, endpoint='after_direct_probe_detection', scope='all_five',
                              result=direct or None, endpoint_status=direct.get('endpoint', {}).get('status', direct.get('status', 'not_recorded')),
                              original_endpoint_status=direct.get('endpoint', {}).get('original_status', direct.get('endpoint', {}).get('status')),
                              time_s=direct.get('endpoint', {}).get('time_s')))
        captured = {}
        for stage in ('before', 'after'):
            summary = read(args.runs_root / record['run_id'] / f'configuration/parameters-{stage}/summary.json')
            if summary and summary.get('run_id') != record['run_id']:
                raise ValueError('Parameter snapshot belongs to a different run')
            started, finished = (summary.get(k + '_host_monotonic_ns') for k in ('started', 'finished'))
            window = result.get('observation_window', {})
            boundary = window.get('start_host_monotonic_ns' if stage == 'before' else 'end_host_monotonic_ns')
            outside = ((finished <= boundary if stage == 'before' else started >= boundary)
                       if all(v is not None for v in (started, finished, boundary)) else None)
            for uid in range(1, 6):
                component = next((r for r in summary.get('components', []) if r.get('system_id') == uid and r.get('component_id') == 1), {})
                values = component.get('parameters', [])
                captured[stage, uid] = component
                expected = component.get('expected_count')
                indices = {r.get('param_index') for r in values if isinstance(r.get('param_index'), int) and r['param_index'] >= 0}
                index_replay = indices == set(range(expected)) if isinstance(expected, int) and expected > 0 else None
                named = {r['param_id']: r.get('value_json') for r in values}
                parameters.append(dict(base, stage=stage, uav_id=uid,
                    snapshot_started_host_ns=started, snapshot_finished_host_ns=finished,
                    outside_measurement_window=outside,
                    status=component.get('status', summary.get('status', 'not_recorded')),
                    reported_complete=component.get('complete'), index_coverage_replayed=index_replay,
                    expected_count=expected, received_index_count=component.get('received_index_count'),
                    replayed_index_count=len(indices) if component else None,
                    param_counts_observed=component.get('param_counts_observed'),
                    missing_indices=component.get('missing_indices'), conflicts=component.get('conflicts'),
                    decoding_issues=component.get('decoding_issues'),
                    WP_SPD=named.get('WP_SPD'), ARMING_SKIPCHK=named.get('ARMING_SKIPCHK'), ARMING_CHECK=named.get('ARMING_CHECK')))
        for uid in range(1, 6):
            before, after = (captured[stage, uid] for stage in ('before', 'after'))
            b, a = ({r['param_id']: r for r in value.get('parameters', [])} for value in (before, after))
            for name in sorted(b.keys() & a.keys()):
                keys = ('param_type', 'wire_value_hex', 'value_json')
                if any(b[name].get(k) != a[name].get(k) for k in keys):
                    changes.append(dict(base, uav_id=uid, param_id=name,
                        before_value_json=b[name].get('value_json'), after_value_json=a[name].get('value_json'),
                        before_wire_hex=b[name].get('wire_value_hex'), after_wire_hex=a[name].get('wire_value_hex'),
                        before_type=b[name].get('param_type'), after_type=a[name].get('param_type'),
                        before_received_host_ns=b[name].get('last_received_monotonic_ns'),
                        after_received_host_ns=a[name].get('last_received_monotonic_ns'),
                        both_readouts_reported_complete=before.get('complete') is True and after.get('complete') is True,
                        scope='matched recorded values; readouts are not atomic; missing values are not zeros'))
    args.output.mkdir(parents=True, exist_ok=True)
    write('descriptive_runs.csv', runs)
    write('endpoints.csv', endpoints)
    write('parameter_coverage.csv', parameters)
    write('parameter_changes.csv', changes, ['run_id', 'uav_id', 'param_id', 'before_value_json', 'after_value_json', 'scope'])
    (args.output / 'table_sources.json').write_text(json.dumps(dict(
        script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), inputs=inputs, missing=missing,
        parameter_changes_recorded=len(changes),
        statistical_unit='independent paired runs; endpoint UAV rows must not be resampled as independent runs',
        semantics='No recalculation of fitted effects/CI. Blank is missing, not zero. Frozen analyzer defines endpoints.'), indent=2) + '\n')
    print(json.dumps(dict(runs=len(runs), endpoint_rows=len(endpoints), parameter_rows=len(parameters), missing_files=len(missing))))


if __name__ == '__main__':
    main()
