#!/usr/bin/env python3
"""Validate and prepare two existing BSF1 adapters; never send bytes to hardware.

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
import sys

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / 'network/config/hitl_reference_devices.json'
CHANNELS = ('control', 'payload')


def _fields(value, required, optional, label):
    if not isinstance(value, dict) or set(value) - required - optional or required - set(value):
        raise ValueError(f'{label}: required fields {sorted(required)}, optional {sorted(optional)}')
    return value


def _number(value, label, *, integer=False, minimum=0):
    if (type(value) not in ((int,) if integer else (int, float))
            or not math.isfinite(value) or value < minimum):
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


def resolve(request, catalog):
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
    _fields(request['channels'], set(CHANNELS), set(), 'channels')
    queues = copy.deepcopy(request.get('queues', dict(packets=256, bytes=1048576, deadline_s=.25)))
    _fields(queues, {'packets', 'bytes', 'deadline_s'}, set(), 'queues')
    for field in ('packets', 'bytes'):
        _number(queues[field], 'queues.' + field, integer=True, minimum=1)
    _number(queues['deadline_s'], 'queues.deadline_s', minimum=.000001)
    watchdog = _number(request.get('watchdog_s', 2), 'watchdog_s', minimum=.000001)
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
        item = request['channels'][channel]
        _fields(item, {'endpoint', 'radio'}, {'radio_namespace', 'required_goodput_bps'}, channel)
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
        _fields(radio, {'bind', 'peer'}, set(), channel + '.radio')
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
    return dict(schema_version=1, validation_scope='configuration_only', hardware_validation='not_performed',
                radio_model_equivalence='not_established',
                selected_controller=copy.deepcopy(catalog['controllers'][request['controller']]),
                selected_radio=copy.deepcopy(device_profile), adapters=adapters, channels=diagnostics,
                open_criteria=['two physical UARTs simultaneously', 'physical Ethernet UDP/TCP',
                               'controller MAVLink behavior and deadlines', 'shared modeled-radio path and no bypass',
                               'modem PHY/MAC equivalence', 'bounded queues under payload saturation'])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--profiles', type=Path, default=CATALOG)
    parser.add_argument('--output', type=Path)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--check', action='store_true')
    action.add_argument('--dry-run', action='store_true')
    action.add_argument('--prepare', action='store_true')
    args = parser.parse_args(argv)
    profile_bytes = args.profiles.read_bytes()
    result = resolve(json.loads(args.config.read_text(encoding='utf-8')), json.loads(profile_bytes))
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
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as error:
        print(f'HitL configuration: {error}', file=sys.stderr)
        raise SystemExit(2)
