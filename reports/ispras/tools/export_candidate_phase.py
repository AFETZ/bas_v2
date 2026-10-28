#!/usr/bin/env python3
"""Export actual fault/selection/fresh-old-echo order from saved probe raw."""
import csv,json,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3];OUT=ROOT/'reports/ispras/data'
rows=json.loads((OUT/'candidate8_product_analysis.json').read_text());result=[]
for row in rows:
 rid=row['run_id'];a=json.loads((ROOT/'runs/ispras-audit-20261028/candidate-analysis'/rid/'metrics/relay_analysis.json').read_text());F=a['actual_fault']['start_host_monotonic_ns'];E=a['actual_fault']['end_host_monotonic_ns'];grants=[];echo=[]
 for line in subprocess.check_output(['rg','"event":"(grant_issued|gcs_probe_echo_received)"',str(ROOT/'runs/native-radio-realtime'/rid/'logs/coordination_gcs.jsonl')],text=True).splitlines():
  e=json.loads(line)
  if e['event']=='grant_issued' and e['reason']=='selection' and e['host_monotonic_ns']>=F:grants.append(e)
  if e['event']=='gcs_probe_echo_received' and e['uav_id']==1 and e['host_monotonic_ns']>=E and e['probe_sent_host_monotonic_ns']>=F and e['reply_sent_host_monotonic_ns']>=E:echo.append(e)
 g=grants[0] if grants else None;restored=min([e['host_monotonic_ns'] for e in echo],default=None)
 result.append(dict(run_id=rid,actual_RF_duration_s=(E-F)/1e9,selection_after_fault_s=(g['host_monotonic_ns']-F)/1e9 if g else None,new_valid_from_after_fault_s=(g['valid_from_ns']-F)/1e9 if g else None,old_channel_fresh_echo_after_fault_s=(restored-F)/1e9 if restored else None,new_valid_from_after_RFend=g['valid_from_ns']>E if g else None,new_valid_from_after_old_echo=g['valid_from_ns']>restored if g and restored else None))
with (OUT/'candidate8_fault_phase.csv').open('w',newline='') as f:w=csv.DictWriter(f,fieldnames=list(result[0]),lineterminator='\n');w.writeheader();w.writerows(result)
lifecycle=[]
for row in rows:
 rid=row['run_id'];events=[json.loads(line) for line in subprocess.check_output(['rg','takeoff_observed|landing_disarm_observed',str(ROOT/'runs/native-radio-realtime'/rid/'logs/coordination_gcs.jsonl')],text=True).splitlines()]
 take={e['uav_id'] for e in events if e['event']=='takeoff_observed'};land={e['uav_id'] for e in events if e['event']=='landing_disarm_observed'}
 assert take==land==set(range(1,6)),(rid,take,land)
 lifecycle.append({'run_id':rid,'takeoff_observed_uavs':sorted(take),'landing_disarm_observed_uavs':sorted(land)})
(OUT/'candidate8_lifecycle.json').write_text(json.dumps({'method':'Independent count of raw GCS takeoff/landing-disarm observations; physical firstpostfault goals reviewed separately','records':lifecycle},indent=2)+'\n')
