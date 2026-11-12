"""Public radio-HitL acceptance through Serial PTYs or socat UDP/TCP. Default: prepare inputs only.

No outer flock: bas.run owns /tmp/bas-paper-runtime.lock throughout its runtime.
Only --execute opens ports/starts the public controller and owned bridge peers.
"""
import argparse,hashlib,json,os,signal,socket,subprocess,sys,time
from pathlib import Path

HERE=Path(__file__).resolve().parent
ROOT=next(p for p in HERE.parents if (p/'bas.py').is_file())
sys.path.insert(0,str(ROOT));import bas
MOUNT='/workspace/multiagent_simulation'
INSPECT='{"id":{{json .Id}},"image":{{json .Image}},"running":{{json .State.Running}},"pid":{{json .State.Pid}},"user":{{json .Config.User}},"cwd":{{json .Config.WorkingDir}},"product":{{json (index .Config.Labels "bas.product")}},"source":{{range .Mounts}}{{if eq .Destination "/workspace/multiagent_simulation"}}{{json .Source}}{{end}}{{end}}}'


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def write(path,value):
    temp=path.with_suffix('.tmp');temp.write_text(json.dumps(value,indent=2)+'\n');temp.replace(path)


def prepare_inputs(run_id, transport, duration_s):
    """Freeze one replayable input pair without opening endpoints or simulators."""
    import math
    bas.identifier(run_id)
    if transport not in ('serial', 'ethernet'):
        raise ValueError('transport must be serial or ethernet')
    if not math.isfinite(duration_s) or duration_s <= 0:
        raise ValueError('duration must be finite and positive')
    folder=ROOT/'runs/control/hitl-fixture-inputs'/run_id
    config=json.loads((ROOT/'runtime-link-test.json').read_text())
    config['environment']['scene']='network/config/urban_low.json'
    config['experiment'].update(policy='off',duration_s=duration_s)
    config['flows']['payload_load']=None
    request=dict(controller='pixhawk_6c',radio_device='microhard_p2400' if transport=='serial' else 'microhard_pmddl2450',
                 uav_id=1,channels={})
    for channel in ('control','payload'):
        if transport=='serial':
            endpoint=dict(kind='serial',device=f'/tmp/bas-native-five-{run_id}/uart/{channel}-adapter-0',baud=115200)
        elif channel=='control':
            endpoint=dict(kind='udp',bind=['127.0.0.1',18811],peer=['127.0.0.1',18801])
        else:
            endpoint=dict(kind='tcp_client',peer=['127.0.0.1',18802])
        request['channels'][channel]=dict(endpoint=endpoint)
    config['hitl']=dict(mode='radio_bench',request=(folder/'request.json').relative_to(ROOT).as_posix())
    files={name:(json.dumps(value,indent=2)+'\n').encode() for name,value in [('request.json',request),('runtime.json',config)]}
    if folder.exists():
        if any(not (folder/name).is_file() or (folder/name).read_bytes()!=value for name,value in files.items()):
            raise ValueError('fixture inputs are immutable; use a new run ID for changed inputs')
    else:
        folder.mkdir(parents=True,exist_ok=False)
        for name,value in files.items():
            with (folder/name).open('xb') as stream:stream.write(value)
    return folder


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id',required=True)
    parser.add_argument('--transport',choices=('serial','ethernet'),required=True)
    parser.add_argument('--duration-s',type=float,default=20)
    parser.add_argument('--expected-head',required=True)
    parser.add_argument('--execute',action='store_true',help='Linux only; opens fixture ports and starts actual public BAS')
    args=parser.parse_args();bas.identifier(args.run_id)
    if len(args.expected_head)!=40 or any(c not in '0123456789abcdef' for c in args.expected_head):parser.error('expected-head must be a full lowercase commit SHA')
    current=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    if current!=args.expected_head:raise RuntimeError('source HEAD differs from the explicitly selected revision')
    inputs=prepare_inputs(args.run_id,args.transport,args.duration_s)
    config=bas.load_config(inputs/'runtime.json')
    _,_,_,bundle=bas._hitl_input(config,ROOT)
    if config['drone']['count']!=5 or bundle['native_binding']['uav_id']!=1 or bundle['source_request']['radio_device']!=('microhard_pmddl2450' if args.transport=='ethernet' else 'microhard_p2400'):
        raise RuntimeError('this fixture requires five UAVs and the selected UAV1 interface reference')
    limits=bas.runtime_duration_limits(config['experiment']['duration_s'],120,5,['control','payload'],mode='latency_diagnostic',radio_bench=True)
    controller=[sys.executable,str(ROOT/'bas.py'),'run','--config',str(inputs/'runtime.json'),'--run-id',args.run_id]
    relative=(HERE/'bridge.py').relative_to(ROOT).as_posix()
    record=dict(schema=1,run_id=args.run_id,transport=args.transport,expected_head=args.expected_head,source_commit=current,
        status='configuration_checked_not_started',controller_argv=controller,config=config,bundle=bundle,lifetime=limits,
        files_sha256={p.relative_to(ROOT).as_posix():sha(p) for p in (HERE/'run_probe.py',HERE/'bridge.py',inputs/'runtime.json',inputs/'request.json',ROOT/config['environment']['scene'],ROOT/'network/config/hitl_reference_devices.json')},
        scope='Software '+args.transport+' to actual SITL UART PTYs on UAV1; five native nodes and NPU; no physical hardware or flight qualification',
        expected_data_path='SITL UART -> '+('socat UDP/TCP -> ' if args.transport=='ethernet' else '')+'external_endpoint BSF1 -> native ns-3 PHY/MAC -> GCS; requests return through the modeled radio',
        goodput_fields='Request reference ceilings only, not a generated/measured 1/10kbps load',commands=[],error=None)
    if not args.execute:
        print(json.dumps(record,indent=2));return 0
    if sys.platform!='linux':raise ValueError('execution requires Linux; configuration-only check remains portable')
    if subprocess.check_output(['git','status','--porcelain','--untracked-files=no'],cwd=ROOT,text=True):
        raise RuntimeError('tracked source changes are present; choose a concrete reviewed revision before this proof')
    for path in (bas.control_dir(args.run_id),bas.raw_dir(args.run_id)):
        if path.exists():raise RuntimeError('run ID is immutable and already used: '+str(path))
    attempt=inputs/'attempt';attempt.mkdir(parents=True,exist_ok=False)
    ledger=attempt/'collector.json';record['status']='starting';write(ledger,record)
    def command(argv,*,timeout=15,required=True):
        begin=time.monotonic_ns();entry=dict(argv=argv,status='started');record['commands'].append(entry);write(ledger,record)
        try:
            result=subprocess.run(argv,cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=timeout)
            entry.update(status='completed',exit_code=result.returncode,stdout=result.stdout,stderr=result.stderr)
        except BaseException as error:
            entry.update(status='failed',error=str(error));raise
        finally:
            entry['duration_ns']=time.monotonic_ns()-begin;write(ledger,record)
        if required and result.returncode:raise RuntimeError(f'command failed {result.returncode}: {argv[0]}')
        return result
    name='bas-core-'+args.run_id
    child=bridge=None;cid=None;stopped=False;requested_stop=False;streams=[]
    def stop(*_):
        nonlocal stopped
        stopped=True
    old={s:signal.signal(s,stop) for s in (signal.SIGTERM,signal.SIGINT)}
    try:
        # The namespace is host for the physical-endpoint sockets. Check only
        # during execution; socket bind failures are never treated as free ports.
        ports=([(socket.SOCK_DGRAM,18811),(socket.SOCK_DGRAM,18801),(socket.SOCK_STREAM,18802)] if args.transport=='ethernet' else [])
        for kind,port in ports:
            with socket.socket(socket.AF_INET,kind) as test:test.bind(('127.0.0.1',port))
        record['port_preflight']=[dict(protocol='udp' if kind==socket.SOCK_DGRAM else 'tcp',host='127.0.0.1',port=port,status='bindable_then_released') for kind,port in ports]
        if command(['docker','inspect','--format',INSPECT,name],required=False).returncode==0:
            raise RuntimeError('container name already exists; never attach to or remove it')
        output=(attempt/'controller.stdout.log').open('xb');streams.append(output)
        child=subprocess.Popen(controller,cwd=ROOT,env=os.environ.copy(),stdout=output,stderr=subprocess.STDOUT,start_new_session=True)
        record.update(controller_pid=child.pid,started_host_monotonic_ns=time.monotonic_ns());write(ledger,record)
        startup_deadline=time.monotonic()+limits['native_budgets']['native_readiness_s']
        while not stopped and child.poll() is None:
            inspected=command(['docker','inspect','--format',INSPECT,name],required=False)
            if inspected.returncode==0:
                actual=json.loads(inspected.stdout)
                if (actual['image']!=config['runtime']['image'] or actual['user']!='0:0' or actual['cwd']!=MOUNT
                        or actual['product']!='native-radio-five-uav' or Path(actual['source']).resolve()!=ROOT):
                    raise RuntimeError('container identity/image/UID/cwd/product/workspace does not match this exact public run')
                if actual['running'] and type(actual['pid']) is int and actual['pid']>0:
                    cid=actual['id'];record['owned_container']=actual;write(ledger,record);break
            if time.monotonic()>=startup_deadline:raise TimeoutError('own BAS container did not start before existing native readiness bound')
            time.sleep(.25)
        if cid and args.transport=='ethernet':
            log=(attempt/'bridge-controller.stdout.log').open('xb');streams.append(log)
            argv=['docker','exec','--user','0:0','--workdir',MOUNT,cid,'python3','-u',MOUNT+'/'+relative,'--run-id',args.run_id,
                  '--wait-timeout-s',str(limits['native_budgets']['native_readiness_s'])]
            bridge=subprocess.Popen(argv,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT)
            record.update(bridge_controller_argv=argv,bridge_controller_pid=bridge.pid,status='running');write(ledger,record)
        end=time.monotonic()+limits['service_duration_s']
        while child.poll() is None:
            if stopped or time.monotonic()>=end:
                if not requested_stop:
                    state=bas.control_dir(args.run_id)/'run.json'
                    if state.exists() and json.loads(state.read_text()).get('status')=='running':bas.request_stop(args.run_id)
                    else:child.terminate()
                    requested_stop=True;end=time.monotonic()+limits['native_budgets']['native_stop_wait_s']+limits['native_budgets']['process_term_grace_s']
                elif time.monotonic()>=end:raise TimeoutError('public controller did not complete its bounded stop')
            if bridge is not None and bridge.poll() is not None and bridge.returncode!=0:
                raise RuntimeError('owned Ethernet bridge supervisor ended unexpectedly: '+str(bridge.returncode))
            time.sleep(.1)
        record['controller_returncode']=child.returncode
        state=bas.control_dir(args.run_id)/'run.json'
        if state.exists():record['public_run']=json.loads(state.read_text())
        proof=record.get('public_run',{}).get('radio_hitl_verification',{})
        record['status']='completed' if child.returncode==0 and proof.get('passed') is True else 'stopped' if requested_stop else 'failed'
    except BaseException as error:
        record.update(status='failed',error=f'{type(error).__name__}: {error}');raise
    finally:
        # Public stop owns backend teardown; bridge stop is confined to the
        # captured immutable container ID and its recorded process identity.
        if child is not None and child.poll() is None:
            state=bas.control_dir(args.run_id)/'run.json'
            try:
                if state.exists() and json.loads(state.read_text()).get('status')=='running':bas.request_stop(args.run_id)
                else:child.terminate()
                child.wait(timeout=limits['native_budgets']['native_stop_wait_s']+5)
            except (OSError,ValueError,subprocess.TimeoutExpired) as error:record['controller_stop_error']=str(error)
        if cid is not None:
            try:
                check=command(['docker','inspect','--format','{{.Id}}',cid],required=False)
                if check.returncode==0 and check.stdout.strip()==cid:
                    if args.transport=='ethernet':
                        command(['docker','exec','--user','0:0','--workdir',MOUNT,cid,'python3',MOUNT+'/'+relative,'--run-id',args.run_id,'--stop'],required=False)
                    # A dead host controller must not strand its still-running
                    # container. This exact CID was accepted only after the
                    # complete image/run/UID/cwd/mount ownership checks above.
                    record['forced_container_stop']=True
                    stopped_container=command(['docker','stop','--time','5',cid],timeout=20,required=False)
                    if stopped_container.returncode:
                        command(['docker','kill',cid],timeout=15,required=False)
            except (OSError,ValueError,subprocess.SubprocessError) as error:record['owned_container_stop_error']=str(error)
        if bridge is not None:
            try:bridge.wait(timeout=5)
            except subprocess.TimeoutExpired:
                bridge.terminate()
                try:bridge.wait(timeout=3)
                except subprocess.TimeoutExpired:bridge.kill();bridge.wait(timeout=3)
            record['bridge_controller_returncode']=bridge.returncode
        if child is not None and child.poll() is None:
            child.kill();child.wait(timeout=5);record['forced_controller_kill']=True
        for stream in streams:stream.close()
        for s,handler in old.items():signal.signal(s,handler)
        record['ended_host_monotonic_ns']=time.monotonic_ns()
        record['log_sha256']={p.name:sha(p) for p in attempt.glob('*.log')}
        state=bas.raw_dir(args.run_id)/'logs/software_ethernet_bridge.json'
        if state.exists():record['bridge_state']=json.loads(state.read_text());record['bridge_state_sha256']=sha(state)
        bridge_failed=(args.transport=='ethernet' and (record.get('bridge_controller_returncode')!=0
                or record.get('bridge_state',{}).get('status')!='closed' or record['bridge_state'].get('error')))
        if record['status']=='completed' and (bridge_failed
                or any(key in record for key in ('forced_controller_kill','forced_container_stop','controller_stop_error','owned_container_stop_error'))):
            record.update(status='failed',error='public exchange passed, but owned bridge closure was not verified')
        write(ledger,record)
    print(json.dumps({k:record.get(k) for k in ('run_id','status','controller_returncode','error','bridge_controller_returncode')}))
    return 0 if record['status']=='completed' else 130 if record['status']=='stopped' else 1


if __name__=='__main__':raise SystemExit(main())
