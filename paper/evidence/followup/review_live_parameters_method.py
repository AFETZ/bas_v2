#!/usr/bin/env python3
"""One-off independent read-only review; run with pinned pymavlink image."""
import argparse
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path
import struct
from pymavlink import mavutil

parser=argparse.ArgumentParser()
parser.add_argument('--raw-root',type=Path,required=True)
parser.add_argument('--run-id',required=True)
args=parser.parse_args()
base=args.raw_root/args.run_id/'configuration'
result={'run_id':args.run_id,'method':'Independent upstream pymavlink tlog decode + exact frame comparison to JSONL; no exporter table code reused', 'stages':{}}
for stage in ('before','after'):
    root=base/('parameters-'+stage)
    if not (root/'summary.json').exists():
        result['stages'][stage]={'status':'not_recorded_or_not_finished'}
        continue
    summary=json.loads((root/'summary.json').read_text())
    csvrows=list(csv.DictReader((root/'parameters.csv').open()))
    output={'summary_status':summary.get('status'),'run_id_matches':summary.get('run_id')==args.run_id,
        'duration_wall_s':(summary['finished_host_monotonic_ns']-summary['started_host_monotonic_ns'])/1e9,'components':[]}
    for sid in range(1,6):
        component=next((c for c in summary['components'] if c['system_id']==sid and c['component_id']==1),None)
        if component is None:
            output['components'].append({'system_id':sid,'status':'not_recorded'})
            continue
        events=[json.loads(line) for line in (root/f'uav{sid}.jsonl').open()]
        jsonframes=Counter(bytes.fromhex(e['frame_hex']) for e in events if e['event']=='frame_received')
        log=mavutil.mavlink_connection(str(root/f'uav{sid}.tlog'),dialect='ardupilotmega')
        frames=[]; counts=set(); indices={}; latest={}; conflicts=[]; params=0; foreign=Counter(); bad=0
        while True:
            m=log.recv_match(blocking=False)
            if m is None:break
            frame=bytes(m.get_msgbuf()); frames.append(frame)
            if m.get_type()=='BAD_DATA':bad+=1;continue
            if (m.get_srcSystem(),m.get_srcComponent())!=(sid,1):
                foreign[f'{m.get_srcSystem()}:{m.get_srcComponent()}']+=1;continue
            if m.get_type()!='PARAM_VALUE':continue
            params+=1; name=str(m.param_id).split('\0',1)[0]
            count,index=int(m.param_count),int(m.param_index);counts.add(count)
            if index!=65535:
                if index in indices and indices[index]!=name:conflicts.append([index,indices[index],name])
                if not 0<=index<count:conflicts.append(['invalid_index',index,count])
                indices[index]=name
            offset=10 if frame[0]==253 else 6
            latest[name]={'wire_value_hex':frame[offset:offset+4].hex(),'param_type':int(m.param_type),'value':float(m.param_value)}
        log.close()
        complete=(len(counts)==1 and set(indices)==set(range(next(iter(counts)))) and len(set(indices.values()))==len(indices) and not conflicts)
        exported={r['param_id']:r for r in component['parameters']}
        discrepancies=[]
        for name,row in latest.items():
            saved=exported.get(name)
            if saved is None or row['wire_value_hex']!=saved['wire_value_hex'] or row['param_type']!=saved['param_type'] or row['value']!=float(json.loads(saved['value_json'])):
                discrepancies.append(name)
        csv_for=[r for r in csvrows if (int(r['system_id']),int(r['component_id']))==(sid,1)]
        csv_equal=all(r['param_id'] in exported and all(str(exported[r['param_id']][k])==str(r[k]) for k in ('wire_value_hex','param_type','value_json','param_index','param_count')) for r in csv_for)
        required={name:float(json.loads(exported[name]['value_json'])) if name in exported else None for name in ('ARMING_SKIPCHK','COMPASS_USE','SIM_SPEEDUP')}
        output['components'].append({'system_id':sid,'component_id':1,'param_counts':sorted(counts),'distinct_valid_indices':len(indices),
            'missing_indices':sorted(set(range(next(iter(counts))))-indices.keys()) if len(counts)==1 else None,
            'independent_index_coverage_complete':complete,'params_decoded':params,'bad_data_frames':bad,'foreign_frames':dict(foreign),
            'index_name_conflicts':conflicts,'tlog_frame_count':len(frames),'jsonl_frame_count':sum(jsonframes.values()),
            'tlog_frames_exactly_equal_JSONL_multiset':Counter(frames)==jsonframes,
            'raw_exactly_equals_all_tlog_frames_in_order':(root/f'uav{sid}.raw').read_bytes()==b''.join(frames),
            'last_value_discrepancies':discrepancies,'csv_count':len(csv_for),'csv_matches_summary_values':csv_equal,
            'requests_by_type':dict(Counter(e['request_type'] for e in events if e['event']=='request_sent')),
            'summary_value_changes':len(component['value_changes_during_readout']),'observed_check_parameters':required,
            'bytes':{ext:(root/f'uav{sid}.{ext}').stat().st_size for ext in ('tlog','raw','jsonl')}})
    result['stages'][stage]=output
if all((base/('parameters-'+s)/'summary.json').exists() for s in ('before','after')):
    before,after=(json.loads((base/('parameters-'+s)/'summary.json').read_text()) for s in ('before','after'))
    old={(c['system_id'],c['component_id'],p['param_id']):p for c in before['components'] for p in c['parameters']}
    new={(c['system_id'],c['component_id'],p['param_id']):p for c in after['components'] for p in c['parameters']}
    changes=[]
    for key in sorted(old.keys()|new.keys()):
        x,y=old.get(key),new.get(key)
        if x is None or y is None or any(x[f]!=y[f] for f in ('wire_value_hex','param_type','param_index')):
            changes.append({'system_id':key[0],'param_id':key[2],'before':json.loads(x['value_json']) if x else None,'after':json.loads(y['value_json']) if y else None})
    result['independent_before_after_changes']=changes
    result['exporter_change_names_match_independent']=set((x['system_id'],x['param_id']) for x in changes)==set((x['system_id'],x['param_id']) for x in after['before_after_changes'])
print(json.dumps(result,indent=2,allow_nan=False))
