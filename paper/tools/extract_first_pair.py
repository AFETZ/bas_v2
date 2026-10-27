#!/usr/bin/env python3
"""Reuse the original evidence loader; export anonymous figure inputs only.

This does not run a simulator or recalculate physical completion. The evidence
bundle supplies the original plot_first_planned_pair.py and raw/full exports.
"""
from pathlib import Path
import argparse
import importlib.util
import hashlib
import json
import gzip
import math

def clean(value):
    if isinstance(value,float) and not math.isfinite(value):return None
    if isinstance(value,dict):return {k:clean(v) for k,v in value.items()}
    if isinstance(value,(tuple,list)):return [clean(v) for v in value]
    return value

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--evidence-root',type=Path,required=True)
    p.add_argument('--raw-root',type=Path,required=True)
    p.add_argument('--plan',type=Path,required=True)
    a=p.parse_args()
    source=a.evidence_root/'plot_first_planned_pair.py'
    spec=importlib.util.spec_from_file_location('original_pair_evidence_loader',source)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    selected=module.select_first_pair(a.plan)
    runs={policy:module.load_run(a.evidence_root/'campaign-analysis',a.raw_root,selected[policy])
          for policy in ('fixed','failover')}
    out=Path(__file__).resolve().parents[1]/'data'
    with gzip.open(out/'first_pair.json.gz','wt') as f:json.dump(clean(runs),f,allow_nan=False,separators=(',',':'))
    (out/'first_pair_provenance.json').write_text(json.dumps({
        'evidence_loader':'plot_first_planned_pair.py',
        'evidence_loader_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
        'plan_sha256':hashlib.sha256(a.plan.read_bytes()).hexdigest(),
        'selection':'First frozen pair, plan orders 1 and 2; RngSeed1/RngRun4',
        'altitude_datum':'world_ENU_z','clock':'host-monotonic seconds from each measurement start',
        'missing':'None; gap separators preserved; no imputation',
        'target':'Only actual UART write-end points; no active-state interpolation',
        'runs':{k:{x:v for x,v in r.items() if x not in ('actual','targets','refresh')} for k,r in runs.items()}
    },indent=2)+'\n')
