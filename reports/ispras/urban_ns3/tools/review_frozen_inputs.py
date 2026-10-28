#!/usr/bin/env python3
"""Independent stdlib frozen-input and derived native-proof consistency check.

Reads inputs only. Does not import project validators or run Docker/PCAP scans.
"""
import argparse
import collections
import copy
import csv
import hashlib
import itertools
import json
from pathlib import Path
import random
import subprocess


def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def load(path):return json.loads(Path(path).read_text())
def jsonl(path):return [json.loads(x)for x in Path(path).open()]
def env(path):return dict(x.split('=',1)for x in Path(path).read_text().splitlines()if '=' in x)


def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True)
    p.add_argument('--freeze',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();root=a.root.resolve();f=load(a.freeze);checks={};errors=[]
    def check(name,value):
        checks[name]=value
        if value is not True:errors.append(name)
    cases=f['cases'];counts=dict(collections.Counter(x['cohort']for x in cases))
    check('exact56_cases',len(cases)==56 and counts=={'main':40,'sensitivity':12,'engineering_boundary':4})
    check('unique_run_ids',len({x['run_id']for x in cases})==56)
    check('contiguous_orders',[x['order']for x in cases]==list(range(1,57)))
    blocks=collections.defaultdict(list)
    for c in cases:blocks[c['cohort'],c['block_id']].append(c)
    main_rng={};grid_ok=True;aux_ok=True;within_ok=True
    for (cohort,block),rows in blocks.items():
        values=[{x[k]for x in rows}for k in ['rng_seed','rng_run','duration_s','scenario_variant','parameter_variant']]
        within_ok &= all(len(x)==1 for x in values)
        actual={(x['policy'],x['radio_mode'])for x in rows}
        if cohort=='main':
            grid_ok &= len(rows)==4 and actual==set(itertools.product(['fixed','failover'],['geometry','los_only']))
            main_rng[block]=[rows[0]['rng_seed'],rows[0]['rng_run']]
        else:aux_ok &= len(rows)==2 and {x['policy']for x in rows}=={'fixed','failover'} and len({x['radio_mode']for x in rows})==1
        for mode in {x['radio_mode']for x in rows}:
            within_ok &= len({x['config_sha256']for x in rows if x['radio_mode']==mode})==1
    check('main_four_case_grid',bool(grid_ok));check('auxiliary_exact_policy_pairs',bool(aux_ok))
    check('common_conditions_inside_blocks',bool(within_ok))
    check('ten_distinct_main_rng_blocks',len(main_rng)==10 and len({tuple(x)for x in main_rng.values()})==10)
    check('main_rng_declaration_matches_cases',main_rng==f['main_block_RNG_pairs'])
    check('all_fixed_horizons240',all(x['duration_s']==240 for x in cases)and f['observation_horizon_source_sim_time_s']==240)
    check('predeclared_endpoints',f['outcome_spec']['optional_restricted_horizon_host_s']==60 and
          f['outcome_spec']['physical_hold_source_s']==2 and f['outcome_spec']['route_hold_autopilot_s']==3)
    check('cleanup60_and_full_parameters',f['land_timeout_s']==60 and f['parameter_snapshots']is True and f['parameter_workers']==5)
    # Reproduce independent RNG ordering without calling the project planner.
    g=random.Random(20261028);repeats=list(range(1,11));g.shuffle(repeats);order=[]
    for repeat in repeats:
        cells=list(itertools.product(['fixed','failover'],['geometry','los_only']));g.shuffle(cells)
        order.extend(('main','nominal','nominal',m,s,1,repeat)for s,m in cells)
    g=random.Random(20261029);sweep=list(itertools.product(['low','high'],range(1,4)));g.shuffle(sweep)
    for parameter,repeat in sweep:
        policies=['fixed','failover'];g.shuffle(policies)
        order.extend(('sensitivity','nominal',parameter,'geometry',s,1,repeat)for s in policies)
    for scenario in ['short_occlusion','no_candidate']:
        policies=['fixed','failover'];g.shuffle(policies)
        order.extend(('engineering_boundary',scenario,'nominal','geometry',s,1,1)for s in policies)
    actual=[tuple(x[k]for k in ['cohort','scenario_variant','parameter_variant','radio_mode','policy','rng_seed','rng_run'])for x in cases]
    check('random_order_reproduced',order==actual)
    scenarios={key:load(v['path'])for key,v in f['scenario_inputs'].items()}
    artifact_checks=[]
    for kind,items in [('scenario',list(f['scenario_inputs'].values())),('review',f['independent_reviews']),
                       ('pilot_analysis',f['pilot_analyses']),('base_environment',[f['base_environment_source']])]:
        for item in items:
            path=Path(item['path']);ok=path.is_file()and path.stat().st_size==item['bytes']and digest(path)==item['sha256']
            artifact_checks.append(dict(kind=kind,path=str(path),matches_frozen_bytes_and_sha256=ok))
    check('all_attached_inputs_match_actual_bytes',all(x['matches_frozen_bytes_and_sha256']for x in artifact_checks))
    for c in cases:
        if digest(c['config'])!=c['config_sha256']:errors.append('case config hash mismatch '+c['run_id'])
    normalized=[]
    for key in ['nominal','los_only']:
        value=copy.deepcopy(scenarios[key]);value['radio'].pop('mode');normalized.append(value)
    check('physical_ablation_exact_except_radio_mode',normalized[0]==normalized[1])
    check('nominal_vs_control_modes',scenarios['nominal']['radio']['mode']=='geometry'and scenarios['los_only']['radio']['mode']=='los_only')
    check('nominal_route_hold3',scenarios['nominal']['mission']['waypoint_dwell_s']==3)
    check('nominal_speed3',scenarios['nominal']['mission']['speed_mps']==3)
    binary=f['native_binary'];check('actual_native_binary_matches_freeze',Path(binary['path']).stat().st_size==binary['bytes']and digest(binary['path'])==binary['sha256'])
    head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip()
    check('integration_HEAD_unchanged',head==f['source_commit'])
    proof_path=root/'reports/ispras/urban_ns3/pilot_pair/native_epoch2.json';proof=load(proof_path)
    run=root/'runs/native-radio-realtime'/proof['run_id'];events={name:jsonl(run/f'logs/coordination_{name}.jsonl')for name in ['gcs','uav1','uav2','uav3','uav4','uav5']}
    sends={x['command_id']:x for x in events['gcs']if x['event']=='gcs_send'}
    admissions={x['command_id']:x for name,rows in events.items()for x in rows if x['event']=='uart_admitted'}
    raw_return={(x.get('origin_id'),x.get('payload_sha256'))for x in events['gcs']if x['event']=='gcs_receive'}
    original_uart={(x.get('node_id'),x.get('payload_sha256'))for name,rows in events.items()for x in rows if x['event']=='uart_observed'}
    start=next(x['host_monotonic_ns']for x in events['gcs']if x['event']=='phase_requested'and x['phase']=='experiment_normal')
    previous=None;nlos=None
    with (run/'logs/geometry_links.csv').open()as stream:
        for row in csv.DictReader(stream):
            if (row['from_id'],row['to_id'])!=('0','1'):continue
            state=row['geom_los'];at=int(row['wall_monotonic_ns'])
            if previous=='1'and state=='0'and at>=start:nlos=at;break
            previous=state
    proof_ok=True;native_commands=[]
    for c in proof['commands']:
        cid=c['command_id'];ad=admissions[cid];sent=sends[cid];master=c['leader_id'];uav=c['destination_id']
        hop_results=[]
        proof_ok &= nlos is not None and sent['command_created_host_monotonic_ns']>=nlos
        proof_ok &= sent['epoch']==ad['epoch']==proof['epoch']and sent['leader_id']==ad['leader_id']==proof['leader_id']
        for hop,(source,destination)in zip(c['command_hops'],[(0,master),(master,uav)]):
            observations=hop['observations'];correct=set(x['capture_node_id']for x in observations)>={source,destination}
            for x in observations:
                correct &= x['capture']==proof['capture_node_mapping'][str(x['capture_node_id'])]
                correct &= (run/'pcap'/x['capture']).is_file()and'pcap_sim_s'in x and'pcap_unix_s'not in x
                correct &= x['payload_sha256']==digest_bytes(bytes.fromhex(x['payload_hex']))==ad['payload_sha256']==sent['payload_sha256']
                correct &= x['payload_bytes']==ad['bytes']
            hop_results.append(bool(correct));proof_ok &= correct
        returned=c['subsequent_telemetry_return'];return_ok=returned is not None and(uav,returned['uart_record_sha256'])in original_uart
        if returned:
            for fragment in returned['fragments']:
                down,up=fragment['follower_to_master'],fragment['master_to_gcs']
                return_ok &= {x['capture_node_id']for x in down}>={uav,master}and{x['capture_node_id']for x in up}>={master,0}
                return_ok &= down[0]['payload_sha256']==up[0]['payload_sha256']and(uav,up[0]['payload_sha256'])in raw_return
        proof_ok &= return_ok
        native_commands.append(dict(command_id=cid,destination_id=uav,command_hops_raw_log_consistent=hop_results,
            actual_UART_return_hash_consistent=bool(return_ok),
            created_after_CP1_NLOS_s=(sent['command_created_host_monotonic_ns']-nlos)/1e9 if nlos else None,
            admitted_after_CP1_NLOS_s=(ad['uart_write_end_host_monotonic_ns']-nlos)/1e9 if nlos else None))
    check('native_four_followers_proof_consistent_with_raw_logs',bool(proof_ok)and sorted(x['destination_id']for x in native_commands)==[1,2,3,5]and proof['capture_kind']=='native'and proof['parse_notes']=={})
    pilot_windows=[]
    for name in ['urban-staged-pilot05-failover','urban-staged-pilot06-fixed']:
        raw=root/'runs/native-radio-realtime'/name;cfg=load(raw/'metrics/relay_experiment_config.json');summary=load(raw/'metrics/relay_experiment_summary.json')
        gcs=jsonl(raw/'logs/coordination_gcs.jsonl');start=next(x for x in gcs if x['event']=='phase_requested'and x['phase']=='experiment_normal');end=next(x for x in gcs if x['event']=='urban_measurement_window_closed')
        offers=sum(x['event']=='control_refresh_offered'and start['host_monotonic_ns']<=x['host_monotonic_ns']<end['host_monotonic_ns']for x in gcs)
        snap=load(raw/'logs/urban_scenario.json')
        check(name+'_window240_offers1200',end['source_sim_time_s']-start['source_sim_time_s']>=240 and offers==1200)
        check(name+'_physical2_route3',cfg['hold_s']==2 and snap['mission']['waypoint_dwell_s']==3)
        pilot_windows.append(dict(run_id=name,status=summary['status'],error=summary['error'],offers=offers,
            observed_source_span_s=end['source_sim_time_s']-start['source_sim_time_s'],physical_hold_s=cfg['hold_s'],route_hold_s=snap['mission']['waypoint_dwell_s'],
            raw_action_completed_count=sum(x['event']=='action_completed'for x in gcs),land_timeout_s=cfg['land_timeout_s']))
    first_ledger=a.freeze.parent/'controllers'/(cases[0]['run_id']+'.json')
    first=load(first_ledger)if first_ledger.is_file()else None
    check('freeze_precedes_first_controller',first is not None and f['frozen_at_host_monotonic_ns']<first['started_host_monotonic_ns'])
    first_environment=root/'runs/native-radio-realtime'/cases[0]['run_id']/'environment.txt'
    first_env=env(first_environment)if first_environment.is_file()else{}
    check('first_main_postbuild_binary_hash_matches_freeze',first_env.get('native_binary_sha256')==binary['sha256'])
    check('first_main_source_identity_matches_freeze',first_env.get('git_head')==f['source_commit']and first_env.get('source_dirty_paths')=='0')
    validation=load(a.freeze.parent/'freeze-validation.json')
    check('validation_matches_actual_freeze',validation['cases']==56 and validation['source_commit']==f['source_commit']and validation['native_binary_sha256']==binary['sha256']and validation['independent_main_seed_pairs']==main_rng)
    result=dict(scope='independent fixed input/design and pilot/native-proof consistency review; no main outcome analysis',
        freeze_path=str(a.freeze.resolve()),freeze_sha256=digest(a.freeze),source_commit=f['source_commit'],
        review_script_sha256=digest(__file__),checks=checks,issues=errors,cohort_counts=counts,
        main_block_RNG_pairs=main_rng,artifact_checks=artifact_checks,pilot_windows=pilot_windows,
        native_proof_path=str(proof_path),native_proof_sha256=digest(proof_path),native_commands=native_commands,
        native_raw_log_sha256={str(run/f'logs/coordination_{name}.jsonl'):digest(run/f'logs/coordination_{name}.jsonl')for name in events},
        limits=['No Docker/image-inspect/build or full-PCAP reread; image identity is checked as frozen metadata only.',
                'Native proof crosschecked against actual UART/GCS logs and file identity; packet bytes were previously read by the native inspector, not rescanned here.',
                'This does not independently recalculate all pilot physical dwell traces or any main-series effect.',
                'Unique RNG blocks are a design property, not a claim that shared-host scheduling noise is absent.'])
    a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(dict(checks=checks,issues=errors),indent=2))


def digest_bytes(value):return hashlib.sha256(value).hexdigest()
if __name__=='__main__':main()
