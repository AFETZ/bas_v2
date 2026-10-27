#!/usr/bin/env python3
"""Sequential small upstream tlog replay, after all18 terminal records exist."""
import json,os,subprocess,sys
from pathlib import Path
os.nice(10)
b=Path(__file__).resolve().parent
rows=json.loads((b/'all18-independent.json').read_text())['records']
assert len(rows)==18 and len({r['run_id'] for r in rows})==18
out=[]
for row in rows:
    argv=[sys.executable,str(b/'review_live_parameters_method.py'),'--raw-root','runs/native-radio-realtime','--run-id',row['run_id']]
    out.append(json.loads(subprocess.check_output(argv,text=True)))
    print('Decoded small parameter tlogs:',row['order'],row['run_id'],file=sys.stderr,flush=True)
print(json.dumps(dict(method='Sequential upstream pymavlink decode of all observed parameter tlogs; no production exporter table code imported',records=out),indent=2))
