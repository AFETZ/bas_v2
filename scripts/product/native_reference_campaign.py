#!/usr/bin/env python3
"""Bounded radio-only scaling and explicit propagation comparison, exact native path."""
import argparse,json,subprocess,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--run-dir',type=Path,required=True);a=p.parse_args();a.run_dir.mkdir(parents=True,exist_ok=False)
 binary=ROOT/'.external/ns-3-sionna-native/build/scratch/ns3.48-upstream-sionna-wifi-smoke-default'
 cases=[]
 for n,moving in [(1,False),(1,True),(5,False),(5,True),(16,False)]:
  cases.append((f'radio_{n}_{"moving" if moving else "stationary"}',dict(staCount=n,velocityY=1 if moving else 0,simulationSeconds=8,offeredPackets=100,distanceM=15)))
 for d in (500,1000,2000):
  for profile in ('sionna','friis'):
   cases.append((f'{profile}_{d}m',dict(propagationProfile=profile,distanceM=d,apX=2500,altitudeM=200,simulationSeconds=6,offeredPackets=100,scene=ROOT/'.external/customer_10km/scene.xml')))
 cases.append(('hybrid_far',dict(propagationProfile='hybrid',distanceM=1000,apX=2500,altitudeM=200,simulationSeconds=6,offeredPackets=100,scene=ROOT/'.external/customer_10km/scene.xml')))
 results=[]
 for name,kw in cases:
  directory=a.run_dir/name;directory.mkdir()
  kw.setdefault('scene',ROOT/'.external/cavise_maps/Town01/map/scene.xml')
  command=[str(binary),f'--output={directory}/result.json',*[f'--{k}={v}' for k,v in kw.items()]]
  (directory/'command.json').write_text(json.dumps(command,indent=2)+'\n')
  start=time.monotonic()
  with (directory/'run.log').open('w') as f: run=subprocess.run(command,stdout=f,stderr=subprocess.STDOUT,timeout=180)
  result=json.loads((directory/'result.json').read_text()) if (directory/'result.json').exists() else {}
  result.update(name=name,process_exit=run.returncode,wall_duration_s=time.monotonic()-start,scope='radio-only; no SITL or Gazebo dynamics',parameters={k:str(v) for k,v in kw.items()})
  results.append(result);print(json.dumps(result),flush=True)
 (a.run_dir/'summary.json').write_text(json.dumps({'cases':results,'clock':'native discrete-event ns-3; wall_duration is compute cost, not real-time lag'},indent=2)+'\n')
 return 0 if all(r['process_exit']==0 for r in results) else 1
if __name__=='__main__':raise SystemExit(main())
