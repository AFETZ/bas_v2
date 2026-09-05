"""Time joins for recorded Gazebo frames and native logs; no inferred packet outcomes."""
import bisect
import csv
import json
import math
from pathlib import Path
import re
import statistics
from collections import defaultdict


def readjson(path, default=None):
    try: return json.loads(path.read_text())
    except (OSError, ValueError): return {} if default is None else default


def jsonlines(path):
    if not path.exists(): return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def rows(path):
    return list(csv.DictReader(path.open())) if path.exists() else []


def number(value):
    try:
        v=float(value)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError): return None


def details(value):
    return dict(piece.split('=',1) for piece in value.split(';') if '=' in piece)


class Series:
    def __init__(self, entries=()):
        entries=sorted(entries,key=lambda item:item[0])
        self.times=[item[0] for item in entries];self.values=[item[1] for item in entries]
    def at(self,t):
        i=bisect.bisect_right(self.times,t)-1
        return (self.times[i],self.values[i]) if i>=0 else (None,None)
    def count(self,t): return bisect.bisect_right(self.times,t)


class Run:
    def __init__(self,path):
        self.path=path; self.id=path.name
        self.manifest=readjson(path/'demo_record.json')
        self.frames=defaultdict(list)
        for row in jsonlines(path/'video/frames.jsonl'):
            self.frames[row['camera']].append(row)
        self.camera_times={c:[r['monotonic_ns']/1e9 for r in records] for c,records in self.frames.items()}
        alltimes=[r['monotonic_ns']/1e9 for records in self.frames.values() for r in records]
        self.start=min(alltimes); self.end=max(alltimes)
        self.phases={}
        for row in jsonlines(path/'logs/scenario_events.jsonl'):
            if row['event']=='phase':self.phases.setdefault(row['detail'],row['monotonic_ns']/1e9)
            else:self.phases.setdefault(row['event'],row['monotonic_ns']/1e9)
        self.native=defaultdict(list); self.native_links=defaultdict(list)
        for row in rows(path/'logs/native_radio_events.csv'):
            event=row['event']; t=int(row['wall_monotonic_ns'])/1e9
            if event in ('realtime_lag','sionna_paths','wifi_data_retry','wifi_mac_queue_enqueue','wifi_mac_queue_dequeue','source_on','source_off','source_switch','foreign_signal','source_signal','phy_rx_drop','phy_rx_end','phy_rx_ok','phy_rx_error','wifi_monitor_rx','wifi_signal_arrival','wifi_foreign_signal','spectrum_signal_arrival','jammer_on','jammer_off') or 'source' in event:
                self.native[event].append((t,row))
            if event=='sionna_paths': self.native_links[(row['node'],row['peer'])].append((t,row))
        self.native={key:Series(value) for key,value in self.native.items()}
        arrivals=self.native.get('spectrum_signal_arrival',Series())
        self.interference=Series((at,row) for at,row in zip(arrivals.times,arrivals.values)
            if row['node']=='uav1' and details(row['details']).get('foreign_signal')=='1')
        self.native_links={key:Series(value) for key,value in self.native_links.items()}
        self.telemetry=defaultdict(list);self.acks=defaultdict(list)
        for row in jsonlines(path/'logs/demo_mavlink.jsonl'):
            t=row['monotonic_ns']/1e9; key=(row['channel'],row['uav']); message=row['message']
            if message['mavpackettype']=='HEARTBEAT': self.telemetry[key].append((t,message))
            if message['mavpackettype']=='COMMAND_ACK': self.acks[key].append((t,message))
        self.telemetry={key:Series(value) for key,value in self.telemetry.items()}
        self.acks={key:Series(value) for key,value in self.acks.items()}
        self.energy=defaultdict(list)
        for row in rows(path/'metrics/received_energy.csv'):
            self.energy[(row['tx'],row['rx'])].append((float(row['wall_monotonic_s']),row))
        self.energy={key:Series(value) for key,value in self.energy.items()}
        self.summary=readjson(path/'metrics/scenario_summary.json')
        self.app=defaultdict(list)
        p=self.summary.get('p2p',{})
        for row in p.get('downlink_sends',[]):self.app[('down_sent',row['uav'])].append((row['source_monotonic_ns']/1e9,row))
        for row in p.get('uplink_deliveries',[]):self.app[('up_rx',row['uav'])].append((row['received_monotonic_ns']/1e9,row))
        for row in self.summary.get('p2mp',{}).get('application_sends',[]):self.app[('roots','all')].append((row['source_monotonic_ns']/1e9,row))
        for row in self.summary.get('simultaneous_uplink',{}).get('application_deliveries',[]):self.app[('shared_rx',row['uav'])].append((row['received_monotonic_ns']/1e9,row))
        for i in range(1,6):
            for row in jsonlines(path/f'logs/additional_uav{i}.jsonl'):
                stamp=row.get('received_monotonic_ns',row.get('monotonic_ns'))
                kind=row.get('kind','')
                if row.get('event')=='transmit':kind='tx_'+kind
                if stamp: self.app[(kind,f'uav{i}')].append((stamp/1e9,row))
        self.app={key:Series(value) for key,value in self.app.items()}
        self.operator_clock=readjson(path/'video/operator_clock.json')
        self.sources=[]
        import yaml
        if (path/'demo_config/sources.yaml').exists():
            self.sources=yaml.safe_load((path/'demo_config/sources.yaml').read_text()).get('sources',[])
        self.stats=readjson(path/'metrics/native_radio_stats.json')
        self.endpoint_events=jsonlines(path/'external_endpoint/events.jsonl')
        self.runtime=Series((row['monotonic_ns']/1e9,row) for row in jsonlines(path/'video/runtime_snapshots.jsonl'))
        solve=[];started=None
        log=path/'logs/ns3_sionna.log'
        if log.exists():
            for line in log.read_text(errors='replace').splitlines():
                match=re.match(r'(\d+)\s+(.*)',line)
                if not match:continue
                stamp=int(match.group(1));value=match.group(2)
                if 'SionnaRtChannelModel:CalculatePaths' in value:started=stamp
                if 'Successfully created new ChannelMatrix' in value and started is not None:
                    solve.append((stamp/1e9,(stamp-started)/1e6));started=None
        self.solves=Series(solve)
        self.performance=self.measure_performance()

    def phase(self,name,default=None):return self.phases.get(name,default if default is not None else self.start)
    def frame(self,camera,t):
        index=max(0,min(len(self.frames[camera])-1,bisect.bisect_right(self.camera_times[camera],t)-1))
        return self.frames[camera][index]
    def interval_rtf(self,camera,row):
        old=self.frame(camera,row['monotonic_ns']/1e9-2)
        duration=(row['monotonic_ns']-old['monotonic_ns'])/1e9
        return (row['simulation_time_s']-old['simulation_time_s'])/duration if duration>.2 else None
    def native_time(self,t):
        at,row=self.native.get('realtime_lag',Series()).at(t)
        return float(row['time_s']) if row else None
    def measure_performance(self):
        result={'run_id':self.id,'scope':'during continuous camera recording; cold samples retained','cameras':{}}
        for camera,frames in self.frames.items():
            wall=(frames[-1]['monotonic_ns']-frames[0]['monotonic_ns'])/1e9
            dt=[(b['monotonic_ns']-a['monotonic_ns'])/1e9 for a,b in zip(frames,frames[1:])]
            result['cameras'][camera]=dict(frames=len(frames),wall_seconds=wall,
                actual_fps=(len(frames)-1)/wall,max_frame_gap_s=max(dt),
                gazebo_clock_rtf=(frames[-1]['simulation_time_s']-frames[0]['simulation_time_s'])/wall)
        lag=self.native.get('realtime_lag',Series())
        values=[float(row['value']) for at,row in zip(lag.times,lag.values) if self.start<=at<=self.end]
        steady=[float(row['value']) for at,row in zip(lag.times,lag.values) if self.start<=at<=self.end and float(row['time_s'])>=10]
        def dist(v):
            v=sorted(v)
            return dict(samples=len(v),p95_ms=v[min(len(v)-1,int(.95*len(v)))],max_ms=max(v)) if v else None
        result['ns3_lag_including_cold']=dist(values);result['ns3_lag_after_native_10s']=dist(steady)
        result['hardware_fc']='blocked_external'
        return result
