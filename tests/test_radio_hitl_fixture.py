"""Collector lifetime regression using local processes only, no simulator."""
from functools import partial
import os
from pathlib import Path
import runpy
import signal
import subprocess
import sys
import tempfile
import time
import unittest

FIXTURE = Path(__file__).parent / 'fixtures/radio_hitl/run_probe.py'


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux parent-death contract')
class FixtureProcessLifetimeTests(unittest.TestCase):
    def test_parent_already_lost_stops_child_before_exec(self):
        callback = runpy.run_path(str(FIXTURE))['stop_on_parent_exit']
        # Production collector has its own SIGTERM handler; it must not eat
        # the parent-death signal in the fork/exec window.
        old = signal.signal(signal.SIGTERM, lambda *_: None)
        try:
            result = subprocess.run([sys.executable, '-c', 'raise SystemExit(27)'],
                                    preexec_fn=partial(callback, -1), timeout=3)
        finally:
            signal.signal(signal.SIGTERM, old)
        self.assertEqual(result.returncode, -signal.SIGTERM)

    def test_killed_collector_delivers_sigterm_to_its_controller(self):
        # A tiny signal-aware child models bas.run's existing SIGTERM handler;
        # the production kernel setup, fork/exec and parent death are real.
        child_code = '''
import signal,sys,time
from pathlib import Path
path=Path(sys.argv[1])
def stopped(signum,frame):
    path.write_text('stop_requested')
    raise SystemExit(0)
signal.signal(signal.SIGTERM,stopped)
path.write_text('ready')
time.sleep(10)
'''
        parent_code = '''
from functools import partial
import os,runpy,subprocess,sys,time
callback=runpy.run_path(sys.argv[1])['stop_on_parent_exit']
child=subprocess.Popen([sys.executable,'-c',sys.argv[2],sys.argv[3]],
                       preexec_fn=partial(callback,os.getpid()),start_new_session=True)
time.sleep(10)
'''
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / 'controller-status'
            parent = subprocess.Popen([sys.executable, '-c', parent_code, str(FIXTURE), child_code, str(marker)])
            try:
                end = time.monotonic() + 3
                while time.monotonic() < end and (not marker.exists() or marker.read_text() != 'ready'):
                    if parent.poll() is not None:
                        self.fail('collector fixture exited before controller readiness')
                    time.sleep(.01)
                self.assertTrue(marker.exists())
                self.assertEqual(marker.read_text(), 'ready')
                parent.kill()
                parent.wait(timeout=3)
                end = time.monotonic() + 3
                while time.monotonic() < end and marker.read_text() != 'stop_requested':
                    time.sleep(.01)
                self.assertEqual(marker.read_text(), 'stop_requested')
            finally:
                if parent.poll() is None:
                    parent.terminate()
                parent.wait(timeout=3)
