import copy
import hashlib
import json
import os
import signal
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from scripts.product.test_radio_hitl import CATALOG, ROOT, load_bundle, main, resolve, run_adapters
from network.scripts.serial_transport import HEADER, Encoder, Reassembler
from tests.test_external_endpoint import free_udp_address, udp_socket


class RadioHitlPreparationTests(unittest.TestCase):
    def setUp(self):
        self.catalog = json.loads(CATALOG.read_text())
        self.request = dict(controller='pixhawk_6c', radio_device='microhard_p2400', uav_id=1, channels={})
        for offset, channel in enumerate(('control', 'payload')):
            self.request['channels'][channel] = dict(
                endpoint=dict(kind='serial', device=f'/dev/ttyUSB{offset}', baud=115200),
                radio=dict(bind=['10.71.1.10', 16001 + offset], peer=['10.71.0.10', 17001 + offset]),
                radio_namespace='ams-uav1')

    def test_two_uart_configs_use_existing_adapter_contract(self):
        original = copy.deepcopy(self.request)
        result = resolve(self.request, self.catalog)
        self.assertEqual(self.request, original)
        self.assertEqual(result['validation_scope'], 'configuration_only')
        self.assertEqual(result['hardware_validation'], 'not_performed')
        self.assertEqual(result['radio_model_equivalence'], 'not_established')
        for channel in ('control', 'payload'):
            config = result['adapters'][channel]
            self.assertEqual(config['channel'], channel)
            self.assertEqual(config['endpoint']['bytesize'], 8)
            self.assertEqual(result['channels'][channel]['serial_interface_ceiling_bps_before_protocol'], 92160)

    def test_duplicate_serial_or_radio_bind_is_rejected(self):
        for field in ('serial', 'radio'):
            with self.subTest(field=field):
                request = copy.deepcopy(self.request)
                if field == 'serial':
                    request['channels']['payload']['endpoint']['device'] = '/dev/ttyUSB0'
                else:
                    request['channels']['payload']['radio']['bind'] = ['0.0.0.0', 16001]
                with self.assertRaises(ValueError):
                    resolve(request, self.catalog)

    def test_required_serial_rate_cannot_exceed_baud_ceiling(self):
        self.request['channels']['payload']['required_goodput_bps'] = 20000000
        with self.assertRaisesRegex(ValueError, '92160'):
            resolve(self.request, self.catalog)

    def test_queue_must_fit_at_least_one_bsf1_payload_byte(self):
        self.request['queues'] = dict(packets=1, bytes=HEADER.size, deadline_s=.25)
        with self.assertRaisesRegex(ValueError, 'BSF1 header'):
            resolve(self.request, self.catalog)
        self.request['queues']['bytes'] += 1
        self.assertEqual(resolve(self.request, self.catalog)['adapters']['control']['queues']['bytes'], HEADER.size + 1)

    def test_typo_bad_ids_and_nonfinite_values_are_rejected(self):
        for key, value in [('uav_id', True), ('uav_id', 6), ('controller', []),
                           ('watchdog_s', float('nan')), ('watchdogs_s', 1)]:
            with self.subTest(key=key, value=value):
                request = copy.deepcopy(self.request)
                request[key] = value
                with self.assertRaises(ValueError):
                    resolve(request, self.catalog)

    def test_udp_tcp_endpoints_require_ethernet_reference(self):
        self.request['channels']['control']['endpoint'] = dict(kind='udp', bind=['127.0.0.1', 14550], peer=['127.0.0.1', 14551])
        self.request['channels']['payload']['endpoint'] = dict(kind='tcp_client', peer=['127.0.0.1', 14552])
        with self.assertRaisesRegex(ValueError, 'endpoint kind'):
            resolve(self.request, self.catalog)
        self.request['radio_device'] = 'microhard_pmddl2450'
        self.request['channels']['payload']['required_goodput_bps'] = 20000000
        result = resolve(self.request, self.catalog)
        self.assertEqual(result['adapters']['payload']['endpoint']['kind'], 'tcp_client')
        self.assertEqual(result['channels']['payload']['physical_validation'], 'not_performed')
        self.assertIn('two physical UARTs simultaneously', result['open_criteria'])

    def test_flow_control_flags_are_typed_and_conflicts_rejected(self):
        self.request['channels']['control']['endpoint']['rtscts'] = 'false'
        with self.assertRaisesRegex(ValueError, 'boolean'):
            resolve(self.request, self.catalog)
        self.request['channels']['control']['endpoint'].update(rtscts=True, xonxoff=True)
        with self.assertRaisesRegex(ValueError, 'not both'):
            resolve(self.request, self.catalog)

    def test_cli_dry_run_does_not_write_and_prepare_is_immutable(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            config = folder / 'config.json'
            config.write_text(json.dumps(self.request))
            output = folder / 'prepared'
            argv = [sys.executable, str(ROOT / 'scripts/product/test_radio_hitl.py'),
                    '--config', str(config), '--output', str(output)]
            result = subprocess.run([*argv, '--dry-run'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(output.exists())
            preview = json.loads(result.stdout)
            result = subprocess.run([*argv, '--prepare'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            manifest = json.loads((output / 'hardware-check.json').read_text())
            self.assertEqual(manifest['adapters'], preview['adapters'])
            self.assertEqual(json.loads((output / 'control.json').read_text()), manifest['adapters']['control'])
            self.assertIn('external_endpoint.py', manifest['adapter_argv']['payload'][1])
            result = subprocess.run([*argv, '--prepare'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)

    def test_native_pair_uses_exact_existing_slots_for_every_supported_uav(self):
        for uav in range(1, 6):
            request = copy.deepcopy(self.request)
            request['uav_id'] = uav
            for channel in request['channels'].values():
                channel.pop('radio')
                channel.pop('radio_namespace')
            result = resolve(request, self.catalog, native_uav_count=5)
            for channel, base in (('control', 14600), ('payload', 14700)):
                config = result['adapters'][channel]
                self.assertEqual(config['radio_namespace'], f'ams-uav{uav}')
                self.assertEqual(config['radio']['bind'], [f'10.71.{uav}.10', base + uav])
                self.assertEqual(config['radio']['peer'], ['10.71.0.10', base])
            self.assertEqual(result['native_binding']['scenario_mode'], 'latency_diagnostic')
        with self.assertRaises(ValueError):
            resolve(request, self.catalog, native_uav_count=4)
        with self.assertRaisesRegex(ValueError, 'binding'):
            resolve(self.request, self.catalog, native_uav_count=5)

    def test_bundle_tampering_or_wrong_roster_is_rejected(self):
        request = copy.deepcopy(self.request)
        for channel in request['channels'].values():
            channel.pop('radio')
            channel.pop('radio_namespace')
        bundle = resolve(request, self.catalog, native_uav_count=5)
        bundle['reference_profiles_sha256'] = hashlib.sha256(CATALOG.read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'bundle.json'
            path.write_text(json.dumps(bundle))
            resolved = load_bundle(path, native_uav_count=5, watchdog_file='/tmp/owned-radio.heartbeat')
            self.assertTrue(all(row['radio_watchdog_file'] == '/tmp/owned-radio.heartbeat' for row in resolved['adapters'].values()))
            with self.assertRaisesRegex(ValueError, 'roster'):
                load_bundle(path, native_uav_count=4)
            for mutate in (lambda b: b['adapters']['control']['radio'].update(peer=['127.0.0.1', 14600]),
                           lambda b: b['native_binding'].update(coordination_policy='fixed'),
                           lambda b: b.update(unrecognized=True)):
                changed = copy.deepcopy(bundle)
                mutate(changed)
                path.write_text(json.dumps(changed))
                with self.assertRaises(ValueError):
                    load_bundle(path, native_uav_count=5)

    def test_profile_change_is_rejected_before_any_adapter_opens(self):
        bundle = resolve(self.request, self.catalog)
        bundle['reference_profiles_sha256'] = hashlib.sha256(CATALOG.read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            profiles, output = Path(temporary) / 'profiles.json', Path(temporary) / 'output'
            profiles.write_bytes(CATALOG.read_bytes() + b'\n')
            with self.assertRaisesRegex(ValueError, 'changed before adapter launch'):
                run_adapters(bundle, output, .01, profiles_path=profiles)
            self.assertFalse(output.exists())


class RadioHitlSoftwarePairTests(unittest.TestCase):
    """Actual two-adapter boundary; loopback contains no simulated PHY/MAC."""
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.catalog = json.loads(CATALOG.read_text())

    def start(self, channels, *, duration=1.2, readiness=2, start_new_session=False):
        request = dict(controller='pixhawk_6c', radio_device='microhard_pmddl2450', uav_id=2,
                       channels=channels, queues=dict(packets=256, bytes=1048576, deadline_s=2), reconnect_s=.05)
        profiles = self.folder / 'profiles.json'
        profiles.write_text(json.dumps(self.catalog, indent=2))
        bundle = resolve(request, self.catalog)
        bundle['reference_profiles_sha256'] = hashlib.sha256(profiles.read_bytes()).hexdigest()
        path = self.folder / 'bundle.json'
        path.write_text(json.dumps(bundle))
        output = self.folder / 'run'
        process = subprocess.Popen([sys.executable, str(ROOT / 'scripts/product/test_radio_hitl.py'),
            '--config', str(path), '--profiles', str(profiles), '--run-adapters', '--output', str(output), '--duration-s', str(duration),
            '--readiness-timeout-s', str(readiness)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=start_new_session)
        self.addCleanup(self.stop, process)
        return process, output

    @staticmethod
    def stop(process):
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=5)

    def ready(self, process, output):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if (output / 'ready.json').exists():
                return
            if process.poll() is not None:
                self.fail(process.communicate())
            time.sleep(.01)
        self.fail('two endpoints did not become ready')

    def exchange_radio(self, channel, radio, address, expected):
        receiver = Reassembler(channel=channel, uav_id=2, direction='uart_to_gcs')
        received = bytearray()
        while len(received) < len(expected):
            received.extend(b''.join(receiver.ingest(radio.recvfrom(65535)[0])))
        self.assertEqual(received, expected)
        reply = b'reply-' + channel.encode() + b'\x00'
        for frame in Encoder(channel=channel, uav_id=2, direction='gcs_to_uart').encode(reply):
            radio.sendto(frame, address)
        return reply

    @unittest.skipUnless(os.name == 'posix', 'actual process-group SIGTERM contract')
    def test_native_group_stop_and_supervisor_stop_close_children_cleanly(self):
        with udp_socket() as peer:
            channels = {channel: dict(endpoint=dict(kind='udp', bind=free_udp_address(), peer=peer.getsockname()),
                radio=dict(bind=free_udp_address(), peer=peer.getsockname())) for channel in ('control', 'payload')}
            process, output = self.start(channels, duration=30, start_new_session=True)
            self.ready(process, output)
            os.killpg(process.pid, signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 130, stdout + stderr)
            summary = json.loads((output / 'result.json').read_text())
            self.assertEqual(summary['status'], 'stopped')
            self.assertEqual(summary['child_returncodes'], dict(control=0, payload=0))
            for channel in ('control', 'payload'):
                metrics = json.loads((output / channel / 'metrics.json').read_text())
                self.assertFalse(metrics['connected'])
                events = [json.loads(line) for line in (output / channel / 'events.jsonl').read_text().splitlines()]
                self.assertEqual(events[-1]['event'], 'disconnected')
                self.assertEqual(events[-1]['reason'], 'shutdown')

    def test_two_real_udp_tcp_adapters_exchange_both_streams(self):
        self.catalog['radios']['microhard_pmddl2450']['notes'] = 'Frozen software interface test profile; no hardware claim.'
        with udp_socket() as control_radio, udp_socket() as payload_radio, udp_socket() as device, socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            listener.settimeout(3)
            endpoint = free_udp_address()
            control_address, payload_address = free_udp_address(), free_udp_address()
            channels = dict(control=dict(endpoint=dict(kind='udp', bind=endpoint, peer=device.getsockname()),
                radio=dict(bind=control_address, peer=control_radio.getsockname())),
                payload=dict(endpoint=dict(kind='tcp_client', peer=listener.getsockname()),
                radio=dict(bind=payload_address, peer=payload_radio.getsockname())))
            process, output = self.start(channels)
            with listener.accept()[0] as payload:
                payload.settimeout(2)
                self.ready(process, output)
                control_bytes, payload_bytes = b'control\x00bytes', bytes(range(256)) * 2
                device.sendto(control_bytes, endpoint)
                payload.sendall(payload_bytes)
                expected_control = self.exchange_radio('control', control_radio, control_address, control_bytes)
                expected_payload = self.exchange_radio('payload', payload_radio, payload_address, payload_bytes)
                self.assertEqual(device.recvfrom(65535)[0], expected_control)
                received = bytearray()
                while len(received) < len(expected_payload):
                    received.extend(payload.recv(4096))
                self.assertEqual(received, expected_payload)
                stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, stderr + stdout)
            result = json.loads((output / 'result.json').read_text())
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(result['hardware_validation'], 'not_performed')
            self.assertEqual(result['native_phy_validation'], 'not_performed')
            self.assertEqual((output / 'reference-profiles.json').read_bytes(), (self.folder / 'profiles.json').read_bytes())
            for channel, tos in (('control', 184), ('payload', 40)):
                metrics = json.loads((output / channel / 'metrics.json').read_text())
                self.assertEqual(metrics['radio_tos'], tos)
                self.assertEqual(metrics['uav_id'], 2)
                self.assertEqual(metrics['channel'], channel)
                self.assertIs(type(metrics['reassembly_pending_records']), int)
                self.assertGreater(metrics['radio_to_endpoint_bytes'], 0)

    def test_connection_readiness_failure_stops_both_children_and_frees_ports(self):
        with udp_socket() as radio, udp_socket() as device, socket.socket() as unused_tcp:
            unused_tcp.bind(('127.0.0.1', 0))  # Deliberately not listening.
            addresses = [free_udp_address(), free_udp_address()]
            channels = dict(control=dict(endpoint=dict(kind='udp', bind=free_udp_address(), peer=device.getsockname()),
                radio=dict(bind=addresses[0], peer=radio.getsockname())),
                payload=dict(endpoint=dict(kind='tcp_client', peer=unused_tcp.getsockname()),
                radio=dict(bind=addresses[1], peer=radio.getsockname())))
            process, output = self.start(channels, duration=5, readiness=.3)
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 1, stderr + stdout)
            result = json.loads((output / 'result.json').read_text())
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(set(result['child_returncodes']), {'control', 'payload'})
            self.assertFalse((output / 'ready.json').exists())
            for address in addresses:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                    probe.bind(address)

    def test_existing_radio_owner_rejects_pair_before_any_adapter_starts(self):
        with udp_socket() as occupied, udp_socket() as device:
            control_address = free_udp_address()
            channels = {channel: dict(endpoint=dict(kind='udp', bind=free_udp_address(), peer=device.getsockname()),
                radio=dict(bind=control_address if channel == 'control' else occupied.getsockname(),
                           peer=device.getsockname())) for channel in ('control', 'payload')}
            process, output = self.start(channels)
            stdout, stderr = process.communicate(timeout=4)
            self.assertEqual(process.returncode, 1, stderr + stdout)
            result = json.loads((output / 'result.json').read_text())
            self.assertEqual(result['child_returncodes'], {})
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.bind(control_address)

    @unittest.skipUnless(hasattr(os, 'openpty'), 'two-PTY serial software test requires POSIX; hardware remains untested')
    def test_serial_symlink_alias_is_rejected_before_adapter_or_socket_launch(self):
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        path = os.ttyname(slave)
        alias = self.folder / 'same-tty'
        alias.symlink_to(path)
        channels = {channel: dict(endpoint=dict(kind='serial', device=device, baud=115200),
            radio=dict(bind=free_udp_address(), peer=free_udp_address()))
            for channel, device in (('control', path), ('payload', str(alias)))}
        process, output = self.start(channels)
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 2, stdout + stderr)
        self.assertIn('same POSIX serial device', stderr)
        self.assertFalse(output.exists())

    @unittest.skipUnless(hasattr(os, 'openpty'), 'two-PTY serial software test requires POSIX; hardware remains untested')
    def test_two_real_pty_serial_adapters_preserve_control_payload_bytes(self):
        import select
        import serial
        import termios
        import tty
        descriptors = [os.openpty(), os.openpty()]
        self.addCleanup(lambda: [os.close(fd) for pair in descriptors for fd in pair])
        for master, slave in descriptors:
            tty.setraw(slave)
        with udp_socket() as control_radio, udp_socket() as payload_radio:
            addresses = [free_udp_address(), free_udp_address()]
            rates = dict(control=57600, payload=115200)
            channels = {channel: dict(endpoint=dict(kind='serial', device=os.ttyname(descriptors[index][1]), baud=rates[channel]),
                radio=dict(bind=addresses[index], peer=radio.getsockname()))
                for index, (channel, radio) in enumerate((('control', control_radio), ('payload', payload_radio)))}
            process, output = self.start(channels)
            self.ready(process, output)
            for channel in ('control', 'payload'):
                with self.assertRaises(serial.SerialException):
                    serial.Serial(channels[channel]['endpoint']['device'], baudrate=115200, exclusive=True)
            for index, (channel, radio) in enumerate((('control', control_radio), ('payload', payload_radio))):
                master, slave = descriptors[index]
                self.assertEqual(termios.tcgetattr(slave)[4:6], [getattr(termios, f'B{rates[channel]}')] * 2)
                original = channel.encode() + bytes(range(256))
                os.write(master, original)
                expected = self.exchange_radio(channel, radio, addresses[index], original)
                self.assertTrue(select.select([master], [], [], 2)[0])
                self.assertEqual(os.read(master, 4096), expected)
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, stderr + stdout)
            identities = json.loads((output / 'result.json').read_text())['serial_device_identities']
            self.assertNotEqual(identities['control']['st_rdev'], identities['payload']['st_rdev'])
            for channel in ('control', 'payload'):
                metrics = json.loads((output / channel / 'metrics.json').read_text())
                self.assertEqual(metrics['serial_baud_requested'], rates[channel])
                self.assertEqual(metrics['serial_baud_reported_by_pyserial'], rates[channel])
                self.assertEqual(metrics['serial_baud_observed'], rates[channel])
                self.assertEqual(metrics['serial_baud_observed_reason'], 'standard_termios_input_output_match')
                self.assertTrue(metrics['serial_exclusive'])
                self.assertEqual(metrics['serial_device_identity'], identities[channel]['st_rdev'])
                with serial.Serial(channels[channel]['endpoint']['device'], baudrate=115200, exclusive=True):
                    pass  # Actual cooperative lock is released when this owned adapter closes.


if __name__ == '__main__':
    unittest.main()
