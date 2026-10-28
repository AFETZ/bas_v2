#!/usr/bin/env python3
"""Measured vertical diagnostic timelines, not a city/NLOS experiment."""
import csv
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
ROOT=Path(__file__).resolve().parents[3];OUT=ROOT/'reports/ispras/data'
rows=list(csv.DictReader((OUT/'candidate_runs.csv').open()))
fig,axes=plt.subplots(3,1,figsize=(10,7),constrained_layout=True)
for ax,duration in zip(axes,[5,10,30]):
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
fig.suptitle('UAV4 direct replies unavailable: vertical Friis/Matrix diagnostics\nSix completed windows; two initial preflight failures retained separately',fontsize=11)
for ext in ('png','svg','pdf'):fig.savefig(OUT/f'candidate_timeline.{ext}',dpi=250)
