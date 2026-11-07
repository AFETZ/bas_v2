#!/usr/bin/env python3
"""Prepare or explicitly run two existing BSF1 Serial/Ethernet adapters.

Input JSON selects controller/radio_device, uav_id and channels.control/payload.
Each channel supplies endpoint, radio.bind/peer, optional radio_namespace and
required_goodput_bps. --dry-run prints the resolved bundle; --prepare writes it.
Actual Serial/Ethernet, controller behaviour and the modeled-radio path remain
separate hardware/integration criteria. No opaque-byte echo is assumed of a FC.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import ipaddress
import json
import math
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import bas

CATALOG = ROOT / 'network/config/hitl_reference_devices.json'
CHANNELS = ('control', 'payload')
NATIVE_TOS = dict(control=184, payload=40)


def _fields(value, required, optional, label):
    if not isinstance(value, dict) or set(value) - required - optional or required - set(value):
        raise ValueError(f'{label}: required fields {sorted(required)}, optional {sorted(optional)}')
    return value


def _number(value, label, *, integer=False, minimum=0):
    try:
        valid = type(value) in ((int,) if integer else (int, float)) and math.isfinite(value) and value >= minimum
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f'{label} must be finite and >= {minimum}')
    return value


def _address(value, label):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f'{label} must be [IPv4 address, port]')
    if not isinstance(value[0], str):
        raise ValueError(f'{label}.host must be an IPv4 address string')
    host = str(ipaddress.IPv4Address(value[0]))
    port = _number(value[1], label + '.port', integer=True, minimum=1)
    if port > 65535:
        raise ValueError(f'{label}.port exceeds 65535')
    return [host, port]


def resolve(request, catalog, *, native_uav_count=None):
    _fields(catalog, {'schema_version', 'controllers', 'radios'},
            {'selection_basis', 'hardware_validation'}, 'reference profiles')
    if type(catalog['schema_version']) is not int or catalog['schema_version'] != 1 or not isinstance(catalog['controllers'], dict) or not isinstance(catalog['radios'], dict):
        raise ValueError('reference profiles require schema_version 1 and controller/radio mappings')
    _fields(request, {'controller', 'radio_device', 'uav_id', 'channels'},
            {'queues', 'watchdog_s', 'reconnect_s', 'radio_watchdog_file'}, 'root')
    if not isinstance(request['controller'], str) or request['controller'] not in catalog['controllers']:
        raise ValueError('unknown controller reference')
    if not isinstance(request['radio_device'], str) or request['radio_device'] not in catalog['radios']:
        raise ValueError('unknown radio reference')
    uav = _number(request['uav_id'], 'uav_id', integer=True, minimum=1)
    if uav > 5:
        raise ValueError('uav_id must be in 1..5 for this acceptance setup')
    if native_uav_count is not None:
        _number(native_uav_count, 'native_uav_count', integer=True, minimum=1)
        if native_uav_count > 5 or uav > native_uav_count:
            raise ValueError('native UAV roster must be 1..5 and contain the selected UAV')
        if 'radio_watchdog_file' in request:
            raise ValueError('native watchdog path is supplied by the owning runtime, not the source request')
    _fields(request['channels'], set(CHANNELS), set(), 'channels')
    queues = copy.deepcopy(request.get('queues', dict(packets=256, bytes=1048576, deadline_s=.25)))
    _fields(queues, {'packets', 'bytes', 'deadline_s'}, set(), 'queues')
    for field in ('packets', 'bytes'):
        _number(queues[field], 'queues.' + field, integer=True, minimum=1)
    _number(queues['deadline_s'], 'queues.deadline_s', minimum=.000001)
    watchdog = _number(request.get('watchdog_s', .3 if native_uav_count else 2), 'watchdog_s', minimum=.000001)
    if native_uav_count and watchdog > .3:
        raise ValueError('native endpoint watchdog_s must be <= the existing 0.3 s boundary guard')
    reconnect = _number(request.get('reconnect_s', 1), 'reconnect_s', minimum=.000001)
    device_profile = catalog['radios'][request['radio_device']]
    adapters, diagnostics, bindings, serial_devices = {}, {}, [], set()

    def add_binding(namespace, address, label):
        for old_ns, old_address, old_label in bindings:
            if (namespace == old_ns and address[1] == old_address[1]
                    and (address[0] == old_address[0] or '0.0.0.0' in (address[0], old_address[0]))):
                raise ValueError(f'{label} conflicts with {old_label}')
        bindings.append((namespace, address, label))

    for channel in CHANNELS:
        item = copy.deepcopy(request['channels'][channel])
        required = {'endpoint'} if native_uav_count else {'endpoint', 'radio'}
        _fields(item, required, {'radio', 'radio_namespace', 'required_goodput_bps'}, channel)
        if native_uav_count:
            port = 14600 if channel == 'control' else 14700
            expected_radio = dict(bind=[f'10.71.{uav}.10', port + uav], peer=['10.71.0.10', port])
            supplied = item.get('radio', expected_radio)
            if not isinstance(supplied, dict) or {k: v for k, v in supplied.items() if k != 'tos'} != expected_radio or supplied.get('tos', NATIVE_TOS[channel]) != NATIVE_TOS[channel]:
                raise ValueError(f'{channel}: native radio binding must match the selected UAV/TAP exactly')
            if item.get('radio_namespace', f'ams-uav{uav}') != f'ams-uav{uav}':
                raise ValueError(f'{channel}: native radio namespace must match the selected UAV')
            item['radio'], item['radio_namespace'] = expected_radio, f'ams-uav{uav}'
        endpoint = copy.deepcopy(item['endpoint'])
        if not isinstance(endpoint, dict) or endpoint.get('kind') not in device_profile['adapter_kinds']:
            raise ValueError(f'{channel}: endpoint kind unsupported by selected radio reference')
        kind = endpoint['kind']
        required_rate = _number(item.get('required_goodput_bps', 0), channel + '.required_goodput_bps')
        detail = dict(required_goodput_bps=required_rate, physical_validation='not_performed')
        if kind == 'serial':
            _fields(endpoint, {'kind', 'device', 'baud'},
                    {'bytesize', 'parity', 'stopbits', 'xonxoff', 'rtscts', 'dsrdtr'}, channel + '.endpoint')
            if not isinstance(endpoint['device'], str) or not endpoint['device'].strip():
                raise ValueError(f'{channel}: serial device is required')
            identity = endpoint['device'].casefold() if endpoint['device'].upper().startswith('COM') else endpoint['device']
            if identity in serial_devices:
                raise ValueError('control and payload must use different serial devices')
            serial_devices.add(identity)
            if type(endpoint['baud']) is not int or endpoint['baud'] not in device_profile['bench_baud_rates']:
                raise ValueError(f'{channel}: supported reference bench baud rates are {device_profile["bench_baud_rates"]}')
            endpoint.setdefault('bytesize', 8)
            endpoint.setdefault('parity', 'N')
            endpoint.setdefault('stopbits', 1)
            if (type(endpoint['bytesize']) is not int or endpoint['bytesize'] != 8
                    or endpoint['parity'] not in ('N', 'E', 'O')
                    or type(endpoint['stopbits']) is not int or endpoint['stopbits'] not in (1, 2)):
                raise ValueError(f'{channel}: bench supports 8 data bits, N/E/O parity, 1/2 stop bits')
            for flag in ('xonxoff', 'rtscts', 'dsrdtr'):
                if flag in endpoint and type(endpoint[flag]) is not bool:
                    raise ValueError(f'{channel}.{flag} must be boolean')
            if endpoint.get('xonxoff') and endpoint.get('rtscts'):
                raise ValueError(f'{channel}: choose software or RTS/CTS flow control, not both')
            frame_bits = 1 + endpoint['bytesize'] + int(endpoint['parity'] != 'N') + endpoint['stopbits']
            ceiling = endpoint['baud'] * 8 / frame_bits
            detail['serial_interface_ceiling_bps_before_protocol'] = ceiling
            if required_rate > ceiling:
                raise ValueError(f'{channel}: required {required_rate:g} bit/s exceeds UART ceiling {ceiling:g} bit/s before protocol overhead')
        elif kind == 'udp':
            _fields(endpoint, {'kind', 'bind', 'peer'}, set(), channel + '.endpoint')
            endpoint['bind'] = _address(endpoint['bind'], channel + '.endpoint.bind')
            endpoint['peer'] = _address(endpoint['peer'], channel + '.endpoint.peer')
            add_binding(None, endpoint['bind'], channel + '.endpoint.bind')
        else:
            _fields(endpoint, {'kind', 'peer'}, set(), channel + '.endpoint')
            endpoint['peer'] = _address(endpoint['peer'], channel + '.endpoint.peer')
        radio = copy.deepcopy(item['radio'])
        _fields(radio, {'bind', 'peer'}, {'tos'}, channel + '.radio')
        radio['tos'] = _number(radio.get('tos', NATIVE_TOS[channel]), channel + '.radio.tos', integer=True)
        if radio['tos'] > 255:
            raise ValueError('radio.tos must be in 0..255')
        for key in ('bind', 'peer'):
            radio[key] = _address(radio[key], channel + '.radio.' + key)
        namespace = item.get('radio_namespace')
        if namespace is not None and (not isinstance(namespace, str) or not namespace
                                      or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in namespace)):
            raise ValueError(f'{channel}: invalid namespace name')
        add_binding(namespace, radio['bind'], channel + '.radio.bind')
        adapter = dict(endpoint=endpoint, radio=radio, channel=channel, uav_id=uav,
                       queues=copy.deepcopy(queues), watchdog_s=watchdog, reconnect_s=reconnect)
        if namespace is not None:
            adapter['radio_namespace'] = namespace
        if 'radio_watchdog_file' in request:
            path = request['radio_watchdog_file']
            if not isinstance(path, str) or not path:
                raise ValueError('radio_watchdog_file must be a nonempty path')
            adapter['radio_watchdog_file'] = path
        adapters[channel], diagnostics[channel] = adapter, detail
    result = dict(schema_version=1, validation_scope='configuration_only', hardware_validation='not_performed',
                source_request=copy.deepcopy(request),
                radio_model_equivalence='not_established',
                selected_controller=copy.deepcopy(catalog['controllers'][request['controller']]),
                selected_radio=copy.deepcopy(device_profile), adapters=adapters, channels=diagnostics,
                open_criteria=['two physical UARTs simultaneously', 'physical Ethernet UDP/TCP',
                               'controller MAVLink behavior and deadlines', 'shared modeled-radio path and no bypass',
                               'modem PHY/MAC equivalence', 'bounded queues under payload saturation'])
    if native_uav_count:
        result['native_binding'] = dict(schema_version=1, uav_id=uav, uav_count=native_uav_count,
            simulation_mode='realtime', coordination_policy='off', scenario_mode='latency_diagnostic',
            channels=list(CHANNELS), endpoint_owner='external', requires_runtime_watchdog=True,
            scope='radio-HitL boundary only; virtual trajectory is not physical FC sensor/flight coupling')
    return result


def load_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'), object_pairs_hook=bas.unique_json_fields,
                      parse_constant=bas.reject_json_constant)


def load_bundle(path, catalog_path=CATALOG, *, native_uav_count=None, watchdog_file=None):
    """Revalidate prepared inputs; never trust saved adapter addresses or argv."""
    bundle = load_json(path)
    if not isinstance(bundle, dict):
        raise ValueError('expected a prepared hardware-check.json bundle')
    binding = bundle.get('native_binding')
    count = binding.get('uav_count') if isinstance(binding, dict) else None
    if native_uav_count is not None and (type(count) is not int or count != native_uav_count):
        raise ValueError('prepared native bundle roster does not match this runtime')
    if binding is not None and count is None:
        raise ValueError('invalid native binding')
    expected = resolve(bundle.get('source_request'), load_json(catalog_path), native_uav_count=count)
    allowed = set(expected) | {'reference_profiles_sha256', 'adapter_argv'}
    if set(bundle) - allowed or any(bundle.get(key) != value for key, value in expected.items()):
        raise ValueError('prepared bundle does not match its validated source request and native contract')
    if bundle.get('reference_profiles_sha256') != hashlib.sha256(Path(catalog_path).read_bytes()).hexdigest():
        raise ValueError('reference profile hash differs from the prepared bundle')
    result = copy.deepcopy(bundle)
    result.pop('adapter_argv', None)  # Always construct executable argv from this checkout.
    if watchdog_file is not None:
        if not isinstance(watchdog_file, str) or not watchdog_file:
            raise ValueError('runtime watchdog path is required')
        for adapter in result['adapters'].values():
            adapter['radio_watchdog_file'] = watchdog_file
    return result


def run_adapters(bundle, output, duration_s, readiness_timeout_s=10):
    """Supervise only this pair; native PHY readiness remains the runner's job."""
    _number(duration_s, 'duration_s', minimum=.001)
    _number(readiness_timeout_s, 'readiness_timeout_s', minimum=.001)
    if duration_s > 86400 or readiness_timeout_s > 300:
        raise ValueError('adapter duration is limited to 86400 s; readiness timeout to 300 s')
    native = bundle.get('native_binding')
    if native and sys.platform != 'linux':
        raise ValueError('native namespace attachment requires Linux')
    if native and any(not row.get('radio_watchdog_file') for row in bundle['adapters'].values()):
        raise ValueError('native adapter launch requires the owning runtime watchdog path')
    from network.scripts.external_endpoint import open_radio_socket
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    bas.write_json(output / 'bundle.json', bundle)
    children, logs, previous = {}, [], {}
    stopped = False
    started = time.monotonic()
    summary = dict(status='starting', hardware_validation='not_performed',
                   native_phy_validation='not_performed', endpoint_owner='external', child_returncodes={})
    def stop(*_):
        nonlocal stopped
        stopped = True
    try:
        # No SO_REUSEADDR: a live SITL adapter or a second external owner rejects
        # attachment. Opening both simultaneously also checks the configured IPs.
        sockets = []
        try:
            for config in bundle['adapters'].values():
                sockets.append(open_radio_socket(config))
        finally:
            for sock in sockets:
                sock.close()
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, stop)
        for channel, config in bundle['adapters'].items():
            path = output / (channel + '.json')
            bas.write_json(path, config)
            log = (output / (channel + '.log')).open('x')
            logs.append(log)
            children[channel] = subprocess.Popen([sys.executable, str(ROOT / 'network/scripts/external_endpoint.py'),
                '--config', str(path), '--output', str(output / channel), '--duration-s', str(duration_s)],
                cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        ready = False
        while not stopped:
            elapsed = time.monotonic() - started
            codes = {channel: process.poll() for channel, process in children.items()}
            if any(code is not None and (code != 0 or elapsed < duration_s) for code in codes.values()):
                raise RuntimeError(f'external adapter exited before completion: {codes}')
            connected = []
            for channel in CHANNELS:
                events = output / channel / 'events.jsonl'
                latest = None
                if events.exists():
                    with events.open('rb') as stream:
                        stream.seek(0, 2)
                        stream.seek(max(0, stream.tell() - 65536))
                        # Discard a concurrent writer's unfinished trailing line.
                        lines = stream.read().rsplit(b'\n', 2)
                    if len(lines) >= 2:
                        latest = json.loads(lines[-2])
                connected.append(bool(latest and latest['event'] == 'connected'))
            if all(connected) and not ready:
                ready = True
                bas.write_json(output / 'ready.json', dict(adapter_endpoints_connected=True,
                    hardware_validation='not_performed', native_radio_readiness='not_evaluated',
                    host_monotonic_ns=time.monotonic_ns(), pids={key: child.pid for key, child in children.items()}))
            if all(code is not None for code in codes.values()):
                if not ready:
                    raise RuntimeError('both external endpoints never became connected')
                summary['status'] = 'completed'
                break
            if not ready and elapsed >= readiness_timeout_s:
                raise RuntimeError('external endpoint connection readiness timed out')
            if elapsed > duration_s + readiness_timeout_s:
                raise RuntimeError('external adapters did not stop at their bounded duration')
            time.sleep(.02)
        if stopped:
            summary['status'] = 'stopped'
    except Exception as error:
        summary.update(status='failed', error=f'{type(error).__name__}: {error}')
    finally:
        for child in children.values():
            if child.poll() is None:
                child.terminate()
        for channel, child in children.items():
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=3)
            summary['child_returncodes'][channel] = child.returncode
        for log in logs:
            log.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        summary['wall_s'] = time.monotonic() - started
        bas.write_json(output / 'result.json', summary)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--profiles', type=Path, default=CATALOG)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--native-uav-count', type=int, help='bind both channels to the exact existing native UAV/TAP roster')
    parser.add_argument('--radio-watchdog-file', help='runtime-supplied native heartbeat path when launching')
    parser.add_argument('--duration-s', type=float)
    parser.add_argument('--readiness-timeout-s', type=float)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--check', action='store_true')
    action.add_argument('--dry-run', action='store_true')
    action.add_argument('--prepare', action='store_true')
    action.add_argument('--check-bundle', action='store_true')
    action.add_argument('--run-adapters', action='store_true', help='explicitly open both configured Serial/Ethernet endpoints')
    args = parser.parse_args(argv)
    if not args.run_adapters and any(value is not None for value in (args.duration_s, args.readiness_timeout_s, args.radio_watchdog_file)):
        parser.error('runtime duration/readiness/watchdog options require --run-adapters')
    if args.check_bundle or args.run_adapters:
        result = load_bundle(args.config, args.profiles, native_uav_count=args.native_uav_count,
                             watchdog_file=args.radio_watchdog_file)
        if args.run_adapters:
            if args.output is None:
                parser.error('--run-adapters requires a new --output directory')
            result = run_adapters(result, args.output, 60 if args.duration_s is None else args.duration_s,
                                  10 if args.readiness_timeout_s is None else args.readiness_timeout_s)
            print(json.dumps(result, indent=2))
            return 130 if result['status'] == 'stopped' else 0 if result['status'] == 'completed' else 1
        if args.output is not None:
            parser.error('--check-bundle does not write output')
        print(json.dumps(result, indent=2))
        return 0
    profile_bytes = args.profiles.read_bytes()
    result = resolve(load_json(args.config), load_json(args.profiles), native_uav_count=args.native_uav_count)
    result['reference_profiles_sha256'] = hashlib.sha256(profile_bytes).hexdigest()
    if args.prepare and args.output is None:
        parser.error('--prepare requires a new --output directory')
    if args.output is not None:
        result['adapter_argv'] = {channel: [sys.executable, str(ROOT / 'network/scripts/external_endpoint.py'),
                                          '--config', str(args.output / (channel + '.json')),
                                          '--output', str(args.output / channel)] for channel in CHANNELS}
    if args.prepare:
        args.output.mkdir(parents=True, exist_ok=False)
        for channel in CHANNELS:
            (args.output / (channel + '.json')).write_text(json.dumps(result['adapters'][channel], indent=2) + '\n')
        (args.output / 'hardware-check.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as error:
        print(f'HitL configuration: {error}', file=sys.stderr)
        raise SystemExit(2)
