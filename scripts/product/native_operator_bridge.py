#!/usr/bin/env python3
"""Expose five local MAVLink UDP ports through the existing BSF1/native radio.
Run inside ams-gcs. MAVProxy uses udpout:127.0.0.1:14551 through :14555.
"""
import argparse
import json
import selectors
import signal
import socket
import sys
import time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from scripts.product.town01_full_stack_scenario import FlightHarness, endpoint_ip
from network.scripts.serial_transport import decode_chunk


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-dir',type=Path,required=True)
    p.add_argument('--node-state',type=Path,required=True)
    p.add_argument('--duration-s',type=float,default=600)
    a=p.parse_args()
    h=FlightHarness(a.run_dir,a.node_state,1)
    clients={}; peers={}; received={i:0 for i in range(1,6)}; sent=received.copy()
    drops=0; running=True
    def stop(*_):
        nonlocal running
        running=False
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    for i in range(1,6):
        s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
        s.bind(('127.0.0.1',14550+i));s.setblocking(False)
        clients[i]=s;h.selector.register(s,selectors.EVENT_READ,i)
    deadline=time.monotonic()+a.duration_s
    try:
        while running and time.monotonic()<deadline:
            for key,_ in h.selector.select(.05):
                data,source=key.fileobj.recvfrom(65535)
                if isinstance(key.data,int):
                    i=key.data;peers[i]=source
                    for datagram in h.transport_encoders[('control',i)].encode(data):
                        try: h.sockets['control'].sendto(datagram,(endpoint_ip(i),14600+i))
                        except BlockingIOError: drops+=1
                    sent[i]+=len(data)
                elif key.data=='control':
                    try: chunk=decode_chunk(data)
                    except ValueError:
                        drops+=1;continue
                    i=chunk.uav_id
                    if i not in clients or source[0]!=endpoint_ip(i):
                        drops+=1;continue
                    for record in h.transport_receivers[('control',i)].ingest(data,time.monotonic_ns()):
                        if i in peers:
                            try: clients[i].sendto(record,peers[i])
                            except BlockingIOError: drops+=1
                            received[i]+=len(record)
            for i in clients:
                # Expired gaps release only genuine assembled records.
                for record in h.transport_receivers[('control',i)].expire(time.monotonic_ns()):
                    if i in peers:
                        try: clients[i].sendto(record,peers[i])
                        except BlockingIOError: drops+=1
                        received[i]+=len(record)
    finally:
        for s in clients.values():s.close()
        h.close()
        (a.run_dir/'metrics/operator_bridge.json').write_text(json.dumps(dict(
            gcs='existing MAVProxy',transport='BSF1 over native Wi-Fi/Sionna',
            received_raw_bytes=received,sent_raw_bytes=sent,nonblocking_drops=drops,
            application_queue='none; bounded kernel UDP buffers; no replay'),indent=2)+'\n')
    return 0

if __name__=='__main__':raise SystemExit(main())
