#!/usr/bin/env python3
"""Independent small-raw timing audit. No project analyzer or simulator imports."""
import argparse,csv,json,subprocess,statistics,html,hashlib
from pathlib import Path

def selected(path,events):
 pattern='|'.join('"event":"'+e+'"' for e in events)
 r=subprocess.run(['rg',pattern,str(path)],capture_output=True,text=True)
 if r.returncode not in (0,1):raise RuntimeError(r.stderr)
 return [json.loads(l) for l in r.stdout.splitlines()]
def delta(a,b):return (a-b)/1e9 if a is not None and b is not None else None
def stamp(r):return r['host_monotonic_ns'] if r else None

def main():
 ap=argparse.ArgumentParser();ap.add_argument('--raw-root',type=Path,required=True);ap.add_argument('--plan',type=Path,default=Path('network/config/relay_campaign_30.csv'));ap.add_argument('--output',type=Path,required=True);a=ap.parse_args();a.output.mkdir(parents=True,exist_ok=True)
 rows=[];chains=[]
 for p in csv.DictReader(a.plan.open()):
  if p['fault_profile']!='selective':continue
  raw=a.raw_root/p['run_id'];g=selected(raw/'logs/coordination_gcs.jsonl',['grant_issued','gcs_send','link_failure_detected'])
  native=subprocess.check_output(['rg',',fault_(start|end),',str(raw/'logs/native_radio_events.csv')],text=True)
  faults=list(csv.reader(native.splitlines()));F=int(next(r[1] for r in faults if r[3]=='fault_start'))
  detections=[e for e in g if e['event']=='link_failure_detected' and stamp(e)>=F];D=stamp(detections[0]) if detections else None
  selections=[e for e in g if e['event']=='grant_issued' and e['reason']=='selection' and stamp(e)>=F];sel=selections[0] if selections else None
  sends={e['command_id']:e for e in g if e['event']=='gcs_send'}
  scope=(sel['leader_id'],sel['epoch']) if sel else (1,1)
  master_ev=selected(raw/f'logs/coordination_uav{scope[0]}.jsonl',['master_receive','master_forward'])
  master_lookup={(e['event'],e['command_id']):e for e in master_ev}
  oldmax=max((e['valid_until_ns'] for e in g if e['event']=='grant_issued' and sel and stamp(e)<stamp(sel) and e['epoch']<sel['epoch']),default=None)
  all_u=[];admission_count=0;invalid_count=0;duplicates=0
  for i in range(1,6):
   ev=selected(raw/f'logs/coordination_uav{i}.jsonl',['uart_admitted','follower_receive','master_receive','master_forward','leader_activated'])
   alladm=[e for e in ev if e['event']=='uart_admitted'];admission_count+=len(alladm)
   ids=[e['command_id'] for e in alladm];duplicates+=len(ids)-len(set(ids))
   invalid_count+=sum(not(e['valid_from_ns']<=e['uart_write_started_host_monotonic_ns']<=e['uart_write_end_host_monotonic_ns']<e['valid_until_ns'] and 0<=e['uart_write_end_host_monotonic_ns']-e['command_created_host_monotonic_ns']<=250_000_000) for e in alladm)
   admissions=sorted([e for e in ev if e['event']=='uart_admitted' and e.get('target_position_m') is not None and e['command_created_host_monotonic_ns']>=F and (e['leader_id'],e['epoch'])==scope],key=lambda e:e['uart_write_end_host_monotonic_ns'])
   if not admissions:raise RuntimeError((p['run_id'],i,'no eligible target UART'))
   u=admissions[0];cid=u['command_id'];receive=next((e for e in ev if e['event']=='follower_receive' and e['command_id']==cid),None)
   act=next((e for e in ev if e['event']=='leader_activated' and (e['leader_id'],e['epoch'])==scope),None)
   master=master_lookup.get(('master_receive',cid));forward=master_lookup.get(('master_forward',cid))
   assert master and (i==scope[0] or forward)
   for origin in [sends[cid],receive,master]+([forward] if forward else []):
    assert origin and all(origin[k]==u[k] for k in ['epoch','leader_id','grant_sequence','payload_sha256','target_position_m','command_created_host_monotonic_ns'])
   c=dict(run_id=p['run_id'],rng_run=int(p['rng_run']),policy=p['policy'],uav_id=i,command_id=cid,leader_id=u['leader_id'],epoch=u['epoch'],grant_sequence=u['grant_sequence'],payload_sha256=u['payload_sha256'],fault_ns=F,detect_ns=D,selection_event_ns=stamp(sel),scheduled_valid_from_ns=sel['valid_from_ns'] if sel else None,endpoint_activation_event_ns=stamp(act) if sel else None,command_created_ns=u['command_created_host_monotonic_ns'],GCS_send_event_ns=stamp(sends[cid]),master_receive_event_ns=stamp(master),master_forward_event_ns=stamp(forward),delivery_event_ns=stamp(receive),UART_start_ns=u['uart_write_started_host_monotonic_ns'],UART_end_ns=u['uart_write_end_host_monotonic_ns'])
   c['activation_jitter_s']=delta(c['endpoint_activation_event_ns'],c['scheduled_valid_from_ns'])
   c['T_UART_individual_s']=delta(c['UART_end_ns'],F);chains.append(c);all_u.append(c)
  critical=max(all_u,key=lambda c:c['UART_end_ns']);c=critical
  stamps=[c[k] for k in ['fault_ns','detect_ns','selection_event_ns','endpoint_activation_event_ns','delivery_event_ns','UART_end_ns']]
  ordered=all(x is not None for x in stamps) and all(x<=y for x,y in zip(stamps,stamps[1:]))
  row=dict(run_id=p['run_id'],rng_run=int(p['rng_run']),policy=p['policy'],critical_uav_id=c['uav_id'],critical_command_id=c['command_id'],T_detect_s=delta(D,F),T_select_s=delta(stamp(sel),D),T_lease_s=delta(c['endpoint_activation_event_ns'],stamp(sel)),planned_fence_wait_s=delta(c['scheduled_valid_from_ns'],stamp(sel)),endpoint_activation_jitter_s=delta(c['endpoint_activation_event_ns'],c['scheduled_valid_from_ns']),T_delivery_s=delta(c['delivery_event_ns'],c['endpoint_activation_event_ns']),T_admission_s=delta(c['UART_end_ns'],c['delivery_event_ns']),T_UART_s=delta(c['UART_end_ns'],F),sequence_verified=ordered if sel else None,old_max_issued_expiry_ns=oldmax,guard_s=delta(sel['valid_from_ns'],oldmax) if sel else None,all_UART_admission_count=admission_count,observed_invalid_UART_count=invalid_count,duplicate_UART_CIDs=duplicates)
  row['additive_sum_error_s']=sum(row[k] for k in ['T_detect_s','T_select_s','T_lease_s','T_delivery_s','T_admission_s'])-row['T_UART_s'] if ordered else None
  assert not ordered or abs(row['additive_sum_error_s'])<1e-9
  rows.append(row)
 for name,data in [('recovery_timing.csv',rows),('recovery_chains.csv',chains)]:
  with (a.output/name).open('w',newline='') as f:w=csv.DictWriter(f,fieldnames=list(data[0]),lineterminator='\n');w.writeheader();w.writerows(data)
 fail=[r for r in rows if r['policy']=='failover'];summary={k:dict(n=len(fail),mean=statistics.mean(r[k] for r in fail),median=statistics.median(r[k] for r in fail),min=min(r[k] for r in fail),max=max(r[k] for r in fail),sample_sd=statistics.stdev(r[k] for r in fail)) for k in ['T_detect_s','T_select_s','T_lease_s','T_delivery_s','T_admission_s','T_UART_s']}
 (a.output/'recovery_summary.json').write_text(json.dumps(dict(measured_source='8e16ec223e895c11891e6e7d7c39dff8cee39cac',audit_script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),audit_base_source=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),command='python3 reports/ispras/tools/recovery_timing.py --raw-root '+str(a.raw_root)+' --output '+str(a.output),definition='T_lease uses observed activation at the critical endpoint; T_delivery includes refresh/issuer wait. delivery is application follower_receive, not PHY arrival. Log timestamp jitter remains included.',summary=summary),indent=2)+'\n')
 colors=['#888','#c49b35','#6a83c5','#258c86','#c95b5b'];keys=['T_detect_s','T_select_s','T_lease_s','T_delivery_s','T_admission_s'];svg=['<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="420" viewBox="0 0 1000 420">','<rect width="1000" height="420" fill="white"/>','<g font-family="sans-serif" font-size="14">','<text x="30" y="25">Observed critical-command timeline; main selective failover, n=5</text>']
 for j,r in enumerate(sorted(fail,key=lambda r:r['rng_run'])):
  y=80+j*45;svg.append(f'<text x="10" y="{y+18}">Run {r["rng_run"]}</text>');left=0
  for key,color in zip(keys,colors):
   value=r[key];svg.append(f'<rect x="{100+left*200}" y="{y}" width="{max(value*200,.3)}" height="24" fill="{color}"><title>{key}: {value:.9f}s</title></rect>');left+=value
  svg.append(f'<text x="{110+left*200}" y="{y+18}">{left:.6f}s</text>')
 for i in range(5):svg.extend([f'<line x1="{100+i*200}" y1="70" x2="{100+i*200}" y2="300" stroke="#ddd"/>',f'<text x="{100+i*200}" y="325">{i}s</text>'])
 for j,(key,color) in enumerate(zip(keys,colors)):svg.extend([f'<rect x="{15+j*195}" y="350" width="14" height="14" fill="{color}"/>',f'<text x="{34+j*195}" y="362">{html.escape(key)}</text>'])
 svg.extend(['<text x="15" y="400">Same critical UAV/CID throughout; microscopic select/UART segments have minimum visible width.</text>','</g></svg>']);(a.output/'recovery_timeline.svg').write_text('\n'.join(svg)+'\n');print(json.dumps(summary))
if __name__=='__main__':main()
