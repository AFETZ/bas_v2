#!/usr/bin/env python3
"""Secondary table/goal joins; explicitly distinct from primary JSONL replay."""
import argparse,csv,json,statistics
from pathlib import Path
from recompute_metrics import writecsv,exact

def rows(p):return list(csv.DictReader(p.open()))
def main():
 ap=argparse.ArgumentParser();ap.add_argument('--repository',type=Path,required=True);ap.add_argument('--output',type=Path,required=True);a=ap.parse_args();root=a.repository;out=a.output
 result=rows(out/'metrics_runs.csv');joins=[];coverage=[];first_lineages=rows(out/'metrics_goal_lineages.csv')
 for r in result:
  rid=r['run_id'];export=root/'runs/paper-live-20261026/campaign-analysis/runs'/rid/'metrics'
  offers=rows(root/'runs/native-radio-realtime'/rid/'metrics/relay_control_refresh.csv');cmds=rows(export/'relay_commands.csv');goals=rows(export/'relay_goals.csv');admitted={c['command_id']:c for c in cmds if c['admitted_ns']};offered={o['command_id']:o for o in offers if o['command_id']}
  assert set(admitted)<=offered.keys()
  for lineage in first_lineages:
   if lineage['run_id']==rid and lineage['first_completion_CID']:
    primary_offer=offered[lineage['first_completion_CID']]
    assert primary_offer['goal_id']==lineage['goal_id'] and primary_offer['uav_id']==lineage['uav_id'] and primary_offer['epoch']==lineage['epoch']
  admitted_goal={(offered[c]['uav_id'],offered[c]['goal_id']) for c in admitted};sent_goal={(o['uav_id'],o['goal_id']) for o in offers if o['command_id']};all_goal={(o['uav_id'],o['goal_id']) for o in offers};complete={(g['uav_id'],g['goal_id']) for g in goals if g['completed_ns']}
  assert len(offered)==int(r['unique_offered_CIDs']) and len(admitted)==int(r['timely'])
  joins.append(dict(run_id=rid,export_target_admitted_any_window=len(admitted),unique_sent=len(offered),sent_without_any_exported_admission=len(offered)-len(admitted),offered_admitted_after_window=0,source='retained relay_commands.csv matched by exact offered CID; not packet loss classification'))
  coverage.append(dict(run_id=rid,condition=r['condition'],policy=r['policy'],created=int(r['unique_created_goals']),offered=len(all_goal),sent=len(sent_goal),admitted=len(admitted_goal),completed=len(complete),sent_without_admission=len(sent_goal-admitted_goal),offered_without_completion=len(all_goal-complete),source='raw offered CSV + retained command/goal exports; no new pose replay'))
 writecsv(out/'metrics_sent_intersection.csv',joins);writecsv(out/'metrics_goal_coverage.csv',coverage)
 decomp=rows(root/'paper/data/decomposition.csv');lineages={(r['run_id'],r['uav_id']):r for r in rows(out/'metrics_goal_lineages.csv')};means=[]
 for policy in ['fixed','failover']:
  chosen=[r for r in decomp if r['group']=='all_five' and r['policy']==policy];assert len(chosen)==5
  for c in chosen:
   r=lineages[c['run_id'],c['critical_uav_id']];assert r['goal_id']==c['goal_id'] and r['first_completion_CID']==c['completion_refresh_CID']
  means.append(dict(policy=policy,n=5,**{k:statistics.fmean(float(r[k]) for r in chosen) for k in ['fault_to_goal_creation_s','goal_creation_to_first_command_creation_s','first_command_creation_to_UART_s','goal_creation_to_first_UART_s','first_UART_to_completion_s','fault_to_completion_s']},source='retained independent decomposition CSV; selected CID/goal identity checked against new primary completion replay'))
 writecsv(out/'metrics_table2.csv',means)
 sensitivity=rows(root/'paper/data/sensitivity_runs.csv');cells=[];points=[]
 for duration in [.5,2,10]:
  for policy in ['fixed','failover']:
   rr=[r for r in sensitivity if float(r['outage_duration_s'])==duration and r['policy']==policy];assert len(rr)==3 and all(r['network_eligible']=='True' for r in rr)
   cells.append(dict(nominal_duration_s=duration,policy=policy,n=3,completed=sum(r['run_status']=='completed' for r in rr),failed=sum(r['run_status']=='failed' for r in rr),timely_per_offered_mean=statistics.fmean(float(r['timely_per_offered']) for r in rr),T_UART_mean=statistics.fmean(float(r['common_UART_recovery_s']) for r in rr),T_goal_mean=statistics.fmean(float(r['physical_new_goal_recovery_s']) for r in rr),source='retained separate18 export, not a new primary replay'))
  for metric in ['common_UART_recovery_s','physical_new_goal_recovery_s','timely_per_offered']:
   diffs=[]
   for repeat in [1,2,3]:
    pair={r['policy']:float(r[metric]) for r in sensitivity if float(r['outage_duration_s'])==duration and int(r['repeat'])==repeat};diffs.append(pair['failover']-pair['fixed'])
   mean,low,high=exact(diffs);points.append(dict(nominal_duration_s=duration,metric=metric,n_pairs=3,mean_difference=mean,interval_low=low,interval_high=high,exact_resamples=27))
 writecsv(out/'metrics_sensitivity_cells.csv',cells);writecsv(out/'metrics_sensitivity_intervals.csv',points)
 print(json.dumps(dict(goal_counts={k:sum(r[k] for r in coverage) for k in ['created','offered','sent','admitted','completed','sent_without_admission','offered_without_completion']},unadmitted_CIDs=sum(r['sent_without_any_exported_admission'] for r in joins),decomposition=means)))
if __name__=='__main__':main()
