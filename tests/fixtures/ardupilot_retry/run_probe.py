#!/usr/bin/env python3
"""Reproduce Gazebo lock-step retry timing using the pinned plugin and a mock controller.

Prepare immutable inputs (no Docker or simulation):
  python tests/fixtures/ardupilot_retry/run_probe.py --output runs/control/retry-demo

Prepare and execute on an otherwise idle Linux runtime host:
  python3 tests/fixtures/ardupilot_retry/run_probe.py --output runs/control/retry-live --execute

The default is prepare-only. Output must not already exist. Execution uses an
isolated network-none container, one Iris, disarmed PWM, and no SITL or RF.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import statistics
import struct
import subprocess
import sys
import time
import shutil

IMAGE='sha256:89d78eff9914b1644a1b01b793612e9ee8b19916c5a7bf5f578ba6d8bfbfafe5'
REPO_ROOT=next((parent for parent in Path(__file__).resolve().parents
                if (parent/'src/multiagent_simulation/models').is_dir()),Path.cwd())
PLUGIN=Path('/workspace/ardu_ws/build/ardupilot_gazebo/libArduPilotPlugin.so')
UPSTREAM=Path('/workspace/ardu_ws/src/ardupilot_gazebo')
PORT=19002
STEP=.001
EXPECTED_PLUGIN_SHA256='ccb83b4cc8c9384404dc01dafa875ff2509e823d8a276fa0365ff080b70ccc13'
EXPECTED_SOURCE_SHA256='e00c3fc98e2ef8e37c9fb6a537c4f6fb599b1e8e3f90885d4916eaa04baac868'
EXPECTED_UPSTREAM_COMMIT='cc0290d964dfa373531963a8fc39093a0836af0a'

def write(path,obj):
    path.write_text(json.dumps(obj,indent=2)+'\n')

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def git(path,*args):
    result=subprocess.run(['git','-c',f'safe.directory={path}','-C',str(path),*args],text=True,capture_output=True)
    return result.stdout.strip() if result.returncode==0 else None

def prepare(repo_root,out):
    out.mkdir(parents=True,exist_ok=False)
    dest=out/'inputs'
    dest.mkdir()
    models=repo_root/'src/multiagent_simulation/models'
    for name in ('iris','iris_radio_headless'):
        shutil.copytree(models/name,dest/name)
    write(dest/'provenance.json',dict(source_commit=git(repo_root,'rev-parse','HEAD'),
        files=[dict(path=p.relative_to(dest).as_posix(),size=p.stat().st_size,sha256=sha(p))
               for p in sorted(dest.rglob('*')) if p.is_file()],
        image=IMAGE,hardware=False,rf=False,sitl=False))
    model=(dest/'iris_radio_headless/model.sdf').read_text()
    assert len(re.findall(r'<connectionTimeoutMaxCount>\d+</connectionTimeoutMaxCount>',model))==1
    assert '<lock_step>1</lock_step>' in model
    for name in ('run_probe.py','verify.py'):
        shutil.copyfile(Path(__file__).parent/name,out/name)
    (out/'entry.sh').write_text('source /opt/ros/humble/setup.bash\nsource /workspace/ardu_ws/install/setup.bash\nexport GZ_VERSION=harmonic\nexec python3 /probe/run_probe.py --inside --output /probe\n')
    write(out/'preparation.json',dict(status='prepared_not_executed',image=IMAGE,
        expected_plugin_sha256=EXPECTED_PLUGIN_SHA256,expected_source_sha256=EXPECTED_SOURCE_SHA256,
        expected_upstream_commit=EXPECTED_UPSTREAM_COMMIT,case_order=[5,0,0,5],trials_per_case=12,
        no_host_network=True,no_host_pid=True,no_privileged=True,hardware=False,rf=False,sitl=False,
        fixture_sha256=sha(out/'run_probe.py'),verifier_sha256=sha(out/'verify.py')))

def loaded_plugins():
    loaded=[]
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():continue
        try:lines=(entry/'maps').read_text().splitlines()
        except (OSError,ProcessLookupError):continue
        paths=sorted({line.split()[-1] for line in lines if 'libArduPilotPlugin.so' in line})
        for name in paths:
            path=Path(name)
            loaded.append(dict(pid=int(entry.name),path=name,sha256=sha(path)))
    return loaded

def stop(process):
    for sig,seconds in ((signal.SIGINT,8),(signal.SIGTERM,3),(signal.SIGKILL,3)):
        if process.poll() is not None:return
        try:os.killpg(process.pid,sig)
        except ProcessLookupError:return
        try:process.wait(timeout=seconds);return
        except subprocess.TimeoutExpired:pass
    raise RuntimeError('owned Gazebo process group did not exit')

def case(out,count,sequence):
    out.mkdir()
    model=(Path('/inputs/iris_radio_headless/model.sdf')).read_text()
    assert len(re.findall(r'<connectionTimeoutMaxCount>\d+</connectionTimeoutMaxCount>',model))==1
    assert '<lock_step>1</lock_step>' in model
    model=model.replace('name="iris_radio_headless"','name="iris_probe"',1)
    model=model.replace('<fdm_port_in>9002</fdm_port_in>',f'<fdm_port_in>{PORT}</fdm_port_in>',1)
    model=re.sub(r'<connectionTimeoutMaxCount>\d+</connectionTimeoutMaxCount>',
                 f'<connectionTimeoutMaxCount>{count}</connectionTimeoutMaxCount>',model,count=1)
    target=out/'models/iris_probe';target.mkdir(parents=True)
    (target/'model.sdf').write_text(model)
    (target/'model.config').write_text('<model><name>iris_probe</name><version>1.0</version><sdf version="1.9">model.sdf</sdf></model>')
    world='''<sdf version="1.9"><world name="retry_probe">
<physics name="1ms" type="ignore"><max_step_size>0.001</max_step_size><real_time_factor>1</real_time_factor></physics>
<gravity>0 0 -9.8</gravity>
<plugin filename="gz-sim-physics-system" name="gz::sim::systems::Physics"/>
<plugin filename="gz-sim-imu-system" name="gz::sim::systems::Imu"/>
<plugin filename="gz-sim-user-commands-system" name="gz::sim::systems::UserCommands"/>
<include><uri>model://iris_probe</uri><pose>0 0 10 0 0 0</pose></include>
</world></sdf>'''
    (out/'world.sdf').write_text(world)
    env=os.environ.copy()
    env['GZ_SIM_RESOURCE_PATH']=f'{out}/models:/inputs:'+env.get('GZ_SIM_RESOURCE_PATH','')
    env['GZ_SIM_SYSTEM_PLUGIN_PATH']=str(PLUGIN.parent)+':'+env.get('GZ_SIM_SYSTEM_PLUGIN_PATH','')
    env['GZ_PARTITION']=f'bas-retry-probe-{os.getpid()}-{sequence}'
    env['GZ_IP']='127.0.0.1'
    events=[];trials=[];frame=1
    sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    sock.bind(('127.0.0.1',0));sock.settimeout(.1)
    packet=lambda n:struct.pack('<HHI16H',18458,1000,n,*([1000]*16))
    assert len(packet(1))==40
    def send(n,label='servo_transmit'):
        raw=packet(n);now=time.monotonic_ns()
        sock.sendto(raw,('127.0.0.1',PORT))
        events.append(dict(kind=label,host_ns=now,frame=n,wire_hex=raw.hex()))
    def receive():
        raw,addr=sock.recvfrom(65535);now=time.monotonic_ns()
        assert addr[0]=='127.0.0.1' and addr[1]==PORT,addr
        state=json.loads(raw)
        events.append(dict(kind='state_received',host_ns=now,state=state,wire_hex=raw.hex()))
        return raw,state,now
    log=(out/'gazebo.log').open('wb')
    process=subprocess.Popen(['gz','sim','-s','-r','-v','3',str(out/'world.sdf')],
                             env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
    result=dict(timeout_count=count,sequence=sequence,status='failed',trials=trials,
                model_sha256=sha(target/'model.sdf'),world_sha256=sha(out/'world.sdf'),
                mock_servo=dict(format='<HHI16H',bytes=40,magic=18458,frame_rate=1000,pwm=[1000]*16),
                only_owned_process_group=process.pid)
    try:
        deadline=time.monotonic()+25
        while time.monotonic()<deadline:
            if process.poll() is not None:raise RuntimeError('Gazebo exited before initial state')
            send(frame,'bootstrap_servo')
            try:current=receive();break
            except socket.timeout:continue
        else:raise TimeoutError('No state from actual plugin within startup deadline')
        sock.settimeout(2)
        result['loaded_plugins']=loaded_plugins()
        assert result['loaded_plugins'] and all(r['sha256']==sha(PLUGIN) for r in result['loaded_plugins'])
        # Upstream initializes uint32 fcu_frame_count=-1, so the first valid
        # controller packet emits its reset notice. Preserve that baseline;
        # a retry must never create an additional controller-reset notice.
        result['startup_controller_reset_warnings']=(out/'gazebo.log').read_text(errors='replace').count('ArduPilot controller has reset')
        for number in range(12):
            for _ in range(25):
                frame+=1;send(frame);current=receive()
            original,previous,started=current
            dropped_frame=frame+1
            events.append(dict(kind='deliberately_dropped_servo',host_ns=time.monotonic_ns(),
                               frame=dropped_frame,wire_hex=packet(dropped_frame).hex(),trial=number))
            repeated,state,retry_ns=receive()
            equal_raw=repeated==original
            equal_epoch=state['timestamp']==previous['timestamp']
            frame=dropped_frame;send(frame,'recovery_servo')
            current=receive()
            delta=current[1]['timestamp']-previous['timestamp']
            row=dict(trial=number,frame=dropped_frame,retry_wait_ms=(retry_ns-started)/1e6,
                     original_epoch=previous['timestamp'],retry_epoch=state['timestamp'],
                     resumed_epoch=current[1]['timestamp'],resumed_step_s=delta,
                     byte_identical_resend=equal_raw,same_epoch_resend=equal_epoch,
                     physics_step_after_valid_servo_only=abs(delta-STEP)<1e-8)
            trials.append(row)
            assert equal_raw and equal_epoch and row['physics_step_after_valid_servo_only'],row
        for _ in range(10):frame+=1;send(frame);current=receive()
        result['status']='passed'
        result['retry_wait_ms_median']=statistics.median(t['retry_wait_ms'] for t in trials)
        result['retry_wait_ms_min']=min(t['retry_wait_ms'] for t in trials)
        result['retry_wait_ms_max']=max(t['retry_wait_ms'] for t in trials)
    except Exception as error:
        result['error']=f'{type(error).__name__}: {error}'
    finally:
        stop(process);sock.close();log.close()
        text=(out/'gazebo.log').read_text(errors='replace')
        result['gazebo_returncode']=process.returncode
        result['motor_reset_warnings']=text.count('resetting motor control')
        result['controller_reset_warnings']=text.count('ArduPilot controller has reset')
        result['retry_phase_controller_reset_warnings']=result['controller_reset_warnings']-result.get('startup_controller_reset_warnings',0)
        result['connect_events']=text.count('Connected to ArduPilot controller')
        result['no_motor_reset_warning']=result['motor_reset_warnings']==0
        result['no_controller_reset_warning_during_retry']=result['retry_phase_controller_reset_warnings']==0
        if not result['no_motor_reset_warning'] or not result['no_controller_reset_warning_during_retry']:
            result['status']='failed'
        with (out/'wire-events.jsonl').open('w') as stream:
            for event in events:stream.write(json.dumps(event,separators=(',',':'))+'\n')
        result['raw_events_sha256']=sha(out/'wire-events.jsonl')
        result['gazebo_log_sha256']=sha(out/'gazebo.log')
        write(out/'result.json',result)
    return result

def inside(out):
    source=UPSTREAM/'src/ArduPilotPlugin.cc';header=UPSTREAM/'include/ArduPilotPlugin.hh'
    identity=dict(image=IMAGE,plugin_path=str(PLUGIN),plugin_sha256=sha(PLUGIN),
                  upstream_commit=git(UPSTREAM,'rev-parse','HEAD'),
                  source_sha256=sha(source),header_sha256=sha(header),
                  environment={k:os.environ.get(k) for k in ('GZ_VERSION','GZ_SIM_SYSTEM_PLUGIN_PATH','LD_LIBRARY_PATH')})
    write(out/'plugin-identity.json',identity)
    assert identity['plugin_sha256']==EXPECTED_PLUGIN_SHA256,'Pinned plugin binary identity mismatch'
    assert identity['source_sha256']==EXPECTED_SOURCE_SHA256,'Pinned plugin source identity mismatch'
    assert identity['upstream_commit']==EXPECTED_UPSTREAM_COMMIT,'Pinned plugin upstream commit mismatch'
    shutil.copyfile(source,out/'ArduPilotPlugin.cc')
    shutil.copyfile(header,out/'ArduPilotPlugin.hh')
    cases=[]
    for sequence,count in enumerate((5,0,0,5),1):
        result=case(out/f'case-{sequence:02d}-count-{count}',count,sequence)
        cases.append(result)
        if result['status']!='passed':break
    by_count={count:[trial['retry_wait_ms'] for case_result in cases if case_result['timeout_count']==count
                     for trial in case_result['trials']] for count in (5,0)}
    medians={str(k):statistics.median(v) for k,v in by_count.items() if v}
    paired_models_only_retry_differs=False
    if len(cases)>=2:
        first=(out/'case-01-count-5/models/iris_probe/model.sdf').read_text()
        second=(out/'case-02-count-0/models/iris_probe/model.sdf').read_text()
        paired_models_only_retry_differs=first.replace('<connectionTimeoutMaxCount>5</connectionTimeoutMaxCount>',
            '<connectionTimeoutMaxCount>0</connectionTimeoutMaxCount>')==second
    success=(len(cases)==4 and all(r['status']=='passed' for r in cases)
             and paired_models_only_retry_differs
             and 50<=medians['5']<=150 and 8<=medians['0']<=35
             and medians['5']/medians['0']>=3)
    result=dict(status='passed' if success else 'failed',identity=identity,cases=cases,
        paired_models_only_retry_differs=paired_models_only_retry_differs,
        median_retry_ms=medians,hardware=False,rf=False,sitl=False,
        semantics='Actual unchanged plugin; lock_step=1 in every case; only retry count differs between paired models. All transmitted PWM are 1000. Same-epoch retry and exactly one 1ms physics step only after the next valid servo are measured.',
        limitations='No physical controller, no flight or RF result. No motor-reset warning plus unchanged lock-step source branch; armed motor behaviour is not exercised.')
    write(out/'result.json',result)
    print(json.dumps(dict(status=result['status'],median_retry_ms=medians,
                          cases=[dict(count=r['timeout_count'],status=r['status'],trials=len(r['trials']),
                                      median=r.get('retry_wait_ms_median'),error=r.get('error')) for r in cases])))
    return 0 if success else 1

def main():
    parser=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--repo-root',type=Path,default=REPO_ROOT)
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--inside',action='store_true',help=argparse.SUPPRESS)
    args=parser.parse_args()
    if args.inside:return inside(args.output)
    if args.execute and sys.platform!='linux':parser.error('--execute requires the Linux Docker runtime host')
    out=args.output.resolve();repo_root=args.repo_root.resolve()
    prepare(repo_root,out)
    if not args.execute:
        print(json.dumps(dict(status='prepared_not_executed',output=str(out),hardware=False)))
        return 0
    import fcntl
    with Path('/tmp/bas-paper-runtime.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        active=subprocess.check_output(['docker','ps','-q','--filter','label=bas.product=native-radio-five-uav'],text=True).strip()
        if active:raise RuntimeError('BAS flight container active')
        container_name=f'bas-plugin-retry-{os.getpid()}-{time.monotonic_ns()}'
        cidfile=out/'container.id'
        argv=['docker','run','--rm','--init','--cidfile',str(cidfile),'--name',container_name,
              '--network','none','--user','0:0','--read-only','--tmpfs','/tmp:rw,size=256m',
              '-e','HOME=/tmp','-e','PYTHONDONTWRITEBYTECODE=1',
              '-v',str(out/'inputs')+':/inputs:ro','-v',str(out)+':/probe:rw',
              IMAGE,'bash','/probe/entry.sh']
        write(out/'command.json',dict(argv=argv,source_commit=git(repo_root,'rev-parse','HEAD'),image=IMAGE,
                                     no_host_network=True,no_host_pid=True,no_privileged=True))
        try:
            with (out/'host.log').open('wb') as log:
                done=subprocess.run(argv,stdout=log,stderr=subprocess.STDOUT,timeout=150)
        finally:
            if cidfile.exists():
                cid=cidfile.read_text().strip()
                if len(cid)==64 and all(c in '0123456789abcdef' for c in cid):
                    check=subprocess.run(['docker','inspect',cid],capture_output=True,text=True)
                    if check.returncode==0:
                        info=json.loads(check.stdout)[0]
                        assert info['Image']==IMAGE and info['Name']==f'/{container_name}'
                        subprocess.run(['docker','stop','-t','10',cid],capture_output=True,check=True)
        print((out/'host.log').read_text())
        if done.returncode==0:
            subprocess.run([sys.executable,str(out/'verify.py'),'--output',str(out)],check=True)
        return done.returncode

if __name__=='__main__':sys.exit(main())
