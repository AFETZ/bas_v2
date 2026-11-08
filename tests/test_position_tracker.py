"""Publication cadence on controlled clocks; no Gazebo or RF acceptance claim."""
from types import SimpleNamespace
import json
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from network.position_tracker import tracker


class ControlledClock:
    def __init__(self):
        self.now_ns = 1_000_000_000_000

    def monotonic_ns(self):
        return self.now_ns


class ControlledStop:
    def __init__(self, clock, interrupt_after_ns=None):
        self.clock, self.interrupt_after_ns = clock, interrupt_after_ns
        self.stopped = False
        self.waits_ns = []

    def set(self):
        self.stopped = True

    def is_set(self):
        return self.stopped

    def wait(self, seconds):
        if self.stopped:
            return True
        duration = round(seconds * 1e9)
        if duration < 0:
            raise AssertionError('negative publication wait')
        self.waits_ns.append(duration)
        if self.interrupt_after_ns is not None:
            self.clock.now_ns += min(duration, self.interrupt_after_ns)
            self.set()
        else:
            self.clock.now_ns += duration
        return self.stopped


class GazeboPublicationTests(unittest.TestCase):
    def test_live_file_is_compact_and_retains_exact_history_and_epochs(self):
        state=dict(published_monotonic_ns=123456789,clock_received_monotonic_ns=123456780,
                   nodes=[dict(id='uav1',sample_monotonic_ns=123456700,
                               history=[dict(sample_monotonic_ns=123456690,position_m=[1,2,3])])])
        with tempfile.TemporaryDirectory() as directory:
            live=Path(directory)/'node.json'
            archive=Path(directory)/'node.jsonl'
            tracker.write_state(live,archive,state)
            raw=live.read_text(encoding='utf-8')
            self.assertEqual(json.loads(raw),state)
            self.assertEqual(raw,json.dumps(state,separators=(',',':'),sort_keys=True)+'\n')
            self.assertFalse(live.with_suffix('.json.tmp').exists())
            saved=json.loads(archive.read_text(encoding='utf-8'))
            self.assertEqual(saved['published_monotonic_ns'],123456789)
            self.assertEqual(saved['nodes'][0]['sample_monotonic_ns'],123456700)
            self.assertNotIn('history',saved['nodes'][0])

    def test_serialization_time_is_inside_the_requested_publication_period(self):
        for period_ns in (20_000_000, 10_000_000):
            clock = ControlledClock()
            stop, published = ControlledStop(clock), []
            def publish():
                published.append(clock.now_ns)
                clock.now_ns += 3_000_000  # Real snapshot/serialization/write cost being diagnosed.
                if len(published) == 5:
                    stop.set()
            with self.subTest(period_ns=period_ns), patch.object(tracker.time, 'monotonic_ns', clock.monotonic_ns):
                tracker._publish_periodically(stop, period_ns, publish)
            self.assertEqual([value - published[0] for value in published],
                             [i * period_ns for i in range(5)])
            self.assertEqual(stop.waits_ns, [period_ns - 3_000_000] * 4)

    def test_overrun_skips_missed_ticks_without_catch_up_publications(self):
        clock = ControlledClock()
        stop, published = ControlledStop(clock), []
        def publish():
            published.append(clock.now_ns)
            clock.now_ns += 47_000_000 if len(published) == 1 else 3_000_000
            if len(published) == 4:
                stop.set()
        with patch.object(tracker.time, 'monotonic_ns', clock.monotonic_ns):
            tracker._publish_periodically(stop, 20_000_000, publish)
        self.assertEqual([value - published[0] for value in published], [0, 60_000_000, 80_000_000, 100_000_000])
        self.assertEqual(stop.waits_ns, [13_000_000, 17_000_000, 17_000_000])

    def test_stop_interrupts_wait_and_prevents_the_next_publication(self):
        clock = ControlledClock()
        stop = ControlledStop(clock, interrupt_after_ns=5_000_000)
        published = []
        def publish():
            published.append(clock.now_ns)
            clock.now_ns += 3_000_000
        with patch.object(tracker.time, 'monotonic_ns', clock.monotonic_ns):
            tracker._publish_periodically(stop, 20_000_000, publish)
        self.assertEqual(len(published), 1)
        self.assertEqual(clock.now_ns - published[0], 8_000_000)
        self.assertEqual(stop.waits_ns, [17_000_000])
        with patch.object(tracker.time, 'monotonic_ns', clock.monotonic_ns):
            tracker._publish_periodically(stop, 20_000_000, publish)
        self.assertEqual(len(published), 1)

    def test_invalid_rate_is_rejected_before_native_imports_or_node_creation(self):
        for rate in (0, -1, float('nan'), float('inf'), -float('inf'), True, '50', 1e300, 1e-300):
            with self.subTest(rate=rate), self.assertRaisesRegex(ValueError, 'rate'):
                tracker.run_gazebo_tracker(SimpleNamespace(rate_hz=rate))


class LiveJournalTests(unittest.TestCase):
    """Real atomic files/shared writer; blocked archive I/O is event-controlled."""
    class StreamProbe:
        def __init__(self, stream, failure=None):
            self.stream, self.failure = stream, failure
            self.entered, self.release = threading.Event(), threading.Event()

        def write(self, value):
            self.entered.set()
            if not self.release.wait(3):
                raise OSError('test archive gate was not released')
            if self.failure == 'write':
                raise OSError('injected archive write failure')
            return self.stream.write(value)

        def flush(self):
            if self.failure == 'flush':
                raise OSError('injected archive flush failure')
            return self.stream.flush()

        def close(self):
            self.stream.close()

        def __getattr__(self, name):
            return getattr(self.stream, name)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.live, self.archive = self.root/'node_state.json', self.root/'node_state.jsonl'
        self.audit = Path(str(self.archive)+'.writer.json')
        self.state = tracker.LiveState(dict(command_post=dict(position_m=[0,0,1]), robots=[dict(name='uav1')]))
        self.state.source = 'gazebo_odometry'
        self.timing = tracker._publication_timing()
        self.probes, self.writers = [], []
        self.addCleanup(self.cleanup_writers)

    def cleanup_writers(self):
        for probe in self.probes:
            probe.release.set()
        for writer in self.writers:
            try: writer.close()
            except RuntimeError: pass  # Sticky errors are asserted, never erased.
            self.assertFalse(writer.snapshot()['listener_alive'], 'Archive listener leaked from the test')

    def gate(self, writer, failure=None):
        self.writers.append(writer)
        probe = self.StreamProbe(writer._file_handler.stream, failure)
        writer._file_handler.stream = probe
        self.probes.append(probe)
        return probe

    def sample(self, published_ns, index=0):
        stamp = SimpleNamespace(sec=1, nanosec=index*10_000_000)
        vector = lambda **kw: SimpleNamespace(**kw)
        message = SimpleNamespace(header=SimpleNamespace(stamp=stamp, frame_id='odom'), child_frame_id='base_link',
            pose=SimpleNamespace(pose=SimpleNamespace(position=vector(x=index,y=2,z=3),orientation=vector(x=0,y=0,z=0,w=1))),
            twist=SimpleNamespace(twist=SimpleNamespace(linear=vector(x=0,y=0,z=0),angular=vector(x=0,y=0,z=0))))
        received_ns = published_ns-2_000_000
        self.state.clock(1+index*.01, received_ns)
        self.assertTrue(self.state.odometry('uav1', message, received_ns))
        return self.state.snapshot(published_ns)

    def test_blocked_archive_does_not_stop_atomic_publication_and_all_original_rows_drain(self):
        clock, published = ControlledClock(), []
        stop = ControlledStop(clock)
        with tracker._live_journal(self.live, self.archive, self.state, self.timing) as journal:
            probe = self.gate(journal)
            def publish():
                published.append(clock.now_ns)
                tracker._publish_live_state(self.live, self.sample(clock.now_ns,len(published)-1), journal, self.timing)
                if len(published)==1:
                    self.assertTrue(probe.entered.wait(1))
                if len(published)==3:
                    stop.set()
            try:
                with patch.object(tracker.time, 'monotonic_ns', clock.monotonic_ns):
                    tracker._publish_periodically(stop, 10_000_000, publish)
                self.assertFalse(probe.release.is_set())
                self.assertEqual(journal.snapshot()['accepted'],3)
                self.assertEqual(journal.snapshot()['written'],0)
                self.assertEqual([t-published[0] for t in published],[0,10_000_000,20_000_000])
                live=json.loads(self.live.read_text())
                self.assertEqual(live['published_monotonic_ns'],published[-1])
                self.assertEqual(live['nodes'][1]['sample_monotonic_ns'],published[-1]-2_000_000)
                self.assertEqual(live['journal_mode'],tracker.JOURNAL_MODE)
                self.assertEqual(len(live['nodes'][1]['history']),3)
                self.assertEqual(json.loads(self.audit.read_text())['status'],'recording')
            finally:
                probe.release.set()
        rows=[json.loads(line) for line in self.archive.read_text().splitlines()]
        self.assertEqual([r['published_monotonic_ns'] for r in rows],published)
        self.assertEqual([r['nodes'][1]['sample_monotonic_ns'] for r in rows],[t-2_000_000 for t in published])
        self.assertTrue(all('history' not in r['nodes'][1] for r in rows))
        audit=json.loads(self.audit.read_text())
        self.assertEqual((audit['status'],audit['source'],audit['session_id']),('closed',self.state.source,self.state.session))
        self.assertEqual((audit['accepted'],audit['written'],audit['failed'],audit['rejected'],audit['error_count']),(3,3,0,0,0))
        self.assertTrue(audit['closed'] and audit['drained'])
        self.assertFalse(audit['listener_alive'])
        self.assertEqual(audit['written_bytes'],len(self.archive.read_bytes()))
        self.assertEqual(audit['sha256'],hashlib.sha256(self.archive.read_bytes()).hexdigest())
        self.assertEqual(audit['publication_timing']['atomic_publish_calls'],3)
        self.assertEqual(audit['publication_timing']['archive_enqueue_calls'],3)
        self.assertNotEqual(audit['producer']['native_thread_id'],audit['writer']['native_thread_id'])

    def test_overflow_is_fatal_and_final_audit_retains_failure_after_successful_drain(self):
        original = tracker.AsyncJsonlWriter
        with patch.object(tracker, 'AsyncJsonlWriter', lambda path:original(path,capacity=1)):
            with self.assertRaisesRegex(RuntimeError, 'not accepted'):
                with tracker._live_journal(self.live,self.archive,self.state,self.timing) as journal:
                    probe=self.gate(journal)
                    tracker._publish_live_state(self.live,self.sample(1_000_000_000),journal,self.timing)
                    self.assertTrue(probe.entered.wait(1))
                    try:
                        tracker._publish_live_state(self.live,self.sample(1_010_000_000,1),journal,self.timing)
                        tracker._publish_live_state(self.live,self.sample(1_020_000_000,2),journal,self.timing)
                    finally:
                        probe.release.set()
        audit=json.loads(self.audit.read_text())
        self.assertEqual(audit['status'],'failed')
        self.assertIn('not accepted',audit['producer_error'])
        self.assertIsNotNone(audit['close_error'])
        self.assertEqual((audit['accepted'],audit['written'],audit['rejected']),(2,2,1))
        self.assertEqual(audit['first_exception']['stage'],'queue_full')
        self.assertTrue(audit['closed'] and audit['drained'])

    def test_actual_archive_write_failure_is_not_a_successful_capture(self):
        with self.assertRaises(RuntimeError):
            with tracker._live_journal(self.live,self.archive,self.state,self.timing) as journal:
                probe=self.gate(journal,'write'); probe.release.set()
                tracker._publish_live_state(self.live,self.sample(1_000_000_000),journal,self.timing)
        audit=json.loads(self.audit.read_text())
        self.assertEqual(audit['status'],'failed')
        self.assertIsNotNone(audit['close_error'])
        self.assertEqual((audit['accepted'],audit['written'],audit['failed']),(1,0,1))
        self.assertEqual(audit['first_exception']['stage'],'file_write_or_flush')

    def test_timed_out_drain_keeps_initial_failure_and_never_claims_closed(self):
        with self.assertRaisesRegex(RuntimeError,'did not close'):
            with tracker._live_journal(self.live,self.archive,self.state,self.timing) as journal:
                probe=self.gate(journal)
                original_close=journal.close
                journal.close=lambda: original_close(timeout_s=.01)
                tracker._publish_live_state(self.live,self.sample(1_000_000_000),journal,self.timing)
                self.assertTrue(probe.entered.wait(1))
        audit=json.loads(self.audit.read_text())
        self.assertEqual(audit['status'],'failed')
        self.assertFalse(audit['closed'] or audit['drained'])
        self.assertTrue(audit['listener_alive'])
        probe.release.set()
        with self.assertRaises(RuntimeError):original_close()
        self.assertFalse(journal.snapshot()['listener_alive'])

    def test_source_failure_does_not_become_success_after_clean_archive_close(self):
        with self.assertRaisesRegex(RuntimeError,'pose source failed'):
            with tracker._live_journal(self.live,self.archive,self.state,self.timing):
                raise RuntimeError('pose source failed')
        audit=json.loads(self.audit.read_text())
        self.assertEqual(audit['status'],'failed')
        self.assertIn('pose source failed',audit['producer_error'])
        self.assertIsNone(audit['close_error'])
        self.assertEqual(audit['error_count'],0)

    def test_exclusive_live_archive_does_not_change_oneoff_append_behavior(self):
        sample=self.sample(1_000_000_000)
        tracker.write_state(self.live,self.archive,sample)
        tracker.write_state(self.live,self.archive,sample)
        original=self.archive.read_bytes()
        self.assertEqual(len(original.splitlines()),2)
        with self.assertRaises(FileExistsError):
            with tracker._live_journal(self.live,self.archive,self.state,self.timing):pass
        self.assertEqual(self.archive.read_bytes(),original)
        self.assertFalse(self.audit.exists())

    def test_direct_script_loads_shared_writer_without_project_in_pythonpath(self):
        import subprocess
        import sys
        environment=os.environ.copy()
        environment['PYTHONPATH']=os.pathsep.join(p for p in environment.get('PYTHONPATH','').split(os.pathsep)
            if p and Path(p).resolve()!=tracker.ROOT_DIR)
        value=subprocess.run([sys.executable,str(Path(tracker.__file__).resolve()),'--help'],cwd=self.root,
            env=environment,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=5)
        self.assertEqual(value.returncode,0,value.stderr)
        self.assertIn('--transport',value.stdout)


if __name__ == '__main__':
    unittest.main()
