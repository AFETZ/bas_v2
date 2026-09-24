"""Measured state and real local UART boundaries; no simulated RF outcomes."""
import json
import math
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from types import SimpleNamespace as NS

import pytest

from network.position_tracker.tracker import LiveState
from network.scripts.serial_transport import BoundedQueue, Encoder, Reassembler, radio_is_live

ROOT = Path(__file__).resolve().parents[2]


def odom(stamp, position=(1,2,3), velocity=(1,0,0), yaw=0):
    vector = lambda v: NS(x=v[0], y=v[1], z=v[2])
    return NS(header=NS(stamp=NS(sec=int(stamp), nanosec=round((stamp-int(stamp))*1e9)), frame_id='odom'),
        child_frame_id='base_link',
        pose=NS(pose=NS(position=vector(position), orientation=NS(x=0,y=0,z=math.sin(yaw/2),w=math.cos(yaw/2)))),
        twist=NS(twist=NS(linear=vector(velocity), angular=vector((0,0,.2)))))


def test_source_age_velocity_orientation_and_bounded_history():
    state=LiveState({'robots':[{'name':'uav1'}]},.5)
    for i in range(50):
        t=10+i*.02; now=1_000_000_000+i*20_000_000
        state.clock(t,now)
        assert state.odometry('uav1',odom(t,position=(i,2,30),yaw=math.pi/2),now)
    snapshot=state.snapshot(now+450_000_000)
    uav=snapshot['nodes'][1]
    assert len(uav['history'])==32
    assert uav['position_m']==[49.,2.,30.]
    assert uav['velocity_enu_mps']==pytest.approx([0,1,0])
    assert uav['sample_age_ms']==450
    assert uav['source_sim_time_s']==pytest.approx(10.98)
    assert state.snapshot(now+550_000_000)['nodes'][1]['stale']


def test_reject_nan_wrong_frames_old_samples_and_clock_reset():
    state=LiveState({'robots':[{'name':'uav1'}]})
    state.clock(10,1_000_000_000)
    assert state.odometry('uav1',odom(10),1_000_000_000)
    assert not state.odometry('uav1',odom(9),1_010_000_000)
    assert not state.odometry('uav1',odom(10.1,position=(float('nan'),0,0)),1_020_000_000)
    for frame in ('NED','map','uav2/odom'):
        wrong=odom(10.1);wrong.header.frame_id=frame
        assert not state.odometry('uav1',wrong,1_020_000_000)
    wrong=odom(10.1);wrong.pose.pose.orientation.w=1e308
    assert not state.odometry('uav1',wrong,1_020_000_000)
    assert len(state.history['uav1'])==1
    state.clock(1,1_030_000_000)
    assert state.snapshot(1_040_000_000)['fault']=='gazebo_clock_reset_restart_required'


def test_queue_preserves_end_to_end_deadline_after_partial_write():
    q=BoundedQueue(2,16,.25)
    assert q.put(b'command',1.0)
    q.sent(2)
    q.expire(1.26)
    assert not q.items and q.expired==1 and q.bytes==0
    assert q.put(b'future',2)
    q.expire(1.9)
    assert not q.items


def test_reassembly_preserves_original_timestamp_and_rejects_old_or_future():
    encoder=Encoder(channel='control',uav_id=1,direction='gcs_to_uart')
    rx=Reassembler(channel='control',uav_id=1,direction='gcs_to_uart',max_age_ms=250)
    assert rx.ingest(encoder.encode(b'old',1)[0],1_000_000_000)==[]
    assert rx.ingest(encoder.encode(b'future',2_000_000_000)[0],1_000_000_000)==[]
    assert rx.ingest(encoder.encode(b'fresh',950_000_000)[0],1_000_000_000)==[b'fresh']
    assert rx.released_sent_ns==[950_000_000]
    assert rx.counters.deadline_drops==2


def test_heartbeat_requires_a_healthy_monotonic_timestamp(tmp_path):
    path=tmp_path/'heartbeat'
    path.write_text('old plain tick')
    assert not radio_is_live(str(path),.3)
    path.write_text(json.dumps(dict(healthy=True,monotonic_ns=time.monotonic_ns())))
    assert radio_is_live(str(path),.3)
    path.write_text(json.dumps(dict(healthy=True,monotonic_ns=time.monotonic_ns()-1_000_000_000)))
    assert not radio_is_live(str(path),.3)


@pytest.mark.skipif(os.name=='nt', reason='PTY boundary requires Linux')
def test_uart_filters_sender_age_and_remains_responsive_under_backpressure(tmp_path):
    import pty
    import select
    master,slave=pty.openpty()
    peer=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);peer.bind(('127.0.0.1',0))
    other=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);other.bind(('127.0.0.1',0))
    probe=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);probe.bind(('127.0.0.1',0))
    address=probe.getsockname();probe.close()
    proc=subprocess.Popen([sys.executable,str(ROOT/'network/scripts/communication_vertical.py'),'uart-adapter',
        '--channel','control','--framed','--tty',os.ttyname(slave),
        '--bind','%s:%s'%address,'--peer','%s:%s'%peer.getsockname(),
        '--event-log',str(tmp_path/'events'),'--ready-file',str(tmp_path/'ready'),
        '--metrics-output',str(tmp_path/'metrics'),'--metrics-period-ms','50'],stderr=subprocess.PIPE)
    encoder=Encoder(channel='control',uav_id=1,direction='gcs_to_uart')
    try:
        until=time.monotonic()+5
        while not (tmp_path/'ready').exists() and time.monotonic()<until:
            assert proc.poll() is None
            time.sleep(.02)
        assert (tmp_path/'ready').exists()
        rogue=Encoder(channel='control',uav_id=1,direction='gcs_to_uart')
        other.sendto(rogue.encode(b'wrong-peer')[0],address)
        peer.sendto(encoder.encode(b'old-command',time.monotonic_ns()-60_000_000_000)[0],address)
        time.sleep(.15)
        assert not select.select([master],[],[],.05)[0]
        peer.sendto(encoder.encode(b'fresh-command')[0],address)
        assert select.select([master],[],[],1)[0]
        assert os.read(master,4096)==b'fresh-command'
        # Fill the OS PTY buffer with actual opaque bytes without draining it.
        for _ in range(80):
            for frame in encoder.encode(b'x'*1024):peer.sendto(frame,address)
            time.sleep(.004)
        before=(tmp_path/'metrics').stat().st_mtime_ns
        time.sleep(.4)
        assert (tmp_path/'metrics').stat().st_mtime_ns>before
        metrics=json.loads((tmp_path/'metrics').read_text())
        assert metrics['unexpected_peer']==1 and metrics['deadline_drops']>=1
        assert metrics['queue_peak_bytes']<=65536
        assert metrics['queue_deadline_drops']>0
    finally:
        proc.terminate();_,error=proc.communicate(timeout=5)
        os.close(master);os.close(slave);peer.close();other.close()
        assert proc.returncode==0,error.decode()


def test_lost_serial_record_does_not_exhaust_deadline_of_next_record():
    encoder=Encoder(channel='control',uav_id=1,direction='gcs_to_uart')
    rx=Reassembler(channel='control',uav_id=1,direction='gcs_to_uart',timeout_ms=500,max_age_ms=250)
    encoder.encode(b'lost',1_000_000_000)
    assert rx.ingest(encoder.encode(b'next',1_000_000_000)[0],1_000_000_000)==[]
    assert rx.expire(1_130_000_000)==[b'next']
    resumed=Encoder(channel='control',uav_id=1,direction='gcs_to_uart',initial_sequence=encoder.sequence)
    assert rx.ingest(resumed.encode(b'probe',1_140_000_000)[0],1_150_000_000)==[b'probe']


@pytest.mark.parametrize('delivery,missing,passed',[(0,False,True),(7,False,False),(0,True,False)])
def test_no_bypass_checks_uart_delivery_even_when_gcs_is_silent(tmp_path,monkeypatch,delivery,missing,passed):
    # Controlled counterexample for report logic, not a simulated RF result.
    from scripts.product import native_radio_five_uav_scenario as scenario
    from pymavlink import mavutil
    (tmp_path/'logs').mkdir();(tmp_path/'metrics').mkdir()
    keys=[(channel,uav) for channel in ('control','payload') for uav in range(1,6)]
    (tmp_path/'logs/transport_sequences.json').write_text(json.dumps({f'{c}:uav{u}':42 for c,u in keys}))
    for c,u in keys:
        (tmp_path/f'metrics/{c}_uart_uav{u}.json').write_text(json.dumps({'uart_output_bytes':100}))
    class CounterHarness:
        def __init__(self,args):
            self.message_counts={};self.additional_received=[];self.acks={};self.mavutil=mavutil
            self.transport_encoders={key:Encoder(channel=key[0],uav_id=key[1],direction='gcs_to_uart') for key in keys}
            self.transmitters={key:mavutil.mavlink.MAVLink(None) for key in keys}
            self.sockets={'additional_data':NS(sendto=lambda *args:None)}
        def send(self,channel,uav,message):
            assert self.transport_encoders[channel,uav].sequence==42
        def observe_for(self,duration):
            path=tmp_path/'metrics/control_uart_uav1.json'
            if missing:path.unlink()
            else:path.write_text(json.dumps({'uart_output_bytes':100+delivery}))
        def close(self):pass
    monkeypatch.setattr(scenario,'NativeFiveUavHarness',CounterHarness)
    args=NS(run_dir=str(tmp_path),node_state='unused',radio_profile='test',scenario_config='unused',
            duration_s=0,output=str(tmp_path/'result.json'))
    status=scenario.run_no_bypass_probe(args)
    result=json.loads((tmp_path/'result.json').read_text())
    assert result['passed'] is passed and status==(0 if passed else 1)
    assert result['transport_sequences_resumed'] is True
