#!/usr/bin/env python3
"""Measured vertical diagnostic timelines, not a city/NLOS experiment."""
import argparse,csv
ap=argparse.ArgumentParser();ap.add_argument("--prefix",default="candidate");prefix=ap.parse_args().prefix
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
ROOT=Path(__file__).resolve().parents[3];OUT=ROOT/'reports/ispras/data'
rows=list(csv.DictReader((OUT/(prefix+'_runs.csv')).open()))
durations=sorted({float(r["nominal_fault_s"]) for r in rows})
fig,axes=plt.subplots(len(durations),1,figsize=(10,2.3*len(durations)),constrained_layout=True)
for ax,duration in zip(axes,durations):
 for y,policy,color in [(0,'fixed','C0'),(1,'failover','C1')]:
  r=next(x for x in rows if float(x['nominal_fault_s'])==duration and x['policy']==policy)
  ax.barh(y,float(r['actual_RF_s']),left=0,height=.3,color='gray',alpha=.2)
  ax.scatter(float(r['T_UART_s']),y,marker='o',color=color,label='UART' if y==0 else None)
  ax.scatter(float(r['T_goal_s']),y,marker='*',s=95,color=color,label='Physical goal' if y==0 else None)
  if r['selection_after_fault_s']:
   ax.scatter(float(r['selection_after_fault_s']),y,marker='|',s=120,color='black',label='Selection')
   ax.scatter(float(r['valid_from_after_fault_s']),y,marker='v',color='black',label='New lease valid_from')
   ax.annotate('UAV'+r['new_leader']+' / epoch'+r['new_epoch'],(float(r['valid_from_after_fault_s']),y),xytext=(5,10),textcoords='offset points',fontsize=8)
 ax.set(yticks=[0,1],yticklabels=['fixed','failover'],xlim=(-.2,60),ylim=(-.55,1.6),title=f'Nominal fault {duration}s; one paired repeat; shading: actual RF fault')
 ax.grid(axis='x',alpha=.25)
axes[0].legend(loc='upper right',fontsize=8,ncol=2)
axes[-1].set_xlabel('Host seconds since actual native fault_start')
fig.suptitle(f'UAV4 direct replies unavailable: vertical Friis/Matrix diagnostics\n{len(rows)} completed windows; two initial preflight failures retained separately',fontsize=11)
for ext in ('png','svg','pdf'):fig.savefig(OUT/f'{prefix}_timeline.{ext}',dpi=250)
