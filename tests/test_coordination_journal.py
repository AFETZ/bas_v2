"""Actual journal/UDP boundaries; no simulator or physical hardware required."""
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import bas
from network.jsonl import AsyncJsonlWriter
from network.coordination.service import Events, Network, build_parser
from network.coordination.policy import decode


class BlockedFile:
    """Delay the real stream.write, keeping its actual file/flush implementation."""
    def __init__(self, stream):
        self.stream = stream
        self.entered, self.release = threading.Event(), threading.Event()

    def write(self, value):
        self.entered.set()
        if not self.release.wait(3):
            raise TimeoutError('test did not release real journal stream')
        return self.stream.write(value)

    def __getattr__(self, name):
        return getattr(self.stream, name)


class CoordinationJournalTests(unittest.TestCase):
    def setUp(self):
        artifact_root = os.environ.get('BAS_COORDINATION_JOURNAL_TEST_ARTIFACT_DIR')
        if artifact_root and self._testMethodName == 'test_five_actual_udp_probes_complete_while_real_journal_write_is_blocked':
            self.run = Path(artifact_root).resolve()/'five_udp_probes'
            self.run.mkdir(parents=True, exist_ok=False)
        else:
            self.temporary = tempfile.TemporaryDirectory()
            self.addCleanup(self.temporary.cleanup)
            self.run = Path(self.temporary.name)
        self.path = self.run / 'logs/coordination_gcs.jsonl'

    def test_five_actual_udp_probes_complete_while_real_journal_write_is_blocked(self):
        peers = []
        for i in range(1, 6):
            peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.addCleanup(peer.close)
            peer.bind((f'127.0.0.{i+1}', peers[0].getsockname()[1] if peers else 0))
            peer.settimeout(1)
            peers.append(peer)
        args = build_parser().parse_args(['gcs', '--run-id', self.run.name,
            '--event-log', str(self.path), '--port', str(peers[0].getsockname()[1])])
        events = Events(self.path, args.run_id, 0)
        # The fallback deliberately permits this regression to exercise the old
        # synchronous Events class on the baseline revision (RED).
        owner = events.writer._file_handler if hasattr(events, 'writer') else events
        blocked = BlockedFile(owner.stream)
        owner.stream = blocked
        errors = []
        with patch('network.coordination.service.endpoint_ip', lambda i: f'127.0.0.{i+1}'), \
             patch('network.coordination.service.signal.signal'):
            node = Network(args, 0, events)
            self.addCleanup(node.close)
            actual_send = node.send
            def send_after_writer_started(kind, body, destination):
                if destination == 2 and not blocked.entered.wait(1):
                    raise AssertionError('first actual file write did not start')
                return actual_send(kind, body, destination)
            node.send = send_after_writer_started
            issued = time.monotonic_ns()
            def produce():
                try:
                    node.periodic(issued)
                except BaseException as error:
                    errors.append(error)
            producer = threading.Thread(target=produce)
            producer.start()
            try:
                self.assertTrue(blocked.entered.wait(1))
                producer.join(.25)
                self.assertFalse(producer.is_alive(), 'file write blocked the five-peer producer')
                self.assertEqual(errors, [])
                received = [decode(peer.recv(65535)) for peer in peers]
                self.assertTrue(all(kind == 'probe' and sender == 0 and body['sent_ns'] == issued
                                    for kind, sender, body in received))
                released = time.monotonic_ns()
            finally:
                blocked.release.set()
                producer.join(2)
                events.close() if hasattr(events, 'close') else events.stream.close()
        rows = [json.loads(line) for line in self.path.read_text().splitlines()]
        self.assertEqual([row['destination_id'] for row in rows], list(range(1, 6)))
        self.assertTrue(all(issued <= row['host_monotonic_ns'] <= released for row in rows))
        self.assertTrue(all(row['send_ok'] for row in rows))
        audit = json.loads(events.audit_path.read_text())
        self.assertEqual(audit['status'], 'closed')
        self.assertEqual((audit['accepted'], audit['written']), (5, 5))
        self.assertEqual(audit['sha256'], hashlib.sha256(self.path.read_bytes()).hexdigest())
        if os.environ.get('BAS_COORDINATION_JOURNAL_TEST_ARTIFACT_DIR'):
            (self.run/'result.json').write_text(json.dumps(dict(passed=True,
                scope='actual five loopback UDP probe sends with blocked actual journal stream.write; no RF/hardware claim',
                probe_count=5, issued_host_monotonic_ns=issued,
                writer_release_host_monotonic_ns=released,
                event_timestamps=[row['host_monotonic_ns'] for row in rows],
                audit_path=str(events.audit_path), journal_sha256=audit['sha256'],
                journal_accepted=audit['accepted'], journal_written=audit['written'],
                writer_closed=audit['closed'], writer_drained=audit['drained']), indent=2)+'\n')

    def test_queued_nested_fields_are_frozen_before_disk_write(self):
        events = Events(self.path, self.run.name, 0)
        blocked = BlockedFile(events.writer._file_handler.stream)
        events.writer._file_handler.stream = blocked
        values = {'peers': [1, 2]}
        try:
            events('original', values=values)
            self.assertTrue(blocked.entered.wait(1))
            values['peers'].append(3)
            events('next')
        finally:
            blocked.release.set()
            events.close()
        rows = [json.loads(line) for line in self.path.read_text().splitlines()]
        self.assertEqual([row['event'] for row in rows], ['original', 'next'])
        self.assertEqual(rows[0]['values'], {'peers': [1, 2]})

    def test_queue_full_fails_closed_and_retains_rejected_record_audit(self):
        with patch('network.coordination.service.AsyncJsonlWriter',
                   side_effect=lambda path: AsyncJsonlWriter(path, capacity=2)):
            events = Events(self.path, self.run.name, 0)
        blocked = BlockedFile(events.writer._file_handler.stream)
        events.writer._file_handler.stream = blocked
        try:
            events('in_progress')
            self.assertTrue(blocked.entered.wait(1))
            events('queued_one'); events('queued_two')
            with self.assertRaises(RuntimeError):
                events('rejected')
            node = Network.__new__(Network)
            node.emit = events
            with self.assertRaises(RuntimeError):
                node.periodic(0)  # Error is checked even before an early no-probe return.
        finally:
            blocked.release.set()
            with self.assertRaises(RuntimeError):
                events.close()
        audit = json.loads(events.audit_path.read_text())
        self.assertEqual(audit['status'], 'failed')
        self.assertEqual((audit['accepted'], audit['written'], audit['rejected']), (3, 3, 1))
        self.assertEqual(json.loads(audit['rejected_record'])['event'], 'rejected')
        self.assertTrue(audit['producer_error'])
        self.assertFalse(audit['listener_alive'])

    def test_real_file_write_error_is_checked_before_another_send_and_audited(self):
        events = Events(self.path, self.run.name, 0)
        stream = events.writer._file_handler.stream
        class FailedFile:
            def write(self, value):
                raise OSError('fixture disk write failure')
            def __getattr__(self, name):
                return getattr(stream, name)
        events.writer._file_handler.stream = FailedFile()
        events('offered')
        deadline = time.monotonic()+1
        while events.writer.snapshot()['error_count'] == 0 and time.monotonic() < deadline:
            time.sleep(.001)
        node = Network.__new__(Network)
        node.emit = events
        with self.assertRaises(RuntimeError):
            node.guard_radio()
        with self.assertRaises(RuntimeError):
            events.close()
        audit = json.loads(events.audit_path.read_text())
        self.assertEqual((audit['status'], audit['written'], audit['failed']), ('failed', 0, 1))
        self.assertEqual(len(audit['unwritten_records']), 1)
        self.assertEqual(audit['first_exception']['stage'], 'file_write_or_flush')
        self.assertFalse(audit['listener_alive'])

    def test_drain_timeout_persists_live_writer_and_never_becomes_valid(self):
        events = Events(self.path, self.run.name, 0)
        blocked = BlockedFile(events.writer._file_handler.stream)
        events.writer._file_handler.stream = blocked
        try:
            events('offered')
            self.assertTrue(blocked.entered.wait(1))
            with self.assertRaises(RuntimeError):
                events.close(timeout_s=.01)
            audit = json.loads(events.audit_path.read_text())
            self.assertEqual(audit['status'], 'failed')
            self.assertTrue(audit['listener_alive'])
            self.assertFalse(audit['drained'])
            self.assertTrue(audit['close_error'])
        finally:
            blocked.release.set()
            with self.assertRaises(RuntimeError):
                events.close()
        self.assertFalse(json.loads(events.audit_path.read_text())['listener_alive'])

    def test_existing_journal_is_preserved_and_restart_is_rejected(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b'previous immutable run\n')
        with self.assertRaises(FileExistsError):
            Events(self.path, self.run.name, 0)
        self.assertEqual(self.path.read_bytes(), b'previous immutable run\n')

    def test_service_constructor_failure_still_closes_and_audits_events(self):
        from network.coordination.service import main
        with patch('network.coordination.service.GCSService', side_effect=RuntimeError('init failed')):
            with self.assertRaisesRegex(RuntimeError, 'init failed'):
                main(['gcs', '--run-id', self.run.name, '--event-log', str(self.path)])
        audit = json.loads(self.path.with_name(self.path.name+'.writer.json').read_text())
        self.assertTrue(audit['closed'] and audit['drained'])
        self.assertEqual([json.loads(row)['event'] for row in self.path.read_text().splitlines()],
                         ['service_failed', 'service_stopped'])

    def test_service_socket_cleanup_failure_does_not_skip_writer_drain(self):
        from network.coordination.service import main
        service = SimpleNamespace(running=False, close=Mock(side_effect=OSError('socket close failed')))
        with patch('network.coordination.service.GCSService', return_value=service):
            with self.assertRaisesRegex(OSError, 'socket close failed'):
                main(['gcs', '--run-id', self.run.name, '--event-log', str(self.path)])
        audit = json.loads(self.path.with_name(self.path.name+'.writer.json').read_text())
        self.assertTrue(audit['closed'] and audit['drained'])
        self.assertEqual((audit['accepted'], audit['written']), (2, 2))

    def test_experiment_writer_failure_preserves_cleanup_summary_and_invalid_outcome(self):
        from scripts.product.relay_experiment import RelayExperiment, ScenarioError, StopRequested
        events = Events(self.path, self.run.name, 0)
        stream = events.writer._file_handler.stream
        class FailedFile:
            def write(self, value): raise OSError('fixture journal failure')
            def __getattr__(self, name): return getattr(stream, name)
        events.writer._file_handler.stream = FailedFile()
        mission = RelayExperiment.__new__(RelayExperiment)
        mission.args = SimpleNamespace(parameter_snapshots=False, fault_profile='none', run_dir=self.run)
        (self.run/'metrics').mkdir()
        mission.summary = dict(uavs={str(i): dict(armed=True, takeoff=True, landed=True, auto_disarm=True) for i in range(1, 6)})
        mission.prepare = Mock(side_effect=StopRequested)
        mission.measure, mission.land = Mock(), Mock()
        mission.source_s, mission.urban = 0, None
        mission.observer = SimpleNamespace(completed={})
        mission.phase = lambda name: events('phase_requested', phase=name)
        mission.emit, mission.csv_handle = events, Mock()
        mission.gcs = SimpleNamespace(close=Mock(side_effect=OSError('fixture socket close failure')))
        with self.assertRaises(ScenarioError):
            mission.run()
        mission.land.assert_called_once()
        mission.csv_handle.close.assert_called_once()
        summary = json.loads((self.run/'metrics/relay_experiment_summary.json').read_text())
        self.assertEqual((summary['status'], summary['runtime_status'], summary['measurement_validity']),
                         ('failed', 'failed', 'not_evaluated'))
        audit = json.loads(events.audit_path.read_text())
        self.assertEqual(audit['status'], 'failed')
        self.assertTrue(audit['closed'])
        self.assertFalse(audit['listener_alive'])


class CoordinationJournalVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.run = Path(self.temporary.name)
        (self.run/'environment.txt').write_text('uav_count=5\ncoordination_journal_mode=bounded_async_jsonl_v1\n')
        for node_id in range(6):
            name = 'gcs' if node_id == 0 else f'uav{node_id}'
            events = Events(self.run/f'logs/coordination_{name}.jsonl', self.run.name, node_id)
            events('service_started'); events('service_stopped'); events.close()
        self.path = self.run/'logs/coordination_uav5.jsonl'
        self.audit_path = self.path.with_name(self.path.name+'.writer.json')

    def test_actual_six_closed_writers_bind_row_identity_counts_bytes_and_sha(self):
        proof = bas.coordination_journal_verification(self.run)
        self.assertTrue(proof['passed'], proof)
        self.assertEqual(proof['expected_node_ids'], list(range(6)))
        self.assertEqual(set(proof['journals']), set(map(str, range(6))))
        self.assertTrue(all(row['rows'] == 2 for row in proof['journals'].values()))

    def test_missing_endpoint_or_sidecar_never_passes(self):
        for path in (self.path, self.audit_path):
            content = path.read_bytes(); path.unlink()
            try:
                self.assertFalse(bas.coordination_journal_verification(self.run)['passed'])
            finally:
                path.write_bytes(content)

    def test_failed_open_incomplete_wrong_identity_or_ledger_is_invalid(self):
        original = json.loads(self.audit_path.read_text())
        for changes in (dict(status='failed'), dict(closed=False), dict(drained=False),
            dict(listener_alive=True), dict(failed=1), dict(rejected=1), dict(error_count=1),
            dict(queue_depth=1), dict(producer_error='broken'), dict(close_error='timeout'),
            dict(accepted=3), dict(written_bytes=0), dict(sha256='0'*64), dict(node_id=4),
            dict(run_id='foreign'), dict(first_exception={}), dict(unwritten_records=['lost']),
            dict(in_progress_record='pending'), dict(rejected_record='lost')):
            with self.subTest(changes=changes):
                self.audit_path.write_text(json.dumps(dict(original, **changes)))
                self.assertFalse(bas.coordination_journal_verification(self.run)['passed'])
        self.audit_path.write_text(json.dumps(original))

    def test_same_length_tamper_and_missing_tail_are_invalid(self):
        original = self.path.read_bytes()
        for content in (original.replace(b'service_started', b'service_tampered'), original[:-1]):
            self.path.write_bytes(content)
            self.assertFalse(bas.coordination_journal_verification(self.run)['passed'])
        self.path.write_bytes(original)

    def test_foreign_row_cannot_pass_even_with_recomputed_file_hash(self):
        rows = self.path.read_text().splitlines()
        row = json.loads(rows[0]); row['node_id'] = 4
        rows[0] = json.dumps(row, separators=(',', ':'))
        self.path.write_text('\n'.join(rows)+'\n', encoding='utf-8', newline='\n')
        audit = json.loads(self.audit_path.read_text())
        audit['sha256'] = hashlib.sha256(self.path.read_bytes()).hexdigest()
        audit['accepted_bytes'] = audit['written_bytes'] = len(self.path.read_bytes())
        self.audit_path.write_text(json.dumps(audit))
        self.assertFalse(bas.coordination_journal_verification(self.run)['passed'])

    def test_missing_advertisement_does_not_hide_async_files_or_sidecars(self):
        (self.run/'environment.txt').write_text('uav_count=5\n')
        self.audit_path.unlink()
        self.assertFalse(bas.coordination_journal_verification(self.run)['passed'])
        for path in (self.run/'logs').glob('*.writer.json'):
            path.unlink()
        self.assertFalse(bas.coordination_journal_verification(self.run)['passed'])

    def test_advertised_but_all_missing_cannot_be_legacy(self):
        for path in (self.run/'logs').iterdir():
            path.unlink()
        self.assertFalse(bas.coordination_journal_verification(self.run)['passed'])
        (self.run/'environment.txt').write_text('uav_count=5\n')
        self.assertEqual(bas.coordination_journal_verification(self.run), dict(status='not_requested_legacy', passed=None))

    def test_duplicate_mode_count_and_extra_combined_file_are_invalid(self):
        original = (self.run/'environment.txt').read_text()
        for extra in ('coordination_journal_mode=bounded_async_jsonl_v1\n', 'uav_count=5\n'):
            (self.run/'environment.txt').write_text(original+extra)
            self.assertFalse(bas.coordination_journal_verification(self.run)['passed'])
        (self.run/'environment.txt').write_text(original)
        (self.run/'logs/coordination_events.jsonl').write_bytes(self.path.read_bytes())
        self.assertFalse(bas.coordination_journal_verification(self.run)['passed'])

    def test_actual_runtime_and_analyzer_use_the_same_fresh_verification(self):
        (self.run/'metrics').mkdir()
        (self.run/'metrics/native_radio_stats.json').write_text(json.dumps(dict(stop_reason='duration')))
        (self.run/'logs/native_capture_storage.json').write_text(json.dumps(dict(publication=dict(status='direct_disk', errors=[]))))
        (self.run/'configuration/final').mkdir(parents=True)
        (self.run/'configuration/final/manifest.json').write_text('{}')
        summary = dict(runtime_status='completed', mission_outcome='succeeded', measurement_validity='valid')
        expected = bas.coordination_journal_verification(self.run)
        outcome = bas.recorded_outcome(self.run, 0, summary)
        self.assertEqual(outcome['coordination_journal_verification'], expected)
        self.assertEqual(outcome['runtime_status'], 'completed')
        self.audit_path.unlink()
        outcome = bas.recorded_outcome(self.run, 0, summary)
        self.assertEqual((outcome['runtime_status'], outcome['measurement_validity']), ('failed', 'invalid'))
        self.assertIn('coordination_journal_invalid', outcome['runtime_verification_errors'])
        from scripts.product.analyze_relay_experiment import analyze_run
        analysis = analyze_run(self.run, plots=False, live_export=False)
        self.assertEqual(analysis['coordination_journal_verification'], outcome['coordination_journal_verification'])
        self.assertIn('coordination journals are not verified closed and complete', analysis['data_quality_errors'])

    def test_runner_advertises_only_when_coordination_services_are_requested(self):
        runner = Path(__file__).resolve().parents[1]/'network/ns3/run_native_radio_five_uav.sh'
        source = runner.read_text()
        block = re.search(r'  if \[\[ "\$COORDINATION_POLICY" != off \]\]; then\n'
                          r"    printf 'coordination_journal_mode=.*?\n  fi", source)
        self.assertIsNotNone(block)
        git_bash = Path(r'C:\Program Files\Git\bin\bash.exe')
        bash = str(git_bash) if os.name == 'nt' and git_bash.is_file() else shutil.which('bash')
        if not bash:
            self.skipTest('Bash is required to execute the actual runner advertisement block')
        for policy in ('off', 'fixed', 'failover'):
            output = subprocess.check_output([bash, '-c', block.group()],
                       env=dict(os.environ, COORDINATION_POLICY=policy), text=True)
            self.assertEqual(output, '' if policy == 'off' else 'coordination_journal_mode=bounded_async_jsonl_v1\n')


if __name__ == '__main__':
    unittest.main()
