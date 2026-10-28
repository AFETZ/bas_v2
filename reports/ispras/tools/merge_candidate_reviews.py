#!/usr/bin/env python3
"""Reuse unchanged six-run review and append separately reviewed2s pair."""
import json
from pathlib import Path
OUT=Path(__file__).resolve().parents[3]/'reports/ispras/data'
a=json.loads((OUT/'candidate_independent_review.json').read_text());b=json.loads((OUT/'candidate2_independent_review.json').read_text());a['records']+=b['records']
assert len(a['records'])==len({r['run_id'] for r in a['records']})==8
a['scope']='Eight engineering probes; previous6 retained plus additional2s pair, separate plan/source in candidate_two_second_plan.json'
(OUT/'candidate8_independent_review.json').write_text(json.dumps(a,indent=2)+'\n')
