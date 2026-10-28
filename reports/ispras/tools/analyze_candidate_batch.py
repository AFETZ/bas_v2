#!/usr/bin/env python3
"""Read-only export of the six candidate-availability probes, after runtime stops."""
import csv,importlib.util,json,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3]
BASE=ROOT/'runs/ispras-audit-20261028';OUT=ROOT/'reports/ispras/data';OUT.mkdir(exist_ok=True,parents=True)
ids=[f'paper-candidate4-unavailable-{p}-20261028-rx'+suffix for suffix in ('','-d5','-d30') for p in ('fixed','failover')]
path=ROOT/'scripts/product/analyze_relay_experiment.py';spec=importlib.util.spec_from_file_location('candidate_analysis',path);A=importlib.util.module_from_spec(spec);spec.loader.exec_module(A)
rows=[]
for rid in ids:
 raw=ROOT/'runs/native-radio-realtime'/rid
 r=A.analyze_run(raw,BASE/'candidate-analysis'/rid,plots=False,live_export=True)
 rows.append(dict(run_id=rid,status=r['run_status'],nominal_fault_s=r['metadata']['config']['fault_end_s']-r['metadata']['config']['fault_start_s'],policy=r['metadata']['config']['policy'],timely_per_offered=r['rates']['all_five']['timely_per_offered'],T_UART_s=r['control_recovery']['all_five'].get('time_s'),T_goal_s=r['goal_recovery_all_five'].get('time_s'),criteria=r['criteria'],errors=r['data_quality_errors']))
(OUT/'candidate_product_analysis.json').write_text(json.dumps(rows,indent=2)+'\n')
# Independent stdlib+rg pose review is run by the host after this export.
print(json.dumps(rows))
