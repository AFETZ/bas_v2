#!/usr/bin/env python3
"""Run the installed MAVProxy in a real PTY; record timestamped I/O, safe requests only."""
import argparse
import json
import os
import pty
import select
import signal
import subprocess
import sys
import time
from pathlib import Path


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-dir', type=Path, required=True)
    p.add_argument('--seconds', type=float, default=35)
    p.add_argument('--vehicle', type=int, default=2)
    a = p.parse_args()
    master, slave = pty.openpty()
    command = ['/home/ubuntu/.local/bin/mavproxy.py', '--streamrate=-1',
               '--aircraft=' + str(a.run_dir / 'mavproxy')]
    command += [f'--master=udpout:127.0.0.1:{14550+i}' for i in range(1, 6)]
    child = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
    os.close(slave)
    started = time.monotonic()
    commands = [(6, 'status'), (9, f'vehicle {a.vehicle}'), (11, 'long REQUEST_MESSAGE 148'),
                (16, 'status'), (21, 'long REQUEST_MESSAGE 148'), (27, 'status'),
                (36, 'long REQUEST_MESSAGE 148'), (42, 'status'),
                (49, 'long REQUEST_MESSAGE 148'), (54, 'status')]
    print('MAVProxy: 5 SITL SYSID; REQUEST_MESSAGE 148; modeled Wi-Fi/Sionna path', flush=True)
    with (a.run_dir / 'video/operator_io.jsonl').open('w', buffering=1) as log:
        try:
            while time.monotonic() - started < a.seconds and child.poll() is None:
                elapsed = time.monotonic() - started
                if commands and elapsed >= commands[0][0]:
                    _, command = commands.pop(0)
                    os.write(master, (command+'\n').encode())
                    log.write(json.dumps(dict(monotonic_ns=time.monotonic_ns(), input=command))+'\n')
                ready, _, _ = select.select([master], [], [], .05)
                if ready:
                    try: data = os.read(master, 65536)
                    except OSError: break
                    if not data: break
                    sys.stdout.buffer.write(data); sys.stdout.buffer.flush()
                    log.write(json.dumps(dict(monotonic_ns=time.monotonic_ns(), output=data.decode(errors='replace'))) + '\n')
        finally:
            log.write(json.dumps(dict(monotonic_ns=time.monotonic_ns(), event='operator_exit'))+'\n')
            os.killpg(child.pid, signal.SIGTERM)
            child.wait(timeout=10)
            os.close(master)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
