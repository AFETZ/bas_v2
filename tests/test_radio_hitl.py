import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.product.test_radio_hitl import CATALOG, ROOT, main, resolve


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


if __name__ == '__main__':
    unittest.main()
