"""OS UART verification on real PTYs; no physical hardware qualification."""
import json
import os
from pathlib import Path
import signal
import socket
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from network.scripts.external_endpoint import run, serial_baud_readback


class SerialBaudReadbackTests(unittest.TestCase):
    def test_nonposix_does_not_mislabel_the_cached_request_as_observed(self):
        device = SimpleNamespace(baudrate=115200, fileno=lambda: self.fail('POSIX descriptor on Windows'))
        with patch('network.scripts.external_endpoint.os.name', 'nt'):
            observed = serial_baud_readback(device)
        self.assertIsNone(observed['serial_baud_observed'])
        self.assertEqual(observed['serial_baud_observed_reason'], 'unsupported_os_termios_unavailable')

    def test_ambiguous_speed_codes_and_failed_readback_never_invent_a_rate(self):
        import sys
        device = SimpleNamespace(baudrate=115200, fileno=lambda: 123)
        for incoming, outgoing, reason in (
                (0, 0, 'custom_zero_or_unrecognized_termios_speed'),
                (4096, 4096, 'custom_zero_or_unrecognized_termios_speed'),
                (4097, 4098, 'termios_input_output_speed_mismatch')):
            api = SimpleNamespace(B57600=4097, B115200=4098, BOTHER=4096, error=OSError,
                                  tcgetattr=lambda _: [0, 0, 48, 0, incoming, outgoing, []])
            with self.subTest(incoming=incoming, outgoing=outgoing), patch.dict(sys.modules, termios=api), \
                    patch('network.scripts.external_endpoint.os.name', 'posix'):
                observed = serial_baud_readback(device)
            self.assertIsNone(observed['serial_baud_observed'])
            self.assertEqual(observed['serial_baud_observed_reason'], reason)
            self.assertEqual(observed['serial_termios']['input_speed'], incoming)
            self.assertEqual(observed['serial_termios']['output_speed'], outgoing)
        api.tcgetattr = lambda _: (_ for _ in ()).throw(OSError('readback failed'))
        with patch.dict(sys.modules, termios=api), patch('network.scripts.external_endpoint.os.name', 'posix'):
            observed = serial_baud_readback(device)
        self.assertIsNone(observed['serial_baud_observed'])
        self.assertEqual(observed['serial_baud_observed_reason'], 'termios_readback_error:OSError')


@unittest.skipUnless(hasattr(os, 'openpty'), 'actual POSIX UART required')
class SerialAppliedBaudTests(unittest.TestCase):
    def test_actual_pty_rates_ignore_a_different_cached_request(self):
        import termios
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        device = SimpleNamespace(baudrate=9600, fileno=lambda: slave)
        for rate in (57600, 115200, 230400):
            with self.subTest(rate=rate):
                attributes = termios.tcgetattr(slave)
                attributes[4:6] = [getattr(termios, f'B{rate}')] * 2
                termios.tcsetattr(slave, termios.TCSANOW, attributes)
                observed = serial_baud_readback(device)
                self.assertEqual(observed['serial_baud_observed'], rate)
                self.assertEqual(observed['serial_baud_observed_reason'], 'standard_termios_input_output_match')
                self.assertGreater(observed['serial_baud_readback_host_monotonic_ns'], 0)

    def test_failed_or_ambiguous_posix_readback_never_connects(self):
        import serial
        import termios
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        original_getattr, original_serial = termios.tcgetattr, serial.Serial
        opened = []

        def open_serial(*args, **kwargs):
            device = original_serial(*args, **kwargs)
            opened.append(device)
            return device

        for mode in ('error', 'different', 'unknown'):
            def readback(fd):
                attributes = original_getattr(fd)
                if any(device.is_open and device.fileno() == fd for device in opened):
                    if mode == 'error':
                        raise OSError('injected OS readback failure')
                    attributes[4:6] = [termios.B57600, termios.B115200] if mode == 'different' else [0, 0]
                return attributes
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as folder, \
                    socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as peer:
                peer.bind(('127.0.0.1', 0))
                config = dict(endpoint=dict(kind='serial', device=os.ttyname(slave), baud=115200,
                    bytesize=8, parity='N', stopbits=1), channel='control', uav_id=1,
                    radio=dict(bind=['127.0.0.1', 0], peer=peer.getsockname()),
                    queues=dict(packets=4, bytes=4096, deadline_s=1), watchdog_s=1, reconnect_s=.01)
                handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
                try:
                    with patch('serial.Serial', side_effect=open_serial), patch('termios.tcgetattr', side_effect=readback):
                        result = run(config, Path(folder), duration=.025)
                finally:
                    for sig, handler in handlers.items():
                        signal.signal(sig, handler)
                    for device in opened:
                        self.assertFalse(device.is_open)
                        device.close()
                self.assertIsNone(result['serial_baud_observed'])
                self.assertEqual(result['reconnects'], 0)
                self.assertEqual(result['endpoint_to_radio_bytes'], 0)
                self.assertEqual(result['radio_to_endpoint_bytes'], 0)

    def test_wrong_applied_speed_cannot_connect_or_forward_bytes(self):
        import serial
        import termios
        import tty

        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        tty.setraw(slave)
        os.set_blocking(master, False)
        opened = []
        real_serial = serial.Serial

        def wrong_baud(*args, **kwargs):
            device = real_serial(*args, **kwargs)
            opened.append(device)
            attributes = termios.tcgetattr(device.fileno())
            attributes[4:6] = [termios.B57600, termios.B57600]
            termios.tcsetattr(device.fileno(), termios.TCSANOW, attributes)
            self.assertEqual(device.baudrate, 115200)  # Cached requested value.
            os.write(master, b'pending-uplink')
            return device

        with tempfile.TemporaryDirectory() as folder, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as peer:
            peer.bind(('127.0.0.1', 0))
            peer.settimeout(.03)
            config = dict(endpoint=dict(kind='serial', device=os.ttyname(slave), baud=115200,
                bytesize=8, parity='N', stopbits=1), channel='control', uav_id=1,
                radio=dict(bind=['127.0.0.1', 0], peer=peer.getsockname()),
                queues=dict(packets=4, bytes=4096, deadline_s=1), watchdog_s=1, reconnect_s=.01)
            handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
            try:
                with patch('serial.Serial', side_effect=wrong_baud):
                    result = run(config, Path(folder), duration=.05)
            finally:
                for sig, handler in handlers.items():
                    signal.signal(sig, handler)
                for device in opened:
                    self.assertFalse(device.is_open)
                    device.close()
            self.assertTrue(opened)
            self.assertEqual(result['reconnects'], 0)
            self.assertEqual(result['endpoint_to_radio_bytes'], 0)
            self.assertEqual(result['radio_to_endpoint_bytes'], 0)
            self.assertEqual(result['serial_baud_requested'], 115200)
            self.assertEqual(result['serial_baud_observed'], 57600)
            events = [json.loads(row) for row in (Path(folder) / 'events.jsonl').read_text().splitlines()]
            self.assertFalse(any(row['event'] == 'connected' for row in events))
            self.assertTrue(any('requested 115200, observed 57600' in row.get('reason', '') for row in events))
            with self.assertRaises(socket.timeout):
                peer.recvfrom(65535)
