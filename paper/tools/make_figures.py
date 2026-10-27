#!/usr/bin/env python3
"""Render manuscript figures from frozen, copied aggregate/paired CSV only."""
from pathlib import Path
import csv
import json
import gzip
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'figures'
plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 8,
                     'axes.titlesize': 9, 'axes.labelsize': 8,
                     'svg.hashsalt': 'anonymous-uav-paper-20261027',
                     'pdf.fonttype': 42, 'ps.fonttype': 42})

def table(name):
    with (ROOT / 'data' / name).open(newline='') as f:
        return list(csv.DictReader(f))

def save(fig, name):
    for ext in ('png', 'pdf', 'svg'):
        fig.savefig(OUT / f'{name}.{ext}', dpi=400, bbox_inches='tight',
                    metadata={'Creator': 'Anonymous manuscript figure generator'})
    plt.close(fig)

def architecture():
    fig, ax = plt.subplots(figsize=(5.5, 2.8))
    ax.set_xlim(0, 10); ax.set_ylim(0, 6.5); ax.axis('off')
    def box(x,y,w,h,text,colour='#eef2f5'):
        ax.add_patch(FancyBboxPatch((x,y),w,h,boxstyle='round,pad=0.08',
                                   facecolor=colour,edgecolor='#37474f',lw=.8))
        ax.text(x+w/2,y+h/2,text,ha='center',va='center',fontsize=8)
    def arrow(a,b,text='',dashed=False,above=.12):
        ax.add_patch(FancyArrowPatch(a,b,arrowstyle='<->' if not dashed else '->',
            mutation_scale=9,lw=.9,color='#275d85' if not dashed else '#7b6b54',
            linestyle='--' if dashed else '-'))
        if text: ax.text((a[0]+b[0])/2,(a[1]+b[1])/2+above,text,ha='center',fontsize=7)
    box(.2,4.6,2.2,1.15,'НПУ / GCS\nцели + issuer\nepoch / lease','#e0edf7')
    box(3.6,4.6,2.4,1.15,'UAV координатор\nrelay unchanged\nMAVLink bytes','#e0edf7')
    box(7.1,4.6,2.6,1.15,'4 follower\nfinal gate → UART\nArduPilot SITL','#e0edf7')
    arrow((2.48,5.17),(3.52,5.17),'ns-3',above=.15)
    arrow((6.08,5.17),(7.02,5.17),'ns-3',above=.15)
    box(2.75,2.5,5.2,1.05,'Native Wi-Fi ad-hoc / TAP / peer routes\nFriis + explicit RF fault; upstream PHY/MAC')
    arrow((4.8,4.52),(4.8,3.63),'',False)
    box(2.75,.25,5.2,1.05,'Gazebo / DART / 5 physical UAV models\nposes → radio mobility; read-only analysis','#f7eee0')
    box(.2,2.5,1.8,1.05,'Raw / CSV\nfigures\nclock / lineage','#f7eee0')
    arrow((4.8,1.38),(4.8,2.42),'poses',True,.1)
    arrow((2.67,.8),(1.1,2.42),'',True)
    arrow((2.67,3.03),(2.08,3.03),'',True)
    ax.text(5,.0,'One host-monotonic clock; ground truth does not select leader',
            ha='center',va='top',fontsize=7)
    save(fig,'architecture')

def paired():
    rows=table('selective_paired_points.csv')
    metrics=[('timely_per_offered','Своевременно / offered, %',100),
             ('actual_control_recovery_s','T_UART, с',1),
             ('actual_goal_recovery_s','T_goal, с',1)]
    fig,axes=plt.subplots(1,3,figsize=(5.5,2.7))
    colors=plt.get_cmap('tab10').colors
    for ax,(metric,label,scale) in zip(axes,metrics):
        selected=sorted((r for r in rows if r['metric']==metric),key=lambda r:int(r['rng_run']))
        assert len(selected)==5
        for i,r in enumerate(selected):
            offset=(i-2)*.04
            ax.plot([offset,1+offset],[float(r['fixed'])*scale,float(r['failover'])*scale],
                    marker='o',ms=3,lw=.8,color=colors[i],label=f"RngRun {r['rng_run']}")
        ax.set_xticks([0,1],['fixed','failover']);ax.set_ylabel(label)
        ax.grid(axis='y',alpha=.25);ax.set_xlim(-.18,1.18)
    axes[0].set_ylim(60,101)
    h,l=axes[0].get_legend_handles_labels()
    fig.legend(h,l,loc='upper center',ncol=5,fontsize=6.7,bbox_to_anchor=(.5,1.03))
    fig.tight_layout(rect=(0,0,1,.92),w_pad=1)
    save(fig,'paired')

def first_pair():
    from matplotlib.lines import Line2D
    with gzip.open(ROOT/'data/first_pair.json.gz','rt') as f:runs=json.load(f)
    fig,axes=plt.subplots(3,1,figsize=(5.5,4.5),sharex=True,
                          gridspec_kw={'height_ratios':[1,1,.65]})
    colors=plt.get_cmap('tab10').colors[:5]
    for ax,policy in zip(axes[:2],('fixed','failover')):
        run=runs[policy]
        ax.axvspan(*run['fault_s'],color='.5',alpha=.15,hatch='///',zorder=0)
        for i,color in enumerate(colors,1):
            xx,zz=run['actual'][f'uav{i}']
            ax.plot(xx,[np.nan if z is None else z for z in zz],color=color,lw=.8)
            points=run['targets'][f'uav{i}']
            if points:
                tx,tz=zip(*points);ax.scatter(tx,tz,s=5,marker='o',facecolors='none',edgecolors=[color],lw=.4)
        ax.set_title(policy,loc='left',fontsize=8)
        ax.set_ylabel('Мировая ENU z, м');ax.grid(alpha=.2);ax.set_ylim(7,30)
    ax=axes[2]
    for policy,color,marker in [('fixed','#6d4c91','x'),('failover','#007c78','o')]:
        run=runs[policy];points=run['refresh']
        ax.scatter([r['host_seconds'] for r in points],
                   [np.nan if r['fraction'] is None else r['fraction']*100 for r in points],
                   s=8,marker=marker,linewidths=.6,label=policy,
                   **({'facecolors':'none','edgecolors':color} if marker=='o' else {'color':color}))
    ax.set_yticks([0,50,100]);ax.set_ylim(-6,106);ax.set_ylabel('Timely / offered, %')
    ax.set_xlabel('Host-monotonic время от начала окна, с');ax.grid(alpha=.2)
    ax.legend(loc='center right',fontsize=7,ncol=2)
    handles=[Line2D([],[],color=c,label=f'UAV{i}') for i,c in enumerate(colors,1)]
    handles.append(Line2D([],[],color='.3',ls='none',marker='o',markersize=3,
                          markerfacecolor='none',label='UART-цель'))
    fig.legend(handles=handles,loc='upper center',bbox_to_anchor=(.5,1),ncol=6,fontsize=7)
    ax.set_xlim(0,max(r['duration_s'] for r in runs.values()))
    fig.tight_layout(rect=(0,0,1,.96),h_pad=.6)
    save(fig,'first_pair')

def decomposition():
    rows=[r for r in table('decomposition.csv') if r['group']=='all_five']
    keys=['fault_to_goal_creation_s','goal_creation_to_first_UART_s','first_UART_to_completion_s']
    labels=['до создания цели','до первого UART','движение + hold']
    colors=['#d5dce5','#4d91ba','#4a9b79']
    fig,ax=plt.subplots(figsize=(5.5,2.2))
    for y,p in enumerate(['fixed','failover']):
        selected=[r for r in rows if r['policy']==p]
        assert len(selected)==5 and all(r['status']=='observed' for r in selected)
        left=0
        for key,label,color in zip(keys,labels,colors):
            val=float(np.mean([float(r[key]) for r in selected]))
            ax.barh(y,val,left=left,color=color,edgecolor='white',height=.5,label=label if y==0 else None)
            ax.text(left+val/2,y,f'{val:.3f}',ha='center',va='center',fontsize=7,
                    color='black',rotation=0 if val>3 else 90)
            left+=val
        ax.text(left+.6,y,f'= {left:.3f} с',va='center',fontsize=7.5)
    ax.set_yticks([0,1],['fixed','failover']);ax.invert_yaxis()
    ax.set_xlim(0,80);ax.set_xlabel('От фактического RF-отказа, с')
    ax.legend(loc='upper center',bbox_to_anchor=(.5,1.25),ncol=3,fontsize=7)
    ax.grid(axis='x',alpha=.2);fig.tight_layout()
    save(fig,'decomposition')

def sensitivity():
    """Called only for an actual complete 18-row export; missing stays missing."""
    path=ROOT/'data/sensitivity_runs.csv'
    if not path.exists():return
    rows=table('sensitivity_runs.csv')
    assert len(rows)==18,'Do not render an incomplete campaign as 18 results'
    durations=[.5,2,10]
    fig,axes=plt.subplots(2,3,figsize=(5.5,3.9),sharex=True)
    colors=plt.get_cmap('tab10').colors[:3]
    for col,duration in enumerate(durations):
        subset=[r for r in rows if float(r['outage_duration_s'])==duration]
        assert len(subset)==6 and {r['policy'] for r in subset}=={'fixed','failover'}
        repeats=sorted({int(r['repeat']) for r in subset});assert len(repeats)==3
        for offset,repeat,color in zip([-.07,0,.07],repeats,colors):
            pair={r['policy']:r for r in subset if int(r['repeat'])==repeat}
            assert len(pair)==2
            for index,(key,status_key) in enumerate([
                    ('common_UART_recovery_s','common_UART_status'),('timely_per_offered',None)]):
                values=[]
                for x,policy in enumerate(('fixed','failover')):
                    row=pair[policy];raw=row[key]
                    observed=(status_key is None or row[status_key]=='observed')
                    if raw and observed:
                        val=float(raw)*(100 if status_key is None else 1)
                        axes[index,col].scatter(x+offset,val,color=color,s=14,zorder=3)
                        values.append((x+offset,val))
                    elif status_key and row.get('common_UART_censor_time_s'):
                        val=float(row['common_UART_censor_time_s'])
                        axes[index,col].scatter(x+offset,val,marker='^',facecolors='none',edgecolors=[color],s=22,zorder=3)
                    else:
                        axes[index,col].text(x+offset,.03,'пропуск',rotation=90,fontsize=6,
                            ha='center',va='bottom',transform=axes[index,col].get_xaxis_transform())
                if len(values)==2:
                    xx,yy=zip(*values);axes[index,col].plot(xx,yy,color=color,lw=.7,alpha=.8)
        axes[0,col].set_title(f'Номинал / nominal {duration:g} s\nn_pair=3',fontsize=8)
        for ax in axes[:,col]:
            ax.set_xticks([0,1],['fixed','failover']);ax.set_xlim(-.25,1.25);ax.grid(axis='y',alpha=.25)
            ax.tick_params(labelsize=7)
    axes[0,0].set_ylabel('T_UART, с');axes[1,0].set_ylabel('Timely / offered, %')
    fig.text(.5,.005,'Кружок: наблюдение; △: цензурирование; пропуск не равен нулю',ha='center',fontsize=7)
    fig.tight_layout(rect=(0,.035,1,1),w_pad=1)
    save(fig,'sensitivity')

if __name__=='__main__':
    OUT.mkdir(exist_ok=True)
    architecture();first_pair();paired();decomposition();sensitivity()
