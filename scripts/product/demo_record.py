#!/usr/bin/env python3
"""Record five editorial configurations of the existing native RC1 runtime."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
CONTAINER = 'bas-v2-native-radio-five-uav'
sys.path.insert(0, str(ROOT / 'scripts/product'))
from native_source_campaign import source_cases
import yaml


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def terminal(run, seconds, vehicle):
    inside = '/workspace/multiagent_simulation/runs/native-radio-realtime/' + run.name
    title = 'BAS-MAVProxy-' + run.name
    command = ['gnome-terminal', '--wait', '--title=' + title, '--geometry=68x26+2200+50', '--',
               'docker', 'exec', '-it', CONTAINER, 'ip', 'netns', 'exec', 'ams-gcs',
               'python3', '/workspace/multiagent_simulation/scripts/product/demo_mavproxy.py',
               '--run-dir', inside, '--seconds', str(seconds), '--vehicle', str(vehicle)]
    env = dict(os.environ, GDK_BACKEND='x11')
    proc = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    window = None
    for _ in range(50):
        windows = subprocess.check_output(['xwininfo', '-root', '-tree'], text=True)
        match = re.search(r'(0x[0-9a-f]+) "' + re.escape(title), windows)
        if match:
            info = subprocess.check_output(['xwininfo', '-id', match[1]], text=True)
            if 'Map State: IsViewable' in info:
                window = match[1]; break
        if proc.poll() is not None: break
        time.sleep(.1)
    if window is None:
        raise RuntimeError('The real MAVProxy terminal window was not found')
    time.sleep(.3)
    info = subprocess.check_output(['xwininfo', '-id', window], text=True)
    def field(name): return int(re.search(name + r':\s*(-?\d+)', info).group(1))
    width, height = field('Width'), field('Height')
    x, y = field('Absolute upper-left X'), field('Absolute upper-left Y')
    clock = dict(monotonic_ns=time.monotonic_ns(), wall_time_s=time.time(), window_id=window,
                 source='X11 capture of actual GNOME Terminal running installed MAVProxy')
    save(run / 'video/operator_clock.json', clock)
    log = (run/'video/operator_capture.log').open('w')
    recorder = subprocess.Popen(['ffmpeg','-hide_banner','-loglevel','info','-y',
        '-f','x11grab','-framerate','25','-video_size',f'{width}x{height}',
        '-i',f'{os.environ["DISPLAY"]}+{x},{y}',
        '-an','-vf','pad=ceil(iw/2)*2:ceil(ih/2)*2','-c:v','libx264','-preset','ultrafast','-crf','20','-threads','2',
        '-pix_fmt','yuv420p',str(run/'video/operator.mkv')],stdout=log,stderr=log)
    return proc, recorder, log


def archive_run(run, output):
    target = output/'raw'/run.name
    if target.exists(): raise RuntimeError(f'Refusing to overwrite {target}')
    shutil.move(str(run), str(target))
    run.symlink_to(target, target_is_directory=True)
    return target


def record_case(config, case, output):
    number = config['id'][:2]
    run_id = 'demo-' + number + '-' + case + '-' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    run = ROOT/'runs/native-radio-realtime'/run_id
    prepared = ROOT/'runs/demo-configs'/run_id
    prepared.mkdir(parents=True)
    env = dict(os.environ, BAS_NATIVE_FIVE_RUN_ID=run_id, BAS_DEMO_RECORD='1',
               BAS_DEMO_CHAPTER=number, BAS_NATIVE_FIVE_SKIP_BUILD='1')
    target = 'demo-customer' if config['world']=='customer' else 'demo-town01'
    extra = []
    operator_seconds = 0
    if number == '01':
        env.update(BAS_DEMO_TOUR='1')
        if case == 'operator':
            env['BAS_NATIVE_OPERATOR_SECONDS'] = '42'
            operator_seconds = 35
    elif number == '04':
        path = prepared/'sources.yaml'
        path.write_text(yaml.safe_dump(source_cases()[case]))
        env.update(BAS_NATIVE_SOURCES=str(path.relative_to(ROOT)), BAS_NATIVE_OPERATOR_SECONDS='25')
    elif number == '05':
        endpoint = yaml.safe_load((ROOT/'network/config/native_external_serial.yaml').read_text())
        tty = f'/tmp/bas-native-five-{run_id}/uart/control-adapter-0'
        if case == 'serial': endpoint['endpoint']['device'] = tty
        elif case == 'udp': endpoint['endpoint'] = dict(kind='udp', bind=['127.0.0.1',14560], peer=['127.0.0.1',14561])
        else: endpoint['endpoint'] = dict(kind='tcp_client', peer=['127.0.0.1',14561])
        path = prepared/'endpoint.yaml'; path.write_text(yaml.safe_dump(endpoint))
        env.update(BAS_NATIVE_EXTERNAL_CONFIG=str(path.relative_to(ROOT)), BAS_DEMO_EXTERNAL=case,
                   BAS_NATIVE_LATENCY_MODE='1', BAS_NATIVE_OPERATOR_SECONDS='65' if case=='tcp' else '42')
        operator_seconds = 58 if case=='tcp' else 35
    command = ['make', target, 'DEMO_GUI=0'] + extra
    manifest = dict(run_id=run_id, scenario=config['id'], case=case, started_monotonic_ns=time.monotonic_ns(),
                    command=command, environment={k:v for k,v in env.items() if k.startswith('BAS_')},
                    physical_fc='blocked_external', source_config=str(prepared))
    logfile = output/'reports'/(run_id+'-launch.log')
    print('RECORD', config['id'], case, run_id, flush=True)
    with logfile.open('w') as log:
        log.write(json.dumps(manifest, ensure_ascii=False)+'\n'); log.flush()
        child = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        terminal_parts = None
        bridge_at = None
        disconnected = reconnected = False
        failure_events = []
        try:
            while child.poll() is None:
                phase_file = run/'logs/current_phase.txt'
                phase = phase_file.read_text().strip() if phase_file.exists() else ''
                if operator_seconds and terminal_parts is None and phase in ('demo_operator','latency_stationary_warmup'):
                    terminal_parts = terminal(run, operator_seconds, 1 if number=='05' else 2)
                    bridge_at = time.monotonic()
                if terminal_parts and terminal_parts[0].poll() is not None and terminal_parts[1].poll() is None:
                    terminal_parts[1].send_signal(signal.SIGINT)
                    terminal_parts[1].wait(timeout=15)
                if case=='tcp' and bridge_at:
                    elapsed = time.monotonic()-bridge_at
                    if elapsed > 23 and not disconnected:
                        pid = (run/'logs/demo_endpoint_bridge.pid').read_text().strip()
                        subprocess.run(['docker','exec',CONTAINER,'kill','-TERM',pid], check=True)
                        failure_events.append(dict(monotonic_ns=time.monotonic_ns(),event='TCP software peer deliberately disconnected'))
                        disconnected = True
                    if elapsed > 30 and not reconnected:
                        tty = f'/tmp/bas-native-five-{run_id}/uart/control-adapter-0'
                        subprocess.run(['docker','exec','-d',CONTAINER,'socat','-b','4096',
                            'TCP4-LISTEN:14561,reuseaddr',f'FILE:{tty},b115200,raw,echo=0'], check=True)
                        failure_events.append(dict(monotonic_ns=time.monotonic_ns(),event='TCP software peer restarted; gateway reconnect observed in endpoint events'))
                        reconnected = True
                time.sleep(.3)
        finally:
            if terminal_parts:
                app, capture, capture_log = terminal_parts
                if capture.poll() is None:
                    capture.send_signal(signal.SIGINT); capture.wait(timeout=15)
                capture_log.close()
                capture_text=(run/'video/operator_capture.log').read_text(errors='replace')
                match=re.search(r'Duration: N/A, start: (\d+\.\d+)',capture_text)
                if match:
                    clock=json.loads((run/'video/operator_clock.json').read_text())
                    clock['recorder_launch_monotonic_ns']=clock['monotonic_ns']
                    clock['first_frame_wall_time_s']=float(match[1])
                    clock['monotonic_ns']+=round((float(match[1])-clock['wall_time_s'])*1e9)
                    clock['clock_basis']='FFmpeg x11grab first input PTS (Unix wall) mapped through simultaneous host wall/monotonic pair'
                    save(run/'video/operator_clock.json',clock)
                if app.poll() is None: app.terminate()
            if child.poll() is None:
                subprocess.run(['make','stop'], cwd=ROOT, stdout=log, stderr=log)
                child.wait(timeout=180)
        manifest.update(exit_code=child.returncode, finished_monotonic_ns=time.monotonic_ns(),failure_events=failure_events)
    if not (run/'video/frames.jsonl').exists():
        save(output/'reports'/(run_id+'-failed.json'),manifest)
        raise RuntimeError(f'No current Gazebo video was captured: {logfile}')
    save(run/'demo_record.json',manifest)
    shutil.copytree(prepared, run/'demo_config')
    archived = archive_run(run,output)
    scenario_summary = archived/'metrics/scenario_summary.json'
    if number in ('01','02') and case != 'operator' and (not scenario_summary.exists() or json.loads(scenario_summary.read_text()).get('status')!='passed'):
        raise RuntimeError(f'Flight did not complete; failed run preserved: {archived}')
    if number=='03' and (not scenario_summary.exists() or json.loads(scenario_summary.read_text()).get('status')!='communications_complete'):
        raise RuntimeError(f'Communications phases did not complete: {archived}')
    if number=='04' and child.returncode!=0:
        raise RuntimeError(f'Native source runtime did not complete: {archived}')
    if operator_seconds:
        video=archived/'video/operator.mkv'
        if not video.exists() or not video.stat().st_size:
            raise RuntimeError(f'Actual operator window capture is absent: {archived}')
        io=(archived/'video/operator_io.jsonl').read_text()
        if 'Got COMMAND_ACK: REQUEST_MESSAGE: ACCEPTED' not in io:
            raise RuntimeError(f'No real MAVProxy REQUEST_MESSAGE ACK observed: {archived}')
    if number=='05':
        proof=json.loads((archived/'metrics/no_bypass_summary.json').read_text())
        if not proof.get('passed'):
            raise RuntimeError(f'Native stop probe did not pass: {archived}')
        events=[json.loads(line) for line in (archived/'external_endpoint/events.jsonl').read_text().splitlines()]
        if case=='tcp' and sum(e['event']=='connected' for e in events)<2:
            raise RuntimeError(f'TCP reconnect not observed: {archived}')
    return manifest


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--scenario', default='all', choices=['all','01','02','03','04','05'])
    p.add_argument('--output', type=Path, default=Path('/home/bas/bas_v2-demo/rc1-2026-09-05'))
    a=p.parse_args(); a.output=a.output.resolve()
    for directory in ['videos','subtitles','raw','screenshots','reports','metrics']:
        (a.output/directory).mkdir(parents=True,exist_ok=True)
    configs=sorted((ROOT/'network/config/demo').glob('*.json'))
    for path in configs:
        config=json.loads(path.read_text()); num=config['id'][:2]
        if a.scenario not in ('all',num): continue
        result_path=a.output/'reports'/(config['id']+'-runs.json')
        if result_path.exists():
            if a.scenario=='all':
                print('REUSE completed scenario',config['id'],flush=True)
                continue
            raise RuntimeError(f'Already recorded: {result_path}; choose a new DEMO_OUTPUT for a repeat')
        cases=config['phases'] if num in ('04','05') else (['operator','main'] if num=='01' else ['main'])
        progress=a.output/'reports'/(config['id']+'-progress.json')
        results=json.loads(progress.read_text()) if progress.exists() else []
        for case in cases:
            if any(result['case']==case for result in results): continue
            results.append(record_case(config,case,a.output))
            save(a.output/'reports'/(config['id']+'-progress.json'),results)
        if num=='04' and not (a.output/'reports/native-maps').exists():
            map_id='demo-native-maps-'+time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())
            env=dict(os.environ,BAS_SIONNA_WIFI_RUN_ID=map_id,
                     BAS_NATIVE_MAP_SOURCES='network/config/native_jammers_reference.yaml',BAS_NATIVE_MAP_TIME_S='4')
            with (a.output/'reports'/f'{map_id}-launch.log').open('w') as log:
                subprocess.run(['make','native-maps'],cwd=ROOT,env=env,stdout=log,stderr=log,check=True)
            src=ROOT/'runs'/map_id;dest=a.output/'reports/native-maps'
            shutil.move(str(src),str(dest));src.symlink_to(dest,target_is_directory=True)
            save(a.output/'reports/native-maps-run.json',dict(run_id=map_id,configuration='native_jammers_reference.yaml',
                 comparable_case='continuous',grid='8x8; x=0..140 m, y=-20..120 m, z=2 m',time_s=4,
                 basis='separate native pipeline after capture; SINR availability is not measured PDR'))
        save(result_path,results)
    return 0


if __name__=='__main__': raise SystemExit(main())
