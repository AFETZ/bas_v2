#!/usr/bin/env python3
"""Independently reconstruct dropped-servo results from the captured wire events."""
import hashlib
import argparse
import json
from pathlib import Path
import statistics
import struct

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output',required=True,type=Path)
root=parser.parse_args().output.resolve()
cases=[]
for folder in sorted(root.glob('case-*')):
    events=[json.loads(line) for line in (folder/'wire-events.jsonl').read_text().splitlines()]
    measured=[]
    for index,event in enumerate(events):
        if event['kind']=='deliberately_dropped_servo':
            before,retry,servo,after=events[index-1],events[index+1],events[index+2],events[index+3]
            assert before['kind']==retry['kind']==after['kind']=='state_received'
            assert servo['kind']=='recovery_servo'
            assert retry['wire_hex']==before['wire_hex']
            assert servo['wire_hex']==event['wire_hex']
            assert after['state']['timestamp']-before['state']['timestamp']>0
            assert abs(after['state']['timestamp']-before['state']['timestamp']-.001)<1e-8
            assert before['host_ns']<=event['host_ns']<retry['host_ns']<=servo['host_ns']<after['host_ns']
            measured.append((retry['host_ns']-before['host_ns'])/1e6)
        if 'servo' in event['kind']:
            packet=bytes.fromhex(event['wire_hex'])
            assert len(packet)==40
            magic,rate,frame,*pwm=struct.unpack('<HHI16H',packet)
            assert magic==18458 and rate==1000 and frame==event['frame'] and pwm==[1000]*16
    assert len(measured)==12
    cases.append(dict(case=folder.name,trials=len(measured),median_retry_ms=statistics.median(measured),
        min_retry_ms=min(measured),max_retry_ms=max(measured),
        wire_events_sha256=hashlib.sha256((folder/'wire-events.jsonl').read_bytes()).hexdigest()))
assert len(cases)==4
result=dict(status='passed',source='raw wire-events.jsonl, not summarized result assertions',
            trials=48,all_servo_packets_disarmed=True,no_servo_between_drop_and_retry=True,
            all_retry_wire_bytes_identical=True,all_recovery_servo_bytes_match_dropped_packet=True,
            all_resumed_physics_steps_s=.001,cases=cases)
(root/'independent-verification.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(dict(status='passed',trials=result['trials'],cases=cases)))
