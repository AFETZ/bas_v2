#!/usr/bin/env python3
"""Independent stdlib numerical review; imports no BAS analyzer, starts no runtime.

Reads the frozen original study and selected raw logs; writes only --output.
"""
import argparse
import collections
import csv
import hashlib
import json
import math
from pathlib import Path
import random
import statistics
import subprocess

NS = 10**9
METRICS = ('timely_per_offered', 'completed_waypoint_fraction', 'all_routes_finished',
           'new_relay_selections', 'integral_RTF', 'restricted_post_NLOS_UART_60s')
BOUNDED = METRICS[:3]


def read(path):
    return json.loads(path.read_text())


def rows(path):
    with path.open() as stream:
        return list(csv.DictReader(stream))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def avg(values):
    return statistics.fmean(values) if values else None


def interval(values):
    # Match the declared ordered resample scheme, independently from raw cells.
    rng = random.Random(20261028)
    n = len(values)
    draws = sorted(sum(values[int(rng.random()*n)] for _ in range(n))/n
                   for _ in range(50000))
    def q(p):
        x = p*(len(draws)-1)
        lo = int(x)
        return draws[lo] + (draws[math.ceil(x)]-draws[lo])*(x-lo)
    return [q(.025), q(.975)]


def close(a, b):
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(close(x, y) for x, y in zip(a, b))
    if a is None or b is None:
        return a is b
    return math.isclose(a, b, rel_tol=1e-10, abs_tol=1e-11)


def review_numbers(root):
    study = root/'runs/urban-ns3-20261028'
    frozen = read(study/'freeze.json')
    result = read(study/'results/urban_study.json')
    checks, differences, analyses, outcomes = [], [], {}, {}
    def check(label, ok, detail=None):
        checks.append(dict(check=label, passed=bool(ok), detail=detail))
        if not ok:
            differences.append(checks[-1])
    check('one result for every frozen case', [r['run_id'] for r in result['runs']] == [r['run_id'] for r in frozen['cases']])
    statuses, counts, groups = collections.Counter(), collections.Counter(), collections.defaultdict(list)
    error_counts = collections.Counter()
    opportunity_counts = collections.Counter()
    for case, record in zip(frozen['cases'], result['runs']):
        rid = case['run_id']
        export = study/'analysis'/rid
        analysis = read(export/'metrics/urban_analysis.json')
        controller = read(study/'controllers'/(rid+'.json'))
        raw_summary = read(root/'runs/native-radio-realtime'/rid/'metrics/relay_experiment_summary.json')
        analyses[rid] = analysis
        statuses[raw_summary['status']] += 1
        counts[case['cohort']] += 1
        error_counts[str(raw_summary.get('error'))] += 1
        check(rid+': controller/source/status', controller['source_commit'] == frozen['source_commit']
              and controller['config_sha256'] == case['config_sha256']
              and raw_summary['status'] == record['recorded_status'])
        check(rid+': source file identities', sha(export/'metrics/urban_analysis.json') == record['input_sha256']['analysis']
              and sha(study/'controllers'/(rid+'.json')) == record['input_sha256']['controller'])
        rates = analysis['rates']['all_five']
        opportunities = rows(export/'metrics/urban_opportunities.csv')
        physical = rows(export/'metrics/urban_physical_checks.csv')
        config = controller['config_snapshot']
        offered = {r['request_id']:r for r in opportunities}
        opportunity_counts['all_full_and_partial_offered']+=len(offered)
        opportunity_counts['timely_CID_but_scheduled_to_UART_over_250ms']+=sum(
            r['timely']=='True' and bool(r['opportunity_to_UART_ms']) and float(r['opportunity_to_UART_ms'])>250
            for r in offered.values())
        verified = {(r['uav_id'], r['waypoint_index']) for r in physical
                    if r['status']=='verified' and r['command_lineage_valid']=='True'
                    and r['configured_waypoint_valid']=='True'}
        planned = sum(len(x) for x in config['routes'].values())
        check(rid+': offer/physical denominators', len(offered) == rates['offered']
              and sum(r['timely']=='True' for r in offered.values()) == rates['timely']
              and len(verified) == sum(p['completed_waypoints'] for p in analysis['mission_progress'].values()))
        full = raw_summary.get('measurement_duration_source_s') is not None
        check(rid+': full source window', full == record['full_measurement_window'])
        out = {m:None for m in METRICS}
        if full:
            out.update(timely_per_offered=sum(r['timely']=='True' for r in offered.values())/len(offered),
                       completed_waypoint_fraction=len(verified)/planned,
                       all_routes_finished=float(all(sum(k[0]==u for k in verified)==len(route)
                                                     for u,route in config['routes'].items())),
                       new_relay_selections=float(analysis['authority']['selections']),
                       integral_RTF=analysis['performance']['integral_RTF'])
        trigger = analysis['trigger'].get('event')
        window = analysis['observation_window']
        end = window.get('end_host_monotonic_ns')
        if trigger and end and (end-trigger['host_monotonic_ns'])/NS >= 60 and full:
            t = analysis['uart_after_NLOS']['all_five'].get('time_s')
            out['restricted_post_NLOS_UART_60s'] = min(t, 60) if t is not None else 60.
        for m in METRICS:
            check(rid+': '+m, close(out[m], record['outcomes'][m]))
        outcomes[rid] = out
        groups[(case['cohort'],case['scenario_variant'],case['parameter_variant'],case['radio_mode'],case['policy'])].append(rid)
    estimates = []
    for contrast in result['paired_contrasts']:
        group = tuple(contrast[k] for k in ('cohort','scenario_variant','parameter_variant','radio_mode'))
        cells = collections.defaultdict(dict)
        for case in frozen['cases']:
            if tuple(case[k] for k in ('cohort','scenario_variant','parameter_variant','radio_mode')) == group:
                cells[case['block_id']][case['policy']] = outcomes[case['run_id']][contrast['metric']]
        ds = [r['failover']-r['fixed'] for r in cells.values() if all(v is not None for v in r.values())]
        ci = interval(ds) if group[0]=='main' else None
        original = contrast['failover_minus_fixed']
        check('/'.join(group)+': '+contrast['metric']+' paired estimate', close(ds, original['differences'])
              and close(avg(ds), original['mean']) and close(ci, original['interval_95'])
              and len(ds)==contrast['complete_pairs'])
        bounds = None
        if contrast['metric'] in BOUNDED:
            bounds = [avg([(r['failover'] if r['failover'] is not None else f)
                           -(r['fixed'] if r['fixed'] is not None else x) for r in cells.values()])
                      for f,x in [(0,1),(1,0)]]
            check('/'.join(group)+': '+contrast['metric']+' bounds',
                  close(bounds, contrast['missing_outcome_sensitivity']['mean_difference_bounds']))
        estimates.append(dict(group=list(group), metric=contrast['metric'], n=len(ds), differences=ds,
                              mean=avg(ds), interval_95=ci, planned_pair_bounds=bounds))
    interactions = []
    for original in result['policy_geometry_interactions']:
        cells = collections.defaultdict(dict)
        m = original['metric']
        for case in frozen['cases']:
            if case['cohort']=='main':
                cells[case['block_id']][(case['radio_mode'],case['policy'])] = outcomes[case['run_id']][m]
        signs = [(('geometry','failover'),1),(('geometry','fixed'),-1),
                 (('los_only','failover'),-1),(('los_only','fixed'),1)]
        ds = [((r['geometry','failover']-r['geometry','fixed'])
               -(r['los_only','failover']-r['los_only','fixed'])) for r in cells.values()
              if all(v is not None for v in r.values())]
        ci = interval(ds)
        check(m+': interaction estimate', close(ds, original['estimate']['differences'])
              and close(avg(ds), original['estimate']['mean']) and close(ci, original['estimate']['interval_95']))
        bounds = None
        if m in BOUNDED:
            bounds = [avg([sum(s*r[k] if r[k] is not None else (0 if s>0 else -1) if lower
                               else (1 if s>0 else 0) for k,s in signs) for r in cells.values()])
                      for lower in [True,False]]
            check(m+': interaction bounds', close(bounds, original['missing_outcome_sensitivity']['mean_interaction_bounds']))
        interactions.append(dict(metric=m,n=len(ds),mean=avg(ds),interval_95=ci,planned_block_bounds=bounds))
    descriptive=[]
    for group, ids in groups.items():
        descriptive.append(dict(group=list(group),planned=len(ids),observed=sum(outcomes[i]['timely_per_offered'] is not None for i in ids),
            means={m:avg([outcomes[i][m] for i in ids if outcomes[i][m] is not None]) for m in METRICS},
            new_goal_statuses=dict(collections.Counter(analyses[i]['new_goal_after_NLOS']['all_five']['status'] for i in ids)),
            post_NLOS_UART_s=[analyses[i]['uart_after_NLOS']['all_five']['time_s'] for i in ids],
            conditional_new_goal_s=[analyses[i]['new_goal_after_NLOS']['all_five']['time_s'] for i in ids]))
    source=[]
    for name in ['analyze_urban_study.py','analyze_urban_experiment.py','analyze_relay_experiment.py']:
        path='scripts/product/'+name
        old=subprocess.check_output(['git','show',frozen['source_commit']+':'+path],cwd=root)
        frozen_digest=hashlib.sha256(old).hexdigest()
        source.append(dict(path=path,current_checkout_unchanged_since_freeze=old==(root/path).read_bytes(),
                           frozen_sha256=frozen_digest,current_checkout_sha256=sha(root/path),
                           original_output_matches_frozen_analyzer=(result['analyzer_sha256']==frozen_digest)
                           if name=='analyze_urban_study.py' else all(a['provenance'][
                               'script_sha256' if name=='analyze_urban_experiment.py' else 'common_audit_script_sha256']==frozen_digest
                               for a in analyses.values())))
    return dict(source_commit=frozen['source_commit'],freeze_sha256=sha(study/'freeze.json'),
        original_result_sha256=sha(study/'results/urban_study.json'),source_scripts=source,
        attempted=len(result['runs']),status_counts=dict(statuses),cohort_counts=dict(counts),
        full_windows=sum(outcomes[i]['timely_per_offered'] is not None for i in outcomes),
        recorded_error_counts=dict(error_counts),opportunity_clock_check=dict(opportunity_counts),arithmetic_checks=len(checks),
        arithmetic_discrepancies=differences,paired_estimates=estimates,interactions=interactions,
        descriptive=descriptive), analyses


def raw_audit(root, rid, analysis, physical=False):
    raw=root/'runs/native-radio-realtime'/rid
    start=analysis['observation_window']['start_host_monotonic_ns']
    end=analysis['observation_window']['end_host_monotonic_ns']
    relevant={'gcs_send','master_receive','master_forward','follower_receive','uart_admitted',
              'grant_issued','goal_created','action_completed','control_refresh_offered','control_refresh_created',
              'link_failure_detected'}
    events=[]
    for f in sorted((raw/'logs').glob('coordination_*.jsonl')):
        for line in f.open():
            r=json.loads(line)
            if r.get('stage') in relevant:
                events.append(r)
    events.sort(key=lambda r:r['host_monotonic_ns'])
    assert all(r['run_id']==rid and r['clock_domain']=='host_monotonic' for r in events)
    grants={r['grant_sequence']:r for r in events if r['stage']=='grant_issued'}
    by_cid=collections.defaultdict(list)
    for r in events:
        if 'command_id' in r: by_cid[r['command_id']].append(r)
    sends={r['command_id']:r for r in events if r['stage']=='gcs_send' and r.get('target_position_m') is not None
           and start<=r['command_created_host_monotonic_ns']<=end}
    offers={r['request_id']:r for r in events if r['stage']=='control_refresh_offered' and start<=r['host_monotonic_ns']<=end}
    links={r['request_id']:r['command_id'] for r in events if r['stage']=='control_refresh_created'}
    valid={}; problems=[]; stage_counts=collections.Counter(); admissions=[]
    identity=('run_id','uav_id','leader_id','epoch','command_id','command_created_host_monotonic_ns','payload_sha256','target_position_m')
    for cid,s in sends.items():
        stage_counts['target_CIDs_created_in_window']+=1
        i,leader=s['uav_id'],s['leader_id']
        required={'master_receive':leader,'follower_receive':i,'uart_admitted':i}
        if i!=leader: required['master_forward']=leader;stage_counts['master_forward_required_CIDs']+=1
        observed={}
        for name,actor in required.items():
            rs=[r for r in by_cid[cid] if r['stage']==name and r.get('node_id')==actor and start<=r['host_monotonic_ns']<=end]
            same=[r for r in rs if all(r.get(k)==s.get(k) for k in identity)]
            if len(rs)!=len(same): problems.append(cid+': stage identity mismatch '+name)
            if same:
                observed[name]=same[0]
                if name!='uart_admitted': stage_counts[name+'_CIDs']+=1
        a=observed.get('uart_admitted')
        if a:
            stage_counts['UART_write_end_CIDs']+=1
            if len([r for r in by_cid[cid] if r['stage']=='uart_admitted'])!=1: problems.append(cid+': duplicate UART')
            g=grants.get(a['grant_sequence'],{})
            authority=all(g.get(k)==a.get(k) for k in ('leader_id','epoch','valid_from_ns','valid_until_ns'))
            ordered=(a['valid_from_ns']<=a['uart_write_started_host_monotonic_ns']
                     <=a['uart_write_end_host_monotonic_ns']<=a['admission_checked_host_monotonic_ns']<a['valid_until_ns'])
            chain=all(k in observed for k in required)
            if not authority or not ordered or not chain: problems.append(cid+': authority/chronology/chain failed')
            if authority and ordered and chain:
                valid[cid]=a;stage_counts['audited_valid_UART_CIDs']+=1
                admissions.append(a)
    timely=sum(cid in valid and 0<=valid[cid]['uart_write_end_host_monotonic_ns']-sends[cid]['command_created_host_monotonic_ns']<=250_000_000
               for request in offers for cid in [links.get(request)])
    expected=analysis['target_stage_counts']
    mapping={'follower_receive_CIDs':'follower_application_receive_CIDs',
             'master_forward_CIDs':'master_forward_observed_CIDs'}
    for key,value in stage_counts.items():
        if value!=expected[mapping.get(key,key)]:problems.append(key+': export count mismatch')
    conflicts=[]
    gs=list(grants.values())
    for pos,g in enumerate(gs):
        for h in gs[:pos]:
            if (g['leader_id'],g['epoch'])!=(h['leader_id'],h['epoch']) and max(g['valid_from_ns'],h['valid_from_ns'])<min(g['valid_until_ns'],h['valid_until_ns']):
                conflicts.append([h['grant_sequence'],g['grant_sequence']])
    trigger=None;previous=None;trigger_bracket=None
    with (raw/'logs/geometry_links.csv').open() as stream:
        for g in csv.DictReader(stream):
            at=int(g['wall_monotonic_ns'])
            if (g['from_id'],g['to_id'])!=('0','1') or not start<=at<=end:continue
            if previous and previous['geom_los']=='1' and g['geom_los']=='0':
                trigger=at;trigger_bracket=(at-int(previous['wall_monotonic_ns']))/NS;break
            previous=g
    assert trigger==(analysis['trigger'].get('event') or {}).get('host_monotonic_ns')
    detections=[r for r in events if r['stage']=='link_failure_detected' and r['leader_id']==1 and start<=r['host_monotonic_ns']<=end]
    def endpoint(t):
        scopes=collections.defaultdict(dict)
        if t is None:return None
        for a in admissions:
            if a['command_created_host_monotonic_ns']>=t:
                key=(a['leader_id'],a['epoch']);u=a['uav_id'];at=a['uart_write_end_host_monotonic_ns']
                scopes[key][u]=min(at,scopes[key].get(u,at))
        values=[max(x.values()) for x in scopes.values() if set(x)==set(range(1,6))]
        return (min(values)-t)/NS if values else None
    output=dict(run_id=rid,target_stage_counts=dict(stage_counts),offered=len(offers),timely=timely,
                timely_per_offered=timely/len(offers),target_lineage_discrepancies=problems,
                overlapping_different_scope_grants=conflicts,
                raw_geometric_trigger_host_ns=trigger,raw_transition_bracket_host_s=trigger_bracket,
                post_NLOS_UART_s=endpoint(trigger),post_detection_UART_s=endpoint(detections[0]['host_monotonic_ns']) if detections else None)
    assert timely==analysis['rates']['all_five']['timely']
    assert close(output['post_NLOS_UART_s'],analysis['uart_after_NLOS']['all_five']['time_s'])
    if physical:
        selected=analysis['new_goal_after_NLOS']['per_uav']
        completions={r['uav_id']:r for r in events if r['stage']=='action_completed'
                     and r['uav_id'] in range(1,6) and r.get('goal_id')==selected[str(r['uav_id'])].get('goal_id')}
        goals={(r['uav_id'],r['goal_id']):r for r in events if r['stage']=='goal_created'}
        samples=collections.defaultdict(dict)
        for line in (raw/'logs/node_state.jsonl').open():
            r=json.loads(line);host=r['published_monotonic_ns']
            if not start<=host<=end:continue
            for n in r['nodes']:
                uid=n['id']
                if not uid.startswith('uav'):continue
                i=int(uid[3:]);c=completions.get(i)
                if not c:continue
                t=n.get('source_sim_time_s',r.get('source_sim_time_s'))
                if t is None or not c['source_sim_time_s']-2.5<=t<=c['source_sim_time_s']:continue
                good=(not r.get('fault') and not n.get('stale',True) and r['coordinate_frame']=='ENU'
                      and 0<=host-n['sample_monotonic_ns']<=500_000_000
                      and t-r['source_sim_time_s']<=.1)
                samples[i].setdefault((r['session_id'],t),dict(time=t,session=r['session_id'],good=good,
                    sample_ns=n['sample_monotonic_ns'],position=n['position_m'],velocity=n['velocity_enu_mps']))
        config=read(raw/'metrics/relay_experiment_config.json');scenario=read(raw/'logs/urban_scenario.json')
        physical_rows=[]
        for i,c in completions.items():
            target=c['target_position_m'];born=c['goal_created_host_monotonic_ns'];finished=c['host_monotonic_ns']
            g=goals[i,c['goal_id']];last=valid[c['admitted_command_id']]
            matching=[a for cid,a in valid.items() if any(x['stage']=='control_refresh_created' and x.get('goal_id')==c['goal_id']
                      and x.get('goal_created_host_monotonic_ns')==born for x in by_cid[cid]) and a['uav_id']==i
                      and a['epoch']==c['epoch'] and a['leader_id']==c['leader_id'] and a['target_position_m']==target
                      and a['uart_write_end_host_monotonic_ns']<=finished]
            first=min(matching,key=lambda r:r['uart_write_end_host_monotonic_ns'])
            times=[trigger,born,first['command_created_host_monotonic_ns'],first['uart_write_end_host_monotonic_ns'],finished]
            phases=[(b-a)/NS for a,b in zip(times,times[1:])]
            chronology=times==sorted(times) and finished<last['valid_until_ns'] and g['target_position_m']==target
            chronology=chronology and target==scenario['routes'][str(i)][c['waypoint_index']]
            streak=[];previous=None
            for p in samples[i].values():
                gap=p['time']-previous['time'] if previous else 0
                error_xy=math.dist(p['position'][:2],target[:2]);error_z=abs(p['position'][2]-target[2]);speed=math.sqrt(sum(v*v for v in p['velocity']))
                if previous and (p['session']!=previous['session'] or not 0<gap<=.5):streak=[]
                if not p['good'] or p['sample_ns']<times[3] or error_xy>config['xy_tolerance_m'] or error_z>config['z_tolerance_m'] or speed>config['hold_speed_mps']:
                    streak=[]
                else:streak.append(dict(time=p['time'],gap=gap,xy=error_xy,z=error_z,speed=speed))
                previous=p
            span=streak[-1]['time']-streak[0]['time'] if streak else None
            physical_rows.append(dict(uav_id=i,goal_id=c['goal_id'],first_CID=first['command_id'],completion_CID=last['command_id'],
                same_as_export=first['command_id']==selected[str(i)]['first_admitted_goal_CID'],chronology_valid=chronology,
                elapsed_host_s=(finished-trigger)/NS,decomposition_host_s=phases,
                raw_dwell_source_s=span,dwell_verified=span is not None and span>=2 and abs(streak[-1]['time']-c['source_sim_time_s'])<=.05,
                max_sample_gap_source_s=max(p['gap'] for p in streak),samples=len(streak)))
        output['selected_new_goal_raw_checks']=physical_rows
    return output


def interpretation_review(root, analyses):
    crash='urban20261028-main-nominal-nominal-geometry-failover-r01'
    a=analyses[crash]
    window=a['observation_window']
    crash_log=root/'runs/native-radio-realtime'/crash/'logs/ns3_sionna.log'
    original=read(root/'runs/urban-ns3-20261028/results/urban_study.json')
    record=next(r for r in original['runs'] if r['run_id']==crash)
    boundary=[]
    for policy in ['fixed','failover']:
        rid='urban20261028-engineering_boundary-short_occlusion-nominal-geometry-'+policy+'-r01'
        item=analyses[rid];trigger=item['trigger']['event']['host_monotonic_ns'];returned=item['geometry']['LOS_return_host_ns']
        boundary.append(dict(run_id=rid,observed_LOS_return_delay_host_s=(returned-trigger)/NS if returned else None,
            followup_host_s=(item['observation_window']['end_host_monotonic_ns']-trigger)/NS))
    geo=[r for r in original['runs'] if r['cohort']=='main' and r['radio_mode']=='geometry' and r['policy']=='failover' and r['primary_eligible']]
    post=[r['post_NLOS_UART']['time_s'] for r in geo]
    det=[r['after_direct_probe_detection']['endpoint']['time_s'] for r in geo]
    issues=[
        dict(id='R1',severity='interpretation_correction_required',scope='original aggregate and per-run endpoint statuses',
             observed='Interrupted order29 is labelled not_applicable_no_trigger / not_applicable_no_observed_NLOS_transition; direct-probe endpoint similarly N/A.',
             correction='Use interrupted_before_observed_trigger and unknown applicability, retaining null values. Full-window no-trigger N/A remains distinct.',
             implication='Primary fixed-horizon missing bounds are valid. No numerical post-NLOS 60s bounds for all ten without the additional assumption that a trigger would occur; use not_identified_due_unknown_trigger_applicability.',
             evidence=dict(run_id=crash,original_restricted=record['restricted_post_NLOS'],full_window=window['complete'])),
        dict(id='R2',severity='provenance_wording_correction_required',scope='original aggregate provenance_errors',
             observed=record['provenance_errors'],
             correction='Absent final native_stats means effective profile not verified from that artifact, not measured mode mismatch or proof of artificial fault.',
             evidence=dict(run_id=crash,native_stats_present=(root/'runs/native-radio-realtime'/crash/'metrics/native_radio_stats.json').exists())),
        dict(id='R3',severity='reporting_constraint',scope='short_occlusion engineering pair',
             observed='Only failover returned to LOS; fixed remained NLOS to the end.',
             correction='Describe the configured boundary and observed policy-dependent trajectories; do not claim matched exogenous short outages.',evidence=boundary),
        dict(id='R4',severity='reporting_constraint',scope='latency clock origin',
             observed='All nine complete main geometry failover first-post-NLOS UART latencies exceed 4s; all corresponding post-detection latencies are below 4s.',
             correction='Report both origins; do not transplant the historical <=4s target or call geometric NLOS itself established service loss.',
             evidence=dict(n=9,post_NLOS_host_s=dict(mean=avg(post),min=min(post),max=max(post)),
                           after_probe_detection_host_s=dict(mean=avg(det),min=min(det),max=max(det)))),
        dict(id='R5',severity='reporting_constraint',scope='conditional physical new-goal endpoint',
             observed='Main geometry failover has 9 observed all-five first-success new-goal endpoints; fixed has zero and zero newly created post-trigger goals in all ten.',
             correction='T_goal is a selected successful-goal latency, not a paired recovery mean or pure flight/control latency. Report opportunities and censoring; retain one critical UAV for additive decomposition. Configured waypoints include initial/vertical positions, so counts are not horizontal-maneuver counts.'),
        dict(id='R6',severity='reporting_constraint',scope='denominators and failure counts',
             observed='All full windows offer 1200 refresh opportunities. Some have no CID; application receipt, UART, and unique configured goals have distinct counts. 19 failed cleanup attempts retain complete windows.',
             correction='Keep 36 completed /20 failed distinct from55 eligible windows; define timely relative to CID creation at<=250host ms with all opportunities in the denominator; never use selected latency samples as availability.')]
    return dict(discrepancies=issues,crash=dict(run_id=crash,observed_window_host_s=(window['end_host_monotonic_ns']-window['start_host_monotonic_ns'])/NS,
        original_log_text=crash_log.read_text(),cause_status='Fatal realtime jitter observed; cause of host stall not established'),
        scope_limits=['No full-series independent raw pose/authority replay; all 56 export arithmetic and selected r03 raw pair only.',
                      'No native PHY/PCAP, packet loss denominator, parameter completeness or hardware certification.',
                      'No runtime launch/build or retry; original measurements and aggregate retained.'])


def review_overlay(root):
    study=root/'runs/urban-ns3-20261028'
    directory=study/('results-final' if (study/'results-final/urban_study.json').exists() else 'results-reviewed')
    path=directory/'urban_study.json'
    if not path.exists():return dict(status='not_available')
    original=read(study/'results/urban_study.json');reviewed=read(path)
    same=([r['run_id'] for r in original['runs']]==[r['run_id'] for r in reviewed['runs']]
          and len(original['paired_contrasts'])==len(reviewed['paired_contrasts'])
          and len(original['policy_geometry_interactions'])==len(reviewed['policy_geometry_interactions'])
          and all(a['outcomes']==b['outcomes'] for a,b in zip(original['runs'],reviewed['runs']))
          and all(a['failover_minus_fixed']==b['failover_minus_fixed'] for a,b in zip(original['paired_contrasts'],reviewed['paired_contrasts']))
          and all(a['estimate']==b['estimate'] for a,b in zip(original['policy_geometry_interactions'],reviewed['policy_geometry_interactions'])))
    assert same
    crash=next(r for r in reviewed['runs'] if r['order']==29)
    contrast=next(c for c in reviewed['paired_contrasts'] if c['cohort']=='main'
                  and c['radio_mode']=='geometry' and c['metric']=='restricted_post_NLOS_UART_60s')
    table=directory/'tables/endpoints.csv'
    records=[r for r in rows(table) if r['run_id']==crash['run_id']] if table.exists() else []
    statuses=collections.Counter(r['endpoint_status'] for r in records)
    retained=[]
    for row in records:
        body=json.loads(row['result'])
        retained.append(bool(body.get('original_status') or body.get('endpoint',{}).get('original_status')))
    return dict(status='read_and_compared',path=str(path),sha256=sha(path),analysis_version=reviewed['analysis_version'],
                all_outcomes_paired_estimates_interaction_estimates_exactly_unchanged=same,
                restricted_crash_endpoint=crash['restricted_post_NLOS'],
                restricted_pair_unknown_ids=contrast.get('unknown_applicability_pair_ids'),
                restricted_pair_NA_ids=contrast['nonapplicable_pair_ids'],
                restricted_missing_bounds=contrast['missing_outcome_sensitivity'],
                crash_provenance_errors=crash['provenance_errors'],crash_table_endpoint_statuses=dict(statuses),
                crash_table_original_statuses_retained=sum(retained),
                crash_embedded_post_NLOS_status=crash['post_NLOS_UART']['status'],
                crash_embedded_detection_status=crash['after_direct_probe_detection']['endpoint']['status'])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path('/home/bas/bas_v2-paper-live'))
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    result,analyses=review_numbers(args.root)
    result['review_method']='Independent stdlib arithmetic and selected raw replay; no analyzer imports, simulator, PCAP or parameter scan.'
    result['raw_checks']=[raw_audit(args.root,'urban20261028-main-nominal-nominal-geometry-'+p+'-r03',
        analyses['urban20261028-main-nominal-nominal-geometry-'+p+'-r03'],physical=p=='failover') for p in ['fixed','failover']]
    result['interpretation_review']=interpretation_review(args.root,analyses)
    result['reviewed_overlay']=review_overlay(args.root)
    overlay=result['reviewed_overlay']
    if overlay.get('analysis_version')==3:
        statuses=overlay['crash_table_endpoint_statuses']
        fixed=(sum(statuses.values())==13 and overlay['crash_table_original_statuses_retained']==13 and all('interrupted' in k for k in statuses)
               and 'interrupted' in overlay['crash_embedded_post_NLOS_status']
               and 'interrupted' in overlay['crash_embedded_detection_status'])
        result['interpretation_review']['discrepancies'][0]['final_resolution']='remediated_in_final_version3' if fixed else 'incomplete_overlay_correction'
        result['interpretation_review']['discrepancies'][1]['final_resolution']='remediated_in_final_version3' if all('not recorded' in s for s in overlay['crash_provenance_errors']) else 'inspect_remaining_provenance_wording'
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    print(json.dumps(dict(checks=result['arithmetic_checks'],discrepancies=result['arithmetic_discrepancies'],output=str(args.output))))


if __name__=='__main__':
    main()
