"""Real Linux sessions/processes; no simulator, ports, devices or GPU required."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


def live(pid):
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(') ', 1)[1].split()
        return fields[0] != 'Z'
    except (OSError, IndexError):
        return False


@unittest.skipUnless(sys.platform.startswith('linux') and Path('/proc').is_dir(),
                     'Linux procfs/session regression; Windows does not qualify POSIX teardown')
class NativeProcessCleanupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.run = self.root / 'owned run'
        self.run.mkdir()
        self.children = []
        self.leaders = []
        self.addCleanup(self.cleanup)
        script = (ROOT / 'network/ns3/run_native_radio_five_uav.sh').read_text()
        self.function = script[script.index('stop_pid() {'):].split('\n}\n', 1)[0] + '\n}\n'

    def cleanup(self):
        # Only exact children recorded by these fixtures, never a process search.
        for pid in self.children:
            if live(pid):
                os.kill(pid, signal.SIGKILL)
        for process in self.leaders:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)

    def group(self, directory, *, fatal=False, ignore_term=False):
        directory.mkdir(parents=True, exist_ok=True)
        record = directory / 'child.pid'
        payload = (
            'import os, pathlib, resource, signal, subprocess, sys, time; '
            'resource.setrlimit(resource.RLIMIT_CORE, (0,0)); '
            'signal.signal(signal.SIGTERM, signal.SIG_IGN) if sys.argv[3]=="1" else None; '
            'child=subprocess.Popen(["sleep","30"]); '
            'pathlib.Path(sys.argv[1]).write_text(str(child.pid)); '
            'os.abort() if sys.argv[2]=="1" else time.sleep(30)')
        log = directory / 'output.log'
        with log.open('wb') as stream:
            process = subprocess.Popen([sys.executable, '-c', payload, str(record), str(int(fatal)), str(int(ignore_term))],
                start_new_session=True, stdout=stream, stderr=subprocess.STDOUT)
        self.leaders.append(process)
        end = time.monotonic() + 3
        while not record.exists() and time.monotonic() < end:
            time.sleep(.01)
        self.assertTrue(record.exists())
        child = int(record.read_text())
        self.children.append(child)
        self.assertEqual(os.getpgid(child), process.pid)
        self.assertEqual(os.getsid(child), process.pid)
        return process, child, log

    def stop(self, pid):
        environment = dict(os.environ, RUN_DIR=str(self.run), RUNTIME_DIR=str(self.run / 'runtime'))
        result = subprocess.run(['bash', '-c', self.function + '\nstop_pid "$1"\nstop_pid "$1"\n',
            'cleanup-regression', str(pid)], env=environment, capture_output=True, text=True, timeout=12)
        self.assertEqual(result.returncode, 0, result.stderr)

    def assert_stopped(self, pid):
        deadline = time.monotonic() + 1
        while live(pid) and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(live(pid))

    def test_fatal_session_leader_does_not_leave_its_term_ignoring_descendant(self):
        leader, child, _ = self.group(self.run, fatal=True, ignore_term=True)
        leader.wait(timeout=3)
        self.assertLess(leader.returncode, 0)
        self.assertTrue(live(child))
        unrelated, unrelated_child, _ = self.group(self.root / 'unrelated')
        self.stop(leader.pid)
        self.assert_stopped(child)
        self.assertTrue(live(unrelated.pid))
        self.assertTrue(live(unrelated_child))

    def test_normal_group_and_deleted_owned_log_are_cleaned_and_repeat_is_safe(self):
        leader, child, log = self.group(self.run)
        log.unlink()  # Open redirected fd remains run-owned after log rotation.
        self.stop(leader.pid)
        leader.wait(timeout=3)
        self.assert_stopped(child)

    def test_unowned_group_with_similar_path_is_refused_even_for_live_leader(self):
        other, child, _ = self.group(self.root / 'owned run-other')
        self.stop(other.pid)
        self.assertTrue(live(other.pid))
        self.assertTrue(live(child))


if __name__ == '__main__':
    unittest.main()
