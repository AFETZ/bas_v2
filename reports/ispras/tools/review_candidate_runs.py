#!/usr/bin/env python3
"""Independent candidate availability/lease/pose review; no production analyzer."""
import argparse,csv,json,subprocess,math
from pathlib import Path
from recovery_timing import selected,delta,stamp

def review(raw):
 config=json.loads((raw/'metrics/relay_experiment_config.json').read_text());summary=json.loads((raw/'metrics/relay_experiment_summary.json').read_text())
 g=selected(raw/'logs/coordination_gcs.jsonl',['grant_issued','gcs_send','gcs_probe_echo_received','action_completed','goal_created','landing_disarm_observed'])
 faults=list(csv.reader(subprocess.check_output(['rg',',fault_(start|end),',str(raw/'logs/native_radio_events.csv')],text=True).splitlines()))
 F=int(next(r[1] for r in faults if r[3]=='fault_start'));E=int(next(r[1] for r in faults if r[3]=='fault_end'))
 sel=[e for e in g if e['event']=='grant_issued' and e['reason']=='selection' and stamp(e)>=F]
 grants=[e for e in g if e['event']=='grant_issued'];lastmax=0;lastleader=None;overlap=[]
 for e in grants:
  if lastleader is not None and e['leader_id']!=lastleader and e['valid_from_ns']<lastmax:overlap.append(e)
  lastmax=max(lastmax,e['valid_until_ns']);lastleader=e['leader_id']
 sends={e['command_id']:e for e in g if e['event']=='gcs_send'};physical={};individual=[];count=invalid=duplicate=0
 master={};scope=(sel[0]['leader_id'],sel[0]['epoch']) if sel else (1,1)
 for i in range(1,6):
  ev=selected(raw/f'logs/coordination_uav{i}.jsonl',['uart_admitted','uart_write_invalidated'])
  admits=[e for e in ev if e['event']=='uart_admitted'];count+=len(admits);ids=[e['command_id'] for e in admits];duplicate+=len(ids)-len(set(ids))
  bad=[e for e in admits if not(e['valid_from_ns']<=e['uart_write_started_host_monotonic_ns']<=e['uart_write_end_host_monotonic_ns']<e['valid_until_ns'] and 0<=e['uart_write_end_host_monotonic_ns']-e['command_created_host_monotonic_ns']<=250_000_000)];invalid+=len(bad)+sum(e['event']=='uart_write_invalidated' for e in ev)
  targets=[e for e in admits if e.get('target_position_m') is not None]
  post=[e for e in targets if e['command_created_host_monotonic_ns']>=F and (e['leader_id'],e['epoch'])==scope]
  first=min(post,key=lambda e:e['uart_write_end_host_monotonic_ns']) if post else None
  if first:
   sent=sends[first['command_id']];assert all(first[k]==sent[k] for k in ('payload_sha256','grant_sequence','target_position_m','epoch','leader_id'))
  individual.append(dict(uav_id=i,first_CID=first['command_id'] if first else None,UART_recovery_s=delta(first['uart_write_end_host_monotonic_ns'],F) if first else None))
  completions=sorted([e for e in g if e['event']=='action_completed' and e['uav_id']==i and e['goal_created_host_monotonic_ns']>=F and (e['leader_id'],e['epoch'])==scope],key=stamp)
  for c in completions:
   matches=[e for e in targets if e['target_position_m']==c['target_position_m'] and e['command_created_host_monotonic_ns']>=c['goal_created_host_monotonic_ns'] and e['epoch']==c['epoch'] and stamp(e)<=stamp(c)]
   if not matches:continue
   start=min(matches,key=lambda e:e['uart_write_end_host_monotonic_ns']);previous=[e for e in targets if e['uart_write_end_host_monotonic_ns']<F]
   if not previous or math.dist(c['target_position_m'],max(previous,key=stamp)['target_position_m'])<1:continue
   physical[i]=dict(CID=c['command_id'],first_goal_CID=start['command_id'],start_ns=start['uart_write_end_host_monotonic_ns'],end_ns=stamp(c),target=c['target_position_m'],completion_source_s=c['source_sim_time_s'],T_goal_s=delta(stamp(c),F),samples=[]);break
 with (raw/'logs/node_state.jsonl').open() as f:
  for line in f:
   r=json.loads(line)
   for n in r['nodes']:
    if n['id']=='cp':continue
    i=int(n['id'][3:]);p=physical.get(i)
    if p and p['start_ns']<=n.get('sample_monotonic_ns',-1)<=p['end_ns'] and not n['stale']:
     source=n['source_sim_time_s'];ss=p['samples']
     if not ss or source!=ss[-1][0]:ss.append((source,n['position_m'],n['velocity_enu_mps']))
 for i,p in physical.items():
  ss=p.pop('samples');dwell=[x for x in ss if p['completion_source_s']-2.0-1e-8<=x[0]<=p['completion_source_s']]
  p.update(pose_samples=len(ss),independent_max_vertical_displacement_m=max(abs(x[1][2]-ss[0][1][2]) for x in ss) if ss else None,
   dwell_observed_source_span_s=dwell[-1][0]-dwell[0][0] if dwell else None,
   dwell_max_speed_mps=max(math.dist(x[2],[0,0,0]) for x in dwell) if dwell else None,
   dwell_max_z_error_m=max(abs(x[1][2]-p['target'][2]) for x in dwell) if dwell else None,
   dwell_max_xy_error_m=max(math.dist(x[1][:2],p['target'][:2]) for x in dwell) if dwell else None,
   dwell_max_source_gap_s=max((b[0]-a[0] for a,b in zip(dwell,dwell[1:])),default=None))
 return dict(run_id=raw.name,status=summary['status'],pilot=config['pilot'],nominal_fault_s=config['fault_end_s']-config['fault_start_s'],actual_RF_duration_s=delta(E,F),policy=config['policy'],selections=[dict(leader=e['leader_id'],epoch=e['epoch'],at_ns=stamp(e),valid_from=e['valid_from_ns']) for e in sel],direct_UAV4_echo_during_fault=sum(e['event']=='gcs_probe_echo_received' and e['uav_id']==4 and F<=stamp(e)<=E for e in g),direct_UAV4_echo_count=sum(e['event']=='gcs_probe_echo_received' and e['uav_id']==4 for e in g),overlap_violations=overlap,all_UART_count=count,invalid_UART_count=invalid,duplicate_UART_CIDs=duplicate,individual_UART=individual,T_UART_s=max(r['UART_recovery_s'] for r in individual) if all(r['UART_recovery_s'] is not None for r in individual) else None,physical_goals=physical,criteria_recorded=summary['criteria'],limits='Vertical diagnostic mission, INPUT-only GCS filter; not all-peer UAV4 isolation, not building-aware, not new PCAP audit.')

def main():
 ap=argparse.ArgumentParser();ap.add_argument('--raw-root',type=Path,required=True);ap.add_argument('--run-ids',nargs='+',required=True);ap.add_argument('--output',type=Path,required=True);a=ap.parse_args();out=[review(a.raw_root/r) for r in a.run_ids];a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(dict(method='Independent raw authority/CID and deduplicated Gazebo pose replay; no production analyzer import',records=out),indent=2)+'\n');print(json.dumps([{k:r[k] for k in ['run_id','all_UART_count','invalid_UART_count','duplicate_UART_CIDs','T_UART_s','selections']} for r in out]))
if __name__=='__main__':main()
