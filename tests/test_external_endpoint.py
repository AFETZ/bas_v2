"""Real localhost transport checks; these do not qualify physical radio-HitL."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from network.scripts.external_endpoint import IO_BATCH, flush_queue, open_radio_socket
from network.scripts.serial_transport import BoundedQueue, Encoder, Reassembler


ROOT = Path(__file__).resolve().parents[1]


def udp_socket():
    value = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    value.bind(('127.0.0.1', 0))
    value.settimeout(2)
    return value


def free_udp_address():
    with udp_socket() as value:
        return value.getsockname()


class ExternalEndpointTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)

    def test_radio_socket_retains_existing_control_payload_tos(self):
        for channel, expected in (('control', 184), ('payload', 40)):
            config = dict(channel=channel, radio=dict(bind=['127.0.0.1', 0]))
            with self.subTest(channel=channel), open_radio_socket(config) as radio:
                self.assertEqual(radio.getsockopt(socket.IPPROTO_IP, socket.IP_TOS), expected)
        for value in (True, -1, 256, '184'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                open_radio_socket(dict(channel='control', radio=dict(bind=['127.0.0.1', 0], tos=value)))

    def start_adapter(self, endpoint, peer, duration=2.0):
        radio_address = free_udp_address()
        config = dict(endpoint=endpoint, radio=dict(bind=radio_address, peer=peer),
                      channel='payload', uav_id=2,
                      queues=dict(packets=4096, bytes=4 * 1024 * 1024, deadline_s=2),
                      watchdog_s=3, reconnect_s=.05)
        path = self.folder / 'config.json'
        path.write_text(json.dumps(config))
        process = subprocess.Popen([sys.executable, str(ROOT / 'network/scripts/external_endpoint.py'),
                                    '--config', str(path), '--output', str(self.folder / 'out'),
                                    '--duration-s', str(duration)], cwd=ROOT,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop_process, process)
        return process, radio_address

    @staticmethod
    def stop_process(process):
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=3)

    def wait_ready(self, process, reconnects=1):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            # Observe the append-only readiness events. On Windows opening the
            # destination of os.replace can prevent atomic metrics publication.
            path = self.folder / 'out/events.jsonl'
            if path.exists():
                rows = [json.loads(line) for line in path.read_text().splitlines() if line]
                connected = [row for row in rows if row['event'] == 'connected']
                if len(connected) >= reconnects:
                    return connected[-1]
            if process.poll() is not None:
                self.fail(process.communicate()[1])
            time.sleep(.01)
        self.fail('adapter did not become ready')

    def finish(self, process):
        _, error = process.communicate(timeout=6)
        self.assertEqual(process.returncode, 0, error)
        return json.loads((self.folder / 'out/metrics.json').read_text())

    def test_partial_tcp_writes_preserve_original_bytes_and_deadline(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            with socket.create_connection(listener.getsockname()) as writer, listener.accept()[0] as reader:
                writer.setblocking(False)
                reader.settimeout(1)
                payload = bytes(range(256)) * 20
                stamp = time.monotonic()
                queue = BoundedQueue(2, len(payload), 2)
                queue.put(payload, stamp)
                received = bytearray()
                while queue.items:
                    sent, operations, _ = flush_queue(queue, lambda data: writer.send(data[:17]))
                    self.assertLessEqual(operations, IO_BATCH)
                    if queue.items:
                        self.assertEqual(queue.items[0][1], stamp)
                    while sent:
                        data = reader.recv(min(sent, 31))
                        received.extend(data)
                        sent -= len(data)
                self.assertEqual(received, payload)
                self.assertEqual(queue.bytes, 0)

    def test_real_tcp_backpressure_keeps_unsent_data_and_deadline_expires_it(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            with socket.create_connection(listener.getsockname()) as writer, listener.accept()[0] as reader:
                writer.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)
                reader.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
                writer.setblocking(False)
                # Winsock may accept one very large write despite a small
                # SO_SNDBUF. Fill with small writes until actual EWOULDBLOCK.
                for _ in range(1024):
                    try:
                        writer.send(b'x' * 4096)
                    except BlockingIOError:
                        break
                else:
                    self.fail('could not establish TCP backpressure within 4 MiB')
                payload = b'pending-command'
                stamp = time.monotonic()
                queue = BoundedQueue(2, len(payload), 1)
                queue.put(payload, stamp)
                sent, _, _ = flush_queue(queue, writer.send)
                self.assertEqual(sent, 0)
                self.assertGreater(queue.bytes, 0)
                self.assertEqual(queue.items[0][0], payload[sent:])
                self.assertEqual(queue.items[0][1], stamp)
                queue.expire(stamp + 2)
                self.assertFalse(queue.items)
                self.assertEqual(queue.expired, 1)

    def test_udp_short_send_is_never_retried_as_a_shortened_datagram(self):
        queue = BoundedQueue(2, 1024, 1)
        queue.put(b'datagram', time.monotonic())
        with self.assertRaisesRegex(OSError, 'short UDP'):
            flush_queue(queue, lambda data: 3, datagram=True)
        self.assertEqual(queue.items[0][0], b'datagram')

    def test_udp_adapter_preserves_datagram_records_in_both_directions(self):
        with udp_socket() as radio, udp_socket() as endpoint:
            endpoint_address = free_udp_address()
            process, radio_address = self.start_adapter(
                dict(kind='udp', bind=endpoint_address, peer=endpoint.getsockname()), radio.getsockname())
            self.wait_ready(process)
            original = [bytes(range(256)) * 2, b'second\x00datagram']
            for data in original:
                endpoint.sendto(data, endpoint_address)
            decoder = Reassembler(channel='payload', uav_id=2, direction='uart_to_gcs')
            received = []
            while len(received) < len(original):
                data, _ = radio.recvfrom(65535)
                received.extend(decoder.ingest(data))
            self.assertEqual(received, original)
            encoder = Encoder(channel='payload', uav_id=2, direction='gcs_to_uart')
            downlink = [b'a' * 1000, b'another\x00record']
            for data in downlink:
                for frame in encoder.encode(data):
                    radio.sendto(frame, radio_address)
            self.assertEqual([endpoint.recvfrom(65535)[0] for _ in downlink], downlink)
            metrics = self.finish(process)
            self.assertEqual(metrics['radio_to_endpoint_bytes'], sum(map(len, downlink)))
            self.assertEqual(metrics['queue_drops'], 0)
            self.assertEqual(metrics['hardware_validation'], 'not_performed')

    def test_tcp_reconnect_rejects_radio_bytes_from_previous_connection(self):
        with udp_socket() as radio, socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            listener.settimeout(3)
            process, radio_address = self.start_adapter(dict(kind='tcp_client', peer=listener.getsockname()),
                                                        radio.getsockname(), duration=3)
            first, _ = listener.accept()
            self.wait_ready(process)
            first.sendall(b'prefix')
            decoder = Reassembler(channel='payload', uav_id=2, direction='uart_to_gcs')
            self.assertEqual(decoder.ingest(radio.recvfrom(65535)[0]), [b'prefix'])
            encoder = Encoder(channel='payload', uav_id=2, direction='gcs_to_uart')
            old = encoder.encode(b'stale-command')
            first.close()
            with listener.accept()[0] as second:
                self.wait_ready(process, reconnects=2)
                second.settimeout(1.5)
                for frame in old:
                    radio.sendto(frame, radio_address)
                for frame in encoder.encode(b'fresh-command'):
                    radio.sendto(frame, radio_address)
                self.assertEqual(second.recv(4096), b'fresh-command')
            metrics = self.finish(process)
            self.assertGreaterEqual(metrics['connection_epoch_drops'], 1)
            self.assertGreaterEqual(metrics['reconnects'], 2)

    def test_tcp_to_radio_burst_integrity_and_measured_goodput(self):
        with udp_socket() as radio, socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            radio.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            listener.settimeout(3)
            process, _ = self.start_adapter(dict(kind='tcp_client', peer=listener.getsockname()),
                                            radio.getsockname(), duration=4)
            with listener.accept()[0] as source:
                self.wait_ready(process)
                payload = bytes(range(256)) * 4096
                errors = []
                def produce():
                    try:
                        source.sendall(payload)
                    except OSError as error:
                        errors.append(str(error))
                sender = threading.Thread(target=produce, daemon=True)
                started = time.monotonic()
                sender.start()
                decoder = Reassembler(channel='payload', uav_id=2, direction='uart_to_gcs',
                                      timeout_ms=1000, max_age_ms=2000)
                received = bytearray()
                while len(received) < len(payload):
                    data, _ = radio.recvfrom(65535)
                    for record in decoder.ingest(data):
                        received.extend(record)
                elapsed = time.monotonic() - started
                sender.join(timeout=1)
                self.assertFalse(sender.is_alive())
                self.assertFalse(errors)
                self.assertEqual(received, payload)
            metrics = self.finish(process)
            self.assertEqual(metrics['queue_drops'], 0)
            result = dict(probe='localhost_tcp_to_bsf1_udp', bytes=len(received), wall_s=elapsed,
                          useful_mbps=len(received) * 8 / elapsed / 1e6,
                          platform=sys.platform, python=sys.version.split()[0],
                          hardware_validation='not_performed', radio_model='not_in_path')
            print(json.dumps(result), flush=True)
            artifact = os.environ.get('BAS_EXTERNAL_TEST_ARTIFACT_DIR')
            if artifact:
                folder = Path(artifact)
                folder.mkdir(parents=True, exist_ok=True)
                (folder / 'external-throughput.json').write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    unittest.main()
