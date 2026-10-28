#!/usr/bin/env python3
"""Join two independently calculated endpoints and export engineering probe table."""
import csv,json,math
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3];OUT=ROOT/'reports/ispras/data'
p=json.loads((OUT/'candidate_product_analysis.json').read_text());i=json.loads((OUT/'candidate_independent_review.json').read_text());ind={r['run_id']:r for r in i['records']};rows=[];poses=[]
for r in p:
 q=ind[r['run_id']];a=json.loads((ROOT/'runs/ispras-audit-20261028/candidate-analysis'/r['run_id']/'metrics/relay_analysis.json').read_text())
 assert q['status']==r['status']=='completed' and not r['errors']
 assert len(q['physical_goals'])==5 and len(q['individual_UART'])==5
 assert not q['overlap_violations'] and q['invalid_UART_count']==q['duplicate_UART_CIDs']==q['direct_UAV4_echo_during_fault']==0
 assert abs(q['T_UART_s']-r['T_UART_s'])<1e-9
 assert abs(max(x['T_goal_s'] for x in q['physical_goals'].values())-r['T_goal_s'])<1e-9
 for u,g in q['physical_goals'].items():
  assert g['independent_max_vertical_displacement_m']>=1 and g['dwell_max_speed_mps']<=1 and g['dwell_max_z_error_m']<=1.5 and g['dwell_max_xy_error_m']<=3
  assert g['dwell_observed_source_span_s']>=1.96 and g['dwell_max_source_gap_s']<=.041
  poses.append(dict(run_id=r['run_id'],uav_id=u,**g))
 F=a['actual_fault']['start_host_monotonic_ns'];s=q['selections'][0] if q['selections'] else None
 rows.append(dict(run_id=r['run_id'],status=r['status'],policy=r['policy'],nominal_fault_s=r['nominal_fault_s'],actual_RF_s=q['actual_RF_duration_s'],timely_per_offered=r['timely_per_offered'],T_UART_s=r['T_UART_s'],T_goal_s=r['T_goal_s'],selection_count=len(q['selections']),new_leader=s['leader'] if s else None,new_epoch=s['epoch'] if s else None,selection_after_fault_s=(s['at_ns']-F)/1e9 if s else None,valid_from_after_fault_s=(s['valid_from']-F)/1e9 if s else None,all_UART_count=q['all_UART_count'],invalid_UART_count=q['invalid_UART_count'],direct_UAV4_echo_during_fault=q['direct_UAV4_echo_during_fault'],UART_after_RFend=r['T_UART_s']>q['actual_RF_duration_s'],scope='vertical Friis/Matrix pilot + direct-GCS INPUT filter; no building shadowing'))
for name,data in [('candidate_runs.csv',rows),('candidate_physical.csv',poses)]:
 with (OUT/name).open('w',newline='') as f:w=csv.DictWriter(f,fieldnames=list(data[0]),lineterminator='\n');w.writeheader();w.writerows(data)
summary=dict(completed=len(rows),failed_before_measurement=2,all_UART=sum(r['all_UART_count'] for r in rows),invalid_UART=0,duplicate_CID=0,independent_endpoints_match=True,min_displacement_m=min(r['independent_max_vertical_displacement_m'] for r in poses),min_dwell_span_s=min(r['dwell_observed_source_span_s'] for r in poses),max_dwell_speed_mps=max(r['dwell_max_speed_mps'] for r in poses),max_dwell_gap_s=max(r['dwell_max_source_gap_s'] for r in poses),n_pairs_per_duration=1,confidence_interval=None)
(OUT/'candidate_summary.json').write_text(json.dumps(summary,indent=2)+'\n');print(json.dumps(summary))
