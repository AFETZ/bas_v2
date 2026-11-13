"""Fresh-process host entrypoint limits; no Docker, GPU or simulated flight."""
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from scripts.product import town01_runtime_monitor as monitor

ROOT = Path(__file__).resolve().parents[1]


def child_program(mode, inherited_stack=False):
    return """
import contextlib,io,json,os,pathlib,runpy,sys
ROOT=pathlib.Path(%r)
sys.path.insert(0,str(ROOT))
mode=%r
initial_mask=sorted(os.sched_getaffinity(0)) if hasattr(os,'sched_getaffinity') else None
if %r:
    from scripts.product.town01_runtime_monitor import cpu_partition
    os.sched_setaffinity(0,cpu_partition()['stack_cpus'])
caller_mask=sorted(os.sched_getaffinity(0)) if hasattr(os,'sched_getaffinity') else None
numpy_before='numpy' in sys.modules
captured=io.StringIO();errors=io.StringIO();entry_status=None
if mode in ('ui-bas','ui-direct'):
    target=ROOT/('bas.py' if mode=='ui-bas' else 'scripts/product/operator_ui.py')
    sys.argv=[str(target)]+(['ui','--port','0'] if mode=='ui-bas' else ['--serve','--port','0'])
    try:runpy.run_path(str(target),run_name='__main__')
    except SystemExit as error:
        if error.code not in (None,0):raise
elif mode=='imports':
    import bas
    from scripts.product import operator_ui,run_batch,town01_runtime_monitor
else:
    target=ROOT/('bas.py' if mode=='cli' else 'scripts/product/run_batch.py')
    sys.argv=[str(target)]+(['inspect','--config',str(ROOT/'runtime-terrain.json')] if mode=='cli' else
        ['--config',str(ROOT/'runtime-terrain.json'),'--batch-id','host-entrypoint-probe','--repeats','1','--dry-run'])
    with contextlib.redirect_stdout(captured),contextlib.redirect_stderr(errors):
        try:runpy.run_path(str(target),run_name='__main__')
        except SystemExit as error:
            entry_status=error.code
            if error.code not in (None,0):
                # Clean unit checkouts intentionally do not carry terrain assets.
                # The real lazy terrain reader has already imported NumPy.
                if error.code!=2 or 'scene-manifest.json' not in errors.getvalue():raise
tasks=pathlib.Path('/proc/self/task')
print(json.dumps(dict(mode=mode,entry_status=entry_status,missing_unit_terrain_assets='scene-manifest.json' in errors.getvalue(),numpy_before=numpy_before,numpy_loaded='numpy' in sys.modules,
    numpy_version=getattr(sys.modules.get('numpy'),'__version__',None),
    math_env={key:os.environ.get(key) for key in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS')},
    initial_mask=initial_mask,caller_mask=caller_mask,
    final_mask=sorted(os.sched_getaffinity(0)) if hasattr(os,'sched_getaffinity') else None,
    native_task_count=len(list(tasks.iterdir())) if tasks.exists() else None)))
""" % (str(ROOT), mode, inherited_stack)


class HostEntrypointTests(unittest.TestCase):
    def environment(self):
        return dict(os.environ, OPENBLAS_NUM_THREADS='32', OMP_NUM_THREADS='7',
                    PYTHONDONTWRITEBYTECODE='1')

    def run_child(self, mode, inherited_stack=False):
        done = subprocess.run([sys.executable, '-c', child_program(mode, inherited_stack)],
                              cwd=ROOT, env=self.environment(), text=True,
                              capture_output=True, timeout=15)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout.strip().splitlines()[-1])

    def assert_limited(self, observed):
        self.assertFalse(observed['numpy_before'])
        self.assertTrue(observed['numpy_loaded'])
        self.assertEqual(observed['math_env'],
                         dict(OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1'))
        self.assertEqual(observed['caller_mask'], observed['final_mask'])
        if observed['native_task_count'] is not None:
            self.assertEqual(observed['native_task_count'], 1)

    def test_library_import_does_not_change_the_embedding_process(self):
        observed = self.run_child('imports')
        self.assertEqual(observed['math_env'],
                         dict(OPENBLAS_NUM_THREADS='32', OMP_NUM_THREADS='7'))
        self.assertFalse(observed['numpy_loaded'])
        self.assertEqual(observed['initial_mask'], observed['final_mask'])

    def test_real_inspect_entrypoint_limits_before_lazy_numpy_import(self):
        self.assert_limited(self.run_child('cli'))

    def test_direct_batch_dry_run_limits_before_configuration_loading(self):
        self.assert_limited(self.run_child('batch'))

    @unittest.skipUnless(hasattr(os, 'sched_setaffinity'), 'Linux affinity inheritance fixture')
    def test_inherited_stack_is_not_partitioned_again(self):
        observed = self.run_child('cli', inherited_stack=True)
        self.assert_limited(observed)
        self.assertTrue(set(observed['caller_mask']).issubset(observed['initial_mask']))

    def test_repeated_helper_calls_do_not_use_linux_affinity_on_windows(self):
        with patch.dict(os.environ, self.environment()), patch.object(sys, 'platform', 'win32'):
            with patch.object(os, 'sched_setaffinity', side_effect=AssertionError('affinity changed'),
                              create=True), patch.object(os, 'sched_getaffinity',
                              side_effect=AssertionError('affinity queried'), create=True):
                monitor.limit_host_math_threads()
                monitor.limit_host_math_threads()
            self.assertEqual(os.environ['OPENBLAS_NUM_THREADS'], '1')
            self.assertEqual(os.environ['OMP_NUM_THREADS'], '1')

    @unittest.skipUnless(sys.platform == 'linux', 'Owned POSIX server shutdown fixture')
    def test_actual_ui_entrypoints_keep_single_math_thread_after_first_http_config(self):
        for mode in ('ui-bas', 'ui-direct'):
            with self.subTest(mode=mode):
                process = subprocess.Popen(
                    [sys.executable, '-c', child_program(mode, inherited_stack=True)],
                    cwd=ROOT, env=self.environment(), text=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    with selectors.DefaultSelector() as ready:
                        ready.register(process.stdout, selectors.EVENT_READ)
                        self.assertTrue(ready.select(10), 'UI did not report an ephemeral listener')
                    line = process.stdout.readline()
                    match = re.search(r'http://127\.0\.0\.1:(\d+)/', line)
                    self.assertIsNotNone(match, line)
                    base = f'http://127.0.0.1:{match[1]}/api/config?path='
                    with urllib.request.urlopen(base+'runtime.json', timeout=10) as response:
                        self.assertEqual(response.status, 200)
                        self.assertEqual(set(json.loads(response.read())), {'config', 'scene'})
                    try:
                        response = urllib.request.urlopen(base+'runtime-terrain.json', timeout=10)
                    except urllib.error.HTTPError as error:
                        # The dependency-import boundary must also be protected
                        # when a fresh checkout has not fetched terrain assets.
                        self.assertEqual(error.code, 404)
                        self.assertIn('scene-manifest.json', error.read().decode())
                    else:
                        with response:
                            self.assertEqual(response.status, 200)
                    process.send_signal(signal.SIGINT)
                    stdout, stderr = process.communicate(timeout=10)
                    self.assertEqual(process.returncode, 0, stderr)
                    self.assert_limited(json.loads(stdout.strip().splitlines()[-1]))
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.communicate(timeout=5)


if __name__ == '__main__':
    unittest.main()
