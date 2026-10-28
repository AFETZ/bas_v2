#!/usr/bin/env python3
"""Read only the frozen main30 primary logs; no production analyzer imports."""
import argparse, collections, csv, hashlib, itertools, json, math, statistics, subprocess
from pathlib import Path


def writecsv(path,rows):
    with path.open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(dict.fromkeys(k for r in rows for k in r)),lineterminator='\n');w.writeheader();w.writerows(rows)

def filtered(path,names):
    # rg avoids JSON decoding high-volume telemetry; it never modifies inputs.
    pattern='"event"\\s*:\\s*"(?:'+'|'.join(names)+')"'
    p=subprocess.Popen(['rg',pattern,str(path)],stdout=subprocess.PIPE,text=True)
    for line in p.stdout:yield json.loads(line)
    rc=p.wait()
    if rc not in (0,1):raise RuntimeError(f'rg failed: {path}: {rc}')

def point(r):
    p=r.get('target_position_m');return tuple(p) if r.get('target_frame')=='ENU' and isinstance(p,list) and len(p)==3 else None

def exact(values):
    if not values:return None,None,None
    means=sorted(sum(s)/len(s) for s in itertools.product(values,repeat=len(values)))
    def q(p):
        at=(len(means)-1)*p;lo=math.floor(at);hi=math.ceil(at);return means[lo]+(means[hi]-means[lo])*(at-lo)
    return statistics.fmean(values),q(.025),q(.975)

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--repository',type=Path,required=True);ap.add_argument('--output',type=Path,required=True);a=ap.parse_args();a.output.mkdir(parents=True,exist_ok=True)
    plan=list(csv.DictReader((a.repository/'network/config/relay_campaign_30.csv').open()))
    result=[];breakdown=[];goalrows=[];changes=[]
    for spec in plan:
        rid=spec['run_id'];root=a.repository/'runs/native-radio-realtime'/rid
        cfg=json.loads((root/'metrics/relay_experiment_config.json').read_text());summary=json.loads((root/'metrics/relay_experiment_summary.json').read_text())
        start=summary['measurement_start_host_monotonic_ns'];end=start+round(summary['measurement_duration_wall_s']*1e9);inside=lambda t:start<=t<=end
        gcs=list(filtered(root/'logs/coordination_gcs.jsonl',['gcs_send','grant_issued','goal_created','action_completed']))
        sends={r['command_id']:r for r in gcs if r['event']=='gcs_send'}
        assert len(sends)==sum(r['event']=='gcs_send' for r in gcs)
        grants={r['grant_sequence']:r for r in gcs if r['event']=='grant_issued'}
        actions=[r for r in gcs if r['event']=='action_completed' and inside(r['host_monotonic_ns'])]
        created=[r for r in gcs if r['event']=='goal_created' and inside(r['host_monotonic_ns'])]
        offer=list(csv.DictReader((root/'metrics/relay_control_refresh.csv').open()));assert len(offer)==len({r['request_id'] for r in offer})==900
        offercids={r['command_id']:r for r in offer if r['command_id']};assert len(offercids)==sum(bool(r['command_id']) for r in offer)
        assert all(inside(int(r['host_monotonic_ns'])) for r in offer)
        stages=collections.defaultdict(list);admissions=[];seen=set();errors=collections.Counter();counts=collections.Counter()
        for path in sorted((root/'logs').glob('coordination_uav*.jsonl')):
            for r in filtered(path,['master_receive','master_forward','follower_receive','uart_admitted','uart_write_invalidated']):
                if r.get('command_id') in sends:stages[r['command_id']].append(r)
                if r['event']!='uart_admitted':continue
                admissions.append(r);key=(r['uav_id'],r['command_id']);errors['duplicate_admitted']+=key in seen;seen.add(key)
                g=grants.get(r['grant_sequence']);c=r['command_created_host_monotonic_ns'];ws=r['uart_write_started_host_monotonic_ns'];we=r['uart_write_end_host_monotonic_ns'];check=r['admission_checked_host_monotonic_ns']
                valid=bool(g and all(g[k]==r[k] for k in ['epoch','leader_id','valid_from_ns','valid_until_ns']) and r['node_id']==r['uav_id'] and g['valid_from_ns']<=c<=ws<=we<=check<g['valid_until_ns'] and check-c<=250e6)
                errors['invalid_authority_or_age']+=not valid
                r['_valid']=valid
                category=('target' if point(r) else 'non_target')+'_'+('window' if inside(we) else 'outside_window');counts[category]+=1
        admitted={r['command_id']:r for r in admissions}
        verified={}
        for cid,s in sends.items():
            r=admitted.get(cid)
            if r is None or not point(s) or not inside(s['host_monotonic_ns']):continue
            actors={'master_receive':s['leader_id'],'follower_receive':s['uav_id'],'uart_admitted':s['uav_id']}
            if s['uav_id']!=s['leader_id']:actors['master_forward']=s['leader_id']
            fields=['epoch','leader_id','uav_id','command_created_host_monotonic_ns','payload_sha256']
            chain=all(any(t['event']==event and t['node_id']==actor and all(t.get(k)==s.get(k) for k in fields) for t in stages[cid]) for event,actor in actors.items())
            policy=spec['policy']!='fixed' or s['leader_id']==cfg['initial_master']
            age=r['uart_write_end_host_monotonic_ns']-s['command_created_host_monotonic_ns']
            if chain and policy and r['_valid'] and point(s)==point(r) and 0<=age<=250e6 and inside(r['uart_write_end_host_monotonic_ns']):verified[cid]=r
        timely=0
        for o in offer:
            v=verified.get(o['command_id'])
            if v:
                assert int(o['uav_id'])==v['uav_id'] and int(o['epoch'])==v['epoch'] and int(o['leader_id'])==v['leader_id']
                assert int(o['goal_created_host_monotonic_ns'])<=v['command_created_host_monotonic_ns']
                timely+=1
        condition=spec['fault_profile'];fault=None
        # Filter two rows externally, not a full Python parse of native-radio events.
        p=subprocess.check_output(['rg',',fault_(start|end),',str(root/'logs/native_radio_events.csv')],text=True) if condition!='none' else ''
        if p:
            header=(root/'logs/native_radio_events.csv').open().readline();native=list(csv.DictReader((header+p).splitlines()));fault=int(next(r for r in native if r['event']=='fault_start')['wall_monotonic_ns'])
        uart=goal=None
        if fault is not None:
            groups=collections.defaultdict(dict)
            for r in sorted(verified.values(),key=lambda v:v['uart_write_end_host_monotonic_ns']):
                if r['command_created_host_monotonic_ns']>=fault:groups[r['leader_id'],r['epoch']].setdefault(r['uav_id'],r)
            times=[max(d[i]['uart_write_end_host_monotonic_ns'] for i in range(1,6)) for d in groups.values() if set(range(1,6))<=d.keys()]
            uart=(min(times)-fault)/1e9 if times else None
            per=[]
            for u in range(1,6):
                before=[r for r in admissions if r['_valid'] and r['uav_id']==u and point(r) and r['uart_write_end_host_monotonic_ns']<fault]
                active=max(before,key=lambda r:r['uart_write_end_host_monotonic_ns']);candidates=[]
                for r in actions:
                    cmd=verified.get(r.get('admitted_command_id'));gc=r.get('goal_created_host_monotonic_ns')
                    if r['uav_id']!=u or gc is None or gc<fault or not cmd:continue
                    if (offercids.get(cmd['command_id'],{}).get('goal_id')==r['goal_id'] and point(r) and point(cmd)==point(r) and math.dist(point(active),point(r))>=1 and gc<=cmd['command_created_host_monotonic_ns'] and cmd['uart_write_end_host_monotonic_ns']<=r['host_monotonic_ns'] and r.get('maneuver_observed') is True and r.get('max_vertical_displacement_m',0)>=1 and r.get('dwell_s',0)>=cfg['hold_s']):candidates.append(r)
                first=min(candidates,key=lambda r:r['host_monotonic_ns']) if candidates else None
                per.append((first['host_monotonic_ns']-fault)/1e9 if first else None)
                goalrows.append(dict(run_id=rid,uav_id=u,goal_id=first['goal_id'] if first else None,first_completion_CID=first['admitted_command_id'] if first else None,epoch=first['admitted_epoch'] if first else None,recovery_s=per[-1],pose_review='prior independent50-goal trajectory review; this script replays completion lineage only'))
            goal=max(per) if all(v is not None for v in per) else None
        row=dict(run_id=rid,condition=condition,policy=spec['policy'],rng_run=int(spec['rng_run']),offered=len(offer),unique_offered_CIDs=len(offercids),offered_unsent=len(offer)-len(offercids),unique_created_goals=len({r['goal_id'] for r in created}),unique_offered_goals=len({r['goal_id'] for r in offer}),timely=timely,timely_per_offered=timely/len(offer),T_UART_s=uart,T_goal_s=goal,all_UART=len(admissions),unique_UART=len(seen),**counts,**errors)
        result.append(row)
        exported=json.loads((a.repository/'runs/paper-live-20261026/campaign-analysis/runs'/rid/'metrics/relay_analysis.json').read_text())
        expected={'timely':exported['rates']['all_five']['timely'],'all_UART':exported['admission_total'],'T_UART_s':exported['control_recovery']['all_five']['time_s'],'T_goal_s':exported['goal_recovery_all_five']['time_s']}
        for k,v in expected.items():
            if row[k]!=v and not(v is not None and row[k] is not None and abs(row[k]-v)<1e-9):changes.append(dict(run_id=rid,metric=k,recomputed=row[k],previous=v))
        print(rid,len(admissions),timely,flush=True)
    writecsv(a.output/'metrics_runs.csv',result);writecsv(a.output/'metrics_goal_lineages.csv',goalrows)
    pairs=[];estimates=[];cells=[]
    for condition in ['none','selective','total_GCS']:
        for u in range(1,6):
            rr={r['policy']:r for r in result if r['condition']==condition and r['rng_run']==u}
            for m in ['timely_per_offered','T_UART_s','T_goal_s']:
                x,y=rr['fixed'][m],rr['failover'][m];pairs.append(dict(condition=condition,rng_run=u,metric=m,fixed=x,failover=y,difference=y-x if None not in (x,y) else None))
        for m in ['timely_per_offered','T_UART_s','T_goal_s']:
            diffs=[r['difference'] for r in pairs if r['condition']==condition and r['metric']==m and r['difference'] is not None];mean,low,high=exact(diffs);estimates.append(dict(condition=condition,metric=m,n_pairs=len(diffs),mean_difference=mean,interval_low=low,interval_high=high,exact_resamples=len(diffs)**len(diffs) if diffs else 0))
        for policy in ['fixed','failover']:
            rr=[r for r in result if r['condition']==condition and r['policy']==policy]
            cell=dict(condition=condition,policy=policy,n=len(rr))
            for m in ['offered','timely','all_UART','unique_offered_CIDs','unique_created_goals']:cell[m+'_sum']=sum(r[m] for r in rr)
            for m in ['timely_per_offered','T_UART_s','T_goal_s']:cell[m+'_mean']=statistics.fmean(r[m] for r in rr) if all(r[m] is not None for r in rr) else None
            cells.append(cell)
    writecsv(a.output/'metrics_pairs.csv',pairs);writecsv(a.output/'metrics_intervals.csv',estimates);writecsv(a.output/'metrics_cells.csv',cells)
    totals={k:sum(r.get(k,0) for r in result) for k in ['offered','unique_offered_CIDs','offered_unsent','unique_created_goals','unique_offered_goals','timely','all_UART','unique_UART','target_window','target_outside_window','non_target_window','non_target_outside_window','duplicate_admitted','invalid_authority_or_age']}
    (a.output/'metrics_replay.json').write_text(json.dumps(dict(source_head='8e16ec223e895c11891e6e7d7c39dff8cee39cac',audit_base='bb026a052a2474df29bee3603cc98af27592e207',primary_inputs='raw GCS/UAV JSONL, offered CSV, native fault CSV, run config/summary',pose_replay=False,counts=totals,discrepancies=changes,run_count=len(result)),indent=2)+'\n')
    assert not changes,changes
    assert totals['all_UART']==totals['unique_UART'] and sum(totals[k] for k in ['target_window','target_outside_window','non_target_window','non_target_outside_window'])==totals['all_UART']
    print(json.dumps(totals),flush=True)
if __name__=='__main__':main()
