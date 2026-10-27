#!/usr/bin/env python3
"""After sensitivity18: bounded grounded observation with upstream light GUI."""
import json,os,re,shutil,subprocess,time
from pathlib import Path
from PIL import ImageGrab
ROOT=Path('/home/bas/bas_v2-paper-live');BASE=ROOT/'runs/paper-followup-20261027'
OUT=BASE/'gui-overhead-07';RUN='paper-gui-overhead-07';VIEWER='bas-paper-gui-minimal-viewer'
RAW=ROOT/'runs/native-radio-realtime'/RUN;OUT.mkdir(exist_ok=False)
env={k:v for k,v in os.environ.items() if not k.startswith('BAS_')}
env.update(json.loads((ROOT/'runs/paper-live-20261026/campaign/freeze.json').read_text())['base_environment'])
env.update(BAS_NATIVE_FIVE_RUN_ID=RUN,BAS_NATIVE_FIVE_GUI='0',BAS_NATIVE_COORDINATION_POLICY='failover',BAS_NATIVE_FAULT_PROFILE='none')
events=(OUT/'controller.jsonl').open('x',buffering=1)
def emit(event,**fields):
 r=dict(event=event,host_monotonic_ns=time.monotonic_ns(),**fields);events.write(json.dumps(r)+'\n');print(json.dumps(r),flush=True);return r
def state():
 p=RAW/'logs/node_state.jsonl'
 if not p.exists():return None
 with p.open('rb') as f:f.seek(max(0,p.stat().st_size-65536));lines=f.read().splitlines()
 for l in reversed(lines):
  try:return json.loads(l)
  except json.JSONDecodeError:pass
def window_info():
 text=subprocess.check_output(['xwininfo','-root','-tree'],text=True)
 for l in text.splitlines():
  if '"gz-sim-gui"' not in l:continue
  m=re.search(r'(0x[0-9a-f]+).*? (\d+)x(\d+)\+(-?\d+)\+(-?\d+)\s+\+(-?\d+)\+(-?\d+)',l)
  if m:return m.groups()
def shot(name):
 info=window_info()
 if not info:raise RuntimeError('actual GUI window unavailable')
 wid,w,h,_,_,x,y=info;w,h,x,y=map(int,(w,h,x,y))
 path=OUT/f'{name}.png';ImageGrab.grab(bbox=(x,y,x+w,y+h),xdisplay=env.get('DISPLAY',':0')).save(path)
 emit('actual_GUI_screenshot',path=str(path),window_id=wid,width=w,height=h,source_sim_time_s=state()['source_sim_time_s'])
argv=['make','operator','DEMO_SCENARIO=town01','DEMO_GUI=0','OPERATOR_DURATION=300']
manifest=dict(argv=argv,cwd=str(ROOT),environment={k:v for k,v in env.items() if k.startswith('BAS_')},
 purpose='Separate grounded60wall-s GUI observation, not sensitivity18; no arming/flight/parameter dump; simulator limits unchanged',
 head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip())
(OUT/'command.json').write_text(json.dumps(manifest,indent=2)+'\n');(OUT/'controller-source.py').write_bytes(Path(__file__).read_bytes())
shutil.copy2(BASE/'gui-grounded-overhead.config',OUT/'actual-gui.config')
native=subprocess.Popen(argv,cwd=ROOT,env=env,stdout=(OUT/'operator-launch.log').open('x'),stderr=subprocess.STDOUT)
viewer=None;sampler=None;probe=None;summary=dict(status='failed',run_id=RUN,grounded=True)
try:
 emit('grounded_native_operator_started',pid=native.pid)
 # Start only after a real world-clock callback has been observed. Record the
 # actual order relative to native radio readiness, rather than assuming it.
 deadline=time.monotonic()+180
 while time.monotonic()<deadline:
  if native.poll() is not None:raise RuntimeError('native stand exited during startup')
  s=state()
  if s and s.get('source_sim_time_s') is not None:break
  time.sleep(.05)
 else:raise TimeoutError('actual source clock')
 command=list(json.loads((BASE/'hook-gui-pilot/viewer-effective-config.json').read_text())['argv'])
 command[2:2]=['--user','0:0','--ulimit','core=0','--cpus','0.75']
 command=[VIEWER if a=='bas-paper-gui-viewer' else f'GZ_PARTITION=native_five_{RUN.replace("-","_")}' if a.startswith('GZ_PARTITION=') else a for a in command]
 command[-1]+=' --gui-config /workspace/multiagent_simulation/runs/paper-followup-20261027/gui-overhead-07/actual-gui.config'
 (OUT/'viewer-effective-config.json').write_text(json.dumps(dict(argv=command,requested_cpu_quota_cores=.75,config='actual-gui.config',real_clock_before_GUI=s['source_sim_time_s']),indent=2)+'\n')
 viewer=subprocess.Popen(command,cwd=ROOT,stdout=(OUT/'viewer.log').open('x'),stderr=subprocess.STDOUT)
 emit('native_GUI_viewer_started',argv=command,source_sim_time_s=s['source_sim_time_s'],radio_ready_already=(RAW/'logs/coordination_gcs.ready').exists())
 deadline=time.monotonic()+90
 while time.monotonic()<deadline:
  if native.poll() is not None:raise RuntimeError('native stand failed before GUI observation')
  if viewer.poll() is not None:raise RuntimeError(f'viewer exited {viewer.returncode}')
  if window_info() and (RAW/'logs/coordination_gcs.ready').exists():break
  time.sleep(.2)
 else:raise TimeoutError('GUI and GCS readiness')
 sampler=subprocess.Popen(['python3','scripts/product/town01_runtime_monitor.py','--output',str(OUT/'gui-host-resources.jsonl'),'--stop-file',str(OUT/'resource-stop')],cwd=ROOT,stdout=(OUT/'resource-sampler.log').open('x'),stderr=subprocess.STDOUT)
 # No late resize or synthetic marker. The actual configured viewer is captured.
 time.sleep(45)
 for name in ('bas-v2-native-radio-five-uav',VIEWER):
  r=subprocess.run(['docker','top',name,'-eo','pid,ppid,args'],capture_output=True,text=True);(OUT/(name+'-processes.txt')).write_text(r.stdout+r.stderr)
 s=state();start=emit('GUI_measurement_start',source_sim_time_s=s['source_sim_time_s']);shot('town01-five-grounded')
 command=['docker','exec','bas-v2-native-radio-five-uav','ip','netns','exec','ams-gcs','env','PYTHONPATH=/home/ubuntu/.local/lib/python3.10/site-packages','python3',
 '/workspace/multiagent_simulation/runs/paper-live-20261026/operator_probe.py',f'/workspace/multiagent_simulation/runs/native-radio-realtime/{RUN}/metrics/GUI_grounded_operator_probe.json']
 (OUT/'probe-command.json').write_text(json.dumps(command,indent=2)+'\n');probe=subprocess.Popen(command,stdout=(OUT/'probe.log').open('x'),stderr=subprocess.STDOUT)
 while time.monotonic_ns()-start['host_monotonic_ns']<60_000_000_000:
  if native.poll() is not None:raise RuntimeError('native stand exited within GUI window')
  if viewer.poll() is not None:raise RuntimeError('viewer exited within GUI window')
  s=state()
  if not s or time.monotonic_ns()-s['published_monotonic_ns']>500_000_000:raise RuntimeError('actual pose publication stale')
  time.sleep(.1)
 emit('GUI_measurement_end',source_sim_time_s=state()['source_sim_time_s']);shot('town01-five-grounded-end')
 summary.update(status='completed_pending_independent_review',probe_returncode=probe.wait(timeout=5))
except Exception as e:
 summary['error']=f'{type(e).__name__}: {e}';emit('grounded_GUI_probe_failed',reason=summary['error'])
finally:
 if viewer is not None and viewer.poll() is None:
  r=subprocess.run(['docker','stop','--timeout','10',VIEWER],capture_output=True,text=True);emit('owned_viewer_stopped',returncode=r.returncode,stdout=r.stdout,stderr=r.stderr);viewer.wait(timeout=20)
 (OUT/'resource-stop').touch()
 if sampler is not None:sampler.wait(timeout=10)
 r=subprocess.run(['docker','stop','--timeout','120','bas-v2-native-radio-five-uav'],capture_output=True,text=True);emit('owned_native_stopped',returncode=r.returncode,stdout=r.stdout,stderr=r.stderr)
 summary['launcher_returncode']=native.wait(timeout=140)
 if probe is not None and probe.poll() is None:probe.wait(timeout=10)
 (OUT/'summary.json').write_text(json.dumps(summary,indent=2)+'\n');events.close()
raise SystemExit(0 if summary['status'].startswith('completed') else 1)
