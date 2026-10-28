#!/usr/bin/env python3
"""Read-only final system spot-check; stdlib, no product analyzer imports.

Reads compact records for all attempts and raw events/poses for the first frozen
nominal geometry pair (r03). Does not decode all-series PCAP or parameter tables.
"""
import argparse
import collections
import csv
import hashlib
import json
import math
from pathlib import Path
import subprocess


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--integration', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    root = args.integration
    study = root / 'runs/urban-ns3-20261028'
    freeze = json.loads((study / 'freeze.json').read_text())
    hashes = {}

    def read(path):
        data = path.read_bytes()
        hashes[str(path.relative_to(root))] = hashlib.sha256(data).hexdigest()
        return json.loads(data)

    source_paths = ['network/ns3/scratch/native-geometry-loss.h',
        'network/ns3/scratch/upstream-sionna-tap-spike.cc',
        'scripts/product/prepare_urban_gazebo.py', 'scripts/product/urban_mission.py',
        'scripts/product/relay_experiment.py', 'network/coordination/policy.py',
        'network/coordination/service.py', 'network/config/urban_north_inspection.json']
    correspondence = {}
    for name in source_paths:
        frozen = subprocess.check_output(['git', 'show', freeze['source_commit'] + ':' + name], cwd=root)
        current = (root / name).read_bytes()
        correspondence[name] = dict(identical_to_measured_source=frozen == current,
                                   sha256=hashlib.sha256(current).hexdigest())
    records = []
    for case in freeze['cases']:
        rid = case['run_id']
        raw = root / 'runs/native-radio-realtime' / rid
        controller = read(study / 'controllers' / (rid + '.json'))
        summary = read(raw / 'metrics/relay_experiment_summary.json')
        config_bytes = (raw / 'logs/urban_scenario.json').read_bytes()
        statfile = raw / 'metrics/native_radio_stats.json'
        stat = read(statfile) if statfile.exists() else None
        params = summary.get('parameter_snapshots', {})
        expected = dict(selected_propagation_profile='geometry_logdistance',
            scalar_base_propagation_model_count=1,
            phased_array_spectrum_propagation_model_count=0,
            sionna_in_process=False, sionna_solver_enabled=False,
            custom_packet_error_model=False, native_ns3_phy=True, native_ns3_mac=True,
            wifi_topology='adhoc', fault_profile='none',
            geometry_radio_mode=case['radio_mode'], rng_seed=case['rng_seed'], rng_run=case['rng_run'])
        records.append(dict(order=case['order'], run_id=rid,
            controller_rc=controller['returncode'], status=summary['status'],
            observed_duration_source_s=summary.get('measurement_duration_source_s'),
            full_window=summary.get('measurement_duration_source_s', 0) >= case['duration_s'],
            error=summary.get('error'), source_matches=controller['source_commit'] == freeze['source_commit'],
            config_snapshot_matches=hashlib.sha256(config_bytes).hexdigest() == case['config_sha256'],
            native_stats_present=stat is not None,
            native_field_discrepancies={} if stat is None else {
                k: dict(expected=v, observed=stat.get(k)) for k,v in expected.items() if stat.get(k) != v},
            before_parameter_rc=params.get('before', {}).get('returncode'),
            after_parameter_rc=params.get('after', {}).get('returncode'),
            after_parameter_summary_present=(raw / 'configuration/parameters-after/summary.json').exists()))

    case = next(c for c in freeze['cases'] if c['order'] == 4)
    rid = case['run_id']; raw = root / 'runs/native-radio-realtime' / rid
    summary = json.loads((raw / 'metrics/relay_experiment_summary.json').read_text())
    start = summary['measurement_start_host_monotonic_ns']
    stop = start + summary['measurement_duration_wall_s'] * 1e9
    trigger = None
    with (raw / 'logs/geometry_links.csv').open() as stream:
        last = None
        for row in csv.DictReader(stream):
            if (row['from_id'], row['to_id']) != ('0', '1'): continue
            now = int(row['wall_monotonic_ns'])
            if start <= now <= stop and last == '1' and row['geom_los'] == '0':
                trigger = row; break
            last = row['geom_los']
    nlos = int(trigger['wall_monotonic_ns'])
    gcs = []
    with (raw / 'logs/coordination_gcs.jsonl').open() as stream:
        for lineno, line in enumerate(stream, 1):
            x = json.loads(line)
            if x['event'] in ('grant_issued', 'gcs_send', 'goal_created', 'action_completed'):
                x['raw_line'] = lineno; gcs.append(x)
    goal = next(x for x in gcs if x['event'] == 'goal_created' and x.get('goal_id') == 'waypoint4:uav1')
    completion = next(x for x in gcs if x['event'] == 'action_completed' and x.get('goal_id') == goal['goal_id'])
    grants = {x['grant_sequence']: x for x in gcs if x['event'] == 'grant_issued'}
    modes = {}; admissions = []
    for i in range(1, 6):
        mode_events = []
        with (raw / f'logs/coordination_uav{i}.jsonl').open() as stream:
            for lineno, line in enumerate(stream, 1):
                x = json.loads(line); t = x['host_monotonic_ns']
                if nlos <= t <= nlos + 8e9 and x['event'] in (
                    'hold_requested', 'hold_mode_observed', 'leader_activated', 'autopilot_mode_observed'):
                    mode_events.append(dict(raw_line=lineno, event=x['event'], host_ns=t,
                        since_NLOS_s=(t-nlos)/1e9, mode=x.get('mode'), epoch=x.get('epoch'), leader=x.get('leader_id')))
                if i == 1 and x['event'] == 'uart_admitted' and x.get('target_position_m') == goal['target_position_m'] and t >= goal['goal_created_host_monotonic_ns']:
                    x['raw_line'] = lineno; admissions.append(x)
        modes[str(i)] = mode_events
    first = min(admissions, key=lambda x: x['uart_write_end_host_monotonic_ns'])
    sent = next(x for x in gcs if x['event'] == 'gcs_send' and x['command_id'] == first['command_id'])
    grant = grants[first['grant_sequence']]
    brake = next(x['host_ns'] for x in modes['1'] if x['mode'] == 'BRAKE')
    guided = next(x['host_ns'] for x in modes['1'] if x['mode'] == 'GUIDED' and x['host_ns'] > brake)
    nodes = collections.defaultdict(list)
    previous = {}
    with (raw / 'logs/node_state.jsonl').open() as stream:
        for lineno, line in enumerate(stream, 1):
            frame = json.loads(line)
            host = frame['published_monotonic_ns']
            if not start <= host <= stop: continue
            for x in frame['nodes']:
                if not x['id'].startswith('uav') or x.get('stale'): continue
                source = x.get('source_sim_time_s')
                if source is None or source == previous.get(x['id']): continue
                previous[x['id']] = source
                nodes[x['id']].append(dict(raw_line=lineno, source_s=source, publication_ns=host,
                    sample_ns=x['sample_monotonic_ns'], position=x['position_m'],
                    speed=math.sqrt(sum(v*v for v in x['velocity_enu_mps']))))
    braking = [x for x in nodes['uav1'] if brake <= x['publication_ns'] <= guided]
    target = goal['target_position_m']; dwell = []
    for x in nodes['uav1']:
        if x['source_s'] > completion['source_sim_time_s'] + 1e-9: break
        xy = math.dist(x['position'][:2], target[:2]); dz = abs(x['position'][2]-target[2])
        if x['sample_ns'] < first['uart_write_end_host_monotonic_ns'] or xy>3 or dz>1.5 or x['speed']>1:
            dwell=[]; continue
        if dwell and x['source_s']-dwell[-1]['source_s']>.5: dwell=[]
        dwell.append(x)
    chain = dict(goal=goal, first_gcs_command=sent, first_UART=first, issued_grant=grant,
        completion=completion,
        chronology_ok=nlos < goal['goal_created_host_monotonic_ns'] <= sent['command_created_host_monotonic_ns'] <= first['uart_write_end_host_monotonic_ns'] <= completion['host_monotonic_ns'],
        payload_hash_matches=sent['payload_sha256']==first['payload_sha256'],
        authority_covers_write=grant['valid_from_ns']<=first['uart_write_started_host_monotonic_ns']<=first['uart_write_end_host_monotonic_ns']<=first['admission_checked_host_monotonic_ns']<grant['valid_until_ns'],
        first_command_age_at_final_check_s=(first['admission_checked_host_monotonic_ns']-sent['command_created_host_monotonic_ns'])/1e9,
        independently_replayed_dwell_source_s=dwell[-1]['source_s']-dwell[0]['source_s'],
        dwell_samples=len(dwell), dwell_max_gap_s=max(b['source_s']-a['source_s'] for a,b in zip(dwell,dwell[1:])),
        dwell_raw_first_line=dwell[0]['raw_line'], dwell_raw_last_line=dwell[-1]['raw_line'])
    fixed_id = next(c['run_id'] for c in freeze['cases'] if c['order'] == 3)
    fixed_path = root/'runs/native-radio-realtime'/fixed_id/'logs/coordination_uav5.jsonl'
    rejected_command = None; rejection = None; admitted = set(); previous_event = None
    with fixed_path.open() as stream:
        for lineno, line in enumerate(stream, 1):
            row = json.loads(line); row['raw_line'] = lineno
            if row['event'] == 'uart_admitted': admitted.add(row['command_id'])
            if (row['event'] == 'packet_rejected' and row.get('reason') == 'no active received grant'
                    and previous_event and previous_event['event'] == 'follower_receive'):
                rejected_command, rejection = previous_event, row
            previous_event = row
    negative = dict(run_id=fixed_id, receive=rejected_command, following_rejection=rejection,
        CID_absent_from_UART=rejected_command['command_id'] not in admitted,
        interpretation='Observed command reception without an active received grant, followed by rejection; no UART admission for this CID. No injected stale-command replay performed.')
    interrupted_id = next(c['run_id'] for c in freeze['cases'] if c['order'] == 29)
    interrupted = root/'runs/native-radio-realtime'/interrupted_id
    ns3log = (interrupted/'logs/ns3_sionna.log').read_text()
    with (interrupted/'logs/geometry_links.csv').open() as stream:
        any_nlos = any(row['geom_los']=='0' for row in csv.DictReader(stream))
    interrupted_evidence = dict(run_id=interrupted_id,
        native_fatal_signature_lines=[line for line in ns3log.splitlines() if 'jitter' in line or 'NS_FATAL' in line],
        any_recorded_geometric_NLOS=any_nlos,
        native_final_stats_present=(interrupted/'metrics/native_radio_stats.json').exists(),
        interpretation='No NLOS observed before interruption; future trigger applicability unknown. Jitter is an observed failure signature, not an attributed root cause.')
    reused={}
    for name in ('native_probe_independent_review.json','staged_native_probe_review.json',
                 'axes_pilot_independent_review.json','pilot_pair/native_epoch2.json'):
        x=read(root/'reports/ispras/urban_ns3'/name)
        reused[name]=dict(sha256=hashes['reports/ispras/urban_ns3/'+name],
                         scope=x.get('scope',x.get('limits')))
    result=dict(scope='Independent source and compact all-attempt audit plus one raw frozen-run spot-check; no all-series PHY/parameter replay',
        measured_source=freeze['source_commit'], reviewed_source=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip(),
        source_correspondence=correspondence, attempts=records,
        counts=dict(attempts=len(records), statuses=dict(collections.Counter(x['status'] for x in records)),
                    full_windows=sum(x['full_window'] for x in records),
                    final_native_stats=sum(x['native_stats_present'] for x in records),
                    before_parameter_capture_rc0=sum(x['before_parameter_rc']==0 for x in records),
                    after_parameter_capture_rc0=sum(x['after_parameter_rc']==0 for x in records),
                    after_parameter_summaries_present=sum(x['after_parameter_summary_present'] for x in records)),
        sample=dict(run_id=rid, trigger=trigger, modes=modes, new_goal_lineage=chain,
            uav1_BRAKE_interval=dict(start_host_ns=brake,end_host_ns=guided,
                min_speed_mps=min(x['speed'] for x in braking),max_speed_mps=max(x['speed'] for x in braking),
                first=braking[0],last=braking[-1],interpretation='Observed BRAKE mode and deceleration; no complete physical stop'),
            horizontal_ranges={k:dict(x_range_m=max(x['position'][0] for x in v)-min(x['position'][0] for x in v),
                y_range_m=max(x['position'][1] for x in v)-min(x['position'][1] for x in v)) for k,v in nodes.items()}),
        negative_sample=negative, interrupted_attempt=interrupted_evidence,
        reused_prior_reviews=reused,input_sha256=hashes,
        script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        manuscript_render_review='pending; recorded separately after actual inspection')
    args.output.write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n')
    print(json.dumps(dict(counts=result['counts'], sample_run=rid,
        chronology=chain['chronology_ok'], payload=chain['payload_hash_matches'],
        authority=chain['authority_covers_write'], dwell_s=chain['independently_replayed_dwell_source_s'],
        source_matches=all(x['identical_to_measured_source'] for x in correspondence.values())),indent=2))


if __name__ == '__main__': main()
