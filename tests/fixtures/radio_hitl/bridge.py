"""Owned socat fixture for a single BAS container; not a real modem/controller."""
import argparse,json,math,os,signal,stat,subprocess,time
from pathlib import Path

ROOT=Path('/workspace/multiagent_simulation')


def identity(pid):
    process=Path('/proc')/str(pid)
    return dict(pid=pid,start_ticks=int((process/'stat').read_text().rsplit(')',1)[1].split()[19]),
                uid=int(next(l for l in (process/'status').read_text().splitlines() if l.startswith('Uid:')).split()[1]),
                cwd=os.readlink(process/'cwd'),argv=(process/'cmdline').read_bytes().rstrip(b'\0').decode().split('\0'))


def publish(path,value):
    temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(value,indent=2)+'\n');temporary.replace(path)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--run-id',required=True);parser.add_argument('--stop',action='store_true')
    parser.add_argument('--wait-timeout-s',type=float,default=180)
    args=parser.parse_args()
    import sys
    sys.path.insert(0,str(ROOT));import bas
    bas.identifier(args.run_id)
    if os.getuid()!=0 or Path.cwd()!=ROOT or os.environ.get('BAS_NATIVE_FIVE_RUN_ID')!=args.run_id:
        raise RuntimeError('fixture requires the exact owning runtime container, UID0 and workspace cwd')
    raw=ROOT/'runs/native-radio-realtime'/args.run_id
    state=raw/'logs/software_ethernet_bridge.json'
    if args.stop:
        if not state.is_file():return 0
        expected=json.loads(state.read_text())
        if expected.get('run_id')!=args.run_id:raise RuntimeError('foreign bridge state')
        if expected.get('status') in ('closed','failed'):return 0
        actual=identity(expected['owner']['pid'])
        if actual!=expected['owner'] or actual['uid']!=0 or actual['cwd']!=str(ROOT):
            raise RuntimeError('bridge PID no longer matches recorded owner/start/argv/cwd')
        os.kill(actual['pid'],signal.SIGTERM);return 0
    if state.exists():raise RuntimeError('bridge fixture attempt already exists')
    tty=Path('/tmp')/('bas-native-five-'+args.run_id)/'uart'
    devices={c:tty/(c+'-adapter-0') for c in ('control','payload')}
    if not math.isfinite(args.wait_timeout_s) or args.wait_timeout_s<=0:raise ValueError('finite positive PTY wait required')
    deadline=time.monotonic()+args.wait_timeout_s
    while not all(p.exists() for p in devices.values()):
        if time.monotonic()>=deadline:raise TimeoutError('own runtime did not create both selected UART PTYs')
        time.sleep(.02)
    identities={c:dict(path=str(p),realpath=str(p.resolve()),st_rdev=p.stat().st_rdev) for c,p in devices.items()}
    if any(not stat.S_ISCHR(p.stat().st_mode) for p in devices.values()) or len({v['st_rdev'] for v in identities.values()})!=2:
        raise RuntimeError('requires two distinct actual own-run UART PTYs')
    if any(Path(v['realpath']).parent!=Path('/dev/pts') for v in identities.values()):
        raise RuntimeError('software fixture accepts PTY slaves only; physical UARTs must use another acceptance setup')
    stopped=False
    def stop(*_):
        nonlocal stopped
        stopped=True
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    record=dict(schema=1,run_id=args.run_id,status='starting',owner=identity(os.getpid()),uart_devices=identities,
                scope='UDP control/TCP payload socat to actual SITL PTYs; no echo/mock/physical hardware/baud qualification',children={},error=None)
    children={};logs=[]
    endpoints=dict(control='UDP4-DATAGRAM:127.0.0.1:18811,bind=127.0.0.1:18801',
                   payload='TCP4-LISTEN:18802,bind=127.0.0.1,reuseaddr')
    publish(state,record)
    try:
        for channel,address in endpoints.items():
            path=raw/'logs'/(channel+'-ethernet-socat.log');log=path.open('xb');logs.append(log)
            argv=['/usr/bin/socat','-d','-d',address,'FILE:'+str(devices[channel])+',raw,echo=0,b115200']
            child=subprocess.Popen(argv,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            children[channel]=child
            record['children'][channel]=dict(owner=None,argv=argv,log=str(path))
            record['children'][channel]['owner']=identity(child.pid)
            publish(state,record)
        record['status']='running';publish(state,record)
        while not stopped:
            result=raw/'external_endpoints/result.json'
            phase=raw/'logs/current_phase.txt'
            if result.is_file() and json.loads(result.read_text()).get('status') in ('completed','stopped','failed'):
                record['close_reason']='external_pair_closed';break
            codes={name:child.poll() for name,child in children.items()}
            if any(code is not None for code in codes.values()):
                if phase.is_file() and phase.read_text().strip()=='no_bypass_stop' and codes.get('payload')==0:
                    record['close_reason']='runtime_completed_and_tcp_endpoint_closed';break
                raise RuntimeError('Ethernet bridge ended during active bench: '+repr(codes))
            time.sleep(.02)
        record.setdefault('close_reason','owned_stop_request')
    except BaseException as error:
        record['error']=str(error);raise
    finally:
        for child in children.values():
            if child.poll() is None:child.terminate()
        for channel,child in children.items():
            try:child.wait(timeout=3)
            except subprocess.TimeoutExpired:child.kill();child.wait(timeout=3)
            record['children'][channel]['returncode']=child.returncode
        for log in logs:log.close()
        record.update(status='failed' if record['error'] else 'closed',ended_host_monotonic_ns=time.monotonic_ns())
        publish(state,record)
    return 0


if __name__=='__main__':raise SystemExit(main())
