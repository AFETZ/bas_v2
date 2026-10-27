#!/usr/bin/env python3
"""One-off read-only recalculation of completed raw cases; no production analyzer import."""
from pathlib import Path
from collections import defaultdict,Counter
import argparse,csv,json,math,re,subprocess
P=argparse.ArgumentParser();P.add_argument('--orders',default='1,2,3');P.add_argument('--output',type=Path,required=True);P.add_argument('--include-failed',action='store_true');args=P.parse_args()
ROOT=Path('/home/bas/bas_v2-paper-live');BASE=ROOT/'runs/paper-followup-20261027/sensitivity';freeze=json.loads((BASE/'freeze.json').read_text())
completed={r['run_id']:r['returncode'] for r in map(json.loads,(BASE/'controller/controller_events.jsonl').open()) if r['event']=='case_exited'}
records=[]
for order in map(int,args.orders.split(',')):
    planned=next(r for r in freeze['rows'] if int(r['order'])==order);run=planned['run_id'];assert completed[run] in ((0,1) if args.include_failed else (0,))
    raw=ROOT/'runs/native-radio-realtime'/run;meta=json.loads((raw/'metrics/relay_experiment_summary.json').read_text());cfg=json.loads((raw/'metrics/relay_experiment_config.json').read_text())
    stats=json.loads((raw/'metrics/native_radio_stats.json').read_text());manifest=json.loads((raw/'configuration/final/manifest.json').read_text());publication=json.loads((raw/'logs/native_capture_storage.json').read_text())['publication']
    env=dict(line.split('=',1) for line in (raw/'environment.txt').read_text().splitlines() if '=' in line)
    lo=meta['measurement_start_host_monotonic_ns'];hi=lo+round(meta['measurement_duration_wall_s']*1e9)
    pattern=r'"event"\s*:\s*"(grant_issued|leader_activated|gcs_send|master_receive|master_forward|follower_receive|uart_admitted|action_completed|phase_requested|gcs_probe_issued|gcs_probe_echo_received|failsafe_hold_observed|autopilot_heartbeat_received|takeoff_observed|land_mode_observed|landing_descent_observed|landing_ground_observed|landing_disarm_observed)"'
    paths=sorted((raw/'logs').glob('coordination_*.jsonl'))
    filtered=subprocess.run(['rg','-N','--no-filename',pattern,*map(str,paths)],capture_output=True,text=True,check=True)
    events=sorted(map(json.loads,filtered.stdout.splitlines()),key=lambda e:e['host_monotonic_ns']);assert all(e['run_id']==run for e in events)
    nativepath=raw/'logs/native_radio_events.csv';header=next(csv.reader(nativepath.open()))
    filtered=subprocess.run(['rg','-N',',fault_(start|end),',str(nativepath)],capture_output=True,text=True,check=True)
    faults=[dict(zip(header,r)) for r in csv.reader(filtered.stdout.splitlines())];faults=[e for e in faults if lo<=int(e['wall_monotonic_ns'])<=hi]
    first=next(e for e in faults if e['event']=='fault_start');last=next(e for e in faults if e['event']=='fault_end');t0,t1=int(first['wall_monotonic_ns']),int(last['wall_monotonic_ns'])
    grants=[e for e in events if e['event']=='grant_issued'];bygrant={e['grant_sequence']:e for e in grants}
    atfault=max((e for e in grants if e['host_monotonic_ns']<=t0 and e['valid_from_ns']<=t0<e['valid_until_ns']),key=lambda e:(e['epoch'],e['grant_sequence']))
    scopes={}
    for g in grants:scopes.setdefault((g['leader_id'],g['epoch']),g)
    new=[e for scope,e in scopes.items() if scope!=(1,1) and lo<=e['host_monotonic_ns']<=hi]
    own={}
    for e in events:
        if e['event']=='leader_activated' and e['node_id']==e['leader_id'] and (e['leader_id'],e['epoch'])!=(1,1):own.setdefault((e['leader_id'],e['epoch']),e)
    own=[e for e in own.values() if lo<=e['host_monotonic_ns']<=hi]
    def role_item(e):
        at=e['host_monotonic_ns'];return dict(leader=e['leader_id'],epoch=e['epoch'],host_ns=at,after_fault_s=(at-t0)/1e9,after_RFend_s=(at-t1)/1e9,inside_RFfault=t0<=at<t1,valid_from_ns=e.get('valid_from_ns'))
    commands=defaultdict(list)
    for e in events:
        if e.get('command_id'):commands[e['command_id']].append(e)
    good={};sent={};invalid=[]
    for cid,es in commands.items():
        g=next((e for e in es if e['event']=='gcs_send' and e.get('target_frame')=='ENU' and e.get('target_position_m') is not None and lo<=e['host_monotonic_ns']<=hi),None)
        if g is None:continue
        sent[cid]=g
        u=next((e for e in es if e['event']=='uart_admitted' and lo<=e['uart_write_end_host_monotonic_ns']<=hi),None)
        if u is None:continue
        issuer=bygrant.get(u['grant_sequence']);created=g['command_created_host_monotonic_ns'];write=u['uart_write_end_host_monotonic_ns'];checked=u['admission_checked_host_monotonic_ns']
        required={'gcs_send':0,'master_receive':g['leader_id'],'follower_receive':g['uav_id'],'uart_admitted':g['uav_id']}
        if g['uav_id']!=g['leader_id']:required['master_forward']=g['leader_id']
        chain=all(any(e['event']==name and e['node_id']==actor and e.get('payload_sha256')==g['payload_sha256'] for e in es) for name,actor in required.items())
        same=all(g[k]==u[k] for k in ('leader_id','epoch','uav_id','command_created_host_monotonic_ns','payload_sha256','target_position_m'))
        lease=bool(issuer and all(issuer[k]==u[k] for k in ('leader_id','epoch','valid_from_ns','valid_until_ns')) and u['node_id']==u['uav_id']
            and u['valid_from_ns']<=created<=u['uart_write_started_host_monotonic_ns']<=write<=checked<u['valid_until_ns']
            and checked-created<=cfg['effective_service_args']['command_max_age_ms']*1e6)
        policy=cfg['policy']!='fixed' or g['leader_id']==1
        if chain and same and lease and policy:good[cid]=dict(g,admitted_ns=write,admission=u)
        else:invalid.append(dict(command_id=cid,chain=chain,same=same,lease=lease,policy=policy))
    eligible=[c for c in good.values() if c['command_created_host_monotonic_ns']>=t0 and c['admitted_ns']>=t0]
    def endpoint(rows,followers=False):
        groups=defaultdict(dict)
        for c in sorted(rows,key=lambda c:c['admitted_ns']):groups[(c['leader_id'],c['epoch'])].setdefault(c['uav_id'],c)
        choices=[]
        for (leader,epoch),members in groups.items():
            required=set(range(1,6))-({leader} if followers else set())
            if required<=members.keys():choices.append((max(members[i]['admitted_ns'] for i in required),leader,epoch,sorted(required),{i:members[i]['command_id'] for i in required}))
        if not choices:return dict(recovered=False,time_s=None,censor_time_s=(hi-t0)/1e9)
        at,leader,epoch,members,cids=min(choices)
        return dict(recovered=True,time_s=(at-t0)/1e9,leader=leader,epoch=epoch,members=members,first_cids=cids,over4s=at-t0>4e9)
    control=endpoint(eligible);handover=endpoint([c for c in eligible if c['leader_id']!=atfault['leader_id'] and c['epoch']>atfault['epoch']])
    if cfg['policy']=='fixed':handover=dict(status='not_applicable_fixed_policy',time_s=None)
    offers=list(csv.DictReader((raw/'metrics/relay_control_refresh.csv').open()));offers=[o for o in offers if lo<=int(o['host_monotonic_ns'])<=hi]
    oldtargets={}
    for i in range(1,6):
        candidates=[c for c in good.values() if c['uav_id']==i and c['admitted_ns']<t0]
        oldtargets[i]=max(candidates,key=lambda c:c['admitted_ns'])['target_position_m'] if candidates else None
    physical={}
    for e in events:
        if e['event']!='action_completed' or not lo<=e['host_monotonic_ns']<=hi:continue
        i=e['uav_id'];c=good.get(e.get('admitted_command_id',e.get('command_id')));created=e.get('goal_created_host_monotonic_ns');reference=oldtargets[i]
        if c and created is not None and created>=t0 and e['host_monotonic_ns']>=t0 and reference and math.dist(reference,e['target_position_m'])>=1 and e.get('maneuver_observed') is True and e.get('max_vertical_displacement_m',0)>=1 and e.get('dwell_s',0)>=cfg['hold_s'] and e['target_position_m']==c['target_position_m'] and c['command_created_host_monotonic_ns']>=created and c['admitted_ns']<=e['host_monotonic_ns']:
            physical.setdefault(i,dict(command_id=c['command_id'],leader=c['leader_id'],epoch=c['epoch'],goal_id=e['goal_id'],goal_created_ns=created,completion_ns=e['host_monotonic_ns'],time_s=(e['host_monotonic_ns']-t0)/1e9))
    physicaltime=max(e['time_s'] for e in physical.values()) if len(physical)==5 else None
    phase=[e for e in events if e['event']=='phase_requested' and e.get('phase') in ('experiment_fault','experiment_recovery')]
    restored=min((e['host_monotonic_ns'] for e in events if e['event']=='gcs_probe_echo_received' and e['uav_id']==1 and t1<=e['host_monotonic_ns']<=hi and e.get('probe_sent_host_monotonic_ns',0)>=t1),default=None)
    lifecycle={}
    for i in range(1,6):
        take=next((e for e in events if e['event']=='takeoff_observed' and e['uav_id']==i),None)
        landing=next((e for e in events if e['event']=='landing_disarm_observed' and e['uav_id']==i),None)
        heartbeats=[e for e in events if e['event']=='autopilot_heartbeat_received' and e['uav_id']==i]
        armed=[e for e in heartbeats if e['base_mode']&128]
        goals=[e for e in events if e['event']=='action_completed' and e['uav_id']==i and lo<=e['host_monotonic_ns']<=hi and e.get('maneuver_observed') is True and e.get('dwell_s',0)>=cfg['hold_s']]
        final=heartbeats[-1] if heartbeats else None
        lifecycle[i]=dict(armed_radio_heartbeat_observed=bool(armed),takeoff_raw=take,landing_disarm_raw=landing,final_radio_heartbeat=final,
            completed_distinct_target_levels=len({e['target_position_m'][2] for e in goals}),
            ordered_lift_and_landing=bool(take and landing and armed and armed[0]['host_monotonic_ns']<=take['host_monotonic_ns']<landing['host_monotonic_ns'] and final and (final['host_monotonic_ns']>=landing['host_monotonic_ns'] or landing['host_monotonic_ns']-final['host_monotonic_ns']<=cfg['effective_service_args']['telemetry_max_age_ms']*1e6) and not final['base_mode']&128))
    params=[]
    for stage in ('before','after'):
        pp=raw/f'configuration/parameters-{stage}'
        if not (pp/'summary.json').exists():
            params.append(dict(stage=stage,status='not_recorded',parent_record=meta.get('parameter_snapshots',{}).get(stage),components=[]));continue
        s=json.loads((pp/'summary.json').read_text());csvrows=list(csv.DictReader((pp/'parameters.csv').open()))
        components=[]
        for c in s['components']:
            rows=[r for r in csvrows if int(r['system_id'])==c['system_id'] and int(r['component_id'])==1]
            names={r['param_id']:r for r in rows};checks={k:json.loads(names[k]['value_json']) if k in names else None for k in ('ARMING_SKIPCHK','COMPASS_USE','SIM_SPEEDUP')}
            components.append(dict(sysid=c['system_id'],component_id=c['component_id'],complete=c['complete'],count=c['expected_count'],received_valid_index_count=c['received_index_count'],csv_count=len(rows),csv_indices_complete=({int(r['param_index']) for r in rows if r['param_index']}==set(range(c['expected_count'])) if c['expected_count'] is not None else None),checks=checks))
        params.append(dict(stage=stage,status=s['status'],workers=s.get('effective_workers'),duration_s=(s['finished_host_monotonic_ns']-s['started_host_monotonic_ns'])/1e9,components=components))
    rec=dict(order=order,run_id=run,planned=planned,exit_code=completed[run],status=meta['status'],criteria=meta['criteria'],frozen_source_matches=env.get('git_head')==freeze['frozen_head'],source_dirty_paths=env.get('source_dirty_paths'),
        config_fault_matches=all(abs(float(cfg[k])-float(planned[k]))<1e-6 for k in ('fault_start_s','fault_end_s','duration_s')),parameter_workers=cfg['parameter_workers'],parameters=params,native_rng_matches=(stats['rng_seed']==int(planned['rng_seed']) and stats['rng_run']==int(planned['rng_run'])),image_matches=manifest['container_image_id']==freeze['container_image_id'],raw_publication_status=publication['status'],raw_publication_errors=publication['errors'],raw_publication_file_sizes_match=all((raw/f['destination'].split('/'+run+'/',1)[1]).stat().st_size==f['bytes'] for f in publication['files']),
        measurement_duration_source_s=meta['measurement_duration_source_s'],measurement_host_ns=[lo,hi],actual_native_fault=faults,actual_RF_duration_host_s=(t1-t0)/1e9,
        fault_phase_source_elapsed_s={e['phase']:e['source_sim_time_s']-meta['measurement_start_source_sim_time_s'] for e in phase},
        prefault_scopes_all_raw=sorted({(e['leader_id'],e['epoch']) for e in grants if e['host_monotonic_ns']<t0}),prefault_active_leader_epoch=[atfault['leader_id'],atfault['epoch']],
        new_selections=list(map(role_item,new)),new_own_activations=list(map(role_item,own)),confirmed_old_channel_restored_after_fault_s=None if restored is None else (restored-t0)/1e9,lifecycle_small_raw=lifecycle,offered=len(offers),unique_offer_ids=len({o['request_id'] for o in offers}),offers_by_uav=dict(Counter(o['uav_id'] for o in offers)),
        target_commands_sent=len(sent),valid_target_commands=len(good),invalid_admitted_commands=invalid,common_UART_all5=control,common_UART_followers=endpoint(eligible,True),new_relay_UART_all5=handover,
        physical_goal_first_by_uav=physical,physical_goal_all5_s=physicaltime,physical_goal_all5_over4s=None if physicaltime is None else physicaltime>4,
        note='Independent terminal-case records (completed or explicitlyfailed); not a completed18 cohort or a new PCAP/trajectory replay.')
    records.append(rec)
args.output.write_text(json.dumps(dict(method='One-off independent RG-filtered raw role/command/UART recomputation, no production analyzer imports',complete18_cohort=False,endpoint_interpretation='Physical goal duration has no4s SLA; common UART and changed-relay UART are separate endpoints.',records=records),indent=2)+'\n')
