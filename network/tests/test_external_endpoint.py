"""Software transport tests: real sockets/PTY, never hardware validation."""
import importlib.util
import json
import os
from pathlib import Path
import pty
import select
import socket
import subprocess
import sys
import time
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/"network/scripts"))
from external_endpoint import BoundedQueue
from serial_transport import Encoder, Reassembler


def test_queue_limits_and_expiry():
    queue = BoundedQueue(2, 8, .5)
    assert queue.put(b"abcd", 1)
    assert queue.put(b"efgh", 1)
    assert not queue.put(b"i", 1)
    queue.sent(2)
    assert queue.bytes == 6
    queue.expire(2)
    assert queue.bytes == 0 and queue.expired == 2 and queue.dropped == 1


def port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.parametrize("kind", ["serial", "udp", "tcp_client"])
def test_real_bidirectional_endpoints(tmp_path, kind):
    radio = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    radio.bind(("127.0.0.1", 0))
    radio.settimeout(4)
    bind_port = port()
    master = slave = None
    external = listener = None
    if kind == "serial":
        master, slave = pty.openpty()
        serial_path = tmp_path/"controller"
        serial_path.symlink_to(os.ttyname(slave))
        endpoint = dict(kind=kind, device=str(serial_path), baud=115200,
                        bytesize=8, parity="N", stopbits=1)
    elif kind == "udp":
        external = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        external.bind(("127.0.0.1", 0))
        external.settimeout(4)
        endpoint = dict(kind=kind, bind=["127.0.0.1", port()], peer=list(external.getsockname()))
    else:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(4)
        endpoint = dict(kind=kind, peer=list(listener.getsockname()))
    config = dict(uav_id=1, channel="control", endpoint=endpoint,
        radio=dict(bind=["127.0.0.1", bind_port], peer=list(radio.getsockname())),
        queues=dict(packets=4, bytes=4096, deadline_s=.5), watchdog_s=.3, reconnect_s=.1)
    path = tmp_path/"config.yaml"
    path.write_text(yaml.safe_dump(config))
    process = subprocess.Popen([sys.executable, str(ROOT/"network/scripts/external_endpoint.py"),
        "--config", str(path), "--output", str(tmp_path/"out")], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        ready = tmp_path/"out/metrics.json"
        deadline = time.monotonic()+4
        while not ready.exists() and time.monotonic() < deadline:
            assert process.poll() is None, process.communicate()[1]
            time.sleep(.02)
        assert ready.exists()
        if kind == "tcp_client":
            external, _ = listener.accept()
            external.settimeout(4)
        outgoing = b"opaque external controller bytes\x00\xff"
        if kind == "serial":
            os.write(master, outgoing)
        elif kind == "udp":
            external.sendto(outgoing, tuple(endpoint["bind"]))
        else:
            external.sendall(outgoing)
        datagram, source = radio.recvfrom(65535)
        decoder = Reassembler(channel="control", uav_id=1, direction="uart_to_gcs")
        assert decoder.ingest(datagram) == [outgoing]
        incoming = b"safe opaque return bytes\xff\x00"
        encoder = Encoder(channel="control", uav_id=1, direction="gcs_to_uart")
        for fragment in encoder.encode(incoming):
            radio.sendto(fragment, source)
        if kind == "serial":
            assert select.select([master], [], [], 4)[0]
            received = os.read(master, 4096)
        elif kind == "udp":
            received, _ = external.recvfrom(4096)
        else:
            received = external.recv(4096)
        assert received == incoming
        if kind == "tcp_client":
            external.close()
            external, _ = listener.accept()
            external.settimeout(4)
        time.sleep(.6)
        metrics = json.loads(ready.read_text())
        assert metrics["endpoint_silent"] and metrics["radio_silent"]
        assert metrics["radio_to_endpoint_bytes"] == len(incoming)
        assert metrics["peak_queue_bytes"] <= 4096
        if kind == "tcp_client":
            assert metrics["reconnects"] >= 2
        if kind == "serial":
            os.close(master)
            os.close(slave)
            time.sleep(.2)
            master, slave = pty.openpty()
            serial_path.unlink()
            serial_path.symlink_to(os.ttyname(slave))
            time.sleep(.3)
            os.write(master, b"after-device-reconnect")
            datagram, _ = radio.recvfrom(65535)
            assert decoder.ingest(datagram) == [b"after-device-reconnect"]
    finally:
        process.terminate()
        _, errors = process.communicate(timeout=5)
        assert process.returncode == 0, errors.decode()
        radio.close()
        if external is not None:
            external.close()
        if listener is not None:
            listener.close()
        if master is not None:
            os.close(master)
            os.close(slave)


def test_native_heartbeat_and_old_radio_bytes_fail_closed(tmp_path):
    radio=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);radio.bind(('127.0.0.1',0))
    device=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);device.bind(('127.0.0.1',0));device.settimeout(.2)
    heartbeat=tmp_path/'native.heartbeat'
    config=dict(uav_id=1,channel='control',endpoint=dict(kind='udp',bind=['127.0.0.1',port()],peer=list(device.getsockname())),
        radio=dict(bind=['127.0.0.1',port()],peer=list(radio.getsockname())),queues=dict(packets=4,bytes=4096,deadline_s=.15),
        watchdog_s=.3,reconnect_s=.1,radio_watchdog_file=str(heartbeat))
    path=tmp_path/'cfg.yaml';path.write_text(yaml.safe_dump(config))
    proc=subprocess.Popen([sys.executable,str(ROOT/'network/scripts/external_endpoint.py'),'--config',str(path),'--output',str(tmp_path/'out')])
    encoder=Encoder(channel='control',uav_id=1,direction='gcs_to_uart')
    try:
        deadline=time.monotonic()+4
        while not (tmp_path/'out/metrics.json').exists() and time.monotonic()<deadline:time.sleep(.02)
        for frame in encoder.encode(b'blocked-no-live-native-runtime'):radio.sendto(frame,tuple(config['radio']['bind']))
        with pytest.raises(socket.timeout):device.recvfrom(4096)
        old=encoder.encode(b'old-command')
        time.sleep(.2);heartbeat.write_text('native simulation tick')
        for frame in old:radio.sendto(frame,tuple(config['radio']['bind']))
        with pytest.raises(socket.timeout):device.recvfrom(4096)
        heartbeat.touch()
        for frame in encoder.encode(b'fresh-command'):radio.sendto(frame,tuple(config['radio']['bind']))
        assert device.recvfrom(4096)[0]==b'fresh-command'
        time.sleep(.3)
        assert json.loads((tmp_path/'out/metrics.json').read_text())['stale_radio_drops']>=2
    finally:
        proc.terminate();proc.wait(timeout=5);radio.close();device.close()
