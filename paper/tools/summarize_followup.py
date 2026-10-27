#!/usr/bin/env python3
"""Join the independent raw audits with the final export, without rerunning BAS."""
import argparse
from collections import Counter
import csv
import json
from pathlib import Path


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--evidence-root', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--raw-runs-root', type=Path, default=Path('runs/native-radio-realtime'))
    args = ap.parse_args()
    base = args.evidence_root
    independent = json.loads((base/'sensitivity-independent-review/all18-independent.json').read_text())['records']
    wire = json.loads((base/'sensitivity-independent-review/all18-upstream-parameters.json').read_text())['records']
    analyzed = json.loads((base/'sensitivity/analysis/metrics/sensitivity_analysis.json').read_text())
    final = {r['run_id']: r for r in analyzed['records']}
    assert len(independent) == len(wire) == len(final) == 18
    assert analyzed['common_configuration_signatures'] == 1
    states, stages, table_counts = Counter(), Counter(), Counter()
    records = []
    for raw, params in zip(independent, wire):
        rid = raw['run_id']
        assert params['run_id'] == rid
        result = final[rid]
        assert raw['frozen_source_matches'] and raw['image_matches'] and raw['native_rng_matches']
        assert raw['source_dirty_paths'] == '0' and raw['parameter_workers'] == 5
        assert raw['raw_publication_file_sizes_match'] and not raw['raw_publication_errors']
        assert raw['prefault_active_leader_epoch'] == [1, 1]
        assert raw['offered'] == raw['unique_offer_ids'] == 900
        assert not raw['invalid_admitted_commands']
        assert result['measurement_window']['complete'] and result['network_eligible']
        observed = raw['common_UART_all5']['time_s']
        assert abs(observed - result['outcomes']['common_UART_recovery_s']) < 1e-9
        assert abs(raw['physical_goal_all5_s'] - result['outcomes']['physical_new_goal_recovery_s']) < 1e-9
        assert abs(raw['actual_RF_duration_host_s'] - result['fault_phase_and_selections']['actual_RF_duration_s']) < 1e-9
        states[raw['status']] += 1
        record = dict(run_id=rid, status=raw['status'], UART_recovery_s=observed,
            changed_relay=bool(raw['new_selections']), before_after={})
        for stage in ('before', 'after'):
            data = params['stages'].get(stage, {})
            components = data.get('components', [])
            directory = args.raw_runs_root/rid/'configuration'/f'parameters-{stage}'
            exported = json.loads((directory/'summary.json').read_text()) if components else {}
            csv_rows = list(csv.DictReader((directory/'parameters.csv').open())) if components else []
            csv_review = []
            complete = sum(c.get('independent_index_coverage_complete') is True for c in components)
            stages[f'{stage}_complete' if complete == 5 else f'{stage}_partial' if components else f'{stage}_unrecorded'] += 1
            for c in components:
                assert not c.get('bad_data_frames') and not c.get('foreign_frames') and not c.get('index_name_conflicts')
                assert not c.get('last_value_discrepancies')
                assert c.get('tlog_frames_exactly_equal_JSONL_multiset') is True
                assert c.get('raw_exactly_equals_all_tlog_frames_in_order') is True
                # csv.writer encodes None as an empty cell. Partial readouts may
                # legitimately contain a PARAM_VALUE with index=65535, retained
                # as an unindexed value, which is not complete index coverage.
                saved = next(x for x in exported['components'] if x['system_id'] == c['system_id'])
                values = {x['param_id']:x for x in saved['parameters']}
                actual = [x for x in csv_rows if int(x['system_id']) == c['system_id']]
                normalize = lambda x: '' if x is None else str(x)
                keys = ('wire_value_hex','param_type','value_json','param_index','param_count')
                equal = len(actual) == len(values) and all(x['param_id'] in values and
                    all(normalize(values[x['param_id']][k]) == x[k] for k in keys) for x in actual)
                assert equal, (rid, stage, c['system_id'], 'CSV differs beyond null serialization')
                csv_review.append(dict(sysid=c['system_id'], original_string_comparison=c.get('csv_matches_summary_values'),
                    normalized_None_to_empty_CSV_matches=equal, unindexed_values=sum(x['param_index']=='' for x in actual)))
                if c.get('independent_index_coverage_complete'):
                    assert c['param_counts'] == [1388] and c['distinct_valid_indices'] == 1388
                table_counts['complete' if c.get('independent_index_coverage_complete') else 'partial'] += 1
            record['before_after'][stage] = dict(complete_components=complete if components else None,
                component_coverage=[dict(sysid=c['system_id'], count=c.get('param_counts'),
                    observed_indices=c['distinct_valid_indices'], missing_indices=c['missing_indices']) for c in components],
                duration_wall_s=data.get('duration_wall_s'), status=data.get('summary_status', 'not_recorded'))
            record['before_after'][stage]['CSV_null_serialization_review'] = csv_review
        changes = params.get('independent_before_after_changes', [])
        matched = [v for v in changes if v.get('before') is not None and v.get('after') is not None]
        unmatched = len(changes) - len(matched)
        record['matched_value_changes'] = len(matched) if changes else None
        record['missing_value_comparisons'] = unmatched if changes else None
        record['change_interpretation'] = 'Missing before/after values are coverage gaps, not changed parameter values.'
        records.append(record)
    out = dict(runtime_source='e6453c8e2a1ed4f6e4f9b16c224104e62e2df5d1',
        attempted=18, terminal_status=dict(states), parameter_stages=dict(stages), parameter_tables=dict(table_counts),
        expected_tables=180, unrecorded_tables=180-sum(table_counts.values()),
        all_18_full_network_windows=True, independent_UART_physical_RF_endpoints_match=True,
        scope='Independent small raw role/command/UART and upstream MAVLink replay; no new native PCAP or complete trajectory replay.',
        records=records)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(out, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({k:v for k,v in out.items() if k != 'records'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
