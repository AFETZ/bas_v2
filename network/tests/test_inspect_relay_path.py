"""Small offline packet fixtures; these are not simulator or flight evidence."""
import base64
import hashlib
import json
from pathlib import Path
import shutil
import socket
import struct
import tempfile
import unittest

from network.scripts.serial_transport import Encoder
from scripts.product.inspect_relay_path import inspect


def digest(data):
    return hashlib.sha256(data).hexdigest()


def envelope(kind, sender, body):
    return json.dumps(dict(v=1, kind=kind, sender_id=sender, body=body), separators=(',', ':')).encode()


def frame(source, destination, data):
    mac = lambda i: bytes.fromhex(f'0271{i:02x}001010')
    ethernet = mac(destination) + mac(source) + b'\x08\x00'
    ip = struct.pack('!BBHHHBBH4s4s', 0x45, 0, 28 + len(data), 0, 0, 64, 17, 0,
                     socket.inet_aton(f'10.71.{source}.10'), socket.inet_aton(f'10.71.{destination}.10'))
    return ethernet + ip + struct.pack('!HHHH', 15000, 15000, 8 + len(data), 0) + data


def pcap(path, packets):
    with path.open('wb') as stream:
        stream.write(struct.pack('<IHHIIII', 0xa1b2c3d4, 2, 4, 0, 0, 65535, 1))
        for i, data in enumerate(packets):
            stream.write(struct.pack('<IIII', 1700000000, i * 1000, len(data), len(data)))
            stream.write(data)


def fixture(root, *, changed_forward=False, telemetry_origin=1):
    (root / 'logs').mkdir(); (root / 'pcap').mkdir()
    raw = b'opaque native UART command'
    body = dict(run_id='unit-fixture', epoch=2, leader_id=2, destination_id=1, sequence=9,
                payload=base64.b64encode(raw).decode(), created_ns=50)
    forwarded = {**body, 'payload': base64.b64encode(b'changed' if changed_forward else raw).decode()}
    telemetry = b'opaque actual UART telemetry'
    encoded = Encoder(channel='control', uav_id=1, direction='uart_to_gcs').encode(telemetry, 200)[0]
    returned = dict(run_id='unit-fixture', epoch=2, leader_id=2, origin_id=telemetry_origin,
                    producer_ns=200, grant_sequence=5, payload=base64.b64encode(encoded).decode())
    packets = [frame(0, 2, envelope('command', 0, body)),
               frame(2, 1, envelope('command', 2, forwarded)),
               frame(1, 2, envelope('telemetry', 1, returned)),
               frame(2, 0, envelope('telemetry', 2, returned))]
    for name, indices in {'gcs': [0, 3], 'uav1': [1, 2], 'uav2': [0, 1, 2, 3],
                          'uav3': [], 'uav4': [], 'uav5': []}.items():
        pcap(root / f'pcap/tap_{name}.pcap', [packets[i] for i in indices])
    sent = dict(run_id='unit-fixture', event='gcs_send', command_id='2:1:9', epoch=2,
                leader_id=2, destination_id=1, host_monotonic_ns=50,
                target_position_m=[0, 0, 12], payload_sha256=digest(raw))
    admission = {**sent, 'event': 'uart_admitted', 'node_id': 1, 'host_monotonic_ns': 100, 'bytes': len(raw)}
    logs = {'gcs': [sent, dict(event='gcs_receive', origin_id=1, payload_sha256=digest(encoded))],
            'uav1': [admission, dict(event='uart_observed', node_id=1, payload_sha256=digest(telemetry))]}
    for name in ['gcs', 'uav1', 'uav2', 'uav3', 'uav4', 'uav5']:
        (root / f'logs/coordination_{name}.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in logs.get(name, [])))


@unittest.skipUnless(shutil.which('tcpdump'), 'uses the existing tcpdump tool')
class RelayPathInspectionTests(unittest.TestCase):
    def test_correlates_two_native_hops_uart_hash_and_reassembled_return(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); fixture(root)
            value = inspect(root, epoch=2)
            self.assertEqual(value['inspected_destinations'], [1])
            self.assertEqual(value['target_bearing_commands'], 1)
            self.assertEqual(value['parse_notes'], {})
            self.assertIn(1, value['expected_followers'])  # former master remains a follower
            self.assertEqual(value['commands'][0]['command_hops'][0]['observations'][0]['pcap_unix_s'], '1700000000.000000')

    def test_changed_forwarded_bytes_do_not_form_an_unchanged_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); fixture(root, changed_forward=True)
            value = inspect(root, epoch=2)
            self.assertEqual(value['inspected_destinations'], [])
            self.assertFalse(value['commands'][0]['command_hops'][1]['payload_matches_uart_log'])

    def test_other_origin_telemetry_is_not_attributed_to_the_follower(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); fixture(root, telemetry_origin=3)
            value = inspect(root, epoch=2)
            self.assertIsNone(value['commands'][0]['subsequent_telemetry_return'])
            self.assertEqual(value['inspected_destinations'], [])


if __name__ == '__main__':
    unittest.main()
