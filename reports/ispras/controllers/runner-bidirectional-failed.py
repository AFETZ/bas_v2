#!/usr/bin/env python3
"""Separate engineering pair: direct GCS<->UAV4 UDP firewall, not RF shading.
Invoke from integration under /tmp/bas-paper-runtime.lock. Never retries raw IDs.
"""
import json,os,subprocess,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3]
OUT=ROOT/'runs/ispras-audit-20261028';OUT.mkdir(exist_ok=True)
BASE=json.loads((ROOT/'runs/paper-live-20261026/campaign/freeze.json').read_text())['base_environment']
records=[]
for policy in ('fixed','failover'):
 rid=f'paper-candidate4-unavailable-{policy}-20261028'
 raw=ROOT/'runs/native-radio-realtime'/rid
 if raw.exists():raise SystemExit('Existing raw ID: '+rid)
 env={k:v for k,v in os.environ.items() if not k.startswith('BAS_')};env.update(BASE)
 env.update(BAS_NATIVE_FIVE_RUN_ID=rid,BAS_NATIVE_COORDINATION_POLICY=policy,BAS_NATIVE_FAULT_PROFILE='selective',BAS_NATIVE_EXPERIMENT_PROFILE='main',BAS_NATIVE_EXPERIMENT_PILOT='1',BAS_NATIVE_EXPERIMENT_DURATION_S='90',BAS_NATIVE_FAULT_START_S='30',BAS_NATIVE_FAULT_END_S='40',BAS_NATIVE_RNG_SEED='1',BAS_NATIVE_RNG_RUN='27',BAS_NATIVE_PARAMETER_SNAPSHOTS='0')
 record=dict(run_id=rid,policy=policy,source=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),environment={k:v for k,v in env.items() if k.startswith('BAS_')},argv=['./network/ns3/run_native_radio_five_uav.sh'],events=[])
 def event(kind,**kw):
  r=dict(event=kind,host_monotonic_ns=time.monotonic_ns(),**kw);record['events'].append(r);(OUT/(rid+'-controller.json')).write_text(json.dumps(record,indent=2)+'\n');print(json.dumps(r),flush=True)
 p=subprocess.Popen(record['argv'],cwd=ROOT,env=env,stdout=(OUT/(rid+'.log')).open('x'),stderr=subprocess.STDOUT)
 try:
  deadline=time.monotonic()+240
  while not (raw/'logs/coordination_gcs.ready').exists():
   if p.poll() is not None:raise RuntimeError('stand exited before readiness')
   if time.monotonic()>deadline:raise TimeoutError('GCS readiness')
   time.sleep(.1)
  # Only direct UDP to/from UAV4 is blocked, in the GCS namespace. Forwarded
  # follower4 commands still arrive over ns-3 from the chosen relay.
  for chain,flag in [('OUTPUT','-d'),('INPUT','-s')]:
   argv=['docker','exec','bas-v2-native-radio-five-uav','ip','netns','exec','ams-gcs','iptables','-I',chain,'1','-p','udp',flag,'10.71.4.10','-j','DROP']
   r=subprocess.run(argv,capture_output=True,text=True);event('firewall_rule',argv=argv,returncode=r.returncode,stdout=r.stdout,stderr=r.stderr)
   if r.returncode:raise RuntimeError('firewall injection failed')
  event('candidate4_direct_GCS_UDP_blocked')
  code=p.wait(timeout=480);event('launcher_exited',returncode=code)
  record['returncode']=code
 finally:
  if p.poll() is None:
   subprocess.run(['docker','stop','--timeout','120','bas-v2-native-radio-five-uav'],capture_output=True);p.wait(timeout=150)
  (OUT/(rid+'-controller.json')).write_text(json.dumps(record,indent=2)+'\n')
 records.append(record)
(OUT/'candidate-pair-controller.json').write_text(json.dumps(records,indent=2)+'\n')
