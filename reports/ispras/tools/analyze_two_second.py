#!/usr/bin/env python3
"""Analyze only the additional2s pair; retain existing six-run exports unchanged."""
import importlib.util,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3];OUT=ROOT/'reports/ispras/data';BASE=ROOT/'runs/ispras-audit-20261028'
spec=importlib.util.spec_from_file_location('candidate_analysis',ROOT/'scripts/product/analyze_relay_experiment.py');A=importlib.util.module_from_spec(spec);spec.loader.exec_module(A)
rows=[]
for policy in ('fixed','failover'):
 rid=f'paper-candidate4-unavailable-{policy}-20261028-rx-d2';r=A.analyze_run(ROOT/'runs/native-radio-realtime'/rid,BASE/'candidate-analysis'/rid,plots=False,live_export=True)
 rows.append(dict(run_id=rid,status=r['run_status'],nominal_fault_s=2,policy=policy,timely_per_offered=r['rates']['all_five']['timely_per_offered'],T_UART_s=r['control_recovery']['all_five'].get('time_s'),T_goal_s=r['goal_recovery_all_five'].get('time_s'),criteria=r['criteria'],errors=r['data_quality_errors']))
(OUT/'candidate2_product_analysis.json').write_text(json.dumps(rows,indent=2)+'\n')
(OUT/'candidate8_product_analysis.json').write_text(json.dumps(json.loads((OUT/'candidate_product_analysis.json').read_text())+rows,indent=2)+'\n');print(json.dumps(rows))
