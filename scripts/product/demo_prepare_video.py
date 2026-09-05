#!/usr/bin/env python3
"""Reuse RC1 metric exporters after simulation/capture; preserve original results."""
import argparse,json,sys,shutil,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts/product'))
from summarize_native_radio_five_uav import native_events, export_received_energy, export_wifi_monitor, summarize_latency_operations, summarize_native_contention, summarize_native_sources
from demo_video_data import readjson,jsonlines,Run


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    performance=[]
    for path in sorted((a.output/'reports').glob('*-runs.json')):
        for manifest in json.loads(path.read_text()):
            run=a.output/'raw'/manifest['run_id']
            operator=run/'video/operator.mkv'
            if operator.exists() and operator.stat().st_size and not (run/'video/operator_lifecycle.json').exists():
                # Keep the continuous application session; omit desktop after its window closes.
                clock=readjson(run/'video/operator_clock.json')
                events=jsonlines(run/'video/operator_io.jsonl')
                ended=next((e['monotonic_ns']/1e9 for e in events if e.get('event')=='operator_exit'),None)
                duration=ended-clock['monotonic_ns']/1e9-.15 if ended else (56.5 if manifest['case']=='tcp' else 33.5)
                trimmed=operator.with_name('operator-session.mkv')
                subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-i',str(operator),'-t',str(duration),'-map','0:v:0','-c','copy',str(trimmed)],check=True)
                trimmed.replace(operator)
                (run/'video/operator_lifecycle.json').write_text(json.dumps(dict(kept_prefix_seconds=duration,
                    reason='continuous original operator session; unrelated desktop after window closure excluded',
                    clock_basis=clock.get('clock_basis','host wall/monotonic pair at recorder launch; first-packet offset not separately measured in this run')),indent=2)+'\n')
            events=native_events(run/'logs/native_radio_events.csv')
            stats=readjson(run/'metrics/native_radio_stats.json')
            if not (run/'metrics/received_energy.csv').exists(): export_received_energy(run,events,stats)
            if not (run/'metrics/wifi_monitor_rx.csv').exists(): export_wifi_monitor(run,events)
            if not (run/'metrics/native_queue_summary.json').exists(): summarize_native_contention(run,events,stats)
            summarize_native_sources(run,events)
            scenario=readjson(run/'metrics/scenario_summary.json')
            if scenario.get('command_operations'):
                result=summarize_latency_operations(run,scenario)
                (run/'metrics/demo_latency.json').write_text(json.dumps(result,indent=2)+'\n')
            del events
            data=Run(run);performance.append(data.performance)
            (a.output/'metrics'/(run.name+'-capture.json')).write_text(json.dumps(data.performance,indent=2)+'\n')
            print('PREPARED',run.name,flush=True)
    (a.output/'metrics/recording_performance.json').write_text(json.dumps(performance,indent=2)+'\n')
    references=a.output/'reports/rc1_reference';references.mkdir(exist_ok=True)
    for name in ['rc1-native-reference-matrix','rc1-cache-study-isolated']:
        src=ROOT/'runs'/name
        dest=references/name
        if not dest.exists(): shutil.copytree(src,dest)
    shutil.copy2(ROOT/'doc/VALIDATION_REPORT.md',references/'VALIDATION_REPORT.md')
    shutil.copy2(ROOT/'doc/DELIVERY_SCOPE.md',references/'DELIVERY_SCOPE.md')
    shutil.copy2(ROOT/'.external/customer_10km/geometry_summary.json',a.output/'reports/geometry_summary.json')
    return 0
if __name__=='__main__':raise SystemExit(main())
